"""Endpoints for exporting and importing expenses (see transfer.py)."""
from datetime import date
from decimal import Decimal

from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from rest_framework.response import Response
from rest_framework.views import APIView

from . import transfer
from .models import Project
from .views import parse_month, settings_for

TYPES = {"pdf": "application/pdf", "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}


def file_response(data, kind):
    body = transfer.to_pdf(data) if kind == "pdf" else transfer.to_xlsx(data)
    res = HttpResponse(body, content_type=TYPES[kind])
    res["Content-Disposition"] = f'attachment; filename="{transfer.filename(data, kind, date.today())}"'
    res["Cache-Control"] = "no-store"
    return res


def kind_of(request):
    kind = request.query_params.get("as", "pdf")  # not "format": DRF reserves that name
    return kind if kind in TYPES else None


class ExpenseExportView(APIView):
    """GET /api/expenses/export/?as=pdf|xlsx&month=2026-11 (or month=all) -> the user's monthly expenses as a file."""

    def get(self, request):
        kind = kind_of(request)
        if not kind:
            return Response({"detail": "as must be pdf or xlsx"}, status=400)
        raw = request.query_params.get("month")
        month = None if raw == "all" else parse_month(raw)
        return file_response(transfer.gather_month(request.user, month), kind)


class ProjectExportView(APIView):
    """GET /api/expenses/projects/{id}/export/?as=pdf|xlsx -> one project's expenses as a file."""

    def get(self, request, pk):
        kind = kind_of(request)
        if not kind:
            return Response({"detail": "as must be pdf or xlsx"}, status=400)
        project = get_object_or_404(Project.objects.filter(user=request.user), pk=pk)
        return file_response(transfer.gather_project(project), kind)


def resolve_project(request, raw_id):
    try:
        return Project.objects.filter(user=request.user, pk=int(raw_id)).first()
    except (TypeError, ValueError):
        return None


class ImportPreviewView(APIView):
    """POST multipart {file, target_type?: monthly|project|new_project, project_id?} -> the parsed rows with problems
    and duplicates flagged for that destination. Nothing is saved."""

    def post(self, request):
        upload = request.FILES.get("file")
        if not upload:
            return Response({"detail": "Choose a file to import."}, status=400)
        if upload.size > transfer.MAX_FILE_BYTES:
            return Response({"detail": "This file is larger than 5 MB."}, status=400)
        target_type = request.data.get("target_type") or "monthly"
        project = None
        if target_type == "project":
            project = resolve_project(request, request.data.get("project_id"))
            if not project:
                return Response({"detail": "Project not found."}, status=404)
        default_currency = project.currency if project else settings_for(request.user).currency
        try:
            result = transfer.read_file(upload.name, upload.read(), default_currency)
        except transfer.TransferError as exc:
            return Response({"detail": str(exc)}, status=400)
        transfer.mark_duplicates(result["rows"], transfer.existing_keys(request.user, target_type, project))
        result["duplicates"] = sum(1 for r in result["rows"] if r["duplicate"])
        result["default_currency"] = default_currency
        return Response(result)


class ImportCommitView(APIView):
    """POST {"target": {"type": "monthly"} | {"type": "project", "id": 3} | {"type": "new_project", "name", "currency"?,
    "budget"?, "template"?}, "rows": [{date, title, category, amount, currency, note}], "skip_duplicates": true}
    -> {created, skipped_duplicates, target}. All-or-nothing."""

    def post(self, request):
        target = request.data.get("target")
        if not isinstance(target, dict) or target.get("type") not in ("monthly", "project", "new_project"):
            return Response({"detail": "Choose where to import: monthly, project or new_project."}, status=400)
        prefs = settings_for(request.user)
        resolved = {"type": target["type"]}
        default_currency = prefs.currency
        if target["type"] == "project":
            project = resolve_project(request, target.get("id"))
            if not project:
                return Response({"detail": "Project not found."}, status=404)
            resolved["project"] = project
            default_currency = project.currency
        elif target["type"] == "new_project":
            name = " ".join(str(target.get("name") or "").split())[:120]
            if not name:
                return Response({"detail": "Give the new project a name."}, status=400)
            currency = str(target.get("currency") or prefs.currency).upper()
            if currency not in transfer.CURRENCIES:
                return Response({"detail": f"Currency {currency} isn't supported."}, status=400)
            budget = None
            if target.get("budget") not in (None, ""):
                budget, _ = transfer._amount(target["budget"])
                if budget is None or budget < 0 or budget >= Decimal("1000000000000"):
                    return Response({"detail": "Budget must be a number."}, status=400)
            if Project.objects.filter(user=request.user).count() >= 200:
                return Response({"detail": "You already have 200 projects. Delete one first."}, status=400)
            resolved.update(name=name, currency=currency, budget=budget, template=target.get("template"))
            default_currency = currency
        try:
            out = transfer.commit(request.user, request.data.get("rows"), resolved,
                                  skip_duplicates=request.data.get("skip_duplicates", True) is not False, default_currency=default_currency)
        except transfer.TransferError as exc:
            return Response({"detail": str(exc)}, status=400)
        return Response(out, status=201)
