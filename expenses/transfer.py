"""Export and import of expenses, so a set of expenses can be shared with someone and brought into another account.

Export: monthly expenses (one month or all) or one project, as a PDF (to read) or an Excel workbook (to read or edit).
Both carry the same rows: Date, Title, Category, Amount, Currency, Note. The PDF also carries those rows as an
attached JSON file, so a PDF made here imports exactly; a PDF from anywhere else has no data to read and is refused.

Import: an .xlsx / .csv with recognisable column headings, or a PDF exported here. `read_file` only parses and
validates (nothing is saved) so the UI can show a preview; `commit` saves the chosen rows into the monthly expenses,
an existing project, or a new project, skipping rows the destination already has when asked to.
"""
import csv
import io
import json
import re
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from django.db import transaction

from ledger.export import SYMBOL, money
from ledger.models import CURRENCY_CHOICES

from .models import CATEGORY_CHOICES, PROJECT_TEMPLATES, Expense, Project, ProjectExpense

FORMAT_VERSION = 1
ATTACHMENT = "installment-ledger-expenses.json"
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_ROWS = 2000
CURRENCIES = {c for c, _ in CURRENCY_CHOICES}
CATEGORY_LABELS = dict(CATEGORY_CHOICES)
CATEGORY_BY_TEXT = {**{k: k for k in CATEGORY_LABELS}, **{v.lower(): k for k, v in CATEGORY_LABELS.items()}}
COLUMNS = ["Date", "Title", "Category", "Amount", "Currency", "Note"]
# Headings people use for each column (lower-case); the first match in a header row wins.
ALIASES = {
    "date": ("date", "spent on", "spent_on", "day", "paid on", "transaction date"),
    "title": ("title", "expense", "description", "item", "name", "details", "particulars"),
    "category": ("category", "type", "head"),
    "amount": ("amount", "price", "cost", "total", "value", "debit"),
    "currency": ("currency", "cur", "ccy"),
    "note": ("note", "notes", "remarks", "comment", "comments", "memo"),
}


class TransferError(Exception):
    """A problem that is safe to show to the user."""


# ---------------------------------------------------------------- gather

def gather_month(user, month):
    """`month` = first day of a month, or None for every month."""
    qs = Expense.objects.filter(user=user)
    if month:
        end = date(month.year + (month.month == 12), month.month % 12 + 1, 1)
        qs = qs.filter(spent_on__gte=month, spent_on__lt=end)
    rows = [{"date": e.spent_on, "title": e.title, "category": CATEGORY_LABELS.get(e.category, e.category), "amount": e.amount,
             "currency": e.currency, "note": e.note} for e in qs.order_by("spent_on", "created_at")]
    name = f"Monthly expenses · {month:%B %Y}" if month else "Monthly expenses · all months"
    return {"kind": "monthly", "name": name, "currency": None, "budget": None, "period": f"{month:%Y-%m}" if month else "all", "rows": rows}


def gather_project(project):
    rows = [{"date": e.spent_on, "title": e.title, "category": e.category, "amount": e.amount, "currency": e.currency, "note": e.note}
            for e in project.expenses.order_by("spent_on", "created_at")]
    period = " to ".join(f"{d:%d %b %Y}" for d in (project.start_date, project.end_date) if d)
    return {"kind": "project", "name": project.name, "currency": project.currency, "budget": project.budget, "template": project.template,
            "note": project.note, "period": period, "rows": rows}


def totals(rows):
    by_currency, by_category = defaultdict(Decimal), defaultdict(lambda: defaultdict(Decimal))
    for r in rows:
        by_currency[r["currency"]] += r["amount"]
        by_category[r["category"]][r["currency"]] += r["amount"]
    return dict(by_currency), {k: dict(v) for k, v in by_category.items()}


def payload(data):
    """The importable form of an export (also what the PDF carries as an attachment)."""
    return {"app": "installment-ledger", "format": FORMAT_VERSION, "kind": data["kind"], "name": data["name"],
            "currency": data.get("currency"), "budget": str(data["budget"]) if data.get("budget") is not None else None,
            "template": data.get("template"),
            "rows": [{"date": r["date"].isoformat(), "title": r["title"], "category": r["category"], "amount": str(r["amount"]),
                      "currency": r["currency"], "note": r["note"]} for r in data["rows"]]}


