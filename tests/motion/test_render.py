import json
import subprocess
from pathlib import Path

import pytest

from minavio.config.schema import OutputFormat
from minavio.core.models import Clip, Overlay, Timeline
from minavio.errors import MotionUnavailableError
from minavio.motion import render

FMT = OutputFormat(name="shorts", aspect="9:16", width=1080, height=1920, fps=30)
THEME = {"primary": "#FF3B30", "accent": "#FFD60A"}


def _timeline(*overlays: Overlay, beats: list[float] | None = None) -> Timeline:
    return Timeline(
        profile_id="minivitch",
        fps=30,
        clips=[Clip(source=Path("a.mp4"), start=0, end=10)],
        overlays=list(overlays),
        beats=beats or [],
    )


@pytest.fixture
def fake_motion(tmp_path, monkeypatch):
    """Faux projet motion/ avec node_modules et npx présents ; capture les appels subprocess."""
    mdir = tmp_path / "motion"
    (mdir / "node_modules").mkdir(parents=True)
    monkeypatch.setenv("MINAVIO_MOTION_DIR", str(mdir))
    monkeypatch.setattr(render.shutil, "which", lambda name: f"/usr/bin/{name}")
    calls: list[dict] = []

    def fake_run(cmd, cwd=None, **kw):
        calls.append({"cmd": cmd, "cwd": cwd})
        if cmd[0] == "npx":
            Path(next(c for c in cmd if c.endswith(".mov"))).write_bytes(b"mov")
        elif cmd[0] == "ffmpeg":
            Path(cmd[-1]).write_bytes(b"mov")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(render.subprocess, "run", fake_run)
    return mdir, calls


def test_remotion_command_and_props(fake_motion, tmp_path):
    mdir, calls = fake_motion
    ov = Overlay(template="progress_bar", start=2.0, duration=1.5, props={"label": "HP"})
    out = render.render_overlays(
        _timeline(ov, beats=[1.0, 2.5, 3.0, 9.0]), FMT, THEME, tmp_path / "o"
    )
    assert list(out) == [0]
    cmd = calls[0]["cmd"]
    assert calls[0]["cwd"] == mdir
    assert cmd[:5] == ["npx", "--no-install", "remotion", "render", "src/index.ts"]
    assert cmd[5] == "progress-bar"
    assert "--codec=prores" in cmd
    assert "--prores-profile=4444" in cmd
    assert "--pixel-format=yuva444p10le" in cmd
    assert out[0].suffix == ".mov"
    props_arg = next(c for c in cmd if c.startswith("--props="))
    props = json.loads(Path(props_arg.split("=", 1)[1]).read_text())
    assert (props["width"], props["height"], props["fps"]) == (1080, 1920, 30)
    assert props["durationInFrames"] == 45
    assert props["theme"] == THEME
    assert props["beats"] == [0.5, 1.0]  # relatifs à l'overlay, hors fenêtre exclus
    assert props["label"] == "HP"


def test_cache_by_props_hash(fake_motion, tmp_path):
    _, calls = fake_motion
    ov = Overlay(template="popup", start=0, duration=1, props={"text": "+1 KILL"})
    tl = _timeline(ov)
    first = render.render_overlays(tl, FMT, THEME, tmp_path)
    second = render.render_overlays(tl, FMT, THEME, tmp_path)
    assert first == second
    assert len(calls) == 1
    other = Overlay(template="popup", start=0, duration=1, props={"text": "FUN FACT"})
    third = render.render_overlays(_timeline(other), FMT, THEME, tmp_path)
    assert third[0] != first[0]
    assert len(calls) == 2


def test_fallback_ffmpeg_for_title_and_captions_without_node(tmp_path, monkeypatch):
    monkeypatch.setenv("MINAVIO_MOTION_DIR", str(tmp_path / "absent"))
    monkeypatch.delenv("MINAVIO_FONT", raising=False)
    calls: list[list[str]] = []

    def fake_run(cmd, cwd=None, **kw):
        calls.append(cmd)
        Path(cmd[-1]).write_bytes(b"mov")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(render.subprocess, "run", fake_run)
    title = Overlay(template="title", start=0, duration=2, props={"text": "gg"})
    caps = Overlay(
        template="captions",
        start=1,
        duration=1,
        props={"words": [{"text": "a", "start": 0, "end": 0.5}], "position": "top"},
    )
    out = render.render_overlays(_timeline(title, caps), FMT, THEME, tmp_path / "o")
    assert set(out) == {0, 1}
    assert all(c[0] == "ffmpeg" for c in calls)
    vf = calls[0][calls[0].index("-vf") + 1]
    assert "drawtext" in vf
    assert "prores_ks" in calls[0]
    assert "between(t,0.000,0.500)" in calls[1][calls[1].index("-vf") + 1]


