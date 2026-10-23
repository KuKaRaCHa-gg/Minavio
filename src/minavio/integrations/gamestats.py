"""Statistiques de jeu : Apex Legends Status API et Riot (account-v1, LoL match-v5, Valorant).

Toutes les réponses sont mises en cache 1 h dans `work/.cache/stats/` (la clé API n'entre jamais
dans la clé de cache). Un champ absent de la réponse est omis, jamais inventé.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import BaseModel

from minavio.config.schema import ChannelProfile
from minavio.errors import MinavioError
from minavio.integrations.keys_stats import require_key

APEX_BASE = "https://api.mozambiquehe.re"
APEX_PLATFORMS = ("PC", "PS4", "X1")
RIOT_REGIONS = ("americas", "asia", "europe")
_VAL_CLUSTERS = {"europe": "eu", "americas": "na", "asia": "ap"}  # routage Valorant != routage Riot
CACHE_DIR = Path("work") / ".cache" / "stats"
CACHE_TTL_S = 3600
TIMEOUT_S = 10.0

# Injecté par les tests (httpx.MockTransport) ; None = réseau réel.
_TRANSPORT: httpx.BaseTransport | None = None

_RANKS_FR = {
    "Bronze": "Bronze",
    "Silver": "Argent",
    "Gold": "Or",
    "Platinum": "Platine",
    "Diamond": "Diamant",
    "Master": "Maître",
    "Apex Predator": "Prédateur",
}
_RANKS_WITHOUT_DIVISION = {"Master", "Apex Predator"}


class ApiError(MinavioError):
    """Erreur HTTP d'une API de statistiques ; `status` permet de dégrader proprement."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


class ApexPlayer(BaseModel):
    name: str
    platform: str
    level: int | None = None
    rank: str | None = None  # nom anglais de l'API (ex. "Diamond")
    division: int | None = None
    rp: int | None = None
    legend: str | None = None
    kills: int | None = None
    damage: int | None = None


class MapRotation(BaseModel):
    current_map: str | None = None
    next_map: str | None = None
    remaining: str | None = None


class RiotAccount(BaseModel):
    puuid: str
    game_name: str
    tag_line: str


class LastMatch(BaseModel):
    game: str
    champion: str | None = None
    kda: str | None = None
    win: bool | None = None


# --- HTTP et cache -----------------------------------------------------------------------


def _get(url: str, *, service: str, params: dict | None = None, headers: dict | None = None) -> Any:
    try:
        with httpx.Client(transport=_TRANSPORT, timeout=TIMEOUT_S) as client:
            resp = client.get(url, params=params, headers=headers)
    except httpx.HTTPError as exc:
        raise MinavioError(
            f"{service} injoignable ({type(exc).__name__}). Vérifie la connexion réseau."
        ) from exc
    code = resp.status_code
    if code in (401, 403):
        raise ApiError(
            f"{service} a refusé la clé (HTTP {code}). Vérifie la clé dans l'environnement "
            "ou dans secrets.env.",
            code,
        )
    if code == 404:
        raise ApiError(
            f"{service} : introuvable (HTTP 404). Vérifie le pseudo, la plateforme ou le tag.",
            code,
        )
    if code == 429:
        raise ApiError(
            f"{service} : quota dépassé (HTTP 429). Réessaie dans quelques minutes.", code
        )
    if code >= 400:
        raise ApiError(f"{service} : erreur HTTP {code}. Réessaie plus tard.", code)
    try:
        return resp.json()
    except ValueError as exc:
        raise MinavioError(f"{service} : réponse non JSON.") from exc


def _cached(key_parts: tuple, fetch: Callable[[], Any]) -> Any:
    digest = hashlib.sha256(json.dumps(key_parts, ensure_ascii=False).encode()).hexdigest()
    path = CACHE_DIR / f"{digest[:32]}.json"
    now = time.time()
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
        if now - float(entry["ts"]) < CACHE_TTL_S:
            return entry["data"]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    data = fetch()  # les erreurs ne sont pas mises en cache
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"ts": now, "data": data}, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass  # cache best effort
    return data


