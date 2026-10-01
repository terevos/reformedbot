"""The SQLite store: entry round-tripping, row-level writes, concurrency,
imports from the old JSON logs, and the periodic export.

The store replaced JSON logs that were rewritten whole on every change. Two
properties matter most here and both are regressions waiting to happen: an entry
must come back exactly as it went in (including fields the schema has no column
for), and two writers must be able to touch one item at once without either of
them losing work.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest

from conftest import CHANNEL, MAIL_CHANNEL, FakeItem
from log_store import KIND_MAIL, KIND_QUEUE, LogStore
from reddit_actions import RedditActions


@pytest.fixture
def store(tmp_path: Path) -> LogStore:
    """A store in a temp directory."""
    return LogStore(str(tmp_path / "logs" / "reformed" / "modlog.db"))


# ---------------------------------------------------------------------------
# round-tripping
# ---------------------------------------------------------------------------

FULL_ENTRY: Dict[str, Any] = {
    "queue_num": 42,
    "report_link": "https://reddit.com/r/reformed/comments/abc",
    "item_type": "submission",
    "author": "someone",
    "slack_ts": "1234.5678",
    "slack_permalink": "https://slack.com/archives/C1/p1",
    "slack_blocks": [{"type": "header", "text": {"type": "plain_text", "text": "#42"}}],
    "done_at": 1755000000.0,
    "reopened_at": 1755000100.0,
    "done_action": "removed",
    "done_checks": 3,
    "done_by": "terevos2",
    "done_note_ts": "1234.9999",
    "ban_hold_at": 1755000200.0,
    "votes": {"U1": ["approve"], "U2": ["remove", "ban"]},
}


def test_a_full_entry_round_trips(store: LogStore) -> None:
    store.replace_modqueue({CHANNEL: {"abc": FULL_ENTRY}})
    assert store.item(CHANNEL, "abc") == FULL_ENTRY


def test_unknown_fields_survive(store: LogStore) -> None:
    """A field with no column of its own goes to ``extra``, not to the floor."""
    entry = {"queue_num": 1, "slack_done_at": 99.0, "something_new": {"a": [1, 2]}}
    store.replace_modqueue({CHANNEL: {"abc": entry}})
    assert store.item(CHANNEL, "abc") == entry


def test_absent_fields_stay_absent(store: LogStore) -> None:
    """Open is encoded as a *missing* done_at, so NULL must not read as None."""
    store.replace_modqueue({CHANNEL: {"abc": {"queue_num": 1}}})
    entry = store.item(CHANNEL, "abc")
    assert "done_at" not in entry and "votes" not in entry
    assert RedditActions.is_done(entry) is False


def test_an_empty_channel_survives(store: LogStore) -> None:
    """The channel key is the "already imported" marker — it must persist."""
    store.replace_modqueue({CHANNEL: {}})
    assert store.modqueue_snapshot() == {CHANNEL: {}}
    assert store.knows_channel(KIND_QUEUE, CHANNEL) is True


def test_conversations_round_trip(store: LogStore) -> None:
    conv = {"conv_num": 3, "subject": "Ban appeal", "author": "someone",
            "slack_ts": "1.2", "done_at": 17.0, "messages": {"m1": True, "m2": True}}
    store.replace_modmail({MAIL_CHANNEL: {"modmail_conv": {"c1": conv}}})
    assert store.modmail_snapshot() == {MAIL_CHANNEL: {"modmail_conv": {"c1": conv}}}


# ---------------------------------------------------------------------------
# row-level writes
# ---------------------------------------------------------------------------

def test_edit_item_touches_only_its_row(store: LogStore) -> None:
    store.replace_modqueue({CHANNEL: {
        "a": {"queue_num": 1, "votes": {"U1": ["approve"]}},
        "b": {"queue_num": 2, "votes": {"U2": ["remove"]}},
    }})
    with store.edit_item(CHANNEL, "a") as entry:
        entry["done_at"] = 5.0

    assert store.item(CHANNEL, "a")["done_at"] == 5.0
    assert store.item(CHANNEL, "a")["votes"] == {"U1": ["approve"]}, "votes are not the editor's business"
    assert store.item(CHANNEL, "b") == {"queue_num": 2, "votes": {"U2": ["remove"]}}


def test_edit_item_yields_none_for_an_unknown_item(store: LogStore) -> None:
    with store.edit_item(CHANNEL, "nope") as entry:
        assert entry is None
    assert store.item(CHANNEL, "nope") == {}


def test_edit_item_can_create(store: LogStore) -> None:
    with store.edit_item(CHANNEL, "new", create=True) as entry:
        entry["queue_num"] = 7
    assert store.item(CHANNEL, "new")["queue_num"] == 7


def test_add_items_does_not_overwrite_state(store: LogStore) -> None:
    """A poll re-seeing an item must not undo what a mod clicked since."""
    store.replace_modqueue({CHANNEL: {"a": {"queue_num": 1, "done_at": 9.0}}})
    store.add_items(CHANNEL, {"a": {"queue_num": 99}, "b": {"queue_num": 2}})
    assert store.item(CHANNEL, "a") == {"queue_num": 1, "done_at": 9.0}
    assert store.item(CHANNEL, "b")["queue_num"] == 2


def test_add_conv_messages_appends(store: LogStore) -> None:
    store.replace_modmail({MAIL_CHANNEL: {"modmail_conv": {"c1": {"messages": {"m1": True}}}}})
    store.add_conv_messages(MAIL_CHANNEL, "c1", ["m2"])
    assert store.conv(MAIL_CHANNEL, "c1")["messages"] == {"m1": True, "m2": True}


# ---------------------------------------------------------------------------
# votes and concurrency
# ---------------------------------------------------------------------------

def test_update_votes_is_scoped_to_one_mod(store: LogStore) -> None:
    store.replace_modqueue({CHANNEL: {"a": {"votes": {"U1": ["approve"], "U2": ["remove"]}}}})
    store.update_votes(CHANNEL, "a", "U1", lambda keys: keys + ["discuss"])
    assert store.votes(CHANNEL, "a") == {"U1": ["approve", "discuss"], "U2": ["remove"]}


def test_legacy_vote_shapes_are_normalised_on_import(store: LogStore) -> None:
    """Older logs held a bare string, and keys with a stale |timestamp suffix."""
    store.replace_modqueue({CHANNEL: {"a": {"votes": {"U1": "approve", "U2": ["warn|1775768648"]}}}})
    assert store.votes(CHANNEL, "a") == {"U1": ["approve"], "U2": ["warn"]}


def test_concurrent_votes_from_two_threads_all_survive(store: LogStore) -> None:
    """The failure the JSON log could not prevent: whole-file writes racing.

    Ten mods vote at once on the same item. Every vote must be there afterwards.
    """
    store.replace_modqueue({CHANNEL: {"a": {"queue_num": 1}}})
    users = [f"U{n}" for n in range(10)]
    barrier = threading.Barrier(len(users))
    errors: List[BaseException] = []

    def vote(user: str) -> None:
        try:
            barrier.wait(timeout=10)
            store.update_votes(CHANNEL, "a", user, lambda keys: keys + ["approve"])
        except BaseException as e:      # noqa: BLE001 - reported, not swallowed
            errors.append(e)

    threads = [threading.Thread(target=vote, args=(u,)) for u in users]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, errors
    assert store.votes(CHANNEL, "a") == {u: ["approve"] for u in users}


def test_a_vote_and_a_poll_insert_do_not_collide(store: LogStore) -> None:
    """A mod voting while the poll thread inserts new items."""
    store.replace_modqueue({CHANNEL: {"existing": {"queue_num": 1}}})
    done = threading.Event()

    def poll() -> None:
        for n in range(50):
            store.add_items(CHANNEL, {f"new{n}": {"queue_num": n + 2}})
        done.set()

    thread = threading.Thread(target=poll)
    thread.start()
    for n in range(50):
        store.update_votes(CHANNEL, "existing", "U1", lambda keys: ["approve"])
    thread.join(timeout=30)

    assert done.is_set()
    assert store.votes(CHANNEL, "existing") == {"U1": ["approve"]}
    assert len(store.channel_items(CHANNEL)) == 51


# ---------------------------------------------------------------------------
# importing the old JSON logs
# ---------------------------------------------------------------------------

def write_json(path: Path, data: Dict[str, Any]) -> None:
    """Write a JSON log at *path*, creating its directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=4))


