"""Détecteur d'intensité visuelle : l'action se voit sans aucun template ni capture.

Par fenêtre de `window_s` (0,5 s) sur frames réduites (320 px, `analysis_fps` = 10 img/s) :
- `flow`  : magnitude moyenne du flux optique (Farneback) ;
- `shake` : secousses de caméra = variance du déplacement global (corrélation de phase) ;
- `flash` : pic bref de luminance au-dessus de sa médiane glissante de `baseline_s` (tirs...) ;
- `red`   : hausse brutale de la dominante rouge, même référence (indicateur de dégâts).
Chaque série est convertie en z-score robuste (médiane/MAD, avec plancher absolu pour qu'un clip
calme n'amplifie pas le bruit) puis fusionnée en « noisy-OR » ; les pics donnent des Highlight
`label="action"` dont `signals` = z de chaque composante + `fused` + `peak_t` (temps source).
Params : `window_s` (0.5), `analysis_fps` (10), `width` (320), `threshold` (0.5), `z_full` (4),
`smooth_s` (0), `baseline_s` (3), `pad_s` (1.0), `min_len_s` (2.0),
`weights` ({flow, shake, flash, red}).
Dépendances : OpenCV + numpy (extra `vision`).
"""

from __future__ import annotations

import logging
from pathlib import Path

from minavio.config.schema import ChannelProfile
from minavio.core import ffmpeg
from minavio.core.detect.killfeed_ocr import _frames
from minavio.core.detect.registry import DetectorUnavailable, register_detector
from minavio.core.models import Highlight

log = logging.getLogger("minavio.detect.motion")

DEFAULT_WEIGHTS = {"flow": 0.6, "shake": 0.5, "flash": 0.6, "red": 0.4}
# Planchers de l'échelle (unités natives) : en dessous, une variation n'est que du bruit vidéo.
_SCALE_FLOOR = {"flow": 0.08, "shake": 0.5, "flash": 2.0, "red": 1.5}


def robust_z(x, floor: float):
    """z-score robuste (médiane/MAD), échelle plancher `floor` ; 0 pour une série vide."""
    import numpy as np

    x = np.asarray(x, dtype=float)
    if x.size == 0:
        return x
    med = float(np.median(x))
    mad = 1.4826 * float(np.median(np.abs(x - med)))
    return (x - med) / max(mad, floor)


