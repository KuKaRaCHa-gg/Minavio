"""Transcription locale (faster-whisper) avec timestamps mot-à-mot."""

from __future__ import annotations

import os
from pathlib import Path

from minavio.core.models import TranscriptSegment, Word


def _cuda_available() -> bool:
    try:
        import ctranslate2

        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


def _word(w) -> Word | None:
    start, end = float(w.start), float(w.end)
    if end <= start:
        end = start + 0.01
    text = (w.word or "").strip()
    if not text:
        return None
    prob = getattr(w, "probability", None)
    return Word(
        start=max(0.0, start),
        end=max(end, 0.011),
        text=text,
        confidence=None if prob is None else min(1.0, max(0.0, float(prob))),
    )


def transcribe(source: Path, language: str | None = None) -> list[TranscriptSegment]:
    from faster_whisper import WhisperModel

    cuda = _cuda_available()
    device = "cuda" if cuda else "cpu"
    size = os.environ.get("MINAVIO_WHISPER_MODEL", "large-v3" if cuda else "small")
    model = WhisperModel(size, device=device, compute_type="float16" if cuda else "int8")
    segments, _info = model.transcribe(str(source), language=language, word_timestamps=True)
    out: list[TranscriptSegment] = []
    for seg in segments:
        start, end = float(seg.start), float(seg.end)
        if end <= start:
            continue
        words = [w for w in (_word(x) for x in (seg.words or [])) if w is not None]
        out.append(
            TranscriptSegment(
                start=max(0.0, start), end=end, text=(seg.text or "").strip(), words=words
            )
        )
    return out
