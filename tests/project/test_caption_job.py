"""STAB-S4A — 공유 받기로 시작한 자막 job의 추정·승인·실행 회귀(합성 최종 내보내기).

실제 엔진 진입점(`macos/scripts/vlogsrt-engine.py`)을 in-process로 부른다. 이 job에는
`input.fcpxml`도 러프컷 workflow도 없다 — 검증하는 것은:

- 추정은 전사기·API 키 없이 스테이징된 최종 내보내기만으로 돈다.
- 실행은 자막 승인 문서(CaptionAuthorization)가 지금 추정에 묶일 때만 열리고,
  러프컷 승인으로는 열리지 않는다.
- 가짜 최종 오디오 전사기가 모든 요청의 출처가 최종 WAV임을 증명한다.
- 실패는 같은 승인으로 재시도되며 내구 원장(누적 예산)은 유지된다.
- 취소는 외부 호출 전에 멈추고, 예산 상한은 요청을 보내지 않고 거부한다.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import uuid
import wave
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from analysis_scope import caption_scope_of

from tests._dotenv_guard import stage_synthetic_dotenv
from vlogsrt import transcribe
from vlogsrt.config import Config
from vlogsrt.project import commands as project_commands
from vlogsrt.project.workflow_protocol import (
    ActualUsage,
    AnalysisAuthorization,
    CancellationRequest,
    CaptionAuthorization,
    CaptionResult,
    CaptionStateEvent,
    decode_message,
    encode_message,
)

# 개발 엔진(`macos/scripts/`)은 POSIX 전용이다 — `fcntl` 잠금 · zsh 설치 스크립트. Windows에는 그
# 엔진이 없다(작업대는 `vlogsrt/filelock.py`의 msvcrt 길을 쓴다, W-84a).
pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX 전용 개발 엔진(macos/scripts)")

_ENGINE_SCRIPT = Path(__file__).resolve().parents[2] / "macos" / "scripts" / "vlogsrt-engine.py"
_JOB_DIR_ENV = "VLOGSRT_ENGINE_JOB_DIR"
_PROJECT_NAME = "캡션 프로젝트"
_PROJECT_UID = "CAPTION-PROJECT-UID-1"

# 실행 문(파이프라인) 시험은 실제 ffmpeg를 쓴다 — 전처리 시험과 같은 환경 조건.
needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe가 없는 환경",
)


def _load_engine() -> ModuleType:
    spec = importlib.util.spec_from_file_location("vlogsrt_engine_caption_job", _ENGINE_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def synthetic_job_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """진입점이 참조하는 dotenv는 이 모듈이 지은 합성물뿐이다(S2A-1 격리 원리)."""
    dotenv = stage_synthetic_dotenv(tmp_path)
    monkeypatch.chdir(tmp_path)
    yield dotenv


@pytest.fixture()
def engine(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """진입점 모듈 — main이 남기는 job 루트 환경변수를 테스트 뒤 되돌린다."""
    saved = os.environ.get(_JOB_DIR_ENV)
    yield _load_engine()
    if saved is None:
        os.environ.pop(_JOB_DIR_ENV, None)
    else:
        os.environ[_JOB_DIR_ENV] = saved


def _stage_caption_job(tmp_path: Path, *, seconds: int = 4) -> Path:
    """스테이징된 최종 내보내기 한 벌 — WAV·FCPXML 길이는 정확히 대응한다."""
    job_dir = tmp_path / "caption-job"
    job_dir.mkdir(parents=True)
    frames = 48_000 * seconds
    with wave.open(str(job_dir / "final.wav"), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(48_000)
        stream.writeframes(b"\0\0" * frames)
    (job_dir / "final.fcpxml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n'
        '<fcpxml version="1.14"><resources/>'
        f'<library><event name="이벤트"><project name="{_PROJECT_NAME}" uid="{_PROJECT_UID}">'
        f'<sequence format="r1" duration="{seconds}s" tcStart="0s"><spine/>'
        "</sequence></project></event></library></fcpxml>\n",
        encoding="utf-8",
    )
    (job_dir / "caption-origin.json").write_text(
        json.dumps(
            {
                "origin_job_id": "roughcut-7",
                "origin_revision": "a" * 64,
                "project_uid": _PROJECT_UID,
            }
        ),
        encoding="utf-8",
    )
    return job_dir


def _read_one_job_message(job_dir: Path, name: str) -> Any:
    return project_commands._read_one(job_dir / name)


def _write_caption_authorization(job_dir: Path) -> None:
    """앱이 승인 때 남기는 문서 — job의 자막 범위(요청 리비전·설정 지문)에 정확히 묶인다."""
    estimate = caption_scope_of(job_dir)
    (job_dir / "caption-authorization.jsonl").write_text(
        encode_message(
            CaptionAuthorization(
                request_revision=estimate.request_revision,
                settings_sha256=estimate.settings_sha256,
                authorized_at="2026-10-01T04:00:00Z",
            )
        ),
        encoding="utf-8",
    )


def _read_attempts(job_dir: Path) -> list[Any]:
    path = job_dir / "caption-attempts.jsonl"
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    return [decode_message(line) for line in lines if line.strip()]


@dataclass
class _FinalAudioTranscriber:
    """가짜 최종 오디오 전사기 — 나간 요청마다 출처를 기록하고 회계 관찰자를 지킨다.

    관찰자 훅 실패는 실제 경계(`_observed_call`)처럼 ObserverFailure로 감싸 올라간다
    — 예산 거부가 일반 전사 오류로 위장되지 않게 한다. ``fail_first``번 호출까지는
    예외로 실패한다(재시도 경계). ``oversell_seconds``를 주면 예약 상한이 승인을
    넘게 조각 길이를 부풀린다(예산 거부 경계). ``jobs``는 **실제로 나간** 요청만
    담는다 — 예약 거부·준비 실패로 가지 못한 호출은 여기 없다.
    """

    expected_source: str
    jobs: list[transcribe.TranscriptionJob] = field(default_factory=list)
    calls: int = 0
    fail_first: int = 0
    fail_after_authorize: int = 0
    oversell_seconds: float = 0.0
    observer: Any = None

    def transcribe(
        self, job: transcribe.TranscriptionJob, cfg: Config
    ) -> list[transcribe.ChunkResponse]:
        if self.calls < self.fail_first:
            self.calls += 1
            raise RuntimeError("전사기가 무너졌다")
        duration = 0.0
        if job.chunks:
            duration = float(job.chunks[0].duration) + self.oversell_seconds
        response: dict[str, Any] = {"duration": duration, "usage": {"input_tokens": 1}}
        if job.kind == transcribe.FIXTURE_KIND:
            response["segments"] = [
                {"start": 0.2, "end": 1.0, "text": " 첫 문장입니다 "},
                {"start": 2.0, "end": 3.4, "text": " 마지막 문장입니다 "},
            ]
        else:
            response["text"] = "첫 문장입니다 마지막 문장입니다"
        meta = ticket = None
        if self.observer is not None:
            meta = transcribe.RequestAttemptMeta(
                attempt_id=uuid.uuid4().hex,
                operation=transcribe.TRANSCRIPTION_OPERATION,
                model=cfg.transcribe_model,
                kind=job.kind,
                source=job.source,
                chunk_index=job.chunks[0].index if job.chunks else None,
                chunk_offset=job.chunks[0].offset if job.chunks else None,
                chunk_duration=duration or None,
            )
            try:
                ticket = self.observer.authorize(meta)
            except Exception as exc:  # noqa: BLE001 - 실제 경계와 같은 감싸기다
                raise transcribe.ObserverFailure(
                    f"요청 관찰자의 authorize 훅이 실패했다: {exc}"
                ) from exc
            if self.fail_after_authorize > 0:
                # 예약은 이미 원장에 남았다 — 호출만 무너뜨린다(미지수 예약의 누적 근거).
                self.fail_after_authorize -= 1
                self.calls += 1
                failure = RuntimeError("전사기가 무너졌다")
                self.observer.on_error(ticket, meta, failure)
                raise failure
        self.calls += 1
        self.jobs.append(job)
        result = [
            transcribe.ChunkResponse(index=chunk.index, offset=chunk.offset, payload=dict(response))
            for chunk in job.chunks
        ]
        if ticket is not None and meta is not None:
            self.observer.on_response(ticket, meta, response)
        return result


def _patch_paid_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    fake: _FinalAudioTranscriber,
) -> None:
    """유료 문을 가짜로 막는다 — 키·전사기는 합성물이고 openai는 아예 못 열린다."""
    monkeypatch.setattr(project_commands.env, "require_api_key", lambda **kwargs: "synthetic-key")
    monkeypatch.setattr(project_commands.env, "base_url", lambda: "https://example.invalid")

    def make_transcriber(*args: object, **kwargs: object) -> _FinalAudioTranscriber:
        fake.observer = kwargs.get("observer")
        return fake

    monkeypatch.setattr(transcribe, "get_transcriber", make_transcriber)

    import openai

    monkeypatch.setattr(
        openai,
        "OpenAI",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("paid path")),
    )


def _forbid_paid_surfaces(monkeypatch: pytest.MonkeyPatch) -> None:
    """추정 경로 증명 — 전사기 공장도 키 확인도 불리면 안 된다."""
    monkeypatch.setattr(
        transcribe,
        "get_transcriber",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("전사기가 만들어졌다")),
    )
    monkeypatch.setattr(
        project_commands.env,
        "require_api_key",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("키가 요구됐다")),
    )


# ─────────────────────────────────────────────────────────────
#  추정 — 전사도 키도 없다
# ─────────────────────────────────────────────────────────────


def test_caption_scope_is_measured_without_transcriber_or_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D110: 비용 추정 문(`caption-estimate`)은 없다 — 범위는 전사기·키 없이 잰다."""
    job_dir = _stage_caption_job(tmp_path)
    _forbid_paid_surfaces(monkeypatch)

    scope = caption_scope_of(job_dir)

    assert scope.audio_seconds == 4
    assert scope.cost_upper_bound is None
    assert scope.pricing_snapshot_revision is None
    # 재기는 파일을 쓰지 않는다 — 원장도 가격 문서도 없다.
    assert not (job_dir / "workspace" / "caption.jsonl").exists()
    assert not (job_dir / "pricing-caption.jsonl").exists()
    # 이 job에는 러프컷 workflow가 없다 — receive를 거치지 않는다(S4A).
    assert not (job_dir / "workspace" / "workflow.jsonl").exists()


