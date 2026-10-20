"""Services de l'onglet « Publication » (YouTube). Aucune dépendance Gradio, testables seuls.

Rien n'est publié sans `confirm=True` ; la prévisualisation (dry-run) n'ouvre aucune connexion.
"""

from __future__ import annotations

import re
from pathlib import Path

from minavio.config import load_profile
from minavio.errors import MinavioError

PUBLISH_VIDEOS = ["youtube", "shorts", "all"]
PUBLISH_WARNING = (
    "⚠️ La publication est **toujours manuelle** : rien n'est envoyé tant que tu n'as pas coché "
    "la confirmation. Par défaut la vidéo est *privée* ; vérifie-la dans YouTube Studio."
)


def auth_markdown() -> str:
    from minavio.publish import auth

    st = auth.auth_status()
    icon = {"connected": "✅", "not_connected": "🟠", "missing_client": "⚪"}[st["state"]]
    return f"{icon} YouTube : {auth.status_text()}  \nDossier de config : `{st['config_dir']}`"


def connect_youtube(client_secret_file: str | None = None) -> str:
    """Copie le client_secret.json (si fourni) puis lance le flux navigateur local."""
    from minavio.publish import auth

    try:
        auth.authenticate(Path(client_secret_file) if client_secret_file else None)
    except MinavioError as e:
        return f"❌ {e}"
    return "✅ YouTube connecté.\n\n" + auth_markdown()


def video_choices(profile_id: str, job_id: str, out_dir: str | Path) -> list[str]:
    """`youtube`, `shorts`, `all` et chaque `short<i>` effectivement rendu pour ce job."""
    from minavio.publish.youtube import short_keys

    return [*PUBLISH_VIDEOS, *short_keys(Path(out_dir) / profile_id, job_id)]


def _overrides(video: str, title: str | None, description: str | None, tags: str | None) -> dict:
    """Textes édités : appliqués seulement à une vidéo précise (pas à `shorts` / `all`)."""
    if video != "youtube" and not re.fullmatch(r"short\d+", video):
        return {}
    out: dict = {}
    if title and title.strip():
        out["title"] = title.strip()
    if description and description.strip():
        out["description"] = description.strip()
    if tags is not None and tags.strip():
        out["tags"] = [t.strip() for t in re.split(r"[,\n]", tags) if t.strip()]
    return out


def _publish(
    profile_id: str,
    job_id: str,
    video: str,
    *,
    dry_run: bool,
    work_dir: str | Path,
    out_dir: str | Path,
    privacy: str | None,
    schedule_at: str | None,
    title: str | None,
    description: str | None,
    tags: str | None,
) -> list[dict]:
    from minavio.publish.youtube import publish_job

    if not job_id:
        raise MinavioError("Choisis un job récent (lance d'abord un montage).")
    return publish_job(
        load_profile(profile_id),
        job_id,
        video=video,
        dry_run=dry_run,
        work_root=Path(work_dir),
        out_dir=Path(out_dir),
        privacy=privacy or None,
        schedule_at=schedule_at,
        overrides=_overrides(video, title, description, tags),
    )


def preview(
    profile_id: str,
    job_id: str,
    video: str = "youtube",
    *,
    work_dir: str | Path = "work",
    out_dir: str | Path = "out",
    privacy: str | None = None,
    schedule_at: str | None = None,
    title: str | None = None,
    description: str | None = None,
    tags: str | None = None,
) -> list[dict]:
    """Dry-run de `publish_job` (aucun réseau) ; lève MinavioError si le job est incomplet."""
    return _publish(
        profile_id,
        job_id,
        video,
        dry_run=True,
        work_dir=work_dir,
        out_dir=out_dir,
        privacy=privacy,
        schedule_at=schedule_at,
        title=title,
        description=description,
        tags=tags,
    )


def profile_publish_settings(profile_id: str) -> dict:
    """Valeurs initiales du formulaire : confidentialité et heure programmée du profil."""
    p = load_profile(profile_id).publish
    return {"privacy": p.privacy, "schedule_at": p.schedule_at or ""}


def results_markdown(results: list[dict]) -> str:
    lines = []
    for r in results:
        when = r["publish_at"] or "immédiat"
        head = f"**{r['key']}** — {r['status'].replace('_', ' ')}"
        if r.get("url"):
            head += f" → {r['url']}"
        lines.append(head)
        lines.append(f"- confidentialité : `{r['privacy']}` · mise en ligne : {when}")
        lines.append(f"- fichier : `{r['file']}`")
        if r.get("thumbnail"):
            lines.append(f"- miniature : `{r['thumbnail']}`")
        lines += [f"- ⚠️ {w}" for w in r.get("warnings", [])]
    return "\n".join(lines)


def run_publish(
    profile_id: str,
    job_id: str,
    video: str = "youtube",
    *,
    work_dir: str | Path = "work",
    out_dir: str | Path = "out",
    privacy: str | None = None,
    schedule_at: str | None = None,
    title: str | None = None,
    description: str | None = None,
    tags: str | None = None,
    dry_run: bool = True,
    confirm: bool = False,
) -> str:
    """Publie réellement seulement avec `confirm=True` ; sinon refuse (jamais d'envoi implicite)."""
    if not dry_run and not confirm:
        return "❌ Coche « Je confirme la publication » avant de publier."
    try:
        results = _publish(
            profile_id,
            job_id,
            video,
            dry_run=dry_run,
            work_dir=work_dir,
            out_dir=out_dir,
            privacy=privacy,
            schedule_at=schedule_at,
            title=title,
            description=description,
            tags=tags,
        )
    except MinavioError as e:
        return f"❌ {e}"
    head = "🔍 Prévisualisation (rien n'a été envoyé)." if dry_run else "✅ Publication terminée."
    return head + "\n\n" + results_markdown(results)
