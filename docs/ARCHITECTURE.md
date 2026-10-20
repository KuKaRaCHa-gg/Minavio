# Minavio — Architecture (Phase 1, à valider)

Moteur de montage hybride (Claude Code + LLM local + traitement hôte) pour
**Minivitch**, **Byte Tricks** et **The Lost Chat**.

## 1. Principe directeur : un contrat JSON entre modules

Chaque module lit et écrit des objets **Pydantic sérialisables en JSON**,
définis une fois dans `src/minavio/core/models.py`. C'est ce qui permet aux
subagents de travailler en parallèle (worktrees) sans se marcher dessus, et à
n'importe quelle IA de reprendre un module isolément.

```
 source.mp4 ──► [detect]  ──► highlights.json ──► [llm.rank] ──► highlights.json (scorés)
                 OpenCV / VAD / audio                Qwen local
                                                         │
                                                         ▼
 music.mp3 ──► [beats] ──► beats.json ──────────► [edit.plan] ──► timeline.json
                                                         │
                         ┌───────────────────────────────┤
                         ▼                               ▼
                  [motion.render]                 [core.render]
                  Remotion → overlays .mov        FFmpeg → 16:9 + 9:16
                  (alpha ProRes 4444)             (cut, reframe, ducking, mix, loudnorm)
```

Chaque étape est **rejouable** à partir de son fichier d'entrée (cache dans
`work/<job_id>/`). Aucun appel cloud dans le pipeline d'exécution.

## 2. Stack retenue

| Besoin | Choix | Pourquoi |
| :--- | :--- | :--- |
| Langage moteur | Python ≥ 3.11, `uv` | Écosystème vidéo/ML, lisible par toutes les IA |
| CLI | **Typer** | Typage → aide auto, facile à étendre |
| Config / contrats | **Pydantic v2** + YAML | Validation stricte, schéma JSON exportable |
| Découpe / rendu | **FFmpeg** via `subprocess` (wrapper maison fin) | `ffmpeg-python` n'est plus maintenu ; MoviePy trop lent pour du batch |
| Vision | **OpenCV** (`opencv-python-headless`) | Template matching (killfeed, UI), détection de scènes |
| Suivi visage / focus | **MediaPipe** Face Detection (CPU) | Reframe 9:16 léger, sans GPU |
| VAD | **Silero VAD** (ONNX) | Plus fiable que WebRTC sur voix + jeu |
| Transcription | **faster-whisper** (CUDA, RTX 5070 Ti) | Local, timestamps mot-à-mot pour sous-titres |
| Beat detection | **librosa** (`beat_track`, onsets) | Standard, suffisant pour du beat-matching |
| LLM local | **Qwen 30B** via endpoint **OpenAI-compatible** (Ollama ou vLLM), client `httpx` | Un seul client pour les deux serveurs |
| Motion design | **Remotion** (React/TS) dans `motion/`, piloté par props JSON | Animations riches, rendu headless local ; fallback FFmpeg `drawtext` si Node absent |
| Qualité | `ruff` (lint + format), `pytest` | Exigé Phase 3 |

Dépendances lourdes en **extras** pour garder l'install de base légère :
`minavio[vision]`, `minavio[audio]`, `minavio[asr]`, `minavio[all]`.

## 3. Arborescence

```
Minavio/
├── CLAUDE.md                     # règles orchestrateur + économie de tokens
├── .claude/agents/               # subagents (cf. §5)
├── configs/
│   ├── channels/                 # minivitch.yaml, bytetricks.yaml, thelostchat.yaml
│   └── games/                    # apex_legends.yaml (+ futurs jeux, sans toucher au code)
├── src/minavio/
│   ├── cli.py                    # Typer : `minavio run|profiles|...`
│   ├── config/                   # schéma Pydantic des profils + loader YAML
│   ├── core/                     # models.py (contrat), ffmpeg, detect/, audio/, reframe, render
│   ├── llm/                      # client OpenAI-compatible + prompts versionnés
│   └── motion/                   # bridge Python → Remotion (props JSON, appel `npx remotion render`)
├── motion/                       # projet Remotion (TS) : Title, PopUp, ProgressBar, Reaction, Captions
├── tests/                        # unitaires + e2e sur vidéo synthétique (ffmpeg lavfi)
└── docs/
```

