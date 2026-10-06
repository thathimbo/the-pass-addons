"""HTTP layer: JSON API (for Zapier/IFTTT/n8n/Home Assistant) + kitchen web pages."""
from __future__ import annotations

import html
from pathlib import Path
from typing import Optional, Union
from urllib.parse import parse_qs

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import Settings
from .core import Pass, PassError

STATIC = Path(__file__).parent / "static"


class TaskIn(BaseModel):
    title: str
    first_step: str = ""
    source: str = ""
    external_id: str = ""


class ProjectIn(BaseModel):
    title: str
    notes: str = ""
    tasks: list[Union[str, TaskIn]] = []
    source: str = ""
    external_id: str = ""
    print_card: bool = False


class CaptureIn(BaseModel):
    title: str
    project_id: Optional[int] = None
    first_step: str = ""
    notes: str = ""
    source: str = ""
    external_id: str = ""


class ProjectPatch(BaseModel):
    title: Optional[str] = None
    notes: Optional[str] = None


class TaskPatch(BaseModel):
    title: Optional[str] = None
    first_step: Optional[str] = None
    position: Optional[int] = None


class ScanIn(BaseModel):
    code: str


class RetroIn(BaseModel):
    went_well: str = ""
    was_hard: str = ""
    next_time: str = ""
    notes: str = ""


class ReminderIn(BaseModel):
    title: str
    body: str = ""
    when: str = ""
    checkoff: bool = False
    source: str = ""


class NotificationIn(BaseModel):
    title: str
    body: str = ""
    checkoff: bool = False
    source: str = ""


class PrinterTestIn(BaseModel):
    device: str = "receipt"   # receipt | label


