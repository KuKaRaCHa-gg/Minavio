"""Détecteur de changements de scène.

Backend PySceneDetect (`AdaptiveDetector`, robuste aux virages de caméra rapides) quand le paquet
`scenedetect` est installé (extra `vision`), sinon filtre FFmpeg `select=gt(scene,T)`.
Params : `backend` ("auto"|"scenedetect"|"opencv"), `adaptive_threshold` (3.0), `min_scene_len_s`
(0.5), `threshold` (backend opencv), `window_s`.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from minavio.config.schema import ChannelProfile
from minavio.core import ffmpeg
from minavio.core.detect.registry import DetectorUnavailable, register_detector
from minavio.core.models import Highlight

log = logging.getLogger("minavio.detect")

_TIME = re.compile(r"pts_time:([0-9.]+)")
_SCORE = re.compile(r"lavfi\.scene_score=([0-9.]+)")


def _highlight(src: Path, t: float, score: float, duration: float, half: float) -> Highlight | None:
    start = max(0.0, t - half)
    end = min(duration, t + half) if duration else t + half
    if end <= start:
        return None
    return Highlight(
        source=src,
        start=start,
        end=end,
        score=min(1.0, max(0.05, score)),
        label="scene_change",
        detector="scene_change",
        reason=f"changement de scène à {t:.2f}s",
    )


def _scenedetect_cuts(src: Path, params: dict) -> list[tuple[float, float]]:
    """(temps, score 0..1) de chaque coupe franche, via AdaptiveDetector."""
    from scenedetect import AdaptiveDetector, SceneManager, open_video  # import paresseux
    from scenedetect.stats_manager import StatsManager

    threshold = float(params.get("adaptive_threshold", 3.0))
    min_len = float(params.get("min_scene_len_s", 0.5))
    video = open_video(str(src))
    stats = StatsManager()
    manager = SceneManager(stats_manager=stats)
    manager.add_detector(AdaptiveDetector(adaptive_threshold=threshold, min_scene_len=min_len))
    manager.detect_scenes(video)
    scenes = manager.get_scene_list()
    cuts: list[tuple[float, float]] = []
    for _, end in scenes[:-1]:  # la fin de chaque scène sauf la dernière = une coupe
        score = 0.8
        try:  # content_val ~ 15 (faible) .. 60+ (coupe franche)
            (val,) = stats.get_metrics(end.frame_num, ["content_val"])
            if val is not None:
                score = float(val) / 60
        except Exception:
            pass
        secs = end.seconds if hasattr(end, "seconds") else end.get_seconds()  # 0.7 / 0.6
        cuts.append((secs, score))
    return cuts


def _opencv_cuts(src: Path, params: dict) -> list[tuple[float, float]]:
    threshold = float(params.get("threshold", 0.3))
    text = ffmpeg.run_capture(
        [
            "-i",
            str(src),
            "-an",
            "-vf",
            f"select='gt(scene,{threshold})',metadata=print:file=-",
            "-f",
            "null",
            "-",
        ]
    ).decode("utf-8", errors="replace")
    cuts: list[tuple[float, float]] = []
    t = None
    for line in text.splitlines():
        if m := _TIME.search(line):
            t = float(m.group(1))
        elif (m := _SCORE.search(line)) and t is not None:
            cuts.append((t, float(m.group(1))))
            t = None
    return cuts


def _has_scenedetect() -> bool:
    try:
        import scenedetect  # noqa: F401
    except ImportError:
        return False
    return True


@register_detector("scene_change")
def scene_change(sources: list[Path], params: dict, profile: ChannelProfile) -> list[Highlight]:
    half = float(params.get("window_s", 1.0))
    backend = str(params.get("backend", "auto"))
    if backend not in ("auto", "scenedetect", "opencv"):
        raise ValueError(f"scene_change: backend inconnu {backend!r} (auto|scenedetect|opencv)")
    use_sd = backend != "opencv" and _has_scenedetect()
    if backend == "scenedetect" and not use_sd:
        raise DetectorUnavailable("backend=scenedetect demandé : `pip install scenedetect`")
    if backend == "auto" and not use_sd:
        log.info("scenedetect absent : scene_change utilise le filtre FFmpeg (extra `vision`)")
    out: list[Highlight] = []
    for src in sources:
        duration = ffmpeg.probe(src).duration
        cuts = _scenedetect_cuts(src, params) if use_sd else _opencv_cuts(src, params)
        for t, score in cuts:
            h = _highlight(src, t, score, duration, half)
            if h:
                out.append(h)
    return out
