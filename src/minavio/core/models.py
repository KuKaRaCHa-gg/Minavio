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


class SfxCue(_Model):
    """Effet sonore ponctuel ; `at` en secondes de la timeline rendue."""

    at: float = Field(ge=0)
    sound: str  # nom logique : "whoosh", "hit", "boom", "rewind"…
    gain_db: float = Field(default=0, le=12)


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
