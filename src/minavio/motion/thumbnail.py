"""Miniatures : Remotion Still `thumbnail` (PNG), repli Pillow, puis FFmpeg en dernier recours.

Les images sont inlinées en data URI dans le fichier de props : Chromium ne peut pas lire un
chemin disque ou `file://` depuis la page de rendu (origine http://localhost), et cela évite de
copier des fichiers dans `motion/public/`. Fonctionne donc hors ligne.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import mimetypes
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from minavio.config.schema import ThumbnailFormat
from minavio.core.models import ThumbnailChoice
from minavio.errors import MotionUnavailableError
from minavio.motion import render

log = logging.getLogger(__name__)

YOUTUBE_MAX_BYTES = 2 * 1024 * 1024
_MAX_SIDE = 2560  # côté max des images inlinées (suffisant pour du 1920x1080)
_TIMEOUT_S = 300
INK = (0, 0, 0)
BG = (16, 16, 28)

# Crâne 8x8 (même bitmap que motion/src/lib/pixel.tsx) : 1 = plein, 2 = orbite.
_SKULL = (
    "00111100",
    "01111110",
    "11111111",
    "12211221",
    "12211221",
    "11111111",
    "01122110",
    "01011010",
)


# --- Props / images ----------------------------------------------------------------------------


def _import_pil() -> Any | None:
    try:
        from PIL import Image
    except ImportError:
        return None
    return Image


def image_data_uri(path: Path) -> str:
    """Image disque → data URI (réduite à `_MAX_SIDE` et recompressée si Pillow est présent)."""
    if not path.is_file():
        raise MotionUnavailableError(f"Image de miniature introuvable : {path}")
    Image = _import_pil()
    if Image is not None:
        with Image.open(path) as im:
            im = im.convert("RGB")
            im.thumbnail((_MAX_SIDE, _MAX_SIDE))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=90)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def build_props(
    choice: ThumbnailChoice,
    fmt: ThumbnailFormat,
    theme: dict[str, str],
    badge: str | None = None,
    kills: int | None = None,
    highlight: tuple[float, float] | None = None,
) -> dict[str, Any]:
    props: dict[str, Any] = {
        "layout": choice.layout,
        "images": [image_data_uri(Path(c.image)) for c in choice.images],
        "text": choice.text,
        "width": fmt.width,
        "height": fmt.height,
        "theme": {**render.DEFAULT_THEME, **theme},
    }
    if badge:
        props["badge"] = badge
    if kills:
        props["kills"] = int(kills)
    if highlight:
        props["highlight"] = {"x": highlight[0], "y": highlight[1]}
    return props


# --- Remotion ----------------------------------------------------------------------------------


def _render_remotion(props: dict[str, Any], out: Path) -> None:
    props_file = out.with_suffix(".props.json")
    props_file.write_text(json.dumps(props, ensure_ascii=False), encoding="utf-8")
    cmd = [
        "npx",
        "--no-install",
        "remotion",
        "still",
        "src/index.ts",
        "thumbnail",
        str(out.resolve()),
        f"--props={props_file.resolve()}",
    ]
    browser = os.environ.get("MINAVIO_BROWSER_EXECUTABLE")
    if browser:
        cmd.append(f"--browser-executable={browser}")
    try:
        proc = subprocess.run(
            cmd,
            cwd=render.motion_dir(),
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_S,
            check=False,
        )
    finally:
        props_file.unlink(missing_ok=True)
    if proc.returncode != 0 or not out.exists():
        tail = (proc.stderr or proc.stdout or "").strip()[-600:]
        out.unlink(missing_ok=True)
        raise MotionUnavailableError(
            f"Rendu Remotion de la miniature en échec (code {proc.returncode}). "
            f"Commande : {' '.join(cmd[:6])} …\n{tail}"
        )


# --- Repli Pillow ------------------------------------------------------------------------------


def _hex(color: str) -> tuple[int, int, int]:
    c = color.lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def fit_text(
    words: list[str], max_w: float, max_h: float, max_font: float
) -> tuple[list[str], int]:
    """Découpe en 1..3 lignes la plus grande taille possible (fonte à chasse fixe : 1 em/car.)."""
    n = len(words)
    if n == 0:
        return [], 0
    best: tuple[list[str], float] = ([" ".join(words)], 0.0)

    def partitions(start: int, left: int) -> list[list[str]]:
        if left == 1:
            return [[" ".join(words[start:])]]
        out: list[list[str]] = []
        for e in range(start + 1, n - left + 2):
            for rest in partitions(e, left - 1):
                out.append([" ".join(words[start:e]), *rest])
        return out

    for k in range(1, min(3, n) + 1):
        for lines in partitions(0, k):
            longest = max(len(line) for line in lines)
            fs = min(max_font, max_w / longest, max_h / (len(lines) * 1.35))
            if fs > best[1]:
                best = (lines, fs)
    return best[0], int(best[1])


class _Painter:
    """Petite boîte à outils Pillow pour les trois compositions."""

    def __init__(self, fmt: ThumbnailFormat, theme: dict[str, str]) -> None:
        from PIL import Image, ImageDraw

        self.Image, self.ImageDraw = Image, ImageDraw
        self.W, self.H = fmt.width, fmt.height
        self.portrait = self.H > self.W
        self.u = max(3, round(min(self.W, self.H) / 90))
        self.primary = _hex(theme.get("primary", render.DEFAULT_THEME["primary"]))
        self.accent = _hex(theme.get("accent", render.DEFAULT_THEME["accent"]))
        self.font_path = render.find_arcade_font()
        self.canvas = Image.new("RGB", (self.W, self.H), BG)
        self.draw = ImageDraw.Draw(self.canvas)

    def font(self, size: int) -> Any:
        from PIL import ImageFont

        if self.font_path:
            return ImageFont.truetype(str(self.font_path), max(size, 1))
        return ImageFont.load_default(size=max(size, 1))

    def photo(
        self, path: Path, size: tuple[int, int], zoom: float = 1.0, vignette: int = 150
    ) -> Any:
        """Image recadrée « cover », sur-saturée, vignettée."""
        from PIL import ImageEnhance, ImageOps

        im = self.Image.open(path).convert("RGB")
        if zoom > 1:
            w, h = im.size
            cw, ch = round(w / zoom), round(h / zoom)
            im = im.crop(((w - cw) // 2, (h - ch) // 2, (w - cw) // 2 + cw, (h - ch) // 2 + ch))
        im = ImageOps.fit(im, size, method=self.Image.LANCZOS, centering=(0.5, 0.4))
        im = ImageEnhance.Contrast(ImageEnhance.Color(im).enhance(1.35)).enhance(1.12)
        # Vignette : dégradé radial 0 (centre) -> 255 (bords) assombrissant les coins.
        radial = self.Image.radial_gradient("L").resize(size, self.Image.BILINEAR)
        mask = radial.point(lambda v: max(0, v - 130) * vignette // 125)
        return self.Image.composite(self.Image.new("RGB", size, INK), im, mask)

    def paste_framed(self, im: Any, xy: tuple[int, int], color: tuple[int, int, int]) -> None:
        """Image collée avec bordure pixel : couleur puis noir, plus ombre décalée."""
        u, (x, y), (w, h) = self.u, xy, im.size
        self.draw.rectangle((x + u * 3, y + u * 3, x + w + u * 5, y + h + u * 5), fill=(8, 8, 12))
        self.draw.rectangle((x - u * 2, y - u * 2, x + w + u * 2 - 1, y + h + u * 2 - 1), fill=INK)
        self.draw.rectangle((x - u, y - u, x + w + u - 1, y + h + u - 1), fill=color)
        self.canvas.paste(im, xy)

    def text_block(
        self,
        text: str,
        box: tuple[int, int, int, int],
        *,
        align: str = "left",
        valign: str = "bottom",
    ) -> None:
        """Gros texte : contour noir épais, ombre en blocs (primary), dernière ligne en accent."""
        x, y, w, h = box
        lines, fs = fit_text(text.upper().split(), w - self.u * 3, h, self.u * 22)
        if not lines:
            return
        font = self.font(fs)
        o = max(round(self.u * 0.75), round(fs / 14))
        lh = round(fs * 1.3)
        top = y if valign == "top" else y + h - lh * len(lines)
        for i, line in enumerate(lines):
            tw = self.draw.textlength(line, font=font)
            lx = x + (w - tw) / 2 if align == "center" else x
            ly = top + i * lh
            fill = self.accent if i == len(lines) - 1 else (255, 255, 255)
            for k in (3, 2, 1):  # ombre en escalier
                off = o + o * k // 2
                self.draw.text(
                    (lx + off, ly + off),
                    line,
                    font=font,
                    fill=self.primary,
                    stroke_width=o,
                    stroke_fill=self.primary,
                )
            self.draw.text((lx, ly), line, font=font, fill=fill, stroke_width=o, stroke_fill=INK)

    def badge(self, text: str, x: int, y: int) -> None:
        u = self.u
        font = self.font(u * 3)
        label = text.upper()
        tw = self.draw.textlength(label, font=font)
        w, h = int(tw + u * 5), u * 6
        d = self.draw
        d.rectangle((x + u * 2, y + u * 2, x + w + u * 2, y + h + u * 2), fill=(8, 8, 12))
        d.rectangle((x - u, y - u, x + w + u, y + h + u), fill=INK)
        d.rectangle((x, y, x + w, y + h), fill=self.accent)
        d.rectangle((x + u // 2, y + u // 2, x + w - u // 2, y + h - u // 2), fill=self.primary)
        d.text((x + u * 2.5 + u / 2, y + u * 1.5 + u / 2), label, font=font, fill=INK)
        d.text((x + u * 2.5, y + u * 1.5), label, font=font, fill=(255, 255, 255))

    def kills(self, kills: int, right: int, y: int) -> None:
        u = self.u
        px = max(2, round(u * 0.9))
        compact = kills > 5
        count = 1 if compact else kills
        label = f"x{kills}" if compact else ""
        font = self.font(u * 5)
        lw = int(self.draw.textlength(label, font=font)) + u if compact else 0
        w = count * (px * 8 + u) - u + lw + u * 3
        h = px * 8 + u * 3
        x1 = self.W - right
        x0 = x1 - w
        d = self.draw
        d.rectangle(
            (x0 - u * 3 // 2, y - u * 3 // 2, x1 + u * 3 // 2, y + h + u * 3 // 2), fill=INK
        )
        d.rectangle((x0 - u, y - u, x1 + u, y + h + u), fill=self.primary)
        d.rectangle((x0, y, x1, y + h), fill=(10, 10, 14))
        for i in range(count):
            sx, sy = x0 + u * 3 // 2 + i * (px * 8 + u), y + u * 3 // 2
            self._skull(sx, sy, px)
        if compact:
            tx = x0 + u * 3 // 2 + px * 8 + u
            d.text((tx + u // 2, y + u), label, font=font, fill=self.primary)
            d.text((tx, y + u // 2), label, font=font, fill=self.accent)

    def _skull(self, x: int, y: int, px: int) -> None:
        d = self.draw
        for j, row in enumerate(_SKULL):
            for i, c in enumerate(row):
                if c != "0":
                    d.rectangle(
                        (x + i * px - 1, y + j * px - 1, x + (i + 1) * px, y + (j + 1) * px),
                        fill=INK,
                    )
        for j, row in enumerate(_SKULL):
            for i, c in enumerate(row):
                if c != "0":
                    fill = self.primary if c == "2" else self.accent
                    d.rectangle(
                        (x + i * px, y + j * px, x + (i + 1) * px - 1, y + (j + 1) * px - 1),
                        fill=fill,
                    )

    def stair(
        self, offset: int, a_len: int, b_len: int, step: int, skew: float
    ) -> list[tuple[int, int]]:
        """Moitié « avant » de la diagonale crénelée (repère a x b), convertie si portrait."""
        pts: list[tuple[int, int]] = [(0, 0)]
        for i in range(-(-b_len // step)):
            b0, b1 = i * step, min(b_len, (i + 1) * step)
            a = round((a_len / 2 - ((b0 + b1) / 2 / b_len - 0.5) * skew) / step) * step + offset
            pts += [(a, b0), (a, b1)]
        pts.append((0, b_len))
        return [(b, a) for a, b in pts] if self.portrait else pts


def render_thumbnail_pillow(
    choice: ThumbnailChoice,
    fmt: ThumbnailFormat,
    theme: dict[str, str],
    out: Path,
    badge: str | None = None,
    kills: int | None = None,
    highlight: tuple[float, float] | None = None,
) -> Path:
    """Composition Pillow simplifiée (hero / split / collage), sans Node."""
    pt = _Painter(fmt, theme)
    W, H, u, portrait = pt.W, pt.H, pt.u, pt.portrait
    paths = [Path(c.image) for c in choice.images]
    for p in paths:
        if not p.is_file():
            raise MotionUnavailableError(f"Image de miniature introuvable : {p}")
    imgs = [paths[i % len(paths)] for i in range(3)]
    m = round(min(W, H) * 0.035)
    safe_r = round(W * 0.14) if portrait else 0
    top_y = round(H * 0.04) if portrait else m
    text_w = W - 2 * m - safe_r
    ptext = (m, round(H * 0.11), text_w, round(H * 0.24))
    layout = choice.layout

    if layout == "split":
        a_len, b_len = (H, W) if portrait else (W, H)
        half = round(a_len * 0.6)
        size = (W, half) if portrait else (half, H)
        pt.canvas.paste(pt.photo(imgs[1], size), (0, H - half) if portrait else (W - half, 0))
        step, skew, band = u * 3, a_len * 0.14, round(u * 2.5)
        pt.draw.polygon(pt.stair(band + u, a_len, b_len, step, skew), fill=INK)
        pt.draw.polygon(pt.stair(band, a_len, b_len, step, skew), fill=pt.accent)
        mask = pt.Image.new("L", (W, H), 0)
        pt.ImageDraw.Draw(mask).polygon(pt.stair(0, a_len, b_len, step, skew), fill=255)
        layer = pt.Image.new("RGB", (W, H), BG)
        layer.paste(pt.photo(imgs[0], size), (0, 0))
        pt.canvas.paste(layer, (0, 0), mask)
        vs = round(min(W, H) * (0.2 if portrait else 0.3))
        font = pt.font(vs)
        tw = pt.draw.textlength("VS", font=font)
        o = max(2, vs // 12)
        vx, vy = (W - tw) / 2, (H - vs) / 2 - (0 if portrait else H * 0.05)
        for k in (3, 2, 1):
            off = o + o * k // 2
            pt.draw.text(
                (vx + off, vy + off),
                "VS",
                font=font,
                fill=pt.primary,
                stroke_width=o,
                stroke_fill=pt.primary,
            )
        pt.draw.text((vx, vy), "VS", font=font, fill=pt.accent, stroke_width=o, stroke_fill=INK)
        box = ptext if portrait else (m, round(H * 0.72), W - 2 * m, round(H * 0.25))
        pt.text_block(choice.text, box, align="center", valign="top" if portrait else "bottom")
    elif layout == "collage":
        g, b = u * 3, u * 2
        if portrait:
            x0 = m + b
            uw = W - 2 * x0
            big = (x0, round(H * 0.17), uw, round(H * 0.47))
            sw = (uw - g - b * 2) // 2
            st = big[1] + big[3] + g + b * 2
            sh = round(H * 0.87) - st
            smalls = [(x0, st, sw, sh), (x0 + sw + g + b * 2, st, sw, sh)]
            box = (x0, round(H * 0.1), uw - safe_r, round(H * 0.15))
        else:
            uh = H - 2 * (m + b)
            bw = round(W * 0.62)
            big = (m + b, m + b, bw, uh)
            sx = big[0] + bw + g + b * 2
            sw = W - m - b - sx
            sh = (uh - g - b * 2) // 2
            smalls = [(sx, m + b, sw, sh), (sx, m + b + sh + g + b * 2, sw, sh)]
            box = (big[0] + u * 2, round(H * 0.45), bw - u * 4, round(H * 0.46))
        # fond rayé
        for d in range(-H, W, u * 8):
            pt.draw.polygon(
                [(d, 0), (d + u * 4, 0), (d + u * 4 + H, H), (d + H, H)], fill=(40, 22, 36)
            )
        pt.paste_framed(pt.photo(imgs[0], (big[2], big[3]), zoom=1.08), (big[0], big[1]), pt.accent)
        for i, s in enumerate(smalls):
            pt.paste_framed(
                pt.photo(imgs[i + 1], (s[2], s[3])),
                (s[0], s[1]),
                pt.primary if i == 0 else (255, 255, 255),
            )
        pt.text_block(choice.text, box, valign="top" if portrait else "bottom")
    else:  # hero
        hx, hy = highlight if highlight else (0.5, 0.42)
        im = pt.photo(imgs[0], (W, H), zoom=1.12, vignette=180)
        grad = pt.Image.linear_gradient("L").resize((W, H))  # noir en haut -> blanc en bas
        if portrait:
            grad = grad.transpose(pt.Image.FLIP_TOP_BOTTOM)
        shade = grad.point(lambda v: max(0, v - 100) * 170 // 155)
        im.paste(pt.Image.new("RGB", (W, H), INK), (0, 0), shade)
        pt.canvas.paste(im, (0, 0))
        if highlight:
            _ring(pt, hx * W, hy * H, round(min(W, H) * 0.13))
        box = ptext if portrait else (m, round(H * 0.5), round(W * 0.7), round(H * 0.46))
        pt.text_block(choice.text, box, valign="top" if portrait else "bottom")

    if badge:
        pt.badge(badge, m, top_y)
    if kills:
        pt.kills(int(kills), m + safe_r, top_y)
    out.parent.mkdir(parents=True, exist_ok=True)
    pt.canvas.save(out)
    return out


def _ring(pt: _Painter, cx: float, cy: float, r: int) -> None:
    """Anneau en cellules carrées + petite flèche pixel, contour noir."""
    cell = max(2, round(pt.u * 0.9))
    n = r // cell + 2
    inner = r - cell * 1.6
    cells = [
        (i, j)
        for j in range(-n, n + 1)
        for i in range(-n, n + 1)
        if inner <= (i * i + j * j) ** 0.5 * cell <= r
    ]
    for color, grow in ((INK, cell // 2 + 1), (pt.accent, 0)):
        for i, j in cells:
            x, y = cx + i * cell, cy + j * cell
            pt.draw.rectangle(
                (x - grow, y - grow, x + cell - 1 + grow, y + cell - 1 + grow), fill=color
            )


# --- Dernier recours FFmpeg --------------------------------------------------------------------


def _render_ffmpeg(
    choice: ThumbnailChoice, fmt: ThumbnailFormat, theme: dict[str, str], out: Path
) -> Path:
    """Sans Pillow : image 1 recadrée + texte (drawtext). Évite l'absence totale de miniature."""
    if shutil.which("ffmpeg") is None:
        raise MotionUnavailableError(
            "Miniature impossible : ni Node/Remotion, ni Pillow, ni FFmpeg. "
            "Installez Pillow (`uv pip install pillow`) ou Node (`cd motion && npm install`)."
        )
    font = render.find_arcade_font()
    text_file = out.with_suffix(".txt")
    text_file.write_text(choice.text.upper(), encoding="utf-8")
    accent = "0x" + theme.get("accent", render.DEFAULT_THEME["accent"]).lstrip("#")[:6]
    size = max(16, fmt.height // 9)
    vf = (
        f"scale={fmt.width}:{fmt.height}:force_original_aspect_ratio=increase,"
        f"crop={fmt.width}:{fmt.height},eq=saturation=1.35:contrast=1.1,"
        f"drawtext=textfile='{render._ff_path(text_file)}':fontcolor={accent}:fontsize={size}"
        f":borderw={max(2, size // 10)}:bordercolor=black:x=(w-text_w)/2:y=h*0.1"
        + (f":fontfile='{render._ff_path(font)}'" if font else "")
    )
    cmd = [
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(Path(choice.images[0].image)),
        "-vf",
        vf,
        "-frames:v",
        "1",
        str(out),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=_TIMEOUT_S, check=False)
    finally:
        text_file.unlink(missing_ok=True)
    if proc.returncode != 0 or not out.exists():
        raise MotionUnavailableError(
            f"Repli FFmpeg de la miniature en échec : {(proc.stderr or '').strip()[-400:]}"
        )
    return out


# --- API ---------------------------------------------------------------------------------------


def jpeg_companion(png: Path) -> Path:
    return png.with_suffix(".jpg")


def upload_path(png: Path) -> Path:
    """Fichier à envoyer à YouTube : le JPEG ≤ 2 Mo s'il existe, sinon le PNG."""
    jpg = jpeg_companion(png)
    return jpg if jpg.exists() else png


def _shrink_to_jpeg(png: Path) -> Path | None:
    """JPEG ≤ 2 Mo à côté d'un PNG trop lourd (qualité décroissante, puis réduction)."""
    Image = _import_pil()
    jpg = jpeg_companion(png)
    if Image is not None:
        with Image.open(png) as src:
            im = src.convert("RGB")
        for scale in (1.0, 0.85, 0.7):
            cur = (
                im
                if scale == 1.0
                else im.resize((round(im.width * scale), round(im.height * scale)))
            )
            for q in (92, 85, 78, 70):
                cur.save(jpg, "JPEG", quality=q, optimize=True)
                if jpg.stat().st_size <= YOUTUBE_MAX_BYTES:
                    return jpg
        return jpg
    if shutil.which("ffmpeg"):
        for q in (3, 6, 10, 16):
            cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(png), "-q:v", str(q), str(jpg)]
            subprocess.run(cmd, capture_output=True, timeout=_TIMEOUT_S, check=False)
            if jpg.exists() and jpg.stat().st_size <= YOUTUBE_MAX_BYTES:
                return jpg
    return jpg if jpg.exists() else None


