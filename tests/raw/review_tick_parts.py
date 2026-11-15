"""화면 틱 하나(W-81, D141)에 실린 처리기의 입출력 몫 — 배선 시험이 처리기마다 칸을 찾는다.

옛 배선은 처리기마다 타이머 의존이 따로라 시험이 `app.fns`에서 처리기 이름으로 의존을 찾았다.
이제 의존은 `review_tick` 하나이고 입출력은 처리기 차례대로 이어 붙어 있다 — 이 도우미가 그
몫을 잘라 준다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ui.workspace_service import RawWorkspaceUi


@dataclass(frozen=True)
class PartSlice:
    """한 처리기의 몫 — 틱 의존(설정 사전)과 그 처리기 몫의 입력·출력 칸 아이디."""

    dep: dict[str, Any]
    inputs: list[int]
    outputs: list[int]


def review_tick_dep(app: Any, ui: RawWorkspaceUi) -> dict[str, Any]:
    """화면 틱 의존 하나 — 처리기가 `review_tick`인 의존이 꼭 하나다."""
    deps = app.config["dependencies"]
    (dep,) = [d for d in deps if app.fns[d["id"]].fn == ui.review_tick]
    return dep


def tick_part(app: Any, ui: RawWorkspaceUi, handler: Any) -> PartSlice:
    """처리기 하나의 몫 — 실린 차례대로 입출력을 잘라 의존의 칸과 같은지 확인한다."""
    dep = review_tick_dep(app, ui)
    at_in = at_out = 0
    found: PartSlice | None = None
    for part in ui._tick_parts:
        inputs = dep["inputs"][at_in : at_in + len(part.inputs)]
        outputs = dep["outputs"][at_out : at_out + len(part.outputs)]
        assert inputs == [block._id for block in part.inputs]
        assert outputs == [block._id for block in part.outputs]
        if part.handler == handler:
            assert found is None, "같은 처리기가 두 번 실렸다"
            found = PartSlice(dep, inputs, outputs)
        at_in, at_out = at_in + len(part.inputs), at_out + len(part.outputs)
    assert (at_in, at_out) == (len(dep["inputs"]), len(dep["outputs"]))
    assert found is not None, f"{getattr(handler, '__name__', handler)}가 화면 틱에 없다"
    return found
