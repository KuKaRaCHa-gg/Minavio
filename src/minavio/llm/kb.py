"""Base de connaissances : recherche par passages (BM25 maison) dans les fiches Markdown.

Les fiches intégrées sont dans `minavio/llm/knowledge/`. Les fiches de l'utilisateur
(`configs/knowledge/` du dossier courant, `~/.config/minavio/knowledge/`, et les dossiers de
`MINAVIO_KNOWLEDGE_DIR`) sont indexées en plus et passent avant. Aucun réseau, aucune dépendance.

Une fiche = un fichier `.md` découpé en sections `##` (un passage chacune). Directive optionnelle
en commentaire HTML dans le fichier :

    <!-- kb: tasks=rank,copy; content_type=gaming; genre=soulslike; mode=pve; tags=topic:foo -->

`genre=` (valeurs de `GameProfile.genre`) et `mode=` (pvp, pve, solo, coop) restreignent la fiche
aux profils dont AU MOINS UN jeu (`configs/games/<id>/game.yaml`) a ce genre / ce mode. Un jeu
absent ou invalide est ignoré ; une fiche `content_type=gaming` sans `genre` reste générique.

Les tags viennent du chemin : `games/<id>.md` -> `game:<id>`, `channels/<id>.md` ->
`channel:<id>`,
`youtube/titles_thumbnails.md` -> `topic:youtube`, `topic:titles`, `topic:thumbnails`...
Une section contenant `<!-- core -->` est toujours candidate (identité de la chaîne). La section
« Sources » n'est jamais indexée.
"""

from __future__ import annotations

import itertools
import logging
import math
import os
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from minavio.config.schema import ChannelProfile

log = logging.getLogger(__name__)

Task = Literal["rank", "copy", "chapters", "thumbnails", "vision_scan"]
ALL_TASKS: frozenset[str] = frozenset({"rank", "copy", "chapters", "thumbnails", "vision_scan"})

KB_FRACTION = 0.25  # part maximale du budget de contexte consacrée aux connaissances
KB_MAX_TOKENS = 1500
USER_BOOST = 1.5
HEADER = (
    "Connaissances utiles (repères métier pour t'aider à juger ; ne les recopie pas, "
    "n'en tire aucun timestamp ni fait absent des données fournies) :"
)

_DIRECTIVE_RE = re.compile(r"<!--\s*kb:\s*(.*?)\s*-->", re.DOTALL)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_CORE_RE = re.compile(r"<!--\s*core\s*-->")
_TOKEN_RE = re.compile(r"[a-z0-9]+")

_STOP_TEXT = """
    le la les un une des du de d l et ou a au aux en dans sur pour par avec sans qui que quoi dont
    est sont ete etre il elle ils elles on ce cet cette ces se sa son ses leur leurs mais donc
    ne pas plus tres si y n s c j m t qu lui eux nous vous je tu mon ma mes ton ta tes
    the an of to in on and or is are was be with for by at as it its this that from
"""
_STOPWORDS = frozenset(_STOP_TEXT.split())

# Synonymes FR/EN : un mot de la requête ajoute ces mots (poids réduit).
_SYNONYMS: dict[str, tuple[str, ...]] = {
    "equipe": ("squad", "escouade", "team"),
    "escouade": ("squad", "equipe"),
    "squad": ("escouade", "equipe"),
    "team": ("equipe", "squad"),
    "combat": ("fight", "fusillade"),
    "fight": ("combat",),
    "tue": ("kill", "elimine"),
    "tuer": ("kill", "elimine"),
    "elimine": ("kill", "eliminated"),
    "victoire": ("champion", "win", "victory"),
    "gagne": ("champion", "win", "victoire"),
    "zone": ("ring",),
    "cercle": ("ring",),
    "rire": ("laugh", "fou"),
    "cri": ("scream", "crie"),
    "miniature": ("thumbnail",),
    "thumbnail": ("miniature",),
    "titre": ("title",),
    "title": ("titre",),
    "chapitre": ("chapter",),
    "chapter": ("chapitre",),
    "musique": ("music", "content", "id"),
    "droits": ("copyright", "content", "id"),
}
_PHRASES: dict[tuple[str, str], tuple[str, ...]] = {
    ("autre", "equipe"): ("third", "party"),
    ("autre", "escouade"): ("third", "party"),
    ("troisieme", "equipe"): ("third", "party"),
    ("another", "team"): ("third", "party"),
    ("another", "squad"): ("third", "party"),
}
_SUFFIXES = ("ement", "ent", "ing", "ed", "er", "ez", "es", "e")


