"""A7-3 검토·확정 — 실제 자동 실행 위의 동결·리비전·게이트·final 게시 검사.

엔진은 가짜 제공자로 실제로 돌리고(유료 호출 없음), 그 결과를 A7-2 게시물과
검토 저장소 양쪽에 몰아 넣은 뒤 사람 편집·확정을 끝까지 돈다. 자동 증거가
하나도 바뀌지 않는다·구조 경색은 허용으로 안 열린다·final만 새 자리에 나온다는
것을 잰다(§11.7·§11.11·A7-3 완료 기준).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import date
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from vlogsrt.output import parse_srt
from vlogsrt.project.raw import caption_review as review
from vlogsrt.project.raw.caption_artifacts import (
    parse_vtt,
    publish_caption_artifacts,
)
from vlogsrt.project.raw.caption_config import FinalCaptionSettings
from vlogsrt.project.raw.caption_manual import (
    CaptionViewState,
    add_cue,
    allow_finding,
    edit_text,
    edit_time,
    merge_cues,
    ms,
    record_no_speech,
    resolve_unplaced,
    split_cue,
)
from vlogsrt.project.raw.caption_provider import (
    parse_text_response,
    parse_timing_response,
)
from vlogsrt.project.raw.caption_store import (
    CaptionStoreConflict,
    CaptionStoreIntegrityError,
)
from vlogsrt.project.raw.final_caption import (
    CaptionRunPlan,
    FinalCaptionResult,
    plan_caption_run,
    run_final_captions,
)
from vlogsrt.project.raw.final_video import FinalVideoChanged, FinalVideoInput
from vlogsrt.project.raw.understanding import PCM_SAMPLE_RATE, SelectedAudio

BEFORE = date(2026, 10, 2)
WRONG = "망원동 커피에 왔어요"
CORRECTED = "망원동 카페에 왔어요"


def make_video(
    tmp_path: Path,
    *,
    revision_body: bytes = b"synthetic-final-video",
    origin: Fraction = Fraction(0),
    seconds: float = 40.0,
    video_duration: float | None = None,
) -> FinalVideoInput:
    """합성 PCM 위의 최종 영상 입력 — 리비전은 내용 해시, 원점은 잰 값이다."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    samples = int(seconds * PCM_SAMPLE_RATE)
    pcm = tmp_path / "caption.pcm"
    pcm.write_bytes(bytes((index * 7 + 3) % 256 for index in range(samples * 2)))
    source = tmp_path / "final.mp4"
    source.write_bytes(revision_body)
    digest = hashlib.sha256(revision_body).hexdigest()
    return FinalVideoInput(
        source_id="final_test",
        final_video_revision=digest,
        original_path=source,
        verified_copy_path=source,
        verified_copy_sha256=digest,
        manifest_path=tmp_path / "final-video.json",
        audio=SelectedAudio(
            source_id="final_test",
            source_path=source,
            source_sha256=digest,
            audio_stream_index=1,
            pcm_path=pcm,
            pcm_sha256=hashlib.sha256(pcm.read_bytes()).hexdigest(),
            sample_rate=PCM_SAMPLE_RATE,
            channels=1,
            sample_count=samples,
            pcm_sample0_file_time=origin,
        ),
        video_duration=seconds + 0.5 if video_duration is None else video_duration,
        audio_stream_count=1,
    )


class FakeTiming:
    """시간 제공자 가짜 — 실패 조각과 호출 수를 잰다."""

    def __init__(
        self, segments_by_chunk: dict[int, list[dict[str, Any]]], *, fail: set[int] | None = None
    ) -> None:
        self.segments_by_chunk = segments_by_chunk
        self.fail = fail or set()
        self.calls: list[str] = []

    def timed_transcript(
        self, *, binding: Any, chunk: Any, settings: Any, on: date, **kwargs: Any
    ) -> Any:
        self.calls.append(binding.request_id)
        if chunk.index in self.fail:
            raise RuntimeError("시간 제공자가 닫혀 있다")
        return parse_timing_response(
            {"segments": self.segments_by_chunk[chunk.index]},
            binding=binding,
            model=settings.models.timing_model,
            provider=settings.caption.timing_provider,
            chunk_duration=chunk.duration,
            chunk_offset=chunk.offset,
        )


