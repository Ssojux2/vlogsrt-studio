"""첫 단계(A4) 의미 분석의 결정적 라이브러리 — 전사·프레임 증거와 안전 경계.

`docs/plans/2026-10-02-raw-video-browser-v2.md` §7의 첫 단계를 값과 순수 계산으로
세운다. Luna 호출은 `understanding_luna.py`가 맡고, 이 모듈은 **어떤 네트워크
호출도 하지 않는다** — whisper 경로는 주입된 `caption_provider.TimingProvider`
(공유 수명주기 문을 지나는)로, 프레임 추출은 주입된 `FrameSampler`로 간다.
ffmpeg·ffprobe는 `vlogsrt/preprocess.py`의 공개 함수만 쓴다(불변 규칙 7).

지키는 선(§7.3·§7.5·§13.3):

- **전사 증거만 만든다.** SRT 조판·최종 자막 파이프라인은 부르지 않는다 —
  첫 단계 산출물은 `analysis_transcript.json` 하나다.
- **모델 입력 분할은 선택 오디오 전체를 빈틈없이 덮는다.** 청크 실패는 그
  청크의 불확실성으로 남지 무발화로 바뀌지 않는다.
- **경계는 제안일 뿐이다.** 발화 중간에 걸리는 경계는 옮기지 않고 버린다 —
  버리면 조각이 길어질 뿐 전체 범위는 그대로다(§7.6).
- **시각은 정수 틱과 정확 유리수로.** JSON에는 분자·분모 또는 틱이 실린다.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import wave
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import date
from fractions import Fraction
from pathlib import Path
from typing import Protocol

from vlogsrt import log
from vlogsrt.models import Chunk
from vlogsrt.preprocess import VideoStreamInfo, extract_frame_index, extract_pcm_s16le
from vlogsrt.project.raw import request_log
from vlogsrt.project.raw.analysis_config import (
    RawUnderstandingSettings,
    validate_understanding_settings,
)
from vlogsrt.project.raw.caption_provider import TimingProvider, audio_chunk_binding
from vlogsrt.project.raw.config import RawWorkflowConfig, transcription_identity_view
from vlogsrt.task_runtime import check_cancel, provider_error

PCM_SAMPLE_RATE = 16000
"""`extract_pcm_s16le`의 고정 계약 주파수 — 이 층의 PCM 좌표는 전부 이 시계다."""

PCM_SAMPLE_WIDTH = 2
"""s16le 폭(바이트) — 청크 바이트 상한 환산에 쓴다."""

VIDEO_PROVEN = "proven"
"""비디오 격자가 증명됐다 — 프레임 표본을 뽑을 수 있다."""

VIDEO_UNPROVEN = "unproven"
"""비디오 격자 증명 실패(VFR 등) — 프레임 없이 오디오 증거만 간다(지원 차단 상태)."""

VIDEO_ABSENT = "absent"
"""비디오 스트림이 없다 — 프레임 증거 자체가 없는 별개 상태다."""

SPEECH_DETECTED = "speech_detected"
SPEECH_UNCONFIRMED_EMPTY = "unconfirmed_empty"
SPEECH_ANALYSIS_FAILED = "analysis_failed"
SPEECH_AUDIO_ABSENT = "audio_absent"

TICK_GRID_PCM = "pcm_sample"
TICK_GRID_VIDEO = "video_frame"


class AnalysisPlanningError(RuntimeError):
    """계획이 나가기 전에 닫힌 상황 — 메시지가 좌표·값을 말한다."""


# ─────────────────────────────────────────────────────────────
#  선택 오디오 — 잰 PCM 좌표의 정본
# ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SelectedAudio:
    """분석이 들여다볼 오디오의 신원 — 원본 해시·스트림·잰 원점을 함께 실는다.

    `pcm_sample0_file_time`은 `extract_pcm_s16le`이 **잰** 디코드 원점이다(컨테이너
    선언이 아니다). 정규화 시각 `t`(초, `[0, extent)`)의 파일 시각은
    `pcm_sample0_file_time + t` 하나뿐 — 원점 차이는 한 번만 적용한다(§13.3).
    """

    source_id: str
    source_path: Path
    source_sha256: str
    audio_stream_index: int
    pcm_path: Path
    pcm_sha256: str
    sample_rate: int
    channels: int
    sample_count: int
    pcm_sample0_file_time: Fraction

    @property
    def extent(self) -> Fraction:
        """정규화 시간축의 전체 길이 — `샘플 수 / 주파수`의 정확 유리수다."""
        return Fraction(self.sample_count, self.sample_rate)

    def file_time(self, normalized: Fraction) -> Fraction:
        """정규화 시각 → 파일 시각. 이 산술이 원점 변환의 전부다."""
        return self.pcm_sample0_file_time + normalized


def select_audio(
    source: Path, workdir: Path, *, source_id: str, audio_ordinal: int = 0
) -> SelectedAudio:
    """원본의 오디오 트랙 하나를 16kHz s16le PCM으로 뽑아 신원을 만든다.

    ffmpeg 호출은 `extract_pcm_s16le` 한 번뿐이다 — 원점·샘플 수는 그 실측값
    그대로다. PCM 파일은 workdir에 남겨 청크 슬라이스가 같은 바이트를 다룬다.
    """
    pcm_path = workdir / "analysis.pcm"
    extraction = extract_pcm_s16le(source, pcm_path, stream_index=audio_ordinal)
    stream = extraction.stream
    if extraction.sample_width != PCM_SAMPLE_WIDTH:
        raise AnalysisPlanningError(
            f"PCM 폭이 s16le 계약과 다르다: {extraction.sample_width}바이트"
        )
    return SelectedAudio(
        source_id=source_id,
        source_path=source,
        source_sha256=_sha256_file(source),
        audio_stream_index=stream.stream_index,
        pcm_path=pcm_path,
        pcm_sha256=_sha256_file(pcm_path),
        sample_rate=PCM_SAMPLE_RATE,
        channels=stream.channels,
        sample_count=extraction.sample_count,
        pcm_sample0_file_time=extraction.pcm_sample0_file_time,
    )


def _sha256_file(path: Path) -> str:
    """파일 내용의 SHA-256 — 스트리밍으로 읽어 큰 원본도 자리를 다 쓰지 않게."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


# ─────────────────────────────────────────────────────────────
#  청크 커버리지 — 선택 오디오 전체를 한 번에 덮는다
# ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SampleSpan:
    """`[start, end)` PCM 샘플 범위 — 채널별 프레임 번호 좌표다."""

    start: int
    end: int


def plan_audio_chunks(
    audio: SelectedAudio, settings: RawUnderstandingSettings
) -> tuple[SampleSpan, ...]:
    """선택 오디오 전체를 빈틈없이 덮는 청크 계획 — 단일 패스, 잘라내기 없다.

    청크 길이 상한은 시간(설정, ≤180초)과 바이트(API 안전 여유) 중 작은 쪽이다.
    마지막 청크는 남은 만큼 짧다 — 늘리거나 붙이지 않는다(§7.3: 입력을 줄이지
    않는다). 조용한 자리 탐색으로 경계를 옮기지도 않는다 — 그것은 엔진 되풀이
    재전사의 권한이고 첫 단계 증거 수집은 그대로 담는 쪽이다.
    """
    _require_settings(settings)
    frame_bytes = PCM_SAMPLE_WIDTH * audio.channels
    seconds_bound = int(float(settings.asr_chunk_max_seconds) * audio.sample_rate)
    bytes_bound = settings.asr_chunk_max_bytes // frame_bytes
    bound = min(seconds_bound, bytes_bound)
    if bound < 1:
        raise AnalysisPlanningError(
            f"청크 상한이 한 프레임보다 작다 — 시간 {settings.asr_chunk_max_seconds}초,"
            f" 바이트 {settings.asr_chunk_max_bytes}, 프레임 {frame_bytes}바이트"
        )
    spans = [
        SampleSpan(start, min(start + bound, audio.sample_count))
        for start in range(0, audio.sample_count, bound)
    ]
    _require_coverage(spans, audio.sample_count)
    return tuple(spans)


