from datetime import date
from decimal import Decimal
from unittest import mock

from django.test import SimpleTestCase
from rest_framework.test import APITestCase

from .calculations import months_between, required_monthly, surplus

D = Decimal


class CalculationTests(SimpleTestCase):
    def test_required_monthly(self):
        self.assertEqual(required_monthly(D("120000"), D("0"), 12), D("10000.00"))
        self.assertEqual(required_monthly(D("120000"), D("20000"), 10), D("10000.00"))
        self.assertEqual(required_monthly(D("100"), D("150"), 5), D("0"))
        self.assertEqual(required_monthly(D("1000"), D("0"), 0), D("1000.00"))
        # With 12%/year (1%/month) for 12 months: FV annuity factor 12.6825 -> 1000/12.6825 = 78.85
        self.assertEqual(required_monthly(D("1000"), D("0"), 12, D("12")), D("78.85"))

    def test_months_between(self):
        self.assertEqual(months_between(date(2026, 11, 25), date(2027, 11, 25)), 12)
        self.assertEqual(months_between(date(2026, 11, 25), date(2027, 11, 24)), 11)
        self.assertEqual(months_between(date(2026, 11, 25), date(2026, 1, 1)), 0)

    def surplus(self, **kw):
        args = dict(currency="PKR", income=D("300000"), expenses={"amount": D("150000"), "source": "user_input"},
                    installments={"amount": D("20737.31"), "source": "ledger_plans"}, other_debts=D("0"),
                    savings=D("200000"), emergency_months=6, near_term_goal_contributions=D("10000"))
        args.update(kw)
        return surplus(**args).as_dict()

    def test_waterfall(self):
        r = self.surplus()
        lines = {l["id"]: l["amount"] for l in r["lines"]}
        self.assertEqual(lines["free_cash_flow"], "129262.69")        # 300000 - 150000 - 20737.31
        self.assertEqual(lines["buffer"], "12926.27")                 # 10%
        self.assertEqual(r["emergency_target"], "1024423.86")         # 6 x 170737.31
        self.assertEqual(r["emergency_gap"], "824423.86")             # - 200000 savings
        self.assertEqual(lines["emergency"], "68701.99")              # gap / 12
        self.assertEqual(lines["near_term_goals"], "10000.00")
        self.assertEqual(r["investable_monthly"], "37634.43")         # 129262.69 - 12926.27 - 68701.99 - 10000
        self.assertTrue(r["complete"])

    def test_nothing_investable_when_outgoings_exceed_income(self):
        r = self.surplus(income=D("100000"))
        self.assertEqual(r["investable_monthly"], "0.00")
        self.assertIn("nothing is investable", r["notes"][0])

    def test_missing_income_or_spending_stops_the_estimate(self):
        r = self.surplus(income=None)
        self.assertFalse(r["complete"])
        self.assertIn("Monthly income", r["missing"])
        r = self.surplus(expenses={"amount": None, "source": "missing"})
        self.assertFalse(r["complete"])

    def test_unknown_savings_is_cautious(self):
        r = self.surplus(savings=None)
        self.assertEqual(r["emergency_gap"], r["emergency_target"])
        self.assertFalse(r["complete"])


class AssessmentApiTests(APITestCase):
    def setUp(self):
        res = self.client.post("/api/auth/register/", {"email": "fin@example.com", "password": "Sup3r-secret!"}, format="json")
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {res.data['access']}")
        fake_rates = {("USD", "PKR"): "276.50"}

        def market(base, quote, wanted=None, **kw):
            rate = fake_rates.get((base, quote))
            return {"rate": rate, "date": "2026-11-24", "source": "test", "provider": "test"} if rate else None

        for target in ("expenses.conversion.market_rate",):
            p = mock.patch(target, side_effect=market)
            p.start()
            self.addCleanup(p.stop)
        self.today = date(2026, 11, 25)
        p = mock.patch("finance.assessment.date")
        fake = p.start()
        fake.today.return_value = self.today
        fake.side_effect = lambda *a, **k: date(*a, **k)
        self.addCleanup(p.stop)

    def test_ledger_used_only_with_consent(self):
        self.client.post("/api/plans/", {"item": "Secret MacBook", "purchase_price": "1199", "total_amount": "1350", "months": 18,
                                         "first_payment_date": "2026-09-01", "payment_currency": "PKR", "exchange_rate": "276"}, format="json")
        for m in ("08", "09", "10"):
            self.client.post("/api/expenses/", {"title": "Rent", "amount": "40000", "category": "rent", "spent_on": f"2026-{m}-01"}, format="json")
        self.client.post("/api/expenses/", {"title": "Book", "amount": "100", "currency": "USD", "category": "education", "spent_on": "2026-10-03"}, format="json")
        self.client.patch("/api/finance/profile/", {"monthly_income": "300000", "cash_savings": "500000"}, format="json")

        before = self.client.get("/api/finance/assessment/").data
        lines = {l["id"]: l for l in before["lines"]}
        self.assertEqual(lines["expenses"]["source"], "missing")
        self.assertFalse(before["complete"])

        res = self.client.patch("/api/finance/profile/", {"consent_use_ledger": True}, format="json")
        self.assertIsNotNone(res.data["consent_updated_at"])
        after = self.client.get("/api/finance/assessment/").data
        lines = {l["id"]: l for l in after["lines"]}
        # Aug 40000, Sep 40000, Oct 40000 + 100 USD x 276.50 = 67650 -> average 49216.67
        self.assertEqual(lines["expenses"]["amount"], "49216.67")
        self.assertEqual(lines["expenses"]["source"], "ledger_expenses")
        self.assertEqual(lines["installments"]["amount"], "20737.50")  # $75/month (USD plan) x 276.50
        self.assertTrue(after["complete"])

    def test_goals_crud_and_projection(self):
        res = self.client.post("/api/finance/goals/", {"name": "My wedding", "kind": "short_term", "target_amount": "1200000",
                                                       "saved_amount": "200000", "target_date": "2027-11-25", "priority": 1,
                                                       "assumed_annual_return": "12"}, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data["currency"], "PKR")
        a = self.client.get("/api/finance/assessment/").data
        g = a["goals"][0]
        self.assertEqual((g["months_left"], g["required_monthly"], g["near_term"]), (12, "83333.33", True))
        # 12%/yr = 1%/month, n=12: (1,200,000 - 200,000 x 1.01^12) x 0.01 / (1.01^12 - 1) = 76,848.79
        self.assertEqual(g["scenario_monthly"], "76848.79")
        gid = res.data["id"]
        self.assertEqual(self.client.patch(f"/api/finance/goals/{gid}/", {"saved_amount": "1200000"}, format="json").status_code, 200)
        self.assertEqual(self.client.get("/api/finance/assessment/").data["goals"][0]["required_monthly"], "0.00")
        self.assertEqual(self.client.delete(f"/api/finance/goals/{gid}/").status_code, 204)

    def test_validation(self):
        self.assertEqual(self.client.patch("/api/finance/profile/", {"monthly_income": "-5"}, format="json").status_code, 400)
        self.assertEqual(self.client.patch("/api/finance/profile/", {"emergency_months_target": 99}, format="json").status_code, 400)
        bad = self.client.post("/api/finance/goals/", {"name": " ", "target_amount": "0", "target_date": "2027-01-01"}, format="json")
        self.assertEqual(bad.status_code, 400)
