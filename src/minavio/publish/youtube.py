"""Publication YouTube (Data API v3) d'un job rendu.

`publish_job` relit `work/<job>/` (copy.json, chapters.txt, thumbnails.json), construit le corps
de la requête depuis le profil (`PublishSettings`), envoie la vidéo en upload résumable, pose la
miniature puis journalise `work/<job>/publish.json` : une vidéo déjà journalisée n'est jamais
republiée. `dry_run` n'ouvre aucune connexion. Rien n'est jamais publié par `minavio run`.

Quotas (2026) : `videos.insert` ≈ 100 unités/jour dans son compartiment, `thumbnails.set` ≈ 50.
Les bibliothèques Google sont importées à l'usage (extra `publish`) ; les tests remplacent
`_build_service` et `_media`.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from minavio.config.schema import ChannelProfile
from minavio.errors import MinavioError

log = logging.getLogger("minavio.publish")

CHUNK_SIZE = 8 * 1024 * 1024  # multiple de 256 Ko, requis par l'upload résumable
MAX_ATTEMPTS = 3
BACKOFF_S = 2.0
THUMB_MAX_BYTES = 2 * 1024 * 1024  # limite YouTube des miniatures
TITLE_MAX = 100
DESCRIPTION_MAX = 5000
TAGS_MAX_CHARS = 500
QUOTA_REASONS = {"quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded", "uploadLimitExceeded"}
_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
VIDEO_CHOICES = ("youtube", "shorts", "all")


# ----------------------------------------------------------------------------- hooks réseau


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _build_service() -> Any:
    """Client `youtube` v3 authentifié (hors tests : jeton local rafraîchi)."""
    from minavio.publish.auth import load_credentials

    creds = load_credentials()
    try:
        from googleapiclient.discovery import build
    except ImportError as e:
        raise MinavioError(
            f"google-api-python-client absent ({e}). Installe-le : uv sync --extra publish"
        ) from e
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def _media(path: Path, mimetype: str, *, resumable: bool) -> Any:
    try:
        from googleapiclient.http import MediaFileUpload
    except ImportError as e:
        raise MinavioError(
            f"google-api-python-client absent ({e}). Installe-le : uv sync --extra publish"
        ) from e
    if resumable:
        return MediaFileUpload(str(path), mimetype=mimetype, chunksize=CHUNK_SIZE, resumable=True)
    return MediaFileUpload(str(path), mimetype=mimetype)


# ----------------------------------------------------------------------------- erreurs API


def _http_info(e: Exception) -> tuple[int, str] | None:
    """(statut HTTP, raison) d'une HttpError de googleapiclient (duck typing) ; sinon None."""
    resp = getattr(e, "resp", None)
    status = getattr(resp, "status", None)
    if status is None:
        return None
    reason = ""
    content = getattr(e, "content", b"") or b""
    try:
        if isinstance(content, bytes):
            content = content.decode("utf-8", "replace")
        err = json.loads(content).get("error", {})
        errors = err.get("errors") or [{}]
        reason = str(errors[0].get("reason") or err.get("status") or "")
    except (ValueError, AttributeError, TypeError):
        pass
    return int(status), reason


def _is_network_error(e: Exception) -> bool:
    names = {c.__name__ for c in type(e).__mro__}
    return isinstance(e, OSError | TimeoutError) or "HttpLib2Error" in names


def _api_error(what: str, status: int, reason: str) -> MinavioError:
    if reason in QUOTA_REASONS:
        return MinavioError(
            f"Quota YouTube épuisé pendant « {what} » ({reason}). videos.insert coûte ≈ 100 unités "
            "du quota quotidien de ton projet Google Cloud et thumbnails.set ≈ 50 ; le quota se "
            "réinitialise à minuit (heure du Pacifique). Réessaie demain avec la même commande "
            "(les vidéos déjà publiées sont dans publish.json et ne seront pas renvoyées) ou "
            "demande une extension de quota dans la console Google Cloud."
        )
    if status in (401, 403):
        return MinavioError(
            f"YouTube refuse « {what} » (HTTP {status} {reason}). Relance `minavio publish auth` ; "
            "vérifie que l'API YouTube Data v3 est activée et que ta chaîne peut publier."
        )
    return MinavioError(f"YouTube a refusé « {what} » (HTTP {status} {reason}).")


