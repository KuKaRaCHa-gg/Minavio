"""Clés d'API des intégrations (environnement ou `secrets.env` ; jamais journalisées)."""

from __future__ import annotations

import os

from minavio.config.secrets import load_secrets
from minavio.errors import ConfigError

INTEGRATION_KEYS = ("YOUTUBE_API_KEY",)


def get_key(name: str) -> str:
    """Valeur de la clé `name` (environnement d'abord, puis `secrets.env`) ou erreur actionnable."""
    load_secrets()
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise ConfigError(
            f"Clé {name} absente. Ajoute `{name}=...` dans secrets.env (ou la variable "
            "d'environnement) ; procédure de création de la clé dans docs/GROWTH.md."
        )
    return value
