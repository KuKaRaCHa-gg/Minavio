"""Banque locale de memes : index JSON, scan du dossier utilisateur, recherche par tags.

Le dossier de la banque sert aussi de cache : les médias téléchargés vont dans `_cache/`, que le
scan ignore. Une licence hors de la liste sûre n'est retenue que si `allow_restricted_licenses`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path

from minavio.config.schema import MemeSettings
from minavio.config.secrets import config_root
from minavio.core.models import MemeAsset
from minavio.errors import ConfigError

log = logging.getLogger(__name__)

INDEX_NAME = "index.json"
CACHE_DIRNAME = "_cache"
SAFE_LICENSES = frozenset({"cc0", "pixabay", "pexels", "user"})
KINDS_BY_EXT = {
    ".gif": "gif",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".webp": "image",
    ".mp4": "video",
    ".webm": "video",
    ".mp3": "sound",
    ".wav": "sound",
    ".ogg": "sound",
}
# Noms de dossiers génériques : ils disent le type, pas le contenu (`sounds/bruh.mp3` -> bruh).
_GENERIC_TAGS = frozenset(
    {
        "sound",
        "sounds",
        "sfx",
        "gif",
        "gifs",
        "image",
        "images",
        "video",
        "videos",
        "meme",
        "memes",
        "media",
        "cache",
    }
)
_TOKEN_RE = re.compile(r"[a-z0-9]+")
MAX_PROMPT_TAGS = 200


def library_dir(settings: MemeSettings | None = None) -> Path:
    """`MemeSettings.library_dir` si défini, sinon <config>/minavio/memes (cf. config_root)."""
    if settings is not None and settings.library_dir:
        return Path(settings.library_dir).expanduser()
    return config_root() / "minavio" / "memes"


def cache_dir(root: Path) -> Path:
    return root / CACHE_DIRNAME


def index_path(root: Path) -> Path:
    return root / INDEX_NAME


def tokens(text: str) -> list[str]:
    """Mots minuscules distincts (2 caractères ou plus) : base des tags."""
    out: list[str] = []
    for tok in _TOKEN_RE.findall(text.lower()):
        if len(tok) >= 2 and tok not in out:
            out.append(tok)
    return out


def is_license_allowed(license: str, allow_restricted: bool) -> bool:
    """Licences sûres : cc0, cc-by* (sans NC), pixabay, pexels, user. Le reste exige l'opt-in."""
    lic = license.strip().lower()
    if allow_restricted or lic in SAFE_LICENSES:
        return True
    # cc-by-nc : usage commercial interdit, donc refusé (chaîne monétisée).
    return lic.startswith("cc-by") and "nc" not in lic.split("-")


def load_index(root: Path) -> list[MemeAsset]:
    path = index_path(root)
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return [MemeAsset.model_validate(item) for item in raw]
    except (
        OSError,
        ValueError,
    ) as e:  # JSON invalide ou ValidationError (sous-classe de ValueError)
        raise ConfigError(
            f"Index de memes illisible ({path}) : {e}. "
            "Supprime ce fichier puis relance `minavio memes scan`."
        ) from e


def save_index(root: Path, assets: list[MemeAsset]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / (INDEX_NAME + ".tmp")
    body = json.dumps([a.model_dump(mode="json") for a in assets], ensure_ascii=False, indent=2)
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(index_path(root))


def scan_user_dir(directory: Path) -> list[MemeAsset]:
    """Médias déposés à la main : kind selon l'extension, tags = dossiers + nom de fichier."""
    if not directory.is_dir():
        return []
    found: list[MemeAsset] = []
    for path in sorted(directory.rglob("*")):
        rel = path.relative_to(directory)
        if not path.is_file() or CACHE_DIRNAME in rel.parts:
            continue
        kind = KINDS_BY_EXT.get(path.suffix.lower())
        if kind is None:
            continue
        folder_tags = [t for part in rel.parts[:-1] for t in tokens(part) if t not in _GENERIC_TAGS]
        tags = list(dict.fromkeys(folder_tags + tokens(path.stem)))
        digest = hashlib.sha1(rel.as_posix().encode("utf-8")).hexdigest()[:12]
        found.append(
            MemeAsset(
                id=f"user:{digest}",
                kind=kind,
                path=path.resolve(),
                source="local",
                tags=tags,
                license="user",
            )
        )
    return found


def refresh_index(root: Path) -> list[MemeAsset]:
    """Réindexe les fichiers déposés dans `root` ; garde les médias téléchargés. Renvoie le scan."""
    kept = [a for a in load_index(root) if a.source != "local"]
    scanned = scan_user_dir(root)
    save_index(root, kept + scanned)
    return scanned


def search(query_tags: list[str], kind: str | None, settings: MemeSettings) -> list[MemeAsset]:
    """Médias dont les tags recoupent `query_tags`, triés par recouvrement décroissant.

    Sans `query_tags`, renvoie tous les médias (filtrés par kind et licence).
    """
    wanted = {tok for q in query_tags for tok in tokens(q)}
    scored: list[tuple[int, MemeAsset]] = []
    for asset in load_index(library_dir(settings)):
        if kind and asset.kind != kind:
            continue
        if not is_license_allowed(asset.license, settings.allow_restricted_licenses):
            continue
        if not asset.path.exists():
            continue
        overlap = len(wanted & set(asset.tags))
        if wanted and overlap == 0:
            continue
        scored.append((overlap, asset))
    scored.sort(key=lambda pair: (-pair[0], pair[1].id))
    return [asset for _, asset in scored]


def available_tags(settings: MemeSettings) -> list[str]:
    """Tags des médias utilisables (licence et fichier présents), pour le prompt du LLM."""
    tags: set[str] = set()
    for asset in load_index(library_dir(settings)):
        usable = is_license_allowed(asset.license, settings.allow_restricted_licenses)
        if usable and asset.path.exists():
            tags.update(asset.tags)
    return sorted(tags)[:MAX_PROMPT_TAGS]
