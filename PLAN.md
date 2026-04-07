# Next-Gen Slack-Reddit Moderation Bot — Upgrade Plan

## Context

The existing `reformedbot` is a read-only Slack bot for r/reformed moderation. It can fetch mod reports and modmail on demand but cannot take any actions (approve, remove, warn, ban) and uses Slack's deprecated RTM API. Moderators currently must visit Reddit to act on reports. This upgrade adds real-time push notifications and full moderation action capabilities directly from Slack using interactive buttons.

---

## Architecture Overview

### Migration: RTM → Slack Bolt + Socket Mode

Replace `slack.RTMClient` (deprecated) with `slack-bolt` + Socket Mode. This enables:
- Button/action interactivity (Block Kit actions)
- Modal dialogs for collecting input (removal reasons, ban duration, etc.)
- No need for a public HTTP server (Socket Mode uses WebSockets)

### Two-process design

1. **`reformed_listener.py`** (rewritten) — Slack Bolt app: handles commands, button interactions, moderation actions
2. **Background polling thread** (inside `reformed_listener.py`) — polls Reddit every 60s and auto-posts new items to configured channels with action buttons

---

## Files Changed

| File | Change |
|------|--------|
| `reddit_actions.py` | Added moderation action methods + Block Kit block builders |
| `reformed_listener.py` | Rewritten with Slack Bolt + Socket Mode + Block Kit + action handlers |
| `requirements.txt` | Replaced deprecated `slack`/`slackclient`/`slackeventsapi` with `slack-bolt` |
| `slack.ini.example` | New — documents all required config fields |

---

## Phase 1: Reddit Action Methods (`reddit_actions.py`)

Five new methods added to `RedditActions`:

```python
def approve_item(self, item_id: str) -> str:
    """Approve a submission or comment."""

def remove_item(self, item_id: str, reason: str = "", item_type: str = "submission") -> str:
    """Remove and optionally send removal reason via modmail."""

def warn_user(self, username: str, message: str) -> str:
    """Send a modmail warning to a Reddit user."""

def ban_user(self, username: str, reason: str, duration: int = None, note: str = "") -> str:
    """Ban a user. duration=None is permanent, otherwise days."""

def unban_user(self, username: str) -> str:
    """Unban a user."""
```

PRAW calls used:
- Approve: `item.mod.approve()`
- Remove: `item.mod.remove()` + `subreddit.mod.send_removal_message()`
- Ban: `subreddit.banned.add(username, ban_reason, note, duration)`
- Modmail: `subreddit.modmail.create(subject, body, recipient)`

Both `get_modqueue()` and `get_conversations()` gained an `as_blocks=True` flag that returns Slack Block Kit payloads instead of plain text strings.

---

## Phase 2: Slack Block Kit Message Format

Each mod report/mail posted to Slack uses Block Kit with inline action buttons.

### Modqueue item
```
┌─────────────────────────────────────────────────────┐
│ #1 | Submission | View on Reddit                    │
│ User: u/someuser                                    │
│ Title: Some post title — URL                        │
│ Reports:                                            │
│ • User: Breaks rule 4                               │
│ • Mod (terevos2): Spam                              │
├─────────────────────────────────────────────────────┤
│ [ Approve ✓ ]  [ Remove 🗑 ]  [ Warn User ]  [ Ban ] │
└─────────────────────────────────────────────────────┘
```

### Modmail message
```
┌─────────────────────────────────────────────────────┐
│ New Modmail | View                                  │
│ From: u/author | Subject: some subject              │
│ Date: 2026-04-07                                    │
│ Message body text...                                │
├─────────────────────────────────────────────────────┤
│ [ Warn User ]  [ Ban User ]                         │
└─────────────────────────────────────────────────────┘
```

---

## Phase 3: Slack Bolt App (`reformed_listener.py`)

```python
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

app = App(token=slack_token, signing_secret=signing_secret)

# Text command handlers (replaces deprecated RTM say_hello)
@app.message(re.compile(r"report|queue|-r"))
def handle_report(message, say, client): ...

@app.message(re.compile(r"mail|conv|modmail"))
def handle_mail(message, say, client): ...

# Button action handlers
@app.action("approve_item")
def handle_approve(ack, body, client): ...      # instant action + message update

@app.action("remove_item")
def handle_remove_click(ack, body, client): ... # opens modal to collect reason

@app.action("warn_user")
def handle_warn_click(ack, body, client): ...   # opens modal for warning message

@app.action("ban_user")
def handle_ban_click(ack, body, client): ...    # opens modal for reason + duration

# Modal submission handlers
@app.view("removal_reason_submitted")
def handle_removal_submitted(ack, body, client): ...

@app.view("warn_submitted")
def handle_warn_submitted(ack, body, client): ...

@app.view("ban_submitted")
def handle_ban_submitted(ack, body, client): ...

if __name__ == "__main__":
    SocketModeHandler(app, app_token).start()
```

