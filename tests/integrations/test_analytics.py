"""Analytics de mes chaînes : faux service YouTube Analytics, scope manquant, conseils."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from minavio.errors import LLMUnavailableError, MinavioError
from minavio.integrations import analytics as an
from minavio.integrations.common import md_table
from minavio.publish import auth

PROFILE = SimpleNamespace(id="gaming", display_name="Gaming")

TOTALS = {
    "views": 1000,
    "engagedViews": 600,
    "subscribersGained": 25,
    "averageViewPercentage": 40.0,
    "averageViewDuration": 90.0,
    "likes": 50,
    "shares": 5,
}
VIDEO_ROWS = [
    {
        "video": "a",
        "views": 500,
        "engagedViews": 400,
        "averageViewPercentage": 55.0,
        "averageViewDuration": 80.0,
        "subscribersGained": 15,
        "likes": 30,
        "shares": 3,
    },
    {
        "video": "b",
        "views": 300,
        "engagedViews": 150,
        "averageViewPercentage": 30.0,
        "averageViewDuration": 40.0,
        "subscribersGained": 8,
        "likes": 15,
        "shares": 1,
    },
    {
        "video": "c",
        "views": 200,
        "engagedViews": 50,
        "averageViewPercentage": 20.0,
        "averageViewDuration": 15.0,
        "subscribersGained": 2,
        "likes": 5,
        "shares": 1,
    },
]
TITLES = {"a": "Meilleur format", "b": "Moyen | tuto", "c": "Raté"}


def _analytics_handler(thumb_supported: bool = False):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer fake-token"
        if "youtubeanalytics" in request.url.host:
            params = dict(request.url.params)
            assert params["ids"] == "channel==MINE"
            dims = params.get("dimensions")
            metrics = params["metrics"]
            if metrics == an.THUMB_METRICS and not thumb_supported:
                return httpx.Response(400, json={"error": {"errors": [{"reason": "badRequest"}]}})
            if dims is None and metrics == an.THUMB_METRICS:
                return _rows(
                    ["videoThumbnailImpressions", "videoThumbnailImpressionsClickRate"],
                    [[1000, 0.05]],
                )
            if dims is None:
                return _rows(list(TOTALS), [list(TOTALS.values())])
            if dims == "video":
                return _rows(list(VIDEO_ROWS[0]), [list(r.values()) for r in VIDEO_ROWS])
            if dims == "creatorContentType":
                return _rows(
                    [
                        "creatorContentType",
                        "views",
                        "engagedViews",
                        "averageViewPercentage",
                        "subscribersGained",
                    ],
                    [["SHORTS", 800, 500, 35.0, 20], ["VIDEO_ON_DEMAND", 200, 100, 60.0, 5]],
                )
            if dims == "insightTrafficSourceType":
                return _rows(
                    ["insightTrafficSourceType", "views", "engagedViews"],
                    [["YT_SEARCH", 600, 300], ["SUGGESTED_VIDEO", 400, 300]],
                )
        if request.url.path.endswith("/videos"):
            ids = request.url.params["id"].split(",")
            return httpx.Response(
                200, json={"items": [{"id": i, "snippet": {"title": TITLES[i]}} for i in ids]}
            )
        return httpx.Response(404)

    return handler


def _rows(names: list[str], rows: list[list[Any]]) -> httpx.Response:
    return httpx.Response(
        200,
        json={"columnHeaders": [{"name": n} for n in names], "rows": rows, "kind": "x"},
    )


def _client(**kw) -> httpx.Client:
    return httpx.Client(
        transport=httpx.MockTransport(_analytics_handler(**kw)),
        headers={"Authorization": "Bearer fake-token"},
    )


class FakeLLM:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls = []

    def chat_json(self, messages, schema):
        self.calls.append(messages)
        if self.error:
            raise self.error
        return schema(recommendations=["Publier plus de Shorts sur le format qui retient."])


def test_channel_report_collects_all_views():
    report = an.channel_report(PROFILE, days=90, client=_client())

    assert report.channel == "gaming" and report.days == 90
    assert report.totals["views"] == 1000
    assert [v["title"] for v in report.videos] == ["Meilleur format", "Moyen | tuto", "Raté"]
    assert report.videos[0]["url"] == "https://www.youtube.com/watch?v=a"
    assert {c["creatorContentType"] for c in report.content_types} == {"SHORTS", "VIDEO_ON_DEMAND"}
    assert report.traffic[0]["insightTrafficSourceType"] == "YT_SEARCH"
    assert report.thumbnail_ctr["disponible"] is False  # métrique refusée -> indisponible


def test_growth_advice_figures_come_from_data_without_llm():
    report = an.channel_report(PROFILE, days=90, client=_client())
    md = an.growth_advice(report, None)

    assert "Abonnés gagnés pour 1 000 vues : 25,00" in md
    assert "## Ce qui marche" in md and "Meilleur format" in md
    assert "## Ce qui ne marche pas" in md and "Raté" in md
    assert "indisponible" in md  # CTR d'impression
    assert "Rapport sans recommandations LLM" in md
    assert md_table(["t"], [["a | b"]]).splitlines()[-1] == "| a \\| b |"  # pipe échappé


def test_growth_advice_with_fake_llm_and_ctr_available():
    report = an.channel_report(PROFILE, days=90, client=_client(thumb_supported=True))
    llm = FakeLLM()
    md = an.growth_advice(report, llm)

    assert "Publier plus de Shorts" in md
    assert "taux de clic" in md
    (messages,) = llm.calls
    payload = json.loads(messages[1]["content"])
    assert "titre" in payload["ce_qui_marche"][0]
    assert "http" not in messages[1]["content"]


def test_growth_advice_llm_unavailable_keeps_report():
    report = an.channel_report(PROFILE, days=90, client=_client())
    md = an.growth_advice(report, FakeLLM(error=LLMUnavailableError("arrêté")))
    assert "LLM local injoignable" in md
    assert "## Ce qui marche" in md


def test_analytics_error_on_forbidden_is_actionable():
    def handler(request):
        return httpx.Response(403, json={"error": {"errors": [{"reason": "forbidden"}]}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(an.AnalyticsError, match="publish auth --logout"):
        an.channel_report(PROFILE, client=client)


def test_missing_analytics_scope_gives_logout_hint(tmp_path, monkeypatch):
    token = tmp_path / "youtube_token.json"
    token.write_text(
        json.dumps(
            {
                "token": "t",
                "scopes": [
                    "https://www.googleapis.com/auth/youtube.upload",
                    "https://www.googleapis.com/auth/youtube",
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(auth, "token_path", lambda: token)
    monkeypatch.setattr(auth, "load_credentials", lambda: SimpleNamespace(token="t"))

    with pytest.raises(MinavioError, match="minavio publish auth --logout"):
        an._authorized_token()


def test_token_with_analytics_scope_is_accepted(tmp_path, monkeypatch):
    token = tmp_path / "youtube_token.json"
    token.write_text(json.dumps({"token": "t", "scopes": [auth.ANALYTICS_SCOPE]}), encoding="utf-8")
    monkeypatch.setattr(auth, "token_path", lambda: token)
    monkeypatch.setattr(auth, "load_credentials", lambda: SimpleNamespace(token="fake-token"))
    assert an._authorized_token() == "fake-token"
    assert auth.ANALYTICS_SCOPE in auth.SCOPES
