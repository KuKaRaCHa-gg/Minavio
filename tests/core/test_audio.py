import itertools
import statistics

import pytest

from minavio.config.schema import DuckingSettings
from minavio.core.audio import mix


def test_loudnorm_uses_profile_lufs():
    assert mix.loudnorm_filter(-16).startswith("loudnorm=I=-16")


def test_audio_graph_with_ducking():
    parts = mix.audio_graph("voice", "4:a", 20, -14, DuckingSettings(music_gain_db=-16))
    joined = ";".join(parts)
    assert "sidechaincompress" in joined and "attack=20" in joined and "release=300" in joined
    assert "volume=-16.0dB" in joined
    assert "loudnorm=I=-14" in joined
    assert joined.endswith("[aout]")


def test_audio_graph_without_ducking_or_music():
    plain = ";".join(mix.audio_graph("voice", "4:a", 10, -14, DuckingSettings(enabled=False)))
    assert "sidechaincompress" not in plain and "amix" in plain
    assert mix.audio_graph("voice", None, 10, -14, DuckingSettings()) == [
        "[voice]loudnorm=I=-14:TP=-1.5:LRA=11[aout]"
    ]


def test_detect_beats(click_track):
    pytest.importorskip("librosa")
    from minavio.core.audio.beats import detect_beats

    beats = detect_beats(click_track)
    assert len(beats) >= 8
    assert beats == sorted(beats)
    gap = statistics.median(b - a for a, b in itertools.pairwise(beats))
    assert gap == pytest.approx(0.5, abs=0.06) or gap == pytest.approx(0.25, abs=0.06)
