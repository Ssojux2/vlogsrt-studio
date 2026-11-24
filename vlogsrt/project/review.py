"""사람 승인 전용의 보수적 컷 후보와 새 러프컷 FCPXML.

기존 ``project.edit``는 프로토타입의 6~10초 블록/3초 유지 정책을 보존한다.
이 모듈은 최종 앱 흐름이다: 성공한 분석 안의 내부 무음만 후보로 만들고,
전사 고유 발화나 화면 연속 동작은 후보를 취소하며, 승인 ID만 새 문서에 적용한다.
"""

from __future__ import annotations

import copy
import hashlib
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction

from vlogsrt.project.fcpxml import element_tree
from vlogsrt.project.models import ClipInstance, GapInstance, ProjectDocument
from vlogsrt.project.timebase import (
    format_fcpx_time,
    parse_fcpx_time,
)
from vlogsrt.project.workflow_protocol import (
    CutCandidate,
    EditPlan,
    ProjectRevision,
    RationalRange,
)

_XML_PROLOGUE = '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n'
_CUT_MARKER_PREFIX = "VLOG_AI_CUT:"


@dataclass(frozen=True, slots=True)
class RenderedRoughCut:
    xml_bytes: bytes
    project_name: str
    removed_candidate_ids: tuple[str, ...]


def build_review_plan(
    revision: ProjectRevision,
    candidates: Sequence[CutCandidate],
    *,
    approved_candidate_ids: Sequence[str],
) -> EditPlan:
    """후보 전부를 승인/거절로 닫고 승인 구간 길이만 출력 길이에서 뺀다."""
    by_id: dict[str, CutCandidate] = {}
    for candidate in candidates:
        _require_candidate_identity(candidate)
        if candidate.input_revision != revision.revision:
            raise ValueError(
                f"후보의 입력 리비전이 현재 프로젝트 리비전과 다르다: "
                f"{candidate.input_revision} ≠ {revision.revision}"
            )
        if candidate.candidate_id in by_id:
            raise ValueError(f"후보 ID가 중복이다: {candidate.candidate_id}")
        by_id[candidate.candidate_id] = candidate
    approved = tuple(sorted(set(approved_candidate_ids)))
    unknown = sorted(set(approved) - set(by_id))
    if unknown:
        raise ValueError(f"승인 ID가 후보 집합에 없다: {unknown}")
    chosen = [by_id[candidate_id] for candidate_id in approved]
    _require_nonoverlapping(chosen)
    removed = sum(
        (candidate.timeline_range.end - candidate.timeline_range.start for candidate in chosen),
        start=Fraction(),
    )
    return EditPlan.create(
        input_revision=revision.revision,
        approved_candidate_ids=approved,
        approved_decision_revisions=tuple(
            (candidate_id, by_id[candidate_id].decision_revision) for candidate_id in approved
        ),
        rejected_candidate_ids=tuple(sorted(set(by_id) - set(approved))),
        expected_output_duration=revision.duration - removed,
    )


