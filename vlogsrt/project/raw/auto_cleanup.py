"""Conservative source-clock decisions. Missing speech alone never removes video."""

from __future__ import annotations

import sys
from array import array
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

from vlogsrt.project.workflow_analysis import (
    AnalysisSettings,
    TranscriptEvidence,
    safe_transcript_candidates,
)
from vlogsrt.project.workflow_protocol import RationalRange

VERSION = "raw-auto-cleanup/v1"
SILENCE_SECONDS = Fraction(2)
SPEECH_HANDLE = Fraction(3, 10)


@dataclass(frozen=True)
class Decision:
    start: Fraction
    end: Fraction
    reason: str
    evidence: tuple[str, ...]

    def document(self) -> dict:
        return {
            "start": {"num": self.start.numerator, "den": self.start.denominator},
            "end": {"num": self.end.numerator, "den": self.end.denominator},
            "reason": self.reason,
            "evidence": list(self.evidence),
            "version": VERSION,
        }


def fraction(value: dict) -> Fraction:
    return Fraction(value["num"], value["den"])


def decisions(
    document: dict,
    *,
    source_id: str,
    revision_id: str,
    pcm_path: Path | None = None,
    audio_start: Fraction = Fraction(0),
) -> tuple[Decision, ...]:
    """Successful dialogue evidence plus complete followup or measured quiet is required.

    Times are common-video clock seconds. Scene/action/ambient windows, failed ASR,
    uncertain annotations and unconfirmed complete takes are conservatively retained.
    """
    asr = document.get("asr", {}).get("chunks", [])
    if not asr or any(row.get("failure") for row in asr):
        return ()
    annotations = {row["window_id"]: row for row in document.get("annotations", [])}
    windows = document.get("windows", {}).get("windows", [])

    def safe(start: Fraction, end: Fraction) -> bool:
        touched = [
            w
            for w in windows
            if fraction(w["core_start"]) < end and fraction(w["core_end"]) > start
        ]
        if not touched:
            return False
        cursor = start
        for window in sorted(touched, key=lambda w: fraction(w["core_start"])):
            if fraction(window["core_start"]) > cursor:
                return False
            annotation = annotations.get(window["window_id"], {})
            tags = {str(t).lower() for t in annotation.get("scene_tags", [])}
            if (
                annotation.get("status") != "accepted"
                or annotation.get("needs_review", True)
                or annotation.get("uncertainties")
                or not tags.intersection({"dialogue", "talking", "interview", "대화", "인터뷰"})
                or tags.intersection(
                    {"action", "scenery", "ambient", "music", "행동", "풍경", "현장음"}
                )
            ):
                return False
            cursor = max(cursor, fraction(window["core_end"]))
        return cursor >= end

    utterances = sorted(document.get("utterances", []), key=lambda u: fraction(u["start"]))
    spoken = [
        u
        for u in utterances
        if str(u.get("text", "")).strip() and fraction(u["end"]) > fraction(u["start"])
    ]
    rows = [
        TranscriptEvidence(
            str(u["utterance_id"]),
            source_id,
            RationalRange(fraction(u["start"]), fraction(u["end"])),
            RationalRange(fraction(u["start"]), fraction(u["end"])),
            str(u["text"]),
            bool(u.get("complete", str(u["text"]).rstrip().endswith((".", "!", "?", "。")))),
        )
        for u in spoken
    ]
    # Legacy NG/followup logic, with exact repetition required for automatic deletion.
    seeds = safe_transcript_candidates(
        rows, revision_id, AnalysisSettings(retake_similarity=Decimal("1"))
    )
    result = [
        Decision(s.source_range.start, s.source_range.end, s.reason, s.evidence)
        for s in seeds
        if safe(s.source_range.start, s.source_range.end)
    ]
    # Prevent chains from removing a candidate's only replacement.
    protected = {
        e.split(":", 1)[1] for d in result for e in d.evidence if e.startswith("followup:")
    }
    result = [
        d
        for d in result
        if not any(
            u.utterance_id in protected
            and u.source_range.start < d.end
            and u.source_range.end > d.start
            for u in rows
        )
    ]
    audio = document.get("audio", {})
    if pcm_path is not None and pcm_path.is_file() and len(spoken) >= 2:
        channels = int(audio.get("channels", 1))
        rate = int(audio.get("sample_rate", 16000))
        if channels < 1 or rate < 1:
            return tuple(result)
        for left, right in zip(spoken, spoken[1:], strict=False):
            a, b = fraction(left["end"]), fraction(right["start"])
            if b - a < SILENCE_SECONDS or not safe(a, b):
                continue
            start, end = a + SPEECH_HANDLE, b - SPEECH_HANDLE
            first, last = int((start - audio_start) * rate), int((end - audio_start) * rate)
            if first < 0 or last <= first:
                continue
            with pcm_path.open("rb") as handle:
                handle.seek(first * channels * 2)
                raw = handle.read((last - first) * channels * 2)
            samples = array("h")
            if len(raw) != (last - first) * channels * 2:
                continue
            samples.frombytes(raw)
            if sys.byteorder != "little":
                samples.byteswap()
            if samples and max(abs(x) for x in samples) <= 103:  # -50 dBFS peak
                result.append(
                    Decision(
                        start,
                        end,
                        "internal_silence",
                        (
                            "pcm:peak<=-50dBFS",
                            f"speech_handle:{SPEECH_HANDLE}",
                            f"between:{left['utterance_id']}:{right['utterance_id']}",
                        ),
                    )
                )
    return tuple(sorted(result, key=lambda d: d.start))


def saved_decisions(
    document_path: Path, record: dict, source_id: str, revision_id: str
) -> list[dict]:
    """Read old successful caches without rewriting them or sending new requests."""
    import json

    if "auto_decisions" in record:
        return record["auto_decisions"]
    document = json.loads(document_path.read_text(encoding="utf-8"))
    common_start = record.get("timeline", {}).get("audio", {}).get("common_start")
    return [
        decision.document()
        for decision in decisions(
            document,
            source_id=source_id,
            revision_id=revision_id,
            pcm_path=document_path.parent / "analysis.pcm",
            audio_start=fraction(common_start) if common_start else Fraction(0),
        )
    ]
