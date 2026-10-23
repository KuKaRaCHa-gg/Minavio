"""Clients optionnels des banques de médias (Tenor, GIPHY, Freesound, Pixabay, Pexels, Imgflip).

Une clé absente vaut avertissement et fournisseur ignoré, jamais une exception. Les valeurs de
clés ne sont jamais journalisées, et les erreurs HTTP ne journalisent que le code de statut
(une URL peut contenir la clé en paramètre).
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from minavio.config.schema import MemeSettings
from minavio.config.secrets import load_secrets
from minavio.core.models import MemeAsset
from minavio.errors import ConfigError
from minavio.memes.library import (
    KINDS_BY_EXT,
    cache_dir,
    library_dir,
    load_index,
    save_index,
    tokens,
)

log = logging.getLogger(__name__)

TIMEOUT_S = 15.0
MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024
DEFAULT_SUFFIX = {"gif": ".gif", "image": ".png", "video": ".mp4", "sound": ".mp3"}


@dataclass(frozen=True)
class Remote:
    """Résultat d'une recherche distante, avant téléchargement."""

    remote_id: str
    kind: str
    download_url: str
    license: str
    author: str | None
    page_url: str | None
    title: str


SearchFn = Callable[[httpx.Client, str | None, str, str, int], list[Remote]]


@dataclass(frozen=True)
class ProviderSpec:
    key_env: str | None
    kinds: frozenset[str]
    search: SearchFn


