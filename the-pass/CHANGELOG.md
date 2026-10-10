# Changelog

## 0.2.2
- New: raw label listener on port 9101 (`label_raw_port`, 0 = off). Each TCP connection is one
  job: CUPS raster (RaS2/RaS3) or a PNG/JPEG image. Every page prints through the normal label
  path (same fit, threshold, heater guard and banding as cards), so The Pass stays the only
  writer to /dev/usb/lp0. Only private/loopback clients are accepted.
- This lets a CUPS queue (e.g. the CUPS add-on, shared over Bonjour/AirPrint) print 4x6 labels
  from a Mac: the queue uses a pass-through PPD and `socket://127.0.0.1:9101`.
- GET /api/printers shows the listener (port, job count, last job).

## 0.2.1
- Fix: the PL80E reset mid-card (judder + beep; USB dropped 1-2 s after each job). The card
  began with a full-width solid black header (83 rows at 96% black) printed at density 10 /
  speed 5. That is a peak-current spike that browns out cheap label printers.
  - The card header is now black text over a rule. No solid bar.
  - Rows over 50% black get their solid interiors hatched (`label_thin`, default 0.5).
  - The bitmap is sent as 200-row BITMAP bands; all-white bands are skipped (`label_band_rows`).
  - New installs default to density 8 and speed 3. Existing installs keep their saved values.
  - Each job starts with a resync CRLF and leaves 2 blank rows at the bottom edge.
- After every USB label job, the device node is watched for 5 s. If the printer drops off USB,
  the print is reported as FAILED ("printer reset") instead of "wrote N bytes".
- New: POST /api/printers/label-diagnostic. Variants: text (no bitmap), bitmap (200x200),
  card (with density/speed/band/thin overrides), solid (power stress), query (status/model,
  prints nothing).
- GET /api/printers now shows the label printer's USB id/name, its IEEE-1284 id, and the job settings.

## 0.2.0
- First Home Assistant add-on packaging (aarch64 + amd64, ingress + port 8787).
- Polono PL80E label printer: direct TSPL BITMAP over USB (/dev/usb/lp0). CUPS fallback.
- ESC/POS network receipt printer: raster slips with retries and cut.
- Printer status + test print endpoints.
