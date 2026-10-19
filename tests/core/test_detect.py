import logging
from pathlib import Path

import pytest
import yaml

from minavio import config
from minavio.config.schema import ChannelProfile, DetectorRef
from minavio.core import detect
from minavio.core.models import Highlight
from tests.core.helpers import H, W, ff


def only(profile: ChannelProfile, *refs: DetectorRef, **kw) -> ChannelProfile:
    return profile.model_copy(update={"detection": list(refs), **kw})


def test_registry_contains_builtin_detectors():
    assert {"scene_change", "audio_peak", "template_match", "speech"} <= set(detect.DETECTORS)


def test_scene_change(flash_video, profile):
    p = only(profile, DetectorRef(name="scene_change", params={"threshold": 0.3}))
    hs = detect.run_detectors([flash_video], p)
    assert hs, "le flash blanc doit être détecté"
    # les deux transitions (3.0 s et 3.2 s) sont fusionnées en un seul highlight
    assert len(hs) == 1
    assert hs[0].start < 3.0 < hs[0].end
    assert 0 < hs[0].score <= 1


def test_audio_peak(testsrc_video, profile):
    p = only(profile, DetectorRef(name="audio_peak", params={"min_db_above_median": 12}))
    hs = detect.run_detectors([testsrc_video], p)
    assert len(hs) == 1
    assert hs[0].start < 3.0 and hs[0].end > 3.5
    assert hs[0].label == "loud_moment"


def test_audio_peak_silent_source_is_ignored(silent_video, profile):
    assert detect.run_detectors([silent_video], only(profile, DetectorRef(name="audio_peak"))) == []


def test_weight_scales_scores(flash_video, profile):
    base = detect.run_detectors([flash_video], only(profile, DetectorRef(name="scene_change")))
    half = detect.run_detectors(
        [flash_video], only(profile, DetectorRef(name="scene_change", weight=0.5))
    )
    assert half[0].score == pytest.approx(base[0].score * 0.5)


def test_merge_highlights():
    src = Path("a.mp4")

    def h(s, e, sc, det, label):
        return Highlight(source=src, start=s, end=e, score=sc, label=label, detector=det)

    merged = detect.merge_highlights(
        [h(0, 3, 0.4, "a", "x"), h(2, 5, 0.6, "b", "y"), h(10, 12, 0.3, "a", "x")]
    )
    assert len(merged) == 2
    assert (merged[0].start, merged[0].end, merged[0].label) == (0, 5, "y")
    assert merged[0].detector == "a+b"
    assert merged[0].score == pytest.approx(0.7)  # meilleur + bonus multi-détecteurs


def test_unknown_and_unavailable_detectors_are_skipped(profile, caplog, monkeypatch):
    def boom(sources, params, prof):
        raise detect.DetectorUnavailable("dépendance absente")

    monkeypatch.setitem(detect.DETECTORS, "boom", boom)
    p = only(profile, DetectorRef(name="boom"), DetectorRef(name="inconnu"))
    with caplog.at_level(logging.WARNING):
        assert detect.run_detectors([], p) == []
    assert "boom" in caplog.text and "inconnu" in caplog.text


# --------------------------------------------------------------------------- template_match


@pytest.fixture
def game_env(tmp_path: Path, monkeypatch):
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    gdir = tmp_path / "games" / "testgame"
    (gdir / "templates").mkdir(parents=True)
    rng = np.random.default_rng(3)
    tpl = (rng.integers(0, 2, (8, 8)) * 255).astype("uint8")
    tpl = cv2.resize(tpl, (48, 48), interpolation=cv2.INTER_NEAREST)
    cv2.imwrite(str(gdir / "templates" / "kill.png"), tpl)
    (gdir / "game.yaml").write_text(
        yaml.safe_dump(
            {
                "id": "testgame",
                "name": "Test",
                "combo_window_s": 3.0,
                "ui_templates": [
                    {
                        "name": "kill_marker",
                        "image": "templates/kill.png",
                        "label": "kill",
                        "roi": [0.2, 0.2, 0.6, 0.6],
                        "threshold": 0.8,
                        "cooldown_s": 0.8,
                    },
                    {
                        "name": "ghost",
                        "image": "templates/absent.png",
                        "label": "win",
                    },
                ],
            }
        )
    )
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    return gdir / "templates" / "kill.png"


def _video_with_template(tpl: Path, out: Path, enable: str) -> Path:
    ff(
        *("-f", "lavfi", "-i", f"color=c=black:s={W}x{H}:r=30:d=6"),
        *("-i", str(tpl)),
        *("-filter_complex", f"[0][1]overlay=x=140:y=70:enable='{enable}'"),
        *("-c:v", "libx264", "-crf", "12", "-preset", "ultrafast", "-pix_fmt", "yuv420p"),
        str(out),
    )
    return out


def test_template_match_multi_kill(game_env, profile, tmp_path, caplog):
    video = _video_with_template(
        game_env, tmp_path / "kills.mp4", "between(t,1.5,2.0)+between(t,3.0,3.5)"
    )
    p = only(profile, DetectorRef(name="template_match"), games=["testgame"])
    with caplog.at_level(logging.WARNING):
        hs = detect.run_detectors([video], p)
    assert "absent.png" in caplog.text  # template manquant -> warning + skip
    assert len(hs) == 1
    assert hs[0].label == "multi_kill"
    assert hs[0].start < 1.6 and hs[0].end > 3.0
    assert hs[0].score > 0.8


def test_template_match_single_kill_and_cooldown(game_env, profile, tmp_path):
    video = _video_with_template(game_env, tmp_path / "one.mp4", "between(t,2,2.6)")
    p = only(profile, DetectorRef(name="template_match"), games=["testgame"])
    hs = detect.run_detectors([video], p)
    assert [h.label for h in hs] == ["kill"]


def test_template_match_no_games_is_empty(flash_video, profile):
    pytest.importorskip("cv2")
    assert (
        detect.run_detectors(
            [flash_video], only(profile, DetectorRef(name="template_match"), games=[])
        )
        == []
    )


def test_group_combos():
    from minavio.core.detect.template_match import _Hit, group_combos

    hits = [
        _Hit(1, 0.9, "kill", "k"),
        _Hit(3, 0.9, "kill", "k"),
        _Hit(4, 0.9, "win", "w"),
        _Hit(20, 0.9, "kill", "k"),
    ]
    groups = group_combos(hits, 6.0)
    assert [len(g) for g in groups] == [2, 1, 1]


# --------------------------------------------------------------------------- speech


def test_speech_runs_on_non_speech(testsrc_video, profile):
    pytest.importorskip("silero_vad")
    pytest.importorskip("torch")
    p = only(profile, DetectorRef(name="speech"))
    hs = detect.run_detectors([testsrc_video], p)  # un sinus n'est pas de la parole
    assert all(h.detector == "speech" for h in hs)


def test_speech_mocked(monkeypatch, profile, tmp_path):
    import sys
    import types

    monkeypatch.setitem(sys.modules, "silero_vad", types.ModuleType("silero_vad"))
    monkeypatch.setattr("minavio.core.audio.vad.detect_speech", lambda path, thr, ms: [(1.0, 3.0)])
    hs = detect.run_detectors([Path("x.mp4")], only(profile, DetectorRef(name="speech")))
    assert len(hs) == 1 and hs[0].label == "speech" and (hs[0].start, hs[0].end) == (1.0, 3.0)
