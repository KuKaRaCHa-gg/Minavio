"""Préréglages utilisateur : thèmes visuels et création de profils à partir d'une base.

Un préréglage utilisateur EST un profil de chaîne (`configs/channels/<id>.yaml`) :
`UserPreset` est un alias documenté de `ChannelProfile`, sans second modèle concurrent.

`theme_preset` choisit un thème nommé ; les valeurs du thème ne remplissent que les champs que
le YAML n'a pas écrits explicitement (`model_fields_set`). Une valeur explicite gagne toujours.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from minavio import config as _config
from minavio.config.schema import CaptionStyle, ChannelProfile, MotionSettings
from minavio.errors import ConfigError

UserPreset = ChannelProfile

# Clés de `CaptionStyle` qu'un thème peut fournir (le reste reste sous contrôle du YAML).
_CAPTION_THEME_KEYS = ("style", "position")

THEMES: dict[str, dict[str, Any]] = {
    "retro_pixel": {
        "style": "pixel",
        "position": "bottom",
        "theme": {"primary": "#FF3B30", "accent": "#FFD60A", "font": "Press Start 2P"},
    },
    "tech_minimal": {
        "style": "clean",
        "position": "bottom",
        "theme": {"primary": "#0A84FF", "accent": "#30D158", "font": "Inter"},
    },
    "streamer_vibrant": {
        "style": "pixel",
        "position": "center",
        "theme": {"primary": "#BF5AF2", "accent": "#FF9F0A", "font": "Press Start 2P"},
    },
    "neutral": {
        "style": "clean",
        "position": "bottom",
        "theme": {"primary": "#1C1C1E", "accent": "#FFFFFF", "font": "Inter"},
    },
}

_ID_RE = re.compile(r"[a-z0-9_]+")


def apply_theme(profile: ChannelProfile) -> ChannelProfile:
    """Renvoie le profil avec le thème `theme_preset` appliqué aux champs non écrits en YAML.

    Sans `theme_preset`, le profil est renvoyé tel quel.
    """
    if profile.theme_preset is None:
        return profile
    preset = THEMES[profile.theme_preset]

    explicit_captions = profile.motion.captions.model_dump(exclude_unset=True)
    caption_data = {k: preset[k] for k in _CAPTION_THEME_KEYS if k in preset}
    caption_data.update(explicit_captions)  # l'explicite gagne
    captions = CaptionStyle.model_validate(caption_data)

    # Clés de couleur/fonte : celles écrites dans le YAML gagnent, les autres viennent du thème.
    theme = {**preset.get("theme", {}), **profile.motion.theme}
    motion = MotionSettings.model_validate(
        {
            "theme": theme,
            "templates": profile.motion.templates,
            "captions": captions,
        }
    )
    return profile.model_copy(update={"motion": motion})


def create_preset(
    new_id: str,
    base: str = "default_vlog",
    config_dir: Path | None = None,
    *,
    display_name: str | None = None,
) -> Path:
    """Copie `configs/channels/<base>.yaml` vers `<new_id>.yaml` et valide le résultat.

    `config_dir` None = `minavio.config.CONFIG_DIR` au moment de l'appel.
    Lève `ConfigError` si l'id est invalide, si le fichier existe déjà ou si la base est absente.
    """
    from minavio.config import list_profiles, load_profile

    root = config_dir if config_dir is not None else _config.CONFIG_DIR
    if not _ID_RE.fullmatch(new_id):
        raise ConfigError(
            f"Identifiant de profil invalide : '{new_id}'. "
            "Utiliser uniquement des minuscules, chiffres et '_' (ex. ma_chaine)."
        )
    channels = root / "channels"
    dst = channels / f"{new_id}.yaml"
    if dst.exists():
        raise ConfigError(
            f"Le profil '{new_id}' existe déjà ({dst}). "
            "Choisir un autre identifiant, ou éditer ce fichier directement."
        )
    src = channels / f"{base}.yaml"
    if not src.exists():
        known = ", ".join(list_profiles(root)) or "aucun"
        raise ConfigError(f"Base '{base}' introuvable ({src}). Disponibles : {known}")

    text = src.read_text(encoding="utf-8")
    # Remplacement ligne à ligne : les commentaires du YAML de base sont conservés.
    text, n_id = re.subn(r"^id:.*$", f"id: {new_id}", text, count=1, flags=re.MULTILINE)
    if n_id == 0:
        raise ConfigError(f"{src} : ligne 'id:' introuvable, base non utilisable.")
    if display_name is not None:
        # JSON est un YAML valide : guillemets et caractères accentués sûrs.
        text, _ = re.subn(
            r"^display_name:.*$",
            f"display_name: {json.dumps(display_name, ensure_ascii=False)}",
            text,
            count=1,
            flags=re.MULTILINE,
        )
    dst.write_text(text, encoding="utf-8")
    try:
        load_profile(new_id, root)
    except ConfigError:
        dst.unlink(missing_ok=True)  # pas de profil cassé laissé derrière
        raise
    return dst
