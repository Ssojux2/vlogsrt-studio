#!/usr/bin/env python3
"""원본 미디어 경계 진단 — 조사·시계·지원 판정·(선택) 실제 추출 스포크.

제품 코드는 아니다. `vlogsrt.preprocess`의 공개 함수만 써서(ffmpeg 호출 경계,
규칙 7) A3 인수 검증과 현장 진단에 필요한 증거를 찍는다. 네트워크·API는 전혀
부르지 않는다.

종료 코드 — **초록이 아니면 0이 아니다**(지원 밖이 조용한 성공으로 보이면
인수 검증이 거짓 초록이 된다):
  0 = 모든 원본이 조사·판정 통과(및 요청한 추출 스포크까지 성공)
  1 = 조사 실패(ffprobe·파싱)
  2 = 추출 지원 밖(VFR·HDR·코덱·정밀도 매트릭스)
  3 = 추출 스포크 실패(추출·검증)
여러 원본을 넘기면 첫 0이 아닌 코드가 전체 종료 코드가 된다.

사용 예:
  scripts/verify_raw_media.py 카드/영상1.mp4 카드/영상2.mov
  scripts/verify_raw_media.py --extract 1.0-2.5 원본.mp4   # 실제 추출 스포크
  scripts/verify_raw_media.py --extract 1.0-2.5 --out /tmp/스포크 원본.mp4
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
    """실제 추출 스포크 — 실패는 깨끗한 보고와 0이 아닌 코드로 끝낸다(3).

    추론 과정(traceback)을 증거로 내놓는 대신, 실패 이유와 원본 불변 확인
    결과를 사람이 읽는 줄로 남긴다. 예외를 삼키지 않는다 — 줄로 보고하고
    종료 코드로 살린다.
    """
    try:
        clock = preprocess.probe_extraction_clock(source)
        start, end = Fraction(str(span[0])), Fraction(str(span[1]))
        source_range = preprocess.clock_range_covering(clock, start, end)
        # 같은 파일명의 다른 카드 원본이 서로를 덮어쓰지 않게 해시를 이름에 싣는다(§5.2).
        digest = _hash(source)[:12]
        target = out_dir / f"{source.stem}.{digest}.span-{span[0]:.3f}-{span[1]:.3f}.mp4"
        # 재실행으로 같은 이름이 남아 있으면 여기서 정리한다 — 경계(preprocess)는
        # 있는 출력을 덮어쓰지 않고 거부한다(A3P-F2). 이 이름은 이 도구가 정한
        # 것이고, unlink는 심볼릭 링크를 따라가지 않으므로 원본을 건드릴 수 없다.
        target.unlink(missing_ok=True)
        before = _hash(source)
        result = preprocess.extract_raw_clip(
            source, target, source_range, preprocess.RAW_SOURCE_MATCHED
        )
        if _hash(source) != before:
            print(f"EXTRACT FAILED: 원본이 바뀌었다 — 즉시 보고: {source}")
            return 3
    except (RuntimeError, OSError) as exc:  # RawMediaUnsupported·검증·스테이징 거부 포함
        print(f"EXTRACT FAILED: {source}\n{exc}")
        return 3
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
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="추출 스포크 출력 폴더(기본: 현재 폴더의 vlogsrt-raw-verify)",
    )
    args = parser.parse_args(argv)

    print(preprocess.ffmpeg_provenance())
    # 첫 0이 아닌 결과가 전체 종료 코드가 된다 — 지원 밖(2)도 조사 실패(1)도
    # 추출 실패(3)도 초록으로 보이게 하지 않는다.
    outcome = 0
    counts = {1: 0, 2: 0, 3: 0}
    for source in args.sources:
        code = _report_probe(source)
        if code == 0 and args.extract:
            start_s, end_s = args.extract.split("-")
            out_dir = args.out if args.out is not None else Path.cwd() / "vlogsrt-raw-verify"
            out_dir.mkdir(parents=True, exist_ok=True)
            code = _report_extraction(source, (float(start_s), float(end_s)), out_dir)
        if code:
            counts[code] += 1
            outcome = outcome or code
    if outcome:
        print(
            f"요약: 조사 실패 {counts[1]}, 지원 밖 {counts[2]}, 추출 실패 {counts[3]}"
            f" — 종료 코드 {outcome}"
        )
    return outcome


if __name__ == "__main__":
    sys.exit(main())
