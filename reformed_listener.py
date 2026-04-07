#!/usr/bin/env python

import logging
import re
import random
import configparser
import json
import threading

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from reddit_actions import RedditActions

logging.basicConfig(level=logging.INFO)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
config = configparser.ConfigParser()
config.read('slack.ini')

slack_token = config['Default']['API_TOKEN']
app_token = config['Default']['APP_TOKEN']
signing_secret = config['Default']['SIGNING_SECRET']

modqueue_channel = config.get('Channels', 'MODQUEUE_CHANNEL', fallback=None)
modmail_channel = config.get('Channels', 'MODMAIL_CHANNEL', fallback=None)

# Slack user ID → Reddit username (from [Mods] section of slack.ini)
mod_slack_ids = {}
if config.has_section('Mods'):
    for slack_uid, reddit_name in config.items('Mods'):
        mod_slack_ids[slack_uid.upper()] = reddit_name

reddit = RedditActions('reformed')

# ---------------------------------------------------------------------------
# Slack Bolt app
# ---------------------------------------------------------------------------
app = App(token=slack_token, signing_secret=signing_secret)


def is_authorized_mod(slack_user_id: str) -> bool:
    return slack_user_id.upper() in mod_slack_ids


# ---------------------------------------------------------------------------
# Modals
# ---------------------------------------------------------------------------
def build_remove_modal(item_id: str, item_type: str = "submission") -> dict:
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
                    "placeholder": {"type": "plain_text", "text": "Explain why this is being removed (sent to user)"}
                }
            }
        ]
    }


def build_warn_modal(username: str) -> dict:
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


def build_ban_modal(username: str) -> dict:
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


def _replace_buttons_with_status(client, body, status_text: str):
    """Replace the action buttons block in a message with a status line."""
    try:
        channel = body["container"]["channel_id"]
        ts = body["container"]["message_ts"]
        original_blocks = body["message"].get("blocks", [])
        # Remove the last actions block (the buttons row)
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
def handle_approve(ack, body, client):
    ack()
    user_id = body["user"]["id"]
    if not is_authorized_mod(user_id):
        client.chat_postEphemeral(
            channel=body["container"]["channel_id"],
            user=user_id,
            text="You are not authorized to take moderation actions."
        )
        return

    item_id = body["actions"][0]["value"]
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
def handle_remove_click(ack, body, client):
    ack()
    user_id = body["user"]["id"]
    if not is_authorized_mod(user_id):
        client.chat_postEphemeral(
            channel=body["container"]["channel_id"],
            user=user_id,
            text="You are not authorized to take moderation actions."
        )
        return

    item_id = body["actions"][0]["value"]
    # Detect item type from block_id if available (format: modqueue_ITEM_ID)
    block_id = body["actions"][0].get("block_id", "")
    item_type = "comment" if "comment" in block_id else "submission"
    client.views_open(
        trigger_id=body["trigger_id"],
        view=build_remove_modal(item_id, item_type)
    )


@app.view("removal_reason_submitted")
def handle_removal_submitted(ack, body, client):
    ack()
    user_id = body["user"]["id"]
    metadata = json.loads(body["view"]["private_metadata"])
    item_id = metadata["item_id"]
    item_type = metadata.get("item_type", "submission")
    reason = body["view"]["state"]["values"]["reason_block"]["reason_input"]["value"]

    try:
        result = reddit.remove_item(item_id, reason=reason, item_type=item_type)
        mod_reddit = mod_slack_ids.get(user_id.upper(), f"<@{user_id}>")
        # Post a follow-up since we can't update from a modal view submission directly
        if modqueue_channel:
            client.chat_postMessage(
                channel=modqueue_channel,
                text=f":x: *Removed* by {mod_reddit} — {result}\n*Reason:* {reason}"
            )
    except Exception as e:
        client.chat_postMessage(
            channel=modqueue_channel or body["user"]["id"],
            text=f"<@{user_id}> Failed to remove item `{item_id}`: {e}"
        )


@app.action("warn_user")
def handle_warn_click(ack, body, client):
    ack()
    user_id = body["user"]["id"]
    if not is_authorized_mod(user_id):
        client.chat_postEphemeral(
            channel=body["container"]["channel_id"],
            user=user_id,
            text="You are not authorized to take moderation actions."
        )
        return

    username = body["actions"][0]["value"]
    client.views_open(trigger_id=body["trigger_id"], view=build_warn_modal(username))


