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


def test_done_marker_is_recognised_whatever_its_action_emoji() -> None:
    assert L._is_status_marker(S(RedditActions.done_marker_text("❌")))
    assert L._is_status_marker(S(RedditActions.done_marker_text("✅")))


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


def test_repeated_state_changes_never_stack(feed: Any, actions: RedditActions, slack: Any) -> None:
    """Three state changes in a row must still leave one header and one marker."""
    _conv_logged(actions)
    slack.seed_message("111.0", [DETAIL, ACTIONS])

    for header in ("✅ DONE — friardon", "💬 REPLIED — friardon", "🗄️ ARCHIVED on Reddit by terevos2"):
        L._mark_conv_as_actioned(slack, feed, CHANNEL, "c1", header)

    blocks = slack.last_update()["blocks"]
    assert sum(b["type"] == "header" for b in blocks) == 1
    assert sum(L._is_status_marker(b) for b in blocks) == 1
    header = blocks[0]["text"]["text"]
    assert header.endswith("🗄️ ARCHIVED on Reddit by terevos2"), "latest state wins"
    assert header.count("#F") == 1, "the title is rebuilt, not accumulated"
    assert DETAIL in blocks, "conversation content is preserved"


def test_marking_actioned_strips_interactive_controls(feed: Any, actions: RedditActions, slack: Any) -> None:
    _conv_logged(actions)
    slack.seed_message("111.0", [DETAIL, ACTIONS])

    L._mark_conv_as_actioned(slack, feed, CHANNEL, "c1", "✅ DONE — friardon")

    assert not any(b["type"] == "actions" for b in slack.last_update()["blocks"])


def test_reopen_collapses_duplicates_left_by_the_old_bug(feed: Any, actions: RedditActions, slack: Any) -> None:
    _conv_logged(actions)
    slack.seed_message("111.0", [H("✅ DONE"), H("💬 REPLIED"), DETAIL,
                                 S(L._DONE_MARKER_TEXT), S(L._DONE_MARKER_TEXT)])

    L._mark_conv_as_reopened(slack, feed, CHANNEL, "111.0", "c1")

    blocks = slack.last_update()["blocks"]
    assert sum(b["type"] == "header" for b in blocks) == 1
    assert sum(L._is_status_marker(b) for b in blocks) == 1
    assert blocks[0]["text"]["text"] == "#F · u/someone · s · 🔄 REOPENED"


def test_a_reopened_card_keeps_its_title(feed: Any, actions: RedditActions, slack: Any) -> None:
    """A card marked done has no block IDs left, so the conv_id must be passed.

    Without it the card came back titled just "REOPENED" — the whole identity
    of the conversation gone from its big first line.
    """
    _conv_logged(actions)
    slack.seed_message("111.0", [DETAIL, ACTIONS])
    L._mark_conv_as_actioned(slack, feed, CHANNEL, "c1", "✅ DONE — friardon")
    slack.seed_message("111.0", slack.last_update()["blocks"])

    L._mark_conv_as_reopened(slack, feed, CHANNEL, "111.0", "c1")

    assert slack.last_update()["blocks"][0]["text"]["text"] == "#F · u/someone · s · 🔄 REOPENED"


def test_a_reopened_card_gets_its_controls_back(feed: Any, actions: RedditActions, slack: Any) -> None:
    """Marking done strips the buttons; reopening has to put them back on the card."""
    _conv_logged(actions)
    slack.seed_message("111.0", [DETAIL, ACTIONS])
    L._mark_conv_as_actioned(slack, feed, CHANNEL, "c1", "✅ DONE — friardon")
    slack.seed_message("111.0", slack.last_update()["blocks"])

    L._mark_conv_as_reopened(slack, feed, CHANNEL, "111.0", "c1")

    blocks = slack.last_update()["blocks"]
    ids = [e["action_id"] for b in blocks if b.get("type") == "actions" for e in b["elements"]]
    assert ids == ["mark_done"]


def test_a_reopened_card_on_an_actions_feed_gets_archive_back_too(actions_feed: Any, actions: RedditActions, slack: Any) -> None:
    _conv_logged(actions)
    slack.seed_message("111.0", [DETAIL, ACTIONS])
    L._mark_conv_as_actioned(slack, actions_feed, CHANNEL, "c1", "✅ DONE — friardon")
    slack.seed_message("111.0", slack.last_update()["blocks"])

    L._mark_conv_as_reopened(slack, actions_feed, CHANNEL, "111.0", "c1")

    blocks = slack.last_update()["blocks"]
    ids = [e["action_id"] for b in blocks if b.get("type") == "actions" for e in b["elements"]]
    assert ids == ["modmail_action", "mark_done"]


