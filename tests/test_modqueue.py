"""Modqueue fetching: deduplication, queue numbering, and resolution detection."""
from __future__ import annotations

from typing import Any, List, Tuple
from reddit_actions import RedditActions

from conftest import CHANNEL, FakeItem, FakeRedditor


def poll(actions: RedditActions, post: bool = True, **kw: Any) -> Tuple[int, List[Any]]:
    """Run one modqueue poll and return ``(total, new blocks)``.

    With *post*, record a ``slack_ts`` for every card as the listener does
    once Slack accepts it; without it, every post is taken to have failed.
    """
    total, blocks = actions.get_modqueue(CHANNEL, no_repost=True, as_blocks=True, **kw)
    if post:
        for item_id, entry in actions.store.channel_items(CHANNEL).items():
            if not entry.get("slack_ts"):
                actions.set_item_slack_ts(CHANNEL, item_id, f"ts-{item_id}")
    return total, blocks


def test_new_item_is_returned_and_logged(actions: RedditActions, fake_reddit: Any) -> None:
    fake_reddit.add_queue_item(FakeItem("a1", created_utc=1.0))
    total, blocks = poll(actions)
    assert total == 1 and len(blocks) == 1
    assert "a1" in actions.get_modqueue_file()[CHANNEL]


def test_already_posted_item_is_not_returned_again(actions: RedditActions, fake_reddit: Any) -> None:
    fake_reddit.add_queue_item(FakeItem("a1", created_utc=1.0))
    poll(actions)
    total, blocks = poll(actions)
    assert total == 1, "still in the queue, so still counted"
    assert blocks == [], "but not re-posted"


def test_only_the_new_item_is_returned_on_a_later_poll(actions: RedditActions, fake_reddit: Any) -> None:
    fake_reddit.add_queue_item(FakeItem("a1", created_utc=1.0))
    poll(actions)
    fake_reddit.add_queue_item(FakeItem("a2", created_utc=2.0))
    total, blocks = poll(actions)
    assert total == 2 and len(blocks) == 1


def test_item_whose_post_failed_is_offered_again_with_its_number(actions: RedditActions, fake_reddit: Any) -> None:
    """The entry is logged before Slack is asked; a rejected post must not lose the item."""
    fake_reddit.add_queue_item(FakeItem("a1", created_utc=1.0))
    poll(actions, post=False)
    num = actions.get_modqueue_file()[CHANNEL]["a1"]["queue_num"]
    fake_reddit.add_queue_item(FakeItem("a2", created_utc=2.0))
    total, blocks = poll(actions)
    assert len(blocks) == 2, "the unposted card comes back alongside the new one"
    assert actions.get_modqueue_file()[CHANNEL]["a1"]["queue_num"] == num, "and keeps its number"
    assert blocks[0][0]["text"]["text"].startswith(f"#{num} ")
    assert poll(actions)[1] == [], "and is not offered again once posted"


def test_long_comment_card_fits_slack_section_limit(actions: RedditActions, fake_reddit: Any) -> None:
    """A long comment used to overflow the 3000-char section, and Slack rejected the card."""
    body = "\n".join(["x" * 280] * 11)
    fake_reddit.add_queue_item(FakeItem("c1", kind="comment", body=body, created_utc=1.0))
    _, blocks = poll(actions)
    section = blocks[0][1]["text"]["text"]
    assert len(section) <= RedditActions.SECTION_LIMIT
    assert "truncated" in section and "*Reports:*" in section and "View on Reddit" in section


def test_queue_numbers_follow_arrival_order_not_reddit_order(actions: RedditActions, fake_reddit: Any) -> None:
    """Reddit returns newest-first; numbering must reflect when items entered."""
    fake_reddit.add_queue_item(FakeItem("newer", created_utc=200.0))
    fake_reddit.add_queue_item(FakeItem("older", created_utc=100.0))
    poll(actions)
    log = actions.get_modqueue_file()[CHANNEL]
    assert log["older"]["queue_num"] < log["newer"]["queue_num"]


def test_queue_numbers_are_never_reused(actions: RedditActions, fake_reddit: Any) -> None:
    fake_reddit.add_queue_item(FakeItem("a1", created_utc=1.0))
    poll(actions)
    first = actions.get_modqueue_file()[CHANNEL]["a1"]["queue_num"]

    fake_reddit.clear_queue()          # a1 actioned and gone from Reddit
    fake_reddit.add_queue_item(FakeItem("a2", created_utc=2.0))
    poll(actions)

    assert actions.get_modqueue_file()[CHANNEL]["a2"]["queue_num"] > first


