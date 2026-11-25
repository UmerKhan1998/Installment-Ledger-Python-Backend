"""Cross-user isolation: user B must not be able to see, change or delete anything that belongs to user A.

Every API route is enumerated from the URL conf; any route with an id in its path that this test doesn't know
how to fill makes the test fail, so new endpoints can't silently skip this check.
"""
import io
import re
from datetime import date

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import URLPattern, URLResolver, get_resolver
from PIL import Image
from rest_framework.test import APITestCase

from expenses.models import Expense, ExpenseSettings, GlobalBudget, MonthBudget, MonthlyRate, Project, ProjectExpense
from finance.models import FinancialProfile, Goal
from ledger.models import Payment, Plan

PASSWORD = "Sup3r-secret!"


def all_routes(resolver=None, prefix=""):
    resolver = resolver or get_resolver()
    for p in resolver.url_patterns:
        route = prefix + str(p.pattern)
        if isinstance(p, URLResolver):
            yield from all_routes(p, route)
        elif isinstance(p, URLPattern):
            yield route


def png():
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (10, 200, 30)).save(buf, "PNG")
    return SimpleUploadedFile("a.png", buf.getvalue(), content_type="image/png")


class CrossUserIsolationTests(APITestCase):
    def login(self, email):
        res = self.client.post("/api/auth/register/", {"email": email, "password": PASSWORD}, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {res.data['access']}")
        return res.data["user"]["id"]

    def setUp(self):
        # ---- user A creates one of everything ----
        self.a = self.login("alice@example.com")
        c = self.client
        self.plan = c.post("/api/plans/", {"item": "A's MacBook", "purchase_price": "1199", "total_amount": "1350", "months": 18,
                                           "first_payment_date": "2026-10-01", "payment_currency": "PKR", "exchange_rate": "276"}, format="json").data
        self.payment = c.post(f"/api/plans/{self.plan['id']}/payments/",
                              {"sent_amount": "20000", "exchange_rate": "276", "paid_on": "2026-10-02", "reference": "REF-A-1",
                               "receipt": png()}, format="multipart").data
        self.expense = c.post("/api/expenses/", {"title": "A's rent", "amount": "40000", "category": "rent",
                                                 "spent_on": "2026-11-01", "is_recurring": True}, format="json").data
        c.patch("/api/expenses/settings/", {"linked_plans": [self.plan["id"]]}, format="json")
        c.put("/api/expenses/budget/", {"scope": "month", "month": "2026-11", "amount": "99999"}, format="json")
        c.put("/api/expenses/rates/", {"month": "2026-11", "currency": "USD", "rate": "999"}, format="json")
        self.project = c.post("/api/expenses/projects/", {"name": "A's wedding", "template": "wedding", "budget": "500000"}, format="json").data
        self.pexp = c.post(f"/api/expenses/projects/{self.project['id']}/expenses/",
                           {"title": "A's venue", "amount": "100000", "category": "Venue", "spent_on": "2026-11-02"}, format="json").data
        self.goal = c.post("/api/finance/goals/", {"name": "A's goal", "target_amount": "777777", "target_date": "2028-01-01"}, format="json").data
        c.patch("/api/finance/profile/", {"monthly_income": "123456", "consent_use_ledger": True}, format="json")
        self.counts = self.snapshot()

        # ---- user B ----
        self.b = self.login("bob@example.com")

    def snapshot(self):
        return {
            "plans": Plan.objects.filter(user_id=self.a).count(),
            "payments": Payment.objects.filter(plan__user_id=self.a).count(),
            "expenses": Expense.objects.filter(user_id=self.a).count(),
            "projects": Project.objects.filter(user_id=self.a).count(),
            "project_expenses": ProjectExpense.objects.filter(project__user_id=self.a).count(),
            "month_budget": list(MonthBudget.objects.filter(user_id=self.a).values_list("amount", flat=True)),
            "rates": list(MonthlyRate.objects.filter(user_id=self.a).values_list("rate", flat=True)),
            "linked": list(ExpenseSettings.objects.get(user_id=self.a).linked_plans.values_list("id", flat=True)),
            "plan_item": Plan.objects.get(pk=self.plan["id"]).item,
            "expense_title": Expense.objects.get(pk=self.expense["id"]).title,
            "project_name": Project.objects.get(pk=self.project["id"]).name,
            "goal_name": Goal.objects.get(pk=self.goal["id"]).name,
            "income": FinancialProfile.objects.get(user_id=self.a).monthly_income,
        }

    def ids(self):
        return {"pk_plan": self.plan["id"], "plan_id": self.plan["id"], "pk_payment": self.payment["id"],
                "pk_expense": self.expense["id"], "project_id": self.project["id"], "pk_project": self.project["id"],
                "pk_pexp": self.pexp["id"], "pk_goal": self.goal["id"]}

    def object_urls(self):
        """Every route with an id, filled with user A's ids."""
        i = self.ids()
        return {
            "api/plans/<int:pk>/": f"/api/plans/{i['pk_plan']}/",
            "api/plans/<int:plan_id>/payments/": f"/api/plans/{i['plan_id']}/payments/",
            "api/plans/<int:plan_id>/payments/<int:pk>/": f"/api/plans/{i['plan_id']}/payments/{i['pk_payment']}/",
            "api/plans/<int:plan_id>/payments/<int:pk>/receipt/": f"/api/plans/{i['plan_id']}/payments/{i['pk_payment']}/receipt/",
            "api/plans/<int:plan_id>/payments/bulk-delete/": f"/api/plans/{i['plan_id']}/payments/bulk-delete/",
            "api/expenses/<int:pk>/": f"/api/expenses/{i['pk_expense']}/",
            "api/expenses/projects/<int:pk>/": f"/api/expenses/projects/{i['pk_project']}/",
            "api/expenses/projects/<int:project_id>/expenses/": f"/api/expenses/projects/{i['project_id']}/expenses/",
            "api/expenses/projects/<int:project_id>/expenses/<int:pk>/": f"/api/expenses/projects/{i['project_id']}/expenses/{i['pk_pexp']}/",
            "api/expenses/projects/<int:project_id>/expenses/bulk-delete/": f"/api/expenses/projects/{i['project_id']}/expenses/bulk-delete/",
            "api/finance/goals/<int:pk>/": f"/api/finance/goals/{i['pk_goal']}/",
        }

    def test_every_id_route_is_covered(self):
        with_ids = {r for r in all_routes() if "<" in r and not r.startswith("admin")}
        missing = with_ids - set(self.object_urls())
        self.assertEqual(missing, set(), f"Add these routes to the isolation test: {sorted(missing)}")

    def test_b_cannot_read_change_or_delete_a_objects(self):
        bodies = {"amount": "1", "item": "hacked", "title": "hacked", "name": "hacked", "status": "closed",
                  "paid_on": "2026-11-01", "spent_on": "2026-11-01", "ids": [self.payment["id"], self.expense["id"], self.pexp["id"]]}
        for route, url in self.object_urls().items():
            for method in ("get", "patch", "put", "delete", "post"):
                res = getattr(self.client, method)(url, bodies, format="json")
                self.assertIn(res.status_code, (403, 404, 405), f"{method.upper()} {url} -> {res.status_code} {getattr(res, 'data', '')}")
        self.assertEqual(self.snapshot(), self.counts)

    def test_b_sees_none_of_a_data_in_lists_and_summaries(self):
        c = self.client
        a_markers = ("A's", "REF-A-1", "99999", "500000", "40000", "777777", "123456")
        for url in ("/api/plans/", "/api/summary/", "/api/expenses/?month=2026-11", "/api/expenses/summary/?month=2026-11",
                    "/api/expenses/projects/", "/api/expenses/settings/", "/api/expenses/budget/?month=2026-11",
                    "/api/expenses/rates/?month=2026-11", "/api/expenses/rates/history/?months=24", "/api/auth/me/",
                    "/api/payments/duplicate-check/?reference=REF-A-1", "/api/finance/profile/", "/api/finance/assessment/",
                    "/api/finance/goals/", "/api/ai/settings/"):
            res = c.get(url)
            self.assertEqual(res.status_code, 200, url)
            body = res.content.decode()
            for marker in a_markers:
                self.assertNotIn(marker, body, f"{url} leaks {marker!r}")
        self.assertFalse(c.get("/api/payments/duplicate-check/?reference=REF-A-1").data["duplicate"])
        self.assertEqual(c.get("/api/auth/me/").data["id"], self.b)

    def test_b_cannot_point_own_requests_at_a_objects(self):
        c = self.client
        # link A's plan to B's expenses
        self.assertEqual(c.patch("/api/expenses/settings/", {"linked_plans": [self.plan["id"]]}, format="json").status_code, 400)
        # bulk-delete A's rows through B's own (empty) collections
        self.assertIn(c.post("/api/expenses/bulk-delete/", {"ids": [self.expense["id"]]}, format="json").status_code, (400, 404))
        # copy A's recurring expenses into B's month
        self.assertEqual(c.post("/api/expenses/copy-recurring/", {"month": "2026-12"}, format="json").data, [])
        # B's own month budget / manual rate don't touch A's
        c.put("/api/expenses/budget/", {"scope": "month", "month": "2026-11", "amount": "1"}, format="json")
        c.put("/api/expenses/rates/", {"month": "2026-11", "currency": "USD", "rate": "1"}, format="json")
        c.delete("/api/expenses/budget/?month=2026-11")
        c.delete("/api/expenses/rates/?month=2026-11&currency=USD")
        # B's plan with A's payment reference / receipt is not flagged against A's data
        mine = c.post("/api/plans/", {"item": "B plan", "purchase_price": "1", "total_amount": "100", "months": 12,
                                      "first_payment_date": "2026-10-01"}, format="json").data
        res = c.post(f"/api/plans/{mine['id']}/payments/", {"amount": "10", "paid_on": "2026-10-02", "reference": "REF-A-1"}, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(self.snapshot(), self.counts)

    def test_unauthenticated_requests_get_nothing(self):
        self.client.credentials()
        for route in all_routes():
            if not route.startswith("api/") or route.startswith(("api/auth/register", "api/auth/login", "api/auth/refresh", "api/auth/logout")):
                continue
            url = "/" + re.sub(r"<int:\w+>", "1", route)
            res = self.client.get(url)
            self.assertIn(res.status_code, (401, 403, 405), f"GET {url} without login -> {res.status_code}")
        self.assertTrue(date.today())
