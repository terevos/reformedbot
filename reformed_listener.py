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

reddit: RedditActions = RedditActions('reformed')

# ---------------------------------------------------------------------------
# Slack Bolt app
# ---------------------------------------------------------------------------
app: App = App(token=slack_token, signing_secret=signing_secret)


def is_authorized_mod(slack_user_id: str) -> bool:
    """Return ``True`` if *slack_user_id* is in the authorised moderator list.

    The list is populated from the ``[Mods]`` section of ``slack.ini`` at
    startup. Comparison is case-insensitive (Slack user IDs are uppercased).

    Args:
        slack_user_id: Slack user ID string (e.g. ``'U0123456789'``).
    """
    return slack_user_id.upper() in mod_slack_ids


# ---------------------------------------------------------------------------
# Modal builders
# ---------------------------------------------------------------------------

def build_remove_modal(item_id: str, item_type: str = "submission") -> Dict[str, Any]:
    """Build the Slack modal view payload for the Remove action.

    The modal collects a free-text removal reason from the moderator.
    ``item_id`` and ``item_type`` are stored in ``private_metadata`` so they
    survive the round-trip and are available in the view-submission handler.

    Args:
        item_id: Reddit item ID (bare, e.g. ``'abc123'``).
        item_type: ``'submission'`` (default) or ``'comment'``.

    Returns:
        A Slack modal view dict suitable for ``client.views_open``.
    """
    return {
        "type": "modal",
        "callback_id": "removal_reason_submitted",
        "private_metadata": json.dumps({"item_id": item_id, "item_type": item_type}),
        "title": {"type": "plain_text", "text": "Remove Post"},
        "submit": {"type": "plain_text", "text": "Remove"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": "reason_block",
                "label": {"type": "plain_text", "text": "Removal Reason"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "reason_input",
                    "multiline": True,
                    "placeholder": {
                        "type": "plain_text",
                        "text": "Explain why this is being removed (sent to user)"
                    }
                }
            }
        ]
    }


