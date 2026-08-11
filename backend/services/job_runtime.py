"""Runtime job map backed by JobStore + optional in-process blobs / MediaStore."""
from __future__ import annotations

import logging
from typing import Any, Dict, Iterator, Optional

from services.job_store import JobStore
from services.media_store import MediaStore

logger = logging.getLogger(__name__)

BLOB_KEYS = {
    "video_data",
    "result_video",
    "word_timings",
    "srt_content",
    "transcription_result",
}


class MutableJob(dict):
    def __init__(self, runtime: "JobRuntime", job_id: str, data: Dict[str, Any]):
        super().__init__(data)
        self._runtime = runtime
        self._job_id = job_id

    def __setitem__(self, key: str, value: Any) -> None:
        super().__setitem__(key, value)
        self._runtime._on_set(self._job_id, key, value, dict(self))

    def __delitem__(self, key: str) -> None:
        super().__delitem__(key)
        self._runtime._on_del(self._job_id, key)


class JobRuntime:
    """Dict-like facade compatible with legacy active_jobs usage."""

    def __init__(self, store: JobStore, media: MediaStore):
        self.store = store
        self.media = media
        self._blobs: Dict[str, Dict[str, Any]] = {}

    def __contains__(self, job_id: object) -> bool:
        if not isinstance(job_id, str):
            return False
        return self.store.get(job_id) is not None or job_id in self._blobs

    def __getitem__(self, job_id: str) -> MutableJob:
        meta = self.store.get(job_id)
        if meta is None and job_id not in self._blobs:
            raise KeyError(job_id)
        data = dict(meta or {"job_id": job_id})
        data.update(self._blobs.get(job_id, {}))
        # Hydrate video bytes from media if only key present
        if "video_data" not in data and data.get("input_video_key"):
            try:
                data["video_data"] = self.media.get_bytes(data["input_video_key"])
            except Exception as e:
                logger.warning("Could not load input video for %s: %s", job_id, e)
        if "result_video" not in data and data.get("result_video_key"):
            try:
                data["result_video"] = self.media.get_bytes(data["result_video_key"])
            except Exception as e:
                logger.warning("Could not load result video for %s: %s", job_id, e)
        if "word_timings" not in data and data.get("word_timings_key"):
            try:
                import json
                raw = self.media.get_bytes(data["word_timings_key"])
                payload = json.loads(raw.decode("utf-8"))
                data["word_timings"] = payload.get("words", payload if isinstance(payload, list) else [])
            except Exception as e:
                logger.warning("Could not load word timings for %s: %s", job_id, e)
        return MutableJob(self, job_id, data)

    def __setitem__(self, job_id: str, value: Dict[str, Any]) -> None:
        blobs = {k: value[k] for k in BLOB_KEYS if k in value}
        if blobs:
            self._blobs[job_id] = {**self._blobs.get(job_id, {}), **blobs}
            if "video_data" in blobs:
                key = value.get("input_video_key") or self.media.new_key(
                    job_id, "input", value.get("filename") or "video.mp4"
                )
                self.media.put_bytes(key, blobs["video_data"], "video/mp4")
                value = {**value, "input_video_key": key}
            if "result_video" in blobs:
                key = value.get("result_video_key") or self.media.new_key(job_id, "output", "result.mp4")
                self.media.put_bytes(key, blobs["result_video"], "video/mp4")
                value = {**value, "result_video_key": key}
            if "word_timings" in blobs and blobs["word_timings"] is not None:
                import json
                key = value.get("word_timings_key") or self.media.new_key(job_id, "meta", "word_timings.json")
                payload = json.dumps({"words": blobs["word_timings"]}).encode("utf-8")
                self.media.put_bytes(key, payload, "application/json")
                value = {**value, "word_timings_key": key, "has_word_timings": bool(blobs["word_timings"])}

        meta = {k: v for k, v in value.items() if k not in BLOB_KEYS}
        meta["job_id"] = job_id
        if self.store.get(job_id) is None:
            self.store.create(job_id, meta)
        else:
            self.store.update(job_id, **meta)

    def get(self, job_id: str, default=None):
        try:
            return self[job_id]
        except KeyError:
            return default

    def pop(self, job_id: str, default=None):
        try:
            job = dict(self[job_id])
        except KeyError:
            return default
        self._blobs.pop(job_id, None)
        try:
            self.store.delete(job_id)
        except Exception:
            pass
        return job

    def __delitem__(self, job_id: str) -> None:
        if self.pop(job_id, None) is None:
            raise KeyError(job_id)

    def _on_set(self, job_id: str, key: str, value: Any, full: Dict[str, Any]) -> None:
        if key in BLOB_KEYS:
            self._blobs.setdefault(job_id, {})[key] = value
            if key == "video_data" and value is not None:
                k = full.get("input_video_key") or self.media.new_key(job_id, "input", full.get("filename") or "video.mp4")
                self.media.put_bytes(k, value, "video/mp4")
                self.store.update(job_id, input_video_key=k)
            elif key == "result_video" and value is not None:
                k = full.get("result_video_key") or self.media.new_key(job_id, "output", "result.mp4")
                self.media.put_bytes(k, value, "video/mp4")
                self.store.update(job_id, result_video_key=k)
            elif key == "word_timings":
                import json
                k = full.get("word_timings_key") or self.media.new_key(job_id, "meta", "word_timings.json")
                self.media.put_bytes(k, json.dumps({"words": value or []}).encode("utf-8"), "application/json")
                self.store.update(job_id, word_timings_key=k, has_word_timings=bool(value))
            return
        try:
            self.store.update(job_id, **{key: value})
        except KeyError:
            self.store.create(job_id, {**{k: v for k, v in full.items() if k not in BLOB_KEYS}, key: value})

    def _on_del(self, job_id: str, key: str) -> None:
        if key in BLOB_KEYS and job_id in self._blobs:
            self._blobs[job_id].pop(key, None)
