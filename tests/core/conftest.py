"""Fixtures : médias synthétiques générés avec `ffmpeg -f lavfi` (aucun fichier versionné)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from minavio.config import load_profile
from minavio.config.schema import ChannelProfile
from tests.core.helpers import H, W, ff

if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
    pytest.skip("ffmpeg/ffprobe requis", allow_module_level=True)


@pytest.fixture(scope="session")
def media_dir(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("media")


@pytest.fixture(scope="session")
def testsrc_video(media_dir: Path) -> Path:
    """6 s, testsrc2 + sinus faible avec une rafale forte entre 3.0 s et 3.5 s."""
    out = media_dir / "testsrc.mp4"
    ff(
        *("-f", "lavfi", "-i", f"testsrc2=s={W}x{H}:r=30:d=6"),
        *("-f", "lavfi", "-i", "sine=f=440:d=6"),
        *("-af", "volume='if(between(t,3,3.5),1,0.02)':eval=frame"),
        *("-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac"),
        str(out),
    )
    return out


@pytest.fixture(scope="session")
def silent_video(media_dir: Path) -> Path:
    out = media_dir / "silent.mp4"
    ff(
        *("-f", "lavfi", "-i", f"testsrc2=s={W}x{H}:r=30:d=4"),
        *("-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"),
        str(out),
    )
    return out


@pytest.fixture(scope="session")
def flash_video(media_dir: Path) -> Path:
    """Fond noir 6 s avec un flash blanc entre 3.0 s et 3.2 s (2 changements de scène)."""
    out = media_dir / "flash.mp4"
    ff(
        *("-f", "lavfi", "-i", f"color=c=black:s={W}x{H}:r=30:d=6"),
        *("-vf", "drawbox=x=0:y=0:w=iw:h=ih:color=white:t=fill:enable='between(t,3,3.2)'"),
        *("-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"),
        str(out),
    )
    return out


@pytest.fixture(scope="session")
def music_file(media_dir: Path) -> Path:
    out = media_dir / "music.wav"
    ff(*("-f", "lavfi", "-i", "sine=f=220:d=3"), str(out))
    return out


@pytest.fixture(scope="session")
def click_track(media_dir: Path) -> Path:
    """Clics à 120 BPM (un toutes les 0.5 s) pendant 10 s."""
    out = media_dir / "clicks.wav"
    ff(
        *("-f", "lavfi", "-i", "aevalsrc='sin(2*PI*1500*t)*lt(mod(t,0.5),0.04)':s=22050:d=10"),
        str(out),
    )
    return out


@pytest.fixture(scope="session")
def alpha_overlay(media_dir: Path) -> Path:
    out = media_dir / "overlay.mov"
    ff(
        *("-f", "lavfi", "-i", "color=c=red@0.6:s=160x90:r=30:d=2,format=yuva444p"),
        *("-c:v", "qtrle"),
        str(out),
    )
    return out


@pytest.fixture
def profile() -> ChannelProfile:
    return load_profile("minivitch")
