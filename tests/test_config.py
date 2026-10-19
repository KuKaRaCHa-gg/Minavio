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
    assert list_profiles() == ["bytetricks", "minivitch", "thelostchat"]
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
