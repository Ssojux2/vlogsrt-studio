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
from pathlib import Path
from typing import Any, Protocol

from . import env, log
from .config import Config
from .models import (
    VERDICT_COMPRESSION_FAIL,
    VERDICT_LOW_LOGPROB,
    VERDICT_OK,
    VERDICT_SILENT,
    Chunk,
    Segment,
    Word,
)

Span = tuple[float, float]


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

    def whisper(self, job: TranscriptionJob, cfg: Config) -> list[ChunkResponse]: ...

    def gpt(self, job: TranscriptionJob, cfg: Config) -> list[ChunkResponse]: ...


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
        f"예상 비용: 약 $0.06 (5분 영상 기준)"
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

    def whisper(self, job: TranscriptionJob, cfg: Config) -> list[ChunkResponse]:
        return _load_responses(
            _fixture_path(self.fixture_dir, job.stem, "whisper"), job.stem, job.source
        )

    def gpt(self, job: TranscriptionJob, cfg: Config) -> list[ChunkResponse]:
        return _load_responses(
            _fixture_path(self.fixture_dir, job.stem, "gpt"), job.stem, job.source
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

    def _parallel(
        self, job: TranscriptionJob, cfg: Config, one: Callable[[Chunk], dict[str, Any]]
    ) -> list[ChunkResponse]:
        with ThreadPoolExecutor(max_workers=env.max_workers()) as pool:
            payloads = list(pool.map(one, job.chunks))
        return [
            ChunkResponse(index=chunk.index, offset=chunk.offset, payload=payload)
            for chunk, payload in zip(job.chunks, payloads, strict=True)
        ]

    def whisper(self, job: TranscriptionJob, cfg: Config) -> list[ChunkResponse]:
        client = self._client()
        granularities = ["segment", "word"] if cfg.word_timestamps else ["segment"]

        def one(chunk: Chunk) -> dict[str, Any]:
            def call() -> Any:
                with chunk.path.open("rb") as handle:
                    return client.audio.transcriptions.create(
                        file=handle,
                        model=cfg.whisper_model,
                        language=cfg.language,
                        prompt=cfg.prompt,
                        temperature=0,
                        response_format="verbose_json",
                        timestamp_granularities=granularities,
                    )

            return _to_dict(self._retry(call, f"whisper 청크 {chunk.index}", cfg))

        return self._parallel(job, cfg, one)

    def gpt(self, job: TranscriptionJob, cfg: Config) -> list[ChunkResponse]:
        client = self._client()

        def one(chunk: Chunk) -> dict[str, Any]:
            def call() -> Any:
                # timestamp_granularities / include=logprobs 를 넣지 마라. whisper-1 전용이다.
                with chunk.path.open("rb") as handle:
                    return client.audio.transcriptions.create(
                        file=handle,
                        model=cfg.gpt_model,
                        temperature=0,
                        response_format="json",
                        **_gpt_extras(cfg),
                    )

            return _to_dict(self._retry(call, f"gpt 청크 {chunk.index}", cfg))

        return self._parallel(job, cfg, one)


def _gpt_extras(cfg: Config) -> dict[str, Any]:
    """`language`와 `languages`는 **함께 보낼 수 없다.**

    TASKS.md §T3.1은 둘 다 넘기라고 적어두었지만 실제 API는 400을 돌려준다:
      "The 'language' and 'languages' parameters cannot be used together."
    여러 언어를 알려주는 쪽이 이 프로젝트의 의도라 `languages`를 택하고,
    설정이 비어 있을 때만 단일 `language`로 떨어진다.
    """
    extras: dict[str, Any] = {}
    if cfg.extra_languages:
        extras["languages"] = list(cfg.extra_languages)
    elif cfg.language:
        extras["language"] = cfg.language
    if cfg.keywords:
        extras["keywords"] = list(cfg.keywords)
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


def rebase_timestamps(result: dict[str, Any], offset: float, seg_id_base: int) -> dict[str, Any]:
    """청크 좌표를 원본 좌표로 옮긴다. 원본 dict는 건드리지 않는다.

    빠뜨리기 쉬운 것: `words[]`의 시각과 `segments[].id`의 전역 재번호.
    """
    segments = []
    for local_index, segment in enumerate(result.get("segments") or []):
        moved = dict(segment)
        moved["id"] = seg_id_base + local_index
        moved["start"] = float(segment.get("start", 0.0)) + offset
        moved["end"] = float(segment.get("end", 0.0)) + offset
        segments.append(moved)

    words = []
    for word in result.get("words") or []:
        moved_word = dict(word)
        moved_word["start"] = float(word.get("start", 0.0)) + offset
        moved_word["end"] = float(word.get("end", 0.0)) + offset
        words.append(moved_word)

    rebased = dict(result)
    rebased["segments"] = segments
    rebased["words"] = words
    return rebased


def parse_spine(raw: dict[str, Any], offset: float = 0.0, seg_id_base: int = 0) -> list[Segment]:
    """whisper 응답에서 골격을 만든다. verdict는 여기서 전부 ok — 판정은 judge_verdict."""
    rebased = rebase_timestamps(raw, offset, seg_id_base)
    spine: list[Segment] = []
    dropped = 0
    for segment in rebased["segments"]:
        text = str(segment.get("text", "")).strip()
        start = float(segment["start"])
        end = float(segment["end"])
        if end <= start:
            # 한 줄씩 찍으면 수백 줄이 되어 정작 볼 경고를 묻는다. 끝에서 한 번 센다.
            dropped += 1
            continue
        spine.append(
            Segment(
                id=int(segment["id"]),
                start=start,
                end=end,
                text=text,
                avg_logprob=float(segment.get("avg_logprob", 0.0)),
                compression_ratio=float(segment.get("compression_ratio", 1.0)),
                no_speech_prob=float(segment.get("no_speech_prob", 0.0)),
            )
        )
    _warn_if_many_degenerate(dropped, len(rebased["segments"]))
    return spine


def _warn_if_many_degenerate(dropped: int, total: int) -> None:
    """길이 0인 세그먼트가 쏟아지면 whisper 디코딩이 무너졌다는 신호다.

    정상 전사에서는 거의 없다. 실측(D13)에서는 1931개 중 452개가 이랬고,
    같은 구간이 반복 환청 구간이었다.
    """
    if not dropped:
        return
    share = dropped / total if total else 0.0
    message = f"길이 0인 whisper 세그먼트 {dropped}/{total}개를 건너뛰었다."
    if share > 0.05:
        log.warn(f"{message} ({share * 100:.0f}%) whisper 전사가 불안정하다.")
    else:
        log.debug(message)


def parse_words(raw: dict[str, Any], offset: float = 0.0) -> list[Word]:
    rebased = rebase_timestamps(raw, offset, 0)
    return [Word.from_dict(word) for word in rebased["words"]]


def build_spine(responses: Sequence[ChunkResponse]) -> tuple[list[Segment], list[Word]]:
    """청크별 응답을 이어 붙여 하나의 골격으로 만든다."""
    spine: list[Segment] = []
    words: list[Word] = []
    for response in sorted(responses, key=lambda r: r.offset):
        spine.extend(parse_spine(response.payload, response.offset, len(spine)))
        words.extend(parse_words(response.payload, response.offset))
    return spine, words


def canonical_text(responses: Sequence[ChunkResponse]) -> str:
    """gpt-transcribe 정본. 최종 SRT의 모든 글자는 여기서 나온다(불변 규칙 2)."""
    parts = []
    for response in sorted(responses, key=lambda r: r.offset):
        text = str(response.payload.get("text", "")).strip()
        if text:
            parts.append(text)
    return " ".join(parts)


def overlap_ratio(segment: Segment, silences: Sequence[Span]) -> float:
    if segment.duration <= 0:
        return 0.0
    covered = 0.0
    for start, end in silences:
        covered += max(0.0, min(segment.end, end) - max(segment.start, start))
    return min(1.0, covered / segment.duration)


def judge_verdict(segment: Segment, silences: Sequence[Span], cfg: Config) -> str:
    """판정 순서가 중요하다. compression_ratio를 가장 먼저 본다."""
    if segment.compression_ratio > cfg.compression_ratio_max:
        return VERDICT_COMPRESSION_FAIL
    if segment.avg_logprob < cfg.avg_logprob_min:
        if segment.no_speech_prob > cfg.no_speech_prob_max:
            return VERDICT_SILENT
        return VERDICT_LOW_LOGPROB
    if overlap_ratio(segment, silences) >= cfg.silence_overlap_ratio:
        return VERDICT_SILENT
    return VERDICT_OK


def apply_verdicts(
    spine: Sequence[Segment], silences: Sequence[Span], cfg: Config
) -> list[Segment]:
    """골격을 수정하지 않는다. 판정이 담긴 새 Segment를 만든다(불변 규칙 1)."""
    judged = []
    for segment in spine:
        verdict = judge_verdict(segment, silences, cfg)
        judged.append(segment if verdict == segment.verdict else _with_verdict(segment, verdict))
    return judged


def _with_verdict(segment: Segment, verdict: str) -> Segment:
    return Segment(
        id=segment.id,
        start=segment.start,
        end=segment.end,
        text=segment.text,
        avg_logprob=segment.avg_logprob,
        compression_ratio=segment.compression_ratio,
        no_speech_prob=segment.no_speech_prob,
        verdict=verdict,
    )


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


def save_meta(
    fixture_dir: Path,
    stem: str,
    cfg: Config,
    duration: float,
    silences: Sequence[Span],
    chunk_count: int,
) -> Path:
    fixture_dir.mkdir(parents=True, exist_ok=True)
    path = _fixture_path(fixture_dir, stem, "meta")
    body = {
        "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "models": [cfg.whisper_model, cfg.gpt_model],
        "params": {
            "language": cfg.language,
            "extra_languages": list(cfg.extra_languages),
            "keywords": list(cfg.keywords),
            "word_timestamps": cfg.word_timestamps,
            "prompt": cfg.prompt,
        },
        "duration": duration,
        "silences": [list(span) for span in silences],
        "chunk_count": chunk_count,
        "cost_estimate": estimate_cost(duration, cfg),
    }
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def estimate_cost(duration_seconds: float, cfg: Config) -> float:
    minutes = duration_seconds / 60.0
    return minutes * (cfg.whisper_cost_per_minute + cfg.gpt_cost_per_minute)