def build_warn_modal(username: str) -> Dict[str, Any]:
    """Build the Slack modal view payload for the Warn User action.

    The modal collects a free-text warning message from the moderator.
    ``username`` is stored in ``private_metadata``.

    Args:
        username: Reddit username of the user to warn (without ``u/`` prefix).

    Returns:
        A Slack modal view dict suitable for ``client.views_open``.
    """
    return {
        "type": "modal",
        "callback_id": "warn_submitted",
        "private_metadata": json.dumps({"username": username}),
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


def build_ban_modal(username: str) -> Dict[str, Any]:
    """Build the Slack modal view payload for the Ban User action.

    The modal collects ban reason, optional duration (days), and an optional
    internal moderator note. ``username`` is stored in ``private_metadata``.

    Args:
        username: Reddit username of the user to ban (without ``u/`` prefix).

    Returns:
        A Slack modal view dict suitable for ``client.views_open``.
    """
    return {
        "type": "modal",
        "callback_id": "ban_submitted",
        "private_metadata": json.dumps({"username": username}),
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
# Button action handlers
# ---------------------------------------------------------------------------

@app.action("approve_item")
def handle_approve(ack: Any, body: Dict[str, Any], client: Any) -> None:
    """Handle a click on the *Approve* button attached to a modqueue item.

    Verifies that the clicking user is an authorised moderator, approves the
    Reddit item via PRAW, then replaces the action buttons with a confirmation
    line. Unauthorised users receive an ephemeral error message.

    Args:
        ack: Slack Bolt acknowledgement callable — must be called within 3 s.
        body: Full Slack action payload.
        client: Slack ``WebClient`` for API calls.
    """
    ack()
    user_id: str = body["user"]["id"]
    if not is_authorized_mod(user_id):
        client.chat_postEphemeral(
            channel=body["container"]["channel_id"],
            user=user_id,
            text="You are not authorized to take moderation actions."
        )
        return

    item_id: str = body["actions"][0]["value"]
    try:
        result = reddit.approve_item(item_id)
        mod_reddit = mod_slack_ids.get(user_id.upper(), f"<@{user_id}>")
        _replace_buttons_with_status(client, body, f":white_check_mark: *Approved* by {mod_reddit} — {result}")
    except Exception as e:
        client.chat_postEphemeral(
            channel=body["container"]["channel_id"],
            user=user_id,
            text=f"Failed to approve: {e}"
        )


@app.action("remove_item")
def handle_remove_click(ack: Any, body: Dict[str, Any], client: Any) -> None:
    """Handle a click on the *Remove* button attached to a modqueue item.

    Opens a modal so the moderator can provide a removal reason before the
    item is removed. The actual removal happens in ``handle_removal_submitted``.

    Args:
        ack: Slack Bolt acknowledgement callable.
        body: Full Slack action payload.
        client: Slack ``WebClient`` for API calls.
    """
    ack()
    user_id: str = body["user"]["id"]
    if not is_authorized_mod(user_id):
        client.chat_postEphemeral(
            channel=body["container"]["channel_id"],
            user=user_id,
            text="You are not authorized to take moderation actions."
        )
        return

    item_id: str = body["actions"][0]["value"]
    block_id: str = body["actions"][0].get("block_id", "")
    item_type = "comment" if "comment" in block_id else "submission"
    client.views_open(
        trigger_id=body["trigger_id"],
        view=build_remove_modal(item_id, item_type)
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
    reason: str = body["view"]["state"]["values"]["reason_block"]["reason_input"]["value"]

    try:
        result = reddit.remove_item(item_id, reason=reason, item_type=item_type)
        mod_reddit = mod_slack_ids.get(user_id.upper(), f"<@{user_id}>")
        if modqueue_channel:
            client.chat_postMessage(
                channel=modqueue_channel,
                text=f":x: *Removed* by {mod_reddit} — {result}\n*Reason:* {reason}"
            )
    except Exception as e:
        client.chat_postMessage(
            channel=modqueue_channel or user_id,
            text=f"<@{user_id}> Failed to remove item `{item_id}`: {e}"
        )


@app.action("warn_user")
def handle_warn_click(ack: Any, body: Dict[str, Any], client: Any) -> None:
    """Handle a click on the *Warn User* button.

    Opens a modal so the moderator can compose a warning message. The actual
    modmail is sent in ``handle_warn_submitted``.

    Args:
        ack: Slack Bolt acknowledgement callable.
        body: Full Slack action payload.
        client: Slack ``WebClient`` for API calls.
    """
    ack()
    user_id: str = body["user"]["id"]
    if not is_authorized_mod(user_id):
        client.chat_postEphemeral(
            channel=body["container"]["channel_id"],
            user=user_id,
            text="You are not authorized to take moderation actions."
        )
        return

    username: str = body["actions"][0]["value"]
    client.views_open(trigger_id=body["trigger_id"], view=build_warn_modal(username))


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
    message: str = body["view"]["state"]["values"]["warn_block"]["warn_input"]["value"]

    try:
        result = reddit.warn_user(username, message)
        mod_reddit = mod_slack_ids.get(user_id.upper(), f"<@{user_id}>")
        notify_channel = modqueue_channel or modmail_channel
        if notify_channel:
            client.chat_postMessage(
                channel=notify_channel,
                text=f":warning: *Warning sent* by {mod_reddit} — {result}"
            )
    except Exception as e:
        client.chat_postMessage(
            channel=modqueue_channel or user_id,
            text=f"<@{user_id}> Failed to warn u/{username}: {e}"
        )


@app.action("ban_user")
def handle_ban_click(ack: Any, body: Dict[str, Any], client: Any) -> None:
    """Handle a click on the *Ban User* button.

    Opens a modal so the moderator can provide ban reason, duration, and an
    optional internal note. The actual ban is applied in ``handle_ban_submitted``.

    Args:
        ack: Slack Bolt acknowledgement callable.
        body: Full Slack action payload.
        client: Slack ``WebClient`` for API calls.
    """
    ack()
    user_id: str = body["user"]["id"]
    if not is_authorized_mod(user_id):
        client.chat_postEphemeral(
            channel=body["container"]["channel_id"],
            user=user_id,
            text="You are not authorized to take moderation actions."
        )
        return

    username: str = body["actions"][0]["value"]
    client.views_open(trigger_id=body["trigger_id"], view=build_ban_modal(username))


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
        notify_channel = modqueue_channel or modmail_channel
        if notify_channel:
            client.chat_postMessage(
                channel=notify_channel,
                text=f":no_entry: *Banned* by {mod_reddit} — {result}"
            )
    except Exception as e:
        client.chat_postMessage(
            channel=modqueue_channel or user_id,
            text=f"<@{user_id}> Failed to ban u/{username}: {e}"
        )


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
    user = message.get("user", "")
    say(f"Hi <@{user}>!")


@app.message(re.compile(r"\bhelp\b", re.IGNORECASE))
def handle_help(message: Dict[str, Any], say: Any) -> None:
    """Respond to a help request with a command reference.

    Args:
        message: Slack message event payload.
        say: Bolt helper for posting to the same channel.
    """
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
