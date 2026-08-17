"""검증 — **표시만 한다**(불변 규칙 5). 순수 함수라 네트워크가 없다."""

from __future__ import annotations

from vlogsrt.config import Config
from vlogsrt.models import Segment
from vlogsrt.transcribe import ChunkResponse
from vlogsrt.verify import chunk_spans, summarize, verify

from .conftest import make_cue


def _response(index: int, offset: float, text: str) -> ChunkResponse:
    return ChunkResponse(index=index, offset=offset, payload={"text": text})


SAID = "안녕하세요 오늘은 망원동에 왔습니다"


def test_matching_text_raises_no_suspicion() -> None:
    cues = [
        make_cue(0.0, 3.0, "안녕하세요 오늘은 망원동에"),
        make_cue(4.0, 7.0, "왔습니다", index=2),
    ]
    report = verify(cues, (), [_response(0, 0.0, SAID)], 10.0, Config())
    assert report.suspects == ()
    assert not any(chunk.flagged for chunk in report.chunks)


def test_a_mismatched_cue_is_flagged() -> None:
    """whisper가 잘못 들었을 가능성. 고치지는 않고 짚어만 준다."""
    cues = [make_cue(0.0, 3.0, "전혀 상관없는 문장이 여기 들어갔다")]
    report = verify(cues, (), [_response(0, 0.0, SAID)], 10.0, Config())
    assert [suspect.cue_index for suspect in report.suspects] == [1]


def test_a_chunk_that_lost_speech_is_flagged() -> None:
    """큐들이 멀쩡해도 검증 텍스트에만 있는 발화가 있으면 조각 수준으로 표시한다."""
    cues = [make_cue(0.0, 3.0, "안녕하세요")]
    long_text = "안녕하세요 " + "그리고 한참 더 말했습니다 " * 10
    report = verify(cues, (), [_response(0, 0.0, long_text)], 60.0, Config())
    assert report.chunks[0].flagged


def test_short_cues_are_exempt() -> None:
    """'네', '음' 같은 큐는 대조가 무의미하다."""
    cues = [make_cue(0.0, 1.0, "네"), make_cue(2.0, 3.0, "음", index=2)]
    report = verify(cues, (), [_response(0, 0.0, "전혀 다른 말")], 10.0, Config())
    assert report.suspects == ()


def test_verification_never_touches_the_cues() -> None:
    """규칙 5 — 입력을 바꾸지 않는다."""
    cues = [make_cue(0.0, 3.0, "안녕하세요")]
    before = list(cues)
    verify(cues, (), [_response(0, 0.0, "완전히 다른 말")], 10.0, Config())
    assert cues == before


def test_no_response_means_no_report() -> None:
    report = verify([make_cue(0.0, 1.0, "가")], (), [], 10.0, Config())
    assert report.chunks == ()
    assert report.cue_total == 1


def test_chunk_spans_cover_the_whole_timeline() -> None:
    spans = chunk_spans([_response(0, 0.0, "가"), _response(1, 300.0, "나")], 600.0)
    assert spans[0][0] == 0.0
    assert spans[-1][1] == 600.0
    for earlier, later in zip(spans, spans[1:], strict=False):
        assert earlier[1] == later[0]


# ─────────────────────────────────────────────────────────────
#  남은 되풀이 — 검출은 repetition.py, 여기서는 표시만 한다(D27)
# ─────────────────────────────────────────────────────────────


def _looping(count: int, text: str = "맥주") -> list[Segment]:
    return [Segment(id=i, start=float(i), end=float(i) + 1.0, text=text) for i in range(count)]


def test_leftover_repeats_are_shown() -> None:
    """재전사가 회복하지 못한 구간은 검증 로그에 그대로 뜬다."""
    report = verify([make_cue(0.0, 3.0, "맥주")], _looping(5), [], 10.0, Config())
    assert [repeat.count for repeat in report.loops] == [5]


def test_the_summary_counts_everything() -> None:
    cues = [make_cue(0.0, 3.0, "전혀 상관없는 문장이 여기 들어갔다")]
    report = verify(cues, _looping(5), [_response(0, 0.0, "안녕하세요 오늘은")], 10.0, Config())
    line = summarize(report)
    assert "의심" in line and "저유사도" in line and "되풀이" in line