def render_approved_roughcut(
    document: ProjectDocument,
    revision: ProjectRevision,
    candidates: Sequence[CutCandidate],
    plan: EditPlan,
) -> RenderedRoughCut:
    """승인된 후보만 리플 제거한 새 FCPXML을 메모리에서 만든다."""
    _require_revision(document, revision)
    if plan.input_revision != revision.revision:
        raise ValueError("계획의 입력 리비전이 현재 프로젝트 리비전과 다르다")
    candidates_by_id = {candidate.candidate_id: candidate for candidate in candidates}
    missing = sorted(set(plan.approved_candidate_ids) - set(candidates_by_id))
    if missing:
        raise ValueError(f"승인 ID가 제공된 후보 집합에 없다: {missing}")
    approved = [candidates_by_id[candidate_id] for candidate_id in plan.approved_candidate_ids]
    expected_decisions = dict(plan.approved_decision_revisions)
    for candidate in approved:
        if expected_decisions.get(candidate.candidate_id) != candidate.decision_revision:
            raise ValueError(f"후보의 판정 리비전이 승인 계획과 다르다: {candidate.candidate_id}")
    if any(candidate.input_revision != revision.revision for candidate in approved):
        raise ValueError("승인 후보에 다른 입력 리비전이 섞였다")
    for candidate in approved:
        _require_candidate_identity(candidate)
        _require_candidate_geometry(document, candidate)
    _require_nonoverlapping(approved)
    root = element_tree(document.raw_xml, str(document.info_path))
    project = root.find("library/event/project")
    sequence = root.find("library/event/project/sequence")
    spine = root.find("library/event/project/sequence/spine")
    if project is None or sequence is None or spine is None:
        raise ValueError("문서의 library/event/project/sequence/spine 사슬이 없다")
    original_children = list(spine)
    if len(original_children) != len(document.storyline):
        raise ValueError("파싱된 스토리라인과 원본 XML 자식 수가 다르다")
    for child in original_children:
        spine.remove(child)
    by_instance: dict[str, list[CutCandidate]] = {}
    for candidate in approved:
        by_instance.setdefault(candidate.clip_instance_id, []).append(candidate)
    removed_before = Fraction()
    resources = {resource.scoped_id: resource for resource in document.resources}
    for item, source in zip(document.storyline, original_children, strict=True):
        if isinstance(item, GapInstance):
            element = copy.deepcopy(source)
            element.set("offset", format_fcpx_time(item.offset - removed_before))
            spine.append(element)
            continue
        cuts = sorted(by_instance.get(item.instance_id, ()), key=lambda row: row.source_range.start)
        pieces = _retained_pieces(item, cuts)
        piece_offset = item.offset - removed_before
        for index, (start, end, preceding_cut) in enumerate(pieces):
            if end <= start:
                continue
            element = copy.deepcopy(source)
            element.set("offset", format_fcpx_time(piece_offset))
            element.set("start", format_fcpx_time(start))
            element.set("duration", format_fcpx_time(end - start))
            _preserve_children(element, source, start, end, first=index == 0)
            if preceding_cut is not None:
                _insert_cut_marker(
                    element,
                    start,
                    document.frame_duration,
                    preceding_cut,
                )
            spine.append(element)
            piece_offset += end - start
        removed_before += sum(
            (cut.source_range.end - cut.source_range.start for cut in cuts), start=Fraction()
        )
        resource = resources[item.resource_scoped_id]
        resource_end = resource.start + resource.duration
        if not resource.start <= item.start < item.start + item.duration <= resource_end:
            raise ValueError(f"클립이 애셋 범위를 벗어난다: {item.element_path}")
    output_duration = revision.duration - removed_before
    if output_duration != plan.expected_output_duration:
        raise ValueError(
            f"렌더 길이가 승인 계획과 다르다: {output_duration} ≠ {plan.expected_output_duration}"
        )
    project_name = roughcut_project_name(revision.project_name, plan.plan_revision)
    project.set("name", project_name)
    project.attrib.pop("uid", None)
    sequence.set("duration", format_fcpx_time(output_duration))
    ET.indent(root, space="  ")
    xml_bytes = f"{_XML_PROLOGUE}{ET.tostring(root, encoding='unicode')}\n".encode()
    return RenderedRoughCut(xml_bytes, project_name, tuple(plan.approved_candidate_ids))


def roughcut_project_name(original_name: str, plan_revision: str) -> str:
    return f"AI Rough Cut · {original_name} · {plan_revision[:12]}"


def _require_revision(document: ProjectDocument, revision: ProjectRevision) -> None:
    if document.input_sha256 != revision.input_sha256:
        raise ValueError("ProjectRevision의 입력 XML 해시가 문서와 다르다")
    if document.frame_duration != revision.frame_duration:
        raise ValueError("ProjectRevision의 프레임 길이가 문서와 다르다")


def _require_nonoverlapping(candidates: Sequence[CutCandidate]) -> None:
    ordered = sorted(candidates, key=lambda row: (row.timeline_range.start, row.timeline_range.end))
    for left, right in zip(ordered, ordered[1:], strict=False):
        if right.timeline_range.start < left.timeline_range.end:
            raise ValueError(
                f"승인 후보의 타임라인 구간이 겹친다: {left.candidate_id}, {right.candidate_id}"
            )


def _retained_pieces(
    clip: ClipInstance, cuts: Sequence[CutCandidate]
) -> tuple[tuple[Fraction, Fraction, CutCandidate | None], ...]:
    cursor = clip.start
    pieces: list[tuple[Fraction, Fraction, CutCandidate | None]] = []
    preceding: CutCandidate | None = None
    for cut in cuts:
        if cut.source_range.start < cursor or cut.source_range.end > clip.start + clip.duration:
            raise ValueError(f"승인 후보가 클립 범위를 벗어난다: {cut.candidate_id}")
        pieces.append((cursor, cut.source_range.start, preceding))
        cursor = cut.source_range.end
        preceding = cut
    pieces.append((cursor, clip.start + clip.duration, preceding))
    return tuple(pieces)


