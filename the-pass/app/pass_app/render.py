"""Render every printout to a PIL image.

Receipt: 576 px wide (80 mm thermal, 72 mm printable @ 203 dpi), grayscale,
pure-black graphics so it survives 1-bit thermal conversion.
Card: 4x6 label, drawn at the PL80E page size 100 x 150 mm @ 203 dpi = 800 x 1200 dots (the brief names a
Polono PL80E, a 4x6 thermal label printer).

Four receipt slip types are visually distinct:
  task          solid black header bar, huge title, big checkoff QR
  retro         double border, outlined READY TO CLOSE header, write-in prompts
  reminder      dashed frame, clock icon, big time, no QR unless checkoff wanted
  notification  hatched header band, compact, no QR
plus two notices built on those looks: void_notice (big hatched VOID) and note.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import qrcode
from PIL import Image, ImageDraw, ImageFont

RECEIPT_W = 576
CARD_W, CARD_H = 800, 1200  # 100 x 150 mm @ 8 dots/mm (PL80E default page)
_FONT_DIRS = [Path("/usr/share/fonts/truetype/dejavu"), Path("/usr/share/fonts/dejavu")]
_cache: dict = {}


def font(size: int, bold: bool = False, mono: bool = False):
    key = (size, bold, mono)
    if key in _cache:
        return _cache[key]
    name = "DejaVuSans" + ("Mono" if mono else "") + ("-Bold" if bold else "") + ".ttf"
    f = None
    for d in _FONT_DIRS:
        p = d / name
        if p.exists():
            f = ImageFont.truetype(str(p), size)
            break
    if f is None:
        f = ImageFont.load_default(size)
    _cache[key] = f
    return f


def qr_image(data: str, target_px: int) -> Image.Image:
    """Crisp QR with integer module size and a 4-module quiet zone."""
    q = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=1, border=4)
    q.add_data(data)
    q.make(fit=True)
    m = q.get_matrix()
    n = len(m)
    mod = max(2, target_px // n)
    img = Image.new("L", (n * mod, n * mod), 255)
    d = ImageDraw.Draw(img)
    for y, row in enumerate(m):
        for x, v in enumerate(row):
            if v:
                d.rectangle([x * mod, y * mod, x * mod + mod - 1, y * mod + mod - 1], fill=0)
    return img


def wrap(draw: ImageDraw.ImageDraw, text: str, f, width: int) -> list[str]:
    lines: list[str] = []
    for para in (text or "").split("\n"):
        words = para.split()
        if not words:
            lines.append("")
            continue
        cur = ""
        for w in words:
            trial = (cur + " " + w).strip()
            if draw.textlength(trial, font=f) <= width:
                cur = trial
                continue
            if cur:
                lines.append(cur)
            # hard-split words longer than the line
            while draw.textlength(w, font=f) > width:
                i = len(w)
                while i > 1 and draw.textlength(w[:i], font=f) > width:
                    i -= 1
                lines.append(w[:i])
                w = w[i:]
            cur = w
        lines.append(cur)
    return lines


def _stamp(at: dt.datetime | None = None) -> str:
    at = at or dt.datetime.now().astimezone()
    return at.strftime("%a %b %-d  %-I:%M %p")


class Paper:
    """A tall receipt canvas with a moving cursor; cropped on finish()."""

    def __init__(self, width: int = RECEIPT_W, margin: int = 28):
        self.w, self.m = width, margin
        self.img = Image.new("L", (width, 5000), 255)
        self.d = ImageDraw.Draw(self.img)
        self.y = 0

    @property
    def inner(self) -> int:
        return self.w - 2 * self.m

    def space(self, h: int):
        self.y += h

    def text(self, s: str, size: int, bold=False, mono=False, align="left", fill=0,
             indent=0, gap=6, strike=False, width=None):
        f = font(size, bold, mono)
        width = width or (self.inner - indent)
        asc, desc = f.getmetrics()
        lh = asc + desc
        for line in wrap(self.d, s, f, width):
            tw = self.d.textlength(line, font=f)
            if align == "center":
                x = (self.w - tw) / 2
            elif align == "right":
                x = self.w - self.m - tw
            else:
                x = self.m + indent
            self.d.text((x, self.y), line, font=f, fill=fill)
            if strike and line:
                sy = self.y + asc * 0.62
                self.d.line([x, sy, x + tw, sy], fill=fill, width=max(2, size // 12))
            self.y += lh + gap

    def rule(self, thick=2, dash: int | None = None, inset=0):
        x0, x1 = self.m + inset, self.w - self.m - inset
        if dash:
            x = x0
            while x < x1:
                self.d.rectangle([x, self.y, min(x + dash, x1), self.y + thick - 1], fill=0)
                x += dash * 2
        else:
            self.d.rectangle([x0, self.y, x1, self.y + thick - 1], fill=0)
        self.y += thick

    def dotted_lines(self, n: int, gap=46):
        for _ in range(n):
            self.y += gap
            x = self.m + 10
            while x < self.w - self.m - 10:
                self.d.ellipse([x, self.y, x + 3, self.y + 3], fill=0)
                x += 12
        self.y += 10

    def paste_center(self, im: Image.Image):
        self.img.paste(im, ((self.w - im.width) // 2, self.y))
        self.y += im.height

    def hatch_band(self, h: int, step=14, thick=5):
        y0, y1 = self.y, self.y + h
        band = Image.new("L", (self.w, h), 255)
        bd = ImageDraw.Draw(band)
        for x in range(-h, self.w + h, step):
            bd.line([x, h, x + h, 0], fill=0, width=thick)
        self.img.paste(band, (0, y0))
        self.y = y1

    def label_box(self, text: str, size: int, cy: int, fill=255, ink=0, pad=14, outline=0):
        f = font(size, True)
        tw = self.d.textlength(text, font=f)
        asc, desc = f.getmetrics()
        x0 = (self.w - tw) / 2 - pad
        y0 = cy - (asc + desc) / 2 - pad / 2
        self.d.rectangle([x0, y0, x0 + tw + 2 * pad, y0 + asc + desc + pad], fill=fill,
                         outline=outline, width=4)
        self.d.text((x0 + pad, y0 + pad / 2), text, font=f, fill=ink)

    def finish(self, bottom=36) -> Image.Image:
        self.y += bottom
        return self.img.crop((0, 0, self.w, self.y))


def _clock_icon(d: ImageDraw.ImageDraw, cx: int, cy: int, r: int):
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=0, width=6)
    d.line([cx, cy, cx, cy - r * 0.6], fill=0, width=6)
    d.line([cx, cy, cx + r * 0.45, cy + r * 0.2], fill=0, width=6)
    d.ellipse([cx - 5, cy - 5, cx + 5, cy + 5], fill=0)


# ---------------------------------------------------------------- receipts

def task_slip(*, code: str, task: str, project: str, first_step: str = "",
              step: int | None = None, steps: int | None = None, at=None) -> Image.Image:
    p = Paper()
    # solid black header bar
    p.d.rectangle([0, 0, p.w, 92], fill=0)
    p.d.text((p.m, 18), "TASK", font=font(50, True), fill=255)
    f = font(26, False, True)
    p.d.text((p.w - p.m - p.d.textlength(code, font=f), 34), code, font=f, fill=255)
    p.y = 116
    label = project if not (step and steps and steps > 1) else f"{project}  ·  step {step} of {steps}"
    p.text(label, 26, bold=True, gap=4)
    p.space(10)
    p.rule(4)
    p.space(18)
    p.text(task, 54, bold=True, gap=8)
    if first_step:
        p.space(10)
        p.text("Smallest bit:", 24, gap=2)
        p.text(first_step, 32, gap=6)
    p.space(22)
    p.paste_center(qr_image(code, 300))
    p.space(4)
    p.text("scan this slip = done", 26, bold=True, align="center", gap=2)
    p.text("(its card puts it back in the pot)", 22, align="center")
    p.space(12)
    p.rule(2, dash=8)
    p.space(10)
    p.text(f"The Pass  ·  {_stamp(at)}", 20, align="center")
    return p.finish(24)


def retro_slip(*, project: str, done_tasks: list[str], form_url: str, code: str, at=None) -> Image.Image:
    p = Paper(margin=40)
    p.y = 40
    p.label_box("READY TO CLOSE", 46, cy=p.y + 34, fill=255, ink=0, outline=0)
    p.y += 96
    p.text(project, 40, bold=True, align="center", gap=6)
    p.space(10)
    p.text("Every slip on this card is done:", 24, gap=6)
    for t in done_tasks[:12]:
        p.text("✓ " + t, 26, indent=8, gap=4)
    if len(done_tasks) > 12:
        p.text(f"…and {len(done_tasks) - 12} more", 22, indent=8)
    p.space(14)
    p.rule(3)
    p.space(14)
    p.text("A few retro prompts (any or none):", 24, bold=True)
    for prompt in ("What went well?", "What was harder than expected?",
                   "Anything to remember next time?"):
        p.space(8)
        p.text(prompt, 28, bold=True, gap=0)
        p.dotted_lines(2, gap=40)
    p.space(8)
    p.paste_center(qr_image(form_url, 230))
    p.text("phone camera → retro form", 22, align="center", gap=2)
    p.text(form_url, 18, mono=True, align="center", gap=4)
    p.space(8)
    p.text("The card stays out until the retro is in.", 24, align="center")
    p.text(f"{code}  ·  {_stamp(at)}", 18, mono=True, align="center")
    img = p.finish(36)
    d = ImageDraw.Draw(img)
    d.rectangle([8, 8, img.width - 9, img.height - 9], outline=0, width=6)
    d.rectangle([20, 20, img.width - 21, img.height - 21], outline=0, width=2)
    return img


def reminder_slip(*, title: str, body: str = "", when: str = "", code: str | None = None,
                  at=None) -> Image.Image:
    p = Paper(margin=36)
    p.y = 26
    p.rule(6, dash=18)
    p.space(18)
    _clock_icon(p.d, p.m + 36, p.y + 36, 32)
    p.d.text((p.m + 92, p.y + 8), "REMINDER", font=font(48, True), fill=0)
    p.y += 86
    if when:
        p.text(when, 64, bold=True, gap=4)
    p.text(title, 44, bold=True, gap=6)
    if body:
        p.space(6)
        p.text(body, 30, gap=6)
    if code:
        p.space(16)
        p.paste_center(qr_image(code, 220))
        p.text("scan when handled", 24, align="center")
    p.space(16)
    p.text(_stamp(at), 20, align="right")
    p.space(8)
    p.rule(6, dash=18)
    return p.finish(24)


def notification_slip(*, title: str, body: str = "", source: str = "", code: str | None = None,
                      at=None, heading: str = "NOTICE") -> Image.Image:
    p = Paper(margin=40)
    p.hatch_band(64)
    p.label_box(heading, 30, cy=32, fill=255, ink=0, outline=255, pad=12)
    p.space(18)
    top = p.y
    p.text(title, 36, bold=True, gap=4)
    if body:
        p.space(4)
        p.text(body, 26, gap=4)
    if code:
        p.space(12)
        p.paste_center(qr_image(code, 200))
        p.text("scan when handled", 22, align="center")
    p.space(10)
    meta = "  ·  ".join(x for x in (source, _stamp(at)) if x)
    p.text(meta, 18, gap=0)
    p.d.rectangle([12, top - 4, 21, p.y], fill=0)   # left rule = notice look
    return p.finish(20)


def void_notice(*, code: str, task: str, project: str, at=None) -> Image.Image:
    p = Paper()
    p.hatch_band(150, step=18, thick=7)
    p.label_box("VOID", 96, cy=75, fill=0, ink=255, pad=18)
    p.space(20)
    p.text(f"Slip {code} is void.", 34, bold=True, align="center")
    p.space(6)
    p.text(task, 30, align="center", strike=True)
    p.text(project, 22, align="center")
    p.space(10)
    p.text("Its card was scanned again, so that task went back in the pot. "
           "Nothing changed with this scan.", 24, align="center")
    p.space(14)
    p.text(_stamp(at), 18, align="center")
    return p.finish(16)


# ---------------------------------------------------------------- card label

def card_label(*, code: str, title: str, notes: str = "", steps: int = 0, at=None) -> Image.Image:
    img = Image.new("L", (CARD_W, CARD_H), 255)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([14, 14, CARD_W - 15, CARD_H - 15], radius=36, outline=0, width=8)
    d.rectangle([14, 14, CARD_W - 15, 110], fill=0)
    d.rounded_rectangle([14, 14, CARD_W - 15, 110], radius=36, fill=0)
    d.text((56, 38), "THE PASS  ·  CARD", font=font(40, True), fill=255)
    # title, auto-shrink to fit 4 lines
    width = CARD_W - 112
    for size in (92, 84, 76, 68, 60, 54, 48, 42):
        f = font(size, True)
        lines = wrap(d, title, f, width)
        if len(lines) <= 4:
            break
    lines = lines[:4]
    asc, desc = f.getmetrics()
    y = 150
    for line in lines:
        d.text((56, y), line, font=f, fill=0)
        y += asc + desc + 6
    if notes:
        nf = font(28)
        for line in wrap(d, notes, nf, width)[:2]:
            d.text((56, y + 4), line, font=nf, fill=0)
            y += 36
    q = qr_image(code, max(240, min(520, CARD_H - 130 - (y + 24))))
    qy = max(y + 24, CARD_H - 130 - q.height)
    img.paste(q, ((CARD_W - q.width) // 2, qy))
    cf = font(56, True, True)
    tw = d.textlength(code, font=cf)
    d.text(((CARD_W - tw) / 2, CARD_H - 120), code, font=cf, fill=0)
    meta = f"{'one step' if steps == 1 else (str(steps) + ' steps' if steps else 'steps added later')}  ·  printed {_stamp(at)}"
    mf = font(22)
    d.text(((CARD_W - d.textlength(meta, font=mf)) / 2, CARD_H - 56), meta, font=mf, fill=0)
    return img
