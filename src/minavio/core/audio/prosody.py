"""Prosodie par fenêtre : loudness, hauteur, variation d'énergie, débit (z-scores sur le clip)."""

from __future__ import annotations

from pathlib import Path

SAMPLE_RATE = 16000
_HOP = 512


def _z(values):
    import numpy as np

    v = np.asarray(values, dtype=np.float64)
    if v.size == 0:
        return v
    std = float(v.std())
    if std < 1e-9:
        return np.zeros_like(v)
    return (v - float(v.mean())) / std


def _speech_rate(transcript, n_win: int, window_s: float):
    """Mots/s par fenêtre ; segments sans mots : répartition uniforme des mots du texte."""
    import numpy as np

    counts = np.zeros(n_win)

    def add(start: float, end: float, n: float) -> None:
        dur = max(end - start, 1e-6)
        i0 = max(0, int(start // window_s))
        i1 = min(n_win - 1, int(max(start, end - 1e-9) // window_s))
        for i in range(i0, i1 + 1):
            lo, hi = max(start, i * window_s), min(end, (i + 1) * window_s)
            if hi > lo:
                counts[i] += n * (hi - lo) / dur

    for seg in transcript:
        words = getattr(seg, "words", None) or []
        if words:
            for w in words:
                add(w.start, w.end, 1.0)
        else:
            add(seg.start, seg.end, float(len(seg.text.split())))
    return counts / window_s


def prosody_features(
    source: Path, transcript=None, window_s: float = 0.5
) -> list[tuple[float, dict[str, float]]]:
    """[(t_début, {loudness_z, pitch_z, energy_var_z[, speech_rate_z]}), ...] par fenêtre."""
    import librosa
    import numpy as np

    from minavio.core.ffmpeg import decode_pcm

    y = decode_pcm(source, SAMPLE_RATE)
    if y.size < SAMPLE_RATE * window_s:
        return []
    win = round(window_s * SAMPLE_RATE)
    n_win = int(np.ceil(y.size / win))

    # loudness : RMS en dB par fenêtre
    pad = np.pad(y, (0, n_win * win - y.size))
    rms = np.sqrt((pad.reshape(n_win, win) ** 2).mean(axis=1)) + 1e-9
    loud = _z(20 * np.log10(rms))

    # hauteur : médiane de f0 voisée par fenêtre (pyin), 0 si fenêtre non voisée
    f0, _, _ = librosa.pyin(y, fmin=65, fmax=800, sr=SAMPLE_RATE, hop_length=_HOP)
    frame_t = np.arange(f0.size) * _HOP / SAMPLE_RATE
    wid = np.minimum((frame_t // window_s).astype(int), n_win - 1)
    pitch_raw = np.full(n_win, np.nan)
    for i in range(n_win):
        sel = f0[(wid == i) & ~np.isnan(f0)]
        if sel.size:
            pitch_raw[i] = float(np.median(sel))
    pitch = np.zeros(n_win)
    ok = ~np.isnan(pitch_raw)
    if ok.sum() >= 2:
        pitch[ok] = _z(pitch_raw[ok])

    # variation d'énergie : force d'onset moyenne
    onset = librosa.onset.onset_strength(y=y, sr=SAMPLE_RATE, hop_length=_HOP)
    ot = np.arange(onset.size) * _HOP / SAMPLE_RATE
    oid = np.minimum((ot // window_s).astype(int), n_win - 1)
    sums = np.bincount(oid, weights=onset, minlength=n_win)
    cnts = np.maximum(np.bincount(oid, minlength=n_win), 1)
    energy = _z(sums / cnts)

    rate = _z(_speech_rate(transcript, n_win, window_s)) if transcript else None

    out: list[tuple[float, dict[str, float]]] = []
    for i in range(n_win):
        feats = {
            "loudness_z": float(loud[i]),
            "pitch_z": float(pitch[i]),
            "energy_var_z": float(energy[i]),
        }
        if rate is not None:
            feats["speech_rate_z"] = float(rate[i])
        out.append((round(i * window_s, 6), feats))
    return out
