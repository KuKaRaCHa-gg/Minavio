"""Titre, hook, description, tags et popups à partir des highlights retenus."""

from __future__ import annotations

import json
import logging

from pydantic import BaseModel, Field, field_validator

from minavio.config.schema import ChannelProfile
from minavio.core.models import Highlight, TranscriptSegment
from minavio.errors import LLMUnavailableError
from minavio.llm._context import transcript_context
from minavio.llm.client import LocalLLM
from minavio.llm.prompts import load_prompt

log = logging.getLogger(__name__)

MAX_HOOK_WORDS = 8
_CONTEXT_CHARS_PER_HIGHLIGHT = 1500


class Popup(BaseModel):
    at: float = Field(ge=0)  # secondes depuis le début de la vidéo montée
    text: str


class VideoCopy(BaseModel):
    title: str
    hook: str  # <= 8 mots, titre animé
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    popups: list[Popup] = Field(default_factory=list)

    @field_validator("hook")
    @classmethod
    def _limit_hook(cls, v: str) -> str:
        return " ".join(v.split()[:MAX_HOOK_WORDS])

    @field_validator("tags")
    @classmethod
    def _clean_tags(cls, v: list[str]) -> list[str]:
        seen: dict[str, None] = {}
        for tag in v:
            t = tag.strip().lstrip("#").strip()
            if t:
                seen.setdefault(t, None)
        return list(seen)[:15]


def _popup_text(label: str, n: int) -> str:
    key = label.lower()
    if "kill" in key or "knock" in key:
        return "+1 KILL"
    if "fact" in key:
        return f"FUN FACT #{n}"
    return label.replace("_", " ").upper()


def _montage_offsets(highlights: list[Highlight]) -> list[float]:
    offsets, t = [], 0.0
    for h in highlights:
        offsets.append(t)
        t += h.duration
    return offsets


def fallback_copy(highlights: list[Highlight], profile: ChannelProfile) -> VideoCopy:
    """Copy déterministe, sans LLM, dérivé des labels."""
    if not highlights:
        return VideoCopy(title=profile.display_name, hook=profile.display_name)
    labels = [h.label for h in highlights]
    top = max(set(labels), key=labels.count).replace("_", " ")
    n = len(highlights)
    title = f"{profile.display_name} : {n} moments {top}".strip()
    return VideoCopy(
        title=title,
        hook=f"{n} moments {top}"[:60],
        description=f"{profile.display_name} - {n} moments ({top}).",
        tags=[profile.display_name.replace(" ", ""), top.replace(" ", ""), *profile.games],
        popups=[
            Popup(at=off, text=_popup_text(h.label, i + 1))
            for i, (h, off) in enumerate(zip(highlights, _montage_offsets(highlights), strict=True))
        ],
    )


def generate_copy(
    highlights: list[Highlight],
    transcript: list[TranscriptSegment],
    profile: ChannelProfile,
    llm: LocalLLM | None = None,
) -> VideoCopy:
    if not profile.llm.enabled or not highlights:
        return fallback_copy(highlights, profile)

    llm = llm or LocalLLM.from_settings(profile.llm)
    offsets = _montage_offsets(highlights)
    total = sum(h.duration for h in highlights)
    moments = [
        {
            "montage_start": round(off, 2),
            "duration": round(h.duration, 2),
            "label": h.label,
            "reason": h.reason,
            "transcript": transcript_context(
                transcript, h.start, h.end, _CONTEXT_CHARS_PER_HIGHLIGHT
            ),
        }
        for h, off in zip(highlights, offsets, strict=True)
    ]
    user = (
        f"Chaîne : {profile.display_name}. {profile.description}\n"
        f"Durée totale du montage : {total:.1f}s.\nMoments :\n"
        + json.dumps(moments, ensure_ascii=False, indent=1)
    )
    messages = [
        {"role": "system", "content": load_prompt(profile.llm.prompt, "copy")},
        {"role": "user", "content": user},
    ]
    try:
        result = llm.chat_json(messages, VideoCopy)
    except LLMUnavailableError as exc:
        log.warning("%s Copy déterministe utilisé.", exc)
        return fallback_copy(highlights, profile)
    except ValueError as exc:
        log.warning("Copy LLM invalide (%s) : copy déterministe utilisé.", exc)
        return fallback_copy(highlights, profile)

    result.popups = [p for p in result.popups if p.at <= total and p.text.strip()]
    if not result.title.strip() or not result.hook.strip():
        fb = fallback_copy(highlights, profile)
        result.title = result.title.strip() or fb.title
        result.hook = result.hook.strip() or fb.hook
    return result