def _call(what: str, request: Any) -> Any:
    """`request.execute()` avec traduction des erreurs HTTP en MinavioError."""
    try:
        return request.execute()
    except MinavioError:
        raise
    except Exception as e:
        info = _http_info(e)
        if info is not None:
            raise _api_error(what, *info) from e
        raise MinavioError(f"Échec réseau pendant « {what} » : {e}") from e


def _upload(request: Any, what: str) -> dict[str, Any]:
    """Upload résumable : reprise sur 5xx / erreur réseau (3 essais consécutifs, backoff)."""
    failures = 0
    last_pct = -1
    while True:
        try:
            status, response = request.next_chunk()
        except Exception as e:
            info = _http_info(e)
            retryable = (info is not None and info[0] >= 500) or (
                info is None and _is_network_error(e)
            )
            if not retryable:
                if info is not None:
                    raise _api_error(what, *info) from e
                raise MinavioError(f"Échec pendant « {what} » : {e}") from e
            failures += 1
            if failures >= MAX_ATTEMPTS:
                raise MinavioError(
                    f"Upload interrompu après {MAX_ATTEMPTS} essais ({what}) : {e}. Vérifie ta "
                    "connexion puis relance la même commande (rien n'est publié en double)."
                ) from e
            delay = BACKOFF_S * 2 ** (failures - 1)
            log.warning("upload : erreur transitoire (%s), reprise dans %.0fs", e, delay)
            _sleep(delay)
            continue
        failures = 0
        if response is not None:
            return response
        if status is not None:
            pct = int(status.progress() * 100)
            if pct // 10 != last_pct // 10:
                log.info("upload %s : %d%%", what, pct)
                last_pct = pct


# ----------------------------------------------------------------------------- métadonnées


def next_occurrence(hhmm: str, now: datetime | None = None) -> str:
    """« HH:MM » locale -> prochaine occurrence en RFC 3339 UTC (`2026-10-21T16:00:00Z`)."""
    m = _HHMM.match(hhmm.strip())
    if not m:
        raise MinavioError(
            f"Heure programmée invalide : '{hhmm}'. Format attendu HH:MM (ex. 18:00)."
        )
    now = now or datetime.now()  # naïf, heure locale
    cand = now.replace(hour=int(m[1]), minute=int(m[2]), second=0, microsecond=0)
    if cand <= now:
        cand += timedelta(days=1)
    return cand.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: UP017


def _clean(text: str) -> str:
    return text.replace("<", "").replace(">", "")  # interdits par l'API dans titre/description


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _tags(copy_tags: list[str], extra: list[str], *, shorts: bool) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    total = 0
    for t in [*copy_tags, *extra, *(["Shorts"] if shorts else [])]:
        t = _clean(str(t)).strip().lstrip("#")
        if not t or t.lower() in seen:
            continue
        cost = len(t) + (2 if " " in t else 0) + 1
        if total + cost > TAGS_MAX_CHARS:
            break
        seen.add(t.lower())
        out.append(t)
        total += cost
    return out


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as e:
        raise MinavioError(f"{path} illisible ({e}) : relance `minavio run … --resume`.") from e


def _chapters_text(work: Path, out_dir: Path, profile_id: str, job_id: str) -> str:
    for p in (work / "chapters.txt", out_dir / profile_id / f"{job_id}_chapters.txt"):
        if p.is_file():
            return p.read_text(encoding="utf-8").strip()
    data = _read_json(work / "chapters.json")
    if data:
        from minavio.core.models import Chapter
        from minavio.llm.chapters import format_chapters

        return format_chapters([Chapter.model_validate(c) for c in data]).strip()
    return ""


