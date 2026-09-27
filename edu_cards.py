"""
Card renderer for the educational posts (DSA, system design, ML system
design, AI engineering, papers) — a small design system, not clip-art.

Every question carries a `visual` spec chosen by the generator, and this
module draws it as a real figure next to the question text:

  {"kind": "array",    "values": [3,1,4,1,5], "highlight": [1,3], "pointers": {"0": "i", "4": "j"}}
  {"kind": "code",     "lang": "python", "code": "def f(...):\\n    ..."}
  {"kind": "boxes",    "items": ["Client", "API GW", "Service", "DB"]}          # left→right flow
  {"kind": "graph",    "nodes": ["plan", "act", "observe"], "edges": [[0,1],[1,2],[2,0]]}
  {"kind": "tree",     "values": [1, 2, 3, null, 4, 5]}                         # level order
  {"kind": "metric",   "big": "10,000×", "small": "fewer trainable params", "big2": "…", "small2": "…"}
  {"kind": "compare",  "left": {"title": "Fan-out on write", "points": [...]}, "right": {...}}
  {"kind": "table",    "header": ["", "A", "B"], "rows": [["latency", "1 ms", "10 ms"], ...]}
  {"kind": "none"}

Unknown or broken specs degrade to a clean text-only card — rendering must
never be the reason a slot is skipped. Canvas 1200×627 (LinkedIn feed image).
"""

import io
import os
import re
import math
import logging

log = logging.getLogger("edu-cards")

W, H = 1200, 627
FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")

INK = (17, 24, 39)
GRAY = (107, 114, 128)
LIGHT = (243, 244, 246)
PANEL = (248, 250, 252)
CODE_BG = (15, 23, 42)

# per-track palette: accent, soft tint, label
TRACKS = {
    "dsa":    {"accent": (37, 99, 235),  "tint": (219, 234, 254), "label": "DAILY DSA"},
    "sd":     {"accent": (5, 150, 105),  "tint": (209, 250, 229), "label": "SYSTEM DESIGN"},
    "mlsd":   {"accent": (124, 58, 237), "tint": (237, 233, 254), "label": "ML SYSTEM DESIGN"},
    "ai":     {"accent": (234, 88, 12),  "tint": (255, 237, 213), "label": "AI ENGINEERING"},
    "papers": {"accent": (190, 24, 93),  "tint": (252, 231, 243), "label": "PAPER OF THE DAY"},
}
DEFAULT_TRACK = {"accent": (37, 99, 235), "tint": (219, 234, 254), "label": "TECH Q&A"}


_GLYPH_MAP = {"→": "->", "←": "<-", "↔": "<->", "⇒": "=>", "≤": "<=", "≥": ">=", "≠": "!=", "≈": "~",
              "∞": "inf", "√": "sqrt", "∑": "sum", "·": "·", "✓": "yes", "✗": "no", "\u2212": "-",
              "\u2009": " ", "\u202f": " ", "\u00a0": " "}
_STRIP = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D\u2B00-\u2BFF]")


def clean_text(s):
    """Replace glyphs Poppins doesn't have (arrows, math) and drop emoji so
    nothing renders as a tofu box."""
    s = str(s or "")
    for k, v in _GLYPH_MAP.items():
        s = s.replace(k, v)
    s = _STRIP.sub("", s)
    return re.sub(r"[ \t]+", " ", s).strip()


def clean_spec(v):
    if isinstance(v, dict):
        return {k: clean_spec(x) for k, x in v.items()}
    if isinstance(v, list):
        return [clean_spec(x) for x in v]
    if isinstance(v, str):
        return clean_text(v)
    return v


def ellipsize(draw, text, fnt, max_w):
    if draw.textlength(text, font=fnt) <= max_w:
        return text
    while len(text) > 1 and draw.textlength(text + "…", font=fnt) > max_w:
        text = text[:-1]
    return text.rstrip() + "…"


