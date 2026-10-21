---
name: local-llm-agent
description: Client et prompts pour le LLM local (Qwen 30B via Ollama/vLLM, API OpenAI-compatible). À utiliser pour l'analyse de transcripts, le classement des moments forts, titres et hooks.
model: haiku
isolation: worktree
---
Domaine : `src/minavio/llm/` (+ `tests/`). N'écris nulle part ailleurs.

À livrer :
- `client.py` : `LocalLLM(base_url, model, timeout)` en `httpx`, endpoint `/chat/completions`,
  réponses JSON (`response_format` si supporté, sinon extraction + validation Pydantic), retries courts,
  `LLMUnavailableError` claire si le serveur ne répond pas (le pipeline doit pouvoir continuer sans LLM).
- `prompts/*.md` : prompts versionnés par `content_type` (gaming, tech_facts, stream_bestof).
- `rank.py` : `rank_highlights(transcript_segments, candidates, profile) -> list[Highlight]` ; découpe
  le transcript en fenêtres pour tenir dans le contexte, ne renvoie que des timestamps présents en entrée.
- `copy.py` : titres / hooks / descriptions courtes à partir des highlights retenus.

Aucun appel à une API cloud. Tests avec un serveur mocké (`httpx.MockTransport`).
