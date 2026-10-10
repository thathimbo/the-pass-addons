"""Settings, all overridable with environment variables (see README)."""
from __future__ import annotations

import os
import socket
from dataclasses import dataclass, field
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent


def _lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "localhost"


@dataclass
class Settings:
    host: str = "0.0.0.0"
    port: int = 8787
    db_path: Path = APP_DIR / "data" / "pass.db"
    out_dir: Path = APP_DIR / "out"
    receipt_printer: str = "png"   # png | escpos://HOST:9100 | cups://QUEUE
    label_printer: str = "png"     # png | escpos://HOST:9100 | cups://QUEUE
    mid_task: str = "note"         # note (default) | allow
    base_url: str = ""             # used in retro-slip QR (phone opens the retro form)
    token: str = ""                # optional shared secret for /api/* writes
    webhooks: list[str] = field(default_factory=list)  # seeded at startup
    out_keep: int = 0              # keep only the newest N PNGs in out/ (0 = keep all)
    start_workers: bool = True
    label_raw_port: int = 0        # raw TCP label listener (CUPS raster / PNG); 0 = off

    def __post_init__(self):
        self.db_path = Path(self.db_path)
        self.out_dir = Path(self.out_dir)
        if not self.base_url:
            self.base_url = f"http://{_lan_ip()}:{self.port}"
        self.base_url = self.base_url.rstrip("/")
        if self.mid_task not in ("note", "allow"):
            self.mid_task = "note"

    @classmethod
    def from_env(cls) -> "Settings":
        e = os.environ.get
        port = int(e("PASS_PORT", "8787"))
        return cls(
            host=e("PASS_HOST", "0.0.0.0"),
            port=port,
            db_path=Path(e("PASS_DB", str(APP_DIR / "data" / "pass.db"))),
            out_dir=Path(e("PASS_OUT", str(APP_DIR / "out"))),
            receipt_printer=e("PASS_RECEIPT_PRINTER", "png"),
            label_printer=e("PASS_LABEL_PRINTER", "png"),
            mid_task=e("PASS_MID_TASK", "note"),
            base_url=e("PASS_BASE_URL", ""),
            token=e("PASS_TOKEN", ""),
            label_raw_port=int(e("PASS_LABEL_RAW_PORT", "0") or 0),
            webhooks=[u.strip() for u in e("PASS_WEBHOOKS", "").split(",") if u.strip()],
            out_keep=int(e("PASS_OUT_KEEP", "0") or 0),
        )
