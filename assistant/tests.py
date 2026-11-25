from unittest import mock

from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APITestCase

from .llm.base import ModelInfo, ProviderError, TestResult
from .llm.gemini import GeminiProvider

SECRET = "AQ.secret-test-key-abcd"
MODELS = [ModelInfo(id="gemini-3.5-flash", display_name="Gemini 3.5 Flash"),
          ModelInfo(id="gemini-3.5-flash-lite", display_name="Gemini 3.5 Flash-Lite"),
          ModelInfo(id="gemini-omni-flash-preview", display_name="Omni", stage="preview")]


@override_settings(GEMINI_API_KEY=SECRET, GEMINI_MODEL="gemini-3.5-flash", GEMINI_FALLBACK_MODEL="gemini-3.5-flash-lite")
class AISettingsTests(APITestCase):
    def setUp(self):
        cache.clear()
        res = self.client.post("/api/auth/register/", {"email": "ai@example.com", "password": "Sup3r-secret!"}, format="json")
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {res.data['access']}")
        self.list_models = mock.patch.object(GeminiProvider, "list_models", return_value=MODELS)
        self.listed = self.list_models.start()
        self.addCleanup(self.list_models.stop)

    def assertNoSecret(self, res):
        self.assertNotIn(SECRET, res.content.decode())
        self.assertNotIn(SECRET[:-4], res.content.decode())

    def test_settings_show_key_status_but_never_the_key(self):
        res = self.client.get("/api/ai/settings/")
        self.assertEqual(res.status_code, 200)
        gem = res.data["providers"][0]
        self.assertEqual((gem["key"], gem["configured"], gem["key_hint"], gem["env_var"]), ("gemini", True, "abcd", "GEMINI_API_KEY"))
        self.assertEqual((res.data["model"], res.data["uses_default_model"]), ("gemini-3.5-flash", True))
        self.assertNoSecret(res)

    def test_load_models_is_cached_and_refreshable(self):
        first = self.client.get("/api/ai/models/")
        self.assertEqual([m["id"] for m in first.data["models"]], [m.id for m in MODELS])
        self.client.get("/api/ai/models/")
        self.assertEqual(self.listed.call_count, 1)
        self.client.get("/api/ai/models/?refresh=1")
        self.assertEqual(self.listed.call_count, 2)
        self.assertNoSecret(first)

    def test_choose_only_a_listed_model_and_reset_to_default(self):
        ok = self.client.patch("/api/ai/settings/", {"model": "gemini-3.5-flash-lite"}, format="json")
        self.assertEqual((ok.data["model"], ok.data["uses_default_model"]), ("gemini-3.5-flash-lite", False))
        bad = self.client.patch("/api/ai/settings/", {"model": "gpt-5"}, format="json")
        self.assertEqual(bad.status_code, 400)
        reset = self.client.patch("/api/ai/settings/", {"model": ""}, format="json")
        self.assertEqual((reset.data["model"], reset.data["uses_default_model"]), ("gemini-3.5-flash", True))
        self.assertEqual(self.client.patch("/api/ai/settings/", {"provider": "openai"}, format="json").status_code, 400)

    def test_test_connection_records_what_answered(self):
        result = TestResult(ok=True, requested_model="gemini-3.5-flash", model_version="gemini-3.6-flash", reply="OK", latency_ms=900)
        with mock.patch.object(GeminiProvider, "test", return_value=result) as tested:
            res = self.client.post("/api/ai/test/", {}, format="json")
        self.assertEqual(res.data["model_version"], "gemini-3.6-flash")
        tested.assert_called_once_with("gemini-3.5-flash", fallback_model="gemini-3.5-flash-lite")
        last = self.client.get("/api/ai/settings/").data["last_test"]
        self.assertEqual((last["ok"], last["model_version"]), (True, "gemini-3.6-flash"))

    def test_changing_model_clears_the_old_test(self):
        with mock.patch.object(GeminiProvider, "test", return_value=TestResult(ok=True, requested_model="gemini-3.5-flash")):
            self.client.post("/api/ai/test/", {}, format="json")
        self.client.patch("/api/ai/settings/", {"model": "gemini-3.5-flash-lite"}, format="json")
        self.assertIsNone(self.client.get("/api/ai/settings/").data["last_test"]["ok"])

    def test_provider_errors_are_safe_and_recorded(self):
        cases = [("overloaded", 503), ("auth", 502), ("rate_limited", 429), ("not_found", 400), ("timeout", 504)]
        for kind, status in cases:
            with self.subTest(kind=kind), mock.patch.object(GeminiProvider, "test", side_effect=ProviderError(f"{kind} happened", kind)):
                res = self.client.post("/api/ai/test/", {}, format="json")
                self.assertEqual((res.status_code, res.data["kind"]), (status, kind))
                self.assertNoSecret(res)
        self.assertFalse(self.client.get("/api/ai/settings/").data["last_test"]["ok"])

    @override_settings(GEMINI_API_KEY="")
    def test_missing_key(self):
        self.list_models.stop()
        info = self.client.get("/api/ai/settings/").data["providers"][0]
        self.assertEqual((info["configured"], info["key_hint"]), (False, None))
        res = self.client.get("/api/ai/models/?refresh=1")
        self.assertEqual((res.status_code, res.data["kind"]), (503, "not_configured"))
        self.list_models.start()

    def test_test_endpoint_is_rate_limited(self):
        from rest_framework.settings import api_settings
        with mock.patch.dict(api_settings.DEFAULT_THROTTLE_RATES, {"ai_test": "2/hour"}), \
                mock.patch.object(GeminiProvider, "test", return_value=TestResult(ok=True, requested_model="x")):
            codes = [self.client.post("/api/ai/test/", {}, format="json").status_code for _ in range(3)]
        self.assertEqual(codes, [200, 200, 429])

    def test_settings_are_per_user(self):
        self.client.patch("/api/ai/settings/", {"model": "gemini-3.5-flash-lite"}, format="json")
        other = self.client.post("/api/auth/register/", {"email": "ai2@example.com", "password": "Sup3r-secret!"}, format="json")
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {other.data['access']}")
        self.assertEqual(self.client.get("/api/ai/settings/").data["model"], "gemini-3.5-flash")


