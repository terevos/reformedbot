# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

ReformedBot is a Slack bot that surfaces Reddit r/reformed moderation activity (modqueue reports and modmail) directly in Slack with interactive Block Kit buttons for taking mod actions (approve, remove, warn, ban) without leaving Slack.

## Setup

Install dependencies with pipenv (Python 3.13):
```
pipenv install
```

Or with pip:
```
pip install -r requirements.txt
```

> **Avoid `pipenv shell`** — it spawns a subshell that breaks terminal input (readline, history, etc.). Instead, activate the virtualenv in your current shell:
> ```
> source $(pipenv --venv)/bin/activate
> ```
> Or run one-off commands without activating:
> ```
> pipenv run python reformed_listener.py
> ```

Two config files are required before running:
- **`praw.ini`** — Reddit OAuth credentials for the `reformedbot` PRAW profile (see [PRAW docs](https://praw.readthedocs.io/en/stable/getting_started/configuration/prawini.html))
- **`slack.ini`** — Slack tokens and channel/mod config (copy from `slack.ini.example`)

## Running the Bot

```
python reformed_listener.py
```

The bot connects via Slack Socket Mode (no public HTTP server needed). It starts a background polling thread that polls Reddit every `POLL_INTERVAL` seconds (default 60).

`reformed_test.py` is a legacy CLI script using the deprecated RTM Slack library — it is not the current bot and is not actively maintained.

## Architecture

### Two-file design

**`reformed_listener.py`** — Slack Bolt app (Socket Mode). Handles:
- Text commands: `report`/`queue`, `mail`/`conv`, `hello`, `help`
- Block Kit button actions: `approve_item`, `remove_item`, `warn_user`, `ban_user`, `cast_vote_*`
- Modal submissions: `removal_reason_submitted`, `warn_submitted`, `ban_submitted`
- Background daemon thread polling Reddit and auto-posting to configured channels

**`reddit_actions.py`** — `RedditActions` class. All Reddit API calls go here:
- `get_modqueue()` / `get_conversations()` — fetch and deduplicate items; both support `as_blocks=True` to return Slack Block Kit payloads instead of plain text
- `approve_item()`, `remove_item()`, `warn_user()`, `ban_user()`, `unban_user()` — moderation actions via PRAW
- `record_vote()` / `get_votes()` — per-item vote tracking stored in the JSON log
- `_build_modqueue_blocks()` / `_build_modmail_blocks()` — Block Kit payload builders
- `get_modqueue_file()` / `write_modqueue_file()` — reads/writes monthly deduplication log at `logs/YYYYMM-modlog.json`

### Deduplication

`RedditActions` tracks which Reddit items have already been posted to each Slack channel in monthly JSON files (`logs/YYYYMM-modlog.json`). The key structure is `{ channel_id: { item_id: { queue_num, report_link, item_type, votes: {...} } } }`. A new file is created each calendar month; items do not roll over.

### Authorization

Only Slack users listed in the `[Mods]` section of `slack.ini` (mapping Slack user ID → Reddit username) can click action buttons. Unauthorized clicks get an ephemeral error visible only to them. `mod_slack_ids` is populated at startup in `reformed_listener.py`.

### Vote tracking

Each modqueue item supports multi-vote tracking via `cast_vote_*` action IDs. Each mod can hold multiple vote keys simultaneously. Opposing vote pairs (e.g. `approve` vs `remove`) automatically cancel each other out. Votes are stored inside the existing JSON log under `item_id.votes`.

### Modal flow for destructive actions

Remove/Warn/Ban open Slack modals to collect input. Context (item ID, channel, message timestamp, Reddit link) is passed through `private_metadata` as JSON so the modal submission handler can update the original message in place after the action completes.

## slack.ini Sections

| Section | Key | Purpose |
|---------|-----|---------|
| `[Default]` | `API_TOKEN` | Bot OAuth token (`xoxb-...`) |
| `[Default]` | `APP_TOKEN` | Socket Mode app-level token (`xapp-...`) |
| `[Default]` | `SIGNING_SECRET` | Slack signing secret |
| `[Default]` | `POLL_INTERVAL` | Seconds between Reddit polls (default 60) |
| `[Channels]` | `MODQUEUE_CHANNEL` | Channel ID for auto-pushed mod reports |
| `[Channels]` | `MODMAIL_CHANNEL` | Channel ID for auto-pushed modmail |
| `[Mods]` | `SLACK_USER_ID = reddit_name` | Authorized moderators |
