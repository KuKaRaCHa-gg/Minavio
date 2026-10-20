from __future__ import annotations

import json
import time
from datetime import datetime

import pytest

from minavio.errors import MinavioError
from minavio.publish import youtube
from tests.publish.conftest import FakeHttpError, noise_png


def publish(profile, job, **kw):
    kw.setdefault("work_root", job.work_root)
    kw.setdefault("out_dir", job.root / "out")
    return youtube.publish_job(profile, "job1", **kw)


@pytest.fixture
def paris(monkeypatch):
    monkeypatch.setenv("TZ", "CET-1CEST,M3.5.0,M10.5.0/3")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def test_next_occurrence_is_utc_and_in_the_future(paris):
    assert youtube.next_occurrence("18:00", datetime(2026, 10, 20, 12, 0)) == "2026-10-20T16:00:00Z"
    assert youtube.next_occurrence("18:00", datetime(2026, 10, 20, 19, 0)) == "2026-10-21T16:00:00Z"
    assert youtube.next_occurrence("9:05", datetime(2026, 1, 10, 8, 0)) == "2026-01-10T08:05:00Z"
    with pytest.raises(MinavioError, match="HH:MM"):
        youtube.next_occurrence("25:00")


def test_long_video_request_body(profile, job, service, paris):
    profile.publish.extra_tags = ["Minivitch", "APEX"]
    profile.publish.made_for_kids = False
    [r] = publish(
        profile, job, privacy="public", schedule_at="18:00", now=datetime(2026, 10, 20, 12, 0)
    )
    assert r["status"] == "uploaded" and r["video_id"] == "vid1"
    (call,) = service.inserted()
    assert call["part"] == "snippet,status"
    sn, st = call["body"]["snippet"], call["body"]["status"]
    assert sn["title"] == "Clutch impossible sur Apex"
    assert sn["description"].endswith("0:00 Intro\n0:30 Le clutch\n1:10 Fin")
    assert "Un clutch fou." in sn["description"]
    assert sn["tags"] == ["apex", "clutch", "Minivitch"]  # dédupliqués sans casse
    assert sn["categoryId"] == "20" and sn["defaultLanguage"] == "fr"
    # programmée publique à 18:00 => envoyée private avec publishAt en UTC
    assert st == {
        "privacyStatus": "private",
        "selfDeclaredMadeForKids": False,
        "publishAt": "2026-10-20T16:00:00Z",
    }
    assert call["media_body"].resumable and call["media_body"].mimetype == "video/mp4"


def test_immediate_public_and_overrides(profile, job, service):
    [r] = publish(
        profile,
        job,
        privacy="public",
        schedule_at="now",
        overrides={"title": "Mon <titre>", "description": "Desc finale", "tags": ["a", "b"]},
    )
    body = service.inserted()[0]["body"]
    assert "publishAt" not in body["status"] and body["status"]["privacyStatus"] == "public"
    assert body["snippet"]["title"] == "Mon titre"
    assert body["snippet"]["description"] == "Desc finale"
    assert body["snippet"]["tags"] == ["a", "b"]
    assert r["privacy"] == "public"


def test_made_for_kids_playlist_and_thumbnail(profile, job, service):
    profile.publish.made_for_kids = True
    profile.publish.playlist_id = "PL123"
    [r] = publish(profile, job, schedule_at="now")
    assert service.inserted()[0]["body"]["status"]["selfDeclaredMadeForKids"] is True
    (thumb,) = service.inserted("thumbnails.set")
    assert thumb["videoId"] == "vid1"
    assert thumb["media_body"].path.endswith("job1_thumb1_youtube.png")  # 1re variante 16:9
    (pl,) = service.inserted("playlistItems.insert")
    assert pl["body"]["snippet"]["resourceId"]["videoId"] == "vid1"
    assert r["warnings"] == []


def test_idempotent_via_journal(profile, job, service):
    publish(profile, job, schedule_at="now")
    journal = json.loads((job.work / "publish.json").read_text())
    assert journal["videos"]["youtube"]["video_id"] == "vid1"
    assert "watch?v=vid1" in journal["videos"]["youtube"]["url"]
    [again] = publish(profile, job, schedule_at="now")
    assert again["status"] == "already_published" and again["video_id"] == "vid1"
    assert len(service.inserted()) == 1  # pas de second upload


def test_dry_run_never_touches_network(profile, job, monkeypatch):
    def boom():
        raise AssertionError("réseau interdit en dry-run")

    monkeypatch.setattr(youtube, "_build_service", boom)
    results = publish(profile, job, video="all", dry_run=True)
    assert [r["key"] for r in results] == ["youtube", "short1", "short2"]
    assert all(r["status"] == "dry_run" for r in results)
    assert not (job.work / "publish.json").exists()
    assert results[0]["thumbnail"].endswith("job1_thumb1_youtube.png")