def render_thumbnail(
    choice: ThumbnailChoice,
    fmt: ThumbnailFormat,
    theme: dict[str, str],
    out: Path,
    badge: str | None = None,
    kills: int | None = None,
    highlight: tuple[float, float] | None = None,
) -> Path:
    """Produit `out` (PNG). Remotion si dispo, sinon Pillow, sinon FFmpeg : toujours un fichier.

    Si le PNG dépasse 2 Mo (limite YouTube), un JPEG `out.with_suffix('.jpg')` est écrit à côté
    (voir `upload_path`). `highlight` = (x, y) normalisés du point d'action (layout hero).
    """
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    rendered = False
    if render.remotion_available():
        try:
            _render_remotion(build_props(choice, fmt, theme, badge, kills, highlight), out)
            rendered = True
        except MotionUnavailableError as exc:
            log.warning("Remotion indisponible pour la miniature, repli Pillow : %s", exc)
    else:
        log.info("Node/Remotion absent : miniature composée avec Pillow.")
    if not rendered:
        if _import_pil() is not None:
            render_thumbnail_pillow(choice, fmt, theme, out, badge, kills, highlight)
        else:
            _render_ffmpeg(choice, fmt, theme, out)
    if out.stat().st_size > YOUTUBE_MAX_BYTES:
        _shrink_to_jpeg(out)
    return out
