import json
import os
import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "src" / "symptom_relay"))
import app


class FakeTable:
    def __init__(self):
        self.items = {}

    def put_item(self, Item, **kwargs):
        self.items[(Item["PK"], Item["SK"])] = dict(Item)
        return {}

    def get_item(self, Key, **kwargs):
        item = self.items.get((Key["PK"], Key["SK"]))
        return {"Item": dict(item)} if item else {}

    def delete_item(self, Key, **kwargs):
        item = self.items.pop((Key["PK"], Key["SK"]), None)
        return {"Attributes": item} if item else {}

    def query(self, **kwargs):
        items = [dict(value) for value in self.items.values() if value["SK"].startswith("ENTRY#")]
        items.sort(key=lambda value: value["SK"], reverse=not kwargs.get("ScanIndexForward", True))
        start = kwargs.get("ExclusiveStartKey")
        if start:
            items = items[items.index(next(value for value in items if value["PK"] == start["PK"] and value["SK"] == start["SK"])) + 1:]
        limit = kwargs.get("Limit", len(items))
        page = items[:limit]
        result = {"Items": page}
        if len(items) > limit:
            result["LastEvaluatedKey"] = {"PK": page[-1]["PK"], "SK": page[-1]["SK"]}
        self.last_query = kwargs
        return result


class FakeBody:
    def __init__(self, data):
        self.data = data

    def read(self):
        return self.data


class FakeS3:
    def __init__(self):
        self.objects = {}

    def generate_presigned_url(self, operation, Params, ExpiresIn):
        return f"https://bucket.example/{operation}/{Params['Key']}?expires={ExpiresIn}"

    def head_object(self, Bucket, Key):
        value = self.objects[Key]
        return {"ContentLength": len(value["body"]), "ContentType": value["content_type"]}

    def get_object(self, Bucket, Key, Range):
        return {"Body": FakeBody(self.objects[Key]["body"][:32])}

    def copy_object(self, Bucket, Key, CopySource, **kwargs):
        self.objects[Key] = dict(self.objects[CopySource["Key"]])
        return {}

    def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)
        return {}


@pytest.fixture(autouse=True)
def environment(monkeypatch):
    monkeypatch.setenv("ACTION_READ_SCOPE", "planelocket-symptoms/read")
    monkeypatch.setenv("ACTION_WRITE_SCOPE", "planelocket-symptoms/write")
    monkeypatch.setenv("COGNITO_ISSUER", "https://issuer.example/pool")
    monkeypatch.setenv("COGNITO_CLIENT_ID", "client")
    monkeypatch.setenv("COGNITO_DASHBOARD_CLIENT_ID", "dashboard-client")
    monkeypatch.setenv("COGNITO_AUTHORIZATION_ENDPOINT", "https://auth.example/authorize")
    monkeypatch.setenv("COGNITO_TOKEN_ENDPOINT", "https://auth.example/token")
    monkeypatch.setenv("ATTACHMENT_BUCKET", "attachments")
    monkeypatch.setenv("ATTACHMENT_BUCKET_ORIGIN", "https://attachments.s3.us-east-2.amazonaws.com")
    monkeypatch.setenv("CURSOR_SECRET", "unit-test-only-secret")
    monkeypatch.setenv("DASHBOARD_ORIGIN", "https://health.loopers.space")
    app._cursor_key = None
    fake = FakeTable()
    fake_s3 = FakeS3()
    monkeypatch.setattr(app, "table", lambda: fake)
    monkeypatch.setattr(app, "s3", lambda: fake_s3)
    fake.s3 = fake_s3
    return fake


def api_event(method, path, body=None):
    return {
        "rawPath": path,
        "headers": {"host": "api.example", "x-forwarded-proto": "https"},
        "requestContext": {"domainName": "api.example", "http": {"method": method, "path": path}},
        "body": json.dumps(body) if body is not None else None,
    }


def test_both_oauth_clients_are_accepted():
    assert app.accepted_client_ids() == {"client", "dashboard-client"}


def test_create_partitions_entries_by_cognito_subject(environment):
    first = app.create_entry({"sub": "person-a"}, {"symptoms": [{"name": "fatigue", "severity": 7}]})
    second = app.create_entry({"sub": "person-b"}, {"symptoms": [{"name": "headache", "severity": 4}]})
    assert first["entry_id"] != second["entry_id"]
    assert {key[0] for key in environment.items} == {"USER#person-a", "USER#person-b"}


