"""Domain logic for The Pass. The scan loop lives in `Pass.scan`.

Statuses
  project: backlog -> todo (card printed) -> in_progress (card scanned)
           -> ready_to_close (last slip done; retro slip printed) -> closed (retro recorded)
  task:    backlog -> todo (card printed) -> in_progress (slip printed) -> done (slip scanned)
           in_progress -> todo again when the card is rescanned while its slip is open
  slip:    open -> done | void ; info slips (notices) are status 'info'

Principles (PDA): the system never picks or assigns Logan's next task. A card scan
is his choice; the card's own next open step (its list order) is what prints. Nothing
auto-returns; only rescanning a card puts its task back. No scores, streaks, nags.
"""
from __future__ import annotations

import re
import secrets
import threading

from . import render
from .db import Database
from .events import Events, now
from .printers import Printers

ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"  # no 0/O/1/I/L/U: safe for eyes and wedges
OPEN_TASK = ("backlog", "todo")


class PassError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message, self.status = message, status


def new_code(prefix: str) -> str:
    return prefix + "".join(secrets.choice(ALPHABET) for _ in range(6))


def normalize_code(raw: str) -> str:
    s = (raw or "").strip()
    m = re.search(r"/retro/(\d+)", s)
    if m:
        return f"RETRO:{m.group(1)}"
    m = re.search(r"[?&]code=([A-Za-z0-9]+)", s)
    if m:
        s = m.group(1)
    return re.sub(r"[^A-Za-z0-9]", "", s).upper()


