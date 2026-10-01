"""Summary posting, digest gating, and modmail conversation numbering."""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

import time
from datetime import datetime

import pytest

import reformed_listener as L
from conftest import CHANNEL, MAIL_CHANNEL, FakeItem
from reddit_actions import RedditActions


@pytest.fixture
def queue_feed(feed: Any, actions: RedditActions, slack: Any) -> Tuple[Any, RedditActions, Any]:
    """The listener's feed, its RedditActions, and the Slack fake."""
    return feed, actions, slack


# The modmail summary reads the same feed; the two names only say which half of
# it a test is exercising.
mail_feed = queue_feed


# ---------------------------------------------------------------------------
# queue summary
# ---------------------------------------------------------------------------

def test_empty_queue_posts_the_all_clear(queue_feed: Any) -> None:
    feed, _, slack = queue_feed
    L._post_queue_summary(slack, feed)
    assert "Mod queue is clear" in slack.texts()[0]


def test_pending_items_are_listed_with_their_numbers(queue_feed: Any, fake_reddit: Any) -> None:
    feed, actions, slack = queue_feed
    fake_reddit.add_queue_item(FakeItem("a1", created_utc=1.0))
    actions.write_modqueue_file({CHANNEL: {"a1": {"queue_num": 7, "slack_permalink": "http://s/1"}}})

    L._post_queue_summary(slack, feed)

    text = slack.texts()[0]
    assert "1 item(s) still pending" in text and "#7" in text and "http://s/1" in text


def test_the_pending_count_links_to_the_subreddits_modqueue(queue_feed: Any, fake_reddit: Any) -> None:
    """The link is per feed, and the all-clear stays plain — nothing to act on."""
    feed, _, slack = queue_feed
    fake_reddit.add_queue_item(FakeItem("a1", created_utc=1.0))

    L._post_queue_summary(slack, feed)

    assert "<https://www.reddit.com/mod/reformed/queue|1 item(s) still pending>" in slack.texts()[0]


def test_the_all_clear_carries_no_queue_link(queue_feed: Any) -> None:
    feed, _, slack = queue_feed
    L._post_queue_summary(slack, feed)
    assert "reddit.com/mod" not in slack.texts()[0]


# ---------------------------------------------------------------------------
# the "items with 3+ votes" line
#
# The pending list says what is outstanding; this line says where the mods have
# landed on it. It reads the log's open items rather than Reddit's queue, so an
# item held open past the modqueue by a ban vote still counts.
# ---------------------------------------------------------------------------

def log_item(actions: RedditActions, item_id: str = "a1", **extra: Any) -> None:
    """Record an item as posted to the modqueue channel."""
    entry: Dict[str, Any] = {"queue_num": 7, "item_type": "submission", "author": "someuser"}
    entry.update(extra)
    data = actions.get_modqueue_file()
    data.setdefault(CHANNEL, {})[item_id] = entry
    actions.write_modqueue_file(data)


def vote(actions: RedditActions, item_id: str, key: str, count: int) -> None:
    """Have *count* different mods cast the same vote on *item_id*."""
    for n in range(count):
        actions.record_vote(CHANNEL, item_id, f"U{n}", key)


VOTE_LINE = "Items with 3+ votes"


def test_three_votes_one_way_put_an_item_on_the_vote_line(queue_feed: Any, fake_reddit: Any) -> None:
    feed, actions, slack = queue_feed
    fake_reddit.add_queue_item(FakeItem("a1", created_utc=1.0))
    log_item(actions)
    vote(actions, "a1", "approve", 3)

    L._post_queue_summary(slack, feed)

    text = slack.texts()[0]
    assert VOTE_LINE in text and ":white_check_mark:3" in text


def test_two_votes_are_not_enough(queue_feed: Any, fake_reddit: Any) -> None:
    feed, actions, slack = queue_feed
    fake_reddit.add_queue_item(FakeItem("a1", created_utc=1.0))
    log_item(actions)
    vote(actions, "a1", "approve", 2)

    L._post_queue_summary(slack, feed)

    assert VOTE_LINE not in slack.texts()[0]


def test_a_spam_vote_counts_as_a_remove(queue_feed: Any, fake_reddit: Any) -> None:
    """Three mods saying the post goes is three mods saying the post goes."""
    feed, actions, slack = queue_feed
    fake_reddit.add_queue_item(FakeItem("a1", created_utc=1.0))
    log_item(actions)
    vote(actions, "a1", "remove", 2)
    actions.record_vote(CHANNEL, "a1", "U_SPAM", "spam")

    L._post_queue_summary(slack, feed)

    assert ":x:3" in slack.texts()[0]


def test_ban_votes_do_not_reach_the_vote_line(queue_feed: Any, fake_reddit: Any) -> None:
    """The line answers "is this post staying?" — a ban vote asks something else."""
    feed, actions, slack = queue_feed
    fake_reddit.add_queue_item(FakeItem("a1", created_utc=1.0))
    log_item(actions)
    vote(actions, "a1", "ban", 3)

    L._post_queue_summary(slack, feed)

    assert VOTE_LINE not in slack.texts()[0]