> Écart assumé vs la spec : `src/core`, `src/llm`, `src/motion` deviennent
> `src/minavio/{core,llm,motion}` pour éviter des noms de paquets génériques
> (`core`, `llm`) qui entrent en collision une fois installés.

## 4. Extensibilité (nouveaux jeux / concepts)

- **Détecteurs = plugins** : registre `@register_detector("killfeed")`. Un profil
  YAML liste les détecteurs + poids ; un nouveau jeu = un YAML dans
  `configs/games/` + ses images de templates, zéro code.
- **Concepts de vidéo** = `content_type` + `editing.pacing` + liste de templates
  motion. Un nouveau format (ex. « fails compilation ») est un nouveau profil.
- **Templates motion** = composants Remotion enregistrés par nom ; le YAML les
  référence par nom, le Python ne connaît que leurs props.

## 5. Subagents

| Agent | Domaine (écrit uniquement dans) | Modèle | Isolation |
| :--- | :--- | :--- | :--- |
| orchestrator (session principale) | plan, `core/models.py`, revue, merge | `inherit` | — |
| `motion-design-agent` | `motion/`, `src/minavio/motion/` | `sonnet` | worktree |
| `media-pipeline-agent` | `src/minavio/core/` (sauf `models.py`) | `sonnet` | worktree |
| `local-llm-agent` | `src/minavio/llm/` | `sonnet` | worktree |
| `cli-config-agent` | `cli.py`, `config/`, `configs/` | `haiku` | worktree |

Les identifiants `claude-3-*` de la spec sont obsolètes : les agents utilisent
les alias `opus` / `sonnet` / `haiku` / `inherit`, qui suivent toujours la
dernière version.

**Règle anti-conflit** : seul l'orchestrateur modifie `core/models.py`. Un agent
qui a besoin d'un champ le demande dans son rapport final.

## 6. Répartition des coûts

| Tâche | Exécutée par | Tokens Claude |
| :--- | :--- | :--- |
| Écrire / corriger le code | Claude Code | oui (ponctuel) |
| Lire transcripts, choisir les moments, titres, hooks | Qwen local | non |
| Template matching, VAD, beats, ASR, rendu | Machine hôte | non |

Le pipeline `minavio run` ne contacte jamais l'API Anthropic.

## 7. Plan Phase 2 (après validation)

Ordre : l'orchestrateur fige `core/models.py` (déjà en place) → lancement en
parallèle des 4 subagents en worktree → merge → Phase 3 (e2e sur vidéo générée
par `ffmpeg -f lavfi`, mock du serveur LLM, `ruff check`).

## 8. Points à valider

1. **Remotion** : licence gratuite pour individus / sociétés ≤ 3 personnes —
   OK pour toi ? Alternative 100 % Python : overlays PIL/Skia + FFmpeg (moins riche).
2. **Serveur LLM** : Ollama (simple) ou vLLM (plus rapide mais Qwen 30B en FP16
   ne tient pas dans 16 Go → il faut un quant AWQ/GPTQ). Par défaut :
   **Ollama + `qwen3:30b-a3b`** (MoE, ~3B actifs, tourne bien avec offload partiel).
3. **Sources** : fichiers locaux uniquement, ou aussi téléchargement (VOD
   Twitch/YouTube via `yt-dlp`) pour The Lost Chat ?
4. **Sous-titres** : animés mot-à-mot (style TikTok) pour les 3 chaînes ?

## 9. Décisions validées (2026-10-19)

1. **Motion** : Remotion.
2. **LLM** : serveur local déjà installé par l'utilisateur ; client OpenAI-compatible, `base_url` et
   `model` réglables par YAML ou `MINAVIO_LLM_BASE_URL` / `MINAVIO_LLM_MODEL`.
3. **Sources** : clips enregistrés en local par un outil de capture (Outplayed) ; `minavio run`
   accepte des fichiers et des dossiers. Pas de téléchargement.
