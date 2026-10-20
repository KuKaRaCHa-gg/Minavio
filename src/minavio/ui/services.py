"""Logique de l'interface graphique, sans dépendance à Gradio (testable seule).

Le montage tourne dans un sous-processus `python -m minavio.cli run …` : on peut l'annuler
proprement, la mémoire GPU est libérée à la fin, et l'UI reste réactive.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from pydantic import ValidationError

from minavio.config import CONFIG_DIR, list_games, list_profiles, load_game, load_profile
from minavio.config.schema import ChannelProfile
from minavio.errors import ConfigError, MinavioError

# Ordre affiché ; une étape absente du profil (autocut, sfx…) reste simplement « — ».
STEPS = [
    ("detect", "Détection des moments"),
    ("transcribe", "Transcription"),
    ("rank", "Classement (Qwen)"),
    ("copy", "Titre et pop-ups (Qwen)"),
    ("music", "Musique"),
    ("beats", "Beats"),
    ("autocut", "Autocut"),
    ("timeline", "Montage"),
    ("overlays", "Habillage"),
    ("sfx", "Effets sonores"),
    ("chapters", "Chapitres"),
    ("overlay_files", "Animations Remotion"),
    ("render", "Rendu vidéo"),
    ("thumbnails", "Miniatures"),
    ("short", "Shorts par moment"),
]

_STEP_RE = re.compile(r"\[(?P<name>[\w]+)\] (?P<state>ok en (?P<secs>[\d.]+)s|reprise)")
_JOB_RE = re.compile(r"job (?P<job>[0-9a-f]{6,}) :")
_WARN_RE = re.compile(r"\b(WARNING|ERROR)\b")
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}


# --------------------------------------------------------------------------- profils


def profile_choices() -> list[tuple[str, str]]:
    """(libellé, id) pour une liste déroulante ; les profils invalides restent listés."""
    out = []
    for pid in list_profiles():
        try:
            out.append((f"{load_profile(pid).display_name} ({pid})", pid))
        except MinavioError:
            out.append((f"{pid} — INVALIDE", pid))
    return out


def profile_defaults(profile_id: str) -> dict:
    """Valeurs par défaut des options de l'onglet Montage pour ce profil."""
    p = load_profile(profile_id)
    return {
        "formats": [o.name for o in p.outputs],
        "shorts": p.shorts.per_highlight,
        "chapters": p.chapters,
        "llm": p.llm.enabled,
        "thumbnails": p.thumbnails.enabled,
    }


def read_profile_yaml(profile_id: str, config_dir: Path = CONFIG_DIR) -> str:
    return (config_dir / "channels" / f"{profile_id}.yaml").read_text(encoding="utf-8")


def validate_profile_yaml(text: str, profile_id: str) -> str:
    """Message d'erreur lisible, ou chaîne vide si le YAML est un profil valide."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        return f"YAML invalide : {e}"
    if not isinstance(data, dict):
        return "Le fichier doit contenir un mapping YAML (clé: valeur)."
    try:
        profile = ChannelProfile.model_validate(data)
    except ValidationError as e:
        lines = [f"- {'.'.join(map(str, err['loc']))} : {err['msg']}" for err in e.errors()]
        return "Profil invalide :\n" + "\n".join(lines)
    if profile.id != profile_id:
        return f"`id: {profile.id}` doit être identique au nom du fichier (`{profile_id}`)."
    for game_id in profile.games:
        try:
            load_game(game_id)
        except ConfigError as e:
            return str(e)
    return ""


def save_profile_yaml(text: str, profile_id: str, config_dir: Path = CONFIG_DIR) -> str:
    """Valide puis écrit (copie de sauvegarde `.bak`). Retourne un message pour l'utilisateur."""
    if err := validate_profile_yaml(text, profile_id):
        return f"❌ Non enregistré.\n\n{err}"
    path = config_dir / "channels" / f"{profile_id}.yaml"
    if path.exists():
        shutil.copy2(path, path.with_suffix(".yaml.bak"))
    path.write_text(text, encoding="utf-8")
    return f"✅ Enregistré ({path.name}, ancienne version dans {path.name}.bak)."


