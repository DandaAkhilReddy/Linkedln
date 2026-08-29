"""
Branded LinkedIn card generator — one reusable template per company, with the
company's REAL logo composited into the corner plate when the PNG is present.

Logos live in the blob container `linkedin-logos` (or a local ./logos dir),
named microsoft.png / apple.png / google.png / amazon.png / meta.png / nvidia.png.
If a logo is missing, the card renders with the company name text as a stand-in.
"""

import io
import os
import logging
from datetime import datetime

log = logging.getLogger("cards")

# Per-company theme: accent bar + gradient base + display name
THEME = {
    "microsoft": {"accent": "#0a66c2", "base": (10, 25, 41),  "name": "MICROSOFT"},
    "apple":     {"accent": "#a2aaad", "base": (20, 20, 22),  "name": "APPLE"},
    "google":    {"accent": "#4285f4", "base": (12, 20, 33),  "name": "GOOGLE"},
    "amazon":    {"accent": "#ff9900", "base": (18, 20, 24),  "name": "AMAZON"},
    "meta":      {"accent": "#0866ff", "base": (11, 16, 33),  "name": "META"},
    "nvidia":    {"accent": "#76b900", "base": (12, 20, 12),  "name": "NVIDIA"},
    "openai":    {"accent": "#10a37f", "base": (13, 17, 23),  "name": "OPENAI"},
    "anthropic": {"accent": "#cc785c", "base": (26, 22, 19),  "name": "ANTHROPIC"},
    "netflix":   {"accent": "#e50914", "base": (18, 18, 18),  "name": "NETFLIX"},
    "xai":       {"accent": "#c9ced6", "base": (10, 10, 12),  "name": "XAI"},
}

DISPLAY = {"microsoft": "Microsoft", "apple": "Apple", "google": "Google",
           "amazon": "Amazon", "meta": "Meta", "nvidia": "NVIDIA",
           "openai": "OpenAI", "anthropic": "Anthropic", "netflix": "Netflix",
           "xai": "xAI"}


def display_name(company):
    return DISPLAY.get(company, company.title())

W, H = 1200, 627


def _font(size, bold=False):
    from PIL import ImageFont
    path = ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold
            else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        try:
            return ImageFont.load_default(size=size)
        except TypeError:
            return ImageFont.load_default()


def _logo_bytes(company, logo_loader=None):
    """logo_loader(company)->bytes|None lets the caller pull from blob storage.
    Falls back to a local ./logos/<company>.png if present."""
    if logo_loader:
        try:
            b = logo_loader(company)
            if b:
                return b
        except Exception as e:
            log.warning("logo_loader failed for %s: %s", company, e)
    p = os.path.join(os.path.dirname(__file__), "logos", f"{company}.png")
    if os.path.exists(p):
        with open(p, "rb") as f:
            return f.read()
    return None


def _trim_logo(im):
    """Crop surrounding white/transparent margin so only the logo mark remains."""
    from PIL import Image, ImageChops
    im = im.convert("RGBA")
    alpha = im.getchannel("A")
    if alpha.getextrema()[0] < 255:            # has real transparency
        bbox = alpha.getbbox()
    else:                                      # white background — trim white
        rgb = im.convert("RGB")
        bg = Image.new("RGB", im.size, (255, 255, 255))
        diff = ImageChops.difference(rgb, bg)
        bbox = diff.getbbox()
    if bbox:
        im = im.crop(bbox)
    return im


def build_card(company, jobs, date_str=None, part=None, total=None, logo_loader=None):
    """Logo-only card: the company logo centered on a clean white canvas.
    All job details live in the post caption, not the image."""
    from PIL import Image, ImageDraw
    theme = THEME.get(company, {"accent": "#0a66c2", "base": (14, 20, 30),
                                "name": company.upper()})
    img = Image.new("RGB", (W, H), "#ffffff")
    d = ImageDraw.Draw(img)

    logo = _logo_bytes(company, logo_loader)
    placed = False
    if logo:
        try:
            lg = _trim_logo(Image.open(io.BytesIO(logo)))
            max_w, max_h = int(W * 0.78), int(H * 0.62)
            scale = min(max_w / lg.width, max_h / lg.height, 3.2)
            lg = lg.resize((max(1, int(lg.width * scale)), max(1, int(lg.height * scale))),
                           Image.LANCZOS)
            img.paste(lg, ((W - lg.width) // 2, (H - lg.height) // 2), lg)
            placed = True
        except Exception as e:
            log.warning("logo composite failed for %s: %s", company, e)
    if not placed:
        d.text((W // 2 - 160, H // 2 - 34), theme["name"],
               font=_font(70, True), fill=theme["accent"])

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def split_into(jobs, n):
    """Split a job list into n roughly-equal chunks (drops empty tail chunks)."""
    if not jobs:
        return []
    n = max(1, min(n, len(jobs)))
    size = (len(jobs) + n - 1) // n
    return [jobs[i:i + size] for i in range(0, len(jobs), size)]
