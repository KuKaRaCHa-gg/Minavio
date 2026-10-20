from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from minavio.cli import app
from minavio.config import list_profiles, load_profile
from minavio.config.schema import OutputFormat
from minavio.core.models import Clip, Timeline
from minavio.errors import ConfigError


def test_three_channel_profiles_are_valid():
    assert list_profiles() == ["bytetricks", "minivitch", "minivitch_top10", "thelostchat"]
    for pid in list_profiles():
        profile = load_profile(pid)
        assert {o.aspect for o in profile.outputs} == {"16:9", "9:16"}


def test_unknown_profile_lists_available():
    with pytest.raises(ConfigError, match="Disponibles : bytetricks"):
        load_profile("nope")


def test_env_overrides_llm(monkeypatch):
    monkeypatch.setenv("MINAVIO_LLM_MODEL", "qwen-test")
    monkeypatch.setenv("MINAVIO_LLM_BASE_URL", "http://gpu-box:8000/v1")
    llm = load_profile("minivitch").llm
    assert (llm.model, llm.base_url) == ("qwen-test", "http://gpu-box:8000/v1")


def test_output_aspect_mismatch_rejected():
    with pytest.raises(ValidationError, match="ne correspond pas"):
        OutputFormat(name="x", aspect="9:16", width=1920, height=1080)


def test_timeline_roundtrip_and_duration():
    tl = Timeline(
        profile_id="minivitch",
        fps=60,
        clips=[Clip(source=Path("a.mp4"), start=10, end=14, speed=2.0)],
    )
    assert tl.duration == 2.0
    assert Timeline.model_validate_json(tl.model_dump_json()) == tl
    with pytest.raises(ValidationError):
        Clip(source=Path("a.mp4"), start=5, end=5)


def test_cli_validate():
    result = CliRunner().invoke(app, ["profiles", "validate"])
    assert result.exit_code == 0, result.output
    assert "ok    thelostchat" in result.output


def test_timeline_moments_group_autocut_fragments():
    a, b = Path("a.mp4"), Path("b.mp4")
    tl = Timeline(
        profile_id="t",
        fps=30,
        cold_open_clips=1,
        clips=[
            Clip(source=b, start=20, end=22, highlight_label="kill"),  # cold open
            Clip(source=a, start=10, end=12, highlight_label="kill"),
            Clip(source=a, start=12.5, end=14, highlight_label="kill"),  # fragment autocut
            Clip(source=b, start=18, end=24, highlight_label="kill"),
        ],
    )
    ms = tl.moments()
    assert [(m.first_clip, m.last_clip, m.cold_open) for m in ms] == [
        (0, 0, True),
        (1, 2, False),
        (3, 3, False),
    ]
    assert (ms[1].start, ms[1].end) == (2.0, 5.5)