def test_caption_run_refuses_broken_origin_documents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine: ModuleType
) -> None:
    job_dir = _stage_caption_job(tmp_path)
    _write_caption_authorization(job_dir)
    fake = _FinalAudioTranscriber(expected_source=str(job_dir / "final.wav"))
    _patch_paid_boundaries(monkeypatch, fake)
    (job_dir / "caption-origin.json").unlink()
    assert engine.main(["caption-run", "caption-1", str(job_dir)]) == 1

    # 원본 문서가 있어도 결과 프로젝트 UID가 다르면 남의 타임라인에 자막을 얹을 뻔한 것이다.
    job_dir = _stage_caption_job(tmp_path / "other")
    _write_caption_authorization(job_dir)
    (job_dir / "caption-origin.json").write_text(
        json.dumps(
            {
                "origin_job_id": "roughcut-9",
                "origin_revision": "a" * 64,
                "project_uid": "SOMEONE-ELSE-UID",
            }
        ),
        encoding="utf-8",
    )
    assert engine.main(["caption-run", "caption-2", str(job_dir)]) == 1
    assert not (job_dir / "caption-estimate.jsonl").exists()
    assert not (job_dir / "workspace" / "caption.jsonl").exists()
    assert fake.jobs == []


def test_first_caption_run_fixes_the_scope_and_moves_straight_to_authorized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine: ModuleType
) -> None:
    """D110: 첫 `caption-run`이 원장을 만들고 요청·범위를 고정한다 — estimate_ready 없이 승인."""
    job_dir = _stage_caption_job(tmp_path)
    _write_caption_authorization(job_dir)
    fake = _FinalAudioTranscriber(expected_source=str(job_dir / "final.wav"), fail_first=1)
    _patch_paid_boundaries(monkeypatch, fake)

    assert engine.main(["caption-run", "caption-1", str(job_dir)]) == 1

    journal = project_commands.CaptionJournal.open(job_dir / "workspace" / "caption.jsonl")
    assert journal.state == "caption_authorized"
    assert (job_dir / "workspace" / "caption-request.jsonl").is_file()
    scope = _read_one_job_message(job_dir / "workspace", "caption-estimate.jsonl")
    assert scope == caption_scope_of(job_dir)
    assert not (job_dir / "workspace" / "pricing-caption.jsonl").exists()


