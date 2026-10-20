"""Débruitage de la voix : DeepFilterNet (48 kHz) sinon arnndn (filtre FFmpeg, modèle embarqué).

Licences : DeepFilterNet MIT/Apache-2.0 ; modèle RNNoise `bd.rnnn` (GregorR/rnnoise-models,
non soumis au droit d'auteur selon son README) ; voir `assets/rnnoise/README.md`.
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path

from minavio.core import ffmpeg
from minavio.errors import MinavioError

log = logging.getLogger(__name__)

RNNOISE_MODEL = Path(__file__).resolve().parent.parent / "assets" / "rnnoise" / "bd.rnnn"
SAMPLE_RATE = 48000


def deepfilternet_available() -> bool:
    return importlib.util.find_spec("df") is not None


def _escape_filter_value(value: str) -> str:
    """Échappement d'une valeur d'option de filtre FFmpeg (chemin)."""
    for ch in ("\\", ":", "'", ",", ";", "[", "]", "="):
        value = value.replace(ch, "\\" + ch)
    return value


def arnndn_filter(model: Path = RNNOISE_MODEL) -> str:
    return f"arnndn=m={_escape_filter_value(model.as_posix())}"


def denoise_arnndn(src: Path, dst: Path, model: Path = RNNOISE_MODEL) -> None:
    if not model.exists():
        raise MinavioError(f"Modèle RNNoise introuvable : {model} (réinstallez minavio).")
    ffmpeg.run(
        [
            *("-i", str(src)),
            *("-vn", "-af", f"aresample={SAMPLE_RATE},{arnndn_filter(model)}"),
            *("-ar", str(SAMPLE_RATE), "-ac", "2", "-c:a", "pcm_s16le"),
            str(dst),
        ]
    )


def denoise_deepfilternet(src: Path, dst: Path) -> None:
    """DeepFilterNet via `df.enhance` ; libère la VRAM ensuite. Lève ImportError si absent."""
    from df.enhance import enhance, init_df, load_audio, save_audio

    model = df_state = audio = enhanced = None
    try:
        model, df_state, _ = init_df()
        audio, _ = load_audio(str(src), sr=df_state.sr())
        enhanced = enhance(model, df_state, audio)
        save_audio(str(dst), enhanced, df_state.sr())
    finally:
        del model, df_state, audio, enhanced
        _free_vram()


def _free_vram() -> None:
    import gc

    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


def resolve_backend(backend: str) -> str:
    if backend == "auto":
        return "deepfilternet" if deepfilternet_available() else "arnndn"
    return backend


def denoise(src: Path, dst: Path, backend: str = "auto") -> str:
    """Débruite `src` -> `dst` (wav 48 kHz). Retourne le backend réellement utilisé.

    DeepFilterNet absent ou en échec -> repli sur arnndn avec un warning.
    """
    chosen = resolve_backend(backend)
    if chosen == "deepfilternet":
        try:
            denoise_deepfilternet(src, dst)
        except Exception as e:
            log.warning("DeepFilterNet indisponible (%s) : repli sur arnndn.", e)
        else:
            return "deepfilternet"
    denoise_arnndn(src, dst)
    return "arnndn"
