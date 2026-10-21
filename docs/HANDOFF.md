# Minavio — reprise (pour Claude Code en local)

À lire en début de session, avec `CLAUDE.md` (règles, routage des agents) et `docs/ARCHITECTURE.md`.

## État (21/10/2026)
- `main` = branche validée. Développement sur `claude/clever-ride-bdvhpa`, promu sur `main` après
  chaque lot (`git push origin claude/clever-ride-bdvhpa:main`). 595 tests verts, ruff propre.
- Rendu de bout en bout vérifié (16:9 + 9:16 + Shorts + miniatures) avec un faux Qwen.

## Ce que fait l'app (carte des modules)
| Domaine | Fichiers |
| :--- | :--- |
| Orchestration, étapes JSON `work/<job>/`, `--resume` | `src/minavio/pipeline.py` |
| Contrats | `core/models.py` (orchestrateur seul), `config/schema.py` |
| Détection | `core/detect/` : killfeed_ocr (RapidOCR), motion_intensity, template_match, audio_peak, hype, speech, scene_change, vision_scan |
| Montage | `core/edit.py` (cold open, countdown, ralenti), `core/autocut.py`, `core/sfx.py` |
| Rendu | `core/render.py`, `core/reframe.py`, `core/interpolate.py` (RIFE / minterpolate) |
| Robustesse | `core/healing.py` (replis déterministes, `healing.json`), réparation des sources |
| Audio | `core/audio/` (beats, mix, vad, separate = Demucs, denoise = DeepFilterNet/arnndn, tracks) |
| LLM | `llm/` : chaîne de fournisseurs, rank, copy, chapitres, vision (miniatures), vision_scan, `kb.py` + `knowledge/` (BM25, fiches par jeu et par genre) |
| Motion | `motion/` (Remotion 4, Press Start 2P), repli FFmpeg/Pillow |
| Publication | `publish/` (OAuth YouTube, upload idempotent), `minavio publish` |
| Ingestion | `watch.py`, `minavio watch`, `scripts/minavio-watch.bat` |
| Presets | `config/presets.py` (thèmes nommés), `minavio profiles new` / `preset create` |
| UI | `ui/` (Gradio, 127.0.0.1), `minavio ui`, `scripts/minavio-ui.bat` |
| Profils | `configs/channels/*.yaml` (5), `configs/games/*/game.yaml` (18 jeux, `genre` + `modes`) |

## Installation sur la machine hôte
```bash
uv sync --all-extras
(cd motion && npm install)
ollama pull qwen3:30b-a3b        # texte (classement, titres, chapitres)
ollama pull qwen3-vl:8b          # vision locale (vision_scan, miniatures)
uv run minavio profiles validate && uv run minavio llm ping minivitch
```
Premier usage : Demucs télécharge ses poids (~80 Mo). RIFE : installer `rife-ncnn-vulkan` dans le
PATH ou `MINAVIO_RIFE_BIN` (sinon repli `minterpolate`).

## Invariants à ne jamais casser
- Le pipeline n'appelle aucune API cloud sauf fournisseur configuré explicitement ; local par défaut.
- `--no-llm` n'envoie rien à aucun LLM (vision_scan compris).
- vision_scan / miniatures : images au cloud seulement si `allow_cloud: true` ET `vision_model` explicite.
- Clés hors dépôt : variables d'env ou `%APPDATA%\minavio\secrets.env` ; jamais affichées en clair.
- Publication YouTube jamais automatique (ni `run`, ni `watch`) ; confirmation explicite ; idempotente ;
  une vidéo programmée est forcément `public`.
- FFmpeg en liste d'arguments, jamais `shell=True` ; imports lourds dans les fonctions.

## Méthode de travail
- Un agent par domaine (`.claude/agents/`, worktree isolé) ; l'agent commence par
  `git merge --ff-only <hash courant>`. Budget serré : `haiku` par défaut, `sonnet` seulement pour
  FFmpeg/OpenCV/Remotion lourds.
- Avant commit : `uv run ruff check . && uv run ruff format --check . && uv run pytest -q`.
- Revue `orchestrator` avant chaque promotion sur `main`.
- Piège : ne jamais `pkill -f` avec un motif qui matche le shell courant.

## À valider en conditions réelles (pas testable dans le cloud)
1. Textes OCR des jeux marqués `# à confirmer` (`configs/games/<jeu>/game.yaml`), surtout en FR :
   lancer `minavio run <profil> clip.mp4 --dry-run -v` et corriger avec ce que l'OCR lit.
2. Publication YouTube réelle (`minavio publish auth`, puis `--dry-run`, puis en privé).
3. Options exactes du vrai binaire rife-ncnn-vulkan (`-i/-o/-n/-f`).
4. Seuils de `motion_intensity` et de `killfeed_ocr` sur du vrai gameplay Apex.
5. `minavio watch` sur le vrai dossier Outplayed ; auto-healing sur une vraie erreur GPU.

## Backlog (validé, pas commencé)
- Analytics YouTube : variantes titre/miniature générées, **validées en 1 clic** (pas d'auto-update).
  Vérifier d'abord si l'API Analytics expose le CTR d'impressions.
- Traduction des sous-titres (EN/ES) par Qwen ; doublage seulement avec Piper (MIT), pas XTTS.
- Commentaire de lancement généré par Qwen et publié ; l'épinglage reste manuel (pas d'API).
- Veille tendances en local (Planificateur Windows + Qwen) : Reddit, Twitch ; pas X (payant).
- Thèmes Remotion distincts par preset (aujourd'hui : couleurs/police/style seulement).
- PR Copilot #6 (installateur Windows), #7 (README EN), #8 (CI) : basées sur un ancien `main`,
  à remettre à jour ; #14 vide, à fermer.