def test_other_templates_without_node(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("MINAVIO_MOTION_DIR", str(tmp_path / "absent"))
    tl = _timeline(Overlay(template="reaction", start=0, duration=1, props={"kind": "skull"}))
    assert render.render_overlays(tl, FMT, THEME, tmp_path / "o") == {}
    assert "ignoré" in caplog.text
    with pytest.raises(MotionUnavailableError, match="reaction"):
        render.render_overlays(tl, FMT, THEME, tmp_path / "o", strict=True)


def test_remotion_failure_falls_back_for_title(fake_motion, tmp_path, monkeypatch):
    def fake_run(cmd, cwd=None, **kw):
        if cmd[0] == "npx":
            return subprocess.CompletedProcess(cmd, 1, "", "boom")
        Path(cmd[-1]).write_bytes(b"mov")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(render.subprocess, "run", fake_run)
    tl = _timeline(Overlay(template="title", start=0, duration=1, props={"text": "x"}))
    assert 0 in render.render_overlays(tl, FMT, THEME, tmp_path / "o")
    tl2 = _timeline(Overlay(template="popup", start=0, duration=1))
    with pytest.raises(MotionUnavailableError, match="boom"):
        render.render_overlays(tl2, FMT, THEME, tmp_path / "o", strict=True)


def test_unknown_template(fake_motion, tmp_path):
    tl = _timeline(Overlay(template="nope", start=0, duration=1))
    assert render.render_overlays(tl, FMT, THEME, tmp_path) == {}
    with pytest.raises(MotionUnavailableError, match="inconnu"):
        render.render_overlays(tl, FMT, THEME, tmp_path, strict=True)


def test_new_templates_composition_ids(fake_motion, tmp_path):
    _, calls = fake_motion
    tl = _timeline(
        Overlay(template="kill_counter", start=0, duration=2, props={"kills": 3}),
        Overlay(template="rewind", start=1, duration=0.8, props={}),
        Overlay(template="rank_card", start=2, duration=1.8, props={"rank": 3, "total": 10}),
    )
    out = render.render_overlays(tl, FMT, THEME, tmp_path / "o")
    assert len(out) == 3
    ids = [c["cmd"][5] for c in calls]
    assert ids == ["kill-counter", "rewind", "rank-card"]


def test_kill_label_defaults_and_override():
    assert render.kill_label({"kills": 2}) == "DOUBLE KILL"
    assert render.kill_label({"kills": 5}) == "SQUAD WIPED"
    assert render.kill_label({"kills": 1, "label": "boom"}) == "BOOM"


def test_fallback_kill_counter_and_rank_card_without_node(tmp_path, monkeypatch):
    monkeypatch.setenv("MINAVIO_MOTION_DIR", str(tmp_path / "absent"))
    calls: list[list[str]] = []

    def fake_run(cmd, cwd=None, **kw):
        calls.append(cmd)
        Path(cmd[-1]).write_bytes(b"mov")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(render.subprocess, "run", fake_run)
    tl = _timeline(
        Overlay(template="kill_counter", start=0, duration=2, props={"kills": 3}),
        Overlay(template="rank_card", start=2, duration=1.8, props={"rank": 4, "total": 10}),
    )
    out = render.render_overlays(tl, FMT, THEME, tmp_path / "o")
    assert len(out) == 2 and all(c[0] == "ffmpeg" for c in calls)
    texts = sorted(p.read_text() for p in (tmp_path / "o").glob("*.txt"))
    assert texts == ["#4", "TRIPLE KILL"]


def test_rewind_without_node(tmp_path, monkeypatch):
    monkeypatch.setenv("MINAVIO_MOTION_DIR", str(tmp_path / "absent"))
    tl = _timeline(Overlay(template="rewind", start=0, duration=0.8, props={}))
    assert render.render_overlays(tl, FMT, THEME, tmp_path / "o") == {}
    with pytest.raises(MotionUnavailableError, match="rewind"):
        render.render_overlays(tl, FMT, THEME, tmp_path / "o", strict=True)