def _require_coverage(spans: Sequence[SampleSpan], sample_count: int) -> None:
    """커버리지 불변 — 순서·빈틈·겹침 없음·전체 덮음을 계획 단계에서 못 박는다."""
    if not spans:
        raise AnalysisPlanningError("청크 계획이 비었다 — 오디오가 없다")
    cursor = 0
    for position, span in enumerate(spans):
        check_cancel()
        if span.start != cursor or span.end <= span.start:
            raise AnalysisPlanningError(
                f"청크 {position}이 커버리지를 어긴다: [{span.start}, {span.end}),"
                f" 기대 시작 {cursor}"
            )
        cursor = span.end
    if cursor != sample_count:
        raise AnalysisPlanningError(
            f"청크 계획이 오디오 전체를 덮지 않는다: {cursor}/{sample_count} 샘플"
        )


def _require_settings(settings: RawUnderstandingSettings) -> None:
    validate_understanding_settings(settings)


@dataclass(frozen=True, slots=True)
class AsrChunkOutcome:
    """청크 하나의 청 단계 전사 결과 — 성공이거나 기록된 실패다.

    `failure`가 빈 문자열이면 `transcript`가 있다. 실패 청크를 무발화로 바꾸는
    길은 없다 — 소비 층은 그 범위를 `analysis_failed`로 다룬다(§7.3).
    """

    chunk_index: int
    sample_start: int
    sample_end: int
    wav_sha256: str
    request_id: str
    transcript: tuple[tuple[float, float, str], ...]
    failure: str = ""
    # 청크 끝을 관용 안에서 넘어 끝에 맞춘 세그먼트 위치(D55) — 화면이 「N줄을 맞춤」으로 보인다.
    clamped_segments: tuple[int, ...] = ()

    @property
    def succeeded(self) -> bool:
        return not self.failure


@dataclass(frozen=True, slots=True)
class AsrEvidence:
    """청 단계 내부 전사 증거 전부 — 청크별 결과와 요청 묶음 신원."""

    request_id: str
    sample_rate: int
    chunks: tuple[AsrChunkOutcome, ...]


def transcribe_selected_audio(
    audio: SelectedAudio,
    spans: Sequence[SampleSpan],
    *,
    settings: RawWorkflowConfig,
    understanding: RawUnderstandingSettings,
    provider: TimingProvider,
    on: date,
    request_id: str,
    workdir: Path,
    cache_dir: Path | None = None,
    asr_epoch: int = 0,
) -> AsrEvidence:
    """계획된 청크를 순서대로 내부 전사한다 — 주입된 시간 제공자를 통해서만.

    각 청크는 PCM 슬라이스를 WAV로 만들어(헤더만 붙인다) 엔진 `Chunk`에 얹고,
    `audio_chunk_binding`이 샘플 범위·해시·요청 신원을 묶는다. 제공자 안에서
    공유 수명주기 문(`capabilities.preflight_online_transcription`)이 먼저
    돈다 — 종료일·미확인 조합은 청크 실패로 기록되지 조용한 빈 성공이 되지
    않는다. 한 청크의 실패는 다음 청크를 멈추지 않는다(§7.3). 다시 듣기 차례는 `asr_chunk_key`.
    """
    _require_settings(understanding)
    outcomes: list[AsrChunkOutcome] = []
    for position, span in enumerate(spans):
        check_cancel()
        outcomes.append(
            _transcribe_span(
                audio,
                span,
                position=position,
                settings=settings,
                provider=provider,
                on=on,
                request_id=request_id,
                workdir=workdir,
                cache_dir=cache_dir,
                asr_epoch=asr_epoch,
            )
        )
    return AsrEvidence(request_id=request_id, sample_rate=audio.sample_rate, chunks=tuple(outcomes))


def _transcribe_span(
    audio: SelectedAudio,
    span: SampleSpan,
    *,
    position: int,
    settings: RawWorkflowConfig,
    provider: TimingProvider,
    on: date,
    request_id: str,
    workdir: Path,
    cache_dir: Path | None = None,
    asr_epoch: int = 0,
) -> AsrChunkOutcome:
    """청크 하나를 전사한다 — 실패는 이 청크의 기록으로 담아 올라간다."""
    wav_path = chunk_wav_path(workdir, position)
    write_pcm_slice(audio, span, wav_path)
    chunk = Chunk(
        path=wav_path,
        offset=float(Fraction(span.start, audio.sample_rate)),
        duration=float(Fraction(span.end - span.start, audio.sample_rate)),
        index=position,
    )
    chunk_request_id = f"{request_id}:chunk-{position:04d}"
    cache_path = workdir / f"asr-chunk-{position:04d}.json"
    fingerprint = asr_chunk_key(
        _sha256_file(wav_path), type(provider).__name__, settings, span, asr_epoch
    )
    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path = cache_dir / (fingerprint + ".json")
    if cache_path.is_file():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
            if cache.get("fingerprint") == fingerprint and not cache["outcome"].get("failure"):
                return AsrChunkOutcome(**cache["outcome"])
        except (ValueError, KeyError, TypeError):
            pass
    try:
        binding = audio_chunk_binding(
            chunk,
            sample_start=span.start,
            sample_end=span.end,
            sample_rate=audio.sample_rate,
            request_id=chunk_request_id,
        )
        attempt = request_log.begin("asr", fingerprint)
        timed = provider.timed_transcript(
            binding=binding,
            chunk=chunk,
            settings=settings,
            on=on,
            stem=wav_path.stem,
            source=audio.source_id,
        )
        request_log.finish(attempt, received=True)
        segments = tuple((seg.start, seg.end, seg.text) for seg in timed.segments)
        clamped = _clamped_of(timed.clamped_segments, position)
    except Exception as exc:  # 청크 하나의 실패 — 전체 실행의 실패가 아니다(§7.3)
        request_log.finish(
            locals().get("attempt"),
            received=not provider_error(exc)[2],
            note=provider_error(exc)[0],
        )
        check_cancel()
        log.warn(f"내부 전사 청크 {position} 실패 — 이 구간에는 자막이 없다: {exc}")
        return _failed_outcome(span, position, chunk_request_id, wav_path, exc)
    outcome = AsrChunkOutcome(
        chunk_index=position,
        sample_start=span.start,
        sample_end=span.end,
        wav_sha256=_sha256_file(wav_path),
        request_id=chunk_request_id,
        transcript=segments,
        clamped_segments=clamped,
    )
    temp = cache_path.with_suffix(".tmp")
    temp.write_text(
        json.dumps(
            {"fingerprint": fingerprint, "outcome": _cached_outcome(outcome)}, ensure_ascii=False
        ),
        encoding="utf-8",
    )
    temp.replace(cache_path)
    return outcome


def chunk_wav_path(workdir: Path, position: int) -> Path:
    """청크 WAV의 자리 — 1단계 글자 경로(D59 19c)가 whisper와 **같은 WAV**를 다시 쓴다."""
    return workdir / f"asr-chunk-{position:04d}.wav"


def asr_chunk_key(
    wav_sha256: str,
    provider_origin: str,
    settings: RawWorkflowConfig,
    span: SampleSpan,
    asr_epoch: int = 0,
) -> str:
    """내부 전사 청크 캐시 열쇠 — WAV·제공자·모델·전사 보기 설정·범위, 그리고 다시 듣기 차례.

    차례는 **0이 아닐 때만** 실린다(D59 19b) — 0이면 열쇠는 W-14 이후의 바이트 그대로라 이미 산
    전사를 그대로 쓴다. 차례가 오르면 그 원본의 청크만 열쇠가 바뀌어 새로 산다.
    """
    if isinstance(asr_epoch, bool) or not isinstance(asr_epoch, int) or asr_epoch < 0:
        raise AnalysisPlanningError(f"다시 듣기 차례는 0 이상의 정수다: {asr_epoch!r}")
    identity: dict[str, object] = {
        "wav": wav_sha256,
        "provider_origin": provider_origin,
        "model": settings.models.timing_model,
        "settings": asdict(transcription_identity_view(settings)),  # D54 — 분석 모델 고정
        "start": span.start,
        "end": span.end,
    }
    if asr_epoch:
        identity["asr_epoch"] = asr_epoch
    return hashlib.sha256(json.dumps(identity, sort_keys=True, default=str).encode()).hexdigest()


def _cached_outcome(outcome: AsrChunkOutcome) -> dict[str, object]:
    """캐시 항목 — 맞춘 줄이 없으면 그 칸을 싣지 않는다(W-15 이전 모양 그대로, D55).

    W-15 이전 빌드는 모르는 칸이 있으면 `AsrChunkOutcome(**…)`에서 TypeError를 내고 캐시를
    놓친 것으로 쳐 유료 전사를 다시 보낸다 — 넘침이 없는 항목은 바이트까지 예전과 같아야 한다.
    """
    body = asdict(outcome)
    if not outcome.clamped_segments:
        del body["clamped_segments"]
    return body


