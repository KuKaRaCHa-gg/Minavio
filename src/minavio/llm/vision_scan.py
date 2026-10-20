"""Juge visuel des moments forts : un VLM local REGARDE la vidéo et note l'intensité.

Confidentialité / coût : seuls les fournisseurs `kind: local` de la chaîne sont utilisés ; un
fournisseur cloud n'entre en jeu que si `allow_cloud: true` ET s'il a un `vision_model` explicite.

Échantillonnage (sinon trop lent) : mode `candidates` = fenêtres suspectes issues des détecteurs
bon marché + points réguliers ; mode `full` = une image toutes les `every_s`. Par fenêtre, 3 images
(-1 s, pic, +1 s) réduites à 448 px, en UN seul appel.
"""

from __future__ import annotations

import hashlib
import json
import logging
import tempfile
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from minavio.config.schema import ChannelProfile, LlmProvider
from minavio.core import ffmpeg
from minavio.core.models import Highlight
from minavio.errors import FFmpegError, LLMUnavailableError
from minavio.llm.prompts import load_prompt
from minavio.llm.providers import LLMChain, build_provider

log = logging.getLogger(__name__)

EVENTS = ("kill", "multi_kill", "knock", "clutch", "win", "fail", "funny", "chaos", "none")
IMAGE_WIDTH = 448
FRAME_OFFSETS = (-1.0, 0.0, 1.0)
CHEAP_DETECTORS = ("audio_peak", "scene_change", "motion_intensity")


