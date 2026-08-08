import base64
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from urllib.parse import unquote

import boto3
import jwt
from boto3.dynamodb.conditions import Key
from jwt import PyJWKClient

LOG = logging.getLogger()
LOG.setLevel(logging.INFO)
VERSION = "0.1.0"
MAX_BODY_BYTES = 64 * 1024
MCP_PROTOCOL_VERSION = "2025-06-18"
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d{1,6})?)?(Z|[+-]\d{2}:\d{2})$")

_table = None
_jwks = None


class RelayError(Exception):
    def __init__(self, status: int, message: str, headers: dict[str, str] | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.headers = headers or {}


def json_default(value):
    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)
    raise TypeError(f"Cannot serialize {type(value).__name__}")


def response(status: int, body: Any, headers: dict[str, str] | None = None):
    result_headers = {"content-type": "application/json; charset=utf-8", "cache-control": "no-store"}
    result_headers.update(headers or {})
    return {"statusCode": status, "headers": result_headers, "body": json.dumps(body, default=json_default)}


def table():
    global _table
    if _table is None:
        _table = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
    return _table


def headers_lower(event):
    return {str(k).lower(): str(v) for k, v in (event.get("headers") or {}).items()}


def method_path(event):
    http = (event.get("requestContext") or {}).get("http") or {}
    return str(http.get("method") or "").upper(), str(event.get("rawPath") or http.get("path") or "/")


def public_base_url(event):
    context = event.get("requestContext") or {}
    domain = context.get("domainName") or headers_lower(event).get("host")
    if not domain:
        raise RelayError(500, "Unable to determine public API hostname.")
    return f"{headers_lower(event).get('x-forwarded-proto', 'https')}://{domain}"


def parse_body(event):
    raw = event.get("body") or ""
    data = base64.b64decode(raw) if event.get("isBase64Encoded") else raw.encode()
    if not data:
        raise RelayError(400, "JSON request body required.")
    if len(data) > MAX_BODY_BYTES:
        raise RelayError(413, "Request body exceeds 64 KiB.")
    try:
        body = json.loads(data)
    except Exception as exc:
        raise RelayError(400, "Request body must be valid UTF-8 JSON.") from exc
    if not isinstance(body, dict):
        raise RelayError(400, "JSON request body must be an object.")
    return body


def jwks_client():
    global _jwks
    if _jwks is None:
        _jwks = PyJWKClient(os.environ["COGNITO_JWKS_URL"], cache_keys=True)
    return _jwks


def authenticate(event, required_scope):
    auth = headers_lower(event).get("authorization", "")
    if not auth.startswith("Bearer "):
        raise RelayError(401, "OAuth bearer token required.")
    token = auth[7:].strip()
    try:
        key = jwks_client().get_signing_key_from_jwt(token)
        claims = jwt.decode(token, key.key, algorithms=["RS256"], issuer=os.environ["COGNITO_ISSUER"], options={"verify_aud": False, "require": ["exp", "iat", "iss", "sub"]})
    except Exception as exc:
        LOG.info("OAuth validation failed: %s", type(exc).__name__)
        raise RelayError(401, "Invalid or expired OAuth access token.") from exc
    if claims.get("token_use") != "access" or claims.get("client_id") != os.environ["COGNITO_CLIENT_ID"]:
        raise RelayError(401, "OAuth access token was not issued for this client.")
    scopes = set(str(claims.get("scope", "")).split())
    if required_scope not in scopes:
        raise RelayError(403, f"OAuth scope required: {required_scope}")
    return claims


def now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def validate_timestamp(value, field="occurred_at"):
    if not isinstance(value, str) or not ISO_RE.match(value):
        raise RelayError(400, f"{field} must be an ISO 8601 timestamp with timezone.")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RelayError(400, f"{field} must be a valid timestamp.") from exc
    return value


def symptom_schema(required=False):
    schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "minLength": 1, "maxLength": 100},
            "severity": {"type": "number", "minimum": 0, "maximum": 10},
            "location": {"type": "string", "maxLength": 100},
            "notes": {"type": "string", "maxLength": 1000},
        },
        "required": ["name"],
        "additionalProperties": False,
    }
    return schema


