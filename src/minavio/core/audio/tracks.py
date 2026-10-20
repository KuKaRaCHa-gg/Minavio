"""Pistes audio retraitées (séparation Demucs et/ou débruitage) consommées par le rendu.

Chaîne par source : extraction -> [Demucs -> voix (+ accompagnement)] -> [débruitage de la voix
ou de tout l'audio] -> recomposition -> conformité (48 kHz stéréo, durée exacte de la source).

Compromis `remove_music` : Demucs ne sépare que vocals/drums/bass/other ; la musique du jeu se
répartit entre drums, bass et other. On garde donc `vocals` + `other` à -6 dB (bruitages et
ambiances) et on jette drums+bass (là où se trouve l'essentiel du rythme musical Content ID).
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import tempfile
from pathlib import Path

from minavio.config.schema import ChannelProfile
from minavio.core import ffmpeg
from minavio.core.audio import denoise as denoise_mod
from minavio.core.audio import separate as separate_mod

log = logging.getLogger(__name__)

SAMPLE_RATE = 48000


def settings_hash(source: Path, profile: ChannelProfile) -> str:
    """Hash court des réglages de nettoyage (+ identité de la source) pour le cache."""
    sep, den = profile.audio.separation, profile.audio.denoise
    stat = source.stat()
    payload = {
        "sep": sep.model_dump() if sep.enabled else None,
        "den": den.model_dump() if den.enabled else None,
        "src": [str(source.resolve()), stat.st_size, stat.st_mtime_ns],
        "v": 1,
    }
    return hashlib.sha1(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:10]


def _conform(src: Path, dst: Path, duration: float) -> None:
    """48 kHz stéréo, durée exacte (pad de silence ou coupe) à l'échantillon près."""
    n = round(duration * SAMPLE_RATE)
    ffmpeg.run(
        [
            *("-i", str(src)),
            *(
                "-af",
                f"aresample={SAMPLE_RATE},aformat=channel_layouts=stereo,"
                f"apad=whole_len={n},atrim=end_sample={n},asetpts=N/SR/TB",
            ),
            *("-ar", str(SAMPLE_RATE), "-ac", "2", "-c:a", "pcm_s16le"),
            str(dst),
        ]
    )


def _process_source(source: Path, profile: ChannelProfile, out: Path, tmp: Path) -> None:
    sep, den = profile.audio.separation, profile.audio.denoise
    raw = tmp / "raw.wav"
    separate_mod.extract_wav(source, raw)
    current = raw  # piste en cours : tout l'audio, ou la voix seule
    accompaniment: list[tuple[Path, float]] = []

    if sep.enabled:
        stems = separate_mod.separate(raw, tmp, sep.model)
        current = stems["vocals"]
        if sep.remove_music:
            accompaniment = [(stems["other"], separate_mod.OTHER_ATTENUATION_DB)]
        else:
            accompaniment = [(stems[s], 0.0) for s in ("drums", "bass", "other")]

    if den.enabled:
        cleaned = tmp / "voice_clean.wav"
        used = denoise_mod.denoise(current, cleaned, den.backend)
        log.info("Débruitage %s : %s", used, source.name)
        current = cleaned

    if accompaniment:
        mixed = tmp / "mixed.wav"
        separate_mod.mix_stems([(current, 0.0), *accompaniment], mixed)
        current = mixed

    _conform(current, out, ffmpeg.probe(source).duration)


def prepare_audio_tracks(
    sources: list[Path], profile: ChannelProfile, work_dir: Path
) -> dict[Path, Path]:
    """source -> piste audio retraitée ; les sources non traitées ou en échec sont absentes."""
    sep, den = profile.audio.separation, profile.audio.denoise
    result: dict[Path, Path] = {}
    if not (sep.enabled or den.enabled):
        return result
    audio_dir = Path(work_dir) / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    for source in sources:
        source = Path(source)
        try:
            if not ffmpeg.probe(source).has_audio:
                continue
            out = audio_dir / f"{source.stem}_{settings_hash(source, profile)}.wav"
            if out.exists() and out.stat().st_size > 0:
                result[source] = out
                continue
            with tempfile.TemporaryDirectory(prefix="minavio_audio_", dir=audio_dir) as t:
                tmp = Path(t)
                part = tmp / "final.wav"
                _process_source(source, profile, part, tmp)
                shutil.move(part, out)  # déplacement final : pas de cache partiel
            result[source] = out
        except Exception as e:
            log.warning("Nettoyage audio ignoré pour %s (audio d'origine conservé) : %s", source, e)
    return result
