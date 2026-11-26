"""Expense export/import: files round-trip exactly, previews save nothing, duplicates are skipped, other users' data is out of reach."""
import io
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from openpyxl import Workbook, load_workbook
from rest_framework.test import APITestCase

from .models import Expense, Project, ProjectExpense

PASSWORD = "Sup3r-secret!"


class TransferTests(APITestCase):
    def login(self, email):
        res = self.client.post("/api/auth/register/", {"email": email, "password": PASSWORD}, format="json")
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {res.data['access']}")

    def setUp(self):
        self.login("sharer@example.com")
        c = self.client
        self.project = c.post("/api/expenses/projects/", {"name": "Wedding", "template": "wedding", "budget": "50000", "currency": "PKR"}, format="json").data
        for title, amount, cat, day, cur, note in (("Hall booking", "25000", "Venue", "2026-11-26", "PKR", "advance paid"),
                                                   ("Photographer", "150.50", "Photography", "2026-12-02", "USD", ""),
                                                   ("Flowers & décor", "8000", "Decor", "2026-12-05", "PKR", "")):
            c.post(f"/api/expenses/projects/{self.project['id']}/expenses/", {"title": title, "amount": amount, "category": cat, "spent_on": day,
                                                                             "currency": cur, "note": note}, format="json")
        c.post("/api/expenses/", {"title": "Rent", "amount": "45000", "category": "rent", "spent_on": "2026-11-01"}, format="json")
        c.post("/api/expenses/", {"title": "Groceries", "amount": "12000", "category": "food", "spent_on": "2026-11-05"}, format="json")
        c.post("/api/expenses/", {"title": "October bill", "amount": "3000", "category": "bills", "spent_on": "2026-10-09"}, format="json")

    def export(self, url):
        res = self.client.get(url)
        self.assertEqual(res.status_code, 200, getattr(res, "data", ""))
        return res

    def preview(self, name, content, **extra):
        return self.client.post("/api/expenses/import/preview/", {"file": SimpleUploadedFile(name, content), **extra}, format="multipart")

    def commit(self, rows, target, **extra):
        return self.client.post("/api/expenses/import/", {"rows": rows, "target": target, **extra}, format="json")

    def test_project_excel_round_trip_into_a_new_project(self):
        res = self.export(f"/api/expenses/projects/{self.project['id']}/export/?as=xlsx")
        self.assertIn('filename="Wedding-', res["Content-Disposition"])
        wb = load_workbook(io.BytesIO(res.content))
        self.assertEqual(wb.sheetnames, ["Expenses", "Summary"])
        self.assertEqual([c.value for c in wb["Expenses"][1]], ["Date", "Title", "Category", "Amount", "Currency", "Note"])
        self.login("receiver@example.com")
        before = (Expense.objects.count(), ProjectExpense.objects.count(), Project.objects.count())
        pre = self.preview("Wedding.xlsx", res.content, target_type="new_project")
        self.assertEqual(pre.status_code, 200, pre.data)
        self.assertEqual((Expense.objects.count(), ProjectExpense.objects.count(), Project.objects.count()), before)  # preview saves nothing
        self.assertEqual(pre.data["source"], {"kind": "project", "name": "Wedding", "currency": "PKR", "budget": "50000.00", "format": "xlsx", "file": "Wedding.xlsx"})
        self.assertEqual((pre.data["valid"], pre.data["invalid"], pre.data["duplicates"]), (3, 0, 0))
        out = self.commit(pre.data["rows"], {"type": "new_project", "name": "Cousin's wedding", "currency": "PKR", "budget": "50000", "template": "wedding"})
        self.assertEqual(out.status_code, 201, out.data)
        self.assertEqual((out.data["created"], out.data["skipped_duplicates"]), (3, 0))
        mine = Project.objects.get(pk=out.data["target"]["id"])
        self.assertEqual((mine.user.email, mine.name, mine.budget), ("receiver@example.com", "Cousin's wedding", Decimal("50000")))
        got = sorted(mine.expenses.values_list("title", "amount", "currency", "category", "spent_on__day", "note"))
        self.assertEqual(got, [("Flowers & décor", Decimal("8000.00"), "PKR", "Decor", 5, ""), ("Hall booking", Decimal("25000.00"), "PKR", "Venue", 26, "advance paid"),
                               ("Photographer", Decimal("150.50"), "USD", "Photography", 2, "")])

    def test_pdf_round_trip_and_duplicates_into_existing_project(self):
        res = self.export(f"/api/expenses/projects/{self.project['id']}/export/?as=pdf")
        self.assertTrue(res.content.startswith(b"%PDF"))
        pre = self.preview("Wedding.pdf", res.content, target_type="project", project_id=self.project["id"])
        self.assertEqual(pre.status_code, 200, pre.data)
        self.assertEqual((pre.data["source"]["format"], pre.data["valid"], pre.data["duplicates"]), ("pdf", 3, 3))  # already all there
        out = self.commit(pre.data["rows"], {"type": "project", "id": self.project["id"]})
        self.assertEqual((out.data["created"], out.data["skipped_duplicates"]), (0, 3))
        out = self.commit(pre.data["rows"], {"type": "project", "id": self.project["id"]}, skip_duplicates=False)
        self.assertEqual(out.data["created"], 3)
        self.assertEqual(ProjectExpense.objects.filter(project_id=self.project["id"]).count(), 6)

    def test_monthly_export_scope_and_import_maps_categories(self):
        wb = load_workbook(io.BytesIO(self.export("/api/expenses/export/?as=xlsx&month=2026-11").content))
        self.assertEqual([r[1].value for r in wb["Expenses"].iter_rows(min_row=2)], ["Rent", "Groceries"])
        self.assertEqual(wb["Expenses"]["C2"].value, "Rent & housing")
        everything = self.export("/api/expenses/export/?as=xlsx&month=all").content
        self.assertEqual(load_workbook(io.BytesIO(everything))["Expenses"].max_row, 4)
        self.assertTrue(self.export("/api/expenses/export/?as=pdf&month=2026-11").content.startswith(b"%PDF"))
        # a project file imported into monthly expenses: unknown categories go to Other, kept in the note
        proj = self.export(f"/api/expenses/projects/{self.project['id']}/export/?as=xlsx").content
        self.login("receiver@example.com")
        pre = self.preview("w.xlsx", proj, target_type="monthly")
        out = self.commit(pre.data["rows"], {"type": "monthly"})
        self.assertEqual(out.data["created"], 3)
        hall = Expense.objects.get(user__email="receiver@example.com", title="Hall booking")
        self.assertEqual((hall.category, hall.note), ("other", "Venue: advance paid"))
        # and the monthly file's own labels map back to real categories
        pre = self.preview("m.xlsx", everything, target_type="monthly")
        self.commit(pre.data["rows"], {"type": "monthly"})
        self.assertEqual(Expense.objects.get(user__email="receiver@example.com", title="Rent").category, "rent")

    def test_someone_elses_spreadsheet_csv_and_bad_rows(self):
        wb = Workbook()
        ws = wb.active
        ws.append(["My trip costs"])
        ws.append([])
        ws.append(["Description", "Spent On", "Cost", "Remarks"])
        ws.append(["Hotel", "03/12/2026", "PKR 18,500.00", "2 nights"])
        ws.append(["Taxi", "2026-12-04", 1200, None])
        ws.append(["", "2026-12-05", 50, None])
        ws.append(["Dinner", "someday", "abc", None])
        ws.append(["Total", None, 19750, None])
        buf = io.BytesIO()
        wb.save(buf)
        pre = self.preview("trip.xlsx", buf.getvalue())
        self.assertEqual(pre.status_code, 200, pre.data)
        rows = pre.data["rows"]
        self.assertEqual((pre.data["valid"], pre.data["invalid"], len(rows)), (2, 2, 4))  # the Total line is ignored
        self.assertEqual((rows[0]["date"], rows[0]["amount"], rows[0]["currency"], rows[0]["note"]), ("2026-12-03", "18500.00", "PKR", "2 nights"))  # day first
        self.assertEqual(rows[1]["currency"], "PKR")  # falls back to the tracker currency
        self.assertIn("no title", rows[2]["error"])
        self.assertIn("date not understood", rows[3]["error"])
        self.assertEqual(self.commit(rows, {"type": "monthly"}).status_code, 400)  # bad rows must be left out by the caller
        out = self.commit([r for r in rows if not r["error"]], {"type": "new_project", "name": "Trip"})
        self.assertEqual(out.data["created"], 2)
        csv_file = "date;item;amount;currency\n2026-12-01;Visa fee;160;USD\n".encode()
        pre = self.preview("visa.csv", csv_file)
        self.assertEqual((pre.data["rows"][0]["title"], pre.data["rows"][0]["currency"], pre.data["source"]["format"]), ("Visa fee", "USD", "csv"))

    def test_refusals(self):
        from reportlab.pdfgen import canvas
        buf = io.BytesIO()
        c = canvas.Canvas(buf)
        c.drawString(72, 720, "Bank statement")
        c.save()
        res = self.preview("statement.pdf", buf.getvalue())
        self.assertEqual(res.status_code, 400)
        self.assertIn("exported from Installment Ledger", res.data["detail"])
        self.assertEqual(self.preview("notes.docx", b"hello").status_code, 400)
        self.assertEqual(self.preview("empty.csv", b"name,age\nx,1\n").status_code, 400)
        self.assertEqual(self.client.post("/api/expenses/import/preview/", {}, format="multipart").status_code, 400)
        self.assertEqual(self.client.get("/api/expenses/export/?as=docx").status_code, 400)
        self.assertEqual(self.commit([], {"type": "monthly"}).status_code, 400)
        self.assertEqual(self.commit([{"title": "x"}], {"type": "nowhere"}).status_code, 400)
        self.assertEqual(self.commit([{"title": "x", "date": "2026-01-01", "amount": "5", "currency": "PKR"}], {"type": "new_project", "name": " "}).status_code, 400)

    def test_other_users_projects_are_out_of_reach(self):
        row = [{"title": "x", "date": "2026-01-01", "amount": "5", "currency": "PKR"}]
        self.login("intruder@example.com")
        self.assertEqual(self.client.get(f"/api/expenses/projects/{self.project['id']}/export/?as=pdf").status_code, 404)
        self.assertEqual(self.commit(row, {"type": "project", "id": self.project["id"]}).status_code, 404)
        self.assertEqual(self.preview("a.csv", b"title,amount\nx,5\n", target_type="project", project_id=self.project["id"]).status_code, 404)
        self.assertEqual(ProjectExpense.objects.filter(project_id=self.project["id"]).count(), 3)
        exported = load_workbook(io.BytesIO(self.client.get("/api/expenses/export/?as=xlsx&month=all").content))
        self.assertEqual(exported["Expenses"].max_row, 1)  # the intruder's own (empty) export, none of the sharer's rows