def filename(data, ext, today=None):
    today = today or date.today()
    slug = re.sub(r"[^A-Za-z0-9]+", "-", data["name"]).strip("-")[:60] or "expenses"
    return f"{slug}-{today.isoformat()}.{ext}"


# ---------------------------------------------------------------- Excel

def to_xlsx(data, today=None):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    today = today or date.today()
    rows = data["rows"]
    wb = Workbook()
    head_font, head_fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="1F2937")

    # Sheet 1: the plain table (this is the sheet an import reads; keep the headings in row 1).
    ws = wb.active
    ws.title = "Expenses"
    for c, label in enumerate(COLUMNS, start=1):
        cell = ws.cell(row=1, column=c, value=label)
        cell.font, cell.fill = head_font, head_fill
    for r, row in enumerate(rows, start=2):
        ws.cell(row=r, column=1, value=row["date"]).number_format = "dd mmm yyyy"
        ws.cell(row=r, column=2, value=row["title"])
        ws.cell(row=r, column=3, value=row["category"])
        sym = SYMBOL.get(row["currency"])
        ws.cell(row=r, column=4, value=float(row["amount"])).number_format = f'"{sym}"#,##0.00' if sym else "#,##0.00"
        ws.cell(row=r, column=5, value=row["currency"])
        ws.cell(row=r, column=6, value=row["note"])
    for i, w in enumerate([14, 36, 22, 16, 10, 40], start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"
    if rows:
        ws.auto_filter.ref = f"A1:F{len(rows) + 1}"

    # Sheet 2: what it is and the totals (read by an import for the name / currency / budget).
    by_currency, by_category = totals(rows)
    s = wb.create_sheet("Summary")
    s["A1"] = data["name"]
    s["A1"].font = Font(bold=True, size=14)
    facts = [("Kind", "Project" if data["kind"] == "project" else "Monthly expenses"), ("Name", data["name"]),
             ("Period", data.get("period") or ""), ("Currency", data.get("currency") or ""),
             ("Budget", float(data["budget"]) if data.get("budget") is not None else ""),
             ("Expenses", len(rows)), ("Exported", today.strftime("%d %b %Y"))]
    for i, (k, v) in enumerate(facts, start=3):
        s.cell(row=i, column=1, value=k).font = Font(bold=True)
        s.cell(row=i, column=2, value=v)
    r = len(facts) + 4
    s.cell(row=r, column=1, value="Totals").font = Font(bold=True, size=12)
    for cur, amount in sorted(by_currency.items()):
        r += 1
        s.cell(row=r, column=1, value=cur)
        s.cell(row=r, column=2, value=float(amount)).number_format = "#,##0.00"
    r += 2
    s.cell(row=r, column=1, value="By category").font = Font(bold=True, size=12)
    for cat, per in sorted(by_category.items(), key=lambda kv: -sum(kv[1].values())):
        for cur, amount in sorted(per.items()):
            r += 1
            s.cell(row=r, column=1, value=cat)
            s.cell(row=r, column=2, value=float(amount)).number_format = "#,##0.00"
            s.cell(row=r, column=3, value=cur)
    s.column_dimensions["A"].width, s.column_dimensions["B"].width = 24, 40

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------- PDF

def to_pdf(data, today=None):
    from xml.sax.saxutils import escape

    from pypdf import PdfReader, PdfWriter
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    today = today or date.today()
    rows = data["rows"]
    by_currency, by_category = totals(rows)
    ss = getSampleStyleSheet()
    ink, muted, line = colors.HexColor("#111827"), colors.HexColor("#6B7280"), colors.HexColor("#E5E7EB")
    h1 = ParagraphStyle("h1", parent=ss["Title"], alignment=0, fontSize=20, leading=24, textColor=ink, spaceAfter=2)
    h2 = ParagraphStyle("h2", parent=ss["Heading2"], fontSize=12, leading=15, textColor=ink, spaceBefore=12, spaceAfter=6)
    body = ParagraphStyle("body", parent=ss["Normal"], fontSize=9, leading=12, textColor=ink)
    small = ParagraphStyle("small", parent=body, fontSize=8, leading=10, textColor=muted)
    cell = ParagraphStyle("cell", parent=body, fontSize=8, leading=10)
    big = ParagraphStyle("big", parent=body, fontSize=13, leading=16)

    def P(text, style=body):
        return Paragraph(escape(str(text)), style)

    def styled(rows_, widths, right=(), total_row=False):
        t = Table(rows_, colWidths=widths, repeatRows=1)
        st = [("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 8), ("FONT", (0, 1), (-1, -1), "Helvetica", 8),
              ("TEXTCOLOR", (0, 0), (-1, 0), colors.white), ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F2937")),
              ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 1), (-1, -1), 0.4, line),
              ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]
        st += [("ALIGN", (c, 0), (c, -1), "RIGHT") for c in right]
        st += [("BACKGROUND", (0, r), (-1, r), colors.HexColor("#F9FAFB")) for r in range(2, len(rows_), 2)]
        if total_row:
            st += [("FONT", (0, -1), (-1, -1), "Helvetica-Bold", 8), ("LINEABOVE", (0, -1), (-1, -1), 0.8, ink)]
        t.setStyle(TableStyle(st))
        return t

    story = [Paragraph(escape(data["name"]), h1)]
    sub = [x for x in (("Project" if data["kind"] == "project" else "Monthly expenses"), data.get("period") if data["kind"] == "project" else "",
                       data.get("note")) if x]
    story.append(P(" · ".join(sub), small))
    story.append(Spacer(1, 8))

    total_text = " + ".join(money(v, c) for c, v in sorted(by_currency.items())) or "No expenses"
    stats = [[P("TOTAL SPENT", small), Paragraph(f"<b>{escape(total_text)}</b>", big), P(f"{len(rows)} expense{'' if len(rows) == 1 else 's'}", small)]]
    if data.get("budget") is not None and data.get("currency"):
        spent = by_currency.get(data["currency"], Decimal("0"))
        left = data["budget"] - spent
        only = set(by_currency) <= {data["currency"]}
        stats.append([P("BUDGET", small), Paragraph(f"<b>{escape(money(data['budget'], data['currency']))}</b>", big),
                      P((f"{money(left, data['currency'])} left" if left >= 0 else f"{money(-left, data['currency'])} over") if only
                        else "other currencies not counted against the budget", small)])
    grid = Table([stats], colWidths=[87 * mm] * len(stats))
    grid.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.6, line), ("INNERGRID", (0, 0), (-1, -1), 0.6, line),
                              ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 6), ("LEFTPADDING", (0, 0), (-1, -1), 8)]))
    story.append(grid)

    if by_category:
        story.append(Paragraph("By category", h2))
        cat_rows = [["Category", "Expenses", "Amount"]]
        counts = defaultdict(int)
        for r in rows:
            counts[r["category"]] += 1
        for cat, per in sorted(by_category.items(), key=lambda kv: -sum(kv[1].values())):
            cat_rows.append([P(cat, cell), str(counts[cat]), " + ".join(money(v, c) for c, v in sorted(per.items()))])
        story.append(styled(cat_rows, [80 * mm, 24 * mm, 70 * mm], right=(1, 2)))

    story.append(Paragraph(f"Expenses ({len(rows)})", h2))
    if rows:
        table = [["Date", "Expense", "Category", "Amount"]]
        for r in rows:
            title = escape(r["title"]) + (f'<br/><font color="#6B7280">{escape(r["note"])}</font>' if r["note"] else "")
            table.append([f"{r['date']:%d %b %Y}", Paragraph(title, cell), P(r["category"], cell), money(r["amount"], r["currency"])])
        table.append(["Total", "", "", total_text])
        story.append(styled(table, [26 * mm, 78 * mm, 34 * mm, 36 * mm], right=(3,), total_row=True))
    else:
        story.append(P("No expenses in this export.", small))

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(muted)
        canvas.drawString(18 * mm, 10 * mm, f"Installment Ledger · exported {today:%d %b %Y} · this PDF can be imported into Installment Ledger")
        canvas.drawRightString(A4[0] - 18 * mm, 10 * mm, f"Page {doc.page}")
        canvas.restoreState()

    buf = io.BytesIO()
    SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=18 * mm,
                      title=data["name"], author="Installment Ledger").build(story, onFirstPage=footer, onLaterPages=footer)

    # Attach the rows as JSON so this PDF can be imported exactly (viewers list it as an attachment).
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(buf.getvalue())))
    writer.add_attachment(ATTACHMENT, json.dumps(payload(data), ensure_ascii=False).encode("utf-8"))
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


