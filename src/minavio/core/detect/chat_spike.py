"""Détecteur bonus « chat_spike » : pics de débit du chat Twitch à côté du clip.

Cherche `<stem>.chat.json`, `<stem>.json`, `<stem>.chat.txt`, `<stem>.txt` (même dossier).
Formats : TwitchDownloader (`comments[].content_offset_seconds`), chat-downloader (liste avec
`time_in_seconds`), texte `[H:MM:SS] user: msg` ou `[MM:SS] ...`. Optionnel : jamais d'erreur.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from minavio.config.schema import ChannelProfile
from minavio.core import ffmpeg
from minavio.core.detect.hype import _make_highlight
from minavio.core.detect.registry import register_detector
from minavio.core.models import Highlight

log = logging.getLogger("minavio.detect.chat_spike")

SUFFIXES = (".chat.json", ".json", ".chat.txt", ".txt")
_TS = re.compile(r"^\s*\[(\d+):(\d{1,2})(?::(\d{1,2}))?(?:[.,]\d+)?\]")


def _from_json(data) -> list[float]:
    if isinstance(data, dict):
        rows = data.get("comments")
        key = "content_offset_seconds"
    else:
        rows, key = data, "time_in_seconds"
    if not isinstance(rows, list):
        raise ValueError("structure JSON de chat non reconnue")
    times = [float(r[key]) for r in rows if isinstance(r, dict) and key in r]
    if rows and not times:
        raise ValueError(f"aucun champ '{key}'")
    return times


def _from_text(text: str) -> list[float]:
    times: list[float] = []
    for line in text.splitlines():
        m = _TS.match(line)
        if not m:
            continue
        a, b, c = m.groups()
        times.append(
            int(a) * 3600 + int(b) * 60 + int(c) if c is not None else int(a) * 60 + int(b)
        )
    if not times:
        raise ValueError("aucune ligne '[H:MM:SS] user: msg'")
    return [float(t) for t in times]


def load_chat_times(path: Path) -> list[float]:
    """Horodatages (s) des messages ; lève ValueError/OSError si le fichier est inexploitable."""
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix == ".json":
        try:
            return _from_json(json.loads(text))
        except json.JSONDecodeError as e:
            raise ValueError(f"JSON invalide ({e})") from e
    return _from_text(text)


def find_chat_log(source: Path) -> list[Path]:
    return [p for s in SUFFIXES if (p := source.with_name(source.stem + s)).is_file()]


@register_detector("chat_spike")
def chat_spike(sources: list[Path], params: dict, profile: ChannelProfile) -> list[Highlight]:
    import numpy as np

    smooth_s = float(params.get("smooth_s", 5.0))
    z_thr = float(params.get("z_threshold", 2.0))
    lag_s = float(params.get("lag_s", 4.0))
    pre_s = float(params.get("pre_s", 5.0))
    post_s = float(params.get("post_s", 3.0))
    min_msgs = float(params.get("min_messages", 5))
    min_gap = float(params.get("min_gap_s", 10.0))

    out: list[Highlight] = []
    for src in sources:
        times: list[float] | None = None
        for cand in find_chat_log(src):
            try:
                times = load_chat_times(cand)
                break
            except (ValueError, OSError, KeyError, TypeError) as e:
                log.warning("chat_spike : log de chat illisible %s : %s", cand, e)
        if not times:
            log.debug("chat_spike : pas de log de chat pour %s", src)
            continue
        try:
            duration = ffmpeg.probe(src).duration
        except Exception:
            duration = max(times) + 1
        n = max(1, int(np.ceil(duration)))
        counts = np.zeros(n)
        for t in times:
            if 0 <= t < n:
                counts[int(t)] += 1
        k = max(1, round(smooth_s))
        window_sum = np.convolve(counts, np.ones(k), mode="same")
        rate = window_sum / k
        std = float(rate.std())
        if std < 1e-9:
            continue
        z = (rate - float(rate.mean())) / std

        picked: list[int] = []
        for i in np.argsort(-z):
            if z[i] < z_thr:
                break
            if window_sum[i] < min_msgs:
                continue
            if all(abs(int(i) - p) >= min_gap for p in picked):
                picked.append(int(i))
        for i in sorted(picked):
            center = max(0.0, i + 0.5 - lag_s)
            start = max(0.0, center - pre_s)
            end = min(duration, center + post_s)
            if end <= start:
                continue
            zi = float(z[i])
            out.append(
                _make_highlight(
                    source=src,
                    signals={"chat_rate": round(float(rate[i]), 3), "chat_z": round(zi, 3)},
                    start=start,
                    end=end,
                    score=float(min(1.0, zi / (2 * z_thr))),
                    label="chat_spike",
                    detector="chat_spike",
                    reason=f"pic de chat {rate[i]:.1f} msg/s (z={zi:.1f}) à {i}s",
                )
            )
    return out
