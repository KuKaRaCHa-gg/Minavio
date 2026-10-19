"""Planification de la timeline : fonction pure (pas de média, pas d'I/O)."""

from __future__ import annotations

import bisect
import logging
from pathlib import Path

from minavio.config.schema import ChannelProfile
from minavio.core.models import Clip, Highlight, Timeline, TranscriptSegment, Word

log = logging.getLogger("minavio.edit")

# Tolérance (s) pour aligner une coupe sur le beat le plus proche, selon le pacing.
BEAT_TOLERANCE_S = {"calm": 0.25, "dynamic": 0.2, "hyper": 0.15}
_MIN_CAPTION_S = 0.05
MIN_FRAGMENT_S = 0.25  # sous-clips plus courts que ça (après autocut) : ignorés

KeepSpans = dict[Path, list[tuple[float, float]]]


class _Window:
    def __init__(self, source: Path, start: float, end: float, score: float, label: str):
        self.source, self.start, self.end, self.score, self.label = source, start, end, score, label

    @property
    def duration(self) -> float:
        return self.end - self.start


def _fit(w: _Window, lo: float, hi: float, center: float) -> None:
    """Borne la durée de w entre lo et hi (recentré sur `center` si trop long)."""
    if w.duration > hi:
        w.start = max(0.0, center - hi / 2)
        w.end = w.start + hi
    elif w.duration < lo:
        w.end = w.start + lo


def _windows(highlights: list[Highlight], profile: ChannelProfile) -> list[_Window]:
    ed = profile.editing
    raw = [
        _Window(
            h.source,
            max(0.0, h.start - ed.pre_roll_s),
            h.end + ed.post_roll_s,
            h.score,
            h.label,
        )
        for h in highlights
    ]
    merged: list[_Window] = []
    for w in sorted(raw, key=lambda x: (str(x.source), x.start)):
        last = merged[-1] if merged else None
        if last and last.source == w.source and w.start < last.end:
            if w.score > last.score:
                last.label = w.label
            last.end = max(last.end, w.end)
            last.score = max(last.score, w.score)
        else:
            merged.append(w)
    for w in merged:
        _fit(w, ed.min_clip_s, ed.max_clip_s, (w.start + w.end) / 2)
    return merged


def _fragments(w: _Window, keep_spans: KeepSpans | None) -> list[tuple[float, float]]:
    """Sous-intervalles de la fenêtre à garder (fragments < MIN_FRAGMENT_S ignorés)."""
    spans = keep_spans.get(w.source) if keep_spans else None
    if spans is None:
        return [(w.start, w.end)]
    out = []
    for s, e in sorted(spans):
        s, e = max(s, w.start), min(e, w.end)
        if e - s >= MIN_FRAGMENT_S:
            out.append((s, e))
    return out


def _effective(w: _Window, keep_spans: KeepSpans | None) -> float:
    return sum(e - s for s, e in _fragments(w, keep_spans))


def _select(
    windows: list[_Window], target: float, keep_spans: KeepSpans | None = None
) -> list[_Window]:
    if keep_spans:  # une fenêtre entièrement coupée disparaît
        windows = [w for w in windows if _fragments(w, keep_spans)]
    chosen: list[_Window] = []
    total = 0.0
    for w in sorted(windows, key=lambda x: -x.score):
        dur = _effective(w, keep_spans)
        if total + dur <= target:
            chosen.append(w)
            total += dur
        elif not chosen:  # au moins un clip, tronqué à la durée cible
            w.end = w.start + target
            chosen.append(w)
            break
    return sorted(chosen, key=lambda x: (str(x.source), x.start))


