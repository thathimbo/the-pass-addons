"""Printer adapter layer.

Every printout is ALWAYS rendered to a PNG under out/ first (archive + preview). Then it
goes to the adapter configured for that device (receipt = slips, label = cards):

  png                                  nothing else (default)
  escpos://HOST[:PORT]?opts            ESC/POS raster (GS v 0) over raw TCP (default 9100)
        opts: width=576 cut=partial|full|none feed=3 band=256 threshold=160 retries=3 timeout=10
  tspl:/dev/usb/lp0?opts               TSPL BITMAP job written to a USB printer device node
  tspl:auto?opts                       first /dev/usb/lp* that exists
  tspl://HOST[:PORT]?opts              same TSPL job over raw TCP (network label printers)
        opts: width_mm=100 height_mm=150 gap_mm=3 density=8 speed=3 direction=0
              invert=0 threshold=160 dpmm=8 tear=1 band_rows=200 thin=0.5 watch=5
  cups://QUEUE[@HOST:PORT]             `lp [-h HOST:PORT] -d QUEUE -o fit-to-page file.png`

The TSPL header mirrors Polono's own PL80E CUPS filter: SIZE 100x150 mm, GAP 3 mm,
DIRECTION 0,0, REFERENCE 0,0, SET TEAR ON, SPEED, DENSITY, CLS, BITMAP x,y,wbytes,h,1,<data>,
PRINT 1,n. BITMAP data is 1 bit per dot, MSB first, bit 0 = burn a dot (TSC spec).
v0.2.1: the bitmap is sent in 200-row bands, solid areas in rows over 50% black are
hatched (peak heater current), density/speed default lower (8/3). After each USB
write we watch the device node; if the printer drops off USB (it reset), the job is
reported as failed instead of "wrote N bytes".

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
import stat
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from PIL import Image, ImageDraw, ImageFilter, ImageFont

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


def thin_solids(bw: Image.Image, max_row: float = 0.5, edge: int = 2) -> Image.Image:
    """Peak-current guard for thermal heads.

    A row that would burn more than `max_row` of its dots at once (a solid bar, a big black
    block) gets the INSIDE of its solid areas hatched 50% (checkerboard). Every edge keeps
    a solid rim `edge` dots wide, so shapes and text stay crisp. Rows under the limit (text,
    QR codes, thin rules) are left exactly as they are. 0 disables it.
    """
    if not max_row or max_row <= 0 or max_row >= 1:
        return bw
    L = bw.convert("L")
    W, H = L.size
    limit = max_row * W
    raw = bytearray(L.tobytes())
    heavy = [y for y in range(H) if raw[y * W:(y + 1) * W].count(0) > limit]
    if not heavy:
        return bw
    padded = Image.new("L", (W + 2 * edge, H + 2 * edge), 255)  # outside the label counts as white
    padded.paste(L, (edge, edge))
    inner = padded.filter(ImageFilter.MaxFilter(2 * edge + 1)).crop(
        (edge, edge, edge + W, edge + H)).tobytes()  # 0 = black with >= `edge` black all around
    for y in heavy:
        row = y * W
        for x in range((y & 1), W, 2):
            if inner[row + x] == 0:
                raw[row + x] = 255
    return Image.frombytes("L", (W, H), bytes(raw)).point(lambda v: 255 if v >= 128 else 0, mode="1")


def _tspl_head(width_mm, height_mm, gap_mm, density=None, speed=None, direction=0,
               tear=True, full=True) -> list[str]:
    lines = [f"SIZE {_num(width_mm)} mm,{_num(height_mm)} mm", f"GAP {_num(gap_mm)} mm,0 mm"]
    if full:
        lines += [f"DIRECTION {direction},0", "REFERENCE 0,0", "SET TEAR ON" if tear else "SET TEAR OFF"]
    if speed is not None:
        lines.append(f"SPEED {speed}")
    if density is not None:
        lines.append(f"DENSITY {density}")
    lines.append("CLS")
    return lines


def tspl_bitmap_job(img: Image.Image, width_mm: float = 100, height_mm: float = 150,
                    gap_mm: float = 3, density: int = 8, speed: int = 3, direction: int = 0,
                    invert: bool = False, threshold: int = 160, dpmm: int = 8,
                    copies: int = 1, tear: bool = True, band_rows: int = 200,
                    thin: float = 0.5, margin_rows: int = 2) -> bytes:
    """One full-label TSPL job.

    * The image is fitted to the label's dot grid (100 x 150 mm @ 8 dots/mm = 800 x 1200;
      the card renderer already draws at that size), keeping `margin_rows` clear at the
      bottom so the bitmap never touches the label edge (SIZE rounding on clones).
    * `thin_solids` caps per-row heater load (see above).
    * The bitmap goes as several BITMAP commands of `band_rows` rows (0 = one command).
      All-white bands are skipped (CLS already cleared the buffer). Smaller commands
      suit the small receive buffers on cheap firmwares.
    * Starts with a bare CRLF to resync a parser left mid-BITMAP by an aborted job.
    """
    w = int(width_mm * dpmm) // 8 * 8
    h = int(height_mm * dpmm) - max(0, margin_rows)
    bw = to_1bit(fit_to(img, w, h), threshold)
    bw = thin_solids(bw, thin)
    wb = w // 8
    data = bw.tobytes()  # PIL '1': bit 1 = white = no dot, MSB first: exactly TSPL polarity
    if invert:           # for firmware that expects 1 = dot
        data = bytes(b ^ 0xFF for b in data)
    white = b"\x00" if invert else b"\xff"
    out = bytearray(b"\r\n")
    out += ("\r\n".join(_tspl_head(width_mm, height_mm, gap_mm, density, speed, direction, tear))
            + "\r\n").encode("ascii")
    step = band_rows if band_rows and band_rows > 0 else h
    for top in range(0, h, step):
        rows = min(step, h - top)
        chunk = data[top * wb:(top + rows) * wb]
        if band_rows and chunk == white * len(chunk):
            continue
        out += f"BITMAP 0,{top},{wb},{rows},1,".encode("ascii") + chunk + b"\r\n"
    out += f"PRINT 1,{copies}\r\n".encode("ascii")
    return bytes(out)


def _diag_bitmap(size: int = 200) -> Image.Image:
    """Small test pattern: frame, diagonals, a checker patch and 'OK'."""
    im = Image.new("L", (size, size), 255)
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, size - 1, size - 1], outline=0, width=4)
    d.line([0, 0, size - 1, size - 1], fill=0, width=3)
    d.line([0, size - 1, size - 1, 0], fill=0, width=3)
    for y in range(16, 64, 8):
        for x in range(16 + (y // 8 % 2) * 8, 64, 16):
            d.rectangle([x, y, x + 7, y + 7], fill=0)
    d.text((size - 70, size - 50), "OK", fill=0, font=ImageFont.load_default(size=36))
    return im


def tspl_diag_job(variant: str = "text", width_mm: float = 100, height_mm: float = 150,
                  gap_mm: float = 3, density: int | None = None, speed: int | None = None,
                  invert: bool = False, rows: int = 40, **_ignored) -> bytes:
    """Diagnostic jobs that need no card:
      text    SIZE/GAP/CLS + BOX + TEXT + PRINT 1 (no bitmap at all)
      bitmap  same + one 200x200 BITMAP (5 KB)
      solid   same as text + a full-width black block `rows` tall (heater power stress)
    """
    lines = ["", *_tspl_head(width_mm, height_mm, gap_mm, density, speed, full=False),
             "BOX 40,40,760,330,4",
             'TEXT 72,80,"3",0,2,2,"THE PASS TEST"',
             f'TEXT 72,180,"2",0,1,1,"TSPL {variant} diagnostic"',
             f'TEXT 72,230,"2",0,1,1,"d={density if density is not None else "-"} s={speed if speed is not None else "-"}"']
    out = bytearray(("\r\n".join(lines) + "\r\n").encode("ascii"))
    if variant == "bitmap":
        bw = to_1bit(_diag_bitmap(200))
        data = bw.tobytes()
        if invert:
            data = bytes(b ^ 0xFF for b in data)
        out += b"BITMAP 300,400,25,200,1," + data + b"\r\n"
    elif variant == "solid":
        rows = max(1, min(400, int(rows)))
        out += f"BAR 0,400,{int(width_mm * 8) // 8 * 8},{rows}\r\n".encode("ascii")
    elif variant != "text":
        raise ValueError(f"unknown diagnostic variant: {variant}")
    out += b"PRINT 1\r\n"
    return bytes(out)


def describe_tspl(data: bytes, limit: int = 1500) -> str:
    """Readable view of a TSPL job: commands as text, binary payloads as <N bytes>."""
    out, i, n = [], 0, len(data)
    while i < n and sum(map(len, out)) < limit:
        if data.startswith((b"BITMAP", b"BMPCPB"), i):
            p, ok = i, True
            for _ in range(5):
                p = data.find(b",", p) + 1
                if p == 0:
                    ok = False
                    break
            if ok:
                head = data[i:p].decode("ascii", "replace")
                try:
                    _, _, wb, h, _ = head[len("BITMAP "):-1].split(",")
                    size = int(wb) * int(h)
                    out.append(f"{head}<{size} bytes>")
                    i = p + size
                    if data.startswith(b"\r\n", i):
                        i += 2
                    continue
                except ValueError:
                    pass
        j = data.find(b"\r\n", i)
        j = n if j < 0 else j
        out.append(data[i:j].decode("ascii", "replace"))
        i = j + 2
    return " | ".join(out)


_LP_STATUS = {0x01: "head open", 0x02: "paper jam", 0x04: "out of paper", 0x08: "out of ribbon",
              0x10: "paused", 0x20: "printing", 0x40: "cover open", 0x80: "other error"}


class Tspl:
    name = "tspl"

    def __init__(self, device: str | None = None, host: str | None = None, port: int = 9100,
                 retries: int = 3, timeout: float = 10.0, watch: float | None = None, **job_opts):
        self.device, self.host, self.port = device, host, port
        # Real device nodes get the 5 s reset watch and the char-device guard; plain files
        # (tests, a capture file) don't.
        self.is_node = not host and (device or "auto") == "auto" or str(device).startswith("/dev/")
        self.retries, self.timeout = retries, timeout
        self.watch = watch if watch is not None else (5.0 if self.is_node else 0.0)
        self.job_opts = job_opts
        self.target = f"{host}:{port}" if host else (device or "auto")
        self._lock = threading.Lock()

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
        return self.send_raw(self.job(img))["result"]

    def _identity(self, dev: str):
        try:
            st = os.stat(dev)
            return (st.st_ino, st.st_rdev, st.st_ctime_ns)
        except OSError:
            return None

    def _watch_drop(self, dev: str, before) -> float | None:
        """After a job, watch the device node: a printer that resets (brown-out, firmware
        crash) drops off USB within a second or two and comes back as a new node."""
        end = time.monotonic() + max(0.0, self.watch)
        t0 = time.monotonic()
        while time.monotonic() < end:
            now = self._identity(dev)
            if now is None or (before and now[:2] != before[:2]) or (before and now[0] != before[0]):
                return round(time.monotonic() - t0, 2)
            time.sleep(0.1)
        return None

    def send_raw(self, data: bytes, raise_on_drop: bool = True) -> dict:
        """Write a ready-made TSPL job. Returns {ok, result, bytes, dropped_after_s}."""
        if self.host:
            _send_tcp(self.host, self.port, data, self.timeout, self.retries)
            return {"ok": True, "bytes": len(data), "result": f"sent {len(data)} bytes to {self.target}"}
        with self._lock:
            dev = self.resolve_device()
            last = None
            for attempt in range(max(1, self.retries)):
                try:
                    if self.is_node and not stat.S_ISCHR(os.stat(dev).st_mode):
                        # a stale regular file where the node was would swallow the job
                        raise OSError(f"{dev} is not a character device (printer dropped off USB?)")
                    before = self._identity(dev)
                    fd = os.open(dev, os.O_WRONLY)
                    try:
                        view = memoryview(data)
                        while view:
                            n = os.write(fd, view[:4096])
                            view = view[n:]
                    finally:
                        os.close(fd)
                    break
                except OSError as e:  # EBUSY / ENODEV during a USB re-enumeration
                    last = e
                    if attempt + 1 >= max(1, self.retries):
                        raise OSError(f"{dev}: {last}")
                    time.sleep(min(8, 2 ** attempt))
            dropped = self._watch_drop(dev, before)
        res = {"ok": dropped is None, "bytes": len(data), "dropped_after_s": dropped,
               "result": f"wrote {len(data)} bytes to {dev}"}
        if dropped is not None:
            res["result"] += (f", then the printer dropped off USB {dropped}s later (it reset mid-job: "
                              "usually power sag from heavy black areas, or a firmware fault)")
            if raise_on_drop:
                raise PrinterReset(res["result"])
        return res

    def query(self) -> dict:
        """Best effort, prints nothing: TSPL <ESC>!? status byte and ~!T model name.
        Many clones are write-only over USB, so silence is not an error."""
        out = {}
        if self.host:
            return {"error": "query only implemented for USB"}
        dev = self.resolve_device()
        with self._lock:
            fd = os.open(dev, os.O_RDWR | os.O_NONBLOCK)
            try:
                for key, cmd in (("status", b"\x1b!?"), ("model", b"~!T\r\n")):
                    try:
                        os.write(fd, cmd)
                    except BlockingIOError:
                        pass
                    buf, end = b"", time.monotonic() + 1.2
                    while time.monotonic() < end:
                        try:
                            got = os.read(fd, 256)
                            if got:
                                buf += got
                                if key == "status" or buf.endswith((b"\r", b"\n", b"\x00")):
                                    break
                        except (BlockingIOError, InterruptedError):
                            pass
                        except OSError as e:
                            out[key + "_error"] = str(e)
                            break
                        time.sleep(0.05)
                    if key == "status" and buf:
                        b = buf[0]
                        out["status_byte"] = b
                        out["status"] = [v for k, v in _LP_STATUS.items() if b & k] or ["ready"]
                    elif buf:
                        out[key] = buf.decode("ascii", "replace").strip("\x00\r\n ")
                    else:
                        out.setdefault(key, None)
            finally:
                os.close(fd)
        return out

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
            exists = os.path.exists(dev)
            st.update(device=dev, exists=exists, writable=os.access(dev, os.W_OK),
                      char_device=exists and stat.S_ISCHR(os.stat(dev).st_mode))
            st["ok"] = bool(exists and st["writable"] and (st["char_device"] or not self.is_node))
            st.update(usb_info(dev))
        except FileNotFoundError as e:
            st.update(ok=False, error=str(e))
        st["job"] = {k: self.job_opts.get(k, v) for k, v in
                     (("density", 8), ("speed", 3), ("band_rows", 200), ("thin", 0.5), ("invert", False))}
        return st


class PrinterReset(OSError):
    pass


def usb_info(dev: str) -> dict:
    """USB identity of a usblp node from sysfs (read-only; prints nothing)."""
    name = os.path.basename(dev)
    base = Path("/sys/class/usbmisc") / name / "device"
    info = {}
    try:
        ieee = (base / "ieee1284_id").read_text().strip()
        if ieee:
            info["ieee1284_id"] = ieee
    except OSError:
        pass
    try:
        d = base.resolve()
        for _ in range(4):
            if (d / "idVendor").exists():
                info["usb_id"] = f"{(d / 'idVendor').read_text().strip()}:{(d / 'idProduct').read_text().strip()}"
                for k in ("manufacturer", "product"):
                    if (d / k).exists():
                        info["usb_" + k] = (d / k).read_text().strip()
                break
            d = d.parent
    except OSError:
        pass
    return info


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
        "direction", "dpmm", "copies", "band_rows", "margin_rows"}
_FLOAT = {"timeout", "width_mm", "height_mm", "gap_mm", "thin", "watch"}
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
        conn = {k: opts.pop(k) for k in ("retries", "timeout", "watch") if k in opts}
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
