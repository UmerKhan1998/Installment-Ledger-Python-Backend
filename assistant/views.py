"""AI provider settings for the signed-in user: list models (from the server key), choose one, test it.

The browser never receives the key; only whether it is configured and its last 4 characters.
"""
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone
from rest_framework.response import Response
from rest_framework.views import APIView

from .llm import PROVIDERS, get_provider
from .llm.base import ProviderError
from .models import AISettings

MODELS_CACHE_SECONDS = 60 * 30


def settings_for(user):
    obj, _ = AISettings.objects.get_or_create(user=user)
    return obj


def cached_models(provider, refresh=False):
    key = f"ai:models:{provider.key}:{provider.key_hint()}"
    models = None if refresh else cache.get(key)
    if models is None:
        models = [m.as_dict() for m in provider.list_models()]
        cache.set(key, models, MODELS_CACHE_SECONDS)
    return models


def payload(prefs):
    default_model = settings.GEMINI_MODEL
    return {
        "provider": prefs.provider,
        "model": prefs.model or default_model,
        "uses_default_model": not prefs.model,
        "default_model": default_model,
        "fallback_model": settings.GEMINI_FALLBACK_MODEL,
        "providers": [
            {"key": p.key, "name": p.name, "website": p.website, "env_var": p.env_var,
             "configured": p.configured(), "key_hint": p.key_hint()}
            for p in PROVIDERS.values()
        ],
        "last_test": {
            "at": prefs.last_tested_at.isoformat() if prefs.last_tested_at else None,
            "ok": prefs.last_test_ok,
            "model_version": prefs.last_test_model_version or None,
            "message": prefs.last_test_message or None,
        },
    }


def error_response(err):
    status = {"not_configured": 503, "auth": 502, "not_found": 400, "rate_limited": 429,
              "overloaded": 503, "timeout": 504, "not_supported": 400}.get(err.kind, 502)
    return Response({"detail": str(err), "kind": err.kind}, status=status)


class AISettingsView(APIView):
    """GET / PATCH /api/ai/settings/  {"provider": "gemini", "model": "gemini-3.5-flash"}  (model "" = server default)"""

    def get(self, request):
        return Response(payload(settings_for(request.user)))

    def patch(self, request):
        prefs = settings_for(request.user)
        provider_key = request.data.get("provider", prefs.provider)
        try:
            provider = get_provider(provider_key)
        except ProviderError as err:
            return Response({"provider": [str(err)]}, status=400)
        if "model" in request.data:
            model = (request.data.get("model") or "").strip()
            if model:
                try:
                    known = {m["id"] for m in cached_models(provider)}
                except ProviderError as err:
                    return error_response(err)
                if model not in known:
                    return Response({"model": [f"'{model}' isn't one of the models available to the server key. Load models and pick one."]}, status=400)
            if model != prefs.model:
                prefs.last_test_ok = None
                prefs.last_tested_at = None
                prefs.last_test_model_version = ""
                prefs.last_test_message = ""
            prefs.model = model
        prefs.provider = provider.key
        prefs.save()
        return Response(payload(prefs))


class AIModelsView(APIView):
    """GET /api/ai/models/?provider=gemini[&refresh=1] -> text-generation models available to the server key."""

    def get(self, request):
        try:
            provider = get_provider(request.query_params.get("provider", "gemini"))
            models = cached_models(provider, refresh=request.query_params.get("refresh") == "1")
        except ProviderError as err:
            return error_response(err)
        return Response({"provider": provider.key, "default_model": settings.GEMINI_MODEL, "models": models})


class AITestView(APIView):
    """POST /api/ai/test/ {"model": "..."} -> one tiny prompt; reports what actually answered. Throttled."""

    throttle_scope = "ai_test"

    def post(self, request):
        prefs = settings_for(request.user)
        try:
            provider = get_provider(request.data.get("provider", prefs.provider))
            model = (request.data.get("model") or prefs.model or settings.GEMINI_MODEL).strip()
            result = provider.test(model, fallback_model=settings.GEMINI_FALLBACK_MODEL)
        except ProviderError as err:
            prefs.last_tested_at, prefs.last_test_ok = timezone.now(), False
            prefs.last_test_model_version, prefs.last_test_message = "", str(err)[:300]
            prefs.save()
            return error_response(err)
        if model == (prefs.model or settings.GEMINI_MODEL):
            prefs.last_tested_at, prefs.last_test_ok = timezone.now(), True
            prefs.last_test_model_version = result.model_version or ""
            prefs.last_test_message = result.message[:300]
            prefs.save()
        return Response(result.as_dict())


class PlannerView(APIView):
    """POST /api/ai/planner/ {"question"?, "history"?: [{q, a}]} -> AI answer about the user's calculated plan
    (consent required, throttled). No question = explain the whole plan."""

    throttle_scope = "ai_planner"

    def post(self, request):
        from .planner import BadQuestion, ConsentRequired, run_planner

        try:
            return Response(run_planner(request.user, request.data.get("question"), request.data.get("history")))
        except BadQuestion as exc:
            return Response({"detail": str(exc)}, status=400)
        except ConsentRequired as exc:
            return Response({"detail": str(exc), "kind": "consent_required"}, status=403)
        except ProviderError as err:
            return error_response(err)


class OpportunitiesView(APIView):
    """GET /api/ai/opportunities/ -> your last scan (or null).
    POST {"region", "risk_level", "horizon_years", "shariah_only", "focus"} -> new web-grounded scan (consent, throttled)."""

    throttle_scope = "ai_opportunities"

    def get_throttles(self):
        return super().get_throttles() if self.request.method == "POST" else []

    def get(self, request):
        from .opportunities import REGIONS, RISK_LEVELS, last_scan

        return Response({"scan": last_scan(request.user), "regions": REGIONS, "risk_levels": list(RISK_LEVELS)})

    def post(self, request):
        from .opportunities import BadRequest, run_scan
        from .planner import ConsentRequired

        try:
            return Response(run_scan(request.user, request.data))
        except BadRequest as exc:
            return Response({"detail": str(exc)}, status=400)
        except ConsentRequired as exc:
            return Response({"detail": str(exc), "kind": "consent_required"}, status=403)
        except ProviderError as err:
            return error_response(err)