# ---------------------------------------------------------------- reading a file

def _date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y/%m/%d", "%d/%m/%y", "%d-%b-%Y", "%d-%b-%y"):
        try:
            return datetime.strptime(text[:30], fmt).date()
        except ValueError:
            continue
    return None


def _amount(value):
    """-> (Decimal | None, currency code found in the text | None). Accepts 25000, "25,000.00", "PKR 25,000", "$75"."""
    if isinstance(value, bool) or value is None:
        return None, None
    if isinstance(value, (int, float, Decimal)):
        try:
            return Decimal(str(value)).quantize(Decimal("0.01")), None
        except InvalidOperation:
            return None, None
    text = str(value).strip()
    code = next((c for c in CURRENCIES if c in text.upper()), None) or next((c for c, s in SYMBOL.items() if s in text), None)
    digits = re.sub(r"[^0-9.\-]", "", text.replace(",", ""))
    try:
        return (Decimal(digits).quantize(Decimal("0.01")) if digits not in ("", "-", ".") else None), code
    except InvalidOperation:
        return None, code


def _normalise(raw, n, default_currency):
    """One raw row {date,title,category,amount,currency,note} -> a clean preview row with `error` when unusable."""
    title = " ".join(str(raw.get("title") or "").split())[:120]
    amount, found = _amount(raw.get("amount"))
    currency = str(raw.get("currency") or "").strip().upper() or found or default_currency or ""
    when = _date(raw.get("date"))
    category = " ".join(str(raw.get("category") or "").split())[:60] or "Other"
    note = " ".join(str(raw.get("note") or "").split())[:255]
    problems = []
    if not title:
        problems.append("no title")
    if when is None:
        problems.append("date not understood" if str(raw.get("date") or "").strip() else "no date")
    if amount is None:
        problems.append("amount not a number" if str(raw.get("amount") or "").strip() else "no amount")
    elif amount <= 0:
        problems.append("amount must be more than zero")
    elif amount >= Decimal("1000000000000"):
        problems.append("amount too large")
    if currency and currency not in CURRENCIES:
        problems.append(f"currency {currency} not supported")
    return {"n": n, "date": when.isoformat() if when else None, "title": title, "category": category,
            "amount": f"{amount:.2f}" if amount is not None else None, "currency": currency if currency in CURRENCIES else "",
            "note": note, "error": "; ".join(problems) or None}


