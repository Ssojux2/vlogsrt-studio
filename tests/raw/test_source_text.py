"""D59 19c — 1단계 글자 경로: 같은 WAV를 두 번째 귀에 묻고 D37 게이트로만 얹는다.

유료 호출은 없다 — 전송은 요청을 기록하는 가짜 sender가 받고, 시간 제공자는 준비된 세그먼트를
돌려준다. 시험이 못 박는 것:

- 요청 모양: 모델·json·언어·고정 문맥·용어집이 이 요청에만 실리고 시각 칸·온도는 없다.
- 게이트: 통과한 발화만 글자가 바뀌고, 거절된 후보는 `alt_text`, whisper 글자는 언제나 남는다.
- 사이에 낀 글자는 어느 발화에도 붙지 않는다(증거로만). 시각·아이디·순서·수는 그대로다.
- 청크마다 따로 계획한다 — 청크 끝으로 닫힌 세그먼트가 다음 청크로 넘어가지 않는다.
- 실패는 `unavailable` + WARN, 캐시하지 않고 whisper 기록(`asr.chunks`)을 건드리지 않는다.
- 제공자가 없으면 아무것도 보내지 않는다 — 캐시에 있는 답만 쓰고 심기는 멱등이다.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from tests.raw.test_understanding import BEFORE_SHUTDOWN, SETTINGS, FakeTimingProvider, make_audio
from vlogsrt import log
from vlogsrt.config import Config
from vlogsrt.models import Chunk
from vlogsrt.project.raw import (
    analysis_workflow,
    capabilities,
    glossary,
    source_text,
    source_transcript,
)
from vlogsrt.project.raw import service as raw
from vlogsrt.project.raw.analysis_approval import AsrPhaseApproval, asr_settings_fingerprint
from vlogsrt.project.raw.analysis_workflow import (
    AsrBudgetExhausted,
    CountingAsrGate,
    _utterances_from_artifact,
)
from vlogsrt.project.raw.caption_provider import (
    TransportTextProvider,
    audio_chunk_binding,
    parse_text_response,
    text_transcription_request,
)
from vlogsrt.project.raw.config import (
    SOURCE_TEXT_PROMPT,
    SOURCE_TEXT_SETTINGS,
    RawModelRoles,
    RawWorkflowConfig,
    SourceTextSettings,
    raw_workflow_config_to_json,
    source_text_problems,
)
from vlogsrt.project.raw.models import RawWorkspaceError
from vlogsrt.project.raw.understanding import (
    AsrEvidence,
    SampleSpan,
    SelectedAudio,
    UtteranceEvidence,
    transcribe_selected_audio,
    utterance_evidence,
    utterances_json,
)
from vlogsrt.project.raw.understanding_luna import (
    ORIGIN_LIVE,
    ORIGIN_REPLAY,
    AnalysisApprovalError,
)
from vlogsrt.transcribe import (
    CAPTION_TEXT_KIND,
    AudioTranscriptionRequest,
    _transport_fields,
)

SPANS = (SampleSpan(0, 160000), SampleSpan(160000, 320000))
"""10초 청크 둘 — 합성 PCM 20초."""

WHISPER: dict[int, list[dict[str, Any]]] = {
    0: [
        {"text": " 망원동 카페에 왔어요", "start": 0.0, "end": 2.0},
        {"text": " 진짜 맛있다", "start": 3.0, "end": 4.0},
        # 청크 끝으로 닫힌 세그먼트(D55의 180/180 꼴) — 시작이 다음 청크 오프셋과 같다.
        {"text": "치즈", "start": 10.0, "end": 10.0},
    ],
    1: [
        {"text": " 개좋아 여기 진짜 좋다", "start": 0.5, "end": 2.0},
        {"text": " 사진 찍자", "start": 6.0, "end": 7.0},
    ],
}
GPT = {
    0: "망원동 카페에 왔어요. 정말 맛있네! 치즈",
    1: "개좋아 여기 진짜 좋다. 저기 봐봐 사진 찍자",
}
GPT_MODEL = RawModelRoles().text_model
TERMS = ("왕방", "백숙")


class RecordingSender:
    """전송 경계 가짜 — (종류, 요청)을 기록하고 청크 번호로 준비된 글자를 돌려준다."""

    def __init__(self, replies: dict[int, str], fail: frozenset[int] = frozenset()) -> None:
        self.sent: list[tuple[str, AudioTranscriptionRequest]] = []
        self.replies = replies
        self.fail = fail

    def send_request(
        self, request: AudioTranscriptionRequest, cfg: Config, *, kind: str, stem: str, source: str
    ) -> dict[str, Any]:
        self.sent.append((kind, request))
        if request.chunk.index in self.fail:
            raise RuntimeError("글자 제공자가 닫혀 있다")
        return {"text": self.replies[request.chunk.index], "languages": [{"code": "ko"}]}


def _whisper(
    tmp_path: Path, fail: set[int] | None = None
) -> tuple[SelectedAudio, AsrEvidence, tuple[UtteranceEvidence, ...]]:
    audio = make_audio(tmp_path, seconds=20.0)
    evidence = transcribe_selected_audio(
        audio,
        SPANS,
        settings=RawWorkflowConfig(),
        understanding=SETTINGS,
        provider=FakeTimingProvider(replies=WHISPER, fail=fail),
        on=BEFORE_SHUTDOWN,
        request_id="text-pass",
        workdir=tmp_path,
    )
    return audio, evidence, utterance_evidence(audio, evidence)


def _request(
    tmp_path: Path, terms: tuple[str, ...] = TERMS, origin: str = ORIGIN_LIVE
) -> source_text.TextPassRequest:
    return source_text.TextPassRequest(
        settings=SOURCE_TEXT_SETTINGS,
        glossary=terms,
        workflow=RawWorkflowConfig(),
        on=BEFORE_SHUTDOWN,
        request_id="rev:src",
        workdir=tmp_path,
        cache_dir=tmp_path / "cache" / source_text.TEXT_CACHE_DIRNAME / "src",
        origin=origin,
    )


def _pass(
    tmp_path: Path,
    sender: RecordingSender | None,
    fail: set[int] | None = None,
    *,
    origin: str = ORIGIN_LIVE,
    terms: tuple[str, ...] = TERMS,
    previous: tuple[dict[str, object], ...] = (),
) -> tuple[source_text.TextPassResult, tuple[UtteranceEvidence, ...], AsrEvidence]:
    audio, evidence, utterances = _whisper(tmp_path, fail)
    provider = None if sender is None else TransportTextProvider(sender=sender, cfg=Config())
    request = _request(tmp_path, terms, origin)
    outcomes = source_text.run_text_pass(audio, evidence, request, provider)
    return (
        source_text.apply_text_pass(utterances, outcomes, request, audio.sample_rate, previous),
        utterances,
        evidence,
    )


def _skeleton(items: tuple[UtteranceEvidence, ...]) -> list[tuple[object, ...]]:
    return [
        (u.utterance_id, u.chunk_index, u.position, u.start, u.end, u.start_sample, u.end_sample)
        for u in items
    ]


# ── 요청 — 힌트는 두 번째 귀에게만, 시각 칸은 없다 ─────────────────────────────


def test_the_text_request_carries_prompt_glossary_and_language_but_no_time_fields(
    tmp_path: Path,
) -> None:
    sender = RecordingSender(GPT)
    _pass(tmp_path, sender)
    assert [kind for kind, _ in sender.sent] == [CAPTION_TEXT_KIND, CAPTION_TEXT_KIND]
    for _kind, request in sender.sent:
        assert request.model == GPT_MODEL and request.response_format == "json"
        assert request.prompt == SOURCE_TEXT_PROMPT
        assert request.keywords == TERMS and request.languages == ("ko",)
        assert request.timestamp_granularities == () and request.temperature is None
        required, extras = _transport_fields(request)
        assert required == {"model": GPT_MODEL, "response_format": "json"}
        assert "timestamp_granularities" not in extras and "temperature" not in extras
        assert extras["prompt"] == SOURCE_TEXT_PROMPT
    # 같은 WAV — whisper가 들은 청크 해시와 글자 요청의 청크가 같다.
    assert [request.chunk.path.name for _, request in sender.sent] == [
        "asr-chunk-0000.wav",
        "asr-chunk-0001.wav",
    ]


def test_the_registry_confirms_the_stage1_text_combo_and_refuses_it_for_whisper() -> None:
    features = (
        capabilities.FEATURE_LANGUAGES,
        capabilities.FEATURE_KEYWORDS,
        capabilities.FEATURE_PROMPT,
    )
    capabilities.confirm_request_combo(
        role=capabilities.ROLE_TEXT, model=GPT_MODEL, response_format="json", features=features
    )
    for feature in features:
        with pytest.raises(capabilities.UnverifiedModelCapability):
            capabilities.confirm_request_combo(
                role=capabilities.ROLE_TIMING,
                model=RawModelRoles().timing_model,
                response_format="verbose_json",
                features=(feature,),
            )


def test_the_final_caption_text_request_is_byte_identical_without_the_new_arguments(
    tmp_path: Path,
) -> None:
    """최종 자막 경로는 새 인자를 모른다 — 요청과 전송 칸이 19c 이전 조립과 같다."""
    audio = tmp_path / "final.wav"
    audio.write_bytes(b"pcm")
    chunk = Chunk(audio, 0.0, 1.0, 0)
    for workflow in (
        RawWorkflowConfig(),
        replace(
            RawWorkflowConfig(),
            caption=replace(RawWorkflowConfig().caption, keywords=("망원동",), languages=()),
        ),
    ):
        for prompt in ("", "사람이 승인한 문맥"):
            built = text_transcription_request(chunk, settings=workflow, prompt=prompt)
            pre_19c = AudioTranscriptionRequest(
                chunk=chunk,
                model=workflow.models.text_model,
                response_format=workflow.caption.text_response_format,
                languages=workflow.caption.languages,
                keywords=workflow.caption.keywords,
                prompt=prompt,
            )
            assert built == pre_19c
            assert _transport_fields(built) == _transport_fields(pre_19c)


def test_text_settings_live_outside_the_workflow_config_and_refuse_drift() -> None:
    assert source_text_problems(SOURCE_TEXT_SETTINGS) == []
    assert SOURCE_TEXT_SETTINGS.prompt == (
        "한국어 여행 브이로그. 반말·구어체·추임새를 들린 그대로 적어 주세요."
    )
    assert SOURCE_TEXT_SETTINGS.languages == ("ko",) and SOURCE_TEXT_SETTINGS.model == GPT_MODEL
    # 설정 JSON(내부 whisper 청크 열쇠가 해시하는 그 설정)에는 문맥도 용어집도 없다.
    whole = raw_workflow_config_to_json(RawWorkflowConfig())
    assert "glossary" not in whole and SOURCE_TEXT_PROMPT not in whole
    assert json.loads(whole)["caption"]["keywords"] == []
    assert source_text_problems(replace(SOURCE_TEXT_SETTINGS, model=RawModelRoles().timing_model))
    assert source_text_problems(replace(SOURCE_TEXT_SETTINGS, languages=()))
    assert source_text_problems(replace(SOURCE_TEXT_SETTINGS, prompt="줄\n바꿈"))


def test_a_glossary_term_with_angle_brackets_never_reaches_the_sender(tmp_path: Path) -> None:
    audio, evidence, _ = _whisper(tmp_path)
    sender = RecordingSender(GPT)
    provider = TransportTextProvider(sender=sender, cfg=Config())
    with pytest.raises(RawWorkspaceError, match="쓸 수 없는 글자"):
        source_text.run_text_pass(audio, evidence, _request(tmp_path, ("<b>왕방",)), provider)
    assert sender.sent == []


# ── 게이트 — 통과만 바꾸고, 거절은 후보로, 사이 글자는 증거로 ──────────────────────


def test_the_gate_replaces_only_passing_utterances_and_keeps_whisper_evidence(
    tmp_path: Path,
) -> None:
    result, whisper, _ = _pass(tmp_path, RecordingSender(GPT))
    by_text = {u.whisper_text: u for u in result.utterances}
    assert len(result.utterances) == len(whisper) == 5
    assert _skeleton(result.utterances) == _skeleton(whisper)  # 시각·아이디·순서·수 그대로
    first = by_text[" 망원동 카페에 왔어요"]
    assert (first.text, first.text_source, first.retext_reason, first.alt_text) == (
        "망원동 카페에 왔어요.",
        GPT_MODEL,
        source_text.REASON_ACCEPTED,
        "",
    )
    rejected = by_text[" 진짜 맛있다"]
    assert rejected.text == " 진짜 맛있다"  # whisper 글자 그대로
    assert (rejected.text_source, rejected.retext_reason, rejected.alt_text) == (
        source_transcript.TEXT_SOURCE_WHISPER,
        "match",
        "정말 맛있네!",
    )
    agreed = by_text["치즈"]
    assert (agreed.text, agreed.text_source, agreed.retext_reason) == (
        "치즈",
        source_transcript.TEXT_SOURCE_WHISPER,
        source_text.REASON_AGREED,
    )
    assert by_text[" 사진 찍자"].retext_reason == source_text.REASON_AGREED
    record = result.record
    assert record["status"] == source_text.PASS_APPLIED
    assert (record["replaced"], record["kept"]) == (2, 3)


def test_text_between_segments_is_recorded_and_never_attached(tmp_path: Path) -> None:
    result, whisper, _ = _pass(tmp_path, RecordingSender(GPT))
    ids = {u.whisper_text: u.utterance_id for u in result.utterances}
    assert result.record["unplaced"] == [
        {
            "chunk_index": 1,
            "text": "저기 봐봐",
            "chars": 4,
            "before": ids[" 개좋아 여기 진짜 좋다"],
            "after": ids[" 사진 찍자"],
        }
    ]
    assert all("저기" not in u.text for u in result.utterances)  # 어느 발화에도 붙지 않는다(규칙 1)
    assert _skeleton(result.utterances) == _skeleton(whisper)


def test_a_segment_closed_at_the_chunk_end_is_planned_with_its_own_chunk(
    tmp_path: Path,
) -> None:
    """청크 0 끝(10.0/10.0)에 닫힌 「치즈」는 청크 1 오프셋과 시작이 같다 — 한꺼번에 계획하면
    청크 1로 넘어가 청크 0의 「치즈」가 사이 글자가 된다. 청크마다 계획하면 제자리다."""
    result, _whisper_items, _ = _pass(tmp_path, RecordingSender(GPT))
    cheese = next(u for u in result.utterances if u.whisper_text == "치즈")
    assert cheese.chunk_index == 0 and cheese.start == Fraction(10)
    assert cheese.retext_reason == source_text.REASON_AGREED
    assert all("치즈" not in item["text"] for item in result.record["unplaced"])


# ── 실패 — whisper 글자 그대로 + WARN, 캐시·전사 기록은 그대로 ─────────────────────


def test_a_failed_text_chunk_keeps_whisper_text_warns_and_is_not_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    warnings: list[str] = []
    monkeypatch.setattr(log, "warn", warnings.append)
    sender = RecordingSender(GPT, fail=frozenset({1}))
    result, whisper, evidence = _pass(tmp_path, sender)
    chunks = {item["chunk_index"]: item for item in result.record["chunks"]}
    assert chunks[0]["status"] == source_text.STATUS_RECEIVED
    assert chunks[1]["status"] == source_text.STATUS_UNAVAILABLE and chunks[1]["error"]
    assert result.record["status"] == source_text.PASS_PARTIAL
    second = [u for u in result.utterances if u.chunk_index == 1]
    assert second == [u for u in whisper if u.chunk_index == 1]  # 증거 칸도 없다 — 19c 이전 그대로
    assert any("글자 경로 청크 1 실패" in line for line in warnings)
    cached = list((tmp_path / "cache" / source_text.TEXT_CACHE_DIRNAME / "src").glob("*.json"))
    assert [path.stem for path in cached] == [chunks[0]["key"]]  # 실패는 캐시하지 않는다
    assert all(chunk.succeeded for chunk in evidence.chunks)  # whisper 기록은 성공 그대로


def test_a_text_failure_never_makes_the_stage1_record_look_failed(tmp_path: Path) -> None:
    """1단계 기록 재사용은 `asr.chunks`만 본다 — 글자 실패(`asr_text`)로 whisper를 다시 안 산다."""
    artifact: dict[str, Any] = {
        "schema": analysis_workflow.ASR_PHASE_SCHEMA,
        "analysis_revision_id": "rev",
        "asset_id": "src",
        "source_sha256": "a" * 64,
        "input_fingerprint": "f" * 64,
        "phase_kind": "semantic",
        "video": {},
        "semantic_fingerprint": "s" * 64,
        "window_count": 1,
        "image_count": 0,
        "asr": {"chunks": [{"chunk_index": 0, "failure": ""}]},
        "asr_text": {
            "status": source_text.STATUS_UNAVAILABLE,
            "chunks": [{"chunk_index": 0, "status": source_text.STATUS_UNAVAILABLE}],
        },
        "frames": {"frames": [], "failures": []},
    }
    path = tmp_path / analysis_workflow.ASR_PHASE_DOCUMENT_NAME
    path.write_text(json.dumps(artifact), encoding="utf-8")
    assert analysis_workflow._phase_reusable(path) is True
    artifact["asr"]["chunks"][0]["failure"] = "전사 실패"  # 대조군 — whisper 실패는 다시 묻는다
    path.write_text(json.dumps(artifact), encoding="utf-8")
    assert analysis_workflow._phase_reusable(path) is False


def test_a_failed_whisper_chunk_is_never_asked_for_text(tmp_path: Path) -> None:
    sender = RecordingSender(GPT)
    result, _, evidence = _pass(tmp_path, sender, fail={1})
    assert [request.chunk.index for _, request in sender.sent] == [0]
    statuses = [item["status"] for item in result.record["chunks"]]
    assert statuses == [source_text.STATUS_RECEIVED, source_text.STATUS_TIMING_FAILED]
    assert not evidence.chunks[1].succeeded


# ── 캐시 — 열쇠의 재료, 제공자 없이도 같은 결과, 멱등 심기 ─────────────────────────


def test_the_text_cache_key_binds_origin_wav_model_languages_prompt_glossary_and_span() -> None:
    span = SampleSpan(0, 160000)

    def key(
        wav: str = "w" * 64,
        settings: SourceTextSettings = SOURCE_TEXT_SETTINGS,
        digest: str = "g" * 64,
        where: SampleSpan = span,
        origin: str = ORIGIN_LIVE,
    ) -> str:
        return source_text.text_chunk_key(wav, settings, digest, where, origin)

    base = key()
    assert base == key(settings=SourceTextSettings())
    changed = {
        key(origin=ORIGIN_REPLAY),
        key(wav="x" * 64),
        key(digest="h" * 64),
        key(where=SampleSpan(0, 1)),
        key(settings=replace(SOURCE_TEXT_SETTINGS, prompt="다른 문맥")),
        key(settings=replace(SOURCE_TEXT_SETTINGS, languages=("en",))),
        key(settings=replace(SOURCE_TEXT_SETTINGS, model="other-model")),
    }
    assert base not in changed and len(changed) == 7
    with pytest.raises(ValueError, match="출처"):
        key(origin="mixed")
    assert glossary.glossary_sha(TERMS) != glossary.glossary_sha(())


def test_replay_answers_are_never_read_by_a_live_run(tmp_path: Path) -> None:
    """검증 지적 1 — 재생 답이 캐시돼도 실제 실행은 그것을 두 번째 귀의 글자로 읽지 않는다."""
    replayed, whisper, _ = _pass(tmp_path, RecordingSender(GPT), origin=ORIGIN_REPLAY)
    assert replayed.record["status"] == source_text.PASS_APPLIED
    assert replayed.record["origin"] == ORIGIN_REPLAY
    live, _, _ = _pass(tmp_path, None, origin=ORIGIN_LIVE)  # 온라인 — 문이 없고 캐시만 읽는다
    assert live.record["status"] == source_text.STATUS_NOT_REQUESTED
    assert live.utterances == whisper  # 재생 글자가 얹히지 않았다
    again, _, _ = _pass(tmp_path, None, origin=ORIGIN_REPLAY)  # 재생은 제 답을 다시 쓴다
    assert again.utterances == replayed.utterances


def test_no_provider_sends_nothing_and_leaves_the_whisper_bytes(tmp_path: Path) -> None:
    result, whisper, _ = _pass(tmp_path, None)
    assert result.utterances == whisper
    assert utterances_json(result.utterances) == utterances_json(whisper)
    assert result.record["status"] == source_text.STATUS_NOT_REQUESTED
    assert result.record["unplaced"] == []
    assert not (tmp_path / "cache").exists()


def test_cached_answers_need_no_provider_and_reseeding_is_a_no_op(tmp_path: Path) -> None:
    sender = RecordingSender(GPT)
    bought, _, _ = _pass(tmp_path, sender)
    assert len(sender.sent) == 2
    again, _, _ = _pass(tmp_path, None)  # 문이 없다 — 캐시만 읽는다
    assert again.utterances == bought.utterances
    assert all(item["cached"] for item in again.record["chunks"])
    root = tmp_path / "ws"
    raw.RawWorkspaceService().create_workspace(root, "멱등")
    workspace = raw.RawWorkspaceService()._workspace(root)
    first = source_transcript.seed(
        workspace, "src", utterances_json(bought.utterances), Fraction(20), "a" * 64
    )
    second = source_transcript.seed(
        workspace, "src", utterances_json(again.utterances), Fraction(20), "a" * 64
    )
    assert (second["revision"], second["sequence"]) == (first["revision"], first["sequence"])
    spoken = source_transcript.spoken(workspace, "src", ())
    assert utterances_json(spoken) == utterances_json(bought.utterances)  # 증거까지 돌아온다


def test_a_glossary_change_keeps_already_cross_checked_lines_instead_of_reverting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """검증 지적 3 — 용어집을 바꿔 글자 열쇠가 달라져도(이번엔 답이 없다) 같은 whisper 답에
    이미 얹힌 글자를 whisper로 되돌리지 않는다. 심기는 아무것도 바꾸지 않는다."""
    bought, whisper, _ = _pass(tmp_path, RecordingSender(GPT))
    seeded = tuple(utterances_json(bought.utterances))
    root = tmp_path / "ws"
    raw.RawWorkspaceService().create_workspace(root, "이어 가기")
    workspace = raw.RawWorkspaceService()._workspace(root)
    first = source_transcript.seed(workspace, "src", list(seeded), Fraction(20), "a" * 64)
    kept, _, _ = _pass(tmp_path, None, terms=("새 용어",), previous=seeded)
    assert kept.record["status"] == source_text.STATUS_NOT_REQUESTED
    assert (kept.record["carried"], kept.record["dropped_cross_checked"]) == (5, 0)
    assert kept.utterances == bought.utterances
    again = source_transcript.seed(
        workspace, "src", utterances_json(kept.utterances), Fraction(20), "a" * 64
    )
    assert again["sequence"] == first["sequence"]  # 더 나은 전사를 조용히 되돌리지 않았다
    # whisper 답이 달라졌으면(다시 듣기) 이을 수 없다 — whisper 글자로 심고 WARN한다.
    warnings: list[str] = []
    monkeypatch.setattr(log, "warn", warnings.append)
    moved = tuple({**row, "evidence_id": "new-request:0"} for row in seeded)
    reverted, _, _ = _pass(tmp_path, None, terms=("새 용어",), previous=moved)
    assert reverted.utterances == whisper
    assert reverted.record["dropped_cross_checked"] == 2  # 바뀐 글자였던 두 줄
    assert any("다시 교차 확인하세요" in line for line in warnings)


def _seed_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rows: tuple[UtteranceEvidence, ...], media: str
) -> tuple[str, str, Any]:
    """`_seed_stage1` 한 번 — 시작 입력 지문은 미디어 M0로 잰다. 심을 때의 미디어는 `media`다."""
    from types import SimpleNamespace

    from vlogsrt.project.raw.understanding import VideoGrid, common_timeline

    tmp_path.mkdir(parents=True, exist_ok=True)
    root = tmp_path / "ws"
    raw.RawWorkspaceService().create_workspace(root, "다시 재기")
    workspace = raw.RawWorkspaceService()._workspace(root)
    audio = make_audio(tmp_path, seconds=20.0)
    grid = VideoGrid(0, Fraction(0), Fraction(1, 25), 500)
    timeline = common_timeline(source_id="src", video=grid, audio=audio)
    asset = SimpleNamespace(asset_id="src", content_sha256="a" * 64)
    values: dict[str, Any] = {
        "source_sha256": "a" * 64,
        "video": grid,
        "audio": audio,
        "settings": SETTINGS,
        "workflow": RawWorkflowConfig(),
    }
    start = analysis_workflow.source_input_fingerprint(**values, media_fingerprint="M0")
    monkeypatch.setattr(analysis_workflow.media_refs, "fingerprint", lambda *_: media)
    _, after = analysis_workflow._seed_stage1(
        workspace, asset, timeline, rows, SETTINGS, RawWorkflowConfig(), start, 0
    )
    expected = analysis_workflow.source_input_fingerprint(
        **values,
        media_fingerprint="M0",
        transcript_fingerprint=source_transcript.fingerprint(workspace, "src"),
    )
    return start, after, expected


def test_stage1_rekeys_the_input_only_when_nothing_but_the_transcript_moved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """검증 지적 2 — 글자가 자막 지문을 바꾸면 그 지문으로만 다시 잰다. 그사이 미디어 결속이
    움직였으면 처음 값 그대로 두어 2단계가 지금 값으로 다시 재 대조해 닫는다(19c 이전과 같은 선)."""
    (tmp_path / "pass").mkdir()
    checked, whisper, _ = _pass(tmp_path / "pass", RecordingSender(GPT))
    start, after, expected = _seed_case(tmp_path / "a", monkeypatch, checked.utterances, "M0")
    assert after == expected and after != start
    start, after, _ = _seed_case(tmp_path / "b", monkeypatch, checked.utterances, "M1")
    assert after == start  # 미디어가 움직였다 — 다시 재지 않는다
    start, after, _ = _seed_case(tmp_path / "c", monkeypatch, whisper, "M0")
    assert after == start  # whisper 글자 그대로 — 지문이 안 바뀌었다


# ── 기록 왕복 — 2단계가 다시 심어도 증거가 사라지지 않는다 ─────────────────────────


def test_the_stage1_artifact_round_trips_the_text_evidence(tmp_path: Path) -> None:
    result, _, _ = _pass(tmp_path, RecordingSender(GPT))
    artifact = {"utterances": utterances_json(result.utterances)}
    assert _utterances_from_artifact(artifact) == result.utterances
    plain = tmp_path / "plain"
    plain.mkdir()
    legacy = {"utterances": utterances_json(_whisper(plain)[2])}
    assert all("text_source" not in row for row in legacy["utterances"])  # 19c 이전 모양
    assert all(u.text_source is None for u in _utterances_from_artifact(legacy))


# ── 제공자 — 승인 문(19d부터 쓰인다)과 재생 ────────────────────────────────────


class FakeInner:
    """승인 문 안쪽의 가짜 전송 — 받은 힌트를 기록한다(유료 호출 없음)."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def text_transcript(self, *, binding: Any, chunk: Chunk, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return parse_text_response({"text": "글자"}, binding=binding, model=GPT_MODEL)


