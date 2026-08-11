"""Done-state encoding: ``done_at`` on both items and conversations, plus the
migration off the legacy ``slack_done_at`` / ``status`` fields."""
from __future__ import annotations

import json
import time
from pathlib import Path

from conftest import CHANNEL, MAIL_CHANNEL
from reddit_actions import RedditActions


# ---------------------------------------------------------------------------
# is_done
# ---------------------------------------------------------------------------

def test_is_done_reads_done_at() -> None:
    assert RedditActions.is_done({"done_at": 1234.5}) is True
    assert RedditActions.is_done({"done_at": None}) is False
    assert RedditActions.is_done({}) is False


def test_is_done_handles_missing_entry() -> None:
    """A conversation absent from the log is open, not done."""
    assert RedditActions.is_done(None) is False


def test_is_done_ignores_legacy_status_after_migration() -> None:
    """Post-migration nothing writes `status`, so it must not be consulted."""
    assert RedditActions.is_done({"status": "done"}) is False


# ---------------------------------------------------------------------------
# setters
# ---------------------------------------------------------------------------

def test_set_item_done_at_round_trip(actions: RedditActions) -> None:
    actions.write_modqueue_file({CHANNEL: {"abc": {"queue_num": 1}}})

    actions.set_item_done_at(CHANNEL, "abc", 999.0)
    assert actions.get_item_info(CHANNEL, "abc")["done_at"] == 999.0
    assert RedditActions.is_done(actions.get_item_info(CHANNEL, "abc"))

    actions.set_item_done_at(CHANNEL, "abc", None)
    assert "done_at" not in actions.get_item_info(CHANNEL, "abc")
    assert not RedditActions.is_done(actions.get_item_info(CHANNEL, "abc"))


def test_set_item_done_at_ignores_unknown_item(actions: RedditActions) -> None:
    """Writing done-state for an item that was never logged must not create one."""
    actions.write_modqueue_file({CHANNEL: {}})
    actions.set_item_done_at(CHANNEL, "nope", 1.0)
    assert actions.get_modqueue_file()[CHANNEL] == {}


def test_set_conv_done_at_round_trip(actions: RedditActions) -> None:
    actions.set_conv_done_at(MAIL_CHANNEL, "conv1", 500.0)
    convs = actions.get_modmail_file()[MAIL_CHANNEL]["modmail_conv"]
    assert convs["conv1"]["done_at"] == 500.0

    actions.set_conv_done_at(MAIL_CHANNEL, "conv1", None)
    assert "done_at" not in actions.get_modmail_file()[MAIL_CHANNEL]["modmail_conv"]["conv1"]


def test_set_conv_done_at_preserves_other_fields(actions: RedditActions) -> None:
    """Marking done must not disturb conv_num — it is rendered into Slack."""
    actions.write_modmail_file({MAIL_CHANNEL: {"modmail_conv": {
        "c1": {"conv_num": 7, "subject": "hi", "author": "someone", "messages": {"m1": True}},
    }}})
    actions.set_conv_done_at(MAIL_CHANNEL, "c1", 42.0)
    entry = actions.get_modmail_file()[MAIL_CHANNEL]["modmail_conv"]["c1"]
    assert entry["conv_num"] == 7 and entry["subject"] == "hi" and entry["messages"] == {"m1": True}


def test_open_and_done_conversations_are_distinguished(actions: RedditActions) -> None:
    actions.write_modmail_file({MAIL_CHANNEL: {"modmail_conv": {
        "open1": {"conv_num": 1, "subject": "s", "author": "a"},
        "done1": {"conv_num": 2, "subject": "s", "author": "a", "done_at": 100.0},
    }}})
    open_ids = [c["conv_id"] for c in actions.get_open_conversations(MAIL_CHANNEL)]
    assert open_ids == ["open1"]


# ---------------------------------------------------------------------------
# migration
# ---------------------------------------------------------------------------

