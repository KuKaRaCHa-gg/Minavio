"""Client minimal pour un serveur LLM local compatible OpenAI (Ollama, vLLM, llama.cpp...)."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from minavio.config.schema import LlmSettings
from minavio.errors import LLMUnavailableError

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


class _BadRequest(Exception):
    """HTTP 400 : typiquement `response_format` non supporté."""


def strip_think(text: str) -> str:
    """Retire les blocs `<think>...</think>` (Qwen3), y compris un bloc non fermé."""
    text = _THINK_RE.sub("", text)
    if "<think>" in text.lower():
        text = re.split(r"<think>", text, flags=re.IGNORECASE)[0]
    return text.replace("</think>", "").strip()


def extract_json(text: str) -> Any:
    """Extrait le premier objet/tableau JSON de `text` (entouré de texte ou de ```json)."""
    text = strip_think(text)
    candidates = [m.group(1) for m in _FENCE_RE.finditer(text)] + [text]
    decoder = json.JSONDecoder()
    for cand in candidates:
        for opener in ("{", "["):
            pos = cand.find(opener)
            while pos != -1:
                try:
                    value, _ = decoder.raw_decode(cand, pos)
                    return value
                except json.JSONDecodeError:
                    pos = cand.find(opener, pos + 1)
    raise ValueError("aucun JSON valide trouvé dans la réponse du modèle")


class LocalLLM:
    def __init__(
        self,
        base_url: str,
        model: str,
        temperature: float = 0.2,
        timeout: float = 120,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.temperature = temperature
        self.timeout = timeout
        self._client = httpx.Client(timeout=timeout, transport=transport)
        self._json_mode = True  # passe à False si le serveur rejette response_format

    @classmethod
    def from_settings(cls, settings: LlmSettings) -> LocalLLM:
        return cls(settings.base_url, settings.model, settings.temperature)

    def _unavailable(self, detail: str) -> LLMUnavailableError:
        return LLMUnavailableError(
            f"LLM local injoignable ({self.base_url}, modèle '{self.model}') : {detail}. "
            "Vérifier que le serveur tourne (ex. `ollama serve`), puis `llm.base_url` / "
            "`llm.model` du profil ou MINAVIO_LLM_BASE_URL / MINAVIO_LLM_MODEL ; "
            "ou désactiver `llm.enabled`."
        )

    def ping(self) -> bool:
        try:
            return self._client.get(f"{self.base_url}/models").status_code == 200
        except httpx.HTTPError:
            return False

    def _post(self, messages: list[dict[str, str]], json_mode: bool) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "stream": False,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        try:
            resp = self._client.post(f"{self.base_url}/chat/completions", json=payload)
        except httpx.HTTPError as exc:
            raise self._unavailable(f"{type(exc).__name__}: {exc}") from exc
        if resp.status_code == 400 and json_mode:
            raise _BadRequest(resp.text[:200])
        if resp.status_code >= 400:
            raise self._unavailable(f"HTTP {resp.status_code} {resp.text[:200]}")
        try:
            content = resp.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise self._unavailable(f"réponse inattendue du serveur ({exc})") from exc
        return strip_think(content or "")

    def chat(self, messages: list[dict[str, str]]) -> str:
        return self._post(messages, json_mode=False)

    def _chat_maybe_json(self, messages: list[dict[str, str]]) -> str:
        if self._json_mode:
            try:
                return self._post(messages, json_mode=True)
            except _BadRequest:
                log.info("response_format refusé par le serveur : extraction JSON sans.")
                self._json_mode = False
        return self._post(messages, json_mode=False)

    def chat_json(self, messages: list[dict[str, str]], schema: type[T]) -> T:
        msgs = list(messages)
        last_error: Exception | None = None
        for attempt in range(2):
            raw = self._chat_maybe_json(msgs)
            try:
                return schema.model_validate(extract_json(raw))
            except (ValueError, ValidationError) as exc:
                last_error = exc
                if attempt == 0:
                    msgs = [
                        *messages,
                        {"role": "assistant", "content": raw},
                        {
                            "role": "user",
                            "content": "Ta réponse est invalide : "
                            f"{str(exc)[:600]}\nRéponds uniquement par un JSON valide "
                            "respectant exactement le format demandé.",
                        },
                    ]
        raise ValueError(f"Réponse JSON invalide du LLM après retry : {last_error}")
