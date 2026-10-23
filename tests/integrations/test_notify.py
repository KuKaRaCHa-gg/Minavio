"""Notifications (Discord, Telegram, ntfy) et crochet de fin de job du watch."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from typer.testing import CliRunner

from minavio import watch as watch_mod
from minavio.errors import MinavioError
from minavio.integrations.cli_trends import trends_app
from minavio.integrations.notify import notify
from minavio.watch import Watcher, notify_job


def _recorder(status: int = 200, requests: list | None = None) -> httpx.Client:
    seen = requests if requests is not None else []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json={"ok": True})

    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture
def thumb(tmp_path: Path) -> Path:
    path = tmp_path / "job_thumb1_youtube.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    return path


def test_discord_multipart_with_thumbnail(monkeypatch, thumb):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.test/api/webhooks/1/abc")
    reqs: list[httpx.Request] = []
    reached = notify(
        "Titre", "Corps", file=thumb, url="https://ex.test", client=_recorder(requests=reqs)
    )
    assert reached == ["discord"]
    req = reqs[0]
    assert str(req.url) == "https://discord.test/api/webhooks/1/abc"
    assert req.headers["content-type"].startswith("multipart/form-data")
    assert b'filename="job_thumb1_youtube.png"' in req.content
    assert b"attachment://job_thumb1_youtube.png" in req.content
    assert b"image/png" in req.content


def test_telegram_send_message_then_send_photo(monkeypatch, thumb):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:TOK")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-42")
    reqs: list[httpx.Request] = []
    assert notify("T", "M", client=_recorder(requests=reqs)) == ["telegram"]
    assert reqs[-1].url.path == "/bot123:TOK/sendMessage"
    assert json.loads(reqs[-1].content)["chat_id"] == "-42"

    assert notify("T", "M", file=thumb, client=_recorder(requests=reqs)) == ["telegram"]
    assert reqs[-1].url.path == "/bot123:TOK/sendPhoto"
    assert b'name="photo"' in reqs[-1].content
    assert b'name="caption"' in reqs[-1].content


def test_ntfy_default_server_and_optional_server(monkeypatch):
    monkeypatch.setenv("NTFY_TOPIC", "minavio-test")
    reqs: list[httpx.Request] = []
    assert notify("T", "M", url="https://ex.test", client=_recorder(requests=reqs)) == ["ntfy"]
    assert str(reqs[0].url) == "https://ntfy.sh/"
    body = json.loads(reqs[0].content)
    assert body == {
        "topic": "minavio-test",
        "title": "T",
        "message": "M",
        "click": "https://ex.test",
    }

    monkeypatch.setenv("NTFY_SERVER", "https://ntfy.example.test/")
    notify("T", "M", client=_recorder(requests=reqs))
    assert str(reqs[-1].url) == "https://ntfy.example.test/"


def test_all_three_channels_reached(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.test/hook")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:x")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "5")
    monkeypatch.setenv("NTFY_TOPIC", "t")
    reqs: list[httpx.Request] = []
    assert notify("T", "M", client=_recorder(requests=reqs)) == ["discord", "telegram", "ntfy"]
    assert len(reqs) == 3


def test_no_channel_configured_returns_empty_list():
    assert notify("T", "M", client=_recorder()) == []


def test_failure_never_raises_and_other_channels_still_sent(monkeypatch, caplog):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.test/hook")
    monkeypatch.setenv("NTFY_TOPIC", "t")

    def handler(request: httpx.Request) -> httpx.Response:
        if "discord" in request.url.host:
            return httpx.Response(500)
        raise httpx.ConnectError("boom")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with caplog.at_level("WARNING"):
        assert notify("T", "M", client=client) == []
    assert "discord" in caplog.text and "ntfy" in caplog.text
    assert "https://discord.test" not in caplog.text  # l'URL du webhook n'est jamais journalisée


def test_missing_thumbnail_is_ignored(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("NTFY_TOPIC", "t")
    assert notify("T", "M", file=tmp_path / "absent.png", client=_recorder()) == ["ntfy"]


def test_notify_job_success_attaches_thumbnail_and_reminder(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    thumb_dir = tmp_path / "out" / "bytetricks"
    thumb_dir.mkdir(parents=True)
    (thumb_dir / "job42_thumb1_youtube.png").write_bytes(b"png")
    calls = []
    monkeypatch.setattr(watch_mod, "notify", lambda *a, **k: calls.append((a, k)) or [])

    notify_job("bytetricks", "job42", outputs=[Path("out/bytetricks/job42_youtube.mp4")])

    ((args, kwargs),) = calls
    assert "job42" in args[1] and "minavio publish bytetricks job42" in args[1]
    assert kwargs["file"] == Path("out/bytetricks/job42_thumb1_youtube.png")


def test_notify_job_failure_uses_first_line_only(monkeypatch):
    calls = []
    monkeypatch.setattr(watch_mod, "notify", lambda *a, **k: calls.append((a, k)) or [])
    notify_job("bytetricks", None, error="Montage impossible\nTrace interne\nplus de détails")
    ((args, kwargs),) = calls
    assert "Erreur : Montage impossible" in args[1]
    assert "Trace interne" not in args[1]
    assert "minavio publish" not in args[1]
    assert kwargs["file"] is None


def test_notify_job_swallows_errors(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("réseau HS")

    monkeypatch.setattr(watch_mod, "notify", boom)
    assert notify_job("bytetricks", "j", outputs=[]) == []


def test_watcher_calls_notify_after_successful_job(monkeypatch, tmp_path: Path):
    calls = []
    monkeypatch.setattr(watch_mod, "notify", lambda *a, **k: calls.append(a) or [])
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"video")

    def run(profile_id, paths):
        return SimpleNamespace(job_id="job7", outputs=[Path("out/bytetricks/job7_youtube.mp4")])

    watcher = Watcher("bytetricks", tmp_path, run=run, state_path=tmp_path / "state.json")
    watcher._launch({str(clip): (clip.stat().st_size, clip.stat().st_mtime)})

    assert len(calls) == 1
    assert "bytetricks" in calls[0][0]
    assert "job7" in calls[0][1] and "job7_youtube.mp4" in calls[0][1]


def test_watcher_calls_notify_after_failed_job(monkeypatch, tmp_path: Path):
    calls = []
    monkeypatch.setattr(watch_mod, "notify", lambda *a, **k: calls.append(a) or [])
    monkeypatch.setattr(watch_mod, "_safe_job_id", lambda profile_id, paths: "jobX")
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"video")

    def run(profile_id, paths):
        raise MinavioError("FFmpeg a échoué\ndétails")

    watcher = Watcher("bytetricks", tmp_path, run=run, state_path=tmp_path / "state.json")
    watcher._launch({str(clip): (clip.stat().st_size, clip.stat().st_mtime)})

    assert len(calls) == 1
    assert "Erreur : FFmpeg a échoué" in calls[0][1]
    assert "jobX" in calls[0][1]


def test_notify_test_command_fails_without_channels():
    result = CliRunner().invoke(trends_app, ["notify-test"])
    assert result.exit_code == 1
    assert "DISCORD_WEBHOOK_URL" in result.output


def test_notify_test_command_reports_reached_channels(monkeypatch):
    monkeypatch.setattr("minavio.integrations.notify.notify", lambda *a, **k: ["ntfy"])
    result = CliRunner().invoke(trends_app, ["notify-test"])
    assert result.exit_code == 0
    assert "ntfy" in result.output