def entry_schema(required=True):
    schema = {
        "type": "object",
        "properties": {
            "occurred_at": {"type": "string", "format": "date-time"},
            "symptoms": {"type": "array", "items": symptom_schema(), "maxItems": 50},
            "sleep_hours": {"type": "number", "minimum": 0, "maximum": 24},
            "medications": {"type": "array", "items": {"type": "string", "maxLength": 200}, "maxItems": 50},
            "tags": {"type": "array", "items": {"type": "string", "maxLength": 50}, "maxItems": 30},
            "notes": {"type": "string", "maxLength": 4000},
            "original_text": {"type": "string", "maxLength": 8000},
        },
        "additionalProperties": False,
    }
    if required:
        schema["required"] = ["symptoms"]
    else:
        schema["minProperties"] = 1
    return schema


ALLOWED_FIELDS = set(entry_schema()["properties"])


def validate_entry(body, partial=False):
    unknown = sorted(set(body) - ALLOWED_FIELDS)
    if unknown:
        raise RelayError(400, "Unsupported fields: " + ", ".join(unknown))
    if partial and not body:
        raise RelayError(400, "Provide at least one field to update.")
    if not partial and not isinstance(body.get("symptoms"), list):
        raise RelayError(400, "symptoms must be an array.")
    if "occurred_at" in body:
        validate_timestamp(body["occurred_at"])
    if "symptoms" in body:
        if not isinstance(body["symptoms"], list) or len(body["symptoms"]) > 50:
            raise RelayError(400, "symptoms must be an array of at most 50 items.")
        for symptom in body["symptoms"]:
            if not isinstance(symptom, dict) or not str(symptom.get("name", "")).strip():
                raise RelayError(400, "Each symptom requires a name.")
            if set(symptom) - {"name", "severity", "location", "notes"}:
                raise RelayError(400, "A symptom contains unsupported fields.")
            if "severity" in symptom and not isinstance(symptom["severity"], (int, float)):
                raise RelayError(400, "Symptom severity must be numeric from 0 through 10.")
            if "severity" in symptom and not 0 <= symptom["severity"] <= 10:
                raise RelayError(400, "Symptom severity must be from 0 through 10.")
    if "sleep_hours" in body and (not isinstance(body["sleep_hours"], (int, float)) or not 0 <= body["sleep_hours"] <= 24):
        raise RelayError(400, "sleep_hours must be numeric from 0 through 24.")
    for field, limit in (("medications", 50), ("tags", 30)):
        if field in body and (not isinstance(body[field], list) or len(body[field]) > limit or not all(isinstance(v, str) for v in body[field])):
            raise RelayError(400, f"{field} must be an array of strings with at most {limit} items.")


def owner_key(claims):
    return "USER#" + claims["sub"]


def encode_entry_id(occurred_at, unique_id):
    return f"{occurred_at}~{unique_id}"


def entry_sk(entry_id):
    decoded = unquote(str(entry_id))
    if "~" not in decoded or len(decoded) > 120:
        raise RelayError(400, "Invalid entry_id.")
    return "ENTRY#" + decoded


def public_item(item):
    return {k: v for k, v in item.items() if k not in {"PK", "SK"}}


def create_entry(claims, body):
    validate_entry(body)
    occurred_at = body.get("occurred_at") or now_iso()
    validate_timestamp(occurred_at)
    unique_id = str(uuid.uuid4())
    entry_id = encode_entry_id(occurred_at, unique_id)
    created_at = now_iso()
    item = {"PK": owner_key(claims), "SK": "ENTRY#" + entry_id, "entry_id": entry_id, "occurred_at": occurred_at, "created_at": created_at, "updated_at": created_at, **body}
    item["occurred_at"] = occurred_at
    table().put_item(Item=item, ConditionExpression="attribute_not_exists(PK) AND attribute_not_exists(SK)")
    return public_item(item)


def list_entries(claims, limit=50, since=None, until=None):
    try:
        limit = max(1, min(int(limit), 100))
    except (TypeError, ValueError):
        raise RelayError(400, "limit must be an integer from 1 through 100.")
    if since:
        validate_timestamp(since, "since")
    if until:
        validate_timestamp(until, "until")
    condition = Key("PK").eq(owner_key(claims)) & Key("SK").begins_with("ENTRY#")
    result = table().query(KeyConditionExpression=condition, Limit=limit, ScanIndexForward=False)
    items = [public_item(item) for item in result.get("Items", [])]
    if since:
        items = [item for item in items if item["occurred_at"] >= since]
    if until:
        items = [item for item in items if item["occurred_at"] <= until]
    return {"entries": items, "count": len(items)}


