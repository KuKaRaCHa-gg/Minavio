import re
from pathlib import Path

import pytest

from minavio.config.schema import OutputFormat, SfxSettings
from minavio.core import render, sfx
from minavio.core.models import Clip, SfxCue, Timeline
from tests.core.helpers import ff

SRC = Path("/x/src.mp4")


def tl(clips, cold=0, **kw) -> Timeline:
    return Timeline(profile_id="p", fps=30, clips=clips, cold_open_clips=cold, **kw)


def clip(start, end, label=None, src=SRC) -> Clip:
    return Clip(source=src, start=start, end=end, highlight_label=label)


def sets(**kw) -> SfxSettings:
    return SfxSettings(enabled=True, **kw)


# ---- plan_sfx (pur) ----


def test_on_cut_between_moments_only_not_autocut():
    # 2 sous-clips d'autocut (écart 0.5 s) puis un autre moment (autre label)
    t = tl([clip(0, 2, "a"), clip(2.5, 4.5, "a"), clip(50, 52, "b")])
    cues = sfx.plan_sfx(t, sets())
    assert [(c.sound, round(c.at, 2)) for c in cues] == [("whoosh", 4 - 0.08)]
    assert cues[0].gain_db == -6


def test_kill_and_multi_kill():
    t = tl([clip(0, 4, "kill"), clip(100, 106, "multi_kill")])
    cues = sfx.plan_sfx(t, sets(on_cut=None))
    assert [(c.sound, round(c.at, 2)) for c in cues] == [
        ("hit", 2.0),
        ("boom", 7.0),
        ("hit", 7.35),
    ]


def test_cold_open_cue_at_end_and_no_double_cut():
    t = tl([clip(40, 43, "kill"), clip(0, 5, "x"), clip(90, 95, "y")], cold=1)
    cues = sfx.plan_sfx(t, sets(on_kill=None))
    assert [(c.sound, round(c.at, 2)) for c in cues] == [("rewind", 3.0), ("whoosh", 7.92)]


def test_none_disables_events():
    t = tl([clip(0, 3, "kill"), clip(50, 53, "multi_kill")])
    off = sets(on_cut=None, on_kill=None, on_multi_kill=None, on_cold_open=None)
    assert sfx.plan_sfx(t, off) == []


def test_no_cue_past_duration():
    t = tl([clip(0, 1, "multi_kill")])
    assert [c.sound for c in sfx.plan_sfx(t, sets(on_cut=None))] == ["boom", "hit"]
    t2 = tl([clip(0, 0.7, "multi_kill")])  # 2e hit à 0.7 s == durée : écarté
    assert [c.sound for c in sfx.plan_sfx(t2, sets(on_cut=None))] == ["boom"]


# ---- resolve_sound ----


def test_bundled_sounds_exist():
    for name in ("whoosh", "hit", "boom", "rewind", "pop", "coin"):
        assert sfx.resolve_sound(name, SfxSettings()) is not None


def test_user_library_has_priority(tmp_path, caplog):
    (tmp_path / "hit.wav").write_bytes(b"x")
    s = SfxSettings(library_dir=str(tmp_path))
    assert sfx.resolve_sound("hit", s) == tmp_path / "hit.wav"
    assert sfx.resolve_sound("boom", s) == sfx.BUNDLED_DIR / "boom.ogg"
    with caplog.at_level("WARNING"):
        assert sfx.resolve_sound("nope", s) is None
    assert "nope" in caplog.text


# ---- rendu ----


def fmt() -> OutputFormat:
    return OutputFormat(name="t", aspect="16:9", width=320, height=180, fps=30, reframe="none")


@pytest.fixture
def quiet_video(media_dir) -> Path:
    out = media_dir / "quiet.mp4"
    ff(
        *("-f", "lavfi", "-i", "testsrc2=s=320x180:r=30:d=6"),
        *("-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"),
        *("-t", "6", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"),
        *("-c:a", "aac", str(out)),
    )
    return out


def _max_db(path: Path, start: float, dur: float) -> float:
    import subprocess

    r = subprocess.run(
        [
            *("ffmpeg", "-hide_banner", "-ss", str(start), "-t", str(dur), "-i", str(path)),
            *("-af", "volumedetect", "-f", "null", "-"),
        ],
        capture_output=True,
        text=True,
    )
    m = re.search(r"max_volume: (-?[\d.]+|-inf) dB", r.stderr)
    assert m, r.stderr
    return float("-inf") if m.group(1) == "-inf" else float(m.group(1))


def test_render_mixes_sfx_at_cue_times(quiet_video, profile, tmp_path):
    profile = profile.model_copy(deep=True)
    profile.audio.sfx.enabled = True
    t = Timeline(
        profile_id="p",
        fps=30,
        clips=[Clip(source=quiet_video, start=0, end=5)],
        sfx=[
            SfxCue(at=1.0, sound="boom", gain_db=0),
            SfxCue(at=3.0, sound="hit", gain_db=0),
            SfxCue(at=3.5, sound="hit", gain_db=0),
        ],
    )
    out = render.render_timeline(t, fmt(), tmp_path / "o.mp4", profile)
    quiet = _max_db(out, 2.0, 0.6)
    assert _max_db(out, 1.0, 0.5) > quiet + 20
    assert _max_db(out, 3.0, 0.4) > quiet + 20
    assert _max_db(out, 3.5, 0.4) > quiet + 20
    cmd = render.build_command(t, fmt(), tmp_path / "o.mp4", profile)
    assert cmd.count("-i") == 3  # 1 source + boom + hit (un -i par fichier distinct)
    assert "adelay=1000|1000" in cmd[cmd.index("-filter_complex") + 1]


def test_sfx_ignored_when_disabled(quiet_video, profile, tmp_path):
    t = Timeline(
        profile_id="p",
        fps=30,
        clips=[Clip(source=quiet_video, start=0, end=3)],
        sfx=[SfxCue(at=1.0, sound="boom")],
    )
    assert profile.audio.sfx.enabled is False
    base = t.model_copy(update={"sfx": []})
    a = render.build_command(t, fmt(), tmp_path / "o.mp4", profile)
    b = render.build_command(base, fmt(), tmp_path / "o.mp4", profile)
    assert a == b
    assert "adelay" not in " ".join(a)


def test_sfx_beyond_max_duration_skipped(quiet_video, profile, tmp_path):
    profile = profile.model_copy(deep=True)
    profile.audio.sfx.enabled = True
    t = Timeline(
        profile_id="p",
        fps=30,
        clips=[Clip(source=quiet_video, start=0, end=5)],
        sfx=[SfxCue(at=4.0, sound="boom")],
    )
    f = fmt().model_copy(update={"max_duration_s": 2.0})
    cmd = render.build_command(t, f, tmp_path / "o.mp4", profile)
    assert "adelay" not in " ".join(cmd)
