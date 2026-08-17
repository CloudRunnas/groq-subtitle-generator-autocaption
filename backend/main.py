from fastapi import FastAPI, File, UploadFile, HTTPException, Form, BackgroundTasks, Request
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
import logging
from typing import Optional, Dict
import json
from datetime import datetime
import io
import uuid

from services.video_processing_service import VideoProcessingService
from services.style_storage_service import StyleStorageService
from services.style_bootstrap import bootstrap_styles, STROKE_PREVIEW_WIDTHS
from services.job_store import build_job_store
from services.media_store import MediaStore, result_download_filename
from services.job_runtime import JobRuntime
from services.job_pipeline import JobPipeline, parse_word_timings_payload, stage_for_mode
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
_cors = [o.strip() for o in (settings.cors_origins or "").split(",") if o.strip()] or [
    "http://localhost:3000"
]
# Browsers reject Access-Control-Allow-Origin: * together with credentials.
_cors_wildcard = "*" in _cors
_cors_origins = ["*"] if _cors_wildcard else _cors

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=not _cors_wildcard,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)
app.add_middleware(ApiKeyMiddleware, api_key=settings.backend_api_key)

video_service = VideoProcessingService()
style_storage = StyleStorageService(settings)
media_store = MediaStore(settings)
job_store = build_job_store(settings)
active_jobs = JobRuntime(job_store, media_store)
pipeline = JobPipeline(
    job_store=job_store,
    media_store=media_store,
    video_service=video_service,
    style_storage=style_storage,
    settings=settings,
)

# Direct S3 GET for the browser; Fargate must not proxy the MP4.
DOWNLOAD_URL_TTL_SECONDS = 12 * 60 * 60


def _ensure_result_video_key(job_id: str, job: Dict) -> Optional[str]:
    key = job.get("result_video_key")
    if key:
        return key
    runtime = active_jobs.get(job_id) or {}
    video_bytes = runtime.get("result_video")
    if not video_bytes:
        return None
    key = media_store.new_key(job_id, "output", "result.mp4")
    media_store.put_bytes(key, video_bytes, "video/mp4")
    try:
        job_store.update(job_id, result_video_key=key)
    except KeyError:
        pass
    return key


@app.on_event("startup")
async def _startup_styles():
    global style_storage
    try:
        style_storage = await bootstrap_styles()
        pipeline.style_storage = style_storage
        logger.info("Style templates bootstrapped (%s)", style_storage.mode)
    except Exception as e:
        logger.warning("Style bootstrap failed: %s", e)


@app.get("/")
async def root():
    return {"message": "Video Subtitle Generator API"}


@app.get("/health")
async def health():
    return {"status": "ok", "storage": "dynamodb" if settings.jobs_table_name else "memory"}


async def _read_json_or_form(request: Request):
    ctype = (request.headers.get("content-type") or "").lower()
    if "application/json" in ctype:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    if "multipart/form-data" in ctype or "application/x-www-form-urlencoded" in ctype:
        form = await request.form()
        return {k: form.get(k) for k in form.keys()}
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


