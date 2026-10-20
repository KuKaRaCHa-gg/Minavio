"""Orchestration du pipeline `minavio run`.

Étapes : collect → detect → transcribe → rank → copy → music → beats → autocut → timeline →
overlays → sfx → chapters → render → thumbnails → shorts.
`autocut` n'existe que si `profile.editing.autocut.enabled` : elle tourne sur chaque source
retenue (après `transcribe`, avant `timeline`) et ses plages conservées (`keep_spans`) sont passées
à `plan_timeline`. En cas d'échec, un warning est émis et le montage se fait sans coupe de silences.

`sfx` n'existe que si `profile.audio.sfx.enabled`. `chapters` n'existe que si `profile.chapters`
et que le LLM est actif (il appelle le LLM local). `shorts` produit, après les rendus principaux,
une timeline dédiée par meilleur highlight (`profile.shorts.per_highlight`, ou `--shorts`) ; un
short qui échoue donne un warning sans interrompre le job.

`thumbnails` n'existe que si `profile.thumbnails.enabled` (et pas `--no-thumbnails`) : candidats
(`work/<job>/thumbs/`) → choix (LLM visuel si activé, sinon heuristique) → un PNG par choix et par
format dans `out/<profil>/<job>_thumb<i>_<format>.png`. Un échec donne un warning. La commande
`minavio thumbnails` rejoue cette étape depuis un job existant (`build_thumbnails`).

Chaque étape écrit son JSON dans `work/<job_id>/<étape>.json` ; avec `resume=True` une étape dont
le JSON existe est relue au lieu d'être recalculée (`collect` est toujours rejouée : elle sert à
calculer le `job_id`). Les modules des autres étapes sont importés paresseusement via `_api`.

Transcript : `transcribe.json` stocke `{chemin_source: [segments]}` et chaque segment porte son
`source`, ce qui permet à `plan_timeline` de rattacher les captions au bon clip en multi-sources.

Popups : `generate_copy` exprime `at` dans la concaténation des highlights qu'il reçoit ; le
montage réordonne et recoupe ces highlights, donc `remap_popups` convertit `at` en temps de la
timeline (popup supprimé si son moment n'a pas été retenu).
"""

from __future__ import annotations

import hashlib
import importlib
import json
import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from minavio.config.schema import ChannelProfile, OutputFormat
from minavio.core.models import (
    REWIND_DURATION_S,
    Chapter,
    Highlight,
    Moment,
    Overlay,
    SfxCue,
    ThumbnailCandidate,
    ThumbnailChoice,
    Timeline,
    TranscriptSegment,
)
from minavio.errors import MinavioError

log = logging.getLogger("minavio.pipeline")

TITLE_DURATION_S = 2.5
POPUP_DURATION_S = 2.0
POPUP_SNAP_S = 2.0
KILL_COUNTER_DELAY_S = 0.3  # le compteur apparaît 0,3 s après le début du moment
KILL_COUNTER_DURATION_S = 2.0
RANK_CARD_DURATION_S = 1.5
MAX_SHORTS = 10
AUDIO_EXTS = {".mp3", ".wav", ".flac", ".m4a", ".ogg", ".aac"}

_API = {
    "collect_sources": "minavio.core.ffmpeg:collect_sources",
    "run_detectors": "minavio.core.detect:run_detectors",
    "transcribe": "minavio.core.asr:transcribe",
    "analyze_keep_spans": "minavio.core.autocut:analyze_keep_spans",
    "detect_beats": "minavio.core.audio.beats:detect_beats",
    "rank_highlights": "minavio.llm.rank:rank_highlights",
    "generate_copy": "minavio.llm.copywriting:generate_copy",
    "generate_chapters": "minavio.llm.chapters:generate_chapters",
    "format_chapters": "minavio.llm.chapters:format_chapters",
    "plan_timeline": "minavio.core.edit:plan_timeline",
    "plan_sfx": "minavio.core.sfx:plan_sfx",
    "build_caption_overlays": "minavio.motion.captions:build_caption_overlays",
    "render_overlays": "minavio.motion.render:render_overlays",
    "render_timeline": "minavio.core.render:render_timeline",
    "extract_candidates": "minavio.core.thumbs:extract_candidates",
    "pick_thumbnails": "minavio.llm.vision:pick_thumbnails",
    "render_thumbnail": "minavio.motion.thumbnail:render_thumbnail",
}


