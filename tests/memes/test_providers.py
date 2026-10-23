from __future__ import annotations

import logging
from pathlib import Path

import httpx
import pytest

from minavio.config.schema import MemeSettings
from minavio.memes import providers
from minavio.memes.library import load_index
from minavio.memes.providers import _freesound_license, fetch

GIF_BYTES = b"GIF89a-test"
MP3_BYTES = b"ID3-test"
PNG_BYTES = b"\x89PNG-test"


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch, tmp_path: Path) -> None:
    """Pas de secrets réels : fichier de secrets absent, aucune clé d'API héritée."""
    monkeypatch.setenv("MINAVIO_SECRETS_FILE", str(tmp_path / "absent-secrets.env"))
    for name in (
        "TENOR_API_KEY",
        "GIPHY_API_KEY",
        "FREESOUND_API_KEY",
        "PIXABAY_API_KEY",
        "PEXELS_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


class Recorder:
    """Transport factice : enregistre chaque requête et répond selon l'hôte et le chemin."""

    def __init__(self, routes: dict[str, httpx.Response]) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = f"{request.url.host}{request.url.path}"
        if key in self.routes:
            return self.routes[key]
        return httpx.Response(404, json={"error": "unexpected"})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


def _json(payload: dict) -> httpx.Response:
    return httpx.Response(200, json=payload)


def _settings(tmp_path: Path) -> MemeSettings:
    return MemeSettings(library_dir=str(tmp_path / "lib"))


def test_tenor_sends_key_parses_and_downloads(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TENOR_API_KEY", "tenor-secret-value")
    rec = Recorder(
        {
            "tenor.googleapis.com/v2/search": _json(
                {
                    "results": [
                        {
                            "id": "111",
                            "itemurl": "https://tenor.com/view/111",
                            "content_description": "bruh moment",
                            "media_formats": {"gif": {"url": "https://media.test/111.gif"}},
                        }
                    ]
                }
            ),
            "media.test/111.gif": httpx.Response(200, content=GIF_BYTES),
        }
    )

    assets = fetch(
        "tenor", "bruh", "gif", 3, settings=_settings(tmp_path), transport=rec.transport()
    )

    search_req = rec.requests[0]
    assert search_req.url.params["key"] == "tenor-secret-value"
    assert search_req.url.params["q"] == "bruh"
    assert len(assets) == 1
    asset = assets[0]
    assert asset.id == "tenor:111" and asset.license == "api-terms" and asset.kind == "gif"
    assert asset.path.read_bytes() == GIF_BYTES
    assert "bruh" in asset.tags
    assert [a.id for a in load_index(tmp_path / "lib")] == ["tenor:111"]


def test_giphy_key_in_param(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GIPHY_API_KEY", "giphy-secret-value")
    rec = Recorder(
        {
            "api.giphy.com/v1/gifs/search": _json(
                {
                    "data": [
                        {
                            "id": "g1",
                            "title": "facepalm",
                            "username": "someone",
                            "url": "https://giphy.com/g1",
                            "images": {"original": {"url": "https://media.test/g1.gif"}},
                        }
                    ]
                }
            ),
            "media.test/g1.gif": httpx.Response(200, content=GIF_BYTES),
        }
    )

    assets = fetch(
        "giphy", "facepalm", "gif", settings=_settings(tmp_path), transport=rec.transport()
    )

    assert rec.requests[0].url.params["api_key"] == "giphy-secret-value"
    assert assets[0].license == "api-terms"
    assert assets[0].author == "someone"
    assert assets[0].path.read_bytes() == GIF_BYTES


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://creativecommons.org/publicdomain/zero/1.0/", "cc0"),
        ("https://creativecommons.org/licenses/by/4.0/", "cc-by-4.0"),
        ("https://creativecommons.org/licenses/by-nc/4.0/", "cc-by-nc"),
        ("", "unknown"),
    ],
)
def test_freesound_license_mapping(url: str, expected: str) -> None:
    assert _freesound_license(url) == expected


def test_freesound_token_header_and_restricted_license(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FREESOUND_API_KEY", "fs-secret-value")
    rec = Recorder(
        {
            "freesound.org/apiv2/search/text/": _json(
                {
                    "results": [
                        {
                            "id": 42,
                            "name": "bruh sound",
                            "username": "dj",
                            "license": "https://creativecommons.org/licenses/by-nc/4.0/",
                            "previews": {"preview-hq-mp3": "https://media.test/42.mp3"},
                        }
                    ]
                }
            ),
            "media.test/42.mp3": httpx.Response(200, content=MP3_BYTES),
        }
    )

    assets = fetch(
        "freesound", "bruh", "sound", settings=_settings(tmp_path), transport=rec.transport()
    )

    req = rec.requests[0]
    assert req.headers["Authorization"] == "Token fs-secret-value"
    assert req.url.params["fields"] == "id,name,previews,license,username"
    assert assets[0].license == "cc-by-nc"
    assert assets[0].path.read_bytes() == MP3_BYTES
    # cc-by-nc est restreint : la recherche de la banque ne le retourne pas par défaut.
    from minavio.memes.library import search

    assert search(["bruh"], "sound", _settings(tmp_path)) == []


def test_pixabay_key_and_license(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PIXABAY_API_KEY", "px-secret-value")
    rec = Recorder(
        {
            "pixabay.com/api/": _json(
                {
                    "hits": [
                        {
                            "id": 9,
                            "largeImageURL": "https://media.test/9.png",
                            "pageURL": "https://pixabay.com/p-9",
                            "user": "photog",
                            "tags": "cat, funny",
                        }
                    ]
                }
            ),
            "media.test/9.png": httpx.Response(200, content=PNG_BYTES),
        }
    )

    assets = fetch(
        "pixabay", "cat", "image", settings=_settings(tmp_path), transport=rec.transport()
    )

    assert rec.requests[0].url.params["key"] == "px-secret-value"
    assert assets[0].license == "pixabay"
    assert assets[0].path.suffix == ".png"


def test_pexels_authorization_header(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PEXELS_API_KEY", "pexels-secret-value")
    rec = Recorder(
        {
            "api.pexels.com/v1/search": _json(
                {
                    "photos": [
                        {
                            "id": 5,
                            "alt": "dog",
                            "photographer": "ana",
                            "url": "https://pexels.com/5",
                            "src": {"large": "https://media.test/5.jpg"},
                        }
                    ]
                }
            ),
            "media.test/5.jpg": httpx.Response(200, content=PNG_BYTES),
        }
    )

    assets = fetch(
        "pexels", "dog", "image", settings=_settings(tmp_path), transport=rec.transport()
    )

    assert rec.requests[0].headers["Authorization"] == "pexels-secret-value"
    assert assets[0].license == "pexels"
    assert assets[0].author == "ana"


def test_imgflip_needs_no_key_and_filters_by_name(tmp_path) -> None:
    rec = Recorder(
        {
            "api.imgflip.com/get_memes": _json(
                {
                    "success": True,
                    "data": {
                        "memes": [
                            {
                                "id": "1",
                                "name": "Distracted Boyfriend",
                                "url": "https://media.test/1.jpg",
                            },
                            {"id": "2", "name": "Drake Hotline", "url": "https://media.test/2.jpg"},
                        ]
                    },
                }
            ),
            "media.test/1.jpg": httpx.Response(200, content=PNG_BYTES),
        }
    )

    assets = fetch(
        "imgflip", "boyfriend", "image", settings=_settings(tmp_path), transport=rec.transport()
    )

    assert [a.id for a in assets] == ["imgflip:1"]
    assert assets[0].license == "api-terms"


def test_missing_key_skips_provider_without_request(tmp_path, caplog) -> None:
    rec = Recorder({})
    with caplog.at_level(logging.WARNING, logger="minavio.memes.providers"):
        assets = fetch(
            "tenor", "bruh", "gif", settings=_settings(tmp_path), transport=rec.transport()
        )

    assert assets == []
    assert rec.requests == []  # aucune requête sans clé
    assert "TENOR_API_KEY" in caplog.text


def test_key_value_never_logged(tmp_path, monkeypatch, caplog) -> None:
    monkeypatch.setenv("TENOR_API_KEY", "super-secret-key-123")
    rec = Recorder({"tenor.googleapis.com/v2/search": httpx.Response(500)})
    with caplog.at_level(logging.DEBUG):
        assets = fetch(
            "tenor", "bruh", "gif", settings=_settings(tmp_path), transport=rec.transport()
        )

    assert assets == []  # statut 500 : avertissement, pas d'exception
    assert "super-secret-key-123" not in caplog.text


def test_fetch_deduplicates_by_provider_and_id(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TENOR_API_KEY", "k")
    rec = Recorder(
        {
            "tenor.googleapis.com/v2/search": _json(
                {
                    "results": [
                        {"id": "7", "media_formats": {"gif": {"url": "https://media.test/7.gif"}}}
                    ]
                }
            ),
            "media.test/7.gif": httpx.Response(200, content=GIF_BYTES),
        }
    )
    settings = _settings(tmp_path)
    fetch("tenor", "x", "gif", settings=settings, transport=rec.transport())
    again = fetch("tenor", "x", "gif", settings=settings, transport=rec.transport())

    downloads = [r for r in rec.requests if r.url.host == "media.test"]
    assert len(downloads) == 1  # pas de re-téléchargement
    assert len(again) == 1
    assert len(load_index(tmp_path / "lib")) == 1


def test_unknown_provider_and_kind_mismatch(tmp_path) -> None:
    from minavio.errors import ConfigError

    with pytest.raises(ConfigError):
        fetch("nope", "x", settings=_settings(tmp_path))
    assert fetch("tenor", "x", "sound", settings=_settings(tmp_path)) == []


def test_provider_table_matches_spec() -> None:
    assert providers.PROVIDERS["tenor"].key_env == "TENOR_API_KEY"
    assert providers.PROVIDERS["imgflip"].key_env is None
    assert sorted(providers.PROVIDERS) == [
        "freesound",
        "giphy",
        "imgflip",
        "pexels",
        "pixabay",
        "tenor",
    ]
