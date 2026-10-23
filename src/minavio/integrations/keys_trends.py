"""Clés optionnelles des sources de tendances et des canaux de notification.

Lues dans l'environnement, puis dans le fichier de secrets local (`minavio.config.secrets`).
Aucune valeur n'est journalisée : les messages ne citent que les noms des variables.
"""

from __future__ import annotations

import os

from minavio.config.secrets import load_secrets

TREND_KEYS: tuple[str, ...] = (
    "REDDIT_CLIENT_ID",
    "REDDIT_CLIENT_SECRET",
    "TWITCH_CLIENT_ID",
    "TWITCH_CLIENT_SECRET",
    "DISCORD_WEBHOOK_URL",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "NTFY_TOPIC",
    "NTFY_SERVER",
)


def get_trend_key(name: str) -> str | None:
    """Valeur de la clé `name` (env puis secrets), None si absente ou vide."""
    if name not in TREND_KEYS:
        raise ValueError(f"Clé non déclarée dans TREND_KEYS : {name}")
    load_secrets()
    value = os.environ.get(name, "").strip()
    return value or None