def test_shared_json_log_is_imported(actions: RedditActions, tmp_path: Path) -> None:
    write_json(tmp_path / "logs" / "modqueue.json", {
        CHANNEL: {"a1": {"queue_num": 4, "votes": {"U1": ["approve"]}}},
        "C_OTHER": {"z9": {"queue_num": 1}},
    })
    write_json(tmp_path / "logs" / "modmail.json",
               {MAIL_CHANNEL: {"modmail_conv": {"c1": {"conv_num": 2, "messages": {"m1": True}}}}})

    assert actions.adopt_legacy_logs([CHANNEL, MAIL_CHANNEL]) == 2
    assert actions.get_item_info(CHANNEL, "a1")["queue_num"] == 4
    assert actions.get_votes(CHANNEL, "a1") == {"U1": ["approve"]}
    assert actions.get_conv_info(MAIL_CHANNEL, "c1")["conv_num"] == 2
    assert "C_OTHER" not in actions.get_modqueue_file(), "another feed's channel is not ours to take"


def test_per_subreddit_json_log_wins_over_the_shared_one(actions: RedditActions, tmp_path: Path) -> None:
    """The per-subreddit JSON layout came second, so it is the newer copy."""
    write_json(tmp_path / "logs" / "modqueue.json", {CHANNEL: {"a1": {"queue_num": 1}}})
    write_json(tmp_path / "logs" / "reformed" / "modqueue.json", {CHANNEL: {"a1": {"queue_num": 500}}})
    actions.adopt_legacy_logs([CHANNEL])
    assert actions.get_item_info(CHANNEL, "a1")["queue_num"] == 500


