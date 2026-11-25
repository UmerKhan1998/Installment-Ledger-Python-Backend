import json
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


@override_settings(GEMINI_API_KEY=SECRET, GEMINI_MODEL="gemini-3.5-flash", GEMINI_FALLBACK_MODEL="gemini-3.5-flash-lite")
class PlannerTests(APITestCase):
    def setUp(self):
        cache.clear()  # throttle history lives in the cache
        res = self.client.post("/api/auth/register/", {"email": "planner-person@example.com", "password": "Sup3r-secret!"}, format="json")
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {res.data['access']}")
        self.client.post("/api/plans/", {"item": "Secret MacBook Plan", "purchase_price": "1199", "total_amount": "1350", "months": 18,
                                         "first_payment_date": "2026-09-01", "currency": "PKR"}, format="json")
        self.client.post("/api/finance/goals/", {"name": "Private wedding of Ayesha", "kind": "short_term", "target_amount": "1200000",
                                                 "target_date": "2027-11-25"}, format="json")
        self.client.patch("/api/finance/profile/", {"monthly_income": "300000", "cash_savings": "500000",
                                                    "monthly_expenses_override": "150000", "consent_use_ledger": True}, format="json")
        self.sent = {}

    def fake_generate(self, answer):
        def generate(model, system, prompt, schema, fallback_model=None):
            self.sent.update(model=model, system=system, prompt=prompt)
            return json.dumps(answer), {"model_version": "gemini-3.6-flash", "latency_ms": 1200, "input_tokens": 900,
                                        "output_tokens": 300, "used_fallback": False}
        return generate

    def run_planner(self, answer):
        from .llm.gemini import GeminiProvider
        with mock.patch.object(GeminiProvider, "generate_json", side_effect=self.fake_generate(answer)):
            return self.client.post("/api/ai/planner/", {}, format="json")

    def test_consent_required(self):
        res = self.run_planner({"summary": "x"})
        self.assertEqual((res.status_code, res.data["kind"]), (403, "consent_required"))
        self.assertEqual(self.sent, {})  # nothing was sent

    def test_answer_validated_and_names_never_sent(self):
        self.client.patch("/api/finance/profile/", {"consent_ai": True}, format="json")
        answer = {
            "summary": "You keep 129,250.00 free each month and can set aside 37,621.25.",
            "sections": [
                {"title": "Cash flow", "text": "Income 300,000.00 minus spending 150,000.00.", "figure_ids": ["income", "expenses", "made_up"]},
                {"title": "Invest", "text": "Markets returned 18% so put 999,999 into ABC Fund, see https://evil.example", "figure_ids": ["investable"]},
            ],
            "next_steps": ["Build the emergency fund."],
            "caveats": ["Estimates only."],
        }
        res = self.run_planner(answer)
        self.assertEqual(res.status_code, 200, res.data)
        sec = res.data["answer"]["sections"]
        self.assertEqual(sec[0]["figure_ids"], ["income", "expenses"])           # unknown id dropped
        self.assertIn("[link removed]", sec[1]["text"])
        warnings = " ".join(res.data["warnings"])
        self.assertIn("made_up", warnings)
        self.assertIn("999,999", warnings)                                       # number not from the app
        self.assertNotIn("300,000.00", warnings)                                  # real figures pass
        self.assertEqual(res.data["model_version"], "gemini-3.6-flash")
        # Data minimisation: no plan/goal names or email in what was sent
        for private in ("Secret MacBook Plan", "Private wedding of Ayesha", "Ayesha", "planner-person@example.com"):
            self.assertNotIn(private, self.sent["prompt"])
        self.assertIn('"income"', self.sent["prompt"])
        self.assertIn("not a licensed financial adviser", self.sent["system"])

    def test_interaction_is_audited(self):
        from .models import AIInteraction
        self.client.patch("/api/finance/profile/", {"consent_ai": True}, format="json")
        self.run_planner({"summary": "ok", "sections": [], "next_steps": [], "caveats": []})
        rec = AIInteraction.objects.get()
        self.assertEqual((rec.kind, rec.prompt_version, rec.method_version, rec.model_version, rec.ok),
                         ("planner", "planner-v3", "surplus-v1", "gemini-3.6-flash", True))
        self.assertIn("investable", rec.figure_ids)
        self.assertEqual(len(rec.figures_hash), 64)

    def test_bad_model_output_is_rejected_and_recorded(self):
        from .llm.gemini import GeminiProvider
        from .models import AIInteraction
        self.client.patch("/api/finance/profile/", {"consent_ai": True}, format="json")
        with mock.patch.object(GeminiProvider, "generate_json", return_value=("not json", {"model_version": "x"})):
            res = self.client.post("/api/ai/planner/", {}, format="json")
        self.assertEqual((res.status_code, res.data["kind"]), (502, "bad_output"))
        self.assertEqual(AIInteraction.objects.get().error_kind, "bad_output")

    def test_planner_is_rate_limited(self):
        from rest_framework.settings import api_settings
        self.client.patch("/api/finance/profile/", {"consent_ai": True}, format="json")
        with mock.patch.dict(api_settings.DEFAULT_THROTTLE_RATES, {"ai_planner": "1/hour"}):
            codes = [self.run_planner({"summary": "ok"}).status_code for _ in range(2)]
        self.assertEqual(codes, [200, 429])


