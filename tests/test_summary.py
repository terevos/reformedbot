"""Summary posting, digest gating, and modmail conversation numbering."""
from __future__ import annotations

from typing import Any, Tuple

import time
from datetime import datetime

import pytest

import reformed_listener as L
from conftest import CHANNEL, MAIL_CHANNEL, FakeItem
from reddit_actions import RedditActions


@pytest.fixture
def queue_feed(monkeypatch: pytest.MonkeyPatch, actions: RedditActions, slack: Any) -> Tuple[RedditActions, Any]:
    """Point the listener's modqueue feed at the fakes and return them."""
    monkeypatch.setattr(L, "reddit", actions)
    monkeypatch.setattr(L, "modqueue_channel", CHANNEL)
    monkeypatch.setattr(L, "_last_summary_key", None)
    monkeypatch.setattr(L, "_last_activity_at", 0.0)
    monkeypatch.setattr(L, "_queue_status_ts", None)
    monkeypatch.setattr(L, "_queue_status_refreshed_at", 0.0)
    return actions, slack


@pytest.fixture
def mail_feed(monkeypatch: pytest.MonkeyPatch, actions: RedditActions, slack: Any) -> Tuple[RedditActions, Any]:
    """Point the listener's modmail feed at the fakes and return them."""
    monkeypatch.setattr(L, "reddit", actions)
    monkeypatch.setattr(L, "modmail_channel", MAIL_CHANNEL)
    monkeypatch.setattr(L, "_last_modmail_summary_key", None)
    monkeypatch.setattr(L, "_modmail_status_ts", None)
    monkeypatch.setattr(L, "_modmail_status_refreshed_at", 0.0)
    return actions, slack


# ---------------------------------------------------------------------------
# queue summary
# ---------------------------------------------------------------------------

def test_empty_queue_posts_the_all_clear(queue_feed: Any) -> None:
    _, slack = queue_feed
    L._post_queue_summary(slack)
    assert "Mod queue is clear" in slack.texts()[0]


def test_pending_items_are_listed_with_their_numbers(queue_feed: Any, fake_reddit: Any) -> None:
    actions, slack = queue_feed
    fake_reddit.add_queue_item(FakeItem("a1", created_utc=1.0))
    actions.write_modqueue_file({CHANNEL: {"a1": {"queue_num": 7, "slack_permalink": "http://s/1"}}})

    L._post_queue_summary(slack)

    text = slack.texts()[0]
    assert "1 item(s) still pending" in text and "#7" in text and "http://s/1" in text


def test_identical_state_is_not_reposted(queue_feed: Any, fake_reddit: Any) -> None:
    _, slack = queue_feed
    fake_reddit.add_queue_item(FakeItem("a1", created_utc=1.0))
    L._post_queue_summary(slack)
    L._post_queue_summary(slack)
    assert len(slack.posted) == 1, "a steady queue must not spam the channel"


def test_a_changed_queue_is_reposted(queue_feed: Any, fake_reddit: Any) -> None:
    _, slack = queue_feed
    fake_reddit.add_queue_item(FakeItem("a1", created_utc=1.0))
    L._post_queue_summary(slack)
    fake_reddit.add_queue_item(FakeItem("a2", created_utc=2.0))
    L._post_queue_summary(slack)
    assert len(slack.posted) == 2


def test_reordering_alone_does_not_repost(queue_feed: Any, fake_reddit: Any) -> None:
    """The dedup key is order-independent."""
    _, slack = queue_feed
    a, b = FakeItem("a1", created_utc=1.0), FakeItem("a2", created_utc=2.0)
    fake_reddit.add_queue_item(a)
    fake_reddit.add_queue_item(b)
    L._post_queue_summary(slack)

    fake_reddit._sub.modqueue_items = [b, a]
    L._post_queue_summary(slack)

    assert len(slack.posted) == 1


def test_force_reposts_unchanged_state(queue_feed: Any, fake_reddit: Any) -> None:
    _, slack = queue_feed
    fake_reddit.add_queue_item(FakeItem("a1", created_utc=1.0))
    L._post_queue_summary(slack)
    L._post_queue_summary(slack, force=True)
    assert len(slack.posted) == 2


def test_status_message_carries_an_updated_timestamp(queue_feed: Any) -> None:
    _, slack = queue_feed
    L._post_queue_summary(slack)
    assert "Updated <!date^" in slack.texts()[0], "status must say when it was last refreshed"


def due_for_refresh(monkeypatch: pytest.MonkeyPatch, attr: str = "_queue_status_refreshed_at") -> None:
    """Backdate the last refresh so the next summary call is due to update."""
    monkeypatch.setattr(L, attr, time.time() - L._STATUS_REFRESH_INTERVAL - 1)


