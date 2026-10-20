"""Rendu des overlays : Remotion (ProRes 4444 alpha) avec repli FFmpeg `drawtext`."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from minavio.config.schema import OutputFormat
from minavio.core.models import Overlay, Timeline
from minavio.errors import FFmpegError, MotionUnavailableError

log = logging.getLogger(__name__)

TEMPLATES = (
    "captions",
    "title",
    "popup",
    "progress_bar",
    "reaction",
    "kill_counter",
    "rewind",
    "rank_card",
)
FALLBACK_TEMPLATES = ("title", "captions", "kill_counter", "rank_card")
KILL_LABELS = ("KILL", "DOUBLE KILL", "TRIPLE KILL", "SQUAD WIPED", "SQUAD WIPED")
DEFAULT_THEME = {"primary": "#FF3B30", "accent": "#FFD60A"}
_RENDER_TIMEOUT_S = 600


def motion_dir() -> Path:
    """Projet Remotion (`motion/`), surchargeable par `MINAVIO_MOTION_DIR`."""
    env = os.environ.get("MINAVIO_MOTION_DIR")
    return Path(env) if env else Path(__file__).parents[3] / "motion"


def remotion_available() -> bool:
    return shutil.which("npx") is not None and (motion_dir() / "node_modules").is_dir()


def composition_id(template: str) -> str:
    """Remotion n'accepte pas `_` dans un id : `progress_bar` → `progress-bar`."""
    return template.replace("_", "-")


def build_props(
    overlay: Overlay, fmt: OutputFormat, theme: dict[str, str], beats: list[float] | None = None
) -> dict[str, Any]:
    """Props Remotion : props de l'overlay + format + thème + beats relatifs à l'overlay."""
    props: dict[str, Any] = dict(overlay.props)
    props.setdefault("beats", [round(b, 3) for b in (beats or [])])
    props.update(
        width=fmt.width,
        height=fmt.height,
        fps=fmt.fps,
        durationInFrames=max(1, math.ceil(overlay.duration * fmt.fps)),
        theme={**DEFAULT_THEME, **theme},
    )
    return props


def _relative_beats(timeline: Timeline, overlay: Overlay) -> list[float]:
    end = overlay.start + overlay.duration
    return [b - overlay.start for b in timeline.beats if overlay.start <= b < end]