def _clamped_of(clamped: Sequence[int], position: int) -> tuple[int, ...]:
    """청크 끝에 맞춘 세그먼트 위치 — 있으면 경고로 알린다(D55, 조용한 손질은 없다)."""
    if clamped:
        log.warn(
            f"내부 전사 청크 {position}: {len(clamped)}줄을 청크 끝에 맞췄다 — 청크는 버리지"
            " 않는다 (D55)"
        )
    return tuple(clamped)


def _failed_outcome(
    span: SampleSpan,
    position: int,
    chunk_request_id: str,
    wav_path: Path,
    exc: Exception,
) -> AsrChunkOutcome:
    """실패 청크 기록 — WAV 해시까지 남겨 무엇이 실패했는지 증명 가능하게."""
    return AsrChunkOutcome(
        chunk_index=position,
        sample_start=span.start,
        sample_end=span.end,
        wav_sha256=_sha256_file(wav_path),
        request_id=chunk_request_id,
        transcript=(),
        failure=provider_error(exc)[1],
    )


def write_pcm_slice(audio: SelectedAudio, span: SampleSpan, destination: Path) -> Path:
    """저장 PCM의 `[start, end)` 프레임을 WAV 헤더만 붙여 옮긴다.

    데이터는 저장 바이트 그대로다 — 재인코딩·이득 보정·덧붙임이 없다. 원자적으로
    공개하므로 실패하면 반쪽 파일이 목격되지 않는다.
    """
    frame_bytes = PCM_SAMPLE_WIDTH * audio.channels
    if span.start < 0 or span.end <= span.start or span.end > audio.sample_count:
        raise AnalysisPlanningError(f"청크 범위가 오디오 밖이다: [{span.start}, {span.end})")
    if destination.resolve() == audio.pcm_path.resolve():
        raise AnalysisPlanningError(f"슬라이스가 저장 PCM 자리로는 못 간다: {destination}")
    expected = (span.end - span.start) * frame_bytes
    with audio.pcm_path.open("rb") as source:
        source.seek(span.start * frame_bytes)
        data = source.read(expected)
    if len(data) != expected:
        raise AnalysisPlanningError(
            f"PCM 슬라이스가 짧다 — {len(data)}/{expected}바이트 ({audio.pcm_path})"
        )
    temp = destination.with_name(destination.name + ".partial")
    try:
        with wave.open(str(temp), "wb") as handle:
            handle.setnchannels(audio.channels)
            handle.setsampwidth(PCM_SAMPLE_WIDTH)
            handle.setframerate(audio.sample_rate)
            handle.writeframes(data)
        os.replace(temp, destination)
    finally:
        temp.unlink(missing_ok=True)
    return destination


# ─────────────────────────────────────────────────────────────
#  발화 증거 — 세그먼트를 PCM 틱으로 옮긴다
# ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class UtteranceEvidence:
    """내부 전사 세그먼트 하나 — 시각은 청크 국소 초에서 PCM 틱으로.

    `start`·`end`는 정규화 시각(초, 정확 유리수)이고 `start_sample`·`end_sample`이
    같은 값의 틱 좌표다. 모델 초 표본을 틱으로 내림/올림한 뒤 범위를 벗어나면
    청크 끝에서 닫는다 — 시각을 지어내지 않는다(§11.11과 같은 선). 글자는
    시각이 길이 0이 되어도 남긴다(규칙 9 정신).
    """

    utterance_id: str
    chunk_index: int
    position: int
    text: str
    start: Fraction
    end: Fraction
    start_sample: int
    end_sample: int
    evidence_id: str
    # 1단계 글자 경로(D59 19c)의 증거 — 두 번째 귀의 답을 받은 청크의 발화에만 넷이 함께 실린다.
    # 없으면(None) 기록 모양이 19c 이전 그대로다. 시각·아이디·순서는 여기서 바뀌지 않는다.
    whisper_text: str | None = None
    text_source: str | None = None
    retext_reason: str | None = None
    alt_text: str | None = None
    # 그 글자 판정이 어디서 온 답인가(`live`·`replay`, D59 19d) — 이어 가기가 출처까지 대조한다.
    # 19c 기록에는 칸이 없다(None) — 그때는 기록 모양이 19c 그대로다.
    text_origin: str | None = None


TEXT_EVIDENCE_FIELDS = ("whisper_text", "text_source", "retext_reason", "alt_text")
"""발화 기록의 글자 증거 칸 — `text_source`가 있을 때만 넷이 함께 실린다(D59 19c)."""

TEXT_ORIGIN_FIELD = "text_origin"
"""글자 답의 출처 칸(D59 19d) — 증거가 있고 출처를 아는 줄에만 실린다(19c 기록은 바이트 그대로)."""


def utterance_text_evidence(raw: Mapping[str, object]) -> dict[str, str]:
    """기록에서 글자 증거를 다시 읽는다 — `text_source`가 없으면 빈 사전(19c 이전 기록).

    출처 칸(19d)은 기록에 있을 때만 돌려준다 — 없는 칸을 빈 글로 지어 다시 쓰면 19c 기록의 바이트가
    바뀌어 심기가 새 리비전을 만든다."""
    if "text_source" not in raw:
        return {}
    evidence = {name: str(raw.get(name) or "") for name in TEXT_EVIDENCE_FIELDS}
    if raw.get(TEXT_ORIGIN_FIELD):
        evidence[TEXT_ORIGIN_FIELD] = str(raw[TEXT_ORIGIN_FIELD])
    return evidence


def utterance_evidence(
    audio: SelectedAudio, evidence: AsrEvidence
) -> tuple[UtteranceEvidence, ...]:
    """성공 청크의 세그먼트를 발화 증거로 — 순서는 청크·위치 순서 그대로다."""
    utterances: list[UtteranceEvidence] = []
    for chunk in evidence.chunks:
        if not chunk.succeeded:
            continue
        for position, (start, end, text) in enumerate(chunk.transcript):
            utterances.append(
                _project_segment(audio, chunk, position, start, end, text, len(utterances))
            )
    return tuple(utterances)


def _project_segment(
    audio: SelectedAudio,
    chunk: AsrChunkOutcome,
    position: int,
    start: float,
    end: float,
    text: str,
    ordinal: int,
) -> UtteranceEvidence:
    """세그먼트 하나를 청크 국소 좌표에서 전역 PCM 틱으로 옮긴다."""
    start_sample = chunk.sample_start + _to_sample(start, audio.sample_rate)
    end_sample = min(chunk.sample_end, chunk.sample_start + _to_sample(end, audio.sample_rate))
    if end_sample < start_sample:
        end_sample = start_sample
    return UtteranceEvidence(
        utterance_id=f"utterance_{ordinal:06d}",
        chunk_index=chunk.chunk_index,
        position=position,
        text=text,
        start=Fraction(start_sample, audio.sample_rate),
        end=Fraction(end_sample, audio.sample_rate),
        start_sample=start_sample,
        end_sample=end_sample,
        evidence_id=f"{chunk.request_id}:{position}",
    )


def _to_sample(seconds: float, sample_rate: int) -> int:
    """모델 초 표본을 틱으로 — 반올림은 이 환산의 전부다(다른 계산은 없다)."""
    return int(math.floor(seconds * sample_rate + 0.5))


# ─────────────────────────────────────────────────────────────
#  비디오 격자 — 프레임 시계의 증명된 정본
# ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class VideoGrid:
    """증명된 CFR 비디오 시계 — 원점·프레임 길이·프레임 수.

    `vlogsrt/preprocess.probe_video_stream`(디코드 증거)의 결과를 값으로 옮긴
    얇은 대응층이다. A3 미디어 계약의 `RawVideoClock`이 통합되면 서비스가 같은
    칸을 채워 넘기면 된다 — 이 레코드가 곧 그 바인딩 지점이다.
    """

    stream_index: int
    presentation_origin: Fraction
    frame_duration: Fraction
    frame_count: int
    slot_packets: tuple[int, ...] = ()
    """map 시계(D70)만 — 슬롯 → 보이는 패킷 순번(`RawVideoClock.slot_packets`). 프레임 번호는
    언제나 슬롯이고, 표본 추출이 이 대응으로 패킷을 고른다. 있으면 지문이 맞춤 규칙 판
    (`preprocess.GRID_CONFORM_TAG`)을 싣는다. 균일·snap은 빈 튜플(번호 = 패킷 순번)."""

    @property
    def extent(self) -> Fraction:
        """비디오 정규화 길이 — `프레임 수 × 프레임 길이`."""
        return self.frame_count * self.frame_duration


