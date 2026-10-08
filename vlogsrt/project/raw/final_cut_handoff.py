"""Build a frame-checked Final Cut timeline beside a verified immutable clip package."""

from __future__ import annotations

import json
import os
import tempfile
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path
from urllib.parse import unquote, urlparse

from vlogsrt.project.clips import probe_clips, synthesize_fcpxml
from vlogsrt.project.raw.confirmed_package import MANIFEST_NAME, verify_confirmed_export
from vlogsrt.project.timebase import parse_fcpx_time


def build_handoff(root: Path, destination_dir: str) -> Path:
    """Only package core ranges reach the spine; extraction handles remain media-only."""
    verify_confirmed_export(root, destination_dir)
    package = root / destination_dir
    manifest = json.loads((package / MANIFEST_NAME).read_text(encoding="utf-8"))
    records = manifest["clips"]
    paths = [package / row["export_relative_path"] for row in records]
    clips = probe_clips(paths)
    ranges: list[tuple[int, int]] = []
    for clip, row in zip(clips, records, strict=True):
        timebase = Fraction(row["time_base"]["num"], row["time_base"]["den"])
        begin, end = row["core_range_pts"]
        media_begin = row["media_range_pts"][0]
        start = (begin - media_begin) * timebase / clip.frame_duration
        length = (end - begin) * timebase / clip.frame_duration
        if start.denominator != 1 or length.denominator != 1:
            raise ValueError(f"{clip.path.name}: 사용 구간이 추출 파일의 프레임 경계와 다릅니다")
        ranges.append((int(start), int(length)))
    payload = synthesize_fcpxml(clips, project_name="vlogsrt 정리 결과", ranges=ranges)
    xml = ET.fromstring(payload)
    spine = xml.findall(".//spine/asset-clip")
    cursor = Fraction(0)
    if len(spine) != len(clips):
        raise ValueError("FCPXML 컷 수 검증 실패")
    for item, clip, (start, length) in zip(spine, clips, ranges, strict=True):
        expected = (cursor, start * clip.frame_duration, length * clip.frame_duration)
        actual = tuple(parse_fcpx_time(item.attrib[key]) for key in ("offset", "start", "duration"))
        if actual != expected:
            raise ValueError(f"{clip.path.name}: FCPXML 시간 검증 실패")
        cursor += expected[2]
    sequence = xml.find(".//sequence")
    if sequence is None or parse_fcpx_time(sequence.attrib["duration"]) != cursor:
        raise ValueError("FCPXML 총길이 검증 실패")
    for media in xml.findall(".//media-rep"):
        path = Path(unquote(urlparse(media.attrib["src"]).path))
        if path not in paths or not path.is_file():
            raise ValueError("FCPXML 미디어 경로 검증 실패")
    # Immutable package contents stay covered by their original manifest. The XML
    # is its sibling, so verification/retries never mutate a published package.
    target = package.with_suffix(".fcpxml")
    fd, temporary = tempfile.mkstemp(prefix=".fcpxml-", dir=package.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return target
