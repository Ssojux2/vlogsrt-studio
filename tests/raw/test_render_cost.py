"""작업대 그리기의 비용과 바이트를 잠근다 — 원본 18개 합성 작업공간(W-23, D63).

네 가지를 못 박는다.

1. **바이트.** 그리기 값(정규화)이 기준 파일과 한 글자도 다르지 않다 — 다음 세션(B: 읽기 범위
   기억, C: 타이머 합치기)이 읽기를 줄여도 화면은 그대로여야 한다. 기준은 W-23 이전 코드(1fc3b44)로
   만든 값이고(`scripts/bench_workbench.py --golden`), W-24 세션 G가 모델 인코딩만 바꿔 다시 잠갔다
   (뿌리 속성 → 숨은 첫 칸의 글자 — 두 기준을 풀어 견주면 모델 JSON과 나머지 HTML이 같다).
   W-24 세션 I가 영상 카드 18장에 카드 그림(`cardThumb`) 칸 하나씩을 더해 다시 잠갔다 — 두 기준을
   풀어 견주면 바뀐 것은 그 18칸뿐이고(모두 그 카드의 대표 컷 `thumb.src`와 같다 — 이 작업공간은
   썸네일이 다 있다), 나머지 모델과 HTML은 바이트째 같다. W-25 세션 a가 등급판 표지를
   `raw-tier/v4` → `v5`로만 바꿔 다시 잠갔다(43곳 — 그 밖은 바이트째 같다, D65). W-46b가 재생기
   껍데기에 시작·끝 표시 줄과 추천 구간 칸을, 원본마다 「내보낼 부분」(`kept`) 칸 하나를 더해 다시
   잠갔다 — 두 기준을 풀어 견주면 모델은 그 18칸 말고 같고, HTML은 껍데기의 그 두 조각 말고
   바이트째 같다(D100 §7b).
2. **부른 수의 천장.** 그리기 한 번이 DB 열기·원본 자막 읽기·검토 머리 읽기를 몇 번 하는지 —
   지금 값이 상한이다. 줄이는 것은 좋고 늘면 실패다.
3. **타이머 틱.** 바뀐 것이 없는 틱은 그리지 않는다. 작업 진행 수만 바뀐 틱은 작업대에 진행이
   보이는 종류(썸네일·미리보기)일 때만 그리고(W-24 세션 G), 정리 고리(`start_upkeep`·
   `start_sync_upkeep`)는 돌지 않는다. 작업 상태가 바뀐 틱은 한 번씩 돈다. 영상 분석 중에는 등급
   진행 문서가 바뀐 틱만 그린다(예전에는 그 단계 내내 틱마다 정리 고리째 그렸다).
4. **동작(W-24 세션 G).** 단추 길(정리 고리 + 그리기)과 영상 카드 누르기의 부른 수 천장, 선택 분석
   상태 100개 · 옛 작업 700줄에서도 작업 목록을 통째로 읽지 않는다, 1초 상태 틱은 실사용 줄을 한
   번 센다, 「자동 제외 전체 복원」은 옛 걸음(353c544)과 바이트째 같다.

유료 호출 0회. 작업공간은 시험 한 벌에 한 번 만들고(약 80초) 시험마다 베낀다.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from collections import Counter
from pathlib import Path

import pytest

pytest.importorskip(
    "gradio",
    reason="웹 UI extra(gradio)가 없다 — ui extra를 설치하면 이 묶음이 활성화된다",
    exc_type=ImportError,
)

from tests.raw import render_cost_fixture as fixture  # noqa: E402
from tests.raw.restore_reference import head_document, reference_restore  # noqa: E402
from tests.raw.test_proposal_queue import _with_tiers, needs_ffmpeg  # noqa: E402
from tests.raw.test_review_workspace import _wait_terminal  # noqa: E402
from tests.raw.test_visual_workbench_ui import _model_of  # noqa: E402
from ui import workspace_service  # noqa: E402
from ui.workspace_service import RawWorkspaceUi  # noqa: E402
from vlogsrt.project.raw import read_scope  # noqa: E402
from vlogsrt.project.raw import service as raw_service  # noqa: E402
from vlogsrt.project.raw.analysis_store import AnalysisStore  # noqa: E402
from vlogsrt.project.raw.review_workspace import ReviewWorkspaceFacade  # noqa: E402
from vlogsrt.project.raw.workspace import DB_NAME, RawWorkspace  # noqa: E402

GOLDEN = Path(__file__).resolve().parents[1] / "fixtures/synthetic/workbench_render_18_sources.html"

RENDER_CEILING: dict[str, int] = {
    "open_db": 30,
    "source_transcript.load": 18,
    "sync_report.gather_source": 18,
    "ReviewWorkspaceFacade()": 1,
    "load_head": 1,
    "analysis_overview": 1,
    "visual_workbench_render": 1,
    "start_upkeep": 0,
    "start_sync_upkeep": 0,
}
"""그리기 한 번의 실제 읽기 수 — 상한이다: 줄면 이 값을 낮춘다.