class FakeText:
    """문구 제공자 가짜 — 실패 조각로 정직한 partial_failure를 만들 수 있다."""

    def __init__(self, text_by_chunk: dict[int, str], *, fail: set[int] | None = None) -> None:
        self.text_by_chunk = text_by_chunk
        self.fail = fail or set()
        self.calls: list[str] = []

    def text_transcript(
        self, *, binding: Any, chunk: Any, settings: Any, on: date, **kwargs: Any
    ) -> Any:
        self.calls.append(binding.request_id)
        if chunk.index in self.fail:
            raise RuntimeError("문구 제공자가 닫혀 있다")
        return parse_text_response(
            {"text": self.text_by_chunk[chunk.index]},
            binding=binding,
            model=settings.models.text_model,
        )


def default_fakes() -> tuple[FakeTiming, FakeText]:
    timing = FakeTiming(
        {
            0: [
                {"text": f" {WRONG}", "start": 0.5, "end": 2.5},
                {"text": " 다음 장면", "start": 3.0, "end": 5.0},
            ],
            1: [{"text": " 두번째 조각", "start": 1.0, "end": 3.0}],
            2: [{"text": "세번째 꼬리", "start": 0.5, "end": 2.0}],
        }
    )
    text = FakeText({0: f"{CORRECTED} 다음 장면", 1: "두번째 조각", 2: "세번째 꼬리"})
    return timing, text


def drive(
    tmp_path: Path,
    *,
    timing: FakeTiming | None = None,
    text: FakeText | None = None,
    run_id: str = "run-1",
    origin: Fraction = Fraction(0),
) -> tuple[FinalVideoInput, CaptionRunPlan, FinalCaptionResult, FakeTiming, FakeText]:
    """40초 오디오를 15초 청크 셋으로 실제 실행 — 교정이 기본 시나리오다."""
    video = make_video(tmp_path, origin=origin)
    plan = plan_caption_run(
        video, run_id=run_id, settings=FinalCaptionSettings(chunk_max_seconds=15.0)
    )
    timing = timing or default_fakes()[0]
    text = text or default_fakes()[1]
    result = run_final_captions(
        plan,
        on=BEFORE,
        timing_provider=timing,
        text_provider=text,
        workdir=tmp_path / "work",
    )
    return video, plan, result, timing, text


def prepared(
    tmp_path: Path, *, timing: FakeText | None = None, text: FakeText | None = None, **kwargs: Any
) -> tuple[review.ReviewStore, review.AutomaticSnapshot, review.ManualRevision, FinalVideoInput]:
    video, plan, result, _, _ = drive(tmp_path, timing=timing, text=text, **kwargs)  # type: ignore[arg-type]
    store = review.open_review_store(tmp_path / "review")
    snap = review.freeze_automatic(store, result, plan)
    head = review.initial_revision(snap)
    review.save_revision(store, head)
    return store, snap, head, video


def confirm(store: review.ReviewStore, video: FinalVideoInput, head: review.ManualRevision) -> Any:
    return review.confirm_final(
        store,
        video,
        head,
        review.ReviewConfirmation(cue_ids=tuple(cue.cue_id for cue in head.cues)),
    )


def snapshot_bytes(snap: review.AutomaticSnapshot) -> bytes:
    return snap.path.read_bytes()


def manual_chain(
    store: review.ReviewStore, snap: review.AutomaticSnapshot
) -> review.ManualRevision:
    """기본 리비전 위에 글자·시각·분할·병합·새 큐·허용을 차례로 얹는다."""
    head = review.initial_revision(snap)
    review.save_revision(store, head)
    for step in (
        lambda h: edit_text(h, "a0000", f"{CORRECTED} (다시 고침)"),
        lambda h: edit_time(h, "a0001", start=ms(2900)),
        lambda h: split_cue(h, "a0001", ms(3500), "다음", "장면"),
        lambda h: merge_cues(h, "m0001", "a0002"),
        lambda h: add_cue(h, ms(20000), ms(22000), "추가로 살린 말", note="미대응 처리"),
    ):
        head = step(head)
        review.save_revision(store, head)  # 연쇄는 디스크 위에서 이어진다
    for warning in review.review_state(snap, head).warnings:
        head = allow_finding(head, warning.key, "검토했다")  # 분할 짧은 표시 허용
        review.save_revision(store, head)
    return head


# ─────────────────────────────────────────────────────────────
#  전체 흐름 — 자동 증거 불변·호출 0회·final 새 자리
# ─────────────────────────────────────────────────────────────


