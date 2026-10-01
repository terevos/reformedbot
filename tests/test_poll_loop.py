"""One pass of the background poll loop, plus the startup helpers around it."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

import reformed_listener as L
from conftest import CHANNEL, MAIL_CHANNEL, FakeConversation, FakeItem, FakeModAction
from reddit_actions import RedditActions


class StopLoop(Exception):
    """Raised from the patched sleep to end the loop after one pass."""


@pytest.fixture
def loop(monkeypatch: pytest.MonkeyPatch, feed: Any, actions: RedditActions, slack: Any) -> Any:
    """Wire the listener to fakes and make the poll loop run exactly one pass."""
    monkeypatch.setattr(L, "_last_digest_slot", "already-fired")
    monkeypatch.setattr(L, "SlackWebClient", lambda token: slack)
    monkeypatch.setattr(L.config, "get", lambda *a, **k: "30")

    slept: List[float] = []

    def stop(seconds: float) -> None:
        """Record the requested delay, then end the loop."""
        slept.append(seconds)
        raise StopLoop

    monkeypatch.setattr(L.time, "sleep", stop)
    return actions, slack, slept


def run_one_pass() -> None:
    """Run the loop until the patched sleep stops it."""
    with pytest.raises(StopLoop):
        L._poll_loop()


# ---------------------------------------------------------------------------
# posting
# ---------------------------------------------------------------------------

def test_a_new_modqueue_item_is_posted_and_recorded(loop: Any, fake_reddit: Any) -> None:
    actions, slack, _ = loop
    fake_reddit.add_queue_item(FakeItem("a1", created_utc=1.0))

    run_one_pass()

    posted = slack.cards(CHANNEL)
    assert len(posted) == 1
    entry = actions.get_item_info(CHANNEL, "a1")
    assert entry["slack_ts"] == posted[0]["ts"]
    assert entry["slack_permalink"].startswith("https://slack.test/")
    assert entry["slack_blocks"], "blocks are cached for later rebuilds"


def test_an_item_is_not_posted_twice(loop: Any, fake_reddit: Any) -> None:
    _, slack, _ = loop
    fake_reddit.add_queue_item(FakeItem("a1", created_utc=1.0))

    run_one_pass()
    before = len(slack.posted)
    run_one_pass()

    new_item_posts = slack.cards(since=before)
    assert new_item_posts == []


def test_a_new_modmail_message_is_posted_and_threaded(loop: Any, fake_reddit: Any) -> None:
    actions, slack, _ = loop
    fake_reddit._sub.modmail.all = [FakeConversation("c1")]

    run_one_pass()

    posted = slack.cards(MAIL_CHANNEL)
    assert len(posted) == 1
    assert actions.get_modmail_file()[MAIL_CHANNEL]["modmail_conv"]["c1"]["slack_ts"] == posted[0]["ts"]


def test_archived_on_reddit_is_reflected_in_slack(loop: Any, fake_reddit: Any) -> None:
    actions, slack, _ = loop
    actions.write_modmail_file({MAIL_CHANNEL: {"modmail_conv": {
        "c1": {"slack_ts": "1.0", "conv_num": 1, "subject": "s", "author": "a"},
    }}})
    slack.seed_message("1.0", [{"type": "section", "text": {"type": "mrkdwn", "text": "d"}}])
    fake_reddit._sub.modmail.by_state["archived"] = [
        FakeConversation("c1", mod_actions=[FakeModAction(RedditActions._ACTION_ARCHIVED, "terevos2", "2026-07-29T10:00")]),
    ]

    run_one_pass()

    assert any("Archived on Reddit by" in p["text"] for p in slack.posted)
    assert RedditActions.is_done(actions.get_modmail_file()[MAIL_CHANNEL]["modmail_conv"]["c1"])


def test_a_resolved_item_is_auto_marked_done(loop: Any, fake_reddit: Any) -> None:
    actions, slack, _ = loop
    fake_reddit.add_queue_item(FakeItem("a1", created_utc=1.0))
    run_one_pass()

    fake_reddit.clear_queue()      # actioned on Reddit between polls
    run_one_pass()

    assert actions.get_item_info(CHANNEL, "a1")["done_at"] is not None


def test_an_empty_queue_posts_the_all_clear(loop: Any) -> None:
    _, slack, _ = loop
    run_one_pass()
    assert any("Mod queue is clear" in p["text"] for p in slack.posted)


# ---------------------------------------------------------------------------
# pacing
# ---------------------------------------------------------------------------

def test_a_healthy_pass_sleeps_the_poll_interval(loop: Any) -> None:
    _, _, slept = loop
    run_one_pass()
    assert slept == [30]


def test_a_reddit_500_retries_on_the_shorter_delay(loop: Any, fake_reddit: Any) -> None:
    _, _, slept = loop

    class ServerError(Exception):
        """Mimics prawcore.exceptions.ServerError."""

    def boom(*args: Any, **kwargs: Any) -> None:
        """Fail the way Reddit does when it returns a 500."""
        raise ServerError("reddit 500")

    fake_reddit._sub.modqueue = boom

    run_one_pass()

    assert slept == [15], "a transient 5xx retries sooner than a normal poll"


def test_an_ordinary_error_does_not_trigger_backoff(loop: Any, fake_reddit: Any) -> None:
    _, _, slept = loop

    def boom(*args: Any, **kwargs: Any) -> None:
        """Fail with an ordinary error, not an upstream 5xx."""
        raise ValueError("something mundane")

    fake_reddit._sub.modqueue = boom

    run_one_pass()

    assert slept == [30]


def test_one_failing_feed_does_not_stop_the_other(loop: Any, fake_reddit: Any) -> None:
    """Each section has its own try/except so modmail survives a modqueue fault."""
    actions, slack, _ = loop

    def boom(*args: Any, **kwargs: Any) -> None:
        """Break only the modqueue half of the poll."""
        raise ValueError("modqueue is broken")

    fake_reddit._sub.modqueue = boom
    fake_reddit._sub.modmail.all = [FakeConversation("c1")]

    run_one_pass()

    assert slack.cards(MAIL_CHANNEL)


def test_unresolved_channels_are_retried_each_pass(monkeypatch: pytest.MonkeyPatch, loop: Any, feed: Any) -> None:
    feed.modqueue_channel = None
    calls: List[str] = []

    def fake_resolve(raw: Optional[str], token: str) -> Optional[str]:
        """Record the lookup and resolve to the test channel."""
        calls.append(raw or "")
        return CHANNEL

    monkeypatch.setattr(L, "_resolve_channel", fake_resolve)

    run_one_pass()

    assert "mod_actions" in calls
    assert feed.modqueue_channel == CHANNEL


# ---------------------------------------------------------------------------
# _resolve_channel
# ---------------------------------------------------------------------------

class ListingSlack:
    """Serves conversations_list pages for channel-name lookups."""

    def __init__(self, pages: List[Dict[str, Any]]) -> None:
        """Serve *pages* from conversations_list, one call at a time."""
        self.pages = pages
        self.calls = 0

    def conversations_list(self, **kwargs: Any) -> Dict[str, Any]:
        """Return the next prepared page."""
        page = self.pages[self.calls]
        self.calls += 1
        return page


def use_listing(monkeypatch: pytest.MonkeyPatch, client: Any) -> None:
    """Point the listener's Slack client at *client*."""
    monkeypatch.setattr(L, "SlackWebClient", lambda token: client)


