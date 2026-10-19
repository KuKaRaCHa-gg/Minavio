"""Prompts système versionnés (un fichier .md par `content_type`)."""

from __future__ import annotations

import re
from importlib import resources

from minavio.errors import ConfigError

_SECTION_RE = re.compile(r"^<!--\s*section:(\w+)\s*-->\s*$", re.MULTILINE)


def load_prompt(name: str, section: str | None = None) -> str:
    """Charge `prompts/<name>.md`.

    Le fichier est découpé par des marqueurs `<!-- section:common|rank|copy -->`. Sans `section`,
    le fichier entier est renvoyé ; avec, la section `common` suivie de la section demandée.
    """
    try:
        text = (resources.files(__package__) / f"{name}.md").read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(
            f"Prompt LLM '{name}' introuvable dans src/minavio/llm/prompts/ "
            "(vérifier `llm.prompt` dans le profil de chaîne)."
        ) from exc
    if section is None:
        return text
    parts = _SECTION_RE.split(text)
    sections = dict(zip(parts[1::2], parts[2::2], strict=False))
    if section not in sections:
        raise ConfigError(f"Le prompt '{name}' n'a pas de section '{section}'.")
    return (sections.get("common", "").strip() + "\n\n" + sections[section].strip()).strip()
