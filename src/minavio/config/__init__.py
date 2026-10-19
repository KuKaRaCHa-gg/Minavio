"""Chargement et validation des profils YAML."""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import ValidationError

from minavio.config.schema import ChannelProfile, GameProfile
from minavio.errors import ConfigError

CONFIG_DIR = Path(os.environ.get("MINAVIO_CONFIG_DIR", Path(__file__).parents[3] / "configs"))


def _read_yaml(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise ConfigError(f"Fichier introuvable : {path}") from e
    except yaml.YAMLError as e:
        raise ConfigError(f"YAML invalide dans {path} : {e}") from e
    if not isinstance(data, dict):
        raise ConfigError(f"{path} doit contenir un mapping YAML")
    return data


def list_profiles(config_dir: Path = CONFIG_DIR) -> list[str]:
    return sorted(p.stem for p in (config_dir / "channels").glob("*.yaml"))


def load_game(game_id: str, config_dir: Path = CONFIG_DIR) -> GameProfile:
    path = config_dir / "games" / game_id / "game.yaml"
    try:
        return GameProfile.model_validate(_read_yaml(path))
    except ValidationError as e:
        raise ConfigError(f"Jeu '{game_id}' invalide ({path}) :\n{e}") from e


def load_profile(profile_id: str, config_dir: Path = CONFIG_DIR) -> ChannelProfile:
    path = config_dir / "channels" / f"{profile_id}.yaml"
    if not path.exists():
        known = ", ".join(list_profiles(config_dir)) or "aucun"
        raise ConfigError(f"Profil '{profile_id}' introuvable ({path}). Disponibles : {known}")
    data = _read_yaml(path)
    llm = data.get("llm")
    if isinstance(llm, dict):
        if base_url := os.environ.get("MINAVIO_LLM_BASE_URL"):
            llm["base_url"] = base_url
        if model := os.environ.get("MINAVIO_LLM_MODEL"):
            llm["model"] = model
    try:
        profile = ChannelProfile.model_validate(data)
    except ValidationError as e:
        raise ConfigError(f"Profil '{profile_id}' invalide ({path}) :\n{e}") from e
    if profile.id != profile_id:
        raise ConfigError(f"{path} : id '{profile.id}' ≠ nom de fichier '{profile_id}'")
    for game_id in profile.games:
        load_game(game_id, config_dir)
    return profile
