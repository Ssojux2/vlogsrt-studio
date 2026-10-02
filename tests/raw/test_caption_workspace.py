"""A7 작업공간 백엔드 — 등록→승인→실행→검토→확정·재개의 실사용 검사(§16 A7).

ffmpeg 합성 미디어(스테레오 지연 오디오·무음·다중 트랙)를 실제 등록하고 가짜
제공자로 엔진을 돌려, 작업공간 계층이 약속한 문을 잰다: 정직한 no_audio 상태,
재시작 재적재, 호출 없는 추정, 지문 결속 승인(어긋나면 0 호출), 동결·초안 바이트
불변, 취소·강제 중단 뒤 캐시 재생 재개(중복 호출 0), 파형 진폭의 실측.
유료 호출은 없다 — 제공자는 전부 가짜고 네트워크는 나가지 않는다.
"""

from __future__ import annotations

import array
import shutil
import subprocess
import time
from collections.abc import Callable
from datetime import date
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from vlogsrt.output import parse_srt
from vlogsrt.project.raw import capabilities
from vlogsrt.project.raw import caption_review as review
from vlogsrt.project.raw.caption_artifacts import parse_vtt
from vlogsrt.project.raw.caption_config import FinalCaptionSettings
from vlogsrt.project.raw.caption_manual import CaptionViewState, ms
from vlogsrt.project.raw.caption_provider import parse_text_response, parse_timing_response
from vlogsrt.project.raw.caption_workspace import CaptionWorkspace, open_caption_workspace
from vlogsrt.project.raw.caption_workspace_types import (
    REGISTRATION_AUDIO_SELECTION_REQUIRED,
    REGISTRATION_NO_AUDIO,
    REGISTRATION_READY,
    RUN_FAILED,
    ApprovalNotFoundError,
    ApprovalRecord,
    CaptionRunRecord,
    FinalVideoRegistration,
    ManualHeadConflict,
    RegistrationNotReadyError,
    RunIdentity,
)
from vlogsrt.project.raw.config import RawWorkflowConfig
from vlogsrt.project.raw.final_caption import CaptionApprovalMismatch, expected_approval
from vlogsrt.project.raw.final_video import FinalVideoChanged
from vlogsrt.project.raw.jobs import JobManager
from vlogsrt.project.raw.workspace import RawWorkspace

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe가 없는 환경",
)

SETTINGS = FinalCaptionSettings(chunk_max_seconds=3.0)
"""4초 오디오를 두 청크(3초+1초)로 나누는 실행 설정 — 재개 검사의 눈금이다."""

BEFORE = date(2026, 10, 2)


def _run_ffmpeg(*args: str) -> None:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-y", *args],
        capture_output=True,
        check=True,
    )


@pytest.fixture
def stereo_delayed(tmp_path: Path) -> Path:
    """스테레오 오디오가 영상보다 2초 늦게 시작하는 합성 최종 영상 — 원점이 양수다."""
    path = tmp_path / "delayed-stereo.mp4"
    _run_ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=320x180:rate=25:duration=8",
        "-f",
        "lavfi",
        "-itsoffset",
        "2",
        "-i",
        "sine=frequency=440:duration=4",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-ac",
        "2",
        str(path),
    )
    return path


@pytest.fixture
def dual_audio(tmp_path: Path) -> Path:
    path = tmp_path / "dual.mp4"
    _run_ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=320x180:rate=25:duration=4",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=4",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=880:duration=4",
        "-map",
        "0:v",
        "-map",
        "1:a",
        "-map",
        "2:a",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        str(path),
    )
    return path


@pytest.fixture
def silent_video(tmp_path: Path) -> Path:
    path = tmp_path / "silent.mp4"
    _run_ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=320x180:rate=25:duration=4",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-an",
        str(path),
    )
    return path


class FakeTiming:
    """시간 제공자 가짜 — 재생 검사의 눈은 inner 호출 수다."""

    def __init__(self, segments_by_chunk: dict[int, list[dict[str, Any]]]) -> None:
        self.segments_by_chunk = segments_by_chunk
        self.calls: list[str] = []

    def timed_transcript(
        self, *, binding: Any, chunk: Any, settings: Any, on: date, **kwargs: Any
    ) -> Any:
        self.calls.append(binding.request_id)
        return parse_timing_response(
            {"segments": self.segments_by_chunk[chunk.index]},
            binding=binding,
            model=settings.models.timing_model,
            provider=settings.caption.timing_provider,
            chunk_duration=chunk.duration,
            chunk_offset=chunk.offset,
        )


