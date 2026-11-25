"""Opportunity research: sources come only from grounding metadata, unsourced ideas are hidden, fit checks are rules."""
from unittest import mock

from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APITestCase

from .llm.base import ProviderError
from .llm.gemini import GeminiProvider
from .models import AIInteraction
from .opportunities import parse_grounded

ANSWER = """### OVERVIEW
The policy rate is 11% since 27 October 2026 — inflation was 5.1% in October.

### OPPORTUNITY: Treasury bills
ASSET CLASS: government_bills
WHAT IT IS: Short-term government debt.
CURRENT FACTS: The 6-month cut-off yield was 10.9% on 12 November 2026.
LIQUIDITY: Matures in 3, 6 or 12 months.
MAIN RISKS: Returns can fall below inflation. See https://evil.example/buy

### OPPORTUNITY: **Equity index fund**
**ASSET CLASS:** equity_index_fund
WHAT IT IS: A fund tracking a broad share index.
CURRENT FACTS: The index rose 40% in 2025, which is history, not a forecast.
MAIN RISKS: Prices can fall sharply.

### OPPORTUNITY: Made-up scheme
ASSET CLASS: other
WHAT IT IS: Something nobody sourced.
"""


def span(text, needle):
    """UTF-8 byte offsets of `needle` in `text` (Google's segment indices are byte offsets)."""
    b, n = text.encode(), needle.encode()
    start = b.index(n)
    return start, start + len(n)


