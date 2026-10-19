"""Sous-titres mot-à-mot : transcript → overlays `captions` (pur Python)."""

from __future__ import annotations

from minavio.config.schema import CaptionStyle
from minavio.core.models import Overlay, TranscriptSegment

_MIN_DURATION_S = 0.05


def _segment_words(seg: TranscriptSegment) -> list[tuple[str, float, float]]:
    """Mots (texte, début, fin) en temps absolu ; répartit le texte si pas de timestamps."""
    if seg.words:
        return [(w.text.strip(), w.start, w.end) for w in seg.words if w.text.strip()]
    tokens = seg.text.split()
    if not tokens:
        return []
    step = seg.duration / len(tokens)
    return [(tok, seg.start + i * step, seg.start + (i + 1) * step) for i, tok in enumerate(tokens)]


def build_caption_overlays(captions: list[TranscriptSegment], style: CaptionStyle) -> list[Overlay]:
    """Un `Overlay(template="captions")` par ligne de `style.max_words_per_line` mots.

    Les temps de `captions` sont ceux de la timeline rendue ; `Overlay.start` reprend le début
    du premier mot de la ligne, et les temps de chaque mot dans `props["words"]` sont relatifs
    à l'overlay. Avec `word_by_word=False`, tous les mots de la ligne sont visibles d'emblée.
    """
    if not style.enabled:
        return []

    overlays: list[Overlay] = []
    n = style.max_words_per_line
    for seg in captions:
        words = _segment_words(seg)
        for i in range(0, len(words), n):
            line = words[i : i + n]
            start = line[0][1]
            end = max(line[-1][2], start + _MIN_DURATION_S)
            duration = end - start
            if style.word_by_word:
                rel = [
                    {
                        "text": text,
                        "start": round(max(0.0, s - start), 3),
                        "end": round(min(duration, max(e, s) - start), 3),
                    }
                    for text, s, e in line
                ]
            else:
                rel = [
                    {"text": text, "start": 0.0, "end": round(duration, 3)} for text, _, _ in line
                ]
            overlays.append(
                Overlay(
                    template="captions",
                    start=round(start, 3),
                    duration=round(duration, 3),
                    props={"words": rel, "position": style.position, "style": style.style},
                )
            )
    return overlays
