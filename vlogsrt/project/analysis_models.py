"""P5 두 갈래(VAD·비전)가 공유하는 결과/오류 어휘 — 이 파일 하나뿐이다.

P5-00 검토로 얼어 있다. 분석 결과의 상태(`AnalysisStatus`)와 실패 기록
(`AnalysisFailure`)만 여기 산다. VAD 고유 레코드는 `vlogsrt/project/vad/`,
비전 고유 레코드는 `vlogsrt/project/vision/`의 소유다 — 두 갈래 어느 쪽도 이
공유 파일을 고치지 않는다. 이 모듈은 project 안 그 무엇도 import하지 않는다:
공유 어휘가 특정 갈래를 알면 반대 갈래가 그 무게를 물려받는다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

AnalysisStatus = Literal["success", "empty", "error"]


@dataclass(frozen=True, slots=True)
class AnalysisFailure:
    """분석 한 번의 실패 — 무엇이(kind)·왜(message)·다시 시도할 수 있나(retryable).

    `kind`는 갈래가 정하는 짧은 분류 코드다(예: `"vad_runtime"`). `message`는
    사람이 읽는 설명이고 로그·`trace`에 그대로 실린다. `retryable`은 다시 시도할
    값어치가 있는가 — 값이 `False`면 재시도 없이 결과를 비운다.
    """

    kind: str
    message: str
    retryable: bool
