"""Détecteur chat_spike : 3 formats de log, absent, corrompu."""

import json
import logging

import pytest

from minavio.config.schema import ChannelProfile, DetectorRef
from minavio.core import detect
from tests.core.helpers import ff

DURATION = 40
# 1 message / 2 s en fond, puis 40 messages entre 20 s et 23 s
TIMES = [float(t) for t in range(0, DURATION, 2)] + [20 + i * 0.075 for i in range(40)]


def only(profile: ChannelProfile, **params) -> ChannelProfile:
    return profile.model_copy(update={"detection": [DetectorRef(name="chat_spike", params=params)]})


def fmt(t: float, with_hours: bool) -> str:
    s = int(t)
    return (
        f"[{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}]"
        if with_hours
        else (f"[{s // 60:02d}:{s % 60:02d}]")
    )


@pytest.fixture
def clip(tmp_path):
    out = tmp_path / "stream.mp4"
    ff(
        *("-f", "lavfi", "-i", f"color=c=black:s=160x90:r=5:d={DURATION}"),
        *("-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(out)),
    )
    return out


def check(hs, lag=4.0):
    assert len(hs) == 1
    h = hs[0]
    assert h.label == h.detector == "chat_spike"
    # pic de chat ~21.5 s -> la réaction se situe ~lag secondes plus tôt
    assert h.start < 21.5 - lag + 1 and h.end <= 21.5
    assert h.end > 21.5 - lag - 1
    assert 0 < h.score <= 1


def test_twitchdownloader_json(clip, profile):
    data = {"comments": [{"content_offset_seconds": t, "message": {"body": "x"}} for t in TIMES]}
    clip.with_name("stream.chat.json").write_text(json.dumps(data))
    check(detect.run_detectors([clip], only(profile)))


def test_chat_downloader_json(clip, profile):
    data = [{"time_in_seconds": t, "message": "x"} for t in TIMES]
    clip.with_name("stream.json").write_text(json.dumps(data))
    check(detect.run_detectors([clip], only(profile)))


@pytest.mark.parametrize("with_hours", [True, False])
def test_text_log(clip, profile, with_hours):
    lines = [f"{fmt(t, with_hours)} user{i}: kappa" for i, t in enumerate(sorted(TIMES))]
    clip.with_name("stream.chat.txt").write_text("\n".join(lines))
    check(detect.run_detectors([clip], only(profile)))


def test_lag_param(clip, profile):
    data = [{"time_in_seconds": t} for t in TIMES]
    clip.with_name("stream.json").write_text(json.dumps(data))
    hs = detect.run_detectors([clip], only(profile, lag_s=8))
    assert hs[0].end <= 21.5 - 8 + 3 + 0.5


def test_no_log_returns_empty_silently(clip, profile, caplog):
    caplog.set_level(logging.WARNING)
    assert detect.run_detectors([clip], only(profile)) == []
    assert not caplog.records


@pytest.mark.parametrize(
    ("name", "content"),
    [("stream.chat.json", "{not json"), ("stream.chat.txt", "rien de reconnaissable\nici")],
)
def test_corrupt_log_warns_and_returns_empty(clip, profile, caplog, name, content):
    caplog.set_level(logging.WARNING, logger="minavio.detect.chat_spike")
    clip.with_name(name).write_text(content)
    assert detect.run_detectors([clip], only(profile)) == []
    assert any("illisible" in r.message for r in caplog.records)


def test_uniform_chat_has_no_spike(clip, profile):
    data = [{"time_in_seconds": float(t)} for t in range(DURATION)]
    clip.with_name("stream.json").write_text(json.dumps(data))
    assert detect.run_detectors([clip], only(profile)) == []
