"""검증 — 완성된 자막을 gpt 텍스트와 대조해 의심 큐만 표시한다.

검증은 읽기 전용이다(불변 규칙 4). 여기 테스트는 두 가지를 고정한다:
의심 판정이 맞는 큐를 짚는가, 그리고 검증이 입력을 절대 바꾸지 않는가.
"""

from __future__ import annotations

from vlogsrt.config import Config
from vlogsrt.transcribe import ChunkResponse
from vlogsrt.verify import Verification, chunk_spans, summarize, verify

from .conftest import FIXTURE_DIR, make_cue


def gpt_response(text: str, *, offset: float = 0.0, index: int = 0) -> ChunkResponse:
    return ChunkResponse(index=index, offset=offset, payload={"text": text})


# ─────────────────────────────────────────────────────────────
#  청크 구간
# ─────────────────────────────────────────────────────────────


def test_chunk_spans_tile_the_duration() -> None:
    responses = [gpt_response("", offset=o, index=i) for i, o in enumerate([0.0, 600.0, 1200.0])]
    assert chunk_spans(responses, duration=1500.0) == [
        (0.0, 600.0),
        (600.0, 1200.0),
        (1200.0, 1500.0),
    ]


def test_a_single_chunk_covers_everything() -> None:
    assert chunk_spans([gpt_response("안녕")], duration=300.0) == [(0.0, 300.0)]


# ─────────────────────────────────────────────────────────────
#  큐 일치율
# ─────────────────────────────────────────────────────────────


def test_matching_subtitles_raise_no_suspects(cfg: Config) -> None:
    cues = [
        make_cue(0.0, 2.0, "오늘은 망원동에 왔어요", index=1),
        make_cue(2.5, 4.5, "빵집부터 가보겠습니다", index=2),
    ]
    responses = [gpt_response("오늘은 망원동에 왔어요 빵집부터 가보겠습니다")]
    verification = verify(cues, responses, duration=5.0, cfg=cfg)
    assert verification.suspects == ()
    assert verification.cue_total == 2
    assert all(not chunk.flagged for chunk in verification.chunks)


def test_disjoint_text_flags_the_cue(cfg: Config) -> None:
    cues = [make_cue(0.0, 2.0, "여기가 보통은 진짜 맛집이에요", index=1)]
    responses = [gpt_response("전혀 다른 내용의 문장입니다 하나도 안 겹칩니다")]
    verification = verify(cues, responses, duration=3.0, cfg=cfg)
    assert len(verification.suspects) == 1
    suspect = verification.suspects[0]
    assert suspect.cue_index == 1
    assert suspect.coverage < cfg.verify_cue_match_floor
    assert suspect.text == "여기가 보통은 진짜 맛집이에요"


def test_short_interjections_are_exempt(cfg: Config) -> None:
    """ "네", "음" 같은 짧은 큐는 어디에나 우연히 일치·불일치한다. 대조 무의미."""
    cues = [make_cue(0.0, 1.0, "네.", index=1)]
    responses = [gpt_response("전혀 다른 긴 문장이 이어집니다")]
    assert verify(cues, responses, duration=2.0, cfg=cfg).suspects == ()


def test_spacing_and_punctuation_do_not_matter(cfg: Config) -> None:
    cues = [make_cue(0.0, 2.0, "안녕하세요, 여러분!", index=1)]
    responses = [gpt_response("안녕 하세요 여러분")]
    assert verify(cues, responses, duration=3.0, cfg=cfg).suspects == ()


def test_cues_are_compared_against_their_own_chunk(cfg: Config) -> None:
    """뒤 청크의 gpt 텍스트가 앞 큐를 구제하면 안 된다. 구간별 대조다."""
    cues = [
        make_cue(100.0, 102.0, "카페에 도착했습니다", index=1),
        make_cue(700.0, 702.0, "요리를 시작해 볼게요", index=2),
    ]
    responses = [
        gpt_response("요리를 시작해 볼게요", offset=0.0, index=0),
        gpt_response("카페에 도착했습니다", offset=600.0, index=1),
    ]
    verification = verify(cues, responses, duration=1200.0, cfg=cfg)
    assert [suspect.cue_index for suspect in verification.suspects] == [1, 2]


