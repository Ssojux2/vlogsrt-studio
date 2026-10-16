"""D59 19c — 원본 자막의 글자 증거: 지문 규칙과 사람 편집(수정·분할·병합)의 증거 처리.

- 고치지 않은 whisper 글자 그대로의 문서는 지문이 `asr-original/v1`이다 — 19c 이전 문서는 바이트까지
  그대로이고, 글자 경로가 돌았어도 바뀐 줄이 없으면 그대로다.
- 두 번째 귀의 글자가 한 줄이라도 실리면 발화를 해시한다 — 교차 확인된 전사가 낡은 분석과 구별된다.
- 고친 자막은 심기가 덮지 않는다. 분할·병합은 증거를 복사하거나 잇는다(caption_manual과 같은 선).
"""

from __future__ import annotations

import hashlib
import json
from fractions import Fraction
from pathlib import Path
from typing import Any

from vlogsrt.project.raw import service as raw
from vlogsrt.project.raw import source_transcript as captions
from vlogsrt.project.raw.workspace import RawWorkspace

GPT = "gpt-transcribe-model"
"""글자 출처 칸에 실리는 모델 이름의 대역 — 지문 규칙은 `whisper`가 아닌지만 본다."""


def _workspace(tmp_path: Path) -> RawWorkspace:
    root = tmp_path / "ws"
    raw.RawWorkspaceService().create_workspace(root, "글자 증거")
    return RawWorkspace.open(root)


def _row(ident: str, start: str, end: str, text: str, **evidence: str) -> dict[str, Any]:
    return {
        "utterance_id": ident,
        "chunk_index": 0,
        "position": int(ident[1:]),
        "text": text,
        "start": captions.encoded(Fraction(start)),
        "end": captions.encoded(Fraction(end)),
        "start_sample": int(Fraction(start) * 16000),
        "end_sample": int(Fraction(end) * 16000),
        "evidence_id": f"req:{ident}",
        **evidence,
    }


def _checked(text: str, whisper: str, source: str, reason: str, alt: str = "") -> dict[str, str]:
    return {
        "text": text,
        "whisper_text": whisper,
        "text_source": source,
        "retext_reason": reason,
        "alt_text": alt,
    }


