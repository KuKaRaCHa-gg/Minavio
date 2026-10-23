"""Veille de tendances : sources gratuites (Reddit, Hacker News, Steam, Twitch), cache 6 h.

`trend_report` produit un Markdown avec les éléments bruts. Si le LLM local est joignable, il
ajoute cinq idées de vidéos, construites uniquement à partir de ces éléments. Une source en échec
ou une clé absente donne une note dans le rapport, jamais une exception.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

import httpx

from minavio import __version__
from minavio.config.schema import ChannelProfile
from minavio.errors import MinavioError
from minavio.integrations.keys_trends import get_trend_key
from minavio.llm.prompts import load_prompt

log = logging.getLogger("minavio.trends")

TIMEOUT_S = 15.0
CACHE_TTL_S = 6 * 3600
DEFAULT_CACHE_DIR = Path("work") / ".cache" / "trends"
USER_AGENT = f"minavio/{__version__}"
CLIP_RIGHTS_NOTE = "Droits des clips : ceux du streamer. Demande sa permission avant de republier."
REDDIT_HINT = (
    "Reddit a refusé la requête (403/429) : configure REDDIT_CLIENT_ID/SECRET "
    "(app OAuth, optionnel ; voir docs/GROWTH.md)."
)
TWITCH_HINT = (
    "clés Twitch manquantes : configure TWITCH_CLIENT_ID et TWITCH_CLIENT_SECRET "
    "(dev.twitch.tv ; voir docs/GROWTH.md)."
)

# Sources par profil (motif fnmatch sur l'id du profil). Modifiable à l'exécution.
DEFAULT_SOURCES: dict[str, list[str]] = {
    "minivitch*": ["reddit:apexlegends", "reddit:gaming", "steam", "twitch:clips:Apex Legends"],
    "bytetricks": ["hn", "reddit:technology", "reddit:todayilearned"],
    "thelostchat": ["twitch:top", "twitch:clips:Just Chatting", "reddit:LivestreamFail"],
}

HN_TOP = "https://hacker-news.firebaseio.com/v0/topstories.json"
HN_ITEM = "https://hacker-news.firebaseio.com/v0/item/{id}.json"
STEAM_MOST_PLAYED = "https://api.steampowered.com/ISteamChartsService/GetMostPlayedGames/v1/"
STEAM_PLAYERS = "https://api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/"
# Repli si GetMostPlayedGames échoue : noms connus, joueurs actuels (pas de tendance).
STEAM_FALLBACK_APPIDS: dict[int, str] = {
    1172470: "Apex Legends",
    730: "Counter-Strike 2",
    570: "Dota 2",
    1085660: "Destiny 2",
}
REDDIT_PUBLIC = "https://www.reddit.com"
REDDIT_OAUTH = "https://oauth.reddit.com"
REDDIT_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
TWITCH_API = "https://api.twitch.tv/helix"

_EXPECTED_ERRORS = (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError)


@dataclass
class Item:
    title: str
    url: str
    score: float | None = None
    detail: str = ""


@dataclass
class SourceResult:
    source: str
    items: list[Item] = field(default_factory=list)
    note: str | None = None


class SourceUnavailable(Exception):
    """Source non exploitable : le message sert de note dans le rapport."""


class _Chat(Protocol):
    def chat(self, messages: list[dict[str, str]], images: list[Path] | None = None) -> str: ...


def _new_client() -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT_S, headers={"User-Agent": USER_AGENT})


def _get_json(client: httpx.Client, url: str, **kwargs: Any) -> Any:
    r = client.get(url, **kwargs)
    r.raise_for_status()
    return r.json()


# ----- sources ---------------------------------------------------------------


def _reddit_headers(client: httpx.Client) -> dict[str, str]:
    """Jeton app-only (client_credentials) si les deux clés sont présentes, sinon accès public."""
    cid = get_trend_key("REDDIT_CLIENT_ID")
    secret = get_trend_key("REDDIT_CLIENT_SECRET")
    if not (cid and secret):
        return {}
    r = client.post(
        REDDIT_TOKEN_URL,
        auth=(cid, secret),
        data={"grant_type": "client_credentials"},
    )
    if r.status_code in (401, 403, 429):
        raise SourceUnavailable(REDDIT_HINT)
    r.raise_for_status()
    return {"Authorization": f"bearer {r.json()['access_token']}"}


def _reddit(client: httpx.Client, sub: str) -> tuple[list[Item], str | None]:
    headers = _reddit_headers(client)
    if headers:
        url, params = f"{REDDIT_OAUTH}/r/{sub}/top", {"t": "day", "limit": 25}
    else:
        url, params = f"{REDDIT_PUBLIC}/r/{sub}/top.json", {"t": "day", "limit": 25}
    r = client.get(url, params=params, headers=headers)
    if r.status_code in (403, 429):
        raise SourceUnavailable(REDDIT_HINT)
    r.raise_for_status()
    items = []
    for child in r.json()["data"]["children"]:
        d = child["data"]
        items.append(
            Item(
                title=d["title"],
                url=REDDIT_PUBLIC + d["permalink"],
                score=d.get("score"),
                detail=f"{d.get('num_comments', 0)} commentaires",
            )
        )
    return items, None


def _hn(client: httpx.Client, _arg: str | None = None) -> tuple[list[Item], str | None]:
    ids = _get_json(client, HN_TOP)[:30]
    items = []
    for story_id in ids:
        d = _get_json(client, HN_ITEM.format(id=story_id))
        if not d or "title" not in d:
            continue
        items.append(
            Item(
                title=d["title"],
                url=d.get("url") or f"https://news.ycombinator.com/item?id={story_id}",
                score=d.get("score"),
                detail=f"{d.get('descendants', 0)} commentaires",
            )
        )
    return items, None


def _steam_name(appid: int) -> str:
    return STEAM_FALLBACK_APPIDS.get(appid, f"appid {appid}")


def _steam_store_url(appid: int) -> str:
    return f"https://store.steampowered.com/app/{appid}"


def _steam_fallback(client: httpx.Client) -> list[Item]:
    items = []
    for appid, name in STEAM_FALLBACK_APPIDS.items():
        try:
            data = _get_json(client, STEAM_PLAYERS, params={"appid": appid})
            count = data["response"]["player_count"]
        except _EXPECTED_ERRORS:
            continue
        items.append(
            Item(title=name, url=_steam_store_url(appid), score=count, detail="joueurs actuels")
        )
    if not items:
        raise SourceUnavailable(
            "Steam injoignable (ni GetMostPlayedGames, ni GetNumberOfCurrentPlayers)."
        )
    return sorted(items, key=lambda i: i.score or 0, reverse=True)


def _steam(client: httpx.Client, _arg: str | None = None) -> tuple[list[Item], str | None]:
    try:
        ranks = _get_json(client, STEAM_MOST_PLAYED)["response"]["ranks"]
    except _EXPECTED_ERRORS:
        items = _steam_fallback(client)
        return (
            items,
            "GetMostPlayedGames indisponible : repli sur les joueurs actuels, sans tendance.",
        )
    rising = []
    for r in ranks:
        last = r.get("last_week_rank", -1)
        if last and last > 0 and last > r["rank"]:
            rising.append((last - r["rank"], r))
    rising.sort(key=lambda t: t[0], reverse=True)
    items = [
        Item(
            title=_steam_name(r["appid"]),
            url=_steam_store_url(r["appid"]),
            score=r.get("peak_in_game"),
            detail=f"rang {r['rank']} (+{gain} places depuis la semaine dernière)",
        )
        for gain, r in rising[:15]
    ]
    note = None if items else "Aucun jeu en hausse dans le classement Steam ce jour-ci."
    return items, note


def _twitch_headers(client: httpx.Client) -> dict[str, str]:
    cid = get_trend_key("TWITCH_CLIENT_ID")
    secret = get_trend_key("TWITCH_CLIENT_SECRET")
    if not (cid and secret):
        raise SourceUnavailable(TWITCH_HINT)
    r = client.post(
        TWITCH_TOKEN_URL,
        data={"client_id": cid, "client_secret": secret, "grant_type": "client_credentials"},
    )
    r.raise_for_status()
    return {"Client-Id": cid, "Authorization": f"Bearer {r.json()['access_token']}"}


def _twitch_get(client: httpx.Client, headers: dict[str, str], path: str, params: dict) -> list:
    data = _get_json(client, f"{TWITCH_API}/{path}", params=params, headers=headers)
    return list(data.get("data", []))


def _twitch_top(client: httpx.Client, _arg: str | None = None) -> tuple[list[Item], str | None]:
    headers = _twitch_headers(client)
    games = _twitch_get(client, headers, "games/top", {"first": 10})
    items = [
        Item(
            title=g["name"],
            url=f"https://www.twitch.tv/directory/game/{quote(g['name'])}",
            detail="jeu le plus regardé (Twitch)",
        )
        for g in games
    ]
    return items, None


def _twitch_clips(
    client: httpx.Client, *, game: str | None = None, login: str | None = None
) -> tuple[list[Item], str | None]:
    """Top clips des 7 derniers jours, pour un jeu ou un streamer."""
    headers = _twitch_headers(client)
    if game:
        found = _twitch_get(client, headers, "games", {"name": game})
        if not found:
            raise SourceUnavailable(f"jeu Twitch introuvable : {game}")
        scope = {"game_id": found[0]["id"]}
    else:
        found = _twitch_get(client, headers, "users", {"login": login})
        if not found:
            raise SourceUnavailable(f"streamer Twitch introuvable : {login}")
        scope = {"broadcaster_id": found[0]["id"]}
    started = (datetime.now(UTC) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    clips = _twitch_get(client, headers, "clips", {**scope, "started_at": started, "first": 20})
    clips.sort(key=lambda c: c.get("view_count", 0), reverse=True)
    items = [
        Item(
            title=c["title"],
            url=c["url"],
            score=c.get("view_count"),
            detail=f"vues · streamer : {c.get('broadcaster_name', '?')}",
        )
        for c in clips[:15]
    ]
    return items, None


def _dispatch(source: str, client: httpx.Client) -> tuple[list[Item], str | None]:
    if source.startswith("reddit:"):
        return _reddit(client, source.removeprefix("reddit:"))
    if source == "hn":
        return _hn(client)
    if source == "steam":
        return _steam(client)
    if source == "twitch:top":
        return _twitch_top(client)
    if source.startswith("twitch:clips:"):
        return _twitch_clips(client, game=source.removeprefix("twitch:clips:"))
    if source.startswith("twitch:streamer:"):
        return _twitch_clips(client, login=source.removeprefix("twitch:streamer:"))
    raise SourceUnavailable(f"source inconnue : {source}")


# ----- cache (6 h, un fichier JSON par source) -------------------------------


def _cache_file(cache_dir: Path, source: str) -> Path:
    return cache_dir / (re.sub(r"[^A-Za-z0-9._-]+", "_", source) + ".json")


def _read_cache(path: Path, now: float) -> tuple[list[Item], str | None] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    try:
        if now - float(data["at"]) > CACHE_TTL_S:
            return None
        return [Item(**i) for i in data["items"]], data.get("note")
    except (KeyError, TypeError, ValueError):
        return None


def _write_cache(path: Path, items: list[Item], note: str | None, now: float) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"at": now, "note": note, "items": [asdict(i) for i in items]}
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        log.warning("Cache des tendances non écrit (%s) : %s", path, e)


def _fetch(source: str, client: httpx.Client, cache_dir: Path | None, now: float) -> SourceResult:
    path = _cache_file(cache_dir, source) if cache_dir is not None else None
    if path is not None:
        cached = _read_cache(path, now)
        if cached is not None:
            return SourceResult(source, cached[0], cached[1])
    try:
        items, note = _dispatch(source, client)
    except SourceUnavailable as e:
        return SourceResult(source, [], str(e))
    except _EXPECTED_ERRORS as e:
        log.warning("Source %s en échec (%s).", source, type(e).__name__)
        return SourceResult(
            source, [], f"source indisponible ({type(e).__name__}), réessaie plus tard."
        )
    if path is not None and items:
        _write_cache(path, items, note, now)
    return SourceResult(source, items, note)


# ----- rapport ---------------------------------------------------------------


def sources_for(profile: ChannelProfile) -> list[str]:
    """Sources par défaut du profil (premier motif de DEFAULT_SOURCES qui correspond)."""
    for pattern, ids in DEFAULT_SOURCES.items():
        if fnmatch.fnmatch(profile.id, pattern):
            return list(ids)
    return []


def _label(source: str) -> str:
    if source.startswith("reddit:"):
        return f"r/{source.removeprefix('reddit:')} (Reddit)"
    if source == "hn":
        return "Hacker News (top 30)"
    if source == "steam":
        return "Steam : jeux en hausse"
    if source == "twitch:top":
        return "Twitch : jeux les plus regardés"
    if source.startswith("twitch:clips:"):
        return f"Twitch : clips 7 j, {source.removeprefix('twitch:clips:')}"
    if source.startswith("twitch:streamer:"):
        return f"Twitch : clips 7 j du streamer {source.removeprefix('twitch:streamer:')}"
    return source


def _fmt_score(score: float) -> str:
    return f"{int(score):,}" if float(score).is_integer() else f"{score:,.2f}"


def _item_line(it: Item) -> str:
    title = it.title.replace("[", "(").replace("]", ")").replace("\n", " ").strip()
    parts = []
    if it.score is not None:
        parts.append(f"score {_fmt_score(it.score)}")
    if it.detail:
        parts.append(it.detail)
    tail = f" — {' · '.join(parts)}" if parts else ""
    return f"- [{title}]({it.url}){tail}"


def _llm_ideas(
    profile: ChannelProfile, results: list[SourceResult], llm: _Chat | None
) -> tuple[str | None, str | None]:
    """(idées, note). Le LLM n'est appelé que s'il reste des données à commenter."""
    payload = [
        {"source": r.source, "titre": i.title, "score": i.score, "detail": i.detail}
        for r in results
        for i in r.items[:15]
    ]
    if not payload:
        return None, "Aucune donnée de tendance : pas d'idées générées."
    if llm is None and not profile.llm.enabled:
        return None, "LLM désactivé dans le profil : pas d'idées générées."
    try:
        if llm is None:
            from minavio.llm.client import LocalLLM

            llm = LocalLLM.from_settings(profile.llm)
        messages = [
            {"role": "system", "content": load_prompt("trends")},
            {
                "role": "user",
                "content": (
                    f"Chaîne : {profile.display_name} ({profile.content_type}). "
                    f"{profile.description}\nTendances (JSON) :\n"
                    + json.dumps(payload, ensure_ascii=False)
                ),
            },
        ]
        text = llm.chat(messages).strip()
    except MinavioError as e:
        return None, f"LLM local indisponible : {e}"
    except _EXPECTED_ERRORS as e:
        return None, f"LLM local indisponible ({type(e).__name__}) : pas d'idées générées."
    if not text:
        return None, "Réponse LLM vide : pas d'idées générées."
    return text, None


def _render(
    profile: ChannelProfile,
    results: list[SourceResult],
    ideas: str | None,
    ideas_note: str | None,
    now: float,
) -> str:
    when = datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"# Tendances : {profile.display_name} ({profile.id})",
        "",
        f"_Généré le {when} · sources publiques · cache {CACHE_TTL_S // 3600} h_",
        "",
    ]
    if not results:
        lines += ["> Aucune source par défaut pour ce profil (voir `DEFAULT_SOURCES`).", ""]
    for r in results:
        lines.append(f"## {_label(r.source)}")
        lines += [_item_line(it) for it in r.items]
        if not r.items and not r.note:
            lines.append("_Aucun élément._")
        if r.note:
            lines.append(f"> Note : {r.note}")
        if r.source.startswith(("twitch:clips:", "twitch:streamer:")):
            lines.append(f"> {CLIP_RIGHTS_NOTE}")
        lines.append("")
    lines.append("## Idées de vidéos (LLM local)")
    lines.append("")
    lines.append(ideas if ideas else f"> {ideas_note}")
    return "\n".join(lines).rstrip() + "\n"


def trend_report(
    profile: ChannelProfile,
    llm: _Chat | None = None,
    *,
    client: httpx.Client | None = None,
    cache_dir: Path | None = DEFAULT_CACHE_DIR,
    sources: list[str] | None = None,
    now: float | None = None,
) -> str:
    """Rapport Markdown de tendances pour un profil. Ne lève pas pour une source en échec.

    `llm` : objet avec `chat(messages)` (LLM local par défaut, via `profile.llm`).
    `cache_dir=None` désactive le cache ; `client` permet d'injecter un httpx.MockTransport.
    """
    stamp = time.time() if now is None else now
    ids = list(sources) if sources is not None else sources_for(profile)
    own_client = client is None
    http = client if client is not None else _new_client()
    try:
        results = [_fetch(s, http, cache_dir, stamp) for s in ids]
    finally:
        if own_client:
            http.close()
    ideas, ideas_note = _llm_ideas(profile, results, llm)
    return _render(profile, results, ideas, ideas_note, stamp)