def _api(name: str) -> Callable[..., Any]:
    """Résout une fonction publique d'un autre module (point de monkeypatch des tests)."""
    module, func = _API[name].split(":")
    try:
        return getattr(importlib.import_module(module), func)
    except (ImportError, AttributeError) as e:
        raise MinavioError(
            f"Module '{module}' indisponible ({e}). Installez les extras : uv sync --all-extras"
        ) from e


@dataclass
class JobResult:
    job_id: str
    work_dir: Path
    timeline_path: Path | None = None
    outputs: list[Path] = field(default_factory=list)
    # Fichiers annexes (chapitres YouTube, miniatures PNG) : affichés après les sorties vidéo.
    extras: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    dry_run: bool = False


def select_formats(profile: ChannelProfile, wanted: list[str] | None) -> list[OutputFormat]:
    """Sélectionne des formats par nom (`shorts`), par aspect (`9:16`) ou `all`."""
    if not wanted or "all" in wanted:
        return list(profile.outputs)
    chosen: list[OutputFormat] = []
    for w in wanted:
        matches = [o for o in profile.outputs if w in (o.name, o.aspect)]
        if not matches:
            known = ", ".join(f"{o.name} ({o.aspect})" for o in profile.outputs)
            raise MinavioError(f"Format '{w}' inconnu pour {profile.id}. Disponibles : {known}")
        chosen += [m for m in matches if m not in chosen]
    return chosen


def compute_job_id(profile: ChannelProfile, sources: list[Path]) -> str:
    h = hashlib.sha1(profile.model_dump_json().encode())
    for s in sorted(sources):
        st = s.stat()
        h.update(f"|{s.resolve()}|{st.st_size}|{st.st_mtime_ns}".encode())
    return h.hexdigest()[:10]


def pick_music(profile: ChannelProfile, job_id: str, explicit: Path | None) -> Path | None:
    if explicit is not None:
        if not explicit.is_file():
            raise MinavioError(f"Musique introuvable : {explicit}")
        return explicit
    music_dir = profile.audio.music_dir
    if not music_dir or not Path(music_dir).is_dir():
        return None
    files = sorted(p for p in Path(music_dir).iterdir() if p.suffix.lower() in AUDIO_EXTS)
    return random.Random(job_id).choice(files) if files else None


class _Steps:
    """Exécute les étapes, persiste leur JSON et gère resume / optionnel / durées."""

    def __init__(self, work_dir: Path, resume: bool, warnings: list[str]):
        self.dir, self.resume, self.warnings = work_dir, resume, warnings
        work_dir.mkdir(parents=True, exist_ok=True)

    def run(
        self,
        name: str,
        compute: Callable[[], Any],
        *,
        dump: Callable[[Any], Any] = lambda x: x,
        load: Callable[[Any], Any] = lambda x: x,
        optional: bool = False,
        default: Any = None,
    ) -> Any:
        path = self.dir / f"{name}.json"
        if self.resume and path.exists():
            try:
                value = load(json.loads(path.read_text(encoding="utf-8")))
                log.info("[%s] reprise depuis %s", name, path.name)
                return value
            except Exception as e:  # JSON corrompu : on recalcule
                log.warning("[%s] reprise impossible (%s), recalcul", name, e)
        t0 = time.perf_counter()
        try:
            value = compute()
        except Exception as e:
            if optional:
                msg = f"étape '{name}' ignorée : {e}"
                log.warning(msg)
                self.warnings.append(msg)
                return default
            if isinstance(e, MinavioError):
                raise
            raise MinavioError(f"Étape '{name}' échouée : {e}") from e
        path.write_text(json.dumps(dump(value), indent=2, ensure_ascii=False), encoding="utf-8")
        log.info("[%s] ok en %.2fs", name, time.perf_counter() - t0)
        return value


def _dump_models(items: list[Any]) -> list[Any]:
    return [i.model_dump(mode="json") for i in items]


def _copy_to_dict(copy: Any) -> dict[str, Any]:
    if hasattr(copy, "model_dump"):
        return copy.model_dump(mode="json")
    return dict(copy)