def test_a_done_item_is_never_listed(queue_feed: Any, fake_reddit: Any) -> None:
    feed, actions, slack = queue_feed
    fake_reddit.add_queue_item(FakeItem("a1", created_utc=1.0))
    log_item(actions, done_at=time.time())
    vote(actions, "a1", "approve", 3)

    L._post_queue_summary(slack, feed)

    assert VOTE_LINE not in slack.texts()[0]


def test_an_item_held_open_past_the_queue_is_still_listed(queue_feed: Any) -> None:
    """A ban hold leaves the Reddit queue but still wants acting on."""
    feed, actions, slack = queue_feed
    log_item(actions, "held", queue_num=5)
    vote(actions, "held", "remove", 3)

    L._post_queue_summary(slack, feed)

    text = slack.texts()[0]
    assert VOTE_LINE in text and "#5" in text and ":x:3" in text


def test_a_card_open_past_the_queue_is_not_called_clear(queue_feed: Any) -> None:
    """Saying "clear" over a line listing what is left contradicts itself."""
    feed, actions, slack = queue_feed
    log_item(actions, "held", queue_num=5)

    L._post_queue_summary(slack, feed)

    text = slack.texts()[0]
    assert "clear" not in text.split("(")[0], "it is not clear — #5 is still open"
    assert "1 item(s) still open here" in text and "#5" in text
    assert "nothing left in the Reddit mod queue" in text, "and why it is not in the queue"


def test_nothing_open_at_all_is_the_plain_all_clear(queue_feed: Any) -> None:
    feed, _, slack = queue_feed
    L._post_queue_summary(slack, feed)
    assert slack.texts()[0].startswith(":white_check_mark: Mod queue is clear.")


def test_the_still_open_list_is_ordered_by_queue_number(queue_feed: Any, actions: RedditActions) -> None:
    feed, actions, slack = queue_feed
    actions.write_modqueue_file({CHANNEL: {
        "a1": {"queue_num": 9, "item_type": "submission"},
        "a2": {"queue_num": 4, "item_type": "submission"},
    }})

    L._post_queue_summary(slack, feed)

    text = slack.texts()[0]
    assert "2 item(s) still open here" in text and text.index("#4") < text.index("#9")


def test_a_forced_summary_reposts_a_clear_queue_with_a_card_still_open(queue_feed: Any) -> None:
    """A card nobody has closed is work, votes or no votes."""
    feed, actions, slack = queue_feed
    log_item(actions, "held")
    L._post_queue_summary(slack, feed)

    L._post_queue_summary(slack, feed, force=True)

    assert len(slack.posted) == 2


def test_the_vote_line_links_each_item_to_its_card(queue_feed: Any, fake_reddit: Any) -> None:
    feed, actions, slack = queue_feed
    fake_reddit.add_queue_item(FakeItem("a1", created_utc=1.0))
    log_item(actions, slack_permalink="http://s/1")
    vote(actions, "a1", "approve", 3)

    L._post_queue_summary(slack, feed)

    assert "<http://s/1|#7>" in slack.texts()[0].split(VOTE_LINE)[1]


def test_an_item_split_both_ways_shows_both_counts(queue_feed: Any, fake_reddit: Any) -> None:
    """A genuine disagreement is exactly what a mod needs to see."""
    feed, actions, slack = queue_feed
    fake_reddit.add_queue_item(FakeItem("a1", created_utc=1.0))
    log_item(actions)
    for n in range(4):
        actions.record_vote(CHANNEL, "a1", f"U_YES{n}", "approve")
    for n in range(3):
        actions.record_vote(CHANNEL, "a1", f"U_NO{n}", "remove")

    L._post_queue_summary(slack, feed)

    line = slack.texts()[0].split(VOTE_LINE)[1]
    assert ":white_check_mark:4" in line and ":x:3" in line


def test_a_new_vote_reposts_the_status(queue_feed: Any, fake_reddit: Any) -> None:
    """The line is part of the body, so reaching the threshold is a change."""
    feed, actions, slack = queue_feed
    fake_reddit.add_queue_item(FakeItem("a1", created_utc=1.0))
    log_item(actions)
    vote(actions, "a1", "approve", 2)
    L._post_queue_summary(slack, feed)

    actions.record_vote(CHANNEL, "a1", "U_LAST", "approve")
    L._post_queue_summary(slack, feed)

    assert len(slack.posted) == 2
    assert VOTE_LINE in slack.texts()[1]


def test_a_forced_summary_still_reposts_a_clear_queue_holding_votes(queue_feed: Any) -> None:
    """An item sitting at the threshold with nobody acting is work to nudge about."""
    feed, actions, slack = queue_feed
    log_item(actions, "held")
    vote(actions, "held", "remove", 3)
    L._post_queue_summary(slack, feed)

    L._post_queue_summary(slack, feed, force=True)

    assert len(slack.posted) == 2


