import base64
import json
import logging
import os
import re
import uuid
from pathlib import Path
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
VERSION = "0.2.0"
MAX_BODY_BYTES = 64 * 1024
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
MAX_ATTACHMENTS_PER_ENTRY = 10
PRESIGNED_URL_SECONDS = 300
MCP_PROTOCOL_VERSION = "2025-06-18"
ATTACHMENT_RESOURCE_URI = "ui://planelocket-symptoms/attachment-uploader.html"
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d{1,6})?)?(Z|[+-]\d{2}:\d{2})$")
SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._ -]+")
CONTENT_TYPES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".heic": "image/heic", ".heif": "image/heif", ".pdf": "application/pdf",
}

_table = None
_jwks = None
_s3 = None


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


def s3():
    global _s3
    if _s3 is None:
        _s3 = boto3.client("s3")
    return _s3


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


def public_attachment(item):
    return {k: v for k, v in item.items() if k not in {"PK", "SK", "object_key"}}


def require_entry(claims, entry_id):
    item = table().get_item(Key={"PK": owner_key(claims), "SK": entry_sk(entry_id)}, ConsistentRead=True).get("Item")
    if not item:
        raise RelayError(404, "Entry not found.")
    return item


def attachment_prefix(entry_id):
    decoded = unquote(str(entry_id))
    entry_sk(decoded)
    return f"ATTACHMENT#{decoded}#"


def attachment_sk(entry_id, attachment_id):
    if not re.fullmatch(r"[0-9a-f-]{36}", str(attachment_id)):
        raise RelayError(400, "Invalid attachment_id.")
    return attachment_prefix(entry_id) + str(attachment_id)


def normalize_file(filename, content_type, size_bytes):
    if not isinstance(filename, str) or not filename.strip() or len(filename) > 255:
        raise RelayError(400, "filename must be from 1 through 255 characters.")
    try:
        size = int(size_bytes)
    except (TypeError, ValueError) as exc:
        raise RelayError(400, "size_bytes must be an integer.") from exc
    if size < 1 or size > MAX_ATTACHMENT_BYTES:
        raise RelayError(400, "Attachments must be from 1 byte through 20 MiB.")
    clean = SAFE_FILENAME_RE.sub("_", Path(filename).name).strip(" .") or "attachment"
    extension = Path(clean).suffix.lower()
    expected = CONTENT_TYPES.get(extension)
    supplied = str(content_type or "").lower().split(";", 1)[0].strip()
    if not expected or supplied not in {expected, "", "application/octet-stream"}:
        raise RelayError(400, "Only JPEG, PNG, HEIC/HEIF, and PDF files are accepted.")
    return clean, expected, size


def list_attachments(claims, entry_id, include_pending=False):
    require_entry(claims, entry_id)
    result = table().query(
        KeyConditionExpression=Key("PK").eq(owner_key(claims)) & Key("SK").begins_with(attachment_prefix(entry_id)),
        ScanIndexForward=False,
    )
    items = [public_attachment(item) for item in result.get("Items", []) if "attachment_id" in item]
    if not include_pending:
        items = [item for item in items if item.get("status") == "ready"]
    return {"entry_id": unquote(str(entry_id)), "attachments": items, "count": len(items)}


def start_attachment_upload(claims, entry_id, filename, content_type, size_bytes):
    require_entry(claims, entry_id)
    existing = list_attachments(claims, entry_id, include_pending=True)["attachments"]
    if len(existing) >= MAX_ATTACHMENTS_PER_ENTRY:
        raise RelayError(409, "This symptom entry already has 10 attachments.")
    clean, normalized_type, size = normalize_file(filename, content_type, size_bytes)
    attachment_id = str(uuid.uuid4())
    object_key = f"users/{claims['sub']}/entries/{unquote(str(entry_id))}/{attachment_id}"
    created_at = now_iso()
    item = {
        "PK": owner_key(claims), "SK": attachment_sk(entry_id, attachment_id),
        "entry_id": unquote(str(entry_id)), "attachment_id": attachment_id,
        "object_key": object_key, "filename": clean, "content_type": normalized_type,
        "size_bytes": size, "status": "pending", "created_at": created_at,
    }
    table().put_item(Item=item, ConditionExpression="attribute_not_exists(PK) AND attribute_not_exists(SK)")
    upload_url = s3().generate_presigned_url(
        "put_object",
        Params={"Bucket": os.environ["ATTACHMENT_BUCKET"], "Key": object_key, "ContentType": normalized_type, "Tagging": "status=pending"},
        ExpiresIn=PRESIGNED_URL_SECONDS,
    )
    return {"entry_id": item["entry_id"], "attachment_id": attachment_id, "filename": clean, "content_type": normalized_type, "size_bytes": size, "upload_url": upload_url, "expires_in": PRESIGNED_URL_SECONDS}


