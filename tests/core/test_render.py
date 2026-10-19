from pathlib import Path

import pytest

from minavio.config.schema import OutputFormat
from minavio.core import ffmpeg, reframe, render
from minavio.core.models import Clip, Overlay, Timeline
from minavio.errors import MinavioError


def fmt(aspect="16:9", w=320, h=180, reframe_mode="none", **kw) -> OutputFormat:
    return OutputFormat(
        name="t", aspect=aspect, width=w, height=h, fps=30, reframe=reframe_mode, **kw
    )


def timeline(src: Path, **kw) -> Timeline:
    clips = kw.pop("clips", [Clip(source=src, start=0, end=2), Clip(source=src, start=3, end=5)])
    return Timeline(profile_id="minivitch", fps=30, clips=clips, **kw)


def test_render_16_9_with_music_and_overlay(
    testsrc_video, music_file, alpha_overlay, profile, tmp_path
):
    tl = timeline(
        testsrc_video,
        music=music_file,
        overlays=[Overlay(template="popup", start=1.0, duration=1.0)],
    )
    out = render.render_timeline(tl, fmt(), tmp_path / "out.mp4", profile, {0: alpha_overlay})
    info = ffmpeg.probe(out)
    assert (info.width, info.height) == (320, 180)
    assert info.duration == pytest.approx(4.0, abs=0.3)
    assert info.has_audio


def test_overlay_command_uses_enable_between(testsrc_video, alpha_overlay, profile, tmp_path):
    tl = timeline(testsrc_video, overlays=[Overlay(template="x", start=1.0, duration=1.5)])
    args = render.build_command(tl, fmt(), tmp_path / "o.mp4", profile, {0: alpha_overlay})
    graph = args[args.index("-filter_complex") + 1]
    assert "overlay=enable='between(t,1.0000,2.5000)'" in graph
    with pytest.raises(MinavioError):
        render.build_command(tl, fmt(), tmp_path / "o.mp4", profile, {3: alpha_overlay})


@pytest.mark.parametrize("mode", ["blur_pad", "face_track", "action_track"])
def test_render_9_16_modes(mode, testsrc_video, profile, tmp_path):
    out = render.render_timeline(
        timeline(testsrc_video), fmt("9:16", 180, 320, mode), tmp_path / f"{mode}.mp4", profile
    )
    info = ffmpeg.probe(out)
    assert (info.width, info.height) == (180, 320)
    assert info.duration == pytest.approx(4.0, abs=0.3)


def test_max_duration_and_speed_and_silent_source(silent_video, profile, tmp_path):
    clips = [
        Clip(source=silent_video, start=0, end=4, speed=2.0),
        Clip(source=silent_video, start=0, end=4),
    ]
    out = render.render_timeline(
        timeline(silent_video, clips=clips), fmt(max_duration_s=3), tmp_path / "m.mp4", profile
    )
    info = ffmpeg.probe(out)
    assert info.duration == pytest.approx(3.0, abs=0.3)
    assert info.has_audio  # piste silencieuse synthétisée


def test_empty_timeline(profile, tmp_path):
    with pytest.raises(MinavioError, match="vide"):
        render.render_timeline(
            Timeline(profile_id="x", fps=30, clips=[]), fmt(), tmp_path / "x.mp4", profile
        )


def test_atempo_chain_and_fit_clips():
    assert render.atempo_chain(1.0) == ""
    assert render.atempo_chain(4.0).count("atempo") == 2
    assert render.atempo_chain(0.25).count("atempo") == 2
    clips = [Clip(source=Path("a"), start=0, end=4), Clip(source=Path("a"), start=0, end=4)]
    fitted = render.fit_clips(clips, 6)
    assert [c.duration for c in fitted] == [4, 2]


# --------------------------------------------------------------------------- reframe


def test_blur_pad_filter_shape():
    parts = reframe.blur_pad_filter("in", "out", 1080, 1920, "c0")
    graph = ";".join(parts)
    assert "boxblur" in graph and "overlay=(W-w)/2:(H-h)/2" in graph and graph.endswith("[out]")


def test_crop_x_expression_and_centered_fallback():
    path = [(0.0, 0.2), (1.0, 0.8)]
    expr = reframe.crop_x_expression(path, 1920, 608)
    assert expr.startswith("if(lt(t,1.000)")
    centered = reframe.track_filter("i", "o", 1920, 1080, 1080, 1920, None)[0]
    assert "crop=w=606:h=1080" in centered and "x='657'" in centered


def test_smooth_path_fills_gaps_and_decimates():
    raw = [(i * 0.25, None if i % 3 == 0 else 0.7) for i in range(200)]
    smooth = reframe.smooth_path(raw)
    assert len(smooth) <= reframe.MAX_PATH_POINTS + 2
    assert smooth[0][0] == 0.0
    assert all(0 <= x <= 1 for _, x in smooth)
    assert reframe.smooth_path([]) == [(0.0, 0.5)]


def test_compute_crop_path_action_track(testsrc_video):
    pytest.importorskip("cv2")
    path = reframe.compute_crop_path(testsrc_video, 0, 2, "action_track")
    assert path is None or all(0 <= x <= 1 for _, x in path)


def test_face_track_falls_back_with_warning(monkeypatch, testsrc_video, caplog):
    monkeypatch.setattr(reframe, "_face_detector", lambda: None)
    with caplog.at_level("WARNING"):
        assert reframe.compute_crop_path(testsrc_video, 0, 2, "face_track") is None
    assert "crop centré" in caplog.text
