import csv
import io

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src" / "symptom_relay"))
import reporting


def entries():
    return [
        {
            "entry_id": "one", "occurred_at": "2026-08-21T23:00:00.000000Z",
            "symptoms": [{"name": "PVCs", "severity": 6}, {"name": "pain", "severity": 4, "location": "hip"}],
            "sleep_hours": 6.5, "tags": ["evening"], "medications": [], "original_text": "PVCs and hip pain",
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


def test_timeline_keeps_count_mean_and_max():
    rows = reporting.normalize_entries(entries())
    result = reporting.timeline(entries(), rows, "2026-08-21T05:00:00.000000Z", "2026-08-23T04:59:59.000000Z")
    first = result["series"][0]
    assert first["occurrence_count"] == 2
    assert first["mean_severity"] == 5
    assert first["max_severity"] == 6


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


def test_occurrence_csv_neutralizes_spreadsheet_formulas():
    values = entries()
    values[0]["original_text"] = "=HYPERLINK(\"https://example.test\")"
    parsed = list(csv.DictReader(io.StringIO(reporting.occurrences_csv(reporting.normalize_entries(values)))))
    assert parsed[0]["original_text"].startswith("'=")
