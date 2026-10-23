"""Ingestion automatique : surveille un dossier de clips et lance le montage par lots.

La logique est dans `Watcher` (horloge et `run` injectables, sans watchdog) ; `serve`
ajoute la surveillance du dossier (watchdog si installé, sinon scrutation).
Ne publie jamais sur YouTube : seul le montage (`run_job`) est déclenché.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path

from minavio.errors import MinavioError
from minavio.integrations.notify import notify

log = logging.getLogger("minavio.watch")

MAX_BATCH = 20
DEFAULT_EXTENSIONS = (".mp4", ".mkv", ".mov")
_TEMP_SUFFIXES = (".part", ".tmp")

Sig = tuple[int, float]  # (taille, mtime) : identifie une version d'un fichier


def _sig(path: Path) -> Sig | None:
    """Taille et mtime d'un fichier régulier, None s'il n'existe plus."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    return (st.st_size, st.st_mtime)


def _default_run(music: Path | None) -> Callable[[str, list[Path]], object]:
    def _run(profile_id: str, paths: list[Path]) -> object:
        from minavio.pipeline import run_job  # import tardif : `minavio --help` reste rapide

        return run_job(profile_id, paths, music=music)

    return _run


def _safe_job_id(profile_id: str, paths: list[Path]) -> str | None:
    """Identifiant du job d'après ses entrées, pour un échec où `run_job` n'a rien renvoyé."""
    try:
        from minavio.config import load_profile
        from minavio.pipeline import compute_job_id

        return compute_job_id(load_profile(profile_id), paths)
    except Exception:  # best effort : la notification d'échec ne doit pas échouer
        return None


def notify_job(
    profile_id: str,
    job_id: str | None,
    outputs: list[Path] | None = None,
    error: str | None = None,
) -> list[str]:
    """Notifie la fin d'un job (succès ou échec). Ne lève jamais ; ne publie jamais."""
    try:
        if error is not None:
            first = (error.strip().splitlines() or ["erreur inconnue"])[0]
            title = f"Minavio : échec · {profile_id}"
            lines = [f"Job {job_id or 'inconnu'}", f"Erreur : {first}"]
        else:
            title = f"Minavio : montage terminé · {profile_id}"
            lines = [f"Job {job_id or 'inconnu'}"]
            names = [p.name for p in outputs or []]
            lines.append("Sorties : " + (", ".join(names) if names else "aucune"))
            if job_id:
                lines.append(
                    f"Publication : `minavio publish {profile_id} {job_id}` (jamais automatique)."
                )
        thumb = Path("out") / profile_id / f"{job_id}_thumb1_youtube.png" if job_id else None
        return notify(
            title,
            "\n".join(lines),
            file=thumb if thumb is not None and thumb.is_file() else None,
        )
    except Exception as e:
        log.warning("Notification de fin de job non envoyée : %s", type(e).__name__)
        return []