def _get_json(
    client: httpx.Client,
    url: str,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> Any:
    resp = client.get(url, params=params, headers=headers)
    resp.raise_for_status()
    return resp.json()


def _freesound_license(url: str) -> str:
    lic = url.lower()
    if "publicdomain/zero" in lic:
        return "cc0"
    if "by-nc" in lic:
        return "cc-by-nc"  # usage commercial interdit : restreint
    if "/by/" in lic:
        return "cc-by-4.0"
    return "unknown"


def _tenor(
    client: httpx.Client, key: str | None, query: str, kind: str, limit: int
) -> list[Remote]:
    data = _get_json(
        client,
        "https://tenor.googleapis.com/v2/search",
        params={"q": query, "key": key, "limit": limit, "media_filter": "gif"},
    )
    out: list[Remote] = []
    for r in data.get("results", []):
        media = (r.get("media_formats") or {}).get("gif") or {}
        if media.get("url"):
            out.append(
                Remote(
                    remote_id=str(r.get("id")),
                    kind="gif",
                    download_url=media["url"],
                    license="api-terms",
                    author=None,
                    page_url=r.get("itemurl"),
                    title=r.get("content_description") or query,
                )
            )
    return out


def _giphy(
    client: httpx.Client, key: str | None, query: str, kind: str, limit: int
) -> list[Remote]:
    data = _get_json(
        client,
        "https://api.giphy.com/v1/gifs/search",
        params={"api_key": key, "q": query, "limit": limit, "rating": "g"},
    )
    out: list[Remote] = []
    for r in data.get("data", []):
        url = ((r.get("images") or {}).get("original") or {}).get("url")
        if url:
            out.append(
                Remote(
                    remote_id=str(r.get("id")),
                    kind="gif",
                    download_url=url,
                    license="api-terms",
                    author=r.get("username") or None,
                    page_url=r.get("url"),
                    title=r.get("title") or query,
                )
            )
    return out


def _freesound(
    client: httpx.Client, key: str | None, query: str, kind: str, limit: int
) -> list[Remote]:
    data = _get_json(
        client,
        "https://freesound.org/apiv2/search/text/",
        params={"query": query, "fields": "id,name,previews,license,username", "page_size": limit},
        headers={"Authorization": f"Token {key}"},
    )
    out: list[Remote] = []
    for r in data.get("results", []):
        preview = (r.get("previews") or {}).get("preview-hq-mp3")
        if preview:
            out.append(
                Remote(
                    remote_id=str(r.get("id")),
                    kind="sound",
                    download_url=preview,
                    license=_freesound_license(r.get("license") or ""),
                    author=r.get("username"),
                    page_url=f"https://freesound.org/s/{r.get('id')}/",
                    title=r.get("name") or query,
                )
            )
    return out


def _pixabay(
    client: httpx.Client, key: str | None, query: str, kind: str, limit: int
) -> list[Remote]:
    per_page = max(3, min(limit, 200))  # minimum imposé par l'API Pixabay
    if kind == "video":
        data = _get_json(
            client,
            "https://pixabay.com/api/videos/",
            params={"key": key, "q": query, "per_page": per_page},
        )
    else:
        data = _get_json(
            client,
            "https://pixabay.com/api/",
            params={"key": key, "q": query, "per_page": per_page},
        )
    out: list[Remote] = []
    for h in data.get("hits", []):
        if kind == "video":
            files = h.get("videos") or {}
            link = (files.get("medium") or files.get("small") or {}).get("url")
        else:
            link = h.get("largeImageURL") or h.get("webformatURL")
        if link:
            out.append(
                Remote(
                    remote_id=str(h.get("id")),
                    kind="video" if kind == "video" else "image",
                    download_url=link,
                    license="pixabay",
                    author=h.get("user"),
                    page_url=h.get("pageURL"),
                    title=h.get("tags") or query,
                )
            )
    return out


def _pexels(
    client: httpx.Client, key: str | None, query: str, kind: str, limit: int
) -> list[Remote]:
    headers = {"Authorization": key or ""}
    params = {"query": query, "per_page": limit}
    out: list[Remote] = []
    if kind == "video":
        data = _get_json(client, "https://api.pexels.com/videos/search", params, headers)
        for v in data.get("videos", []):
            files = [f for f in v.get("video_files", []) if f.get("file_type") == "video/mp4"]
            files.sort(key=lambda f: abs((f.get("width") or 0) - 1280))
            if files and files[0].get("link"):
                out.append(
                    Remote(
                        remote_id=str(v.get("id")),
                        kind="video",
                        download_url=files[0]["link"],
                        license="pexels",
                        author=(v.get("user") or {}).get("name"),
                        page_url=v.get("url"),
                        title=query,
                    )
                )
        return out
    data = _get_json(client, "https://api.pexels.com/v1/search", params, headers)
    for p in data.get("photos", []):
        src = p.get("src") or {}
        link = src.get("large") or src.get("original")
        if link:
            out.append(
                Remote(
                    remote_id=str(p.get("id")),
                    kind="image",
                    download_url=link,
                    license="pexels",
                    author=p.get("photographer"),
                    page_url=p.get("url"),
                    title=p.get("alt") or query,
                )
            )
    return out


def _imgflip(
    client: httpx.Client, key: str | None, query: str, kind: str, limit: int
) -> list[Remote]:
    data = _get_json(client, "https://api.imgflip.com/get_memes")
    if not data.get("success"):
        return []
    wanted = set(tokens(query))
    out: list[Remote] = []
    for m in data.get("data", {}).get("memes", []):
        name = m.get("name") or ""
        if wanted and not wanted & set(tokens(name)):
            continue  # l'API n'a pas de recherche : filtre côté client
        if m.get("url"):
            out.append(
                Remote(
                    remote_id=str(m.get("id")),
                    kind="image",
                    download_url=m["url"],
                    license="api-terms",
                    author=None,
                    page_url=None,
                    title=name,
                )
            )
    return out


PROVIDERS: dict[str, ProviderSpec] = {
    "tenor": ProviderSpec("TENOR_API_KEY", frozenset({"gif"}), _tenor),
    "giphy": ProviderSpec("GIPHY_API_KEY", frozenset({"gif"}), _giphy),
    "freesound": ProviderSpec("FREESOUND_API_KEY", frozenset({"sound"}), _freesound),
    "pixabay": ProviderSpec("PIXABAY_API_KEY", frozenset({"image", "video"}), _pixabay),
    "pexels": ProviderSpec("PEXELS_API_KEY", frozenset({"image", "video"}), _pexels),
    "imgflip": ProviderSpec(None, frozenset({"image"}), _imgflip),
}


def _unknown_provider(provider: str) -> str:
    return f"Fournisseur de memes inconnu : '{provider}' (choix : {', '.join(PROVIDERS)})."


def default_kind(provider: str) -> str:
    if provider not in PROVIDERS:
        raise ConfigError(_unknown_provider(provider))
    return sorted(PROVIDERS[provider].kinds)[0]


@contextmanager
def _httpx_quiet() -> Iterator[None]:
    """httpx journalise chaque URL en INFO, clé comprise : on réduit son niveau pendant un fetch."""
    logger = logging.getLogger("httpx")
    previous = logger.level
    logger.setLevel(logging.WARNING)
    try:
        yield
    finally:
        logger.setLevel(previous)


def _describe(exc: Exception) -> str:
    """Message sûr : code HTTP ou nom de l'exception, jamais l'URL (clé possible en paramètre)."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    return type(exc).__name__


def _download(client: httpx.Client, url: str, folder: Path, stem: str, kind: str) -> Path:
    resp = client.get(url)
    resp.raise_for_status()
    if len(resp.content) > MAX_DOWNLOAD_BYTES:
        raise ValueError("fichier trop volumineux")
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix not in KINDS_BY_EXT:
        suffix = DEFAULT_SUFFIX[kind]
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / (re.sub(r"[^A-Za-z0-9._-]", "_", stem) + suffix)
    dest.write_bytes(resp.content)
    return dest.resolve()


def fetch(
    provider: str,
    query: str,
    kind: str | None = None,
    limit: int = 5,
    *,
    settings: MemeSettings | None = None,
    root: Path | None = None,
    transport: httpx.BaseTransport | None = None,
) -> list[MemeAsset]:
    """Télécharge jusqu'à `limit` médias dans le cache et les ajoute à l'index.

    Dédoublonnage par `provider:id` : un média déjà présent n'est pas re-téléchargé.
    Une clé absente vaut avertissement et liste vide. Une erreur HTTP ou une réponse invalide
    d'une recherche aussi ; un téléchargement en échec est ignoré seul.
    """
    if provider not in PROVIDERS:
        raise ConfigError(_unknown_provider(provider))
    spec = PROVIDERS[provider]
    kind = kind or default_kind(provider)
    if kind not in spec.kinds:
        log.warning("%s ne fournit pas de '%s' (types : %s).", provider, kind, sorted(spec.kinds))
        return []
    key: str | None = None
    if spec.key_env:
        load_secrets()
        key = os.environ.get(spec.key_env)
        if not key:
            log.warning(
                "%s absent : fournisseur %s ignoré (`minavio memes set-key %s`).",
                spec.key_env,
                provider,
                spec.key_env,
            )
            return []
    root = root or library_dir(settings)
    assets = load_index(root)
    by_id = {a.id: a for a in assets}
    results: list[MemeAsset] = []
    changed = False
    with (
        _httpx_quiet(),
        httpx.Client(timeout=TIMEOUT_S, transport=transport, follow_redirects=True) as client,
    ):
        try:
            remotes = spec.search(client, key, query, kind, limit)
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("%s : recherche échouée (%s).", provider, _describe(exc))
            return []
        for remote in remotes[:limit]:
            asset_id = f"{provider}:{remote.remote_id}"
            existing = by_id.get(asset_id)
            if existing is not None and existing.path.exists():
                results.append(existing)
                continue
            try:
                path = _download(
                    client,
                    remote.download_url,
                    cache_dir(root) / provider,
                    remote.remote_id,
                    remote.kind,
                )
            except (httpx.HTTPError, ValueError, OSError) as exc:
                log.warning("%s : %s ignoré (%s).", provider, asset_id, _describe(exc))
                continue
            asset = MemeAsset(
                id=asset_id,
                kind=remote.kind,  # type: ignore[arg-type]
                path=path,
                source=provider,
                tags=list(dict.fromkeys(tokens(query) + tokens(remote.title))),
                license=remote.license,
                author=remote.author,
                url=remote.page_url,
            )
            assets.append(asset)
            by_id[asset_id] = asset
            results.append(asset)
            changed = True
    if changed:
        save_index(root, assets)
    return results
