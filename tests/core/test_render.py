import stat
import sys
from pathlib import Path

import pytest

from minavio.config.schema import OutputFormat
from minavio.core import ffmpeg, reframe, render
from minavio.core.models import Clip, Overlay, Timeline
from minavio.errors import MinavioError
from tests.core.helpers import ff
from tests.core.test_interpolate import FAKE_RIFE


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


# --------------------------------------------------------------------------- seek en entrée


def test_inputs_are_seeked_before_each_i(testsrc_video, profile, tmp_path):
    clips = [
        Clip(source=testsrc_video, start=1.0, end=2.5),
        Clip(source=testsrc_video, start=3.0, end=5.0, speed=2.0),
    ]
    args = render.build_command(
        timeline(testsrc_video, clips=clips), fmt(), tmp_path / "o.mp4", profile
    )
    idx = [k for k, a in enumerate(args) if a == "-i"]
    assert len(idx) == 2
    for k, c in zip(idx, clips, strict=True):
        assert args[k - 4 : k] == ["-ss", f"{c.start:.4f}", "-t", f"{c.duration:.4f}"]
    graph = args[args.index("-filter_complex") + 1]
    assert "trim=" not in graph and "atrim=start" not in graph
    assert "[0:v]setpts=PTS-STARTPTS[t0]" in graph and "asetpts=PTS-STARTPTS" in graph
    assert "atempo=2.00000" in graph and "setpts=PTS/2.00000" in graph


def test_rendered_duration_matches_timeline(testsrc_video, profile, tmp_path):
    clips = [
        Clip(source=testsrc_video, start=0.5, end=1.5),
        Clip(source=testsrc_video, start=2.0, end=3.2, speed=2.0),
        Clip(source=testsrc_video, start=4.0, end=5.3),
    ]
    tl = timeline(testsrc_video, clips=clips)
    out = render.render_timeline(tl, fmt(), tmp_path / "d.mp4", profile)
    assert ffmpeg.probe(out).duration == pytest.approx(tl.duration, abs=len(clips) / 30)


def test_flash_and_beep_stay_in_sync(media_dir, profile, tmp_path):
    from tests.core.helpers import ff

    src = media_dir / "sync.mp4"
    ff(
        *("-f", "lavfi", "-i", "color=c=black:s=160x90:r=30:d=8"),
        *("-f", "lavfi", "-i", "aevalsrc='0.8*sin(2*PI*1000*t)*between(t,4,4.2)':s=48000:d=8"),
        *("-vf", "drawbox=x=0:y=0:w=iw:h=ih:color=white:t=fill:enable='between(t,4,4.2)'"),
        *("-c:v", "libx264", "-preset", "ultrafast", "-g", "60", "-pix_fmt", "yuv420p"),
        *("-c:a", "aac", "-b:a", "192k", str(src)),
    )
    clips = [Clip(source=src, start=2.5, end=3.0), Clip(source=src, start=3.5, end=6.0)]
    # le flash (4.0 s source) tombe à 0.5 + 0.5 = 1.0 s dans la sortie
    out = render.render_timeline(
        timeline(src, clips=clips), fmt(w=160, h=90), tmp_path / "s.mp4", profile
    )
    import subprocess

    import numpy as np

    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(out), "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        capture_output=True,
        check=True,
    ).stdout
    frames = np.frombuffer(raw, np.uint8).reshape(-1, 90, 160)
    t_video = int(np.argmax(frames.mean(axis=(1, 2)) > 128)) / 30
    pcm = ffmpeg.decode_pcm(out, 16000)
    t_audio = int(np.argmax(np.abs(pcm) > 0.2)) / 16000
    assert t_video == pytest.approx(1.0, abs=2 / 30)
    assert t_audio == pytest.approx(1.0, abs=0.06)
    assert abs(t_video - t_audio) < 0.06


# --------------------------------------------------------------------------- ralenti + pistes audio


def _unique_frames(path: Path) -> int:
    """Nombre d'images distinctes (mpdecimate écarte les doublons)."""
    err = ffmpeg.run_capture(
        ["-i", str(path), "-vf", "mpdecimate,showinfo", "-f", "null", "-"], stream="stderr"
    ).decode()
    return sum(1 for ln in err.splitlines() if "Parsed_showinfo" in ln and " n:" in ln)


def _slow_profile(profile, interpolation: str):
    from minavio.config.schema import SlowmoSettings

    p = profile.model_copy(deep=True)
    p.editing.slowmo = SlowmoSettings(enabled=True, interpolation=interpolation)
    return p


def test_plain_command_has_no_slowmo_artifacts(testsrc_video, profile, tmp_path):
    args = render.build_command(timeline(testsrc_video), fmt(), tmp_path / "o.mp4", profile)
    graph = args[args.index("-filter_complex") + 1]
    assert "minterpolate" not in graph and "volume=-4" not in graph
    assert args.count("-i") == 2


def test_slowmo_minterpolate_filter_and_audio_gain(testsrc_video, profile, tmp_path):
    p = _slow_profile(profile, "minterpolate")
    clip = Clip(source=testsrc_video, start=1, end=2, speed=0.5, interpolate=True)
    args = render.build_command(timeline(testsrc_video, clips=[clip]), fmt(), tmp_path / "o.mp4", p)
    graph = args[args.index("-filter_complex") + 1]
    assert "minterpolate=fps=60.000:mi_mode=mci:mc_mode=aobmc:vsbmc=1,setpts=PTS/0.50000" in graph
    assert "atempo=0.50000,volume=-4dB" in graph