class VisionVerdict(BaseModel):
    model_config = ConfigDict(extra="ignore")

    excitement: float = Field(ge=0, le=10)
    events: list[str] = Field(default_factory=list)
    kills_visible: int = Field(default=0, ge=0)
    reason: str = ""

    @field_validator("excitement", mode="before")
    @classmethod
    def _clamp(cls, v: Any) -> Any:
        # 11/10 ou -1 : on borne plutôt que de jeter la fenêtre
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return min(10.0, max(0.0, float(v)))
        return v

    @field_validator("events", mode="before")
    @classmethod
    def _events(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = [v]
        if not isinstance(v, list):
            return v
        return [e for e in (str(x).strip().lower() for x in v) if e in EVENTS]

    @field_validator("kills_visible", mode="before")
    @classmethod
    def _kills(cls, v: Any) -> Any:
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return max(0, int(v))
        return v


class ScanStats:
    def __init__(self) -> None:
        self.calls = 0
        self.cached = 0
        self.invalid = 0
        self.seconds = 0.0


# Cache mémoire : (source, t, mtime_ns, modèle, prompt) -> verdict (dict) ou None (JSON invalide).
_MEM_CACHE: dict[tuple[str, float, int, str, str], dict | None] = {}


def select_vision_providers(profile: ChannelProfile, allow_cloud: bool = False) -> LLMChain:
    """Chaîne limitée aux fournisseurs autorisés pour la vision ; vide si aucun."""
    settings = profile.llm
    if not settings.enabled:
        return LLMChain([])
    declared = settings.providers or [LlmProvider(kind="local")]
    chosen = []
    for p in declared:
        if p.kind != "local" and not (allow_cloud and p.vision_model):
            log.info("vision_scan : fournisseur %s ignoré (allow_cloud + vision_model)", p.kind)
            continue
        built = build_provider(p, settings)
        if built is not None and built.vision_model:
            chosen.append(built)
    return LLMChain(chosen)


# ---------------------------------------------------------------- échantillonnage


def _frange(start: float, stop: float, step: float) -> list[float]:
    out, t = [], max(0.0, start)
    step = max(step, 0.5)
    while t < stop:
        out.append(round(t, 3))
        t += step
    return out


def _even_subsample(items: list[float], n: int) -> list[float]:
    if n <= 0:
        return []
    if len(items) <= n:
        return list(items)
    step = len(items) / n
    return [items[int(i * step)] for i in range(n)]


def candidate_points(
    src: Path, duration: float, params: dict, profile: ChannelProfile
) -> list[tuple[float, float]]:
    """(t, score) issus des détecteurs bon marché du registre ; absents ou en échec = ignorés."""
    from minavio.core.detect.registry import DETECTORS

    names = params.get("candidate_detectors") or CHEAP_DETECTORS
    sub_params = params.get("detector_params") or {}
    points: list[tuple[float, float]] = []
    for name in names:
        fn = DETECTORS.get(name)
        if fn is None or name == "vision_scan":
            continue
        try:
            found = fn([src], dict(sub_params.get(name, {})), profile)
        except Exception as exc:  # détecteur optionnel : ne bloque jamais le scan
            log.info("vision_scan : détecteur candidat '%s' ignoré (%s)", name, exc)
            continue
        for h in found:
            t = h.signals.get("peak_t", (h.start + h.end) / 2)
            points.append((min(max(0.0, t), duration), h.score))
    return points


def plan_times(
    duration: float,
    mode: str,
    params: dict,
    candidates: list[tuple[float, float]] | None = None,
) -> list[float]:
    """Instants (pics) à regarder, au plus `max_calls`, triés."""
    max_calls = int(params.get("max_calls", 60))
    if duration <= 0 or max_calls <= 0:
        return []
    if mode == "full":
        every = max(float(params.get("every_s", 3.0)), 0.5)
        times = _frange(every / 2, duration, every)
        if len(times) > max_calls:
            log.warning(
                "vision_scan (full) : %d points > max_calls=%d, échantillonnage espacé.",
                len(times),
                max_calls,
            )
        return sorted(_even_subsample(times, max_calls))
    every = float(params.get("every_s", 20.0))
    min_gap = float(params.get("min_gap_s", 4.0))
    regular = _frange(every / 2, duration, every)
    reserve = min(len(regular), max_calls // 3)  # couverture minimale « pour ne rien rater »
    chosen: list[float] = []
    for t, _ in sorted(candidates or [], key=lambda p: p[1], reverse=True):
        if len(chosen) >= max_calls - reserve:
            break
        if all(abs(t - x) >= min_gap for x in chosen):
            chosen.append(t)
    free = [t for t in regular if all(abs(t - x) >= min_gap for x in chosen)]
    chosen += _even_subsample(free, max_calls - len(chosen))
    return sorted(chosen)


# ---------------------------------------------------------------- images


def extract_frames(src: Path, t: float, duration: float, out_dir: Path, tag: str) -> list[Path]:
    """3 images (-1 s, pic, +1 s) en JPEG, 448 px de large."""
    paths = []
    last = max(0.0, duration - 0.1)
    for i, off in enumerate(FRAME_OFFSETS):
        ts = min(max(0.0, t + off), last)
        dest = out_dir / f"{tag}_{i}.jpg"
        ffmpeg.run(
            [
                "-ss",
                f"{ts:.3f}",
                "-i",
                str(src),
                "-frames:v",
                "1",
                "-vf",
                f"scale={IMAGE_WIDTH}:-2",
                "-q:v",
                "4",
                str(dest),
            ]
        )
        if not dest.exists():
            raise FFmpegError(f"Image non extraite à {ts:.2f}s de {src}")
        paths.append(dest)
    return paths


# ---------------------------------------------------------------- cache disque optionnel


def _disk_cache_path(cache_dir: Path, src: Path) -> Path:
    key = hashlib.sha1(str(Path(src).resolve()).encode()).hexdigest()[:16]
    return cache_dir / f"vision_scan_{key}.json"


def _load_disk(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


# ---------------------------------------------------------------- scan


def _system(profile: ChannelProfile, llm: LLMChain) -> str:
    """Prompt système + fiches de connaissances (jeux de la chaîne : quoi repérer à l'image)."""
    from minavio.llm.kb import query_from, with_knowledge

    query = query_from(
        profile.display_name, *profile.games, "killfeed kill knock clutch squad wipe finish"
    )
    return with_knowledge(
        load_prompt(profile.llm.prompt, "vision_scan"), profile, "vision_scan", query, llm
    )


def _messages(profile: ChannelProfile, t: float, system: str | None = None) -> list[dict[str, str]]:
    user = (
        f"Chaîne : {profile.display_name}.\n"
        f"3 images consécutives ci-jointes : t-1 s, t (pic présumé), t+1 s, avec t = {t:.1f} s.\n"
        "Juge le moment montré. Réponds UNIQUEMENT par le JSON demandé."
    )
    return [
        {"role": "system", "content": system or load_prompt(profile.llm.prompt, "vision_scan")},
        {"role": "user", "content": user},
    ]


def _to_highlight(
    src: Path, t: float, v: VisionVerdict, duration: float, pre: float, post: float
) -> Highlight:
    label = next((e for e in v.events if e != "none"), "hype")
    return Highlight(
        source=src,
        start=max(0.0, t - pre),
        end=min(duration, t + post) if duration else t + post,
        score=v.excitement / 10,
        label=label,
        detector="vision_scan",
        reason=v.reason or None,
        signals={"excitement": v.excitement, "kills": float(v.kills_visible), "peak_t": t},
    )


def scan_source(
    src: Path,
    profile: ChannelProfile,
    params: dict,
    llm: LLMChain,
    stats: ScanStats | None = None,
) -> list[Highlight]:
    stats = stats or ScanStats()
    mode = str(params.get("mode", "candidates"))
    if mode not in ("candidates", "full"):
        raise ValueError(f"vision_scan : mode inconnu '{mode}' (candidates | full)")
    min_exc = float(params.get("min_excitement", 6))
    pre, post = float(params.get("pre_s", 3.0)), float(params.get("post_s", 1.5))
    duration = ffmpeg.probe(src).duration
    cands = candidate_points(src, duration, params, profile) if mode == "candidates" else None
    times = plan_times(duration, mode, params, cands)
    mtime = Path(src).stat().st_mtime_ns
    model = llm.vision_model or ""
    prompt_id = profile.llm.prompt
    cache_dir = Path(params["cache_dir"]) if params.get("cache_dir") else None
    disk_path = _disk_cache_path(cache_dir, src) if cache_dir else None
    disk = _load_disk(disk_path) if disk_path else {}
    disk_dirty = False

    out: list[Highlight] = []
    answered = False
    system: str | None = None  # calculé au premier appel réel (pas pour un scan tout en cache)
    with tempfile.TemporaryDirectory(prefix="minavio_vscan_") as td:
        for i, t in enumerate(times):
            key = (str(src), round(t, 2), mtime, model, prompt_id)
            dkey = f"{mtime}|{round(t, 2)}|{model}|{prompt_id}"
            if key in _MEM_CACHE:
                raw = _MEM_CACHE[key]
                stats.cached += 1
            elif dkey in disk:
                raw = disk[dkey]
                stats.cached += 1
            else:
                try:
                    frames = extract_frames(src, t, duration, Path(td), f"w{i}")
                except FFmpegError as exc:
                    log.warning("vision_scan : extraction impossible à %.1fs (%s)", t, exc)
                    continue
                t0 = time.monotonic()
                stats.calls += 1
                try:
                    system = system or _system(profile, llm)
                    msgs = _messages(profile, t, system)
                    raw = llm.chat_json(msgs, VisionVerdict, frames).model_dump()
                except (ValueError, ValidationError) as exc:
                    log.warning("vision_scan : JSON invalide à %.1fs, fenêtre ignorée (%s)", t, exc)
                    stats.invalid += 1
                    raw = None
                except LLMUnavailableError:
                    if not answered:
                        raise
                    log.warning("vision_scan : VLM indisponible, scan interrompu à %.1fs.", t)
                    break
                finally:
                    stats.seconds += time.monotonic() - t0
                answered = True
                _MEM_CACHE[key] = raw
                disk[dkey] = raw
                disk_dirty = True
            if raw is None:
                continue
            v = VisionVerdict.model_validate(raw)
            if v.excitement >= min_exc:
                out.append(_to_highlight(src, t, v, duration, pre, post))
    if disk_path and disk_dirty:
        try:
            disk_path.parent.mkdir(parents=True, exist_ok=True)
            disk_path.write_text(json.dumps(disk), encoding="utf-8")
        except OSError as exc:
            log.warning("vision_scan : cache disque non écrit (%s)", exc)
    return out


def run_vision_scan(
    sources: list[Path], params: dict, profile: ChannelProfile, llm: LLMChain | None = None
) -> list[Highlight]:
    """Point d'entrée : lève LLMUnavailableError si aucun fournisseur vision n'est utilisable."""
    llm = llm or select_vision_providers(profile, bool(params.get("allow_cloud", False)))
    if not llm.providers:
        raise LLMUnavailableError(
            "vision_scan : aucun fournisseur vision autorisé (fournisseur `local` avec modèle "
            "vision, ou cloud avec `allow_cloud: true` et `vision_model`). "
            "Ex. : `ollama pull qwen3-vl:8b`."
        )
    stats = ScanStats()
    start = time.monotonic()
    found: list[Highlight] = []
    for src in sources:
        found += scan_source(Path(src), profile, params, llm, stats)
    log.info(
        "vision_scan : %d appels VLM (%d en cache, %d invalides), %.1fs de VLM, %.1fs au total, "
        "%d moments",
        stats.calls,
        stats.cached,
        stats.invalid,
        stats.seconds,
        time.monotonic() - start,
        len(found),
    )
    return found