def test_review_edits_and_confirm_freeze_all_automatic_evidence(tmp_path: Path) -> None:
    """글자·시각·분할·병합·새 큐 뒤에도 스냅샷·초안·응답 증거는 바이트 그대로다."""
    video, plan, result, timing, text = drive(tmp_path)
    calls_after_run = (len(timing.calls), len(text.calls))
    draft = publish_caption_artifacts(tmp_path / "out", result, plan)
    draft_bytes = {
        path: path.read_bytes() for path in [*draft.paths(), *draft.responses] if path.is_file()
    }
    store = review.open_review_store(tmp_path / "review")
    snap = review.freeze_automatic(store, result, plan)
    frozen_snapshot = snapshot_bytes(snap)
    spine_frozen = json.loads(snap.body)["content"]["spine"]  # 골격 증거

    head = manual_chain(store, snap)

    state = review.review_state(snap, head)
    assert state.head_revision == head.sequence
    assert (len(timing.calls), len(text.calls)) == calls_after_run  # 검토는 API를 안 부른다
    assert snapshot_bytes(snap) == frozen_snapshot
    assert json.loads(snap.body)["content"]["spine"] == spine_frozen
    for path, body in draft_bytes.items():
        assert path.read_bytes() == body  # 초안·응답 증거도 그대로다
    exported = confirm(store, video, head)
    assert exported.srt.is_file() and exported.vtt.is_file() and exported.manifest.is_file()
    assert "final.ko.srt" in str(exported.srt) and "draft.ko.srt" in str(draft.srt)
    assert snapshot_bytes(snap) == frozen_snapshot  # 확정 뒤에도 자동은 무사하다
    for path, body in draft_bytes.items():
        assert path.read_bytes() == body
    assert (len(timing.calls), len(text.calls)) == calls_after_run  # 확정도 API를 안 부른다
    manifest = json.loads(exported.manifest.read_text(encoding="utf-8"))
    assert manifest["text_model_complete"] and manifest["timing_model_complete"]
    assert manifest["manual_revision"] == head.sequence
    assert manifest["identity"]["run_id"] == "run-1"
    assert len(manifest["cues"]) == len(head.cues)


def test_draft_and_final_reparsed_to_same_cue_data(tmp_path: Path) -> None:
    """초안과 final 모두 재파싱해서 큐 자료와 같다 — 밀리초 반올림까지(§11.7)."""
    from vlogsrt.project.raw.caption_artifacts import caption_srt_text

    video, plan, result, *_ = drive(tmp_path)
    store = review.open_review_store(tmp_path / "review")
    snap = review.freeze_automatic(store, result, plan)
    head = review.initial_revision(snap)
    review.save_revision(store, head)
    exported = confirm(store, video, head)

    def rounded(value: Fraction) -> Fraction:
        return review._ms_of(value)

    draft_cues = parse_srt(caption_srt_text(result))
    assert len(draft_cues) == len(snap.cues)
    for parsed, cue in zip(draft_cues, snap.cues, strict=True):
        assert parsed.text == cue.text
        assert parsed.start == float(rounded(cue.start))
    final_cues = parse_srt(exported.srt.read_text(encoding="utf-8"))
    assert [(c.start, c.end, c.text) for c in final_cues] == [
        (float(rounded(c.start)), float(rounded(c.end)), c.text) for c in head.cues
    ]
    vtt_cues = parse_vtt(exported.vtt.read_text(encoding="utf-8"))
    assert [(s, e, t) for s, e, t in vtt_cues] == [
        (rounded(c.start), rounded(c.end), c.text) for c in head.cues
    ]


# ─────────────────────────────────────────────────────────────
#  게이트 — 구조 경색은 허용으로 못 뚫는다
# ─────────────────────────────────────────────────────────────


