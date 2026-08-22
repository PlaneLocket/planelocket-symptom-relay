import csv
import io
import statistics
import re
from collections import defaultdict
from datetime import datetime, timedelta
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
        context = entry.get("context") or {}
        wellness = context.get("wellness") or {}
        activity = context.get("activity") or {}
        hydration = context.get("hydration") or {}
        weather = context.get("weather") or {}
        treatment = context.get("treatment") or {}
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
                "context_source": context.get("source"),
                "resting_heart_rate_bpm": wellness.get("resting_heart_rate_bpm"),
                "average_stress": wellness.get("average_stress"),
                "hrv_ms": wellness.get("hrv_ms"),
                "body_battery_high": wellness.get("body_battery_high"),
                "body_battery_low": wellness.get("body_battery_low"),
                "sleep_score": wellness.get("sleep_score"),
                "steps": wellness.get("steps"),
                "activity_type": activity.get("type"),
                "activity_duration_minutes": activity.get("duration_minutes"),
                "activity_intensity_minutes": activity.get("intensity_minutes"),
                "activity_distance_km": activity.get("distance_km"),
                "activity_calories": activity.get("calories"),
                "activity_average_heart_rate_bpm": activity.get("average_heart_rate_bpm"),
                "activity_max_heart_rate_bpm": activity.get("max_heart_rate_bpm"),
                "hydration_fluid_ml": hydration.get("fluid_ml"),
                "hydration_sodium_mg": hydration.get("sodium_mg"),
                "hydration_potassium_mg": hydration.get("potassium_mg"),
                "hydration_magnesium_mg": hydration.get("magnesium_mg"),
                "hydration_carbohydrate_g": hydration.get("carbohydrate_g"),
                "weather_temperature_f": weather.get("temperature_f"),
                "weather_feels_like_f": weather.get("feels_like_f"),
                "weather_humidity_percent": weather.get("humidity_percent"),
                "weather_dew_point_f": weather.get("dew_point_f"),
                "treatment_name": treatment.get("name"),
                "treatment_dose": treatment.get("dose"),
                "treatment_event": treatment.get("event"),
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
        "context_coverage": context_coverage(entries),
        "coverage": coverage(entries, since, until),
    }


def context_coverage(entries):
    sections = ("wellness", "activity", "hydration", "weather", "treatment")
    observed = {section: 0 for section in sections}
    fields = defaultdict(int)
    for entry in entries:
        context = entry.get("context") or {}
        for section in sections:
            values = context.get(section) or {}
            if values:
                observed[section] += 1
                for field, value in values.items():
                    if value is not None:
                        fields[f"{section}.{field}"] += 1
    return {
        "entry_count": len(entries),
        "entries_by_section": observed,
        "observations_by_field": dict(sorted(fields.items())),
        "warning": "Context is reported only when logged; missing fields are not interpreted as zero or absent.",
    }


def timeline(entries, rows, since, until):
    days = defaultdict(lambda: {"entry_ids": set(), "rows": [], "sleep": [], "entries": []})
    zone = ZoneInfo(REPORTING_TIMEZONE)
    for entry in entries:
        date = datetime.fromisoformat(entry["occurred_at"].replace("Z", "+00:00")).astimezone(zone).date().isoformat()
        days[date]["entries"].append(entry)
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
            "context_coverage": context_coverage(day["entries"]),
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
    fields = [
        "occurred_at", "local_date", "local_hour", "symptom_name", "canonical_symptom", "symptom_group",
        "severity", "location", "symptom_notes", "sleep_hours", "context_source",
        "resting_heart_rate_bpm", "average_stress", "hrv_ms", "body_battery_high", "body_battery_low",
        "sleep_score", "steps", "activity_type", "activity_duration_minutes", "activity_intensity_minutes",
        "activity_distance_km", "activity_calories", "activity_average_heart_rate_bpm",
        "activity_max_heart_rate_bpm", "hydration_fluid_ml", "hydration_sodium_mg",
        "hydration_potassium_mg", "hydration_magnesium_mg", "hydration_carbohydrate_g",
        "weather_temperature_f", "weather_feels_like_f", "weather_humidity_percent", "weather_dew_point_f",
        "treatment_name", "treatment_dose", "treatment_event", "medications", "tags", "entry_notes",
        "original_text", "entry_id",
    ]
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


def _severity_stats(rows):
    scores = [float(row["severity"]) for row in rows if row.get("severity") is not None]
    return {
        "count": len(rows),
        "severity_count": len(scores),
        "mean_severity": round(statistics.fmean(scores), 2) if scores else None,
        "max_severity": max(scores) if scores else None,
    }


def _time_of_day(rows):
    buckets = {"overnight": 0, "morning": 0, "afternoon": 0, "evening": 0}
    for row in rows:
        hour = int(row["local_hour"])
        bucket = "overnight" if hour < 6 else "morning" if hour < 12 else "afternoon" if hour < 18 else "evening"
        buckets[bucket] += 1
    return buckets


