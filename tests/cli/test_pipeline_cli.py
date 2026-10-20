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
from minavio.errors import FFmpegError, LLMUnavailableError, MinavioError

runner = CliRunner()


class Fakes:
    def __init__(self, src: Path):
        self.src = src
        self.calls: list[str] = []
        self.fail: set[str] = set()
        self.highlights = [
            Highlight(source=src, start=1, end=5, score=0.5, label="kill", detector="t")
        ]
        self.keep_spans = [(1.0, 4.0)]  # plages renvoyées par analyze_keep_spans
        self.autocut_inputs: list[tuple[Path, object, list | None]] = []
        self.plan_kwargs: dict = {}
        self.audio_calls: list[list[Path]] = []
        self.rendered: list = []  # timelines passées à render_timeline

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
            self.detect_profile = profile
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

        def autocut(source, settings, transcript=None):
            self._rec("autocut")
            self.autocut_inputs.append((source, settings, transcript))
            return list(self.keep_spans)

        def plan(h, profile, beats=None, music=None, transcript=None, keep_spans=None):
            self._rec("plan")
            self.plan_kwargs = {"keep_spans": keep_spans, "highlights": h}
            return Timeline(
                profile_id=profile.id,
                fps=60,
                clips=[Clip(source=self.src, start=1, end=5)],
                captions=transcript or [],
            )

        def audio_tracks(sources, profile, work_dir, warnings=None):
            self.audio_calls.append(list(sources))
            if "audio_clean" in self.fail:
                raise RuntimeError("demucs absent")
            out = {}
            for s in sources:
                clean = Path(work_dir) / f"{s.stem}.clean.wav"
                clean.write_bytes(b"wav")
                out[s] = clean
            return out

        def caps(captions, style):
            self._rec("captions")
            return []

        def ov(timeline, fmt, theme, out_dir):
            self._rec("render_overlays")
            return {}

        def sfx(timeline, settings):
            self._rec("sfx")
            return []

        def chapters(timeline, highlights, profile):
            self._rec("chapters")
            return []

        def format_chapters(chapters):
            return "\n".join(f"{c.start} {c.title}" for c in chapters)

        def render(timeline, fmt, out, profile, overlay_files=None):
            self._rec(f"render:{fmt.name}")
            self.rendered.append(timeline)
            out.write_bytes(b"x")
            return out

        return {
            "collect_sources": collect,
            "run_detectors": detect,
            "transcribe": transcribe,
            "analyze_keep_spans": autocut,
            "prepare_audio_tracks": audio_tracks,
            "detect_beats": beats,
            "rank_highlights": rank,
            "generate_copy": copy,
            "plan_timeline": plan,
            "plan_sfx": sfx,
            "generate_chapters": chapters,
            "format_chapters": format_chapters,
            "build_caption_overlays": caps,
            "render_overlays": ov,
            "render_timeline": render,
            "probe_source": lambda path: None,
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


def job_without_autocut(env, monkeypatch, **kw):
    """Charge le profil minivitch avec autocut désactivé (le profil livré l'active)."""
    from minavio import config as config_mod

    real_load = config_mod.load_profile

    def load(profile_id, *a, **k):
        p = real_load(profile_id, *a, **k)
        p.editing.autocut = p.editing.autocut.model_copy(update={"enabled": False})
        return p

    monkeypatch.setattr(config_mod, "load_profile", load)
    return job(env, **kw)


def test_step_order_and_json(env):
    fakes, _ = env
    res = job(env)
    assert fakes.calls[:8] == [
        "collect",
        "detect",
        "transcribe",
        "rank",
        "copy",
        "autocut",
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
        "autocut",
        "timeline",
        "overlays",
    ):
        assert (res.work_dir / f"{step}.json").exists(), step
    # 2 formats principaux + 1 short (un seul highlight dans les fakes)
    assert len(res.outputs) == 3
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
    assert len(res.outputs) == 3
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
    job(env, formats=wanted, shorts=0)
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


def test_autocut_skipped_when_disabled(env, monkeypatch):
    fakes, _ = env
    res = job_without_autocut(env, monkeypatch)
    assert "autocut" not in fakes.calls
    assert not (res.work_dir / "autocut.json").exists()
    assert fakes.plan_kwargs["keep_spans"] is None


def test_autocut_runs_when_enabled_and_writes_json(env):
    fakes, _ = env
    res = job(env)
    assert fakes.calls.index("transcribe") < fakes.calls.index("autocut")
    assert fakes.calls.index("autocut") < fakes.calls.index("plan")
    data = json.loads((res.work_dir / "autocut.json").read_text(encoding="utf-8"))
    assert data == {str(fakes.src): [[1.0, 4.0]]}


def test_autocut_gets_source_transcript_and_settings(env):
    fakes, _ = env
    job(env)
    assert len(fakes.autocut_inputs) == 1
    source, settings, transcript = fakes.autocut_inputs[0]
    assert source == fakes.src
    assert settings.enabled is True
    assert transcript and transcript[0].source == fakes.src


def test_autocut_without_transcript_passes_none(env):
    fakes, _ = env
    fakes.fail = {"transcribe"}
    job(env)
    assert fakes.autocut_inputs[0][2] is None


def test_keep_spans_passed_to_plan_timeline(env):
    fakes, _ = env
    job(env)
    assert fakes.plan_kwargs["keep_spans"] == {fakes.src: [(1.0, 4.0)]}


def test_autocut_failure_is_warning_and_continues(env):
    fakes, _ = env
    fakes.fail = {"autocut"}
    res = job(env)
    assert any("autocut" in w for w in res.warnings)
    assert fakes.plan_kwargs["keep_spans"] is None
    assert len(res.outputs) == 3
    assert not (res.work_dir / "autocut.json").exists()


def test_autocut_resume_reads_json(env):
    fakes, _ = env
    res = job(env)
    (res.work_dir / "timeline.json").unlink()  # forcer le recalcul du montage seul
    fakes.calls.clear()
    job(env, resume=True)
    assert "autocut" not in fakes.calls
    assert "plan" in fakes.calls
    assert fakes.plan_kwargs["keep_spans"] == {fakes.src: [(1.0, 4.0)]}


def test_render_unknown_encoder_healed_with_warning(env, monkeypatch):
    fakes, _ = env
    table = fakes.table()
    real_render = table["render_timeline"]
    seen: list[dict] = []

    def flaky(timeline, fmt, out, profile, overlay_files=None, **overrides):
        seen.append(dict(overrides))
        if not overrides:
            raise FFmpegError(
                "Commande échouée (code 1) : ffmpeg -c:v h264_nvenc\nUnknown encoder 'h264_nvenc'"
            )
        return real_render(timeline, fmt, out, profile, overlay_files=overlay_files)

    table["render_timeline"] = flaky
    monkeypatch.setattr(pipeline, "_api", lambda name: table[name])
    res = job(env)
    assert {"video_codec": "libx264"} in seen
    assert any("video_codec=libx264" in w for w in res.warnings)
    log = json.loads((res.work_dir / "healing.json").read_text(encoding="utf-8"))
    assert log[0]["category"] == "encoder"
