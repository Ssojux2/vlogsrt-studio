from __future__ import annotations

import hashlib
import wave
import xml.etree.ElementTree as ET
from dataclasses import replace
from fractions import Fraction
from pathlib import Path

import pytest

from vlogsrt import cli
from vlogsrt.project import commands as project_commands
from vlogsrt.project.fcpxml import parse_project
from vlogsrt.project.final_caption import (
    CaptionDelivery,
    CaptionGenerationError,
)
from vlogsrt.project.review import (
    CandidateAnalysis,
    build_review_plan,
    generate_silence_candidates,
    render_approved_roughcut,
)
from vlogsrt.project.timebase import (
    SUPPORTED_FRAME_DURATIONS,
    FrameInterval,
    format_fcpx_time,
    frame_ceil,
    parse_fcpx_time,
)
from vlogsrt.project.vad.guard import InstanceGuard
from vlogsrt.project.workflow_protocol import (
    CaptionResult,
    ProjectRevision,
    WorkflowJournal,
    encode_message,
)


def _project(tmp_path: Path) -> tuple[Path, object]:
    xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>
<fcpxml version="1.14">
  <resources>
    <format id="r1" name="FFVideoFormat1080p25" frameDuration="1/25s"/>
    <asset id="r2" name="clip.mov" uid="ASSET-1" start="0s" duration="12s" hasAudio="1" format="r1">
      <media-rep kind="original-media" src="file:///tmp/clip.mov"/>
    </asset>
  </resources>
  <library><event name="Event" uid="EVENT-1"><project name="My Vlog" uid="PROJECT-1">
    <sequence format="r1" duration="12s" tcStart="0s"><spine>
      <asset-clip ref="r2" name="clip" offset="0s" start="0s" duration="12s"/>
    </spine></sequence>
  </project></event></library>
