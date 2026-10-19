"""Autocut : intervalles à garder (on ne coupe que ce qui n'est ni voix ni action)."""

from __future__ import annotations

import logging
from pathlib import Path

from minavio.config.schema import AutocutSettings
from minavio.core.audio.vad import detect_speech
from minavio.core.ffmpeg import decode_pcm, probe
from minavio.core.models import TranscriptSegment

log = logging.getLogger("minavio.autocut")

SAMPLE_RATE = 16000
FRAME_S = 0.05
_EPS = 1e-9


def _frame_energy_db(pcm) -> list[float]:
    import numpy as np

    n = SAMPLE_RATE * 5 // 100  # 800 échantillons = 50 ms
    count = -(-pcm.size // n)
    padded = np.zeros(count * n, dtype=np.float64)
    padded[: pcm.size] = pcm
    rms = np.sqrt(np.mean(padded.reshape(count, n) ** 2, axis=1))
    return (20 * np.log10(rms + _EPS)).tolist()


def _voice_frames(source: Path, count: int) -> list[bool]:
    voice = [False] * count
    try:
        segments = detect_speech(source)
    except Exception as exc:  # dépendance absente (torch/silero) ou modèle indisponible
        log.warning("VAD indisponible (%s) : autocut sur l'énergie seule.", exc)
        return voice
    for s, e in segments:
        for i in range(max(0, int(s / FRAME_S)), min(count, int(e / FRAME_S) + 1)):
            voice[i] = True
    return voice


def _words(source: Path, transcript: list[TranscriptSegment] | None) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for seg in transcript or []:
        if seg.source is None or seg.source == source:
            out.extend((w.start, w.end) for w in seg.words)
    return sorted(out)


def _protect_words(
    cuts: list[tuple[float, float]], words: list[tuple[float, float]]
) -> list[tuple[float, float]]:
    """Réduit chaque coupe pour qu'aucun de ses bords ne tombe à l'intérieur d'un mot."""
    out = []
    for s, e in cuts:
        for ws, we in words:
            if ws < s < we:
                s = we
            if ws < e < we:
                e = ws
        if e - s > 1e-6:
            out.append((s, e))
    return out


def analyze_keep_spans(
    source: Path,
    settings: AutocutSettings,
    transcript: list[TranscriptSegment] | None = None,
) -> list[tuple[float, float]]:
    """Intervalles à GARDER (temps source, triés, disjoints) ; les creux longs sont exclus."""
    import numpy as np

    duration = probe(source).duration
    pcm = decode_pcm(source, SAMPLE_RATE)
    if pcm.size == 0:
        return [(0.0, duration)] if duration > 0 else []
    duration = max(duration, pcm.size / SAMPLE_RATE)

    energy = _frame_energy_db(pcm)
    count = len(energy)
    threshold = float(np.percentile(energy, settings.energy_percentile)) + settings.energy_margin_db
    voice = _voice_frames(source, count)
    active = [v or e > threshold for v, e in zip(voice, energy, strict=True)]

    cuts: list[tuple[float, float]] = []
    i = 0
    while i < count:
        if active[i]:
            i += 1
            continue
        j = i
        while j < count and not active[j]:
            j += 1
        gap_s, gap_e = i * FRAME_S, min(j * FRAME_S, duration)
        if gap_e - gap_s >= settings.min_silence_s:
            cut_s = gap_s + (settings.margin_after_s if i > 0 else 0.0)
            cut_e = gap_e - (settings.margin_before_s if j < count else 0.0)
            if cut_e > cut_s:
                cuts.append((cut_s, cut_e))
        i = j

    cuts = _protect_words(cuts, _words(source, transcript))

    keep: list[tuple[float, float]] = []
    pos = 0.0
    for s, e in cuts:
        if s > pos:
            keep.append((pos, s))
        pos = max(pos, e)
    if duration > pos:
        keep.append((pos, duration))
    return keep
