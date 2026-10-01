#!/usr/bin/env python
"""ReformedBot — Slack bot for subreddit moderation.

Connects to Slack via Socket Mode (Slack Bolt) and exposes:
- On-demand commands: report, queue, mail, conv, hello, help
- Interactive Block Kit buttons: Approve, Remove, Warn User, Ban User
- A background polling thread that auto-posts new mod reports and modmail
  to configured Slack channels every ``POLL_INTERVAL`` seconds.

Any number of subreddits can be served at once: each is a :class:`Feed` with
its own pair of Slack channels, configured in a ``[Subreddit:<name>]`` section.

Configuration is read from ``slack.ini`` (see ``slack.ini.example``).
Reddit authentication is handled by PRAW via ``praw.ini``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import configparser

import praw
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk import WebClient as SlackWebClient

from reddit_actions import RedditActions

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s:%(name)s:%(filename)s:%(lineno)d: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
#
# Importing this module must stay side-effect-free: no network calls, no
# Reddit session, no reliance on slack.ini being present. Everything that
# talks to the outside world happens in ``_startup()``, which ``__main__``
# calls. That lets the test suite import the module and exercise its
# functions without credentials or a live Slack.
#
config = configparser.ConfigParser()
config.read('slack.ini')

slack_token: str = config.get('Default', 'API_TOKEN', fallback='')
app_token: str = config.get('Default', 'APP_TOKEN', fallback='')
signing_secret: str = config.get('Default', 'SIGNING_SECRET', fallback='')


def _resolve_channel(value: Optional[str], token: str) -> Optional[str]:
    """Resolve a configured channel to a Slack channel ID.

    Accepts either a channel ID (``C...``/``G...``) which is returned as-is, or
    a channel name (with or without a leading ``#``) which is looked up via the
    Slack API. Name lookup only finds public channels and private channels the
    bot has been invited to.

    Args:
        value: Raw ``slack.ini`` value, or ``None``/empty to disable the feed.
        token: Bot OAuth token used for the lookup.

    Returns:
        The channel ID, or ``None`` if the value was empty or unresolvable.
    """
    value = (value or "").strip().lstrip('#')
    if not value:
        return None
    if re.fullmatch(r"[CGD][A-Z0-9]{6,}", value):
        return value
    try:
        client = SlackWebClient(token=token)
        cursor = ""
        while True:
            resp = client.conversations_list(limit=1000, cursor=cursor, exclude_archived=True, types="public_channel,private_channel")
            for ch in resp.get("channels", []):
                if ch.get("name") == value:
                    return ch["id"]
            cursor = resp.get("response_metadata", {}).get("next_cursor", "")
            if not cursor:
                break
    except Exception as e:
        logging.error(f"Could not look up channel #{value}: {e}")
        return None
    logging.error(f"Channel #{value} not found — if it is private, invite the bot to it first")
    return None


def _is_configured(raw: Optional[str]) -> bool:
    """Return True if a channel was given a non-empty value in ``slack.ini``."""
    return bool((raw or "").strip())


# ---------------------------------------------------------------------------
# Feeds
# ---------------------------------------------------------------------------
#
# A Feed is one subreddit together with the two Slack channels its activity is
# pushed to. Everything that used to be "the" modqueue channel or "the" Reddit
# session hangs off a Feed instead, so serving a second subreddit is another
# entry in ``feeds`` rather than a second copy of the poll loop.
#
# The deduplication logs stay shared and keyed by channel ID: each feed writes
# under its own channels, so two subreddits never collide, and an existing log
# keeps working untouched.


class StatusMessage:
    """The single live status message a feed keeps at the bottom of one channel.

    Slack itself is the store. Nothing here is persisted: after a restart the
    message is found again in the channel (:func:`_adopt_status`) and taken
    over, so a restart edits the status already there instead of stranding it
    and posting a second one.

    ``body`` is the summary text without its trailing "Updated ..." line, and is
    what decides whether the channel needs a new message or only a refreshed
    timestamp — comparing rendered text is what makes an adopted message
    comparable with a freshly built one.
    """

    def __init__(self) -> None:
        """Start with no message: nothing posted, and nothing adopted yet."""
        self.ts: Optional[str] = None
        self.body: Optional[str] = None
        self.refreshed_at: float = 0.0
        self.adopted: bool = False  # whether the channel has been searched for a pre-restart message

    def shows(self, body: str) -> bool:
        """Return True if the live message already says exactly *body*.

        ``None`` (nothing known) is not a match for anything, including the
        empty-queue summary — conflating the two suppressed the all-clear
        notice after a restart.
        """
        return self.body is not None and self.body == body


# The praw.ini profile a feed uses when neither its own section nor [Default]
# names one — the single account every feed shared before this was configurable.
DEFAULT_REDDIT_ACCOUNT: str = "reformedbot"


class Feed:
    """One subreddit and the Slack channels its modqueue and modmail go to.

    Holds the per-feed state that used to be module-level: the resolved channel
    IDs, the ``RedditActions`` for the subreddit, and each channel's live
    status message.
    """

    def __init__(self, subreddit: str, raw_modqueue_channel: Optional[str], raw_modmail_channel: Optional[str], mods: Optional[Dict[str, str]] = None, controls: Optional[str] = None, reddit_account: Optional[str] = None) -> None:
        """Build a feed from its ``slack.ini`` configuration.

        Args:
            subreddit: Subreddit name without the ``r/`` prefix.
            raw_modqueue_channel: Raw MODQUEUE_CHANNEL value (name or ID).
            raw_modmail_channel: Raw MODMAIL_CHANNEL value (name or ID).
            mods: Slack user ID → Reddit username for moderators of this
                subreddit specifically, on top of the global ``[Mods]`` list.
            controls: Raw CONTROLS value (``vote``, ``actions``, or both).
                Parsed here; the resolved set is handed to this feed's
                ``RedditActions``, which builds the cards.
            reddit_account: The ``praw.ini`` profile whose Reddit account
                polls and acts on this subreddit. Defaults to
                ``DEFAULT_REDDIT_ACCOUNT``.
        """
        self.subreddit: str = subreddit
        self.reddit_account: str = (reddit_account or "").strip() or DEFAULT_REDDIT_ACCOUNT
        self.controls: frozenset = RedditActions.parse_controls(controls)
        # Raw values are kept so a channel that could not be resolved at startup
        # (Slack unreachable, bot not yet invited) can be retried by the poll
        # loop instead of staying disabled until the bot is restarted.
        self.raw_modqueue_channel: Optional[str] = raw_modqueue_channel
        self.raw_modmail_channel: Optional[str] = raw_modmail_channel
        # Resolved channel IDs. Left unresolved at import (resolution is a
        # network call); ``_startup()`` fills them in.
        self.modqueue_channel: Optional[str] = None
        self.modmail_channel: Optional[str] = None
        self.mods: Dict[str, str] = {k.upper(): v for k, v in (mods or {}).items()}
        # Built by ``_startup()`` so importing this module neither reads
        # praw.ini nor opens a session; tests assign a fake here instead.
        self.reddit: RedditActions = None  # type: ignore[assignment]

        # Each channel keeps its own live status message.
        self.queue_status: StatusMessage = StatusMessage()
        self.modmail_status: StatusMessage = StatusMessage()
        self.last_activity_at: float = 0.0  # time.time() of the last bot post to this feed

    def __repr__(self) -> str:
        """Identify the feed by subreddit and channels in log output."""
        return f"<Feed r/{self.subreddit} account={self.reddit_account} modqueue={self.modqueue_channel} modmail={self.modmail_channel} controls={sorted(self.controls)}>"

    @property
    def label(self) -> str:
        """Human-readable name used in log lines and pending-channel reports."""
        return f"r/{self.subreddit}"

    @property
    def modqueue_url(self) -> str:
        """Link to this subreddit's modqueue on Reddit, for the status message."""
        return f"https://www.reddit.com/mod/{self.subreddit}/queue"

    def is_configured(self) -> bool:
        """Return True if either channel was given a value in ``slack.ini``."""
        return _is_configured(self.raw_modqueue_channel) or _is_configured(self.raw_modmail_channel)

    def channels(self) -> List[str]:
        """Return this feed's resolved channel IDs."""
        return [c for c in (self.modqueue_channel, self.modmail_channel) if c]

    def owns(self, channel_id: str) -> bool:
        """Return True if *channel_id* is one of this feed's resolved channels."""
        return channel_id in self.channels()

    def pending(self) -> List[str]:
        """Return labels for this feed's configured but unresolved channels."""
        pending: List[str] = []
        if _is_configured(self.raw_modqueue_channel) and not self.modqueue_channel:
            pending.append(f"{self.label} MODQUEUE_CHANNEL")
        if _is_configured(self.raw_modmail_channel) and not self.modmail_channel:
            pending.append(f"{self.label} MODMAIL_CHANNEL")
        return pending

    def resolve(self, token: str) -> None:
        """Resolve any channel name still missing its ID. Safe to call repeatedly."""
        if _is_configured(self.raw_modqueue_channel) and not self.modqueue_channel:
            self.modqueue_channel = _resolve_channel(self.raw_modqueue_channel, token)
        if _is_configured(self.raw_modmail_channel) and not self.modmail_channel:
            self.modmail_channel = _resolve_channel(self.raw_modmail_channel, token)


def _mods_for_subreddit(cfg: configparser.ConfigParser, subreddit: str) -> Dict[str, str]:
    """Return the ``[Mods:<subreddit>]`` entries, if that section exists."""
    for section in cfg.sections():
        if section.lower() == f"mods:{subreddit.lower()}":
            return {uid.upper(): name for uid, name in cfg.items(section)}
    return {}


def _controls_for(cfg: configparser.ConfigParser, section: str) -> Optional[str]:
    """Return the raw CONTROLS value for a feed's section.

    A feed's own ``CONTROLS`` wins; ``[Default] CONTROLS`` sets the house rule
    for every feed that does not override it; absent from both leaves
    :meth:`RedditActions.parse_controls` to apply its default.
    """
    return cfg.get(section, 'CONTROLS', fallback=cfg.get('Default', 'CONTROLS', fallback=None))


def _reddit_account_for(cfg: configparser.ConfigParser, section: str) -> Optional[str]:
    """Return the raw REDDIT_ACCOUNT (a ``praw.ini`` profile) for a feed's section.

    Resolved like CONTROLS: the feed's own key, then ``[Default]``, then
    ``DEFAULT_REDDIT_ACCOUNT`` inside :class:`Feed`.
    """
    return cfg.get(section, 'REDDIT_ACCOUNT', fallback=cfg.get('Default', 'REDDIT_ACCOUNT', fallback=None))


def _load_feeds(cfg: configparser.ConfigParser) -> List[Feed]:
    """Build the configured feeds from ``slack.ini``.

    One ``[Subreddit:<name>]`` section per feed, each with its own
    MODQUEUE_CHANNEL and MODMAIL_CHANNEL. The older single-subreddit layout — a
    bare ``[Channels]`` section — is still honoured when no ``[Subreddit:...]``
    section exists, so an un-migrated slack.ini keeps working; the subreddit it
    names comes from ``[Default] SUBREDDIT`` (default ``reformed``).
    """
    feeds: List[Feed] = []
    for section in cfg.sections():
        if not section.lower().startswith("subreddit:"):
            continue
        name = section.split(":", 1)[1].strip().lstrip("/").removeprefix("r/")
        if not name:
            logging.warning(f"Ignoring [{section}]: no subreddit name")
            continue
        feeds.append(Feed(
            name,
            cfg.get(section, 'MODQUEUE_CHANNEL', fallback=None),
            cfg.get(section, 'MODMAIL_CHANNEL', fallback=None),
            _mods_for_subreddit(cfg, name),
            _controls_for(cfg, section),
            _reddit_account_for(cfg, section),
        ))

    if cfg.has_section('Channels'):
        if feeds:
            logging.warning("Ignoring the legacy [Channels] section — [Subreddit:...] sections take precedence")
        else:
            name = cfg.get('Default', 'SUBREDDIT', fallback='reformed')
            feeds.append(Feed(
                name,
                cfg.get('Channels', 'MODQUEUE_CHANNEL', fallback=None),
                cfg.get('Channels', 'MODMAIL_CHANNEL', fallback=None),
                _mods_for_subreddit(cfg, name),
                _controls_for(cfg, 'Channels'),
                _reddit_account_for(cfg, 'Channels'),
            ))
    return feeds


feeds: List[Feed] = _load_feeds(config)

_resolve_attempts: int = 0  # counts retry passes, to keep the failure log from repeating every poll


def _pending_channels() -> List[str]:
    """Return labels for channels configured in slack.ini but not yet resolved.

    A non-empty result means the bot could not reach Slack (or has not been
    invited to a private channel) and does not yet know all of its channel IDs.
    """
    return [label for feed in feeds for label in feed.pending()]


def _feed_for_channel(channel_id: str) -> Optional[Feed]:
    """Return the feed that owns *channel_id*, or None if no feed does."""
    for feed in feeds:
        if feed.owns(channel_id):
            return feed
    return None


def _retry_unresolved_channels() -> None:
    """Re-attempt resolution for any configured channel still missing its ID.

    Called once per poll so a bot started while Slack was unreachable starts
    working on its own once Slack comes back, with no restart. Resolution is
    skipped entirely when nothing is pending, so the normal path costs nothing.
    """
    global _resolve_attempts
    pending = _pending_channels()
    if not pending:
        return

    _resolve_attempts += 1
    for feed in feeds:
        feed.resolve(slack_token)

    resolved = [label for label in pending if label not in _pending_channels()]
    if resolved:
        logging.info(f"Channels resolved on retry: {', '.join(resolved)}")
    still_pending = _pending_channels()
    # _resolve_channel already logs each failure; repeat the summary sparingly
    # so a long outage does not fill the log at one line per poll.
    if still_pending and _resolve_attempts % 20 == 1:
        logging.warning(f"Still unresolved after {_resolve_attempts} attempt(s): {', '.join(still_pending)}")

# Slack user ID → Reddit username mapping (from the [Mods] section of
# slack.ini). These moderators may act in every feed; a [Mods:<subreddit>]
# section adds moderators for that subreddit alone (see Feed.mods).
mod_slack_ids: Dict[str, str] = {}
if config.has_section('Mods'):
    for slack_uid, reddit_name in config.items('Mods'):
        mod_slack_ids[slack_uid.upper()] = reddit_name

# ---------------------------------------------------------------------------
# Slack Bolt app
# ---------------------------------------------------------------------------
# token_verification_enabled=False: Bolt otherwise calls auth.test while
# constructing the App, which would make importing this module hit the network.
# The token is still verified for real when SocketModeHandler connects.
#
# The placeholders keep the import working when slack.ini is absent (tests) —
# Bolt refuses to build an App without a token. ``_startup()`` is what refuses
# to actually run the bot with an unconfigured token.
app: App = App(
    token=slack_token or "xoxb-not-configured",
    signing_secret=signing_secret or "not-configured",
    token_verification_enabled=False,
)


def _startup() -> None:
    """Perform the side-effecting initialisation the bot needs to run.

    Separated from import so the module can be imported by tests without
    credentials or network access. Opens one Reddit session per distinct
    ``praw.ini`` profile the feeds name, resolves channel names to IDs, lifts each feed's entries out of the
    pre-split shared logs, migrates any legacy done-state, and loads each
    subreddit's moderator list.
    """
    missing = [k for k, v in (("API_TOKEN", slack_token), ("APP_TOKEN", app_token)) if not v]
    if missing:
        raise SystemExit(f"slack.ini is missing required key(s): {', '.join(missing)}")
    if not feeds:
        raise SystemExit("slack.ini configures no subreddits — add a [Subreddit:<name>] section (see slack.ini.example)")

    # One PRAW session per Reddit account, not per feed: feeds that name the
    # same praw.ini profile share it, and with it one rate-limit budget.
    sessions: Dict[str, Any] = {}
    for feed in feeds:
        if feed.reddit_account not in sessions:
            # Reads praw.ini only — no network — so a missing profile is a
            # config mistake to report, not an outage to ride out.
            try:
                sessions[feed.reddit_account] = praw.Reddit(feed.reddit_account, user_agent=f'reformedbot user agent ({feed.reddit_account})')
            except configparser.NoSectionError:
                raise SystemExit(f"{feed.label} uses REDDIT_ACCOUNT = {feed.reddit_account}, but praw.ini has no [{feed.reddit_account}] section")
        feed.reddit = RedditActions(feed.subreddit, reddit=sessions[feed.reddit_account], controls=feed.controls)

    for feed in feeds:
        feed.resolve(slack_token)
        # Each subreddit owns its logs, so both of these are per feed. Adoption
        # needs resolved channel IDs; a channel still unresolved here is picked
        # up by the poll loop once it resolves.
        feed.reddit.adopt_legacy_logs(feed.channels())
        feed.reddit.migrate_done_state()
        feed.reddit.refresh_mod_list()
        logging.info(f"Feed {feed.label}: account={feed.reddit_account} modqueue={feed.modqueue_channel} modmail={feed.modmail_channel} controls={sorted(feed.controls) or 'none'}")


def _is_allowed_channel(channel_id: str) -> bool:
    """Return True if *channel_id* is one of the configured mod channels.

    Guards against interactions arriving from a message that outlived its
    channel's configuration — a feed repointed at a different channel leaves
    the old messages in place with working buttons, and clicking one would
    otherwise take a real Reddit action and write to the log under a stale key.

    Fails open in two cases:

    - No channels configured at all: the bot is not acting as a feed, so there
      is nothing to restrict.
    - A configured channel is not yet resolved: the allow-list is not known
      to be complete, and refusing here would disable buttons on messages
      posted before the outage. See ``_retry_unresolved_channels``.
    """
    if _pending_channels():
        return True
    allowed = [c for feed in feeds for c in feed.channels()]
    return not allowed or channel_id in allowed


def is_authorized_mod(slack_user_id: str, feed: Optional[Feed] = None) -> bool:
    """Return ``True`` if *slack_user_id* may moderate *feed*.

    Two lists apply: the global ``[Mods]`` section of ``slack.ini``, whose
    moderators may act in every feed, and a per-subreddit ``[Mods:<name>]``
    section, whose moderators may act in that feed alone. Comparison is
    case-insensitive (Slack user IDs are uppercased).

    Args:
        slack_user_id: Slack user ID string (e.g. ``'U0123456789'``).
        feed: Feed the interaction came from, or ``None`` to check the global
            list only.
    """
    uid = slack_user_id.upper()
    feed_ids = list(feed.mods.keys()) if feed else []
    logging.info(f"AUTH CHECK: user={uid!r}, global mods={list(mod_slack_ids.keys())}, {feed.label if feed else 'no feed'} mods={feed_ids}")
    return uid in mod_slack_ids or (feed is not None and uid in feed.mods)


def _mod_display_name(slack_user_id: str, feed: Optional[Feed] = None, default: Optional[str] = None) -> str:
    """Return the Reddit username configured for a Slack user.

    Looks in the feed's own ``[Mods:<subreddit>]`` list first, so a mod listed
    under two names is credited under the one for the subreddit being acted on.

    Args:
        slack_user_id: Slack user ID of the clicker.
        feed: Feed the interaction came from, if known.
        default: Returned when the user is in neither list; defaults to a
            Slack mention of the user.
    """
    uid = slack_user_id.upper()
    if feed and uid in feed.mods:
        return feed.mods[uid]
    return mod_slack_ids.get(uid, default if default is not None else f"<@{slack_user_id}>")


def _feed_for_action(channel: str) -> Optional[Feed]:
    """Return the feed an interaction from *channel* belongs to.

    Normally that is the feed that owns the channel. When no feed claims it —
    the guard fails open while a channel is unresolved, or nothing is
    configured — a single configured feed is unambiguous and is used anyway.
    With several feeds there is no honest guess: acting on one could take a
    Reddit action against the wrong subreddit, so this returns ``None``.
    """
    feed = _feed_for_channel(channel)
    if feed is None and len(feeds) == 1:
        return feeds[0]
    return feed


def _interaction_allowed(client: Any, channel: str, user_id: str, verb: str = "take moderation actions") -> Optional[Feed]:
    """Return the feed *user_id* may act on for an interaction from *channel*.

    Checks both gates every interactive handler needs: the message must live in
    a configured mod channel, and the clicker must be a moderator of that
    channel's subreddit. The rejection is reported ephemerally, so only the
    clicker sees it.

    Args:
        client: Slack WebClient.
        channel: Channel the interaction arrived from.
        user_id: Slack user ID of the clicker.
        verb: Phrase completing "You are not authorized to ..." in the notice.

    Returns:
        The :class:`Feed` owning *channel* — the handler needs it to reach the
        right subreddit — or ``None`` if the interaction was rejected. Callers
        must test for ``None`` rather than truthiness.
    """
    if not _is_allowed_channel(channel):
        logging.warning(f"Rejected interaction from unconfigured channel {channel} by {user_id}")
        client.chat_postEphemeral(channel=channel, user=user_id, text="This channel is not a configured mod feed — action ignored.")
        return None

    feed = _feed_for_action(channel)
    if feed is None:
        logging.warning(f"Cannot attribute channel {channel} to a subreddit — {len(feeds)} feed(s) configured, pending: {_pending_channels()}")
        client.chat_postEphemeral(channel=channel, user=user_id, text="This feed is still starting up — try again in a moment.")
        return None

    if not is_authorized_mod(user_id, feed):
        logging.warning(f"Unauthorized interaction from {user_id} in {channel} ({feed.label})")
        client.chat_postEphemeral(channel=channel, user=user_id, text=f"You are not authorized to {verb}.")
        return None
    return feed


# ---------------------------------------------------------------------------
# Modal builders
# ---------------------------------------------------------------------------

def build_remove_modal(
    item_id: str,
    item_type: str = "submission",
    channel: str = "",
    ts: str = "",
    reddit_link: str = "",
    reasons: Optional[List[Dict[str, str]]] = None,
    selected_reason_id: Optional[str] = None,
    initial_text: str = "",
    initial_notes: str = "",
    initial_delivery: Optional[str] = None,
    saved_metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the Slack modal view payload for the Remove action.

    Always shows a reason dropdown (Reddit presets + Custom), an editable
    removal message text area (auto-filled when a preset is selected), an
    optional Additional Notes field, and delivery radio buttons.

    Selecting a preset fires the ``removal_reason_selected`` action which calls
    ``views_update`` to populate the text area with the reason's template text.
    The mod can edit that text before submitting.

    Args:
        item_id: Reddit item ID (bare).
        item_type: ``'submission'`` or ``'comment'``.
        channel: Slack channel ID of the originating message.
        ts: Timestamp of the originating Slack message.
        reddit_link: Full Reddit permalink.
        reasons: List of ``{'id', 'title', 'message'}`` dicts from Reddit.
        selected_reason_id: Pre-select this option in the dropdown (used on update).
        initial_text: Pre-fill the removal message text area (used on update).
        initial_notes: Pre-fill the notes field (used on update).
        initial_delivery: Pre-select delivery radio button value (used on update).

    Returns:
        A Slack modal view dict suitable for ``client.views_open`` / ``views_update``.
    """
    reasons = reasons or []
    delivery_options = [
        {"text": {"type": "plain_text", "text": "Post Public"}, "value": "public"},
        {"text": {"type": "plain_text", "text": "Post Private"}, "value": "private"},
        {"text": {"type": "plain_text", "text": "Silent Remove"}, "value": "silent"},
    ]

    # Dropdown: preset reasons + Custom
    dropdown_options = [
        {"text": {"type": "plain_text", "text": r["title"][:75]}, "value": r["id"]}
        for r in reasons
    ] + [{"text": {"type": "plain_text", "text": "Custom"}, "value": "custom"}]

    reason_element: Dict[str, Any] = {
        "type": "static_select",
        "action_id": "removal_reason_selected",
        "placeholder": {"type": "plain_text", "text": "Select a reason..."},
        "options": dropdown_options,
    }
    if selected_reason_id:
        match = next((o for o in dropdown_options if o["value"] == selected_reason_id), None)
        if match:
            reason_element["initial_option"] = match

    text_element: Dict[str, Any] = {
        "type": "plain_text_input",
        "action_id": "removal_text",
        "multiline": True,
        "placeholder": {"type": "plain_text", "text": "Message sent to user (edit as needed)"},
    }
    if initial_text:
        text_element["initial_value"] = initial_text

    notes_element: Dict[str, Any] = {
        "type": "plain_text_input",
        "action_id": "notes_input",
        "multiline": True,
        "placeholder": {"type": "plain_text", "text": "Appended to message (optional)"},
    }
    if initial_notes:
        notes_element["initial_value"] = initial_notes

    delivery_element: Dict[str, Any] = {
        "type": "radio_buttons",
        "action_id": "delivery_input",
        "options": delivery_options,
    }
    if initial_delivery:
        delivery_match = next((o for o in delivery_options if o["value"] == initial_delivery), None)
        if delivery_match:
            delivery_element["initial_option"] = delivery_match

    return {
        "type": "modal",
        "callback_id": "removal_reason_submitted",
        "private_metadata": json.dumps(saved_metadata if saved_metadata is not None else {
            "item_id": item_id, "item_type": item_type,
            "channel": channel, "ts": ts, "reddit_link": reddit_link,
        }),
        "title": {"type": "plain_text", "text": "Remove"},
        "submit": {"type": "plain_text", "text": "Remove"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                # Optional in the view, conditionally required in the handler: a
                # Silent Remove sends the user nothing, so there is nothing for a
                # reason to fill in. Slack cannot make one input depend on
                # another, so `handle_removal_submitted` enforces the rule and
                # pushes an error back onto this block when it is not met.
                "type": "input",
                "block_id": "reason_select_block",
                "dispatch_action": True,
                "optional": True,
                "label": {"type": "plain_text", "text": "Removal Reason"},
                "hint": {"type": "plain_text", "text": "Not needed for a Silent Remove."},
                "element": reason_element,
            },
            {
                "type": "input",
                "block_id": "removal_text_block",
                "optional": True,
                "label": {"type": "plain_text", "text": "Removal Message"},
                "element": text_element,
            },
            {
                "type": "input",
                "block_id": "notes_block",
                "optional": True,
                "label": {"type": "plain_text", "text": "Additional Notes"},
                "element": notes_element,
            },
            {
                "type": "input",
                "block_id": "delivery_block",
                "label": {"type": "plain_text", "text": "Delivery"},
                "element": delivery_element,
            },
        ],
    }


def build_warn_modal(username: str, channel: str = "", ts: str = "", reddit_link: str = "", item_id: str = "") -> Dict[str, Any]:
    """Build the Slack modal view payload for the Warn User action.

    The modal collects a free-text warning message from the moderator.
    ``username``, ``channel``, ``ts``, and ``reddit_link`` are stored in
    ``private_metadata``.

    Args:
        username: Reddit username of the user to warn (without ``u/`` prefix).
        channel: Slack channel ID of the originating message.
        ts: Timestamp of the originating Slack message.
        reddit_link: Full Reddit permalink for building the confirmation header.

    Returns:
        A Slack modal view dict suitable for ``client.views_open``.
    """
    return {
        "type": "modal",
        "callback_id": "warn_submitted",
        "private_metadata": json.dumps({"username": username, "channel": channel, "ts": ts, "reddit_link": reddit_link, "item_id": item_id}),
        "title": {"type": "plain_text", "text": "Warn User"},
        "submit": {"type": "plain_text", "text": "Send Warning"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Sending warning to u/{username}*"}
            },
            {
                "type": "input",
                "block_id": "warn_block",
                "label": {"type": "plain_text", "text": "Warning Message"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "warn_input",
                    "multiline": True,
                    "placeholder": {"type": "plain_text", "text": "Your warning message to the user"}
                }
            }
        ]
    }


def build_ban_modal(username: str, channel: str = "", ts: str = "", reddit_link: str = "", item_id: str = "") -> Dict[str, Any]:
    """Build the Slack modal view payload for the Ban User action.

    The modal collects ban reason, optional duration (days), and an optional
    internal moderator note. ``username``, ``channel``, ``ts``, and
    ``reddit_link`` are stored in ``private_metadata``.

    Args:
        username: Reddit username of the user to ban (without ``u/`` prefix).
        channel: Slack channel ID of the originating message.
        ts: Timestamp of the originating Slack message.
        reddit_link: Full Reddit permalink for building the confirmation header.

    Returns:
        A Slack modal view dict suitable for ``client.views_open``.
    """
    return {
        "type": "modal",
        "callback_id": "ban_submitted",
        "private_metadata": json.dumps({"username": username, "channel": channel, "ts": ts, "reddit_link": reddit_link, "item_id": item_id}),
        "title": {"type": "plain_text", "text": "Ban User"},
        "submit": {"type": "plain_text", "text": "Ban"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Banning u/{username}*"}
            },
            {
                "type": "input",
                "block_id": "reason_block",
                "label": {"type": "plain_text", "text": "Ban Reason"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "reason_input",
                    "placeholder": {"type": "plain_text", "text": "Reason visible to moderators"}
                }
            },
            {
                "type": "input",
                "block_id": "duration_block",
                "label": {"type": "plain_text", "text": "Duration (days, leave blank for permanent)"},
                "optional": True,
                "element": {
                    "type": "plain_text_input",
                    "action_id": "duration_input",
                    "placeholder": {"type": "plain_text", "text": "e.g. 7 (leave blank = permanent)"}
                }
            },
            {
                "type": "input",
                "block_id": "note_block",
                "label": {"type": "plain_text", "text": "Mod Note (internal only)"},
                "optional": True,
                "element": {
                    "type": "plain_text_input",
                    "action_id": "note_input"
                }
            }
        ]
    }


def build_reply_modal(conv_id: str, channel: str = "", ts: str = "") -> Dict[str, Any]:
    """Build the Slack modal view payload for the modmail Reply action.

    The reply is sent from the mod team (``author_hidden=True``), not the
    individual moderator.  ``conv_id``, ``channel``, and ``ts`` are stored in
    ``private_metadata``.

    Args:
        conv_id: Reddit modmail conversation ID.
        channel: Slack channel ID of the originating message.
        ts: Timestamp of the originating Slack message.

    Returns:
        A Slack modal view dict suitable for ``client.views_open``.
    """
    return {
        "type": "modal",
        "callback_id": "reply_submitted",
        "private_metadata": json.dumps({"conv_id": conv_id, "channel": channel, "ts": ts}),
        "title": {"type": "plain_text", "text": "Reply"},
        "submit": {"type": "plain_text", "text": "Send Reply"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "_Sent from the mod team (your name will not be shown)_"},
            },
            {
                "type": "input",
                "block_id": "reply_block",
                "label": {"type": "plain_text", "text": "Message"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "reply_input",
                    "multiline": True,
                    "placeholder": {"type": "plain_text", "text": "Your reply..."},
                },
            },
        ],
    }


def _selected_value(body: Dict[str, Any]) -> str:
    """Return the value of the control that fired, button or dropdown alike.

    A button carries its payload in ``value``; a ``static_select`` carries it in
    ``selected_option.value``. Action handlers that serve both — Archive is a
    button, Unarchive is a one-option dropdown — read it through here rather
    than knowing which they were given.
    """
    action = (body.get("actions") or [{}])[0]
    selected = action.get("selected_option")
    if isinstance(selected, dict):
        return selected.get("value", "")
    return action.get("value", "") or ""


def _reddit_link_from_body(body: Dict[str, Any]) -> str:
    """Extract the Reddit permalink from the first section block of a message."""
    blocks = body.get("message", {}).get("blocks", [])
    if blocks and blocks[0].get("type") == "section":
        text = blocks[0].get("text", {}).get("text", "")
        m = re.search(r'<(https://(?:reddit|mod\.reddit)\.com[^|>]+)\|(?:View on Reddit|View)>', text)
        if m:
            return m.group(1)
    return ""


def _build_item_header(client: Any, item_id: str, reddit_link: str = "", channel: str = "", ts: str = "", queue_num: Optional[int] = None) -> str:
    """Return a single mrkdwn line: ``#N <reddit_link|id> (<slack_link|Orig message>)``.

    Args:
        client: Slack WebClient for fetching the message permalink.
        item_id: Reddit item ID (bare).
        reddit_link: Full Reddit permalink URL for the item.
        channel: Slack channel ID of the original message.
        ts: Timestamp of the original Slack message.
        queue_num: Queue position to show as ``#N`` prefix.
    """
    slack_link = ""
    if channel and ts:
        try:
            slack_link = client.chat_getPermalink(channel=channel, message_ts=ts)["permalink"]
        except Exception:
            pass
    num_part = f"#{queue_num} " if queue_num else ""
    item_part = f"<{reddit_link}|{item_id}>" if reddit_link else item_id
    slack_part = f"(<{slack_link}|Orig message>)" if slack_link else ""
    return " ".join(p for p in [num_part + item_part, slack_part] if p)


def _reddit_user_link(username: str) -> str:
    """Return a Slack mrkdwn link to a Reddit user profile, or '' if no username."""
    return f"<https://reddit.com/u/{username}|u/{username}>" if username else ""


def _mark_item_as_ban_hold(client: Any, feed: Feed, channel: str, item_id: str, item_data: Dict[str, Any], ts: str, now: float) -> None:
    """Note on an open card that Reddit resolved it while a ban vote stands.

    The item is gone from the Reddit modqueue, so it would normally be
    auto-marked done. A ban vote holds it open instead (see
    ``RedditActions.HOLD_OPEN_VOTES``), and this is what tells the channel why:
    the header says what happened on Reddit and that the ban question is still
    open, and the card keeps every control it had, Done included.

    Args:
        client: Slack WebClient.
        feed: Feed owning the message.
        channel: Modqueue channel ID.
        item_id: Reddit item ID (bare).
        item_data: The item's log entry.
        ts: Timestamp of the card to update.
        now: Unix time to record as when the notice went out.
    """
    reddit = feed.reddit
    try:
        mod_name, action = reddit.get_item_resolution(item_id, item_data.get("item_type", "submission"))
        emoji = reddit.action_emoji(action)
        acted = f"{emoji} {(action or 'resolved').upper()}"
        acted = f"{acted} — {mod_name}" if mod_name else f"{acted} ON REDDIT"
        status = f"{acted}{reddit.HEADER_SEP}{reddit.BAN_HOLD_STATUS}"

        hold_blocks = reddit.build_item_blocks_open(channel, item_id, status=status)
        if not hold_blocks:
            logging.warning(f"ban hold: no blocks available for {item_id}")
            return

        client.chat_update(channel=channel, ts=ts, blocks=hold_blocks, text="Mod report item (ban vote outstanding)")
        client.chat_postMessage(
            channel=channel,
            thread_ts=ts,
            text=":hourglass_flowing_sand: Handled on Reddit, but left open here — a ban vote is outstanding. Click *Done* once the ban is settled.",
        )
        reddit.set_item_slack_ts(channel, item_id, ts, blocks=hold_blocks)
        reddit.set_item_ban_hold_at(channel, item_id, now)
        logging.info(f"Held open (ban vote outstanding): {item_id} ({status})")
    except Exception as e:
        logging.warning(f"Could not mark ban hold for {item_id}: {e}")


def _mark_item_as_actioned(client: Any, feed: Feed, channel: str, ts: str, header_text: str, done_emoji: str = "") -> None:
    """Update a modqueue item message with a prominent status header and strip buttons.

    Rebuilds the message in its done state: status header, item details, the
    current vote tally, and a Re-open dropdown in place of the vote and
    moderation controls. The rebuilt blocks are cached in the log so later
    updates (e.g. the vote tally) do not resurrect the open state.

    Args:
        client: Slack WebClient.
        feed: Feed owning the message, used to reach its subreddit's log.
        channel: Channel ID containing the message.
        ts: Timestamp of the message to update.
        header_text: Short plain-text status shown in the header block.
        done_emoji: Emoji for the in-card DONE marker, so it says the same thing
            as the header's. Defaults to the gavel — a done state whose Reddit
            action is not known.
    """
    reddit = feed.reddit
    try:
        original_blocks: List[Dict[str, Any]] = []
        try:
            resp = client.conversations_history(channel=channel, latest=ts, inclusive=True, limit=1)
            messages = resp.get("messages", [])
            if messages:
                original_blocks = messages[0].get("blocks", [])
        except Exception as e:
            logging.warning(f"mark_item_as_actioned: could not fetch message {ts}: {e}")

        item_id = _item_id_from_blocks(original_blocks) or reddit.find_item_by_slack_ts(channel, ts)
        if not item_id:
            logging.warning(f"mark_item_as_actioned: could not identify item for ts {ts}")
            return

        new_blocks = reddit.build_item_blocks_done(channel, item_id, header_text, original_blocks, done_emoji=done_emoji)
        if not new_blocks:
            logging.warning(f"mark_item_as_actioned: no blocks available for {item_id}")
            return

        client.chat_update(channel=channel, ts=ts, blocks=new_blocks, text=header_text)
        reddit.set_item_slack_ts(channel, item_id, ts, blocks=new_blocks)
    except Exception as e:
        logging.warning(f"Could not mark item as actioned: {e}")


def _done_note_text(emoji: str, mod_reddit: str) -> str:
    """Return the thread note posted when a mod marks a modqueue card done.

    One function because the note is written twice: when Done is clicked, and
    again if the Reddit action turns up afterwards and the gavel has to become
    a ✅ or an ❌ (see _upgrade_done_status).
    """
    return f"{emoji} Marked done by {mod_reddit}"


def _upgrade_done_status(client: Any, feed: Feed, channel: str, item_id: str, item_data: Dict[str, Any], ts: str) -> bool:
    """Re-ask Reddit what happened to a done card that is still showing the gavel.

    A mod who clicks Done before acting on Reddit gets the gavel: at that
    moment nothing had happened there to name. The answer usually lands
    seconds later, and this carries it back into Slack — the header, the
    in-card DONE marker and the thread note all pick up the action's emoji, so
    a card never sits on a gavel that outlived its own uncertainty.

    Args:
        client: Slack WebClient.
        feed: Feed owning the card.
        channel: Modqueue channel ID.
        item_id: Reddit item ID (bare).
        item_data: The item's log entry.
        ts: Timestamp of the card.

    Returns:
        True when the card was re-stamped; False while Reddit still names no
        resolution, which is the permanent answer for an item deleted by its
        author or caught by the spam filter. The attempt is counted either way
        (:meth:`RedditActions.record_resolution_check`), which is what stops
        those from being asked about forever.
    """
    reddit = feed.reddit
    mod_name, action = reddit.get_item_resolution(item_id, item_data.get("item_type", "submission"))
    reddit.record_resolution_check(channel, item_id, action)
    if not action:
        return False

    emoji = reddit.action_emoji(action)
    # A card closed by hand keeps crediting the mod who clicked Done; one that
    # was auto-done takes the auto-done wording, since nobody clicked anything.
    done_by = item_data.get("done_by") or ""
    header = f"{emoji} DONE — {done_by}" if done_by else f"{emoji} DONE — {mod_name} ({action} on Reddit)"
    _mark_item_as_actioned(client, feed, channel, ts, header, done_emoji=emoji)

    note_ts = item_data.get("done_note_ts")
    if note_ts and done_by:
        try:
            client.chat_update(channel=channel, ts=note_ts, text=_done_note_text(emoji, done_by))
        except Exception as e:
            logging.warning(f"Could not update the done note for {item_id}: {e}")
    logging.info(f"Upgraded done status: {item_id} ({header})")
    return True


def _build_modmail_actions_block(conv_id: str, author: str) -> Dict[str, Any]:
    """Return the full modmail action dropdown block for a conversation.

    DORMANT — nothing emits this. Archive came back as its own button
    (``RedditActions.modmail_control_elements``); this is the shape the rest of
    the menu takes when Reply / Mute / Warn / Ban are revived with it.
    """
    return {
        "type": "actions",
        "block_id": f"modmail_{conv_id}_actions",
        "elements": [{
            "type": "static_select",
            "action_id": "modmail_action",
            "placeholder": {"type": "plain_text", "text": "Take action..."},
            "options": [
                {"text": {"type": "plain_text", "text": "Reply on Reddit"},     "value": f"reply|{conv_id}|{author}"},
                {"text": {"type": "plain_text", "text": "Archive on Reddit"},   "value": f"archive|{conv_id}|{author}"},
                {"text": {"type": "plain_text", "text": "Mute on Reddit"},      "value": f"mute|{conv_id}|{author}"},
                {"text": {"type": "plain_text", "text": "Warn User on Reddit"}, "value": f"warn|{conv_id}|{author}"},
                {"text": {"type": "plain_text", "text": "Ban User on Reddit"},  "value": f"ban|{conv_id}|{author}"},
            ],
        }],
    }


_DONE_MARKER_TEXT: str = ":completed: DONE :completed:"
_REOPENED_MARKER_TEXT: str = ":arrows_counterclockwise: *REOPENED*"

# The button on the modqueue status message. A channel message renders the same
# for everyone, so "what have I not voted on?" cannot be answered in the message
# itself; it takes a click, which tells us who is asking (see handle_my_unvoted).
_UNVOTED_ACTION: str = "my_unvoted"
_UNVOTED_BUTTON_TEXT: str = "Items I haven't voted on"


def _is_status_marker(block: Dict[str, Any]) -> bool:
    """Return True if *block* is a DONE/REOPENED marker section.

    The DONE marker's emoji follows the resolving action, so it is recognised
    by shape (``RedditActions.is_done_marker``) rather than by looking for the
    gavel — a card marked ``❌ DONE ❌`` still has to be stripped on rebuild.
    """
    if block.get("type") != "section":
        return False
    if RedditActions.is_done_marker(block):
        return True
    return "*REOPENED*" in block.get("text", {}).get("text", "")


def _strip_status_blocks(blocks: List[Dict[str, Any]], drop_actions: bool = False) -> List[Dict[str, Any]]:
    """Return *blocks* without the card's header and DONE/REOPENED marker.

    A conversation can change state several times (done, replied, archived on
    Reddit), and each change rebuilds the header and marker. Without stripping
    the previous pair first they stack up, leaving a message with several
    headers and several DONE markers.

    The header holds the card's title as well as its status, so every caller
    has to put one back — rebuilt from the log, not carried over — or the card
    loses its big first line. There is always exactly one.

    Args:
        blocks: Blocks of the message as it currently stands.
        drop_actions: Also remove ``actions`` blocks (the caller re-adds the
            ones that still apply).
    """
    return [
        b for b in blocks
        if b.get("type") != "header"
        and not _is_status_marker(b)
        and not (drop_actions and b.get("type") == "actions")
    ]


def _mark_conv_as_archived(client: Any, feed: Feed, channel: str, conv_id: str, author: str, header_text: str) -> None:
    """Mark a modmail conversation as archived on the top-level Slack message.

    Like _mark_conv_as_actioned but replaces the actions block with a single
    "Unarchive on Reddit" option instead of removing it entirely.

    Args:
        client: Slack WebClient.
        feed: Feed owning the conversation.
        channel: Channel ID containing the message.
        conv_id: Reddit modmail conversation ID.
        author: Reddit username of the conversation author (for the unarchive value).
        header_text: Short plain-text status shown in the header block.
    """
    conv_ts = feed.reddit.get_conv_info(channel, conv_id).get('slack_ts')
    if not conv_ts:
        logging.warning(f"_mark_conv_as_archived: no slack_ts for conv {conv_id}")
        return
    try:
        resp = client.conversations_history(channel=channel, latest=conv_ts, inclusive=True, limit=1)
        messages = resp.get("messages", [])
        if not messages:
            return
        original_blocks: List[Dict[str, Any]] = messages[0].get("blocks", [])
        kept = _strip_status_blocks(original_blocks, drop_actions=True)
        unarchive_block: Dict[str, Any] = {
            "type": "actions",
            "block_id": f"modmail_archived_{conv_id}",
            "elements": [{
                "type": "static_select",
                "action_id": "modmail_action",
                "placeholder": {"type": "plain_text", "text": "Options..."},
                "options": [
                    {"text": {"type": "plain_text", "text": "Unarchive on Reddit"}, "value": f"unarchive|{conv_id}|{author}"},
                ],
            }],
        }
        title = feed.reddit.conv_title_for(channel, conv_id)
        new_blocks = (
            [feed.reddit.header_block(feed.reddit.header_text(title, header_text))]
            + kept
            + [{"type": "section", "text": {"type": "mrkdwn", "text": _DONE_MARKER_TEXT}}]
            + [unarchive_block]
        )
        client.chat_update(channel=channel, ts=conv_ts, blocks=new_blocks, text=header_text)
    except Exception as e:
        logging.warning(f"Could not mark conv as archived: {e}")


def _restore_conv_after_unarchive(client: Any, feed: Feed, channel: str, conv_ts: str, conv_id: str, author: str) -> None:
    """Restore the top-level modmail message to its active state after unarchiving.

    Drops the archived status from the header and the DONE marker with it, and
    swaps the Unarchive-only actions block back to the full action menu.

    Args:
        client: Slack WebClient.
        feed: Feed owning the conversation.
        channel: Channel ID containing the message.
        conv_ts: Timestamp of the conversation's top-level Slack message.
        conv_id: Reddit modmail conversation ID.
        author: Reddit username of the conversation author.
    """
    try:
        resp = client.conversations_history(channel=channel, latest=conv_ts, inclusive=True, limit=1)
        messages = resp.get("messages", [])
        if not messages:
            return
        original_blocks: List[Dict[str, Any]] = messages[0].get("blocks", [])
        title = feed.reddit.conv_title_for(channel, conv_id)
        new_blocks: List[Dict[str, Any]] = []
        seen_header = False
        for b in original_blocks:
            if b.get("type") == "header":
                # Back to the plain title: the card keeps its big first line,
                # the archived status goes.
                if seen_header:
                    continue
                seen_header = True
                new_blocks.append(feed.reddit.header_block(title))
            elif _is_status_marker(b):
                continue  # remove the DONE marker
            elif b.get("block_id", "") == f"modmail_archived_{conv_id}":
                new_blocks.append({
                    "type": "actions",
                    "block_id": f"modmail_{conv_id}_done",
                    "elements": feed.reddit.modmail_control_elements(conv_id, author),
                })
            else:
                new_blocks.append(b)
        client.chat_update(channel=channel, ts=conv_ts, blocks=new_blocks, text="Unarchived")
    except Exception as e:
        logging.warning(f"Could not restore conv after unarchive: {e}")


def _mark_conv_as_actioned(client: Any, feed: Feed, channel: str, conv_id: str, header_text: str) -> None:
    """Update a modmail conversation's top-level Slack message with a status header.

    Looks up the stored slack_ts for the conversation, fetches that message,
    removes all action blocks, and prepends a header + DONE marker.

    Args:
        client: Slack WebClient.
        feed: Feed owning the conversation.
        channel: Channel ID containing the message.
        conv_id: Reddit modmail conversation ID.
        header_text: Short plain-text status shown in the header block.
    """
    conv_ts = feed.reddit.get_conv_info(channel, conv_id).get('slack_ts')
    if not conv_ts:
        logging.warning(f"_mark_conv_as_actioned: no slack_ts for conv {conv_id}")
        return
    try:
        resp = client.conversations_history(channel=channel, latest=conv_ts, inclusive=True, limit=1)
        messages = resp.get("messages", [])
        if not messages:
            return
        original_blocks: List[Dict[str, Any]] = messages[0].get("blocks", [])
        kept = _strip_status_blocks(original_blocks, drop_actions=True)
        title = feed.reddit.conv_title_for(channel, conv_id)
        new_blocks = (
            [feed.reddit.header_block(feed.reddit.header_text(title, header_text))]
            + kept
            + [{"type": "section", "text": {"type": "mrkdwn", "text": _DONE_MARKER_TEXT}}]
        )
        client.chat_update(channel=channel, ts=conv_ts, blocks=new_blocks, text=header_text)
    except Exception as e:
        logging.warning(f"Could not mark conv as actioned: {e}")


def _conv_id_from_blocks(blocks: List[Dict[str, Any]]) -> Optional[str]:
    """Recover a conversation ID from a modmail message's block IDs.

    Every modmail block ID is ``modmail_{conv_id}_{suffix}``. This is a
    fallback only: a card that has been marked done has had its actions blocks
    stripped, and those are the only blocks carrying an ID — so callers that
    know the conv_id must pass it rather than rely on this.
    """
    for b in blocks:
        bid = b.get("block_id", "")
        if bid.startswith("modmail_") and bid.count("_") >= 2:
            return bid.split("_")[1]
    return None


def _mark_conv_as_reopened(client: Any, feed: Feed, channel: str, conv_ts: str, conv_id: Optional[str] = None) -> None:
    """Rebuild a modmail card in its reopened state: title, REOPENED, controls.

    Called when a new user message arrives in a previously-done conversation,
    or when the poll loop detects a conversation was unarchived on Reddit.

    Rebuilt the same way as :func:`_mark_conv_as_actioned` — one header, the
    kept content, one marker, one controls block — so repeated state changes
    cannot stack, and a card that comes back open has the controls it needs.

    Args:
        client: Slack WebClient.
        feed: Feed owning the conversation.
        channel: Channel ID containing the message.
        conv_ts: Timestamp of the conversation's top-level Slack message.
        conv_id: Reddit modmail conversation ID. Both callers know it, and it
            cannot be recovered from a card that was marked done — that strips
            the actions blocks, which are the only ones carrying an ID. Without
            it the card came back titled just "REOPENED" and with no buttons.
    """
    try:
        resp = client.conversations_history(channel=channel, latest=conv_ts, inclusive=True, limit=1)
        messages = resp.get("messages", [])
        if not messages:
            return
        original_blocks: List[Dict[str, Any]] = messages[0].get("blocks", [])
        conv_id = conv_id or _conv_id_from_blocks(original_blocks)
        if not conv_id:
            logging.warning(f"_mark_conv_as_reopened: no conv_id for message {conv_ts}")

        title = feed.reddit.conv_title_for(channel, conv_id) if conv_id else ""
        kept = _strip_status_blocks(original_blocks, drop_actions=True)
        new_blocks: List[Dict[str, Any]] = (
            [feed.reddit.header_block(feed.reddit.header_text(title, feed.reddit.REOPENED_STATUS))]
            + kept
            + [{"type": "section", "text": {"type": "mrkdwn", "text": _REOPENED_MARKER_TEXT}}]
        )
        if conv_id:
            author = feed.reddit.get_conv_info(channel, conv_id).get('author', '')
            new_blocks.append({
                "type": "actions",
                "block_id": f"modmail_{conv_id}_done",
                "elements": feed.reddit.modmail_control_elements(conv_id, author),
            })

        client.chat_update(channel=channel, ts=conv_ts, blocks=new_blocks, text="REOPENED")
    except Exception as e:
        logging.warning(f"Could not mark conv as reopened: {e}")


# ---------------------------------------------------------------------------
# Action dropdown handler
# ---------------------------------------------------------------------------

@app.action("mark_done")
def handle_mark_done(ack: Any, body: Dict[str, Any], client: Any) -> None:
    """Mark a modqueue or modmail item as done in Slack."""
    ack()
    user_id: str = body["user"]["id"]
    channel: str = body["container"]["channel_id"]
    feed = _interaction_allowed(client, channel, user_id)
    if feed is None:
        return

    value: str = body["actions"][0]["value"]
    ts: str = body["container"]["message_ts"]
    mod_reddit = _mod_display_name(user_id, feed)

    try:
        parts = value.split("|", 2)
        kind = parts[0]
        item_id = parts[1] if len(parts) > 1 else ""
        extra = parts[2] if len(parts) > 2 else ""
    except (ValueError, IndexError):
        logging.warning(f"mark_done: unexpected value format: {value!r}")
        return

    if kind == "queue":
        # Say what actually happened on Reddit rather than a blanket check: the
        # mod normally clicks Done because they just approved or removed it. The
        # header, the in-card marker and the thread note all carry the same
        # emoji. When Reddit has no answer yet — the click landed first — all
        # three show the gavel and the reconcile pass upgrades them once it
        # does (see _upgrade_done_status), which is why the mod's name and the
        # note's ts are recorded here.
        item_type = feed.reddit.get_item_info(channel, item_id).get("item_type", "submission")
        _, action = feed.reddit.get_item_resolution(item_id, item_type)
        emoji = feed.reddit.action_emoji(action)
        _mark_item_as_actioned(client, feed, channel, ts, f"{emoji} DONE — {mod_reddit}", done_emoji=emoji)
        note_ts = ""
        try:
            note_ts = (client.chat_postMessage(channel=channel, thread_ts=ts, text=_done_note_text(emoji, mod_reddit)) or {}).get("ts", "")
        except Exception as e:
            logging.warning(f"mark_done: could not post the thread note for {item_id}: {e}")
        feed.reddit.set_item_done_at(channel, item_id, time.time(), action=action, done_by=mod_reddit, note_ts=note_ts)
        _check_queue_clear_and_post(client, feed)
    elif kind == "mail":
        conv_id = item_id
        _mark_conv_as_actioned(client, feed, channel, conv_id, f"✅ DONE — {mod_reddit}")
        client.chat_postMessage(channel=channel, thread_ts=ts, text=f":white_check_mark: Marked done by {mod_reddit}")
        feed.reddit.set_conv_done_at(channel, conv_id, time.time())


@app.action("modqueue_action")
def handle_modqueue_action(ack: Any, body: Dict[str, Any], client: Any) -> None:
    """Decline a click on a retired modqueue "Take action…" dropdown.

    The dropdown is no longer emitted (see
    ``RedditActions._build_take_action_element``), but a card posted while it
    was live still carries one and Slack will happily deliver the click. Every
    branch behind it wrote to Reddit for real, so it is declined here rather
    than left quietly working on a bot that no longer offers it. Modmail
    Archive/Unarchive is the only Reddit action still live.

    Args:
        ack: Slack Bolt acknowledgement callable.
        body: Full Slack action payload.
        client: Slack ``WebClient`` for API calls.
    """
    ack()
    user_id: str = body["user"]["id"]
    channel: str = body["container"]["channel_id"]
    logging.info(f"modqueue_action: declined — retired dropdown, channel={channel} user={user_id}")
    client.chat_postEphemeral(channel=channel, user=user_id, text="Reddit actions on modqueue items have been withdrawn — this is an older message. Act on Reddit directly, then mark the item Done here.")


# ---------------------------------------------------------------------------
# DORMANT — the modqueue Reddit actions.
#
# Approve / Ignore reports & Approve / Remove / Warn / Ban, as they worked when
# the Take action… dropdown was emitted. Nothing calls this: the dropdown is
# dormant in RedditActions._build_take_action_element and the registration
# above now declines instead. Kept, with the modals below, so reviving the
# feature is re-emitting the dropdown and putting the @app.action decorator
# back on this function — not writing it again.
#
# One thing to restore with it: the "Ignore reports & Approve" option had its
# own `ignore_reports` control in slack.ini, and this handler re-checked it so
# a card posted before the config changed could not use it. Both went with the
# dropdown; the branch below no longer re-checks anything.
# ---------------------------------------------------------------------------

def handle_modqueue_action_dormant(ack: Any, body: Dict[str, Any], client: Any) -> None:
    """Handle a selection from the modqueue "Take action…" dropdown.

    Unlike a vote or the Done button, every branch here writes to Reddit.
    Approve goes straight through; Remove, Warn, and Ban open a modal for the
    text they need, and the ``@app.view`` handlers below finish the job.

    Args:
        ack: Slack Bolt acknowledgement callable.
        body: Full Slack action payload.
        client: Slack ``WebClient`` for API calls.
    """
    ack()
    user_id: str = body["user"]["id"]
    channel: str = body["container"]["channel_id"]
    feed = _interaction_allowed(client, channel, user_id)
    if feed is None:
        return

    value: str = _selected_value(body)
    try:
        action, item_id, item_type, author = value.split("|", 3)
    except ValueError:
        logging.warning(f"modqueue_action: unexpected value format: {value!r}")
        return

    ts: str = body["container"]["message_ts"]
    reddit_link: str = _reddit_link_from_body(body)
    reddit = feed.reddit

    if action == "approve":
        logging.info(f"approve action: item_id={item_id} item_type={item_type} user={user_id} ({feed.label})")
        try:
            reddit.approve_item(item_id)
            mod_reddit = _mod_display_name(user_id, feed)
            _mark_item_as_actioned(client, feed, channel, ts, f"✅ APPROVED — {mod_reddit}", done_emoji=feed.reddit.action_emoji("approved"))
            client.chat_postMessage(channel=channel, thread_ts=ts, text=f":white_check_mark: *Approved* by {mod_reddit}")
            # Approving takes the item out of the Reddit modqueue, so record it
            # done here rather than waiting for the poll loop to notice.
            reddit.set_item_done_at(channel, item_id, time.time())
            _check_queue_clear_and_post(client, feed)
        except Exception as e:
            logging.error(f"approve failed: {e}")
            client.chat_postEphemeral(channel=channel, user=user_id, text=f"Failed to approve: {e}")

    elif action == "ignore_approve":
        logging.info(f"ignore_approve action: item_id={item_id} item_type={item_type} user={user_id} ({feed.label})")
        try:
            reddit.approve_and_ignore_reports(item_id, item_type)
            mod_reddit = _mod_display_name(user_id, feed)
            _mark_item_as_actioned(client, feed, channel, ts, f"✅ APPROVED — {mod_reddit} (reports ignored)", done_emoji=feed.reddit.action_emoji("approved"))
            client.chat_postMessage(channel=channel, thread_ts=ts, text=f":white_check_mark: *Approved* by {mod_reddit}, and future reports on this item are ignored")
            reddit.set_item_done_at(channel, item_id, time.time())
            _check_queue_clear_and_post(client, feed)
        except Exception as e:
            logging.error(f"ignore_approve failed: {e}")
            client.chat_postEphemeral(channel=channel, user=user_id, text=f"Failed to approve and ignore reports: {e}")

    elif action == "remove":
        try:
            client.views_open(
                trigger_id=body["trigger_id"],
                view=build_remove_modal(item_id, item_type, channel=channel, ts=ts, reddit_link=reddit_link, reasons=reddit.get_removal_reasons()),
            )
        except Exception as e:
            logging.error(f"remove views_open failed: {e}")
            client.chat_postEphemeral(channel=channel, user=user_id, text=f"Failed to open remove modal: {e}")

    elif action == "warn":
        try:
            client.views_open(
                trigger_id=body["trigger_id"],
                view=build_warn_modal(author, channel=channel, ts=ts, reddit_link=reddit_link, item_id=item_id),
            )
        except Exception as e:
            logging.error(f"warn views_open failed: {e}")
            client.chat_postEphemeral(channel=channel, user=user_id, text=f"Failed to open warn modal: {e}")

    elif action == "ban":
        try:
            client.views_open(
                trigger_id=body["trigger_id"],
                view=build_ban_modal(author, channel=channel, ts=ts, reddit_link=reddit_link, item_id=item_id),
            )
        except Exception as e:
            logging.error(f"ban views_open failed: {e}")
            client.chat_postEphemeral(channel=channel, user=user_id, text=f"Failed to open ban modal: {e}")

    else:
        logging.warning(f"modqueue_action: unknown action {action!r}")


@app.action("removal_reason_selected")
def handle_removal_reason_selected(ack: Any, body: Dict[str, Any], client: Any) -> None:
    """Populate the removal message text area when a preset reason is selected.

    Fires via ``dispatch_action`` on the reason dropdown.  Looks up the selected
    reason's template text and calls ``views_update`` to pre-fill the text area,
    preserving any notes or delivery selection the mod has already made.
    Selecting 'Custom' clears the text area.
    """
    ack()
    view = body["view"]
    selected_value: str = body["actions"][0]["selected_option"]["value"]
    state: Dict[str, Any] = view["state"]["values"]

    # Preserve any input the mod has already entered in other fields
    current_notes: str = state.get("notes_block", {}).get("notes_input", {}).get("value") or ""
    delivery_opt = state.get("delivery_block", {}).get("delivery_input", {}).get("selected_option")
    current_delivery: Optional[str] = delivery_opt["value"] if delivery_opt else None

    metadata_channel: str = json.loads(view["private_metadata"]).get("channel", "")
    feed = _feed_for_action(metadata_channel)
    if feed is None:
        logging.warning(f"removal_reason_selected: no feed owns channel {metadata_channel!r}")
        return
    reasons = feed.reddit.get_removal_reasons()
    if selected_value == "custom":
        reason_text = ""
    else:
        reason = next((r for r in reasons if r["id"] == selected_value), None)
        reason_text = reason["message"] if reason else ""

    metadata: Dict[str, Any] = json.loads(view["private_metadata"])
    metadata["reason_id"] = selected_value
    updated_view = build_remove_modal(
        item_id=metadata["item_id"],
        item_type=metadata.get("item_type", "submission"),
        channel=metadata.get("channel", ""),
        ts=metadata.get("ts", ""),
        reddit_link=metadata.get("reddit_link", ""),
        reasons=reasons,
        selected_reason_id=selected_value,
        initial_text=reason_text,
        initial_notes=current_notes,
        initial_delivery=current_delivery,
        saved_metadata=metadata,
    )
    try:
        client.views_update(view_id=view["id"], view=updated_view)
    except Exception as e:
        logging.error(f"removal_reason_selected: views_update failed: {e}")


@app.view("removal_reason_submitted")
def handle_removal_submitted(ack: Any, body: Dict[str, Any], client: Any) -> None:
    """Handle submission of the Remove modal form.

    Extracts item ID and removal reason from the modal state, calls
    ``reddit.remove_item``, and posts a confirmation to the modqueue channel.

    A reason is required only when the removal is being communicated. The
    acknowledgement is therefore deferred until the state has been read, so a
    removal that would send an empty message can be bounced back to the modal
    with an error instead of being accepted.

    Args:
        ack: Slack Bolt acknowledgement callable.
        body: Full Slack view-submission payload.
        client: Slack ``WebClient`` for API calls.
    """
    user_id: str = body["user"]["id"]
    metadata: Dict[str, str] = json.loads(body["view"]["private_metadata"])
    item_id: str = metadata["item_id"]
    item_type: str = metadata.get("item_type", "submission")
    channel: str = metadata.get("channel", "")
    ts: str = metadata.get("ts", "")
    reddit_link: str = metadata.get("reddit_link", "")
    values = body["view"]["state"]["values"]

    # Read from the unified modal layout
    selected_reason_opt = values.get("reason_select_block", {}).get("removal_reason_selected", {}).get("selected_option")
    reason_id: str = ""
    reason_title: str = ""
    if selected_reason_opt and selected_reason_opt["value"] != "custom":
        reason_id = selected_reason_opt["value"]
        reason_title = selected_reason_opt.get("text", {}).get("text", "")
    removal_text: str = values.get("removal_text_block", {}).get("removal_text", {}).get("value") or ""
    extra_notes: str = values.get("notes_block", {}).get("notes_input", {}).get("value") or ""

    # If the text area is empty (Slack doesn't update initial_value via views_update),
    # fall back to the saved reason_id from private_metadata so remove_item can look it up.
    saved_reason_id: str = metadata.get("reason_id", "") or reason_id
    if not removal_text and saved_reason_id and saved_reason_id != "custom":
        reason_id = saved_reason_id
        removal_text = ""  # let remove_item look up the text from Reddit

    notes: str = extra_notes  # extra notes only; removal_text handled via reason_id or passed separately
    if removal_text:
        notes = "\n\n".join(filter(None, [removal_text, extra_notes]))

    # Fall back to silent rather than to a delivery: an unintended message to
    # the user is the worse failure, and it is what remove_item defaults to.
    delivery: str = (values.get("delivery_block", {}).get("delivery_input", {}).get("selected_option") or {}).get("value", "silent")

    # A silent remove tells the user nothing, so it needs no reason. Any other
    # delivery does: without a preset or some text there is no message to send,
    # and the removal would go out silently in all but name.
    if delivery != "silent" and not reason_id and not removal_text:
        ack(response_action="errors", errors={"reason_select_block": "Pick a reason (or write a message below) — or choose Silent Remove."})
        return
    ack()

    feed = _feed_for_action(channel)
    if feed is None:
        logging.warning(f"removal_submitted: no feed owns channel {channel!r}")
        client.chat_postEphemeral(channel=channel or user_id, user=user_id, text=f"Could not remove `{item_id}`: this channel is not a configured mod feed.")
        return

    try:
        message_url = feed.reddit.remove_item(item_id, reason_id=reason_id, notes=notes, delivery=delivery, item_type=item_type)
        mod_reddit = _mod_display_name(user_id, feed)
        delivery_label = {"public": "public reply", "private": "private message", "silent": "silently"}.get(delivery, delivery)
        detail_parts = []
        if reason_title:
            detail_parts.append(f"Reason: {reason_title}")
        if extra_notes:
            detail_parts.append(f"Notes: {extra_notes}")
        detail_parts.append(f"Delivery: {delivery_label}")
        if message_url:
            detail_parts.append(f"<{message_url}|View Removal Message>")
        details = " | ".join(detail_parts)
        if channel and ts:
            _mark_item_as_actioned(client, feed, channel, ts, f"❌ REMOVED — {mod_reddit}", done_emoji=feed.reddit.action_emoji("removed"))
            client.chat_postMessage(channel=channel, thread_ts=ts, text=f":x: *Removed* by {mod_reddit}\n{details}")
        # Removing takes the item out of the Reddit modqueue. Recording it done
        # here stops the next reconcile pass treating it as resolved-on-Reddit
        # and overwriting the REMOVED header with a generic DONE one.
        if channel:
            feed.reddit.set_item_done_at(channel, item_id, time.time())
        _check_queue_clear_and_post(client, feed)
    except Exception as e:
        client.chat_postMessage(
            channel=feed.modqueue_channel or user_id,
            text=f"<@{user_id}> Failed to remove item `{item_id}`: {e}"
        )


@app.view("warn_submitted")
def handle_warn_submitted(ack: Any, body: Dict[str, Any], client: Any) -> None:
    """Handle submission of the Warn User modal form.

    Sends a modmail warning to the target Reddit user and posts a confirmation
    to the configured notification channel.

    Args:
        ack: Slack Bolt acknowledgement callable.
        body: Full Slack view-submission payload.
        client: Slack ``WebClient`` for API calls.
    """
    ack()
    user_id: str = body["user"]["id"]
    metadata: Dict[str, str] = json.loads(body["view"]["private_metadata"])
    username: str = metadata["username"]
    channel: str = metadata.get("channel", "")
    ts: str = metadata.get("ts", "")
    reddit_link: str = metadata.get("reddit_link", "")
    item_id: str = metadata.get("item_id", "")
    message: str = body["view"]["state"]["values"]["warn_block"]["warn_input"]["value"]

    feed = _feed_for_action(channel)
    if feed is None:
        logging.warning(f"warn_submitted: no feed owns channel {channel!r}")
        client.chat_postEphemeral(channel=channel or user_id, user=user_id, text=f"Could not warn u/{username}: this channel is not a configured mod feed.")
        return

    try:
        logging.info(f"warn_user: sending warning to u/{username} from {user_id} ({feed.label})")
        modmail_url = feed.reddit.warn_user(username, message)
        mod_reddit = _mod_display_name(user_id, feed)
        link_part = f" | <{modmail_url}|View Warning>" if modmail_url else ""
        if item_id and channel and ts:
            _append_action_note(client, feed, channel, item_id, ts, f":warning: *Warning sent* to u/{username} by {mod_reddit}{link_part}")
        if channel and ts:
            client.chat_postMessage(channel=channel, thread_ts=ts, text=f":warning: *Warning sent* by {mod_reddit}\nRecipient: u/{username}{link_part}")
    except Exception as e:
        logging.exception(f"warn_user failed for u/{username}: {e}")
        notify = channel or feed.modqueue_channel
        if notify:
            client.chat_postMessage(
                channel=notify,
                thread_ts=ts or None,
                text=f"<@{user_id}> Failed to warn u/{username}: {e}"
            )


@app.view("ban_submitted")
def handle_ban_submitted(ack: Any, body: Dict[str, Any], client: Any) -> None:
    """Handle submission of the Ban User modal form.

    Extracts ban reason, optional duration (days), and optional mod note from
    the modal state, then calls ``reddit.ban_user``. Posts a confirmation to
    the configured notification channel.

    Args:
        ack: Slack Bolt acknowledgement callable.
        body: Full Slack view-submission payload.
        client: Slack ``WebClient`` for API calls.
    """
    ack()
    user_id: str = body["user"]["id"]
    metadata: Dict[str, str] = json.loads(body["view"]["private_metadata"])
    username: str = metadata["username"]
    channel: str = metadata.get("channel", "")
    ts: str = metadata.get("ts", "")
    reddit_link: str = metadata.get("reddit_link", "")
    item_id: str = metadata.get("item_id", "")
    values: Dict[str, Any] = body["view"]["state"]["values"]
    reason: str = values["reason_block"]["reason_input"]["value"]
    duration_str: Optional[str] = values["duration_block"]["duration_input"].get("value")
    note: str = values["note_block"]["note_input"].get("value") or ""

    duration: Optional[int] = None
    if duration_str:
        try:
            duration = int(duration_str.strip())
        except ValueError:
            pass

    feed = _feed_for_action(channel)
    if feed is None:
        logging.warning(f"ban_submitted: no feed owns channel {channel!r}")
        client.chat_postEphemeral(channel=channel or user_id, user=user_id, text=f"Could not ban u/{username}: this channel is not a configured mod feed.")
        return

    try:
        logging.info(f"ban_user: banning u/{username} from {user_id} ({feed.label})")
        feed.reddit.ban_user(username, reason=reason, duration=duration, note=note)
        mod_reddit = _mod_display_name(user_id, feed)
        duration_label = f"{duration} days" if duration else "permanent"
        detail_parts = [f"Reason: {reason}", f"Duration: {duration_label}"]
        if note:
            detail_parts.append(f"Note: {note}")
        if item_id and channel and ts:
            link_part = f" | <{reddit_link}|View on Reddit>" if reddit_link else ""
            _append_action_note(client, feed, channel, item_id, ts, f":no_entry: *Banned* u/{username} ({duration_label}) by {mod_reddit}{link_part}")
        if channel and ts:
            client.chat_postMessage(channel=channel, thread_ts=ts, text=f":no_entry: *Banned* by {mod_reddit}\n{' | '.join(detail_parts)}")
    except Exception as e:
        logging.exception(f"ban_user failed for u/{username}: {e}")
        notify = channel or feed.modqueue_channel
        if notify:
            client.chat_postMessage(
                channel=notify,
                thread_ts=ts or None,
                text=f"<@{user_id}> Failed to ban u/{username}: {e}"
            )


# ---------------------------------------------------------------------------
# Modmail action dropdown handler
# ---------------------------------------------------------------------------

@app.action("modmail_action")
def handle_modmail_action(ack: Any, body: Dict[str, Any], client: Any) -> None:
    """Archive or unarchive a modmail conversation on Reddit.

    Serves the Archive button on an open card and the Unarchive dropdown that
    ``_mark_conv_as_archived`` leaves on an archived one — hence
    :func:`_selected_value`, which reads either shape.

    Both write to Reddit for the whole mod team, so both are gated on the
    feed's ``actions`` setting. Reply / Mute / Warn / Ban remain dormant here
    (see the modal note in CLAUDE.md).

    Args:
        ack: Slack Bolt acknowledgement callable.
        body: Full Slack action payload.
        client: Slack ``WebClient`` for API calls.
    """
    ack()
    user_id: str = body["user"]["id"]
    channel: str = body["container"]["channel_id"]
    feed = _interaction_allowed(client, channel, user_id)
    if feed is None:
        return
    if not feed.reddit.actions_enabled:
        logging.info(f"modmail_action: ignored — actions are off for {feed.label}")
        client.chat_postEphemeral(channel=channel, user=user_id, text="Reddit actions are switched off for this channel — this is an older message.")
        return

    value: str = _selected_value(body)
    try:
        action, conv_id, author = value.split("|", 2)
    except ValueError:
        logging.warning(f"modmail_action: unexpected value format: {value!r}")
        return

    ts: str = body["container"]["message_ts"]
    mod_reddit = _mod_display_name(user_id, feed)

    if action == "archive":
        try:
            feed.reddit.archive_conversation(conv_id)
            # Archiving on Reddit resolves the thread, so Slack follows: the
            # card gets the archived header and the Unarchive control. The
            # poll loop would otherwise report this back as a change made on
            # Reddit, which is the same end state by a slower route.
            _mark_conv_as_archived(client, feed, channel, conv_id, author, f"📥 ARCHIVED — {mod_reddit}")
            feed.reddit.set_conv_done_at(channel, conv_id, time.time())
            client.chat_postMessage(channel=channel, thread_ts=ts, text=f":inbox_tray: *Archived on Reddit* by {mod_reddit}")
        except Exception as e:
            logging.exception(f"archive failed for {conv_id}: {e}")
            client.chat_postEphemeral(channel=channel, user=user_id, text=f"Failed to archive: {e}")

    elif action == "unarchive":
        try:
            feed.reddit.unarchive_conversation(conv_id)
            conv_ts = feed.reddit.get_conv_info(channel, conv_id).get('slack_ts') or ts
            _restore_conv_after_unarchive(client, feed, channel, conv_ts, conv_id, author)
            feed.reddit.set_conv_done_at(channel, conv_id, None)
            client.chat_postMessage(channel=channel, thread_ts=conv_ts, text=f":outbox_tray: *Unarchived on Reddit* by {mod_reddit}")
        except Exception as e:
            logging.exception(f"unarchive failed for {conv_id}: {e}")
            client.chat_postEphemeral(channel=channel, user=user_id, text=f"Failed to unarchive: {e}")

    # DORMANT — reply / mute / warn / ban on modmail. The handlers they need
    # (handle_reply_submitted, warn/ban) are live; only the emitter and these
    # branches are missing. See "Modal flow for destructive actions".
    else:
        logging.warning(f"modmail_action: unknown or dormant action {action!r}")


@app.view("reply_submitted")
def handle_reply_submitted(ack: Any, body: Dict[str, Any], client: Any) -> None:
    """Handle submission of the modmail Reply modal.

    Sends a team reply (author hidden) to the Reddit modmail conversation and
    posts a confirmation to the configured modmail channel.

    Args:
        ack: Slack Bolt acknowledgement callable.
        body: Full Slack view-submission payload.
        client: Slack ``WebClient`` for API calls.
    """
    ack()
    user_id: str = body["user"]["id"]
    metadata: Dict[str, str] = json.loads(body["view"]["private_metadata"])
    conv_id: str = metadata["conv_id"]
    channel: str = metadata.get("channel", "")
    ts: str = metadata.get("ts", "")
    message: str = body["view"]["state"]["values"]["reply_block"]["reply_input"]["value"]

    feed = _feed_for_action(channel)
    if feed is None:
        logging.warning(f"reply_submitted: no feed owns channel {channel!r}")
        client.chat_postEphemeral(channel=channel or user_id, user=user_id, text=f"Could not reply to {conv_id}: this channel is not a configured mod feed.")
        return

    try:
        feed.reddit.reply_modmail(conv_id, message)
        mod_reddit = _mod_display_name(user_id, feed)
        reply_text = f":speech_balloon: *Reply sent* by {mod_reddit}\n{message}"
        if channel and ts:
            client.chat_postMessage(channel=channel, thread_ts=ts, text=reply_text)
        _mark_conv_as_actioned(client, feed, channel, conv_id, f"💬 REPLIED — {mod_reddit}")
        feed.reddit.set_conv_done_at(channel, conv_id, time.time())
    except Exception as e:
        notify_channel = feed.modmail_channel or feed.modqueue_channel
        if notify_channel:
            client.chat_postMessage(channel=notify_channel, text=f"<@{user_id}> Failed to send reply to {conv_id}: {e}")


# ---------------------------------------------------------------------------
# Vote button handler
# ---------------------------------------------------------------------------

@app.action(re.compile(r"^cast_vote"))
def handle_cast_vote(ack: Any, body: Dict[str, Any], client: Any) -> None:
    """Record a moderator's vote from the vote dropdown and update the main message tally.

    ack() is called immediately and all work runs in a background thread so the
    Bolt thread pool slot is freed at once.

    Args:
        ack: Slack Bolt acknowledgement callable.
        body: Full Slack action payload.
        client: Slack ``WebClient`` for API calls.
    """
    ack()

    user_id: str = body["user"]["id"]
    channel: str = body["container"]["channel_id"]
    value: str = body["actions"][0]["selected_option"]["value"]
    action_ts: float = float(body["actions"][0].get("action_ts", 0) or 0)
    dispatch_delay: float = round(time.time() - action_ts, 2) if action_ts else -1
    reddit_name: str = _mod_display_name(user_id, _feed_for_channel(channel), default=user_id)

    logging.info(f"cast_vote RECEIVED: user={reddit_name} delay={dispatch_delay}s value={value!r}")

    def _process() -> None:
        """Do the vote work off the Bolt thread, which has already acked."""
        feed = _interaction_allowed(client, channel, user_id, verb="vote")
        if feed is None:
            return
        if not feed.reddit.voting_enabled:
            # A card posted before voting was switched off still carries the
            # dropdown, so the setting is checked here too, not just at build.
            logging.info(f"cast_vote: ignored — voting is off for {feed.label}")
            client.chat_postEphemeral(channel=channel, user=user_id, text="Voting is switched off for this channel — this is an older message.")
            return
        reddit = feed.reddit

        try:
            item_id, item_type, vote_option = value.split("|", 2)
        except ValueError:
            logging.warning(f"cast_vote: unexpected value format: {value!r}")
            return

        item_info = reddit.get_item_info(channel, item_id)
        queue_num = item_info.get("queue_num", "?")
        logging.info(f"cast_vote PROCESSING: user={reddit_name} vote={vote_option} item=#{queue_num} ({item_id})")

        reddit.record_vote(channel, item_id, user_id, vote_option)
        votes = reddit.get_votes(channel, item_id)
        tally_text = reddit.format_vote_tally(votes)

        main_ts = item_info.get("slack_ts")
        cached_blocks: List[Dict[str, Any]] = item_info.get("slack_blocks", [])
        if main_ts and cached_blocks:
            tally_block_id = f"vote_tally_{item_id}"
            new_vote_action_id = f"cast_vote_{int(time.time())}"
            updated: List[Dict[str, Any]] = []
            for b in cached_blocks:
                if b.get("block_id") == tally_block_id:
                    updated.append({**b, "text": {"type": "mrkdwn", "text": tally_text}})
                elif b.get("block_id", "").startswith("actions_"):
                    # Rotate the cast_vote action_id so Slack treats it as a fresh
                    # element and clears the last-selected option from the dropdown.
                    new_elems = [
                        {**e, "action_id": new_vote_action_id}
                        if e.get("action_id", "").startswith("cast_vote") else e
                        for e in b.get("elements", [])
                    ]
                    updated.append({**b, "elements": new_elems})
                else:
                    updated.append(b)
            try:
                client.chat_update(channel=channel, ts=main_ts, blocks=updated, text="Mod report item")
                reddit.set_item_slack_ts(channel, item_id, main_ts, blocks=updated)
                logging.info(f"cast_vote DONE: tally updated for #{queue_num} — {tally_text!r}")
            except Exception as e:
                logging.error(f"cast_vote: chat_update failed for #{queue_num} ({item_id}): {e}")
        else:
            logging.warning(f"cast_vote: no cached blocks for #{queue_num} ({item_id}), tally not updated")

    threading.Thread(target=_process, daemon=True).start()


@app.action(_UNVOTED_ACTION)
def handle_my_unvoted(ack: Any, body: Dict[str, Any], client: Any) -> None:
    """Tell the clicking moderator which open items they have not voted on.

    The status message is one message shown to the whole channel, so it cannot
    answer this for anyone in particular.  A click can: Slack names the user who
    made it, and the reply goes back as an ephemeral, which only that user sees.

    Everything this reads is in the local store, so unlike
    :func:`handle_cast_vote` it needs no background thread — there is no Reddit
    call to keep off the Bolt thread.

    Args:
        ack: Slack Bolt acknowledgement callable.
        body: Full Slack action payload.
        client: Slack ``WebClient`` for API calls.
    """
    ack()
    user_id: str = body["user"]["id"]
    channel: str = body["container"]["channel_id"]

    feed = _interaction_allowed(client, channel, user_id, verb="vote")
    if feed is None:
        return
    if not feed.reddit.voting_enabled:
        # A status message posted before voting was switched off still carries
        # the button, and Slack will happily deliver the click.
        logging.info(f"my_unvoted: ignored — voting is off for {feed.label}")
        client.chat_postEphemeral(channel=channel, user=user_id, text="Voting is switched off for this channel — this is an older message.")
        return

    items = feed.reddit.items_without_vote_from(channel, user_id)
    logging.info(f"my_unvoted: {_mod_display_name(user_id, feed, default=user_id)} has {len(items)} unvoted item(s) in {feed.label}")
    if not items:
        client.chat_postEphemeral(channel=channel, user=user_id, text=":white_check_mark: You have voted on every open item.")
        return

    lines = [
        f"• {_queue_link(item, feed.reddit.item_title(item.get('queue_num'), item.get('item_type', ''), item.get('author', '')))}"
        for item in items
    ]
    client.chat_postEphemeral(channel=channel, user=user_id, text=f"You have not voted on {len(items)} open item(s):\n" + "\n".join(lines))


@app.action("reopen_item")
def handle_reopen_item(ack: Any, body: Dict[str, Any], client: Any) -> None:
    """Restore an actioned modqueue item to its original interactive state.

    Triggered by the Re-open dropdown appended after a moderation action.
    Re-fetches the Reddit item via PRAW, rebuilds the full Block Kit payload
    (vote dropdown, Done button, and the current vote tally), and updates the
    message in place. If the item can no longer be fetched from Reddit, the
    details of the existing message are reused instead.

    Args:
        ack: Slack Bolt acknowledgement callable.
        body: Full Slack action payload.
        client: Slack ``WebClient`` for API calls.
    """
    ack()
    user_id: str = body["user"]["id"]
    channel: str = body["container"]["channel_id"]
    feed = _interaction_allowed(client, channel, user_id)
    if feed is None:
        return
    reddit = feed.reddit

    value: str = body["actions"][0]["selected_option"]["value"]
    try:
        item_id, item_type = value.split("|", 1)
    except ValueError:
        logging.warning(f"reopen_item: unexpected value format: {value!r}")
        return

    ts: str = body["container"]["message_ts"]
    live_blocks: List[Dict[str, Any]] = body.get("message", {}).get("blocks", [])
    mod_reddit = _mod_display_name(user_id, feed)
    status = f"{reddit.REOPENED_STATUS} — {mod_reddit}" if mod_reddit else reddit.REOPENED_STATUS
    blocks = reddit.build_item_blocks_open(channel, item_id, live_blocks, status)
    if not blocks:
        client.chat_postEphemeral(channel=channel, user=user_id, text="Could not re-open item — it may no longer be accessible on Reddit.")
        return

    reddit.set_item_done_at(channel, item_id, None)
    try:
        client.chat_update(channel=channel, ts=ts, blocks=blocks, text="Mod report item (re-opened)")
        reddit.set_item_slack_ts(channel, item_id, ts, blocks=blocks)
        client.chat_postMessage(channel=channel, thread_ts=ts, text=f":arrows_counterclockwise: Re-opened by {mod_reddit}")
    except Exception as e:
        logging.warning(f"reopen_item: could not update message: {e}")


# ---------------------------------------------------------------------------
# Error and event handlers
# ---------------------------------------------------------------------------

@app.error
def handle_error(error: Exception, body: Dict[str, Any]) -> None:
    """Log any exception Bolt did not handle, with the payload that caused it.

    Args:
        error: The unhandled exception.
        body: The Slack payload being processed when it was raised.
    """
    logging.error(f"Bolt error: {error} | body: {body}")


@app.event("message")
def handle_message_noop() -> None:
    """Absorb ``message`` events.

    The bot has no text commands; registering a no-op stops Bolt logging an
    "unhandled request" warning for every message in its channels.
    """
    pass  # Absorb message events to prevent Bolt warnings


@app.event("app_mention")
def handle_mention_noop() -> None:
    """Absorb ``app_mention`` events. See :func:`handle_message_noop`."""
    pass  # Absorb app_mention events to prevent Bolt warnings


# ---------------------------------------------------------------------------
# Background polling thread
# ---------------------------------------------------------------------------

_SUMMARY_INTERVAL: int = 5 * 60  # seconds between queue summaries

# Each channel keeps one live status message rather than posting the same
# summary again and again: while the state is unchanged the message is edited
# in place on this cadence so its "Updated ..." line stays honest. The per-feed
# bookkeeping behind it lives on the Feed itself.
_STATUS_REFRESH_INTERVAL: int = 10 * 60

# Scheduled digest: a summary forced out at fixed times of day even when the
# state has not changed, so the channel gets a status check spread across the
# working day.
_DIGEST_TZ = ZoneInfo("America/New_York")
_DIGEST_HOURS: Tuple[int, ...] = (6, 10, 13, 17)  # local hours in _DIGEST_TZ
_DIGEST_WINDOW: int = 15 * 60  # only fire this long after the scheduled hour
_DIGEST_QUIET_PERIOD: int = 60 * 60  # skip the digest if the bot posted this recently
_last_digest_slot: Optional[str] = None  # "YYYY-MM-DD:H" of the last digest decision

def _item_id_from_blocks(blocks: List[Dict[str, Any]]) -> Optional[str]:
    """Extract the Reddit item ID from a modqueue Block Kit message.

    Checks every block ID that embeds the item ID, so the ID is still
    recoverable from a message in its done state (which has no ``actions_``
    block, only ``vote_tally_`` and ``reopen_``).
    """
    for prefix in ("actions_", "vote_tally_", "reopen_"):
        for block in blocks:
            bid = block.get("block_id", "")
            if bid.startswith(prefix):
                return bid[len(prefix):]
    return None


def _updated_line(now: Optional[float] = None) -> str:
    """Return the "Updated ..." footer stamped on every status message.

    The timestamp goes through Slack's ``<!date^...>`` token so each mod reads
    it in their own timezone; the pipe-delimited fallback is what clients that
    cannot render the token (and the notification text) show instead.
    """
    stamp = int(now if now is not None else time.time())
    fallback = datetime.fromtimestamp(stamp, _DIGEST_TZ).strftime("%b %-d at %-I:%M %p %Z")
    return f"_Updated <!date^{stamp}^{{date_short_pretty}} at {{time}}|{fallback}>_"


def _is_last_message(web_client: SlackWebClient, channel: str, ts: str) -> bool:
    """Return whether *ts* is currently the newest message in *channel*.

    Fails safe: if the history lookup does not work, report ``True`` so the
    caller edits in place rather than posting a message that may turn out to be
    a duplicate.
    """
    try:
        messages = web_client.conversations_history(channel=channel, limit=1).get("messages") or []
        return bool(messages) and messages[0].get("ts") == ts
    except Exception as e:
        logging.warning(f"Could not check whether {channel}/{ts} is the latest message: {e}")
        return True


# Signature of a status message in a channel's history. Every status message
# ends with the _updated_line() footer and nothing else the bot posts does, so
# this is what identifies one written by a previous run of the bot.
_STATUS_SIGNATURE: str = "_Updated <!date^"
_STATUS_SCAN_LIMIT: int = 100  # messages to search back through for it

# Slack's hard cap on the text of a single section block. The status message was
# plain text before it carried a button, and plain text has no such limit — a
# channel with hundreds of pending items would now have its whole message
# rejected rather than merely looking long, so the section is trimmed to fit.
# ``text=`` keeps the full body either way.
_SECTION_TEXT_MAX: int = 3000


def _queue_link(entry: Dict[str, Any], label: str = "") -> str:
    """Return a modqueue item's ``#12`` label, linked to its Slack card.

    Entries logged before the permalink was recorded — and any item that has not
    been posted to this channel — have nothing to link to and read as plain
    text, which is what the summary showed before the link existed.

    Args:
        entry: A modqueue log entry, or ``{}``.
        label: Text to show instead of ``#<queue_num>``.
    """
    label = label or f"#{entry.get('queue_num', '?')}"
    permalink = entry.get("slack_permalink")
    return f"<{permalink}|{label}>" if permalink else label


def _status_blocks(text: str, with_button: bool) -> List[Dict[str, Any]]:
    """Return the Block Kit payload for a status message showing *text*."""
    shown = text if len(text) <= _SECTION_TEXT_MAX else text[:_SECTION_TEXT_MAX - 1] + "…"
    blocks: List[Dict[str, Any]] = [{"type": "section", "text": {"type": "mrkdwn", "text": shown}}]
    if with_button:
        blocks.append({
            "type": "actions",
            "block_id": "status_actions",
            "elements": [{
                "type": "button",
                "action_id": _UNVOTED_ACTION,
                "text": {"type": "plain_text", "text": _UNVOTED_BUTTON_TEXT, "emoji": True},
                "value": "unvoted",
            }],
        })
    return blocks


def _adopt_status(web_client: SlackWebClient, channel: str, status: StatusMessage) -> bool:
    """Take over the status message a previous run left in *channel*.

    The ts of the live status message is held only in memory, so without this a
    restart abandons the message already in the channel and posts a second one —
    every restart leaving another stale "N item(s) still pending" behind. Slack
    is the store: the most recent bot message carrying the ``Updated ...``
    footer *is* this channel's status message, whichever process posted it.

    Reading its body back as well as its ts is what makes the restart silent:
    an unchanged queue then matches, so the message is refreshed in place
    rather than deleted and reposted.

    Runs once per channel per process, lazily on the first summary rather than
    at startup, so it uses the poll loop's client and costs nothing on a feed
    that never publishes.

    Returns:
        ``True`` once the channel's status message is known — adopted, or
        confirmed absent — and the caller may publish. ``False`` if the lookup
        itself failed, in which case the caller must not post: it does not yet
        know whether it would be posting a duplicate, and the next poll retries.
    """
    if status.adopted or status.ts:
        return True
    try:
        messages = web_client.conversations_history(channel=channel, limit=_STATUS_SCAN_LIMIT).get("messages") or []
    except Exception as e:
        logging.warning(f"Could not look for an existing status message in {channel}, deferring: {e}")
        return False

    status.adopted = True
    for message in messages:
        text = message.get("text") or ""
        if not message.get("bot_id") or _STATUS_SIGNATURE not in text:
            continue
        body, footer, _ = text.rpartition(f"\n{_STATUS_SIGNATURE}")
        status.ts = message.get("ts")
        status.body = body if footer else text
        logging.info(f"Adopted the status message already in {channel} ({status.ts})")
        return True
    logging.info(f"No status message found in {channel} — a new one will be posted")
    return True


def _publish_status(web_client: SlackWebClient, channel: str, status: StatusMessage, body: str, repost: bool, with_button: bool = False) -> None:
    """Show *body* as the channel's single live status message.

    The status message is always kept at the bottom of the channel, so it is
    edited in place only when it is still the newest message and the summary
    itself has not changed (the periodic timestamp refresh).  Otherwise the old
    one is deleted and a fresh one posted, which both surfaces a changed queue
    and stops the status from being stranded above newer reports.

    An edit of a message that is no longer there — deleted by hand — falls
    through to posting, so the feed repairs itself without a restart.

    ``text`` carries the whole body whether or not there are blocks, and is not
    a fallback detail: :func:`_adopt_status` reads a message back out of the
    channel by it, ``StatusMessage.shows`` compares against it, and it is what a
    notification previews.  The blocks are what make the button clickable.

    Args:
        with_button: Offer the "items I haven't voted on" button.  Decided by
            the caller — the modmail status has nothing to vote on, and neither
            does a feed that does not vote.

    Updates *status* in place with the ts and body now live in the channel.
    """
    text = f"{body}\n{_updated_line()}"
    blocks = _status_blocks(text, with_button)
    if status.ts and not repost and _is_last_message(web_client, channel, status.ts):
        try:
            web_client.chat_update(channel=channel, ts=status.ts, text=text, blocks=blocks)
            status.body = body
            return
        except Exception as e:
            logging.warning(f"Status refresh failed for {channel}/{status.ts}, posting a new one: {e}")
            status.ts = None  # the message is gone; nothing left to delete
    if status.ts:
        try:
            web_client.chat_delete(channel=channel, ts=status.ts)
        except Exception as e:
            logging.warning(f"Could not delete the old status message {channel}/{status.ts}: {e}")
    status.ts = web_client.chat_postMessage(channel=channel, text=text, blocks=blocks)["ts"]
    status.body = body


def _post_queue_summary(web_client: SlackWebClient, feed: Feed, force: bool = False) -> None:
    """Update the feed's modqueue channel status message with what is pending.

    Fetches the live modqueue from Reddit, looks up each item's Slack message
    permalink from the log, and keeps one status message in the feed's
    ``modqueue_channel`` rather than posting the same summary repeatedly: a
    changed queue is reposted at the bottom of the channel, an unchanged one
    only has its timestamp refreshed, at most once per
    ``_STATUS_REFRESH_INTERVAL``.

    Args:
        web_client: Slack WebClient.
        feed: Feed whose modqueue channel is being updated.
        force: Repost even when the state is unchanged.  Used by the scheduled
            digest so a channel with pending items still gets a status line.
            Ignored when the queue is clear.
    """
    channel = feed.modqueue_channel
    if not channel:
        return
    status = feed.queue_status
    try:
        current_ids = feed.reddit.get_current_modqueue_ids()
        now = time.time()

        # Pick up the status message left by a previous run before deciding
        # anything, so a restart compares against what the channel actually
        # shows instead of assuming it shows nothing.
        if not _adopt_status(web_client, channel, status):
            return

        # Both of these read the log's open items rather than Reddit's queue, so
        # an item held open past the modqueue by a ban vote still counts.
        open_entries = feed.reddit.open_items(channel)
        voted = feed.reddit.items_with_consensus(channel)

        if not current_ids:
            if open_entries:
                # The all-clear is about Reddit's queue, but a card can outlive
                # it — a ban vote holds one open, and so does an item Reddit has
                # dealt with that the next reconcile pass has not caught up to.
                # Saying "clear" over the top of those contradicts the line
                # below listing them, so it says what is actually left instead.
                still_open = " | ".join(_queue_link(e) for e in sorted(open_entries.values(), key=lambda e: e.get("queue_num") or 0))
                body = f":clock2: *{len(open_entries)} item(s) still open here* (nothing left in the Reddit mod queue): {still_open}"
            else:
                body = ":white_check_mark: Mod queue is clear."
            # A scheduled digest is a nudge about pending work.  A genuine
            # all-clear says nothing new, so it drops back to the ordinary
            # refresh; a card still open is work, and keeps the nudge.
            force = force and bool(open_entries)
        else:
            channel_data = feed.reddit.store.channel_items(channel)
            parts: List[str] = []
            current_ids = sorted(current_ids, key=lambda iid: channel_data.get(iid, {}).get("queue_num") or 0)
            for item_id in current_ids:
                parts.append(_queue_link(channel_data.get(item_id, {})))
            # The count links to the Reddit queue itself: a mod acting on the
            # backlog needs it, and hanging it off the existing line keeps the
            # status message one line rather than two.
            body = f":clock2: *<{feed.modqueue_url}|{len(current_ids)} item(s) still pending>:* {' | '.join(parts)}"

        # Where the mods have landed, on its own line: an item three of them
        # have voted to remove otherwise looks exactly like one nobody has
        # opened.  No control gate — a feed switched off voting can still hold
        # votes on older cards, which _wants_tally() keeps showing too.
        if voted:
            ready = [
                f"{_queue_link(item)} {feed.reddit.vote_emoji(item['key'])}{item['count']}"
                for item in voted
            ]
            body += f"\n:ballot_box_with_ballot: *Items with {feed.reddit.CONSENSUS_THRESHOLD}+ votes:* " + " | ".join(ready)

        unchanged = status.shows(body)
        if unchanged and not force and (not status.ts or now - status.refreshed_at < _STATUS_REFRESH_INTERVAL):
            return

        repost = force or not unchanged
        _publish_status(web_client, channel, status, body, repost=repost, with_button=feed.reddit.voting_enabled and bool(open_entries))
        status.refreshed_at = now
        # An in-place edit is silent, so it must not count as channel activity —
        # the digest's quiet check is about whether mods have seen something new.
        if repost:
            feed.last_activity_at = now
    except Exception as e:
        logging.error(f"Queue summary error ({feed.label}): {e}")


def _post_modmail_summary(web_client: SlackWebClient, feed: Feed, force: bool = False) -> None:
    """Update the feed's modmail channel status with its open conversations.

    Keeps one live status message, on the same terms as
    :func:`_post_queue_summary`.

    Args:
        web_client: Slack WebClient.
        feed: Feed whose modmail channel is being updated.
        force: Repost even when the state is unchanged.  Used by the scheduled
            digest so a channel with open threads still gets a status line.
            Ignored when nothing is open.
    """
    channel = feed.modmail_channel
    if not channel:
        return
    status = feed.modmail_status
    try:
        open_convs = feed.reddit.get_open_conversations(channel)
        now = time.time()
        if not _adopt_status(web_client, channel, status):
            return

        if not open_convs:
            body = ":white_check_mark: All modmail conversations are resolved."
            force = False  # nothing open; see _post_queue_summary
        else:
            parts: List[str] = []
            for conv in open_convs:
                label = f"#{feed.reddit.conv_label(conv.get('conv_num'))}. u/{conv['author']} — {conv['subject']}"
                link = conv.get("slack_permalink")
                parts.append(f"<{link}|{label}>" if link else label)
            body = f":speech_balloon: *{len(open_convs)} open modmail thread(s):*\n" + "\n".join(f"• {p}" for p in parts)

        unchanged = status.shows(body)
        if unchanged and not force and (not status.ts or now - status.refreshed_at < _STATUS_REFRESH_INTERVAL):
            return

        repost = force or not unchanged
        _publish_status(web_client, channel, status, body, repost=repost)
        status.refreshed_at = now
        if repost:
            feed.last_activity_at = now
    except Exception as e:
        logging.error(f"Modmail summary error ({feed.label}): {e}")


def _maybe_post_digest(web_client: SlackWebClient) -> bool:
    """Force a queue + modmail summary at the scheduled times of day.

    Fires once per hour in ``_DIGEST_HOURS`` (local to ``_DIGEST_TZ``), bypassing
    the normal "state unchanged" dedup so a channel with outstanding work gets a
    fresh status line at each check-in through the day.  A channel with nothing
    pending is left alone — the summary functions drop the force and fall back
    to the usual refresh, so an all-clear is never reposted just because the
    clock came round.  A feed the bot has posted to within
    ``_DIGEST_QUIET_PERIOD`` is skipped — its channels are not quiet, so a
    forced repeat would just be noise. The quiet check is per feed: a busy
    subreddit does not suppress the digest of a silent one.

    Each scheduled hour is only considered for ``_DIGEST_WINDOW`` seconds after
    it passes, and the decision is recorded either way, so a slow poll iteration
    still catches it but a restart later in the day does not re-fire it.

    Returns:
        ``True`` if a digest was posted for any feed.
    """
    global _last_digest_slot
    now_local = datetime.now(_DIGEST_TZ)
    for hour in _DIGEST_HOURS:
        scheduled = now_local.replace(hour=hour, minute=0, second=0, microsecond=0)
        if not timedelta(0) <= now_local - scheduled < timedelta(seconds=_DIGEST_WINDOW):
            continue

        slot = f"{now_local:%Y-%m-%d}:{hour}"
        if slot == _last_digest_slot:
            return False
        # Record the decision before acting so a suppressed digest is not
        # reconsidered on every poll for the rest of the window.
        _last_digest_slot = slot

        posted = False
        for feed in feeds:
            quiet_for = time.time() - feed.last_activity_at
            if quiet_for < _DIGEST_QUIET_PERIOD:
                logging.info(f"Digest {slot} ({feed.label}): skipped, last post was {int(quiet_for / 60)}m ago")
                continue
            logging.info(f"Digest {slot} ({feed.label}): posting forced summary")
            _post_queue_summary(web_client, feed, force=True)
            _post_modmail_summary(web_client, feed, force=True)
            posted = True
        return posted
    return False


def _append_action_note(client: Any, feed: Feed, channel: str, item_id: str, ts: str, note_text: str) -> None:
    """Append a note to a modqueue item message without removing its interactive elements.

    Used for actions (warn, ban) that are worth recording but don't resolve the
    item — it still needs to be approved or removed on Reddit.

    Args:
        client: Slack WebClient.
        feed: Feed owning the message.
        channel: Channel ID containing the message.
        item_id: Reddit item ID (bare), used to look up cached blocks.
        ts: Timestamp of the Slack message to update.
        note_text: Mrkdwn text for the note block appended to the message.
    """
    reddit = feed.reddit
    cached_blocks: List[Dict[str, Any]] = reddit.get_item_info(channel, item_id).get("slack_blocks", [])
    if not (ts and cached_blocks):
        return
    note_block: Dict[str, Any] = {"type": "section", "text": {"type": "mrkdwn", "text": note_text}}
    new_blocks = cached_blocks + [note_block]
    try:
        client.chat_update(channel=channel, ts=ts, blocks=new_blocks, text="Mod report item")
        reddit.set_item_slack_ts(channel, item_id, ts, blocks=new_blocks)
    except Exception as e:
        logging.warning(f"Could not append action note to {item_id}: {e}")


# Seconds to wait after an upstream 5xx before polling again. Shorter than a
# typical POLL_INTERVAL on purpose: a 5xx is usually a brief blip, so the
# feed retries sooner than it otherwise would rather than backing off.
_SERVER_ERROR_RETRY_DELAY: int = 15


def _is_server_error(exc: BaseException) -> bool:
    """Return True if *exc* represents an upstream 5xx from Reddit or Slack.

    Both client libraries hang the HTTP response off the exception: prawcore
    raises ``ServerError`` carrying a ``requests`` response, and slack_sdk
    raises ``SlackApiError`` carrying a ``SlackResponse``. Either way the
    status code is at ``exc.response.status_code``, so one check covers both.
    """
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status, int) and 500 <= status < 600:
        return True
    # prawcore.exceptions.ServerError is 5xx by definition, even if the
    # response object is not shaped as expected.
    return type(exc).__name__ == "ServerError"


def _check_queue_clear_and_post(client: Any, feed: Feed) -> None:
    """After a moderation action, post a queue-clear notice if the Reddit modqueue is now empty.

    Runs in a background thread so it does not block the action handler.
    If the queue still has items, nothing is posted (the regular interval handles it).

    Args:
        client: Slack WebClient.
        feed: Feed whose modqueue was just acted on.
    """
    def _run() -> None:
        """Check the live queue and post the all-clear if it is now empty."""
        try:
            current_ids = feed.reddit.get_current_modqueue_ids()
            if not current_ids:
                _post_queue_summary(client, feed)
        except Exception as e:
            logging.error(f"Post-action queue check error ({feed.label}): {e}")
    threading.Thread(target=_run, daemon=True).start()


# How many times a done card showing the gavel is re-asked what Reddit did to
# it (see _upgrade_done_status). One per poll, so this is ~20 minutes at the
# default interval — long enough for a mod who clicks Done and then goes and
# removes the post, bounded because an item that left the queue with nobody to
# credit never gets an answer and would otherwise be re-fetched forever.
_RESOLUTION_RECHECK_LIMIT: int = 40

# ...and how many of them are asked about in one poll. The modqueue log holds
# every item ever posted to the channel, so without this the first poll after
# an upgrade would re-fetch the entire history at once — the shape of request
# storm that has run this account into a 429 before.
_RESOLUTION_RECHECK_BATCH: int = 5


def _reconcile_modqueue_state(web_client: SlackWebClient, feed: Feed, poll_interval: int) -> bool:
    """Sync Slack done-state with the live Reddit modqueue for one feed.

    - Auto-done: item is no longer in the Reddit modqueue but Slack still shows
      it as open → mark it done in Slack automatically.
    - Auto-reopen: item was marked done in Slack but is still in the Reddit
      modqueue after a grace period (2× poll_interval) → reopen it in Slack.
    - Late resolution: item is done here but still showing the gavel → ask
      Reddit again what happened and re-stamp the card once it says.
    """
    reddit = feed.reddit
    modqueue_channel = feed.modqueue_channel
    if not modqueue_channel:
        return False
    changed = False
    try:
        current_id_set = set(reddit.get_current_modqueue_ids())
        channel_data = reddit.store.channel_items(modqueue_channel)
        now = time.time()
        grace = 2 * poll_interval
        recheck: List[Tuple[float, str, Dict[str, Any], str]] = []

        for item_id, item_data in channel_data.items():
            slack_ts = item_data.get("slack_ts")
            done_at = item_data.get("done_at")
            if not slack_ts:
                continue

            if done_at is None and item_id not in current_id_set and reddit.held_open_by_vote(item_data.get("votes")):
                # Gone from the modqueue, but a ban vote is outstanding — the
                # item is dealt with, the user is not, so leave the card open
                # for a mod to close by hand. Say so on the card, once: the
                # reconcile pass sees this again on every poll.
                if not item_data.get("ban_hold_at"):
                    _mark_item_as_ban_hold(web_client, feed, modqueue_channel, item_id, item_data, slack_ts, now)
                    changed = True

            elif done_at is None and item_id not in current_id_set:
                # Resolved on Reddit without using the bot — auto-mark done in
                # Slack, naming the mod who acted when Reddit records one.
                mod_name, action = reddit.get_item_resolution(item_id, item_data.get("item_type", "submission"))
                emoji = reddit.action_emoji(action)
                header = f"{emoji} DONE — {mod_name} ({action} on Reddit)" if mod_name else f"{reddit.DONE_EMOJI_DEFAULT} DONE (resolved on Reddit)"
                _mark_item_as_actioned(web_client, feed, modqueue_channel, slack_ts, header, done_emoji=emoji)
                reddit.set_item_done_at(modqueue_channel, item_id, now, action=action)
                logging.info(f"Auto-marked done: {item_id} ({header})")
                changed = True

            elif done_at is not None and item_id in current_id_set and (now - done_at) >= grace:
                # Still in Reddit modqueue after grace period — reopen in Slack.
                # Rebuilds from Reddit when possible, otherwise reuses the item
                # details of the cached done message.
                reopen_blocks = reddit.build_item_blocks_open(modqueue_channel, item_id, status=f"{reddit.REOPENED_STATUS} — still in modqueue")
                if reopen_blocks:
                    try:
                        web_client.chat_update(channel=modqueue_channel, ts=slack_ts, blocks=reopen_blocks, text="Mod report item (re-opened)")
                        web_client.chat_postMessage(channel=modqueue_channel, thread_ts=slack_ts, text=":arrows_counterclockwise: Re-opened by bot — item is still in the Reddit modqueue")
                        reddit.set_item_slack_ts(modqueue_channel, item_id, slack_ts, blocks=reopen_blocks)
                        reddit.set_item_done_at(modqueue_channel, item_id, None)
                        logging.info(f"Auto-reopened: {item_id} (still in Reddit modqueue after grace period)")
                        changed = True
                    except Exception as e:
                        logging.warning(f"Auto-reopen failed for {item_id}: {e}")

            elif (done_at is not None and item_id not in current_id_set
                  and not item_data.get("done_action")
                  and item_data.get("done_checks", 0) < _RESOLUTION_RECHECK_LIMIT):
                # Done here with the gavel, because Reddit had no answer when
                # the card was closed. Ask again: a mod who clicks Done and
                # then removes the post gets the ❌ a moment later instead of
                # keeping a gavel that never says what happened. Collected
                # rather than done inline — the log holds every item ever
                # posted, so this has to be rationed.
                recheck.append((done_at, item_id, item_data, slack_ts))

        # Newest first, a few per poll. A card a mod just closed is the one
        # they are still looking at, and it must not wait behind a backlog of
        # old items that will never name a resolver.
        for _, item_id, item_data, slack_ts in sorted(recheck, key=lambda c: c[0], reverse=True)[:_RESOLUTION_RECHECK_BATCH]:
            if _upgrade_done_status(web_client, feed, modqueue_channel, item_id, item_data, slack_ts):
                changed = True
    except Exception as e:
        logging.error(f"Reconcile modqueue state error ({feed.label}): {e}")
    return changed


def _poll_feed(web_client: SlackWebClient, feed: Feed, poll_interval: int) -> Tuple[bool, bool]:
    """Run one poll pass for a single feed.

    Posts new modqueue items and modmail messages to the feed's channels,
    reconciles Slack done-state against Reddit, and mirrors archive changes.
    Each phase is guarded separately so a failure in one does not stop the
    others, and no failure escapes to the caller: the loop must keep running.

    Args:
        web_client: Slack WebClient.
        feed: Feed to poll.
        poll_interval: Configured seconds between polls, used for the
            auto-reopen grace period.

    Returns:
        ``(changed, server_error)`` — whether anything about this feed's state
        changed (so its summary is worth refreshing), and whether an upstream
        5xx was seen (so the next poll comes sooner).
    """
    reddit = feed.reddit
    modqueue_channel = feed.modqueue_channel
    modmail_channel = feed.modmail_channel
    server_error = False
    modqueue_changed = False

    try:
        # Cheap after the first pass (each channel is looked at once per
        # process), and this is where a channel that only resolved after
        # startup gets its entries imported out of the older JSON logs.
        reddit.adopt_legacy_logs(feed.channels())
        # Due once a week; the stamp is in the database, so restarts do not
        # restart the schedule.
        reddit.maybe_export()
    except Exception as e:
        logging.error(f"Poller error (log housekeeping, {feed.label}): {e}")

    try:
        if modqueue_channel:
            total, blocks = reddit.get_modqueue(modqueue_channel, no_repost=True, as_blocks=True)
            logging.info(f"Modqueue ({feed.label}): {total} total, {len(blocks)} new to post")
            if blocks:
                # Forget what the status message says so it is reposted rather
                # than edited: these new item cards have pushed it up the
                # channel, and an edit up there is invisible.
                feed.queue_status.body = None
                feed.last_activity_at = time.time()
            for block_list in blocks:
                item_id = _item_id_from_blocks(block_list)
                # One post at a time: a card Slack rejects is left without a
                # slack_ts and rebuilt next poll, and must not take the rest
                # of the batch down with it.
                try:
                    resp = web_client.chat_postMessage(
                        channel=modqueue_channel,
                        blocks=block_list,
                        text="New mod report"
                    )
                except Exception as e:
                    server_error = server_error or _is_server_error(e)
                    logging.error(f"Poller error (posting modqueue item {item_id}, {feed.label}): {e}")
                    continue
                if item_id:
                    try:
                        permalink = web_client.chat_getPermalink(channel=modqueue_channel, message_ts=resp["ts"])["permalink"]
                    except Exception:
                        permalink = None
                    reddit.set_item_slack_ts(modqueue_channel, item_id, resp["ts"], permalink=permalink, blocks=block_list)
                logging.info(f"Posted modqueue item to {modqueue_channel}")

            # Reconcile Slack done-state with Reddit modqueue.
            # Auto-done: item left the Reddit queue but Slack still shows it open.
            # Auto-reopen: item marked done in Slack but still in Reddit queue after grace period.
            modqueue_changed = _reconcile_modqueue_state(web_client, feed, poll_interval)
    except Exception as e:
        server_error = server_error or _is_server_error(e)
        logging.error(f"Poller error (modqueue, {feed.label}): {e}")

    try:
        if modmail_channel:
            items = reddit.get_conversations(modmail_channel, as_blocks=True)
            logging.info(f"Modmail ({feed.label}): {len(items)} new message(s) to post")
            if items:
                feed.last_activity_at = time.time()
            # Track slack_ts for conv threads created in this batch so
            # subsequent messages in the same conv can be threaded correctly.
            batch_thread_ts: Dict[str, str] = {}
            for item in items:
                conv_id: str = item["conv_id"]
                thread_ts: Optional[str] = item["thread_ts"] or batch_thread_ts.get(conv_id)
                try:
                    resp = web_client.chat_postMessage(
                        channel=modmail_channel,
                        thread_ts=thread_ts,
                        blocks=item["blocks"],
                        text=item["text"],
                    )
                    if item["is_new_conv"]:
                        ts = resp["ts"]
                        try:
                            permalink = web_client.chat_getPermalink(channel=modmail_channel, message_ts=ts)["permalink"]
                        except Exception:
                            permalink = None
                        reddit.set_conv_slack_ts(modmail_channel, conv_id, ts, permalink=permalink)
                        batch_thread_ts[conv_id] = ts
                    if item["is_user_message"]:
                        if item.get("was_done"):
                            conv_ts = item["thread_ts"] or batch_thread_ts.get(conv_id)
                            if conv_ts:
                                _mark_conv_as_reopened(web_client, feed, modmail_channel, conv_ts, conv_id)
                        reddit.set_conv_done_at(modmail_channel, conv_id, None)
                    logging.info(f"Posted modmail {'conv' if item['is_new_conv'] else 'reply'} {conv_id} to {modmail_channel}")
                except Exception as e:
                    logging.error(f"Poller error posting modmail {conv_id}: {e}")
    except Exception as e:
        server_error = server_error or _is_server_error(e)
        logging.error(f"Poller error (modmail, {feed.label}): {e}")

    modmail_changed = False
    try:
        if modmail_channel:
            changes = reddit.sync_archived_conversations(modmail_channel)
            for conv in changes['archived']:
                conv_id = conv["conv_id"]
                conv_ts = conv["slack_ts"]
                by = conv.get("by") or ""
                header = f"🗄️ ARCHIVED on Reddit by {by}" if by else "🗄️ ARCHIVED (on Reddit)"
                note = f":file_cabinet: Archived on Reddit by {_reddit_user_link(by)}" if by else ":file_cabinet: Archived on Reddit"
                _mark_conv_as_actioned(web_client, feed, modmail_channel, conv_id, header)
                web_client.chat_postMessage(channel=modmail_channel, thread_ts=conv_ts, text=note)
                logging.info(f"Auto-archived modmail conv {conv_id} in Slack (by {by or 'unknown'})")
                modmail_changed = True
            for conv in changes['unarchived']:
                conv_id = conv["conv_id"]
                conv_ts = conv["slack_ts"]
                by = conv.get("by") or ""
                note = f":inbox_tray: Unarchived on Reddit by {_reddit_user_link(by)}" if by else ":inbox_tray: Unarchived on Reddit"
                _mark_conv_as_reopened(web_client, feed, modmail_channel, conv_ts, conv_id)
                web_client.chat_postMessage(channel=modmail_channel, thread_ts=conv_ts, text=note)
                logging.info(f"Auto-unarchived modmail conv {conv_id} in Slack (by {by or 'unknown'})")
                modmail_changed = True
    except Exception as e:
        server_error = server_error or _is_server_error(e)
        logging.error(f"Poller error (modmail archive sync, {feed.label}): {e}")

    return modqueue_changed or modmail_changed, server_error


def _poll_loop() -> None:
    """Continuously poll Reddit and push new items to configured Slack channels.

    Runs as a daemon thread started at bot launch. Polls every configured
    feed's modqueue and modmail conversations every ``POLL_INTERVAL`` seconds
    (configured in ``slack.ini``; default: 30). Uses the same deduplication
    logic as the on-demand commands so items are never posted twice to the
    same channel.

    Each feed pushes its subreddit's reports to its own MODQUEUE_CHANNEL and
    its modmail to its own MODMAIL_CHANNEL. Either channel can be omitted to
    disable that half of the feed.
    """
    poll_interval: int = int(config.get('Default', 'POLL_INTERVAL', fallback='30'))
    web_client: SlackWebClient = SlackWebClient(token=slack_token)
    last_summary: float = 0.0

    while True:
        # A channel that could not be resolved at startup (Slack unreachable,
        # or the bot not yet invited to a private channel) is retried here, so
        # the feed starts on its own once the problem clears.
        _retry_unresolved_channels()

        logging.info(f"Polling Reddit ({len(feeds)} feed(s))...")
        server_error = False
        changed: List[Tuple[Feed, bool]] = []
        for feed in feeds:
            # Moderator lists are read from Reddit and expire; a failed load
            # backs off on its own, so this is a no-op on most passes.
            if feed.reddit:
                feed.reddit.refresh_mod_list_if_due()
            feed_changed, feed_error = _poll_feed(web_client, feed, poll_interval)
            changed.append((feed, feed_changed))
            server_error = server_error or feed_error

        now = time.time()
        if _maybe_post_digest(web_client):
            last_summary = now
        else:
            # A feed whose state changed refreshes its own summary immediately;
            # the rest wait for the interval, so a busy subreddit does not
            # rewrite a quiet one's status message on every pass.
            due = now - last_summary >= _SUMMARY_INTERVAL
            for feed, feed_changed in changed:
                if feed_changed or due:
                    _post_queue_summary(web_client, feed)
                    _post_modmail_summary(web_client, feed)
            if due:
                last_summary = now

        # A 5xx is usually transient, so retry on the shorter delay instead of
        # waiting out a full poll interval.
        delay = _SERVER_ERROR_RETRY_DELAY if server_error else poll_interval
        if server_error:
            logging.warning(f"Upstream 5xx this pass — retrying in {delay}s")
        time.sleep(delay)


def _check_pidfile() -> None:
    """Prevent two instances from running out of the same directory.

    Writes a pidfile at ``reformedbot.pid`` in the current working directory.
    If the file exists and its PID belongs to a running process, logs an error
    and exits.  Stale pidfiles (process no longer running) are silently replaced.
    """
    pidfile = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reformedbot.pid")
    if os.path.exists(pidfile):
        try:
            existing_pid = int(open(pidfile).read().strip())
            os.kill(existing_pid, 0)  # signal 0: check if process exists, don't send anything
            logging.error(
                f"Another instance of ReformedBot is already running from this directory "
                f"(PID {existing_pid}, pidfile: {pidfile}). Exiting."
            )
            raise SystemExit(1)
        except (ProcessLookupError, PermissionError):
            pass  # process is gone — stale pidfile, safe to overwrite
        except ValueError:
            pass  # pidfile contents unreadable, overwrite it
    with open(pidfile, "w") as f:
        f.write(str(os.getpid()))
    import atexit
    atexit.register(lambda: os.path.exists(pidfile) and os.remove(pidfile))


if __name__ == "__main__":
    _check_pidfile()
    _startup()

    # Start on what is *configured*, not on what resolved — a channel that
    # could not be resolved at startup is retried inside the poll loop, so
    # gating on resolution here would strand it permanently.
    if any(feed.is_configured() for feed in feeds):
        poller_thread = threading.Thread(target=_poll_loop, daemon=True)
        poller_thread.start()
        logging.info(f"Polling thread started for {', '.join(feed.label for feed in feeds)}.")
        if _pending_channels():
            logging.warning(f"Starting with unresolved channel(s): {', '.join(_pending_channels())} — will retry each poll")
    else:
        logging.warning("No MODQUEUE_CHANNEL or MODMAIL_CHANNEL configured — polling disabled.")

    logging.info("Starting ReformedBot via Socket Mode...")
    SocketModeHandler(app, app_token).start()