# --------------------------------------------------------------------------- captures de jeu


@dataclass
class TemplateStatus:
    name: str
    label: str
    path: Path
    exists: bool
    roi: tuple[float, float, float, float]
    threshold: float


def game_choices() -> list[tuple[str, str]]:
    out = []
    for gid in list_games():
        try:
            out.append((load_game(gid).name, gid))
        except MinavioError:
            out.append((f"{gid} — INVALIDE", gid))
    return out


def template_statuses(game_id: str, config_dir: Path = CONFIG_DIR) -> list[TemplateStatus]:
    game = load_game(game_id, config_dir)
    base = config_dir / "games" / game_id
    return [
        TemplateStatus(
            name=t.name,
            label=t.label,
            path=base / t.image,
            exists=(base / t.image).is_file(),
            roi=t.roi,
            threshold=t.threshold,
        )
        for t in game.ui_templates
    ]


def templates_markdown(game_id: str) -> str:
    rows = ["| Capture | Événement | Fichier | État |", "| :--- | :--- | :--- | :--- |"]
    for t in template_statuses(game_id):
        state = "✅ présente" if t.exists else "❌ manquante"
        rows.append(f"| `{t.name}` | {t.label} | `{t.path.name}` | {state} |")
    return "\n".join(rows)


def roi_hint(game_id: str, template_name: str) -> str:
    for t in template_statuses(game_id):
        if t.name == template_name:
            x, y, w, h = t.roi
            return (
                f"Zone de recherche : {x:.0%} à {x + w:.0%} de la largeur, "
                f"{y:.0%} à {y + h:.0%} de la hauteur de l'écran. "
                f"Recadre **uniquement l'élément** (« {t.label} »), sans fond inutile, "
                "sur une capture 1080p. Seuil de reconnaissance : "
                f"{t.threshold:.2f}."
            )
    return ""


def save_template(game_id: str, template_name: str, image, config_dir: Path = CONFIG_DIR) -> str:
    """Enregistre l'image recadrée (PIL.Image) à l'emplacement attendu par le jeu."""
    if image is None:
        return "❌ Aucune image : uploade une capture d'écran puis recadre l'élément."
    match = [t for t in template_statuses(game_id, config_dir) if t.name == template_name]
    if not match:
        return f"❌ Capture inconnue pour ce jeu : {template_name}"
    w, h = image.size
    if w < 8 or h < 8:
        return f"❌ Image trop petite ({w}x{h}) : recadre un peu plus large."
    if w * h > 1920 * 1080 * 0.25:
        return (
            f"⚠️ Image très grande ({w}x{h}) : recadre seulement l'élément (marqueur, bandeau), "
            "sinon la reconnaissance sera lente et peu fiable. Non enregistré."
        )
    path = match[0].path
    path.parent.mkdir(parents=True, exist_ok=True)
    image.convert("RGB").save(path, format="PNG")
    return f"✅ `{path.name}` enregistré ({w}x{h})."


# --------------------------------------------------------------------------- montage


def count_clips(folder: str) -> str:
    if not folder.strip():
        return "Indique le dossier qui contient tes clips."
    p = Path(folder.strip()).expanduser()
    if not p.exists():
        return f"❌ Introuvable : {p}"
    from minavio.core.ffmpeg import VIDEO_EXTENSIONS, collect_sources  # mêmes règles que `run`

    files = collect_sources([p])
    if not files:
        exts = ", ".join(sorted(VIDEO_EXTENSIONS))
        return f"❌ Aucun clip vidéo ({exts}) dans {p}"
    size = sum(f.stat().st_size for f in files) / 1e9
    return f"✅ {len(files)} clip(s), {size:.1f} Go"