def test_a_channel_id_is_used_as_is(monkeypatch: pytest.MonkeyPatch) -> None:
    """An ID needs no lookup, so this path never touches the network."""
    def explode(token: str) -> None:
        """Fail the test if Slack is contacted."""
        raise AssertionError("should not call Slack for an ID")

    monkeypatch.setattr(L, "SlackWebClient", explode)
    assert L._resolve_channel("C0ARRHHT8M7", "tok") == "C0ARRHHT8M7"


def test_a_channel_name_is_looked_up(monkeypatch: pytest.MonkeyPatch) -> None:
    use_listing(monkeypatch, ListingSlack([{"channels": [{"name": "mod_actions", "id": "C_QUEUE"}]}]))
    assert L._resolve_channel("mod_actions", "tok") == "C_QUEUE"


def test_a_leading_hash_is_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    use_listing(monkeypatch, ListingSlack([{"channels": [{"name": "mod_actions", "id": "C_QUEUE"}]}]))
    assert L._resolve_channel("#mod_actions", "tok") == "C_QUEUE"


def test_lookup_follows_pagination(monkeypatch: pytest.MonkeyPatch) -> None:
    use_listing(monkeypatch, ListingSlack([
        {"channels": [{"name": "other", "id": "C_X"}], "response_metadata": {"next_cursor": "abc"}},
        {"channels": [{"name": "mod_actions", "id": "C_QUEUE"}]},
    ]))
    assert L._resolve_channel("mod_actions", "tok") == "C_QUEUE"


def test_an_unknown_channel_resolves_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    use_listing(monkeypatch, ListingSlack([{"channels": [{"name": "other", "id": "C_X"}]}]))
    assert L._resolve_channel("mod_actions", "tok") is None


def test_an_empty_value_resolves_to_none() -> None:
    assert L._resolve_channel(None, "tok") is None
    assert L._resolve_channel("   ", "tok") is None


def test_a_slack_failure_resolves_to_none_rather_than_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    """Startup must survive an unreachable Slack; the poll loop retries."""
    class Broken:
        """A Slack client that cannot reach the API."""
        def conversations_list(self, **kwargs: Any) -> Dict[str, Any]:
            """Fail the way an unreachable Slack does."""
            raise ConnectionError("slack unreachable")

    use_listing(monkeypatch, Broken())
    assert L._resolve_channel("mod_actions", "tok") is None


# ---------------------------------------------------------------------------
# misc helpers
# ---------------------------------------------------------------------------

def test_item_id_is_read_back_from_posted_blocks(actions: RedditActions) -> None:
    blocks = actions._build_modqueue_blocks(
        item_id="a1", author="someone", report_link="http://r", item_type="submission",
        content="body", user_reports=[], mod_reports=[], queue_num=1,
    )
    assert L._item_id_from_blocks(blocks) == "a1"


def test_item_id_from_unrelated_blocks_is_none() -> None:
    assert L._item_id_from_blocks([{"type": "divider"}]) is None


def test_reddit_user_link_renders_a_profile_link() -> None:
    assert "terevos2" in L._reddit_user_link("terevos2")


def test_reddit_user_link_of_nobody_is_harmless() -> None:
    assert isinstance(L._reddit_user_link(""), str)