def _rolling_excess(series, win: int):
    """Excès positif d'une série sur sa médiane glissante centrée (pics brefs)."""
    import numpy as np

    s = np.asarray(series, dtype=float)
    out = np.zeros_like(s)
    half = max(1, win // 2)
    for i in range(len(s)):
        lo, hi = max(0, i - half), min(len(s), i + half + 1)
        out[i] = max(0.0, s[i] - float(np.median(s[lo:hi])))
    return out


def window_features(src: Path, info: ffmpeg.MediaInfo, params: dict):
    """Séries par fenêtre : dict nom -> ndarray (flow, shake, flash, red)."""
    import cv2
    import numpy as np

    window_s = float(params.get("window_s", 0.5))
    fps = float(params.get("analysis_fps", 10))
    width = int(params.get("width", 320))
    n_win = max(1, int(np.ceil(max(info.duration, 1.0 / fps) / window_s)))

    flows: list[list[float]] = [[] for _ in range(n_win)]
    shifts: list[list[tuple[float, float]]] = [[] for _ in range(n_win)]
    lum, red, widx = [], [], []
    prev = hann = None
    for t, frame in _frames(src, info, fps, None, width):
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        if hann is None:
            hann = cv2.createHanningWindow((gray.shape[1], gray.shape[0]), cv2.CV_32F)
        w = min(n_win - 1, int(t / window_s))
        f = frame.astype(np.float32)
        lum.append(float(gray.mean()))
        red.append(float((f[..., 2] - (f[..., 0] + f[..., 1]) / 2).mean()))
        widx.append(w)
        if prev is not None:
            flow = cv2.calcOpticalFlowFarneback(prev, gray, None, 0.5, 3, 15, 3, 5, 1.2, 0)
            flows[w].append(float(np.linalg.norm(flow, axis=2).mean()))
            (dx, dy), _ = cv2.phaseCorrelate(prev.astype(np.float32), gray.astype(np.float32), hann)
            shifts[w].append((float(dx), float(dy)))
        prev = gray
    if not lum:
        raise DetectorUnavailable(f"aucune frame décodée dans {src}")

    per_s = max(3, round(fps * float(params.get("baseline_s", 3.0))))  # référence glissante
    flash_f = _rolling_excess(lum, per_s)
    red_f = _rolling_excess(red, per_s)
    series = {k: np.zeros(n_win) for k in DEFAULT_WEIGHTS}
    for i in range(n_win):
        if flows[i]:
            series["flow"][i] = np.mean(flows[i])
        if len(shifts[i]) >= 2:
            arr = np.asarray(shifts[i])
            series["shake"][i] = float(arr[:, 0].var() + arr[:, 1].var())
    widx_a = np.asarray(widx)
    for i in range(n_win):
        m = widx_a == i
        if m.any():
            series["flash"][i] = flash_f[m].max()
            series["red"][i] = red_f[m].max()
    return series


@register_detector("motion_intensity")
def motion_intensity(sources: list[Path], params: dict, profile: ChannelProfile) -> list[Highlight]:
    try:
        import cv2  # noqa: F401
        import numpy as np
    except ImportError as e:
        raise DetectorUnavailable("OpenCV/numpy absents (extra 'vision')") from e

    window_s = float(params.get("window_s", 0.5))
    threshold = float(params.get("threshold", 0.5))
    z_full = float(params.get("z_full", 4.0))
    smooth_s = float(params.get("smooth_s", 0.0))
    pad_s = float(params.get("pad_s", 1.0))
    min_len_s = float(params.get("min_len_s", 2.0))
    weights = {**DEFAULT_WEIGHTS, **{k: float(v) for k, v in params.get("weights", {}).items()}}
    names = [k for k in DEFAULT_WEIGHTS if weights.get(k, 0) > 0]
    if not names:
        return []

    out: list[Highlight] = []
    for src in sources:
        info = ffmpeg.probe(src)
        series = window_features(src, info, params)
        n = len(next(iter(series.values())))
        z = {k: robust_z(series[k], _SCALE_FLOOR[k]) for k in names}
        contrib = np.stack(
            [np.clip(weights[k] * np.clip(z[k], 0, None) / z_full, 0, 1) for k in names]
        )
        k_s = max(1, round(smooth_s / window_s))
        if k_s > 1:
            kern = np.ones(k_s) / k_s
            contrib = np.stack([np.convolve(c, kern, mode="same") for c in contrib])
        fused = 1 - np.prod(1 - contrib, axis=0)

        lo = threshold * 0.7
        duration = info.duration
        i = 0
        while i < n:
            if fused[i] < lo:
                i += 1
                continue
            j = i
            while j + 1 < n and fused[j + 1] >= lo:
                j += 1
            peak_i = i + int(np.argmax(fused[i : j + 1]))
            peak = float(fused[peak_i])
            if peak >= threshold:
                dom = names[int(np.argmax(contrib[:, peak_i]))]
                start = max(0.0, i * window_s - pad_s)
                end = min(duration, (j + 1) * window_s + pad_s)
                if end - start < min_len_s:
                    mid = (start + end) / 2
                    start = max(0.0, mid - min_len_s / 2)
                    end = min(duration, start + min_len_s)
                if end > start:
                    signals = {f"{k}_z": round(float(z[k][peak_i]), 3) for k in names}
                    signals["fused"] = round(peak, 3)
                    signals["peak_t"] = round((peak_i + 0.5) * window_s, 3)
                    out.append(
                        Highlight(
                            source=src,
                            start=start,
                            end=end,
                            score=min(1.0, peak),
                            label="action",
                            detector="motion_intensity",
                            reason=f"action visuelle ({dom}) à {peak_i * window_s:.1f}s",
                            signals=signals,
                        )
                    )
            i = j + 1
    return out