def video_grid_from_stream(info: VideoStreamInfo) -> VideoGrid:
    """`probe_video_stream` 결과를 비디오 격자로 — 값은 그대로, 계산은 없다."""
    return VideoGrid(
        stream_index=info.stream_index,
        presentation_origin=info.presentation_origin,
        frame_duration=info.frame_duration,
        frame_count=info.frame_count,
    )


# ─────────────────────────────────────────────────────────────
#  시간창 계획 — 창·프레임 표본·경계 후보
# ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class FramePlan:
    """창 안 프레임 표본 하나 — 프레임 번호는 비디오 격자의 정수 좌표다."""

    frame_id: str
    frame_index: int
    window_time: Fraction


@dataclass(frozen=True, slots=True)
class BoundaryCandidate:
    """경계 후보 — 위치는 정규화 시각(초)이고 틱은 PCM 격자 정수다.

    `kind`는 근거의 종류이다(§7.1): `utterance_end`는 발화 종료, `window_length`는
    단순 길이 분할. 각 후보는 정확히 한 창이 소유한다 — Luna는 자기 창의 후보만
    제안받는다.
    """

    boundary_id: str
    window_id: str
    kind: str
    position: Fraction
    tick: int


@dataclass(frozen=True, slots=True)
class AnalysisWindow:
    """분석 시간창 — 창은 증거 단위이지 편집 조각이 아니다(§7.4)."""

    window_id: str
    ordinal: int
    core_start: Fraction
    core_end: Fraction
    context_start: Fraction
    context_end: Fraction
    frame_plans: tuple[FramePlan, ...]
    utterance_ids: tuple[str, ...]
    boundary_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WindowPlan:
    """시간창 계획 전체 — 정규화 범위·비디오 상태·후보 목록까지 함께."""

    extent: Fraction
    source_id: str
    video: VideoGrid | None
    video_status: str
    video_note: str
    windows: tuple[AnalysisWindow, ...]
    candidates: tuple[BoundaryCandidate, ...]

    def boundary(self, boundary_id: str) -> BoundaryCandidate | None:
        """아이디로 후보를 찾는다 — 모르는 아이디는 None(거짓말의 재료가 아니다)."""
        return next((item for item in self.candidates if item.boundary_id == boundary_id), None)


def plan_windows(
    audio: SelectedAudio,
    utterances: Sequence[UtteranceEvidence],
    settings: RawUnderstandingSettings,
    video: VideoGrid | None,
    *,
    video_status: str = VIDEO_PROVEN,
    video_note: str = "",
) -> WindowPlan:
    """선택 오디오 전체를 시간창으로 나누고 증거·후보를 창에 배정한다.

    창 코어는 `[k×창길이, …)` 순서로 전체를 빈틈없이 덮고, 문맥은 창 앞뒤로
    설정만큼(범위 안에서) 넓힌다 — 겹치는 문맥은 같은 프레임·발화 아이디로
    중복을 추적할 뿐 조각을 중복 만들지 않는다(§7.4).
    """
    _require_settings(settings)
    extent = audio.extent
    width = Fraction(str(settings.window_seconds))
    context = Fraction(str(settings.context_seconds))
    shells = [
        _plan_window(
            audio,
            utterances,
            ordinal,
            width=width,
            context=context,
            extent=extent,
            settings=settings,
            video=video,
        )
        for ordinal in range(_core_count(extent, width))
    ]
    candidates: list[BoundaryCandidate] = []
    windows: list[AnalysisWindow] = []
    for shell in shells:
        owned = _window_candidates(shell, utterances)
        candidates.extend(owned)
        windows.append(replace(shell, boundary_ids=tuple(item.boundary_id for item in owned)))
    _require_unique_ids(windows, candidates)
    return WindowPlan(
        extent=extent,
        source_id=audio.source_id,
        video=video,
        video_status=video_status,
        video_note=video_note,
        windows=tuple(windows),
        candidates=tuple(candidates),
    )


def _core_count(extent: Fraction, width: Fraction) -> int:
    """창 수 — 마지막 창은 남은 만큼 짧다(0이 아닌 이상 항상 한 창 있다)."""
    whole = extent / width
    return max(1, math.ceil(whole))


def _plan_window(
    audio: SelectedAudio,
    utterances: Sequence[UtteranceEvidence],
    ordinal: int,
    *,
    width: Fraction,
    context: Fraction,
    extent: Fraction,
    settings: RawUnderstandingSettings,
    video: VideoGrid | None,
) -> AnalysisWindow:
    """창 하나의 코어·문맥·프레임·발화 배정 — 모든 좌표는 정확 유리수다."""
    core_start = ordinal * width
    core_end = min(core_start + width, extent)
    context_start = max(Fraction(0), core_start - context)
    context_end = min(extent, core_end + context)
    return AnalysisWindow(
        window_id=f"window_{ordinal:06d}",
        ordinal=ordinal,
        core_start=core_start,
        core_end=core_end,
        context_start=context_start,
        context_end=context_end,
        frame_plans=_frame_plans(audio, context_start, context_end, settings, video),
        utterance_ids=_window_utterances(utterances, context_start, context_end),
        boundary_ids=(),
    )


def _frame_plans(
    audio: SelectedAudio,
    context_start: Fraction,
    context_end: Fraction,
    settings: RawUnderstandingSettings,
    video: VideoGrid | None,
) -> tuple[FramePlan, ...]:
    """문맥 전체에 고르게 분포한 프레임 표본 — 문맥 중심·순서·중복 제거가 계약.

    표본 시각은 `(i + ½) × 문맥길이 / 요청수`로 정규화 시각을 정하고 비디오
    격자의 정수 프레임 번호로 내린다. 같은 프레임이 두 번 계산되면 하나로 접는다
    — 겹치는 문맥이 같은 증거를 공유하는 길이다(§7.4). 비디오가 없으면(증명
    실패·부재) 표본은 없다 — 지어내지 않는다.
    """
    if video is None or video_status_blocked(video):
        return ()
    span = context_end - context_start
    if span <= 0:
        return ()
    plans: list[FramePlan] = []
    seen: set[int] = set()
    requested = settings.max_images_per_window
    for index in range(requested):
        moment = context_start + Fraction(2 * index + 1, 2 * requested) * span
        frame = _frame_at(audio, video, moment)
        if frame is None or frame in seen:
            continue
        seen.add(frame)
        plans.append(FramePlan(f"frame_{frame:08d}", frame, moment))
    return tuple(plans)


def video_status_blocked(video: VideoGrid) -> bool:
    """비디오 격자가 표본 추출에 못 쓰는 모양 — 0프레임·비양수 프레임 길이."""
    return video.frame_count < 1 or video.frame_duration <= 0


def _frame_at(audio: SelectedAudio, video: VideoGrid, moment: Fraction) -> int | None:
    """정규화 시각 → 격자 프레임 번호. 범위 밖은 None(버리지 옮기지 않는다).

    격자 위가 아니면 안쪽(이른 프레임)으로 내린다 — 표본이 창보다 늦어지는
    길은 없다.
    """
    frames = (audio.file_time(moment) - video.presentation_origin) / video.frame_duration
    index = math.floor(frames)
    return index if 0 <= index < video.frame_count else None


def _window_utterances(
    utterances: Sequence[UtteranceEvidence], context_start: Fraction, context_end: Fraction
) -> tuple[str, ...]:
    """문맥과 겹치는 발화 — 길이 0 발화는 시작점이 문맥 안이면 포함한다."""
    ids = [
        item.utterance_id
        for item in utterances
        if (item.start < context_end and item.end > context_start)
        or (item.start == item.end and context_start <= item.start < context_end)
    ]
    return tuple(ids)


