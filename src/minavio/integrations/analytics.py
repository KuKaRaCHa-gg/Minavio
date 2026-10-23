"""Analytics de MES chaînes : YouTube Analytics API v2 (OAuth, scope yt-analytics.readonly).

Réutilise le flux OAuth de `minavio.publish.auth` (même jeton, scope Analytics ajouté). Si le jeton
ne porte pas ce scope, erreur actionnable : `minavio publish auth --logout` puis re-auth.
Le CTR d'impression n'est pas une métrique documentée de l'API Analytics : tenté, puis signalé
« indisponible » si l'API refuse la métrique.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from pydantic import BaseModel, Field

from minavio.errors import ConfigError, MinavioError
from minavio.integrations.common import ask_llm, fmt_dec, fmt_int, fmt_pct, md_table
from minavio.publish import auth

log = logging.getLogger("minavio.integrations")

ANALYTICS_URL = "https://youtubeanalytics.googleapis.com/v2/reports"
YT_DATA_URL = "https://www.googleapis.com/youtube/v3/videos"
VIDEO_METRICS = (
    "views,engagedViews,averageViewPercentage,averageViewDuration,subscribersGained,likes,shares"
)
THUMB_METRICS = "videoThumbnailImpressions,videoThumbnailImpressionsClickRate"
TOP_SHARE = 0.2
MISSING_SCOPE_MSG = (
    "Le jeton YouTube n'a pas le scope Analytics (yt-analytics.readonly). Lance "
    "`minavio publish auth --logout` puis `minavio publish auth` pour ré-autoriser la chaîne."
)


class AnalyticsError(MinavioError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class ChannelReport:
    channel: str
    start: str
    end: str
    days: int
    totals: dict[str, Any]
    videos: list[dict[str, Any]] = field(default_factory=list)
    content_types: list[dict[str, Any]] = field(default_factory=list)
    traffic: list[dict[str, Any]] = field(default_factory=list)
    thumbnail_ctr: dict[str, Any] = field(default_factory=dict)


# ----------------------------------------------------------------------------- auth


def _granted_scopes() -> set[str]:
    """Scopes réellement accordés, lus dans le fichier de jeton (pas dans les scopes demandés)."""
    try:
        data = json.loads(auth.token_path().read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise MinavioError(
            f"Jeton YouTube illisible : relance `minavio publish auth` ({e})."
        ) from e
    scopes = data.get("scopes") or []
    if isinstance(scopes, str):
        scopes = scopes.split()
    return set(scopes)


def _authorized_token() -> str:
    creds = auth.load_credentials()  # non connecté / expiré : erreur actionnable du flux publish
    if auth.ANALYTICS_SCOPE not in _granted_scopes():
        raise MinavioError(MISSING_SCOPE_MSG)
    token = getattr(creds, "token", None)
    if not token:
        raise MinavioError("Jeton YouTube vide : relance `minavio publish auth`.")
    return str(token)


# ----------------------------------------------------------------------------- requêtes


def _reason(resp: httpx.Response) -> str:
    try:
        err = resp.json().get("error", {})
        return str((err.get("errors") or [{}])[0].get("reason") or err.get("status") or "")
    except (ValueError, AttributeError, TypeError, IndexError):
        return ""


def _analytics_error(resp: httpx.Response) -> AnalyticsError:
    code, reason = resp.status_code, _reason(resp)
    if code in (401, 403):
        return AnalyticsError(
            f"YouTube Analytics refuse l'accès (HTTP {code}, {reason or 'sans raison'}). Vérifie "
            "que l'API YouTube Analytics est activée dans le projet Google Cloud, puis lance "
            "`minavio publish auth --logout` et `minavio publish auth`.",
            code,
        )
    return AnalyticsError(
        f"YouTube Analytics a refusé la requête (HTTP {code}, {reason or 'sans raison'}).", code
    )


def _window(days: int) -> tuple[str, str]:
    end = datetime.now(UTC).date()
    return (end - timedelta(days=days)).isoformat(), end.isoformat()


def _report_rows(
    http: httpx.Client,
    start: str,
    end: str,
    metrics: str,
    *,
    dimensions: str | None = None,
    sort: str | None = None,
    max_results: int | None = None,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "ids": "channel==MINE",
        "startDate": start,
        "endDate": end,
        "metrics": metrics,
    }
    if dimensions:
        params["dimensions"] = dimensions
    if sort:
        params["sort"] = sort
    if max_results:
        params["maxResults"] = max_results
    resp = http.get(ANALYTICS_URL, params=params)
    if resp.status_code != 200:
        raise _analytics_error(resp)
    try:
        body = resp.json()
    except ValueError as e:
        raise AnalyticsError("Réponse YouTube Analytics non JSON.") from e
    names = [str(h.get("name", "")) for h in body.get("columnHeaders") or []]
    return [dict(zip(names, row, strict=False)) for row in body.get("rows") or []]


def _titles(http: httpx.Client, ids: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for i in range(0, len(ids), 50):
        resp = http.get(YT_DATA_URL, params={"part": "snippet", "id": ",".join(ids[i : i + 50])})
        if resp.status_code != 200:
            log.warning("titres des vidéos indisponibles (HTTP %s)", resp.status_code)
            return out
        for it in resp.json().get("items", []):
            out[str(it.get("id", ""))] = str((it.get("snippet") or {}).get("title", ""))
    return out


def _thumbnail_ctr(http: httpx.Client, start: str, end: str) -> dict[str, Any]:
    try:
        rows = _report_rows(http, start, end, THUMB_METRICS)
    except AnalyticsError as e:
        if e.status in (401, 403):
            raise
        return {
            "disponible": False,
            "raison": f"métrique refusée par l'API (HTTP {e.status}) : CTR d'impression à lire "
            "dans YouTube Studio.",
        }
    row = rows[0] if rows else {}
    return {
        "disponible": True,
        "impressions": row.get("videoThumbnailImpressions"),
        "taux_clic": row.get("videoThumbnailImpressionsClickRate"),
    }


def channel_report(
    profile: Any, days: int = 90, *, client: httpx.Client | None = None
) -> ChannelReport:
    """Rapport de MA chaîne sur `days` jours : par vidéo, formats, sources de trafic, CTR."""
    if days < 1:
        raise ConfigError("--days doit être au moins 1.")
    owns = client is None
    http = client or httpx.Client(
        headers={"Authorization": f"Bearer {_authorized_token()}"}, timeout=30.0
    )
    try:
        start, end = _window(days)
        totals_rows = _report_rows(http, start, end, VIDEO_METRICS)
        videos = _report_rows(
            http, start, end, VIDEO_METRICS, dimensions="video", sort="-views", max_results=200
        )
        content = _report_rows(
            http, start, end, VIDEO_METRICS, dimensions="creatorContentType", sort="-views"
        )
        traffic = _report_rows(
            http,
            start,
            end,
            "views,engagedViews",
            dimensions="insightTrafficSourceType",
            sort="-views",
            max_results=25,
        )
        ctr = _thumbnail_ctr(http, start, end)
        titles = _titles(http, [str(r.get("video", "")) for r in videos])
    finally:
        if owns:
            http.close()
    for r in videos:
        vid = str(r.get("video", ""))
        r["title"] = titles.get(vid, "")
        r["url"] = f"https://www.youtube.com/watch?v={vid}"
    return ChannelReport(
        channel=getattr(profile, "id", ""),
        start=start,
        end=end,
        days=days,
        totals=dict(totals_rows[0]) if totals_rows else {},
        videos=videos,
        content_types=content,
        traffic=traffic,
        thumbnail_ctr=ctr,
    )


# ----------------------------------------------------------------------------- conseils


class _Advice(BaseModel):
    recommendations: list[str] = Field(default_factory=list)


def _f(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _top_size(n: int) -> int:
    return max(1, math.ceil(TOP_SHARE * n)) if n else 0


def _per_1000(subs: float, views: float) -> float | None:
    return subs / views * 1000 if views else None


def _video_row(v: dict[str, Any]) -> list[Any]:
    return [
        str(v.get("title") or v.get("video")).replace("|", "/"),
        fmt_int(_f(v.get("engagedViews"))),
        fmt_pct(_f(v.get("averageViewPercentage"))),
        fmt_int(_f(v.get("subscribersGained"))),
        f"[voir]({v.get('url', '')})",
    ]


def growth_advice(report: ChannelReport, llm: Any = None) -> str:
    """Rapport Markdown : ce qui marche, ce qui ne marche pas, abonnés / 1 000 vues, conseils LLM.

    Tous les chiffres viennent de `report` ; le LLM ne fait que des recommandations écrites.
    """
    vids = report.videos
    t = report.totals
    views, subs = _f(t.get("views")), _f(t.get("subscribersGained"))
    rate = _per_1000(subs, views)
    n_top = _top_size(len(vids))
    by_eng = sorted(vids, key=lambda v: _f(v.get("engagedViews")), reverse=True)
    works = by_eng[:n_top]
    weak = list(reversed(by_eng[len(by_eng) - n_top :])) if n_top and len(by_eng) > n_top else []
    retention = sorted(vids, key=lambda v: _f(v.get("averageViewPercentage")), reverse=True)
    retention = retention[:n_top]
    ctr = report.thumbnail_ctr
    if ctr.get("disponible"):
        ctr_line = (
            f"impressions de vignettes : {fmt_int(_f(ctr.get('impressions')))}, taux de clic : "
            f"{fmt_pct(_f(ctr.get('taux_clic')) * 100)} (métrique à confirmer dans Studio)."
        )
    else:
        reason = ctr.get("raison") or "métrique non exposée par l'API"
        ctr_line = f"indisponible — {reason}"

    lines = [
        f"# Analytics — {report.channel}",
        "",
        f"Période : {report.start} → {report.end} ({report.days} jours).",
        "",
        "## Vue d'ensemble",
        "",
        f"- Vues : {fmt_int(views)} · Vues engagées : {fmt_int(_f(t.get('engagedViews')))} · "
        f"Abonnés gagnés : {fmt_int(subs)}",
        f"- Abonnés gagnés pour 1 000 vues : {fmt_dec(rate) if rate is not None else '—'}",
        f"- CTR d'impression : {ctr_line}",
        "",
        "## Formats (Shorts / longs)",
        "",
        md_table(
            ["Type", "Vues", "Vues engagées", "Rétention", "Abonnés", "Abonnés / 1 000 vues"],
            [
                [
                    c.get("creatorContentType", "?"),
                    fmt_int(_f(c.get("views"))),
                    fmt_int(_f(c.get("engagedViews"))),
                    fmt_pct(_f(c.get("averageViewPercentage"))),
                    fmt_int(_f(c.get("subscribersGained"))),
                    fmt_dec(_per_1000(_f(c.get("subscribersGained")), _f(c.get("views")))),
                ]
                for c in report.content_types
            ],
        ),
        "",
        f"## Ce qui marche (top 20 % par vues engagées : {len(works)} vidéos)",
        "",
        md_table(
            ["Titre", "Vues engagées", "Rétention", "Abonnés", "Lien"],
            [_video_row(v) for v in works],
        ),
        "",
        "## Meilleure rétention (top 20 %)",
        "",
        md_table(
            ["Titre", "Vues engagées", "Rétention", "Abonnés", "Lien"],
            [_video_row(v) for v in retention],
        ),
        "",
        "## Ce qui ne marche pas (bas 20 % par vues engagées)",
        "",
        md_table(
            ["Titre", "Vues engagées", "Rétention", "Abonnés", "Lien"],
            [_video_row(v) for v in weak],
        ),
        "",
        "## Sources de trafic (top 5)",
        "",
        md_table(
            ["Source", "Vues", "Vues engagées"],
            [
                [
                    s.get("insightTrafficSourceType", "?"),
                    fmt_int(_f(s.get("views"))),
                    fmt_int(_f(s.get("engagedViews"))),
                ]
                for s in report.traffic[:5]
            ],
        ),
        "",
        "## Recommandations (LLM local)",
        "",
    ]
    payload = {
        "totaux": {
            "vues": views,
            "vues_engagees": _f(t.get("engagedViews")),
            "abonnes": subs,
            "abonnes_pour_1000_vues": rate,
        },
        "formats": [
            {
                "type": c.get("creatorContentType"),
                "vues": _f(c.get("views")),
                "retention_pct": _f(c.get("averageViewPercentage")),
                "abonnes": _f(c.get("subscribersGained")),
            }
            for c in report.content_types
        ],
        "ce_qui_marche": [_brief(v) for v in works],
        "ce_qui_ne_marche_pas": [_brief(v) for v in weak],
        "meilleure_retention": [_brief(v) for v in retention],
        "sources": [
            {"source": s.get("insightTrafficSourceType"), "vues": _f(s.get("views"))}
            for s in report.traffic[:5]
        ],
    }
    advice, note = ask_llm(llm, "advice", payload, _Advice)
    if advice is None:
        lines.append(f"_{note} Rapport sans recommandations LLM._")
    else:
        lines.extend(f"- {r}" for r in advice.recommendations[:5])
    lines.append("")
    return "\n".join(lines)


def _brief(v: dict[str, Any]) -> dict[str, Any]:
    return {
        "titre": v.get("title", ""),
        "vues_engagees": _f(v.get("engagedViews")),
        "retention_pct": _f(v.get("averageViewPercentage")),
        "abonnes": _f(v.get("subscribersGained")),
    }