def test_unchanged_state_refreshes_the_time_in_place(monkeypatch: pytest.MonkeyPatch, queue_feed: Any, fake_reddit: Any) -> None:
    _, slack = queue_feed
    fake_reddit.add_queue_item(FakeItem("a1", created_utc=1.0))
    L._post_queue_summary(slack)
    due_for_refresh(monkeypatch)

    L._post_queue_summary(slack)

    assert len(slack.posted) == 1, "a refresh must edit, not repost"
    assert slack.last_update()["ts"] == slack.posted[0]["ts"]
    assert "Updated <!date^" in slack.last_update()["text"]


def test_refresh_reposts_when_the_status_is_no_longer_at_the_bottom(monkeypatch: pytest.MonkeyPatch, queue_feed: Any, fake_reddit: Any) -> None:
    """An edit far up the channel is invisible, so the status moves down instead."""
    _, slack = queue_feed
    fake_reddit.add_queue_item(FakeItem("a1", created_utc=1.0))
    L._post_queue_summary(slack)
    status_ts = slack.posted[0]["ts"]
    slack.chat_postMessage(channel=CHANNEL, text="a newer report")
    due_for_refresh(monkeypatch)

    L._post_queue_summary(slack)

    assert not slack.updated, "the stranded message must not be edited in place"
    assert [d["ts"] for d in slack.deleted] == [status_ts]
    assert slack.posted[-1]["text"].startswith(":clock2:")


def test_a_changed_queue_replaces_the_old_status_message(queue_feed: Any, fake_reddit: Any) -> None:
    _, slack = queue_feed
    fake_reddit.add_queue_item(FakeItem("a1", created_utc=1.0))
    L._post_queue_summary(slack)
    first_ts = slack.posted[0]["ts"]

    fake_reddit.add_queue_item(FakeItem("a2", created_utc=2.0))
    L._post_queue_summary(slack)

    assert [d["ts"] for d in slack.deleted] == [first_ts], "the superseded status must be cleaned up"
    assert "2 item(s) still pending" in slack.texts()[-1]


def test_a_silent_refresh_does_not_count_as_channel_activity(monkeypatch: pytest.MonkeyPatch, queue_feed: Any, fake_reddit: Any) -> None:
    """The digest's quiet check is about what mods have seen, and an edit is silent."""
    _, slack = queue_feed
    fake_reddit.add_queue_item(FakeItem("a1", created_utc=1.0))
    L._post_queue_summary(slack)
    due_for_refresh(monkeypatch)
    monkeypatch.setattr(L, "_last_activity_at", 0.0)

    L._post_queue_summary(slack)

    assert L._last_activity_at == 0.0


def test_summary_records_activity_for_the_digest(queue_feed: Any) -> None:
    _, slack = queue_feed
    before = L._last_activity_at
    L._post_queue_summary(slack)
    assert L._last_activity_at > before


# ---------------------------------------------------------------------------
# modmail summary
# ---------------------------------------------------------------------------

def test_no_open_conversations_posts_the_all_clear(mail_feed: Any) -> None:
    _, slack = mail_feed
    L._post_modmail_summary(slack)
    assert "resolved" in slack.texts()[0]


def test_open_conversations_are_listed_with_letter_labels(mail_feed: Any) -> None:
    actions, slack = mail_feed
    actions.write_modmail_file({MAIL_CHANNEL: {"modmail_conv": {
        "c1": {"conv_num": 1, "subject": "Ban appeal", "author": "someone", "slack_ts": "1.0"},
    }}})

    L._post_modmail_summary(slack)

    text = slack.texts()[0]
    assert "1 open modmail thread(s)" in text
    assert "#A." in text and "Ban appeal" in text


def test_done_conversations_are_excluded(mail_feed: Any) -> None:
    actions, slack = mail_feed
    actions.write_modmail_file({MAIL_CHANNEL: {"modmail_conv": {
        "c1": {"conv_num": 1, "subject": "open one", "author": "a", "slack_ts": "1.0"},
        "c2": {"conv_num": 2, "subject": "closed one", "author": "b", "slack_ts": "2.0", "done_at": 5.0},
    }}})

    L._post_modmail_summary(slack)

    assert "open one" in slack.texts()[0] and "closed one" not in slack.texts()[0]


def test_unchanged_modmail_state_is_not_reposted(mail_feed: Any) -> None:
    _, slack = mail_feed
    L._post_modmail_summary(slack)
    L._post_modmail_summary(slack)
    assert len(slack.posted) == 1


def test_unchanged_modmail_state_refreshes_the_time_in_place(monkeypatch: pytest.MonkeyPatch, mail_feed: Any) -> None:
    _, slack = mail_feed
    L._post_modmail_summary(slack)
    due_for_refresh(monkeypatch, "_modmail_status_refreshed_at")

    L._post_modmail_summary(slack)

    assert len(slack.posted) == 1
    assert slack.last_update()["ts"] == slack.posted[0]["ts"]


# ---------------------------------------------------------------------------
# conversation labels / numbering
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("num,label", [(1, "A"), (2, "B"), (26, "Z"), (27, "AA"), (28, "AB"), (52, "AZ"), (53, "BA")])
def test_conv_labels_are_spreadsheet_style(num: Any, label: Any) -> None:
    assert RedditActions.conv_label(num) == label


