"""Pydantic models for autocaption style templates."""
from __future__ import annotations

from typing import Literal, Optional
from pydantic import BaseModel, Field, field_validator
import re

Formatting = Literal["normal", "bold", "italic", "bold_italic"]
HEX_RE = re.compile(r"^#?[0-9A-Fa-f]{6}$")


def normalize_hex(value: str) -> str:
    v = (value or "").strip()
    if not HEX_RE.match(v):
        raise ValueError(f"Invalid hex color: {value}")
    if not v.startswith("#"):
        v = f"#{v}"
    return v.upper()


class TextRoleStyle(BaseModel):
    color: str = "#FFFFFF"
    formatting: Formatting = "normal"
    strokeColor: Optional[str] = None
    strokeWidth: Optional[float] = None

    @field_validator("color", mode="before")
    @classmethod
    def _color(cls, v):
        return normalize_hex(v)

    @field_validator("strokeColor", mode="before")
    @classmethod
    def _stroke_color(cls, v):
        if v is None or v == "":
            return None
        return normalize_hex(v)


class BounceEffect(BaseModel):
    enabled: bool = False
    scalePercent: float = Field(135, ge=100, le=250)
    upMs: int = Field(80, ge=0, le=2000)
    downMs: int = Field(100, ge=0, le=2000)


class OutlinePulseEffect(BaseModel):
    enabled: bool = False
    peakWidth: float = Field(10, ge=0, le=40)
    upMs: int = Field(100, ge=0, le=2000)
    downMs: int = Field(120, ge=0, le=2000)


class TemplateEffects(BaseModel):
    bounce: BounceEffect = Field(default_factory=BounceEffect)
    outlinePulse: OutlinePulseEffect = Field(default_factory=OutlinePulseEffect)


class StyleTemplate(BaseModel):
    version: int = 1
    name: str = Field(..., min_length=1, max_length=80)
    slug: Optional[str] = None
    fontS3Key: Optional[str] = None
    fontName: Optional[str] = None  # display / ASS Fontname override
    strokeColor: str = "#000000"
    strokeWidth: float = Field(4, ge=0, le=40)
    spokenText: TextRoleStyle = Field(default_factory=lambda: TextRoleStyle(color="#FFFFFF", formatting="bold"))
    activeText: TextRoleStyle = Field(default_factory=lambda: TextRoleStyle(color="#F97316", formatting="bold"))
    normalText: TextRoleStyle = Field(default_factory=lambda: TextRoleStyle(color="#FFFFFF", formatting="normal"))
    effects: TemplateEffects = Field(default_factory=TemplateEffects)

    @field_validator("strokeColor", mode="before")
    @classmethod
    def _stroke(cls, v):
        return normalize_hex(v)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str):
        return v.strip()


class StyleTemplateSummary(BaseModel):
    name: str
    slug: str
    fontS3Key: Optional[str] = None
    strokeWidth: float = 4
    activeTextColor: str = "#F97316"


class FontAsset(BaseModel):
    name: str
    s3Key: str
    contentType: str = "font/ttf"
