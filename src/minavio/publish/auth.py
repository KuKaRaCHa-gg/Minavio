"""OAuth « application installée » pour YouTube : client_secret.json + jeton local.

Fichiers (dossier de config de Minavio = celui du fichier de secrets) :
`youtube_client_secret.json` (fourni par l'utilisateur) et `youtube_token.json` (jeton + refresh
token), tous deux en permissions 600. Le jeton est rafraîchi automatiquement. Aucune valeur de
jeton n'est journalisée ni affichée. Les bibliothèques Google sont importées à l'usage (extra
`publish`).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from pathlib import Path
from typing import Any

from minavio.config.secrets import secrets_path
from minavio.errors import MinavioError

log = logging.getLogger("minavio.publish")

SCOPES = (
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",  # miniatures et playlists
)
CLIENT_SECRET_NAME = "youtube_client_secret.json"
TOKEN_NAME = "youtube_token.json"
SETUP_HINT = (
    "Crée un client OAuth « Application de bureau » dans Google Cloud (API YouTube Data v3 "
    "activée), télécharge son JSON, puis lance `minavio publish auth --client-secret <fichier>` "
    "(voir README.md, section Publication YouTube)."
)


def config_dir() -> Path:
    return secrets_path().parent


def client_secret_path() -> Path:
    return config_dir() / CLIENT_SECRET_NAME


def token_path() -> Path:
    return config_dir() / TOKEN_NAME


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # 600 dès la création
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    with contextlib.suppress(OSError):  # best effort (Windows)
        path.chmod(0o600)


def _import_google() -> tuple[Any, Any, Any]:
    """(Request, Credentials, InstalledAppFlow) ; point de monkeypatch des tests."""
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as e:
        raise MinavioError(
            f"Bibliothèques Google absentes ({e}). Installe-les : uv sync --extra publish"
        ) from e
    return Request, Credentials, InstalledAppFlow


def auth_status() -> dict[str, Any]:
    """État sans réseau : `state` = missing_client | not_connected | connected."""
    has_client = client_secret_path().is_file()
    has_token = token_path().is_file()
    state = "connected" if has_token else ("not_connected" if has_client else "missing_client")
    return {
        "state": state,
        "client_secret": has_client,
        "token": has_token,
        "config_dir": str(config_dir()),
    }


def status_text() -> str:
    st = auth_status()
    return {
        "connected": "connecté (jeton présent)",
        "not_connected": "client_secret.json présent, pas encore connecté : `minavio publish auth`",
        "missing_client": "non configuré : fournis ton client_secret.json (`minavio publish auth`)",
    }[st["state"]]


def install_client_secret(source: Path) -> Path:
    """Valide puis copie le client_secret.json dans le dossier de config (600)."""
    source = Path(source).expanduser()
    if not source.is_file():
        raise MinavioError(f"Fichier introuvable : {source}. {SETUP_HINT}")
    try:
        text = source.read_text(encoding="utf-8")
        data = json.loads(text)
    except (OSError, json.JSONDecodeError) as e:
        raise MinavioError(f"{source} n'est pas un JSON valide ({e}). {SETUP_HINT}") from e
    if not isinstance(data, dict) or "installed" not in data:
        kind = "web" if isinstance(data, dict) and "web" in data else "inconnu"
        raise MinavioError(
            f"{source} : client OAuth de type '{kind}'. Il faut un client « Application de "
            "bureau » (clé `installed`). Recrée-le dans Google Cloud > Identifiants."
        )
    dest = client_secret_path()
    if source.resolve() != dest.resolve():
        _write_private(dest, text)
    return dest


def authenticate(client_secret: Path | None = None) -> Path:
    """Flux navigateur local (scopes upload + youtube) ; écrit le jeton (600)."""
    if client_secret is not None:
        install_client_secret(client_secret)
    secret = client_secret_path()
    if not secret.is_file():
        raise MinavioError(f"Aucun client_secret.json dans {config_dir()}. {SETUP_HINT}")
    _, _, flow_cls = _import_google()
    flow = flow_cls.from_client_secrets_file(str(secret), list(SCOPES))
    creds = flow.run_local_server(port=0)
    _write_private(token_path(), creds.to_json())
    log.info("YouTube : connecté, jeton enregistré dans %s", token_path())
    return token_path()


def load_credentials() -> Any:
    """Identifiants valides (rafraîchis si besoin) ou erreur actionnable."""
    path = token_path()
    if not path.is_file():
        raise MinavioError("YouTube non connecté. Lance `minavio publish auth`. " + SETUP_HINT)
    request_cls, creds_cls, _ = _import_google()
    try:
        creds = creds_cls.from_authorized_user_file(str(path), list(SCOPES))
    except (ValueError, OSError, KeyError) as e:
        raise MinavioError(
            f"Jeton YouTube illisible ({path}) : relance `minavio publish auth`."
        ) from e
    if not getattr(creds, "valid", False):
        if getattr(creds, "refresh_token", None):
            try:
                creds.refresh(request_cls())
            except Exception as e:  # invalid_grant, réseau… (jamais la valeur du jeton)
                raise MinavioError(
                    f"Rafraîchissement du jeton YouTube refusé ({type(e).__name__}). "
                    "Relance `minavio publish auth`."
                ) from e
            _write_private(path, creds.to_json())
        else:
            raise MinavioError("Jeton YouTube expiré sans refresh token : `minavio publish auth`.")
    return creds


def forget() -> bool:
    """Supprime le jeton local (déconnexion). True s'il existait."""
    path = token_path()
    if path.exists():
        path.unlink()
        return True
    return False