# ---------------------------------------------------------------------------
# the "items I haven't voted on" button
# ---------------------------------------------------------------------------

def button_ids(post: Dict[str, Any]) -> List[str]:
    """Return the action_ids of every button on a posted message."""
    return [e.get("action_id") for b in (post["blocks"] or []) if b.get("type") == "actions" for e in b.get("elements", [])]


def test_the_status_message_offers_the_unvoted_button(queue_feed: Any, fake_reddit: Any) -> None:
    feed, actions, slack = queue_feed
    fake_reddit.add_queue_item(FakeItem("a1", created_utc=1.0))
    log_item(actions)

    L._post_queue_summary(slack, feed)

    assert button_ids(slack.posted[0]) == [L._UNVOTED_ACTION]


def test_a_clear_channel_gets_no_button(queue_feed: Any) -> None:
    """Nothing open to vote on, nothing to ask about."""
    feed, _, slack = queue_feed
    L._post_queue_summary(slack, feed)
    assert button_ids(slack.posted[0]) == []


def test_a_feed_that_does_not_vote_gets_no_button(actions_feed: Any, actions: RedditActions, slack: Any, fake_reddit: Any) -> None:
    fake_reddit.add_queue_item(FakeItem("a1", created_utc=1.0))
    log_item(actions)

    L._post_queue_summary(slack, actions_feed)

    assert button_ids(slack.posted[0]) == []


def test_the_status_text_still_carries_the_whole_body(queue_feed: Any, fake_reddit: Any) -> None:
    """text= is what _adopt_status reads back and what a notification shows."""
    feed, actions, slack = queue_feed
    fake_reddit.add_queue_item(FakeItem("a1", created_utc=1.0))
    log_item(actions)

    L._post_queue_summary(slack, feed)

    post = slack.posted[0]
    assert "1 item(s) still pending" in post["text"] and L._STATUS_SIGNATURE in post["text"]
    assert post["blocks"][0]["text"]["text"] == post["text"], "the section shows the same body"


def test_a_long_body_is_trimmed_to_fit_a_section(queue_feed: Any) -> None:
    """Slack rejects the whole message over the cap; text= keeps the full body."""
    feed, _, slack = queue_feed
    L._publish_status(slack, CHANNEL, feed.queue_status, "x" * 4000, repost=True)

    post = slack.posted[0]
    assert len(post["blocks"][0]["text"]["text"]) == L._SECTION_TEXT_MAX
    assert len(post["text"]) > L._SECTION_TEXT_MAX


# ---------------------------------------------------------------------------
# handle_my_unvoted
#
# A channel message renders the same for every reader, so the personal list has
# to come from a click: Slack names who made it, and the reply is ephemeral.
# ---------------------------------------------------------------------------

@pytest.fixture
def authorised(monkeypatch: pytest.MonkeyPatch) -> None:
    """Authorise two mods to act in every feed."""
    monkeypatch.setattr(L, "mod_slack_ids", {"U_ALICE": "alice", "U_BOB": "bob"})


def click(user: str = "U_ALICE", channel: str = CHANNEL) -> Dict[str, Any]:
    """Build the payload Bolt delivers for a button click."""
    return {"user": {"id": user}, "container": {"channel_id": channel, "message_ts": "900.0"}, "actions": [{"action_id": L._UNVOTED_ACTION}]}


def ack() -> None:
    """Stand-in for Bolt's acknowledgement callable."""
    return None


def test_two_mods_clicking_get_different_lists(queue_feed: Any, authorised: None) -> None:
    """The whole point: one button, one answer per mod."""
    feed, actions, slack = queue_feed
    log_item(actions, "a1", queue_num=1)
    log_item(actions, "a2", queue_num=2)
    actions.record_vote(CHANNEL, "a1", "U_ALICE", "approve")

    L.handle_my_unvoted(ack, click("U_ALICE"), slack)
    L.handle_my_unvoted(ack, click("U_BOB"), slack)

    alice, bob = slack.ephemeral[0], slack.ephemeral[1]
    assert alice["user"] == "U_ALICE" and "#2" in alice["text"] and "#1" not in alice["text"]
    assert bob["user"] == "U_BOB" and "#1" in bob["text"] and "#2" in bob["text"]


def test_the_reply_is_ephemeral(queue_feed: Any, authorised: None) -> None:
    """Nobody else in the channel sees another mod's list."""
    feed, actions, slack = queue_feed
    log_item(actions)

    L.handle_my_unvoted(ack, click(), slack)

    assert len(slack.ephemeral) == 1 and slack.posted == []


def test_an_unvoted_item_is_named_the_way_its_card_is(queue_feed: Any, authorised: None) -> None:
    feed, actions, slack = queue_feed
    log_item(actions, slack_permalink="http://s/1")

    L.handle_my_unvoted(ack, click(), slack)

    assert "<http://s/1|#7 · post by u/someuser>" in slack.ephemeral[0]["text"]


