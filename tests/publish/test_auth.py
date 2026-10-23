from __future__ import annotations

import json
import os
import stat
from types import SimpleNamespace

import pytest

from minavio.errors import MinavioError
from minavio.publish import auth


class FakeCreds:
    def __init__(self, valid=True, refresh_token="rt"):
        self.valid, self.refresh_token, self.refreshed = valid, refresh_token, False

    def refresh(self, request):
        self.valid, self.refreshed = True, True

    def to_json(self):
        return json.dumps({"token": "SECRET-ACCESS", "refresh_token": "SECRET-REFRESH"})


class FakeFlow:
    scopes = None

    @classmethod
    def from_client_secrets_file(cls, path, scopes):
        cls.scopes = scopes
        return cls()

    def run_local_server(self, port=0):
        return FakeCreds()


def fake_google(creds):
    class Creds:
        @staticmethod
        def from_authorized_user_file(path, scopes=None):
            return creds

    return lambda: (lambda: SimpleNamespace(), Creds, FakeFlow)


def secret_file(tmp_path, kind="installed"):
    p = tmp_path / "dl" / "client_secret_123.json"
    p.parent.mkdir()
    p.write_text(json.dumps({kind: {"client_id": "x", "client_secret": "y"}}))
    return p


def mode(p):
    return stat.S_IMODE(os.stat(p).st_mode)


def test_install_client_secret_copies_with_600(tmp_path):
    dest = auth.install_client_secret(secret_file(tmp_path))
    assert dest == auth.client_secret_path() and dest.is_file()
    if os.name != "nt":
        assert mode(dest) == 0o600
    assert auth.auth_status()["state"] == "not_connected"


def test_web_client_is_rejected(tmp_path):
    with pytest.raises(MinavioError, match="Application de bureau"):
        auth.install_client_secret(secret_file(tmp_path, "web"))
    with pytest.raises(MinavioError, match="introuvable"):
        auth.install_client_secret(tmp_path / "absent.json")


def test_authenticate_writes_private_token(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "_import_google", fake_google(FakeCreds()))
    path = auth.authenticate(secret_file(tmp_path))
    assert path == auth.token_path() and path.is_file()
    if os.name != "nt":
        assert mode(path) == 0o600
    assert set(FakeFlow.scopes) == set(auth.SCOPES)
    assert auth.auth_status()["state"] == "connected"


def test_authenticate_without_secret_explains(monkeypatch):
    monkeypatch.setattr(auth, "_import_google", fake_google(FakeCreds()))
    with pytest.raises(MinavioError, match=r"client_secret\.json"):
        auth.authenticate()


def test_load_credentials_refreshes_and_rewrites(tmp_path, monkeypatch):
    creds = FakeCreds(valid=False)
    monkeypatch.setattr(auth, "_import_google", fake_google(creds))
    with pytest.raises(MinavioError, match="non connecté"):
        auth.load_credentials()
    auth._write_private(auth.token_path(), "{}")
    assert auth.load_credentials() is creds and creds.refreshed
    assert "SECRET-ACCESS" in auth.token_path().read_text()  # jeton rafraîchi sauvegardé


def test_expired_without_refresh_token(monkeypatch):
    monkeypatch.setattr(auth, "_import_google", fake_google(FakeCreds(False, None)))
    auth._write_private(auth.token_path(), "{}")
    with pytest.raises(MinavioError, match="publish auth"):
        auth.load_credentials()


def test_status_and_forget_never_print_token(capsys):
    auth._write_private(auth.token_path(), '{"token": "SECRET-ACCESS"}')
    assert "SECRET" not in auth.status_text()
    assert auth.forget() and not auth.forget()