def _window_candidates(
    window: AnalysisWindow, utterances: Sequence[UtteranceEvidence]
) -> tuple[BoundaryCandidate, ...]:
    """창이 소유할 경계 후보 — 발화 종료(코어 안)와 창 끝 길이 경계."""
    candidates: list[BoundaryCandidate] = []
    for item in utterances:
        if window.core_start <= item.end < window.core_end and item.end > item.start:
            candidates.append(
                BoundaryCandidate(
                    boundary_id=f"boundary_{item.end_sample:09d}",
                    window_id=window.window_id,
                    kind="utterance_end",
                    position=item.end,
                    tick=item.end_sample,
                )
            )
    tail = _length_candidate(window)
    if tail is not None:
        candidates.append(tail)
    return tuple(candidates)


def _length_candidate(window: AnalysisWindow) -> BoundaryCandidate | None:
    """창 끝의 길이 경계 — 마지막 창에는 없다(전체 끝은 경계가 아니다).

    위치를 PCM 틱으로 안쪽(floor) 내린다 — 창 길이 설정이 틱 격자와 어긋나도
    경계가 범위 밖으로 나가는 일은 없다.
    """
    tick = math.floor(window.core_end * PCM_SAMPLE_RATE)
    if tick <= 0 or Fraction(tick, PCM_SAMPLE_RATE) <= window.core_start:
        return None
    return BoundaryCandidate(
        boundary_id=f"boundary_{tick:09d}",
        window_id=window.window_id,
        kind="window_length",
        position=Fraction(tick, PCM_SAMPLE_RATE),
        tick=tick,
    )


def _require_unique_ids(
    windows: Sequence[AnalysisWindow], candidates: Sequence[BoundaryCandidate]
) -> None:
    """아이디 유일성 — 창·후보 아이디가 겹치면 증거 추적이 무너진다."""
    window_ids = [window.window_id for window in windows]
    if len(set(window_ids)) != len(window_ids):
        raise AnalysisPlanningError("창 아이디가 중복된다 — 계획이 결정적이지 않다")
    ticks = [candidate.tick for candidate in candidates]
    if len(set(ticks)) != len(ticks):
        raise AnalysisPlanningError("경계 후보 틱이 중복된다 — 계획이 결정적이지 않다")
    boundary_ids = [candidate.boundary_id for candidate in candidates]
    if len(set(boundary_ids)) != len(boundary_ids):
        raise AnalysisPlanningError("경계 아이디가 중복된다 — 계획이 결정적이지 않다")


# ─────────────────────────────────────────────────────────────
#  공통 파일 시간축 — 증명된 비디오 전체와 선택 오디오를 함께 덮는다(A4)
# ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CommonFileTimeline:
    """원본 하나의 공통 재생 시간축 — 비디오 제시 원점을 0으로 정규화한 시계다.

    `plan_windows`는 선택 오디오의 PCM 범위만 덮는다 — 오디오보다 긴 영상의
    조용한 머리·꼬리는 창 밖에 남는다. 이 시간축은 **증명된 비디오 격자 전체**와
    **선택 오디오 전체**를 함께 `[0, extent)`로 덮는다(§7.4 — 원본 전체를
    시간창으로 나눈다). 오디오 트랙이 없으면(`audio=None`) 비디오만의 시간축이
    되고, 0짜리 PCM이나 없는 샘플을 지어내지 않는다(§7.3 — 오디오 부재는
    무발화와 다른 상태다). 좌표는 전부 정확 유리수다(§13.3).
    """

    source_id: str
    video: VideoGrid
    audio: SelectedAudio | None
    extent: Fraction
    audio_start: Fraction | None
    """선택 오디오 0번 샘플의 공통 시계 시각 — 트랙이 없으면 None."""

    def audio_span(self) -> tuple[Fraction, Fraction] | None:
        """선택 오디오의 공통 시계 범위 `[start, end)` — 트랙이 없으면 None."""
        if self.audio is None or self.audio_start is None:
            return None
        return (self.audio_start, self.audio_start + self.audio.extent)

    def audio_time(self, playback: Fraction) -> Fraction | None:
        """공통 시계 → 오디오 정규화 시각. 오디오 범위 밖은 None(메우지 않는다)."""
        span = self.audio_span()
        if span is None or playback < span[0] or playback >= span[1]:
            return None
        return playback - span[0]

    def frame_index(self, playback: Fraction) -> int | None:
        """공통 시계 → 비디오 격자 프레임 번호. 격자 밖은 None.

        공통 시계는 이미 비디오 제시 원점 기준이므로 원점 뺄셈은 없다 —
        `playback / 프레임 길이`의 바닥이 곧 프레임 번호다.
        """
        index = math.floor(playback / self.video.frame_duration)
        return index if 0 <= index < self.video.frame_count else None


def common_timeline(
    *, source_id: str, video: VideoGrid, audio: SelectedAudio | None
) -> CommonFileTimeline:
    """증명된 비디오 격자와 선택 오디오로 공통 시간축을 만든다.

    길이는 **증명된 비디오 전체**와 **선택 오디오 전체** 중 넓은 쪽이다 — 오디오
    꼬리가 마지막 프레임보다 길어도 분석 증거는 그 끝까지 덮는다(§7.3). 비디오
    격자가 증명되지 않으면 공통 시간축 자체가 못 생긴다 — 못 믿는 격자 위에
    창을 얹지 않는다.
    """
    if video_status_blocked(video):
        raise AnalysisPlanningError(
            "비디오 격자가 증명된 상태가 아니다 — 공통 시간축을 만들 수 없다"
        )
    audio_start: Fraction | None = None
    extent = video.extent
    if audio is not None:
        audio_start = audio.pcm_sample0_file_time - video.presentation_origin
        extent = max(extent, audio_start + audio.extent)
    return CommonFileTimeline(
        source_id=source_id, video=video, audio=audio, extent=extent, audio_start=audio_start
    )


def utterances_in_common_clock(
    timeline: CommonFileTimeline, utterances: Sequence[UtteranceEvidence]
) -> tuple[UtteranceEvidence, ...]:
    """발화 증거를 공통 시계로 옮긴다 — 시각만 이동하고 틱·아이디·글자는 그대로.

    오디오가 없는데 발화가 오면 조립 결함이다 — 없는 트랙의 발화를 조용히 버리는
    길은 없다(§7.3).
    """
    if timeline.audio_start is None:
        if utterances:
            raise AnalysisPlanningError(
                "오디오 트랙이 없는데 발화 증거가 왔다 — 증거 조립 결함이다"
            )
        return ()
    shift = timeline.audio_start
    return tuple(
        replace(item, start=item.start + shift, end=item.end + shift) for item in utterances
    )


def plan_common_windows(
    timeline: CommonFileTimeline,
    utterances: Sequence[UtteranceEvidence],
    settings: RawUnderstandingSettings,
) -> WindowPlan:
    """공통 시간축 전체를 시간창으로 나눈다 — 증거·경계 후보를 창에 배정한다.

    `plan_windows`와 같은 규칙을 공통 시계에 적용한다: 창 코어는 `[0, extent)`를
    빈틈없이 덮고, 문맥은 창 앞뒤로 설정만큼(범위 안에서) 넓히며, 겹치는 문맥의
    같은 프레임은 같은 아이디로 공유된다(§7.4). 경계 후보의 `tick`은 **비디오
    격자 프레임 번호**다 — 조각은 비디오 격자에서 확정되므로(A4 서비스 조정).
    비디오 격자 밖(원점·마지막 프레임 뒤 오디오 꼬리)의 후보는 애초에 만들지
    않는다 — 오디오 꼬리는 마지막 조각에만 속한다(§8.2).
    """
    _require_settings(settings)
    width = Fraction(str(settings.window_seconds))
    context = Fraction(str(settings.context_seconds))
    spoken = utterances_in_common_clock(timeline, utterances)
    windows: list[AnalysisWindow] = []
    candidates: list[BoundaryCandidate] = []
    for ordinal in range(_core_count(timeline.extent, width)):
        core_start = ordinal * width
        core_end = min(core_start + width, timeline.extent)
        context_start = max(Fraction(0), core_start - context)
        context_end = min(timeline.extent, core_end + context)
        owned = _common_window_candidates(timeline, ordinal, core_start, core_end, spoken)
        candidates.extend(owned)
        windows.append(
            AnalysisWindow(
                window_id=f"window_{ordinal:06d}",
                ordinal=ordinal,
                core_start=core_start,
                core_end=core_end,
                context_start=context_start,
                context_end=context_end,
                frame_plans=_common_frame_plans(timeline, context_start, context_end, settings),
                utterance_ids=_window_utterances(spoken, context_start, context_end),
                boundary_ids=tuple(item.boundary_id for item in owned),
            )
        )
    _require_unique_common_ids(windows, candidates)
    return WindowPlan(
        extent=timeline.extent,
        source_id=timeline.source_id,
        video=timeline.video,
        video_status=VIDEO_PROVEN,
        video_note="common-file-timeline: 비디오 전체와 선택 오디오 전체를 함께 덮는다",
        windows=tuple(windows),
        candidates=tuple(candidates),
    )


