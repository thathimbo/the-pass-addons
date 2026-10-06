# The Pass

A scan-only kitchen-island task loop for one person who chooses their own next task.

- **Cards** are projects. They print as 4x6 labels on the **Polono PL80E**, over USB to this Pi, using direct TSPL.
- **Slips** are tasks. They print on the **ESC/POS receipt printer** on the network, at 192.168.1.87:9100.
- A keyboard-wedge **barcode scanner** is the only input in the kitchen. The projector page and the
  scanner page both send scans to the server.
- The pass never assigns the next task. Scanning a card is the choice.

## Pages

| URL | What |
|---|---|
| `http://192.168.1.92:8787/` | Projector view. It also catches scanner keystrokes, so a scanner plugged into the projector machine works. |
| `http://192.168.1.92:8787/scanner` | Scanner input page with an autofocused box. |
| `http://192.168.1.92:8787/desk` | Capture a project and print or reprint its card. |
| `http://192.168.1.92:8787/retro/<id>` | Retro form. The retro slip's QR opens it on a phone. |
| `http://192.168.1.92:8787/docs` | API docs. |
| HA sidebar → **The Pass** | The same app through ingress, with no port and HA login. |

## Printers

**Receipt (ESC/POS, network).** The default is `receipt_printer: escpos` with `receipt_host: 192.168.1.87`
and port 9100. Each slip goes out as a 576-dot raster image followed by a partial cut. If the printer is busy
because Home Assistant's own ESC/POS integration is talking to it, the send is retried 3 times.

**Label (Polono PL80E, USB).** The default is `label_printer: tspl_usb` with `label_device: /dev/usb/lp0`.
Cards are drawn at the PL80E's native page size, 100 x 150 mm (800 x 1200 dots). They go out as TSPL with the same
header Polono's own driver uses (SIZE, GAP 3 mm, DIRECTION, REFERENCE, SET TEAR, SPEED, DENSITY, CLS). The bitmap is
split into 200-row `BITMAP` bands (`label_band_rows`), then `PRINT 1,1`.
- **The printer resets mid-print** (judder, beep, nothing comes out). That's a power spike from big solid black
  areas. Keep `label_thin` at 0.5 and lower `label_density` (8) and `label_speed` (3). The status page reports
  these prints as failed ("dropped off USB … it reset").
- If cards come out as a negative (black background), turn on `label_invert`.
- If they're too light, raise `label_density`, which goes up to 15.
- If labels drift across the gap, check `label_gap_mm`. You can also hold the printer's feed button to
  re-calibrate.
- Fallback: set `label_printer: cups` with `cups_queue` set to the queue name from the CUPS add-on and
  `cups_server: 172.30.32.1:631`.

**Test prints**, which don't touch any tasks:

```
curl -X POST http://192.168.1.92:8787/api/printers/test -H 'Content-Type: application/json' -d '{"device":"label"}'
curl -X POST http://192.168.1.92:8787/api/printers/test -H 'Content-Type: application/json' -d '{"device":"receipt"}'
curl http://192.168.1.92:8787/api/printers      # reachability, USB id, last send results
```

**Label diagnostics** (`POST /api/printers/label-diagnostic`). These wait about 5 s and report whether the
printer reset:

| body | prints |
|---|---|
| `{"variant":"text"}` | SIZE/GAP/CLS + a box + "THE PASS TEST" text. No bitmap. |
| `{"variant":"bitmap"}` | The same plus one small 200x200 BITMAP. |
| `{"variant":"card","density":6,"speed":2}` | The real test card, with optional `density`/`speed`/`band_rows`/`thin` overrides. |
| `{"variant":"solid","rows":40}` | Text plus a full-width black bar. A power stress test that may reset a weak printer. |
| `{"variant":"query"}` | Nothing. Asks the printer for its status byte and model name (many clones don't answer). |

Every printout is also saved as a PNG under `/share/the-pass/out/`. The newest 3000 are kept.

## Home Assistant calling The Pass

From HA itself, the add-on's hostname is `local-the-pass` when installed as a local add-on.
```yaml
rest_command:
  pass_reminder:
    url: http://local-the-pass:8787/api/print/reminder
    method: POST
    content_type: application/json
    payload: '{"title":"{{ title }}","body":"{{ body }}","when":"{{ when }}","source":"home-assistant"}'
  pass_notification:
    url: http://local-the-pass:8787/api/print/notification
    method: POST
    content_type: application/json
    payload: '{"title":"{{ title }}","body":"{{ body }}","source":"home-assistant"}'
```
If you set a `token`, add `headers: {X-Pass-Token: "..."}`.

## Data

The database is `/data/pass.db` and is included in add-on backups. PNG previews are in `/share/the-pass/out`
and are excluded from backups.