def test_invalid_severity_is_rejected():
    with pytest.raises(app.RelayError, match="0 through 10"):
        app.create_entry({"sub": "person-a"}, {"symptoms": [{"name": "pain", "severity": 11}]})


def test_decimal_sleep_hours_are_stored_as_dynamodb_decimal(environment):
    created = app.create_entry(
        {"sub": "person-a"},
        {"symptoms": [{"name": "Garmin wellness"}], "sleep_hours": 7.37},
    )
    stored = next(iter(environment.items.values()))
    assert created["sleep_hours"] == 7.37
    assert stored["sleep_hours"] == Decimal("7.37")
    assert not isinstance(stored["sleep_hours"], float)


def test_normalized_phase5_context_is_stored_as_dynamodb_decimals(environment):
    created = app.create_entry(
        {"sub": "person-a"},
        {
            "symptoms": [{"name": "Garmin wellness"}],
            "context": {
                "source": "garmin",
                "wellness": {"resting_heart_rate_bpm": 58, "hrv_ms": 42.5, "sleep_score": 81},
                "activity": {"type": "running", "duration_minutes": 52.4, "distance_km": 7.1},
                "weather": {"temperature_f": 88.2, "humidity_percent": 71},
            },
        },
    )
    stored = next(iter(environment.items.values()))
    assert created["context"]["activity"]["duration_minutes"] == 52.4
    assert stored["context"]["activity"]["duration_minutes"] == Decimal("52.4")
    assert stored["context"]["weather"]["temperature_f"] == Decimal("88.2")


def test_phase5_context_rejects_unknown_and_out_of_range_fields():
    with pytest.raises(app.RelayError, match="Unsupported context.activity fields"):
        app.validate_entry({"symptoms": [{"name": "PVCs"}], "context": {"activity": {"mystery": 1}}})
    with pytest.raises(app.RelayError, match="humidity_percent must be from 0 through 100"):
        app.validate_entry({"symptoms": [{"name": "PVCs"}], "context": {"weather": {"humidity_percent": 101}}})


def test_phase5_treatment_event_is_restricted():
    with pytest.raises(app.RelayError, match="must be one of"):
        app.validate_entry({
            "symptoms": [{"name": "morning stiffness"}],
            "context": {"treatment": {"name": "adalimumab", "event": "maybe"}},
        })


def test_decimal_sleep_hours_can_be_updated(environment):
    created = app.create_entry({"sub": "person-a"}, {"symptoms": [{"name": "Garmin wellness"}]})
    updated = app.update_entry({"sub": "person-a"}, created["entry_id"], {"sleep_hours": 6.45})
    stored = environment.items[("USER#person-a", "ENTRY#" + created["entry_id"])]
    assert updated["sleep_hours"] == 6.45
    assert stored["sleep_hours"] == Decimal("6.45")
    assert not isinstance(stored["sleep_hours"], float)


def test_entry_with_stored_decimal_sleep_hours_can_receive_unrelated_update(environment):
    created = app.create_entry(
        {"sub": "person-a"},
        {"symptoms": [{"name": "Garmin wellness"}], "sleep_hours": 7.37},
    )
    updated = app.update_entry({"sub": "person-a"}, created["entry_id"], {"notes": "Corrected intensity minutes."})
    assert updated["sleep_hours"] == Decimal("7.37")
    assert updated["notes"] == "Corrected intensity minutes."


def test_update_cannot_cross_user_partition(environment):
    created = app.create_entry({"sub": "person-a"}, {"symptoms": [{"name": "pain", "severity": 3}]})
    with pytest.raises(app.RelayError) as exc:
        app.update_entry({"sub": "person-b"}, created["entry_id"], {"notes": "not allowed"})
    assert exc.value.status == 404


def test_delete_returns_deleted_entry(environment):
    created = app.create_entry({"sub": "person-a"}, {"symptoms": [{"name": "pain"}]})
    result = app.delete_entry({"sub": "person-a"}, created["entry_id"])
    assert result["deleted"] is True
    assert result["entry"]["entry_id"] == created["entry_id"]


