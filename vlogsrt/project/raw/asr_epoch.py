"""원본마다의 「다시 듣기」 차례(asr epoch, D59 19b) — 0이면 어느 열쇠에도 들어가지 않는다.

기존 원본 전사는 온도 0 버그(D55)로 산 것이다. whisper 청크 캐시 열쇠에는 온도가 없고(설정 보기만
묶는다) 끝난 분석은 지문이 같으면 다시 쓰이므로, 그냥 다시 돌리면 아무것도 다시 사지 않는다. 사람이
영상 하나를 「다시 듣기」로 고르면 그 원본의 차례가 하나 오르고, 그 차례가 청크 열쇠·원본 입력 지문·
분석 결속·견적 digest에 실려 **그 원본의 청크만** 새로 산다(승인 뒤에만).

차례 0은 어디에도 실리지 않는다 — 이미 산 전사·끝난 분석·승인 지문이 바이트까지 그대로다. 표는 처음
올릴 때 만들어지고, 읽기는 표를 만들지 않는다(읽기만 하는 작업공간을 건드리지 않는다).

`heard`는 그 차례로 전사가 끝까지(실패 청크 없이)돈 차례다. `epoch > heard`이면 다시 듣기를 기다리는
중이다 — 그동안 다시 누르면 차례를 또 올리지 않는다.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from vlogsrt.project.raw import read_scope
from vlogsrt.project.raw.models import RawWorkspaceError

TABLE = "source_asr_epochs"
TAG_PREFIX = "asr-epoch/"
"""결속·입력 지문에 실리는 꼬리표의 머리 — `asr-epoch/1`."""


@dataclass(frozen=True, slots=True)
class AsrEpoch:
    """원본 하나의 차례 — 지금 차례와 끝까지 들은 차례."""

    epoch: int = 0
    heard: int = 0

    @property
    def pending(self) -> bool:
        """다시 듣기를 골랐고 아직 그 차례로 끝까지 듣지 않았다."""
        return self.epoch > self.heard


def binding_tag(epoch: int) -> str | None:
    """결속·입력 지문에 덧붙일 꼬리표 — 0이면 None(덧붙이지 않는다, 바이트 그대로)."""
    _require_epoch(epoch)
    return None if epoch == 0 else f"{TAG_PREFIX}{epoch}"


def state(workspace: Any, asset_id: str) -> AsrEpoch:
    """원본의 차례 — 표나 줄이 없으면 0/0이다. 표를 만들지 않는다.

    읽기 범위(그리기 한 번) 안에서는 표를 한 번에 읽는다(D63)."""
    scope = read_scope.current()
    if scope is not None:
        epochs = scope.memo(("asr_epoch.states", str(workspace.root)), lambda: _states(workspace))
        return epochs.get(asset_id, AsrEpoch())
    with workspace.connect() as db:
        if not _table_exists(db):
            return AsrEpoch()
        row = db.execute(
            f"SELECT epoch, heard FROM {TABLE} WHERE asset_id=?", (asset_id,)
        ).fetchone()
    return AsrEpoch() if row is None else AsrEpoch(int(row[0]), int(row[1]))


def _states(workspace: Any) -> dict[str, AsrEpoch]:
    with workspace.connect() as db:
        if not _table_exists(db):
            return {}
        rows = db.execute(f"SELECT asset_id, epoch, heard FROM {TABLE}").fetchall()
    return {str(row[0]): AsrEpoch(int(row[1]), int(row[2])) for row in rows}


def current(workspace: Any, asset_id: str) -> int:
    return state(workspace, asset_id).epoch


def bump(workspace: Any, asset_id: str, *, guard: Callable[[Any], None] | None = None) -> AsrEpoch:
    """차례를 하나 올린다 — 이미 기다리는 중이면 그대로 돌려준다(두 번 눌러도 한 번이다).

    `guard`는 쓰기 트랜잭션(`BEGIN IMMEDIATE`) 안에서 같은 연결로 돈다 — 부른 쪽이 그 검사와 이
    쓰기 사이에 다른 쓰기(원본 자막의 시각 맞춤 적용 등)가 끼지 못하게 한다(D59 19h). 거절하려면
    `RawWorkspaceError`를 올리고, 그러면 아무것도 쓰지 않는다.
    """
    with workspace.connect() as db:
        _create(db)
        db.execute("BEGIN IMMEDIATE")
        if guard is not None:
            guard(db)
        row = db.execute(
            f"SELECT epoch, heard FROM {TABLE} WHERE asset_id=?", (asset_id,)
        ).fetchone()
        now = AsrEpoch() if row is None else AsrEpoch(int(row[0]), int(row[1]))
        if not now.pending:
            now = AsrEpoch(now.epoch + 1, now.heard)
            db.execute(
                f"INSERT INTO {TABLE}(asset_id, epoch, heard) VALUES(?,?,?) "
                "ON CONFLICT(asset_id) DO UPDATE SET epoch=excluded.epoch",
                (asset_id, now.epoch, now.heard),
            )
        db.commit()
    return now


def mark_heard(workspace: Any, asset_id: str, epoch: int) -> None:
    """그 차례로 끝까지 들었다 — 0은 기록하지 않는다(표를 만들지 않는다)."""
    _require_epoch(epoch)
    if epoch == 0:
        return
    with workspace.connect() as db:
        if not _table_exists(db):
            return
        db.execute(
            f"UPDATE {TABLE} SET heard=? WHERE asset_id=? AND epoch=? AND heard<?",
            (epoch, asset_id, epoch, epoch),
        )
        db.commit()


def pending_on(db: Any, asset_id: str) -> bool:
    """연결 하나 위에서 읽은 「다시 듣기 대기」 — 다른 쓰기 트랜잭션 안의 검사가 같은 연결로 읽는다.

    표를 만들지 않는다(읽기만).
    """
    if not _table_exists(db):
        return False
    row = db.execute(f"SELECT epoch, heard FROM {TABLE} WHERE asset_id=?", (asset_id,)).fetchone()
    return row is not None and int(row[0]) > int(row[1])


def withdraw(workspace: Any, asset_id: str) -> bool:
    """기다리던 다시 듣기를 거둔다(차례를 들은 차례로 되돌린다) — 거뒀으면 True.

    사람이 그 원본의 자막을 고치면 부른다: 다시 들은 전사는 고친 자막을 덮지 않으므로(seed 규칙)
    돈만 들고 화면에 남지 않는다.
    """
    with workspace.connect() as db:
        if not _table_exists(db):
            return False
        changed = db.execute(
            f"UPDATE {TABLE} SET epoch=heard WHERE asset_id=? AND epoch>heard", (asset_id,)
        ).rowcount
        db.commit()
    return changed > 0


def _require_epoch(epoch: object) -> None:
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 0:
        raise RawWorkspaceError(f"다시 듣기 차례는 0 이상의 정수입니다: {epoch!r}")


def _table_exists(db: Any) -> bool:
    found = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)
    ).fetchone()
    return found is not None


def _create(db: Any) -> None:
    db.execute(
        f"CREATE TABLE IF NOT EXISTS {TABLE} (asset_id TEXT PRIMARY KEY, "
        "epoch INTEGER NOT NULL, heard INTEGER NOT NULL)"
    )


__all__ = [
    "TABLE",
    "AsrEpoch",
    "binding_tag",
    "bump",
    "current",
    "mark_heard",
    "pending_on",
    "state",
    "withdraw",
]
