"""SQLite storage: chat history, what Cerberus learns about you, writing samples, agents, watchlist.

One small database file (data/cerberus.db). A lock makes it safe to use from many threads.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Optional

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages(
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, mode TEXT, role TEXT, content TEXT);
CREATE INDEX IF NOT EXISTS idx_messages_mode ON messages(mode, id);

CREATE TABLE IF NOT EXISTS facts(
    id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT UNIQUE, value TEXT, category TEXT,
    confidence REAL DEFAULT 0.5, source TEXT, first_seen REAL, last_seen REAL, seen_count INTEGER DEFAULT 1);

CREATE TABLE IF NOT EXISTS samples(
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, kind TEXT, title TEXT, content TEXT);

CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS agents(
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE, role TEXT, backstory TEXT,
    skills TEXT, task TEXT, schedule TEXT, model_role TEXT DEFAULT 'generalist',
    active INTEGER DEFAULT 0, created REAL, last_run REAL, last_status TEXT, last_output TEXT);

CREATE TABLE IF NOT EXISTS agent_runs(
    id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id INTEGER, ts REAL, trigger TEXT, status TEXT, output TEXT);

CREATE TABLE IF NOT EXISTS watchlist(symbol TEXT PRIMARY KEY, added REAL);

CREATE TABLE IF NOT EXISTS budget(
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, kind TEXT, amount REAL, category TEXT, note TEXT);

CREATE TABLE IF NOT EXISTS embeddings(
    kind TEXT, ref TEXT, text TEXT, model TEXT, vec BLOB, PRIMARY KEY(kind, ref));

CREATE TABLE IF NOT EXISTS notifications(
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, kind TEXT, title TEXT, body TEXT, read INTEGER DEFAULT 0);
"""


class Database:
    def __init__(self, path: Path | str | None = None):
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.path = str(path or config.DATA_DIR / "cerberus.db")
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    # -- generic helpers
    def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return cur.lastrowid or 0

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, tuple(params)).fetchall()]

    def one(self, sql: str, params: Iterable[Any] = ()) -> Optional[dict]:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- key/value
    def kv_get(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value FROM kv WHERE key=?", (key,))
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return default

    def kv_set(self, key: str, value: Any) -> None:
        self.execute("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, json.dumps(value)))

    # -- messages
    def add_message(self, mode: str, role: str, content: str) -> int:
        return self.execute("INSERT INTO messages(ts,mode,role,content) VALUES(?,?,?,?)",
                            (time.time(), mode, role, content))

    def recent_messages(self, mode: str, limit: int = 12) -> list[dict]:
        rows = self.query("SELECT role,content,ts FROM messages WHERE mode=? ORDER BY id DESC LIMIT ?",
                          (mode, limit))
        return list(reversed(rows))

    def clear_messages(self, mode: Optional[str] = None) -> None:
        if mode:
            self.execute("DELETE FROM messages WHERE mode=?", (mode,))
        else:
            self.execute("DELETE FROM messages")

    def search_messages(self, words: list[str], limit: int = 4, role: str = "user") -> list[dict]:
        """Very small keyword memory: find old messages that share words with the new one."""
        words = [w for w in words if len(w) > 3][:6]
        if not words:
            return []
        clause = " OR ".join(["content LIKE ?"] * len(words))
        return self.query(f"SELECT mode,role,content,ts FROM messages WHERE role=? AND ({clause}) "
                          f"ORDER BY id DESC LIMIT ?", (role, *[f"%{w}%" for w in words], limit))

    # -- notifications (proactive messages from Cerberus)
    def add_notification(self, kind: str, title: str, body: str) -> int:
        return self.execute("INSERT INTO notifications(ts,kind,title,body) VALUES(?,?,?,?)", (time.time(), kind, title, body))

    def notifications(self, unread_only: bool = False, limit: int = 30, after: Optional[int] = None) -> list[dict]:
        if after is not None:
            return self.query("SELECT id,ts,kind,title,body,read FROM notifications WHERE id>? ORDER BY id LIMIT ?", (after, limit))
        where = "WHERE read=0" if unread_only else ""
        rows = self.query(f"SELECT id,ts,kind,title,body,read FROM notifications {where} ORDER BY id DESC LIMIT ?", (limit,))
        return list(reversed(rows)) if unread_only else rows

    def mark_notifications_read(self, ids: Optional[list[int]] = None) -> None:
        if ids is None:
            self.execute("UPDATE notifications SET read=1")
        elif ids:
            self.execute(f"UPDATE notifications SET read=1 WHERE id IN ({','.join('?' * len(ids))})", [int(i) for i in ids])

    # -- samples (writing samples, resume, notes)
    def add_sample(self, kind: str, title: str, content: str) -> int:
        return self.execute("INSERT INTO samples(ts,kind,title,content) VALUES(?,?,?,?)",
                            (time.time(), kind, title, content))

    def list_samples(self, kind: Optional[str] = None) -> list[dict]:
        if kind:
            return self.query("SELECT * FROM samples WHERE kind=? ORDER BY id DESC", (kind,))
        return self.query("SELECT * FROM samples ORDER BY id DESC")

    def delete_sample(self, sample_id: int) -> None:
        self.execute("DELETE FROM samples WHERE id=?", (sample_id,))
