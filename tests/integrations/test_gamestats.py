"""Statistiques de jeu : Apex, Riot, cache, erreurs, CLI (HTTP simulé, aucun appel réseau)."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from typer.testing import CliRunner

from minavio.errors import MinavioError
from minavio.integrations import gamestats
from minavio.integrations.cli_stats import stats_app

APEX_OK = {
    "global": {
        "name": "Minivitch",
        "level": 512,
        "rank": {"rankName": "Diamond", "rankDiv": 2, "rankScore": 12345},
    },
    "legends": {"selected": {"LegendName": "Wraith"}},
}


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("MINAVIO_SECRETS_FILE", str(tmp_path / "absent.env"))
    monkeypatch.setenv("APEX_API_KEY", "test-apex-key")
    monkeypatch.setenv("RIOT_API_KEY", "test-riot-key")
    monkeypatch.setattr(gamestats, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(gamestats, "_TRANSPORT", None)


def _mock(monkeypatch, handler) -> None:
    monkeypatch.setattr(gamestats, "_TRANSPORT", httpx.MockTransport(handler))


def test_apex_player_parsed_and_key_in_header_only(monkeypatch):
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=APEX_OK)

    _mock(monkeypatch, handler)
    p = gamestats.apex_player("Minivitch", "pc")

    assert (p.rank, p.division, p.rp, p.legend, p.level) == ("Diamond", 2, 12345, "Wraith", 512)
    assert seen[0].headers["Authorization"] == "test-apex-key"
    assert seen[0].url.params["platform"] == "PC"
    assert "test-apex-key" not in str(seen[0].url)


def test_apex_missing_fields_are_none_not_invented(monkeypatch):
    _mock(monkeypatch, lambda r: httpx.Response(200, json={"global": {"name": "X"}}))
    p = gamestats.apex_player("X")
    assert p.rank is None and p.rp is None and p.kills is None and p.damage is None


def test_stats_context_omits_absent_fields(monkeypatch):
    _mock(monkeypatch, lambda r: httpx.Response(200, json=APEX_OK))
    ctx = gamestats.stats_context(SimpleNamespace(display_name="Minivitch"), "Minivitch")
    assert "Rang : Diamant 2, 12 345 RP, légende : Wraith" in ctx
    assert "Kills" not in ctx and "Dégâts" not in ctx


def test_apex_missing_key_gives_actionable_error(monkeypatch):
    monkeypatch.delenv("APEX_API_KEY")
    with pytest.raises(MinavioError, match="APEX_API_KEY absente"):
        gamestats.apex_player("Minivitch")


@pytest.mark.parametrize(
    ("status", "expected"),
    [(403, "refusé"), (404, "introuvable"), (429, "quota"), (500, "HTTP 500")],
)
def test_apex_http_errors_are_minavio_errors(monkeypatch, status, expected):
    _mock(monkeypatch, lambda r: httpx.Response(status, json={}))
    with pytest.raises(MinavioError, match=expected):
        gamestats.apex_player("Minivitch")


def test_apex_error_payload_on_200(monkeypatch):
    _mock(monkeypatch, lambda r: httpx.Response(200, json={"Error": "Player not found"}))
    with pytest.raises(MinavioError, match="introuvable"):
        gamestats.apex_player("Nobody")


def test_apex_rejects_unknown_platform(monkeypatch):
    with pytest.raises(MinavioError, match="Plateforme Apex inconnue"):
        gamestats.apex_player("X", "switch")


def test_cache_avoids_second_call_and_expires(monkeypatch, tmp_path):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json=APEX_OK)

    _mock(monkeypatch, handler)
    gamestats.apex_player("Minivitch")
    gamestats.apex_player("Minivitch")
    assert calls["n"] == 1
    assert any((tmp_path / "cache").iterdir())

    monkeypatch.setattr(gamestats, "CACHE_TTL_S", 0)
    gamestats.apex_player("Minivitch")
    assert calls["n"] == 2


def test_apex_map_rotation(monkeypatch):
    body = {
        "battle_royale": {
            "current": {"map": "Olympus", "remainingTimer": "00:12:00"},
            "next": {"map": "Kings Canyon"},
        }
    }
    _mock(monkeypatch, lambda r: httpx.Response(200, json=body))
    rot = gamestats.apex_map_rotation()
    assert (rot.current_map, rot.next_map, rot.remaining) == ("Olympus", "Kings Canyon", "00:12:00")


def test_riot_account_parsed(monkeypatch):
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"puuid": "abc", "gameName": "Faker", "tagLine": "KR1"})

    _mock(monkeypatch, handler)
    acc = gamestats.riot_account("Faker", "KR1", "europe")
    assert (acc.puuid, acc.game_name, acc.tag_line) == ("abc", "Faker", "KR1")
    assert seen[0].url.host == "europe.api.riotgames.com"
    assert seen[0].headers["X-Riot-Token"] == "test-riot-key"


def test_riot_bad_region(monkeypatch):
    with pytest.raises(MinavioError, match="Région Riot inconnue"):
        gamestats.riot_account("Faker", "KR1", "mars")


def test_riot_missing_key(monkeypatch):
    monkeypatch.delenv("RIOT_API_KEY")
    with pytest.raises(MinavioError, match="RIOT_API_KEY absente"):
        gamestats.riot_account("Faker", "KR1")


def test_riot_last_lol_match(monkeypatch):
    def handler(request):
        if request.url.path.endswith("/ids"):
            return httpx.Response(200, json=["EUW1_1"])
        body = {
            "info": {
                "participants": [
                    {
                        "puuid": "other",
                        "championName": "Zed",
                        "kills": 1,
                        "deaths": 1,
                        "assists": 0,
                        "win": False,
                    },
                    {
                        "puuid": "abc",
                        "championName": "Ahri",
                        "kills": 5,
                        "deaths": 2,
                        "assists": 7,
                        "win": True,
                    },
                ]
            }
        }
        return httpx.Response(200, json=body)

    _mock(monkeypatch, handler)
    m = gamestats.riot_last_lol_match("abc", "europe")
    assert m is not None
    assert (m.champion, m.kda, m.win) == ("Ahri", "5/2/7", True)


def test_valorant_degrades_with_dev_key(monkeypatch):
    _mock(monkeypatch, lambda r: httpx.Response(403, json={}))
    assert gamestats.riot_valorant_last_match_id("abc", "europe") is None


def test_cli_apex_prints_context(monkeypatch):
    _mock(monkeypatch, lambda r: httpx.Response(200, json=APEX_OK))
    result = CliRunner().invoke(stats_app, ["apex", "Minivitch", "--platform", "PC"])
    assert result.exit_code == 0, result.output
    assert "Rang : Diamant 2" in result.output


def test_cli_apex_missing_key_exits_nonzero(monkeypatch):
    monkeypatch.delenv("APEX_API_KEY")
    result = CliRunner().invoke(stats_app, ["apex", "Minivitch"])
    assert result.exit_code == 1
    assert "APEX_API_KEY" in result.output


def test_cli_riot_requires_tag(monkeypatch):
    result = CliRunner().invoke(stats_app, ["riot", "Faker"])
    assert result.exit_code == 1
    assert "Pseudo#TAG" in result.output