class Watcher:
    """File d'attente de clips : stabilité, lots, état persistant. Synchrone et sans I/O réseau."""

    def __init__(
        self,
        profile_id: str,
        folder: Path,
        *,
        stable_s: float = 30,
        batch_s: float = 600,
        extensions: Iterable[str] = DEFAULT_EXTENSIONS,
        state_path: Path | None = None,
        run: Callable[[str, list[Path]], object] | None = None,
        clock: Callable[[], float] = time.monotonic,
        music: Path | None = None,
    ) -> None:
        self.profile_id = profile_id
        self.folder = Path(folder)
        self.stable_s = stable_s
        self.batch_s = batch_s
        self.extensions = tuple(e.lower() for e in extensions)
        self.state_path = (
            Path(state_path) if state_path else Path("work") / f".watch_{profile_id}.json"
        )
        self._run = run or _default_run(music)
        self._clock = clock
        self._lock = threading.RLock()  # offer() peut venir du thread watchdog
        # chemin -> (signature, instant du dernier changement observé)
        self._pending: dict[str, tuple[Sig, float]] = {}
        # chemin -> signature, fichiers stables en attente de lancement
        self._batch: dict[str, Sig] = {}
        self._last_arrival = 0.0
        self._files: dict[str, dict] = self._load_state()

    # ----- état persistant -------------------------------------------------

    def _load_state(self) -> dict[str, dict]:
        try:
            raw = self.state_path.read_text("utf-8")
        except FileNotFoundError:
            return {}
        except OSError as e:
            raise MinavioError(f"État du watch illisible ({self.state_path}) : {e}") from e
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise MinavioError(
                f"État du watch corrompu ({self.state_path}) : {e}. Supprime ce fichier "
                "pour repartir de zéro (les clips seront réanalysés)."
            ) from e
        files = data.get("files") if isinstance(data, dict) else None
        return files if isinstance(files, dict) else {}

    def _save(self) -> None:
        tmp = self.state_path.parent / (self.state_path.name + ".tmp")
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"profile": self.profile_id, "files": self._files}
            tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), "utf-8")
            os.replace(tmp, self.state_path)
        except OSError as e:
            log.error("Impossible d'écrire l'état du watch (%s) : %s", self.state_path, e)

    # ----- candidats -------------------------------------------------------

    def offer(self, path: Path | str) -> bool:
        """Enregistre un candidat. Renvoie False si ignoré (extension, temporaire, déjà traité)."""
        p = Path(path)
        name = p.name
        if name.startswith(("~", ".")) or name.lower().endswith(_TEMP_SUFFIXES):
            return False
        if p.suffix.lower() not in self.extensions:
            return False
        key = os.path.abspath(p)
        with self._lock:
            if key in self._batch:
                return False
            sig = _sig(p)
            if sig is None:
                self._pending.pop(key, None)
                return False
            done = self._files.get(key)
            if done and (done.get("size"), done.get("mtime")) == sig:
                return False  # traité (ou échoué) dans cette version : pas de relance auto
            now = self._clock()
            prev = self._pending.get(key)
            if prev is None:
                # Un fichier déjà vieux (lancement --once, redémarrage) est stable d'emblée.
                age = max(0.0, time.time() - sig[1])
                self._pending[key] = (sig, now - age)
            elif prev[0] != sig:
                self._pending[key] = (sig, now)
            return True

    def scan(self) -> None:
        """Propose tous les fichiers du dossier (récursif)."""
        for p in self.folder.rglob("*"):
            self.offer(p)

    def tick(self) -> None:
        """Avance d'un pas : fichiers devenus stables, lot à lancer. Ne lève pas (sauf Ctrl+C)."""
        with self._lock:
            now = self._clock()
            for key, (sig, since) in list(self._pending.items()):
                cur = _sig(Path(key))
                if cur is None:
                    del self._pending[key]
                elif cur != sig:
                    self._pending[key] = (cur, now)
                elif now - since >= self.stable_s:
                    del self._pending[key]
                    self._batch[key] = sig
                    self._last_arrival = now
            while len(self._batch) >= MAX_BATCH:
                self._launch(self._take(MAX_BATCH))
            if self._batch and now - self._last_arrival >= self.batch_s:
                self._launch(self._take(len(self._batch)))

    def flush(self) -> None:
        """Lance immédiatement ce qui est stable (utilisé par `--once`)."""
        with self._lock:
            while self._batch:
                self._launch(self._take(MAX_BATCH))

    def _take(self, n: int) -> dict[str, Sig]:
        keys = list(self._batch)[:n]
        return {k: self._batch.pop(k) for k in keys}

    # ----- lancement -------------------------------------------------------

    def _launch(self, items: dict[str, Sig]) -> None:
        paths = [Path(k) for k in items]
        log.info("Montage de %d clip(s) (profil %s)", len(paths), self.profile_id)
        try:
            result = self._run(self.profile_id, paths)
        except Exception as e:  # le démon ne meurt jamais sur un job en échec
            msg = str(e) or type(e).__name__
            for key, (size, mtime) in items.items():
                self._files[key] = {"size": size, "mtime": mtime, "status": "failed", "error": msg}
            self._save()
            log.error("Échec du montage de %d clip(s) : %s", len(paths), msg)
            log.debug("Trace de l'échec", exc_info=True)
            log.error(
                "Ces clips ne seront pas relancés automatiquement. Pour relancer un clip, "
                "supprime son entrée dans %s (ou modifie le fichier), "
                "puis relance `minavio watch`.",
                self.state_path,
            )
            notify_job(
                self.profile_id,
                _safe_job_id(self.profile_id, paths),
                error=msg,
            )
            return
        for key, (size, mtime) in items.items():
            self._files[key] = {"size": size, "mtime": mtime, "status": "done"}
        self._save()
        log.info("Lot terminé : %d clip(s).", len(paths))
        notify_job(
            self.profile_id,
            getattr(result, "job_id", None),
            outputs=[Path(p) for p in getattr(result, "outputs", []) or []],
        )


def serve(watcher: Watcher, poll_s: float = 5) -> None:
    """Surveille le dossier jusqu'à Ctrl+C. watchdog si installé, sinon scrutation du dossier."""
    observer = None
    try:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer
    except ImportError:
        log.info("watchdog absent : scrutation toutes les %s s (extra `watch`).", poll_s)
    else:

        class _Handler(FileSystemEventHandler):
            def on_any_event(self, event):  # type: ignore[no-untyped-def]
                if event.is_directory:
                    return
                for raw in (event.src_path, getattr(event, "dest_path", "")):
                    if raw:
                        watcher.offer(raw)

        observer = Observer()
        observer.schedule(_Handler(), str(watcher.folder), recursive=True)
        observer.start()
        log.info("Surveillance de %s (watchdog).", watcher.folder)

    log.info("Analyse initiale de %s…", watcher.folder)
    watcher.scan()
    try:
        while True:
            if observer is None:
                watcher.scan()
            watcher.tick()
            time.sleep(poll_s)
    except KeyboardInterrupt:
        log.info("Arrêt demandé (Ctrl+C).")
    finally:
        if observer is not None:
            observer.stop()
            observer.join()
