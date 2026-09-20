"""Final Cut 사용자 지정 공유 대상이 보낸 최종 오디오의 자막 생성 경계."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path

from vlogsrt import pipeline, transcribe
from vlogsrt.config import Config
from vlogsrt.project.fcpxml import element_tree
from vlogsrt.project.workflow_protocol import CaptionRequest, CaptionResult


class CaptionGenerationError(RuntimeError):
    """최종 자막을 성공으로 공개할 수 없는 입력 또는 실행 실패."""


@dataclass(frozen=True, slots=True)
class CaptionDelivery:
    result: CaptionResult
    review_report: Path
    estimated_cost: float


def create_caption_request(
    origin_revision: str, render_audio: Path, final_fcpxml: Path
) -> CaptionRequest:
    """공유 동작을 현재 타임라인의 명시적 승인 요청으로 고정한다."""
    try:
        raw = final_fcpxml.read_bytes()
    except OSError as exc:
        raise CaptionGenerationError(f"최종 FCPXML을 읽을 수 없다: {final_fcpxml}") from exc
    return CaptionRequest(
        origin_revision=origin_revision,
        final_revision=hashlib.sha256(raw).hexdigest(),
        render_audio=render_audio,
        final_fcpxml=final_fcpxml,
    )


def generate_final_captions(
    request: CaptionRequest,
    config: Config,
    destination: Path,
    *,
    online: bool,
    confirmed_cost: bool,
) -> CaptionDelivery:
    """최종 48kHz PCM을 새로 전사하고 SRT와 검토 보고서를 원자적으로 공개한다."""
    duration = _validate_pcm(request.render_audio)
    project_name = _validate_final_xml(request)
    estimated_cost = transcribe.estimate_cost(duration, config) * 2
    if online and not confirmed_cost:
        raise CaptionGenerationError(
            f"예상 비용 ${estimated_cost:.3f}를 확인해야 온라인 전사를 시작할 수 있다"
        )
    destination.mkdir(parents=True, exist_ok=True)
    stem = _safe_filename(project_name)
    basename = f"{stem}.{request.final_revision}.ko"
    review_path = destination / f"{basename}.review.json"
    try:
        with tempfile.TemporaryDirectory(prefix=".caption-", dir=destination) as temporary:
            staging = Path(temporary)
            try:
                run = pipeline.run(
                    request.render_audio,
                    config,
                    staging / "out",
                    online=online,
                    force_offline=not online,
                    workdir=staging / "work",
                    artifacts=True,
                    verify_with_second_model=True,
                    repair_repeats=True,
                    require_complete_coverage=True,
                )
            except transcribe.EmptyTranscript:
                result = CaptionResult(
                    final_revision=request.final_revision,
                    srt_path=None,
                    warnings=("발화 없음",),
                    quality=("coverage:complete",),
                    speech_status="no_speech",
                )
                _write_review(review_path, request, result, estimated_cost)
                return CaptionDelivery(result, review_path, estimated_cost)
            source_srt = run.outputs.get("srt")
            if source_srt is None or not source_srt.is_file():
                raise CaptionGenerationError("전사는 끝났지만 SRT 파일이 없다")
            if source_srt.stat().st_size == 0:
                raise CaptionGenerationError("0바이트 SRT는 성공으로 공개할 수 없다")
            if not run.cues:
                raise CaptionGenerationError("발화 결과에 자막 큐가 하나도 없다")
            target_srt = destination / f"{basename}.srt"
            os.replace(source_srt, target_srt)
            quality = ["coverage:complete", "render-audio:48khz-pcm"]
            if run.verification is not None:
                quality.append("second-model:checked")
            result = CaptionResult(
                final_revision=request.final_revision,
                srt_path=target_srt,
                warnings=(),
                quality=tuple(quality),
                speech_status="speech",
            )
            _write_review(review_path, request, result, estimated_cost)
            return CaptionDelivery(result, review_path, estimated_cost)
    except CaptionGenerationError:
        raise
    except Exception as exc:
        raise CaptionGenerationError(f"최종 자막 생성 실패: {exc}") from exc


def _validate_pcm(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as stream:
            rate = stream.getframerate()
            compression = stream.getcomptype()
            frames = stream.getnframes()
    except (OSError, EOFError, wave.Error) as exc:
        raise CaptionGenerationError(f"렌더 오디오가 읽을 수 있는 WAV가 아니다: {path}") from exc
    if rate != 48_000 or compression != "NONE":
        raise CaptionGenerationError(
            f"렌더 오디오는 48000Hz PCM WAV여야 한다: {rate}Hz, {compression}"
        )
    if frames <= 0:
        raise CaptionGenerationError("렌더 오디오에 샘플이 없다")
    return frames / rate


def _validate_final_xml(request: CaptionRequest) -> str:
    try:
        raw = request.final_fcpxml.read_bytes()
    except OSError as exc:
        raise CaptionGenerationError(f"최종 FCPXML을 읽을 수 없다: {request.final_fcpxml}") from exc
    actual = hashlib.sha256(raw).hexdigest()
    if actual != request.final_revision:
        raise CaptionGenerationError(
            f"최종 FCPXML 리비전이 요청과 다르다: {actual} ≠ {request.final_revision}"
        )
    try:
        root = element_tree(raw, str(request.final_fcpxml))
    except Exception as exc:
        raise CaptionGenerationError(f"최종 FCPXML을 해석할 수 없다: {exc}") from exc
    project = root.find("library/event/project")
    name = project.get("name", "").strip() if project is not None else ""
    if not name:
        raise CaptionGenerationError("최종 FCPXML에 프로젝트 이름이 없다")
    return name


def _safe_filename(value: str) -> str:
    safe = "".join("_" if char in "/:\\" or ord(char) < 32 else char for char in value).strip()
    return safe or "Final Cut Project"


def _write_review(
    path: Path, request: CaptionRequest, result: CaptionResult, estimated_cost: float
) -> None:
    payload = {
        "caption_import": {
            "command": "File > Import > Captions",
            "placement": "Relative to Timeline",
        },
        "estimated_cost": estimated_cost,
        "final_revision": request.final_revision,
        "origin_revision": request.origin_revision,
        "quality": list(result.quality),
        "speech_status": result.speech_status,
        "timeline_approval": "custom_share_destination",
        "warnings": list(result.warnings),
    }
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)