세션 A(1fc3b44)의 실측은 DB 열기 1602 · 원본 자막 540 · 시각 확인 재료 18 · 검토 파사드 3 · 검토
머리 3 · 분석 개요 19였다. 세션 B는 그리기 한 번을 읽기 범위(`read_scope`) 하나로 묶어 같은 읽기를
한 번만 한다 — 파사드는 작업공간마다, 검토 머리는 기본 계획마다, 원본 자막은 지금 리비전 전부를 한
번에, 분석 개요는 리비전마다 한 번이다.
"""


BUTTON_CEILING: dict[str, int] = {
    "open_db": 60,
    "source_transcript.load": 1,
    "sync_report.gather_source": 18,
    "sync_report.stage1_audio": 18,
    "ReviewWorkspaceFacade()": 1,
    "load_head": 1,
    "analysis_overview": 1,
    "jobs.list": 1,
    "visual_workbench_render": 1,
    "start_upkeep": 1,
    "start_sync_upkeep": 1,
}
"""단추 하나(`_capcut_result` — 정리 고리 둘 + 그리기)의 실제 읽기 수 — 상한이다(W-24 세션 G).

세션 G 앞(353c544)의 실측은 DB 열기 262 · 원본 자막 55 · 시각 확인 재료 54 · 1단계 기록 54 · 검토
파사드 2 · 검토 머리 2였다 — 정리 고리가 읽기 범위 밖에서 그리기를 한 번 더 한 셈이다. 이제 고리의
할 일 재기가 그리기와 한 범위를 쓴다(걸 일이 없으면 기억을 버리지 않는다).
"""

CLICK_CEILING: dict[str, int] = {
    "jobs.list": 1,
    "jobs.by_ids": 1,
    "source_transcript.load": 1,
    "sync_report.gather_source": 18,
    "sync_report.stage1_audio": 18,
    "visual_workbench_render": 1,
    "start_upkeep": 1,
    "start_sync_upkeep": 1,
}
"""영상 카드 누르기 한 번(선택 분석 상태 100개 · 옛 작업 700줄) — 상한이다(W-24 세션 G).

