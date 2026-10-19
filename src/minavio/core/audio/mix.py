"""Filtres FFmpeg audio : musique, ducking (sidechaincompress voix -> musique) et loudnorm."""

from __future__ import annotations

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


def audio_graph(
    voice: str,
    music: str | None,
    duration: float,
    lufs: float,
    ducking: DuckingSettings,
    out: str = "aout",
) -> list[str]:
    """Segments de filter_complex produisant `[out]` à partir des labels `voice` et `music`.

    Sans musique : voix seule + loudnorm. Avec musique : ducking si activé, sinon mix simple.
    """
    if music is None:
        return [f"[{voice}]{loudnorm_filter(lufs)}[{out}]"]
    parts = [f"[{music}]{music_chain(duration, ducking.music_gain_db)}[mus]"]
    if ducking.enabled:
        parts += [
            f"[{voice}]asplit=2[vo][sc]",
            f"[mus][sc]{sidechain_filter(ducking)}[duck]",
            "[vo][duck]amix=inputs=2:duration=first:normalize=0[mix]",
        ]
    else:
        parts.append(f"[{voice}][mus]amix=inputs=2:duration=first:normalize=0[mix]")
    parts.append(f"[mix]{loudnorm_filter(lufs)}[{out}]")
    return parts
