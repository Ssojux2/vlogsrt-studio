"""W-25c / D73 — 1단계 whisper가 단어 시각을 함께 받아 두되, 이미 산 것은 하나도 낡지 않는다.

단어 시각·세그먼트 점수(`no_speech_prob`·`avg_logprob`)는 **제안·표시 재료**다. 줄 시각·경계는
여전히 `segments[]`에서만 온다(규칙 1) — 그래서 같은 응답을 단어와 함께 읽든 빼고 읽든 원본 자막·
발화 기록·시각이 바이트까지 같아야 한다. 단어 입자는 저장된 설정의 칸이 아니라 1단계 요청만
덧붙이므로, 다시 듣기 차례 0의 청크 열쇠·승인 지문·분석 입력 지문·견적 지문이 W-25c 이전(HEAD
b8208dc에서 잰 값) 그대로다 — 아무것도 누르지 않으면 아무것도 다시 사지 않는다.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, replace
from datetime import date
from fractions import Fraction
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from tests.raw.test_caption_provider import RecordingSender, binding_of, chunk
from tests.raw.test_raw_config import ACCEPTED_SETTINGS_JSON
from tests.raw.test_timing_truth import _report_module
from tests.raw.test_understanding import BEFORE_SHUTDOWN, SETTINGS, make_audio
from vlogsrt.config import Config
from vlogsrt.models import Chunk
from vlogsrt.project.raw import analysis_workflow, speech_refill, timing_truth
from vlogsrt.project.raw import understanding as stage1
from vlogsrt.project.raw.analysis_approval import AsrPhaseApproval, asr_settings_fingerprint
from vlogsrt.project.raw.analysis_config import RawUnderstandingSettings
from vlogsrt.project.raw.analysis_workflow import source_input_fingerprint
from vlogsrt.project.raw.browser_runtime import asr_estimate_digest
from vlogsrt.project.raw.caption_provider import (
    TimedWord,
    TransportTimingProvider,
    parse_timing_response,
    stage1_timing_provider,
    text_transcription_request,
    timing_transcription_request,
)
from vlogsrt.project.raw.config import (
    RawWorkflowConfig,
    raw_workflow_config_from_json,
    raw_workflow_config_to_json,
)
from vlogsrt.project.raw.models import AnalysisEstimate
from vlogsrt.project.raw.source_transcript import encoded
from vlogsrt.project.raw.understanding import (
    AsrChunkOutcome,
    SampleSpan,
    SelectedAudio,
    VideoGrid,
    asr_chunk_key,
    asr_evidence_json,
    transcribe_selected_audio,
    utterance_evidence,
    utterances_json,
)
from vlogsrt.transcribe import CAPTION_TIMING_KIND, _transport_fields

F = Fraction
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "api" / "fr_day_1_2_fhd.whisper.json"
PRE_W18_DEFAULT = raw_workflow_config_from_json(ACCEPTED_SETTINGS_JSON)
META_FIELDS = ("no_speech_prob", "avg_logprob", "words", "words_requested")


def _fixture_head(seconds: float) -> dict[str, Any]:
    """녹화 응답(청크 1)의 앞 `seconds`초 — 세그먼트·단어를 그대로 잘라 온다(픽스처는 읽기만)."""
    response = json.loads(FIXTURE.read_text(encoding="utf-8"))["chunks"][1]["response"]
    return {
        "segments": [seg for seg in response["segments"] if seg["end"] <= seconds],
        "words": [word for word in response["words"] if word["end"] <= seconds],
    }


def _segments_only(payload: dict[str, Any]) -> dict[str, Any]:
    """같은 응답의 W-25c 이전 모양 — 단어·점수가 없다."""
    keep = ("start", "end", "text")
    return {"segments": [{name: seg[name] for name in keep} for seg in payload["segments"]]}


class PayloadProvider:
    """청크마다 같은 응답을 파서에 넣어 돌려주는 시간 제공자 가짜 — 전송은 없다."""

    def __init__(self, payload: dict[str, Any], *, words_requested: bool) -> None:
        self.payload = payload
        self.words_requested = words_requested
        self.seen: list[int] = []

    def timed_transcript(self, *, binding: Any, chunk: Chunk, settings: Any, **_: Any) -> Any:
        self.seen.append(chunk.index)
        return parse_timing_response(
            self.payload,
            binding=binding,
            model=settings.models.timing_model,
            provider=settings.caption.timing_provider,
            chunk_duration=chunk.duration,
            chunk_offset=chunk.offset,
            words_requested=self.words_requested,
        )


def _hear(
    folder: Path, payload: dict[str, Any], *, words: bool, cache: Path | None = None
) -> tuple[SelectedAudio, stage1.AsrEvidence, PayloadProvider]:
    folder.mkdir(parents=True, exist_ok=True)
    audio = make_audio(folder, seconds=20.0)
    provider = PayloadProvider(payload, words_requested=words)
    evidence = transcribe_selected_audio(
        audio,
        (SampleSpan(0, audio.sample_count),),
        settings=RawWorkflowConfig(),
        understanding=SETTINGS,
        provider=provider,
        on=BEFORE_SHUTDOWN,
        request_id="run-words",
        workdir=folder,
        cache_dir=cache,
    )
    return audio, evidence, provider


# ── 응답 읽기 ────────────────────────────────────────────────────────────────


def test_the_recorded_whisper_response_has_top_level_words() -> None:
    """whisper는 단어를 세그먼트 안이 아니라 응답 맨 위 `words[]`로 준다 — 청크 국소 초다."""
    response = json.loads(FIXTURE.read_text(encoding="utf-8"))["chunks"][1]["response"]
    assert response["words"] and all("words" not in seg for seg in response["segments"])
    assert response["words"][0] == {
        "end": 3.380000114440918,
        "start": 2.359999895095825,
        "word": "고기가",
    }
    assert response["segments"][0]["start"] == response["words"][0]["start"]


def test_parsing_keeps_words_and_segment_scores(tmp_path: Path) -> None:
    payload = _fixture_head(20.0)
    target = chunk(tmp_path)
    timed = parse_timing_response(
        payload,
        binding=binding_of(target),
        model="whisper-1",
        provider="whisper_verbose_json",
        chunk_duration=target.duration + 20.0,
        words_requested=True,
    )
    assert timed.words_requested
    assert timed.words[0] == TimedWord(2.359999895095825, 3.380000114440918, "고기가")
    assert len(timed.words) == len(payload["words"])
    assert timed.no_speech_prob == tuple(seg["no_speech_prob"] for seg in payload["segments"])
    assert timed.avg_logprob == tuple(seg["avg_logprob"] for seg in payload["segments"])
    old = parse_timing_response(
        _segments_only(payload),
        binding=binding_of(target),
        model="whisper-1",
        provider="whisper_verbose_json",
        chunk_duration=target.duration + 20.0,
    )
    assert old.segments == timed.segments  # 줄 시각·글자는 메타와 무관하다
    assert (old.words, old.no_speech_prob, old.avg_logprob, old.words_requested) == (
        (),
        (),
        (),
        False,
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"segments": [{"start": 0.0, "end": 1.0, "text": "a", "no_speech_prob": 0.1}, {
            "start": 1.0, "end": 2.0, "text": "b"}], "words": [{"start": 0.0, "end": 1.0}]},
        {"segments": [{"start": 0.0, "end": 1.0, "text": "a", "avg_logprob": "x"}],
         "words": [{"start": 2.0, "end": 1.0, "word": "뒤집힘"}]},
        {"segments": [{"start": 0.0, "end": 1.0, "text": "a"}], "words": "아님"},
    ],
)  # fmt: skip
def test_malformed_metadata_is_dropped_with_a_warning_not_a_failed_chunk(
    tmp_path: Path, payload: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """메타는 제안 재료라 꼴이 어긋나도 유료 청크를 실패로 돌리지 않는다 — 그 메타만 빠지고 경고."""
    warnings: list[str] = []
    monkeypatch.setattr("vlogsrt.log.warn", warnings.append)
    target = chunk(tmp_path)
    timed = parse_timing_response(
        payload,
        binding=binding_of(target),
        model="whisper-1",
        provider="whisper_verbose_json",
        chunk_duration=target.duration,
    )
    assert timed.segments and timed.words == ()
    assert timed.no_speech_prob == () and timed.avg_logprob == ()
    assert warnings and all("D73" in line for line in warnings)


# ── 아래층은 단어를 모른다 ────────────────────────────────────────────────────


def test_the_same_response_with_or_without_words_gives_identical_lines(tmp_path: Path) -> None:
    """원본 자막에 심기는 발화 기록·1단계 문서의 전사 칸·시각이 바이트까지 같다(규칙 1)."""
    payload = _fixture_head(20.0)
    audio, rich, _ = _hear(tmp_path / "rich", payload, words=True)
    plain_audio, plain, _ = _hear(tmp_path / "plain", _segments_only(payload), words=False)
    (chunk_with,) = rich.chunks
    assert chunk_with.words and chunk_with.words_requested
    assert len(chunk_with.no_speech_prob) == len(chunk_with.transcript)
    assert chunk_with.transcript == plain.chunks[0].transcript
    with_lines = utterance_evidence(audio, rich)
    plain_lines = utterance_evidence(plain_audio, plain)
    assert with_lines == plain_lines
    dump = json.dumps
    assert dump(utterances_json(with_lines), ensure_ascii=False) == dump(
        utterances_json(plain_lines), ensure_ascii=False
    )
    assert dump(asr_evidence_json(rich), ensure_ascii=False) == dump(
        asr_evidence_json(plain), ensure_ascii=False
    )


def test_word_spans_use_the_segment_tick_conversion(tmp_path: Path) -> None:
    payload = _fixture_head(20.0)
    audio, evidence, _ = _hear(tmp_path, payload, words=True)
    (outcome,) = evidence.chunks
    spans = stage1.word_spans(outcome, audio.sample_rate)
    first_line = utterance_evidence(audio, evidence)[0]
    assert spans[0][0] == first_line.start  # 첫 단어와 첫 줄이 같은 틱에서 시작한다
    assert spans[0][2] == "고기가" and len(spans) == len(payload["words"])
    assert all(low <= high <= F(outcome.sample_end, audio.sample_rate) for low, high, _ in spans)


# ── 캐시 모양 ────────────────────────────────────────────────────────────────


def test_an_old_shaped_cache_entry_keeps_its_bytes_and_stays_a_hit(tmp_path: Path) -> None:
    payload = _fixture_head(20.0)
    cache = tmp_path / "asr-cache"
    _audio, evidence, _ = _hear(
        tmp_path / "first", _segments_only(payload), words=False, cache=cache
    )
    (path,) = cache.glob("*.json")
    (outcome,) = evidence.chunks
    expected = {
        "fingerprint": path.stem,
        "outcome": {
            "chunk_index": 0,
            "sample_start": 0,
            "sample_end": outcome.sample_end,
            "wav_sha256": outcome.wav_sha256,
            "request_id": "run-words:chunk-0000",
            "transcript": [list(segment) for segment in outcome.transcript],
            "failure": "",
        },
    }  # W-25c 이전 빌드가 쓰던 모양 그대로 — 새 칸이 하나도 없다
    stored = path.read_bytes()
    assert stored == json.dumps(expected, ensure_ascii=False).encode("utf-8")
    loaded = AsrChunkOutcome(**json.loads(stored)["outcome"])
    again = {"fingerprint": path.stem, "outcome": stage1._cached_outcome(loaded)}
    assert json.dumps(again, ensure_ascii=False).encode("utf-8") == stored  # 왕복도 그대로
    # 단어를 묻는 새 빌드도 이 항목을 그대로 맞힌다 — 열쇠가 같아 다시 사지 않는다.
    _audio, cached, provider = _hear(tmp_path / "second", payload, words=True, cache=cache)
    assert provider.seen == [] and cached.chunks[0].words == ()
    assert [p.name for p in cache.glob("*.json")] == [path.name]


@dataclass(frozen=True, slots=True)
class PreW25cOutcome:
    """W-25c 이전 빌드의 `AsrChunkOutcome` 칸 그대로 — 거꾸로 읽기를 보인다."""

    chunk_index: int
    sample_start: int
    sample_end: int
    wav_sha256: str
    request_id: str
    transcript: tuple[tuple[float, float, str], ...]
    failure: str = ""
    clamped_segments: tuple[int, ...] = ()


def test_a_new_entry_carries_the_metadata_and_an_old_build_misses_it(tmp_path: Path) -> None:
    """새 항목은 메타를 싣고 왕복한다. 옛 빌드는 모르는 칸에서 TypeError를 내고 그 항목을
    놓친 것으로 친다(`_transcribe_span`이 잡는다) — 그 빌드로 되돌릴 때만의 재구매다(D73)."""
    cache = tmp_path / "asr-cache"
    _audio, evidence, _ = _hear(tmp_path / "new", _fixture_head(20.0), words=True, cache=cache)
    (path,) = cache.glob("*.json")
    body = json.loads(path.read_text(encoding="utf-8"))["outcome"]
    assert all(name in body for name in META_FIELDS)
    loaded = AsrChunkOutcome(**body)
    assert stage1._cached_outcome(loaded) == json.loads(
        json.dumps(stage1._cached_outcome(evidence.chunks[0]))
    )
    assert stage1.word_spans(loaded, 16000) == stage1.word_spans(evidence.chunks[0], 16000)
    with pytest.raises(TypeError):
        PreW25cOutcome(**body)


# ── 요청 ─────────────────────────────────────────────────────────────────────


def test_stage1_whisper_asks_for_words_and_the_text_path_never_does(tmp_path: Path) -> None:
    sender = RecordingSender(text={"text": "글"}, timing={"segments": [], "words": []})
    target = chunk(tmp_path)
    ask = {"binding": binding_of(target), "chunk": target, "settings": RawWorkflowConfig()}
    timed = stage1_timing_provider(sender, Config()).timed_transcript(on=BEFORE_SHUTDOWN, **ask)
    plain = TransportTimingProvider(sender=sender, cfg=Config()).timed_transcript(
        on=BEFORE_SHUTDOWN, **ask
    )
    (kind, words_request), (_, plain_request) = sender.sent
    assert kind == CAPTION_TIMING_KIND
    assert words_request.timestamp_granularities == ("segment", "word")
    assert _transport_fields(words_request)[1]["timestamp_granularities"] == ["segment", "word"]
    assert plain_request.timestamp_granularities == ("segment",)  # 최종 자막·회복 재전사
    assert timed.words_requested and not plain.words_requested
    text = text_transcription_request(target, settings=RawWorkflowConfig(), prompt="문맥")
    assert text.timestamp_granularities == ()
    assert "timestamp_granularities" not in _transport_fields(text)[1]


def test_the_timing_request_always_asks_for_the_segment_skeleton(tmp_path: Path) -> None:
    from vlogsrt.project.raw.capabilities import UnverifiedModelCapability

    settings = RawWorkflowConfig()
    words_only = replace(
        settings, caption=replace(settings.caption, timestamp_granularities=("word",))
    )
    with pytest.raises(UnverifiedModelCapability, match="세그먼트"):
        timing_transcription_request(chunk(tmp_path), settings=words_only, word_times=True)


def test_stage1_and_refill_wire_the_word_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """1단계·빈칸 재전사의 실제 전송은 단어를 묻는다 — 객체만 만든다(전송 0회)."""
    monkeypatch.setattr("vlogsrt.env.is_offline", lambda: False)
    workflow = replace(RawWorkflowConfig(), network_mode="online")
    audio = make_audio(tmp_path, seconds=20.0)
    approval = AsrPhaseApproval(
        approved=True,
        source_sha256=audio.source_sha256,
        audio_stream_index=1,
        chunk_budget=1,
        settings_fingerprint=asr_settings_fingerprint(SETTINGS, workflow),
        timing_model=workflow.models.timing_model,
        approved_on=date(2026, 10, 2).isoformat(),
    )
    sender = RecordingSender(text={}, timing={})
    providers = analysis_workflow.AnalysisProviders(
        origin="live", phase="asr", live_sender=sender, live_cfg=Config(),
        asr_approvals={"src": approval}, on=BEFORE_SHUTDOWN,
    )  # fmt: skip
    built = analysis_workflow._asr_provider_for(providers, workflow, SETTINGS, audio, "src")
    assert built.inner.word_times is True
    plan = SimpleNamespace(workflow=workflow)
    quote = SimpleNamespace(pcm_sha256=audio.pcm_sha256, whisper_due=())
    transport = SimpleNamespace(sender=sender, cfg=Config())
    refill = speech_refill._timing_provider(plan, quote, audio, approval, transport)  # type: ignore[arg-type]
    assert refill.inner.inner.word_times is True
    assert sender.sent == []


# ── 다시 듣기 차례 0의 신원 — HEAD b8208dc에서 잰 값 그대로 ─────────────────────


KEY_EPOCH0 = "14c726b7a830337100bb8ebfadd890d8cd223e6338cd258c200860f824f6b62d"
KEY_EPOCH1 = "e1553c1d65eaa86182bbe20ae76d5d6f572349ca89db4b26c4d9eaead28d641a"
REFILL_KEY = "6884188b5708c691c13d351e8a746d7e19d151b236f20b9b060d710e7a50b58f"
ASR_FINGERPRINT = "06a193d29b7550982c69b44d88f9a6e5a93effd67978b19714ab03bc60dec41f"
DEFAULT_CONFIG_JSON_SHA = "9c5e126433fd55e45d3949db15ec056bc67760b9838648a46510a07245f07cce"
PRE_W18_CONFIG_JSON_SHA = "1492a05b7bd2aa61a5d3470f5ab6faebd90ad3ccb2682cb06f9a72bf6a3e5ee2"
INPUT_FINGERPRINT = "e95caf9f383ac0d71b1d91d5c3c5f88d1f4ad07294fafc63cfd351ff0d391141"
ESTIMATE_DIGEST = "0f5b132630033e136d0d92eef7302621086c54d7fb399d54110de12b6646597e"


@pytest.mark.parametrize("workflow", [RawWorkflowConfig(), PRE_W18_DEFAULT])
def test_epoch_zero_keys_and_fingerprints_are_the_pre_w25c_bytes(
    workflow: RawWorkflowConfig,
) -> None:
    """새 작업공간 기본값이든 저장된 옛 설정이든 — 청크 열쇠·승인 지문이 W-25c 이전 그대로다."""
    span = SampleSpan(0, 2880000)
    assert asr_chunk_key("w" * 64, "ApprovedPhaseAsrProvider", workflow, span) == KEY_EPOCH0
    assert asr_chunk_key("w" * 64, "ApprovedPhaseAsrProvider", workflow, span, 1) == KEY_EPOCH1
    refill = asr_chunk_key(
        "w" * 64, speech_refill.WHISPER_ORIGIN, workflow, SampleSpan(16000, 48000)
    )
    assert refill == REFILL_KEY
    assert asr_settings_fingerprint(RawUnderstandingSettings(), workflow) == ASR_FINGERPRINT


def test_stored_settings_analysis_input_and_quote_digest_are_the_pre_w25c_bytes() -> None:
    """저장되는 설정 JSON·분석 입력 지문·1단계 견적 지문 — 단어 입자는 어디에도 없다."""
    sha = hashlib.sha256
    assert sha(raw_workflow_config_to_json(RawWorkflowConfig()).encode()).hexdigest() == (
        DEFAULT_CONFIG_JSON_SHA
    )
    assert sha(raw_workflow_config_to_json(PRE_W18_DEFAULT).encode()).hexdigest() == (
        PRE_W18_CONFIG_JSON_SHA
    )
    assert RawWorkflowConfig().caption.timestamp_granularities == ("segment",)
    audio = SelectedAudio(
        source_id="s", source_path=Path("/x.mp4"), source_sha256="a" * 64, audio_stream_index=1,
        pcm_path=Path("/x.pcm"), pcm_sha256="p" * 64, sample_rate=16000, channels=1,
        sample_count=16000 * 200, pcm_sample0_file_time=F(-1, 50),
    )  # fmt: skip
    grid = VideoGrid(
        stream_index=0, presentation_origin=F(0), frame_duration=F(1001, 30000), frame_count=6000
    )
    fingerprint = source_input_fingerprint(
        source_sha256="a" * 64, video=grid, audio=audio, settings=RawUnderstandingSettings(),
        workflow=RawWorkflowConfig(),
    )  # fmt: skip
    assert fingerprint == INPUT_FINGERPRINT
    estimate = AnalysisEstimate(
        asset_id="src", source_order=1, source_name="a.mov", has_audio=True,
        audio_stream_index=1, sample_count=3200000, video_frame_count=6000,
        extent_seconds="200", window_count=4, frame_sample_count=16, asr_chunk_count=2,
        asr_seconds="200", luna_request_count=4, price_status="official_snapshot",
        user_ceiling_total=None, note="n",
    )  # fmt: skip
    assert asr_estimate_digest([estimate]) == ESTIMATE_DIGEST


# ── 시각 정답 보고서의 「단어 경계」 ───────────────────────────────────────────


def test_word_pairs_pick_the_nearest_word_edge_and_skip_stale_marks() -> None:
    def mark(edge: str, at: str, utterance: str = "u0") -> timing_truth.TruthMark:
        return timing_truth.TruthMark("src", utterance, edge, F(at), "r", "말", "ordinary", "t")

    words = [(F(1), F("1.5")), (F("2.5"), F("3.75"))]
    states = (
        timing_truth.MarkState(mark("start", "1.6"), None),
        timing_truth.MarkState(mark("end", "3.0"), None),
        timing_truth.MarkState(mark("start", "2"), None),  # 1과 2.5 사이 — 같은 거리면 이른 쪽
        timing_truth.MarkState(mark("end", "9", "gone"), timing_truth.STALE_LINE_MISSING),
    )
    found = timing_truth.word_pairs(states, words)
    assert [(pair.edge, pair.measured) for pair in found] == [
        ("start", F(1)),
        ("end", F("3.75")),
        ("start", F("2.5")),
    ]
    assert [pair.error for pair in found] == [F("-0.6"), F("0.75"), F("0.5")]
    assert timing_truth.word_pairs(states, []) == ()


REPORT_CHUNKS = [
    {"chunk_index": 0, "sample_start": 0, "sample_end": 2880000, "failure": "",
     "request_id": "run:chunk-0000", "wav_sha256": "0" * 64},
    {"chunk_index": 1, "sample_start": 2880000, "sample_end": 3200000, "failure": "",
     "request_id": "run:chunk-0001", "wav_sha256": "1" * 64},
]  # fmt: skip


def _report_workspace(root: Path, words: bool) -> None:
    """보고서가 읽는 것만 가진 합성 작업공간 — 원본 자막·정답 표·1단계 기록·전사 캐시."""
    root.mkdir(parents=True)
    _report_db(root)
    _report_stage1(root)
    _report_cache(root, words)


def _report_db(root: Path) -> None:
    transcript = {
        "asset_id": "src", "revision": "r1", "source_sha256": "f" * 64,
        "duration": encoded(F(200)),
        "utterances": [{"utterance_id": "u0", "text": "말", "start": encoded(F(1)),
                        "end": encoded(F(4))}],
    }  # fmt: skip
    with sqlite3.connect(root / "project.sqlite3") as db:
        db.execute("CREATE TABLE source_transcripts (asset_id, sequence, revision, payload)")
        db.execute(
            "INSERT INTO source_transcripts VALUES ('src', 0, 'r1', ?)", (json.dumps(transcript),)
        )
        db.execute("CREATE TABLE source_assets (asset_id, source_name)")
        db.execute("INSERT INTO source_assets VALUES ('src', 'a.mov')")
        timing_truth._create(db)
        for utterance, edge, at in (
            ("u0", "start", "1.6"),
            ("u0", "end", "3.9"),
            ("u9", "end", "5"),
        ):
            entry = timing_truth.TruthMark(
                "src", utterance, edge, F(at), "r1", "말", "ordinary", "t"
            )
            timing_truth._upsert(db, entry)


def _report_stage1(root: Path) -> None:
    """1단계 기록 — PCM 표본 0의 파일 시각 0.5초, 영상 원점 0이라 공통 시계 = PCM + 0.5초."""
    stage1_doc = {
        "source_sha256": "f" * 64, "created_at": "2026-10-22T00:00:00+00:00",
        "audio": {"pcm_sha256": "p" * 64, "audio_stream_index": 1, "sample_rate": 16000,
                  "channels": 1, "sample_count": 3200000,
                  "pcm_sample0_file_time": {"num": 1, "den": 2}},
        "asr": {"chunks": REPORT_CHUNKS},
    }  # fmt: skip
    folder = root / "analysis" / "anal_1" / "000001_src"
    folder.mkdir(parents=True)
    (folder / "asr_phase.json").write_text(json.dumps(stage1_doc), encoding="utf-8")


def _report_cache(root: Path, words: bool) -> None:
    """청크마다 캐시 항목 하나(단어는 `words`일 때만)와 깨진 항목 하나 — 깨진 것은 건너뛴다."""
    cache = root / "cache" / "source-asr" / "src"
    cache.mkdir(parents=True)
    said = ([[1.0, 1.5, "안녕"], [2.0, 3.25, "망원동"]], [[0.5, 1.0, "둘째"]])
    for row, spoken in zip(REPORT_CHUNKS, said, strict=True):
        outcome = {name: row[name] for name in ("chunk_index", "sample_start", "sample_end",
                   "wav_sha256", "request_id")}  # fmt: skip
        outcome.update(transcript=[], failure="")
        if words:
            outcome.update(words=spoken, words_requested=True)
        body = {"fingerprint": row["wav_sha256"], "outcome": outcome}
        (cache / f"{row['wav_sha256'][:8]}.json").write_text(json.dumps(body), encoding="utf-8")
    (cache / "broken.json").write_text("{", encoding="utf-8")


def _bytes(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _run_report(root: Path, capsys: pytest.CaptureFixture[str]) -> tuple[ModuleType, dict]:
    report = _report_module()
    capsys.readouterr()
    assert report.main([str(root), "--json"]) == 0
    return report, json.loads(capsys.readouterr().out)


def test_the_report_scores_the_nearest_word_boundary_read_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """공통 시계 = PCM + 0.5초. 시작 정답 1.6 ↔ 단어 시작 1.5 · 끝 정답 3.9 ↔ 단어 끝 3.75."""
    root = tmp_path / "ws"
    _report_workspace(root, words=True)
    before = _bytes(root)
    report, document = _run_report(root, capsys)
    assert _bytes(root) == before  # 아무것도 쓰지 않았다
    (video,) = document["videos"]
    words = video["word_boundary"]
    assert (video["word_note"], words["starts"], words["ends"]) == (None, 1, 1)
    assert (words["start_mae"], words["end_mae"]) == ("1/10", "3/20")
    assert (words["start_hit"], words["end_hit"], words["end_cut"]) == ("1", "1", "1")
    assert document["overall"]["word_boundary"] == words
    assert document["strata"]["ordinary"]["word_boundary"] == words
    assert document["strata"]["chunk_first"]["word_boundary"]["starts"] == 0
    printed = "\n".join(report.render(document))
    assert "단어 경계" in printed and "단어 시각 없음" not in printed


def test_the_report_says_no_word_times_for_transcripts_bought_before_words(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "ws"
    _report_workspace(root, words=False)
    report, document = _run_report(root, capsys)
    (video,) = document["videos"]
    assert video["word_note"] == "단어 시각 없음" and video["word_boundary"]["starts"] == 0
    assert document["word_note"] == "단어 시각 없음"
    printed = report.render(document)
    assert sum("단어 시각 없음" in line for line in printed) == 2 + len(timing_truth.STRATA)
    assert document["overall"]["subtitle"]["starts"] == 1  # 자막 지표는 그대로 잰다
