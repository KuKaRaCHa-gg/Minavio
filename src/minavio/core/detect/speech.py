"""Détecteur de parole (Silero VAD) : réactions vocales, commentaires."""

from __future__ import annotations

from pathlib import Path

from minavio.config.schema import ChannelProfile
from minavio.core.detect.registry import DetectorUnavailable, register_detector
from minavio.core.models import Highlight


@register_detector("speech")
def speech(sources: list[Path], params: dict, profile: ChannelProfile) -> list[Highlight]:
    vad = profile.audio.vad
    if not vad.enabled:
        return []
    try:
        import silero_vad  # noqa: F401  (échoue tôt si l'extra 'audio' manque)

        from minavio.core.audio.vad import detect_speech
    except ImportError as e:
        raise DetectorUnavailable(f"silero-vad indisponible ({e}) : extra 'audio'") from e

    threshold = float(params.get("threshold", vad.threshold))
    min_ms = int(params.get("min_speech_ms", vad.min_speech_ms))
    out: list[Highlight] = []
    for src in sources:
        for start, end in detect_speech(src, threshold, min_ms):
            dur = end - start
            out.append(
                Highlight(
                    source=src,
                    start=start,
                    end=end,
                    score=min(1.0, 0.3 + dur / 8),
                    label="speech",
                    detector="speech",
                    reason=f"parole {dur:.1f}s",
                )
            )
    return out
