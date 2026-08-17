"""유일한 네트워크 경계(불변 규칙 5).

다른 모듈은 전부 순수 함수다. `openai`를 import하는 파일은 이 파일뿐이며,
그것도 실제 호출 직전에 지연 import한다.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any, Protocol

from . import env, log
from .config import Config
from .models import Chunk, Segment

FIXTURE_KIND = "transcribe"


@dataclass(frozen=True, slots=True)
class ChunkResponse:
    """청크 하나에 대한 API 응답. `payload`는 재기준화 전 원본이다."""

    index: int
    offset: float
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class TranscriptionJob:
    stem: str
    chunks: tuple[Chunk, ...]
    source: str = ""


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
        return _load_responses(
            _fixture_path(self.fixture_dir, job.stem, FIXTURE_KIND), job.stem, job.source
        )


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
    """실제 OpenAI 호출. `record`와 `--online`에서만 만들어진다."""

    api_key: str
    base_url: str | None = None

    def _client(self) -> Any:
        import openai  # 지연 import — 오프라인 경로에서는 패키지조차 건드리지 않는다.

        if self.base_url:
            return openai.OpenAI(api_key=self.api_key, base_url=self.base_url)
        return openai.OpenAI(api_key=self.api_key)

    def _retry(self, call: Callable[[], Any], what: str, cfg: Config) -> Any:
        last: Exception | None = None
        for attempt in range(cfg.api_max_retries):
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
        self, client: Any, chunk: Chunk, cfg: Config, extras: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """청크 하나를 전사한다. 응답과 **통과한 부가 인자**를 함께 돌려준다.

        400이면 오류 메시지가 지목한 인자만 떼고 다시 부른다. 아무 이름도 짚어주지
        않으면 그때만 전부 뗀다 — 힌트 하나 때문에 언어 지정까지 잃을 이유가 없다.
        """
        what = f"전사 청크 {chunk.index}"
        while True:
            attempt = partial(_create, client, chunk, _required(cfg), extras)
            try:
                return _to_dict(self._retry(attempt, what, cfg)), extras
            except Exception as exc:  # noqa: BLE001 - 400만 걸러내고 나머지는 재던진다
                if not _is_bad_request(exc):
                    raise
                _blame_required(exc, cfg)
                if not extras:
                    raise
                dropped = _rejected(str(exc), extras) or sorted(extras)
                log.warn(f"{what}: API가 {dropped}를 거부했다({exc}). 빼고 다시 부른다.")
                extras = {name: value for name, value in extras.items() if name not in dropped}
                if "languages" in dropped and cfg.language:
                    # 복수형이 거부당했다고 언어 지정 자체를 잃을 이유는 없다.
                    # 단수형으로 바꿔 본다. 이것도 거부당하면 그때 떨어져 나간다.
                    extras["language"] = cfg.language

    def transcribe(self, job: TranscriptionJob, cfg: Config) -> list[ChunkResponse]:
        """자막의 시각과 글자를 모두 만드는 유일한 호출.

        첫 조각을 혼자 보내 인자 조합을 확정한 뒤 나머지를 병렬로 던진다. 인자가
        틀렸을 때 조각 수만큼 똑같이 실패하며 업로드를 낭비하지 않기 위해서다.
        """
        if not job.chunks:
            return []
        client = self._client()
        head, *tail = job.chunks
        first, extras = self._one_chunk(client, head, cfg, _extras(cfg))

        payloads = [first]
        if tail:
            with ThreadPoolExecutor(max_workers=env.max_workers()) as pool:
                payloads.extend(
                    pool.map(lambda chunk: self._one_chunk(client, chunk, cfg, extras)[0], tail)
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


def _required(cfg: Config) -> dict[str, Any]:
    """뺄 수 없는 인자. 400이 나도 이건 그대로 둔다.

    `chunking_strategy`가 여기 있는 이유: diarize 모델은 이 인자 없이는 요청을
    아예 받지 않는다(400 "chunking_strategy is required for diarization models",
    D22). 부가 인자와 함께 떨어져 나가면 재시도도 같은 이유로 실패한다.
    빈 문자열이면 보내지 않는다 — whisper-1은 이 인자를 모른다.
    """
    required: dict[str, Any] = {
        "model": cfg.transcribe_model,
        "response_format": cfg.transcribe_format,
    }
    if cfg.chunking_strategy:
        required["chunking_strategy"] = cfg.chunking_strategy
    return required


def _blame_required(exc: Exception, cfg: Config) -> None:
    """뺄 수 없는 인자가 거부당했다면 고쳐야 할 설정 키를 이름으로 알려주고 멈춘다."""
    message = str(exc)
    named = [
        (key, value)
        for key, value in (
            ("chunking_strategy", cfg.chunking_strategy),
            ("response_format", cfg.transcribe_format),
        )
        if value and f"'{key}'" in message
    ]
    if not named:
        return
    key, value = named[0]
    raise RuntimeError(
        f"API가 {key}={value!r}를 거부했습니다: {exc}\n"
        f"  vlogsrt.toml 의 [transcribe] {key} 를 확인하세요.\n"
        f"  diarize 모델은 chunking_strategy가 필요하고, whisper-1은 이 인자를 모릅니다."
    ) from exc


def _rejected(message: str, extras: dict[str, Any]) -> list[str]:
    """400 메시지가 따옴표로 짚어준 부가 인자만 고른다. 못 고르면 빈 목록."""
    return sorted(name for name in extras if f"'{name}'" in message or f'"{name}"' in message)


def _extras(cfg: Config) -> dict[str, Any]:
    """모델마다 지원 여부가 갈리는 인자들. 거부당하면 하나씩 떼어낸다.

    `language`와 `languages`는 **함께 보낼 수 없다.** 실제 API가 400을 돌려준다:
    "The 'language' and 'languages' parameters cannot be used together." 여러
    언어를 알려주는 쪽이 이 프로젝트의 의도라 `languages`를 택하고, 설정이 비어
    있을 때만 단일 `language`로 떨어진다.

    `temperature`도 여기 있다. 결정성 설정일 뿐이라 거부당하면 버려도 자막은 나온다.
    """
    extras: dict[str, Any] = {"temperature": 0}
    if cfg.extra_languages:
        extras["languages"] = list(cfg.extra_languages)
    elif cfg.language:
        extras["language"] = cfg.language
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
    cfg: Config | None = None, *, online: bool = False, force_offline: bool = False
) -> Transcriber:
    """오프라인이면 언제나 픽스처를 돌려준다. 이 함수가 비용 안전장치다."""
    if force_offline or (not online and env.is_offline()):
        return FixtureTranscriber(fixture_dir=env.fixture_dir())
    # online=True는 사람이 --online을 직접 친 경우뿐이다. 그때만 오프라인 기본값을 넘는다.
    return LiveTranscriber(api_key=env.require_api_key(force=online), base_url=env.base_url())


# ─────────────────────────────────────────────────────────────
#  응답 해석
# ─────────────────────────────────────────────────────────────


def parse_segments(raw: dict[str, Any], offset: float = 0.0, seg_id_base: int = 0) -> list[Segment]:
    """전사 응답에서 골격을 만든다. 시각은 청크 좌표에서 원본 좌표로 옮긴다.

    **모델이 보낸 글자는 하나도 버리지 않는다.** 시각이 뒤집혔거나 길이가 0인
    세그먼트도 글자가 있으면 남긴다 — 시각은 `end = start`로 접어두고, 정제의
    `min_duration`이 규칙 3의 한도 안에서 늘려준다. 버리는 것은 글자가 아예 없는
    세그먼트뿐이고, 그건 화면에 띄울 것이 없다는 뜻이다.

    응답의 `id`는 쓰지 않는다. diarize는 `"seg_0"` 같은 문자열을 주고 청크마다
    0부터 다시 시작하므로, 통번호는 우리가 붙인다.
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
            Segment(id=seg_id_base + len(spine), start=start, end=max(end, start), text=text)
        )
    return spine