@override_settings(GEMINI_API_KEY=SECRET, GEMINI_MODEL="gemini-3.5-flash", GEMINI_FALLBACK_MODEL="gemini-3.5-flash-lite")
class PlannerChatTests(APITestCase):
    """The chat panel: a free-form question plus a short history; same guardrails as the plan explanation."""

    setUp = PlannerTests.setUp
    fake_generate = PlannerTests.fake_generate

    def ask(self, body):
        from .llm.gemini import GeminiProvider
        answer = {"summary": "Build your emergency fund first.", "sections": [], "next_steps": [], "caveats": []}
        with mock.patch.object(GeminiProvider, "generate_json", side_effect=self.fake_generate(answer)):
            return self.client.post("/api/ai/planner/", body, format="json")

    def test_question_and_history_reach_the_prompt(self):
        self.client.patch("/api/finance/profile/", {"consent_ai": True}, format="json")
        history = [{"q": f"old {i}", "a": "x" * 900} for i in range(6)]
        res = self.ask({"question": "  Am I ready\n to invest?  ", "history": history})
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["question"], "Am I ready to invest?")
        self.assertEqual(res.data["prompt_version"], "planner-v3")
        self.assertIn("QUESTION: Am I ready to invest?", self.sent["prompt"])
        self.assertNotIn("old 1", self.sent["prompt"])          # only the last 4 turns
        self.assertIn("old 5", self.sent["prompt"])
        self.assertNotIn("x" * 501, self.sent["prompt"])        # turns trimmed
        self.assertNotIn("Ayesha", self.sent["prompt"])

    def test_bad_chat_input(self):
        self.client.patch("/api/finance/profile/", {"consent_ai": True}, format="json")
        self.assertEqual(self.ask({"question": "q" * 501}).status_code, 400)
        self.assertEqual(self.ask({"question": "hi", "history": "nope"}).status_code, 400)
        self.assertEqual(self.ask({"question": ["x"]}).status_code, 400)


class OverloadRetryTests(APITestCase):
    @override_settings(GEMINI_API_KEY=SECRET)
    def test_overloaded_retries_chosen_model_after_fallback(self):
        from google.genai import errors
        busy = errors.APIError(503, {"error": {"code": 503, "status": "UNAVAILABLE", "message": "busy"}})
        calls = []

        def structured(client, model, *a):
            calls.append(model)
            if len(calls) < 3:
                raise busy
            return "{}", {"used_fallback": False}
        with mock.patch.object(GeminiProvider, "_client"), mock.patch.object(GeminiProvider, "_structured", side_effect=structured), \
                mock.patch("assistant.llm.gemini.RETRY_DELAY_S", 0):
            GeminiProvider().generate_json("m", "s", "p", dict, fallback_model="lite")
        self.assertEqual(calls, ["m", "lite", "m"])
