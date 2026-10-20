from pathlib import Path

import pytest

from minavio.errors import MinavioError
from minavio.ui import services as svc


def test_build_run_args_options():
    args = svc.build_run_args(
        "minivitch",
        "~/clips",
        music="/m.mp3",
        formats=["shorts"],
        all_formats=["youtube", "shorts"],
        shorts=2,
        use_llm=False,
        chapters=False,
        resume=True,
        dry_run=True,
        out_dir="/out",
    )
    assert args[1:5] == ["-m", "minavio", "run", "minivitch"]
    assert args[5] == str(Path("~/clips").expanduser())
    for flag in ("--no-llm", "--no-chapters", "--resume", "--dry-run"):
        assert flag in args
    assert args[args.index("--format") + 1] == "shorts"
    assert args[args.index("--shorts") + 1] == "2"
    assert args[-2:] == ["--out", "/out"]


def test_build_run_args_all_formats_means_no_flag_and_errors():
    args = svc.build_run_args("x", "/c", formats=["a", "b"], all_formats=["b", "a"])
    assert "--format" not in args
    with pytest.raises(MinavioError, match="dossier"):
        svc.build_run_args("x", "  ")
    with pytest.raises(MinavioError, match="format"):
        svc.build_run_args("x", "/c", formats=[], all_formats=["a"])


def test_job_state_parses_cli_logs(tmp_path):
    video = tmp_path / "abc_youtube.mp4"
    video.write_bytes(b"x")
    st = svc.JobState()
    for line in [
        "12:00:00 INFO    job 908f7937e9 : 2 source(s), formats ['youtube']",
        "12:00:05 INFO    [detect] ok en 5.57s",
        "12:00:06 INFO    [transcribe] reprise depuis transcribe.json",
        "12:00:07 WARNING étape 'rank' ignorée : LLM injoignable",
        "12:01:00 INFO    [render_youtube] ok en 18.26s",
        "12:01:10 INFO    [short2_render] ok en 9.68s",
        str(video),
        "Erreur : boom",
    ]:
        st.feed(line)
    assert st.job_id == "908f7937e9"
    assert st.done == {"detect": "5.6 s", "transcribe": "reprise", "render": "18.3 s",
                       "short": "9.7 s"}  # fmt: skip
    assert st.warnings == ["étape 'rank' ignorée : LLM injoignable"]
    assert st.outputs == [video]
    assert st.error == "boom"
    assert "✅ Détection des moments" in st.progress_markdown()
    mid = svc.JobState()
    mid.feed("12:00:05 INFO    [detect] ok en 5.57s")
    assert "⏳ **Transcription…**" in mid.progress_markdown()  # étape suivante en cours


def test_step_key():
    assert svc._step_key("overlay_files_shorts") == "overlay_files"
    assert svc._step_key("render_youtube") == "render"
    assert svc._step_key("short1_timeline") == "short"
    assert svc._step_key("timeline") == "timeline"


def test_validate_profile_yaml():
    good = svc.read_profile_yaml("bytetricks")
    assert svc.validate_profile_yaml(good, "bytetricks") == ""
    assert "id: bytetricks" in svc.validate_profile_yaml(good, "autre")
    assert svc.validate_profile_yaml("a: [", "x").startswith("YAML invalide")
    bad = good.replace("target_duration_s: 300", "target_duration_s: -1")
    assert "target_duration_s" in svc.validate_profile_yaml(bad, "bytetricks")


def test_save_profile_yaml_refuses_invalid_and_backs_up(tmp_path):
    (tmp_path / "channels").mkdir()
    src = svc.read_profile_yaml("bytetricks")
    path = tmp_path / "channels" / "bytetricks.yaml"
    path.write_text(src)
    assert svc.save_profile_yaml("a: [", "bytetricks", tmp_path).startswith("❌")
    assert path.read_text() == src
    msg = svc.save_profile_yaml(src.replace("pacing: dynamic", "pacing: hyper"), "bytetricks",
                                tmp_path)  # fmt: skip
    assert msg.startswith("✅") and "pacing: hyper" in path.read_text()
    assert (tmp_path / "channels" / "bytetricks.yaml.bak").read_text() == src


def test_save_template(tmp_path):
    from PIL import Image

    game = tmp_path / "games" / "g"
    game.mkdir(parents=True)
    (game / "game.yaml").write_text(
        "id: g\nname: G\nui_templates:\n  - {name: kill, image: templates/kill.png, label: kill}\n"
    )
    assert svc.save_template("g", "kill", None, tmp_path).startswith("❌")
    assert svc.save_template("g", "kill", Image.new("RGB", (4, 4)), tmp_path).startswith("❌")
    big = Image.new("RGB", (1920, 1080))
    assert svc.save_template("g", "kill", big, tmp_path).startswith("⚠️")
    assert svc.save_template("g", "kill", Image.new("RGBA", (80, 40)), tmp_path).startswith("✅")
    assert (game / "templates" / "kill.png").is_file()
    assert svc.template_statuses("g", tmp_path)[0].exists


def test_count_clips(tmp_path):
    assert "Indique" in svc.count_clips(" ")
    assert svc.count_clips(str(tmp_path / "nope")).startswith("❌ Introuvable")
    assert svc.count_clips(str(tmp_path)).startswith("❌ Aucun clip")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.mp4").write_bytes(b"0" * 1000)
    assert svc.count_clips(str(tmp_path)).startswith("✅ 1 clip(s)")


def test_job_summary(tmp_path):
    work = tmp_path / "work" / "abc123"
    work.mkdir(parents=True)
    (work / "copy.json").write_text('{"title": "T", "hook": "H", "tags": ["a"]}')
    ch = tmp_path / "out" / "minivitch"
    ch.mkdir(parents=True)
    (ch / "abc123_chapters.txt").write_text("0:00 Intro")
    info = svc.job_summary("abc123", "out", "minivitch", tmp_path)
    assert info["title"] == "T" and info["chapters"] == "0:00 Intro"
    assert "**Titre** : T" in svc.summary_markdown(info)
    assert svc.job_summary(None, "out", "minivitch", tmp_path) == {}


def test_run_job_process_streams_and_cancel(tmp_path):
    import sys

    code = "import time\nprint('12:00:00 INFO    [detect] ok en 1.0s', flush=True)\ntime.sleep(30)"
    gen = svc.run_job_process([sys.executable, "-c", code], tmp_path)
    state, proc = next(gen)
    assert state.done == {"detect": "1.0 s"}
    svc.cancel(proc)
    *_, (state, proc) = gen
    assert state.returncode != 0


def test_app_builds():
    pytest.importorskip("gradio")
    from minavio.ui.app import build

    demo = build(Path.cwd(), Path.cwd() / "out")
    assert demo is not None


def test_clean_exit_without_any_step_is_a_failure(tmp_path):
    import sys

    *_, (state, _proc) = svc.run_job_process([sys.executable, "-c", "pass"], tmp_path)
    assert state.returncode == -1 and "aucune étape" in state.error


def test_python_m_minavio_runs_the_cli():
    import subprocess
    import sys

    out = subprocess.run([sys.executable, "-m", "minavio", "profiles", "list"],
                         capture_output=True, text=True, check=True)  # fmt: skip
    assert "minivitch" in out.stdout
