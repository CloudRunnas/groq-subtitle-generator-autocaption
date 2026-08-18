"""Lambda control-plane routing and auth (no AWS)."""
import json
import os
import sys
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError

CONTROL = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "infra", "lambda", "control_plane")
)
sys.path.insert(0, CONTROL)

os.environ.setdefault("JOBS_TABLE_NAME", "AutocaptionJobs")
os.environ.setdefault("MEDIA_BUCKET", "media-test")
os.environ.setdefault("STYLES_BUCKET", "styles-test")
os.environ.setdefault("API_KEY_SECRET_NAME", "autocaption/backend-api-key")
os.environ.setdefault("ECS_CLUSTER", "cluster")
os.environ.setdefault("TRANSCRIBE_TASK_FAMILY", "transcribe-family")
os.environ.setdefault("BURN_TASK_FAMILY", "burn-family")
os.environ.setdefault("ECS_SUBNETS", "subnet-1,subnet-2")
os.environ.setdefault("ECS_SECURITY_GROUP", "sg-1")

import index as cp  # noqa: E402


def _event(method, path, *, body=None, headers=None, origin="https://d111.cloudfront.net"):
    hdrs = {"origin": origin, "content-type": "application/json"}
    if headers:
        hdrs.update(headers)
    encoded = None
    if body is not None and not isinstance(body, str):
        encoded = json.dumps(body)
    else:
        encoded = body
    return {
        "version": "2.0",
        "rawPath": path,
        "headers": hdrs,
        "queryStringParameters": {},
        "requestContext": {"http": {"method": method, "path": path}},
        "body": encoded,
        "isBase64Encoded": False,
    }


def test_health_is_public():
    resp = cp.handler(_event("GET", "/health"), None)
    assert resp["statusCode"] == 200
    payload = json.loads(resp["body"])
    assert payload["status"] == "ok"
    acao = resp["headers"]["access-control-allow-origin"]
    assert "," not in acao


def test_warmup_removed():
    with patch.object(cp, "expected_api_key", return_value="k"):
        resp = cp.handler(_event("GET", "/warmup"), None)
    assert resp["statusCode"] in {401, 404}


def test_process_without_key_is_401():
    with patch.object(cp, "expected_api_key", return_value="secret-key-value-32chars______"):
        resp = cp.handler(_event("POST", "/process/abc", body={"target_language": "de"}), None)
    assert resp["statusCode"] == 401
    payload = json.loads(resp["body"])
    assert "API key" in payload["detail"]


def test_secretsmanager_access_denied_is_500_not_401():
    cp._api_key_cache["value"] = None
    cp._api_key_cache["expires"] = 0
    err = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "not allowed"}},
        "GetSecretValue",
    )
    sm = MagicMock()
    sm.get_secret_value.side_effect = err
    with patch.object(cp, "_sm", return_value=sm):
        resp = cp.handler(_event("POST", "/jobs/presign", body={"filename": "clip.mp4"}), None)
    assert resp["statusCode"] == 500
    assert json.loads(resp["body"])["detail"] == "Auth not configured"


def test_presign_starts_job_and_returns_put_url():
    with patch.object(cp, "expected_api_key", return_value="k"), \
         patch.object(cp, "put_job") as put, \
         patch.object(cp, "presign_put", return_value="https://s3.example/put"):
        resp = cp.handler(
            _event(
                "POST",
                "/jobs/presign",
                body={"filename": "clip.mp4", "content_type": "video/mp4", "mode": "generate"},
                headers={"x-api-key": "k"},
            ),
            None,
        )
    assert resp["statusCode"] == 200
    payload = json.loads(resp["body"])
    assert payload["use_s3"] is True
    assert payload["upload_url"] == "https://s3.example/put"
    assert payload["job_id"]
    put.assert_called_once()
    created = put.call_args[0][0]
    assert created["status"] == "awaiting_upload"
    assert created["mode"] == "generate"


def test_process_generate_runs_transcribe_task():
    job = {
        "job_id": "j1",
        "status": "awaiting_upload",
        "mode": "generate",
        "input_video_key": "jobs/j1/input/clip.mp4",
        "filename": "clip.mp4",
        "karaoke": True,
    }
    with patch.object(cp, "expected_api_key", return_value="k"), \
         patch.object(cp, "get_job", return_value=job), \
         patch.object(cp, "s3_exists", return_value=True), \
         patch.object(cp, "put_job") as put, \
         patch.object(cp, "run_worker", return_value="arn:task") as run:
        resp = cp.handler(
            _event(
                "POST",
                "/process/j1",
                body={"target_language": "de", "karaoke": True},
                headers={"x-api-key": "k"},
            ),
            None,
        )
    assert resp["statusCode"] == 200
    payload = json.loads(resp["body"])
    assert payload["status"] == "processing_started"
    run.assert_called_once_with("j1", "transcribe")
    saved = put.call_args[0][0]
    assert saved["status"] == "queued"
    assert saved["target_language"] == "de"


