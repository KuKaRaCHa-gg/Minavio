"""Re-scoring des highlights candidats par le LLM local."""

from __future__ import annotations

import json
import logging

from pydantic import BaseModel, Field

from minavio.config.schema import ChannelProfile
from minavio.core.models import Highlight, TranscriptSegment
from minavio.errors import LLMUnavailableError
from minavio.llm._context import CONTEXT_PAD_S, estimate_tokens, transcript_context
from minavio.llm.client import LocalLLM
from minavio.llm.prompts import load_prompt

log = logging.getLogger(__name__)

TOKEN_BUDGET = 8000
_MAX_CONTEXT_CHARS = 6000  # plafond de transcript par candidat (~1500 tokens)


class _RankItem(BaseModel):
    id: int
    score: float = Field(ge=0, le=1)
    label: str | None = None
    reason: str | None = None
    start: float | None = None
    end: float | None = None


class _RankResponse(BaseModel):
    items: list[_RankItem]


def _fallback(highlights: list[Highlight], limit: int) -> list[Highlight]:
    return sorted(highlights, key=lambda h: h.score, reverse=True)[:limit]


def _candidate_block(idx: int, h: Highlight, transcript: list[TranscriptSegment]) -> str:
    lo, hi = max(0.0, h.start - CONTEXT_PAD_S), h.end + CONTEXT_PAD_S
    return json.dumps(
        {
            "id": idx,
            "window": [round(lo, 2), round(hi, 2)],
            "moment": [round(h.start, 2), round(h.end, 2)],
            "detector": h.detector,
            "detector_score": round(h.score, 3),
            "label": h.label,
            "transcript": transcript_context(transcript, lo, hi, _MAX_CONTEXT_CHARS),
        },
        ensure_ascii=False,
    )


def _make_batches(blocks: list[tuple[int, str]], budget_tokens: int) -> list[list[tuple[int, str]]]:
    batches: list[list[tuple[int, str]]] = []
    current: list[tuple[int, str]] = []
    used = 0
    for item in blocks:
        cost = estimate_tokens(item[1])
        if current and used + cost > budget_tokens:
            batches.append(current)
            current, used = [], 0
        current.append(item)
        used += cost
    if current:
        batches.append(current)
    return batches


def _refined_bounds(h: Highlight, item: _RankItem) -> tuple[float, float]:
    """Bornes du LLM si elles restent dans [start-5, end+5] et sont ordonnées, sinon originales."""
    if item.start is None or item.end is None:
        return h.start, h.end
    lo, hi = max(0.0, h.start - CONTEXT_PAD_S), h.end + CONTEXT_PAD_S
    if lo <= item.start < item.end <= hi:
        return item.start, item.end
    log.warning("Bornes LLM hors fenêtre ignorées pour [%.1f, %.1f].", h.start, h.end)
    return h.start, h.end


def rank_highlights(
    highlights: list[Highlight],
    transcript: list[TranscriptSegment],
    profile: ChannelProfile,
    llm: LocalLLM | None = None,
) -> list[Highlight]:
    limit = profile.llm.max_highlights
    if not highlights:
        return []
    if not profile.llm.enabled:
        log.warning("LLM désactivé : classement par score du détecteur uniquement.")
        return _fallback(highlights, limit)

    llm = llm or LocalLLM.from_settings(profile.llm)
    system = load_prompt(profile.llm.prompt, "rank")
    system_tokens = estimate_tokens(system)
    budget = min(TOKEN_BUDGET, getattr(llm, "context_budget_tokens", TOKEN_BUDGET))
    blocks = [(i, _candidate_block(i, h, transcript)) for i, h in enumerate(highlights)]
    batches = _make_batches(blocks, max(500, budget - system_tokens - 300))

    results: dict[int, _RankItem] = {}
    try:
        for batch in batches:
            ids = {i for i, _ in batch}
            user = "Candidats :\n" + "\n".join(b for _, b in batch)
            try:
                resp = llm.chat_json(
                    [{"role": "system", "content": system}, {"role": "user", "content": user}],
                    _RankResponse,
                )
            except ValueError as exc:
                log.warning("Lot ignoré, réponse LLM invalide : %s", exc)
                continue
            for item in resp.items:
                if item.id in ids:
                    results[item.id] = item
    except LLMUnavailableError as exc:
        log.warning("%s Classement par score du détecteur uniquement.", exc)
        return _fallback(highlights, limit)

    ranked: list[Highlight] = []
    for i, h in enumerate(highlights):
        item = results.get(i)
        if item is None:  # non évalué : score neutre = celui du détecteur
            ranked.append(h)
            continue
        start, end = _refined_bounds(h, item)
        ranked.append(
            h.model_copy(
                update={
                    "start": start,
                    "end": end,
                    "score": min(1.0, max(0.0, 0.5 * h.score + 0.5 * item.score)),
                    "label": (item.label or h.label).strip() or h.label,
                    "reason": item.reason or h.reason,
                }
            )
        )
    return sorted(ranked, key=lambda h: h.score, reverse=True)[:limit]
