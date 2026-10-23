"""Recherche de niche : quelles vidéos marchent sur une requête (YouTube Data API v3, clé d'API).

Chaîne d'appels : `search.list` (100 unités) -> `videos.list` (1) -> `channels.list` (1).
Quota : compteur quotidien persistant (`yt_quota.json`, budget configurable, 3 000 par défaut),
cache disque 24 h des réponses (`work/.cache/yt/`) : un cache valide ne consomme aucune unité.
Scores : `outlier = vues / max(abonnés, 100)`, `vph = vues / heures depuis la publication`.
Les motifs sont calculés localement ; le LLM ne propose que des idées, à partir des titres.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import logging
import math
import os
import re
import statistics
import time
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, Field

from minavio.config.secrets import secrets_path
from minavio.errors import ConfigError, MinavioError
from minavio.integrations.common import ask_llm, fmt_dec, fmt_int, llm_for_profile, md_table
from minavio.integrations.keys import get_key

log = logging.getLogger("minavio.integrations")

API_BASE = "https://www.googleapis.com/youtube/v3"
SEARCH_UNITS = 100
LIST_UNITS = 1
PAGE_MAX = 50
MAX_RESULTS_CAP = 200
CACHE_TTL_S = 24 * 3600
DEFAULT_BUDGET = 3000
BUDGET_ENV = "MINAVIO_YT_QUOTA_BUDGET"
CACHE_DIR = Path("work/.cache/yt")
SHORT_MAX_S = 60
OUTLIER_FLOOR = 100
TOP_SHARE = 0.2
_DURATION = re.compile(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?")
_EMOJI = re.compile("[\U0001f000-\U0001faff☀-➿]")
_CAPS = re.compile(r"\b[A-ZÀ-Ý]{3,}\b")
_JOURS = ("lun", "mar", "mer", "jeu", "ven", "sam", "dim")
_STOP = frozenset(
    {
        "le",
        "la",
        "les",
        "un",
        "une",
        "des",
        "du",
        "de",
        "et",
        "en",
        "au",
        "aux",
        "pour",
        "par",
        "sur",
        "dans",
        "avec",
        "qui",
        "que",
        "est",
        "sont",
        "ce",
        "ces",
        "il",
        "elle",
        "je",
        "tu",
        "on",
        "nous",
        "vous",
        "ils",
        "the",
        "and",
        "of",
        "to",
        "in",
        "is",
        "it",
        "for",
        "with",
        "you",
        "my",
        "this",
        "that",
        "mais",
        "ou",
        "donc",
        "car",
        "ne",
        "pas",
        "plus",
        "tout",
        "tous",
        "cette",
        "comme",
    }
)


class QuotaExceededError(MinavioError):
    """Budget quotidien du compteur local atteint : aucune requête n'est envoyée."""


class YouTubeAPIError(MinavioError):
    """Réponse HTTP non valide de l'API YouTube (message sans la clé ni le corps brut)."""


# ----------------------------------------------------------------------------- quota


def quota_path() -> Path:
    """`~/.config/minavio/yt_quota.json` (suit `MINAVIO_SECRETS_FILE` comme les secrets)."""
    return secrets_path().parent / "yt_quota.json"


def budget_from_env() -> int:
    raw = os.environ.get(BUDGET_ENV)
    if not raw:
        return DEFAULT_BUDGET
    try:
        return int(raw)
    except ValueError as e:
        raise ConfigError(f"{BUDGET_ENV}='{raw}' : un entier est attendu (ex. 3000).") from e


def _quota_day() -> str:
    """Le quota Google se réinitialise à minuit, heure du Pacifique."""
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()
    except (ImportError, KeyError):
        return datetime.now(UTC).date().isoformat()


