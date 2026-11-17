"""D59 19b — 「다시 듣기」 차례(asr epoch).

차례가 0이면 어느 열쇠도 바뀌지 않고, 오르면 그 원본만 새로 산다. 기존 원본 전사는 온도 0
버그(D55)로 산 것이다. 청크 캐시 열쇠에 온도가 없고 끝난 분석은 지문이 같으면 다시 쓰이므로,
다시 들으려면 사람이 영상마다 차례를 올린다. 차례 0의 열쇠·결속·digest·입력 지문은 19b 이전
조립 함수로 다시 계산한 값과 바이트까지 같다(18a·18b와 같은 증명 방식). 유료 호출 0회 — 전사·
의미 분석은 가짜 전송이 센다.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from dataclasses import asdict
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from tests.raw.test_background_library import setup_library
from tests.raw.test_browser_runtime import _make_media
from tests.raw.test_review_workspace import _wait_terminal
from tests.raw.test_selection_analysis import wait_until
from tests.raw.test_transcription_identity import PRE_W18_DEFAULT, SPANS, pre_w14_chunk_key
from tests.raw.test_understanding import BEFORE_SHUTDOWN, SETTINGS, FakeTimingProvider, make_audio
from tests.raw.test_visual_workbench_ui import _model_of
from ui import workspace_service
from vlogsrt.project.raw import asr_epoch, media_refs, source_transcript
from vlogsrt.project.raw import service as raw
from vlogsrt.project.raw.analysis_config import (
    UNDERSTANDING_SCHEMA_VERSION,
    RawUnderstandingSettings,
)
from vlogsrt.project.raw.analysis_workflow import (
    INPUT_FINGERPRINT_SCHEMA,
    VideoGrid,
    _fraction_text,
    source_input_fingerprint,
)
from vlogsrt.project.raw.browser_runtime import asr_estimate_digest
from vlogsrt.project.raw.config import RawWorkflowConfig
from vlogsrt.project.raw.models import RawWorkspaceError
from vlogsrt.project.raw.understanding import (
    AnalysisPlanningError,
    SampleSpan,
    asr_chunk_key,
    transcribe_selected_audio,
)
from vlogsrt.project.raw.workspace import DB_NAME, RawWorkspace

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")

TRANSCRIBE = raw.SELECTION_MODE_TRANSCRIBE
FULL = raw.SELECTION_MODE_FULL
DONE = "추천 구간 준비 완료"
RELISTENED = "다시 들은 문장입니다."


# ── 차례 저장 — 읽기는 아무것도 만들지 않는다 ─────────────────────────────────────


def _table_exists(root: Path) -> bool:
    with sqlite3.connect(root / DB_NAME) as db:
        found = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (asr_epoch.TABLE,)
        ).fetchone()
    return found is not None


def test_the_epoch_store_reads_zero_without_writing_and_survives_reopening(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    raw.RawWorkspaceService().create_workspace(root, "다시 듣기")
    workspace = RawWorkspace.open(root)
    assert asr_epoch.state(workspace, "source_a") == asr_epoch.AsrEpoch(0, 0)
    asr_epoch.mark_heard(workspace, "source_a", 0)
    assert asr_epoch.withdraw(workspace, "source_a") is False
    assert not _table_exists(root)  # 읽기·차례 0 기록·거둘 것 없음은 표를 만들지 않는다
    first = asr_epoch.bump(workspace, "source_a")
    assert first == asr_epoch.AsrEpoch(1, 0) and first.pending
    assert asr_epoch.bump(workspace, "source_a") == first  # 기다리는 동안 두 번 눌러도 한 번
    reopened = RawWorkspace.open(root)
    assert asr_epoch.state(reopened, "source_a") == first  # 앱을 다시 열어도 남는다
    assert asr_epoch.state(reopened, "source_b") == asr_epoch.AsrEpoch(0, 0)
    asr_epoch.mark_heard(reopened, "source_a", 2)  # 지금 차례가 아니면 적지 않는다
    assert asr_epoch.state(reopened, "source_a").pending
    asr_epoch.mark_heard(reopened, "source_a", 1)
    assert asr_epoch.state(reopened, "source_a") == asr_epoch.AsrEpoch(1, 1)
    assert asr_epoch.bump(reopened, "source_a") == asr_epoch.AsrEpoch(2, 1)
    assert asr_epoch.withdraw(reopened, "source_a") is True
    assert asr_epoch.state(reopened, "source_a") == asr_epoch.AsrEpoch(1, 1)
    assert asr_epoch.withdraw(reopened, "source_a") is False


def test_the_binding_tag_is_absent_for_zero_and_refuses_bad_epochs() -> None:
    assert asr_epoch.binding_tag(0) is None
    assert asr_epoch.binding_tag(3) == "asr-epoch/3"
    for bad in (-1, True, 1.0, "1"):
        with pytest.raises(RawWorkspaceError):
            asr_epoch.binding_tag(bad)  # type: ignore[arg-type]
        with pytest.raises(AnalysisPlanningError):
            asr_chunk_key("w" * 64, "P", RawWorkflowConfig(), SampleSpan(0, 1), bad)  # type: ignore[arg-type]


# ── 청크 열쇠 — 차례 0은 19b 이전 열쇠, 차례가 오르면 그 원본의 청크만 새로 산다 ─────────


def _run(tmp_path: Path, cache: Path, epoch: int) -> FakeTimingProvider:
    provider = FakeTimingProvider(
        replies={0: [{"text": "첫 문장", "start": 0.0, "end": 4.0}], 1: []}
    )
    transcribe_selected_audio(
        make_audio(tmp_path, seconds=20.0),
        SPANS,
        settings=RawWorkflowConfig(),
        understanding=SETTINGS,
        provider=provider,
        on=BEFORE_SHUTDOWN,
        request_id="relisten-pin",
        workdir=tmp_path,
        cache_dir=cache,
        asr_epoch=epoch,
    )
    return provider


def epoch_chunk_key(wav: Path, span: SampleSpan, epoch: int) -> str:
    """19b의 열쇠 — W-14 이전 조립의 칸 그대로에 차례 하나를 더한다(0이 아닐 때만)."""
    identity = {
        "wav": hashlib.sha256(wav.read_bytes()).hexdigest(),
        "provider_origin": "FakeTimingProvider",
        "model": PRE_W18_DEFAULT.models.timing_model,
        "settings": asdict(PRE_W18_DEFAULT),
        "start": span.start,
        "end": span.end,
        "asr_epoch": epoch,
    }
    return hashlib.sha256(json.dumps(identity, sort_keys=True, default=str).encode()).hexdigest()


def test_epoch_zero_keeps_the_pre_19b_chunk_key(tmp_path: Path) -> None:
    cache = tmp_path / "asr-cache"
    _run(tmp_path, cache, 0)
    expected = {
        pre_w14_chunk_key(tmp_path / f"asr-chunk-{position:04d}.wav", span, PRE_W18_DEFAULT)
        for position, span in enumerate(SPANS)
    }
    assert {path.stem for path in cache.glob("*.json")} == expected


def test_a_raised_epoch_buys_every_chunk_once_and_keeps_the_old_answers(tmp_path: Path) -> None:
    cache = tmp_path / "asr-cache"
    assert len(_run(tmp_path, cache, 0).seen) == len(SPANS)
    old = {path.stem for path in cache.glob("*.json")}
    again = _run(tmp_path, cache, 1)
    assert len(again.seen) == len(SPANS)  # 다시 듣기 — 청크마다 한 번 새로 산다
    new = {path.stem for path in cache.glob("*.json")} - old
    assert new == {
        epoch_chunk_key(tmp_path / f"asr-chunk-{position:04d}.wav", span, 1)
        for position, span in enumerate(SPANS)
    }
    assert _run(tmp_path, cache, 1).seen == []  # 같은 차례는 다시 사지 않는다
    assert _run(tmp_path, cache, 0).seen == []  # 옛 답은 그대로 남는다


# ── 원본 입력 지문 — 차례 0은 19b 이전 조립 그대로 ────────────────────────────────


def pre_19b_input_fingerprint(**values: Any) -> str:
    """19b 이전 `source_input_fingerprint`의 조립 그대로(차례 칸이 없다)."""
    video, audio, settings, workflow = (
        values["video"],
        values["audio"],
        values["settings"],
        values["workflow"],
    )
    parts = [
        INPUT_FINGERPRINT_SCHEMA,
        media_refs.ORIGINAL_ONLY_FINGERPRINT,
        "asr-original/v1",
        UNDERSTANDING_SCHEMA_VERSION,
        values["source_sha256"],
        str(video.stream_index),
        _fraction_text(video.presentation_origin),
        _fraction_text(video.frame_duration),
        str(video.frame_count),
        "|".join(
            [
                str(audio.audio_stream_index),
                f"{audio.sample_rate}/{audio.channels}/{audio.sample_count}",
                _fraction_text(audio.pcm_sample0_file_time),
            ]
        ),
        "|".join(
            str(getattr(settings, name))
            for name in (
                "window_seconds",
                "context_seconds",
                "max_images_per_window",
                "image_long_edge",
                "prompt_revision",
                "asr_chunk_max_seconds",
                "asr_chunk_max_bytes",
            )
        ),
        workflow.models.timing_model,
        workflow.models.luna_model,
    ]
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()


def test_the_input_fingerprint_moves_only_for_a_raised_epoch(tmp_path: Path) -> None:
    values: dict[str, Any] = {
        "source_sha256": "a" * 64,
        "video": VideoGrid(0, Fraction(0), Fraction(1, 25), 500),
        "audio": make_audio(tmp_path),
        "settings": RawUnderstandingSettings(),
        "workflow": RawWorkflowConfig(),
    }
    pinned = pre_19b_input_fingerprint(**values)
    assert source_input_fingerprint(**values) == pinned
    assert source_input_fingerprint(**values, asr_epoch=0) == pinned
    raised = source_input_fingerprint(**values, asr_epoch=1)
    assert raised != pinned and raised != source_input_fingerprint(**values, asr_epoch=2)


# ── 작업대 — 결속·digest가 그대로이고, 다시 듣기는 그 원본만 새로 산다 ───────────────


def pre_19b_bindings(ui: Any, root: Path, asset_ids: tuple[str, ...]) -> dict[str, list[str]]:
    workspace = ui.raw._workspace(root)
    return {
        a: [source_transcript.fingerprint(workspace, a), media_refs.fingerprint(workspace, a)]
        for a in asset_ids
    }


GEMINI_TAG = "timing-engine/gemini"
"""Gemini 시간 엔진 원본의 결속 꼬리표(D92) — 실제(가짜) 흐름은 Gemini로 듣는다(D163)."""


def pre_19b_digest(ui: Any, root: Path, quote: Any) -> str:
    """19b 이전 `_assemble_quote`의 digest 조립 그대로 — 범위·차례 칸이 없다."""
    workspace = ui.raw._workspace(root)
    bindings = [
        (a, media_refs.fingerprint(workspace, a), source_transcript.fingerprint(workspace, a))
        for a in quote.asset_ids
    ]
    body = asr_estimate_digest(quote.estimates) + json.dumps(bindings)
    return hashlib.sha256(body.encode()).hexdigest()


def approve(ui: Any, mode: str) -> None:
    """사람이 범위를 고르고 일괄 승인 단추를 누른다(D110 — 견적 없이)."""
    signature = ui.current_signature()
    ui.approval_mode_view(signature, mode)
    ui.library_analysis_view(signature)


def stage(ui: Any, root: Path, asset_id: str) -> str | None:
    return (ui._task_for(root, (asset_id,)) or {}).get("stage")


def relisten_view(ui: Any, root: Path, asset_id: str) -> dict:
    return _model_of(ui.visual_workbench_render(root, source_id=asset_id)[1])["sources"][asset_id][
        "relisten"
    ]


def relisten(ui: Any, asset_id: str, *, token: bool = True) -> dict:
    payload = {"epoch": "e", "signature": ui.current_signature(), "sourceId": asset_id}
    if token:
        payload["token"] = ui._visual_lifetime_token
    function = {fn.__name__: fn for fn in ui._visual_server_functions()}["vl_relisten"]
    return function(payload)


@needs_ffmpeg
def test_relisten_rebuys_only_that_video_and_a_full_run_then_buys_no_transcription(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ui, sender, client, root, ids = setup_library(tmp_path, monkeypatch)
    client.resume.set()
    a, b = ids["a.mov"], ids["b.mov"]
    every = ui._library_ids(root)
    # 차례 0 — 결속은 19b 이전 조립에 Gemini 시간 엔진 꼬리표 하나만 더한 것이다(D92·D163 —
    # whisper 작업공간의 바이트 그대로는 `test_transcribe_only`가 본다).
    expected = {a_id: [*row, GEMINI_TAG] for a_id, row in pre_19b_bindings(ui, root, every).items()}
    assert ui.raw.source_bindings(root, dict.fromkeys(every)) == expected
    approve(ui, FULL)
    wait_until(lambda: all(stage(ui, root, i) == DONE for i in (a, b)))
    assert (len(sender.calls), len(client.calls)) == (2, 4)
    revision_a = ui._task_for(root, (a,))["revision"]
    assert relisten_view(ui, root, a) == {
        "pending": False,
        "edited": False,
        "heard": False,
        "explanation": workspace_service.RELISTEN_EXPLANATION,
        "engine": workspace_service.RELISTEN_ENGINE_SAME_TEXT.format(
            engine="Gemini 3.5 Transcribe"
        ),
    }

    refused = relisten(ui, a, token=False)
    assert refused["ok"] is False and asr_epoch.state(ui.raw._workspace(root), a).epoch == 0
    sender.segments = [{"start": 0.5, "end": 1.5, "text": RELISTENED}]
    reply = relisten(ui, a)
    assert reply["ok"] is True and reply["message"] == workspace_service.RELISTEN_DONE_TEXT
    assert _model_of(reply["html"])["sources"][a]["relisten"]["pending"] is True
    assert relisten(ui, a)["message"] == workspace_service.RELISTEN_PENDING_TEXT  # 두 번째는 그대로
    assert ui.raw.asr_epoch_state(root, a) == asr_epoch.AsrEpoch(1, 0)
    assert ui.raw.approval_mode(root) == TRANSCRIBE  # 다시 듣기는 「전사만」이 기본이다
    assert stage(ui, root, a) == raw.RELISTEN_STAGE and stage(ui, root, b) == DONE
    bound = ui.raw.source_bindings(root, dict.fromkeys(every))
    assert bound[b] == [*pre_19b_bindings(ui, root, (b,))[b], GEMINI_TAG]  # 다른 영상은 그대로
    assert bound[a] == [*pre_19b_bindings(ui, root, (a,))[a], "asr-epoch/1", GEMINI_TAG]
    assert ui.raw.cached_selection_analysis(root, (a,)) is None  # 끝난 분석을 다시 쓰지 않는다
    assert ui.raw.cached_selection_analysis(root, (b,)) is not None
    # 끝난 b와 남은 a가 섞였다 — 승인은 남은 a만 묶는다(D64 E).
    assert ui._library_pending(root) == (a,)
    assert len(sender.calls) == 2  # 누르기만 했다 — 유료 호출 0회

    approve(ui, TRANSCRIBE)
    wait_until(lambda: stage(ui, root, a) == raw.TRANSCRIBED_STAGE)
    assert (len(sender.calls), len(client.calls)) == (3, 4)  # a의 조각 하나만 새로 샀다
    transcript = ui.raw.source_transcript(root, a)
    assert [u["text"] for u in transcript["utterances"]] == [RELISTENED]
    assert transcript["edited"] is False and transcript["sequence"] == 1
    assert ui.raw.source_transcript(root, b)["sequence"] == 0
    assert ui.raw.asr_epoch_state(root, a) == asr_epoch.AsrEpoch(1, 1)  # 끝까지 들었다
    assert len(list((root / "cache" / "source-asr" / a).glob("*.json"))) == 2
    assert len(list((root / "cache" / "source-asr" / b).glob("*.json"))) == 1
    assert ui._task_for(root, (a,))["revision"] != revision_a
    # 검토 화면 — a는 판정 없이 「전사 완료 · 분석 대기」(지금 검토는 그대로), b는 등급 그대로.
    model = _model_of(ui.visual_workbench_render(root, source_id=a)[1])
    assert model["sources"][a]["analysisState"] == raw.TRANSCRIBED_STAGE
    assert model["sources"][a].get("suggestions", []) == []
    assert model["sources"][b]["analysisState"] == DONE and model["sources"][b]["suggestions"]
    assert model["sources"][a]["relisten"]["heard"] is True

    approve(ui, FULL)  # 마지막 전체 분석 — 1단계는 그대로 다시 쓴다
    wait_until(lambda: stage(ui, root, a) == DONE)
    # 전사 0회 추가. 영상 분석은 바뀐 말을 듣는 창 하나만 새로 묻는다 — 말이 없는 두 창은 요청이
    # 바이트까지 같아 창 판정 캐시가 그대로 맞는다.
    assert (len(sender.calls), len(client.calls)) == (3, 5)
    assert ui.raw.cached_selection_analysis(root, (a,)) is not None


@needs_ffmpeg
def test_relisten_never_overwrites_a_human_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ui, sender, client, root, ids = setup_library(tmp_path, monkeypatch)
    client.resume.set()
    a = ids["a.mov"]
    assert relisten(ui, a)["error"] == raw.RELISTEN_UNTRANSCRIBED_REFUSAL  # 처음 분석은 이미 0.6
    approve(ui, TRANSCRIBE)
    wait_until(lambda: stage(ui, root, a) == raw.TRANSCRIBED_STAGE)
    assert len(sender.calls) == 2
    assert ui.raw.relisten_source(root, a) == asr_epoch.AsrEpoch(1, 0)
    # 기다리는 동안 사람이 자막을 고치면 다시 듣기를 거둔다 — 고친 자막은 덮지 않으니 돈만 든다.
    document = ui.raw.source_transcript(root, a)
    cue = document["utterances"][0]
    ui.raw.edit_source_transcript(
        root, a, document["revision"], action="edit", utterance_id=cue["utterance_id"], text="고침"
    )
    assert ui.raw.asr_epoch_state(root, a) == asr_epoch.AsrEpoch(0, 0)
    with pytest.raises(RawWorkspaceError, match="직접 고친"):
        ui.raw.relisten_source(root, a)
    refused = relisten(ui, a)
    assert refused == {"ok": False, "error": raw.RELISTEN_EDITED_REFUSAL}
    assert ui.raw.asr_epoch_state(root, a) == asr_epoch.AsrEpoch(0, 0)
    # 차례를 억지로 올려 들어도(서비스 문 밖) 1단계 seed는 고친 자막을 그대로 둔다.
    asr_epoch.bump(ui.raw._workspace(root), a)
    sender.segments = [{"start": 0.5, "end": 1.5, "text": RELISTENED}]
    approve(ui, TRANSCRIBE)
    wait_until(lambda: len(sender.calls) == 3)
    wait_until(lambda: stage(ui, root, a) == raw.TRANSCRIBED_STAGE)
    kept = ui.raw.source_transcript(root, a)
    assert [u["text"] for u in kept["utterances"]] == ["고침"] and kept["edited"] is True


@needs_ffmpeg
def test_relisten_refuses_a_silent_video_and_reads_legacy_edits_like_the_fingerprint(
    tmp_path: Path,
) -> None:
    """검증 지적 3·6 — 소리 없는 영상은 들을 것이 없다(차례가 끝나지 않고 끝난 분석만 낡는다).

    `edited` 칸이 없는 옛 문서는 지문(`source_transcript.fingerprint`)처럼 둘째 리비전부터 고친
    것으로 읽는다 — 다시 듣기 거절도 같은 규칙(`human_edited`)이다.
    """
    ui = workspace_service.RawWorkspaceUi(port=7993, state_path=tmp_path / "state.json")
    ui.create_workspace_view("무성", str(tmp_path / "ws"))
    root = ui.workspace_root()
    silent = tmp_path / "silent.mov"
    _make_media(silent, audio=False, seconds="3")
    handle = ui.raw.import_selected_video(root, silent)
    assert _wait_terminal(ui.raw, root, handle.job_id).status == "succeeded"
    asset = ui.raw.list_source_assets(root)[0]
    workspace = ui.raw._workspace(root)
    with pytest.raises(RawWorkspaceError, match="아직 전사하지 않았습니다"):
        ui.raw.relisten_source(root, asset.asset_id)
    source_transcript.seed(workspace, asset.asset_id, [], Fraction(3), asset.content_sha256)
    payload = {"epoch": "e", "signature": ui.current_signature(), "sourceId": asset.asset_id}
    payload["token"] = ui._visual_lifetime_token
    function = {fn.__name__: fn for fn in ui._visual_server_functions()}["vl_relisten"]
    assert function(payload) == {"ok": False, "error": raw.RELISTEN_SILENT_REFUSAL}
    assert not _table_exists(root)  # 차례를 올리지 않았다 — 끝난 영상 분석이 그대로 쓰인다
    assert ui.raw.approval_mode(root) == FULL  # 범위도 바꾸지 않았다
    # 옛 문서: sequence 1, `edited` 칸 없음 — 지문은 고친 것으로 본다. 다시 듣기도 같은 규칙.
    legacy = {
        key: value
        for key, value in source_transcript.load(workspace, asset.asset_id).items()
        if key != "edited"
    }
    legacy.update(sequence=1, revision="legacy-revision")
    with sqlite3.connect(root / DB_NAME) as db:
        db.execute(
            "INSERT INTO source_transcripts VALUES(?,?,?,?)",
            (asset.asset_id, 1, "legacy-revision", json.dumps(legacy, ensure_ascii=False)),
        )
    assert source_transcript.fingerprint(workspace, asset.asset_id) != "asr-original/v1"
    assert source_transcript.human_edited(source_transcript.load(workspace, asset.asset_id))
    assert ui.raw.transcript_human_edited(legacy) is True
    with pytest.raises(RawWorkspaceError, match="직접 고친"):
        ui.raw.relisten_source(root, asset.asset_id)
    assert not _table_exists(root)


@needs_ffmpeg
def test_relisten_on_a_missing_original_refuses_without_paths_and_changes_nothing(
    tmp_path: Path,
) -> None:
    """19b 2차 검증 — 원본을 못 읽으면 경로 없는 존댓말로 거절하고 차례를 올리지 않는다."""
    ui = workspace_service.RawWorkspaceUi(port=7993, state_path=tmp_path / "state.json")
    ui.create_workspace_view("없는 원본", str(tmp_path / "ws"))
    root = ui.workspace_root()
    media = tmp_path / "talk.mov"
    _make_media(media, audio=True, seconds="3")
    handle = ui.raw.import_selected_video(root, media)
    assert _wait_terminal(ui.raw, root, handle.job_id).status == "succeeded"
    asset = ui.raw.list_source_assets(root)[0]
    workspace = ui.raw._workspace(root)
    source_transcript.seed(workspace, asset.asset_id, [], Fraction(3), asset.content_sha256)
    original = workspace.source_media_path(asset)
    original.rename(tmp_path / "moved.mov")
    with pytest.raises(RawWorkspaceError) as refused:
        ui.raw.relisten_source(root, asset.asset_id)
    assert str(refused.value) == raw.RELISTEN_MEDIA_MISSING_REFUSAL
    assert str(original) not in str(refused.value) and "ffprobe" not in str(refused.value)
    assert not _table_exists(root)  # 차례를 올리지 않았다
