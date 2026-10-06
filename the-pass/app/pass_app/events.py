"""Event log + outbound webhooks.

Every state change is written to the `events` table (pollable at GET /api/events)
and POSTed as JSON to each active webhook whose filter matches:

  {"id": 12, "event": "task.done", "at": "2026-10-05T20:31:00-05:00", "data": {...}}

Header X-Pass-Event carries the event type; if the webhook has a secret,
X-Pass-Signature: sha256=<hex HMAC of the raw body>.
Filters: "*" (all), exact names, or prefixes like "task.*", comma-separated.
Delivery runs on a background thread with 3 attempts; results land in `deliveries`.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import logging
import queue
import threading
import time
import urllib.request

log = logging.getLogger("pass.events")

EVENT_TYPES = [
    "project.created", "project.updated", "project.status_changed", "project.card_printed",
    "project.ready_to_close", "project.closed", "retro.recorded",
    "task.created", "task.updated", "task.status_changed", "task.started", "task.done",
    "task.put_back", "slip.printed", "slip.voided", "reminder.printed",
    "notification.printed", "reminder.acknowledged", "scan.received", "webhook.test",
]


def now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def matches(filters: str, event: str) -> bool:
    for f in (x.strip() for x in (filters or "*").split(",")):
        if f in ("*", event) or (f.endswith(".*") and event.startswith(f[:-1])):
            return True
    return False


class Events:
    def __init__(self, db, start_worker: bool = True):
        self.db = db
        self._q: queue.Queue = queue.Queue()
        if start_worker:
            threading.Thread(target=self._worker, daemon=True).start()

    def emit(self, type_: str, data: dict) -> int:
        at = now()
        eid = self.db.run("INSERT INTO events(type,payload,created_at) VALUES(?,?,?)",
                          type_, json.dumps(data, default=str), at)
        hooks = self.db.all("SELECT * FROM webhooks WHERE active=1")
        body = {"id": eid, "event": type_, "at": at, "data": data}
        for h in hooks:
            if matches(h["events"], type_):
                self._q.put((h, eid, body))
        return eid

    def _deliver(self, hook, eid, body):
        raw = json.dumps(body, default=str).encode()
        headers = {"Content-Type": "application/json", "X-Pass-Event": body["event"],
                   "User-Agent": "the-pass/0.1"}
        if hook["secret"]:
            sig = hmac.new(hook["secret"].encode(), raw, hashlib.sha256).hexdigest()
            headers["X-Pass-Signature"] = "sha256=" + sig
        code, err = None, ""
        for attempt in range(3):
            try:
                req = urllib.request.Request(hook["url"], data=raw, headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=8) as r:
                    code = r.status
                err = ""
                break
            except Exception as e:  # noqa: BLE001
                code = getattr(e, "code", None)
                err = str(e)
                time.sleep(0.5 * (attempt + 1))
        try:
            self.db.run("INSERT INTO deliveries(event_id,webhook_id,status_code,error,created_at)"
                        " VALUES(?,?,?,?,?)", eid, hook["id"], code, err, now())
        except Exception:  # db closed during shutdown
            pass

    def _worker(self):
        while True:
            hook, eid, body = self._q.get()
            self._deliver(hook, eid, body)
            self._q.task_done()

    def drain(self, timeout: float = 10.0):
        """Wait for queued deliveries (tests/demo)."""
        end = time.time() + timeout
        while self._q.unfinished_tasks and time.time() < end:
            time.sleep(0.05)