def test_confirm_requires_stored_head_and_complete_confirmation(tmp_path: Path) -> None:
    store, snap, head, video = prepared(tmp_path)
    unsaved = edit_text(head, "a0000", "저장 안 한 편집")
    with pytest.raises(review.StaleManualHead):
        confirm(store, video, unsaved)  # 저장되지 않은 리비전은 머리가 아니다
    newer = edit_text(head, "a0001", "다른 편집")
    review.save_revision(store, newer)
    with pytest.raises(review.StaleManualHead):
        confirm(store, video, head)  # 앞선 편집 위로 덮지 못한다
    with pytest.raises(review.ConfirmationIncomplete):
        review.confirm_final(store, video, newer, review.ReviewConfirmation(cue_ids=("a0000",)))
    with pytest.raises(review.ConfirmationIncomplete):
        review.confirm_final(
            store,
            video,
            newer,
            review.ReviewConfirmation(cue_ids=tuple(c.cue_id for c in newer.cues) + ("a0000",)),
        )
    assert confirm(store, video, newer).manifest.is_file()


def test_foreign_identity_changed_source_and_stale_basis_are_refused(
    tmp_path: Path,
) -> None:
    store, snap, head, video = prepared(tmp_path)
    other_video = make_video(tmp_path / "other", revision_body=b"another-render")
    with pytest.raises(review.IdentityMismatch):
        confirm(store, other_video, head)  # 다른 렌더의 머리 리비전이다
    video.verified_copy_path.write_bytes(b"tampered")
    with pytest.raises(FinalVideoChanged):
        confirm(store, video, head)  # 소스가 바뀌었다 — 구조 경색
    video.verified_copy_path.write_bytes(b"synthetic-final-video")
    stale = replace(
        head,
        identity=replace(head.identity, auto_content_sha256="e" * 64),
    )
    with pytest.raises(review.StaleAutoBasis):
        confirm(store, video, stale)  # 낡은 자동 근거
    tampered = json.loads(snap.body)
    tampered["content"]["status"] = "ready-hacked"
    snap.path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(CaptionStoreIntegrityError):
        confirm(store, video, head)  # 스냅샷이 손상됐다 — 내용 해시가 어긋난다
    missing = replace(head.identity, run_id="run-404")
    with pytest.raises(review.AutoBasisMissing):
        review.confirm_final(
            store,
            video,
            replace(head, identity=missing),
            review.ReviewConfirmation(cue_ids=()),
        )


def test_unprocessed_audio_blocks_final_even_with_allowances(tmp_path: Path) -> None:
    timing = FakeTiming(
        {
            0: [{"text": f" {WRONG}", "start": 0.5, "end": 2.5}],
            1: [{"text": " 두번째 조각", "start": 1.0, "end": 3.0}],
            2: [{"text": "세번째 꼬리", "start": 0.5, "end": 2.0}],
        },
        fail={1},
    )
    store, snap, head, video = prepared(tmp_path / "w", timing=timing)
    allowed = head
    for warning in review.review_state(snap, head).warnings:
        allowed = allow_finding(allowed, warning.key, "봤다")
    review.save_revision(store, allowed)
    with pytest.raises(review.UnprocessedAudio):
        confirm(store, video, allowed)  # 듣지 못한 구간 — 허용이 못 연다


def test_text_failure_confirms_with_honest_manifest(tmp_path: Path) -> None:
    """문구 실패 초안 — 확정은 되되 성공으로 둔갑하지 않는다(§11.3·§11.11)."""
    _, text = default_fakes()
    text.fail = {1}
    store, snap, head, video = prepared(tmp_path / "w", text=text)
    allowed = head
    for warning in review.review_state(snap, head).warnings:
        allowed = allow_finding(allowed, warning.key, "문구 실패를 알고 확정한다")
    review.save_revision(store, allowed)
    assert any(chunk.text_ok is False for chunk in snap.chunks)
    exported = confirm(store, video, allowed)
    manifest = json.loads(exported.manifest.read_text(encoding="utf-8"))
    assert manifest["text_model_complete"] is False  # 최신 문구 성공이 아니다
    assert manifest["run_status"] == "partial_failure"
    assert manifest["allowances"]  # 허용 결정이 보고서에 남는다


def test_empty_run_needs_explicit_no_speech_decision(tmp_path: Path) -> None:
    """빈 성공 실행 — 무발화 확정 전에는 final이 없다(§11.11)."""
    timing = FakeTiming({0: [], 1: [], 2: []})
    text = FakeText({0: "", 1: "", 2: ""})
    store, snap, head, video = prepared(tmp_path / "w", timing=timing, text=text)
    with pytest.raises(review.EmptyRunUndecided):
        confirm(store, video, head)
    decided = record_no_speech(head, note="무음 구간을 직접 확인했다")
    review.save_revision(store, decided)
    exported = confirm(store, video, decided)
    manifest = json.loads(exported.manifest.read_text(encoding="utf-8"))
    assert manifest["no_speech"] is True
    assert manifest["cue_count"] == 0
    assert manifest["speech_basis"] == "unconfirmed_empty"


