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

import reformed_listener as L  # noqa: E402
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
        """Record a post and hand back a synthetic, monotonically increasing ts.

        ``bot_id`` is set the way Slack sets it on anything posted with a bot
        token: the status-message adoption that runs after a restart uses it to
        tell the bot's own messages from a human's.
        """
        if self.fail_with:
            raise self.fail_with
        self._ts_counter += 1
        ts = f"{self._ts_counter:.6f}"
        self.posted.append({"channel": channel, "text": text, "blocks": blocks, "thread_ts": thread_ts, "ts": ts, "bot_id": "B_SELF"})
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

        With no *latest*, serve the channel's own messages newest first, up to
        *limit* — that covers both the "is the status message still at the
        bottom?" check and the wider scan that adopts a status message left
        behind by an earlier run.
        """
        if latest is None:
            gone = {d["ts"] for d in self.deleted}
            in_channel = [
                p for p in self.posted
                if p["channel"] == channel and p["thread_ts"] is None and p["ts"] not in gone
            ]
            newest_first = sorted(in_channel, key=lambda p: float(p["ts"]), reverse=True)[:limit]
            return {"messages": [
                {"ts": p["ts"], "text": p["text"], "blocks": p["blocks"], "bot_id": p.get("bot_id")}
                for p in newest_first
            ]}
        blocks = self.history.get(latest)
        return {"messages": [{"blocks": blocks}] if blocks is not None else []}

    def seed_message(self, ts: str, blocks: List[Dict[str, Any]]) -> None:
        """Pretend a message with *blocks* already exists at *ts*."""
        self.history[ts] = blocks

    def seed_channel_message(self, channel: str, text: str, ts: Optional[str] = None, bot_id: Optional[str] = "B_SELF") -> str:
        """Pretend *text* was already posted to *channel* before this process started.

        Used to stand up the state a restart finds: a status message in the
        channel that this run of the bot has no memory of posting.
        """
        if ts is None:
            self._ts_counter += 1
            ts = f"{self._ts_counter:.6f}"
        self.posted.append({"channel": channel, "text": text, "blocks": None, "thread_ts": None, "ts": ts, "bot_id": bot_id})
        return ts

    # -- helpers -----------------------------------------------------------
    def last_update(self) -> Dict[str, Any]:
        """Return the most recent chat_update, asserting one happened."""
        assert self.updated, "expected a chat_update but none was made"
        return self.updated[-1]

    def texts(self) -> List[str]:
        """Return the fallback text of every posted message, in order."""
        return [p["text"] for p in self.posted]

    def cards(self, channel: Optional[str] = None, since: int = 0) -> List[Dict[str, Any]]:
        """Return the item and conversation cards posted, newest last.

        "Has blocks" no longer separates a card from the channel's status
        message — the status message carries blocks of its own now that it has a
        button — so the status footer is what tells them apart.

        Args:
            channel: Limit to one channel; all channels when omitted.
            since: Index into ``posted`` to start from, for "what did this pass
                post?" assertions.
        """
        return [
            p for p in self.posted[since:]
            if p["blocks"] and L._STATUS_SIGNATURE not in (p["text"] or "")
            and (channel is None or p["channel"] == channel)
        ]


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


class FakeModeration:
    """``item.mod`` — records the moderation calls made against an item.

    PRAW exposes this as a cached property, so the fake is planted straight in
    the instance ``__dict__``; a real one would try to reach Reddit.
    """
    def __init__(self) -> None:
        """Start with nothing done to the item."""
        self.approved = False
        self.removed = False
        self.calls: List[str] = []  # in order, so approve-after-ignore is checkable

    def approve(self) -> None:
        """Record an approval."""
        self.approved = True
        self.calls.append("approve")

    def remove(self) -> None:
        """Record a removal."""
        self.removed = True
        self.calls.append("remove")

    def ignore_reports(self) -> None:
        """Record that future reports on this item are to be ignored."""
        self.calls.append("ignore_reports")


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
        mod=FakeModeration(),
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
        self.archived: Optional[bool] = None  # None = never touched by the bot

    def archive(self) -> None:
        """Record an archive, as ``conversation.archive()`` does on Reddit."""
        self.archived = True

    def unarchive(self) -> None:
        """Record an unarchive."""
        self.archived = False


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

    def __call__(self, conv_id: str) -> FakeConversation:
        """``sub.modmail(conv_id)`` — look one conversation up by ID.

        Searches every state as well as ``all``, so a conversation seeded for
        the archive sync is reachable by the archive/unarchive actions too.
        """
        for conv in [*self.all, *(c for convs in self.by_state.values() for c in convs)]:
            if conv.id == conv_id:
                return conv
        raise KeyError(f"no such conversation {conv_id}")


class FakeSubreddit:
    """A subreddit exposing the ``mod`` and ``modmail`` surfaces the bot uses."""
    def __init__(self, reddit: Optional[Any] = None) -> None:
        """Start with an empty modqueue, empty modmail, and no moderators."""
        self.modqueue_items: List[Any] = []
        self.mod = self          # sub.mod.modqueue()
        self.modmail = FakeModmail()
        self.moderators: List[str] = []
        self.removal_reasons: List[Any] = []  # sub.mod.removal_reasons
        self.display_name = "reformed"
        # approve_item reaches back through the subreddit to fetch the item.
        self._reddit = reddit

    def modqueue(self, limit: Optional[int] = None) -> List[Any]:
        """Return a copy so callers cannot mutate the queue by iterating it."""
        return list(self.modqueue_items)

    def moderator(self) -> List[FakeRedditor]:
        """Return the subreddit's moderators, as ``sub.moderator()`` does."""
        return [FakeRedditor(name) for name in self.moderators]


