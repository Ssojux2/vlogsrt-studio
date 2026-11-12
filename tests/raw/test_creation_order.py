"""영상은 언제나 만든 시각 순이다(W-13, D53) — 실제 미디어로 끝까지 잰다.

사용자 지시(2026-10-09): "make it so that videos are always sorted by the time they were created."
실측: FCP는 원본을 다시 감싸며 컨테이너 `creation_time`을 **가져온 시각**으로 덮는다 — 그래서
먼저 찍은 0008을 나중에 가져오면 전체 순서 맨 뒤(18번)에 섰다.

합성 원본 셋을 촬영의 **역순**으로 가져온다(파일 번호도 촬영 역순이라 이름순과 헷갈리지 않는다).
D53 이전 작업공간을 흉내 내려고 촬영 시각 칸을 FCP 가져온 시각으로 덮고 근거 판 표지를 지운 뒤,
재생 분석(유료 호출 0회)으로 검토 머리를 세우고 사람 결정을 싣는다. 그다음 앱 열기의 정리 고리가
원본 태그를 다시 읽어(실제 ffprobe) 순서를 촬영 시각 순으로 맞추고, 사람 결정을 그대로 들고
머리를 옮기는지 본다.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.raw.test_review_workspace import _wait_terminal, _write_replay_fixtures
from vlogsrt import log, preprocess
from vlogsrt.project.raw.review_workspace import ReviewWorkspaceFacade
from vlogsrt.project.raw.segment_review import (
    AUTO_EXCLUDED_UNREVIEWED_TAG,
    chronological,
    unreviewed_auto_exclusions,
)
from vlogsrt.project.raw.service import UPKEEP_JOB_KIND, RawWorkspaceError, RawWorkspaceService
from vlogsrt.project.raw.workspace import CAPTURE_BASIS_KEY, CAPTURE_TIME_BASIS, DB_NAME

# D104 — whisper 응답 모양의 재생으로 재는 시험이다. 새로 듣는 엔진을 whisper로 둔다.
pytestmark = pytest.mark.usefixtures("whisper_for_new_hearings")

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")

# 파일 번호는 촬영 역순이다 — 이름순·가져온 순서·FCP 가져온 시각이 전부 촬영과 거꾸로다.
SHOT = {
    "DJI_0003.mov": "2026-08-11T00:00:00+00:00",  # 아침 — 가장 먼저 찍었다
    "DJI_0002.mov": "2026-08-11T03:00:00+00:00",
    "DJI_0001.mov": "2026-08-11T09:00:00+00:00",  # 저녁
}
INGEST = {
    "DJI_0001.mov": "2026-08-14T10:38:00+00:00",
    "DJI_0002.mov": "2026-08-14T10:38:10+00:00",
    "DJI_0003.mov": "2026-08-14T10:38:20+00:00",
}
# 길이를 같게 둔다 — 재생 Luna 픽스처는 창 하나의 판정을 모든 원본에 내므로 증거 프레임
# 격자가 같아야 받아들여진다. 내용은 creation_time 태그가 달라 해시가 갈린다.
SECONDS = {"DJI_0001.mov": 15, "DJI_0002.mov": 15, "DJI_0003.mov": 15}
SPEECH = {
    "DJI_0001.mov": [
        {"start": 0.5, "end": 7, "text": "저녁 노을이에요."},
        {"start": 7.5, "end": 14.5, "text": "하늘이 붉어요."},
    ],
    # 3→11초가 자막 없는 긴 공백 — Luna '사용 가능'이면 빨강 제안(미검토 자동 제외)이 된다.
    "DJI_0002.mov": [
        {"start": 0.5, "end": 1.5, "text": "안녕하세요."},
        {"start": 2, "end": 3, "text": "계곡에 왔어요."},
        {"start": 11, "end": 12, "text": "물이 맑네요."},
        {"start": 12.5, "end": 13.5, "text": "들어가 볼게요."},
    ],
    "DJI_0003.mov": [
        {"start": 0.5, "end": 7, "text": "아침 산책이에요."},
        {"start": 7.5, "end": 14.5, "text": "바람이 시원해요."},
    ],
}
SHOT_ORDER = ["DJI_0003.mov", "DJI_0002.mov", "DJI_0001.mov"]
OLD_ORDER = ["DJI_0001.mov", "DJI_0002.mov", "DJI_0003.mov"]
FIRST_FRAME = "frame_00000023"  # 15초·25fps 창 하나의 첫 표본(test_tier_review_gate와 같은 격자)


def _encode(path: Path, seconds: int, shot: str) -> None:
    result = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", f"testsrc2=size=160x90:rate=25:duration={seconds}",
            "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono",
            "-t", str(seconds), "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
            "-x264-params", "colorprim=bt709:transfer=bt709:colormatrix=bt709",
            "-c:a", "pcm_s16le", "-metadata", f"creation_time={shot}", str(path),
        ],
        capture_output=True,
    )  # fmt: skip
    assert result.returncode == 0, result.stderr


def _names(service: RawWorkspaceService, root: Path) -> dict[str, str]:
    return {asset.asset_id: asset.source_name for asset in service.list_source_assets(root)}


def _ids(service: RawWorkspaceService, root: Path) -> dict[str, str]:
    return {name: asset_id for asset_id, name in _names(service, root).items()}


def _order_names(service: RawWorkspaceService, root: Path) -> list[str]:
    names = _names(service, root)
    return [names[e.asset_id] for e in service.latest_global_source_order(root).entries]


def _head_names(service: RawWorkspaceService, root: Path, head) -> list[str]:
    names = _names(service, root)
    return list(dict.fromkeys(names[s.source_id] for s in chronological(head)))


def _decisions(service: RawWorkspaceService, root: Path, head) -> dict:
    """사람·기계 결정의 순서 무관 지문 — (원본 이름, 경계) → (선택, 메모, 덮기, 미검토)."""
    names = _names(service, root)
    return {
        (names[s.source_id], s.start_pts, s.end_pts): (
            s.selection,
            s.notes,
            s.override_recommendation,
            AUTO_EXCLUDED_UNREVIEWED_TAG in s.visual_tags,
        )
        for s in head.segments
    }


def _make_pre_d53(service: RawWorkspaceService, root: Path) -> None:
    """D53 이전 작업공간 — 촬영 시각 칸이 FCP 가져온 시각이고 근거 판 표지가 없다."""
    service._workspace(root).record_capture_times(
        {asset.asset_id: INGEST[asset.source_name] for asset in service.list_source_assets(root)}
    )
    with sqlite3.connect(root / DB_NAME) as connection:
        connection.execute("DELETE FROM meta WHERE key = ?", (CAPTURE_BASIS_KEY,))


def _analyze(service: RawWorkspaceService, root: Path) -> str:
    shas = {a.source_name: a.content_sha256 for a in service.list_source_assets(root)}
    _write_replay_fixtures(root, {shas[name]: SPEECH[name] for name in SPEECH})
    luna_path = root / "analysis/replay/luna-fixtures.json"
    fixtures = json.loads(luna_path.read_text())
    fixtures["windows"]["window_000000"].update(
        suitability="usable",
        suitability_reason="연결 장면으로 사용 가능",
        evidence_ids=[FIRST_FRAME],
    )
    luna_path.write_text(json.dumps(fixtures, ensure_ascii=False))
    handle = service.start_analysis(root, mode="replay")
    finished = _wait_terminal(service, root, handle.job_id)
    assert finished.status == "succeeded", finished.error
    return service.analysis_overview(root).revision.revision_id


def reverse_imported_project(
    tmp_path: Path, *, human: bool = True
) -> tuple[RawWorkspaceService, Path]:
    """촬영 역순으로 가져온 D53 이전 작업공간 — 옛 순서 위의 검토 머리에 사람 결정이 실렸다."""
    card = tmp_path / "card"
    card.mkdir()
    service = RawWorkspaceService()
    root = tmp_path / "project"
    service.create_workspace(root, "촬영 순서")
    for name in OLD_ORDER:  # 가져온 순서 = 촬영 역순
        _encode(card / name, SECONDS[name], SHOT[name])
        handle = service.import_selected_video(root, card / name)
        assert _wait_terminal(service, root, handle.job_id).status == "succeeded"
    _make_pre_d53(service, root)
    service.ensure_source_order(root)
    assert _order_names(service, root) == OLD_ORDER
    _analyze(service, root)
    finished = _wait_terminal(service, root, service.start_cleanup(root).job_id)
    assert finished.status == "succeeded", finished.error
    if human:
        _human_decisions(service, root)
    return service, root


def _human_decisions(service: RawWorkspaceService, root: Path) -> None:
    facade = ReviewWorkspaceFacade(root)
    head = facade.load_head().revision
    assert _head_names(service, root, head) == OLD_ORDER
    assert len(unreviewed_auto_exclusions(head)) == 1  # 0002의 자막 없는 긴 공백
    ids = _ids(service, root)
    by_source = {name: [s for s in head.segments if s.source_id == ids[name]] for name in ids}
    base = head.base_plan_id
    facade.set_notes(base, by_source["DJI_0003.mov"][0].segment_id, "아침 첫 장면 — 사람 메모")
    facade.set_override(base, by_source["DJI_0001.mov"][0].segment_id, "review_required")
    facade.set_selection(base, by_source["DJI_0002.mov"][-1].segment_id, "excluded")


def _analysis_revision_count(root: Path) -> int:
    with sqlite3.connect(root / DB_NAME) as connection:
        return len(connection.execute("SELECT 1 FROM analysis_revisions").fetchall())


def _wait_upkeep(service: RawWorkspaceService, root: Path):
    job = next(j for j in service.list_jobs(root) if j.kind == UPKEEP_JOB_KIND)
    return _wait_terminal(service, root, job.job_id)


@needs_ffmpeg
def test_reopen_backfills_shot_times_and_moves_the_head_without_losing_decisions(
    tmp_path: Path,
):
    """앱 열기 하나로 — 촬영 시각 되채움 → 촬영 시각 순 → 사람 결정을 든 머리 옮기기(D53)."""
    pytest.importorskip("gradio", reason="웹 UI extra(gradio)가 없다", exc_type=ImportError)
    from tests.raw.test_visual_workbench_ui import _model_of
    from ui.workspace_service import RawWorkspaceUi

    service, root = reverse_imported_project(tmp_path)
    before = ReviewWorkspaceFacade(root).load_head().revision
    decisions = _decisions(service, root, before)
    analyses = _analysis_revision_count(root)

    server = RawWorkspaceUi(state_path=tmp_path / "ui-state.json")  # 앱을 다시 연다
    server.open_workspace_view(str(root))
    finished = _wait_upkeep(server.raw, root)
    assert finished.status == "succeeded", finished.error

    raw = server.raw
    times = {a.source_name: a.capture_time for a in raw.list_source_assets(root)}
    assert times == SHOT  # 실제 ffprobe가 원본 태그에서 다시 읽었다
    assert raw._workspace(root).meta_value(CAPTURE_BASIS_KEY) == CAPTURE_TIME_BASIS
    assert _order_names(raw, root) == SHOT_ORDER
    head = ReviewWorkspaceFacade(root).load_head().revision
    plan = raw.segment_plan_revision(root, head.base_plan_id)
    assert plan.order_revision_id == raw.latest_global_source_order(root).revision_id
    assert head.base_plan_id != before.base_plan_id
    assert _head_names(raw, root, head) == SHOT_ORDER
    assert _decisions(raw, root, head) == decisions  # 선택·메모·덮기·미검토 표지 그대로
    assert [s.source_order for s in chronological(head)] == sorted(
        s.source_order for s in head.segments
    )
    # 화면 — 레일·원본 카드·조각 카드와 라이브러리 분석 줄이 모두 촬영 시각 순이다.
    names = _names(raw, root)
    model = _model_of(server.visual_workbench_render(root)[1])
    assert [names[sid] for sid in model["sources"]] == SHOT_ORDER
    assert [names[card["sourceId"]] for card in model["mediaCards"]] == SHOT_ORDER
    card_sources = [names[card["sourceId"]] for card in model["cards"]]
    assert list(dict.fromkeys(card_sources)) == SHOT_ORDER
    assert [names[sid] for sid in server._library_ids(root)] == SHOT_ORDER
    # 유료 호출 0회 — 새 분석 리비전도 분석 작업도 없다. 할 일이 끝났으니 다시 걸 것도 없다.
    assert _analysis_revision_count(root) == analyses
    jobs = len(raw.list_jobs(root))
    assert raw.start_upkeep(root) is None
    assert len(raw.list_jobs(root)) == jobs


@needs_ffmpeg
def test_export_after_reorder_follows_shot_time(tmp_path: Path):
    """Final Cut으로 보내는 전체 타임라인도 촬영 시각 순이다(유료 호출 없음, D53·D56).

    옛 길은 순서를 옮긴 머리의 조각 파일을 다시 뽑아(`materialize`) 확정 패키지로 냈다. 지금
    보내기는 원본을 그대로 참조해 조각을 뽑지 않는다 — 작업이 하나도 생기지 않는다.
    """
    service, root = reverse_imported_project(tmp_path)
    reopened = RawWorkspaceService()
    assert "촬영 시각 순" in (reopened.start_upkeep(root) or "")
    assert _wait_upkeep(reopened, root).status == "succeeded"
    head = ReviewWorkspaceFacade(root).confirm_automatic_exclusions()
    jobs = {job.job_id for job in reopened.list_jobs(root)}
    path = reopened.export_finalcut_timeline(root)
    report = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    assert [Path(row["path"]).name for row in report["sources"]] == SHOT_ORDER
    assert report["review_head"] == head.revision_id
    document = ET.parse(path).getroot()
    assets = {asset.get("id"): asset.get("name") for asset in document.iter("asset")}
    spine = document.find("./library/event/project/sequence/spine")
    assert spine is not None
    order = [f"{assets[clip.get('ref')]}.mov" for clip in spine]
    assert list(dict.fromkeys(order)) == SHOT_ORDER
    assert {job.job_id for job in reopened.list_jobs(root)} == jobs  # 다시 뽑지 않는다


@needs_ffmpeg
def test_pure_reorder_never_resets_the_head_in_preview_or_cleanup(tmp_path: Path):
    """순서만 바뀌면 미리보기 준비도 자동 정리도 머리를 다시 세우지 않는다 — 옮길 뿐이다(D53).

    예전 미리보기는 계획의 순서가 최신이 아니면 원본마다 한 장으로 다시 깔아 사람 결정을 잃었고,
    정리는 분석 당시 순서로 다시 세워 옛 순서로 되돌렸다.
    """
    service, root = reverse_imported_project(tmp_path)
    decisions = _decisions(service, root, ReviewWorkspaceFacade(root).load_head().revision)
    assert service.refresh_capture_times(root) == ()
    assert service.ensure_source_order(root) is True  # 순서만 바뀌었다(같은 원본 셋)

    preview = service.start_source_preview(root)
    assert _wait_terminal(service, root, preview.job_id).status == "succeeded"
    head = ReviewWorkspaceFacade(root).load_head().revision
    assert _head_names(service, root, head) == SHOT_ORDER
    assert _decisions(service, root, head) == decisions

    cleanup = _wait_terminal(service, root, service.start_cleanup(root).job_id)
    assert cleanup.status == "succeeded", cleanup.error
    after = ReviewWorkspaceFacade(root).load_head().revision
    assert _head_names(service, root, after) == SHOT_ORDER
    assert _decisions(service, root, after) == decisions
    plan = service.segment_plan_revision(root, after.base_plan_id)
    assert plan.order_revision_id == service.latest_global_source_order(root).revision_id


@needs_ffmpeg
def test_upkeep_failure_warns_runs_safe_steps_and_retries_once_per_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """원본 하나를 못 읽어도(볼륨이 빠졌다) 경고하고, 나머지 단계는 돌고, 같은 실행에선 다시
    걸지 않으며, 앱을 다시 열면 한 번 더 시도한다(D52 등급판 옮기기와 같은 규칙)."""
    service, root = reverse_imported_project(tmp_path)
    decisions = _decisions(service, root, ReviewWorkspaceFacade(root).load_head().revision)
    real_probe = preprocess.probe_creation_time
    warnings: list[str] = []
    monkeypatch.setattr(log, "warn", warnings.append)

    def unmounted(path: Path) -> preprocess.CreationTime:
        if path.name == "DJI_0003.mov":
            raise RuntimeError("ffprobe 실패 (코드 1)\nNo such file or directory")
        return real_probe(path)

    monkeypatch.setattr(preprocess, "probe_creation_time", unmounted)
    first = RawWorkspaceService()
    assert first.start_upkeep(root) is not None
    failed = _wait_upkeep(first, root)
    assert failed.status == "failed"
    assert "DJI_0003.mov" in (failed.error or "")
    assert any("DJI_0003.mov" in w and "No such file" in w for w in warnings)
    # 읽은 두 원본은 고쳐졌고, 그 시각으로 정렬·머리 옮기기는 이미 돌았다(0003은 가져온 시각).
    assert _order_names(first, root) == ["DJI_0002.mov", "DJI_0001.mov", "DJI_0003.mov"]
    head = ReviewWorkspaceFacade(root).load_head().revision
    assert _head_names(first, root, head) == _order_names(first, root)
    assert _decisions(first, root, head) == decisions
    jobs = len(first.list_jobs(root))
    for _ in range(3):  # 화면 갱신마다 불려도 같은 실행 안에서는 다시 걸지 않는다
        message = first.start_upkeep(root) or ""
        assert message.startswith("작업공간 정리 실패:") and "DJI_0003.mov" in message
    assert len(first.list_jobs(root)) == jobs

    monkeypatch.setattr(preprocess, "probe_creation_time", real_probe)  # 볼륨이 돌아왔다
    reopened = RawWorkspaceService()
    assert reopened.start_upkeep(root) is not None
    assert _wait_upkeep(reopened, root).status == "succeeded"
    assert _order_names(reopened, root) == SHOT_ORDER
    head = ReviewWorkspaceFacade(root).load_head().revision
    assert _head_names(reopened, root, head) == SHOT_ORDER
    assert _decisions(reopened, root, head) == decisions
    assert reopened.start_upkeep(root) is None


@needs_ffmpeg
def test_failed_tier_step_defers_head_reorder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """등급판 옮기기가 실패하면 머리 순서 맞춤은 미룬다 — 입양이 손대지 않은 옛 머리를 먼저
    고치면 다음 옮기기가 '등급만 다시 달기'로 내려앉는다(D52). 전체 순서 확정은 그래도 돈다."""
    from vlogsrt.project.raw import tiering

    service, root = reverse_imported_project(tmp_path, human=False)
    facade = ReviewWorkspaceFacade(root)
    with monkeypatch.context() as patch:  # D51 이전 모양의 손대지 않은 머리
        patch.setattr(tiering, "red_decisions", lambda report: [])
        patch.setattr(
            tiering, "segment_grade", lambda report, a, b: ("normal", ("Luna 평가: 옛 등급",))
        )
        legacy = facade.initialize_review(service.plan_segmentation_from_analysis(root).revision_id)
    assert facade.is_untouched(legacy) and not facade.has_tier_marker(legacy)
    warnings: list[str] = []
    monkeypatch.setattr(log, "warn", warnings.append)

    def broken(*args, **kwargs):
        raise RawWorkspaceError("원본 볼륨이 빠졌다")

    reopened = RawWorkspaceService()
    monkeypatch.setattr(reopened, "_migrate_unedited_head", broken)
    message = reopened.start_upkeep(root) or ""
    assert "촬영 시각 순" in message and "다시 세웁니다" in message
    failed = _wait_upkeep(reopened, root)
    assert failed.status == "failed"
    assert "등급판: 원본 볼륨이 빠졌다" in (failed.error or "")
    assert "검토 순서: 등급판 옮기기가 실패해" in (failed.error or "")
    assert any("등급판 실패" in w for w in warnings)
    assert _order_names(reopened, root) == SHOT_ORDER  # 안전한 단계는 돌았다
    head = facade.load_head().revision
    assert head == legacy  # 옛 머리는 손대지 않은 채 다음 시도를 기다린다
    assert facade.is_untouched(head)


@needs_ffmpeg
def test_cached_analysis_survives_a_pure_reorder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """끝난 분석은 순서만 바뀌었다고 낡지 않는다 — 다시 승인도 유료 호출도 없다(D53).

    분석 지문은 그때의 전체 순서 리비전을 묶는다. 예전엔 순서가 바뀌면 같은 영상이 '승인 대기'로
    돌아갔다(실측 작업공간의 분석 18개가 전부 그 길에 놓여 있었다).
    """
    pytest.importorskip("gradio", reason="웹 UI extra(gradio)가 없다", exc_type=ImportError)
    from tests.raw.test_browser_runtime import _make_media
    from tests.raw.test_selection_analysis import make_server, wait_until

    first, second = tmp_path / "DJI_0001.mov", tmp_path / "DJI_0002.mov"
    _make_media(first, audio=True, seconds="3")
    _make_media(second, audio=True, seconds="4")
    server, sender, client = make_server(tmp_path, monkeypatch, first)
    root = server.workspace_root()
    for source in (first, second):
        handle = server.raw.import_selected_video(root, source)
        assert _wait_terminal(server.raw, root, handle.job_id).status == "succeeded"
    server.raw.ensure_source_order(root)
    ids = _ids(server.raw, root)
    server._start_quick(root, (), asset_ids=(ids["DJI_0001.mov"],))
    wait_until(lambda: server._quick.get("stage") == "추천 구간 준비 완료")
    calls = (len(sender.calls), len(client.calls))
    revision = server.raw.cached_selection_analysis(root, (ids["DJI_0001.mov"],))
    assert revision is not None

    # 순서만 뒤집힌 새 전체 순서 리비전 — 같은 원본 묶음이다.
    server.raw.confirm_global_source_order(root, [ids["DJI_0002.mov"], ids["DJI_0001.mov"]])
    assert server.raw.cached_selection_analysis(root, (ids["DJI_0001.mov"],)) == revision
    server._start_quick(root, (), asset_ids=(ids["DJI_0001.mov"],))
    wait_until(lambda: server._quick.get("stage") == "추천 구간 준비 완료")
    assert (len(sender.calls), len(client.calls)) == calls  # 다시 승인·호출 없음
    assert server._quick.get("stage") == "추천 구간 준비 완료"


def _encode_apple_only(path: Path, apple_date: str) -> None:
    """애플 촬영 날짜만 있는 원본 — D53 이전 조사는 이 시각을 못 읽어 '미조사'로 남겼다."""
    result = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=25:duration=1",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-movflags", "use_metadata_tags",
            "-metadata", f"com.apple.quicktime.creationdate={apple_date}", str(path),
        ],
        capture_output=True,
    )  # fmt: skip
    assert result.returncode == 0, result.stderr


@needs_ffmpeg
def test_fcp_scan_checkboxes_follow_apple_shot_dates(tmp_path: Path):
    """FCP 원본 고르기 목록이 실제 조사(ffprobe)의 애플 촬영 날짜 순이다 — 이름순의 역순이어도.

    D53 이전 조사는 `creation_time`만 읽어 이 원본들의 시각을 몰랐고, 목록은 이름순이었다.
    """
    pytest.importorskip("gradio", reason="웹 UI extra(gradio)가 없다", exc_type=ImportError)
    from ui import workspace_service

    library = tmp_path / "촬영.fcpbundle"
    event = library / "2026-10-07" / "CurrentVersion.fcpevent"
    originals = event / "Original Media"
    originals.mkdir(parents=True)
    (event / "Transcoded Media" / "Proxy Media").mkdir(parents=True)
    shots = {
        "C001.mov": "2026-08-11T18:00:00+0900",
        "C002.mov": "2026-08-11T12:00:00+0900",
        "C003.mov": "2026-08-11T09:00:00+0900",
    }
    for name, apple_date in shots.items():
        _encode_apple_only(originals / name, apple_date)
    server = workspace_service.RawWorkspaceUi(state_path=tmp_path / "state.json")
    server.open_fcp_library_view(library, originals_only=False)
    checkboxes = server.capcut_fcp_panel_view()[0]
    labels = [Path(label).name for label, _value in checkboxes.choices]
    assert labels == ["C003.mov", "C002.mov", "C001.mov"]


def _row_names(markdown: str) -> list[str]:
    """표 줄에 처음 나온 원본 이름 순서."""
    names = [
        name
        for line in markdown.splitlines()
        if line.startswith("|")
        for name in SHOT
        if name in line
    ]
    return list(dict.fromkeys(names))


@needs_ffmpeg
def test_ingest_and_analysis_panels_list_videos_in_shot_order(tmp_path: Path):
    """'언제나'는 어디서나다 — 가져오기 표와 고급 분석 표도 촬영 시각 순이다(D53).

    가져온 배치·순번은 가져오기 기록 칸으로만 남는다. 분석 표는 분석 당시 순서가 아니라 지금
    순서로 줄을 세운다.
    """
    pytest.importorskip("gradio", reason="웹 UI extra(gradio)가 없다", exc_type=ImportError)
    from ui.workspace_service import RawWorkspaceUi

    service, root = reverse_imported_project(tmp_path)
    assert service.refresh_capture_times(root) == ()
    assert service.ensure_source_order(root) is True
    server = RawWorkspaceUi(state_path=tmp_path / "ui-state.json")
    server.open_workspace_view(str(root))
    assert _wait_upkeep(server.raw, root).status == "succeeded"
    ingest = server._ingest_state_markdown(root).split("**전체 원본 순서**")[0]
    assert "촬영 시각 순" in ingest and "가져오기 기록" in ingest
    assert _row_names(ingest) == SHOT_ORDER
    assert "import_000003 #1" in ingest  # 가져온 기록은 남는다(0003은 세 번째 배치)
    assert _row_names(server._analysis_state_markdown(root)) == SHOT_ORDER


def test_manual_global_order_box_is_gone(tmp_path: Path, local_socket: None):
    """D53 — '언제나 촬영 시각 순'이라 순서를 손으로 적는 칸이 없다. 확정 단추만 남는다."""
    pytest.importorskip("gradio", reason="웹 UI extra(gradio)가 없다", exc_type=ImportError)
    import gradio as gr

    from ui import workspace_service

    server = workspace_service.RawWorkspaceUi(state_path=tmp_path / "ui-state.json")
    app = server.build_blocks()
    textboxes = [str(b.label) for b in app.blocks.values() if isinstance(b, gr.Textbox)]
    assert not [label for label in textboxes if "전체 원본 순서" in label]
    buttons = [str(b.value) for b in app.blocks.values() if isinstance(b, gr.Button)]
    assert workspace_service.GLOBAL_ORDER_CONFIRM_LABEL in buttons
    confirm = [fn for fn in app.fns.values() if fn.fn == server.ingest_global_confirm_view]
    assert len(confirm) == 1
    assert [id(block) for block in confirm[0].inputs] == [id(server._signature_state)]


@needs_ffmpeg
def test_edit_during_reorder_is_never_dropped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """옮기기는 원본을 다시 해시한다(오래 걸린다) — 그 사이 사람이 고치면 옮기지 않고 미룬다.

    읽어 둔 머리로 입양하면 그 뒤의 편집은 옛 계획의 머리에만 남아 화면에서 사라진다.
    다음 시도는 바뀐 머리를 들고 옮긴다.
    """
    service, root = reverse_imported_project(tmp_path)
    assert service.refresh_capture_times(root) == ()
    assert service.ensure_source_order(root) is True
    warnings: list[str] = []
    monkeypatch.setattr(log, "warn", warnings.append)
    real_plan_from_review = ReviewWorkspaceFacade.plan_from_review
    edited: list[str] = []

    def plan_while_a_person_edits(self, base_plan_id=None, **kwargs):
        planned = real_plan_from_review(self, base_plan_id, **kwargs)
        if not edited:
            head = self.load_head(base_plan_id).revision
            segment = head.segments[0]
            self.set_notes(head.base_plan_id, segment.segment_id, "옮기는 동안 쓴 메모")
            edited.append(segment.segment_id)
        return planned

    monkeypatch.setattr(ReviewWorkspaceFacade, "plan_from_review", plan_while_a_person_edits)
    assert service.align_review_order(root) is False
    assert any("다음 갱신으로 미룬다" in w for w in warnings)
    head = ReviewWorkspaceFacade(root).load_head().revision
    assert _head_names(service, root, head) == OLD_ORDER  # 옮기지 않았다
    decisions = _decisions(service, root, head)
    assert "옮기는 동안 쓴 메모" in {notes for _sel, notes, _o, _u in decisions.values()}

    assert service.align_review_order(root) is True  # 바뀐 머리를 들고 옮긴다
    moved = ReviewWorkspaceFacade(root).load_head().revision
    assert _head_names(service, root, moved) == SHOT_ORDER
    assert _decisions(service, root, moved) == decisions
