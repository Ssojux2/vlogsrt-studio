"""FCPXML **전송 모델 전용** 네임스페이스.

이 자리는 P1의 검토된 FCPXML 파서 모델을 위한 것이다 — 계약(§3)이 말하는
불변 전송 구조만 여기 산다. 분석 모델은 P5가 별도의 `analysis_models.py`에서
소유한다. 여기 들어오는 모든 데이터클래스는 `frozen=True`여야 한다 —
`scripts/check_invariants.py`의 파생 frozen 검사가 가변 선언을 거부한다
(docs/PROJECT_CONTRACTS.md §8). 시각은 전부 `Fraction`이고, 원본 XML 바이트는
`ProjectDocument.raw_xml`이 통째로 든다 — 파싱이 원본 트리를 정규화하는 일은
없다(P1-02).

신원의 정의(계획 "Source analysis, clocks, identity"):

- `scoped_id = sha256(input_sha256 + ":resource:" + xml_id)` — XML 안의 `r2` 같은
  낡은 id는 **이 문서 안에서만** 뜻이 있다. 문서 해시가 스코프다.
- `instance_id = sha256(input_sha256 + ":instance:" + element_path)` — 같은 애셋을
  여러 번 쓰면 리소스는 하나, 인스턴스는 매번 다르다.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Literal


@dataclass(frozen=True, slots=True)
class MediaRep:
    """애셋의 미디어 표현 한 가지 — 원본 또는 프록시."""

    kind: Literal["original", "proxy"]
    src: str


@dataclass(frozen=True, slots=True)
class AssetResource:
    """`<resources>/<asset>` 하나 — 소스 미디어의 정적 사실."""

    xml_id: str
    scoped_id: str
    uid: str
    name: str
    format_id: str
    frame_duration: Fraction
    start: Fraction
    duration: Fraction
    has_audio: bool
    media_reps: tuple[MediaRep, ...]


@dataclass(frozen=True, slots=True)
class ClipInstance:
    """스토리라인의 `asset-clip` 한 번 — 시각은 전부 원본 XML 그대로."""

    instance_id: str
    element_path: str
    resource_scoped_id: str
    offset: Fraction
    start: Fraction
    duration: Fraction
    enabled: bool
    src_enable: Literal["all", "video"]


@dataclass(frozen=True, slots=True)
class GapInstance:
    """스토리라인의 `gap` 한 번 — 소스가 없는 시간 조각."""

    instance_id: str
    element_path: str
    offset: Fraction
    duration: Fraction


TimelineItem = ClipInstance | GapInstance
"""스토리라인 항목 — 자막 투영은 이 순서를 그대로 따른다."""


@dataclass(frozen=True, slots=True)
class ProjectDocument:
    """파싱이 끝난 입력 프로젝트 — 여기서부터 문서는 불변 증거다."""

    input_path: Path
    info_path: Path
    input_sha256: str
    raw_xml: bytes
    version: str
    sequence_tc_start: Fraction
    frame_duration: Fraction
    resources: tuple[AssetResource, ...]
    storyline: tuple[TimelineItem, ...]


class UnsupportedProject(RuntimeError):
    """지정 부분집합 밖의 입력 — 어디서(location) 왜(reason) 거부했는지 말한다.

    조용히 눌러 펴지는(flaten) 일이 없어야 한다(계획 "Input and output").
    `location`은 `/fcpxml[1]/library[1]/…/asset-clip[3]` 같은 1기반 위치 경로나
    입력 파일 경로다.
    """

    def __init__(self, location: str, reason: str) -> None:
        super().__init__(f"{location}: {reason}")
        self.location = location
        self.reason = reason