def pick_thumbnail(work: Path) -> Path | None:
    """1re variante 16:9 de `thumbnails.json` (format « youtube », sinon hors « short »)."""
    records = _read_json(work / "thumbnails.json")
    if not isinstance(records, list):
        return None
    ok = [r for r in records if isinstance(r, dict) and Path(str(r.get("file", ""))).is_file()]
    for r in ok:
        if r.get("format") == "youtube":
            return Path(r["file"])
    for r in ok:
        if "short" not in str(r.get("format", "")):
            return Path(r["file"])
    return None


def prepare_thumbnail(path: Path, work: Path) -> Path:
    """PNG > 2 Mo -> JPEG (qualité décroissante jusqu'à passer sous la limite)."""
    if path.stat().st_size <= THUMB_MAX_BYTES:
        return path
    from PIL import Image

    dest = work / "publish_thumb.jpg"
    with Image.open(path) as im:
        rgb = im.convert("RGB")
    for q in (92, 85, 75, 65, 50):
        rgb.save(dest, "JPEG", quality=q, optimize=True)
        if dest.stat().st_size <= THUMB_MAX_BYTES:
            return dest
    raise MinavioError(
        f"Miniature {path.name} trop lourde même en JPEG (> 2 Mo) : réduis sa taille."
    )


def _mimetype(path: Path) -> str:
    return "image/jpeg" if path.suffix.lower() in (".jpg", ".jpeg") else "image/png"


# ----------------------------------------------------------------------------- plan


def short_keys(files_dir: Path, job_id: str) -> list[str]:
    """`short1`, `short2`... rendus pour ce job (pas la compilation 9:16 `<job>_shorts.mp4`)."""
    keys = [
        p.stem.rsplit("_", 1)[1]
        for p in Path(files_dir).glob(f"{job_id}_short*.mp4")
        if re.fullmatch(rf"{re.escape(job_id)}_short\d+", p.stem)
    ]
    return sorted(keys, key=lambda k: int(k[5:]))


def _resolve_keys(profile: ChannelProfile, files_dir: Path, job_id: str, video: str) -> list[str]:
    shorts = short_keys(files_dir, job_id)
    if video == "youtube":
        return ["youtube"]
    if video == "shorts":
        if not shorts:
            raise MinavioError(
                f"Aucun short pour le job {job_id} dans {files_dir} (<job>_short<i>.mp4). "
                f"Lance `minavio run {profile.id} … --shorts 3`."
            )
        return shorts
    if re.fullmatch(r"short\d+", video):
        return [video]
    if video == "all":
        keys = ["youtube"] if (files_dir / f"{job_id}_youtube.mp4").is_file() else []
        return keys + (shorts if profile.publish.shorts else [])
    raise MinavioError(f"--video '{video}' inconnu : youtube, shorts, short<N> ou all.")


