"""Job persistence: in-memory (local/tests) or DynamoDB (AWS)."""
from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

TTL_DAYS = 7


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ttl_epoch(days: int = TTL_DAYS) -> int:
    return int((datetime.now(timezone.utc) + timedelta(days=days)).timestamp())


class JobStore(ABC):
    @abstractmethod
    def create(self, job_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        ...

    @abstractmethod
    def get(self, job_id: str) -> Optional[Dict[str, Any]]:
        ...

    @abstractmethod
    def update(self, job_id: str, **fields: Any) -> Dict[str, Any]:
        ...

    @abstractmethod
    def delete(self, job_id: str) -> None:
        ...

    @abstractmethod
    def list_by_tenant(self, tenant_id: str = "default", limit: int = 50) -> List[Dict[str, Any]]:
        ...

    @abstractmethod
    def list_by_status(self, status: str, limit: int = 50) -> List[Dict[str, Any]]:
        ...


class MemoryJobStore(JobStore):
    """Process-local store for tests and local dev without AWS."""

    def __init__(self):
        self._jobs: Dict[str, Dict[str, Any]] = {}

    def create(self, job_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        created = _now_iso()
        job = {
            **data,
            "job_id": job_id,
            "tenant_id": data.get("tenant_id", "default"),
            "status": data.get("status", "uploaded"),
            "created_at": created,
            "updated_at": created,
            "progress": data.get("progress", 0),
        }
        self._jobs[job_id] = job
        return job

    def get(self, job_id: str) -> Optional[Dict[str, Any]]:
        return self._jobs.get(job_id)

    def update(self, job_id: str, **fields: Any) -> Dict[str, Any]:
        job = self._jobs.get(job_id)
        if not job:
            raise KeyError(job_id)
        job.update(fields)
        job["updated_at"] = _now_iso()
        if fields.get("status") in ("completed", "failed"):
            job["completed_at"] = job.get("completed_at") or _now_iso()
        return job

    def delete(self, job_id: str) -> None:
        self._jobs.pop(job_id, None)

    def list_by_tenant(self, tenant_id: str = "default", limit: int = 50) -> List[Dict[str, Any]]:
        items = [j for j in self._jobs.values() if j.get("tenant_id") == tenant_id]
        items.sort(key=lambda j: j.get("created_at", ""), reverse=True)
        return items[:limit]

    def list_by_status(self, status: str, limit: int = 50) -> List[Dict[str, Any]]:
        items = [j for j in self._jobs.values() if j.get("status") == status]
        items.sort(key=lambda j: j.get("created_at", ""), reverse=True)
        return items[:limit]


class DynamoJobStore(JobStore):
    """
    Table keys:
      PK = JOB#<job_id>, SK = META
    GSI StatusByCreated: statusKey / createdSk
    GSI TenantByCreated: tenantId / createdSk
    """

    def __init__(self, table_name: str, region: str = "eu-central-1"):
        import boto3

        self.table_name = table_name
        self.ddb = boto3.resource("dynamodb", region_name=region)
        self.table = self.ddb.Table(table_name)

    @staticmethod
    def _pk(job_id: str) -> str:
        return f"JOB#{job_id}"

    @staticmethod
    def _status_key(status: str) -> str:
        return f"STATUS#{status}"

    @staticmethod
    def _created_sk(created_at: str, job_id: str) -> str:
        return f"{created_at}#{job_id}"

    def _to_item(self, job: Dict[str, Any]) -> Dict[str, Any]:
        job_id = job["job_id"]
        created_at = job.get("created_at") or _now_iso()
        status = job.get("status") or "uploaded"
        tenant_id = job.get("tenant_id") or "default"
        # Strip large in-memory blobs — only S3 keys belong in DDB
        skip = {"video_data", "result_video", "srt_content", "word_timings"}
        item = {
            "PK": self._pk(job_id),
            "SK": "META",
            "jobId": job_id,
            "tenantId": tenant_id,
            "status": status,
            "statusKey": self._status_key(status),
            "createdAt": created_at,
            "createdSk": self._created_sk(created_at, job_id),
            "updatedAt": job.get("updated_at") or created_at,
            "ttl": job.get("ttl") or _ttl_epoch(),
        }
        for k, v in job.items():
            if k in skip or v is None:
                continue
            if k in ("job_id", "tenant_id", "status", "created_at", "updated_at"):
                continue
            # DynamoDB-friendly keys (camelCase for GSI attrs already set)
            item[k] = v
        if job.get("completed_at"):
            item["completedAt"] = job["completed_at"]
        return item

    def _from_item(self, item: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "job_id": item.get("jobId"),
            "tenant_id": item.get("tenantId", "default"),
            "status": item.get("status"),
            "created_at": item.get("createdAt"),
            "updated_at": item.get("updatedAt"),
            "completed_at": item.get("completedAt"),
            "filename": item.get("filename"),
            "mode": item.get("mode"),
            "progress": int(item.get("progress") or 0),
            "error": item.get("error") or "",
            "karaoke": bool(item.get("karaoke", False)),
            "message": item.get("message") or "",
            "style_template_slug": item.get("style_template_slug"),
            "input_video_key": item.get("input_video_key"),
            "result_video_key": item.get("result_video_key"),
            "word_timings_key": item.get("word_timings_key"),
            "srt_key": item.get("srt_key"),
            "source_language": item.get("source_language"),
            "target_language": item.get("target_language"),
            "window_size": item.get("window_size"),
            "karaoke_layout": item.get("karaoke_layout"),
            "has_word_timings": bool(item.get("has_word_timings", False)),
        }

    def create(self, job_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        created = _now_iso()
        job = {
            **data,
            "job_id": job_id,
            "tenant_id": data.get("tenant_id", "default"),
            "status": data.get("status", "uploaded"),
            "created_at": created,
            "updated_at": created,
            "progress": data.get("progress", 0),
            "ttl": _ttl_epoch(),
        }
        self.table.put_item(Item=self._to_item(job))
        return job

    def get(self, job_id: str) -> Optional[Dict[str, Any]]:
        resp = self.table.get_item(Key={"PK": self._pk(job_id), "SK": "META"})
        item = resp.get("Item")
        if not item:
            return None
        return self._from_item(item)

    def update(self, job_id: str, **fields: Any) -> Dict[str, Any]:
        existing = self.get(job_id)
        if not existing:
            raise KeyError(job_id)
        existing.update(fields)
        existing["updated_at"] = _now_iso()
        if fields.get("status") in ("completed", "failed"):
            existing["completed_at"] = existing.get("completed_at") or _now_iso()
            existing["ttl"] = _ttl_epoch()
        # statusKey must refresh when status changes — full put keeps GSIs consistent
        # Merge with any raw keys still needed
        raw = dict(existing)
        raw["ttl"] = existing.get("ttl") or _ttl_epoch()
        self.table.put_item(Item=self._to_item(raw))
        return existing

    def delete(self, job_id: str) -> None:
        self.table.delete_item(Key={"PK": self._pk(job_id), "SK": "META"})

    def list_by_tenant(self, tenant_id: str = "default", limit: int = 50) -> List[Dict[str, Any]]:
        from boto3.dynamodb.conditions import Key

        resp = self.table.query(
            IndexName="TenantByCreated",
            KeyConditionExpression=Key("tenantId").eq(tenant_id),
            ScanIndexForward=False,
            Limit=limit,
        )
        return [self._from_item(i) for i in resp.get("Items", [])]

    def list_by_status(self, status: str, limit: int = 50) -> List[Dict[str, Any]]:
        from boto3.dynamodb.conditions import Key

        resp = self.table.query(
            IndexName="StatusByCreated",
            KeyConditionExpression=Key("statusKey").eq(self._status_key(status)),
            ScanIndexForward=False,
            Limit=limit,
        )
        return [self._from_item(i) for i in resp.get("Items", [])]


def build_job_store(settings) -> JobStore:
    table = getattr(settings, "jobs_table_name", "") or ""
    if table:
        logger.info("Using DynamoDB job store: %s", table)
        return DynamoJobStore(table, region=getattr(settings, "aws_region", "eu-central-1"))
    logger.info("Using in-memory job store")
    return MemoryJobStore()