def _preserve_children(
    target: ET.Element,
    source: ET.Element,
    start: Fraction,
    end: Fraction,
    *,
    first: bool,
) -> None:
    for child in list(target):
        target.remove(child)
    timed = {
        "marker",
        "chapter-marker",
        "analysis-marker",
        "hidden-clip-marker",
        "keyword",
        "rating",
    }
    for child in source:
        child_start = child.get("start")
        if child.tag in timed and child_start is not None:
            try:
                position = parse_fcpx_time(child_start)
            except ValueError:
                continue
            duration_text = child.get("duration")
            if duration_text is None:
                if start <= position < end:
                    target.append(copy.deepcopy(child))
                continue
            try:
                child_end = position + parse_fcpx_time(duration_text)
            except ValueError:
                continue
            overlap_start = max(position, start)
            overlap_end = min(child_end, end)
            if overlap_start < overlap_end:
                copied = copy.deepcopy(child)
                copied.set("start", format_fcpx_time(overlap_start))
                copied.set("duration", format_fcpx_time(overlap_end - overlap_start))
                target.append(copied)
        else:
            target.append(copy.deepcopy(child))


def _candidate_id(
    input_revision: str,
    clip_instance_id: str,
    source_range: RationalRange,
    timeline_range: RationalRange,
    reason: str,
    evidence: tuple[str, ...],
    window_id: str,
) -> str:
    return CutCandidate.identity(
        input_revision=input_revision,
        clip_instance_id=clip_instance_id,
        source_range=source_range,
        timeline_range=timeline_range,
        reason=reason,
        evidence=evidence,
        window_id=window_id,
    )


def _require_candidate_identity(candidate: CutCandidate) -> None:
    expected = _candidate_id(
        candidate.input_revision,
        candidate.clip_instance_id,
        candidate.source_range,
        candidate.timeline_range,
        candidate.reason,
        candidate.evidence,
        candidate.window_id,
    )
    if candidate.candidate_id != expected:
        raise ValueError(f"후보 ID가 후보 내용과 다르다: {candidate.candidate_id} ≠ {expected}")


def _window_id(input_revision: str, clip_instance_id: str, timeline_range: RationalRange) -> str:
    material = "\0".join(
        (
            "vlogsrt-workflow-window-v1",
            input_revision,
            clip_instance_id,
            str(timeline_range.start),
            str(timeline_range.end),
        )
    )
    return hashlib.sha256(material.encode()).hexdigest()


def _require_candidate_geometry(document: ProjectDocument, candidate: CutCandidate) -> None:
    clips = {
        item.instance_id: item for item in document.storyline if isinstance(item, ClipInstance)
    }
    clip = clips.get(candidate.clip_instance_id)
    if clip is None:
        raise ValueError(f"후보의 클립 인스턴스가 문서에 없다: {candidate.clip_instance_id}")
    if (
        not clip.start
        < candidate.source_range.start
        < candidate.source_range.end
        < (clip.start + clip.duration)
    ):
        raise ValueError(f"후보 소스 구간이 클립 내부가 아니다: {candidate.candidate_id}")
    expected_start = clip.offset + candidate.source_range.start - clip.start
    expected_end = clip.offset + candidate.source_range.end - clip.start
    if candidate.timeline_range != RationalRange(expected_start, expected_end):
        raise ValueError(f"후보의 소스·타임라인 매핑이 어긋난다: {candidate.candidate_id}")
    fd = document.frame_duration
    endpoints = (
        candidate.source_range.start,
        candidate.source_range.end,
        candidate.timeline_range.start - document.sequence_tc_start,
        candidate.timeline_range.end - document.sequence_tc_start,
    )
    if any((value / fd).denominator != 1 for value in endpoints):
        raise ValueError(f"후보 끝점이 프레임 격자 밖이다: {candidate.candidate_id}")


def _insert_cut_marker(
    element: ET.Element,
    start: Fraction,
    frame_duration: Fraction,
    candidate: CutCandidate,
) -> None:
    marker = ET.Element(
        "marker",
        {
            "start": format_fcpx_time(start),
            "duration": format_fcpx_time(frame_duration),
            "value": f"{_CUT_MARKER_PREFIX}{candidate.candidate_id}:{candidate.reason}",
        },
    )
    metadata_index = next(
        (index for index, child in enumerate(element) if child.tag == "metadata"), len(element)
    )
    element.insert(metadata_index, marker)
