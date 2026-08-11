from fastapi import FastAPI, File, UploadFile, HTTPException, Form, BackgroundTasks
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
import logging
from typing import Optional, Dict
import asyncio
import json
from datetime import datetime
import io
import uuid

from services.video_processing_service import VideoProcessingService
from services.style_storage_service import StyleStorageService
from services.style_bootstrap import bootstrap_styles, STROKE_PREVIEW_WIDTHS
from services.job_store import build_job_store
from services.media_store import MediaStore
from services.job_runtime import JobRuntime
from models.style_template import StyleTemplate
from utils.config import get_settings
from utils.auth import ApiKeyMiddleware

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Video Subtitle Generator", version="1.0.0")

settings = get_settings()
_cors = [o.strip() for o in (settings.cors_origins or "").split(",") if o.strip()] or ["http://localhost:3000"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=(_cors + ["*"]) if not settings.backend_api_key else _cors,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(ApiKeyMiddleware, api_key=settings.backend_api_key)

video_service = VideoProcessingService()
style_storage = StyleStorageService(settings)
media_store = MediaStore(settings)
job_store = build_job_store(settings)
active_jobs = JobRuntime(job_store, media_store)


@app.on_event("startup")
async def _startup_styles():
    global style_storage
    try:
        style_storage = await bootstrap_styles()
        logger.info("Style templates bootstrapped (%s)", style_storage.mode)
    except Exception as e:
        logger.warning("Style bootstrap failed: %s", e)


def _resolve_job_style(job: dict):
    slug = job.get("style_template_slug")
    if not slug:
        return None, None
    template = style_storage.get_template(slug)
    if not template:
        return None, None
    fonts_dir = style_storage.fonts_dir_for_template(template)
    return template, fonts_dir


@app.get("/")
async def root():
    return {"message": "Video Subtitle Generator API"}


@app.get("/health")
async def health():
    return {"status": "ok", "storage": "dynamodb" if settings.jobs_table_name else "memory"}


@app.get("/warmup")
async def warmup():
    """Lightweight ping to reduce cold starts (UI calls via Warmup Lambda)."""
    _ = job_store.list_by_tenant("default", limit=1)
    return {"status": "warm", "ts": datetime.now().isoformat()}


@app.post("/jobs/presign")
async def presign_upload(filename: str = Form("video.mp4"), content_type: str = Form("video/mp4")):
    """Create job + S3/local upload target for large files."""
    job_id = str(uuid.uuid4())
    key = media_store.new_key(job_id, "input", filename)
    active_jobs[job_id] = {
        "status": "awaiting_upload",
        "filename": filename,
        "mode": "generate",
        "input_video_key": key,
        "progress": 0,
        "karaoke": settings.karaoke_enabled_default,
        "created_at": datetime.now().isoformat(),
    }
    url = media_store.presign_put(key, content_type=content_type)
    return {
        "job_id": job_id,
        "upload_url": url,
        "input_video_key": key,
        "use_s3": media_store.use_s3,
    }


@app.get("/styles/templates")
async def list_style_templates():
    return {"templates": [t.model_dump() for t in style_storage.list_templates()]}


@app.get("/styles/templates/{slug}")
async def get_style_template(slug: str):
    tmpl = style_storage.get_template(slug)
    if not tmpl:
        raise HTTPException(status_code=404, detail="Template not found")
    return tmpl.model_dump()


@app.post("/styles/templates")
async def save_style_template(payload: dict):
    try:
        tmpl = StyleTemplate.model_validate(payload)
        saved = style_storage.save_template(tmpl)
        return saved.model_dump()
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/styles/fonts")
async def list_style_fonts():
    return {"fonts": [f.model_dump() for f in style_storage.list_fonts()]}


@app.post("/styles/fonts")
async def upload_style_font(file: UploadFile = File(...)):
    name = file.filename or "font.ttf"
    if not name.lower().endswith((".ttf", ".otf")):
        raise HTTPException(status_code=400, detail="Only .ttf and .otf fonts are supported")
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty font file")
    try:
        asset = style_storage.upload_font(name, data)
        return asset.model_dump()
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/styles/stroke-previews")
async def list_stroke_previews():
    items = []
    for i, width in enumerate(STROKE_PREVIEW_WIDTHS):
        path = style_storage.stroke_preview_path(i)
        items.append({
            "index": i,
            "strokeWidth": width,
            "url": f"/styles/stroke-previews/{i}" if path.exists() else None,
        })
    return {"previews": items}


@app.get("/styles/stroke-previews/{index}")
async def get_stroke_preview(index: int):
    if index < 0 or index >= len(STROKE_PREVIEW_WIDTHS):
        raise HTTPException(status_code=404, detail="Preview not found")
    path = style_storage.stroke_preview_path(index)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Preview not generated")
    return FileResponse(path, media_type="image/png")


@app.post("/upload")
async def upload_video(
    file: UploadFile = File(...),
    mode: str = Form("generate"),
    srt_file: Optional[UploadFile] = File(None),
    word_timings_file: Optional[UploadFile] = File(None),
):
    """Upload video (optional SRT for burn, optional word-timings JSON for burn_words)"""
    try:
        # validate
        if not file.content_type or not file.content_type.startswith('video/'):
            raise HTTPException(status_code=400, detail="File must be a video")

        if mode not in ("generate", "burn", "burn_words"):
            raise HTTPException(
                status_code=400,
                detail="Mode must be 'generate', 'burn', or 'burn_words'",
            )
        
        # read video data
        video_data = await file.read()
        
        # check file size 
        if len(video_data) > settings.max_file_size:
            max_size_mb = settings.max_file_size / (1024 * 1024)
            raise HTTPException(status_code=400, detail=f"File size exceeds {max_size_mb:.0f}MB limit")

        srt_content = None
        srt_filename = None
        word_timings = None
        word_timings_filename = None
        window_size = settings.karaoke_window_size

        if mode == "burn":
            if srt_file is None:
                raise HTTPException(status_code=400, detail="SRT file is required for burn mode")

            srt_filename = srt_file.filename or ""
            if not srt_filename.lower().endswith(".srt"):
                raise HTTPException(status_code=400, detail="Subtitle file must be an .srt file")

            srt_bytes = await srt_file.read()
            try:
                srt_content = srt_bytes.decode("utf-8")
            except UnicodeDecodeError:
                srt_content = srt_bytes.decode("latin-1")

            if not srt_content.strip():
                raise HTTPException(status_code=400, detail="SRT file is empty")

        if mode == "burn_words":
            if word_timings_file is None:
                raise HTTPException(
                    status_code=400,
                    detail="word_timings JSON file is required for burn_words mode",
                )
            word_timings_filename = word_timings_file.filename or ""
            if not word_timings_filename.lower().endswith(".json"):
                raise HTTPException(status_code=400, detail="Word timings file must be a .json file")

            raw = await word_timings_file.read()
            try:
                payload = json.loads(raw.decode("utf-8"))
            except Exception:
                raise HTTPException(status_code=400, detail="Invalid word timings JSON")

            word_timings, window_size = _parse_word_timings_payload(payload)
        
        import uuid
        job_id = str(uuid.uuid4())
        
        active_jobs[job_id] = {
            "status": "uploaded",
            "filename": file.filename,
            "video_data": video_data,
            "mode": mode,
            "srt_content": srt_content,
            "srt_filename": srt_filename,
            "word_timings": word_timings,
            "word_timings_filename": word_timings_filename,
            "window_size": window_size,
            "karaoke": mode == "burn_words" or settings.karaoke_enabled_default,
            "created_at": datetime.now().isoformat(),
            "progress": 0
        }
        
        logger.info(f"Video uploaded successfully: {file.filename} (Job ID: {job_id}, mode: {mode})")
        
        return {
            "job_id": job_id,
            "filename": file.filename,
            "size": len(video_data),
            "status": "uploaded",
            "mode": mode,
            "srt_filename": srt_filename,
            "word_timings_filename": word_timings_filename,
            "word_count": len(word_timings) if word_timings else 0,
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error uploading video: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


def _parse_word_timings_payload(payload) -> tuple:
    """Validate and normalize word timings JSON. Returns (words, window_size)."""
    if isinstance(payload, list):
        words_raw = payload
        window_size = settings.karaoke_window_size
    elif isinstance(payload, dict):
        words_raw = payload.get("words")
        window_size = int(payload.get("window_size") or settings.karaoke_window_size)
    else:
        raise HTTPException(status_code=400, detail="Word timings JSON must be an object or array")

    if not isinstance(words_raw, list) or not words_raw:
        raise HTTPException(status_code=400, detail="Word timings must include a non-empty 'words' array")

    words = []
    for i, item in enumerate(words_raw):
        if not isinstance(item, dict):
            raise HTTPException(status_code=400, detail=f"Invalid word entry at index {i}")
        text = str(item.get("word", "")).strip()
        if not text:
            continue
        try:
            start = float(item["start"])
            end = float(item["end"])
        except (KeyError, TypeError, ValueError):
            raise HTTPException(
                status_code=400,
                detail=f"Word entry {i} needs numeric 'start' and 'end'",
            )
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
        raise HTTPException(status_code=400, detail="No valid words found in word timings file")

    words.sort(key=lambda w: w["start"])
    return words, max(1, window_size)


@app.post("/process/{job_id}")
async def process_video(
    job_id: str,
    background_tasks: BackgroundTasks,
    target_language: Optional[str] = Form(None),
    source_language: Optional[str] = Form(None),
    karaoke: Optional[str] = Form(None),
    style_template_slug: Optional[str] = Form(None),
):
    """Start video processing (generate, burn SRT, or burn word timings)"""
    try:
        if job_id not in active_jobs:
            raise HTTPException(status_code=404, detail="Job not found")
        
        job = active_jobs[job_id]
        
        if job["status"] != "uploaded":
            raise HTTPException(status_code=400, detail="Job not ready for processing")

        mode = job.get("mode", "generate")
        if style_template_slug:
            job["style_template_slug"] = style_template_slug.strip()

        karaoke_enabled = settings.karaoke_enabled_default
        if mode == "burn_words":
            karaoke_enabled = True
        elif karaoke is not None:
            karaoke_enabled = str(karaoke).lower() in ("1", "true", "yes", "on")
        job["karaoke"] = karaoke_enabled

        if mode == "burn_words":
            if not job.get("word_timings"):
                raise HTTPException(status_code=400, detail="No word timings available for burn_words mode")
            background_tasks.add_task(burn_word_timings_background, job_id)
            logger.info(f"Started burn-word-timings processing for job: {job_id}")
            return {
                "job_id": job_id,
                "status": "processing_started",
                "message": "Burning karaoke from word timings",
                "mode": "burn_words",
                "karaoke": True,
            }

        if mode == "burn":
            if not job.get("srt_content"):
                raise HTTPException(status_code=400, detail="No SRT content available for burn mode")
            if not source_language:
                raise HTTPException(status_code=400, detail="source_language is required for burn mode")
            if not target_language:
                raise HTTPException(status_code=400, detail="target_language is required for burn mode")

            job["source_language"] = source_language
            job["target_language"] = target_language

            background_tasks.add_task(
                burn_subtitles_background,
                job_id,
                source_language,
                target_language
            )
            logger.info(f"Started burn-subtitles processing for job: {job_id} (karaoke={karaoke_enabled})")
            return {
                "job_id": job_id,
                "status": "processing_started",
                "message": "Burning subtitles into video",
                "mode": "burn",
                "karaoke": karaoke_enabled,
            }

        if not target_language:
            raise HTTPException(status_code=400, detail="target_language is required for generate mode")
        
        job["target_language"] = target_language
        
        background_tasks.add_task(
            process_video_background,
            job_id,
            job["video_data"],
            target_language,
            source_language
        )
        
        logger.info(f"Started processing for job: {job_id} (karaoke={karaoke_enabled})")
        
        return {
            "job_id": job_id,
            "status": "processing_started",
            "message": "Video processing started",
            "mode": "generate",
            "karaoke": karaoke_enabled,
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error starting processing: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


async def burn_word_timings_background(job_id: str):
    """Burn karaoke ASS from precomputed word timings (skip align)."""
    try:
        job = active_jobs[job_id]
        job["status"] = "generating_karaoke"
        job["progress"] = 40
        job["error"] = ""

        video_data = job["video_data"]
        word_timings = job["word_timings"]

        job["status"] = "rendering_video"
        job["progress"] = 70
        template, fonts_dir = _resolve_job_style(job)
        result_video_bytes = await video_service.render_with_karaoke(
            video_data,
            word_timings,
            style_template=template,
            fonts_dir=fonts_dir,
        )

        job["status"] = "completed"
        job["progress"] = 100
        job["result_video"] = result_video_bytes
        job["karaoke"] = True
        job["karaoke_layout"] = video_service.karaoke_service.layout_css()
        job["completed_at"] = datetime.now().isoformat()

        del job["video_data"]
        logger.info(f"Burn-word-timings job completed successfully: {job_id}")

    except Exception as e:
        logger.error(f"Error burning word timings for job {job_id}: {str(e)}")
        if job_id in active_jobs:
            active_jobs[job_id]["status"] = "failed"
            active_jobs[job_id]["error"] = str(e)
            active_jobs[job_id]["progress"] = 0


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


async def _render_final_video(
    job: Dict,
    video_data: bytes,
    final_transcription,
    align_language: str,
) -> bytes:
    """Render with karaoke ASS when enabled, otherwise plain SRT."""
    karaoke_enabled = job.get("karaoke", settings.karaoke_enabled_default)
    cues = _segments_to_align_cues(final_transcription.segments)

    if karaoke_enabled and cues:
        job["status"] = "aligning"
        job["progress"] = 75
        job["error"] = ""
        word_timings = await video_service.align_and_build_karaoke(
            video_data,
            cues,
            language_code=align_language or "de",
        )
        job["word_timings"] = word_timings

        job["status"] = "generating_karaoke"
        job["progress"] = 88

        job["status"] = "rendering_video"
        job["progress"] = 92
        template, fonts_dir = _resolve_job_style(job)
        result = await video_service.render_with_karaoke(
            video_data,
            word_timings,
            style_template=template,
            fonts_dir=fonts_dir,
        )
        job["karaoke_layout"] = video_service.karaoke_service.layout_css()
        return result

    # Plain SRT burn
    srt_content = await video_service.subtitle_service.generate_srt_content(final_transcription)
    job["word_timings"] = []
    job["karaoke_layout"] = None
    job["status"] = "rendering_video"
    job["progress"] = 90

    async with video_service.temporary_file(suffix=".mp4") as temp_video_path:
        with open(temp_video_path, 'wb') as f:
            f.write(video_data)
        async with video_service.temporary_file(suffix=".srt") as temp_subtitle_path:
            with open(temp_subtitle_path, 'w', encoding='utf-8') as f:
                f.write(srt_content)
            return await video_service._render_video_to_bytes(temp_video_path, temp_subtitle_path)


async def burn_subtitles_background(job_id: str, source_language: str, target_language: str):
    """Burn an uploaded SRT into the video, optionally translating + karaoke aligning"""
    try:
        import tempfile
        import os
        import pysrt
        from models.requests import TranscriptionSegment, TranscriptionResult

        job = active_jobs[job_id]
        job["status"] = "generating_subtitles"
        job["progress"] = 20
        job["error"] = ""

        video_data = job["video_data"]
        srt_content = job["srt_content"]

        with tempfile.NamedTemporaryFile(mode='w', suffix='.srt', delete=False, encoding='utf-8') as tmp:
            tmp.write(srt_content)
            tmp_path = tmp.name

        try:
            subs = pysrt.open(tmp_path, encoding='utf-8')
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
                        text=sub.text.replace('\n', ' ').strip(),
                        confidence=1.0
                    )
                )
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

        if source_language.lower() != target_language.lower():
            job["status"] = "translating"
            job["progress"] = 40
            segments = await video_service.translation_service.translate_segments(
                segments, source_language, target_language
            )

        job["status"] = "generating_subtitles"
        job["progress"] = 55

        final_transcription = TranscriptionResult(
            text=" ".join([seg.text for seg in segments]),
            segments=segments,
            detected_language=source_language,
            confidence=1.0
        )

        # Align against the language of the burned text
        align_language = target_language or source_language or "de"
        result_video_bytes = await _render_final_video(
            job, video_data, final_transcription, align_language
        )

        job["status"] = "completed"
        job["progress"] = 100
        job["result_video"] = result_video_bytes
        job["completed_at"] = datetime.now().isoformat()

        del job["video_data"]
        if "srt_content" in job:
            del job["srt_content"]

        logger.info(f"Burn-subtitles job completed successfully: {job_id}")

    except Exception as e:
        logger.error(f"Error burning subtitles for job {job_id}: {str(e)}")
        if job_id in active_jobs:
            active_jobs[job_id]["status"] = "failed"
            active_jobs[job_id]["error"] = str(e)
            active_jobs[job_id]["progress"] = 0

async def process_video_background(job_id: str, video_data: bytes, target_language: str, source_language: Optional[str]):
    """Background task for video processing"""
    try:
        job = active_jobs[job_id]
        
        job["progress"] = 20
        job["status"] = "extracting_audio"
        
        async with video_service.temporary_file(suffix=".mp4") as temp_video_path:
            with open(temp_video_path, 'wb') as f:
                f.write(video_data)
            
            async with video_service.temporary_file(suffix=".wav") as temp_audio_path:
                await video_service._extract_audio(temp_video_path, temp_audio_path)
                
                job["progress"] = 50
                job["status"] = "transcribing"
                
                logger.info(f"Starting transcription for job {job_id}")
                transcription_result = await video_service.transcription_service.transcribe_audio(
                    temp_audio_path, 
                    language=source_language
                )
                
                logger.info(f"Transcription completed for job {job_id}. Segments: {len(transcription_result.segments)}, Text length: {len(transcription_result.text)}")
                
                # detect language if not provided
                if not source_language:
                    source_language = transcription_result.detected_language or "en"
                
                # store transcription result for user review
                job["transcription_result"] = {
                    "text": transcription_result.text,
                    "segments": [
                        {
                            "start": seg.start,
                            "end": seg.end,
                            "text": seg.text,
                            "confidence": seg.confidence
                        } for seg in transcription_result.segments
                    ],
                    "detected_language": transcription_result.detected_language,
                    "confidence": transcription_result.confidence
                }
                job["source_language"] = source_language
                job["status"] = "transcription_complete"
                job["progress"] = 60
                
                logger.info(f"Transcription completed for job {job_id}, waiting for user review")
                
    except Exception as e:
        logger.error(f"Error processing video {job_id}: {str(e)}")
        if job_id in active_jobs:
            active_jobs[job_id]["status"] = "failed"
            active_jobs[job_id]["error"] = str(e)
            active_jobs[job_id]["progress"] = 0

@app.get("/status/{job_id}")
async def get_job_status(job_id: str):
    """Get job status"""
    try:
        if job_id not in active_jobs:
            raise HTTPException(status_code=404, detail="Job not found")
        
        job = active_jobs[job_id]
        
        return {
            "job_id": job_id,
            "status": job["status"],
            "progress": job["progress"],
            "message": job.get("error", ""),
            "filename": job.get("filename", ""),
            "karaoke": bool(job.get("karaoke", False)),
            "has_word_timings": bool(job.get("word_timings")),
        }
        
    except Exception as e:
        logger.error(f"Error getting job status: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/transcription/{job_id}")
async def get_transcription(job_id: str):
    """Get transcription result for user review"""
    try:
        if job_id not in active_jobs:
            raise HTTPException(status_code=404, detail="Job not found")
        
        job = active_jobs[job_id]
        
        if job["status"] != "transcription_complete":
            raise HTTPException(status_code=400, detail="Transcription not ready for review")
        
        return {
            "job_id": job_id,
            "transcription": job["transcription_result"],
            "source_language": job["source_language"],
            "target_language": job["target_language"],
            "filename": job["filename"]
        }
        
    except Exception as e:
        logger.error(f"Error getting transcription: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/transcription/{job_id}/continue")
async def continue_with_transcription(
    job_id: str,
    background_tasks: BackgroundTasks,
    transcription: dict
):
    """Continue processing with edited transcription"""
    try:
        if job_id not in active_jobs:
            raise HTTPException(status_code=404, detail="Job not found")
        
        job = active_jobs[job_id]
        
        if job["status"] != "transcription_complete":
            raise HTTPException(status_code=400, detail="Job not ready for transcription continuation")
        
        # start background processing with edited transcription
        background_tasks.add_task(
            continue_processing_after_transcription,
            job_id,
            transcription
        )
        
        logger.info(f"Continuing processing with edited transcription for job: {job_id}")
        
        return {
            "job_id": job_id,
            "status": "processing_continued",
            "message": "Processing continued with edited transcription"
        }
        
    except Exception as e:
        logger.error(f"Error continuing with transcription: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

async def continue_processing_after_transcription(job_id: str, edited_transcription: dict):
    """Continue processing after user has reviewed/edited transcription"""
    try:
        job = active_jobs[job_id]
        
        from models.requests import TranscriptionResult, TranscriptionSegment
        
        segments = [
            TranscriptionSegment(
                start=seg["start"],
                end=seg["end"], 
                text=seg["text"],
                confidence=seg.get("confidence")
            ) for seg in edited_transcription["segments"]
        ]
        
        transcription_result = TranscriptionResult(
            text=edited_transcription["text"],
            segments=segments,
            detected_language=edited_transcription.get("detected_language"),
            confidence=edited_transcription.get("confidence")
        )
        
        job["status"] = "translating"
        job["progress"] = 70
        
        source_language = job["source_language"]
        target_language = job["target_language"]
        
        if source_language != target_language:
            translated_segments = await video_service.translation_service.translate_segments(
                transcription_result.segments, source_language, target_language
            )
            
            final_transcription = TranscriptionResult(
                text=" ".join([segment.text for segment in translated_segments]),
                segments=translated_segments,
                detected_language=source_language,
                confidence=transcription_result.confidence
            )
        else:
            final_transcription = transcription_result
        
        job["status"] = "generating_subtitles"
        job["progress"] = 80
        
        video_data = job["video_data"]
        align_language = target_language or source_language or "de"
        result_video_bytes = await _render_final_video(
            job, video_data, final_transcription, align_language
        )
        
        job["status"] = "completed"
        job["progress"] = 100
        job["result_video"] = result_video_bytes
        job["completed_at"] = datetime.now().isoformat()
        
        del job["video_data"]
        if "transcription_result" in job:
            del job["transcription_result"]
        
        logger.info(f"Job completed successfully: {job_id}")
        
    except Exception as e:
        logger.error(f"Error continuing processing for job {job_id}: {str(e)}")
        if job_id in active_jobs:
            active_jobs[job_id]["status"] = "failed"
            active_jobs[job_id]["error"] = str(e)
            active_jobs[job_id]["progress"] = 0

@app.get("/word-timings/{job_id}")
async def get_word_timings(job_id: str):
    """Return word-level karaoke timings for live preview overlay"""
    try:
        if job_id not in active_jobs:
            raise HTTPException(status_code=404, detail="Job not found")

        job = active_jobs[job_id]

        if job["status"] != "completed":
            raise HTTPException(status_code=400, detail="Word timings not ready yet")

        word_timings = job.get("word_timings") or []
        cues = video_service.karaoke_service.build_cues(word_timings)
        layout = job.get("karaoke_layout")
        if not layout:
            # Recompute fit if layout was not stored (e.g. older in-memory jobs)
            ks = video_service.karaoke_service
            play_x = int(ks.style.play_res_x or 1920)
            play_y = int(ks.style.play_res_y or 1080)
            ks.apply_layout_from_resolution(play_x, play_y)
            ks.fit_constant_font_size(cues)
            layout = ks.layout_css()
        return {
            "job_id": job_id,
            "karaoke": bool(job.get("karaoke", False)),
            "word_count": len(word_timings),
            "cue_count": len(cues),
            "words": word_timings,
            "cues": cues,
            "cue_rules": video_service.karaoke_service.cue_rules(),
            "layout": layout,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting word timings: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/download/word-timings/{job_id}")
async def download_word_timings(job_id: str):
    """Download word-level timings as JSON (does not delete the job)"""
    try:
        if job_id not in active_jobs:
            raise HTTPException(status_code=404, detail="Job not found")

        job = active_jobs[job_id]
        if job["status"] != "completed":
            raise HTTPException(status_code=400, detail="Job not completed yet")

        word_timings = job.get("word_timings") or []
        if not word_timings:
            raise HTTPException(status_code=404, detail="No word timings available for this job")

        payload = {
            "version": 1,
            "words": word_timings,
            "cue_rules": video_service.karaoke_service.cue_rules(),
        }
        content = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        filename = f"word_timings_{job_id}.json"

        return StreamingResponse(
            io.BytesIO(content),
            media_type="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Length": str(len(content)),
            },
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error downloading word timings: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/download/{job_id}")
async def download_video(job_id: str):
    """Download processed video with subtitles"""
    try:
        if job_id not in active_jobs:
            raise HTTPException(status_code=404, detail="Job not found")
        
        job = active_jobs[job_id]
        
        if job["status"] != "completed":
            raise HTTPException(status_code=400, detail="Job not completed yet")
        
        if "result_video" not in job:
            raise HTTPException(status_code=404, detail="Processed video not found")
        
        video_bytes = job["result_video"]
        
        original_filename = job["filename"]
        name, ext = original_filename.rsplit('.', 1)
        download_filename = f"{name}_subtitled.{ext}"
        
        def cleanup_job():
            if job_id in active_jobs:
                del active_jobs[job_id]
                logger.info(f"Cleaned up job from memory: {job_id}")
        
        def generate():
            yield video_bytes
            cleanup_job()
        
        return StreamingResponse(
            generate(),
            media_type="video/mp4",
            headers={
                "Content-Disposition": f"attachment; filename={download_filename}",
                "Content-Length": str(len(video_bytes))
            }
        )
        
    except Exception as e:
        logger.error(f"Error downloading video: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/video/preview/{job_id}")
async def preview_video(job_id: str):
    """Stream processed video for preview"""
    try:
        if job_id not in active_jobs:
            raise HTTPException(status_code=404, detail="Job not found")
        
        job = active_jobs[job_id]
        
        if job["status"] != "completed":
            raise HTTPException(status_code=400, detail="Job not completed yet")
        
        if "result_video" not in job:
            raise HTTPException(status_code=404, detail="Processed video not found")
        
        video_bytes = job["result_video"]
        
        return StreamingResponse(
            io.BytesIO(video_bytes),
            media_type="video/mp4",
            headers={
                "Content-Length": str(len(video_bytes)),
                "Accept-Ranges": "bytes"
            }
        )
        
    except Exception as e:
        logger.error(f"Error streaming video preview: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000) 