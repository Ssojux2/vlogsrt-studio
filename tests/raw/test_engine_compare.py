"""W-31f(D92) — 시간 엔진 비교: 리비전 고르기 · 가장 가까운 경계 · 환각류 줄 · 판정 · 보고서.

순수 계층(`engine_compare`)과 읽기 전용 보고서(`scripts/engine_compare_report.py`). 보고서는 손으로
지은 작업공간 DB(필요한 표만)를 `mode=ro`로 읽는다 — 아무것도 쓰지 않고 기본 엔진을 바꾸지 않는다.
유료 호출·디코드·VAD는 없다.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from vlogsrt.project.raw import engine_compare, source_transcript, timing_engine, timing_truth
from vlogsrt.project.raw.config import (
    ENGINE_COMPARE_SETTINGS,
    TIMING_ENGINE_GEMINI,
    EngineCompareSettings,
)

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import engine_compare_report as report  # noqa: E402

F = Fraction
STRATUM = timing_truth.STRATA[0]


def _row(cue_id: str, start: Fraction, end: Fraction, text: str) -> dict[str, Any]:
    return {
        "utterance_id": cue_id,
        "start": source_transcript.encoded(start),
        "end": source_transcript.encoded(end),
        "text": text,
    }


def _revision(sequence: int, rows: list[dict[str, Any]], *, edited: bool = False) -> dict:
    return {
        "asset_id": "a1",
        "revision": f"rev{sequence}",
        "sequence": sequence,
        "edited": edited,
        "source_sha256": "a" * 64,
        "duration": source_transcript.encoded(F(30)),
        "utterances": rows,
        "dirty_ranges": [],
    }


def _mark(cue_id: str, edge: str, seconds: Fraction, text: str = "") -> timing_truth.TruthMark:
    return timing_truth.TruthMark("a1", cue_id, edge, seconds, "rev0", text, STRATUM, "2026-10-27")


WHISPER_ROWS = [
    _row("utterance_000000", F(1), F(3), "안녕하세요"),
    _row("utterance_000001", F(4), F(6), "Be safe"),
    _row("utterance_000002", F(6), F(8), "Be safe"),
    _row("utterance_000003", F(8), F(10), "Be safe"),
]
GEMINI_ROWS = [
    _row("utterance_000000", F(11, 10), F(29, 10), "안녕하세요."),
    _row("utterance_000001", F(41, 10), F(5), "오늘은 한강에 왔어요!"),
]


# ── 순수 계층 ─────────────────────────────────────────────────────────────


def test_the_pair_is_the_latest_unedited_seed_and_the_revision_just_before_it() -> None:
    whisper, snapped = _revision(0, WHISPER_ROWS), _revision(1, WHISPER_ROWS, edited=True)
    gemini = _revision(2, GEMINI_ROWS)
    pair = engine_compare.pick_pair([gemini, whisper, snapped])
    assert pair is not None and pair.engine is gemini and pair.baseline is snapped
    assert engine_compare.pick_pair([snapped]) is None  # 고치지 않은 리비전이 없다
    alone = engine_compare.pick_pair([gemini | {"sequence": 0}])
    assert alone is not None and alone.baseline is None


def test_marks_meet_the_nearest_edge_of_the_same_side_whatever_the_line_ids() -> None:
    lines = timing_truth.lines_of(_revision(2, GEMINI_ROWS))
    marks = [
        _mark("utterance_000003", timing_truth.EDGE_START, F(4)),
        _mark("utterance_000003", timing_truth.EDGE_END, F(52, 10)),
    ]
    start, end = engine_compare.nearest_pairs(marks, lines)
    assert (start.cue_id, start.measured, start.error) == ("utterance_000001", F(41, 10), F(1, 10))
    assert (end.cue_id, end.measured, end.error) == ("utterance_000001", F(5), F(-2, 10))
    assert engine_compare.nearest_pairs(marks, ()) == ()


def test_hallucination_lines_count_the_text_rules_only() -> None:
    assert engine_compare.hallucination_lines(_revision(0, WHISPER_ROWS)) == 3  # 같은 말 런 3줄
    assert engine_compare.hallucination_lines(_revision(2, GEMINI_ROWS)) == 0
    stats = engine_compare.line_stats(_revision(2, GEMINI_ROWS))
    assert (stats.lines, stats.chars) == (2, 16)
    assert stats.seconds == F(27, 10) and stats.chars_per_second == F(16) / F(27, 10)


def test_the_verdict_applies_each_line_and_skips_missing_material() -> None:
    base = {"start_hit": F(6, 10), "end_hit": F(6, 10), "start_mae": F(2, 10),
            "hallucination": 10, "recall": F(8, 10)}  # fmt: skip
    edge = {"start_hit": F(6, 10), "end_hit": F(55, 100), "start_mae": F(25, 100),
            "hallucination": 5, "recall": F(78, 100)}  # fmt: skip
    assert engine_compare.verdict(base, edge) == dict.fromkeys(engine_compare.CRITERIA, True)
    worse = {"start_hit": F(59, 100), "end_hit": F(54, 100), "start_mae": F(26, 100),
             "hallucination": 6, "recall": F(77, 100)}  # fmt: skip
    assert engine_compare.verdict(base, worse) == dict.fromkeys(engine_compare.CRITERIA, False)
    missing = engine_compare.verdict(base, {**edge, "recall": None})
    assert missing["recall"] is None and missing["start_hit"] is True


def test_the_criteria_are_the_proposed_lines_and_refuse_nonsense() -> None:
    assert ENGINE_COMPARE_SETTINGS.end_hit_slack == F(5, 100)
    assert ENGINE_COMPARE_SETTINGS.start_mae_slack == F(5, 100)
    assert ENGINE_COMPARE_SETTINGS.hallucination_share == F(1, 2)
    assert ENGINE_COMPARE_SETTINGS.recall_slack == F(2, 100)
    with pytest.raises(ValueError):
        EngineCompareSettings(recall_slack=F(-1, 100))
    with pytest.raises(ValueError):
        EngineCompareSettings(hallucination_share=F(3, 2))


# ── 읽기 전용 보고서 ────────────────────────────────────────────────────────


def _workspace(tmp_path: Path, *, pending: bool = False) -> Path:
    root = tmp_path / "ws"
    root.mkdir()
    db = sqlite3.connect(root / report.DB_NAME)
    db.execute("CREATE TABLE source_assets (asset_id TEXT, source_name TEXT)")
    db.execute("INSERT INTO source_assets VALUES ('a1', 'IMG_8863.MOV')")
    db.execute(
        "CREATE TABLE source_transcripts (asset_id TEXT, sequence INTEGER, revision TEXT,"
        " payload TEXT)"
    )
    for revision in (_revision(0, WHISPER_ROWS), _revision(1, GEMINI_ROWS)):
        db.execute(
            "INSERT INTO source_transcripts VALUES (?,?,?,?)",
            ("a1", revision["sequence"], revision["revision"], json.dumps(revision)),
        )
    db.execute(f"CREATE TABLE {timing_engine.TABLE} (asset_id TEXT, epoch INTEGER, engine TEXT)")
    db.execute(f"INSERT INTO {timing_engine.TABLE} VALUES ('a1', 1, ?)", (TIMING_ENGINE_GEMINI,))
    db.execute(f"CREATE TABLE {report.EPOCH_TABLE} (asset_id TEXT, epoch INTEGER, heard INTEGER)")
    db.execute(f"INSERT INTO {report.EPOCH_TABLE} VALUES ('a1', ?, 1)", (2 if pending else 1,))
    timing_truth._create(db)
    timing_truth._upsert(db, _mark("utterance_000000", timing_truth.EDGE_START, F(1), "안녕하세요"))
    timing_truth._upsert(db, _mark("utterance_000000", timing_truth.EDGE_END, F(3), "안녕하세요"))
    db.commit()
    db.close()
    return root


def test_the_report_compares_the_engine_seed_with_the_revision_before_it(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    before = (root / report.DB_NAME).read_bytes()
    document = report.collect(root)
    assert (root / report.DB_NAME).read_bytes() == before  # 읽기만 한다
    (video,) = document["videos"]
    assert (video["source_name"], video["engine_key"]) == ("IMG_8863.MOV", TIMING_ENGINE_GEMINI)
    assert video["baseline"]["truth"]["start_mae"] == "0"
    assert video["engine"]["truth"]["start_mae"] == "1/10"
    assert (video["baseline"]["hallucination"], video["engine"]["hallucination"]) == (3, 0)
    assert video["paired"]["starts"] == 1  # 이전 자막은 줄 짝 지표도 낸다
    assert video["verdict"]["hallucination"] is True and video["verdict"]["recall"] is None
    assert video["verdict"]["start_hit"] is True
    text = "\n".join(report.render(document))
    assert "IMG_8863.MOV — 새 엔진 gemini" in text and "환각류 줄 통과" in text
    assert "말소리 재현율 재료 없음" in text


def test_a_pending_relisten_and_an_empty_workspace_are_reported_not_measured(
    tmp_path: Path,
) -> None:
    document = report.collect(_workspace(tmp_path, pending=True))
    assert document["videos"] == []
    assert "새 엔진으로 다시 들은 원본이 없다" in "\n".join(report.render(document))