def test_mcp_tool_catalog_is_available_without_oauth():
    event = api_event("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    result = app.lambda_handler(event, None)
    payload = json.loads(result["body"])
    assert result["statusCode"] == 200
    assert {tool["name"] for tool in payload["result"]["tools"]} == {
        "log_symptoms", "list_symptom_entries", "update_symptom_entry", "delete_symptom_entry",
        "show_attachment_uploader", "start_attachment_upload", "complete_attachment_upload",
        "list_symptom_attachments", "get_symptom_attachment", "delete_symptom_attachment",
    }


def test_mcp_tools_declare_custom_oauth_scopes():
    tools = {tool["name"]: tool for tool in app.mcp_tools()}
    assert tools["list_symptom_entries"]["securitySchemes"] == [
        {"type": "oauth2", "scopes": ["planelocket-symptoms/read"]}
    ]
    for name in ("log_symptoms", "update_symptom_entry", "delete_symptom_entry", "show_attachment_uploader",
                 "start_attachment_upload", "complete_attachment_upload", "delete_symptom_attachment"):
        expected = [{"type": "oauth2", "scopes": ["planelocket-symptoms/write"]}]
        assert tools[name]["securitySchemes"] == expected
        assert tools[name]["_meta"]["securitySchemes"] == expected
    for name in ("list_symptom_attachments", "get_symptom_attachment"):
        assert tools[name]["securitySchemes"] == [{"type": "oauth2", "scopes": ["planelocket-symptoms/read"]}]


def test_attachment_upload_is_verified_and_partitioned(environment):
    entry = app.create_entry({"sub": "person-a"}, {"symptoms": [{"name": "swelling"}]})
    started = app.start_attachment_upload({"sub": "person-a"}, entry["entry_id"], "ankle.png", "image/png", 12)
    item = next(value for value in environment.items.values() if value.get("attachment_id") == started["attachment_id"])
    environment.s3.objects[item["object_key"]] = {"body": b"\x89PNG\r\n\x1a\nDATA", "content_type": "image/png"}
    completed = app.complete_attachment_upload({"sub": "person-a"}, entry["entry_id"], started["attachment_id"])
    assert completed["attachment"]["status"] == "ready"
    assert "object_key" not in completed["attachment"]
    with pytest.raises(app.RelayError) as exc:
        app.list_attachments({"sub": "person-b"}, entry["entry_id"])
    assert exc.value.status == 404


def test_attachment_resource_declares_exact_s3_origin():
    resource = app.attachment_resource()["contents"][0]
    assert resource["mimeType"] == "text/html;profile=mcp-app"
    assert resource["_meta"]["ui"]["csp"]["connectDomains"] == ["https://attachments.s3.us-east-2.amazonaws.com"]


def test_mcp_tool_call_returns_authentication_metadata_without_token():
    event = api_event("POST", "/mcp", {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "list_symptom_entries", "arguments": {}},
    })
    result = app.lambda_handler(event, None)
    payload = json.loads(result["body"])
    assert result["statusCode"] == 200
    auth = payload["result"]["_meta"]["mcp/www_authenticate"][0]
    assert "resource_metadata=" in auth
    assert 'error="insufficient_scope"' in auth


def test_empty_mcp_probe_receives_oauth_challenge():
    result = app.lambda_handler(api_event("POST", "/mcp"), None)
    assert result["statusCode"] == 401
    assert "resource_metadata=" in result["headers"]["WWW-Authenticate"]


def test_openapi_arrays_define_items():
    result = app.lambda_handler(api_event("GET", "/openapi.json"), None)
    payload = json.loads(result["body"])
    properties = payload["components"]["schemas"]["Entry"]["properties"]
    for value in properties.values():
        if value.get("type") == "array":
            assert "items" in value


def test_timestamps_are_canonicalized_to_utc(environment):
    created = app.create_entry({"sub": "person-a"}, {
        "occurred_at": "2026-08-21T18:00:00-05:00",
        "symptoms": [{"name": "PVCs"}],
    })
    assert created["occurred_at"] == "2026-08-21T23:00:00.000000Z"
    assert environment.items[("USER#person-a", "ENTRY#" + created["entry_id"])]["SK"].startswith("ENTRY#2026-08-21T23:00:00.000000Z~")