def _approved(
    directory: Path, seconds: float = 20.0, **overrides: Any
) -> tuple[source_text.ApprovedPhaseTextProvider, FakeInner, RawWorkflowConfig]:
    directory.mkdir(parents=True, exist_ok=True)
    workflow = replace(RawWorkflowConfig(), network_mode="online")
    approval = AsrPhaseApproval(
        approved=True,
        source_sha256="a" * 64,
        audio_stream_index=1,
        chunk_budget=1,
        settings_fingerprint=asr_settings_fingerprint(SETTINGS, workflow),
        timing_model=workflow.models.timing_model,
        approved_on=date(2026, 10, 2).isoformat(),
    )
    inner = FakeInner()
    values: dict[str, Any] = {
        "inner": inner,
        "workflow": workflow,
        "understanding": SETTINGS,
        "audio": make_audio(directory, seconds=seconds),
        "approval": approval,
        "approved_model": GPT_MODEL,
        "gate": CountingAsrGate(limit=1),
    }
    values.update(overrides)
    return source_text.ApprovedPhaseTextProvider(**values), inner, workflow


def _binding_and_chunk(tmp_path: Path) -> tuple[Any, Chunk]:
    wav = tmp_path / "asr-chunk-0000.wav"
    wav.write_bytes(b"wav")
    chunk = Chunk(wav, 0.0, 10.0, 0)
    binding = audio_chunk_binding(
        chunk, sample_start=0, sample_end=160000, sample_rate=16000, request_id="r:text-0000"
    )
    return binding, chunk