def test_empty_run_with_errors_is_not_a_no_speech_success(tmp_path: Path) -> None:
    timing = FakeTiming({0: [], 1: []}, fail={2})
    store, snap, head, video = prepared(tmp_path / "w", timing=timing)
    decided = record_no_speech(head)
    review.save_revision(store, decided)
    with pytest.raises((review.UnprocessedAudio, review.EmptyRunUndecided)):
        confirm(store, video, decided)  # 오류 섞인 0바이트 SRT 성공은 없다


def test_unplaced_and_warnings_need_explicit_resolution(tmp_path: Path) -> None:
    """미대응은 처리 기록, 경고는 정확 key 허용 — 없으면 final이 막힌다(§11.11)."""
    _, text = default_fakes()
    text.text_by_chunk[0] = f"{CORRECTED} 그리고 사진을 찍었어요 다음 장면"
    store, snap, head, video = prepared(tmp_path / "w", text=text)
    unplaced = review.review_state(snap, head).unplaced
    assert unplaced and unplaced[0].resolution is None
    with pytest.raises(review.UnresolvedUnplaced):
        confirm(store, video, head)
    placed = add_cue(head, ms(6000), ms(8000), "그리고 사진을 찍었어요", note="미대응 말")
    review.save_revision(store, placed)
    placed = resolve_unplaced(placed, 0, "그리고 사진을 찍었어요", placed_cue_id="m0001")
    review.save_revision(store, placed)
    assert confirm(store, video, placed).manifest.is_file()

    # 경로 갈래 — 같은 실행의 다른 연쇄는 못 쓰니 별도 저장소에서 잰다
    _, text_default = default_fakes()
    store2, snap2, head2, video2 = prepared(tmp_path / "w2")
    long_text = (
        "이 문장은 매우 길어서 한 줄에 담기 어렵고 읽기 속도 경고가 반드시 나오도록"
        " 충분히 길게 쓴 문장이다"
    )
    speedy = edit_text(head2, "a0000", long_text)
    review.save_revision(store2, speedy)
    with pytest.raises(review.UnallowedWarnings):
        confirm(store2, video2, speedy)
    warnings = review.review_state(snap2, speedy).warnings
    reading = next(warning for warning in warnings if warning.code == "reading_speed")
    allowed = allow_finding(speedy, reading.key, "의도한 속도다")
    review.save_revision(store2, allowed)
    assert confirm(store2, video2, allowed).manifest.is_file()
    state2 = review.review_state(snap2, allowed)
    assert any(item.code == "out_of_final_range" for item in state2.auto_findings) is False


def test_invalid_auto_time_blocks_until_manual_time_fix(tmp_path: Path) -> None:
    """영상보다 늦은 자동 큐 — 뒤집힌 시각은 수동 정정으로만 풀린다(§11.11)."""
    video = make_video(tmp_path, video_duration=4.0)  # 영상은 4초뿐이다
    plan = plan_caption_run(
        video, run_id="run-1", settings=FinalCaptionSettings(chunk_max_seconds=15.0)
    )
    timing = FakeTiming({0: [{"text": " 망원동", "start": 5.0, "end": 6.0}], 1: [], 2: []})
    text = FakeText({0: "망원동", 1: "", 2: ""})
    result = run_final_captions(
        plan, on=BEFORE, timing_provider=timing, text_provider=text, workdir=tmp_path / "work"
    )
    assert result.cues[0].start > result.cues[0].end  # 글자는 살았고 시각이 뒤집혔다
    store = review.open_review_store(tmp_path / "review")
    snap = review.freeze_automatic(store, result, plan)
    head = review.initial_revision(snap)
    review.save_revision(store, head)
    with pytest.raises(review.InvalidCueTimes):
        confirm(store, video, head)
    fixed = edit_time(head, "a0000", start=Fraction(2), end=Fraction(4))
    review.save_revision(store, fixed)
    exported = confirm(store, video, fixed)
    assert parse_srt(exported.srt.read_text(encoding="utf-8"))[0].start == 2.0


# ─────────────────────────────────────────────────────────────
#  리비전 격리·멱등 — 새 확정 자리, 옛 바이트 보존
# ─────────────────────────────────────────────────────────────