def _entry_context(entries):
    exercise_terms = re.compile(r"\b(run|running|exercise|workout|yardwork|cleaning|heavy effort|activity)\b", re.I)
    hydration_terms = re.compile(r"\b(hydrat|electrolyte|liquid i\.v\.|water)\b", re.I)
    weather_terms = re.compile(r"\b(heat|hot|humid|humidity|temperature|weather)\b", re.I)
    result = {"sleep_observations": 0, "exercise_or_effort_entries": 0, "hydration_entries": 0, "weather_entries": 0}
    medication_markers = []
    for entry in entries:
        context = entry.get("context") or {}
        text = " ".join([str(entry.get("original_text") or ""), str(entry.get("notes") or ""), " ".join(entry.get("tags") or [])])
        result["sleep_observations"] += int(entry.get("sleep_hours") is not None)
        result["exercise_or_effort_entries"] += int(bool(context.get("activity")) or bool(exercise_terms.search(text)))
        result["hydration_entries"] += int(bool(context.get("hydration")) or bool(hydration_terms.search(text)))
        result["weather_entries"] += int(bool(context.get("weather")) or bool(weather_terms.search(text)))
        if entry.get("medications"):
            medication_markers.append({"occurred_at": entry["occurred_at"], "medications": entry["medications"]})
        treatment = context.get("treatment") or {}
        if treatment:
            label = " ".join(str(treatment.get(key) or "").strip() for key in ("name", "dose", "event")).strip()
            medication_markers.append({"occurred_at": entry["occurred_at"], "medications": [label]})
    result["normalized_context_coverage"] = context_coverage(entries)
    result["medication_markers"] = medication_markers
    return result


def _weekly_burden(rows):
    weeks = defaultdict(list)
    for row in rows:
        day = datetime.fromisoformat(row["local_date"]).date()
        monday = day - timedelta(days=day.weekday())
        weeks[monday.isoformat()].append(row)
    return [{"week_start": week, **_severity_stats(values)} for week, values in sorted(weeks.items())]


def _possible_flares(rows):
    by_day = defaultdict(list)
    for row in rows:
        if row.get("severity") is not None:
            by_day[row["local_date"]].append(float(row["severity"]))
    daily = [(datetime.fromisoformat(day).date(), statistics.fmean(scores)) for day, scores in sorted(by_day.items())]
    flagged = []
    for index, (day, mean) in enumerate(daily):
        trailing = [value for prior_day, value in daily[:index] if 0 < (day - prior_day).days <= 14]
        if len(trailing) >= 7 and mean > statistics.fmean(trailing):
            flagged.append((day, mean, statistics.fmean(trailing)))
    periods = []
    run = []
    for item in flagged:
        if run and (item[0] - run[-1][0]).days != 1:
            if len(run) >= 3:
                periods.append(run)
            run = []
        run.append(item)
    if len(run) >= 3:
        periods.append(run)
    return [{
        "label": "possible flare",
        "start": period[0][0].isoformat(),
        "end": period[-1][0].isoformat(),
        "logged_days": len(period),
        "mean_severity": round(statistics.fmean(item[1] for item in period), 2),
        "trailing_baseline": round(statistics.fmean(item[2] for item in period), 2),
    } for period in periods]


def clinician_report(specialty, entries, rows, attachments, since, until):
    specialty = str(specialty or "").casefold()
    if specialty not in {"cardiology", "rheumatology"}:
        raise ValueError("specialty must be cardiology or rheumatology")
    selected = filter_rows(rows, group=specialty)
    report = {
        "report_type": specialty,
        "title": f"{specialty.title()} Symptom Report",
        "period": {"since": since, "until": until, "timezone": REPORTING_TIMEZONE},
        "generated_at": datetime.now(ZoneInfo("UTC")).isoformat(),
        "coverage": coverage(entries, since, until),
        "summary": _severity_stats(selected),
        "context": _entry_context(entries),
        "events": selected,
        "attachments": attachments,
        "disclosures": [
            "This is a patient-maintained symptom log and is not a diagnosis.",
            "Days without entries are missing or unlogged and are not assumed symptom-free.",
            "Associations shown here do not establish causation.",
        ],
    }
    if specialty == "cardiology":
        report["time_of_day"] = _time_of_day(selected)
        report["ecg_attachments"] = [item for item in attachments if "ecg" in str(item.get("filename", "")).casefold() or "kardia" in str(item.get("filename", "")).casefold()]
    else:
        locations = defaultdict(list)
        stiffness = []
        for row in selected:
            if row.get("location"):
                locations[row["location"]].append(row)
            if "stiff" in row["canonical_symptom"].casefold():
                stiffness.append(row)
        report["morning_stiffness"] = _severity_stats(stiffness)
        report["pain_by_location"] = [{"location": location, **_severity_stats(values)} for location, values in sorted(locations.items(), key=lambda item: (-len(item[1]), item[0].casefold()))]
        report["weekly_burden"] = _weekly_burden(selected)
        report["possible_flares"] = _possible_flares(selected)
        report["flare_rule"] = "At least 3 consecutive logged days with daily mean severity above the trailing 14-day baseline; at least 7 prior severity-logged days are required. Results are labeled possible flare."
    return report
