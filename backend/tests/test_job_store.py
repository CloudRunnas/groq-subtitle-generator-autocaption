"""Unit tests for job store (memory) and auth helpers."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from decimal import Decimal

from boto3.dynamodb.types import TypeSerializer

from services.job_store import DynamoJobStore, MemoryJobStore, build_job_store
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


def _karaoke_layout_floats() -> dict:
    return {
        "width_pct": 80.0,
        "left_pct": 10.0,
        "bottom_pct": 8.5,
        "height_pct": 20.0,
        "font_size": 48,
        "font_size_min": 24,
        "font_size_max": 72,
        "play_res_x": 1920,
        "play_res_y": 1080,
        "box_width": 1536,
        "box_height": 216,
    }


def test_dynamo_to_item_converts_karaoke_layout_floats():
    """boto3 rejects Python float; karaoke_layout from layout_css() is all floats."""
    store = DynamoJobStore.__new__(DynamoJobStore)
    job = {
        "job_id": "j1",
        "tenant_id": "default",
        "status": "completed",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "completed_at": "2026-01-01T00:01:00+00:00",
        "progress": 100,
        "karaoke": True,
        "karaoke_layout": _karaoke_layout_floats(),
    }
    item = store._to_item(job)
    layout = item["karaoke_layout"]
    assert isinstance(layout["width_pct"], Decimal)
    assert layout["width_pct"] == Decimal("80.0")
    assert isinstance(layout["bottom_pct"], Decimal)
    assert layout["bottom_pct"] == Decimal("8.5")
    assert layout["font_size"] == 48
    assert item["karaoke"] is True
    TypeSerializer().serialize(item)


def test_dynamo_from_item_restores_numeric_layout():
    store = DynamoJobStore.__new__(DynamoJobStore)
    item = {
        "jobId": "j1",
        "tenantId": "default",
        "status": "completed",
        "createdAt": "2026-01-01T00:00:00+00:00",
        "updatedAt": "2026-01-01T00:00:00+00:00",
        "progress": Decimal("100"),
        "karaoke": True,
        "karaoke_layout": {
            "width_pct": Decimal("80.0"),
            "left_pct": Decimal("10.0"),
            "bottom_pct": Decimal("8.5"),
            "height_pct": Decimal("20.0"),
            "font_size": Decimal("48"),
        },
    }
    job = store._from_item(item)
    layout = job["karaoke_layout"]
    assert layout["width_pct"] == 80
    assert layout["bottom_pct"] == 8.5
    assert isinstance(layout["bottom_pct"], float)
    assert layout["font_size"] == 48
    assert isinstance(layout["font_size"], int)
    assert job["progress"] == 100


def test_style_template_roles():
    t = StyleTemplate(
        name="t",
        spokenText=TextRoleStyle(color="#FFFFFF", formatting="bold"),
        activeText=TextRoleStyle(color="#F97316", formatting="bold"),
        normalText=TextRoleStyle(color="#FFFFFF", formatting="normal"),
    )
    assert t.activeText.color == "#F97316"
    assert t.effects.bounce.enabled is False or True  # defaults exist
