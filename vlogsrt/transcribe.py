"""유일한 네트워크 경계(불변 규칙 5).

다른 모듈은 전부 순수 함수다. `openai`를 import하는 파일은 이 파일뿐이며,
그것도 실제 호출 직전에 지연 import한다. Claude Messages(D54)는 HTTP 전송만
`anthropic_messages.py`에 살고, 그 파일을 import하는 곳도 이 파일뿐이다 — 승인 문·
관찰·취소는 여기 있다.
"""

from __future__ import annotations

import base64
import hashlib
import inspect
import json
import math
import threading
import time
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, Protocol
from uuid import uuid4

from . import env, log
from .config import Config
from .models import Chunk, Segment

FIXTURE_KIND = "whisper"  # 자막을 만드는 호출
VERIFY_KIND = "gpt"  # 대조용 호출. 출력을 바꾸지 못한다(규칙 5)
RESCUE_KIND = "rescue"  # 되풀이 구간만 다시 묻는 호출. 같은 모델, 다른 온도(D27)
ARBITER_KIND = "arbiter"  # 그 구간을 심판하는 두 번째 모델. 시각을 만들지 않는다
CAPTION_TEXT_KIND = "caption-text"  # 파생 계층 문구 경로 — json `text`, 시각 없음(A7-1)
CAPTION_TIMING_KIND = "caption-timing"  # 파생 계층 시간 경로 — verbose_json 세그먼트(A7-1)


@dataclass(frozen=True, slots=True)
class ChunkResponse:
    """조각 하나에 대한 API 응답. `payload`는 조각 좌표 그대로의 원본이다.

    `offset`을 더하면 **원본 오디오 시각**이 된다. 그 덧셈이 시각에 가해지는 유일한
    계산이며, ffmpeg가 실제로 잘린 자리라고 알려준 값이다(규칙 1).
    """

    index: int
    offset: float
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class TranscriptionJob:
    """`kind`가 어느 모델로 부를지 정한다 — 그리고 힌트가 실릴지도 정한다(규칙 6)."""

    stem: str
    chunks: tuple[Chunk, ...]
    source: str = ""
    kind: str = FIXTURE_KIND
    # 픽스처 네임스페이스. None이면 지금처럼 설정된 픽스처 디렉터리에서 **전부** 읽는다.
    # 있으면 `<namespace>/<stem>.<kind>.json` 하나만 보고 요청한 조각만 엄격히 고른다.
    namespace: Path | None = None


# 힌트가 실릴 수 있는 호출. **whisper 쪽은 절대 여기 들어오지 않는다**(규칙 6, D16).
_HINTED_KINDS = frozenset({VERIFY_KIND, ARBITER_KIND})

_KIND_LABELS = {
    FIXTURE_KIND: "전사",
    VERIFY_KIND: "검증",
    RESCUE_KIND: "재전사",
    ARBITER_KIND: "심판",
    CAPTION_TEXT_KIND: "문구 전사",
    CAPTION_TIMING_KIND: "시간 전사",
}


class Transcriber(Protocol):
    """이 파일에서만 Protocol을 쓴다. 구현체가 둘(실제/픽스처)이라 실제로 필요하다."""

    def transcribe(self, job: TranscriptionJob, cfg: Config) -> list[ChunkResponse]: ...


# ─────────────────────────────────────────────────────────────
#  픽스처
# ─────────────────────────────────────────────────────────────


def _fixture_path(fixture_dir: Path, stem: str, kind: str) -> Path:
    return fixture_dir / f"{stem}.{kind}.json"


def _missing_fixture(path: Path, stem: str, source: str = "") -> RuntimeError:
    video = source or f"golden/{stem}.mp4"
    return RuntimeError(
        f"픽스처가 없습니다: {path}\n"
        f"사람이 직접 다음을 실행해야 합니다:\n"
        f"  vlogsrt record {video}\n"
        f"예상 비용: 약 $0.03 (5분 영상 기준)"
    )


def _load_responses(path: Path, stem: str, source: str = "") -> list[ChunkResponse]:
    if not path.is_file():
        raise _missing_fixture(path, stem, source)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"픽스처가 올바른 JSON이 아닙니다: {path} ({exc})") from exc
    if isinstance(raw, dict) and "chunks" in raw:
        return [
            ChunkResponse(
                index=int(item.get("index", i)),
                offset=float(item.get("offset", 0.0)),
                payload=item["response"],
            )
            for i, item in enumerate(raw["chunks"])
        ]
    if isinstance(raw, dict):
        return [ChunkResponse(index=0, offset=0.0, payload=raw)]
    raise RuntimeError(f"픽스처 형식을 알 수 없습니다: {path}")


@dataclass(frozen=True, slots=True)
class FixtureTranscriber:
    """녹화된 응답을 읽는다. 개발 중 기본값이며 네트워크를 쓰지 않는다."""

    fixture_dir: Path

    def transcribe(self, job: TranscriptionJob, cfg: Config) -> list[ChunkResponse]:
        if job.namespace is not None:
            return _load_namespaced(_namespace_path(job.namespace, job), job)
        return _load_responses(
            _fixture_path(self.fixture_dir, job.stem, job.kind), job.stem, job.source
        )


def _namespace_path(namespace: Path, job: TranscriptionJob) -> Path:
    """namespace 안에 머무는 경로만 만든다. 이름 조각 하나로 서지 않으면 만들지도 않는다.

    파일명 조각 검사일 뿐 샌드박스가 아니다 — 한글·공백·점은 그대로 통과하고,
    구분자·`.`·`..`·빈칸만 거부한다. 레거시(namespace 없음) 경로에는 이 선이 없다.
    """
    for what, value in (("stem", job.stem), ("kind", job.kind)):
        if not value or value in {".", ".."} or "/" in value or "\\" in value:
            raise MalformedTranscript(
                f"namespace 픽스처의 {what}이(가) 안전한 파일명 조각이 아닙니다: {value!r}"
            )
    return _fixture_path(namespace, job.stem, job.kind)


def _load_namespaced(path: Path, job: TranscriptionJob) -> list[ChunkResponse]:
    """namespace 픽스처에서 **요청한 조각만** 엄격히 골라 낸다.

    저장된 전체 집합에서 재시도 부분집합을 고르는 길이다. 원본 JSON을 int()/float()
    강제나 `item.get` 기본값이 붙기 **전에** 본다 — 강제가 숨기는 잘못된 값은 그 다음
    단계에서는 이미 늦는다.
    """
    if not path.is_file():
        raise _missing_fixture(path, job.stem, job.source)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"픽스처가 올바른 JSON이 아닙니다: {path} ({exc})") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("chunks"), list):
        raise RuntimeError(f"픽스처 형식을 알 수 없습니다: {path}")
    saved = _raw_records(raw["chunks"], path)
    return _pick_requested(saved, job.chunks, path)


def _raw_records(items: list[Any], path: Path) -> list[tuple[int, float, dict[str, Any]]]:
    """강제 이전의 원본 기록을 검사한다. 없는 칸은 기본값으로 몰래 메워지지 않는다."""
    records: list[tuple[int, float, dict[str, Any]]] = []
    for item in items:
        if not isinstance(item, dict):
            raise RuntimeError(f"픽스처 조각 기록이 JSON 객체가 아닙니다: {path}")
        missing = [key for key in ("index", "offset", "response") if key not in item]
        if missing:
            raise RuntimeError(f"픽스처 조각 기록에 없는 칸: {missing} ({path})")
        if not _exact_int(item["index"]):
            raise RuntimeError(f"픽스처 조각 번호가 정수가 아닙니다: {item['index']!r} ({path})")
        if not _finite_number(item["offset"]):
            raise RuntimeError(
                f"픽스처 조각 오프셋이 유한 숫자가 아닙니다: {item['offset']!r} ({path})"
            )
        records.append((item["index"], item["offset"], item["response"]))
    return records


def _pick_requested(
    saved: list[tuple[int, float, dict[str, Any]]], chunks: tuple[Chunk, ...], path: Path
) -> list[ChunkResponse]:
    """요청한 조각 번호마다 저장된 응답이 **정확히 하나**여야 한다."""
    requested = {chunk.index: chunk for chunk in chunks}
    picked: dict[int, ChunkResponse] = {}
    for index, offset, payload in saved:
        if index not in requested:
            continue  # 저장된 전체 집합에서 요청한 부분집합만 고른다
        if index in picked:
            raise RuntimeError(f"픽스처에 조각 {index} 응답이 둘 이상 있다: {path}")
        chunk = requested[index]
        if offset != chunk.offset:
            raise RuntimeError(
                f"픽스처 조각 {index} 오프셋이 요청({chunk.offset})과 다릅니다: {offset!r} ({path})"
            )
        picked[index] = ChunkResponse(index=index, offset=float(offset), payload=payload)
    absent = sorted(set(requested) - set(picked))
    if absent:
        raise RuntimeError(f"픽스처에 없는 조각: {absent} ({path})")
    return [picked[chunk.index] for chunk in sorted(chunks, key=lambda chunk: chunk.index)]


def load_fixture_responses(path: Path) -> list[ChunkResponse]:
    """검증 스크립트가 픽스처 파일 하나만 직접 읽을 때 쓴다."""
    return _load_responses(path, path.stem.split(".")[0])


def load_meta(fixture_dir: Path, stem: str) -> dict[str, Any]:
    path = _fixture_path(fixture_dir, stem, "meta")
    if not path.is_file():
        return {}
    return dict(json.loads(path.read_text(encoding="utf-8")))


# ─────────────────────────────────────────────────────────────
#  실제 호출
# ─────────────────────────────────────────────────────────────


