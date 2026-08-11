"""Vote recording: toggling, opposing-vote cancellation, and the read-merge-write
guarantee that keeps a concurrent poll from erasing votes."""
from __future__ import annotations

from typing import Any

from conftest import CHANNEL
from reddit_actions import RedditActions


def seed(actions: RedditActions, item_id: str = "abc", **extra: Any) -> None:
    """Record a modqueue item so votes have something to attach to."""
    actions.write_modqueue_file({CHANNEL: {item_id: {"queue_num": 1, "item_type": "submission", **extra}}})


def test_first_vote_is_recorded(actions: RedditActions) -> None:
    seed(actions)
    actions.record_vote(CHANNEL, "abc", "U1", "approve")
    assert actions.get_votes(CHANNEL, "abc") == {"U1": ["approve"]}


def test_a_mod_can_hold_several_non_opposing_votes(actions: RedditActions) -> None:
    seed(actions)
    for key in ("approve", "discuss", "lock_thread"):
        actions.record_vote(CHANNEL, "abc", "U1", key)
    assert set(actions.get_votes(CHANNEL, "abc")["U1"]) == {"approve", "discuss", "lock_thread"}


def test_voting_the_same_key_twice_toggles_it_off(actions: RedditActions) -> None:
    seed(actions)
    actions.record_vote(CHANNEL, "abc", "U1", "approve")
    actions.record_vote(CHANNEL, "abc", "U1", "approve")
    assert actions.get_votes(CHANNEL, "abc").get("U1", []) == []


def test_opposing_vote_cancels_the_previous_one(actions: RedditActions) -> None:
    seed(actions)
    actions.record_vote(CHANNEL, "abc", "U1", "approve")
    actions.record_vote(CHANNEL, "abc", "U1", "remove")
    assert actions.get_votes(CHANNEL, "abc")["U1"] == ["remove"]


def test_approve_clears_both_of_its_opposites(actions: RedditActions) -> None:
    """approve opposes remove *and* spam — both must go."""
    seed(actions)
    actions.record_vote(CHANNEL, "abc", "U1", "remove")
    actions.record_vote(CHANNEL, "abc", "U1", "spam")
    actions.record_vote(CHANNEL, "abc", "U1", "approve")
    assert actions.get_votes(CHANNEL, "abc")["U1"] == ["approve"]


def test_opposing_vote_leaves_unrelated_votes_alone(actions: RedditActions) -> None:
    seed(actions)
    actions.record_vote(CHANNEL, "abc", "U1", "discuss")
    actions.record_vote(CHANNEL, "abc", "U1", "approve")
    actions.record_vote(CHANNEL, "abc", "U1", "remove")
    votes = actions.get_votes(CHANNEL, "abc")["U1"]
    assert "discuss" in votes and "remove" in votes and "approve" not in votes


def test_mods_vote_independently(actions: RedditActions) -> None:
    seed(actions)
    actions.record_vote(CHANNEL, "abc", "U1", "approve")
    actions.record_vote(CHANNEL, "abc", "U2", "remove")
    votes = actions.get_votes(CHANNEL, "abc")
    assert votes["U1"] == ["approve"] and votes["U2"] == ["remove"]


def test_votes_survive_a_concurrent_bulk_write(actions: RedditActions, fake_reddit: Any) -> None:
    """Regression: get_modqueue used to write back a stale whole-file snapshot,
    erasing votes recorded while it was running. It must merge, not clobber."""
    from conftest import FakeItem

    fake_reddit.add_queue_item(FakeItem("existing", created_utc=1.0))
    actions.get_modqueue(CHANNEL, no_repost=True, as_blocks=True)   # logs "existing"

    # Poll starts and takes its snapshot of the file...
    actions.posted_to_slack = actions.get_modqueue_file()

    # ...a mod votes while it runs...
    actions.record_vote(CHANNEL, "existing", "U1", "approve")

    # ...and a second poll finds a new item and writes.
    fake_reddit.add_queue_item(FakeItem("fresh", created_utc=2.0))
    actions.get_modqueue(CHANNEL, no_repost=True, as_blocks=True)

    assert actions.get_votes(CHANNEL, "existing") == {"U1": ["approve"]}, "vote was clobbered"
    assert "fresh" in actions.get_modqueue_file()[CHANNEL], "new item was not recorded"


def test_get_votes_on_unknown_item_is_empty(actions: RedditActions) -> None:
    actions.write_modqueue_file({CHANNEL: {}})
    assert actions.get_votes(CHANNEL, "nope") == {}


def test_legacy_string_vote_is_upgraded_to_a_list(actions: RedditActions) -> None:
    """Older logs stored a single vote as a bare string."""
    seed(actions, votes={"U1": "approve"})
    actions.record_vote(CHANNEL, "abc", "U1", "discuss")
    assert set(actions.get_votes(CHANNEL, "abc")["U1"]) == {"approve", "discuss"}


def test_vote_tally_renders_each_voter(actions: RedditActions) -> None:
    tally = RedditActions.format_vote_tally({"U1": ["approve"], "U2": ["approve", "discuss"]})
    assert "U1" in tally and "U2" in tally


def test_vote_tally_is_stable_when_empty(actions: RedditActions) -> None:
    assert isinstance(RedditActions.format_vote_tally({}), str)
