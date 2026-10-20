"""Détecteur « juge visuel » : enveloppe mince autour de `minavio.llm.vision_scan`.

Params : `mode` ("candidates"|"full"), `every_s`, `max_calls` (60/source), `min_excitement` (6),
`pre_s` (3), `post_s` (1.5), `allow_cloud` (false), `cache_dir`, `candidate_detectors`.
"""

from __future__ import annotations

from pathlib import Path

from minavio.config.schema import ChannelProfile
from minavio.core.detect.registry import DetectorUnavailable, register_detector
from minavio.core.models import Highlight
from minavio.errors import LLMUnavailableError


@register_detector("vision_scan")
def vision_scan(sources: list[Path], params: dict, profile: ChannelProfile) -> list[Highlight]:
    from minavio.llm.vision_scan import run_vision_scan

    try:
        return run_vision_scan(sources, params, profile)
    except LLMUnavailableError as exc:
        raise DetectorUnavailable(str(exc)) from exc
