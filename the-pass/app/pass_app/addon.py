"""Home Assistant add-on entrypoint: /data/options.json -> PASS_* env -> server.

Run inside the add-on container as `python3 -m pass_app.addon`.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import urlencode

OPTIONS = Path(os.environ.get("PASS_OPTIONS_FILE", "/data/options.json"))


def options_to_env(o: dict) -> dict[str, str]:
    env: dict[str, str] = {
        "PASS_HOST": "0.0.0.0",
        "PASS_PORT": "8787",
        "PASS_DB": "/data/pass.db",
        "PASS_OUT": o.get("out_dir") or "/share/the-pass/out",
        "PASS_OUT_KEEP": str(o.get("out_keep", 3000)),
        "PASS_MID_TASK": o.get("mid_task", "note"),
        "PASS_BASE_URL": (o.get("base_url") or "").strip(),
        "PASS_TOKEN": (o.get("token") or "").strip(),
        "PASS_WEBHOOKS": ",".join(u.strip() for u in (o.get("webhooks") or []) if u and u.strip()),
    }

    rmode = o.get("receipt_printer", "escpos")
    if rmode == "escpos" and o.get("receipt_host"):
        q = urlencode({"cut": o.get("receipt_cut", "partial"), "width": o.get("receipt_width", 576)})
        env["PASS_RECEIPT_PRINTER"] = f"escpos://{o['receipt_host']}:{o.get('receipt_port', 9100)}?{q}"
    else:
        env["PASS_RECEIPT_PRINTER"] = "png"

    lmode = o.get("label_printer", "tspl_usb")
    if lmode == "tspl_usb":
        q = urlencode({
            "density": o.get("label_density", 8), "speed": o.get("label_speed", 3),
            "band_rows": o.get("label_band_rows", 200), "thin": o.get("label_thin", 0.5),
            "gap_mm": o.get("label_gap_mm", 3), "invert": int(bool(o.get("label_invert", False))),
            "width_mm": o.get("label_width_mm", 100), "height_mm": o.get("label_height_mm", 150),
        })
        env["PASS_LABEL_PRINTER"] = f"tspl:{o.get('label_device') or 'auto'}?{q}"
    elif lmode == "cups" and o.get("cups_queue"):
        server = (o.get("cups_server") or "").strip()
        env["PASS_LABEL_PRINTER"] = f"cups://{o['cups_queue']}" + (f"@{server}" if server else "")
    else:
        env["PASS_LABEL_PRINTER"] = "png"
    return env


def main():
    opts = json.loads(OPTIONS.read_text()) if OPTIONS.exists() else {}
    env = options_to_env(opts)
    os.environ.update(env)
    Path(env["PASS_OUT"]).mkdir(parents=True, exist_ok=True)
    shown = {k: ("***" if k == "PASS_TOKEN" and v else v) for k, v in env.items()}
    print("The Pass add-on config:", json.dumps(shown, indent=1), file=sys.stderr, flush=True)
    from .__main__ import main as serve
    serve()


if __name__ == "__main__":
    main()