def test_legacy_caption_estimate_ready_journal_still_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine: ModuleType
) -> None:
    """D110 이전 원장(caption_estimate_ready)도 열리고 승인으로 이어진다."""
    job_dir = _stage_caption_job(tmp_path)
    request, _ = project_commands._caption_job_facts(job_dir)
    workspace = job_dir / "workspace"
    workspace.mkdir()
    journal = project_commands.CaptionJournal.create(workspace / "caption.jsonl", request)
    project_commands._write_messages(workspace / "caption-request.jsonl", (request,))
    # 새 쓰기는 이 전이를 만들지 않는다 — D110 이전 원장의 한 줄을 손으로 붙인다.
    with (workspace / "caption.jsonl").open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(
            encode_message(CaptionStateEvent(request.request_revision, "caption_estimate_ready"))
        )
    assert journal.state == "caption_received"
    assert (
        project_commands.CaptionJournal.open(workspace / "caption.jsonl").state
        == "caption_estimate_ready"
    )
    _write_caption_authorization(job_dir)
    fake = _FinalAudioTranscriber(expected_source=str(job_dir / "final.wav"), fail_first=1)
    _patch_paid_boundaries(monkeypatch, fake)

    assert engine.main(["caption-run", "caption-1", str(job_dir)]) == 1

    reopened = project_commands.CaptionJournal.open(workspace / "caption.jsonl")
    assert reopened.state == "caption_authorized"


