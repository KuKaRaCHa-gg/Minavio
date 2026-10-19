"""Classification d'événements sonores (PANNs CNN14 / AudioSet) : rires, cris, applaudissements…

Import paresseux de `panns_inference`/`torch` (extra `hype`). Aucun appel réseau par Minavio :
le checkpoint est lu via `MINAVIO_PANNS_CHECKPOINT`, sinon `panns_inference` le télécharge
lui-même (sur la machine de l'utilisateur) à la première utilisation.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from minavio.core.detect.registry import DetectorUnavailable

log = logging.getLogger("minavio.audio.events")

SAMPLE_RATE = 32000
CHECKPOINT_ENV = "MINAVIO_PANNS_CHECKPOINT"

# Signal Minavio -> libellés AudioSet (le max des classes du groupe est retenu).
EVENT_GROUPS: dict[str, tuple[str, ...]] = {
    "laughter": ("Laughter", "Giggle", "Belly laugh", "Chuckle, chortle"),
    "scream": ("Screaming", "Shout", "Yell", "Whoop"),
    "cheering": ("Cheering",),
    "applause": ("Applause",),
    "gunshot": ("Gunshot, gunfire", "Machine gun"),
    "explosion": ("Explosion",),
    "crying": ("Crying, sobbing",),
}


def _load_tagger(device: str):
    """Charge AudioTagging (CNN14). Point d'injection pour les tests."""
    try:
        from panns_inference import AudioTagging
    except ImportError as e:
        raise DetectorUnavailable("panns-inference absent : extra 'hype'") from e
    ckpt = os.environ.get(CHECKPOINT_ENV) or None
    if ckpt and not Path(ckpt).is_file():
        raise DetectorUnavailable(f"{CHECKPOINT_ENV}={ckpt} : fichier introuvable")
    try:
        return AudioTagging(checkpoint_path=ckpt, device=device)
    except Exception as e:  # téléchargement impossible, checkpoint corrompu...
        raise DetectorUnavailable(f"chargement PANNs impossible ({e})") from e


def _label_names() -> list[str]:
    try:
        from panns_inference import labels
    except ImportError as e:
        raise DetectorUnavailable("panns-inference absent : extra 'hype'") from e
    return list(labels)


def _device() -> str:
    try:
        import torch

        return "cuda" if torch.cuda.is_available() else "cpu"
    except ImportError:
        return "cpu"


def _free_vram() -> None:
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:  # pragma: no cover - best effort
        pass


def audio_events(
    source: Path,
    window_s: float = 0.5,
    *,
    context_s: float = 2.0,
    batch_size: int = 32,
) -> list[tuple[float, dict[str, float]]]:
    """Probabilités d'événements par fenêtre : [(t_début, {signal: proba 0..1}), ...].

    Chaque fenêtre de `window_s` est classée sur un contexte de `context_s` centré sur elle
    (CNN14 est peu fiable en dessous d'~1 s). Lève `DetectorUnavailable` si PANNs est absent.
    """
    import numpy as np

    from minavio.core.ffmpeg import decode_pcm

    labels = _label_names()
    index = {name: i for i, name in enumerate(labels)}
    groups = {sig: [index[n] for n in names if n in index] for sig, names in EVENT_GROUPS.items()}
    groups = {sig: idx for sig, idx in groups.items() if idx}

    audio = decode_pcm(source, SAMPLE_RATE)
    if audio.size == 0:
        return []
    win = round(window_s * SAMPLE_RATE)
    ctx = max(win, round(context_s * SAMPLE_RATE))
    n_win = max(1, int(np.ceil(audio.size / win)))
    margin = (ctx - win) // 2
    padded = np.pad(audio, (margin, ctx + win))

    tagger = _load_tagger(_device())
    out: list[tuple[float, dict[str, float]]] = []
    try:
        for b0 in range(0, n_win, batch_size):
            ids = range(b0, min(n_win, b0 + batch_size))
            batch = np.stack([padded[i * win : i * win + ctx] for i in ids]).astype(np.float32)
            clipwise = np.asarray(tagger.inference(batch)[0])
            for row, i in zip(clipwise, ids, strict=True):
                probs = {sig: float(row[idx].max()) for sig, idx in groups.items()}
                out.append((round(i * window_s, 6), probs))
    finally:
        del tagger
        _free_vram()
    return out
