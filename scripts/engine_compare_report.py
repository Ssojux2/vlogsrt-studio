#!/usr/bin/env python3
"""시간 엔진 비교 보고서(W-31f, D92) — 새 엔진으로 다시 들은 원본을 그 앞 자막과 견준다.

    python scripts/engine_compare_report.py <작업공간> [--json]

작업공간을 **읽기만** 한다: DB는 `file:...?mode=ro` URI로만 열고, 자막 시각 확인 보고서와 저장된
말소리 칸(`cache/sync-check`)은 파일을 읽기만 한다(`scripts/timing_truth_report.py`와 같은 길).
디코드·VAD·유료 호출 0. 기본 엔진을 바꾸지 않는다 — 판정을 적을 뿐이다(바꾸는 것은 사람이다).

대상은 고정 표(`source_timing_engines`)가 지금 차례를 whisper가 아닌 엔진으로 적은 원본이다(다시
듣기를 기다리는 원본은 뺀다). 원본마다 새 엔진 전사와 그 바로 앞 리비전
(`engine_compare.pick_pair`)을 같은 재료로 잰다:

- 시각 정답 — 가장 가까운 경계 MAE · ±적중 · 끝 자름(두 쪽 같은 규칙), 기준은 줄 짝 지표도
- 환각류 「확인 필요」 줄 수
- 말소리 정밀도 Pτ · 재현율 Rτ(같은 PCM의 저장된 말소리 칸으로 두 리비전을 다시 잰다)
- 줄 수 · 초당 글자

그리고 `EngineCompareSettings`의 기준마다 새 엔진이 선을 넘는지(영상마다 · 전체).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Mapping, Sequence
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from vlogsrt.project import sync_check  # noqa: E402
from vlogsrt.project.config import SYNC_CHECK_SETTINGS, ProjectConfig  # noqa: E402
from vlogsrt.project.raw import (  # noqa: E402
    engine_compare,
    sync_report,
    timing_engine,
    timing_truth,
)
from vlogsrt.project.raw.config import TIMING_ENGINE_WHISPER  # noqa: E402

DB_NAME = "project.sqlite3"
EPOCH_TABLE = "source_asr_epochs"
BASELINE = "baseline"
ENGINE = "engine"
SIDES = (BASELINE, ENGINE)


def _connect(workspace: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{workspace / DB_NAME}?mode=ro", uri=True)


def _table(connection: sqlite3.Connection, name: str) -> bool:
    found = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return found is not None


def engine_sources(connection: sqlite3.Connection) -> dict[str, str]:
    """원본 → 지금 차례의 엔진 열쇠 — whisper가 아니고 다시 듣기를 기다리지 않는 원본만."""
    if not _table(connection, timing_engine.TABLE):
        return {}
    epochs: dict[str, tuple[int, int]] = {}
    if _table(connection, EPOCH_TABLE):
        epochs = {
            str(asset): (int(epoch), int(heard))
            for asset, epoch, heard in connection.execute(
                f"SELECT asset_id, epoch, heard FROM {EPOCH_TABLE}"
            )
        }
    found: dict[str, str] = {}
    rows = connection.execute(f"SELECT asset_id, epoch, engine FROM {timing_engine.TABLE}")
    for asset, epoch, engine in rows:
        current, heard = epochs.get(str(asset), (0, 0))
        if int(epoch) == current and current == heard and engine != TIMING_ENGINE_WHISPER:
            found[str(asset)] = str(engine)
    return found


def _revisions(connection: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    found: dict[str, list[dict[str, Any]]] = {}
    for asset_id, payload in connection.execute("SELECT asset_id, payload FROM source_transcripts"):
        found.setdefault(str(asset_id), []).append(json.loads(payload))
    return found


def _speech(workspace: Path, asset_id: str, revisions: Sequence[Mapping[str, Any]]) -> Any:
    """그 원본 PCM의 (보고서의 pcm, 저장된 말소리 칸) — 어느 리비전의 보고서든 같은 PCM이다."""
    cache_root = workspace / sync_report.SYNC_CACHE
    for revision in revisions:
        path = sync_report.report_path(
            cache_root, asset_id, str(revision["revision"]), SYNC_CHECK_SETTINGS
        )
        report = sync_report.load_report(path)
        if report is None or not report.get("pcm"):
            continue
        speech = sync_report.stored_speech(cache_root, report["pcm"], ProjectConfig())
        if speech is not None:
            return report["pcm"], speech
    return None


def _coverage(transcript: Mapping[str, Any], measured: Any) -> dict[str, Any]:
    """말소리 정밀도·재현율 — 저장된 말소리 칸으로 그 리비전의 큐를 다시 잰다(메모리에서만)."""
    if measured is None:
        return {"precision": None, "recall": None, "speech_seconds": None}
    pcm, speech = measured
    shift = Fraction(str(pcm["audio_start"]))
    cues = tuple(
        replace(cue, start=cue.start - shift, end=cue.end - shift)
        for cue in sync_report.cues_of(transcript)
    )
    metrics = sync_check.measure(cues, speech, SYNC_CHECK_SETTINGS)
    return {
        "precision": metrics.precision,
        "recall": metrics.recall,
        "speech_seconds": metrics.speech_seconds,
    }


def _side(
    transcript: Mapping[str, Any], marks: Sequence[timing_truth.TruthMark], measured: Any
) -> dict[str, Any]:
    """리비전 하나의 재료 — 가장 가까운 경계 짝 · 환각류 줄 · 말소리 몫 · 줄 통계."""
    lines = timing_truth.lines_of(transcript)
    stats = engine_compare.line_stats(transcript)
    return {
        "revision": str(transcript["revision"]),
        "sequence": int(transcript["sequence"]),
        "nearest": engine_compare.nearest_pairs(marks, lines),
        "hallucination": engine_compare.hallucination_lines(transcript),
        "lines": stats.lines,
        "chars_per_second": stats.chars_per_second,
        **_coverage(transcript, measured),
    }


def _video(
    workspace: Path,
    asset_id: str,
    revisions: Sequence[Mapping[str, Any]],
    marks: Sequence[timing_truth.TruthMark],
) -> dict[str, Any] | str:
    """원본 하나 — 두 리비전의 재료와 기준 리비전의 줄 짝(없으면 그 까닭)."""
    pair = engine_compare.pick_pair(revisions)
    if pair is None:
        return "고치지 않은 리비전 없음"
    if pair.baseline is None:
        return "앞 리비전 없음(처음부터 새 엔진으로 들었다)"
    measured = _speech(workspace, asset_id, (pair.engine, pair.baseline))
    baseline = _side(pair.baseline, marks, measured)
    states = timing_truth.judge(marks, timing_truth.lines_of(pair.baseline))
    baseline["paired"] = timing_truth.pairs(states, timing_truth.lines_of(pair.baseline))
    return {BASELINE: baseline, ENGINE: _side(pair.engine, marks, measured)}


def collect(workspace: Path) -> dict[str, Any]:
    """작업공간 하나의 비교 문서 — 아무것도 쓰지 않는다."""
    connection = _connect(workspace)
    try:
        engines = engine_sources(connection)
        revisions = _revisions(connection)
        names = dict(connection.execute("SELECT asset_id, source_name FROM source_assets"))
        everything = timing_truth.marks_on(connection)
    finally:
        connection.close()
    marks: dict[str, list[timing_truth.TruthMark]] = {}
    for entry in everything:
        marks.setdefault(entry.asset_id, []).append(entry)
    videos: dict[str, dict[str, Any] | str] = {
        asset_id: _video(workspace, asset_id, revisions.get(asset_id, []), marks.get(asset_id, []))
        for asset_id in sorted(engines, key=lambda asset: str(names.get(asset, asset)))
    }
    return _document(videos, names, engines)


# ── 문서 ─────────────────────────────────────────────────────────────────────


def _summary(sides: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """한쪽(기준 또는 새 엔진)의 합 — 짝은 모아 재고, 재현율은 말소리 길이로 가중한다."""
    truth = timing_truth.metrics(pair for side in sides for pair in side["nearest"])
    measured = [s for s in sides if s["recall"] is not None and s["speech_seconds"]]
    weight = sum((s["speech_seconds"] for s in measured), Fraction(0))
    recall = (
        None if not weight else sum(s["recall"] * s["speech_seconds"] for s in measured) / weight
    )
    precision_rows = [s for s in measured if s["precision"] is not None]
    precision_weight = sum((s["speech_seconds"] for s in precision_rows), Fraction(0))
    precision = (
        None
        if not precision_weight
        else sum(s["precision"] * s["speech_seconds"] for s in precision_rows) / precision_weight
    )
    return {
        "truth": truth,
        engine_compare.START_HIT: truth.start_hit,
        engine_compare.END_HIT: truth.end_hit,
        engine_compare.START_MAE: truth.start_mae,
        engine_compare.HALLUCINATION: sum(side["hallucination"] for side in sides),
        engine_compare.RECALL: recall,
        "precision": precision,
        "lines": sum(side["lines"] for side in sides),
    }


def _document(
    videos: Mapping[str, dict[str, Any] | str], names: Mapping[str, str], engines: Mapping[str, str]
) -> dict[str, Any]:
    measured = {asset: video for asset, video in videos.items() if not isinstance(video, str)}
    rows = []
    for asset_id, video in videos.items():
        row: dict[str, Any] = {
            "asset_id": asset_id,
            "source_name": names.get(asset_id, asset_id),
            "engine_key": engines[asset_id],
        }
        if isinstance(video, str):
            row["note"] = video
        else:
            summaries = {side: _summary([video[side]]) for side in SIDES}
            row.update(_jsonable(summaries))
            row["paired"] = _jsonable(timing_truth.metrics(video[BASELINE]["paired"]))
            row["verdict"] = engine_compare.verdict(summaries[BASELINE], summaries[ENGINE])
        rows.append(row)
    overall = {side: _summary([video[side] for video in measured.values()]) for side in SIDES}
    return {
        "hit_seconds": str(timing_truth.TIMING_TRUTH_SETTINGS.hit_seconds),
        "videos": rows,
        "overall": _jsonable(overall),
        "verdict": engine_compare.verdict(overall[BASELINE], overall[ENGINE])
        if measured
        else dict.fromkeys(engine_compare.CRITERIA),
    }


def _jsonable(value: Any) -> Any:
    """정확 유리수는 글자로, 지표 묶음은 사전으로 — JSON과 인쇄가 같은 값을 쓴다."""
    if isinstance(value, Fraction):
        return str(value)
    if isinstance(value, timing_truth.TruthMetrics):
        return _jsonable(_fields(value))
    if isinstance(value, Mapping):
        return {key: _jsonable(item) for key, item in value.items()}
    return value


def _fields(metrics: timing_truth.TruthMetrics) -> dict[str, Any]:
    names = ("starts", "ends", "start_mae", "end_mae", "start_hit", "end_hit", "end_cut")
    return {name: getattr(metrics, name) for name in names}


# ── 인쇄 ─────────────────────────────────────────────────────────────────────


def _seconds(value: str | None) -> str:
    return "    -" if value is None else f"{float(Fraction(value)):5.3f}"


def _share(value: str | None) -> str:
    return "    -" if value is None else f"{float(Fraction(value)) * 100:4.0f}%"


def _side_line(label: str, side: Mapping[str, Any]) -> str:
    truth = side["truth"]
    starts = (
        f"정답 시작 {truth['starts']:>3}개 MAE {_seconds(truth['start_mae'])}초"
        f" 적중 {_share(truth['start_hit'])}"
    )
    ends = (
        f"끝 {truth['ends']:>3}개 MAE {_seconds(truth['end_mae'])}초"
        f" 적중 {_share(truth['end_hit'])} 자름 {_share(truth['end_cut'])}"
    )
    speech = f"Pτ {_share(side['precision'])} Rτ {_share(side['recall'])}"
    return (
        f"{label:<10} {starts} │ {ends} │ 환각류 {side['hallucination']:>3}줄 │ {speech}"
        f" │ {side['lines']}줄"
    )


VERDICT_LABELS = {
    engine_compare.START_HIT: "시작 적중",
    engine_compare.END_HIT: "끝 적중",
    engine_compare.START_MAE: "시작 MAE",
    engine_compare.HALLUCINATION: "환각류 줄",
    engine_compare.RECALL: "말소리 재현율",
}


def _verdict_line(verdict: Mapping[str, bool | None]) -> str:
    marks = {True: "통과", False: "미달", None: "재료 없음"}
    return " · ".join(f"{VERDICT_LABELS[name]} {marks[verdict[name]]}" for name in VERDICT_LABELS)


def render(document: Mapping[str, Any]) -> list[str]:
    hit = float(Fraction(document["hit_seconds"])) * 1000
    rows = [f"시간 엔진 비교 보고서 — 정답 적중 폭 ±{hit:.0f}ms · 정답은 가장 가까운 경계로 잰다"]
    for video in document["videos"]:
        rows.append(f"{video['source_name']} — 새 엔진 {video['engine_key']}")
        if "note" in video:
            rows.append(f"  재지 않음: {video['note']}")
            continue
        rows.append(_side_line("  이전", video[BASELINE]))
        rows.append(_side_line("  새 엔진", video[ENGINE]))
        paired = video["paired"]
        rows.append(
            f"  (이전 자막의 줄 짝 지표: 시작 MAE {_seconds(paired['start_mae'])}초 적중"
            f" {_share(paired['start_hit'])} · 끝 MAE {_seconds(paired['end_mae'])}초)"
        )
        rows.append(f"  판정: {_verdict_line(video['verdict'])}")
    if not document["videos"]:
        rows.append(
            "새 엔진으로 다시 들은 원본이 없다 — 「새로 듣는 전사 엔진」을 고르고 다시 들은 뒤"
            " 다시 돌리세요"
        )
        return rows
    rows.append("전체")
    rows.append(_side_line("  이전", document["overall"][BASELINE]))
    rows.append(_side_line("  새 엔진", document["overall"][ENGINE]))
    rows.append(f"  판정: {_verdict_line(document['verdict'])}")
    rows.append("  (글자 품질은 사람이 20줄쯤 들어 보고 정한다 — 이 보고서는 재지 않는다)")
    return rows


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--json", action="store_true", help="비교 문서를 JSON으로 인쇄")
    args = parser.parse_args(argv)
    workspace = args.workspace.expanduser().resolve()
    if not (workspace / DB_NAME).is_file():
        parser.error(f"작업공간 DB가 없다: {workspace / DB_NAME}")
    document = collect(workspace)
    if args.json:
        print(json.dumps(document, ensure_ascii=False, indent=2))
    else:
        print("\n".join(render(document)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