def _hash(rows: list[dict[str, Any]]) -> str:
    body = json.dumps(rows, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(body).hexdigest()


def test_a_legacy_whisper_document_keeps_the_original_fingerprint(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    rows = [_row("u0", "1", "2", " 망원동"), _row("u1", "3", "4", " 카페")]
    captions.seed(workspace, "src", rows, Fraction(10), "a" * 64)
    assert captions.fingerprint(workspace, "src") == "asr-original/v1"
    assert not captions.cross_checked(captions.load(workspace, "src"))


def test_a_cross_check_that_changed_nothing_keeps_the_original_fingerprint(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    rows = [
        _row("u0", "1", "2", **_checked(" 망원동", " 망원동", "whisper", "agreed")),
        _row("u1", "3", "4", **_checked(" 카페", " 카페", "whisper", "match", "까페")),
    ]
    captions.seed(workspace, "src", rows, Fraction(10), "a" * 64)
    assert captions.fingerprint(workspace, "src") == "asr-original/v1"


def test_one_cross_checked_line_hashes_the_utterances(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    rows = [
        _row("u0", "1", "2", **_checked("망원동.", " 망원동", GPT, "accepted")),
        _row("u1", "3", "4", **_checked(" 카페", " 카페", "whisper", "agreed")),
    ]
    document = captions.seed(workspace, "src", rows, Fraction(10), "a" * 64)
    assert document["edited"] is False and captions.cross_checked(document)
    assert captions.fingerprint(workspace, "src") == _hash(rows)


def test_seeding_never_overwrites_an_edited_cross_checked_transcript(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    rows = [_row("u0", "1", "2", **_checked("망원동.", " 망원동", GPT, "accepted"))]
    document = captions.seed(workspace, "src", rows, Fraction(10), "a" * 64)
    edited = captions.edit(
        workspace, "src", document["revision"], action="edit", utterance_id="u0", text="망원동!"
    )
    again = captions.seed(workspace, "src", rows, Fraction(10), "a" * 64)
    assert again["revision"] == edited["revision"]
    assert again["utterances"][0]["text"] == "망원동!"
    assert again["utterances"][0]["text_source"] == captions.TEXT_SOURCE_MANUAL
    assert again["utterances"][0]["whisper_text"] == " 망원동"  # whisper 글자는 언제나 남는다


def test_a_time_only_edit_keeps_the_text_source(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    rows = [_row("u0", "1", "2", **_checked("망원동.", " 망원동", GPT, "accepted"))]
    document = captions.seed(workspace, "src", rows, Fraction(10), "a" * 64)
    moved = captions.edit(
        workspace, "src", document["revision"], action="edit", utterance_id="u0", end="2.5"
    )
    assert moved["utterances"][0]["text_source"] == GPT


def test_split_copies_the_evidence_to_both_halves_and_marks_them_typed(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    rows = [_row("u0", "1", "3", **_checked("망원동 카페.", " 망원동 카페", GPT, "accepted"))]
    document = captions.seed(workspace, "src", rows, Fraction(10), "a" * 64)
    split = captions.edit(
        workspace,
        "src",
        document["revision"],
        action="split",
        utterance_id="u0",
        split_at="2",
        text="망원동",
        right_text="카페.",
    )
    left, right = split["utterances"]
    for half in (left, right):
        assert half["whisper_text"] == " 망원동 카페"
        assert half["text_source"] == captions.TEXT_SOURCE_MANUAL
        assert half["retext_reason"] == "accepted"


def test_merge_joins_the_evidence(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    rows = [
        _row("u0", "1", "2", **_checked("망원동.", " 망원동", GPT, "accepted")),
        _row("u1", "3", "4", **_checked(" 카페", " 카페", "whisper", "match", "까페")),
        _row("u2", "5", "6", **_checked("커피", "커피", "whisper", "agreed")),
        _row("u3", "7", "8", **_checked("빵", "빵", "whisper", "too_long", "빵 맛있다")),
    ]
    document = captions.seed(workspace, "src", rows, Fraction(10), "a" * 64)
    mixed = captions.edit(workspace, "src", document["revision"], action="merge", utterance_id="u0")
    merged = mixed["utterances"][0]
    assert merged["text"] == "망원동.  카페"
    assert merged["whisper_text"] == "망원동  카페"  # 양쪽 whisper 글자를 그대로 잇는다
    assert merged["text_source"] == captions.TEXT_SOURCE_MIXED
    assert merged["retext_reason"] == "accepted+match"
    assert merged["alt_text"] == "까페"
    same = captions.edit(workspace, "src", mixed["revision"], action="merge", utterance_id="u2")
    joined = same["utterances"][1]
    assert joined["text_source"] == "whisper" and joined["alt_text"] == "빵 맛있다"
    typed = captions.edit(
        workspace, "src", same["revision"], action="merge", utterance_id="u0", text="다 합침"
    )
    assert typed["utterances"][0]["text_source"] == captions.TEXT_SOURCE_MANUAL


def test_legacy_rows_gain_no_evidence_from_edits(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    rows = [_row("u0", "1", "3", " 망원동 카페"), _row("u1", "4", "5", " 커피")]
    document = captions.seed(workspace, "src", rows, Fraction(10), "a" * 64)
    split = captions.edit(
        workspace,
        "src",
        document["revision"],
        action="split",
        utterance_id="u0",
        split_at="2",
        text="망원동",
        right_text="카페",
    )
    merged = captions.edit(workspace, "src", split["revision"], action="merge", utterance_id="u0")
    edited = captions.edit(
        workspace, "src", merged["revision"], action="edit", utterance_id="u1", text="커피!"
    )
    for row in edited["utterances"]:
        assert "text_source" not in row and "whisper_text" not in row
