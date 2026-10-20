import pytest

from minavio.config import secrets


@pytest.fixture(autouse=True)
def isolated_secrets(tmp_path, monkeypatch):
    """Jamais le vrai fichier de secrets de l'utilisateur dans les tests."""
    monkeypatch.setenv("MINAVIO_SECRETS_FILE", str(tmp_path / "secrets.env"))
    for k in secrets.KNOWN_KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(secrets, "_loaded", False)
