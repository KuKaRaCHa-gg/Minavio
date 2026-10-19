"""Détection des temps (beats) d'une musique avec librosa."""

from __future__ import annotations

from pathlib import Path


def detect_beats(music: Path) -> list[float]:
    """Temps des beats en secondes (depuis le début de la musique)."""
    import librosa

    y, sr = librosa.load(str(music), sr=22050, mono=True)
    _tempo, frames = librosa.beat.beat_track(y=y, sr=sr)
    return [round(float(t), 4) for t in librosa.frames_to_time(frames, sr=sr)]
