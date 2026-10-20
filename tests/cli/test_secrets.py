import os
import stat

import pytest

from minavio.config import load_profile, secrets
from minavio.errors import ConfigError


def test_set_then_status_and_permissions():
    secrets.set_secret("ANTHROPIC_API_KEY", "sk-ant-1234567890abcd")
    path = secrets.secrets_path()
    assert "ANTHROPIC_API_KEY=sk-ant-1234567890abcd" in path.read_text()
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    st = secrets.secret_status(["ANTHROPIC_API_KEY", "GEMINI_API_KEY"])
    assert st == {"ANTHROPIC_API_KEY": "fichier", "GEMINI_API_KEY": "absent"}


def test_load_does_not_override_env(monkeypatch):
    secrets.secrets_path().write_text("GEMINI_API_KEY=from-file\nOPENAI_API_KEY=file-openai\n")
    monkeypatch.setenv("GEMINI_API_KEY", "from-env")
    added = secrets.load_secrets(force=True)
    assert os.environ["GEMINI_API_KEY"] == "from-env"
    assert os.environ["OPENAI_API_KEY"] == "file-openai"
    assert added == ["OPENAI_API_KEY"]
    assert secrets.secret_status(["GEMINI_API_KEY"])["GEMINI_API_KEY"] == "env"


def test_load_profile_loads_secrets_once():
    secrets.secrets_path().write_text("OPENAI_API_KEY=abc\n")
    load_profile("minivitch")
    assert os.environ["OPENAI_API_KEY"] == "abc"
    secrets.secrets_path().write_text("OPENAI_API_KEY=changed\n")
    load_profile("minivitch")  # déjà chargé : pas de relecture
    assert os.environ["OPENAI_API_KEY"] == "abc"


def test_delete_secret():
    secrets.set_secret("GITHUB_TOKEN", "ghp_xxxxxxxxxxxx")
    assert secrets.delete_secret("GITHUB_TOKEN") is True
    assert "GITHUB_TOKEN" not in os.environ
    assert secrets.delete_secret("GITHUB_TOKEN") is False
    assert secrets.secret_status(["GITHUB_TOKEN"])["GITHUB_TOKEN"] == "absent"


def test_mask_never_reveals_value():
    m = secrets.mask("sk-ant-api03-SECRETSECRETSECRET1234")
    assert m == "sk-…1234"
    assert secrets.mask("short") == "…"


@pytest.mark.parametrize(("name", "value"), [("bad name", "x"), ("OK", ""), ("OK", "a\nb")])
def test_set_secret_rejects_invalid(name, value):
    with pytest.raises(ConfigError):
        secrets.set_secret(name, value)
