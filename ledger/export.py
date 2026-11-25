"""Export chosen plans to Excel (.xlsx, openpyxl) or PDF (reportlab).

Both formats are built from one gathered structure (`gather`) using the same figures the app shows
(`schedule.plan_figures`), so an export always matches the screen on the day it was made. Amounts stay real numbers
in Excel (formatted, summable); the PDF is a printable statement per plan.
"""
import io
import re
from datetime import date
from decimal import Decimal

from .schedule import plan_figures

STATUS = {"paid": "Paid", "partial": "Partly paid", "overdue": "Overdue", "upcoming": "Upcoming"}
SYMBOL = {"USD": "$", "GBP": "£", "EUR": "€"}


def money(value, currency):
    if value is None:
        return ""
    v = Decimal(value)
    sym = SYMBOL.get(currency)
    return f"{'-' if v < 0 else ''}{sym}{abs(v):,.2f}" if sym else f"{currency} {v:,.2f}"


def gather(plan, today):
    fig = plan_figures(plan, today)
    conv = fig["conversion"]
    payments = sorted(plan.payments.all(), key=lambda p: (p.paid_on, p.paid_time or p.created_at.time(), p.id))
    return {"plan": plan, "fig": fig, "conv": conv, "payments": payments}


def filename(plans, ext, today):
    if len(plans) == 1:
        slug = re.sub(r"[^A-Za-z0-9]+", "-", plans[0].item).strip("-")[:60] or "plan"
        return f"{slug}-{today.isoformat()}.{ext}"
    return f"installment-plans-{today.isoformat()}.{ext}"


# ---------------------------------------------------------------- Excel

