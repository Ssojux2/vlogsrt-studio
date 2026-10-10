"""A2 가져오기 화면(1번 탭) — 조사→확정→복사·검증의 배선과 상태 계약.

서버 계층(view 메서드)을 직접 부른다 — 브라우저 없이도 실제 배선 값이 오가는
길을 시험한다. 조사자는 가짜로 주입해 ffprobe를 부르지 않는다. 서명 게이트·
작업공간 전환 뒤 새 상태·의미 있는 오류가 이 화면의 계약이다.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

pytest.importorskip(
    "gradio",
    reason="웹 UI extra(gradio)가 없다 — ui extra를 설치하면 이 묶음이 활성화된다",
    exc_type=ImportError,
)
pytest.importorskip(
    "fastapi",
    reason="웹 UI extra(fastapi)가 없다 — ui extra를 설치하면 이 묶음이 활성화된다",
    exc_type=ImportError,
)

import gradio as gr  # noqa: E402

from ui import workspace_service  # noqa: E402
from vlogsrt.project.raw import ingest  # noqa: E402
from vlogsrt.project.raw.models import JobRecord  # noqa: E402

TERMINAL = frozenset({"succeeded", "failed", "cancelled"})


def _fake_inspector(path: Path) -> ingest.SourceInspection:
    return ingest.SourceInspection(
        duration_seconds=2.0, width=320, height=180, audio_streams=1, capture_time=None
    )


def _wait_terminal(server: workspace_service.RawWorkspaceUi, root: Path, job_id: str) -> JobRecord:
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        record = server.raw.get_job(root, job_id)
        if record.status in TERMINAL:
            return record
        time.sleep(0.01)
    return server.raw.get_job(root, job_id)


@pytest.fixture
def server(tmp_path: Path) -> workspace_service.RawWorkspaceUi:
    return workspace_service.RawWorkspaceUi(
        port=7860, state_path=tmp_path / "state.json", inspector=_fake_inspector
    )


@pytest.fixture
def card(tmp_path: Path) -> Path:
    source = tmp_path / "card" / "DCIM"
    source.mkdir(parents=True)
    (source / "DJI_0002.MP4").write_bytes(b"two")
    (source / "DJI_0010.MP4").write_bytes(b"ten")
    return source.parent


def _opened(server: workspace_service.RawWorkspaceUi, tmp_path: Path) -> tuple[str, Path]:
    view = server.create_workspace_view("제주", str(tmp_path / "ws"))
    return view[1], Path(view[0].split("`")[1])


def test_scan_shows_proposal_with_unknown_capture_time(
    server: workspace_service.RawWorkspaceUi, tmp_path: Path, card: Path
) -> None:
    """조사는 표를 내려놓고 촬영 시각이 미조사임을 숨기지 않는다(§5.1·§6.2)."""
    signature, _ = _opened(server, tmp_path)
    markdown = server.ingest_scan_view(signature, str(card))
    assert "DJI_0002.MP4" in markdown and "DJI_0010.MP4" in markdown
    assert "미조사" in markdown
    assert "320×180" in markdown
    assert "충분" in markdown  # 공간 견적이 같이 내려온다


def test_scan_requires_workspace_and_real_folder(
    server: workspace_service.RawWorkspaceUi, tmp_path: Path
) -> None:
    """열린 작업공간 없이·빈 경로로는 의미 있는 오류가 난다 — 조용한 성공이 아니다."""
    with pytest.raises(gr.Error, match="작업공간이 열려 있지 않습니다"):
        server.ingest_scan_view(None, "/tmp")
    signature, _ = _opened(server, tmp_path)
    with pytest.raises(gr.Error, match="선택기로 고르세요"):
        server.ingest_scan_view(signature, "  ")


def test_confirm_requires_a_scan_of_this_workspace(
    server: workspace_service.RawWorkspaceUi, tmp_path: Path, card: Path
) -> None:
    """확정은 이 작업공간의 조사 제안만 받는다 — 없으면 오류, 다른 작업공간 제안이면 오류."""
    signature, _ = _opened(server, tmp_path)
    with pytest.raises(gr.Error, match="조사를 실행"):
        server.ingest_confirm_view(signature)

    server.ingest_scan_view(signature, str(card))
    view_b = server.create_workspace_view("부산", str(tmp_path / "ws-b"))
    signature_b = view_b[1]
    with pytest.raises(gr.Error, match="조사를 실행"):
        server.ingest_confirm_view(signature_b)  # B에는 제안이 없다 — A 것이 새지 않는다


def test_stale_signature_is_refused_on_every_ingest_step(
    server: workspace_service.RawWorkspaceUi, tmp_path: Path, card: Path
) -> None:
    """다른 탭의 오래된 서명으로는 조사·확정·복사 어디도 못 지난다(루트 재현 방지)."""
    signature_a, _ = _opened(server, tmp_path)
    stale = signature_a
    server.create_workspace_view("B", str(tmp_path / "ws-b"))
    with pytest.raises(gr.Error, match="바뀌었습니다"):
        server.ingest_scan_view(stale, str(card))
    with pytest.raises(gr.Error, match="바뀌었습니다"):
        server.ingest_confirm_view(stale)
    with pytest.raises(gr.Error, match="바뀌었습니다"):
        server.ingest_copy_view(stale)
    with pytest.raises(gr.Error, match="바뀌었습니다"):
        server.ingest_retry_view(stale, "job_000001")


def test_copy_runs_as_a_job_and_reports_progress(
    server: workspace_service.RawWorkspaceUi, tmp_path: Path, card: Path
) -> None:
    """복사·검증은 작업으로 돌고 화면은 상태를 다시 읽어 보인다 — 완료 위장 없음."""
    signature, root = _opened(server, tmp_path)
    server.ingest_scan_view(signature, str(card))
    confirmed = server.ingest_confirm_view(signature)
    assert "확정 순서" in confirmed
    assert "import_000001" in confirmed

    markdown, _ = server.ingest_copy_view(signature)
    assert "가져오기 작업" in markdown
    job_id = server.raw.list_jobs(root)[0].job_id
    record = _wait_terminal(server, root, job_id)
    assert record.status == "succeeded", record.error

    header, refreshed, jobs_dropdown = server.ingest_refresh_view(signature)
    assert "ready" in refreshed
    assert "DJI_0002.MP4" in refreshed and "DJI_0010.MP4" in refreshed
    assert jobs_dropdown.choices == []  # 성공한 작업은 재시도 목록에 남지 않는다
    assert "등록 미디어 2건" in header  # 머리글도 함께 내린다 — 오래된 0건 표시가 없다
    assert len(server.raw.list_media(root)) == 2


def test_workspace_switch_resets_ingest_panel(
    server: workspace_service.RawWorkspaceUi, tmp_path: Path, card: Path
) -> None:
    """작업공간을 만들거나 열면 가져오기 패널은 새 상태로 돌아간다."""
    signature_a, root_a = _opened(server, tmp_path)
    server.ingest_scan_view(signature_a, str(card))
    assert "조사 결과" in server._ingest_state_markdown(root_a)

    view_b = server.create_workspace_view("B", str(tmp_path / "ws-b"))
    signature_b, root_b = view_b[1], Path(view_b[0].split("`")[1])
    assert "조사 결과" not in view_b[6]  # B의 첫 보기에는 A의 제안이 없다
    with pytest.raises(gr.Error, match="조사를 실행"):
        server.ingest_confirm_view(signature_b)
    assert server._ingest_proposal is None  # 서버 상태도 비었다
    _ = root_b


def test_copy_before_confirm_is_a_meaningful_error(
    server: workspace_service.RawWorkspaceUi, tmp_path: Path
) -> None:
    """확정 없이 복사 버튼을 누르면 안내로 막는다 — 빈 성공·조용한 무시가 아니다."""
    signature, _ = _opened(server, tmp_path)
    with pytest.raises(gr.Error, match="원본 순서 확인"):
        server.ingest_copy_view(signature)


def test_retry_view_reports_submission(
    server: workspace_service.RawWorkspaceUi, tmp_path: Path, card: Path
) -> None:
    """재시도 보기는 제출 사실을 알린다 — 진행 판정은 새로고침의 몫이다."""
    from vlogsrt.project.raw import ingest as ingest_module

    signature, root = _opened(server, tmp_path)
    server.ingest_scan_view(signature, str(card))
    confirmed = server.ingest_confirm_view(signature)

    broken = {"active": True}
    real_copy = ingest_module._copy_with_hash

    def failing(source: Path, target: Path) -> str:
        if broken["active"] and source.name == "DJI_0010.MP4":
            raise OSError("카드 분리")
        return real_copy(source, target)

    ingest_module._copy_with_hash = failing  # type: ignore[assignment]
    try:
        server.ingest_copy_view(signature)
        job_id = server.raw.list_jobs(root)[0].job_id
        _wait_terminal(server, root, job_id)
    finally:
        ingest_module._copy_with_hash = real_copy  # type: ignore[assignment]
    _ = confirmed

    broken["active"] = False
    choices = server.resumable_ingest_choices(root)
    assert choices and choices[0][1] == job_id  # 실패 작업이 재시도 목록에 보인다
    message = server.ingest_retry_view(signature, job_id)
    assert job_id in message
    record = _wait_terminal(server, root, job_id)
    assert record.status == "succeeded", record.error


def test_global_order_confirm_view_flow(
    server: workspace_service.RawWorkspaceUi, tmp_path: Path, card: Path
) -> None:
    """전체 원본 순서 확정 — 언제나 촬영 시각 순(D53), 배치 안 순서는 그대로.

    D53 이전에는 이 화면이 손으로 적은 순서(역순)를 그대로 확정했다 — 사용자 지시
    "영상은 언제나 만든 시각 순"으로 그 칸을 없앴다. 모르는 ID 검증은 서비스 시험
    (`test_global_order_rejects_incomplete_or_unknown_inputs`)이 그대로 지킨다.
    """
    signature, root = _opened(server, tmp_path)
    server.ingest_scan_view(signature, str(card))
    server.ingest_confirm_view(signature)
    markdown, _ = server.ingest_copy_view(signature)
    job_id = server.raw.list_jobs(root)[0].job_id
    assert _wait_terminal(server, root, job_id).status == "succeeded"

    second_card = tmp_path / "card-b" / "DCIM"
    second_card.mkdir(parents=True)
    (second_card / "B1.MP4").write_bytes(b"b-one")
    view_b = server.create_workspace_view("B", str(tmp_path / "ws-b"))  # 다른 작업공간 차단 확인용
    server.open_workspace_view(str(root))  # 다시 A로 — 서명 갱신
    signature = server.current_signature()
    server.ingest_scan_view(signature, str(tmp_path / "card-b"))
    server.ingest_confirm_view(signature)
    server.ingest_copy_view(signature)
    latest_job = [
        job for job in server.raw.list_jobs(root) if job.status == "running"
    ] or server.raw.list_jobs(root)
    assert _wait_terminal(server, root, latest_job[0].job_id).status == "succeeded"
    _ = view_b

    # 확정 = 제안(촬영 시각 순 — 여기선 모두 미조사라 배치를 넘는 파일 번호 순, D53)
    confirmed = server.ingest_global_confirm_view(signature)
    assert "전체 원본 순서" in confirmed and "gorder_" in confirmed
    first_revision = server.raw.latest_global_source_order(root)
    ready = server.raw.global_order_proposal(root)
    assert [asset.source_name for asset in ready] == ["B1.MP4", "DJI_0002.MP4", "DJI_0010.MP4"]
    assert [entry.asset_id for entry in first_revision.entries] == [a.asset_id for a in ready]
    batch_revisions_before = server.raw.list_order_revisions(root)
    # 같은 순서면 다시 눌러도 새 리비전이 없다.
    assert "리비전 총 1개" in server.ingest_global_confirm_view(signature)

    # D53 — 손으로 정한 순서는 더 이상 살아남지 않는다(예전엔 역순 입력이 그대로 확정됐다).
    # 옛 작업공간에 남은 손 순서를 서비스로 심고, 단추가 촬영 시각 순으로 되돌리는지 본다.
    server.raw.confirm_global_source_order(root, [asset.asset_id for asset in reversed(ready)])
    resorted = server.ingest_global_confirm_view(signature)
    assert "리비전 총 3개" in resorted
    latest = server.raw.latest_global_source_order(root)
    assert latest.revision_id != first_revision.revision_id
    assert [entry.asset_id for entry in latest.entries] == [asset.asset_id for asset in ready]
    assert server.raw.list_order_revisions(root) == batch_revisions_before

    # 오래된 서명은 역시 거부
    server.create_workspace_view("C", str(tmp_path / "ws-c"))
    with pytest.raises(gr.Error, match="바뀌었습니다"):
        server.ingest_global_confirm_view(signature)