def build_spine(responses: Sequence[ChunkResponse]) -> list[Segment]:
    """청크별 응답을 이어 붙여 하나의 골격으로 만든다.

    **빈 청크는 넘어가지 않고 멈춘다.** 세그먼트가 하나도 없는 응답은 그 조각의
    구간 전체가 자막 없이 비워진다는 뜻이다. 25분짜리 구멍이 조용히 뚫린 SRT보다
    실패가 낫다 — 뚫린 자막은 눈으로 보기 전까지 아무도 모른다(D23).
    """
    spine: list[Segment] = []
    empty: list[str] = []
    for response in sorted(responses, key=lambda r: r.offset):
        parsed = parse_segments(response.payload, response.offset, len(spine))
        if not parsed:
            empty.append(f"청크 {response.index}(오프셋 {response.offset:.1f}초)")
        spine.extend(parsed)
    if empty:
        raise RuntimeError(
            f"전사가 비어 돌아온 조각이 있습니다: {', '.join(empty)}\n"
            f"  그 구간의 자막이 통째로 비므로 중단합니다.\n"
            f"  해당 구간에 소리가 있는지 `vlogsrt probe`로 확인하고 다시 실행하세요."
        )
    return spine


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
            "extra_languages": list(cfg.extra_languages),
            "keywords": list(cfg.keywords),
            "prompt": cfg.prompt,
        },
        "duration": duration,
        "chunk_count": chunk_count,
        "cost_estimate": estimate_cost(duration, cfg),
    }
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def estimate_cost(duration_seconds: float, cfg: Config) -> float:
    return duration_seconds / 60.0 * cfg.cost_per_minute