class GeminiProviderTests(APITestCase):
    """The adapter itself, with the SDK client mocked."""

    @override_settings(GEMINI_API_KEY=SECRET, GEMINI_MODEL="m")
    def test_fallback_when_overloaded(self):
        from google.genai import errors

        p = GeminiProvider()
        calls = []

        def ask(client, model):
            calls.append(model)
            if model == "main":
                raise errors.ServerError(503, {"error": {"code": 503, "status": "UNAVAILABLE", "message": "busy"}})
            return TestResult(ok=True, requested_model=model, model_version="lite-001")

        with mock.patch.object(GeminiProvider, "_client", return_value=object()), mock.patch.object(GeminiProvider, "_ask", side_effect=ask):
            r = p.test("main", fallback_model="lite")
        self.assertEqual((calls, r.used_fallback, r.requested_model, r.model_version), (["main", "lite"], True, "main", "lite-001"))

    @override_settings(GEMINI_API_KEY=SECRET)
    def test_auth_error_message_has_no_key(self):
        from google.genai import errors

        err = GeminiProvider._translate(errors.ClientError(403, {"error": {"code": 403, "status": "PERMISSION_DENIED", "message": f"key {SECRET} bad"}}))
        self.assertEqual(err.kind, "auth")
        self.assertNotIn(SECRET, str(err))

    @override_settings(GEMINI_API_KEY=SECRET)
    def test_model_filtering(self):
        class M:
            def __init__(self, name, actions):
                self.name, self.supported_actions = f"models/{name}", actions
                self.display_name, self.description, self.input_token_limit, self.output_token_limit = name, "", 1, 1

        raw = [M("gemini-3.5-flash", ["generateContent"]), M("gemini-embedding-001", ["embedContent"]),
               M("gemini-2.5-flash-preview-tts", ["generateContent"]), M("gemma-3-27b-it", ["generateContent"]),
               M("gemini-3-pro-image-preview", ["generateContent"]), M("gemini-omni-flash-preview", ["generateContent"])]
        fake = mock.Mock()
        fake.models.list.return_value = raw
        with mock.patch.object(GeminiProvider, "_client", return_value=fake):
            got = [(m.id, m.stage) for m in GeminiProvider().list_models()]
        self.assertEqual(got, [("gemini-3.5-flash", "stable"), ("gemini-omni-flash-preview", "preview")])
