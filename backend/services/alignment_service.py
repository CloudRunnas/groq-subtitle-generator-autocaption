import asyncio
import gc
import logging
from typing import List, Dict, Optional, Any

from utils.config import get_settings

logger = logging.getLogger(__name__)


class AlignmentService:
    """
    Forced alignment of SRT cue text to audio via WhisperX (no re-transcription).
    Defaults to German wav2vec2 / VoxPopuli align models.
    """

    def __init__(self):
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
    def _even_word_timings(
        words: List[str],
        start: float,
        end: float,
        cue_index: int,
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
                "aligned": False,
            })
        return timings

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
        word_timings = []

        for cue_index, original in enumerate(segments):
            original_words = self._tokenize_words(original["text"])
            matched = None
            if cue_index < len(aligned_segments):
                matched = aligned_segments[cue_index]

            aligned_words = []
            if matched and matched.get("words"):
                for w in matched["words"]:
                    token = (w.get("word") or "").strip()
                    if not token:
                        continue
                    if w.get("start") is None or w.get("end") is None:
                        continue
                    aligned_words.append({
                        "word": token,
                        "start": round(float(w["start"]), 3),
                        "end": round(float(w["end"]), 3),
                        "score": w.get("score"),
                        "cue_index": cue_index,
                        "aligned": True,
                    })

            # Prefer original casing/punctuation when counts match
            if aligned_words and len(aligned_words) == len(original_words):
                for i, ow in enumerate(original_words):
                    aligned_words[i]["word"] = ow
                word_timings.extend(aligned_words)
            elif aligned_words:
                # Keep aligned tokens; may differ slightly in tokenization
                word_timings.extend(aligned_words)
            else:
                logger.warning(
                    f"No word alignments for cue {cue_index}; using even distribution"
                )
                word_timings.extend(
                    self._even_word_timings(
                        original_words,
                        original["start"],
                        original["end"],
                        cue_index,
                    )
                )

        return word_timings

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