def valid_magic(content_type, header):
    if content_type == "image/jpeg":
        return header.startswith(b"\xff\xd8\xff")
    if content_type == "image/png":
        return header.startswith(b"\x89PNG\r\n\x1a\n")
    if content_type == "application/pdf":
        return header.startswith(b"%PDF-")
    return len(header) >= 12 and header[4:8] == b"ftyp" and header[8:12] in {b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"}


def complete_attachment_upload(claims, entry_id, attachment_id):
    key = {"PK": owner_key(claims), "SK": attachment_sk(entry_id, attachment_id)}
    item = table().get_item(Key=key, ConsistentRead=True).get("Item")
    if not item:
        raise RelayError(404, "Pending attachment not found.")
    if item.get("status") == "ready":
        return {"attachment": public_attachment(item)}
    bucket = os.environ["ATTACHMENT_BUCKET"]
    try:
        head = s3().head_object(Bucket=bucket, Key=item["object_key"])
        body = s3().get_object(Bucket=bucket, Key=item["object_key"], Range="bytes=0-31")["Body"].read()
    except Exception as exc:
        raise RelayError(409, "The upload has not completed yet.") from exc
    if int(head.get("ContentLength", -1)) != int(item["size_bytes"]) or head.get("ContentType") != item["content_type"] or not valid_magic(item["content_type"], body):
        s3().delete_object(Bucket=bucket, Key=item["object_key"])
        table().delete_item(Key=key)
        raise RelayError(400, "The uploaded file did not match its declared type or size and was removed.")
    item.update({"status": "ready", "verified_at": now_iso()})
    table().put_item(Item=item, ConditionExpression="attribute_exists(PK) AND attribute_exists(SK)")
    s3().put_object_tagging(Bucket=bucket, Key=item["object_key"], Tagging={"TagSet": [{"Key": "status", "Value": "ready"}]})
    return {"attachment": public_attachment(item)}


def get_attachment_download(claims, entry_id, attachment_id):
    key = {"PK": owner_key(claims), "SK": attachment_sk(entry_id, attachment_id)}
    item = table().get_item(Key=key, ConsistentRead=True).get("Item")
    if not item or item.get("status") != "ready":
        raise RelayError(404, "Attachment not found.")
    url = s3().generate_presigned_url(
        "get_object",
        Params={"Bucket": os.environ["ATTACHMENT_BUCKET"], "Key": item["object_key"], "ResponseContentDisposition": f'inline; filename="{item["filename"]}"', "ResponseContentType": item["content_type"]},
        ExpiresIn=PRESIGNED_URL_SECONDS,
    )
    return {"attachment": public_attachment(item), "download_url": url, "expires_in": PRESIGNED_URL_SECONDS}


def delete_attachment(claims, entry_id, attachment_id):
    key = {"PK": owner_key(claims), "SK": attachment_sk(entry_id, attachment_id)}
    old = table().delete_item(Key=key, ReturnValues="ALL_OLD").get("Attributes")
    if not old:
        raise RelayError(404, "Attachment not found.")
    s3().delete_object(Bucket=os.environ["ATTACHMENT_BUCKET"], Key=old["object_key"])
    return {"deleted": True, "attachment": public_attachment(old)}


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
    attachments = list_attachments(claims, entry_id, include_pending=True)["attachments"]
    for attachment in attachments:
        delete_attachment(claims, entry_id, attachment["attachment_id"])
    result = table().delete_item(Key={"PK": owner_key(claims), "SK": entry_sk(entry_id)}, ReturnValues="ALL_OLD")
    old = result.get("Attributes")
    if not old:
        raise RelayError(404, "Entry not found.")
    return {"deleted": True, "entry": public_item(old), "attachments_deleted": len(attachments)}


def mcp_metadata(event):
    return {"resource": public_base_url(event) + "/mcp", "authorization_servers": [os.environ["COGNITO_ISSUER"]], "scopes_supported": [os.environ["ACTION_READ_SCOPE"], os.environ["ACTION_WRITE_SCOPE"]], "bearer_methods_supported": ["header"], "resource_name": "PlaneLocket Symptom Log"}


def mcp_challenge(event, scopes=True):
    value = f'Bearer resource_metadata="{public_base_url(event)}/.well-known/oauth-protected-resource/mcp"'
    if scopes:
        value += f', scope="{os.environ["ACTION_READ_SCOPE"]} {os.environ["ACTION_WRITE_SCOPE"]}"'
    return {"WWW-Authenticate": value}


def mcp_tools():
    read_security = [{"type": "oauth2", "scopes": [os.environ["ACTION_READ_SCOPE"]]}]
    write_security = [{"type": "oauth2", "scopes": [os.environ["ACTION_WRITE_SCOPE"]]}]
    tools = [
        {"name": "log_symptoms", "description": "Log symptoms and related context for the signed-in person. Preserve the user's wording in original_text when available.", "inputSchema": entry_schema(), "securitySchemes": write_security, "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}},
        {"name": "list_symptom_entries", "description": "List only the signed-in person's recent symptom entries.", "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 100}, "since": {"type": "string", "format": "date-time"}, "until": {"type": "string", "format": "date-time"}}, "additionalProperties": False}, "securitySchemes": read_security, "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}},
        {"name": "update_symptom_entry", "description": "Correct fields on one of the signed-in person's symptom entries.", "inputSchema": {"type": "object", "required": ["entry_id", "changes"], "properties": {"entry_id": {"type": "string"}, "changes": entry_schema(False)}, "additionalProperties": False}, "securitySchemes": write_security, "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}},
        {"name": "delete_symptom_entry", "description": "Permanently delete one of the signed-in person's symptom entries. Use only when explicitly requested.", "inputSchema": {"type": "object", "required": ["entry_id"], "properties": {"entry_id": {"type": "string"}}, "additionalProperties": False}, "securitySchemes": write_security, "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False}},
        {"name": "show_attachment_uploader", "description": "Open the secure file picker for a specific symptom entry. Use when the person asks to attach a photo or document.", "inputSchema": {"type": "object", "required": ["entry_id"], "properties": {"entry_id": {"type": "string"}}, "additionalProperties": False}, "securitySchemes": write_security, "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}},
        {"name": "start_attachment_upload", "description": "Create a short-lived private upload for a supported file after the person chooses it in the attachment picker.", "inputSchema": {"type": "object", "required": ["entry_id", "filename", "content_type", "size_bytes"], "properties": {"entry_id": {"type": "string"}, "filename": {"type": "string", "maxLength": 255}, "content_type": {"type": "string"}, "size_bytes": {"type": "integer", "minimum": 1, "maximum": MAX_ATTACHMENT_BYTES}}, "additionalProperties": False}, "securitySchemes": write_security, "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}},
        {"name": "complete_attachment_upload", "description": "Verify a completed private upload and link it to its symptom entry.", "inputSchema": {"type": "object", "required": ["entry_id", "attachment_id"], "properties": {"entry_id": {"type": "string"}, "attachment_id": {"type": "string"}}, "additionalProperties": False}, "securitySchemes": write_security, "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}},
        {"name": "list_symptom_attachments", "description": "List file attachments belonging to one of the signed-in person's symptom entries.", "inputSchema": {"type": "object", "required": ["entry_id"], "properties": {"entry_id": {"type": "string"}}, "additionalProperties": False}, "securitySchemes": read_security, "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}},
        {"name": "get_symptom_attachment", "description": "Create a five-minute private download link for one attachment belonging to the signed-in person.", "inputSchema": {"type": "object", "required": ["entry_id", "attachment_id"], "properties": {"entry_id": {"type": "string"}, "attachment_id": {"type": "string"}}, "additionalProperties": False}, "securitySchemes": read_security, "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}},
        {"name": "delete_symptom_attachment", "description": "Permanently delete one attachment. Use only when explicitly requested.", "inputSchema": {"type": "object", "required": ["entry_id", "attachment_id"], "properties": {"entry_id": {"type": "string"}, "attachment_id": {"type": "string"}}, "additionalProperties": False}, "securitySchemes": write_security, "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False}},
    ]
    # Compatibility mirror for hosts that still read OpenAI auth metadata from
    # _meta while adopting the top-level MCP securitySchemes field.
    for tool in tools:
        tool["_meta"] = {"securitySchemes": tool["securitySchemes"]}
    render_tool = next(tool for tool in tools if tool["name"] == "show_attachment_uploader")
    render_tool["_meta"].update({"ui": {"resourceUri": ATTACHMENT_RESOURCE_URI}, "openai/outputTemplate": ATTACHMENT_RESOURCE_URI})
    return tools


def attachment_resource():
    html = (Path(__file__).with_name("attachment_widget.html")).read_text(encoding="utf-8")
    return {
        "contents": [{
            "uri": ATTACHMENT_RESOURCE_URI,
            "mimeType": "text/html;profile=mcp-app",
            "text": html,
            "_meta": {"ui": {"prefersBorder": True, "csp": {"connectDomains": [os.environ["ATTACHMENT_BUCKET_ORIGIN"]], "resourceDomains": []}}},
        }]
    }


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
        return mcp_result(request_id, {"protocolVersion": MCP_PROTOCOL_VERSION, "capabilities": {"tools": {"listChanged": False}, "resources": {"listChanged": False}}, "serverInfo": {"name": "planelocket-symptoms", "version": VERSION}})
    if method == "notifications/initialized":
        return {"statusCode": 202, "headers": {"cache-control": "no-store"}, "body": ""}
    if method == "tools/list":
        return mcp_result(request_id, {"tools": mcp_tools()})
    if method == "resources/list":
        return mcp_result(request_id, {"resources": [{"uri": ATTACHMENT_RESOURCE_URI, "name": "Symptom attachment uploader", "mimeType": "text/html;profile=mcp-app"}]})
    if method == "resources/read":
        if params.get("uri") != ATTACHMENT_RESOURCE_URI:
            return mcp_error(request_id, -32602, "Unknown resource URI.")
        return mcp_result(request_id, attachment_resource())
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
        elif name == "show_attachment_uploader":
            claims = authenticate(event, os.environ["ACTION_WRITE_SCOPE"])
            entry = require_entry(claims, args.get("entry_id", ""))
            payload = {"entry_id": entry["entry_id"], "message": "Choose a supported file in the secure uploader."}
        elif name == "start_attachment_upload":
            payload = start_attachment_upload(authenticate(event, os.environ["ACTION_WRITE_SCOPE"]), args.get("entry_id", ""), args.get("filename"), args.get("content_type"), args.get("size_bytes"))
        elif name == "complete_attachment_upload":
            payload = complete_attachment_upload(authenticate(event, os.environ["ACTION_WRITE_SCOPE"]), args.get("entry_id", ""), args.get("attachment_id", ""))
        elif name == "list_symptom_attachments":
            payload = list_attachments(authenticate(event, os.environ["ACTION_READ_SCOPE"]), args.get("entry_id", ""))
        elif name == "get_symptom_attachment":
            payload = get_attachment_download(authenticate(event, os.environ["ACTION_READ_SCOPE"]), args.get("entry_id", ""), args.get("attachment_id", ""))
        elif name == "delete_symptom_attachment":
            payload = delete_attachment(authenticate(event, os.environ["ACTION_WRITE_SCOPE"]), args.get("entry_id", ""), args.get("attachment_id", ""))
        else:
            raise RelayError(404, f"Unknown MCP tool: {name}")
        return mcp_result(request_id, {"content": [{"type": "text", "text": json.dumps(payload, default=json_default)}], "structuredContent": payload, "isError": False})
    except RelayError as exc:
        if exc.status in (401, 403):
            challenge = mcp_challenge(event)["WWW-Authenticate"]
            challenge += ', error="insufficient_scope", error_description="Authentication with the required symptom-log scope is required"'
            return mcp_result(request_id, {
                "content": [{"type": "text", "text": exc.message}],
                "_meta": {"mcp/www_authenticate": [challenge]},
                "isError": True,
            })
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
