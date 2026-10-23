"""Rendu d'une Timeline en une passe FFmpeg `filter_complex`."""

from __future__ import annotations

import logging
from pathlib import Path

from minavio.config.schema import ChannelProfile, OutputFormat
from minavio.core import effects, ffmpeg, interpolate, reframe, sfx
from minavio.core.audio import mix
from minavio.core.models import Clip, Timeline
from minavio.errors import FFmpegError, MinavioError

log = logging.getLogger("minavio.render")

SAMPLE_RATE = 48000
SLOWMO_GAIN_DB = -4.0  # atténuation de l'audio d'un ralenti (atempo seul sonne agressif)


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
    i: int,
    clip: Clip,
    info,
    fmt: OutputFormat,
    plan: reframe.ClipReframe,
    *,
    v_in: int | None = None,
    a_in: int | None = None,
    has_audio: bool | None = None,
    interp_mode: str = "none",
    slow_gain: bool = False,
) -> list[str]:
    """Segments de filter_complex d'un clip.

    `v_in` / `a_in` : index d'entrée vidéo / audio (défaut i) ; `interp_mode` : rife (vidéo déjà
    interpolée et ralentie), minterpolate ou none ; `slow_gain` : -4 dB sur l'audio ralenti.
    """
    v_in = i if v_in is None else v_in
    a_in = i if a_in is None else a_in
    has_audio = info.has_audio if has_audio is None else has_audio
    dur = clip.duration / clip.speed
    if clip.interpolate and interp_mode != "none":
        stretch = interpolate.interpolation_filter(clip.speed, fmt.fps, interp_mode)
    else:
        stretch = f"setpts=PTS/{clip.speed:.5f}"
    path = plan.path
    if path and clip.interpolate and interp_mode == "rife":
        # entrée déjà ralentie (durée / speed) : chemin en temps source étiré d'autant
        path = [(t / clip.speed, cx) for t, cx in path]
    seg = [
        f"[{v_in}:v]setpts=PTS-STARTPTS[t{i}]",
        *reframe.build_reframe(
            plan.mode,
            f"t{i}",
            f"r{i}",
            info.width,
            info.height,
            fmt.width,
            fmt.height,
            path,
            f"c{i}",
            zoom=plan.zoom,
            facecam=plan.facecam,
            split_ratio=plan.split_ratio,
        ),
        f"[r{i}]{stretch + ',' if stretch else ''}fps={fmt.fps},format=yuv420p[v{i}]",
    ]
    if has_audio:
        tempo = atempo_chain(clip.speed)
        chain = ",".join(
            p
            for p in (
                "asetpts=PTS-STARTPTS",
                tempo,
                f"volume={SLOWMO_GAIN_DB:g}dB" if slow_gain else "",
                f"aresample={SAMPLE_RATE}",
                "aformat=sample_fmts=fltp:channel_layouts=stereo",
            )
            if p
        )
        seg.append(f"[{a_in}:a]{chain}[a{i}]")
    else:
        seg.append(
            f"anullsrc=r={SAMPLE_RATE}:cl=stereo,atrim=duration={dur:.4f},asetpts=PTS-STARTPTS[a{i}]"
        )
    return seg


def _track_has_audio(track: Path) -> bool:
    """Piste de remplacement (souvent audio seul, que `ffmpeg.probe` refuse faute de vidéo)."""
    if not Path(track).exists():
        raise MinavioError(f"Piste audio de remplacement introuvable : {track}")
    try:
        return ffmpeg.probe(track).has_audio
    except FFmpegError:
        return True


def _audio_track(timeline: Timeline, source: Path) -> Path | None:
    """Piste audio retraitée de `source` (clé exacte, sinon chemin résolu)."""
    tracks = timeline.audio_tracks
    if not tracks:
        return None
    if source in tracks:
        return tracks[source]
    resolved = source.resolve()
    return next((v for k, v in tracks.items() if Path(k).resolve() == resolved), None)


