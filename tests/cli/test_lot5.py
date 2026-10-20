"""Lot 5 : étape audio_clean, profils livrés, commande `minavio publish`."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from minavio import config as config_mod
from minavio import pipeline
from minavio.cli import app
from minavio.config import list_profiles, load_profile
from tests.cli.test_pipeline_cli import Fakes

runner = CliRunner()


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


def with_profile(monkeypatch, mutate):
    real = config_mod.load_profile

    def load(profile_id, *a, **k):
        p = real(profile_id, *a, **k)
        mutate(p)
        return p

    monkeypatch.setattr(config_mod, "load_profile", load)


# --------------------------------------------------------------------------- audio_clean


def test_audio_clean_runs_after_collect_and_feeds_every_render(env):
    fakes, _ = env
    res = job(env)
    assert len(fakes.audio_calls) == 1
    data = json.loads((res.work_dir / "audio_tracks.json").read_text())
    assert list(data) == [str(fakes.src)]
    assert fakes.calls.index("collect") < fakes.calls.index("detect")
    # compilation (2 formats) + 1 short : toutes les timelines rendues portent les pistes propres
    assert len(fakes.rendered) == 3
    assert all(
        tl.audio_tracks == {fakes.src: res.work_dir / "clip.clean.wav"} for tl in fakes.rendered
    )
    assert not [w for w in res.warnings if "audio" in w]


def test_audio_clean_skipped_when_disabled(env, monkeypatch):
    fakes, _ = env

    def off(p):
        p.audio.separation.enabled = False
        p.audio.denoise.enabled = False

    with_profile(monkeypatch, off)
    res = job(env)
    assert fakes.audio_calls == []
    assert not (res.work_dir / "audio_tracks.json").exists()
    assert all(tl.audio_tracks == {} for tl in fakes.rendered)


def test_denoise_alone_triggers_audio_clean(env, monkeypatch):
    fakes, _ = env

    def only_denoise(p):
        p.audio.separation.enabled = False
        p.audio.denoise.enabled = True

    with_profile(monkeypatch, only_denoise)
    job(env)
    assert len(fakes.audio_calls) == 1


def test_audio_clean_failure_is_a_warning(env):
    fakes, _ = env
    fakes.fail.add("audio_clean")
    res = job(env)
    assert any("audio_clean" in w for w in res.warnings)
    assert res.outputs and all(tl.audio_tracks == {} for tl in fakes.rendered)


def test_audio_clean_resume_reuses_json_and_recomputes_when_files_vanish(env):
    fakes, _ = env
    res = job(env)
    job(env, resume=True)
    assert len(fakes.audio_calls) == 1
    (res.work_dir / "clip.clean.wav").unlink()
    job(env, resume=True)
    assert len(fakes.audio_calls) == 2


def test_audio_clean_not_run_on_dry_run(env):
    fakes, _ = env
    res = job(env, dry_run=True)
    assert fakes.audio_calls == [] and res.timeline_path.exists()


def test_chapters_copied_to_work_for_publish(env, monkeypatch):
    from minavio.core.models import Chapter

    fakes, _ = env
    table = fakes.table()
    table["generate_chapters"] = lambda tl, h, p: [Chapter(start=0, title="Intro")]
    monkeypatch.setattr(pipeline, "_api", lambda name: table[name])
    res = job(env)
    assert (res.work_dir / "chapters.txt").read_text() == "0.0 Intro"


# --------------------------------------------------------------------------- profils


def test_all_profiles_validate_via_cli():
    r = runner.invoke(app, ["profiles", "validate"])
    assert r.exit_code == 0, r.output
    for pid in list_profiles():
        assert f"ok    {pid}" in r.output


@pytest.mark.parametrize("pid", ["minivitch", "minivitch_top10"])
def test_minivitch_profiles_are_autonomous(pid):
    p = load_profile(pid)
    det = {d.name: d for d in p.detection}
    assert det["killfeed_ocr"].weight == 1.0
    assert det["motion_intensity"].weight == 0.6
    assert det["vision_scan"].weight == 0.8 and det["vision_scan"].params == {"mode": "candidates"}
    assert det["hype"].weight == 0.8 and "audio_peak" in det and "scene_change" in det
    assert det["template_match"].weight == 0.5
    assert p.editing.slowmo.enabled and p.editing.slowmo.factor == 0.5
    assert p.editing.slowmo.duration_s == 1.2
    assert p.audio.separation.enabled and p.audio.separation.remove_music
    assert p.publish.privacy == "private" and p.publish.category_id == "20"
    assert p.publish.schedule_at == "18:00"


def test_other_profiles():
    bt = load_profile("bytetricks")
    assert bt.audio.denoise.enabled and not bt.audio.separation.enabled
    assert {d.name: d.weight for d in bt.detection}["vision_scan"] == 0.5
    assert bt.publish.category_id == "28"
    tl = load_profile("thelostchat")
    det = {d.name: d.weight for d in tl.detection}
    assert det["vision_scan"] == 0.7 and det["motion_intensity"] == 0.4
    assert tl.publish.category_id == "24"


def test_schema_exports_publish_block():
    r = runner.invoke(app, ["schema"])
    assert r.exit_code == 0 and "PublishSettings" in r.output


# --------------------------------------------------------------------------- minavio publish


@pytest.fixture
def published(tmp_path, monkeypatch):
    work = tmp_path / "work" / "job1"
    out = tmp_path / "out" / "minivitch"
    work.mkdir(parents=True)
    out.mkdir(parents=True)
    (work / "copy.json").write_text(
        json.dumps({"title": "Titre", "hook": "h", "description": "Desc.", "tags": ["x"]})
    )
    (out / "job1_youtube.mp4").write_bytes(b"v")
    monkeypatch.setenv("MINAVIO_SECRETS_FILE", str(tmp_path / "cfg" / "secrets.env"))
    return ["--work", str(tmp_path / "work"), "--out", str(tmp_path / "out")]


def test_publish_dry_run_via_default_command(published):
    r = runner.invoke(app, ["publish", "minivitch", "job1", "--dry-run", "--at", "now", *published])
    assert r.exit_code == 0, r.output
    assert "[dry-run] youtube" in r.output and "Titre" in r.output and "private" in r.output


def test_publish_unknown_job_is_actionable(published):
    r = runner.invoke(app, ["publish", "minivitch", "nope", "--dry-run", *published])
    assert r.exit_code == 1 and "minavio run" in r.output


def test_publish_without_auth_points_to_auth_command(published):
    r = runner.invoke(app, ["publish", "minivitch", "job1", *published])
    assert r.exit_code == 1 and "minavio publish auth" in r.output


def test_publish_help_and_auth_status(published):
    assert runner.invoke(app, ["publish", "--help"]).exit_code == 0
    r = runner.invoke(app, ["publish", "auth", "--status"])
    assert r.exit_code == 0 and "non configuré" in r.output


def test_publish_auth_rejects_bad_client_secret(published, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{}")
    r = runner.invoke(app, ["publish", "auth", "--client-secret", str(bad)])
    assert r.exit_code == 1 and "Application de bureau" in r.output