class QuotaLedger:
    """Compteur d'unités consommées aujourd'hui, persisté en JSON (écriture atomique)."""

    def __init__(self, path: Path | None = None, budget: int | None = None) -> None:
        self.path = path or quota_path()
        self.budget = budget if budget is not None else budget_from_env()

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def used(self) -> int:
        data = self._read()
        if data.get("date") != _quota_day():
            return 0
        try:
            return int(data.get("units", 0))
        except (TypeError, ValueError):
            return 0

    def spend(self, units: int) -> None:
        used = self.used()
        if used + units > self.budget:
            raise QuotaExceededError(
                f"Budget quota YouTube atteint : {used}/{self.budget} unités aujourd'hui "
                f"(+{units} demandées). Attends demain (reset à minuit, heure du Pacifique), "
                f"ou relève {BUDGET_ENV} (plafond Google : 10 000 unités/jour)."
            )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"date": _quota_day(), "units": used + units}), encoding="utf-8")
        tmp.replace(self.path)


# ----------------------------------------------------------------------------- cache


class ResponseCache:
    """Réponses brutes de l'API sur disque, valides `ttl_s` secondes. Best effort."""

    def __init__(
        self,
        directory: Path = CACHE_DIR,
        *,
        ttl_s: float = CACHE_TTL_S,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.dir = directory
        self.ttl_s = ttl_s
        self.clock = clock

    @staticmethod
    def key(resource: str, params: dict[str, Any]) -> str:
        raw = json.dumps([resource, sorted(params.items())], sort_keys=True, default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def get(self, key: str) -> dict[str, Any] | None:
        try:
            data = json.loads((self.dir / f"{key}.json").read_text(encoding="utf-8"))
            if self.clock() - float(data["ts"]) <= self.ttl_s:
                return data["body"]
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return None

    def put(self, key: str, body: dict[str, Any]) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            payload = json.dumps({"ts": self.clock(), "body": body}, ensure_ascii=False)
            (self.dir / f"{key}.json").write_text(payload, encoding="utf-8")
        except OSError as e:
            log.warning("cache YouTube non écrit (%s) : requête refaite la prochaine fois.", e)


# ----------------------------------------------------------------------------- client HTTP


def _api_message(resource: str, resp: httpx.Response) -> str:
    """Message actionnable ; ne reprend ni la clé ni le corps brut de la réponse."""
    reason = ""
    try:
        err = resp.json().get("error", {})
        reason = str((err.get("errors") or [{}])[0].get("reason") or err.get("status") or "")
    except (ValueError, AttributeError, TypeError, IndexError):
        pass
    hint = ""
    if reason in ("quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"):
        hint = " Quota Google épuisé : réessaie après minuit (heure du Pacifique)."
    elif resp.status_code in (400, 403) and reason in (
        "keyInvalid",
        "accessNotConfigured",
        "forbidden",
        "ipRefererBlocked",
        "badRequest",
    ):
        hint = " Vérifie YOUTUBE_API_KEY et que l'API YouTube Data v3 est activée (docs/GROWTH.md)."
    detail = reason or "sans raison"
    return f"YouTube « {resource} » a refusé la requête (HTTP {resp.status_code}, {detail}).{hint}"


class YouTubeClient:
    """Appels GET à l'API avec quota et cache. `transport` permet les tests (`MockTransport`)."""

    def __init__(
        self,
        api_key: str,
        *,
        transport: httpx.BaseTransport | None = None,
        cache: ResponseCache | None = None,
        ledger: QuotaLedger | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._key = api_key
        self._transport = transport
        self.cache = cache if cache is not None else ResponseCache(CACHE_DIR)
        self.ledger = ledger if ledger is not None else QuotaLedger()
        self.clock = clock or (lambda: datetime.now(UTC))

    def get(self, resource: str, params: dict[str, Any], units: int) -> dict[str, Any]:
        ck = self.cache.key(resource, params)  # la clé API n'entre jamais dans la clé de cache
        hit = self.cache.get(ck)
        if hit is not None:
            return hit
        self.ledger.spend(units)  # refus avant envoi si le budget est atteint
        try:
            with httpx.Client(transport=self._transport, timeout=20.0) as http:
                resp = http.get(f"{API_BASE}/{resource}", params={**params, "key": self._key})
        except httpx.HTTPError as e:
            raise MinavioError(
                f"Appel YouTube « {resource} » impossible ({type(e).__name__}) : "
                "vérifie la connexion."
            ) from e
        if resp.status_code != 200:
            raise YouTubeAPIError(_api_message(resource, resp))
        try:
            body = resp.json()
        except ValueError as e:
            raise YouTubeAPIError(f"Réponse YouTube « {resource} » non JSON.") from e
        self.cache.put(ck, body)
        return body


# ----------------------------------------------------------------------------- modèle


@dataclass(frozen=True)
class Video:
    id: str
    title: str
    channel_id: str
    channel_title: str
    published_at: datetime
    duration_s: int
    views: int
    likes: int
    comments: int
    tags: tuple[str, ...]
    subs: int | None  # None = abonnés masqués ou absents
    query: str
    hours: float
    vph: float
    outlier: float

    @property
    def is_short(self) -> bool:
        return 0 < self.duration_s <= SHORT_MAX_S

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.id}"


def parse_duration(iso: str) -> int:
    """`PT1M30S` -> 90. Format inconnu -> 0."""
    m = _DURATION.fullmatch(iso or "")
    if not m:
        return 0
    days, hours, minutes, seconds = (int(x) if x else 0 for x in m.groups())
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def _parse_ts(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _chunks(seq: Sequence[str], size: int) -> Iterator[list[str]]:
    for i in range(0, len(seq), size):
        yield list(seq[i : i + size])


def _build_video(
    item: dict[str, Any], query: str, subs: dict[str, int | None], now: datetime
) -> Video:
    snip = item.get("snippet") or {}
    stats = item.get("statistics") or {}
    content = item.get("contentDetails") or {}
    published = _parse_ts(str(snip.get("publishedAt", "")))
    hours = max((now - published).total_seconds() / 3600, 1.0)
    views = _int(stats.get("viewCount"))
    channel_id = str(snip.get("channelId", ""))
    sub_count = subs.get(channel_id)
    return Video(
        id=str(item.get("id", "")),
        title=str(snip.get("title", "")),
        channel_id=channel_id,
        channel_title=str(snip.get("channelTitle", "")),
        published_at=published,
        duration_s=parse_duration(str(content.get("duration", ""))),
        views=views,
        likes=_int(stats.get("likeCount")),
        comments=_int(stats.get("commentCount")),
        tags=tuple(str(t) for t in snip.get("tags") or []),
        subs=sub_count,
        query=query,
        hours=hours,
        vph=views / hours,
        outlier=views / max(sub_count or 0, OUTLIER_FLOOR),
    )


# ----------------------------------------------------------------------------- recherche


def _search_ids(
    client: YouTubeClient,
    query: str,
    *,
    published_after: str,
    max_results: int,
    region: str,
    lang: str,
    video_duration: str | None,
) -> list[str]:
    ids: list[str] = []
    token: str | None = None
    while len(ids) < max_results:
        params: dict[str, Any] = {
            "part": "snippet",
            "type": "video",
            "q": query,
            "order": "viewCount",
            "publishedAfter": published_after,
            "regionCode": region,
            "relevanceLanguage": lang,
            "maxResults": min(PAGE_MAX, max_results - len(ids)),
        }
        if video_duration:
            params["videoDuration"] = video_duration
        if token:
            params["pageToken"] = token
        body = client.get("search", params, SEARCH_UNITS)
        ids += [
            str(it["id"]["videoId"])
            for it in body.get("items", [])
            if (it.get("id") or {}).get("videoId")
        ]
        token = body.get("nextPageToken")
        if not token:
            break
    return ids[:max_results]


def _channel_subs(client: YouTubeClient, ids: Sequence[str]) -> dict[str, int | None]:
    out: dict[str, int | None] = {}
    for chunk in _chunks(sorted(ids), PAGE_MAX):
        body = client.get("channels", {"part": "statistics", "id": ",".join(chunk)}, LIST_UNITS)
        for it in body.get("items", []):
            stats = it.get("statistics") or {}
            hidden = bool(stats.get("hiddenSubscriberCount")) or "subscriberCount" not in stats
            out[str(it.get("id", ""))] = None if hidden else _int(stats["subscriberCount"])
    return out


def search_niche(
    queries: Sequence[str],
    *,
    days: int = 30,
    max_results: int = 50,
    region: str = "FR",
    lang: str = "fr",
    video_duration: str | None = None,
    client: YouTubeClient | None = None,
) -> list[Video]:
    """Vidéos les plus vues par requête, publiées depuis `days` jours (dédoublonnées)."""
    terms = [q.strip() for q in queries if q and q.strip()]
    if not terms:
        raise ConfigError('Au moins une requête de niche est nécessaire (ex. "minecraft survie").')
    if not 1 <= max_results <= MAX_RESULTS_CAP:
        raise ConfigError(f"max_results doit être entre 1 et {MAX_RESULTS_CAP}.")
    client = client or YouTubeClient(get_key("YOUTUBE_API_KEY"))
    now = client.clock()
    published_after = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")

    first_query: dict[str, str] = {}
    for term in terms:
        for vid in _search_ids(
            client,
            term,
            published_after=published_after,
            max_results=max_results,
            region=region,
            lang=lang,
            video_duration=video_duration,
        ):
            first_query.setdefault(vid, term)
    if not first_query:
        return []

    ids = list(first_query)
    items: list[dict[str, Any]] = []
    for chunk in _chunks(ids, PAGE_MAX):
        body = client.get(
            "videos",
            {"part": "snippet,statistics,contentDetails", "id": ",".join(chunk)},
            LIST_UNITS,
        )
        items.extend(body.get("items", []))
    subs = _channel_subs(
        client, [str((it.get("snippet") or {}).get("channelId", "")) for it in items]
    )
    videos = [_build_video(it, first_query.get(str(it.get("id")), ""), subs, now) for it in items]
    return sorted(videos, key=lambda v: v.outlier, reverse=True)


# ----------------------------------------------------------------------------- motifs (local)


def _top_size(n: int) -> int:
    return max(1, math.ceil(TOP_SHARE * n)) if n else 0


def _paris(dt: datetime) -> datetime:
    try:
        from zoneinfo import ZoneInfo

        return dt.astimezone(ZoneInfo("Europe/Paris"))
    except (ImportError, KeyError):
        return dt


def _words(title: str) -> list[str]:
    return [
        w
        for w in re.findall(r"[^\W_]+", title.lower())
        if len(w) >= 3 and not w.isdigit() and w not in _STOP
    ]


def _terms(title: str) -> set[str]:
    toks = _words(title)
    return set(toks) | {f"{a} {b}" for a, b in itertools.pairwise(toks)}


def _median(values: Sequence[float]) -> float | None:
    return statistics.median(values) if values else None


def _share(videos: Sequence[Video], pred: Callable[[Video], bool]) -> float | None:
    return round(sum(1 for v in videos if pred(v)) / len(videos), 3) if videos else None


def _pair(top: Sequence[Video], rest: Sequence[Video], fn: Callable[[Video], float]) -> dict:
    return {"top": _median([fn(v) for v in top]), "reste": _median([fn(v) for v in rest])}


def _share_pair(top: Sequence[Video], rest: Sequence[Video], pred: Callable[[Video], bool]) -> dict:
    return {"top": _share(top, pred), "reste": _share(rest, pred)}


def _terms_block(top: Sequence[Video], rest: Sequence[Video]) -> list[dict[str, Any]]:
    c_top: Counter[str] = Counter()
    c_rest: Counter[str] = Counter()
    for v in top:
        c_top.update(_terms(v.title))
    for v in rest:
        c_rest.update(_terms(v.title))
    min_count = 2 if len(top) >= 5 else 1
    rows = []
    for term, k in c_top.items():
        if k < min_count:
            continue
        share_top = k / len(top)
        share_rest = c_rest.get(term, 0) / len(rest) if rest else 0.0
        lift = (share_top + 0.01) / (share_rest + 0.01)  # lissage : évite la division par zéro
        rows.append({"terme": term, "top": k, "reste": c_rest.get(term, 0), "lift": round(lift, 2)})
    rows.sort(key=lambda r: (-r["lift"], -r["top"], r["terme"]))
    return rows[:15]


def patterns(videos: Sequence[Video]) -> dict[str, Any]:
    """Motifs des vidéos du top 20 % (outlier) comparés au reste. Calcul local, sans LLM."""
    if not videos:
        return {"videos": 0}
    ranked = sorted(videos, key=lambda v: v.outlier, reverse=True)
    n_top = _top_size(len(ranked))
    top, rest = ranked[:n_top], ranked[n_top:]
    tag_counter: Counter[str] = Counter()
    for v in top:
        tag_counter.update({t.lower() for t in v.tags})
    hours = Counter(_paris(v.published_at).hour for v in top)
    days = Counter(_paris(v.published_at).weekday() for v in top)
    return {
        "videos": len(ranked),
        "groupes": {"top": len(top), "reste": len(rest)},
        "titres": {
            "longueur_mediane": _pair(top, rest, lambda v: len(v.title)),
            "emoji": _share_pair(top, rest, lambda v: bool(_EMOJI.search(v.title))),
            "chiffres": _share_pair(top, rest, lambda v: any(c.isdigit() for c in v.title)),
            "mot_en_majuscules": _share_pair(top, rest, lambda v: bool(_CAPS.search(v.title))),
        },
        "duree": {
            "mediane_s": _pair(top, rest, lambda v: v.duration_s),
            "shorts": _share_pair(top, rest, lambda v: v.is_short),
        },
        "publication_top": {
            "heures_paris": [{"heure": h, "n": n} for h, n in hours.most_common(3)],
            "jours": [{"jour": _JOURS[d], "n": n} for d, n in days.most_common(3)],
        },
        "termes": _terms_block(top, rest),
        "tags_top": [{"tag": t, "n": n} for t, n in tag_counter.most_common(10)],
    }


# ----------------------------------------------------------------------------- rapport


class _Ideas(BaseModel):
    titles: list[str] = Field(default_factory=list)
    subjects: list[str] = Field(default_factory=list)


def _render_patterns(p: dict[str, Any]) -> str:
    if not p.get("videos"):
        return "_Aucune vidéo à analyser._"
    t, d = p["titres"], p["duree"]
    lines = [
        f"- Groupes : {p['groupes']['top']} vidéos du top 20 % contre {p['groupes']['reste']} "
        "dans le reste.",
        f"- Longueur médiane du titre : {_val(t['longueur_mediane'], 'car.')}.",
        f"- Emoji dans le titre : {_pc(t['emoji'])} (top) contre {_pc(t['emoji'], 'reste')}.",
        f"- Chiffre dans le titre : {_pc(t['chiffres'])} contre {_pc(t['chiffres'], 'reste')}.",
        f"- Mot en MAJUSCULES : {_pc(t['mot_en_majuscules'])} contre "
        f"{_pc(t['mot_en_majuscules'], 'reste')}.",
        f"- Durée médiane : {_val(d['mediane_s'], 's')} ; part de Shorts : "
        f"{_pc(d['shorts'])} contre {_pc(d['shorts'], 'reste')}.",
    ]
    pub = p["publication_top"]
    if pub["heures_paris"]:
        hs = ", ".join(f"{h['heure']} h ({h['n']})" for h in pub["heures_paris"])
        js = ", ".join(f"{j['jour']} ({j['n']})" for j in pub["jours"])
        lines.append(f"- Heures de publication (Paris) les plus fréquentes dans le top : {hs}.")
        lines.append(f"- Jours les plus fréquents dans le top : {js}.")
    if p["termes"]:
        lines.append(
            "\n**Termes sur-représentés dans le top 20 %** (lift = part top / part reste) :\n"
        )
        lines.append(
            md_table(
                ["Terme", "Top", "Reste", "Lift"],
                [[r["terme"], r["top"], r["reste"], fmt_dec(r["lift"])] for r in p["termes"]],
            )
        )
    if p["tags_top"]:
        tags = ", ".join(f"{t['tag']} ({t['n']})" for t in p["tags_top"])
        lines.append(f"\n**Tags fréquents dans le top** : {tags}.")
    return "\n".join(lines)


def _val(pair: dict[str, Any], unit: str) -> str:
    def fmt(x: float | None) -> str:
        return "—" if x is None else f"{fmt_dec(x, 0)} {unit}"

    return f"{fmt(pair['top'])} (top) contre {fmt(pair['reste'])} (reste)"


def _pc(pair: dict[str, Any], key: str = "top") -> str:
    value = pair[key]
    return "—" if value is None else f"{round(value * 100)} %"


def _is_kind(v: Video, kind: str | None) -> bool:
    if kind == "short":
        return v.is_short
    if kind == "long":
        return not v.is_short
    return True


def niche_report(
    profile: Any,
    queries: Sequence[str],
    llm: Any = None,
    *,
    days: int = 30,
    kind: str | None = None,
    max_results: int = 50,
    region: str = "FR",
    lang: str = "fr",
    client: YouTubeClient | None = None,
) -> str:
    """Rapport Markdown : top outliers, motifs locaux et, si LLM dispo, 5 titres + 5 sujets.

    `kind` : "short", "long" ou None (tous). Les chiffres viennent des données, jamais du LLM.
    """
    client = client or YouTubeClient(get_key("YOUTUBE_API_KEY"))
    fetched = search_niche(
        queries, days=days, max_results=max_results, region=region, lang=lang, client=client
    )
    videos = [v for v in fetched if _is_kind(v, kind)]
    kind_label = {"short": "Shorts", "long": "formats longs"}.get(kind or "", "tous formats")
    name = getattr(profile, "display_name", None) or getattr(profile, "id", "")
    head = [
        f"# Recherche de niche — {name}",
        "",
        f"- Requêtes : {', '.join(f'« {q} »' for q in queries)}",
        f"- Fenêtre : {days} derniers jours · Format : {kind_label} · Pays : {region} · "
        f"Langue : {lang}",
        f"- Vidéos analysées : {len(videos)} (sur {len(fetched)} récupérées)",
        f"- Quota YouTube utilisé aujourd'hui : {fmt_int(client.ledger.used())} / "
        f"{fmt_int(client.ledger.budget)} unités",
        "- Outlier = vues / max(abonnés, 100) ; vph = vues par heure depuis la publication.",
        "",
    ]
    if not videos:
        return "\n".join([*head, "## Résultats", "", "_Aucune vidéo pour ces critères._", ""])

    top = videos[:10]
    table = md_table(
        ["#", "Titre", "Chaîne", "Vues", "Outlier", "Vues/h", "Abonnés", "Lien"],
        [
            [
                i,
                v.title.replace("|", "/"),
                v.channel_title,
                fmt_int(v.views),
                fmt_dec(v.outlier),
                fmt_dec(v.vph, 1),
                "masqué" if v.subs is None else fmt_int(v.subs),
                f"[voir]({v.url})",
            ]
            for i, v in enumerate(top, 1)
        ],
    )
    pats = patterns(videos)
    body = [
        *head,
        "## Top 10 outliers",
        "",
        table,
        "",
        "## Motifs (top 20 % outliers vs reste, calcul local)",
        "",
        _render_patterns(pats),
        "",
    ]

    llm = llm if llm is not None else llm_for_profile(profile)
    payload = {
        "chaine": name,
        "requetes": list(queries),
        "motifs": {k: pats[k] for k in ("titres", "duree", "publication_top", "termes")},
        "top_titres": [
            {
                "titre": v.title,
                "vues": v.views,
                "outlier": round(v.outlier, 2),
                "short": v.is_short,
            }
            for v in top
        ],
    }
    ideas, note = ask_llm(llm, "niche", payload, _Ideas)
    body.append("## Idées (LLM local)")
    body.append("")
    if ideas is None:
        body.append(f"_{note}_")
    else:
        body.append("### 5 titres")
        body.extend(f"- {t}" for t in ideas.titles[:5])
        body.append("")
        body.append("### 5 sujets")
        body.extend(f"- {s}" for s in ideas.subjects[:5])
    body.append("")
    return "\n".join(body)