def to_xlsx(plans, today=None):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    today = today or date.today()
    data = [gather(p, today) for p in plans]
    wb = Workbook()
    head_font = Font(bold=True, color="FFFFFF")
    head_fill = PatternFill("solid", fgColor="1F2937")
    title_font = Font(bold=True, size=14)
    thin = Border(bottom=Side(style="thin", color="D1D5DB"))
    status_fill = {"paid": "DCFCE7", "partial": "FEF3C7", "overdue": "FEE2E2", "upcoming": None}

    def money_fmt(cur):
        sym = SYMBOL.get(cur)
        return f'"{sym}"#,##0.00' if sym else f'"{cur} "#,##0.00'

    def header(ws, row, labels):
        for col, label in enumerate(labels, start=1):
            c = ws.cell(row=row, column=col, value=label)
            c.font, c.fill, c.alignment = head_font, head_fill, Alignment(vertical="center")

    def widths(ws, sizes):
        for i, w in enumerate(sizes, start=1):
            ws.column_dimensions[get_column_letter(i)].width = w

    # Summary: one row per plan
    ws = wb.active
    ws.title = "Summary"
    ws["A1"] = "Installment plans"
    ws["A1"].font = title_font
    ws["A2"] = f"Exported {today:%d %b %Y} · {len(plans)} plan(s) · figures as of this date"
    cols = ["Plan", "Currency", "Purchase price", "Total to repay", "Months", "Monthly", "Paid", "Remaining", "Progress",
            "Installments paid", "Months left", "Overdue", "Next due", "Next amount", "First due", "Final due",
            "Paid in", "Rate (1 plan cur =)", "Sent so far"]
    header(ws, 4, cols)
    for r, d in enumerate(data, start=5):
        p, f, conv = d["plan"], d["fig"], d["conv"]
        row = [p.item, p.currency, p.purchase_price, p.total_amount, p.months, f["monthly_amount"], f["paid_amount"],
               f["remaining_amount"], f["progress_percent"] / 100, f["installments_paid"], f["months_left"], f["overdue_count"],
               f["next_due_date"], f["next_due_amount"], p.first_payment_date, f["final_due_date"],
               conv["currency"] if conv else p.currency, conv["rate"] if conv else None, conv["sent_total"] if conv else f["paid_amount"]]
        for c, v in enumerate(row, start=1):
            cell = ws.cell(row=r, column=c, value=float(v) if isinstance(v, Decimal) else v)
            if c in (3, 4, 6, 7, 8, 14):
                cell.number_format = money_fmt(p.currency)
            elif c == 9:
                cell.number_format = "0.0%"
            elif c in (13, 15, 16):
                cell.number_format = "dd mmm yyyy"
            elif c == 18:
                cell.number_format = "#,##0.0000"
            elif c == 19:
                cell.number_format = money_fmt(conv["currency"] if conv else p.currency)
    widths(ws, [30, 9, 15, 15, 8, 13, 13, 13, 10, 10, 10, 9, 13, 13, 13, 13, 9, 12, 16])
    ws.freeze_panes = "B5"

    # One sheet per plan: facts, payments, schedule
    used = {"Summary"}
    for d in data:
        p, f, conv = d["plan"], d["fig"], d["conv"]
        name = re.sub(r"[\[\]:*?/\\]", " ", p.item).strip()[:28] or "Plan"
        base, n = name, 2
        while name in used:
            name = f"{base[:25]} ({n})"
            n += 1
        used.add(name)
        ws = wb.create_sheet(name)
        ws["A1"] = p.item
        ws["A1"].font = title_font
        facts = [
            ("Bought for", money(p.purchase_price, p.currency)),
            ("Repaying", f"{money(p.total_amount, p.currency)} over {p.months} months"),
            ("Dates", f"{p.first_payment_date:%d %b %Y} to {f['final_due_date']:%d %b %Y}"),
            ("Monthly", money(f["monthly_amount"], p.currency) + (f" ({money(conv['monthly_amount'], conv['currency'])})" if conv else "")),
            ("Paid", money(f["paid_amount"], p.currency) + (f" ({money(conv['sent_total'], conv['currency'])} sent)" if conv else "")),
            ("Remaining", money(f["remaining_amount"], p.currency) + (f" (about {money(conv['remaining_amount'], conv['currency'])})" if conv else "")),
            ("Progress", f"{f['installments_paid']} of {p.months} installments paid · {f['progress_percent']}%"),
            ("Next due", f"{money(f['next_due_amount'], p.currency)} on {f['next_due_date']:%d %b %Y}" if f["next_due_date"] else "Fully paid"),
            ("Overdue installments", f["overdue_count"]),
        ]
        if conv:
            facts.append(("Paid in", f"{conv['currency']} · 1 {p.currency} = {conv['rate']} {conv['currency']}"))
        if p.notes:
            facts.append(("Notes", p.notes))
        for i, (k, v) in enumerate(facts, start=3):
            ws.cell(row=i, column=1, value=k).font = Font(bold=True)
            ws.cell(row=i, column=2, value=v)

        row = len(facts) + 5
        ws.cell(row=row, column=1, value=f"Payments ({len(d['payments'])})").font = Font(bold=True, size=12)
        header(ws, row + 1, ["Date", "Time", f"Amount ({p.currency})", "Sent", "Sent currency", "Rate", "Reference", "Note", "Receipt"])
        r = row + 2
        for pay in d["payments"]:
            values = [pay.paid_on, pay.paid_time.strftime("%H:%M") if pay.paid_time else "", float(pay.amount),
                      float(pay.sent_amount) if pay.sent_amount is not None else None, pay.sent_currency or "",
                      float(pay.exchange_rate) if pay.exchange_rate is not None else None, pay.reference, pay.note,
                      "Yes" if (pay.receipt_path or pay.receipt_name) else ""]
            for c, v in enumerate(values, start=1):
                cell = ws.cell(row=r, column=c, value=v)
                cell.border = thin
                if c == 1:
                    cell.number_format = "dd mmm yyyy"
                elif c == 3:
                    cell.number_format = money_fmt(p.currency)
                elif c == 4:
                    cell.number_format = money_fmt(pay.sent_currency or p.currency)
                elif c == 6:
                    cell.number_format = "#,##0.0000"
            r += 1
        if d["payments"]:
            ws.cell(row=r, column=2, value="Total").font = Font(bold=True)
            total = ws.cell(row=r, column=3, value=f"=SUM(C{row + 2}:C{r - 1})")
            total.font, total.number_format = Font(bold=True), money_fmt(p.currency)
            r += 1

        r += 2
        ws.cell(row=r, column=1, value=f"Schedule ({f['installments_paid']} of {p.months} paid)").font = Font(bold=True, size=12)
        header(ws, r + 1, ["#", "Due date", f"Amount ({p.currency})", "Paid", "Status"])
        r += 2
        for s in f["schedule"]:
            values = [s["number"], s["due_date"], float(s["amount"]), float(s["paid_amount"]),
                      STATUS[s["status"]] + (" (late)" if s["is_overdue"] and s["status"] == "partial" else "")]
            for c, v in enumerate(values, start=1):
                cell = ws.cell(row=r, column=c, value=v)
                cell.border = thin
                if c == 2:
                    cell.number_format = "dd mmm yyyy"
                elif c in (3, 4):
                    cell.number_format = money_fmt(p.currency)
            fill = status_fill[s["status"]]
            if fill:
                ws.cell(row=r, column=5).fill = PatternFill("solid", fgColor=fill)
            r += 1
        widths(ws, [22, 46, 16, 16, 13, 12, 16, 36, 9])

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------- PDF

