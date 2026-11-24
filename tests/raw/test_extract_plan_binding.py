"""같은 형상의 새 계획 리비전 추출 — 캐시 키가 리비전 신원을 묶는다(루트 재현).

루트 브라우저 재현: 결정적 계획으로 추출을 끝낸 뒤 의미 분석이 **같은 형상**의
새 계획 리비전을 만들면(받아들인 경계가 없으면 전체 보존 한 조각 — 형상 동일),
완성 package 재사용 판정이 계획 지문만 보아 새 리비전의 추출이 "이미 완성된
package"로 영영 막혔다. 추출 캐시 키는 이제 계획 **리비전 신원**까지 묶는다:

- 새 리비전(같은 형상) → 새 package에서 뽑고 옛 package·전달물은 불변이다.
- 같은 리비전의 재추출 → 여전히 멱등 경계로 막힌다(§6.5).
- 승격 전 예약 키(계획 지문만)로 묶인 옛 package의 재개도 여전히 붙는다.
- 화면의 추출 단추는 제어된 실패를 패널 마크다운으로 돌린다 — 출력 칸이
  브라우저에서 Error로 무효화되지 않는다.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from tests.raw.gemini_echo import write_utterance_replay
from tests.raw.test_review_workspace import keep_luna_workspace
from vlogsrt import preprocess
from vlogsrt.project.raw import runner as extract_runner
from vlogsrt.project.raw import service as raw
from vlogsrt.project.raw.analysis_store import AnalysisStore

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe가 없는 환경",
)

TERMINAL = frozenset({"succeeded", "failed", "cancelled"})


def _make_media(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=25:duration=6",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=4",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _wait_terminal(service: raw.RawWorkspaceService, root: Path, job_id: str):
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        if service.get_job(root, job_id).status in TERMINAL:
            break
        time.sleep(0.05)
    return service.get_job(root, job_id)


def _prepared_workspace(tmp_path: Path) -> tuple[raw.RawWorkspaceService, Path]:
    service = raw.RawWorkspaceService()
    root = tmp_path / "VlogProject"
    service.create_workspace(root, "제주")
    keep_luna_workspace(root)  # 재생 픽스처는 gpt-6-luna 응답 모양이다(D58)
    card = tmp_path / "card" / "DCIM"
    _make_media(card / "제주.mov")
    proposal = service.scan_sources(root, tmp_path / "card")
    confirmation = service.confirm_ingest(root, proposal)
    handle = service.start_ingest_copy(root, confirmation)
    record = _wait_terminal(service, root, handle.job_id)
    assert record.status == "succeeded", record.error
    service.confirm_global_source_order(root)
    return service, root


def _write_replay_fixtures(root: Path, source_sha: str) -> None:
    """재생 픽스처 — 받아들인 경계가 없어 의미 계획이 전체 보존 한 조각이 된다."""
    fixtures = root / "analysis" / "replay"
    fixtures.mkdir(parents=True)
    write_utterance_replay(fixtures, {source_sha: []})
    (fixtures / "luna-fixtures.json").write_text(
        json.dumps(
            {
                "windows": {
                    "window_000000": {
                        "window_id": "window_000000",
                        "speech_summary": "",
                        "visual_summary": "실내 풍경이 이어진다",
                        "scene_tags": ["indoor"],
                        "evidence_ids": ["window_000000"],
                        "suggested_boundary_ids": [],
                        "editing_notes": [],
                        "needs_review": False,
                        "uncertainties": [],
                    }
                }
            }
        ),
        encoding="utf-8",
    )


@needs_ffmpeg
def test_new_revision_same_geometry_extracts_new_package(tmp_path: Path) -> None:
    """결정적 → 의미, 형상 동일 — 새 리비전은 새 package, 옛 것은 불변(루트 재현)."""
    service, root = _prepared_workspace(tmp_path)
    asset = service.list_source_assets(root)[0]

    # 1) 결정적 계획 → 추출 완료.
    plan1 = service.plan_segmentation(root)
    handle1 = service.start_segment_extraction(root, plan_revision_id=plan1.revision_id)
    record1 = _wait_terminal(service, root, handle1.job_id)
    assert record1.status == "succeeded", record1.error
    summary1 = service.package_overview(root)
    assert summary1 is not None and summary1.package.package_dir == "clips/package_000001"
    old_manifest = _sha256(root / summary1.manifest_relative_paths[0])

    # 2) 의미 분석(재생) → 받아들인 경계 없음 → **같은 형상**의 새 계획 리비전.
    _write_replay_fixtures(root, asset.content_sha256 or "")
    analysis = service.start_analysis(root)
    analysis_record = _wait_terminal(service, root, analysis.job_id)
    assert analysis_record.status == "succeeded", analysis_record.error
    store = AnalysisStore(service._workspace(root))
    latest = store.list_revisions()[-1]
    assert [row.status for row in store.list_source_results(latest.revision_id)] == ["analyzed"]
    plan2 = service.plan_segmentation_from_analysis(root)
    assert plan2.revision_id != plan1.revision_id
    assert plan2.fingerprint == plan1.fingerprint  # 형상은 같다 — 충돌의 전제
    same_geometry = [(e.core_start_pts, e.core_end_pts) for e in plan1.entries] == [
        (e.core_start_pts, e.core_end_pts) for e in plan2.entries
    ]
    assert same_geometry

    # 3) 새 리비전 추출 — 막히지 않고 새 package에서 뽑는다.
    handle2 = service.start_segment_extraction(root, plan_revision_id=plan2.revision_id)
    record2 = _wait_terminal(service, root, handle2.job_id)
    assert record2.status == "succeeded", record2.error
    summary2 = service.package_overview(root, "clips/package_000002")
    assert summary2 is not None and summary2.package.package_dir == "clips/package_000002"

    # 옛 package의 전달물·바이트는 그대로다(§6.5 불변).
    assert _sha256(root / summary1.manifest_relative_paths[0]) == old_manifest
    assert [clip.status for clip in summary1.clips] == ["published"] * len(summary1.clips)

    # 4) 같은 리비전의 재추출 — 여전히 멱등 경계로 막힌다.
    with pytest.raises(raw.PackageError, match="이미 완성된 package"):
        service.start_segment_extraction(root, plan_revision_id=plan2.revision_id)


@needs_ffmpeg
def test_legacy_fingerprint_package_retry_still_reattaches(tmp_path: Path) -> None:
    """승격 전 예약 키(계획 지문만)로 묶인 옛 package — 재개는 여전히 붙는다."""
    from vlogsrt.project.raw.jobs import JobStore
    from vlogsrt.project.raw.workspace import RawWorkspace

    service, root = _prepared_workspace(tmp_path)
    plan = service.plan_segmentation(root)
    handles = preprocess.RawClipHandles(before=0, after=0)
    legacy = extract_runner.extraction_fingerprint(
        plan.fingerprint, "source_matched", handles, preprocess.ffmpeg_provenance()
    )
    workspace = RawWorkspace.open(root)
    workspace.reserve_package(
        plan_revision_id=plan.revision_id,
        order_revision_id=plan.order_revision_id,
        profile_name="source_matched",
        handles_before="0",
        handles_after="0",
        job_fingerprint=legacy,
        clip_total=len(plan.entries),
    )
    handle = service.submit_job(root, raw.SEGMENT_JOB_KIND, legacy, lambda ctx: None)
    finished = _wait_terminal(service, root, handle.job_id)
    assert finished.status in TERMINAL
    JobStore(workspace).update(handle.job_id, status="interrupted")
    retry = service.retry_segment_extraction(root, handle.job_id)
    assert retry.job_id == handle.job_id  # 옛 키로 묶인 재개도 같은 작업에 붙는다


@needs_ffmpeg
def test_ui_extract_failure_returns_outputs_without_raising(tmp_path: Path) -> None:
    """제어된 추출 실패 — 패널 마크다운으로 돌고 출력 칸은 무효화되지 않는다."""
    pytest.importorskip(
        "gradio",
        reason="웹 UI extra(gradio)가 없다 — ui extra를 설치하면 이 묶음이 활성화된다",
        exc_type=ImportError,
    )
    from ui import workspace_service

    service, root = _prepared_workspace(tmp_path)
    plan = service.plan_segmentation(root)
    handle = service.start_segment_extraction(root, plan_revision_id=plan.revision_id)
    record = _wait_terminal(service, root, handle.job_id)
    assert record.status == "succeeded", record.error

    server = workspace_service.RawWorkspaceUi(port=7860, state_path=tmp_path / "state.json")
    signature = server.open_workspace_view(str(root))[1]
    # 같은 리비전 재추출 — 서비스는 경계로 막지만 화면은 출력을 지키며 안내한다.
    segment_md, jobs_dd, package_md = server.segment_extract_view(signature, "source_matched")
    assert "추출을 시작하지 못했다" in segment_md
    assert "새 계획 리비전" in segment_md
    assert isinstance(jobs_dd.choices, list)  # 재시도 목록 칸이 살아 있다
    assert "package" in package_md or "clips" in package_md
