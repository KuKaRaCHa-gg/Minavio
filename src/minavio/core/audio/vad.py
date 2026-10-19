"""Détection de parole (Silero VAD, ONNX si possible)."""

from __future__ import annotations

from pathlib import Path


def detect_speech(
    path: Path, threshold: float = 0.5, min_speech_ms: int = 250
) -> list[tuple[float, float]]:
    """Segments de parole (début, fin) en secondes."""
    import torch
    from silero_vad import get_speech_timestamps, load_silero_vad

    from minavio.core.ffmpeg import decode_pcm

    pcm = decode_pcm(path, 16000)
    if pcm.size == 0:
        return []
    try:
        model = load_silero_vad(onnx=True)
    except Exception:
        model = load_silero_vad()
    stamps = get_speech_timestamps(
        torch.from_numpy(pcm),
        model,
        threshold=threshold,
        min_speech_duration_ms=min_speech_ms,
        sampling_rate=16000,
        return_seconds=True,
    )
    return [(float(s["start"]), float(s["end"])) for s in stamps if s["end"] > s["start"]]
