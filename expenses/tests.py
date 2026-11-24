from datetime import date
from decimal import Decimal
from unittest import mock

from rest_framework.test import APITestCase

from .models import GlobalBudget, MonthlyRate, ProjectExpense


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
        self.assertEqual(self.client.get("/api/expenses/settings/").data, {"currency": "PKR", "linked_plans": []})
        res = self.client.patch("/api/expenses/settings/", {"currency": "USD"}, format="json")
        self.assertEqual(res.data, {"currency": "USD", "linked_plans": []})

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
        self.client.put("/api/expenses/budget/", {"scope": "month", "month": "2026-11", "amount": "60000"}, format="json")
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
        self.client.put("/api/expenses/budget/", {"scope": "month", "month": "2026-11", "amount": "100000"}, format="json")
        self.add(title="Rent", amount="40000", category="rent")
        self.add(title="Laptop plan", amount="75", currency="USD")
        res = self.client.patch("/api/expenses/settings/", {"currency": "USD"}, format="json")
        # The stored budget is never rewritten...
        self.assertEqual(res.data, {"currency": "USD", "linked_plans": []})
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
        res = self.client.put("/api/expenses/budget/", {"scope": "month", "month": "2026-11", "amount": "400"}, format="json")
        self.assertEqual(res.data["applies"], {"amount": "400.00", "currency": "USD", "source": "month"})

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


