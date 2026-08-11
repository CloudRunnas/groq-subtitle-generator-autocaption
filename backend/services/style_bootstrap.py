"""Seed default style templates and generate ASS stroke-width previews."""
from __future__ import annotations

import asyncio
import logging
import subprocess
import tempfile
from pathlib import Path

from models.style_template import (
    BounceEffect,
    OutlinePulseEffect,
    StyleTemplate,
    TemplateEffects,
    TextRoleStyle,
)
from services.style_storage_service import StyleStorageService
from utils.config import get_settings

logger = logging.getLogger(__name__)

STROKE_PREVIEW_WIDTHS = [i * 0.5 for i in range(1, 17)]  # 0.5 .. 8.0  (16 steps)


def default_templates() -> list[StyleTemplate]:
    """Seed styles: neon variants + orange classic (+ bounce) and a few useful presets."""
    white_bold = TextRoleStyle(color="#FFFFFF", formatting="bold", strokeColor="#000000", strokeWidth=3)
    white_normal = TextRoleStyle(color="#FFFFFF", formatting="normal", strokeColor="#000000", strokeWidth=3)
    orange_active = TextRoleStyle(color="#F97316", formatting="bold", strokeColor="#000000", strokeWidth=3)

    return [
        # Previous default look (white + orange active) + Bounce only
        StyleTemplate(
            name="Orange Bounce",
            slug="orange-bounce",
            fontS3Key="fonts/Ubuntu-B.ttf",
            fontName="Ubuntu-B",
            strokeColor="#000000",
            strokeWidth=3,
            spokenText=white_bold.model_copy(),
            activeText=orange_active.model_copy(),
            normalText=white_normal.model_copy(),
            effects=TemplateEffects(
                bounce=BounceEffect(enabled=True, scalePercent=135, upMs=80, downMs=100),
                outlinePulse=OutlinePulseEffect(enabled=False, peakWidth=8, upMs=100, downMs=120),
            ),
        ),
        # Same orange look + Bounce + Outline-Pulse
        StyleTemplate(
            name="Orange Classic",
            slug="orange-classic",
            fontS3Key="fonts/Ubuntu-B.ttf",
            fontName="Ubuntu-B",
            strokeColor="#000000",
            strokeWidth=3,
            spokenText=white_bold.model_copy(update={"strokeWidth": 3}),
            activeText=orange_active.model_copy(),
            normalText=white_normal.model_copy(),
            effects=TemplateEffects(
                bounce=BounceEffect(enabled=True, scalePercent=130, upMs=80, downMs=100),
                outlinePulse=OutlinePulseEffect(enabled=True, peakWidth=8, upMs=100, downMs=120),
            ),
        ),
        # Legacy flat highlight (no motion) — closest to original ASS behavior
        StyleTemplate(
            name="Orange Flat",
            slug="orange-flat",
            fontS3Key="fonts/Ubuntu-R.ttf",
            fontName="Ubuntu-R",
            strokeColor="#000000",
            strokeWidth=3,
            spokenText=TextRoleStyle(color="#FFFFFF", formatting="bold", strokeColor="#000000", strokeWidth=3),
            activeText=TextRoleStyle(color="#F97316", formatting="bold", strokeColor="#000000", strokeWidth=3),
            normalText=TextRoleStyle(color="#FFFFFF", formatting="normal", strokeColor="#000000", strokeWidth=3),
            effects=TemplateEffects(
                bounce=BounceEffect(enabled=False),
                outlinePulse=OutlinePulseEffect(enabled=False),
            ),
        ),
        StyleTemplate(
            name="Neon Gelb",
            slug="neon-gelb",
            fontS3Key="fonts/Ubuntu-B.ttf",
            fontName="Ubuntu-B",
            strokeColor="#000000",
            strokeWidth=4,
            spokenText=TextRoleStyle(color="#FFFFFF", formatting="bold", strokeColor="#000000", strokeWidth=4),
            activeText=TextRoleStyle(color="#F7FF00", formatting="bold", strokeColor="#000000", strokeWidth=4),
            normalText=TextRoleStyle(color="#FFFFFF", formatting="normal", strokeColor="#000000", strokeWidth=4),
            effects=TemplateEffects(
                bounce=BounceEffect(enabled=True, scalePercent=135, upMs=80, downMs=100),
                outlinePulse=OutlinePulseEffect(enabled=True, peakWidth=10, upMs=100, downMs=120),
            ),
        ),
        StyleTemplate(
            name="Neon Lila",
            slug="neon-lila",
            fontS3Key="fonts/Ubuntu-B.ttf",
            fontName="Ubuntu-B",
            strokeColor="#000000",
            strokeWidth=5,
            spokenText=TextRoleStyle(color="#FFFFFF", formatting="bold", strokeColor="#000000", strokeWidth=5),
            activeText=TextRoleStyle(color="#D946EF", formatting="bold", strokeColor="#000000", strokeWidth=5),
            normalText=TextRoleStyle(color="#FFFFFF", formatting="normal", strokeColor="#000000", strokeWidth=5),
            effects=TemplateEffects(
                bounce=BounceEffect(enabled=True, scalePercent=140, upMs=70, downMs=110),
                outlinePulse=OutlinePulseEffect(enabled=True, peakWidth=12, upMs=90, downMs=130),
            ),
        ),
        StyleTemplate(
            name="Cyan Pulse",
            slug="cyan-pulse",
            fontS3Key="fonts/UbuntuMono-B.ttf",
            fontName="UbuntuMono-B",
            strokeColor="#000000",
            strokeWidth=4,
            spokenText=TextRoleStyle(color="#E5E7EB", formatting="normal", strokeColor="#000000", strokeWidth=4),
            activeText=TextRoleStyle(color="#22D3EE", formatting="bold", strokeColor="#000000", strokeWidth=4),
            normalText=TextRoleStyle(color="#FFFFFF", formatting="normal", strokeColor="#000000", strokeWidth=4),
            effects=TemplateEffects(
                bounce=BounceEffect(enabled=False),
                outlinePulse=OutlinePulseEffect(enabled=True, peakWidth=11, upMs=90, downMs=140),
            ),
        ),
    ]


