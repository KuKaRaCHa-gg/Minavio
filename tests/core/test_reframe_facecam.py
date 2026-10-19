"""Détection facecam (vrai visage incrusté dans une vidéo lavfi) et rendu split_facecam / zoom."""

from pathlib import Path

import pytest

from minavio.config.schema import OutputFormat
from minavio.core import ffmpeg, reframe, render
from minavio.core.models import Clip, Timeline
from tests.core.helpers import ff

cv2 = pytest.importorskip("cv2")
FACE = Path(__file__).parent.parent / "fixtures" / "face.jpg"
SCRATCH = Path("/tmp/claude-0/-home-user-Minavio/4faba626-2c48-5eab-8447-73c2f5abd788/scratchpad")
BG = "color=c=0x24304a:s=1280x720:r=30:d=4"
BOX = "color=c=orange:s=120x120:r=30:d=4"
ENC = ("-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-t", "4")
POS = {"bottom_left": "x=24:y=H-h-24", "bottom_right": "x=W-w-24:y=H-h-24"}


def make_video(out: Path, corner: str | None) -> Path:
    """1280x720, 4 s : fond uni + carré orange mobile (gameplay) + vrai visage dans un coin."""
    graph = "[0:v][1:v]overlay=x='200+t*150':y=250[g]"
    args = ["-f", "lavfi", "-i", BG, "-f", "lavfi", "-i", BOX]
    if corner is None:
        graph += ";[g]null[v]"
    else:
        args += ["-loop", "1", "-i", str(FACE)]
        graph += f";[2:v]scale=280:280[f];[g][f]overlay={POS[corner]}:shortest=1[v]"
    args += ["-f", "lavfi", "-i", "sine=f=440:d=4"]
    n_inputs = 3 if corner is None else 4
    audio = n_inputs - 1
    ff(*args, "-filter_complex", graph, "-map", "[v]", "-map", f"{audio}:a", *ENC, str(out))
    return out


@pytest.fixture(scope="module")
def vids(tmp_path_factory):
    d = tmp_path_factory.mktemp("facecam")
    return {
        c: make_video(d / f"{c}.mp4", None if c == "none" else c)
        for c in ("bottom_left", "bottom_right", "none")
    }


@pytest.mark.parametrize("corner", ["bottom_left", "bottom_right"])
def test_detect_facecam_auto(vids, corner, caplog):
    with caplog.at_level("INFO", logger="minavio.reframe"):
        fc = reframe.detect_facecam(vids[corner], 0, 4)
    assert fc is not None and fc.corner == corner
    assert fc.confidence >= 0.6
    x, y, w, h = fc.box
    assert 0.1 < w < 0.45 and 0.1 < h < 0.6
    assert x >= 0 and y >= 0 and x + w <= 1.001 and y + h <= 1.001
    assert x <= fc.face[0] <= x + w and y <= fc.face[1] <= y + h
    assert f"coin={corner}" in caplog.text  # le résultat est loggé


def test_detect_facecam_forced_corner_and_absence(vids):
    assert reframe.detect_facecam(vids["bottom_left"], 0, 4, "bottom_left") is not None
    assert reframe.detect_facecam(vids["bottom_left"], 0, 4, "top_right") is None
    assert reframe.detect_facecam(vids["none"], 0, 4) is None


def fmt(w=360, h=640, mode="split_facecam") -> OutputFormat:
    return OutputFormat(name="s", aspect="9:16", width=w, height=h, fps=30, reframe=mode)


def tl(src: Path, **kw) -> Timeline:
    return Timeline(profile_id="x", fps=30, clips=[Clip(source=src, start=0, end=2, **kw)])


def test_render_split_facecam_1080x1920(vids, profile, tmp_path):
    out = render.render_timeline(
        tl(vids["bottom_right"]), fmt(1080, 1920), tmp_path / "s.mp4", profile
    )
    info = ffmpeg.probe(out)
    assert (info.width, info.height) == (1080, 1920)
    # la partie basse contient le visage, la partie haute non
    png = tmp_path / "f.png"
    ff("-i", str(out), "-vf", "select=eq(n\\,30)", "-vframes", "1", str(png))
    img = cv2.imread(str(png))
    faces = reframe._yunet_faces()
    assert faces(img[: int(1920 * 0.65)]) == []
    bottom = faces(img[int(1920 * 0.65) :])
    assert bottom and bottom[0][4] > 0.7
    if SCRATCH.is_dir():
        cv2.imwrite(str(SCRATCH / "split_facecam.png"), img)


def test_split_facecam_without_facecam_falls_back(vids, profile, tmp_path, caplog):
    with caplog.at_level("WARNING", logger="minavio.reframe"):
        out = render.render_timeline(tl(vids["none"]), fmt(), tmp_path / "fb.mp4", profile)
    assert "repli sur" in caplog.text
    info = ffmpeg.probe(out)
    assert (info.width, info.height) == (360, 640)


@pytest.mark.parametrize("mode", ["none", "blur_pad", "action_track", "split_facecam"])
def test_punch_in_keeps_output_resolution(vids, profile, tmp_path, mode):
    if mode == "none":
        f = OutputFormat(name="w", aspect="16:9", width=640, height=360, fps=30, reframe="none")
    else:
        f = fmt(mode=mode)
    clip_tl = tl(vids["bottom_left"], zoom=1.5)
    if mode in ("none", "blur_pad"):
        args = render.build_command(clip_tl, f, tmp_path / "g.mp4", profile)
        assert "crop=852:480" in args[args.index("-filter_complex") + 1]  # 1280/1.5 x 720/1.5
    out = render.render_timeline(clip_tl, f, tmp_path / f"{mode}.mp4", profile)
    info = ffmpeg.probe(out)
    assert (info.width, info.height) == (f.width, f.height)


def test_window_geometry_zoom_and_exclusion():
    w, h, lo, hi, _y = reframe.window_geometry(1920, 1080, 1080 / 1248, 1.0, ("left", 500))
    assert h == 1080 and lo == 500 and hi == 1920 - w
    _w2, h2, *_ = reframe.window_geometry(1920, 1080, 1080 / 1248, 2.0)
    assert h2 == 540


def test_cut_detector_flags_scene_change():
    import numpy as np

    cd = reframe.CutDetector()
    black = np.zeros((180, 320, 3), np.uint8)
    white = np.full((180, 320, 3), 255, np.uint8)
    assert [cd.update(f) for f in (black, black, white, white)] == [False, False, True, False]