def test_a_mod_who_has_voted_on_everything_is_told_so(queue_feed: Any, authorised: None) -> None:
    feed, actions, slack = queue_feed
    log_item(actions)
    actions.record_vote(CHANNEL, "a1", "U_ALICE", "approve")

    L.handle_my_unvoted(ack, click(), slack)

    assert "voted on every open item" in slack.ephemeral[0]["text"]


def test_toggling_the_last_vote_off_counts_as_not_voted(queue_feed: Any, authorised: None) -> None:
    feed, actions, slack = queue_feed
    log_item(actions)
    actions.record_vote(CHANNEL, "a1", "U_ALICE", "approve")
    actions.record_vote(CHANNEL, "a1", "U_ALICE", "approve")  # toggles it off

    L.handle_my_unvoted(ack, click(), slack)

    assert "#7" in slack.ephemeral[0]["text"]


def test_done_items_are_never_in_the_list(queue_feed: Any, authorised: None) -> None:
    feed, actions, slack = queue_feed
    log_item(actions, done_at=time.time())

    L.handle_my_unvoted(ack, click(), slack)

    assert "voted on every open item" in slack.ephemeral[0]["text"]


def test_a_non_mod_gets_no_list(queue_feed: Any, authorised: None) -> None:
    feed, actions, slack = queue_feed
    log_item(actions)

    L.handle_my_unvoted(ack, click("U_STRANGER"), slack)

    assert "#7" not in slack.ephemeral[0]["text"], "the rejection, not the list"
    assert "not authorized" in slack.ephemeral[0]["text"]


def test_a_click_from_a_feed_that_does_not_vote_is_declined(actions_feed: Any, actions: RedditActions, slack: Any, authorised: None) -> None:
    """A status message posted before voting was switched off still has the button."""
    log_item(actions)

    L.handle_my_unvoted(ack, click(), slack)

    assert "Voting is switched off" in slack.ephemeral[0]["text"]


def test_identical_state_is_not_reposted(queue_feed: Any, fake_reddit: Any) -> None:
    feed, _, slack = queue_feed
    fake_reddit.add_queue_item(FakeItem("a1", created_utc=1.0))
    L._post_queue_summary(slack, feed)
    L._post_queue_summary(slack, feed)
    assert len(slack.posted) == 1, "a steady queue must not spam the channel"


def test_a_changed_queue_is_reposted(queue_feed: Any, fake_reddit: Any) -> None:
    feed, _, slack = queue_feed
    fake_reddit.add_queue_item(FakeItem("a1", created_utc=1.0))
    L._post_queue_summary(slack, feed)
    fake_reddit.add_queue_item(FakeItem("a2", created_utc=2.0))
    L._post_queue_summary(slack, feed)
    assert len(slack.posted) == 2


def test_reordering_alone_does_not_repost(queue_feed: Any, fake_reddit: Any) -> None:
    """The dedup key is order-independent."""
    feed, _, slack = queue_feed
    a, b = FakeItem("a1", created_utc=1.0), FakeItem("a2", created_utc=2.0)
    fake_reddit.add_queue_item(a)
    fake_reddit.add_queue_item(b)
    L._post_queue_summary(slack, feed)

    fake_reddit._sub.modqueue_items = [b, a]
    L._post_queue_summary(slack, feed)

    assert len(slack.posted) == 1


def test_force_reposts_unchanged_state(queue_feed: Any, fake_reddit: Any) -> None:
    feed, _, slack = queue_feed
    fake_reddit.add_queue_item(FakeItem("a1", created_utc=1.0))
    L._post_queue_summary(slack, feed)
    L._post_queue_summary(slack, feed, force=True)
    assert len(slack.posted) == 2


def test_status_message_carries_an_updated_timestamp(queue_feed: Any) -> None:
    feed, _, slack = queue_feed
    L._post_queue_summary(slack, feed)
    assert "Updated <!date^" in slack.texts()[0], "status must say when it was last refreshed"


def due_for_refresh(status: Any) -> None:
    """Backdate a status message's last refresh so the next call is due."""
    status.refreshed_at = time.time() - L._STATUS_REFRESH_INTERVAL - 1


def test_unchanged_state_refreshes_the_time_in_place(monkeypatch: pytest.MonkeyPatch, queue_feed: Any, fake_reddit: Any) -> None:
    feed, _, slack = queue_feed
    fake_reddit.add_queue_item(FakeItem("a1", created_utc=1.0))
    L._post_queue_summary(slack, feed)
    due_for_refresh(feed.queue_status)

    L._post_queue_summary(slack, feed)

    assert len(slack.posted) == 1, "a refresh must edit, not repost"
    assert slack.last_update()["ts"] == slack.posted[0]["ts"]
    assert "Updated <!date^" in slack.last_update()["text"]


