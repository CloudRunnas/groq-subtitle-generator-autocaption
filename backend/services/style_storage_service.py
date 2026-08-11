"""Store style templates and fonts on S3, with local filesystem fallback."""
from __future__ import annotations

import json
import logging
import re
import shutil
from pathlib import Path
from typing import List, Optional, Tuple

from models.style_template import FontAsset, StyleTemplate, StyleTemplateSummary

logger = logging.getLogger(__name__)

SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    s = SLUG_RE.sub("-", (name or "").strip().lower()).strip("-")
    return s or "template"


class StyleStorageService:
    def __init__(self, settings):
        self.settings = settings
        self.mode = (getattr(settings, "style_storage", None) or "local").lower()
        self.bucket = getattr(settings, "aws_s3_bucket", None) or ""
        self.region = getattr(settings, "aws_region", None) or "eu-central-1"
        self.local_root = Path(getattr(settings, "style_local_dir", None) or "data/styles")
        if not self.local_root.is_absolute():
            # relative to backend/
            self.local_root = Path(__file__).resolve().parent.parent / self.local_root
        self.local_root.mkdir(parents=True, exist_ok=True)
        (self.local_root / "templates").mkdir(exist_ok=True)
        (self.local_root / "fonts").mkdir(exist_ok=True)
        (self.local_root / "stroke-previews").mkdir(exist_ok=True)
        (self.local_root / "cache" / "fonts").mkdir(parents=True, exist_ok=True)
        self._s3 = None
        if self.mode == "s3" and self.bucket:
            try:
                import boto3
                self._s3 = boto3.client("s3", region_name=self.region)
                logger.info("Style storage: S3 bucket=%s region=%s", self.bucket, self.region)
            except Exception as e:
                logger.warning("S3 unavailable (%s); falling back to local style storage", e)
                self.mode = "local"
                self._s3 = None
        else:
            logger.info("Style storage: local dir=%s", self.local_root)

    # ---- templates ----

    def list_templates(self) -> List[StyleTemplateSummary]:
        templates = self._load_all_templates()
        return [
            StyleTemplateSummary(
                name=t.name,
                slug=t.slug or slugify(t.name),
                fontS3Key=t.fontS3Key,
                strokeWidth=t.strokeWidth,
                activeTextColor=t.activeText.color,
            )
            for t in templates
        ]

    def get_template(self, slug: str) -> Optional[StyleTemplate]:
        slug = slugify(slug)
        for t in self._load_all_templates():
            if (t.slug or slugify(t.name)) == slug:
                return t
        return None

    def save_template(self, template: StyleTemplate) -> StyleTemplate:
        template.slug = slugify(template.slug or template.name)
        payload = template.model_dump(mode="json")
        key = f"templates/{template.slug}.json"
        body = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
        self._put_bytes(key, body, "application/json")
        # always mirror locally for cache / offline
        local = self.local_root / key
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(body)
        return template

    def _load_all_templates(self) -> List[StyleTemplate]:
        items: List[StyleTemplate] = []
        keys = self._list_keys("templates/")
        for key in keys:
            if not key.endswith(".json"):
                continue
            try:
                raw = self._get_bytes(key)
                data = json.loads(raw.decode("utf-8"))
                t = StyleTemplate.model_validate(data)
                if not t.slug:
                    t.slug = Path(key).stem
                items.append(t)
            except Exception as e:
                logger.warning("Skip template %s: %s", key, e)
        # also scan local if S3 empty
        if not items:
            for path in sorted((self.local_root / "templates").glob("*.json")):
                try:
                    t = StyleTemplate.model_validate_json(path.read_text(encoding="utf-8"))
                    if not t.slug:
                        t.slug = path.stem
                    items.append(t)
                except Exception as e:
                    logger.warning("Skip local template %s: %s", path, e)
        items.sort(key=lambda t: (t.name or "").lower())
        return items

    # ---- fonts ----

    def list_fonts(self) -> List[FontAsset]:
        fonts: List[FontAsset] = []
        for key in self._list_keys("fonts/"):
            lower = key.lower()
            if not (lower.endswith(".ttf") or lower.endswith(".otf")):
                continue
            name = Path(key).stem
            ctype = "font/otf" if lower.endswith(".otf") else "font/ttf"
            fonts.append(FontAsset(name=name, s3Key=key, contentType=ctype))
        if not fonts:
            for path in sorted((self.local_root / "fonts").glob("*")):
                if path.suffix.lower() not in (".ttf", ".otf"):
                    continue
                fonts.append(FontAsset(
                    name=path.stem,
                    s3Key=f"fonts/{path.name}",
                    contentType="font/otf" if path.suffix.lower() == ".otf" else "font/ttf",
                ))
        fonts.sort(key=lambda f: f.name.lower())
        return fonts

    def upload_font(self, filename: str, data: bytes) -> FontAsset:
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", filename)
        if not safe.lower().endswith((".ttf", ".otf")):
            raise ValueError("Font must be .ttf or .otf")
        key = f"fonts/{safe}"
        ctype = "font/otf" if safe.lower().endswith(".otf") else "font/ttf"
        self._put_bytes(key, data, ctype)
        local = self.local_root / key
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(data)
        return FontAsset(name=Path(safe).stem, s3Key=key, contentType=ctype)

    def resolve_font_file(self, font_s3_key: Optional[str]) -> Optional[Path]:
        """Download/cache font and return local path for FFmpeg fontsdir."""
        if not font_s3_key:
            return None
        name = Path(font_s3_key).name
        cached = self.local_root / "cache" / "fonts" / name
        if cached.exists():
            return cached
        local_copy = self.local_root / font_s3_key
        if local_copy.exists():
            cached.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(local_copy, cached)
            return cached
        try:
            data = self._get_bytes(font_s3_key)
            cached.parent.mkdir(parents=True, exist_ok=True)
            cached.write_bytes(data)
            return cached
        except Exception as e:
            logger.warning("Could not resolve font %s: %s", font_s3_key, e)
            return None

    def fonts_dir_for_template(self, template: Optional[StyleTemplate]) -> Optional[str]:
        if not template or not template.fontS3Key:
            return None
        path = self.resolve_font_file(template.fontS3Key)
        return str(path.parent) if path else None

    # ---- stroke previews (ASS-generated PNGs cached locally / S3) ----

    def stroke_preview_path(self, width_index: int) -> Path:
        return self.local_root / "stroke-previews" / f"stroke_{width_index:02d}.png"

    def put_stroke_preview(self, width_index: int, png_bytes: bytes) -> str:
        key = f"stroke-previews/stroke_{width_index:02d}.png"
        self._put_bytes(key, png_bytes, "image/png")
        path = self.local_root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(png_bytes)
        return key

    # ---- low-level IO ----

    def _list_keys(self, prefix: str) -> List[str]:
        keys: List[str] = []
        if self._s3 and self.bucket:
            try:
                token = None
                while True:
                    kwargs = {"Bucket": self.bucket, "Prefix": prefix}
                    if token:
                        kwargs["ContinuationToken"] = token
                    resp = self._s3.list_objects_v2(**kwargs)
                    for obj in resp.get("Contents") or []:
                        keys.append(obj["Key"])
                    if not resp.get("IsTruncated"):
                        break
                    token = resp.get("NextContinuationToken")
            except Exception as e:
                logger.warning("S3 list failed: %s", e)
        # merge local
        local_dir = self.local_root / prefix.rstrip("/")
        if local_dir.exists():
            for path in local_dir.rglob("*"):
                if path.is_file():
                    rel = path.relative_to(self.local_root).as_posix()
                    if rel not in keys:
                        keys.append(rel)
        return sorted(set(keys))

    def _get_bytes(self, key: str) -> bytes:
        local = self.local_root / key
        if local.exists():
            return local.read_bytes()
        if self._s3 and self.bucket:
            resp = self._s3.get_object(Bucket=self.bucket, Key=key)
            return resp["Body"].read()
        raise FileNotFoundError(key)

    def _put_bytes(self, key: str, data: bytes, content_type: str) -> None:
        local = self.local_root / key
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(data)
        if self._s3 and self.bucket:
            try:
                self._s3.put_object(
                    Bucket=self.bucket,
                    Key=key,
                    Body=data,
                    ContentType=content_type,
                )
            except Exception as e:
                logger.warning("S3 put %s failed (kept local): %s", key, e)
