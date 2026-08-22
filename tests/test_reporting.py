import base64
import csv
import io

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src" / "symptom_relay"))
import reporting
import pdf_reports


def entries():
    return [
        {
            "entry_id": "one", "occurred_at": "2026-08-21T23:00:00.000000Z",
            "symptoms": [{"name": "PVCs", "severity": 6}, {"name": "pain", "severity": 4, "location": "hip"}],
            "sleep_hours": 6.5, "tags": ["evening"], "medications": [], "original_text": "PVCs and hip pain",
            "context": {
                "source": "garmin",
                "wellness": {"resting_heart_rate_bpm": 58, "sleep_score": 81},
                "activity": {"type": "running", "duration_minutes": 52},
                "hydration": {"fluid_ml": 500, "sodium_mg": 500, "potassium_mg": 200, "magnesium_mg": 50},
                "weather": {"temperature_f": 88, "humidity_percent": 71},
            },
        },
        {
            "entry_id": "two", "occurred_at": "2026-08-22T13:00:00.000000Z",
            "symptoms": [{"name": "palpitations", "severity": 3}], "sleep_hours": 7.2,
        },
    ]


def test_normalization_preserves_raw_aliases_and_rolls_up_group():
    rows = reporting.normalize_entries(entries())
    assert len(rows) == 3
    assert rows[0]["symptom_name"] == "PVCs"
    assert rows[0]["canonical_symptom"] == "PVCs"
    assert rows[0]["symptom_group"] == "cardiology"
    assert rows[0]["local_date"] == "2026-08-21"


def test_summary_includes_count_mean_max_and_missingness_warning():
    rows = reporting.normalize_entries(entries())
    result = reporting.summary(entries(), rows, "2026-08-21T05:00:00.000000Z", "2026-08-23T04:59:59.000000Z")
    assert result["entry_count"] == 2
    assert result["occurrence_count"] == 3
    assert result["mean_severity"] == 4.33
    assert result["max_severity"] == 6
    assert "not assumed symptom-free" in result["coverage"]["warning"]
    assert result["context_coverage"]["entries_by_section"]["activity"] == 1
    assert result["context_coverage"]["observations_by_field"]["hydration.potassium_mg"] == 1
    assert "not interpreted as zero" in result["context_coverage"]["warning"]


def test_timeline_keeps_count_mean_and_max():
    rows = reporting.normalize_entries(entries())
    result = reporting.timeline(entries(), rows, "2026-08-21T05:00:00.000000Z", "2026-08-23T04:59:59.000000Z")
    first = result["series"][0]
    assert first["occurrence_count"] == 2
    assert first["mean_severity"] == 5
    assert first["max_severity"] == 6
    assert first["context_coverage"]["entries_by_section"]["weather"] == 1


def test_small_association_is_suppressed_and_disclaimed():
    rows = reporting.normalize_entries(entries())
    result = reporting.sleep_association(entries(), rows, "palpitations")
    assert result["suppressed"] is True
    assert result["result"] is None
    assert "does not establish causation" in result["warnings"][0]


def test_occurrence_csv_is_flat_and_parseable():
    rows = reporting.normalize_entries(entries())
    parsed = list(csv.DictReader(io.StringIO(reporting.occurrences_csv(rows))))
    assert len(parsed) == 3
    assert parsed[0]["symptom_name"] == "PVCs"
    assert parsed[0]["tags"] == "evening"
    assert parsed[0]["activity_type"] == "running"
    assert parsed[0]["hydration_magnesium_mg"] == "50"
    assert parsed[0]["weather_humidity_percent"] == "71"


def test_occurrence_csv_neutralizes_spreadsheet_formulas():
    values = entries()
    values[0]["original_text"] = "=HYPERLINK(\"https://example.test\")"
    parsed = list(csv.DictReader(io.StringIO(reporting.occurrences_csv(reporting.normalize_entries(values)))))
    assert parsed[0]["original_text"].startswith("'=")


def test_cardiology_report_has_time_distribution_context_and_disclosures():
    values = entries()
    rows = reporting.normalize_entries(values)
    result = reporting.clinician_report(
        "cardiology", values, rows,
        [{"filename": "ecg-20260821.pdf", "occurred_at": values[0]["occurred_at"]}],
        "2026-08-21T05:00:00.000000Z", "2026-08-23T04:59:59.000000Z",
    )
    assert result["summary"]["count"] == 2
    assert sum(result["time_of_day"].values()) == 2
    assert result["ecg_attachments"][0]["filename"].startswith("ecg-")
    assert "not a diagnosis" in result["disclosures"][0]


def test_rheumatology_report_detects_only_transparent_possible_flares():
    values = []
    for day in range(1, 11):
        severity = 3 if day <= 7 else 7
        values.append({
            "entry_id": str(day),
            "occurred_at": f"2026-08-{day:02d}T14:00:00.000000Z",
            "symptoms": [{"name": "morning stiffness", "severity": severity, "location": "back"}],
        })
    result = reporting.clinician_report(
        "rheumatology", values, reporting.normalize_entries(values), [],
        "2026-08-01T05:00:00.000000Z", "2026-08-11T04:59:59.000000Z",
    )
    assert result["morning_stiffness"]["count"] == 10
    assert result["possible_flares"][0]["label"] == "possible flare"
    assert result["possible_flares"][0]["start"] == "2026-08-08"
    assert "3 consecutive logged days" in result["flare_rule"]


def test_clinician_pdf_is_a_real_multipage_pdf():
    values = []
    for index in range(45):
        values.append({
            "entry_id": str(index),
            "occurred_at": f"2026-08-{(index % 20) + 1:02d}T18:00:00.000000Z",
            "symptoms": [{"name": "PVCs", "severity": index % 10}],
            "original_text": "Patient wording retained for clinician review.",
        })
    report = reporting.clinician_report(
        "cardiology", values, reporting.normalize_entries(values), [],
        "2026-08-01T05:00:00.000000Z", "2026-08-31T04:59:59.000000Z",
    )
    pdf = pdf_reports.build_pdf(report)
    assert pdf.startswith(b"%PDF-")
    assert len(pdf) > 5000


def test_clinician_pdf_embeds_supported_attachment_images():
    values = entries()
    attachment = {
        "filename": "symptom.png", "content_type": "image/png", "size_bytes": 68,
        "occurred_at": values[0]["occurred_at"], "object_key": "users/private/symptom.png",
    }
    report = reporting.clinician_report(
        "rheumatology", values, reporting.normalize_entries(values), [attachment],
        "2026-08-21T05:00:00.000000Z", "2026-08-23T04:59:59.000000Z",
    )
    image = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")
    pdf = pdf_reports.build_pdf(report, lambda item: image)
    assert pdf.startswith(b"%PDF-")
    assert b"/Subtype /Image" in pdf
    assert len(pdf) > 4000
