"""Détecteur « hype » : intensité audio (rires, cris, explosions + prosodie) fusionnée par fenêtre.

Fusion en « noisy-OR » : score = 1 - prod(1 - clip(poids * valeur, 0, 1)). Les probabilités PANNs
sont utilisées telles quelles ; les z-scores de prosodie sont ramenés dans 0..1 par z / z_full.
Sans PANNs (extra `hype`), seule la prosodie est utilisée (warning unique).
"""

from __future__ import annotations

import logging
from pathlib import Path

from minavio.config.schema import ChannelProfile
from minavio.core import ffmpeg
from minavio.core.detect.registry import DetectorUnavailable, register_detector
from minavio.core.models import Highlight

log = logging.getLogger("minavio.detect.hype")

DEFAULT_WEIGHTS: dict[str, float] = {
    # événements sonores (probabilités PANNs)
    "laughter": 1.0,
    "scream": 1.0,
    "cheering": 0.7,
    "applause": 0.4,
    "gunshot": 0.8,
    "explosion": 0.8,
    "crying": 0.3,
    # prosodie (z-scores)
    "loudness_z": 0.5,
    "pitch_z": 0.3,
    "energy_var_z": 0.3,
    "speech_rate_z": 0.2,
}

LABELS: dict[str, str] = {
    "laughter": "laugh",
    "scream": "scream",
    "cheering": "cheer",
    "applause": "cheer",
    "gunshot": "gunshot",
    "explosion": "explosion",
    "crying": "cry",
}  # tout signal de prosodie -> "hype"

_warned: set[str] = set()


def _warn_once(key: str, msg: str) -> None:
    if key not in _warned:
        _warned.add(key)
        log.warning(msg)


def _make_highlight(**kw) -> Highlight:
    """Renseigne `signals` si le contrat `Highlight` le porte (sinon repli dans `reason`)."""
    signals = kw.pop("signals")
    if "signals" in Highlight.model_fields:
        return Highlight(**kw, signals=signals)
    reason = kw.pop("reason", "") or ""
    detail = ", ".join(f"{k}={v:.2f}" for k, v in signals.items())
    return Highlight(**kw, reason=f"{reason} [{detail}]".strip())


def _collect(source: Path, window_s: float, params: dict):
    """Signaux par fenêtre : (n_fenêtres, {nom: liste}). Lève DetectorUnavailable si rien."""
    from minavio.core.audio import events, prosody

    series: dict[str, dict[int, float]] = {}

    def feed(rows) -> None:
        for t, sigs in rows:
            i = round(t / window_s)
            for name, value in sigs.items():
                series.setdefault(name, {})[i] = float(value)

    have = False
    if params.get("use_panns", True):
        try:
            feed(events.audio_events(source, window_s))
            have = True
        except (DetectorUnavailable, ImportError) as e:
            _warn_once("panns", f"hype : PANNs indisponible ({e}) ; prosodie seule")
    try:
        feed(prosody.prosody_features(source, params.get("transcript"), window_s))
        have = True
    except ImportError as e:
        _warn_once("prosody", f"hype : prosodie indisponible ({e}) : extra 'audio'")
    if not have:
        raise DetectorUnavailable("ni PANNs (extra 'hype') ni librosa (extra 'audio') disponibles")
    n = max((max(s) for s in series.values() if s), default=-1) + 1
    return n, series


@register_detector("hype")
def hype(sources: list[Path], params: dict, profile: ChannelProfile) -> list[Highlight]:
    try:
        import numpy as np
    except ImportError as e:
        raise DetectorUnavailable("numpy absent (extra 'vision')") from e

    window_s = float(params.get("window_s", 0.5))
    threshold = float(params.get("threshold", 0.45))
    smooth_s = float(params.get("smooth_s", 1.0))
    z_full = float(params.get("z_full", 3.0))
    pad_s = float(params.get("pad_s", 1.0))
    min_len_s = float(params.get("min_len_s", 2.0))
    weights = {**DEFAULT_WEIGHTS}
    weights.update({k: float(v) for k, v in params.get("weights", {}).items()})
    weights.update({k: float(params[k]) for k in DEFAULT_WEIGHTS if k in params})

    out: list[Highlight] = []
    for src in sources:
        if not ffmpeg.probe(src).has_audio:
            continue
        n, series = _collect(src, window_s, params)
        if n < 1:
            continue
        duration = ffmpeg.probe(src).duration

        names = [k for k in series if weights.get(k, 0) > 0]
        if not names:
            continue
        raw = np.zeros((len(names), n))
        for r, name in enumerate(names):
            for i, v in series[name].items():
                raw[r, i] = v
        # contribution 0..1 de chaque signal
        scale = np.array([z_full if k.endswith("_z") else 1.0 for k in names])[:, None]
        w = np.array([weights[k] for k in names])[:, None]
        contrib = np.clip(w * np.clip(raw, 0, None) / scale, 0, 1)
        # lissage (moyenne glissante) par signal puis fusion noisy-OR
        k = max(1, round(smooth_s / window_s))
        if k > 1:
            kern = np.ones(k) / k
            contrib = np.stack([np.convolve(c, kern, mode="same") for c in contrib])
        fused = 1 - np.prod(1 - contrib, axis=0)

        lo = threshold * 0.7
        i = 0
        while i < n:
            if fused[i] < lo:
                i += 1
                continue
            j = i
            while j + 1 < n and fused[j + 1] >= lo:
                j += 1
            peak_i = i + int(np.argmax(fused[i : j + 1]))
            peak = float(fused[peak_i])
            if peak >= threshold:
                col = contrib[:, peak_i]
                dom = names[int(np.argmax(col))]
                label = LABELS.get(dom, "hype")
                start = max(0.0, i * window_s - pad_s)
                end = min(duration, (j + 1) * window_s + pad_s)
                if end - start < min_len_s:
                    mid = (start + end) / 2
                    start = max(0.0, mid - min_len_s / 2)
                    end = min(duration, start + min_len_s)
                if end > start:
                    signals = {nm: round(float(raw[r, peak_i]), 3) for r, nm in enumerate(names)}
                    signals["fused"] = round(peak, 3)
                    out.append(
                        _make_highlight(
                            source=src,
                            start=start,
                            end=end,
                            score=min(1.0, peak),
                            label=label,
                            detector="hype",
                            reason=f"intensité {label} ({dom}) à {peak_i * window_s:.1f}s",
                            signals=signals,
                        )
                    )
            i = j + 1
    return out