def grounding(text=ANSWER):
    supports = []
    for needle, chunks in (("inflation was 5.1% in October.", [0]),
                           ("The 6-month cut-off yield was 10.9% on 12 November 2026.", [1, 0]),
                           ("The index rose 40% in 2025", [2]),
                           ("Prices can fall sharply.", [5])):  # index 5 doesn't exist -> ignored
        s, e = span(text, needle)
        supports.append({"part": 0, "start": s, "end": e, "chunks": chunks})
    return {"parts": [text], "supports": supports, "queries": ["pakistan t-bill yield november 2026"],
            "entry_html": "<div class='chips'>suggestions</div>",
            "chunks": [{"uri": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/aaa", "title": "sbp.org.pk", "domain": "sbp.org.pk"},
                       {"uri": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/bbb", "title": "Auction results", "domain": "sbp.org.pk"},
                       {"uri": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/ccc", "title": "psx.com.pk", "domain": "psx.com.pk"}]}


class ParseTests(APITestCase):
    def test_sources_attach_by_byte_offset_and_unsourced_ideas_drop(self):
        out = parse_grounded(grounding())
        self.assertEqual([o["name"] for o in out["opportunities"]], ["Treasury bills", "Equity index fund"])
        self.assertEqual(out["dropped"], 1)
        # overview text after a multi-byte dash still lines up with its source
        ov = out["overview"]["segments"]
        self.assertEqual(ov[-1], {"text": "inflation was 5.1% in October.", "sources": [1]})
        tb = out["opportunities"][0]
        self.assertEqual(tb["asset_class"], "government_bills")
        facts = next(f for f in tb["fields"] if f["key"] == "facts")
        self.assertEqual(facts["segments"], [{"text": "The 6-month cut-off yield was 10.9% on 12 November 2026.", "sources": [1, 2]}])
        risks = next(f for f in tb["fields"] if f["key"] == "risks")
        self.assertFalse(risks["sourced"])
        self.assertIn("[link removed]", risks["segments"][0]["text"])
        eq = out["opportunities"][1]
        self.assertEqual((eq["name"], eq["asset_class"]), ("Equity index fund", "equity_index_fund"))  # markdown bold tolerated
        self.assertEqual([s["n"] for s in out["sources"]], [1, 2, 3])
        self.assertTrue(all(s["uri"].startswith("https://") for s in out["sources"]))

    def test_multi_part_offsets(self):
        first, second = ANSWER[:200], ANSWER[200:]
        g = grounding()
        s, e = span(second, "The index rose 40% in 2025")
        g["parts"] = [first, second]
        g["supports"] = [{"part": 1, "start": s, "end": e, "chunks": [2]}]
        out = parse_grounded(g)
        self.assertEqual([o["name"] for o in out["opportunities"]], ["Equity index fund"])


@override_settings(GEMINI_API_KEY="AQ.test-key-abcd", GEMINI_MODEL="gemini-3.5-flash", GEMINI_FALLBACK_MODEL="gemini-3.5-flash-lite")
class OpportunitiesApiTests(APITestCase):
    def setUp(self):
        cache.clear()
        res = self.client.post("/api/auth/register/", {"email": "opps-person@example.com", "password": "Sup3r-secret!"}, format="json")
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {res.data['access']}")
        self.client.post("/api/finance/goals/", {"name": "Private wedding of Ayesha", "target_amount": "1200000", "target_date": "2027-11-25"}, format="json")
        # income 300k, spending 150k, savings 500k -> emergency target 900k, gap 400k
        self.client.patch("/api/finance/profile/", {"monthly_income": "300000", "cash_savings": "500000", "monthly_expenses_override": "150000",
                                                    "investment_horizon_years": 10}, format="json")
        self.sent = {}

    def scan(self, body=None, result=None, error=None):
        def fake(model, system, prompt, fallback_model=None):
            self.sent.update(model=model, system=system, prompt=prompt)
            if error:
                raise error
            return result or grounding(), {"model_version": "gemini-3.5-flash", "latency_ms": 9000, "input_tokens": 300,
                                           "output_tokens": 900, "used_fallback": False}
        with mock.patch.object(GeminiProvider, "generate_grounded", side_effect=fake):
            return self.client.post("/api/ai/opportunities/", body or {"region": "pakistan", "risk_level": "balanced"}, format="json")

    def test_consent_required_and_nothing_sent(self):
        res = self.scan()
        self.assertEqual((res.status_code, res.data["kind"]), (403, "consent_required"))
        self.assertEqual(self.sent, {})

    def test_scan_fit_checks_and_minimal_request(self):
        self.client.patch("/api/finance/profile/", {"consent_ai": True}, format="json")
        res = self.scan()
        self.assertEqual(res.status_code, 200, res.data)
        for private in ("300000", "500000", "150000", "Ayesha", "wedding", "opps-person"):
            self.assertNotIn(private, self.sent["prompt"])  # income, savings, names, email never leave
        self.assertIn('"region": "Pakistan"', self.sent["prompt"])
        opps = res.data["opportunities"]
        self.assertEqual([o["name"] for o in opps], ["Treasury bills", "Equity index fund"])  # ordered by fit
        self.assertEqual(opps[0]["fit"]["level"], "good")
        eq = opps[1]["fit"]
        self.assertEqual(eq["level"], "not_now")  # emergency gap + risk above 'balanced'
        texts = " ".join(r["text"] for r in eq["reasons"])
        self.assertIn("emergency fund", texts)
        self.assertIn("Riskier than", texts)
        self.assertIn("1 idea(s) were hidden", res.data["warnings"][0])
        self.assertIn("Emergency fund first: PKR 400,000.00", " ".join(res.data["plan"]["notes"]))
        self.assertEqual(res.data["search_entry_point_html"], "<div class='chips'>suggestions</div>")
        rec = AIInteraction.objects.get(kind="opportunities")
        self.assertTrue(rec.ok)
        self.assertEqual(rec.prompt_version, "opportunities-v1")
        # the last scan comes back on GET
        got = self.client.get("/api/ai/opportunities/").data
        self.assertEqual(got["scan"]["interaction_id"], rec.id)
        self.assertIn("pakistan", got["regions"])

    def test_shariah_and_horizon_rules(self):
        self.client.patch("/api/finance/profile/", {"consent_ai": True, "cash_savings": "2000000"}, format="json")  # no gap
        res = self.scan({"region": "pakistan", "risk_level": "growth", "shariah_only": True, "horizon_years": 2})
        by = {o["name"]: o["fit"] for o in res.data["opportunities"]}
        self.assertEqual(by["Treasury bills"]["level"], "not_now")
        self.assertIn("Shariah", " ".join(r["text"] for r in by["Treasury bills"]["reasons"]))
        self.assertIn("your horizon is 2", " ".join(r["text"] for r in by["Equity index fund"]["reasons"]))

    def test_bad_request_quota_and_empty(self):
        self.client.patch("/api/finance/profile/", {"consent_ai": True}, format="json")
        self.assertEqual(self.scan({"region": "mars"}).status_code, 400)
        res = self.scan(error=ProviderError("quota", "rate_limited", 429))
        self.assertEqual((res.status_code, res.data["kind"]), (429, "rate_limited"))
        empty = grounding()
        empty["supports"] = []
        res = self.scan(result=empty)
        self.assertEqual((res.status_code, res.data["kind"]), (502, "bad_output"))
        self.assertEqual(AIInteraction.objects.filter(kind="opportunities", ok=False).count(), 2)
        self.assertIsNone(self.client.get("/api/ai/opportunities/").data["scan"])

    def test_throttled(self):
        self.client.patch("/api/finance/profile/", {"consent_ai": True}, format="json")
        codes = [self.scan().status_code for _ in range(6)]
        self.assertEqual(codes[-1], 429)


class GroundedProviderTests(APITestCase):
    @override_settings(GEMINI_API_KEY="AQ.test-key-abcd")
    def test_quota_error_explains_billing(self):
        from google.genai import errors

        exc = errors.APIError(429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "quota"}})
        with mock.patch.object(GeminiProvider, "_grounded", side_effect=exc):
            with self.assertRaises(ProviderError) as ctx:
                GeminiProvider().generate_grounded("gemini-3.5-flash", "s", "p", fallback_model="gemini-3.5-flash-lite")
        self.assertEqual(ctx.exception.kind, "rate_limited")
        self.assertIn("billing", str(ctx.exception))
