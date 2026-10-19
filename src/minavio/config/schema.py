"""Schéma des profils de chaîne (`configs/channels/`) et de jeu (`configs/games/`)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Aspect = Literal["16:9", "9:16", "1:1"]
Reframe = Literal["none", "blur_pad", "face_track", "action_track"]
ContentType = Literal["gaming", "tech_facts", "stream_bestof"]
Pacing = Literal["calm", "dynamic", "hyper"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OutputFormat(_Model):
    name: str
    aspect: Aspect
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    fps: int = Field(default=60, gt=0)
    reframe: Reframe = "none"
    max_duration_s: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _check_aspect(self) -> OutputFormat:
        w, h = (int(x) for x in self.aspect.split(":"))
        if abs(self.width / self.height - w / h) > 0.01:
            raise ValueError(f"{self.width}x{self.height} ne correspond pas à {self.aspect}")
        return self


class DuckingSettings(_Model):
    enabled: bool = True
    music_gain_db: float = Field(default=-14, le=0)
    attack_ms: int = Field(default=20, gt=0)
    release_ms: int = Field(default=300, gt=0)


class VadSettings(_Model):
    enabled: bool = True
    threshold: float = Field(default=0.5, ge=0, le=1)
    min_speech_ms: int = Field(default=250, ge=0)


class AudioSettings(_Model):
    music_dir: str | None = None
    beat_sync: bool = True
    loudness_lufs: float = Field(default=-14, le=0)
    ducking: DuckingSettings = Field(default_factory=DuckingSettings)
    vad: VadSettings = Field(default_factory=VadSettings)


class DetectorRef(_Model):
    name: str
    weight: float = Field(default=1.0, ge=0)
    params: dict[str, Any] = Field(default_factory=dict)


class LlmSettings(_Model):
    enabled: bool = True
    base_url: str = "http://localhost:11434/v1"
    model: str = "qwen3:30b-a3b"
    temperature: float = Field(default=0.2, ge=0, le=2)
    prompt: str  # nom d'un fichier de src/minavio/llm/prompts/ sans extension
    max_highlights: int = Field(default=10, gt=0)


class CaptionStyle(_Model):
    enabled: bool = True
    word_by_word: bool = True
    position: Literal["top", "center", "bottom"] = "bottom"
    style: Literal["pixel", "clean"] = "pixel"  # pixel = fonte arcade, pop rétro, contour en blocs
    max_words_per_line: int = Field(default=3, gt=0)


class MotionSettings(_Model):
    theme: dict[str, str] = Field(default_factory=dict)  # primary, accent, font...
    templates: list[str] = Field(default_factory=list)
    captions: CaptionStyle = Field(default_factory=CaptionStyle)


class EditingSettings(_Model):
    pacing: Pacing = "dynamic"
    target_duration_s: float = Field(gt=0)
    min_clip_s: float = Field(default=1.5, gt=0)
    max_clip_s: float = Field(default=15, gt=0)
    pre_roll_s: float = Field(default=1.0, ge=0)
    post_roll_s: float = Field(default=0.8, ge=0)

    @model_validator(mode="after")
    def _check_bounds(self) -> EditingSettings:
        if self.min_clip_s > self.max_clip_s:
            raise ValueError("min_clip_s doit être <= max_clip_s")
        return self


class ChannelProfile(_Model):
    id: str
    display_name: str
    content_type: ContentType
    description: str = ""
    games: list[str] = Field(default_factory=list)  # ids de configs/games/<id>/
    outputs: list[OutputFormat] = Field(min_length=1)
    detection: list[DetectorRef] = Field(min_length=1)
    audio: AudioSettings = Field(default_factory=AudioSettings)
    llm: LlmSettings
    motion: MotionSettings = Field(default_factory=MotionSettings)
    editing: EditingSettings


class UiTemplate(_Model):
    """Élément d'UI à repérer par template matching OpenCV."""

    name: str
    image: str  # relatif au dossier du jeu
    label: str  # label du Highlight produit
    roi: tuple[float, float, float, float] = (0, 0, 1, 1)  # x, y, w, h normalisés
    threshold: float = Field(default=0.8, ge=0, le=1)
    cooldown_s: float = Field(default=1.0, ge=0)


class GameProfile(_Model):
    id: str
    name: str
    ui_templates: list[UiTemplate] = Field(default_factory=list)
    combo_window_s: float = Field(default=6.0, gt=0)  # ex. multi-kills regroupés
