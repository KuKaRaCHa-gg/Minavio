# Minavio

Moteur de montage automatique pour **Minivitch**, **Byte Tricks** et **The Lost Chat** :
détection locale des moments forts (OpenCV, VAD, audio), classement par LLM local (Qwen 30B),
beat-sync, ducking, motion design programmatique, rendu simultané 16:9 et 9:16.

```bash
uv sync
uv run minavio profiles validate
uv run minavio profiles show minivitch
uv run pytest -q
```

- Architecture et plan : [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
- Règles Claude Code et routage des agents : [`CLAUDE.md`](CLAUDE.md)
- Nouveau jeu : copier `configs/games/apex_legends/`, remplacer les templates PNG.
