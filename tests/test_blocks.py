"""Slack block rebuilds. A message's blocks are its state store, so a rebuild
that fails to strip the previous status blocks stacks duplicates — the #F bug."""
from __future__ import annotations

from typing import Any, Dict
import pytest

import reformed_listener as L
from conftest import CHANNEL
from reddit_actions import RedditActions


def H(text: str = "✅ DONE — someone") -> Dict[str, Any]:
    """Build a header block."""
    return {"type": "header", "text": {"type": "plain_text", "text": text}}


def S(text: str) -> Dict[str, Any]:
    """Build an mrkdwn section block."""
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


DETAIL = S("*Report* <http://reddit.test|link>\nsome content")
ACTIONS = {"type": "actions", "block_id": "modmail_c1_done", "elements": []}


# ---------------------------------------------------------------------------
# marker detection
# ---------------------------------------------------------------------------

def test_done_marker_is_recognised() -> None:
    assert L._is_status_marker(S(L._DONE_MARKER_TEXT))


def test_reopened_marker_is_recognised() -> None:
    assert L._is_status_marker(S(L._REOPENED_MARKER_TEXT))


def test_ordinary_section_is_not_a_marker() -> None:
    assert not L._is_status_marker(DETAIL)


def test_non_section_block_is_not_a_marker() -> None:
    assert not L._is_status_marker(ACTIONS)
    assert not L._is_status_marker(H())


# ---------------------------------------------------------------------------
# _strip_status_blocks
# ---------------------------------------------------------------------------

def test_strip_removes_header_and_marker_keeps_content() -> None:
    blocks = [H(), DETAIL, S(L._DONE_MARKER_TEXT)]
    assert L._strip_status_blocks(blocks) == [DETAIL]


def test_strip_keeps_actions_by_default() -> None:
    assert ACTIONS in L._strip_status_blocks([H(), DETAIL, ACTIONS])


def test_strip_can_drop_actions() -> None:
    assert ACTIONS not in L._strip_status_blocks([H(), DETAIL, ACTIONS], drop_actions=True)


def test_strip_collapses_an_already_stacked_message() -> None:
    """Conversation #F: done -> replied -> archived left 3 headers and 3 markers."""
    messy = [H("🗄️ ARCHIVED"), H("💬 REPLIED"), H("✅ DONE"),
             DETAIL,
             S(L._DONE_MARKER_TEXT), S(L._DONE_MARKER_TEXT), S(L._DONE_MARKER_TEXT),
             ACTIONS]

    rebuilt = ([H("🗄️ ARCHIVED")]
               + L._strip_status_blocks(messy, drop_actions=True)
               + [S(L._DONE_MARKER_TEXT)])

    assert sum(b["type"] == "header" for b in rebuilt) == 1
    assert sum(L._is_status_marker(b) for b in rebuilt) == 1
    assert DETAIL in rebuilt


def test_strip_is_idempotent() -> None:
    once = L._strip_status_blocks([H(), DETAIL, S(L._DONE_MARKER_TEXT)])
    assert L._strip_status_blocks(once) == once


def test_strip_of_empty_blocks_is_empty() -> None:
    assert L._strip_status_blocks([]) == []


# ---------------------------------------------------------------------------
# marking a conversation actioned, repeatedly
# ---------------------------------------------------------------------------

def _conv_logged(actions: RedditActions, ts: Any="111.0") -> None:
    """Record a conversation as already posted to Slack at *ts*."""
    actions.write_modmail_file({CHANNEL: {"modmail_conv": {
        "c1": {"slack_ts": ts, "conv_num": 6, "subject": "s", "author": "someone"},
    }}})


def test_repeated_state_changes_never_stack(monkeypatch: pytest.MonkeyPatch, actions: RedditActions, slack: Any) -> None:
    """Three state changes in a row must still leave one header and one marker."""
    monkeypatch.setattr(L, "reddit", actions)
    _conv_logged(actions)
    slack.seed_message("111.0", [DETAIL, ACTIONS])

    for header in ("✅ DONE — friardon", "💬 REPLIED — friardon", "🗄️ ARCHIVED on Reddit by terevos2"):
        L._mark_conv_as_actioned(slack, CHANNEL, "c1", header)

    blocks = slack.last_update()["blocks"]
    assert sum(b["type"] == "header" for b in blocks) == 1
    assert sum(L._is_status_marker(b) for b in blocks) == 1
    assert blocks[0]["text"]["text"] == "🗄️ ARCHIVED on Reddit by terevos2", "latest state wins"
    assert DETAIL in blocks, "conversation content is preserved"


