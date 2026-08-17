"""One-shot job stages for local FastAPI and Fargate workers.

Task sizing (CDK / Fargate, documented here for tests):
  transcribe  → 0.5 vCPU (512) + 1 GB
  align_burn / burn / burn_words → 4 vCPU (4096) + 8 GB
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from typing import Any, Dict, Optional, Tuple, TYPE_CHECKING

from models.requests import TranscriptionResult, TranscriptionSegment
from services.job_store import JobStore
from services.media_store import MediaStore
from services.style_storage_service import StyleStorageService

if TYPE_CHECKING:
    from services.video_processing_service import VideoProcessingService

logger = logging.getLogger(__name__)

PROCESS_STAGE = {
    "generate": "transcribe",
    "burn": "burn",
    "burn_words": "burn_words",
}

# Fargate cpu/memory must match infra/cdk/lib/autocaption-stack.ts
STAGE_TASK_SIZE = {
    "transcribe": {"cpu": "512", "memoryMiB": "1024"},
    "align_burn": {"cpu": "4096", "memoryMiB": "8192"},
    "burn": {"cpu": "4096", "memoryMiB": "8192"},
    "burn_words": {"cpu": "4096", "memoryMiB": "8192"},
}

VALID_STAGES = frozenset(STAGE_TASK_SIZE)


def stage_for_mode(mode: str) -> str:
    try:
        return PROCESS_STAGE[mode]
    except KeyError as exc:
        raise ValueError(f"Unknown job mode: {mode}") from exc


def task_size_for_stage(stage: str) -> Dict[str, str]:
    try:
        return STAGE_TASK_SIZE[stage]
    except KeyError as exc:
        raise ValueError(f"Unknown pipeline stage: {stage}") from exc


def _segments_to_align_cues(segments) -> list:
    cues = []
    for seg in segments:
        text = seg.text if hasattr(seg, "text") else seg.get("text", "")
        start = seg.start if hasattr(seg, "start") else seg["start"]
        end = seg.end if hasattr(seg, "end") else seg["end"]
        text = (text or "").replace("\n", " ").strip()
        if not text:
            continue
        cues.append({"text": text, "start": float(start), "end": float(end)})
    return cues


def _parse_srt_segments(srt_content: str) -> list:
    import pysrt

    with tempfile.NamedTemporaryFile(mode="w", suffix=".srt", delete=False, encoding="utf-8") as tmp:
        tmp.write(srt_content)
        tmp_path = tmp.name
    try:
        subs = pysrt.open(tmp_path, encoding="utf-8")
        segments = []
        for sub in subs:
            start = (
                sub.start.hours * 3600
                + sub.start.minutes * 60
                + sub.start.seconds
                + sub.start.milliseconds / 1000
            )
            end = (
                sub.end.hours * 3600
                + sub.end.minutes * 60
                + sub.end.seconds
                + sub.end.milliseconds / 1000
            )
            segments.append(
                TranscriptionSegment(
                    start=start,
                    end=end,
                    text=sub.text.replace("\n", " ").strip(),
                    confidence=1.0,
                )
            )
        return segments
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def parse_word_timings_payload(payload, default_window: int) -> Tuple[list, int]:
    """Validate and normalize word timings JSON. Returns (words, window_size)."""
    if isinstance(payload, list):
        words_raw = payload
        window_size = default_window
    elif isinstance(payload, dict):
        words_raw = payload.get("words")
        window_size = int(payload.get("window_size") or default_window)
    else:
        raise ValueError("Word timings JSON must be an object or array")

    if not isinstance(words_raw, list) or not words_raw:
        raise ValueError("Word timings must include a non-empty 'words' array")

    words = []
    for i, item in enumerate(words_raw):
        if not isinstance(item, dict):
            raise ValueError(f"Invalid word entry at index {i}")
        text = str(item.get("word", "")).strip()
        if not text:
            continue
        try:
            start = float(item["start"])
            end = float(item["end"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Word entry {i} needs numeric 'start' and 'end'") from exc
        if end < start:
            end = start + 0.05
        words.append({
            "word": text,
            "start": start,
            "end": end,
            "score": item.get("score"),
            "cue_index": item.get("cue_index"),
            "aligned": item.get("aligned", True),
        })

    if not words:
        raise ValueError("No valid words found in word timings file")

    words.sort(key=lambda w: w["start"])
    return words, max(1, window_size)


class JobPipeline:
    def __init__(
        self,
        *,
        job_store: JobStore,
        media_store: MediaStore,
        video_service: "VideoProcessingService",
        style_storage: StyleStorageService,
        settings=None,
    ):
        self.job_store = job_store
        self.media_store = media_store
        self.video_service = video_service
        self.style_storage = style_storage
        self.settings = settings

    def _update(self, job_id: str, **fields: Any) -> Dict[str, Any]:
        return self.job_store.update(job_id, **fields)

    def _fail(self, job_id: str, err: Exception) -> None:
        logger.exception("Job %s failed: %s", job_id, err)
        try:
            self.job_store.update(job_id, status="failed", error=str(err), progress=0)
        except KeyError:
            pass

    def _resolve_style(self, job: dict):
        slug = job.get("style_template_slug")
        if not slug:
            return None, None
        template = self.style_storage.get_template(slug)
        if not template:
            return None, None
        fonts_dir = self.style_storage.fonts_dir_for_template(template)
        return template, fonts_dir

    def _video_to_temp(self, job: dict, dest: str) -> str:
        key = job.get("input_video_key")
        if key and self.media_store.exists(key):
            self.media_store.download_file(key, dest)
            return dest
        raise FileNotFoundError("Input video not found in media store")

    def _put_json(self, job_id: str, kind_name: str, payload: Any) -> str:
        key = self.media_store.new_key(job_id, "meta", kind_name)
        self.media_store.put_bytes(
            key,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            "application/json",
        )
        return key

    def _load_json(self, key: Optional[str]) -> Optional[Any]:
        if not key:
            return None
        raw = self.media_store.get_bytes(key)
        return json.loads(raw.decode("utf-8"))

    def persist_transcription(self, job_id: str, result: dict) -> str:
        return self._put_json(job_id, "transcription.json", result)

    def persist_word_timings(self, job_id: str, word_timings: list, layout=None) -> str:
        ks = self.video_service.karaoke_service
        cues = ks.build_cues(word_timings) if word_timings else []
        if layout is None and word_timings:
            layout = ks.layout_css()
        payload = {
            "version": 1,
            "words": word_timings,
            "cues": cues,
            "cue_rules": ks.cue_rules(),
            "layout": layout,
        }
        return self._put_json(job_id, "word_timings.json", payload)

    async def _render_final_video(
        self,
        job_id: str,
        job: Dict,
        video_path: str,
        final_transcription,
        align_language: str,
    ) -> bytes:
        karaoke_enabled = job.get("karaoke", self.settings.karaoke_enabled_default)
        cues = _segments_to_align_cues(final_transcription.segments)

        if karaoke_enabled and cues:
            self._update(job_id, status="aligning", progress=75, error="")
            word_timings = await self.video_service.align_and_build_karaoke_from_path(
                video_path,
                cues,
                language_code=align_language or "de",
            )
            self._update(job_id, status="generating_karaoke", progress=88)
            self._update(job_id, status="rendering_video", progress=92)
            template, fonts_dir = self._resolve_style(job)
            result = await self.video_service.render_with_karaoke_from_path(
                video_path,
                word_timings,
                style_template=template,
                fonts_dir=fonts_dir,
            )
            layout = self.video_service.karaoke_service.layout_css()
            wt_key = self.persist_word_timings(job_id, word_timings, layout)
            self._update(
                job_id,
                word_timings_key=wt_key,
                has_word_timings=True,
                karaoke_layout=layout,
                karaoke=True,
            )
            return result

        srt_content = await self.video_service.subtitle_service.generate_srt_content(
            final_transcription
        )
        self._update(
            job_id,
            status="rendering_video",
            progress=90,
            has_word_timings=False,
            karaoke_layout=None,
        )
        async with self.video_service.temporary_file(suffix=".srt") as temp_subtitle_path:
            with open(temp_subtitle_path, "w", encoding="utf-8") as f:
                f.write(srt_content)
            return await self.video_service._render_video_to_bytes(video_path, temp_subtitle_path)

    def _store_result(self, job_id: str, result_bytes: bytes) -> str:
        key = self.media_store.new_key(job_id, "output", "result.mp4")
        self.media_store.put_bytes(key, result_bytes, "video/mp4")
        return key

    async def run_transcribe(self, job_id: str) -> None:
        job = self.job_store.get(job_id)
        if not job:
            raise KeyError(job_id)
        try:
            self._update(job_id, status="extracting_audio", progress=20, error="")
            async with self.video_service.temporary_file(suffix=".mp4") as video_path:
                self._video_to_temp(job, video_path)
                async with self.video_service.temporary_file(suffix=".wav") as audio_path:
                    await self.video_service._extract_audio(video_path, audio_path)
                    self._update(job_id, status="transcribing", progress=50)
                    source_language = job.get("source_language")
                    transcription_result = await self.video_service.transcription_service.transcribe_audio(
                        audio_path,
                        language=source_language,
                    )
                    if not source_language:
                        source_language = transcription_result.detected_language or "en"
                    payload = {
                        "text": transcription_result.text,
                        "segments": [
                            {
                                "start": seg.start,
                                "end": seg.end,
                                "text": seg.text,
                                "confidence": seg.confidence,
                            }
                            for seg in transcription_result.segments
                        ],
                        "detected_language": transcription_result.detected_language,
                        "confidence": transcription_result.confidence,
                    }
                    t_key = self.persist_transcription(job_id, payload)
                    self._update(
                        job_id,
                        transcription_key=t_key,
                        source_language=source_language,
                        status="transcription_complete",
                        progress=60,
                    )
                    logger.info("Transcription complete for %s (%s segments)", job_id, len(payload["segments"]))
        except Exception as e:
            self._fail(job_id, e)
            raise

    async def run_align_burn(self, job_id: str) -> None:
        job = self.job_store.get(job_id)
        if not job:
            raise KeyError(job_id)
        try:
            raw = self._load_json(job.get("transcription_key"))
            if not raw or not raw.get("segments"):
                raise ValueError("No transcription available for align/burn")
            segments = [
                TranscriptionSegment(
                    start=seg["start"],
                    end=seg["end"],
                    text=seg["text"],
                    confidence=seg.get("confidence"),
                )
                for seg in raw["segments"]
            ]
            transcription_result = TranscriptionResult(
                text=raw.get("text") or " ".join(s.text for s in segments),
                segments=segments,
                detected_language=raw.get("detected_language"),
                confidence=raw.get("confidence"),
            )
            self._update(job_id, status="translating", progress=70, error="")
            source_language = job.get("source_language") or "en"
            target_language = job.get("target_language") or source_language
            if source_language != target_language:
                translated = await self.video_service.translation_service.translate_segments(
                    transcription_result.segments, source_language, target_language
                )
                final_transcription = TranscriptionResult(
                    text=" ".join(segment.text for segment in translated),
                    segments=translated,
                    detected_language=source_language,
                    confidence=transcription_result.confidence,
                )
            else:
                final_transcription = transcription_result

            self._update(job_id, status="generating_subtitles", progress=80)
            async with self.video_service.temporary_file(suffix=".mp4") as video_path:
                self._video_to_temp(job, video_path)
                align_language = target_language or source_language or "de"
                result_bytes = await self._render_final_video(
                    job_id, job, video_path, final_transcription, align_language
                )
            result_key = self._store_result(job_id, result_bytes)
            self._update(
                job_id,
                status="completed",
                progress=100,
                result_video_key=result_key,
            )
            logger.info("Align/burn complete for %s", job_id)
        except Exception as e:
            self._fail(job_id, e)
            raise

    async def run_burn(self, job_id: str) -> None:
        job = self.job_store.get(job_id)
        if not job:
            raise KeyError(job_id)
        try:
            self._update(job_id, status="generating_subtitles", progress=20, error="")
            srt_content = job.get("srt_content")
            if not srt_content and job.get("srt_key"):
                srt_content = self.media_store.get_bytes(job["srt_key"]).decode("utf-8")
            if not srt_content:
                raise ValueError("No SRT content available for burn mode")
            segments = _parse_srt_segments(srt_content)
            source_language = job.get("source_language") or "en"
            target_language = job.get("target_language") or source_language
            if source_language.lower() != target_language.lower():
                self._update(job_id, status="translating", progress=40)
                segments = await self.video_service.translation_service.translate_segments(
                    segments, source_language, target_language
                )
            self._update(job_id, status="generating_subtitles", progress=55)
            final_transcription = TranscriptionResult(
                text=" ".join(seg.text for seg in segments),
                segments=segments,
                detected_language=source_language,
                confidence=1.0,
            )
            async with self.video_service.temporary_file(suffix=".mp4") as video_path:
                self._video_to_temp(job, video_path)
                align_language = target_language or source_language or "de"
                result_bytes = await self._render_final_video(
                    job_id, job, video_path, final_transcription, align_language
                )
            result_key = self._store_result(job_id, result_bytes)
            self._update(
                job_id,
                status="completed",
                progress=100,
                result_video_key=result_key,
            )
            logger.info("Burn complete for %s", job_id)
        except Exception as e:
            self._fail(job_id, e)
            raise

    async def run_burn_words(self, job_id: str) -> None:
        job = self.job_store.get(job_id)
        if not job:
            raise KeyError(job_id)
        try:
            self._update(job_id, status="generating_karaoke", progress=40, error="")
            word_timings = None
            key = job.get("word_timings_key")
            if key:
                payload = self._load_json(key)
                if isinstance(payload, dict):
                    word_timings = payload.get("words") or []
                elif isinstance(payload, list):
                    word_timings = payload
            if not word_timings:
                raise ValueError("No word timings available for burn_words mode")
            self._update(job_id, status="rendering_video", progress=70)
            template, fonts_dir = self._resolve_style(job)
            async with self.video_service.temporary_file(suffix=".mp4") as video_path:
                self._video_to_temp(job, video_path)
                result_bytes = await self.video_service.render_with_karaoke_from_path(
                    video_path,
                    word_timings,
                    style_template=template,
                    fonts_dir=fonts_dir,
                )
            layout = self.video_service.karaoke_service.layout_css()
            wt_key = self.persist_word_timings(job_id, word_timings, layout)
            result_key = self._store_result(job_id, result_bytes)
            self._update(
                job_id,
                status="completed",
                progress=100,
                result_video_key=result_key,
                word_timings_key=wt_key,
                has_word_timings=True,
                karaoke=True,
                karaoke_layout=layout,
            )
            logger.info("Burn-words complete for %s", job_id)
        except Exception as e:
            self._fail(job_id, e)
            raise

    async def run_stage(self, job_id: str, stage: str) -> None:
        if stage not in VALID_STAGES:
            raise ValueError(f"Unknown pipeline stage: {stage}")
        logger.info("Running stage %s for job %s", stage, job_id)
        if stage == "transcribe":
            await self.run_transcribe(job_id)
        elif stage == "align_burn":
            await self.run_align_burn(job_id)
        elif stage == "burn":
            await self.run_burn(job_id)
        elif stage == "burn_words":
            await self.run_burn_words(job_id)


def build_pipeline() -> JobPipeline:
    from utils.config import get_settings
    from services.job_store import build_job_store
    from services.video_processing_service import VideoProcessingService

    settings = get_settings()

    return JobPipeline(
        job_store=build_job_store(settings),
        media_store=MediaStore(settings),
        video_service=VideoProcessingService(),
        style_storage=StyleStorageService(settings),
        settings=settings,
    )