def build_run_args(
    profile_id: str,
    inputs: str,
    *,
    music: str = "",
    formats: list[str] | None = None,
    all_formats: list[str] | None = None,
    shorts: int | None = None,
    use_llm: bool = True,
    chapters: bool = True,
    thumbnails: bool = True,
    resume: bool = False,
    dry_run: bool = False,
    out_dir: str = "out",
) -> list[str]:
    """Arguments de `minavio run` (liste, jamais de shell)."""
    if not inputs.strip():
        raise MinavioError("Choisis le dossier de clips à monter.")
    args = [sys.executable, "-m", "minavio", "run", profile_id, str(Path(inputs).expanduser())]
    if music.strip():
        args += ["--music", str(Path(music.strip()).expanduser())]
    if formats is not None and all_formats is not None and set(formats) != set(all_formats):
        if not formats:
            raise MinavioError("Coche au moins un format de sortie.")
        for f in formats:
            args += ["--format", f]
    if shorts is not None:
        args += ["--shorts", str(int(shorts))]
    if not use_llm:
        args.append("--no-llm")
    if not chapters:
        args.append("--no-chapters")
    if not thumbnails:
        args.append("--no-thumbnails")
    if resume:
        args.append("--resume")
    if dry_run:
        args.append("--dry-run")
    args += ["--out", str(Path(out_dir or "out").expanduser())]
    return args


@dataclass
class JobState:
    """État d'un montage, reconstruit à partir des lignes de log."""

    job_id: str | None = None
    done: dict[str, str] = field(default_factory=dict)  # étape -> "1.2 s" | "reprise"
    current: str | None = None
    warnings: list[str] = field(default_factory=list)
    outputs: list[Path] = field(default_factory=list)
    thumbs: list[Path] = field(default_factory=list)
    log: list[str] = field(default_factory=list)
    returncode: int | None = None
    error: str | None = None

    def feed(self, line: str) -> None:
        line = line.rstrip()
        if not line:
            return
        self.log.append(line)
        if m := _JOB_RE.search(line):
            self.job_id = m["job"]
        if m := _STEP_RE.search(line):
            key = _step_key(m["name"])
            self.done[key] = "reprise" if m["state"] == "reprise" else f"{float(m['secs']):.1f} s"
            self.current = None
        elif _WARN_RE.search(line):
            self.warnings.append(line.split(maxsplit=2)[-1])
        elif line.startswith("Erreur :"):
            self.error = line.removeprefix("Erreur :").strip()
        else:  # la CLI imprime le chemin de chaque vidéo produite, seul sur sa ligne
            p = Path(line.strip())
            if p.suffix.lower() in VIDEO_EXTS and p.is_file():
                self.outputs.append(p)
            elif p.suffix.lower() in IMAGE_EXTS and p.is_file():
                self.thumbs.append(p)

    def progress_markdown(self) -> str:
        rows = []
        for key, label in STEPS:
            if key in self.done:
                rows.append(f"✅ {label} — {self.done[key]}")
            elif self.returncode is None and self._is_next(key):
                rows.append(f"⏳ **{label}…**")
            else:
                rows.append(f"▫️ {label}")
        head = f"**Job `{self.job_id}`**\n\n" if self.job_id else ""
        return head + "\n\n".join(rows)

    def _is_next(self, key: str) -> bool:
        keys = [k for k, _ in STEPS]
        done_idx = [keys.index(k) for k in self.done if k in keys]
        nxt = max(done_idx) + 1 if done_idx else 0
        return nxt < len(keys) and keys[nxt] == key


def _step_key(name: str) -> str:
    """`render_youtube` -> `render`, `short2_render` -> `short`, `overlay_files_shorts` -> …"""
    if name.startswith("short") and name != "shorts":
        return "short"
    for key, _ in STEPS:
        if name == key or name.startswith(key + "_"):
            return key
    return name