def test_reopening_twice_does_not_stack_controls(feed: Any, actions: RedditActions, slack: Any) -> None:
    _conv_logged(actions)
    slack.seed_message("111.0", [DETAIL, ACTIONS])

    for _ in range(3):
        L._mark_conv_as_reopened(slack, feed, CHANNEL, "111.0", "c1")
        slack.seed_message("111.0", slack.last_update()["blocks"])

    blocks = slack.last_update()["blocks"]
    assert sum(b["type"] == "header" for b in blocks) == 1
    assert sum(L._is_status_marker(b) for b in blocks) == 1
    assert sum(b["type"] == "actions" for b in blocks) == 1


def test_marking_a_conversation_without_a_slack_ts_is_a_no_op(feed: Any, actions: RedditActions, slack: Any) -> None:
    actions.write_modmail_file({CHANNEL: {"modmail_conv": {"c1": {"conv_num": 1}}}})
    L._mark_conv_as_actioned(slack, feed, CHANNEL, "c1", "✅ DONE")
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
    assert blocks[0]["text"]["text"] == "#4 · post · ✅ DONE — friardon"
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
    assert twice[0]["text"]["text"].endswith("❌ DONE")
    assert "✅" not in twice[0]["text"]["text"], "the superseded status is gone, not appended to"


def test_reopened_item_gets_its_controls_back(actions: RedditActions, fake_reddit: Any) -> None:
    from conftest import FakeItem
    fake_reddit.items["a1"] = FakeItem("a1")
    actions.write_modqueue_file({CHANNEL: {"a1": {
        "queue_num": 4, "item_type": "submission", "report_link": "http://r", "votes": {},
    }}})

    blocks = actions.build_item_blocks_open(CHANNEL, "a1")

    assert sum(b["type"] == "header" for b in blocks) == 1, "one header, holding the title"
    assert "DONE" not in blocks[0]["text"]["text"], "and no done status once reopened"
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


def test_a_reopened_item_says_so_in_its_header(actions: RedditActions, fake_reddit: Any) -> None:
    """An item that went done and came back must not look like a fresh one."""
    from conftest import FakeItem
    fake_reddit.items["a1"] = FakeItem("a1", author="someone")
    actions.write_modqueue_file({CHANNEL: {"a1": {
        "queue_num": 4, "item_type": "submission", "report_link": "http://r",
        "author": "someone", "votes": {},
    }}})

    blocks = actions.build_item_blocks_open(CHANNEL, "a1", status=f"{RedditActions.REOPENED_STATUS} — terevos2")

    assert sum(b["type"] == "header" for b in blocks) == 1
    assert blocks[0]["text"]["text"] == "#4 · post by u/someone · 🔄 REOPENED — terevos2"


def test_a_reopened_item_says_so_even_when_reddit_cannot_serve_it(actions: RedditActions) -> None:
    """The cached-detail fallback carries the status too, not just the rebuild."""
    actions.write_modqueue_file({CHANNEL: {"gone": {
        "queue_num": 9, "item_type": "submission", "report_link": "http://r",
        "author": "someone", "votes": {},
    }}})

    blocks = actions.build_item_blocks_open(CHANNEL, "gone", [DETAIL], RedditActions.REOPENED_STATUS)

    assert blocks[0]["text"]["text"] == "#9 · post by u/someone · 🔄 REOPENED"


def test_marking_a_reopened_item_done_replaces_the_reopened_status(actions: RedditActions) -> None:
    actions.write_modqueue_file({CHANNEL: {"a1": {
        "queue_num": 4, "item_type": "submission", "report_link": "http://r", "votes": {},
    }}})
    reopened = actions.build_item_blocks_open(CHANNEL, "a1", [DETAIL], RedditActions.REOPENED_STATUS)
    done = actions.build_item_blocks_done(CHANNEL, "a1", "✅ DONE — friardon", reopened)

    assert sum(b["type"] == "header" for b in done) == 1
    assert done[0]["text"]["text"] == "#4 · post · ✅ DONE — friardon"
    assert "REOPENED" not in done[0]["text"]["text"]


# ---------------------------------------------------------------------------
# card headers
#
# A header block is the only larger text Slack offers: plain_text, no links,
# 150 characters. Every card carries exactly one, holding its title and — once
# resolved — the status that used to be a header of its own.
# ---------------------------------------------------------------------------