def test_refresh_reposts_when_the_status_is_no_longer_at_the_bottom(monkeypatch: pytest.MonkeyPatch, queue_feed: Any, fake_reddit: Any) -> None:
    """An edit far up the channel is invisible, so the status moves down instead."""
    feed, _, slack = queue_feed
    fake_reddit.add_queue_item(FakeItem("a1", created_utc=1.0))
    L._post_queue_summary(slack, feed)
    status_ts = slack.posted[0]["ts"]
    slack.chat_postMessage(channel=CHANNEL, text="a newer report")
    due_for_refresh(feed.queue_status)

    L._post_queue_summary(slack, feed)

    assert not slack.updated, "the stranded message must not be edited in place"
    assert [d["ts"] for d in slack.deleted] == [status_ts]
    assert slack.posted[-1]["text"].startswith(":clock2:")


def test_a_changed_queue_replaces_the_old_status_message(queue_feed: Any, fake_reddit: Any) -> None:
    feed, _, slack = queue_feed
    fake_reddit.add_queue_item(FakeItem("a1", created_utc=1.0))
    L._post_queue_summary(slack, feed)
    first_ts = slack.posted[0]["ts"]

    fake_reddit.add_queue_item(FakeItem("a2", created_utc=2.0))
    L._post_queue_summary(slack, feed)

    assert [d["ts"] for d in slack.deleted] == [first_ts], "the superseded status must be cleaned up"
    assert "2 item(s) still pending" in slack.texts()[-1]


def test_a_silent_refresh_does_not_count_as_channel_activity(monkeypatch: pytest.MonkeyPatch, queue_feed: Any, fake_reddit: Any) -> None:
    """The digest's quiet check is about what mods have seen, and an edit is silent."""
    feed, _, slack = queue_feed
    fake_reddit.add_queue_item(FakeItem("a1", created_utc=1.0))
    L._post_queue_summary(slack, feed)
    due_for_refresh(feed.queue_status)
    feed.last_activity_at = 0.0

    L._post_queue_summary(slack, feed)

    assert feed.last_activity_at == 0.0


def test_summary_records_activity_for_the_digest(queue_feed: Any) -> None:
    feed, _, slack = queue_feed
    before = feed.last_activity_at
    L._post_queue_summary(slack, feed)
    assert feed.last_activity_at > before


# ---------------------------------------------------------------------------
# restarts
#
# The live status message is held only in memory, so a restart has to find it
# again in the channel. Without that, every restart strands the old status
# message and posts another one.
# ---------------------------------------------------------------------------

def restart(feed: Any) -> None:
    """Forget exactly what a process restart forgets: the in-memory status."""
    feed.queue_status = L.StatusMessage()
    feed.modmail_status = L.StatusMessage()
    feed.last_activity_at = 0.0


def old_status(slack: Any, channel: str, body: str) -> str:
    """Put a status message in *channel* as if a previous run had posted it."""
    return slack.seed_channel_message(channel, f"{body}\n{L._updated_line()}")


def is_adoption_scan(latest: Any, limit: int) -> bool:
    """Return True for the wide history read that looks for a status message.

    Distinguishes it from the ``limit=1`` "is it still at the bottom?" check.
    """
    return latest is None and limit > 1


def count_scans(monkeypatch: pytest.MonkeyPatch, slack: Any) -> List[str]:
    """Record the channel of every adoption scan; return the growing list."""
    scans: List[str] = []
    original = slack.conversations_history

    def counted(channel: str, latest: Any = None, inclusive: bool = True, limit: int = 1) -> Any:
        """Count adoption scans, then serve the history as usual."""
        if is_adoption_scan(latest, limit):
            scans.append(channel)
        return original(channel=channel, latest=latest, inclusive=inclusive, limit=limit)

    monkeypatch.setattr(slack, "conversations_history", counted)
    return scans


def fail_scans(monkeypatch: pytest.MonkeyPatch, slack: Any) -> Dict[str, bool]:
    """Make adoption scans fail until the returned flag is set to True."""
    reachable = {"value": False}
    original = slack.conversations_history

    def flaky(channel: str, latest: Any = None, inclusive: bool = True, limit: int = 1) -> Any:
        """Fail the adoption scan the way an unreachable Slack does."""
        if is_adoption_scan(latest, limit) and not reachable["value"]:
            raise ConnectionError("slack unreachable")
        return original(channel=channel, latest=latest, inclusive=inclusive, limit=limit)

    monkeypatch.setattr(slack, "conversations_history", flaky)
    return reachable


def test_a_restart_updates_the_status_already_in_the_channel(queue_feed: Any, fake_reddit: Any) -> None:
    feed, _, slack = queue_feed
    fake_reddit.add_queue_item(FakeItem("a1", created_utc=1.0))
    L._post_queue_summary(slack, feed)
    original_ts = slack.posted[0]["ts"]

    restart(feed)
    L._post_queue_summary(slack, feed)

    assert len(slack.posted) == 1, "a restart must not post a second status message"
    assert slack.deleted == [], "nor delete and repost the one already there"
    assert slack.last_update()["ts"] == original_ts, "the existing message is refreshed in place"


