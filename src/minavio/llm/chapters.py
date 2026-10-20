"""Chapitres YouTube : un chapitre par moment du montage, titres courts via le LLM local."""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field

from pydantic import BaseModel

from minavio.config.schema import ChannelProfile
from minavio.core.models import Chapter, Highlight, Moment, Timeline
from minavio.errors import LLMUnavailableError
from minavio.llm._context import transcript_context
from minavio.llm.client import LocalLLM
from minavio.llm.kb import genre_query, query_from, with_knowledge
from minavio.llm.prompts import load_prompt

log = logging.getLogger(__name__)

MIN_CHAPTERS = 3
MIN_CHAPTER_S = 10.0
MAX_TITLE_WORDS = 5
INTRO_TITLE = "Intro"
_CONTEXT_CHARS = 400

_FALLBACK_TITLES = {
    "kill": "Kill",
    "double kill": "Double kill",
    "triple kill": "Triple kill",
    "knock": "Knock",
    "clutch": "Clutch",
    "laugh": "Gros rire",
    "funny": "Gros rire",
    "fail": "Fail",
    "chaos": "Moment chaud",
    "hot": "Moment chaud",
    "fun fact": "Fun fact",
}
_DEFAULT_TITLE = "Moment chaud"
_CHAPTER_NUM_RE = re.compile(r"^\s*chapitres?\s*\d+\s*[:\-\u2013.)]*\s*", re.IGNORECASE)
_LEADING_NUM_RE = re.compile(r"^\s*\d+\s*[.):\-\u2013]\s+")


class _Title(BaseModel):
    id: int
    title: str


class _TitlesResponse(BaseModel):
    items: list[_Title]


@dataclass
class _Draft:
    start: float
    end: float
    label: str | None
    strength: float
    reason: str | None = None
    signals: dict[str, float] = field(default_factory=dict)
    pinned_title: str | None = None


def _best_highlight(
    timeline: Timeline, moment: Moment, highlights: list[Highlight]
) -> Highlight | None:
    """Highlight source qui recouvre le plus le moment (en temps source, via ses clips)."""
    best, best_ov = None, 0.0
    for h in highlights:
        ov = 0.0
        for clip in timeline.clips[moment.first_clip : moment.last_clip + 1]:
            if clip.source == h.source:
                ov += max(0.0, min(clip.end, h.end) - max(clip.start, h.start))
        if ov > best_ov:
            best, best_ov = h, ov
    return best


def _drafts(timeline: Timeline, highlights: list[Highlight]) -> list[_Draft]:
    drafts: list[_Draft] = []
    total = timeline.duration
    moments = timeline.moments()
    for i, m in enumerate(moments):
        end = moments[i + 1].start if i + 1 < len(moments) else max(m.end, total)
        h = _best_highlight(timeline, m, highlights)
        d = _Draft(
            start=m.start,
            end=end,
            label=m.label or (h.label if h else None),
            strength=h.score if h else 0.0,
            reason=h.reason if h else None,
            signals=dict(h.signals) if h else {},
        )
        if m.cold_open:
            d.pinned_title = INTRO_TITLE
            d.strength = float("inf")
        drafts.append(d)
    return drafts


def _merge_short(drafts: list[_Draft]) -> list[_Draft]:
    """Fusionne les chapitres < 10 s avec le suivant (dernier : avec le précédent)."""
    drafts = list(drafts)
    while len(drafts) > 1:
        idx = next((i for i, d in enumerate(drafts) if d.end - d.start < MIN_CHAPTER_S), None)
        if idx is None:
            break
        a, b = (idx, idx + 1) if idx + 1 < len(drafts) else (idx - 1, idx)
        first, second = drafts[a], drafts[b]
        keep = first if first.strength >= second.strength else second
        drafts[a : b + 1] = [
            _Draft(
                start=first.start,
                end=second.end,
                label=keep.label,
                strength=keep.strength,
                reason=keep.reason,
                signals=keep.signals,
                pinned_title=first.pinned_title or second.pinned_title,
            )
        ]
    return drafts


def _base_title(label: str | None) -> str:
    key = (label or "").replace("_", " ").replace("-", " ").strip().lower()
    if not key:
        return _DEFAULT_TITLE
    return _FALLBACK_TITLES.get(key, key[0].upper() + key[1:])


