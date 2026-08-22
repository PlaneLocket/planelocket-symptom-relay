from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import BaseDocTemplate, Frame, PageTemplate, Paragraph, Spacer, Table, TableStyle


TEAL = colors.HexColor("#164f4d")
PALE = colors.HexColor("#e8f3f0")
INK = colors.HexColor("#173237")
MUTED = colors.HexColor("#657f82")
LINE = colors.HexColor("#d7e2df")


def _text(value, fallback="Not recorded"):
    if value is None or value == "":
        return fallback
    return escape(str(value))


def _number(value):
    return "-" if value is None else str(value)


def _footer(canvas, doc):
    canvas.saveState()
    canvas.setStrokeColor(LINE)
    canvas.line(0.65 * inch, 0.52 * inch, 7.85 * inch, 0.52 * inch)
    canvas.setFillColor(MUTED)
    canvas.setFont("Helvetica", 7)
    canvas.drawString(0.65 * inch, 0.34 * inch, "PlaneLocket Health - patient-maintained symptom log")
    canvas.drawRightString(7.85 * inch, 0.34 * inch, f"Page {doc.page}")
    canvas.restoreState()


def _styles():
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("ReportTitle", parent=base["Title"], fontName="Helvetica-Bold", fontSize=22, leading=26, textColor=INK, spaceAfter=6),
        "subtitle": ParagraphStyle("Subtitle", parent=base["Normal"], fontSize=9, leading=13, textColor=MUTED, spaceAfter=14),
        "h2": ParagraphStyle("H2", parent=base["Heading2"], fontName="Helvetica-Bold", fontSize=13, leading=16, textColor=TEAL, spaceBefore=12, spaceAfter=7),
        "body": ParagraphStyle("Body", parent=base["BodyText"], fontSize=8.5, leading=12, textColor=INK),
        "small": ParagraphStyle("Small", parent=base["BodyText"], fontSize=7.5, leading=10, textColor=MUTED),
        "metric": ParagraphStyle("Metric", parent=base["Normal"], fontName="Helvetica-Bold", fontSize=16, leading=18, textColor=TEAL, alignment=TA_CENTER),
        "metric_label": ParagraphStyle("MetricLabel", parent=base["Normal"], fontSize=7, leading=9, textColor=MUTED, alignment=TA_CENTER),
        "right": ParagraphStyle("Right", parent=base["Normal"], fontSize=7.5, leading=10, textColor=INK, alignment=TA_RIGHT),
    }


def _metrics(report, styles):
    summary = report["summary"]
    coverage = report["coverage"]
    cells = [
        (summary["count"], "Observed occurrences"),
        (_number(summary["mean_severity"]), "Mean severity"),
        (_number(summary["max_severity"]), "Maximum severity"),
        (f'{coverage["logged_days"]}/{coverage["period_days"]}', "Days with any entry"),
    ]
    content = [[Paragraph(_text(value), styles["metric"]), Paragraph(label, styles["metric_label"])] for value, label in cells]
    table = Table([[item for item in content]], colWidths=[1.77 * inch] * 4, rowHeights=[0.68 * inch])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), PALE), ("BOX", (0, 0), (-1, -1), 0.5, LINE),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 7),
    ]))
    return table


def _event_table(events, styles):
    data = [["Date / time", "Symptom", "Severity", "Location", "Patient wording"]]
    for event in events:
        wording = event.get("original_text") or event.get("symptom_notes") or event.get("entry_notes") or "-"
        data.append([
            Paragraph(_text(event["occurred_at"].replace("T", " ").replace(".000000Z", " UTC")), styles["small"]),
            Paragraph(_text(event["symptom_name"]), styles["body"]),
            Paragraph(_text(event.get("severity"), "-"), styles["right"]),
            Paragraph(_text(event.get("location"), "-"), styles["small"]),
            Paragraph(_text(wording, "-"), styles["small"]),
        ])
    table = Table(data, colWidths=[1.22 * inch, 1.02 * inch, 0.48 * inch, 0.8 * inch, 3.55 * inch], repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), TEAL), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, 0), 7),
        ("GRID", (0, 0), (-1, -1), 0.35, LINE), ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f7faf9")]),
        ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    return table


