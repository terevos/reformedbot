"""Keeping Slack's done-state in step with the live Reddit modqueue, and the
status header that reports who resolved an item."""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple
from reddit_actions import RedditActions

import time

import pytest

import reformed_listener as L
from conftest import CHANNEL, FakeItem, FakeRedditor

POLL = 30
GRACE = 2 * POLL


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch, actions: RedditActions, slack: Any) -> Tuple[RedditActions, Any]:
    """Point the listener at the fakes and return them."""
    monkeypatch.setattr(L, "reddit", actions)
    monkeypatch.setattr(L, "modqueue_channel", CHANNEL)
    return actions, slack


def log_item(actions: RedditActions, item_id: str = "a1", ts: str = "900.0",
             done_at: Optional[float] = None, **extra: Any) -> Dict[str, Any]:
    """Record an item as posted to Slack, optionally already done."""
    entry = {"queue_num": 1, "item_type": "submission", "report_link": "http://r",
             "slack_ts": ts, "votes": {}, **extra}
    if done_at is not None:
        entry["done_at"] = done_at
    data = actions.get_modqueue_file()
    data.setdefault(CHANNEL, {})[item_id] = entry
    actions.write_modqueue_file(data)
    return entry


def header_of(slack: Any) -> str:
    """Return the header text of the most recent message update."""
    return slack.last_update()["blocks"][0]["text"]["text"]


# ---------------------------------------------------------------------------
# auto-done
# ---------------------------------------------------------------------------

def test_item_gone_from_reddit_is_marked_done(wired: Any, fake_reddit: Any) -> None:
    actions, slack = wired
    log_item(actions)
    slack.seed_message("900.0", [{"type": "section", "text": {"type": "mrkdwn", "text": "detail"}}])
    fake_reddit.items["a1"] = FakeItem("a1", approved=True, approved_by=FakeRedditor("friardon"))

    changed = L._reconcile_modqueue_state(slack, POLL)

    assert changed is True
    assert actions.get_item_info(CHANNEL, "a1")["done_at"] is not None
    assert slack.updated, "the Slack message was updated"


def test_done_header_names_the_approving_mod(wired: Any, fake_reddit: Any) -> None:
    actions, slack = wired
    log_item(actions)
    slack.seed_message("900.0", [{"type": "section", "text": {"type": "mrkdwn", "text": "d"}}])
    fake_reddit.items["a1"] = FakeItem("a1", approved=True, approved_by=FakeRedditor("friardon"))

    L._reconcile_modqueue_state(slack, POLL)

    assert header_of(slack) == "✅ DONE — friardon (approved on Reddit)"


def test_done_header_names_the_removing_mod_with_the_vote_button_emoji(wired: Any, fake_reddit: Any) -> None:
    actions, slack = wired
    log_item(actions)
    slack.seed_message("900.0", [{"type": "section", "text": {"type": "mrkdwn", "text": "d"}}])
    fake_reddit.items["a1"] = FakeItem("a1", removed=True, banned_by=FakeRedditor("terevos2"))

    L._reconcile_modqueue_state(slack, POLL)

    header = header_of(slack)
    assert header == "❌ DONE — terevos2 (removed on Reddit)"
    assert "🗑" not in header, "remove uses the :x: of the vote button"


def test_unknown_resolver_falls_back_to_the_gavel(wired: Any, fake_reddit: Any) -> None:
    actions, slack = wired
    log_item(actions)
    slack.seed_message("900.0", [{"type": "section", "text": {"type": "mrkdwn", "text": "d"}}])
    # item not present in fake_reddit at all: author deleted it

    L._reconcile_modqueue_state(slack, POLL)

    assert header_of(slack) == ":completed: DONE (resolved on Reddit)"


def test_item_still_in_the_queue_is_left_alone(wired: Any, fake_reddit: Any) -> None:
    actions, slack = wired
    log_item(actions)
    fake_reddit.add_queue_item(FakeItem("a1"))

    changed = L._reconcile_modqueue_state(slack, POLL)

    assert changed is False
    assert "done_at" not in actions.get_item_info(CHANNEL, "a1")
    assert slack.updated == []


