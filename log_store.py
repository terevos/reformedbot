"""SQLite persistence for one subreddit's Slack bookkeeping.

Replaces the JSON logs this bot used to keep. Those were one document per
subreddit, so changing one field of one entry meant re-serialising the whole
file — 122 ms and 2.8 MB of churn at the numbering cap, several times per poll,
and the reason concurrent writers silently erased each other's votes. Here a
change is one ``UPDATE`` of one row.

The shape callers see is unchanged. :meth:`modqueue_snapshot` and
:meth:`modmail_snapshot` hand back exactly the nested dicts the JSON logs held,
which is what the summary code, the exports and the tests read; everything that
*writes* goes through the row-level helpers instead.

**Entries are not stored as blobs.** The fields the bot queries or sorts by are
real columns; anything else an entry carries — old field names, data written by
a future version — round-trips through the ``extra`` JSON column rather than
being dropped. Votes and modmail message IDs live in their own tables, because
those are the two places where two writers touch one entry at once.

Concurrency: WAL mode, one connection per thread, and every read-modify-write
inside a ``BEGIN IMMEDIATE`` transaction. The poll thread and the Slack handler
threads can therefore write at the same time without a lock of our own, which
is what makes the read-merge-write convention the JSON logs needed unnecessary.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence

SCHEMA_VERSION: int = 1

# Modqueue fields promoted to columns. Everything else an entry holds goes to
# ``extra``; ``votes`` is excluded here because it has its own table.
ITEM_COLUMNS: Sequence[str] = (
    "queue_num", "report_link", "item_type", "author",
    "slack_ts", "slack_permalink", "slack_blocks",
    "done_at", "reopened_at", "done_action", "done_checks",
    "done_by", "done_note_ts", "ban_hold_at",
)
# Columns holding a JSON document rather than a scalar.
ITEM_JSON_COLUMNS: Sequence[str] = ("slack_blocks",)

# Modmail fields promoted to columns; ``messages`` has its own table.
CONV_COLUMNS: Sequence[str] = (
    "conv_num", "subject", "author", "slack_ts", "slack_permalink",
    "done_at", "reopened_at",
)
CONV_JSON_COLUMNS: Sequence[str] = ()

KIND_QUEUE: str = "modqueue"
KIND_MAIL: str = "modmail"

_SCHEMA: str = """
CREATE TABLE IF NOT EXISTS items (
    channel         TEXT NOT NULL,
    item_id         TEXT NOT NULL,
    queue_num       INTEGER,
    report_link     TEXT,
    item_type       TEXT,
    author          TEXT,
    slack_ts        TEXT,
    slack_permalink TEXT,
    slack_blocks    TEXT,
    done_at         REAL,
    reopened_at     REAL,
    done_action     TEXT,
    done_checks     INTEGER,
    done_by         TEXT,
    done_note_ts    TEXT,
    ban_hold_at     REAL,
    extra           TEXT,
    PRIMARY KEY (channel, item_id)
);
CREATE INDEX IF NOT EXISTS items_by_done ON items (channel, done_at);
CREATE INDEX IF NOT EXISTS items_by_ts ON items (channel, slack_ts);

CREATE TABLE IF NOT EXISTS votes (
    channel    TEXT NOT NULL,
    item_id    TEXT NOT NULL,
    slack_user TEXT NOT NULL,
    vote_key   TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    PRIMARY KEY (channel, item_id, slack_user, vote_key)
);

CREATE TABLE IF NOT EXISTS convs (
    channel         TEXT NOT NULL,
    conv_id         TEXT NOT NULL,
    conv_num        INTEGER,
    subject         TEXT,
    author          TEXT,
    slack_ts        TEXT,
    slack_permalink TEXT,
    done_at         REAL,
    reopened_at     REAL,
    extra           TEXT,
    PRIMARY KEY (channel, conv_id)
);
CREATE INDEX IF NOT EXISTS convs_by_done ON convs (channel, done_at);

CREATE TABLE IF NOT EXISTS conv_messages (
    channel TEXT NOT NULL,
    conv_id TEXT NOT NULL,
    msg_id  TEXT NOT NULL,
    PRIMARY KEY (channel, conv_id, msg_id)
);