@pytest.mark.parametrize("bad", [None, 0, -1, "x"])
def test_invalid_conv_numbers_render_as_question_mark(bad: Any) -> None:
    assert RedditActions.conv_label(bad) == "?"


def test_backfill_numbers_follow_slack_post_order(actions: RedditActions) -> None:
    """Log order is alphabetical by ID, so numbering must use slack_ts."""
    actions.write_modmail_file({MAIL_CHANNEL: {"modmail_conv": {
        "zzz": {"slack_ts": "100.0", "subject": "first posted", "author": "a"},
        "aaa": {"slack_ts": "200.0", "subject": "second posted", "author": "b"},
    }}})

    convs = {c["conv_id"]: c["conv_num"] for c in actions.get_open_conversations(MAIL_CHANNEL)}

    assert convs["zzz"] < convs["aaa"]


def test_backfill_does_not_reuse_an_assigned_number(actions: RedditActions) -> None:
    actions.write_modmail_file({MAIL_CHANNEL: {"modmail_conv": {
        "has": {"slack_ts": "100.0", "conv_num": 1, "subject": "s", "author": "a"},
        "needs": {"slack_ts": "200.0", "subject": "s", "author": "b"},
    }}})

    convs = {c["conv_id"]: c["conv_num"] for c in actions.get_open_conversations(MAIL_CHANNEL)}

    assert convs["has"] == 1 and convs["needs"] != 1


def test_backfill_persists_so_numbers_are_stable(actions: RedditActions) -> None:
    actions.write_modmail_file({MAIL_CHANNEL: {"modmail_conv": {
        "c1": {"slack_ts": "100.0", "subject": "s", "author": "a"},
    }}})
    first = actions.get_open_conversations(MAIL_CHANNEL)[0]["conv_num"]
    second = actions.get_open_conversations(MAIL_CHANNEL)[0]["conv_num"]
    assert first == second
    assert actions.get_modmail_file()[MAIL_CHANNEL]["modmail_conv"]["c1"]["conv_num"] == first


# ---------------------------------------------------------------------------
# digest gating
# ---------------------------------------------------------------------------

def at_hour(monkeypatch: pytest.MonkeyPatch, hour: int, minute: int = 0) -> None:
    """Freeze the digest clock at a local time."""
    real = datetime

    class FrozenDatetime(real):
        """A datetime whose ``now()`` is pinned to the time under test."""

        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            """Return the pinned local time, honouring the requested tzinfo."""
            return real(2026, 7, 29, hour, minute, tzinfo=tz)

    monkeypatch.setattr(L, "datetime", FrozenDatetime)


def test_digest_fires_at_a_scheduled_hour_after_a_quiet_period(monkeypatch: pytest.MonkeyPatch, queue_feed: Any, mail_feed: Any) -> None:
    _, slack = queue_feed
    at_hour(monkeypatch, L._DIGEST_HOURS[0])
    monkeypatch.setattr(L, "_last_digest_slot", None)
    monkeypatch.setattr(L, "_last_activity_at", 0.0)   # long quiet

    assert L._maybe_post_digest(slack) is True


def test_digest_does_not_fire_twice_in_the_same_slot(monkeypatch: pytest.MonkeyPatch, queue_feed: Any, mail_feed: Any) -> None:
    _, slack = queue_feed
    at_hour(monkeypatch, L._DIGEST_HOURS[0])
    monkeypatch.setattr(L, "_last_digest_slot", None)
    monkeypatch.setattr(L, "_last_activity_at", 0.0)

    assert L._maybe_post_digest(slack) is True
    assert L._maybe_post_digest(slack) is False


def test_digest_is_skipped_when_the_channel_was_recently_active(monkeypatch: pytest.MonkeyPatch, queue_feed: Any, mail_feed: Any) -> None:
    _, slack = queue_feed
    at_hour(monkeypatch, L._DIGEST_HOURS[0])
    monkeypatch.setattr(L, "_last_digest_slot", None)
    monkeypatch.setattr(L, "_last_activity_at", time.time())   # just posted

    assert L._maybe_post_digest(slack) is False


def test_no_digest_outside_the_scheduled_hours(monkeypatch: pytest.MonkeyPatch, queue_feed: Any) -> None:
    _, slack = queue_feed
    at_hour(monkeypatch, 3)
    monkeypatch.setattr(L, "_last_digest_slot", None)
    assert L._maybe_post_digest(slack) is False


def test_no_digest_once_the_window_has_passed(monkeypatch: pytest.MonkeyPatch, queue_feed: Any) -> None:
    """A restart late in the day must not fire the morning digest."""
    _, slack = queue_feed
    at_hour(monkeypatch, L._DIGEST_HOURS[0], minute=L._DIGEST_WINDOW // 60 + 5)
    monkeypatch.setattr(L, "_last_digest_slot", None)
    assert L._maybe_post_digest(slack) is False