def test_second_confirm_keeps_old_final_bytes_stable(tmp_path: Path) -> None:
    store, snap, head, video = prepared(tmp_path)
    first = confirm(store, video, head)
    first_bytes = {path: path.read_bytes() for path in first.directory.iterdir() if path.is_file()}
    again = confirm(store, video, head)  # 같은 머리의 다시 확정은 멱등이다
    assert again.directory == first.directory
    edited = edit_text(head, "a0000", "두번째 확정 문구")
    review.save_revision(store, edited)
    second = confirm(store, video, edited)
    assert second.directory != first.directory
    for path, body in first_bytes.items():
        assert path.read_bytes() == body  # 옛 final은 바이트 그대로다
    with pytest.raises(review.StaleManualHead):
        confirm(store, video, head)  # 옛 머리로는 더 이상 확정 못 한다


def test_manual_changes_never_reapply_to_new_run_or_new_render(tmp_path: Path) -> None:
    """새 실행·새 렌더에는 수동 변경이 따라가지 않는다(A7-3 완료 기준)."""
    store, snap, head, video = prepared(tmp_path)
    edited = edit_text(head, "a0000", "옛 실행의 편집")
    review.save_revision(store, edited)
    _, plan2, result2, _, _ = drive(tmp_path / "v2", run_id="run-2")
    snap2 = review.freeze_automatic(store, result2, plan2)
    assert snap2.identity.run_id == "run-2"
    assert (
        store.manual_head(
            final_video_revision=snap2.identity.final_video_revision,
            run_id="run-2",
            fingerprint=snap2.identity.fingerprint,
            auto_content_sha256=snap2.identity.auto_content_sha256,
        )
        is None
    )  # 새 실행에는 수동 리비전이 하나도 없다
    video3 = make_video(tmp_path / "v3", revision_body=b"re-rendered-final")
    with pytest.raises(review.IdentityMismatch):
        confirm(store, video3, edited)  # 새 렌더에 옛 머리를 못 박는다


def test_same_identity_different_auto_content_is_refused(tmp_path: Path) -> None:
    """같은 신원에 다른 자동 내용 — 두 번째 동결이 거부된다(§11.1)."""
    _, plan, result, *_ = drive(tmp_path)
    store = review.open_review_store(tmp_path / "review")
    review.freeze_automatic(store, result, plan)
    _, text2 = default_fakes()
    text2.text_by_chunk[0] = f"{CORRECTED} 다른 문구"
    _, plan_b, result_b, *_ = drive(tmp_path / "again", text=text2)
    result_b = (
        replace(result_b, run_id=result.run_id) if result_b.run_id != result.run_id else result_b
    )
    with pytest.raises(CaptionStoreConflict):
        review.freeze_automatic(store, result_b, plan_b)


# ─────────────────────────────────────────────────────────────
#  검토 상태 — 비교 행·탐색 구간·파형 자료
# ─────────────────────────────────────────────────────────────


def test_review_state_exposes_comparison_unplaced_seek_and_playback(
    tmp_path: Path,
) -> None:
    _, text = default_fakes()
    text.text_by_chunk[0] = f"{CORRECTED} 그리고 사진을 찍었어요 다음 장면"
    store, snap, head, video = prepared(tmp_path / "w", text=text)
    edited = edit_text(head, "a0000", "사람이 고친 글자")
    state = review.review_state(snap, edited)
    first = state.rows[0].cue
    assert first.whisper_text != first.auto_text  # 원문과 채택 문구가 다르다(교정)
    assert first.text == "사람이 고친 글자" and first.auto_text == CORRECTED
    unplaced = state.unplaced[0].item
    assert unplaced.text == "그리고 사진을 찍었어요"
    assert unplaced.seek_start == Fraction(0) and unplaced.seek_end == Fraction(15)
    assert state.playback.sample_rate == PCM_SAMPLE_RATE
    assert state.playback.chunk_boundaries == (
        (Fraction(0), Fraction(15)),
        (Fraction(15), Fraction(30)),
        (Fraction(30), Fraction(40)),
    )
    assert state.playback.cue_spans[0] == (edited.cues[0].start, edited.cues[0].end)
    CaptionViewState(query="카페")  # 보기 상태는 상태 객체 밖이다
    assert not hasattr(state, "view_state")
