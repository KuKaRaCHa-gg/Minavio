"""Connecteurs LLM : local (Qwen via Ollama/vLLM) par défaut, Claude / Gemini / GitHub Models /
OpenAI-compatible en option et en secours (`llm.providers`)."""

from minavio.llm.client import LocalLLM
from minavio.llm.copywriting import Popup, VideoCopy, generate_copy
from minavio.llm.providers import LLMChain
from minavio.llm.rank import rank_highlights
from minavio.llm.vision import pick_thumbnails

__all__ = [
    "LLMChain",
    "LocalLLM",
    "Popup",
    "VideoCopy",
    "generate_copy",
    "pick_thumbnails",
    "rank_highlights",
]