def update_entry(claims, entry_id, patch):
    validate_entry(patch, partial=True)
    key = {"PK": owner_key(claims), "SK": entry_sk(entry_id)}
    current = table().get_item(Key=key, ConsistentRead=True).get("Item")
    if not current:
        raise RelayError(404, "Entry not found.")
    merged = {**current, **patch, "updated_at": now_iso()}
    validate_entry({k: merged[k] for k in ALLOWED_FIELDS if k in merged})
    table().put_item(Item=merged, ConditionExpression="attribute_exists(PK) AND attribute_exists(SK)")
    return public_item(merged)


def delete_entry(claims, entry_id):
    result = table().delete_item(Key={"PK": owner_key(claims), "SK": entry_sk(entry_id)}, ReturnValues="ALL_OLD")
    old = result.get("Attributes")
    if not old:
        raise RelayError(404, "Entry not found.")
    return {"deleted": True, "entry": public_item(old)}


def mcp_metadata(event):
    return {"resource": public_base_url(event) + "/mcp", "authorization_servers": [os.environ["COGNITO_ISSUER"]], "scopes_supported": [os.environ["ACTION_READ_SCOPE"], os.environ["ACTION_WRITE_SCOPE"]], "bearer_methods_supported": ["header"], "resource_name": "PlaneLocket Symptom Log"}


def mcp_challenge(event, scopes=True):
    value = f'Bearer resource_metadata="{public_base_url(event)}/.well-known/oauth-protected-resource/mcp"'
    if scopes:
        value += f', scope="{os.environ["ACTION_READ_SCOPE"]} {os.environ["ACTION_WRITE_SCOPE"]}"'
    return {"WWW-Authenticate": value}