def test_a_restart_reuses_the_message_it_did_not_post(queue_feed: Any) -> None:
    """The message is found by its footer, not by a ts this process remembers."""
    feed, _, slack = queue_feed
    ts = old_status(slack, CHANNEL, ":white_check_mark: Mod queue is clear.")

    L._post_queue_summary(slack, feed)

    assert feed.queue_status.ts == ts
    assert slack.last_update()["ts"] == ts
    assert len(slack.posted) == 1, "only the pre-restart message is in the channel"


def test_a_restart_with_a_changed_queue_replaces_the_old_status(queue_feed: Any, fake_reddit: Any) -> None:
    """The stale message is cleaned up rather than left above the new one."""
    feed, _, slack = queue_feed
    ts = old_status(slack, CHANNEL, ":white_check_mark: Mod queue is clear.")
    fake_reddit.add_queue_item(FakeItem("a1", created_utc=1.0))

    L._post_queue_summary(slack, feed)

    assert [d["ts"] for d in slack.deleted] == [ts]
    assert "1 item(s) still pending" in slack.texts()[-1]


def test_the_newest_status_message_is_the_one_adopted(queue_feed: Any) -> None:
    """A channel littered by earlier restarts is taken over at its newest one."""
    feed, _, slack = queue_feed
    old_status(slack, CHANNEL, ":clock2: *1 item(s) still pending:* #1")
    newest = old_status(slack, CHANNEL, ":white_check_mark: Mod queue is clear.")

    L._post_queue_summary(slack, feed)

    assert feed.queue_status.ts == newest


def test_a_human_message_is_never_adopted(queue_feed: Any) -> None:
    feed, _, slack = queue_feed
    ts = slack.seed_channel_message(
        CHANNEL, f":white_check_mark: Mod queue is clear.\n{L._updated_line()}", bot_id=None,
    )

    L._post_queue_summary(slack, feed)

    assert feed.queue_status.ts != ts
    assert slack.posted[-1]["text"].startswith(":white_check_mark:"), "a status of its own was posted"


def test_an_ordinary_bot_message_is_never_adopted(queue_feed: Any) -> None:
    """Item cards and thread notes carry no Updated footer."""
    feed, _, slack = queue_feed
    ts = slack.seed_channel_message(CHANNEL, ":white_check_mark: Marked done by terevos2")

    L._post_queue_summary(slack, feed)

    assert feed.queue_status.ts != ts


def test_the_channel_is_only_searched_once(monkeypatch: pytest.MonkeyPatch, queue_feed: Any) -> None:
    feed, _, slack = queue_feed
    scans = count_scans(monkeypatch, slack)

    L._post_queue_summary(slack, feed)
    due_for_refresh(feed.queue_status)
    L._post_queue_summary(slack, feed)

    assert len(scans) == 1, "adoption is a one-off, not a per-poll history call"


def test_a_failed_search_defers_rather_than_duplicating(monkeypatch: pytest.MonkeyPatch, queue_feed: Any) -> None:
    """Posting while blind to the channel is what leaves duplicates behind."""
    feed, _, slack = queue_feed
    ts = old_status(slack, CHANNEL, ":white_check_mark: Mod queue is clear.")
    reachable = fail_scans(monkeypatch, slack)

    L._post_queue_summary(slack, feed)

    assert len(slack.posted) == 1 and slack.updated == [], "nothing published while blind"
    assert feed.queue_status.adopted is False, "and the next poll tries again"

    reachable["value"] = True
    L._post_queue_summary(slack, feed)

    assert feed.queue_status.ts == ts
    assert len(slack.posted) == 1, "the recovered poll adopts instead of posting"


def test_a_modmail_status_survives_a_restart(mail_feed: Any) -> None:
    """The multi-line modmail body has to round-trip through the channel too."""
    feed, actions, slack = mail_feed
    actions.write_modmail_file({MAIL_CHANNEL: {"modmail_conv": {
        "c1": {"conv_num": 1, "subject": "Ban appeal", "author": "someone", "slack_ts": "1.0"},
    }}})
    L._post_modmail_summary(slack, feed)
    original_ts = slack.posted[0]["ts"]

    restart(feed)
    L._post_modmail_summary(slack, feed)

    assert len(slack.posted) == 1
    assert slack.last_update()["ts"] == original_ts


# ---------------------------------------------------------------------------
# modmail summary
# ---------------------------------------------------------------------------

def test_no_open_conversations_posts_the_all_clear(mail_feed: Any) -> None:
    feed, _, slack = mail_feed
    L._post_modmail_summary(slack, feed)
    assert "resolved" in slack.texts()[0]