def remap_popups(
    popups: list[dict[str, Any]], highlights: list[Highlight], timeline: Timeline
) -> list[tuple[float, str]]:
    """`at` (concaténation des highlights donnés au copy) -> temps de la timeline."""
    timed: list[tuple[float, str]] = []
    for p in popups:
        at, offset, source_t, source = float(p["at"]), 0.0, None, None
        for h in highlights:
            if at < offset + h.duration:
                source_t, source = h.start + max(0.0, at - offset), h.source
                break
            offset += h.duration
        if source_t is None:
            continue
        # Clip contenant l'instant ; sinon (instant retiré par l'autocut) le passage gardé le plus
        # proche de la même source, à moins de POPUP_SNAP_S.
        best: tuple[float, float] | None = None  # (distance, temps timeline)
        tl = 0.0
        for i, c in enumerate(timeline.clips):
            # Le cold open rejoue un extrait d'un moment : les popups vont dans le vrai moment.
            if c.source == source and i >= timeline.cold_open_clips:
                t_src = min(max(source_t, c.start), c.end - 1e-3)
                dist = abs(t_src - source_t)
                if dist <= POPUP_SNAP_S and (best is None or dist < best[0]):
                    best = (dist, tl + (t_src - c.start) / c.speed)
            tl += c.duration / c.speed
        if best is not None:
            timed.append((best[1], str(p["text"])))
    out: list[tuple[float, str]] = []
    for t, text in sorted(timed):
        if not out or t >= out[-1][0] + POPUP_DURATION_S:  # pas de chevauchement
            out.append((t, text))
    return out


def _timed(
    timeline: Timeline, template: str, start: float, duration: float, props: dict[str, Any]
) -> Overlay | None:
    """Overlay borné à la durée de la timeline ; None si rien ne reste à afficher."""
    dur = min(duration, timeline.duration - start)
    if start < 0 or dur <= 0:
        return None
    return Overlay(template=template, start=start, duration=dur, props=props)


def _kill_count(
    moment: Moment, timeline: Timeline, highlights: list[Highlight] | None
) -> int | None:
    """kill -> 1 ; multi_kill -> `signals.kills` du highlight source s'il existe, sinon 2."""
    if moment.label == "kill":
        return 1
    if moment.label != "multi_kill":
        return None
    clip = timeline.clips[moment.first_clip]
    for h in highlights or []:
        if (
            h.label == "multi_kill"
            and h.source == clip.source
            and h.start <= clip.end
            and clip.start <= h.end
            and "kills" in h.signals
        ):
            return max(2, round(h.signals["kills"]))
    return 2


def build_overlays(
    timeline: Timeline,
    profile: ChannelProfile,
    copy: dict[str, Any] | None,
    highlights: list[Highlight] | None = None,
) -> list[Overlay]:
    """Sous-titres (si activés), titre (hook, 2.5 s), popups du copy et motion par moment.

    Le titre se place au début du premier vrai moment quand il y a un cold open, sinon à t=0.
    Motion (seulement si le template est dans `motion.templates`) :
    - `kill_counter` : calé sur l'instant du kill (`Moment.peak`), sinon 0,3 s après le début ;
    - `rank_card` : début de chaque vrai moment si `editing.order == "countdown"` (N…1) ;
    - `rewind` : sur la dernière seconde du cold open.
    """
    overlays: list[Overlay] = []
    cap = profile.motion.captions
    if cap.enabled and timeline.captions:
        overlays += _api("build_caption_overlays")(timeline.captions, cap)
    moments = timeline.moments()
    cold = [m for m in moments if m.cold_open]
    real = [m for m in moments if not m.cold_open]
    if copy:
        if copy.get("hook"):
            title_start = real[0].start if cold and real else 0.0
            overlays.append(
                Overlay(
                    template="title",
                    start=title_start,
                    duration=TITLE_DURATION_S,
                    # Titre en haut si les sous-titres occupent déjà le centre.
                    props={
                        "text": copy["hook"],
                        "position": "top" if cap.enabled and cap.position == "center" else "center",
                    },
                )
            )
        popups = copy.get("popups") or []
        timed = (
            remap_popups(popups, highlights, timeline)
            if highlights is not None
            else [(max(0.0, float(p["at"])), str(p["text"])) for p in popups]
        )
        overlays += [
            Overlay(template="popup", start=t, duration=POPUP_DURATION_S, props={"text": text})
            for t, text in timed
            if t < timeline.duration
        ]
    templates = set(profile.motion.templates)
    motion: list[Overlay | None] = []
    if "kill_counter" in templates:
        for m in real:
            kills = _kill_count(m, timeline, highlights)
            if kills is not None:
                motion.append(
                    _timed(
                        timeline,
                        "kill_counter",
                        # Un crâne s'allume toutes les 0,25 s : le dernier (flash) tombe sur
                        # l'instant du kill quand il est connu.
                        max(m.start, m.peak - 0.25 * kills)
                        if m.peak is not None
                        else m.start + KILL_COUNTER_DELAY_S,
                        KILL_COUNTER_DURATION_S,
                        {"kills": kills},
                    )
                )
    if "rank_card" in templates and profile.editing.order == "countdown":
        total = len(real)
        for idx, m in enumerate(real):
            motion.append(
                _timed(
                    timeline,
                    "rank_card",
                    m.start,
                    RANK_CARD_DURATION_S,
                    {"rank": total - idx, "total": total},
                )
            )
    if "rewind" in templates and cold:
        end = max(m.end for m in cold)
        dur = min(REWIND_DURATION_S, end)
        motion.append(_timed(timeline, "rewind", end - dur, dur, {}))
    overlays += [o for o in motion if o is not None]
    return overlays