def _normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()


def _stem(word: str) -> str:
    if len(word) > 3 and word[-1] in "sx":
        word = word[:-1]
    for suf in _SUFFIXES:
        if word.endswith(suf) and len(word) - len(suf) >= 4:
            return word[: -len(suf)]
    return word


def _words(text: str) -> list[str]:
    return [w for w in _TOKEN_RE.findall(_normalize(text)) if w not in _STOPWORDS and len(w) > 1]


def tokenize(text: str) -> list[str]:
    """Mots normalisés (accents, casse), sans mots vides, racinisés légèrement (FR/EN)."""
    return [_stem(w) for w in _words(text)]


@dataclass(frozen=True)
class Passage:
    id: str
    source: str  # chemin relatif de la fiche, ex. games/apex_legends.md
    doc_title: str
    title: str
    text: str
    tags: frozenset[str]
    tasks: frozenset[str]
    content_types: frozenset[str] = frozenset()
    genres: frozenset[str] = frozenset()
    modes: frozenset[str] = frozenset()
    core: bool = False
    user: bool = False
    tokens: tuple[str, ...] = field(default=(), repr=False, compare=False)

    def tag_values(self, prefix: str) -> list[str]:
        return [t.split(":", 1)[1] for t in self.tags if t.startswith(prefix + ":")]


# --------------------------------------------------------------------------- chargement


def _parse_directive(text: str) -> dict[str, str]:
    m = _DIRECTIVE_RE.search(text)
    out: dict[str, str] = {}
    if m:
        for part in m.group(1).split(";"):
            key, sep, value = part.partition("=")
            if sep:
                out[key.strip().lower()] = value.strip()
    return out


def _csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def _path_tags(rel: str) -> set[str]:
    parts = Path(rel).with_suffix("").parts
    stem = parts[-1]
    tags: set[str] = set()
    if len(parts) > 1:
        folder = parts[0]
        if folder == "games":
            tags.add(f"game:{stem}")
        elif folder == "channels":
            tags.add(f"channel:{stem}")
        else:
            tags.add(f"topic:{folder}")
            tags.update(f"topic:{w}" for w in stem.split("_"))
    else:
        tags.add(f"topic:{stem}")
        tags.update(f"topic:{w}" for w in stem.split("_"))
    return tags


def parse_document(rel: str, text: str, *, user: bool = False) -> list[Passage]:
    """Découpe une fiche Markdown en passages (une section `##` chacune)."""
    directive = _parse_directive(text)
    tasks = frozenset(_csv(directive["tasks"])) if "tasks" in directive else ALL_TASKS
    ctypes = frozenset(_csv(directive.get("content_type", "")))
    genres = frozenset(_csv(directive.get("genre", "")))
    modes = frozenset(_csv(directive.get("mode", "")))
    tags = _path_tags(rel) | set(_csv(directive.get("tags", "")))
    h1 = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
    doc_title = h1.group(1).strip() if h1 else Path(rel).stem
    passages: list[Passage] = []
    sections = re.split(r"^##\s+", text, flags=re.MULTILINE)[1:]
    for i, sec in enumerate(sections):
        head, _, body = sec.partition("\n")
        title = head.strip()
        if not title or title.lower().startswith("sources"):
            continue
        core = bool(_CORE_RE.search(body))
        body = _COMMENT_RE.sub("", body).strip()
        if not body:
            continue
        tokens = tuple(tokenize(title) * 2 + tokenize(body))
        passages.append(
            Passage(
                id=f"{rel}#{i}",
                source=rel,
                doc_title=doc_title,
                title=title,
                text=body,
                tags=frozenset(tags),
                tasks=tasks,
                content_types=ctypes,
                genres=genres,
                modes=modes,
                core=core,
                user=user,
                tokens=tokens,
            )
        )
    return passages


