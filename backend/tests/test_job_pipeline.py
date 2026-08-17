"""Pipeline stage map and Fargate size contract."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.job_pipeline import STAGE_TASK_SIZE, stage_for_mode, task_size_for_stage
from services.job_store import MemoryJobStore
from services.job_pipeline import JobPipeline


def test_generate_starts_transcribe_stage():
    assert stage_for_mode("generate") == "transcribe"
    assert stage_for_mode("burn") == "burn"
    assert stage_for_mode("burn_words") == "burn_words"


def test_transcribe_is_half_vcpu_1gb():
    size = task_size_for_stage("transcribe")
    assert size["cpu"] == "512"
    assert size["memoryMiB"] == "1024"


def test_align_burn_is_4_vcpu_8gb():
    for stage in ("align_burn", "burn", "burn_words"):
        size = task_size_for_stage(stage)
        assert size["cpu"] == "4096"
        assert size["memoryMiB"] == "8192"
        assert STAGE_TASK_SIZE[stage] == size


def test_unknown_mode_raises():
    try:
        stage_for_mode("nope")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_persist_transcription_writes_json_not_job_blob(tmp_path):
    class Settings:
        media_bucket = ""
        aws_region = "eu-central-1"
        media_local_dir = str(tmp_path)
        karaoke_enabled_default = True
        karaoke_window_size = 5

    from services.media_store import MediaStore

    store = MemoryJobStore()
    media = MediaStore(Settings())
    pipeline = JobPipeline(
        job_store=store,
        media_store=media,
        video_service=None,  # type: ignore
        style_storage=None,  # type: ignore
        settings=Settings(),
    )
    store.create("j1", {"status": "transcribing", "filename": "a.mp4"})
    payload = {"text": "hello", "segments": [{"start": 0, "end": 1, "text": "hello"}]}
    key = pipeline.persist_transcription("j1", payload)
    loaded = pipeline._load_json(key)
    assert loaded["text"] == "hello"
    assert "transcription_result" not in (store.get("j1") or {})
    store.update("j1", transcription_key=key)
    assert store.get("j1")["transcription_key"] == key