def test_a_modqueue_card_leads_with_its_number_type_and_author(actions: RedditActions) -> None:
    blocks = actions._build_modqueue_blocks(
        item_id="a1", author="someone", report_link="http://r", item_type="submission",
        content="body", user_reports=[], mod_reports=[], queue_num=12,
    )

    assert blocks[0]["type"] == "header"
    assert blocks[0]["text"]["text"] == "#12 · post by u/someone"
    assert blocks[0]["text"]["emoji"] is True, "shortcodes must render as emoji"


def test_the_link_stays_in_the_section_a_header_cannot_hold_it(actions: RedditActions) -> None:
    """plain_text headers render no links, so the permalink moves below."""
    blocks = actions._build_modqueue_blocks(
        item_id="a1", author="someone", report_link="http://r", item_type="submission",
        content="body", user_reports=[], mod_reports=[], queue_num=12,
    )

    assert "http://r" not in blocks[0]["text"]["text"]
    assert "<http://r|View on Reddit>" in blocks[1]["text"]["text"]


def test_a_modmail_card_leads_with_its_letter_author_and_subject(actions: RedditActions) -> None:
    blocks = actions._build_modmail_blocks(
        conv_id="c1", message_id="m1", author="someone", subject="Ban appeal",
        body="hello", date_str="2026-07-29", conv_num=1,
    )

    assert blocks[0]["type"] == "header"
    assert blocks[0]["text"]["text"] == "#A · u/someone · Ban appeal"


def test_a_modmail_reply_gets_no_header_of_its_own(actions: RedditActions) -> None:
    """Replies are threaded under the card, which already has the big line."""
    blocks = actions._build_modmail_blocks(
        conv_id="c1", message_id="m2", author="someone", subject="Ban appeal",
        body="a follow-up", date_str="2026-07-29", conv_num=1, is_reply=True,
    )

    assert not any(b["type"] == "header" for b in blocks)


def test_a_long_header_is_trimmed_to_slacks_limit(actions: RedditActions) -> None:
    """Slack rejects the whole message if a header runs past 150 characters."""
    blocks = actions._build_modmail_blocks(
        conv_id="c1", message_id="m1", author="someone", subject="x" * 400,
        body="hello", date_str="2026-07-29", conv_num=1,
    )

    header = blocks[0]["text"]["text"]
    assert len(header) == RedditActions.HEADER_LIMIT
    assert header.endswith("…")


def test_a_long_title_is_trimmed_before_the_status_is(actions: RedditActions) -> None:
    """The status is what a mod scrolling past most needs to read."""
    text = RedditActions.header_text("#A · u/someone · " + "x" * 400, "✅ DONE — terevos2")

    assert len(text) <= RedditActions.HEADER_LIMIT
    assert text.endswith("✅ DONE — terevos2")
    assert text.startswith("#A · u/someone")


def test_a_status_with_no_title_stands_alone(actions: RedditActions) -> None:
    """A conversation whose log entry is gone still gets a readable header."""
    assert RedditActions.header_text("", "🔄 REOPENED") == "🔄 REOPENED"


def test_a_title_survives_the_done_and_reopen_round_trip(actions: RedditActions, fake_reddit: Any) -> None:
    from conftest import FakeItem
    fake_reddit.items["a1"] = FakeItem("a1", author="someone")
    actions.write_modqueue_file({CHANNEL: {"a1": {
        "queue_num": 4, "item_type": "submission", "report_link": "http://r",
        "author": "someone", "votes": {},
    }}})

    done = actions.build_item_blocks_done(CHANNEL, "a1", "✅ DONE — friardon", [DETAIL])
    reopened = actions.build_item_blocks_open(CHANNEL, "a1", done)

    assert done[0]["text"]["text"] == "#4 · post by u/someone · ✅ DONE — friardon"
    assert reopened[0]["text"]["text"] == "#4 · post by u/someone"


def test_an_item_logged_before_authors_were_recorded_still_gets_a_title(actions: RedditActions) -> None:
    """Entries written by an older build have no author; the rest still renders."""
    actions.write_modqueue_file({CHANNEL: {"a1": {
        "queue_num": 4, "item_type": "comment", "report_link": "http://r", "votes": {},
    }}})

    blocks = actions.build_item_blocks_done(CHANNEL, "a1", "✅ DONE", [DETAIL])

    assert blocks[0]["text"]["text"] == "#4 · comment · ✅ DONE"