def _walk(node: Traversable, prefix: str = "") -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for child in sorted(node.iterdir(), key=lambda c: c.name):
        rel = f"{prefix}{child.name}"
        if child.is_dir():
            out.extend(_walk(child, rel + "/"))
        elif child.name.endswith(".md"):
            out.append((rel, child.read_text(encoding="utf-8")))
    return out


@lru_cache(maxsize=1)
def builtin_passages() -> tuple[Passage, ...]:
    root = resources.files(__package__) / "knowledge"
    out: list[Passage] = []
    for rel, text in _walk(root):
        out.extend(parse_document(rel, text))
    return tuple(out)


def user_dirs() -> list[Path]:
    """Dossiers de fiches de l'utilisateur, par ordre de priorité décroissante."""
    dirs = [Path.cwd() / "configs" / "knowledge"]
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    dirs.append(base / "minavio" / "knowledge")
    dirs.extend(Path(p) for p in os.environ.get("MINAVIO_KNOWLEDGE_DIR", "").split(os.pathsep) if p)
    return dirs


def user_passages(extra_dirs: list[Path] | None = None) -> list[Passage]:
    out: list[Passage] = []
    seen: set[Path] = set()
    for d in [*(extra_dirs or []), *user_dirs()]:
        try:
            root = d.resolve()
            if root in seen or not root.is_dir():
                continue
            seen.add(root)
            for f in sorted(root.rglob("*.md")):
                rel = f.relative_to(root).as_posix()
                out.extend(parse_document(rel, f.read_text(encoding="utf-8"), user=True))
        except (OSError, UnicodeDecodeError) as exc:
            log.warning("Fiche de connaissances ignorée (%s) : %s", d, exc)
    return out


def load_passages(extra_dirs: list[Path] | None = None) -> list[Passage]:
    return [*user_passages(extra_dirs), *builtin_passages()]


# --------------------------------------------------------------------------- BM25


def _query_weights(query: str) -> dict[str, float]:
    words = _words(query)
    weights: dict[str, float] = {}
    for w in words:
        weights[_stem(w)] = 1.0
    extra: list[str] = []
    for w in words:
        extra.extend(_SYNONYMS.get(w) or _SYNONYMS.get(w.rstrip("s"), ()))
    for a, b in itertools.pairwise(words):
        extra.extend(_PHRASES.get((a, b), ()))
    for e in extra:
        weights.setdefault(_stem(e), 0.5)
    return weights


def bm25_scores(
    query: str, passages: list[Passage], corpus: list[Passage] | None = None, k1=1.5, b=0.75
) -> dict[str, float]:
    """Score BM25 de chaque passage (id -> score > 0). L'IDF vient de `corpus` (par défaut idem)."""
    corpus = corpus if corpus is not None else passages
    weights = _query_weights(query)
    if not weights or not corpus:
        return {}
    n = len(corpus)
    df: Counter[str] = Counter()
    for p in corpus:
        df.update(set(p.tokens))
    avg = sum(len(p.tokens) for p in corpus) / n or 1.0
    scores: dict[str, float] = {}
    for p in passages:
        tf = Counter(p.tokens)
        length = len(p.tokens) or 1
        s = 0.0
        for term, w in weights.items():
            f = tf.get(term, 0)
            if not f:
                continue
            idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
            s += w * idf * f * (k1 + 1) / (f + k1 * (1 - b + b * length / avg))
        if s > 0:
            scores[p.id] = s
    return scores


# --------------------------------------------------------------------------- filtres


def _config_dir() -> Path:
    """Dossier de configuration courant (lu à chaque appel : `MINAVIO_CONFIG_DIR` ou défaut)."""
    from minavio import config

    env = os.environ.get("MINAVIO_CONFIG_DIR")
    return Path(env) if env else Path(config.CONFIG_DIR)


