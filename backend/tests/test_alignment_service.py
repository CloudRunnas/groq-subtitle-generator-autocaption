"""A/B unit tests for SRT → word-timing mapping after WhisperX sentence splits.

A = synthetic two-sentence cue
B = real SRT from rassistische-personen (Cue 11 splits, Cue 18 was dropped)
"""
import os
import re
import sys
from collections import defaultdict
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.alignment_service import AlignmentService

FIXTURE_SRT = os.path.join(
    os.path.dirname(__file__), "fixtures", "rassistische_personen.srt"
)

# WhisperX align() splits on sentence boundaries (NLTK Punkt).
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-ZÄÖÜ])")


def parse_srt(path: str) -> List[Dict[str, Any]]:
    raw = open(path, encoding="utf-8").read().strip()
    cues = []
    for block in raw.split("\n\n"):
        lines = [ln for ln in block.split("\n") if ln.strip() != ""]
        if len(lines) < 3:
            continue
        timing = lines[1]
        start_s, end_s = timing.split(" --> ")
        cues.append({
            "text": " ".join(lines[2:]).replace("\n", " ").strip(),
            "start": _srt_ts(start_s),
            "end": _srt_ts(end_s),
        })
    return cues


def _srt_ts(value: str) -> float:
    h, m, rest = value.strip().split(":")
    sec, ms = rest.split(",")
    return int(h) * 3600 + int(m) * 60 + int(sec) + int(ms) / 1000.0


def split_sentences(text: str) -> List[str]:
    parts = _SENTENCE_SPLIT_RE.split((text or "").strip())
    return [p.strip() for p in parts if p.strip()]


