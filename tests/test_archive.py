"""Per-subreddit logs, numbering counters, and rollover at the cap.

Each subreddit writes its own ``logs/<subreddit>/`` pair; reaching ``#999``
(modqueue) or ``#ZZ`` (modmail) archives that channel's entries and starts the
numbering over. Anything still open — and anything closed recently enough that
the reconcile pass might still touch it — is carried into the new cycle.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List

from conftest import CHANNEL, MAIL_CHANNEL, MOD_LIST, FakeConversation, FakeItem
from reddit_actions import RedditActions


def poll(actions: RedditActions) -> Any:
    """Run one modqueue poll."""
    return actions.get_modqueue(CHANNEL, no_repost=True, as_blocks=True)


def fetch(actions: RedditActions) -> List[Dict[str, Any]]:
    """Run one modmail poll."""
    return actions.get_conversations(MAIL_CHANNEL, as_blocks=True)


def archives(actions: RedditActions) -> List[Path]:
    """Every archive file this subreddit has written, oldest name first."""
    return sorted(Path(actions.archive_dir).glob("*.json"))


def set_counter(actions: RedditActions, kind: str, channel: str, nxt: int, cycle: int = 1) -> None:
    """Fast-forward a channel's counter, so a test need not post 999 items."""
    counters = actions.read_counters()
    counters.setdefault(kind, {})[channel] = {"next": nxt, "cycle": cycle}
    actions.write_counters(counters)


# ---------------------------------------------------------------------------
# per-subreddit files
# ---------------------------------------------------------------------------

def test_logs_live_under_a_per_subreddit_directory(actions: RedditActions, tmp_path: Path) -> None:
    """One database per subreddit, in that subreddit's own directory."""
    actions.write_modqueue_file({CHANNEL: {"a": {}}})
    actions.write_modmail_file({MAIL_CHANNEL: {"modmail_conv": {}}})
    assert (tmp_path / "logs" / "reformed" / "modlog.db").exists()
    assert actions.get_modqueue_file() == {CHANNEL: {"a": {}}}
    assert actions.get_modmail_file() == {MAIL_CHANNEL: {"modmail_conv": {}}}


def test_two_subreddits_do_not_share_a_log(fake_reddit: Any, tmp_path: Path) -> None:
    """The same channel key in two subreddits' logs must not collide."""
    log_dir = str(tmp_path / "logs")
    one = RedditActions("reformed", reddit=fake_reddit, log_dir=log_dir, mod_list=MOD_LIST)
    two = RedditActions("Christianity", reddit=fake_reddit, log_dir=log_dir, mod_list=MOD_LIST)

    one.write_modqueue_file({CHANNEL: {"a1": {"queue_num": 7}}})
    two.write_modqueue_file({CHANNEL: {"b1": {"queue_num": 3}}})

    assert list(one.get_modqueue_file()[CHANNEL]) == ["a1"]
    assert list(two.get_modqueue_file()[CHANNEL]) == ["b1"]
    assert one.sub_log_dir != two.sub_log_dir


def test_subreddit_name_is_slugged_for_the_path(fake_reddit: Any, tmp_path: Path) -> None:
    """Names come from slack.ini, so they are sanitised rather than trusted."""
    actions = RedditActions("../Odd Name", reddit=fake_reddit, log_dir=str(tmp_path / "logs"))
    actions.write_modqueue_file({})
    assert (tmp_path / "logs" / "___odd_name" / "modlog.db").exists()


# ---------------------------------------------------------------------------
# adopting the pre-split shared logs
# ---------------------------------------------------------------------------

def write_legacy(tmp_path: Path, name: str, data: Dict[str, Any]) -> None:
    """Write one of the old shared logs at the root of the log directory."""
    root = tmp_path / "logs"
    root.mkdir(parents=True, exist_ok=True)
    (root / name).write_text(json.dumps(data))


def test_legacy_channel_slice_is_adopted(actions: RedditActions, tmp_path: Path) -> None:
    write_legacy(tmp_path, "modqueue.json", {
        CHANNEL: {"a1": {"queue_num": 4}},
        "C_OTHER_SUBREDDIT": {"z9": {"queue_num": 1}},
    })
    write_legacy(tmp_path, "modmail.json", {MAIL_CHANNEL: {"modmail_conv": {"c1": {"conv_num": 2}}}})

    assert actions.adopt_legacy_logs([CHANNEL, MAIL_CHANNEL]) == 2

    assert actions.get_modqueue_file()[CHANNEL]["a1"]["queue_num"] == 4
    assert "C_OTHER_SUBREDDIT" not in actions.get_modqueue_file(), "another feed's channel is not ours to take"
    assert actions.get_modmail_file()[MAIL_CHANNEL]["modmail_conv"]["c1"]["conv_num"] == 2


