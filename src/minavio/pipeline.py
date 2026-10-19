"""Orchestration du pipeline `minavio run`.

Étapes : collect → detect → transcribe → rank → copy → music → beats → autocut → timeline →
overlays → render. `autocut` n'existe que si `profile.editing.autocut.enabled` : elle tourne sur
chaque source retenue (après `transcribe`, avant `timeline`) et ses plages conservées (`keep_spans`)
sont passées à `plan_timeline`. En cas d'échec, un warning est émis et le montage se fait sans
coupe de silences.

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
from minavio.core.models import Highlight, Overlay, Timeline, TranscriptSegment
from minavio.errors import MinavioError

log = logging.getLogger("minavio.pipeline")

TITLE_DURATION_S = 2.5
POPUP_DURATION_S = 2.0
POPUP_SNAP_S = 2.0
AUDIO_EXTS = {".mp3", ".wav", ".flac", ".m4a", ".ogg", ".aac"}

_API = {
    "collect_sources": "minavio.core.ffmpeg:collect_sources",
    "run_detectors": "minavio.core.detect:run_detectors",
    "transcribe": "minavio.core.asr:transcribe",
    "analyze_keep_spans": "minavio.core.autocut:analyze_keep_spans",
    "detect_beats": "minavio.core.audio.beats:detect_beats",
    "rank_highlights": "minavio.llm.rank:rank_highlights",
    "generate_copy": "minavio.llm.copywriting:generate_copy",
    "plan_timeline": "minavio.core.edit:plan_timeline",
    "build_caption_overlays": "minavio.motion.captions:build_caption_overlays",
    "render_overlays": "minavio.motion.render:render_overlays",
    "render_timeline": "minavio.core.render:render_timeline",
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
        for c in timeline.clips:
            if c.source == source:
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


def build_overlays(
    timeline: Timeline,
    profile: ChannelProfile,
    copy: dict[str, Any] | None,
    highlights: list[Highlight] | None = None,
) -> list[Overlay]:
    """Sous-titres (si activés) + titre (hook, 2.5 s au début) + popups du copy."""
    overlays: list[Overlay] = []
    cap = profile.motion.captions
    if cap.enabled and timeline.captions:
        overlays += _api("build_caption_overlays")(timeline.captions, cap)
    if copy:
        if copy.get("hook"):
            overlays.append(
                Overlay(
                    template="title",
                    start=0,
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
    return overlays


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
) -> JobResult:
    from minavio.config import load_profile

    profile = load_profile(profile_id)
    fmts = select_formats(profile, formats)
    if music is not None and not Path(music).is_file():  # échouer avant les étapes longues
        raise MinavioError(f"Musique introuvable : {music}")
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

    overlays: list[Overlay] = steps.run(
        "overlays",
        lambda: build_overlays(timeline, profile, copy, highlights),
        dump=_dump_models,
        load=lambda d: [Overlay.model_validate(x) for x in d],
        optional=True,
        default=[],
    )
    timeline = timeline.model_copy(update={"overlays": overlays})

    target = out_dir / profile.id
    target.mkdir(parents=True, exist_ok=True)
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
    return result