CREATE TABLE IF NOT EXISTS counters (
    kind     TEXT NOT NULL,
    channel  TEXT NOT NULL,
    next_num INTEGER NOT NULL,
    cycle    INTEGER NOT NULL,
    PRIMARY KEY (kind, channel)
);

-- A channel with no entries yet still has to be *known*: its presence is what
-- says "already imported from the old JSON logs", so an import never runs twice
-- and resurrects entries a rollover has since archived.
CREATE TABLE IF NOT EXISTS channels (
    kind    TEXT NOT NULL,
    channel TEXT NOT NULL,
    PRIMARY KEY (kind, channel)
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def _row_to_entry(row: sqlite3.Row, columns: Sequence[str], json_columns: Sequence[str]) -> Dict[str, Any]:
    """Rebuild a log entry dict from a database row.

    NULL columns are **omitted**, not returned as ``None``: the done-state
    encoding is "absent means open", and a key holding ``None`` would read as
    present to anything doing a membership test.
    """
    entry: Dict[str, Any] = {}
    for col in columns:
        value = row[col]
        if value is None:
            continue
        entry[col] = json.loads(value) if col in json_columns else value
    extra = row["extra"]
    if extra:
        entry.update(json.loads(extra))
    return entry


def _entry_to_values(entry: Dict[str, Any], columns: Sequence[str], json_columns: Sequence[str], skip: Sequence[str] = ()) -> List[Any]:
    """Flatten a log entry into column values plus the ``extra`` JSON blob.

    Fields with no column of their own are preserved in ``extra`` rather than
    dropped — the logs have carried several generations of field names, and a
    store that silently loses the ones it does not recognise is not a store.
    """
    values: List[Any] = []
    for col in columns:
        value = entry.get(col)
        if value is not None and col in json_columns:
            value = json.dumps(value)
        values.append(value)
    known = set(columns) | set(skip)
    extra = {k: v for k, v in entry.items() if k not in known}
    values.append(json.dumps(extra, sort_keys=True) if extra else None)
    return values


class LogStore:
    """One subreddit's Slack bookkeeping, in one SQLite file."""

    def __init__(self, db_path: str) -> None:
        """Open (creating if needed) the database at *db_path*.

        Args:
            db_path: Full path to the ``.db`` file. Its directory is created.
        """
        self.db_path: str = db_path
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._local = threading.local()
        # executescript() commits and ends any open transaction of its own, so
        # the schema goes in outside one; the version stamp then takes the
        # normal path.
        self._connection().executescript(_SCHEMA)
        self.set_meta("schema_version", str(SCHEMA_VERSION))

    # ------------------------------------------------------------------
    # Connection handling
    # ------------------------------------------------------------------

    def _connection(self) -> sqlite3.Connection:
        """Return this thread's connection, opening one if needed.

        SQLite connections are not safe to share across threads, and this bot
        writes from the poll thread and from Bolt's handler threads at once.
        WAL lets those proceed concurrently; ``busy_timeout`` covers the moment
        one writer holds the lock.
        """
        conn: Optional[sqlite3.Connection] = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.db_path, timeout=30.0, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=10000")
            self._local.conn = conn
        return conn

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """Run a block inside one ``BEGIN IMMEDIATE`` transaction.

        Immediate rather than deferred: every caller here reads and then writes,
        and taking the write lock up front is what makes the pair atomic instead
        of merely ordered.
        """
        conn = self._connection()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")

    def close(self) -> None:
        """Close this thread's connection, if it has one."""
        conn: Optional[sqlite3.Connection] = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # ------------------------------------------------------------------
    # Channels
    # ------------------------------------------------------------------

    def mark_channel(self, kind: str, channel: str) -> None:
        """Record that *channel* is known for *kind*, even with no entries."""
        with self._transaction() as conn:
            self._mark_channel(conn, kind, channel)

    @staticmethod
    def _mark_channel(conn: sqlite3.Connection, kind: str, channel: str) -> None:
        """Record a known channel inside an open transaction."""
        conn.execute("INSERT OR IGNORE INTO channels (kind, channel) VALUES (?, ?)", (kind, channel))

    def knows_channel(self, kind: str, channel: str) -> bool:
        """Return True if *channel* has already been seen for *kind*."""
        row = self._connection().execute(
            "SELECT 1 FROM channels WHERE kind = ? AND channel = ?", (kind, channel)
        ).fetchone()
        return row is not None

    def _channels(self, conn: sqlite3.Connection, kind: str) -> List[str]:
        """Return every known channel for *kind*."""
        return [r[0] for r in conn.execute("SELECT channel FROM channels WHERE kind = ?", (kind,))]

    # ------------------------------------------------------------------
    # Modqueue items
    # ------------------------------------------------------------------

    def _votes_for_channel(self, conn: sqlite3.Connection, channel: str) -> Dict[str, Dict[str, List[str]]]:
        """Return ``{item_id: {slack_user: [vote_key, ...]}}`` for one channel."""
        votes: Dict[str, Dict[str, List[str]]] = {}
        for row in conn.execute(
            "SELECT item_id, slack_user, vote_key FROM votes WHERE channel = ? ORDER BY seq", (channel,)
        ):
            votes.setdefault(row["item_id"], {}).setdefault(row["slack_user"], []).append(row["vote_key"])
        return votes

    def channel_items(self, channel: str) -> Dict[str, Dict[str, Any]]:
        """Return one channel's modqueue entries, votes included.

        This is what replaced ``get_modqueue_file()[channel]`` on the hot paths:
        the poll loop and the summary want one channel, not every channel of
        every feed.
        """
        conn = self._connection()
        votes = self._votes_for_channel(conn, channel)
        entries: Dict[str, Dict[str, Any]] = {}
        for row in conn.execute("SELECT * FROM items WHERE channel = ?", (channel,)):
            entry = _row_to_entry(row, ITEM_COLUMNS, ITEM_JSON_COLUMNS)
            item_votes = votes.get(row["item_id"])
            if item_votes:
                entry["votes"] = item_votes
            entries[row["item_id"]] = entry
        return entries

    def modqueue_snapshot(self) -> Dict[str, Dict[str, Dict[str, Any]]]:
        """Return every channel's modqueue entries in the old log's shape."""
        conn = self._connection()
        data: Dict[str, Dict[str, Dict[str, Any]]] = {c: {} for c in self._channels(conn, KIND_QUEUE)}
        for channel in list(data):
            data[channel] = self.channel_items(channel)
        return data

    def item(self, channel: str, item_id: str) -> Dict[str, Any]:
        """Return one modqueue entry (votes included), or an empty dict."""
        conn = self._connection()
        row = conn.execute("SELECT * FROM items WHERE channel = ? AND item_id = ?", (channel, item_id)).fetchone()
        if row is None:
            return {}
        entry = _row_to_entry(row, ITEM_COLUMNS, ITEM_JSON_COLUMNS)
        votes = self.votes(channel, item_id)
        if votes:
            entry["votes"] = votes
        return entry

    @staticmethod
    def _write_item(conn: sqlite3.Connection, channel: str, item_id: str, entry: Dict[str, Any]) -> None:
        """Insert or replace one item row (votes are not touched)."""
        cols = ", ".join(ITEM_COLUMNS) + ", extra"
        marks = ", ".join("?" for _ in range(len(ITEM_COLUMNS) + 1))
        values = _entry_to_values(entry, ITEM_COLUMNS, ITEM_JSON_COLUMNS, skip=("votes",))
        conn.execute(
            f"INSERT OR REPLACE INTO items (channel, item_id, {cols}) VALUES (?, ?, {marks})",
            [channel, item_id, *values],
        )

    def add_items(self, channel: str, entries: Dict[str, Dict[str, Any]]) -> None:
        """Insert newly-discovered modqueue items.

        Existing rows are left alone: a poll that re-sees an item must not
        overwrite the state a mod has since clicked into it.
        """
        if not entries:
            self.mark_channel(KIND_QUEUE, channel)
            return
        with self._transaction() as conn:
            self._mark_channel(conn, KIND_QUEUE, channel)
            for item_id, entry in entries.items():
                row = conn.execute(
                    "SELECT 1 FROM items WHERE channel = ? AND item_id = ?", (channel, item_id)
                ).fetchone()
                if row is None:
                    self._write_item(conn, channel, item_id, entry)

    @contextmanager
    def edit_item(self, channel: str, item_id: str, create: bool = False) -> Iterator[Optional[Dict[str, Any]]]:
        """Yield one item's entry for mutation, writing just that row back.

        Yields ``None`` when the item is unknown and *create* is False, so
        callers keep the "only touch what we have posted" behaviour they had
        when they walked the JSON log. Votes are not included and not written —
        see :meth:`update_votes`.
        """
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM items WHERE channel = ? AND item_id = ?", (channel, item_id)).fetchone()
            if row is None and not create:
                yield None
                return
            entry = _row_to_entry(row, ITEM_COLUMNS, ITEM_JSON_COLUMNS) if row is not None else {}
            yield entry
            self._mark_channel(conn, KIND_QUEUE, channel)
            self._write_item(conn, channel, item_id, entry)

    def delete_items(self, channel: str, item_ids: Sequence[str]) -> None:
        """Delete items and their votes — used by the rollover, after archiving."""
        if not item_ids:
            return
        with self._transaction() as conn:
            for item_id in item_ids:
                conn.execute("DELETE FROM items WHERE channel = ? AND item_id = ?", (channel, item_id))
                conn.execute("DELETE FROM votes WHERE channel = ? AND item_id = ?", (channel, item_id))

    def import_items(self, channel: str, entries: Dict[str, Any]) -> int:
        """Add entries (votes included) for a channel, skipping ones already stored.

        The JSON-log import path. Unlike :meth:`replace_modqueue` it touches
        nothing outside *channel*, so one feed importing its own channels cannot
        disturb another feed's.

        Returns:
            The number of entries written.
        """
        written = 0
        with self._transaction() as conn:
            self._mark_channel(conn, KIND_QUEUE, channel)
            for item_id, entry in (entries or {}).items():
                if not isinstance(entry, dict):
                    continue
                if conn.execute("SELECT 1 FROM items WHERE channel = ? AND item_id = ?",
                                (channel, item_id)).fetchone() is not None:
                    continue
                self._write_item(conn, channel, item_id, entry)
                self._write_votes(conn, channel, item_id, entry.get("votes") or {})
                written += 1
        return written

    def import_convs(self, channel: str, convs: Dict[str, Any]) -> int:
        """Add conversations (messages included) for a channel, skipping known ones.

        Returns:
            The number of conversations written.
        """
        written = 0
        with self._transaction() as conn:
            self._mark_channel(conn, KIND_MAIL, channel)
            for conv_id, entry in (convs or {}).items():
                if not isinstance(entry, dict):
                    continue
                if conn.execute("SELECT 1 FROM convs WHERE channel = ? AND conv_id = ?",
                                (channel, conv_id)).fetchone() is not None:
                    continue
                self._write_conv(conn, channel, conv_id, entry)
                for msg_id in (entry.get("messages") or {}):
                    conn.execute(
                        "INSERT OR IGNORE INTO conv_messages (channel, conv_id, msg_id) VALUES (?, ?, ?)",
                        (channel, conv_id, msg_id),
                    )
                written += 1
        return written

    def replace_modqueue(self, data: Dict[str, Any]) -> None:
        """Replace the whole modqueue log with *data* (old log shape).

        The one remaining whole-log write. It exists for imports from the JSON
        logs and for tests that seed a state directly; nothing on the poll or
        click paths uses it.
        """
        with self._transaction() as conn:
            conn.execute("DELETE FROM items")
            conn.execute("DELETE FROM votes")
            conn.execute("DELETE FROM channels WHERE kind = ?", (KIND_QUEUE,))
            for channel, entries in data.items():
                self._mark_channel(conn, KIND_QUEUE, channel)
                if not isinstance(entries, dict):
                    continue
                for item_id, entry in entries.items():
                    if not isinstance(entry, dict):
                        continue
                    self._write_item(conn, channel, item_id, entry)
                    self._write_votes(conn, channel, item_id, entry.get("votes") or {})

    # ------------------------------------------------------------------
    # Votes
    # ------------------------------------------------------------------

    @staticmethod
    def _write_votes(conn: sqlite3.Connection, channel: str, item_id: str, votes: Dict[str, Any]) -> None:
        """Replace an item's votes with *votes* (``{user: keys}``).

        Accepts the two older shapes the logs hold — a bare string, and keys
        carrying a stale ``|<timestamp>`` suffix — so an import does not have to
        normalise before it stores.
        """
        conn.execute("DELETE FROM votes WHERE channel = ? AND item_id = ?", (channel, item_id))
        seq = 0
        for user, keys in (votes or {}).items():
            if isinstance(keys, str):
                keys = [keys]
            for key in keys or []:
                key = str(key).split("|", 1)[0]
                conn.execute(
                    "INSERT OR IGNORE INTO votes (channel, item_id, slack_user, vote_key, seq) VALUES (?, ?, ?, ?, ?)",
                    (channel, item_id, user, key, seq),
                )
                seq += 1

    def votes(self, channel: str, item_id: str) -> Dict[str, List[str]]:
        """Return ``{slack_user: [vote_key, ...]}`` for one item."""
        votes: Dict[str, List[str]] = {}
        for row in self._connection().execute(
            "SELECT slack_user, vote_key FROM votes WHERE channel = ? AND item_id = ? ORDER BY seq",
            (channel, item_id),
        ):
            votes.setdefault(row["slack_user"], []).append(row["vote_key"])
        return votes

    def update_votes(self, channel: str, item_id: str, user_id: str, change: Callable[[List[str]], List[str]]) -> List[str]:
        """Apply *change* to one mod's votes on one item, atomically.

        The whole point of the votes table: two mods clicking at the same moment
        touch different rows, and one mod clicking twice is serialised by the
        transaction. The JSON log could only do this by rewriting the file, which
        is how votes used to get erased.

        Args:
            channel: Slack channel ID.
            item_id: Reddit item ID (bare).
            user_id: Slack user ID of the voting moderator.
            change: Takes this mod's current vote keys and returns the new list.

        Returns:
            The mod's vote keys after the change.
        """
        with self._transaction() as conn:
            current = [
                r["vote_key"] for r in conn.execute(
                    "SELECT vote_key FROM votes WHERE channel = ? AND item_id = ? AND slack_user = ? ORDER BY seq",
                    (channel, item_id, user_id),
                )
            ]
            updated = change(list(current))
            conn.execute(
                "DELETE FROM votes WHERE channel = ? AND item_id = ? AND slack_user = ?",
                (channel, item_id, user_id),
            )
            base = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM votes WHERE channel = ? AND item_id = ?",
                                (channel, item_id)).fetchone()[0]
            for offset, key in enumerate(updated, start=1):
                conn.execute(
                    "INSERT OR IGNORE INTO votes (channel, item_id, slack_user, vote_key, seq) VALUES (?, ?, ?, ?, ?)",
                    (channel, item_id, user_id, key, base + offset),
                )
            # An item nobody has logged yet still gets a row, so the vote has
            # something to hang on and the card can be rebuilt from the store.
            conn.execute("INSERT OR IGNORE INTO items (channel, item_id) VALUES (?, ?)", (channel, item_id))
            self._mark_channel(conn, KIND_QUEUE, channel)
            return updated

    # ------------------------------------------------------------------
    # Modmail conversations
    # ------------------------------------------------------------------

    def _messages_for_channel(self, conn: sqlite3.Connection, channel: str) -> Dict[str, Dict[str, bool]]:
        """Return ``{conv_id: {msg_id: True}}`` for one channel."""
        messages: Dict[str, Dict[str, bool]] = {}
        for row in conn.execute("SELECT conv_id, msg_id FROM conv_messages WHERE channel = ?", (channel,)):
            messages.setdefault(row["conv_id"], {})[row["msg_id"]] = True
        return messages

    def channel_convs(self, channel: str) -> Dict[str, Dict[str, Any]]:
        """Return one channel's modmail conversations, messages included."""
        conn = self._connection()
        messages = self._messages_for_channel(conn, channel)
        convs: Dict[str, Dict[str, Any]] = {}
        for row in conn.execute("SELECT * FROM convs WHERE channel = ?", (channel,)):
            entry = _row_to_entry(row, CONV_COLUMNS, CONV_JSON_COLUMNS)
            entry["messages"] = messages.get(row["conv_id"], {})
            convs[row["conv_id"]] = entry
        return convs

    def modmail_snapshot(self) -> Dict[str, Dict[str, Dict[str, Any]]]:
        """Return every channel's modmail in the old log's shape.

        Including the ``modmail_conv`` level, which is an accident of history
        that every caller and every stored export already expects.
        """
        conn = self._connection()
        return {c: {"modmail_conv": self.channel_convs(c)} for c in self._channels(conn, KIND_MAIL)}

    def conv(self, channel: str, conv_id: str) -> Dict[str, Any]:
        """Return one modmail conversation entry, or an empty dict."""
        conn = self._connection()
        row = conn.execute("SELECT * FROM convs WHERE channel = ? AND conv_id = ?", (channel, conv_id)).fetchone()
        if row is None:
            return {}
        entry = _row_to_entry(row, CONV_COLUMNS, CONV_JSON_COLUMNS)
        entry["messages"] = {
            r["msg_id"]: True for r in conn.execute(
                "SELECT msg_id FROM conv_messages WHERE channel = ? AND conv_id = ?", (channel, conv_id)
            )
        }
        return entry

    @staticmethod
    def _write_conv(conn: sqlite3.Connection, channel: str, conv_id: str, entry: Dict[str, Any]) -> None:
        """Insert or replace one conversation row (messages are not touched)."""
        cols = ", ".join(CONV_COLUMNS) + ", extra"
        marks = ", ".join("?" for _ in range(len(CONV_COLUMNS) + 1))
        values = _entry_to_values(entry, CONV_COLUMNS, CONV_JSON_COLUMNS, skip=("messages",))
        conn.execute(
            f"INSERT OR REPLACE INTO convs (channel, conv_id, {cols}) VALUES (?, ?, {marks})",
            [channel, conv_id, *values],
        )

    @contextmanager
    def edit_conv(self, channel: str, conv_id: str, create: bool = False) -> Iterator[Optional[Dict[str, Any]]]:
        """Yield one conversation's entry for mutation, writing that row back.

        ``messages`` is present for reading but not written back here; new
        message IDs go through :meth:`add_conv_messages`, which appends rather
        than replacing.
        """
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM convs WHERE channel = ? AND conv_id = ?", (channel, conv_id)).fetchone()
            if row is None and not create:
                yield None
                return
            entry = _row_to_entry(row, CONV_COLUMNS, CONV_JSON_COLUMNS) if row is not None else {}
            yield entry
            self._mark_channel(conn, KIND_MAIL, channel)
            self._write_conv(conn, channel, conv_id, entry)

    def add_conv_messages(self, channel: str, conv_id: str, msg_ids: Sequence[str]) -> None:
        """Record message IDs as posted, leaving the ones already there."""
        if not msg_ids:
            return
        with self._transaction() as conn:
            self._mark_channel(conn, KIND_MAIL, channel)
            for msg_id in msg_ids:
                conn.execute(
                    "INSERT OR IGNORE INTO conv_messages (channel, conv_id, msg_id) VALUES (?, ?, ?)",
                    (channel, conv_id, msg_id),
                )

    def delete_convs(self, channel: str, conv_ids: Sequence[str]) -> None:
        """Delete conversations and their messages — used by the rollover."""
        if not conv_ids:
            return
        with self._transaction() as conn:
            for conv_id in conv_ids:
                conn.execute("DELETE FROM convs WHERE channel = ? AND conv_id = ?", (channel, conv_id))
                conn.execute("DELETE FROM conv_messages WHERE channel = ? AND conv_id = ?", (channel, conv_id))

    def replace_modmail(self, data: Dict[str, Any]) -> None:
        """Replace the whole modmail log with *data* (old log shape)."""
        with self._transaction() as conn:
            conn.execute("DELETE FROM convs")
            conn.execute("DELETE FROM conv_messages")
            conn.execute("DELETE FROM channels WHERE kind = ?", (KIND_MAIL,))
            for channel, payload in data.items():
                self._mark_channel(conn, KIND_MAIL, channel)
                convs = (payload or {}).get("modmail_conv") if isinstance(payload, dict) else None
                if not isinstance(convs, dict):
                    continue
                for conv_id, entry in convs.items():
                    if not isinstance(entry, dict):
                        continue
                    self._write_conv(conn, channel, conv_id, entry)
                    for msg_id in (entry.get("messages") or {}):
                        conn.execute(
                            "INSERT OR IGNORE INTO conv_messages (channel, conv_id, msg_id) VALUES (?, ?, ?)",
                            (channel, conv_id, msg_id),
                        )

    # ------------------------------------------------------------------
    # Counters and meta
    # ------------------------------------------------------------------

    def counters(self) -> Dict[str, Dict[str, Dict[str, int]]]:
        """Return ``{kind: {channel: {'next': n, 'cycle': n}}}``."""
        out: Dict[str, Dict[str, Dict[str, int]]] = {}
        for row in self._connection().execute("SELECT kind, channel, next_num, cycle FROM counters"):
            out.setdefault(row["kind"], {})[row["channel"]] = {"next": row["next_num"], "cycle": row["cycle"]}
        return out

    def set_counter(self, kind: str, channel: str, next_num: int, cycle: int) -> None:
        """Store one channel's numbering counter."""
        with self._transaction() as conn:
            conn.execute(
                "INSERT INTO counters (kind, channel, next_num, cycle) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(kind, channel) DO UPDATE SET next_num = excluded.next_num, cycle = excluded.cycle",
                (kind, channel, int(next_num), int(cycle)),
            )

    def replace_counters(self, data: Dict[str, Any]) -> None:
        """Replace every counter — the import path and the tests use this."""
        with self._transaction() as conn:
            conn.execute("DELETE FROM counters")
            for kind, channels in (data or {}).items():
                for channel, state in (channels or {}).items():
                    conn.execute(
                        "INSERT OR REPLACE INTO counters (kind, channel, next_num, cycle) VALUES (?, ?, ?, ?)",
                        (kind, channel, int(state.get("next") or 1), int(state.get("cycle") or 1)),
                    )

    def get_meta(self, key: str, default: str = "") -> str:
        """Return a stored bookkeeping value (schema version, last export …)."""
        row = self._connection().execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return default if row is None else row["value"]

    def set_meta(self, key: str, value: str) -> None:
        """Store a bookkeeping value."""
        with self._transaction() as conn:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    # ------------------------------------------------------------------
    # Housekeeping
    # ------------------------------------------------------------------

    def is_empty(self) -> bool:
        """Return True if nothing has ever been stored here.

        What the JSON import tests: a database with rows is not re-imported.
        """
        conn = self._connection()
        for table in ("items", "convs", "channels", "counters"):
            if conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None:
                return False
        return True

    def checkpoint(self) -> None:
        """Fold the write-ahead log back into the database file.

        SQLite checkpoints on its own once the WAL grows past a threshold, so
        this is housekeeping, not correctness: it keeps the ``.db`` file a
        complete copy between polls, which is what anyone taking a backup by
        hand will assume it is.
        """
        self._connection().execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def vacuum(self) -> None:
        """Reclaim space after a rollover deleted a cycle's worth of rows."""
        self._connection().execute("VACUUM")

    def stats(self) -> Dict[str, int]:
        """Return row counts, for log lines and for tests."""
        conn = self._connection()
        return {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("items", "votes", "convs", "conv_messages", "counters")
        }