def test_counters_are_imported(actions: RedditActions, tmp_path: Path) -> None:
    """Without this a bot that had already rolled over would renumber from the log."""
    write_json(tmp_path / "logs" / "reformed" / "counters.json",
               {"modqueue": {CHANNEL: {"next": 12, "cycle": 3}}})
    write_json(tmp_path / "logs" / "reformed" / "modqueue.json", {CHANNEL: {}})
    actions.adopt_legacy_logs([CHANNEL])
    assert actions.read_counters()["modqueue"][CHANNEL] == {"next": 12, "cycle": 3}


def test_import_runs_once_per_channel(actions: RedditActions, tmp_path: Path) -> None:
    """A second pass must not resurrect what a rollover has since archived."""
    write_json(tmp_path / "logs" / "modqueue.json", {CHANNEL: {"a1": {"queue_num": 4}}})
    actions.adopt_legacy_logs([CHANNEL])
    actions.store.delete_items(CHANNEL, ["a1"])

    actions._legacy_checked.clear()          # as a restart would leave it
    assert actions.adopt_legacy_logs([CHANNEL]) == 0
    assert actions.get_modqueue_file()[CHANNEL] == {}


def test_import_of_real_shaped_entries_keeps_every_field(actions: RedditActions, tmp_path: Path) -> None:
    write_json(tmp_path / "logs" / "modqueue.json", {CHANNEL: {"a1": dict(FULL_ENTRY)}})
    actions.adopt_legacy_logs([CHANNEL])
    assert actions.get_item_info(CHANNEL, "a1") == FULL_ENTRY


# ---------------------------------------------------------------------------
# exports
# ---------------------------------------------------------------------------

def exports(actions: RedditActions) -> List[str]:
    """Export file names, sorted."""
    return sorted(p.name for p in Path(actions.export_dir).glob("*.json"))


def test_first_export_happens_immediately(actions: RedditActions, fake_reddit: Any) -> None:
    """A store with no stamp yet has never had a baseline copy taken."""
    fake_reddit.add_queue_item(FakeItem("a1", created_utc=1.0))
    actions.get_modqueue(CHANNEL, no_repost=True, as_blocks=True)

    written = actions.maybe_export(now=1_755_000_000.0)
    assert len(written) == 2
    data = json.loads(Path(written[0]).read_text())
    assert data[CHANNEL]["a1"]["queue_num"] == 1, "the export is the log's own shape"


def test_export_is_not_repeated_within_the_interval(actions: RedditActions) -> None:
    now = 1_755_000_000.0
    actions.maybe_export(now=now)
    assert actions.maybe_export(now=now + 6 * 86400) == []
    assert actions.maybe_export(now=now + 8 * 86400) != []


def test_export_schedule_survives_a_restart(actions: RedditActions, fake_reddit: Any, tmp_path: Path) -> None:
    """The stamp is in the database, not in memory."""
    now = 1_755_000_000.0
    actions.maybe_export(now=now)
    reborn = RedditActions("reformed", reddit=fake_reddit, log_dir=str(tmp_path / "logs"))
    assert reborn.maybe_export(now=now + 3600) == []


def test_exports_are_pruned_to_the_keep_limit(actions: RedditActions) -> None:
    actions.export_keep = 3
    start = 1_700_000_000.0
    for week in range(6):
        actions.maybe_export(now=start + week * 8 * 86400)
    names = exports(actions)
    assert len([n for n in names if n.startswith("modqueue-")]) == 3
    assert len([n for n in names if n.startswith("modmail-")]) == 3
    assert names == sorted(names), "the newest are the ones kept"


def test_a_failed_export_does_not_stop_the_poll(actions: RedditActions, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: Any, **kw: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(actions, "export_logs", boom)
    assert actions.maybe_export(now=1_755_000_000.0) == []
    assert actions.store.get_meta(actions._EXPORT_STAMP_KEY) == "", "a failed export is not a completed one"
