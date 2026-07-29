#!/usr/bin/env python
"""ReformedBot — Slack bot for r/reformed moderation.

Connects to Slack via Socket Mode (Slack Bolt) and exposes:
- On-demand commands: report, queue, mail, conv, hello, help
- Interactive Block Kit buttons: Approve, Remove, Warn User, Ban User
- A background polling thread that auto-posts new mod reports and modmail
  to configured Slack channels every ``POLL_INTERVAL`` seconds.

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
config = configparser.ConfigParser()
config.read('slack.ini')

slack_token: str = config['Default']['API_TOKEN']
app_token: str = config['Default']['APP_TOKEN']
signing_secret: str = config['Default']['SIGNING_SECRET']


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


modqueue_channel: Optional[str] = _resolve_channel(config.get('Channels', 'MODQUEUE_CHANNEL', fallback=None), slack_token)
modmail_channel: Optional[str] = _resolve_channel(config.get('Channels', 'MODMAIL_CHANNEL', fallback=None), slack_token)
logging.info(f"Channels resolved: modqueue={modqueue_channel} modmail={modmail_channel}")

# Slack user ID → Reddit username mapping (from [Mods] section of slack.ini)
mod_slack_ids: Dict[str, str] = {}
if config.has_section('Mods'):
    for slack_uid, reddit_name in config.items('Mods'):
        mod_slack_ids[slack_uid.upper()] = reddit_name

reddit: RedditActions = RedditActions('reformed')

# ---------------------------------------------------------------------------
# Slack Bolt app
# ---------------------------------------------------------------------------
app: App = App(token=slack_token, signing_secret=signing_secret)


def _is_allowed_channel(channel_id: str) -> bool:
    """Return True if *channel_id* is one of the configured mod channels."""
    allowed = {c for c in (modqueue_channel, modmail_channel) if c}
    return not allowed or channel_id in allowed


def is_authorized_mod(slack_user_id: str) -> bool:
    """Return ``True`` if *slack_user_id* is in the authorised moderator list.

    The list is populated from the ``[Mods]`` section of ``slack.ini`` at
    startup. Comparison is case-insensitive (Slack user IDs are uppercased).

    Args:
        slack_user_id: Slack user ID string (e.g. ``'U0123456789'``).
    """
    logging.info(f"AUTH CHECK: user={slack_user_id.upper()!r}, mod_slack_ids keys={list(mod_slack_ids.keys())}")
    return slack_user_id.upper() in mod_slack_ids


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
                "type": "input",
                "block_id": "reason_select_block",
                "dispatch_action": True,
                "label": {"type": "plain_text", "text": "Removal Reason"},
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


def _mark_item_as_actioned(client: Any, channel: str, ts: str, header_text: str) -> None:
    """Update a modqueue item message with a prominent status header and strip buttons.

    Rebuilds the message in its done state: status header, item details, the
    current vote tally, and a Re-open dropdown in place of the vote and
    moderation controls. The rebuilt blocks are cached in the log so later
    updates (e.g. the vote tally) do not resurrect the open state.

    Args:
        client: Slack WebClient.
        channel: Channel ID containing the message.
        ts: Timestamp of the message to update.
        header_text: Short plain-text status shown in the header block.
    """
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

        new_blocks = reddit.build_item_blocks_done(channel, item_id, header_text, original_blocks)
        if not new_blocks:
            logging.warning(f"mark_item_as_actioned: no blocks available for {item_id}")
            return

        client.chat_update(channel=channel, ts=ts, blocks=new_blocks, text=header_text)
        reddit.set_item_slack_ts(channel, item_id, ts, blocks=new_blocks)
    except Exception as e:
        logging.warning(f"Could not mark item as actioned: {e}")


def _build_modmail_actions_block(conv_id: str, author: str) -> Dict[str, Any]:
    """Return the full modmail action dropdown block for a conversation."""
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


def _is_status_marker(block: Dict[str, Any]) -> bool:
    """Return True if *block* is a DONE/REOPENED marker section."""
    if block.get("type") != "section":
        return False
    text: str = block.get("text", {}).get("text", "")
    return ":completed: DONE" in text or "*REOPENED*" in text


def _strip_status_blocks(blocks: List[Dict[str, Any]], drop_actions: bool = False) -> List[Dict[str, Any]]:
    """Return *blocks* without the status header and DONE/REOPENED marker.

    A conversation can change state several times (done, replied, archived on
    Reddit), and each change rebuilds the header and marker. Without stripping
    the previous pair first they stack up, leaving a message with several
    headers and several DONE markers.

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


