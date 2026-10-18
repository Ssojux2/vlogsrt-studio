"""W-25b — 시각 정답: 사람이 찍은 참 시작·끝은 자막과 따로 산다(D67).

유료 호출은 없다. 시험이 못 박는 것:

- 기준: 층별 줄 수·적중 폭의 값역, 적중 폭은 원본 평가의 ±200ms(`Config.start_tol`)와 정확히 같다.
- 층: 청크마다 그 안의 첫 줄(앞 층이 이긴다) · 「못 찾음」 줄 · PCM 시계의 정수 초 · 보통.
  권하는 줄은 층마다 고르게 퍼뜨려 고르고 두 번 불러도 같다. 모자란 층을 다른 층으로 채우지
  않는다.
- 표: 찍기는 같은 줄·경계를 바꿔 쓰고, 지우기는 하나만, 읽기는 표를 만들지 않는다. 잘못된 값은
  아무것도 쓰지 않는다.
- 낡음: 새 리비전에 그 아이디가 없거나 글자가 다르면 낡았다(다시 듣기 뒤 같은 아이디가 다른 말을
  가리키는 꼴). 시각만 바뀐 줄(시각 맞춤 적용)의 정답은 그대로이고 새 시각과 짝짓는다.
- 지표: 손으로 잰 예(경계 값 포함) · 비면 None.
- 서비스: 찍어도 원본 자막 리비전·시각 맞춤 제안 sha가 그대로다. 낡은 화면·없는 줄·범위 밖은
  거절한다. 층은 찍을 때의 것을 적는다. 화면 값은 권하는 층·찍은 정답·낡음·사라진 줄 수다.
- 보고서: `mode=ro`로만 읽고(작업공간 바이트 그대로), 자막과 메모리 안 시각 맞춤 제안을 같은
  정답으로 잰다.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import sqlite3
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from tests._sync_fakes import BURSTS, energy_factory, numpy_or_fake
from tests.raw.test_sync_snap_apply import snap_workspace
from vlogsrt.config import Config
from vlogsrt.project import config as project_config
from vlogsrt.project.raw import service as raw
from vlogsrt.project.raw import source_transcript, sync_report, timing_truth
from vlogsrt.project.raw.models import RawWorkspaceError
from vlogsrt.project.raw.workspace import DB_NAME

enc = source_transcript.encoded
F = Fraction
needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe가 없는 환경",
)
REPORT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "timing_truth_report.py"


def _line(cue_id: str, start: str, end: str, text: str = "말") -> timing_truth.TruthLine:
    return timing_truth.TruthLine(cue_id, text, F(start), F(end))


# ── 기준 ─────────────────────────────────────────────────────────────────────


def test_the_hit_window_is_the_evaluation_start_tolerance() -> None:
    settings = project_config.TIMING_TRUTH_SETTINGS
    assert settings.hit_seconds == F(str(Config().start_tol)) == F(1, 5)
    assert project_config.timing_truth_problems(settings) == []
    total = (
        settings.chunk_first_lines
        + settings.no_speech_lines
        + settings.whole_second_lines
        + settings.ordinary_lines
    )
    assert total == 5  # 영상당 약 다섯 줄


@pytest.mark.parametrize(
    "changes",
    [
        {"chunk_first_lines": -1},
        {"ordinary_lines": True},
        {"no_speech_lines": 1.5},
        {"hit_seconds": F(0)},
        {"hit_seconds": 0.2},
    ],
)
def test_bad_settings_are_named(changes: dict[str, Any]) -> None:
    bad = replace(project_config.TIMING_TRUTH_SETTINGS, **changes)
    problems = project_config.timing_truth_problems(bad)
    assert len(problems) == 1 and next(iter(changes)) in problems[0]


# ── 층 ───────────────────────────────────────────────────────────────────────

ORIGIN = F(1, 2)  # PCM 표본 0의 공통 시계 위치
STRATA_LINES = (
    _line("a", "1/2", "3"),  # 청크 0의 첫 줄 — PCM 0초라 정수 초이기도 하지만 앞 층이 이긴다
    _line("b", "21/5", "11/2"),  # 끝이 PCM 5초
    _line("c", "7", "83/10"),  # 「못 찾음」
    _line("d", "103/10", "111/10"),
    _line("g", "12", "13"),  # 공통 시계로는 정수지만 PCM 시계로는 11.5 · 12.5 — 보통
    _line("e", "361/2", "1823/10"),  # 청크 1(PCM 180초)의 첫 줄
    _line("f", "181", "1822/10"),
)


def _strata() -> dict[str, str]:
    return timing_truth.stratify(
        STRATA_LINES,
        chunk_starts=(ORIGIN, ORIGIN + 180),
        audio_start=ORIGIN,
        no_speech=frozenset({"c", "e"}),
    )


def test_each_line_gets_exactly_one_stratum_in_priority_order() -> None:
    assert _strata() == {
        "a": timing_truth.STRATUM_CHUNK_FIRST,
        "b": timing_truth.STRATUM_WHOLE_SECOND,
        "c": timing_truth.STRATUM_NO_SPEECH,
        "d": timing_truth.STRATUM_ORDINARY,
        "g": timing_truth.STRATUM_ORDINARY,
        "e": timing_truth.STRATUM_CHUNK_FIRST,  # 「못 찾음」이어도 청크 첫 줄이 이긴다
        "f": timing_truth.STRATUM_ORDINARY,
    }


def test_without_a_stage_one_record_there_is_no_chunk_stratum() -> None:
    strata = timing_truth.stratify(
        STRATA_LINES, chunk_starts=(), audio_start=F(0), no_speech=frozenset()
    )
    assert timing_truth.STRATUM_CHUNK_FIRST not in strata.values()
    assert strata["g"] == timing_truth.STRATUM_WHOLE_SECOND  # 원점 0이면 공통 시계가 곧 PCM 시계


def test_the_chunk_first_line_is_the_earliest_start_inside_the_chunk() -> None:
    lines = (_line("late", "5", "6"), _line("early", "2", "3"), _line("before", "1/2", "4"))
    strata = timing_truth.stratify(
        lines, chunk_starts=(F(1),), audio_start=F(0), no_speech=frozenset()
    )
    assert strata["early"] == timing_truth.STRATUM_CHUNK_FIRST
    assert strata["late"] != timing_truth.STRATUM_CHUNK_FIRST
    assert strata["before"] != timing_truth.STRATUM_CHUNK_FIRST  # 청크 앞에서 시작한 줄


def test_suggestions_spread_across_each_stratum_and_are_deterministic() -> None:
    lines = tuple(_line(f"o{index}", f"{index}1/10", f"{index}3/10") for index in range(10))
    strata = {line.cue_id: timing_truth.STRATUM_ORDINARY for line in lines}
    settings = replace(project_config.TIMING_TRUTH_SETTINGS, ordinary_lines=3)
    first = timing_truth.suggest(lines, strata, settings)
    assert first == timing_truth.suggest(lines, strata, settings)
    assert list(first) == ["o1", "o5", "o8"]  # 열 칸을 셋으로 나눈 가운데 자리


def test_a_short_stratum_is_not_filled_from_another() -> None:
    picked = timing_truth.suggest(STRATA_LINES, _strata())
    assert picked == {
        "a": timing_truth.STRATUM_CHUNK_FIRST,
        "e": timing_truth.STRATUM_CHUNK_FIRST,
        "c": timing_truth.STRATUM_NO_SPEECH,
        "b": timing_truth.STRATUM_WHOLE_SECOND,
        "g": timing_truth.STRATUM_ORDINARY,  # 보통 셋(d·g·f) 가운데 가운데 자리
    }
    sparse = {"a": timing_truth.STRATUM_ORDINARY}
    assert timing_truth.suggest(STRATA_LINES[:1], sparse) == sparse


# ── 표 ───────────────────────────────────────────────────────────────────────


def _rows() -> list[dict[str, Any]]:
    return [
        {"utterance_id": "a", "text": "첫째", "start": enc(F(1)), "end": enc(F(2))},
        {"utterance_id": "b", "text": "둘째", "start": enc(F(5)), "end": enc(F(6))},
    ]


@pytest.fixture
def plain(tmp_path: Path) -> tuple[Any, dict[str, Any]]:
    """미디어 없는 작업공간 하나에 심은 원본 자막."""
    service = raw.RawWorkspaceService()
    root = tmp_path / "plain"
    service.create_workspace(root, "정답")
    workspace = service._workspace(root)
    document = source_transcript.seed(workspace, "asset_x", _rows(), F(10), "sha")
    return workspace, document


def _table_exists(workspace: Any) -> bool:
    with workspace.connect() as db:
        found = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (timing_truth.TABLE,)
        ).fetchone()
    return found is not None


def _mark(workspace: Any, document: dict[str, Any], cue_id: str, edge: str, at: F) -> Any:
    line = next(item for item in timing_truth.lines_of(document) if item.cue_id == cue_id)
    return timing_truth.mark(
        workspace,
        "asset_x",
        line,
        edge,
        at,
        transcript_revision=document["revision"],
        stratum=timing_truth.STRATUM_ORDINARY,
    )


def test_reading_never_creates_the_table(plain: tuple[Any, dict[str, Any]]) -> None:
    workspace, _document = plain
    assert timing_truth.marks(workspace, "asset_x") == ()
    assert timing_truth.clear(workspace, "asset_x", "a", timing_truth.EDGE_START) is False
    assert not _table_exists(workspace)


def test_mark_replaces_the_same_edge_and_clear_removes_one(
    plain: tuple[Any, dict[str, Any]],
) -> None:
    workspace, document = plain
    _mark(workspace, document, "a", timing_truth.EDGE_START, F(11, 10))
    _mark(workspace, document, "a", timing_truth.EDGE_END, F(21, 10))
    again = _mark(workspace, document, "a", timing_truth.EDGE_START, F(23, 20))
    stored = timing_truth.marks(workspace, "asset_x")
    assert [(m.utterance_id, m.edge, m.seconds) for m in stored] == [
        ("a", "end", F(21, 10)),
        ("a", "start", F(23, 20)),  # 바꿔 썼다 — 줄·경계마다 하나
    ]
    assert stored[1] == again and again.line_text == "첫째"
    assert again.transcript_revision == document["revision"]
    assert timing_truth.clear(workspace, "asset_x", "a", timing_truth.EDGE_END) is True
    assert timing_truth.clear(workspace, "asset_x", "a", timing_truth.EDGE_END) is False
    assert [m.edge for m in timing_truth.marks(workspace, "asset_x")] == ["start"]
    assert timing_truth.marks(workspace, "other") == ()


@pytest.mark.parametrize(
    ("edge", "at", "stratum"),
    [("middle", F(1), "ordinary"), ("start", F(-1), "ordinary"), ("start", 1.5, "ordinary"),
     ("start", F(1), "loud")],
)  # fmt: skip
def test_bad_marks_write_nothing(
    plain: tuple[Any, dict[str, Any]], edge: str, at: Any, stratum: str
) -> None:
    workspace, document = plain
    line = timing_truth.lines_of(document)[0]
    with pytest.raises(RawWorkspaceError):
        timing_truth.mark(
            workspace, "asset_x", line, edge, at, transcript_revision="r", stratum=stratum
        )
    assert not _table_exists(workspace)


def test_marking_leaves_the_transcript_bytes_alone(plain: tuple[Any, dict[str, Any]]) -> None:
    workspace, document = plain
    before = _transcript_rows(workspace)
    _mark(workspace, document, "b", timing_truth.EDGE_END, F(63, 10))
    assert _transcript_rows(workspace) == before


def _transcript_rows(workspace: Any) -> list[tuple[Any, ...]]:
    with workspace.connect() as db:
        return db.execute("SELECT * FROM source_transcripts ORDER BY sequence").fetchall()


# ── 낡음 ─────────────────────────────────────────────────────────────────────


def test_a_new_revision_with_other_words_at_the_same_id_makes_the_mark_stale(
    plain: tuple[Any, dict[str, Any]],
) -> None:
    """다시 듣기 꼴 — 줄 아이디는 차례 번호라 같은 아이디가 다른 말을 가리킨다."""
    workspace, document = plain
    _mark(workspace, document, "a", timing_truth.EDGE_START, F(11, 10))
    _mark(workspace, document, "b", timing_truth.EDGE_END, F(63, 10))
    relistened = [
        {"utterance_id": "a", "text": "전혀 다른 말", "start": enc(F(1)), "end": enc(F(2))}
    ]
    newer = source_transcript.seed(workspace, "asset_x", relistened, F(10), "sha")
    assert newer["revision"] != document["revision"]
    states = timing_truth.judge(
        timing_truth.marks(workspace, "asset_x"), timing_truth.lines_of(newer)
    )
    assert {(s.mark.utterance_id, s.stale) for s in states} == {
        ("a", timing_truth.STALE_TEXT_CHANGED),
        ("b", timing_truth.STALE_LINE_MISSING),
    }
    assert timing_truth.pairs(states, timing_truth.lines_of(newer)) == ()


def test_a_line_that_only_moved_keeps_its_mark_and_pairs_with_the_new_time(
    plain: tuple[Any, dict[str, Any]],
) -> None:
    """시각 맞춤 적용 꼴 — 아이디·글자가 그대로면 정답도 그대로다(그 전후를 견주려는 정답)."""
    workspace, document = plain
    _mark(workspace, document, "a", timing_truth.EDGE_START, F(11, 10))
    moved = [dict(row) for row in _rows()]
    moved[0]["start"] = enc(F(6, 5))
    lines = timing_truth.lines_of({"utterances": moved})
    (state,) = timing_truth.judge(timing_truth.marks(workspace, "asset_x"), lines)
    assert state.stale is None
    (pair,) = timing_truth.pairs([state], lines)
    assert (pair.truth, pair.measured, pair.error) == (F(11, 10), F(6, 5), F(1, 10))


# ── 지표 ─────────────────────────────────────────────────────────────────────


def _pair(edge: str, error: str, stratum: str = "ordinary") -> timing_truth.TruthPair:
    return timing_truth.TruthPair("s", "c", stratum, edge, F(10), F(10) + F(error))


def test_metrics_on_a_hand_computed_example() -> None:
    found = [
        _pair("start", "1/10"),
        _pair("start", "-3/10"),
        _pair("start", "1/5"),  # 폭 경계 — 적중이다
        _pair("end", "-1/10"),
        _pair("end", "1/2"),
    ]
    values = timing_truth.metrics(found)
    assert values == timing_truth.TruthMetrics(
        starts=3,
        ends=2,
        start_mae=F(1, 5),  # (0.1 + 0.3 + 0.2) / 3
        end_mae=F(3, 10),  # (0.1 + 0.5) / 2
        start_hit=F(2, 3),
        end_hit=F(1, 2),
        end_cut=F(1, 2),  # 끝이 말보다 먼저 끝난 하나
    )


def test_metrics_without_pairs_are_none() -> None:
    assert timing_truth.metrics([]) == timing_truth.TruthMetrics(0, 0, None, None, None, None, None)
    only_starts = timing_truth.metrics([_pair("start", "0")])
    assert only_starts.end_mae is None and only_starts.end_cut is None
    assert only_starts.start_hit == 1


# ── 1단계 기록의 청크 시작 ───────────────────────────────────────────────────


def test_stage_one_records_carry_the_chunk_starts(tmp_path: Path) -> None:
    folder = tmp_path / "analysis" / "anal_1" / "000001_src"
    folder.mkdir(parents=True)
    body = {
        "source_sha256": "f" * 64,
        "created_at": "2026-10-18T00:00:00+00:00",
        "audio": {
            "pcm_sha256": "0" * 64,
            "audio_stream_index": 1,
            "sample_rate": 16000,
            "channels": 1,
            "sample_count": 16000 * 200,
            "pcm_sample0_file_time": {"num": 0, "den": 1},
        },
        "asr": {
            "chunks": [
                {"chunk_index": 0, "sample_start": 0, "sample_end": 2880000, "failure": ""},
                {"chunk_index": 1, "sample_start": 2880000, "sample_end": 3200000, "failure": ""},
            ]
        },
    }
    (folder / "asr_phase.json").write_text(json.dumps(body), encoding="utf-8")
    stage1 = sync_report.stage1_audio(tmp_path, "src", "f" * 64)
    assert stage1 is not None and stage1.chunk_starts == (F(0), F(180))


# ── 서비스 ───────────────────────────────────────────────────────────────────


@pytest.fixture
def snapped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Path, str]:
    numpy_or_fake(monkeypatch)
    monkeypatch.setattr(sync_report, "default_session_factory", energy_factory)
    return snap_workspace(tmp_path)


def _truth(service: Any, root: Path, source_id: str, cue_id: str, edge: str, at: F) -> Any:
    transcript = service.source_transcript(root, source_id)
    return service.mark_timing_truth(
        root,
        source_id,
        utterance_id=cue_id,
        edge=edge,
        seconds=at,
        transcript_revision=transcript["revision"],
    )


@needs_ffmpeg
def test_marking_through_the_service_changes_no_subtitle_and_no_proposal(
    snapped: tuple[Any, Path, str],
) -> None:
    service, root, source_id = snapped
    workspace = service._workspace(root)
    before_rows = _transcript_rows(workspace)
    before_sha = service.apply_snap_preview(root, source_id).sha256
    start, end = (F(value) for value in BURSTS[5])
    entry = _truth(service, root, source_id, "c05", "start", start)
    _truth(service, root, source_id, "c05", "end", end)
    assert entry.stratum in timing_truth.STRATA and entry.line_text == "말 5"
    assert _transcript_rows(workspace) == before_rows  # 리비전도 줄도 그대로
    assert service.apply_snap_preview(root, source_id).sha256 == before_sha
    stored = timing_truth.marks(workspace, source_id)
    assert {(m.edge, m.seconds) for m in stored} == {("start", start), ("end", end)}


@needs_ffmpeg
def test_the_service_refuses_stale_screens_missing_lines_and_out_of_range(
    snapped: tuple[Any, Path, str],
) -> None:
    service, root, source_id = snapped
    revision = service.source_transcript(root, source_id)["revision"]
    cases = [
        ({"utterance_id": "c05", "transcript_revision": "old"}, raw.TRUTH_REVISION_CHANGED),
        ({"utterance_id": "없음", "transcript_revision": revision}, raw.TRUTH_LINE_MISSING),
        (
            {"utterance_id": "c05", "transcript_revision": revision, "seconds": F(41)},
            raw.TRUTH_OUT_OF_RANGE,
        ),
    ]
    for arguments, message in cases:
        call = {"edge": "start", "seconds": F(14), **arguments}
        with pytest.raises(RawWorkspaceError, match=message):
            service.mark_timing_truth(root, source_id, **call)
    assert timing_truth.marks(service._workspace(root), source_id) == ()


@needs_ffmpeg
def test_the_view_suggests_strata_and_shows_marks_and_stale_ones(
    snapped: tuple[Any, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    service, root, source_id = snapped
    transcript = service.source_transcript(root, source_id)
    stage1 = sync_report.Stage1Audio(
        folder=root,
        created_at="x",
        absent=False,
        origin=F(1, 2),  # 공통 시계 = PCM 시계 + 0.5초
        chunk_starts=(F(0), F(20)),
    )
    monkeypatch.setattr(sync_report, "stage1_audio", lambda *_args: stage1)
    _truth(service, root, source_id, "c05", "end", F(154, 10))
    view = service.sync_check_view(root, source_id)
    notes, missing = service.timing_truth_view(root, source_id, transcript, view)
    for low in (F(1, 2), F(41, 2)):  # 청크 시작(공통 시계) = PCM 0초·20초 + 0.5초
        first = min(
            (row for row in transcript["utterances"]
             if source_transcript.rational(row["start"]) >= low),
            key=lambda row: source_transcript.rational(row["start"]),
        )  # fmt: skip
        assert notes[first["utterance_id"]]["truthStratum"] == timing_truth.STRATUM_CHUNK_FIRST
    assert "truthStratum" not in notes.get("z0", {})  # 0.2초 — 청크 0(0.5초) 앞이다
    assert notes["c05"]["truth"] == {"end": {"at": "77/5"}} and missing == 0
    picked = [note["truthStratum"] for note in notes.values() if "truthStratum" in note]
    assert len(picked) <= 5
    # 그 줄의 글자를 고치면 정답은 그 줄에 낡음으로, 줄이 사라지면 수로만 남는다.
    service.edit_source_transcript(
        root, source_id, transcript["revision"], action="edit", utterance_id="c05", text="고침"
    )
    edited = service.source_transcript(root, source_id)
    notes, missing = service.timing_truth_view(root, source_id, edited, None)
    assert notes["c05"]["truth"] == {"end": {"at": "77/5", "stale": True}} and missing == 0


# ── 보고서 ───────────────────────────────────────────────────────────────────


def _report_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("timing_truth_report", REPORT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _digest(root: Path) -> dict[str, str]:
    """작업공간 바이트 — 공유 메모리 색인(`-shm`)은 읽는 연결도 고치므로 뺀다."""
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.name.endswith("-shm")
    }


@needs_ffmpeg
def test_the_report_reads_only_and_scores_subtitles_and_proposals(
    snapped: tuple[Any, Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    service, root, source_id = snapped
    for cue_id, (start, end) in (("c05", BURSTS[5]), ("c09", BURSTS[9])):
        _truth(service, root, source_id, cue_id, "start", F(start))
        _truth(service, root, source_id, cue_id, "end", F(end))
    plan, _measured = service._apply_snap_plan(root, source_id, strict=False)
    proposals = {item.cue_id: item for item in plan.proposals}
    with sqlite3.connect(root / DB_NAME) as db:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
    before = _digest(root)
    report = _report_module()
    capsys.readouterr()  # 찍을 때의 로그는 보고서가 아니다
    assert report.main([str(root), "--json"]) == 0
    document = json.loads(capsys.readouterr().out)
    assert _digest(root) == before  # 아무것도 쓰지 않았다
    (video,) = document["videos"]
    assert (video["marks"], video["stale"], video["proposal_note"]) == (4, 0, None)
    subtitle = video["subtitle"]
    assert (subtitle["start_mae"], subtitle["end_mae"]) == ("3/2", "3/2")  # 1.5초 이른 자막
    assert (subtitle["start_hit"], subtitle["end_hit"], subtitle["end_cut"]) == ("0", "0", "1")
    errors = [
        plan.on_common(getattr(proposals[cue_id], f"new_{edge}")) - F(BURSTS[index][slot])
        for cue_id, index in (("c05", 5), ("c09", 9))
        for slot, edge in enumerate(("start", "end"))
    ]
    starts, ends = errors[0::2], errors[1::2]
    assert video["proposal"]["start_mae"] == str(sum(map(abs, starts)) / 2)
    assert video["proposal"]["end_mae"] == str(sum(map(abs, ends)) / 2)
    assert video["proposal"]["end_cut"] == str(F(sum(1 for e in ends if e < 0), 2))
    assert document["overall"]["subtitle_matched"] == document["overall"]["subtitle"]
    assert report.render(document)[0].startswith("시각 정답 보고서 — 적중 폭 ±200ms")


@needs_ffmpeg
def test_the_report_names_why_a_video_has_no_proposal(
    snapped: tuple[Any, Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    service, root, source_id = snapped
    _truth(service, root, source_id, "c05", "start", F(BURSTS[5][0]))
    shutil.rmtree(root / sync_report.SYNC_CACHE / "reports")
    report = _report_module()
    capsys.readouterr()
    assert report.main([str(root)]) == 0
    printed = capsys.readouterr().out
    assert "(제안: 지금 리비전의 확인 보고서 없음)" in printed
    document = report.collect(root)
    assert document["overall"]["proposal"]["starts"] == 0
    assert document["overall"]["subtitle"]["starts"] == 1
    assert document["overall"]["subtitle_matched"]["starts"] == 0


# ── 그리기 범위의 연결 함께 쓰기 — 정답 표가 DB 열기를 늘리지 않는다 ─────────────


def _count_opens(monkeypatch: pytest.MonkeyPatch, workspace: Any) -> list[int]:
    opened = [0]
    original = type(workspace).connect

    def counted(self: Any) -> Any:
        opened[0] += 1
        return original(self)

    monkeypatch.setattr(type(workspace), "connect", counted)
    return opened


def test_in_a_read_scope_the_truth_read_reuses_the_open_connection(
    plain: tuple[Any, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    from vlogsrt.project.raw import asr_epoch, read_scope

    workspace, document = plain
    _mark(workspace, document, "a", timing_truth.EDGE_START, F(11, 10))
    opened = _count_opens(monkeypatch, workspace)
    with read_scope.open():
        asr_epoch.state(workspace, "asset_x")  # 그리기가 이미 읽는 작은 표
        assert opened[0] == 1
        assert len(timing_truth.marks(workspace, "asset_x")) == 1
        assert timing_truth.marks(workspace, "other") == ()
        assert opened[0] == 1  # 정답 표는 같은 연결로 — 열기 0번 더
    with read_scope.open():
        assert len(timing_truth.marks(workspace, "asset_x")) == 1
        asr_epoch.state(workspace, "asset_x")
    assert opened[0] == 2  # 범위마다 한 번
    assert len(timing_truth.marks(workspace, "asset_x")) == 1  # 범위 밖은 예전 그대로 매번
    assert opened[0] == 3


def test_shared_resources_close_with_the_scope_and_after_forget() -> None:
    import contextlib
    import threading

    from vlogsrt.project.raw import read_scope

    events: list[str] = []

    @contextlib.contextmanager
    def opener() -> Any:
        events.append("open")
        yield object()
        events.append("close")

    with read_scope.open() as scope:
        with read_scope.shared("k", opener) as first, read_scope.shared("k", opener) as again:
            assert first is again
        seen: list[Any] = []
        worker = threading.Thread(target=lambda: seen.append(scope.held("k", opener)))
        worker.start()
        worker.join()
        assert seen == [None]  # 다른 스레드는 함께 쓰지 않는다 — 제 연결을 연다
        scope.forget()
        assert events == ["open", "close"]
        with read_scope.shared("k", opener):
            pass
    assert events == ["open", "close", "open", "close"]
    with read_scope.shared("k", opener):
        pass  # 범위 밖은 매번 여닫는다
    assert events[-2:] == ["open", "close"] and len(events) == 6
