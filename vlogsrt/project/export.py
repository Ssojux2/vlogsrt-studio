"""P6-04 — 원자적 연계 패키지 내보내기. FCPXML·EDIT SRT·SOURCE SRT·review·manifest.

**다섯 파일은 하나의 계획 리비전으로만 나온다.** `publish_package`가 불변 리비전
디렉터리에 다섯을 함께 쓰고, 검증한 뒤 `current` 심링크의 원자적 교체로 공개한다.
독자는 포인터를 지나 언제나 한 리비전의 완전한 묶음만 본다. 실패는 포인터 교체
이전에 멈춘다 — 옛 리비전과 옛 포인터는 온전하다.

**신뢰 경계 — 프로세스 안에서 만든 플랜만 렌더한다.** `EditPlan`을 역직렬화하는
코드는 이 패키지에 없다. 재개는 저장된 분석에서 `build_edit_plan`으로 플랜을 다시
만들고 `revision`을 대조한다(명세: plan/export는 저장된 분석에서 결정적).
`validate_edit_plan`은 구조 관문이라 출처를 볼 수 없다 — 출처는 이 규칙이 지키고,
렌더는 관문 findings가 하나라도 있으면 거부한다.

**스토리라인은 조회만 한다 — 리플 산수를 다시 하지 않는다.** `asset-clip`은
`plan.runs`마다 하나다(`retained`마다가 아니라 — 제거 없는 블록 경계마다 쪼개져
through-edit을 낸다). 갭은 `plan.gaps`에서 그대로, 오프셋은 `m × frame_duration`,
`tcStart=0`이다. 낡은 XML 리소스 id(`a1`·`r2`)는 이 문서 안의 참조일 뿐이며,
미디어 재묶음은 문서 해시가 스코프인 `resource_scoped_id` → `ResolvedMedia`로만
한다 — 원본 경로만 링크하고 프록시를 원본으로 가장하지 않는다.

**마커는 생성된·위치 있는 ReviewIssue만.** 산문을 파싱해 시각을 되찾는 일은
없다. 소유자는 내보낸 run이고 반개 구간 `[start, end)`이다 — 칼 경계 프레임은
뒤 조각이 가진다. 마커 시계는 `asset.start + source_frame × frame_duration`
(애셋 시작 앵커)이며 잘린 클립 시작을 빼지 않는다. 자리가 지워진 이슈는 마커
없이 review에 남는다.

**`final_accepted`는 여기서만 계산한다.** SOURCE에서 접힌 큐는 EDIT에서
표현 가능해져도 EDIT 이슈를 남기지 않는다(P6-03b) — 그 차단 증거는
`source.unresolved_cue_ids`뿐이므로 반드시 본다. caption·edit 리뷰 코드는
허용 집합으로 판정한다 — `unavailable_declared_no_audio`(완결된 기록) 외의
코드는 예외 없이 블로커다. 판정이 없거나 오류인 블록도 리뷰 증거로 흘러
분석이 일어나지 않은 패키지가 승인으로 나가지 못한다. 스트림·PCM 부재는
재시도 가능한 실패라 막는다. **실패 닫힘(`keep_all`) 인스턴스 가드도 막는다**
(PROTO-6b) — 판정은 타입(`status`)으로만 하고 한국어 review 문장을 파싱하지
않는다. `inaudible`(선언된 무음)은 완결된 사실이라 막지 않는다. 가드의 이유
문장은 `analysis_failures`에 인스턴스·소스 가드 그대로 남는다 — 실패 이유가
기록 어디에도 없던 결함을 이 필드가 닫는다.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, replace
from fractions import Fraction
from pathlib import Path

from vlogsrt import oscompat
from vlogsrt.project import xml_validation
from vlogsrt.project.boundary import BoundaryProjection, UnavailableInstance
from vlogsrt.project.captions import SUBMS_COLLAPSED, EditCaptionSet
from vlogsrt.project.edit import (
    EditPlan,
    RetainedGap,
    RetainedRun,
    ReviewIssue,
    validate_edit_plan,
)
from vlogsrt.project.fcpxml import element_tree, parse_project
from vlogsrt.project.media import ResolvedMedia
from vlogsrt.project.models import AssetResource, ClipInstance, ProjectDocument
from vlogsrt.project.timebase import FrameInterval, format_fcpx_time
from vlogsrt.project.vad.guard import InstanceGuard, SourceGuard

FCPXML_NAME = "VLOG_AI_EDIT.fcpxml"
EDIT_SRT_NAME = "VLOG_AI_EDIT.srt"
SOURCE_SRT_NAME = "VLOG_AI_SOURCE.srt"
TRACE_NAME = "project_trace.json"
REVIEW_NAME = "review.json"

_EXPORT_PROJECT_NAME = "VLOG_AI_EDIT"
"""새 문서의 event/project 이름 — 입력과 다른 프로젝트로 가져오게 한다."""

_MARKER_VALUE_PREFIX = "VLOG_AI_REVIEW:"
_TRACE_KIND = "vlogsrt-project-trace-v1"
_REVIEW_KIND = "vlogsrt-review-v1"

_REVISIONS_DIR = "revisions"
_CURRENT = "current"
_LOCK = ".vlogsrt-publish.lock"
_STAGING_PREFIX = ".current."
_STAGING_SUFFIX = ".tmp"

_RETRYABLE_UNAVAILABLE_REASONS = frozenset({"stream_unavailable", "pcm_unavailable"})
"""재시도 가능한 부재 — 준비 블로커다. 선언된 무음은 완결된 기록이라 빠진다."""

_FINAL_REVIEW_CODES_ALLOWED = frozenset({"unavailable_declared_no_audio"})
"""최종 수용과 양립하는 리뷰 코드 — 선언된 무음 부재 하나뿐이다.

