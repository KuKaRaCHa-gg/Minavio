"""Schéma des profils de chaîne (`configs/channels/`) et de jeu (`configs/games/`)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Aspect = Literal["16:9", "9:16", "1:1"]
Reframe = Literal["none", "blur_pad", "face_track", "action_track", "split_facecam"]
Corner = Literal["auto", "bottom_left", "bottom_right", "top_left", "top_right"]
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


class SfxSettings(_Model):
    """Effets sonores locaux (CC0) posés automatiquement sur les événements du montage."""

    enabled: bool = False
    library_dir: str | None = None  # None = banque embarquée (minavio/core/assets/sfx)
    gain_db: float = Field(default=-6, le=12)
    on_cut: str | None = "whoosh"  # entre deux moments (pas aux coupes internes d'autocut)
    on_kill: str | None = "hit"
    on_multi_kill: str | None = "boom"
    on_cold_open: str | None = "rewind"


class AudioSettings(_Model):
    music_dir: str | None = None
    beat_sync: bool = True
    loudness_lufs: float = Field(default=-14, le=0)
    ducking: DuckingSettings = Field(default_factory=DuckingSettings)
    vad: VadSettings = Field(default_factory=VadSettings)
    sfx: SfxSettings = Field(default_factory=SfxSettings)


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


class AutocutSettings(_Model):
    """Suppression des creux : ne coupe que ce qui n'est ni de la voix ni de l'action."""

    enabled: bool = False
    min_silence_s: float = Field(default=0.5, gt=0)  # creux plus courts conservés
    margin_before_s: float = Field(default=0.12, ge=0)  # avant la reprise de la voix
    margin_after_s: float = Field(default=0.25, ge=0)  # après la fin de la voix
    energy_percentile: float = Field(default=20, ge=0, le=100)  # base du seuil adaptatif
    energy_margin_db: float = Field(default=6, ge=0)  # au-dessus de la base = action, gardé
    punch_in: float = Field(default=1.0, ge=1.0, le=1.3)  # zoom alterné aux jump-cuts


class ColdOpenSettings(_Model):
    """Ouvre la vidéo sur le pic du meilleur moment, puis effet rewind et montage normal."""

    enabled: bool = False
    duration_s: float = Field(default=2.5, gt=0.5, le=8)


class EditingSettings(_Model):
    autocut: AutocutSettings = Field(default_factory=AutocutSettings)
    cold_open: ColdOpenSettings = Field(default_factory=ColdOpenSettings)
    # chronological = ordre des sources ; countdown = du moins bon au meilleur (Top N).
    order: Literal["chronological", "countdown"] = "chronological"
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


class SmoothingSettings(_Model):
    """Caméra virtuelle 9:16 : One Euro filter + zone morte + vitesse de pan bornée."""

    min_cutoff: float = Field(default=0.3, gt=0)  # Hz ; plus bas = plus stable
    beta: float = Field(default=0.02, ge=0)  # réactivité aux mouvements rapides
    dead_zone: float = Field(default=0.04, ge=0, le=0.5)  # fraction de largeur ignorée
    max_pan_per_s: float = Field(default=0.35, gt=0)  # fraction de largeur par seconde
    reset_on_scene_cut: bool = True


class FacecamSettings(_Model):
    """`reframe: split_facecam` : facecam en bas, gameplay en haut (format clips de streamers)."""

    corner: Corner = "auto"  # auto = détection YuNet par clip (les créateurs repris varient)
    split_ratio: float = Field(default=0.35, gt=0.1, lt=0.6)  # part de hauteur pour la facecam
    fallback: Literal["blur_pad", "face_track", "action_track"] = "face_track"  # si aucune facecam


class FramingSettings(_Model):
    smoothing: SmoothingSettings = Field(default_factory=SmoothingSettings)
    facecam: FacecamSettings = Field(default_factory=FacecamSettings)


class ShortsSettings(_Model):
    """Shorts supplémentaires : un par meilleur moment, en plus de la compilation."""

    per_highlight: int = Field(default=0, ge=0, le=10)
    max_duration_s: float = Field(default=30, gt=5, le=60)
    format: str = "shorts"  # nom d'un `outputs[]` 9:16 du profil


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
    framing: FramingSettings = Field(default_factory=FramingSettings)
    shorts: ShortsSettings = Field(default_factory=ShortsSettings)
    chapters: bool = False  # chapitres YouTube dans out/<profil>/<job>_chapters.txt


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
