"""Effets « .exe », overlays média, SFX par chemin et limiteur anti-earrape."""

import re
import subprocess
from pathlib import Path

import pytest

from minavio.config import load_profile
from minavio.config.schema import OutputFormat
from minavio.core import effects, ffmpeg, render
from minavio.core.models import Clip, Effect, Overlay, SfxCue, Timeline
from tests.core.helpers import ff

KINDS = [
    "shake",
    "zoom_punch",
    "deep_fry",
    "glitch",
    "flash",
    "freeze",
    "bass_boost",
    "error_popup",
]


def fmt() -> OutputFormat:
    return OutputFormat(name="t", aspect="16:9", width=320, height=180, fps=30, reframe="none")


@pytest.fixture
def quiet_video(media_dir) -> Path:
    out = media_dir / "quiet_fx.mp4"
    ff(
        *("-f", "lavfi", "-i", "testsrc2=s=320x180:r=30:d=6"),
        *("-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"),
        *("-t", "6", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"),
        *("-c:a", "aac", str(out)),
    )
    return out


def mprofile():
    p = load_profile("minivitch_exe")
    p.editing.cold_open.enabled = False
    p.editing.slowmo.enabled = False
    p.audio.sfx.enabled = False
    p.shorts.per_highlight = 0
    return p


def _max_db(path: Path, start: float, dur: float) -> float:
    r = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-ss",
            str(start),
            "-t",
            str(dur),
            "-i",
            str(path),
            "-af",
            "volumedetect",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
    )
    m = re.search(r"max_volume: (-?[\d.]+|-inf) dB", r.stderr)
    assert m, r.stderr
    return float("-inf") if m.group(1) == "-inf" else float(m.group(1))


def _graph(effect: Effect) -> str:
    return ";".join(effects.video_graph([effect], "in", "out", 320, 180, 30))


def test_profile_exe_valid():
    p = load_profile("minivitch_exe")
    assert p.memes.enabled and p.memes.style == "exe" and p.theme_preset == "retro_pixel"
    assert "error_popup" in p.memes.effects


@pytest.mark.parametrize(
    ("kind", "needle"),
    [
        ("shake", "sin(t*55)"),
        ("zoom_punch", "eval=frame"),
        ("deep_fry", "unsharp"),
        ("glitch", "rgbashift"),
        ("flash", "brightness"),
    ],
)
def test_video_filter_strings_and_times(kind, needle):
    g = _graph(Effect(kind=kind, at=1.5, duration=0.5))
    assert needle in g
    assert "between(t,1.5000,2.0000)" in g
    assert g.startswith("[in]split") and g.endswith("[out]")


def test_freeze_and_audio_filters():
    g = _graph(Effect(kind="freeze", at=2, duration=1))
    assert "trim=end_frame=60" in g and "tpad=stop_mode=clone" in g and "concat=n=3" in g
    a = effects.audio_filters([Effect(kind="bass_boost", at=1, duration=2, intensity=1)])
    assert "bass=g=20.0:enable='between(t,1.0000,3.0000)'" in a and "volume=" in a
    assert effects.audio_filters([Effect(kind="shake", at=1)]) == ""
    assert effects.limiter_filter(-6.0).startswith("alimiter=limit=0.5012")


def test_render_every_effect_kind(quiet_video, tmp_path):
    t = Timeline(
        profile_id="p",
        fps=30,
        clips=[Clip(source=quiet_video, start=0, end=5)],
        effects=[Effect(kind=k, at=0.4 + 0.5 * i, duration=0.4) for i, k in enumerate(KINDS)],
    )
    out = render.render_timeline(t, fmt(), tmp_path / "o.mp4", mprofile())
    assert ffmpeg.probe(out).duration == pytest.approx(5.0, abs=0.2)


def _red_at(video: Path, at: float) -> bool:
    r = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-ss",
            str(at),
            "-i",
            str(video),
            "-frames:v",
            "1",
            "-vf",
            "crop=4:4:20:20,format=rgb24",
            "-f",
            "rawvideo",
            "-",
        ],
        capture_output=True,
    )
    px = r.stdout[:3]
    return px[0] > 200 and px[1] < 80


def test_gif_overlay_and_sfx_path(quiet_video, tmp_path):
    gif = tmp_path / "m.gif"
    ff("-f", "lavfi", "-i", "color=c=red:s=64x64:r=10:d=1", str(gif))
    beep = tmp_path / "beep.wav"
    ff("-f", "lavfi", "-i", "sine=f=800:d=0.4", str(beep))
    props = {"path": str(gif), "kind": "gif", "x": 0.0, "y": 0.0, "w": 0.5, "h": 0.5}
    t = Timeline(
        profile_id="p",
        fps=30,
        clips=[Clip(source=quiet_video, start=0, end=5)],
        overlays=[Overlay(template="media", start=1, duration=1.5, props=props)],
        sfx=[SfxCue(at=3.0, sound="inexistant", path=beep)],
    )
    out = render.render_timeline(t, fmt(), tmp_path / "o.mp4", mprofile())
    assert ffmpeg.probe(out).duration == pytest.approx(5.0, abs=0.2)
    assert _max_db(out, 3.0, 0.3) > _max_db(out, 4.0, 0.5) + 20  # bip à t=3
    assert _red_at(out, 1.5) and not _red_at(out, 3.5)


def test_limiter_caps_loud_sine(tmp_path):
    loud = tmp_path / "loud.mp4"
    ff(
        "-f", "lavfi", "-i", "testsrc2=s=320x180:r=30:d=3",
        "-f", "lavfi", "-i", "sine=f=200:d=3",
        "-af", "volume=4",
        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "pcm_s16le", str(loud),
    )  # fmt: skip
    p = mprofile()
    p.memes.max_true_peak_db = -6.0
    p.audio.loudness_lufs = -5  # loudnorm pousserait au-delà sans limiteur
    t = Timeline(profile_id="p", fps=30, clips=[Clip(source=loud, start=0, end=3)])
    out = render.render_timeline(t, fmt(), tmp_path / "o.mp4", p)
    assert _max_db(out, 0.5, 2.0) <= -6.0 + 1.0  # marge de l'encodage AAC
