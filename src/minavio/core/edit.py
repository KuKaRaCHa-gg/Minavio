"""Planification de la timeline : fonction pure (pas de média, pas d'I/O)."""

from __future__ import annotations

import bisect
import logging
from pathlib import Path

from minavio.config.schema import ChannelProfile, SlowmoSettings
from minavio.core.models import Clip, Highlight, Timeline, TranscriptSegment, Word

log = logging.getLogger("minavio.edit")

# Tolérance (s) pour aligner une coupe sur le beat le plus proche, selon le pacing.
BEAT_TOLERANCE_S = {"calm": 0.25, "dynamic": 0.2, "hyper": 0.15}
_MIN_CAPTION_S = 0.05
# Clés de `Highlight.signals` = instant du pic (s source) ; sinon centre du highlight.
PEAK_SIGNAL_KEYS = ("peak_t", "peak_time", "peak_s", "t_peak")
MIN_FRAGMENT_S = 0.25  # sous-clips plus courts que ça (après autocut) : fusionnés au voisin

MIN_SLOW_EDGE_S = 0.1  # un [avant]/[après] plus court que ça est absorbé par le ralenti

KeepSpans = dict[Path, list[tuple[float, float]]]


class _Window:
    def __init__(
        self,
        source: Path,
        start: float,
        end: float,
        score: float,
        label: str,
        peak: float | None = None,
    ):
        self.source, self.start, self.end, self.score, self.label = source, start, end, score, label
        self.peak = (start + end) / 2 if peak is None else peak

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


def _peak(h: Highlight) -> float:
    """Instant du pic : signal temporel exploitable (dans le highlight), sinon son centre."""
    for key in PEAK_SIGNAL_KEYS:
        t = h.signals.get(key)
        if t is not None and h.start <= t <= h.end:
            return float(t)
    return (h.start + h.end) / 2


def _windows(highlights: list[Highlight], profile: ChannelProfile) -> list[_Window]:
    ed = profile.editing
    raw = [
        _Window(
            h.source,
            max(0.0, h.start - ed.pre_roll_s),
            h.end + ed.post_roll_s,
            h.score,
            h.label,
            _peak(h),
        )
        for h in highlights
    ]
    merged: list[_Window] = []
    for w in sorted(raw, key=lambda x: (str(x.source), x.start)):
        last = merged[-1] if merged else None
        if last and last.source == w.source and w.start < last.end:
            if w.score > last.score:
                last.label, last.peak = w.label, w.peak
            last.end = max(last.end, w.end)
            last.score = max(last.score, w.score)
        else:
            merged.append(w)
    for w in merged:
        _fit(w, ed.min_clip_s, ed.max_clip_s, (w.start + w.end) / 2)
    return merged


MIN_SOLO_FRAGMENT_S = 0.1  # un fragment seul dans sa fenêtre est gardé au-dessus de ce seuil


def _fragments(w: _Window, keep_spans: KeepSpans | None) -> list[tuple[float, float]]:
    """Sous-intervalles de la fenêtre à garder.

    Un fragment < MIN_FRAGMENT_S est fusionné avec son voisin le plus proche (le trou est comblé) ;
    seul dans sa fenêtre il est gardé, sauf s'il fait < MIN_SOLO_FRAGMENT_S.
    """
    spans = keep_spans.get(w.source) if keep_spans else None
    if spans is None:
        return [(w.start, w.end)]
    frags = []
    for s, e in sorted(spans):
        s, e = max(s, w.start), min(e, w.end)
        if e - s > 1e-9:
            frags.append((s, e))
    while len(frags) > 1:
        i = next((k for k, (s, e) in enumerate(frags) if e - s < MIN_FRAGMENT_S), None)
        if i is None:
            break
        left = frags[i][0] - frags[i - 1][1] if i > 0 else float("inf")
        right = frags[i + 1][0] - frags[i][1] if i + 1 < len(frags) else float("inf")
        j = i - 1 if left <= right else i + 1
        lo, hi = min(i, j), max(i, j)
        frags[lo : hi + 1] = [(frags[lo][0], frags[hi][1])]
    if len(frags) == 1 and frags[0][1] - frags[0][0] < MIN_SOLO_FRAGMENT_S - 1e-9:
        return []
    return frags


def _slow_range(
    w: _Window, frag: tuple[float, float], slow: SlowmoSettings | None
) -> tuple[float, float] | None:
    """Plage source ralentie dans `frag` : `duration_s` centrée sur le pic, bornée au fragment.

    None si le ralenti est inactif, si le label n'est pas ciblé ou si le pic n'est pas dans `frag`.
    """
    if slow is None or not slow.enabled or w.label not in slow.labels:
        return None
    s, e = frag
    if not s <= w.peak <= e:
        return None
    lo, hi = max(s, w.peak - slow.duration_s / 2), min(e, w.peak + slow.duration_s / 2)
    if lo - s < MIN_SLOW_EDGE_S:
        lo = s
    if e - hi < MIN_SLOW_EDGE_S:
        hi = e
    return (lo, hi) if hi - lo > 1e-9 else None