def test_slowmo_render_real_interpolates_frames(testsrc_video, profile, tmp_path):
    clip = Clip(source=testsrc_video, start=1, end=2, speed=0.5, interpolate=True)
    outs = {}
    for mode, interp in (("mi", True), ("plain", False)):
        c = clip.model_copy(update={"interpolate": interp})
        p = _slow_profile(profile, "minterpolate")
        out = render.render_timeline(
            timeline(testsrc_video, clips=[c]), fmt(), tmp_path / f"{mode}.mp4", p
        )
        outs[mode] = out
        assert ffmpeg.probe(out).duration == pytest.approx(2.0, abs=0.15)  # 2x la source
    assert _unique_frames(outs["mi"]) > _unique_frames(outs["plain"]) * 1.3


def test_slowmo_rife_path_uses_intermediate_and_cleans_up(
    testsrc_video, profile, tmp_path, monkeypatch
):
    exe = tmp_path / "fake-rife"
    exe.write_text(FAKE_RIFE.format(python=sys.executable))
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("MINAVIO_RIFE_BIN", str(exe))
    p = _slow_profile(profile, "auto")
    clip = Clip(source=testsrc_video, start=1, end=2, speed=0.5, interpolate=True)
    tl = timeline(testsrc_video, clips=[clip])
    outdir = tmp_path / "o"
    out = render.render_timeline(tl, fmt(), outdir / "r.mp4", p)
    assert ffmpeg.probe(out).duration == pytest.approx(2.0, abs=0.15)
    assert [f.name for f in outdir.iterdir()] == ["r.mp4"]  # intermédiaires nettoyés
    temps: list[Path] = []
    args = render.build_command(tl, fmt(), outdir / "r.mp4", p, temps=temps)
    assert len(temps) == 1 and args.count("-i") == 2  # source (audio) + vidéo interpolée
    graph = args[args.index("-filter_complex") + 1]
    assert "minterpolate" not in graph and "setpts=PTS/0.5" not in graph
    for t in temps:
        t.unlink(missing_ok=True)


def _beep_track(path: Path, beep_at: float, total: float = 6.0) -> Path:
    ff(
        *("-f", "lavfi", "-i", f"anullsrc=r=48000:cl=stereo:d={total}"),
        *(
            "-f",
            "lavfi",
            "-i",
            f"sine=f=1000:r=48000:d=0.2,adelay={int(beep_at * 1000)}|{int(beep_at * 1000)}",
        ),
        *("-filter_complex", "[0:a][1:a]amix=inputs=2:duration=first:normalize=0[a]"),
        *("-map", "[a]", "-c:a", "pcm_s16le", str(path)),
    )
    return path


def _onset(path: Path) -> float:
    import numpy as np

    raw = ffmpeg.run_capture(
        ["-i", str(path), "-vn", "-ac", "1", "-ar", "8000", "-f", "f32le", "-"]
    )
    x = np.abs(np.frombuffer(raw, dtype=np.float32))
    return float(np.argmax(x > 0.3 * x.max()) / 8000)


def test_audio_tracks_replace_source_audio_at_same_time(testsrc_video, profile, tmp_path):
    pytest.importorskip("numpy")
    track = _beep_track(tmp_path / "clean.wav", beep_at=2.0)
    clip = Clip(source=testsrc_video, start=1.0, end=4.0)
    tl = timeline(testsrc_video, clips=[clip], audio_tracks={testsrc_video: track})
    args = render.build_command(tl, fmt(), tmp_path / "o.mp4", profile)
    idx = [k for k, a in enumerate(args) if a == "-i"]
    assert len(idx) == 2 and args[idx[1] + 1] == str(track)
    assert args[idx[1] - 4 : idx[1]] == ["-ss", "1.0000", "-t", "3.0000"]  # même seek que la source
    out = render.render_timeline(tl, fmt(), tmp_path / "o.mp4", profile)
    assert _onset(out) == pytest.approx(1.0, abs=0.1)  # 2.0 s source - 1.0 s de seek
    # sans piste de remplacement : la rafale 3.0-3.5 s de la source tombe à 2.0 s
    base = render.render_timeline(
        timeline(testsrc_video, clips=[clip]), fmt(), tmp_path / "b.mp4", profile
    )
    assert _onset(base) == pytest.approx(2.0, abs=0.15)


def _rife_env(tmp_path, monkeypatch):
    exe = tmp_path / "fake-rife"
    exe.write_text(FAKE_RIFE.format(python=sys.executable))
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("MINAVIO_RIFE_BIN", str(exe))


def test_rife_reframe_path_follows_slowed_input_time(testsrc_video, profile, tmp_path, monkeypatch):
    """En rife l'entrée est déjà ralentie : les temps du chemin (source) sont étirés de 1/speed."""
    _rife_env(tmp_path, monkeypatch)
    path = [(0.0, 0.3), (1.0, 0.7)]
    monkeypatch.setattr(
        reframe,
        "plan_clip",
        lambda *a, **k: reframe.ClipReframe(mode="action_track", path=path),
    )
    clip = Clip(source=testsrc_video, start=1, end=2, speed=0.5, interpolate=True)
    tl = timeline(testsrc_video, clips=[clip])
    f = fmt("9:16", 180, 320, "action_track")
    temps: list[Path] = []
    p = _slow_profile(profile, "auto")
    rife = render.build_command(tl, f, tmp_path / "o.mp4", p, temps=temps)
    graph = rife[rife.index("-filter_complex") + 1]
    assert "lt(t,2.000)" in graph and "lt(t,1.000)" not in graph  # fin du chemin à 2 s (1 / 0.5)
    for t in temps:
        t.unlink(missing_ok=True)
    # minterpolate : ralenti après le reframe, chemin inchangé
    mi = render.build_command(tl, f, tmp_path / "o.mp4", _slow_profile(profile, "minterpolate"))
    assert "lt(t,1.000)" in mi[mi.index("-filter_complex") + 1]
