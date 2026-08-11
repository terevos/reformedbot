"""Shared fixtures and fakes for the ReformedBot test suite.

Nothing here touches the network, the real ``logs/`` directory, or the real
``slack.ini`` / ``praw.ini``. Reddit and Slack are both replaced by in-memory
fakes, and every ``RedditActions`` instance is pointed at a ``tmp_path``.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from praw.models.reddit.comment import Comment  # noqa: E402
from praw.models.reddit.submission import Submission  # noqa: E402

from reddit_actions import RedditActions  # noqa: E402

# PRAW models want a Reddit instance; ours never make a request because every
# attribute the code reads is pre-populated, so a bare sentinel suffices.
_UNUSED_REDDIT = object()


# ---------------------------------------------------------------------------
# Fake Slack
# ---------------------------------------------------------------------------

class FakeSlackClient:
    """Records Slack calls and serves canned message history.

    Only the handful of methods the bot actually uses are implemented; anything
    else raises, so a test that exercises an unexpected call path fails loudly
    instead of silently passing.
    """

    def __init__(self) -> None:
        """Start with empty call logs and no seeded history."""
        self.posted: List[Dict[str, Any]] = []
        self.updated: List[Dict[str, Any]] = []
        self.deleted: List[Dict[str, Any]] = []
        self.ephemeral: List[Dict[str, Any]] = []
        self.history: Dict[str, List[Dict[str, Any]]] = {}  # ts -> blocks
        self.permalinks: Dict[str, str] = {}
        self.fail_with: Optional[Exception] = None
        self._ts_counter = 1000.0

    # -- outbound ----------------------------------------------------------
    def chat_postMessage(self, channel: str, text: str = "", blocks: Any = None, thread_ts: Optional[str] = None) -> Dict[str, Any]:
        """Record a post and hand back a synthetic, monotonically increasing ts."""
        if self.fail_with:
            raise self.fail_with
        self._ts_counter += 1
        ts = f"{self._ts_counter:.6f}"
        self.posted.append({"channel": channel, "text": text, "blocks": blocks, "thread_ts": thread_ts, "ts": ts})
        if blocks:
            self.history[ts] = blocks
        return {"ts": ts, "ok": True}

    def chat_update(self, channel: str, ts: str, text: str = "", blocks: Any = None) -> Dict[str, Any]:
        """Record an edit and replace the stored blocks for that message."""
        if self.fail_with:
            raise self.fail_with
        self.updated.append({"channel": channel, "ts": ts, "text": text, "blocks": blocks})
        if blocks is not None:
            self.history[ts] = blocks
        return {"ok": True}

    def chat_postEphemeral(self, channel: str, user: str, text: str) -> Dict[str, Any]:
        """Record a message visible only to one user."""
        self.ephemeral.append({"channel": channel, "user": user, "text": text})
        return {"ok": True}

    def chat_getPermalink(self, channel: str, message_ts: str) -> Dict[str, Any]:
        """Return a stable fake permalink for a message."""
        return {"permalink": self.permalinks.get(message_ts, f"https://slack.test/{channel}/{message_ts}")}

    def chat_delete(self, channel: str, ts: str) -> Dict[str, Any]:
        """Record a deletion and drop the message from the channel's history."""
        if self.fail_with:
            raise self.fail_with
        self.deleted.append({"channel": channel, "ts": ts})
        self.history.pop(ts, None)
        return {"ok": True}

    # -- inbound -----------------------------------------------------------
    def conversations_history(self, channel: str, latest: Optional[str] = None, inclusive: bool = True, limit: int = 1) -> Dict[str, Any]:
        """Serve the blocks previously posted or seeded at *latest*.

        With no *latest*, serve the newest message posted to *channel* instead —
        that is the "is the status message still at the bottom?" lookup, which
        needs the ts rather than the blocks.
        """
        if latest is None:
            gone = {d["ts"] for d in self.deleted}
            in_channel = [
                p for p in self.posted
                if p["channel"] == channel and p["thread_ts"] is None and p["ts"] not in gone
            ]
            if not in_channel:
                return {"messages": []}
            newest = max(in_channel, key=lambda p: float(p["ts"]))
            return {"messages": [{"ts": newest["ts"], "text": newest["text"], "blocks": newest["blocks"]}]}
        blocks = self.history.get(latest)
        return {"messages": [{"blocks": blocks}] if blocks is not None else []}

    def seed_message(self, ts: str, blocks: List[Dict[str, Any]]) -> None:
        """Pretend a message with *blocks* already exists at *ts*."""
        self.history[ts] = blocks

    # -- helpers -----------------------------------------------------------
    def last_update(self) -> Dict[str, Any]:
        """Return the most recent chat_update, asserting one happened."""
        assert self.updated, "expected a chat_update but none was made"
        return self.updated[-1]

    def texts(self) -> List[str]:
        """Return the fallback text of every posted message, in order."""
        return [p["text"] for p in self.posted]


# ---------------------------------------------------------------------------
# Fake PRAW
# ---------------------------------------------------------------------------

class FakeRedditor:
    """A Reddit user, which PRAW exposes as an object with a ``name``."""
    def __init__(self, name: str) -> None:
        """Store the username."""
        self.name = name

    def __str__(self) -> str:
        """PRAW code often stringifies an author directly."""
        return self.name