# ─────────────────────────────────────────────────────────────
#  승인 게이트 — 자막 승인만 자막을 승인한다
# ─────────────────────────────────────────────────────────────


def test_caption_run_refuses_without_matching_caption_authorization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine: ModuleType
) -> None:
    job_dir = _stage_caption_job(tmp_path)
    fake = _FinalAudioTranscriber(expected_source=str(job_dir / "final.wav"))
    _patch_paid_boundaries(monkeypatch, fake)

    # 승인 문서가 없으면 실행은 열리지 않는다 — 시도 기록도 없다.
    assert engine.main(["caption-run", "caption-1", str(job_dir)]) == 1
    assert _read_attempts(job_dir) == []

    # 러프컷 승인(AnalysisAuthorization)은 자막을 승인하지 않는다.
    estimate = caption_scope_of(job_dir)
    (job_dir / "caption-authorization.jsonl").write_text(
        encode_message(
            AnalysisAuthorization(
                input_revision=estimate.request_revision,
                settings_sha256=estimate.settings_sha256,
                media_grant_revision="b" * 64,
                authorized_at="2026-10-01T04:00:00Z",
            )
        ),
        encoding="utf-8",
    )
    assert engine.main(["caption-run", "caption-1", str(job_dir)]) == 1
    assert _read_attempts(job_dir) == []
    assert fake.jobs == []
    # 승인이 거부되면 원장도 만들지 않는다 — 거부가 흔적을 남기지 않는다.
    assert not (job_dir / "workspace" / "caption.jsonl").exists()

    # 자막 승인이라도 설정 지문이 다르면(바꿔치기) 거부다.
    _write_caption_authorization(job_dir)
    stale = (job_dir / "caption-authorization.jsonl").read_text(encoding="utf-8")
    (job_dir / "caption-authorization.jsonl").write_text(
        stale.replace(estimate.settings_sha256, "c" * 64), encoding="utf-8"
    )
    assert engine.main(["caption-run", "caption-1", str(job_dir)]) == 1
    assert _read_attempts(job_dir) == []
    assert fake.jobs == []
    assert not (job_dir / "workspace" / "caption.jsonl").exists()

    # 요청 리비전이 다른 승인도 같다.
    (job_dir / "caption-authorization.jsonl").write_text(
        encode_message(
            CaptionAuthorization(
                request_revision="e" * 64,
                settings_sha256=estimate.settings_sha256,
                authorized_at="2026-10-01T04:00:00Z",
            )
        ),
        encoding="utf-8",
    )
    assert engine.main(["caption-run", "caption-1", str(job_dir)]) == 1
    assert fake.jobs == []