def font(size, weight="Bold"):
    from PIL import ImageFont
    for path in (os.path.join(FONT_DIR, f"Poppins-{weight}.ttf"),
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def mono(size, bold=False):
    from PIL import ImageFont
    name = "DejaVuSansMono-Bold.ttf" if bold else "DejaVuSansMono.ttf"
    for path in (os.path.join(FONT_DIR, name), f"/usr/share/fonts/truetype/dejavu/{name}"):
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return font(size, "Regular")


def wrap(draw, text, fnt, max_w, max_lines=6):
    """Greedy word wrap; long words are split; last line gets an ellipsis if truncated."""
    words = text.split()
    lines, cur = [], ""
    for w in words:
        while draw.textlength(w, font=fnt) > max_w and len(w) > 1:   # split very long tokens
            cut = max(1, int(len(w) * max_w / max(draw.textlength(w, font=fnt), 1)) - 1)
            head, w = w[:cut], w[cut:]
            if cur:
                lines.append(cur)
                cur = ""
            lines.append(head)
        t = (cur + " " + w).strip()
        if draw.textlength(t, font=fnt) <= max_w:
            cur = t
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip(" .,;:") + "…"
    return lines


def fit_text(draw, text, max_w, max_h, start=48, min_size=26, weight="Bold", max_lines=8):
    """Largest font size whose wrapped text fits the box. Returns (font, lines, line_h)."""
    size = start
    while size >= min_size:
        f = font(size, weight)
        lines = wrap(draw, text, f, max_w, max_lines)
        lh = int(size * 1.28)
        if len(lines) * lh <= max_h and not lines[-1].endswith("…"):
            return f, lines, lh
        size -= 3
    f = font(min_size, weight)
    return f, wrap(draw, text, f, max_w, max_lines), int(min_size * 1.28)


def _arrow(d, x0, y0, x1, y1, color, width=4, head=14):
    d.line([x0, y0, x1, y1], fill=color, width=width)
    ang = math.atan2(y1 - y0, x1 - x0)
    p1 = (x1 - head * math.cos(ang - 0.45), y1 - head * math.sin(ang - 0.45))
    p2 = (x1 - head * math.cos(ang + 0.45), y1 - head * math.sin(ang + 0.45))
    d.polygon([(x1, y1), p1, p2], fill=color)


def _centered(d, text, fnt, cx, cy, fill):
    tw = d.textlength(text, font=fnt)
    bbox = fnt.getbbox("Ag")
    th = bbox[3] - bbox[1]
    d.text((cx - tw / 2, cy - th / 2 - bbox[1] * 0.5), text, font=fnt, fill=fill)


# ---------- visual primitives (each draws inside box=(x0,y0,x1,y1)) ----------

def draw_array(im, d, box, spec, pal):
    values = [str(v) for v in (spec.get("values") or [])][:12]
    if not values:
        return False
    hl = {int(i) for i in (spec.get("highlight") or []) if str(i).lstrip("-").isdigit()}
    pointers = {str(k): str(v) for k, v in (spec.get("pointers") or {}).items()}
    x0, y0, x1, y1 = box
    n = len(values)
    gap = 8
    cell = min(72, int((x1 - x0 - gap * (n - 1)) / n))
    total = cell * n + gap * (n - 1)
    sx = x0 + (x1 - x0 - total) // 2
    cy = (y0 + y1) // 2
    f_val = font(30 if cell >= 56 else 22, "Bold")
    f_idx = font(18, "Medium")
    f_ptr = font(20, "Bold")
    for i, v in enumerate(values):
        cx0 = sx + i * (cell + gap)
        fill = pal["accent"] if i in hl else "white"
        d.rounded_rectangle([cx0, cy - cell // 2, cx0 + cell, cy + cell // 2], radius=10,
                            fill=fill, outline=pal["accent"] if i in hl else (203, 213, 225), width=3)
        _centered(d, v[:6], f_val, cx0 + cell / 2, cy, "white" if i in hl else INK)
        _centered(d, str(i), f_idx, cx0 + cell / 2, cy + cell // 2 + 18, GRAY)
        if str(i) in pointers:
            _centered(d, pointers[str(i)][:6], f_ptr, cx0 + cell / 2, cy - cell // 2 - 30, pal["accent"])
            _arrow(d, cx0 + cell / 2, cy - cell // 2 - 16, cx0 + cell / 2, cy - cell // 2 - 4, pal["accent"], 3, 8)
    if spec.get("caption"):
        _centered(d, str(spec["caption"])[:60], font(20, "Medium"), (x0 + x1) / 2, cy + cell // 2 + 52, GRAY)
    return True


_KW = re.compile(r"\b(def|return|for|while|if|elif|else|in|not|and|or|import|from|class|None|True|False|"
                 r"yield|with|as|try|except|lambda|pass|break|continue|const|let|var|function|await|async)\b")


def draw_code(im, d, box, spec, pal):
    code = (spec.get("code") or "").replace("\t", "    ").rstrip()
    lines = [l.rstrip() for l in code.splitlines() if l.strip() or True][:16]
    if not any(l.strip() for l in lines):
        return False
    x0, y0, x1, y1 = box
    d.rounded_rectangle([x0, y0, x1, y1], radius=18, fill=CODE_BG)
    for i, c in enumerate(((255, 95, 86), (255, 189, 46), (39, 201, 63))):      # window dots
        d.ellipse([x0 + 18 + i * 22, y0 + 16, x0 + 30 + i * 22, y0 + 28], fill=c)
    lang = (spec.get("lang") or "python").lower()
    d.text((x1 - 90, y0 + 12), lang[:10], font=mono(16), fill=(148, 163, 184))
    longest = max(len(l) for l in lines)
    size = 22
    while size > 13 and (longest * size * 0.62 > (x1 - x0 - 40) or len(lines) * size * 1.35 > (y1 - y0 - 50)):
        size -= 1
    f = mono(size)
    lh = int(size * 1.35)
    y = y0 + 44
    for line in lines:
        x = x0 + 20
        # comment → gray; string literals → green; keywords → violet; rest → light
        if line.strip().startswith(("#", "//")):
            d.text((x, y), line, font=f, fill=(148, 163, 184))
        else:
            pos = 0
            for m in re.finditer(r"(\"[^\"]*\"|'[^']*')|" + _KW.pattern, line):
                if m.start() > pos:
                    seg = line[pos:m.start()]
                    d.text((x, y), seg, font=f, fill=(226, 232, 240))
                    x += d.textlength(seg, font=f)
                seg = m.group(0)
                d.text((x, y), seg, font=f, fill=(134, 239, 172) if m.group(1) else (196, 181, 253))
                x += d.textlength(seg, font=f)
                pos = m.end()
            if pos < len(line):
                d.text((x, y), line[pos:], font=f, fill=(226, 232, 240))
        y += lh
    return True


def draw_boxes(im, d, box, spec, pal):
    items = [str(s) for s in (spec.get("items") or [])][:8]
    if len(items) < 2:
        return False
    x0, y0, x1, y1 = box
    per_row = len(items) if len(items) <= 4 else math.ceil(len(items) / 2)
    rows = math.ceil(len(items) / per_row)
    gap = 34
    bw = int((x1 - x0 - gap * (per_row - 1)) / per_row)
    bh = 78 if rows == 1 else 68
    f = font(20 if bw >= 150 else 17, "Medium")
    total_h = rows * bh + (rows - 1) * 60
    ty = (y0 + y1) // 2 - total_h // 2
    centers = []
    for i, label in enumerate(items):
        r, c = divmod(i, per_row)
        if r % 2 == 1:                     # snake: second row runs right→left
            c = per_row - 1 - c
        bx = x0 + c * (bw + gap)
        by = ty + r * (bh + 60)
        d.rounded_rectangle([bx, by, bx + bw, by + bh], radius=14, fill="white", outline=pal["accent"], width=3)
        lines = wrap(d, label, f, bw - 16, 2)
        lh = int(f.size * 1.2)
        for k, ln in enumerate(lines):
            _centered(d, ln, f, bx + bw / 2, by + bh / 2 + (k - (len(lines) - 1) / 2) * lh, INK)
        centers.append((bx, by, bx + bw, by + bh))
    for a, b in zip(centers, centers[1:]):
        ax0, ay0, ax1, ay1 = a
        bx0, by0, bx1, by1 = b
        if abs(ay0 - by0) < 5:            # same row
            if bx0 > ax1:
                _arrow(d, ax1 + 4, (ay0 + ay1) / 2, bx0 - 4, (by0 + by1) / 2, pal["accent"])
            else:
                _arrow(d, ax0 - 4, (ay0 + ay1) / 2, bx1 + 4, (by0 + by1) / 2, pal["accent"])
        else:                             # row change: straight down
            _arrow(d, (ax0 + ax1) / 2, ay1 + 4, (bx0 + bx1) / 2, by0 - 4, pal["accent"])
    return True


def draw_graph(im, d, box, spec, pal):
    nodes = [str(n) for n in (spec.get("nodes") or [])][:8]
    edges = spec.get("edges") or []
    if len(nodes) < 2:
        return False
    x0, y0, x1, y1 = box
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    r = min(x1 - x0, y1 - y0) / 2 - 60
    n = len(nodes)
    pos = [(cx + r * math.cos(2 * math.pi * i / n - math.pi / 2),
            cy + r * math.sin(2 * math.pi * i / n - math.pi / 2)) for i in range(n)]
    nr = 46 if n <= 5 else 38
    f = font(17 if n <= 5 else 15, "Medium")
    for e in edges[:16]:
        try:
            a, b = int(e[0]), int(e[1])
        except Exception:
            continue
        if not (0 <= a < n and 0 <= b < n) or a == b:
            continue
        (ax, ay), (bx, by) = pos[a], pos[b]
        ang = math.atan2(by - ay, bx - ax)
        # small perpendicular offset so A→B and B→A don't overlap
        off = 7
        ox, oy = -math.sin(ang) * off, math.cos(ang) * off
        _arrow(d, ax + math.cos(ang) * (nr + 2) + ox, ay + math.sin(ang) * (nr + 2) + oy,
               bx - math.cos(ang) * (nr + 4) + ox, by - math.sin(ang) * (nr + 4) + oy, pal["accent"], 3, 12)
    for i, (px, py) in enumerate(pos):
        d.ellipse([px - nr, py - nr, px + nr, py + nr], fill="white", outline=pal["accent"], width=3)
        lines = wrap(d, nodes[i], f, nr * 2 - 12, 2)
        lh = int(f.size * 1.2)
        for k, ln in enumerate(lines):
            _centered(d, ln, f, px, py + (k - (len(lines) - 1) / 2) * lh, INK)
    return True


def draw_tree(im, d, box, spec, pal):
    vals = spec.get("values") or []
    vals = vals[:15]
    if not vals or vals[0] is None:
        return False
    x0, y0, x1, y1 = box
    depth = int(math.floor(math.log2(len(vals)))) + 1
    nr = 26 if depth <= 3 else 22
    f = font(20 if depth <= 3 else 16, "Bold")
    level_h = (y1 - y0 - 2 * nr - 10) / max(depth - 1, 1)
    pos = {}
    for i, v in enumerate(vals):
        if v is None:
            continue
        lvl = int(math.floor(math.log2(i + 1)))
        idx = i - (2 ** lvl - 1)
        count = 2 ** lvl
        px = x0 + (x1 - x0) * (idx + 0.5) / count
        py = y0 + nr + 5 + lvl * level_h
        pos[i] = (px, py)
    hl = {int(i) for i in (spec.get("highlight") or []) if str(i).isdigit()}
    for i, (px, py) in pos.items():
        if i > 0 and (i - 1) // 2 in pos:
            qx, qy = pos[(i - 1) // 2]
            d.line([qx, qy, px, py], fill=(148, 163, 184), width=3)
    for i, (px, py) in pos.items():
        fill = pal["accent"] if i in hl else "white"
        d.ellipse([px - nr, py - nr, px + nr, py + nr], fill=fill, outline=pal["accent"], width=3)
        _centered(d, str(vals[i])[:4], f, px, py, "white" if i in hl else INK)
    return True


def draw_metric(im, d, box, spec, pal):
    big = str(spec.get("big") or "")
    if not big:
        return False
    x0, y0, x1, y1 = box
    small = str(spec.get("small") or "")
    big2, small2 = str(spec.get("big2") or ""), str(spec.get("small2") or "")
    cols = 2 if big2 else 1
    cw = (x1 - x0) / cols
    for k, (b, s) in enumerate([(big, small), (big2, small2)][:cols]):
        cx = x0 + cw * (k + 0.5)
        size = 76 if len(b) <= 7 else 58 if len(b) <= 12 else 42
        _centered(d, b[:18], font(size), cx, (y0 + y1) / 2 - 24, pal["accent"])
        fs = font(22, "Medium")
        for j, ln in enumerate(wrap(d, s, fs, cw - 30, 2)):
            _centered(d, ln, fs, cx, (y0 + y1) / 2 + 40 + j * 28, GRAY)
    return True


def draw_compare(im, d, box, spec, pal):
    left, right = spec.get("left") or {}, spec.get("right") or {}
    if not (left.get("title") and right.get("title")):
        return False
    x0, y0, x1, y1 = box
    mid = (x0 + x1) / 2
    d.line([mid, y0 + 10, mid, y1 - 10], fill=(203, 213, 225), width=2)
    ft, fp = font(22, "Bold"), font(18, "Regular")
    for k, side in enumerate((left, right)):
        sx = x0 + 10 if k == 0 else mid + 16
        sw = mid - x0 - 26 if k == 0 else x1 - mid - 26
        y = y0 + 6
        for ln in wrap(d, str(side["title"]), ft, sw, 2):
            d.text((sx, y), ln, font=ft, fill=pal["accent"])
            y += 28
        y += 6
        for p in (side.get("points") or [])[:4]:
            lines = wrap(d, "• " + str(p), fp, sw, 2)
            for ln in lines:
                if y > y1 - 22:
                    break
                d.text((sx, y), ln, font=fp, fill=INK)
                y += 24
            y += 4
    return True


def draw_table(im, d, box, spec, pal):
    header = [str(h) for h in (spec.get("header") or [])][:4]
    rows = [[str(c) for c in r][:4] for r in (spec.get("rows") or [])][:5]
    if not header or not rows:
        return False
    x0, y0, x1, y1 = box
    cols = len(header)
    # first column (row labels) narrower when there are 3+ columns
    first = 0.28 if cols >= 3 else 0.5
    widths = [(x1 - x0) * first] + [(x1 - x0) * (1 - first) / (cols - 1)] * (cols - 1) if cols > 1 else [x1 - x0]
    xs = [x0]
    for w in widths[:-1]:
        xs.append(xs[-1] + w)
    rh = min(46, (y1 - y0) / (len(rows) + 1))
    size = 18 if cols <= 3 else 16
    fh, fc = font(size, "Bold"), font(size, "Regular")
    d.rounded_rectangle([x0, y0, x1, y0 + rh], radius=10, fill=pal["tint"])
    for c, h in enumerate(header):
        d.text((xs[c] + 10, y0 + rh / 2 - size * 0.65), ellipsize(d, h, fh, widths[c] - 16), font=fh, fill=pal["accent"])
    for r, row in enumerate(rows):
        ry = y0 + rh * (r + 1)
        if r % 2 == 1:
            d.rectangle([x0, ry, x1, ry + rh], fill=PANEL)
        for c, cell in enumerate(row[:cols]):
            f = fc if c else fh
            d.text((xs[c] + 10, ry + rh / 2 - size * 0.65), ellipsize(d, cell, f, widths[c] - 16), font=f, fill=INK)
    return True


DRAWERS = {"array": draw_array, "code": draw_code, "boxes": draw_boxes, "pipeline": draw_boxes,
           "linkedlist": draw_boxes, "graph": draw_graph, "tree": draw_tree, "metric": draw_metric,
           "compare": draw_compare, "table": draw_table}


# ---------- the card ----------

def render(track, question, visual=None, number=None, difficulty=None, hook=None, footer=None):
    """1200×627 PNG bytes. `question` is the text shown; `visual` the spec dict."""
    from PIL import Image, ImageDraw
    pal = TRACKS.get(track, DEFAULT_TRACK)
    question = clean_text(question)
    visual = clean_spec(visual) if isinstance(visual, dict) else visual
    im = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, 22, H], fill=pal["accent"])
    # label pill + number + difficulty
    label = pal["label"] + (f"  ·  #{number}" if number else "")
    fl = font(20, "Bold")
    lw = d.textlength(label, font=fl)
    d.rounded_rectangle([60, 40, 60 + lw + 36, 78], radius=19, fill=pal["tint"])
    d.text((78, 47), label, font=fl, fill=pal["accent"])
    if difficulty:
        diff = str(difficulty).title()
        col = {"Easy": (5, 150, 105), "Medium": (217, 119, 6), "Hard": (220, 38, 38)}.get(diff, GRAY)
        fd = font(18, "Bold")
        dw = d.textlength(diff, font=fd)
        d.rounded_rectangle([W - 60 - dw - 32, 42, W - 60, 76], radius=17, outline=col, width=2)
        d.text((W - 60 - dw - 16, 49), diff, font=fd, fill=col)

    kind = (visual or {}).get("kind", "none") if isinstance(visual, dict) else "none"
    has_visual = kind in DRAWERS
    text_w = 560 if has_visual else W - 140
    f, lines, lh = fit_text(d, question, text_w, 380, start=44 if has_visual else 50, min_size=24,
                            max_lines=9 if has_visual else 7)
    y = 110
    for ln in lines:
        d.text((60, y), ln, font=f, fill=INK)
        y += lh
    if has_visual:
        box = (660, 104, W - 50, H - 92)
        d.rounded_rectangle([box[0] - 14, box[1] - 14, box[2] + 14, box[3] + 14], radius=22, fill=PANEL)
        ok = False
        try:
            ok = DRAWERS[kind](im, d, box, visual, pal)
        except Exception as e:                  # a bad spec must never kill the post
            log.warning("visual %s failed: %s", kind, e)
        if not ok:
            d.rounded_rectangle([box[0] - 14, box[1] - 14, box[2] + 14, box[3] + 14], radius=22, fill="white")
            f, lines, lh = fit_text(d, question, W - 140, 380, start=50, min_size=24, max_lines=7)
            d.rectangle([40, 100, W, H - 80], fill="white")
            y = 110
            for ln in lines:
                d.text((60, y), ln, font=f, fill=INK)
                y += lh
    # footer
    d.line([60, H - 70, W - 60, H - 70], fill=(229, 231, 235), width=2)
    d.text((60, H - 56), footer or "Answer is under “…more”  ·  comment yours first", font=font(22, "Medium"), fill=INK)
    right = "follow for a new question every day"
    fr = font(20, "Regular")
    d.text((W - 60 - d.textlength(right, font=fr), H - 54), right, font=fr, fill=GRAY)
    buf = io.BytesIO()
    im.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