def _media_overlay(
    args: list[str],
    graph: list[str],
    sfx_cues: list[tuple[int, float, float]],
    cur: str,
    idx: int,
    k: int,
    ov,
    fmt: OutputFormat,
    total: float,
) -> tuple[str, int]:
    """Overlay média (GIF / image / vidéo) : props {path, kind, x, y, w, h} normalisés 0-1
    (x, y = coin haut-gauche). L'audio d'une vidéo est mixé sur son intervalle."""
    props = ov.props
    path = Path(str(props.get("path", "")))
    if not path.is_file():
        raise MinavioError(f"Média d'overlay introuvable : {path}")
    kind = props.get("kind", "gif")
    dur = min(ov.duration, total - ov.start)
    flags = {"gif": ["-ignore_loop", "0"], "image": ["-loop", "1"], "video": ["-stream_loop", "-1"]}
    if kind not in flags:
        raise MinavioError(f"Overlay média : kind invalide {kind!r} (gif, image, video)")
    args += [*flags[kind], "-t", f"{dur:.4f}", "-i", str(path)]
    w = max(2, round(fmt.width * float(props.get("w", 0.3)) / 2) * 2)
    h = max(2, round(fmt.height * float(props.get("h", 0.3)) / 2) * 2)
    x = round(fmt.width * float(props.get("x", 0.35)))
    y = round(fmt.height * float(props.get("y", 0.35)))
    graph.append(f"[{idx}:v]format=rgba,scale={w}:{h},setpts=PTS-STARTPTS+{ov.start:.4f}/TB[mv{k}]")
    graph.append(
        f"[{cur}][mv{k}]overlay={x}:{y}:enable='{effects.between(ov.start, ov.start + dur)}':"
        f"eof_action=pass:format=auto[mo{k}]"
    )
    if kind == "video":
        try:
            has_audio = ffmpeg.probe(path).has_audio
        except FFmpegError:
            has_audio = False
        if has_audio:
            sfx_cues.append((idx, ov.start, 0.0))
    return f"mo{k}", idx + 1


