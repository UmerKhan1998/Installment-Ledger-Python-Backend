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
    """POST /api/ai/planner/ {"question"?, "chat_id"?, "page"?} -> AI answer about the user's calculated plan
    (consent required, throttled). No question = explain the whole plan. Each turn is saved in a chat ("Old chats"):
    without chat_id a new chat is started; with it, earlier turns of that chat are the follow-up context."""

    throttle_scope = "ai_planner"

    def post(self, request):
        from .chats import chat_summary, run_chat_turn
        from .models import PlannerChat
        from .planner import BadQuestion, ConsentRequired

        chat = None
        if request.data.get("chat_id") not in (None, ""):
            try:
                chat = PlannerChat.objects.get(pk=int(request.data["chat_id"]), user=request.user)
            except (PlannerChat.DoesNotExist, TypeError, ValueError):
                return Response({"detail": "Chat not found."}, status=404)
        try:
            out, chat = run_chat_turn(request.user, request.data.get("question"), chat, request.data.get("page", ""))
        except BadQuestion as exc:
            return Response({"detail": str(exc)}, status=400)
        except ConsentRequired as exc:
            return Response({"detail": str(exc), "kind": "consent_required"}, status=403)
        except ProviderError as err:
            res = error_response(err)
            if getattr(err, "chat", None) and err.chat.pk:
                res.data["chat"] = chat_summary(err.chat)
            return res
        return Response({**out, "chat": chat_summary(chat)})


class PlannerChatsView(APIView):
    """GET /api/ai/chats/?q=&period=7d|30d|90d&page=Invest -> the user's planner chats, newest first (max 100)."""

    def get(self, request):
        from .chats import chat_summary, search
        from .models import PlannerChat

        p = request.query_params
        chats = search(request.user, p.get("q", ""), p.get("period", ""), p.get("page", ""))[:100]
        pages = sorted(set(PlannerChat.objects.filter(user=request.user).exclude(page="").values_list("page", flat=True)))
        return Response({"chats": [chat_summary(c) for c in chats], "pages": pages})


class PlannerChatDetailView(APIView):
    """GET /api/ai/chats/{id}/ -> the chat with its messages; DELETE removes it. Only the owner's chats exist here."""

    def _chat(self, request, pk):
        from .models import PlannerChat

        return PlannerChat.objects.filter(pk=pk, user=request.user).first()

    def get(self, request, pk):
        from .chats import chat_summary, message_dict

        chat = self._chat(request, pk)
        if not chat:
            return Response({"detail": "Chat not found."}, status=404)
        return Response({**chat_summary(chat), "messages": [message_dict(m) for m in chat.messages.all()]})

    def delete(self, request, pk):
        chat = self._chat(request, pk)
        if not chat:
            return Response({"detail": "Chat not found."}, status=404)
        chat.delete()
        return Response(status=204)


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
