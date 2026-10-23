"""Outils partagés des intégrations : LLM local optionnel, appel JSON gardé, tableaux Markdown.

Le LLM ne reçoit jamais d'images : uniquement des titres et des statistiques déjà calculées.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any, TypeVar

from pydantic import BaseModel

from minavio.errors import LLMUnavailableError
from minavio.llm.client import LocalLLM
from minavio.llm.prompts import load_prompt

T = TypeVar("T", bound=BaseModel)


def llm_for_profile(profile: Any) -> LocalLLM | None:
    """Client Qwen local si le profil l'active ; None sinon (rapport sans recommandations LLM)."""
    settings = getattr(profile, "llm", None)
    if settings is None or not getattr(settings, "enabled", False):
        return None
    try:
        return LocalLLM.from_settings(settings)
    except LLMUnavailableError:
        return None


def ask_llm(
    llm: Any, section: str, payload: dict[str, Any], schema: type[T]
) -> tuple[T | None, str | None]:
    """(réponse validée, note). Ne lève jamais : sans LLM, la note dit pourquoi."""
    if llm is None:
        return None, "LLM non sollicité (désactivé dans le profil ou non fourni)."
    messages = [
        {"role": "system", "content": load_prompt("growth", section)},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, indent=1)},
    ]
    try:
        return llm.chat_json(messages, schema), None
    except LLMUnavailableError as exc:
        return None, f"LLM local injoignable ({exc})."
    except ValueError as exc:
        return None, f"Réponse LLM invalide ({exc})."


def md_table(headers: list[str], rows: Iterable[Iterable[Any]]) -> str:
    rows = [list(r) for r in rows]
    if not rows:
        return "_Aucune donnée._"
    head = "| " + " | ".join(headers) + " |"
    sep = "|" + "|".join("---" for _ in headers) + "|"
    body = ["| " + " | ".join(_cell(c) for c in r) + " |" for r in rows]
    return "\n".join([head, sep, *body])


def _cell(value: Any) -> str:
    text = "—" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ")


def fmt_int(value: float | int | None) -> str:
    return "—" if value is None else f"{round(float(value)):,}".replace(",", " ")


def fmt_dec(value: float | None, digits: int = 2) -> str:
    return "—" if value is None else f"{value:.{digits}f}".replace(".", ",")


def fmt_pct(value: float | None) -> str:
    """Valeur déjà en pourcentage (0-100), ex. averageViewPercentage."""
    return "—" if value is None else f"{fmt_dec(value, 1)} %"