def _common_frame_plans(
    timeline: CommonFileTimeline,
    context_start: Fraction,
    context_end: Fraction,
    settings: RawUnderstandingSettings,
) -> tuple[FramePlan, ...]:
    """문맥 전체에 고르게 분포한 프레임 표본 — `_frame_plans`의 공통 시계 판."""
    span = context_end - context_start
    if span <= 0:
        return ()
    plans: list[FramePlan] = []
    seen: set[int] = set()
    requested = settings.max_images_per_window
    for index in range(requested):
        moment = context_start + Fraction(2 * index + 1, 2 * requested) * span
        frame = timeline.frame_index(moment)
        if frame is None or frame in seen:
            continue
        seen.add(frame)
        plans.append(FramePlan(f"frame_{frame:08d}", frame, moment))
    return tuple(plans)


def _common_window_candidates(
    timeline: CommonFileTimeline,
    ordinal: int,
    core_start: Fraction,
    core_end: Fraction,
    spoken: Sequence[UtteranceEvidence],
) -> tuple[BoundaryCandidate, ...]:
    """창이 소유할 경계 후보 — 발화 종료(코어 안)와 창 끝 길이 경계(공통 시계)."""
    window_id = f"window_{ordinal:06d}"
    owned: list[BoundaryCandidate] = []
    for item in spoken:
        if core_start <= item.end < core_end and item.end > item.start:
            candidate = _common_candidate(
                timeline, window_id, "utterance_end", item.end, f"u{item.end_sample:09d}"
            )
            if candidate is not None:
                owned.append(candidate)
    tail = _common_candidate(timeline, window_id, "window_length", core_end, f"w{ordinal:06d}")
    if tail is not None:
        owned.append(tail)
    return tuple(owned)


def _common_candidate(
    timeline: CommonFileTimeline,
    window_id: str,
    kind: str,
    position: Fraction,
    tag: str,
) -> BoundaryCandidate | None:
    """공통 시계 위치의 후보 — tick은 비디오 격자 프레임 번호다.

    격자 위가 아니면 **바깥(다음 프레임)으로 올린다** — 발화 종료 경계를 앞
    조각에 온전히 남기는 쪽이다(§7.6: 대화의 완결성이 목표 길이보다 우선).
    원점(0)·마지막 프레임 이상은 후보가 못 된다: 첫 경계는 원점이 아니고 끝
    경계는 전체 끝이라 조각을 자르는 경계가 아니다. 같은 프레임에 후보가
    여럿일 수 있다 — 아이디는 종류·근거 틱으로 갈라 고유하다(확정은 조정
    계층이 접는다).
    """
    frame = math.ceil(position / timeline.video.frame_duration)
    if frame <= 0 or frame >= timeline.video.frame_count:
        return None
    return BoundaryCandidate(
        boundary_id=f"boundary_{tag}_{frame:08d}",
        window_id=window_id,
        kind=kind,
        position=position,
        tick=frame,
    )


def _require_unique_common_ids(
    windows: Sequence[AnalysisWindow], candidates: Sequence[BoundaryCandidate]
) -> None:
    """공통 시계 계획의 아이디 유일성 — 틱은 같은 프레임에 후보가 겹칠 수 있다."""
    window_ids = [window.window_id for window in windows]
    if len(set(window_ids)) != len(window_ids):
        raise AnalysisPlanningError("창 아이디가 중복된다 — 계획이 결정적이지 않다")
    boundary_ids = [candidate.boundary_id for candidate in candidates]
    if len(set(boundary_ids)) != len(boundary_ids):
        raise AnalysisPlanningError("경계 아이디가 중복된다 — 계획이 결정적이지 않다")


# ─────────────────────────────────────────────────────────────
#  프레임 표본 — 주입된 추출 경계로 실제 이미지를 만든다
# ─────────────────────────────────────────────────────────────


class FrameSampler(Protocol):
    """프레임 한 장 추출의 주입 경계 — 실제 구현은 preprocess를 감싼다."""

    def sample(
        self, *, stream_index: int, frame_index: int, destination: Path, long_edge: int
    ) -> Path: ...


@dataclass(frozen=True, slots=True)
class PreprocessFrameSampler:
    """실제 추출 구현 — `extract_frame_index`의 얇은 감싸개다.

    같은 프레임은 같은 설정에서 같은 바이트를 낸다(결정성 계약) — 해시 대상이
    곧 증거다.
    """

    source: Path

    def sample(
        self, *, stream_index: int, frame_index: int, destination: Path, long_edge: int
    ) -> Path:
        return extract_frame_index(
            self.source, stream_index, frame_index, destination, long_edge=long_edge
        )


@dataclass(frozen=True, slots=True)
class FrameEvidence:
    """뽑힌 프레임 한 장 — 파일·해시·격자 좌표가 다 남는다."""

    frame_id: str
    frame_index: int
    image_path: Path
    image_sha256: str
    file_time: Fraction


@dataclass(frozen=True, slots=True)
class FrameExtractionFailure:
    """프레임 한 장의 추출 실패 — 창은 나머지 표본으로 계속한다(지어내지 않는다)."""

    frame_id: str
    error: str


@dataclass(frozen=True, slots=True)
class FrameSamplingResult:
    """프레임 표본 결과 전부 — 성공·실패가 갈라져 기록된다."""

    frames: tuple[FrameEvidence, ...]
    failures: tuple[FrameExtractionFailure, ...]


def sample_frames(
    plan: WindowPlan,
    *,
    directory: Path,
    settings: RawUnderstandingSettings,
    sampler: FrameSampler,
) -> FrameSamplingResult:
    """계획의 모든 프레임 표본을 뽑는다 — 프레임마다 추출은 한 번이다.

    겹치는 문맥이 같은 프레임을 가리키면 아티팩트 하나를 공유한다(§7.4). 한 장의
    실패는 그 장의 실패로 남고 나머지를 멈추지 않는다 — 프레임이 없어도 전사
    증거는 유효하다.
    """
    if plan.video is None or plan.video_status != VIDEO_PROVEN:
        return FrameSamplingResult((), ())
    directory.mkdir(parents=True, exist_ok=True)
    ordered = sorted(
        {item.frame_index: item for window in plan.windows for item in window.frame_plans}.items()
    )
    frames: list[FrameEvidence] = []
    failures: list[FrameExtractionFailure] = []
    prepare = getattr(sampler, "prepare", None)
    if prepare is not None:
        try:
            prepare(
                frame_indices=tuple(index for index, _ in ordered),
                directory=directory,
                long_edge=settings.image_long_edge,
            )
        except Exception:
            check_cancel()  # cancellation propagates; otherwise retain completed frame cache
    for _, item in ordered:
        check_cancel()
        destination = directory / f"{item.frame_id}.jpg"
        try:
            path = sampler.sample(
                stream_index=plan.video.stream_index,
                frame_index=item.frame_index,
                destination=destination,
                long_edge=settings.image_long_edge,
            )
            digest = _sha256_file(path)
        except Exception as exc:
            check_cancel()
            failures.append(FrameExtractionFailure(item.frame_id, f"{type(exc).__name__}: {exc}"))
            continue
        frames.append(
            FrameEvidence(
                frame_id=item.frame_id,
                frame_index=item.frame_index,
                image_path=path,
                image_sha256=digest,
                file_time=plan.video.presentation_origin
                + item.frame_index * plan.video.frame_duration,
            )
        )
    return FrameSamplingResult(tuple(frames), tuple(failures))


# ─────────────────────────────────────────────────────────────
#  판정 요약과 분할 제안 — 서버의 결정적 경계 확정
# ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class WindowVerdict:
    """창 하나의 판정 요약 — Luna 결과를 결정적 분할이 쓰는 모양으로.

    `accepted`가 거짓이면 그 창 안에서는 경계를 만들지 않는다(보수적 유지,
    §7.6). `note`는 거짓·실패의 이유다 — 검토 기록의 재료.
    """

    window_id: str
    accepted: bool
    suggested_boundary_ids: tuple[str, ...]
    needs_review: bool
    note: str = ""