class WebhookIn(BaseModel):
    url: str
    events: str = "*"
    secret: str = ""


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    core = Pass(settings)
    app = FastAPI(title="The Pass", version="0.1.0",
                  description="Scan-only kitchen-island task loop. Cards = projects, slips = tasks.")
    app.state.core = core
    app.state.settings = settings
    settings.out_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/out", StaticFiles(directory=str(settings.out_dir)), name="out")

    def auth(request: Request):
        if not settings.token:
            return
        # Requests through Home Assistant ingress are already authenticated by HA.
        if request.headers.get("x-ingress-path") and request.client and \
                request.client.host == "172.30.32.2":
            return
        tok = (request.headers.get("x-pass-token")
               or request.headers.get("authorization", "").removeprefix("Bearer ").strip()
               or request.query_params.get("token"))
        if tok != settings.token:
            raise HTTPException(401, "missing or wrong token")

    @app.exception_handler(PassError)
    async def _pass_error(_req, exc: PassError):
        return JSONResponse({"ok": False, "error": exc.message}, status_code=exc.status)

    # ---------------------------------------------------------- pages
    @app.get("/", include_in_schema=False)
    @app.get("/projector", include_in_schema=False)
    def projector():
        return FileResponse(STATIC / "projector.html")

    @app.get("/scanner", include_in_schema=False)
    def scanner():
        return FileResponse(STATIC / "scanner.html")

    @app.get("/desk", include_in_schema=False)
    def desk():
        return FileResponse(STATIC / "desk.html")

    def root(request: Request) -> str:
        """URL prefix: '' normally, '/api/hassio_ingress/<token>' behind HA ingress."""
        return request.headers.get("x-ingress-path", "").rstrip("/")

    @app.get("/retro", response_class=HTMLResponse, include_in_schema=False)
    def retro_index(request: Request):
        r = root(request)
        rows = core.db.all("SELECT * FROM projects WHERE status='ready_to_close' ORDER BY ready_at")
        items = "".join(f'<li><a href="{r}/retro/{p["id"]}">{html.escape(p["title"])}</a></li>'
                        for p in rows) or "<li>Nothing waiting on a retro.</li>"
        return _page("Retros", f"<h1>Waiting on a retro</h1><ul class=big>{items}</ul>")

    @app.get("/retro/{pid}", response_class=HTMLResponse, include_in_schema=False)
    def retro_form(pid: int, request: Request):
        r0 = root(request)
        p = core.project_view(pid)
        title = html.escape(p["title"])
        done = "".join(f"<li>✓ {html.escape(t['title'])}</li>" for t in p["tasks"] if t["status"] == "done")
        if p["status"] == "closed":
            r = p["retro"] or {}
            body = "".join(f"<h3>{q}</h3><p>{html.escape(r.get(k, '') or '—')}</p>" for k, q in _PROMPTS)
            return _page("Retro", f"<h1>{title}</h1><p class=ok>Closed. Retro is in.</p>{body}"
                                  f'<p><a href="{r0}/">back to the pass</a></p>')
        if p["status"] != "ready_to_close":
            left = sum(t["status"] != "done" for t in p["tasks"])
            return _page("Retro", f"<h1>{title}</h1><p>The retro opens after the last slip on this "
                                  f"card is done ({left} still open).</p>")
        fields = "".join(f'<label>{q}<textarea name="{k}" rows=3></textarea></label>' for k, q in _PROMPTS)
        return _page("Retro", f"""<h1>Ready to close: {title}</h1><ul>{done}</ul>
<form method=post action="{r0}/retro/{pid}">{fields}
<label>Anything else<textarea name="notes" rows=2></textarea></label>
<p class=hint>Any or none. Saving closes the card.</p>
<button type=submit>Save retro and close the card</button></form>""")

    @app.post("/retro/{pid}", include_in_schema=False)
    async def retro_submit(pid: int, request: Request):
        form = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
        core.record_retro(pid, **{k: form.get(k, "") for k in ("went_well", "was_hard", "next_time", "notes")})
        return RedirectResponse(f"{root(request)}/retro/{pid}", status_code=303)

    # ---------------------------------------------------------- scan + state (kitchen, no token)
    @app.post("/scan", tags=["scan"])
    @app.post("/api/scan", tags=["scan"])
    def scan(body: ScanIn):
        """Scanner input. Cards, slips, and retro-slip URLs are all accepted."""
        return core.scan(body.code)

    @app.get("/api/state", tags=["views"])
    def state():
        """Everything the projector shows."""
        return core.state()

    @app.get("/api/health", tags=["views"])
    def health():
        return {"ok": True, "receipt": settings.receipt_printer, "label": settings.label_printer,
                "mid_task": settings.mid_task, "base_url": settings.base_url}

    # ---------------------------------------------------------- projects / tasks
    @app.get("/api/projects", tags=["projects"])
    def list_projects(status: Optional[str] = None):
        rows = (core.db.all("SELECT * FROM projects WHERE status=? ORDER BY id", status) if status
                else core.db.all("SELECT * FROM projects ORDER BY id"))
        for p in rows:
            p["tasks"] = core.tasks_of(p["id"])
        return rows

    @app.post("/api/projects", tags=["projects"], dependencies=[Depends(auth)], status_code=201)
    def create_project(body: ProjectIn):
        tasks = [t if isinstance(t, str) else t.model_dump() for t in body.tasks]
        return core.create_project(body.title, tasks, body.notes, body.source, body.external_id,
                                   body.print_card)

    @app.get("/api/projects/{pid}", tags=["projects"])
    def get_project(pid: int):
        return core.project_view(pid)

    @app.patch("/api/projects/{pid}", tags=["projects"], dependencies=[Depends(auth)])
    def patch_project(pid: int, body: ProjectPatch):
        return core.update_project(pid, **body.model_dump())

    @app.post("/api/projects/{pid}/tasks", tags=["projects"], dependencies=[Depends(auth)], status_code=201)
    def add_task(pid: int, body: TaskIn):
        return core.add_task(pid, body.title, body.first_step, body.source, body.external_id)

    @app.post("/api/projects/{pid}/print-card", tags=["printing"], dependencies=[Depends(auth)])
    def print_card(pid: int):
        """Print (or reprint) the project's card on the label printer. backlog -> todo."""
        return core.print_card(pid)

    @app.post("/api/projects/{pid}/retro", tags=["projects"], dependencies=[Depends(auth)])
    def retro(pid: int, body: RetroIn):
        """Record the retrospective; this is what closes a project."""
        return core.record_retro(pid, **body.model_dump())

    @app.get("/api/tasks", tags=["tasks"])
    def list_tasks(status: Optional[str] = None):
        q = ("SELECT t.*, p.title AS project_title, p.code AS card_code FROM tasks t "
             "JOIN projects p ON p.id=t.project_id")
        return (core.db.all(q + " WHERE t.status=? ORDER BY t.id", status) if status
                else core.db.all(q + " ORDER BY t.id"))

    @app.post("/api/tasks", tags=["tasks"], dependencies=[Depends(auth)], status_code=201)
    def capture(body: CaptureIn):
        """Title-only capture (Zapier/IFTTT/n8n). No project_id -> one-step project in Backlog."""
        return core.capture(body.title, body.project_id, body.first_step, body.source,
                            body.external_id, body.notes)

    @app.get("/api/tasks/{tid}", tags=["tasks"])
    def get_task(tid: int):
        return core.task(tid)

    @app.patch("/api/tasks/{tid}", tags=["tasks"], dependencies=[Depends(auth)])
    def patch_task(tid: int, body: TaskPatch):
        return core.update_task(tid, **body.model_dump())

    # ---------------------------------------------------------- receipts for Home Assistant
    @app.post("/api/print/reminder", tags=["printing"], dependencies=[Depends(auth)])
    def print_reminder(body: ReminderIn):
        return core.print_reminder(body.title, body.body, body.when, body.checkoff, body.source)

    @app.post("/api/print/notification", tags=["printing"], dependencies=[Depends(auth)])
    def print_notification(body: NotificationIn):
        return core.print_notification(body.title, body.body, body.checkoff, body.source)

    @app.get("/api/printers", tags=["printing"])
    def printers_status():
        """Adapter + reachability per device, and the last send results."""
        return {"devices": core.printers.status(), "recent": core.printers.history[-15:][::-1]}

    @app.post("/api/printers/test", tags=["printing"], dependencies=[Depends(auth)])
    def printer_test(body: PrinterTestIn):
        """Print a test card (label) or test slip (receipt). Nothing else changes."""
        return core.print_test(body.device)

    @app.get("/api/printouts", tags=["printing"])
    def printouts(limit: int = 30):
        rows = core.db.all("SELECT * FROM printouts ORDER BY id DESC LIMIT ?", limit)
        for r in rows:
            r["url"] = "/out/" + Path(r["path"]).name if Path(r["path"]).parent == settings.out_dir else None
        return rows

    # ---------------------------------------------------------- webhooks + events
    @app.get("/api/webhooks", tags=["webhooks"], dependencies=[Depends(auth)])
    def list_webhooks():
        return core.db.all("SELECT * FROM webhooks ORDER BY id")

    @app.post("/api/webhooks", tags=["webhooks"], dependencies=[Depends(auth)], status_code=201)
    def add_webhook(body: WebhookIn):
        return core.add_webhook(body.url, body.events, body.secret)

    @app.delete("/api/webhooks/{wid}", tags=["webhooks"], dependencies=[Depends(auth)])
    def delete_webhook(wid: int):
        core.db.run("DELETE FROM webhooks WHERE id=?", wid)
        return {"ok": True}

    @app.post("/api/webhooks/{wid}/test", tags=["webhooks"], dependencies=[Depends(auth)])
    def test_webhook(wid: int):
        eid = core.events.emit("webhook.test", {"webhook_id": wid, "message": "hello from The Pass"})
        return {"ok": True, "event_id": eid}

    @app.get("/api/webhooks/deliveries", tags=["webhooks"], dependencies=[Depends(auth)])
    def deliveries(limit: int = 50):
        return core.db.all("SELECT * FROM deliveries ORDER BY id DESC LIMIT ?", limit)

    @app.get("/api/events", tags=["webhooks"])
    def events(since_id: int = 0, limit: int = 100):
        """Pollable event feed (alternative to webhooks for n8n/Zapier polling triggers)."""
        import json
        rows = core.db.all("SELECT * FROM events WHERE id>? ORDER BY id LIMIT ?", since_id, limit)
        return [{"id": r["id"], "event": r["type"], "at": r["created_at"],
                 "data": json.loads(r["payload"])} for r in rows]

    return app


_PROMPTS = [("went_well", "What went well?"), ("was_hard", "What was harder than expected?"),
            ("next_time", "Anything to remember next time?")]


def _page(title: str, body: str) -> str:
    return f"""<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>{title} · The Pass</title>
<style>
body{{background:#121212;color:#eee;font:20px/1.45 Inter,"DejaVu Sans",system-ui,sans-serif;
max-width:720px;margin:0 auto;padding:28px}}
h1{{font-size:32px;margin:0 0 12px}} a{{color:#ffd166}} ul{{padding-left:22px}} .big li{{font-size:26px;margin:8px 0}}
label{{display:block;margin:18px 0 0;font-weight:600}}
textarea{{width:100%;box-sizing:border-box;margin-top:6px;font:inherit;background:#1e1e1e;color:#eee;
border:1px solid #444;border-radius:8px;padding:10px}}
button{{margin-top:18px;font:inherit;font-weight:700;padding:14px 22px;border-radius:10px;border:0;
background:#ffd166;color:#111}} .hint{{color:#aaa}} .ok{{color:#8ce99a;font-weight:700}}
</style></head><body>{body}</body></html>"""
