"""Clés d'API des statistiques de jeu. Une valeur de clé n'est jamais journalisée ni affichée."""

from __future__ import annotations

import os

from minavio.config.secrets import load_secrets
from minavio.errors import MinavioError

STATS_KEYS = ("APEX_API_KEY", "RIOT_API_KEY")

_WHERE = {
    "APEX_API_KEY": "clé gratuite sur https://apexlegendsapi.com",
    "RIOT_API_KEY": "clé sur https://developer.riotgames.com",
}


def get_key(name: str) -> str | None:
    """Valeur de la clé (environnement, puis secrets.env), ou None si absente."""
    if name not in STATS_KEYS:
        raise ValueError(f"Clé de statistiques inconnue : {name}")
    load_secrets()
    return os.environ.get(name, "").strip() or None


def require_key(name: str) -> str:
    """Comme `get_key`, mais lève une `MinavioError` actionnable si la clé manque."""
    key = get_key(name)
    if key is None:
        raise MinavioError(
            f"{name} absente. Ajoute-la dans l'environnement ou dans secrets.env ({_WHERE[name]})."
        )
    return key