def _table(grid):
    """Find the header row in a list of rows (lists of cell values) and return raw dict rows under it."""
    for h, row in enumerate(grid[:30]):
        heads = [str(c or "").strip().lower() for c in row]
        cols = {}
        for key, names in ALIASES.items():
            idx = next((i for i, hd in enumerate(heads) if hd in names), None)
            if idx is not None:
                cols[key] = idx
        if {"title", "amount"} <= set(cols) or {"date", "amount"} <= set(cols):
            out = []
            for row2 in grid[h + 1:]:
                if not any(str(c or "").strip() for c in row2):
                    continue
                raw = {k: (row2[i] if i < len(row2) else None) for k, i in cols.items()}
                if str(raw.get("title") or raw.get("date") or "").strip().lower() in ("total", "totals", "grand total"):
                    continue
                out.append(raw)
            return out
    return None


def _read_xlsx(content):
    from openpyxl import load_workbook

    try:
        wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception:  # noqa: BLE001 - any unreadable workbook is the same message for the user
        raise TransferError("This Excel file couldn't be opened. Save it as .xlsx and try again.") from None
    meta = {}
    if "Summary" in wb.sheetnames:
        for row in wb["Summary"].iter_rows(min_row=1, max_row=15, max_col=2, values_only=True):
            if row and row[0] and len(row) > 1:
                meta[str(row[0]).strip().lower()] = row[1]
    names = (["Expenses"] if "Expenses" in wb.sheetnames else []) + [n for n in wb.sheetnames if n != "Expenses"]
    for name in names:
        grid = []
        for i, row in enumerate(wb[name].iter_rows(values_only=True)):
            if i > MAX_ROWS + 60:
                raise TransferError(f"This file has more than {MAX_ROWS:,} rows. Split it into smaller files.")
            grid.append(list(row))
        rows = _table(grid)
        if rows is not None:
            budget, _ = _amount(meta.get("budget"))
            kind = "project" if str(meta.get("kind") or "").lower().startswith("project") else "monthly" if meta.get("kind") else "unknown"
            return rows, {"kind": kind, "name": str(meta.get("name") or "").strip(), "currency": str(meta.get("currency") or "").strip().upper(),
                          "budget": f"{budget:.2f}" if budget is not None and budget > 0 else None}
    raise TransferError("No expense table found. The first row of the sheet needs headings such as Date, Title, Category, Amount, Currency, Note.")