def test_continue_runs_align_burn_task():
    job = {
        "job_id": "j1",
        "status": "transcription_complete",
        "transcription_key": "jobs/j1/meta/transcription.json",
        "mode": "generate",
    }
    body = {"text": "hi", "segments": [{"start": 0, "end": 1, "text": "hi"}]}
    with patch.object(cp, "expected_api_key", return_value="k"), \
         patch.object(cp, "get_job", return_value=job), \
         patch.object(cp, "s3_put_bytes") as put_s3, \
         patch.object(cp, "update_job") as upd, \
         patch.object(cp, "run_worker", return_value="arn") as run:
        resp = cp.handler(
            _event("POST", "/transcription/j1/continue", body=body, headers={"x-api-key": "k"}),
            None,
        )
    assert resp["statusCode"] == 200
    run.assert_called_once_with("j1", "align_burn")
    put_s3.assert_called_once()
    upd.assert_called()


def test_download_returns_presign_json():
    job = {
        "job_id": "j1",
        "status": "completed",
        "result_video_key": "jobs/j1/output/result.mp4",
        "filename": "clip.mp4",
    }
    with patch.object(cp, "expected_api_key", return_value="k"), \
         patch.object(cp, "get_job", return_value=job), \
         patch.object(cp, "presign_get", return_value="https://s3.example/get") as presign:
        resp = cp.handler(_event("GET", "/download/j1", headers={"x-api-key": "k"}), None)
    assert resp["statusCode"] == 200
    payload = json.loads(resp["body"])
    assert payload["url"] == "https://s3.example/get"
    assert payload["filename"] == "clip_subtitled.mp4"
    presign.assert_called_once()


def test_run_worker_uses_transcribe_family_and_public_ip():
    ecs = MagicMock()
    ecs.run_task.return_value = {"tasks": [{"taskArn": "arn"}], "failures": []}
    with patch.object(cp, "_ecs", return_value=ecs):
        arn = cp.run_worker("job-1", "transcribe")
    assert arn == "arn"
    kwargs = ecs.run_task.call_args.kwargs
    assert kwargs["taskDefinition"] == "transcribe-family"
    assert kwargs["networkConfiguration"]["awsvpcConfiguration"]["assignPublicIp"] == "ENABLED"
    cmd = kwargs["overrides"]["containerOverrides"][0]["command"]
    assert cmd == ["python", "-m", "worker", "--job-id", "job-1", "--stage", "transcribe"]


def test_run_worker_align_burn_uses_burn_family():
    ecs = MagicMock()
    ecs.run_task.return_value = {"tasks": [{"taskArn": "arn"}], "failures": []}
    with patch.object(cp, "_ecs", return_value=ecs):
        cp.run_worker("job-1", "align_burn")
    assert ecs.run_task.call_args.kwargs["taskDefinition"] == "burn-family"


def test_templates_get_is_public():
    with patch.object(cp, "list_keys", return_value=[]), \
         patch.object(cp, "s3_get_bytes"):
        resp = cp.handler(_event("GET", "/styles/templates"), None)
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["templates"] == []


def test_origin_secret_rejects_direct_function_url():
    with patch.dict(os.environ, {"ORIGIN_SECRET": "cf-origin-token-value-32chars____"}):
        resp = cp.handler(_event("GET", "/health"), None)
    assert resp["statusCode"] == 403
    assert json.loads(resp["body"])["detail"] == "Forbidden"


def test_origin_secret_allows_cloudfront_header():
    token = "cf-origin-token-value-32chars____"
    with patch.dict(os.environ, {"ORIGIN_SECRET": token}):
        resp = cp.handler(
            _event("GET", "/health", headers={"x-autocaption-origin": token}),
            None,
        )
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["status"] == "ok"


def test_origin_secret_blocks_before_api_key():
    token = "cf-origin-token-value-32chars____"
    with patch.dict(os.environ, {"ORIGIN_SECRET": token}), \
         patch.object(cp, "expected_api_key", return_value="k"):
        resp = cp.handler(
            _event("POST", "/jobs/presign", body={"filename": "clip.mp4"}, headers={"x-api-key": "k"}),
            None,
        )
    assert resp["statusCode"] == 403


def test_origin_secret_then_api_key_still_required():
    token = "cf-origin-token-value-32chars____"
    with patch.dict(os.environ, {"ORIGIN_SECRET": token}), \
         patch.object(cp, "expected_api_key", return_value="secret-key-value-32chars______"):
        resp = cp.handler(
            _event(
                "POST",
                "/jobs/presign",
                body={"filename": "clip.mp4", "content_type": "video/mp4"},
                headers={"x-autocaption-origin": token},
            ),
            None,
        )
    assert resp["statusCode"] == 401
    assert "API key" in json.loads(resp["body"])["detail"]