def test_online_stage1_builds_no_text_provider_until_an_approval_binds_a_text_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """19c — 승인·전송 경계·cfg가 다 있어도 승인이 글자 모델을 묶지 않으면 제공자가 없다(0송신)."""
    provider, _inner, workflow = _approved(tmp_path / "wire")
    live = analysis_workflow.AnalysisProviders(
        origin="live",
        phase="asr",
        live_sender=RecordingSender(GPT),
        live_cfg=Config(),
        asr_approvals={"src": provider.approval},
    )
    choose = analysis_workflow._text_provider_for
    assert choose(live, workflow, SETTINGS, provider.audio, "src") is None
    assert analysis_workflow._approved_text_model(provider.approval) is None
    # 19d가 승인에 글자 모델을 묶으면 — 그때만 승인 문이 있는 실제 제공자가 생긴다.
    monkeypatch.setattr(analysis_workflow, "_approved_text_model", lambda approval: GPT_MODEL)
    built = choose(live, workflow, SETTINGS, provider.audio, "src")
    assert isinstance(built, source_text.ApprovedPhaseTextProvider)
    assert isinstance(built.inner, TransportTextProvider) and built.approved_model == GPT_MODEL
    assert choose(live, workflow, SETTINGS, provider.audio, "other") is None  # 승인 없는 원본


