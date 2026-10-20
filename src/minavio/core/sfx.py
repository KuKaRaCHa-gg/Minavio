"""Effets sonores : résolution des fichiers (bibliothèque utilisateur puis banque embarquée)
et planification des cues à partir des moments de la timeline (pur, sans média)."""

from __future__ import annotations

import logging
from pathlib import Path

from minavio.config.schema import SfxSettings
from minavio.core.models import REWIND_DURATION_S, SfxCue, Timeline

log = logging.getLogger(__name__)

BUNDLED_DIR = Path(__file__).resolve().parent / "assets" / "sfx"
EXTENSIONS = ("ogg", "wav", "mp3")

CUT_LEAD_S = 0.08  # le whoosh démarre juste avant la coupe
MULTI_KILL_SECOND_HIT_S = 0.35


def resolve_sound(name: str, settings: SfxSettings) -> Path | None:
    """`name.(ogg|wav|mp3)` dans `settings.library_dir` puis dans la banque embarquée."""
    dirs: list[Path] = []
    if settings.library_dir:
        dirs.append(Path(settings.library_dir).expanduser())
    dirs.append(BUNDLED_DIR)
    for d in dirs:
        for ext in EXTENSIONS:
            p = d / f"{name}.{ext}"
            if p.is_file():
                return p
    log.warning(
        "SFX '%s' introuvable (cherché .%s dans %s) : événement ignoré",
        name,
        "/.".join(EXTENSIONS),
        ", ".join(str(d) for d in dirs),
    )
    return None


def plan_sfx(timeline: Timeline, settings: SfxSettings) -> list[SfxCue]:
    """Cues SFX d'une timeline (`settings.enabled` est géré par le rendu / le pipeline).

    - `on_cut` : début de chaque moment sauf le premier (jamais aux coupes internes d'autocut) ;
      omis à la jonction cold open -> contenu quand `on_cold_open` joue déjà à cet endroit.
    - `on_kill` / `on_multi_kill` : milieu du moment ; multi_kill ajoute un 2e `hit`.
    - `on_cold_open` : fin du moment cold open.
    Un champ `None` désactive l'événement ; aucune cue au-delà de `timeline.duration`.
    """
    moments = timeline.moments()
    gain = settings.gain_db
    cues: list[SfxCue] = []

    def add(at: float, sound: str) -> None:
        cues.append(SfxCue(at=max(at, 0.0), sound=sound, gain_db=gain))

    for i, m in enumerate(moments):
        after_cold_open = i > 0 and moments[i - 1].cold_open and not m.cold_open
        if i > 0 and settings.on_cut and not (after_cold_open and settings.on_cold_open):
            add(m.start - CUT_LEAD_S, settings.on_cut)
        at = m.peak if m.peak is not None else (m.start + m.end) / 2  # instant du kill
        if m.cold_open:
            pass  # pas de hit/boom dans le cold open : il rejoue le moment, qui aura les siens
        elif m.label == "kill" and settings.on_kill:
            add(at, settings.on_kill)
        elif m.label == "multi_kill" and settings.on_multi_kill:
            add(at, settings.on_multi_kill)
            if settings.on_kill:
                add(at + MULTI_KILL_SECOND_HIT_S, settings.on_kill)
        last_cold = m.cold_open and (i + 1 == len(moments) or not moments[i + 1].cold_open)
        if last_cold and settings.on_cold_open:
            # Même départ que l'overlay visuel `rewind` (pipeline) : fin du cold open - durée.
            add(max(m.start, m.end - REWIND_DURATION_S), settings.on_cold_open)

    total = timeline.duration
    return sorted((c for c in cues if c.at < total), key=lambda c: c.at)
