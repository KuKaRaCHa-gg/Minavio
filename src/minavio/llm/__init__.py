"""Connecteur LLM local (Qwen via Ollama/vLLM, API OpenAI-compatible). Aucun appel cloud."""

from minavio.llm.client import LocalLLM
from minavio.llm.copywriting import Popup, VideoCopy, generate_copy
from minavio.llm.rank import rank_highlights

__all__ = ["LocalLLM", "Popup", "VideoCopy", "generate_copy", "rank_highlights"]
