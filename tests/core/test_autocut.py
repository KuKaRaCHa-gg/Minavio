from pathlib import Path

import pytest

from minavio.config.schema import AutocutSettings
from minavio.core import autocut
from minavio.core.autocut import analyze_keep_spans
from minavio.core.models import TranscriptSegment, Word
from tests.core.helpers import ff

TOL = 0.08  # trames de 50 ms
EXPECTED = [(0, 2.25), (3.88, 5.25), (6.88, 8.0)]


@pytest.fixture(scope="module")
def audio(tmp_path_factory) -> Path:
    """0-2 s sinus « voix », 2-4 silence, 4-5 bruit fort « action », 5-7 silence, 7-8 sinus."""
    out = tmp_path_factory.mktemp("autocut") / "mix.mkv"
    sr = "r=16000"
    ff(
        *("-f", "lavfi", "-i", "color=c=black:s=64x64:r=10:d=8"),
        *("-f", "lavfi", "-i", "sine=f=440:d=2:sample_rate=16000"),
        *("-f", "lavfi", "-i", f"anullsrc={sr}:cl=mono:d=2"),
        *("-f", "lavfi", "-i", f"anoisesrc=d=1:a=0.5:{sr}"),
        *("-f", "lavfi", "-i", f"anullsrc={sr}:cl=mono:d=2"),
        *("-f", "lavfi", "-i", "sine=f=440:d=1:sample_rate=16000"),
        *(
            "-filter_complex",
            "[1][2][3][4][5]concat=n=5:v=0:a=1,aformat=sample_fmts=s16:channel_layouts=mono[a]",
        ),
        *(
            "-map",
            "0:v",
            "-map",
            "[a]",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "pcm_s16le",
        ),
        str(out),
    )
    return out


@pytest.fixture
def fake_vad(monkeypatch):
    monkeypatch.setattr(autocut, "detect_speech", lambda path: [(0.0, 2.0), (7.0, 8.0)])


def _close(spans, expected):
    assert len(spans) == len(expected), spans
    for (a, b), (c, d) in zip(spans, expected, strict=True):
        assert a == pytest.approx(c, abs=TOL) and b == pytest.approx(d, abs=TOL)


def test_cuts_silences_keeps_voice_and_action(audio, fake_vad):
    _close(analyze_keep_spans(audio, AutocutSettings()), EXPECTED)


def test_spans_sorted_disjoint(audio, fake_vad):
    spans = analyze_keep_spans(audio, AutocutSettings(margin_after_s=0.5, margin_before_s=0.4))
    assert spans == sorted(spans)
    assert all(a < b for a, b in spans)
    assert all(spans[i][1] <= spans[i + 1][0] for i in range(len(spans) - 1))


def test_short_gaps_kept(audio, fake_vad):
    spans = analyze_keep_spans(audio, AutocutSettings(min_silence_s=3.0))
    _close(spans, [(0.0, 8.0)])


def test_voice_in_quiet_zone_is_kept(audio, monkeypatch):
    monkeypatch.setattr(autocut, "detect_speech", lambda path: [(0, 2), (2.5, 3.5), (7, 8)])
    spans = analyze_keep_spans(audio, AutocutSettings())
    assert any(a <= 2.5 and b >= 3.5 for a, b in spans)


def test_vad_failure_falls_back_to_energy(audio, monkeypatch, caplog):
    def boom(path):
        raise ImportError("no silero")

    monkeypatch.setattr(autocut, "detect_speech", boom)
    with caplog.at_level("WARNING"):
        spans = analyze_keep_spans(audio, AutocutSettings())
    _close(spans, EXPECTED)
    assert "VAD indisponible" in caplog.text


def test_no_cut_edge_inside_word(audio, fake_vad):
    seg = TranscriptSegment(
        start=2.1,
        end=4.0,
        text="a b",
        words=[Word(start=2.1, end=2.6, text="a"), Word(start=3.6, end=4.0, text="b")],
    )
    spans = analyze_keep_spans(audio, AutocutSettings(), transcript=[seg])
    assert spans[0][1] == pytest.approx(2.6, abs=TOL)  # coupe repoussée après le mot
    assert spans[1][0] == pytest.approx(3.6, abs=TOL)  # coupe reculée avant le mot
    other = seg.model_copy(update={"source": Path("autre.mp4")})  # autre source : ignoré
    _close(analyze_keep_spans(audio, AutocutSettings(), [other]), EXPECTED)


def test_no_audio_track_keeps_everything(tmp_path):
    out = tmp_path / "mute.mp4"
    ff(*("-f", "lavfi", "-i", "color=c=black:s=64x64:r=10:d=2"), "-pix_fmt", "yuv420p", str(out))
    _close(analyze_keep_spans(out, AutocutSettings()), [(0.0, 2.0)])


def test_real_silero(audio):
    pytest.importorskip("torch")
    pytest.importorskip("silero_vad")
    spans = analyze_keep_spans(audio, AutocutSettings())
    assert spans and spans == sorted(spans)
    # le bruit fort (4-5 s) est de l'action : toujours gardé, quel que soit le verdict du VAD
    assert any(a <= 4.1 and b >= 4.9 for a, b in spans)
