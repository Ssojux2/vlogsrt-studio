#!/usr/bin/env python3
"""파생 계층 전 구간을 **무료로** 한 번 돌린다 — 합성 클립·합성 픽스처·비전 재생.

`vlogsrt project run`이 파싱부터 패키지 게시까지 실제로 도는지 사람이 손으로
재현하지 않고 확인하는 자리다. ffprobe·ffmpeg·Silero VAD·xmllint는 진짜로 돌고,
전사와 비전 판정만 합성 픽스처로 대신한다. **API를 부르지 않는다.**

소스 신원은 내용 해시라 미리 알 수 없다. 그래서 세 번 돌린다:

1. 픽스처가 없어 실패로 닫힌다(그 자체가 PROTO-6b의 확인이다). artifacts 디렉터리
   이름에서 소스 신원을 읽어 전사 픽스처를 놓는다.
2. 닫힌 비전(무플래그)으로 돈다 — 블록마다 `decision_error`로 막히되
   `artifacts/vision/<block_id>.request.json` 덤프가 블록 수만큼 남는다.
   덤프에서 블록 아이디와 후보를 읽어 재생 픽스처를 만든다.
3. `--vision-fixtures`로 재생한다 — 판정이 실제로 적용돼 시퀀스가 짧아진다.

    python scripts/smoke_project.py                 # 임시 디렉터리에 만들고 지운다
    python scripts/smoke_project.py --keep OUT_DIR  # 산출물을 남긴다

**덮지 못하는 것**: 유료 비전 1콜(호환성)과 실물 Final Cut 왕복. 재생 판정은
진짜 판정이 아니고 stdout이 그 사실을 밝힌다.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLIP_SECONDS = 30
SOUND_SECONDS = 10  # 0~10초만 소리 — 합성 전사도 이 구간만 덮는다

FCPXML = """<?xml version="1.0" encoding="UTF-8"?>
<fcpxml version="1.14">
  <resources>
    <format id="r1" name="FFVideoFormat320x240p30" frameDuration="1/30s" width="320" height="240"/>
    <asset id="r2" name="clip" start="0s" duration="{seconds}s" hasVideo="1" hasAudio="1"
           format="r1" audioSources="1" audioChannels="2" audioRate="48000">
      <media-rep kind="original-media" src="file://{clip}"/>
    </asset>
  </resources>
  <library>
    <event name="SmokeEvent">
      <project name="SmokeProject">
        <sequence format="r1" duration="{seconds}s" tcStart="0s" tcFormat="NDF">
          <spine>
            <asset-clip ref="r2" name="clip" offset="0s" start="0s" duration="{seconds}s"
                        format="r1"/>
          </spine>
        </sequence>
      </project>
    </event>
  </library>
