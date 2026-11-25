"""Google Gemini via the official Google Gen AI SDK (`google-genai`). Key from settings.GEMINI_API_KEY only."""
import logging
import re
import time

from django.conf import settings

from .base import LLMProvider, ModelInfo, ProviderError, TestResult

log = logging.getLogger(__name__)
TIMEOUT_MS = 20_000
RETRY_DELAY_S = 2
# Text-generation models only; drop media/speech/live/embedding/robotics variants and non-Gemini families.
EXCLUDE = re.compile(r"(tts|image|audio|live|embedding|robotics|computer-use|aqa|veo|imagen|banana|native)", re.I)


def _stage(model_id: str) -> str:
    if model_id.endswith("-latest"):
        return "alias"
    if "preview" in model_id:
        return "preview"
    if "exp" in model_id:
        return "experimental"
    return "stable"


class GeminiProvider(LLMProvider):
    key = "gemini"
    name = "Google Gemini"
    website = "https://aistudio.google.com"
    env_var = "GEMINI_API_KEY"

    def configured(self):
        return bool(settings.GEMINI_API_KEY)

    def key_hint(self):
        return settings.GEMINI_API_KEY[-4:] if self.configured() else None

    def _client(self):
        if not self.configured():
            raise ProviderError("GEMINI_API_KEY is not set on the server.", kind="not_configured")
        from google import genai
        from google.genai import types

        return genai.Client(api_key=settings.GEMINI_API_KEY, http_options=types.HttpOptions(timeout=TIMEOUT_MS))

    @staticmethod
    def _translate(exc):
        """SDK/network errors -> ProviderError with a safe message (no key, no raw payload)."""
        from google.genai import errors

        # Log the exception type and status only: messages can echo request details.
        log.warning("Gemini call failed: %s code=%s status=%s", type(exc).__name__, getattr(exc, "code", None), getattr(exc, "status", None))

        if isinstance(exc, errors.APIError):
            code, status = getattr(exc, "code", None), getattr(exc, "status", None) or ""
            if code in (401, 403) or status in ("PERMISSION_DENIED", "UNAUTHENTICATED"):
                return ProviderError("Gemini rejected the API key (check it's valid and allowed to use the Generative Language API).", "auth", code)
            if code == 404 or status == "NOT_FOUND":
                return ProviderError("This model isn't available to your key.", "not_found", code)
            if code == 429 or status == "RESOURCE_EXHAUSTED":
                return ProviderError("Gemini quota or rate limit reached. Try again later.", "rate_limited", code)
            if code in (500, 502, 503, 504) or status == "UNAVAILABLE":
                return ProviderError("Gemini is overloaded right now (temporary).", "overloaded", code)
            return ProviderError(f"Gemini returned an error ({code or status}).", "error", code)
        if "timed out" in str(exc).lower() or exc.__class__.__name__.lower().endswith("timeout"):
            return ProviderError("Gemini didn't answer in time.", "timeout")
        return ProviderError("Couldn't reach Gemini.", "error")

    def list_models(self):
        # Keep the client referenced while the pager is consumed: a temporary client can be garbage-collected
        # (closing its HTTP connection) before the lazy pager fetches its pages -> "client has been closed".
        client = self._client()
        try:
            raw = list(client.models.list(config={"page_size": 200}))
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001 - translated to a safe error
            raise self._translate(exc) from None
        models = []
        for m in raw:
            model_id = (m.name or "").split("/")[-1]
            actions = m.supported_actions or []
            if "generateContent" not in actions or not model_id.startswith("gemini") or EXCLUDE.search(model_id):
                continue
            models.append(ModelInfo(
                id=model_id,
                display_name=m.display_name or model_id,
                description=(m.description or "")[:300],
                input_token_limit=m.input_token_limit,
                output_token_limit=m.output_token_limit,
                stage=_stage(model_id),
                thinking=bool(getattr(m, "thinking", False)),
                tags=[t for t in ("flash", "pro", "lite") if t in model_id],
            ))
        order = {"stable": 0, "alias": 1, "preview": 2, "experimental": 3}
        return sorted(models, key=lambda x: (order[x.stage], x.id), reverse=False)

    def _ask(self, client, model):
        from google.genai import types

        started = time.monotonic()
        response = client.models.generate_content(
            model=model,
            contents="Reply with exactly: OK",
            config=types.GenerateContentConfig(max_output_tokens=64, temperature=0),
        )
        latency = int((time.monotonic() - started) * 1000)
        usage = getattr(response, "usage_metadata", None)
        return TestResult(
            ok=True,
            requested_model=model,
            model_version=getattr(response, "model_version", None),
            reply=(response.text or "").strip()[:40],
            latency_ms=latency,
            total_tokens=getattr(usage, "total_token_count", None),
        )

    def test(self, model, fallback_model=None):
        client = self._client()
        try:
            return self._ask(client, model)
        except Exception as exc:  # noqa: BLE001
            err = exc if isinstance(exc, ProviderError) else self._translate(exc)
        if err.kind in ("overloaded", "timeout") and fallback_model and fallback_model != model:
            try:
                result = self._ask(client, fallback_model)
                result.requested_model = model
                result.used_fallback = True
                result.message = f"{model} is overloaded; the fallback {fallback_model} answered."
                return result
            except Exception:  # noqa: BLE001 - report the original failure
                pass
        raise err

    def _structured(self, client, model, system, prompt, schema):
        from google.genai import types

        started = time.monotonic()
        response = client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=system,
                response_mime_type="application/json",
                response_schema=schema,
                temperature=0.2,
                max_output_tokens=4096,
            ),
        )
        usage = getattr(response, "usage_metadata", None)
        meta = {
            "model_version": getattr(response, "model_version", None),
            "latency_ms": int((time.monotonic() - started) * 1000),
            "input_tokens": getattr(usage, "prompt_token_count", None),
            "output_tokens": getattr(usage, "candidates_token_count", None),
            "used_fallback": False,
        }
        return response.text or "", meta

    def generate_json(self, model, system, prompt, schema, fallback_model=None):
        client = self._client()
        try:
            return self._structured(client, model, system, prompt, schema)
        except Exception as exc:  # noqa: BLE001
            err = exc if isinstance(exc, ProviderError) else self._translate(exc)
        if err.kind in ("overloaded", "timeout") and fallback_model and fallback_model != model:
            try:
                text, meta = self._structured(client, fallback_model, system, prompt, schema)
                meta["used_fallback"] = True
                return text, meta
            except Exception:  # noqa: BLE001 - report the original failure
                pass
        if err.kind == "overloaded":  # overload is usually seconds long: one more try on the chosen model
            time.sleep(RETRY_DELAY_S)
            try:
                return self._structured(client, model, system, prompt, schema)
            except Exception:  # noqa: BLE001 - report the original failure
                pass
        raise err

    # ---- Google Search grounding (opportunities research) ----

    GROUNDED_TIMEOUT_MS = 90_000

    def _grounded(self, client, model, system, prompt):
        from google.genai import types

        started = time.monotonic()
        response = client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=system,
                tools=[types.Tool(google_search=types.GoogleSearch())],
                temperature=0.2,
                max_output_tokens=8192,
            ),
        )
        cand = (response.candidates or [None])[0]
        parts = [p.text or "" for p in ((cand.content.parts if cand and cand.content else None) or []) if getattr(p, "text", None) is not None]
        gm = getattr(cand, "grounding_metadata", None) if cand else None
        chunks, supports, queries, entry_html = [], [], [], ""
        if gm:
            for ch in gm.grounding_chunks or []:
                web = getattr(ch, "web", None)
                chunks.append({"uri": getattr(web, "uri", "") or "", "title": getattr(web, "title", "") or "",
                               "domain": getattr(web, "domain", "") or ""} if web else None)
            for s in gm.grounding_supports or []:
                seg = s.segment
                if seg is None or seg.end_index is None:
                    continue
                supports.append({"part": seg.part_index or 0, "start": seg.start_index or 0, "end": seg.end_index,
                                 "chunks": list(s.grounding_chunk_indices or [])})
            queries = list(gm.web_search_queries or [])
            if gm.search_entry_point and gm.search_entry_point.rendered_content:
                entry_html = gm.search_entry_point.rendered_content
        usage = getattr(response, "usage_metadata", None)
        meta = {
            "model_version": getattr(response, "model_version", None),
            "latency_ms": int((time.monotonic() - started) * 1000),
            "input_tokens": getattr(usage, "prompt_token_count", None),
            "output_tokens": getattr(usage, "candidates_token_count", None),
            "used_fallback": False,
        }
        return {"parts": parts, "chunks": chunks, "supports": supports, "queries": queries, "entry_html": entry_html}, meta

    def generate_grounded(self, model, system, prompt, fallback_model=None):
        """Prose answer grounded with Google Search. Returns (grounding, meta); grounding has the text parts and the
        sources/supports exactly as Google returned them (segment offsets are UTF-8 byte offsets within a part)."""
        if not self.configured():
            raise ProviderError("GEMINI_API_KEY is not set on the server.", kind="not_configured")
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=settings.GEMINI_API_KEY, http_options=types.HttpOptions(timeout=self.GROUNDED_TIMEOUT_MS))
        try:
            return self._grounded(client, model, system, prompt)
        except Exception as exc:  # noqa: BLE001
            err = exc if isinstance(exc, ProviderError) else self._translate(exc)
        if err.kind in ("overloaded", "timeout") and fallback_model and fallback_model != model:
            try:
                grounding, meta = self._grounded(client, fallback_model, system, prompt)
                meta["used_fallback"] = True
                return grounding, meta
            except Exception:  # noqa: BLE001 - report the original failure
                pass
        if err.kind == "rate_limited":
            err = ProviderError("Gemini refused the web-search request: quota reached for this key. Grounding with Google "
                                "Search usually needs billing enabled on the key's Google Cloud project (AI Studio → Billing).",
                                "rate_limited", err.status)
        raise err
