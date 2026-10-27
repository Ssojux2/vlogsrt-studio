"""최종 자막 산출물 게시(A7-2) — 초안 SRT/VTT와 불변 기록을 원자적으로 남긴다.

`docs/plans/2026-10-02-raw-video-browser-v2.md` §11.7. 이 모듈은 **초안까지만** 게시한다
— `final.*` 게시·사람 검토 확정·수동 patch 적용은 A7-3의 권한이다. 산출물:

    draft.<lang>.srt / draft.<lang>.vtt   # 검토 전 결과(상태와 무관하게 초안 이름)
    caption_trace.json                    # 실행 전체의 불변 기록
    alignment_report.json                 # 문구 대응·검증 표시
    quality_report.json                   # 상태·검토 항목·커버리지
    provider_snapshot.json                # 동결 설정·기능 등록표·요금 스냅샷
    responses/<request_id>.json           # 요청 하나의 불변 응답 증거

모든 `.srt`/`.vtt`는 **직렬화 뒤 다시 파싱해 같은지 확인하고** 게시한다(§11.7 —
재파싱 검사 후 staging에서 원자적으로 게시). 검사를 통과하지 못하면 아무 파일도
그 자리에 남지 않는다. 같은 자리에는 같은 실행(리비전·run_id·지문이 전부 같은)의
다시 게시만 허용된다 — 다른 실행의 자리 위에 덮어쓰는 길은 없다(루트 재검).
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

from vlogsrt import oscompat
from vlogsrt.models import Cue
from vlogsrt.output import format_timecode, parse_srt, write_srt
from vlogsrt.project.raw import capabilities
from vlogsrt.project.raw.capabilities import ModelCapabilitySnapshot
from vlogsrt.project.raw.caption_config import DEFAULT_DRAFT_LANGUAGE
from vlogsrt.project.raw.config import (
    RawWorkflowConfig,
    raw_workflow_config_to_json,
)
from vlogsrt.project.raw.final_caption import (
    CaptionCue,
    CaptionRunPlan,
    FinalCaptionResult,
    ResponseEvidence,
)
from vlogsrt.project.raw.understanding import fraction_json, selected_audio_json

SRT_NAME = "draft.{lang}.srt"
VTT_NAME = "draft.{lang}.vtt"
TRACE_NAME = "caption_trace.json"
ALIGNMENT_NAME = "alignment_report.json"
QUALITY_NAME = "quality_report.json"
PROVIDER_NAME = "provider_snapshot.json"
RESPONSES_DIR = "responses"
MANUAL_EDITS_NAME = "manual_edits.jsonl"
"""수동 patch 기록의 자리 — A7-3이 소유한다. 여기서는 만들지도 읽지도 않는다."""

_VTT_TIMELINE = r"(\d{2}):(\d{2}):(\d{2})\.(\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})\.(\d{3})"
VTT_TIME = re.compile(rf"^\s*{_VTT_TIMELINE}")


class ArtifactVerificationError(RuntimeError):
    """직렬화한 파일을 다시 파싱해 같지 않다 — 그 파일은 게시되지 않는다(§11.7)."""


class ArtifactRevisionMismatch(RuntimeError):
    """게시 directory가 다른 실행(렌더·run·지문)의 기록이다 — 섞어 쓸 수 없다(§11.1).

    같은 자리에 예전 실행의 응답·기록이 남은 채 새 실행을 게시하면 두 실행의
    증거가 한 폴더에서 서로를 인용하게 된다. 리비전·run_id·지문 중 어느 하나라도
    다르면 새 실행 디렉터리다. 같은 실행의 다시 게시만 같은 자리에 허용된다.
    """


@dataclass(frozen=True, slots=True)
class CaptionArtifactSet:
    """한 실행이 게시한 파일들 — 전부 원자적 바꿈치기로 나갔다."""

    srt: Path
    vtt: Path
    trace: Path
    alignment: Path
    quality: Path
    provider: Path
    responses: tuple[Path, ...]

    def paths(self) -> tuple[Path, ...]:
        return (self.srt, self.vtt, self.trace, self.alignment, self.quality, self.provider)


# ─────────────────────────────────────────────────────────────
#  VTT 코덱 — SRT와 같은 큐 자료의 별도 직렬화(§11.7)
# ─────────────────────────────────────────────────────────────


def format_vtt_timecode(seconds: Fraction | float) -> str:
    """`HH:MM:SS.mmm` — SRT와 같은 반올림, 마침표만 다르다."""
    return format_timecode(float(seconds)).replace(",", ".")


def to_engine_cues(cues: Sequence[CaptionCue]) -> list[Cue]:
    """최종 자막 큐를 엔진 큐로 — 시각은 이미 최종 영상 시각이다(환산은 끝났다)."""
    return [
        Cue(
            start=float(cue.start),
            end=float(cue.end),
            lines=(cue.text,),
            seg_ids=cue.timing_segment_ids,
            index=position + 1,
        )
        for position, cue in enumerate(cues)
    ]


def write_vtt(cues: Sequence[CaptionCue]) -> str:
    blocks = ["WEBVTT", ""]
    for number, cue in enumerate(cues, start=1):
        timeline = f"{format_vtt_timecode(cue.start)} --> {format_vtt_timecode(cue.end)}"
        blocks.append(f"{number}\n{timeline}\n{cue.text}\n")
    return "\n".join(blocks)


def parse_vtt(text: str) -> list[tuple[Fraction, Fraction, str]]:
    """VTT를 다시 읽는다 — 게시 검사용 코덱. 큐 외 블록은 실패가 아니라 건너뛴다."""
    body = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    cues: list[tuple[Fraction, Fraction, str]] = []
    position = 0
    while position < len(body):
        match = VTT_TIME.match(body[position])
        if not match:
            position += 1
            continue
        groups = match.groups()
        start = _vtt_time((groups[0], groups[1], groups[2], groups[3]))
        end = _vtt_time((groups[4], groups[5], groups[6], groups[7]))
        position += 1
        lines: list[str] = []
        while position < len(body) and body[position].strip():
            lines.append(body[position])
            position += 1
        cues.append((start, end, "\n".join(lines)))
    return cues


def _vtt_time(parts: tuple[str, str, str, str]) -> Fraction:
    hours, minutes, seconds, millis = (int(part) for part in parts)
    total_ms = ((hours * 60 + minutes) * 60 + seconds) * 1000 + millis
    return Fraction(total_ms, 1000)


# ─────────────────────────────────────────────────────────────
#  게시 — 재파싱 검사 뒤 원자적 바꿈치기
# ─────────────────────────────────────────────────────────────


def caption_srt_text(result: FinalCaptionResult) -> str:
    """SRT 본문 — 엔진 코덱으로 직렬화한다(UTF-8, 1부터 번호, 빈 줄 구분)."""
    return write_srt(to_engine_cues(result.cues))


def caption_vtt_text(result: FinalCaptionResult) -> str:
    """VTT 본문 — 같은 큐 자료에서 별도 직렬화한다."""
    return write_vtt(result.cues)


def verify_serialization(srt_text: str, vtt_text: str) -> None:
    """직렬화한 파일을 다시 파싱해 **글자 그대로** 같은지 본다(§11.7).

    큐 줄이 타임코드처럼 생겨 파싱이 어긋나는 경우가 진짜 위반이다 — 그런 큐는
    게시되지 못하게 이 문에서 닫는다. 시각은 밀리초 직렬화가 원본 유리수의
    반올림이므로, 되읽은 값이 직렬화 값과 정확히 같은지로 판정한다.
    """
    reparsed = write_srt(parse_srt(srt_text))
    if reparsed != srt_text:
        raise ArtifactVerificationError(
            "SRT 재파싱이 직렬화와 같지 않다 — 큐 본문이 타임라인처럼 읽힐 수 있다"
        )
    vtt_cues = parse_vtt(vtt_text)
    rebuilt = write_vtt(_vtt_cues_back(vtt_cues))
    if rebuilt != vtt_text:
        raise ArtifactVerificationError("VTT 재파싱이 직렬화와 같지 않다")


def _vtt_cues_back(cues: list[tuple[Fraction, Fraction, str]]) -> list[CaptionCue]:
    """되읽은 VTT 큐를 다시 직렬화 비교용 껍데기로 — 근거 칸은 비운다(비교용일 뿐)."""
    return [
        CaptionCue(
            start=start,
            end=end,
            text=text,
            whisper_text="",
            timing_segment_ids=(),
            timing_evidence_id="",
            text_evidence_ids=(),
            text_source="",
            timing_source="",
            correction_status="",
        )
        for start, end, text in cues
    ]


def publish_caption_artifacts(
    outdir: Path,
    result: FinalCaptionResult,
    plan: CaptionRunPlan,
    *,
    language: str = DEFAULT_DRAFT_LANGUAGE,
) -> CaptionArtifactSet:
    """실행 결과를 게시한다 — 검사를 통과한 내용만 원자적으로 나간다.

    응답 증거→기록·보고서→초안 SRT/VTT 순서로 바꿔치기한다. 중간 산출물을 완성
    결과로 노출하지 않는다는 규칙(§14.1)의 파일 판이다 — SRT가 보이면 나머지도
    전부 그 실행의 것이다.
    """
    srt_text = caption_srt_text(result)
    vtt_text = caption_vtt_text(result)
    verify_serialization(srt_text, vtt_text)
    _require_revision_match(outdir, result)
    outdir.mkdir(parents=True, exist_ok=True)
    responses_dir = outdir / RESPONSES_DIR
    responses_dir.mkdir(parents=True, exist_ok=True)
    response_paths = tuple(
        _atomic_write(responses_dir / f"{evidence.request_id}.json", _response_body(evidence))
        for evidence in result.evidence
    )
    trace = _atomic_write(outdir / TRACE_NAME, _trace_body(result, plan, language))
    alignment = _atomic_write(outdir / ALIGNMENT_NAME, _alignment_body(result))
    quality = _atomic_write(outdir / QUALITY_NAME, _quality_body(result))
    provider = _atomic_write(outdir / PROVIDER_NAME, _provider_body(plan.workflow))
    srt = _atomic_write_text(outdir / SRT_NAME.format(lang=language), srt_text)
    vtt = _atomic_write_text(outdir / VTT_NAME.format(lang=language), vtt_text)
    return CaptionArtifactSet(
        srt=srt,
        vtt=vtt,
        trace=trace,
        alignment=alignment,
        quality=quality,
        provider=provider,
        responses=response_paths,
    )


def _atomic_write(path: Path, payload: object) -> Path:
    """JSON을 원자적으로 게시한다 — 부분 파일이 목격되는 길을 닫는다(§14.1)."""
    body = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    return _atomic_write_text(path, body)


def _require_revision_match(outdir: Path, result: FinalCaptionResult) -> None:
    """같은 자리에 다른 실행의 기록이 있으면 닫는다 — 렌더·run·지문을 안 섞는다.

    빈 directory는 새 실행의 자리다. trace가 있는데 리비전·run_id·지문 중 하나라도
    다르면 다른 실행의 잔해다 — 그 위에 덮어쓰면 두 실행의 응답이 한 폴더에서
    섞인다(§11.1). 세 개가 전부 같은 다시 게시만 같은 자리에 남는다.
    """
    trace = outdir / TRACE_NAME
    if not trace.is_file():
        return
    try:
        previous = json.loads(trace.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise ArtifactRevisionMismatch(
            f"게시 자리의 지난 기록을 읽을 수 없다: {trace} — 새 실행 디렉터리를 써라"
        ) from None
    identity = {
        "final_video_revision": result.final_video_revision,
        "run_id": result.run_id,
        "fingerprint": result.fingerprint,
    }
    for name, current in identity.items():
        recorded = str(previous.get(name, ""))
        if recorded != str(current):
            raise ArtifactRevisionMismatch(
                f"게시 자리가 다른 실행의 기록이다 — {name}: {recorded[:12]}… ≠"
                f" {str(current)[:12]}… — 렌더·run·지문마다 새 실행 디렉터리다(§11.1)"
            )


def _atomic_write_text(path: Path, body: str) -> Path:
    temp = path.with_name(path.name + ".partial")
    try:
        temp.write_text(body, encoding="utf-8")
        oscompat.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    return path


# ─────────────────────────────────────────────────────────────
#  보고서 본체 — 전부 값이다. 여기서 판정하지 않는다
# ─────────────────────────────────────────────────────────────


def _response_body(evidence: ResponseEvidence) -> dict[str, object]:
    """응답 하나의 기록 — 신원과 원 payload를 나란히(§11.3 — 대응의 증명)."""
    return {
        "request_id": evidence.request_id,
        "role": evidence.role,
        "chunk_index": evidence.chunk_index,
        "audio_sha256": evidence.audio_sha256,
        "sample_start": evidence.sample_start,
        "sample_end": evidence.sample_end,
        "sample_rate": evidence.sample_rate,
        "response": evidence.payload,
    }


def _cue_body(cue: CaptionCue) -> dict[str, object]:
    """큐 하나의 기록 — 시각은 유리수 원문과 밀리초 표기를 함께 남긴다."""
    return {
        "start": fraction_json(cue.start),
        "end": fraction_json(cue.end),
        "start_seconds": float(cue.start),
        "end_seconds": float(cue.end),
        "text": cue.text,
        "whisper_text": cue.whisper_text,
        "timing_segment_ids": list(cue.timing_segment_ids),
        "timing_evidence_id": cue.timing_evidence_id,
        "text_evidence_ids": list(cue.text_evidence_ids),
        "text_source": cue.text_source,
        "timing_source": cue.timing_source,
        "correction_status": cue.correction_status,
        "flags": list(cue.flags),
    }


def _trace_body(
    result: FinalCaptionResult, plan: CaptionRunPlan, language: str
) -> dict[str, object]:
    """실행 전체의 불변 기록 — 골격은 시간 제공자 글자 그대로다(증거)."""
    return {
        "schema": plan.settings.schema_version,
        "run_id": result.run_id,
        "status": result.status,
        "is_draft": result.is_draft,
        "draft_language": language,
        "final_video_revision": result.final_video_revision,
        "fingerprint": result.fingerprint,
        "video_duration": plan.video.video_duration,
        "audio": selected_audio_json(plan.video.audio),
        "spans": [[span.start, span.end] for span in plan.spans],
        "chunks": [asdict(record) for record in result.chunks],
        "spine": [asdict(segment) for segment in result.spine],
        "repairs": [asdict(repair) for repair in result.repairs],
        "retext": result.retext.to_dict(),
        "cues": [_cue_body(cue) for cue in result.cues],
        "cancellation_reason": result.cancellation_reason,
    }


def _alignment_body(result: FinalCaptionResult) -> dict[str, object]:
    """문구 대응 기록 — 수락·반려·미대응·제외와 검증 표시(§11.5)."""
    return {
        "schema": "raw-caption-alignment/v1",
        "run_id": result.run_id,
        "retext_status": result.retext.status,
        "chunks": [asdict(chunk) for chunk in result.retext.chunks],
        "accepted": {str(seg_id): text for seg_id, text in result.retext.texts},
        "rejections": [asdict(item) for item in result.retext.rejections],
        "unmapped_text": [asdict(item) for item in result.retext.unplaced],
        "excluded": [asdict(item) for item in result.retext.excluded],
        "verify_rows": [asdict(row) for row in result.verify_rows],
    }


def _quality_body(result: FinalCaptionResult) -> dict[str, object]:
    """품질 보고 — 상태·검토 항목·커버리지. coverage 100%는 정확도 증명이 아니다."""
    timing_ok = sum(1 for record in result.chunks if record.timing_ok)
    text_ok = sum(1 for record in result.chunks if record.text_ok)
    return {
        "schema": "raw-caption-quality/v1",
        "run_id": result.run_id,
        "status": result.status,
        "is_draft": result.is_draft,
        "speech_basis": result.speech_basis,
        "cue_count": len(result.cues),
        "chunk_count": len(result.chunks),
        "timing_chunks_ok": timing_ok,
        "text_chunks_ok": text_ok,
        "findings": [asdict(item) for item in result.findings],
        "cancellation_reason": result.cancellation_reason,
    }


def _provider_body(workflow: RawWorkflowConfig) -> dict[str, object]:
    """공급자 스냅샷 — 동결 설정·기능 등록표·요금(§11.7 provider_snapshot)."""
    return {
        "schema": "raw-caption-provider/v1",
        "workflow": json.loads(raw_workflow_config_to_json(workflow)),
        "capabilities": [
            _capability_body(snapshot) for snapshot in capabilities.confirmed_capability_snapshots()
        ],
    }


def _capability_body(snapshot: ModelCapabilitySnapshot) -> dict[str, object]:
    """등록표 스냅샷 — date는 ISO로, Decimal은 문자열로(부동소수점을 요금에 안 섞는다)."""
    body = asdict(snapshot)
    for key, value in body.items():
        if isinstance(value, date):
            body[key] = value.isoformat()
        elif isinstance(value, list):
            body[key] = [_json_scalar(item) for item in value]
    pricing = body.get("pricing_snapshot")
    if isinstance(pricing, dict):
        for key, value in pricing.items():
            pricing[key] = _json_scalar(value)
    return body


def _json_scalar(value: object) -> object:
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value
