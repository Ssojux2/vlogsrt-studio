#!/usr/bin/env python3
"""원본 미디어 경계 진단 — 조사·시계·지원 판정·(선택) 실제 추출 스포크.

제품 코드는 아니다. `vlogsrt.preprocess`의 공개 함수만 써서(ffmpeg 호출 경계,
규칙 7) A3 인수 검증과 현장 진단에 필요한 증거를 찍는다. 네트워크·API는 전혀
부르지 않는다.

사용 예:
  scripts/verify_raw_media.py 카드/영상1.mp4 카드/영상2.mov
  scripts/verify_raw_media.py --extract 1.0-2.5 원본.mp4   # 실제 추출 스포크
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from fractions import Fraction
from pathlib import Path

from vlogsrt import preprocess


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _report_probe(source: Path) -> int:
    """하나의 원본에 대한 조사·시계·지원 판정을 찍는다. 실패는 0이 아닌 종료."""
    print(f"=== {source}")
    try:
        probe = preprocess.probe_raw_media(source)
        clock = preprocess.probe_extraction_clock(source, probe=probe)
        extents = preprocess.probe_audio_extents(source)
    except RuntimeError as exc:
        print(f"PROBE FAILED: {exc}")
        return 1
    video = probe.video
    print(f"container {probe.container} duration {probe.duration}s")
    shot = probe.creation_time
    print(
        f"creation_time {shot.raw!r} valid={shot.valid}"
        + (f" utc={shot.utc.isoformat()}" if shot.utc else "")
    )
    print(
        f"video #{video.stream_index}: {video.codec_name} {video.profile} "
        f"{video.pix_fmt}({video.bit_depth}bit) {video.width}x{video.height} "
        f"rot {video.rotation_degrees}° tb {video.time_base} r {video.r_frame_rate}"
    )
    colors = (
        f"primaries={video.color_primaries or '?'} transfer={video.color_transfer or '?'} "
        f"matrix={video.color_matrix or '?'} range={video.color_range or '?'}"
    )
    print(f"colors: {colors}")
    print(
        f"clock: origin {clock.presentation_origin}s fd {clock.frame_duration}s "
        f"frames {clock.frame_count} exact_end {clock.exact_end}s "
        f"uniform={clock.uniform_grid}"
    )
    if clock.grid_note:
        print(f"grid_note: {clock.grid_note}")
    for track, extent in zip(probe.audio, extents, strict=False):
        first = "None" if extent.first_pts is None else f"{extent.first_pts * extent.time_base}s"
        print(
            f"audio #{track.stream_index}: {track.codec_name} {track.channels}ch "
            f"{track.sample_rate}Hz start(선언) {track.start_time}s "
            f"first-pkt {first} packets {extent.packet_count}"
        )
    if probe.other_tracks:
        print(f"other tracks: {probe.other_tracks}")
    try:
        preprocess._assert_raw_supported(probe, clock)
    except preprocess.RawMediaUnsupported as exc:
        print(f"VERDICT: 지원 밖 — {exc}")
        return 2
    print("VERDICT: source_matched/edit_prores 추출 가능")
    return 0


def _report_extraction(source: Path, span: tuple[float, float], out_dir: Path) -> int:
    clock = preprocess.probe_extraction_clock(source)
    start, end = Fraction(str(span[0])), Fraction(str(span[1]))
    source_range = preprocess.clock_range_covering(clock, start, end)
    # 같은 파일명의 다른 카드 원본이 서로를 덮어쓰지 않게 해시를 이름에 싣는다(§5.2).
    digest = _hash(source)[:12]
    target = out_dir / f"{source.stem}.{digest}.span-{span[0]:.3f}-{span[1]:.3f}.mp4"
    before = _hash(source)
    result = preprocess.extract_raw_clip(
        source, target, source_range, preprocess.RAW_SOURCE_MATCHED
    )
    assert _hash(source) == before, "원본이 바뀌었다 — 즉시 보고"
    print(
        f"clip {target.name}: {result.frame_count}프레임 fd {result.frame_duration}s "
        f"core 지역 [{result.core_local_start}, {result.core_local_end})s "
        f"절대 [{result.media_range.start_seconds}, {result.media_range.end_seconds})s "
        f"audio {result.audio_local_starts} 경고 {result.warnings or '없음'}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sources", nargs="+", type=Path, help="조사할 미디어 파일")
    parser.add_argument(
        "--extract",
        metavar="START-END",
        default=None,
        help="초 범위를 실제로 추출해 본다(예: 1.0-2.5). 원본은 불변",
    )
    args = parser.parse_args(argv)

    print(preprocess.ffmpeg_provenance())
    failures = 0
    for source in args.sources:
        code = _report_probe(source)
        if code == 0 and args.extract:
            start_s, end_s = args.extract.split("-")
            out_dir = Path.cwd() / "vlogsrt-raw-verify"
            out_dir.mkdir(parents=True, exist_ok=True)
            code = _report_extraction(source, (float(start_s), float(end_s)), out_dir)
        if code == 1:
            failures += 1
    if failures:
        print(f"{failures}개 파일을 조사하지 못했다")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
