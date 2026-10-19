"""Détecteur de changements de scène (filtre FFmpeg `select=gt(scene,T)`)."""

from __future__ import annotations

import re
from pathlib import Path

from minavio.config.schema import ChannelProfile
from minavio.core import ffmpeg
from minavio.core.detect.registry import register_detector
from minavio.core.models import Highlight

_TIME = re.compile(r"pts_time:([0-9.]+)")
_SCORE = re.compile(r"lavfi\.scene_score=([0-9.]+)")


@register_detector("scene_change")
def scene_change(sources: list[Path], params: dict, profile: ChannelProfile) -> list[Highlight]:
    threshold = float(params.get("threshold", 0.3))
    half = float(params.get("window_s", 1.0))
    out: list[Highlight] = []
    for src in sources:
        info = ffmpeg.probe(src)
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
        t = None
        for line in text.splitlines():
            if m := _TIME.search(line):
                t = float(m.group(1))
            elif (m := _SCORE.search(line)) and t is not None:
                score = min(1.0, float(m.group(1)))
                start = max(0.0, t - half)
                end = min(info.duration, t + half) if info.duration else t + half
                if end > start:
                    out.append(
                        Highlight(
                            source=src,
                            start=start,
                            end=end,
                            score=score,
                            label="scene_change",
                            detector="scene_change",
                            reason=f"changement de scène à {t:.2f}s",
                        )
                    )
                t = None
    return out
