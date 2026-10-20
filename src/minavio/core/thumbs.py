"""Extraction et notation des images candidates pour les miniatures.

Pour les meilleurs highlights, on échantillonne des frames (~4 fps) autour de l'instant décisif,
on les note (netteté, couleurs, visage, proximité du pic…), on dédoublonne par hash perceptuel
puis on renvoie les `n` meilleures sous forme de `ThumbnailCandidate` (PNG pleine résolution).
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from minavio.core.models import Highlight, ThumbnailCandidate

log = logging.getLogger(__name__)

# --- Paramètres d'échantillonnage -------------------------------------------------------------
SAMPLE_FPS = 4.0
WINDOW_BEFORE = 1.5  # s avant l'instant décisif
WINDOW_AFTER = 1.0  # s après
MAX_HIGHLIGHTS = 6
MAX_PER_HIGHLIGHT = 3  # variété : au plus 3 candidats retenus par highlight
HAMMING_DUP = 6  # dHash : distance <= 6 sur 64 bits => doublon
ANALYSIS_WIDTH = 640  # largeur de travail pour les mesures (pas pour le PNG écrit)

# --- Poids de la note (somme = 1) -------------------------------------------------------------
WEIGHTS = {
    "sharpness": 0.20,  # variance du Laplacien normalisée (percentile 90 de l'ensemble)
    "calm": 0.10,  # absence de flou de mouvement (faible écart avec la frame précédente)
    "vivid": 0.15,  # contraste + saturation : la miniature doit « claquer »
    "face": 0.25,  # présence et taille d'un visage (YuNet)
    "peak": 0.15,  # proximité de l'instant décisif
    "highlight": 0.15,  # score du highlight d'origine
}
FACE_MIN_HEIGHT = 0.08  # visage >= 8 % de la hauteur => bonus fort
FACE_FULL_HEIGHT = 0.30  # taille à laquelle le bonus visage est maximal
SHARP_PERCENTILE = 90
SHARP_FLOOR = 20.0  # évite de gonfler le bruit quand tout l'ensemble est flou
MOTION_FULL = 30.0  # écart moyen (0..255) jugé « flou de mouvement total »
DARK_LUMA, BRIGHT_LUMA = 18.0, 238.0  # frames quasi noires / blanches (fondus, flashs)
BLOWN_FRACTION = 0.35  # part de pixels brûlés en blanc au-delà de laquelle on pénalise
MIN_ENTROPY = 2.0  # bits ; en dessous : image d'interface / aplat => pénalité croissante


@dataclass
class _Frame:
    path: Path
    t: float
    hl: int  # index du highlight
    sharp_raw: float = 0.0
    calm: float = 1.0
    vivid: float = 0.0
    face: float = 0.0
    face_h: float = 0.0
    exposure: float = 1.0
    entropy: float = 0.0
    entropy_factor: float = 1.0
    dhash: int = 0
    score: float = 0.0
    signals: dict[str, float] = field(default_factory=dict)


def _dhash(gray) -> int:
    import cv2

    small = cv2.resize(gray, (9, 8), interpolation=cv2.INTER_AREA)
    bits = (small[:, 1:] > small[:, :-1]).flatten()
    return int("".join("1" if b else "0" for b in bits), 2)


def _hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def _measure(frame, f: _Frame, prev_gray, faces):
    """Remplit les mesures brutes de `f` ; renvoie la version grise réduite (pour la suivante)."""
    import cv2
    import numpy as np

    h, w = frame.shape[:2]
    scale = ANALYSIS_WIDTH / w if w > ANALYSIS_WIDTH else 1.0
    small = cv2.resize(frame, (round(w * scale), round(h * scale))) if scale != 1.0 else frame
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)

    f.sharp_raw = float(cv2.Laplacian(gray, cv2.CV_64F).var())

    if prev_gray is not None and prev_gray.shape == gray.shape:
        diff = float(np.mean(cv2.absdiff(gray, prev_gray)))
        f.calm = 1.0 - min(1.0, diff / MOTION_FULL)

    contrast = min(1.0, float(gray.std()) / 64.0)
    sat = min(1.0, float(cv2.cvtColor(small, cv2.COLOR_BGR2HSV)[..., 1].mean()) / 160.0)
    f.vivid = 0.5 * contrast + 0.5 * sat

    luma = float(gray.mean())
    # Flash partiel : la moyenne reste « normale » mais une grande zone est brûlée en blanc.
    # (Le noir n'est pas compté : bandes noires d'un contenu 4:3, fonds de facecam…)
    blown = float(np.mean(gray > 245))
    if luma < DARK_LUMA or luma > BRIGHT_LUMA:
        f.exposure = 0.05
    elif blown > BLOWN_FRACTION:
        f.exposure = 0.15
    else:
        f.exposure = 1.0

    hist = np.bincount((gray // 8).ravel(), minlength=32).astype(np.float64)
    p = hist[hist > 0] / hist.sum()
    f.entropy = float(-(p * np.log2(p)).sum())
    f.entropy_factor = min(1.0, f.entropy / MIN_ENTROPY) ** 2

    f.dhash = _dhash(gray)

    if faces is not None:
        try:
            found = faces(frame)
        except Exception as e:  # détecteur capricieux : on continue sans visage
            log.debug("Détection de visage échouée sur %s : %s", f.path, e)
            found = []
        if found:
            best = max(found, key=lambda x: x[4])
            f.face_h = float(best[3])
            if f.face_h >= FACE_MIN_HEIGHT:
                f.face = 0.6 + 0.4 * min(1.0, f.face_h / FACE_FULL_HEIGHT)
            else:
                f.face = 0.3 * f.face_h / FACE_MIN_HEIGHT
    return gray


def _finalize(frames: list[_Frame], hls: list[Highlight], peaks: list[float]) -> None:
    import numpy as np

    if not frames:
        return
    ref = max(float(np.percentile([f.sharp_raw for f in frames], SHARP_PERCENTILE)), SHARP_FLOOR)
    for f in frames:
        comp = {
            "sharpness": min(1.0, f.sharp_raw / ref),
            "calm": f.calm,
            "vivid": f.vivid,
            "face": f.face,
            "peak": max(0.0, 1.0 - abs(f.t - peaks[f.hl]) / WINDOW_BEFORE),
            "highlight": hls[f.hl].score,
        }
        base = sum(WEIGHTS[k] * v for k, v in comp.items())
        f.score = float(min(1.0, max(0.0, base * f.exposure * f.entropy_factor)))
        f.signals = {
            **comp,
            "face_height": f.face_h,
            "entropy": f.entropy,
            "exposure_factor": f.exposure,
            "entropy_factor": f.entropy_factor,
        }


def _extract_window(src: Path, start: float, dur: float, out: Path) -> list[Path]:
    from minavio.core import ffmpeg

    out.mkdir(parents=True, exist_ok=True)
    ffmpeg.run(
        [
            *("-ss", f"{start:.3f}"),
            *("-t", f"{dur:.3f}"),
            *("-i", str(src)),
            *("-vf", f"fps={SAMPLE_FPS:g}"),
            *("-an", "-sn"),
            str(out / "f_%03d.png"),
        ]
    )
    return sorted(out.glob("f_*.png"))


def _face_fn():
    try:
        from minavio.core.reframe import _yunet_faces

        return _yunet_faces()
    except Exception as e:
        log.info("Détection de visage indisponible (%s) : note sans bonus visage", e)
        return None


def extract_candidates(
    highlights: list[Highlight],
    profile,
    out_dir: Path,
    n: int | None = None,
) -> list[ThumbnailCandidate]:
    """Extrait, note et dédoublonne les images candidates ; renvoie les `n` meilleures."""
    import cv2

    from minavio.errors import FFmpegError

    n = n if n is not None else profile.thumbnails.candidates
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    top = sorted(highlights, key=lambda h: h.score, reverse=True)[:MAX_HIGHLIGHTS]
    if not top or n <= 0:
        return []

    faces = _face_fn()
    frames: list[_Frame] = []
    peaks: list[float] = []
    tmp_root = Path(tempfile.mkdtemp(prefix=".thumbs_", dir=out_dir))
    try:
        for i, hl in enumerate(top):
            peak = float(hl.signals.get("peak_t", (hl.start + hl.end) / 2))
            peaks.append(peak)
            start = max(0.0, peak - WINDOW_BEFORE)
            try:
                paths = _extract_window(
                    hl.source, start, peak + WINDOW_AFTER - start, tmp_root / f"h{i}"
                )
            except FFmpegError as e:
                log.warning("Source illisible pour les miniatures (%s) : %s", hl.source, e)
                continue
            prev = None
            for k, p in enumerate(paths):
                img = cv2.imread(str(p))
                if img is None:
                    log.warning("Frame illisible ignorée : %s", p)
                    continue
                f = _Frame(path=p, t=round(start + k / SAMPLE_FPS, 3), hl=i)
                prev = _measure(img, f, prev, faces)
                frames.append(f)

        _finalize(frames, top, peaks)

        kept: list[_Frame] = []
        per_hl: dict[int, int] = {}
        for f in sorted(frames, key=lambda x: x.score, reverse=True):
            if len(kept) >= n:
                break
            if per_hl.get(f.hl, 0) >= MAX_PER_HIGHLIGHT:
                continue
            if any(_hamming(f.dhash, k.dhash) <= HAMMING_DUP for k in kept):
                continue
            kept.append(f)
            per_hl[f.hl] = per_hl.get(f.hl, 0) + 1

        result: list[ThumbnailCandidate] = []
        for f in kept:
            hl = top[f.hl]
            dest = out_dir / f"{hl.source.stem}_h{f.hl}_{round(f.t * 1000):07d}.png"
            shutil.move(str(f.path), dest)
            result.append(
                ThumbnailCandidate(
                    source=hl.source,
                    t=f.t,
                    image=dest,
                    score=round(f.score, 4),
                    label=hl.label,
                    signals={k: round(v, 4) for k, v in f.signals.items()},
                )
            )
        return result
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)