def _read_csv(content):
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("latin-1")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    grid = list(csv.reader(io.StringIO(text), dialect))
    rows = _table(grid)
    if rows is None:
        raise TransferError("No expense table found. The first line needs headings such as Date, Title, Category, Amount, Currency, Note.")
    return rows, {"kind": "unknown", "name": "", "currency": "", "budget": None}


def _read_pdf(content):
    from pypdf import PdfReader

    try:
        attachments = PdfReader(io.BytesIO(content)).attachments
        blob = attachments.get(ATTACHMENT)
    except Exception:  # noqa: BLE001
        raise TransferError("This PDF couldn't be opened.") from None
    if not blob:
        raise TransferError("Only PDFs exported from Installment Ledger can be imported (they carry the expense data inside). "
                            "For anything else, use an Excel or CSV file.")
    try:
        data = json.loads(blob[0].decode("utf-8"))
        assert data.get("app") == "installment-ledger" and isinstance(data.get("rows"), list)
    except Exception:  # noqa: BLE001
        raise TransferError("The expense data inside this PDF is damaged. Ask for the file to be exported again.") from None
    budget, _ = _amount(data.get("budget"))
    return data["rows"], {"kind": data.get("kind") if data.get("kind") in ("project", "monthly") else "unknown",
                          "name": str(data.get("name") or "")[:120], "currency": str(data.get("currency") or "").upper(),
                          "budget": f"{budget:.2f}" if budget is not None and budget > 0 else None}


def read_file(name, content, default_currency):
    """Parse an uploaded file. Returns {source, rows, valid, invalid}; saves nothing."""
    if len(content) > MAX_FILE_BYTES:
        raise TransferError("This file is larger than 5 MB.")
    ext = (name or "").lower().rsplit(".", 1)[-1]
    if content[:5] == b"%PDF-" or ext == "pdf":
        raw, source = _read_pdf(content)
        fmt = "pdf"
    elif content[:2] == b"PK" or ext in ("xlsx", "xlsm"):
        raw, source = _read_xlsx(content)
        fmt = "xlsx"
    elif ext in ("csv", "txt", "tsv"):
        raw, source = _read_csv(content)
        fmt = "csv"
    elif ext == "xls":
        raise TransferError("Old .xls files aren't supported. Open it and save as .xlsx (or CSV), then import that.")
    else:
        raise TransferError("Choose an Excel (.xlsx), CSV or PDF file.")
    if len(raw) > MAX_ROWS:
        raise TransferError(f"This file has {len(raw):,} rows; the limit is {MAX_ROWS:,}. Split it into smaller files.")
    if not raw:
        raise TransferError("The file has headings but no expenses under them.")
    if source["currency"] not in CURRENCIES:
        source["currency"] = ""
    fallback = source["currency"] or default_currency
    rows = [_normalise(r if isinstance(r, dict) else {}, i, fallback) for i, r in enumerate(raw, start=1)]
    for r in rows:
        if not r["error"] and not r["currency"]:
            r["error"] = "no currency"
    source["format"] = fmt
    source["file"] = (name or "")[:120]
    valid = sum(1 for r in rows if not r["error"])
    return {"source": source, "rows": rows, "valid": valid, "invalid": len(rows) - valid}


