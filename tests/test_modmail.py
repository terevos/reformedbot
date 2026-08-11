"""Modmail fetching: per-message deduplication, reopen detection, and syncing
Slack's done-state with Reddit's archive state."""
from __future__ import annotations

from typing import Any, Dict, List

from conftest import MAIL_CHANNEL, FakeConversation, FakeModAction, FakeModmailMessage
from reddit_actions import RedditActions

ARCHIVED = RedditActions._ACTION_ARCHIVED
UNARCHIVED = RedditActions._ACTION_UNARCHIVED


def fetch(actions: RedditActions) -> List[Dict[str, Any]]:
    """Run one modmail poll and return the items it would post."""
    return actions.get_conversations(MAIL_CHANNEL, as_blocks=True)


# ---------------------------------------------------------------------------
# deduplication
# ---------------------------------------------------------------------------

def test_new_conversation_is_returned(actions: RedditActions, fake_reddit: Any) -> None:
    fake_reddit._sub.modmail.all = [FakeConversation("c1")]
    items = fetch(actions)
    assert len(items) == 1
    assert items[0]["conv_id"] == "c1" and items[0]["is_new_conv"] is True


def test_a_seen_conversation_is_not_returned_again(actions: RedditActions, fake_reddit: Any) -> None:
    fake_reddit._sub.modmail.all = [FakeConversation("c1")]
    fetch(actions)
    assert fetch(actions) == []


def test_a_new_reply_in_a_known_conversation_is_returned(actions: RedditActions, fake_reddit: Any) -> None:
    """Deduplication is per message, so replies still surface."""
    conv = FakeConversation("c1")
    fake_reddit._sub.modmail.all = [conv]
    fetch(actions)

    conv.messages.append(FakeModmailMessage("c1m2", "someuser", "a follow-up"))
    items = fetch(actions)

    assert len(items) == 1
    assert items[0]["is_new_conv"] is False, "the thread already exists in Slack"


def test_a_reply_is_threaded_under_the_original_message(actions: RedditActions, fake_reddit: Any) -> None:
    conv = FakeConversation("c1")
    fake_reddit._sub.modmail.all = [conv]
    fetch(actions)
    actions.set_conv_slack_ts(MAIL_CHANNEL, "c1", "555.0")

    conv.messages.append(FakeModmailMessage("c1m2", "someuser"))
    items = fetch(actions)

    assert items[0]["thread_ts"] == "555.0"


def test_bare_automated_notices_are_skipped(actions: RedditActions, fake_reddit: Any) -> None:
    fake_reddit._sub.modmail.all = [FakeConversation("auto1", is_auto=True)]
    assert fetch(actions) == []


def test_an_automated_notice_with_a_reply_is_posted(actions: RedditActions, fake_reddit: Any) -> None:
    """Once someone replies it is real modmail, notice included for context."""
    conv = FakeConversation("auto1", is_auto=True)
    conv.messages.append(FakeModmailMessage("auto1m2", "someuser"))
    fake_reddit._sub.modmail.all = [conv]

    assert len(fetch(actions)) == 2


def test_moderator_messages_are_not_flagged_as_user_messages(actions: RedditActions, fake_reddit: Any) -> None:
    fake_reddit._sub.modmail.all = [
        FakeConversation("c1", messages=[FakeModmailMessage("m1", "terevos2")]),
    ]
    assert fetch(actions)[0]["is_user_message"] is False


def test_non_moderator_messages_are_flagged(actions: RedditActions, fake_reddit: Any) -> None:
    fake_reddit._sub.modmail.all = [
        FakeConversation("c1", messages=[FakeModmailMessage("m1", "randomuser")]),
    ]
    assert fetch(actions)[0]["is_user_message"] is True


def test_a_user_reply_marks_a_done_conversation_as_reopened(actions: RedditActions, fake_reddit: Any) -> None:
    conv = FakeConversation("c1")
    fake_reddit._sub.modmail.all = [conv]
    fetch(actions)
    actions.set_conv_done_at(MAIL_CHANNEL, "c1", 100.0)

    conv.messages.append(FakeModmailMessage("c1m2", "randomuser"))
    items = fetch(actions)

    assert items[0]["was_done"] is True, "Slack should show the thread re-opening"
    assert not RedditActions.is_done(actions.get_modmail_file()[MAIL_CHANNEL]["modmail_conv"]["c1"])


def test_conversation_numbers_are_assigned_and_kept(actions: RedditActions, fake_reddit: Any) -> None:
    fake_reddit._sub.modmail.all = [FakeConversation("c1"), FakeConversation("c2")]
    fetch(actions)
    convs = actions.get_modmail_file()[MAIL_CHANNEL]["modmail_conv"]
    assert convs["c1"]["conv_num"] != convs["c2"]["conv_num"]
    assert {convs["c1"]["conv_num"], convs["c2"]["conv_num"]} == {1, 2}


