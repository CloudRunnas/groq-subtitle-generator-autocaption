"""S3 / local media storage for videos and job artifacts."""
from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


def result_download_filename(filename: Optional[str]) -> str:
    original = filename or "video.mp4"
    if "." in original:
        name, ext = original.rsplit(".", 1)
        return f"{name}_subtitled.{ext}"
    return f"{original}_subtitled.mp4"


class MediaStore:
    def __init__(self, settings):
        self.settings = settings
        self.bucket = getattr(settings, "media_bucket", "") or ""
        self.region = getattr(settings, "aws_region", "eu-central-1")
        self.local_root = Path(getattr(settings, "media_local_dir", "data/media"))
        if not self.local_root.is_absolute():
            self.local_root = Path(__file__).resolve().parent.parent / self.local_root
        self.local_root.mkdir(parents=True, exist_ok=True)
        self._s3 = None
        if self.bucket:
            try:
                import boto3

                self._s3 = boto3.client("s3", region_name=self.region)
            except Exception as e:
                logger.warning("S3 media client failed: %s", e)

    @property
    def use_s3(self) -> bool:
        return bool(self._s3 and self.bucket)

    def new_key(self, job_id: str, kind: str, filename: str = "video.mp4") -> str:
        safe = filename.replace("/", "_")
        return f"jobs/{job_id}/{kind}/{safe}"

    def put_bytes(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        if self.use_s3:
            self._s3.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)
            return key
        path = self.local_root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return key

    def get_bytes(self, key: str) -> bytes:
        if self.use_s3:
            resp = self._s3.get_object(Bucket=self.bucket, Key=key)
            return resp["Body"].read()
        path = self.local_root / key
        return path.read_bytes()

    def exists(self, key: str) -> bool:
        if self.use_s3:
            try:
                self._s3.head_object(Bucket=self.bucket, Key=key)
                return True
            except Exception:
                return False
        return (self.local_root / key).exists()

    def presign_put(self, key: str, content_type: str = "video/mp4", expires: int = 3600) -> Optional[str]:
        if not self.use_s3:
            return None
        return self._s3.generate_presigned_url(
            "put_object",
            Params={"Bucket": self.bucket, "Key": key, "ContentType": content_type},
            ExpiresIn=expires,
        )

    def presign_get(
        self,
        key: str,
        expires: int = 3600,
        response_content_disposition: Optional[str] = None,
        response_content_type: Optional[str] = None,
    ) -> Optional[str]:
        if not self.use_s3:
            return None
        params = {"Bucket": self.bucket, "Key": key}
        if response_content_disposition:
            params["ResponseContentDisposition"] = response_content_disposition
        if response_content_type:
            params["ResponseContentType"] = response_content_type
        return self._s3.generate_presigned_url(
            "get_object",
            Params=params,
            ExpiresIn=expires,
        )

    def local_path(self, key: str) -> Path:
        return self.local_root / key
