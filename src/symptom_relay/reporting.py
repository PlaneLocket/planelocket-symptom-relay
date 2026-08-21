import csv
import io
import statistics
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo


REPORTING_TIMEZONE = "America/Chicago"

ALIASES = {
    "pvc": ("PVC", "cardiology"),
    "pvcs": ("PVCs", "cardiology"),
    "premature ventricular contraction": ("PVC", "cardiology"),
    "premature ventricular contractions": ("PVCs", "cardiology"),
    "palpitation": ("palpitations", "cardiology"),
    "palpitations": ("palpitations", "cardiology"),
    "morning stiffness": ("morning stiffness", "rheumatology"),
    "stiffness": ("stiffness", "rheumatology"),
    "pain": ("pain", "rheumatology"),
    "cramp": ("cramps", "general"),
    "cramps": ("cramps", "general"),
}


def canonical_symptom(name):
    raw = " ".join(str(name or "").strip().split())
    canonical, group = ALIASES.get(raw.casefold(), (raw, "general"))
    return raw, canonical, group


def normalize_entries(entries, timezone_name=REPORTING_TIMEZONE):
    zone = ZoneInfo(timezone_name)
    rows = []
    for entry in entries:
        occurred = datetime.fromisoformat(entry["occurred_at"].replace("Z", "+00:00"))
        local = occurred.astimezone(zone)
        for index, symptom in enumerate(entry.get("symptoms") or []):
            raw, canonical, group = canonical_symptom(symptom.get("name"))
            rows.append({
                "occurrence_id": f'{entry["entry_id"]}:{index}',
                "entry_id": entry["entry_id"],
                "occurred_at": entry["occurred_at"],
                "local_date": local.date().isoformat(),
                "local_hour": local.hour,
                "symptom_index": index,
                "symptom_name": raw,
                "canonical_symptom": canonical,
                "symptom_group": group,
                "severity": symptom.get("severity"),
                "location": symptom.get("location"),
                "symptom_notes": symptom.get("notes"),
                "sleep_hours": entry.get("sleep_hours"),
                "medications": entry.get("medications") or [],
                "tags": entry.get("tags") or [],
                "entry_notes": entry.get("notes"),
                "original_text": entry.get("original_text"),
                "created_at": entry.get("created_at"),
                "updated_at": entry.get("updated_at"),
            })
    return rows


def filter_rows(rows, symptom=None, group=None):
    if symptom:
        wanted = symptom.casefold()
        rows = [row for row in rows if row["symptom_name"].casefold() == wanted or row["canonical_symptom"].casefold() == wanted]
    if group:
        rows = [row for row in rows if row["symptom_group"] == group.casefold()]
    return rows


def coverage(entries, since, until, timezone_name=REPORTING_TIMEZONE):
    zone = ZoneInfo(timezone_name)
    start = datetime.fromisoformat(since.replace("Z", "+00:00")).astimezone(zone).date()
    end = datetime.fromisoformat(until.replace("Z", "+00:00")).astimezone(zone).date()
    period_days = max(1, (end - start).days + 1)
    logged_days = len({datetime.fromisoformat(e["occurred_at"].replace("Z", "+00:00")).astimezone(zone).date() for e in entries})
    return {
        "period_days": period_days,
        "logged_days": logged_days,
        "missing_or_unlogged_days": max(0, period_days - logged_days),
        "completeness_ratio": round(logged_days / period_days, 3),
        "warning": "Days without entries are missing or unlogged; they are not assumed symptom-free.",
    }


def summary(entries, rows, since, until):
    severities = [float(row["severity"]) for row in rows if row["severity"] is not None]
    by_symptom = defaultdict(list)
    for row in rows:
        by_symptom[row["canonical_symptom"]].append(row)
    symptoms = []
    for name, values in sorted(by_symptom.items(), key=lambda item: (-len(item[1]), item[0].casefold())):
        scores = [float(value["severity"]) for value in values if value["severity"] is not None]
        symptoms.append({"symptom": name, "count": len(values), "severity_count": len(scores), "mean_severity": round(statistics.fmean(scores), 2) if scores else None, "max_severity": max(scores) if scores else None})
    return {
        "period": {"since": since, "until": until, "timezone": REPORTING_TIMEZONE},
        "entry_count": len(entries),
        "occurrence_count": len(rows),
        "severity_count": len(severities),
        "mean_severity": round(statistics.fmean(severities), 2) if severities else None,
        "max_severity": max(severities) if severities else None,
        "symptoms": symptoms,
        "coverage": coverage(entries, since, until),
    }


def timeline(entries, rows, since, until):
    days = defaultdict(lambda: {"entry_ids": set(), "rows": [], "sleep": []})
    for row in rows:
        day = days[row["local_date"]]
        day["entry_ids"].add(row["entry_id"])
        day["rows"].append(row)
        if row["sleep_hours"] is not None:
            day["sleep"].append(float(row["sleep_hours"]))
    series = []
    for date, day in sorted(days.items()):
        scores = [float(row["severity"]) for row in day["rows"] if row["severity"] is not None]
        series.append({
            "date": date,
            "entry_count": len(day["entry_ids"]),
            "occurrence_count": len(day["rows"]),
            "severity_count": len(scores),
            "mean_severity": round(statistics.fmean(scores), 2) if scores else None,
            "max_severity": max(scores) if scores else None,
            "sleep_hours": round(statistics.fmean(day["sleep"]), 2) if day["sleep"] else None,
            "data_status": "logged",
        })
    return {"period": {"since": since, "until": until, "timezone": REPORTING_TIMEZONE}, "series": series, "coverage": coverage(entries, since, until)}


def sleep_association(entries, rows, outcome):
    selected = filter_rows(rows, symptom=outcome)
    by_date = defaultdict(list)
    for row in selected:
        by_date[row["local_date"]].append(row)
    sleep_by_date = {}
    zone = ZoneInfo(REPORTING_TIMEZONE)
    for entry in entries:
        if entry.get("sleep_hours") is not None:
            date = datetime.fromisoformat(entry["occurred_at"].replace("Z", "+00:00")).astimezone(zone).date().isoformat()
            sleep_by_date[date] = float(entry["sleep_hours"])
    pairs = [{"date": date, "sleep_hours": hours, "occurrence_count": len(by_date.get(date, [])), "max_severity": max((float(r["severity"]) for r in by_date.get(date, []) if r["severity"] is not None), default=None)} for date, hours in sorted(sleep_by_date.items())]
    suppressed = len(pairs) < 7
    return {
        "outcome": outcome,
        "factor": "sleep_hours",
        "method": "daily sleep paired with observed symptom-log occurrences",
        "sample_size": len(pairs),
        "result": None if suppressed else {"pairs": pairs},
        "suppressed": suppressed,
        "warnings": ["This describes an association and does not establish causation.", "Days without entries are not assumed symptom-free."] + (["At least 7 complete daily observations are required."] if suppressed else []),
    }


def occurrences_csv(rows):
    fields = ["occurred_at", "local_date", "local_hour", "symptom_name", "canonical_symptom", "symptom_group", "severity", "location", "symptom_notes", "sleep_hours", "medications", "tags", "entry_notes", "original_text", "entry_id"]
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        value = dict(row)
        value["medications"] = " | ".join(value["medications"])
        value["tags"] = " | ".join(value["tags"])
        for field, item in value.items():
            if isinstance(item, str) and item.startswith(("=", "+", "-", "@", "\t", "\r")):
                value[field] = "'" + item
        writer.writerow(value)
    return output.getvalue()