def _effective(
    w: _Window, keep_spans: KeepSpans | None, slow: SlowmoSettings | None = None
) -> float:
    """Durée RENDUE de la fenêtre (autocut et ralenti inclus)."""
    total = 0.0
    for f in _fragments(w, keep_spans):
        total += f[1] - f[0]
        if (r := _slow_range(w, f, slow)) is not None:
            total += (r[1] - r[0]) * (1 / slow.factor - 1)  # type: ignore[union-attr]
    return total


def _select(
    windows: list[_Window],
    target: float,
    keep_spans: KeepSpans | None = None,
    slow: SlowmoSettings | None = None,
) -> list[_Window]:
    if keep_spans:  # une fenêtre entièrement coupée disparaît
        windows = [w for w in windows if _fragments(w, keep_spans)]
    chosen: list[_Window] = []
    total = 0.0
    for w in sorted(windows, key=lambda x: -x.score):
        dur = _effective(w, keep_spans, slow)
        if total + dur <= target:
            chosen.append(w)
            total += dur
        elif not chosen:  # au moins un clip, tronqué à la durée cible
            w.end = w.start + target
            for _ in range(30):  # le ralenti allonge le rendu : on réduit jusqu'à tenir
                eff = _effective(w, keep_spans, slow)
                if eff <= target + 1e-6:
                    break
                w.end = w.start + max(w.duration * target / eff * 0.999, 0.1)
            chosen.append(w)
            break
    return sorted(chosen, key=lambda x: (str(x.source), x.start))


def _snap(
    windows: list[_Window],
    beats: list[float],
    profile: ChannelProfile,
    keep_spans: KeepSpans | None = None,
    t0: float = 0.0,
    slow: SlowmoSettings | None = None,
) -> None:
    """Aligne chaque coupe de fin sur le beat le plus proche (dans la tolérance du pacing).

    `t0` : durée déjà écoulée avant le premier clip (cold open), en temps timeline.
    """
    ed = profile.editing
    tol = BEAT_TOLERANCE_S[ed.pacing]
    sorted_beats = sorted(beats)
    t = t0
    for w in windows:
        eff = _effective(w, keep_spans, slow)
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
                if (keep_spans or slow) and abs(t + _effective(w, keep_spans, slow) - beat) > 1e-6:
                    w.end = old_end
        t += _effective(w, keep_spans, slow)


def _runs(clips: list[Clip]) -> list[list[int]]:
    """Groupes d'indices de clips consécutifs ET contigus dans la source (pièces d'un ralenti)."""
    runs: list[list[int]] = []
    for i, c in enumerate(clips):
        prev = clips[i - 1] if i else None
        if prev is not None and prev.source == c.source and abs(prev.end - c.start) < 1e-9:
            runs[-1].append(i)
        else:
            runs.append([i])
    return runs


def _remap_captions(
    clips: list[Clip], transcript: list[TranscriptSegment], start_offset: float = 0.0
) -> list[TranscriptSegment]:
    out: list[TranscriptSegment] = []
    offsets: list[float] = []
    offset = start_offset
    for c in clips:
        offsets.append(offset)
        offset += c.duration / c.speed
    # Un « run » (clips contigus dans la source, ex. avant/ralenti/après) se comporte comme un
    # seul clip dont le temps timeline est affine par morceaux.
    runs = _runs(clips)
    bounds = [(clips[r[0]].start, clips[r[-1]].end) for r in runs]
    # segment sans mots : émis uniquement sur le run de plus grand recouvrement
    best: dict[int, int] = {}
    for si, seg in enumerate(transcript):
        if seg.words:
            continue
        top = 0.0
        for ri, r in enumerate(runs):
            if seg.source is not None and seg.source != clips[r[0]].source:
                continue
            ov = min(seg.end, bounds[ri][1]) - max(seg.start, bounds[ri][0])
            if ov > top + 1e-9:
                top, best[si] = ov, ri
    for ri, r in enumerate(runs):
        lo, hi = bounds[ri]
        source = clips[r[0]].source

        def to_tl(t: float, r: list[int] = r, lo: float = lo, hi: float = hi) -> float:
            t = min(max(t, lo), hi)
            k = next((j for j in r if t <= clips[j].end + 1e-9), r[-1])
            c = clips[k]
            return offsets[k] + (min(max(t, c.start), c.end) - c.start) / c.speed

        for si, seg in enumerate(transcript):
            if seg.end <= lo or seg.start >= hi:
                continue
            if seg.source is not None and seg.source != source:
                continue
            if not seg.words and best.get(si) != ri:
                continue
            words: list[Word] = []
            for w in seg.words:
                if w.end <= lo or w.start >= hi:
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
    return out


