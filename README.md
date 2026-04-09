# ReformedBot v2

A Slack bot that surfaces Reddit r/reformed moderation activity directly in Slack with interactive dropdowns for taking mod actions — without ever leaving Slack.

---

## Features

### Automatic Posting

The bot runs a background polling thread (default every 60 seconds) that watches Reddit and automatically posts new items to configured Slack channels:

- **Mod reports** — new reported submissions and comments appear in the modqueue channel
- **Modmail** — new conversations and replies to existing conversations appear in the modmail channel
- **Deduplication** — items are tracked in monthly JSON log files so the same item is never posted twice in the same calendar month

### Mod Report Cards

Each report is posted as a rich Slack Block Kit card containing:

- Item number, type (submission or comment), and a link to view on Reddit
- Author's Reddit username (linked to their profile)
- Full content — post title/URL for submissions, italicized body for comments
- All user and mod reports with report text
- A **Vote dropdown** for non-binding discussion votes
- A **Vote tally** showing who voted for what
- An **Actions dropdown** for taking mod action

### Voting

Each report card has a Vote dropdown with 9 options:

| Option | Notes |
|--------|-------|
| Approve | Cancels any Remove, Remove + Ban, or Spam votes |
| Remove | Cancels any Approve vote |
| Discuss | |
| Meh | |
| Remove + Ban | Cancels any Approve vote |
| Lock Thread | |
| Warn | |
| Spam | Cancels any Approve vote |
| Don't Understand Why It's Here | |

- Each mod can hold multiple votes simultaneously
- Selecting a vote you already cast toggles it off
- Selecting a vote that opposes an existing vote removes the opposing vote first
- The live tally updates in the card immediately, showing each option with a count and the names of voters

Votes are for mod discussion only — they do not automatically take action on Reddit.

### Mod Actions (Mod Reports)

The Actions dropdown on each report card supports:

- **Approve** — Approves the item on Reddit immediately. Posts a thread reply confirming who approved it. Marks the card as done.
- **Remove** — Opens a modal to select a removal reason (from your subreddit's configured reasons), add notes, and choose delivery method (public reply, private message, or silent). Posts a thread reply with details. Marks the card as done.
- **Warn User** — Opens a modal to compose a warning message. Sends the warning as a modmail to the user. Posts a thread reply. Marks the card as done.
- **Ban User** — Opens a modal to enter ban reason, duration (blank = permanent), and an optional mod note. Bans the user on Reddit. Posts a thread reply. Marks the card as done.

When an action is taken, the card is updated in place: the dropdowns are removed, a status header is added (e.g. `✅ APPROVED — username`), and a `:completed: DONE :completed:` marker is shown at the bottom.

### Re-opening Items

After an item is marked as done, a **Re-open** dropdown appears on the card. Clicking it re-fetches the item from Reddit and restores the full interactive card (vote dropdown, vote tally, actions dropdown) so the item can be reconsidered.

### Modmail Actions

Each modmail card supports:

- **Reply** — Opens a modal to compose a reply. The reply is sent from the mod team (author hidden) and posted as a thread reply in Slack.
- **Mute** — Mutes the conversation for 72 hours. Posts a thread reply confirming the mute.
- **Warn User** — Same as warn from a report card.
- **Ban User** — Same as ban from a report card.

Modmail action confirmations are posted as thread replies to the original modmail card.

### Queue Status Summaries

Every 5 minutes the bot posts a one-line summary of items still pending in the modqueue channel:

```
🕐 3 item(s) still pending: #1 | #3 | #7
```

- Each number links directly to its Slack message
- Items are listed in queue order (#1 first)
- If the queue is clear, the bot posts `:white_check_mark: Mod queue is clear.`
- Duplicate summaries are suppressed — if the queue state hasn't changed since the last post, nothing is posted

### Text Commands

| Command | Description |
|---------|-------------|
| `hello` | Bot says hi |
| `help` | Shows command reference |

### Authorization

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
POLL_INTERVAL = 60

[Channels]
MODQUEUE_CHANNEL = C0123456789
MODMAIL_CHANNEL  = C0987654321

[Mods]
U0123456789 = reddit_username
UABCDEFGHIJ = another_mod
```

Channel values should be Slack channel IDs (not names). Leave blank to disable auto-posting for that category.

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

---

## Data Storage

Items are tracked in monthly JSON log files at `logs/YYYYMM-modlog.json`. A new file is created each calendar month; items do not roll over between months.

Log structure per channel:

```json
{
  "CHANNEL_ID": {
    "ITEM_ID": {
      "queue_num": 1,
      "report_link": "https://reddit.com/...",
      "item_type": "submission",
      "slack_ts": "1234567890.123456",
      "slack_permalink": "https://workspace.slack.com/archives/...",
      "votes": {
        "USLACKID": ["approve"]
      }
    },
    "modmail_conv": {
      "CONV_ID": {
        "messages": { "MESSAGE_ID": "..." }
      }
    }
  }
}
```