class LinkedPlanTests(APITestCase):
    """Payments of a linked installment plan count as monthly expenses."""

    RATES = {("PKR", "USD", None): "0.00361818", ("USD", "PKR", None): "276.38228878",
             ("PKR", "USD", "2026-10-31"): "0.0036145", ("USD", "PKR", "2026-10-31"): "276.66333169"}

    def setUp(self):
        res = self.client.post("/api/auth/register/", {"email": "lp@exp.com", "password": "Sup3r-secret!"}, format="json")
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {res.data['access']}")
        patcher = mock.patch("expenses.conversion.market_rate", side_effect=fake_market(self.RATES))
        patcher.start()
        self.addCleanup(patcher.stop)
        fake_today = mock.patch("expenses.conversion.date")
        fake = fake_today.start()
        fake.today.return_value = date(2026, 11, 24)
        fake.fromisoformat.side_effect = date.fromisoformat
        self.addCleanup(fake_today.stop)
        self.plan = self.client.post("/api/plans/", {
            "item": "MacBook", "purchase_price": "1199", "total_amount": "1350", "months": 18,
            "first_payment_date": "2026-10-01", "payment_currency": "PKR", "exchange_rate": "276.5",
        }, format="json").data

    def pay(self, sent, on, rate="276.5"):
        res = self.client.post(f"/api/plans/{self.plan['id']}/payments/",
                               {"sent_amount": sent, "exchange_rate": rate, "paid_on": on}, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        return res.data

    def link(self, ids):
        return self.client.patch("/api/expenses/settings/", {"linked_plans": ids}, format="json")

    def summary(self, month="2026-11"):
        return self.client.get(f"/api/expenses/summary/?month={month}").data

    def test_not_counted_until_linked(self):
        self.pay("20820", "2026-11-01")
        self.assertEqual(self.summary()["total"], "0.00")
        self.assertEqual(self.summary()["plan_payments"], [])

    def test_linked_payments_count_at_the_amount_actually_sent(self):
        self.client.post("/api/expenses/", {"title": "Rent", "amount": "40000", "category": "rent", "spent_on": "2026-11-01"}, format="json")
        self.pay("20820", "2026-11-01")
        self.pay("20000", "2026-10-15")  # October: not in November's total
        res = self.link([self.plan["id"]])
        self.assertEqual(res.data["linked_plans"], [self.plan["id"]])
        s = self.summary()
        self.assertEqual(s["total"], "60820.00")  # 40000 + PKR 20,820 sent (not its USD value)
        self.assertEqual(s["count"], 2)
        inst = next(c for c in s["by_category"] if c["category"] == "installments")
        self.assertEqual((inst["label"], inst["total"]), ("Installments", "20820.00"))
        row = s["plan_payments"][0]
        self.assertEqual((row["plan_item"], row["amount"], row["currency"], row["spent_on"], row["converted"]),
                         ("MacBook", "20820.00", "PKR", "2026-11-01", None))
        self.assertEqual({t["month"]: t["total"] for t in s["trend"]}["2026-10"], "20000.00")
        card = s["linked_plans"][0]
        self.assertEqual(card["paid_this_month"], "20820.00")
        self.assertEqual(card["due_numbers"], [2])  # installment #2 is due in November

    def test_linked_payments_are_converted_when_tracking_in_usd(self):
        self.pay("20820", "2026-11-01")
        self.link([self.plan["id"]])
        self.client.patch("/api/expenses/settings/", {"currency": "USD"}, format="json")
        s = self.summary()
        self.assertEqual(s["total"], "75.33")  # 20820 x 0.00361818 = 75.3305 -> 75.33
        self.assertEqual(s["plan_payments"][0]["converted"]["amount"], "75.33")
        self.assertIsNone(s["linked_plans"][0]["due_left_converted"])  # plan is in USD, same as tracker

    def test_only_own_plans_can_be_linked(self):
        other = self.client.post("/api/auth/register/", {"email": "x2@exp.com", "password": "Sup3r-secret!"}, format="json")
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {other.data['access']}")
        self.assertEqual(self.link([self.plan["id"]]).status_code, 400)

    def test_unlink_and_delete(self):
        self.pay("20820", "2026-11-01")
        self.link([self.plan["id"]])
        self.assertEqual(self.link([]).data["linked_plans"], [])
        self.assertEqual(self.summary()["total"], "0.00")
        self.link([self.plan["id"]])
        self.client.delete(f"/api/plans/{self.plan['id']}/")
        self.assertEqual(self.client.get("/api/expenses/settings/").data["linked_plans"], [])
        self.assertEqual(self.summary()["linked_plans"], [])



class BudgetTests(APITestCase):
    """Global budget (from a month onward, versioned) + per-month budgets. "Today" is Nov 24, 2026."""

    def setUp(self):
        res = self.client.post("/api/auth/register/", {"email": "b@exp.com", "password": "Sup3r-secret!"}, format="json")
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {res.data['access']}")
        patcher = mock.patch("expenses.views.date")
        fake = patcher.start()
        fake.today.return_value = date(2026, 11, 24)
        fake.side_effect = lambda *a, **k: date(*a, **k)
        self.addCleanup(patcher.stop)

    def put(self, **body):
        return self.client.put("/api/expenses/budget/", body, format="json")

    def budget(self, month):
        return self.client.get(f"/api/expenses/budget/?month={month}").data

    def summary(self, month):
        return self.client.get(f"/api/expenses/summary/?month={month}").data

    def test_global_budget_applies_from_this_month_on_not_before(self):
        res = self.put(scope="global", amount="120000")
        self.assertEqual(res.data["global_now"]["amount"], "120000.00")
        self.assertEqual(self.budget("2026-11")["applies"], {"amount": "120000.00", "currency": "PKR", "source": "global"})
        self.assertEqual(self.budget("2027-03")["applies"]["amount"], "120000.00")   # future months follow
        self.assertEqual(self.budget("2026-10")["applies"]["amount"], None)          # past months don't
        self.assertIsNone(self.summary("2026-10")["budget"])

    def test_changing_global_keeps_old_value_for_past_months(self):
        GlobalBudget.objects.create(user_id=self.user_id(), effective_from=date(2026, 6, 1), amount="100000", currency="PKR")
        self.put(scope="global", amount="150000")
        self.assertEqual(self.budget("2026-08")["applies"]["amount"], "100000.00")  # before the change
        self.assertEqual(self.budget("2026-11")["applies"]["amount"], "150000.00")  # from this month
        self.assertEqual(self.summary("2026-08")["budget"], "100000.00")
        hist = self.budget("2026-11")["global_history"]
        self.assertEqual([(h["effective_from"], h["amount"]) for h in hist], [("2026-11", "150000.00"), ("2026-06", "100000.00")])

    def test_changing_global_twice_in_a_month_updates_that_version(self):
        self.put(scope="global", amount="100000")
        self.put(scope="global", amount="110000")
        self.assertEqual(GlobalBudget.objects.count(), 1)
        self.assertEqual(self.budget("2026-11")["applies"]["amount"], "110000.00")

    def test_global_from_a_future_month_and_not_from_the_past(self):
        self.put(scope="global", amount="100000")
        self.put(scope="global", amount="130000", from_month="2027-01")
        self.assertEqual(self.budget("2026-12")["applies"]["amount"], "100000.00")
        self.assertEqual(self.budget("2027-01")["applies"]["amount"], "130000.00")
        self.assertEqual(self.put(scope="global", amount="1", from_month="2026-10").status_code, 400)

    def test_removing_global_budget_from_this_month(self):
        GlobalBudget.objects.create(user_id=self.user_id(), effective_from=date(2026, 6, 1), amount="100000", currency="PKR")
        self.put(scope="global", amount=None)
        self.assertIsNone(self.budget("2026-11")["applies"]["amount"])
        self.assertEqual(self.budget("2026-09")["applies"]["amount"], "100000.00")

    def test_month_budget_overrides_and_resets_to_global(self):
        self.put(scope="global", amount="120000")
        res = self.put(scope="month", month="2026-12", amount="200000")  # e.g. a wedding month
        self.assertEqual(res.data["applies"], {"amount": "200000.00", "currency": "PKR", "source": "month"})
        self.assertEqual(self.budget("2027-01")["applies"]["amount"], "120000.00")  # only that month
        self.assertEqual(self.summary("2026-12")["budget_source"], "month")
        reset = self.client.delete("/api/expenses/budget/?month=2026-12").data
        self.assertEqual(reset["applies"], {"amount": "120000.00", "currency": "PKR", "source": "global"})
        self.assertIsNone(reset["month_budget"])

    def test_past_month_can_get_its_own_budget(self):
        self.put(scope="global", amount="120000")
        self.put(scope="month", month="2026-09", amount="90000")
        self.assertEqual(self.budget("2026-09")["applies"]["amount"], "90000.00")
        self.assertIsNone(self.budget("2026-10")["applies"]["amount"])

    def test_validation(self):
        self.assertEqual(self.put(scope="month", month="2026-11", amount="-5").status_code, 400)
        self.assertEqual(self.put(scope="month", month="2026-11", amount=None).status_code, 400)
        self.assertEqual(self.put(scope="month", month="2026-11", amount="abc").status_code, 400)
        self.assertEqual(self.put(scope="other", amount="1").status_code, 400)

    def test_budgets_are_per_user(self):
        self.put(scope="global", amount="120000")
        other = self.client.post("/api/auth/register/", {"email": "b2@exp.com", "password": "Sup3r-secret!"}, format="json")
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {other.data['access']}")
        self.assertIsNone(self.budget("2026-11")["applies"]["amount"])

    def test_migration_keeps_the_old_budget_for_every_month(self):
        from importlib import import_module
        from django.apps import apps
        from .models import ExpenseSettings
        mig = import_module("expenses.migrations.0005_budgets")

        class Prefs:  # the removed fields, as the migration saw them
            def __init__(self, user_id):
                self.user_id, self.monthly_budget, self.budget_currency, self.currency = user_id, Decimal("80000"), "PKR", "PKR"

        fake_apps = mock.Mock()
        fake_apps.get_model.side_effect = lambda app, name: (
            mock.Mock(objects=mock.Mock(exclude=lambda **kw: [Prefs(self.user_id())])) if name == "ExpenseSettings"
            else apps.get_model(app, name)
        )
        mig.copy_budget_to_global(fake_apps, None)
        self.assertEqual(self.budget("2025-01")["applies"]["amount"], "80000.00")
        self.assertTrue(self.budget("2026-11")["global_now"]["since_start"])
        self.assertTrue(ExpenseSettings)

    def user_id(self):
        from django.contrib.auth import get_user_model
        return get_user_model().objects.get(username="b@exp.com").id


class ProjectTests(APITestCase):
    """Separate expense books (wedding, Qurbani cow...), never counted in monthly expenses."""

    def setUp(self):
        res = self.client.post("/api/auth/register/", {"email": "pj@exp.com", "password": "Sup3r-secret!"}, format="json")
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {res.data['access']}")
        patcher = mock.patch("expenses.conversion.market_rate", side_effect=fake_market({("USD", "PKR", "2026-10-31"): "277.12345678"}))
        patcher.start()
        self.addCleanup(patcher.stop)
        fake_today = mock.patch("expenses.conversion.date")
        fake = fake_today.start()
        fake.today.return_value = date(2026, 11, 24)
        fake.fromisoformat.side_effect = date.fromisoformat
        self.addCleanup(fake_today.stop)

    def project(self, **kw):
        body = {"name": "Wedding", "template": "wedding", "budget": "2000000", "start_date": "2026-09-01", **kw}
        res = self.client.post("/api/expenses/projects/", body, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        return res.data

    def spend(self, pid, **kw):
        body = {"title": "Hall booking", "amount": "350000", "category": "Venue", "spent_on": "2026-10-05", **kw}
        res = self.client.post(f"/api/expenses/projects/{pid}/expenses/", body, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        return res.data

    def test_create_from_template(self):
        p = self.project()
        self.assertEqual(p["currency"], "PKR")  # defaults to the tracker currency
        self.assertIn("Catering", p["categories"])
        cow = self.project(name="Qurbani cow", template="qurbani", budget=None)
        self.assertEqual(cow["categories"][:2], ["Animal", "Feed"])
        self.assertIsNone(cow["remaining"])

    def test_totals_and_breakdown(self):
        pid = self.project()["id"]
        self.spend(pid)
        self.spend(pid, title="Caterer advance", amount="150000", category="Catering", spent_on="2026-10-20")
        self.spend(pid, title="Bridal dress", amount="1000", currency="USD", category="Clothing", spent_on="2026-10-28")
        p = self.client.get(f"/api/expenses/projects/{pid}/").data
        self.assertEqual(p["spent"], "777123.46")  # 350000 + 150000 + 1000 x 277.12345678
        self.assertEqual(p["remaining"], "1222876.54")
        self.assertEqual(p["count"], 3)
        self.assertEqual([c["category"] for c in p["by_category"]], ["Venue", "Clothing", "Catering"])
        self.assertEqual(p["by_month"], [{"month": "2026-10", "total": "777123.46"}])
        rows = self.client.get(f"/api/expenses/projects/{pid}/expenses/").data
        dress = next(r for r in rows if r["title"] == "Bridal dress")
        self.assertEqual(dress["converted"]["amount"], "277123.46")
        self.assertEqual(sum(Decimal(r["converted"]["amount"] if r["converted"] else r["amount"]) for r in rows), Decimal(p["spent"]))

    def test_new_category_is_remembered(self):
        pid = self.project()["id"]
        self.spend(pid, category="Mehndi night")
        self.assertIn("Mehndi night", self.client.get(f"/api/expenses/projects/{pid}/").data["categories"])

    def test_never_counted_in_monthly_expenses(self):
        pid = self.project()["id"]
        self.spend(pid, spent_on="2026-11-02")
        self.assertEqual(self.client.get("/api/expenses/summary/?month=2026-11").data["total"], "0.00")
        self.assertEqual(self.client.get("/api/expenses/?month=2026-11").data, [])

    def test_edit_close_delete(self):
        pid = self.project()["id"]
        eid = self.spend(pid)["id"]
        self.assertEqual(self.client.patch(f"/api/expenses/projects/{pid}/expenses/{eid}/", {"amount": "360000"}, format="json").data["amount"], "360000.00")
        self.assertEqual(self.client.patch(f"/api/expenses/projects/{pid}/", {"status": "closed"}, format="json").data["status"], "closed")
        self.assertEqual(self.client.patch(f"/api/expenses/projects/{pid}/", {"end_date": "2026-08-01"}, format="json").status_code, 400)
        self.assertEqual(self.client.delete(f"/api/expenses/projects/{pid}/").status_code, 204)
        self.assertEqual(ProjectExpense.objects.count(), 0)

    def test_bulk_delete_is_all_or_nothing(self):
        a = self.project()["id"]
        b = self.project(name="Cow", template="qurbani")["id"]
        x, y = self.spend(a)["id"], self.spend(a, title="Decor")["id"]
        other = self.spend(b, title="Cow")["id"]
        self.assertEqual(self.client.post(f"/api/expenses/projects/{a}/expenses/bulk-delete/", {"ids": [x, other]}, format="json").status_code, 404)
        self.assertEqual(self.client.post(f"/api/expenses/projects/{a}/expenses/bulk-delete/", {"ids": [x, y]}, format="json").data, {"deleted": 2})

    def test_validation_and_isolation(self):
        self.assertEqual(self.client.post("/api/expenses/projects/", {"name": " "}, format="json").status_code, 400)
        self.assertEqual(self.client.post("/api/expenses/projects/", {"name": "X", "template": "nope"}, format="json").status_code, 400)
        pid = self.project()["id"]
        other = self.client.post("/api/auth/register/", {"email": "pj2@exp.com", "password": "Sup3r-secret!"}, format="json")
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {other.data['access']}")
        self.assertEqual(self.client.get(f"/api/expenses/projects/{pid}/").status_code, 404)
        self.assertEqual(self.client.post(f"/api/expenses/projects/{pid}/expenses/", {"title": "x", "amount": "1", "spent_on": "2026-11-01"}, format="json").status_code, 404)
        self.assertEqual(self.client.get("/api/expenses/projects/").data, [])
