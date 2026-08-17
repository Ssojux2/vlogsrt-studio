"""데이터 모델. 전부 frozen이다 — spine은 생성 후 수정되지 않는다(불변 규칙 1)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from . import TRACE_VERSION

VERDICT_OK = "ok"
VERDICT_LOW_LOGPROB = "low_logprob"
VERDICT_COMPRESSION_FAIL = "compression_fail"
VERDICT_SILENT = "silent"

ORIGIN_SPINE = "spine"
ORIGIN_SPLIT = "split"
ORIGIN_MERGE = "merge"
ORIGIN_ORPHAN = "orphan"


@dataclass(frozen=True, slots=True)
class Word:
    """whisper `words[]` 항목. 어절 소유 판정에만 쓴다 — 큐 시각을 정하지 않는다."""

    text: str
    start: float
    end: float

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> Word:
        return Word(
            text=str(raw.get("word", raw.get("text", ""))),
            start=float(raw.get("start", 0.0)),
            end=float(raw.get("end", 0.0)),
        )


@dataclass(frozen=True, slots=True)
class Segment:
    """골격 세그먼트. whisper-1 원본에서 유래하며 이후 절대 수정되지 않는다."""

    id: int
    start: float
    end: float
    text: str
    avg_logprob: float = 0.0
    compression_ratio: float = 1.0
    no_speech_prob: float = 0.0
    verdict: str = VERDICT_OK

    @property
    def duration(self) -> float:
        return self.end - self.start

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> Segment:
        return Segment(
            id=int(raw["id"]),
            start=float(raw["start"]),
            end=float(raw["end"]),
            text=str(raw["text"]),
            avg_logprob=float(raw.get("avg_logprob", 0.0)),
            compression_ratio=float(raw.get("compression_ratio", 1.0)),
            no_speech_prob=float(raw.get("no_speech_prob", 0.0)),
            verdict=str(raw.get("verdict", VERDICT_OK)),
        )


@dataclass(frozen=True, slots=True)
class Cue:
    """자막 큐 하나. `lines`가 출력 형태, `text`는 한 줄로 이은 형태다."""

    start: float
    end: float
    lines: tuple[str, ...]
    seg_ids: tuple[int, ...] = ()
    origin: str = ORIGIN_SPINE
    conf: float = 1.0
    flags: tuple[str, ...] = ()
    whisper_text: str = ""
    index: int = 0

    @property
    def text(self) -> str:
        return " ".join(line for line in self.lines if line)

    @property
    def duration(self) -> float:
        return self.end - self.start

    @property
    def cps(self) -> float:
        """초당 글자 수. duration이 0 이하면 무한대로 취급해 항상 위반으로 잡힌다."""
        if self.duration <= 0:
            return float("inf")
        return len(self.text) / self.duration

    def with_text(self, text: str) -> Cue:
        return replace(self, lines=(text,))

    def with_flag(self, flag: str) -> Cue:
        if flag in self.flags:
            return self
        return replace(self, flags=(*self.flags, flag))

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> Cue:
        return Cue(
            start=float(raw["start"]),
            end=float(raw["end"]),
            lines=tuple(raw["lines"]),
            seg_ids=tuple(int(i) for i in raw.get("seg_ids", ())),
            origin=str(raw.get("origin", ORIGIN_SPINE)),
            conf=float(raw.get("conf", 1.0)),
            flags=tuple(raw.get("flags", ())),
            whisper_text=str(raw.get("whisper_text", "")),
            index=int(raw.get("index", 0)),
        )


@dataclass(frozen=True, slots=True)
class Chunk:
    """분할된 오디오 조각. `offset`은 원본 오디오 기준 시작 시각(초)."""

    path: Path
    offset: float
    duration: float
    index: int


@dataclass(frozen=True, slots=True)
class Issue:
    """정제 결과 이상치. level은 ERR / WARN / INFO."""

    level: str
    code: str
    cue_id: int
    message: str


@dataclass(frozen=True, slots=True)
class Trace:
    """파이프라인 전 과정의 스냅샷. `redo`는 이 파일만 읽고 API를 호출하지 않는다."""

    source: str
    duration: float
    spine: tuple[Segment, ...]
    cues: tuple[Cue, ...] = ()
    words: tuple[Word, ...] = ()
    silences: tuple[tuple[float, float], ...] = ()
    canonical: str = ""
    config_snapshot: dict[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    version: str = TRACE_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "source": self.source,
            "duration": self.duration,
            "spine": [asdict(seg) for seg in self.spine],
            "cues": [asdict(cue) for cue in self.cues],
            "words": [asdict(word) for word in self.words],
            "silences": [list(span) for span in self.silences],
            "canonical": self.canonical,
            "config_snapshot": self.config_snapshot,
            "warnings": list(self.warnings),
        }

    def save(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return path

    @staticmethod
    def load(path: Path) -> Trace:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise RuntimeError(f"trace를 읽지 못했습니다: {path} ({exc})") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"trace가 올바른 JSON이 아닙니다: {path} ({exc})") from exc
        return Trace.from_dict(raw)

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> Trace:
        version = str(raw.get("version", ""))
        if version != TRACE_VERSION:
            raise RuntimeError(f"trace 버전이 맞지 않습니다: {version!r} (기대: {TRACE_VERSION!r})")
        return Trace(
            source=str(raw.get("source", "")),
            duration=float(raw.get("duration", 0.0)),
            spine=tuple(Segment.from_dict(s) for s in raw.get("spine", ())),
            cues=tuple(Cue.from_dict(c) for c in raw.get("cues", ())),
            words=tuple(Word.from_dict(w) for w in raw.get("words", ())),
            silences=tuple((float(a), float(b)) for a, b in raw.get("silences", ())),
            canonical=str(raw.get("canonical", "")),
            config_snapshot=dict(raw.get("config_snapshot", {})),
            warnings=tuple(raw.get("warnings", ())),
            version=version,
        )
