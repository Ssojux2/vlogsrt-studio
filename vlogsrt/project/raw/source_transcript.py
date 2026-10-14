"""Editable, versioned source-clock captions for cut review, never final SRT."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from fractions import Fraction
from typing import Any

from vlogsrt.hangul import strip_for_compare
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
    current = load(workspace, asset_id)
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
        current,
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


def _store_edit(workspace: Any, asset_id: str, expected_revision: str, payload: dict) -> None:
    """고친 리비전을 쌓는다 — 그사이 다른 편집이 먼저 저장됐으면 거절한다."""
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