### Authorization

Only Slack user IDs listed in the `[Mods]` section of `slack.ini` can click action buttons. Unauthorized clicks get an ephemeral "not authorized" message visible only to them.

After any action is taken, the Slack message is updated in-place (buttons replaced with a confirmation line) to prevent double-actions.

---

## Phase 4: Real-time Polling

A daemon thread runs inside the main process, polling Reddit every `POLL_INTERVAL` seconds (default: 60) and auto-posting new items to configured channels:

```python
def _poll_loop():
    while True:
        if modqueue_channel:
            total, blocks = reddit.get_modqueue(modqueue_channel, no_repost=True, as_blocks=True)
            for block_list in blocks:
                web_client.chat_postMessage(channel=modqueue_channel, blocks=block_list)
        if modmail_channel:
            blocks = reddit.get_conversations(modmail_channel, as_blocks=True)
            for block_list in blocks:
                web_client.chat_postMessage(channel=modmail_channel, blocks=block_list)
        time.sleep(poll_interval)

threading.Thread(target=_poll_loop, daemon=True).start()
```

The existing monthly JSON deduplication files (`logs/YYYYMM-modlog.json`) prevent duplicate posts.

---

## Phase 5: Configuration (`slack.ini`)

See `slack.ini.example` for the full template. New keys required:

```ini
[Default]
API_TOKEN = xoxb-...         # Bot token (existing)
APP_TOKEN = xapp-...         # Socket Mode app-level token (NEW)
SIGNING_SECRET = ...         # Slack signing secret (NEW)
POLL_INTERVAL = 60           # Seconds between Reddit polls (NEW, optional)

[Channels]
MODQUEUE_CHANNEL = C123...   # Auto-push mod reports here (NEW)
MODMAIL_CHANNEL  = C456...   # Auto-push modmail here (NEW)

[Mods]
# Slack user ID → Reddit username. Only these users can take mod actions.
U0123456789 = terevos2
UABCDEFGHIJ = bishopofreddit
```

---

## Phase 6: Dependency Changes (`requirements.txt`)

**Removed:** `slack`, `slackclient`, `slackeventsapi` (all deprecated)

**Added:** `slack-bolt` (includes `slack-sdk` with `WebClient`)

---

## Slack App Setup (one-time admin steps)

1. Enable **Socket Mode** in the Slack app dashboard → generates the `xapp-` token
2. Add bot token scopes: `chat:write`, `channels:read`, `channels:history`, `im:write`
3. Enable **Interactivity** (required for buttons and modals)
4. Subscribe to bot events: `message.channels`, `message.im`
5. Reinstall app to workspace

---

## Key Design Decisions

- **Modals for input**: Remove/warn/ban require free-text input — button click opens a modal, submission triggers the action.
- **In-place message update**: After approve/remove, `client.chat_update()` replaces buttons with a "✓ Action by @mod" line, preventing double-clicks.
- **Thread-based polling**: PRAW is synchronous; threads are simpler than async and Slack Bolt coexists fine with daemon threads.
- **60s poll interval**: Leaves ample headroom under Reddit's 60 req/min OAuth rate limit (one poll = 1–3 API calls).
- **JSON deduplication preserved**: Existing monthly log files continue preventing duplicate Slack posts.

---

## Verification

1. `pip install -r requirements.txt`
2. Fill in `slack.ini` from `slack.ini.example`
3. `python reformed_listener.py` — bot connects via Socket Mode (no port required)
4. In Slack: `@bot report` → Block Kit messages appear with action buttons
5. Click **Approve** → message updates with confirmation, post approved on Reddit
6. Click **Remove** → modal opens, enter reason, submit → post removed, user notified
7. `@bot mail` → modmail shown with Warn/Ban buttons
8. Click **Warn** → modal for message → modmail sent to user
9. Click **Ban** → modal for reason + duration → user banned on subreddit
10. Leave bot running → new mod reports appear automatically every 60s
