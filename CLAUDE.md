# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

ReformedBot is a Slack bot that surfaces Reddit r/reformed moderation activity (modqueue reports and modmail) directly in Slack. Mods triage from Slack with interactive Block Kit controls — a vote dropdown, a Done button, and a Re-open dropdown — while the bot keeps Slack's state in step with what actually happens on Reddit.

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

The bot connects via Slack Socket Mode (no public HTTP server needed). It starts a background polling thread that polls Reddit every `POLL_INTERVAL` seconds (configured in `slack.ini`; default 30).

The bot is built to start and stay up even when Slack or Reddit is unavailable — it never exits on an upstream failure. A channel that cannot be resolved at startup is retried on every poll, so a bot launched during a Slack outage starts working on its own once Slack returns, with no restart. An upstream 5xx shortens the next poll to `_SERVER_ERROR_RETRY_DELAY` (15s) rather than waiting out a full interval.

`reformed_test.py` is a legacy CLI script using the deprecated RTM Slack library — it is not the current bot and is not actively maintained.

## Testing

```
python -m pytest
```

The suite in `tests/` is hermetic: no network, no credentials, and it never touches the real `logs/`. `pytest.ini` scopes collection to `tests/` so the legacy `reformed_test.py` is not picked up by pytest's default `*_test.py` pattern.

Two constraints keep it working, and breaking either one breaks the whole suite at collection time:

- **`reformed_listener.py` must stay import-side-effect-free.** Everything that touches the network or reads credentials lives in `_startup()`, called from `__main__`. The Bolt `App` is constructed with `token_verification_enabled=False` and placeholder token/secret so the module imports without `slack.ini`.
- **PRAW fakes in `tests/conftest.py` are real `Submission`/`Comment` instances** with their attributes pre-filled and `_fetched=True`. `get_modqueue` branches on `isinstance`, so a duck-typed stand-in falls into the "Unknown" branch and proves nothing; without `_fetched=True`, PRAW treats an absent attribute as a cue to fetch and recurses.

Handlers that do their work off-thread (`handle_cast_vote`, `_check_queue_clear_and_post`) need `monkeypatch.setattr(L.threading, "Thread", ImmediateThread)` or assertions race the background work.

## Architecture

### Two-file design

**`reformed_listener.py`** — Slack Bolt app (Socket Mode). Handles:
- Block Kit actions: `mark_done`, `cast_vote_*`, `reopen_item`
- Background daemon thread polling Reddit, auto-posting to configured channels, reconciling done-state, and keeping the queue/modmail status messages current
- A dormant modal subsystem (see below) registered but currently unreachable

**`reddit_actions.py`** — `RedditActions` class. All Reddit API calls go here:
- `get_modqueue()` / `get_conversations()` — fetch and deduplicate items; both support `as_blocks=True` to return Slack Block Kit payloads instead of plain text
- `get_current_modqueue_ids()` / `get_item_resolution()` — what is still queued, and who approved or removed an item that left the queue
- `sync_archived_conversations()` — detects modmail archived/unarchived on Reddit, and by whom
- `record_vote()` / `get_votes()` — per-item vote tracking stored in the JSON log
- `is_done()` / `set_item_done_at()` / `set_conv_done_at()` / `migrate_done_state()` — Slack done-state (see below)
- `_build_modqueue_blocks()` / `_build_modmail_blocks()` / `build_item_blocks_open()` / `build_item_blocks_done()` — Block Kit payload builders
- `approve_item()`, `remove_item()`, `warn_user()`, `ban_user()`, `unban_user()`, `reply_modmail()`, and the modmail archive/mute actions — dormant, kept for the modal revival

Construction takes optional `reddit=` and `log_dir=` arguments so tests can inject a fake PRAW client and redirect the logs.

### Deduplication

`RedditActions` tracks what has already been posted to each Slack channel in two JSON logs. These are **not** rotated — there is one file each, growing over time:

- `logs/modqueue.json` — `{ channel_id: { item_id: { queue_num, report_link, item_type, slack_ts, slack_permalink, slack_blocks, done_at, votes: {...} } } }`
- `logs/modmail.json` — `{ channel_id: { 'modmail_conv': { conv_id: { conv_num, subject, author, slack_ts, slack_permalink, done_at, messages: {...} } } } }`

Note the extra `modmail_conv` nesting level on the modmail side; it is an accident of history and the reason most accessors exist in two versions. Modmail deduplicates per **message**, not per conversation, so new replies in a known thread still surface.

Both logs are written atomically (temp file + rename). Any bulk write must re-read the file and merge, never write back a snapshot taken earlier in the operation — a stale snapshot silently erases votes recorded while the poll was running.

### Done-state

Modqueue items and modmail conversations share one encoding: **`done_at`** holds the unix timestamp the entry was marked done in Slack, and is absent while it is still open. `RedditActions.is_done(entry)` is the single predicate. Older logs used `slack_done_at` for items and `status: 'open'|'done'` for conversations; `migrate_done_state()` converts both at startup and is idempotent.

