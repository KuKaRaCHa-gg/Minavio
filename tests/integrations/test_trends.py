"""Veille de tendances : parsing des sources, clés absentes, cache, rapport avec/sans LLM."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from minavio.config import load_profile
from minavio.errors import LLMUnavailableError
from minavio.integrations import trends as tr
from minavio.integrations.cli_trends import trends_app

NOW = 1_800_000_000.0


def _client(routes: dict, calls: list | None = None) -> httpx.Client:
    """Client MockTransport : `routes` associe une sous-chaîne d'URL à (status, corps JSON)."""

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(str(request.url))
        for needle, (status, body) in routes.items():
            if needle in str(request.url):
                if isinstance(body, Exception):
                    raise body
                if body is None:
                    return httpx.Response(status, content=b"null")
                return httpx.Response(status, json=body)
        return httpx.Response(404, json={})

    return httpx.Client(
        transport=httpx.MockTransport(handler), headers={"User-Agent": tr.USER_AGENT}
    )


def _reddit_body(*titles: str) -> dict:
    children = [
        {
            "data": {
                "title": t,
                "score": 100 - i,
                "permalink": f"/r/x/comments/{i}/",
                "num_comments": 3,
            }
        }
        for i, t in enumerate(titles)
    ]
    return {"data": {"children": children}}


class FakeLLM:
    def __init__(self, reply: str = "1. **Idée** : angle\n") -> None:
        self.reply = reply
        self.messages: list[list[dict[str, str]]] = []

    def chat(self, messages, images=None) -> str:
        self.messages.append(messages)
        return self.reply


class BrokenLLM:
    def chat(self, messages, images=None) -> str:
        raise LLMUnavailableError("Qwen injoignable (test)")


@pytest.fixture
def bytetricks():
    profile = load_profile("bytetricks")
    return profile.model_copy(update={"llm": profile.llm.model_copy(update={"enabled": False})})


def test_reddit_parses_top_posts():
    client = _client({"/r/apexlegends/top.json": (200, _reddit_body("Titre A", "Titre B"))})
    items, note = tr._reddit(client, "apexlegends")
    assert note is None
    assert [i.title for i in items] == ["Titre A", "Titre B"]
    assert items[0].score == 100
    assert items[0].url == "https://www.reddit.com/r/x/comments/0/"


def test_reddit_403_gives_actionable_note_not_crash(bytetricks):
    client = _client({"/r/technology/top.json": (403, {})})
    report = tr.trend_report(
        bytetricks, client=client, cache_dir=None, sources=["reddit:technology"]
    )
    assert "REDDIT_CLIENT_ID/SECRET" in report
    assert "## r/technology (Reddit)" in report


def test_hn_parses_top_stories_and_skips_missing_items():
    routes = {
        "topstories.json": (200, [1, 2]),
        "item/1.json": (
            200,
            {"title": "Story 1", "url": "https://a.test", "score": 42, "descendants": 7},
        ),
        "item/2.json": (200, None),
    }
    items, _ = tr._hn(_client(routes))
    assert len(items) == 1
    assert items[0].title == "Story 1" and items[0].score == 42
    assert "7 commentaires" in items[0].detail


def test_steam_rising_games_sorted_by_gain():
    ranks = {
        "response": {
            "ranks": [
                {"rank": 1, "appid": 1, "last_week_rank": 1, "peak_in_game": 1000},
                {"rank": 5, "appid": 2, "last_week_rank": 12, "peak_in_game": 900},
                {"rank": 9, "appid": 3, "last_week_rank": 10, "peak_in_game": 800},
                {"rank": 20, "appid": 4, "last_week_rank": 2, "peak_in_game": 700},
            ]
        }
    }
    items, note = tr._steam(_client({"GetMostPlayedGames": (200, ranks)}))
    assert note is None
    assert [i.url.rsplit("/", 1)[1] for i in items] == ["2", "3"]
    assert "+7 places" in items[0].detail


def test_steam_fallback_when_most_played_fails():
    count = {"response": {"player_count": 1234}}
    client = _client({"GetMostPlayedGames": (500, {}), "GetNumberOfCurrentPlayers": (200, count)})
    items, note = tr._steam(client)
    assert items and items[0].score == 1234
    assert "repli" in note


def test_twitch_missing_keys_is_a_note_and_no_request(bytetricks):
    calls: list[str] = []
    client = _client({}, calls)
    report = tr.trend_report(
        bytetricks, client=client, cache_dir=None, sources=["twitch:clips:Apex Legends"]
    )
    assert "TWITCH_CLIENT_ID" in report
    assert calls == []