@lru_cache(maxsize=64)
def _game_traits(game_id: str, config_dir: str) -> tuple[str, frozenset[str]] | None:
    try:
        from minavio.config import load_game

        game = load_game(game_id, Path(config_dir))
        return str(game.genre), frozenset(str(m) for m in game.modes)
    except Exception as exc:  # jeu absent ou invalide : ignoré, jamais bloquant
        log.debug("Jeu '%s' ignoré pour la base de connaissances : %s", game_id, exc)
        return None


def clear_game_cache() -> None:
    """Vide le cache des genres/modes de jeux (après modification d'un `game.yaml`)."""
    _game_traits.cache_clear()


def game_traits(profile: ChannelProfile) -> list[tuple[str, frozenset[str]]]:
    """(genre, modes) de chaque jeu du profil dont le `game.yaml` est lisible."""
    cdir = str(_config_dir())
    found = (_game_traits(g, cdir) for g in getattr(profile, "games", None) or [])
    return [t for t in found if t is not None]


# Mots-clés par genre pour les requêtes (alignés sur le vocabulaire des fiches `genres/`).
GENRE_TERMS: dict[str, str] = {
    "battle_royale": "killfeed kill knock clutch squad wipe champion victoire",
    "tactical_fps": "round ace clutch bombe spike killfeed",
    "arena_shooter": "killfeed kill multi kill frag",
    "extraction_shooter": "extraction raid butin loot kill survived",
    "moba": "teamfight penta kill ace nexus baron",
    "fighting": "combo ko round perfect super",
    "racing": "course dépassement arrivée accident",
    "sports": "but goal arrêt overtime match",
    "soulslike": "boss you died mort victoire barre de vie",
    "action_adventure": "boss cinématique découverte quête",
    "horror": "jumpscare horreur cri monstre peur",
    "survival": "survie construction mort découverte",
    "sandbox": "construction build minecraft survie découverte",
    "roguelike": "run build boss mort objets",
    "coop_pve": "coop pve extraction wave stratagème tir allié boss",
    "strategy": "victoire défaite bataille",
    "rhythm": "combo score perfect",
}
MODE_TERMS: dict[str, str] = {
    "pvp": "joueurs adverses kill",
    "pve": "boss ennemis monstres mort",
    "solo": "solo histoire découverte boss",
    "coop": "coop équipe chaos tir allié",
}


def genre_query(profile: ChannelProfile) -> str:
    """Mots-clés issus des genres et modes des jeux du profil (vide si aucun jeu lisible)."""
    seen: list[str] = []
    for genre, modes in game_traits(profile):
        for key, table in [(genre, GENRE_TERMS), *((m, MODE_TERMS) for m in sorted(modes))]:
            if key != "other":
                seen.append(key.replace("_", " "))
            if key in table:
                seen.append(table[key])
    return " ".join(dict.fromkeys(seen))


def applies(p: Passage, profile: ChannelProfile, task: str) -> bool:
    """Le passage concerne-t-il ce profil (jeux, genre, chaîne, type de contenu) et cette tâche ?"""
    if task not in p.tasks:
        return False
    ctype = str(getattr(profile.content_type, "value", profile.content_type))
    if p.genres or p.modes:
        traits = game_traits(profile)
        if p.genres and not any(g in p.genres for g, _ in traits):
            return False
        if p.modes and not any(p.modes & m for _, m in traits):
            return False
    if p.content_types:  # fiche générique pour un type de contenu (ex. FPS pour `gaming`)
        return ctype in p.content_types
    games = p.tag_values("game")
    if games and not any(g in profile.games for g in games):
        return False
    channels = p.tag_values("channel")
    return not channels or any(profile.id == c or profile.id.startswith(c + "_") for c in channels)


# --------------------------------------------------------------------------- API publique