def _plan_one(
    key: str,
    profile: ChannelProfile,
    *,
    job_id: str,
    work: Path,
    files_dir: Path,
    out_dir: Path,
    copy: dict[str, Any],
    privacy: str,
    publish_at: str | None,
    overrides: dict[str, Any],
    n_shorts: int,
) -> dict[str, Any]:
    cfg = profile.publish
    is_short = key.startswith("short")
    path = files_dir / f"{job_id}_{key}.mp4"
    if not path.is_file():
        raise MinavioError(
            f"Vidéo introuvable : {path}. Lance `minavio run {profile.id} …` (ou vérifie --out)."
        )
    title = str(overrides.get("title") or copy.get("title") or profile.display_name)
    base_desc = str(copy.get("description") or "").strip()
    if is_short:
        idx = key[5:]
        first = re.split(r"(?<=[.!?])\s", base_desc, maxsplit=1)[0] if base_desc else ""
        if n_shorts > 1 and not overrides.get("title"):
            title = f"{title} ({idx})"
        tag = "" if "#shorts" in title.lower() else " #Shorts"
        title = _clip(_clean(title), TITLE_MAX - len(tag)).rstrip() + tag
        description = _clip(_clean(first), 300) + ("\n\n" if first else "") + "#Shorts"
        thumb = None  # YouTube ignore les miniatures personnalisées des Shorts
    else:
        title = _clip(_clean(title), TITLE_MAX)
        chapters = _chapters_text(work, out_dir, profile.id, job_id)
        description = _clean(base_desc) + (f"\n\n{chapters}" if chapters else "")
        thumb = pick_thumbnail(work)
    if overrides.get("description") is not None:  # texte final édité à la main : tel quel
        description = _clean(str(overrides["description"]))
    description = _clip(description.strip(), DESCRIPTION_MAX)
    raw_tags = overrides.get("tags")
    if raw_tags is None:
        raw_tags = copy.get("tags") or []
    tags = _tags(list(raw_tags), cfg.extra_tags, shorts=is_short)
    snippet: dict[str, Any] = {
        "title": title,
        "description": description,
        "categoryId": cfg.category_id,
        "defaultLanguage": cfg.language,
        "defaultAudioLanguage": cfg.language,
    }
    if tags:
        snippet["tags"] = tags
    status: dict[str, Any] = {
        # programmée (forcément public, vérifié plus haut) : envoyée private, YouTube la rend
        # publique à publishAt
        "privacyStatus": "private" if publish_at else privacy,
        "selfDeclaredMadeForKids": cfg.made_for_kids,
    }
    if publish_at:
        status["publishAt"] = publish_at
    return {
        "key": key,
        "file": str(path),
        "title": title,
        "description": description,
        "tags": tags,
        "privacy": privacy,
        "publish_at": publish_at,
        "thumbnail": str(thumb) if thumb else None,
        "playlist_id": cfg.playlist_id,
        "body": {"snippet": snippet, "status": status},
    }


def _journal_path(work: Path) -> Path:
    return work / "publish.json"


def _load_journal(work: Path) -> dict[str, Any]:
    data = _read_json(_journal_path(work))
    return (
        data if isinstance(data, dict) and isinstance(data.get("videos"), dict) else {"videos": {}}
    )


