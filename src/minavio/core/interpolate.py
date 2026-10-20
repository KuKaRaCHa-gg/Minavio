"""Interpolation d'images pour le ralenti (aucun appel réseau, imports légers).

Deux moteurs :
- `rife` : binaire externe rife-ncnn-vulkan (https://github.com/nihui/rife-ncnn-vulkan, licence
  MIT, modèles RIFE inclus sous la même licence ; GPU Vulkan, ni torch ni python). Trouvé sur le
  PATH ou via `MINAVIO_RIFE_BIN`. Le segment est extrait en images, interpolé, réassemblé dans une
  vidéo intermédiaire DÉJÀ ralentie (durée = durée source / vitesse).
- `minterpolate` : filtre FFmpeg (CPU, lent mais sans dépendance).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from minavio.core import ffmpeg
from minavio.errors import FFmpegError

RIFE_ENV = "MINAVIO_RIFE_BIN"
RIFE_NAME = "rife-ncnn-vulkan"
_STDERR_LIMIT = 2000


def rife_binary() -> str | None:
    """Chemin du binaire rife-ncnn-vulkan (`MINAVIO_RIFE_BIN` prioritaire, puis PATH)."""
    env = os.environ.get(RIFE_ENV)
    if env:
        return shutil.which(env) or (env if Path(env).is_file() else None)
    return shutil.which(RIFE_NAME)


def rife_available() -> bool:
    return rife_binary() is not None


def resolve_mode(mode: str) -> str:
    """`auto` -> rife si disponible sinon minterpolate. Retourne rife | minterpolate | none."""
    if mode == "auto":
        return "rife" if rife_available() else "minterpolate"
    if mode not in {"rife", "minterpolate", "none"}:
        raise ValueError(f"mode d'interpolation inconnu : {mode!r}")
    return mode


def interpolation_filter(speed: float, fps: float, mode: str) -> str:
    """Fragment de filtre vidéo FFmpeg qui ralentit à `speed` (<1) en sortie `fps`.

    - minterpolate : crée des images intermédiaires jusqu'à fps/speed, puis étire les timestamps
      (pas de duplication d'images).
    - none : simple `setpts` (images dupliquées par le `fps` suivant).
    - rife : '' (chaîne vide) car la vidéo intermédiaire produite par `rife_slowmo` est déjà
      ralentie ; l'appelant n'ajoute aucun setpts.
    `mode` accepte aussi `auto` (résolu ici).
    """
    mode = resolve_mode(mode)
    if mode == "rife":
        return ""
    stretch = f"setpts=PTS/{speed:.5f}"
    if mode == "none" or abs(speed - 1.0) < 1e-6:
        return stretch
    target = fps / speed
    return f"minterpolate=fps={target:.3f}:mi_mode=mci:mc_mode=aobmc:vsbmc=1,{stretch}"


def _run(cmd: list[str]) -> None:
    try:
        proc = subprocess.run(cmd, capture_output=True, check=False)
    except FileNotFoundError as e:
        raise FFmpegError(f"'{cmd[0]}' est introuvable : vérifiez {RIFE_ENV} ou le PATH.") from e
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        if len(err) > _STDERR_LIMIT:
            err = "[...]" + err[-_STDERR_LIMIT:]
        raise FFmpegError(f"Commande échouée (code {proc.returncode}) : {' '.join(cmd)}\n{err}")


def rife_slowmo(
    source: Path, start: float, end: float, speed: float, dest: Path, work_dir: Path | None = None
) -> Path:
    """Ralentit `source[start:end]` par interpolation RIFE ; écrit la vidéo (sans audio) `dest`.

    La vidéo produite dure (end-start)/speed, à la cadence de la source. Les images temporaires
    vont dans un sous-dossier de `work_dir` (par défaut le dossier de `dest`), supprimé ensuite.
    """
    binary = rife_binary()
    if binary is None:
        raise FFmpegError(
            f"{RIFE_NAME} introuvable : installez-le (MIT) et ajoutez-le au PATH ou "
            f"définissez {RIFE_ENV}."
        )
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dur = end - start
    tmp = Path(tempfile.mkdtemp(prefix="rife_", dir=work_dir or dest.parent))
    try:
        frames_in, frames_out = tmp / "in", tmp / "out"
        frames_in.mkdir()
        frames_out.mkdir()
        ffmpeg.run(
            [
                *("-ss", f"{start:.4f}", "-t", f"{dur:.4f}", "-i", str(source)),
                *("-an", "-fps_mode", "passthrough", str(frames_in / "%08d.png")),
            ]
        )
        n_in = len(list(frames_in.glob("*.png")))
        if n_in == 0:
            raise FFmpegError(f"Aucune image extraite de {source} [{start:.2f}-{end:.2f}s].")
        n_out = max(round(n_in / speed), n_in)
        _run(
            [
                binary,
                "-i",
                str(frames_in),
                "-o",
                str(frames_out),
                "-n",
                str(n_out),
                "-f",
                "%08d.png",
            ]
        )
        if not any(frames_out.glob("*.png")):
            raise FFmpegError(f"{RIFE_NAME} n'a produit aucune image ({frames_out}).")
        # n_out images sur dur/speed secondes = cadence source
        ffmpeg.run(
            [
                *("-framerate", f"{n_out / (dur / speed):.5f}", "-i", str(frames_out / "%08d.png")),
                *("-c:v", "libx264", "-preset", "veryfast", "-crf", "12", "-pix_fmt", "yuv420p"),
                str(dest),
            ]
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return dest
