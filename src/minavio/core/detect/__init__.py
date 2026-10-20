"""Détecteurs de moments forts (plugins enregistrés par nom)."""

from minavio.core.detect import (
    audio_peak,
    chat_spike,
    hype,
    scene_change,
    speech,
    template_match,
    vision_scan,
)
from minavio.core.detect.registry import (
    DETECTORS,
    DetectorUnavailable,
    merge_highlights,
    register_detector,
    run_detectors,
)

__all__ = [
    "DETECTORS",
    "DetectorUnavailable",
    "audio_peak",
    "chat_spike",
    "hype",
    "merge_highlights",
    "register_detector",
    "run_detectors",
    "scene_change",
    "speech",
    "template_match",
    "vision_scan",
]