def simulate_whisperx_sentence_split(cues: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Reproduce WhisperX emitting one aligned segment per sentence."""
    aligned = []
    for cue in cues:
        sentences = split_sentences(cue["text"])
        duration = max(float(cue["end"]) - float(cue["start"]), 0.05)
        total_chars = sum(len(s) for s in sentences) or 1
        t = float(cue["start"])
        for sent in sentences:
            frac = len(sent) / total_chars
            sent_end = t + duration * frac
            words = AlignmentService._tokenize_words(sent)
            n = max(len(words), 1)
            step = (sent_end - t) / n
            word_objs = []
            for i, word in enumerate(words):
                w_start = t + i * step
                w_end = t + (i + 1) * step
                word_objs.append({
                    "word": word,
                    "start": round(w_start, 3),
                    "end": round(w_end, 3),
                    "score": 0.9,
                })
            aligned.append({
                "text": sent,
                "start": t,
                "end": sent_end,
                "words": word_objs,
            })
            t = sent_end
    return aligned


def legacy_map_aligned_segments_by_index(
    original_segments: List[Dict[str, Any]],
    aligned_segments: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Pre-fix mapping: assume WhisperX output is 1:1 with SRT cues."""
    word_timings: List[Dict[str, Any]] = []
    for cue_index, original in enumerate(original_segments):
        original_words = AlignmentService._tokenize_words(original["text"])
        matched = aligned_segments[cue_index] if cue_index < len(aligned_segments) else None
        aligned_words = []
        if matched and matched.get("words"):
            for w in matched["words"]:
                token = (w.get("word") or "").strip()
                if not token or w.get("start") is None or w.get("end") is None:
                    continue
                aligned_words.append({
                    "word": token,
                    "start": round(float(w["start"]), 3),
                    "end": round(float(w["end"]), 3),
                    "score": w.get("score"),
                    "cue_index": cue_index,
                    "aligned": True,
                })
        if aligned_words and len(aligned_words) == len(original_words):
            for i, ow in enumerate(original_words):
                aligned_words[i]["word"] = ow
            word_timings.extend(aligned_words)
        elif aligned_words:
            word_timings.extend(aligned_words)
        else:
            word_timings.extend(
                AlignmentService._even_word_timings(
                    original_words, original["start"], original["end"], cue_index
                )
            )
    return word_timings


def flatten_text(words: List[Dict[str, Any]]) -> str:
    return " ".join(w["word"] for w in words)


def text_by_cue(words: List[Dict[str, Any]]) -> Dict[int, str]:
    grouped = defaultdict(list)
    for w in words:
        grouped[w["cue_index"]].append(w["word"])
    return {k: " ".join(v) for k, v in grouped.items()}


def synthetic_cues() -> List[Dict[str, Any]]:
    return [
        {"text": "Hallo ich bin Mike.", "start": 0.0, "end": 2.0},
        {
            "text": "Und das ist dann passiert. Ich habe gefragt, was hast du gerade gesagt?",
            "start": 2.5,
            "end": 6.0,
        },
        {"text": "Eine strukturell gewaltbetroffene Person.", "start": 6.5, "end": 8.5},
    ]


# --- A: synthetic two-sentence cue (before / after) ---

def test_a_before_index_mapping_drops_last_cue():
    original = synthetic_cues()
    aligned = simulate_whisperx_sentence_split(original)
    assert len(aligned) == len(original) + 1

    words = legacy_map_aligned_segments_by_index(original, aligned)
    text = flatten_text(words)

    assert "gewaltbetroffene" not in text
    assert original[-1]["text"] not in text


def test_a_after_time_mapping_keeps_every_srt_cue():
    original = synthetic_cues()
    aligned = simulate_whisperx_sentence_split(original)

    words = AlignmentService.map_aligned_segments_to_cues(original, aligned)
    by_cue = text_by_cue(words)

    assert len(by_cue) == len(original)
    for i, cue in enumerate(original):
        assert by_cue[i] == cue["text"]
    assert "gewaltbetroffene" in flatten_text(words)


# --- B: real rassistische-personen SRT (before / after) ---

def test_b_before_index_mapping_drops_cue_18():
    original = parse_srt(FIXTURE_SRT)
    aligned = simulate_whisperx_sentence_split(original)
    assert original[10]["text"].startswith("Und das ist dann passiert.")
    assert len(split_sentences(original[10]["text"])) == 2
    assert len(aligned) == 21
    assert original[17]["text"] == "Eine strukturell gewaltbetroffene Person."

    words = legacy_map_aligned_segments_by_index(original, aligned)
    text = flatten_text(words)

    assert "gewaltbetroffene" not in text
    assert "Eine strukturell gewaltbetroffene Person." not in text
    by_cue = text_by_cue(words)
    assert by_cue.get(17) != original[17]["text"]


def test_b_after_time_mapping_keeps_cue_18_and_all_lines():
    original = parse_srt(FIXTURE_SRT)
    aligned = simulate_whisperx_sentence_split(original)

    words = AlignmentService.map_aligned_segments_to_cues(original, aligned)
    by_cue = text_by_cue(words)
    text = flatten_text(words)

    assert len(original) == 20
    assert len(by_cue) == 20
    for i, cue in enumerate(original):
        assert by_cue[i] == cue["text"], f"cue {i + 1} mismatch: {by_cue.get(i)!r}"

    assert "gewaltbetroffene" in text
    assert by_cue[17] == "Eine strukturell gewaltbetroffene Person."
    assert by_cue[18] == "Ich könnte das sein."
    assert by_cue[19] == "Dann hatte ich Mitleid."
    expected_words = sum(len(AlignmentService._tokenize_words(c["text"])) for c in original)
    assert len(words) == expected_words


def test_missing_aligned_window_falls_back_to_original_srt_text():
    original = [
        {"text": "Erster Satz hier.", "start": 0.0, "end": 1.0},
        {"text": "Eine strukturell gewaltbetroffene Person.", "start": 2.0, "end": 4.0},
    ]
    aligned = simulate_whisperx_sentence_split([original[0]])

    words = AlignmentService.map_aligned_segments_to_cues(original, aligned)
    by_cue = text_by_cue(words)
    assert by_cue[0] == original[0]["text"]
    assert by_cue[1] == original[1]["text"]
    assert all(w["aligned"] is False for w in words if w["cue_index"] == 1)
