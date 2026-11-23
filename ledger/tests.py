from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase
from rest_framework.test import APITestCase

from .schedule import add_months, build_schedule, installment_amounts

User = get_user_model()


class ScheduleMathTests(SimpleTestCase):
    def test_macbook_plan_is_75_a_month(self):
        amounts = installment_amounts(Decimal("1350.00"), 18)
        self.assertEqual(set(amounts), {Decimal("75.00")})
        self.assertEqual(sum(amounts), Decimal("1350.00"))

    def test_last_installment_absorbs_rounding(self):
        amounts = installment_amounts(Decimal("1300.00"), 18)
        self.assertEqual(amounts[0], Decimal("72.22"))
        self.assertEqual(sum(amounts), Decimal("1300.00"))
        self.assertEqual(len(amounts), 18)

    def test_add_months_clamps_to_month_end(self):
        self.assertEqual(add_months(date(2026, 1, 31), 1), date(2026, 2, 28))
        self.assertEqual(add_months(date(2026, 11, 23), 17), date(2028, 4, 23))

    def test_payments_cover_installments_in_order(self):
        rows = build_schedule(Decimal("1350"), 18, date(2026, 11, 23), Decimal("112.50"), date(2026, 11, 23))
        self.assertEqual(rows[0]["status"], "paid")
        self.assertEqual(rows[1]["status"], "partial")
        self.assertEqual(rows[1]["paid_amount"], Decimal("37.50"))
        self.assertEqual(rows[2]["status"], "upcoming")


class LedgerApiTests(APITestCase):
    def setUp(self):
        res = self.client.post("/api/auth/register/", {"email": "me@example.com", "password": "Sup3r-secret!"}, format="json")
        self.assertEqual(res.status_code, 201)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {res.data['access']}")

    def create_plan(self, **overrides):
        body = {
            "item": "MacBook (Best Buy)",
            "purchase_price": "1199.00",
            "total_amount": "1350.00",
            "months": 18,
            "first_payment_date": "2026-11-23",
            **overrides,
        }
        return self.client.post("/api/plans/", body, format="json")

    def test_create_plan_and_record_payment(self):
        res = self.create_plan()
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data["monthly_amount"], "75.00")
        self.assertEqual(res.data["months_left"], 18)
        self.assertEqual(len(res.data["schedule"]), 18)

        pid = res.data["id"]
        pay = self.client.post(f"/api/plans/{pid}/payments/", {"amount": "75.00", "paid_on": "2026-11-23"}, format="json")
        self.assertEqual(pay.status_code, 201, pay.data)

        detail = self.client.get(f"/api/plans/{pid}/").data
        self.assertEqual(detail["paid_amount"], "75.00")
        self.assertEqual(detail["remaining_amount"], "1275.00")
        self.assertEqual(detail["months_left"], 17)

        summary = self.client.get("/api/summary/").data
        self.assertEqual(summary["total_left"], "1275.00")

    def test_rejects_unsupported_month_count(self):
        self.assertEqual(self.create_plan(months=6).status_code, 400)

    def test_cannot_overpay(self):
        pid = self.create_plan(total_amount="100.00", months=12).data["id"]
        res = self.client.post(f"/api/plans/{pid}/payments/", {"amount": "100.01", "paid_on": "2026-11-23"}, format="json")
        self.assertEqual(res.status_code, 400)

    def test_users_only_see_their_own_plans(self):
        pid = self.create_plan().data["id"]
        other = self.client.post("/api/auth/register/", {"email": "other@example.com", "password": "Sup3r-secret!"}, format="json")
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {other.data['access']}")
        self.assertEqual(self.client.get(f"/api/plans/{pid}/").status_code, 404)
        self.assertEqual(self.client.get("/api/plans/").data, [])


class AuthFlowTests(APITestCase):
    def test_login_refresh_logout(self):
        self.client.post("/api/auth/register/", {"email": "a@example.com", "password": "Sup3r-secret!"}, format="json")
        login = self.client.post("/api/auth/login/", {"email": "a@example.com", "password": "Sup3r-secret!"}, format="json")
        self.assertEqual(login.status_code, 200)

        refreshed = self.client.post("/api/auth/refresh/", {"refresh": login.data["refresh"]}, format="json")
        self.assertEqual(refreshed.status_code, 200)
        self.assertIn("access", refreshed.data)
        self.assertIn("refresh", refreshed.data)  # rotated

        # The old refresh token was blacklisted on rotation.
        again = self.client.post("/api/auth/refresh/", {"refresh": login.data["refresh"]}, format="json")
        self.assertEqual(again.status_code, 401)

        out = self.client.post("/api/auth/logout/", {"refresh": refreshed.data["refresh"]}, format="json")
        self.assertEqual(out.status_code, 205)
        after = self.client.post("/api/auth/refresh/", {"refresh": refreshed.data["refresh"]}, format="json")
        self.assertEqual(after.status_code, 401)

    def test_protected_endpoints_need_a_token(self):
        self.assertEqual(self.client.get("/api/plans/").status_code, 401)

    def test_wrong_password(self):
        self.client.post("/api/auth/register/", {"email": "a@example.com", "password": "Sup3r-secret!"}, format="json")
        res = self.client.post("/api/auth/login/", {"email": "a@example.com", "password": "nope"}, format="json")
        self.assertEqual(res.status_code, 400)
