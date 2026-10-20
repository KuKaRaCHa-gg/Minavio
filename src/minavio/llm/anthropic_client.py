"""Client Claude via le SDK officiel `anthropic` (extra `cloud`). Import paresseux."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from minavio.errors import LLMUnavailableError
from minavio.llm.client import DEFAULT_CONTEXT_BUDGET, BaseLLM, encode_image

log = logging.getLogger(__name__)

BETAS = ["server-side-fallback-2026-07-01"]
MAX_TOKENS = 16000


class AnthropicLLM(BaseLLM):
    def __init__(
        self,
        model: str,
        api_key: str,
        effort: str | None = None,
        vision_model: str | None = None,
        context_budget_tokens: int = DEFAULT_CONTEXT_BUDGET,
    ) -> None:
        self.model = model
        # None = pas de vision : les images ne partent chez Anthropic que si c'est demandé.
        self.vision_model = vision_model
        self.api_key = api_key
        self.effort = effort or "low"
        self.name = f"Claude {model}"
        self.context_budget_tokens = context_budget_tokens
        self._sdk_mod: Any = None
        self._client: Any = None

    def _sdk(self) -> Any:
        if self._sdk_mod is None:
            try:
                import anthropic
            except ImportError as exc:
                raise LLMUnavailableError(
                    "Le SDK `anthropic` n'est pas installé : lancer `uv sync --extra cloud` "
                    "(ou retirer le fournisseur anthropic de `llm.providers`)."
                ) from exc
            self._sdk_mod = anthropic
        return self._sdk_mod

    def _get_client(self) -> Any:
        if self._client is None:
            self._client = self._sdk().Anthropic(api_key=self.api_key)
        return self._client

    def ping(self) -> bool:
        try:
            self._get_client().models.retrieve(self.model)
            return True
        except Exception:  # SDK absent, clé invalide, réseau : tout est « KO » pour un ping
            return False

    def _build(
        self, messages: list[dict[str, str]], images: list[Path] | None
    ) -> tuple[str, list[dict[str, Any]]]:
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        msgs: list[dict[str, Any]] = [
            {"role": m["role"], "content": m["content"]} for m in messages if m["role"] != "system"
        ]
        if images:
            blocks = []
            for img in images:
                media, b64 = encode_image(img)
                blocks.append(
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": media, "data": b64},
                    }
                )
            for m in reversed(msgs):
                if m["role"] == "user":
                    m["content"] = [*blocks, {"type": "text", "text": m["content"]}]
                    break
            else:
                msgs.append({"role": "user", "content": blocks})
        return system, msgs

    def chat(self, messages: list[dict[str, str]], images: list[Path] | None = None) -> str:
        sdk = self._sdk()
        system, msgs = self._build(messages, images)
        # Ni `thinking` ni `temperature` : thinking toujours actif, sampling rejeté ; seul
        # `effort` règle le raisonnement.
        kwargs: dict[str, Any] = {
            "model": self.vision_model if images else self.model,
            "max_tokens": MAX_TOKENS,
            "betas": BETAS,
            "fallbacks": "default",
            "output_config": {"effort": self.effort},
            "messages": msgs,
        }
        if system:
            kwargs["system"] = system
        try:
            response = self._get_client().beta.messages.create(**kwargs)
        except sdk.AuthenticationError as exc:
            raise LLMUnavailableError(
                f"Clé Anthropic invalide ou refusée ({exc}). Vérifier la variable "
                "ANTHROPIC_API_KEY (ou `api_key_env` du fournisseur)."
            ) from exc
        except sdk.RateLimitError as exc:
            raise LLMUnavailableError(f"Claude : limite de débit atteinte ({exc}).") from exc
        except sdk.APIStatusError as exc:
            code = getattr(exc, "status_code", "?")
            raise LLMUnavailableError(f"Claude : erreur API HTTP {code} ({exc}).") from exc
        except sdk.APIConnectionError as exc:
            raise LLMUnavailableError(f"Claude : API injoignable ({exc}).") from exc
        if getattr(response, "stop_reason", None) == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None) or "inconnue"
            raise LLMUnavailableError(f"Claude a refusé la requête (catégorie : {category}).")
        return "".join(
            b.text for b in response.content if getattr(b, "type", None) == "text"
        ).strip()