</fcpxml>
"""
    path = tmp_path / "input.fcpxml"
    path.write_bytes(xml)
    return path, parse_project(path)


def _revision(document: object) -> ProjectRevision:
    return ProjectRevision(
        input_sha256=document.input_sha256,
        project_uid="PROJECT-1",
        project_name="My Vlog",
        frame_duration=document.frame_duration,
        duration=Fraction(12),
    )


def _guard(document: object, *, status: str = "success") -> InstanceGuard:
    clip = document.storyline[0]
    protected = (
        FrameInterval(0, 50),
        FrameInterval(100, 150),
        FrameInterval(200, 300),
    )
    return InstanceGuard(
        instance_id=clip.instance_id,
        source_id="source-1",
        status=status,
        protected_frames=protected if status == "success" else (FrameInterval(0, 300),),
        review=(),
    )


def _analysis(document: object, **changes: object) -> dict[str, CandidateAnalysis]:
    clip = document.storyline[0]
    values = {
        "transcript_status": "success",
        "vision_status": "success",
        "unique_speech_frames": (),
        "motion_continuity_frames": (),
        "confidence": "0.95",
    }
    values.update(changes)
    return {clip.instance_id: CandidateAnalysis(**values)}


def test_internal_silence_keeps_breath_handles_and_snaps_inward_to_frames(tmp_path: Path) -> None:
    """호흡 여유를 바깥으로 반올림해 0.3초보다 덜 남기는 회귀를 잡는다."""
    _path, document = _project(tmp_path)
    revision = _revision(document)

    batch = generate_silence_candidates(
        document,
        revision,
        (_guard(document),),
        locked_instance_ids=frozenset(),
        analysis=_analysis(document),
    )

    assert len(batch.candidates) == 2
    assert batch.suppressed == ()
    assert [candidate.source_range for candidate in batch.candidates] == [
        # raw 2~4초, 6~8초에서 양쪽 0.3초보다 적게 남기지 않는다.
        candidate_range(Fraction(58, 25), Fraction(92, 25)),
        candidate_range(Fraction(158, 25), Fraction(192, 25)),
    ]
    assert [candidate.timeline_range for candidate in batch.candidates] == [
        candidate_range(Fraction(58, 25), Fraction(92, 25)),
        candidate_range(Fraction(158, 25), Fraction(192, 25)),
    ]
    assert all(candidate.reason == "internal_silence" for candidate in batch.candidates)
    assert all(candidate.selected_by_default for candidate in batch.candidates)


def candidate_range(start: Fraction, end: Fraction):
    from vlogsrt.project.workflow_protocol import RationalRange

    return RationalRange(start, end)


def test_unique_speech_and_motion_only_cancel_overlapping_candidates(tmp_path: Path) -> None:
    """비전이 삭제를 새로 만들거나, 한 구간의 취소가 다른 무음까지 지우는 버그를 잡는다."""
    _path, document = _project(tmp_path)
    revision = _revision(document)
    speech = (FrameInterval(70, 71),)
    motion = (FrameInterval(170, 171),)

    batch = generate_silence_candidates(
        document,
        revision,
        (_guard(document),),
        locked_instance_ids=frozenset(),
        analysis=_analysis(
            document,
            unique_speech_frames=speech,
            motion_continuity_frames=motion,
        ),
    )

    assert batch.candidates == ()
    assert [row.reason for row in batch.suppressed] == ["unique_speech", "motion_continuity"]


@pytest.mark.parametrize("locked,status", [(True, "success"), (False, "keep_all")])
def test_locked_or_failed_instances_never_emit_candidates(
    tmp_path: Path, locked: bool, status: str
) -> None:
    """분석 실패나 잠금이 빈 보호처럼 해석돼 삭제 권한을 만드는 회귀를 잡는다."""
    _path, document = _project(tmp_path)
    clip = document.storyline[0]
    batch = generate_silence_candidates(
        document,
        _revision(document),
        (_guard(document, status=status),),
        locked_instance_ids=frozenset({clip.instance_id}) if locked else frozenset(),
        analysis=_analysis(document),
    )
    assert batch.candidates == ()


@pytest.mark.parametrize("field", ["transcript_status", "vision_status"])
def test_incomplete_candidate_analysis_locks_the_instance(tmp_path: Path, field: str) -> None:
    """빈 모델 응답과 분석 오류가 '아무것도 없음'으로 통과하는 회귀를 잡는다."""
    _path, document = _project(tmp_path)
    batch = generate_silence_candidates(
        document,
        _revision(document),
        (_guard(document),),
        locked_instance_ids=frozenset(),
        analysis=_analysis(document, **{field: "error"}),
    )
    assert batch.candidates == ()
    assert batch.suppressed[0].reason == f"{field}_error"


def test_only_explicitly_approved_candidate_changes_the_new_fcpxml(tmp_path: Path) -> None:
    """기본 선택 또는 미검토 후보가 승인 없이 출력 XML에 반영되는 회귀를 잡는다."""
    input_path, document = _project(tmp_path)
    original = input_path.read_bytes()
    revision = _revision(document)
    batch = generate_silence_candidates(
        document,
        revision,
        (_guard(document),),
        locked_instance_ids=frozenset(),
        analysis=_analysis(document),
    )
    approved = batch.candidates[0]
    rejected = batch.candidates[1]
    plan = build_review_plan(
        revision,
        batch.candidates,
        approved_candidate_ids=(approved.candidate_id,),
    )

    rendered = render_approved_roughcut(document, revision, batch.candidates, plan)
    root = ET.fromstring(rendered.xml_bytes)
    project = root.find("library/event/project")
    sequence = root.find("library/event/project/sequence")
    clips = root.findall("library/event/project/sequence/spine/asset-clip")
    markers = root.findall("library/event/project/sequence/spine/asset-clip/marker")

    assert project is not None
    assert project.get("name") == f"AI Rough Cut · My Vlog · {plan.plan_revision[:12]}"
    assert "uid" not in project.attrib
    assert sequence is not None
    assert parse_fcpx_time(sequence.get("duration", "")) == Fraction(266, 25)
    assert len(clips) == 2
    assert len(markers) == 1
    assert approved.candidate_id in markers[0].get("value", "")
    assert rejected.candidate_id not in rendered.xml_bytes.decode("utf-8")
    assert input_path.read_bytes() == original
    assert rendered.removed_candidate_ids == (approved.candidate_id,)


def test_review_plan_rejects_unknown_and_stale_candidate_ids(tmp_path: Path) -> None:
    """다른 XML 분석의 후보 ID나 존재하지 않는 승인이 현재 계획에 붙는 버그를 잡는다."""
    _path, document = _project(tmp_path)
    revision = _revision(document)
    batch = generate_silence_candidates(
        document,
        revision,
        (_guard(document),),
        locked_instance_ids=frozenset(),
        analysis=_analysis(document),
    )
    with pytest.raises(ValueError, match="후보 집합에 없다"):
        build_review_plan(revision, batch.candidates, approved_candidate_ids=("unknown",))

    stale = batch.candidates[0]
    wrong_revision = ProjectRevision(
        input_sha256=hashlib.sha256(b"other").hexdigest(),
        project_uid=revision.project_uid,
        project_name=revision.project_name,
        frame_duration=revision.frame_duration,
        duration=revision.duration,
    )
    with pytest.raises(ValueError, match="리비전"):
        build_review_plan(wrong_revision, (stale,), approved_candidate_ids=())

    moved = replace(
        stale,
        source_range=candidate_range(stale.source_range.start + 2, stale.source_range.end + 2),
        timeline_range=candidate_range(
            stale.timeline_range.start + 2, stale.timeline_range.end + 2
        ),
    )
    with pytest.raises(ValueError, match="후보 ID"):
        build_review_plan(revision, (moved,), approved_candidate_ids=(moved.candidate_id,))


def test_staged_cli_roundtrip_applies_only_the_reviewed_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """receive→analyze→review→roughcut의 파일 계약과 사람 게이트를 끝에서 끝까지 지킨다."""
    input_path, document = _project(tmp_path)
    workspace = tmp_path / "workspace"
    assert cli.main(["project", "receive", str(input_path), "--workspace", str(workspace)]) == 0
    journal = WorkflowJournal.open(workspace / "workflow.jsonl")
    batch = generate_silence_candidates(
        document,
        journal.revision,
        (_guard(document),),
        locked_instance_ids=frozenset(),
        analysis=_analysis(document),
    )
    candidates_path = tmp_path / "candidates.jsonl"
    candidates_path.write_text(
        "".join(encode_message(candidate) for candidate in batch.candidates), encoding="utf-8"
    )

    assert (
        cli.main(
            [
                "project",
                "analyze",
                str(workspace),
                "--candidates",
                str(candidates_path),
            ]
        )
        == 0
    )
    assert (
        cli.main(
            [
                "project",
                "review",
                str(workspace),
                "--approve",
                batch.candidates[0].candidate_id,
            ]
        )
        == 0
    )
    original = input_path.read_bytes()
    assert cli.main(["project", "roughcut", str(workspace), "-o", str(input_path)]) == 2
    assert input_path.read_bytes() == original
    assert WorkflowJournal.open(workspace / "workflow.jsonl").state == "review_required"

    roughcut = tmp_path / "rough.fcpxml"
    assert cli.main(["project", "roughcut", str(workspace), "-o", str(roughcut)]) == 0

    assert roughcut.is_file()
    assert WorkflowJournal.open(workspace / "workflow.jsonl").state == "roughcut_imported"
    root = ET.fromstring(roughcut.read_bytes())
    markers = root.findall("library/event/project/sequence/spine/asset-clip/marker")
    assert len(markers) == 1
    assert batch.candidates[0].candidate_id in markers[0].get("value", "")

    audio = tmp_path / "final.wav"
    with wave.open(str(audio), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(48_000)
        stream.writeframes(b"\0\0" * 48_000 * 12)

    def caption_stub(request, config, destination, *, online, confirmed_cost):
        if not confirmed_cost:
            raise CaptionGenerationError("예상 비용 확인 필요")
        report = destination / "review.json"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text("{}\n", encoding="utf-8")
        result = CaptionResult(
            final_revision=request.final_revision,
            srt_path=None,
            warnings=("발화 없음",),
            quality=("coverage:complete",),
            speech_status="no_speech",
        )
        return CaptionDelivery(result=result, review_report=report, estimated_cost=0.1)

    monkeypatch.setattr(project_commands, "generate_final_captions", caption_stub)
    caption_args = [
        "project",
        "caption",
        str(workspace),
        str(audio),
        str(roughcut),
        "-o",
        str(tmp_path / "captions"),
        "--online",
    ]
    assert cli.main(caption_args) == 2
    assert WorkflowJournal.open(workspace / "workflow.jsonl").state == "roughcut_imported"
    assert cli.main([*caption_args, "--yes"]) == 0
    assert WorkflowJournal.open(workspace / "workflow.jsonl").state == "caption_ready"


def test_noop_render_preserves_sparse_leading_timeline_space(tmp_path: Path) -> None:
    """명시 gap 없이 offset으로 표현된 앞 2초가 no-op 러프컷에서 사라지는 회귀를 잡는다."""
    path = tmp_path / "sparse.fcpxml"
    path.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n'
        '<fcpxml version="1.14"><resources>'
        '<format id="r1" name="rate" frameDuration="1/25s"/>'
        '<asset id="r2" name="clip" uid="A" start="0s" duration="10s" '
        'hasAudio="1" format="r1"><media-rep kind="original-media" '
        'src="file:///tmp/clip.mov"/></asset></resources>'
        '<library><event name="E"><project name="Sparse" uid="P">'
        '<sequence format="r1" duration="12s" tcStart="0s"><spine>'
        '<asset-clip ref="r2" offset="2s" start="0s" duration="10s"/>'
        "</spine></sequence></project></event></library></fcpxml>\n",
        encoding="utf-8",
    )
    document = parse_project(path)
    revision = ProjectRevision(
        input_sha256=document.input_sha256,
        project_uid="P",
        project_name="Sparse",
        frame_duration=Fraction(1, 25),
        duration=Fraction(12),
    )
    plan = build_review_plan(revision, (), approved_candidate_ids=())
    rendered = render_approved_roughcut(document, revision, (), plan)
    root = ET.fromstring(rendered.xml_bytes)
    clip = root.find("library/event/project/sequence/spine/asset-clip")
    sequence = root.find("library/event/project/sequence")
    assert clip is not None and clip.get("offset") == "2s"
    assert sequence is not None and sequence.get("duration") == "12s"


def test_split_clip_preserves_and_clamps_timed_and_untimed_metadata(tmp_path: Path) -> None:
    """컷을 가로지르는 keyword와 clip metadata가 뒤 조각에서 사라지는 회귀를 잡는다."""
    path = tmp_path / "metadata.fcpxml"
    path.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n'
        '<fcpxml version="1.14"><resources>'
        '<format id="r1" name="rate" frameDuration="1/25s"/>'
        '<asset id="r2" name="clip" uid="A" start="0s" duration="12s" '
        'hasAudio="1" format="r1"><media-rep kind="original-media" '
        'src="file:///tmp/clip.mov"/></asset></resources>'
        '<library><event name="E"><project name="Metadata" uid="P">'
        '<sequence format="r1" duration="12s" tcStart="0s"><spine>'
        '<asset-clip ref="r2" offset="0s" start="0s" duration="12s">'
        '<keyword start="1s" duration="10s" value="wide"/>'
        '<metadata><md key="com.example.note" value="keep"/></metadata>'
        "</asset-clip></spine></sequence></project></event></library></fcpxml>\n",
        encoding="utf-8",
    )
    document = parse_project(path)
    revision = ProjectRevision(
        input_sha256=document.input_sha256,
        project_uid="P",
        project_name="Metadata",
        frame_duration=Fraction(1, 25),
        duration=Fraction(12),
    )
    batch = generate_silence_candidates(
        document,
        revision,
        (_guard(document),),
        locked_instance_ids=frozenset(),
        analysis=_analysis(document),
    )
    plan = build_review_plan(
        revision,
        batch.candidates,
        approved_candidate_ids=(batch.candidates[0].candidate_id,),
    )
    rendered = render_approved_roughcut(document, revision, batch.candidates, plan)
    root = ET.fromstring(rendered.xml_bytes)
    clips = root.findall("library/event/project/sequence/spine/asset-clip")
    assert len(clips) == 2
    keywords = [clip.find("keyword") for clip in clips]
    assert all(keyword is not None for keyword in keywords)
    assert [(row.get("start"), row.get("duration")) for row in keywords if row is not None] == [
        ("1s", "33/25s"),
        ("92/25s", "183/25s"),
    ]
    assert all(clip.find("metadata/md") is not None for clip in clips)


@pytest.mark.parametrize("frame_duration", sorted(SUPPORTED_FRAME_DURATIONS))
def test_silence_candidates_and_ripple_stay_on_every_supported_frame_grid(
    tmp_path: Path, frame_duration: Fraction
) -> None:
    """지원 frame rate 하나가 float 환산으로 격자 밖 컷이나 틀린 출력 길이를 내는 회귀를 잡는다."""
    speech = frame_ceil(Fraction(1), frame_duration)
    silence = frame_ceil(Fraction(2), frame_duration)
    boundaries = (0, speech, speech + silence, 2 * speech + silence, 2 * (speech + silence))
    total_frames = boundaries[-1] + speech
    duration = total_frames * frame_duration
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n'
        '<fcpxml version="1.14"><resources>'
        f'<format id="r1" name="rate" frameDuration="{format_fcpx_time(frame_duration)}"/>'
        f'<asset id="r2" name="clip" uid="A" start="0s" '
        f'duration="{format_fcpx_time(duration)}" hasAudio="1" format="r1">'
        '<media-rep kind="original-media" src="file:///tmp/clip.mov"/></asset>'
        '</resources><library><event name="E"><project name="P" uid="P1">'
        f'<sequence format="r1" duration="{format_fcpx_time(duration)}" tcStart="0s">'
        f'<spine><asset-clip ref="r2" offset="0s" start="0s" '
        f'duration="{format_fcpx_time(duration)}"/></spine></sequence>'
        "</project></event></library></fcpxml>\n"
    )
    path = tmp_path / f"rate-{frame_duration.numerator}-{frame_duration.denominator}.fcpxml"
    path.write_text(xml, encoding="utf-8")
    document = parse_project(path)
    clip = document.storyline[0]
    revision = ProjectRevision(
        input_sha256=document.input_sha256,
        project_uid="P1",
        project_name="P",
        frame_duration=frame_duration,
        duration=duration,
    )
    guard = InstanceGuard(
        instance_id=clip.instance_id,
        source_id="source",
        status="success",
        protected_frames=(
            FrameInterval(boundaries[0], boundaries[1]),
            FrameInterval(boundaries[2], boundaries[3]),
            FrameInterval(boundaries[4], total_frames),
        ),
        review=(),
    )
    batch = generate_silence_candidates(
        document,
        revision,
        (guard,),
        locked_instance_ids=frozenset(),
        analysis=_analysis(document),
    )
    assert len(batch.candidates) == 2
    for candidate in batch.candidates:
        assert (candidate.source_range.start / frame_duration).denominator == 1
        assert (candidate.source_range.end / frame_duration).denominator == 1
        assert (candidate.timeline_range.start / frame_duration).denominator == 1
        assert (candidate.timeline_range.end / frame_duration).denominator == 1
    plan = build_review_plan(
        revision,
        batch.candidates,
        approved_candidate_ids=tuple(row.candidate_id for row in batch.candidates),
    )
    rendered = render_approved_roughcut(document, revision, batch.candidates, plan)
    root = ET.fromstring(rendered.xml_bytes)
    sequence = root.find("library/event/project/sequence")
    assert sequence is not None
    assert parse_fcpx_time(sequence.get("duration", "")) == plan.expected_output_duration
