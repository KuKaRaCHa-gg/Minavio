"""Auto-réparation déterministe : classification FFmpeg, nouvelles tentatives, remux de sources.

Aucun LLM, aucune réécriture de code : les options de repli sont définies à l'avance (`ladder`).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from minavio.core import ffmpeg
from minavio.errors import FFmpegError, MinavioError

_ERROR_LIMIT = 300

# Ordre significatif : « Unknown encoder 'h264_nvenc' » doit rester une erreur d'encodeur.
_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("encoder", re.compile(r"Unknown encoder|Encoder not found", re.IGNORECASE)),
    (
        "gpu",
        re.compile(
            r"nvenc|cuda|(?:gpu|cuda|nvenc).{0,80}out of memory|out of memory.{0,80}(?:gpu|cuda)",
            re.IGNORECASE,
        ),
    ),
    ("oom", re.compile(r"Cannot allocate memory|MemoryError|\bKilled\b")),
    (
        "corrupt",
        re.compile(
            r"moov atom not found|Invalid data found when processing input|corrupt",
            re.IGNORECASE,
        ),
    ),
)

Step = Callable[[str], "dict[str, Any] | None"]


def classify(stderr: str) -> str | None:
    """Catégorie (`gpu`, `oom`, `corrupt`, `encoder`) d'une erreur FFmpeg, ou None."""
    text = stderr or ""
    for category, pattern in _RULES:
        if pattern.search(text):
            return category
    return None


@dataclass
class Attempt:
    category: str | None
    action: str | None
    error: str | None

    def __post_init__(self) -> None:
        if self.error is not None:
            self.error = self.error[:_ERROR_LIMIT]


def ladder(plan: dict[str, list[dict[str, Any]]]) -> Step:
    """Étape déterministe : renvoie, à chaque appel pour une catégorie, son option suivante."""
    queues = {category: list(options) for category, options in plan.items()}

    def step(category: str) -> dict[str, Any] | None:
        options = queues.get(category)
        return options.pop(0) if options else None

    return step


def _collect(steps: list[Step], category: str) -> dict[str, Any] | None:
    merged: dict[str, Any] = {}
    for step in steps:
        extra = step(category)
        if extra:
            merged.update(extra)
    return merged or None


def _describe(overrides: dict[str, Any]) -> str:
    return ", ".join(f"{k}={v}" for k, v in sorted(overrides.items()))


def _append_log(path: Path, attempts: list[Attempt]) -> None:
    previous: list[Any] = []
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            previous = loaded if isinstance(loaded, list) else []
        except (OSError, json.JSONDecodeError):
            previous = []
    path.parent.mkdir(parents=True, exist_ok=True)
    body = previous + [asdict(a) for a in attempts]
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")


def _exhausted(err: MinavioError, attempts: list[Attempt], log_path: Path | None) -> MinavioError:
    lines = []
    for n, a in enumerate(attempts, 1):
        cause = a.category or "erreur non classée"
        repair = f" -> relance avec {a.action}" if a.action else " -> aucune réparation"
        lines.append(f"  essai {n} : {cause}{repair} : {a.error}")
    hint = f" Journal : {log_path}." if log_path else ""
    message = (
        f"Échec après {len(attempts)} essai(s) :\n"
        + "\n".join(lines)
        + "\nVérifiez FFmpeg (encodeurs, GPU, mémoire) puis relancez avec --resume."
        + hint
    )
    try:
        return type(err)(message)
    except TypeError:  # sous-classe exotique : on garde la base
        return MinavioError(message)


def with_healing(
    fn: Callable[..., Any],
    *,
    steps: list[Step],
    log_path: Path | None,
    max_attempts: int = 3,
) -> Any:
    """Appelle `fn(**overrides)` ; sur erreur classée, applique les overrides des étapes et relance.

    Chaque tentative échouée est ajoutée à `log_path` (liste JSON). Relance la dernière erreur,
    enrichie de la liste des essais, quand il n'y a plus de tentative ou plus d'étape applicable.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts doit être >= 1")
    overrides: dict[str, Any] = {}
    attempts: list[Attempt] = []
    try:
        for number in range(1, max_attempts + 1):
            try:
                return fn(**overrides)
            except MinavioError as err:
                category = classify(str(err))
                action: str | None = None
                if category is not None and number < max_attempts:
                    extra = _collect(steps, category)
                    if extra:
                        overrides = {**overrides, **extra}
                        action = _describe(extra)
                attempts.append(Attempt(category, action, str(err)))
                if action is None:
                    raise _exhausted(err, attempts, log_path) from err
        raise AssertionError("inatteignable")  # pragma: no cover
    finally:
        if attempts and log_path is not None:
            _append_log(Path(log_path), attempts)


def repair_source(src: Path, work_dir: Path) -> Path:
    """Rend une source lisible : remux sans ré-encodage, sinon ré-encodage H.264/AAC.

    Sortie dans `work_dir/repaired/`. Lève `FFmpegError` si les deux tentatives échouent.
    """
    src = Path(src)
    out_dir = Path(work_dir) / "repaired"
    out_dir.mkdir(parents=True, exist_ok=True)
    remux = out_dir / f"{src.stem}.remux{src.suffix}"
    try:
        ffmpeg.run(["-err_detect", "ignore_err", "-i", str(src), "-c", "copy", str(remux)])
        ffmpeg.probe(remux)
        return remux
    except FFmpegError:
        remux.unlink(missing_ok=True)

    reenc = out_dir / f"{src.stem}.reenc.mp4"
    try:
        ffmpeg.run(
            [
                "-err_detect",
                "ignore_err",
                "-i",
                str(src),
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "23",
                "-c:a",
                "aac",
                str(reenc),
            ]
        )
        ffmpeg.probe(reenc)
        return reenc
    except FFmpegError as e:
        reenc.unlink(missing_ok=True)
        raise FFmpegError(
            f"Réparation impossible pour {src} (remux puis ré-encodage ont échoué) : {e}"
        ) from e
