"""Registre des détecteurs et fusion des highlights."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from minavio.config.schema import ChannelProfile
from minavio.core.models import Highlight

log = logging.getLogger("minavio.detect")

# (sources, params, profile) -> highlights avec score 0..1 (le poids est appliqué par run_detectors)
Detector = Callable[[list[Path], dict, ChannelProfile], list[Highlight]]

DETECTORS: dict[str, Detector] = {}


class DetectorUnavailable(Exception):
    """Dépendance ou ressource absente : le détecteur est ignoré avec un warning."""


def register_detector(name: str) -> Callable[[Detector], Detector]:
    def deco(fn: Detector) -> Detector:
        DETECTORS[name] = fn
        return fn

    return deco


def merge_highlights(highlights: list[Highlight]) -> list[Highlight]:
    """Fusionne les highlights qui se chevauchent (même source) ; garde le label du meilleur."""
    by_source: dict[Path, list[Highlight]] = {}
    for h in highlights:
        by_source.setdefault(h.source, []).append(h)
    merged: list[Highlight] = []
    for source in sorted(by_source, key=str):
        group: list[list[Highlight]] = []
        end = -1.0
        for h in sorted(by_source[source], key=lambda x: (x.start, x.end)):
            if group and h.start < end:
                group[-1].append(h)
                end = max(end, h.end)
            else:
                group.append([h])
                end = h.end
        for members in group:
            best = max(members, key=lambda x: x.score)
            detectors = sorted({m.detector for m in members})
            bonus = 0.1 * (len(detectors) - 1)
            reasons = [m.reason for m in members if m.reason]
            signals: dict[str, float] = {}
            for m in members:  # explicabilité : max par signal sur les détecteurs fusionnés
                for k, v in m.signals.items():
                    signals[k] = max(v, signals.get(k, v))
            merged.append(
                Highlight(
                    source=source,
                    start=min(m.start for m in members),
                    end=max(m.end for m in members),
                    score=min(1.0, best.score + bonus),
                    label=best.label,
                    detector="+".join(detectors),
                    reason="; ".join(dict.fromkeys(reasons)) or None,
                    signals=signals,
                )
            )
    return merged


def run_detectors(sources: list[Path], profile: ChannelProfile) -> list[Highlight]:
    """Exécute les détecteurs du profil, pondère les scores et fusionne les chevauchements."""
    collected: list[Highlight] = []
    for ref in profile.detection:
        fn = DETECTORS.get(ref.name)
        if fn is None:
            log.warning(
                "Détecteur inconnu '%s' : ignoré (connus : %s)", ref.name, sorted(DETECTORS)
            )
            continue
        if ref.weight == 0:
            continue
        try:
            found = fn(list(sources), dict(ref.params), profile)
        except (DetectorUnavailable, ImportError) as e:
            log.warning("Détecteur '%s' ignoré : %s", ref.name, e)
            continue
        for h in found:
            collected.append(h.model_copy(update={"score": min(1.0, h.score * ref.weight)}))
    return sorted(merge_highlights(collected), key=lambda h: (str(h.source), h.start))
