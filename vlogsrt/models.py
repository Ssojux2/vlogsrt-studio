"""데이터 모델. 전부 frozen이다 — spine은 생성 후 수정되지 않는다(불변 규칙 2)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from . import TRACE_VERSION

ORIGIN_SPINE = "spine"
ORIGIN_SPLIT = "split"
ORIGIN_MERGE = "merge"

# 시간 구간 `(start, end)`. 초 단위.
Span = tuple[float, float]


@dataclass(frozen=True, slots=True)
class Segment:
    """골격 세그먼트. 발화 구간 하나 + 거기 배분된 글자이며 이후 수정되지 않는다.

    `id`는 우리가 붙인 통번호다. 모델은 세그먼트라는 개념 자체를 주지 않는다 —
    이 세그먼트는 발화 구간에 글자를 배분해 우리가 만든 것이다(D24).
    """

    id: int
    start: float
    end: float
    text: str

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
        )


@dataclass(frozen=True, slots=True)
class Cue:
    """자막 큐 하나. `lines`가 출력 형태, `text`는 한 줄로 이은 형태다."""

    start: float
    end: float
    lines: tuple[str, ...]
    seg_ids: tuple[int, ...] = ()
    origin: str = ORIGIN_SPINE
    flags: tuple[str, ...] = ()
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
            flags=tuple(raw.get("flags", ())),
            index=int(raw.get("index", 0)),
        )


@dataclass(frozen=True, slots=True)
class Chunk:
    """전사할 오디오 조각 하나.

    `offset`은 **ffmpeg가 실제로 잘렸다고 알려준** 원본 오디오 기준 시작 시각이다.
    세그먼트 시각에 이 값을 더한 것이 자막 시각 전부다(규칙 1).
    """

    path: Path
    offset: float
    duration: float
    index: int


@dataclass(frozen=True, slots=True)
class Trace:
    """파이프라인 전 과정의 스냅샷. `redo`는 이 파일만 읽고 API를 호출하지 않는다."""

    source: str
    duration: float
    spine: tuple[Segment, ...]
    cues: tuple[Cue, ...] = ()
    config_snapshot: dict[str, Any] = field(default_factory=dict)
    version: str = TRACE_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "source": self.source,
            "duration": self.duration,
            "spine": [asdict(seg) for seg in self.spine],
            "cues": [asdict(cue) for cue in self.cues],
            "config_snapshot": self.config_snapshot,
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
            config_snapshot=dict(raw.get("config_snapshot", {})),
            version=version,
        )
