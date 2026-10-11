"""Editable, versioned source-clock captions for cut review, never final SRT."""

from __future__ import annotations

import hashlib
import json
import uuid
from fractions import Fraction
from typing import Any

from vlogsrt.project.raw.models import RawWorkspaceError


def _init(workspace: Any) -> None:
    with workspace.connect() as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS source_transcripts (asset_id TEXT NOT NULL, "
            "sequence INTEGER NOT NULL, revision TEXT NOT NULL UNIQUE, payload TEXT NOT NULL, "
            "PRIMARY KEY(asset_id, sequence))"
        )
        db.commit()


def load(workspace: Any, asset_id: str) -> dict | None:
    _init(workspace)
    with workspace.connect() as db:
        row = db.execute(
            "SELECT payload FROM source_transcripts WHERE asset_id=? "
            "ORDER BY sequence DESC LIMIT 1",
            (asset_id,),
        ).fetchone()
    return json.loads(row[0]) if row else None


def rational(value: Any) -> Fraction:
    return Fraction(value["num"], value["den"]) if isinstance(value, dict) else Fraction(str(value))


def encoded(value: Fraction) -> dict[str, int]:
    return {"num": value.numerator, "den": value.denominator}


def seed(
    workspace: Any, asset_id: str, utterances: list[dict], duration: Fraction, source_sha256: str
) -> dict:
    current = load(workspace, asset_id)
    if current is not None:
        if current["source_sha256"] != source_sha256:
            raise RawWorkspaceError("자막의 원본 신원이 다릅니다. 원본을 확인하세요")
        if current.get("edited") or current["utterances"] == utterances:
            return current
    sequence = current["sequence"] + 1 if current else 0
    payload = {
        "asset_id": asset_id,
        "revision": uuid.uuid4().hex,
        "sequence": sequence,
        "edited": False,
        "source_sha256": source_sha256,
        "duration": encoded(duration),
        "utterances": utterances,
        "dirty_ranges": [],
    }
    with workspace.connect() as db:
        db.execute(
            "INSERT OR IGNORE INTO source_transcripts VALUES(?,?,?,?)",
            (asset_id, sequence, payload["revision"], json.dumps(payload, ensure_ascii=False)),
        )
        db.commit()
    saved = load(workspace, asset_id)
    assert saved is not None
    return saved


def human_edited(document: dict) -> bool:
    """사람이 고친 원본 자막인가 — `edited` 칸이 없는 옛 문서는 둘째 리비전부터 고친 것으로 본다.

    지문과 「다시 듣기」 거절(D59 19b)이 같은 규칙을 쓴다 — 고친 자막을 덮을 수 있는 쪽으로 읽지
    않는다.
    """
    return bool(document.get("edited", document["sequence"] > 0))


