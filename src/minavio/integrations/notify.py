"""Notifications : Discord (webhook), Telegram (bot) et ntfy. Ne lève jamais.

Un canal non configuré est ignoré ; un canal en échec émet un avertissement (sans l'URL ni le
jeton, qui figurent dans les URL des API) et les autres canaux sont quand même essayés.
"""

from __future__ import annotations

import json
import logging
import mimetypes
from collections.abc import Callable
from pathlib import Path

import httpx

from minavio import __version__
from minavio.integrations.keys_trends import get_trend_key

log = logging.getLogger("minavio.notify")

TIMEOUT_S = 15.0
NTFY_DEFAULT_SERVER = "https://ntfy.sh"


def _warn(channel: str, exc: Exception) -> None:
    status = getattr(getattr(exc, "response", None), "status_code", None)
    detail = f", HTTP {status}" if status else ""
    log.warning(
        "Notification %s non envoyée (%s%s). Vérifie sa configuration : docs/GROWTH.md.",
        channel,
        type(exc).__name__,
        detail,
    )


def _file_part(file: Path) -> tuple[str, bytes, str]:
    name = file.name
    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    return name, file.read_bytes(), mime


def _existing(file: str | Path | None) -> Path | None:
    if file is None:
        return None
    path = Path(file)
    if path.is_file():
        return path
    log.warning("Miniature absente, notification envoyée sans image : %s", path)
    return None


def _plain(title: str, message: str, url: str | None) -> str:
    parts = [title, "", message] if message else [title]
    if url:
        parts.append(url)
    return "\n".join(parts)


def _discord(
    http: httpx.Client, title: str, message: str, file: Path | None, url: str | None
) -> bool:
    webhook = get_trend_key("DISCORD_WEBHOOK_URL")
    if not webhook:
        return False
    embed: dict = {"title": title[:256], "description": message[:4000]}
    if url:
        embed["url"] = url
    files = None
    if file is not None:
        name, data, mime = _file_part(file)
        embed["image"] = {"url": f"attachment://{name}"}
        files = {"files[0]": (name, data, mime)}
    r = http.post(
        webhook,
        data={"payload_json": json.dumps({"embeds": [embed]}, ensure_ascii=False)},
        files=files,
    )
    r.raise_for_status()
    return True


def _telegram(
    http: httpx.Client, title: str, message: str, file: Path | None, url: str | None
) -> bool:
    token = get_trend_key("TELEGRAM_BOT_TOKEN")
    chat_id = get_trend_key("TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        return False
    text = _plain(title, message, url)
    base = f"https://api.telegram.org/bot{token}"
    if file is not None:
        name, data, mime = _file_part(file)
        r = http.post(
            f"{base}/sendPhoto",
            data={"chat_id": chat_id, "caption": text[:1024]},
            files={"photo": (name, data, mime)},
        )
    else:
        r = http.post(
            f"{base}/sendMessage",
            json={"chat_id": chat_id, "text": text[:4096], "disable_web_page_preview": True},
        )
    r.raise_for_status()
    return True


def _ntfy(http: httpx.Client, title: str, message: str, file: Path | None, url: str | None) -> bool:
    topic = get_trend_key("NTFY_TOPIC")
    if not topic:
        return False
    server = (get_trend_key("NTFY_SERVER") or NTFY_DEFAULT_SERVER).rstrip("/")
    body: dict = {"topic": topic, "title": title, "message": message or title}
    if url:
        body["click"] = url
    if file is not None:
        log.info("ntfy : la miniature n'est pas jointe (texte et lien seulement).")
    # Publication JSON à la racine du serveur : évite les en-têtes non ASCII.
    r = http.post(server + "/", json=body)
    r.raise_for_status()
    return True


CHANNELS: tuple[tuple[str, Callable[..., bool]], ...] = (
    ("discord", _discord),
    ("telegram", _telegram),
    ("ntfy", _ntfy),
)


def notify(
    title: str,
    message: str,
    *,
    file: str | Path | None = None,
    url: str | None = None,
    client: httpx.Client | None = None,
) -> list[str]:
    """Envoie à chaque canal configuré. Renvoie les noms des canaux joints. Ne lève jamais."""
    reached: list[str] = []
    try:
        own_client = client is None
        http = (
            client
            if client is not None
            else httpx.Client(timeout=TIMEOUT_S, headers={"User-Agent": f"minavio/{__version__}"})
        )
        try:
            thumb = _existing(file)
            for name, sender in CHANNELS:
                try:
                    if sender(http, title, message, thumb, url):
                        reached.append(name)
                except Exception as exc:  # un canal en échec ne bloque pas les autres
                    _warn(name, exc)
        finally:
            if own_client:
                http.close()
    except Exception as exc:
        log.warning("Notifications non envoyées : %s", type(exc).__name__)
    return reached
