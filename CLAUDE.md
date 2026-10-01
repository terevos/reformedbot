# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

ReformedBot is a Slack bot that surfaces Reddit moderation activity (modqueue reports and modmail) directly in Slack. Mods triage from Slack with interactive Block Kit controls — a vote dropdown, a Done button, and a Re-open dropdown — while the bot keeps Slack's state in step with what actually happens on Reddit.

It serves any number of subreddits at once. Each is a **feed**: one subreddit plus the two Slack channels its activity is pushed to (see [Feeds](#feeds)).

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
- **`praw.ini`** — Reddit OAuth credentials, one profile per Reddit account a feed names in `REDDIT_ACCOUNT` (default `reformedbot`; see [PRAW docs](https://praw.readthedocs.io/en/stable/getting_started/configuration/prawini.html))
- **`slack.ini`** — Slack tokens and channel/mod config (copy from `slack.ini.example`)

## Running the Bot

```
python reformed_listener.py
```

The bot connects via Slack Socket Mode (no public HTTP server needed). It starts a background polling thread that polls Reddit every `POLL_INTERVAL` seconds (configured in `slack.ini`; default 30).

The bot is built to start and stay up even when Slack or Reddit is unavailable — it never exits on an upstream failure. A channel that cannot be resolved at startup is retried on every poll, so a bot launched during a Slack outage starts working on its own once Slack returns, with no restart. An upstream 5xx shortens the next poll to `_SERVER_ERROR_RETRY_DELAY` (15s) rather than waiting out a full interval. Each feed is polled inside its own guards (`_poll_feed`), so one broken subreddit never silences the others.

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

**"Has blocks" no longer means "is a card."** The status message carries blocks of its own now that it has a button, so a test asking what a poll posted uses `FakeSlackClient.cards()`, which tells them apart by the `_STATUS_SIGNATURE` footer. Filtering `slack.posted` on `p["blocks"]` counts the status message as a card and fails.

Anything that touches a feed needs the `feed` fixture from `conftest.py`: it installs a single resolved `Feed` on the listener (`L.feeds`) wired to the fakes, standing in for what `_startup()` builds. `actions_feed` is the same feed switched to `CONTROLS = actions`. `tests/test_feeds.py` covers config parsing and the two-subreddit case; `tests/test_controls.py` covers which controls a card gets and the handlers behind them; `tests/test_archive.py` covers the per-subreddit layout, the numbering counters, and rollover — it fast-forwards the counter rather than posting 999 items; `tests/test_store.py` covers the store itself: entry round-tripping (including fields with no column), row-level writes, the JSON imports, the export schedule, and two concurrency tests that would have caught the vote-clobbering bug the JSON logs had.

## Architecture

### Three-file design

**`log_store.py`** — `LogStore`, the SQLite persistence layer: one database per subreddit, row-level reads and writes, and the snapshot API the rest of the code reads through (see [The store](#the-store))

**`reformed_listener.py`** — Slack Bolt app (Socket Mode). Handles:
- Block Kit actions: `mark_done`, `cast_vote_*`, `reopen_item`, `modmail_action` (archive/unarchive), `my_unvoted` (the status message's personal vote list)
- The `Feed` class and `_load_feeds()`, which turn `slack.ini` into the list of subreddits being served
- Background daemon thread polling every feed, auto-posting to configured channels, reconciling done-state, and keeping the queue/modmail status messages current
- A dormant modal subsystem (see below) registered but currently unreachable, and `handle_modqueue_action`, which exists only to decline a click from a card carrying the withdrawn Take action… dropdown

**`reddit_actions.py`** — `RedditActions` class. All Reddit API calls go here:
- `get_modqueue()` / `get_conversations()` — fetch and deduplicate items; both support `as_blocks=True` to return Slack Block Kit payloads instead of plain text
- `get_current_modqueue_ids()` / `get_item_resolution()` — what is still queued, and who approved or removed an item that left the queue
- `sync_archived_conversations()` — detects modmail archived/unarchived on Reddit, and by whom. **`mod_actions` is not in the modmail listing payload**, so reading it makes PRAW fetch that whole conversation — and `getattr(conv, 'mod_actions', None)` is no guard, because the fetch raises its own errors, not `AttributeError`. Doing that for every conversation in both listings cost ~250 requests per feed per poll and ran the account into a 429. `_last_action_author()` is now called only for the conversations whose state actually changed (normally none), and returns `''` rather than raising if the fetch fails — the attribution is a nicety, the state change is not
- `record_vote()` / `get_votes()` — per-item vote tracking, stored as rows in the `votes` table
- `is_done()` / `set_item_done_at()` / `set_conv_done_at()` / `migrate_done_state()` — Slack done-state (see below)
- `_build_modqueue_blocks()` / `_build_modmail_blocks()` / `build_item_blocks_open()` / `build_item_blocks_done()` — Block Kit payload builders
- `archive_conversation()` / `unarchive_conversation()` — the only Reddit write the bot still offers from a card
- `approve_item()`, `remove_item()`, `approve_and_ignore_reports()`, `warn_user()`, `ban_user()`, `unban_user()`, `reply_modmail()`, the modmail mute action, and `_build_take_action_element()` — dormant, kept for the modal revival
- `roll_log()` / `read_counters()` / `adopt_legacy_logs()` / `maybe_export()` — log rollover, the persisted card-numbering counters, the one-time import out of the older JSON logs, and the weekly export (see [The store](#the-store) and [Exports](#exports))
- `refresh_mod_list()` / `is_mod()` — the subreddit's moderator list, read from Reddit rather than configured (it differs per subreddit) and cached for `_MOD_LIST_TTL` (6h). It is what tells a mod's modmail reply from a user's, so a failed or empty load **keeps the previous list** and retries after `_MOD_LIST_RETRY_DELAY` (5 min) — an empty list would make every mod reply look like a user reply and re-open resolved threads.

One instance is one subreddit — including its logs, which live in `logs/<subreddit>/`. Construction takes optional `reddit=`, `log_dir=`, and `mod_list=` arguments so tests can inject a fake PRAW client, redirect the logs (`log_dir` is the *root*; the subreddit directory is created beneath it), and skip the moderator fetch. `_startup()` builds one PRAW session per distinct `Feed.reddit_account` (a `praw.ini` profile), so feeds on the same account share a session and its rate-limit budget while a feed on its own account gets its own. A profile missing from `praw.ini` is a `SystemExit` naming it — it is a config mistake, not an outage, since building a session reads only the file.

### Feeds

A `Feed` is one subreddit plus the Slack channels its activity goes to, and it owns everything that used to be module-level: the resolved channel IDs, the `RedditActions` for that subreddit, its two `StatusMessage`s (`queue_status`, `modmail_status`), and `last_activity_at`. `feeds` is the module-level list; adding a subreddit is adding an entry, never a second poll loop.

Configuration is one `[Subreddit:<name>]` section per feed. The single-subreddit layout that came before — a bare `[Channels]` section, subreddit from `[Default] SUBREDDIT` — is still read when no `[Subreddit:...]` section exists, so an un-migrated `slack.ini` keeps working.

**Each feed owns its store.** State was shared once — one `logs/modqueue.json` keyed by channel ID, written by every feed — and is now one database per subreddit (`logs/<subreddit>/modlog.db`), so one busy subreddit's traffic never decides when another one's log rolls over. Channel ID stays a key *inside* the tables, which is what keeps numbering per channel for a feed whose two channels differ. Everything that touches state is therefore per feed: `migrate_done_state()` and `adopt_legacy_logs()` both run once per feed in `_startup()`, not once on `feeds[0]`.

Interactive handlers find their feed by channel: `_interaction_allowed()` returns the owning `Feed` (or `None` when rejected — callers must test for `None`, not truthiness) and every helper that touches Reddit takes that feed. `_feed_for_action()` handles the fail-open case where no feed claims a channel: with one feed configured there is no ambiguity, so it is used; with several, guessing could take a Reddit action against the wrong subreddit, so the interaction is declined with "still starting up".

### Deduplication

`RedditActions` tracks what has already been posted to each Slack channel in **one SQLite database per subreddit**, `logs/<subreddit>/modlog.db`, owned by `log_store.LogStore`. The shape callers see is still the old JSON logs' nested dicts:

- modqueue — `{ channel_id: { item_id: { queue_num, report_link, item_type, author, slack_ts, slack_permalink, slack_blocks, done_at, reopened_at, done_action, done_checks, done_by, done_note_ts, votes: {...} } } }`
- modmail — `{ channel_id: { 'modmail_conv': { conv_id: { conv_num, subject, author, slack_ts, slack_permalink, done_at, reopened_at, messages: {...} } } } }`

Note the extra `modmail_conv` nesting level on the modmail side; it is an accident of history and the reason most accessors exist in two versions.

**A modqueue entry is logged before it is posted.** `get_modqueue` numbers and stores new items, and the listener then posts them and records `slack_ts`. So an entry with **no `slack_ts` was never posted**, not "already posted": `get_modqueue` rebuilds its card, keeping its number, on every poll until a post succeeds. The listener posts each card inside its own `try`, so one rejected card does not drop the rest of the batch. This is how #334 went missing — a 2,915-character comment overflowed Slack's 3000-character section cap (`SECTION_LIMIT`), the post was rejected, and the logged entry was then treated as posted forever. `_build_modqueue_blocks` now trims the item body (never the links or reports) to fit. Modmail deduplicates per **message**, not per conversation, so new replies in a known thread still surface.

### The store

`get_modqueue_file()` / `write_modqueue_file()` and their modmail and counter twins are a **compatibility surface**, not the interface to write through. They hand back and take the whole log as nested dicts, which is what the summary, the exports, the archive files and the tests want. Every write on the poll or click path goes to one row instead:

| Reading | Writing |
|---|---|
| `store.item()` / `store.conv()` — one entry | `store.edit_item()` / `store.edit_conv()` — context manager yielding one entry, writing that row back |
| `store.channel_items()` / `store.channel_convs()` — one channel | `store.add_items()` / `store.add_conv_messages()` — insert only what is new |
| `store.modqueue_snapshot()` / `store.modmail_snapshot()` — everything | `store.update_votes()` — one mod's votes on one item |
| `store.votes()` / `store.counters()` | `store.set_counter()`, `store.import_items()` / `import_convs()` |

Three rules hold this together:

- **Never write through a snapshot.** `replace_modqueue()` / `replace_modmail()` delete everything and re-insert; they exist for the JSON import and for tests that seed a state. Using one on a live path would reintroduce exactly the whole-file clobbering the store was built to end.
- **`edit_item()` yields `None` for an item that is not there** unless `create=True` is passed, which is how the mutators keep their old "only touch what we have posted" behaviour. It does not carry votes, and does not write them.
- **The schema is columns, not blobs.** Queryable fields (`queue_num`, `done_at`, `done_action`, `slack_ts` …) are real columns; anything else an entry carries — an old field name, something a future version writes — round-trips through the `extra` JSON column. A NULL column reads as an *absent* key, never `None`, because the done-state encoding is "absent means open".

**Votes and modmail message IDs have their own tables.** They are the two places where two writers touch one entry at once, and rows let them: `update_votes()` runs the toggle inside one `BEGIN IMMEDIATE` transaction over that mod's rows, so two mods clicking simultaneously do not even touch the same record. This is what retired the read-merge-write convention the JSON logs needed — the bug it guarded against (a poll's whole-file write erasing votes cast while it ran) cannot happen against a row.

Connections are per thread (Bolt handlers and the poll thread both write), the database runs in WAL mode with `busy_timeout`, and every read-modify-write is one immediate transaction.

### Log files, numbering, and archiving

Every subreddit owns a directory under `logs/`:

```
logs/reformed/modlog.db              the store — items, votes, convs, messages, counters, meta
logs/reformed/archive/               modqueue-<channel>-cycle001-<stamp>.json, written at a rollover
logs/reformed/export/                modqueue-<YYYYMMDD>.json, written weekly
```

`RedditActions.log_slug()` builds the directory name, replacing anything unsafe in a path — subreddit names come from `slack.ini` and are not trusted with a path.

**Numbering is a persisted counter, not `max(log) + 1`.** `_NumberCycle` hands out `queue_num` / `conv_num` for one channel: it reads the `counters` table, hands out numbers, and writes the counter back once per poll (never per item). The old rule — one above the highest number in the log — survives only as the fallback for a channel with no counter yet, which is what makes an upgrade seamless. It cannot stay the rule, because after a rollover the live log still holds carried-over entries numbered near the cap and `max(...) + 1` would hand out 999 again on the next item.

**Reaching the cap rolls the log over.** Reports run `#1`–`#999` (`QUEUE_NUM_MAX`), modmail `#A`–`#ZZ` (`CONV_NUM_MAX` = 702, the last two-letter label). Asking for a number past the cap calls `roll_log()`, which writes every one of that channel's entries to an archive JSON file and then deletes the rows `_survives_archive()` does not keep:

- **open entries** — a card a mod is still working on must not vanish out from under them
- **entries closed within `_ARCHIVE_KEEP_DONE` (7 days)** — the reconcile pass still re-asks Reddit what happened to a recently-done item (late resolution) and can still re-open one
- **anything that is not a dict** — an unrecognised shape is kept rather than dropped

Then the counter resets to 1 and `cycle` increments. Carried-over entries **keep their old numbers**, so `_NumberCycle` skips any number a live entry still holds: two cards visible in the channel never share a label. If every number up to the cap were somehow still held even after a rollover, numbering continues past the cap rather than blocking the poll — the label is cosmetic, the item is not.

Three things follow that are easy to get wrong:

- **The archive file is written before any row is deleted.** A failed write fails the rollover and leaves the store whole, rather than deleting a cycle that was never saved.
- **The rollover happens mid-poll, inside `numbers.next()`.** It deletes rows, so `get_modqueue` refreshes its in-memory `posted_to_slack` when `numbers.rolled` is set rather than carrying a view that still holds the archived entries.
- **Archiving is a *cap* trigger only.** There is no timer and no size threshold; a quiet subreddit's store simply stays small for a long time. The *export* is the thing on a timer.

### Exports

The database is the state; the exports are the readable copy of it. `maybe_export()` runs from `_poll_feed()` and writes both logs as JSON — the same nested shape they had as files — into `logs/<subreddit>/export/` every `EXPORT_INTERVAL_DAYS` (7), keeping `EXPORT_KEEP` (52, a year of weekly snapshots) of each kind and pruning the rest by name, which sorts by date because the stamp is `YYYYMMDD`.

- **The stamp lives in the database** (`meta.last_export_at`), so the schedule survives a restart rather than starting over each time the bot comes up. A store with no stamp exports on its first poll — deliberately: that is the baseline copy.
- **A failed export is not a failed poll.** `maybe_export()` logs and returns `[]`, and the stamp is only written after a successful export, so the next poll tries again.
- **An export is a snapshot of the *live* store**, so it does not contain cycles that have already been archived. Archives plus exports are the full history; exports alone are not.
- Each export is also where the WAL is checkpointed back into the `.db` file, so the database beside them is a complete copy rather than half a state.

**`adopt_legacy_logs()` handles the upgrade from the JSON era.** Two layouts came before the store and both are read, newest first: the per-subreddit `logs/<subreddit>/modqueue.json`, then the shared `logs/modqueue.json` keyed by channel and written by every feed at once. On the first pass over a resolved channel — in `_startup()`, and again in `_poll_feed()` for a channel that only resolved later — the feed imports that channel's slice and the store records the channel as known. **That record is the guard**: without it a second pass would resurrect entries a rollover has since archived, which is why a channel is marked even when there was nothing to import. `counters.json` comes across too, or a bot that had already rolled over would renumber from a carried-over entry near the cap. The JSON files are left where they are — another feed's channels may still be in them, and they are the fallback if the import ever has to be redone.

### Done-state

Modqueue items and modmail conversations share one encoding: **`done_at`** holds the unix timestamp the entry was marked done in Slack, and is absent while it is still open. `RedditActions.is_done(entry)` is the single predicate. Older logs used `slack_done_at` for items and `status: 'open'|'done'` for conversations; `migrate_done_state()` converts both at startup and is idempotent.

Because open is encoded as an *absent* `done_at`, reopening is a removal and leaves no trace by itself. **`reopened_at`** is that trace, and `RedditActions.clear_done(entry)` is the one way to go done → open — it stamps it, skipping entries that were not done. It is a record only, never a state predicate, and it deliberately survives the entry being marked done again. The four paths that reopen (the two `set_*_done_at` setters, a user reply in `get_conversations`, and an unarchive in `sync_archived_conversations`) all go through it.

The poll loop reconciles this against Reddit each pass (`_reconcile_modqueue_state`):
- **Auto-done** — an item that left the Reddit modqueue is marked done in Slack, and the header names who did it and how (`get_item_resolution` reads `approved_by` / `banned_by`).
- **Auto-reopen** — an item marked done in Slack but still in the Reddit modqueue after a grace period of `2 × POLL_INTERVAL` is reopened.
- **Ban hold** — auto-done is skipped while any mod holds a vote in `HOLD_OPEN_VOTES` (`ban`). The modqueue answers one question, "is this post staying?", and a ban vote asks another one that leaving the queue does not settle. `held_open_by_vote()` is the predicate; `_mark_item_as_ban_hold()` retitles the still-open card with what Reddit did plus `BAN_HOLD_STATUS` and posts one thread note, and the card keeps every control it had so a mod can close it with Done. The reconcile pass re-reads that item on every poll, so the notice is fired once and `ban_hold_at` records that — a flag tracking a notice, never a state predicate, cleared by `clear_done()` so a reopened card can announce again. Withdraw the ban vote and the ordinary auto-done takes over on the next pass.
- **Late resolution** — a done card still showing the gavel is re-asked what Reddit did to it, and re-stamped once there is an answer (`_upgrade_done_status`, see [Vote tracking](#vote-tracking) for the emoji rules and the two bounds on the re-asking).

### Status messages

Each feed keeps **one live status message per channel** — "N item(s) still pending" / "N open modmail thread(s)" — instead of posting the same summary again and again. It is modelled by `StatusMessage` (`ts`, `body`, `refreshed_at`, `adopted`), one for each of a feed's two channels. Every status message ends with an `Updated …` line built by `_updated_line()`, which wraps the unix time in Slack's `<!date^…>` token so each mod reads it in their own timezone. The pending count itself links to that subreddit's Reddit modqueue (`Feed.modqueue_url`), hung off the existing line rather than added as a second one; the all-clear carries no link, since there is nothing to go and do.

`_publish_status()` decides between two paths, and the rule behind both is that **the status message must be the last message in its channel**:

- **Repost** (delete the old message, post a fresh one) when the summary content changed, or when the scheduled digest forces it, or when the message is no longer at the bottom (`_is_last_message` — an edit made far up the channel is invisible).

**The digest only forces a repost when there is something pending.** `force=True` is dropped inside `_post_queue_summary` / `_post_modmail_summary` when the queue is clear or no modmail thread is open, and the call falls back to the ordinary in-place refresh. The digest exists to nudge mods about outstanding work; an all-clear says nothing new, and reposting it at every scheduled hour is just noise. So a feed with a clear queue but open modmail is reposted in one channel and left alone in the other.
- **Edit in place** otherwise, at most once per `_STATUS_REFRESH_INTERVAL` (10 min), which is the timestamp refresh. `_SUMMARY_INTERVAL` (5 min) only controls how often the poll loop *calls* the summary; the function itself decides whether anything happens.

**The modqueue status carries a second line once the mods have agreed.** `:ballot_box_with_ballot: Items with 3+ votes:` lists each item that has reached `CONSENSUS_THRESHOLD` (3) votes on one of `CONSENSUS_KEYS` (`approve`, `remove`), with the count and the vote button's own emoji — `vote_emoji()` reads it out of `VOTE_OPTIONS` rather than a second table. `CONSENSUS_ALIASES` folds `spam` into `remove`: the two already cancel `approve` together, and three mods saying the post goes is three mods saying the post goes. A `ban` or `discuss` pile-up is the question, not the answer, and does not appear. An item over the threshold both ways is listed twice, larger count first — that disagreement is the thing to see.

Both this line and the button below it read `RedditActions.open_items()`, the *log's* open entries, not `get_current_modqueue_ids()`. That is deliberately wider than the pending list above them: an item held open past the modqueue by a ban vote still wants votes and still wants acting on.

**That width is why the all-clear is conditional.** "Mod queue is clear" is a statement about Reddit's queue, and printing it above a line listing what is still open contradicts itself — which is exactly what it did the first time this shipped. So an empty Reddit queue with open cards left reads `N item(s) still open here (nothing left in the Reddit mod queue): #129` instead, naming them; only an empty queue with nothing open at all gets the plain all-clear. The digest's `force` follows the same line: it is dropped on a genuine all-clear, because there is nothing to nudge about, and survives whenever a card is still open, because that is work nobody has closed.

**The personal list is a button, because a channel message cannot be per-viewer.** Slack renders one message the same way for everyone, so "what have *I* not voted on?" has no answer in the message itself. `_status_blocks()` hangs an `_UNVOTED_ACTION` button off the status message and `handle_my_unvoted` answers the click with `chat_postEphemeral` — Slack names who clicked, and only they see the reply. The button is offered only when the feed votes and something is open; the handler re-checks the setting anyway, since a message posted before the config changed still carries it. It reads nothing but the local store, so unlike `handle_cast_vote` it needs no background thread, and it must never touch `last_activity_at` — an ephemeral is invisible to everyone else, so it is not channel activity.

Two things the button changed about the message itself:

- **The status message now has blocks, but `text=` stays authoritative.** It is what `_adopt_status()` reads a message back out of the channel by, what `StatusMessage.shows` compares, and what a notification previews, so `_publish_status` passes the full body as `text=` alongside the blocks rather than treating it as a fallback.
- **A section block caps at 3000 characters and plain text did not**, so the section is trimmed to `_SECTION_TEXT_MAX`. Over the cap Slack rejects the whole message; the `text=` copy stays complete.

"Changed" is decided by comparing the **rendered body text** (`StatusMessage.shows`), not by a key derived from item IDs. That is what lets a message read back out of the channel be compared with a freshly built one — which is the whole basis of restart recovery below. `body is None` means "nothing known" and never matches, including against the empty-queue summary; conflating the two suppressed the all-clear notice after a restart.

### Restarts adopt the status message

The status message's `ts` lives only in memory, so a restart used to strand it and post a second one — a channel collecting one dead "N item(s) still pending" per restart. `_adopt_status()` closes that: on the first summary after boot it reads back `_STATUS_SCAN_LIMIT` (100) messages of channel history and takes over the newest message that has a `bot_id` and carries the `_STATUS_SIGNATURE` footer, recovering both its `ts` and its body. An unchanged queue then matches, so the restart is a silent in-place refresh; a changed one deletes that message and reposts, leaving nothing behind either way.

Two properties worth keeping:

- It runs **once per channel per process** (`adopted`), lazily on the first summary rather than in `_startup()` — it needs the poll loop's Slack client, and a feed that never publishes never pays for it.
- A failed history lookup returns `False` and the summary **defers to the next poll** instead of publishing. Posting while blind to the channel is exactly what creates the duplicate this is meant to prevent, and the next poll is 30s away.

Item and conversation messages need none of this: their `slack_ts` is in the store, so they already survive a restart.

An in-place edit deliberately does **not** update `feed.last_activity_at` — the digest's quiet-period check is about whether mods have seen something new, and a silent edit is not that. A `chat_update` that fails (message deleted by hand) falls through to posting, so the feed repairs itself without a restart.

The digest's quiet check is **per feed**: a busy subreddit does not suppress the digest of a silent one. The scheduled slot (`_last_digest_slot`) is global, since it is about the time of day.

### Authorization

Interactive handlers go through `_interaction_allowed()`, which checks two gates and reports failure ephemerally so only the clicker sees it:

1. **Channel** — the message must live in a configured feed channel (`_is_allowed_channel`). This exists because a message left behind in a de-configured channel keeps working buttons. It fails open when no channel is configured at all, or while a configured channel is still unresolved (the allow-list is not known to be complete).
2. **Moderator** — the Slack user must be listed in the `[Mods]` section of `slack.ini` (global, loaded into `mod_slack_ids` at import) or in the `[Mods:<subreddit>]` section of the feed being acted on (`feed.mods`), which authorizes them for that subreddit alone. `_mod_display_name()` credits the action using the feed's own name for that mod when it has one.

### Card controls

Which controls a card carries is per feed, set by `CONTROLS` in `slack.ini` and parsed by `RedditActions.parse_controls()`:

- **`vote`** — the Cast vote… dropdown and its tally on modqueue cards. The mods decide together and somebody acts on Reddit separately.
- **`actions`** — Archive / Unarchive on modmail cards (`modmail_action`), performed **on Reddit for real**, for the whole mod team rather than in Slack alone.

**Modqueue cards carry no Reddit action.** `actions` used to put a Take action… dropdown (`modqueue_action`) on them — Approve / Remove / Warn / Ban, plus an *Ignore reports & Approve* option behind its own `ignore_reports` control — and that was withdrawn. What survives is dormant and deliberately intact: `RedditActions._build_take_action_element()` (the dropdown, now with an explicit `include_ignore_reports=` argument in place of the removed control), `handle_modqueue_action_dormant()` (every branch), the Remove/Warn/Ban modals and their `@app.view` handlers, and `approve_and_ignore_reports()`. Reviving it is emitting the dropdown from `_build_item_actions_block()` again, gated on a control, and moving the `@app.action("modqueue_action")` decorator back onto the dormant handler. `tests/test_controls.py` drives all of it directly, so a revival starts from working code.

Both controls, comma-separated, gives both; an absent key means `vote`, which is what every card carried before this existed; a present-but-empty value means neither, which is legitimate. `[Default] CONTROLS` sets the house rule and a feed's own key overrides it (`_controls_for`).

The resolved set lives on the feed's `RedditActions` (`controls`, with `voting_enabled` / `actions_enabled`), because that is what builds the blocks — no threading a flag down the call chain. `Feed.controls` holds the same set for the handlers.

Two rules hold this together:

- **Done is not one of the controls.** Every card gets a Done button, so an item can always be closed out in Slack whatever else it offers.
- **Every handler re-checks the setting.** A card posted before the config changed still carries the old buttons, and Slack will happily deliver a click from it. `handle_cast_vote` and `handle_modmail_action` decline with an ephemeral notice rather than trusting the payload; `handle_modqueue_action` is the same rule taken to its end — the dropdown is gone entirely, so the registration stays only to decline a click from a card that still has one.

The vote tally section follows `_wants_tally()`: shown when the feed votes, and also when votes already exist, so switching a feed to `actions` does not erase the tally from cards that have one.

### Vote tracking

Each modqueue item supports multi-vote tracking via `cast_vote_*` action IDs. Each mod can hold multiple vote keys simultaneously. Opposing vote pairs (`approve` vs `remove`/`spam`, `ban` vs `dont_ban`) automatically cancel each other out — **per mod**, which is why one mod's `dont_ban` does not clear another's `ban`, and why the ban hold above survives a split vote. Votes are rows in the `votes` table, one per (item, mod, key), and reach the card as `{mod: [keys]}`; `vote_keys()` normalises what comes back out, since imported logs hold two older shapes (a bare string, and keys with a stale `|<timestamp>` suffix) which the import flattens.

Status emoji reuse the vote-button vocabulary in `VOTE_OPTIONS` — ✅ for approve, ❌ for remove — so the same action looks the same everywhere. `RedditActions.ACTION_EMOJI` holds that mapping and `action_emoji()` is how everything reads it; a done state with no known action falls back to the custom `:completed:` gavel (`DONE_EMOJI_DEFAULT`), which is a shortcode, so any `plain_text` header rendering it needs `"emoji": True`.

**All three places a done state shows carry the same emoji**: the card header, the in-card DONE marker (`done_marker_text()`, threaded down as `build_item_blocks_done(done_emoji=…)` / `_mark_item_as_actioned(done_emoji=…)`), and the thread note. Auto-done already knows the action from `get_item_resolution()`; a hand-clicked **Done** now asks for it too — one extra Reddit fetch per click, which is a click-path cost, not a poll-path one — and falls back to the gavel when Reddit names no resolution.

**The gavel is provisional, not a resting state.** A mod who clicks Done and *then* removes the post gets a card Reddit had no answer for at the moment it was closed, and it must not sit on a gavel forever. `_upgrade_done_status()` is the fourth job of the reconcile pass: for a done item that left the modqueue with no `done_action` recorded, it asks again, and on an answer re-stamps all three places — header, marker, and the thread note, rewritten in place via the `done_note_ts` and `done_by` recorded by the Done click. A card closed by hand keeps crediting the mod who clicked; an auto-done one takes the auto-done wording, since nobody clicked anything. `clear_done()` drops all four fields, so a reopened card starts the hunt over rather than inheriting a stale answer.

Two bounds keep that from becoming a request storm, which is the failure mode this account has hit before:

- **`_RESOLUTION_RECHECK_LIMIT` (40)** — attempts per item, counted by `record_resolution_check()` whether or not it got an answer. An item that left the queue with nobody to credit — deleted by its author, caught by the spam filter — never gets one, and without the count would be re-fetched every poll forever.
- **`_RESOLUTION_RECHECK_BATCH` (5)** — items per poll, **newest `done_at` first**. The modqueue log holds every item ever posted to the channel, so candidates are collected during the reconcile loop and rationed after it; the priority is what stops a card a mod just closed from queuing behind a backlog of old unresolvable ones.

Because the marker's emoji varies, it is matched **by shape** — `RedditActions.is_done_marker()`, `<emoji> DONE <emoji>` — not by comparing against `DONE_MARKER_TEXT`. Both `_is_status_marker()` (which strips it on rebuild) and `_find_detail_section()` (which must not mistake it for the detail section) go through that predicate; a literal comparison would leave `❌ DONE ❌` markers stacking up. `DONE_MARKER_TEXT` remains the gavel form, which is what the modmail paths still post.

### Card headers

Every modqueue item and modmail conversation card starts with a `header` block — Slack's only larger text. It is `plain_text`: no links, no bold, no mentions, and 150 characters hard (exceed it and Slack rejects the whole message, so `RedditActions._fit_header` trims). `emoji: True` is required or the `:completed:` shortcode renders literally.

**One header per card, always**, holding the title and — once resolved — the status:

- `#12 · post by u/someone` while open (`item_title`) — a `submission` reads as **post** on a card (`ITEM_TYPE_LABELS`); the stored `item_type` stays Reddit's own name, since it drives the PRAW branching and the action payloads
- `#12 · post by u/someone · ✅ DONE — terevos2` once done (`header_text(title, status)`) — ✅ / ❌ / the gavel, following what Reddit says happened
- `#12 · post by u/someone · 🔄 REOPENED — terevos2` once done and reopened — `REOPENED_STATUS` plus who did it (the auto-reopen says `still in modqueue`), passed as the `status=` argument of `build_item_blocks_open`. An item that was never done passes nothing, so a fresh card is unchanged. Marking it done again replaces the status, since the header is rebuilt rather than appended to.
- `#A · u/someone · Ban appeal` for modmail (`conv_title`); threaded replies get no header, the card above them has one. A reopened conversation gets the same `REOPENED_STATUS` from `_mark_conv_as_reopened`

**A conversation's card owns its controls; its replies carry none.** The buttons act on the conversation, so putting them on a threaded reply puts them somewhere other than the item they act on. `_build_modmail_blocks` gives them to the first post only.

**`_mark_conv_as_reopened()` must be passed the `conv_id`.** It used to recover one by scanning block IDs, which works only while the card still has an actions block — and `_mark_conv_as_actioned` strips those, so exactly the cards that get reopened are the ones it could not identify. The result was a card retitled bare `🔄 REOPENED`, with no controls restored, and the reply's own buttons the only ones left. Both poll-loop callers know the ID; the block scan (`_conv_id_from_blocks`) survives as a fallback.

That single-header rule is what keeps the rebuild invariant below workable: strip every header, add exactly one back. When title and status together overflow, `header_text` trims the **title** — the status is what a mod scrolling past needs to read. Because a header cannot hold a link, the permalink lives in the section beneath it.

A rebuild has only the log to work from, so titles come from there: `author` is recorded in the modqueue log entry for this reason (entries written before that survive without it — `item_title` simply drops the `by u/…`), and `conv_title_for()` reads conv_num/author/subject out of the modmail log.

### Slack messages are the state store

A posted message is re-fetched with `conversations_history`, filtered, and written back with `chat_update` on every state change. Anything a rebuild does not explicitly remove survives forever, so a rebuild **must** call `_strip_status_blocks()` to drop the previous header and DONE/REOPENED marker before adding its own. Skipping this stacks duplicates — a conversation that went done → replied → archived once ended up with three headers and three markers. Since the header now carries the card's title too, every caller of `_strip_status_blocks()` has to rebuild one, or the card loses its big first line.

### Modal flow for destructive actions

Remove/Warn/Ban/Reply modals collect input and pass context (item ID, channel, message timestamp, Reddit link) through `private_metadata` as JSON, so the submission handler can update the original message in place.

The whole subsystem is **dormant**: the modqueue dropdown that opened these modals was withdrawn (see [Card controls](#card-controls)), so nothing reaches them. The `@app.view` handlers stay registered anyway — an unregistered view is a worse failure than an unreachable one, and keeping them registered is what makes the revival a one-line change. `handle_modqueue_action_dormant` opens the modals and those handlers finish the job. Approve and Remove record `done_at` themselves rather than leaving it to the reconcile pass, which would otherwise see the item gone from the Reddit modqueue and overwrite the `✅ APPROVED` / `❌ REMOVED` header with a generic DONE one.

**A Silent Remove needs no removal reason.** Slack cannot make one input conditional on another, so `reason_select_block` is `optional` in the view and the rule is enforced in `handle_removal_submitted`: a delivery other than `silent` with neither a preset reason nor written text is bounced back with `ack(response_action="errors", …)` on that block, because it would send an empty message and be a silent removal in all but name. That is why the handler reads the view state **before** acknowledging — an `ack()` at the top cannot be taken back. A missing delivery choice falls back to `silent`, matching `remove_item`'s own default: an unintended message to the user is the worse of the two failures.

Modmail **Reply / Mute / Warn / Ban** are dormant in the same way, and were before this. `handle_modmail_action` — the one Reddit-action handler still live — covers `archive` and `unarchive` only, and `_build_modmail_actions_block()` is the unemitted dropdown holding the rest — it is the shape those branches take when they come back. `handle_modmail_action` reads its payload through `_selected_value()`, which accepts either a button (Archive) or a one-option dropdown (the Unarchive control left behind by `_mark_conv_as_archived`); that Unarchive control was live but dead before this, since every branch behind it was commented out.

## slack.ini Sections

| Section | Key | Purpose |
|---------|-----|---------|
| `[Default]` | `API_TOKEN` | Bot OAuth token (`xoxb-...`) |
| `[Default]` | `APP_TOKEN` | Socket Mode app-level token (`xapp-...`) |
| `[Default]` | `SIGNING_SECRET` | Slack signing secret |
| `[Default]` | `POLL_INTERVAL` | Seconds between Reddit polls (default 30) |
| `[Subreddit:<name>]` | `MODQUEUE_CHANNEL` | Channel name or ID for that subreddit's mod reports |
| `[Subreddit:<name>]` | `MODMAIL_CHANNEL` | Channel name or ID for that subreddit's modmail |
| `[Subreddit:<name>]` | `CONTROLS` | Which card controls that subreddit gets: `vote`, `actions`, comma-separated. Default `vote` (see [Card controls](#card-controls)) |
| `[Default]` | `CONTROLS` | `CONTROLS` for every feed that does not set its own |
| `[Subreddit:<name>]` | `REDDIT_ACCOUNT` | `praw.ini` profile whose Reddit account polls and acts on that subreddit. Default `reformedbot` |
| `[Default]` | `REDDIT_ACCOUNT` | `REDDIT_ACCOUNT` for every feed that does not set its own |
| `[Mods]` | `SLACK_USER_ID = reddit_name` | Moderators authorized in every feed |
| `[Mods:<name>]` | `SLACK_USER_ID = reddit_name` | Moderators authorized for that subreddit only |
| `[Channels]` | `MODQUEUE_CHANNEL` / `MODMAIL_CHANNEL` | Legacy single-subreddit form; read only when no `[Subreddit:...]` section exists, paired with `[Default] SUBREDDIT` (default `reformed`) |

Channel values may be a name (`mod_actions`) or an ID (`C0ARRHHT8M7`). Names are resolved to IDs by `_resolve_channel()`; a private channel is only resolvable once the bot has been invited to it. A name that fails to resolve is retried on every poll rather than disabling the feed until restart — using an ID skips the lookup entirely.

The deduplication logs are keyed by channel ID, so pointing a feed at a different channel re-posts currently-open items there and leaves the old messages behind with live buttons (which is what the channel guard above is for).