def test_open_conversations_are_listed_with_letter_labels(mail_feed: Any) -> None:
    feed, actions, slack = mail_feed
    actions.write_modmail_file({MAIL_CHANNEL: {"modmail_conv": {
        "c1": {"conv_num": 1, "subject": "Ban appeal", "author": "someone", "slack_ts": "1.0"},
    }}})

    L._post_modmail_summary(slack, feed)

    text = slack.texts()[0]
    assert "1 open modmail thread(s)" in text
    assert "#A." in text and "Ban appeal" in text


def test_done_conversations_are_excluded(mail_feed: Any) -> None:
    feed, actions, slack = mail_feed
    actions.write_modmail_file({MAIL_CHANNEL: {"modmail_conv": {
        "c1": {"conv_num": 1, "subject": "open one", "author": "a", "slack_ts": "1.0"},
        "c2": {"conv_num": 2, "subject": "closed one", "author": "b", "slack_ts": "2.0", "done_at": 5.0},
    }}})

    L._post_modmail_summary(slack, feed)

    assert "open one" in slack.texts()[0] and "closed one" not in slack.texts()[0]


def test_unchanged_modmail_state_is_not_reposted(mail_feed: Any) -> None:
    feed, _, slack = mail_feed
    L._post_modmail_summary(slack, feed)
    L._post_modmail_summary(slack, feed)
    assert len(slack.posted) == 1


def test_unchanged_modmail_state_refreshes_the_time_in_place(monkeypatch: pytest.MonkeyPatch, mail_feed: Any) -> None:
    feed, _, slack = mail_feed
    L._post_modmail_summary(slack, feed)
    due_for_refresh(feed.modmail_status)

    L._post_modmail_summary(slack, feed)

    assert len(slack.posted) == 1
    assert slack.last_update()["ts"] == slack.posted[0]["ts"]


# ---------------------------------------------------------------------------
# conversation labels / numbering
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("num,label", [(1, "A"), (2, "B"), (26, "Z"), (27, "AA"), (28, "AB"), (52, "AZ"), (53, "BA")])
def test_conv_labels_are_spreadsheet_style(num: Any, label: Any) -> None:
    assert RedditActions.conv_label(num) == label


@pytest.mark.parametrize("bad", [None, 0, -1, "x"])
def test_invalid_conv_numbers_render_as_question_mark(bad: Any) -> None:
    assert RedditActions.conv_label(bad) == "?"


def test_backfill_numbers_follow_slack_post_order(actions: RedditActions) -> None:
    """Log order is alphabetical by ID, so numbering must use slack_ts."""
    actions.write_modmail_file({MAIL_CHANNEL: {"modmail_conv": {
        "zzz": {"slack_ts": "100.0", "subject": "first posted", "author": "a"},
        "aaa": {"slack_ts": "200.0", "subject": "second posted", "author": "b"},
    }}})

    convs = {c["conv_id"]: c["conv_num"] for c in actions.get_open_conversations(MAIL_CHANNEL)}

    assert convs["zzz"] < convs["aaa"]


def test_backfill_does_not_reuse_an_assigned_number(actions: RedditActions) -> None:
    actions.write_modmail_file({MAIL_CHANNEL: {"modmail_conv": {
        "has": {"slack_ts": "100.0", "conv_num": 1, "subject": "s", "author": "a"},
        "needs": {"slack_ts": "200.0", "subject": "s", "author": "b"},
    }}})

    convs = {c["conv_id"]: c["conv_num"] for c in actions.get_open_conversations(MAIL_CHANNEL)}

    assert convs["has"] == 1 and convs["needs"] != 1


def test_backfill_persists_so_numbers_are_stable(actions: RedditActions) -> None:
    actions.write_modmail_file({MAIL_CHANNEL: {"modmail_conv": {
        "c1": {"slack_ts": "100.0", "subject": "s", "author": "a"},
    }}})
    first = actions.get_open_conversations(MAIL_CHANNEL)[0]["conv_num"]
    second = actions.get_open_conversations(MAIL_CHANNEL)[0]["conv_num"]
    assert first == second
    assert actions.get_modmail_file()[MAIL_CHANNEL]["modmail_conv"]["c1"]["conv_num"] == first


# ---------------------------------------------------------------------------
# digest gating
# ---------------------------------------------------------------------------

def at_hour(monkeypatch: pytest.MonkeyPatch, hour: int, minute: int = 0) -> None:
    """Freeze the digest clock at a local time."""
    real = datetime

    class FrozenDatetime(real):
        """A datetime whose ``now()`` is pinned to the time under test."""

        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            """Return the pinned local time, honouring the requested tzinfo."""
            return real(2026, 7, 29, hour, minute, tzinfo=tz)

    monkeypatch.setattr(L, "datetime", FrozenDatetime)


