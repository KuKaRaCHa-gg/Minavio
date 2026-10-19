import sys
import types
from pathlib import Path
from types import SimpleNamespace as NS

from minavio.core import asr


def test_transcribe_with_mock_model(monkeypatch):
    calls = {}

    class FakeModel:
        def __init__(self, size, device, compute_type):
            calls["init"] = (size, device, compute_type)

        def transcribe(self, path, language=None, word_timestamps=False):
            calls["transcribe"] = (path, language, word_timestamps)
            words = [
                NS(start=0.0, end=0.4, word=" Salut", probability=0.9),
                NS(start=0.4, end=0.4, word=" tout", probability=1.2),  # durée nulle
                NS(start=0.5, end=0.9, word="  ", probability=0.5),  # vide
                NS(start=0.9, end=1.2, word=" le", probability=None),
            ]
            segs = [
                NS(start=0.0, end=1.2, text=" Salut tout le", words=words),
                NS(start=2.0, end=2.0, text="vide", words=[]),  # ignoré
                NS(start=3.0, end=4.0, text="sans mots", words=None),
            ]
            return iter(segs), NS(language="fr")

    fake = types.ModuleType("faster_whisper")
    fake.WhisperModel = FakeModel
    monkeypatch.setitem(sys.modules, "faster_whisper", fake)
    monkeypatch.setattr(asr, "_cuda_available", lambda: False)

    out = asr.transcribe(Path("clip.mp4"), language="fr")
    assert calls["init"][1:] == ("cpu", "int8")
    assert calls["transcribe"] == ("clip.mp4", "fr", True)
    assert len(out) == 2
    assert [w.text for w in out[0].words] == ["Salut", "tout", "le"]
    assert out[0].words[1].end > out[0].words[1].start
    assert out[0].words[1].confidence == 1.0 and out[0].words[2].confidence is None
    assert out[1].words == []


def test_transcribe_uses_cuda_when_available(monkeypatch):
    seen = {}

    class FakeModel:
        def __init__(self, size, device, compute_type):
            seen["args"] = (size, device, compute_type)

        def transcribe(self, *a, **k):
            return iter([]), NS()

    fake = types.ModuleType("faster_whisper")
    fake.WhisperModel = FakeModel
    monkeypatch.setitem(sys.modules, "faster_whisper", fake)
    monkeypatch.setattr(asr, "_cuda_available", lambda: True)
    assert asr.transcribe(Path("x.mp4")) == []
    assert seen["args"][1:] == ("cuda", "float16")