def _decorate(
    steps: _Steps,
    prefix: str,
    timeline: Timeline,
    profile: ChannelProfile,
    copy: dict[str, Any] | None,
    highlights: list[Highlight],
) -> Timeline:
    """Ajoute overlays (étape `<prefix>overlays`) puis SFX (`<prefix>sfx`, si activés)."""
    overlays: list[Overlay] = steps.run(
        f"{prefix}overlays",
        lambda: build_overlays(timeline, profile, copy, highlights),
        dump=_dump_models,
        load=lambda d: [Overlay.model_validate(x) for x in d],
        optional=True,
        default=[],
    )
    timeline = timeline.model_copy(update={"overlays": overlays})
    if profile.audio.sfx.enabled:
        sfx: list[SfxCue] = steps.run(
            f"{prefix}sfx",
            lambda: _api("plan_sfx")(timeline, profile.audio.sfx),
            dump=_dump_models,
            load=lambda d: [SfxCue.model_validate(x) for x in d],
            optional=True,
            default=[],
        )
        timeline = timeline.model_copy(update={"sfx": sfx})
    return timeline


def short_profile(profile: ChannelProfile) -> ChannelProfile:
    """Profil d'un short : durée = `shorts.max_duration_s`, sans cold open, ordre chronologique."""
    sp = profile.model_copy(deep=True)
    sp.editing.target_duration_s = profile.shorts.max_duration_s
    sp.editing.cold_open.enabled = False
    sp.editing.order = "chronological"
    return sp


def _short_format(profile: ChannelProfile) -> OutputFormat:
    """Format `shorts.format` du profil ; erreur claire s'il est absent ou pas en 9:16."""
    name = profile.shorts.format
    matches = [o for o in profile.outputs if o.name == name]
    if not matches:
        known = ", ".join(o.name for o in profile.outputs)
        raise MinavioError(
            f"shorts.format '{name}' absent des outputs de {profile.id}. "
            f"Disponibles : {known}. Corrigez shorts.format dans configs/channels/{profile.id}.yaml"
        )
    if matches[0].aspect != "9:16":
        raise MinavioError(
            f"shorts.format '{name}' est en {matches[0].aspect} dans {profile.id} ; "
            "les shorts doivent être en 9:16. Corrigez shorts.format ou l'aspect de l'output."
        )
    return matches[0]


def _load_highlights(d: list[Any]) -> list[Highlight]:
    return [Highlight.model_validate(x) for x in d]