@dataclass(frozen=True, slots=True)
class SegmentProposal:
    """제안된 조각 하나 — `[start, end)` 정규화 범위와 근거·검토 상태."""

    segment_id: str
    ordinal: int
    start: Fraction
    end: Fraction
    start_tick: int
    end_tick: int
    speech_status: str
    needs_review: bool
    basis: tuple[str, ...]
    review_reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SegmentationProposal:
    """분할 제안 전체 — 전체 범위 보존·검토 이력과 함께."""

    extent: Fraction
    tick_grid: str
    segments: tuple[SegmentProposal, ...]
    dropped_boundary_ids: tuple[str, ...]
    review_reasons: tuple[str, ...]


def propose_segmentation(
    plan: WindowPlan,
    verdicts: Mapping[str, WindowVerdict],
    utterances: Sequence[UtteranceEvidence],
    asr_failures: Sequence[AsrChunkOutcome] = (),
) -> SegmentationProposal:
    """판정을 결정적 분할로 확정한다 — 전체 범위는 언제나 그대로 보존된다.

    규칙(§7.6): 받아들인 창의 제안 경계만 후보가 되고, 발화 중간(열린 구간)에
    걸리는 경계는 **옮기지 않고 버린다** — 버리면 조각이 길어질 뿐이다. 판정이
    없거나 거짓인 창은 경계를 만들지 않는다(보수적 긴 조각 + 검토 표시). 창을
    무시한 강제 절단(45초 같은 목표 길이)은 이 함수에 존재하지 않는다.
    """
    cuts: list[int] = []
    dropped: list[str] = []
    reasons: list[str] = []
    for window in plan.windows:
        verdict = verdicts.get(window.window_id)
        if verdict is None:
            reasons.append(f"{window.window_id}:판정 없음 — 보수적 유지")
            continue
        if verdict.needs_review:
            reasons.append(f"{window.window_id}:{verdict.note or '검토 필요'}")
        if not verdict.accepted:
            continue
        cuts, dropped, reasons = _accept_verdict(
            plan, window, verdict, utterances, cuts, dropped, reasons
        )
    return _assemble_segments(
        plan, sorted(set(cuts)), tuple(dropped), tuple(reasons), utterances, asr_failures
    )


def _accept_verdict(
    plan: WindowPlan,
    window: AnalysisWindow,
    verdict: WindowVerdict,
    utterances: Sequence[UtteranceEvidence],
    cuts: list[int],
    dropped: list[str],
    reasons: list[str],
) -> tuple[list[int], list[str], list[str]]:
    """받아들인 판정의 경계를 검증해 자른 점 목록에 더한다."""
    for boundary_id in verdict.suggested_boundary_ids:
        candidate = plan.boundary(boundary_id)
        if candidate is None or candidate.window_id != window.window_id:
            dropped.append(boundary_id)
            reasons.append(f"{window.window_id}:{boundary_id}:제공되지 않은 경계 — 거부")
            continue
        if any(item.start < candidate.position < item.end for item in utterances):
            dropped.append(boundary_id)
            reasons.append(f"{window.window_id}:{boundary_id}:발화 중간 — 버리고 긴 조각 유지")
            continue
        cuts.append(candidate.tick)
    return cuts, dropped, reasons


def _assemble_segments(
    plan: WindowPlan,
    ticks: list[int],
    dropped: tuple[str, ...],
    reasons: tuple[str, ...],
    utterances: Sequence[UtteranceEvidence],
    asr_failures: Sequence[AsrChunkOutcome],
) -> SegmentationProposal:
    """자른 점을 조각으로 — 빈틈·겹침·뒤집힘 없는 전체 덮음을 못 박는다."""
    rate = PCM_SAMPLE_RATE
    edges = [0, *ticks]
    if edges[-1] < math.floor(plan.extent * rate):
        edges.append(math.floor(plan.extent * rate))
    segments = [
        _segment(plan, ordinal, start, end, utterances, asr_failures, reasons)
        # 짝 지을 목록과 한 칸 민 목록이라 길이가 하나 다른 것이 의도다
        for ordinal, (start, end) in enumerate(zip(edges, edges[1:], strict=False))
        if end > start
    ]
    _require_segment_coverage(segments, plan.extent)
    return SegmentationProposal(
        extent=plan.extent,
        tick_grid=TICK_GRID_PCM,
        segments=tuple(segments),
        dropped_boundary_ids=dropped,
        review_reasons=reasons,
    )


def _segment(
    plan: WindowPlan,
    ordinal: int,
    start: int,
    end: int,
    utterances: Sequence[UtteranceEvidence],
    asr_failures: Sequence[AsrChunkOutcome],
    reasons: tuple[str, ...],
) -> SegmentProposal:
    """조각 하나 — 상태·근거·검토 이유를 범위 산술으로 계산한다."""
    rate = PCM_SAMPLE_RATE
    start_at, end_at = Fraction(start, rate), Fraction(end, rate)
    windows = [
        window
        for window in plan.windows
        if window.core_start < end_at and window.core_end > start_at
    ]
    spoken = [
        item
        for item in utterances
        if (item.start < end_at and item.end > start_at and item.text.strip())
        or (item.start == item.end and start_at <= item.start < end_at and item.text.strip())
    ]
    failed = [
        outcome
        for outcome in asr_failures
        if Fraction(outcome.sample_start, rate) < end_at
        and Fraction(outcome.sample_end, rate) > start_at
    ]
    status = (
        SPEECH_ANALYSIS_FAILED
        if failed
        else SPEECH_DETECTED
        if spoken
        else SPEECH_UNCONFIRMED_EMPTY
    )
    review = [
        reason
        for reason in reasons
        if any(reason.startswith(f"{window.window_id}:") for window in windows)
    ]
    basis = tuple(
        [f"tick:{start}", *(window.window_id for window in windows)]
        + [item.utterance_id for item in spoken]
    )
    return SegmentProposal(
        segment_id=f"segment_{start:09d}",
        ordinal=ordinal,
        start=start_at,
        end=end_at,
        start_tick=start,
        end_tick=end,
        speech_status=status,
        needs_review=bool(review) or status != SPEECH_DETECTED,
        basis=basis,
        review_reasons=tuple(review),
    )


def _require_segment_coverage(segments: Sequence[SegmentProposal], extent: Fraction) -> None:
    """조각 불변 — 순서·빈틈·겹침 없음·전체 덮음(§7.6 6단계)."""
    if not segments:
        raise AnalysisPlanningError("조각 제안이 비었다 — 전체 보존 계약 위반")
    cursor = Fraction(0)
    for position, segment in enumerate(segments):
        if segment.start != cursor:
            raise AnalysisPlanningError(
                f"조각 {position}이 범위를 어긴다: 시작 {segment.start}, 기대 {cursor}"
            )
        if segment.end <= segment.start:
            raise AnalysisPlanningError(f"조각 {position}이 비거나 뒤집혔다")
        cursor = segment.end
    if cursor != extent:
        raise AnalysisPlanningError(
            f"조각이 전체를 덮지 않는다: {cursor}/{extent}초 — 보존 계약 위반"
        )


def audio_absent_proposal(video: VideoGrid, *, reason: str) -> SegmentationProposal:
    """오디오가 없는 원본의 제안 — 비디오 전체를 한 조각으로 남긴다.

    오디오 부재는 무발화와 다른 상태다(§7.3). 경계를 만들 근거가 없으므로 전체를
    하나로 보존하고 검토 표시를 남긴다 — 삭제도 자동 분할도 하지 않는다.
    """
    if video_status_blocked(video):
        raise AnalysisPlanningError("비디오 격자가 없다 — 오디오 부재 상태를 만들 수 없다")
    extent = video.extent
    return SegmentationProposal(
        extent=extent,
        tick_grid=TICK_GRID_VIDEO,
        segments=(
            SegmentProposal(
                segment_id="segment_000000000",
                ordinal=0,
                start=Fraction(0),
                end=extent,
                start_tick=0,
                end_tick=video.frame_count,
                speech_status=SPEECH_AUDIO_ABSENT,
                needs_review=True,
                basis=("video:whole",),
                review_reasons=(f"audio_absent:{reason}",),
            ),
        ),
        dropped_boundary_ids=(),
        review_reasons=(f"audio_absent:{reason}",),
    )


