"""Raw label-print listener (TCP, "port 9100 style", default port 9101).

Lets other systems print on the 4x6 label printer *through* The Pass, so The Pass
stays the only process writing to /dev/usb/lp0. Each connection is one job: send the
bytes, close the socket. Accepted payloads:

  * CUPS raster (RaS2 / RaS3, either byte order) - what a CUPS queue with a
    "pass-through" PPD sends (see DOCS: "Print from a Mac"). Every page = one label.
  * PNG / JPEG / BMP / GIF / TIFF image - one label.

Each page goes through the normal label path (Printers.output -> worker -> TSPL job),
so it gets the same fit, threshold, heater guard and banding as project cards.
Only private / loopback client addresses are accepted.
"""
from __future__ import annotations

import io
import ipaddress
import logging
import socket
import struct
import threading

from PIL import Image

log = logging.getLogger("pass.rawprint")

MAX_BYTES = 64 * 1024 * 1024
HEADER = 1796


def _decode_raster(data: bytes) -> list[Image.Image]:
    sync = data[:4]
    if sync in (b"RaS2", b"RaS3", b"RaSt"):
        end = ">"
    elif sync in (b"2SaR", b"3SaR", b"tSaR"):
        end = "<"
    else:
        raise ValueError("not CUPS raster")
    if sync in (b"RaSt", b"tSaR"):
        raise ValueError("CUPS raster v1 not supported")
    compressed = sync in (b"RaS2", b"2SaR")
    pos, pages = 4, []
    while pos + HEADER <= len(data):
        h = data[pos:pos + HEADER]
        pos += HEADER
        u = lambda off: struct.unpack(end + "I", h[off:off + 4])[0]  # noqa: E731
        width, height = u(372), u(376)
        bpc, bpp, bpl, space = u(384), u(388), u(392), u(400)
        if not width or not height or bpl == 0 or bpp not in (1, 8):
            raise ValueError(f"unsupported raster: {width}x{height} bpp={bpp}")
        unit = max(1, bpp // 8)
        # white: luminance spaces (W=0, SW=18) store 0xff; K/black spaces store 0x00.
        white = b"\xff" if space in (0, 18) else b"\x00"
        if compressed:
            rows, y = [], 0
            while y < height:
                rep = data[pos] + 1
                pos += 1
                line = bytearray()
                while len(line) < bpl:
                    n = data[pos]
                    pos += 1
                    if n == 128:
                        line += white * (bpl - len(line))
                    elif n < 128:
                        line += data[pos:pos + unit] * (n + 1)
                        pos += unit
                    else:
                        cnt = (257 - n) * unit
                        line += data[pos:pos + cnt]
                        pos += cnt
                line = bytes(line[:bpl])
                rows.extend([line] * min(rep, height - y))
                y += rep
            raw = b"".join(rows)
        else:
            raw = data[pos:pos + bpl * height]
            pos += bpl * height
        if bpp == 1:
            # PIL "1" raw: bit 1 = white (luminance spaces); "1;I": bit 1 = black (K spaces)
            img = Image.frombytes("1", (width, height), raw, "raw",
                                  "1" if space in (0, 18) else "1;I", bpl).convert("L")
        else:
            img = Image.frombytes("L", (width, height), raw[:width * height] if bpl == width else
                                  b"".join(raw[r * bpl:r * bpl + width] for r in range(height)))
            if space not in (0, 18):
                img = img.point(lambda v: 255 - v)
        pages.append(img)
    if not pages:
        raise ValueError("empty raster")
    return pages


def decode(data: bytes) -> list[Image.Image]:
    if data[:4] in (b"RaS2", b"RaS3", b"RaSt", b"2SaR", b"3SaR", b"tSaR"):
        return _decode_raster(data)
    img = Image.open(io.BytesIO(data))
    img.load()
    if img.mode in ("RGBA", "LA", "P"):
        bg = Image.new("RGBA", img.size, "white")
        img = Image.alpha_composite(bg, img.convert("RGBA"))
    return [img.convert("L")]


def _allowed(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local


class RawLabelServer:
    def __init__(self, printers, port: int, host: str = "0.0.0.0"):
        self.printers, self.port, self.host = printers, port, host
        self.jobs = 0
        self.last: dict = {}

    def start(self):
        threading.Thread(target=self._serve, daemon=True, name="pass-rawprint").start()

    def _serve(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind((self.host, self.port))
        except OSError as e:
            log.error("raw label listener could not bind :%s: %s", self.port, e)
            return
        srv.listen(4)
        log.warning("raw label listener on :%s (CUPS raster / PNG / JPEG -> label printer)", self.port)
        while True:
            conn, (addr, _) = srv.accept()
            threading.Thread(target=self._handle, args=(conn, addr), daemon=True).start()

    def _handle(self, conn: socket.socket, addr: str):
        with conn:
            if not _allowed(addr):
                log.warning("raw label job refused from %s", addr)
                return
            conn.settimeout(60)
            buf = bytearray()
            try:
                while len(buf) < MAX_BYTES:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
            except socket.timeout:
                pass
            if not buf:
                return
            try:
                pages = decode(bytes(buf))
            except Exception as e:  # noqa: BLE001
                self.last = {"from": addr, "bytes": len(buf), "ok": False, "error": f"{type(e).__name__}: {e}"}
                log.warning("raw label job from %s not printable: %s", addr, e)
                return
            for i, img in enumerate(pages, 1):
                self.printers.output("label", "raw", img, f"raw-{addr}-p{i}")
            self.jobs += 1
            self.last = {"from": addr, "bytes": len(buf), "pages": len(pages), "ok": True,
                         "size": list(pages[0].size)}
            log.warning("raw label job from %s: %d page(s), %d bytes", addr, len(pages), len(buf))
