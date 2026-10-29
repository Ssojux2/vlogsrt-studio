"""D59 19d — 「다른 귀로 교차 확인」: 설정 · 견적 · 승인 v2 · 결속, 그리고 19c 검증이 남긴 (a)–(e).

유료 호출은 없다 — 두 귀(whisper·gpt-transcribe)는 요청을 세는 가짜 전송이 받는다.
시험이 못 박는 것:

- 설정은 작업공간 meta 한 칸이고 기본은 켜짐이다. 꺼져 있으면 견적·digest·결속·승인이 19d 이전
  바이트 그대로다.
- 켜져 있으면 견적은 「아직 답이 없는 청크」만 글자 분량(분 × 공식 단가)으로 세고, 승인(v2)이 그
  수·글자 모델·요청 신원을 묶는다. 1단계 승인의 설정 지문과 whisper 청크 열쇠는 그대로다.
- 이미 들은 원본의 「전사만」은 whisper 0회(캐시) · 글자 N회다. 글자 실패는 whisper를 막지 않는다.
- (a) 글자가 자막 지문을 바꾼 전체 실행도 다음 견적이 끝난 분석으로 다시 찾는다.
- (b) 견적이 재분석 결과를 말한다. (c) 접은 글자가 같은 답은 바꾸지 않는다. (d) 이어 가기는 출처까지
  대조한다. (e) 이번에 바꾼 줄과 이어 간 줄을 따로 센다.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
from dataclasses import asdict, replace
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip(
    "gradio",
    reason="웹 UI extra(gradio)가 없다 — ui extra를 설치하면 이 묶음이 활성화된다",
    exc_type=ImportError,
)

import gradio as gr  # noqa: E402

from tests.raw.test_background_library import setup_library  # noqa: E402
from tests.raw.test_relisten import approve, pre_19b_bindings, pre_19b_digest, stage  # noqa: E402
from tests.raw.test_selection_analysis import wait_until  # noqa: E402
from tests.raw.test_source_text import GPT, TERMS, RecordingSender, _pass  # noqa: E402
from tests.raw.test_workbench_layout import _GUARD_HARNESS, needs_node  # noqa: E402
from ui import ui_facade, workspace_service  # noqa: E402
from ui.workspace_service import (  # noqa: E402
    APPROVAL_MODE_ID,
    CROSS_CHECK_ID,
    CROSS_CHECK_INFO,
    CROSS_CHECK_LABEL,
    LIBRARY_APPROVAL_JOB_KIND,
    RawWorkspaceUi,
)
from vlogsrt import log  # noqa: E402
from vlogsrt.project.raw import (  # noqa: E402
    browser_runtime,
    cross_check,
    glossary,
    selection_analysis,
    source_text,
    source_transcript,
    text_ear,
)
from vlogsrt.project.raw import service as raw  # noqa: E402
from vlogsrt.project.raw.analysis_approval import (  # noqa: E402
    ASR_APPROVAL_SCHEMA,
    AsrPhaseApproval,
    asr_settings_fingerprint,
)
from vlogsrt.project.raw.analysis_config import (  # noqa: E402
    TRANSCRIPTION_IDENTITY_PROMPT_REVISION,
    UNDERSTANDING_SCHEMA_VERSION,
    RawUnderstandingSettings,
)
from vlogsrt.project.raw.config import (  # noqa: E402
    SOURCE_TEXT_PROMPT,
    SOURCE_TEXT_SETTINGS,
    RawModelRoles,
    RawWorkflowConfig,
    transcription_identity_view,
)
from vlogsrt.project.raw.models import RawWorkspaceError  # noqa: E402
from vlogsrt.project.raw.understanding import SampleSpan, utterances_json  # noqa: E402
from vlogsrt.project.raw.understanding_luna import ORIGIN_LIVE, ORIGIN_REPLAY  # noqa: E402
from vlogsrt.project.raw.workspace import DB_NAME  # noqa: E402
from vlogsrt.transcribe import CAPTION_TEXT_KIND  # noqa: E402

# D104 — whisper 응답 모양의 재생으로 재는 시험이다. 새로 듣는 엔진을 whisper로 둔다.
pytestmark = pytest.mark.usefixtures("whisper_for_new_hearings")


@pytest.fixture(autouse=True)
def _gpt_ear_for_new_hearings(monkeypatch: pytest.MonkeyPatch) -> None:
    """이 파일은 gpt 다른 귀(D59 19d)의 견적·승인·흐름을 시험한다. W-35b부터 처음 듣는 whisper
    영상의 글자 귀는 Gemini다(D95) — gpt 귀는 그 전에 들은 영상의 귀로 남는다. 여기서는 처음 듣는
    영상도 gpt 귀로 듣게 해 그 흐름을 그대로 잰다(Gemini 글자 경로는 `test_gemini_text*.py`)."""
    monkeypatch.setattr(text_ear, "DEFAULT_NEW_HEARING_EAR", text_ear.EAR_GPT)


needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")

TRANSCRIBE = raw.SELECTION_MODE_TRANSCRIBE
FULL = raw.SELECTION_MODE_FULL
DONE = "추천 구간 준비 완료"
WHISPER_TEXT = "합성 영상입니다."
GPT_TEXT = "합성 영상이에요."
TEXT_MODEL = RawModelRoles().text_model
POLITE_END = re.compile(r"(니다|세요|까요)[.)]*$")


class EarsSender:
    """두 귀의 가짜 전송 — whisper(시각)와 두 번째 귀(글자)를 따로 센다(유료 호출 없음)."""

    def __init__(self, *, fail_text: bool = False) -> None:
        self.whisper: list[Any] = []
        self.text: list[Any] = []
        self.fail_text = fail_text

    def send_request(self, request: Any, cfg: Any, *, kind: str, stem: str, source: str) -> Any:
        if kind == CAPTION_TEXT_KIND:
            self.text.append(request)
            if self.fail_text:
                raise RuntimeError("두 번째 귀가 닫혀 있다")
            return {"text": GPT_TEXT, "languages": [{"code": "ko"}]}
        self.whisper.append(request)
        return {"segments": [{"start": 0.5, "end": 1.5, "text": WHISPER_TEXT}]}


def _library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, on: bool, sender: EarsSender | None = None
) -> tuple[RawWorkspaceUi, EarsSender, Any, Path, dict[str, str]]:
    """영상 둘(32초·3초)의 작업대 — 흐름 시험 공용 재료는 교차 확인을 끈 채 온다(19d 이전 흐름)."""
    ui, _counting, client, root, ids = setup_library(tmp_path, monkeypatch)
    client.resume.set()
    ears = sender or EarsSender()
    ui._live_transport = replace(ui._live_transport, sender=ears)
    assert ui.raw.cross_check_enabled(root) is False
    if on:
        _turn(ui, root, True)
    return ui, ears, client, root, ids


def _turn(ui: RawWorkspaceUi, root: Path, on: bool) -> None:
    """사람이 칸을 누른다 — 저장 · 견적 다시 묶기 · 라이브러리 견적 다시 재기(유료 호출 0회)."""
    ui.cross_check_view(ui.current_signature(), on)
    assert ui.raw.cross_check_enabled(root) is on
    _settled(ui, root)


def _settled(ui: RawWorkspaceUi, root: Path) -> None:
    kinds = (
        raw.ANALYSIS_JOB_KIND,
        "selection_analysis",
        "library_quote",
        LIBRARY_APPROVAL_JOB_KIND,
    )
    wait_until(lambda: not ui.raw.active_jobs(root, kinds))


def _requote(ui: RawWorkspaceUi, root: Path) -> str:
    ui._prepare_library_quote(root)
    _settled(ui, root)
    return ui.library_analysis_summary()


def _phase(root: Path, asset_id: str, revision: str) -> dict[str, Any]:
    (path,) = [
        p
        for p in (root / "analysis" / revision).glob(f"*_{asset_id}/asr_phase.json")
        if p.is_file()
    ]
    return json.loads(path.read_text(encoding="utf-8"))


def _text_request_rows(root: Path) -> int:
    with sqlite3.connect(root / DB_NAME) as db:
        return int(
            db.execute(
                "SELECT COUNT(*) FROM analysis_requests WHERE stage=?",
                (source_text.TEXT_REQUEST_STAGE,),
            ).fetchone()[0]
        )


def _assert_whisper_has_no_hints(ears: EarsSender) -> None:
    """규칙 6 — 교차 확인이 켜져 있고 용어집이 있어도 whisper 요청에는 힌트 칸이 비어 있다."""
    timing = RawModelRoles().timing_model
    for request in ears.whisper:
        assert request.model == timing and request.response_format == "verbose_json"
        assert (request.keywords, request.languages, request.prompt) == ((), (), "")


def pre_19d_estimate_digest(estimates: Any) -> str:
    """19d 이전 `asr_estimate_digest`의 조립 그대로 — 글자 분량 칸이 없다."""
    rows = sorted(
        (
            item.asset_id,
            item.source_order,
            item.source_name,
            bool(item.has_audio),
            item.audio_stream_index,
            item.sample_count,
            item.window_count,
            item.asr_chunk_count,
            item.luna_request_count,
            *(() if item.ceiling_usd is None else (item.ceiling_usd,)),
        )
        for item in estimates
    )
    body = json.dumps(rows, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def pre_19d_asr_settings_fingerprint(
    settings: RawUnderstandingSettings, workflow: RawWorkflowConfig
) -> str:
    """19d 이전 `asr_settings_fingerprint`의 조립 그대로 — 승인 schema v1 문자열이 맨 앞이다."""
    workflow = transcription_identity_view(workflow)
    parts = [
        "raw-analysis-asr-approval/v1",
        UNDERSTANDING_SCHEMA_VERSION,
        str(settings.window_seconds),
        str(settings.context_seconds),
        str(settings.max_images_per_window),
        str(settings.image_long_edge),
        TRANSCRIPTION_IDENTITY_PROMPT_REVISION,
        str(settings.asr_chunk_max_seconds),
        str(settings.asr_chunk_max_bytes),
        workflow.models.timing_model,
        workflow.models.luna_model,
    ]
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()


# ── 설정 — meta 한 칸, 기본 켜짐, 설정(RawWorkflowConfig) 밖 ─────────────────────────────


def test_the_setting_defaults_on_survives_reopening_and_stays_outside_the_workflow_config(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ws"
    service = raw.RawWorkspaceService()
    service.create_workspace(root, "교차 확인")
    workspace = service._workspace(root)
    assert service.cross_check_enabled(root) is True  # 고른 적이 없으면 켜짐(사용자 결정)
    before = asdict(service.settings(root))
    assert service.set_cross_check(root, False) is False
    assert raw.RawWorkspaceService().cross_check_enabled(root) is False  # 다시 열어도 남는다
    assert cross_check.binding_tag(workspace) is None
    service.set_cross_check(root, True)
    tag = cross_check.binding_tag(workspace)
    assert tag == cross_check.TAG_PREFIX + cross_check.request_identity(workspace)
    assert asdict(service.settings(root)) == before  # 설정·whisper 열쇠의 재료는 그대로
    glossary.save(workspace, "왕방\n백숙\n")
    assert cross_check.binding_tag(workspace) != tag  # 용어집이 바뀌면 꼬리표가 바뀐다
    with pytest.raises(RawWorkspaceError):
        cross_check.set_enabled(workspace, "off")  # type: ignore[arg-type]


def test_the_request_identity_binds_model_languages_prompt_and_glossary() -> None:
    digest = glossary.glossary_sha(TERMS)
    base = source_text.text_identity(SOURCE_TEXT_SETTINGS, digest)
    assert base == source_text.call_identity(TEXT_MODEL, SOURCE_TEXT_PROMPT, TERMS, ("ko",))
    moved = {
        source_text.text_identity(replace(SOURCE_TEXT_SETTINGS, model="other"), digest),
        source_text.text_identity(replace(SOURCE_TEXT_SETTINGS, languages=("en",)), digest),
        source_text.text_identity(replace(SOURCE_TEXT_SETTINGS, prompt="다른 문맥"), digest),
        source_text.text_identity(SOURCE_TEXT_SETTINGS, glossary.glossary_sha(())),
    }
    assert base not in moved and len(moved) == 4


# ── 승인 v2 — 글자 칸 셋, 설정 지문·whisper 열쇠는 그대로 ─────────────────────────────


def test_approval_v2_adds_the_text_fields_and_keeps_the_settings_fingerprint_bytes() -> None:
    assert ASR_APPROVAL_SCHEMA == "raw-analysis-asr-approval/v2"
    understanding, workflow = RawUnderstandingSettings(), RawWorkflowConfig()
    assert asr_settings_fingerprint(understanding, workflow) == pre_19d_asr_settings_fingerprint(
        understanding, workflow
    )
    legacy = AsrPhaseApproval(
        approved=True,
        source_sha256="a" * 64,
        audio_stream_index=1,
        chunk_budget=1,
        settings_fingerprint=asr_settings_fingerprint(understanding, workflow),
        timing_model=workflow.models.timing_model,
        approved_on="2026-10-12",
    )
    # 글자 칸을 주지 않은 승인(교차 확인 꺼짐)은 19d 이전 모양 — 글자 요청이 없다.
    assert (legacy.text_model, legacy.text_identity, legacy.text_chunk_budget) == (None, None, 0)


# ── 꺼짐 — 견적·digest·결속·승인이 19d 이전 바이트 그대로, 글자 요청 0 ─────────────────────


@needs_ffmpeg
def test_off_keeps_quotes_digests_bindings_and_approvals_as_before_19d(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ui, ears, _client, root, ids = _library(tmp_path, monkeypatch, on=False)
    measured = ui._library_quotes[root]
    every = tuple(measured.asset_ids)
    assert all(
        (e.text_chunk_count, e.text_seconds, e.text_usd) == (None, None, None)
        for e in measured.estimates
    )
    assert browser_runtime.asr_estimate_digest(measured.estimates) == pre_19d_estimate_digest(
        measured.estimates
    )
    assert measured.digest == pre_19b_digest(ui, root, measured)
    assert ui.raw.source_bindings(root, dict.fromkeys(every)) == pre_19b_bindings(ui, root, every)
    for mode in (FULL, TRANSCRIBE):
        scoped = ui.raw.selection_rebound_quote(root, measured, every, mode)
        assert "다른 귀" not in ui._library_summary_for(root, scoped)
    minted = browser_runtime.mint_asr_approvals(ui.raw, root, every, quoted=measured.estimates)
    for approval in minted.values():
        assert (approval.text_model, approval.text_identity, approval.text_chunk_budget) == (
            None,
            None,
            0,
        )
    glossary.save(ui.raw._workspace(root), "왕방\n")
    approve(ui, TRANSCRIBE)
    wait_until(lambda: all(stage(ui, root, i) == raw.TRANSCRIBED_STAGE for i in ids.values()))
    _settled(ui, root)
    assert (len(ears.whisper), len(ears.text)) == (2, 0)
    _assert_whisper_has_no_hints(ears)


# ── 켜짐 — 견적은 답이 없는 청크만, 승인이 그 수·모델·신원을 묶는다 ─────────────────────────


def _cache_live_answer(ui: RawWorkspaceUi, root: Path, asset_id: str) -> None:
    """실제 실행이 이미 산 글자 답(출처 live)을 그 원본의 청크 0 자리에 둔다."""
    revision = ui._task_for(root, (asset_id,))["revision"]
    chunk = _phase(root, asset_id, revision)["asr"]["chunks"][0]
    key = source_text.text_chunk_key(
        chunk["wav_sha256"],
        SOURCE_TEXT_SETTINGS,
        glossary.current(ui.raw._workspace(root)).sha,
        SampleSpan(chunk["sample_start"], chunk["sample_end"]),
        ORIGIN_LIVE,
    )
    cache = root / "cache" / source_text.TEXT_CACHE_DIRNAME / asset_id
    cache.mkdir(parents=True, exist_ok=True)
    body = {"key": key, "text": GPT_TEXT, "languages": ["ko"]}
    (cache / f"{key}.json").write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")


@needs_ffmpeg
def test_on_quotes_text_minutes_only_for_chunks_without_a_cached_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ui, ears, _client, root, ids = _library(tmp_path, monkeypatch, on=False)
    a, b = ids["a.mov"], ids["b.mov"]
    off = ui._library_quotes[root]
    approve(ui, TRANSCRIBE)  # 1단계 기록(청크 WAV·범위)이 생긴다 — 아직 글자는 묻지 않는다
    wait_until(lambda: all(stage(ui, root, i) == raw.TRANSCRIBED_STAGE for i in (a, b)))
    _settled(ui, root)
    _cache_live_answer(ui, root, a)
    _turn(ui, root, True)
    quote = ui._library_quotes[root]
    by_id = {e.asset_id: e for e in quote.estimates}
    assert by_id[a].text_chunk_count == 0 and Decimal(str(by_id[a].text_usd)) == 0
    chunk = _phase(root, b, ui._task_for(root, (b,))["revision"])["asr"]["chunks"][0]
    seconds = Fraction(chunk["sample_end"] - chunk["sample_start"], 16000)
    assert (by_id[b].text_chunk_count, Fraction(str(by_id[b].text_seconds))) == (1, seconds)
    assert Decimal(str(by_id[b].text_usd)) == cross_check.text_cost(seconds)
    assert quote.digest != off.digest  # 글자 분량·꼬리표가 digest에 접혔다
    only = ui.raw.selection_rebound_quote(root, quote, tuple(quote.asset_ids), TRANSCRIBE)
    summary = ui._library_summary_for(root, only)
    assert (
        " 다른 귀(GPT-Transcribe)로 영상 1개 1분을 한 번 더 듣습니다(공식 단가로 약 $0.01)."
        " 바뀐 자막이 있는 창은 다음 영상 분석 때 다시 판정합니다. 최대 3개를" in summary
    )
    identity = cross_check.request_identity(ui.raw._workspace(root))
    approvals = browser_runtime.mint_asr_approvals(
        ui.raw, root, (a, b), quoted=quote.estimates, text_identity=identity
    )
    assert (approvals[a].text_model, approvals[a].text_chunk_budget) == (None, 0)  # 답이 있다
    assert (approvals[b].text_model, approvals[b].text_chunk_budget) == (TEXT_MODEL, 1)
    assert approvals[b].text_identity == identity
    # 승인은 견적이 센 바로 그 청크(글자 캐시 열쇠)만 묶는다 — 다른 청크로 상한을 채우지 못한다.
    assert by_id[b].text_chunk_keys is not None and len(by_id[b].text_chunk_keys) == 1
    assert approvals[b].text_chunk_keys == by_id[b].text_chunk_keys
    assert approvals[b].settings_fingerprint == approvals[a].settings_fingerprint
    assert approvals[b].settings_fingerprint == pre_19d_asr_settings_fingerprint(
        RawUnderstandingSettings(), ui.raw.settings(root)
    )
    # 견적을 주지 않거나(고급 탭 — 글자 분량을 보이지 않는다) 승인이 묶은 신원이 없으면 글자 칸이
    # 없다 — 주조는 지금 용어집을 다시 읽지 않는다(검증 지적 1).
    bare = browser_runtime.mint_asr_approvals(ui.raw, root, (a, b))
    unbound = browser_runtime.mint_asr_approvals(ui.raw, root, (a, b), quoted=quote.estimates)
    assert all(item.text_model is None for item in (*bare.values(), *unbound.values()))
    assert not ears.text  # 견적·승인 주조는 아무것도 보내지 않는다


# ── 이미 들은 원본의 「전사만」 — whisper 0회(캐시) · 글자 청크마다 한 번 ───────────────────


@needs_ffmpeg
def test_transcribe_only_cross_check_over_existing_sources_buys_text_once_and_no_whisper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ui, ears, client, root, ids = _library(tmp_path, monkeypatch, on=False)
    workspace = ui.raw._workspace(root)
    glossary.save(workspace, "왕방\n백숙\n")
    approve(ui, TRANSCRIBE)
    wait_until(lambda: all(stage(ui, root, i) == raw.TRANSCRIBED_STAGE for i in ids.values()))
    _settled(ui, root)
    assert (len(ears.whisper), len(ears.text)) == (2, 0)
    whisper_keys = {p.name for p in (root / "cache" / "source-asr").rglob("*.json")}
    _turn(ui, root, True)
    assert "다른 귀(GPT-Transcribe)로 영상 2개 1분을" in ui.library_analysis_summary()
    approve(ui, TRANSCRIBE)  # 보이는 견적 그대로 한 번 승인한다
    wait_until(lambda: len(ears.text) == 2)
    _settled(ui, root)
    assert len(ears.whisper) == 2  # whisper는 한 번도 더 사지 않았다(청크 캐시)
    assert {p.name for p in (root / "cache" / "source-asr").rglob("*.json")} == whisper_keys
    assert client.calls == []  # 영상 분석 0회
    _assert_whisper_has_no_hints(ears)
    for request in ears.text:  # 힌트는 두 번째 귀에게만 간다
        assert request.model == TEXT_MODEL and request.response_format == "json"
        assert (request.prompt, request.keywords, request.languages) == (
            SOURCE_TEXT_PROMPT,
            ("왕방", "백숙"),
            ("ko",),
        )
    assert _text_request_rows(root) == 2
    for asset_id in ids.values():
        assert stage(ui, root, asset_id) == raw.TRANSCRIBED_STAGE
        (row,) = source_transcript.load(workspace, asset_id)["utterances"]  # type: ignore[index]
        assert (row["text"], row["whisper_text"], row["text_source"], row["text_origin"]) == (
            GPT_TEXT,
            WHISPER_TEXT,
            TEXT_MODEL,
            ORIGIN_LIVE,
        )
        record = _phase(root, asset_id, ui._task_for(root, (asset_id,))["revision"])["asr_text"]
        assert (record["status"], record["origin"], record["replaced"], record["carried"]) == (
            source_text.PASS_APPLIED,
            ORIGIN_LIVE,
            1,
            0,
        )
    # 산 글자는 캐시에 있다 — 다음 견적은 더 묻지 않는다(같은 용어집이면 다시 사지 않는다).
    assert "다른 귀" not in _requote(ui, root)


# ── 글자 실패 · 지어낸 승인 — whisper는 그대로, 글자는 보내기 전에 닫히거나 실패로 남는다 ──────


@needs_ffmpeg
def test_a_text_failure_stays_unavailable_and_never_blocks_whisper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    warnings: list[str] = []
    monkeypatch.setattr(log, "warn", warnings.append)
    ui, ears, _client, root, ids = _library(
        tmp_path, monkeypatch, on=True, sender=EarsSender(fail_text=True)
    )
    approve(ui, TRANSCRIBE)
    wait_until(lambda: all(stage(ui, root, i) == raw.TRANSCRIBED_STAGE for i in ids.values()))
    _settled(ui, root)
    assert (len(ears.whisper), len(ears.text)) == (2, 2)
    workspace = ui.raw._workspace(root)
    for asset_id in ids.values():
        (row,) = source_transcript.load(workspace, asset_id)["utterances"]  # type: ignore[index]
        assert row["text"] == WHISPER_TEXT and "text_source" not in row  # whisper 글자 그대로
        assert source_transcript.fingerprint(workspace, asset_id) == "asr-original/v1"
        phase = _phase(root, asset_id, ui._task_for(root, (asset_id,))["revision"])
        assert phase["asr_text"]["status"] == source_text.STATUS_UNAVAILABLE
        assert all(chunk["failure"] == "" for chunk in phase["asr"]["chunks"])  # 전사는 성공
    assert sum("1단계 글자 경로 청크 0 실패" in line for line in warnings) == 2
    # 실패는 캐시하지 않는다 — 다음 견적이 그 청크를 다시 세고, 그 승인이 그 청크만 다시 묻는다
    # (같은 리비전의 1단계 기록을 그대로 쓰지 않는다 — whisper는 청크 캐시에서 온다).
    _requote(ui, root)
    assert [e.text_chunk_count for e in ui._library_quotes[root].estimates] == [1, 1]
    ears.fail_text = False
    approve(ui, TRANSCRIBE)
    wait_until(lambda: len(ears.text) == 4)
    _settled(ui, root)
    assert len(ears.whisper) == 2
    for asset_id in ids.values():
        assert stage(ui, root, asset_id) == raw.TRANSCRIBED_STAGE
        (row,) = source_transcript.load(workspace, asset_id)["utterances"]  # type: ignore[index]
        assert (row["text"], row["text_origin"]) == (GPT_TEXT, ORIGIN_LIVE)
    assert "다른 귀" not in _requote(ui, root)


@needs_ffmpeg
@pytest.mark.parametrize("forged", ["forged", "omitted"])
def test_a_forged_or_omitted_text_model_is_refused_before_any_text_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, forged: str
) -> None:
    warnings: list[str] = []
    monkeypatch.setattr(log, "warn", warnings.append)
    ui, ears, _client, root, ids = _library(tmp_path, monkeypatch, on=True)

    def minted(estimate: Any, text_model: str, identity: str) -> dict[str, Any]:
        if forged == "omitted":
            return {}
        return {"text_model": "forged-model", "text_identity": identity, "text_chunk_budget": 1}

    monkeypatch.setattr(browser_runtime, "_text_approval_fields", minted)
    approve(ui, TRANSCRIBE)
    wait_until(lambda: all(stage(ui, root, i) == raw.TRANSCRIBED_STAGE for i in ids.values()))
    _settled(ui, root)
    assert (len(ears.whisper), len(ears.text)) == (2, 0)  # whisper는 그대로, 글자는 0송신
    refused = [line for line in warnings if "글자 요청을 보내지 않습니다" in line]
    assert len(refused) == (2 if forged == "forged" else 0)


# ── 검증 지적 1 — 승인이 묶은 용어집만, 승인 뒤에 저장된 용어집은 보내지 않는다 ─────────────────


class GlossaryEars(EarsSender):
    """두 번째 귀의 답이 용어집에 따라 다르다 — 「왕방」을 실으면 다른 글자를 돌려준다."""

    def send_request(self, request: Any, cfg: Any, *, kind: str, stem: str, source: str) -> Any:
        if kind == CAPTION_TEXT_KIND and "왕방" in tuple(request.keywords or ()):
            self.text.append(request)
            return {"text": GLOSSARY_TEXT, "languages": [{"code": "ko"}]}
        return super().send_request(request, cfg, kind=kind, stem=stem, source=source)


GLOSSARY_TEXT = "합성 영상이네요."


class _Context:
    """실행 문을 직접 부르는 시험의 작업 맥락 — 취소 없음, 진행은 버린다."""

    job_id = "cross-check-test"

    def check_cancel(self) -> None:
        return None

    def set_total(self, total: int) -> None:
        return None

    def set_progress(self, current: int) -> None:
        return None


def test_the_execute_gate_reads_the_identity_the_approved_digest_bound() -> None:
    def quoted(tag: str | None) -> selection_analysis.SelectionQuote:
        return selection_analysis.SelectionQuote(
            ("a",), (), "d", RawWorkflowConfig(), cross_tag=tag
        )

    identity = "i" * 64
    assert selection_analysis._approved_text_identity(quoted(None), identity) is None  # 꺼짐
    bound = quoted(cross_check.TAG_PREFIX + identity)
    assert selection_analysis._approved_text_identity(bound, identity) == identity
    with pytest.raises(RawWorkspaceError, match="용어집이 바뀌었습니다"):  # 다시 재는 사이 저장됐다
        selection_analysis._approved_text_identity(bound, "j" * 64)


@needs_ffmpeg
def test_a_glossary_saved_after_the_digest_check_is_never_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """실행 문이 digest를 다시 잰 뒤(승인 주조 앞) 저장된 용어집은 승인 밖이다 — 주조는 digest가
    묶은 신원으로만 글자 칸을 채우고, 원본의 글자 문이 지금 요청(새 용어집)과 달라 0송신이다.
    whisper는 그대로 간다."""
    warnings: list[str] = []
    monkeypatch.setattr(log, "warn", warnings.append)
    ui, ears, _client, root, ids = _library(tmp_path, monkeypatch, on=True, sender=GlossaryEars())
    workspace = ui.raw._workspace(root)
    approved_identity = cross_check.request_identity(workspace)
    a = ids["a.mov"]
    child = ui.raw.selection_rebound_quote(root, ui._library_quotes[root], (a,), TRANSCRIBE)
    assert (
        child.estimates[0].text_chunk_count == 1
    )  # 사람이 본 견적 — 빈 용어집으로 한 번 더 듣는다
    real_prepare = browser_runtime.prepare_approved_browser_network
    real_mint = browser_runtime.mint_asr_approvals
    minted: list[dict[str, AsrPhaseApproval]] = []

    def saved_after_the_check() -> None:
        glossary.save(workspace, "왕방\n")  # 실행 문의 digest 대조는 이미 지났다
        real_prepare()

    def recording_mint(*args: Any, **kwargs: Any) -> dict[str, AsrPhaseApproval]:
        minted.append(real_mint(*args, **kwargs))
        return minted[-1]

    monkeypatch.setattr(browser_runtime, "prepare_approved_browser_network", saved_after_the_check)
    monkeypatch.setattr(browser_runtime, "mint_asr_approvals", recording_mint)
    approved = replace(child, approved=True)
    ui.raw.run_selection_analysis(root, approved, ui._live_transport, _Context(), lambda *_: None)
    assert (len(ears.whisper), len(ears.text)) == (1, 0)  # 새 용어집은 한 번도 나가지 않았다
    ((_, approval),) = minted[0].items()
    assert approval.text_identity == approved_identity  # 지금 용어집을 다시 읽지 않았다
    assert sum("글자 요청을 보내지 않습니다" in line for line in warnings) == 1
    (row,) = source_transcript.load(workspace, a)["utterances"]  # type: ignore[index]
    assert row["text"] == WHISPER_TEXT and "text_source" not in row


@needs_ffmpeg
def test_a_glossary_saved_before_the_gate_closes_the_run_without_sends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """승인 뒤·실행 문 앞에 저장된 용어집 — 다시 잰 digest가 달라 아무것도 보내지 않는다."""
    ui, ears, _client, root, ids = _library(tmp_path, monkeypatch, on=True, sender=GlossaryEars())
    child = ui.raw.selection_rebound_quote(
        root, ui._library_quotes[root], (ids["a.mov"],), TRANSCRIBE
    )
    glossary.save(ui.raw._workspace(root), "왕방\n")
    with pytest.raises(RawWorkspaceError, match="바뀌었습니다"):
        ui.raw.run_selection_analysis(
            root, replace(child, approved=True), ui._live_transport, _Context(), lambda *_: None
        )
    assert (len(ears.whisper), len(ears.text)) == (0, 0)


# ── 검증 지적 2 — 용어집을 되돌려도 다른 글자로 판정한 분석은 다시 쓰지 않는다 ─────────────────


def _texts(ui: RawWorkspaceUi, root: Path, ids: dict[str, str]) -> dict[str, str]:
    workspace = ui.raw._workspace(root)
    return {
        asset_id: source_transcript.load(workspace, asset_id)["utterances"][0]["text"]  # type: ignore[index]
        for asset_id in ids.values()
    }


@needs_ffmpeg
def test_reverting_the_glossary_never_reuses_an_analysis_made_on_other_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ui, ears, client, root, ids = _library(tmp_path, monkeypatch, on=True, sender=GlossaryEars())
    workspace = ui.raw._workspace(root)
    approve(ui, FULL)  # R1 — 빈 용어집, 글자 X1
    wait_until(lambda: all(stage(ui, root, i) == DONE for i in ids.values()))
    _settled(ui, root)
    first = {i: ui.raw.cached_selection_analysis(root, (i,)) for i in ids.values()}
    assert all(first.values()) and set(_texts(ui, root, ids).values()) == {GPT_TEXT}
    glossary.save(workspace, "왕방\n")  # R2 — 꼬리표가 바뀌어 다시 듣는다(글자 X2)
    assert _requote(ui, root).startswith("분석 대상 2개 · 저장 결과 0개 재사용")
    approve(ui, FULL)
    wait_until(lambda: len(ears.text) == 4)
    wait_until(lambda: all(stage(ui, root, i) == DONE for i in ids.values()))
    _settled(ui, root)
    assert set(_texts(ui, root, ids).values()) == {GLOSSARY_TEXT}
    sent = (len(ears.whisper), len(ears.text), len(client.calls))
    glossary.save(workspace, "")  # 되돌린다 — 꼬리표는 R1과 같아진다
    reverted = _requote(ui, root)
    # R1은 X1로 판정했는데 지금 심긴 글자는 X2다 — 저장 결과로 세지 않는다(검증 지적 2).
    assert reverted.startswith("분석 대상 2개 · 저장 결과 0개 재사용") and "다른 귀" not in reverted
    assert all(ui.raw.cached_selection_analysis(root, (i,)) is None for i in ids.values())
    approve(ui, FULL)
    wait_until(lambda: set(_texts(ui, root, ids).values()) == {GPT_TEXT})
    wait_until(lambda: all(stage(ui, root, i) == DONE for i in ids.values()))
    _settled(ui, root)
    # 다시 들어도 전부 캐시다 — whisper·글자·영상 분석 모두 0회 추가(유료 0).
    assert (len(ears.whisper), len(ears.text), len(client.calls)) == sent
    assert {i: ui.raw.cached_selection_analysis(root, (i,)) for i in ids.values()} == first


# ── (a) 결속 — 글자가 지문을 바꾼 전체 실행도 다음 견적이 다시 찾는다 ───────────────────────


@needs_ffmpeg
def test_a_full_run_whose_text_flips_the_transcript_is_found_again_by_the_next_quote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ui, ears, client, root, ids = _library(tmp_path, monkeypatch, on=True)
    approve(ui, FULL)
    wait_until(lambda: all(stage(ui, root, i) == DONE for i in ids.values()))
    _settled(ui, root)
    assert (len(ears.whisper), len(ears.text), len(client.calls)) == (2, 2, 4)
    workspace = ui.raw._workspace(root)
    for asset_id in ids.values():
        revision = ui._task_for(root, (asset_id,))["revision"]
        # 글자가 실려 자막 지문은 발화 해시로 바뀌었다(19c-5) — 그래도 결속은 같다.
        assert source_transcript.fingerprint(workspace, asset_id) != "asr-original/v1"
        assert ui.raw.cached_selection_analysis(root, (asset_id,)) == revision
        analysis = ui.raw.analysis_document(root, revision, asset_id)
        assert analysis is not None
        assert analysis["transcript_fingerprint"] == source_transcript.fingerprint(
            workspace, asset_id
        )
    assert _requote(ui, root).startswith("분석 대상 0개 · 저장 결과 2개 재사용")
    # 끄면 19d 이전 결속(실제 자막 지문)이다 — 19c-5의 한계 그대로라 그 분석을 찾지 못한다.
    _turn(ui, root, False)
    assert ui.raw.cached_selection_analysis(root, (ids["a.mov"],)) is None


# ── (b) 견적이 재분석 결과를 말한다 ─────────────────────────────────────────────────────


@needs_ffmpeg
def test_the_quote_tells_what_cross_checking_finished_analyses_means(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ui, ears, client, root, ids = _library(tmp_path, monkeypatch, on=False)
    approve(ui, FULL)
    wait_until(lambda: all(stage(ui, root, i) == DONE for i in ids.values()))
    _settled(ui, root)
    assert (len(ears.whisper), len(ears.text), len(client.calls)) == (2, 0, 4)
    assert _requote(ui, root).startswith("분석 대상 0개 · 저장 결과 2개 재사용 · 전사 최대 0회")
    _turn(ui, root, True)  # 끝난 분석은 두 번째 귀로 들은 적이 없다 — 다시 분석 대상이다
    quote = ui._library_quotes[root]
    windows = sum(e.luna_request_count for e in quote.estimates)
    full = ui.library_analysis_summary()
    assert full.startswith(
        f"분석 대상 2개 · 저장 결과 0개 재사용 · 전사 최대 2회 + 영상 분석 최대 {windows}회."
    )
    assert windows > 0  # 그 창들이 영상 분석 견적(횟수·상한)에 들어 있다
    assert (
        " 다른 귀(GPT-Transcribe)로 영상 2개 1분을 한 번 더 듣습니다(공식 단가로 약 $0.01)"
        " — 바뀐 자막이 있는 창도 이번 영상 분석이 판정하며 위 견적에 들어 있습니다." in full
    )
    ui.approval_mode_view(ui.current_signature(), TRANSCRIBE)
    only = ui.library_analysis_summary()
    assert only.startswith("전사만 · 대상 영상 2개 · 저장 결과 0개 재사용")
    assert "바뀐 자막이 있는 창은 다음 영상 분석 때 다시 판정합니다." in only
    assert not ears.text and len(client.calls) == 4  # 견적은 아무것도 보내지 않는다


# ── (c)·(d)·(e) — 얹기의 세 규칙(순수 단위, 가짜 전송) ──────────────────────────────────


FOLD_EQUAL = {
    0: "망원동 카페에 왔어요. 정말 맛있네! 치즈",
    1: "개좋아 여기 진짜 좋다. 저기 봐봐 사진 찍자",
}
"""두 청크 모두 공백·부호만 다른 답(19c 픽스처의 원래 글자) — 글자가 실제로 바뀌는 줄이 없다."""


def test_c_a_fold_equal_answer_is_agreed_and_never_flips_the_fingerprint(tmp_path: Path) -> None:
    result, whisper, _ = _pass(tmp_path, RecordingSender(FOLD_EQUAL))
    first = next(u for u in result.utterances if u.whisper_text == " 망원동 카페에 왔어요")
    assert (first.text, first.text_source, first.retext_reason) == (
        " 망원동 카페에 왔어요",  # '망원동 카페에 왔어요.'는 접으면 같다 — whisper 글자를 둔다
        source_transcript.TEXT_SOURCE_WHISPER,
        source_text.REASON_AGREED,
    )
    assert [u.text for u in result.utterances] == [u.text for u in whisper]
    assert result.record["replaced"] == 0
    root = tmp_path / "ws"
    service = raw.RawWorkspaceService()
    service.create_workspace(root, "접은 글자")
    workspace = service._workspace(root)
    rows = utterances_json(result.utterances)
    source_transcript.seed(workspace, "src", rows, Fraction(20), "a" * 64)
    assert all("text_source" in row for row in rows)  # 증거는 남는다
    assert source_transcript.fingerprint(workspace, "src") == "asr-original/v1"  # 지문은 그대로


def _seeded_live(tmp_path: Path) -> tuple[Any, tuple[dict[str, object], ...]]:
    bought, _whisper, _ = _pass(tmp_path, RecordingSender(GPT))
    return bought, tuple(utterances_json(bought.utterances))


def test_d_carried_rows_keep_their_origin_and_another_origin_is_never_carried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bought, seeded = _seeded_live(tmp_path)
    assert {row["text_origin"] for row in seeded} == {ORIGIN_LIVE}  # 증거가 있는 줄마다 출처
    kept, _, _ = _pass(tmp_path, None, terms=("새 용어",), previous=seeded)
    assert kept.record["carried"] == 5 and kept.utterances == bought.utterances
    assert {u.text_origin for u in kept.utterances} == {ORIGIN_LIVE}
    warnings: list[str] = []
    monkeypatch.setattr(log, "warn", warnings.append)
    replayed, whisper, _ = _pass(
        tmp_path, None, terms=("새 용어",), previous=seeded, origin=ORIGIN_REPLAY
    )
    assert replayed.record["carried"] == 0 and replayed.utterances == whisper  # 출처가 다르다
    assert replayed.record["dropped_cross_checked"] == 1
    legacy = tuple({k: v for k, v in row.items() if k != "text_origin"} for row in seeded)
    unknown, _, _ = _pass(tmp_path, None, terms=("새 용어",), previous=legacy)
    assert unknown.record["carried"] == 0  # 출처 칸이 없는 19c 줄 — 어디서 온 글자인지 모른다
    assert sum("다시 교차 확인하세요" in line for line in warnings) == 2


def test_e_rows_replaced_now_and_rows_carried_are_counted_apart(tmp_path: Path) -> None:
    changed = {0: "망원동 카페에 갔어요. 정말 맛있네! 치즈", 1: GPT[1]}
    bought, _whisper, _ = _pass(tmp_path, RecordingSender(changed))
    assert bought.record["replaced"] == 2  # 「갔어요」·「정말 좋다」 — 두 청크에서 한 줄씩
    seeded = tuple(utterances_json(bought.utterances))
    # 용어집을 바꿔 다시 묻는데 청크 1은 실패한다 — 청크 0은 이번에 바꾸고, 청크 1은 잇는다.
    sender = RecordingSender(changed, fail=frozenset({1}))
    again, _, _ = _pass(tmp_path, sender, terms=("새 용어",), previous=seeded)
    record = again.record
    assert (record["replaced"], record["carried"], record["carried_replaced"]) == (1, 2, 1)
    assert again.utterances == bought.utterances


# ── 화면 — 칸 · 배선 · 문구 · 승인 앞 단계 ───────────────────────────────────────────────


def _skipped(update: Any) -> bool:
    return bool(update == gr.skip())


def test_blocks_put_the_cross_check_under_the_scope_and_wire_it_like_the_radio(
    tmp_path: Path, local_socket: None
) -> None:
    ui = RawWorkspaceUi(port=7993, state_path=tmp_path / "state.json")
    ui.create_workspace_view("교차 확인", str(tmp_path / "ws"))
    app = ui.build_blocks()
    by_id = {getattr(b, "elem_id", None): b for b in app.blocks.values()}
    box, radio, quote = by_id[CROSS_CHECK_ID], by_id[APPROVAL_MODE_ID], by_id["vl-quote"]
    assert isinstance(box, gr.Checkbox) and box is ui._cross_check_box
    assert box.value is True  # 새 작업공간 — 기본 켜짐
    # 승인 범위 바로 아래, 견적 문구 바로 위(그라디오는 이어진 입력 칸을 폼 하나로 감싼다).
    form = box.parent
    assert form is radio.parent and form.parent is quote
    assert form.children.index(box) == form.children.index(radio) + 1
    assert quote.children.index(form) + 1 == quote.children.index(by_id["library-analysis-scope"])
    deps = app.config["dependencies"]
    (chosen,) = [d for d in deps if d["targets"] == [(box._id, "input")]]
    assert chosen["inputs"] == [ui._signature_state._id, box._id]
    assert chosen["outputs"] == [ui._library_summary_md._id]
    follow = [d for d in deps if d.get("trigger_after") == chosen["id"]]
    assert [d["outputs"] for d in follow] == [[ui._quote_note_md._id]]
    # 틱은 1초 상태 틱 하나다(D63 세션 C) — 교차 확인 칸은 범위 칸 다음 칸이다.
    refresh = {d["targets"][0][1]: d for d in deps if box._id in d["outputs"]}
    assert sorted(refresh) == ["load", "tick"]
    assert refresh["tick"]["inputs"][:3] == [ui._signature_state._id, radio._id, box._id]
    css = ui_facade.WORKBENCH_CSS
    assert "#review-tab:has(#library-approve:disabled) #vl-cross-check { display: none; }" in css


def test_the_cross_check_view_saves_rebinds_and_the_tick_follows_other_tabs(
    tmp_path: Path, local_socket: None
) -> None:
    ui = RawWorkspaceUi(port=7993, state_path=tmp_path / "state.json")
    ui.create_workspace_view("교차 확인", str(tmp_path / "ws"))
    root = ui.workspace_root()
    signature = ui.current_signature()
    assert ui.cross_check_value() is True
    assert _skipped(ui.cross_check_tick(signature, True))  # 칸이 저장값과 같으면 보내지 않는다
    shown = ui.cross_check_view(signature, False)
    assert shown == ui.library_analysis_summary_markdown()  # 누르면 견적 칸을 바로 다시 그린다
    assert ui.raw.cross_check_enabled(root) is False
    reopened = RawWorkspaceUi(port=7993, state_path=tmp_path / "state2.json")
    reopened.open_workspace_view(str(root))
    assert reopened.cross_check_value() is False  # 앱을 다시 열어도 남는다
    ui.raw.set_cross_check(root, True)  # 다른 탭이 켰다 — 이 탭의 칸(꺼짐)을 틱이 맞춘다
    assert ui.cross_check_tick(signature, False).value is True
    assert _skipped(ui.cross_check_tick("다른 화면", False))
    with pytest.raises(gr.Error):
        ui.cross_check_view("다른 화면", True)


def test_the_cross_check_copy_is_polite_and_names_no_model_or_price() -> None:
    for text in (CROSS_CHECK_LABEL, CROSS_CHECK_INFO):
        assert "$" not in text and "GPT" not in text and "whisper" not in text, text
    assert POLITE_END.search(CROSS_CHECK_INFO)
    assert not re.search(r"(?<!니)다\.", CROSS_CHECK_INFO)
    quote = selection_analysis.SelectionQuote(
        asset_ids=("a",),
        estimates=(),
        digest="d",
        workflow=raw.RawWorkflowConfig(),
        mode=TRANSCRIBE,
    )
    assert workspace_service._text_pass_sentence(quote, ()) == ""  # 물을 것이 없으면 말하지 않는다


@needs_node
def test_the_approve_pre_step_passes_a_quote_with_the_text_pass_sentence(tmp_path: Path) -> None:
    """교차 확인 문장(괄호·$·「」·—)이 실린 견적도 보이면 그대로 간다 — escape 왕복을 지난다."""
    sentence = (
        " 다른 귀(GPT-Transcribe)로 영상 2개 75분을 한 번 더 듣습니다(공식 단가로 약 $0.34)."
        " 바뀐 자막이 있는 창은 다음 영상 분석 때 다시 판정합니다."
    )
    raw_text = workspace_service._transcribe_only_summary(18, 0, 30, 3, sentence)
    harness = tmp_path / "guard.js"
    harness.write_text(_GUARD_HARNESS, encoding="utf-8")
    script = tmp_path / "guard_src.js"
    script.write_text(ui_facade.APPROVE_GUARD_JS, encoding="utf-8")
    quotes = tmp_path / "quotes.json"
    shown = ui_facade.literal_markdown(raw_text)
    quotes.write_text(
        json.dumps(
            {"raw": raw_text, "shown": shown, "specialRaw": raw_text, "specialShown": shown}
        ),
        encoding="utf-8",
    )
    completed = subprocess.run(
        ["node", str(harness), str(script), str(quotes)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    out = json.loads(completed.stdout)
    assert out["visible"] == {"quote": raw_text, "scrolled": 0, "closed": 0}
