"""Fiches par genre : un profil PvE reçoit soulslike, pas le FPS ; Apex garde battle_royale."""

from __future__ import annotations

import socket
from pathlib import Path

import pytest
import yaml

from minavio.config.schema import ChannelProfile
from minavio.llm import kb, vision_scan

ROOT = Path(__file__).resolve().parents[2]
QUERY = "boss you died mort victoire killfeed kill squad wipe clutch"


def apex_profile() -> ChannelProfile:
    return ChannelProfile.model_validate(
        yaml.safe_load((ROOT / "configs/channels/minivitch.yaml").read_text())
    )


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("MINAVIO_KNOWLEDGE_DIR", raising=False)
    monkeypatch.delenv("MINAVIO_KB", raising=False)
    monkeypatch.setattr(
        socket.socket, "connect", lambda *a, **k: (_ for _ in ()).throw(AssertionError("réseau"))
    )
    kb.clear_game_cache()
    yield
    kb.clear_game_cache()


@pytest.fixture
def pve_profile(tmp_path, monkeypatch) -> ChannelProfile:
    cfg = tmp_path / "cfg"
    game = cfg / "games" / "elden_test"
    game.mkdir(parents=True)
    (game / "game.yaml").write_text(
        yaml.safe_dump(
            {
                "id": "elden_test",
                "name": "Elden Test",
                "genre": "soulslike",
                "modes": ["solo", "pve"],
            }
        )
    )
    monkeypatch.setenv("MINAVIO_CONFIG_DIR", str(cfg))
    p = apex_profile().model_copy(update={"games": ["elden_test"]})
    return p


def retrieve(p: ChannelProfile, task: str = "rank") -> str:
    return kb.retrieve(QUERY, profile=p, task=task, budget_tokens=6000)


def test_pve_profile_gets_soulslike_not_fps(pve_profile):
    out = retrieve(pve_profile)
    assert "Soulslike et combats de boss" in out
    assert "FPS et battle royale" not in out
    assert "Apex Legends -" not in out


def test_apex_still_gets_battle_royale():
    out = retrieve(apex_profile())
    assert "FPS et battle royale" in out
    assert "Soulslike et combats de boss" not in out


def test_unknown_or_invalid_game_is_ignored(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg2"
    bad = cfg / "games" / "bad"
    bad.mkdir(parents=True)
    (bad / "game.yaml").write_text("id: bad\nname: X\ngenre: not_a_genre\n")
    monkeypatch.setenv("MINAVIO_CONFIG_DIR", str(cfg))
    p = apex_profile().model_copy(update={"games": ["bad", "missing"]})
    out = retrieve(p)
    assert "FPS et battle royale" not in out and "Soulslike" not in out
    assert kb.game_traits(p) == []


def test_generic_gaming_sheet_without_genre_stays_generic(tmp_path, monkeypatch):
    d = tmp_path / "ud"
    d.mkdir()
    (d / "general.md").write_text(
        "<!-- kb: content_type=gaming -->\n# Général\n\n## Bonne réaction\nCris et rires.\n"
    )
    monkeypatch.setenv("MINAVIO_KNOWLEDGE_DIR", str(d))
    p = apex_profile().model_copy(update={"games": []})
    assert "Bonne réaction" in kb.retrieve(
        "réaction cris", profile=p, task="rank", budget_tokens=3000
    )


def test_mode_directive(pve_profile, monkeypatch):
    doc = "<!-- kb: content_type=gaming; mode=coop -->\n# C\n\n## Coop seulement\ncoop truc\n"
    (p,) = kb.parse_document("x.md", doc)
    assert p.modes == {"coop"}
    assert not kb.applies(p, pve_profile, "rank")  # modes [solo, pve]
    monkeypatch.delenv("MINAVIO_CONFIG_DIR")
    kb.clear_game_cache()
    assert kb.applies(p, apex_profile(), "rank")  # apex : pvp, coop


def test_kb_disabled(monkeypatch, pve_profile):
    monkeypatch.setenv("MINAVIO_KB", "0")
    assert retrieve(pve_profile) == ""
    assert retrieve(apex_profile()) == ""
    assert kb.knowledge_for(pve_profile, "vision_scan", QUERY) == ""


def test_every_genre_sheet_is_indexed_with_genre():
    by_src = {p.source: p for p in kb.builtin_passages()}
    for name in (
        "soulslike",
        "horror",
        "coop_pve",
        "action_adventure",
        "roguelike",
        "sandbox",
        "moba",
        "fighting",
        "sports",
        "extraction_shooter",
        "tactical_fps",
    ):
        assert by_src[f"genres/{name}.md"].genres, name
    assert "soulslike" not in by_src["games/fps_battle_royale.md"].genres
    assert "battle_royale" in by_src["games/fps_battle_royale.md"].genres


def test_genre_query_and_vision_system(pve_profile):
    assert "boss" in kb.genre_query(pve_profile)
    system = vision_scan._system(pve_profile, None)
    assert (
        "Soulslike" in system and "killfeed" not in system.split("Connaissances utiles")[0].lower()
    )


def test_vision_events_accept_pve_labels():
    v = vision_scan.VisionVerdict(excitement=8, events=["boss_kill", "bogus"], kills_visible=0)
    assert v.events == ["boss_kill"]
    h = vision_scan._to_highlight(Path("a.mp4"), 10.0, v, 60.0, 3.0, 1.5)
    assert h.label == "boss_kill"
