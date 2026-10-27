"""Final Cut 사용자 지정 공유 대상이 보낸 최종 오디오의 자막 생성 경계."""

from __future__ import annotations

import hashlib
import json
import tempfile
import wave
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

from vlogsrt import oscompat, pipeline, retext, transcribe
from vlogsrt.config import Config
from vlogsrt.project.fcpxml import element_tree
from vlogsrt.project.timebase import parse_fcpx_time
from vlogsrt.project.workflow_protocol import (
    CaptionEstimate,
    CaptionRequest,
    CaptionResult,
    PricingSnapshot,
)


class CaptionGenerationError(RuntimeError):
    """최종 자막을 성공으로 공개할 수 없는 입력 또는 실행 실패."""


@dataclass(frozen=True, slots=True)
class CaptionDelivery:
    result: CaptionResult
    review_report: Path
    estimated_cost: float


@dataclass(frozen=True, slots=True)
class CaptionOrigin:
    """공유 받기 내보내기가 어늌 러프컷 job에서 왔는지의 기록(S4A).

    앱이 잡을 만들 때 스테이징한다. 엔진은 이 문서로 결과 프로젝트 UID와 요청의
    원본 리비전이 같은 내보내기인지 잰다 — UID가 다르면 남의 타임라인에 자막을
    얹을 뻔한 것이다.
    """

    origin_job_id: str
    origin_revision: str
    project_uid: str

    def __post_init__(self) -> None:
        if not self.origin_job_id:
            raise ValueError("자막 원본 job ID가 비어 있다")
        if len(self.origin_revision) != 64 or any(
            char not in "0123456789abcdef" for char in self.origin_revision
        ):
            raise ValueError("자막 원본 리비전이 SHA-256이 아니다")
        if not self.project_uid:
            raise ValueError("자막 원본 프로젝트 UID가 비어 있다")


def load_caption_origin(path: Path) -> CaptionOrigin:
    """앱이 스테이징한 원본 매핑 문서를 읽는다 — 없으면 그 자체가 실패다.

    원본 오디오로 되돌아가는 길은 없다(S4A). 매핑을 모르는 내보내기에서 자막을
    만들면 어느 타임라인의 것인지 말할 수 없으므로 조용히 진행하지 않는다.
    """
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CaptionGenerationError(f"자막 원본 매핑 문서가 없다: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise CaptionGenerationError(f"자막 원본 매핑 문서를 해석할 수 없다: {path}") from exc
    if not isinstance(document, dict):
        raise CaptionGenerationError("자막 원본 매핑 문서가 객체가 아니다")
    try:
        return CaptionOrigin(
            origin_job_id=str(document["origin_job_id"]),
            origin_revision=str(document["origin_revision"]),
            project_uid=str(document["project_uid"]),
        )
    except (KeyError, ValueError) as exc:
        raise CaptionGenerationError(f"자막 원본 매핑 문서의 계약이 깨졌다: {exc}") from exc


@dataclass(frozen=True, slots=True)
class ValidatedCaptionInput:
    """추정·실행 앞에서 함께 잰 최종 내보내기의 사실."""

    audio_seconds: Fraction
    project_name: str
    timeline_duration: Fraction
    project_uid: str


def validate_caption_request(
    request: CaptionRequest, origin: CaptionOrigin
) -> ValidatedCaptionInput:
    """추정 직전·실행 직전에 같은 문으로 잰다 — 재검증이지 캐시가 아니다.

    PCM 모양·오디오 해시·XML 해시·결과 프로젝트 UID·길이 대응·원본 매핑 전부를
    하나의 실패로 닫는다. 원본 소스 오디오로 되돌아가는 갈림길은 없다.
    """
    audio_seconds = _validate_pcm(request)
    project_name, timeline_duration, project_uid = _validate_final_xml(request)
    if abs(audio_seconds - timeline_duration) > Fraction(1, 48_000):
        raise CaptionGenerationError(
            f"렌더 오디오와 최종 타임라인 길이가 다르다: "
            f"{float(audio_seconds):.6f}초 ≠ {float(timeline_duration):.6f}초"
        )
    if project_uid != origin.project_uid:
        if not project_uid:
            raise CaptionGenerationError("최종 FCPXML에 프로젝트 UID가 없다 — 매핑할 수 없다")
        raise CaptionGenerationError(
            f"최종 FCPXML의 프로젝트 UID가 원본 매핑과 다르다: {project_uid} ≠ {origin.project_uid}"
        )
    if request.origin_revision != origin.origin_revision:
        raise CaptionGenerationError(
            "자막 요청의 원본 리비전이 매핑 문서와 다르다: "
            f"{request.origin_revision} ≠ {origin.origin_revision}"
        )
    return ValidatedCaptionInput(
        audio_seconds=audio_seconds,
        project_name=project_name,
        timeline_duration=timeline_duration,
        project_uid=project_uid,
    )


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
        render_audio_sha256=_file_sha256(render_audio),
        final_fcpxml=final_fcpxml,
    )