def test_marking_actioned_strips_interactive_controls(monkeypatch: pytest.MonkeyPatch, actions: RedditActions, slack: Any) -> None:
    monkeypatch.setattr(L, "reddit", actions)
    _conv_logged(actions)
    slack.seed_message("111.0", [DETAIL, ACTIONS])

    L._mark_conv_as_actioned(slack, CHANNEL, "c1", "✅ DONE — friardon")

    assert not any(b["type"] == "actions" for b in slack.last_update()["blocks"])


def test_reopen_collapses_duplicates_left_by_the_old_bug(monkeypatch: pytest.MonkeyPatch, actions: RedditActions, slack: Any) -> None:
    monkeypatch.setattr(L, "reddit", actions)
    _conv_logged(actions)
    slack.seed_message("111.0", [H("✅ DONE"), H("💬 REPLIED"), DETAIL,
                                 S(L._DONE_MARKER_TEXT), S(L._DONE_MARKER_TEXT)])

    L._mark_conv_as_reopened(slack, CHANNEL, "111.0")

    blocks = slack.last_update()["blocks"]
    assert sum(b["type"] == "header" for b in blocks) == 1
    assert sum(L._is_status_marker(b) for b in blocks) == 1
    assert blocks[0]["text"]["text"] == "🔄 REOPENED"


def test_marking_a_conversation_without_a_slack_ts_is_a_no_op(monkeypatch: pytest.MonkeyPatch, actions: RedditActions, slack: Any) -> None:
    monkeypatch.setattr(L, "reddit", actions)
    actions.write_modmail_file({CHANNEL: {"modmail_conv": {"c1": {"conv_num": 1}}}})
    L._mark_conv_as_actioned(slack, CHANNEL, "c1", "✅ DONE")
    assert slack.updated == []


# ---------------------------------------------------------------------------
# modqueue item rebuild (done <-> open round trip)
# ---------------------------------------------------------------------------

def test_done_blocks_carry_header_detail_and_reopen(actions: RedditActions) -> None:
    actions.write_modqueue_file({CHANNEL: {"a1": {
        "queue_num": 4, "item_type": "submission", "report_link": "http://r",
        "slack_blocks": [DETAIL], "votes": {},
    }}})
    blocks = actions.build_item_blocks_done(CHANNEL, "a1", "✅ DONE — friardon", [DETAIL])

    assert blocks[0]["type"] == "header"
    assert blocks[0]["text"]["text"] == "✅ DONE — friardon"
    assert blocks[0]["text"]["emoji"] is True, ":completed: is a shortcode; it needs emoji=True"
    assert any(b.get("block_id") == "reopen_a1" for b in blocks)
    assert any(b.get("text", {}).get("text") == RedditActions.DONE_MARKER_TEXT for b in blocks)


def test_done_blocks_do_not_stack_when_rebuilt(actions: RedditActions) -> None:
    actions.write_modqueue_file({CHANNEL: {"a1": {
        "queue_num": 4, "item_type": "submission", "report_link": "http://r", "votes": {},
    }}})
    once = actions.build_item_blocks_done(CHANNEL, "a1", "✅ DONE", [DETAIL])
    twice = actions.build_item_blocks_done(CHANNEL, "a1", "❌ DONE", once)

    assert sum(b["type"] == "header" for b in twice) == 1
    assert twice[0]["text"]["text"] == "❌ DONE"


def test_reopened_item_gets_its_controls_back(actions: RedditActions, fake_reddit: Any) -> None:
    from conftest import FakeItem
    fake_reddit.items["a1"] = FakeItem("a1")
    actions.write_modqueue_file({CHANNEL: {"a1": {
        "queue_num": 4, "item_type": "submission", "report_link": "http://r", "votes": {},
    }}})

    blocks = actions.build_item_blocks_open(CHANNEL, "a1")

    assert not any(b["type"] == "header" for b in blocks), "no done header once reopened"
    action_ids = [e.get("action_id") for b in blocks if b.get("type") == "actions" for e in b["elements"]]
    assert "mark_done" in action_ids
    assert any(a.startswith("cast_vote") for a in action_ids)


def test_reopen_falls_back_to_cached_detail_when_reddit_cannot_serve_it(actions: RedditActions) -> None:
    """Deleted items can no longer be fetched; the message must still reopen."""
    actions.write_modqueue_file({CHANNEL: {"gone": {
        "queue_num": 9, "item_type": "submission", "report_link": "http://r", "votes": {},
    }}})
    blocks = actions.build_item_blocks_open(CHANNEL, "gone", [DETAIL])
    assert blocks is not None and DETAIL in blocks
