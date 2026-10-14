"""HTC flight-progress-strip card: 6x4 in landscape (1218x812 @ 203 dpi).
Exact half fold: the strip fills the top half (0-406 px), the fold line sits at 406, notes only below."""
from __future__ import annotations
from PIL import Image, ImageDraw
from .render import font, qr_image, wrap

W, H = 1218, 812
FOLD = H // 2  # 406: exact half fold
M = 10


def box(d, x0, y0, x1, y1, w=3):
    d.rectangle([x0, y0, x1, y1], outline=0, width=w)

def ctext(d, cx, cy, s, f):
    l, t, r, b = d.textbbox((0, 0), s, font=f)
    d.text((cx - (r - l) / 2 - l, cy - (b - t) / 2 - t), s, font=f, fill=0)

def fit(d, s, maxw, sizes, bold=True, mono=False):
    for sz in sizes:
        f = font(sz, bold, mono)
        if d.textlength(s, font=f) <= maxw:
            return f
    return font(sizes[-1], bold, mono)

def strip_card(*, code, title, kind, stack, due, steps, notes, other, printed):
    img = Image.new("L", (W, H), 255)
    d = ImageDraw.Draw(img)
    y0, y1 = M, FOLD - 8
    rh = (y1 - y0) // 3
    r1, r2 = y0 + rh, y0 + 2 * rh
    box(d, M, y0, W - M - 1, y1, 5)
    # --- column A: type / count, CALLSIGN, equipment row
    ax1 = 540
    d.line([ax1, y0, ax1, y1], fill=0, width=4)
    d.line([M, r1, ax1, r1], fill=0, width=3)
    d.line([M, r2, ax1, r2], fill=0, width=3)
    d.line([200, y0, 200, r1], fill=0, width=3)
    d.line([370, y0, 370, r1], fill=0, width=3)
    small, tiny = font(30, True), font(16, False)
    d.text((M + 8, y0 + 4), "TYPE/STACK", font=tiny, fill=0)
    ctext(d, 125, (y0 + r1) / 2 + 8, f"{kind}/{stack[0]}", small)
    d.text((208, y0 + 4), "TASKS", font=tiny, fill=0)
    done = sum(1 for s in steps if s[2])
    ctext(d, 285, (y0 + r1) / 2 + 8, f"{done}/{len(steps)}", small)
    d.text((378, y0 + 4), "DUE", font=tiny, fill=0)
    ctext(d, 455, (y0 + r1) / 2 + 8, due or "--", fit(d, due or "--", 150, [28, 24, 20]))
    # callsign (bold title, uppercase, shrink to fit one line)
    cs = title.upper()
    f = fit(d, cs, ax1 - M - 30, [64, 58, 52, 46, 40, 36, 32, 28])
    while d.textlength(cs, font=f) > ax1 - M - 30:
        cs = cs[:-2].rstrip() + "…"
    ctext(d, (M + ax1) / 2, (r1 + r2) / 2, cs, f)
    # equipment row: stack name, printed stamp, HTC mark
    eq = [("STACK", stack), ("PRINTED", printed)]
    xs = [M, 230, 440, ax1]
    for i, (k, v) in enumerate(eq):
        d.text((xs[i] + 8, r2 + 4), k, font=tiny, fill=0)
        ctext(d, (xs[i] + xs[i + 1]) / 2, (r2 + y1) / 2 + 8, v, fit(d, v, xs[i + 1] - xs[i] - 16, [26, 22, 18], bold=False))
        d.line([xs[i + 1], r2, xs[i + 1], y1], fill=0, width=3)
    # HTC mark: outlined, not solid (avoid big solid blocks on thermal)
    d.rectangle([xs[2] + 8, r2 + 10, ax1 - 8, y1 - 10], outline=0, width=3)
    ctext(d, (xs[2] + ax1) / 2, (r2 + y1) / 2, "HTC", font(30, True))
    # --- column B (ADEP-style): QR + code
    bx1 = ax1 + 220
    d.line([bx1, y0, bx1, y1], fill=0, width=4)
    q = qr_image(code, 200)
    img.paste(q, (ax1 + (220 - q.width) // 2, y0 + max(4, (y1 - y0 - 50 - q.height) // 2)))
    ctext(d, (ax1 + bx1) / 2, y1 - 22, code, font(30, True, True))
    # --- column C: phase boxes, label row + diagonal est/actual cells
    ox0 = W - M - 140
    n = 4
    pw = (ox0 - bx1) / n
    d.line([ox0, y0, ox0, y1], fill=0, width=4)
    mid = y0 + (y1 - y0) // 2
    d.line([bx1, mid, ox0, mid], fill=0, width=3)
    shown = steps[:n]
    for i in range(n):
        x0, x1 = bx1 + i * pw, bx1 + (i + 1) * pw
        if i:
            d.line([x0, y0, x0, y1], fill=0, width=3)
        if i < len(shown):
            name, est, actual = shown[i]
            d.text((x0 + 6, y0 + 4), f"{i + 1:02d}", font=font(18, True), fill=0)
            up = name.upper()
            for sz in (22, 20, 18, 16, 15, 14, 13, 12, 11):
                lf = font(sz, True)
                if all(d.textlength(w, font=lf) <= pw - 10 for w in up.split()):
                    break
            else:  # a word is too long even small: abbreviate it rather than split mid-word
                sz, lf = 13, font(13, True)
                ws = []
                for w in up.split():
                    while d.textlength(w, font=lf) > pw - 10 and len(w) > 2:
                        w = w[:-2] + "."
                    ws.append(w)
                up = " ".join(ws)
            lines = wrap(d, up, lf, int(pw - 10))[:max(3, (mid - y0 - 36) // (sz + 3))]
            for j, ln in enumerate(lines):
                d.text((x0 + 6, y0 + 28 + j * (sz + 3)), ln, font=lf, fill=0)
            d.line([x0 + 2, y1 - 2, x1 - 2, mid + 2], fill=0, width=2)
            if est:
                d.text((x0 + 6, mid + 6), est, font=font(18), fill=0)
            if actual:
                f2 = font(20, True)
                d.text((x1 - 8 - d.textlength(actual, font=f2), y1 - 30), actual, font=f2, fill=0)
    if len(steps) > n:
        d.text((ox0 - 40, mid - 24), f"+{len(steps) - n}", font=font(18, True), fill=0)
    # --- column D: other info
    d.text((ox0 + 8, y0 + 4), "OTHER", font=tiny, fill=0)
    for j, ln in enumerate(wrap(d, other, font(18), 124)[:8]):
        d.text((ox0 + 8, y0 + 26 + j * 23), ln, font=font(18), fill=0)
    box(d, M, y0, W - M - 1, y1, 5)
    # --- fold line
    x = 0
    while x < W:
        d.line([x, FOLD, x + 18, FOLD], fill=0, width=3)
        x += 30
    # --- below the fold: notes only
    d.text((40, FOLD + 16), "NOTES", font=font(20, True), fill=0)
    nf = font(26)
    for k, ln in enumerate(wrap(d, notes or "", nf, W - 80)[:11]):
        d.text((40, FOLD + 48 + k * 32), ln, font=nf, fill=0)
    return img.point(lambda v: 0 if v < 128 else 255, "1")

