import xml.etree.ElementTree as ET
from dataclasses import replace
from fractions import Fraction
from pathlib import Path

import pytest

from tests.raw.test_confirmed_package import build_request, make_review
from vlogsrt.project.clips import ClipProbe, synthesize_fcpxml
from vlogsrt.project.raw.confirmed_package import ConfirmedExportError
from vlogsrt.project.timebase import parse_fcpx_time


def test_export_filters_excluded_even_with_explicit_selection():
    review = make_review()
    review = replace(
        review, segments=(replace(review.segments[0], selection="excluded"), *review.segments[1:])
    )
    assert [c.segment_id for c in build_request(review=review).clips] == ["seg_a2", "seg_b1"]
    with pytest.raises(ConfirmedExportError, match="사용할 컷"):
        build_request(review=review, selected=frozenset({"seg_a1"}))
    restored = replace(
        review, segments=(replace(review.segments[0], selection="included"), *review.segments[1:])
    )
    assert len(build_request(review=restored).clips) == 3


def test_xml_uses_core_frames_without_reinserting_handles():
    clip = ClipProbe(
        Path("/tmp/synthetic.mov"),
        Fraction(1001, 30000),
        120,
        1920,
        1080,
        "1-1-1 (Rec. 709)",
        False,
        None,
        None,
        "a" * 64,
    )
    xml = ET.fromstring(
        synthesize_fcpxml([clip, clip], project_name="정리", ranges=[(9, 60), (12, 45)])
    )
    rows = xml.findall(".//spine/asset-clip")
    assert [parse_fcpx_time(r.attrib["start"]) for r in rows] == [
        9 * clip.frame_duration,
        12 * clip.frame_duration,
    ]
    assert [parse_fcpx_time(r.attrib["offset"]) for r in rows] == [0, 60 * clip.frame_duration]
    assert parse_fcpx_time(xml.find(".//sequence").attrib["duration"]) == 105 * clip.frame_duration
    assert parse_fcpx_time(xml.find(".//asset").attrib["duration"]) == 120 * clip.frame_duration
    with pytest.raises(Exception, match="프레임 범위"):
        synthesize_fcpxml([clip], project_name="bad", ranges=[(90, 60)])


def test_cancellation_of_cleanup_propagates_to_child(monkeypatch):
    from types import SimpleNamespace

    from vlogsrt.project.raw.jobs import JobCancelledError
    from vlogsrt.project.raw.service import RawWorkspaceService

    service = RawWorkspaceService()
    cancelled = []
    monkeypatch.setattr(service, "cancel_job", lambda root, job_id: cancelled.append(job_id))

    class CancelledContext:
        def check_cancel(self):
            raise JobCancelledError("parent")

    with pytest.raises(JobCancelledError):
        service.wait_child_job(
            Path("/synthetic"), SimpleNamespace(job_id="child"), CancelledContext()
        )
    assert cancelled == ["child"]