def build_command(
    timeline: Timeline,
    fmt: OutputFormat,
    out: Path,
    profile: ChannelProfile,
    overlay_files: dict[int, Path] | None = None,
    temps: list[Path] | None = None,
    *,
    video_codec: str | None = None,
    slowmo_mode: str | None = None,
    threads: int | None = None,
) -> list[str]:
    """Construit les arguments ffmpeg (sans le binaire) ; sonde les sources.

    `temps` : reçoit les fichiers intermédiaires créés (vidéos RIFE) à supprimer après le rendu.
    `video_codec` : encodeur vidéo (défaut libx264) ; `slowmo_mode` : "minterpolate" ou "none"
    remplace le mode de ralenti résolu (rife/auto) ; `threads` : threads de l'encodeur.
    """
    clips = fit_clips(timeline.clips, fmt.max_duration_s)
    if not clips:
        raise MinavioError("Timeline vide : aucun clip à rendre (aucun highlight retenu ?)")
    total = sum(c.duration / c.speed for c in clips)

    infos = {s: ffmpeg.probe(s) for s in {c.source for c in clips}}
    slowmo = profile.editing.slowmo
    if slowmo_mode is not None:
        if slowmo_mode not in {"minterpolate", "none"}:
            raise MinavioError(f"slowmo_mode invalide : {slowmo_mode!r} (minterpolate ou none)")
        interp_mode = slowmo_mode
    else:
        interp_mode = (
            interpolate.resolve_mode(slowmo.interpolation)
            if any(c.interpolate for c in clips)
            else "none"
        )
    args: list[str] = []
    n_inputs = 0
    v_ins: list[int] = []
    a_ins: list[int] = []
    a_has: list[bool] = []
    modes: list[str] = []
    for i, c in enumerate(clips):
        # seek en entrée : chaque entrée ne décode que son fragment (pas depuis t=0)
        seek = ["-ss", f"{c.start:.4f}", "-t", f"{c.duration:.4f}"]
        args += [*seek, "-i", str(c.source)]
        main = n_inputs
        n_inputs += 1
        mode = interp_mode if c.interpolate else "none"
        if mode == "rife":
            tmp = out.parent / f".rife_{out.stem}_{i}.mp4"
            if temps is not None:
                temps.append(tmp)
            try:
                interpolate.rife_slowmo(c.source, c.start, c.end, c.speed, tmp, out.parent)
            except MinavioError as e:
                if slowmo.interpolation != "auto":
                    raise
                log.warning("RIFE a échoué (%s) : repli sur minterpolate.", str(e).splitlines()[0])
                mode = "minterpolate"
            else:
                args += ["-i", str(tmp)]
                v_ins.append(n_inputs)
                n_inputs += 1
        if mode != "rife":
            v_ins.append(main)
        modes.append(mode)
        track = _audio_track(timeline, c.source)
        if track is not None:
            args += [*seek, "-i", str(track)]
            a_ins.append(n_inputs)
            a_has.append(_track_has_audio(track))
            n_inputs += 1
        else:
            a_ins.append(main)
            a_has.append(infos[c.source].has_audio)
    n = len(clips)

    graph: list[str] = []
    for i, c in enumerate(clips):
        plan = reframe.plan_clip(c.source, c.start, c.end, fmt.reframe, c.zoom, profile.framing)
        graph += _clip_segments(
            i,
            c,
            infos[c.source],
            fmt,
            plan,
            v_in=v_ins[i],
            a_in=a_ins[i],
            has_audio=a_has[i],
            interp_mode=modes[i],
            slow_gain=c.speed < 1 and (c.interpolate or slowmo.enabled),
        )
    graph.append("".join(f"[v{i}][a{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=1[vcat][voice]")

    # overlays (index du dict = index dans timeline.overlays)
    cur = "vcat"
    idx = n_inputs
    music_label = None
    if timeline.music is not None:
        args += ["-stream_loop", "-1", "-i", str(timeline.music)]
        music_label = f"{idx}:a"
        idx += 1
    sfx_cues: list[tuple[int, float, float]] = []
    sfx_cfg = profile.audio.sfx
    if (sfx_cfg.enabled or profile.memes.enabled) and timeline.sfx:
        input_of: dict[Path, int] = {}
        for cue in sorted(timeline.sfx, key=lambda c: c.at):
            if cue.at >= total:
                continue
            path = sfx.cue_path(cue, sfx_cfg)
            if path is None:
                continue
            if path not in input_of:
                args += ["-i", str(path)]
                input_of[path] = idx
                idx += 1
            sfx_cues.append((input_of[path], cue.at, cue.gain_db))
    vfx = [e for e in timeline.effects if e.kind in effects.VIDEO_KINDS and e.at < total]
    if vfx:
        graph += effects.video_graph(vfx, cur, "vfx", fmt.width, fmt.height, fmt.fps)
        cur = "vfx"
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
    for k, ov in enumerate(timeline.overlays):
        if ov.template != "media" or ov.start >= total:
            continue
        cur, idx = _media_overlay(args, graph, sfx_cues, cur, idx, k, ov, fmt, total)
    for k, e in enumerate(effects.popup_effects(timeline.effects)):
        if e.at >= total:
            continue
        png = out.parent / f".popup_{out.stem}_{k}.png"
        if temps is not None:
            temps.append(png)
        effects.make_error_popup(png, max(int(fmt.width * 0.5), 64))
        args += ["-loop", "1", "-t", f"{e.duration:.4f}", "-i", str(png)]
        graph.append(f"[{idx}:v]format=rgba,setpts=PTS-STARTPTS+{e.at:.4f}/TB[pp{k}]")
        graph.append(
            f"[{cur}][pp{k}]overlay=x=(W-w)/2:y=(H-h)/2:"
            f"enable='{effects.between(e.at, e.at + e.duration)}':eof_action=pass[pop{k}]"
        )
        cur = f"pop{k}"
        idx += 1
    graph.append(f"[{cur}]format=yuv420p[vout]")

    sfx_parts, sfx_labels = mix.sfx_graph(sfx_cues, SAMPLE_RATE) if sfx_cues else ([], [])
    graph += sfx_parts
    post = [
        f
        for f in (
            effects.audio_filters([e for e in timeline.effects if e.at < total]),
            effects.limiter_filter(profile.memes.max_true_peak_db) if profile.memes.enabled else "",
        )
        if f
    ]
    graph += mix.audio_graph(
        "voice",
        music_label,
        total,
        profile.audio.loudness_lufs,
        profile.audio.ducking,
        "apre" if post else "aout",
        sfx=sfx_labels or None,
    )
    if post:
        graph.append(f"[apre]{','.join(post)}[aout]")

    args += ["-filter_complex", ";".join(graph), "-map", "[vout]", "-map", "[aout]"]
    codec = video_codec or "libx264"
    args += ["-c:v", codec]
    if codec == "libx264":
        args += ["-preset", "medium", "-crf", "18"]
    args += ["-pix_fmt", "yuv420p"]
    args += ["-r", str(fmt.fps), "-c:a", "aac", "-b:a", "192k", "-ar", str(SAMPLE_RATE)]
    if threads:
        args += ["-threads", str(threads)]
    args += ["-t", f"{total:.4f}", "-movflags", "+faststart", str(out)]
    return args


def render_timeline(
    timeline: Timeline,
    fmt: OutputFormat,
    out: Path,
    profile: ChannelProfile,
    overlay_files: dict[int, Path] | None = None,
    *,
    video_codec: str | None = None,
    slowmo_mode: str | None = None,
    threads: int | None = None,
) -> Path:
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    temps: list[Path] = []
    try:
        args = build_command(
            timeline,
            fmt,
            out,
            profile,
            overlay_files,
            temps,
            video_codec=video_codec,
            slowmo_mode=slowmo_mode,
            threads=threads,
        )
        ffmpeg.run(args)
    finally:
        for t in temps:
            t.unlink(missing_ok=True)
    return out
