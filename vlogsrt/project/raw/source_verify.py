"""원본 재검증의 stat 믿기(D62) — 지난 완전 검증 때와 stat이 같으면 기록된 SHA-256을 믿는다.

작업대에서 영상 카드를 누르거나 「다음」으로 다른 영상에 갈 때마다 캐시 재사용 검사
(`selection_analysis.cached_revision`)와 미리보기 준비(`visual_review._select_source`)가 원본
전체를 다시 해시했다 — 9.3GB 0018이면 한 번에 1–2분이고, 그동안 「옮기는 중」이 멈춰 보였다.

**믿는 조건**: 지금 경로의 (크기, mtime_ns, inode)가 지난 완전 검증 때 잰 값과 같다. 그 값은 둘 중
하나에서 온다 — (1) 이 모듈이 완전 해시가 기록과 맞았을 때 남긴 메모
(`cache/source-verify/<asset_id>.json`, 경로·stat·해시), (2) 참조 원본이면 가져오기의
`source_assets` 행(크기·`source_mtime_ns`·`source_inode`) — 참조 가져오기는 해시한 뒤 stat이
그대로인지 다시 확인하고서야 해시를 기록한다. 복사본은 행의 stat이 카드 쪽 원본의 것이라 메모만
믿는다. 하나라도 다르면 예전과 똑같이 완전 해시이고, 다르면 호출자가 예전 문장 그대로 거부한다.

**대가**: 크기·mtime·inode를 그대로 둔 채 내용만 바꾸는 쓰기(mtime을 되돌리는 도구)는 못 잡는다 —
rsync·git 색인과 같은 선택이다. 그래서 이 믿기는 **재사용·표시·승인 견적**(캐시 검사·미리보기·
썸네일·`selection_analysis.quote`, A2)에만 쓰고, 새 기록·유료 작업을 만드는 검증(1단계 승인 만들기·
실행 문 `start_analysis`·분석 본체·계획·추출)은 예전대로 완전 해시한다. 견적의 digest는
stat이 아니라 기록된 해시를 묶으므로, 위조된 stat은 견적을 지나도 유료 요청 앞의 완전 해시에서
닫힌다.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from pathlib import Path
from typing import Any

from vlogsrt import log

__all__ = ["MEMO_DIR", "source_digest"]

MEMO_DIR = "cache/source-verify"
"""완전 검증 메모의 자리 — 작업공간 `cache/` 아래, 브라우저 제공 대상이 아니다."""

_MEMO_SCHEMA = "source-verify/v1"
_HASH_BLOCK_BYTES = 1 << 20


def source_digest(workspace: Any, asset: Any, path: Path, *, trust_stat: bool) -> str:
    """이 경로의 내용 해시 — `trust_stat`이고 stat이 지난 완전 검증과 같으면 기록값이다.

    완전 해시가 기록(`asset.content_sha256`)과 맞으면 그때의 stat을 메모로 남긴다(다음 번의 믿기
    재료). 맞지 않으면 메모를 쓰지 않고 잰 값을 돌려준다 — 거부 문장은 호출자의 것이다.
    """
    recorded = getattr(asset, "content_sha256", None)
    probed = path.stat()
    if trust_stat and isinstance(recorded, str) and _stat_trusted(workspace, asset, path, probed):
        return recorded
    digest = _full_sha256(path)
    if digest == recorded:
        _remember(workspace, asset, path, probed, digest)
    return digest


def _full_sha256(path: Path) -> str:
    """원본 전체의 SHA-256 — 시험이 이 함수를 엿봐 해시를 했는지 센다."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(_HASH_BLOCK_BYTES), b""):
            digest.update(block)
    return digest.hexdigest()


def _stamp(probed: os.stat_result) -> tuple[int, int, int]:
    return (probed.st_size, probed.st_mtime_ns, probed.st_ino)


def _stat_trusted(workspace: Any, asset: Any, path: Path, probed: os.stat_result) -> bool:
    memo = _read_memo(workspace, asset)
    if (
        memo is not None
        and memo.get("path") == str(path)
        and memo.get("content_sha256") == asset.content_sha256
        and (memo.get("size_bytes"), memo.get("mtime_ns"), memo.get("inode")) == _stamp(probed)
    ):
        return True
    return _reference_row_trusted(workspace, asset, path, probed)


def _reference_row_trusted(workspace: Any, asset: Any, path: Path, probed: os.stat_result) -> bool:
    """참조 원본이고 가져오기 행의 stat 그대로인가 — 복사본의 행 stat은 카드 쪽 것이라 안 본다."""
    if getattr(asset, "status", None) != "ready":
        return False
    referenced = workspace.source_reference_path(asset.asset_id)
    expected = str(Path(asset.source_root) / asset.source_relative_path)
    if referenced is None or referenced != expected or str(path) != expected:
        return False
    return (asset.size_bytes, asset.source_mtime_ns, asset.source_inode) == _stamp(probed)


def _memo_path(workspace: Any, asset: Any) -> Path:
    return Path(workspace.root) / MEMO_DIR / f"{asset.asset_id}.json"


def _read_memo(workspace: Any, asset: Any) -> dict[str, object] | None:
    target = _memo_path(workspace, asset)
    try:
        body = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        log.warn(f"원본 검증 메모를 읽지 못해 완전 해시합니다: {target.name} ({error})")
        return None
    if not isinstance(body, dict) or body.get("schema") != _MEMO_SCHEMA:
        return None
    return body


def _remember(workspace: Any, asset: Any, path: Path, probed: os.stat_result, digest: str) -> None:
    """완전 검증의 stat을 남긴다 — 못 남겨도 경고뿐이다(다음 번이 다시 해시할 뿐)."""
    target = _memo_path(workspace, asset)
    size, mtime_ns, inode = _stamp(probed)
    body = {
        "schema": _MEMO_SCHEMA,
        "asset_id": asset.asset_id,
        "path": str(path),
        "size_bytes": size,
        "mtime_ns": mtime_ns,
        "inode": inode,
        "content_sha256": digest,
    }
    temp = target.with_name(f".{target.name}.{os.getpid()}.{secrets.token_hex(4)}.partial")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temp.write_text(json.dumps(body, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temp, target)
    except OSError as error:
        temp.unlink(missing_ok=True)
        log.warn(f"원본 검증 메모를 남기지 못했습니다(다음에 다시 해시): {target.name} ({error})")
