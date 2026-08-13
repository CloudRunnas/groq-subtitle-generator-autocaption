import asyncio
import gc
import logging
import re
from typing import List, Dict, Optional, Any

logger = logging.getLogger(__name__)

_TOKEN_NORM_RE = re.compile(r"[^\wßäöüÄÖÜ]+", re.UNICODE)


class AlignmentService:
    """
    Forced alignment of SRT cue text to audio via WhisperX (no re-transcription).
    Defaults to German wav2vec2 / VoxPopuli align models.
    """

    def __init__(self):
        from utils.config import get_settings
        self.settings = get_settings()
        self._align_model = None
        self._align_metadata = None
        self._loaded_language: Optional[str] = None
        self._device = "cpu"

    def _ensure_model(self, language_code: str):
        if self._align_model is not None and self._loaded_language == language_code:
            return

        import whisperx

        self.release_model()
        logger.info(f"Loading WhisperX align model for language={language_code} on {self._device}")
        self._align_model, self._align_metadata = whisperx.load_align_model(
            language_code=language_code,
            device=self._device,
        )
        self._loaded_language = language_code
        logger.info("WhisperX align model loaded")

    def release_model(self):
        self._align_model = None
        self._align_metadata = None
        self._loaded_language = None
        gc.collect()

    @staticmethod
    def segments_from_srt_cues(cues: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Normalize cue dicts for whisperx.align."""
        segments = []
        for cue in cues:
            text = (cue.get("text") or "").replace("\n", " ").strip()
            if not text:
                continue
            segments.append({
                "text": text,
                "start": float(cue["start"]),
                "end": float(cue["end"]),
            })
        return segments

    @staticmethod
    def _tokenize_words(text: str) -> List[str]:
        return [w for w in text.split() if w.strip()]

    @staticmethod
    def _normalize_token(text: str) -> str:
        return _TOKEN_NORM_RE.sub("", (text or "")).lower()

    @staticmethod
    def _even_word_timings(
        words: List[str],
        start: float,
        end: float,
        cue_index: int,
        aligned: bool = False,
    ) -> List[Dict[str, Any]]:
        """Fallback: distribute words evenly across the cue interval."""
        if not words:
            return []
        duration = max(end - start, 0.05)
        step = duration / len(words)
        timings = []
        for i, word in enumerate(words):
            w_start = start + i * step
            w_end = start + (i + 1) * step if i < len(words) - 1 else end
            timings.append({
                "word": word,
                "start": round(w_start, 3),
                "end": round(w_end, 3),
                "score": None,
                "cue_index": cue_index,
                "aligned": aligned,
            })
        return timings

    @staticmethod
    def _flatten_aligned_words(aligned_segments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        pool: List[Dict[str, Any]] = []
        for seg in aligned_segments or []:
            for w in seg.get("words") or []:
                token = (w.get("word") or "").strip()
                if not token:
                    continue
                if w.get("start") is None or w.get("end") is None:
                    continue
                pool.append({
                    "word": token,
                    "start": float(w["start"]),
                    "end": float(w["end"]),
                    "score": w.get("score"),
                })
        pool.sort(key=lambda w: (w["start"], w["end"]))
        return pool

    @staticmethod
    def _apply_times_to_original_words(
        original_words: List[str],
        candidates: List[Dict[str, Any]],
        cue_start: float,
        cue_end: float,
        cue_index: int,
    ) -> List[Dict[str, Any]]:
        """
        Always emit the original SRT tokens. Attach WhisperX timestamps when possible.
        """
        if not original_words:
            return []
        if not candidates:
            return AlignmentService._even_word_timings(
                original_words, cue_start, cue_end, cue_index, aligned=False
            )

        candidates = sorted(candidates, key=lambda w: w["start"])

        if len(candidates) == len(original_words):
            return [
                {
                    "word": ow,
                    "start": round(float(cw["start"]), 3),
                    "end": round(float(cw["end"]), 3),
                    "score": cw.get("score"),
                    "cue_index": cue_index,
                    "aligned": True,
                }
                for ow, cw in zip(original_words, candidates)
            ]

        # Sequential match by normalized token (punctuation / casing may differ)
        picked: List[Optional[Dict[str, Any]]] = [None] * len(original_words)
        used = [False] * len(candidates)
        cursor = 0
        for oi, ow in enumerate(original_words):
            needle = AlignmentService._normalize_token(ow)
            if not needle:
                continue
            for cj in range(cursor, len(candidates)):
                if used[cj]:
                    continue
                if AlignmentService._normalize_token(candidates[cj]["word"]) == needle:
                    picked[oi] = candidates[cj]
                    used[cj] = True
                    cursor = cj + 1
                    break

        span_start = float(candidates[0]["start"])
        span_end = float(candidates[-1]["end"])
        if span_end <= span_start:
            span_end = span_start + 0.05 * len(original_words)
        base = AlignmentService._even_word_timings(
            original_words, span_start, span_end, cue_index, aligned=True
        )
        for i, match in enumerate(picked):
            if match is None:
                continue
            base[i]["start"] = round(float(match["start"]), 3)
            base[i]["end"] = round(float(match["end"]), 3)
            base[i]["score"] = match.get("score")
        return base

    @staticmethod
    def map_aligned_segments_to_cues(
        original_segments: List[Dict[str, Any]],
        aligned_segments: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """
        Map WhisperX align() output back onto the original SRT cues.

        WhisperX splits cues on sentence boundaries, so it may return more
        segments than SRT cues. Matching by list index drops later cue text.
        Group aligned words by time window instead, and always keep SRT tokens.
        """
        pool = AlignmentService._flatten_aligned_words(aligned_segments)
        claimed = [False] * len(pool)
        word_timings: List[Dict[str, Any]] = []
        n = len(original_segments)

        for cue_index, original in enumerate(original_segments):
            original_words = AlignmentService._tokenize_words(original["text"])
            cue_start = float(original["start"])
            cue_end = float(original["end"])
            prev_end = float(original_segments[cue_index - 1]["end"]) if cue_index > 0 else None
            next_start = (
                float(original_segments[cue_index + 1]["start"]) if cue_index + 1 < n else None
            )
            bound_left = ((prev_end + cue_start) / 2.0) if prev_end is not None else (cue_start - 0.05)
            bound_right = ((cue_end + next_start) / 2.0) if next_start is not None else (cue_end + 0.05)

            idxs: List[int] = []
            for i, w in enumerate(pool):
                if claimed[i]:
                    continue
                mid = (w["start"] + w["end"]) / 2.0
                if bound_left <= mid <= bound_right:
                    idxs.append(i)

            candidates = [pool[i] for i in idxs]
            mapped = AlignmentService._apply_times_to_original_words(
                original_words, candidates, cue_start, cue_end, cue_index
            )
            if candidates and any(item.get("aligned") for item in mapped):
                for i in idxs:
                    claimed[i] = True
            word_timings.extend(mapped)

        return word_timings

    def _align_sync(
        self,
        audio_path: str,
        segments: List[Dict[str, Any]],
        language_code: str,
    ) -> List[Dict[str, Any]]:
        import whisperx

        if not segments:
            return []

        self._ensure_model(language_code)
        audio = whisperx.load_audio(audio_path)

        try:
            result = whisperx.align(
                segments,
                self._align_model,
                self._align_metadata,
                audio,
                self._device,
                return_char_alignments=False,
            )
        except Exception as e:
            logger.warning(f"WhisperX align failed, using even fallback for all cues: {e}")
            word_timings: List[Dict[str, Any]] = []
            for cue_index, seg in enumerate(segments):
                words = self._tokenize_words(seg["text"])
                word_timings.extend(
                    self._even_word_timings(words, seg["start"], seg["end"], cue_index)
                )
            return word_timings

        aligned_segments = result.get("segments") or []
        return self.map_aligned_segments_to_cues(segments, aligned_segments)

    async def align_segments(
        self,
        audio_path: str,
        segments: List[Dict[str, Any]],
        language_code: str = "de",
        release_after: bool = True,
    ) -> List[Dict[str, Any]]:
        """
        Align cue segments to audio and return flat word timings.
        """
        normalized = self.segments_from_srt_cues(segments)
        lang = (language_code or "de").lower()

        loop = asyncio.get_event_loop()
        try:
            word_timings = await loop.run_in_executor(
                None,
                self._align_sync,
                audio_path,
                normalized,
                lang,
            )
            logger.info(f"Aligned {len(word_timings)} words from {len(normalized)} cues")
            return word_timings
        finally:
            if release_after:
                self.release_model()