def run_job(
    profile_id: str,
    inputs: list[Path],
    *,
    formats: list[str] | None = None,
    music: Path | None = None,
    use_llm: bool = True,
    dry_run: bool = False,
    resume: bool = False,
    work_root: Path = Path("work"),
    out_dir: Path = Path("out"),
    shorts: int | None = None,
    chapters: bool = True,
    thumbnails: bool = True,
) -> JobResult:
    from minavio.config import load_profile

    profile = load_profile(profile_id)
    fmts = select_formats(profile, formats)
    if music is not None and not Path(music).is_file():  # échouer avant les étapes longues
        raise MinavioError(f"Musique introuvable : {music}")
    # Shorts : vérifié avant les étapes longues (format absent ou pas en 9:16 = erreur claire).
    n_shorts = profile.shorts.per_highlight if shorts is None else shorts
    if not 0 <= n_shorts <= MAX_SHORTS:
        raise MinavioError(f"Nombre de shorts invalide : {n_shorts} (attendu 0 à {MAX_SHORTS})")
    short_fmt = _short_format(profile) if n_shorts > 0 else None
    llm_on = use_llm and profile.llm.enabled
    warnings: list[str] = []

    sources = [Path(p) for p in _api("collect_sources")([Path(i) for i in inputs])]
    if not sources:
        raise MinavioError(f"Aucun clip vidéo trouvé dans : {', '.join(map(str, inputs))}")
    job_id = compute_job_id(profile, sources)
    work = work_root / job_id
    log.info("job %s : %d source(s), formats %s", job_id, len(sources), [f.name for f in fmts])
    steps = _Steps(work, resume, warnings)
    result = JobResult(job_id=job_id, work_dir=work, warnings=warnings, dry_run=dry_run)
    # collect est toujours rejouée (calcul du job_id) ; on persiste juste son résultat.
    (work / "collect.json").write_text(json.dumps([str(p) for p in sources], indent=2))

    highlights: list[Highlight] = steps.run(
        "detect",
        lambda: _api("run_detectors")(sources, profile),
        dump=_dump_models,
        load=_load_highlights,
    )
    if not highlights:
        raise MinavioError(
            "Aucun highlight détecté. Vérifiez les templates du jeu (configs/games/), "
            "le threshold, ou les détecteurs du profil."
        )

    by_source: dict[Path, list[TranscriptSegment]] = steps.run(
        "transcribe",
        lambda: {
            s: [seg.model_copy(update={"source": s}) for seg in _api("transcribe")(s)]
            for s in sources
        },
        dump=lambda d: {str(k): _dump_models(v) for k, v in d.items()},
        load=lambda d: {
            Path(k): [TranscriptSegment.model_validate(x) for x in v] for k, v in d.items()
        },
        optional=True,
        default={},
    )
    transcript = [seg for segs in by_source.values() for seg in segs]

    by_score = sorted(highlights, key=lambda h: h.score, reverse=True)
    copy: dict[str, Any] | None = None
    if llm_on:
        highlights = steps.run(
            "rank",
            lambda: _api("rank_highlights")(highlights, transcript, profile),
            dump=_dump_models,
            load=_load_highlights,
            optional=True,
            default=by_score,
        )
        copy = steps.run(
            "copy",
            lambda: _api("generate_copy")(highlights, transcript, profile),
            dump=_copy_to_dict,
            optional=True,
        )
        copy = _copy_to_dict(copy) if copy is not None else None
    else:
        highlights = by_score
    if not highlights:
        raise MinavioError("Le classement n'a laissé aucun highlight.")

    music_path: Path | None = steps.run(
        "music",
        lambda: pick_music(profile, job_id, music),
        dump=lambda p: {"path": str(p) if p else None},
        load=lambda d: Path(d["path"]) if d["path"] else None,
    )
    beats: list[float] = []
    if profile.audio.beat_sync and music_path:
        beats = steps.run(
            "beats", lambda: _api("detect_beats")(music_path), optional=True, default=[]
        )

    used = {h.source for h in highlights}
    plan_transcript = [seg for s, segs in by_source.items() if s in used for seg in segs]
    keep_spans: dict[Path, list[tuple[float, float]]] | None = None
    if profile.editing.autocut.enabled:
        autocut_settings = profile.editing.autocut
        analysed: dict[Path, list[tuple[float, float]]] | None = steps.run(
            "autocut",
            lambda: {
                s: _api("analyze_keep_spans")(s, autocut_settings, by_source.get(s) or None)
                for s in sorted(used, key=str)
            },
            dump=lambda d: {str(k): [[a, b] for a, b in v] for k, v in d.items()},
            load=lambda d: {Path(k): [(float(a), float(b)) for a, b in v] for k, v in d.items()},
            optional=True,
            default=None,
        )
        if analysed is not None:
            keep_spans = {s: v for s, v in analysed.items() if s in used}
    timeline: Timeline = steps.run(
        "timeline",
        lambda: _api("plan_timeline")(
            highlights,
            profile,
            beats=beats or None,
            music=music_path,
            transcript=plan_transcript or None,
            keep_spans=keep_spans,
        ),
        dump=lambda t: t.model_dump(mode="json"),
        load=Timeline.model_validate,
    )
    result.timeline_path = work / "timeline.json"
    if dry_run:
        log.info("dry-run : arrêt après timeline.json")
        return result

    target = out_dir / profile.id
    target.mkdir(parents=True, exist_ok=True)
    timeline = _decorate(steps, "", timeline, profile, copy, highlights)

    if chapters and profile.chapters:
        if not llm_on:
            log.info("chapitres ignorés : LLM désactivé (--no-llm ou llm.enabled: false)")
        else:
            chapter_list: list[Chapter] = steps.run(
                "chapters",
                lambda: _api("generate_chapters")(timeline, highlights, profile),
                dump=_dump_models,
                load=lambda d: [Chapter.model_validate(x) for x in d],
                optional=True,
                default=[],
            )
            if chapter_list:
                ch_path = target / f"{job_id}_chapters.txt"
                ch_path.write_text(_api("format_chapters")(chapter_list), encoding="utf-8")
                result.extras.append(ch_path)

    for fmt in fmts:
        out = target / f"{job_id}_{fmt.name}.mp4"
        if resume and (work / f"render_{fmt.name}.json").exists() and out.exists():
            log.info("[render_%s] reprise : %s existe", fmt.name, out)
            result.outputs.append(out)
            continue
        files = steps.run(
            f"overlay_files_{fmt.name}",
            lambda fmt=fmt: _api("render_overlays")(
                timeline, fmt, profile.motion.theme, work / "overlays" / fmt.name
            ),
            dump=lambda d: {str(k): str(v) for k, v in d.items()},
            load=lambda d: {int(k): Path(v) for k, v in d.items()},
            optional=True,
            default=None,
        )
        rendered = steps.run(
            f"render_{fmt.name}",
            lambda fmt=fmt, out=out, files=files: _api("render_timeline")(
                timeline, fmt, out, profile, overlay_files=files or None
            ),
            dump=lambda p: {"output": str(p)},
            load=lambda d: d["output"],
        )
        result.outputs.append(Path(rendered))

    if thumbnails and profile.thumbnails.enabled:
        result.extras += build_thumbnails(
            steps,
            profile,
            job_id,
            highlights,
            copy,
            work=work,
            target=target,
            use_llm=llm_on,
        )

    if n_shorts > 0 and short_fmt is not None:
        best = sorted(highlights, key=lambda h: h.score, reverse=True)[:n_shorts]
        s_profile = short_profile(profile)
        for i, h in enumerate(best, 1):
            out = target / f"{job_id}_short{i}.mp4"
            if resume and (work / f"short{i}_render.json").exists() and out.exists():
                log.info("[short%d] reprise : %s existe", i, out)
                result.outputs.append(out)
                continue
            try:
                result.outputs.append(
                    _render_short(
                        steps,
                        i,
                        h,
                        out=out,
                        fmt=short_fmt,
                        profile=s_profile,
                        work=work,
                        resume=resume,
                        transcript=[s for s in plan_transcript if s.source == h.source] or None,
                        beats=beats,
                        music=music_path,
                        keep_spans=(
                            {h.source: keep_spans[h.source]}
                            if keep_spans and h.source in keep_spans
                            else None
                        ),
                    )
                )
            except Exception as e:  # un short raté ne bloque pas le job
                msg = f"short {i} ignoré ({h.source.name} @ {h.start:.1f}s) : {e}"
                log.warning(msg)
                warnings.append(msg)
    return result