def ensure_seed_templates(storage: StyleStorageService) -> None:
    for tmpl in default_templates():
        # Always refresh seed templates so font keys stay in sync during development
        storage.save_template(tmpl)
        logger.info("Seeded style template: %s", tmpl.slug)


def _ass_stroke_preview(stroke_width: float, font_name: str = "Arial") -> str:
    # neon yellow fill, black stroke — PlayRes matches preview frame
    bord = max(0, float(stroke_width))
    return "\n".join([
        "[Script Info]",
        "ScriptType: v4.00+",
        "PlayResX: 640",
        "PlayResY: 180",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, "
        "Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Preview,{font_name},54,&H0000FFF7,&H0000FFF7,&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,{bord:.1f},0,5,20,20,20,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        "Dialogue: 0,0:00:00.00,0:00:01.00,Preview,,0,0,0,,Textumrandung",
        "",
    ])


def generate_stroke_previews(storage: StyleStorageService, force: bool = False) -> list[dict]:
    """Render 16 stroke-width preview PNGs via ASS + FFmpeg (no Pillow)."""
    results = []
    for i, width in enumerate(STROKE_PREVIEW_WIDTHS):
        out_path = storage.stroke_preview_path(i)
        if out_path.exists() and not force:
            results.append({"index": i, "strokeWidth": width, "path": str(out_path)})
            continue
        with tempfile.TemporaryDirectory() as tmp:
            ass_path = Path(tmp) / "preview.ass"
            png_path = Path(tmp) / "out.png"
            ass_path.write_text(_ass_stroke_preview(width), encoding="utf-8")
            ass_esc = str(ass_path).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
            cmd = [
                "ffmpeg", "-y",
                "-f", "lavfi", "-i", "color=c=0x1a1a1a:s=640x180:d=0.12",
                "-vf", f"subtitles='{ass_esc}'",
                "-frames:v", "1",
                str(png_path),
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode != 0 or not png_path.exists():
                logger.error("Stroke preview %s failed: %s", i, proc.stderr[-800:])
                continue
            png_bytes = png_path.read_bytes()
            storage.put_stroke_preview(i, png_bytes)
            results.append({"index": i, "strokeWidth": width, "path": str(out_path)})
    return results


async def bootstrap_styles() -> StyleStorageService:
    settings = get_settings()
    storage = StyleStorageService(settings)
    ensure_seed_templates(storage)
    await asyncio.to_thread(generate_stroke_previews, storage, False)
    return storage