def run_job_process(args: list[str], cwd: Path) -> Iterator[tuple[JobState, subprocess.Popen]]:
    """Lance le montage et produit l'état après chaque ligne de log (stdout + stderr)."""
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    kwargs: dict = {}
    if os.name == "posix":
        kwargs["start_new_session"] = True  # pour tuer ffmpeg/node enfants à l'annulation
    proc = subprocess.Popen(
        args,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        **kwargs,
    )
    state = JobState()
    assert proc.stdout is not None
    for line in proc.stdout:
        state.feed(line)
        yield state, proc
    state.returncode = proc.wait()
    if state.returncode == 0 and not state.done and not state.outputs and not state.thumbs:
        # Sortie « propre » sans aucune étape : rien n'a tourné (commande mal formée…).
        state.returncode = -1
        state.error = state.error or "le montage n'a exécuté aucune étape (voir le journal)"
    yield state, proc


def cancel(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGTERM)
        else:
            proc.terminate()
    except ProcessLookupError:
        pass


def job_summary(job_id: str | None, out_dir: str, profile_id: str, cwd: Path) -> dict:
    """Titre / hook / description / tags (copy.json) et chapitres du job, s'ils existent."""
    if not job_id:
        return {}
    work = cwd / "work" / job_id
    info: dict = {}
    copy_path = work / "copy.json"
    if copy_path.is_file():
        with contextlib.suppress(json.JSONDecodeError):
            info.update(json.loads(copy_path.read_text(encoding="utf-8")))
    chapters = Path(out_dir).expanduser()
    if not chapters.is_absolute():
        chapters = cwd / chapters
    chapters = chapters / profile_id / f"{job_id}_chapters.txt"
    if chapters.is_file():
        info["chapters"] = chapters.read_text(encoding="utf-8")
    return info


def summary_markdown(info: dict) -> str:
    if not info:
        return "_Pas de texte généré (LLM désactivé ou indisponible)._"
    parts = []
    if info.get("title"):
        parts.append(f"**Titre** : {info['title']}")
    if info.get("hook"):
        parts.append(f"**Hook** : {info['hook']}")
    if info.get("description"):
        parts.append(f"**Description** :\n\n{info['description']}")
    if info.get("tags"):
        parts.append("**Tags** : " + ", ".join(info["tags"]))
    return "\n\n".join(parts) or "_Pas de texte généré._"


# --------------------------------------------------------------------------- système