def _kills_for(choice: ThumbnailChoice, highlights: list[Highlight]) -> int | None:
    """Nombre de kills du moment de l'image principale (signals), s'il est connu."""
    main = choice.images[0]
    if "kills" in main.signals:
        return max(1, round(main.signals["kills"]))
    for h in highlights:
        if h.source == main.source and h.start <= main.t <= h.end and "kills" in h.signals:
            return max(1, round(h.signals["kills"]))
    return None


def _thumbs_llm(profile: ChannelProfile, use_llm: bool) -> Any:
    """Client LLM pour le juge visuel ; None (heuristique) si désactivé ou indisponible."""
    if not (use_llm and profile.thumbnails.use_vision_llm):
        return None
    try:
        from minavio.llm.client import LocalLLM

        return LocalLLM.from_settings(profile.llm)
    except Exception as e:
        log.warning("juge visuel indisponible (%s), choix heuristique", e)
        return None


def build_thumbnails(
    steps: _Steps,
    profile: ChannelProfile,
    job_id: str,
    highlights: list[Highlight],
    copy: dict[str, Any] | None,
    *,
    work: Path,
    target: Path,
    use_llm: bool = True,
    text: str | None = None,
    layout: str | None = None,
) -> list[Path]:
    """Étape `thumbnails` : candidats -> choix -> un PNG par choix et par format.

    Optionnelle (échec = warning). `text` impose le texte de toutes les miniatures ; `layout`
    restreint les compositions. Écrit `work/<job>/thumbnails.json`.
    """
    if layout is not None:
        profile = profile.model_copy(deep=True)
        profile.thumbnails.layouts = [layout]  # type: ignore[list-item]
    cfg = profile.thumbnails
    target.mkdir(parents=True, exist_ok=True)

    def compute() -> list[dict[str, Any]]:
        cands: list[ThumbnailCandidate] = _api("extract_candidates")(
            highlights, profile, work / "thumbs", cfg.candidates
        )
        if not cands:
            raise MinavioError("aucune image candidate extraite des sources")
        choices: list[ThumbnailChoice] = _api("pick_thumbnails")(
            cands, profile, copy=copy, llm=_thumbs_llm(profile, use_llm)
        )
        if not choices:
            raise MinavioError("aucune miniature retenue")
        records: list[dict[str, Any]] = []
        last_err: Exception | None = None
        for i, choice in enumerate(choices, 1):
            if text:
                choice = choice.model_copy(update={"text": text})
            kills = _kills_for(choice, highlights)
            for fmt in cfg.formats:
                out = target / f"{job_id}_thumb{i}_{fmt.name}.png"
                try:
                    rendered = _api("render_thumbnail")(
                        choice,
                        fmt,
                        profile.motion.theme,
                        out,
                        badge=profile.display_name.upper(),
                        kills=kills,
                    )
                except Exception as e:
                    last_err = e
                    msg = f"miniature {i} ({fmt.name}) ignorée : {e}"
                    log.warning(msg)
                    steps.warnings.append(msg)
                    continue
                records.append(
                    {
                        "index": i,
                        "format": fmt.name,
                        "layout": choice.layout,
                        "text": choice.text,
                        "file": str(rendered),
                    }
                )
        if not records and last_err is not None:
            raise MinavioError(f"aucune miniature rendue ({last_err})")
        return records

    def load(d: list[dict[str, Any]]) -> list[dict[str, Any]]:
        missing = [r["file"] for r in d if not Path(r["file"]).exists()]
        if missing or not d:
            raise MinavioError(f"fichier(s) absent(s) : {missing[:1]}")
        return d

    records = steps.run("thumbnails", compute, load=load, optional=True, default=[])
    return [Path(r["file"]) for r in records]