def fingerprint(workspace: Any, asset_id: str) -> str:
    document = load(workspace, asset_id)
    if not document or not human_edited(document):
        return "asr-original/v1"
    return hashlib.sha256(
        json.dumps(document["utterances"], sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def edit(
    workspace: Any,
    asset_id: str,
    expected_revision: str,
    *,
    action: str,
    utterance_id: str,
    text: str | None = None,
    start: str | None = None,
    end: str | None = None,
    split_at: str | None = None,
    right_text: str | None = None,
) -> dict:
    current = load(workspace, asset_id)
    if not current or current["revision"] != expected_revision:
        raise RawWorkspaceError("원본 자막이 바뀌었습니다. 최신 자막에서 다시 수정하세요")
    rows = [dict(row) for row in current["utterances"]]
    index = next((i for i, row in enumerate(rows) if row["utterance_id"] == utterance_id), None)
    if index is None:
        raise RawWorkspaceError("현재 원본 자막을 선택하세요")
    row = rows[index]
    changed_ids = {utterance_id}
    old_start, old_end = rational(row["start"]), rational(row["end"])
    changed_start, changed_end = old_start, old_end
    if action == "edit":
        row.update(
            text=row["text"] if text is None else text,
            start=encoded(rational(start) if start is not None else old_start),
            end=encoded(rational(end) if end is not None else old_end),
        )
        row["start_sample"] = int(
            row.get("start_sample", old_start * 16000)
            + (rational(row["start"]) - old_start) * 16000
        )
        row["end_sample"] = int(
            row.get("end_sample", old_end * 16000) + (rational(row["end"]) - old_end) * 16000
        )
        changed_start = min(old_start, rational(row["start"]))
        changed_end = max(old_end, rational(row["end"]))
    elif action == "split":
        at = rational(split_at)
        if not old_start < at < old_end or not text or not right_text:
            raise RawWorkspaceError("분할 시각은 자막 안에, 양쪽 문구는 모두 입력하세요")
        right = dict(row, utterance_id="u_" + uuid.uuid4().hex, start=encoded(at), text=right_text)
        right["start_sample"] = int(
            row.get("start_sample", old_start * 16000) + (at - old_start) * 16000
        )
        row.update(end=encoded(at), text=text, end_sample=right["start_sample"])
        rows.insert(index + 1, right)
        changed_ids.add(right["utterance_id"])
    elif action == "merge":
        if index + 1 >= len(rows):
            raise RawWorkspaceError("합칠 다음 자막이 없습니다")
        right = rows.pop(index + 1)
        row.update(end=right["end"], text=text or row["text"] + " " + right["text"])
        row["end_sample"] = right.get("end_sample", int(rational(right["end"]) * 16000))
        changed_end = rational(right["end"])
    else:
        raise RawWorkspaceError("알 수 없는 자막 편집입니다")
    _validate_rows(rows, rational(current["duration"]), changed_ids)
    payload = dict(
        current,
        sequence=current["sequence"] + 1,
        edited=True,
        revision=uuid.uuid4().hex,
        utterances=rows,
        dirty_ranges=current["dirty_ranges"]
        + [{"start": encoded(changed_start), "end": encoded(changed_end)}],
    )
    with workspace.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        latest = db.execute(
            "SELECT revision FROM source_transcripts WHERE asset_id=? "
            "ORDER BY sequence DESC LIMIT 1",
            (asset_id,),
        ).fetchone()
        if not latest or latest[0] != expected_revision:
            raise RawWorkspaceError("다른 자막 편집이 먼저 저장됐습니다. 다시 확인하세요")
        db.execute(
            "INSERT INTO source_transcripts VALUES(?,?,?,?)",
            (
                asset_id,
                payload["sequence"],
                payload["revision"],
                json.dumps(payload, ensure_ascii=False),
            ),
        )
        db.commit()
    return payload


def _seconds(value: Fraction) -> str:
    """작업대 편집 칸과 같은 표기(`VL.fmt` — 소수 셋째 자리)로 초를 쓴다."""
    return f"{float(value):.3f}"


def _validate_rows(rows: list[dict], duration: Fraction, changed_ids: set[str]) -> None:
    """범위·겹침은 모든 줄에, `start < end`와 빈 문구 금지는 이번 편집이 만들거나 바꾼 줄에만.

    전사가 심은 길이 0 줄(D55 — 28.44=28.44 · 청크 끝으로 닫힌 180/180)은 글자째 남는다(규칙 9).
    그 줄 하나 때문에 다른 줄을 못 고치면 안 된다 — 손대지 않은 줄은 `start ≤ end`면 된다.
    """
    for position, cue in enumerate(rows):
        a, b = rational(cue["start"]), rational(cue["end"])
        changed = cue["utterance_id"] in changed_ids
        valid = (a < b and bool(str(cue["text"]).strip())) if changed else a <= b
        if not (a >= 0 and b <= duration and valid):
            raise RawWorkspaceError(
                f"{position + 1}번 자막({_seconds(a)}~{_seconds(b)}초)의 문구와 원본 안의"
                " 시작·끝 시각을 확인하세요"
                + (" — 바꾸는 자막은 끝이 시작보다 뒤여야 합니다" if changed and a >= b else "")
            )
        if position and a < rational(rows[position - 1]["end"]):
            raise RawWorkspaceError(f"{position + 1}번 자막 시각이 앞 자막과 겹칩니다")


def spoken(workspace: Any, asset_id: str, fallback: Any) -> tuple:
    from vlogsrt.project.raw.understanding import UtteranceEvidence

    doc = load(workspace, asset_id)
    if not doc:
        return tuple(fallback)
    return tuple(
        UtteranceEvidence(
            utterance_id=u["utterance_id"],
            start=rational(u["start"]),
            end=rational(u["end"]),
            text=u["text"],
            chunk_index=int(u.get("chunk_index", 0)),
            position=int(u.get("position", i)),
            start_sample=int(u.get("start_sample", rational(u["start"]) * 16000)),
            end_sample=int(u.get("end_sample", rational(u["end"]) * 16000)),
            evidence_id=str(u.get("evidence_id", u["utterance_id"])),
        )
        for i, u in enumerate(doc["utterances"])
    )


def cross_source_candidates(workspace: Any, asset_id: str) -> list[dict]:
    """Exact repeated text across originals is informational only, never a cut."""
    doc = load(workspace, asset_id)
    if not doc:
        return []
    texts = {
        " ".join(u["text"].split()): u["utterance_id"]
        for u in doc["utterances"]
        if len(u["text"].strip()) >= 12
    }
    result = []
    for asset in workspace.list_source_assets():
        if asset.asset_id == asset_id:
            continue
        other = load(workspace, asset.asset_id)
        for cue in other["utterances"] if other else []:
            normalized = " ".join(cue["text"].split())
            if normalized in texts:
                result.append(
                    {
                        "utterance_id": texts[normalized],
                        "other_source": asset.source_name,
                        "other_utterance": cue["utterance_id"],
                        "text": cue["text"],
                    }
                )
    return result
