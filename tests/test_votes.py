"""Vote recording: toggling, opposing-vote cancellation, and the guarantee that a
concurrent poll cannot erase a vote.

Votes are rows now (see tests/test_store.py), so the poll and the vote handler no
longer write the same record at all. The regression test below stays as it is:
it describes the failure in the caller's terms, and it is the one that would
notice if a whole-log write ever came back."""
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
    erasing votes recorded while it was running. It now inserts only the rows it
    discovered, so there is nothing for it to clobber."""
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


# ---------------------------------------------------------------------------
# Don't ban
# ---------------------------------------------------------------------------

def test_dont_ban_is_offered_next_to_ban() -> None:
    keys = [k for k, _ in RedditActions.VOTE_OPTIONS]
    assert keys.index("dont_ban") == keys.index("ban") + 1, "the pair reads together in the dropdown"


def test_dont_ban_cancels_a_ban_vote(actions: RedditActions) -> None:
    seed(actions)
    actions.record_vote(CHANNEL, "abc", "U1", "ban")
    actions.record_vote(CHANNEL, "abc", "U1", "dont_ban")
    assert actions.get_votes(CHANNEL, "abc")["U1"] == ["dont_ban"]


def test_ban_cancels_a_dont_ban_vote(actions: RedditActions) -> None:
    seed(actions)
    actions.record_vote(CHANNEL, "abc", "U1", "dont_ban")
    actions.record_vote(CHANNEL, "abc", "U1", "ban")
    assert actions.get_votes(CHANNEL, "abc")["U1"] == ["ban"]


def test_dont_ban_leaves_the_removal_vote_alone(actions: RedditActions) -> None:
    """The two questions are separate: remove the post, ban the user."""
    seed(actions)
    actions.record_vote(CHANNEL, "abc", "U1", "remove")
    actions.record_vote(CHANNEL, "abc", "U1", "dont_ban")
    assert set(actions.get_votes(CHANNEL, "abc")["U1"]) == {"remove", "dont_ban"}


# ---------------------------------------------------------------------------
# Which votes hold an item open
# ---------------------------------------------------------------------------

def test_a_ban_vote_holds_the_item_open() -> None:
    assert RedditActions.held_open_by_vote({"U1": ["ban"]}) is True


def test_no_votes_hold_nothing_open() -> None:
    assert RedditActions.held_open_by_vote({}) is False
    assert RedditActions.held_open_by_vote(None) is False


def test_ordinary_votes_do_not_hold_the_item_open() -> None:
    assert RedditActions.held_open_by_vote({"U1": ["remove", "spam"], "U2": ["discuss"]}) is False


def test_another_mods_dont_ban_does_not_release_the_hold() -> None:
    """Votes cancel per mod. Two mods disagreeing is the thing still to resolve."""
    assert RedditActions.held_open_by_vote({"U1": ["ban"], "U2": ["dont_ban"]}) is True


def test_dont_ban_on_its_own_holds_nothing() -> None:
    assert RedditActions.held_open_by_vote({"U1": ["dont_ban"]}) is False


def test_a_legacy_string_ban_vote_still_holds() -> None:
    assert RedditActions.held_open_by_vote({"U1": "ban"}) is True


def test_a_timestamped_ban_vote_still_holds() -> None:
    """Old keys carry a |<timestamp> suffix; vote_keys strips it."""
    assert RedditActions.held_open_by_vote({"U1": ["ban|1775768648"]}) is True


# ---------------------------------------------------------------------------
# counting votes for the status message
# ---------------------------------------------------------------------------

def test_votes_are_counted_across_mods() -> None:
    counts = RedditActions.count_votes({"U1": ["approve"], "U2": ["approve", "discuss"]})
    assert counts == {"approve": 2, "discuss": 1}


def test_spam_counts_as_a_remove() -> None:
    """The two already cancel approve together; they say the same thing."""
    assert RedditActions.count_votes({"U1": ["remove"], "U2": ["spam"]}) == {"remove": 2}


def test_counting_no_votes_is_empty() -> None:
    assert RedditActions.count_votes(None) == {} and RedditActions.count_votes({}) == {}


def test_legacy_vote_shapes_are_counted() -> None:
    """A bare string from the single-vote era, and a stale |<timestamp> suffix."""
    assert RedditActions.count_votes({"U1": "approve", "U2": ["approve|1775768648"]}) == {"approve": 2}


def test_vote_emoji_comes_from_the_button_label() -> None:
    assert RedditActions.vote_emoji("approve") == ":white_check_mark:"
    assert RedditActions.vote_emoji("remove") == ":x:"


def test_an_unknown_vote_key_has_no_emoji() -> None:
    assert RedditActions.vote_emoji("nonsense") == ""


def test_consensus_needs_the_threshold(actions: RedditActions) -> None:
    seed(actions)
    for n in range(RedditActions.CONSENSUS_THRESHOLD - 1):
        actions.record_vote(CHANNEL, "abc", f"U{n}", "approve")
    assert actions.items_with_consensus(CHANNEL) == []

    actions.record_vote(CHANNEL, "abc", "U_LAST", "approve")
    reached = actions.items_with_consensus(CHANNEL)
    assert [(i["item_id"], i["key"], i["count"]) for i in reached] == [("abc", "approve", 3)]


def seed_items(actions: RedditActions, **nums: int) -> None:
    """Record several modqueue items at once (``seed`` replaces the whole log)."""
    actions.write_modqueue_file({CHANNEL: {
        item_id: {"queue_num": num, "item_type": "submission"} for item_id, num in nums.items()
    }})


def test_unvoted_items_exclude_the_asking_mod(actions: RedditActions) -> None:
    seed_items(actions, a1=1, a2=2)
    actions.record_vote(CHANNEL, "a1", "U1", "approve")
    assert [i["item_id"] for i in actions.items_without_vote_from(CHANNEL, "U1")] == ["a2"]


def test_another_mods_vote_does_not_count_as_yours(actions: RedditActions) -> None:
    seed(actions)
    actions.record_vote(CHANNEL, "abc", "U1", "approve")
    assert [i["item_id"] for i in actions.items_without_vote_from(CHANNEL, "U2")] == ["abc"]


def test_the_voter_match_ignores_case(actions: RedditActions) -> None:
    """The mod roster is upper-cased at load; vote rows keep the payload's own."""
    seed(actions)
    actions.record_vote(CHANNEL, "abc", "u_mod", "approve")
    assert actions.items_without_vote_from(CHANNEL, "U_MOD") == []


def test_unvoted_items_are_ordered_by_queue_number(actions: RedditActions) -> None:
    seed_items(actions, a1=9, a2=4)
    assert [i["queue_num"] for i in actions.items_without_vote_from(CHANNEL, "U1")] == [4, 9]