def build_pdf(report):
    output = BytesIO()
    doc = BaseDocTemplate(output, pagesize=letter, leftMargin=0.65 * inch, rightMargin=0.65 * inch, topMargin=0.62 * inch, bottomMargin=0.7 * inch, title=report["title"], author="PlaneLocket Health")
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="content")
    doc.addPageTemplates(PageTemplate(id="report", frames=[frame], onPage=_footer))
    s = _styles()
    period = report["period"]
    story = [
        Paragraph(report["title"], s["title"]),
        Paragraph(f'Reporting period: {_text(period["since"])} through {_text(period["until"])} | Timezone: {_text(period["timezone"])}', s["subtitle"]),
        _metrics(report, s),
        Spacer(1, 10),
        Paragraph("Data completeness", s["h2"]),
        Paragraph(_text(report["coverage"]["warning"]), s["body"]),
    ]
    if report["report_type"] == "cardiology":
        buckets = report["time_of_day"]
        story.extend([
            Paragraph("Cardiology overview", s["h2"]),
            Paragraph("PVC and palpitation observations by time of day: " + ", ".join(f"{name} {count}" for name, count in buckets.items()) + ".", s["body"]),
            Paragraph("Available context", s["h2"]),
            Paragraph(f'Sleep observations: {report["context"]["sleep_observations"]}; exercise or heavy-effort entries: {report["context"]["exercise_or_effort_entries"]}; hydration entries: {report["context"]["hydration_entries"]}; weather entries: {report["context"]["weather_entries"]}.', s["body"]),
        ])
    else:
        stiffness = report["morning_stiffness"]
        story.extend([
            Paragraph("Rheumatology overview", s["h2"]),
            Paragraph(f'Morning stiffness observations: {stiffness["count"]}; mean severity: {_number(stiffness["mean_severity"])}; maximum severity: {_number(stiffness["max_severity"])}.', s["body"]),
            Paragraph("Possible flare rule", s["h2"]),
            Paragraph(_text(report["flare_rule"]), s["body"]),
        ])
        if report["possible_flares"]:
            for flare in report["possible_flares"]:
                story.append(Paragraph(f'Possible flare: {flare["start"]} through {flare["end"]}; mean severity {flare["mean_severity"]}, trailing baseline {flare["trailing_baseline"]}.', s["body"]))
        else:
            story.append(Paragraph("No possible flare period met the rule in the selected period.", s["body"]))
        if report["pain_by_location"]:
            story.extend([Paragraph("Pain and stiffness by location", s["h2"]), Paragraph("; ".join(f'{item["location"]}: {item["count"]} observations, mean {_number(item["mean_severity"])}, max {_number(item["max_severity"])}' for item in report["pain_by_location"]) + ".", s["body"])])
    story.extend([Paragraph("Symptom events", s["h2"]), _event_table(report["events"], s)])
    story.append(Spacer(1, 10))
    story.append(Paragraph("Attachments and treatment context", s["title"] if False else s["h2"]))
    attachments = report.get("ecg_attachments") if report["report_type"] == "cardiology" else report.get("attachments")
    if attachments:
        for item in attachments:
            story.append(Paragraph(f'{_text(item.get("occurred_at") or item.get("created_at"))} - {_text(item.get("filename"))}', s["body"]))
    else:
        story.append(Paragraph("No relevant attachments were indexed for this reporting period.", s["body"]))
    markers = report["context"]["medication_markers"]
    story.append(Paragraph("Medication and treatment markers", s["h2"]))
    if markers:
        for marker in markers:
            story.append(Paragraph(f'{_text(marker["occurred_at"])} - {_text(", ".join(marker["medications"]))}', s["body"]))
    else:
        story.append(Paragraph("No medication or treatment markers were recorded in the selected entries.", s["body"]))
    story.append(Paragraph("Important disclosures", s["h2"]))
    for disclosure in report["disclosures"]:
        story.append(Paragraph("- " + _text(disclosure), s["small"]))
    doc.build(story)
    return output.getvalue()