def measure_caption_request(request: CaptionRequest) -> Fraction:
    """승인 전에 돈이 되는 입력만 검사한다 — 48kHz PCM 길이를 돌려준다."""
    return _validate_pcm(request)


def caption_settings_sha256(config: Config) -> str:
    """자막 생성에 영향을 주는 엔진 설정의 지문 — 추정·승인이 여기에 묶인다."""
    payload = {
        "transcribe_model": config.transcribe_model,
        "verify_model": config.verify_model,
        "transcribe_temperature": repr(config.transcribe_temperature),
        "chunk_max_seconds": config.chunk_max_seconds,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def build_caption_estimate(
    request: CaptionRequest,
    *,
    audio_seconds: Fraction,
    pricing: PricingSnapshot,
    settings_sha256: str,
    transcription_call_multiplier: int = 2,
) -> CaptionEstimate:
    """whisper 1차＋gpt-transcribe 교정(×2)의 공식 가격 상한을 고정한다."""
    if audio_seconds <= 0:
        raise CaptionGenerationError("자막 오디오 길이는 양수여야 한다")
    if transcription_call_multiplier < 1:
        raise ValueError("전사 호출 배수는 1 이상이어야 한다")
    transcription_seconds = audio_seconds * transcription_call_multiplier
    cost = (
        Decimal(transcription_seconds.numerator)
        / Decimal(transcription_seconds.denominator)
        / Decimal(60)
        * (Decimal(pricing.whisper_per_minute) + Decimal(pricing.gpt_transcribe_per_minute))
    )
    return CaptionEstimate.create(
        request_revision=request.request_revision,
        origin_revision=request.origin_revision,
        final_revision=request.final_revision,
        render_audio_sha256=request.render_audio_sha256,
        audio_seconds=audio_seconds,
        transcription_seconds=transcription_seconds,
        cost_upper_bound=_decimal_text(cost),
        pricing_snapshot_revision=pricing.snapshot_revision,
        settings_sha256=settings_sha256,
    )


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def generate_final_captions(
    request: CaptionRequest,
    config: Config,
    destination: Path,
    *,
    online: bool,
    confirmed_cost: bool,
    transcriber: transcribe.Transcriber | None = None,
    srt_stem: str | None = None,
) -> CaptionDelivery:
    """최종 48kHz PCM을 새로 전사하고 SRT와 검토 보고서를 원자적으로 공개한다.

    ``transcriber``를 주면 그것이 곧 전사기다 — 취소 게이트와 요청 회계가 붙은
    위임물을 넘겨 실제 호출마다 예약·확정·취소 확인을 지나게 한다(S4A). 주지
    않으면 파이프라인이 스스로 만드는 기본 경로 그대로다. ``srt_stem``은 엔진
    호출이 앱에 보이는 안정된 파일 이름을 쓰기 위한 것만이다.
    """
    audio_duration = _validate_pcm(request)
    project_name, timeline_duration, _project_uid = _validate_final_xml(request)
    if abs(audio_duration - timeline_duration) > Fraction(1, 48_000):
        raise CaptionGenerationError(
            f"렌더 오디오와 최종 타임라인 길이가 다르다: "
            f"{float(audio_duration):.6f}초 ≠ {float(timeline_duration):.6f}초"
        )
    estimated_cost = transcribe.estimate_cost(float(audio_duration), config) * 2
    if online and not confirmed_cost:
        raise CaptionGenerationError(
            f"예상 비용 ${estimated_cost:.3f}를 확인해야 온라인 전사를 시작할 수 있다"
        )
    destination.mkdir(parents=True, exist_ok=True)
    if srt_stem is None:
        basename = f"{_safe_filename(project_name)}.{request.final_revision}.ko"
    else:
        basename = _safe_filename(srt_stem)
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
                    transcriber=transcriber,
                )
            except transcribe.ConfirmedNoSpeech:
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
            oscompat.replace(source_srt, target_srt)
            quality = ["coverage:complete", "render-audio:48khz-pcm"]
            if run.verification is not None:
                quality.append("second-model:checked")
            result = CaptionResult(
                final_revision=request.final_revision,
                srt_path=target_srt,
                warnings=_caption_warnings(run),
                quality=tuple(quality),
                speech_status="speech",
            )
            _write_review(review_path, request, result, estimated_cost)
            return CaptionDelivery(result, review_path, estimated_cost)
    except CaptionGenerationError:
        raise
    except Exception as exc:
        raise CaptionGenerationError(f"최종 자막 생성 실패: {exc}") from exc


