"""유일한 네트워크 경계(불변 규칙 5).

다른 모듈은 전부 순수 함수다. `openai`를 import하는 파일은 이 파일뿐이며,
그것도 실제 호출 직전에 지연 import한다.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any, Literal, Protocol

from . import env, log
from .config import Config
from .models import Chunk, Segment

FIXTURE_KIND = "whisper"  # 자막을 만드는 호출
VERIFY_KIND = "gpt"  # 대조용 호출. 출력을 바꾸지 못한다(규칙 5)
RESCUE_KIND = "rescue"  # 되풀이 구간만 다시 묻는 호출. 같은 모델, 다른 온도(D27)
ARBITER_KIND = "arbiter"  # 그 구간을 심판하는 두 번째 모델. 시각을 만들지 않는다


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


@dataclass(frozen=True, slots=True)
class LiveTranscriber:
    """실제 OpenAI 호출. `record`와 `--online`에서만 만들어진다.

    `cancellation`은 외부 호출 직전 게이트다(S1A) — 조각·재시도·거부 인자 재요청의
    모든 시도가 `_retry`를 지나므로 게이트도 거기서 한 번에 지난다. 게이트가 던지는
    취소(WorkflowCancelled, BaseException)는 아래 `except Exception` 재시도 판단에
    잡히지 않고 그대로 올라간다. 기본 None은 하위 호환이다 — 게이트 없이 만든
    호출자(레거시 `pipeline`)는 예전과 같이 움직인다.
    """

    api_key: str
    base_url: str | None = None
    cancellation: Callable[[], None] | None = None

    def _client(self) -> Any:
        import openai  # 지연 import — 오프라인 경로에서는 패키지조차 건드리지 않는다.

        # SDK 기본 재시도(2, 설치된 3.8.0 실측)는 `_retry`의 게이트를 우회하는 숨은
        # 재호출이다 — 재시도는 게이트를 지나는 `_retry`에서만 일어난다.
        if self.base_url:
            return openai.OpenAI(api_key=self.api_key, base_url=self.base_url, max_retries=0)
        return openai.OpenAI(api_key=self.api_key, max_retries=0)

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
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """청크 하나를 전사한다. 응답과 **통과한 부가 인자**를 함께 돌려준다.

        400이면 오류 메시지가 지목한 인자만 떼고 다시 부른다. 아무 이름도 짚어주지
        않으면 그때만 전부 뗀다 — 힌트 하나 때문에 언어 지정까지 잃을 이유가 없다.
        """
        what = f"{_KIND_LABELS.get(kind, '전사')} 조각 {chunk.index}"
        while True:
            attempt = partial(_create, client, chunk, _required(cfg, kind), extras)
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
        """
        if not job.chunks:
            return []
        client = self._client()
        head, *tail = job.chunks
        opening = _verify_extras(cfg) if job.kind in _HINTED_KINDS else _extras(cfg)
        first, extras = self._one_chunk(client, head, cfg, opening, job.kind)

        payloads = [first]
        if tail:
            with ThreadPoolExecutor(max_workers=env.max_workers()) as pool:
                payloads.extend(
                    pool.map(
                        lambda chunk: self._one_chunk(client, chunk, cfg, extras, job.kind)[0],
                        tail,
                    )
                )
        return [
            ChunkResponse(index=chunk.index, offset=chunk.offset, payload=payload)
            for chunk, payload in zip(job.chunks, payloads, strict=True)
        ]


def _create(client: Any, chunk: Chunk, required: dict[str, Any], extras: dict[str, Any]) -> Any:
    with chunk.path.open("rb") as handle:
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
) -> Transcriber:
    """오프라인이면 언제나 픽스처를 돌려준다. 이 함수가 비용 안전장치다.

    `cancellation`은 실제 호출 경로에만 심는다(S1A) — 픽스처는 외부 호출이
    없어 게이트가 확인할 것도 없다.
    """
    if force_offline or (not online and env.is_offline()):
        return FixtureTranscriber(fixture_dir=env.fixture_dir())
    # online=True는 사람이 --online을 직접 친 경우뿐이다. 그때만 오프라인 기본값을 넘는다.
    return LiveTranscriber(
        api_key=env.require_api_key(force=online),
        base_url=env.base_url(),
        cancellation=cancellation,
    )


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
        return openai.OpenAI(api_key=api_key, base_url=base_url, max_retries=0)
    return openai.OpenAI(api_key=api_key, base_url=_PUBLIC_OPENAI_BASE_URL, max_retries=0)


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
    client: Any, *, model: str, payload: Mapping[str, object], online: bool = False
) -> dict[str, Any]:
    """승인형 rough-cut 비전의 Responses API 경계.

    기존 B-roll 프로토타입의 Chat Completions 경로를 바꾸지 않고, 새 1.1 분석만
    이 함수를 쓴다. 명시적 승인에서 온 ``online=True`` 없이는 클라이언트도
    만지지 않는다.
    """
    if not online:
        raise RuntimeError(
            "Luna 비전 호출은 명시적 online=True 없이는 닫혀 있다 — "
            "AnalysisAuthorization 승인이 필요하다"
        )
    response = client.responses.create(model=model, **dict(payload))
    converted = _to_dict(response)
    output_text = getattr(response, "output_text", None)
    if "output_text" not in converted and isinstance(output_text, str):
        converted["output_text"] = output_text
    return converted


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
        "cost_estimate": estimate_cost(duration, cfg),
    }
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def estimate_cost(duration_seconds: float, cfg: Config) -> float:
    return duration_seconds / 60.0 * cfg.cost_per_minute
