"""원본 순서 — 자연스러운 파일 번호 정렬과 제안·확정 계약(계획 §6.2).

여기는 순수 계산만 한다: 파일시스템·DB를 건드리지 않는다. 순서의 정본은
`workspace.py`의 `SourceOrderRevision`(불변)이고, 이 모듈은 그 **초안**을
만드는 규칙이다. 원본 파일 내부의 순서는 다루지 않는다 — 그건 시간순 분할
(A3)의 것이고, 이 계층은 파일 사이의 순서만 정한다.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from vlogsrt.project.raw.models import (
    ORDER_BASIS_USER_CONFIRMED,
    SourceAsset,
    SourceOrderEntry,
    SourceOrderRevision,
)

DIGIT_RUN = re.compile(r"(\d+)")
"""연속한 숫자 뭉치 — 자연 정렬의 단위다. `DJI_0002`가 `DJI_0010`보다 앞에 선다."""

CAPTURE_BASIS_UNKNOWN = "capture_time:uninspected"
"""A2의 촬영 시각 근거 — 아직 조사하지 않았다(빈값이 아니다, 미조사다)."""

CAPTURE_BASIS_KNOWN = "capture_time"
"""신뢰 근거가 생긴 뒤의 표기 — 루트가 더 풍부한 메타데이터를 묶은 뒤에 쓴다."""


def natural_key(text: str) -> tuple[tuple[int, int, str], ...]:
    """자연 정렬 키 — 숫자 뭉치는 값으로, 글자 뭉치는 casefold로 비교한다.

    카메라의 연속 파일명(`DJI_0002.MP4` … `DJI_0010.MP4`)은 사전식으로는
    `0010`이 `0002`보다 앞에 온다. 숫자 뭉치를 정수로 비교해 촬영 순서를
    지키는 것이 이 함수 하나의 이유다(§6.2 tie-breaker).
    """
    key: list[tuple[int, int, str]] = []
    for chunk in DIGIT_RUN.split(text.casefold()):
        if not chunk:
            continue
        if chunk.isdigit():
            key.append((0, int(chunk), ""))
        else:
            key.append((1, 0, chunk))
    return tuple(key)


def natural_path_key(relative_path: str) -> tuple[tuple[tuple[int, int, str], ...], ...]:
    """경로 전체의 자연 키 — 칸별로 나눠 비교한다(깊은 경로가 얕은 뒤에)."""
    return tuple(natural_key(part) for part in relative_path.split("/"))


def capture_rank(capture_time: str | None) -> tuple[int, str]:
    """촬영 시각 정렬 순위 — 알면 앞(0, 시각), 모르면 뒤(1)에 자연 정렬로 간다.

    촬영 시각을 모르는 항목을 시각 아닌 값(mtime 같은)으로 대체하지 않는다 —
    모르는 항목은 **모른 채로** 표시되고 자연 번호 정렬이 그 자리를 맡는다.
    """
    if capture_time is None:
        return (1, "")
    return (0, capture_time)


def capture_basis_note(capture_time: str | None) -> str:
    """순서 항목의 근거 문장 — 미조사 항목이 조용히 넘어가지 않게 한다."""
    if capture_time is None:
        return CAPTURE_BASIS_UNKNOWN
    return f"{CAPTURE_BASIS_KNOWN}:{capture_time}"


def order_key(capture_time: str | None, relative_path: str) -> tuple[object, ...]:
    """정렬 키 — 촬영 시각(아는 것만) → 자연 번호 경로(§6.2).

    후보(스캔)와 자산(확정)이 **같은 규칙**을 지나가게 키 만들기만 여기 산다.
    """
    return (capture_rank(capture_time), natural_path_key(relative_path))


def propose_order(candidates: Sequence[SourceAsset]) -> tuple[SourceAsset, ...]:
    """제안 순서 — 촬영 시각(아는 것만) → 자연 번호 → 상대 경로(§6.2).

    이것은 어디까지나 **제안**이다. 확정은 사람의 명시적 확인이 만드는
    `SourceOrderRevision`이고, 이 함수는 그 입력을 정렬할 뿐이다. 후보 사이의
    순서를 자동으로 다시 배열하는 확정 경로는 없다.
    """
    return tuple(
        sorted(
            candidates,
            key=lambda asset: order_key(asset.capture_time, asset.source_relative_path),
        )
    )


def propose_global_order(assets: Sequence[SourceAsset]) -> tuple[SourceAsset, ...]:
    """배치를 넘는 제안 순서 — 촬영 시각 오래된 것부터, 모르면 맨 뒤에 자연 경로 순(D53).

    "영상은 언제나 만든 시각 순"이 사용자 지시다(2026-10-09) — 가져온 배치 순서를
    앞세우던 §6.5의 배치 사이 규칙을 작업대에서는 이것이 대신한다. 먼저 찍은 영상을
    나중에 가져와도 맨 앞에 선다. 같은 시각·같은 경로는 배치 자연 순 → 확정 순번 →
    자산 ID로 갈라 언제나 같은 답을 낸다. 확정은 여전히 새 불변 리비전이다.
    """
    return tuple(
        sorted(
            assets,
            key=lambda asset: (
                *order_key(asset.capture_time, asset.source_relative_path),
                natural_key(asset.batch_dir),
                asset.ordinal,
                asset.asset_id,
            ),
        )
    )


def revision_entries(ordered: Sequence[SourceAsset]) -> tuple[SourceOrderEntry, ...]:
    """정렬된 자산에 확정 순번을 붙인다 — 순번은 1부터, 제안 그대로다."""
    return tuple(
        SourceOrderEntry(
            asset_id=asset.asset_id,
            ordinal=index + 1,
            basis_note=capture_basis_note(asset.capture_time),
        )
        for index, asset in enumerate(ordered)
    )


def user_confirmed_revision(
    ordered: Sequence[SourceAsset],
    revision_id: str,
    batch_dir: str,
    created_at: str,
) -> SourceOrderRevision:
    """사람이 확인한 순서의 불변 리비전 — 한 번 만들어지면 고칠 수 없다."""
    return SourceOrderRevision(
        revision_id=revision_id,
        batch_dir=batch_dir,
        basis=ORDER_BASIS_USER_CONFIRMED,
        entries=revision_entries(ordered),
        created_at=created_at,
    )