def _caption_warnings(run: pipeline.RunResult) -> tuple[str, ...]:
    """사람이 검토해야 할 사실만 뽑는다 — 경고는 결과와 함께 남는다(S4A).

    되풀이 재전사·글자 교정의 예외·검증 의심은 모두 파이프라인 로그에 WARN로
    남던 것들이다. 자막 작업은 그 사실을 SRT 곁의 기록에도 실어 보낸다.
    """
    warnings: list[str] = []
    if run.repairs:
        warnings.append(f"되풀이 재전사 {len(run.repairs)}구간")
    record = run.trace.retext
    if record.status != retext.RETEXT_APPLIED:
        warnings.append(f"글자 교정 {record.status} — whisper 글자로 나갔다")
    elif record.unplaced:
        dropped = sum(item.chars for item in record.unplaced)
        warnings.append(f"세그먼트 사이에 낀 글자 {dropped}자는 시각이 없어 출력하지 않았다")
    if run.verification is not None and run.verification.suspects:
        warnings.append(f"검증 의심 큐 {len(run.verification.suspects)}건")
    return tuple(warnings)


def _validate_pcm(request: CaptionRequest) -> Fraction:
    path = request.render_audio
    actual_sha256 = _file_sha256(path)
    if actual_sha256 != request.render_audio_sha256:
        raise CaptionGenerationError(
            f"렌더 오디오 해시가 요청과 다르다: {actual_sha256} ≠ {request.render_audio_sha256}"
        )
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
    return Fraction(frames, rate)


def _validate_final_xml(request: CaptionRequest) -> tuple[str, Fraction, str]:
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
    sequence = root.find("library/event/project/sequence")
    name = project.get("name", "").strip() if project is not None else ""
    if not name:
        raise CaptionGenerationError("최종 FCPXML에 프로젝트 이름이 없다")
    # 결과 프로젝트 UID — 원본 매핑과 잴 재료다. 비어 있어도 여기서는 실패하지
    # 않는다: 이 함수를 쓰는 통합 흐름(같은 작업공간의 `project caption`)에는
    # 매핑 문서가 없다. 매핑을 요구하는 문은 `validate_caption_request`다(S4A).
    uid = project.get("uid", "").strip() if project is not None else ""
    duration_text = sequence.get("duration") if sequence is not None else None
    if duration_text is None:
        raise CaptionGenerationError("최종 FCPXML에 sequence.duration이 없다")
    try:
        duration = parse_fcpx_time(duration_text)
    except ValueError as exc:
        raise CaptionGenerationError(f"최종 타임라인 길이를 읽을 수 없다: {exc}") from exc
    return name, duration, uid


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while block := stream.read(1024 * 1024):
                digest.update(block)
    except OSError as exc:
        raise CaptionGenerationError(f"파일을 읽을 수 없다: {path}") from exc
    return digest.hexdigest()


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
        "render_audio_sha256": request.render_audio_sha256,
        "quality": list(result.quality),
        "speech_status": result.speech_status,
        "timeline_approval": "custom_share_destination",
        "warnings": list(result.warnings),
    }
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    oscompat.replace(temporary, path)
