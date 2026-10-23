"""Recherche de niche YouTube : requêtes, outliers, quota, cache, motifs, rapport (mock)."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from minavio.errors import ConfigError, LLMUnavailableError
from minavio.integrations import keys
from minavio.integrations import youtube_research as yr

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
KEY = "SECRET-KEY-123"

VIDEOS = {
    "v1": {
        "id": "v1",
        "snippet": {
            "title": "Minecraft survie : le ROI des défis 🔥 TOP 5",
            "channelId": "c1",
            "channelTitle": "Chaine A",
            "publishedAt": "2026-10-20T12:00:00Z",
            "tags": ["minecraft", "survie"],
        },
        "statistics": {"viewCount": "50000", "likeCount": "2000", "commentCount": "150"},
        "contentDetails": {"duration": "PT45S"},
    },
    "v2": {
        "id": "v2",
        "snippet": {
            "title": "Test long format minecraft",
            "channelId": "c2",
            "channelTitle": "Chaine B",
            "publishedAt": "2026-10-01T12:00:00Z",
        },
        "statistics": {"viewCount": "3000"},
        "contentDetails": {"duration": "PT12M5S"},
    },
    "v3": {
        "id": "v3",
        "snippet": {
            "title": "Vlog de la semaine",
            "channelId": "c1",
            "channelTitle": "Chaine A",
            "publishedAt": "2026-10-22T12:00:00Z",
        },
        "statistics": {"viewCount": "900"},
        "contentDetails": {"duration": "PT30S"},
    },
}
CHANNELS = {
    "c1": {"id": "c1", "statistics": {"subscriberCount": "1000"}},
    "c2": {"id": "c2", "statistics": {"hiddenSubscriberCount": True}},
}


def yt_handler(calls: list[str], queries: list[str] | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        resource = request.url.path.rsplit("/", 1)[-1]
        params = dict(request.url.params)
        calls.append(resource)
        assert params["key"] == KEY
        if resource == "search":
            if queries is not None:
                queries.append(params["q"])
            ids = ["v1", "v2"] if params["q"] == "minecraft" else ["v1", "v3"]
            return httpx.Response(
                200, json={"items": [{"id": {"kind": "youtube#video", "videoId": i}} for i in ids]}
            )
        if resource == "videos":
            return httpx.Response(200, json={"items": [VIDEOS[i] for i in params["id"].split(",")]})
        if resource == "channels":
            return httpx.Response(
                200, json={"items": [CHANNELS[c] for c in params["id"].split(",")]}
            )
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def make_client(
    tmp_path, transport: httpx.BaseTransport, *, budget: int = 3000, cache_clock=None
) -> yr.YouTubeClient:
    return yr.YouTubeClient(
        KEY,
        transport=transport,
        cache=yr.ResponseCache(tmp_path / "cache", clock=cache_clock or (lambda: NOW.timestamp())),
        ledger=yr.QuotaLedger(tmp_path / "quota.json", budget=budget),
        clock=lambda: NOW,
    )


PROFILE_NO_LLM = SimpleNamespace(
    id="gaming", display_name="Gaming", llm=SimpleNamespace(enabled=False, prompt="gaming")
)


class FakeLLM:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[list[dict[str, str]]] = []

    def chat_json(self, messages: list[dict[str, str]], schema: Any) -> Any:
        self.calls.append(messages)
        if self.error:
            raise self.error
        return schema(
            titles=[f"Titre {i}" for i in range(1, 7)],
            subjects=[f"Sujet {i}" for i in range(1, 6)],
        )


# ----------------------------------------------------------------------------- requêtes & scores


def test_search_niche_requests_and_outlier_scores(tmp_path):
    calls: list[str] = []
    client = make_client(tmp_path, yt_handler(calls))

    videos = yr.search_niche(["minecraft", "vlog"], days=30, client=client)

    # 2 search (100 u.) + 1 videos.list + 1 channels.list (1 u. chacun)
    assert calls == ["search", "search", "videos", "channels"]
    assert client.ledger.used() == 202
    # doublons fusionnés, tri par outlier décroissant
    assert [v.id for v in videos] == ["v1", "v2", "v3"]
    v1, v2, v3 = videos
    assert v1.outlier == pytest.approx(50000 / 1000)  # 1 000 abonnés
    assert v2.subs is None and v2.outlier == pytest.approx(3000 / 100)  # masqués -> plancher 100
    assert v3.outlier == pytest.approx(900 / 1000)
    assert v1.vph == pytest.approx(50000 / 72)  # publiée il y a 72 h
    assert v1.is_short is True and v2.is_short is False  # 45 s / 12 min 5 s
    assert v1.query == "minecraft" and v3.query == "vlog"
    assert v1.url == "https://www.youtube.com/watch?v=v1"


def test_search_window_and_filters_are_sent(tmp_path):
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search"):
            seen.append(dict(request.url.params))
            return httpx.Response(200, json={"items": []})
        return httpx.Response(500)

    client = make_client(tmp_path, httpx.MockTransport(handler))
    assert yr.search_niche(["minecraft"], days=30, video_duration="short", client=client) == []
    assert seen[0]["publishedAfter"] == "2026-09-23T12:00:00Z"
    assert seen[0]["order"] == "viewCount"
    assert seen[0]["regionCode"] == "FR" and seen[0]["relevanceLanguage"] == "fr"
    assert seen[0]["videoDuration"] == "short"


def test_api_error_message_has_no_key(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403, json={"error": {"errors": [{"reason": "quotaExceeded"}], "message": KEY}}
        )

    client = make_client(tmp_path, httpx.MockTransport(handler))
    with pytest.raises(yr.YouTubeAPIError) as exc:
        yr.search_niche(["minecraft"], client=client)
    assert "minuit" in str(exc.value)
    assert KEY not in str(exc.value)


# ----------------------------------------------------------------------------- quota


def test_quota_refused_beyond_budget_and_persisted(tmp_path):
    calls: list[str] = []
    client = make_client(tmp_path, yt_handler(calls), budget=150)

    with pytest.raises(yr.QuotaExceededError, match="Budget quota YouTube atteint"):
        yr.search_niche(["minecraft", "vlog"], client=client)

    assert calls == ["search"]  # la seconde recherche n'est jamais envoyée
    persisted = yr.QuotaLedger(tmp_path / "quota.json", budget=150)
    assert persisted.used() == 100  # compteur relu depuis le disque


def test_quota_budget_from_env(monkeypatch):
    monkeypatch.setenv(yr.BUDGET_ENV, "5000")
    assert yr.budget_from_env() == 5000
    monkeypatch.setenv(yr.BUDGET_ENV, "beaucoup")
    with pytest.raises(ConfigError, match=yr.BUDGET_ENV):
        yr.budget_from_env()


def test_quota_counter_resets_on_new_day(tmp_path):
    path = tmp_path / "quota.json"
    path.write_text('{"date": "1999-01-01", "units": 2900}', encoding="utf-8")
    ledger = yr.QuotaLedger(path, budget=3000)
    assert ledger.used() == 0
    ledger.spend(100)
    assert ledger.used() == 100


# ----------------------------------------------------------------------------- cache


def test_cache_avoids_requests_and_quota_then_expires(tmp_path):
    calls: list[str] = []
    now = [NOW.timestamp()]
    client = make_client(tmp_path, yt_handler(calls), cache_clock=lambda: now[0])

    first = yr.search_niche(["minecraft"], client=client)
    spent = client.ledger.used()
    calls_after_first = len(calls)
    second = yr.search_niche(["minecraft"], client=client)

    assert [v.id for v in second] == [v.id for v in first]
    assert len(calls) == calls_after_first  # 100 % servi par le cache
    assert client.ledger.used() == spent

    now[0] += 25 * 3600  # au-delà de 24 h
    yr.search_niche(["minecraft"], client=client)
    assert len(calls) > calls_after_first
    assert client.ledger.used() > spent


def test_cache_files_never_contain_the_key(tmp_path):
    client = make_client(tmp_path, yt_handler([]))
    yr.search_niche(["minecraft"], client=client)
    files = list((tmp_path / "cache").glob("*.json"))
    assert files
    assert all(KEY not in f.read_text(encoding="utf-8") for f in files)


# ----------------------------------------------------------------------------- motifs


def _video(i: int, title: str, outlier: float, duration: int) -> yr.Video:
    return yr.Video(
        id=f"x{i}",
        title=title,
        channel_id="c",
        channel_title="C",
        published_at=datetime(2026, 10, 20, 18, 0, tzinfo=UTC),
        duration_s=duration,
        views=int(outlier * 100),
        likes=0,
        comments=0,
        tags=("minecraft",) if outlier > 5 else ("vlog",),
        subs=100,
        query="q",
        hours=24.0,
        vph=1.0,
        outlier=outlier,
    )


def test_patterns_on_fixed_set():
    videos = [
        _video(1, "Minecraft survie 🔥 TOP 5", 9.0, 30),
        _video(2, "Minecraft survie 🔥 INCROYABLE", 8.0, 40),
        *[_video(10 + i, f"Vlog semaine {'x' * (i + 1)}", 0.5, 600) for i in range(8)],
    ]
    # 10 vidéos -> top 20 % = 2 vidéos (les outliers les plus forts)
    pats = yr.patterns(videos)

    assert pats["groupes"] == {"top": 2, "reste": 8}
    assert pats["titres"]["emoji"] == {"top": 1.0, "reste": 0.0}
    assert pats["titres"]["mot_en_majuscules"] == {"top": 1.0, "reste": 0.0}
    assert pats["titres"]["chiffres"] == {"top": 0.5, "reste": 0.0}
    assert pats["duree"]["shorts"] == {"top": 1.0, "reste": 0.0}
    assert pats["duree"]["mediane_s"] == {"top": 35.0, "reste": 600.0}
    terms = {t["terme"]: t for t in pats["termes"]}
    assert terms["minecraft"]["top"] == 2 and terms["minecraft"]["reste"] == 0
    assert "minecraft survie" in terms
    assert terms["minecraft"]["lift"] > 1
    assert pats["tags_top"][0] == {"tag": "minecraft", "n": 2}


def test_patterns_empty():
    assert yr.patterns([]) == {"videos": 0}


def test_parse_duration():
    assert yr.parse_duration("PT45S") == 45
    assert yr.parse_duration("PT1M30S") == 90
    assert yr.parse_duration("PT1H2M3S") == 3723
    assert yr.parse_duration("P1DT1S") == 86401
    assert yr.parse_duration("") == 0


# ----------------------------------------------------------------------------- rapport


def test_report_without_llm(tmp_path):
    client = make_client(tmp_path, yt_handler([]))
    md = yr.niche_report(PROFILE_NO_LLM, ["minecraft"], client=client)

    assert "## Top 10 outliers" in md
    assert "https://www.youtube.com/watch?v=v1" in md
    assert "LLM non sollicité" in md
    assert "### 5 titres" not in md
    assert "Quota YouTube utilisé aujourd'hui : 102 / 3 000" in md


def test_report_short_filter(tmp_path):
    client = make_client(tmp_path, yt_handler([]))
    md = yr.niche_report(PROFILE_NO_LLM, ["minecraft"], kind="short", client=client)
    assert "Test long format minecraft" not in md
    assert "Minecraft survie" in md


def test_report_with_fake_llm_gets_titles_only(tmp_path):
    client = make_client(tmp_path, yt_handler([]))
    llm = FakeLLM()
    md = yr.niche_report(PROFILE_NO_LLM, ["minecraft"], llm, client=client)

    assert "### 5 titres" in md and "- Titre 1" in md
    assert "- Titre 6" not in md  # plafonné à 5
    assert "### 5 sujets" in md and "- Sujet 5" in md
    (messages,) = llm.calls[:1]
    assert messages[0]["role"] == "system"
    user = messages[1]["content"]
    assert "Minecraft survie" in user and "outlier" in user
    assert "http" not in user  # ni liens, ni images


def test_report_when_llm_unavailable(tmp_path):
    client = make_client(tmp_path, yt_handler([]))
    llm = FakeLLM(error=LLMUnavailableError("serveur local arrêté"))
    md = yr.niche_report(PROFILE_NO_LLM, ["minecraft"], llm, client=client)
    assert "LLM local injoignable" in md
    assert "## Top 10 outliers" in md  # le rapport chiffré reste produit


def test_report_needs_at_least_one_query(tmp_path):
    client = make_client(tmp_path, yt_handler([]))
    with pytest.raises(ConfigError):
        yr.search_niche(["  "], client=client)


# ----------------------------------------------------------------------------- clé


def test_get_key_from_env_and_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("YOUTUBE_API_KEY", "abc")
    assert keys.get_key("YOUTUBE_API_KEY") == "abc"
    monkeypatch.delenv("YOUTUBE_API_KEY")
    monkeypatch.setenv("MINAVIO_SECRETS_FILE", str(tmp_path / "absent.env"))
    with pytest.raises(ConfigError, match="YOUTUBE_API_KEY"):
        keys.get_key("YOUTUBE_API_KEY")
