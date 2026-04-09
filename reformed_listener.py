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
import re
import threading
import time
from typing import Any, Dict, List, Optional

import configparser

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk import WebClient as SlackWebClient

from reddit_actions import RedditActions

logging.basicConfig(level=logging.INFO)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
config = configparser.ConfigParser()
config.read('slack.ini')

slack_token: str = config['Default']['API_TOKEN']
app_token: str = config['Default']['APP_TOKEN']
signing_secret: str = config['Default']['SIGNING_SECRET']

modqueue_channel: Optional[str] = config.get('Channels', 'MODQUEUE_CHANNEL', fallback=None)
modmail_channel: Optional[str] = config.get('Channels', 'MODMAIL_CHANNEL', fallback=None)

# Slack user ID → Reddit username mapping (from [Mods] section of slack.ini)
mod_slack_ids: Dict[str, str] = {}
if config.has_section('Mods'):
    for slack_uid, reddit_name in config.items('Mods'):
        mod_slack_ids[slack_uid.upper()] = reddit_name

reddit: RedditActions = RedditActions('reformedtesting')

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

def build_remove_modal(item_id: str, item_type: str = "submission", channel: str = "", ts: str = "", reddit_link: str = "", reasons: Optional[List[Dict[str, str]]] = None) -> Dict[str, Any]:
    """Build the Slack modal view payload for the Remove action.

    Shows a dropdown of the subreddit's configured removal reasons (if any),
    an optional notes field, and radio buttons for delivery method.

    Args:
        item_id: Reddit item ID (bare, e.g. ``'abc123'``).
        item_type: ``'submission'`` (default) or ``'comment'``.
        channel: Slack channel ID of the originating message.
        ts: Timestamp of the originating Slack message.
        reddit_link: Full Reddit permalink for building the confirmation header.
        reasons: List of ``{'id', 'title', 'message'}`` dicts from Reddit.

    Returns:
        A Slack modal view dict suitable for ``client.views_open``.
    """
    has_preset = bool(reasons)
    metadata = json.dumps({
        "item_id": item_id, "item_type": item_type,
        "channel": channel, "ts": ts, "reddit_link": reddit_link,
        "has_preset": has_preset,
    })

    if has_preset:
        reason_block: Dict[str, Any] = {
            "type": "input",
            "block_id": "reason_block",
            "label": {"type": "plain_text", "text": "Removal Reason"},
            "element": {
                "type": "static_select",
                "action_id": "reason_input",
                "placeholder": {"type": "plain_text", "text": "Select a reason"},
                "options": [
                    {"text": {"type": "plain_text", "text": r["title"][:75]}, "value": r["id"]}
                    for r in reasons
                ],
            },
        }
    else:
        reason_block = {
            "type": "input",
            "block_id": "reason_block",
            "label": {"type": "plain_text", "text": "Removal Reason"},
            "element": {
                "type": "plain_text_input",
                "action_id": "reason_input",
                "multiline": True,
                "placeholder": {"type": "plain_text", "text": "Explain why this is being removed"},
            },
        }

    return {
        "type": "modal",
        "callback_id": "removal_reason_submitted",
        "private_metadata": metadata,
        "title": {"type": "plain_text", "text": "Remove"},
        "submit": {"type": "plain_text", "text": "Remove"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            reason_block,
            {
                "type": "input",
                "block_id": "notes_block",
                "optional": True,
                "label": {"type": "plain_text", "text": "Additional Notes"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "notes_input",
                    "multiline": True,
                    "placeholder": {"type": "plain_text", "text": "Appended to the reason (optional)"},
                },
            },
            {
                "type": "input",
                "block_id": "delivery_block",
                "label": {"type": "plain_text", "text": "Delivery"},
                "element": {
                    "type": "radio_buttons",
                    "action_id": "delivery_input",
                    "options": [
                        {"text": {"type": "plain_text", "text": "Post Public"}, "value": "public"},
                        {"text": {"type": "plain_text", "text": "Post Private"}, "value": "private"},
                        {"text": {"type": "plain_text", "text": "Silent Remove"}, "value": "silent"},
                    ],
                },
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


def _mark_item_as_actioned(client: Any, channel: str, ts: str, header_text: str) -> None:
    """Update a modqueue item message with a prominent status header and strip buttons.

    Fetches the current message, removes all ``actions`` blocks (vote and
    moderation buttons), and prepends a ``header`` block so the item is
    visually distinct from un-actioned items.

    Args:
        client: Slack WebClient.
        channel: Channel ID containing the message.
        ts: Timestamp of the message to update.
        header_text: Short plain-text status shown in the header block.
    """
    try:
        resp = client.conversations_history(channel=channel, latest=ts, inclusive=True, limit=1)
        messages = resp.get("messages", [])
        if not messages:
            return
        original_blocks: List[Dict[str, Any]] = messages[0].get("blocks", [])

        # Extract item_id and item_type from the original blocks before stripping actions
        item_id: Optional[str] = None
        item_type: str = "submission"
        for b in original_blocks:
            bid = b.get("block_id", "")
            if bid.startswith("vote_tally_"):
                item_id = bid[len("vote_tally_"):]
            elif bid.startswith("modqueue_"):
                parts = bid.split("_", 2)
                if len(parts) == 3:
                    item_type = parts[1]

        kept = [b for b in original_blocks if b.get("type") != "actions"]
        reopen_block: List[Dict[str, Any]] = []
        if item_id:
            reopen_block = [{
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
            }]
        new_blocks = (
            [{"type": "header", "text": {"type": "plain_text", "text": header_text}}]
            + kept
            + [{"type": "section", "text": {"type": "mrkdwn", "text": ":completed: DONE :completed:"}}]
            + reopen_block
        )
        client.chat_update(channel=channel, ts=ts, blocks=new_blocks, text=header_text)
    except Exception as e:
        logging.warning(f"Could not mark item as actioned: {e}")


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

@app.action("modqueue_action")
def handle_modqueue_action(ack: Any, body: Dict[str, Any], client: Any) -> None:
    """Handle a selection from the modqueue action dropdown.

    Option values are encoded as ``'{action}|{item_id}|{item_type}|{author}'``.
    Approve executes immediately; Remove / Warn / Ban open a modal to collect
    further input before acting.

    Args:
        ack: Slack Bolt acknowledgement callable — must be called within 3 s.
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
        action, item_id, item_type, author = value.split("|", 3)
    except ValueError:
        logging.warning(f"modqueue_action: unexpected value format: {value!r}")
        return

    ts: str = body["container"]["message_ts"]
    reddit_link: str = _reddit_link_from_body(body)

    if action == "approve":
        try:
            reddit.approve_item(item_id)
            mod_reddit = mod_slack_ids.get(user_id.upper(), f"<@{user_id}>")
            info = reddit.get_item_info(channel, item_id)
            header = _build_item_header(client, item_id, reddit_link=reddit_link, channel=channel, ts=ts, queue_num=info.get("queue_num"))
            _mark_item_as_actioned(client, channel, ts, f"✅ APPROVED — {mod_reddit}")
            client.chat_postMessage(channel=channel, thread_ts=ts, text=f":white_check_mark: *Approved* by {mod_reddit}")
        except Exception as e:
            client.chat_postEphemeral(channel=channel, user=user_id, text=f"Failed to approve: {e}")

    elif action == "remove":
        client.views_open(
            trigger_id=body["trigger_id"],
            view=build_remove_modal(item_id, item_type, channel=channel, ts=ts, reddit_link=reddit_link, reasons=reddit.get_removal_reasons()),
        )

    elif action == "warn":
        client.views_open(
            trigger_id=body["trigger_id"],
            view=build_warn_modal(author, channel=channel, ts=ts, reddit_link=reddit_link, item_id=item_id),
        )

    elif action == "ban":
        client.views_open(
            trigger_id=body["trigger_id"],
            view=build_ban_modal(author, channel=channel, ts=ts, reddit_link=reddit_link, item_id=item_id),
        )

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
    has_preset: bool = metadata.get("has_preset", False)
    values = body["view"]["state"]["values"]

    # Reason: preset select gives a reason_id; plain text gives the message directly
    if has_preset:
        reason_id: str = values["reason_block"]["reason_input"]["selected_option"]["value"]
        notes: str = values["notes_block"]["notes_input"].get("value") or ""
    else:
        reason_id = ""
        notes = values["reason_block"]["reason_input"].get("value") or ""

    delivery: str = values["delivery_block"]["delivery_input"]["selected_option"]["value"]

    try:
        result = reddit.remove_item(item_id, reason_id=reason_id, notes=notes, delivery=delivery, item_type=item_type)
        mod_reddit = mod_slack_ids.get(user_id.upper(), f"<@{user_id}>")
        info = reddit.get_item_info(channel, item_id)
        header = _build_item_header(client, item_id, reddit_link=reddit_link, channel=channel, ts=ts, queue_num=info.get("queue_num"))
        delivery_label = {"public": "public reply", "private": "private message", "silent": "silently"}.get(delivery, delivery)
        detail_parts = []
        if reason_id:
            detail_parts.append(f"Reason ID: {reason_id}")
        if notes:
            detail_parts.append(f"Notes: {notes}")
        detail_parts.append(f"Delivery: {delivery_label}")
        details = " | ".join(detail_parts)
        if channel and ts:
            _mark_item_as_actioned(client, channel, ts, f"🗑️ REMOVED — {mod_reddit}")
            client.chat_postMessage(channel=channel, thread_ts=ts, text=f":x: *Removed* by {mod_reddit}\n{details}")
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
        reddit.warn_user(username, message)
        mod_reddit = mod_slack_ids.get(user_id.upper(), f"<@{user_id}>")
        if item_id:
            info = reddit.get_item_info(channel, item_id)
            header = _build_item_header(client, item_id, reddit_link=reddit_link, channel=channel, ts=ts, queue_num=info.get("queue_num"))
            if channel and ts:
                _mark_item_as_actioned(client, channel, ts, f":shell: WARNED — {mod_reddit}")
        else:
            header = _build_item_header(client, f"u/{username}", reddit_link=reddit_link, channel=channel, ts=ts)
        if channel and ts:
            client.chat_postMessage(channel=channel, thread_ts=ts, text=f":warning: *Warning sent* by {mod_reddit}\nRecipient: u/{username}")
    except Exception as e:
        client.chat_postMessage(
            channel=modqueue_channel or user_id,
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
        reddit.ban_user(username, reason=reason, duration=duration, note=note)
        mod_reddit = mod_slack_ids.get(user_id.upper(), f"<@{user_id}>")
        if item_id:
            info = reddit.get_item_info(channel, item_id)
            header = _build_item_header(client, item_id, reddit_link=reddit_link, channel=channel, ts=ts, queue_num=info.get("queue_num"))
            if channel and ts:
                _mark_item_as_actioned(client, channel, ts, f":no_entry: BANNED — {mod_reddit}")
        else:
            header = _build_item_header(client, f"u/{username}", reddit_link=reddit_link, channel=channel, ts=ts)
        duration_label = f"{duration} days" if duration else "permanent"
        detail_parts = [f"Reason: {reason}", f"Duration: {duration_label}"]
        if note:
            detail_parts.append(f"Note: {note}")
        if channel and ts:
            client.chat_postMessage(channel=channel, thread_ts=ts, text=f":no_entry: *Banned* by {mod_reddit}\n{' | '.join(detail_parts)}")
    except Exception as e:
        client.chat_postMessage(
            channel=modqueue_channel or user_id,
            text=f"<@{user_id}> Failed to ban u/{username}: {e}"
        )


# ---------------------------------------------------------------------------
# Modmail action dropdown handler
# ---------------------------------------------------------------------------

@app.action("modmail_action")
def handle_modmail_action(ack: Any, body: Dict[str, Any], client: Any) -> None:
    """Handle a selection from the modmail action dropdown.

    Option values are encoded as ``'{action}|{conv_id}|{author}'``.
    Reply and Mute act on the conversation; Warn / Ban reuse the modqueue modals.

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
        action, conv_id, author = value.split("|", 2)
    except ValueError:
        logging.warning(f"modmail_action: unexpected value format: {value!r}")
        return

    ts: str = body["container"]["message_ts"]

    if action == "reply":
        client.views_open(
            trigger_id=body["trigger_id"],
            view=build_reply_modal(conv_id, channel=channel, ts=ts),
        )

    elif action == "mute":
        try:
            reddit.mute_conversation(conv_id)
            mod_reddit = mod_slack_ids.get(user_id.upper(), f"<@{user_id}>")
            _replace_buttons_with_status(client, body, f":mute: *Muted* by {mod_reddit}")
        except Exception as e:
            client.chat_postEphemeral(channel=channel, user=user_id, text=f"Failed to mute: {e}")

    elif action == "warn":
        client.views_open(
            trigger_id=body["trigger_id"],
            view=build_warn_modal(author, channel=channel, ts=ts),
        )

    elif action == "ban":
        client.views_open(
            trigger_id=body["trigger_id"],
            view=build_ban_modal(author, channel=channel, ts=ts),
        )


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
    except Exception as e:
        notify_channel = modmail_channel or modqueue_channel
        if notify_channel:
            client.chat_postMessage(channel=notify_channel, text=f"<@{user_id}> Failed to send reply to {conv_id}: {e}")


# ---------------------------------------------------------------------------
# Vote dropdown handler
# ---------------------------------------------------------------------------

@app.action("cast_vote_overflow")
def handle_cast_vote(ack: Any, body: Dict[str, Any], client: Any) -> None:
    """Record a moderator's vote and update the tally in the Slack message.

    Parses the dropdown selected option value (``'{item_id}|{item_type}|{vote_option}'``),
    persists the vote, then updates the ``votes_overflow_{item_id}`` block
    (checkmarks) and ``vote_tally_{item_id}`` block in-place.

    Args:
        ack: Slack Bolt acknowledgement callable.
        body: Full Slack action payload.
        client: Slack ``WebClient`` for API calls.
    """
    ack()
    user_id: str = body["user"]["id"]
    value: str = body["actions"][0]["selected_option"]["value"]

    try:
        item_id, item_type, vote_option = value.split("|", 2)
    except ValueError:
        logging.warning(f"cast_vote: unexpected value format: {value!r}")
        return

    channel: str = body["container"]["channel_id"]
    ts: str = body["container"]["message_ts"]

    reddit.record_vote(channel, item_id, user_id, vote_option)
    votes = reddit.get_votes(channel, item_id)
    tally_text = reddit.format_vote_tally(votes)

    tally_block_id = f"vote_tally_{item_id}"
    new_blocks = [
        {**b, "text": {"type": "mrkdwn", "text": tally_text}} if b.get("block_id") == tally_block_id else b
        for b in body["message"].get("blocks", [])
    ]

    try:
        client.chat_update(channel=channel, ts=ts, blocks=new_blocks, text="Mod report item")
    except Exception as e:
        logging.warning(f"cast_vote: could not update message: {e}")


@app.action("reopen_item")
def handle_reopen_item(ack: Any, body: Dict[str, Any], client: Any) -> None:
    """Restore an actioned modqueue item to its original interactive state.

    Triggered by the Re-open dropdown appended after a moderation action.
    Re-fetches the Reddit item via PRAW, rebuilds the full Block Kit payload
    (including vote and action dropdowns), and updates the message in place.

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
    blocks = reddit.get_item_blocks_for_reopen(channel, item_id)
    if not blocks:
        client.chat_postEphemeral(channel=channel, user=user_id, text="Could not re-open item — it may no longer be accessible on Reddit.")
        return

    try:
        client.chat_update(channel=channel, ts=ts, blocks=blocks, text="Mod report item (re-opened)")
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
    pass  # Absorb message events to prevent Bolt warnings; app_mention handles mentions


_handled_event_ids: set = set()


@app.event("app_mention")
def handle_mention(event: Dict[str, Any], body: Dict[str, Any], say: Any) -> None:
    """Respond to @mentions with hello or help, based on message text.

    Args:
        event: Slack app_mention event payload.
        body: Full request body (used for event ID deduplication).
        say: Bolt helper for posting to the same channel.
    """
    event_id = body.get("event_id")
    if event_id:
        if event_id in _handled_event_ids:
            return
        _handled_event_ids.add(event_id)

    logging.info(f"app_mention received: {event}")
    text: str = event.get("text", "").lower()
    user: str = event.get("user", "")

    if "hello" in text:
        say(f"Hi <@{user}>!")
    elif "help" in text:
        say(text="""Welcome to ReformedBot — where all your dreams come true.

Commands (mention me with):
  *hello* — I'll say hi back
  *help* — You're looking at it

Interactive dropdowns appear on each posted item:
  *Vote* — Cast a non-binding discussion vote
  *Approve* — Approve the post on Reddit
  *Remove* — Remove with a reason (sent to user)
  *Warn User* — Send a modmail warning
  *Ban User* — Ban with reason and optional duration""", thread_ts=event.get("ts"))


# ---------------------------------------------------------------------------
# Background polling thread
# ---------------------------------------------------------------------------

_SUMMARY_INTERVAL: int = 5 * 60  # seconds between queue summaries
_last_summary_key: str = ""  # tracks last posted summary content to avoid duplicates


def _item_id_from_blocks(blocks: List[Dict[str, Any]]) -> Optional[str]:
    """Extract the Reddit item ID from a modqueue Block Kit message."""
    for block in blocks:
        bid = block.get("block_id", "")
        if bid.startswith("vote_tally_"):
            return bid[len("vote_tally_"):]
    return None


def _post_queue_summary(web_client: SlackWebClient) -> None:
    """Post a one-line summary of items still pending in the Reddit modqueue.

    Fetches the live modqueue from Reddit, looks up each item's Slack message
    permalink from the monthly log, and posts a compact summary to
    ``modqueue_channel``.  Skips posting if the summary content is identical
    to the last posted summary (avoids repeating the same state).
    """
    global _last_summary_key
    if not modqueue_channel:
        return
    try:
        current_ids = reddit.get_current_modqueue_ids()

        # Build a deduplication key from the current queue state.
        # Use sorted IDs so order changes don't trigger a new post.
        summary_key = ",".join(sorted(current_ids))

        if summary_key == _last_summary_key:
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
    except Exception as e:
        logging.error(f"Queue summary error: {e}")


def _poll_loop() -> None:
    """Continuously poll Reddit and push new items to configured Slack channels.

    Runs as a daemon thread started at bot launch. Polls the subreddit
    modqueue and modmail conversations every ``POLL_INTERVAL`` seconds
    (configured in ``slack.ini``; default: 60). Uses the same deduplication
    logic as the on-demand commands so items are never posted twice to the
    same channel.

    New modqueue items go to ``MODQUEUE_CHANNEL``; new modmail messages go to
    ``MODMAIL_CHANNEL``. Either channel can be omitted to disable that feed.
    """
    poll_interval: int = int(config.get('Default', 'POLL_INTERVAL', fallback='60'))
    web_client: SlackWebClient = SlackWebClient(token=slack_token)
    last_summary: float = 0.0

    while True:
        try:
            if modqueue_channel:
                total, blocks = reddit.get_modqueue(modqueue_channel, no_repost=True, as_blocks=True)
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
                        reddit.set_item_slack_ts(modqueue_channel, item_id, resp["ts"], permalink=permalink)
                    logging.info(f"Posted modqueue item to {modqueue_channel}")
        except Exception as e:
            logging.error(f"Poller error (modqueue): {e}")

        try:
            if modmail_channel:
                blocks = reddit.get_conversations(modmail_channel, as_blocks=True)
                for block_list in blocks:
                    web_client.chat_postMessage(
                        channel=modmail_channel,
                        blocks=block_list,
                        text="New modmail message"
                    )
                    logging.info(f"Posted modmail item to {modmail_channel}")
        except Exception as e:
            logging.error(f"Poller error (modmail): {e}")

        now = time.time()
        if now - last_summary >= _SUMMARY_INTERVAL:
            _post_queue_summary(web_client)
            last_summary = now

        time.sleep(poll_interval)


if __name__ == "__main__":
    if modqueue_channel or modmail_channel:
        poller_thread = threading.Thread(target=_poll_loop, daemon=True)
        poller_thread.start()
        logging.info("Polling thread started.")
    else:
        logging.warning("No MODQUEUE_CHANNEL or MODMAIL_CHANNEL configured — polling disabled.")

    logging.info("Starting ReformedBot via Socket Mode...")
    SocketModeHandler(app, app_token).start()