def to_pdf(plans, today=None):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    from xml.sax.saxutils import escape

    today = today or date.today()
    data = [gather(p, today) for p in plans]
    ss = getSampleStyleSheet()
    ink, muted, line = colors.HexColor("#111827"), colors.HexColor("#6B7280"), colors.HexColor("#E5E7EB")
    h1 = ParagraphStyle("h1", parent=ss["Title"], alignment=0, fontSize=20, leading=24, textColor=ink, spaceAfter=2)
    h2 = ParagraphStyle("h2", parent=ss["Heading2"], fontSize=12, leading=15, textColor=ink, spaceBefore=12, spaceAfter=6)
    body = ParagraphStyle("body", parent=ss["Normal"], fontSize=9, leading=12, textColor=ink)
    small = ParagraphStyle("small", parent=body, fontSize=8, leading=10, textColor=muted)
    cell = ParagraphStyle("cell", parent=body, fontSize=8, leading=10)
    status_bg = {"paid": "#DCFCE7", "partial": "#FEF3C7", "overdue": "#FEE2E2"}

    def P(text, style=body):
        return Paragraph(escape(str(text)), style)

    def table(rows, widths, money_cols=(), zebra=True):
        t = Table(rows, colWidths=widths, repeatRows=1)
        style = [("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 8), ("FONT", (0, 1), (-1, -1), "Helvetica", 8),
                 ("TEXTCOLOR", (0, 0), (-1, 0), colors.white), ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F2937")),
                 ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 1), (-1, -1), 0.4, line),
                 ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]
        for c in money_cols:
            style.append(("ALIGN", (c, 0), (c, -1), "RIGHT"))
        if zebra:
            for r in range(2, len(rows), 2):
                style.append(("BACKGROUND", (0, r), (-1, r), colors.HexColor("#F9FAFB")))
        t.setStyle(TableStyle(style))
        return t, style

    story = []
    for n, d in enumerate(data):
        p, f, conv = d["plan"], d["fig"], d["conv"]
        cur = p.currency
        if n:
            story.append(PageBreak())
        story.append(Paragraph(escape(p.item), h1))
        story.append(P(f"Bought for {money(p.purchase_price, cur)} · repaying {money(p.total_amount, cur)} over {p.months} months · "
                       f"{p.first_payment_date:%d %b %Y} to {f['final_due_date']:%d %b %Y}", small))
        if conv:
            story.append(P(f"Paid in {conv['currency']} · 1 {cur} = {conv['rate']} {conv['currency']}", small))
        if p.notes:
            story.append(P(p.notes, small))
        story.append(Spacer(1, 8))

        def stat(label, value, sub=""):
            return [P(label.upper(), small), Paragraph(f"<b>{escape(value)}</b>", ParagraphStyle("v", parent=body, fontSize=13, leading=16)),
                    P(sub, small)]
        stats = [stat("Monthly", money(f["monthly_amount"], cur), money(conv["monthly_amount"], conv["currency"]) if conv else ""),
                 stat("Paid", money(f["paid_amount"], cur), f"{money(conv['sent_total'], conv['currency'])} sent" if conv else ""),
                 stat("Remaining", money(f["remaining_amount"], cur), f"about {money(conv['remaining_amount'], conv['currency'])}" if conv else ""),
                 stat("Months left", str(f["months_left"]), f"{f['overdue_count']} overdue" if f["overdue_count"] else "")]
        grid = Table([[s for s in stats]], colWidths=[43 * mm] * 4)
        grid.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.6, line), ("INNERGRID", (0, 0), (-1, -1), 0.6, line),
                                  ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 6),
                                  ("LEFTPADDING", (0, 0), (-1, -1), 8)]))
        story.append(grid)
        progress = f"{f['installments_paid']} of {p.months} installments paid · {f['progress_percent']}%"
        if f["next_due_date"]:
            progress += f" · next {money(f['next_due_amount'], cur)} due {f['next_due_date']:%d %b %Y}"
        story.append(Spacer(1, 6))
        story.append(P(progress, small))

        story.append(Paragraph(f"Payment history ({len(d['payments'])})", h2))
        if d["payments"]:
            rows = [["Date", f"Amount ({cur})", "Sent", "Rate", "Reference / note"]]
            for pay in d["payments"]:
                when = f"{pay.paid_on:%d %b %Y}" + (f" {pay.paid_time:%H:%M}" if pay.paid_time else "")
                sent = money(pay.sent_amount, pay.sent_currency) if pay.sent_amount is not None and pay.sent_currency else ""
                ref = " · ".join(x for x in (pay.note, f"Ref {pay.reference}" if pay.reference else "") if x)
                rows.append([when, money(pay.amount, cur), sent, f"{pay.exchange_rate:,.4f}" if pay.exchange_rate else "", P(ref, cell)])
            rows.append(["Total", money(f["paid_amount"], cur), money(conv["sent_total"], conv["currency"]) if conv else "", "", ""])
            t, style = table(rows, [30 * mm, 28 * mm, 30 * mm, 20 * mm, 64 * mm], money_cols=(1, 2, 3))
            style += [("FONT", (0, -1), (-1, -1), "Helvetica-Bold", 8), ("LINEABOVE", (0, -1), (-1, -1), 0.8, ink)]
            t.setStyle(TableStyle(style))
            story.append(t)
        else:
            story.append(P("No payments recorded yet.", small))

        rows = [["#", "Due date", f"Amount ({cur})", "Paid", "Status"]]
        for s in f["schedule"]:
            rows.append([str(s["number"]), f"{s['due_date']:%d %b %Y}", money(s["amount"], cur), money(s["paid_amount"], cur),
                         STATUS[s["status"]] + (" (late)" if s["is_overdue"] and s["status"] == "partial" else "")])
        t, style = table(rows, [12 * mm, 32 * mm, 32 * mm, 32 * mm, 30 * mm], money_cols=(2, 3), zebra=False)
        for r, s in enumerate(f["schedule"], start=1):
            if s["status"] in status_bg:
                style.append(("BACKGROUND", (4, r), (4, r), colors.HexColor(status_bg[s["status"]])))
        t.setStyle(TableStyle(style))
        story.append(KeepTogether([Paragraph(f"Schedule ({f['installments_paid']} of {p.months} paid)", h2), t]))

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(muted)
        canvas.drawString(18 * mm, 10 * mm, f"Installment Ledger · exported {today:%d %b %Y} · figures as of this date")
        canvas.drawRightString(A4[0] - 18 * mm, 10 * mm, f"Page {doc.page}")
        canvas.restoreState()

    buf = io.BytesIO()
    title = plans[0].item if len(plans) == 1 else "Installment plans"
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=18 * mm,
                            title=title, author="Installment Ledger")
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()