# ─────────────────────────────────────────────────────────────
#  청크 유사도 — 자막이 발화를 놓친 구간
# ─────────────────────────────────────────────────────────────


def test_missing_speech_flags_the_chunk_not_the_cues(cfg: Config) -> None:
    """큐가 gpt 안에 다 있어도 gpt에 자막 없는 발화가 많으면 청크가 표시된다.

    강등·판정 필터가 실제 발화를 떨궜을 때 사람이 알아채는 경로다.
    큐 자체는 멀쩡하므로 의심 큐 목록에는 올리지 않는다.
    """
    cues = [make_cue(0.0, 2.0, "시작 인사말입니다", index=1)]
    long_missing = "시작 인사말입니다 " + "자막에는 없는 발화가 아주 길게 이어지는 구간입니다 " * 10
    verification = verify(cues, [gpt_response(long_missing)], duration=60.0, cfg=cfg)
    assert verification.chunks[0].flagged
    assert verification.suspects == ()


# ─────────────────────────────────────────────────────────────
#  순수성 — 검증은 읽기 전용이다 (불변 규칙 4)
# ─────────────────────────────────────────────────────────────


def test_verify_returns_flags_and_touches_nothing(cfg: Config) -> None:
    cues = [make_cue(0.0, 2.0, "오늘은 망원동에 왔어요", index=1)]
    before = [(cue.start, cue.end, cue.text, cue.flags) for cue in cues]
    verification = verify(cues, [gpt_response("무관한 텍스트")], duration=3.0, cfg=cfg)
    assert isinstance(verification, Verification)
    assert [(cue.start, cue.end, cue.text, cue.flags) for cue in cues] == before


def test_empty_inputs_do_not_crash(cfg: Config) -> None:
    verification = verify([], [gpt_response("")], duration=10.0, cfg=cfg)
    assert verification.cue_total == 0
    assert verification.suspects == ()


# ─────────────────────────────────────────────────────────────
#  요약 문구
# ─────────────────────────────────────────────────────────────


def test_summarize_counts_suspects_and_flagged_chunks(cfg: Config) -> None:
    cues = [
        make_cue(0.0, 2.0, "여기가 보통은 진짜 맛집이에요", index=1),
        make_cue(2.5, 4.5, "그래서 오늘도 와봤습니다", index=2),
    ]
    responses = [gpt_response("그래서 오늘도 와봤습니다")]
    line = summarize(verify(cues, responses, duration=5.0, cfg=cfg))
    assert "큐 2개 중 1개 의심" in line
    assert "50.0%" in line


# ─────────────────────────────────────────────────────────────
#  실측 픽스처 — 77분 녹화 전체
# ─────────────────────────────────────────────────────────────


def test_real_fixture_end_to_end(cfg: Config) -> None:
    from vlogsrt import pipeline, transcribe
    from vlogsrt.models import VERDICT_OK

    fixture_dir = FIXTURE_DIR / "api"
    transcriber = transcribe.FixtureTranscriber(fixture_dir)
    job = transcribe.TranscriptionJob(stem="fr_day_1_2_fhd", chunks=())
    meta = transcribe.load_meta(fixture_dir, "fr_day_1_2_fhd")
    silences = tuple(tuple(span) for span in meta["silences"])

    spine, _ = pipeline.fetch_spine(transcriber, job, cfg, silences)
    ok_segments = [segment for segment in spine if segment.verdict == VERDICT_OK]
    cues = [
        make_cue(seg.start, seg.end, seg.text.strip(), seg_ids=(seg.id,), index=position + 1)
        for position, seg in enumerate(ok_segments)
    ]
    responses = transcriber.gpt(job, cfg)

    verification = verify(cues, responses, duration=float(meta["duration"]), cfg=cfg)

    assert verification.cue_total == len(cues) > 0
    assert len(verification.chunks) == len(responses)
    cue_indexes = {cue.index for cue in cues}
    assert all(suspect.cue_index in cue_indexes for suspect in verification.suspects)
    assert 0 <= len(verification.suspects) <= verification.cue_total
    assert "검증: 큐" in summarize(verification)
