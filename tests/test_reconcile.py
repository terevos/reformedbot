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
def wired(feed: Any, actions: RedditActions, slack: Any) -> Tuple[Any, RedditActions, Any]:
    """The listener's feed, its RedditActions, and the Slack fake."""
    return feed, actions, slack


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


def card_blocks(slack: Any) -> Any:
    """Return the blocks of the most recently rebuilt card.

    Filtered to updates that carry blocks: re-stamping a card also rewrites the
    plain-text thread note beneath it, and that is the later of the two calls.
    """
    with_blocks = [u for u in slack.updated if u.get("blocks")]
    assert with_blocks, "expected a card rebuild but none was made"
    return with_blocks[-1]["blocks"]


def header_of(slack: Any) -> str:
    """Return the header text of the most recent card rebuild."""
    return card_blocks(slack)[0]["text"]["text"]


def marker_of(slack: Any) -> str:
    """Return the DONE marker text of the most recent card rebuild."""
    markers = [b for b in card_blocks(slack) if RedditActions.is_done_marker(b)]
    assert len(markers) == 1, f"expected exactly one DONE marker, got {len(markers)}"
    return markers[0]["text"]["text"]


# ---------------------------------------------------------------------------
# auto-done
# ---------------------------------------------------------------------------

def test_item_gone_from_reddit_is_marked_done(wired: Any, fake_reddit: Any) -> None:
    feed, actions, slack = wired
    log_item(actions)
    slack.seed_message("900.0", [{"type": "section", "text": {"type": "mrkdwn", "text": "detail"}}])
    fake_reddit.items["a1"] = FakeItem("a1", approved=True, approved_by=FakeRedditor("friardon"))

    changed = L._reconcile_modqueue_state(slack, feed, POLL)

    assert changed is True
    assert actions.get_item_info(CHANNEL, "a1")["done_at"] is not None
    assert slack.updated, "the Slack message was updated"


def test_done_header_names_the_approving_mod(wired: Any, fake_reddit: Any) -> None:
    feed, actions, slack = wired
    log_item(actions)
    slack.seed_message("900.0", [{"type": "section", "text": {"type": "mrkdwn", "text": "d"}}])
    fake_reddit.items["a1"] = FakeItem("a1", approved=True, approved_by=FakeRedditor("friardon"))

    L._reconcile_modqueue_state(slack, feed, POLL)

    assert header_of(slack).endswith("✅ DONE — friardon (approved on Reddit)")
    assert header_of(slack).startswith("#1 · post"), "the card keeps its title"


def test_done_header_names_the_removing_mod_with_the_vote_button_emoji(wired: Any, fake_reddit: Any) -> None:
    feed, actions, slack = wired
    log_item(actions)
    slack.seed_message("900.0", [{"type": "section", "text": {"type": "mrkdwn", "text": "d"}}])
    fake_reddit.items["a1"] = FakeItem("a1", removed=True, banned_by=FakeRedditor("terevos2"))

    L._reconcile_modqueue_state(slack, feed, POLL)

    header = header_of(slack)
    assert header.endswith("❌ DONE — terevos2 (removed on Reddit)")
    assert "🗑" not in header, "remove uses the :x: of the vote button"


def test_unknown_resolver_falls_back_to_the_gavel(wired: Any, fake_reddit: Any) -> None:
    feed, actions, slack = wired
    log_item(actions)
    slack.seed_message("900.0", [{"type": "section", "text": {"type": "mrkdwn", "text": "d"}}])
    # item not present in fake_reddit at all: author deleted it

    L._reconcile_modqueue_state(slack, feed, POLL)

    assert header_of(slack).endswith(":completed: DONE (resolved on Reddit)")
    assert marker_of(slack) == RedditActions.DONE_MARKER_TEXT


def test_the_done_marker_matches_the_action_in_the_header(wired: Any, fake_reddit: Any) -> None:
    feed, actions, slack = wired
    log_item(actions)
    slack.seed_message("900.0", [{"type": "section", "text": {"type": "mrkdwn", "text": "d"}}])
    fake_reddit.items["a1"] = FakeItem("a1", removed=True, banned_by=FakeRedditor("terevos2"))

    L._reconcile_modqueue_state(slack, feed, POLL)

    assert marker_of(slack) == "❌ DONE ❌", "the marker says the same as the header"


