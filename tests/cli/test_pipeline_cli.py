import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from minavio import pipeline
from minavio.cli import app
from minavio.core.models import Clip, Highlight, Timeline, TranscriptSegment
from minavio.errors import LLMUnavailableError, MinavioError

runner = CliRunner()


class Fakes:
    def __init__(self, src: Path):
        self.src = src
        self.calls: list[str] = []
        self.fail: set[str] = set()
        self.highlights = [
            Highlight(source=src, start=1, end=5, score=0.5, label="kill", detector="t")
        ]

    def _rec(self, name):
        self.calls.append(name)
        if name in self.fail:
            if name in ("rank", "copy"):
                raise LLMUnavailableError("down")
            raise RuntimeError("boom")

    def table(self):
        def collect(inputs):
            self._rec("collect")
            return [self.src]

        def detect(sources, profile):
            self._rec("detect")
            return self.highlights

        def transcribe(source, language=None):
            self._rec("transcribe")
            return [TranscriptSegment(start=0, end=2, text="hi")]

        def beats(music):
            self._rec("beats")
            return [0.5, 1.0]

        def rank(h, t, profile):
            self._rec("rank")
            return h

        def copy(h, t, profile):
            self._rec("copy")
            data = {
                "title": "T",
                "hook": "Hook!",
                "description": "d",
                "tags": [],
                "popups": [{"at": 3.0, "text": "wow"}],
            }
            return SimpleNamespace(model_dump=lambda mode=None: data)

        def plan(h, profile, beats=None, music=None, transcript=None):
            self._rec("plan")
            return Timeline(
                profile_id=profile.id,
                fps=60,
                clips=[Clip(source=self.src, start=1, end=5)],
                captions=transcript or [],
            )

        def caps(captions, style):
            self._rec("captions")
            return []

        def ov(timeline, fmt, theme, out_dir):
            self._rec("render_overlays")
            return {}

        def render(timeline, fmt, out, profile, overlay_files=None):
            self._rec(f"render:{fmt.name}")
            out.write_bytes(b"x")
            return out

        return {
            "collect_sources": collect,
            "run_detectors": detect,
            "transcribe": transcribe,
            "detect_beats": beats,
            "rank_highlights": rank,
            "generate_copy": copy,
            "plan_timeline": plan,
            "build_caption_overlays": caps,
            "render_overlays": ov,
            "render_timeline": render,
        }


@pytest.fixture
def env(tmp_path, monkeypatch):
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"video")
    fakes = Fakes(src)
    table = fakes.table()
    monkeypatch.setattr(pipeline, "_api", lambda name: table[name])
    return fakes, tmp_path


def job(env, **kw):
    fakes, tmp = env
    return pipeline.run_job(
        "minivitch", [fakes.src], work_root=tmp / "work", out_dir=tmp / "out", **kw
    )


def test_step_order_and_json(env):
    fakes, _ = env
    res = job(env)
    assert fakes.calls[:7] == [
        "collect",
        "detect",
        "transcribe",
        "rank",
        "copy",
        "plan",
        "captions",
    ]
    for step in (
        "collect",
        "detect",
        "transcribe",
        "rank",
        "copy",
        "music",
        "timeline",
        "overlays",
    ):
        assert (res.work_dir / f"{step}.json").exists(), step
    assert len(res.outputs) == 2
    assert all(o.exists() and o.parent.name == "minivitch" for o in res.outputs)
    ov = json.loads((res.work_dir / "overlays.json").read_text())
    assert [o["template"] for o in ov] == ["title", "popup"]
    assert ov[0]["props"]["text"] == "Hook!"
    assert ov[0]["duration"] == 2.5


def test_resume_skips_steps(env):
    fakes, _ = env
    job(env)
    fakes.calls.clear()
    job(env, resume=True)
    assert fakes.calls == ["collect"]


def test_no_resume_recomputes(env):
    fakes, _ = env
    job(env)
    fakes.calls.clear()
    job(env)
    assert "detect" in fakes.calls


