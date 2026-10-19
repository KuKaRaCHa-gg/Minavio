---
name: cli-config-agent
description: CLI Typer et profils YAML des chaînes (minivitch, bytetricks, thelostchat) et des jeux. À utiliser pour ajouter une commande, un réglage de profil ou un nouveau jeu.
model: haiku
isolation: worktree
---
Domaine : `src/minavio/cli.py`, `src/minavio/config/`, `configs/` (+ `tests/`). N'écris nulle part ailleurs.

À livrer :
- Commandes : `minavio profiles list|show|validate`, `minavio run <profile> <input>... [--format 16:9|9:16|all] [--no-llm] [--dry-run]`,
  `minavio schema` (export JSON Schema des profils).
- `run` enchaîne les étapes du pipeline en appelant les fonctions publiques des autres modules ;
  chaque étape écrit son JSON dans `work/<job_id>/` et peut être reprise (`--resume`).
- Variables d'env `MINAVIO_LLM_BASE_URL`, `MINAVIO_LLM_MODEL` surchargent le YAML.
- Messages d'erreur actionnables, code retour non nul.