def test_item_without_a_slack_message_is_skipped(wired: Any, fake_reddit: Any) -> None:
    actions, slack = wired
    data = actions.get_modqueue_file()
    data.setdefault(CHANNEL, {})["a1"] = {"queue_num": 1, "item_type": "submission"}
    actions.write_modqueue_file(data)

    assert L._reconcile_modqueue_state(slack, POLL) is False


# ---------------------------------------------------------------------------
# auto-reopen
# ---------------------------------------------------------------------------

def test_done_item_still_in_the_queue_reopens_after_the_grace_period(wired: Any, fake_reddit: Any) -> None:
    actions, slack = wired
    log_item(actions, done_at=time.time() - GRACE - 1)
    fake_reddit.add_queue_item(FakeItem("a1"))

    changed = L._reconcile_modqueue_state(slack, POLL)

    assert changed is True
    assert "done_at" not in actions.get_item_info(CHANNEL, "a1")
    assert any("Re-opened by bot" in p["text"] for p in slack.posted)


def test_reopen_waits_out_the_grace_period(wired: Any, fake_reddit: Any) -> None:
    """A mod who clicks Done then actions it on Reddit must not be second-guessed."""
    actions, slack = wired
    log_item(actions, done_at=time.time() - 1)
    fake_reddit.add_queue_item(FakeItem("a1"))

    changed = L._reconcile_modqueue_state(slack, POLL)

    assert changed is False
    assert actions.get_item_info(CHANNEL, "a1")["done_at"] is not None


def test_grace_period_scales_with_the_poll_interval(wired: Any, fake_reddit: Any) -> None:
    actions, slack = wired
    log_item(actions, done_at=time.time() - 70)
    fake_reddit.add_queue_item(FakeItem("a1"))

    assert L._reconcile_modqueue_state(slack, 60) is False, "grace is 2x the interval = 120s"
    assert L._reconcile_modqueue_state(slack, POLL) is True, "at 30s the grace is 60s"


def test_reconcile_without_a_configured_channel_is_a_no_op(monkeypatch: pytest.MonkeyPatch, actions: RedditActions, slack: Any) -> None:
    monkeypatch.setattr(L, "reddit", actions)
    monkeypatch.setattr(L, "modqueue_channel", None)
    assert L._reconcile_modqueue_state(slack, POLL) is False


# ---------------------------------------------------------------------------
# 5xx backoff
# ---------------------------------------------------------------------------

class _Resp:
    """The HTTP response both client libraries hang off an exception."""
    def __init__(self, status_code: Any) -> None:
        """Store the status code."""
        self.status_code = status_code


class _ApiError(Exception):
    """Mimics slack_sdk's SlackApiError, which carries its response."""
    def __init__(self, status: Any) -> None:
        """Build an error carrying *status*."""
        super().__init__(f"status {status}")
        self.response = _Resp(status)


class ServerError(Exception):
    """Same name as prawcore's, which is what the check keys off."""


def test_500_is_a_server_error() -> None:
    assert L._is_server_error(_ApiError(500))


def test_503_is_a_server_error() -> None:
    assert L._is_server_error(_ApiError(503))


def test_prawcore_server_error_is_recognised_without_a_response() -> None:
    assert L._is_server_error(ServerError("reddit is down"))


def test_429_is_not_a_server_error() -> None:
    assert not L._is_server_error(_ApiError(429))


def test_404_is_not_a_server_error() -> None:
    assert not L._is_server_error(_ApiError(404))


def test_plain_exception_is_not_a_server_error() -> None:
    assert not L._is_server_error(ValueError("bad data"))


def test_server_error_retry_delay_is_fifteen_seconds() -> None:
    """Deliberately shorter than POLL_INTERVAL: a 5xx retries sooner, not later."""
    assert L._SERVER_ERROR_RETRY_DELAY == 15
