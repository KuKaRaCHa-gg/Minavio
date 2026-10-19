"""Détecteur hype : prosodie et fusion sur audio synthétique, PANNs entièrement mocké."""

import logging

import numpy as np
import pytest

from minavio.config.schema import ChannelProfile, DetectorRef
from minavio.core import detect
from minavio.core.audio import events, prosody
from minavio.core.detect import hype as hype_mod
from minavio.core.detect.registry import DetectorUnavailable
from minavio.core.models import Highlight
from tests.core.helpers import ff

pytest.importorskip("librosa")


def video(out, expr):
    """Vidéo noire 12 s + piste audio synthétique (ffprobe/decode_pcm exigent un flux vidéo)."""
    ff(
        *("-f", "lavfi", "-i", "color=c=black:s=160x90:r=10:d=12"),
        *("-f", "lavfi", "-i", f"aevalsrc='{expr}':s=32000:d=12"),
        *("-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac"),
        *("-b:a", "192k", "-shortest", str(out)),
    )


@pytest.fixture(scope="module")
def burst_audio(tmp_path_factory):
    """12 s : bruit calme grave (200 Hz) + rafale forte et aiguë entre 5.0 et 6.5 s."""
    out = tmp_path_factory.mktemp("hype") / "burst.mp4"
    expr = "0.04*sin(2*PI*200*t)+between(t,5,6.5)*0.8*sin(2*PI*900*t)"
    video(out, expr)
    return out


@pytest.fixture(scope="module")
def calm_audio(tmp_path_factory):
    out = tmp_path_factory.mktemp("hype") / "calm.mp4"
    video(out, "0.04*sin(2*PI*200*t)")
    return out


def only(profile: ChannelProfile, **params) -> ChannelProfile:
    return profile.model_copy(update={"detection": [DetectorRef(name="hype", params=params)]})


@pytest.fixture(autouse=True)
def _reset_warnings():
    hype_mod._warned.clear()


@pytest.fixture
def no_panns(monkeypatch):
    def boom(*a, **k):
        raise DetectorUnavailable("panns absent (mock)")

    monkeypatch.setattr(events, "audio_events", boom)


def test_registered():
    assert {"hype", "chat_spike"} <= set(detect.DETECTORS)


def test_prosody_burst_has_higher_z(burst_audio):
    rows = prosody.prosody_features(burst_audio, window_s=0.5)
    assert len(rows) == 24
    by_t = dict(rows)
    assert by_t[5.5]["loudness_z"] > by_t[1.0]["loudness_z"] + 1.5
    assert by_t[5.5]["energy_var_z"] != by_t[1.0]["energy_var_z"]
    assert "speech_rate_z" not in by_t[0.0]


def test_prosody_speech_rate_from_transcript(calm_audio):
    from minavio.core.models import TranscriptSegment

    seg = TranscriptSegment(start=2.0, end=4.0, text="un deux trois quatre cinq six")
    by_t = dict(prosody.prosody_features(calm_audio, [seg], window_s=0.5))
    assert by_t[3.0]["speech_rate_z"] > 1.0 > by_t[8.0]["speech_rate_z"] + 0.5


def test_hype_prosody_only_warns_once(burst_audio, profile, no_panns, caplog):
    caplog.set_level(logging.WARNING, logger="minavio.detect.hype")
    p = only(profile)
    hs = detect.run_detectors([burst_audio], p)
    detect.run_detectors([burst_audio], p)
    assert len(hs) == 1
    h = hs[0]
    assert h.start < 5.5 < h.end
    assert h.label == "hype"
    assert 0 < h.score <= 1
    assert sum("PANNs indisponible" in r.message for r in caplog.records) == 1


def test_hype_calm_audio_has_no_highlight(calm_audio, profile, no_panns):
    assert detect.run_detectors([calm_audio], only(profile)) == []


def test_hype_threshold_param(burst_audio, profile, no_panns):
    assert detect.run_detectors([burst_audio], only(profile, threshold=0.99)) == []


def test_hype_fuses_mocked_events(calm_audio, profile, monkeypatch):
    rows = [(i * 0.5, {"laughter": 0.05, "scream": 0.0}) for i in range(24)]
    for i in range(14, 18):  # rire entre 7 s et 9 s
        rows[i] = (i * 0.5, {"laughter": 0.95, "scream": 0.1})
    monkeypatch.setattr(events, "audio_events", lambda src, window_s=0.5: rows)
    hs = detect.run_detectors([calm_audio], only(profile))
    assert len(hs) == 1
    assert hs[0].label == "laugh"
    assert hs[0].start < 8.0 < hs[0].end
    if "signals" in Highlight.model_fields:
        assert hs[0].signals["laughter"] == pytest.approx(0.95)


def test_hype_weights_override_changes_label(calm_audio, profile, monkeypatch):
    rows = [(i * 0.5, {"laughter": 0.0, "scream": 0.0}) for i in range(24)]
    for i in range(14, 18):
        rows[i] = (i * 0.5, {"laughter": 0.9, "scream": 0.9})
    monkeypatch.setattr(events, "audio_events", lambda src, window_s=0.5: rows)
    hs = detect.run_detectors([calm_audio], only(profile, weights={"laughter": 0.2}))
    assert [h.label for h in hs] == ["scream"]


def test_hype_everything_unavailable_is_skipped(calm_audio, profile, no_panns, monkeypatch):
    def no_librosa(*a, **k):
        raise ImportError("librosa")

    monkeypatch.setattr(prosody, "prosody_features", no_librosa)
    assert detect.run_detectors([calm_audio], only(profile)) == []


def test_audio_events_with_fake_tagger(burst_audio, monkeypatch):
    labels = [f"c{i}" for i in range(527)]
    labels[10], labels[11], labels[20] = "Laughter", "Giggle", "Screaming"

    class FakeTagger:
        def __init__(self):
            self.calls = 0

        def inference(self, batch):
            self.calls += 1
            assert batch.ndim == 2 and batch.dtype == np.float32
            out = np.zeros((batch.shape[0], 527), dtype=np.float32)
            loud = np.abs(batch).max(axis=1) > 0.5
            out[loud, 20] = 0.9
            out[~loud, 11] = 0.1
            return out, np.zeros((batch.shape[0], 2048), dtype=np.float32)

    fake = FakeTagger()
    monkeypatch.setattr(events, "_label_names", lambda: labels)
    monkeypatch.setattr(events, "_load_tagger", lambda device: fake)
    rows = events.audio_events(burst_audio, 0.5)
    assert len(rows) == 24
    by_t = dict(rows)
    assert set(by_t[0.0]) == {"laughter", "scream"}  # seuls les groupes présents dans labels
    assert by_t[5.5]["scream"] == pytest.approx(0.9)
    assert by_t[0.0]["scream"] == 0.0
    assert by_t[0.0]["laughter"] == pytest.approx(0.1)
    assert fake.calls == 1  # 24 fenêtres < batch_size


def test_audio_events_unavailable(burst_audio, monkeypatch):
    def boom():
        raise DetectorUnavailable("panns")

    monkeypatch.setattr(events, "_label_names", boom)
    with pytest.raises(DetectorUnavailable):
        events.audio_events(burst_audio)


def test_audio_events_missing_checkpoint(monkeypatch, tmp_path):
    pytest.importorskip("panns_inference", reason="extra 'hype' absent")
    monkeypatch.setenv(events.CHECKPOINT_ENV, str(tmp_path / "nope.pth"))
    with pytest.raises(DetectorUnavailable):
        events._load_tagger("cpu")
