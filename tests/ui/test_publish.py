"""Services de l'onglet Publication (sans Gradio, sans réseau)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from minavio.publish import auth, youtube
from minavio.ui import publish_services as ps


@pytest.fixture
def job(tmp_path, monkeypatch):
    monkeypatch.setenv("MINAVIO_SECRETS_FILE", str(tmp_path / "cfg" / "secrets.env"))
    work = tmp_path / "work" / "job1"
    out = tmp_path / "out" / "minivitch"
    work.mkdir(parents=True)
    out.mkdir(parents=True)
    (work / "detect.json").write_text("[]")
    (work / "copy.json").write_text(
        json.dumps({"title": "Titre", "hook": "h", "description": "Desc.", "tags": ["a"]})
    )
    (work / "chapters.txt").write_text("0:00 Intro")
    for n in ("youtube", "short1", "short2"):
        (out / f"job1_{n}.mp4").write_bytes(b"v")
    return SimpleNamespace(root=tmp_path, work=tmp_path / "work", out=tmp_path / "out")


@pytest.fixture
def no_network(monkeypatch):
    calls = []

    def boom():
        calls.append(1)
        raise AssertionError("réseau interdit")

    monkeypatch.setattr(youtube, "_build_service", boom)
    return calls


def test_video_choices_lists_rendered_shorts(job):
    assert ps.video_choices("minivitch", "job1", job.out) == [
        "youtube",
        "shorts",
        "all",
        "short1",
        "short2",
    ]
    assert ps.video_choices("minivitch", "absent", job.out) == ["youtube", "shorts", "all"]


def test_preview_shows_editable_fields_without_network(job, no_network):
    [r] = ps.preview("minivitch", "job1", work_dir=job.work, out_dir=job.out, schedule_at="now")
    assert r["title"] == "Titre" and r["tags"] == ["a"]
    assert r["description"] == "Desc.\n\n0:00 Intro"
    assert r["status"] == "dry_run" and no_network == []


def test_preview_applies_edits_for_one_video_only(job, no_network):
    kw = {"work_dir": job.work, "out_dir": job.out, "schedule_at": "now"}
    [r] = ps.preview("minivitch", "job1", title="Mon titre", tags="x, y\nz", **kw)
    assert r["title"] == "Mon titre" and r["tags"] == ["x", "y", "z"]
    shorts = ps.preview("minivitch", "job1", "shorts", title="Ignoré", **kw)
    assert len(shorts) == 2 and all("Ignoré" not in s["title"] for s in shorts)


def test_publish_requires_explicit_confirmation(job, no_network):
    msg = ps.run_publish("minivitch", "job1", work_dir=job.work, out_dir=job.out, dry_run=False)
    assert msg.startswith("❌") and "confirme" in msg
    assert no_network == []


def test_run_publish_dry_run_message(job, no_network):
    msg = ps.run_publish("minivitch", "job1", work_dir=job.work, out_dir=job.out, schedule_at="now")
    assert "rien n'a été envoyé" in msg and "dry run" in msg


def test_run_publish_confirmed_calls_service(job, monkeypatch):
    class Svc:
        def videos(self):
            return self

        def insert(self, **kw):
            self.kw = kw
            return SimpleNamespace(next_chunk=lambda: (None, {"id": "abc"}))

    svc = Svc()
    monkeypatch.setattr(youtube, "_build_service", lambda: svc)
    monkeypatch.setattr(youtube, "_media", lambda p, m, *, resumable: SimpleNamespace())
    msg = ps.run_publish(
        "minivitch",
        "job1",
        work_dir=job.work,
        out_dir=job.out,
        schedule_at="now",
        privacy="unlisted",
        dry_run=False,
        confirm=True,
    )
    assert "abc" in msg and svc.kw["body"]["status"]["privacyStatus"] == "unlisted"


def test_errors_become_messages(job, no_network):
    assert ps.run_publish("minivitch", "", work_dir=job.work, out_dir=job.out).startswith("❌")
    assert "introuvable" in ps.run_publish("minivitch", "zzz", work_dir=job.work, out_dir=job.out)


def test_profile_settings_and_auth_markdown(job):
    s = ps.profile_publish_settings("minivitch")
    assert s == {"privacy": "private", "schedule_at": ""}  # privée, non programmée
    assert "non configuré" in ps.auth_markdown()
    auth._write_private(auth.token_path(), '{"token": "SECRET"}')
    md = ps.auth_markdown()
    assert "connecté" in md and "SECRET" not in md


def test_connect_youtube_reports_errors(job):
    assert ps.connect_youtube(None).startswith("❌")