def test_comment_and_submission_are_typed_correctly(actions: RedditActions, fake_reddit: Any) -> None:
    fake_reddit.add_queue_item(FakeItem("sub1", kind="submission", created_utc=1.0))
    fake_reddit.add_queue_item(FakeItem("com1", kind="comment", created_utc=2.0))
    poll(actions)
    log = actions.get_modqueue_file()[CHANNEL]
    assert log["sub1"]["item_type"] == "submission"
    assert log["com1"]["item_type"] == "comment"


def test_deleted_author_does_not_break_the_poll(actions: RedditActions, fake_reddit: Any) -> None:
    fake_reddit.add_queue_item(FakeItem("a1", author="", created_utc=1.0))
    total, blocks = poll(actions)
    assert total == 1 and len(blocks) == 1


def test_dedup_is_per_channel(actions: RedditActions, fake_reddit: Any) -> None:
    """Pointing a feed at another channel re-posts currently-open items there."""
    fake_reddit.add_queue_item(FakeItem("a1", created_utc=1.0))
    poll(actions)
    _, blocks_elsewhere = actions.get_modqueue("C_OTHER", no_repost=True, as_blocks=True)
    assert len(blocks_elsewhere) == 1


def test_get_current_modqueue_ids(actions: RedditActions, fake_reddit: Any) -> None:
    fake_reddit.add_queue_item(FakeItem("a1"))
    fake_reddit.add_queue_item(FakeItem("a2"))
    assert set(actions.get_current_modqueue_ids()) == {"a1", "a2"}


# ---------------------------------------------------------------------------
# get_item_resolution — who actioned an item on Reddit, and how
# ---------------------------------------------------------------------------

def test_resolution_reports_the_approving_mod(actions: RedditActions, fake_reddit: Any) -> None:
    fake_reddit.items["x"] = FakeItem("x", approved=True, approved_by=FakeRedditor("friardon"))
    assert actions.get_item_resolution("x") == ("friardon", "approved")


def test_resolution_reports_the_removing_mod(actions: RedditActions, fake_reddit: Any) -> None:
    fake_reddit.items["x"] = FakeItem("x", removed=True, banned_by=FakeRedditor("terevos2"))
    assert actions.get_item_resolution("x") == ("terevos2", "removed")


def test_resolution_accepts_a_bare_username(actions: RedditActions, fake_reddit: Any) -> None:
    """PRAW hands back a plain string in some responses."""
    fake_reddit.items["x"] = FakeItem("x", removed=True, banned_by="AutoModerator")
    assert actions.get_item_resolution("x") == ("AutoModerator", "removed")


def test_resolution_ignores_the_spam_filter_sentinel(actions: RedditActions, fake_reddit: Any) -> None:
    """banned_by is True when Reddit's own filter acted — there is no person."""
    fake_reddit.items["x"] = FakeItem("x", removed=True, banned_by=True)
    assert actions.get_item_resolution("x") == ("", "")


def test_resolution_is_empty_when_nobody_acted(actions: RedditActions, fake_reddit: Any) -> None:
    """e.g. the author deleted their own post."""
    fake_reddit.items["x"] = FakeItem("x")
    assert actions.get_item_resolution("x") == ("", "")


def test_approval_wins_over_an_earlier_removal(actions: RedditActions, fake_reddit: Any) -> None:
    fake_reddit.items["x"] = FakeItem("x", approved=True,
                                      approved_by=FakeRedditor("drkc9n"),
                                      banned_by=FakeRedditor("AutoModerator"))
    assert actions.get_item_resolution("x") == ("drkc9n", "approved")


def test_resolution_survives_an_unfetchable_item(actions: RedditActions, fake_reddit: Any) -> None:
    """Deleted items raise inside PRAW; the caller gets a clean empty answer."""
    assert actions.get_item_resolution("missing") == ("", "")


def test_resolution_looks_up_comments_as_comments(actions: RedditActions, fake_reddit: Any) -> None:
    fake_reddit.items["c1"] = FakeItem("c1", kind="comment", approved=True,
                                       approved_by=FakeRedditor("superlewis"))
    assert actions.get_item_resolution("c1", "comment") == ("superlewis", "approved")
