"""Séparation de sources avec Demucs (htdemucs : vocals / drums / bass / other).

Demucs (MIT) tourne dans un sous-processus (`python -m demucs.separate`, liste d'arguments) : la
VRAM est donc intégralement libérée à la sortie du processus. Aucun modèle n'est embarqué ; Demucs
le télécharge lui-même au premier usage (les tests mockent `run_demucs`).
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

from minavio.core import ffmpeg
from minavio.errors import MinavioError

STEMS = ("vocals", "drums", "bass", "other")
DEMUCS_RATE = 44100
OTHER_ATTENUATION_DB = -6.0
_STDERR_LIMIT = 2000


def demucs_available() -> bool:
    return importlib.util.find_spec("demucs") is not None


def _device() -> str:
    try:
        import torch

        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:
        return "cpu"


def run_demucs(wav: Path, out_dir: Path, model: str) -> dict[str, Path]:
    """Sépare `wav` ; retourne {stem: fichier wav}. Lève MinavioError en cas d'échec."""
    if not demucs_available():
        raise MinavioError("Demucs absent : installez l'extra avec `uv sync --extra cleanaudio`.")
    cmd = [
        sys.executable,
        *("-m", "demucs.separate"),
        *("-n", model),
        *("-d", _device()),
        *("-o", str(out_dir)),
        "--float32",
        str(wav),
    ]
    proc = subprocess.run(cmd, capture_output=True, check=False)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip()[-_STDERR_LIMIT:]
        hint = ""
        if "URLError" in err or "urlopen" in err:
            hint = (
                "\nDemucs télécharge ses poids au premier usage (~80 Mo, dl.fbaipublicfiles.com) : "
                f"vérifie la connexion, ou lance une fois `python -m demucs.separate -n {model} "
                "<fichier.wav>` pour les mettre en cache."
            )
        raise MinavioError(
            f"Demucs a échoué (code {proc.returncode}) : {' '.join(cmd)}\n{err}{hint}"
        )
    stem_dir = out_dir / model / wav.stem
    stems = {s: stem_dir / f"{s}.wav" for s in STEMS}
    missing = [s for s, p in stems.items() if not p.exists()]
    if missing:
        raise MinavioError(f"Demucs n'a pas produit les pistes {missing} dans {stem_dir}.")
    return stems


def extract_wav(source: Path, dst: Path, rate: int = DEMUCS_RATE) -> None:
    ffmpeg.run(
        [
            *("-i", str(source)),
            *("-vn", "-ac", "2", "-ar", str(rate), "-c:a", "pcm_f32le"),
            str(dst),
        ]
    )


def mix_stems(parts: list[tuple[Path, float]], dst: Path, rate: int = DEMUCS_RATE) -> None:
    """Somme de pistes [(wav, gain dB)] -> wav ; sans normalisation automatique."""
    args: list[str] = []
    chains = []
    for i, (path, gain) in enumerate(parts):
        args += ["-i", str(path)]
        chains.append(
            f"[{i}:a]aresample={rate},aformat=channel_layouts=stereo,volume={gain}dB[a{i}]"
        )
    mix_in = "".join(f"[a{i}]" for i in range(len(parts)))
    graph = ";".join(chains) + f";{mix_in}amix=inputs={len(parts)}:normalize=0:duration=longest[o]"
    ffmpeg.run(
        [
            *args,
            *("-filter_complex", graph),
            *("-map", "[o]", "-ar", str(rate), "-c:a", "pcm_f32le"),
            str(dst),
        ]
    )


def separate(source_wav: Path, work: Path, model: str = "htdemucs") -> dict[str, Path]:
    return run_demucs(source_wav, work / "demucs", model)
