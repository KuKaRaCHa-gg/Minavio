# Minavio

[🇫🇷 Français](README.md)

Automatic editing engine for **Minivitch**, **Byte Tricks**, and **The Lost Chat**:
local highlight detection (OpenCV, VAD, audio), ranking by local LLM (Qwen 30B),
beat-sync, ducking, pixel/retro motion design (Remotion), simultaneous 16:9 and 9:16 rendering.
No cloud API call during a render.

## Installation (host machine)

```bash
uv sync --all-extras                 # OpenCV, librosa, Silero VAD, faster-whisper
(cd motion && npm install)           # Remotion templates (Node ≥ 18)
uv run minavio profiles validate
```

Local LLM: any OpenAI-compatible server (Ollama, vLLM, LM Studio…).

```bash
export MINAVIO_LLM_BASE_URL=http://localhost:11434/v1   # default: Ollama
export MINAVIO_LLM_MODEL=qwen3:30b-a3b                  # the name of YOUR model
uv run minavio llm ping minivitch
```

## Graphical interface

```bash
uv sync --all-extras          # includes the interface (extra `ui`, Gradio)
uv run minavio ui             # opens http://127.0.0.1:7860 in the browser
```

On Windows: double-click `scripts/minavio-ui.bat`. The interface only listens on your machine.

| Tab | Role |
| :--- | :--- |
| 🎬 Editing | channel / profile, clips folder, music, formats, Shorts, Qwen/chapters; step-by-step progress, cancellation, produced videos, title/description/chapters |
| 🖼️ Thumbnails | choose a recent job (`work/`), custom text and layout (hero / split / collage), generate and download PNGs |
| 🎯 Game captures | upload a screenshot, crop the element (kill marker, etc.), save it in the right place |
| ⚙️ Settings | edit YAML profiles, validated before saving (`.bak` kept) |
| 🩺 System | FFmpeg, Node/Remotion, installed modules, GPU, test each LLM provider, API key management |

## Command-line usage

```bash
# One Outplayed clips folder → out/minivitch/<job>_youtube.mp4 and <job>_shorts.mp4
uv run minavio run minivitch ~/Videos/Outplayed/Apex --music ~/music/track.mp3

uv run minavio run thelostchat clips/ --format 9:16      # single format
uv run minavio run bytetricks clips/ --dry-run           # stops at work/<job>/timeline.json
uv run minavio run minivitch clips/ --resume             # resumes after an error / a setting change
uv run minavio run minivitch clips/ --no-llm             # without LLM (score-based ranking)
```

Each step writes its JSON into `work/<job_id>/` (detect, transcribe, rank, copy, beats,
timeline, overlays…): you can inspect it, fix it manually, and relaunch with `--resume`.

| Variable | Role |
| :--- | :--- |
| `MINAVIO_LLM_BASE_URL`, `MINAVIO_LLM_MODEL` | local LLM server / model |
| `MINAVIO_WHISPER_MODEL` | faster-whisper model (default `large-v3` on CUDA, `small` on CPU) |
| `MINAVIO_BROWSER_EXECUTABLE` | Chromium used by Remotion (otherwise it downloads it) |
| `MINAVIO_MOTION_DIR`, `MINAVIO_FONT` | Remotion project / FFmpeg fallback font |

## Thumbnails

If `thumbnails.enabled` (profile), `minavio run` produces thumbnails after renders:
`out/<profil>/<job>_thumb<i>_<format>.png` (can be disabled with `--no-thumbnails`). To regenerate
them from an existing job, with forced text if needed:

```bash
uv run minavio thumbnails minivitch <job_id> --text "3 KILLS EN 5 SECONDES" --layout hero
```

## LLM providers, cost, and privacy

By default, everything stays local (Qwen via Ollama). A profile can declare an `llm.providers`
chain (local, Claude, Gemini, GitHub Models, OpenAI-compatible API) tried in order, for example
local first and Claude as fallback (see `configs/README.md`).

- **Cloud APIs are paid** (Claude, Gemini, OpenAI) and **send your transcripts and images to the provider**:
  enable them only if that works for you. GitHub Models (Copilot subscription) is limited
  (~8k input tokens, a few requests/min): keep it for fallback.
- Keys are never in YAML: environment variables or local file
  `~/.config/minavio/secrets.env` (`%APPDATA%\minavio\secrets.env` on Windows, permissions 600).

```bash
uv run minavio llm set-key ANTHROPIC_API_KEY   # hidden input (also GEMINI_API_KEY, OPENAI_API_KEY, GITHUB_TOKEN)
uv run minavio llm keys                        # key status, without values
uv run minavio llm ping minivitch              # tests each provider in the chain
```

## Go further

- Architecture, decisions, and limits: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
- Claude Code rules and agent routing: [`CLAUDE.md`](CLAUDE.md)
- New game: [`configs/games/README.md`](configs/games/README.md)
- Motion templates: [`motion/README.md`](motion/README.md)

## Development

```bash
uv run ruff check . && uv run ruff format --check . && uv run pytest -q
```

`tests/test_e2e.py` generates synthetic clips, simulates the Qwen server on localhost, and
renders both formats with real FFmpeg/OpenCV (~1 min).