def test_dry_run_stops_after_timeline(env):
    fakes, _ = env
    res = job(env, dry_run=True)
    assert res.outputs == [] and res.timeline_path.exists()
    assert "plan" in fakes.calls
    assert not any(c.startswith("render") for c in fakes.calls)


def test_no_llm_skips_llm_steps(env):
    fakes, _ = env
    res = job(env, use_llm=False)
    assert "rank" not in fakes.calls and "copy" not in fakes.calls
    assert json.loads((res.work_dir / "overlays.json").read_text()) == []


def test_llm_failure_is_warning(env):
    fakes, _ = env
    fakes.fail = {"rank", "copy"}
    res = job(env)
    assert len(res.outputs) == 2
    assert any("rank" in w for w in res.warnings)


def test_asr_failure_is_warning(env):
    fakes, _ = env
    fakes.fail = {"transcribe"}
    res = job(env)
    assert any("transcribe" in w for w in res.warnings)
    assert res.outputs


def test_music_and_beats(env):
    fakes, tmp = env
    m = tmp / "m.mp3"
    m.write_bytes(b"a")
    job(env, music=m)
    assert "beats" in fakes.calls


def test_missing_music_errors(env):
    with pytest.raises(MinavioError, match="Musique introuvable"):
        job(env, music=Path("/nope.mp3"))


def test_no_highlights_error(env):
    fakes, _ = env
    fakes.highlights = []
    with pytest.raises(MinavioError, match="Aucun highlight"):
        job(env)


def test_required_step_failure_wrapped(env):
    fakes, _ = env
    fakes.fail = {"plan"}
    with pytest.raises(MinavioError, match="timeline"):
        job(env)


@pytest.mark.parametrize(
    ("wanted", "names"),
    [
        (None, ["youtube", "shorts"]),
        (["9:16"], ["shorts"]),
        (["youtube"], ["youtube"]),
        (["all"], ["youtube", "shorts"]),
    ],
)
def test_format_selection(env, wanted, names):
    fakes, _ = env
    job(env, formats=wanted)
    assert [c.split(":")[1] for c in fakes.calls if c.startswith("render:")] == names


def test_unknown_format(env):
    with pytest.raises(MinavioError, match="Format 'xx' inconnu"):
        job(env, formats=["xx"])


def test_cli_run_ok(env, monkeypatch):
    fakes, tmp = env
    monkeypatch.chdir(tmp)
    r = runner.invoke(app, ["run", "minivitch", str(fakes.src), "-f", "9:16", "--out", "o"])
    assert r.exit_code == 0, r.output
    assert "shorts.mp4" in r.output


def test_cli_dry_run(env, monkeypatch):
    fakes, tmp = env
    monkeypatch.chdir(tmp)
    r = runner.invoke(app, ["run", "minivitch", str(fakes.src), "--dry-run", "--no-llm"])
    assert r.exit_code == 0, r.output
    assert "timeline.json" in r.output


def test_cli_error_exit_1_no_traceback(env, monkeypatch):
    fakes, tmp = env
    monkeypatch.chdir(tmp)
    r = runner.invoke(app, ["run", "nope", str(fakes.src)])
    assert r.exit_code == 1
    assert "Traceback" not in r.output
    assert "introuvable" in r.output


def test_cli_games_list():
    r = runner.invoke(app, ["games", "list"])
    assert r.exit_code == 0
    assert "apex_legends" in r.output


@pytest.mark.parametrize(("ok", "code"), [(True, 0), (False, 1)])
def test_cli_llm_ping(monkeypatch, ok, code):
    mod = types.ModuleType("minavio.llm.client")
    mod.LocalLLM = SimpleNamespace(from_settings=lambda s: SimpleNamespace(ping=lambda: ok))
    monkeypatch.setitem(sys.modules, "minavio.llm.client", mod)
    assert runner.invoke(app, ["llm", "ping", "minivitch"]).exit_code == code
