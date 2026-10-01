from __future__ import annotations

import logging
import praw
import json
import os
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from log_store import LogStore


def _as_int(value: Any, default: int = 0) -> int:
    """Return *value* as an int, falling back to *default* for junk or None.

    The logs have been written by several generations of this bot; a number
    that came back as a string or ``None`` must not take down a poll.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class _NumberCycle:
    """Hands out a channel's card numbers, rolling the log over at the cap.

    Modqueue reports are numbered ``#1``…``#999`` and modmail conversations
    lettered ``#A``…``#ZZ`` (702). Asking for a number past the cap archives
    that channel's entries and starts the numbering over at 1 — see
    :meth:`RedditActions.roll_log`.

    The counter is **persisted** (``counters.json``) rather than re-derived from
    the log, which is what the bot did before rollover existed. After a rollover
    the live log still holds the carried-over open entries with their high
    numbers, so ``max(...) + 1`` would hand out 999 again on the very next item.
    The old rule survives as the fallback for a log that has no counter yet.

    Numbers still held by an entry in the live log are skipped, so two cards a
    mod can see in the channel never share a label. If every number up to the
    cap is somehow still held even after a rollover, numbering continues past
    the cap rather than blocking the poll: the label is cosmetic, the item is not.
    """

    def __init__(self, actions: "RedditActions", kind: str, channel: str, entries: Dict[str, Any], num_key: str, cap: int) -> None:
        """Prepare a counter for one channel.

        Args:
            actions: The :class:`RedditActions` owning the logs, used to read
                and write the counter file and to perform a rollover.
            kind: ``'modqueue'`` or ``'modmail'`` — which log is being numbered.
            channel: Slack channel ID; numbering is per channel.
            entries: The channel's live log entries, used for the fallback
                counter and for the in-use numbers to skip.
            num_key: Field holding the number in an entry (``queue_num`` /
                ``conv_num``).
            cap: Last number of a cycle; asking past it rolls the log over.
        """
        self._actions = actions
        self._kind = kind
        self._channel = channel
        self._num_key = num_key
        self._cap = cap
        self.rolled: bool = False   # True once this allocation archived the log
        self.issued: int = 0        # numbers handed out, so save() can no-op

        state = actions.store.counters().get(kind, {}).get(channel) or {}
        self._cycle: int = _as_int(state.get("cycle"), 1) or 1
        self._next: int = _as_int(state.get("next"), 0) or self._highest(entries) + 1
        self._in_use: set = self._used(entries)

    def _numbers(self, entries: Dict[str, Any]) -> List[int]:
        """Return every number currently held by an entry in *entries*."""
        return [n for n in (_as_int(v.get(self._num_key), 0) for v in entries.values() if isinstance(v, dict)) if n]

    def _used(self, entries: Dict[str, Any]) -> set:
        """Return the set of numbers held by *entries*."""
        return set(self._numbers(entries))

    def _highest(self, entries: Dict[str, Any]) -> int:
        """Return the highest number held by *entries*, or 0."""
        return max(self._numbers(entries), default=0)

    def next(self) -> int:
        """Return the next free number, archiving the log if the cap is reached."""
        rolled_now = False
        while True:
            while self._next <= self._cap and self._next in self._in_use:
                self._next += 1
            if self._next <= self._cap or rolled_now:
                break
            self._in_use = self._used(self._actions.roll_log(self._kind, self._channel, self._cycle))
            self._cycle += 1
            self._next = 1
            self.rolled = rolled_now = True
        num = self._next
        self._in_use.add(num)
        self._next += 1
        self.issued += 1
        return num

    def save(self) -> None:
        """Persist the counter, unless no number was handed out.

        One row, whatever else the counters table holds.
        """
        if not self.issued:
            return
        self._actions.store.set_counter(self._kind, self._channel, self._next, self._cycle)


class RedditActions:
    """Encapsulates all Reddit API interactions for subreddit moderation.

    Connects to a subreddit via PRAW using the 'reformedbot' profile defined
    in ``praw.ini``. Tracks which items have been posted to Slack in this
    subreddit's own JSON logs (``logs/<subreddit>/modqueue.json`` and
    ``modmail.json``) to prevent duplicate posts. Reaching the numbering cap
    archives a channel's entries under ``logs/<subreddit>/archive/`` and starts
    the numbering over — see :meth:`roll_log`.

    One instance is one subreddit: the bot builds a separate ``RedditActions``
    per configured feed, so anything subreddit-specific (the moderator list
    below, the subreddit handle) has to be per instance, not per class.
    """

    @staticmethod
    def conv_label(conv_num: Optional[int]) -> str:
        """Return the letter label for a modmail conversation number.

        Modmail conversations are labelled with letters (A, B, C ... Z, AA, AB)
        to keep them visually distinct from modqueue reports, which are
        numbered. The stored ``conv_num`` remains an integer; this is purely a
        display concern.

        Args:
            conv_num: 1-based conversation number, or ``None`` if unassigned.

        Returns:
            The letter label, or ``'?'`` when *conv_num* is missing or invalid.
        """
        try:
            n = int(conv_num)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return "?"
        if n < 1:
            return "?"
        label = ""
        while n > 0:
            n, rem = divmod(n - 1, 26)
            label = chr(ord('A') + rem) + label
        return label

    # ------------------------------------------------------------------
    # Slack done-state
    #
    # Modqueue items and modmail conversations share one encoding: ``done_at``
    # holds the unix timestamp the entry was marked done in Slack, and is
    # absent (or ``None``) while it is still open. Older logs used
    # ``slack_done_at`` for items and ``status: 'open'|'done'`` for
    # conversations; ``migrate_done_state`` rewrites those in place at startup.
    # ------------------------------------------------------------------

    @staticmethod
    def is_done(entry: Optional[Dict[str, Any]]) -> bool:
        """Return True if a log *entry* is currently marked done in Slack."""
        if not entry:
            return False
        return entry.get("done_at") is not None

    @staticmethod
    def clear_done(entry: Dict[str, Any], reopened_at: Optional[float] = None) -> None:
        """Move a log *entry* from done back to open, recording when.

        Open is encoded as an absent ``done_at``, so reopening is a removal —
        which by itself leaves no trace that the entry was ever done.
        ``reopened_at`` is that trace: a record only, never a state predicate
        (:meth:`is_done` is the predicate), and it deliberately survives the
        entry being marked done again so the log keeps the last reopen.

        An entry that was not done is left alone: there is no reopen to record.

        Args:
            entry: The modqueue item or modmail conversation log entry.
            reopened_at: Unix timestamp to record; defaults to now.
        """
        if entry.pop("done_at", None) is None:
            return
        entry["reopened_at"] = time.time() if reopened_at is None else reopened_at
        # A reopened card has a fresh header, so the ban-hold notice (which is
        # posted once, see ``ban_hold_at``) is allowed to be posted again.
        entry.pop("ban_hold_at", None)
        # Everything recorded about the *last* done state goes with it: which
        # Reddit action it showed, how many times we asked for one, who clicked
        # Done and where their thread note is. A card done again starts the
        # resolution hunt over rather than inheriting a stale answer.
        for key in ("done_action", "done_checks", "done_by", "done_note_ts"):
            entry.pop(key, None)

    def is_mod(self, username: str) -> bool:
        """Return True if *username* moderates this instance's subreddit.

        Reddit preserves the case a username was registered with, so the
        comparison against ``mod_list`` is case-insensitive.
        """
        return (username or "").lower() in {m.lower() for m in self.mod_list}

    # How long a fetched moderator list is trusted before it is reloaded, and
    # how soon a failed load is retried. A stale list only misreads a brand-new
    # mod's modmail reply as a user message, so a long TTL is fine; a *missing*
    # list misreads every mod reply, so failures retry far sooner.
    _MOD_LIST_TTL: float = 6 * 60 * 60
    _MOD_LIST_RETRY_DELAY: float = 5 * 60

    def refresh_mod_list(self) -> List[str]:
        """Reload this subreddit's moderator list from Reddit.

        The list differs per subreddit and changes over time, so it is read
        from Reddit rather than configured. A failed or empty response keeps
        whatever list is already loaded — a stale list is much better than an
        empty one, which would make every moderator look like a user.

        Returns:
            The moderator usernames now in effect.
        """
        try:
            names = [str(m) for m in self.sub.moderator()]
        except Exception as e:
            self._mod_list_next_refresh = time.time() + self._MOD_LIST_RETRY_DELAY
            logging.warning(f"Could not load the r/{self.subreddit_name} moderator list: {e}")
            return self.mod_list
        if not names:
            self._mod_list_next_refresh = time.time() + self._MOD_LIST_RETRY_DELAY
            logging.warning(f"r/{self.subreddit_name} returned no moderators — keeping the previous list")
            return self.mod_list
        self.mod_list = names
        self._mod_list_next_refresh = time.time() + self._MOD_LIST_TTL
        logging.info(f"r/{self.subreddit_name}: loaded {len(names)} moderator(s)")
        return self.mod_list

    def refresh_mod_list_if_due(self) -> None:
        """Reload the moderator list when its TTL (or a failure backoff) has passed."""
        if time.time() >= self._mod_list_next_refresh:
            self.refresh_mod_list()

    # Maps Slack emoji names to vote keys
    EMOJI_VOTE_MAP: Dict[str, str] = {
        "white_check_mark": "approve",
        "x":                "remove",
        "thought_balloon":  "discuss",
        "man-shrugging":    "meh",
        "party_hammer":     "ban",
        "completed":        "action_completed",
        "lock":             "lock_thread",
        "shell":            "warn",
        "spam":             "spam",
        "question":         "dont_understand",
    }

    # ------------------------------------------------------------------
    # Card controls
    #
    # Which interactive controls a card carries, set per feed by ``CONTROLS``
    # in slack.ini. A team that decides together wants the vote dropdown; a
    # team that also wants to act from Slack wants ``actions``, which adds the
    # modmail Archive/Unarchive controls. Either, both, or neither.
    #
    # ``actions`` used to put a Take action… dropdown on modqueue cards as well
    # (approve/remove/warn/ban on Reddit for real). That was withdrawn — see
    # _build_take_action_element, which is kept but no longer emitted.
    #
    # The Done button is not one of these: every card gets one, so an item can
    # always be closed out in Slack whatever else it offers.
    # ------------------------------------------------------------------

    CONTROL_VOTE: str = "vote"
    CONTROL_ACTIONS: str = "actions"
    VALID_CONTROLS: Tuple[str, ...] = (CONTROL_VOTE, CONTROL_ACTIONS)
    # What a feed gets when slack.ini says nothing — the vote dropdown, which is
    # what every card carried before CONTROLS existed.
    DEFAULT_CONTROLS: Tuple[str, ...] = (CONTROL_VOTE,)

    @classmethod
    def parse_controls(cls, raw: Optional[str]) -> frozenset:
        """Parse a ``CONTROLS`` value from slack.ini into a set of control names.

        Accepts a comma- or space-separated list (``vote``, ``actions``,
        ``vote, actions``). An unknown name is dropped with a warning rather
        than taken as "everything off" — a typo should cost one control, not
        silently strip the card.

        Args:
            raw: The configured value, or ``None`` when the key is absent.

        Returns:
            The control names, as a frozenset. Absent means
            :attr:`DEFAULT_CONTROLS`; present but empty means no controls
            beyond the Done button, which is a legitimate choice.
        """
        if raw is None:
            return frozenset(cls.DEFAULT_CONTROLS)
        names = [n.strip().lower() for n in raw.replace(",", " ").split()]
        unknown = [n for n in names if n not in cls.VALID_CONTROLS]
        if unknown:
            logging.warning(f"Ignoring unknown CONTROLS value(s) {', '.join(unknown)} — valid: {', '.join(cls.VALID_CONTROLS)}")
        return frozenset(n for n in names if n in cls.VALID_CONTROLS)

    def __init__(self, subreddit: str, no_repost: bool = False, reddit: Optional[Any] = None, log_dir: str = "logs", mod_list: Optional[List[str]] = None, controls: Optional[Any] = None) -> None:
        """Initialise the Reddit connection and subreddit handle.

        Args:
            subreddit: Name of the subreddit to moderate (e.g. ``'reformed'``).
            no_repost: When ``True``, ``get_modqueue`` skips items already
                posted to Slack by default. Can be overridden per-call.
            reddit: Pre-built PRAW ``Reddit`` instance. Defaults to building one
                from the ``reformedbot`` profile in ``praw.ini``; tests inject a
                fake so no network or credentials are needed. Several instances
                can share one session — the account is the same for every
                subreddit the bot serves.
            log_dir: Root directory holding the JSON logs. Each subreddit gets
                its own directory beneath it (``<log_dir>/<subreddit>/``). Tests
                point this at a temporary directory to keep the real ``logs/``
                untouched.
            mod_list: Seed moderator list, used until :meth:`refresh_mod_list`
                reads the real one from Reddit.
            controls: Which card controls this feed offers (see
                :attr:`VALID_CONTROLS`). Defaults to :attr:`DEFAULT_CONTROLS`.
        """
        self._reddit = reddit if reddit is not None else praw.Reddit('reformedbot', user_agent='reformedbot user agent')
        self.subreddit_name: str = subreddit
        self.sub = self._reddit.subreddit(subreddit)
        self.posted_to_slack: Dict[str, Any] = {}
        self.no_repost: bool = no_repost
        self.log_dir: str = log_dir
        # Opened lazily: constructing this class must not create a database, so
        # that building one for a subreddit the bot never polls costs nothing.
        self._store: Optional[LogStore] = None
        # Channels whose slice of the pre-split JSON logs has already been
        # looked for; see :meth:`adopt_legacy_logs`.
        self._legacy_checked: set = set()
        self.export_interval: float = self.EXPORT_INTERVAL_DAYS * 86400
        self.export_keep: int = self.EXPORT_KEEP
        self.mod_list: List[str] = list(mod_list or [])
        self._mod_list_next_refresh: float = 0.0  # 0 = never loaded, refresh at the first opportunity
        self.controls: frozenset = frozenset(controls) if controls is not None else frozenset(self.DEFAULT_CONTROLS)

    @property
    def voting_enabled(self) -> bool:
        """Return True if this feed's cards carry the vote dropdown and tally."""
        return self.CONTROL_VOTE in self.controls

    @property
    def actions_enabled(self) -> bool:
        """Return True if this feed's modmail cards carry Archive/Unarchive.

        These act on Reddit for the whole mod team rather than on Slack alone,
        which is why they are opt-in. Modqueue cards carry no Reddit action of
        their own — see the note on :attr:`CONTROL_ACTIONS`.
        """
        return self.CONTROL_ACTIONS in self.controls

    # ------------------------------------------------------------------
    # Block Kit builders
    # ------------------------------------------------------------------

    # (key, display_label) — key is used in button values/action_ids (no special chars)
    VOTE_OPTIONS: List[Tuple[str, str]] = [
        ("approve",           ":white_check_mark: Approve"),
        ("remove",            ":x: Remove"),
        ("discuss",           ":thought_balloon: Discuss"),
        ("meh",               ":man-shrugging: Meh"),
        ("ban",               ":party_hammer: Ban"),
        ("dont_ban",          ":dont_ban: Don't ban"),
        ("lock_thread",       ":lock: Lock"),
        ("warn",              ":shell: Warn"),
        ("spam",              ":spam: Spam"),
        ("dont_understand",   ":question: Huh?"),
    ]

    # Voting for any key removes votes for its opposing keys
    OPPOSING_VOTES: Dict[str, set] = {
        "approve":         {"remove", "spam"},
        "remove":          {"approve"},
        "spam":            {"approve"},
        "ban":             {"dont_ban"},
        "dont_ban":        {"ban"},
    }

    # Vote keys that hold an item open. An item that leaves the Reddit modqueue
    # is normally auto-marked done here, but a ban vote is a question the
    # modqueue cannot answer: the post is dealt with, the user is not. While any
    # mod holds one of these, the card stays open until a human clicks Done.
    #
    # A ``dont_ban`` from a *different* mod does not release the hold. Votes
    # cancel per mod (see OPPOSING_VOTES), and two mods disagreeing about a ban
    # is exactly the thing that still needs resolving.
    HOLD_OPEN_VOTES: Tuple[str, ...] = ("ban",)

    # Votes needed on one key before an item is called out in the channel's
    # status message, and the keys that count toward it. The status line answers
    # "where have the mods landed?", so it counts the two votes that settle that
    # — a pile of `discuss` or `ban` votes is the question, not the answer.
    #
    # `spam` folds into `remove`: the two already cancel `approve` together in
    # OPPOSING_VOTES, and three mods saying the post goes is three mods saying
    # the post goes, whichever word they used.
    CONSENSUS_THRESHOLD: int = 3
    CONSENSUS_KEYS: Tuple[str, ...] = ("approve", "remove")
    CONSENSUS_ALIASES: Dict[str, str] = {"spam": "remove"}

    # Header status for an item resolved on Reddit but held open by a ban vote.
    BAN_HOLD_STATUS: str = "⏳ BAN VOTE OUTSTANDING"

    # Emoji per resolving action, so a done card reads as approved or removed at
    # a glance wherever the state is shown — header, in-card marker, thread
    # note. Reuses the vote-button vocabulary above so the same action always
    # looks the same; the gavel is the fallback for a done state whose Reddit
    # action is not known.
    ACTION_EMOJI: Dict[str, str] = {
        "approved": "✅",  # :white_check_mark: Approve
        "removed":  "❌",  # :x: Remove
    }
    DONE_EMOJI_DEFAULT: str = ":completed:"

    # Marker section appended to a modqueue message once it is marked done. The
    # emoji varies with the resolving action (see :meth:`done_marker_text`);
    # this is the shape it takes when that action is unknown.
    DONE_MARKER_TEXT: str = ":completed: DONE :completed:"

    # A done marker as written by any version of the bot: the word DONE flanked
    # by one emoji, whichever it is. Cards posted before the emoji varied still
    # have to be recognised, so the marker is matched by shape rather than by
    # comparing against DONE_MARKER_TEXT.
    _DONE_MARKER_RE = re.compile(r"^\S+ DONE \S+$")

    # Header status for a card that went done and came back open, so a mod
    # scrolling past can tell a re-opened item from one that was never resolved.
    # Shared by modqueue items and modmail conversations.
    REOPENED_STATUS: str = "🔄 REOPENED"

    # ------------------------------------------------------------------
    # Card headers
    #
    # A ``header`` block is the only larger text Slack offers, and it is
    # plain_text: no links, no bold, no mentions, and 150 characters hard.
    # Every card carries exactly one, holding its title and — once resolved —
    # the status that used to be a header of its own. One header per card is
    # what keeps the rebuild rule ("strip the headers, add one") honest.
    # ------------------------------------------------------------------

    HEADER_LIMIT: int = 150
    HEADER_SEP: str = " · "
    # A section block's text caps at 3000 characters; over it Slack rejects
    # the whole message, so a long reported comment would never be posted.
    SECTION_LIMIT: int = 3000
    TRUNCATED_NOTE: str = "\n… _(truncated — view on Reddit for the rest)_"

    @classmethod
    def action_emoji(cls, action: str) -> str:
        """Return the emoji standing for a resolving *action*.

        *action* is what :meth:`get_item_resolution` reports (``'approved'`` /
        ``'removed'``), and is ``''`` when Reddit names no resolution — an item
        marked done here before anything happened there, or one that left the
        queue with nobody to credit. That falls back to the gavel, which is what
        "done, action unknown" looks like everywhere in the bot.
        """
        return cls.ACTION_EMOJI.get(action, cls.DONE_EMOJI_DEFAULT)

    @classmethod
    def done_marker_text(cls, emoji: str = "") -> str:
        """Return the in-card DONE marker, flanked by *emoji*.

        Defaults to the gavel, so a card marked done with no known Reddit
        action reads the same as it always has.
        """
        e = emoji or cls.DONE_EMOJI_DEFAULT
        return f"{e} DONE {e}"

    @classmethod
    def is_done_marker(cls, block: Dict[str, Any]) -> bool:
        """Return whether *block* is a card's DONE marker section.

        Matched by shape (see :attr:`_DONE_MARKER_RE`) rather than against one
        literal, since the emoji now follows the resolving action and older
        cards carry the gavel.
        """
        if block.get("type") != "section":
            return False
        return bool(cls._DONE_MARKER_RE.match(block.get("text", {}).get("text", "").strip()))

    @classmethod
    def _fit_header(cls, text: str) -> str:
        """Trim *text* to Slack's header limit, marking where it was cut."""
        if len(text) <= cls.HEADER_LIMIT:
            return text
        return text[:cls.HEADER_LIMIT - 1].rstrip() + "…"

    @classmethod
    def header_block(cls, text: str) -> Dict[str, Any]:
        """Return the big first line of a card.

        ``emoji=True`` so shortcode names (e.g. the custom ``:completed:``
        gavel) render as emoji rather than literal text.
        """
        return {"type": "header", "text": {"type": "plain_text", "text": cls._fit_header(text), "emoji": True}}

    @classmethod
    def header_text(cls, title: str, status: str = "") -> str:
        """Combine a card's title with its status, e.g. ``#12 · submission · ✅ DONE``.

        The title is what gets trimmed when the two do not fit: a status is
        short, and it is the part a mod scrolling past most needs to read.
        """
        if not status:
            return cls._fit_header(title)
        if not title:
            return cls._fit_header(status)
        room = cls.HEADER_LIMIT - len(status) - len(cls.HEADER_SEP)
        if room < 1:
            return cls._fit_header(status)
        return cls._fit_header(title)[:room].rstrip() + cls.HEADER_SEP + status

    # How an item_type reads on a card. The stored value stays Reddit's own
    # ("submission"), since it drives the PRAW branching and the action
    # payloads — this is the display name only.
    ITEM_TYPE_LABELS: Dict[str, str] = {"submission": "post"}

    @classmethod
    def item_title(cls, queue_num: Any, item_type: str, author: str = "") -> str:
        """Return the title of a modqueue card: ``#12 · post by u/someone``.

        *author* is absent from entries logged before it was recorded, and is
        simply left off those.
        """
        label = cls.ITEM_TYPE_LABELS.get(item_type, item_type) or "item"
        title = f"#{queue_num or '?'}{cls.HEADER_SEP}{label}"
        return f"{title} by u/{author}" if author else title

    @classmethod
    def conv_title(cls, conv_num: Any, author: str = "", subject: str = "") -> str:
        """Return the title of a modmail card: ``#A · u/someone · Ban appeal``."""
        parts = [f"#{cls.conv_label(conv_num)}"]
        if author:
            parts.append(f"u/{author}")
        if subject:
            parts.append(subject)
        return cls.HEADER_SEP.join(parts)

    def _wants_tally(self, votes: Optional[Dict[str, Any]] = None) -> bool:
        """Return True if a card should carry the vote tally section.

        A feed that does not vote gets no "_No votes yet_" line under every
        card — it would be a standing lie about what the card offers. Votes
        already cast are still shown, so switching a feed off voting does not
        erase the tally from the cards that have one.
        """
        return self.voting_enabled or bool(votes)

    def _build_vote_tally_block(self, item_id: str, votes: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Return the section block holding the current vote tally for *item_id*."""
        return {
            "type": "section",
            "block_id": f"vote_tally_{item_id}",
            "text": {"type": "mrkdwn", "text": self.format_vote_tally(votes or {})},
        }

    # Authors that name no real account, so Warn/Ban have no target and are
    # left off the action dropdown rather than opening a modal that must fail.
    _NO_AUTHOR: Tuple[str, ...] = ("", "[deleted]", "[removed]")

    def _build_take_action_element(self, item_id: str, item_type: str, author: str, include_ignore_reports: bool = False) -> Dict[str, Any]:
        """Return the Take action… dropdown, which acts on Reddit for real.

        **DORMANT — nothing emits this.** Modqueue cards no longer offer Reddit
        actions; the only live Reddit action is modmail Archive/Unarchive. The
        handler behind this dropdown (``handle_modqueue_action_dormant``) and
        the Remove/Warn/Ban modals are likewise kept and unreachable, so this
        is the shape the feature takes if it comes back. Reviving it means
        emitting this from :meth:`_build_item_actions_block` again, gated on a
        control of its own, and re-registering the handler.

        Approve goes straight through; the other three open a modal for the
        text they need. Warn and Ban are offered only when there is an account
        to aim them at (see :attr:`_NO_AUTHOR`).

        Args:
            item_id: Reddit item ID (bare, e.g. ``'abc123'``).
            item_type: ``'submission'`` or ``'comment'``.
            author: Reddit username of the item's author.
            include_ignore_reports: Add *Ignore reports & Approve*, which
                approves **and** tells Reddit to ignore every future report on
                the item, so nothing can put it back in the modqueue. A heavier
                decision than a plain approve, hence separate. This used to be
                the ``ignore_reports`` control in slack.ini, dropped along with
                the dropdown.
        """
        options = [("approve", "Approve")]
        if include_ignore_reports:
            options.append(("ignore_approve", "Ignore reports & Approve"))
        options.append(("remove", "Remove"))
        if author not in self._NO_AUTHOR:
            options += [("warn", "Warn User"), ("ban", "Ban User")]
        return {
            "type": "static_select",
            "action_id": "modqueue_action",
            "placeholder": {"type": "plain_text", "text": "Take action..."},
            "options": [
                {"text": {"type": "plain_text", "text": label}, "value": f"{key}|{item_id}|{item_type}|{author}"}
                for key, label in options
            ],
        }

    def _build_item_actions_block(self, item_id: str, item_type: str) -> Dict[str, Any]:
        """Return the actions block for an open modqueue item.

        Which controls appear is per feed (see :attr:`VALID_CONTROLS`); the Done
        button is always the last element, so a card is never left with nothing
        to click. ``actions`` adds nothing here — it is a modmail control (see
        :meth:`modmail_control_elements`); the modqueue dropdown it used to add
        is dormant in :meth:`_build_take_action_element`.
        """
        elements: List[Dict[str, Any]] = []
        if self.voting_enabled:
            elements.append({
                "type": "static_select",
                "action_id": f"cast_vote_{int(time.time())}",
                "placeholder": {"type": "plain_text", "text": "Cast vote...", "emoji": True},
                "options": [
                    {"text": {"type": "plain_text", "text": label, "emoji": True}, "value": f"{item_id}|{item_type}|{key}"}
                    for key, label in self.VOTE_OPTIONS
                ],
            })
        elements.append({
            "type": "button",
            "action_id": "mark_done",
            "text": {"type": "plain_text", "text": "Done"},
            "value": f"queue|{item_id}|{item_type}",
        })
        return {"type": "actions", "block_id": f"actions_{item_id}", "elements": elements}

    def _build_reopen_block(self, item_id: str, item_type: str) -> Dict[str, Any]:
        """Return the Re-open dropdown block shown on a done modqueue item."""
        return {
            "type": "actions",
            "block_id": f"reopen_{item_id}",
            "elements": [{
                "type": "static_select",
                "action_id": "reopen_item",
                "placeholder": {"type": "plain_text", "text": "Options..."},
                "options": [
                    {"text": {"type": "plain_text", "text": "Re-open"}, "value": f"{item_id}|{item_type}"},
                ],
            }],
        }

    def _build_modqueue_blocks(self, item_id: str, author: str, report_link: str, item_type: str, content: str, user_reports: List[Any], mod_reports: List[Any], queue_num: int, votes: Optional[Dict[str, str]] = None, status: str = "") -> List[Dict[str, Any]]:
        """Build a Slack Block Kit payload for a single modqueue item.

        Returns blocks containing item details, a vote dropdown, and a
        moderation action dropdown.

        Args:
            item_id: Reddit item ID (bare, e.g. ``'abc123'``).
            author: Reddit username of the item's author.
            report_link: Full permalink URL to the reported item.
            item_type: ``'submission'`` or ``'comment'``.
            content: Formatted body text or title of the item.
            user_reports: Raw PRAW user-report tuples ``[(reason, count), ...]``.
            mod_reports: Raw PRAW mod-report tuples ``[(reason, mod_name), ...]``.
            queue_num: This item's permanent per-channel queue number.
            votes: Existing votes dict; used to populate the tally section.
            status: Short plain-text status appended to the header, e.g.
                ``🔄 REOPENED — terevos2``. Empty for a freshly posted item.

        Returns:
            A list of Slack Block Kit block dicts suitable for the ``blocks``
            parameter of ``chat_postMessage``.
        """
        report_lines: List[str] = []
        for r in user_reports:
            report_lines.append(f"• User: {r[0]}")
        for r in mod_reports:
            mod_name = r[1] if len(r) > 1 else "UNKNOWN"
            report_lines.append(f"• Mod ({mod_name}): {r[0]}")
        reports_text = "\n".join(report_lines) if report_lines else "_No report text_"

        # The number and type live in the header block; the detail section
        # carries what a header cannot render — the links.
        head = (
            f"<{report_link}|View on Reddit>\n"
            f"*User:* <https://reddit.com/u/{author}|u/{author}>\n"
        )
        tail = f"\n*Reports:*\n{reports_text}"
        # Trim the item body, never the links or reports, to keep the section
        # under Slack's cap.
        budget = self.SECTION_LIMIT - len(head) - len(tail)
        if len(content) > budget:
            content = content[:max(0, budget - len(self.TRUNCATED_NOTE))].rstrip() + self.TRUNCATED_NOTE
        text = (head + content + tail)[:self.SECTION_LIMIT]

        blocks: List[Dict[str, Any]] = [
            self.header_block(self.header_text(self.item_title(queue_num, item_type, author), status)),
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": text}
            },
        ]
        if self._wants_tally(votes):
            blocks.append(self._build_vote_tally_block(item_id, votes))
        blocks += [
            {"type": "divider"},
            self._build_item_actions_block(item_id, item_type),
            {"type": "divider"},
        ]
        return blocks

    @staticmethod
    def vote_keys(user_votes: Any) -> List[str]:
        """Return one moderator's vote keys, normalised.

        Handles both shapes the log has held: a bare string from the
        single-vote era, and keys carrying a stale ``|<timestamp>`` suffix.
        """
        keys = user_votes if isinstance(user_votes, list) else [user_votes]
        return [re.sub(r'\|\d+$', '', k) for k in keys if k]

    @classmethod
    def held_open_by_vote(cls, votes: Optional[Dict[str, Any]]) -> bool:
        """Return True if a vote pins this item open (see :attr:`HOLD_OPEN_VOTES`).

        Args:
            votes: The entry's votes dict, mapping Slack user IDs to vote keys.
        """
        if not votes:
            return False
        return any(key in cls.HOLD_OPEN_VOTES for user_votes in votes.values() for key in cls.vote_keys(user_votes))

    @staticmethod
    def format_vote_tally(votes: Dict[str, Any]) -> str:
        """Return a mrkdwn summary of current votes.

        Groups voters by their choice and lists Slack user mentions next to
        each option.  Returns ``'_No votes yet_'`` when *votes* is empty.

        Args:
            votes: Dict mapping Slack user IDs to a list of vote-key strings.
        """
        if not votes:
            return "_No votes yet_"
        key_to_display = {k: d for k, d in RedditActions.VOTE_OPTIONS}
        tally: Dict[str, List[str]] = {}
        for user_id, user_votes in votes.items():
            for key in RedditActions.vote_keys(user_votes):
                display = key_to_display.get(key, key)
                tally.setdefault(display, []).append(f"<@{user_id}>")
        if not tally:
            return "_No votes yet_"
        lines = [
            f"({len(tally[d])}): *{d}*: {', '.join(tally[d])}"
            for _, d in RedditActions.VOTE_OPTIONS
            if d in tally
        ]
        return "*Votes:*\n" + "\n".join(lines)

    @classmethod
    def count_votes(cls, votes: Optional[Dict[str, Any]]) -> Dict[str, int]:
        """Return ``{vote_key: count}`` across every mod, aliases applied.

        Goes through :meth:`vote_keys` so the shapes imported logs hold — a bare
        string, and keys carrying a stale ``|<timestamp>`` suffix — count as the
        keys they are. :attr:`CONSENSUS_ALIASES` is applied here rather than at
        the call site, so everything reading a count agrees on what a spam vote
        is.

        Args:
            votes: The entry's votes dict, mapping Slack user IDs to vote keys.
        """
        counts: Dict[str, int] = {}
        for user_votes in (votes or {}).values():
            for key in cls.vote_keys(user_votes):
                key = cls.CONSENSUS_ALIASES.get(key, key)
                counts[key] = counts.get(key, 0) + 1
        return counts

    @classmethod
    def vote_emoji(cls, key: str) -> str:
        """Return the emoji a vote key is shown with, or ``''`` if unknown.

        Read out of the :attr:`VOTE_OPTIONS` display label rather than held in a
        second table: that list and :attr:`ACTION_EMOJI` are already one
        vocabulary, and a third copy is how they drift apart.
        """
        for vote_key, display in cls.VOTE_OPTIONS:
            if vote_key == key:
                return display.split(" ", 1)[0]
        return ""

    def modmail_control_elements(self, conv_id: str, author: str) -> List[Dict[str, Any]]:
        """Return the control elements for an open modmail card.

        Done is always there — it resolves the thread in Slack alone. Archive
        is offered only on a feed configured for ``actions``, because it
        archives the conversation on Reddit for every mod, not just in Slack.

        Every place that rebuilds an open modmail card uses this, so a card that
        goes done → reopened comes back with the same controls it started with.
        """
        elements: List[Dict[str, Any]] = []
        if self.actions_enabled:
            elements.append({
                "type": "button",
                "action_id": "modmail_action",
                "text": {"type": "plain_text", "text": "Archive"},
                "value": f"archive|{conv_id}|{author}",
            })
        elements.append({
            "type": "button",
            "action_id": "mark_done",
            "text": {"type": "plain_text", "text": "Done"},
            "value": f"mail|{conv_id}|{author}",
        })
        return elements

    def _build_modmail_blocks(self, conv_id: str, message_id: str, author: str, subject: str, body: str, date_str: str, include_actions: bool = True, is_reply: bool = False, conv_num: Optional[int] = None) -> List[Dict[str, Any]]:
        """Build a Slack Block Kit payload for a single modmail message.

        Args:
            conv_id: Reddit modmail conversation ID.
            message_id: Reddit modmail message ID within the conversation.
            author: Reddit username of the message sender.
            subject: Subject line of the conversation.
            body: Markdown body of the message (truncated to 500 chars).
            date_str: ISO-formatted timestamp of the message.
            include_actions: When True, include the moderation action dropdown.
            is_reply: When True, format as a thread reply (omit "New Modmail" header).
            conv_num: Sequential conversation number, displayed as a letter
                label (``A``, ``B``, ``C``...) via :meth:`conv_label`.

        Returns:
            A list of Slack Block Kit block dicts suitable for the ``blocks``
            parameter of ``chat_postMessage``.
        """
        blocks: List[Dict[str, Any]] = []
        if is_reply:
            # A reply is a threaded message under the conversation's card, so it
            # gets no header of its own — the card above it already has one.
            text = (
                f"*<https://reddit.com/u/{author}|u/{author}>* — {date_str}\n"
                f"{body[:500]}{'...' if len(body) > 500 else ''}"
            )
        else:
            blocks.append(self.header_block(self.conv_title(conv_num, author, subject)))
            text = (
                f"*New Modmail* | <https://mod.reddit.com/mail/perma/{conv_id}|View>\n"
                f"*From:* <https://reddit.com/u/{author}|u/{author}> | *Subject:* {subject}\n"
                f"*Date:* {date_str}\n"
                f"{body[:500]}{'...' if len(body) > 500 else ''}"
            )
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": text}
        })
        if include_actions:
            blocks.append({
                "type": "actions",
                "block_id": f"modmail_{conv_id}_{message_id}",
                "elements": self.modmail_control_elements(conv_id, author),
                # The dormant full dropdown (Reply / Mute / Warn / Ban on
                # Reddit) belongs here when it is revived; Archive came back
                # first, as its own button in modmail_control_elements.
            })
        blocks.append({"type": "divider"})
        return blocks

    # ------------------------------------------------------------------
    # Reddit data fetchers
    # ------------------------------------------------------------------

    def get_modqueue(self, channel: str, no_repost: Optional[bool] = None, as_blocks: bool = False) -> Tuple[int, List[Any]]:
        """Fetch all items currently in the subreddit modqueue.

        Items already posted to the given Slack *channel* are tracked via the
        JSON log file and skipped when *no_repost* is ``True``.

        Each new item is assigned a ``queue_num`` from a per-channel counter, so
        a number identifies an item for as long as the log lives. The counter
        runs to :attr:`QUEUE_NUM_MAX` (``#999``); past that the channel's
        entries are archived and the numbering starts over at ``#1``, skipping
        anything a carried-over entry still holds (see :class:`_NumberCycle`).

        Args:
            channel: Slack channel ID used as a deduplication key.
            no_repost: Skip items already posted to *channel*. Defaults to
                ``self.no_repost`` when ``None``.
            as_blocks: When ``True``, return Block Kit block lists instead of
                plain-text strings.  Each element in the returned list is itself
                a list of blocks representing one modqueue item.

        Returns:
            A ``(total, items)`` tuple where *total* is the total number of
            items currently in the modqueue (including already-posted ones) and
            *items* is a list of formatted strings (plain-text mode) or a list
            of block lists (Block Kit mode) for **new** items only.
        """
        if no_repost is None:
            no_repost = self.no_repost

        # One channel's rows, not every channel of every feed: this runs on
        # every poll, and the store can answer the narrow question.
        self.posted_to_slack = {channel: self.store.channel_items(channel)}

        # Track only newly-discovered items; they are inserted at the end.
        new_items: Dict[str, Any] = {}

        messages_dict: Dict[str, Dict[str, Any]] = {}
        total = 0

        for reported_item in self.sub.mod.modqueue():
            total += 1
            item_id: str = reported_item.id
            messages_dict[item_id] = {
                "queue_num": None,  # assigned after the loop for new items
                "created": reported_item.created_utc,
                "messages": [],
                "block_data": None,
            }
            report_link = f"https://reddit.com{reported_item.permalink}?context=3"

            logged = self.posted_to_slack[channel].get(item_id)
            if logged is not None:
                # Numbered before its post: keep the number either way.
                messages_dict[item_id]['queue_num'] = logged.get('queue_num')
            if logged is not None and logged.get('slack_ts'):
                if no_repost:
                    messages_dict.pop(item_id)
                    continue
                else:
                    messages_dict[item_id]["messages"].append(
                        f"Item: <{self.posted_to_slack[channel][item_id]['report_link']}|{item_id}>"
                        " already posted in Slack\n"
                    )
            else:
                # New, or logged but never posted: the entry is written before
                # Slack is asked, so a rejected or failed post leaves it with
                # no slack_ts. Build its card again rather than lose it.
                messages_dict[item_id]["messages"].append(report_link)

                author_name: str
                if reported_item.author is None:
                    messages_dict[item_id]["messages"].append(
                        f"User: NONE FOUND, Item ID: {reported_item.id}"
                    )
                    author_name = "[deleted]"
                else:
                    author_name = reported_item.author.name
                    messages_dict[item_id]["messages"].append(
                        f"User: <https://reddit.com/u/{author_name}|{author_name}>,"
                        f" Item ID: {reported_item.id}"
                    )

                created_date = datetime.fromtimestamp(reported_item.created)
                updated_date = (
                    datetime.fromtimestamp(reported_item.edited)
                    if reported_item.edited is not False
                    else reported_item.edited
                )
                messages_dict[item_id]["messages"].append(
                    f"Reported Item Created Date: {created_date}, Edited: {updated_date}"
                )

                item_type = "Unknown"
                content = ""
                if isinstance(reported_item, praw.models.reddit.comment.Comment):
                    item_type = "comment"
                    comment_body = [
                        ("_ " + line + " _" if line else line)
                        for line in reported_item.body.splitlines()
                    ]
                    content = "\n".join(comment_body)
                    messages_dict[item_id]["messages"].append("Type: Comment - \n" + content)
                elif isinstance(reported_item, praw.models.reddit.submission.Submission):
                    item_type = "submission"
                    content = f"Title: {reported_item.title} — <{reported_item.url}|URL>"
                    messages_dict[item_id]["messages"].append(f"Type: Submission - {content}")
                else:
                    messages_dict[item_id]["messages"].append("Type: Unknown")

                for uidx, r in enumerate(reported_item.user_reports):
                    if uidx == 0:
                        messages_dict[item_id]["messages"].append("User Reports:")
                    messages_dict[item_id]["messages"].append(f"  {uidx + 1}. {r[0]}")

                for midx, r in enumerate(reported_item.mod_reports):
                    if midx == 0:
                        messages_dict[item_id]["messages"].append("Mod Reports:")
                    if len(r) > 1:
                        messages_dict[item_id]["messages"].append(f"   {r[1]}: {r[0]}")
                    else:
                        messages_dict[item_id]["messages"].append(f"   UNKNOWN: {r[0]}")

                messages_dict[item_id]["messages"].append("\n")
                messages_dict[item_id]["block_data"] = {
                    "author": author_name,
                    "report_link": report_link,
                    "item_type": item_type,
                    "content": content,
                    "user_reports": list(reported_item.user_reports),
                    "mod_reports": list(reported_item.mod_reports),
                }

        # Queue numbers are a per-channel counter, not a position in the live
        # modqueue: the modqueue is newest-first, so a position would hand every
        # freshly-seen item the same low number.
        numbers = _NumberCycle(self, self.KIND_QUEUE, channel, self.posted_to_slack[channel], "queue_num", self.QUEUE_NUM_MAX)

        # Number the newly-discovered items oldest-first so queue numbers follow
        # the order items entered the queue rather than Reddit's newest-first sort.
        for item_id in sorted(
            (iid for iid, v in messages_dict.items() if v['queue_num'] is None and v['block_data']),
            key=lambda iid: messages_dict[iid]['created'],
        ):
            val = messages_dict[item_id]
            val['queue_num'] = numbers.next()
            entry = {
                "queue_num": val['queue_num'],
                "report_link": val['block_data']['report_link'],
                "item_type": val['block_data']['item_type'],
                # Recorded for the card header: a rebuild (done, re-open) has
                # only the log to work from, and cannot re-derive the author
                # once the item is gone from the modqueue.
                "author": val['block_data']['author'],
            }
            self.posted_to_slack[channel][item_id] = entry
            new_items[item_id] = entry

        numbers.save()
        if numbers.rolled:
            # The rollover deleted the closed entries; take the pruned view
            # rather than keep a stale one in memory.
            self.posted_to_slack = {channel: {**self.store.channel_items(channel), **new_items}}

        sorted_messages: List[str] = [f"=== MODQUEUE - Total Entries: {total} ==="]
        sorted_blocks: List[List[Dict[str, Any]]] = []

        for key, val in sorted(messages_dict.items(), key=lambda kv: kv[1]['queue_num'] or 0):
            sorted_messages.append(
                "{v}. {m}".format(v=val['queue_num'], m="\n".join(val['messages']))
            )
            if val.get('block_data'):
                bd = val['block_data']
                sorted_blocks.append(self._build_modqueue_blocks(
                    item_id=key,
                    author=bd['author'],
                    report_link=bd['report_link'],
                    item_type=bd['item_type'],
                    content=bd['content'],
                    user_reports=bd['user_reports'],
                    mod_reports=bd['mod_reports'],
                    queue_num=val['queue_num'],
                ))

        # Insert only the rows we discovered. Nothing else in the channel is
        # touched, so a vote or a Done click landing mid-poll is not at risk —
        # which is what the read-merge-write dance here used to be guarding.
        self.store.add_items(channel, new_items)

        if as_blocks:
            return total, sorted_blocks
        return total, sorted_messages

    def get_conversations(self, channel: str, as_blocks: bool = False) -> List[Any]:
        """Fetch new modmail conversations and messages for the subreddit.

        Deduplication is at the individual message level so new replies inside
        an existing conversation are still surfaced.

        When ``as_blocks`` is ``True`` each entry in the returned list is a dict:
          - ``conv_id``        — Reddit conversation ID
          - ``msg_id``         — Reddit message ID
          - ``is_new_conv``    — True only for the first message of a brand-new conv
          - ``thread_ts``      — Existing Slack thread_ts (None for brand-new convs)
          - ``blocks``         — Block Kit block list
          - ``is_user_message``— True if the author is not a known moderator
          - ``text``           — Fallback plain text

        Args:
            channel: Slack channel ID used as a deduplication key.
            as_blocks: When ``True``, return structured dicts (see above).
                When ``False``, return plain-text strings (legacy).
        """
        conv_log: Dict[str, Any] = self.store.channel_convs(channel)
        new_data: Dict[str, Any] = {}   # conv_id → partial update to merge into log
        results: List[Dict[str, Any]] = []
        plain_texts: List[str] = ["=== MODMAIL CONVERSATIONS ==="]

        # Backfill, then prepare the counter. Conversations are numbered up to
        # :attr:`CONV_NUM_MAX` (``#ZZ``); past that the channel's entries are
        # archived and the lettering starts over at ``#A``.
        self._backfill_conv_nums(channel, conv_log)
        numbers = _NumberCycle(self, self.KIND_MAIL, channel, conv_log, "conv_num", self.CONV_NUM_MAX)

        for mod_conv in self.sub.modmail.conversations():
            messages = list(mod_conv.messages)
            # Skip bare automated notices (mod invitations, approved-user adds, ban
            # notifications). Once someone has replied, the conversation is real
            # modmail — post it, notice included, so the replies have context.
            if getattr(mod_conv, 'is_auto', False) and len(messages) <= 1:
                continue
            conv_id: str = mod_conv.id
            known: Optional[Dict[str, Any]] = conv_log.get(conv_id)
            is_new_conv: bool = known is None
            known_msgs: Dict[str, Any] = {} if known is None else known.get("messages", {})
            thread_ts: Optional[str] = None if known is None else known.get("slack_ts")
            # A brand-new conversation always has at least one unseen message
            # (``known_msgs`` is empty), so taking its number here never burns one.
            conv_num: int = numbers.next() if is_new_conv else _as_int(known.get("conv_num"), 0)

            first_in_batch: bool = True  # True for the first new message we yield per conv

            for message in messages:
                msg_id: str = message.id
                if msg_id in known_msgs:
                    continue  # already posted

                author: str = str(message.author) if message.author else "[deleted]"
                is_user_msg: bool = not self.is_mod(author)
                is_first_post: bool = is_new_conv and first_in_batch
                # Only the conversation's own card carries controls. A threaded
                # reply used to get its own Done button, which put the buttons
                # somewhere other than the item they act on; the card gets its
                # controls back when a reply reopens it (_mark_conv_as_reopened).
                is_reply_fmt: bool = not is_first_post
                include_actions: bool = is_first_post

                blocks = self._build_modmail_blocks(
                    conv_id=conv_id,
                    message_id=msg_id,
                    author=author,
                    subject=mod_conv.subject,
                    body=message.body_markdown,
                    date_str=str(message.date),
                    include_actions=include_actions,
                    is_reply=is_reply_fmt,
                    conv_num=conv_num,
                )
                fallback_text = f"#{self.conv_label(conv_num)}. Modmail from u/{author}: {mod_conv.subject}"

                results.append({
                    "conv_id":        conv_id,
                    "msg_id":         msg_id,
                    "is_new_conv":    is_first_post,
                    "thread_ts":      thread_ts,   # None for brand-new convs; poller fills it in
                    "blocks":         blocks,
                    "is_user_message": is_user_msg,
                    "was_done":       self.is_done(known),
                    "text":           fallback_text,
                })
                plain_texts.append(fallback_text)

                # Track what we need to merge into the log
                if conv_id not in new_data:
                    new_data[conv_id] = {"messages": {}}
                    if is_new_conv:
                        new_data[conv_id]["conv_num"] = conv_num
                        new_data[conv_id]["subject"] = mod_conv.subject
                        new_data[conv_id]["author"] = author
                        new_data[conv_id]["done_at"] = None
                new_data[conv_id]["messages"][msg_id] = True
                if is_user_msg:
                    # A new message from the user re-opens a done conversation.
                    new_data[conv_id]["done_at"] = None

                first_in_batch = False

        numbers.save()

        # Write each conversation's own row, plus its new message IDs.
        for conv_id, updates in new_data.items():
            with self.store.edit_conv(channel, conv_id, create=True) as entry:
                # conv_num must persist: it is rendered into the posted Slack
                # message, and the summary reads it back from here. Dropping it
                # let _backfill_conv_nums reassign a different number later.
                for key in ("conv_num", "subject", "author", "done_at"):
                    if key in updates:
                        if key == "done_at" and updates[key] is None:
                            self.clear_done(entry)  # open is encoded as absent
                        else:
                            entry[key] = updates[key]
            self.store.add_conv_messages(channel, conv_id, list(updates.get("messages", {})))

        if as_blocks:
            return results
        return plain_texts

    def set_conv_slack_ts(self, channel: str, conv_id: str, slack_ts: str, permalink: Optional[str] = None) -> None:
        """Store the Slack thread_ts (and optional permalink) for a modmail conversation.

        Args:
            channel: Slack channel ID.
            conv_id: Reddit modmail conversation ID.
            slack_ts: Timestamp of the top-level Slack message for this conversation.
            permalink: Full Slack permalink URL, if available.
        """
        with self.store.edit_conv(channel, conv_id, create=True) as entry:
            entry["slack_ts"] = slack_ts
            if permalink:
                entry["slack_permalink"] = permalink

    def set_conv_done_at(self, channel: str, conv_id: str, done_at: Optional[float]) -> None:
        """Set or clear the Slack-done timestamp for a modmail conversation.

        Mirrors ``set_item_done_at`` — both sides of the bot record done-state
        the same way. See the note on the class for the encoding.

        Args:
            channel: Slack channel ID.
            conv_id: Reddit modmail conversation ID.
            done_at: Unix timestamp when the conversation was marked done, or
                ``None`` to clear (re-opened).
        """
        with self.store.edit_conv(channel, conv_id, create=True) as entry:
            if done_at is None:
                self.clear_done(entry)
            else:
                entry["done_at"] = done_at

    def _backfill_conv_nums(self, channel: str, conv_log: Optional[Dict[str, Any]] = None) -> None:
        """Assign conv_num to any modmail conversations in the log that are missing one.

        Numbers are handed out in Slack-post order (``slack_ts``) so they match
        the sequence the conversations appear in the channel; entries not yet
        posted sort last.  Note that dict order is *not* usable here — the log is
        written with ``sort_keys=True``, so it comes back alphabetised by
        conversation ID rather than in insertion order.  Fills gaps in
        already-assigned numbers.  No-ops when all conversations have a number.

        Args:
            channel: Slack channel ID.
            conv_log: Already-loaded conv_log dict to update in place.  When
                ``None`` the log file is read from disk.
        """
        if conv_log is None:
            conv_log = self.store.channel_convs(channel)

        used_nums = {v["conv_num"] for v in conv_log.values() if isinstance(v, dict) and v.get("conv_num")}
        counter: int = 1
        updates: Dict[str, int] = {}
        ordered = sorted(
            conv_log.items(),
            key=lambda kv: float(kv[1].get("slack_ts") or "inf") if isinstance(kv[1], dict) else float("inf"),
        )
        for cid, cdata in ordered:
            if isinstance(cdata, dict) and not cdata.get("conv_num"):
                while counter in used_nums:
                    counter += 1
                updates[cid] = counter
                used_nums.add(counter)
                counter += 1

        for cid, num in updates.items():
            with self.store.edit_conv(channel, cid) as entry:
                if entry is not None:
                    entry['conv_num'] = num
            conv_log[cid]['conv_num'] = num  # keep caller's reference in sync

    def get_conv_info(self, channel: str, conv_id: str) -> Dict[str, Any]:
        """Return a modmail conversation's log entry, or an empty dict.

        Spares every caller the two-level ``channel → modmail_conv → conv_id``
        walk into the log.
        """
        return self.store.conv(channel, conv_id)

    def conv_title_for(self, channel: str, conv_id: str) -> str:
        """Return the header title of a logged modmail conversation.

        Rebuilding a conversation's card has only the log to work from, so the
        title comes from there rather than from the message being rebuilt.
        """
        entry = self.get_conv_info(channel, conv_id)
        return self.conv_title(entry.get('conv_num'), entry.get('author', ''), entry.get('subject', ''))

    def get_open_conversations(self, channel: str) -> List[Dict[str, Any]]:
        """Return all modmail conversations with status ``'open'`` for *channel*.

        Returns:
            List of dicts with keys: ``conv_id``, ``conv_num``, ``subject``,
            ``author``, ``slack_ts``, ``slack_permalink``.
        """
        conv_log = self.store.channel_convs(channel)
        self._backfill_conv_nums(channel, conv_log)
        open_convs: List[Dict[str, Any]] = []
        for conv_id, cdata in conv_log.items():
            if not self.is_done(cdata):
                open_convs.append({
                    "conv_id":        conv_id,
                    "conv_num":       cdata.get("conv_num"),
                    "subject":        cdata.get("subject", conv_id),
                    "author":         cdata.get("author", "?"),
                    "slack_ts":       cdata.get("slack_ts"),
                    "slack_permalink": cdata.get("slack_permalink"),
                })
        return sorted(open_convs, key=lambda c: c.get("conv_num") or 0)

    def open_items(self, channel: str) -> Dict[str, Dict[str, Any]]:
        """Return *channel*'s modqueue entries that are not marked done.

        The basis of both status-message queries below. It is the log's own view
        of what is open rather than Reddit's view of what is queued, which is
        deliberately wider: an item held open past the modqueue by a ban vote
        still wants votes and still wants acting on.
        """
        return {item_id: entry for item_id, entry in self.store.channel_items(channel).items() if not self.is_done(entry)}

    def items_with_consensus(self, channel: str, threshold: Optional[int] = None) -> List[Dict[str, Any]]:
        """Return open items that have reached *threshold* votes on one key.

        Only :attr:`CONSENSUS_KEYS` are counted, with spam folded into remove
        (see :meth:`count_votes`). An item over the threshold on both keys — a
        genuine split — is returned once per key, the larger count first, since
        that disagreement is exactly what a mod reading the status line needs to
        see.

        Args:
            channel: Slack channel ID.
            threshold: Votes needed on one key; defaults to
                :attr:`CONSENSUS_THRESHOLD`.

        Returns:
            List of dicts with keys: ``item_id``, ``queue_num``,
            ``slack_permalink``, ``key``, ``count``; sorted by ``queue_num``.
        """
        threshold = self.CONSENSUS_THRESHOLD if threshold is None else threshold
        reached: List[Dict[str, Any]] = []
        for item_id, entry in self.open_items(channel).items():
            counts = self.count_votes(entry.get("votes"))
            keys = [k for k in self.CONSENSUS_KEYS if counts.get(k, 0) >= threshold]
            for key in sorted(keys, key=lambda k: counts[k], reverse=True):
                reached.append({
                    "item_id":         item_id,
                    "queue_num":       entry.get("queue_num"),
                    "slack_permalink": entry.get("slack_permalink"),
                    "key":             key,
                    "count":           counts[key],
                })
        return sorted(reached, key=lambda i: i.get("queue_num") or 0)

    def items_without_vote_from(self, channel: str, user_id: str) -> List[Dict[str, Any]]:
        """Return the open items *user_id* has cast no vote on.

        A mod who toggled their last vote off has no rows left and so counts as
        not having voted, which is what the list is for. The comparison is
        case-insensitive: the configured mod roster is upper-cased when it is
        loaded, while vote rows keep whatever the click payload carried.

        Args:
            channel: Slack channel ID.
            user_id: Slack user ID of the moderator asking.

        Returns:
            List of dicts with keys: ``item_id``, ``queue_num``,
            ``slack_permalink``, ``item_type``, ``author``; sorted by
            ``queue_num``.
        """
        uid = user_id.upper()
        unvoted: List[Dict[str, Any]] = []
        for item_id, entry in self.open_items(channel).items():
            if any(voter.upper() == uid for voter in (entry.get("votes") or {})):
                continue
            unvoted.append({
                "item_id":         item_id,
                "queue_num":       entry.get("queue_num"),
                "slack_permalink": entry.get("slack_permalink"),
                "item_type":       entry.get("item_type", ""),
                "author":          entry.get("author", ""),
            })
        return sorted(unvoted, key=lambda i: i.get("queue_num") or 0)

    # ------------------------------------------------------------------
    # Vote tracking
    # ------------------------------------------------------------------

    def record_vote(self, channel: str, item_id: str, user_id: str, vote_key: str) -> None:
        """Record a moderator's vote for a modqueue item.

        Each user holds a list of vote keys. Clicking an already-selected option
        toggles it off. Clicking a vote that opposes an existing selection
        removes the opposing vote(s) before adding the new one.

        Args:
            channel: Slack channel ID the item was posted to.
            item_id: Reddit item ID (bare).
            user_id: Slack user ID of the voting moderator.
            vote_key: The vote key from ``VOTE_OPTIONS`` (e.g. ``'remove_ban'``).
        """
        # Strip any stale timestamp suffix (e.g. "warn|1775768648" → "warn")
        vote_key = re.sub(r'\|\d+$', '', vote_key)

        def apply(current: List[str]) -> List[str]:
            """Toggle *vote_key* into or out of this mod's selection."""
            # Normalize any stale timestamped keys already stored
            current = [re.sub(r'\|\d+$', '', v) for v in current]
            logging.info(f"record_vote: {user_id} votes before={current} new_vote={vote_key}")
            if vote_key in current:
                current.remove(vote_key)  # toggle off
                logging.info(f"record_vote: toggled off {vote_key}, now={current}")
            else:
                opposing = self.OPPOSING_VOTES.get(vote_key, set())
                current = [v for v in current if v not in opposing]
                current.append(vote_key)
                logging.info(f"record_vote: added {vote_key}, now={current}")
            return current

        # One transaction over this mod's rows for this item: two mods clicking
        # at once no longer touch the same record at all, and one mod clicking
        # twice is serialised. The whole-file rewrite this replaced is what used
        # to erase votes cast while a poll was running.
        updated = self.store.update_votes(channel, item_id, user_id, apply)
        logging.info(f"record_vote: stored votes for {item_id}, {user_id}={updated}")

    def set_item_slack_ts(self, channel: str, item_id: str, slack_ts: str, permalink: Optional[str] = None, blocks: Optional[List[Dict[str, Any]]] = None) -> None:
        """Store the Slack message timestamp, permalink, and blocks for a posted modqueue item.

        Args:
            channel: Slack channel ID the item was posted to.
            item_id: Reddit item ID (bare).
            slack_ts: Slack message timestamp returned by ``chat_postMessage``.
            permalink: Full Slack permalink URL, if available.
            blocks: Block Kit blocks of the posted message, cached to avoid
                fetching the message again when updating the vote tally.
        """
        with self.store.edit_item(channel, item_id) as entry:
            if entry is None:
                return
            entry["slack_ts"] = slack_ts
            if permalink:
                entry["slack_permalink"] = permalink
            if blocks is not None:
                entry["slack_blocks"] = blocks

    def set_item_done_at(self, channel: str, item_id: str, done_at: Optional[float], action: str = "", done_by: str = "", note_ts: str = "") -> None:
        """Set or clear the Slack-done timestamp for a modqueue item.

        Args:
            channel: Slack channel ID the item was posted to.
            item_id: Reddit item ID (bare).
            done_at: Unix timestamp when the item was marked done, or ``None`` to clear (reopen).
            action: What Reddit says happened (``'approved'`` / ``'removed'``),
                when that is already known. Left unset, the card shows the
                gavel and the reconcile pass keeps asking (see
                :meth:`record_resolution_check`).
            done_by: Mod credited with the Done click, so the thread note can be
                rewritten in their name once the action turns up.
            note_ts: Timestamp of that thread note, which is the only way back
                to it — it is a reply, not the card.
        """
        with self.store.edit_item(channel, item_id) as entry:
            if entry is None:
                return
            if done_at is None:
                self.clear_done(entry)
            else:
                entry["done_at"] = done_at
                if action:
                    entry["done_action"] = action
                if done_by:
                    entry["done_by"] = done_by
                if note_ts:
                    entry["done_note_ts"] = note_ts

    def record_resolution_check(self, channel: str, item_id: str, action: str) -> None:
        """Count one attempt to learn what Reddit did to a done item, and store any answer.

        A card marked done before Reddit had an answer shows the gavel, and the
        reconcile pass keeps asking until it gets one. The count is what stops
        it: an item that left the modqueue with nobody to credit — deleted by
        its author, caught by the spam filter — never gets an answer, and
        without a bound it would cost a Reddit fetch every poll forever.

        Args:
            channel: Slack channel ID the item was posted to.
            item_id: Reddit item ID (bare).
            action: What :meth:`get_item_resolution` reported, or ``''``.
        """
        with self.store.edit_item(channel, item_id) as entry:
            if entry is None:
                return
            entry["done_checks"] = entry.get("done_checks", 0) + 1
            if action:
                entry["done_action"] = action

    def set_item_ban_hold_at(self, channel: str, item_id: str, ban_hold_at: Optional[float]) -> None:
        """Record that the ban-hold notice has been posted for a modqueue item.

        The item left the Reddit modqueue but a ban vote held it open here
        (:attr:`HOLD_OPEN_VOTES`). The reconcile pass sees that on every poll,
        so this flag is what keeps it from re-announcing every 30 seconds. It is
        cleared by :meth:`clear_done` when the card is reopened, and is a
        notice-tracking flag only — never a state predicate.

        Args:
            channel: Slack channel ID the item was posted to.
            item_id: Reddit item ID (bare).
            ban_hold_at: Unix timestamp the notice went out, or ``None`` to clear.
        """
        with self.store.edit_item(channel, item_id) as entry:
            if entry is None:
                return
            if ban_hold_at is None:
                entry.pop("ban_hold_at", None)
            else:
                entry["ban_hold_at"] = ban_hold_at

    def get_current_modqueue_ids(self) -> List[str]:
        """Return the IDs of all items currently in the subreddit modqueue.

        Returns:
            List of bare Reddit item ID strings.
        """
        return [item.id for item in self.sub.mod.modqueue()]

    @staticmethod
    def _mod_name(value: Any) -> str:
        """Normalise a PRAW moderator field to a username string.

        The field may be a ``Redditor``, a plain username, ``None``, or ``True``
        when Reddit's own spam filter acted rather than a person.
        """
        if value is None or isinstance(value, bool):
            return ""
        return str(getattr(value, "name", value) or "")

    def get_item_resolution(self, item_id: str, item_type: str = "submission") -> Tuple[str, str]:
        """Return who resolved a modqueue item on Reddit, and how.

        Reddit records the acting moderator on the item itself: ``approved_by``
        for approvals, ``banned_by`` / ``removed_by`` for removals. An item can
        also leave the modqueue with nobody to name — the author deleted it, or
        Reddit's own spam filter acted.

        Args:
            item_id: Bare Reddit item ID.
            item_type: ``'comment'`` or ``'submission'``.

        Returns:
            ``(moderator_name, action)`` where action is ``'approved'`` or
            ``'removed'``; both are ``''`` when the resolver cannot be determined.
        """
        try:
            item = self._reddit.comment(id=item_id) if item_type == "comment" else self._reddit.submission(id=item_id)
            approver = self._mod_name(getattr(item, "approved_by", None))
            remover = self._mod_name(getattr(item, "banned_by", None)) or self._mod_name(getattr(item, "removed_by", None))
            if approver and (getattr(item, "approved", False) or not remover):
                return approver, "approved"
            if remover:
                return remover, "removed"
        except Exception as e:
            logging.warning(f"Could not determine who resolved {item_id}: {e}")
        return "", ""

    def get_item_info(self, channel: str, item_id: str) -> Dict[str, Any]:
        """Return the stored log entry for *item_id* in *channel*.

        Useful for retrieving ``queue_num``, ``report_link``, and ``item_type``.
        Returns an empty dict if not found.
        """
        return self.store.item(channel, item_id)

    def get_item_blocks_for_reopen(self, channel: str, item_id: str, status: str = "") -> Optional[List[Dict[str, Any]]]:
        """Fetch a Reddit item by ID and rebuild its full Block Kit payload.

        Used to restore a previously actioned Slack message back to its
        interactive state when a mod clicks Re-open.

        Args:
            channel: Slack channel ID (used to look up log data).
            item_id: Bare Reddit item ID.
            status: Short plain-text status for the header, e.g.
                ``🔄 REOPENED — terevos2``.

        Returns:
            Block Kit block list, or ``None`` if the item could not be fetched.
        """
        item_data = self.store.item(channel, item_id)
        item_type = item_data.get("item_type", "submission")
        queue_num = item_data.get("queue_num", 1)
        report_link = item_data.get("report_link", "")
        votes = item_data.get("votes", {})

        try:
            if item_type == "comment":
                reddit_item = self._reddit.comment(id=item_id)
                comment_body = [
                    ("_ " + line + " _" if line else line)
                    for line in reddit_item.body.splitlines()
                ]
                content = "\n".join(comment_body)
            else:
                reddit_item = self._reddit.submission(id=item_id)
                content = f"Title: {reddit_item.title} — <{reddit_item.url}|URL>"

            author_name = reddit_item.author.name if reddit_item.author else "[deleted]"
            return self._build_modqueue_blocks(
                item_id=item_id,
                author=author_name,
                report_link=report_link,
                item_type=item_type,
                content=content,
                user_reports=list(reddit_item.user_reports),
                mod_reports=list(reddit_item.mod_reports),
                queue_num=queue_num,
                votes=votes,
                status=status,
            )
        except Exception:
            return None

    def find_item_by_slack_ts(self, channel: str, slack_ts: str) -> Optional[str]:
        """Return the item ID posted to *channel* at *slack_ts*, or ``None``."""
        for item_id, info in self.store.channel_items(channel).items():
            if info.get("slack_ts") == slack_ts:
                return item_id
        return None

    def _find_detail_section(self, channel: str, item_id: str, live_blocks: Optional[List[Dict[str, Any]]] = None) -> Optional[Dict[str, Any]]:
        """Return the item-detail section block for *item_id*.

        The detail section (report link, author, content, reports) is the only
        part of a modqueue message that cannot be rebuilt from the log alone,
        so it is carried over from the live message or the cached blocks when
        rebuilding a message in a different state.

        Args:
            channel: Slack channel ID.
            item_id: Reddit item ID (bare).
            live_blocks: Blocks of the message as it currently stands, if known.
                Preferred over the cached copy in the log.

        Returns:
            The detail section block, or ``None`` if it could not be found.
        """
        cached = self.get_item_info(channel, item_id).get("slack_blocks", [])
        for blocks in (live_blocks or [], cached):
            for b in blocks:
                if b.get("type") != "section" or b.get("block_id"):
                    continue
                if self.is_done_marker(b):
                    continue
                return b
        return None

    def build_item_blocks_open(self, channel: str, item_id: str, live_blocks: Optional[List[Dict[str, Any]]] = None, status: str = "") -> Optional[List[Dict[str, Any]]]:
        """Build the blocks for an open (interactive) modqueue item.

        Prefers a full rebuild from Reddit so report counts stay current; falls
        back to reusing the detail section of the existing message when the item
        can no longer be fetched. Either way the result carries the current vote
        tally and a fresh vote dropdown.

        Args:
            channel: Slack channel ID.
            item_id: Reddit item ID (bare).
            live_blocks: Blocks of the message as it currently stands, if known.
            status: Short plain-text status for the header. Callers reopening a
                done item pass :attr:`REOPENED_STATUS` (plus who reopened it) so
                the card says so; an item that was never done passes nothing.

        Returns:
            Block Kit block list, or ``None`` if the item cannot be rendered.
        """
        blocks = self.get_item_blocks_for_reopen(channel, item_id, status)
        if blocks:
            return blocks

        detail = self._find_detail_section(channel, item_id, live_blocks)
        if not detail:
            return None
        info = self.get_item_info(channel, item_id)
        title = self.item_title(info.get("queue_num"), info.get("item_type", "submission"), info.get("author", ""))
        blocks = [self.header_block(self.header_text(title, status)), detail]
        if self._wants_tally(info.get("votes")):
            blocks.append(self._build_vote_tally_block(item_id, info.get("votes", {})))
        blocks += [
            {"type": "divider"},
            self._build_item_actions_block(item_id, info.get("item_type", "submission")),
            {"type": "divider"},
        ]
        return blocks

    def build_item_blocks_done(self, channel: str, item_id: str, header_text: str, live_blocks: Optional[List[Dict[str, Any]]] = None, done_emoji: str = "") -> Optional[List[Dict[str, Any]]]:
        """Build the blocks for a done modqueue item.

        Keeps the item details and the vote tally visible, replaces the vote and
        moderation controls with a Re-open dropdown, and appends the status to
        the card's header.

        Args:
            channel: Slack channel ID.
            item_id: Reddit item ID (bare).
            header_text: Short plain-text status (e.g. ``✅ DONE — terevos2``),
                appended to the card's title in its single header block.
            live_blocks: Blocks of the message as it currently stands, if known.
            done_emoji: Emoji for the in-card DONE marker, so it agrees with the
                header instead of always showing the gavel. Callers pass
                :meth:`action_emoji` of whatever Reddit says happened; the
                default is the gavel, for a done state with no known action.

        Returns:
            Block Kit block list, or ``None`` if the item cannot be rendered.
        """
        detail = self._find_detail_section(channel, item_id, live_blocks)
        if not detail:
            return None
        info = self.get_item_info(channel, item_id)
        title = self.item_title(info.get("queue_num"), info.get("item_type", "submission"), info.get("author", ""))
        blocks = [self.header_block(self.header_text(title, header_text)), detail]
        if self._wants_tally(info.get("votes")):
            blocks.append(self._build_vote_tally_block(item_id, info.get("votes", {})))
        blocks += [
            {"type": "divider"},
            {"type": "section", "text": {"type": "mrkdwn", "text": self.done_marker_text(done_emoji)}},
            self._build_reopen_block(item_id, info.get("item_type", "submission")),
        ]
        return blocks

    def get_votes(self, channel: str, item_id: str) -> Dict[str, str]:
        """Return the current votes for *item_id* in *channel*.

        Args:
            channel: Slack channel ID.
            item_id: Reddit item ID (bare).

        Returns:
            Dict mapping Slack user IDs to their vote choice, or ``{}`` if none.
        """
        return self.store.votes(channel, item_id)

    # ------------------------------------------------------------------
    # Moderation actions
    # ------------------------------------------------------------------

    def approve_item(self, item_id: str) -> str:
        """Approve a submission or comment so it is visible on the subreddit.

        Tries to approve as a submission first; falls back to comment if the
        submission lookup raises an exception.

        Args:
            item_id: Reddit item ID, optionally prefixed (e.g. ``'t3_abc123'``
                or bare ``'abc123'``).

        Returns:
            A human-readable confirmation string.
        """
        clean_id = item_id.split('_')[-1]
        try:
            item = self.sub._reddit.submission(id=clean_id)
            item.mod.approve()
            return f"Approved submission {clean_id}"
        except Exception:
            item = self.sub._reddit.comment(id=clean_id)
            item.mod.approve()
            return f"Approved comment {clean_id}"

    def _fetch_item(self, item_id: str, item_type: str) -> Any:
        """Return the PRAW object for *item_id*, by the type the log recorded."""
        clean_id = item_id.split('_')[-1]
        if item_type == "comment":
            return self.sub._reddit.comment(id=clean_id)
        return self.sub._reddit.submission(id=clean_id)

    def approve_and_ignore_reports(self, item_id: str, item_type: str = "submission") -> str:
        """Approve an item and tell Reddit to ignore all future reports on it.

        Dormant, like the rest of the modqueue Reddit actions — reachable only
        if :meth:`_build_take_action_element` is emitted again.

        The pair is what stops a contested item cycling back into the modqueue
        every time somebody reports it again — a plain approve leaves it open
        to exactly that. Reports are ignored **first**: doing it the other way
        round leaves a window where a report landing between the two calls
        re-queues the item that was just approved.

        Args:
            item_id: Reddit item ID, optionally prefixed (e.g. ``'t3_abc123'``).
            item_type: ``'submission'`` (default) or ``'comment'``.

        Returns:
            A human-readable confirmation string.
        """
        item = self._fetch_item(item_id, item_type)
        item.mod.ignore_reports()
        item.mod.approve()
        return f"Approved {item_type} {item_id} and ignored its reports"

    def get_removal_reasons(self) -> List[Dict[str, str]]:
        """Fetch the subreddit's configured removal reasons from Reddit.

        Returns:
            List of dicts with ``id``, ``title``, and ``message`` keys.
            Empty list if none are configured or on error.
        """
        try:
            reasons = [
                {"id": r.id, "title": r.title, "message": r.message}
                for r in self.sub.mod.removal_reasons
            ]
            return reasons
        except Exception:
            # subreddit_name, not sub.display_name: the latter is a lazy PRAW
            # fetch, so it can raise from inside the handler meant to swallow.
            logging.exception(f"get_removal_reasons: failed for r/{self.subreddit_name}")
            return []

    def remove_item(self, item_id: str, reason_id: str = "", notes: str = "", delivery: str = "silent", item_type: str = "submission") -> str:
        """Remove a submission or comment from the subreddit.

        Args:
            item_id: Reddit item ID, optionally prefixed (e.g. ``'t3_abc123'``).
            reason_id: Reddit removal reason ID to use. Its message text is
                fetched and sent to the user unless delivery is ``'silent'``.
            notes: Additional text appended to the reason message.
            delivery: How to communicate the removal — ``'public'`` posts a
                distinguished reply, ``'private'`` sends modmail, ``'silent'``
                removes with no message.
            item_type: ``'submission'`` (default) or ``'comment'``.

        Returns:
            URL of the removal message (modmail permalink or Reddit comment link),
            or an empty string if delivery is silent or no message was sent.
        """
        clean_id = item_id.split('_')[-1]
        if item_type == "comment":
            item = self.sub._reddit.comment(id=clean_id)
        else:
            item = self.sub._reddit.submission(id=clean_id)
        item.mod.remove()

        message_url = ""
        if delivery != "silent":
            message = ""
            if reason_id:
                for r in self.sub.mod.removal_reasons:
                    if r.id == reason_id:
                        message = r.message
                        break
            if notes:
                message = f"{message}\n\n{notes}".strip() if message else notes

            if message:
                try:
                    if delivery == "public":
                        if item_type == "comment":
                            reply = item.reply(message)
                            reply.mod.distinguish(sticky=False)
                            message_url = f"https://reddit.com{reply.permalink}"
                        else:
                            result = item.mod.send_removal_message(
                                message=message, title="Post Removal", type="public"
                            )
                            logging.info(f"remove_item: send_removal_message (public) returned {result!r}, id={getattr(result,'id',None)!r}")
                            conv_id = getattr(result, 'id', None)
                            if conv_id:
                                message_url = f"https://mod.reddit.com/mail/perma/{conv_id}"
                    elif delivery == "private":
                        if item_type == "comment":
                            if item.author:
                                result = self.sub.modmail.create(
                                    subject="Regarding your comment",
                                    body=message,
                                    recipient=item.author.name,
                                )
                                conv_id = getattr(result, 'id', None)
                                if conv_id:
                                    message_url = f"https://mod.reddit.com/mail/perma/{conv_id}"
                        else:
                            result = item.mod.send_removal_message(
                                message=message, title="Post Removal", type="private"
                            )
                            logging.info(f"remove_item: send_removal_message (private) returned {result!r}, id={getattr(result,'id',None)!r}")
                            conv_id = getattr(result, 'id', None)
                            if conv_id:
                                message_url = f"https://mod.reddit.com/mail/perma/{conv_id}"
                except Exception as e:
                    logging.warning(f"Could not send removal message: {e}")

        return message_url

    def reply_modmail(self, conv_id: str, body: str) -> str:
        """Send a team reply to a modmail conversation (author hidden).

        Args:
            conv_id: Reddit modmail conversation ID.
            body: Body text of the reply.

        Returns:
            A human-readable confirmation string.
        """
        conversation = self.sub.modmail(conv_id)
        conversation.reply(body=body, author_hidden=True)
        return f"Reply sent to conversation {conv_id}"

    def archive_conversation(self, conv_id: str) -> str:
        """Archive a modmail conversation on Reddit.

        Args:
            conv_id: Reddit modmail conversation ID.

        Returns:
            A human-readable confirmation string.
        """
        conversation = self.sub.modmail(conv_id)
        conversation.archive()
        return f"Archived conversation {conv_id}"

    def unarchive_conversation(self, conv_id: str) -> str:
        """Unarchive a modmail conversation on Reddit.

        Args:
            conv_id: Reddit modmail conversation ID.

        Returns:
            A human-readable confirmation string.
        """
        conversation = self.sub.modmail(conv_id)
        conversation.unarchive()
        return f"Unarchived conversation {conv_id}"

    # Reddit modmail mod-action type IDs (from the conversation's mod_actions list)
    _ACTION_ARCHIVED: int = 2
    _ACTION_UNARCHIVED: int = 3

    @staticmethod
    def _last_action_author(mod_conv: Any, action_type_id: int) -> str:
        """Return the moderator who most recently performed an action on a conversation.

        **This costs one HTTP request per call.** ``mod_actions`` is not in the
        modmail listing payload, so reading it makes PRAW fetch the whole
        conversation — and ``getattr(..., default)`` is no protection, because
        the fetch raises its own errors rather than ``AttributeError``. Calling
        this for every conversation in a listing is what got the bot 429'd;
        call it only for the conversations whose state actually changed.

        Args:
            mod_conv: PRAW ``ModmailConversation``.
            action_type_id: Reddit action type to match, e.g. ``_ACTION_ARCHIVED``.

        Returns:
            The moderator's Reddit username, or ``''`` if no such action is
            recorded — Reddit does not log an action when a conversation is
            re-opened by an incoming user reply — or if the lookup failed. The
            attribution is a nicety; losing it must not lose the state change
            it decorates.
        """
        try:
            actions = getattr(mod_conv, 'mod_actions', None) or []
        except Exception as e:
            logging.warning(f"Could not read mod_actions for {getattr(mod_conv, 'id', '?')}: {e}")
            return ""
        latest_date: str = ""
        author: str = ""
        for action in actions:
            try:
                if int(getattr(action, 'action_type_id', -1)) != action_type_id:
                    continue
            except (TypeError, ValueError):
                continue
            date = str(getattr(action, 'date', '') or '')  # ISO-8601, so string order == time order
            if date >= latest_date:
                latest_date = date
                author = str(getattr(action, 'author', '') or '')
        return author

    def sync_archived_conversations(self, channel: str) -> Dict[str, List[Dict[str, Any]]]:
        """Sync Slack done-state with Reddit's modmail archive state.

        Scans both archived and active conversations on Reddit and compares
        against the local log to detect state changes:

        - ``newly_archived``: log status was ``'open'``, now archived on Reddit.
        - ``newly_unarchived``: log status was ``'done'``, now active on Reddit.

        Updates the log and returns both lists so the poll loop can update Slack.

        Args:
            channel: Slack channel ID for the modmail feed.

        Returns:
            Dict with keys ``'archived'`` and ``'unarchived'``, each a list of
            ``{'conv_id', 'author', 'slack_ts', 'by'}`` dicts, where ``'by'`` is
            the moderator who performed the action (``''`` if Reddit did not
            record one).
        """
        try:
            conv_log = self.store.channel_convs(channel)

            # Both listings are read for their IDs alone, which the listing
            # payload carries. Nothing else is touched on these objects here:
            # every other attribute is a per-conversation fetch (see
            # _last_action_author), and there are hundreds of them.
            archived: Dict[str, Any] = {}
            for mod_conv in self.sub.modmail.conversations(state='archived', limit=100):
                archived[mod_conv.id] = mod_conv

            active: Dict[str, Any] = {}
            for state in ('new', 'inprogress', 'mod'):
                try:
                    for mod_conv in self.sub.modmail.conversations(state=state, limit=50):
                        active[mod_conv.id] = mod_conv
                except Exception as e:
                    # One unreadable state leaves the others usable; the worst
                    # case is missing an unarchive until the next pass.
                    logging.warning(f"Could not list {state!r} modmail for r/{self.subreddit_name}: {e}")

            newly_archived: List[Dict[str, Any]] = []
            newly_unarchived: List[Dict[str, Any]] = []
            done_updates: Dict[str, Optional[float]] = {}
            now = time.time()

            # Attribution is looked up only for the conversations that actually
            # changed — normally none. Doing it while building the listings
            # above cost one request per conversation per poll and ran the
            # account into Reddit's rate limit.
            for conv_id, entry in conv_log.items():
                if not entry.get('slack_ts'):
                    continue
                done = self.is_done(entry)
                info = {'conv_id': conv_id, 'author': entry.get('author', ''), 'slack_ts': entry['slack_ts']}

                if not done and conv_id in archived:
                    by = self._last_action_author(archived[conv_id], self._ACTION_ARCHIVED)
                    newly_archived.append({**info, 'by': by})
                    done_updates[conv_id] = now
                elif done and conv_id in active:
                    by = self._last_action_author(active[conv_id], self._ACTION_UNARCHIVED)
                    newly_unarchived.append({**info, 'by': by})
                    done_updates[conv_id] = None

            for conv_id, done_at in done_updates.items():
                with self.store.edit_conv(channel, conv_id, create=True) as entry:
                    if done_at is None:
                        self.clear_done(entry, now)
                    else:
                        entry['done_at'] = done_at

            return {'archived': newly_archived, 'unarchived': newly_unarchived}
        except Exception:
            logging.exception("sync_archived_conversations failed")
            return {'archived': [], 'unarchived': []}

    def mute_conversation(self, conv_id: str, num_hours: int = 72) -> str:
        """Mute a modmail conversation so the user cannot reply for a period.

        Args:
            conv_id: Reddit modmail conversation ID.
            num_hours: Duration to mute (default 72). Reddit accepts 72, 168, or 672.

        Returns:
            A human-readable confirmation string.
        """
        conversation = self.sub.modmail(conv_id)
        conversation.mute(num_hours=num_hours)
        return f"Muted conversation {conv_id} for {num_hours} hours"

    def warn_user(self, username: str, message: str) -> str:
        """Send a modmail warning message to a Reddit user.

        Args:
            username: Reddit username of the recipient (without the ``u/`` prefix).
            message: Body text of the warning message.

        Returns:
            A human-readable confirmation string.
        """
        logging.info(f"warn_user: calling modmail.create for u/{username}")
        result = self.sub.modmail.create(subject="Moderator Warning", body=message, recipient=username)
        conv_id = getattr(result, 'id', None)
        logging.info(f"warn_user: modmail.create returned id={conv_id!r} type={type(result).__name__}")
        return f"https://mod.reddit.com/mail/perma/{conv_id}" if conv_id else ""

    def ban_user(self, username: str, reason: str, duration: Optional[int] = None, note: str = "") -> str:
        """Ban a user from the subreddit.

        Args:
            username: Reddit username to ban (without the ``u/`` prefix).
            reason: Public ban reason shown to the user (truncated to 100 chars
                by Reddit's API).
            duration: Ban length in days. ``None`` (default) means permanent.
            note: Internal moderator note (not visible to the banned user).

        Returns:
            A human-readable confirmation string.
        """
        self.sub.banned.add(
            username,
            ban_reason=reason[:100],
            note=note,
            duration=duration,
        )
        duration_str = f"for {duration} days" if duration else "permanently"
        return f"Banned u/{username} {duration_str}"

    def unban_user(self, username: str) -> str:
        """Remove a ban for a user, restoring their access to the subreddit.

        Args:
            username: Reddit username to unban (without the ``u/`` prefix).

        Returns:
            A human-readable confirmation string.
        """
        self.sub.banned.remove(username)
        return f"Unbanned u/{username}"

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    # File names only — the directory comes from ``self.log_dir`` so tests can
    # redirect the logs without changing the process working directory. Each
    # subreddit owns a directory beneath it, so one subreddit's traffic never
    # decides when another one's log rolls over.
    _QUEUE_LOG_NAME: str = "modqueue.json"
    _MAIL_LOG_NAME: str = "modmail.json"
    _COUNTER_LOG_NAME: str = "counters.json"
    _ARCHIVE_DIR_NAME: str = "archive"
    _EXPORT_DIR_NAME: str = "export"
    _DB_NAME: str = "modlog.db"

    # Exports are the readable copy of a database nobody can grep: the same
    # nested JSON the logs used to be, written on a timer and pruned.
    EXPORT_INTERVAL_DAYS: float = 7
    EXPORT_KEEP: int = 52   # one year of weekly snapshots
    _EXPORT_STAMP_KEY: str = "last_export_at"

    # Which log a number belongs to, as stored in ``counters.json`` and used in
    # archive file names.
    KIND_QUEUE: str = "modqueue"
    KIND_MAIL: str = "modmail"

    # Numbering caps. Past these the channel's entries are archived and the
    # numbering starts over at 1 (``#1`` / ``#A``); 702 is ``ZZ``, the last
    # two-letter modmail label.
    QUEUE_NUM_MAX: int = 999
    CONV_NUM_MAX: int = 702

    # A rollover archives everything but keeps entries closed within this window
    # in the live log: the reconcile pass still re-asks Reddit what happened to a
    # recently-done item (late resolution) and can still re-open one.
    _ARCHIVE_KEEP_DONE: int = 7 * 24 * 3600

    @staticmethod
    def log_slug(subreddit: str) -> str:
        """Return the directory name holding *subreddit*'s logs.

        Subreddit names come from ``slack.ini``, so anything that is not safe in
        a path is replaced rather than trusted.
        """
        slug = re.sub(r'[^A-Za-z0-9_-]', '_', (subreddit or '').strip().lower())
        return slug or "_unknown"

    @property
    def sub_log_dir(self) -> str:
        """Directory holding this subreddit's logs, ``<log_dir>/<subreddit>/``."""
        return os.path.join(self.log_dir, self.log_slug(self.subreddit_name))

    @property
    def archive_dir(self) -> str:
        """Directory holding this subreddit's rolled-over logs."""
        return os.path.join(self.sub_log_dir, self._ARCHIVE_DIR_NAME)

    @property
    def export_dir(self) -> str:
        """Directory holding this subreddit's periodic JSON exports."""
        return os.path.join(self.sub_log_dir, self._EXPORT_DIR_NAME)

    @property
    def db_path(self) -> str:
        """Full path to this subreddit's SQLite database."""
        return os.path.join(self.sub_log_dir, self._DB_NAME)

    @property
    def store(self) -> LogStore:
        """This subreddit's store, opened on first use."""
        if self._store is None:
            self._store = LogStore(self.db_path)
        return self._store

    @property
    def _queue_log_path(self) -> str:
        """Full path to this subreddit's modqueue log."""
        return os.path.join(self.sub_log_dir, self._QUEUE_LOG_NAME)

    @property
    def _mail_log_path(self) -> str:
        """Full path to this subreddit's modmail log."""
        return os.path.join(self.sub_log_dir, self._MAIL_LOG_NAME)

    @property
    def _counters_path(self) -> str:
        """Full path to this subreddit's numbering counters."""
        return os.path.join(self.sub_log_dir, self._COUNTER_LOG_NAME)

    def _read_log(self, path: str) -> Dict[str, Any]:
        """Load a JSON log, creating the directory and an empty file if needed.

        Args:
            path: Full path to the log file.

        Returns:
            The decoded log, or an empty dict for a newly created file.
        """
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        if not os.path.exists(path):
            with open(path, 'w') as f:
                f.write("{}")
        with open(path, 'r') as f:
            return json.load(f)

    def _write_log(self, path: str, jdata: Dict[str, Any]) -> None:
        """Persist a JSON log atomically.

        Writes to a temporary file and renames it over the target, so a reader
        never observes a partially written log.

        Args:
            path: Full path to the log file.
            jdata: Log contents to serialise.
        """
        # Not just _read_log's job: a write can land first on a fresh install.
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        formatted_json = json.dumps(jdata, indent=4, sort_keys=True)
        tmp_path = path + ".tmp"
        with open(tmp_path, 'w') as outfile:
            outfile.write(formatted_json)
        os.replace(tmp_path, path)  # atomic on POSIX — readers always see a complete file

    # The four accessors below are the compatibility surface over the store:
    # they hand back (and take) the nested dicts the JSON logs held. Reading a
    # whole log is fine — the summary and the exports want exactly that — but
    # **nothing on the poll or click paths writes through them**. A write there
    # goes to one row via ``store.edit_item`` / ``store.edit_conv``, which is
    # the entire point of moving off the single-document logs.

    def get_modqueue_file(self) -> Dict[str, Any]:
        """Return the whole modqueue log, in the shape the JSON file had."""
        return self.store.modqueue_snapshot()

    def write_modqueue_file(self, jdata: Dict[str, Any]) -> None:
        """Replace the whole modqueue log — imports and tests only."""
        self.store.replace_modqueue(jdata)

    def migrate_done_state(self) -> Dict[str, int]:
        """Rewrite legacy done-state fields in both logs to the ``done_at`` encoding.

        Converts modqueue ``slack_done_at`` (same meaning, old name) and modmail
        ``status: 'open'|'done'`` (no timestamp, so done entries are stamped with
        the migration time). Safe to run on every startup: once converted there
        is nothing left to change and no row is written.

        Returns:
            Counts of converted entries, ``{'items': n, 'convs': n}``.
        """
        now = time.time()
        counts = {'items': 0, 'convs': 0}

        for channel, entries in self.get_modqueue_file().items():
            for item_id, entry in entries.items():
                if 'slack_done_at' not in entry:
                    continue
                with self.store.edit_item(channel, item_id) as fresh:
                    if fresh is None:
                        continue
                    legacy = fresh.pop('slack_done_at', None)
                    if legacy is not None:
                        fresh['done_at'] = legacy
                counts['items'] += 1

        for channel, payload in self.get_modmail_file().items():
            for conv_id, entry in payload.get('modmail_conv', {}).items():
                if 'status' not in entry:
                    continue
                with self.store.edit_conv(channel, conv_id) as fresh:
                    if fresh is None:
                        continue
                    # No timestamp exists for legacy done conversations; stamp
                    # them now so ordering stays sane, and drop the old field.
                    if fresh.pop('status', None) == 'done':
                        fresh['done_at'] = now
                counts['convs'] += 1

        if counts['items'] or counts['convs']:
            logging.info(f"Migrated done-state: {counts['items']} item(s), {counts['convs']} conversation(s)")
        return counts

    def get_modmail_file(self) -> Dict[str, Any]:
        """Return the whole modmail log, in the shape the JSON file had."""
        return self.store.modmail_snapshot()

    def write_modmail_file(self, jdata: Dict[str, Any]) -> None:
        """Replace the whole modmail log — imports and tests only."""
        self.store.replace_modmail(jdata)

    # ------------------------------------------------------------------
    # Numbering counters and log rollover
    # ------------------------------------------------------------------

    def read_counters(self) -> Dict[str, Any]:
        """Return this subreddit's numbering counters.

        Shape is ``{kind: {channel: {'next': n, 'cycle': n}}}``. An absent
        counter is not fatal — numbering falls back to the highest number
        already in the log, which is what the bot did before rollover existed.
        """
        return self.store.counters()

    def write_counters(self, jdata: Dict[str, Any]) -> None:
        """Replace this subreddit's numbering counters — imports and tests only."""
        self.store.replace_counters(jdata)

    def _survives_archive(self, entry: Any, now: float) -> bool:
        """Return True if *entry* stays in the live log through a rollover.

        Open entries always stay: their cards are still being worked. So do the
        recently-closed ones — the reconcile pass still re-asks Reddit what
        happened to them and can still re-open them. Anything that is not a dict
        is kept as well, since dropping a shape this code does not understand
        would lose data.
        """
        if not isinstance(entry, dict):
            return True
        if not self.is_done(entry):
            return True
        try:
            done_at = float(entry.get("done_at"))
        except (TypeError, ValueError):
            return True
        return (now - done_at) < self._ARCHIVE_KEEP_DONE

    def archive_path(self, kind: str, channel: str, cycle: int, when: Optional[float] = None) -> str:
        """Return the file a rollover of *channel* writes to.

        The name carries the channel and the cycle it closes, so archives sort
        and read in order: ``modqueue-C0123-cycle001-20260820-171500.json``.
        """
        stamp = datetime.fromtimestamp(when if when is not None else time.time()).strftime("%Y%m%d-%H%M%S")
        return os.path.join(self.archive_dir, f"{kind}-{channel}-cycle{cycle:03d}-{stamp}.json")

    def roll_log(self, kind: str, channel: str, cycle: int) -> Dict[str, Any]:
        """Archive a channel's entries and prune the closed ones from the live log.

        Called when numbering reaches the cap (``#999`` / ``#ZZ``). Every entry
        is written to a file under :attr:`archive_dir`; the live log keeps the
        ones :meth:`_survives_archive` names, so nothing a mod is still working
        on disappears when the numbering starts over.

        The archive file is written first and the rows deleted second, so a
        failed write leaves the store untouched rather than half-rolled.

        Args:
            kind: :attr:`KIND_QUEUE` or :attr:`KIND_MAIL`.
            channel: Slack channel ID whose entries are rolling over.
            cycle: The cycle being closed, recorded in the archive.

        Returns:
            The entries that stayed in the live log.
        """
        now = time.time()
        is_queue = kind == self.KIND_QUEUE
        snapshot = self.store.channel_items(channel) if is_queue else self.store.channel_convs(channel)
        keep = {k: v for k, v in snapshot.items() if self._survives_archive(v, now)}
        drop = [k for k in snapshot if k not in keep]

        # The archive file is written before anything is deleted: if the write
        # fails, the rollover fails with it and the rows are still there.
        path = self.archive_path(kind, channel, cycle, now)
        self._write_log(path, {
            "subreddit":   self.subreddit_name,
            "kind":        kind,
            "channel":     channel,
            "cycle":       cycle,
            "archived_at": now,
            "entries":     snapshot,
        })

        if is_queue:
            self.store.delete_items(channel, drop)
        else:
            self.store.delete_convs(channel, drop)
        try:
            # A cycle's worth of rows just left; give the space back. Rare
            # enough that the brief exclusive lock costs nothing.
            self.store.vacuum()
        except Exception as e:
            logging.warning(f"Could not vacuum {self.db_path}: {e}")

        logging.info(
            f"Rolled over {kind} for {channel} (r/{self.subreddit_name}, cycle {cycle}): "
            f"archived {len(snapshot)} entr(ies) to {path}, {len(keep)} carried forward"
        )
        return keep

    # ------------------------------------------------------------------
    # Exports
    # ------------------------------------------------------------------

    def maybe_export(self, now: Optional[float] = None) -> List[str]:
        """Write a JSON export if one is due, and prune old ones.

        The database is the state; the exports are the readable copy of it — the
        same nested JSON the logs used to be, so the tooling that read those
        still works and the data is never locked inside a binary file. One is
        written every :attr:`export_interval` (weekly) and
        :attr:`export_keep` of each kind are kept.

        The stamp lives in the database, so the schedule survives restarts
        rather than starting over every time the bot comes up. A brand-new
        store has no stamp, which makes the first poll export immediately —
        deliberately: that is the baseline copy.

        Args:
            now: Current unix time; injectable for tests.

        Returns:
            Paths written, empty when nothing was due.
        """
        now = time.time() if now is None else now
        try:
            last = float(self.store.get_meta(self._EXPORT_STAMP_KEY) or 0)
        except ValueError:
            last = 0.0
        if now - last < self.export_interval:
            return []
        try:
            written = self.export_logs(now)
        except OSError as e:
            # An export that cannot be written is not a reason to stop polling.
            logging.error(f"Export failed for r/{self.subreddit_name}: {e}")
            return []
        self.store.set_meta(self._EXPORT_STAMP_KEY, str(now))
        self.prune_exports()
        try:
            # The export is the natural moment to fold the WAL back in, so the
            # .db file beside it is a complete copy rather than half a state.
            self.store.checkpoint()
        except Exception as e:
            logging.warning(f"Could not checkpoint {self.db_path}: {e}")
        return written

    def export_logs(self, now: Optional[float] = None) -> List[str]:
        """Write both logs to ``logs/<subreddit>/export/`` as JSON.

        Args:
            now: Timestamp used for the file names; defaults to the current time.

        Returns:
            The paths written.
        """
        now = time.time() if now is None else now
        stamp = datetime.fromtimestamp(now).strftime("%Y%m%d")
        written: List[str] = []
        for name, snapshot in (
            (self.KIND_QUEUE, self.get_modqueue_file()),
            (self.KIND_MAIL, self.get_modmail_file()),
        ):
            path = os.path.join(self.export_dir, f"{name}-{stamp}.json")
            self._write_log(path, snapshot)
            written.append(path)
        logging.info(f"Exported r/{self.subreddit_name} logs to {self.export_dir}")
        return written

    def prune_exports(self) -> List[str]:
        """Delete all but the newest :attr:`export_keep` exports of each kind.

        Returns:
            The paths removed.
        """
        removed: List[str] = []
        if not os.path.isdir(self.export_dir):
            return removed
        for kind in (self.KIND_QUEUE, self.KIND_MAIL):
            # Names carry a sortable date stamp, so lexical order is date order.
            files = sorted(f for f in os.listdir(self.export_dir) if f.startswith(f"{kind}-") and f.endswith(".json"))
            for name in files[:max(0, len(files) - self.export_keep)]:
                path = os.path.join(self.export_dir, name)
                try:
                    os.remove(path)
                    removed.append(path)
                except OSError as e:
                    logging.warning(f"Could not prune export {path}: {e}")
        return removed

    def adopt_legacy_logs(self, channels: List[str]) -> int:
        """Import a channel's entries out of the older JSON logs, once.

        Two layouts came before the database, and both are read here, newest
        first:

        1. ``logs/<subreddit>/modqueue.json`` — the per-subreddit JSON logs.
        2. ``logs/modqueue.json`` — one shared log keyed by channel ID, written
           by every feed at once.

        A channel the store already knows is skipped, which is what stops a
        second pass from resurrecting entries a rollover has since archived. The
        JSON files are left where they are: another feed's channels may still be
        in them, and they are the fallback if the import ever has to be redone.

        Args:
            channels: This feed's resolved channel IDs. Unresolved channels are
                simply retried on a later poll.

        Returns:
            The number of channel slices imported.
        """
        todo = [c for c in channels if c and c not in self._legacy_checked]
        if not todo:
            return 0

        adopted = 0
        for kind, name, own_path, importer in (
            (self.KIND_QUEUE, self._QUEUE_LOG_NAME, self._queue_log_path, self._import_queue_channel),
            (self.KIND_MAIL, self._MAIL_LOG_NAME, self._mail_log_path, self._import_mail_channel),
        ):
            paths = [p for p in (own_path, os.path.join(self.log_dir, name)) if os.path.exists(p)]
            if not paths:
                continue
            for channel in todo:
                if self.store.knows_channel(kind, channel):
                    continue
                imported = 0
                for path in paths:
                    try:
                        data = self._read_log(path)
                    except (ValueError, OSError) as e:
                        # Nothing to recover from a log that will not parse, and
                        # retrying it every poll would only repeat the error.
                        logging.error(f"Could not import {path} for r/{self.subreddit_name}: {e}")
                        continue
                    if channel in data:
                        imported = importer(channel, data[channel])
                        break   # the per-subreddit log wins over the shared one
                # Marked either way: a channel with nothing to import is a
                # channel that must not be looked for again.
                self.store.mark_channel(kind, channel)
                if imported:
                    adopted += 1
                    logging.info(f"Imported {imported} {kind} entr(ies) for {channel} into r/{self.subreddit_name}")

        self._import_legacy_counters()
        self._legacy_checked.update(todo)
        return adopted

    def _import_queue_channel(self, channel: str, entries: Any) -> int:
        """Import one channel's modqueue slice from a JSON log."""
        return self.store.import_items(channel, entries if isinstance(entries, dict) else {})

    def _import_mail_channel(self, channel: str, payload: Any) -> int:
        """Import one channel's modmail slice from a JSON log."""
        convs = payload.get('modmail_conv') if isinstance(payload, dict) else None
        return self.store.import_convs(channel, convs if isinstance(convs, dict) else {})

    def _import_legacy_counters(self) -> None:
        """Carry the JSON counters file over, if the store has none yet.

        Without this a bot that had already rolled over under the JSON layout
        would start its numbering again from the highest number in the log,
        which after a rollover is a carried-over entry near the cap.
        """
        path = self._counters_path
        if not os.path.exists(path) or self.store.counters():
            return
        try:
            self.store.replace_counters(self._read_log(path))
            logging.info(f"Imported numbering counters for r/{self.subreddit_name} from {path}")
        except (ValueError, OSError) as e:
            logging.error(f"Could not import {path} for r/{self.subreddit_name}: {e}")