def test_old_shape_caption_authorization_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine: ModuleType
) -> None:
    """D110 이전 승인(estimate_revision·pricing_snapshot_revision 포함)은 읽히지 않는다."""
    job_dir = _stage_caption_job(tmp_path)
    scope = caption_scope_of(job_dir)
    (job_dir / "caption-authorization.jsonl").write_text(
        encode_message(
            CaptionAuthorization(
                request_revision=scope.request_revision,
                settings_sha256=scope.settings_sha256,
                authorized_at="2026-10-01T04:00:00Z",
            )
        ),
        encoding="utf-8",
    )
    current = json.loads((job_dir / "caption-authorization.jsonl").read_text(encoding="utf-8"))
    current["payload"]["estimate_revision"] = "f" * 64
    current["payload"]["pricing_snapshot_revision"] = "f" * 64
    (job_dir / "caption-authorization.jsonl").write_text(
        json.dumps(current, sort_keys=True) + "\n", encoding="utf-8"
    )
    fake = _FinalAudioTranscriber(expected_source=str(job_dir / "final.wav"))
    _patch_paid_boundaries(monkeypatch, fake)

    assert engine.main(["caption-run", "caption-1", str(job_dir)]) == 1
    assert fake.jobs == []
    assert not (job_dir / "workspace" / "caption.jsonl").exists()


def test_caption_run_refuses_when_export_or_settings_drifted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine: ModuleType
) -> None:
    job_dir = _stage_caption_job(tmp_path)
    _write_caption_authorization(job_dir)
    fake = _FinalAudioTranscriber(expected_source=str(job_dir / "final.wav"))
    _patch_paid_boundaries(monkeypatch, fake)

    # 오디오가 승인 뒤 바뀌면 요청 리비전이 달라진다 — 새 job이다.
    audio_backup = (job_dir / "final.wav").read_bytes()
    with (job_dir / "final.wav").open("r+b") as stream:
        stream.seek(-2, 2)
        stream.write(b"\1\0")
    assert engine.main(["caption-run", "caption-1", str(job_dir)]) == 1
    assert fake.jobs == []

    # 오디오를 되돌려도 XML이 바뀌면 같은 거부다.
    (job_dir / "final.wav").write_bytes(audio_backup)
    xml = (job_dir / "final.fcpxml").read_text(encoding="utf-8")
    (job_dir / "final.fcpxml").write_text(
        xml.replace('name="이벤트"', 'name="바뀜"'), encoding="utf-8"
    )
    assert engine.main(["caption-run", "caption-1", str(job_dir)]) == 1
    assert fake.jobs == []

    # 재료가 같아도 자막 설정 지문이 바뀌면 승인은 낡은 것이다.
    (job_dir / "final.fcpxml").write_text(xml, encoding="utf-8")
    monkeypatch.setattr(project_commands, "caption_settings_sha256", lambda cfg: "d" * 64)
    assert engine.main(["caption-run", "caption-1", str(job_dir)]) == 1
    assert fake.jobs == []


# ─────────────────────────────────────────────────────────────
#  실행 — 최종 WAV가 모든 요청을 공급한다
# ─────────────────────────────────────────────────────────────


@needs_ffmpeg
def test_caption_run_produces_srt_and_result_in_the_app_visible_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine: ModuleType
) -> None:
    job_dir = _stage_caption_job(tmp_path)
    _write_caption_authorization(job_dir)
    final_wav = job_dir / "final.wav"
    fake = _FinalAudioTranscriber(expected_source=str(final_wav))
    _patch_paid_boundaries(monkeypatch, fake)

    assert engine.main(["caption-run", "caption-1", str(job_dir)]) == 0

    # 모든 전사 요청(1차·두 번째 모델 포함)의 출처는 최종 WAV다 — 원본 오디오 경로는 없다.
    assert fake.jobs, "전사기가 한 번도 불리지 않았다"
    assert {job.source for job in fake.jobs} == {str(final_wav)}

    srt = job_dir / "captions" / f"{_PROJECT_NAME}.ko.srt"
    assert srt.is_file() and srt.stat().st_size > 0
    review = job_dir / "captions" / f"{_PROJECT_NAME}.ko.review.json"
    assert review.is_file()

    result = _read_one_job_message(job_dir, "caption-result.jsonl")
    assert isinstance(result, CaptionResult)
    assert result.speech_status == "speech"
    assert result.srt_path == srt
    assert "coverage:complete" in result.quality

    attempts = _read_attempts(job_dir)
    assert [row.status for row in attempts] == ["pending", "running", "succeeded"]
    journal = project_commands.CaptionJournal.open(job_dir / "workspace" / "caption.jsonl")
    assert journal.state == "caption_ready"
    # 이 job은 끝까지 러프컷 workflow를 만들지 않는다.
    assert not (job_dir / "workspace" / "workflow.jsonl").exists()