def _is_retryable(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if isinstance(status, int):
        return status == 429 or status >= 500
    return exc.__class__.__name__ in {"APIConnectionError", "APITimeoutError"}


# ─────────────────────────────────────────────────────────────
#  요청 관찰 (S2B-2a) — 실제 호출을 겉에서 본다. 출력은 못 바꾼다
# ─────────────────────────────────────────────────────────────


TRANSCRIPTION_OPERATION = "audio.transcriptions"
"""오디오 전사 SDK 연산 — `client.audio.transcriptions.create` 한 번."""

RESPONSES_OPERATION = "responses.create"
"""Responses SDK 연산 — `client.responses.create` 한 번."""

MESSAGES_OPERATION = "messages.create"
"""Claude Messages API 연산 — `POST /v1/messages` 한 번(D54)."""

GENERATE_CONTENT_OPERATION = "models.generateContent"
"""Gemini generateContent 연산 — `POST /v1beta/models/{model}:generateContent` 한 번(W-30a, D83)."""
GEMINI_TRANSCRIBE_OPERATION = "interactions.transcribe"
"""시간 엔진 Gemini 3.5 Transcribe 연산 — `POST /v1beta/interactions` 한 번(W-31a, D92)."""

_REDACTED_OPTION_KEYS = frozenset({"api_key", "extra_headers"})
"""메타의 인자 사본에 아예 담지 않는 자격 칸 — 나가는 요청에서 걷어내는 게 아니라
비추지 않는 것뿐이다(요건 2)."""


class ObserverFailure(RuntimeError):
    """관찰자(회계 기반구조) 자신의 실패 — SDK 오류와는 종류가 다른 치명 사고다.

    SDK 오류로 위장할 수 없다. 이 클래스는 `status_code`를 물려받지 않으므로
    `_is_retryable`·`_is_bad_request` 어느 재시도 판정에도 걸리지 않고, 관찰자의
    실패를 오류 훅으로 다시 보고하지도 않는다. 원인 예외는 `__cause__` 사슬로
    보존된다. 관찰자가 없는 호출자는 이 예외를 아예 볼 수 없다.
    """


@dataclass(frozen=True, slots=True)
class RequestAttemptMeta:
    """실제 SDK 호출 **시도 하나**의 불변 신원(S2B-2a).

    `attempt_id`는 SDK 요청 한 번의 신원이지 프로젝트 실행의 AnalysisAttempt ID와는
    다른 층이다 — 재시도·거부 인자 재요청마다 새로 생긴다. 넣는 것: 시도 신원·실제
    모델·연산·(오디오) 작업 종류와 출처·조각 좌표·실제로 실리는 인자의 방어 사본.
    안 넣는 것: API 키·인증 헤더·파일 핸들. 엔드포인트·설정·가격 신원은 관찰자를
    만드는 호출자 소유다 — 이 파일은 환경 탐색을 하지 않는다.

    `options`는 나가는 요청의 깊은 사본을 읽기 전용으로 감싼 것이다. 관찰자가 이
    사본을 어떻게 손대도 나가는 요청은 변하지 않고, 나르개의 자격 칸(`api_key`·
    `extra_headers`)은 사본에 아예 담지 않는다.
    """

    attempt_id: str
    operation: str
    model: str
    kind: str = ""  # 오디오 호출의 작업 종류. Responses 호출은 비워 둔다
    stem: str = ""
    source: str = ""
    chunk_index: int | None = None
    chunk_offset: float | None = None
    chunk_duration: float | None = None
    # 빈 사전을 그대로 기본값으로 두면 3.11의 dataclass가 "해시 불가능한 기본값"으로
    # 거부한다(mappingproxy의 hash 지원은 3.12부터) — 공장으로 만들어 피한다.
    options: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))


class RequestObserver(Protocol):
    """실제 호출 하나하나를 겉에서 보는 주입 경계(S2B-2a).

    구현체가 실제로 여럿(예산 원장 어댑터·기록기)이므로 Protocol이 필요하다 —
    `Transcriber`·`VisionResponder`와 같은 판단이다. 티켓은 불투명하다: `authorize`가
    돌려준 값을 되돌려줄 뿐 해석하지 않는다. 훅이 던지면 그 관찰자는 고장난 것이다
    — `ObserverFailure`로 갈라 올라가고 SDK 재시도로 취급되지 않는다.
    """

    def authorize(self, meta: RequestAttemptMeta) -> Any: ...

    def on_response(self, ticket: Any, meta: RequestAttemptMeta, response: Any) -> None: ...

    def on_error(self, ticket: Any, meta: RequestAttemptMeta, error: Exception) -> None: ...


@dataclass(slots=True)
class _AttemptWatch:
    """`transcribe` 호출 하나의 관찰 맥락. 치명 표시는 잠금 안에서 한 번만 정해진다.

    `admit`의 잠금이 이 호출의 **입장 선형화 지점**이다 — 치명 확인·취소 확인·관찰자
    승인이 같은 잠금 안에서 일어나므로 그 사이를 비집고 들어올 틈이 없다. 이미
    승인받은 병렬 조각은 그대로 끝까지 돌고 결과를 보고한다 — 그 위의 실행 전체를
    끊는 건 이 계층의 권한이 아니다.
    """

    observer: RequestObserver
    stem: str = ""
    source: str = ""
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _fatal: ObserverFailure | None = None

    def admit(
        self,
        make_meta: Callable[[], RequestAttemptMeta],
        gate: Callable[[], None] | None = None,
    ) -> tuple[Any, RequestAttemptMeta]:
        """치명 확인 · 메타 준비 · 취소 확인 · 승인을 한 잠금 안에서 끝낸다(S2B-2a).

        취소 확인이 잠금 **안**에 있는 것이 핵심이다 — 입장 줄에 서서 다른 승인을
        기다리는 동안 취소가 켜져도 잠금을 얻는 순간 새로 확인하므로 그 시도의
        승인은 일어나지 않는다.
        """
        with self._lock:
            if self._fatal is not None:
                raise ObserverFailure(
                    "관찰자 치명 실패 뒤에는 새 승인이 없다 — 이 시도는 SDK를 부르지 않는다"
                ) from self._fatal
            try:
                meta = make_meta()
            except Exception as exc:  # noqa: BLE001 - 관찰 준비의 실패도 관찰자 실패다
                failure = ObserverFailure(f"요청 관찰의 메타 준비가 실패했다: {exc}")
                self._fatal = failure
                raise failure from exc
            if gate is not None:
                gate()  # 메타 뒤·승인 직전 — 잠금 안의 마지막 확인
            try:
                ticket = self.observer.authorize(meta)
            except Exception as exc:  # noqa: BLE001 - 관찰자 실패로 바꿔 올린다
                failure = ObserverFailure(f"요청 관찰자의 authorize 훅이 실패했다: {exc}")
                self._fatal = failure  # 잠금 안이다 — 첫 실패가 그대로 first-wins다
                raise failure from exc
            return ticket, meta

    def record_fatal(self, failure: ObserverFailure) -> None:
        with self._lock:
            if self._fatal is None:
                self._fatal = failure


def _hook_failure(watch: _AttemptWatch, stage: str, exc: Exception) -> ObserverFailure:
    """관찰자 실패를 치명 사고로 만든다. 첫 실패가 이 호출의 새 승인을 막는다."""
    failure = ObserverFailure(f"요청 관찰자의 {stage} 훅이 실패했다: {exc}")
    watch.record_fatal(failure)
    return failure


def _observed_call(
    watch: _AttemptWatch,
    make_meta: Callable[[], RequestAttemptMeta],
    call: Callable[[Any], Any],
    prepare: Callable[[], Any] | None = None,
    gate: Callable[[], None] | None = None,
) -> Callable[[], Any]:
    """SDK 호출 하나를 관찰 훅으로 감싼다 — S2B-2a 요건 7의 작은 래퍼.

    `_retry`는 결과를 일반 호출처럼 부른다. 안에서는 (지역 전제) → 승인 → (취소
    확인) → SDK 호출 → 응답/오류 훅 순서로만 움직이므로 재시도·거부 인자 재요청마다
    새 티켓이 자연히 생긴다. 승인 **직전**의 취소 확인은 `watch.admit`의 잠금 안에
    하나만 있다 — 줄 밖의 확인은 기다림 사이가 뚫리므로 따로 두지 않는다. 관찰자
    자신의 실패는 `ObserverFailure`로 갈라 올라가 SDK 예외와 섞이지 않는다. 취소
    (BaseException)는 오류 훅 없이 그대로 올라간다 — 승인이 이미 났다면 예약은
    불확실성으로 보수적으로 남는다.
    """

    def attempt() -> Any:
        prepared = None if prepare is None else prepare()  # 지역 전제가 승인보다 먼저다
        try:
            ticket, meta = watch.admit(make_meta, gate)
            if gate is not None:
                gate()  # 승인이 늦어지는 동안 취소가 왔으면 SDK를 부르지 않는다
            try:
                response = call(prepared)
            except Exception as exc:  # noqa: BLE001 - 오류 훅이 먼저, 재분류는 _retry가 한다
                try:
                    watch.observer.on_error(ticket, meta, exc)
                except Exception as hook_exc:  # noqa: BLE001
                    raise _hook_failure(watch, "on_error", hook_exc) from hook_exc
                raise
            try:
                watch.observer.on_response(ticket, meta, response)
            except Exception as exc:  # noqa: BLE001
                raise _hook_failure(watch, "on_response", exc) from exc
            return response
        finally:
            if prepared is not None:
                prepared.close()

    return attempt


def _snapshot_options(options: Mapping[str, Any]) -> Mapping[str, Any]:
    """나가는 인자의 깊은 사본을 읽기 전용으로 돌려준다. 자격 칸은 담지 않는다.

    사본일 뿐 나가는 요청은 한 글자도 안 바뀐다 — 관찰자가 이 사본을 어떻게 손대도
    실제 요청은 그대로다(요건 2).
    """
    return MappingProxyType(
        {
            name: deepcopy(value)
            for name, value in options.items()
            if name not in _REDACTED_OPTION_KEYS
        }
    )


def _audio_attempt_meta(
    watch: _AttemptWatch,
    chunk: Chunk,
    kind: str,
    required: dict[str, Any],
    extras: dict[str, Any],
) -> RequestAttemptMeta:
    """오디오 시도의 관찰 메타. 인자는 **지금 이 시도에** 실릴 것만 담는다.

    400으로 인자를 떼고 다시 부르는 재요청은 `extras`가 줄어든 채로 여기를 다시
    지나므로 관찰자가 원요청과 축소 재요청을 구분할 수 있다.
    """
    return RequestAttemptMeta(
        attempt_id=uuid4().hex,
        operation=TRANSCRIPTION_OPERATION,
        model=str(required.get("model", "")),
        kind=kind,
        stem=watch.stem,
        source=watch.source,
        chunk_index=chunk.index,
        chunk_offset=chunk.offset,
        chunk_duration=chunk.duration,
        options=_snapshot_options({**required, **extras}),
    )


