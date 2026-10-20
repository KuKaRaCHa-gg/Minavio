"""Filtres FFmpeg audio : musique, ducking (sidechaincompress voix -> musique) et loudnorm."""

from __future__ import annotations

from collections import defaultdict

from minavio.config.schema import DuckingSettings

TRUE_PEAK_DB = -1.5


def loudnorm_filter(lufs: float) -> str:
    return f"loudnorm=I={lufs}:TP={TRUE_PEAK_DB}:LRA=11"


def sidechain_filter(ducking: DuckingSettings) -> str:
    return (
        "sidechaincompress=threshold=0.03:ratio=8:"
        f"attack={ducking.attack_ms}:release={ducking.release_ms}:makeup=1"
    )


def music_chain(duration: float, gain_db: float, fade_out_s: float = 1.5) -> str:
    """Musique (bouclée via -stream_loop) coupée à `duration`, atténuée, fondu de sortie."""
    fade = min(fade_out_s, duration)
    return (
        f"atrim=0:{duration:.4f},asetpts=PTS-STARTPTS,aresample=48000,"
        f"aformat=channel_layouts=stereo,volume={gain_db}dB,"
        f"afade=t=out:st={max(duration - fade, 0):.4f}:d={fade:.4f}"
    )


def sfx_graph(
    cues: list[tuple[int, float, float]], sample_rate: int = 48000
) -> tuple[list[str], list[str]]:
    """Segments pour les SFX : `cues` = [(index d'entrée ffmpeg, début en s, gain dB)].

    Une entrée utilisée par plusieurs cues est dupliquée par `asplit`. Retourne
    (segments, labels des flux prêts à être mixés). Les SFX ne passent pas par le ducking.
    """
    by_input: dict[int, list[int]] = defaultdict(list)
    for k, (inp, _, _) in enumerate(cues):
        by_input[inp].append(k)
    parts: list[str] = []
    labels: list[str] = []
    for inp, ks in by_input.items():
        if len(ks) == 1:
            srcs = {ks[0]: f"{inp}:a"}
        else:
            outs = "".join(f"[sp{inp}_{j}]" for j in range(len(ks)))
            parts.append(f"[{inp}:a]asplit={len(ks)}{outs}")
            srcs = {k: f"sp{inp}_{j}" for j, k in enumerate(ks)}
        for k in ks:
            _, at, gain = cues[k]
            ms = max(round(at * 1000), 0)
            parts.append(
                f"[{srcs[k]}]aresample={sample_rate},"
                f"aformat=sample_fmts=fltp:channel_layouts=stereo,"
                f"adelay={ms}|{ms},volume={gain}dB[sfx{k}]"
            )
    labels = [f"sfx{k}" for k in range(len(cues))]
    return parts, labels


def audio_graph(
    voice: str,
    music: str | None,
    duration: float,
    lufs: float,
    ducking: DuckingSettings,
    out: str = "aout",
    sfx: list[str] | None = None,
) -> list[str]:
    """Segments de filter_complex produisant `[out]` à partir des labels `voice` et `music`.

    Sans musique : voix seule + loudnorm. Avec musique : ducking si activé, sinon mix simple.
    `sfx` : labels de flux SFX (voir `sfx_graph`) ajoutés au mix avant loudnorm, sans ducking.
    """
    if music is None:
        if not sfx:
            return [f"[{voice}]{loudnorm_filter(lufs)}[{out}]"]
        labels = "".join(f"[{s}]" for s in sfx)
        return [
            f"[{voice}]{labels}amix=inputs={len(sfx) + 1}:duration=first:normalize=0[mix]",
            f"[mix]{loudnorm_filter(lufs)}[{out}]",
        ]
    parts = [f"[{music}]{music_chain(duration, ducking.music_gain_db)}[mus]"]
    if ducking.enabled:
        parts += [
            f"[{voice}]asplit=2[vo][sc]",
            f"[mus][sc]{sidechain_filter(ducking)}[duck]",
            "[vo][duck]amix=inputs=2:duration=first:normalize=0[mix]",
        ]
    else:
        parts.append(f"[{voice}][mus]amix=inputs=2:duration=first:normalize=0[mix]")
    if sfx:
        labels = "".join(f"[{s}]" for s in sfx)
        parts.append(f"[mix]{labels}amix=inputs={len(sfx) + 1}:duration=first:normalize=0[mixfx]")
        parts.append(f"[mixfx]{loudnorm_filter(lufs)}[{out}]")
    else:
        parts.append(f"[mix]{loudnorm_filter(lufs)}[{out}]")
    return parts
