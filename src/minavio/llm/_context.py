"""Utilitaires partagés : extraction de contexte transcript et estimation de tokens."""

from __future__ import annotations

from minavio.core.models import TranscriptSegment

CONTEXT_PAD_S = 5.0


def estimate_tokens(text: str) -> int:
    return len(text) // 4 + 1


def transcript_context(
    transcript: list[TranscriptSegment], start: float, end: float, max_chars: int | None = None
) -> str:
    """Texte des segments qui chevauchent [start, end], une ligne `[t] texte` par segment."""
    lines = [
        f"[{seg.start:.1f}] {seg.text.strip()}"
        for seg in transcript
        if seg.end > start and seg.start < end and seg.text.strip()
    ]
    text = "\n".join(lines)
    if max_chars is not None and len(text) > max_chars:
        text = text[:max_chars].rstrip() + " [...]"
    return text