def mcp_tools():
    return [
        {"name": "log_symptoms", "description": "Log symptoms and related context for the signed-in person. Preserve the user's wording in original_text when available.", "inputSchema": entry_schema(), "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}},
        {"name": "list_symptom_entries", "description": "List only the signed-in person's recent symptom entries.", "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 100}, "since": {"type": "string", "format": "date-time"}, "until": {"type": "string", "format": "date-time"}}, "additionalProperties": False}, "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}},
        {"name": "update_symptom_entry", "description": "Correct fields on one of the signed-in person's symptom entries.", "inputSchema": {"type": "object", "required": ["entry_id", "changes"], "properties": {"entry_id": {"type": "string"}, "changes": entry_schema(False)}, "additionalProperties": False}, "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}},
        {"name": "delete_symptom_entry", "description": "Permanently delete one of the signed-in person's symptom entries. Use only when explicitly requested.", "inputSchema": {"type": "object", "required": ["entry_id"], "properties": {"entry_id": {"type": "string"}}, "additionalProperties": False}, "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False}},
    ]


def mcp_result(request_id, payload):
    return response(200, {"jsonrpc": "2.0", "id": request_id, "result": payload})


def mcp_error(request_id, code, message):
    return response(200, {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})


def mcp_endpoint(event):
    if not headers_lower(event).get("authorization") and event.get("body") in (None, ""):
        return response(401, {"error": "unauthorized"}, mcp_challenge(event))
    body = parse_body(event)
    request_id, method, params = body.get("id"), body.get("method"), body.get("params") or {}
    if body.get("jsonrpc") != "2.0" or not isinstance(method, str):
        return mcp_error(request_id, -32600, "Invalid JSON-RPC request.")
    if method == "initialize":
        return mcp_result(request_id, {"protocolVersion": MCP_PROTOCOL_VERSION, "capabilities": {"tools": {"listChanged": False}}, "serverInfo": {"name": "planelocket-symptoms", "version": VERSION}})
    if method == "notifications/initialized":
        return {"statusCode": 202, "headers": {"cache-control": "no-store"}, "body": ""}
    if method == "tools/list":
        return mcp_result(request_id, {"tools": mcp_tools()})
    if method != "tools/call":
        return mcp_error(request_id, -32601, f"Method not found: {method}")
    name, args = params.get("name"), params.get("arguments")
    if not isinstance(name, str) or not isinstance(args, dict):
        return mcp_error(request_id, -32602, "tools/call requires name and arguments.")
    try:
        if name == "log_symptoms":
            payload = create_entry(authenticate(event, os.environ["ACTION_WRITE_SCOPE"]), args)
        elif name == "list_symptom_entries":
            payload = list_entries(authenticate(event, os.environ["ACTION_READ_SCOPE"]), args.get("limit", 50), args.get("since"), args.get("until"))
        elif name == "update_symptom_entry":
            payload = update_entry(authenticate(event, os.environ["ACTION_WRITE_SCOPE"]), args.get("entry_id", ""), args.get("changes") or {})
        elif name == "delete_symptom_entry":
            payload = delete_entry(authenticate(event, os.environ["ACTION_WRITE_SCOPE"]), args.get("entry_id", ""))
        else:
            raise RelayError(404, f"Unknown MCP tool: {name}")
        return mcp_result(request_id, {"content": [{"type": "text", "text": json.dumps(payload, default=json_default)}], "structuredContent": payload, "isError": False})
    except RelayError as exc:
        if exc.status in (401, 403):
            return response(exc.status, {"message": exc.message}, mcp_challenge(event))
        payload = {"message": exc.message, "status": exc.status}
        return mcp_result(request_id, {"content": [{"type": "text", "text": json.dumps(payload)}], "isError": True})


def openapi_document(event):
    base = public_base_url(event)
    read_scope, write_scope = os.environ["ACTION_READ_SCOPE"], os.environ["ACTION_WRITE_SCOPE"]
    auth = {"type": "oauth2", "flows": {"authorizationCode": {"authorizationUrl": os.environ["COGNITO_AUTHORIZATION_ENDPOINT"], "tokenUrl": os.environ["COGNITO_TOKEN_ENDPOINT"], "scopes": {read_scope: "Read personal symptom entries", write_scope: "Write personal symptom entries"}}}}
    return {"openapi": "3.1.0", "info": {"title": "PlaneLocket Symptom Log", "version": VERSION}, "servers": [{"url": base}], "components": {"securitySchemes": {"cognitoOAuth": auth}, "schemas": {"Entry": entry_schema()}}, "paths": {"/entries": {"get": {"operationId": "listSymptomEntries", "security": [{"cognitoOAuth": [read_scope]}], "responses": {"200": {"description": "Personal symptom entries"}}}, "post": {"operationId": "logSymptoms", "security": [{"cognitoOAuth": [write_scope]}], "requestBody": {"required": True, "content": {"application/json": {"schema": entry_schema()}}}, "responses": {"201": {"description": "Entry created"}}}}}}


def lambda_handler(event: dict[str, Any], context: Any):
    try:
        method, path = method_path(event)
        if method == "GET" and path == "/.well-known/oauth-protected-resource/mcp":
            return response(200, mcp_metadata(event))
        if method == "POST" and path == "/mcp":
            return mcp_endpoint(event)
        if method == "GET" and path == "/openapi.json":
            return response(200, openapi_document(event))
        if method == "GET" and path == "/health":
            claims = authenticate(event, os.environ["ACTION_READ_SCOPE"])
            return response(200, {"ok": True, "service": "planelocket-symptom-relay", "version": VERSION, "subject": claims["sub"]})
        if path == "/entries" and method == "POST":
            return response(201, create_entry(authenticate(event, os.environ["ACTION_WRITE_SCOPE"]), parse_body(event)))
        if path == "/entries" and method == "GET":
            query = event.get("queryStringParameters") or {}
            return response(200, list_entries(authenticate(event, os.environ["ACTION_READ_SCOPE"]), query.get("limit", 50), query.get("since"), query.get("until")))
        if path.startswith("/entries/") and method == "PUT":
            return response(200, update_entry(authenticate(event, os.environ["ACTION_WRITE_SCOPE"]), path.split("/", 2)[2], parse_body(event)))
        if path.startswith("/entries/") and method == "DELETE":
            return response(200, delete_entry(authenticate(event, os.environ["ACTION_WRITE_SCOPE"]), path.split("/", 2)[2]))
        return response(404, {"message": "Route not found."})
    except RelayError as exc:
        return response(exc.status, {"message": exc.message}, exc.headers)
    except Exception:
        LOG.exception("Unhandled symptom relay failure")
        return response(500, {"message": "Internal relay error."})

