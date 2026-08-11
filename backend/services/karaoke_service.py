import logging
from dataclasses import dataclass
from typing import List, Dict, Any, Optional, Tuple

from utils.config import get_settings

logger = logging.getLogger(__name__)


@dataclass
class KaraokeStyle:
    """Central style params so designs can be swapped later."""
    font_name: str = "Arial"
    font_size: int = 42
    primary_color: str = "&H00FFFFFF"      # white (ASS BGR)
    highlight_color: str = "&H001673F9"    # orange accent ~#F97316
    outline_color: str = "&H00000000"      # black
    back_color: str = "&H80000000"
    outline: int = 3
    shadow: int = 0
    alignment: int = 2  # bottom-center
    margin_l: int = 192
    margin_r: int = 192
    margin_v: int = 54
    play_res_x: int = 1920
    play_res_y: int = 1080
    window_size: int = 5
    bold: int = 1


class KaraokeService:
    """Build karaoke ASS from word-level timings."""

    def __init__(self, style: Optional[KaraokeStyle] = None):
        self.settings = get_settings()
        self.style = style or KaraokeStyle(
            font_name=self.settings.subtitle_font or "Arial",
            font_size=max(self.settings.subtitle_font_size, 36),
            window_size=int(getattr(self.settings, "karaoke_window_size", 5) or 5),
        )
        # Apply percentage layout defaults for fallback resolution
        self.apply_layout_from_resolution(self.style.play_res_x, self.style.play_res_y)

    def apply_layout_from_resolution(self, width: int, height: int) -> KaraokeStyle:
        """
        Compute ASS margins/font from percentage layout vars.
        - width 80% centered => 10% left + 10% right
        - vertical margin 5% from bottom
        - height band 15% drives font size
        """
        width = max(int(width or 1920), 160)
        height = max(int(height or 1080), 90)

        h_margin_pct = float(self.settings.subtitle_horizontal_margin_pct)
        v_margin_pct = float(self.settings.subtitle_vertical_margin_pct)
        height_pct = float(self.settings.subtitle_height_pct)
        width_pct = float(self.settings.subtitle_width_pct)

        # Keep horizontal margins consistent with width (prefer explicit H-margin)
        if abs((1.0 - width_pct) / 2.0 - h_margin_pct) > 0.001:
            h_margin_pct = (1.0 - width_pct) / 2.0

        margin_l = max(0, int(round(width * h_margin_pct)))
        margin_r = max(0, int(round(width * h_margin_pct)))
        margin_v = max(0, int(round(height * v_margin_pct)))

        # Fit a single karaoke line into ~45% of the subtitle height band
        band_px = max(24, int(round(height * height_pct)))
        font_size = max(24, int(round(band_px * 0.45)))

        self.style.play_res_x = width
        self.style.play_res_y = height
        self.style.margin_l = margin_l
        self.style.margin_r = margin_r
        self.style.margin_v = margin_v
        self.style.font_size = font_size

        logger.info(
            "Karaoke layout: %sx%s, margins L/R=%s V=%s, font=%s "
            "(width=%.0f%% hMargin=%.0f%% vMargin=%.0f%% height=%.0f%%)",
            width, height, margin_l, margin_v, font_size,
            width_pct * 100, h_margin_pct * 100, v_margin_pct * 100, height_pct * 100,
        )
        return self.style

    def layout_css(self) -> Dict[str, float]:
        """CSS-friendly percentages for the live overlay."""
        return {
            "width_pct": float(self.settings.subtitle_width_pct) * 100,
            "left_pct": float(self.settings.subtitle_horizontal_margin_pct) * 100,
            "bottom_pct": float(self.settings.subtitle_vertical_margin_pct) * 100,
            "height_pct": float(self.settings.subtitle_height_pct) * 100,
        }

    @staticmethod
    def _ass_time(seconds: float) -> str:
        if seconds < 0:
            seconds = 0.0
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = seconds % 60
        # ASS uses h:mm:ss.cs (centiseconds)
        return f"{hours}:{minutes:02d}:{secs:05.2f}"

    @staticmethod
    def _escape_ass_text(text: str) -> str:
        return (
            text.replace("\\", "\\\\")
            .replace("{", "\\{")
            .replace("}", "\\}")
            .replace("\n", " ")
        )

    def _window_indices(self, active_index: int, total: int) -> range:
        window = max(1, self.style.window_size)
        half = window // 2
        start = max(0, active_index - half)
        end = min(total, start + window)
        start = max(0, end - window)
        return range(start, end)

    def _format_window_line(self, words: List[Dict[str, Any]], active_index: int) -> str:
        style = self.style
        parts = []
        for i in self._window_indices(active_index, len(words)):
            token = self._escape_ass_text(words[i]["word"])
            if i == active_index:
                parts.append(
                    f"{{\\c{style.highlight_color}&\\b1}}{token}{{\\c{style.primary_color}&\\b0}}"
                )
            else:
                parts.append(f"{{\\c{style.primary_color}&}}{token}")
        return " ".join(parts)

    def generate_ass_content(
        self,
        word_timings: List[Dict[str, Any]],
        video_size: Optional[Tuple[int, int]] = None,
    ) -> str:
        """
        Create ASS with one dialogue event per spoken word.
        Each event shows a small running window with the active word highlighted.
        """
        if video_size:
            self.apply_layout_from_resolution(video_size[0], video_size[1])

        if not word_timings:
            return self._ass_header() + "\n"

        lines = [self._ass_header()]

        for i, word in enumerate(word_timings):
            start = float(word["start"])
            end = float(word["end"])
            if end <= start:
                end = start + 0.08

            # Extend slightly into next word gap for smoother highlight
            if i + 1 < len(word_timings):
                next_start = float(word_timings[i + 1]["start"])
                if next_start > end:
                    end = min(end + 0.05, next_start)

            text = self._format_window_line(word_timings, i)
            lines.append(
                f"Dialogue: 0,{self._ass_time(start)},{self._ass_time(end)},Karaoke,,0,0,0,,{text}"
            )

        logger.info(f"Generated karaoke ASS with {len(word_timings)} word events")
        return "\n".join(lines) + "\n"

    def _ass_header(self) -> str:
        s = self.style
        # Format: Name, Fontname, Fontsize, Primary, Secondary, Outline, Back,
        # Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle,
        # BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
        style_line = (
            f"Style: Karaoke,{s.font_name},{s.font_size},"
            f"{s.primary_color},{s.highlight_color},{s.outline_color},{s.back_color},"
            f"{s.bold},0,0,0,100,100,0,0,1,{s.outline},{s.shadow},{s.alignment},"
            f"{s.margin_l},{s.margin_r},{s.margin_v},1"
        )
        return "\n".join([
            "[Script Info]",
            "Title: Karaoke Subtitles",
            "ScriptType: v4.00+",
            "WrapStyle: 0",
            "ScaledBorderAndShadow: yes",
            f"PlayResX: {s.play_res_x}",
            f"PlayResY: {s.play_res_y}",
            "YCbCr Matrix: None",
            "",
            "[V4+ Styles]",
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
            "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, "
            "Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
            style_line,
            "",
            "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        ])

    def window_for_time(
        self,
        word_timings: List[Dict[str, Any]],
        current_time: float,
    ) -> Dict[str, Any]:
        """Helper for API/tests: which window is active at current_time."""
        if not word_timings:
            return {"active_index": -1, "words": []}

        active_index = -1
        for i, w in enumerate(word_timings):
            if float(w["start"]) <= current_time < float(w["end"]):
                active_index = i
                break
            if current_time >= float(w["start"]):
                active_index = i

        if active_index < 0:
            return {"active_index": -1, "words": []}

        indices = list(self._window_indices(active_index, len(word_timings)))
        return {
            "active_index": active_index,
            "words": [
                {
                    **word_timings[i],
                    "is_active": i == active_index,
                    "index": i,
                }
                for i in indices
            ],
        }
