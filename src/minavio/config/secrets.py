"""Secrets locaux (clés d'API des fournisseurs LLM cloud).

Fichier `~/.config/minavio/secrets.env` (ou `%APPDATA%\\minavio\\secrets.env` sous Windows),
une ligne `CLE=valeur` par secret, permissions 600. `MINAVIO_SECRETS_FILE` change l'emplacement.
Une variable d'environnement déjà définie n'est jamais écrasée. Aucune valeur n'est journalisée.
"""

from __future__ import annotations

import contextlib
import os
import re
from pathlib import Path

from minavio.errors import ConfigError

KNOWN_KEYS = ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY", "GITHUB_TOKEN")
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_loaded = False


def secrets_path() -> Path:
    if override := os.environ.get("MINAVIO_SECRETS_FILE"):
        return Path(override).expanduser()
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "minavio" / "secrets.env"


def _parse(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as e:
        raise ConfigError(f"Fichier de secrets illisible ({path}) : {e}") from e
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name, value = name.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if _NAME_RE.match(name):
            values[name] = value
    return values


def _write(path: Path, values: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "# Secrets Minavio : ne pas partager ni versionner.\n"
    body += "".join(f"{k}={v}\n" for k, v in values.items())
    # Crée le fichier en 600 d'emblée (pas de fenêtre où il serait lisible par d'autres).
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(body)
    with contextlib.suppress(OSError):  # best effort (Windows)
        path.chmod(0o600)


def load_secrets(force: bool = False) -> list[str]:
    """Charge le fichier dans `os.environ` sans écraser l'existant ; une seule fois par processus.

    Retourne les NOMS des variables effectivement définies (jamais les valeurs).
    """
    global _loaded
    if _loaded and not force:
        return []
    _loaded = True
    try:
        values = _parse(secrets_path())
    except ConfigError:
        return []  # un fichier illisible ne doit pas bloquer les commandes sans clé
    added = []
    for name, value in values.items():
        if value and name not in os.environ:
            os.environ[name] = value
            added.append(name)
    return added


def set_secret(name: str, value: str) -> None:
    if not _NAME_RE.match(name):
        raise ConfigError(f"Nom de secret invalide : '{name}' (lettres, chiffres, _).")
    value = value.strip()
    if not value or "\n" in value or "\r" in value:
        raise ConfigError("Valeur vide ou multi-lignes : colle la clé sur une seule ligne.")
    path = secrets_path()
    values = _parse(path)
    values[name] = value
    _write(path, values)
    os.environ[name] = value


def delete_secret(name: str) -> bool:
    """Retire la clé du fichier (et de l'environnement si elle en venait). True si elle existait."""
    path = secrets_path()
    values = _parse(path)
    existed = name in values
    if existed:
        stored = values.pop(name)
        _write(path, values)
        if os.environ.get(name) == stored:
            del os.environ[name]
    return existed


def secret_status(names: tuple[str, ...] | list[str] = KNOWN_KEYS) -> dict[str, str]:
    """`env` (variable d'environnement), `fichier` ou `absent`, pour chaque nom."""
    try:
        stored = _parse(secrets_path())
    except ConfigError:
        stored = {}
    out = {}
    for n in names:
        if os.environ.get(n) and stored.get(n) != os.environ.get(n):
            out[n] = "env"
        elif stored.get(n):
            out[n] = "fichier"
        else:
            out[n] = "absent"
    return out


def mask(value: str) -> str:
    """`sk-…1234` au plus : préfixe court et 4 derniers caractères."""
    if len(value) <= 8:
        return "…"
    return f"{value[:3]}…{value[-4:]}"
