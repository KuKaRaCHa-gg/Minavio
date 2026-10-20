# Minavio

Moteur de montage automatique pour **Minivitch**, **Byte Tricks** et **The Lost Chat** :
détection locale des moments forts (OpenCV, VAD, audio), classement par LLM local (Qwen 30B),
beat-sync, ducking, motion design pixel/rétro (Remotion), rendu simultané 16:9 et 9:16.
Aucun appel à une API cloud pendant un rendu.

## Installation (machine hôte)

```bash
uv sync --all-extras                 # OpenCV, librosa, Silero VAD, faster-whisper
(cd motion && npm install)           # templates Remotion (Node ≥ 18)
uv run minavio profiles validate
```

LLM local : n'importe quel serveur OpenAI-compatible (Ollama, vLLM, LM Studio…).

```bash
export MINAVIO_LLM_BASE_URL=http://localhost:11434/v1   # défaut : Ollama
export MINAVIO_LLM_MODEL=qwen3:30b-a3b                  # le nom de TON modèle
uv run minavio llm ping minivitch
```

## Interface graphique

```bash
uv sync --all-extras          # inclut l'interface (extra `ui`, Gradio)
uv run minavio ui             # ouvre http://127.0.0.1:7860 dans le navigateur
```

Sous Windows : double-clic sur `scripts/minavio-ui.bat`. L'interface n'écoute que sur ta machine.

| Onglet | Rôle |
| :--- | :--- |
| 🎬 Montage | chaîne, dossier de clips, musique, formats, Shorts, Qwen/chapitres ; progression par étape, annulation, vidéos produites, titre/description/chapitres |
| 🎯 Captures de jeu | upload d'une capture d'écran, recadrage de l'élément (marqueur de kill…), enregistrement au bon endroit |
| ⚙️ Réglages | édition des profils YAML, vérifiés avant enregistrement (`.bak` conservé) |
| 🩺 Système | FFmpeg, Node/Remotion, modules installés, GPU, test de connexion au Qwen |

## Utilisation en ligne de commande

```bash
# Un dossier de clips Outplayed → out/minivitch/<job>_youtube.mp4 et <job>_shorts.mp4
uv run minavio run minivitch ~/Videos/Outplayed/Apex --music ~/music/track.mp3

uv run minavio run thelostchat clips/ --format 9:16      # un seul format
uv run minavio run bytetricks clips/ --dry-run           # s'arrête à work/<job>/timeline.json
uv run minavio run minivitch clips/ --resume             # reprend après une erreur / un réglage
uv run minavio run minivitch clips/ --no-llm             # sans LLM (classement par score)
```

Chaque étape écrit son JSON dans `work/<job_id>/` (detect, transcribe, rank, copy, beats,
timeline, overlays…) : on peut l'inspecter, le corriger à la main et relancer avec `--resume`.

| Variable | Rôle |
| :--- | :--- |
| `MINAVIO_LLM_BASE_URL`, `MINAVIO_LLM_MODEL` | serveur / modèle LLM local |
| `MINAVIO_WHISPER_MODEL` | modèle faster-whisper (défaut `large-v3` sur CUDA, `small` sur CPU) |
| `MINAVIO_BROWSER_EXECUTABLE` | Chromium à utiliser par Remotion (sinon il le télécharge) |
| `MINAVIO_MOTION_DIR`, `MINAVIO_FONT` | projet Remotion / fonte du repli FFmpeg |

## Pour aller plus loin

- Architecture, décisions et limites : [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
- Règles Claude Code et routage des agents : [`CLAUDE.md`](CLAUDE.md)
- Nouveau jeu : [`configs/games/README.md`](configs/games/README.md)
- Templates motion : [`motion/README.md`](motion/README.md)

## Développement

```bash
uv run ruff check . && uv run ruff format --check . && uv run pytest -q
```

`tests/test_e2e.py` génère des clips synthétiques, simule le serveur Qwen sur localhost et
rend les deux formats avec le vrai FFmpeg/OpenCV (~1 min).
