"""W-35b(D95) — 1단계 Gemini 글자 경로: 같은 WAV를 Gemini 3.5 Transcribe에 묻고 단어를 whisper 줄에
배정한다.

유료 호출은 없다 — 제공자는 준비된 Gemini 응답(합성)을 돌려주는 가짜다. 시험이 못 박는 것:

- 단어 가운데 자리가 든 whisper 줄이 Gemini 글자가 된다 — 시각·아이디·순서·수는 그대로다.
- 단어가 없는 줄은 whisper 글자 그대로(`no_words`), 줄 밖 단어는 기록만(앞뒤 발화 아이디) + WARN.
- 실패·받아 줄 수 없는 응답은 `unavailable` + WARN이고 캐시하지 않는다. 다른 청크는 그대로 얹힌다.
- 제공자가 없으면 아무것도 보내지 않는다 — 캐시에 있는 답만 쓰고 같은 결과다.
- 요청 신원·캐시 열쇠에는 힌트(문맥·용어집)가 없고, gpt 글자 열쇠와 겹치지 않는다.
- 승인된 전송 경계는 승인이 묶은 모델·신원·청크 밖을 보내지 않고, 예약 문을 센다.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from tests.raw.test_source_text import _whisper
from tests.raw.test_understanding import BEFORE_SHUTDOWN, SETTINGS
from vlogsrt import log
from vlogsrt.models import Chunk
from vlogsrt.project.raw import gemini_text, source_text
from vlogsrt.project.raw.analysis_approval import AsrPhaseApproval, asr_settings_fingerprint
from vlogsrt.project.raw.analysis_workflow import CountingAsrGate
from vlogsrt.project.raw.caption_provider import AudioChunkBinding
from vlogsrt.project.raw.config import (
    GEMINI_TEXT_SETTINGS,
    SOURCE_TEXT_SETTINGS,
    RawWorkflowConfig,
    gemini_text_problems,
    text_model_label,
)
from vlogsrt.project.raw.understanding import SampleSpan
from vlogsrt.project.raw.understanding_luna import ORIGIN_LIVE, ORIGIN_REPLAY

GEMINI_MODEL = GEMINI_TEXT_SETTINGS.model

# whisper 줄(`test_source_text.WHISPER`): 청크 0 — [0,2) 망원동 · [3,4) 진짜 맛있다 ·
# 길이 0 치즈(10/10)
# 청크 1 — [0.5,2) 개좋아 여기 진짜 좋다 · [6,7) 사진 찍자.
WORDS: dict[int, list[tuple[str, str, str]]] = {
    0: [
        ("망원동", "0.2s", "0.6s"),
        ("카페에", "0.7s", "1.1s"),
        ("왔어요", "1.2s", "1.8s"),
        ("정말", "3.1s", "3.4s"),
        ("맛있네", "3.5s", "3.9s"),
        ("음", "5.0s", "5.4s"),  # 어느 줄도 담지 않는다 — whisper가 놓친 말
    ],
    1: [
        ("개좋아", "0.6s", "1.0s"),
        ("여기", "1.1s", "1.3s"),
        ("정말", "1.4s", "1.6s"),
        ("좋다", "1.7s", "1.9s"),
    ],
}
"""청크 국소 시계의 Gemini 단어 — 청크 1의 「사진 찍자」 줄에는 단어가 없다."""


def _payload(words: list[tuple[str, str, str]]) -> dict[str, Any]:
    text = " ".join(word for word, _, _ in words)
    return {
        "status": "completed",
        "model": GEMINI_MODEL,
        "output_text": text,
        "steps": [
            {
                "type": "model_output",
                "content": [
                    {
                        "type": "text",
                        "text": text,
                        "annotations": [
                            {"type": "word_info", "text": w, "start_offset": s, "end_offset": e}
                            for w, s, e in words
                        ],
                    }
                ],
            }
        ],
    }


class FakeWords:
    """Gemini 글자 제공자 가짜 — 청크 번호로 준비된 응답을 돌려주고 받은 요청을 센다."""

    def __init__(
        self,
        replies: dict[int, dict[str, Any]] | None = None,
        fail: frozenset[int] = frozenset(),
    ) -> None:
        self.replies = (
            replies if replies is not None else {i: _payload(w) for i, w in WORDS.items()}
        )
        self.fail = fail
        self.seen: list[tuple[int, str, str]] = []

    def transcription(
        self,
        *,
        binding: AudioChunkBinding,
        chunk: Chunk,
        settings: RawWorkflowConfig,
        on: date,
        stem: str = "",
        source: str = "",
    ) -> dict[str, Any]:
        self.seen.append((binding.chunk_index, chunk.path.name, settings.models.timing_model))
        if binding.chunk_index in self.fail:
            raise RuntimeError("Gemini 문이 닫혀 있다")
        return self.replies[binding.chunk_index]


def _request(tmp_path: Path, origin: str = ORIGIN_LIVE) -> gemini_text.WordPassRequest:
    return gemini_text.WordPassRequest(
        settings=GEMINI_TEXT_SETTINGS,
        workflow=RawWorkflowConfig(),
        on=BEFORE_SHUTDOWN,
        request_id="rev:src",
        workdir=tmp_path,
        cache_dir=tmp_path / "cache" / gemini_text.TEXT_CACHE_DIRNAME / "src",
        origin=origin,
    )


def _pass(
    tmp_path: Path, provider: Any, *, origin: str = ORIGIN_LIVE, previous: tuple = ()
) -> tuple[source_text.TextPassResult, tuple, tuple[gemini_text.WordChunkOutcome, ...]]:
    audio, evidence, utterances = _whisper(tmp_path)
    request = _request(tmp_path, origin)
    outcomes = gemini_text.run_word_pass(audio, evidence, request, provider)
    result = gemini_text.apply_word_pass(utterances, outcomes, request, audio.sample_rate, previous)
    return result, utterances, outcomes


def _skeleton(items: Any) -> list[tuple[object, ...]]:
    return [
        (u.utterance_id, u.chunk_index, u.position, u.start, u.end, u.start_sample, u.end_sample)
        for u in items
    ]


# ── 배정 — 글자는 Gemini, 시각은 whisper ────────────────────────────────────


def test_gemini_words_become_the_text_of_the_whisper_line_they_fall_in(tmp_path: Path) -> None:
    provider = FakeWords()
    result, utterances, _ = _pass(tmp_path, provider)
    assert _skeleton(result.utterances) == _skeleton(utterances)  # 규칙 5
    rows = [(u.text, u.whisper_text, u.text_source, u.retext_reason) for u in result.utterances]
    assert rows == [
        ("망원동 카페에 왔어요", " 망원동 카페에 왔어요", GEMINI_MODEL, gemini_text.REASON_PLACED),
        ("정말 맛있네", " 진짜 맛있다", GEMINI_MODEL, gemini_text.REASON_PLACED),
        ("치즈", "치즈", "whisper", gemini_text.REASON_NO_WORDS),  # 길이 0 줄은 담지 않는다
        (
            "개좋아 여기 정말 좋다",
            " 개좋아 여기 진짜 좋다",
            GEMINI_MODEL,
            gemini_text.REASON_PLACED,
        ),
        (" 사진 찍자", " 사진 찍자", "whisper", gemini_text.REASON_NO_WORDS),
    ]
    assert {u.text_origin for u in result.utterances} == {ORIGIN_LIVE}
    # 같은 WAV — whisper가 들은 청크를 Gemini 전사 엔진 보기로 묻는다(힌트 없는 시간 엔진 문).
    assert provider.seen == [
        (0, "asr-chunk-0000.wav", GEMINI_MODEL),
        (1, "asr-chunk-0001.wav", GEMINI_MODEL),
    ]


def test_a_word_no_line_holds_is_recorded_with_its_neighbours_and_warned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    warnings: list[str] = []
    monkeypatch.setattr(log, "warn", warnings.append)
    result, utterances, _ = _pass(tmp_path, FakeWords())
    record = result.record
    (word,) = record["unplaced"]
    assert (word["chunk_index"], word["text"], word["start"], word["end"]) == (
        0,
        "음",
        "5/1",
        "27/5",
    )
    assert (word["before"], word["after"]) == (
        utterances[1].utterance_id,
        utterances[2].utterance_id,
    )
    assert record["status"] == source_text.PASS_APPLIED
    assert (record["replaced"], record["kept"], record["no_words"]) == (3, 2, 2)
    assert record["model"] == GEMINI_MODEL and record["version"] == "word-text/v1"
    assert "glossary_sha" not in record and "prompt" not in record  # 힌트가 없다(규칙 6)
    chunk0 = record["chunks"][0]
    assert [w["text"] for w in chunk0["words"]][:2] == ["망원동", "카페에"]
    assert chunk0["placed"][0] == {"utterance_id": utterances[0].utterance_id, "words": [0, 1, 2]}
    assert any("어느 줄에도 들지 않은 단어 1개" in line for line in warnings)
    assert any("말을 듣지 못한 줄 2개" in line for line in warnings)


# ── 실패·캐시 ───────────────────────────────────────────────────────────────


def test_a_failed_chunk_keeps_whisper_text_is_never_cached_and_the_other_lands(
    tmp_path: Path,
) -> None:
    result, utterances, outcomes = _pass(tmp_path, FakeWords(fail=frozenset({0})))
    assert [o.status for o in outcomes] == [
        source_text.STATUS_UNAVAILABLE,
        source_text.STATUS_RECEIVED,
    ]
    assert [u.text for u in result.utterances[:3]] == [u.text for u in utterances[:3]]
    assert result.utterances[3].text == "개좋아 여기 정말 좋다"
    assert result.record["status"] == source_text.PASS_PARTIAL
    cache = tmp_path / "cache" / gemini_text.TEXT_CACHE_DIRNAME / "src"
    assert [path.name for path in cache.iterdir()] == [f"{outcomes[1].key}.json"]


def test_a_response_with_text_but_no_timed_word_is_unavailable_and_not_cached(
    tmp_path: Path,
) -> None:
    broken = _payload([])
    broken["output_text"] = "시각 없는 글자"
    replies = {0: broken, 1: _payload(WORDS[1])}
    _, _, outcomes = _pass(tmp_path, FakeWords(replies))
    assert outcomes[0].status == source_text.STATUS_UNAVAILABLE
    assert "받아 줄 수 없다" in outcomes[0].error
    assert not gemini_text.cached_answer_exists(
        tmp_path / "cache" / gemini_text.TEXT_CACHE_DIRNAME / "src", outcomes[0].key
    )


def test_without_a_provider_only_cached_answers_land_and_nothing_is_sent(tmp_path: Path) -> None:
    first, _, _ = _pass(tmp_path, FakeWords())
    again, _, outcomes = _pass(tmp_path, None)
    assert all(o.cached for o in outcomes)
    assert [u.text for u in again.utterances] == [u.text for u in first.utterances]
    fresh = tmp_path / "other"
    fresh.mkdir()
    untouched, utterances, outcomes = _pass(fresh, None)
    assert {o.status for o in outcomes} == {source_text.STATUS_NOT_REQUESTED}
    assert untouched.utterances == utterances  # 증거 칸도 붙지 않는다(바이트 그대로)


def test_a_chunk_without_an_answer_carries_the_seeded_gemini_text(tmp_path: Path) -> None:
    first, _, _ = _pass(tmp_path, FakeWords())
    seeded = tuple(
        {
            "utterance_id": u.utterance_id,
            "chunk_index": u.chunk_index,
            "position": u.position,
            "start_sample": u.start_sample,
            "end_sample": u.end_sample,
            "evidence_id": u.evidence_id,
            "text": u.text,
            "whisper_text": u.whisper_text,
            "text_source": u.text_source,
            "retext_reason": u.retext_reason,
            "alt_text": u.alt_text,
            "text_origin": u.text_origin,
        }
        for u in first.utterances
    )
    fresh = tmp_path / "again"
    fresh.mkdir()
    carried, _, _ = _pass(fresh, FakeWords(fail=frozenset({0, 1})), previous=seeded)
    assert [u.text for u in carried.utterances] == [u.text for u in first.utterances]
    assert carried.record["carried"] == 5


# ── 신원·열쇠 — 힌트가 없고 gpt 글자와 겹치지 않는다 ────────────────────────


def test_the_identity_and_keys_carry_no_hint_and_never_meet_the_gpt_text_keys() -> None:
    span = SampleSpan(0, 160000)
    live = gemini_text.chunk_key("w" * 64, GEMINI_TEXT_SETTINGS, span, ORIGIN_LIVE)
    replay = gemini_text.chunk_key("w" * 64, GEMINI_TEXT_SETTINGS, span, ORIGIN_REPLAY)
    gpt = source_text.text_chunk_key("w" * 64, SOURCE_TEXT_SETTINGS, "g", span, ORIGIN_LIVE)
    assert len({live, replay, gpt}) == 3
    bumped = replace(GEMINI_TEXT_SETTINGS, version="word-text/v2")
    assert gemini_text.text_identity(bumped) != gemini_text.text_identity(GEMINI_TEXT_SETTINGS)
    assert gemini_text_problems(GEMINI_TEXT_SETTINGS) == []
    assert gemini_text_problems(bumped)  # 판은 배정 규칙 판과 같아야 한다
    assert gemini_text_problems(replace(GEMINI_TEXT_SETTINGS, model="whisper-1"))
    assert text_model_label(GEMINI_MODEL) == "Gemini 3.5 Transcribe"
    with pytest.raises(ValueError):
        gemini_text.chunk_key("w" * 64, GEMINI_TEXT_SETTINGS, span, "fixture")


# ── 승인된 전송 경계 ─────────────────────────────────────────────────────────


def _approval(**changes: Any) -> AsrPhaseApproval:
    workflow = RawWorkflowConfig(network_mode="online")
    base = AsrPhaseApproval(
        approved=True,
        source_sha256="a" * 64,
        audio_stream_index=1,
        chunk_budget=2,
        settings_fingerprint=asr_settings_fingerprint(SETTINGS, workflow),
        timing_model=workflow.models.timing_model,
        approved_on=BEFORE_SHUTDOWN.isoformat(),
        text_model=GEMINI_MODEL,
        text_identity=gemini_text.text_identity(GEMINI_TEXT_SETTINGS),
        text_chunk_budget=1,
    )
    return replace(base, **changes)


def _gated_pass(tmp_path: Path, approval: AsrPhaseApproval, keys: Any = None) -> Any:
    tmp_path.mkdir(parents=True, exist_ok=True)
    audio, evidence, utterances = _whisper(tmp_path)
    workflow = RawWorkflowConfig(network_mode="online")
    request = replace(_request(tmp_path), workflow=workflow)
    inner = FakeWords()
    provider = gemini_text.ApprovedPhaseWordProvider(
        inner=inner,
        workflow=workflow,
        understanding=SETTINGS,
        audio=audio,
        approval=approval,
        settings=GEMINI_TEXT_SETTINGS,
        approved_identity=str(approval.text_identity),
        gate=CountingAsrGate(limit=approval.text_chunk_budget),
        approved_keys=keys,
    )
    return gemini_text.run_word_pass(audio, evidence, request, provider), inner


def test_the_approved_boundary_sends_only_the_quoted_chunks_within_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("vlogsrt.env.is_offline", lambda: False)  # 전송은 가짜다
    probe, _ = _gated_pass(tmp_path / "probe", _approval(text_chunk_budget=2))
    keys = frozenset({probe[1].key})
    outcomes, inner = _gated_pass(tmp_path / "keyed", _approval(), keys)
    assert [o.status for o in outcomes] == [
        source_text.STATUS_UNAVAILABLE,
        source_text.STATUS_RECEIVED,
    ]
    assert [seen[0] for seen in inner.seen] == [1]  # 견적에 없던 청크는 보내지 않는다
    outcomes, inner = _gated_pass(tmp_path / "budget", _approval())
    assert [seen[0] for seen in inner.seen] == [0]  # 상한 1 — 둘째 청크는 예약 문이 닫는다
    assert outcomes[1].status == source_text.STATUS_UNAVAILABLE


def test_the_approved_boundary_refuses_another_model_or_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("vlogsrt.env.is_offline", lambda: False)  # 전송은 가짜다
    for index, wrong in enumerate(
        (_approval(text_model="gpt-4o-transcribe"), _approval(text_identity="x" * 64))
    ):
        outcomes, inner = _gated_pass(tmp_path / str(index), wrong)
        assert inner.seen == []
        assert {o.status for o in outcomes} == {source_text.STATUS_UNAVAILABLE}