@app.view("warn_submitted")
def handle_warn_submitted(ack, body, client):
    ack()
    user_id = body["user"]["id"]
    metadata = json.loads(body["view"]["private_metadata"])
    username = metadata["username"]
    message = body["view"]["state"]["values"]["warn_block"]["warn_input"]["value"]

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
            channel=modqueue_channel or body["user"]["id"],
            text=f"<@{user_id}> Failed to warn u/{username}: {e}"
        )


@app.action("ban_user")
def handle_ban_click(ack, body, client):
    ack()
    user_id = body["user"]["id"]
    if not is_authorized_mod(user_id):
        client.chat_postEphemeral(
            channel=body["container"]["channel_id"],
            user=user_id,
            text="You are not authorized to take moderation actions."
        )
        return

    username = body["actions"][0]["value"]
    client.views_open(trigger_id=body["trigger_id"], view=build_ban_modal(username))


@app.view("ban_submitted")
def handle_ban_submitted(ack, body, client):
    ack()
    user_id = body["user"]["id"]
    metadata = json.loads(body["view"]["private_metadata"])
    username = metadata["username"]
    values = body["view"]["state"]["values"]
    reason = values["reason_block"]["reason_input"]["value"]
    duration_str = values["duration_block"]["duration_input"].get("value")
    note = values["note_block"]["note_input"].get("value") or ""

    duration = None
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
            channel=modqueue_channel or body["user"]["id"],
            text=f"<@{user_id}> Failed to ban u/{username}: {e}"
        )


# ---------------------------------------------------------------------------
# Text command handlers (replaces RTM say_hello)
# ---------------------------------------------------------------------------

REPORT_PATTERN = re.compile(r"(report|queue|-r)", re.IGNORECASE)
MAIL_PATTERN = re.compile(r"(mail|conv|modmail)", re.IGNORECASE)

EMPTY_QUEUE_MESSAGES = [
    "Nothing in the report queue. I wish you a pleasant day.",
    "Nothing here, I am pleased to report.",
    "There is no queue! Hurray!",
    "Amazingly, nothing has been reported.",
    "I am happy to report that there are no reports. Take a break and relax.",
    "Anything to serve you. Except that I have no reports to serve you with.",
    "I have no reports, but I could make something up if you really want."
]

EMPTY_MAIL_MESSAGES = [
    "Reports and mail are my joy. Alas, I have no mail to report.",
    "No mail.",
    "Your mailbox is empty.",
    "You have NO mail.",
    "There is no mail to report, but I'm sure that doesn't mean that no one loves you. I love you!"
]

UNKNOWN_MESSAGES = [
    "I do not know that command.",
    "I wish I could help you, but I don't understand.",
    "I apologize. I'm not familiar with that command",
    "I'm sorry. I don't know what that means.",
    "Your sin smells to high heaven."
]


@app.message(re.compile(r"hello", re.IGNORECASE))
def handle_hello(message, say):
    user = message.get("user", "")
    say(f"Hi <@{user}>!")


@app.message(re.compile(r"\bhelp\b", re.IGNORECASE))
def handle_help(message, say):
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
def handle_report(message, say, client):
    channel_id = message["channel"]
    text = message.get("text", "").lower()
    no_repost = "full" not in text
    try:
        total, blocks = reddit.get_modqueue(channel_id, no_repost=no_repost, as_blocks=True)
        if not blocks:
            say(random.choice(EMPTY_QUEUE_MESSAGES) if total == 0
                else f"Total in the queue: {total}. Run 'report full' to see which ones.")
            return
        say(f"=== MODQUEUE — {total} total item(s) ===")
        for block_list in blocks:
            client.chat_postMessage(channel=channel_id, blocks=block_list, text="Mod report item")
    except Exception as e:
        import traceback
        say(f"Could not grab the modqueue. Exception: {e}.\n```{traceback.format_exc()}```")


@app.message(MAIL_PATTERN)
def handle_mail(message, say, client):
    channel_id = message["channel"]
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
# Polling thread (auto-push new items to configured channels)
# ---------------------------------------------------------------------------

def _poll_loop():
    import time
    poll_interval = int(config.get('Default', 'POLL_INTERVAL', fallback='60'))
    from slack_sdk import WebClient as SlackWebClient
    web_client = SlackWebClient(token=slack_token)

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
    # Start background polling thread if channels are configured
    if modqueue_channel or modmail_channel:
        poller_thread = threading.Thread(target=_poll_loop, daemon=True)
        poller_thread.start()
        logging.info("Polling thread started.")
    else:
        logging.warning("No MODQUEUE_CHANNEL or MODMAIL_CHANNEL configured — polling disabled.")

    logging.info("Starting ReformedBot via Socket Mode...")
    SocketModeHandler(app, app_token).start()
