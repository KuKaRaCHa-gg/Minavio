"""Préréglages : thèmes, création de profils (`create_preset`, `profiles new`, `preset create`)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

import minavio.config as config_pkg
from minavio.cli import app
from minavio.config import CONFIG_DIR, load_profile
from minavio.config.presets import THEMES, UserPreset, apply_theme, create_preset
from minavio.config.schema import ChannelProfile
from minavio.errors import ConfigError

REAL_CHANNELS = ["bytetricks", "minivitch", "minivitch_top10", "thelostchat"]


@pytest.fixture
def cfg(tmp_path, monkeypatch) -> Path:
    """Copie temporaire de `configs/` ; `minavio.config.CONFIG_DIR` pointe dessus."""
    root = tmp_path / "configs"
    shutil.copytree(CONFIG_DIR, root)
    monkeypatch.setattr(config_pkg, "CONFIG_DIR", root)
    return root


def _raw(profile_id: str) -> dict:
    return yaml.safe_load((CONFIG_DIR / "channels" / f"{profile_id}.yaml").read_text("utf-8"))


def _without_motion(data: dict, theme_preset: str | None) -> ChannelProfile:
    """Profil sans bloc `motion` (donc sans captions ni theme explicites)."""
    data = {k: v for k, v in data.items() if k != "motion"}
    data["theme_preset"] = theme_preset
    return ChannelProfile.model_validate(data)


def test_user_preset_is_channel_profile():
    assert UserPreset is ChannelProfile


@pytest.mark.parametrize("name", sorted(THEMES))
def test_each_theme_applies_without_explicit_captions(name):
    profile = _without_motion(_raw("bytetricks"), name)
    themed = apply_theme(profile)
    expected = THEMES[name]
    assert themed.motion.captions.style == expected["style"]
    assert themed.motion.captions.position == expected["position"]
    assert themed.motion.theme == expected["theme"]


def test_theme_keys_are_the_ones_consumed_by_code():
    allowed_caption_keys = {"style", "position"}
    for name, preset in THEMES.items():
        assert preset["style"] in {"pixel", "clean"}, name
        assert set(preset) <= allowed_caption_keys | {"theme"}, name
        assert set(preset["theme"]) <= {"primary", "accent", "font"}, name


def test_no_theme_preset_leaves_profile_untouched():
    profile = _without_motion(_raw("bytetricks"), None)
    assert apply_theme(profile) is profile


def test_explicit_caption_value_wins_over_theme():
    data = _raw("bytetricks")
    data["motion"] = {"captions": {"style": "clean"}}
    data["theme_preset"] = "retro_pixel"
    themed = apply_theme(ChannelProfile.model_validate(data))
    assert themed.motion.captions.style == "clean"  # écrit en YAML -> gagne
    assert themed.motion.captions.position == THEMES["retro_pixel"]["position"]


def test_explicit_theme_key_wins_over_theme():
    data = _raw("bytetricks")
    data["motion"] = {"theme": {"primary": "#123456"}}
    data["theme_preset"] = "streamer_vibrant"
    themed = apply_theme(ChannelProfile.model_validate(data))
    assert themed.motion.theme["primary"] == "#123456"
    assert themed.motion.theme["accent"] == THEMES["streamer_vibrant"]["theme"]["accent"]


@pytest.mark.parametrize("pid", REAL_CHANNELS)
def test_existing_profile_captions_unchanged(pid):
    """Les captions et couleurs des profils existants ne bougent pas avec `theme_preset`."""
    raw = ChannelProfile.model_validate(_raw(pid))  # sans apply_theme
    loaded = load_profile(pid)
    assert loaded.motion.captions == raw.motion.captions
    assert loaded.motion.theme == raw.motion.theme


def test_minivitch_rendering_values_are_pinned():
    cap = load_profile("minivitch").motion.captions
    assert (cap.enabled, cap.word_by_word, cap.position, cap.style) == (
        True,
        True,
        "bottom",
        "pixel",
    )
    assert load_profile("minivitch").motion.theme == {
        "primary": "#FF3B30",
        "accent": "#FFD60A",
        "font": "Press Start 2P",
    }


def test_thelostchat_keeps_center_position():
    assert load_profile("thelostchat").motion.captions.position == "center"


def test_default_vlog_validates():
    profile = load_profile("default_vlog")
    assert profile.theme_preset == "neutral"
    assert profile.content_type == "stream_bestof"
    assert {d.name for d in profile.detection} <= {"speech", "scene_change", "hype"}
    assert profile.games == []
    assert profile.motion.captions.style == THEMES["neutral"]["style"]


def test_create_preset_ok(cfg):
    path = create_preset("ma_chaine", config_dir=cfg, display_name="Ma chaîne")
    assert path == cfg / "channels" / "ma_chaine.yaml"
    profile = load_profile("ma_chaine", cfg)
    assert profile.id == "ma_chaine"
    assert profile.display_name == "Ma chaîne"
    assert profile.theme_preset == "neutral"


def test_create_preset_keeps_base_comments(cfg):
    path = create_preset("ma_chaine", config_dir=cfg)
    assert "# Profil générique" in path.read_text("utf-8")


def test_create_preset_duplicate_refused(cfg):
    create_preset("ma_chaine", config_dir=cfg)
    with pytest.raises(ConfigError, match="existe déjà"):
        create_preset("ma_chaine", config_dir=cfg)


def test_create_preset_duplicate_of_real_profile_refused(cfg):
    with pytest.raises(ConfigError, match="existe déjà"):
        create_preset("bytetricks", config_dir=cfg)


@pytest.mark.parametrize("bad", ["Ma-Chaine", "ma chaine", "Été", "", "../x"])
def test_create_preset_invalid_id_refused(cfg, bad):
    with pytest.raises(ConfigError, match="invalide"):
        create_preset(bad, config_dir=cfg)
    assert not any(p.name.startswith("..") for p in (cfg / "channels").iterdir())


def test_create_preset_unknown_base_refused(cfg):
    with pytest.raises(ConfigError, match="Base 'inconnue' introuvable"):
        create_preset("ma_chaine", base="inconnue", config_dir=cfg)


def test_create_preset_removes_file_if_validation_fails(cfg):
    (cfg / "channels" / "casse.yaml").write_text(
        "id: casse\ndisplay_name: X\ncontent_type: gaming\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError):
        create_preset("nouveau", base="casse", config_dir=cfg)
    assert not (cfg / "channels" / "nouveau.yaml").exists()


def test_profiles_new_cli(cfg):
    result = CliRunner().invoke(app, ["profiles", "new", "cli_chaine", "--name", "Chaîne CLI"])
    assert result.exit_code == 0, result.output
    assert str(cfg / "channels" / "cli_chaine.yaml") in result.output
    assert "profiles validate" in result.output
    assert load_profile("cli_chaine", cfg).display_name == "Chaîne CLI"


def test_preset_create_alias_cli(cfg):
    result = CliRunner().invoke(app, ["preset", "create", "alias_chaine", "--from", "bytetricks"])
    assert result.exit_code == 0, result.output
    assert load_profile("alias_chaine", cfg).id == "alias_chaine"


def test_profiles_new_cli_duplicate_exits_nonzero(cfg):
    runner = CliRunner()
    assert runner.invoke(app, ["profiles", "new", "dup"]).exit_code == 0
    result = runner.invoke(app, ["profiles", "new", "dup"])
    assert result.exit_code != 0
    assert "existe déjà" in result.output


def test_profiles_new_cli_invalid_id_exits_nonzero(cfg):
    result = CliRunner().invoke(app, ["profiles", "new", "Mauvais-Id"])
    assert result.exit_code != 0
    assert "invalide" in result.output