def _dedupe(titles: list[str]) -> list[str]:
    """Numérote les titres répétés : « Kill », « Kill (2) », « Kill (3) »."""
    seen: dict[str, int] = {}
    taken = {t.casefold() for t in titles}
    out: list[str] = []
    for t in titles:
        k = t.casefold()
        seen[k] = seen.get(k, 0) + 1
        if seen[k] == 1:
            out.append(t)
            continue
        n = seen[k]
        cand = f"{t} ({n})"
        while cand.casefold() in taken:
            n += 1
            cand = f"{t} ({n})"
        taken.add(cand.casefold())
        out.append(cand)
    return out


def _clean_title(raw: str) -> str:
    text = "".join(
        c for c in raw if c.isalnum() or unicodedata.category(c)[0] in {"Z", "P", "S", "M"}
    )
    text = "".join(c for c in text if unicodedata.category(c) not in {"So", "Sk", "Mn", "Cf"})
    text = text.strip().strip("\"'«»“”`")
    text = _CHAPTER_NUM_RE.sub("", text)
    text = _LEADING_NUM_RE.sub("", text)
    return " ".join(text.split()[:MAX_TITLE_WORDS]).strip(" .:-\u2013")


def _llm_titles(
    timeline: Timeline, drafts: list[_Draft], profile: ChannelProfile, llm: LocalLLM
) -> dict[int, str]:
    budget = getattr(llm, "context_budget_tokens", 8000)
    chars = max(80, min(_CONTEXT_CHARS, budget * 2 // max(1, len(drafts)) - 150))
    ctx = [
        {
            "id": i,
            "start": round(d.start, 1),
            "label": d.label,
            "reason": d.reason,
            "signals": d.signals,
            "transcript": transcript_context(timeline.captions, d.start, d.end, chars),
        }
        for i, d in enumerate(drafts)
        if d.pinned_title is None
    ]
    if not ctx:
        return {}
    user = (
        f"Chaîne : {profile.display_name}. {profile.description}\n"
        f"Durée de la vidéo : {timeline.duration:.0f}s.\nChapitres :\n"
        + json.dumps(ctx, ensure_ascii=False, indent=1)
    )
    query = query_from(
        genre_query(profile),
        *(part for c in ctx for part in (c["label"], c["reason"])),
        "chapitres",
    )
    system = with_knowledge(
        load_prompt(profile.llm.prompt, "chapters"), profile, "chapters", query, llm
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    try:
        result = llm.chat_json(messages, _TitlesResponse)
    except LLMUnavailableError as exc:
        log.warning("%s Titres de chapitres déterministes utilisés.", exc)
        return {}
    except ValueError as exc:
        log.warning("Titres de chapitres LLM invalides (%s) : titres déterministes.", exc)
        return {}
    valid = {c["id"] for c in ctx}
    out: dict[int, str] = {}
    for item in result.items:
        title = _clean_title(item.title)
        if item.id in valid and title and item.id not in out:
            out[item.id] = title
    return out


def generate_chapters(
    timeline: Timeline,
    highlights: list[Highlight],
    profile: ChannelProfile,
    llm: LocalLLM | None = None,
) -> list[Chapter]:
    """Chapitres YouTube (premier à 0:00, >= 3, chacun >= 10 s) ou [] si impossible."""
    drafts = _merge_short(_drafts(timeline, highlights))
    if len(drafts) < MIN_CHAPTERS:
        log.info(
            "Chapitres ignorés : %d chapitre(s) valide(s) (YouTube en exige %d, >= %d s chacun).",
            len(drafts),
            MIN_CHAPTERS,
            int(MIN_CHAPTER_S),
        )
        return []
    drafts[0].start = 0.0

    proposed: dict[int, str] = {}
    if profile.llm.enabled:
        client = llm or LocalLLM.from_settings(profile.llm)
        proposed = _llm_titles(timeline, drafts, profile, client)
    titles = _dedupe(
        [d.pinned_title or proposed.get(i) or _base_title(d.label) for i, d in enumerate(drafts)]
    )
    return [Chapter(start=round(d.start, 2), title=t) for d, t in zip(drafts, titles, strict=True)]


def _stamp(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def format_chapters(chapters: list[Chapter]) -> str:
    """Lignes `M:SS Titre` (`H:MM:SS` dès une heure), à coller dans la description YouTube."""
    return "\n".join(f"{_stamp(c.start)} {c.title}" for c in chapters)