def test_list_entries_paginates_without_duplicates_or_attachments(environment):
    claims = {"sub": "person-a"}
    for second in range(12):
        app.create_entry(claims, {"occurred_at": f"2026-08-21T12:00:{second:02d}Z", "symptoms": [{"name": "pain"}]})
    entry = next(value for value in environment.items.values() if value["SK"].startswith("ENTRY#"))
    environment.put_item(Item={"PK": entry["PK"], "SK": "ATTACHMENT#ignored", "attachment_id": "ignored"})
    seen = []
    cursor = None
    while True:
        page = app.list_entries(claims, limit=5, cursor=cursor)
        seen.extend(item["entry_id"] for item in page["entries"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert len(seen) == 12
    assert len(set(seen)) == 12


def test_cursor_is_bound_to_user_and_filters(environment):
    claims = {"sub": "person-a"}
    for second in range(3):
        app.create_entry(claims, {"occurred_at": f"2026-08-21T12:00:{second:02d}Z", "symptoms": [{"name": "pain"}]})
    cursor = app.list_entries(claims, limit=1)["next_cursor"]
    with pytest.raises(app.RelayError, match="Invalid or expired"):
        app.list_entries({"sub": "person-b"}, limit=1, cursor=cursor)
    with pytest.raises(app.RelayError, match="Invalid or expired"):
        app.list_entries(claims, limit=1, since="2026-08-21T00:00:00Z", cursor=cursor)
    with pytest.raises(app.RelayError, match="Invalid or expired"):
        app.list_entries(claims, limit=1, cursor=cursor[:-1] + ("A" if cursor[-1] != "A" else "B"))


def test_date_bounds_are_applied_in_dynamodb_key_condition(environment):
    app.list_entries({"sub": "person-a"}, since="2026-08-01T00:00:00-05:00", until="2026-08-31T23:59:59-05:00")
    expression = environment.last_query["KeyConditionExpression"]
    assert expression._values[1].__class__.__name__ == "Between"
    assert "occurred_at" not in environment.last_query


def test_since_after_until_is_rejected():
    with pytest.raises(app.RelayError, match="since must not be later"):
        app.list_entries({"sub": "person-a"}, since="2026-09-01T00:00:00Z", until="2026-08-01T00:00:00Z")


def test_reporting_loader_traverses_more_than_100_entries(environment):
    claims = {"sub": "person-a"}
    for index in range(105):
        minute, second = divmod(index, 60)
        app.create_entry(claims, {"occurred_at": f"2026-08-21T12:{minute:02d}:{second:02d}Z", "symptoms": [{"name": "pain"}]})
    loaded = app.report_entries(claims, "2026-08-21T00:00:00Z", "2026-08-22T00:00:00Z")
    assert len(loaded) == 105


def test_report_summary_route_is_oauth_protected_and_no_store(environment, monkeypatch):
    claims = {"sub": "person-a"}
    app.create_entry(claims, {"occurred_at": "2026-08-21T12:00:00Z", "symptoms": [{"name": "PVCs", "severity": 5}]})
    monkeypatch.setattr(app, "authenticate", lambda event, scope: claims)
    event = api_event("GET", "/reports/summary")
    event["queryStringParameters"] = {"since": "2026-08-21T00:00:00Z", "until": "2026-08-22T00:00:00Z"}
    result = app.lambda_handler(event, None)
    payload = json.loads(result["body"])
    assert result["statusCode"] == 200
    assert result["headers"]["cache-control"] == "no-store"
    assert result["headers"]["access-control-allow-origin"] == "https://health.loopers.space"
    assert payload["occurrence_count"] == 1


def test_report_options_is_unauthenticated_and_cors_enabled():
    result = app.lambda_handler(api_event("OPTIONS", "/reports/summary"), None)
    assert result["statusCode"] == 204
    assert result["body"] == ""
    assert result["headers"]["access-control-allow-origin"] == "https://health.loopers.space"
    assert result["headers"]["access-control-allow-methods"] == "GET,OPTIONS"
    assert result["headers"]["access-control-allow-headers"] == "Authorization,Content-Type"


def test_clinician_report_json_route_is_oauth_protected(environment, monkeypatch):
    claims = {"sub": "person-a"}
    app.create_entry(claims, {
        "occurred_at": "2026-08-21T12:00:00Z",
        "symptoms": [{"name": "PVCs", "severity": 5}],
    })
    monkeypatch.setattr(app, "authenticate", lambda event, scope: claims)
    event = api_event("GET", "/reports/clinician-report")
    event["queryStringParameters"] = {
        "specialty": "cardiology",
        "format": "json",
        "since": "2026-08-21T00:00:00Z",
        "until": "2026-08-22T00:00:00Z",
    }
    result = app.lambda_handler(event, None)
    payload = json.loads(result["body"])
    assert result["statusCode"] == 200
    assert payload["report_type"] == "cardiology"
    assert payload["summary"]["count"] == 1
    assert result["headers"]["access-control-allow-origin"] == "https://health.loopers.space"
