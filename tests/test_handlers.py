"""The three live Bolt action handlers: Done, vote, and Re-open.

Bolt hands each handler a payload dict; the handlers are plain functions, so
they are called directly with a synthetic body and a fake client.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

import reformed_listener as L
from conftest import CHANNEL, FakeItem
from reddit_actions import RedditActions

MOD = "U_MOD"
TS = "900.0"


def ack() -> None:
    """Stand-in for Bolt's acknowledgement callable."""
    return None


def body(action: Dict[str, Any], channel: str = CHANNEL, user: str = MOD, ts: str = TS,
         message_blocks: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Build a Slack interaction payload of the shape Bolt delivers."""
    payload: Dict[str, Any] = {
        "user": {"id": user},
        "container": {"channel_id": channel, "message_ts": ts},
        "actions": [action],
    }
    if message_blocks is not None:
        payload["message"] = {"blocks": message_blocks}
    return payload


DETAIL = {"type": "section", "text": {"type": "mrkdwn", "text": "*Report* detail"}}


class ImmediateThread:
    """Runs the target on ``start()`` instead of in a background thread.

    ``handle_cast_vote`` acks immediately and does its work off-thread; running
    it inline makes the assertions deterministic rather than racy.
    """

    def __init__(self, target: Any = None, daemon: bool = False, **kwargs: Any) -> None:
        """Capture the target instead of spawning a thread."""
        self._target = target

    def start(self) -> None:
        """Run the target inline."""
        if self._target is not None:
            self._target()


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch, feed: Any, actions: RedditActions, slack: Any) -> Any:
    """One resolved feed pointed at the fakes, with one mod authorised."""
    monkeypatch.setattr(L.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(L, "mod_slack_ids", {MOD: "terevos2"})
    return actions, slack


def log_item(actions: RedditActions, item_id: str = "a1", done_at: Any = None) -> None:
    """Record an item as posted to Slack at ``TS``."""
    entry: Dict[str, Any] = {
        "queue_num": 3, "item_type": "submission", "report_link": "http://r",
        "slack_ts": TS, "votes": {}, "slack_blocks": [DETAIL],
    }
    if done_at is not None:
        entry["done_at"] = done_at
    data = actions.get_modqueue_file()
    data.setdefault(CHANNEL, {})[item_id] = entry
    actions.write_modqueue_file(data)


# ---------------------------------------------------------------------------
# mark_done
# ---------------------------------------------------------------------------

def test_done_marks_the_item_and_names_the_mod(wired: Any) -> None:
    actions, slack = wired
    log_item(actions)
    slack.seed_message(TS, [DETAIL])

    L.handle_mark_done(ack, body({"value": "queue|a1|submission"}), slack)

    assert actions.get_item_info(CHANNEL, "a1")["done_at"] is not None
    # Reddit names no resolution for this item, so the card falls back to the
    # gavel — done, action unknown — in the header, the marker and the note.
    assert slack.last_update()["blocks"][0]["text"]["text"].endswith(":completed: DONE — terevos2")
    assert any(RedditActions.is_done_marker(b) for b in slack.last_update()["blocks"])
    assert any(p["text"] == ":completed: Marked done by terevos2" for p in slack.posted)


def test_done_uses_the_emoji_of_what_happened_on_reddit(wired: Any, fake_reddit: Any) -> None:
    actions, slack = wired
    log_item(actions)
    slack.seed_message(TS, [DETAIL])
    fake_reddit.items["a1"] = FakeItem("a1", banned_by="friardon")

    L.handle_mark_done(ack, body({"value": "queue|a1|submission"}), slack)

    blocks = slack.last_update()["blocks"]
    assert blocks[0]["text"]["text"].endswith("❌ DONE — terevos2")
    marker = [b for b in blocks if RedditActions.is_done_marker(b)]
    assert marker and marker[0]["text"]["text"] == "❌ DONE ❌"
    assert any(p["text"] == "❌ Marked done by terevos2" for p in slack.posted)


def test_done_on_a_conversation_sets_conversation_state(wired: Any) -> None:
    actions, slack = wired
    actions.write_modmail_file({"C_MAIL": {"modmail_conv": {"c1": {"slack_ts": TS, "conv_num": 1}}}})
    slack.seed_message(TS, [DETAIL])

    L.handle_mark_done(ack, body({"value": "mail|c1|someone"}, channel="C_MAIL"), slack)

    entry = actions.get_modmail_file()["C_MAIL"]["modmail_conv"]["c1"]
    assert RedditActions.is_done(entry)


def test_done_from_an_unauthorised_user_changes_nothing(wired: Any) -> None:
    actions, slack = wired
    log_item(actions)

    L.handle_mark_done(ack, body({"value": "queue|a1|submission"}, user="U_STRANGER"), slack)

    assert "done_at" not in actions.get_item_info(CHANNEL, "a1")
    assert slack.updated == [] and "not authorized" in slack.ephemeral[0]["text"]


def test_done_from_an_unconfigured_channel_changes_nothing(wired: Any) -> None:
    actions, slack = wired
    log_item(actions)

    L.handle_mark_done(ack, body({"value": "queue|a1|submission"}, channel="C_RANDOM"), slack)

    assert "done_at" not in actions.get_item_info(CHANNEL, "a1")
    assert "not a configured mod feed" in slack.ephemeral[0]["text"]


def test_done_with_a_malformed_value_is_ignored(wired: Any) -> None:
    _, slack = wired
    L.handle_mark_done(ack, body({"value": "garbage"}), slack)
    assert slack.updated == []


# ---------------------------------------------------------------------------
# cast_vote
# ---------------------------------------------------------------------------

def test_vote_is_recorded_and_the_tally_updated(wired: Any) -> None:
    actions, slack = wired
    log_item(actions)
    slack.seed_message(TS, [DETAIL])

    L.handle_cast_vote(ack, body({"selected_option": {"value": "a1|submission|approve"}}), slack)

    assert actions.get_votes(CHANNEL, "a1") == {MOD: ["approve"]}


def test_opposing_vote_replaces_the_earlier_one(wired: Any) -> None:
    actions, slack = wired
    log_item(actions)
    slack.seed_message(TS, [DETAIL])

    L.handle_cast_vote(ack, body({"selected_option": {"value": "a1|submission|approve"}}), slack)
    L.handle_cast_vote(ack, body({"selected_option": {"value": "a1|submission|remove"}}), slack)

    assert actions.get_votes(CHANNEL, "a1") == {MOD: ["remove"]}


def test_unauthorised_vote_is_rejected_with_its_own_wording(wired: Any) -> None:
    actions, slack = wired
    log_item(actions)

    L.handle_cast_vote(ack, body({"selected_option": {"value": "a1|submission|approve"}}, user="U_STRANGER"), slack)

    assert actions.get_votes(CHANNEL, "a1") == {}
    assert slack.ephemeral[0]["text"] == "You are not authorized to vote."


def test_vote_from_an_unconfigured_channel_is_rejected(wired: Any) -> None:
    actions, slack = wired
    log_item(actions)

    L.handle_cast_vote(ack, body({"selected_option": {"value": "a1|submission|approve"}}, channel="C_RANDOM"), slack)

    assert actions.get_votes(CHANNEL, "a1") == {}


# ---------------------------------------------------------------------------
# reopen_item
# ---------------------------------------------------------------------------

def test_reopen_clears_done_state_and_restores_controls(wired: Any, fake_reddit: Any) -> None:
    actions, slack = wired
    log_item(actions, done_at=500.0)
    fake_reddit.items["a1"] = FakeItem("a1")

    L.handle_reopen_item(ack, body({"selected_option": {"value": "a1|submission"}},
                                   message_blocks=[DETAIL]), slack)

    assert "done_at" not in actions.get_item_info(CHANNEL, "a1")
    blocks = slack.last_update()["blocks"]
    action_ids = [e.get("action_id") for b in blocks if b.get("type") == "actions" for e in b["elements"]]
    assert "mark_done" in action_ids
    assert any("Re-opened by terevos2" in p["text"] for p in slack.posted)
    assert blocks[0]["text"]["text"].endswith("🔄 REOPENED — terevos2"), "the card says it was reopened, and by whom"


def test_reopen_falls_back_to_the_existing_message_when_reddit_cannot_serve_it(wired: Any) -> None:
    """A deleted item can no longer be fetched, but must still re-open."""
    actions, slack = wired
    log_item(actions, done_at=500.0)

    L.handle_reopen_item(ack, body({"selected_option": {"value": "a1|submission"}},
                                   message_blocks=[DETAIL]), slack)

    assert "done_at" not in actions.get_item_info(CHANNEL, "a1")
    assert slack.updated, "the message was still rebuilt"


def test_unauthorised_reopen_changes_nothing(wired: Any) -> None:
    actions, slack = wired
    log_item(actions, done_at=500.0)

    L.handle_reopen_item(ack, body({"selected_option": {"value": "a1|submission"}}, user="U_STRANGER"), slack)

    assert actions.get_item_info(CHANNEL, "a1")["done_at"] == 500.0
    assert slack.updated == []


def test_reopen_with_a_malformed_value_is_ignored(wired: Any) -> None:
    _, slack = wired
    L.handle_reopen_item(ack, body({"selected_option": {"value": "garbage"}}), slack)
    assert slack.updated == []
