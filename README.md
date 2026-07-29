# ReformedBot v2

A Slack bot that surfaces Reddit r/reformed moderation activity directly in Slack with interactive dropdowns for taking mod actions — without ever leaving Slack.

---

## Mod Reports

New reported submissions and comments are automatically posted to the modqueue channel as rich cards containing the item number, type, author, content, and all report reasons.

### Voting

Each report card has a **Vote dropdown** for non-binding discussion before taking action:

| Option | Notes |
|--------|-------|
| ✅ Approve | Cancels any Remove, Remove + Ban, or Spam votes |
| ❌ Remove | Cancels any Approve vote |
| 💭 Discuss | |
| 🤷 Meh | |
| 🎉 Remove + Ban | Cancels any Approve vote |
| 🔒 Lock | |
| 🐚 Warn | |
| 🥫 Spam | Cancels any Approve vote |
| ❓ Huh? | |

- Each mod can hold multiple votes simultaneously
- Selecting a vote you already cast toggles it off
- Opposing votes (e.g. Approve vs. Remove) automatically cancel each other
- The live tally updates immediately on the card, showing each option with a count and voter names

Votes are for mod discussion only — they do not automatically take action on Reddit.

### Actions

The **Actions dropdown** on each report card supports:

- **Approve on Reddit** — Approves the item immediately. Posts a thread reply confirming who approved it.
- **Remove on Reddit** — Opens a modal to select a removal reason, add notes, and choose delivery method (public reply, private message, or silent). Posts a thread reply with details.
- **Warn User on Reddit** — Opens a modal to compose a warning message sent as modmail to the user. Posts a thread reply.
- **Ban User on Reddit** — Opens a modal to enter ban reason, duration (blank = permanent), and an optional mod note. Posts a thread reply.

When an action is taken the card is updated in place: dropdowns are removed, a status header is added (e.g. `✅ APPROVED — username`), and a `:completed: DONE :completed:` marker appears at the bottom. A **Re-open** dropdown also appears to restore the full interactive card if needed.

### Report Summaries

Every 5 minutes the bot posts a one-line summary of items still pending:

```
🕐 3 item(s) still pending: #1 | #3 | #7
```

Each number links directly to its Slack message. If the queue is clear: `:white_check_mark: Mod queue is clear.` Duplicate summaries are suppressed if the queue state hasn't changed.

---

## Modmail

New modmail conversations are posted as top-level messages in the modmail channel and assigned a sequential `#N` number. Replies within the same conversation are posted as **thread replies**, keeping each conversation together. Auto-generated Reddit messages (mod invitations, approved-user additions, etc.) are silently skipped.

### Actions

The action dropdown on each modmail message supports:

- **Reply on Reddit** — Opens a modal to compose a reply sent from the mod team (author hidden). Posts a thread reply in Slack confirming who replied. Marks the conversation **done**.
- **Archive on Reddit** — Archives the conversation on Reddit. Marks **done**. Replaces the action dropdown with a single **Unarchive on Reddit** option.
- **Mute on Reddit** — Mutes the conversation for 72 hours. Marks **done**.
- **Warn User on Reddit** — Same as warn from a report card.
- **Ban User on Reddit** — Same as ban from a report card.

### Done and Re-opened

A conversation is marked **done** when any of the following occur:

| Trigger | How |
|---|---|
| Mod sends a reply via bot | Reply modal submitted |
| Mod archives via bot | Archive selected in dropdown |
| Mod mutes via bot | Mute selected in dropdown |
| Archived directly on Reddit | Detected on next poll |

When done, the top-level message gains a status header and `:completed: DONE :completed:` marker.

A conversation is **re-opened** when a new message arrives from a non-mod. The new message is posted as a thread reply and the top-level message is updated to show `🔄 REOPENED`. Unarchiving via the bot also re-opens the conversation and restores the full action dropdown.

### Modmail Summaries

Every 5 minutes the bot posts a summary of all open conversations:

```
💬 2 open modmail thread(s):
• #1 u/username — Subject line
• #3 u/other_user — Another subject
```

Each entry links directly to the Slack thread. If all conversations are resolved: `:white_check_mark: All modmail conversations are resolved.` Duplicate summaries are suppressed if the open set hasn't changed.

---

## Authorization

Only Slack users listed in the `[Mods]` section of `slack.ini` can use action dropdowns. Unauthorized clicks receive a private ephemeral error visible only to them.

---

## Setup

### Requirements

- Python 3.13
- `praw` and `slack-bolt` (see `requirements.txt`)

Install with pipenv:

```
pipenv install
```

Or with pip:

```
pip install -r requirements.txt
```

### Configuration Files

Two config files are required:

**`praw.ini`** — Reddit OAuth credentials for the `reformedbot` PRAW profile. See [PRAW docs](https://praw.readthedocs.io/en/stable/getting_started/configuration/prawini.html).

**`slack.ini`** — Copy from `slack.ini.example` and fill in:

```ini
[Default]
API_TOKEN = xoxb-your-bot-token-here
APP_TOKEN = xapp-your-app-level-token-here
SIGNING_SECRET = your-signing-secret-here
POLL_INTERVAL = 30

[Channels]
MODQUEUE_CHANNEL = mod_actions
MODMAIL_CHANNEL  = mod_mail

[Mods]
U0123456789 = reddit_username
UABCDEFGHIJ = another_mod
```

Channel values may be a channel name (`mod_actions`) or a Slack channel ID (`C0123456789`); names are resolved to IDs when the bot starts. For a private channel, invite the bot to it first or the lookup will fail. Leave a channel blank to disable auto-posting for that category.

### Slack App

Import `slack_app_manifest.yaml` into your Slack app configuration. The bot uses **Socket Mode** — no public HTTP endpoint is required.

Required OAuth scopes:
- `chat:write` — post messages
- `channels:history` / `groups:history` / `im:history` / `mpim:history` — read messages for commands
- `channels:read` / `groups:read` — look up channel info

### Running

```
python reformed_listener.py
```

> **Avoid `pipenv shell`** — it spawns a subshell that can break terminal input. Instead activate the virtualenv directly:
> ```
> source $(pipenv --venv)/bin/activate
> ```
> Or run without activating:
> ```
> pipenv run python reformed_listener.py
> ```

The bot prevents duplicate instances using a pidfile (`reformedbot.pid`) in the project directory. Starting a second instance from the same directory will exit immediately with an error.

---

## Data Storage

State is stored in two persistent JSON files under `logs/`:

| File | Contents |
|------|----------|
| `logs/modqueue.json` | Report deduplication, vote tallies, Slack message timestamps |
| `logs/modmail.json` | Modmail conversation tracking, thread timestamps, open/done status |

### `logs/modqueue.json` structure

```json
{
  "CHANNEL_ID": {
    "ITEM_ID": {
      "queue_num": 1,
      "report_link": "https://reddit.com/...",
      "item_type": "submission",
      "slack_ts": "1234567890.123456",
      "slack_permalink": "https://workspace.slack.com/archives/...",
      "slack_blocks": [...],
      "votes": {
        "USLACKID": ["approve"]
      }
    }
  }
}
```

### `logs/modmail.json` structure

```json
{
  "CHANNEL_ID": {
    "modmail_conv": {
      "CONV_ID": {
        "conv_num": 1,
        "subject": "Conversation subject",
        "author": "reddit_username",
        "status": "open",
        "slack_ts": "1234567890.123456",
        "slack_permalink": "https://workspace.slack.com/archives/...",
        "messages": {
          "MESSAGE_ID": true
        }
      }
    }
  }
}
```