def test_the_approved_text_gate_passes_only_an_open_matching_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """19d가 쓸 승인 문 — 승인·환경·모델·범위·예산이 모두 맞을 때만 안쪽 전송에 닿는다."""
    monkeypatch.setattr("vlogsrt.env.is_offline", lambda: False)
    provider, inner, workflow = _approved(tmp_path / "open")
    binding, chunk = _binding_and_chunk(tmp_path)
    ask: dict[str, Any] = {"binding": binding, "chunk": chunk, "on": BEFORE_SHUTDOWN}
    assert provider.text_transcript(settings=workflow, keywords=TERMS, **ask).text == "글자"
    assert inner.calls[0]["keywords"] == TERMS
    with pytest.raises(AsrBudgetExhausted):  # 예약 문은 승인 예산을 넘지 않는다
        provider.text_transcript(settings=workflow, **ask)
    refusals: list[tuple[dict[str, Any], RawWorkflowConfig]] = [
        ({"approved_model": "other-text-model"}, workflow),
        ({}, replace(workflow, network_mode="offline")),
        ({"seconds": 5.0}, workflow),  # 청크가 선택 오디오 밖이다
    ]
    for number, (overrides, settings) in enumerate(refusals):
        closed, closed_inner, _ = _approved(tmp_path / f"closed-{number}", **overrides)
        with pytest.raises(AnalysisApprovalError):
            closed.text_transcript(settings=settings, **ask)
        assert closed_inner.calls == []
    monkeypatch.setattr("vlogsrt.env.is_offline", lambda: True)
    offline, offline_inner, _ = _approved(tmp_path / "offline")
    with pytest.raises(AnalysisApprovalError):
        offline.text_transcript(settings=workflow, **ask)
    assert offline_inner.calls == []


