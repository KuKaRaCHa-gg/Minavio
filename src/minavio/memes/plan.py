"""Plan des effets « .exe » : cues du LLM local (ou heuristique) -> SfxCue / Effect / Overlay.

Le LLM ne voit que des moments (label, temps de la timeline, extrait de transcript) et la liste
des tags disponibles. Tout ce qu'il renvoie est revalidé ici : temps dans la timeline, effets
autorisés, média résolu par `search`, densité plafonnée. Un cue invalide est simplement retiré.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from typing import Any, get_args

from pydantic import BaseModel

from minavio.config.schema import ChannelProfile, MemeSettings
from minavio.core.models import (
    Clip,
    Effect,
    EffectKind,
    Highlight,
    Overlay,
    SfxCue,
    Timeline,
    TranscriptSegment,
)
from minavio.errors import LLMUnavailableError
from minavio.llm._context import transcript_context
from minavio.llm.client import LocalLLM
from minavio.llm.prompts import load_prompt
from minavio.memes.library import available_tags, search

log = logging.getLogger(__name__)

CUE_TYPES = ("sound", "gif", "image", "video", "effect")
MEDIA_TYPES = ("gif", "image", "video")
EFFECT_KINDS = frozenset(get_args(EffectKind))
LIGHT_EXCLUDED = frozenset({"deep_fry", "glitch", "error_popup"})
MAX_MOMENTS = 40
MAX_EXCERPT_CHARS = 400
OVERLAY_BOX = {"x": 0.62, "y": 0.12, "w": 0.3, "h": 0.3}  # fractions du cadre
EFFECT_INTENSITY = 0.6

_EMPTY_PLAN: dict[str, list[Any]] = {"sfx": [], "effects": [], "overlays": []}


class _CuesResponse(BaseModel):
    cues: list[Any]  # validé cue par cue ensuite : un cue mal formé ne fait pas échouer le lot


@dataclass(frozen=True)
class _Moment:
    at: float  # temps dans la timeline rendue
    label: str
    excerpt: str


def _to_timeline(clips: list[Clip], starts: list[float], source: Any, t: float) -> float | None:
    for clip, start in zip(clips, starts, strict=True):
        if clip.source == source and clip.start <= t < clip.end:
            return start + (t - clip.start) / clip.speed
    return None


def _moments(
    timeline: Timeline, highlights: list[Highlight], transcripts: list[TranscriptSegment]
) -> list[_Moment]:
    starts = timeline.clip_starts()
    out: list[_Moment] = []
    for h in highlights:
        at = _to_timeline(timeline.clips, starts, h.source, h.start)
        if at is None:  # moment non retenu dans le montage
            continue
        segs = [s for s in transcripts if s.source in (None, h.source)]
        excerpt = transcript_context(segs, h.start, h.end, MAX_EXCERPT_CHARS)
        out.append(_Moment(at=round(at, 2), label=h.label, excerpt=excerpt))
    return sorted(out, key=lambda m: m.at)[:MAX_MOMENTS]


def _allowed_effects(settings: MemeSettings) -> list[str]:
    excluded = LIGHT_EXCLUDED if settings.style == "light" else frozenset()
    return [e for e in settings.effects if e in EFFECT_KINDS and e not in excluded]


def _user_message(moments: list[_Moment], duration: float, settings: MemeSettings) -> str:
    payload = {
        "duration": round(duration, 2),
        "tags": available_tags(settings),
        "effects": _allowed_effects(settings),
        "moments": [{"at": m.at, "label": m.label, "transcript": m.excerpt} for m in moments],
    }
    return "Données :\n" + json.dumps(payload, ensure_ascii=False)


def _ask_llm(
    moments: list[_Moment],
    duration: float,
    settings: MemeSettings,
    profile: ChannelProfile,
    llm: LocalLLM | None,
) -> list[Any]:
    client = llm or LocalLLM.from_settings(profile.llm)
    system = load_prompt("memes", "memes")
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": _user_message(moments, duration, settings)},
    ]
    return client.chat_json(messages, _CuesResponse).cues


def _valid_cue(raw: Any, duration: float) -> dict[str, Any] | None:
    """Cue normalisé, ou None si le type, le temps ou les tags sont inexploitables."""
    if not isinstance(raw, dict):
        return None
    ctype, at = raw.get("type"), raw.get("at")
    if ctype not in CUE_TYPES:
        return None
    if isinstance(at, bool) or not isinstance(at, int | float) or not math.isfinite(at):
        return None
    if not 0 <= at <= duration:
        return None
    tags_raw = raw.get("tags")
    tags = [t for t in tags_raw if isinstance(t, str) and t] if isinstance(tags_raw, list) else []
    return {
        "type": ctype,
        "at": float(at),
        "tags": tags,
        "effect": raw.get("effect"),
        "duration": raw.get("duration"),
    }


def _heuristic_cues(moments: list[_Moment]) -> list[dict[str, Any]]:
    """Sans LLM : kill -> zoom + hit ; multi_kill -> shake + flash ; fail -> freeze + bruh."""
    cues: list[dict[str, Any]] = []

    def effect(kind: str, at: float, duration: float | None = None) -> None:
        cues.append({"type": "effect", "at": at, "effect": kind, "duration": duration, "tags": []})

    def sound(tag: str, at: float) -> None:
        cues.append({"type": "sound", "at": at, "tags": [tag], "effect": None, "duration": None})

    for m in moments:
        label = m.label.lower()
        if "multi" in label and "kill" in label:
            effect("shake", m.at, 0.6)
            effect("flash", m.at, 0.3)
        elif "kill" in label:
            effect("zoom_punch", m.at, 0.4)
            sound("hit", m.at)
        elif "fail" in label or "funny" in label:
            effect("freeze", m.at, 0.6)
            sound("bruh", m.at)
    return cues


def _clamp(value: Any, default: float, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return default
    return min(high, max(low, float(value)))


def _materialize(
    cues: list[dict[str, Any]], settings: MemeSettings
) -> list[tuple[float, str, Any]]:
    """Transforme chaque cue en objet final ; renvoie (at, bucket, objet) avec bucket de plan()."""
    allowed = set(_allowed_effects(settings))
    produced: list[tuple[float, str, Any]] = []
    for cue in cues:
        at = cue["at"]
        if cue["type"] == "effect":
            kind = cue.get("effect")
            if kind not in allowed:
                continue
            effect = Effect(
                kind=kind,
                at=at,
                duration=_clamp(cue.get("duration"), 0.5, 0.1, 5.0),
                intensity=EFFECT_INTENSITY,
            )
            produced.append((at, "effects", effect))
            continue
        if not cue["tags"]:
            continue
        assets = search(cue["tags"], kind=cue["type"], settings=settings)
        if not assets:  # aucun média correspondant : cue abandonné
            continue
        asset = assets[0]
        if cue["type"] == "sound":
            name = asset.tags[0] if asset.tags else cue["tags"][0]
            produced.append((at, "sfx", SfxCue(at=at, sound=name, path=asset.path)))
        else:
            overlay = Overlay(
                template="media",
                start=at,
                duration=_clamp(cue.get("duration"), 2.0, 0.5, 8.0),
                props={"path": str(asset.path), "kind": cue["type"], **OVERLAY_BOX},
            )
            produced.append((at, "overlays", overlay))
    return produced


def _build(cues: list[dict[str, Any]], duration: float, settings: MemeSettings) -> dict[str, list]:
    """Matérialise, puis applique la densité (cues par minute, divisée par deux en style light)."""
    per_min = settings.density_per_min * (0.5 if settings.style == "light" else 1.0)
    cap = math.floor(per_min * duration / 60)
    produced = sorted(_materialize(cues, settings), key=lambda p: p[0])
    plan: dict[str, list[Any]] = {"sfx": [], "effects": [], "overlays": []}
    for _, bucket, obj in produced[:cap]:
        plan[bucket].append(obj)
    return plan


def plan_memes(
    timeline: Timeline,
    highlights: list[Highlight],
    transcripts: list[TranscriptSegment],
    profile: ChannelProfile,
    llm: LocalLLM | None = None,
    *,
    use_llm: bool = True,
) -> dict[str, list]:
    """Renvoie `{"sfx": [SfxCue], "effects": [Effect], "overlays": [Overlay]}` pour la timeline.

    LLM local si `use_llm` et `profile.llm.enabled` ; sinon, ou si le serveur est injoignable ou
    répond du JSON inexploitable, heuristique déterministe.
    """
    settings = profile.memes
    if not settings.enabled:
        return {key: [] for key in _EMPTY_PLAN}
    duration = timeline.duration
    moments = _moments(timeline, highlights, transcripts)
    if not moments:
        return {key: [] for key in _EMPTY_PLAN}
    cues: list[dict[str, Any]] | None = None
    if use_llm and profile.llm.enabled:
        try:
            raw = _ask_llm(moments, duration, settings, profile, llm)
            cues = [c for r in raw if (c := _valid_cue(r, duration)) is not None]
        except (LLMUnavailableError, ValueError) as exc:
            log.warning("Memes : LLM indisponible ou réponse invalide (%s). Heuristique.", exc)
    if cues is None:
        cues = [n for c in _heuristic_cues(moments) if (n := _valid_cue(c, duration)) is not None]
    return _build(cues, duration, settings)
