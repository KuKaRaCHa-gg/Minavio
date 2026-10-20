#!/usr/bin/env sh
# Ouvre l'interface Minavio dans le navigateur.
cd "$(dirname "$0")/.." && exec uv run minavio ui "$@"