def _has(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def system_status(repo_root: Path) -> list[tuple[str, bool, str]]:
    """(élément, ok, conseil) pour l'onglet Système. Rapide : aucun modèle n'est chargé."""
    motion = Path(os.environ.get("MINAVIO_MOTION_DIR", repo_root / "motion"))
    checks = [
        ("FFmpeg", shutil.which("ffmpeg") is not None, "Installe FFmpeg et ajoute-le au PATH."),
        ("Node.js (npx)", shutil.which("npx") is not None, "Installe Node.js ≥ 18."),
        (
            "Remotion (motion/node_modules)",
            (motion / "node_modules").is_dir(),
            "Lance `npm install` dans le dossier motion/.",
        ),
        ("Vision (OpenCV)", _has("cv2"), "uv sync --extra vision"),
        ("PySceneDetect", _has("scenedetect"), "uv sync --extra vision"),
        ("Audio (librosa)", _has("librosa"), "uv sync --extra audio"),
        ("Détection de voix (Silero)", _has("silero_vad"), "uv sync --extra audio"),
        ("Transcription (faster-whisper)", _has("faster_whisper"), "uv sync --extra asr"),
        ("Rires / cris (PANNs)", _has("panns_inference"), "uv sync --extra hype"),
    ]
    gpu_ok, gpu_hint = False, "torch absent : uv sync --extra hype (ou --extra audio)"
    if _has("torch"):
        try:
            import torch

            gpu_ok = bool(torch.cuda.is_available())
            gpu_hint = (
                torch.cuda.get_device_name(0)
                if gpu_ok
                else "CUDA indisponible : installe torch avec CUDA pour ta RTX."
            )
        except Exception as e:  # torch cassé : ne pas faire tomber l'onglet
            gpu_hint = f"torch inutilisable : {e}"
    checks.append(("GPU CUDA", gpu_ok, gpu_hint))
    return checks


def system_markdown(repo_root: Path) -> str:
    rows = ["| Élément | État | Détail / action |", "| :--- | :--- | :--- |"]
    for name, ok, hint in system_status(repo_root):
        detail = hint if not ok or "GPU" in name else ""
        rows.append(f"| {name} | {'✅' if ok else '❌'} | {detail} |")
    return "\n".join(rows)


def llm_ping_markdown(profile_id: str) -> str:
    from minavio.llm.client import LocalLLM

    p = load_profile(profile_id)
    try:
        ok = LocalLLM.from_settings(p.llm).ping()
    except MinavioError as e:
        ok, detail = False, str(e)
    else:
        detail = ""
    if p.llm.providers:
        return "\n\n".join(f"- {n} : {m}" for n, m in _ping_rows(p))
    state = "✅ connecté" if ok else "❌ injoignable"
    tip = (
        ""
        if ok
        else "\n\nLance ton serveur (ex. `ollama serve`) ou règle `MINAVIO_LLM_BASE_URL` / "
        "`MINAVIO_LLM_MODEL`, ou `llm.base_url` / `llm.model` dans l'onglet Réglages."
    )
    return f"**{p.llm.model}** sur `{p.llm.base_url}` : {state}{detail and ' — ' + detail}{tip}"


# --------------------------------------------------------------------------- LLM multi-fournisseurs

DEFAULT_KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "google": "GEMINI_API_KEY",
    "github": "GITHUB_TOKEN",
    "openai_compat": "OPENAI_API_KEY",
}
KEY_LABELS = {
    "ANTHROPIC_API_KEY": "Anthropic (Claude)",
    "GEMINI_API_KEY": "Google (Gemini)",
    "OPENAI_API_KEY": "OpenAI-compatible",
    "GITHUB_TOKEN": "GitHub Models (Copilot)",
}
GITHUB_TOKEN_URL = "https://github.com/settings/personal-access-tokens"
LLM_WARNING = (
    "⚠️ **Claude, Gemini et les API cloud sont payants** (sauf quotas gratuits) et envoient les "
    "transcripts et images de tes clips au fournisseur. GitHub Models (abonnement Copilot) a des "
    "limites strictes (~8k tokens d'entrée, quelques requêtes/min) : plutôt en secours. "
    "Les clés sont stockées en clair dans un fichier local réservé à ton compte, jamais dans les "
    "profils YAML."
)


def _ping_rows(p: ChannelProfile) -> list[tuple[str, str]]:
    from minavio.cli import ping_providers
    from minavio.llm.client import LocalLLM

    try:
        rows = ping_providers(LocalLLM.from_settings(p.llm), p.llm)
    except MinavioError as e:
        return [(p.llm.model, f"❌ {e}")]
    return [(n, "✅ ok" if ok else f"❌ {d}") for n, ok, d in rows]


def provider_rows(profile_id: str, ping: bool = False) -> list[list[str]]:
    """Tableau [ordre, type, modèle, clé, état] de la chaîne de fournisseurs du profil."""
    from minavio.config.secrets import secret_status

    p = load_profile(profile_id)
    providers = p.llm.providers or []
    pings = dict(_ping_rows(p)) if ping else {}
    rows: list[list[str]] = []
    if not providers:
        name = f"{p.llm.model} @ {p.llm.base_url}"
        rows.append(["1", "local", p.llm.model, "—", _state(pings, name, ping)])
        return rows
    for i, prov in enumerate(providers, 1):
        env = prov.api_key_env or DEFAULT_KEY_ENV.get(prov.kind)
        key = secret_status([env])[env] if env else "—"
        model = prov.model or ("défaut" if prov.kind != "local" else p.llm.model)
        state = _ping_one(prov, p.llm) if ping else "non testé"
        rows.append([str(i), prov.kind, model, key, state])
    return rows