def test_a_non_gavel_marker_is_still_stripped_on_rebuild(wired: Any, fake_reddit: Any) -> None:
    """A card done as ❌ then reopened and done again must not stack markers."""
    feed, actions, slack = wired
    log_item(actions)
    slack.seed_message("900.0", [
        {"type": "section", "text": {"type": "mrkdwn", "text": "d"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "❌ DONE ❌"}},
    ])
    fake_reddit.items["a1"] = FakeItem("a1", removed=True, banned_by=FakeRedditor("terevos2"))

    L._reconcile_modqueue_state(slack, feed, POLL)

    blocks = card_blocks(slack)
    assert sum(RedditActions.is_done_marker(b) for b in blocks) == 1
    assert blocks[1]["text"]["text"] == "d", "the detail section survives, not the old marker"


# ---------------------------------------------------------------------------
# late resolution: a gavel that becomes the real action
# ---------------------------------------------------------------------------

def test_a_gavel_card_is_restamped_once_reddit_names_the_action(wired: Any, fake_reddit: Any) -> None:
    """The mod clicked Done first and removed the post a moment later."""
    feed, actions, slack = wired
    log_item(actions, done_at=time.time(), done_by="terevos2", done_note_ts="800.0")
    slack.seed_message("900.0", [{"type": "section", "text": {"type": "mrkdwn", "text": "d"}}])
    fake_reddit.items["a1"] = FakeItem("a1", removed=True, banned_by=FakeRedditor("terevos2"))

    L._reconcile_modqueue_state(slack, feed, POLL)

    assert header_of(slack).endswith("❌ DONE — terevos2"), "still credits the mod who clicked Done"
    assert marker_of(slack) == "❌ DONE ❌"
    assert actions.get_item_info(CHANNEL, "a1")["done_action"] == "removed"


def test_the_thread_note_is_restamped_too(wired: Any, fake_reddit: Any) -> None:
    feed, actions, slack = wired
    log_item(actions, done_at=time.time(), done_by="terevos2", done_note_ts="800.0")
    slack.seed_message("900.0", [{"type": "section", "text": {"type": "mrkdwn", "text": "d"}}])
    fake_reddit.items["a1"] = FakeItem("a1", approved=True, approved_by=FakeRedditor("friardon"))

    L._reconcile_modqueue_state(slack, feed, POLL)

    note = [u for u in slack.updated if u["ts"] == "800.0"]
    assert note and note[0]["text"] == "✅ Marked done by terevos2"


def test_an_auto_done_card_takes_the_auto_done_wording(wired: Any, fake_reddit: Any) -> None:
    """Nobody clicked Done, so there is no mod to credit but Reddit's own."""
    feed, actions, slack = wired
    log_item(actions, done_at=time.time())
    slack.seed_message("900.0", [{"type": "section", "text": {"type": "mrkdwn", "text": "d"}}])
    fake_reddit.items["a1"] = FakeItem("a1", removed=True, banned_by=FakeRedditor("friardon"))

    L._reconcile_modqueue_state(slack, feed, POLL)

    assert header_of(slack).endswith("❌ DONE — friardon (removed on Reddit)")
    assert not slack.updated or all(u["ts"] != "800.0" for u in slack.updated), "no note to rewrite"


def test_a_card_with_a_known_action_is_never_re_asked(wired: Any, fake_reddit: Any) -> None:
    feed, actions, slack = wired
    log_item(actions, done_at=time.time(), done_action="removed")
    slack.seed_message("900.0", [{"type": "section", "text": {"type": "mrkdwn", "text": "d"}}])
    fake_reddit.items["a1"] = FakeItem("a1", approved=True, approved_by=FakeRedditor("friardon"))

    L._reconcile_modqueue_state(slack, feed, POLL)

    assert not slack.updated, "the card is settled; asking again is a wasted Reddit call"
    assert "done_checks" not in actions.get_item_info(CHANNEL, "a1")


def test_an_unresolvable_card_stops_being_asked_about(wired: Any) -> None:
    """Deleted by its author: Reddit will never name a resolver."""
    feed, actions, slack = wired
    log_item(actions, done_at=time.time())
    slack.seed_message("900.0", [{"type": "section", "text": {"type": "mrkdwn", "text": "d"}}])

    for _ in range(L._RESOLUTION_RECHECK_LIMIT + 3):
        L._reconcile_modqueue_state(slack, feed, POLL)

    assert actions.get_item_info(CHANNEL, "a1")["done_checks"] == L._RESOLUTION_RECHECK_LIMIT
    assert not slack.updated, "nothing to re-stamp, so the card is left alone"


def test_only_a_few_cards_are_re_asked_per_poll(wired: Any, fake_reddit: Any) -> None:
    """The log holds every item ever posted — a backlog must not go out at once."""
    feed, actions, slack = wired
    for n in range(L._RESOLUTION_RECHECK_BATCH + 4):
        log_item(actions, item_id=f"a{n}", ts=f"90{n}.0", done_at=time.time())
        slack.seed_message(f"90{n}.0", [{"type": "section", "text": {"type": "mrkdwn", "text": "d"}}])

    L._reconcile_modqueue_state(slack, feed, POLL)

    asked = [i for i in actions.get_modqueue_file()[CHANNEL].values() if i.get("done_checks")]
    assert len(asked) == L._RESOLUTION_RECHECK_BATCH