The poll loop reconciles this against Reddit each pass (`_reconcile_modqueue_state`):
- **Auto-done** — an item that left the Reddit modqueue is marked done in Slack, and the header names who did it and how (`get_item_resolution` reads `approved_by` / `banned_by`).
- **Auto-reopen** — an item marked done in Slack but still in the Reddit modqueue after a grace period of `2 × POLL_INTERVAL` is reopened.

### Status messages

Each feed keeps **one live status message** — "N item(s) still pending" / "N open modmail thread(s)" — instead of posting the same summary again and again. Every status message ends with an `Updated …` line built by `_updated_line()`, which wraps the unix time in Slack's `<!date^…>` token so each mod reads it in their own timezone.

`_publish_status()` decides between two paths, and the rule behind both is that **the status message must be the last message in its channel**:

- **Repost** (delete the old message, post a fresh one) when the summary content changed, or when the scheduled digest forces it, or when the message is no longer at the bottom (`_is_last_message` — an edit made far up the channel is invisible).
- **Edit in place** otherwise, at most once per `_STATUS_REFRESH_INTERVAL` (10 min), which is the timestamp refresh. `_SUMMARY_INTERVAL` (5 min) only controls how often the poll loop *calls* the summary; the function itself decides whether anything happens.

An in-place edit deliberately does **not** update `_last_activity_at` — the digest's quiet-period check is about whether mods have seen something new, and a silent edit is not that. A `chat_update` that fails (message deleted by hand) falls through to posting, so the feed repairs itself without a restart.

### Authorization

Interactive handlers go through `_interaction_allowed()`, which checks two gates and reports failure ephemerally so only the clicker sees it:

1. **Channel** — the message must live in a configured feed channel (`_is_allowed_channel`). This exists because a message left behind in a de-configured channel keeps working buttons. It fails open when no channel is configured at all, or while a configured channel is still unresolved (the allow-list is not known to be complete).
2. **Moderator** — the Slack user must be listed in the `[Mods]` section of `slack.ini`, loaded into `mod_slack_ids` at import.

### Vote tracking

Each modqueue item supports multi-vote tracking via `cast_vote_*` action IDs. Each mod can hold multiple vote keys simultaneously. Opposing vote pairs (e.g. `approve` vs `remove`) automatically cancel each other out. Votes are stored inside the modqueue log under `item_id.votes`.

Status emoji reuse the vote-button vocabulary in `VOTE_OPTIONS` — ✅ for approve, ❌ for remove — so the same action looks the same everywhere. A done state with no known action uses the custom `:completed:` gavel, which is a shortcode, so any `plain_text` header rendering it needs `"emoji": True`.

### Slack messages are the state store

A posted message is re-fetched with `conversations_history`, filtered, and written back with `chat_update` on every state change. Anything a rebuild does not explicitly remove survives forever, so a rebuild **must** call `_strip_status_blocks()` to drop the previous status header and DONE/REOPENED marker before adding its own. Skipping this stacks duplicates — a conversation that went done → replied → archived once ended up with three headers and three markers.

### Modal flow for destructive actions (dormant)

Remove/Warn/Ban/Reply modals collect input and pass context (item ID, channel, message timestamp, Reddit link) through `private_metadata` as JSON, so the submission handler can update the original message in place. **This whole subsystem is currently unreachable** — `views_open` is never called from live code — but it is kept deliberately for a planned revival.

Reviving it needs more than uncommenting `handle_modqueue_action`: nothing emits its `action_id` any more, because the "Take action…" dropdown was removed from `_build_item_actions_block` with no commented remnant to restore. The same is true of `modmail_action`.

## slack.ini Sections

| Section | Key | Purpose |
|---------|-----|---------|
| `[Default]` | `API_TOKEN` | Bot OAuth token (`xoxb-...`) |
| `[Default]` | `APP_TOKEN` | Socket Mode app-level token (`xapp-...`) |
| `[Default]` | `SIGNING_SECRET` | Slack signing secret |
| `[Default]` | `POLL_INTERVAL` | Seconds between Reddit polls (default 30) |
| `[Channels]` | `MODQUEUE_CHANNEL` | Channel name or ID for auto-pushed mod reports |
| `[Channels]` | `MODMAIL_CHANNEL` | Channel name or ID for auto-pushed modmail |
| `[Mods]` | `SLACK_USER_ID = reddit_name` | Authorized moderators |

Channel values may be a name (`mod_actions`) or an ID (`C0ARRHHT8M7`). Names are resolved to IDs by `_resolve_channel()`; a private channel is only resolvable once the bot has been invited to it. A name that fails to resolve is retried on every poll rather than disabling the feed until restart — using an ID skips the lookup entirely.

The deduplication logs are keyed by channel ID, so pointing a feed at a different channel re-posts currently-open items there and leaves the old messages behind with live buttons (which is what the channel guard above is for).
