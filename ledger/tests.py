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


class CurrencyExchangeTests(APITestCase):
    """Debt in USD, money sent in PKR at 1 USD = 275.77 PKR."""

    def setUp(self):
        res = self.client.post("/api/auth/register/", {"email": "fx@example.com", "password": "Sup3r-secret!"}, format="json")
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {res.data['access']}")
        plan = self.client.post(
            "/api/plans/",
            {
                "item": "MacBook", "purchase_price": "1199", "total_amount": "1350", "months": 18,
                "first_payment_date": "2026-11-23", "currency": "USD", "payment_currency": "PKR",
                "exchange_rate": "275.77",
            },
            format="json",
        )
        self.assertEqual(plan.status_code, 201, plan.data)
        self.plan = plan.data

    def test_plan_shows_pkr_figures(self):
        conv = self.plan["conversion"]
        self.assertEqual(conv["currency"], "PKR")
        self.assertEqual(conv["monthly_amount"], "20682.75")   # 75 x 275.77
        self.assertEqual(conv["remaining_amount"], "372289.50")  # 1350 x 275.77

    def test_pkr_payment_is_converted_to_usd(self):
        pid = self.plan["id"]
        res = self.client.post(f"/api/plans/{pid}/payments/", {"sent_amount": "20682.75", "paid_on": "2026-11-23"}, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data["amount"], "75.00")
        self.assertEqual(res.data["sent_currency"], "PKR")
        self.assertEqual(res.data["exchange_rate"], "275.7700")

        # Next month the rupee is weaker; that payment keeps its own rate.
        res = self.client.post(
            f"/api/plans/{pid}/payments/",
            {"sent_amount": "21000", "exchange_rate": "280", "paid_on": "2026-12-23"},
            format="json",
        )
        self.assertEqual(res.data["amount"], "75.00")
        detail = self.client.get(f"/api/plans/{pid}/").data
        self.assertEqual(detail["paid_amount"], "150.00")
        self.assertEqual(detail["conversion"]["sent_total"], "41682.75")
        self.assertEqual(detail["months_left"], 16)

        summary = self.client.get("/api/summary/").data
        self.assertEqual(summary["total_left"], "1200.00")
        self.assertEqual(summary["payment_totals"][0]["currency"], "PKR")
        # The newest payment (Dec, @280) set the plan rate, so remaining = 1200 x 280.
        self.assertEqual(summary["payment_totals"][0]["total_left"], "336000.00")

    def test_usd_payment_still_works(self):
        res = self.client.post(f"/api/plans/{self.plan['id']}/payments/", {"amount": "75", "paid_on": "2026-11-23"}, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        self.assertIsNone(res.data["sent_amount"])

    def test_final_pkr_payment_rounding_settles_plan(self):
        pid = self.plan["id"]
        res = self.client.post(f"/api/plans/{pid}/payments/", {"sent_amount": "372289.52", "paid_on": "2026-11-23"}, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        self.assertTrue(self.client.get(f"/api/plans/{pid}/").data["is_complete"])

    def test_rate_required_when_currencies_differ(self):
        res = self.client.post(
            "/api/plans/",
            {"item": "X", "purchase_price": "1", "total_amount": "10", "months": 12,
             "first_payment_date": "2026-11-23", "payment_currency": "PKR"},
            format="json",
        )
        self.assertEqual(res.status_code, 400)

    def test_newest_payment_rate_becomes_plan_rate(self):
        pid = self.plan["id"]
        self.client.post(f"/api/plans/{pid}/payments/", {"sent_amount": "21000", "exchange_rate": "280", "paid_on": "2026-12-01"}, format="json")
        self.assertEqual(self.client.get(f"/api/plans/{pid}/").data["exchange_rate"], "280.0000")
        # A back-dated payment does not override the newer rate.
        self.client.post(f"/api/plans/{pid}/payments/", {"sent_amount": "20000", "exchange_rate": "270", "paid_on": "2026-11-01"}, format="json")
        self.assertEqual(self.client.get(f"/api/plans/{pid}/").data["exchange_rate"], "280.0000")


class DatedRateTests(APITestCase):
    """/api/rates/ picks the rate for the payment date (network mocked)."""

    def setUp(self):
        res = self.client.post("/api/auth/register/", {"email": "r@example.com", "password": "Sup3r-secret!"}, format="json")
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {res.data['access']}")

    def fake_source(self, published):
        def fake(base, quote, tag):
            if tag in published:
                return {"rate": Decimal(published[tag]), "date": tag if tag != "latest" else "2026-11-22", "source": "test"}
            return None
        return fake

    def test_past_date_uses_that_days_rate(self):
        from unittest import mock
        with mock.patch("ledger.views._dated_rate", self.fake_source({"2026-06-15": "278.2685"})):
            res = self.client.get("/api/rates/?base=USD&quote=PKR&date=2026-06-15")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["rate"], "278.2685")
        self.assertEqual(res.data["date"], "2026-06-15")

    def test_missing_day_falls_back_to_earlier_day(self):
        from unittest import mock
        with mock.patch("ledger.views._dated_rate", self.fake_source({"2026-06-13": "279.0000"})):
            res = self.client.get("/api/rates/?base=USD&quote=PKR&date=2026-06-15")
        self.assertEqual(res.data["date"], "2026-06-13")

    def test_future_date_uses_latest(self):
        from unittest import mock
        with mock.patch("ledger.views._dated_rate", self.fake_source({"latest": "276.4819"})):
            res = self.client.get("/api/rates/?base=USD&quote=PKR&date=2099-01-01")
        self.assertEqual(res.data["rate"], "276.4819")

    def test_before_history_is_404(self):
        self.assertEqual(self.client.get("/api/rates/?base=USD&quote=PKR&date=2019-05-24").status_code, 404)

    def test_bad_date_is_400(self):
        self.assertEqual(self.client.get("/api/rates/?base=USD&quote=PKR&date=yesterday").status_code, 400)


class ReceiptTests(APITestCase):
    def setUp(self):
        res = self.client.post("/api/auth/register/", {"email": "rc@example.com", "password": "Sup3r-secret!"}, format="json")
        self.token = res.data["access"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.token}")
        self.plan = self.client.post(
            "/api/plans/",
            {"item": "MacBook", "purchase_price": "1199", "total_amount": "1350", "months": 18,
             "first_payment_date": "2026-11-21", "payment_currency": "PKR", "exchange_rate": "276.5"},
            format="json",
        ).data

    def png(self):
        import io
        from django.core.files.uploadedfile import SimpleUploadedFile
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (20, 20), "white").save(buf, "PNG")
        return SimpleUploadedFile("receipt.png", buf.getvalue(), content_type="image/png")

    def test_upload_receipt_with_ocr_values(self):
        pid = self.plan["id"]
        res = self.client.post(
            f"/api/plans/{pid}/payments/",
            {"sent_amount": "13750", "sent_currency": "PKR", "exchange_rate": "276.5", "paid_on": "2026-11-21",
             "paid_time": "21:24", "note": "Receipt · Ref 972228", "receipt": self.png()},
            format="multipart",
        )
        self.assertEqual(res.status_code, 201, res.data)
        self.assertTrue(res.data["has_receipt"])
        self.assertEqual(res.data["paid_time"], "21:24:00")
        self.assertEqual(res.data["amount"], "49.73")  # 13750 / 276.5
        self.assertNotIn("receipt_data", res.data)

        img = self.client.get(f"/api/plans/{pid}/payments/{res.data['id']}/receipt/")
        self.assertEqual(img.status_code, 200)
        self.assertEqual(img["Content-Type"], "image/png")

        # Lists never carry the image bytes.
        detail = self.client.get(f"/api/plans/{pid}/").data
        self.assertTrue(detail["payments"][0]["has_receipt"])

    def test_non_image_is_rejected(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        res = self.client.post(
            f"/api/plans/{self.plan['id']}/payments/",
            {"sent_amount": "100", "paid_on": "2026-11-21", "receipt": SimpleUploadedFile("x.png", b"not an image")},
            format="multipart",
        )
        self.assertEqual(res.status_code, 400)

    def test_receipt_of_other_user_is_hidden(self):
        pid = self.plan["id"]
        pay = self.client.post(
            f"/api/plans/{pid}/payments/",
            {"sent_amount": "100", "paid_on": "2026-11-21", "receipt": self.png()}, format="multipart",
        ).data
        other = self.client.post("/api/auth/register/", {"email": "o@example.com", "password": "Sup3r-secret!"}, format="json")
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {other.data['access']}")
        self.assertEqual(self.client.get(f"/api/plans/{pid}/payments/{pay['id']}/receipt/").status_code, 404)
