"""Fournisseurs LLM (local, Claude, Gemini, GitHub, OpenAI-compatible) et chaîne de secours."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlparse

from pydantic import BaseModel

from minavio.config.schema import LlmProvider, LlmSettings
from minavio.errors import ConfigError, LLMUnavailableError
from minavio.llm.anthropic_client import AnthropicLLM
from minavio.llm.client import DEFAULT_CONTEXT_BUDGET, BaseLLM, LocalLLM

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

LOCAL_VISION_MODEL = "qwen3-vl:8b"
GOOGLE_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
GITHUB_BASE_URL = "https://models.github.ai/inference"
# GitHub Models (preview) : ~8 000 tokens en entrée par requête, on garde de la marge.
GITHUB_CONTEXT_BUDGET = 6000

_KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "google": "GEMINI_API_KEY",
    "github": "GITHUB_TOKEN",
    "openai_compat": "OPENAI_API_KEY",
}
_warned: set[str] = set()


def _warn_once(key: str, msg: str) -> None:
    if key not in _warned:
        _warned.add(key)
        log.warning(msg)


def build_provider(p: LlmProvider, settings: LlmSettings) -> BaseLLM | None:
    """Construit un fournisseur ; None (warning unique) si une clé requise est absente."""
    env = p.api_key_env or _KEY_ENV.get(p.kind)
    key = (os.environ.get(env) or None) if env else None
    temp = settings.temperature

    if p.kind == "local":
        model = p.model or settings.model
        return LocalLLM(
            p.base_url or settings.base_url,
            model,
            temp,
            api_key=key,
            vision_model=p.vision_model or LOCAL_VISION_MODEL,
            name=f"{'Qwen local' if 'qwen' in model.lower() else 'LLM local'} {model}",
        )

    if p.kind == "anthropic":
        if not key:
            _warn_once(
                f"anthropic:{env}",
                f"Fournisseur Claude ignoré : variable d'environnement {env} absente.",
            )
            return None
        model = p.model or "claude-opus-5-5"
        return AnthropicLLM(model, key, p.effort, p.vision_model)

    if p.kind in ("google", "github"):
        if not key:
            _warn_once(
                f"{p.kind}:{env}",
                f"Fournisseur {p.kind} ignoré : variable d'environnement {env} absente.",
            )
            return None
        if p.kind == "google":
            model, base, label, budget = "gemini-2.5-flash", GOOGLE_BASE_URL, "Gemini", None
        else:
            model, base, label = "openai/gpt-4.1", GITHUB_BASE_URL, "GitHub Models"
            budget = GITHUB_CONTEXT_BUDGET
        model = p.model or model
        return LocalLLM(
            p.base_url or base,
            model,
            temp,
            api_key=key,
            vision_model=p.vision_model or model,
            name=f"{label} {model}",
            label=label,
            context_budget_tokens=budget or DEFAULT_CONTEXT_BUDGET,
        )

    if p.kind == "openai_compat":
        if not (p.base_url and p.model):
            raise ConfigError("Fournisseur openai_compat : `base_url` et `model` obligatoires.")
        host = urlparse(p.base_url).netloc or p.base_url
        return LocalLLM(
            p.base_url,
            p.model,
            temp,
            api_key=key,  # optionnelle : certains serveurs n'en veulent pas
            vision_model=p.vision_model or p.model,
            name=f"{p.model} ({host})",
            label=f"LLM {host}",
        )

    raise ConfigError(f"Type de fournisseur LLM inconnu : {p.kind}")


def build_chain(settings: LlmSettings) -> LLMChain:
    if not settings.providers:
        # Comportement historique : un seul fournisseur local implicite.
        local = build_provider(LlmProvider(kind="local"), settings)
        return LLMChain([local] if local else [])
    built = [b for p in settings.providers if (b := build_provider(p, settings)) is not None]
    return LLMChain(built)


class LLMChain:
    """Essaie les fournisseurs dans l'ordre ; le premier qui répond est mémorisé."""

    def __init__(self, providers: list[BaseLLM]) -> None:
        self.providers = list(providers)
        self._preferred: BaseLLM | None = None

    @property
    def name(self) -> str:
        return " -> ".join(p.name for p in self.providers) or "(aucun fournisseur)"

    @property
    def context_budget_tokens(self) -> int:
        return min(
            (p.context_budget_tokens for p in self.providers), default=DEFAULT_CONTEXT_BUDGET
        )

    @property
    def vision_model(self) -> str | None:
        return next((p.vision_model for p in self.providers if p.vision_model), None)

    @property
    def has_vision(self) -> bool:
        return any(p.vision_model for p in self.providers)

    def _order(self, images: list[Path] | None) -> list[BaseLLM]:
        order = list(self.providers)
        if self._preferred in order:
            order.remove(self._preferred)
            order.insert(0, self._preferred)
        if images:
            order = [p for p in order if p.vision_model]
        return order

    def _run(self, images: list[Path] | None, call: Any) -> Any:
        order = self._order(images)
        if not order:
            what = "avec vision " if images else ""
            raise LLMUnavailableError(
                f"Aucun fournisseur LLM {what}disponible (clés absentes ou `vision_model` "
                "non configuré). Voir `llm.providers` du profil."
            )
        failures: list[tuple[str, LLMUnavailableError]] = []
        for p in order:
            try:
                result = call(p)
            except LLMUnavailableError as exc:
                log.warning("Fournisseur %s indisponible : %s", p.name, exc)
                failures.append((p.name, exc))
                continue
            self._preferred = p
            return result
        if len(failures) == 1:
            raise failures[0][1]
        raise LLMUnavailableError(
            "Tous les fournisseurs LLM ont échoué :\n- "
            + "\n- ".join(f"{n} : {e}" for n, e in failures)
        )

    def chat(self, messages: list[dict[str, str]], images: list[Path] | None = None) -> str:
        return self._run(images, lambda p: p.chat(messages, images))

    def chat_json(
        self, messages: list[dict[str, str]], schema: type[T], images: list[Path] | None = None
    ) -> T:
        return self._run(images, lambda p: p.chat_json(messages, schema, images))

    def ping(self) -> bool:
        return any(p.ping() for p in self.providers)