def _ping_one(prov, settings) -> str:
    """Teste CE fournisseur, pas la chaîne (elle saute ceux sans clé : décalage des lignes)."""
    from minavio.llm.providers import build_provider

    try:
        client = build_provider(prov, settings)
    except MinavioError as e:
        return f"❌ {e}"
    if client is None:
        return "⏭ ignoré : clé absente"
    try:
        return "✅ ok" if client.ping() else "❌ injoignable ou clé refusée"
    except Exception as e:  # jamais de valeur de clé dans le message : erreurs du client
        return f"❌ {e}"


def _state(pings: dict, name: str, ping: bool) -> str:
    if not ping:
        return "non testé"
    return next(iter(pings.values()), "non testé")


def key_status_rows() -> list[list[str]]:
    """[service, variable, état, aperçu masqué] ; jamais la valeur."""
    from minavio.config.secrets import KNOWN_KEYS, load_secrets, mask, secret_status

    load_secrets()
    import os

    rows = []
    for name, state in secret_status(KNOWN_KEYS).items():
        value = os.environ.get(name, "")
        rows.append([KEY_LABELS.get(name, name), name, state, mask(value) if value else "—"])
    return rows


def save_key(name: str, value: str) -> str:
    from minavio.config.secrets import mask, set_secret

    if name not in KEY_LABELS:
        return f"❌ Clé inconnue : {name}"
    try:
        set_secret(name, value or "")
    except MinavioError as e:
        return f"❌ {e}"
    return f"✅ {name} enregistrée ({mask(value.strip())})."


def remove_key(name: str) -> str:
    from minavio.config.secrets import delete_secret

    if name not in KEY_LABELS:
        return f"❌ Clé inconnue : {name}"
    try:
        gone = delete_secret(name)
    except MinavioError as e:
        return f"❌ {e}"
    return f"✅ {name} supprimée du fichier." if gone else f"{name} n'était pas dans le fichier."


# --------------------------------------------------------------------------- miniatures

THUMB_LAYOUTS = ["auto", "hero", "split", "collage"]


@dataclass
class JobEntry:
    job_id: str
    mtime: float
    label: str


def list_jobs(work_root: Path) -> list[JobEntry]:
    """Dossiers `work/<job>/` (les plus récents d'abord) avec leur date."""
    import datetime as dt

    if not work_root.is_dir():
        return []
    jobs = []
    for d in work_root.iterdir():
        if d.is_dir() and (d / "detect.json").exists():
            m = d.stat().st_mtime
            when = dt.datetime.fromtimestamp(m).strftime("%d/%m/%Y %H:%M")
            jobs.append(JobEntry(d.name, m, f"{d.name} — {when}"))
    return sorted(jobs, key=lambda j: j.mtime, reverse=True)


def job_choices(work_root: Path) -> list[tuple[str, str]]:
    return [(j.label, j.job_id) for j in list_jobs(work_root)]


def build_thumbnails_args(
    profile_id: str,
    job_id: str,
    *,
    text: str = "",
    layout: str = "auto",
    out_dir: str = "out",
    work_dir: str = "work",
) -> list[str]:
    if not job_id:
        raise MinavioError("Choisis un job récent (lance d'abord un montage).")
    args = [sys.executable, "-m", "minavio", "thumbnails", profile_id, job_id]
    if text.strip():
        args += ["--text", text.strip()]
    if layout and layout != "auto":
        args += ["--layout", layout]
    args += ["--work", str(Path(work_dir).expanduser())]
    args += ["--out", str(Path(out_dir or "out").expanduser())]
    return args


def run_thumbnails_process(args: list[str], cwd: Path) -> tuple[JobState, list[str]]:
    """Exécute `minavio thumbnails` ; (état, PNG produits)."""
    state = JobState()
    for current, _ in run_job_process(args, cwd):
        state = current
    return state, [str(p) for p in state.thumbs]
