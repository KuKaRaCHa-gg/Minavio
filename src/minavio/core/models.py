"""Contrat de données entre modules (détection → LLM → montage → motion → rendu).

Seul l'orchestrateur modifie ce fichier. Tous les temps sont en secondes dans le média source,
sauf mention contraire.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "1"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _TimeSpan(_Model):
    start: float = Field(ge=0)
    end: float = Field(gt=0)

    @model_validator(mode="after")
    def _check_order(self) -> _TimeSpan:
        if self.end <= self.start:
            raise ValueError(f"end ({self.end}) doit être > start ({self.start})")
        return self

    @property
    def duration(self) -> float:
        return self.end - self.start


class Word(_TimeSpan):
    text: str
    confidence: float | None = Field(default=None, ge=0, le=1)


class TranscriptSegment(_TimeSpan):
    text: str
    speaker: str | None = None
    source: Path | None = None  # média d'origine ; requis pour les captions multi-sources
    words: list[Word] = Field(default_factory=list)  # requis pour les sous-titres mot-à-mot


class Highlight(_TimeSpan):
    """Moment candidat produit par un détecteur, puis éventuellement re-scoré par le LLM."""

    source: Path
    score: float = Field(ge=0, le=1)
    label: str  # ex. "kill", "clutch", "laugh", "fun_fact"
    detector: str
    reason: str | None = None
    # Explicabilité : contribution de chaque signal (ex. {"laughter": 0.8, "loudness_z": 2.1}).
    signals: dict[str, float] = Field(default_factory=dict)


class Overlay(_Model):
    """Élément motion design ; `start` est relatif au début de la timeline rendue."""

    template: str
    start: float = Field(ge=0)
    duration: float = Field(gt=0)
    props: dict[str, Any] = Field(default_factory=dict)


class Clip(_TimeSpan):
    source: Path
    speed: float = Field(default=1.0, gt=0)
    highlight_label: str | None = None
    zoom: float = Field(default=1.0, ge=1.0, le=2.0)  # punch-in centré (masque les jump-cuts)
    # Posés par plan_timeline : identifiant du moment (fragments d'autocut d'une même fenêtre =
    # même id ; cold open = -1) et instant décisif du moment (kill, pic), en temps SOURCE.
    moment: int | None = None
    peak_t: float | None = None


class SfxCue(_Model):
    """Effet sonore ponctuel ; `at` en secondes de la timeline rendue."""

    at: float = Field(ge=0)
    sound: str  # nom logique : "whoosh", "hit", "boom", "rewind"…
    gain_db: float = Field(default=0, le=12)


class ThumbnailCandidate(_Model):
    """Image extraite d'une source, notée avant le choix final des miniatures."""

    source: Path
    t: float = Field(ge=0)  # temps source
    image: Path  # PNG extrait (work/<job>/thumbs/)
    score: float = Field(ge=0, le=1)
    label: str | None = None  # label du moment (kill, laugh…)
    signals: dict[str, float] = Field(default_factory=dict)  # sharpness, face, motion…


class ThumbnailChoice(_Model):
    """Composition choisie : 1 (hero), 2 (split) ou 3 (collage) images + texte."""

    layout: str
    images: list[ThumbnailCandidate] = Field(min_length=1, max_length=3)
    text: str
    reason: str | None = None


class Chapter(_Model):
    start: float = Field(ge=0)  # secondes de la timeline rendue
    title: str


class Timeline(_Model):
    schema_version: str = SCHEMA_VERSION
    profile_id: str
    fps: int = Field(gt=0)
    clips: list[Clip]
    overlays: list[Overlay] = Field(default_factory=list)
    music: Path | None = None
    beats: list[float] = Field(default_factory=list)  # temps de la timeline rendue
    captions: list[TranscriptSegment] = Field(default_factory=list)
    sfx: list[SfxCue] = Field(default_factory=list)
    # Index (dans `clips`) du premier clip « réel » : les clips avant sont un cold open.
    cold_open_clips: int = Field(default=0, ge=0)

    @property
    def duration(self) -> float:
        return sum(c.duration / c.speed for c in self.clips)

    def clip_starts(self) -> list[float]:
        """Début de chaque clip en temps de la timeline rendue."""
        out, t = [], 0.0
        for c in self.clips:
            out.append(t)
            t += c.duration / c.speed
        return out

    def moments(self) -> list[Moment]:
        """Regroupe les clips consécutifs d'un même moment. Source de vérité : `Clip.moment`
        (posé par plan_timeline). Repli pour les timelines sans ids : même source, même label,
        écart source < MOMENT_GAP_S. Le cold open est toujours un moment à part."""
        starts = self.clip_starts()
        use_ids = bool(self.clips) and all(c.moment is not None for c in self.clips)
        moments: list[Moment] = []
        for i, c in enumerate(self.clips):
            prev = self.clips[i - 1] if i else None
            if prev is None or i == self.cold_open_clips:
                same = False
            elif use_ids:
                same = c.moment == prev.moment
            else:
                same = (
                    c.source == prev.source
                    and c.highlight_label == prev.highlight_label
                    and 0 <= c.start - prev.end < MOMENT_GAP_S
                )
            end = starts[i] + c.duration / c.speed
            if same:
                moments[-1] = moments[-1].model_copy(update={"end": end, "last_clip": i})
            else:
                moments.append(
                    Moment(
                        start=starts[i],
                        end=end,
                        first_clip=i,
                        last_clip=i,
                        label=c.highlight_label,
                        cold_open=i < self.cold_open_clips,
                    )
                )
        # Instant décisif en temps timeline : le clip du moment qui contient peak_t.
        for k, m in enumerate(moments):
            for i in range(m.first_clip, m.last_clip + 1):
                c = self.clips[i]
                if c.peak_t is not None and c.start <= c.peak_t < c.end:
                    peak = starts[i] + (c.peak_t - c.start) / c.speed
                    moments[k] = m.model_copy(update={"peak": peak})
                    break
        return moments


MOMENT_GAP_S = 3.0
REWIND_DURATION_S = 0.8  # transition rewind (visuel + son) sur la fin du cold open


class Moment(_TimeSpan):
    """Groupe de clips consécutifs (temps de la timeline rendue)."""

    first_clip: int
    last_clip: int
    label: str | None = None
    cold_open: bool = False
    peak: float | None = None  # instant décisif (temps timeline), si le moment le contient