def test_the_newest_card_is_asked_about_first(wired: Any) -> None:
    """A card a mod just closed must not queue behind an old unresolvable one."""
    feed, actions, slack = wired
    now = time.time()
    for n in range(L._RESOLUTION_RECHECK_BATCH):
        log_item(actions, item_id=f"old{n}", ts=f"70{n}.0", done_at=now - 9000)
    log_item(actions, item_id="fresh", ts="990.0", done_at=now)

    L._reconcile_modqueue_state(slack, feed, POLL)

    assert actions.get_item_info(CHANNEL, "fresh").get("done_checks") == 1


def test_reopening_forgets_what_was_recorded_about_the_done_state(wired: Any) -> None:
    feed, actions, slack = wired
    entry = log_item(actions, done_at=time.time(), done_action="removed", done_checks=3,
                     done_by="terevos2", done_note_ts="800.0")

    RedditActions.clear_done(entry)

    for key in ("done_action", "done_checks", "done_by", "done_note_ts"):
        assert key not in entry


def test_item_still_in_the_queue_is_left_alone(wired: Any, fake_reddit: Any) -> None:
    feed, actions, slack = wired
    log_item(actions)
    fake_reddit.add_queue_item(FakeItem("a1"))

    changed = L._reconcile_modqueue_state(slack, feed, POLL)

    assert changed is False
    assert "done_at" not in actions.get_item_info(CHANNEL, "a1")
    assert slack.updated == []


def test_item_without_a_slack_message_is_skipped(wired: Any, fake_reddit: Any) -> None:
    feed, actions, slack = wired
    data = actions.get_modqueue_file()
    data.setdefault(CHANNEL, {})["a1"] = {"queue_num": 1, "item_type": "submission"}
    actions.write_modqueue_file(data)

    assert L._reconcile_modqueue_state(slack, feed, POLL) is False


# ---------------------------------------------------------------------------
# ban votes hold an item open
# ---------------------------------------------------------------------------

def test_ban_vote_stops_the_item_being_auto_marked_done(wired: Any, fake_reddit: Any) -> None:
    """The post is dealt with on Reddit; the user is not. A mod closes it by hand."""
    feed, actions, slack = wired
    log_item(actions, votes={"U1": ["ban"]})
    slack.seed_message("900.0", [{"type": "section", "text": {"type": "mrkdwn", "text": "d"}}])
    fake_reddit.items["a1"] = FakeItem("a1", removed=True, banned_by=FakeRedditor("terevos2"))

    changed = L._reconcile_modqueue_state(slack, feed, POLL)

    assert changed is True
    assert "done_at" not in actions.get_item_info(CHANNEL, "a1"), "still open"


def test_a_held_item_says_on_the_card_why_it_is_still_open(wired: Any, fake_reddit: Any) -> None:
    feed, actions, slack = wired
    log_item(actions, votes={"U1": ["ban"]})
    slack.seed_message("900.0", [{"type": "section", "text": {"type": "mrkdwn", "text": "d"}}])
    fake_reddit.items["a1"] = FakeItem("a1", removed=True, banned_by=FakeRedditor("terevos2"))

    L._reconcile_modqueue_state(slack, feed, POLL)

    header = header_of(slack)
    assert header.startswith("#1 · post"), "the card keeps its title"
    assert "❌ REMOVED — terevos2" in header, "what happened on Reddit"
    assert header.endswith(RedditActions.BAN_HOLD_STATUS), "and why it is still here"
    assert any("ban vote is outstanding" in p["text"] for p in slack.posted)


def test_a_held_item_keeps_its_controls(wired: Any, fake_reddit: Any) -> None:
    """Done included — clicking it is how the hold is meant to end."""
    feed, actions, slack = wired
    log_item(actions, votes={"U1": ["ban"]})
    slack.seed_message("900.0", [{"type": "section", "text": {"type": "mrkdwn", "text": "d"}}])
    fake_reddit.items["a1"] = FakeItem("a1", removed=True, banned_by=FakeRedditor("terevos2"))

    L._reconcile_modqueue_state(slack, feed, POLL)

    actions_blocks = [b for b in slack.last_update()["blocks"] if b["type"] == "actions"]
    action_ids = [e["action_id"] for b in actions_blocks for e in b["elements"]]
    assert "mark_done" in action_ids
    assert any(a.startswith("cast_vote_") for a in action_ids), "the ban vote can still be changed"
    assert not any(a == "reopen_item" for a in action_ids), "it never went done, so there is nothing to reopen"


