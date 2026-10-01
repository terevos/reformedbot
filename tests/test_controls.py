"""Per-feed card controls: which buttons a subreddit's cards carry.

``CONTROLS`` in slack.ini picks between the vote dropdown on modqueue cards and
``actions``, which puts Archive/Unarchive on modmail cards and writes to Reddit
for real. Done is not part of the choice — every card has one — so these tests
check it survives whatever else is switched off.

Modqueue cards used to get a Take action… dropdown from ``actions`` as well.
That was withdrawn; the builder and handler behind it are kept but unreachable,
and the tests covering them below drive them directly so they still work if the
feature is revived.
"""
from __future__ import annotations

import configparser
import json
from typing import Any, Dict, List, Optional

import pytest

import reformed_listener as L
from conftest import CHANNEL, MAIL_CHANNEL, FakeConversation, FakeItem
from reddit_actions import RedditActions

MOD = "U_MOD"
TS = "900.0"
DETAIL = {"type": "section", "text": {"type": "mrkdwn", "text": "*Report* detail"}}


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
        "trigger_id": "T_1",
    }
    if message_blocks is not None:
        payload["message"] = {"blocks": message_blocks}
    return payload


def action_ids(blocks: List[Dict[str, Any]]) -> List[str]:
    """Return every element action_id in *blocks*, in order."""
    return [e.get("action_id", "") for b in blocks if b.get("type") == "actions" for e in b.get("elements", [])]


def log_item(actions: RedditActions, item_id: str = "a1", **extra: Any) -> None:
    """Record an item as posted to Slack at ``TS``."""
    entry: Dict[str, Any] = {
        "queue_num": 3, "item_type": "submission", "report_link": "http://r",
        "slack_ts": TS, "votes": {}, "slack_blocks": [DETAIL], "author": "someuser",
    }
    entry.update(extra)
    data = actions.get_modqueue_file()
    data.setdefault(CHANNEL, {})[item_id] = entry
    actions.write_modqueue_file(data)


class ImmediateThread:
    """Runs the target on ``start()`` instead of in a background thread."""

    def __init__(self, target: Any = None, daemon: bool = False, **kwargs: Any) -> None:
        """Capture the target instead of spawning a thread."""
        self._target = target

    def start(self) -> None:
        """Run the target inline."""
        if self._target is not None:
            self._target()