@needs_ffmpeg
def test_caption_no_speech_is_a_successful_state_without_srt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine: ModuleType
) -> None:
    job_dir = _stage_caption_job(tmp_path)
    _write_caption_authorization(job_dir)

    @dataclass
    class _Silent:
        """모든 조각이 명시적으로 빈 응답 — ConfirmedNoSpeech의 실제 계약 형태."""

        def transcribe(
            self, job: transcribe.TranscriptionJob, cfg: Config
        ) -> list[transcribe.ChunkResponse]:
            return [
                transcribe.ChunkResponse(
                    index=chunk.index, offset=chunk.offset, payload={"segments": []}
                )
                for chunk in job.chunks
            ]

    monkeypatch.setattr(project_commands.env, "require_api_key", lambda **kw: "synthetic-key")
    monkeypatch.setattr(project_commands.env, "base_url", lambda: "https://example.invalid")
    monkeypatch.setattr(transcribe, "get_transcriber", lambda *a, **kw: _Silent())

    assert engine.main(["caption-run", "caption-1", str(job_dir)]) == 0

    result = _read_one_job_message(job_dir, "caption-result.jsonl")
    assert isinstance(result, CaptionResult)
    assert result.speech_status == "no_speech"
    assert result.srt_path is None
    assert not (job_dir / "captions").exists() or not list((job_dir / "captions").glob("*.srt"))
    attempts = _read_attempts(job_dir)
    assert [row.status for row in attempts] == ["pending", "running", "succeeded"]
    journal = project_commands.CaptionJournal.open(job_dir / "workspace" / "caption.jsonl")
    assert journal.state == "caption_ready"


# ─────────────────────────────────────────────────────────────
#  실패·재시도 — 누적 원장을 유지한 채 같은 승인으로 다시 연다
# ─────────────────────────────────────────────────────────────


@needs_ffmpeg
def test_caption_failure_is_retryable_and_keeps_the_cumulative_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine: ModuleType
) -> None:
    job_dir = _stage_caption_job(tmp_path)
    _write_caption_authorization(job_dir)
    # 예약이 이미 원장에 남은 뒤에 호출이 무너진다 — 미지수 예약이 재시도까지 산다.
    fake = _FinalAudioTranscriber(
        expected_source=str(job_dir / "final.wav"), fail_after_authorize=1
    )
    _patch_paid_boundaries(monkeypatch, fake)

    assert engine.main(["caption-run", "caption-1", str(job_dir)]) == 1

    # 실패 사실은 진단으로 나간다 — 성공 산물(SRT·결과)은 나가지 않는다.
    statuses = [row.status for row in _read_attempts(job_dir)]
    assert statuses == ["pending", "running", "failed"]
    assert not (job_dir / "caption-result.jsonl").exists()
    delivery_dir = job_dir / "captions"
    delivered = list(delivery_dir.glob("*")) if delivery_dir.exists() else []
    assert delivered == []
    # 원장은 caption_authorized에 있다 — 같은 유효 승인으로 재시도할 수 있다.
    journal = project_commands.CaptionJournal.open(job_dir / "workspace" / "caption.jsonl")
    assert journal.state == "caption_authorized"
    failed_attempt = _read_attempts(job_dir)[-1]
    ledger = (job_dir / "workspace" / "accounting" / "budget-ledger.json").read_text(
        encoding="utf-8"
    )
    assert failed_attempt.attempt_id in ledger  # 첫 시도의 예약이 원장에 남아 있다

    fake.fail_after_authorize = 0
    assert engine.main(["caption-run", "caption-1", str(job_dir)]) == 0

    statuses = [row.status for row in _read_attempts(job_dir)]
    assert statuses == ["pending", "running", "failed", "pending", "running", "succeeded"]
    assert (job_dir / "captions" / f"{_PROJECT_NAME}.ko.srt").is_file()
    # 재시도의 확정 사용량도 앱에 보인다 — 원장은 두 시도를 함께 기억한다.
    usage_text = (job_dir / "caption-usage.jsonl").read_text(encoding="utf-8")
    usage_lines = [line for line in usage_text.splitlines(keepends=True) if line.strip()]
    assert usage_lines
    assert all(isinstance(decode_message(line), ActualUsage) for line in usage_lines)
    ledger = (job_dir / "workspace" / "accounting" / "budget-ledger.json").read_text(
        encoding="utf-8"
    )
    succeeded_attempt = _read_attempts(job_dir)[-1]
    assert failed_attempt.attempt_id in ledger and succeeded_attempt.attempt_id in ledger


