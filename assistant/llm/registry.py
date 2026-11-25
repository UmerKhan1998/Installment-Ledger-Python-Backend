from .base import ProviderError
from .gemini import GeminiProvider

# Add providers here (each implements llm.base.LLMProvider). Gemini is the chosen provider (agents.md §21).
PROVIDERS = {p.key: p for p in (GeminiProvider(),)}


def get_provider(key):
    if key not in PROVIDERS:
        raise ProviderError(f"Unknown AI provider '{key}'.", kind="error")
    return PROVIDERS[key]
