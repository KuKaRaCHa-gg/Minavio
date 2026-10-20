"""Rendu d'une Timeline en une passe FFmpeg `filter_complex`."""

from __future__ import annotations

from pathlib import Path

from minavio.config.schema import ChannelProfile, OutputFormat
from minavio.core import ffmpeg, reframe, sfx
from minavio.core.audio import mix
from minavio.core.models import Clip, Timeline
from minavio.errors import MinavioError

SAMPLE_RATE = 48000


def atempo_chain(speed: float) -> str:
    """atempo n'accepte que [0.5, 2] : on enchaîne si besoin. '' si speed == 1."""
    if abs(speed - 1.0) < 1e-6:
        return ""
    factors: list[float] = []
    s = speed
    while s > 2.0:
        factors.append(2.0)
        s /= 2.0
    while s < 0.5:
        factors.append(0.5)
        s /= 0.5
    factors.append(s)
    return ",".join(f"atempo={f:.5f}" for f in factors)


def fit_clips(clips: list[Clip], max_duration: float | None) -> list[Clip]:
    """Tronque la liste de clips pour que la durée rendue <= max_duration."""
    if max_duration is None:
        return list(clips)
    out: list[Clip] = []
    total = 0.0
    for c in clips:
        d = c.duration / c.speed
        if total + d <= max_duration + 1e-6:
            out.append(c)
            total += d
            continue
        remaining = max_duration - total
        if remaining > 0.05:
            out.append(c.model_copy(update={"end": c.start + remaining * c.speed}))
        break
    return out


def _clip_segments(
    i: int, clip: Clip, info, fmt: OutputFormat, plan: reframe.ClipReframe
) -> list[str]:
    dur = clip.duration / clip.speed
    seg = [
        f"[{i}:v]setpts=PTS-STARTPTS[t{i}]",
        *reframe.build_reframe(
            plan.mode,
            f"t{i}",
            f"r{i}",
            info.width,
            info.height,
            fmt.width,
            fmt.height,
            plan.path,
            f"c{i}",
            zoom=plan.zoom,
            facecam=plan.facecam,
            split_ratio=plan.split_ratio,
        ),
        f"[r{i}]setpts=PTS/{clip.speed:.5f},fps={fmt.fps},format=yuv420p[v{i}]",
    ]
    if info.has_audio:
        tempo = atempo_chain(clip.speed)
        chain = ",".join(
            p
            for p in (
                "asetpts=PTS-STARTPTS",
                tempo,
                f"aresample={SAMPLE_RATE}",
                "aformat=sample_fmts=fltp:channel_layouts=stereo",
            )
            if p
        )
        seg.append(f"[{i}:a]{chain}[a{i}]")
    else:
        seg.append(
            f"anullsrc=r={SAMPLE_RATE}:cl=stereo,atrim=duration={dur:.4f},asetpts=PTS-STARTPTS[a{i}]"
        )
    return seg


def build_command(
    timeline: Timeline,
    fmt: OutputFormat,
    out: Path,
    profile: ChannelProfile,
    overlay_files: dict[int, Path] | None = None,
) -> list[str]:
    """Construit les arguments ffmpeg (sans le binaire) ; sonde les sources."""
    clips = fit_clips(timeline.clips, fmt.max_duration_s)
    if not clips:
        raise MinavioError("Timeline vide : aucun clip à rendre (aucun highlight retenu ?)")
    total = sum(c.duration / c.speed for c in clips)

    infos = {s: ffmpeg.probe(s) for s in {c.source for c in clips}}
    args: list[str] = []
    for c in clips:
        # seek en entrée : chaque entrée ne décode que son fragment (pas depuis t=0)
        args += ["-ss", f"{c.start:.4f}", "-t", f"{c.duration:.4f}", "-i", str(c.source)]
    n = len(clips)

    graph: list[str] = []
    for i, c in enumerate(clips):
        plan = reframe.plan_clip(c.source, c.start, c.end, fmt.reframe, c.zoom, profile.framing)
        graph += _clip_segments(i, c, infos[c.source], fmt, plan)
    graph.append("".join(f"[v{i}][a{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=1[vcat][voice]")

    # overlays (index du dict = index dans timeline.overlays)
    cur = "vcat"
    idx = n
    music_label = None
    if timeline.music is not None:
        args += ["-stream_loop", "-1", "-i", str(timeline.music)]
        music_label = f"{idx}:a"
        idx += 1
    sfx_cues: list[tuple[int, float, float]] = []
    sfx_cfg = profile.audio.sfx
    if sfx_cfg.enabled and timeline.sfx:
        input_of: dict[Path, int] = {}
        for cue in sorted(timeline.sfx, key=lambda c: c.at):
            if cue.at >= total:
                continue
            path = sfx.resolve_sound(cue.sound, sfx_cfg)
            if path is None:
                continue
            if path not in input_of:
                args += ["-i", str(path)]
                input_of[path] = idx
                idx += 1
            sfx_cues.append((input_of[path], cue.at, cue.gain_db))
    sfx_parts, sfx_labels = mix.sfx_graph(sfx_cues, SAMPLE_RATE) if sfx_cues else ([], [])
    for k, file in sorted((overlay_files or {}).items()):
        if not 0 <= k < len(timeline.overlays):
            raise MinavioError(f"overlay_files[{k}] : index hors de timeline.overlays")
        ov = timeline.overlays[k]
        if ov.start >= total:
            continue
        args += ["-i", str(file)]
        end = min(ov.start + ov.duration, total)
        graph.append(
            f"[{idx}:v]format=rgba,scale={fmt.width}:{fmt.height},"
            f"setpts=PTS-STARTPTS+{ov.start:.4f}/TB[ov{k}]"
        )
        graph.append(
            f"[{cur}][ov{k}]overlay=enable='between(t,{ov.start:.4f},{end:.4f})':"
            f"eof_action=pass:format=auto[vo{k}]"
        )
        cur = f"vo{k}"
        idx += 1
    graph.append(f"[{cur}]format=yuv420p[vout]")

    graph += sfx_parts
    graph += mix.audio_graph(
        "voice",
        music_label,
        total,
        profile.audio.loudness_lufs,
        profile.audio.ducking,
        "aout",
        sfx=sfx_labels or None,
    )

    args += ["-filter_complex", ";".join(graph), "-map", "[vout]", "-map", "[aout]"]
    args += ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p"]
    args += ["-r", str(fmt.fps), "-c:a", "aac", "-b:a", "192k", "-ar", str(SAMPLE_RATE)]
    args += ["-t", f"{total:.4f}", "-movflags", "+faststart", str(out)]
    return args


def render_timeline(
    timeline: Timeline,
    fmt: OutputFormat,
    out: Path,
    profile: ChannelProfile,
    overlay_files: dict[int, Path] | None = None,
) -> Path:
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg.run(build_command(timeline, fmt, out, profile, overlay_files))
    return out
