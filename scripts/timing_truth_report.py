#!/usr/bin/env python3
"""시각 정답 보고서(W-25b, D67) — 사람이 찍은 참 시작·끝과 원본 자막·시각 맞춤 제안을 견준다.

    python scripts/timing_truth_report.py <작업공간> [--json]

작업공간을 **읽기만** 한다: DB는 `file:...?mode=ro` URI로만 열고, 자막 시각 확인 보고서와 저장된
말소리 칸(`cache/sync-check`)은 파일을 읽기만 한다. `RawWorkspaceService`/`RawWorkspace`를 거치지
않는다 — 그 둘은 열기만 해도 DB를 쓴다(`scripts/verify_sync_workspace.py`와 같은 길). 디코드·VAD·
유료 호출 0.

지표(영상마다 · 전체 · 층마다): 시작 MAE · 끝 MAE · ±적중 폭(설정 `TimingTruthSettings.hit_seconds`,
원본 평가의 ±200ms와 같다) 적중률 · 끝 자름 비율(자막 끝이 말보다 먼저 끝남). 정답과 자막은 줄
아이디로 짝짓는다 — 원본 평가(`vlogsrt.evaluate`)의 탐욕 짝짓기는 두 큐 목록을 시각으로 맞추는
도구라 쓰지 않는다(여기는 같은 줄이 이미 정해져 있다). 낡은 정답(줄이 사라졌거나 글자가 바뀜)은
세지 않고 수만 적는다.

「단어 경계」는 줄 경계를 1단계 기록의 시간 엔진 단어 시각(whisper W-25c·D73, Gemini D92)
가운데 찾는 폭
(`SyncSnapSettings.reach_seconds`) 안의 가장 가까운 것으로 옮겨 본 값이다 — 단어 시각으로 줄 경계를
정할지 판단할 숫자일 뿐 자막을 바꾸지 않는다. 단어가 없는 기록(W-25c 앞에 산 전사)은 까닭만 적는다.

같은 지표를 작업대 「시각 맞춤 제안」 칸과 같은 재료(지금 리비전의 보고서 · 저장된 말소리 칸)로
메모리에서만 잰 `sync_snap.propose` 결과에도 매긴다 — 끝 맞춤이 도움이 되는지 여기서 판가름한다.
시각 맞춤을 이미 적용한 자막은 제안이 없다(한 걸음 규칙) — 그때 자막 칸이 곧 적용 뒤의 값이다.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from vlogsrt.project import sync_check, sync_snap  # noqa: E402
from vlogsrt.project.config import (  # noqa: E402
    SYNC_CHECK_SETTINGS,
    SYNC_SNAP_SETTINGS,
    TIMING_TRUTH_SETTINGS,
    ProjectConfig,
)
from vlogsrt.project.raw import source_transcript, sync_report, timing_truth  # noqa: E402

DB_NAME = "project.sqlite3"
SUBTITLE = "subtitle"
PROPOSAL = "proposal"
MATCHED = "subtitle_matched"
"""제안이 있는 영상의 정답만으로 잰 자막 — 제안과 같은 표본에서 견준다."""
WORD = "word"
"""줄 경계를 가장 가까운 시간 엔진 단어 경계로 옮겨 본 값(W-25c — Gemini 원본은 그 단어, D92)."""


def _connect(workspace: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{workspace / DB_NAME}?mode=ro", uri=True)


def _latest_transcripts(connection: sqlite3.Connection) -> dict[str, dict]:
    latest: dict[str, tuple[int, dict]] = {}
    for asset_id, sequence, payload in connection.execute(
        "SELECT asset_id, sequence, payload FROM source_transcripts"
    ):
        if asset_id not in latest or sequence > latest[asset_id][0]:
            latest[asset_id] = (sequence, json.loads(payload))
    return {asset_id: payload for asset_id, (_, payload) in latest.items()}


def _source_names(connection: sqlite3.Connection) -> dict[str, str]:
    return dict(connection.execute("SELECT asset_id, source_name FROM source_assets"))


def _offset(report: Mapping[str, Any]) -> sync_check.ShiftEstimate:
    """보고서의 전체 어긋남 추정 — 서비스의 시각 맞춤 제안과 같은 읽기(정확 유리수)."""
    body = report["metrics"]["offset"]
    seconds = None if body.get("seconds") is None else Fraction(str(body["seconds"]))
    gain = None if body.get("gain") is None else Fraction(str(body["gain"]))
    peak = None if body.get("peak_z") is None else float(body["peak_z"])
    return sync_check.ShiftEstimate(str(body["status"]), seconds, gain, peak)


def _measured(workspace: Path, asset_id: str, revision: str) -> tuple[dict[str, Any], Any] | str:
    """지금 리비전의 확인 보고서와 그 PCM의 저장된 말소리 칸 — 없으면 그 까닭(읽기만)."""
    cache_root = workspace / sync_report.SYNC_CACHE
    path = sync_report.report_path(cache_root, asset_id, revision, SYNC_CHECK_SETTINGS)
    report = sync_report.load_report(path)
    if report is None:
        return "지금 리비전의 확인 보고서 없음"
    if not report.get("pcm"):
        return "측정 불가"
    speech = sync_report.stored_speech(cache_root, report["pcm"], ProjectConfig())
    if speech is None:
        return "저장된 말소리 칸 없음"
    return report, speech


def proposal_lines(
    workspace: Path, asset_id: str, transcript: Mapping[str, Any]
) -> tuple[timing_truth.TruthLine, ...] | str:
    """지금 리비전의 시각 맞춤 제안(공통 시계) — 잴 수 없으면 그 까닭."""
    if source_transcript.snap_applied(transcript) is not None:
        return "이미 적용함"
    revision = str(transcript["revision"])
    measured = _measured(workspace, asset_id, revision)
    if isinstance(measured, str):
        return measured
    report, speech = measured
    pcm = report["pcm"]
    project = ProjectConfig()
    shift = Fraction(str(pcm["audio_start"]))
    cues = tuple(
        replace(cue, start=cue.start - shift, end=cue.end - shift)
        for cue in sync_report.cues_of(transcript)
    )
    source = sync_snap.SnapSource(
        source_id=asset_id,
        transcript_revision=revision,
        pcm_sha256=str(pcm["pcm_sha256"]),
        vad_key=sync_report.vad_key(project),
        audio_start=shift,
        transcript_duration=source_transcript.rational(transcript["duration"]),
    )
    plan = sync_snap.propose(
        cues, speech, _offset(report), source=source, settings=SYNC_SNAP_SETTINGS
    )
    return tuple(
        timing_truth.TruthLine(
            item.cue_id, item.text, plan.on_common(item.new_start), plan.on_common(item.new_end)
        )
        for item in plan.proposals
    )


def word_lines_of(
    workspace: Path, asset_id: str, transcript: Mapping[str, Any]
) -> tuple[timing_truth.TruthLine, ...] | str:
    """줄 경계를 1단계 기록의 단어 경계로 옮겨 본 값(공통 시계) — 잴 수 없으면 그 까닭."""
    stage1 = sync_report.stage1_audio(
        workspace, asset_id, str(transcript.get("source_sha256") or "")
    )
    if stage1 is None or stage1.absent:
        return "1단계 기록 없음"
    if not stage1.word_edges:
        return "단어 시각 없음"
    origin = stage1.origin - stage1.presentation_origin
    edges = tuple((origin + start, origin + end) for start, end in stage1.word_edges)
    return timing_truth.word_lines(
        timing_truth.lines_of(transcript), edges, reach=SYNC_SNAP_SETTINGS.reach_seconds
    )


def _video(
    workspace: Path, asset_id: str, transcript: Mapping[str, Any], marks: Sequence[Any]
) -> dict[str, Any]:
    """영상 하나 — 정답 처지 · 자막 짝 · 제안 짝 · 단어 경계 짝(없으면 까닭)."""
    lines = timing_truth.lines_of(transcript)
    states = timing_truth.judge(marks, lines)
    proposed = proposal_lines(workspace, asset_id, transcript)
    worded = word_lines_of(workspace, asset_id, transcript)
    return {
        "states": states,
        SUBTITLE: timing_truth.pairs(states, lines),
        PROPOSAL: () if isinstance(proposed, str) else timing_truth.pairs(states, proposed),
        "proposal_note": proposed if isinstance(proposed, str) else None,
        WORD: () if isinstance(worded, str) else timing_truth.pairs(states, worded),
        "word_note": worded if isinstance(worded, str) else None,
    }


def collect(workspace: Path) -> dict[str, Any]:
    """작업공간 하나의 보고서 문서 — 아무것도 쓰지 않는다."""
    connection = _connect(workspace)
    try:
        transcripts = _latest_transcripts(connection)
        names = _source_names(connection)
        everything = timing_truth.marks_on(connection)
    finally:
        connection.close()
    by_asset: dict[str, list[Any]] = {}
    for entry in everything:
        by_asset.setdefault(entry.asset_id, []).append(entry)
    videos = {
        asset_id: _video(workspace, asset_id, transcripts[asset_id], marks)
        for asset_id, marks in sorted(by_asset.items(), key=lambda item: names.get(item[0], ""))
        if asset_id in transcripts
    }
    orphaned = sum(
        len(marks) for asset_id, marks in by_asset.items() if asset_id not in transcripts
    )
    return _document(videos, names, orphaned)


def _document(
    videos: Mapping[str, Mapping[str, Any]], names: Mapping[str, str], orphaned: int
) -> dict[str, Any]:
    subtitle = [pair for video in videos.values() for pair in video[SUBTITLE]]
    proposal = [pair for video in videos.values() for pair in video[PROPOSAL]]
    matched = [pair for video in videos.values() if video[PROPOSAL] for pair in video[SUBTITLE]]
    word = [pair for video in videos.values() for pair in video[WORD]]
    return {
        "hit_seconds": str(TIMING_TRUTH_SETTINGS.hit_seconds),
        "videos": [
            _video_row(asset_id, names.get(asset_id, asset_id), video)
            for asset_id, video in videos.items()
        ],
        "overall": {
            SUBTITLE: _metrics(subtitle),
            MATCHED: _metrics(matched),
            PROPOSAL: _metrics(proposal),
            WORD: _metrics(word),
        },
        "strata": {
            stratum: {
                SUBTITLE: _metrics(_only(subtitle, stratum)),
                MATCHED: _metrics(_only(matched, stratum)),
                PROPOSAL: _metrics(_only(proposal, stratum)),
                WORD: _metrics(_only(word, stratum)),
            }
            for stratum in timing_truth.STRATA
        },
        "orphaned_marks": orphaned,
    }


def _video_row(asset_id: str, name: str, video: Mapping[str, Any]) -> dict[str, Any]:
    states = video["states"]
    return {
        "asset_id": asset_id,
        "source_name": name,
        "marks": len(states),
        "stale": sum(1 for state in states if state.stale is not None),
        SUBTITLE: _metrics(video[SUBTITLE]),
        PROPOSAL: _metrics(video[PROPOSAL]),
        "proposal_note": video["proposal_note"],
        WORD: _metrics(video[WORD]),
        "word_note": video["word_note"],
    }


def _only(found: Iterable[Any], stratum: str) -> list[Any]:
    return [pair for pair in found if pair.stratum == stratum]


def _metrics(found: Iterable[Any]) -> dict[str, Any]:
    values = timing_truth.metrics(found)
    return {
        "starts": values.starts,
        "ends": values.ends,
        **{
            name: None if getattr(values, name) is None else str(getattr(values, name))
            for name in ("start_mae", "end_mae", "start_hit", "end_hit", "end_cut")
        },
    }


# ── 인쇄 ─────────────────────────────────────────────────────────────────────


def _seconds(value: str | None) -> str:
    return "    -" if value is None else f"{float(Fraction(value)):5.3f}"


def _share(value: str | None) -> str:
    return "    -" if value is None else f"{float(Fraction(value)) * 100:4.0f}%"


def _line(label: str, values: Mapping[str, Any]) -> str:
    return (
        f"{label:<22} 시작 {values['starts']:>3}줄 MAE {_seconds(values['start_mae'])}초"
        f" 적중 {_share(values['start_hit'])} │ 끝 {values['ends']:>3}줄"
        f" MAE {_seconds(values['end_mae'])}초 적중 {_share(values['end_hit'])}"
        f" 자름 {_share(values['end_cut'])}"
    )


def render(document: Mapping[str, Any]) -> list[str]:
    hit = float(Fraction(document["hit_seconds"])) * 1000
    rows = [f"시각 정답 보고서 — 적중 폭 ±{hit:.0f}ms"]
    for video in document["videos"]:
        note = f" (제안: {video['proposal_note']})" if video["proposal_note"] else ""
        note += f" (단어: {video['word_note']})" if video["word_note"] else ""
        rows.append(
            f"{video['source_name']} — 정답 {video['marks']}개 · 낡음 {video['stale']}개{note}"
        )
        rows.append(_line("  자막", video[SUBTITLE]))
        rows.append(_line("  제안", video[PROPOSAL]))
        rows.append(_line("  단어 경계", video[WORD]))
    rows.append("전체")
    rows.append(_line("  자막", document["overall"][SUBTITLE]))
    rows.append(_line("  자막(제안과 같은 표본)", document["overall"][MATCHED]))
    rows.append(_line("  제안", document["overall"][PROPOSAL]))
    rows.append(_line("  단어 경계", document["overall"][WORD]))
    for stratum, values in document["strata"].items():
        rows.append(f"층 {stratum}")
        rows.append(_line("  자막", values[SUBTITLE]))
        rows.append(_line("  제안", values[PROPOSAL]))
        rows.append(_line("  단어 경계", values[WORD]))
    if document["orphaned_marks"]:
        rows.append(f"원본 자막이 없는 원본의 정답 {document['orphaned_marks']}개 — 세지 않음")
    return rows


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--json", action="store_true", help="보고서 문서를 JSON으로 인쇄")
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
