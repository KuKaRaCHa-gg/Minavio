"""Wrapper fin autour de ffmpeg/ffprobe (sous-processus, listes d'arguments, jamais de shell)."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from minavio.errors import FFmpegError

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov"}
_STDERR_LIMIT = 2000


@dataclass(frozen=True)
class MediaInfo:
    duration: float
    width: int
    height: int
    fps: float
    has_audio: bool


def _truncate(text: str, limit: int = _STDERR_LIMIT) -> str:
    text = text.strip()
    return text if len(text) <= limit else "[...]" + text[-limit:]


def _exec(cmd: list[str]) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(cmd, capture_output=True, check=False)
    except FileNotFoundError as e:
        raise FFmpegError(
            f"'{cmd[0]}' est introuvable : installez FFmpeg et ajoutez-le au PATH."
        ) from e
    if proc.returncode != 0:
        err = _truncate(proc.stderr.decode("utf-8", errors="replace"))
        raise FFmpegError(f"Commande échouée (code {proc.returncode}) : {' '.join(cmd)}\n{err}")
    return proc


def _clean(args: list[str]) -> list[str]:
    return args[1:] if args and Path(args[0]).name in {"ffmpeg", "ffmpeg.exe"} else list(args)


def run(args: list[str]) -> None:
    """Exécute `ffmpeg -y -hide_banner -loglevel error <args>` (args sans le binaire)."""
    _exec(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *_clean(args)])


def run_capture(args: list[str], *, stream: str = "stdout") -> bytes:
    """Comme `run` mais renvoie stdout (ou stderr) brut ; utile pour pipes PCM et filtres texte."""
    proc = _exec(["ffmpeg", "-y", "-hide_banner", "-nostats", *_clean(args)])
    return proc.stdout if stream == "stdout" else proc.stderr


def _parse_rate(value: str | None) -> float:
    if not value or value == "0/0":
        return 0.0
    num, _, den = value.partition("/")
    try:
        return float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        return 0.0


def probe(path: Path) -> MediaInfo:
    path = Path(path)
    if not path.exists():
        raise FFmpegError(f"Fichier média introuvable : {path}")
    proc = _exec(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_streams",
            "-show_format",
            str(path),
        ]
    )
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise FFmpegError(f"Sortie ffprobe illisible pour {path}") from e
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise FFmpegError(f"Aucun flux vidéo dans {path}")
    duration = float(data.get("format", {}).get("duration") or video.get("duration") or 0)
    fps = _parse_rate(video.get("avg_frame_rate")) or _parse_rate(video.get("r_frame_rate"))
    return MediaInfo(
        duration=duration,
        width=int(video["width"]),
        height=int(video["height"]),
        fps=fps,
        has_audio=any(s.get("codec_type") == "audio" for s in streams),
    )


def collect_sources(inputs: list[Path]) -> list[Path]:
    """Fichiers et dossiers (récursif) -> vidéos mp4/mkv/mov triées, sans doublon."""
    found: dict[Path, None] = {}
    for item in inputs:
        item = Path(item)
        if item.is_dir():
            for p in item.rglob("*"):
                if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS:
                    found[p] = None
        elif item.is_file():
            if item.suffix.lower() in VIDEO_EXTENSIONS:
                found[item] = None
        else:
            raise FFmpegError(f"Source introuvable : {item}")
    return sorted(found, key=lambda p: str(p))


def decode_pcm(path: Path, sample_rate: int = 16000):
    """Décode l'audio en mono float32 (numpy) ; tableau vide si pas de piste audio."""
    import numpy as np

    if not probe(path).has_audio:
        return np.zeros(0, dtype=np.float32)
    raw = run_capture(
        [
            "-v",
            "error",
            "-i",
            str(path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            "-f",
            "f32le",
            "-",
        ]
    )
    return np.frombuffer(raw, dtype=np.float32).copy()
