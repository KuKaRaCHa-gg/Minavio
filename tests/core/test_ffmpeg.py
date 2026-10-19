from pathlib import Path

import pytest

from minavio.core import ffmpeg
from minavio.errors import FFmpegError


def test_probe(testsrc_video: Path, silent_video: Path):
    info = ffmpeg.probe(testsrc_video)
    assert (info.width, info.height) == (320, 180)
    assert info.duration == pytest.approx(6, abs=0.2)
    assert info.fps == pytest.approx(30, abs=0.5)
    assert info.has_audio
    assert not ffmpeg.probe(silent_video).has_audio


def test_probe_missing_file(tmp_path: Path):
    with pytest.raises(FFmpegError, match="introuvable"):
        ffmpeg.probe(tmp_path / "nope.mp4")


def test_run_error_has_truncated_stderr(tmp_path: Path):
    with pytest.raises(FFmpegError) as exc:
        ffmpeg.run(["-i", str(tmp_path / "absent.mp4"), str(tmp_path / "o.mp4")])
    msg = str(exc.value)
    assert "absent.mp4" in msg
    assert len(msg) < 4500


def test_collect_sources(tmp_path: Path):
    (tmp_path / "b.mp4").write_bytes(b"")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.MKV").write_bytes(b"")
    (tmp_path / "sub" / "c.mov").write_bytes(b"")
    (tmp_path / "notes.txt").write_text("x")
    single = tmp_path / "z.mp4"
    single.write_bytes(b"")
    found = ffmpeg.collect_sources([tmp_path, single])
    assert found == sorted(found, key=str)
    assert {p.name for p in found} == {"b.mp4", "a.MKV", "c.mov", "z.mp4"}
    with pytest.raises(FFmpegError):
        ffmpeg.collect_sources([tmp_path / "missing"])