허용 집합으로 판정한다: 이 집합에 없는 caption·edit 리뷰 코드는 예외 없이
블로커다. 블로커 쪽을 열거하면 다음 리뷰 코드가 생길 때마다 이 관문이
묵시적으로 뚫린다 — 판정 없음·오류가 리뷰에 남지 않아 분석이 일어나지 않은
패키지가 승인으로 나가던 결함(P6-04b)이 그 형태였다. 양립해야 하는 새 코드는
여기에 하나씩 더하고 그 근거를 문장으로 남긴다.
"""

_MAX_REFUSAL_LINES = 4
"""거부 문장에 실을 findings·진단의 상한 — 로그 위생 상수다(P1-04와 같은 규율)."""

_XML_PROLOGUE = '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n'

_CLIP_STATIC_ATTRIBUTES = ("name", "enabled", "srcEnable", "format")
_GAP_STATIC_ATTRIBUTES = ("name",)
"""이름 붙은 정적 속성만 원본 문자열 그대로 옮긴다 — 시각·참조는 계획에서 만든다."""


@dataclass(frozen=True, slots=True)
class ExportPackage:
    """한 번의 게시가 내놓은 다섯 파일 — 경로는 모두 선택된 불변 리비전 안이다."""

    plan_revision: str
    fcpxml: Path
    edit_srt: Path
    source_srt: Path
    project_trace: Path
    review: Path


# ═════════════════════════════════════════════════════════════
#  공개 기록 인코더 — P7-01이 읽는 유일한 형태
# ═════════════════════════════════════════════════════════════


def plan_record(plan: EditPlan) -> dict:
    """`EditPlan`의 정규 기록 — `Fraction`은 "num/den", `FrameInterval`은 [start, end]."""
    return {
        "revision": plan.revision,
        "input_sha256": plan.input_sha256,
        "config_sha256": plan.config_sha256,
        "frame_duration": _fraction(plan.frame_duration),
        "locked_instance_ids": sorted(plan.locked_instance_ids),
        "retained": [_retained_record(row) for row in plan.retained],
        "runs": [_run_record(run) for run in plan.runs],
        "gaps": [
            {
                "instance_id": row.instance_id,
                "input_timeline_frames": _interval(row.input_timeline_frames),
                "output_timeline_frames": _interval(row.output_timeline_frames),
            }
            for row in plan.gaps
        ],
        "removed": [
            {
                "instance_id": row.instance_id,
                "source_frames": _interval(row.source_frames),
                "input_timeline_frames": _interval(row.input_timeline_frames),
                "restore_at_output_frame": row.restore_at_output_frame,
            }
            for row in plan.removed
        ],
        "decisions": [_decision_record(row) for row in plan.decisions],
        "unavailable_instances": [_unavailable_record(row) for row in plan.unavailable_instances],
        "review": [_issue_record(issue) for issue in plan.review],
    }


def _run_record(run: RetainedRun) -> dict:
    return {
        "instance_id": run.instance_id,
        "source_frames": _interval(run.source_frames),
        "input_timeline_frames": _interval(run.input_timeline_frames),
        "output_timeline_frames": _interval(run.output_timeline_frames),
        "reasons": list(run.reasons),
    }


def _retained_record(row: object) -> dict:
    """유지 조각의 기록 — run 인코더와 같은 열 이름(reasons는 단일 원소)."""
    return {
        "instance_id": row.instance_id,  # type: ignore[attr-defined]
        "source_frames": _interval(row.source_frames),  # type: ignore[attr-defined]
        "input_timeline_frames": _interval(row.input_timeline_frames),  # type: ignore[attr-defined]
        "output_timeline_frames": _interval(row.output_timeline_frames),  # type: ignore[attr-defined]
        "reasons": [row.reason],  # type: ignore[attr-defined]
    }


def _decision_record(row: object) -> dict:
    return {
        "block_id": row.block_id,  # type: ignore[attr-defined]
        "request_sha256": row.request_sha256,  # type: ignore[attr-defined]
        "choice": row.choice,  # type: ignore[attr-defined]
        "reason": row.reason,  # type: ignore[attr-defined]
        "evidence_frame_ids": list(row.evidence_frame_ids),  # type: ignore[attr-defined]
        "confidence": row.confidence,  # type: ignore[attr-defined]
        "status": row.status,  # type: ignore[attr-defined]
    }


def edit_captions_record(captions: EditCaptionSet) -> dict:
    """`EditCaptionSet`의 정규 기록 — 리플 뒤 출력 시각과 출처 큐를 그대로 싣는다."""
    return {
        "plan_revision": captions.plan_revision,
        "cues": [
            {
                "cue_id": cue.cue_id,
                "slice_id": cue.slice_id,
                "instance_ids": list(cue.instance_ids),
                "source_id": cue.source_id,
                "source_start": _fraction(cue.source_start),
                "source_end": _fraction(cue.source_end),
                "timeline_start": _fraction(cue.timeline_start),
                "timeline_end": _fraction(cue.timeline_end),
                "text": cue.text,
                "provenance": cue.provenance,
                "flags": list(cue.flags),
            }
            for cue in captions.cues
        ],
        "srt_text": captions.srt_text,
        "review": [_issue_record(issue) for issue in captions.review],
    }


def _issue_record(issue: ReviewIssue) -> dict:
    return {
        "code": issue.code,
        "message": issue.message,
        "instance_id": issue.instance_id,
        "source_frame": issue.source_frame,
    }


def _unavailable_record(row: UnavailableInstance) -> dict:
    return {
        "resource_scoped_id": row.resource_scoped_id,
        "instance_id": row.instance_id,
        "reason_code": row.reason_code,
    }


def _interval(window: FrameInterval) -> list[int]:
    """반개 구간의 정확한 직렬 [start, end]."""
    return [window.start_frame, window.end_frame]


def _fraction(value: Fraction) -> str:
    """유리수의 정확한 직렬 — 같은 값은 늘 같은 글자가 된다."""
    return f"{value.numerator}/{value.denominator}"


# ═════════════════════════════════════════════════════════════
#  렌더 — 계획을 새 FCPXML 문서로
# ═════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class _EmittedClip:
    """내보낸 asset-clip의 기하 — 재구문 해석 대조의 정본."""

    resource_xml_id: str
    offset: Fraction
    start: Fraction
    duration: Fraction
    enabled: bool
    src_enable: str
    markers: tuple[tuple[Fraction, str], ...]


@dataclass(frozen=True, slots=True)
class _EmittedGap:
    """내보낸 gap의 기하."""

    offset: Fraction
    duration: Fraction


def render_edit_fcpxml(
    document: ProjectDocument,
    media: Sequence[ResolvedMedia],
    plan: EditPlan,
    captions: EditCaptionSet,
    guards: Sequence[InstanceGuard],
) -> str:
    """계획을 새 FCPXML 문서의 글월로 만든다 — 관문 findings가 하나라도 있으면 거부."""
    _require_renderable(document, plan, captions, guards)
    text, _ = _edit_fcpxml_parts(document, media, plan)
    return text


def _require_renderable(
    document: ProjectDocument,
    plan: EditPlan,
    captions: EditCaptionSet,
    guards: Sequence[InstanceGuard],
) -> None:
    """렌더 관문 — 구조 검증 → 리비전 짝 → 부재·리뷰 짝. 순서가 계약이다."""
    findings = validate_edit_plan(plan, document, guards)
    if findings:
        shown = "; ".join(findings[:_MAX_REFUSAL_LINES])
        more = (
            f" … 외 {len(findings) - _MAX_REFUSAL_LINES}건"
            if len(findings) > _MAX_REFUSAL_LINES
            else ""
        )
        raise ValueError(f"렌더 거부 — 편집 계획 검증 findings {len(findings)}건: {shown}{more}")
    if captions.plan_revision != plan.revision:
        raise ValueError(
            f"자막의 계획 리비전이 어긋난다: {captions.plan_revision} ≠ {plan.revision}"
            " — 섞인 리비전이다"
        )
    _require_unavailable_review(plan.unavailable_instances, captions.review)


def _require_unavailable_review(
    records: Sequence[UnavailableInstance], review: Sequence[ReviewIssue]
) -> None:
    """부재 기록과 리뷰의 짝 — 생략·모순된 리뷰로 패키지를 내지 않는다.

    짝은 코드 다중집합으로 맞춘다(`unavailable_{reason}`). 리뷰 이슈는 위치가
    없어 기록의 인스턴스를 타입으로 싣지 못한다 — 산문(message)을 파싱해 짝을
    맞추는 일은 없다(P6-04 계약).
    """
    expected = Counter(f"unavailable_{row.reason_code}" for row in records)
    found = Counter(issue.code for issue in review if issue.code.startswith("unavailable_"))
    if expected != found:
        raise ValueError(
            f"부재 기록과 리뷰가 어긋난다: 기록 {dict(expected)} ≠ 리뷰 {dict(found)}"
            " — 생략·모순된 리뷰로 패키지를 낼 수 없다"
        )


def _edit_fcpxml_parts(
    document: ProjectDocument, media: Sequence[ResolvedMedia], plan: EditPlan
) -> tuple[str, tuple[_EmittedClip | _EmittedGap, ...]]:
    """트리 복사로 새 문서를 짓는다 — (글월, 내보낸 항목의 기하)."""
    media_by_resource = _media_index(document, media)
    root = element_tree(document.raw_xml, str(document.info_path))
    spine = root.find("library/event/project/sequence/spine")
    if spine is None or len(tuple(spine)) != len(document.storyline):
        raise ValueError("문서의 스토리라인이 원본 트리와 짝이 맞지 않다 — 파싱된 문서가 아니다")
    resources = {resource.scoped_id: resource for resource in document.resources}
    clips_by_instance = {
        clip.instance_id: clip for clip in document.storyline if isinstance(clip, ClipInstance)
    }
    emitted, runs = _rebuild_spine(
        spine, plan, document, resources, clips_by_instance, plan.frame_duration
    )
    _attach_markers(spine, emitted, runs, plan, resources, clips_by_instance)
    _rebind_original_media(root, document, media_by_resource)
    _renew_document_identity(root, _total_output_frames(plan) * plan.frame_duration)
    ET.indent(root, space="  ")
    return f"{_XML_PROLOGUE}{ET.tostring(root, encoding='unicode')}\n", tuple(emitted)


def _media_index(
    document: ProjectDocument, media: Sequence[ResolvedMedia]
) -> dict[str, ResolvedMedia]:
    """자원마다 미디어 하나 — 못 푼 원본은 곧 링크 못 할 애셋이다(전부 있어야 한다)."""
    known = {resource.scoped_id for resource in document.resources}
    indexed: dict[str, ResolvedMedia] = {}
    for row in media:
        if row.resource_scoped_id not in known:
            raise ValueError(
                f"문서에 없는 자원을 가진 미디어다: {row.resource_scoped_id!r} — 섞인 입력이다"
            )
        if row.resource_scoped_id in indexed:
            raise ValueError(f"미디어 자원이 중복이다: {row.resource_scoped_id!r}")
        indexed[row.resource_scoped_id] = row
    missing = sorted(known - set(indexed))
    if missing:
        raise ValueError(
            f"해석되지 않은 자원이 있다: {missing} — 원본 링크를 지어낼 수 없어 거부한다"
        )
    return indexed


def _rebuild_spine(
    spine: ET.Element,
    plan: EditPlan,
    document: ProjectDocument,
    resources: dict[str, AssetResource],
    clips_by_instance: dict[str, ClipInstance],
    fd: Fraction,
) -> tuple[list[_EmittedClip | _EmittedGap], list[tuple[RetainedRun, int]]]:
    """런·갭을 출력 순서로 잇는다 — 오프셋은 m × frame_duration, tcStart=0 세계."""
    tiles = sorted(
        [(run.output_timeline_frames.start_frame, run) for run in plan.runs]
        + [(gap.output_timeline_frames.start_frame, gap) for gap in plan.gaps],
        key=lambda tile: tile[0],
    )
    if not tiles:
        raise ValueError("빈 스토리라인은 내보낼 수 없다 — 0길이 시퀀스는 곧 무효 문서다")
    source_of = _storyline_sources(spine, document)
    for child in list(spine):
        spine.remove(child)
    emitted: list[_EmittedClip | _EmittedGap] = []
    runs: list[tuple[RetainedRun, int]] = []
    for _, tile in tiles:
        if isinstance(tile, RetainedRun):
            runs.append((tile, len(emitted)))
            emitted.append(_emit_run(spine, tile, source_of, resources, clips_by_instance, fd))
        else:
            emitted.append(_emit_gap(spine, tile, source_of, fd))
    return emitted, runs


def _storyline_sources(spine: ET.Element, document: ProjectDocument) -> dict[str, ET.Element]:
    """항목 → 원본 XML 원소 — 정적 속성을 그대로 옮기기 위한 위치 짝."""
    paired: dict[str, ET.Element] = {}
    for item, element in zip(document.storyline, spine, strict=False):
        key = item.element_path if isinstance(item, ClipInstance) else f"gap:{item.instance_id}"
        paired[key] = element
    return paired


def _emit_run(
    spine: ET.Element,
    run: RetainedRun,
    source_of: dict[str, ET.Element],
    resources: dict[str, AssetResource],
    clips_by_instance: dict[str, ClipInstance],
    fd: Fraction,
) -> _EmittedClip:
    """런 하나 = asset-clip 하나 — 시작은 애셋 앵커 소스 좌표, 길이는 정확히 런 길이."""
    clip = clips_by_instance.get(run.instance_id)
    if clip is None:
        raise ValueError(f"런의 인스턴스가 문서 클립이 아니다: {run.instance_id!r}")
    resource = resources[clip.resource_scoped_id]
    start = resource.start + Fraction(run.source_frames.start_frame) * fd
    duration = Fraction(run.source_frames.end_frame - run.source_frames.start_frame) * fd
    offset = Fraction(run.output_timeline_frames.start_frame) * fd
    element = ET.Element(
        "asset-clip",
        {
            "ref": resource.xml_id,
            "offset": format_fcpx_time(offset),
            "start": format_fcpx_time(start),
            "duration": format_fcpx_time(duration),
        },
    )
    _copy_static_attributes(element, source_of.get(clip.element_path), _CLIP_STATIC_ATTRIBUTES)
    spine.append(element)
    return _EmittedClip(
        resource_xml_id=resource.xml_id,
        offset=offset,
        start=start,
        duration=duration,
        enabled=clip.enabled,
        src_enable=clip.src_enable,
        markers=(),
    )


def _emit_gap(
    spine: ET.Element,
    gap: RetainedGap,
    source_of: dict[str, ET.Element],
    fd: Fraction,
) -> _EmittedGap:
    """갭 하나 — 입력 순서·길이 그대로, 자리만 리플 뒤 출력 오프셋이다."""
    duration = (
        Fraction(gap.output_timeline_frames.end_frame - gap.output_timeline_frames.start_frame) * fd
    )
    offset = Fraction(gap.output_timeline_frames.start_frame) * fd
    element = ET.Element(
        "gap",
        {"offset": format_fcpx_time(offset), "duration": format_fcpx_time(duration)},
    )
    _copy_static_attributes(
        element, source_of.get(f"gap:{gap.instance_id}"), _GAP_STATIC_ATTRIBUTES
    )
    spine.append(element)
    return _EmittedGap(offset=offset, duration=duration)


def _copy_static_attributes(
    element: ET.Element, source: ET.Element | None, names: tuple[str, ...]
) -> None:
    """이름 붙은 정적 속성만 원본 문자열 그대로 옮긴다 — 시각·참조는 제외이다.

    지원 부분집합이 읽는 속성만 보존한다. 그 밖의 속성(시각 오버라이드·uid·
    modDate)은 버린다 — 새 문서는 새 신원이고 시각은 계획에서 다시 만든다.
    """
    if source is None:
        return
    for name in names:
        value = source.get(name)
        if value is not None:
            element.set(name, value)


def _attach_markers(
    spine: ET.Element,
    emitted: list[_EmittedClip | _EmittedGap],
    runs: list[tuple[RetainedRun, int]],
    plan: EditPlan,
    resources: dict[str, AssetResource],
    clips_by_instance: dict[str, ClipInstance],
) -> None:
    """위치 있는 리뷰만 마커로 — 소유자는 내보낸 run, 반개 구간, 애셋 앵커 시계.

    조각을 다 나눈 **뒤에** 소유자를 고른다: 같은 인스턴스의 run은 서로소이므로
    프레임을 담는 run은 최대 하나다. 없으면 그 자리가 지워진 것이다 — 마커를
    옮겨 붙이지 않고 review 기록으로만 남는다.
    """
    fd = plan.frame_duration
    markers_by_index: dict[int, list[tuple[Fraction, str]]] = {}
    for issue in plan.review:
        if issue.instance_id is None or issue.source_frame is None:
            continue
        owner = _marker_owner(issue.instance_id, issue.source_frame, runs)
        if owner is None:
            continue
        run, index = owner
        clip = clips_by_instance[run.instance_id]
        resource = resources[clip.resource_scoped_id]
        start = resource.start + Fraction(issue.source_frame) * fd
        clip_start = resource.start + Fraction(run.source_frames.start_frame) * fd
        clip_end = resource.start + Fraction(run.source_frames.end_frame) * fd
        if not clip_start <= start < clip_end or start + fd > clip_end:
            raise ValueError(
                f"마커가 소유 클립 안에 있지 않다: 프레임 {issue.source_frame}"
                f" → {format_fcpx_time(start)}초 ∉ [{format_fcpx_time(clip_start)}초,"
                f" {format_fcpx_time(clip_end)}초) — 렌더 결함이다"
            )
        value = f"{_MARKER_VALUE_PREFIX}{issue.code}"
        # DTD 1.14 자식 순서(note → … → marker* → … → metadata?)에서 이 클립의
        # 자식은 마커뿐이므로 뒤에 잇는 것이 곧 정확한 위치다.
        spine[index].append(
            ET.Element(
                "marker",
                {
                    "start": format_fcpx_time(start),
                    "duration": format_fcpx_time(fd),
                    "value": value,
                },
            )
        )
        markers_by_index.setdefault(index, []).append((start, value))
    for index, markers in markers_by_index.items():
        item = emitted[index]
        if isinstance(item, _EmittedClip):
            emitted[index] = replace(item, markers=tuple(markers))


def _marker_owner(
    instance_id: str, frame: int, runs: list[tuple[RetainedRun, int]]
) -> tuple[RetainedRun, int] | None:
    """이슈 프레임을 담는 유일한 run — 반개 [start, end)라 경계 프레임은 뒤 조각의 것."""
    for run, index in runs:
        if run.instance_id != instance_id:
            continue
        if run.source_frames.start_frame <= frame < run.source_frames.end_frame:
            return run, index
    return None


def _rebind_original_media(
    root: ET.Element,
    document: ProjectDocument,
    media_by_resource: dict[str, ResolvedMedia],
) -> None:
    """애셋의 media-rep을 원본 하나로 다시 묶는다 — 프록시는 원본으로 못 갈아입는다."""
    resources_element = root.find("resources")
    if resources_element is None:
        raise ValueError("문서의 resources가 없다 — 파싱된 문서가 아니다")
    asset_elements = [child for child in resources_element if child.tag == "asset"]
    if len(asset_elements) != len(document.resources):
        raise ValueError("애셋 수가 문서와 다르다 — 원본이 바뀌었다")
    for element, resource in zip(asset_elements, document.resources, strict=True):
        original = media_by_resource[resource.scoped_id].original
        rep = ET.Element(
            "media-rep",
            {"kind": "original-media", "src": original.as_uri()},
        )
        kept = [child for child in element if child.tag not in ("media-rep", "bookmark")]
        for child in list(element):
            element.remove(child)
        element.append(rep)
        element.extend(kept)


def _renew_document_identity(root: ET.Element, duration: Fraction) -> None:
    """새 event/project 이름과 tcStart=0 — 낡은 uid는 못 박고 길이는 출력 전체다."""
    event = root.find("library/event")
    project = root.find("library/event/project")
    sequence = root.find("library/event/project/sequence")
    if event is None or project is None or sequence is None:
        raise ValueError("문서의 library/event/project/sequence 사슬이 없다 — 파싱된 문서가 아니다")
    event.set("name", _EXPORT_PROJECT_NAME)
    project.set("name", _EXPORT_PROJECT_NAME)
    event.attrib.pop("uid", None)
    project.attrib.pop("uid", None)
    sequence.set("tcStart", format_fcpx_time(Fraction(0)))
    sequence.set("duration", format_fcpx_time(duration))


def _total_output_frames(plan: EditPlan) -> int:
    """출력 전체 길이 — 타일링이 [0, 끝)을 정확히 덮으므로 최댓값이 끝이다."""
    tiles = [
        *(run.output_timeline_frames.end_frame for run in plan.runs),
        *(gap.output_timeline_frames.end_frame for gap in plan.gaps),
    ]
    return max(tiles, default=0)


# ═════════════════════════════════════════════════════════════
#  게시 — review·trace 기록과 원자적 포인터 교체
# ═════════════════════════════════════════════════════════════


def publish_package(
    destination: Path,
    document: ProjectDocument,
    media: Sequence[ResolvedMedia],
    plan: EditPlan,
    edit_captions: EditCaptionSet,
    source: BoundaryProjection,
    guards: Sequence[InstanceGuard],
    source_guards: Sequence[SourceGuard],
    *,
    dtd_path: Path | None = None,
) -> ExportPackage:
    """다섯 파일을 한 리비전으로 쓰고 검증해 `current`의 원자적 교체로 공개한다.

    관문 순서: 관리 뿌리 검사 → 렌더(구조·리비전·부재 짝) → 출처 신원 →
    불변 리비전 쓰기·해시·재구문·xmllint → 배타 잠금 아래 포인터 읽기·검사·교체.
    포인터 교체 전에 무엇이 실패하면 이 호출이 만든 리비전 디렉터리만 치우고
    옛 포인터·옛 리비전·사용자 물건은 그대로 둔다.

    `source_guards`는 필수 인자다 — 빈 기본값을 주지 않는다. 실패 닫힌 소스의
    이유 문장이 여기서 review.json으로 흐르는데, 안 넘기면 조용히 빈 성공이
    되는 길이 다시 열린다(PROTO-6b).
    """
    root = Path(os.path.abspath(destination))
    _require_managed_root(root)
    text, emitted = _render_for_publication(document, media, plan, edit_captions, guards)
    _require_source_identity(document, plan, source)
    review_payload = _review_payload(plan, edit_captions, source, guards, source_guards)
    revision_dir = _create_revision(root, plan.revision)
    try:
        package = _write_revision(
            revision_dir,
            plan.revision,
            fcpxml_text=text,
            edit_srt_text=edit_captions.srt_text,
            source_srt_text=source.source_srt,
            review_payload=review_payload,
            document=document,
            plan=plan,
            captions=edit_captions,
            source=source,
            emitted=emitted,
            dtd_path=dtd_path,
        )
        _switch_pointer(root, plan.revision)
    except BaseException:
        shutil.rmtree(revision_dir, ignore_errors=True)
        raise
    return package


def _render_for_publication(
    document: ProjectDocument,
    media: Sequence[ResolvedMedia],
    plan: EditPlan,
    edit_captions: EditCaptionSet,
    guards: Sequence[InstanceGuard],
) -> tuple[str, tuple[_EmittedClip | _EmittedGap, ...]]:
    """게시용 렌더 — `render_edit_fcpxml`와 같은 관문을 그대로 지나 전체를 돌려준다."""
    _require_renderable(document, plan, edit_captions, guards)
    return _edit_fcpxml_parts(document, media, plan)


def _require_source_identity(
    document: ProjectDocument, plan: EditPlan, source: BoundaryProjection
) -> None:
    """출처 신원 — 문서·계획·출처가 같은 입력을 말하고 부재 기록이 정확히 같아야 한다."""
    if plan.input_sha256 != document.input_sha256:
        raise ValueError(
            f"계획의 문서 해시가 문서와 다르다: {plan.input_sha256} ≠"
            f" {document.input_sha256} — 섞인 리비전이다"
        )
    if source.input_sha256 != plan.input_sha256:
        raise ValueError(
            f"출처의 문서 해시가 계획과 다르다: {source.input_sha256} ≠"
            f" {plan.input_sha256} — 섞인 리비전이다"
        )
    if plan.unavailable_instances != source.unavailable_instances:
        raise ValueError(
            "계획의 부재 기록이 출처와 정확히 같지 않다:"
            f" 계획 {len(plan.unavailable_instances)}건, 출처"
            f" {len(source.unavailable_instances)}건 — 섞인 리비전이다"
        )


def _review_payload(
    plan: EditPlan,
    captions: EditCaptionSet,
    source: BoundaryProjection,
    guards: Sequence[InstanceGuard],
    source_guards: Sequence[SourceGuard],
) -> dict:
    """review.json의 전부 — 최종 수용 판정은 여기서만 계산된다."""
    blockers = _final_blockers(plan, captions, source, guards)
    return {
        "kind": _REVIEW_KIND,
        "plan_revision": plan.revision,
        "input_sha256": plan.input_sha256,
        "final_accepted": not blockers,
        "final_blockers": blockers,
        "analysis_failures": _analysis_failures(guards, source_guards),
        "review": [_issue_record(issue) for issue in captions.review],
        "unavailable_instances": [_unavailable_record(row) for row in plan.unavailable_instances],
        "unresolved_request_ids": list(source.unresolved_request_ids),
        "unresolved_cue_ids": list(source.unresolved_cue_ids),
        "unresolved_cues": [
            _cue_endpoint_record(cue)
            for cue in source.cues
            if cue.cue_id in set(source.unresolved_cue_ids)
        ],
        "collapsed_cues": [
            _cue_endpoint_record(cue) for cue in captions.cues if SUBMS_COLLAPSED in cue.flags
        ],
    }


def _analysis_failures(
    guards: Sequence[InstanceGuard], source_guards: Sequence[SourceGuard]
) -> list[dict]:
    """실패 닫힘(`keep_all`) 가드의 기록 — 이유 문장을 문서 인스턴스 순서로 남긴다.

    인스턴스 문장(`review`)은 그 인스턴스의 가드가, 소스 문장(`source_review`)은
    그 소스의 keep_all 가드가 말한 것을 그대로 실는다 — 전사 트레이스 error 같은
    원인은 소스 가드에만 산다. 부재 소스의 합성 가드는 소스 가드가 없으므로
    `source_review`가 비지만 인스턴스 문장이 이미 원인을 말한다. `inaudible`은
    완결된 사실이지 실패가 아니므로 여기에 오지 않는다.
    """
    keep_all_sources = {
        guard.source_id: guard for guard in source_guards if guard.status == "keep_all"
    }
    return [
        {
            "instance_id": guard.instance_id,
            "source_id": guard.source_id,
            "review": list(guard.review),
            "source_review": (
                list(keep_all_sources[guard.source_id].review)
                if guard.source_id in keep_all_sources
                else []
            ),
        }
        for guard in guards
        if guard.status == "keep_all"
    ]


def _final_blockers(
    plan: EditPlan,
    captions: EditCaptionSet,
    source: BoundaryProjection,
    guards: Sequence[InstanceGuard],
) -> list[str]:
    """최종 수용을 막는 기계 코드 — 미해결 경계·출처 큐·EDIT 접힘·재시도 부재·가드·리뷰.

    SOURCE에서 접힌 큐는 EDIT에서 표현 가능해져도 EDIT 이슈를 남기지 않는다
    (P6-03b). 그래서 접힘 깃발을 큐에서도 다시 본다 — 이슈 목록만 보면 빈
    EDIT 이슈를 '미해결 없음'으로 잘못 읽는다.

    실패 닫힘(`keep_all`) 인스턴스 가드는 블로커다(PROTO-6b): 전사·VAD가 죽어
    소스 전체를 보호로 닫은 패키지가 '성공, 자막 0장'으로 나가던 결함이다.
    판정은 타입(`status`)으로만 한다 — 한국어 review 문장을 파싱하지 않는다.
    `inaudible`은 완결된 사실이라 막지 않는다. 합성 부재 가드도 keep_all이므로
    `retryable_unavailable_instances`와 함께 뜬다 — 중복이 아니라 다른 증거다.

    리뷰 코드는 허용 집합으로 판정한다: caption·edit 리뷰에
    `unavailable_declared_no_audio` 외의 코드가 하나라도 있으면 그 코드가
    곧 블로커다(중복 제거·정렬). 판정이 없거나 오류인 블록의 대기·실패
    증거가 이 길로 흘러 최종 수용을 막는다 — `decisions == ()` 같은 개수
    검사는 하지 않는다: 블록 없는 프로젝트도 정상이므로 신호는 계획과
    리뷰에서 온다(P4-04 모양).
    """
    blockers: list[str] = []
    if source.unresolved_request_ids:
        blockers.append("unresolved_boundary_requests")
    if source.unresolved_cue_ids:
        blockers.append("unresolved_source_cues")
    collapsed = any(issue.code == SUBMS_COLLAPSED for issue in captions.review) or any(
        SUBMS_COLLAPSED in cue.flags for cue in captions.cues
    )
    if collapsed:
        blockers.append("subms_collapsed_edit_cues")
    if any(row.reason_code in _RETRYABLE_UNAVAILABLE_REASONS for row in plan.unavailable_instances):
        blockers.append("retryable_unavailable_instances")
    if any(guard.status == "keep_all" for guard in guards):
        blockers.append("analysis_failed_instances")
    blockers.extend(
        sorted(
            {
                issue.code
                for issue in (*plan.review, *captions.review)
                if issue.code not in _FINAL_REVIEW_CODES_ALLOWED
            }
        )
    )
    return blockers


def _cue_endpoint_record(cue: object) -> dict:
    """미해결·접힘 큐의 정확한 끝점 — 산문이 아니라 기계 읽기 가능한 기록."""
    return {
        "cue_id": cue.cue_id,  # type: ignore[attr-defined]
        "instance_ids": list(cue.instance_ids),  # type: ignore[attr-defined]
        "timeline_start": _fraction(cue.timeline_start),  # type: ignore[attr-defined]
        "timeline_end": _fraction(cue.timeline_end),  # type: ignore[attr-defined]
    }


def _trace_payload(
    document: ProjectDocument,
    plan: EditPlan,
    captions: EditCaptionSet,
    source: BoundaryProjection,
    digests: dict[str, str],
) -> dict:
    """project_trace.json(= manifest)의 전부 — P7 재개가 읽는 정규 기록."""
    return {
        "kind": _TRACE_KIND,
        "plan_revision": plan.revision,
        "input_sha256": document.input_sha256,
        "config_sha256": plan.config_sha256,
        "frame_duration": _fraction(plan.frame_duration),
        "plan": plan_record(plan),
        "edit_captions": edit_captions_record(captions),
        "source": {
            "input_sha256": source.input_sha256,
            "unresolved_request_ids": list(source.unresolved_request_ids),
            "unresolved_cue_ids": list(source.unresolved_cue_ids),
            "unavailable_instances": [
                _unavailable_record(row) for row in source.unavailable_instances
            ],
        },
        "artifacts": {name: {"sha256": digest} for name, digest in sorted(digests.items())},
    }


def _write_revision(
    revision_dir: Path,
    revision: str,
    *,
    fcpxml_text: str,
    edit_srt_text: str,
    source_srt_text: str,
    review_payload: dict,
    document: ProjectDocument,
    plan: EditPlan,
    captions: EditCaptionSet,
    source: BoundaryProjection,
    emitted: tuple[_EmittedClip | _EmittedGap, ...],
    dtd_path: Path | None,
) -> ExportPackage:
    """다섯 파일을 쓰고·fsync하고·다시 읽어 증명한다 — 공개는 아직 아니다."""
    fcpxml_path = revision_dir / FCPXML_NAME
    edit_srt_path = revision_dir / EDIT_SRT_NAME
    source_srt_path = revision_dir / SOURCE_SRT_NAME
    review_path = revision_dir / REVIEW_NAME
    trace_path = revision_dir / TRACE_NAME
    payloads = {
        FCPXML_NAME: fcpxml_text.encode("utf-8"),
        EDIT_SRT_NAME: edit_srt_text.encode("utf-8"),
        SOURCE_SRT_NAME: source_srt_text.encode("utf-8"),
        REVIEW_NAME: _json_bytes(review_payload),
    }
    for name, payload in payloads.items():
        _write_durable(revision_dir / name, payload)
    digests = {
        name: _verified_digest(revision_dir / name, payload) for name, payload in payloads.items()
    }
    trace_payload = _trace_payload(document, plan, captions, source, digests)
    _write_durable(trace_path, _json_bytes(trace_payload))
    _fsync_directory(revision_dir)
    _require_trace_revision(trace_path, revision)
    _require_published_geometry(fcpxml_path, document, emitted)
    validation = xml_validation.validate_xml(fcpxml_path, dtd_path=dtd_path)
    if not validation.valid:
        raise ValueError(
            f"생성된 FCPXML이 xmllint 검증을 통과하지 못해 게시를 막는다 —"
            f" {len(validation.diagnostics)}줄 진단:"
            f" {'; '.join(validation.diagnostics[:_MAX_REFUSAL_LINES])}"
        )
    return ExportPackage(
        plan_revision=revision,
        fcpxml=fcpxml_path,
        edit_srt=edit_srt_path,
        source_srt=source_srt_path,
        project_trace=trace_path,
        review=review_path,
    )


def _verified_digest(path: Path, payload: bytes) -> str:
    """다시 읽은 바이트가 쓴 바이트와 같음을 증명하고 그 해시를 돌려준다."""
    written = path.read_bytes()
    if written != payload:
        raise ValueError(f"쓴 파일을 다시 읽으니 다르다: {path.name} — 디스크가 쓰기를 배신었다")
    return hashlib.sha256(written).hexdigest()


def _require_trace_revision(trace_path: Path, revision: str) -> None:
    """manifest의 리비전 — 다시 읽어 계획 리비전과 같은지 본다."""
    loaded = json.loads(trace_path.read_text(encoding="utf-8"))
    if loaded.get("plan_revision") != revision:
        raise ValueError(
            f"manifest의 리비전이 어긋난다: {loaded.get('plan_revision')!r} ≠"
            f" {revision!r} — 섞인 패키지다"
        )


def _require_published_geometry(
    fcpxml_path: Path, document: ProjectDocument, emitted: tuple[_EmittedClip | _EmittedGap, ...]
) -> None:
    """쓴 FCPXML을 지원 구조로 다시 읽어 의도한 기하와 정확히 같은지 본다.

    재구문 해석(`parse_project`)이 매달린 참조·겹침·0길이·격자 이탈을 이미
    거부한다. 여기는 그 위에서 **내보낸 기하 자신**과의 대조다 — 렌더가 뜻한
    것과 디스크에 있는 것이 다르면 게시하지 않는다.
    """
    reparsed = parse_project(fcpxml_path)
    if reparsed.sequence_tc_start != 0 or reparsed.frame_duration != document.frame_duration:
        raise ValueError("재구문 해석의 tcStart·프레임 길이가 의도와 다르다")
    if len(reparsed.storyline) != len(emitted):
        raise ValueError(
            f"재구문 해석의 스토리라인 길이가 다르다: {len(reparsed.storyline)} ≠ {len(emitted)}"
        )
    xml_ids = {resource.scoped_id: resource.xml_id for resource in reparsed.resources}
    for item, expected in zip(reparsed.storyline, emitted, strict=True):
        if isinstance(expected, _EmittedClip):
            _require_clip_geometry(item, expected, xml_ids)
        else:
            _require_gap_geometry(item, expected)


def _require_clip_geometry(item: object, expected: _EmittedClip, xml_ids: dict[str, str]) -> None:
    if not isinstance(item, ClipInstance):
        raise ValueError("재구문 해석의 항목 종류가 의도와 다르다 (클립 자리에 다른 것)")
    if (
        xml_ids.get(item.resource_scoped_id) != expected.resource_xml_id
        or item.offset != expected.offset
        or item.start != expected.start
        or item.duration != expected.duration
        or item.enabled != expected.enabled
        or item.src_enable != expected.src_enable
    ):
        raise ValueError(
            f"재구문 해석의 클립 기하가 의도와 다르다: ref {xml_ids.get(item.resource_scoped_id)}"
            f" offset {item.offset} start {item.start} duration {item.duration}"
            f" ≠ ref {expected.resource_xml_id} offset {expected.offset}"
            f" start {expected.start} duration {expected.duration}"
        )


def _require_gap_geometry(item: object, expected: _EmittedGap) -> None:
    from vlogsrt.project.models import GapInstance

    if not isinstance(item, GapInstance):
        raise ValueError("재구문 해석의 항목 종류가 의도와 다르다 (갭 자리에 다른 것)")
    if item.offset != expected.offset or item.duration != expected.duration:
        raise ValueError(
            f"재구문 해석의 갭 기하가 의도와 다르다: offset {item.offset}"
            f" duration {item.duration} ≠ offset {expected.offset}"
            f" duration {expected.duration}"
        )


# ─── 관리 뿌리·배타 잠금·원자적 포인터 ───────────────────────────────────


def _require_managed_root(root: Path) -> None:
    """목적지 검사 — 관리 이름 외 내용이 하나라도 있으면 사용자 물건이다.

    실패는 그 자리에서 끝난다: 이름 바꾸기·지우기·덮어쓰기는 일절 없다. 비어 있거나
    관리 이름뿐인 디렉터리는 관리 뿌리로 쓸 수 있다(첫 게시가 나머지를 만든다).
    """
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise ValueError(f"목적지가 관리 디렉터리가 아니다: {root} — 파일·심링크는 받지 않는다")
    if not root.exists():
        return
    unexpected = sorted(
        entry.name
        for entry in root.iterdir()
        if entry.name not in {_REVISIONS_DIR, _CURRENT, _LOCK}
        and not _is_staging_remnant(entry.name)
    )
    if unexpected:
        raise ValueError(
            f"목적지에 관리 밖 내용이 있다: {unexpected} — 사용자 물건을 건드리지 않고 거부한다"
        )
    _require_revisions_layout(root)
    if os.path.lexists(root / _CURRENT):
        _require_valid_pointer(root, root / _CURRENT)
    lock = root / _LOCK
    if os.path.lexists(lock) and not lock.is_file():
        raise ValueError(f"게시 잠금이 일반 파일이 아니다: {lock} — 레이아웃이 무효다")


def _is_staging_remnant(name: str) -> bool:
    """죽은 작성자가 남길 수 있는 포인터 임시 심링크 — 관리 이름 공간이 소유한다."""
    return name.startswith(_STAGING_PREFIX) and name.endswith(_STAGING_SUFFIX)


def _require_revisions_layout(root: Path) -> None:
    revisions = root / _REVISIONS_DIR
    if not revisions.exists():
        return
    if not revisions.is_dir():
        raise ValueError(f"revisions가 디렉터리가 아니다: {revisions} — 레이아웃이 무효다")
    stray = sorted(entry.name for entry in revisions.iterdir() if not entry.is_dir())
    if stray:
        raise ValueError(f"revisions 안에 디렉터리가 아닌 항목이 있다: {stray}")


def _require_valid_pointer(root: Path, current: Path) -> None:
    """`current`는 `revisions/<이름>`을 가리키는 심링크여야 하고 대상이 있어야 한다."""
    if not current.is_symlink():
        raise ValueError(f"current 포인터가 심링크가 아니다: {current} — 레이아웃이 무효다")
    target = os.readlink(current)
    prefix = f"{_REVISIONS_DIR}/"
    name = target[len(prefix) :] if target.startswith(prefix) else ""
    if not name or "/" in name:
        raise ValueError(f"current 포인터의 형식이 무효다: {target!r} — {prefix}<이름>이어야 한다")
    if not (root / target).is_dir():
        raise ValueError(f"current 포인터가 없는 리비전을 가리킨다: {target} — 복구가 필요하다")


def _create_revision(root: Path, revision: str) -> Path:
    """불변 리비전 디렉터리를 배타적으로 만든다 — 같은 리비전의 재게시는 여기서 진다."""
    revisions = root / _REVISIONS_DIR
    revisions.mkdir(parents=True, exist_ok=True)
    target = revisions / revision
    try:
        target.mkdir()
    except FileExistsError as error:
        raise ValueError(
            f"리비전 디렉터리가 이미 있다: {revision} — 같은 리비전의 재게시는"
            " 배타 생성에서 결정적으로 실패한다"
        ) from error
    return target


def _switch_pointer(root: Path, revision: str) -> None:
    """배타 잠금 아래 포인터를 읽고·검사하고·원자적으로 교체한다.

    잠금은 `os.open(O_CREAT|O_EXCL)`로 만든다 — 이미 있으면(살아있든 오래됐든)
    충돌이고, 나이만 보고 훔치지 않는다. 잠금 소유자만 임시 심링크를 만지므로,
    잠금 아래 보이는 임시 파일은 죽은 작성자의 잔해다. 독립된 읽기＋바꾸기를
    compare-and-swap이라 부르지 않는다 — 순서의 보증은 잠금이 준다.
    """
    lock = root / _LOCK
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError as error:
        raise ValueError(
            f"게시 잠금 충돌: {lock} — 다른 작성자가 있거나 명시적 복구가 필요하다"
            " (나이만으로 훔치지 않는다)"
        ) from error
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(f"{os.getpid()}\n")
            handle.flush()
            os.fsync(handle.fileno())
        current = root / _CURRENT
        if os.path.lexists(current):
            _require_valid_pointer(root, current)
        staging = root / f"{_STAGING_PREFIX}{revision}.{os.getpid()}{_STAGING_SUFFIX}"
        if os.path.lexists(staging):
            staging.unlink()
        os.symlink(f"{_REVISIONS_DIR}/{revision}", staging)
        os.replace(staging, current)
        _fsync_directory(root)
    finally:
        lock.unlink()


def _write_durable(path: Path, payload: bytes) -> None:
    """쓰고·flush하고·fsync하고 닫는다 — 반쪽 파일이 보이는 창을 닫는다."""
    with path.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    """디렉터리 항목의 내구성 — 파일 fsync만으로는 이름이 살아남지 않는다(Windows는 하지 않는다)."""
    oscompat.fsync_directory(path)


def _json_bytes(payload: dict) -> bytes:
    """정규형 JSON 바이트 — 결정적 키 순서, NaN 거부, 끝에 줄바꿈 하나."""
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
    return f"{text}\n".encode()