def _snap(
    windows: list[_Window],
    beats: list[float],
    profile: ChannelProfile,
    keep_spans: KeepSpans | None = None,
) -> None:
    """Aligne chaque coupe de fin sur le beat le plus proche (dans la tolérance du pacing)."""
    ed = profile.editing
    tol = BEAT_TOLERANCE_S[ed.pacing]
    sorted_beats = sorted(beats)
    t = 0.0
    for w in windows:
        eff = _effective(w, keep_spans)
        cut = t + eff
        i = bisect.bisect_left(sorted_beats, cut)
        cands = sorted_beats[max(0, i - 1) : i + 1]
        if cands:
            beat = min(cands, key=lambda b: abs(b - cut))
            new_dur = w.duration + (beat - cut)
            if abs(beat - cut) <= tol and ed.min_clip_s <= new_dur <= ed.max_clip_s:
                old_end = w.end
                w.end = w.start + new_dur
                # avec autocut, la fin déplacée peut tomber dans un creux : on vérifie le résultat
                if keep_spans and abs(t + _effective(w, keep_spans) - beat) > 1e-6:
                    w.end = old_end
        t += _effective(w, keep_spans)


def _remap_captions(
    clips: list[Clip], transcript: list[TranscriptSegment]
) -> list[TranscriptSegment]:
    out: list[TranscriptSegment] = []
    offset = 0.0
    for clip in clips:
        for seg in transcript:
            if seg.end <= clip.start or seg.start >= clip.end:
                continue
            if seg.source is not None and seg.source != clip.source:
                continue

            def to_tl(t: float, clip: Clip = clip, offset: float = offset) -> float:
                return offset + (min(max(t, clip.start), clip.end) - clip.start) / clip.speed

            words: list[Word] = []
            for w in seg.words:
                if w.end <= clip.start or w.start >= clip.end:
                    continue
                s, e = to_tl(w.start), to_tl(w.end)
                if e - s > 0:
                    words.append(w.model_copy(update={"start": s, "end": e}))
            start, end = to_tl(seg.start), to_tl(seg.end)
            if seg.words:
                if not words:
                    continue
                start, end = words[0].start, words[-1].end
                text = " ".join(w.text for w in words)
            else:
                text = seg.text
            if end - start >= _MIN_CAPTION_S:
                out.append(
                    seg.model_copy(
                        update={"start": start, "end": end, "text": text, "words": words}
                    )
                )
        offset += clip.duration / clip.speed
    return out


def plan_timeline(
    highlights: list[Highlight],
    profile: ChannelProfile,
    beats: list[float] | None = None,
    music: Path | None = None,
    transcript: list[TranscriptSegment] | None = None,
    keep_spans: KeepSpans | None = None,
) -> Timeline:
    """Highlights -> Timeline (pre/post roll, bornes, durée cible, snap beats, captions).

    `keep_spans` (autocut) : chaque fenêtre est éclatée en sous-clips sur les intervalles gardés ;
    seuls les bords externes sont snappés sur les beats.
    """
    windows = _select(_windows(highlights, profile), profile.editing.target_duration_s, keep_spans)
    if beats and profile.audio.beat_sync:
        _snap(windows, beats, profile, keep_spans)

    punch = profile.editing.autocut.punch_in
    clips: list[Clip] = []
    for w in windows:
        for k, (s, e) in enumerate(_fragments(w, keep_spans)):
            zoom = punch if punch > 1 and k % 2 == 1 else 1.0
            clips.append(Clip(source=w.source, start=s, end=e, highlight_label=w.label, zoom=zoom))
    timeline = Timeline(
        profile_id=profile.id,
        fps=profile.outputs[0].fps,
        clips=clips,
        music=music,
        beats=[b for b in sorted(beats or []) if 0 <= b <= sum(c.duration for c in clips)],
    )
    if transcript:
        if len({c.source for c in clips}) > 1 and any(s.source is None for s in transcript):
            log.warning(
                "Transcript sans champ 'source' et plusieurs sources : captions appliquées "
                "à tous les clips par temps source (risque d'incohérence)."
            )
        timeline.captions = _remap_captions(clips, transcript)
    return timeline