def test_shorts_title_and_description(profile, job, service):
    results = publish(profile, job, video="shorts", schedule_at="now")
    assert [r["key"] for r in results] == ["short1", "short2"]
    sn = service.inserted()[0]["body"]["snippet"]
    assert sn["title"].endswith("(1) #Shorts") and len(sn["title"]) <= 100
    assert sn["description"] == "Un clutch fou.\n\n#Shorts"
    assert "Shorts" in sn["tags"]
    assert not service.inserted("thumbnails.set")  # pas de miniature pour les shorts


def test_single_short_selection_and_missing_video(profile, job, service):
    [r] = publish(profile, job, video="short2", schedule_at="now")
    assert r["key"] == "short2"
    with pytest.raises(MinavioError, match="Vidéo introuvable"):
        publish(profile, job, video="short9", schedule_at="now")
    with pytest.raises(MinavioError, match="inconnu"):
        publish(profile, job, video="nope")


def test_quota_exceeded_is_explained(profile, job, service):
    service.chunk_outcomes = [FakeHttpError(403, "quotaExceeded")]
    with pytest.raises(MinavioError, match=r"Quota YouTube épuisé.*100 unités"):
        publish(profile, job, schedule_at="now")
    assert not (job.work / "publish.json").exists()  # rien de journalisé : on peut relancer


def test_quota_exceeded_on_thumbnail_is_a_warning(profile, job, service):
    service.errors["thumbnails.set"] = FakeHttpError(403, "quotaExceeded")
    [r] = publish(profile, job, schedule_at="now")
    assert r["status"] == "uploaded" and "Quota" in r["warnings"][0]
    journal = json.loads((job.work / "publish.json").read_text())
    assert journal["videos"]["youtube"]["video_id"] == "vid1"  # la vidéo n'est pas renvoyée


def test_resume_after_5xx_and_network_errors(profile, job, service, sleeps):
    service.chunk_outcomes = [FakeHttpError(503), ConnectionError("reset"), "progress"]
    [r] = publish(profile, job, schedule_at="now")
    assert r["video_id"] == "vid1"
    assert sleeps == [2.0, 4.0]  # backoff exponentiel


def test_gives_up_after_three_failures(profile, job, service, sleeps):
    service.chunk_outcomes = [FakeHttpError(500)] * 3
    with pytest.raises(MinavioError, match="3 essais"):
        publish(profile, job, schedule_at="now")
    assert len(sleeps) == 2


def test_client_error_is_not_retried(profile, job, service, sleeps):
    service.chunk_outcomes = [FakeHttpError(400, "invalidTitle")]
    with pytest.raises(MinavioError, match="HTTP 400"):
        publish(profile, job, schedule_at="now")
    assert sleeps == []


def test_auth_error_points_to_publish_auth(profile, job, service):
    service.chunk_outcomes = [FakeHttpError(401, "authError")]
    with pytest.raises(MinavioError, match="minavio publish auth"):
        publish(profile, job, schedule_at="now")


def test_big_png_thumbnail_becomes_jpeg(profile, job, service):
    big = job.root / "out" / "minivitch" / "job1_thumb1_youtube.png"
    noise_png(big)
    assert big.stat().st_size > youtube.THUMB_MAX_BYTES
    publish(profile, job, schedule_at="now")
    (thumb,) = service.inserted("thumbnails.set")
    assert thumb["media_body"].mimetype == "image/jpeg"
    jpg = job.work / "publish_thumb.jpg"
    assert thumb["media_body"].path == str(jpg)
    assert 0 < jpg.stat().st_size <= youtube.THUMB_MAX_BYTES


def test_small_png_is_sent_as_is(profile, job, service):
    publish(profile, job, schedule_at="now")
    (thumb,) = service.inserted("thumbnails.set")
    assert thumb["media_body"].mimetype == "image/png"


def test_missing_job_and_copy(profile, job, service):
    with pytest.raises(MinavioError, match="introuvable"):
        youtube.publish_job(profile, "nope", work_root=job.work_root)
    (job.work / "copy.json").unlink()
    with pytest.raises(MinavioError, match=r"copy\.json"):
        publish(profile, job, dry_run=True)
    [r] = publish(profile, job, dry_run=True, overrides={"title": "À la main"})
    assert r["title"] == "À la main"


def test_chapters_fall_back_to_out_dir(profile, job):
    (job.work / "chapters.txt").unlink()
    (job.out / "job1_chapters.txt").write_text("0:00 Debut")
    [r] = publish(profile, job, dry_run=True)
    assert r["description"].endswith("0:00 Debut")


def test_schedule_requires_public(profile, job, service):
    # publishAt rendrait publique une vidéo demandée private/unlisted : refusé, rien n'est envoyé
    for privacy in ("private", "unlisted"):
        with pytest.raises(MinavioError, match="--privacy public"):
            publish(profile, job, privacy=privacy, schedule_at="18:00")
    assert not service.inserted()


def test_compilation_shorts_is_not_a_short(job):
    assert youtube.short_keys(job.out, "job1") == ["short1", "short2"]