class FakeText:
    """문구 제공자 가짜 — 청크당 하나의 응답만 준다."""

    def __init__(self, text_by_chunk: dict[int, str]) -> None:
        self.text_by_chunk = text_by_chunk
        self.calls: list[str] = []

    def text_transcript(
        self, *, binding: Any, chunk: Any, settings: Any, on: date, **kwargs: Any
    ) -> Any:
        self.calls.append(binding.request_id)
        return parse_text_response(
            {"text": self.text_by_chunk[chunk.index]},
            binding=binding,
            model=settings.models.text_model,
        )


def plain_fakes() -> tuple[FakeTiming, FakeText]:
    return (
        FakeTiming(
            {
                0: [{"text": " 망원동 커피에 왔어요", "start": 0.5, "end": 2.5}],
                1: [{"text": "두번째 장면", "start": 0.2, "end": 0.9}],
            }
        ),
        FakeText({0: "망원동 카페에 왔어요", 1: "두번째 장면"}),
    )


def unplaced_fakes() -> tuple[FakeTiming, FakeText]:
    timing, _ = plain_fakes()
    return timing, FakeText({0: "망원동 카페에 왔어요 그리고 사진을 찍었어요", 1: "두번째 장면"})


def open_ws(tmp_path: Path) -> tuple[RawWorkspace, Path]:
    root = tmp_path / f"ws-{time.monotonic_ns()}"  # 시험마다 제 각각의 작업공간
    return RawWorkspace.create(root, "프로젝트"), root


def prepared(
    tmp_path: Path,
    media: Path,
    *,
    run_id: str = "run-1",
    fakes: Callable[[], tuple[FakeTiming, FakeText]] | None = None,
) -> tuple[CaptionWorkspace, FinalVideoRegistration, RunIdentity, CaptionRunRecord]:
    """등록·계획·승인·실행까지 마친 작업공간 — 검토·확정 검사의 출발점."""
    workspace, _ = open_ws(tmp_path)
    ws = open_caption_workspace(workspace)
    reg = ws.register_final_video(media, source_id="final_1")
    timing, text = (fakes or unplaced_fakes)()
    plan = ws.plan(reg, run_id, settings=SETTINGS)
    ws.approve(plan)
    record = ws.run_now(
        reg,
        run_id,
        timing_provider=timing,
        text_provider=text,
        settings=SETTINGS,
        on=BEFORE,
    )
    identity = RunIdentity(reg.final_video_revision, run_id, plan.fingerprint)
    return ws, reg, identity, record


def draft_srt(ws: CaptionWorkspace, identity: RunIdentity) -> Path:
    run = ws.run_record(identity)
    assert run is not None and run.draft_rel is not None
    return ws.root / run.draft_rel / "draft.ko.srt"


def current_warnings_of(ws: CaptionWorkspace, identity: RunIdentity) -> Any:
    """지금 머리가 남긴 경고 목록 — 허용 여부와 무관한 현재 값이다."""
    bundle = ws.review_bundle(identity)
    assert bundle.head is not None
    return bundle.state.warnings


def allow_open_warnings(ws: CaptionWorkspace, identity: RunIdentity) -> None:
    """열린 경고를 전부 이유와 함께 허용한다 — 확정 문을 여는 시험 보조.

    경고 목록은 허용으로 비워지지 않는다(게이트가 허용 장부를 본다) — 1회 통과로
    충분하고, 머리 번호는 허용마다 한 칸씩 앞으로 간다.
    """
    bundle = ws.review_bundle(identity)
    assert bundle.head is not None
    head_sequence = bundle.head.sequence
    for warning in list(bundle.state.warnings):
        allowed = ws.allow_warning(identity, warning.key, "검토했다", expected_head=head_sequence)
        head_sequence = allowed.sequence


def confirm_all(ws: CaptionWorkspace, identity: RunIdentity, *, note: str = "") -> Any:
    """지금 머리의 큐 전부를 명시적으로 짚어 확정한다 — 시험 보조."""
    bundle = ws.review_bundle(identity)
    assert bundle.head is not None
    return ws.confirm(
        identity,
        expected_head=bundle.head.sequence,
        cue_ids=tuple(cue.cue_id for cue in bundle.head.cues),
        note=note,
    )