class FakeReddit:
    """Minimal stand-in for ``praw.Reddit``."""

    def __init__(self) -> None:
        """Start with one subreddit and no fetchable items."""
        self._sub = FakeSubreddit(reddit=self)
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


MOD_LIST = [
    'terevos2', 'bishopofreddit', 'friardon', 'superlewis',
    'jcmathetes', 'drkc9n', 'partypastor', 'ciroflexo',
    'deolater', '22duckys',
]


@pytest.fixture
def actions(fake_reddit: FakeReddit, tmp_path: Path) -> RedditActions:
    """A RedditActions wired to fakes, with its logs in a temp directory.

    Seeded with a moderator list rather than left to load one: production calls
    ``refresh_mod_list`` at startup, and an empty list would make every modmail
    author look like a user.
    """
    return RedditActions("reformed", reddit=fake_reddit, log_dir=str(tmp_path / "logs"), mod_list=MOD_LIST)


@pytest.fixture
def feed(monkeypatch: pytest.MonkeyPatch, actions: RedditActions) -> Any:
    """Install a single resolved feed on the listener and return it.

    Stands in for what ``_startup`` builds: one subreddit, both channels
    resolved, its ``RedditActions`` pointed at the fakes.
    """
    import reformed_listener as L

    f = L.Feed("reformed", "mod_actions", "mod_mail")
    f.reddit = actions
    f.modqueue_channel = CHANNEL
    f.modmail_channel = MAIL_CHANNEL
    monkeypatch.setattr(L, "feeds", [f])
    return f


@pytest.fixture
def actions_feed(feed: Any, actions: RedditActions) -> Any:
    """The standard feed switched from voting to Reddit actions.

    ``CONTROLS = actions`` in slack.ini: modmail cards carry Archive/Unarchive
    and the vote dropdown is gone. Modqueue cards gain nothing — the Take
    action… dropdown they used to get is dormant.
    """
    controls = frozenset({RedditActions.CONTROL_ACTIONS})
    actions.controls = controls
    feed.controls = controls
    return feed
