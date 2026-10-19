"""Détecteur de pics de volume (RMS court terme au-dessus de la médiane)."""

from __future__ import annotations

from pathlib import Path

from minavio.config.schema import ChannelProfile
from minavio.core import ffmpeg
from minavio.core.detect.registry import DetectorUnavailable, register_detector
from minavio.core.models import Highlight

SAMPLE_RATE = 16000


@register_detector("audio_peak")
def audio_peak(sources: list[Path], params: dict, profile: ChannelProfile) -> list[Highlight]:
    try:
        import numpy as np
    except ImportError as e:
        raise DetectorUnavailable("numpy absent (extra 'vision')") from e

    min_db = float(params.get("min_db_above_median", 12))
    frame_s = float(params.get("frame_s", 0.1))
    half = float(params.get("window_s", 1.5))
    out: list[Highlight] = []
    for src in sources:
        pcm = ffmpeg.decode_pcm(src, SAMPLE_RATE)
        n = int(SAMPLE_RATE * frame_s)
        frames = len(pcm) // n
        if frames < 3:
            continue
        x = pcm[: frames * n].reshape(frames, n)
        rms = np.sqrt((x**2).mean(axis=1)) + 1e-9
        db = 20 * np.log10(rms)
        excess = db - float(np.median(db))
        hot = excess >= min_db
        duration = len(pcm) / SAMPLE_RATE
        i = 0
        while i < frames:
            if not hot[i]:
                i += 1
                continue
            j = i
            while j + 1 < frames and hot[j + 1]:
                j += 1
            peak = float(excess[i : j + 1].max())
            t0, t1 = i * frame_s, (j + 1) * frame_s
            out.append(
                Highlight(
                    source=src,
                    start=max(0.0, t0 - half),
                    end=min(duration, t1 + half),
                    score=float(min(1.0, peak / (2 * min_db))),
                    label="loud_moment",
                    detector="audio_peak",
                    reason=f"pic de {peak:.0f} dB au-dessus de la médiane à {t0:.1f}s",
                )
            )
            i = j + 1
    return out