def wait_job(manager: JobManager, job_id: str) -> Any:
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        record = manager.get(job_id)
        if record.status in ("succeeded", "failed", "cancelled"):
            return record
        time.sleep(0.05)
    raise AssertionError("작업이 제한 시간 안에 끝나지 않았다")


# ─────────────────────────────────────────────────────────────
#  등록 — 정직한 상태·재시작 재적재
# ─────────────────────────────────────────────────────────────


@needs_ffmpeg
def test_noaudio_and_multiaudio_registration_is_honest(
    silent_video: Path, dual_audio: Path, tmp_path: Path
) -> None:
    """오디오 없음은 상태요, 여럿이면 선택이 필요하다 — 어느 쪽도 호출이 없다."""
    workspace, _ = open_ws(tmp_path)
    ws = open_caption_workspace(workspace)
    silent = ws.register_final_video(silent_video, source_id="s")
    assert silent.status == REGISTRATION_NO_AUDIO and silent.audio_stream_count == 0
    assert silent.pcm_sha256 is None and silent.verified_copy_rel is None
    timing, text = plain_fakes()
    with pytest.raises(RegistrationNotReadyError):
        ws.run_now(silent, "run-1", timing_provider=timing, text_provider=text, settings=SETTINGS)
    with pytest.raises(RegistrationNotReadyError):
        ws.estimate(silent, "run-1", settings=SETTINGS)
    assert timing.calls == [] and text.calls == []

    undecided = ws.register_final_video(dual_audio, source_id="d")
    assert undecided.status == REGISTRATION_AUDIO_SELECTION_REQUIRED
    assert undecided.audio_stream_count == 2
    first = ws.register_final_video(dual_audio, source_id="d", audio_ordinal=0)
    second = ws.register_final_video(dual_audio, source_id="d", audio_ordinal=1)
    assert first.status == second.status == REGISTRATION_READY
    assert first.pcm_sha256 != second.pcm_sha256  # 선택마다 다른 PCM이다(§11.1)
    assert ws.registration(dual_audio_hashes(dual_audio), 1) is not None