</fcpxml>
"""

# 합성 전사. 글자 교정이 실제로 걸리는지 보이려고 whisper와 gpt가 한 낱말만 다르다.
# 0~10초 구간만 덮는다 — 나머지 20초는 무음이라 보호가 없고 블록의 재료가 된다.
WHISPER = {
    "chunks": [
        {
            "index": 0,
            "offset": 0.0,
            "response": {
                "task": "transcribe",
                "language": "korean",
                "duration": float(CLIP_SECONDS),
                "segments": [
                    {
                        "id": 0,
                        "start": 0.5,
                        "end": 2.5,
                        "text": " 안녕하세요 오늘은 망원동에 왔습니다",
                    },
                    {
                        "id": 1,
                        "start": 3.0,
                        "end": 5.5,
                        "text": " 여기는 블루팽귄이라는 가게입니다",
                    },
                    {"id": 2, "start": 6.5, "end": 9.0, "text": " 낮술 하러 왔어요"},
                ],
            },
        }
    ]
}
GPT = {
    "chunks": [
        {
            "index": 0,
            "offset": 0.0,
            "response": {
                "text": "안녕하세요 오늘은 망원동에 왔습니다 "
                "여기는 블루펭귄이라는 가게입니다 낮술 하러 왔어요"
            },
        }
    ]
}
RETEXTED_WORD = "블루펭귄"  # gpt만 아는 철자. 이것이 SRT에 있으면 교정이 닿은 것이다

JPEG_MAGIC = b"\xff\xd8\xff"
"""프레임 표본이 진짜 JPEG인지 확인하는 머리 바이트 — 재생 판정의 재료다."""


def build_clip(workdir: Path) -> Path:
    """소리 10초 + 무음 20초 클립. 필터 없이 만든다.

    영상은 `testsrc`다 — 화면 안에 프레임 카운터가 각인돼 사람이 뽑힌 프레임을
    눈으로 읽을 수 있다(이 머신의 ffmpeg 9.0.1에는 `drawtext`가 없다).
    """
    clip = workdir / "clip.mp4"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"testsrc=duration={CLIP_SECONDS}:size=320x240:rate=30",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={SOUND_SECONDS}:sample_rate=48000",
         "-f", "lavfi", "-i",
         f"anullsrc=duration={CLIP_SECONDS - SOUND_SECONDS}:sample_rate=48000:channel_layout=mono",
         "-filter_complex", "[1:a][2:a]concat=n=2:v=0:a=1[a]",
         "-map", "0:v", "-map", "[a]",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "64k",
         "-shortest", str(clip)],
        check=True,
    )  # fmt: skip
    return clip


def build_project(workdir: Path, clip: Path) -> tuple[Path, Path]:
    """최소 FCPXML과 프로젝트 설정. 구문은 xmllint로 확인한다."""
    document = workdir / "smoke.fcpxml"
    document.write_text(FCPXML.format(seconds=CLIP_SECONDS, clip=clip), encoding="utf-8")
    subprocess.run(["xmllint", "--noout", str(document)], check=True)
    config = workdir / "smoke.project.toml"
    config.write_text("schema_version = 1\n", encoding="utf-8")
    return document, config


def run_pipeline(
    document: Path, out: Path, config: Path, *extra: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "vlogsrt.cli", "project", "run", str(document),
         "-o", str(out), "--project-config", str(config), *extra],
        cwd=ROOT, capture_output=True, text=True,
        env={"PYTHONPATH": str(ROOT), "PATH": __import__("os").environ.get("PATH", "")},
    )  # fmt: skip


def source_directory(out: Path) -> Path:
    """첫 실행이 남긴 소스 신원 디렉터리. `frames`·`pcm`·`vision`은 신원이 아니다."""
    artifacts = out / "artifacts"
    named = [
        d for d in artifacts.iterdir() if d.is_dir() and d.name not in {"frames", "pcm", "vision"}
    ]
    if len(named) != 1:
        raise SystemExit(f"소스 신원 디렉터리를 하나로 좁히지 못했다: {[d.name for d in named]}")
    return named[0]


def place_fixtures(source: Path) -> None:
    (source / "clip.whisper.json").write_text(
        json.dumps(WHISPER, ensure_ascii=False), encoding="utf-8"
    )
    (source / "clip.gpt.json").write_text(json.dumps(GPT, ensure_ascii=False), encoding="utf-8")


def block_dumps(out: Path) -> list[Path]:
    """닫힌 실행이 남긴 비전 요청 덤프 — 파일 이름이 곧 블록 아이디다."""
    vision = out / "artifacts" / "vision"
    return sorted(vision.glob("*.request.json")) if vision.is_dir() else []


def front_candidate(dump: Path) -> str:
    """덤프의 안내 문장에서 첫 후보 아이디 — 재생 픽스처가 고를 값이다."""
    payload = json.loads(dump.read_text(encoding="utf-8"))
    text = payload["messages"][0]["content"][0]["text"]
    for line in text.splitlines():
        if line.startswith("후보(블록 앞쪽부터): "):
            return line.split("후보(블록 앞쪽부터): ", 1)[1].split(",")[0].strip()
    raise SystemExit(f"덤프에서 후보 목록을 찾지 못했다: {dump}")


def place_vision_fixtures(dumps: list[Path], fixtures: Path) -> int:
    """블록마다 승인 응답 픽스처를 만든다 — 재생 판정의 재료다."""
    fixtures.mkdir(parents=True, exist_ok=True)
    for dump in dumps:
        body = json.dumps(
            {
                "choice": front_candidate(dump),
                "reason": "스모크 재생 픽스처 — 같은 내용의 단순 되풀이 B-roll로 적립했다",
                "reason_kind": "repetitive_ordinary_broll",
                "evidence_frame_ids": [],
                "confidence": 0.9,
            },
            ensure_ascii=False,
        )
        reply = {"choices": [{"message": {"content": body}}]}
        (fixtures / f"{dump.name.split('.')[0]}.vision.json").write_text(
            json.dumps(reply, ensure_ascii=False), encoding="utf-8"
        )
    return len(dumps)


def sequence_duration(fcpxml: Path) -> float:
    """시퀀스 duration을 기계로 읽는다 — 입력·출력을 같은 눈금으로 잰다."""
    from vlogsrt.project.timebase import parse_fcpx_time

    sequence = ET.parse(fcpxml).getroot().find("library/event/project/sequence")
    if sequence is None or sequence.get("duration") is None:
        raise SystemExit(f"sequence duration을 읽지 못했다: {fcpxml}")
    return float(parse_fcpx_time(sequence.get("duration")))


def all_frames_are_jpeg(out: Path) -> int:
    """프레임 표본 전부가 진짜 JPEG인지 머리 바이트로 본다 — 개수를 돌려준다."""
    frames = sorted((out / "artifacts" / "frames").glob("*.jpg"))
    if not frames:
        raise SystemExit("프레임 표본이 한 장도 없다 — 재생 판정의 재료가 없다")
    for frame in frames:
        if frame.read_bytes()[:3] != JPEG_MAGIC:
            raise SystemExit(f"프레임이 JPEG이 아니다: {frame}")
    return len(frames)


def report(out: Path, runs: dict[str, int], blocks: int, removed: int, frames: int) -> int:
    """확인 줄. 하나라도 어긋나면 1로 끝난다 — 조용히 통과시키지 않는다."""
    current = out / "package" / "current"
    srt = (current / "VLOG_AI_SOURCE.srt").read_text(encoding="utf-8")
    review = json.loads((current / "review.json").read_text(encoding="utf-8"))
    output_duration = sequence_duration(current / "VLOG_AI_EDIT.fcpxml")
    checks = [
        ("픽스처 없는 첫 실행이 실패로 닫힌다 (PROTO-6b)", runs["first"] == 1),
        ("닫힌 비전(무플래그)도 decision_error로 막힌다", runs["closed"] == 1),
        ("재생 실행이 종료 0으로 끝까지 간다", runs["replayed"] == 0),
        ("자막이 비어 있지 않다", srt.strip() != ""),
        (f"글자 교정이 산출물까지 닿는다 ({RETEXTED_WORD})", RETEXTED_WORD in srt),
        ("최종 판정이 차단 없음이다", review["final_accepted"] is True),
        (f"블록이 둘 이상 나온다 (실측 {blocks}개)", blocks >= 2),
        (f"제거 {removed}건 ≥ 1", removed >= 1),
        (
            f"출력 시퀀스 {output_duration:g}초 < 입력 {CLIP_SECONDS}초",
            output_duration < CLIP_SECONDS,
        ),
        (f"프레임 {frames}장이 전부 진짜 JPEG이다", frames > 0),
    ]
    for label, passed in checks:
        print(f"[{'OK' if passed else 'FAIL'}] {label}")
    if all(passed for _label, passed in checks):
        print(
            f"\n자막 {srt.count('-->')}장 · 블록 {blocks}개 · 제거 {removed}건 ·"
            f" 리비전 {current.resolve().name[:12]}"
        )
        print(srt)
        return 0
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", type=Path, default=None, help="산출물을 남길 디렉터리")
    args = parser.parse_args()

    workdir = args.keep if args.keep else Path(tempfile.mkdtemp(prefix="vlogsrt-smoke-"))
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        clip = build_clip(workdir)
        document, config = build_project(workdir, clip)

        first = run_pipeline(document, workdir / "out-1", config)
        place_fixtures(source_directory(workdir / "out-1"))
        shutil.copytree(workdir / "out-1" / "artifacts", workdir / "out-2" / "artifacts")
        closed = run_pipeline(document, workdir / "out-2", config)
        dumps = block_dumps(workdir / "out-2")
        if not dumps:
            print(closed.stdout, closed.stderr, sep="\n")
            raise SystemExit("닫힌 실행이 비전 요청 덤프를 남기지 않았다 — 블록이 0개다")
        blocks = place_vision_fixtures(dumps, workdir / "vision-fixtures")
        shutil.copytree(workdir / "out-2" / "artifacts", workdir / "out-3" / "artifacts")
        replayed = run_pipeline(
            document,
            workdir / "out-3",
            config,
            "--vision-fixtures",
            str(workdir / "vision-fixtures"),
        )
        if replayed.returncode != 0:
            print(replayed.stdout, replayed.stderr, sep="\n")
        trace = json.loads(
            (workdir / "out-3" / "package" / "current" / "project_trace.json").read_text(
                encoding="utf-8"
            )
        )
        runs = {
            "first": first.returncode,
            "closed": closed.returncode,
            "replayed": replayed.returncode,
        }
        return report(
            workdir / "out-3",
            runs,
            blocks,
            removed=len(trace["plan"]["removed"]),
            frames=all_frames_are_jpeg(workdir / "out-3"),
        )
    finally:
        if not args.keep:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
