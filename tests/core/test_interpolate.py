import stat
import sys
from pathlib import Path

import pytest

from minavio.core import ffmpeg, interpolate

FAKE_RIFE = """#!{python}
import shutil, sys
from pathlib import Path

a = sys.argv[1:]
src, dst, n = Path(a[a.index("-i") + 1]), Path(a[a.index("-o") + 1]), int(a[a.index("-n") + 1])
frames = sorted(src.glob("*.png"))
for k in range(n):
    shutil.copy(frames[min(int(k * len(frames) / n), len(frames) - 1)], dst / f"{{k + 1:08d}}.png")
"""


@pytest.fixture
def fake_rife(tmp_path, monkeypatch) -> Path:
    """Faux rife-ncnn-vulkan (duplique les images au plus proche) : teste la plomberie."""
    exe = tmp_path / "fake-rife"
    exe.write_text(FAKE_RIFE.format(python=sys.executable))
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv(interpolate.RIFE_ENV, str(exe))
    return exe


def test_minterpolate_filter():
    f = interpolate.interpolation_filter(0.5, 30, "minterpolate")
    assert f == ("minterpolate=fps=60.000:mi_mode=mci:mc_mode=aobmc:vsbmc=1,setpts=PTS/0.50000")
    assert interpolate.interpolation_filter(0.25, 60, "minterpolate").startswith(
        "minterpolate=fps=240.000:"
    )


def test_none_and_rife_filters(monkeypatch):
    assert interpolate.interpolation_filter(0.5, 30, "none") == "setpts=PTS/0.50000"
    assert interpolate.interpolation_filter(0.5, 30, "rife") == ""  # vidéo déjà ralentie
    with pytest.raises(ValueError):
        interpolate.interpolation_filter(0.5, 30, "magic")


def test_auto_resolution(monkeypatch, fake_rife):
    assert interpolate.rife_available()
    assert interpolate.resolve_mode("auto") == "rife"
    monkeypatch.setenv(interpolate.RIFE_ENV, "/nonexistent/rife")
    assert not interpolate.rife_available()
    assert interpolate.resolve_mode("auto") == "minterpolate"
    assert interpolate.interpolation_filter(0.5, 30, "auto").startswith("minterpolate=")


def test_rife_slowmo_duration_and_cleanup(testsrc_video, fake_rife, tmp_path):
    dest = tmp_path / "work" / "slow.mp4"
    interpolate.rife_slowmo(testsrc_video, 1.0, 2.0, 0.5, dest)
    assert ffmpeg.probe(dest).duration == pytest.approx(2.0, abs=0.15)
    assert [p.name for p in dest.parent.iterdir()] == ["slow.mp4"]  # images temporaires supprimées


def test_rife_missing_is_actionable(testsrc_video, monkeypatch, tmp_path):
    monkeypatch.setenv(interpolate.RIFE_ENV, "/nonexistent/rife")
    with pytest.raises(Exception, match="MINAVIO_RIFE_BIN"):
        interpolate.rife_slowmo(testsrc_video, 0, 1, 0.5, tmp_path / "x.mp4")
