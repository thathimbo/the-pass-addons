"""Printer adapter layer.

Every printout is ALWAYS rendered to a PNG under out/ first (archive + preview). Then it
goes to the adapter configured for that device (receipt = slips, label = cards):

  png                                  nothing else (default)
  escpos://HOST[:PORT]?opts            ESC/POS raster (GS v 0) over raw TCP (default 9100)
        opts: width=576 cut=partial|full|none feed=3 band=256 threshold=160 retries=3 timeout=10
  tspl:/dev/usb/lp0?opts               TSPL BITMAP job written to a USB printer device node
  tspl:auto?opts                       first /dev/usb/lp* that exists
  tspl://HOST[:PORT]?opts              same TSPL job over raw TCP (network label printers)
        opts: width_mm=100 height_mm=150 gap_mm=3 density=10 speed=5 direction=0
              invert=0 threshold=160 dpmm=8 tear=1
  cups://QUEUE[@HOST:PORT]             `lp [-h HOST:PORT] -d QUEUE -o fit-to-page file.png`

TSPL defaults mirror Polono's own PL80E CUPS filter: SIZE 100x150 mm, GAP 3 mm, SPEED 5,
DENSITY 10, DIRECTION 0,0, REFERENCE 0,0, SET TEAR ON, CLS, BITMAP x,y,wbytes,h,1,<data>,
PRINT 1,n. BITMAP data is 1 bit per dot, MSB first, bit 0 = burn a dot (TSC spec).

Sending runs on one background worker so a slow/offline printer never blocks a scan,
and printouts keep their order. Failures are logged and kept in `history`.
"""
from __future__ import annotations

import datetime as dt
import glob
import itertools
import logging
import os
import queue
import re
import socket
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from PIL import Image

log = logging.getLogger("pass.printers")