# --- Helpers de parsing --------------------------------------------------------------------


def _dig(data: Any, *path: str | int) -> Any:
    cur = data
    for key in path:
        if isinstance(cur, dict):
            cur = cur.get(key)
        elif isinstance(cur, list) and isinstance(key, int) and 0 <= key < len(cur):
            cur = cur[key]
        else:
            return None
        if cur is None:
            return None
    return cur


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value.replace(",", "").strip().isdigit():
        return int(value.replace(",", "").strip())
    return None


def _str(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _fmt(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def _apex_platform(platform: str) -> str:
    p = platform.strip().upper()
    if p not in APEX_PLATFORMS:
        raise MinavioError(
            f"Plateforme Apex inconnue : {platform}. Valeurs : {', '.join(APEX_PLATFORMS)}."
        )
    return p


def _riot_host(region: str) -> str:
    r = region.strip().lower()
    if r not in RIOT_REGIONS:
        raise MinavioError(f"Région Riot inconnue : {region}. Valeurs : {', '.join(RIOT_REGIONS)}.")
    return r


# --- Apex Legends --------------------------------------------------------------------------


def apex_player(name: str, platform: str = "PC") -> ApexPlayer:
    key = require_key("APEX_API_KEY")
    plat = _apex_platform(platform)
    player = name.strip()
    if not player:
        raise MinavioError("Pseudo Apex vide.")
    data = _cached(
        ("apex-player", plat, player.lower()),
        lambda: _get(
            f"{APEX_BASE}/bridge",
            service="Apex",
            params={"player": player, "platform": plat},
            headers={"Authorization": key},
        ),
    )
    if not isinstance(data, dict) or data.get("Error"):
        detail = data.get("Error") if isinstance(data, dict) else None
        raise MinavioError(
            f"Apex : joueur « {player} » introuvable sur {plat}"
            + (f" ({detail})" if detail else "")
            + "."
        )
    damage = _int(_dig(data, "total", "damage", "value"))
    if damage is None:
        damage = _int(_dig(data, "total", "damageDone", "value"))
    return ApexPlayer(
        name=_str(_dig(data, "global", "name")) or player,
        platform=plat,
        level=_int(_dig(data, "global", "level")),
        rank=_str(_dig(data, "global", "rank", "rankName")),
        division=_int(_dig(data, "global", "rank", "rankDiv")),
        rp=_int(_dig(data, "global", "rank", "rankScore")),
        legend=_str(_dig(data, "legends", "selected", "LegendName")),
        kills=_int(_dig(data, "total", "kills", "value")),
        damage=damage,
    )


def apex_map_rotation() -> MapRotation:
    key = require_key("APEX_API_KEY")
    data = _cached(
        ("apex-maprotation",),
        lambda: _get(
            f"{APEX_BASE}/maprotation",
            service="Apex",
            params={"version": 2},
            headers={"Authorization": key},
        ),
    )
    return MapRotation(
        current_map=_str(_dig(data, "battle_royale", "current", "map")),
        next_map=_str(_dig(data, "battle_royale", "next", "map")),
        remaining=_str(_dig(data, "battle_royale", "current", "remainingTimer")),
    )


def _rank_text(p: ApexPlayer) -> str | None:
    if p.rank is None:
        return None
    label = _RANKS_FR.get(p.rank, p.rank)
    if p.division and p.rank not in _RANKS_WITHOUT_DIVISION:
        label = f"{label} {p.division}"
    return label


def apex_lines(p: ApexPlayer) -> list[str]:
    """Lignes de texte factuel ; seuls les champs présents sont listés."""
    lines = [f"Joueur Apex : {p.name} ({p.platform})"]
    if p.level is not None:
        lines.append(f"Niveau : {p.level}")
    rank_parts = []
    if (rank := _rank_text(p)) is not None:
        rank_parts.append(f"Rang : {rank}")
    if p.rp is not None:
        rank_parts.append(f"{_fmt(p.rp)} RP")
    if p.legend is not None:
        rank_parts.append(f"légende : {p.legend}")
    if rank_parts:
        lines.append(", ".join(rank_parts))
    elif p.legend is not None:
        lines.append(f"Légende : {p.legend}")
    if p.kills is not None:
        lines.append(f"Kills : {_fmt(p.kills)}")
    if p.damage is not None:
        lines.append(f"Dégâts infligés : {_fmt(p.damage)}")
    return lines


def stats_context(profile: ChannelProfile, player: str, platform: str = "PC") -> str:
    """Bloc de texte factuel pour la copy (aucune valeur inventée)."""
    p = apex_player(player, platform)
    return "\n".join([f"Chaîne : {profile.display_name}", *apex_lines(p)])


# --- Riot ----------------------------------------------------------------------------------


def riot_account(game_name: str, tag_line: str, region: str = "europe") -> RiotAccount:
    key = require_key("RIOT_API_KEY")
    host = _riot_host(region)
    name, tag = game_name.strip(), tag_line.strip().lstrip("#")
    if not name or not tag:
        raise MinavioError("Riot ID incomplet : format attendu Pseudo#TAG (ex. Faker#KR1).")
    url = (
        f"https://{host}.api.riotgames.com/riot/account/v1/accounts/by-riot-id/"
        f"{quote(name, safe='')}/{quote(tag, safe='')}"
    )
    data = _cached(
        ("riot-account", host, name.lower(), tag.lower()),
        lambda: _get(url, service="Riot", headers={"X-Riot-Token": key}),
    )
    puuid = _str(_dig(data, "puuid"))
    if puuid is None:
        raise MinavioError("Riot : réponse account-v1 sans puuid.")
    return RiotAccount(
        puuid=puuid,
        game_name=_str(_dig(data, "gameName")) or name,
        tag_line=_str(_dig(data, "tagLine")) or tag,
    )


def riot_last_lol_match(puuid: str, region: str = "europe") -> LastMatch | None:
    key = require_key("RIOT_API_KEY")
    host = _riot_host(region)
    base = f"https://{host}.api.riotgames.com/lol/match/v5/matches"
    headers = {"X-Riot-Token": key}
    ids = _cached(
        ("lol-ids", host, puuid),
        lambda: _get(
            f"{base}/by-puuid/{quote(puuid, safe='')}/ids",
            service="Riot",
            params={"count": 1},
            headers=headers,
        ),
    )
    if not isinstance(ids, list) or not ids:
        return None
    match = _cached(
        ("lol-match", host, str(ids[0])),
        lambda: _get(f"{base}/{ids[0]}", service="Riot", headers=headers),
    )
    participants = _dig(match, "info", "participants")
    me = next(
        (p for p in participants or [] if isinstance(p, dict) and p.get("puuid") == puuid), None
    )
    if me is None:
        return None
    k, d, a = _int(me.get("kills")), _int(me.get("deaths")), _int(me.get("assists"))
    kda = f"{k}/{d}/{a}" if None not in (k, d, a) else None
    win = me.get("win") if isinstance(me.get("win"), bool) else None
    return LastMatch(
        game="League of Legends", champion=_str(me.get("championName")), kda=kda, win=win
    )


def riot_valorant_last_match_id(puuid: str, region: str = "europe") -> str | None:
    """Id du dernier match Valorant, ou None si la clé ne donne pas accès à l'API match.

    L'API match Valorant est réservée aux clés de production : une clé de développement
    (401/403) dégrade en None au lieu de lever une erreur.
    """
    key = require_key("RIOT_API_KEY")
    host = _riot_host(region)
    cluster = _VAL_CLUSTERS[host]
    try:
        data = _cached(
            ("val-matchlist", cluster, puuid),
            lambda: _get(
                f"https://{cluster}.api.riotgames.com/val/match/v1/matchlists/"
                f"by-puuid/{quote(puuid, safe='')}",
                service="Riot Valorant",
                headers={"X-Riot-Token": key},
            ),
        )
    except ApiError as exc:
        if exc.status in (401, 403):
            return None
        raise
    history = _dig(data, "history")
    if isinstance(history, list) and history and isinstance(history[0], dict):
        return _str(history[0].get("matchId"))
    return None
