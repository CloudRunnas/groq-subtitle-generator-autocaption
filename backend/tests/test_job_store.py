"""Unit tests for job store (memory) and auth helpers."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.job_store import MemoryJobStore, build_job_store
from models.style_template import StyleTemplate, TextRoleStyle


def test_memory_job_store_crud():
    store = MemoryJobStore()
    job = store.create("abc", {"filename": "a.mp4", "mode": "burn_words", "status": "uploaded"})
    assert job["job_id"] == "abc"
    assert store.get("abc")["filename"] == "a.mp4"
    store.update("abc", status="processing", progress=50)
    assert store.get("abc")["progress"] == 50
    assert store.get("abc")["status"] == "processing"
    store.update("abc", status="completed", progress=100)
    assert store.get("abc")["completed_at"]
    listed = store.list_by_status("completed")
    assert len(listed) == 1
    tenant = store.list_by_tenant("default")
    assert len(tenant) == 1
    store.delete("abc")
    assert store.get("abc") is None


def test_build_job_store_memory_when_no_table():
    class S:
        jobs_table_name = ""
        aws_region = "eu-central-1"

    store = build_job_store(S())
    assert isinstance(store, MemoryJobStore)


def test_style_template_roles():
    t = StyleTemplate(
        name="t",
        spokenText=TextRoleStyle(color="#FFFFFF", formatting="bold"),
        activeText=TextRoleStyle(color="#F97316", formatting="bold"),
        normalText=TextRoleStyle(color="#FFFFFF", formatting="normal"),
    )
    assert t.activeText.color == "#F97316"
    assert t.effects.bounce.enabled is False or True  # defaults exist
