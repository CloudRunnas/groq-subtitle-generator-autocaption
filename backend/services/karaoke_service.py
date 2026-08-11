import logging
import re
from dataclasses import dataclass
from typing import List, Dict, Any, Optional, Tuple, Set

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
    bold: int = 1


class KaraokeService:
    """Build karaoke ASS from word-level timings using cue segmentation."""

    def __init__(self, style: Optional[KaraokeStyle] = None):
        self.settings = get_settings()
        self.style = style or KaraokeStyle(
            font_name=self.settings.subtitle_font or "Arial",
            font_size=max(self.settings.subtitle_font_size, 36),
        )
        self.apply_layout_from_resolution(self.style.play_res_x, self.style.play_res_y)

    def _cue_limits(self) -> Tuple[int, int]:
        min_w = max(1, int(self.settings.karaoke_cue_min_words))
        max_w = max(min_w, int(self.settings.karaoke_cue_max_words))
        return min_w, max_w

    def _hard_chars(self) -> Set[str]:
        return set(c for c in (self.settings.karaoke_hard_boundary_chars or "") if c.strip())

    def _soft_chars(self) -> Set[str]:
        return set(c for c in (self.settings.karaoke_soft_boundary_chars or "") if c.strip())

    def _soft_words(self) -> Set[str]:
        raw = self.settings.karaoke_soft_boundary_words or ""
        return {w.strip().lower() for w in raw.split(",") if w.strip()}

    @staticmethod
    def _word_core(text: str) -> str:
        """Lowercased word without trailing punctuation for soft-word matching."""
        return re.sub(r"^[\W_]+|[\W_]+$", "", (text or "").strip(), flags=re.UNICODE).lower()

    def _is_hard_boundary(self, text: str) -> bool:
        hard = self._hard_chars()
        if not hard or not text:
            return False
        # Any hard char in the token (e.g. "Brot." or standalone "?")
        return any(ch in text for ch in hard)

    def _is_soft_boundary(self, text: str) -> bool:
        if not text:
            return False
        soft_chars = self._soft_chars()
        if soft_chars and any(ch in text for ch in soft_chars):
            return True
        core = self._word_core(text)
        return bool(core) and core in self._soft_words()

    def build_cues(self, word_timings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Group words into cues.
        - Each word appears in exactly one cue.
        - Hard boundary (.?!) ends the cue immediately.
        - Soft boundary (,, und, oder) ends only when len(cue) is in [min, max].
        - Always close at max words.
        """
        if not word_timings:
            return []

        min_w, max_w = self._cue_limits()
        cues: List[Dict[str, Any]] = []
        current: List[Dict[str, Any]] = []
        global_indices: List[int] = []

        def close_cue():
            nonlocal current, global_indices
            if not current:
                return
            start = float(current[0]["start"])
            end = float(current[-1]["end"])
            if end <= start:
                end = start + 0.08 * len(current)
            cues.append({
                "start": start,
                "end": end,
                "words": [
                    {
                        **w,
                        "cue_local_index": i,
                        "global_index": global_indices[i],
                    }
                    for i, w in enumerate(current)
                ],
            })
            current = []
            global_indices = []

        for gi, word in enumerate(word_timings):
            text = str(word.get("word") or "").strip()
            if not text:
                continue
            current.append(word)
            global_indices.append(gi)
            n = len(current)

            if self._is_hard_boundary(text):
                close_cue()
            elif n >= max_w:
                close_cue()
            elif n >= min_w and self._is_soft_boundary(text):
                close_cue()

        close_cue()
        logger.info(
            "Built %s karaoke cues from %s words (min=%s max=%s)",
            len(cues), len(word_timings), min_w, max_w,
        )
        return cues

    def cue_rules(self) -> Dict[str, Any]:
        min_w, max_w = self._cue_limits()
        return {
            "min_words": min_w,
            "max_words": max_w,
            "hard_boundary_chars": "".join(sorted(self._hard_chars())),
            "soft_boundary_chars": "".join(sorted(self._soft_chars())),
            "soft_boundary_words": sorted(self._soft_words()),
        }

    def apply_layout_from_resolution(self, width: int, height: int) -> KaraokeStyle:
        """
        Compute ASS margins and max font from percentage layout vars.
        Final font size is fitted later via fit_constant_font_size().
        """
        width = max(int(width or 1920), 160)
        height = max(int(height or 1080), 90)

        h_margin_pct = float(self.settings.subtitle_horizontal_margin_pct)
        v_margin_pct = float(self.settings.subtitle_vertical_margin_pct)
        height_pct = float(self.settings.subtitle_height_pct)
        width_pct = float(self.settings.subtitle_width_pct)

        if abs((1.0 - width_pct) / 2.0 - h_margin_pct) > 0.001:
            h_margin_pct = (1.0 - width_pct) / 2.0

        margin_l = max(0, int(round(width * h_margin_pct)))
        margin_r = max(0, int(round(width * h_margin_pct)))
        margin_v = max(0, int(round(height * v_margin_pct)))

        band_px = max(24, int(round(height * height_pct)))
        self.style.play_res_x = width
        self.style.play_res_y = height
        self.style.margin_l = margin_l
        self.style.margin_r = margin_r
        self.style.margin_v = margin_v
        self._box_width = max(1, width - margin_l - margin_r)
        self._box_height = band_px

        # Max font: largest size where a single line still fits the box height
        safety = max(0.0, float(self.settings.subtitle_fit_safety_pct))
        usable_h = band_px * (1.0 - safety)
        font_max = int((usable_h - 2 * self.style.outline) / 1.25)
        font_min = max(8, int(self.settings.subtitle_font_size_min))
        font_size = max(font_min, font_max)

        self.style.font_size = font_size
        self._font_size_max = font_size

        logger.info(
            "Karaoke layout: %sx%s, box=%sx%s, margins L/R=%s V=%s, font_max=%s "
            "(width=%.0f%% hMargin=%.0f%% vMargin=%.0f%% height=%.0f%%)",
            width, height, self._box_width, self._box_height, margin_l, margin_v, font_size,
            width_pct * 100, h_margin_pct * 100, v_margin_pct * 100, height_pct * 100,
        )
        return self.style

    def _char_width_factor(self) -> float:
        factor = float(self.settings.subtitle_char_width_factor)
        if self.style.bold:
            factor *= 1.05
        return factor

    def _estimate_text_width(self, text: str, font_size: int) -> float:
        """Approximate rendered width for a text run (no wrapping)."""
        return max(0, len(text)) * font_size * self._char_width_factor()

    def _estimate_line_height(self, font_size: int) -> float:
        """Approximate line height including outline."""
        return font_size * 1.25 + 2 * self.style.outline

    def _wrap_words_to_lines(self, words: List[str], font_size: int, usable_w: float) -> List[List[str]]:
        """Greedy word-wrap into lines that fit usable_w (same idea as ASS soft wrap)."""
        if not words:
            return []
        space_w = self._estimate_text_width(" ", font_size)
        lines: List[List[str]] = []
        current: List[str] = []
        current_w = 0.0

        for word in words:
            ww = self._estimate_text_width(word, font_size)
            if not current:
                current = [word]
                current_w = ww
                continue
            needed = current_w + space_w + ww
            if needed <= usable_w:
                current.append(word)
                current_w = needed
            else:
                lines.append(current)
                current = [word]
                current_w = ww

        if current:
            lines.append(current)
        return lines

    def _text_fits_in_box(self, text: str, font_size: int, usable_w: float, usable_h: float) -> bool:
        """True if text can wrap into multiple lines and still fit width×height of the box."""
        words = [w for w in text.split() if w]
        if not words:
            return True

        lines = self._wrap_words_to_lines(words, font_size, usable_w)
        if not lines:
            return True

        # A single token wider than the box cannot wrap — must shrink font
        for line in lines:
            line_text = " ".join(line)
            if self._estimate_text_width(line_text, font_size) > usable_w:
                return False

        total_h = len(lines) * self._estimate_line_height(font_size)
        return total_h <= usable_h

    def _cue_plain_text(self, cue: Dict[str, Any]) -> str:
        return " ".join(
            str(w.get("word") or "").strip()
            for w in (cue.get("words") or [])
            if str(w.get("word") or "").strip()
        )

    def _max_font_for_text(self, text: str, box_w: float, box_h: float, font_max: int, font_min: int) -> int:
        """Largest font size in [font_min, font_max] that fits wrapped text into the box."""
        if not text:
            return font_max

        safety = max(0.0, float(self.settings.subtitle_fit_safety_pct))
        usable_w = box_w * (1.0 - safety)
        usable_h = box_h * (1.0 - safety)

        lo, hi = font_min, font_max
        best = font_min
        while lo <= hi:
            mid = (lo + hi) // 2
            if self._text_fits_in_box(text, mid, usable_w, usable_h):
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1
        return best

    def fit_constant_font_size(self, cues: List[Dict[str, Any]]) -> int:
        """
        Pick one constant font size for the whole video:
        the minimum of the per-cue max sizes that still fit each cue
        when wrapped onto multiple lines inside the subtitle box.
        """
        font_max = int(getattr(self, "_font_size_max", self.style.font_size) or self.style.font_size)
        font_min = max(8, int(self.settings.subtitle_font_size_min))
        if font_min > font_max:
            font_min = font_max

        box_w = float(getattr(self, "_box_width", self.style.play_res_x * 0.8))
        box_h = float(getattr(self, "_box_height", self.style.play_res_y * 0.15))

        if not cues:
            self.style.font_size = font_max
            return font_max

        fitted = font_max
        limiting_text = ""
        limiting_lines = 1
        for cue in cues:
            text = self._cue_plain_text(cue)
            if not text:
                continue
            cue_size = self._max_font_for_text(text, box_w, box_h, font_max, font_min)
            if cue_size < fitted:
                fitted = cue_size
                limiting_text = text
                safety = max(0.0, float(self.settings.subtitle_fit_safety_pct))
                limiting_lines = max(
                    1,
                    len(self._wrap_words_to_lines(
                        text.split(), fitted, box_w * (1.0 - safety),
                    )),
                )

        self.style.font_size = fitted
        logger.info(
            "Fitted constant karaoke font_size=%s (min=%s max=%s box=%.0fx%.0f, ~%s lines); "
            "limiting cue=%r",
            fitted, font_min, font_max, box_w, box_h, limiting_lines,
            (limiting_text[:60] + "…") if len(limiting_text) > 60 else limiting_text,
        )
        return fitted

    def layout_css(self) -> Dict[str, Any]:
        """CSS-friendly layout + fitted font for the live overlay."""
        return {
            "width_pct": float(self.settings.subtitle_width_pct) * 100,
            "left_pct": float(self.settings.subtitle_horizontal_margin_pct) * 100,
            "bottom_pct": float(self.settings.subtitle_vertical_margin_pct) * 100,
            "height_pct": float(self.settings.subtitle_height_pct) * 100,
            "font_size": int(self.style.font_size),
            "font_size_min": int(self.settings.subtitle_font_size_min),
            "font_size_max": int(getattr(self, "_font_size_max", self.style.font_size)),
            "play_res_x": int(self.style.play_res_x),
            "play_res_y": int(self.style.play_res_y),
            "box_width": int(getattr(self, "_box_width", 0)),
            "box_height": int(getattr(self, "_box_height", 0)),
        }

    @staticmethod
    def _ass_time(seconds: float) -> str:
        if seconds < 0:
            seconds = 0.0
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = seconds % 60
        return f"{hours}:{minutes:02d}:{secs:05.2f}"

    @staticmethod
    def _escape_ass_text(text: str) -> str:
        return (
            text.replace("\\", "\\\\")
            .replace("{", "\\{")
            .replace("}", "\\}")
            .replace("\n", " ")
        )

    def _wrap_cue_word_indices(self, cue_words: List[Dict[str, Any]]) -> List[List[int]]:
        """Split cue word indices into display lines using the fitted font + box width."""
        plain = [
            str(w.get("word") or "").strip() or " "
            for w in cue_words
        ]
        safety = max(0.0, float(self.settings.subtitle_fit_safety_pct))
        usable_w = float(getattr(self, "_box_width", self.style.play_res_x * 0.8)) * (1.0 - safety)
        lines_words = self._wrap_words_to_lines(plain, int(self.style.font_size), usable_w)

        # Map wrapped plain tokens back to indices (1:1 with cue_words order)
        indices: List[List[int]] = []
        cursor = 0
        for line in lines_words:
            n = len(line)
            indices.append(list(range(cursor, cursor + n)))
            cursor += n
        if cursor < len(cue_words):
            indices.append(list(range(cursor, len(cue_words))))
        return indices or [list(range(len(cue_words)))]

    def _format_cue_line(self, cue_words: List[Dict[str, Any]], active_local_index: int) -> str:
        """Format cue with karaoke colors and hard line breaks matching the fit wrap."""
        style = self.style
        line_groups = self._wrap_cue_word_indices(cue_words)
        ass_lines = []
        for group in line_groups:
            parts = []
            for i in group:
                token = self._escape_ass_text(str(cue_words[i].get("word") or ""))
                if i == active_local_index:
                    parts.append(
                        f"{{\\c{style.highlight_color}&\\b1}}{token}{{\\c{style.primary_color}&\\b0}}"
                    )
                else:
                    parts.append(f"{{\\c{style.primary_color}&}}{token}")
            ass_lines.append(" ".join(parts))
        return "\\N".join(ass_lines)

    def generate_ass_content(
        self,
        word_timings: List[Dict[str, Any]],
        video_size: Optional[Tuple[int, int]] = None,
    ) -> str:
        """
        Create ASS with cue-based dialogue events.
        Full cue text stays visible; highlight advances word-by-word inside the cue.
        """
        if video_size:
            self.apply_layout_from_resolution(video_size[0], video_size[1])

        if not word_timings:
            return self._ass_header() + "\n"

        cues = self.build_cues(word_timings)
        self.fit_constant_font_size(cues)
        lines = [self._ass_header()]
        event_count = 0

        for cue in cues:
            cue_words = cue["words"]
            cue_end = float(cue["end"])
            for local_i, word in enumerate(cue_words):
                start = float(word["start"])
                end = float(word["end"])
                if end <= start:
                    end = start + 0.08

                # Extend into gap before next word in the same cue
                if local_i + 1 < len(cue_words):
                    next_start = float(cue_words[local_i + 1]["start"])
                    if next_start > end:
                        end = min(end + 0.05, next_start)
                else:
                    # Last word in cue: keep highlight until cue end
                    end = max(end, cue_end)

                text = self._format_cue_line(cue_words, local_i)
                lines.append(
                    f"Dialogue: 0,{self._ass_time(start)},{self._ass_time(end)},Karaoke,,0,0,0,,{text}"
                )
                event_count += 1

        logger.info(
            "Generated karaoke ASS with %s events across %s cues",
            event_count, len(cues),
        )
        return "\n".join(lines) + "\n"

    def _ass_header(self) -> str:
        s = self.style
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
            "WrapStyle: 2",
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

    def cue_for_time(
        self,
        word_timings: List[Dict[str, Any]],
        current_time: float,
    ) -> Dict[str, Any]:
        """Return the active cue and highlighted word at current_time."""
        cues = self.build_cues(word_timings)
        if not cues:
            return {"cue_index": -1, "active_local_index": -1, "words": []}

        active_cue_index = -1
        for ci, cue in enumerate(cues):
            if float(cue["start"]) <= current_time <= float(cue["end"]):
                active_cue_index = ci
                break
            if current_time >= float(cue["start"]):
                active_cue_index = ci

        if active_cue_index < 0:
            return {"cue_index": -1, "active_local_index": -1, "words": []}

        cue = cues[active_cue_index]
        active_local = -1
        for i, w in enumerate(cue["words"]):
            if float(w["start"]) <= current_time < float(w["end"]):
                active_local = i
                break
            if current_time >= float(w["start"]):
                active_local = i

        if active_local < 0:
            active_local = 0

        return {
            "cue_index": active_cue_index,
            "active_local_index": active_local,
            "start": cue["start"],
            "end": cue["end"],
            "words": [
                {
                    **w,
                    "is_active": i == active_local,
                    "index": w.get("global_index", i),
                }
                for i, w in enumerate(cue["words"])
            ],
        }

    # Backwards-compatible alias
    def window_for_time(
        self,
        word_timings: List[Dict[str, Any]],
        current_time: float,
    ) -> Dict[str, Any]:
        return self.cue_for_time(word_timings, current_time)