def test_replay_text_declarations_by_wav_or_chunk_and_failure_without_one(
    tmp_path: Path,
) -> None:
    binding, chunk = _binding_and_chunk(tmp_path)
    ask: dict[str, Any] = {
        "binding": binding,
        "chunk": chunk,
        "settings": RawWorkflowConfig(),
        "on": BEFORE_SHUTDOWN,
    }
    exact = source_text.ReplayTextProvider(
        exact={binding.audio_sha256: "정밀"}, declared={0: "번호"}
    )
    assert exact.text_transcript(**ask).text == "정밀"
    by_index = source_text.ReplayTextProvider(exact={}, declared={0: "번호"})
    assert by_index.text_transcript(**ask).text == "번호"
    with pytest.raises(source_text.ReplayTextMissing):
        source_text.ReplayTextProvider(exact={}).text_transcript(**ask)


def test_a_corrupt_cache_entry_is_a_miss_with_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pass(tmp_path, RecordingSender(GPT))
    warnings: list[str] = []
    monkeypatch.setattr(log, "warn", warnings.append)
    for entry in (tmp_path / "cache").rglob("*.json"):
        entry.write_text("{", encoding="utf-8")
    result, whisper, _ = _pass(tmp_path, None)
    assert result.utterances == whisper and len(warnings) == 2
    assert all("글자 캐시를 읽지 못했다" in line for line in warnings)