def _save_journal(work: Path, journal: dict[str, Any]) -> None:
    tmp = _journal_path(work).with_suffix(".tmp")
    tmp.write_text(json.dumps(journal, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(_journal_path(work))


def _url(key: str, video_id: str) -> str:
    if key.startswith("short"):
        return f"https://www.youtube.com/shorts/{video_id}"
    return f"https://www.youtube.com/watch?v={video_id}"


def publish_job(
    profile: ChannelProfile,
    job_id: str,
    *,
    video: str = "youtube",
    dry_run: bool = False,
    work_root: Path = Path("work"),
    out_dir: Path = Path("out"),
    privacy: str | None = None,
    schedule_at: str | None = None,
    overrides: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Publie (ou prévisualise) les vidéos d'un job ; un dict par vidéo.

    `video` : `youtube`, `shorts`, `short<i>` ou `all`. `privacy` / `schedule_at` ("HH:MM", ou
    "now" pour publier immédiatement) surchargent le profil. `overrides` (title, description,
    tags) remplace les textes de `copy.json` (l'éditeur de l'interface). Statuts :
    `dry_run`, `uploaded`, `already_published`.
    """
    work = work_root / job_id
    if not work.is_dir():
        raise MinavioError(
            f"Job '{job_id}' introuvable ({work}). Lance d'abord `minavio run {profile.id} …` "
            "ou vérifie --work."
        )
    cfg = profile.publish
    privacy = privacy or cfg.privacy
    if privacy not in ("private", "unlisted", "public"):
        raise MinavioError(f"Confidentialité '{privacy}' inconnue : private, unlisted ou public.")
    when = cfg.schedule_at if schedule_at is None else schedule_at
    publish_at = None if when in (None, "", "now") else next_occurrence(when, now)
    if publish_at and privacy != "public":
        # publishAt rend la vidéo PUBLIQUE à l'heure dite : incompatible avec private/unlisted.
        raise MinavioError(
            f"Programmation à {when} demandée avec la confidentialité '{privacy}' : une vidéo "
            "programmée devient publique à l'heure dite. Choisis --privacy public pour la "
            "programmer, ou --at now (ou schedule_at: null dans le profil) pour la garder "
            f"{privacy}."
        )
    copy = _read_json(work / "copy.json")
    if not isinstance(copy, dict):
        if not (overrides and overrides.get("title")):
            raise MinavioError(
                f"{work / 'copy.json'} absent : titre et description viennent du LLM. Relance "
                f"`minavio run {profile.id} …` sans --no-llm, ou fournis un titre à la main."
            )
        copy = {}
    files_dir = out_dir / profile.id
    keys = _resolve_keys(profile, files_dir, job_id, video)
    if not keys:
        raise MinavioError(f"Aucune vidéo à publier dans {files_dir} pour le job {job_id}.")
    n_shorts = len(short_keys(files_dir, job_id))
    plans = [
        _plan_one(
            k,
            profile,
            job_id=job_id,
            work=work,
            files_dir=files_dir,
            out_dir=out_dir,
            copy=copy,
            privacy=privacy,
            publish_at=publish_at,
            overrides=overrides or {},
            n_shorts=n_shorts,
        )
        for k in keys
    ]
    journal = _load_journal(work)
    results: list[dict[str, Any]] = []
    service: Any = None
    for plan in plans:
        key = plan["key"]
        done = journal["videos"].get(key)
        if done and done.get("video_id"):
            log.info("%s déjà publiée (%s) : ignorée", key, done.get("url"))
            results.append({**plan, "status": "already_published", **_public(done)})
            continue
        if dry_run:
            results.append({**plan, "status": "dry_run"})
            continue
        if service is None:
            service = _build_service()
        results.append(_publish_one(service, plan, work, journal, cfg.playlist_id))
    return results


def _public(done: dict[str, Any]) -> dict[str, Any]:
    return {k: done[k] for k in ("video_id", "url") if k in done}


def _publish_one(
    service: Any, plan: dict[str, Any], work: Path, journal: dict[str, Any], playlist_id: str | None
) -> dict[str, Any]:
    key, warnings = plan["key"], []
    what = f"upload de {Path(plan['file']).name}"
    request = service.videos().insert(
        part="snippet,status",
        body=plan["body"],
        media_body=_media(Path(plan["file"]), "video/mp4", resumable=True),
    )
    response = _upload(request, what)
    video_id = response.get("id")
    if not video_id:
        raise MinavioError(f"Réponse YouTube sans identifiant de vidéo pour {key}.")
    record: dict[str, Any] = {
        "video_id": video_id,
        "url": _url(key, video_id),
        "status": "scheduled" if plan["publish_at"] else "uploaded",
        "title": plan["title"],
        "privacy": plan["body"]["status"]["privacyStatus"],
        "publish_at": plan["publish_at"],
        "uploaded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),  # noqa: UP017
    }
    journal["videos"][key] = record
    _save_journal(work, journal)  # tout de suite : un échec ensuite ne provoque pas de doublon

    if plan["thumbnail"]:
        try:
            thumb = prepare_thumbnail(Path(plan["thumbnail"]), work)
            _call(
                "thumbnails.set",
                service.thumbnails().set(
                    videoId=video_id, media_body=_media(thumb, _mimetype(thumb), resumable=False)
                ),
            )
            record["thumbnail"] = "set"
        except Exception as e:  # la vidéo est en ligne : on signale, sans tout faire échouer
            msg = f"miniature non posée pour {key} : {e}"
            log.warning(msg)
            warnings.append(msg)
            record["thumbnail"] = f"error: {e}"
    if playlist_id:
        try:
            _call(
                "playlistItems.insert",
                service.playlistItems().insert(
                    part="snippet",
                    body={
                        "snippet": {
                            "playlistId": playlist_id,
                            "resourceId": {"kind": "youtube#video", "videoId": video_id},
                        }
                    },
                ),
            )
            record["playlist"] = playlist_id
        except Exception as e:
            msg = f"ajout à la playlist impossible pour {key} : {e}"
            log.warning(msg)
            warnings.append(msg)
    _save_journal(work, journal)
    return {**plan, **_public(record), "status": "uploaded", "warnings": warnings}
