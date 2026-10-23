from __future__ import annotations

from pathlib import Path

import pytest

from minavio.config.schema import MemeSettings
from minavio.core.models import MemeAsset
from minavio.memes.library import (
    is_license_allowed,
    library_dir,
    load_index,
    refresh_index,
    save_index,
    scan_user_dir,
    search,
)


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    return path


def _asset(root: Path, name: str, kind: str, tags: list[str], license: str) -> MemeAsset:
    return MemeAsset(
        id=name,
        kind=kind,  # type: ignore[arg-type]
        path=_touch(root / "cache" / name),
        source="tenor",
        tags=tags,
        license=license,
    )


def test_scan_builds_tags_from_file_name_and_subfolders(tmp_path: Path) -> None:
    _touch(tmp_path / "sounds" / "bruh.mp3")
    _touch(tmp_path / "gifs" / "facepalm_guy.gif")
    _touch(tmp_path / "Hit-Boom.wav")
    _touch(tmp_path / "pic.PNG")
    _touch(tmp_path / "clip.webm")
    _touch(tmp_path / "notes.txt")
    _touch(tmp_path / "_cache" / "ignored.mp3")

    found = {a.path.name: a for a in scan_user_dir(tmp_path)}

    assert set(found) == {"bruh.mp3", "facepalm_guy.gif", "Hit-Boom.wav", "pic.PNG", "clip.webm"}
    assert found["bruh.mp3"].tags == ["bruh"]  # le dossier « sounds » est générique
    assert found["bruh.mp3"].kind == "sound"
    assert found["facepalm_guy.gif"].tags == ["facepalm", "guy"]
    assert found["facepalm_guy.gif"].kind == "gif"
    assert found["Hit-Boom.wav"].tags == ["hit", "boom"]
    assert found["pic.PNG"].kind == "image"
    assert found["clip.webm"].kind == "video"
    assert all(a.license == "user" and a.source == "local" for a in found.values())


def test_refresh_index_keeps_downloads_and_replaces_local_entries(tmp_path: Path) -> None:
    _touch(tmp_path / "bruh.mp3")
    downloaded = _asset(tmp_path, "pixabay:1", "image", ["cat"], "pixabay")
    save_index(tmp_path, [downloaded])

    refresh_index(tmp_path)
    refresh_index(tmp_path)  # idempotent : pas de doublon « local »

    ids = sorted(a.id for a in load_index(tmp_path))
    assert ids[0] == "pixabay:1"
    assert len(ids) == 2


@pytest.mark.parametrize(
    ("license", "allowed"),
    [
        ("cc0", True),
        ("cc-by-4.0", True),
        ("cc-by-sa-4.0", True),
        ("pixabay", True),
        ("pexels", True),
        ("user", True),
        ("cc-by-nc-4.0", False),
        ("api-terms", False),
        ("unknown", False),
    ],
)
def test_license_filter_default(license: str, allowed: bool) -> None:
    assert is_license_allowed(license, allow_restricted=False) is allowed


def test_license_opt_in_allows_restricted() -> None:
    assert is_license_allowed("api-terms", allow_restricted=True) is True


def test_search_filters_licenses_and_ranks_by_tag_overlap(tmp_path: Path) -> None:
    settings = MemeSettings(library_dir=str(tmp_path))
    save_index(
        tmp_path,
        [
            _asset(tmp_path, "a", "sound", ["bruh"], "cc0"),
            _asset(tmp_path, "b", "sound", ["bruh", "hit"], "cc-by-4.0"),
            _asset(tmp_path, "c", "sound", ["hit", "boom"], "api-terms"),  # restreint
            _asset(tmp_path, "d", "gif", ["hit"], "pexels"),
        ],
    )

    sounds = search(["hit", "bruh"], "sound", settings)
    assert [a.id for a in sounds] == ["b", "a"]  # « c » exclu par la licence

    opted_in = search(
        ["boom"], "sound", settings.model_copy(update={"allow_restricted_licenses": True})
    )
    assert [a.id for a in opted_in] == ["c"]

    assert search(["hit"], "video", settings) == []


def test_library_dir_uses_settings_then_default(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    assert library_dir(MemeSettings(library_dir=str(tmp_path / "mine"))) == tmp_path / "mine"
    assert library_dir(MemeSettings()) == tmp_path / "cfg" / "minavio" / "memes"