def _render_short(
    steps: _Steps,
    i: int,
    h: Highlight,
    *,
    out: Path,
    fmt: OutputFormat,
    profile: ChannelProfile,
    work: Path,
    resume: bool,
    transcript: list[TranscriptSegment] | None,
    beats: list[float],
    music: Path | None,
    keep_spans: dict[Path, list[tuple[float, float]]] | None,
) -> Path:
    """Timeline dédiée d'un highlight (overlays captions + kill_counter, sfx), puis rendu."""
    tl: Timeline = steps.run(
        f"short{i}_timeline",
        lambda: _api("plan_timeline")(
            [h],
            profile,
            beats=beats or None,
            music=music,
            transcript=transcript,
            keep_spans=keep_spans,
        ),
        dump=lambda t: t.model_dump(mode="json"),
        load=Timeline.model_validate,
    )
    if not tl.clips:
        raise MinavioError("montage vide")
    tl = _decorate(steps, f"short{i}_", tl, profile, None, [h])
    files = steps.run(
        f"short{i}_overlay_files",
        lambda: _api("render_overlays")(
            tl, fmt, profile.motion.theme, work / "overlays" / f"short{i}"
        ),
        dump=lambda d: {str(k): str(v) for k, v in d.items()},
        load=lambda d: {int(k): Path(v) for k, v in d.items()},
        optional=True,
        default=None,
    )
    rendered = steps.run(
        f"short{i}_render",
        lambda: _api("render_timeline")(tl, fmt, out, profile, overlay_files=files or None),
        dump=lambda p: {"output": str(p)},
        load=lambda d: d["output"],
    )
    return Path(rendered)
