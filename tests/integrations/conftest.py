"""Environnement isolé pour les intégrations : aucune clé réelle, aucun secrets.env lu."""

from __future__ import annotations

import pytest

from minavio.integrations.keys_trends import TREND_KEYS


@pytest.fixture(autouse=True)
def _isolated_keys(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory):
    for name in TREND_KEYS:
        monkeypatch.delenv(name, raising=False)
    empty = tmp_path_factory.mktemp("secrets") / "secrets.env"
    monkeypatch.setenv("MINAVIO_SECRETS_FILE", str(empty))