# ---------------------------------------------------------------- saving

def _key(when, title, amount, currency):
    return (str(when), " ".join(str(title).lower().split()), f"{Decimal(amount):.2f}", currency)


def existing_keys(user, target_type, project=None):
    if target_type == "monthly":
        qs = Expense.objects.filter(user=user).values_list("spent_on", "title", "amount", "currency")
    elif target_type == "project" and project is not None:
        qs = ProjectExpense.objects.filter(project=project).values_list("spent_on", "title", "amount", "currency")
    else:
        return set()
    return {_key(*row) for row in qs}


def mark_duplicates(rows, keys):
    """Flag rows the destination already has, and repeats inside the file itself."""
    seen = set()
    for r in rows:
        if r.get("error"):
            r["duplicate"] = False
            continue
        k = _key(r["date"], r["title"], r["amount"], r["currency"])
        r["duplicate"] = k in keys or k in seen
        seen.add(k)
    return rows


def monthly_category(text):
    return CATEGORY_BY_TEXT.get(" ".join(str(text or "").lower().split()))


@transaction.atomic
def commit(user, rows, target, skip_duplicates=True, default_currency="PKR"):
    """Save rows into target = {"type": "monthly"} | {"type": "project", "project": Project}
    | {"type": "new_project", "name", "currency", "budget", "template"}. Every row is validated again here."""
    if not isinstance(rows, list) or not rows:
        raise TransferError("There is nothing to import.")
    if len(rows) > MAX_ROWS:
        raise TransferError(f"Import up to {MAX_ROWS:,} expenses at a time.")
    clean = [_normalise(r if isinstance(r, dict) else {}, i, default_currency) for i, r in enumerate(rows, start=1)]
    bad = [r for r in clean if r["error"] or not r["currency"]]
    if bad:
        raise TransferError(f"Row {bad[0]['n']} can't be imported: {bad[0]['error'] or 'no currency'}.")

    kind = target["type"]
    project = None
    if kind == "project":
        project = target["project"]
    elif kind == "new_project":
        template = target.get("template") if target.get("template") in PROJECT_TEMPLATES else "custom"
        project = Project.objects.create(user=user, name=target["name"], template=template, currency=target["currency"],
                                         budget=target.get("budget"), categories=list(PROJECT_TEMPLATES[template]))
    keys = existing_keys(user, "monthly" if kind == "monthly" else "project", project) if skip_duplicates else set()
    if skip_duplicates:
        mark_duplicates(clean, keys)
    todo = [r for r in clean if not (skip_duplicates and r.get("duplicate"))]

    if kind == "monthly":
        objs = []
        for r in todo:
            cat = monthly_category(r["category"])
            # A category the monthly tracker doesn't have goes under "Other", and is kept in the note so it isn't lost.
            note = r["note"] if cat or r["category"] == "Other" else (f"{r['category']}: {r['note']}" if r["note"] else r["category"])[:255]
            objs.append(Expense(user=user, title=r["title"], amount=Decimal(r["amount"]), currency=r["currency"], category=cat or "other",
                                spent_on=date.fromisoformat(r["date"]), note=note))
        Expense.objects.bulk_create(objs)
    else:
        ProjectExpense.objects.bulk_create([
            ProjectExpense(project=project, title=r["title"], amount=Decimal(r["amount"]), currency=r["currency"], category=r["category"],
                           spent_on=date.fromisoformat(r["date"]), note=r["note"]) for r in todo])
        known = {c.lower() for c in project.categories}
        extra = []
        for r in todo:
            if r["category"].lower() not in known:
                known.add(r["category"].lower())
                extra.append(r["category"])
        if extra:
            project.categories = [*project.categories, *extra][:60]
            project.save(update_fields=["categories", "updated_at"])
    return {"created": len(todo), "skipped_duplicates": len(clean) - len(todo),
            "target": {"type": "monthly"} if kind == "monthly" else {"type": "project", "id": project.id, "name": project.name}}
