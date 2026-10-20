"""Détecteur motion_intensity : vidéos lavfi calme vs secousses / flash / dégâts rouges."""

from __future__ import annotations

from pathlib import Path

import pytest

from minavio.config.schema import ChannelProfile, DetectorRef
from minavio.core import detect
from tests.core.helpers import ff

pytest.importorskip("cv2")

BASE = "smptebars=s=480x270:r=20:d=12"
ENC = ("-c:v", "libx264", "-preset", "ultrafast", "-crf", "18", "-pix_fmt", "yuv420p")


def _make(out: Path, vf: str | None) -> Path:
    args = ["-f", "lavfi", "-i", BASE]
    if vf:
        args += ["-vf", vf]
    ff(*args, *ENC, str(out))
    return out


@pytest.fixture(scope="module")
def calm(tmp_path_factory) -> Path:
    return _make(tmp_path_factory.mktemp("m") / "calm.mp4", None)


@pytest.fixture(scope="module")
def shaky(tmp_path_factory) -> Path:
    jitter = "between(t,6,7)*14*sin(t*90)"
    vf = f"crop=w=iw-60:h=ih-60:x='30+{jitter}':y='30+{jitter}*0.7'"
    return _make(tmp_path_factory.mktemp("m") / "shaky.mp4", vf)


@pytest.fixture(scope="module")
def flashy(tmp_path_factory) -> Path:
    vf = "drawbox=x=0:y=0:w=iw:h=ih:color=white:t=fill:enable='between(t,6,6.15)'"
    return _make(tmp_path_factory.mktemp("m") / "flash.mp4", vf)


@pytest.fixture(scope="module")
def reddish(tmp_path_factory) -> Path:
    vf = "drawbox=x=0:y=0:w=iw:h=ih:color=red@0.6:t=fill:enable='between(t,6,6.6)'"
    return _make(tmp_path_factory.mktemp("m") / "red.mp4", vf)


def _run(src: Path, profile: ChannelProfile):
    p = profile.model_copy(update={"detection": [DetectorRef(name="motion_intensity")]})
    return detect.run_detectors([src], p)


def test_registered():
    assert "motion_intensity" in detect.DETECTORS


def test_calm_video_has_no_action(calm, profile):
    assert _run(calm, profile) == []


def test_shake_peaks_at_the_right_time(shaky, profile):
    (h,) = _run(shaky, profile)
    assert h.label == "action" and h.detector == "motion_intensity"
    assert 6.0 <= h.signals["peak_t"] <= 7.0
    assert h.start <= 6.0 and h.end >= 7.0
    assert h.signals["shake_z"] > 2 and h.signals["flow_z"] > 2


def test_flash_peaks_at_the_right_time(flashy, profile):
    (h,) = _run(flashy, profile)
    assert 5.5 <= h.signals["peak_t"] <= 6.5
    assert h.signals["flash_z"] > 2


def test_red_flash_has_red_signal(reddish, profile):
    (h,) = _run(reddish, profile)
    assert 5.5 <= h.signals["peak_t"] <= 7.0
    assert h.signals["red_z"] > 2


def test_robust_z_floor_keeps_flat_series_flat():
    from minavio.core.detect.motion_intensity import robust_z

    z = robust_z([0.0] * 20, 2.0)
    assert float(abs(z).max()) == 0.0
    z = robust_z([0.0] * 19 + [1.0], 2.0)  # variation sous le plancher : pas de pic
    assert float(z.max()) < 1
