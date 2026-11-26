"""Tasks: one set of rows behind the to-do list and the Kanban board; columns stay densely ordered."""
from rest_framework.test import APITestCase

from .models import Task


class TaskTests(APITestCase):
    def setUp(self):
        res = self.client.post("/api/auth/register/", {"email": "tasks@example.com", "password": "Sup3r-secret!"}, format="json")
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {res.data['access']}")

    def add(self, title, **extra):
        res = self.client.post("/api/tasks/", {"title": title, **extra}, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        return res.data

    def board(self):
        cols = {"todo": [], "doing": [], "done": []}
        for t in self.client.get("/api/tasks/").data:
            cols[t["status"]].append((t["title"], t["position"]))
        return cols

    def test_create_goes_to_bottom_and_title_is_cleaned(self):
        self.add("  Pay   installment ")
        self.add("Call bank", priority=1, due_date="2026-12-01")
        self.add("Old win", status="done")
        self.assertEqual(self.board(), {"todo": [("Pay installment", 0), ("Call bank", 1)], "doing": [], "done": [("Old win", 0)]})
        self.assertIsNotNone(Task.objects.get(title="Old win").completed_at)
        self.assertEqual(self.client.post("/api/tasks/", {"title": "   "}, format="json").status_code, 400)

    def test_move_between_and_within_columns(self):
        a, b, c = self.add("A"), self.add("B"), self.add("C")
        # drop C at the top of "doing", then B above it
        self.client.post(f"/api/tasks/{c['id']}/move/", {"status": "doing", "index": 0}, format="json")
        res = self.client.post(f"/api/tasks/{b['id']}/move/", {"status": "doing", "index": 0}, format="json")
        self.assertEqual(len(res.data), 3)  # the whole board comes back
        self.assertEqual(self.board(), {"todo": [("A", 0)], "doing": [("B", 0), ("C", 1)], "done": []})
        # reorder inside a column; an index past the end means bottom
        self.client.post(f"/api/tasks/{b['id']}/move/", {"status": "doing", "index": 99}, format="json")
        self.assertEqual(self.board()["doing"], [("C", 0), ("B", 1)])
        # done stamps completed_at; moving back clears it
        self.client.post(f"/api/tasks/{a['id']}/move/", {"status": "done"}, format="json")
        self.assertIsNotNone(Task.objects.get(pk=a["id"]).completed_at)
        self.client.post(f"/api/tasks/{a['id']}/move/", {"status": "todo"}, format="json")
        self.assertIsNone(Task.objects.get(pk=a["id"]).completed_at)
        self.assertEqual(self.client.post(f"/api/tasks/{a['id']}/move/", {"status": "nope"}, format="json").status_code, 400)
        self.assertEqual(self.client.post(f"/api/tasks/{a['id']}/move/", {"index": "x"}, format="json").status_code, 400)

    def test_patch_status_ticks_done_and_delete_closes_the_gap(self):
        a, b, c = self.add("A"), self.add("B"), self.add("C")
        res = self.client.patch(f"/api/tasks/{a['id']}/", {"status": "done", "title": "A!"}, format="json")  # the list checkbox
        self.assertEqual((res.data["status"], res.data["title"]), ("done", "A!"))
        self.assertIsNotNone(res.data["completed_at"])
        self.assertEqual(self.board()["todo"], [("B", 0), ("C", 1)])
        self.client.delete(f"/api/tasks/{b['id']}/")
        self.assertEqual(self.board()["todo"], [("C", 0)])

    def test_filters_and_clear_done(self):
        self.add("Renew passport", notes="embassy form")
        self.add("Buy cable", status="done")
        self.add("Fix bike", status="done")
        self.assertEqual([t["title"] for t in self.client.get("/api/tasks/?q=EMBASSY").data], ["Renew passport"])
        self.assertEqual(len(self.client.get("/api/tasks/?status=done").data), 2)
        self.assertEqual(self.client.post("/api/tasks/clear-done/").data, {"deleted": 2})
        self.assertEqual([t["title"] for t in self.client.get("/api/tasks/").data], ["Renew passport"])
