from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import BaseModel

from minavio.config.schema import ChannelProfile, MemeSettings
from minavio.core.models import Clip, Highlight, Timeline, TranscriptSegment
from minavio.errors import LLMUnavailableError
from minavio.memes.library import refresh_index
from minavio.memes.plan import plan_memes

ROOT = Path(__file__).resolve().parents[2]
SRC = Path("/media/a.mp4")


def _profile(tmp_path: Path, **meme_over: Any) -> ChannelProfile:
    data = yaml.safe_load((ROOT / "configs/channels/minivitch.yaml").read_text())
    # style « exe » par défaut dans ces tests (le défaut du schéma est « light », densité /2).
    base = {
        "enabled": True,
        "style": "exe",
        "library_dir": str(tmp_path / "lib"),
        "density_per_min": 6,
    }
    base.update(meme_over)
    profile = ChannelProfile.model_validate(data)
    return profile.model_copy(update={"memes": MemeSettings.model_validate(base)})


def _library(tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    for name in ("sounds/hit.wav", "sounds/bruh.mp3", "gifs/bruh.gif"):
        path = lib / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
    refresh_index(lib)


def _timeline() -> Timeline:
    # 60 s rendues : 0-30 = source 0-30, 30-60 = source 60-90.
    return Timeline(
        profile_id="minivitch",
        fps=30,
        clips=[
            Clip(source=SRC, start=0, end=30),
            Clip(source=SRC, start=60, end=90),
        ],
    )


def _hl(start: float, label: str) -> Highlight:
    return Highlight(
        source=SRC, start=start, end=start + 3, score=0.8, label=label, detector="test"
    )


HIGHLIGHTS = [_hl(10.0, "kill"), _hl(65.0, "fail")]  # -> at 10.0 et 35.0
TRANSCRIPT = [TranscriptSegment(start=9, end=12, text="ça tombe", source=SRC)]


class _Resp(BaseModel):
    cues: list[Any]


class FakeLLM:
    def __init__(self, cues: list[Any] | None = None, error: Exception | None = None) -> None:
        self.cues = cues or []
        self.error = error
        self.calls = 0

    def chat_json(self, messages, schema):
        self.calls += 1
        if self.error:
            raise self.error
        return schema.model_validate({"cues": self.cues})


def _kinds(plan: dict) -> tuple[int, int, int]:
    return len(plan["sfx"]), len(plan["effects"]), len(plan["overlays"])


def test_llm_invalid_cues_are_dropped(tmp_path: Path) -> None:
    _library(tmp_path)
    profile = _profile(tmp_path)
    llm = FakeLLM(
        [
            {"type": "effect", "at": 10.0, "effect": "zoom_punch", "duration": 0.4},  # OK
            {"type": "sound", "at": 10.5, "tags": ["hit"]},  # OK -> SfxCue
            {"type": "sound", "at": 500.0, "tags": ["hit"]},  # hors timeline
            {"type": "explosion", "at": 11.0, "tags": ["hit"]},  # type inconnu
            {"type": "sound", "at": 12.0, "tags": ["licorne"]},  # aucun asset
            {"type": "effect", "at": 13.0, "effect": "deep_fry"},  # pas dans settings.effects
            {"type": "effect", "at": 14.0, "effect": "unknown_fx"},
            "pas un objet",
            {"type": "gif", "at": -1.0, "tags": ["bruh"]},  # négatif
        ]
    )

    plan = plan_memes(_timeline(), HIGHLIGHTS, TRANSCRIPT, profile, llm)

    assert llm.calls == 1
    assert [e.kind for e in plan["effects"]] == ["zoom_punch"]
    assert [s.sound for s in plan["sfx"]] == ["hit"]
    assert plan["sfx"][0].path == tmp_path / "lib" / "sounds" / "hit.wav"
    assert plan["overlays"] == []


def test_llm_media_cue_becomes_overlay_with_props(tmp_path: Path) -> None:
    _library(tmp_path)
    profile = _profile(tmp_path)
    llm = FakeLLM([{"type": "gif", "at": 35.0, "tags": ["bruh"], "duration": 1.5}])

    plan = plan_memes(_timeline(), HIGHLIGHTS, TRANSCRIPT, profile, llm)

    assert len(plan["overlays"]) == 1
    overlay = plan["overlays"][0]
    assert overlay.template == "media"
    assert overlay.start == 35.0 and overlay.duration == 1.5
    assert overlay.props["kind"] == "gif"
    assert overlay.props["path"].endswith("bruh.gif")
    assert {"x", "y", "w", "h"} <= set(overlay.props)


def test_density_is_respected(tmp_path: Path) -> None:
    _library(tmp_path)
    profile = _profile(tmp_path, density_per_min=1)  # 60 s -> 1 cue max
    many = [{"type": "effect", "at": float(t), "effect": "shake"} for t in range(1, 9)]

    plan = plan_memes(_timeline(), HIGHLIGHTS, TRANSCRIPT, profile, FakeLLM(many))

    assert _kinds(plan) == (0, 1, 0)
    assert plan["effects"][0].at == 1.0  # ordre temporel conservé


def test_light_style_halves_density_and_excludes_heavy_effects(tmp_path: Path) -> None:
    _library(tmp_path)
    profile = _profile(
        tmp_path,
        style="light",
        density_per_min=6,
        effects=["shake", "deep_fry", "glitch", "error_popup", "zoom_punch"],
    )
    cues = [{"type": "effect", "at": float(t), "effect": "shake"} for t in range(1, 6)]
    cues += [{"type": "effect", "at": 20.0, "effect": "deep_fry"}]
    cues += [{"type": "effect", "at": 21.0, "effect": "glitch"}]

    plan = plan_memes(_timeline(), HIGHLIGHTS, TRANSCRIPT, profile, FakeLLM(cues))

    # 6 cues/min halvés = 3 par minute sur 60 s ; deep_fry et glitch exclus.
    assert len(plan["effects"]) == 3
    assert all(e.kind == "shake" for e in plan["effects"])


def test_cue_without_matching_asset_is_dropped(tmp_path: Path) -> None:
    _library(tmp_path)
    profile = _profile(tmp_path)
    llm = FakeLLM([{"type": "image", "at": 10.0, "tags": ["absent"]}])

    plan = plan_memes(_timeline(), HIGHLIGHTS, TRANSCRIPT, profile, llm)

    assert _kinds(plan) == (0, 0, 0)


def test_heuristic_without_llm(tmp_path: Path) -> None:
    _library(tmp_path)
    profile = _profile(tmp_path)
    highlights = [_hl(10.0, "kill"), _hl(65.0, "fail"), _hl(20.0, "multi_kill")]

    plan = plan_memes(_timeline(), highlights, TRANSCRIPT, profile, use_llm=False)

    effects = {(e.kind, e.at) for e in plan["effects"]}
    assert ("zoom_punch", 10.0) in effects
    assert ("freeze", 35.0) in effects
    assert ("shake", 20.0) in effects and ("flash", 20.0) in effects
    sounds = {(s.sound, s.at) for s in plan["sfx"]}
    assert ("hit", 10.0) in sounds
    assert ("bruh", 35.0) in sounds
    assert all(s.path.suffix == ".wav" or s.path.name == "bruh.mp3" for s in plan["sfx"])


def test_unavailable_llm_falls_back_to_heuristic(tmp_path: Path) -> None:
    _library(tmp_path)
    profile = _profile(tmp_path)
    llm = FakeLLM(error=LLMUnavailableError("serveur éteint"))

    plan = plan_memes(_timeline(), HIGHLIGHTS, TRANSCRIPT, profile, llm)

    assert ("zoom_punch", 10.0) in {(e.kind, e.at) for e in plan["effects"]}


def test_disabled_memes_returns_empty_plan(tmp_path: Path) -> None:
    _library(tmp_path)
    profile = _profile(tmp_path, enabled=False)
    plan = plan_memes(_timeline(), HIGHLIGHTS, TRANSCRIPT, profile, use_llm=False)
    assert _kinds(plan) == (0, 0, 0)


@pytest.mark.parametrize("style", ["exe", "light"])
def test_heuristic_style_light_drops_forbidden_effects(tmp_path: Path, style: str) -> None:
    _library(tmp_path)
    profile = _profile(tmp_path, style=style, effects=["zoom_punch", "error_popup", "freeze"])
    plan = plan_memes(_timeline(), [_hl(10.0, "kill")], TRANSCRIPT, profile, use_llm=False)
    assert "error_popup" not in {e.kind for e in plan["effects"]}
    assert "zoom_punch" in {e.kind for e in plan["effects"]}
