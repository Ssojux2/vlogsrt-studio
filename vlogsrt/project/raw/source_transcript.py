"""Editable, versioned source-clock captions for cut review, never final SRT."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

from vlogsrt.hangul import strip_for_compare
from vlogsrt.project.raw import read_scope
from vlogsrt.project.raw.models import RawWorkspaceError

TEXT_SOURCE_WHISPER = "whisper"
"""발화 글자가 whisper의 것이다 — 칸이 없는 19c 이전 기록도 이 값으로 읽는다(D59 19c)."""

TEXT_SOURCE_MANUAL = "manual"
"""사람이 글자를 고쳤다(수정·분할 — 양쪽 글자를 사람이 준다·글자를 준 병합)."""

TEXT_SOURCE_MIXED = "mixed"
"""글자 출처가 다른 두 줄을 그대로 이어 붙였다."""

ORIGINAL_FINGERPRINT = "asr-original/v1"
"""고치지 않은 whisper 글자의 원본 자막 지문 — 19c 이전 바이트 그대로."""


def _init(workspace: Any) -> None:
    with workspace.connect() as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS source_transcripts (asset_id TEXT NOT NULL, "
            "sequence INTEGER NOT NULL, revision TEXT NOT NULL UNIQUE, payload TEXT NOT NULL, "
            "PRIMARY KEY(asset_id, sequence))"
        )
        db.commit()


def load(workspace: Any, asset_id: str) -> dict | None:
    """지금 리비전 — 읽기 범위 안에서는 원본마다 한 번 읽는다(W-23 세션 B, D63).

    범위 안의 첫 읽기가 모든 원본의 지금 리비전을 한 연결로 가져온다(그리기는 어차피 전부 읽는다 —
    다른 원본과 같은 자막 후보). 돌려준 문서는 읽기만 한다 — 고치는 쓰기는 `_read_latest`로 새로
    읽는다. 범위가 없으면 예전 그대로 매번 읽는다.
    """
    scope = read_scope.current()
    if scope is None:
        return _read_latest(workspace, asset_id)
    root = str(workspace.root)

    def parsed() -> dict | None:
        payload = scope.memo(
            ("source_transcript.payloads", root), lambda: _read_latest_payloads(workspace)
        ).get(asset_id)
        return None if payload is None else json.loads(payload)

    return scope.memo(("source_transcript.load", root, asset_id), parsed)


def _read_latest(workspace: Any, asset_id: str) -> dict | None:
    """원본 하나의 지금 리비전을 DB에서 — 쓰기는 늘 이것으로 읽는다(기억한 문서를 믿지 않는다)."""
    _init(workspace)
    with workspace.connect() as db:
        row = db.execute(
            "SELECT payload FROM source_transcripts WHERE asset_id=? "
            "ORDER BY sequence DESC LIMIT 1",
            (asset_id,),
        ).fetchone()
    return json.loads(row[0]) if row else None


def _read_latest_payloads(workspace: Any) -> dict[str, str]:
    """원본마다 지금 리비전의 본문(JSON 글) — `(asset_id, sequence)`가 기본 키라 원본마다 하나다."""
    _init(workspace)
    with workspace.connect() as db:
        rows = db.execute(
            "SELECT t.asset_id, t.payload FROM source_transcripts AS t JOIN (SELECT asset_id,"
            " MAX(sequence) AS top FROM source_transcripts GROUP BY asset_id) AS m"
            " ON t.asset_id = m.asset_id AND t.sequence = m.top"
        ).fetchall()
    return {str(row[0]): str(row[1]) for row in rows}


def rational(value: Any) -> Fraction:
    return Fraction(value["num"], value["den"]) if isinstance(value, dict) else Fraction(str(value))


def encoded(value: Fraction) -> dict[str, int]:
    return {"num": value.numerator, "den": value.denominator}


def seed(
    workspace: Any, asset_id: str, utterances: list[dict], duration: Fraction, source_sha256: str
) -> dict:
    current = _read_latest(workspace, asset_id)
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
    saved = _read_latest(workspace, asset_id)
    assert saved is not None
    return saved


def human_edited(document: dict) -> bool:
    """사람이 고친 원본 자막인가 — `edited` 칸이 없는 옛 문서는 둘째 리비전부터 고친 것으로 본다.

    지문과 「다시 듣기」 거절(D59 19b)이 같은 규칙을 쓴다 — 고친 자막을 덮을 수 있는 쪽으로 읽지
    않는다.
    """
    return bool(document.get("edited", document["sequence"] > 0))


def cross_checked(document: dict) -> bool:
    """whisper 것이 아닌 글자가 한 줄이라도 있는가(D59 19c) — 칸이 없는 줄은 whisper다."""
    return any(
        str(row.get("text_source", TEXT_SOURCE_WHISPER)) != TEXT_SOURCE_WHISPER
        for row in document["utterances"]
    )


EAR_ALT = "alt"
"""화면 「다른 귀」 표시 — 두 번째 귀가 다르게 들었지만 게이트를 지나지 못한 후보가 있다
(D59 19e)."""

EAR_REPLACED = "replaced"
"""화면 「다른 귀」 표시 — 두 번째 귀의 글자로 바꾼 줄이다(D59 19e)."""


def ear_evidence(row: Mapping[str, Any]) -> dict[str, str] | None:
    """원본 자막 한 줄의 「다른 귀」 증거(D59 19e) — 화면에 보일 것만. 읽기만 한다.

    `mark`는 `alt`(고르지 않은 후보 `alt_text`가 지금 글자와 다르다) 또는 `replaced`(글자 출처가
    whisper·사람·섞임이 아닌 두 번째 귀다), `other`는 다른 귀의 글자, `first`는 지금 글자와 다른
    whisper 글자(처음 들은 글)다. 「다르다」는 19d (c)의 `agreed`와 같은 접기(`strip_for_compare` —
    공백·부호·대소문자)로 견준다 — 부호만 다른 후보는 후보가 아니다. 증거 칸이 없는 줄(19c 이전·답이
    없던 청크)과 두 귀가 같은 줄은 None이다.
    """
    if "text_source" not in row:
        return None
    text = str(row["text"])
    folded = strip_for_compare(text)
    whisper = str(row.get("whisper_text") or "")
    first = whisper if strip_for_compare(whisper) not in ("", folded) else ""
    alt = str(row.get("alt_text") or "")
    if strip_for_compare(alt) not in ("", folded):
        return {"mark": EAR_ALT, "other": alt, "first": first}
    source = str(row.get("text_source") or TEXT_SOURCE_WHISPER)
    if source not in (TEXT_SOURCE_WHISPER, TEXT_SOURCE_MANUAL, TEXT_SOURCE_MIXED):
        return {"mark": EAR_REPLACED, "other": text, "first": first}
    return None


def fingerprint(workspace: Any, asset_id: str) -> str:
    """원본 자막 지문 — 고치지 않은 whisper 글자 그대로면 `asr-original/v1`(19c 이전 바이트 그대로).

    사람이 고쳤거나(`human_edited`) 두 번째 귀의 글자가 한 줄이라도 실렸으면(`cross_checked`,
    D59 19c) 발화를 해시한다 — 교차 확인된 전사가 whisper 글자로 판정한 낡은 분석과 구별된다.
    """
    document = load(workspace, asset_id)
    if not document or not (human_edited(document) or cross_checked(document)):
        return ORIGINAL_FINGERPRINT
    return _utterance_hash(document)


def binding_fingerprint(workspace: Any, asset_id: str, cross_check: bool) -> str:
    """분석 결속·견적 digest에 싣는 원본 자막 지문(D59 19d).

    교차 확인이 꺼져 있으면 `fingerprint` 그대로다(19d 이전 바이트). 켜져 있으면 두 번째 귀가 얹은
    글자는 지문에서 뺀다 — 그 글자는 결속의 교차 확인 꼬리표(글자 요청의 신원)와 whisper 답이 함께
    정하는 결과라서, 실행 **전**에 묶은 결속이 실행 **뒤**에도 같아야 끝난 분석을 다시 찾는다(19c-5
    알려진 한계). 사람이 고친 자막은 언제나 발화를 해시한다 — 고친 자막은 결속을 바꾼다."""
    if not cross_check:
        return fingerprint(workspace, asset_id)
    document = load(workspace, asset_id)
    if not document or not human_edited(document):
        return ORIGINAL_FINGERPRINT
    return _utterance_hash(document)


def _utterance_hash(document: dict) -> str:
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
    current = _read_latest(workspace, asset_id)
    if not current or current["revision"] != expected_revision:
        raise RawWorkspaceError("원본 자막이 바뀌었습니다. 최신 자막에서 다시 수정하세요")
    rows = [dict(row) for row in current["utterances"]]
    index = next((i for i, row in enumerate(rows) if row["utterance_id"] == utterance_id), None)
    if index is None:
        raise RawWorkspaceError("현재 원본 자막을 선택하세요")
    if action == "edit":
        changed_ids, changed = {utterance_id}, _edit_row(rows[index], text, start, end)
    elif action == "split":
        changed_ids, changed = _split_row(rows, index, split_at, text, right_text)
    elif action == "merge":
        changed_ids, changed = {utterance_id}, _merge_rows(rows, index, text)
    else:
        raise RawWorkspaceError("알 수 없는 자막 편집입니다")
    _validate_rows(rows, rational(current["duration"]), changed_ids)
    payload = dict(
        _carried(current),
        sequence=current["sequence"] + 1,
        edited=True,
        revision=uuid.uuid4().hex,
        utterances=rows,
        dirty_ranges=current["dirty_ranges"]
        + [{"start": encoded(changed[0]), "end": encoded(changed[1])}],
    )
    _store_edit(workspace, asset_id, expected_revision, payload)
    return payload


def _edit_row(
    row: dict, text: str | None, start: str | None, end: str | None
) -> tuple[Fraction, Fraction]:
    """한 줄의 글자·시각을 고친다 — 글자가 바뀌면 그 줄의 글자 출처는 사람이다(D59 19c)."""
    old_start, old_end, old_text = rational(row["start"]), rational(row["end"]), row["text"]
    row.update(
        text=row["text"] if text is None else text,
        start=encoded(rational(start) if start is not None else old_start),
        end=encoded(rational(end) if end is not None else old_end),
    )
    row["start_sample"] = int(
        row.get("start_sample", old_start * 16000) + (rational(row["start"]) - old_start) * 16000
    )
    row["end_sample"] = int(
        row.get("end_sample", old_end * 16000) + (rational(row["end"]) - old_end) * 16000
    )
    if row["text"] != old_text:
        _mark_typed(row)
    return min(old_start, rational(row["start"])), max(old_end, rational(row["end"]))


def _split_row(
    rows: list[dict], index: int, split_at: str | None, text: str | None, right_text: str | None
) -> tuple[set[str], tuple[Fraction, Fraction]]:
    """한 줄을 둘로 — 양쪽 글자는 사람이 준다. 글자 증거(whisper 글자·다른 귀)는 둘 다 갖는다."""
    row = rows[index]
    old_start, old_end = rational(row["start"]), rational(row["end"])
    at = rational(split_at)
    if not old_start < at < old_end or not text or not right_text:
        raise RawWorkspaceError("분할 시각은 자막 안에, 양쪽 문구는 모두 입력하세요")
    right = dict(row, utterance_id="u_" + uuid.uuid4().hex, start=encoded(at), text=right_text)
    right["start_sample"] = int(
        row.get("start_sample", old_start * 16000) + (at - old_start) * 16000
    )
    row.update(end=encoded(at), text=text, end_sample=right["start_sample"])
    _mark_typed(row)
    _mark_typed(right)
    rows.insert(index + 1, right)
    return {row["utterance_id"], right["utterance_id"]}, (old_start, old_end)


def _merge_rows(rows: list[dict], index: int, text: str | None) -> tuple[Fraction, Fraction]:
    """다음 줄과 합친다 — 글자 증거는 양쪽을 이어 붙인다(caption_manual 병합과 같은 선)."""
    if index + 1 >= len(rows):
        raise RawWorkspaceError("합칠 다음 자막이 없습니다")
    row = rows[index]
    old_start = rational(row["start"])
    right = rows.pop(index + 1)
    evidence = _merged_evidence(row, right, typed=bool(text))
    row.update(end=right["end"], text=text or row["text"] + " " + right["text"])
    row["end_sample"] = right.get("end_sample", int(rational(right["end"]) * 16000))
    row.update(evidence)
    return old_start, rational(right["end"])


def _mark_typed(row: dict) -> None:
    """사람이 준 글자 — 글자 증거가 있는 줄만 출처를 바꾼다(없는 줄은 19c 이전 모양 그대로)."""
    if "text_source" in row:
        row["text_source"] = TEXT_SOURCE_MANUAL


def _merged_evidence(left: dict, right: dict, *, typed: bool) -> dict:
    """합친 줄의 글자 증거 — 어느 쪽에도 없으면 아무것도 더하지 않는다."""
    if "text_source" not in left and "text_source" not in right:
        return {}
    sources = {str(side.get("text_source", TEXT_SOURCE_WHISPER)) for side in (left, right)}
    source = sources.pop() if len(sources) == 1 else TEXT_SOURCE_MIXED
    reasons = (str(side.get("retext_reason") or "") for side in (left, right))
    merged = {
        "whisper_text": _joined(
            str(left.get("whisper_text", left["text"])),
            str(right.get("whisper_text", right["text"])),
        ),
        "text_source": TEXT_SOURCE_MANUAL if typed else source,
        "retext_reason": "+".join(dict.fromkeys(reason for reason in reasons if reason)),
        "alt_text": _joined(str(left.get("alt_text") or ""), str(right.get("alt_text") or "")),
    }
    # 글자 답의 출처(D59 19d)는 양쪽이 같을 때만 남긴다 — 섞인 줄은 출처를 지어내지 않는다.
    origins = {str(side["text_origin"]) for side in (left, right) if side.get("text_origin")}
    if len(origins) == 1:
        merged["text_origin"] = origins.pop()
    return merged


def _joined(left: str, right: str) -> str:
    return f"{left} {right}".strip()


StoreGuard = Callable[[Any], None]
"""쓰기 트랜잭션 안에서 같은 연결로 하는 검사 — 거절하려면 `RawWorkspaceError`를 올린다."""


def _store_edit(
    workspace: Any,
    asset_id: str,
    expected_revision: str,
    payload: dict,
    *,
    guard: StoreGuard | None = None,
    stale: str = "다른 자막 편집이 먼저 저장됐습니다. 다시 확인하세요",
) -> None:
    """고친 리비전을 쌓는다 — 그사이 다른 편집이 먼저 저장됐으면 `stale`로 거절한다.

    `guard`는 `BEGIN IMMEDIATE` 안에서 같은 연결로 돈다 — 그 검사와 이 쓰기 사이에 다른 쓰기
    (다시 듣기 차례 올리기·작업 등록, 모두 같은 DB의 쓰기 트랜잭션)가 끼지 못한다. 거절이면 아무것도
    쓰지 않는다(커밋 전에 연결이 닫혀 되감긴다).
    """
    with workspace.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        latest = db.execute(
            "SELECT revision FROM source_transcripts WHERE asset_id=? "
            "ORDER BY sequence DESC LIMIT 1",
            (asset_id,),
        ).fetchone()
        if not latest or latest[0] != expected_revision:
            raise RawWorkspaceError(stale)
        if guard is not None:
            guard(db)
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


REFILL_FIELD = "refill_id"
"""말소리 빈칸 재전사(D59 19f)가 넣은 줄의 출처 칸 — 그 재전사 견적 digest의 앞 16자리."""


@dataclass(frozen=True, slots=True)
class InsertOutcome:
    """빈칸 줄 넣기의 결과 — 넣은 줄, 기존 줄과 겹쳐 넣지 않은 줄, 원본 범위 밖이라 넣지 않은 줄.

    넣지 않은 줄도 글자·시각·이유째 남는다(규칙 9 — 조용히 사라지지 않는다). 넣은 줄이 없으면
    새 리비전을 만들지 않는다(`document`는 지금 문서 그대로).
    """

    document: dict
    inserted: tuple[str, ...]
    overlapped: tuple[dict, ...]
    out_of_range: tuple[dict, ...]


def insert_segments(
    workspace: Any, asset_id: str, expected_revision: str, rows: list[dict]
) -> InsertOutcome:
    """빈칸 재전사의 whisper 줄을 넣는다(D59 19f) — 기존 줄은 바이트까지 그대로, 겹치지 않는 줄만.

    기존 줄과(또는 먼저 받은 새 줄과) 시각이 겹치는 줄, 원본 범위 `[0, 길이]` 밖의 줄은 넣지 않고
    결과에 남긴다 — 부르는 쪽이 WARN하고 기록한다. 넣으면 새 리비전은 `edited=True`다: 다음 1단계
    심기(`seed`)가 이 자막을 덮지 않는다(그래서 다시 듣기는 거절되고 교차 확인 글자는 제안으로만
    남는다 — 화면이 순서를 경고한다). 바뀐 범위는 줄마다 `dirty_ranges`에 남는다. 지금 리비전이
    `expected_revision`이 아니면 거절한다 — 견적 뒤에 바뀐 자막에 넣지 않는다.
    """
    current = _read_latest(workspace, asset_id)
    if not current or current["revision"] != expected_revision:
        raise RawWorkspaceError(
            "원본 자막이 바뀌었습니다. 빈칸을 다시 세어 새 견적으로 승인하세요 — 자막은"
            " 그대로입니다"
        )
    duration = rational(current["duration"])
    existing = list(current["utterances"])
    taken = {str(row["utterance_id"]) for row in existing}
    accepted: list[dict] = []
    overlapped: list[dict] = []
    outside: list[dict] = []
    for row in sorted(rows, key=lambda item: (rational(item["start"]), rational(item["end"]))):
        _require_new_row(row, taken)
        clash = _clash(row, [*existing, *accepted])
        if not _within(row, duration):
            outside.append(_left_out(row, None))
        elif clash is not None:
            overlapped.append(_left_out(row, clash))
        else:
            accepted.append(dict(row))
            taken.add(str(row["utterance_id"]))
    if not accepted:
        return InsertOutcome(current, (), tuple(overlapped), tuple(outside))
    payload = _inserted_payload(current, existing, accepted)
    _store_edit(workspace, asset_id, expected_revision, payload)
    inserted = tuple(str(row["utterance_id"]) for row in accepted)
    return InsertOutcome(payload, inserted, tuple(overlapped), tuple(outside))


def _require_new_row(row: dict, taken: set[str]) -> None:
    """새 줄의 꼴 — 아이디가 겹치지 않고 글자가 있으며 시각이 뒤집히지 않았다(길이 0은 된다)."""
    if str(row.get("utterance_id")) in taken or not isinstance(row.get("text"), str):
        raise RawWorkspaceError(
            f"넣을 자막 줄의 아이디·글자가 맞지 않습니다: {row.get('utterance_id')}"
        )
    if rational(row["start"]) > rational(row["end"]):
        raise RawWorkspaceError(f"넣을 자막 줄의 시각이 뒤집혔습니다: {row['utterance_id']}")


def _within(row: dict, duration: Fraction) -> bool:
    return rational(row["start"]) >= 0 and rational(row["end"]) <= duration


def _clash(row: dict, others: list[dict]) -> str | None:
    """시각이 겹치는 첫 줄의 아이디 — 맞닿기만 한 줄은 겹치지 않는다(길이 0 줄도 같은 규칙)."""
    start, end = rational(row["start"]), rational(row["end"])
    for other in others:
        if start < rational(other["end"]) and rational(other["start"]) < end:
            return str(other["utterance_id"])
    return None


def _left_out(row: dict, clash: str | None) -> dict:
    """넣지 않은 줄의 기록 — 글자·시각·겹친 줄(범위 밖이면 None)을 그대로 남긴다."""
    return {
        "utterance_id": row["utterance_id"],
        "text": row["text"],
        "start": row["start"],
        "end": row["end"],
        "with": clash,
    }


def _inserted_payload(current: dict, existing: list[dict], accepted: list[dict]) -> dict:
    """기존 줄의 순서·바이트를 그대로 두고 새 줄을 시각 자리에 끼운 새 리비전."""
    pending = list(accepted)
    merged: list[dict] = []
    for row in existing:
        while pending and rational(row["end"]) > rational(pending[0]["start"]):
            merged.append(pending.pop(0))
        merged.append(row)
    merged.extend(pending)
    ranges = [{"start": row["start"], "end": row["end"]} for row in accepted]
    return dict(
        _carried(current),
        sequence=current["sequence"] + 1,
        edited=True,
        revision=uuid.uuid4().hex,
        utterances=merged,
        dirty_ranges=current["dirty_ranges"] + ranges,
    )


# ─────────────────────────────────────────────────────────────
#  시각 맞춤 적용·되돌리기(D59 19h) — 사람이 누를 때만, 서비스의 `apply_snap`·`revert_snap`만 부른다
# ─────────────────────────────────────────────────────────────

SNAP_FIELD = "snap"
"""시각 맞춤을 적용한 리비전의 출처 기록 — 그 리비전에만 있다(다음 리비전으로 옮기지 않는다)."""

SNAP_UNDO_FIELD = "snap_undo"
"""시각 맞춤을 되돌린 리비전의 기록 — 그 리비전에만 있다."""

REVISION_ONLY_FIELDS = (SNAP_FIELD, SNAP_UNDO_FIELD)
"""리비전 하나에만 속하는 기록 — 다음 편집·넣기·적용·되돌리기가 새 리비전으로 옮기지 않는다."""

SNAP_SCHEMA = "sync-snap-apply/v1"
SNAP_UNDO_SCHEMA = "sync-snap-undo/v1"

SNAP_REVISION_CHANGED = (
    "원본 자막이 그사이 바뀌었습니다 — 새 시각 맞춤 제안을 확인하고 다시 적용하세요. 아무것도"
    " 바꾸지 않았습니다."
)
SNAP_ROWS_MISMATCH = (
    "시각 맞춤 제안이 지금 원본 자막의 줄과 맞지 않습니다 — 모든 줄의 새 시각을 함께 적용합니다."
    " 아무것도 바꾸지 않았습니다."
)
SNAP_NO_CHANGE = "바꿀 시각이 없습니다 — 아무것도 바꾸지 않았습니다."
UNDO_CHANGED = (
    "원본 자막이 그사이 바뀌어 되돌리지 않습니다 — 나중에 고치거나 넣은 내용을 지우지 않습니다."
    " 새 화면을 확인하세요. 아무것도 바꾸지 않았습니다."
)
SNAP_ALREADY_APPLIED = (
    "이미 시각 맞춤을 적용한 자막입니다 — 다시 맞추려면 「되돌리기」로 적용 전 자막으로 돌린 뒤 새"
    " 제안을 들어 보세요. 아무것도 바꾸지 않았습니다."
)
UNDO_NOTHING = "되돌릴 시각 맞춤이 없습니다 — 아무것도 바꾸지 않았습니다."
UNDO_MISSING = "적용 전 원본 자막을 찾지 못해 되돌리지 않습니다 — 아무것도 바꾸지 않았습니다."


def _carried(current: Mapping[str, Any]) -> dict:
    """새 리비전이 이어받는 칸 — 리비전 하나에만 속하는 기록(`REVISION_ONLY_FIELDS`)은 뺀다.

    그 기록이 없는 문서(19h 이전·적용한 적 없는 자막)에서는 `dict(current)`와 바이트까지 같다.
    """
    return {key: value for key, value in current.items() if key not in REVISION_ONLY_FIELDS}


def load_revision(workspace: Any, asset_id: str, revision: str) -> dict | None:
    """원본 자막의 정해진 리비전 하나 — 없으면 None. 읽기만 한다."""
    _init(workspace)
    with workspace.connect() as db:
        row = db.execute(
            "SELECT payload FROM source_transcripts WHERE asset_id=? AND revision=?",
            (asset_id, revision),
        ).fetchone()
    return json.loads(row[0]) if row else None


def latest_on(db: Any, asset_id: str) -> dict | None:
    """연결 하나 위의 지금 리비전 — 다른 쓰기 트랜잭션 안의 검사가 같은 연결로 읽는다(읽기만).

    표가 없으면(원본 자막을 심은 적이 없는 작업공간) None이다 — 표를 만들지 않는다.
    """
    table = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_transcripts'"
    ).fetchone()
    if table is None:
        return None
    row = db.execute(
        "SELECT payload FROM source_transcripts WHERE asset_id=? ORDER BY sequence DESC LIMIT 1",
        (asset_id,),
    ).fetchone()
    return json.loads(row[0]) if row else None


def snap_applied(document: Mapping[str, Any]) -> dict | None:
    """이 리비전이 시각 맞춤을 적용한 리비전이면 그 기록 — 아니면 None.

    기록은 자기 리비전 아이디를 싣는다 — 기록이 다른 리비전으로 옮겨 가도(옮기지 않지만) 그
    리비전을 적용한 리비전으로 읽지 않는다.
    """
    record = document.get(SNAP_FIELD)
    if isinstance(record, dict) and record.get("revision") == document.get("revision"):
        return record
    return None


def apply_changes(
    workspace: Any,
    asset_id: str,
    expected_revision: str,
    times: Sequence[tuple[str, Fraction, Fraction]],
    *,
    plan_sha256: str,
    settings_version: str,
    guard: StoreGuard | None = None,
) -> dict:
    """시각 맞춤 제안을 리비전 하나로 적용한다(D59 19h) — **모든** 줄의 새 시각을 함께, 아니면 없다.

    `times`는 지금 줄 **전부**의 (아이디, 시작, 끝)이다 — 아이디·순서·수가 지금 줄과 같아야 하고
    시각은 정확 유리수(공통 시계)다. 일부만 주면 거절한다(D59 19g 「적용 규칙」). 바뀌는 칸은
    `start`·`end`뿐이고 시각이 같은 줄은 바이트 그대로다 — 글자·아이디·순서·수·증거 칸(whisper 틱
    `start_sample`·`end_sample` 포함)은 손대지 않는다. 새 리비전은 `edited=True`, 바뀐 줄마다
    `dirty_ranges` 하나, 출처 기록(`SNAP_FIELD`)을 싣는다. 편집 검사를 지나야 하고, 본 리비전·한
    걸음 규칙(이미 적용한 리비전은 거절)은 `_snap_target`, `guard`는 쓰기 트랜잭션 안에서 돈다.
    """
    current = _snap_target(workspace, asset_id, expected_revision)
    rows, changed_ids, spans = _retimed(current["utterances"], times)
    if not changed_ids:
        raise RawWorkspaceError(SNAP_NO_CHANGE)
    _validate_rows(rows, rational(current["duration"]), changed_ids)
    revision = uuid.uuid4().hex
    payload = dict(
        _carried(current),
        sequence=current["sequence"] + 1,
        edited=True,
        revision=revision,
        utterances=rows,
        dirty_ranges=current["dirty_ranges"] + spans,
    )
    payload[SNAP_FIELD] = _snap_record(current, revision, plan_sha256, settings_version)
    payload[SNAP_FIELD]["changed"] = len(changed_ids)
    _store_edit(
        workspace, asset_id, expected_revision, payload, guard=guard, stale=SNAP_REVISION_CHANGED
    )
    return payload


def _snap_target(workspace: Any, asset_id: str, expected_revision: str) -> dict:
    """적용할 리비전 — 화면이 본 그 리비전이 지금이고, 이미 시각 맞춤을 적용한 리비전이 아니다."""
    current = _read_latest(workspace, asset_id)
    if not current or current["revision"] != expected_revision:
        raise RawWorkspaceError(SNAP_REVISION_CHANGED)
    if snap_applied(current) is not None:
        raise RawWorkspaceError(SNAP_ALREADY_APPLIED)
    return current


def _snap_record(current: dict, revision: str, plan_sha256: str, settings_version: str) -> dict:
    """적용한 리비전의 출처 기록 — 자기 리비전·적용 전 리비전과 그 `edited`·제안 sha·기준 판."""
    return {
        "schema": SNAP_SCHEMA,
        "revision": revision,
        "from_revision": current["revision"],
        "from_edited": human_edited(current),
        "plan_sha256": plan_sha256,
        "settings_version": settings_version,
    }


def _retimed(
    rows: list[dict], times: Sequence[tuple[str, Fraction, Fraction]]
) -> tuple[list[dict], set[str], list[dict]]:
    """새 시각을 얹은 줄 · 바뀐 줄 아이디 · 바뀐 범위 — 같은 시각의 줄은 그 사전 그대로다."""
    if [str(row["utterance_id"]) for row in rows] != [str(item[0]) for item in times]:
        raise RawWorkspaceError(SNAP_ROWS_MISMATCH)
    retimed: list[dict] = []
    changed: set[str] = set()
    spans: list[dict] = []
    for row, (row_id, start, end) in zip(rows, times, strict=True):
        if not (isinstance(start, Fraction) and isinstance(end, Fraction)):
            raise TypeError(f"시각 맞춤의 시각은 정확 유리수여야 한다: {row_id} {start!r} {end!r}")
        old_start, old_end = rational(row["start"]), rational(row["end"])
        if (start, end) == (old_start, old_end):
            retimed.append(row)
            continue
        retimed.append(dict(row, start=encoded(start), end=encoded(end)))
        changed.add(str(row_id))
        spans.append({"start": encoded(min(old_start, start)), "end": encoded(max(old_end, end))})
    return retimed, changed, spans


def restore(
    workspace: Any, asset_id: str, expected_revision: str, *, guard: StoreGuard | None = None
) -> dict:
    """시각 맞춤을 되돌린다(D59 19h) — 적용 전 리비전의 줄과 `edited`를 바이트 그대로 새 리비전에.

    지금 리비전이 `expected_revision`이고 그것이 시각 맞춤을 적용한 리비전일 때만 된다 — 그 뒤에
    고치거나 넣은 리비전이 있으면 거절한다(나중의 사람 편집·빈칸 재전사를 조용히 지우지 않는다).
    그래서 지문(`fingerprint`)·`human_edited`·다시 듣기 자격이 적용 전과 같아진다. 새 리비전은
    되돌린 범위를 `dirty_ranges`에 더하고 되돌린 기록(`SNAP_UNDO_FIELD`)을 싣는다.
    """
    current = _read_latest(workspace, asset_id)
    if not current or current["revision"] != expected_revision:
        raise RawWorkspaceError(UNDO_CHANGED)
    record = snap_applied(current)
    if record is None:
        raise RawWorkspaceError(UNDO_NOTHING)
    source = load_revision(workspace, asset_id, str(record["from_revision"]))
    if source is None:
        raise RawWorkspaceError(UNDO_MISSING)
    revision = uuid.uuid4().hex
    payload = dict(
        _carried(current),
        sequence=current["sequence"] + 1,
        revision=revision,
        utterances=source["utterances"],
        dirty_ranges=current["dirty_ranges"]
        + _restored_spans(current["utterances"], source["utterances"]),
    )
    _restore_edited(payload, source)
    payload[SNAP_UNDO_FIELD] = {
        "schema": SNAP_UNDO_SCHEMA,
        "revision": revision,
        "undone_revision": current["revision"],
        "restored_revision": source["revision"],
        "plan_sha256": record["plan_sha256"],
    }
    _store_edit(workspace, asset_id, expected_revision, payload, guard=guard, stale=UNDO_CHANGED)
    return payload


def _restore_edited(payload: dict, source: Mapping[str, Any]) -> None:
    """되돌린 리비전의 `edited` — `human_edited`와 심기(`seed`)가 적용 전과 **똑같이** 읽게.

    칸이 있으면 그 값 그대로다. 칸이 없는 옛 문서는 둘로 갈린다: 차례 > 0이면 칸을 빼 둔다 —
    되돌린 리비전의 차례도 > 0이라 `human_edited`는 그대로 참이고, 심기는 적용 전처럼 칸이 없는
    문서로 본다(`current.get("edited")`가 없음). 차례 0이면 `edited=False`를 적는다 — 칸 없이 두면
    새 차례(> 0) 때문에 「고친 자막」으로 읽힌다.
    """
    if "edited" in source:
        payload["edited"] = source["edited"]
    elif int(source["sequence"]) > 0:
        payload.pop("edited", None)
    else:
        payload["edited"] = False


def _restored_spans(applied: list[dict], restored: list[dict]) -> list[dict]:
    """되돌려 시각이 바뀌는 줄마다 그 범위 — 적용은 줄의 아이디·순서를 바꾸지 않는다."""
    if [row["utterance_id"] for row in applied] != [row["utterance_id"] for row in restored]:
        raise RawWorkspaceError(UNDO_MISSING)
    spans: list[dict] = []
    for now, before in zip(applied, restored, strict=True):
        now_span = (rational(now["start"]), rational(now["end"]))
        old_span = (rational(before["start"]), rational(before["end"]))
        if now_span != old_span:
            low, high = min(now_span[0], old_span[0]), max(now_span[1], old_span[1])
            spans.append({"start": encoded(low), "end": encoded(high)})
    return spans


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
    from vlogsrt.project.raw.understanding import UtteranceEvidence, utterance_text_evidence

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
            # 글자 증거(D59 19c)도 돌려준다 — 2단계가 같은 발화로 다시 심을 때 증거가 사라지면
            # 지문이 whisper 것으로 돌아가 교차 확인된 전사가 낡은 분석과 섞인다.
            **utterance_text_evidence(u),
        )
        for i, u in enumerate(doc["utterances"])
    )


def cross_source_candidates(workspace: Any, asset_id: str) -> list[dict]:
    """Exact repeated text across originals is informational only, never a cut.

    읽기 범위 안에서는 모든 원본의 줄을 글자별 색인 하나로 한 번만 모은다(W-23 세션 B) — 원본마다
    다른 원본 17개를 다시 읽던 길이다. 결과의 줄과 순서(원본 목록 순 → 줄 순)는 범위 밖과 같다.
    """
    doc = load(workspace, asset_id)
    if not doc:
        return []
    texts = {
        " ".join(u["text"].split()): u["utterance_id"]
        for u in doc["utterances"]
        if len(u["text"].strip()) >= 12
    }
    scope = read_scope.current()
    if scope is not None:
        index = scope.memo(
            ("source_transcript.cue_index", str(workspace.root)), lambda: _cue_index(workspace)
        )
        return _indexed_candidates(index, texts, asset_id)
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


@dataclass(frozen=True, slots=True)
class _IndexedCue:
    """색인의 줄 하나 — 원본 목록 안 자리·줄 자리가 범위 밖 순회의 순서를 다시 만든다."""

    asset_position: int
    cue_position: int
    asset_id: str
    source_name: str
    utterance_id: Any
    text: Any


def _cue_index(workspace: Any) -> dict[str, list[_IndexedCue]]:
    """모든 원본의 줄 — 공백을 접은 글 → 그 글의 줄들(원본 목록 순, 그 안은 줄 순)."""
    index: dict[str, list[_IndexedCue]] = {}
    for asset_position, asset in enumerate(workspace.list_source_assets()):
        other = load(workspace, asset.asset_id)
        for cue_position, cue in enumerate(other["utterances"] if other else []):
            entry = _IndexedCue(
                asset_position,
                cue_position,
                asset.asset_id,
                asset.source_name,
                cue["utterance_id"],
                cue["text"],
            )
            index.setdefault(" ".join(cue["text"].split()), []).append(entry)
    return index


def _indexed_candidates(
    index: Mapping[str, list[_IndexedCue]], texts: Mapping[str, Any], asset_id: str
) -> list[dict]:
    """색인으로 찾은 다른 원본의 같은 글 — 범위 밖 순회와 같은 줄, 같은 순서."""
    found = sorted(
        (entry for normalized in texts for entry in index.get(normalized, ())),
        key=lambda entry: (entry.asset_position, entry.cue_position),
    )
    return [
        {
            "utterance_id": texts[" ".join(entry.text.split())],
            "other_source": entry.source_name,
            "other_utterance": entry.utterance_id,
            "text": entry.text,
        }
        for entry in found
        if entry.asset_id != asset_id
    ]