def test_twitch_clips_with_rights_note(monkeypatch, bytetricks):
    monkeypatch.setenv("TWITCH_CLIENT_ID", "cid")
    monkeypatch.setenv("TWITCH_CLIENT_SECRET", "sec")
    routes = {
        "id.twitch.tv/oauth2/token": (200, {"access_token": "tok"}),
        "helix/games?name=": (200, {"data": [{"id": "511", "name": "Apex Legends"}]}),
        "helix/clips": (
            200,
            {
                "data": [
                    {
                        "title": "Clip faible",
                        "url": "https://clips.test/a",
                        "view_count": 5,
                        "broadcaster_name": "Alice",
                    },
                    {
                        "title": "Clip fort",
                        "url": "https://clips.test/b",
                        "view_count": 500,
                        "broadcaster_name": "Bob",
                    },
                ]
            },
        ),
    }
    report = tr.trend_report(
        bytetricks, client=_client(routes), cache_dir=None, sources=["twitch:clips:Apex Legends"]
    )
    assert report.index("Clip fort") < report.index("Clip faible")
    assert "streamer : Bob" in report
    assert "Droits des clips" in report and "permission" in report


def test_cache_avoids_second_request_and_expires_after_6h(tmp_path: Path, bytetricks):
    calls: list[str] = []
    routes = {
        "topstories.json": (200, [1]),
        "item/1.json": (200, {"title": "T", "url": "https://x"}),
    }
    cache = tmp_path / "trends"
    tr.trend_report(
        bytetricks, client=_client(routes, calls), cache_dir=cache, sources=["hn"], now=NOW
    )
    first = len(calls)
    assert first == 2
    assert (cache / "hn.json").is_file()

    tr.trend_report(
        bytetricks, client=_client(routes, calls), cache_dir=cache, sources=["hn"], now=NOW + 3600
    )
    assert len(calls) == first  # encore frais : cache utilisé

    tr.trend_report(
        bytetricks,
        client=_client(routes, calls),
        cache_dir=cache,
        sources=["hn"],
        now=NOW + 6 * 3600 + 1,
    )
    assert len(calls) == first + 2  # expiré : nouvelle requête


def test_report_without_llm_has_no_invented_ideas(bytetricks):
    routes = {
        "topstories.json": (200, [1]),
        "item/1.json": (200, {"title": "Sujet", "url": "https://x"}),
    }
    report = tr.trend_report(bytetricks, client=_client(routes), cache_dir=None, sources=["hn"])
    assert "- [Sujet](https://x)" in report
    assert "LLM désactivé" in report
    assert "1. " not in report.split("## Idées")[1]


def test_report_with_fake_llm_adds_ideas_from_prompt(bytetricks):
    routes = {
        "topstories.json": (200, [1]),
        "item/1.json": (200, {"title": "Sujet", "url": "https://x"}),
    }
    llm = FakeLLM("1. **Idée test** : angle\n2. **Idée deux** : autre")
    report = tr.trend_report(
        bytetricks, llm=llm, client=_client(routes), cache_dir=None, sources=["hn"]
    )
    assert "Idée test" in report
    system = llm.messages[0][0]["content"]
    assert "stratège de contenu" in system
    assert "Sujet" in llm.messages[0][1]["content"]


def test_report_with_unavailable_llm_is_a_note(bytetricks):
    routes = {
        "topstories.json": (200, [1]),
        "item/1.json": (200, {"title": "Sujet", "url": "https://x"}),
    }
    report = tr.trend_report(
        bytetricks, llm=BrokenLLM(), client=_client(routes), cache_dir=None, sources=["hn"]
    )
    assert "LLM local indisponible" in report


def test_network_failure_and_unknown_source_never_raise(bytetricks):
    routes = {"topstories.json": (200, [1]), "item/1.json": (200, httpx.ConnectError("refused"))}
    report = tr.trend_report(
        bytetricks, client=_client(routes), cache_dir=None, sources=["hn", "source-inconnue"]
    )
    assert "source indisponible (ConnectError)" in report
    assert "source inconnue : source-inconnue" in report


def test_default_sources_per_profile_and_override(bytetricks, monkeypatch):
    assert tr.sources_for(bytetricks) == ["hn", "reddit:technology", "reddit:todayilearned"]
    monkeypatch.setitem(tr.DEFAULT_SOURCES, "bytetricks", ["hn"])
    assert tr.sources_for(bytetricks) == ["hn"]


def test_trends_command_writes_file(tmp_path: Path, monkeypatch):
    seen = {}

    def fake_report(profile, *args, **kwargs):
        seen["profile"] = profile.id
        return "# Rapport factice\n"

    monkeypatch.setattr(tr, "trend_report", fake_report)
    out = tmp_path / "t.md"
    result = CliRunner().invoke(trends_app, ["trends", "bytetricks", "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert seen["profile"] == "bytetricks"
    assert out.read_text(encoding="utf-8") == "# Rapport factice\n"


def test_trends_command_unknown_profile_exits_nonzero(tmp_path: Path):
    result = CliRunner().invoke(
        trends_app, ["trends", "profil-inexistant", "--out", str(tmp_path / "x.md")]
    )
    assert result.exit_code != 0


def test_json_cache_file_is_readable_text(tmp_path: Path, bytetricks):
    routes = {
        "topstories.json": (200, [1]),
        "item/1.json": (200, {"title": "T", "url": "https://x"}),
    }
    tr.trend_report(bytetricks, client=_client(routes), cache_dir=tmp_path, sources=["hn"], now=NOW)
    data = json.loads((tmp_path / "hn.json").read_text(encoding="utf-8"))
    assert data["items"][0]["title"] == "T"