4. **Sous-titres** : mot-à-mot style TikTok/Reels, **thème pixel / rétro** sur les 3 chaînes
   (fonte arcade *Press Start 2P* — OFL, via `@remotion/google-fonts` — rendu sans anti-aliasing,
   pop en paliers, contour en blocs). `TranscriptSegment.words` porte les timestamps par mot.

## 10. État après la Phase 3 (intégration)

Les 4 modules sont fusionnés ; `ruff` est propre et les 100 tests passent, dont un test de bout en bout
(clips synthétiques → `run_job` → MP4 16:9 + 9:16, LLM simulé par un serveur HTTP local).
Un rendu complet avec Remotion réel (titre, sous-titres et pop-up en ProRes 4444 alpha) a été vérifié.

Corrections d'intégration faites par l'orchestrateur :
- `TranscriptSegment.source` : les captions sont rattachées au bon clip en multi-sources.
- `remap_popups` : les `at` du copy (exprimés dans l'ordre des highlights) sont recalés sur la
  timeline montée ; popups hors montage supprimés, chevauchements filtrés.
- Fonte *Press Start 2P* embarquée (`motion/public/fonts`, OFL) : rendu Remotion hors ligne.
- `face_track` : détecteur YuNet embarqué (`core/assets`, Apache-2.0) ; OpenCV 5 ne fournit plus les
  cascades Haar et MediaPipe 1.x exige un téléchargement de modèle.
- Titre placé en haut quand les sous-titres sont centrés (The Lost Chat).

Limites connues :
- Prompts Qwen non encore validés sur le vrai modèle ; ASR faster-whisper jamais chargé ici (pas de GPU).
- Templates PNG d'Apex Legends à fournir (`configs/games/apex_legends/templates/`) : sans eux,
  `template_match` est ignoré avec un warning et la détection repose sur l'audio et la voix.
- `loudnorm` en une passe ; `action_track` = centroïde de mouvement OpenCV.

## 11. Base de connaissances et genres de jeux

`src/minavio/llm/kb.py` indexe des fiches Markdown (`src/minavio/llm/knowledge/`, plus les fiches
utilisateur de `configs/knowledge/`) et injecte les passages pertinents dans les prompts de `rank`,
`copy`, `chapters`, `thumbnails` et `vision_scan`. Le prompt `gaming.md` reste neutre : ce qui fait
un bon moment dépend du **genre** du jeu, pas de la chaîne.

- `GameProfile.genre` (battle_royale, tactical_fps, soulslike, horror, coop_pve, ...) et
  `GameProfile.modes` (pvp, pve, solo, coop) sont lus dans `configs/games/<id>/game.yaml`
  (`minavio.config.load_game`, résultat mis en cache, jamais bloquant : un jeu absent ou invalide
  est ignoré).
- Une fiche déclare `genre=a,b` et/ou `mode=pve` dans sa directive `<!-- kb: ... -->` : elle
  s'applique si au moins un jeu du profil a l'un de ces genres (et l'un de ces modes). Sans `genre`,
  une fiche `content_type=gaming` reste générique.
- Fiches par genre : `knowledge/genres/*.md` (soulslike, horror, coop_pve, action_adventure,
  roguelike, sandbox, moba, fighting, sports, extraction_shooter, tactical_fps) ; chacune décrit les
  moments forts, les signaux d'interface et audio, et les faux positifs.
  `games/fps_battle_royale.md` ne vise que battle_royale, tactical_fps, arena_shooter et
  extraction_shooter (plus de killfeed injecté pour un jeu PvE ou solo).
- Les requêtes de recherche sont complétées par `kb.genre_query(profile)` (mots-clés du genre et des
  modes). `vision_scan` accepte les événements PvE/solo (boss_kill, death, near_death, jumpscare,
  victory, goal, overtake, combo, discovery, build_reveal) en plus de kill/clutch/chaos.
- Ajouter un genre : une fiche `genres/<genre>.md` avec sa directive, et ses mots-clés dans
  `kb.GENRE_TERMS`. `MINAVIO_KB=0` désactive toute l'injection.
