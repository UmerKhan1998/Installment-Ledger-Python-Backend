"""Plan export: PDF and Excel contain the chosen plans' figures, payments and schedule; only the owner's plans."""
import io

from openpyxl import load_workbook
from rest_framework.test import APITestCase

PASSWORD = "Sup3r-secret!"


class PlanExportTests(APITestCase):
    def login(self, email):
        res = self.client.post("/api/auth/register/", {"email": email, "password": PASSWORD}, format="json")
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {res.data['access']}")

    def setUp(self):
        self.login("exporter@example.com")
        c = self.client
        self.mac = c.post("/api/plans/", {"item": "Macbook Air: M5/2026", "purchase_price": "1199", "total_amount": "1350", "months": 18,
                                          "first_payment_date": "2026-09-01", "payment_currency": "PKR", "exchange_rate": "276.35"}, format="json").data
        c.post(f"/api/plans/{self.mac['id']}/payments/", {"sent_amount": "20820", "exchange_rate": "277.2801", "paid_on": "2026-09-01",
                                                          "reference": "100441", "note": "Raast"}, format="json")
        self.phone = c.post("/api/plans/", {"item": "Phone", "purchase_price": "500", "total_amount": "600", "months": 12,
                                            "first_payment_date": "2026-10-01"}, format="json").data

    def get(self, ids, fmt):
        return self.client.get(f"/api/plans/export/?ids={ids}&as={fmt}")

    def test_excel_has_summary_and_a_sheet_per_plan(self):
        res = self.get(f"{self.phone['id']},{self.mac['id']}", "xlsx")
        self.assertEqual(res.status_code, 200)
        self.assertIn("installment-plans-", res["Content-Disposition"])
        wb = load_workbook(io.BytesIO(res.content))
        self.assertEqual(wb.sheetnames, ["Summary", "Phone", "Macbook Air  M5 2026"])  # chosen order; : and / are illegal
        summary = wb["Summary"]
        self.assertEqual([summary.cell(row=r, column=1).value for r in (5, 6)], ["Phone", "Macbook Air: M5/2026"])
        self.assertEqual(summary.cell(row=6, column=4).value, 1350)
        mac = wb["Macbook Air  M5 2026"]
        values = [c.value for row in mac.iter_rows() for c in row if c.value is not None]
        self.assertIn("Raast", values)
        self.assertIn(20820, values)
        status = [r[4].value for r in mac.iter_rows() if r[4].value in ("Paid", "Partly paid", "Partly paid (late)", "Overdue", "Upcoming")]
        self.assertEqual(len(status), 18)  # one schedule row per month
        self.assertEqual(status[0], "Paid")

    def test_pdf_single_plan(self):
        res = self.get(self.mac["id"], "pdf")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res["Content-Type"], "application/pdf")
        self.assertTrue(res.content.startswith(b"%PDF"))
        self.assertIn('filename="Macbook-Air-M5-2026-', res["Content-Disposition"])

    def test_bad_requests_and_other_users_plans(self):
        self.assertEqual(self.get("", "pdf").status_code, 400)
        self.assertEqual(self.get("abc", "pdf").status_code, 400)
        self.assertEqual(self.get(self.mac["id"], "docx").status_code, 400)
        self.login("someone-else@example.com")
        self.assertEqual(self.get(self.mac["id"], "pdf").status_code, 404)
        mine = self.client.post("/api/plans/", {"item": "Mine", "purchase_price": "1", "total_amount": "100", "months": 12,
                                                "first_payment_date": "2026-10-01"}, format="json").data
        self.assertEqual(self.get(f"{mine['id']},{self.mac['id']}", "xlsx").status_code, 404)  # can't mix in A's plan
        self.client.credentials()
        self.assertIn(self.get(mine["id"], "pdf").status_code, (401, 403))
