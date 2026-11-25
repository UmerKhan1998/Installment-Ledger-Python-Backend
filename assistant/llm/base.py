"""Provider interface and typed results. Providers raise ProviderError with a user-safe message (never the key)."""
from dataclasses import asdict, dataclass, field


class ProviderError(Exception):
    """A failure that is safe to show to the user. `kind` lets callers decide on retry/fallback."""

    def __init__(self, message, kind="error", status=None):
        super().__init__(message)
        self.kind = kind  # not_configured | auth | not_found | overloaded | rate_limited | timeout | error
        self.status = status


@dataclass
class ModelInfo:
    id: str
    display_name: str
    description: str = ""
    input_token_limit: int | None = None
    output_token_limit: int | None = None
    stage: str = "stable"  # stable | preview | experimental | alias
    thinking: bool = False
    tags: list = field(default_factory=list)

    def as_dict(self):
        return asdict(self)


@dataclass
class TestResult:
    ok: bool
    requested_model: str
    model_version: str | None = None  # what the provider actually served (names can be redirected)
    reply: str = ""
    latency_ms: int = 0
    total_tokens: int | None = None
    used_fallback: bool = False
    message: str = ""

    def as_dict(self):
        return asdict(self)


class LLMProvider:
    key = ""
    name = ""
    website = ""
    env_var = ""

    def configured(self) -> bool:
        raise NotImplementedError

    def key_hint(self) -> str | None:
        """Last characters of the server key, for recognition only (never the key)."""
        raise NotImplementedError

    def list_models(self) -> list[ModelInfo]:
        raise NotImplementedError

    def test(self, model: str, fallback_model: str | None = None) -> TestResult:
        raise NotImplementedError

    def generate_json(self, model: str, system: str, prompt: str, schema, fallback_model: str | None = None):
        """Structured generation. Returns (text, meta) where meta has model_version, used_fallback,
        latency_ms, input_tokens, output_tokens. The caller validates `text` against `schema`."""
        raise NotImplementedError