def test_digest_fires_at_a_scheduled_hour_after_a_quiet_period(monkeypatch: pytest.MonkeyPatch, queue_feed: Any) -> None:
    feed, _, slack = queue_feed
    at_hour(monkeypatch, L._DIGEST_HOURS[0])
    monkeypatch.setattr(L, "_last_digest_slot", None)
    feed.last_activity_at = 0.0   # long quiet

    assert L._maybe_post_digest(slack) is True


def test_digest_does_not_fire_twice_in_the_same_slot(monkeypatch: pytest.MonkeyPatch, queue_feed: Any) -> None:
    feed, _, slack = queue_feed
    at_hour(monkeypatch, L._DIGEST_HOURS[0])
    monkeypatch.setattr(L, "_last_digest_slot", None)
    feed.last_activity_at = 0.0

    assert L._maybe_post_digest(slack) is True
    assert L._maybe_post_digest(slack) is False


def test_digest_is_skipped_when_the_channel_was_recently_active(monkeypatch: pytest.MonkeyPatch, queue_feed: Any) -> None:
    feed, _, slack = queue_feed
    at_hour(monkeypatch, L._DIGEST_HOURS[0])
    monkeypatch.setattr(L, "_last_digest_slot", None)
    feed.last_activity_at = time.time()   # just posted

    assert L._maybe_post_digest(slack) is False


def test_no_digest_outside_the_scheduled_hours(monkeypatch: pytest.MonkeyPatch, queue_feed: Any) -> None:
    feed, _, slack = queue_feed
    at_hour(monkeypatch, 3)
    monkeypatch.setattr(L, "_last_digest_slot", None)
    assert L._maybe_post_digest(slack) is False


def test_no_digest_once_the_window_has_passed(monkeypatch: pytest.MonkeyPatch, queue_feed: Any) -> None:
    """A restart late in the day must not fire the morning digest."""
    feed, _, slack = queue_feed
    at_hour(monkeypatch, L._DIGEST_HOURS[0], minute=L._DIGEST_WINDOW // 60 + 5)
    monkeypatch.setattr(L, "_last_digest_slot", None)
    assert L._maybe_post_digest(slack) is False


def test_a_forced_summary_is_dropped_when_the_queue_is_clear(queue_feed: Any) -> None:
    """The scheduled digest is a nudge about pending work, so silence stays silent."""
    feed, _, slack = queue_feed
    L._post_queue_summary(slack, feed)

    L._post_queue_summary(slack, feed, force=True)

    assert len(slack.posted) == 1, "an all-clear must not be reposted on the clock"
    assert slack.deleted == []


def test_a_forced_summary_reposts_while_items_are_pending(queue_feed: Any, fake_reddit: Any) -> None:
    feed, _, slack = queue_feed
    fake_reddit.add_queue_item(FakeItem("a1", created_utc=1.0))
    L._post_queue_summary(slack, feed)
    first_ts = slack.posted[0]["ts"]

    L._post_queue_summary(slack, feed, force=True)

    assert [d["ts"] for d in slack.deleted] == [first_ts]
    assert "1 item(s) still pending" in slack.texts()[-1]


def test_a_forced_modmail_summary_is_dropped_when_nothing_is_open(mail_feed: Any) -> None:
    feed, _, slack = mail_feed
    L._post_modmail_summary(slack, feed)

    L._post_modmail_summary(slack, feed, force=True)

    assert len(slack.posted) == 1
    assert slack.deleted == []


def test_a_forced_modmail_summary_reposts_while_a_thread_is_open(mail_feed: Any) -> None:
    feed, actions, slack = mail_feed
    actions.write_modmail_file({MAIL_CHANNEL: {"modmail_conv": {
        "c1": {"conv_num": 1, "subject": "Ban appeal", "author": "someone", "slack_ts": "1.0"},
    }}})
    L._post_modmail_summary(slack, feed)
    first_ts = slack.posted[0]["ts"]

    L._post_modmail_summary(slack, feed, force=True)

    assert [d["ts"] for d in slack.deleted] == [first_ts]
    assert "1 open modmail thread(s)" in slack.texts()[-1]


def test_the_digest_reposts_only_the_channel_with_work_pending(monkeypatch: pytest.MonkeyPatch, queue_feed: Any, fake_reddit: Any) -> None:
    """One feed, two channels: the busy one gets a fresh post, the quiet one does not."""
    feed, _, slack = queue_feed
    fake_reddit.add_queue_item(FakeItem("a1", created_utc=1.0))
    L._post_queue_summary(slack, feed)
    L._post_modmail_summary(slack, feed)

    at_hour(monkeypatch, L._DIGEST_HOURS[0])
    monkeypatch.setattr(L, "_last_digest_slot", None)
    feed.last_activity_at = 0.0

    L._maybe_post_digest(slack)

    by_channel = [p["channel"] for p in slack.posted]
    assert by_channel.count(CHANNEL) == 2, "the pending queue is reposted at the bottom"
    assert by_channel.count(MAIL_CHANNEL) == 1, "the resolved modmail channel is left alone"
