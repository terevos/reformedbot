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
import random
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


def build_warn_modal(username: str, channel: str = "", ts: str = "", reddit_link: str = "") -> Dict[str, Any]:
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
        "private_metadata": json.dumps({"username": username, "channel": channel, "ts": ts, "reddit_link": reddit_link}),
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


def build_ban_modal(username: str, channel: str = "", ts: str = "", reddit_link: str = "") -> Dict[str, Any]:
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
        "private_metadata": json.dumps({"username": username, "channel": channel, "ts": ts, "reddit_link": reddit_link}),
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
        kept = [b for b in original_blocks if b.get("type") != "actions"]
        new_blocks = (
            [{"type": "header", "text": {"type": "plain_text", "text": header_text}}]
            + kept
            + [{"type": "section", "text": {"type": "mrkdwn", "text": ":completed:"}}]
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
            if modqueue_channel:
                client.chat_postMessage(channel=modqueue_channel, text=f"{header}\n:white_check_mark: *Approved* by {mod_reddit}")
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
            view=build_warn_modal(author, channel=channel, ts=ts, reddit_link=reddit_link),
        )

    elif action == "ban":
        client.views_open(
            trigger_id=body["trigger_id"],
            view=build_ban_modal(author, channel=channel, ts=ts, reddit_link=reddit_link),
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
        if modqueue_channel:
            client.chat_postMessage(
                channel=modqueue_channel,
                text=f"{header}\n:x: *Removed* by {mod_reddit}\n{details}"
            )
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
    message: str = body["view"]["state"]["values"]["warn_block"]["warn_input"]["value"]

    try:
        result = reddit.warn_user(username, message)
        mod_reddit = mod_slack_ids.get(user_id.upper(), f"<@{user_id}>")
        header = _build_item_header(client, f"u/{username}", reddit_link=reddit_link, channel=channel, ts=ts)
        notify_channel = modqueue_channel or modmail_channel
        if notify_channel:
            client.chat_postMessage(
                channel=notify_channel,
                text=f"{header}\n:warning: *Warning sent* by {mod_reddit}\nRecipient: u/{username}"
            )
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
        result = reddit.ban_user(username, reason=reason, duration=duration, note=note)
        mod_reddit = mod_slack_ids.get(user_id.upper(), f"<@{user_id}>")
        header = _build_item_header(client, f"u/{username}", reddit_link=reddit_link, channel=channel, ts=ts)
        duration_label = f"{duration} days" if duration else "permanent"
        detail_parts = [f"Reason: {reason}", f"Duration: {duration_label}"]
        if note:
            detail_parts.append(f"Note: {note}")
        notify_channel = modqueue_channel or modmail_channel
        if notify_channel:
            client.chat_postMessage(
                channel=notify_channel,
                text=f"{header}\n:no_entry: *Banned* by {mod_reddit}\n{' | '.join(detail_parts)}"
            )
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
            # Post as a thread on the original modmail message
            client.chat_postMessage(channel=channel, thread_ts=ts, text=reply_text)
            # Also post to the main channel (not threaded)
            client.chat_postMessage(channel=channel, text=reply_text)
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



# ---------------------------------------------------------------------------
# Text command handlers
# ---------------------------------------------------------------------------

REPORT_PATTERN: re.Pattern[str] = re.compile(r"(report|queue|-r)", re.IGNORECASE)
MAIL_PATTERN: re.Pattern[str] = re.compile(r"(mail|conv|modmail)", re.IGNORECASE)

EMPTY_QUEUE_MESSAGES: List[str] = [
    "Nothing in the report queue. I wish you a pleasant day.",
    "Nothing here, I am pleased to report.",
    "There is no queue! Hurray!",
    "Amazingly, nothing has been reported.",
    "I am happy to report that there are no reports. Take a break and relax.",
    "Anything to serve you. Except that I have no reports to serve you with.",
    "I have no reports, but I could make something up if you really want."
]

EMPTY_MAIL_MESSAGES: List[str] = [
    "Reports and mail are my joy. Alas, I have no mail to report.",
    "No mail.",
    "Your mailbox is empty.",
    "You have NO mail.",
    "There is no mail to report, but I'm sure that doesn't mean that no one loves you. I love you!"
]

UNKNOWN_MESSAGES: List[str] = [
    "I do not know that command.",
    "I wish I could help you, but I don't understand.",
    "I apologize. I'm not familiar with that command",
    "I'm sorry. I don't know what that means.",
    "Your sin smells to high heaven."
]


@app.message(re.compile(r"hello", re.IGNORECASE))
def handle_hello(message: Dict[str, Any], say: Any) -> None:
    """Respond to a greeting message with a personalised hello.

    Args:
        message: Slack message event payload.
        say: Bolt helper for posting to the same channel.
    """
    if not _is_allowed_channel(message["channel"]):
        return
    user = message.get("user", "")
    say(f"Hi <@{user}>!")


@app.message(re.compile(r"\bhelp\b", re.IGNORECASE))
def handle_help(message: Dict[str, Any], say: Any) -> None:
    """Respond to a help request with a command reference.

    Args:
        message: Slack message event payload.
        say: Bolt helper for posting to the same channel.
    """
    if not _is_allowed_channel(message["channel"]):
        return
    say("""Welcome to ReformedBot — where all your dreams come true.

Commands (mention me + keyword):
  *hello* — I'll say hi back
  *help* — You're looking at it
  *report* / *queue* / *-r* — List new items in the mod queue
  *report full* — List all mod queue items (including already-posted)
  *mail* / *conv* / *modmail* — List new modmail conversations

Interactive buttons appear on each posted item:
  *Approve* — Approve the post on Reddit
  *Remove* — Remove with a reason (sent to user)
  *Warn User* — Send a modmail warning
  *Ban User* — Ban with reason and optional duration""")


@app.message(REPORT_PATTERN)
def handle_report(message: Dict[str, Any], say: Any, client: Any) -> None:
    """Fetch and post current modqueue items to the requesting channel.

    Responds with Block Kit messages, each containing inline moderation
    buttons. If the message text includes ``'full'``, already-posted items
    are included; otherwise only new items are shown.

    Args:
        message: Slack message event payload.
        say: Bolt helper for posting to the same channel.
        client: Slack ``WebClient`` for sending Block Kit messages.
    """
    channel_id: str = message["channel"]
    if not _is_allowed_channel(channel_id):
        return
    text: str = message.get("text", "").lower()
    no_repost: bool = "full" not in text
    try:
        total, blocks = reddit.get_modqueue(channel_id, no_repost=no_repost, as_blocks=True)
        if not blocks:
            say(
                random.choice(EMPTY_QUEUE_MESSAGES) if total == 0
                else f"Total in the queue: {total}. Run 'report full' to see which ones."
            )
            return
        say(f"=== MODQUEUE — {total} total item(s) ===")
        for block_list in blocks:
            client.chat_postMessage(channel=channel_id, blocks=block_list, text="Mod report item")
    except Exception as e:
        import traceback
        say(f"Could not grab the modqueue. Exception: {e}.\n```{traceback.format_exc()}```")


@app.message(MAIL_PATTERN)
def handle_mail(message: Dict[str, Any], say: Any, client: Any) -> None:
    """Fetch and post new modmail conversations to the requesting channel.

    Responds with Block Kit messages, each containing Warn / Ban buttons.

    Args:
        message: Slack message event payload.
        say: Bolt helper for posting to the same channel.
        client: Slack ``WebClient`` for sending Block Kit messages.
    """
    channel_id: str = message["channel"]
    if not _is_allowed_channel(channel_id):
        return
    try:
        blocks = reddit.get_conversations(channel_id, as_blocks=True)
        if not blocks:
            say(random.choice(EMPTY_MAIL_MESSAGES))
            return
        say("=== MODMAIL CONVERSATIONS ===")
        for block_list in blocks:
            client.chat_postMessage(channel=channel_id, blocks=block_list, text="Modmail message")
    except Exception as e:
        import traceback
        say(f"Could not grab modmail. Exception: {e}.\n```{traceback.format_exc()}```")


# ---------------------------------------------------------------------------
# Background polling thread
# ---------------------------------------------------------------------------

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

    while True:
        try:
            if modqueue_channel:
                total, blocks = reddit.get_modqueue(modqueue_channel, no_repost=True, as_blocks=True)
                for block_list in blocks:
                    web_client.chat_postMessage(
                        channel=modqueue_channel,
                        blocks=block_list,
                        text="New mod report"
                    )
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
