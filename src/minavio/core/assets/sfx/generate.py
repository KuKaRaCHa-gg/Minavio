"""Génère la banque SFX embarquée (procédural, ffmpeg lavfi, style pixel/8-bit).

Usage : python generate.py [dossier_de_sortie]   (défaut : le dossier de ce script)
Les sons produits sont CC0 de fait (voir LICENSE.txt). Aucun appel réseau.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SR = 48000
PI2 = "2*PI"


def _expr(expr: str, dur: float) -> list[str]:
    return ["-f", "lavfi", "-i", f"aevalsrc=exprs='{expr}':s={SR}:d={dur}"]


def _noise(dur: float) -> list[str]:
    return ["-f", "lavfi", "-i", f"anoisesrc=color=pink:r={SR}:d={dur}:seed=7:amplitude=1"]


# nom -> (entrées ffmpeg, filtres audio / filter_complex)
def _specs() -> dict[str, tuple[list[str], list[str]]]:
    return {
        # deux bandes de bruit rose en fondu croisé : balayage grave -> aigu
        "whoosh": (
            _noise(0.55) + _noise(0.55),
            [
                "-filter_complex",
                "[0:a]bandpass=f=500:width_type=h:w=600,afade=t=out:st=0.05:d=0.5[a];"
                "[1:a]bandpass=f=3500:width_type=h:w=3000,afade=t=in:st=0:d=0.3,afade=t=out:st=0.3:d=0.25[b];"
                "[a][b]amix=inputs=2:normalize=0,volume=6dB,alimiter=limit=0.8,volume=-2dB",
            ],
        ),
        # clic + sinus grave court
        "hit": (
            _expr(
                f"0.9*sin({PI2}*t*(55+160*exp(-t*45)))*exp(-t*16)+0.6*(2*random(0)-1)*exp(-t*220)",
                0.3,
            ),
            ["-af", "alimiter=limit=0.8,volume=-3dB"],
        ),
        # sinus 50 Hz décroissant + bruit
        "boom": (
            _expr(
                f"0.95*sin({PI2}*t*(45+25*exp(-t*6)))*exp(-t*3.2)+0.5*(2*random(0)-1)*exp(-t*9)",
                1.2,
            ),
            ["-af", "lowpass=f=900,volume=1.4,alimiter=limit=0.8,volume=-3dB"],
        ),
        # sweep carré descendant bitcrushé
        "rewind": (
            _expr(f"0.5*sgn(sin({PI2}*(2000*t-1500*t*t)))*(1-t/0.7)", 0.7),
            ["-af", "acrusher=bits=4:mix=1:samples=6,lowpass=f=6000,afade=t=in:d=0.02"],
        ),
        # bulle courte
        "pop": (
            _expr(f"0.9*sin({PI2}*t*(250+900*exp(-t*70)))*exp(-t*38)", 0.15),
            ["-af", "alimiter=limit=0.8"],
        ),
        # pièce 8-bit : deux notes carrées (si5 -> mi6)
        "coin": (
            _expr(
                f"0.35*sgn(sin({PI2}*t*if(lt(t,0.08),988,1319)))*if(lt(t,0.08),1,exp(-(t-0.08)*9))",
                0.4,
            ),
            ["-af", "lowpass=f=8000,afade=t=out:st=0.32:d=0.08"],
        ),
    }


def generate(out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    made = []
    for name, (inputs, flt) in _specs().items():
        out = out_dir / f"{name}.ogg"
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *inputs, *flt]
        cmd += ["-ac", "1", "-ar", str(SR), "-c:a", "libvorbis", "-q:a", "3", str(out)]
        subprocess.run(cmd, check=True)
        made.append(out)
    return made


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent
    for p in generate(target):
        print(p)