def _mark_conv_as_archived(client: Any, channel: str, conv_id: str, author: str, header_text: str) -> None:
    """Mark a modmail conversation as archived on the top-level Slack message.

    Like _mark_conv_as_actioned but replaces the actions block with a single
    "Unarchive on Reddit" option instead of removing it entirely.

    Args:
        client: Slack WebClient.
        channel: Channel ID containing the message.
        conv_id: Reddit modmail conversation ID.
        author: Reddit username of the conversation author (for the unarchive value).
        header_text: Short plain-text status shown in the header block.
    """
    conv_ts = reddit.get_modmail_file().get(channel, {}).get('modmail_conv', {}).get(conv_id, {}).get('slack_ts')
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
        new_blocks = (
            [{"type": "header", "text": {"type": "plain_text", "text": header_text, "emoji": True}}]
            + kept
            + [{"type": "section", "text": {"type": "mrkdwn", "text": _DONE_MARKER_TEXT}}]
            + [unarchive_block]
        )
        client.chat_update(channel=channel, ts=conv_ts, blocks=new_blocks, text=header_text)
    except Exception as e:
        logging.warning(f"Could not mark conv as archived: {e}")


def _restore_conv_after_unarchive(client: Any, channel: str, conv_ts: str, conv_id: str, author: str) -> None:
    """Restore the top-level modmail message to its active state after unarchiving.

    Removes the archived header and DONE marker, swaps the Unarchive-only
    actions block back to the full action menu.

    Args:
        client: Slack WebClient.
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
        new_blocks: List[Dict[str, Any]] = []
        for b in original_blocks:
            if b.get("type") == "header":
                continue  # remove the archived header
            elif _is_status_marker(b):
                continue  # remove the DONE marker
            elif b.get("block_id", "") == f"modmail_archived_{conv_id}":
                new_blocks.append(_build_modmail_actions_block(conv_id, author))
            else:
                new_blocks.append(b)
        client.chat_update(channel=channel, ts=conv_ts, blocks=new_blocks, text="Unarchived")
    except Exception as e:
        logging.warning(f"Could not restore conv after unarchive: {e}")


def _mark_conv_as_actioned(client: Any, channel: str, conv_id: str, header_text: str) -> None:
    """Update a modmail conversation's top-level Slack message with a status header.

    Looks up the stored slack_ts for the conversation, fetches that message,
    removes all action blocks, and prepends a header + DONE marker.

    Args:
        client: Slack WebClient.
        channel: Channel ID containing the message.
        conv_id: Reddit modmail conversation ID.
        header_text: Short plain-text status shown in the header block.
    """
    conv_ts = reddit.get_modmail_file().get(channel, {}).get('modmail_conv', {}).get(conv_id, {}).get('slack_ts')
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
        new_blocks = (
            [{"type": "header", "text": {"type": "plain_text", "text": header_text, "emoji": True}}]
            + kept
            + [{"type": "section", "text": {"type": "mrkdwn", "text": _DONE_MARKER_TEXT}}]
        )
        client.chat_update(channel=channel, ts=conv_ts, blocks=new_blocks, text=header_text)
    except Exception as e:
        logging.warning(f"Could not mark conv as actioned: {e}")


def _mark_conv_as_reopened(client: Any, channel: str, conv_ts: str) -> None:
    """Replace the DONE header/marker on a modmail top-level message with REOPENED.

    Called when a new user message arrives in a previously-done conversation,
    or when the poll loop detects a conversation was unarchived on Reddit.

    Args:
        client: Slack WebClient.
        channel: Channel ID containing the message.
        conv_ts: Timestamp of the conversation's top-level Slack message.
    """
    try:
        resp = client.conversations_history(channel=channel, latest=conv_ts, inclusive=True, limit=1)
        messages = resp.get("messages", [])
        if not messages:
            return
        original_blocks: List[Dict[str, Any]] = messages[0].get("blocks", [])

        # Extract conv_id from any modmail block_id (format: modmail_{conv_id}_{msg_id})
        conv_id: Optional[str] = None
        for b in original_blocks:
            bid = b.get("block_id", "")
            if bid.startswith("modmail_") and bid.count("_") >= 2:
                conv_id = bid.split("_")[1]
                break

        # Keep the first header and marker in place and drop any extras, so a
        # message that accumulated duplicates before this was fixed collapses
        # back to a single header and a single marker.
        new_blocks: List[Dict[str, Any]] = []
        seen_header = seen_marker = False
        for b in original_blocks:
            if b.get("type") == "header":
                if seen_header:
                    continue
                seen_header = True
                new_blocks.append({"type": "header", "text": {"type": "plain_text", "text": "🔄 REOPENED", "emoji": True}})
            elif _is_status_marker(b):
                if seen_marker:
                    continue
                seen_marker = True
                new_blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": _REOPENED_MARKER_TEXT}})
            else:
                new_blocks.append(b)

        # Re-add Done button if it was stripped and we know the conv_id
        has_done_button = any(
            e.get("action_id") == "mark_done"
            for b in new_blocks if b.get("type") == "actions"
            for e in b.get("elements", [])
        )
        if conv_id and not has_done_button:
            author = reddit.get_modmail_file().get(channel, {}).get('modmail_conv', {}).get(conv_id, {}).get('author', '')
            new_blocks.append({
                "type": "actions",
                "block_id": f"modmail_{conv_id}_done",
                "elements": [{
                    "type": "button",
                    "action_id": "mark_done",
                    "text": {"type": "plain_text", "text": "Done"},
                    "value": f"mail|{conv_id}|{author}",
                }],
            })

        client.chat_update(channel=channel, ts=conv_ts, blocks=new_blocks, text="REOPENED")
    except Exception as e:
        logging.warning(f"Could not mark conv as reopened: {e}")


def _replace_buttons_with_status(client: Any, body: Dict[str, Any], status_text: str) -> None:
    """Update a Slack message in-place, replacing action buttons with a status line.

    This prevents double-actions by removing the buttons after a moderation
    action has been taken and showing a confirmation instead.

    Args:
        client: Slack ``WebClient`` instance (injected by Bolt).
        body: The full Slack action payload dict.
        status_text: Mrkdwn-formatted confirmation text to display.
    """
    try:
        channel: str = body["container"]["channel_id"]
        ts: str = body["container"]["message_ts"]
        original_blocks: List[Dict[str, Any]] = body["message"].get("blocks", [])
        new_blocks = [b for b in original_blocks if b.get("type") != "actions"]
        new_blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": status_text}
        })
        client.chat_update(channel=channel, ts=ts, blocks=new_blocks, text=status_text)
    except Exception as e:
        logging.warning(f"Could not update message: {e}")


# ---------------------------------------------------------------------------
# Action dropdown handler
# ---------------------------------------------------------------------------

@app.action("mark_done")
def handle_mark_done(ack: Any, body: Dict[str, Any], client: Any) -> None:
    """Mark a modqueue or modmail item as done in Slack."""
    ack()
    user_id: str = body["user"]["id"]
    channel: str = body["container"]["channel_id"]
    if not is_authorized_mod(user_id):
        client.chat_postEphemeral(channel=channel, user=user_id, text="You are not authorized to take moderation actions.")
        return

    value: str = body["actions"][0]["value"]
    ts: str = body["container"]["message_ts"]
    mod_reddit = mod_slack_ids.get(user_id.upper(), f"<@{user_id}>")

    try:
        parts = value.split("|", 2)
        kind = parts[0]
        item_id = parts[1] if len(parts) > 1 else ""
        extra = parts[2] if len(parts) > 2 else ""
    except (ValueError, IndexError):
        logging.warning(f"mark_done: unexpected value format: {value!r}")
        return

    if kind == "queue":
        _mark_item_as_actioned(client, channel, ts, f"✅ DONE — {mod_reddit}")
        client.chat_postMessage(channel=channel, thread_ts=ts, text=f":white_check_mark: Marked done by {mod_reddit}")
        reddit.set_item_done_at(channel, item_id, time.time())
        _check_queue_clear_and_post(client)
    elif kind == "mail":
        conv_id = item_id
        _mark_conv_as_actioned(client, channel, conv_id, f"✅ DONE — {mod_reddit}")
        client.chat_postMessage(channel=channel, thread_ts=ts, text=f":white_check_mark: Marked done by {mod_reddit}")
        reddit.set_conv_status(channel, conv_id, "done")


@app.action("modqueue_action")
def handle_modqueue_action(ack: Any, body: Dict[str, Any], client: Any) -> None:
    """Handle a selection from the modqueue action dropdown (currently disabled)."""
    ack()
    # COMMENTED OUT — actions replaced by mark_done button
    # user_id: str = body["user"]["id"]
    # channel: str = body["container"]["channel_id"]
    # if not is_authorized_mod(user_id):
    #     logging.warning(f"modqueue_action: unauthorized user {user_id}")
    #     client.chat_postEphemeral(channel=channel, user=user_id, text="You are not authorized to take moderation actions.")
    #     return
    #
    # value: str = body["actions"][0]["selected_option"]["value"]
    # try:
    #     action, item_id, item_type, author = value.split("|", 3)
    # except ValueError:
    #     logging.warning(f"modqueue_action: unexpected value format: {value!r}")
    #     return
    #
    # ts: str = body["container"]["message_ts"]
    # reddit_link: str = _reddit_link_from_body(body)
    #
    # if action == "approve":
    #     logging.info(f"approve action: item_id={item_id} item_type={item_type} user={user_id}")
    #     try:
    #         reddit.approve_item(item_id)
    #         mod_reddit = mod_slack_ids.get(user_id.upper(), f"<@{user_id}>")
    #         _mark_item_as_actioned(client, channel, ts, f"✅ APPROVED — {mod_reddit}")
    #         client.chat_postMessage(channel=channel, thread_ts=ts, text=f":white_check_mark: *Approved* by {mod_reddit}")
    #         _check_queue_clear_and_post(client)
    #     except Exception as e:
    #         logging.error(f"approve failed: {e}")
    #         client.chat_postEphemeral(channel=channel, user=user_id, text=f"Failed to approve: {e}")
    #
    # elif action == "remove":
    #     client.views_open(
    #         trigger_id=body["trigger_id"],
    #         view=build_remove_modal(item_id, item_type, channel=channel, ts=ts, reddit_link=reddit_link, reasons=reddit.get_removal_reasons()),
    #     )
    #
    # elif action == "warn":
    #     try:
    #         client.views_open(
    #             trigger_id=body["trigger_id"],
    #             view=build_warn_modal(author, channel=channel, ts=ts, reddit_link=reddit_link, item_id=item_id),
    #         )
    #     except Exception as e:
    #         logging.error(f"warn views_open failed: {e}")
    #         client.chat_postEphemeral(channel=channel, user=user_id, text=f"Failed to open warn modal: {e}")
    #
    # elif action == "ban":
    #     client.views_open(
    #         trigger_id=body["trigger_id"],
    #         view=build_ban_modal(author, channel=channel, ts=ts, reddit_link=reddit_link, item_id=item_id),
    #     )

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

    reasons = reddit.get_removal_reasons()
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

    Args:
        ack: Slack Bolt acknowledgement callable.
        body: Full Slack view-submission payload.
        client: Slack ``WebClient`` for API calls.
    """
    ack()
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

    delivery: str = values["delivery_block"]["delivery_input"]["selected_option"]["value"]

    try:
        message_url = reddit.remove_item(item_id, reason_id=reason_id, notes=notes, delivery=delivery, item_type=item_type)
        mod_reddit = mod_slack_ids.get(user_id.upper(), f"<@{user_id}>")
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
            _mark_item_as_actioned(client, channel, ts, f"🗑️ REMOVED — {mod_reddit}")
            client.chat_postMessage(channel=channel, thread_ts=ts, text=f":x: *Removed* by {mod_reddit}\n{details}")
        _check_queue_clear_and_post(client)
    except Exception as e:
        client.chat_postMessage(
            channel=modqueue_channel or user_id,
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

    try:
        logging.info(f"warn_user: sending warning to u/{username} from {user_id}")
        modmail_url = reddit.warn_user(username, message)
        mod_reddit = mod_slack_ids.get(user_id.upper(), f"<@{user_id}>")
        link_part = f" | <{modmail_url}|View Warning>" if modmail_url else ""
        if item_id and channel and ts:
            _append_action_note(client, channel, item_id, ts, f":warning: *Warning sent* to u/{username} by {mod_reddit}{link_part}")
        if channel and ts:
            client.chat_postMessage(channel=channel, thread_ts=ts, text=f":warning: *Warning sent* by {mod_reddit}\nRecipient: u/{username}{link_part}")
    except Exception as e:
        logging.exception(f"warn_user failed for u/{username}: {e}")
        notify = channel or modqueue_channel
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

    try:
        logging.info(f"ban_user: banning u/{username} from {user_id}")
        reddit.ban_user(username, reason=reason, duration=duration, note=note)
        mod_reddit = mod_slack_ids.get(user_id.upper(), f"<@{user_id}>")
        duration_label = f"{duration} days" if duration else "permanent"
        detail_parts = [f"Reason: {reason}", f"Duration: {duration_label}"]
        if note:
            detail_parts.append(f"Note: {note}")
        if item_id and channel and ts:
            link_part = f" | <{reddit_link}|View on Reddit>" if reddit_link else ""
            _append_action_note(client, channel, item_id, ts, f":no_entry: *Banned* u/{username} ({duration_label}) by {mod_reddit}{link_part}")
        if channel and ts:
            client.chat_postMessage(channel=channel, thread_ts=ts, text=f":no_entry: *Banned* by {mod_reddit}\n{' | '.join(detail_parts)}")
    except Exception as e:
        logging.exception(f"ban_user failed for u/{username}: {e}")
        notify = channel or modqueue_channel
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
    """Handle modmail action dropdown — currently only unarchive is active."""
    ack()
    user_id: str = body["user"]["id"]
    channel: str = body["container"]["channel_id"]
    if not is_authorized_mod(user_id):
        client.chat_postEphemeral(channel=channel, user=user_id, text="You are not authorized to take moderation actions.")
        return

    value: str = body["actions"][0]["selected_option"]["value"]
    try:
        action, conv_id, author = value.split("|", 2)
    except ValueError:
        logging.warning(f"modmail_action: unexpected value format: {value!r}")
        return

    ts: str = body["container"]["message_ts"]

    # COMMENTED OUT — all actions are read-only for now
    # if action == "unarchive": ...
    # elif action == "reply": ...
    # elif action == "archive": ...
    # elif action == "mute": ...
    # elif action == "warn": ...
    # elif action == "ban": ...


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

    try:
        reddit.reply_modmail(conv_id, message)
        mod_reddit = mod_slack_ids.get(user_id.upper(), f"<@{user_id}>")
        reply_text = f":speech_balloon: *Reply sent* by {mod_reddit}\n{message}"
        if channel and ts:
            client.chat_postMessage(channel=channel, thread_ts=ts, text=reply_text)
        _mark_conv_as_actioned(client, channel, conv_id, f"💬 REPLIED — {mod_reddit}")
        reddit.set_conv_status(channel, conv_id, "done")
    except Exception as e:
        notify_channel = modmail_channel or modqueue_channel
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
    reddit_name: str = mod_slack_ids.get(user_id.upper(), user_id)

    logging.info(f"cast_vote RECEIVED: user={reddit_name} delay={dispatch_delay}s value={value!r}")

    def _process() -> None:
        if not is_authorized_mod(user_id):
            logging.warning(f"cast_vote: unauthorized user {reddit_name} ({user_id})")
            client.chat_postEphemeral(channel=channel, user=user_id, text="You are not authorized to vote.")
            return

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
    if not is_authorized_mod(user_id):
        client.chat_postEphemeral(channel=channel, user=user_id, text="You are not authorized to take moderation actions.")
        return

    value: str = body["actions"][0]["selected_option"]["value"]
    try:
        item_id, item_type = value.split("|", 1)
    except ValueError:
        logging.warning(f"reopen_item: unexpected value format: {value!r}")
        return

    ts: str = body["container"]["message_ts"]
    live_blocks: List[Dict[str, Any]] = body.get("message", {}).get("blocks", [])
    blocks = reddit.build_item_blocks_open(channel, item_id, live_blocks)
    if not blocks:
        client.chat_postEphemeral(channel=channel, user=user_id, text="Could not re-open item — it may no longer be accessible on Reddit.")
        return

    reddit.set_item_done_at(channel, item_id, None)
    mod_reddit = mod_slack_ids.get(user_id.upper(), f"<@{user_id}>")
    try:
        client.chat_update(channel=channel, ts=ts, blocks=blocks, text="Mod report item (re-opened)")
        reddit.set_item_slack_ts(channel, item_id, ts, blocks=blocks)
        client.chat_postMessage(channel=channel, thread_ts=ts, text=f":arrows_counterclockwise: Re-opened by {mod_reddit}")
    except Exception as e:
        logging.warning(f"reopen_item: could not update message: {e}")


# ---------------------------------------------------------------------------
# Text command handlers
# ---------------------------------------------------------------------------

UNKNOWN_MESSAGES: List[str] = [
    "I do not know that command.",
    "I wish I could help you, but I don't understand.",
    "I apologize. I'm not familiar with that command",
    "I'm sorry. I don't know what that means.",
    "Your sin smells to high heaven."
]


@app.error
def handle_error(error: Exception, body: Dict[str, Any]) -> None:
    logging.error(f"Bolt error: {error} | body: {body}")


@app.event("message")
def handle_message_noop() -> None:
    pass  # Absorb message events to prevent Bolt warnings


@app.event("app_mention")
def handle_mention_noop() -> None:
    pass  # Absorb app_mention events to prevent Bolt warnings


# ---------------------------------------------------------------------------
# Background polling thread
# ---------------------------------------------------------------------------

_SUMMARY_INTERVAL: int = 5 * 60  # seconds between queue summaries
_last_summary_key: Optional[str] = None  # tracks last posted summary; None forces next post

# Scheduled digest: a summary forced out at fixed times of day even when the
# state has not changed, so the channel gets a morning and midday status check.
_DIGEST_TZ = ZoneInfo("America/New_York")
_DIGEST_HOURS: Tuple[int, ...] = (6, 13)  # local hours in _DIGEST_TZ
_DIGEST_WINDOW: int = 15 * 60  # only fire this long after the scheduled hour
_DIGEST_QUIET_PERIOD: int = 60 * 60  # skip the digest if the bot posted this recently
_last_digest_slot: Optional[str] = None  # "YYYY-MM-DD:H" of the last digest decision
_last_activity_at: float = 0.0  # time.time() of the last bot post to a feed channel

# Header emoji per resolving action, so a done item reads as approved or
# removed at a glance. Matches the vote buttons (RedditActions.VOTE_OPTIONS)
# so the same action always looks the same. Falls back to the neutral check
# when the action is unknown (see _ACTION_EMOJI_DEFAULT).
_ACTION_EMOJI: Dict[str, str] = {
    "approved": "✅",  # :white_check_mark: Approve
    "removed": "❌",   # :x: Remove
}
_ACTION_EMOJI_DEFAULT: str = ":completed:"  # same gavel as the DONE marker at the bottom of the message


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


def _post_queue_summary(web_client: SlackWebClient, force: bool = False) -> None:
    """Post a one-line summary of items still pending in the Reddit modqueue.

    Fetches the live modqueue from Reddit, looks up each item's Slack message
    permalink from the log, and posts a compact summary to ``modqueue_channel``.
    Skips posting if the summary content is identical to the last posted summary
    (avoids repeating the same state).

    Args:
        force: Post even when the state is unchanged.  Used by the scheduled
            digest so a quiet channel still gets a status line.
    """
    global _last_summary_key, _last_activity_at
    if not modqueue_channel:
        return
    try:
        current_ids = reddit.get_current_modqueue_ids()

        # Build a deduplication key from the current queue state.
        # Use sorted IDs so order changes don't trigger a new post.
        summary_key = ",".join(sorted(current_ids))

        if not force and _last_summary_key is not None and summary_key == _last_summary_key:
            return

        if not current_ids:
            text = ":white_check_mark: Mod queue is clear."
        else:
            channel_data = reddit.get_modqueue_file().get(modqueue_channel, {})
            parts: List[str] = []
            current_ids = sorted(current_ids, key=lambda iid: channel_data.get(iid, {}).get("queue_num") or 0)
            for item_id in current_ids:
                item_data = channel_data.get(item_id, {})
                queue_num = item_data.get("queue_num", "?")
                slack_link = item_data.get("slack_permalink")
                if slack_link:
                    label = f"<{slack_link}|#{queue_num}>"
                else:
                    label = f"#{queue_num}"
                parts.append(label)
            text = f":clock2: *{len(current_ids)} item(s) still pending:* {' | '.join(parts)}"

        web_client.chat_postMessage(channel=modqueue_channel, text=text)
        _last_summary_key = summary_key
        _last_activity_at = time.time()
    except Exception as e:
        logging.error(f"Queue summary error: {e}")


_last_modmail_summary_key: str = ""


def _post_modmail_summary(web_client: SlackWebClient, force: bool = False) -> None:
    """Post a summary of open modmail conversations to the modmail channel.

    Skips posting if the open conversation set is identical to the last summary.

    Args:
        force: Post even when the state is unchanged.  Used by the scheduled
            digest so a quiet channel still gets a status line.
    """
    global _last_modmail_summary_key, _last_activity_at
    if not modmail_channel:
        return
    try:
        open_convs = reddit.get_open_conversations(modmail_channel)
        summary_key = ",".join(sorted(c["conv_id"] for c in open_convs))
        if not force and summary_key == _last_modmail_summary_key:
            return
        if not open_convs:
            text = ":white_check_mark: All modmail conversations are resolved."
        else:
            parts: List[str] = []
            for conv in open_convs:
                label = f"#{reddit.conv_label(conv.get('conv_num'))}. u/{conv['author']} — {conv['subject']}"
                link = conv.get("slack_permalink")
                parts.append(f"<{link}|{label}>" if link else label)
            text = f":speech_balloon: *{len(open_convs)} open modmail thread(s):*\n" + "\n".join(f"• {p}" for p in parts)
        web_client.chat_postMessage(channel=modmail_channel, text=text)
        _last_modmail_summary_key = summary_key
        _last_activity_at = time.time()
    except Exception as e:
        logging.error(f"Modmail summary error: {e}")


def _maybe_post_digest(web_client: SlackWebClient) -> bool:
    """Force a queue + modmail summary at the scheduled times of day.

    Fires once per hour in ``_DIGEST_HOURS`` (local to ``_DIGEST_TZ``), bypassing
    the normal "state unchanged" dedup so a quiet channel still gets a status
    line each morning and midday.  Suppressed when the bot has already posted
    within ``_DIGEST_QUIET_PERIOD`` — the channel is not quiet, so a forced
    repeat would just be noise.

    Each scheduled hour is only considered for ``_DIGEST_WINDOW`` seconds after
    it passes, and the decision is recorded either way, so a slow poll iteration
    still catches it but a restart later in the day does not re-fire it.

    Returns:
        ``True`` if a digest was posted.
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

        quiet_for = time.time() - _last_activity_at
        if quiet_for < _DIGEST_QUIET_PERIOD:
            logging.info(f"Digest {slot}: skipped, last post was {int(quiet_for / 60)}m ago")
            return False

        logging.info(f"Digest {slot}: posting forced summary")
        _post_queue_summary(web_client, force=True)
        _post_modmail_summary(web_client, force=True)
        return True
    return False


def _append_action_note(client: Any, channel: str, item_id: str, ts: str, note_text: str) -> None:
    """Append a note to a modqueue item message without removing its interactive elements.

    Used for actions (warn, ban) that are worth recording but don't resolve the
    item — it still needs to be approved or removed on Reddit.

    Args:
        client: Slack WebClient.
        channel: Channel ID containing the message.
        item_id: Reddit item ID (bare), used to look up cached blocks.
        ts: Timestamp of the Slack message to update.
        note_text: Mrkdwn text for the note block appended to the message.
    """
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


def _check_queue_clear_and_post(client: Any) -> None:
    """After a moderation action, post a queue-clear notice if the Reddit modqueue is now empty.

    Runs in a background thread so it does not block the action handler.
    If the queue still has items, nothing is posted (the regular interval handles it).
    """
    def _run() -> None:
        try:
            current_ids = reddit.get_current_modqueue_ids()
            if not current_ids:
                _post_queue_summary(client)
        except Exception as e:
            logging.error(f"Post-action queue check error: {e}")
    threading.Thread(target=_run, daemon=True).start()


def _reconcile_modqueue_state(web_client: SlackWebClient, poll_interval: int) -> bool:
    """Sync Slack done-state with the live Reddit modqueue.

    - Auto-done: item is no longer in the Reddit modqueue but Slack still shows
      it as open → mark it done in Slack automatically.
    - Auto-reopen: item was marked done in Slack but is still in the Reddit
      modqueue after a grace period (2× poll_interval) → reopen it in Slack.
    """
    if not modqueue_channel:
        return False
    changed = False
    try:
        current_id_set = set(reddit.get_current_modqueue_ids())
        channel_data = reddit.get_modqueue_file().get(modqueue_channel, {})
        now = time.time()
        grace = 2 * poll_interval

        for item_id, item_data in channel_data.items():
            slack_ts = item_data.get("slack_ts")
            done_at = item_data.get("slack_done_at")
            if not slack_ts:
                continue

            if done_at is None and item_id not in current_id_set:
                # Resolved on Reddit without using the bot — auto-mark done in
                # Slack, naming the mod who acted when Reddit records one.
                mod_name, action = reddit.get_item_resolution(item_id, item_data.get("item_type", "submission"))
                emoji = _ACTION_EMOJI.get(action, _ACTION_EMOJI_DEFAULT)
                header = f"{emoji} DONE — {mod_name} ({action} on Reddit)" if mod_name else f"{_ACTION_EMOJI_DEFAULT} DONE (resolved on Reddit)"
                _mark_item_as_actioned(web_client, modqueue_channel, slack_ts, header)
                reddit.set_item_done_at(modqueue_channel, item_id, now)
                logging.info(f"Auto-marked done: {item_id} ({header})")
                changed = True

            elif done_at is not None and item_id in current_id_set and (now - done_at) >= grace:
                # Still in Reddit modqueue after grace period — reopen in Slack.
                # Rebuilds from Reddit when possible, otherwise reuses the item
                # details of the cached done message.
                reopen_blocks = reddit.build_item_blocks_open(modqueue_channel, item_id)
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
    except Exception as e:
        logging.error(f"Reconcile modqueue state error: {e}")
    return changed


def _poll_loop() -> None:
    """Continuously poll Reddit and push new items to configured Slack channels.

    Runs as a daemon thread started at bot launch. Polls the subreddit
    modqueue and modmail conversations every ``POLL_INTERVAL`` seconds
    (configured in ``slack.ini``; default: 30). Uses the same deduplication
    logic as the on-demand commands so items are never posted twice to the
    same channel.

    New modqueue items go to ``MODQUEUE_CHANNEL``; new modmail messages go to
    ``MODMAIL_CHANNEL``. Either channel can be omitted to disable that feed.
    """
    global _last_summary_key, _last_activity_at
    poll_interval: int = int(config.get('Default', 'POLL_INTERVAL', fallback='30'))
    web_client: SlackWebClient = SlackWebClient(token=slack_token)
    last_summary: float = 0.0

    while True:
        logging.info("Polling Reddit...")
        modqueue_changed = False
        try:
            if modqueue_channel:
                total, blocks = reddit.get_modqueue(modqueue_channel, no_repost=True, as_blocks=True)
                logging.info(f"Modqueue: {total} total, {len(blocks)} new to post")
                if blocks:
                    # Reset dedup key so a "queue is clear" notice will re-fire
                    # after these new items are actioned.
                    _last_summary_key = None
                    _last_activity_at = time.time()
                for block_list in blocks:
                    resp = web_client.chat_postMessage(
                        channel=modqueue_channel,
                        blocks=block_list,
                        text="New mod report"
                    )
                    item_id = _item_id_from_blocks(block_list)
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
                modqueue_changed = _reconcile_modqueue_state(web_client, poll_interval)
        except Exception as e:
            logging.error(f"Poller error (modqueue): {e}")

        try:
            if modmail_channel:
                items = reddit.get_conversations(modmail_channel, as_blocks=True)
                logging.info(f"Modmail: {len(items)} new message(s) to post")
                if items:
                    _last_activity_at = time.time()
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
                                    _mark_conv_as_reopened(web_client, modmail_channel, conv_ts)
                            reddit.set_conv_status(modmail_channel, conv_id, "open")
                        logging.info(f"Posted modmail {'conv' if item['is_new_conv'] else 'reply'} {conv_id} to {modmail_channel}")
                    except Exception as e:
                        logging.error(f"Poller error posting modmail {conv_id}: {e}")
        except Exception as e:
            logging.error(f"Poller error (modmail): {e}")

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
                    _mark_conv_as_actioned(web_client, modmail_channel, conv_id, header)
                    web_client.chat_postMessage(channel=modmail_channel, thread_ts=conv_ts, text=note)
                    logging.info(f"Auto-archived modmail conv {conv_id} in Slack (by {by or 'unknown'})")
                    modmail_changed = True
                for conv in changes['unarchived']:
                    conv_id = conv["conv_id"]
                    conv_ts = conv["slack_ts"]
                    by = conv.get("by") or ""
                    note = f":inbox_tray: Unarchived on Reddit by {_reddit_user_link(by)}" if by else ":inbox_tray: Unarchived on Reddit"
                    _mark_conv_as_reopened(web_client, modmail_channel, conv_ts)
                    web_client.chat_postMessage(channel=modmail_channel, thread_ts=conv_ts, text=note)
                    logging.info(f"Auto-unarchived modmail conv {conv_id} in Slack (by {by or 'unknown'})")
                    modmail_changed = True
        except Exception as e:
            logging.error(f"Poller error (modmail archive sync): {e}")

        now = time.time()
        if _maybe_post_digest(web_client):
            last_summary = now
        elif modqueue_changed or modmail_changed or now - last_summary >= _SUMMARY_INTERVAL:
            _post_queue_summary(web_client)
            _post_modmail_summary(web_client)
            last_summary = now

        time.sleep(poll_interval)


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

    if modqueue_channel or modmail_channel:
        poller_thread = threading.Thread(target=_poll_loop, daemon=True)
        poller_thread.start()
        logging.info("Polling thread started.")
    else:
        logging.warning("No MODQUEUE_CHANNEL or MODMAIL_CHANNEL configured — polling disabled.")

    logging.info("Starting ReformedBot via Socket Mode...")
    SocketModeHandler(app, app_token).start()