def search(
    query: str, *, passages: list[Passage] | None = None, k: int = 5
) -> list[tuple[float, Passage]]:
    """Recherche brute (sans filtre de profil) : les `k` meilleurs passages."""
    pool = passages if passages is not None else load_passages()
    scores = bm25_scores(query, pool)
    ranked = sorted(pool, key=lambda p: -scores.get(p.id, 0.0) * (USER_BOOST if p.user else 1.0))
    return [(scores[p.id] * (USER_BOOST if p.user else 1.0), p) for p in ranked if p.id in scores][
        :k
    ]


def _block(p: Passage) -> str:
    return f"### {p.doc_title} - {p.title}\n{p.text}"


def retrieve(
    query: str,
    *,
    profile: ChannelProfile,
    task: Task,
    budget_tokens: int,
    extra_dirs: list[Path] | None = None,
) -> str:
    """Bloc « Connaissances utiles » prêt pour le prompt système ('' s'il n'y a rien à dire)."""
    if budget_tokens <= 0 or os.environ.get("MINAVIO_KB", "1") in {"0", "false", "off"}:
        return ""
    corpus = load_passages(extra_dirs)
    pool = [p for p in corpus if applies(p, profile, task)]
    if not pool:
        return ""
    scores = bm25_scores(query, pool, corpus)
    top = max(scores.values(), default=0.0)
    bonus = 0.5 * top if top > 0 else 1.0
    ranked: list[tuple[int, float, int, Passage]] = []
    for idx, p in enumerate(pool):
        raw = scores.get(p.id, 0.0)
        s = raw + (bonus if p.core else 0.0)
        if s <= 0:
            continue
        # Les fiches de l'utilisateur qui répondent à la requête passent avant les intégrées.
        tier = 1 if p.user and raw > 0 else 0
        ranked.append((tier, s * (USER_BOOST if p.user else 1.0), idx, p))
    ranked.sort(key=lambda t: (-t[0], -t[1], t[2]))
    limit = budget_tokens * 4 - len(HEADER) - 2
    chosen: list[str] = []
    used = 0
    for _, _, _, p in ranked:
        block = _block(p)
        cost = len(block) + 2
        if used + cost > limit:
            continue
        chosen.append(block)
        used += cost
    if not chosen and ranked and limit > 200:  # un seul gros passage : on le tronque
        chosen.append(_block(ranked[0][3])[: limit - 2].rstrip() + " [...]")
    if not chosen:
        return ""
    return HEADER + "\n\n" + "\n\n".join(chosen)


def budget_for(llm: Any = None, fraction: float = KB_FRACTION) -> int:
    """Budget (tokens) des connaissances : au plus 25 % du contexte du fournisseur, plafonné."""
    ctx = getattr(llm, "context_budget_tokens", None)
    ctx = ctx if isinstance(ctx, int) and ctx > 0 else 8000
    return min(KB_MAX_TOKENS, int(ctx * fraction))


def knowledge_for(
    profile: ChannelProfile,
    task: Task,
    query: str,
    budget_tokens: int | None = None,
    *,
    llm: Any = None,
) -> str:
    """Point d'entrée pour les modules LLM (et l'agent vision_scan). Ne lève jamais.

    `budget_tokens` None : déduit de `llm.context_budget_tokens` (25 %).
    """
    try:
        budget = budget_for(llm) if budget_tokens is None else budget_tokens
        return retrieve(query, profile=profile, task=task, budget_tokens=budget)
    except Exception as exc:  # la base de connaissances ne doit jamais bloquer le pipeline
        log.warning("Base de connaissances ignorée : %s", exc)
        return ""


def with_knowledge(
    system: str, profile: ChannelProfile, task: Task, query: str, llm: Any = None
) -> str:
    """`system` suivi du bloc de connaissances (inchangé s'il est vide)."""
    block = knowledge_for(profile, task, query, llm=llm)
    return f"{system}\n\n{block}" if block else system


def query_from(*parts: object, max_chars: int = 1500) -> str:
    """Assemble une requête depuis des labels, raisons, extraits de transcript (None ignorés)."""
    text = " ".join(str(p).replace("_", " ") for p in parts if p)
    return text[:max_chars]