def test_legacy_adoption_leaves_the_shared_log_in_place(actions: RedditActions, tmp_path: Path) -> None:
    """Another feed still has to find its own entries in there."""
    write_legacy(tmp_path, "modqueue.json", {CHANNEL: {"a1": {"queue_num": 4}}})
    actions.adopt_legacy_logs([CHANNEL])
    assert (tmp_path / "logs" / "modqueue.json").exists()


def test_legacy_adoption_runs_once_per_channel(actions: RedditActions, tmp_path: Path) -> None:
    """A second pass must not resurrect entries a rollover has since archived."""
    write_legacy(tmp_path, "modqueue.json", {CHANNEL: {"a1": {"queue_num": 4}}})
    actions.adopt_legacy_logs([CHANNEL])

    data = actions.get_modqueue_file()
    data[CHANNEL].pop("a1")
    actions.write_modqueue_file(data)

    assert actions.adopt_legacy_logs([CHANNEL]) == 0
    assert actions.get_modqueue_file()[CHANNEL] == {}


def test_legacy_adoption_is_a_no_op_without_shared_logs(actions: RedditActions) -> None:
    assert actions.adopt_legacy_logs([CHANNEL, MAIL_CHANNEL]) == 0


def test_adopted_numbering_continues_where_the_shared_log_left_off(actions: RedditActions, fake_reddit: Any, tmp_path: Path) -> None:
    write_legacy(tmp_path, "modqueue.json", {CHANNEL: {"a1": {"queue_num": 41, "done_at": time.time()}}})
    actions.adopt_legacy_logs([CHANNEL])

    fake_reddit.add_queue_item(FakeItem("a2", created_utc=2.0))
    poll(actions)
    assert actions.get_modqueue_file()[CHANNEL]["a2"]["queue_num"] == 42


# ---------------------------------------------------------------------------
# counters
# ---------------------------------------------------------------------------

def test_counter_is_persisted_after_a_poll(actions: RedditActions, fake_reddit: Any) -> None:
    fake_reddit.add_queue_item(FakeItem("a1", created_utc=1.0))
    poll(actions)
    assert actions.read_counters()["modqueue"][CHANNEL] == {"next": 2, "cycle": 1}


def test_counter_survives_entries_leaving_the_log(actions: RedditActions, fake_reddit: Any) -> None:
    """The stored counter, not ``max(log) + 1``, is what hands out numbers."""
    fake_reddit.add_queue_item(FakeItem("a1", created_utc=1.0))
    poll(actions)

    data = actions.get_modqueue_file()
    data[CHANNEL].clear()          # as a rollover would leave it
    actions.write_modqueue_file(data)

    fake_reddit.clear_queue()
    fake_reddit.add_queue_item(FakeItem("a2", created_utc=2.0))
    poll(actions)
    assert actions.get_modqueue_file()[CHANNEL]["a2"]["queue_num"] == 2


def test_a_poll_with_nothing_new_does_not_write_a_counter(actions: RedditActions) -> None:
    poll(actions)
    assert actions.read_counters() == {}


# ---------------------------------------------------------------------------
# modqueue rollover
# ---------------------------------------------------------------------------

def test_numbering_wraps_at_the_cap(actions: RedditActions, fake_reddit: Any) -> None:
    set_counter(actions, "modqueue", CHANNEL, RedditActions.QUEUE_NUM_MAX)
    fake_reddit.add_queue_item(FakeItem("last", created_utc=1.0))
    poll(actions)
    assert actions.get_modqueue_file()[CHANNEL]["last"]["queue_num"] == 999

    fake_reddit.clear_queue()
    fake_reddit.add_queue_item(FakeItem("first", created_utc=2.0))
    poll(actions)
    assert actions.get_modqueue_file()[CHANNEL]["first"]["queue_num"] == 1
    assert actions.read_counters()["modqueue"][CHANNEL]["cycle"] == 2


def test_rollover_archives_every_entry(actions: RedditActions, fake_reddit: Any) -> None:
    fake_reddit.add_queue_item(FakeItem("old", created_utc=1.0))
    poll(actions)
    actions.set_item_done_at(CHANNEL, "old", time.time() - 30 * 24 * 3600)

    set_counter(actions, "modqueue", CHANNEL, RedditActions.QUEUE_NUM_MAX + 1)
    fake_reddit.clear_queue()
    fake_reddit.add_queue_item(FakeItem("new", created_utc=2.0))
    poll(actions)

    assert len(archives(actions)) == 1
    archived = json.loads(archives(actions)[0].read_text())
    assert archived["kind"] == "modqueue" and archived["channel"] == CHANNEL
    assert archived["cycle"] == 1 and archived["subreddit"] == "reformed"
    assert "old" in archived["entries"]


