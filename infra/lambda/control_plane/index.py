"""HTTP control plane for Autocaption (Lambda Function URL behind CloudFront /api).

Starts one-shot Fargate tasks; does not render video. Payload format 2.0.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, Optional, Tuple
from urllib.parse import unquote

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

AWS_REGION = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "eu-central-1"
JOBS_TABLE = os.environ.get("JOBS_TABLE_NAME") or "AutocaptionJobs"
MEDIA_BUCKET = os.environ.get("MEDIA_BUCKET") or ""
STYLES_BUCKET = os.environ.get("STYLES_BUCKET") or "autocaption-styles-deadzone-423623826655"
API_KEY_SECRET = os.environ.get("API_KEY_SECRET_NAME") or "autocaption/backend-api-key"
ECS_CLUSTER = os.environ.get("ECS_CLUSTER") or ""
TRANSCRIBE_TASK = os.environ.get("TRANSCRIBE_TASK_FAMILY") or ""
BURN_TASK = os.environ.get("BURN_TASK_FAMILY") or ""
CONTAINER_NAME = os.environ.get("CONTAINER_NAME") or "worker"
ECS_SUBNETS = [s.strip() for s in (os.environ.get("ECS_SUBNETS") or "").split(",") if s.strip()]
ECS_SECURITY_GROUP = os.environ.get("ECS_SECURITY_GROUP") or ""
DOWNLOAD_TTL = int(os.environ.get("DOWNLOAD_URL_TTL_SECONDS") or str(12 * 60 * 60))
KARAOKE_DEFAULT = (os.environ.get("KARAOKE_ENABLED_DEFAULT") or "true").lower() in (
    "1",
    "true",
    "yes",
    "on",
)

PUBLIC_PATHS = {"/", "/health"}
PUBLIC_GET_PREFIXES = (
    "/styles/templates",
    "/styles/fonts",
    "/styles/stroke-previews",
)

PROCESS_STAGE = {
    "generate": "transcribe",
    "burn": "burn",
    "burn_words": "burn_words",
}
BURN_STAGES = frozenset({"align_burn", "burn", "burn_words"})

SLUG_RE = re.compile(r"[^a-z0-9]+")
STROKE_PREVIEW_COUNT = 16

_clients: Dict[str, Any] = {}
_api_key_cache: Dict[str, Any] = {"value": None, "expires": 0}


def _client(name: str):
    if name not in _clients:
        _clients[name] = boto3.client(name, region_name=AWS_REGION)
    return _clients[name]


def _ddb():
    return _client("dynamodb")


def _s3():
    return _client("s3")


def _ecs():
    return _client("ecs")


def _sm():
    return _client("secretsmanager")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ttl_epoch(days: int = 7) -> int:
    return int((datetime.now(timezone.utc) + timedelta(days=days)).timestamp())


def _json_default(obj):
    if isinstance(obj, Decimal):
        as_int = int(obj)
        if Decimal(as_int) == obj:
            return as_int
        return float(obj)
    raise TypeError(type(obj))


def _from_ddb(value: Any) -> Any:
    if isinstance(value, Decimal):
        as_int = int(value)
        if Decimal(as_int) == value:
            return as_int
        return float(value)
    if isinstance(value, dict):
        # DynamoDB typed JSON from low-level client
        if len(value) == 1:
            k = next(iter(value))
            if k == "S":
                return value["S"]
            if k == "N":
                n = Decimal(value["N"])
                return _from_ddb(n)
            if k == "BOOL":
                return value["BOOL"]
            if k == "NULL":
                return None
            if k == "M":
                return {ik: _from_ddb(iv) for ik, iv in value["M"].items()}
            if k == "L":
                return [_from_ddb(i) for i in value["L"]]
        return {ik: _from_ddb(iv) for ik, iv in value.items()}
    if isinstance(value, list):
        return [_from_ddb(i) for i in value]
    return value


def _to_attr(value: Any) -> Dict[str, Any]:
    if value is None:
        return {"NULL": True}
    if isinstance(value, bool):
        return {"BOOL": value}
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
            return {"NULL": True}
        return {"N": str(value)}
    if isinstance(value, dict):
        return {"M": {k: _to_attr(v) for k, v in value.items() if v is not None}}
    if isinstance(value, (list, tuple)):
        return {"L": [_to_attr(v) for v in value]}
    return {"S": str(value)}


def expected_api_key() -> str:
    now = time.time()
    if _api_key_cache["value"] is not None and now < _api_key_cache["expires"]:
        return _api_key_cache["value"]
    raw = _sm().get_secret_value(SecretId=API_KEY_SECRET)["SecretString"]
    data = json.loads(raw) if raw.strip().startswith("{") else {"apiKey": raw}
    key = (data.get("apiKey") or data.get("api_key") or "").strip()
    _api_key_cache["value"] = key
    _api_key_cache["expires"] = now + 60
    return key


def is_public_request(method: str, path: str) -> bool:
    if path in PUBLIC_PATHS:
        return True
    if path.startswith("/styles/stroke-previews/"):
        return True
    if (method or "").upper() in ("GET", "HEAD", "OPTIONS"):
        for prefix in PUBLIC_GET_PREFIXES:
            if path == prefix or path.startswith(prefix + "/"):
                return True
    return False


def extract_api_key(headers: Dict[str, str], query: Dict[str, str]) -> str:
    provided = headers.get("x-api-key") or ""
    auth = headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        provided = auth[7:].strip()
    if not provided:
        provided = (query.get("api_key") or "").strip()
    return provided


def api_key_accepted(provided: str, expected: str) -> bool:
    if not expected:
        return True
    if not provided or len(provided) != len(expected):
        return False
    return secrets.compare_digest(provided, expected)


def cors_headers(origin: str) -> Dict[str, str]:
    allow = origin or "*"
    return {
        "access-control-allow-origin": allow,
        "access-control-allow-headers": "Authorization,Content-Type,X-API-Key",
        "access-control-allow-methods": "GET,POST,PUT,OPTIONS",
        "access-control-max-age": "86400",
        "vary": "Origin",
    }


def respond(
    status: int,
    body: Any,
    *,
    origin: str = "",
    headers: Optional[Dict[str, str]] = None,
    binary: bool = False,
):
    hdrs = {"content-type": "application/json"}
    hdrs.update(cors_headers(origin))
    if headers:
        hdrs.update(headers)
    if binary:
        return {
            "statusCode": status,
            "headers": hdrs,
            "body": body,
            "isBase64Encoded": True,
        }
    if not isinstance(body, str):
        body = json.dumps(body, default=_json_default, ensure_ascii=False)
    return {"statusCode": status, "headers": hdrs, "body": body, "isBase64Encoded": False}


def event_path(event: dict) -> str:
    raw = event.get("rawPath") or (event.get("requestContext") or {}).get("http", {}).get("path") or "/"
    path = unquote(raw.split("?")[0] or "/")
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    return path or "/"


def event_method(event: dict) -> str:
    return (
        (event.get("requestContext") or {}).get("http", {}).get("method")
        or event.get("httpMethod")
        or "GET"
    ).upper()


def event_headers(event: dict) -> Dict[str, str]:
    raw = event.get("headers") or {}
    return {str(k).lower(): str(v) for k, v in raw.items() if v is not None}


def event_query(event: dict) -> Dict[str, str]:
    q = event.get("queryStringParameters") or {}
    return {str(k): str(v) for k, v in q.items() if v is not None}


def event_body(event: dict) -> Any:
    raw = event.get("body")
    if raw is None or raw == "":
        return None
    if event.get("isBase64Encoded"):
        decoded = base64.b64decode(raw)
        ctype = event_headers(event).get("content-type") or ""
        if "multipart/form-data" in ctype:
            return decoded
        raw = decoded.decode("utf-8", errors="replace")
    ctype = event_headers(event).get("content-type") or ""
    if "multipart/form-data" in ctype:
        return raw.encode("latin-1") if isinstance(raw, str) else raw
    if "application/json" in ctype or (isinstance(raw, str) and raw.lstrip().startswith("{")):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
    return raw


def result_download_filename(filename: Optional[str]) -> str:
    original = filename or "video.mp4"
    if "." in original:
        name, ext = original.rsplit(".", 1)
        return f"{name}_subtitled.{ext}"
    return f"{original}_subtitled.mp4"


def slugify(name: str) -> str:
    s = SLUG_RE.sub("-", (name or "").strip().lower()).strip("-")
    return s or "template"


# ---- DynamoDB jobs ----

def job_pk(job_id: str) -> str:
    return f"JOB#{job_id}"


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    resp = _ddb().get_item(
        TableName=JOBS_TABLE,
        Key={"PK": {"S": job_pk(job_id)}, "SK": {"S": "META"}},
    )
    item = resp.get("Item")
    if not item:
        return None
    plain = {k: _from_ddb(v) for k, v in item.items()}
    return {
        "job_id": plain.get("jobId"),
        "tenant_id": plain.get("tenantId", "default"),
        "status": plain.get("status"),
        "created_at": plain.get("createdAt"),
        "updated_at": plain.get("updatedAt"),
        "completed_at": plain.get("completedAt"),
        "filename": plain.get("filename"),
        "mode": plain.get("mode"),
        "progress": int(plain.get("progress") or 0),
        "error": plain.get("error") or "",
        "karaoke": bool(plain.get("karaoke", False)),
        "message": plain.get("message") or "",
        "style_template_slug": plain.get("style_template_slug"),
        "input_video_key": plain.get("input_video_key"),
        "result_video_key": plain.get("result_video_key"),
        "word_timings_key": plain.get("word_timings_key"),
        "srt_key": plain.get("srt_key"),
        "transcription_key": plain.get("transcription_key"),
        "source_language": plain.get("source_language"),
        "target_language": plain.get("target_language"),
        "window_size": plain.get("window_size"),
        "karaoke_layout": plain.get("karaoke_layout"),
        "has_word_timings": bool(plain.get("has_word_timings", False)),
    }


def put_job(job: Dict[str, Any]) -> None:
    job_id = job["job_id"]
    created_at = job.get("created_at") or _now_iso()
    status = job.get("status") or "uploaded"
    item = {
        "PK": {"S": job_pk(job_id)},
        "SK": {"S": "META"},
        "jobId": {"S": job_id},
        "tenantId": {"S": job.get("tenant_id") or "default"},
        "status": {"S": status},
        "statusKey": {"S": f"STATUS#{status}"},
        "createdAt": {"S": created_at},
        "createdSk": {"S": f"{created_at}#{job_id}"},
        "updatedAt": {"S": job.get("updated_at") or created_at},
        "ttl": {"N": str(job.get("ttl") or _ttl_epoch())},
        "progress": {"N": str(int(job.get("progress") or 0))},
        "karaoke": {"BOOL": bool(job.get("karaoke", False))},
        "has_word_timings": {"BOOL": bool(job.get("has_word_timings", False))},
    }
    skip = {
        "job_id",
        "tenant_id",
        "status",
        "created_at",
        "updated_at",
        "progress",
        "karaoke",
        "has_word_timings",
        "ttl",
        "completed_at",
        "video_data",
        "result_video",
        "srt_content",
        "word_timings",
        "transcription_result",
    }
    for k, v in job.items():
        if k in skip or v is None:
            continue
        item[k] = _to_attr(v)
    if job.get("completed_at"):
        item["completedAt"] = {"S": job["completed_at"]}
    _ddb().put_item(TableName=JOBS_TABLE, Item=item)


def update_job(job_id: str, **fields: Any) -> Dict[str, Any]:
    existing = get_job(job_id)
    if not existing:
        raise KeyError(job_id)
    existing.update(fields)
    existing["updated_at"] = _now_iso()
    if fields.get("status") in ("completed", "failed"):
        existing["completed_at"] = existing.get("completed_at") or _now_iso()
        existing["ttl"] = _ttl_epoch()
    put_job(existing)
    return existing


# ---- S3 helpers ----

def media_key(job_id: str, kind: str, filename: str) -> str:
    safe = filename.replace("/", "_")
    return f"jobs/{job_id}/{kind}/{safe}"


def s3_put_bytes(bucket: str, key: str, data: bytes, content_type: str) -> None:
    _s3().put_object(Bucket=bucket, Key=key, Body=data, ContentType=content_type)


def s3_get_bytes(bucket: str, key: str) -> bytes:
    return _s3().get_object(Bucket=bucket, Key=key)["Body"].read()


def s3_exists(bucket: str, key: str) -> bool:
    try:
        _s3().head_object(Bucket=bucket, Key=key)
        return True
    except ClientError:
        return False


def presign_put(key: str, content_type: str, expires: int = 3600) -> str:
    return _s3().generate_presigned_url(
        "put_object",
        Params={"Bucket": MEDIA_BUCKET, "Key": key, "ContentType": content_type},
        ExpiresIn=expires,
    )


def presign_get(
    key: str,
    expires: int = 3600,
    disposition: Optional[str] = None,
    content_type: Optional[str] = None,
) -> str:
    params: Dict[str, str] = {"Bucket": MEDIA_BUCKET, "Key": key}
    if disposition:
        params["ResponseContentDisposition"] = disposition
    if content_type:
        params["ResponseContentType"] = content_type
    return _s3().generate_presigned_url("get_object", Params=params, ExpiresIn=expires)


def list_keys(bucket: str, prefix: str) -> list:
    keys = []
    token = None
    while True:
        kwargs = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token
        resp = _s3().list_objects_v2(**kwargs)
        for obj in resp.get("Contents") or []:
            keys.append(obj["Key"])
        if not resp.get("IsTruncated"):
            break
        token = resp.get("NextContinuationToken")
    return keys


# ---- ECS ----

def run_worker(job_id: str, stage: str) -> str:
    family = BURN_TASK if stage in BURN_STAGES else TRANSCRIBE_TASK
    if not ECS_CLUSTER or not family:
        raise RuntimeError("ECS cluster / task family not configured")
    resp = _ecs().run_task(
        cluster=ECS_CLUSTER,
        taskDefinition=family,
        launchType="FARGATE",
        count=1,
        networkConfiguration={
            "awsvpcConfiguration": {
                "subnets": ECS_SUBNETS,
                "securityGroups": [ECS_SECURITY_GROUP] if ECS_SECURITY_GROUP else [],
                "assignPublicIp": "ENABLED",
            }
        },
        overrides={
            "containerOverrides": [
                {
                    "name": CONTAINER_NAME,
                    "command": [
                        "python",
                        "-m",
                        "worker",
                        "--job-id",
                        job_id,
                        "--stage",
                        stage,
                    ],
                }
            ]
        },
        startedBy=f"job-{job_id[:8]}-{stage}"[:36],
    )
    failures = resp.get("failures") or []
    if failures:
        raise RuntimeError(f"RunTask failed: {failures}")
    tasks = resp.get("tasks") or []
    arn = (tasks[0].get("taskArn") if tasks else "") or ""
    logger.info("Started %s task %s for job %s", stage, arn, job_id)
    return arn


def _truthy(value) -> Optional[bool]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    return str(value).lower() in ("1", "true", "yes", "on")


def parse_multipart_file(body, content_type: str) -> Tuple[str, bytes]:
    """Minimal single-file multipart parser (font uploads)."""
    match = re.search(r"boundary=([^;]+)", content_type)
    if not match:
        raise ValueError("multipart boundary missing")
    boundary = match.group(1).strip().strip('"')
    raw = body if isinstance(body, (bytes, bytearray)) else str(body).encode("latin-1")
    parts = raw.split(b"--" + boundary.encode("ascii"))
    for part in parts:
        if b"Content-Disposition" not in part:
            continue
        header, _, data = part.partition(b"\r\n\r\n")
        if not data:
            continue
        data = data.rstrip(b"\r\n")
        if data.endswith(b"--"):
            data = data[:-2]
        name_m = re.search(rb'filename="([^"]+)"', header)
        filename = name_m.group(1).decode("utf-8", errors="replace") if name_m else "font.ttf"
        return filename, bytes(data)
    raise ValueError("No file part in multipart body")


# ---- Routes ----

def handle(method: str, path: str, body: Any, headers: Dict[str, str], origin: str):
    if method == "OPTIONS":
        return respond(204, "", origin=origin)

    if path in ("/", "/health") and method == "GET":
        return respond(200, {"status": "ok", "storage": "dynamodb"}, origin=origin)

    if path == "/jobs/presign" and method == "POST":
        return handle_presign(body if isinstance(body, dict) else {}, origin)

    m = re.fullmatch(r"/process/([^/]+)", path)
    if m and method == "POST":
        return handle_process(m.group(1), body if isinstance(body, dict) else {}, origin)

    m = re.fullmatch(r"/status/([^/]+)", path)
    if m and method == "GET":
        return handle_status(m.group(1), origin)

    m = re.fullmatch(r"/transcription/([^/]+)", path)
    if m and method == "GET":
        return handle_get_transcription(m.group(1), origin)

    m = re.fullmatch(r"/transcription/([^/]+)/continue", path)
    if m and method == "POST":
        return handle_continue(m.group(1), body if isinstance(body, dict) else {}, origin)

    m = re.fullmatch(r"/download/word-timings/([^/]+)", path)
    if m and method == "GET":
        return handle_download_word_timings(m.group(1), origin)

    m = re.fullmatch(r"/download/([^/]+)", path)
    if m and method == "GET":
        return handle_download(m.group(1), origin)

    m = re.fullmatch(r"/word-timings/([^/]+)", path)
    if m and method == "GET":
        return handle_word_timings(m.group(1), origin)

    if path == "/styles/templates" and method == "GET":
        return handle_list_templates(origin)
    m = re.fullmatch(r"/styles/templates/([^/]+)", path)
    if m and method == "GET":
        return handle_get_template(m.group(1), origin)
    if path == "/styles/templates" and method == "POST":
        return handle_save_template(body if isinstance(body, dict) else {}, origin)

    if path == "/styles/fonts" and method == "GET":
        return handle_list_fonts(origin)
    if path == "/styles/fonts" and method == "POST":
        return handle_upload_font(body, headers, origin)

    if path == "/styles/stroke-previews" and method == "GET":
        return handle_list_stroke_previews(origin)
    m = re.fullmatch(r"/styles/stroke-previews/(\d+)", path)
    if m and method == "GET":
        return handle_get_stroke_preview(int(m.group(1)), origin)

    return respond(404, {"detail": "Not found"}, origin=origin)


def handle_presign(body: dict, origin: str):
    filename = str(body.get("filename") or "video.mp4")
    content_type = str(body.get("content_type") or "video/mp4")
    mode = str(body.get("mode") or "generate")
    if mode not in PROCESS_STAGE:
        return respond(400, {"detail": "Mode must be 'generate', 'burn', or 'burn_words'"}, origin=origin)
    job_id = str(uuid.uuid4())
    key = media_key(job_id, "input", filename)
    put_job(
        {
            "job_id": job_id,
            "status": "awaiting_upload",
            "filename": filename,
            "mode": mode,
            "input_video_key": key,
            "progress": 0,
            "karaoke": KARAOKE_DEFAULT,
            "created_at": _now_iso(),
        }
    )
    url = presign_put(key, content_type)
    return respond(
        200,
        {
            "job_id": job_id,
            "upload_url": url,
            "input_video_key": key,
            "use_s3": True,
        },
        origin=origin,
    )


def handle_process(job_id: str, body: dict, origin: str):
    job = get_job(job_id)
    if not job:
        return respond(404, {"detail": "Job not found"}, origin=origin)
    status = job.get("status")
    if status == "awaiting_upload":
        key = job.get("input_video_key")
        if not key or not s3_exists(MEDIA_BUCKET, key):
            return respond(400, {"detail": "Video upload not found"}, origin=origin)
        job["status"] = "uploaded"
    elif status != "uploaded":
        return respond(400, {"detail": "Job not ready for processing"}, origin=origin)

    mode = job.get("mode") or "generate"
    slug = body.get("style_template_slug")
    if slug:
        job["style_template_slug"] = str(slug).strip()

    karaoke_flag = _truthy(body.get("karaoke"))
    karaoke_enabled = KARAOKE_DEFAULT
    if mode == "burn_words":
        karaoke_enabled = True
    elif karaoke_flag is not None:
        karaoke_enabled = karaoke_flag
    job["karaoke"] = karaoke_enabled

    if body.get("target_language"):
        job["target_language"] = body["target_language"]
    if body.get("source_language"):
        job["source_language"] = body["source_language"]

    if body.get("srt_content"):
        srt_key = media_key(job_id, "meta", "input.srt")
        s3_put_bytes(MEDIA_BUCKET, srt_key, str(body["srt_content"]).encode("utf-8"), "application/x-subrip")
        job["srt_key"] = srt_key

    if body.get("word_timings") is not None:
        wt_key = media_key(job_id, "meta", "word_timings.json")
        payload = body["word_timings"]
        if isinstance(payload, list):
            payload = {"words": payload}
        s3_put_bytes(
            MEDIA_BUCKET,
            wt_key,
            json.dumps(payload).encode("utf-8"),
            "application/json",
        )
        job["word_timings_key"] = wt_key
        job["has_word_timings"] = True

    if mode == "burn_words" and not job.get("word_timings_key"):
        return respond(400, {"detail": "No word timings available for burn_words mode"}, origin=origin)
    if mode == "burn":
        if not job.get("srt_key") and not body.get("srt_content"):
            return respond(400, {"detail": "No SRT content available for burn mode"}, origin=origin)
        if not job.get("source_language"):
            return respond(400, {"detail": "source_language is required for burn mode"}, origin=origin)
        if not job.get("target_language"):
            return respond(400, {"detail": "target_language is required for burn mode"}, origin=origin)
    if mode == "generate" and not job.get("target_language"):
        return respond(400, {"detail": "target_language is required for generate mode"}, origin=origin)

    stage = PROCESS_STAGE[mode]
    job["status"] = "queued"
    job["progress"] = 5
    job["error"] = ""
    put_job(job)
    run_worker(job_id, stage)
    messages = {
        "generate": "Video processing started",
        "burn": "Burning subtitles into video",
        "burn_words": "Burning karaoke from word timings",
    }
    return respond(
        200,
        {
            "job_id": job_id,
            "status": "processing_started",
            "message": messages[mode],
            "mode": mode,
            "karaoke": karaoke_enabled,
        },
        origin=origin,
    )


def handle_status(job_id: str, origin: str):
    job = get_job(job_id)
    if not job:
        return respond(404, {"detail": "Job not found"}, origin=origin)
    return respond(
        200,
        {
            "job_id": job_id,
            "status": job["status"],
            "progress": job.get("progress") or 0,
            "message": job.get("error") or "",
            "filename": job.get("filename") or "",
            "karaoke": bool(job.get("karaoke", False)),
            "has_word_timings": bool(job.get("has_word_timings")),
        },
        origin=origin,
    )


def handle_get_transcription(job_id: str, origin: str):
    job = get_job(job_id)
    if not job:
        return respond(404, {"detail": "Job not found"}, origin=origin)
    if job.get("status") != "transcription_complete":
        return respond(400, {"detail": "Transcription not ready for review"}, origin=origin)
    key = job.get("transcription_key")
    if not key:
        return respond(404, {"detail": "Transcription not found"}, origin=origin)
    payload = json.loads(s3_get_bytes(MEDIA_BUCKET, key).decode("utf-8"))
    return respond(
        200,
        {
            "job_id": job_id,
            "transcription": payload,
            "source_language": job.get("source_language"),
            "target_language": job.get("target_language"),
            "filename": job.get("filename"),
        },
        origin=origin,
    )


def handle_continue(job_id: str, body: dict, origin: str):
    job = get_job(job_id)
    if not job:
        return respond(404, {"detail": "Job not found"}, origin=origin)
    if job.get("status") != "transcription_complete":
        return respond(400, {"detail": "Job not ready for transcription continuation"}, origin=origin)
    if not isinstance(body, dict) or not body.get("segments"):
        return respond(400, {"detail": "Edited transcription with segments is required"}, origin=origin)
    key = job.get("transcription_key") or media_key(job_id, "meta", "transcription.json")
    s3_put_bytes(MEDIA_BUCKET, key, json.dumps(body, ensure_ascii=False).encode("utf-8"), "application/json")
    update_job(job_id, transcription_key=key, status="queued_align", progress=65, error="")
    run_worker(job_id, "align_burn")
    return respond(
        200,
        {
            "job_id": job_id,
            "status": "processing_continued",
            "message": "Processing continued with edited transcription",
        },
        origin=origin,
    )


def handle_download(job_id: str, origin: str):
    job = get_job(job_id)
    if not job:
        return respond(404, {"detail": "Job not found"}, origin=origin)
    if job.get("status") != "completed":
        return respond(400, {"detail": "Job not completed yet"}, origin=origin)
    key = job.get("result_video_key")
    if not key:
        return respond(404, {"detail": "Processed video not found"}, origin=origin)
    filename = result_download_filename(job.get("filename"))
    url = presign_get(
        key,
        expires=DOWNLOAD_TTL,
        disposition=f'attachment; filename="{filename}"',
        content_type="video/mp4",
    )
    return respond(200, {"url": url, "expires_in": DOWNLOAD_TTL, "filename": filename}, origin=origin)


def handle_download_word_timings(job_id: str, origin: str):
    job = get_job(job_id)
    if not job:
        return respond(404, {"detail": "Job not found"}, origin=origin)
    if job.get("status") != "completed":
        return respond(400, {"detail": "Job not completed yet"}, origin=origin)
    key = job.get("word_timings_key")
    if not key:
        return respond(404, {"detail": "No word timings available for this job"}, origin=origin)
    payload = json.loads(s3_get_bytes(MEDIA_BUCKET, key).decode("utf-8"))
    words = payload.get("words", payload if isinstance(payload, list) else [])
    body = {
        "version": 1,
        "words": words,
        "cue_rules": payload.get("cue_rules") or {},
    }
    content = json.dumps(body, ensure_ascii=False, indent=2)
    filename = f"word_timings_{job_id}.json"
    return respond(
        200,
        content,
        origin=origin,
        headers={
            "content-type": "application/json",
            "content-disposition": f'attachment; filename="{filename}"',
        },
    )


def handle_word_timings(job_id: str, origin: str):
    job = get_job(job_id)
    if not job:
        return respond(404, {"detail": "Job not found"}, origin=origin)
    if job.get("status") != "completed":
        return respond(400, {"detail": "Word timings not ready yet"}, origin=origin)
    key = job.get("word_timings_key")
    words, cues, layout, cue_rules = [], [], job.get("karaoke_layout"), {}
    if key:
        payload = json.loads(s3_get_bytes(MEDIA_BUCKET, key).decode("utf-8"))
        if isinstance(payload, dict):
            words = payload.get("words") or []
            cues = payload.get("cues") or []
            layout = payload.get("layout") or layout
            cue_rules = payload.get("cue_rules") or {}
        elif isinstance(payload, list):
            words = payload
    return respond(
        200,
        {
            "job_id": job_id,
            "karaoke": bool(job.get("karaoke", False)),
            "word_count": len(words),
            "cue_count": len(cues),
            "words": words,
            "cues": cues,
            "cue_rules": cue_rules,
            "layout": layout,
        },
        origin=origin,
    )


def handle_list_templates(origin: str):
    templates = []
    for key in list_keys(STYLES_BUCKET, "templates/"):
        if not key.endswith(".json"):
            continue
        try:
            data = json.loads(s3_get_bytes(STYLES_BUCKET, key).decode("utf-8"))
            slug = data.get("slug") or slugify(data.get("name") or key)
            active = (data.get("activeText") or {}).get("color")
            templates.append(
                {
                    "name": data.get("name") or slug,
                    "slug": slug,
                    "fontS3Key": data.get("fontS3Key"),
                    "strokeWidth": data.get("strokeWidth"),
                    "activeTextColor": active,
                }
            )
        except Exception as e:
            logger.warning("Skip template %s: %s", key, e)
    templates.sort(key=lambda t: (t.get("name") or "").lower())
    return respond(200, {"templates": templates}, origin=origin)


def handle_get_template(slug: str, origin: str):
    slug = slugify(slug)
    key = f"templates/{slug}.json"
    try:
        data = json.loads(s3_get_bytes(STYLES_BUCKET, key).decode("utf-8"))
        return respond(200, data, origin=origin)
    except ClientError:
        return respond(404, {"detail": "Template not found"}, origin=origin)


def handle_save_template(body: dict, origin: str):
    name = (body.get("name") or "").strip()
    if not name:
        return respond(400, {"detail": "name is required"}, origin=origin)
    slug = slugify(body.get("slug") or name)
    body = {**body, "name": name, "slug": slug}
    key = f"templates/{slug}.json"
    s3_put_bytes(
        STYLES_BUCKET,
        key,
        json.dumps(body, indent=2, ensure_ascii=False).encode("utf-8"),
        "application/json",
    )
    return respond(200, body, origin=origin)


def handle_list_fonts(origin: str):
    fonts = []
    for key in list_keys(STYLES_BUCKET, "fonts/"):
        lower = key.lower()
        if not (lower.endswith(".ttf") or lower.endswith(".otf")):
            continue
        name = key.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        ctype = "font/otf" if lower.endswith(".otf") else "font/ttf"
        fonts.append({"name": name, "s3Key": key, "contentType": ctype})
    fonts.sort(key=lambda f: f["name"].lower())
    return respond(200, {"fonts": fonts}, origin=origin)


def handle_upload_font(body: Any, headers: Dict[str, str], origin: str):
    ctype = headers.get("content-type") or ""
    try:
        if "multipart/form-data" in ctype:
            filename, data = parse_multipart_file(body, ctype)
        elif isinstance(body, dict):
            filename = body.get("filename") or "font.ttf"
            raw_b64 = body.get("content_base64") or body.get("data") or ""
            data = base64.b64decode(raw_b64)
        else:
            return respond(400, {"detail": "Expected multipart font upload"}, origin=origin)
    except Exception as e:
        return respond(400, {"detail": str(e)}, origin=origin)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", filename)
    if not safe.lower().endswith((".ttf", ".otf")):
        return respond(400, {"detail": "Only .ttf and .otf fonts are supported"}, origin=origin)
    if not data:
        return respond(400, {"detail": "Empty font file"}, origin=origin)
    key = f"fonts/{safe}"
    font_ctype = "font/otf" if safe.lower().endswith(".otf") else "font/ttf"
    s3_put_bytes(STYLES_BUCKET, key, data, font_ctype)
    return respond(
        200,
        {"name": safe.rsplit(".", 1)[0], "s3Key": key, "contentType": font_ctype},
        origin=origin,
    )


def handle_list_stroke_previews(origin: str):
    items = []
    for i in range(STROKE_PREVIEW_COUNT):
        key = f"stroke-previews/stroke_{i:02d}.png"
        exists = s3_exists(STYLES_BUCKET, key)
        items.append(
            {
                "index": i,
                "strokeWidth": (i + 1) * 0.5,
                "url": f"/styles/stroke-previews/{i}" if exists else None,
            }
        )
    return respond(200, {"previews": items}, origin=origin)


def handle_get_stroke_preview(index: int, origin: str):
    if index < 0 or index >= STROKE_PREVIEW_COUNT:
        return respond(404, {"detail": "Preview not found"}, origin=origin)
    key = f"stroke-previews/stroke_{index:02d}.png"
    try:
        data = s3_get_bytes(STYLES_BUCKET, key)
    except ClientError:
        return respond(404, {"detail": "Preview not generated"}, origin=origin)
    return respond(
        200,
        base64.b64encode(data).decode("ascii"),
        origin=origin,
        headers={"content-type": "image/png"},
        binary=True,
    )


def handler(event, context):
    origin = event_headers(event).get("origin") or ""
    method = event_method(event)
    path = event_path(event)
    headers = event_headers(event)
    query = event_query(event)
    logger.info("%s %s", method, path)

    if method == "OPTIONS" or is_public_request(method, path):
        try:
            return handle(method, path, event_body(event), headers, origin)
        except Exception as e:
            logger.exception("Public handler failed")
            return respond(500, {"detail": str(e)}, origin=origin)

    try:
        expected = expected_api_key()
    except Exception as e:
        logger.exception("Could not load API key")
        return respond(500, {"detail": "Auth not configured"}, origin=origin)

    provided = extract_api_key(headers, query)
    if not api_key_accepted(provided, expected):
        return respond(401, {"detail": "Invalid or missing API key"}, origin=origin)

    try:
        return handle(method, path, event_body(event), headers, origin)
    except KeyError:
        return respond(404, {"detail": "Job not found"}, origin=origin)
    except Exception as e:
        logger.exception("Handler failed")
        return respond(500, {"detail": str(e)}, origin=origin)