def _responses_attempt_meta(
    model: str, payload: Mapping[str, object], operation: str = RESPONSES_OPERATION
) -> RequestAttemptMeta:
    """Responses·Messages 호출의 관찰 메타. 페이로드는 방어적 깊은 사본이다.

    창·블록 같은 사업 신원은 페이로드 안에 있고 그 해석은 호출자 소유다 — 여기는
    실려 나가는 값 자체만 비춘다.
    """
    return RequestAttemptMeta(
        attempt_id=uuid4().hex,
        operation=operation,
        model=model,
        options=_snapshot_options({"model": model, **dict(payload)}),
    )


# A stalled HTTP read must surface as a failed attempt instead of pinning a job
# forever; retries stay in `_retry`, so this bounds one attempt only.
SDK_TIMEOUT_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class LiveTranscriber:
    """실제 OpenAI 호출. `record`와 `--online`에서만 만들어진다.

    `cancellation`은 외부 호출 직전 게이트다(S1A) — 조각·재시도·거부 인자 재요청의
    모든 시도가 `_retry`를 지나므로 게이트도 거기서 한 번에 지난다. 게이트가 던지는
    취소(WorkflowCancelled, BaseException)는 아래 `except Exception` 재시도 판단에
    잡히지 않고 그대로 올라간다. 기본 None은 하위 호환이다 — 게이트 없이 만든
    호출자(레거시 `pipeline`)는 예전과 같이 움직인다.

    `observer`는 실제 SDK 호출 하나하나를 겉에서 보는 선택 경계다(S2B-2a). 맨 뒤에
    붙었으니 위치 인자 호출자는 그대로다. 기본 None은 하위 호환 — 관찰자가 없으면
    지금과 완전히 같이 움직인다. 키는 repr에 나오지 않는다.
    """

    api_key: str = field(repr=False)
    base_url: str | None = None
    cancellation: Callable[[], None] | None = None
    observer: RequestObserver | None = None

    def _client(self) -> Any:
        import openai  # 지연 import — 오프라인 경로에서는 패키지조차 건드리지 않는다.

        # SDK 기본 재시도(2, 설치된 3.8.0 실측)는 `_retry`의 게이트를 우회하는 숨은
        # 재호출이다 — 재시도는 게이트를 지나는 `_retry`에서만 일어난다.
        if self.base_url:
            return openai.OpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
                max_retries=0,
                timeout=SDK_TIMEOUT_SECONDS,
            )
        return openai.OpenAI(api_key=self.api_key, max_retries=0, timeout=SDK_TIMEOUT_SECONDS)

    def _gate(self) -> None:
        """매 API 시도 직전 취소 확인 — 취소 뒤 새 조각·재시도·재요청이 없다."""
        if self.cancellation is not None:
            self.cancellation()

    def _retry(self, call: Callable[[], Any], what: str, cfg: Config) -> Any:
        last: Exception | None = None
        for attempt in range(cfg.api_max_retries):
            self._gate()
            try:
                return call()
            except Exception as exc:  # noqa: BLE001 - 재시도 판단 후 즉시 재던진다
                last = exc
                if not _is_retryable(exc) or attempt == cfg.api_max_retries - 1:
                    raise
                delay = cfg.api_backoff_base**attempt
                log.warn(f"{what} 실패({exc.__class__.__name__}). {delay:.0f}초 후 재시도.")
                time.sleep(delay)
        raise RuntimeError(f"{what} 재시도 실패") from last

    def _one_chunk(
        self,
        client: Any,
        chunk: Chunk,
        cfg: Config,
        extras: dict[str, Any],
        kind: str = FIXTURE_KIND,
        watch: _AttemptWatch | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """청크 하나를 전사한다. 응답과 **통과한 부가 인자**를 함께 돌려준다.

        400이면 오류 메시지가 지목한 인자만 떼고 다시 부른다. 아무 이름도 짚어주지
        않으면 그때만 전부 뗀다 — 힌트 하나 때문에 언어 지정까지 잃을 이유가 없다.
        `watch`가 있으면 매 시도를 관찰 훅으로 감싼다(S2B-2a).
        """
        what = f"{_KIND_LABELS.get(kind, '전사')} 조각 {chunk.index}"
        while True:
            required = _required(cfg, kind)
            attempt: Callable[[], Any] = partial(_create, client, chunk, required, extras)
            if watch is not None:
                attempt = _observed_call(
                    watch,
                    partial(_audio_attempt_meta, watch, chunk, kind, required, extras),
                    partial(_send_audio, client, required, extras),
                    prepare=partial(_open_audio, chunk),
                    gate=self.cancellation,
                )
            try:
                return _to_dict(self._retry(attempt, what, cfg)), extras
            except Exception as exc:  # noqa: BLE001 - 400만 걸러내고 나머지는 재던진다
                if not _is_bad_request(exc):
                    raise
                _blame_required(exc, cfg)
                if not extras:
                    raise
                dropped = _rejected(str(exc), extras) or sorted(extras)
                # 400 문면은 그대로 stderr로 나간다 — SDK가 키를 되뇌면 여기가
                # 첫 표면이다. 인자 해석(_rejected)은 원문으로, 인쇄는 가려서.
                log.warn(
                    f"{what}: API가 {dropped}를 거부했다({env.redact_secrets(str(exc))}). "
                    "빼고 다시 부른다."
                )
                extras = {name: value for name, value in extras.items() if name not in dropped}

    def transcribe(self, job: TranscriptionJob, cfg: Config) -> list[ChunkResponse]:
        """자막의 **글자**를 만드는 유일한 호출. 시각은 여기서 나오지 않는다(규칙 1).

        첫 조각을 혼자 보내 인자 조합을 확정한 뒤 나머지를 병렬로 던진다. 인자가
        틀렸을 때 조각 수만큼 똑같이 실패하며 업로드를 낭비하지 않기 위해서다.
        빈 작업은 클라이언트도 관찰 훅도 없이 곧장 돌아간다(요건 3).
        """
        if not job.chunks:
            return []
        client = self._client()
        head, *tail = job.chunks
        opening = _verify_extras(cfg) if job.kind in _HINTED_KINDS else _extras(cfg)
        watch = (
            None
            if self.observer is None
            else _AttemptWatch(observer=self.observer, stem=job.stem, source=job.source)
        )
        first, extras = self._one_chunk(client, head, cfg, opening, job.kind, watch)

        payloads = [first]
        if tail:
            with ThreadPoolExecutor(max_workers=env.max_workers()) as pool:
                payloads.extend(
                    pool.map(
                        lambda chunk: self._one_chunk(client, chunk, cfg, extras, job.kind, watch)[
                            0
                        ],
                        tail,
                    )
                )
        return [
            ChunkResponse(index=chunk.index, offset=chunk.offset, payload=payload)
            for chunk, payload in zip(job.chunks, payloads, strict=True)
        ]

    def send_request(
        self,
        request: AudioTranscriptionRequest,
        cfg: Config,
        *,
        kind: str,
        stem: str = "",
        source: str = "",
    ) -> dict[str, Any]:
        """파생 계층의 공개 전송(A7-1) — 레거시 전사와 **같은 호출 지점**을 지난다.

        검증은 클라이언트를 만들기 전에 끝난다. 400이 나도 인자를 몰래 떼고 다시
        부르지 않는다 — 확인된 기능 등록표가 틀렸다는 신호라 값을 더 쓰지 않고
        그대로 올린다(§11.8). 재시도·취소 게이트·관찰 훅은 `_retry` 한 길이다.
        """
        validate_transport_request(request)
        client = self._client()
        required, extras = _transport_fields(request)
        chunk = request.chunk
        what = f"{_KIND_LABELS.get(kind, kind)} 조각 {chunk.index}"
        attempt: Callable[[], Any]
        if self.observer is None:
            attempt = partial(_create, client, chunk, required, extras)
        else:
            watch = _AttemptWatch(observer=self.observer, stem=stem, source=source)
            attempt = _observed_call(
                watch,
                partial(_audio_attempt_meta, watch, chunk, kind, required, extras),
                partial(_send_audio, client, required, extras),
                prepare=partial(_open_audio, chunk),
                gate=self.cancellation,
            )
        return _to_dict(self._retry(attempt, what, cfg))


def _create(client: Any, chunk: Chunk, required: dict[str, Any], extras: dict[str, Any]) -> Any:
    """관찰이 없을 때의 한 덩어리 길 — 열어서 보내고 닫는다."""
    with chunk.path.open("rb") as handle:
        return _send_audio(client, required, extras, handle)


def _open_audio(chunk: Chunk) -> Any:
    """SDK 호출의 지역 전제(S2B-2a) — 승인은 이 문이 지난 뒤에만 일어난다.

    파일이 없거나 읽히지 않으면 승인도 SDK 호출도 없이 그대로 실패한다.
    """
    return chunk.path.open("rb")


def _send_audio(client: Any, required: dict[str, Any], extras: dict[str, Any], handle: Any) -> Any:
    return client.audio.transcriptions.create(file=handle, **required, **extras)


def _is_bad_request(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    return status == 400


def _required(cfg: Config, kind: str = FIXTURE_KIND) -> dict[str, Any]:
    """뺄 수 없는 인자. 400이 나도 이건 그대로 둔다.

    부가 인자와 달리 400을 맞아도 떨어져 나가지 않는다 — 이게 빠지면 재시도도 같은
    이유로 실패한다.
    """
    if kind in {VERIFY_KIND, ARBITER_KIND}:
        return {"model": cfg.verify_model, "response_format": cfg.verify_format}
    return {"model": cfg.transcribe_model, "response_format": cfg.transcribe_format}


def _blame_required(exc: Exception, cfg: Config) -> None:
    """뺄 수 없는 인자가 거부당했다면 고쳐야 할 설정 키를 이름으로 알려주고 멈춘다."""
    message = str(exc)
    named = [
        (key, value)
        for key, value in (("response_format", cfg.transcribe_format),)
        if value and f"'{key}'" in message
    ]
    if not named:
        return
    key, value = named[0]
    raise RuntimeError(
        f"API가 {key}={value!r}를 거부했습니다: {exc}\n"
        f"  vlogsrt.toml 의 [transcribe] {key} 를 확인하세요.\n"
        f"  세그먼트 시각을 주는 조합이어야 합니다 — whisper-1 + verbose_json."
    ) from exc


def _rejected(message: str, extras: dict[str, Any]) -> list[str]:
    """400 메시지가 따옴표로 짚어준 부가 인자만 고른다. 못 고르면 빈 목록."""
    return sorted(name for name in extras if f"'{name}'" in message or f'"{name}"' in message)


def _extras(cfg: Config) -> dict[str, Any]:
    """자막을 만드는 호출에 실리는 부가 인자. 거부당하면 하나씩 떼어낸다.

    **`prompt`와 `keywords`는 여기 없다(불변 규칙 6).** whisper는 `prompt`를 지시가
    아니라 직전 자막 텍스트로 읽는다 — 한 줄이 `'한국 여행 브이로그'` 138회·564초를
    낳았다(D13, D16). 힌트는 검증 모델로만 간다.

    `temperature`는 결정성 설정일 뿐이라 거부당하면 버려도 자막은 나온다. 값은
    config에서 온다(규칙 8) — 되풀이 재전사가 같은 오디오를 **다른 온도로** 다시
    물으려면 이 값이 바뀔 수 있어야 한다(D27).
    """
    extras: dict[str, Any] = {"temperature": cfg.transcribe_temperature}
    if cfg.language:
        extras["language"] = cfg.language
    return extras


def _verify_extras(cfg: Config) -> dict[str, Any]:
    """대조용 호출에만 실리는 인자. **힌트가 갈 수 있는 유일한 곳이다**(규칙 6).

    이 모델은 `prompt`를 지시로 읽으므로 고유명사를 나열해도 되풀이 사고가 나지 않는다.
    거부당하면 다른 부가 인자처럼 떨어져 나가고, 대조는 힌트 없이 계속된다.
    """
    extras = _extras(cfg)
    if cfg.keywords:
        extras["keywords"] = list(cfg.keywords)
    if cfg.prompt:
        extras["prompt"] = cfg.prompt
    return extras


def _to_dict(response: Any) -> dict[str, Any]:
    if isinstance(response, dict):
        return response
    for attribute in ("model_dump", "to_dict"):
        converter = getattr(response, attribute, None)
        if callable(converter):
            return dict(converter())
    return json.loads(str(response))


def get_transcriber(
    cfg: Config | None = None,
    *,
    online: bool = False,
    force_offline: bool = False,
    cancellation: Callable[[], None] | None = None,
    observer: RequestObserver | None = None,
) -> Transcriber:
    """오프라인이면 언제나 픽스처를 돌려준다. 이 함수가 비용 안전장치다.

    `cancellation`은 실제 호출 경로에만 심는다(S1A) — 픽스처는 외부 호출이
    없어 게이트가 확인할 것도 없다. `observer`도 같은 길이다(S2B-2a): 픽스처
    재생은 관찰 훅을 일으키지 않는다(요건 3).
    """
    if force_offline or (not online and env.is_offline()):
        return FixtureTranscriber(fixture_dir=env.fixture_dir())
    # online=True는 사람이 --online을 직접 친 경우뿐이다. 그때만 오프라인 기본값을 넘는다.
    return LiveTranscriber(
        api_key=env.require_api_key(force=online),
        base_url=env.base_url(),
        cancellation=cancellation,
        observer=observer,
    )


# ─────────────────────────────────────────────────────────────
#  공개 전송 경계 (A7-1) — 파생 계층이 실제 호출에 쓰는 좁은 타입 요청
# ─────────────────────────────────────────────────────────────
#
# 파생 계층(vlogsrt/project/raw)은 SDK를 모른다. 이 아래의 불변 요청형과
# 전송 메서드가 **이미 존재하는** 유일한 호출 지점(_send_audio)을 그대로 쓴다 —
# 호출 지점이 둘이면 출처가 둘이다(불변 규칙 1). 역할별 조합의 금지(문구 모델에
# 세그먼트 시간 요청 등)는 파생 계층의 기능 등록표가 정하고, 여기는 모든 역할에
# 참인 형태만 검사한다.

TRANSPORT_RESPONSE_FORMATS = ("json", "verbose_json")
"""전송 경계가 아는 응답 형식 둘뿐이다 — srt·vtt는 어떤 역할에도 못 실는다."""

ISO_639_1_LENGTH = 2
"""whisper 단수 `language`의 모양 — ISO 639-1 코드는 두 글자다(D77)."""

TRANSPORT_TIMESTAMP_GRANULARITIES = ("segment", "word")
"""timestamp_granularities의 API enum. word는 별도 계약이 필요하다(§11.3)."""


class UnsupportedTranscriptionRequest(ValueError):
    """전송 요청의 형태가 계약을 어긴다 — 호출 한 번도 나가기 전에 닫힌다."""


@dataclass(frozen=True, slots=True)
class AudioTranscriptionRequest:
    """파생 계층이 실제 호출에 실을 수 있는 인자 전부 — 이 칸 밖은 없다.

    `logprobs`를 보낼 칸이 아예 없다. `response_format`은 확인된 두 형식만
    받는다(아래 검사). 문구 어댑터는 힌트 칸만 쓰고 시간 어댑터는 세그먼트
    입자 칸만 쓴다 — 힌트가 시간 요청에 실리는 일은 어댑터 구조로 막힌다.
    """

    chunk: Chunk
    model: str
    response_format: str
    languages: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    prompt: str = ""
    timestamp_granularities: tuple[str, ...] = ()
    temperature: float | None = None
    # whisper의 단수 언어 칸(D77) — 힌트가 아니라 디코더 언어 고정이다(레거시 `_extras`도 보낸다).
    # 비었으면 칸째 빠진다. 복수형 `languages`(문구 모델 힌트)와 다르다.
    language: str = ""


def transport_request_problems(request: AudioTranscriptionRequest) -> list[str]:
    """모든 역할에 참인 형태 검사. 역할별 조합은 등록표 계층의 몫이다(A7-1)."""
    problems: list[str] = []
    if not request.model.strip():
        problems.append("모델이 비어 있다")
    if request.response_format not in TRANSPORT_RESPONSE_FORMATS:
        problems.append(
            f"응답 형식 {request.response_format!r}는 전송 계약에 없다 — srt·vtt는 못 실는다"
        )
    problems += _hint_shape_problems(request.languages, request.keywords, request.prompt)
    problems += _granularity_problems(request)
    problems += _language_problems(request.language)
    if request.temperature is not None and not math.isfinite(request.temperature):
        problems.append("온도가 유한 숫자가 아니다")
    if not _finite_number(request.chunk.offset) or not _finite_number(request.chunk.duration):
        problems.append(f"조각 {request.chunk.index} 좌표가 유한 숫자가 아니다")
    return problems


def _hint_shape_problems(
    languages: tuple[str, ...], keywords: tuple[str, ...], prompt: str
) -> list[str]:
    """힌트는 전송 전에 검사한다 — 줄바꿈·제어문자는 못 넣는다(§11.4)."""
    problems: list[str] = []
    for label, values in (("예상 언어", languages), ("고유명사 힌트", keywords)):
        for value in values:
            if not value.strip():
                problems.append(f"{label}에 빈 문자열이 있다")
            elif any(unicodedata.category(char).startswith("C") for char in value):
                problems.append(f"{label}에 줄바꿈·제어문자가 있다")
    if prompt and any(unicodedata.category(char).startswith("C") for char in prompt):
        problems.append("문맥 힌트에 줄바꿈·제어문자가 있다")
    return problems


def _granularity_problems(request: AudioTranscriptionRequest) -> list[str]:
    """입자는 API enum 안이고 verbose_json과만 짝한다 — json+입자는 서버가 거절한다."""
    problems = [
        f"시간 입자 {granularity!r}는 API enum에 없다"
        for granularity in request.timestamp_granularities
        if granularity not in TRANSPORT_TIMESTAMP_GRANULARITIES
    ]
    if request.timestamp_granularities and request.response_format != "verbose_json":
        problems.append("시간 입자는 verbose_json과만 함께 보낼 수 있다")
    return problems


def _language_problems(language: str) -> list[str]:
    """단수 언어는 ISO 639-1 두 글자 소문자다 — 나머지는 서버가 거절하거나 다른 뜻으로 읽는다."""
    if not language:
        return []
    if (
        len(language) != ISO_639_1_LENGTH
        or not language.isascii()
        or not language.isalpha()
        or not language.islower()
    ):
        return [f"언어 {language!r}는 ISO 639-1 두 글자 소문자가 아니다"]
    return []


def validate_transport_request(request: AudioTranscriptionRequest) -> None:
    """문제가 하나라도 있으면 호출 전에 끝난다 — 유료 호출은 깨진 요청에 값을 쓰지 않는다."""
    problems = transport_request_problems(request)
    if problems:
        raise UnsupportedTranscriptionRequest("전송 요청이 계약을 어긴다: " + "; ".join(problems))


_SDK_NATIVE_HINT_CACHE: tuple[str, ...] | None = None
"""프로세스마다 한 번만 묻는다 — 설치된 SDK가 languages·keywords를 정식 인자로 받는가."""


def sdk_native_hint_params() -> tuple[str, ...]:
    """설치된 SDK가 정식 인자로 받는 힌트 칸 — 3.8.0 실측으로 둘 다 정식이다.

    정식 인자가 없는 옛 버전에서는 공식 안내의 `extra_body` 길로 보낸다. 어느
    한쪽만 쓰므로 같은 필드가 두 경로로 중복 전달되는 일은 없다(§11.4).
    """
    global _SDK_NATIVE_HINT_CACHE
    if _SDK_NATIVE_HINT_CACHE is None:
        _SDK_NATIVE_HINT_CACHE = _probe_sdk_native_hint_params()
    return _SDK_NATIVE_HINT_CACHE


def _probe_sdk_native_hint_params() -> tuple[str, ...]:
    import openai  # 지연 import — openai을 얻는 유일한 파일이다(불변 규칙 5)

    try:
        signature = inspect.signature(openai.resources.audio.transcriptions.Transcriptions.create)
    except (AttributeError, TypeError, ValueError):
        # 인터페이스를 들여다볼 수 없는 빌드에서 정식 인자라고 단정하면 안 된다 —
        # 문서화된 extra_body 길이 안전하다. 실패를 숨기는 게 아니라 보수적 선택이다.
        return ()
    return tuple(name for name in ("languages", "keywords") if name in signature.parameters)


def _transport_fields(request: AudioTranscriptionRequest) -> tuple[dict[str, Any], dict[str, Any]]:
    """요청을 뺄 수 없는 인자와 부가 인자로 가른다. 빈 힌트는 필드째 뺀다(§11.4)."""
    required = {"model": request.model, "response_format": request.response_format}
    extras: dict[str, Any] = {}
    if request.temperature is not None:
        extras["temperature"] = request.temperature
    if request.timestamp_granularities:
        extras["timestamp_granularities"] = list(request.timestamp_granularities)
    if request.language:
        extras["language"] = request.language
    extras.update(_hint_fields(request.languages, request.keywords, request.prompt))
    return required, extras


def _hint_fields(
    languages: tuple[str, ...], keywords: tuple[str, ...], prompt: str
) -> dict[str, Any]:
    """힌트 필드 조립 — SDK 정식 인자 또는 extra_body, 한쪽만(§11.4).

    `prompt`는 옛 SDK에서도 정식 인자다. languages·keywords만 SDK 버전에 따라
    갈린다. 비어 있으면 아예 실리지 않는다.
    """
    native = frozenset(sdk_native_hint_params())
    fields: dict[str, Any] = {}
    body: dict[str, Any] = {}
    for name, values in (("languages", languages), ("keywords", keywords)):
        if not values:
            continue
        packed = list(values)
        if name in native:
            fields[name] = packed
        else:
            body[name] = packed
    if prompt:
        fields["prompt"] = prompt
    if body:
        fields["extra_body"] = body
    return fields


class TranscriptionSender(Protocol):
    """파생 계층이 보는 좁은 전송 경계 — 구현체는 LiveTranscriber와 시험 픽스처다."""

    def send_request(
        self,
        request: AudioTranscriptionRequest,
        cfg: Config,
        *,
        kind: str,
        stem: str = "",
        source: str = "",
    ) -> dict[str, Any]: ...


# ─────────────────────────────────────────────────────────────
#  비전 호출 경계 (P5-05) — 파생 계층 비전 모델의 유일한 호출 지점
# ─────────────────────────────────────────────────────────────


_PUBLIC_OPENAI_BASE_URL = "https://api.openai.com/v1"
"""SDK 3.8.0이 환경 변수 없이 쓰는 공식 공개 엔드포인트(`_client.py`의 기본값).

`base_url=None`을 SDK에 그대로 넘기면 SDK가 먼저 `OPENAI_BASE_URL`을 물려받는다
(설치된 3.8.0 실측) — 레거시 STT 프록시가 비전 쪽 공급자를 바꿔치는 길이다.
None은 "환경을 따라간다"가 아니라 **공개 엔드포인트**라 여기서 명시적으로 못 박는다.
"""


def create_vision_client(*, api_key: str, base_url: str | None) -> Any:
    """비전 전용 클라이언트 — 값은 받은 것만 쓴다.

    환경·파일을 스스로 읽지 않는다. 실제 값은 사람이 승인한 온라인 실행기가
    `vlogsrt.project.toml`의 자격증명 이름(`vision_api_key_env`·`vision_base_url_env`,
    P5-00)으로 명시적으로 풀어 넘긴다. 레거시 STT helpers(`env.require_api_key`·
    `env.base_url`)를 물려받지 않는다 — 한쪽 키가 양쪽 공급자를 열면 안 된다.

    `base_url=None`은 환경 상속이 아니라 공개 엔드포인트다(`_PUBLIC_OPENAI_BASE_URL`
    문서 참조). 호출자가 준 base_url은 그대로 간다.

    `max_retries=0`도 명시적으로 못 박는다 — SDK 기본값(2, 설치된 3.8.0 실측)은
    `request_vision`의 "재시도 없음" 계약 뒤에 숨어서 429·타임아웃을 조용한 지연으로
    바꾼다. 실패는 한 번의 명시적 시도여야 한다.
    """
    import openai  # 지연 import — openai를 얻는 유일한 파일이다(불변 규칙 5)

    if base_url:
        return openai.OpenAI(
            api_key=api_key, base_url=base_url, max_retries=0, timeout=SDK_TIMEOUT_SECONDS
        )
    return openai.OpenAI(
        api_key=api_key,
        base_url=_PUBLIC_OPENAI_BASE_URL,
        max_retries=0,
        timeout=SDK_TIMEOUT_SECONDS,
    )


def request_vision(
    client: Any, *, model: str, payload: Mapping[str, object], online: bool = False
) -> dict[str, Any]:
    """비전 판정 호출 — 파생 계층이 얻는 유일한 비전 네트워크 경계다.

    `online=True`는 사람이 명시적으로 승인한 실행에서만 온다(기본은 닫힘).
    모델은 호출자가 준 `cfg.vision_model` 문자열 그대로다 — 페이로드가 같은 이름을
    끼어들면 크게 실패하고, 호출이 실패해도 대체 모델로 조용히 갈아끼우지 않는다.
    재시도도 없다: 429·타임아웃은 명시적 오류 기록물이 되어야지 조용한 지연이
    되면 안 된다.
    """
    if not online:
        raise RuntimeError(
            "비전 호출은 명시적 online=True 없이는 닫혀 있다 — "
            "오프라인 기본값을 넘는 것은 사람의 승인이다"
        )
    response = client.chat.completions.create(model=model, **dict(payload))
    return _to_dict(response)


def request_luna_vision(
    client: Any,
    *,
    model: str,
    payload: Mapping[str, object],
    online: bool = False,
    observer: RequestObserver | None = None,
    cancellation: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """승인형 rough-cut 비전의 Responses API 경계.

    기존 B-roll 프로토타입의 Chat Completions 경로를 바꾸지 않고, 새 1.1 분석만
    이 함수를 쓴다. 명시적 승인에서 온 ``online=True`` 없이는 클라이언트도
    만지지 않는다. `observer`는 실제 요청 하나를 겉에서 보는 선택 경계다(S2B-2a)
    — 승인 게이트를 지난 뒤에만 움직이고, 단발 요청이라 기다리는 대기열이 없다.
    `cancellation`은 승인·전송 주위의 선택 게이트다. 이미 날아간 SDK 호출은
    되돌릴 수 없으니 결과 보고는 그대로 둔다.
    """
    if not online:
        raise RuntimeError(
            "Luna 비전 호출은 명시적 online=True 없이는 닫혀 있다 — "
            "AnalysisAuthorization 승인이 필요하다"
        )
    if "model" in payload:
        # 지역 요청 결함이다 — 승인(유료 예약)을 소비한 뒤 SDK가 TypeError로 닫는
        # 길을 만들지 않는다. 관찰자가 없어도 같은 TypeError로 닫힌다.
        raise TypeError(
            f"payload가 model을 다시 정의한다({payload['model']!r}) — "
            "모델의 정본은 함수 인자 하나뿐이다"
        )

    def send(_prepared: Any) -> Any:
        return _responses_create(client, model, payload)

    if observer is None:
        if cancellation is not None:
            cancellation()
        response = send(None)
    else:
        response = _observed_call(
            _AttemptWatch(observer=observer),
            partial(_responses_attempt_meta, model, payload),
            send,
            gate=cancellation,
        )()
    converted = _to_dict(response)
    output_text = getattr(response, "output_text", None)
    if "output_text" not in converted and isinstance(output_text, str):
        converted["output_text"] = output_text
    return converted


def _responses_create(client: Any, model: str, payload: Mapping[str, object]) -> Any:
    return client.responses.create(model=model, **dict(payload))


# ─────────────────────────────────────────────────────────────
#  Claude Messages 경계(D54) — HTTP 전송은 `anthropic_messages`, 문은 여기
# ─────────────────────────────────────────────────────────────


def create_claude_client(
    *, api_key: str, base_url: str, api_version: str, timeout_seconds: float
) -> Any:
    """Claude 의미 분석 클라이언트 — 값은 받은 것만 쓴다(환경·파일을 스스로 읽지 않는다).

    만드는 것만으로는 아무 호출도 없다. 전송 모듈은 지연 import한다 — 네트워크 모듈을
    얻는 파일은 그 하나이고, 그 파일을 얻는 파일은 이 파일 하나다.
    """
    from . import anthropic_messages

    return anthropic_messages.ClaudeMessagesClient(
        api_key=api_key,
        base_url=base_url,
        api_version=api_version,
        timeout_seconds=timeout_seconds,
    )


def is_claude_client(candidate: object) -> bool:
    """Claude 클라이언트인가 — 응답자가 공급자와 클라이언트 종류를 맞춰 보는 자리다."""
    from . import anthropic_messages

    return isinstance(candidate, anthropic_messages.ClaudeMessagesClient)


def request_claude_messages(
    client: Any,
    *,
    model: str,
    payload: Mapping[str, object],
    online: bool = False,
    observer: RequestObserver | None = None,
    cancellation: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """승인형 의미 분석의 Claude Messages 경계 — `request_luna_vision`과 같은 문이다.

    명시적 승인에서 온 ``online=True`` 없이는 클라이언트를 만지지 않는다. 지역 요청
    결함(모델 재정의·Claude가 아닌 클라이언트)은 승인(유료 예약)을 쓰기 **전에** 닫는다.
    재시도는 없다 — 429·529·시간 초과는 명시적 실패 기록이다.
    """
    _require_claude_request(client, payload, online=online)

    def send(_prepared: Any) -> Any:
        return _messages_create(client, model, payload)

    if observer is None:
        if cancellation is not None:
            cancellation()
        return _messages_create(client, model, payload)
    reply: dict[str, Any] = _observed_call(
        _AttemptWatch(observer=observer),
        partial(_responses_attempt_meta, model, payload, MESSAGES_OPERATION),
        send,
        gate=cancellation,
    )()
    return reply


def _require_claude_request(client: Any, payload: Mapping[str, object], *, online: bool) -> None:
    """승인 문과 지역 요청 결함 — 관찰자 승인(유료 예약)보다 먼저 닫는다."""
    if not online:
        raise RuntimeError(
            "Claude 의미 분석 호출은 명시적 online=True 없이는 닫혀 있다 — "
            "AnalysisAuthorization 승인이 필요하다"
        )
    if "model" in payload:
        raise TypeError(
            f"payload가 model을 다시 정의한다({payload['model']!r}) — "
            "모델의 정본은 함수 인자 하나뿐이다"
        )
    if not is_claude_client(client):
        raise TypeError(f"Claude 클라이언트가 아니다: {type(client).__name__}")


def _messages_create(client: Any, model: str, payload: Mapping[str, object]) -> dict[str, Any]:
    from . import anthropic_messages

    return anthropic_messages.post_messages(client, {"model": model, **dict(payload)})


# ─────────────────────────────────────────────────────────────
#  Gemini generateContent 경계(W-30a, D83) — HTTP 전송은 `gemini_messages`, 문은 여기
# ─────────────────────────────────────────────────────────────


def create_gemini_client(
    *, api_key: str, base_url: str, api_version: str, timeout_seconds: float
) -> Any:
    """Gemini 의미 분석 클라이언트 — 값은 받은 것만 쓴다(환경·파일을 스스로 읽지 않는다).

    만드는 것만으로는 아무 호출도 없다. 전송 모듈은 지연 import한다 — 그 파일을 얻는 파일은
    이 파일 하나다(Claude 전송과 같은 선).
    """
    from . import gemini_messages

    return gemini_messages.GeminiClient(
        api_key=api_key,
        base_url=base_url,
        api_version=api_version,
        timeout_seconds=timeout_seconds,
    )


def is_gemini_client(candidate: object) -> bool:
    """Gemini 클라이언트인가 — 응답자가 공급자와 클라이언트 종류를 맞춰 보는 자리다."""
    from . import gemini_messages

    return isinstance(candidate, gemini_messages.GeminiClient)


def request_gemini_generate(
    client: Any,
    *,
    model: str,
    payload: Mapping[str, object],
    online: bool = False,
    observer: RequestObserver | None = None,
    cancellation: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """승인형 의미 분석의 Gemini generateContent 경계 — `request_claude_messages`와 같은 문이다.

    명시적 승인에서 온 ``online=True`` 없이는 클라이언트를 만지지 않는다. 지역 요청 결함(본문의
    모델 재정의·Gemini가 아닌 클라이언트)은 승인(유료 예약)을 쓰기 **전에** 닫는다. 모델은 본문이
    아니라 경로에 실린다 — 관찰 메타에는 다른 경계처럼 `model`이 함께 비친다. 재시도는 없다.
    """
    _require_gemini_request(client, model, payload, online=online)

    def send(_prepared: Any) -> Any:
        return _generate_content(client, model, payload)

    if observer is None:
        if cancellation is not None:
            cancellation()
        return _generate_content(client, model, payload)
    reply: dict[str, Any] = _observed_call(
        _AttemptWatch(observer=observer),
        partial(_responses_attempt_meta, model, payload, GENERATE_CONTENT_OPERATION),
        send,
        gate=cancellation,
    )()
    return reply


def _require_gemini_request(
    client: Any, model: str, payload: Mapping[str, object], *, online: bool
) -> None:
    """승인 문과 지역 요청 결함 — 관찰자 승인(유료 예약)보다 먼저 닫는다. 모델 이름은 경로에
    들어가므로 경로 조각 검사도 여기서 한다(전송이 키를 얹기 전에 한 번 더 본다)."""
    if not online:
        raise RuntimeError(
            "Gemini 의미 분석 호출은 명시적 online=True 없이는 닫혀 있다 — "
            "AnalysisAuthorization 승인이 필요하다"
        )
    if "model" in payload:
        raise TypeError(
            f"payload가 model을 다시 정의한다({payload['model']!r}) — "
            "모델의 정본은 함수 인자 하나뿐이다"
        )
    if not is_gemini_client(client):
        raise TypeError(f"Gemini 클라이언트가 아니다: {type(client).__name__}")
    from . import gemini_messages

    gemini_messages.require_model_segment(model)


def _generate_content(client: Any, model: str, payload: Mapping[str, object]) -> dict[str, Any]:
    from . import gemini_messages

    return gemini_messages.post_generate_content(client, model, payload)


# ─────────────────────────────────────────────────────────────
#  Gemini 3.5 Transcribe 경계(W-31a, D92) — 시간 엔진의 문
# ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class GeminiTranscriptionRequest:
    """시간 엔진 요청 하나 — 오디오 조각 하나를 단어 시각과 함께 받아 적는다(D92).

    보내는 것은 오디오·언어 잠금·verbatim·단어 시각뿐이다. `prompt`·`custom_vocabulary`·
    `system_instruction` 칸은 없다(규칙 6 — 힌트는 시간 엔진에 가지 않는다, 그리고 공급자는
    custom_vocabulary와 단어 시각을 함께 받지 않는다). 한도는 호출자가 등록표에서 넘긴다.
    """

    model: str
    chunk: Chunk
    language_codes: tuple[str, ...]
    max_audio_seconds: float
    max_request_bytes: int
    mime_type: str = "audio/wav"
    mode: str = "verbatim"
    granularities: tuple[str, ...] = ("word",)


def request_gemini_transcription(
    client: Any,
    *,
    request: GeminiTranscriptionRequest,
    online: bool = False,
    observer: RequestObserver | None = None,
    cancellation: Callable[[], None] | None = None,
    stem: str = "",
    source: str = "",
) -> dict[str, Any]:
    """승인형 1단계 전사의 Gemini 3.5 Transcribe 경계 — `request_gemini_generate`의 거울이다.

    명시적 ``online=True`` 없이는 클라이언트를 만지지 않는다. 지역 결함(Gemini가 아닌 클라이언트·
    경로 조각이 아닌 모델·빈 언어·한도를 넘는 길이와 크기)은 승인(유료 예약)보다 **먼저** 닫는다.
    오디오는 인라인 base64로 싣는다(조각이 180초 이하라 20 MB 안 — 업로드·서버 보관이 없다).
    `store: false` — 공급자 쪽에 오디오를 남기지 않는다. 관찰 기록에는 오디오 대신 지문과
    크기만 남는다. 재시도는 없다.
    """
    _require_gemini_transcription(client, request, online=online)
    audio = request.chunk.path.read_bytes()
    body = _transcription_body(request, audio)
    _require_transcription_size(request, body)

    def send(_prepared: Any) -> Any:
        return _interaction(client, body)

    if observer is None:
        if cancellation is not None:
            cancellation()
        return _interaction(client, body)
    watch = _AttemptWatch(observer=observer, stem=stem, source=source)
    reply: dict[str, Any] = _observed_call(
        watch,
        partial(_transcription_attempt_meta, watch, request, body, audio),
        send,
        gate=cancellation,
    )()
    return reply


def _require_gemini_transcription(
    client: Any, request: GeminiTranscriptionRequest, *, online: bool
) -> None:
    if not online:
        raise RuntimeError(
            "Gemini 전사 호출은 명시적 online=True 없이는 닫혀 있다 — 1단계 승인이 필요하다"
        )
    if not is_gemini_client(client):
        raise TypeError(f"Gemini 클라이언트가 아니다: {type(client).__name__}")
    from . import gemini_messages

    gemini_messages.require_model_segment(request.model)
    if not request.language_codes or not all(request.language_codes):
        raise ValueError("Gemini 전사의 언어 잠금(language_codes)이 비었다")
    if request.chunk.duration > request.max_audio_seconds:
        raise ValueError(
            f"조각 {request.chunk.index}의 길이 {request.chunk.duration}초가 단어 시각 전사의"
            f" 한도 {request.max_audio_seconds}초를 넘는다"
        )


def _transcription_body(request: GeminiTranscriptionRequest, audio: bytes) -> dict[str, Any]:
    audio_part = {
        "type": "audio",
        "data": base64.b64encode(audio).decode("ascii"),
        "mime_type": request.mime_type,
    }
    return {
        "model": request.model,
        "store": False,
        "input": [audio_part],
        "generation_config": {"transcription_config": _transcription_config(request)},
    }


def _transcription_config(request: GeminiTranscriptionRequest) -> dict[str, Any]:
    mode = {"type": request.mode, "timestamp_granularities": list(request.granularities)}
    return {"language_codes": list(request.language_codes), "mode": mode}


def _require_transcription_size(request: GeminiTranscriptionRequest, body: dict[str, Any]) -> None:
    """인라인 요청 크기 — 넘으면 보내지 않는다(Files API로 몰래 바꾸지 않는다)."""
    size = len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    if size > request.max_request_bytes:
        raise ValueError(
            f"조각 {request.chunk.index}의 요청 크기 {size}바이트가 인라인 한도"
            f" {request.max_request_bytes}바이트를 넘는다"
        )


def _transcription_attempt_meta(
    watch: _AttemptWatch, request: GeminiTranscriptionRequest, body: dict[str, Any], audio: bytes
) -> RequestAttemptMeta:
    """관찰 메타 — 오디오(수 MB의 base64)는 다시 싣지 않고 sha256과 크기만 남긴다."""
    audio_facts = {
        "type": "audio",
        "mime_type": request.mime_type,
        "sha256": hashlib.sha256(audio).hexdigest(),
        "bytes": len(audio),
    }
    chunk = request.chunk
    return RequestAttemptMeta(
        attempt_id=uuid4().hex,
        operation=GEMINI_TRANSCRIBE_OPERATION,
        model=request.model,
        kind=CAPTION_TIMING_KIND,
        stem=watch.stem,
        source=watch.source,
        chunk_index=chunk.index,
        chunk_offset=chunk.offset,
        chunk_duration=chunk.duration,
        options=_snapshot_options({**body, "input": [audio_facts]}),
    )


def _interaction(client: Any, body: Mapping[str, object]) -> dict[str, Any]:
    from . import gemini_messages

    return gemini_messages.post_interaction(client, body)


class VisionResponder(Protocol):
    """비전 판정의 주입 경계 — `Transcriber`와 대칭이다.

    구현체가 셋(실제/픽스처/닫힘)이라 실제로 필요하다. `block_id`는 픽스처를
    블록마다 고르는 재료다 — 실제·닫힘 구현은 그 값을 쓰지 않아도 계약의
    대칭이 우선한다.
    """

    def decide(
        self, *, model: str, payload: Mapping[str, object], block_id: str
    ) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class LiveVisionResponder:
    """실제 비전 호출 — `create_vision_client`로 만든 클라이언트를 들고 있다.

    **유료 경로는 여기 하나뿐이다.** 클라이언트는 사람이 `--online`을 직접 친
    실행에서 `commands`가 만들어 넘긴다 — 이 객체를 만들었다고 저절로 비용이
    나가지는 않고, `request_vision(online=True)`만이 문을 연다.
    """

    client: Any

    def decide(self, *, model: str, payload: Mapping[str, object], block_id: str) -> dict[str, Any]:
        return request_vision(self.client, model=model, payload=payload, online=True)


class MissingVisionFixture(RuntimeError):
    """재생할 비전 픽스처가 없다 — 블록 하나의 실패가 아니라 실행 설정의 결함이다.

    `FixtureTranscriber`의 `_missing_fixture`와 같은 선: 전사 픽스처가 없으면
    블록마다 오류로 닫히는 게 아니라 실행 자체가 실패로 끝난다. 러너는 이 예외를
    블록 실패 판정으로 바꾸지 않고 그대로 올린다 — 남은 블록도 같은 이유로
    비어 있으므로 오류 판정 N개는 아무것도 새로 말하지 않는다.
    """


def _vision_fixture_path(namespace: Path, block_id: str) -> Path:
    """namespace 안에 머무는 경로만 만든다 — `_namespace_path`와 같은 선이다.

    파일명 조각 검사일 뿐 샌드박스가 아니다(block_id는 sha256 16진수라 지금은
    항상 통과한다). 검사는 block_id의 모양이 바뀌어도 이 선이 먼저 서 있게 한다.
    """
    if not block_id or block_id in {".", ".."} or "/" in block_id or "\\" in block_id:
        raise RuntimeError(f"비전 픽스처의 block_id가 안전한 파일명 조각이 아닙니다: {block_id!r}")
    return namespace / f"{block_id}.vision.json"


def _missing_vision_fixture(path: Path, block_id: str) -> MissingVisionFixture:
    """문투는 `_missing_fixture`를 따르되, 이 파일은 사람이 직접 놓는다(녹화기가 없다)."""
    return MissingVisionFixture(
        f"비전 픽스처가 없습니다: {path}\n"
        f"재생하려면 그 경로에 파일을 놓아야 합니다:\n"
        f"  {block_id}.vision.json — 기록된 모델 응답 전체(JSON 객체, choices 포함)\n"
        f"블록 아이디와 후보는 artifacts/vision/{block_id}.request.json 덤프에서 확인한다"
    )


@dataclass(frozen=True, slots=True)
class FixtureVisionResponder:
    """녹화된 비전 응답을 읽는 재생기 — 네트워크를 모른다.

    `openai`를 import하지 않고 클라이언트를 만들지도 않는다. 블록마다
    `<namespace>/<block_id>.vision.json`을 읽는데, 모양이 멀쩡한 답의 `reason`에는
    반드시 출처를 달아 돌려준다(`_REPLAY_MARK`) — 이 문장이 판정을 거쳐
    패키지 기록(`project_trace.json`의 `decisions[].reason`)에 실리므로 재생 판정이
    진짜 판정으로 읽히는 길이 없다. 정규화·요청 신원 연결은 실제 호출과 같은 길을
    지나므로 낡은 픽스처는 연결 검사에서 닫힌다.
    """

    namespace: Path

    def decide(self, *, model: str, payload: Mapping[str, object], block_id: str) -> dict[str, Any]:
        path = _vision_fixture_path(self.namespace, block_id)
        if not path.is_file():
            raise _missing_vision_fixture(path, block_id)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"비전 픽스처가 올바른 JSON이 아닙니다: {path} ({exc})") from exc
        if not isinstance(raw, dict):
            raise RuntimeError(f"비전 픽스처가 JSON 객체가 아닙니다: {path}")
        return _marked_as_replayed(raw)


_REPLAY_MARK = "재생 판정(픽스처) — "
"""재생 답변의 reason 앞에 다는 출처 — 패키지 기록까지 그대로 흘러간다(V1)."""


def _marked_as_replayed(raw: dict[str, Any]) -> dict[str, Any]:
    """모양이 멀쩡한 답에만 출처를 단다 — 깨진 답은 손대지 않고 그대로 흘려보낸다.

    깨진 답은 어차피 오류 판정으로 정규화되므로 accepted로 위장될 수 없다. 그래서
    표시는 **승인될 수 있는 형태의 답**에만 필요하고, 그 형태가 아니면 건드리지
    않는다(표시를 다다가 답을 깨뜨리는 것이 더 나쁘다). 답의 다른 칸은 한 글자도
    바꾸지 않는다.
    """
    choices = raw.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        return raw
    first = dict(choices[0])
    message = first.get("message")
    if not isinstance(message, dict):
        return raw
    content = message.get("content")
    if not isinstance(content, str):
        return raw
    try:
        body = json.loads(content)
    except ValueError:
        return raw
    if not isinstance(body, dict) or not isinstance(body.get("reason"), str):
        return raw
    body["reason"] = _REPLAY_MARK + body["reason"]
    replayed_message = dict(message)
    replayed_message["content"] = json.dumps(body, ensure_ascii=False)
    first["message"] = replayed_message
    marked = dict(raw)
    marked["choices"] = [first]
    return marked


@dataclass(frozen=True, slots=True)
class ClosedVisionResponder:
    """재생기도 승인도 선택되지 않은 기본값 — 승인 없는 호출은 닫혀 있다.

    게이트 문구를 다시 쓰지 않는다: `request_vision(online=False)`가 던지는
    그 예외가 곧 이 재생기의 답이다. 블록마다 오류 판정으로 남고 출판은
    종료 1로 막힌다 — 승인의 부재가 승인이 되는 길이 없다.
    """

    def decide(self, *, model: str, payload: Mapping[str, object], block_id: str) -> dict[str, Any]:
        return request_vision(client=None, model=model, payload=payload, online=False)


# ─────────────────────────────────────────────────────────────
#  응답 해석
# ─────────────────────────────────────────────────────────────


def parse_segments(raw: dict[str, Any], offset: float = 0.0, seg_id_base: int = 0) -> list[Segment]:
    """전사 응답에서 골격을 만든다. **시각은 조각 좌표에서 원본 좌표로 옮기기만 한다.**

    `+ offset`이 시각에 가해지는 유일한 계산이고, `offset`은 ffmpeg가 실제로 잘렸다고
    알려준 값이다. 시각을 유도하거나 배분하지 않는다(규칙 1).

    **모델이 보낸 글자는 하나도 버리지 않는다.** 시각이 뒤집혔거나 길이가 0인 세그먼트도
    글자가 있으면 남긴다 — 시각은 `end = start`로 접어두고 정제의 `min_duration`이
    한도 안에서 늘려준다. 버리는 것은 글자가 아예 없는 세그먼트뿐이다(규칙 9).

    신뢰도 메타(`compression_ratio`·`avg_logprob`·`no_speech_prob`)도 함께 옮긴다.
    되풀이를 짚는 신호가 여기 들어 있는데 예전에는 그냥 버렸다(D27).
    """
    if "segments" not in raw:
        raise RuntimeError(
            "전사 응답에 segments가 없습니다. 세그먼트 시각을 주는 모델이어야 합니다.\n"
            "  vlogsrt.toml 의 [transcribe] transcribe_model / transcribe_format 을 확인하세요.\n"
            f"  받은 필드: {sorted(raw)}"
        )
    spine: list[Segment] = []
    for segment in raw["segments"] or []:
        text = str(segment.get("text", "")).strip()
        if not text:
            continue
        start = float(segment.get("start", 0.0)) + offset
        end = float(segment.get("end", 0.0)) + offset
        spine.append(
            Segment(
                id=seg_id_base + len(spine),
                start=start,
                end=max(end, start),
                text=text,
                compression_ratio=float(segment.get("compression_ratio", 0.0)),
                avg_logprob=float(segment.get("avg_logprob", 0.0)),
                no_speech_prob=float(segment.get("no_speech_prob", 0.0)),
            )
        )
    return spine


def parse_text(raw: dict[str, Any]) -> str:
    """응답에서 글자만 꺼낸다. 검증 모델(`gpt-4o-transcribe`)이 주는 것은 이것뿐이다."""
    if "text" in raw:
        return str(raw["text"]).strip()
    return " ".join(str(s.get("text", "")).strip() for s in raw.get("segments", ()))


def build_spine(responses: Sequence[ChunkResponse]) -> list[Segment]:
    """조각별 응답을 시각 순으로 이어 붙여 하나의 골격으로 만든다.

    세그먼트가 하나도 없는 조각은 그 구간에 자막이 서지 않는다는 뜻이다. **멈추지 않고
    경고만 한다**(규칙 9) — 긴 실행이 조각 하나로 통째로 죽는 대가가 더 크다.
    """
    spine: list[Segment] = []
    silent: list[str] = []
    for response in sorted(responses, key=lambda r: r.offset):
        parsed = parse_segments(response.payload, response.offset, len(spine))
        if not parsed:
            silent.append(f"{response.index}번({response.offset:.1f}초)")
        spine.extend(parsed)
    if silent:
        log.warn(
            f"세그먼트가 없이 돌아온 조각 {len(silent)}개: {', '.join(silent)}. "
            f"그 구간에는 자막이 서지 않는다."
        )
    return spine


# ─────────────────────────────────────────────────────────────
#  전사 결과형 (P3-01)
# ─────────────────────────────────────────────────────────────


class EmptyTranscript(RuntimeError):
    """조립 가능한 초안이 하나도 없다. **무음·무성음의 증거가 아니다.**

    응답이 전부 빈 리스트였을 수도 있고, 글이 전부 공백이라 걸러졌을 수도 있다 —
    어느 쪽인지는 이 예외만으로는 알 수 없다.
    """


class ConfirmedNoSpeech(EmptyTranscript):
    """요청한 모든 조각이 정상 응답했고 segments가 전부 명시적으로 비었다."""


class MalformedTranscript(RuntimeError):
    """응답 꼴이 계약을 어겨 초안을 만들 수 없다. 호출은 이웃하지 계산은 못 한다."""


@dataclass(frozen=True, slots=True)
class ChunkCoverage:
    """조각 하나의 응답 덮개. 시각을 만들지 않고 **무엇이 돌아왔는지만** 남긴다."""

    index: int
    offset: float
    duration: float
    segment_count: int


@dataclass(frozen=True, slots=True)
class TranscriptionOutcome:
    """전사 호출 하나의 결과. 예외를 밖으로 새지 않고 상태로 남긴다."""

    status: Literal["success", "empty", "error"]
    segments: tuple[Segment, ...]
    coverage: tuple[ChunkCoverage, ...]
    error: str | None


def _exact_int(value: Any) -> bool:
    """bool은 int의 서브클래스지만 조각 번호로 쓸 수 없다 — 1과 True를 갈라낸다."""
    return isinstance(value, int) and not isinstance(value, bool)


def _finite_number(value: Any) -> bool:
    """숫자이면서 유한한 값만 받는다. bool과 NaN·무한대는 시각이 될 수 없다."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _match_responses(
    chunks: tuple[Chunk, ...], responses: Sequence[ChunkResponse]
) -> list[ChunkResponse]:
    """돌아온 응답을 요청한 조각과 하나씩 짝짓는다. 남는 것·겹치는 것 전부 계약 위반.

    덮개 검사가 느슨하면 조각 하나가 몰래 빠져도 조립은 그대로 돌아간다 — 요청한
    조각 수와 응답 수가 같은지, 번호와 오프셋이 정확히 맞는지를 여기서 못 박는다.
    """
    requested = {chunk.index: chunk for chunk in chunks}
    matched: dict[int, ChunkResponse] = {}
    for response in responses:
        index = response.index
        if not _exact_int(index):
            raise MalformedTranscript(f"응답 조각 번호가 정수가 아닙니다: {index!r}")
        if index not in requested:
            raise MalformedTranscript(f"요청하지 않은 조각 응답이 돌아왔다: {index}")
        if index in matched:
            raise MalformedTranscript(f"조각 {index} 응답이 둘 이상 돌아왔다")
        chunk = requested[index]
        if not _finite_number(response.offset) or response.offset != chunk.offset:
            raise MalformedTranscript(
                f"조각 {index} 응답 오프셋이 요청({chunk.offset})과 다릅니다: {response.offset!r}"
            )
        matched[index] = response
    absent = sorted(set(requested) - set(matched))
    if absent:
        raise MalformedTranscript(f"응답이 돌아오지 않은 조각: {absent}")
    return [matched[chunk.index] for chunk in sorted(chunks, key=lambda chunk: chunk.index)]


def _payload_defect(payload: Any, where: str) -> str | None:
    """응답 꼴의 계약 위반을 하나만 골라 돌려준다. 맞으면 None.

    `build_spine`가 기본값으로 덮던 `start`·`end`도 여기서는 **있어야** 한다 — 0.0으로
    몰래 대체된 시각은 그대로 SRT로 나가기 때문이다. `text`도 명시적인 문자열이어야
    한다 — `str()` 강제는 객체를 글자모양으로 바꿔 내보낸다. 빈 문자열·공백은 위반이
    아니므로 그대로 둔다(걸러져 빈 성공, 규칙 9). 길이 0·뒤집힌 시각도 건드리지 않는다.
    """
    if not isinstance(payload, dict):
        return f"{where}: 응답이 JSON 객체가 아닙니다: {type(payload).__name__}"
    if "segments" not in payload:
        return f"{where}: 응답에 segments가 없습니다"
    segments = payload["segments"]
    if not isinstance(segments, list):
        return f"{where}: segments가 리스트가 아닙니다: {type(segments).__name__}"
    for position, segment in enumerate(segments):
        if not isinstance(segment, dict):
            return f"{where}: 세그먼트 {position}이 JSON 객체가 아닙니다"
        if "text" not in segment:
            return f"{where}: 세그먼트 {position}에 text가 없습니다"
        if not isinstance(segment["text"], str):
            return f"{where}: 세그먼트 {position}의 text가 문자열이 아닙니다: {segment['text']!r}"
        for key in ("start", "end"):
            if key not in segment:
                return f"{where}: 세그먼트 {position}에 {key}가 없습니다"
            if not _finite_number(segment[key]):
                return (
                    f"{where}: 세그먼트 {position}의 {key}가 유한 숫자가 아닙니다: {segment[key]!r}"
                )
        for key in ("compression_ratio", "avg_logprob", "no_speech_prob"):
            if key in segment and not _finite_number(segment[key]):
                return (
                    f"{where}: 세그먼트 {position}의 {key}가 유한 숫자가 아닙니다: {segment[key]!r}"
                )
    return None


def _coverage(chunks: tuple[Chunk, ...], matched: list[ChunkResponse]) -> tuple[ChunkCoverage, ...]:
    """요청한 조각마다 덮개를 하나씩 남긴다. 순서는 조각 번호 순서다."""
    records: list[ChunkCoverage] = []
    for chunk, response in zip(sorted(chunks, key=lambda chunk: chunk.index), matched, strict=True):
        defect = _payload_defect(response.payload, f"조각 {chunk.index}")
        if defect is not None:
            raise MalformedTranscript(defect)
        records.append(
            ChunkCoverage(
                index=chunk.index,
                offset=chunk.offset,
                duration=chunk.duration,
                segment_count=len(response.payload["segments"]),
            )
        )
    return tuple(records)


def _request_defect(chunks: tuple[Chunk, ...]) -> str | None:
    """요청 자체의 계약 위반. 위반이면 위임조차 하지 않는다.

    깨진 요청(빈 조각·겹치는 번호·그르른 오프셋·길이)을 그대로 보내면 같은 응답이
    두 번 조립되는 길이 생긴다 — 유료 호출은 깨진 요청에 값을 쓰지 않는다.
    """
    if not chunks:
        return "요청한 조각이 하나도 없습니다"
    seen: set[int] = set()
    for chunk in chunks:
        if not _exact_int(chunk.index):
            return f"조각 번호가 정수가 아닙니다: {chunk.index!r}"
        if chunk.index in seen:
            return f"요청 안에서 조각 번호가 겹친다: {chunk.index}"
        seen.add(chunk.index)
        if not _finite_number(chunk.offset):
            return f"조각 {chunk.index} 오프셋이 유한 숫자가 아닙니다: {chunk.offset!r}"
        if not _finite_number(chunk.duration) or chunk.duration <= 0:
            return f"조각 {chunk.index} 길이가 양수가 아닙니다: {chunk.duration!r}"
    return None


def transcribe_outcome(
    transcriber: Transcriber, job: TranscriptionJob, cfg: Config
) -> TranscriptionOutcome:
    """전사를 호출하고 결과를 상태로 돌려준다. 예외를 밖으로 새지 않는다.

    'empty'는 **모든 조각이 빈 리스트를 명시적으로** 돌려준 경우뿐이다. 꼴이 틀어진
    경우는 전부 'error'다 — 빈 목록과 못 얻은 목록을 섞으면 안 되기 때문이다. 요청
    가드를 통과해야 위임한다. 낱낱의 세그먼트 해석·조립은 기존 `build_spine` 그대로다
    — 여기는 결과형을 만드는 곳일 뿐.
    """
    try:
        defect = _request_defect(job.chunks)
        if defect is not None:
            raise MalformedTranscript(defect)
        responses = transcriber.transcribe(job, cfg)
        matched = _match_responses(job.chunks, responses)
        coverage = _coverage(job.chunks, matched)
        if all(cover.segment_count == 0 for cover in coverage):
            return TranscriptionOutcome(status="empty", segments=(), coverage=coverage, error=None)
        segments = tuple(build_spine(matched))
    except ObserverFailure:
        # 관찰자(회계) 실패는 전사 결과가 아니라 기반구조 사고다 — 문자열로 상태에
        # 묻으면 원인 사슬이 끊겨 예산 거부가 일반 오류로 위장된다(S4A). 그대로 올린다.
        raise
    except Exception as exc:  # noqa: BLE001 - 상태로 바꿔 남긴다. 조용히 두지는 않는다
        return TranscriptionOutcome(
            status="error", segments=(), coverage=(), error=f"{exc.__class__.__name__}: {exc}"
        )
    return TranscriptionOutcome(status="success", segments=segments, coverage=coverage, error=None)


def require_nonempty(outcome: TranscriptionOutcome) -> tuple[Segment, ...]:
    """쓸 수 있는 초안이 있는 결과만 통과시킨다.

    실패는 둘로 갈린다: 꼴이 틀어졌으면 `MalformedTranscript`에 기록된 맥락을 실어
    던지고, 꼴은 맞는데 초안이 비었으면 `EmptyTranscript`를 던진다.
    """
    if outcome.status == "error":
        raise MalformedTranscript(f"전사가 실패로 돌아왔다: {outcome.error}")
    if not outcome.segments:
        raise EmptyTranscript("조립 가능한 초안이 하나도 없습니다. 무음의 증거는 아니다.")
    return outcome.segments


# ─────────────────────────────────────────────────────────────
#  픽스처 저장 (record 전용)
# ─────────────────────────────────────────────────────────────


def save_fixture(
    fixture_dir: Path, stem: str, kind: str, responses: Sequence[ChunkResponse]
) -> Path:
    fixture_dir.mkdir(parents=True, exist_ok=True)
    path = _fixture_path(fixture_dir, stem, kind)
    body: dict[str, Any] = {
        "chunks": [
            {"index": r.index, "offset": r.offset, "response": r.payload}
            for r in sorted(responses, key=lambda r: r.index)
        ]
    }
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def save_meta(fixture_dir: Path, stem: str, cfg: Config, duration: float, chunk_count: int) -> Path:
    fixture_dir.mkdir(parents=True, exist_ok=True)
    path = _fixture_path(fixture_dir, stem, "meta")
    body = {
        "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "model": cfg.transcribe_model,
        "params": {
            "response_format": cfg.transcribe_format,
            "language": cfg.language,
        },
        "duration": duration,
        "chunk_count": chunk_count,
    }
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def estimate_cost(duration_seconds: float, cfg: Config) -> float:
    """레거시 파생 계층의 자막 추정만 아직 쓴다 — D110이 그 단계를 지울 때 함께 지운다."""
    return duration_seconds / 60.0 * cfg.cost_per_minute