def dual_audio_hashes(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


@needs_ffmpeg
def test_registration_survives_restart_with_same_identity(
    stereo_delayed: Path, tmp_path: Path
) -> None:
    """재시작 뒤 같은 등록 행·같은 오디오 신원 — 외부 원본이 없어도 재적재된다."""
    workspace, _ = open_ws(tmp_path)
    ws = open_caption_workspace(workspace)
    reg = ws.register_final_video(stereo_delayed, source_id="final_1")
    assert reg.status == REGISTRATION_READY and reg.channels == 2
    assert reg.origin is not None and reg.origin > 0  # 잰 원점이 양수로 살아 있다
    video = ws.video_input(reg)

    stereo_delayed.unlink()  # 외부 원본이 사라져도 작업공간 사본이 정본이다
    reopened = open_caption_workspace(RawWorkspace.open(workspace.root))
    again = reopened.registration(reg.final_video_revision, 0)
    assert again == reg
    revived = reopened.video_input(again)
    assert revived.audio.pcm_sha256 == video.audio.pcm_sha256
    assert revived.audio.sample_count == video.audio.sample_count
    assert revived.audio.pcm_sample0_file_time == video.audio.pcm_sample0_file_time
    assert revived.audio.channels == 2


@needs_ffmpeg
def test_reregistration_is_idempotent(stereo_delayed: Path, tmp_path: Path) -> None:
    """같은 파일의 재등록은 같은 행을 돌려준다 — 검증 사본도 다시 안 만든다."""
    workspace, _ = open_ws(tmp_path)
    ws = open_caption_workspace(workspace)
    first = ws.register_final_video(stereo_delayed, source_id="final_1")
    copy_bytes = (
        (ws.root / first.verified_copy_rel).read_bytes() if first.verified_copy_rel else b""
    )
    second = ws.register_final_video(stereo_delayed, source_id="final_1")
    assert second == first
    assert (ws.root / second.verified_copy_rel).read_bytes() == copy_bytes
    assert len(ws.registrations()) == 1


# ─────────────────────────────────────────────────────────────
#  추정·승인 — 호출 없이 굳는다, 지문에 묶인다
# ─────────────────────────────────────────────────────────────


@needs_ffmpeg
def test_estimate_is_local_and_fingerprint_bound(stereo_delayed: Path, tmp_path: Path) -> None:
    """추정은 제공자 없이 나오고 계획 지문과 같다 — 모델·수명주기·청크·비용까지."""
    from decimal import Decimal

    workspace, _ = open_ws(tmp_path)
    ws = open_caption_workspace(workspace)
    reg = ws.register_final_video(stereo_delayed, source_id="final_1")
    estimate = ws.estimate(reg, "run-1", settings=SETTINGS)
    assert estimate.chunk_count == 2
    assert estimate.chunk_seconds[0] == (Fraction(0), Fraction(3))
    assert estimate.chunk_seconds[-1][1] == Fraction(reg.sample_count or 0, 16000)
    assert estimate.channels == 2 and Decimal(estimate.estimated_cost_usd) > 0
    assert estimate.text_model == RawWorkflowConfig().models.text_model
    assert estimate.timing_model == RawWorkflowConfig().models.timing_model
    assert {item.role for item in estimate.lifecycle} == {
        capabilities.ROLE_TEXT,
        capabilities.ROLE_TIMING,
    }
    assert estimate.identity.fingerprint == ws.plan(reg, "run-1", settings=SETTINGS).fingerprint


@needs_ffmpeg
def test_run_requires_stored_fingerprint_bound_approval(
    stereo_delayed: Path, tmp_path: Path
) -> None:
    """승인 없음·남의 지문 승인 — 첫 호출도 나가지 않고 실행 행은 실패다(§14.3)."""
    workspace, _ = open_ws(tmp_path)
    ws = open_caption_workspace(workspace)
    reg = ws.register_final_video(stereo_delayed, source_id="final_1")
    timing, text = plain_fakes()
    with pytest.raises(ApprovalNotFoundError):
        ws.run_now(
            reg,
            "run-1",
            timing_provider=timing,
            text_provider=text,
            settings=SETTINGS,
            on=BEFORE,
        )
    assert timing.calls == [] and text.calls == []

    # 위조 승인 — 자리는 이 실행의 것이지만 값은 남의 계획에서 왔다
    plan = ws.plan(reg, "run-1", settings=SETTINGS)
    other = ws.plan(reg, "run-1", settings=FinalCaptionSettings(chunk_max_seconds=2.0))
    forged_values = expected_approval(other)
    ws.store.save_approval(
        ApprovalRecord(
            identity=RunIdentity(reg.final_video_revision, "run-1", plan.fingerprint),
            audio_stream_index=forged_values.audio_stream_index,
            sample_start=forged_values.sample_start,
            sample_end=forged_values.sample_end,
            text_model=forged_values.text_model,
            timing_model=forged_values.timing_model,
            settings_fingerprint=forged_values.settings_fingerprint,
            budget_usd=str(forged_values.budget_usd),
            note="위조 또는 낡은 승인",
            created_at=BEFORE.isoformat(),
        )
    )
    with pytest.raises(CaptionApprovalMismatch):
        ws.run_now(
            reg,
            "run-1",
            timing_provider=timing,
            text_provider=text,
            settings=SETTINGS,
            on=BEFORE,
        )
    assert timing.calls == [] and text.calls == []
    record = ws.run_record(RunIdentity(reg.final_video_revision, "run-1", plan.fingerprint))
    assert record is not None and record.status == RUN_FAILED  # 거부가 기록에 남는다


# ─────────────────────────────────────────────────────────────
#  실행 — 초안·동결·재제출 0 호출
# ─────────────────────────────────────────────────────────────


@needs_ffmpeg
def test_full_workflow_then_rerun_makes_no_calls(stereo_delayed: Path, tmp_path: Path) -> None:
    """승인된 실행은 초안을 게시하고 동결한다 — 재제출은 공급자를 안 부른다."""
    ws, reg, identity, record = prepared(tmp_path, stereo_delayed)
    assert record.status == "succeeded" and record.draft_rel is not None
    srt = draft_srt(ws, identity)
    assert srt.is_file() and "draft.ko.srt" in str(srt)
    bundle = ws.review_bundle(identity)
    assert bundle.head is not None and bundle.head.sequence == 0  # 기본 리비전 저장됐다
    assert bundle.state.playback.channels == 2
    frozen_bytes = bundle.snapshot.path.read_bytes()
    draft_bytes = srt.read_bytes()

    timing, text = plain_fakes()
    again = ws.run_now(
        reg,
        identity.run_id,
        timing_provider=timing,
        text_provider=text,
        settings=SETTINGS,
        on=BEFORE,
    )
    assert again.status == "succeeded"
    assert timing.calls == [] and text.calls == []  # 동결된 실행은 다시 안 돈다
    assert srt.read_bytes() == draft_bytes
    assert ws.review_bundle(identity).snapshot.path.read_bytes() == frozen_bytes


@needs_ffmpeg
def test_job_manager_runs_body_to_success(stereo_delayed: Path, tmp_path: Path) -> None:
    """작업 본문이 기존 JobContext·취소·진행을 탄다 — 상태는 DB에 남는다(§14.1)."""
    workspace, _ = open_ws(tmp_path)
    ws = open_caption_workspace(workspace)
    reg = ws.register_final_video(stereo_delayed, source_id="final_1")
    timing, text = unplaced_fakes()
    plan = ws.plan(reg, "run-j", settings=SETTINGS)
    ws.approve(plan)
    manager = JobManager(workspace)
    handle = manager.submit(
        "caption_run",
        plan.fingerprint,
        ws.run_body(
            reg,
            "run-j",
            timing_provider=timing,
            text_provider=text,
            settings=SETTINGS,
            on=BEFORE,
        ),
    )
    finished = wait_job(manager, handle.job_id)
    assert finished.status == "succeeded"
    assert finished.progress_total == 3  # 계획→실행→정착의 눈금이 jobs 표에 있다
    record = ws.run_record(RunIdentity(reg.final_video_revision, "run-j", plan.fingerprint))
    assert record is not None and record.status == "succeeded"
    assert draft_srt(ws, record.identity).is_file()


# ─────────────────────────────────────────────────────────────
#  취소·강제 중단·재개 — 캐시 재생으로 중복 호출 0
# ─────────────────────────────────────────────────────────────


@needs_ffmpeg
def test_cancel_then_resume_replays_cached_responses(stereo_delayed: Path, tmp_path: Path) -> None:
    """취소는 유효 결과를 보존하고, 재개는 들은 청크를 다시 묻지 않는다(§14.1)."""
    workspace, _ = open_ws(tmp_path)
    ws = open_caption_workspace(workspace)
    reg = ws.register_final_video(stereo_delayed, source_id="final_1")
    plan = ws.plan(reg, "run-1", settings=SETTINGS)
    ws.approve(plan)
    passes = {"count": 0}

    def gate() -> None:
        from vlogsrt.project.raw.final_caption import CaptionRunCancelled

        passes["count"] += 1
        if passes["count"] > 1:
            raise CaptionRunCancelled("취소 시험")

    timing, text = plain_fakes()
    cancelled = ws.run_now(
        reg,
        "run-1",
        timing_provider=timing,
        text_provider=text,
        settings=SETTINGS,
        on=BEFORE,
        gate=gate,
    )
    assert cancelled.status == "cancelled" and cancelled.cancellation_reason
    assert len(timing.calls) == 1  # 청크 0의 시간 응답만 받았다

    resumed_timing, resumed_text = plain_fakes()
    identity = RunIdentity(reg.final_video_revision, "run-1", plan.fingerprint)
    resumed = ws.run_now(
        reg,
        "run-1",
        timing_provider=resumed_timing,
        text_provider=resumed_text,
        settings=SETTINGS,
        on=BEFORE,
    )
    assert resumed.status == "succeeded"
    assert resumed_timing.calls == ["run-1:timing:0001"]  # 청크 0은 재생했다
    assert len(resumed_text.calls) == 2

    reference_timing, reference_text = plain_fakes()  # 중단 없는 참조 실행
    reference_plan = ws.plan(reg, "run-full", settings=SETTINGS)
    ws.approve(reference_plan)
    ws.run_now(
        reg,
        "run-full",
        timing_provider=reference_timing,
        text_provider=reference_text,
        settings=SETTINGS,
        on=BEFORE,
    )
    reference = RunIdentity(reg.final_video_revision, "run-full", reference_plan.fingerprint)
    assert draft_srt(ws, identity).read_bytes() == draft_srt(ws, reference).read_bytes()


@needs_ffmpeg
def test_hard_interrupt_leaves_durable_evidence_and_restart_resumes(
    stereo_delayed: Path, tmp_path: Path
) -> None:
    """강제 중단(프로세스 사망 모양) — 응답은 호출 직후 굳어서 재시작 재개가 안 묻는다."""
    workspace, _ = open_ws(tmp_path)
    ws = open_caption_workspace(workspace)
    reg = ws.register_final_video(stereo_delayed, source_id="final_1")
    plan = ws.plan(reg, "run-1", settings=SETTINGS)
    ws.approve(plan)
    passes = {"count": 0}

    def collapsing_gate() -> None:
        passes["count"] += 1
        if passes["count"] > 1:
            raise RuntimeError("강제 중단 — 프로세스가 사라졌다")

    timing, text = plain_fakes()
    with pytest.raises(RuntimeError, match="강제 중단"):
        ws.run_now(
            reg,
            "run-1",
            timing_provider=timing,
            text_provider=text,
            settings=SETTINGS,
            on=BEFORE,
            gate=collapsing_gate,
        )
    identity = RunIdentity(reg.final_video_revision, "run-1", plan.fingerprint)
    interrupted = ws.run_record(identity)
    assert interrupted is not None and interrupted.status == RUN_FAILED
    assert len(timing.calls) == 1  # 청크 0 응답은 이미 캐시에 굳었다

    reopened = open_caption_workspace(RawWorkspace.open(workspace.root))  # 서버 재시작
    revived = reopened.registration(reg.final_video_revision, 0)
    assert revived is not None
    resumed_timing, resumed_text = plain_fakes()
    resumed = reopened.run_now(
        revived,
        "run-1",
        timing_provider=resumed_timing,
        text_provider=resumed_text,
        settings=SETTINGS,
        on=BEFORE,
    )
    assert resumed.status == "succeeded"
    assert resumed_timing.calls == ["run-1:timing:0001"]  # 중단 전 응답은 재생이다
    assert len(resumed_text.calls) == 2


# ─────────────────────────────────────────────────────────────
#  검토·확정 — 편집 래퍼·전체 큐 확인·옛 바이트 불변
# ─────────────────────────────────────────────────────────────


@needs_ffmpeg
def test_manual_edits_confirm_reparse_and_old_bytes_stable(
    stereo_delayed: Path, tmp_path: Path
) -> None:
    """글자·시각·분할·병합·미대응·허용·확정 — 동결·초안은 한 바이트도 안 바뀐다."""
    ws, reg, identity, _ = prepared(tmp_path, stereo_delayed)
    bundle = ws.review_bundle(identity)
    frozen_bytes = bundle.snapshot.path.read_bytes()
    draft_bytes = draft_srt(ws, identity).read_bytes()
    unplaced = bundle.state.unplaced
    assert unplaced and unplaced[0].resolution is None  # 미대응이 처리 대기다

    head = ws.edit_cue_text(identity, "a0000", "망원동 카페에 왔어요(고침)", expected_head=0)
    with pytest.raises(ManualHeadConflict):  # 낡은 기대 머리 — 덮는 길이 없다
        ws.edit_cue_text(identity, "a0000", "낡은 머리 편집", expected_head=0)
    head = ws.edit_cue_time(
        identity, "a0000", start=ms(2500), end=ms(4400), expected_head=head.sequence
    )
    head = ws.split_cue(identity, "a0000", ms(3300), "앞", "뒤", expected_head=head.sequence)
    warning_head = head.sequence
    for warning in list(current_warnings_of(ws, identity)):  # 허용은 목록을 비우지 않는다
        head = ws.allow_warning(
            identity, warning.key, "짧은 표시를 확인했다", expected_head=warning_head
        )
        warning_head = head.sequence
    split_head = ws.review_bundle(identity).head
    assert split_head is not None
    left = next(cue for cue in split_head.cues if cue.text == "앞")
    right = next(cue for cue in split_head.cues if cue.text == "뒤")
    head = ws.merge_cues(identity, left.cue_id, right.cue_id, expected_head=split_head.sequence)
    added = ws.add_manual_cue(
        identity,
        ms(6000),
        ms(7800),
        "그리고 사진을 찍었어요",
        note="미대응 말을 살렸다",
        expected_head=head.sequence,
    )
    placed = next(cue for cue in added.cues if cue.text == "그리고 사진을 찍었어요")
    head = ws.resolve_unplaced_item(
        identity,
        unplaced[0].item.index,
        unplaced[0].item.text,
        placed_cue_id=placed.cue_id,
        expected_head=added.sequence,
    )
    bundle = ws.review_bundle(identity)
    assert bundle.head is not None and bundle.head.sequence == head.sequence

    CaptionViewState(query="카페")  # 보기 상태는 어디에도 기록되지 않는다
    assert ws.review_bundle(identity).snapshot.path.read_bytes() == frozen_bytes
    assert draft_srt(ws, identity).read_bytes() == draft_bytes

    with pytest.raises(ManualHeadConflict):  # 확정도 낡은 머리를 못 박는다
        ws.confirm(
            identity, expected_head=0, cue_ids=tuple(c.cue_id for c in head.cues), note="낡은 머리"
        )
    done = ws.confirm(
        identity,
        expected_head=head.sequence,
        cue_ids=tuple(cue.cue_id for cue in head.cues),
        note="모든 큐를 짚었다",
    )
    assert done.registration.ready and done.run.status == "succeeded"
    assert done.export.srt.is_file() and done.export.vtt.is_file()
    assert done.draft_srt.read_bytes() == draft_bytes  # 초안도 여전히 무사하다
    srt_cues = parse_srt(done.export.srt.read_text(encoding="utf-8"))
    vtt_cues = parse_vtt(done.export.vtt.read_text(encoding="utf-8"))
    assert bundle.head is not None
    assert [(round(cue.start * 1000), round(cue.end * 1000), cue.text) for cue in srt_cues] == [
        (review._ms_int(cue.start), review._ms_int(cue.end), cue.text) for cue in bundle.head.cues
    ]
    assert [(review._ms_int(s), review._ms_int(e), t) for s, e, t in vtt_cues] == [
        (review._ms_int(cue.start), review._ms_int(cue.end), cue.text) for cue in bundle.head.cues
    ]

    first_final = {
        path: path.read_bytes() for path in done.export.directory.iterdir() if path.is_file()
    }
    # 서버 재시작 — 등록·검토 연쇄를 새 인스턴스로 다시 연다
    ws = open_caption_workspace(RawWorkspace.open(ws.workspace.root))
    reopened_bundle = ws.review_bundle(identity)
    assert reopened_bundle.head is not None
    assert reopened_bundle.head.sequence == bundle.head.sequence  # 연쇄가 그대로 이어진다
    edited = ws.edit_cue_text(
        identity,
        reopened_bundle.head.cues[0].cue_id,
        "두번째 확정 문구",
        expected_head=reopened_bundle.head.sequence,
    )
    second = ws.confirm(
        identity,
        expected_head=edited.sequence,
        cue_ids=tuple(cue.cue_id for cue in edited.cues),
    )
    assert second.export.directory != done.export.directory  # 새 확정 자리다
    for path, body in first_final.items():
        assert path.read_bytes() == body  # 옛 final은 바이트 그대로다


# ─────────────────────────────────────────────────────────────
#  문 — 원본·PCM 변조, 검증 토글, 파형
# ─────────────────────────────────────────────────────────────


@needs_ffmpeg
def test_source_and_pcm_gating_survive_restart(stereo_delayed: Path, tmp_path: Path) -> None:
    """검증 사본·PCM이 바뀌면 실행·확정이 닫힌다 — 되돌리면 다시 연다."""
    ws, reg, identity, _ = prepared(tmp_path, stereo_delayed, fakes=plain_fakes)
    copy_path = ws.root / reg.verified_copy_rel
    assert reg.pcm_rel is not None
    pcm_path = ws.root / reg.pcm_rel
    copy_bytes = copy_path.read_bytes()
    pcm_bytes = pcm_path.read_bytes()

    timing, text = plain_fakes()
    plan = ws.plan(reg, "run-9", settings=SETTINGS)  # 멀쩡할 때 계획·승인까지
    ws.approve(plan)
    copy_path.write_bytes(copy_bytes + b"tampered")
    with pytest.raises(FinalVideoChanged):
        ws.run_now(
            reg,
            "run-9",
            timing_provider=timing,
            text_provider=text,
            settings=SETTINGS,
            on=BEFORE,
        )
    assert timing.calls == [] and text.calls == []

    copy_path.write_bytes(copy_bytes)
    flipped = bytearray(pcm_bytes)
    flipped[len(flipped) // 2] ^= 0xFF  # 같은 길이·다른 바이트 — 해시 어긋남이 확정이다
    pcm_path.write_bytes(bytes(flipped))
    with pytest.raises(FinalVideoChanged):
        confirm_all(ws, identity)

    pcm_path.write_bytes(pcm_bytes)
    allow_open_warnings(ws, identity)  # 되돌리면 확정이 다시 열린다
    assert confirm_all(ws, identity).export.srt.is_file()


@needs_ffmpeg
def test_waveform_peaks_come_from_local_pcm(stereo_delayed: Path, tmp_path: Path) -> None:
    """파형 피크는 등록 PCM의 실측 진폭이다 — 발화·시각은 만들지 않는다(§11.7)."""
    ws, reg, identity, _ = prepared(tmp_path, stereo_delayed)
    wave = ws.waveform(reg, bin_seconds=0.05)
    assert reg.pcm_rel is not None and reg.channels == 2
    pcm_bytes = (ws.root / reg.pcm_rel).read_bytes()
    bin_frames = wave.bin_frames
    width = 2 * 2
    first = array.array("h")
    first.frombytes(pcm_bytes[: bin_frames * width])
    assert wave.peaks[0] == max(abs(sample) for sample in first)
    assert len(wave.peaks) == -(-(reg.sample_count or 0) // bin_frames)
    assert wave.peak_scale == 32768
    assert all(0 <= peak <= 32767 for peak in wave.peaks)
    assert any(peak > 1000 for peak in wave.peaks)  # 정현파가 실제로 울렸다
    assert wave.pcm_sha256 == reg.pcm_sha256
    assert not hasattr(wave, "speech") and not hasattr(wave, "timestamps")  # 지어낸 칸이 없다


@needs_ffmpeg
def test_verify_toggle_and_view_state_leave_bytes_identical(
    stereo_delayed: Path, tmp_path: Path
) -> None:
    """검증 on/off·보기 토글은 SRT 바이트를 바꾸지 않는다(규칙 5·§11.3)."""
    workspace, _ = open_ws(tmp_path)
    ws = open_caption_workspace(workspace)
    reg = ws.register_final_video(stereo_delayed, source_id="final_1")
    timing, text = plain_fakes()
    plan = ws.plan(reg, "run-1", settings=SETTINGS)
    ws.approve(plan)
    ws.run_now(
        reg,
        "run-1",
        timing_provider=timing,
        text_provider=text,
        settings=SETTINGS,
        on=BEFORE,
        verify_enabled=True,
    )
    identity = RunIdentity(reg.final_video_revision, "run-1", plan.fingerprint)
    frozen_bytes = ws.review_bundle(identity).snapshot.path.read_bytes()
    draft_bytes = draft_srt(ws, identity).read_bytes()

    quiet_timing, quiet_text = plain_fakes()
    ws.run_now(  # 동결된 실행 — 검증 토글과 무관하게 0 호출
        reg,
        "run-1",
        timing_provider=quiet_timing,
        text_provider=quiet_text,
        settings=SETTINGS,
        on=BEFORE,
        verify_enabled=False,
    )
    assert quiet_timing.calls == [] and quiet_text.calls == []
    assert draft_srt(ws, identity).read_bytes() == draft_bytes

    off_plan = ws.plan(reg, "run-v", settings=SETTINGS)
    ws.approve(off_plan)
    off_timing, off_text = plain_fakes()
    ws.run_now(
        reg,
        "run-v",
        timing_provider=off_timing,
        text_provider=off_text,
        settings=SETTINGS,
        on=BEFORE,
        verify_enabled=False,
    )
    off_identity = RunIdentity(reg.final_video_revision, "run-v", off_plan.fingerprint)
    assert draft_srt(ws, off_identity).read_bytes() == draft_bytes  # 검증은 바이트를 안 바꾼다
    assert ws.review_bundle(identity).snapshot.path.read_bytes() == frozen_bytes