세션 G 앞의 실측은 작업 목록(1000줄 창) 101번 · 원본 자막 55 · 시각 확인 재료 54였다 — 상태마다
목록을 통째로 읽었다. 누르기가 건 미리보기 준비 작업이 끝날 때까지 센다(그 작업의 읽기도 든다).
DB 열기·파사드·검토 머리는 그 작업의 몫이 섞여 천장에 넣지 않는다.
"""


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg required")
    root = fixture.build_workspace(tmp_path_factory.mktemp("render_cost"))
    service = raw_service.RawWorkspaceService()
    fixture.settle(service, root)
    _checkpoint(root)
    return root


def _checkpoint(root: Path) -> None:
    """베끼기 전에 WAL을 DB 파일로 접는다 — 옆 파일 없이 DB 하나로 온전하다(D63)."""
    connection = sqlite3.connect(root / DB_NAME)
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
    finally:
        connection.close()


def _opened(
    built: Path, tmp_path: Path, *, inflated: bool = False
) -> tuple[RawWorkspaceUi, Path, str]:
    root = tmp_path / "project"
    shutil.copytree(built, root, ignore=shutil.ignore_patterns(f"{DB_NAME}-*"))
    if inflated:
        fixture.inflate(root)  # 실데이터 크기 — 선택 분석 상태 100개 · 끝난 옛 작업 700줄
    server = RawWorkspaceUi(state_path=tmp_path / "ui-state.json")
    signature = server.open_workspace_view(str(root))[1]
    _with_tiers(server)
    fixture.settle(server.raw, root)
    return server, root, signature


def _counted(monkeypatch: pytest.MonkeyPatch) -> Counter[str]:
    return fixture.install_counters(monkeypatch.setattr)


def _during(counts: Counter[str], call: object) -> Counter[str]:
    before = Counter(counts)
    call()  # type: ignore[operator]
    return counts - before


@needs_ffmpeg
def test_the_rendered_bytes_match_the_locked_render(built: Path, tmp_path: Path) -> None:
    server, root, _signature = _opened(built, tmp_path)
    first = fixture.normalize("\n".join(server.visual_workbench_render(root)), tmp_path)
    second = fixture.normalize("\n".join(server.visual_workbench_render(root)), tmp_path)
    assert first == second  # 두 번 그려도 같다 — 잠글 수 있는 값이다
    assert first == GOLDEN.read_text(encoding="utf-8")


@needs_ffmpeg
def test_one_render_stays_under_the_measured_call_ceiling(
    built: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server, root, _signature = _opened(built, tmp_path)
    counts = _counted(monkeypatch)
    calls = _during(counts, lambda: server.visual_workbench_render(root))
    over = {name: calls[name] for name, ceiling in RENDER_CEILING.items() if calls[name] > ceiling}
    assert over == {}, f"그리기 한 번의 부른 수가 늘었다: {dict(calls)}"
    assert calls["visual_workbench_render"] == 1
    assert read_scope.current() is None  # 범위는 그리기 한 번만 산다


@needs_ffmpeg
def test_an_unchanged_tick_neither_renders_nor_runs_upkeep(
    built: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server, _root, signature = _opened(built, tmp_path)
    counts = _counted(monkeypatch)
    tab = fixture.TickTab(server, signature)
    tab.tick()  # 열쇠를 채운다
    calls = _during(counts, tab.tick)
    assert calls["visual_workbench_render"] == 0
    assert calls["start_upkeep"] == calls["start_sync_upkeep"] == 0


UNCHANGED_TICK_CEILING: dict[str, int] = {
    "open_db": 3,
    "source_transcript.load": 0,
    "jobs.list": 0,
    "analysis_usage_summary": 0,
    "analysis_overview": 0,
    "visual_workbench_render": 0,
}
"""바뀐 것 없는 2초 틱 하나의 읽기 — 상한이다(W-46b, D100 §7b). 앞(W-46a)의 실측은 DB 열기 12 ·
원본 자막 본문 전부 1 · 작업 목록 1 · 실사용 요약 1이었다. 이제 서명 확인 둘과 도장 하나(원본 자막
차례 · 최근·활성 작업 · 선택 상태 CRC를 한 연결로)뿐이고, 상태 줄 글은 짓지 않는다."""


@needs_ffmpeg
def test_an_unchanged_tick_reads_one_stamp_and_no_transcripts(
    built: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server, root, signature = _opened(built, tmp_path)
    summary = server.raw.analysis_overview(root)
    assert summary is not None
    assets = tuple(asset.asset_id for asset in server.raw.list_source_assets(root))
    server._quick = {"root": root, "token": "tick", "asset_ids": assets,
                     "revision": summary.revision.revision_id}  # fmt: skip
    counts = _counted(monkeypatch)
    tab = fixture.TickTab(server, signature)
    tab.tick()
    stamps = Counter[str]()
    original = server.raw.poll_stamp

    def stamp(*args: object, **kwargs: object) -> object:
        stamps["poll"] += 1
        return original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(server.raw, "poll_stamp", stamp)
    calls = _during(counts, tab.tick)
    assert _over(calls, UNCHANGED_TICK_CEILING) == {}, dict(calls)
    assert stamps["poll"] == 1
    # 원본 자막을 하나 고치면 도장의 차례가 바뀌어 그 틱이 그린다.
    asset = assets[0]
    document = server.raw.source_transcript(root, asset)
    assert document is not None
    row = document["utterances"][0]
    server.raw.edit_source_transcript(
        root, asset, document["revision"], action="edit", utterance_id=row["utterance_id"],
        text=str(row["text"]) + "!", start=None, end=None, split_at=None, right_text=None,
    )  # fmt: skip
    redrawn = _during(counts, tab.tick)
    assert redrawn["visual_workbench_render"] == 1


@needs_ffmpeg
@pytest.mark.parametrize(
    ("kind", "renders"),
    [
        (fixture.SteppedJob.KIND, 0),  # 작업대에 진행이 안 보이는 종류 — 1초 상태 칸이 보인다
        (raw_service.VISUAL_PREPARE_JOB_KIND, 1),  # 썸네일 하나 = 진행 한 걸음 — 작업대가 바뀐다
        ("source_preview", 1),
    ],
)
def test_a_progress_only_tick_renders_only_visible_progress_and_never_upkeep(
    built: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, renders: int
) -> None:
    """진행 수만 바뀐 틱 — 그 진행이 작업대에 보이는 종류만 그린다(W-24 세션 G).

    예전엔 어느 작업이든 진행이 한 칸 움직이면 2초마다 작업대 전체를 그려 보냈다.
    """
    server, root, signature = _opened(built, tmp_path)
    counts = _counted(monkeypatch)
    stepped = fixture.SteppedJob(server.raw, root, kind)
    tab = fixture.TickTab(server, signature)
    try:
        stepped.step(1)
        tab.tick()  # 작업이 돌기 시작한 틱 — 상태가 바뀌었다
        for value in (2, 3):
            stepped.step(value)
            calls = _during(counts, tab.tick)
            assert calls["visual_workbench_render"] == renders, (kind, dict(calls))
            assert calls["start_upkeep"] == calls["start_sync_upkeep"] == 0
            # 고급 패널 머리 맞춤과 그리기가 한 범위를 함께 쓴다 — 파사드·검토 머리는 한 번이다.
            assert calls["ReviewWorkspaceFacade()"] == calls["load_head"] == renders, dict(calls)
            assert calls["source_transcript.load"] <= RENDER_CEILING["source_transcript.load"]
    finally:
        stepped.finish()


@needs_ffmpeg
def test_a_job_status_change_runs_upkeep_once(
    built: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server, root, signature = _opened(built, tmp_path)
    counts = _counted(monkeypatch)
    stepped = fixture.SteppedJob(server.raw, root)
    tab = fixture.TickTab(server, signature)
    stepped.step(1)
    tab.tick()
    stepped.finish()  # 돌던 작업이 끝났다 — 정리 고리의 재료가 바뀌었을 수 있는 순간이다
    calls = _during(counts, tab.tick)
    assert calls["visual_workbench_render"] == 1
    assert calls["start_upkeep"] == calls["start_sync_upkeep"] == 1
    again = _during(counts, tab.tick)
    assert again["start_upkeep"] == again["visual_workbench_render"] == 0


@needs_ffmpeg
def test_during_semantic_analysis_only_a_changed_progress_document_redraws(
    built: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """영상 분석 중 — 창 판정이 진행 문서에 쌓일 때만 다시 그린다(그 단계 내내 그리지 않는다)."""
    server, root, signature = _opened(built, tmp_path)
    summary = server.raw.analysis_overview(root)
    assert summary is not None
    revision = summary.revision.revision_id
    asset_id = server.raw.list_source_assets(root)[0].asset_id
    server._quick = {
        "root": root,
        "token": "semantic-probe",
        "stage": workspace_service.SEMANTIC_STAGE,
        "revision": revision,
        "asset_ids": (asset_id,),
    }
    counts = _counted(monkeypatch)
    tab = fixture.TickTab(server, signature)
    tab.tick()
    still = _during(counts, tab.tick)
    assert still["visual_workbench_render"] == 0
    row = AnalysisStore(RawWorkspace.open(root)).source_result(revision, asset_id)
    assert row is not None
    directory, _document = raw_service._tier_paths(root.resolve(), row)
    before = server.raw.analysis_tier_stamp(root, revision, (asset_id,))
    progress = directory / "suitability_progress.json"
    progress.write_text(json.dumps({"analysis_revision_id": "다른 리비전"}), encoding="utf-8")
    assert server.raw.analysis_tier_stamp(root, revision, (asset_id,)) != before
    redrawn = _during(counts, tab.tick)
    assert redrawn["visual_workbench_render"] == 1
    assert redrawn["start_upkeep"] == redrawn["start_sync_upkeep"] == 0


# ── 동작의 비용(W-24 세션 G) — 단추 길 · 영상 카드 · 1초 상태 틱 · 복원 ──────────────────


def _over(calls: Counter[str], ceiling: dict[str, int]) -> dict[str, int]:
    return {name: calls[name] for name, top in ceiling.items() if calls[name] > top}


@needs_ffmpeg
def test_a_button_reads_its_upkeep_and_render_in_one_scope(
    built: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """단추 하나 — 정리 고리 둘의 할 일 재기와 그리기가 같은 읽기를 한 번만 한다."""
    server, root, _signature = _opened(built, tmp_path)
    counts = _counted(monkeypatch)
    calls = _during(counts, lambda: server._capcut_result(root, "", upkeep=True))
    assert _over(calls, BUTTON_CEILING) == {}, f"단추 하나의 부른 수가 늘었다: {dict(calls)}"
    assert calls["start_upkeep"] == calls["start_sync_upkeep"] == 1
    assert read_scope.current() is None  # 범위는 단추 한 번만 산다


@needs_ffmpeg
def test_upkeep_that_starts_a_job_drops_the_scope_before_the_render(
    built: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """고리가 작업을 걸면(안내가 돌아오면) 그리기는 기억을 버린 범위에서 처음부터 읽는다."""
    server, root, _signature = _opened(built, tmp_path)
    seen: list[int] = []

    def started(_root: Path) -> str:
        scope = read_scope.current()
        assert scope is not None and len(scope) > 0  # 고리는 범위 안에서 읽었다
        scope.memo(("started-marker",), lambda: 1)
        return "정리 작업을 걸었다"

    real_render = RawWorkspaceUi.visual_workbench_render

    def render(self: RawWorkspaceUi, *args: object, **kwargs: object) -> tuple[str, str]:
        scope = read_scope.current()
        assert scope is not None
        seen.append(scope.memo(("started-marker",), lambda: 2))
        return real_render(self, *args, **kwargs)  # type: ignore[arg-type]

    real_pending = server.raw._upkeep_pending

    def pending_then_start(root_arg: Path) -> str | None:
        real_pending(root_arg)
        return started(root_arg)

    monkeypatch.setattr(server.raw, "start_upkeep", pending_then_start)
    monkeypatch.setattr(RawWorkspaceUi, "visual_workbench_render", render)
    message, _shell, _body = server._capcut_result(root, "처음", upkeep=True)
    assert message.startswith("처음") and "정리 작업을 걸었다" in message
    assert seen == [2]  # 고리가 남긴 기억은 그리기 앞에서 버려졌다


@needs_ffmpeg
def test_a_video_click_finds_selection_jobs_by_id(
    built: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """영상 카드 — 상태 100개 · 옛 작업 700줄에서도 작업 목록을 상태마다 읽지 않는다."""
    server, root, signature = _opened(built, tmp_path, inflated=True)
    sources = [asset.asset_id for asset in server.raw.list_source_assets(root)]
    counts = _counted(monkeypatch)

    def click() -> None:
        server.quick_source_view(signature, sources[1])
        for job in server.raw.all_active_jobs(root):
            assert _wait_terminal(server.raw, root, job.job_id).status == "succeeded"

    calls = _during(counts, click)
    assert _over(calls, CLICK_CEILING) == {}, f"영상 누르기의 부른 수가 늘었다: {dict(calls)}"
    assert server._quick["view_source_id"] == sources[1]
    assert server._quick["stage"] == "추천 구간 준비 완료"  # 부풀린 상태 — 끝난 작업을 찾았다


@needs_ffmpeg
def test_the_status_tick_counts_usage_once(
    built: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """1초 상태 틱 — 상태 줄과 실사용 칸이 같은 실사용 줄을 쓴다(한 범위에서 한 번 센다)."""
    server, root, signature = _opened(built, tmp_path)
    summary = server.raw.analysis_overview(root)
    assert summary is not None
    server._quick = {"root": root, "token": "usage", "revision": summary.revision.revision_id}
    counts = _counted(monkeypatch)
    calls = _during(
        counts, lambda: server.status_tick(signature, None, None, None, None, *(None,) * 5)
    )
    assert calls["analysis_usage_summary"] == 1, dict(calls)
    assert server.raw.analysis_usage_line(root, summary.revision.revision_id) == ""  # 유료 0


@needs_ffmpeg
def test_restoring_auto_exclusions_matches_the_old_walk_byte_for_byte(
    built: Path, tmp_path: Path
) -> None:
    """「자동 제외 전체 복원」 — 사슬을 한 질의로 걸어도 새 머리 문서가 옛 걸음과 바이트째 같다."""
    root = tmp_path / "a"
    shutil.copytree(built, root, ignore=shutil.ignore_patterns(f"{DB_NAME}-*"))
    facade = ReviewWorkspaceFacade(root)
    head = facade.load_head().revision
    auto = [s for s in head.segments if s.notes.startswith("자동 제외")]
    plain = [s for s in head.segments if not s.notes.startswith("자동 제외")]
    assert len(auto) >= 2 and len(plain) >= 2
    base = head.base_plan_id
    facade.set_selection(base, auto[0].segment_id, "excluded")  # 사람이 고른 자동 제외 — 남는다
    for index in range(12):  # 사람 선택의 긴 사슬
        facade.set_selection(base, plain[0].segment_id, ("excluded", "included")[index % 2])
    facade.set_notes(base, plain[1].segment_id, "현장 메모")
    facade.set_selection(base, plain[1].segment_id, "excluded")
    copy = tmp_path / "b"
    _checkpoint(root)
    shutil.copytree(root, copy, ignore=shutil.ignore_patterns(f"{DB_NAME}-*"))
    restored = facade.restore_automatic()
    reference = reference_restore(ReviewWorkspaceFacade(copy))
    assert head_document(facade) == head_document(ReviewWorkspaceFacade(copy))
    assert restored.segments == reference.segments
    kept = next(s for s in restored.segments if s.segment_id == auto[0].segment_id)
    assert kept.selection == "excluded"
    assert all(
        s.selection == "included"
        for s in restored.segments
        if s.segment_id in {a.segment_id for a in auto[1:]}
    )


# ── 화면과 동작 일치(W-24 세션 D, D64) — 같은 18개 작업공간 ──────────────────────────


@needs_ffmpeg
def test_every_video_gets_a_card_with_its_real_thumbnail(built: Path, tmp_path: Path) -> None:
    """영상 18개 — 컷은 한 쪽(24)을 넘지만 카드는 모두이고, 쪽 밖 영상도 진짜 썸네일 상태다."""
    server, root, _signature = _opened(built, tmp_path)
    model = _model_of(server.visual_workbench_render(root)[1])
    assert model["pageCount"] > 1 and len(model["cards"]) == raw_service.VISUAL_PAGE_SIZE
    cards = model["mediaCards"]
    assert [card["sourceId"] for card in cards] == list(model["sources"])
    assert len(cards) == fixture.SOURCE_COUNT
    paged = {card["sourceId"] for card in model["cards"]}
    assert any(card["sourceId"] not in paged for card in cards)  # 이 쪽에 컷이 없는 영상
    assert all(card["thumb"]["state"] == "ready" and card["thumb"]["src"] for card in cards)
    for card in cards:  # 대표 컷은 그 영상의 컷이다(누르면 그 컷이 골라진다)
        cuts = model["sources"][card["sourceId"]]["rail"]["intervals"]
        assert card["id"] in [cut["id"] for cut in cuts]


@needs_ffmpeg
def test_the_selected_video_beyond_the_first_page_ships_its_cuts(
    built: Path, tmp_path: Path
) -> None:
    """첫 쪽(24컷) 밖 영상을 고르면 그 영상의 컷(썸네일 포함)만 따로 싣는다 — 다른 영상은 없다."""
    server, root, _signature = _opened(built, tmp_path)
    overview = server.raw.visual_review_query(root, include_all_sources=True)
    last = overview.representatives[-1]
    model = _model_of(
        server.visual_workbench_render(root, preferred_selected_id=last.segment_id)[1]
    )
    assert model["selectedSourceId"] == last.source_id
    rail = [cut["id"] for cut in model["sources"][last.source_id]["rail"]["intervals"]]
    paged = {card["id"] for card in model["cards"]}
    assert not paged & set(rail)  # 쪽 목록에는 이 영상의 컷이 없다
    view = model["viewCards"]
    assert [card["id"] for card in view] == rail
    assert {card["sourceId"] for card in view} == {last.source_id}
    assert all(card["thumb"]["state"] == "ready" and card["thumb"]["src"] for card in view)


@needs_ffmpeg
def test_two_tabs_keep_their_own_render_keys(
    built: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """한 탭이 그렸다고 다른 탭이 건너뛰지 않는다 — 열쇠는 탭마다다(서버 하나의 값이 아니다)."""
    server, root, signature = _opened(built, tmp_path)
    counts = _counted(monkeypatch)
    first, second = fixture.TickTab(server, signature), fixture.TickTab(server, signature)
    for tab in (first, second):  # 처음 본 탭은 각자 그린다
        assert _during(counts, tab.tick)["visual_workbench_render"] == 1
    for tab in (first, second):
        assert _during(counts, tab.tick)["visual_workbench_render"] == 0
    stepped = fixture.SteppedJob(server.raw, root)
    try:
        stepped.step(1)
        assert _during(counts, first.tick)["visual_workbench_render"] == 1
        assert _during(counts, second.tick)["visual_workbench_render"] == 1  # 예전엔 건너뛰었다
        assert _during(counts, second.tick)["visual_workbench_render"] == 0
    finally:
        stepped.finish()


@needs_ffmpeg
def test_redraws_without_a_filter_leave_the_tab_filter_alone(built: Path, tmp_path: Path) -> None:
    """틱·명령 줄 단추의 다시 그리기는 필터 칸이 비었다 — 실행본이 탭의 필터를 그대로 둔다."""
    server, root, signature = _opened(built, tmp_path)
    ticked = fixture.TickTab(server, signature).tick()
    assert _model_of(ticked[2])["filter"] is None
    assert _model_of(server.capcut_refresh_view(signature)[2])["filter"] is None
    shown = _model_of(server.visual_workbench_render(root)[1])
    fns = {fn.__name__: fn for fn in server._visual_server_functions()}
    reply = fns["vl_refresh"](
        {"epoch": shown["epoch"], "signature": signature, "page": 1, "filter": "excluded"}
    )
    assert reply["ok"] and _model_of(reply["html"])["filter"] == "excluded"


@needs_ffmpeg
def test_reprepare_fills_the_selected_video_beyond_the_first_page(
    built: Path, tmp_path: Path
) -> None:
    """「미리보기 다시 준비」는 고른 영상의 썸네일을 채운다 — 첫 쪽 밖이어도, 다른 영상은 그대로."""
    server, root, signature = _opened(built, tmp_path)
    overview = server.raw.visual_review_query(root, include_all_sources=True)
    first, last = overview.representatives[0], overview.representatives[-1]
    assert last.segment_id not in {card.segment_id for card in overview.segments}  # 쪽 밖
    for card in (first, last):
        assert card.thumb_relative_path is not None
        (root / card.thumb_relative_path).unlink()
    shown = _model_of(server.visual_workbench_render(root)[1])
    fns = {fn.__name__: fn for fn in server._visual_server_functions()}
    reply = fns["vl_prepare"](
        {
            "epoch": shown["epoch"],
            "token": shown["token"],
            "signature": signature,
            "page": 1,
            "selectedId": last.segment_id,
            "sourceId": last.source_id,
        }
    )
    assert reply["ok"] and "이 영상의 미리보기 준비 작업" in reply["message"]
    for job in server.raw.all_active_jobs(root):
        assert _wait_terminal(server.raw, root, job.job_id).status == "succeeded"
    assert (root / str(last.thumb_relative_path)).is_file()  # 고른 영상 — 채웠다
    assert not (root / str(first.thumb_relative_path)).is_file()  # 첫 쪽 영상 — 그대로