def test_conversation_metadata_is_logged(actions: RedditActions, fake_reddit: Any) -> None:
    fake_reddit._sub.modmail.all = [
        FakeConversation("c1", subject="Ban appeal", messages=[FakeModmailMessage("m1", "someuser")]),
    ]
    fetch(actions)
    entry = actions.get_modmail_file()[MAIL_CHANNEL]["modmail_conv"]["c1"]
    assert entry["subject"] == "Ban appeal" and entry["author"] == "someuser"


def test_a_deleted_author_does_not_break_the_fetch(actions: RedditActions, fake_reddit: Any) -> None:
    fake_reddit._sub.modmail.all = [
        FakeConversation("c1", messages=[FakeModmailMessage("m1", "")]),
    ]
    assert len(fetch(actions)) == 1


# ---------------------------------------------------------------------------
# archive sync
# ---------------------------------------------------------------------------

def seed_conv(actions: RedditActions, conv_id: str = "c1", ts: str = "1.0", done_at: Any = None) -> None:
    """Put a conversation in the log as if it had been posted to Slack."""
    entry: Dict[str, Any] = {"slack_ts": ts, "conv_num": 1, "subject": "s", "author": "someone"}
    if done_at is not None:
        entry["done_at"] = done_at
    data = actions.get_modmail_file()
    data.setdefault(MAIL_CHANNEL, {}).setdefault("modmail_conv", {})[conv_id] = entry
    actions.write_modmail_file(data)


def test_archiving_on_reddit_marks_the_conversation_done(actions: RedditActions, fake_reddit: Any) -> None:
    seed_conv(actions)
    fake_reddit._sub.modmail.by_state["archived"] = [
        FakeConversation("c1", mod_actions=[FakeModAction(ARCHIVED, "terevos2", "2026-07-29T10:00")]),
    ]

    changes = actions.sync_archived_conversations(MAIL_CHANNEL)

    assert [c["conv_id"] for c in changes["archived"]] == ["c1"]
    assert changes["archived"][0]["by"] == "terevos2"
    assert RedditActions.is_done(actions.get_modmail_file()[MAIL_CHANNEL]["modmail_conv"]["c1"])


def test_unarchiving_on_reddit_reopens_the_conversation(actions: RedditActions, fake_reddit: Any) -> None:
    seed_conv(actions, done_at=100.0)
    fake_reddit._sub.modmail.by_state["new"] = [
        FakeConversation("c1", mod_actions=[FakeModAction(UNARCHIVED, "friardon", "2026-07-29T11:00")]),
    ]

    changes = actions.sync_archived_conversations(MAIL_CHANNEL)

    assert [c["conv_id"] for c in changes["unarchived"]] == ["c1"]
    assert changes["unarchived"][0]["by"] == "friardon"
    assert not RedditActions.is_done(actions.get_modmail_file()[MAIL_CHANNEL]["modmail_conv"]["c1"])


def test_an_already_done_archived_conversation_is_not_reported_twice(actions: RedditActions, fake_reddit: Any) -> None:
    seed_conv(actions, done_at=100.0)
    fake_reddit._sub.modmail.by_state["archived"] = [FakeConversation("c1")]

    changes = actions.sync_archived_conversations(MAIL_CHANNEL)

    assert changes == {"archived": [], "unarchived": []}


def test_the_most_recent_archiver_is_reported(actions: RedditActions, fake_reddit: Any) -> None:
    seed_conv(actions)
    fake_reddit._sub.modmail.by_state["archived"] = [
        FakeConversation("c1", mod_actions=[
            FakeModAction(ARCHIVED, "oldermod", "2026-07-01T10:00"),
            FakeModAction(ARCHIVED, "newermod", "2026-07-29T10:00"),
        ]),
    ]

    changes = actions.sync_archived_conversations(MAIL_CHANNEL)

    assert changes["archived"][0]["by"] == "newermod"


def test_an_unattributed_archive_reports_an_empty_actor(actions: RedditActions, fake_reddit: Any) -> None:
    """Reddit logs no action when a user reply re-opens a conversation."""
    seed_conv(actions)
    fake_reddit._sub.modmail.by_state["archived"] = [FakeConversation("c1", mod_actions=[])]

    changes = actions.sync_archived_conversations(MAIL_CHANNEL)

    assert changes["archived"][0]["by"] == ""


def test_conversations_never_posted_to_slack_are_ignored(actions: RedditActions, fake_reddit: Any) -> None:
    actions.write_modmail_file({MAIL_CHANNEL: {"modmail_conv": {"c1": {"conv_num": 1}}}})
    fake_reddit._sub.modmail.by_state["archived"] = [FakeConversation("c1")]

    assert actions.sync_archived_conversations(MAIL_CHANNEL) == {"archived": [], "unarchived": []}


def test_sync_survives_a_reddit_failure(actions: RedditActions, fake_reddit: Any) -> None:
    """A raising Reddit must not take the poll loop down."""
    def boom(*args: Any, **kwargs: Any) -> None:
        """Fail as a broken Reddit would."""
        raise RuntimeError("reddit is unwell")

    fake_reddit._sub.modmail.conversations = boom

    assert actions.sync_archived_conversations(MAIL_CHANNEL) == {"archived": [], "unarchived": []}
