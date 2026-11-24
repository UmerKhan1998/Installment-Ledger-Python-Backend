from datetime import date
from decimal import Decimal
from unittest import mock

from rest_framework.test import APITestCase

from .models import MonthlyRate


class ExpenseTests(APITestCase):
    def setUp(self):
        res = self.client.post("/api/auth/register/", {"email": "exp@example.com", "password": "Sup3r-secret!"}, format="json")
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {res.data['access']}")

    def add(self, **kw):
        body = {"title": "Groceries", "amount": "5000", "category": "food", "spent_on": "2026-11-10", **kw}
        res = self.client.post("/api/expenses/", body, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        return res.data

    def test_settings_default_and_update(self):
        self.assertEqual(self.client.get("/api/expenses/settings/").data, {"currency": "PKR", "monthly_budget": None, "budget_currency": ""})
        res = self.client.patch("/api/expenses/settings/", {"currency": "USD", "monthly_budget": "800"}, format="json")
        self.assertEqual(res.data, {"currency": "USD", "monthly_budget": "800.00", "budget_currency": "USD"})

    def test_new_expense_uses_tracker_currency(self):
        self.assertEqual(self.add()["currency"], "PKR")
        self.client.patch("/api/expenses/settings/", {"currency": "AED"}, format="json")
        self.assertEqual(self.add(title="Taxi")["currency"], "AED")
        self.assertEqual(self.add(title="Book", currency="USD")["currency"], "USD")

    def test_list_is_per_month(self):
        self.add(spent_on="2026-11-01")
        self.add(spent_on="2026-11-30")
        self.add(spent_on="2026-10-31")
        self.add(spent_on="2026-12-01")
        self.assertEqual(len(self.client.get("/api/expenses/?month=2026-11").data), 2)
        self.assertEqual(self.client.get("/api/expenses/?month=nope").status_code, 400)

    def test_summary(self):
        self.client.patch("/api/expenses/settings/", {"monthly_budget": "60000"}, format="json")
        self.add(title="Rent", amount="40000", category="rent", spent_on="2026-11-01")
        self.add(title="Groceries", amount="8000", category="food", spent_on="2026-11-05")
        self.add(title="Fruit", amount="2000", category="food", spent_on="2026-11-06")
        self.add(title="Rent", amount="40000", category="rent", spent_on="2026-10-01")
        with mock.patch("expenses.views.date") as fake_date:
            fake_date.today.return_value = date(2026, 11, 10)
            fake_date.side_effect = lambda *a, **k: date(*a, **k)
            s = self.client.get("/api/expenses/summary/?month=2026-11").data
        self.assertEqual(s["currency"], "PKR")
        self.assertEqual(s["total"], "50000.00")
        self.assertEqual(s["count"], 3)
        self.assertEqual(s["remaining"], "10000.00")
        self.assertEqual(s["budget_used_percent"], 83.3)
        self.assertEqual(s["daily_average"], "5000.00")   # 10 days elapsed
        self.assertEqual(s["previous_month_total"], "40000.00")
        self.assertEqual(s["change_percent"], 25.0)
        self.assertEqual([c["category"] for c in s["by_category"]], ["rent", "food"])
        self.assertEqual(s["by_category"][1]["total"], "10000.00")
        self.assertEqual(s["by_category"][1]["share"], 20.0)
        self.assertEqual(s["rates"], [])
        self.assertEqual([t["month"] for t in s["trend"]], ["2026-06", "2026-07", "2026-08", "2026-09", "2026-10", "2026-11"])
        self.assertEqual(s["trend"][-2]["total"], "40000.00")

    def test_copy_recurring(self):
        self.add(title="Rent", amount="40000", category="rent", spent_on="2026-10-31", is_recurring=True)
        self.add(title="Internet", amount="3000", category="bills", spent_on="2026-10-05", is_recurring=True)
        self.add(title="Dinner", amount="4000", category="food", spent_on="2026-10-12")  # not recurring
        self.add(title="internet", amount="3200", category="bills", spent_on="2026-11-05")  # already added this month
        self.assertEqual(self.client.get("/api/expenses/summary/?month=2026-11").data["recurring_pending"], 1)
        res = self.client.post("/api/expenses/copy-recurring/", {"month": "2026-11"}, format="json")
        self.assertEqual([(e["title"], e["spent_on"]) for e in res.data], [("Rent", "2026-11-30")])  # 31st clamped
        again = self.client.post("/api/expenses/copy-recurring/", {"month": "2026-11"}, format="json")
        self.assertEqual(again.data, [])

    def test_edit_delete_and_bulk_delete(self):
        a, b, c = self.add()["id"], self.add(title="B")["id"], self.add(title="C")["id"]
        self.assertEqual(self.client.patch(f"/api/expenses/{a}/", {"amount": "6000"}, format="json").data["amount"], "6000.00")
        self.assertEqual(self.client.delete(f"/api/expenses/{a}/").status_code, 204)
        self.assertEqual(self.client.post("/api/expenses/bulk-delete/", {"ids": [b, 999999]}, format="json").status_code, 404)
        self.assertEqual(self.client.post("/api/expenses/bulk-delete/", {"ids": [b, c]}, format="json").data, {"deleted": 2})

    def test_validation(self):
        bad = self.client.post("/api/expenses/", {"title": "  ", "amount": "0", "spent_on": "2026-11-01"}, format="json")
        self.assertEqual(bad.status_code, 400)
        self.assertIn("title", bad.data)
        self.assertIn("amount", bad.data)

    def test_users_are_isolated(self):
        eid = self.add()["id"]
        other = self.client.post("/api/auth/register/", {"email": "o2@example.com", "password": "Sup3r-secret!"}, format="json")
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {other.data['access']}")
        self.assertEqual(self.client.get(f"/api/expenses/{eid}/").status_code, 404)
        self.assertEqual(self.client.get("/api/expenses/?month=2026-11").data, [])
        self.assertEqual(self.client.post("/api/expenses/bulk-delete/", {"ids": [eid]}, format="json").status_code, 404)

    def test_plans_are_untouched(self):
        self.add()
        self.assertEqual(self.client.get("/api/summary/").data["plan_count"], 0)


def fake_market(rates):
    """market_rate stand-in: rates = {(base, quote, iso_date_or_None): Decimal-ish}."""
    def lookup(base, quote, wanted=None):
        key = (base, quote, wanted.isoformat() if wanted else None)
        if key in rates:
            return {"rate": rates[key], "date": key[2] or "2026-11-24", "source": "test"}
        return None
    return lookup


class ConversionTests(APITestCase):
    """Expenses in other currencies are converted with one stored rate per month."""

    RATES = {
        ("USD", "PKR", "2026-10-31"): "277.12345678",  # past month: end-of-month rate
        ("USD", "PKR", None): "276.4974",               # current month: latest
        ("PKR", "USD", None): "0.00361667",
        ("PKR", "USD", "2026-10-31"): "0.00360850",
        ("AED", "PKR", None): "75.28",
    }

    def setUp(self):
        res = self.client.post("/api/auth/register/", {"email": "fx@exp.com", "password": "Sup3r-secret!"}, format="json")
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {res.data['access']}")
        patcher = mock.patch("expenses.conversion.market_rate", side_effect=fake_market(self.RATES))
        self.market = patcher.start()
        self.addCleanup(patcher.stop)
        today = mock.patch("expenses.conversion.date")
        fake = today.start()
        fake.today.return_value = date(2026, 11, 24)
        fake.fromisoformat.side_effect = date.fromisoformat
        self.addCleanup(today.stop)

    def add(self, **kw):
        body = {"title": "x", "amount": "1000", "category": "bills", "spent_on": "2026-11-10", **kw}
        res = self.client.post("/api/expenses/", body, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        return res.data

    def summary(self, month="2026-11"):
        return self.client.get(f"/api/expenses/summary/?month={month}").data

    def test_foreign_expense_is_converted_and_included(self):
        self.add(title="Rent", amount="40000", category="rent")
        usd = self.add(title="Laptop plan", amount="75", currency="USD")
        self.assertEqual(usd["converted"], {"currency": "PKR", "amount": "20737.31", "rate": "276.4974", "source": "market"})
        s = self.summary()
        self.assertEqual(s["total"], "60737.31")  # 40000 + 75 x 276.4974 = 20737.305 -> 20737.31
        self.assertEqual(s["count"], 2)
        self.assertEqual(s["rates"], [{"currency": "USD", "quote": "PKR", "rate": "276.4974", "source": "market", "as_of": "2026-11-24"}])
        self.assertEqual(s["unconverted"], [])

    def test_rows_add_up_to_total(self):
        for amt in ("0.01", "0.07", "3.33", "12.35", "75", "19.99"):
            self.add(amount=amt, currency="USD")
        listed = self.client.get("/api/expenses/?month=2026-11").data
        row_sum = sum(Decimal(e["converted"]["amount"]) for e in listed)
        self.assertEqual(Decimal(self.summary()["total"]), row_sum)

    def test_past_month_uses_end_of_month_rate_and_is_stored(self):
        self.add(amount="100", currency="USD", spent_on="2026-10-05")
        self.assertEqual(self.summary("2026-10")["total"], "27712.35")  # 100 x 277.12345678
        calls = self.market.call_count
        self.summary("2026-10")
        self.assertEqual(self.market.call_count, calls)  # second time comes from the stored MonthlyRate
        self.assertEqual(MonthlyRate.objects.get(month=date(2026, 10, 1), base="USD").rate, Decimal("277.12345678"))

    def test_trend_converts_each_month_with_its_own_rate(self):
        self.add(amount="100", currency="USD", spent_on="2026-10-05")
        self.add(amount="100", currency="USD", spent_on="2026-11-05")
        trend = {t["month"]: t["total"] for t in self.summary()["trend"]}
        self.assertEqual(trend["2026-10"], "27712.35")
        self.assertEqual(trend["2026-11"], "27649.74")
        self.assertEqual(self.summary()["previous_month_total"], "27712.35")

    def test_switching_currency_reconverts_everything_and_the_budget(self):
        self.client.patch("/api/expenses/settings/", {"monthly_budget": "100000"}, format="json")
        self.add(title="Rent", amount="40000", category="rent")
        self.add(title="Laptop plan", amount="75", currency="USD")
        res = self.client.patch("/api/expenses/settings/", {"currency": "USD"}, format="json")
        # The stored budget is never rewritten...
        self.assertEqual(res.data, {"currency": "USD", "monthly_budget": "100000.00", "budget_currency": "PKR"})
        s = self.summary()
        self.assertEqual(s["currency"], "USD")
        self.assertEqual(s["budget"], "361.67")  # ...only shown converted: 100000 x 0.00361667
        self.assertEqual(s["budget_original"], {"amount": "100000.00", "currency": "PKR", "rate": "0.00361667"})
        self.assertEqual(s["total"], "219.67")  # 40000 x 0.00361667 = 144.6668 -> 144.67, + 75.00
        listed = {e["title"]: e for e in self.client.get("/api/expenses/?month=2026-11").data}
        self.assertEqual(listed["Rent"]["converted"]["amount"], "144.67")
        self.assertIsNone(listed["Laptop plan"]["converted"])  # already in USD
        # Switching back shows the exact original budget (no round-trip drift).
        self.client.patch("/api/expenses/settings/", {"currency": "PKR"}, format="json")
        back = self.summary()
        self.assertEqual(back["budget"], "100000.00")
        self.assertIsNone(back["budget_original"])

    def test_new_budget_is_in_the_current_tracker_currency(self):
        self.client.patch("/api/expenses/settings/", {"currency": "USD"}, format="json")
        res = self.client.patch("/api/expenses/settings/", {"monthly_budget": "400"}, format="json")
        self.assertEqual(res.data["budget_currency"], "USD")
        cleared = self.client.patch("/api/expenses/settings/", {"monthly_budget": None}, format="json")
        self.assertEqual(cleared.data["budget_currency"], "")

    def test_manual_rate_overrides_and_can_be_reset(self):
        self.add(amount="75", currency="USD")
        res = self.client.put("/api/expenses/rates/", {"month": "2026-11", "currency": "USD", "rate": "280"}, format="json")
        self.assertEqual(res.data["source"], "manual")
        self.assertEqual(self.summary()["total"], "21000.00")
        # The daily market refresh never overwrites a manual rate.
        self.assertEqual(self.summary()["rates"][0]["rate"], "280")
        back = self.client.delete("/api/expenses/rates/?month=2026-11&currency=USD")
        self.assertEqual(back.data["source"], "market")
        self.assertEqual(self.summary()["total"], "20737.31")

    def test_rates_endpoint_for_the_add_form(self):
        res = self.client.get("/api/expenses/rates/?month=2026-11&currency=AED").data
        self.assertEqual(res["rates"][0]["rate"], "75.28")
        listed = self.client.get("/api/expenses/rates/?month=2026-11").data
        self.assertEqual(listed["rates"], [])  # no foreign expenses this month yet

    def test_rate_validation(self):
        url = "/api/expenses/rates/"
        self.assertEqual(self.client.put(url, {"month": "2026-11", "currency": "USD", "rate": "0"}, format="json").status_code, 400)
        self.assertEqual(self.client.put(url, {"month": "2026-11", "currency": "USD", "rate": "abc"}, format="json").status_code, 400)
        self.assertEqual(self.client.put(url, {"month": "2026-11", "currency": "PKR", "rate": "1"}, format="json").status_code, 400)
        self.assertEqual(self.client.put(url, {"month": "2026-11", "currency": "XYZ", "rate": "1"}, format="json").status_code, 400)

    def test_missing_rate_is_reported_not_guessed(self):
        self.add(amount="10", currency="GBP")  # no GBP rate in the fake market
        s = self.summary()
        self.assertEqual(s["total"], "0.00")
        self.assertEqual(s["unconverted"], [{"currency": "GBP", "total": "10.00", "count": 1}])

    def test_rates_are_per_user(self):
        self.client.put("/api/expenses/rates/", {"month": "2026-11", "currency": "USD", "rate": "999"}, format="json")
        other = self.client.post("/api/auth/register/", {"email": "o@exp.com", "password": "Sup3r-secret!"}, format="json")
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {other.data['access']}")
        res = self.client.get("/api/expenses/rates/?month=2026-11&currency=USD").data
        self.assertEqual(res["rates"][0]["rate"], "276.4974")


class RateStorageTests(APITestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model
        self.user = get_user_model().objects.create_user("r@exp.com", "r@exp.com", "Sup3r-secret!")

    def test_current_month_rate_is_fetched_once_a_day_even_if_market_date_lags(self):
        from .conversion import Converter
        calls = []
        def lagging(base, quote, wanted=None):
            calls.append(1)
            return {"rate": "276.38", "date": "2026-11-23", "source": "test"}  # one day behind "today"
        with mock.patch("expenses.conversion.market_rate", side_effect=lagging), \
                mock.patch("expenses.conversion.date") as fake:
            fake.today.return_value = date.today()
            fake.fromisoformat.side_effect = date.fromisoformat
            first = date.today().replace(day=1)
            for _ in range(3):
                Converter(self.user, "PKR").rate(first, "USD")
        self.assertEqual(len(calls), 1)

    def test_concurrent_create_reuses_the_stored_row(self):
        from .conversion import Converter
        first = date(2026, 10, 1)
        MonthlyRate.objects.create(user=self.user, month=first, base="USD", quote="PKR", rate=Decimal("277"), source="market")
        conv = Converter(self.user, "PKR")
        row = conv._store(first, "USD", "PKR", {"rate": "278", "date": "2026-10-31"}, None)  # simulates losing the race
        self.assertEqual(row.rate, Decimal("277"))

    def test_market_refresh_never_overwrites_a_manual_rate(self):
        from .conversion import Converter
        first = date(2026, 11, 1)
        manual = MonthlyRate.objects.create(user=self.user, month=first, base="USD", quote="PKR", rate=Decimal("280"), source="manual")
        row = Converter(self.user, "PKR")._store(first, "USD", "PKR", {"rate": "276", "date": "2026-11-23"}, manual)
        self.assertEqual((row.rate, row.source), (Decimal("280"), "manual"))
