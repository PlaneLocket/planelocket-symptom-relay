import json
import os
import sys
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
        # The production query applies the PK in DynamoDB. Tests verify the
        # owner partition by inspecting stored keys and use a filtered view.
        return {"Items": list(self.items.values())}


@pytest.fixture(autouse=True)
def environment(monkeypatch):
    monkeypatch.setenv("ACTION_READ_SCOPE", "planelocket-symptoms/read")
    monkeypatch.setenv("ACTION_WRITE_SCOPE", "planelocket-symptoms/write")
    monkeypatch.setenv("COGNITO_ISSUER", "https://issuer.example/pool")
    monkeypatch.setenv("COGNITO_CLIENT_ID", "client")
    monkeypatch.setenv("COGNITO_AUTHORIZATION_ENDPOINT", "https://auth.example/authorize")
    monkeypatch.setenv("COGNITO_TOKEN_ENDPOINT", "https://auth.example/token")
    fake = FakeTable()
    monkeypatch.setattr(app, "table", lambda: fake)
    return fake


def api_event(method, path, body=None):
    return {
        "rawPath": path,
        "headers": {"host": "api.example", "x-forwarded-proto": "https"},
        "requestContext": {"domainName": "api.example", "http": {"method": method, "path": path}},
        "body": json.dumps(body) if body is not None else None,
    }


def test_create_partitions_entries_by_cognito_subject(environment):
    first = app.create_entry({"sub": "person-a"}, {"symptoms": [{"name": "fatigue", "severity": 7}]})
    second = app.create_entry({"sub": "person-b"}, {"symptoms": [{"name": "headache", "severity": 4}]})
    assert first["entry_id"] != second["entry_id"]
    assert {key[0] for key in environment.items} == {"USER#person-a", "USER#person-b"}


def test_invalid_severity_is_rejected():
    with pytest.raises(app.RelayError, match="0 through 10"):
        app.create_entry({"sub": "person-a"}, {"symptoms": [{"name": "pain", "severity": 11}]})


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
        "log_symptoms", "list_symptom_entries", "update_symptom_entry", "delete_symptom_entry"
    }


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

