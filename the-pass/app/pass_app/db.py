"""SQLite storage. One connection, serialized by a lock (single-household scale)."""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects(
  id INTEGER PRIMARY KEY,
  code TEXT UNIQUE NOT NULL,
  title TEXT NOT NULL,
  notes TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'backlog',   -- backlog|todo|in_progress|ready_to_close|closed
  source TEXT NOT NULL DEFAULT '',
  external_id TEXT NOT NULL DEFAULT '',
  created_at TEXT, updated_at TEXT,
  card_printed_at TEXT, started_at TEXT, ready_at TEXT, closed_at TEXT
);
CREATE TABLE IF NOT EXISTS tasks(
  id INTEGER PRIMARY KEY,
  project_id INTEGER NOT NULL REFERENCES projects(id),
  title TEXT NOT NULL,
  first_step TEXT NOT NULL DEFAULT '',
  position INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'backlog',   -- backlog|todo|in_progress|done
  source TEXT NOT NULL DEFAULT '',
  external_id TEXT NOT NULL DEFAULT '',
  put_backs INTEGER NOT NULL DEFAULT 0,
  created_at TEXT, updated_at TEXT, started_at TEXT, done_at TEXT
);
CREATE TABLE IF NOT EXISTS slips(
  id INTEGER PRIMARY KEY,
  code TEXT UNIQUE NOT NULL,
  kind TEXT NOT NULL,                       -- task|retro|reminder|notification|void_notice|note
  project_id INTEGER, task_id INTEGER,
  status TEXT NOT NULL,                     -- open|done|void|info
  title TEXT NOT NULL DEFAULT '',
  body TEXT NOT NULL DEFAULT '',
  created_at TEXT, resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS retros(
  id INTEGER PRIMARY KEY,
  project_id INTEGER NOT NULL,
  went_well TEXT NOT NULL DEFAULT '',
  was_hard TEXT NOT NULL DEFAULT '',
  next_time TEXT NOT NULL DEFAULT '',
  notes TEXT NOT NULL DEFAULT '',
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS webhooks(
  id INTEGER PRIMARY KEY,
  url TEXT NOT NULL,
  secret TEXT NOT NULL DEFAULT '',
  events TEXT NOT NULL DEFAULT '*',
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY,
  type TEXT NOT NULL,
  payload TEXT NOT NULL,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS deliveries(
  id INTEGER PRIMARY KEY,
  event_id INTEGER, webhook_id INTEGER,
  status_code INTEGER, error TEXT NOT NULL DEFAULT '',
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS printouts(
  id INTEGER PRIMARY KEY,
  device TEXT NOT NULL, kind TEXT NOT NULL,
  path TEXT NOT NULL, ref TEXT NOT NULL DEFAULT '',
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS scans(
  id INTEGER PRIMARY KEY,
  code TEXT NOT NULL, action TEXT NOT NULL, message TEXT NOT NULL,
  created_at TEXT
);
CREATE INDEX IF NOT EXISTS tasks_project ON tasks(project_id, position);
CREATE INDEX IF NOT EXISTS slips_task ON slips(task_id, status);
"""


class Database:
    def __init__(self, path: Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self.lock = threading.RLock()
        self._depth = 0

    @contextmanager
    def tx(self):
        with self.lock:
            outer = self._depth == 0
            if outer:
                self.conn.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield self.conn
            except BaseException:
                self._depth -= 1
                if outer:
                    self.conn.execute("ROLLBACK")
                raise
            else:
                self._depth -= 1
                if outer:
                    self.conn.execute("COMMIT")

    def one(self, sql, *args):
        with self.lock:
            r = self.conn.execute(sql, args).fetchone()
            return dict(r) if r else None

    def all(self, sql, *args):
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def run(self, sql, *args) -> int:
        with self.lock:
            cur = self.conn.execute(sql, args)
            return cur.lastrowid

    def close(self):
        with self.lock:
            self.conn.close()
