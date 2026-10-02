"""A5 검토용 자막 투영의 계약 — 좌표·자르기·무근거 큐 금지·WebVTT 왕복.

`docs/plans/2026-10-02-raw-video-browser-v2.md` §10.2·§10.3·§13.3·16-A5. 발화
증거는 `understanding.UtteranceEvidence`를 **그대로** 만들어 구조 계약을 증명하고,
시각은 전부 정확 유리수로 잡는다. 네트워크·ffmpeg·파일이 없다.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from fractions import Fraction

import pytest

from vlogsrt.project.raw.review_projection import (
    BOUNDARY_REVIEW_NOTE,
    NO_TIMED_EVIDENCE,
    TRANSCRIPT_STATUS_PREFIX,
    ClipWindow,
    ProjectionClock,
    ProjectionError,
    RenderedVtt,
    UtteranceLike,
    parse_review_vtt,
    project_review_vtt,
    render_review_vtt,
    serialize_review_vtt,
    timed_utterances,
)
from vlogsrt.project.raw.segment_review import (
    DISPLAY_VIDEO_ONLY,
    DISPLAY_VIDEO_WITH_TRANSCRIPT,
    SPEECH_ANALYSIS_FAILED,
    SPEECH_AUDIO_ABSENT,
    SPEECH_DETECTED,
    SPEECH_UNCONFIRMED_EMPTY,
    ReviewViewState,
    SourceClock,
    initial_revision,
    make_source_clock,
)
from vlogsrt.project.raw.understanding import UtteranceEvidence

TB_NUM, TB_DEN = 1, 90000
FRAME_30 = Fraction(1, 30)
SECOND = 90_000


def clock() -> SourceClock:
    return make_source_clock("source_a", "sha-a", TB_NUM, TB_DEN, 0, 40 * SECOND, FRAME_30)


@dataclass(frozen=True, slots=True)
class PlanEntry:
    segment_id: str
    core_start_pts: int
    core_end_pts: int
    speech_status: str
    asset_id: str = "source_a"
    source_order: int = 0
    segment_order: int = 0
    time_base_num: int = TB_NUM
    time_base_den: int = TB_DEN
    recommendation: str = "normal"


def revision_with_speech():
    """40초 원본을 4조각으로 — a2(10~22초)만 발화가 측정돼 있다."""
    return initial_revision(
        "proj",
        "plan-1",
        (clock(),),
        (
            PlanEntry("a1", 0, 10 * SECOND, SPEECH_AUDIO_ABSENT),
            PlanEntry("a2", 10 * SECOND, 22 * SECOND, SPEECH_DETECTED),
            PlanEntry("a3", 22 * SECOND, 31 * SECOND, SPEECH_ANALYSIS_FAILED),
            PlanEntry("a4", 31 * SECOND, 40 * SECOND, SPEECH_UNCONFIRMED_EMPTY),
        ),
    )


def exact(value) -> Fraction:
    """float를 정확 유리수로 — `str`을 거쳐 이진 오차를 남기지 않는다."""
    return Fraction(str(value))


def utterance(utterance_id, start_s, end_s, text) -> UtteranceEvidence:
    return UtteranceEvidence(
        utterance_id=utterance_id,
        chunk_index=0,
        position=0,
        text=text,
        start=exact(start_s),
        end=exact(end_s),
        start_sample=int(float(start_s) * 16000),
        end_sample=int(float(end_s) * 16000),
        evidence_id=f"req:{utterance_id}",
    )


def clip(start_s, end_s) -> ClipWindow:
    return ClipWindow("clip_a2", "source_a", int(start_s * SECOND), int(end_s * SECOND))


def projection_clock() -> ProjectionClock:
    """발화 파일축 0초 == 원본 PTS 0 — A/V 오프셋 없는 기본 다리."""
    return ProjectionClock(TB_NUM, TB_DEN, Fraction(0))


def project(revision, utterances, window=None, *, clock_=None, language="ko"):
    return project_review_vtt(
        revision,
        "a2",
        window or clip(10, 22),
        utterances,
        clock_ or projection_clock(),
        language=language,
    )


# ─────────────────────────────────────────────────────────────
#  좌표 — 원점은 딱 한 번
# ─────────────────────────────────────────────────────────────


def test_utterance_evidence_satisfies_protocol():
    assert isinstance(utterance("u1", 11, 12, "안녕"), UtteranceLike)


def test_pcm_origin_is_applied_once():
    evidence = (utterance("u1", 9, 10, "첫 문장"), utterance("u2", 11.5, 13, "둘째"))
    timed = timed_utterances(evidence, Fraction(2))
    assert [unit.start_file for unit in timed] == [Fraction(11), Fraction(27, 2)]
    assert [unit.end_file for unit in timed] == [Fraction(12), Fraction(15)]
    # 원점이 이미 더해진 파일 축을 투영하면 클립 국소 좌표가 정확히 나온다(10초 창).
    result = project(revision_with_speech(), timed)
    assert [(cue.start, cue.end) for cue in result.cues] == [
        (Fraction(1), Fraction(2)),
        (Fraction(7, 2), Fraction(5)),
    ]
    assert result.cues[0].text == "첫 문장"


def test_media_start_maps_to_local_time_exactly():
    result = project(
        revision_with_speech(),
        timed_utterances((utterance("u1", 10.5, 12, "도착"),), Fraction(0)),
    )
    (cue,) = result.cues
    assert cue.start == Fraction(1, 2)
    assert cue.end == Fraction(2)
    assert cue.boundary_clipped is False


def test_delayed_pcm_and_av_offset_project_through_one_bridge():
    # PCM이 2초 늦게 시작: 정규화 축 0초가 파일 2초다.
    timed = timed_utterances((utterance("u1", 0.5, 1.5, "인사"),), Fraction(2))
    # 파일축 0초가 원본 PTS 1초에 앉아 있다고 잰 다리 하나로 잇는다.
    bridge = ProjectionClock(TB_NUM, TB_DEN, Fraction(SECOND))
    result = project(revision_with_speech(), timed, window=clip(3, 5), clock_=bridge)
    (cue,) = result.cues
    # 파일 2.5~3.5초 → 원본 3.5~4.5초 → 클립 국소 0.5~1.5초.
    assert (cue.start, cue.end) == (Fraction(1, 2), Fraction(3, 2))
    assert cue.utterance_id == "u1"


# ─────────────────────────────────────────────────────────────
#  자르기와 무근거 큐 금지
# ─────────────────────────────────────────────────────────────


def test_boundary_spanning_cues_are_clamped_and_flagged():
    timed = timed_utterances(
        (
            utterance("u1", 9, 11, "경계 앞"),
            utterance("u2", 12, 23, "경계 뒤"),
            utterance("u3", 30, 33, "다른 클립"),
        ),
        Fraction(0),
    )
    result = project(revision_with_speech(), timed)
    cues = {cue.utterance_id: cue for cue in result.cues}
    assert set(cues) == {"u1", "u2"}
    assert (cues["u1"].start, cues["u1"].end) == (Fraction(0), Fraction(1))
    assert (cues["u2"].start, cues["u2"].end) == (Fraction(2), Fraction(12))
    assert cues["u1"].boundary_clipped and cues["u2"].boundary_clipped
    assert BOUNDARY_REVIEW_NOTE in result.status_notes
    assert cues["u1"].text == "경계 앞" and cues["u2"].text == "경계 뒤"


def test_statuses_without_timed_evidence_are_recorded_distinctly():
    revision = revision_with_speech()
    windows = {"a1": clip(0, 10), "a3": clip(22, 31), "a4": clip(31, 40)}
    notes = {}
    for segment_id, window in windows.items():
        result = project_review_vtt(revision, segment_id, window, (), projection_clock())
        notes[segment_id] = result.status_notes
        assert result.cues == ()
    assert notes["a1"] == (f"{TRANSCRIPT_STATUS_PREFIX}{SPEECH_AUDIO_ABSENT}",)
    assert notes["a3"] == (f"{TRANSCRIPT_STATUS_PREFIX}{SPEECH_ANALYSIS_FAILED}",)
    assert notes["a4"] == (f"{TRANSCRIPT_STATUS_PREFIX}{SPEECH_UNCONFIRMED_EMPTY}",)
    assert len(set(notes.values())) == 3


def test_detected_without_utterances_and_zero_length_utterances():
    revision = revision_with_speech()
    empty = project(revision, ())
    assert empty.cues == ()
    assert empty.status_notes == (f"{TRANSCRIPT_STATUS_PREFIX}{NO_TIMED_EVIDENCE}",)
    zero = project(revision, timed_utterances((utterance("u0", 15, 15, "길이 0"),), Fraction(0)))
    assert zero.cues == ()
    assert any(note.startswith("zero_length_utterance:") for note in zero.status_notes)


def test_one_cue_per_measured_utterance_no_equal_splitting():
    timed = timed_utterances(
        (
            utterance("u1", 10.2, 11.1, "하나"),
            utterance("u2", 13, 19.9, "둘"),
            utterance("u3", 20, 21.4, "셋"),
        ),
        Fraction(0),
    )
    result = project(revision_with_speech(), timed, language="en")
    assert [cue.utterance_id for cue in result.cues] == ["u1", "u2", "u3"]
    assert [(cue.start, cue.end) for cue in result.cues] == [
        (Fraction(1, 5), Fraction(11, 10)),
        (Fraction(3), Fraction(99, 10)),
        (Fraction(10), Fraction(57, 5)),
    ]
    assert result.language == "en"


def test_projection_contract_refusals():
    revision = revision_with_speech()
    with pytest.raises(ProjectionError, match="조각"):
        project_review_vtt(revision, "nope", clip(10, 22), (), projection_clock())
    with pytest.raises(ProjectionError, match="다른 원본"):
        project_review_vtt(
            revision, "a2", ClipWindow("c", "source_b", 0, SECOND), (), projection_clock()
        )
    with pytest.raises(ProjectionError, match="격자·범위 밖"):
        project_review_vtt(
            revision,
            "a2",
            ClipWindow("c", "source_a", SECOND + 1, 22 * SECOND),
            (),
            projection_clock(),
        )
    with pytest.raises(ProjectionError, match="뒤집혔다"):
        project_review_vtt(revision, "a2", clip(22, 10), (), projection_clock())
    with pytest.raises(ProjectionError, match="timebase"):
        project_review_vtt(
            revision,
            "a2",
            clip(10, 22),
            (),
            ProjectionClock(1, 60000, Fraction(0)),
        )


# ─────────────────────────────────────────────────────────────
#  WebVTT — 이스케이프·양자화·왕복·거부
# ─────────────────────────────────────────────────────────────


def result_for_projection():
    timed = timed_utterances(
        (
            utterance("u1", 10.5, 12, "안녕 <b>&</b> --><![CDATA[x]]>"),
            utterance("u2", 13, 14, "줄바꿈\n둘째 줄"),
            utterance("u3", 20.25, 20.75, "0.5초"),
        ),
        Fraction(0),
    )
    return project(revision_with_speech(), timed)


def test_vtt_bytes_are_deterministic_and_reparse_roundtrips():
    result = result_for_projection()
    document = serialize_review_vtt(result)
    assert document.startswith("WEBVTT\n\n")
    assert document.endswith("\n")
    assert serialize_review_vtt(result) == document
    parsed = parse_review_vtt(document)
    rendered = render_review_vtt(result)
    assert len(parsed) == len(result.cues)
    assert [cue.identifier for cue in parsed] == [str(cue.index) for cue in rendered.cues]
    assert [cue.start_ms for cue in parsed] == [cue.start_ms for cue in rendered.cues]
    assert [cue.end_ms for cue in parsed] == [cue.end_ms for cue in rendered.cues]
    assert parsed[0].text == "안녕 <b>&</b> --><![CDATA[x]]>"
    assert parsed[1].text == "줄바꿈\n둘째 줄"
    assert parsed[2].text == "0.5초"


def test_vtt_escaping_hides_arrows_and_markup():
    document = serialize_review_vtt(result_for_projection())
    text_lines = [line for line in document.split("\n")[2:] if line and "-->" not in line]
    assert "안녕 &lt;b&gt;&amp;&lt;/b&gt; --&gt;&lt;![CDATA[x]]&gt;" in text_lines


def test_sub_millisecond_cue_gets_recorded_floor():
    result = project(
        revision_with_speech(),
        timed_utterances((utterance("u1", 10.0001, 10.0003, "짧다"),), Fraction(0)),
    )
    rendered = render_review_vtt(result)
    assert rendered.notes == ("degenerate_ms_floor:u1",)
    (cue,) = rendered.cues
    assert (cue.start_ms, cue.end_ms) == (0, 1)
    assert "00:00:00.000 --> 00:00:00.001" in rendered.text


def test_display_mode_toggle_changes_no_bytes():
    revision = revision_with_speech()
    view = ReviewViewState()
    with_transcript = view.with_display_mode(DISPLAY_VIDEO_WITH_TRANSCRIPT)
    timed = timed_utterances((utterance("u1", 11, 12, "같은 문장"),), Fraction(0))
    assert serialize_review_vtt(project(revision, timed)) == serialize_review_vtt(
        project(revision, timed)
    )
    assert view.display_mode == DISPLAY_VIDEO_ONLY
    assert with_transcript.display_mode == DISPLAY_VIDEO_WITH_TRANSCRIPT


def test_parse_refuses_foreign_vtt_documents():
    with pytest.raises(ProjectionError, match="머리"):
        parse_review_vtt("")
    with pytest.raises(ProjectionError, match="머리"):
        parse_review_vtt("SRT가 아니다\n\n1\n00:00:01.000 --> 00:00:02.000\n안녕\n")
    with pytest.raises(ProjectionError, match="NOTE"):
        parse_review_vtt("WEBVTT\n\nNOTE 이건 검토용 문서에 없다\n")
    with pytest.raises(ProjectionError, match="뒤집"):
        parse_review_vtt("WEBVTT\n\n1\n00:00:02.000 --> 00:00:01.000\n안녕\n")
    with pytest.raises(ProjectionError, match="타임라인"):
        parse_review_vtt("WEBVTT\n\n1\n00:01.000 --> 00:02.000\n안녕\n")
    with pytest.raises(ProjectionError, match="문장 없는"):
        parse_review_vtt("WEBVTT\n\n1\n00:00:01.000 --> 00:00:02.000\n\n")


def test_render_exposes_notes_and_ms_cues():
    rendered = render_review_vtt(result_for_projection())
    assert isinstance(rendered, RenderedVtt)
    assert [(cue.start_ms, cue.end_ms) for cue in rendered.cues] == [
        (500, 2000),
        (3000, 4000),
        (10250, 10750),
    ]
    assert rendered.text.count(" --> ") == 3


def test_projection_does_not_mutate_revision():
    revision = revision_with_speech()
    snapshot = dataclasses.replace(revision)
    project(revision, timed_utterances((utterance("u1", 11, 12, "x"),), Fraction(0)))
    assert revision == snapshot
