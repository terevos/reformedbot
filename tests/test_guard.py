"""Channel/authorization guard and the lazy channel resolution behind it."""
from __future__ import annotations

from typing import Any, List, Optional

import pytest

import reformed_listener as L


def make_feed(subreddit: str = "reformed", queue_raw: Optional[str] = "mod_actions", mail_raw: Optional[str] = "mod_mail",
              queue_id: Optional[str] = None, mail_id: Optional[str] = None, mods: Optional[dict] = None) -> L.Feed:
    """Build a feed with its channels in whatever state the test needs."""
    feed = L.Feed(subreddit, queue_raw, mail_raw, mods)
    feed.modqueue_channel = queue_id
    feed.modmail_channel = mail_id
    return feed


def install(monkeypatch: pytest.MonkeyPatch, *feeds: L.Feed) -> List[L.Feed]:
    """Make *feeds* the listener's configured feeds."""
    monkeypatch.setattr(L, "feeds", list(feeds))
    return list(feeds)


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> L.Feed:
    """One feed, both channels configured and resolved — the normal state."""
    monkeypatch.setattr(L, "mod_slack_ids", {"U_MOD": "terevos2"})
    return install(monkeypatch, make_feed(queue_id="C_QUEUE", mail_id="C_MAIL"))[0]


@pytest.fixture
def unresolved(monkeypatch: pytest.MonkeyPatch) -> L.Feed:
    """Configured, but Slack was unreachable at startup."""
    monkeypatch.setattr(L, "mod_slack_ids", {"U_MOD": "terevos2"})
    monkeypatch.setattr(L, "_resolve_attempts", 0)
    return install(monkeypatch, make_feed())[0]


# ---------------------------------------------------------------------------
# _is_allowed_channel
# ---------------------------------------------------------------------------

def test_feed_channels_are_allowed(configured: L.Feed) -> None:
    assert L._is_allowed_channel("C_QUEUE")
    assert L._is_allowed_channel("C_MAIL")


def test_other_channels_are_rejected(configured: L.Feed) -> None:
    """A message left in a de-configured channel keeps working buttons."""
    assert not L._is_allowed_channel("C_RANDOM")


def test_every_feeds_channels_are_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch,
            make_feed(queue_id="C_QUEUE", mail_id="C_MAIL"),
            make_feed("whatcouldgowrong", "wcgw_reports", "wcgw_mail", "C_WCGW", "C_WCGW_MAIL"))
    assert L._is_allowed_channel("C_WCGW")
    assert L._is_allowed_channel("C_QUEUE")
    assert not L._is_allowed_channel("C_RANDOM")


def test_no_channels_configured_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, make_feed(queue_raw=None, mail_raw=None))
    assert L._is_allowed_channel("C_ANYTHING")


def test_unresolved_channels_fail_open(unresolved: L.Feed) -> None:
    """The allow-list is not known to be complete, so do not block buttons."""
    assert L._is_allowed_channel("C_ANYTHING")


def test_one_unresolved_channel_still_fails_open(monkeypatch: pytest.MonkeyPatch, configured: L.Feed) -> None:
    configured.modmail_channel = None
    assert L._is_allowed_channel("C_RANDOM")


# ---------------------------------------------------------------------------
# _feed_for_channel / _feed_for_action
# ---------------------------------------------------------------------------

def test_a_channel_resolves_to_its_own_feed(monkeypatch: pytest.MonkeyPatch) -> None:
    reformed, wcgw = install(monkeypatch,
                             make_feed(queue_id="C_QUEUE", mail_id="C_MAIL"),
                             make_feed("whatcouldgowrong", "wcgw_reports", "wcgw_mail", "C_WCGW", "C_WCGW_MAIL"))
    assert L._feed_for_channel("C_MAIL") is reformed
    assert L._feed_for_channel("C_WCGW") is wcgw
    assert L._feed_for_channel("C_RANDOM") is None


def test_a_lone_feed_claims_an_unattributable_channel(unresolved: L.Feed) -> None:
    """With one subreddit configured there is nothing else the message can be."""
    assert L._feed_for_action("C_ANYTHING") is unresolved


