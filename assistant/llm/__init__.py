"""LLM provider layer (agents.md §21). Business code talks to `get_provider(...)`, never to an SDK directly."""
from .registry import PROVIDERS, get_provider  # noqa: F401