def props_hash(template: str, props: dict[str, Any]) -> str:
    blob = json.dumps({"t": template, "p": props}, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _run(cmd: list[str], cwd: Path | None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, timeout=_RENDER_TIMEOUT_S, check=False
    )


def _render_remotion(template: str, props: dict[str, Any], out: Path, out_dir: Path) -> None:
    props_file = out.with_suffix(".props.json")
    props_file.write_text(json.dumps(props, ensure_ascii=False), encoding="utf-8")
    cmd = [
        "npx",
        "--no-install",
        "remotion",
        "render",
        "src/index.ts",
        composition_id(template),
        str(out.resolve()),
        f"--props={props_file.resolve()}",
        "--codec=prores",
        "--prores-profile=4444",
        "--pixel-format=yuva444p10le",
        "--image-format=png",
    ]
    browser = os.environ.get("MINAVIO_BROWSER_EXECUTABLE")
    if browser:
        cmd.append(f"--browser-executable={browser}")
    proc = _run(cmd, motion_dir())
    if proc.returncode != 0 or not out.exists():
        tail = (proc.stderr or proc.stdout or "").strip()[-600:]
        out.unlink(missing_ok=True)
        raise MotionUnavailableError(
            f"Rendu Remotion de '{template}' en échec (code {proc.returncode}). "
            f"Commande : {' '.join(cmd)}\n{tail}"
        )


# --- Repli FFmpeg drawtext ---------------------------------------------------------------------


def find_arcade_font() -> Path | None:
    """Cherche une fonte arcade (Press Start 2P) ; `MINAVIO_FONT` prioritaire."""
    env = os.environ.get("MINAVIO_FONT")
    if env and Path(env).is_file():
        return Path(env)
    roots = [motion_dir() / "public" / "fonts", Path.home() / ".fonts", Path("/usr/share/fonts")]
    for root in roots:
        if root.is_dir():
            for pattern in ("PressStart2P*.ttf", "PressStart2P*.otf"):
                found = next(root.rglob(pattern), None)
                if found:
                    return found
    return None


def _ff_color(color: str) -> str:
    return "0x" + color.lstrip("#")[:6]


def _ff_path(path: Path) -> str:
    return str(path).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")


def _drawtext(
    textfile: Path,
    *,
    fmt: OutputFormat,
    position: str,
    color: str,
    shadow: str,
    font: Path | None,
    enable: str | None = None,
) -> str:
    portrait = fmt.height > fmt.width
    size = max(16, round(min(fmt.width, fmt.height) / (16 if portrait else 18)))
    cx = "w*0.44" if portrait else "w*0.5"  # centre de la zone sûre (colonne UI à droite)
    y = {
        "top": "h*0.10",
        "center": "(h-text_h)/2",
        "bottom": "h*0.85-text_h",
    }.get(position, "(h-text_h)/2")
    border = max(2, size // 8)
    parts = [
        f"textfile='{_ff_path(textfile)}'",
        "expansion=none",
        f"fontsize={size}",
        f"fontcolor={_ff_color(color)}",
        f"borderw={border}",
        "bordercolor=0x000000",
        f"shadowcolor={_ff_color(shadow)}",
        f"shadowx={border}",
        f"shadowy={border}",
        f"x={cx}-text_w/2",
        f"y={y}",
    ]
    if font:
        parts.insert(0, f"fontfile='{_ff_path(font)}'")
    if enable:
        parts.append(f"enable='{enable}'")
    return "drawtext=" + ":".join(parts)


def kill_label(props: dict[str, Any]) -> str:
    """Libellé du compteur de kills (même logique que `KillCounter.tsx`)."""
    label = str(props.get("label") or "").strip()
    if label:
        return label.upper()
    kills = min(5, max(1, int(props.get("kills", 1))))
    return KILL_LABELS[kills - 1]


def _render_ffmpeg_fallback(
    overlay: Overlay, props: dict[str, Any], fmt: OutputFormat, out: Path
) -> None:
    font = find_arcade_font()
    theme = props["theme"]
    default_pos = {"title": "center", "kill_counter": "top", "rank_card": "top"}
    position = str(props.get("position", default_pos.get(overlay.template, "bottom")))
    filters: list[str] = []
    if overlay.template in ("kill_counter", "rank_card"):
        text = (
            kill_label(props) if overlay.template == "kill_counter" else f"#{props.get('rank', 1)}"
        )
        tf = out.with_suffix(".txt")
        tf.write_text(text, encoding="utf-8")
        filters.append(
            _drawtext(
                tf,
                fmt=fmt,
                position=position,
                color=theme["accent"],
                shadow=theme["primary"],
                font=font,
            )
        )
    elif overlay.template == "title":
        text = str(props.get("text", "PRESS START")).upper()
        tf = out.with_suffix(".txt")
        tf.write_text(text, encoding="utf-8")
        filters.append(
            _drawtext(
                tf,
                fmt=fmt,
                position=position,
                color="#FFFFFF",
                shadow=theme["primary"],
                font=font,
            )
        )
    else:  # captions : un mot à la fois, mot courant en `accent`
        for i, w in enumerate(props.get("words", [])):
            tf = out.with_suffix(f".w{i}.txt")
            tf.write_text(str(w["text"]).upper(), encoding="utf-8")
            end = max(float(w["end"]), float(w["start"]) + 0.05)
            filters.append(
                _drawtext(
                    tf,
                    fmt=fmt,
                    position=position,
                    color=theme["accent"],
                    shadow=theme["primary"],
                    font=font,
                    enable=f"between(t,{float(w['start']):.3f},{end:.3f})",
                )
            )
    if not filters:
        raise MotionUnavailableError(f"Overlay '{overlay.template}' sans contenu à dessiner.")
    vf = "format=yuva444p," + ",".join(filters) + ",format=yuva444p10le"
    cmd = [
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"color=c=black@0.0:s={fmt.width}x{fmt.height}:r={fmt.fps}:d={overlay.duration:.3f}",
        "-vf",
        vf,
        "-c:v",
        "prores_ks",
        "-profile:v",
        "4444",
        "-pix_fmt",
        "yuva444p10le",
        str(out),
    ]
    try:
        proc = _run(cmd, None)
    except FileNotFoundError as e:
        raise MotionUnavailableError(
            "Ni Node/Remotion ni FFmpeg ne sont disponibles : installe Node (npm install dans "
            "motion/) ou FFmpeg."
        ) from e
    if proc.returncode != 0:
        out.unlink(missing_ok=True)
        raise FFmpegError(f"drawtext a échoué : {' '.join(cmd)}\n{proc.stderr.strip()[-600:]}")


# --- API publique ------------------------------------------------------------------------------


def render_overlays(
    timeline: Timeline,
    fmt: OutputFormat,
    theme: dict[str, str],
    out_dir: Path,
    strict: bool = False,
) -> dict[int, Path]:
    """Rend chaque `timeline.overlays[i]` en `.mov` ProRes 4444 alpha ; renvoie {i: chemin}.

    Cache : le nom du fichier dépend du hash des props (un rendu existant est réutilisé).
    Sans `npx`/`node_modules` (ou si Remotion échoue) : repli FFmpeg `drawtext` pour `title`,
    `captions`, `kill_counter` et `rank_card` ; pour les autres templates (dont `rewind`),
    `MotionUnavailableError` si `strict`, sinon l'overlay est ignoré avec un warning.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    use_remotion = remotion_available()
    if not use_remotion:
        log.warning(
            "Remotion indisponible (npx ou %s absent) : repli FFmpeg drawtext pour "
            "title/captions. Lance `npm install` dans motion/.",
            motion_dir() / "node_modules",
        )

    results: dict[int, Path] = {}
    for i, overlay in enumerate(timeline.overlays):
        template = overlay.template
        if template not in TEMPLATES:
            msg = f"Template motion inconnu '{template}'. Disponibles : {', '.join(TEMPLATES)}."
            if strict:
                raise MotionUnavailableError(msg)
            log.warning("%s Overlay %d ignoré.", msg, i)
            continue
        props = build_props(overlay, fmt, theme, _relative_beats(timeline, overlay))
        out = out_dir / f"{template}_{props_hash(template, props)}.mov"
        if out.exists() and out.stat().st_size > 0:
            results[i] = out
            continue

        try:
            if use_remotion:
                try:
                    _render_remotion(template, props, out, out_dir)
                except (MotionUnavailableError, subprocess.TimeoutExpired, OSError) as e:
                    if template not in FALLBACK_TEMPLATES:
                        raise
                    log.warning("Remotion a échoué (%s) : repli FFmpeg pour %s.", e, template)
                    _render_ffmpeg_fallback(overlay, props, fmt, out)
            elif template in FALLBACK_TEMPLATES:
                _render_ffmpeg_fallback(overlay, props, fmt, out)
            else:
                raise MotionUnavailableError(
                    f"Le template '{template}' exige Remotion (Node + `npm install` dans "
                    f"{motion_dir()}) ; seuls {', '.join(FALLBACK_TEMPLATES)} ont un repli FFmpeg."
                )
        except MotionUnavailableError as e:
            if strict:
                raise
            log.warning("Overlay %d (%s) ignoré : %s", i, template, e)
            continue
        results[i] = out
    return results
