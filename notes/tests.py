"""Notes: pinned first then newest edit, search, validation."""
from rest_framework.test import APITestCase


class NoteTests(APITestCase):
    def setUp(self):
        res = self.client.post("/api/auth/register/", {"email": "notes@example.com", "password": "Sup3r-secret!"}, format="json")
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {res.data['access']}")

    def add(self, **body):
        res = self.client.post("/api/notes/", body, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        return res.data

    def titles(self, url="/api/notes/"):
        return [n["title"] or n["body"] for n in self.client.get(url).data]

    def test_order_pin_edit_and_delete(self):
        a = self.add(title="Bank details", body="IBAN PK00 ...")
        b = self.add(body="just text, no title")
        c = self.add(title="  Cousin's   address ", color="yellow")
        self.assertEqual(self.titles(), ["Cousin's address", "just text, no title", "Bank details"])  # newest edit first
        self.client.patch(f"/api/notes/{a['id']}/", {"pinned": True}, format="json")
        self.assertEqual(self.titles()[0], "Bank details")  # pinned jumps to the top
        res = self.client.patch(f"/api/notes/{b['id']}/", {"body": "edited\r\nline two  "}, format="json")
        self.assertEqual(res.data["body"], "edited\nline two")
        self.assertEqual(self.titles(), ["Bank details", "edited\nline two", "Cousin's address"])
        self.assertEqual(self.client.delete(f"/api/notes/{c['id']}/").status_code, 204)
        self.assertEqual(len(self.titles()), 2)

    def test_search_and_validation(self):
        self.add(title="Raast limits", body="Daily limit is PKR 500,000")
        self.add(title="Groceries", body="rice, daal")
        self.assertEqual(self.titles("/api/notes/?q=DAILY"), ["Raast limits"])
        self.assertEqual(self.titles("/api/notes/?q=grocer"), ["Groceries"])
        self.assertEqual(self.client.post("/api/notes/", {"title": "  ", "body": ""}, format="json").status_code, 400)
        self.assertEqual(self.client.post("/api/notes/", {"body": "x" * 20_001}, format="json").status_code, 400)
        self.assertEqual(self.client.post("/api/notes/", {"body": "ok", "color": "neon"}, format="json").status_code, 400)
        note = self.add(body="keep me")
        self.assertEqual(self.client.patch(f"/api/notes/{note['id']}/", {"body": ""}, format="json").status_code, 400)  # can't empty it