def test_several_feeds_do_not_guess(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guessing could take a Reddit action against the wrong subreddit."""
    install(monkeypatch, make_feed(), make_feed("whatcouldgowrong", "wcgw_reports", "wcgw_mail"))
    assert L._feed_for_action("C_ANYTHING") is None


# ---------------------------------------------------------------------------
# _interaction_allowed
# ---------------------------------------------------------------------------

def test_mod_in_a_feed_channel_is_allowed(configured: L.Feed, slack: Any) -> None:
    assert L._interaction_allowed(slack, "C_QUEUE", "U_MOD") is configured
    assert slack.ephemeral == []


def test_wrong_channel_is_rejected_with_a_private_notice(configured: L.Feed, slack: Any) -> None:
    assert L._interaction_allowed(slack, "C_RANDOM", "U_MOD") is None
    assert "not a configured mod feed" in slack.ephemeral[0]["text"]
    assert slack.ephemeral[0]["user"] == "U_MOD", "only the clicker sees it"


def test_non_mod_is_rejected(configured: L.Feed, slack: Any) -> None:
    assert L._interaction_allowed(slack, "C_QUEUE", "U_STRANGER") is None
    assert "not authorized" in slack.ephemeral[0]["text"]


def test_rejection_verb_is_per_handler(configured: L.Feed, slack: Any) -> None:
    L._interaction_allowed(slack, "C_QUEUE", "U_STRANGER", verb="vote")
    assert slack.ephemeral[0]["text"] == "You are not authorized to vote."


def test_mod_ids_are_matched_case_insensitively(monkeypatch: pytest.MonkeyPatch, configured: L.Feed, slack: Any) -> None:
    monkeypatch.setattr(L, "mod_slack_ids", {"U_MOD": "terevos2"})
    assert L._interaction_allowed(slack, "C_QUEUE", "u_mod") is configured


def test_channel_is_checked_before_authorization(configured: L.Feed, slack: Any) -> None:
    """A stranger in the wrong channel hears about the channel, not their status."""
    L._interaction_allowed(slack, "C_RANDOM", "U_STRANGER")
    assert "not a configured mod feed" in slack.ephemeral[0]["text"]


def test_an_unattributable_channel_is_held_off(monkeypatch: pytest.MonkeyPatch, slack: Any) -> None:
    """Several feeds, none resolved: the guard fails open but the feed is unknown."""
    monkeypatch.setattr(L, "mod_slack_ids", {"U_MOD": "terevos2"})
    install(monkeypatch, make_feed(), make_feed("whatcouldgowrong", "wcgw_reports", "wcgw_mail"))
    assert L._interaction_allowed(slack, "C_ANYTHING", "U_MOD") is None
    assert "still starting up" in slack.ephemeral[0]["text"]


# ---------------------------------------------------------------------------
# per-subreddit moderators
# ---------------------------------------------------------------------------

def test_global_mods_can_act_in_every_feed(monkeypatch: pytest.MonkeyPatch, slack: Any) -> None:
    monkeypatch.setattr(L, "mod_slack_ids", {"U_MOD": "terevos2"})
    _, wcgw = install(monkeypatch,
                      make_feed(queue_id="C_QUEUE", mail_id="C_MAIL"),
                      make_feed("whatcouldgowrong", "wcgw_reports", "wcgw_mail", "C_WCGW", "C_WCGW_MAIL"))
    assert L._interaction_allowed(slack, "C_WCGW", "U_MOD") is wcgw


def test_a_subreddit_mod_may_act_only_on_that_subreddit(monkeypatch: pytest.MonkeyPatch, slack: Any) -> None:
    monkeypatch.setattr(L, "mod_slack_ids", {})
    _, wcgw = install(monkeypatch,
                      make_feed(queue_id="C_QUEUE", mail_id="C_MAIL"),
                      make_feed("whatcouldgowrong", "wcgw_reports", "wcgw_mail", "C_WCGW", "C_WCGW_MAIL",
                                mods={"U_WCGW": "wcgw_mod"}))
    assert L._interaction_allowed(slack, "C_WCGW", "U_WCGW") is wcgw
    assert L._interaction_allowed(slack, "C_QUEUE", "U_WCGW") is None
    assert "not authorized" in slack.ephemeral[-1]["text"]


def test_the_feeds_own_name_for_a_mod_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """A mod with a different Reddit account per subreddit is credited correctly."""
    monkeypatch.setattr(L, "mod_slack_ids", {"U_MOD": "terevos2"})
    feed = make_feed("whatcouldgowrong", mods={"U_MOD": "terevos_wcgw"})
    assert L._mod_display_name("U_MOD", feed) == "terevos_wcgw"
    assert L._mod_display_name("U_MOD", None) == "terevos2"
    assert L._mod_display_name("U_NOBODY", feed) == "<@U_NOBODY>"


# ---------------------------------------------------------------------------
# lazy resolution
# ---------------------------------------------------------------------------

def test_pending_lists_configured_but_unresolved(unresolved: L.Feed) -> None:
    assert L._pending_channels() == ["r/reformed MODQUEUE_CHANNEL", "r/reformed MODMAIL_CHANNEL"]


def test_pending_names_the_subreddit_of_each_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch,
            make_feed(queue_id="C_QUEUE", mail_id="C_MAIL"),
            make_feed("whatcouldgowrong", "wcgw_reports", "wcgw_mail"))
    assert L._pending_channels() == ["r/whatcouldgowrong MODQUEUE_CHANNEL", "r/whatcouldgowrong MODMAIL_CHANNEL"]


def test_nothing_pending_once_resolved(configured: L.Feed) -> None:
    assert L._pending_channels() == []


def test_unconfigured_channel_is_not_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, make_feed(queue_raw=None, mail_raw=None))
    assert L._pending_channels() == []


def test_retry_resolves_once_slack_returns(monkeypatch: pytest.MonkeyPatch, unresolved: L.Feed) -> None:
    """A bot started while Slack was down recovers without a restart."""
    slack_up = {"value": False}

    def fake_resolve(raw: Optional[str], token: str) -> Optional[str]:
        """Resolve names only once Slack is deemed reachable."""
        if not slack_up["value"]:
            return None
        return {"mod_actions": "C_QUEUE", "mod_mail": "C_MAIL"}.get((raw or "").strip())

    monkeypatch.setattr(L, "_resolve_channel", fake_resolve)

    L._retry_unresolved_channels()
    assert unresolved.modqueue_channel is None, "still down"

    slack_up["value"] = True
    L._retry_unresolved_channels()

    assert (unresolved.modqueue_channel, unresolved.modmail_channel) == ("C_QUEUE", "C_MAIL")
    assert L._pending_channels() == []


def test_retry_covers_every_feed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(L, "_resolve_attempts", 0)
    reformed, wcgw = install(monkeypatch,
                             make_feed(),
                             make_feed("whatcouldgowrong", "wcgw_reports", "wcgw_mail"))
    monkeypatch.setattr(L, "_resolve_channel", lambda raw, token: {
        "mod_actions": "C_QUEUE", "mod_mail": "C_MAIL",
        "wcgw_reports": "C_WCGW", "wcgw_mail": "C_WCGW_MAIL",
    }[raw])

    L._retry_unresolved_channels()

    assert reformed.channels() == ["C_QUEUE", "C_MAIL"]
    assert wcgw.channels() == ["C_WCGW", "C_WCGW_MAIL"]


def test_guard_becomes_strict_after_recovery(monkeypatch: pytest.MonkeyPatch, unresolved: L.Feed) -> None:
    monkeypatch.setattr(L, "_resolve_channel", lambda raw, token: {"mod_actions": "C_QUEUE", "mod_mail": "C_MAIL"}[raw])
    assert L._is_allowed_channel("C_RANDOM") is True, "permissive while unresolved"
    L._retry_unresolved_channels()
    assert L._is_allowed_channel("C_RANDOM") is False, "strict once resolved"


def test_retry_makes_no_calls_when_nothing_is_pending(monkeypatch: pytest.MonkeyPatch, configured: L.Feed) -> None:
    def explode(raw: Any, token: Any) -> None:
        """Fail the test if channel resolution is attempted."""
        raise AssertionError("_resolve_channel must not be called when nothing is pending")

    monkeypatch.setattr(L, "_resolve_channel", explode)
    L._retry_unresolved_channels()   # must not raise