def test_rollover_prunes_long_closed_entries_from_the_live_log(actions: RedditActions, fake_reddit: Any) -> None:
    fake_reddit.add_queue_item(FakeItem("old", created_utc=1.0))
    poll(actions)
    actions.set_item_done_at(CHANNEL, "old", time.time() - 30 * 24 * 3600)

    set_counter(actions, "modqueue", CHANNEL, RedditActions.QUEUE_NUM_MAX + 1)
    fake_reddit.clear_queue()
    fake_reddit.add_queue_item(FakeItem("new", created_utc=2.0))
    poll(actions)

    live = actions.get_modqueue_file()[CHANNEL]
    assert "old" not in live
    assert live["new"]["queue_num"] == 1


def test_rollover_carries_open_and_recently_closed_entries_forward(actions: RedditActions, fake_reddit: Any) -> None:
    fake_reddit.add_queue_item(FakeItem("open", created_utc=1.0))
    fake_reddit.add_queue_item(FakeItem("justdone", created_utc=2.0))
    poll(actions)
    actions.set_item_done_at(CHANNEL, "justdone", time.time())

    set_counter(actions, "modqueue", CHANNEL, RedditActions.QUEUE_NUM_MAX + 1)
    fake_reddit.clear_queue()
    fake_reddit.add_queue_item(FakeItem("new", created_utc=3.0))
    poll(actions)

    live = actions.get_modqueue_file()[CHANNEL]
    assert "open" in live, "an item a mod is still working on must not vanish"
    assert "justdone" in live, "the reconcile pass may still re-ask what happened to it"


def test_a_carried_over_number_is_not_handed_out_again(actions: RedditActions, fake_reddit: Any) -> None:
    """Two cards visible in the channel must never share a label."""
    set_counter(actions, "modqueue", CHANNEL, 1)
    fake_reddit.add_queue_item(FakeItem("stays_open", created_utc=1.0))
    poll(actions)                                   # takes #1, still in the queue

    set_counter(actions, "modqueue", CHANNEL, RedditActions.QUEUE_NUM_MAX + 1, cycle=1)
    fake_reddit.add_queue_item(FakeItem("new", created_utc=2.0))
    poll(actions)

    live = actions.get_modqueue_file()[CHANNEL]
    assert live["stays_open"]["queue_num"] == 1
    assert live["new"]["queue_num"] == 2, "1 is still on a card in the channel"


def test_no_rollover_before_the_cap(actions: RedditActions, fake_reddit: Any) -> None:
    set_counter(actions, "modqueue", CHANNEL, RedditActions.QUEUE_NUM_MAX)
    fake_reddit.add_queue_item(FakeItem("a1", created_utc=1.0))
    poll(actions)
    assert archives(actions) == []


# ---------------------------------------------------------------------------
# modmail rollover
# ---------------------------------------------------------------------------

def test_modmail_lettering_wraps_at_zz(actions: RedditActions, fake_reddit: Any) -> None:
    set_counter(actions, "modmail", MAIL_CHANNEL, RedditActions.CONV_NUM_MAX)
    fake_reddit._sub.modmail.all = [FakeConversation("c1")]
    fetch(actions)
    log = actions.get_modmail_file()[MAIL_CHANNEL]["modmail_conv"]
    assert RedditActions.conv_label(log["c1"]["conv_num"]) == "ZZ"

    actions.set_conv_done_at(MAIL_CHANNEL, "c1", time.time() - 30 * 24 * 3600)
    fake_reddit._sub.modmail.all = [FakeConversation("c2")]
    fetch(actions)

    log = actions.get_modmail_file()[MAIL_CHANNEL]["modmail_conv"]
    assert RedditActions.conv_label(log["c2"]["conv_num"]) == "A"
    assert "c1" not in log, "closed a month ago, so it went to the archive"
    assert len(archives(actions)) == 1


def test_modmail_rollover_keeps_an_open_conversation(actions: RedditActions, fake_reddit: Any) -> None:
    set_counter(actions, "modmail", MAIL_CHANNEL, RedditActions.CONV_NUM_MAX + 1)
    fake_reddit._sub.modmail.all = [FakeConversation("c1")]
    fetch(actions)

    log = actions.get_modmail_file()[MAIL_CHANNEL]["modmail_conv"]
    assert log["c1"]["conv_num"] == 1
    assert json.loads(archives(actions)[0].read_text())["kind"] == "modmail"


def test_modmail_and_modqueue_counters_are_independent(actions: RedditActions, fake_reddit: Any) -> None:
    fake_reddit.add_queue_item(FakeItem("a1", created_utc=1.0))
    poll(actions)
    fake_reddit._sub.modmail.all = [FakeConversation("c1")]
    fetch(actions)

    counters = actions.read_counters()
    assert counters["modqueue"][CHANNEL]["next"] == 2
    assert counters["modmail"][MAIL_CHANNEL]["next"] == 2