def test_migrate_renames_item_field_preserving_value(actions: RedditActions) -> None:
    actions.write_modqueue_file({CHANNEL: {
        "done": {"queue_num": 1, "slack_done_at": 123.0},
        "open": {"queue_num": 2, "slack_done_at": None},
    }})

    counts = actions.migrate_done_state()

    entries = actions.get_modqueue_file()[CHANNEL]
    assert counts["items"] == 2
    assert entries["done"]["done_at"] == 123.0
    assert "slack_done_at" not in entries["done"]
    assert "done_at" not in entries["open"], "a null slack_done_at means open"


def test_migrate_converts_conversation_status(actions: RedditActions) -> None:
    actions.write_modmail_file({MAIL_CHANNEL: {"modmail_conv": {
        "d": {"status": "done", "conv_num": 1},
        "o": {"status": "open", "conv_num": 2},
    }}})

    counts = actions.migrate_done_state()

    convs = actions.get_modmail_file()[MAIL_CHANNEL]["modmail_conv"]
    assert counts["convs"] == 2
    assert convs["d"]["done_at"] is not None and "status" not in convs["d"]
    assert "done_at" not in convs["o"] and "status" not in convs["o"]
    assert convs["d"]["conv_num"] == 1 and convs["o"]["conv_num"] == 2


def test_migrate_is_idempotent(actions: RedditActions) -> None:
    actions.write_modqueue_file({CHANNEL: {"a": {"slack_done_at": 1.0}}})
    actions.write_modmail_file({MAIL_CHANNEL: {"modmail_conv": {"c": {"status": "done"}}}})

    actions.migrate_done_state()
    after_first = (actions.get_modqueue_file(), actions.get_modmail_file())

    assert actions.migrate_done_state() == {"items": 0, "convs": 0}
    assert (actions.get_modqueue_file(), actions.get_modmail_file()) == after_first


def test_migrate_preserves_votes_and_blocks(actions: RedditActions) -> None:
    """The migration rewrites entries in place; nothing else may be lost."""
    entry = {
        "queue_num": 3, "item_type": "comment", "report_link": "http://x",
        "slack_ts": "1.1", "slack_permalink": "http://s", "slack_done_at": 7.0,
        "votes": {"U1": ["approve"]}, "slack_blocks": [{"type": "divider"}],
    }
    actions.write_modqueue_file({CHANNEL: {"a": dict(entry)}})

    actions.migrate_done_state()

    after = actions.get_modqueue_file()[CHANNEL]["a"]
    assert after["votes"] == {"U1": ["approve"]}
    assert after["slack_blocks"] == [{"type": "divider"}]
    for key in ("queue_num", "item_type", "report_link", "slack_ts", "slack_permalink"):
        assert after[key] == entry[key]


def test_migrate_stamps_legacy_done_conversations_with_a_time(actions: RedditActions) -> None:
    """Legacy `status: done` carries no timestamp, so one is assigned."""
    before = time.time()
    actions.write_modmail_file({MAIL_CHANNEL: {"modmail_conv": {"c": {"status": "done"}}}})
    actions.migrate_done_state()
    stamped = actions.get_modmail_file()[MAIL_CHANNEL]["modmail_conv"]["c"]["done_at"]
    assert before <= stamped <= time.time()


def test_migrate_tolerates_empty_and_malformed_logs(actions: RedditActions) -> None:
    actions.write_modqueue_file({})
    actions.write_modmail_file({MAIL_CHANNEL: {"modmail_conv": {}}})
    assert actions.migrate_done_state() == {"items": 0, "convs": 0}


def test_logs_are_written_to_the_configured_directory(actions: RedditActions, tmp_path: Path) -> None:
    """The suite must never touch the repo's real logs/ directory."""
    actions.write_modqueue_file({CHANNEL: {"a": {}}})
    written = tmp_path / "logs" / "modqueue.json"
    assert written.exists()
    assert json.loads(written.read_text())[CHANNEL] == {"a": {}}