def FakeItem(id: str, author: str = "someuser", kind: str = "submission",
             title: str = "A post", body: str = "a comment", url: str = "https://reddit.test/x",
             user_reports: Optional[List[Any]] = None, mod_reports: Optional[List[Any]] = None,
             created_utc: float = 0.0, approved: bool = False, removed: bool = False,
             approved_by: Any = None, banned_by: Any = None, removed_by: Any = None) -> Any:
    """Build a real PRAW ``Submission``/``Comment`` with its fields pre-filled.

    ``get_modqueue`` branches on ``isinstance(item, Submission/Comment)``, so a
    duck-typed stand-in would silently fall through to the "Unknown" branch and
    the test would prove nothing. Constructing the genuine model class and
    stuffing ``__dict__`` gives correct isinstance behaviour.

    ``_fetched=True`` matters: without it PRAW treats any attribute the object
    lacks as a cue to load it from Reddit, which recurses forever against a
    sentinel Reddit. Marked fetched, a missing attribute raises AttributeError
    the way ``getattr(item, name, default)`` in the production code expects.
    """
    cls = Comment if kind == "comment" else Submission
    item = cls(_UNUSED_REDDIT, id=id)
    item.__dict__.update(
        _fetched=True,
        id=id,
        author=FakeRedditor(author) if author else None,
        title=title,
        body=body,
        url=url,
        permalink=f"/r/reformed/comments/{id}",
        user_reports=user_reports or [],
        mod_reports=mod_reports or [],
        created_utc=created_utc,
        created=created_utc,
        edited=False,
        approved=approved,
        removed=removed,
        approved_by=approved_by,
        banned_by=banned_by,
        removed_by=removed_by,
    )
    return item


class FakeModmailMessage:
    """One message inside a modmail conversation."""
    def __init__(self, id: str, author: str, body: str = "hello", date: str = "2026-07-29T12:00:00") -> None:
        """Build a message, defaulting to a plain user note."""
        self.id = id
        self.author = FakeRedditor(author) if author else None
        self.body_markdown = body
        self.body = body
        self.date = date


class FakeModAction:
    """An entry in a conversation's ``mod_actions`` (archive, unarchive, ...)."""
    def __init__(self, action_type_id: int, author: str, date: str) -> None:
        """Record the action type, who did it, and when."""
        self.action_type_id = action_type_id
        self.author = author
        self.date = date


class FakeConversation:
    """Stands in for a PRAW ``ModmailConversation``."""

    def __init__(self, id: str, subject: str = "A question", messages: Optional[List[Any]] = None,
                 is_auto: bool = False, mod_actions: Optional[List[Any]] = None) -> None:
        """Build a conversation with one user message unless told otherwise."""
        self.id = id
        self.subject = subject
        self.messages = messages or [FakeModmailMessage(f"{id}m1", "someuser")]
        self.is_auto = is_auto
        self.mod_actions = mod_actions or []


class FakeModmail:
    """``subreddit.modmail`` — conversations, optionally filtered by state."""

    def __init__(self) -> None:
        """Start with no conversations in any state."""
        self.by_state: Dict[str, List[FakeConversation]] = {}
        self.all: List[FakeConversation] = []

    def conversations(self, state: Optional[str] = None, limit: Optional[int] = None) -> List[FakeConversation]:
        """Return every conversation, or only those in *state*."""
        if state is None:
            return list(self.all)
        return list(self.by_state.get(state, []))


class FakeSubreddit:
    """A subreddit exposing the ``mod`` and ``modmail`` surfaces the bot uses."""
    def __init__(self) -> None:
        """Start with an empty modqueue and empty modmail."""
        self.modqueue_items: List[Any] = []
        self.mod = self          # sub.mod.modqueue()
        self.modmail = FakeModmail()

    def modqueue(self, limit: Optional[int] = None) -> List[Any]:
        """Return a copy so callers cannot mutate the queue by iterating it."""
        return list(self.modqueue_items)


class FakeReddit:
    """Minimal stand-in for ``praw.Reddit``."""

    def __init__(self) -> None:
        """Start with one subreddit and no fetchable items."""
        self._sub = FakeSubreddit()
        self.items: Dict[str, Any] = {}

    def subreddit(self, name: str) -> FakeSubreddit:
        """Return the single fake subreddit, whatever name is asked for."""
        return self._sub

    def submission(self, id: str) -> Any:
        """Fetch a submission, raising for an unknown id as PRAW would."""
        if id not in self.items:
            raise KeyError(f"no such submission {id}")
        return self.items[id]

    def comment(self, id: str) -> Any:
        """Fetch a comment, raising for an unknown id as PRAW would."""
        if id not in self.items:
            raise KeyError(f"no such comment {id}")
        return self.items[id]

    # -- helpers -----------------------------------------------------------
    def add_queue_item(self, item: Any) -> Any:
        """Put an item in the modqueue and make it fetchable by id."""
        self._sub.modqueue_items.append(item)
        self.items[item.id] = item
        return item

    def clear_queue(self) -> None:
        """Empty the modqueue, as if every item had been actioned."""
        self._sub.modqueue_items.clear()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

CHANNEL = "C_QUEUE"
MAIL_CHANNEL = "C_MAIL"


@pytest.fixture
def fake_reddit() -> FakeReddit:
    """A Reddit stand-in with an empty modqueue and modmail."""
    return FakeReddit()


@pytest.fixture
def slack() -> FakeSlackClient:
    """A Slack stand-in that records every call it receives."""
    return FakeSlackClient()


@pytest.fixture
def actions(fake_reddit: FakeReddit, tmp_path: Path) -> RedditActions:
    """A RedditActions wired to fakes, with its logs in a temp directory."""
    return RedditActions("reformed", reddit=fake_reddit, log_dir=str(tmp_path / "logs"))
