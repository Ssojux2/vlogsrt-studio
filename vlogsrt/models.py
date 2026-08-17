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

# 되풀이를 무엇이 짚었는가
WHY_ECHO = "echo"  # 연속 세그먼트가 글자 그대로 같다
WHY_COMPRESSION = "compression"  # whisper 자신의 compression_ratio가 임계값을 넘었다

# 되풀이 구간을 어떻게 처리했는가
REPAIR_REPLACED = "replaced"  # 재전사가 성공해 그 구간을 갈아끼웠다
REPAIR_COLLAPSED = "collapsed"  # 회복 못 했다. 첫 장만 남기고 나머지는 비웠다
REPAIR_KEPT = "kept"  # 손대지 않았다

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
    # whisper가 세그먼트마다 실어 보내는 신뢰도 메타. 우리가 계산하지 않는다 —
    # 응답에 있던 값을 그대로 옮길 뿐이다. `compression_ratio`가 되풀이를 짚는다(D27).
    compression_ratio: float = 0.0
    avg_logprob: float = 0.0
    no_speech_prob: float = 0.0

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
            compression_ratio=float(raw.get("compression_ratio", 0.0)),
            avg_logprob=float(raw.get("avg_logprob", 0.0)),
            no_speech_prob=float(raw.get("no_speech_prob", 0.0)),
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
class Repeat:
    """같은 말이 되풀이된 구간. **자리를 짚을 뿐 아직 아무것도 바꾸지 않았다.**"""

    start: float
    end: float
    count: int
    text: str
    reason: str
    seg_ids: tuple[int, ...] = ()

    @property
    def duration(self) -> float:
        return self.end - self.start

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> Repeat:
        return Repeat(
            start=float(raw["start"]),
            end=float(raw["end"]),
            count=int(raw["count"]),
            text=str(raw["text"]),
            reason=str(raw.get("reason", WHY_ECHO)),
            seg_ids=tuple(int(i) for i in raw.get("seg_ids", ())),
        )


@dataclass(frozen=True, slots=True)
class Repair:
    """되풀이 구간 하나를 어떻게 했는지의 기록. 사람이 나중에 따질 수 있어야 한다."""

    repeat: Repeat
    outcome: str
    arbiter_clean: bool  # 심판 모델이 "이 구간은 되풀이가 아니다"라고 했는가
    attempts: int
    before: int  # 걷어낸 세그먼트 수
    after: int  # 대체 뒤 세그먼트 수
    # **다시 물어본 구간.** 되풀이 자체보다 넓다 — 앞뒤 여유에 걸친 세그먼트를 통째로
    # 데려가기 때문이다. 사람이 "어디까지 다시 물었나"를 따질 수 있어야 한다.
    span: Span = (0.0, 0.0)
    # 심판이 답을 하기는 했는가. **`arbiter_clean=False`만으로는 "되풀이가 맞다"와
    # "심판을 못 불렀다"를 구별할 수 없다.** 사람이 무엇을 고쳐야 할지가 달라진다.
    arbiter_answered: bool = False
    # **갈아끼운 뒤에도 남은 되풀이 초.** 합격이 곧 깨끗함은 아니다 — 예전에는 이 값이
    # 없어서 8구간 전부 "회복"으로 찍히는 동안 114초가 SRT로 나갔다(D28).
    residual_seconds: float = 0.0

    @property
    def verdict(self) -> str:
        if not self.arbiter_answered:
            return "심판 판정 없음"
        return "심판: 말이 있다" if self.arbiter_clean else "심판: 되풀이가 맞다"

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> Repair:
        return Repair(
            repeat=Repeat.from_dict(raw["repeat"]),
            outcome=str(raw["outcome"]),
            arbiter_clean=bool(raw.get("arbiter_clean", False)),
            attempts=int(raw.get("attempts", 0)),
            before=int(raw.get("before", 0)),
            after=int(raw.get("after", 0)),
            arbiter_answered=bool(raw.get("arbiter_answered", False)),
            span=(float(raw.get("span", (0.0, 0.0))[0]), float(raw.get("span", (0.0, 0.0))[1])),
            residual_seconds=float(raw.get("residual_seconds", 0.0)),
        )


@dataclass(frozen=True, slots=True)
class Trace:
    """파이프라인 전 과정의 스냅샷. `redo`는 이 파일만 읽고 API를 호출하지 않는다."""

    source: str
    duration: float
    spine: tuple[Segment, ...]
    cues: tuple[Cue, ...] = ()
    config_snapshot: dict[str, Any] = field(default_factory=dict)
    repairs: tuple[Repair, ...] = ()
    version: str = TRACE_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "source": self.source,
            "duration": self.duration,
            "spine": [asdict(seg) for seg in self.spine],
            "cues": [asdict(cue) for cue in self.cues],
            "config_snapshot": self.config_snapshot,
            "repairs": [asdict(repair) for repair in self.repairs],
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
            repairs=tuple(Repair.from_dict(r) for r in raw.get("repairs", ())),
            version=version,
        )