# ------------------------------------------------------------ image prep
def fit_to(img: Image.Image, w: int, h: int | None = None) -> Image.Image:
    """Grayscale, scaled to width w (keeping aspect) and, if h is given, centered on a
    w x h white canvas. Same-size images pass through untouched (crisp QR modules)."""
    img = img.convert("L")
    if img.width != w:
        nh = max(1, round(img.height * w / img.width))
        if h is not None and nh > h:
            nw = max(1, round(img.width * h / img.height))
            img = img.resize((nw, h), Image.LANCZOS)
        else:
            img = img.resize((w, nh), Image.LANCZOS)
    if h is not None and img.size != (w, h):
        canvas = Image.new("L", (w, h), 255)
        canvas.paste(img.crop((0, 0, min(img.width, w), min(img.height, h))),
                     ((w - min(img.width, w)) // 2, (h - min(img.height, h)) // 2))
        img = canvas
    return img


def to_1bit(img: Image.Image, threshold: int = 160) -> Image.Image:
    """Hard threshold (no dither): text and QR stay crisp on thermal paper."""
    return img.convert("L").point(lambda v: 255 if v >= threshold else 0, mode="1")


# ------------------------------------------------------------ ESC/POS
def escpos_raster_bytes(img: Image.Image, cut: str | bool = "partial", band: int = 256,
                        width: int | None = None, threshold: int = 160, feed: int = 3) -> bytes:
    """init, GS v 0 raster bands, feed, cut."""
    if cut is True:
        cut = "partial"
    elif cut is False:
        cut = "none"
    img = img.convert("L")
    if width and img.width != width:
        img = fit_to(img, width)
    w = (img.width + 7) // 8 * 8
    if w != img.width:
        padded = Image.new("L", (w, img.height), 255)
        padded.paste(img, (0, 0))
        img = padded
    bw = to_1bit(img, threshold)
    out = bytearray(b"\x1b@")  # ESC @ init
    bpr = w // 8
    for top in range(0, bw.height, band):
        part = bw.crop((0, top, w, min(top + band, bw.height)))
        raw = bytes(b ^ 0xFF for b in part.tobytes())  # PIL: 1=white, ESC/POS: 1=black
        h = part.height
        out += b"\x1dv0\x00" + bytes([bpr & 0xFF, bpr >> 8, h & 0xFF, h >> 8]) + raw
    if feed:
        out += b"\x1bd" + bytes([max(0, min(255, feed))])  # ESC d n: feed n lines
    if cut == "partial":
        out += b"\x1dV\x42\x00"  # GS V 66 0: feed to cutter + partial cut
    elif cut == "full":
        out += b"\x1dV\x41\x00"  # GS V 65 0: feed to cutter + full cut
    return bytes(out)


def _send_tcp(host: str, port: int, data: bytes, timeout: float, retries: int,
              chunk: int = 16384) -> None:
    last: Exception | None = None
    for attempt in range(max(1, retries)):
        try:
            with socket.create_connection((host, port), timeout=timeout) as s:
                for i in range(0, len(data), chunk):
                    s.sendall(data[i:i + chunk])
                # half-close and let the printer drain before we drop the socket
                try:
                    s.shutdown(socket.SHUT_WR)
                    s.settimeout(1.5)
                    while s.recv(1024):
                        pass
                except OSError:
                    pass
            return
        except OSError as e:  # refused (busy with another client), timeout, unreachable
            last = e
            if attempt + 1 < retries:
                time.sleep(min(8, 2 ** attempt))
    raise ConnectionError(f"{host}:{port} unreachable after {retries} tries: {last}")


class PngOnly:
    name = "png"
    target = "out/ only"

    def send(self, path, img) -> str:
        return "png only"

    def status(self) -> dict:
        return {"adapter": "png", "ok": True}


class EscPosNetwork:
    name = "escpos"

    def __init__(self, host: str, port: int = 9100, width: int = 576, cut: str = "partial",
                 feed: int = 3, band: int = 256, threshold: int = 160, retries: int = 3,
                 timeout: float = 10.0):
        self.host, self.port = host, port
        self.width, self.cut, self.feed, self.band = width, cut, feed, band
        self.threshold, self.retries, self.timeout = threshold, retries, timeout
        self.target = f"{host}:{port}"

    def job(self, img: Image.Image) -> bytes:
        return escpos_raster_bytes(img, cut=self.cut, band=self.band, width=self.width,
                                   threshold=self.threshold, feed=self.feed)

    def send(self, path, img) -> str:
        data = self.job(img)
        _send_tcp(self.host, self.port, data, self.timeout, self.retries)
        return f"sent {len(data)} bytes to {self.target}"

    def status(self) -> dict:
        """Best effort: TCP connect + DLE EOT 4 (paper sensor). Many printers answer;
        some don't, which is still 'reachable'."""
        st = {"adapter": "escpos", "target": self.target}
        try:
            with socket.create_connection((self.host, self.port), timeout=3) as s:
                s.sendall(b"\x10\x04\x04")
                s.settimeout(1.5)
                try:
                    b = s.recv(1)
                    if b:
                        st["paper_end"] = bool(b[0] & 0x60)
                        st["paper_near_end"] = bool(b[0] & 0x0C)
                except OSError:
                    pass
            st["ok"] = True
        except OSError as e:
            st.update(ok=False, error=str(e))
        return st


# ------------------------------------------------------------ TSPL (Polono PL80E etc.)
def _num(v: float) -> str:
    return f"{v:g}"


def tspl_bitmap_job(img: Image.Image, width_mm: float = 100, height_mm: float = 150,
                    gap_mm: float = 3, density: int = 10, speed: int = 5, direction: int = 0,
                    invert: bool = False, threshold: int = 160, dpmm: int = 8,
                    copies: int = 1, tear: bool = True) -> bytes:
    """One full-label TSPL job. The image is fitted to the label's dot grid
    (100 x 150 mm @ 8 dots/mm = 800 x 1200; the card renderer already draws at that size)."""
    w = int(width_mm * dpmm) // 8 * 8
    h = int(height_mm * dpmm)
    bw = to_1bit(fit_to(img, w, h), threshold)
    data = bw.tobytes()  # PIL '1': bit 1 = white = no dot, MSB first: exactly TSPL polarity
    if invert:           # for firmware that expects 1 = dot
        data = bytes(b ^ 0xFF for b in data)
    lines = [
        f"SIZE {_num(width_mm)} mm,{_num(height_mm)} mm",
        f"GAP {_num(gap_mm)} mm,0 mm",
        f"DIRECTION {direction},0",
        "REFERENCE 0,0",
        "SET TEAR ON" if tear else "SET TEAR OFF",
        f"SPEED {speed}",
        f"DENSITY {density}",
        "CLS",
    ]
    head = ("\r\n".join(lines) + "\r\n" + f"BITMAP 0,0,{w // 8},{h},1,").encode("ascii")
    return head + data + f"\r\nPRINT 1,{copies}\r\n".encode("ascii")


class Tspl:
    name = "tspl"

    def __init__(self, device: str | None = None, host: str | None = None, port: int = 9100,
                 retries: int = 3, timeout: float = 10.0, **job_opts):
        self.device, self.host, self.port = device, host, port
        self.retries, self.timeout = retries, timeout
        self.job_opts = job_opts
        self.target = f"{host}:{port}" if host else (device or "auto")

    def resolve_device(self) -> str:
        if self.device and self.device != "auto":
            return self.device
        found = sorted(glob.glob("/dev/usb/lp*"))
        if not found:
            raise FileNotFoundError("no /dev/usb/lp* device (label printer unplugged or not mapped)")
        return found[0]

    def job(self, img: Image.Image) -> bytes:
        return tspl_bitmap_job(img, **self.job_opts)

    def send(self, path, img) -> str:
        data = self.job(img)
        if self.host:
            _send_tcp(self.host, self.port, data, self.timeout, self.retries)
            return f"sent {len(data)} bytes to {self.target}"
        dev = self.resolve_device()
        last = None
        for attempt in range(max(1, self.retries)):
            try:
                fd = os.open(dev, os.O_WRONLY)
                try:
                    view = memoryview(data)
                    while view:
                        n = os.write(fd, view[:16384])
                        view = view[n:]
                finally:
                    os.close(fd)
                return f"wrote {len(data)} bytes to {dev}"
            except OSError as e:  # EBUSY / ENODEV during a USB re-enumeration
                last = e
                time.sleep(min(8, 2 ** attempt))
        raise OSError(f"{dev}: {last}")

    def status(self) -> dict:
        st = {"adapter": "tspl", "target": self.target}
        if self.host:
            try:
                socket.create_connection((self.host, self.port), timeout=3).close()
                st["ok"] = True
            except OSError as e:
                st.update(ok=False, error=str(e))
            return st
        try:
            dev = self.resolve_device()
            st.update(device=dev, exists=os.path.exists(dev), writable=os.access(dev, os.W_OK))
            st["ok"] = st["exists"] and st["writable"]
        except FileNotFoundError as e:
            st.update(ok=False, error=str(e))
        return st


# ------------------------------------------------------------ CUPS fallback
class Cups:
    name = "cups"

    def __init__(self, queue_name: str, server: str | None = None):
        self.queue, self.server = queue_name, server
        self.target = f"{queue_name}@{server}" if server else queue_name

    def cmd(self, path) -> list[str]:
        c = ["lp"]
        if self.server:
            c += ["-h", self.server]
        return c + ["-d", self.queue, "-o", "fit-to-page", str(path)]

    def send(self, path, img) -> str:
        r = subprocess.run(self.cmd(path), capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout).strip() or f"lp exit {r.returncode}")
        return r.stdout.strip()

    def status(self) -> dict:
        c = ["lpstat"] + (["-h", self.server] if self.server else []) + ["-p", self.queue]
        try:
            r = subprocess.run(c, capture_output=True, text=True, timeout=10)
            return {"adapter": "cups", "target": self.target, "ok": r.returncode == 0,
                    "detail": (r.stdout or r.stderr).strip()}
        except (OSError, subprocess.TimeoutExpired) as e:
            return {"adapter": "cups", "target": self.target, "ok": False, "error": str(e)}


# ------------------------------------------------------------ factory
_INT = {"port", "width", "feed", "band", "threshold", "retries", "density", "speed",
        "direction", "dpmm", "copies"}
_FLOAT = {"timeout", "width_mm", "height_mm", "gap_mm"}
_BOOL = {"invert", "tear"}


def _opts(query: str) -> dict:
    out = {}
    for k, v in parse_qs(query).items():
        v = v[-1]
        if k in _INT:
            out[k] = int(v)
        elif k in _FLOAT:
            out[k] = float(v)
        elif k in _BOOL:
            out[k] = v.lower() in ("1", "true", "yes", "on")
        else:
            out[k] = v
    return out


def make_adapter(uri: str):
    uri = (uri or "png").strip()
    if uri in ("", "png", "file", "none"):
        return PngOnly()
    u = urlparse(uri)
    opts = _opts(u.query)
    if u.scheme == "escpos":
        if not u.hostname:
            raise ValueError(f"escpos needs a host: {uri}")
        allowed = {"width", "cut", "feed", "band", "threshold", "retries", "timeout"}
        return EscPosNetwork(u.hostname, u.port or 9100, **{k: v for k, v in opts.items() if k in allowed})
    if u.scheme == "tspl":
        conn = {k: opts.pop(k) for k in ("retries", "timeout") if k in opts}
        if u.hostname:
            return Tspl(host=u.hostname, port=u.port or 9100, **conn, **opts)
        return Tspl(device=u.path or "auto", **conn, **opts)
    if u.scheme == "cups":
        if "@" in u.netloc:
            q, server = u.netloc.split("@", 1)
            return Cups(q, server)
        return Cups(u.netloc or u.path.lstrip("/"))
    raise ValueError(f"unknown printer uri: {uri}")


class Printers:
    """receipt = slips (task/retro/reminder/notification/void/note); label = cards."""

    def __init__(self, out_dir: Path, receipt_uri: str = "png", label_uri: str = "png",
                 start_worker: bool = True, keep: int = 0):
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.adapters = {"receipt": make_adapter(receipt_uri), "label": make_adapter(label_uri)}
        self.keep = keep
        self._seq = itertools.count(1)
        self._q: queue.Queue = queue.Queue()
        self.history: list[dict] = []
        self._worker_on = start_worker and any(a.name != "png" for a in self.adapters.values())
        if self._worker_on:
            threading.Thread(target=self._worker, daemon=True, name="pass-printer").start()

    def output(self, device: str, kind: str, img: Image.Image, name: str = "") -> Path:
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40]
        path = self.out_dir / f"{stamp}-{next(self._seq):04d}-{device}-{kind}{'-' + slug if slug else ''}.png"
        img.save(path)
        adapter = self.adapters[device]
        if adapter.name != "png":
            if self._worker_on:
                self._q.put((adapter, path, img))
            else:
                self._send(adapter, path, img)
        self._prune()
        return path

    def _prune(self):
        if self.keep and self.keep > 0:
            files = sorted(self.out_dir.glob("*.png"))
            for f in files[:-self.keep]:
                try:
                    f.unlink()
                except OSError:
                    pass

    def _send(self, adapter, path, img):
        rec = {"file": path.name, "adapter": adapter.name, "target": adapter.target,
               "at": dt.datetime.now().astimezone().isoformat(timespec="seconds")}
        try:
            rec.update(ok=True, result=adapter.send(path, img))
        except Exception as e:  # printer offline etc. The PNG is still on disk.
            rec.update(ok=False, result=f"{type(e).__name__}: {e}")
            log.warning("print failed for %s via %s: %s", path.name, adapter.target, e)
        self.history = (self.history + [rec])[-50:]
        return rec

    def _worker(self):
        while True:
            adapter, path, img = self._q.get()
            self._send(adapter, path, img)
            self._q.task_done()

    def drain(self, timeout: float = 30):
        end = time.time() + timeout
        while self._q.unfinished_tasks and time.time() < end:
            time.sleep(0.05)

    def status(self) -> dict:
        return {dev: {**a.status(), "uri_target": a.target} for dev, a in self.adapters.items()}