@needs_ffmpeg
def test_caption_run_has_no_budget_cap_so_a_huge_reservation_still_goes_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine: ModuleType
) -> None:
    """D110: 승인 상한이 없다 — 예약이 아무리 커도 요청은 거부되지 않고 나간다."""
    job_dir = _stage_caption_job(tmp_path)
    _write_caption_authorization(job_dir)
    fake = _FinalAudioTranscriber(
        expected_source=str(job_dir / "final.wav"), oversell_seconds=100_000.0
    )
    _patch_paid_boundaries(monkeypatch, fake)

    engine.main(["caption-run", "caption-1", str(job_dir)])

    assert fake.jobs, "예산 거부로 요청이 막혔다"
    assert all("보내지 않았다" not in (row.error or "") for row in _read_attempts(job_dir))


def test_caption_cancellation_stops_before_external_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine: ModuleType
) -> None:
    job_dir = _stage_caption_job(tmp_path)
    _write_caption_authorization(job_dir)
    fake = _FinalAudioTranscriber(expected_source=str(job_dir / "final.wav"))
    _patch_paid_boundaries(monkeypatch, fake)

    # 앱이 승인 문서에 묶은 요청 리비전으로 취소를 남긴다(시도 ID는 와일드카드).
    request, _ = project_commands._caption_job_facts(job_dir)
    (job_dir / "cancellation.jsonl").write_text(
        encode_message(
            CancellationRequest.create(
                input_revision=request.request_revision,
                attempt_id="*",
                requested_at="2026-10-01T05:00:00Z",
                reason="user",
            )
        ),
        encoding="utf-8",
    )

    assert engine.main(["caption-run", "caption-1", str(job_dir)]) == 1

    attempts = _read_attempts(job_dir)
    assert [row.status for row in attempts] == ["pending", "running", "cancelled"]
    assert fake.jobs == []
    journal = project_commands.CaptionJournal.open(job_dir / "workspace" / "caption.jsonl")
    assert journal.state == "cancelled"
    assert not (job_dir / "caption-result.jsonl").exists()


def test_caption_cancellation_accepts_the_staged_fcpxml_hash_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine: ModuleType
) -> None:
    """receive 전 취소 묶음(S1A-R1의 자막 자리) — 최종 FCPXML 해시로도 잡힌다."""
    import hashlib

    job_dir = _stage_caption_job(tmp_path)
    _write_caption_authorization(job_dir)
    fake = _FinalAudioTranscriber(expected_source=str(job_dir / "final.wav"))
    _patch_paid_boundaries(monkeypatch, fake)
    digest = hashlib.sha256((job_dir / "final.fcpxml").read_bytes()).hexdigest()
    (job_dir / "cancellation.jsonl").write_text(
        encode_message(
            CancellationRequest.create(
                input_revision=digest,
                attempt_id="*",
                requested_at="2026-10-01T05:00:00Z",
                reason="user",
            )
        ),
        encoding="utf-8",
    )

    assert engine.main(["caption-run", "caption-1", str(job_dir)]) == 1
    assert fake.jobs == []
    assert [row.status for row in _read_attempts(job_dir)][-1] == "cancelled"


def test_caption_run_without_estimate_never_invents_a_workflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine: ModuleType
) -> None:
    """추정 없는 실행은 열리지 않는다 — 러프컷 workflow를 새로 만드는 길도 없다."""
    job_dir = _stage_caption_job(tmp_path)
    fake = _FinalAudioTranscriber(expected_source=str(job_dir / "final.wav"))
    _patch_paid_boundaries(monkeypatch, fake)

    assert engine.main(["caption-run", "caption-1", str(job_dir)]) == 1
    assert fake.jobs == []
    assert not (job_dir / "workspace" / "workflow.jsonl").exists()
    assert not (job_dir / "workspace" / "caption.jsonl").exists()
