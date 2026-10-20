"""Détecteur killfeed_ocr : logique pure (OCR mocké) + un vrai test RapidOCR sur vidéo lavfi."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

from minavio import config
from minavio.config.schema import ChannelProfile, DetectorRef, KillfeedSettings
from minavio.core import detect, ffmpeg
from minavio.core.detect import killfeed_ocr as kf
from minavio.core.detect.killfeed_ocr import Event, OcrLine
from tests.core.helpers import ff

KW = KillfeedSettings().keywords


# --------------------------------------------------------------------------- texte


@pytest.mark.parametrize(
    ("raw", "label"),
    [
        ("Player ELIMINATED Enemy", "kill"),
        ("Player ELIMlNATED Enemy", "kill"),  # I -> l
        ("Player elimin4ted Enemy", "kill"),
        ("Player ÉLIMINÉ Enemy", "kill"),
        ("Player knocked dwn Enemy", "knock"),
        ("Player knockeddown Enemy", "knock"),  # mots collés
        ("Player a mis à terre Enemy", "knock"),
        ("CHAMPlON", "win"),
        ("Player waved at Enemy", None),
    ],
)
def test_classify_tolerates_ocr_typos(raw, label):
    kw = {**KW, "knock": [*KW["knock"], "a mis à terre"]}
    assert kf.classify(kf.normalize(raw), kw) == label


def test_short_keywords_are_exact():
    assert kf.classify("x a tue y", {"kill": ["a tué"]}) == "kill"
    assert kf.classify("x a tub y", {"kill": ["a tué"]}) is None


def test_mentions_player_is_fuzzy():
    assert kf.mentions_player(kf.normalize("Minivitch eliminated Zed"), ["Minivitch"])
    assert kf.mentions_player(kf.normalize("Minivltch eliminated Zed"), ["Minivitch"])
    assert not kf.mentions_player(kf.normalize("Rival eliminated Zed"), ["Minivitch"])


# --------------------------------------------------------------------------- logique pure


def _ev(t, label="kill", text="player eliminated enemy", mine=None, conf=0.9):
    return Event(t, t, label, text, mine, conf)


def test_dedupe_same_line_displayed_for_seconds():
    obs = [_ev(2 + 0.5 * i) for i in range(10)]  # visible de 2 s à 6.5 s : un seul kill
    events = kf.dedupe_events(obs, 4.0)
    assert [e.t for e in events] == [2.0]


def test_dedupe_ocr_jitter_and_distinct_lines():
    obs = [
        _ev(1.0, text="player eliminated alpha"),
        _ev(1.5, text="player ellminated alpha"),  # même ligne, OCR bruité
        _ev(2.0, text="player eliminated bravo"),  # autre ennemi : autre kill
        _ev(30.0, text="player eliminated alpha"),  # bien plus tard : nouveau kill
    ]
    events = kf.dedupe_events(obs, 4.0)
    assert [e.t for e in events] == [1.0, 2.0, 30.0]


def test_highlights_combo_single_knock_win():
    events = [
        _ev(2.0, text="a"),
        _ev(5.0, text="b"),
        _ev(20.0, text="c"),
        _ev(30.0, "knock", "d"),
        _ev(40.0, "win", "champion"),
    ]
    hs = kf.to_highlights(Path("v.mp4"), events, 6.0, {})
    assert [h.label for h in hs] == ["multi_kill", "kill", "knock", "win"]
    multi, single, knock, win = hs
    assert multi.signals == {"kills": 2.0, "peak_t": 5.0}
    assert (multi.start, multi.end) == (0.5, 6.5)
    assert single.signals == {"kills": 1.0, "peak_t": 20.0}
    assert multi.score > single.score > knock.score
    assert win.score >= 0.9


def test_foreign_kills_are_downweighted_and_not_comboed():
    events = [_ev(2.0, mine=True), _ev(4.0, mine=False, text="x"), _ev(20.0, mine=True, text="y")]
    hs = kf.to_highlights(Path("v.mp4"), events, 6.0, {})
    assert [h.label for h in hs] == ["kill", "kill", "kill"]
    assert hs[1].score == pytest.approx(hs[0].score * 0.5)


def test_envelope_box_and_enlarge():
    frames = [
        [(0.70, 0.05, 0.95, 0.09), (0.72, 0.10, 0.95, 0.14)],
        [(0.70, 0.05, 0.95, 0.09)],
        [(0.66, 0.05, 0.95, 0.09)],
    ]
    box = kf.envelope_box(frames, q=0.0)  # q=0 : enveloppe exacte
    assert box == pytest.approx((0.66, 0.05, 0.95, 0.14))
    x, y, w, h = kf.enlarge_roi(box, 0.2, min_w=0.0, min_h=0.0)
    assert w == pytest.approx(0.29 * 1.2, abs=1e-3)
    assert h == pytest.approx(0.09 * 1.2, abs=1e-3)
    assert x < 0.66 and x + w <= 1.0 and y >= 0.0


def test_enlarge_roi_enforces_minimum_size_inside_the_frame():
    x, y, w, h = kf.enlarge_roi((0.9, 0.0, 1.0, 0.03), 0.2)
    assert (w, h) == (pytest.approx(0.25), pytest.approx(0.2))
    assert x + w == pytest.approx(1.0) and y == 0.0


# --------------------------------------------------------------------------- pipeline mocké


class Fake:
    """Remplace `_frames` + moteur OCR : un script [(début, fin, texte, dy)] sur vidéo fictive."""

    def __init__(self, script, duration=40.0):
        self.script = script
        self.duration = duration
        self.crops: list = []

    def frames(self, src, info, fps, crop, max_w):
        self.crops.append(crop)
        for k in range(int(self.duration * fps)):
            yield k / fps, ("roi" if crop else "full", k / fps)

    def ocr(self, frame):
        mode, t = frame
        lines = []
        for start, end, text, dy in self.script:
            if start <= t < end:
                if mode == "full":  # killfeed en haut à droite de l'écran
                    box = (0.70, 0.04 + dy, 0.96, 0.08 + dy)
                else:  # dans la ROI, la ligne occupe toute la largeur
                    box = (0.0, 0.1 + dy, 1.0, 0.3 + dy)
                lines.append(OcrLine(text, box, 0.9))
        return lines


@pytest.fixture
def video(tmp_path) -> Path:
    p = tmp_path / "fake.mp4"
    p.write_bytes(b"x")
    return p


@pytest.fixture
def mocked(monkeypatch, tmp_path):
    game_dir = tmp_path / "cfg" / "games" / "tg"
    game_dir.mkdir(parents=True)

    def setup(script, killfeed=None, duration=40.0):
        (game_dir / "game.yaml").write_text(
            "id: tg\nname: TG\ncombo_window_s: 6.0\n"
            + (f"killfeed:\n{killfeed}\n" if killfeed else ""),
            encoding="utf-8",
        )
        fake = Fake(script, duration)
        monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "cfg")
        monkeypatch.setattr(kf, "_make_ocr", lambda: fake.ocr)
        monkeypatch.setattr(kf, "_frames", fake.frames)
        monkeypatch.setattr(
            ffmpeg, "probe", lambda p: ffmpeg.MediaInfo(duration, 1920, 1080, 60.0, True)
        )
        kf._ROI_CACHE.clear()
        return fake

    return setup


def _profile(profile: ChannelProfile) -> ChannelProfile:
    return profile.model_copy(
        update={"games": ["tg"], "detection": [DetectorRef(name="killfeed_ocr")]}
    )


SCRIPT = [
    (2.0, 6.5, "Me ELIMINATED Alpha", 0.0),
    (5.0, 8.0, "Me ELIMlNATED Bravo", 0.04),
    (20.0, 23.0, "Me KNOCKED DOWN Charlie", 0.0),
    (30.0, 34.0, "CHAMPION DE L'ARENE", 0.2),
    (12.0, 40.0, "Score 1234", 0.1),  # bruit sans mot-clé
]


def test_pipeline_auto_roi_dedupe_combo(mocked, video, profile, caplog):
    fake = mocked(SCRIPT)
    caplog.set_level(logging.INFO, logger="minavio.detect.killfeed")
    hs = detect.run_detectors([video], _profile(profile))
    assert [h.label for h in hs] == ["multi_kill", "knock", "win"]
    multi = hs[0]
    assert multi.signals["kills"] == 2.0
    assert multi.signals["peak_t"] == pytest.approx(5.0, abs=0.5)
    assert hs[1].signals["peak_t"] == pytest.approx(20.0, abs=0.5)
    assert hs[2].score >= 0.9
    # la ROI auto (haut-droite) est utilisée pour la 2e passe et loguée pour le YAML
    roi = fake.crops[-1]
    assert roi is not None and roi[0] > 0.6 and roi[1] < 0.3 and roi[0] + roi[2] <= 1.0
    assert fake.crops[0] is None
    assert any("roi: [" in r.message for r in caplog.records)


def test_configured_roi_skips_localisation(mocked, video, profile):
    fake = mocked(SCRIPT, killfeed="  roi: [0.7, 0.0, 0.3, 0.2]")
    hs = detect.run_detectors([video], _profile(profile))
    assert hs
    assert fake.crops == [(0.7, 0.0, 0.3, 0.2)]


def test_player_name_reduces_foreign_kills(mocked, video, profile):
    script = [
        (2.0, 4.0, "Minivitch ELIMINATED Alpha", 0.0),
        (10.0, 12.0, "Rival ELIMINATED Bravo", 0.0),
    ]
    mocked(script, killfeed="  player_names: [Minivitch]")
    hs = detect.run_detectors([video], _profile(profile))
    assert [h.label for h in hs] == ["kill", "kill"]
    assert hs[0].score > hs[1].score * 1.5
    assert "pseudo absent" in hs[1].reason


def test_no_keyword_anywhere_is_skipped(mocked, video, profile, caplog):
    mocked([(0.0, 40.0, "Score 1234", 0.0)])
    assert detect.run_detectors([video], _profile(profile)) == []
    assert any("introuvable" in r.message for r in caplog.records)


def test_missing_rapidocr_is_unavailable_not_a_crash(monkeypatch, tmp_path, video, profile, caplog):
    monkeypatch.setitem(sys.modules, "rapidocr", None)
    cfg = tmp_path / "cfg" / "games" / "tg"
    cfg.mkdir(parents=True)
    (cfg / "game.yaml").write_text("id: tg\nname: TG\n", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "cfg")
    with pytest.raises(detect.DetectorUnavailable):
        kf.killfeed_ocr([video], {}, _profile(profile))
    assert detect.run_detectors([video], _profile(profile)) == []
    assert any("ignoré" in r.message for r in caplog.records)


def test_registered():
    assert "killfeed_ocr" in detect.DETECTORS


# --------------------------------------------------------------------------- vrai OCR


def _killfeed_video(out: Path) -> Path:
    def line(a, b, text, y):
        return (
            f"drawtext=text='{text}':fontcolor=white:fontsize=30:box=1:boxcolor=black@0.6:"
            f"x=w-tw-20:y={y}:enable='between(t,{a},{b})'"
        )

    vf = ",".join(
        [
            line(2, 6, "Player ELIMINATED Alpha", 20),
            line(5, 8, "Player ELIMINATED Bravo", 70),
            line(14, 16.5, "Player ELIMINATED Charlie", 20),
        ]
    )
    ff(
        *("-f", "lavfi", "-i", "color=c=0x303a44:s=960x540:r=10:d=18"),
        *("-vf", vf, "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"),
        str(out),
    )
    return out


def test_real_rapidocr_on_synthetic_killfeed(tmp_path, monkeypatch, profile):
    pytest.importorskip("rapidocr")
    pytest.importorskip("onnxruntime")
    src = _killfeed_video(tmp_path / "kf.mp4")
    game = tmp_path / "cfg" / "games" / "tg"
    game.mkdir(parents=True)
    (game / "game.yaml").write_text("id: tg\nname: TG\ncombo_window_s: 6.0\n", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "cfg")
    kf._ROI_CACHE.clear()

    hs = detect.run_detectors([src], _profile(profile))
    assert [h.label for h in hs] == ["multi_kill", "kill"]
    assert hs[0].signals["kills"] == 2.0
    assert hs[0].signals["peak_t"] == pytest.approx(5.0, abs=0.75)
    assert hs[1].signals["peak_t"] == pytest.approx(14.0, abs=0.75)
    (roi,) = kf._ROI_CACHE.values()
    assert roi[0] > 0.4 and roi[1] < 0.3 and roi[0] + roi[2] <= 1.0


def test_frames_early_stop_ignores_epipe_exit_code(monkeypatch, tmp_path):
    """Arrêt volontaire (limit) : un code de sortie ffmpeg exotique (EPIPE) n'est pas une erreur."""
    pytest.importorskip("numpy")
    src = tmp_path / "v.mp4"
    ff("-f", "lavfi", "-i", "testsrc=size=64x64:rate=10:d=2", "-pix_fmt", "yuv420p", str(src))
    info = ffmpeg.probe(src)

    real_popen = kf.subprocess.Popen

    class Proc:
        def __init__(self, *a, **k):
            self._p = real_popen(*a, **k)
            self.stdout = self._p.stdout

        def poll(self):
            return self._p.poll()

        def kill(self):
            self._p.kill()

        def wait(self):
            self._p.wait()
            return 141  # SIGPIPE/EPIPE

    monkeypatch.setattr(kf.subprocess, "Popen", Proc)
    frames = kf._frames(src, info, 10, None, 64)
    next(frames)
    frames.close()  # lecteur qui s'arrête tôt : ne doit pas lever
    # lecture complète avec code d'erreur réel : l'erreur reste signalée
    with pytest.raises(ffmpeg.FFmpegError):
        list(kf._frames(src, info, 10, None, 64))
