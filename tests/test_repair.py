"""되풀이 재전사 — **출력을 바꾸는 유일한 판정 계층이다**(D27).

가짜 전사기만 쓴다. 네트워크도 ffmpeg도 없다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from dataclasses import replace as data_replace
from pathlib import Path

from vlogsrt import repair
from vlogsrt.config import Config
from vlogsrt.models import REPAIR_COLLAPSED, REPAIR_REPLACED, Chunk, Segment
from vlogsrt.transcribe import ARBITER_KIND, RESCUE_KIND, ChunkResponse, TranscriptionJob

LOOP = "꼬불꼬불 꼬불하고 여기 안으로 들어갔다가"
CLEAN = "성당 앞을 지나서 골목으로 들어가면 작은 광장이 나옵니다"


def _echoing(count: int, start: float = 10.0, text: str = LOOP, first_id: int = 0) -> list[Segment]:
    """골격의 id는 언제나 유일하다(`build_spine`가 통번호를 매긴다). 테스트도 그래야 한다."""
    return [
        Segment(id=first_id + index, start=start + index, end=start + index + 1.0, text=text)
        for index in range(count)
    ]


def _speaking(count: int, start: float, first_id: int) -> list[Segment]:
    return [
        Segment(
            id=first_id + index,
            start=start + index * 2,
            end=start + index * 2 + 1.5,
            text=f"{index}번째 가게에 들어와 봤습니다",
        )
        for index in range(count)
    ]


def _whisper_payload(offset: float, texts: list[str]) -> dict:
    """조각 좌표의 응답. 오프셋을 더하면 원본 오디오 시각이 된다(규칙 1)."""
    return {
        "segments": [
            {"id": index, "start": index * 2.0, "end": index * 2.0 + 1.8, "text": text}
            for index, text in enumerate(texts)
        ]
    }


@dataclass
class FakeTranscriber:
    """`kind`별로 정해둔 응답을 돌려준다. 호출 기록을 남겨 시도 횟수를 센다."""

    rescue: list[str] = field(default_factory=lambda: [CLEAN])
    arbiter_text: str = CLEAN
    calls: list[tuple[str, float]] = field(default_factory=list)
    explode: bool = False
    arbiter_explode: bool = False
    # 픽스처 경로(오디오 없음)에서는 조각이 없으므로 오프셋을 테스트가 알려줘야 한다.
    # 실제 실행에서는 ffmpeg가 잘라준 조각의 오프셋이 그대로 들어온다.
    offset: float = 10.0

    def transcribe(self, job: TranscriptionJob, cfg: Config) -> list[ChunkResponse]:
        self.calls.append((job.kind, cfg.transcribe_temperature))
        if self.explode and job.kind == RESCUE_KIND:
            raise RuntimeError("API가 죽었다")
        if self.arbiter_explode and job.kind == ARBITER_KIND:
            raise RuntimeError("심판 모델이 죽었다")
        offset = job.chunks[0].offset if job.chunks else self.offset
        if job.kind == ARBITER_KIND:
            return [ChunkResponse(index=0, offset=offset, payload={"text": self.arbiter_text})]
        return [
            ChunkResponse(index=0, offset=offset, payload=_whisper_payload(offset, self.rescue))
        ]


def _source() -> repair.Source:
    return repair.Source(stem="demo", duration=60.0)


def _settle(draft: list[Segment], transcriber: FakeTranscriber, cfg: Config) -> repair.Settled:
    return repair.settle(draft, _source(), cfg, transcriber)


# ─────────────────────────────────────────────────────────────
#  아무 일도 없을 때
# ─────────────────────────────────────────────────────────────


def test_a_clean_spine_is_returned_untouched() -> None:
    draft = _speaking(6, start=0.0, first_id=0)
    fake = FakeTranscriber()
    settled = _settle(draft, fake, Config())
    assert list(settled.spine) == draft
    assert settled.repairs == ()
    assert fake.calls == [], "되풀이가 없으면 한 푼도 쓰지 않는다"


def test_disabling_repair_leaves_everything_alone() -> None:
    draft = _echoing(8)
    fake = FakeTranscriber()
    settled = _settle(draft, fake, data_replace(Config(), repair_enabled=False))
    assert list(settled.spine) == draft
    assert fake.calls == []


def test_zero_attempts_means_no_repair_not_silent_deletion() -> None:
    """시도 횟수를 0으로 두면 아무것도 묻지 않고 전부 접게 된다. 그건 재전사가 아니다."""
    fake = FakeTranscriber()
    settled = _settle(_echoing(8), fake, data_replace(Config(), repair_max_attempts=0))
    assert len(settled.spine) == 8
    assert fake.calls == []


def test_a_short_repeat_is_below_the_bar() -> None:
    """3초짜리 맞장구까지 다시 물어보지는 않는다."""
    cfg = data_replace(Config(), repair_min_seconds=60.0)
    fake = FakeTranscriber()
    settled = _settle(_echoing(8), fake, cfg)
    assert len(settled.spine) == 8
    assert fake.calls == []


# ─────────────────────────────────────────────────────────────
#  재전사가 성공했을 때 — 갈아끼운다
# ─────────────────────────────────────────────────────────────


def test_a_recovered_span_is_replaced() -> None:
    settled = _settle(_echoing(8), FakeTranscriber(rescue=[CLEAN]), Config())
    assert [repair_.outcome for repair_ in settled.repairs] == [REPAIR_REPLACED]
    assert [segment.text for segment in settled.spine] == [CLEAN]


def test_the_arbiter_is_not_paid_for_when_whisper_recovers() -> None:
    """첫 시도에 회복하면 심판의 답이 바꿀 것이 없다. 부르지 않는다."""
    fake = FakeTranscriber(rescue=[CLEAN])
    _settle(_echoing(8), fake, Config())
    assert [kind for kind, _ in fake.calls] == [RESCUE_KIND]


def test_the_arbiter_is_asked_once_even_across_attempts() -> None:
    """실패한 구간에만, 그리고 한 번만 묻는다."""
    fake = FakeTranscriber(rescue=[LOOP] * 12, arbiter_text=CLEAN)
    _settle(_echoing(8), fake, Config())
    kinds = [kind for kind, _ in fake.calls]
    assert kinds.count(ARBITER_KIND) == 1
    assert kinds.count(RESCUE_KIND) == Config().repair_max_attempts


def test_replacement_times_stay_on_the_original_timeline() -> None:
    """규칙 1 — 시각은 `세그먼트 시각 + 조각 오프셋`이다. 그 밖의 계산은 없다."""
    draft = _echoing(8, start=10.0)
    offset = repair.plan_spans(draft, _source(), Config())[0][0]
    assert offset == 10.0, "이웃이 없으면 구간은 되풀이 세그먼트의 시작에서 시작한다"
    fake = FakeTranscriber(rescue=[CLEAN, "그리고 여기가 광장입니다"], offset=offset)
    settled = _settle(draft, fake, Config())
    assert [(segment.start, segment.end) for segment in settled.spine] == [
        (offset + 0.0, offset + 1.8),
        (offset + 2.0, offset + 3.8),
    ]


def test_segments_outside_the_repeat_are_never_touched() -> None:
    """규칙 9 — 되풀이 밖은 한 글자도 건드리지 않는다."""
    before = _speaking(3, start=0.0, first_id=0)
    after = _speaking(3, start=40.0, first_id=100)
    draft = [*before, *_echoing(8, start=20.0), *after]
    settled = _settle(draft, FakeTranscriber(), Config())
    survived = [segment.text for segment in settled.spine]
    for original in (*before, *after):
        assert original.text in survived


def test_the_settled_spine_is_ordered_and_renumbered() -> None:
    draft = [*_speaking(2, start=0.0, first_id=0), *_echoing(8, start=20.0)]
    settled = _settle(draft, FakeTranscriber(), Config())
    assert [segment.id for segment in settled.spine] == list(range(len(settled.spine)))
    starts = [segment.start for segment in settled.spine]
    assert starts == sorted(starts)


# ─────────────────────────────────────────────────────────────
#  회복 못 했을 때 — 첫 장만 남기고 비운다
# ─────────────────────────────────────────────────────────────


def test_a_span_that_loops_again_is_collapsed() -> None:
    """1668장 대신 한 장. 나머지 구간은 자막이 서지 않는다."""
    fake = FakeTranscriber(rescue=[LOOP] * 12, arbiter_text=LOOP * 12)
    settled = _settle(_echoing(8), fake, Config())
    assert [repair_.outcome for repair_ in settled.repairs] == [REPAIR_COLLAPSED]
    assert [segment.text for segment in settled.spine] == [LOOP]


def test_a_dead_api_collapses_instead_of_killing_the_run() -> None:
    """재전사가 죽어도 파이프라인은 계속 간다. 다만 조용하지는 않다."""
    fake = FakeTranscriber(explode=True)
    settled = _settle(_echoing(8), fake, Config())
    assert [repair_.outcome for repair_ in settled.repairs] == [REPAIR_COLLAPSED]
    assert len(settled.spine) == 1


def test_a_stale_fixture_offset_is_refused_not_forced_into_place() -> None:
    """낡은 픽스처가 엉뚱한 오프셋을 들고 오면 억지로 붙이지 않는다.

    가장 가까운 조각이라고 무조건 짝지으면, 남의 구간에서 들은 말이 이 자리에 선다 —
    시각이 whisper 응답에서 온다는 보장이 그 순간 무의미해진다(규칙 1).
    """
    draft = _echoing(8, start=10.0)
    span = repair.plan_spans(draft, _source(), Config())[0]
    chunks = (Chunk(path=Path("nowhere.mp3"), offset=span[0], duration=span[1] - span[0], index=0),)
    far = [ChunkResponse(index=0, offset=span[0] + 900.0, payload={"text": CLEAN})]
    assert repair._by_span(far, chunks) == [], "900초 떨어진 응답을 붙였다"
    near = [ChunkResponse(index=0, offset=span[0] + 0.03, payload={"text": CLEAN})]
    assert len(repair._by_span(near, chunks)) == 1, "프레임 경계 차이는 같은 조각이다"


def test_a_silent_arbiter_is_not_reported_as_a_verdict() -> None:
    """심판을 못 부른 것과 심판이 '되풀이가 맞다'고 한 것은 다르다.

    사람이 `repair_max_attempts`를 올릴지 API를 고칠지가 갈린다.
    """
    fake = FakeTranscriber(rescue=[LOOP] * 12, arbiter_explode=True)
    settled = _settle(_echoing(8), fake, Config())
    record = settled.repairs[0]
    assert not record.arbiter_answered
    assert record.verdict == "심판 판정 없음"


# ─────────────────────────────────────────────────────────────
#  심판 모델 — 두 번째 모델이 실제로 하는 일
# ─────────────────────────────────────────────────────────────


def test_the_arbiter_buys_another_attempt_when_it_hears_speech() -> None:
    """심판이 '여기 말이 있다'고 하면 온도를 올려 한 번 더 묻는다."""
    fake = FakeTranscriber(rescue=[LOOP] * 12, arbiter_text=CLEAN)
    settled = _settle(_echoing(8), fake, Config())
    rescue_calls = [call for call in fake.calls if call[0] == RESCUE_KIND]
    assert len(rescue_calls) == Config().repair_max_attempts
    assert rescue_calls[0][1] != rescue_calls[1][1], "같은 온도로 다시 물으면 같은 답이 온다"
    assert settled.repairs[0].arbiter_clean


def test_the_arbiter_stops_the_retry_when_it_hears_the_same_loop() -> None:
    """심판도 되풀이라고 하면 화자가 정말 되풀이한 것이다. 더 살 이유가 없다."""
    fake = FakeTranscriber(rescue=[LOOP] * 12, arbiter_text=LOOP * 12)
    settled = _settle(_echoing(8), fake, Config())
    assert len([call for call in fake.calls if call[0] == RESCUE_KIND]) == 1
    assert not settled.repairs[0].arbiter_clean


def _straddling_draft() -> list[Segment]:
    """되풀이의 앞뒤 여유가 **세그먼트 한복판**에 떨어지는 골격.

    여유는 2초다. 되풀이가 13~21초이므로 여유는 11~23초이고, 앞뒤 문장이 각각 그
    안으로 걸쳐 들어온다.
    """
    return [
        Segment(id=0, start=0.0, end=5.0, text="맨 앞 문장"),
        Segment(id=1, start=6.0, end=12.5, text="앞에서 걸쳐 들어오는 문장"),
        *_echoing(8, start=13.0, first_id=2),
        Segment(id=10, start=22.0, end=29.0, text="뒤로 걸쳐 나가는 문장"),
        Segment(id=11, start=35.0, end=40.0, text="맨 뒤 문장"),
    ]


def test_no_segment_is_removed_without_being_re_asked() -> None:
    """**조용한 손실의 회귀 테스트(규칙 9).**

    구간을 먼저 정하고 걸치는 세그먼트를 지우면, 재전사가 덮지 않는 꼬리가 남는다.
    경고도 없이 자막이 빈다 — 접기와 달리 이건 허용된 예외가 아니다.

    그래서 순서가 반대여야 한다: **세그먼트를 통째로 데려간 뒤 그 범위를 구간으로 삼는다.**
    """
    draft = _straddling_draft()
    edges = {segment.start for segment in draft} | {segment.end for segment in draft}
    spans = repair.plan_spans(draft, _source(), Config())
    assert spans, "되풀이를 못 찾았다면 테스트 자체가 틀렸다"
    for low, high in spans:
        assert low in edges, f"구간 시작 {low}이 세그먼트 경계가 아니다"
        assert high in edges, f"구간 끝 {high}이 세그먼트 경계가 아니다"
        # 걸치는 세그먼트는 반이 아니라 통째로 구간 안에 있어야 한다
        for segment in draft:
            crosses = segment.start < high and segment.end > low
            if crosses:
                assert low <= segment.start and segment.end <= high, (
                    f"세그먼트 {segment.id}({segment.start}~{segment.end})가 "
                    f"구간 {low}~{high} 경계에 걸쳤다 — 지우면 꼬리가 사라진다"
                )


def test_speech_straddling_the_span_is_not_duplicated() -> None:
    """걸친 세그먼트는 대체본과 같은 오디오를 말한다. 둘이 함께 남으면 글자가 두 번 나온다."""
    draft = _straddling_draft()
    span = repair.plan_spans(draft, _source(), Config())[0]
    fake = FakeTranscriber(rescue=[CLEAN], offset=span[0])
    settled = _settle(draft, fake, Config())
    texts = [segment.text for segment in settled.spine]
    assert "앞에서 걸쳐 들어오는 문장" not in texts
    assert "뒤로 걸쳐 나가는 문장" not in texts
    assert "맨 앞 문장" in texts and "맨 뒤 문장" in texts


def test_a_long_recovery_is_not_collapsed_for_being_long() -> None:
    """**회복본이 길기만 해도 접히던 버그의 회귀 테스트.**

    되풀이 판정을 글자 전체 압축률로 하면 길이 검사가 된다. 27분짜리 구간을 완벽하게
    되살려도 접혔다 — D27이 고치려던 바로 그 사고다.
    """
    long_clean = [
        f"{index}번 골목을 지나면 광장이 나오고 사람들이 앉아 이야기합니다" for index in range(200)
    ]
    settled = _settle(_echoing(40, start=10.0), FakeTranscriber(rescue=long_clean), Config())
    assert [record.outcome for record in settled.repairs] == [REPAIR_REPLACED]
    assert len(settled.spine) == len(long_clean)


def test_neighbouring_repeats_never_share_a_segment() -> None:
    """되풀이 둘 사이가 여유보다 좁으면, 한 세그먼트를 두 계획이 가져갈 수 있다.

    그러면 같은 자리를 두 번 갈아끼워 글자가 두 번 들어간다.
    """
    draft = [
        *_echoing(4, start=10.0, text=LOOP, first_id=0),
        *_echoing(4, start=16.0, text="소세지 소세지", first_id=4),
    ]
    spans = repair.plan_spans(draft, _source(), Config())
    for earlier, later in zip(spans, spans[1:], strict=False):
        assert earlier[1] <= later[0], f"구간이 겹쳤다: {spans}"


def test_overlapping_spans_lose_a_repair_not_a_subtitle() -> None:
    """구간이 겹치면 고치지 않고 남긴다. **못 고치는 것이 잘못 고치는 것보다 낫다.**

    whisper는 시각이 겹치는 세그먼트를 내놓는다. 여기서는 긴 문장 하나가 앞 되풀이의
    여유에 딸려가면서 뒤 되풀이의 구간까지 밀고 들어간다. 그대로 두면 조각 수가 구간
    수와 어긋나 재전사가 통째로 취소되고, 억지로 짝지으면 남의 오디오를 갈아끼운다.
    """
    draft = [
        *_echoing(3, start=10.0, text=LOOP, first_id=0),
        Segment(id=3, start=12.5, end=20.0, text="앞 되풀이에 딸려가는 긴 문장"),
        *_echoing(3, start=19.5, text="소세지 소세지", first_id=4),
    ]
    spans = repair.plan_spans(draft, _source(), Config())
    for earlier, later in zip(spans, spans[1:], strict=False):
        assert earlier[1] <= later[0], f"구간이 겹쳤다: {spans}"
    settled = _settle(draft, FakeTranscriber(rescue=[CLEAN]), Config())
    kept = [segment.text for segment in settled.spine]
    assert "소세지 소세지" in kept, "재전사를 못 해도 자막은 남아야 한다(규칙 9)"


def test_no_segment_belongs_to_two_plans() -> None:
    """한 세그먼트가 두 계획에 속하면 같은 자리를 두 번 갈아끼운다."""
    draft = [
        *_echoing(4, start=10.0, text=LOOP, first_id=0),
        *_echoing(4, start=16.0, text="소세지 소세지", first_id=4),
    ]
    settled = _settle(draft, FakeTranscriber(rescue=[CLEAN]), Config())
    texts = [segment.text for segment in settled.spine]
    assert len(texts) == len(set(texts)), f"같은 글자가 두 번 들어갔다: {texts}"


def test_the_repair_record_counts_what_changed() -> None:
    settled = _settle(_echoing(8), FakeTranscriber(rescue=[CLEAN]), Config())
    record = settled.repairs[0]
    assert record.before == 8
    assert record.after == 1
    assert record.repeat.count == 8
