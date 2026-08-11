"""Channel/authorization guard and the lazy channel resolution behind it."""
from __future__ import annotations

from typing import Any, Optional

import pytest

import reformed_listener as L


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both feeds configured and resolved — the normal running state."""
    monkeypatch.setattr(L, "_raw_modqueue_channel", "mod_actions")
    monkeypatch.setattr(L, "_raw_modmail_channel", "mod_mail")
    monkeypatch.setattr(L, "modqueue_channel", "C_QUEUE")
    monkeypatch.setattr(L, "modmail_channel", "C_MAIL")
    monkeypatch.setattr(L, "mod_slack_ids", {"U_MOD": "terevos2"})


@pytest.fixture
def unresolved(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configured, but Slack was unreachable at startup."""
    monkeypatch.setattr(L, "_raw_modqueue_channel", "mod_actions")
    monkeypatch.setattr(L, "_raw_modmail_channel", "mod_mail")
    monkeypatch.setattr(L, "modqueue_channel", None)
    monkeypatch.setattr(L, "modmail_channel", None)
    monkeypatch.setattr(L, "mod_slack_ids", {"U_MOD": "terevos2"})
    monkeypatch.setattr(L, "_resolve_attempts", 0)


# ---------------------------------------------------------------------------
# _is_allowed_channel
# ---------------------------------------------------------------------------

def test_feed_channels_are_allowed(configured: None) -> None:
    assert L._is_allowed_channel("C_QUEUE")
    assert L._is_allowed_channel("C_MAIL")


def test_other_channels_are_rejected(configured: None) -> None:
    """A message left in a de-configured channel keeps working buttons."""
    assert not L._is_allowed_channel("C_RANDOM")


def test_no_channels_configured_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(L, "_raw_modqueue_channel", None)
    monkeypatch.setattr(L, "_raw_modmail_channel", None)
    monkeypatch.setattr(L, "modqueue_channel", None)
    monkeypatch.setattr(L, "modmail_channel", None)
    assert L._is_allowed_channel("C_ANYTHING")


def test_unresolved_channels_fail_open(unresolved: None) -> None:
    """The allow-list is not known to be complete, so do not block buttons."""
    assert L._is_allowed_channel("C_ANYTHING")


def test_one_unresolved_channel_still_fails_open(monkeypatch: pytest.MonkeyPatch, configured: None) -> None:
    monkeypatch.setattr(L, "modmail_channel", None)
    assert L._is_allowed_channel("C_RANDOM")


# ---------------------------------------------------------------------------
# _interaction_allowed
# ---------------------------------------------------------------------------

def test_mod_in_a_feed_channel_is_allowed(configured: None, slack: Any) -> None:
    assert L._interaction_allowed(slack, "C_QUEUE", "U_MOD") is True
    assert slack.ephemeral == []


def test_wrong_channel_is_rejected_with_a_private_notice(configured: None, slack: Any) -> None:
    assert L._interaction_allowed(slack, "C_RANDOM", "U_MOD") is False
    assert "not a configured mod feed" in slack.ephemeral[0]["text"]
    assert slack.ephemeral[0]["user"] == "U_MOD", "only the clicker sees it"


def test_non_mod_is_rejected(configured: None, slack: Any) -> None:
    assert L._interaction_allowed(slack, "C_QUEUE", "U_STRANGER") is False
    assert "not authorized" in slack.ephemeral[0]["text"]


def test_rejection_verb_is_per_handler(configured: None, slack: Any) -> None:
    L._interaction_allowed(slack, "C_QUEUE", "U_STRANGER", verb="vote")
    assert slack.ephemeral[0]["text"] == "You are not authorized to vote."


def test_mod_ids_are_matched_case_insensitively(monkeypatch: pytest.MonkeyPatch, configured: None, slack: Any) -> None:
    monkeypatch.setattr(L, "mod_slack_ids", {"U_MOD": "terevos2"})
    assert L._interaction_allowed(slack, "C_QUEUE", "u_mod") is True


def test_channel_is_checked_before_authorization(configured: None, slack: Any) -> None:
    """A stranger in the wrong channel hears about the channel, not their status."""
    L._interaction_allowed(slack, "C_RANDOM", "U_STRANGER")
    assert "not a configured mod feed" in slack.ephemeral[0]["text"]


# ---------------------------------------------------------------------------
# lazy resolution
# ---------------------------------------------------------------------------

def test_pending_lists_configured_but_unresolved(unresolved: None) -> None:
    assert L._pending_channels() == ["MODQUEUE_CHANNEL", "MODMAIL_CHANNEL"]


def test_nothing_pending_once_resolved(configured: None) -> None:
    assert L._pending_channels() == []


def test_unconfigured_channel_is_not_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(L, "_raw_modqueue_channel", None)
    monkeypatch.setattr(L, "_raw_modmail_channel", None)
    monkeypatch.setattr(L, "modqueue_channel", None)
    monkeypatch.setattr(L, "modmail_channel", None)
    assert L._pending_channels() == []


def test_retry_resolves_once_slack_returns(monkeypatch: pytest.MonkeyPatch, unresolved: None) -> None:
    """A bot started while Slack was down recovers without a restart."""
    slack_up = {"value": False}

    def fake_resolve(raw: Optional[str], token: str) -> Optional[str]:
        """Resolve names only once Slack is deemed reachable."""
        if not slack_up["value"]:
            return None
        return {"mod_actions": "C_QUEUE", "mod_mail": "C_MAIL"}.get((raw or "").strip())

    monkeypatch.setattr(L, "_resolve_channel", fake_resolve)

    L._retry_unresolved_channels()
    assert L.modqueue_channel is None, "still down"

    slack_up["value"] = True
    L._retry_unresolved_channels()

    assert (L.modqueue_channel, L.modmail_channel) == ("C_QUEUE", "C_MAIL")
    assert L._pending_channels() == []


def test_guard_becomes_strict_after_recovery(monkeypatch: pytest.MonkeyPatch, unresolved: None) -> None:
    monkeypatch.setattr(L, "_resolve_channel", lambda raw, token: {"mod_actions": "C_QUEUE", "mod_mail": "C_MAIL"}[raw])
    assert L._is_allowed_channel("C_RANDOM") is True, "permissive while unresolved"
    L._retry_unresolved_channels()
    assert L._is_allowed_channel("C_RANDOM") is False, "strict once resolved"


def test_retry_makes_no_calls_when_nothing_is_pending(monkeypatch: pytest.MonkeyPatch, configured: None) -> None:
    def explode(raw: Any, token: Any) -> None:
        """Fail the test if channel resolution is attempted."""
        raise AssertionError("_resolve_channel must not be called when nothing is pending")

    monkeypatch.setattr(L, "_resolve_channel", explode)
    L._retry_unresolved_channels()   # must not raise