# ─────────────────────────────────────────────────────────────
#  직렬화 — analysis_transcript.json (SRT가 아니다)
# ─────────────────────────────────────────────────────────────


def fraction_json(value: Fraction) -> dict[str, int]:
    """유리수를 JSON으로 — 분자·분모 쌍이 정본이다(§13.3)."""
    return {"num": value.numerator, "den": value.denominator}


def selected_audio_json(audio: SelectedAudio) -> dict[str, object]:
    """선택 오디오 신원의 JSON — 해시·원점·샘플 수까지 전부."""
    return {
        "source_id": audio.source_id,
        "source_sha256": audio.source_sha256,
        "audio_stream_index": audio.audio_stream_index,
        "pcm_sha256": audio.pcm_sha256,
        "sample_rate": audio.sample_rate,
        "channels": audio.channels,
        "sample_count": audio.sample_count,
        "pcm_sample0_file_time": fraction_json(audio.pcm_sample0_file_time),
        "extent": fraction_json(audio.extent),
    }


def asr_evidence_json(evidence: AsrEvidence) -> dict[str, object]:
    """내부 전사 증거의 JSON — 실패 청크도 그대로 실린다."""
    return {
        "request_id": evidence.request_id,
        "sample_rate": evidence.sample_rate,
        "chunks": [_asr_chunk_json(chunk) for chunk in evidence.chunks],
    }


def _asr_chunk_json(chunk: AsrChunkOutcome) -> dict[str, object]:
    """청크 하나 — 끝에 맞춘 줄이 있을 때만 그 칸을 싣는다(없으면 예전 기록과 바이트가 같다)."""
    body: dict[str, object] = {
        "chunk_index": chunk.chunk_index,
        "sample_start": chunk.sample_start,
        "sample_end": chunk.sample_end,
        "wav_sha256": chunk.wav_sha256,
        "request_id": chunk.request_id,
        "segments": [
            {"start": start, "end": end, "text": text} for start, end, text in chunk.transcript
        ],
        "failure": chunk.failure,
    }
    if chunk.clamped_segments:
        body["clamped_segments"] = list(chunk.clamped_segments)
    return body


def utterances_json(utterances: Sequence[UtteranceEvidence]) -> list[dict[str, object]]:
    """발화 증거의 JSON — 틱과 유리수 시각을 함께 실운다."""
    return [_utterance_json(item) for item in utterances]


def _utterance_json(item: UtteranceEvidence) -> dict[str, object]:
    """발화 하나 — 글자 증거는 있을 때만 싣는다(없으면 19c 이전 기록과 바이트가 같다)."""
    body: dict[str, object] = {
        "utterance_id": item.utterance_id,
        "chunk_index": item.chunk_index,
        "position": item.position,
        "text": item.text,
        "start": fraction_json(item.start),
        "end": fraction_json(item.end),
        "start_sample": item.start_sample,
        "end_sample": item.end_sample,
        "evidence_id": item.evidence_id,
    }
    if item.text_source is not None:
        body.update(
            whisper_text=item.text if item.whisper_text is None else item.whisper_text,
            text_source=item.text_source,
            retext_reason=item.retext_reason or "",
            alt_text=item.alt_text or "",
        )
        if item.text_origin:
            body[TEXT_ORIGIN_FIELD] = item.text_origin
    return body


def window_plan_json(plan: WindowPlan) -> dict[str, object]:
    """시간창 계획의 JSON — 창·프레임·후보 아이디까지 전부."""
    return {
        "extent": fraction_json(plan.extent),
        "source_id": plan.source_id,
        "video_status": plan.video_status,
        "video_note": plan.video_note,
        "windows": [
            {
                "window_id": window.window_id,
                "ordinal": window.ordinal,
                "core_start": fraction_json(window.core_start),
                "core_end": fraction_json(window.core_end),
                "context_start": fraction_json(window.context_start),
                "context_end": fraction_json(window.context_end),
                "frame_ids": [item.frame_id for item in window.frame_plans],
                "utterance_ids": list(window.utterance_ids),
                "boundary_ids": list(window.boundary_ids),
            }
            for window in plan.windows
        ],
        "boundary_candidates": [
            {
                "boundary_id": item.boundary_id,
                "window_id": item.window_id,
                "kind": item.kind,
                "position": fraction_json(item.position),
                "tick": item.tick,
            }
            for item in plan.candidates
        ],
    }


def frame_sampling_json(result: FrameSamplingResult) -> dict[str, object]:
    """프레임 표본의 JSON — 성공·실패가 갈라져 기록된다."""
    return {
        "frames": [
            {
                "frame_id": item.frame_id,
                "frame_index": item.frame_index,
                "image_sha256": item.image_sha256,
                "file_time": fraction_json(item.file_time),
            }
            for item in result.frames
        ],
        "failures": [{"frame_id": item.frame_id, "error": item.error} for item in result.failures],
    }


def segmentation_json(proposal: SegmentationProposal) -> dict[str, object]:
    """분할 제안의 JSON — 조각·버린 경계·검토 이유 전부."""
    return {
        "extent": fraction_json(proposal.extent),
        "tick_grid": proposal.tick_grid,
        "segments": [
            {
                "segment_id": segment.segment_id,
                "ordinal": segment.ordinal,
                "start": fraction_json(segment.start),
                "end": fraction_json(segment.end),
                "start_tick": segment.start_tick,
                "end_tick": segment.end_tick,
                "speech_status": segment.speech_status,
                "needs_review": segment.needs_review,
                "basis": list(segment.basis),
                "review_reasons": list(segment.review_reasons),
            }
            for segment in proposal.segments
        ],
        "dropped_boundary_ids": list(proposal.dropped_boundary_ids),
        "review_reasons": list(proposal.review_reasons),
    }


EXECUTION_MODE_REPLAY = "offline_replay"
"""실행 증거에 실제 호출이 없다 — 픽스처 재생만 있었거나 호출이 아직 없다."""

EXECUTION_MODE_LIVE = "online_live"
"""실행 증거 전부가 실제 호출이다."""

EXECUTION_MODE_MIXED = "mixed_live_and_replay"
"""실행 증거가 섞여 있다 — 재생 판정과 실제 호출 판정이 같은 기록에 있다."""

EXECUTION_MODES = frozenset({EXECUTION_MODE_REPLAY, EXECUTION_MODE_LIVE, EXECUTION_MODE_MIXED})
"""실행 모드의 값역 — 이 세 값 밖은 기록 자체가 계약 위반이다."""


def stage1_evidence_document(
    *,
    audio: SelectedAudio,
    asr: AsrEvidence,
    utterances: Sequence[UtteranceEvidence],
    plan: WindowPlan,
    frames: FrameSamplingResult,
    segmentation: SegmentationProposal,
    annotations: Sequence[Mapping[str, object]],
    models: Mapping[str, str],
    prompt_revision: str,
    fingerprint: str,
    execution_mode: str,
) -> dict[str, object]:
    """첫 단계 산출물 문서 — `analysis_transcript.json`의 본문이다.

    SRT·VTT는 없다(§7.1): 이 문서가 전부다. `annotations`는 이미 JSON 모양으로
    변환된 Luna 판정(`understanding_luna.annotation_json`)이고 `execution_mode`는
    실행 **증거**에서 내린 값 세 개뿐이다 — 재생을 실제 성공으로 표시하지
    않는다(§14.3). 섞인 실행은 섞였다고 말한다.
    """
    if execution_mode not in EXECUTION_MODES:
        raise AnalysisPlanningError(f"실행 모드 값이 계약 밖이다: {execution_mode!r}")
    return {
        "stage": "understanding_stage1",
        "audio": selected_audio_json(audio),
        "asr": asr_evidence_json(asr),
        "utterances": utterances_json(utterances),
        "windows": window_plan_json(plan),
        "frames": frame_sampling_json(frames),
        "annotations": list(annotations),
        "segmentation": segmentation_json(segmentation),
        "models": dict(models),
        "prompt_revision": prompt_revision,
        "fingerprint": fingerprint,
        "execution_mode": execution_mode,
    }


def write_stage1_document(path: Path, document: Mapping[str, object]) -> Path:
    """문서를 원자적으로 쓴다 — 반쪽 파일이 목격되지 않게 `.partial`을 거친다."""
    body = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".partial")
    try:
        temp.write_text(body, encoding="utf-8")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    return path
