from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path

import pytest

from vlogsrt import cli
from vlogsrt.project.fcpxml import parse_project
from vlogsrt.project.review import (
    CandidateAnalysis,
    build_review_plan,
    generate_silence_candidates,
    render_approved_roughcut,
)
from vlogsrt.project.timebase import FrameInterval, parse_fcpx_time
from vlogsrt.project.vad.guard import InstanceGuard
from vlogsrt.project.workflow_protocol import ProjectRevision, WorkflowJournal, encode_message


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


def test_staged_cli_roundtrip_applies_only_the_reviewed_candidate(tmp_path: Path) -> None:
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
    roughcut = tmp_path / "rough.fcpxml"
    assert cli.main(["project", "roughcut", str(workspace), "-o", str(roughcut)]) == 0

    assert roughcut.is_file()
    assert WorkflowJournal.open(workspace / "workflow.jsonl").state == "roughcut_imported"
    root = ET.fromstring(roughcut.read_bytes())
    markers = root.findall("library/event/project/sequence/spine/asset-clip/marker")
    assert len(markers) == 1
    assert batch.candidates[0].candidate_id in markers[0].get("value", "")