def test_the_hold_notice_is_posted_once_not_every_poll(wired: Any, fake_reddit: Any) -> None:
    feed, actions, slack = wired
    log_item(actions, votes={"U1": ["ban"]})
    slack.seed_message("900.0", [{"type": "section", "text": {"type": "mrkdwn", "text": "d"}}])
    fake_reddit.items["a1"] = FakeItem("a1", removed=True, banned_by=FakeRedditor("terevos2"))

    L._reconcile_modqueue_state(slack, feed, POLL)
    posts_after_first = len(slack.posted)
    updates_after_first = len(slack.updated)

    assert L._reconcile_modqueue_state(slack, feed, POLL) is False, "nothing new to say"
    assert len(slack.posted) == posts_after_first
    assert len(slack.updated) == updates_after_first
    assert actions.get_item_info(CHANNEL, "a1")["ban_hold_at"] is not None


def test_withdrawing_the_ban_vote_lets_the_item_be_auto_done(wired: Any, fake_reddit: Any) -> None:
    feed, actions, slack = wired
    log_item(actions, votes={"U1": ["ban"]})
    slack.seed_message("900.0", [{"type": "section", "text": {"type": "mrkdwn", "text": "d"}}])
    fake_reddit.items["a1"] = FakeItem("a1", removed=True, banned_by=FakeRedditor("terevos2"))

    L._reconcile_modqueue_state(slack, feed, POLL)
    actions.record_vote(CHANNEL, "a1", "U1", "ban")   # toggles it back off

    assert L._reconcile_modqueue_state(slack, feed, POLL) is True
    assert actions.get_item_info(CHANNEL, "a1")["done_at"] is not None
    assert header_of(slack).endswith("❌ DONE — terevos2 (removed on Reddit)")


def test_a_held_item_that_returns_to_the_queue_is_left_alone(wired: Any, fake_reddit: Any) -> None:
    feed, actions, slack = wired
    log_item(actions, votes={"U1": ["ban"]})
    fake_reddit.add_queue_item(FakeItem("a1"))

    assert L._reconcile_modqueue_state(slack, feed, POLL) is False
    assert slack.updated == []


def test_reopening_lets_the_hold_notice_be_posted_again(wired: Any) -> None:
    """ban_hold_at tracks a notice, not a state: a fresh card gets a fresh one."""
    entry = {"done_at": 1.0, "ban_hold_at": 2.0}
    RedditActions.clear_done(entry)
    assert "ban_hold_at" not in entry
    assert entry["reopened_at"] is not None


# ---------------------------------------------------------------------------
# auto-reopen
# ---------------------------------------------------------------------------

def test_done_item_still_in_the_queue_reopens_after_the_grace_period(wired: Any, fake_reddit: Any) -> None:
    feed, actions, slack = wired
    log_item(actions, done_at=time.time() - GRACE - 1)
    fake_reddit.add_queue_item(FakeItem("a1"))

    changed = L._reconcile_modqueue_state(slack, feed, POLL)

    assert changed is True
    assert "done_at" not in actions.get_item_info(CHANNEL, "a1")
    assert any("Re-opened by bot" in p["text"] for p in slack.posted)
    header = slack.last_update()["blocks"][0]
    assert header["text"]["text"].endswith("🔄 REOPENED — still in modqueue")


def test_reopen_waits_out_the_grace_period(wired: Any, fake_reddit: Any) -> None:
    """A mod who clicks Done then actions it on Reddit must not be second-guessed."""
    feed, actions, slack = wired
    log_item(actions, done_at=time.time() - 1)
    fake_reddit.add_queue_item(FakeItem("a1"))

    changed = L._reconcile_modqueue_state(slack, feed, POLL)

    assert changed is False
    assert actions.get_item_info(CHANNEL, "a1")["done_at"] is not None


def test_grace_period_scales_with_the_poll_interval(wired: Any, fake_reddit: Any) -> None:
    feed, actions, slack = wired
    log_item(actions, done_at=time.time() - 70)
    fake_reddit.add_queue_item(FakeItem("a1"))

    assert L._reconcile_modqueue_state(slack, feed, 60) is False, "grace is 2x the interval = 120s"
    assert L._reconcile_modqueue_state(slack, feed, POLL) is True, "at 30s the grace is 60s"


def test_reconcile_without_a_configured_channel_is_a_no_op(wired: Any) -> None:
    feed, _, slack = wired
    feed.modqueue_channel = None
    assert L._reconcile_modqueue_state(slack, feed, POLL) is False


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