@pytest.fixture
def authorised(monkeypatch: pytest.MonkeyPatch) -> None:
    """Authorise MOD everywhere and run background work inline."""
    monkeypatch.setattr(L.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(L, "mod_slack_ids", {MOD: "terevos2"})


# ---------------------------------------------------------------------------
# parsing CONTROLS
# ---------------------------------------------------------------------------

def test_an_absent_controls_key_means_voting() -> None:
    """Every card carried the vote dropdown before CONTROLS existed."""
    assert RedditActions.parse_controls(None) == frozenset({"vote"})


def test_controls_accepts_one_name_or_several() -> None:
    assert RedditActions.parse_controls("actions") == frozenset({"actions"})
    assert RedditActions.parse_controls("vote, actions") == frozenset({"vote", "actions"})
    assert RedditActions.parse_controls("VOTE  Actions") == frozenset({"vote", "actions"})


def test_an_empty_controls_leaves_only_the_done_button() -> None:
    """Present but blank is a real choice, not the same as absent."""
    assert RedditActions.parse_controls("") == frozenset()


def test_an_unknown_control_costs_only_itself() -> None:
    """A typo should not silently strip the controls that were spelled right."""
    assert RedditActions.parse_controls("vote, aktions") == frozenset({"vote"})


def test_ignore_reports_is_no_longer_a_control(caplog: Any) -> None:
    """It lived in the Take action… dropdown and went with it."""
    assert RedditActions.parse_controls("actions, ignore_reports") == frozenset({"actions"})
    assert "ignore_reports" in caplog.text


# ---------------------------------------------------------------------------
# ignore reports & approve — dormant, driven directly
# ---------------------------------------------------------------------------

def test_the_dormant_dropdown_leaves_ignore_reports_off_unless_asked(actions: RedditActions) -> None:
    """The heavier of the two approves is never the default."""
    element = actions._build_take_action_element("a1", "submission", "someuser")

    assert "ignore_approve" not in [o["value"].split("|")[0] for o in element["options"]]


def test_the_dormant_dropdown_can_offer_ignore_reports(actions: RedditActions) -> None:
    element = actions._build_take_action_element("a1", "submission", "someuser",
                                                 include_ignore_reports=True)

    assert [o["value"].split("|")[0] for o in element["options"]] == [
        "approve", "ignore_approve", "remove", "warn", "ban"]
    label = next(o["text"]["text"] for o in element["options"] if o["value"].startswith("ignore_approve"))
    assert label == "Ignore reports & Approve"


def test_ignore_reports_ignores_before_it_approves(actions: RedditActions, fake_reddit: Any) -> None:
    """A report landing between the two calls would re-queue the item."""
    item = FakeItem("a1")
    fake_reddit.items["a1"] = item

    actions.approve_and_ignore_reports("a1", "submission")

    assert item.mod.calls == ["ignore_reports", "approve"]


def test_ignore_reports_finds_a_comment_by_its_type(actions: RedditActions, fake_reddit: Any) -> None:
    item = FakeItem("c1", kind="comment")
    fake_reddit.items["c1"] = item

    actions.approve_and_ignore_reports("t1_c1", "comment")

    assert item.mod.calls == ["ignore_reports", "approve"]


# ---------------------------------------------------------------------------
# reading CONTROLS out of slack.ini
# ---------------------------------------------------------------------------

def _cfg(text: str) -> configparser.ConfigParser:
    """Parse an inline slack.ini fragment."""
    cfg = configparser.ConfigParser()
    cfg.read_string(text)
    return cfg


def test_each_subreddit_gets_its_own_controls() -> None:
    feeds = L._load_feeds(_cfg("""
[Subreddit:reformed]
MODQUEUE_CHANNEL = mod_actions
CONTROLS = vote

[Subreddit:whatcouldgowrong]
MODQUEUE_CHANNEL = wcgw_reports
CONTROLS = actions
"""))

    by_name = {f.subreddit: f for f in feeds}
    assert by_name["reformed"].controls == frozenset({"vote"})
    assert by_name["whatcouldgowrong"].controls == frozenset({"actions"})


def test_default_controls_apply_to_feeds_that_do_not_override() -> None:
    feeds = L._load_feeds(_cfg("""
[Default]
CONTROLS = vote, actions

[Subreddit:reformed]
MODQUEUE_CHANNEL = mod_actions

[Subreddit:whatcouldgowrong]
MODQUEUE_CHANNEL = wcgw_reports
CONTROLS = actions
"""))

    by_name = {f.subreddit: f for f in feeds}
    assert by_name["reformed"].controls == frozenset({"vote", "actions"})
    assert by_name["whatcouldgowrong"].controls == frozenset({"actions"}), "its own key wins"


def test_a_config_that_says_nothing_still_votes() -> None:
    feeds = L._load_feeds(_cfg("[Subreddit:reformed]\nMODQUEUE_CHANNEL = mod_actions\n"))
    assert feeds[0].controls == frozenset({"vote"})


# ---------------------------------------------------------------------------
# what the cards carry
# ---------------------------------------------------------------------------

def test_a_voting_feed_gets_the_vote_dropdown_and_no_reddit_actions(actions: RedditActions) -> None:
    blocks = actions._build_modqueue_blocks(
        item_id="a1", author="someuser", report_link="http://r", item_type="submission",
        content="body", user_reports=[], mod_reports=[], queue_num=12,
    )

    ids = action_ids(blocks)
    assert any(a.startswith("cast_vote") for a in ids)
    assert "modqueue_action" not in ids
    assert "mark_done" in ids
    assert any(b.get("block_id") == "vote_tally_a1" for b in blocks)


def test_an_actions_feed_leaves_a_modqueue_card_with_only_done(actions: RedditActions) -> None:
    """`actions` is a modmail control now — it adds nothing to a modqueue card."""
    actions.controls = frozenset({"actions"})

    blocks = actions._build_modqueue_blocks(
        item_id="a1", author="someuser", report_link="http://r", item_type="submission",
        content="body", user_reports=[], mod_reports=[], queue_num=12,
    )

    assert action_ids(blocks) == ["mark_done"], "Done is not part of the choice"
    assert not any(b.get("block_id") == "vote_tally_a1" for b in blocks), \
        "no _No votes yet_ under a card that cannot be voted on"


def test_a_feed_can_have_both(actions: RedditActions) -> None:
    """Both controls: voting on modqueue cards, Archive on modmail ones."""
    actions.controls = frozenset({"vote", "actions"})

    queue = actions._build_modqueue_blocks(
        item_id="a1", author="someuser", report_link="http://r", item_type="submission",
        content="body", user_reports=[], mod_reports=[], queue_num=12,
    )
    mail = actions._build_modmail_blocks(
        conv_id="c1", message_id="m1", author="someone", subject="Ban appeal",
        body="hello", date_str="2026-07-29", conv_num=1,
    )

    assert any(a.startswith("cast_vote") for a in action_ids(queue))
    assert action_ids(mail) == ["modmail_action", "mark_done"]


def test_votes_already_cast_stay_visible_after_voting_is_switched_off(actions: RedditActions) -> None:
    """Switching a feed to actions must not erase the tally it already had."""
    actions.controls = frozenset({"actions"})
    log_item(actions, votes={"U1": ["approve"]})

    blocks = actions.build_item_blocks_done(CHANNEL, "a1", "✅ DONE", [DETAIL])

    assert any(b.get("block_id") == "vote_tally_a1" for b in blocks)


def test_no_control_setting_puts_a_reddit_action_on_a_modqueue_card(actions: RedditActions) -> None:
    """The withdrawn dropdown must not come back through any combination."""
    for controls in (frozenset(), frozenset({"vote"}), frozenset({"actions"}), frozenset({"vote", "actions"})):
        actions.controls = controls
        blocks = actions._build_modqueue_blocks(
            item_id="a1", author="someuser", report_link="http://r", item_type="submission",
            content="body", user_reports=[], mod_reports=[], queue_num=12,
        )
        assert "modqueue_action" not in action_ids(blocks), controls


def test_the_dormant_dropdown_carries_what_the_handler_needs(actions: RedditActions) -> None:
    element = actions._build_take_action_element("a1", "comment", "someuser")

    values = [o["value"] for o in element["options"]]
    assert values == ["approve|a1|comment|someuser", "remove|a1|comment|someuser",
                      "warn|a1|comment|someuser", "ban|a1|comment|someuser"]


def test_warn_and_ban_are_left_off_when_there_is_nobody_to_aim_them_at(actions: RedditActions) -> None:
    """A deleted author has no account, so those two modals could only fail."""
    element = actions._build_take_action_element("a1", "submission", "[deleted]")

    assert [o["value"].split("|")[0] for o in element["options"]] == ["approve", "remove"]


def test_a_reopened_card_comes_back_with_the_feeds_own_controls(actions: RedditActions, fake_reddit: Any) -> None:
    actions.controls = frozenset({"actions"})
    fake_reddit.items["a1"] = FakeItem("a1")
    log_item(actions)

    blocks = actions.build_item_blocks_open(CHANNEL, "a1", [DETAIL])

    assert action_ids(blocks) == ["mark_done"]


# ---------------------------------------------------------------------------
# modmail cards
# ---------------------------------------------------------------------------

def test_modmail_gets_an_archive_button_only_on_an_actions_feed(actions: RedditActions) -> None:
    voting = actions._build_modmail_blocks(
        conv_id="c1", message_id="m1", author="someone", subject="Ban appeal",
        body="hello", date_str="2026-07-29", conv_num=1,
    )
    assert action_ids(voting) == ["mark_done"]

    actions.controls = frozenset({"actions"})
    acting = actions._build_modmail_blocks(
        conv_id="c1", message_id="m1", author="someone", subject="Ban appeal",
        body="hello", date_str="2026-07-29", conv_num=1,
    )
    assert action_ids(acting) == ["modmail_action", "mark_done"]

    archive = next(e for b in acting if b.get("type") == "actions"
                   for e in b["elements"] if e["action_id"] == "modmail_action")
    assert archive["value"] == "archive|c1|someone"


# ---------------------------------------------------------------------------
# the retired modqueue dropdown
# ---------------------------------------------------------------------------

def test_a_click_on_a_retired_dropdown_takes_no_reddit_action(actions_feed: Any, actions: RedditActions,
                                                              slack: Any, fake_reddit: Any, authorised: None) -> None:
    """A card posted while the dropdown was live still carries a working one."""
    item = FakeItem("a1")
    fake_reddit.items["a1"] = item
    log_item(actions)
    slack.seed_message(TS, [DETAIL])

    L.handle_modqueue_action(ack, body({"selected_option": {"value": "approve|a1|submission|someuser"}}), slack)

    assert item.mod.approved is False
    assert slack.updated == []
    assert slack.ephemeral and "withdrawn" in slack.ephemeral[-1]["text"]


# ---------------------------------------------------------------------------
# the dormant handler behind it, driven directly so a revival still works
# ---------------------------------------------------------------------------

def test_approve_acts_on_reddit_and_closes_the_card(actions_feed: Any, actions: RedditActions,
                                                    slack: Any, fake_reddit: Any, authorised: None) -> None:
    item = FakeItem("a1")
    fake_reddit.items["a1"] = item
    log_item(actions)
    slack.seed_message(TS, [DETAIL])

    L.handle_modqueue_action_dormant(ack, body({"selected_option": {"value": "approve|a1|submission|someuser"}}), slack)

    assert item.mod.approved is True
    assert actions.get_item_info(CHANNEL, "a1")["done_at"] is not None, \
        "recorded done here, so the next reconcile does not overwrite the header"
    assert slack.last_update()["blocks"][0]["text"]["text"].endswith("✅ APPROVED — terevos2")


def test_ignore_approve_acts_on_reddit_and_says_so_on_the_card(actions_feed: Any, actions: RedditActions,
                                                               slack: Any, fake_reddit: Any, authorised: None) -> None:
    item = FakeItem("a1")
    fake_reddit.items["a1"] = item
    log_item(actions)
    slack.seed_message(TS, [DETAIL])

    L.handle_modqueue_action_dormant(ack, body({"selected_option": {"value": "ignore_approve|a1|submission|someuser"}}), slack)

    assert item.mod.calls == ["ignore_reports", "approve"]
    assert actions.get_item_info(CHANNEL, "a1")["done_at"] is not None
    assert slack.last_update()["blocks"][0]["text"]["text"].endswith("✅ APPROVED — terevos2 (reports ignored)")
    assert any("future reports on this item are ignored" in p["text"] for p in slack.posted)


def test_remove_opens_the_modal_rather_than_acting_immediately(actions_feed: Any, actions: RedditActions,
                                                               slack: Any, fake_reddit: Any, authorised: None) -> None:
    item = FakeItem("a1")
    fake_reddit.items["a1"] = item
    log_item(actions)
    opened: List[Dict[str, Any]] = []
    slack.views_open = lambda trigger_id, view: opened.append(view) or {"ok": True}

    L.handle_modqueue_action_dormant(ack, body({"selected_option": {"value": "remove|a1|submission|someuser"}}), slack)

    assert opened and opened[0]["callback_id"] == "removal_reason_submitted"
    assert item.mod.removed is False, "nothing is removed until the modal is submitted"


# ---------------------------------------------------------------------------
# the Remove modal: a silent remove needs no reason
# ---------------------------------------------------------------------------

def view_body(delivery: str, reason: Optional[str] = None, text: str = "",
              notes: str = "", channel: str = CHANNEL, item_id: str = "a1") -> Dict[str, Any]:
    """Build a view-submission payload for the Remove modal."""
    values: Dict[str, Any] = {
        "reason_select_block": {"removal_reason_selected": {}},
        "removal_text_block": {"removal_text": {"value": text}},
        "notes_block": {"notes_input": {"value": notes}},
        "delivery_block": {"delivery_input": {"selected_option": {"value": delivery}}},
    }
    if reason:
        values["reason_select_block"]["removal_reason_selected"]["selected_option"] = {
            "value": reason, "text": {"type": "plain_text", "text": "Rule 1"},
        }
    return {
        "user": {"id": MOD},
        "view": {
            "private_metadata": json.dumps({"item_id": item_id, "item_type": "submission",
                                            "channel": channel, "ts": TS, "reddit_link": "http://r"}),
            "state": {"values": values},
        },
    }


class RecordingAck:
    """Bolt's ack, remembering how it was called."""

    def __init__(self) -> None:
        """Start with nothing recorded."""
        self.calls: List[Dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> None:
        """Record one acknowledgement."""
        self.calls.append(kwargs)

    @property
    def errors(self) -> Dict[str, str]:
        """The errors pushed back to the modal, if any."""
        return self.calls[-1].get("errors", {}) if self.calls else {}


def test_the_reason_dropdown_is_optional_in_the_modal() -> None:
    """Slack cannot make one input depend on another, so the rule lives in the handler."""
    view = L.build_remove_modal("a1", reasons=[{"id": "r1", "title": "Rule 1", "message": "m"}])
    reason_block = next(b for b in view["blocks"] if b["block_id"] == "reason_select_block")
    assert reason_block["optional"] is True
    assert "Silent Remove" in reason_block["hint"]["text"]


def test_a_silent_remove_goes_through_without_a_reason(actions_feed: Any, actions: RedditActions,
                                                        slack: Any, fake_reddit: Any, authorised: None) -> None:
    item = FakeItem("a1")
    fake_reddit.items["a1"] = item
    log_item(actions)
    slack.seed_message(TS, [DETAIL])
    acker = RecordingAck()

    L.handle_removal_submitted(acker, view_body("silent"), slack)

    assert acker.errors == {}, "no complaint"
    assert item.mod.removed is True
    assert actions.get_item_info(CHANNEL, "a1")["done_at"] is not None
    assert slack.last_update()["blocks"][0]["text"]["text"].endswith("❌ REMOVED — terevos2")


def test_a_public_remove_without_a_reason_is_bounced_back(actions_feed: Any, actions: RedditActions,
                                                           slack: Any, fake_reddit: Any, authorised: None) -> None:
    """Nothing to send is a silent removal in all but name — say so rather than do it."""
    item = FakeItem("a1")
    fake_reddit.items["a1"] = item
    log_item(actions)
    acker = RecordingAck()

    L.handle_removal_submitted(acker, view_body("public"), slack)

    assert "reason_select_block" in acker.errors
    assert item.mod.removed is False, "the modal stays open, nothing is removed"


def test_a_public_remove_with_only_a_written_message_is_allowed(actions_feed: Any, actions: RedditActions,
                                                                 slack: Any, fake_reddit: Any, authorised: None) -> None:
    """A custom message is a reason; the preset dropdown is one way to write one."""
    item = FakeItem("a1")
    fake_reddit.items["a1"] = item
    log_item(actions)
    slack.seed_message(TS, [DETAIL])
    acker = RecordingAck()

    L.handle_removal_submitted(acker, view_body("public", text="Off topic, please repost in the daily thread."), slack)

    assert acker.errors == {}
    assert item.mod.removed is True


def test_a_private_remove_with_a_preset_reason_is_allowed(actions_feed: Any, actions: RedditActions,
                                                           slack: Any, fake_reddit: Any, authorised: None) -> None:
    item = FakeItem("a1")
    fake_reddit.items["a1"] = item
    log_item(actions)
    slack.seed_message(TS, [DETAIL])
    acker = RecordingAck()

    L.handle_removal_submitted(acker, view_body("private", reason="r1"), slack)

    assert acker.errors == {}
    assert item.mod.removed is True


def test_a_missing_delivery_choice_falls_back_to_silent(actions_feed: Any, actions: RedditActions,
                                                         slack: Any, fake_reddit: Any, authorised: None) -> None:
    """An unintended message to the user is the worse failure of the two."""
    item = FakeItem("a1")
    fake_reddit.items["a1"] = item
    log_item(actions)
    slack.seed_message(TS, [DETAIL])
    acker = RecordingAck()
    payload = view_body("silent")
    payload["view"]["state"]["values"]["delivery_block"] = {"delivery_input": {}}

    L.handle_removal_submitted(acker, payload, slack)

    assert acker.errors == {}
    assert item.mod.removed is True


def test_a_vote_is_refused_on_an_actions_feed(actions_feed: Any, actions: RedditActions,
                                              slack: Any, authorised: None) -> None:
    log_item(actions)

    L.handle_cast_vote(ack, body({"selected_option": {"value": "a1|submission|approve"}}), slack)

    assert actions.get_votes(CHANNEL, "a1") == {}
    assert slack.ephemeral and "switched off" in slack.ephemeral[-1]["text"]


def test_archive_archives_on_reddit_and_marks_the_thread_done(actions_feed: Any, actions: RedditActions,
                                                              slack: Any, fake_reddit: Any, authorised: None) -> None:
    conv = FakeConversation("c1")
    fake_reddit._sub.modmail.all = [conv]
    actions.write_modmail_file({MAIL_CHANNEL: {"modmail_conv": {
        "c1": {"conv_num": 1, "subject": "s", "author": "someone", "slack_ts": TS},
    }}})
    slack.seed_message(TS, [DETAIL])

    L.handle_modmail_action(ack, body({"value": "archive|c1|someone"}, channel=MAIL_CHANNEL), slack)

    entry = actions.get_modmail_file()[MAIL_CHANNEL]["modmail_conv"]["c1"]
    assert conv.archived is True
    assert RedditActions.is_done(entry)
    assert slack.last_update()["blocks"][0]["text"]["text"].endswith("📥 ARCHIVED — terevos2")
    assert any("Archived on Reddit" in p["text"] for p in slack.posted)


def test_archive_is_refused_on_a_voting_feed(feed: Any, actions: RedditActions,
                                             slack: Any, fake_reddit: Any, authorised: None) -> None:
    conv = FakeConversation("c1")
    fake_reddit._sub.modmail.all = [conv]
    actions.write_modmail_file({MAIL_CHANNEL: {"modmail_conv": {
        "c1": {"conv_num": 1, "subject": "s", "author": "someone", "slack_ts": TS},
    }}})

    L.handle_modmail_action(ack, body({"value": "archive|c1|someone"}, channel=MAIL_CHANNEL), slack)

    assert conv.archived is None
    assert slack.ephemeral and "switched off" in slack.ephemeral[-1]["text"]


def test_unarchive_puts_the_conversation_back(actions_feed: Any, actions: RedditActions,
                                              slack: Any, fake_reddit: Any, authorised: None) -> None:
    """The Unarchive dropdown left by _mark_conv_as_archived reaches the same handler."""
    conv = FakeConversation("c1")
    conv.archived = True
    fake_reddit._sub.modmail.all = [conv]
    actions.write_modmail_file({MAIL_CHANNEL: {"modmail_conv": {
        "c1": {"conv_num": 1, "subject": "s", "author": "someone", "slack_ts": TS, "done_at": 100.0},
    }}})
    slack.seed_message(TS, [DETAIL])

    L.handle_modmail_action(ack, body({"selected_option": {"value": "unarchive|c1|someone"}},
                                      channel=MAIL_CHANNEL), slack)

    entry = actions.get_modmail_file()[MAIL_CHANNEL]["modmail_conv"]["c1"]
    assert conv.archived is False
    assert not RedditActions.is_done(entry)
    assert any("Unarchived on Reddit" in p["text"] for p in slack.posted)


def test_an_unauthorised_click_takes_no_reddit_action(actions_feed: Any, actions: RedditActions,
                                                      slack: Any, fake_reddit: Any, authorised: None) -> None:
    item = FakeItem("a1")
    fake_reddit.items["a1"] = item
    log_item(actions)

    L.handle_modqueue_action_dormant(ack, body({"selected_option": {"value": "approve|a1|submission|someuser"}},
                                               user="U_STRANGER"), slack)

    assert item.mod.approved is False