def _cold_open_clip(
    w: _Window, duration: float, keep_spans: KeepSpans | None
) -> tuple[float, float] | None:
    """Plage source de `duration` s centrée sur le pic de `w`, dans les bornes de la fenêtre.

    Avec autocut : contenue dans une plage gardée contiguë (celle du pic, sinon la plus proche) ;
    plus courte que `duration` si cette plage l'est. None si rien n'est gardé.
    """
    spans = _fragments(w, keep_spans)
    if not spans:
        return None
    span = min(
        spans, key=lambda f: 0.0 if f[0] <= w.peak <= f[1] else min(abs(w.peak - x) for x in f)
    )
    lo, hi = span
    dur = min(duration, hi - lo)
    start = min(max(w.peak - dur / 2, lo), hi - dur)
    return start, start + dur


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
    `editing.order == "countdown"` : moments du moins bon au meilleur (score croissant).
    `editing.cold_open.enabled` : un clip centré sur le pic du meilleur moment ouvre la timeline
    (sans zoom, sans captions, non éclaté) ; il compte dans la durée cible.
    """
    ed = profile.editing
    target = ed.target_duration_s
    cold_cfg = ed.cold_open if ed.cold_open.enabled else None
    if cold_cfg and target - cold_cfg.duration_s < ed.min_clip_s:
        log.warning(
            "cold_open ignoré : durée cible %.1fs trop courte pour %.1fs de cold open + un moment.",
            target,
            cold_cfg.duration_s,
        )
        cold_cfg = None
    all_windows = _windows(highlights, profile)
    slow = ed.slowmo if ed.slowmo.enabled else None
    windows = _select(
        all_windows, target - (cold_cfg.duration_s if cold_cfg else 0), keep_spans, slow
    )

    cold: Clip | None = None
    if cold_cfg and windows:
        best = max(windows, key=lambda x: x.score)  # à score égal : le premier chronologiquement
        span = _cold_open_clip(best, cold_cfg.duration_s, keep_spans)
        if span:
            cold = Clip(
                source=best.source,
                start=span[0],
                end=span[1],
                highlight_label=best.label,
                moment=-1,
                peak_t=best.peak,
            )
    cold_dur = cold.duration / cold.speed if cold else 0.0

    if ed.order == "countdown":
        windows.sort(key=lambda x: x.score)  # tri stable : égalité = ordre chronologique
    if beats and profile.audio.beat_sync:
        _snap(windows, beats, profile, keep_spans, t0=cold_dur, slow=slow)

    punch = ed.autocut.punch_in
    clips: list[Clip] = [cold] if cold else []
    for mid, w in enumerate(windows):
        for k, (s, e) in enumerate(_fragments(w, keep_spans)):
            zoom = punch if punch > 1 and k % 2 == 1 else 1.0
            # [avant][ralenti][après] : même moment, même zoom, vitesse 1 / factor / 1
            pieces = [(s, e, False)]
            if slow is not None and (r := _slow_range(w, (s, e), slow)) is not None:
                pieces = [(s, r[0], False), (r[0], r[1], True), (r[1], e, False)]
            for ps, pe, is_slow in pieces:
                if pe - ps <= 1e-9:
                    continue
                clips.append(
                    Clip(
                        source=w.source,
                        start=ps,
                        end=pe,
                        speed=slow.factor if is_slow and slow else 1.0,
                        interpolate=bool(is_slow and slow and slow.interpolation != "none"),
                        highlight_label=w.label,
                        zoom=zoom,
                        moment=mid,
                        peak_t=w.peak,
                    )
                )
    n_real = len(clips) - (1 if cold else 0)
    timeline = Timeline(
        profile_id=profile.id,
        fps=profile.outputs[0].fps,
        clips=clips,
        cold_open_clips=1 if cold else 0,
        music=music,
        beats=[
            b for b in sorted(beats or []) if 0 <= b <= sum(c.duration / c.speed for c in clips)
        ],
    )
    if transcript:
        if len({c.source for c in clips[len(clips) - n_real :]}) > 1 and any(
            s.source is None for s in transcript
        ):
            log.warning(
                "Transcript sans champ 'source' et plusieurs sources : captions appliquées "
                "à tous les clips par temps source (risque d'incohérence)."
            )
        timeline.captions = _remap_captions(clips[len(clips) - n_real :], transcript, cold_dur)
    return timeline
