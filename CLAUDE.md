# Minavio — règles pour Claude Code

Moteur de montage automatique pour Minivitch, Byte Tricks, The Lost Chat.
Architecture complète : `docs/ARCHITECTURE.md`. État, reprise et backlog : `docs/HANDOFF.md`.
Paramétrage (clés, comptes, installation) : `docs/SETUP.md`.
Contexte du LLM local (Qwen) : `docs/QWEN_CONTEXT.md`.

## Rôle de la session principale (orchestrateur)
- Planifie, fige les contrats, revoit et fusionne le travail des subagents.
- Seul l'orchestrateur modifie `src/minavio/core/models.py` (contrat inter-modules).
- Délègue l'implémentation aux agents de `.claude/agents/` (worktrees isolés).

## Économie de tokens
- Le pipeline d'exécution n'appelle **jamais** l'API Anthropic : analyse de texte → LLM local
  (`src/minavio/llm/`), vision/audio/rendu → machine hôte.
- Ne pas lire de médias, de logs volumineux ou de transcripts : les résumer via le LLM local ou `head`.
- Lire le module ciblé, pas tout le dépôt.

## Conventions
- Python ≥ 3.11, `uv`, Pydantic v2, Typer. Imports lourds (cv2, librosa, torch) à l'intérieur des
  fonctions ou derrière un extra, pour que `minavio --help` reste instantané.
- FFmpeg via `subprocess` (liste d'arguments, jamais `shell=True`).
- Erreurs : exceptions de `minavio.errors`, message actionnable (quel fichier, quelle commande).
- Avant commit : `uv run ruff check . && uv run ruff format --check . && uv run pytest -q`.

## Routage des tâches → agent le plus adapté
Choisir l'agent selon le domaine touché ; une tâche qui touche plusieurs domaines est découpée.

| Tâche | Agent | Modèle | Pourquoi ce modèle |
| :--- | :--- | :--- | :--- |
| Contrat `models.py`, plan, arbitrage, merge | session principale | inherit | Décisions transverses |
| Revue avant merge | `orchestrator` | inherit | Doit voir les interactions entre modules |
| `filter_complex` FFmpeg, reframe, beat-sync, ducking, détecteurs OpenCV | `media-pipeline-agent` | sonnet | Logique média dense, bugs subtils |
| Templates Remotion, animations, sous-titres animés | `motion-design-agent` | sonnet | React/TS + timing visuel |
| Client Qwen, prompts, ranking, parsing JSON robuste | `local-llm-agent` | haiku | Fenêtrage de contexte et validation des sorties du LLM local |
| Commandes CLI, YAML de chaîne, nouveau jeu | `cli-config-agent` | haiku | Mécanique, schéma déjà fixé |
| Recherche dans le code (« où est X ? ») | `Explore` | haiku | Lecture seule, bon marché |

Escalade : si un agent `haiku` échoue deux fois sur la même tâche, la relancer avec `sonnet`.