@app.post("/jobs/presign")
async def presign_upload(request: Request):
    """Create job + S3 upload target for large files (CloudFront / Lambda path)."""
    body = await _read_json_or_form(request)
    filename = str(body.get("filename") or "video.mp4")
    content_type = str(body.get("content_type") or "video/mp4")
    mode = str(body.get("mode") or "generate")
    if mode not in ("generate", "burn", "burn_words"):
        raise HTTPException(status_code=400, detail="Mode must be 'generate', 'burn', or 'burn_words'")
    job_id = str(uuid.uuid4())
    key = media_store.new_key(job_id, "input", filename)
    active_jobs[job_id] = {
        "status": "awaiting_upload",
        "filename": filename,
        "mode": mode,
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
    try:
        return parse_word_timings_payload(payload, settings.karaoke_window_size)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


def _truthy(value) -> Optional[bool]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    return str(value).lower() in ("1", "true", "yes", "on")


async def _run_pipeline_stage(job_id: str, stage: str) -> None:
    await pipeline.run_stage(job_id, stage)


@app.post("/process/{job_id}")
async def process_video(job_id: str, request: Request, background_tasks: BackgroundTasks):
    """Start video processing (generate, burn SRT, or burn word timings)."""
    try:
        if job_id not in active_jobs:
            raise HTTPException(status_code=404, detail="Job not found")

        job = active_jobs[job_id]
        body = await _read_json_or_form(request)

        status = job.get("status")
        if status == "awaiting_upload":
            key = job.get("input_video_key")
            if not key or not media_store.exists(key):
                raise HTTPException(status_code=400, detail="Video upload not found")
            job["status"] = "uploaded"
        elif status != "uploaded":
            raise HTTPException(status_code=400, detail="Job not ready for processing")

        mode = job.get("mode", "generate")
        slug = body.get("style_template_slug")
        if slug:
            job["style_template_slug"] = str(slug).strip()

        karaoke_flag = _truthy(body.get("karaoke"))
        karaoke_enabled = settings.karaoke_enabled_default
        if mode == "burn_words":
            karaoke_enabled = True
        elif karaoke_flag is not None:
            karaoke_enabled = karaoke_flag
        job["karaoke"] = karaoke_enabled

        target_language = body.get("target_language")
        source_language = body.get("source_language")
        if target_language:
            job["target_language"] = target_language
        if source_language:
            job["source_language"] = source_language

        srt_content = body.get("srt_content")
        if srt_content:
            srt_key = media_store.new_key(job_id, "meta", "input.srt")
            media_store.put_bytes(srt_key, str(srt_content).encode("utf-8"), "application/x-subrip")
            job["srt_key"] = srt_key

        word_timings_payload = body.get("word_timings")
        if word_timings_payload is not None:
            words, window_size = _parse_word_timings_payload(word_timings_payload)
            wt_key = pipeline.persist_word_timings(job_id, words)
            job["word_timings_key"] = wt_key
            job["window_size"] = window_size
            job["has_word_timings"] = True

        if mode == "burn_words":
            if not job.get("word_timings") and not job.get("word_timings_key"):
                raise HTTPException(status_code=400, detail="No word timings available for burn_words mode")
            background_tasks.add_task(_run_pipeline_stage, job_id, "burn_words")
            return {
                "job_id": job_id,
                "status": "processing_started",
                "message": "Burning karaoke from word timings",
                "mode": "burn_words",
                "karaoke": True,
            }

        if mode == "burn":
            if not job.get("srt_content") and not job.get("srt_key") and not srt_content:
                raise HTTPException(status_code=400, detail="No SRT content available for burn mode")
            if not job.get("source_language"):
                raise HTTPException(status_code=400, detail="source_language is required for burn mode")
            if not job.get("target_language"):
                raise HTTPException(status_code=400, detail="target_language is required for burn mode")
            background_tasks.add_task(_run_pipeline_stage, job_id, "burn")
            logger.info("Started burn for job %s (karaoke=%s)", job_id, karaoke_enabled)
            return {
                "job_id": job_id,
                "status": "processing_started",
                "message": "Burning subtitles into video",
                "mode": "burn",
                "karaoke": karaoke_enabled,
            }

        if not job.get("target_language"):
            raise HTTPException(status_code=400, detail="target_language is required for generate mode")

        background_tasks.add_task(_run_pipeline_stage, job_id, stage_for_mode("generate"))
        logger.info("Started transcribe for job %s (karaoke=%s)", job_id, karaoke_enabled)
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
        logger.error("Error starting processing: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


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
            "progress": job.get("progress") or 0,
            "message": job.get("error") or job.get("message") or "",
            "filename": job.get("filename", ""),
            "karaoke": bool(job.get("karaoke", False)),
            "has_word_timings": bool(job.get("has_word_timings") or job.get("word_timings")),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error getting job status: %s", e)
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

        result = job.get("transcription_result")
        if not result and job.get("transcription_key"):
            result = json.loads(media_store.get_bytes(job["transcription_key"]).decode("utf-8"))
        if not result:
            raise HTTPException(status_code=404, detail="Transcription not found")

        return {
            "job_id": job_id,
            "transcription": result,
            "source_language": job.get("source_language"),
            "target_language": job.get("target_language"),
            "filename": job.get("filename"),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error getting transcription: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/transcription/{job_id}/continue")
async def continue_with_transcription(
    job_id: str,
    background_tasks: BackgroundTasks,
    transcription: dict,
):
    """Continue processing with edited transcription (starts align/burn stage)."""
    try:
        if job_id not in active_jobs:
            raise HTTPException(status_code=404, detail="Job not found")

        job = active_jobs[job_id]
        if job["status"] != "transcription_complete":
            raise HTTPException(status_code=400, detail="Job not ready for transcription continuation")

        if not isinstance(transcription, dict) or not transcription.get("segments"):
            raise HTTPException(status_code=400, detail="Edited transcription with segments is required")

        key = pipeline.persist_transcription(job_id, transcription)
        job_store.update(job_id, transcription_key=key, status="queued_align", progress=65, error="")
        background_tasks.add_task(_run_pipeline_stage, job_id, "align_burn")
        logger.info("Continuing with edited transcription for job %s", job_id)
        return {
            "job_id": job_id,
            "status": "processing_continued",
            "message": "Processing continued with edited transcription",
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error continuing with transcription: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


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
        stored = None
        if job.get("word_timings_key"):
            try:
                stored = json.loads(media_store.get_bytes(job["word_timings_key"]).decode("utf-8"))
            except Exception:
                stored = None
        if isinstance(stored, dict) and stored.get("words"):
            word_timings = stored.get("words") or word_timings
            cues = stored.get("cues") or video_service.karaoke_service.build_cues(word_timings)
            layout = stored.get("layout") or job.get("karaoke_layout")
            cue_rules = stored.get("cue_rules") or video_service.karaoke_service.cue_rules()
        else:
            cues = video_service.karaoke_service.build_cues(word_timings)
            layout = job.get("karaoke_layout")
            cue_rules = video_service.karaoke_service.cue_rules()
        if not layout:
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
            "cue_rules": cue_rules,
            "layout": layout,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting word timings: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/download/word-timings/{job_id}")
async def download_word_timings(job_id: str):
    """Download word-level timings as JSON (does not delete the job)."""
    try:
        job = job_store.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        if job.get("status") != "completed":
            raise HTTPException(status_code=400, detail="Job not completed yet")

        word_timings = job.get("word_timings") or []
        key = job.get("word_timings_key")
        if not word_timings and key:
            raw = media_store.get_bytes(key)
            payload = json.loads(raw.decode("utf-8"))
            word_timings = payload.get("words", payload if isinstance(payload, list) else [])
        if not word_timings:
            runtime = active_jobs.get(job_id) or {}
            word_timings = runtime.get("word_timings") or []
        if not word_timings:
            raise HTTPException(status_code=404, detail="No word timings available for this job")

        body = {
            "version": 1,
            "words": word_timings,
            "cue_rules": video_service.karaoke_service.cue_rules(),
        }
        content = json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8")
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
    """Return a 12h S3 GET URL for the rendered video (no proxy through Fargate)."""
    try:
        job = job_store.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        if job.get("status") != "completed":
            raise HTTPException(status_code=400, detail="Job not completed yet")

        key = _ensure_result_video_key(job_id, job)
        if not key:
            raise HTTPException(status_code=404, detail="Processed video not found")

        filename = result_download_filename(job.get("filename"))
        if media_store.use_s3:
            url = media_store.presign_get(
                key,
                expires=DOWNLOAD_URL_TTL_SECONDS,
                response_content_disposition=f'attachment; filename="{filename}"',
                response_content_type="video/mp4",
            )
            if not url:
                raise HTTPException(status_code=500, detail="Could not create download URL")
            logger.info("Issued S3 download URL for job %s key=%s ttl=%ss", job_id, key, DOWNLOAD_URL_TTL_SECONDS)
            return JSONResponse(
                {
                    "url": url,
                    "expires_in": DOWNLOAD_URL_TTL_SECONDS,
                    "filename": filename,
                }
            )

        video_bytes = media_store.get_bytes(key) if media_store.exists(key) else None
        if not video_bytes:
            runtime = active_jobs.get(job_id) or {}
            video_bytes = runtime.get("result_video")
        if not video_bytes:
            raise HTTPException(status_code=404, detail="Processed video not found")
        return StreamingResponse(
            io.BytesIO(video_bytes),
            media_type="video/mp4",
            headers={
                "Content-Disposition": f"attachment; filename={filename}",
                "Content-Length": str(len(video_bytes)),
            },
        )
    except HTTPException:
        raise
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