class Pass:
    def __init__(self, settings, printers: Printers | None = None):
        self.s = settings
        self.db = Database(settings.db_path)
        self.printers = printers or Printers(settings.out_dir, settings.receipt_printer,
                                             settings.label_printer, settings.start_workers,
                                             keep=settings.out_keep)
        self.events = Events(self.db, settings.start_workers)
        self.lock = threading.RLock()
        for url in settings.webhooks:
            if not self.db.one("SELECT id FROM webhooks WHERE url=?", url):
                self.add_webhook(url)

    # ------------------------------------------------------------ helpers
    def _code(self, prefix: str) -> str:
        while True:
            c = new_code(prefix)
            if not (self.db.one("SELECT 1 FROM projects WHERE code=?", c)
                    or self.db.one("SELECT 1 FROM slips WHERE code=?", c)):
                return c

    def project(self, pid: int) -> dict:
        p = self.db.one("SELECT * FROM projects WHERE id=?", pid)
        if not p:
            raise PassError(f"project {pid} not found", 404)
        return p

    def task(self, tid: int) -> dict:
        t = self.db.one("SELECT * FROM tasks WHERE id=?", tid)
        if not t:
            raise PassError(f"task {tid} not found", 404)
        return t

    def tasks_of(self, pid: int) -> list[dict]:
        return self.db.all("SELECT * FROM tasks WHERE project_id=? ORDER BY position, id", pid)

    def project_view(self, pid: int) -> dict:
        p = self.project(pid)
        p["tasks"] = self.tasks_of(pid)
        p["open_slip"] = self.db.one(
            "SELECT code, task_id, created_at FROM slips WHERE project_id=? AND kind='task' "
            "AND status='open'", pid)
        p["retro"] = self.db.one("SELECT * FROM retros WHERE project_id=? ORDER BY id DESC", pid)
        return p

    def _task_payload(self, t: dict) -> dict:
        p = self.db.one("SELECT id, title, code, status, source, external_id FROM projects WHERE id=?",
                        t["project_id"])
        return {"task": t, "project": p}

    def _set_project_status(self, p: dict, new: str, **stamps):
        if p["status"] == new and not stamps:
            return
        sets = ["status=?", "updated_at=?"] + [f"{k}=?" for k in stamps]
        self.db.run(f"UPDATE projects SET {', '.join(sets)} WHERE id=?",
                    new, now(), *stamps.values(), p["id"])
        old = p["status"]
        p.update(status=new, **stamps)
        if old != new:
            self.events.emit("project.status_changed", {"project": self.project(p["id"]),
                                                        "from": old, "to": new})

    def _set_task_status(self, t: dict, new: str, **stamps):
        sets = ["status=?", "updated_at=?"] + [f"{k}=?" for k in stamps]
        self.db.run(f"UPDATE tasks SET {', '.join(sets)} WHERE id=?", new, now(),
                    *stamps.values(), t["id"])
        old = t["status"]
        t = self.task(t["id"])
        if old != new:
            self.events.emit("task.status_changed", {**self._task_payload(t), "from": old, "to": new})
        return t

    def _print(self, device: str, kind: str, img, name: str, ref: str = "") -> str:
        path = self.printers.output(device, kind, img, name)
        self.db.run("INSERT INTO printouts(device,kind,path,ref,created_at) VALUES(?,?,?,?,?)",
                    device, kind, str(path), ref, now())
        return str(path)

    # ------------------------------------------------------------ capture
    def create_project(self, title: str, tasks: list | None = None, notes: str = "",
                       source: str = "", external_id: str = "", print_card: bool = False) -> dict:
        title = (title or "").strip()
        if not title:
            raise PassError("title is required")
        with self.lock, self.db.tx():
            if external_id:
                ex = self.db.one("SELECT id FROM projects WHERE source=? AND external_id=?",
                                 source, external_id)
                if ex:
                    return {**self.project_view(ex["id"]), "duplicate": True}
            ts = now()
            pid = self.db.run(
                "INSERT INTO projects(code,title,notes,status,source,external_id,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?)", self._code("C"), title, notes or "", "backlog",
                source or "", external_id or "", ts, ts)
            self.events.emit("project.created", {"project": self.project(pid)})
            for t in tasks or []:
                if isinstance(t, str):
                    t = {"title": t}
                self.add_task(pid, **t)
            if print_card:
                self.print_card(pid)
            return self.project_view(pid)

    def add_task(self, project_id: int, title: str, first_step: str = "", source: str = "",
                 external_id: str = "", position: int | None = None) -> dict:
        title = (title or "").strip()
        if not title:
            raise PassError("task title is required")
        with self.lock, self.db.tx():
            p = self.project(project_id)
            if p["status"] == "closed":
                raise PassError("that project is closed", 409)
            if external_id:
                ex = self.db.one("SELECT * FROM tasks WHERE source=? AND external_id=?",
                                 source, external_id)
                if ex:
                    return {**ex, "duplicate": True}
            if position is None:
                row = self.db.one("SELECT COALESCE(MAX(position),0)+1 AS n FROM tasks WHERE project_id=?",
                                  project_id)
                position = row["n"]
            status = "backlog" if p["status"] == "backlog" else "todo"
            ts = now()
            tid = self.db.run(
                "INSERT INTO tasks(project_id,title,first_step,position,status,source,external_id,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)", project_id, title,
                first_step or "", position, status, source or "", external_id or "", ts, ts)
            t = self.task(tid)
            self.events.emit("task.created", self._task_payload(t))
            if p["status"] == "ready_to_close":
                # new work on a card waiting for its retro: card goes back to in progress,
                # the old retro slip is void.
                self.db.run("UPDATE slips SET status='void', resolved_at=? WHERE project_id=? "
                            "AND kind='retro' AND status='open'", now(), project_id)
                self._set_project_status(p, "in_progress", ready_at=None)
            return t

    def capture(self, title: str, project_id: int | None = None, first_step: str = "",
                source: str = "", external_id: str = "", notes: str = "") -> dict:
        """Title-only capture. Without project_id it becomes a one-step project
        (one card, one slip)."""
        if project_id:
            return {"task": self.add_task(project_id, title, first_step, source, external_id)}
        p = self.create_project(title, [{"title": title, "first_step": first_step}], notes=notes,
                                source=source, external_id=external_id)
        return {"project": p, "task": p["tasks"][0] if p["tasks"] else None}

    def update_project(self, pid: int, **fields) -> dict:
        allowed = {k: v for k, v in fields.items() if k in ("title", "notes") and v is not None}
        with self.lock, self.db.tx():
            self.project(pid)
            if allowed:
                sets = ", ".join(f"{k}=?" for k in allowed)
                self.db.run(f"UPDATE projects SET {sets}, updated_at=? WHERE id=?",
                            *allowed.values(), now(), pid)
                self.events.emit("project.updated", {"project": self.project(pid), "changed": list(allowed)})
            return self.project_view(pid)

    def update_task(self, tid: int, **fields) -> dict:
        allowed = {k: v for k, v in fields.items()
                   if k in ("title", "first_step", "position") and v is not None}
        with self.lock, self.db.tx():
            self.task(tid)
            if allowed:
                sets = ", ".join(f"{k}=?" for k in allowed)
                self.db.run(f"UPDATE tasks SET {sets}, updated_at=? WHERE id=?",
                            *allowed.values(), now(), tid)
                self.events.emit("task.updated", {**self._task_payload(self.task(tid)),
                                                  "changed": list(allowed)})
            return self.task(tid)

    # ------------------------------------------------------------ printing
    def print_card(self, pid: int) -> dict:
        with self.lock, self.db.tx():
            p = self.project(pid)
            if p["status"] == "closed":
                raise PassError("that project is closed", 409)
            n = len(self.tasks_of(pid))
            path = self._print("label", "card", render.card_label(
                code=p["code"], title=p["title"], notes=p["notes"], steps=n), p["title"], p["code"])
            first = p["card_printed_at"] is None
            if p["status"] == "backlog":
                self.db.run("UPDATE tasks SET status='todo', updated_at=? WHERE project_id=? "
                            "AND status='backlog'", now(), pid)
                self._set_project_status(p, "todo", card_printed_at=now())
            elif first:
                self.db.run("UPDATE projects SET card_printed_at=? WHERE id=?", now(), pid)
            self.events.emit("project.card_printed", {"project": self.project(pid), "png": path,
                                                       "reprint": not first})
            return {"project": self.project_view(pid), "png": path}

    def print_reminder(self, title: str, body: str = "", when: str = "", checkoff: bool = False,
                       source: str = "") -> dict:
        return self._print_notice("reminder", title, body, when=when, checkoff=checkoff, source=source)

    def print_notification(self, title: str, body: str = "", checkoff: bool = False,
                           source: str = "") -> dict:
        return self._print_notice("notification", title, body, checkoff=checkoff, source=source)

    def _print_notice(self, kind, title, body, when="", checkoff=False, source=""):
        if not (title or "").strip():
            raise PassError("title is required")
        with self.lock, self.db.tx():
            code = self._code("S")
            if kind == "reminder":
                img = render.reminder_slip(title=title, body=body, when=when,
                                           code=code if checkoff else None)
            else:
                img = render.notification_slip(title=title, body=body, source=source,
                                               code=code if checkoff else None)
            path = self._print("receipt", kind, img, title, code)
            sid = self.db.run("INSERT INTO slips(code,kind,status,title,body,created_at)"
                              " VALUES(?,?,?,?,?,?)", code, kind, "open" if checkoff else "info",
                              title, body or "", now())
            slip = self.db.one("SELECT * FROM slips WHERE id=?", sid)
            self.events.emit(f"{kind}.printed", {"slip": slip, "png": path, "source": source})
            return {"slip": slip, "png": path}

    def print_test(self, device: str) -> dict:
        if device not in ("receipt", "label"):
            raise PassError("device must be receipt or label")
        if device == "label":
            img = render.card_label(code="CTEST23", title="Label printer test", steps=0,
                                    notes="If this QR scans as CTEST23, cards are good.")
        else:
            img = render.notification_slip(
                title="Receipt printer test", heading="TEST", code="STEST23",
                body="Black bar edges should reach both sides. QR should scan as STEST23.")
        path = self._print(device, "test", img, f"{device}-test")
        a = self.printers.adapters[device]
        return {"png": path, "adapter": a.name, "target": a.target,
                "note": "sent in the background; see GET /api/printers for the result"}

    def _info_slip(self, kind, title, body="", project_id=None, task_id=None):
        code = self._code("S")
        self.db.run("INSERT INTO slips(code,kind,project_id,task_id,status,title,body,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?)", code, kind, project_id, task_id, "info", title,
                    body, now())
        return code

    # ------------------------------------------------------------ the scan loop
    def scan(self, raw: str) -> dict:
        code = normalize_code(raw)
        with self.lock, self.db.tx():
            if not code:
                res = {"ok": False, "action": "empty", "message": "Empty scan."}
            elif code.startswith("RETRO"):
                pid = int(code.split(":")[1]) if ":" in code else 0
                p = self.db.one("SELECT * FROM projects WHERE id=?", pid)
                res = ({"ok": True, "action": "retro_link", "project": p,
                        "message": f"Retro form for “{p['title']}”.",
                        "open_url": f"retro/{pid}"} if p else
                       {"ok": False, "action": "unknown", "message": "Unknown retro link."})
            elif (p := self.db.one("SELECT * FROM projects WHERE code=?", code)):
                res = self._scan_card(p)
            elif (s := self.db.one("SELECT * FROM slips WHERE code=?", code)):
                res = self._scan_slip(s)
            else:
                res = {"ok": False, "action": "unknown",
                       "message": f"“{code}” isn't a Pass code. Nothing changed."}
            res["code"] = code
            res.setdefault("printouts", [])
            self.db.run("INSERT INTO scans(code,action,message,created_at) VALUES(?,?,?,?)",
                        code, res["action"], res["message"], now())
            self.events.emit("scan.received", {k: v for k, v in res.items() if k != "printouts"})
            return res

    def _active_slips(self) -> list[dict]:
        return self.db.all(
            "SELECT s.code, s.project_id, s.task_id, s.created_at, t.title AS task_title, "
            "p.title AS project_title, p.code AS card_code FROM slips s "
            "JOIN tasks t ON t.id=s.task_id JOIN projects p ON p.id=s.project_id "
            "WHERE s.kind='task' AND s.status='open' ORDER BY s.id")

    def _scan_card(self, p: dict) -> dict:
        title = p["title"]
        if p["status"] == "closed":
            path = self._print("receipt", "notification", render.notification_slip(
                title=f"“{title}” is closed", body="Its retro is in. Nothing changed with this scan.",
                heading="CLOSED CARD"), title)
            self._info_slip("note", f"{title} is closed", project_id=p["id"])
            return {"ok": True, "action": "closed_card", "project": p, "printouts": [path],
                    "message": f"“{title}” is already closed. Nothing changed."}

        if p["status"] == "ready_to_close":
            retro = self.db.one("SELECT * FROM slips WHERE project_id=? AND kind='retro' AND "
                                "status='open' ORDER BY id DESC", p["id"])
            path = self._print_retro(p, retro["code"] if retro else None)
            return {"ok": True, "action": "retro_reprint", "project": p, "printouts": [path],
                    "open_url": f"retro/{p['id']}",
                    "message": f"“{title}” is waiting on its retro. Retro slip printed again."}

        open_slip = self.db.one("SELECT * FROM slips WHERE project_id=? AND kind='task' AND "
                                "status='open'", p["id"])
        if open_slip:  # put-back: task returns to the pot, its slip is void
            t = self.task(open_slip["task_id"])
            self.db.run("UPDATE slips SET status='void', resolved_at=? WHERE id=?", now(), open_slip["id"])
            self.db.run("UPDATE tasks SET put_backs=put_backs+1 WHERE id=?", t["id"])
            t = self._set_task_status(t, "todo", started_at=None)
            self.events.emit("slip.voided", {"slip_code": open_slip["code"], **self._task_payload(t)})
            self.events.emit("task.put_back", self._task_payload(t))
            return {"ok": True, "action": "put_back", "project": self.project(p["id"]), "task": t,
                    "message": f"Back in the pot: “{t['title']}”. Slip {open_slip['code']} is void now."}

        active = self._active_slips()
        if active and self.s.mid_task == "note":
            a = active[0]
            body = (f"“{a['task_title']}” ({a['project_title']}) is still out. "
                    f"Its slip finishes it; its card puts it back in the pot. "
                    f"“{title}” stays as it is, ready whenever.")
            path = self._print("receipt", "note", render.notification_slip(
                title="Already on the go", body=body, heading="HEADS UP"), "already-on-the-go")
            self._info_slip("note", "Already on the go", body, project_id=p["id"])
            return {"ok": True, "action": "mid_task_note", "project": p, "active": a,
                    "printouts": [path],
                    "message": f"Still on the go: “{a['task_title']}”. "
                               f"“{title}” stays as it is. Nothing changed."}

        tasks = self.tasks_of(p["id"])
        if not tasks:  # one-step project: the card itself is the step
            self.add_task(p["id"], title)
            tasks = self.tasks_of(p["id"])
        nxt = next((t for t in tasks if t["status"] in OPEN_TASK), None)
        if nxt is None:  # everything done but not marked ready (edge): go to retro
            return self._make_ready(p)

        t = self._set_task_status(nxt, "in_progress", started_at=now())
        stamps = {} if p["started_at"] else {"started_at": now()}
        self._set_project_status(p, "in_progress", **stamps)
        code = self._code("S")
        self.db.run("INSERT INTO slips(code,kind,project_id,task_id,status,title,created_at)"
                    " VALUES(?,?,?,?,?,?,?)", code, "task", p["id"], t["id"], "open", t["title"], now())
        idx = [x["id"] for x in tasks].index(t["id"]) + 1
        path = self._print("receipt", "task", render.task_slip(
            code=code, task=t["title"], project=title, first_step=t["first_step"],
            step=idx, steps=len(tasks)), t["title"], code)
        self.events.emit("task.started", {**self._task_payload(t), "slip_code": code})
        self.events.emit("slip.printed", {"slip_code": code, "kind": "task", "png": path,
                                          **self._task_payload(t)})
        return {"ok": True, "action": "started", "project": self.project(p["id"]), "task": t,
                "slip_code": code, "printouts": [path],
                "message": f"On the go: “{t['title']}” ({title}). Slip printed."}

    def _print_retro(self, p: dict, code: str | None = None) -> str:
        code = code or self._code("S")
        done = [t["title"] for t in self.tasks_of(p["id"]) if t["status"] == "done"]
        return self._print("receipt", "retro", render.retro_slip(
            project=p["title"], done_tasks=done, form_url=f"{self.s.base_url}/retro/{p['id']}",
            code=code), p["title"], code)

    def _make_ready(self, p: dict) -> dict:
        code = self._code("S")
        self.db.run("INSERT INTO slips(code,kind,project_id,status,title,created_at)"
                    " VALUES(?,?,?,?,?,?)", code, "retro", p["id"], "open", p["title"], now())
        self._set_project_status(p, "ready_to_close", ready_at=now())
        path = self._print_retro(p, code)
        self.events.emit("project.ready_to_close", {"project": self.project(p["id"]),
                                                    "retro_url": f"{self.s.base_url}/retro/{p['id']}"})
        return {"ok": True, "action": "ready_to_close", "project": self.project(p["id"]),
                "printouts": [path], "open_url": f"retro/{p['id']}",
                "message": f"Every slip on “{p['title']}” is done. Retro slip printed; "
                           f"the card stays out until the retro is in."}

    def _scan_slip(self, s: dict) -> dict:
        if s["kind"] == "task":
            t = self.task(s["task_id"])
            p = self.project(s["project_id"])
            if s["status"] == "open":
                self.db.run("UPDATE slips SET status='done', resolved_at=? WHERE id=?", now(), s["id"])
                t = self._set_task_status(t, "done", done_at=now())
                self.events.emit("task.done", {**self._task_payload(t), "slip_code": s["code"]})
                left = [x for x in self.tasks_of(p["id"]) if x["status"] != "done"]
                if not left:
                    r = self._make_ready(p)
                    r.update(task=t, action="done_ready_to_close",
                             message=f"Done: “{t['title']}”. " + r["message"])
                    return r
                return {"ok": True, "action": "done", "project": p, "task": t,
                        "message": f"Done: “{t['title']}”. {len(left)} more on “{p['title']}” "
                                   f"whenever."}
            if s["status"] == "void":
                path = self._print("receipt", "void", render.void_notice(
                    code=s["code"], task=t["title"], project=p["title"]), "void-" + s["code"])
                self._info_slip("void_notice", f"Slip {s['code']} is void", project_id=p["id"],
                                task_id=t["id"])
                return {"ok": True, "action": "void_slip", "project": p, "task": t,
                        "printouts": [path],
                        "message": f"Slip {s['code']} is void (its card was scanned again). "
                                   f"Nothing changed."}
            return {"ok": True, "action": "already_done", "project": p, "task": t,
                    "message": f"“{t['title']}” was already checked off. Nothing changed."}

        if s["kind"] == "retro":
            p = self.project(s["project_id"])
            if s["status"] == "open":
                return {"ok": True, "action": "retro_link", "project": p, "open_url": f"retro/{p['id']}",
                        "message": f"Retro for “{p['title']}” goes in the form."}
            return {"ok": True, "action": "retro_slip_inactive", "project": p,
                    "message": "That retro slip is no longer open. Nothing changed."}

        if s["kind"] in ("reminder", "notification") and s["status"] == "open":
            self.db.run("UPDATE slips SET status='done', resolved_at=? WHERE id=?", now(), s["id"])
            slip = self.db.one("SELECT * FROM slips WHERE id=?", s["id"])
            self.events.emit("reminder.acknowledged", {"slip": slip})
            return {"ok": True, "action": "acknowledged", "slip": slip,
                    "message": f"Handled: “{s['title']}”."}
        return {"ok": True, "action": "info_slip", "slip": s,
                "message": "That's an info slip. Nothing to check off."}

    # ------------------------------------------------------------ retro
    def record_retro(self, pid: int, went_well: str = "", was_hard: str = "", next_time: str = "",
                     notes: str = "") -> dict:
        with self.lock, self.db.tx():
            p = self.project(pid)
            if p["status"] == "closed":
                raise PassError("that project is already closed", 409)
            if p["status"] != "ready_to_close":
                left = [t for t in self.tasks_of(pid) if t["status"] != "done"]
                if left or not self.tasks_of(pid):
                    raise PassError("the retro opens after the last slip on this card is done", 409)
            rid = self.db.run("INSERT INTO retros(project_id,went_well,was_hard,next_time,notes,created_at)"
                              " VALUES(?,?,?,?,?,?)", pid, went_well or "", was_hard or "",
                              next_time or "", notes or "", now())
            self.db.run("UPDATE slips SET status='done', resolved_at=? WHERE project_id=? AND "
                        "kind='retro' AND status='open'", now(), pid)
            retro = self.db.one("SELECT * FROM retros WHERE id=?", rid)
            self.events.emit("retro.recorded", {"project": p, "retro": retro})
            self._set_project_status(p, "closed", closed_at=now())
            self.events.emit("project.closed", {"project": self.project(pid), "retro": retro})
            return self.project_view(pid)

    # ------------------------------------------------------------ webhooks
    def add_webhook(self, url: str, events: str = "*", secret: str = "") -> dict:
        if not re.match(r"^https?://", url or ""):
            raise PassError("url must start with http:// or https://")
        wid = self.db.run("INSERT INTO webhooks(url,secret,events,active,created_at) VALUES(?,?,?,?,?)",
                          url, secret or "", events or "*", 1, now())
        return self.db.one("SELECT * FROM webhooks WHERE id=?", wid)

    # ------------------------------------------------------------ views
    def state(self) -> dict:
        active = self._active_slips()
        cur = None
        if active:
            a = active[0]
            tasks = self.tasks_of(a["project_id"])
            t = next(x for x in tasks if x["id"] == a["task_id"])
            idx = [x["id"] for x in tasks].index(t["id"])
            after = next((x for x in tasks[idx + 1:] if x["status"] in OPEN_TASK), None) or \
                next((x for x in tasks if x["status"] in OPEN_TASK), None)
            cur = {**a, "first_step": t["first_step"], "step": idx + 1, "steps": len(tasks),
                   "started_at": t["started_at"], "after": after["title"] if after else None}
        projects = self.db.all("SELECT * FROM projects WHERE status!='closed' ORDER BY id")
        for p in projects:
            ts = self.tasks_of(p["id"])
            p["open_count"] = sum(t["status"] != "done" for t in ts)
            p["task_count"] = len(ts)
            nxt = next((t for t in ts if t["status"] in OPEN_TASK), None)
            p["next_open"] = nxt["title"] if nxt else None
        group = lambda st: [p for p in projects if p["status"] == st]  # noqa: E731
        done = self.db.all(
            "SELECT t.id, t.title, t.done_at, p.title AS project_title FROM tasks t "
            "JOIN projects p ON p.id=t.project_id WHERE t.status='done' "
            "ORDER BY t.done_at DESC, t.id DESC LIMIT 10")
        closed = self.db.all("SELECT id, title, closed_at FROM projects WHERE status='closed' "
                             "ORDER BY closed_at DESC, id DESC LIMIT 6")
        last = self.db.one("SELECT * FROM scans ORDER BY id DESC LIMIT 1")
        return {"now": cur, "also_active": active[1:], "in_progress": group("in_progress"),
                "todo": group("todo"), "backlog": group("backlog"),
                "ready_to_close": group("ready_to_close"), "done": done, "closed": closed,
                "last_scan": last, "server_time": now()}
