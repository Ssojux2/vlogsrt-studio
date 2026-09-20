#!/usr/bin/env python3
"""파생 계층 전 구간을 **무료로** 한 번 돌린다 — 합성 클립·합성 픽스처.

`vlogsrt project run`이 파싱부터 패키지 게시까지 실제로 도는지 사람이 손으로
재현하지 않고 확인하는 자리다. ffprobe·ffmpeg·Silero VAD·xmllint는 진짜로 돌고,
전사만 합성 픽스처로 대신한다. **API를 부르지 않는다.**

소스 신원은 내용 해시라 미리 알 수 없다. 그래서 두 번 돌린다 — 첫 번째는 픽스처가
없어 실패로 닫히고(그 자체가 PROTO-6b의 확인이다), 그때 생긴 artifacts 디렉터리
이름에서 신원을 읽어 픽스처를 놓고 두 번째로 끝까지 간다.

    python scripts/smoke_project.py                 # 임시 디렉터리에 만들고 지운다
    python scripts/smoke_project.py --keep OUT_DIR  # 산출물을 남긴다

**덮지 못하는 것**: 비전 판정과 실제 컷. `vision_client`는 `--online`일 때만
만들어지므로(`project/commands.py`) 오프라인에서는 블록이 생기는 순간 막힌다.
이 스모크의 클립은 블록이 0이라 비전이 한 번도 돌지 않는다.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLIP_SECONDS = 10

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


def build_clip(workdir: Path) -> Path:
    """소리 있는 10초 클립. 필터 없이 만든다."""
    clip = workdir / "clip.mp4"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"testsrc2=duration={CLIP_SECONDS}:size=320x240:rate=30",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={CLIP_SECONDS}",
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


def run_pipeline(document: Path, out: Path, config: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "vlogsrt.cli", "project", "run", str(document),
         "-o", str(out), "--project-config", str(config)],
        cwd=ROOT, capture_output=True, text=True,
        env={"PYTHONPATH": str(ROOT), "PATH": __import__("os").environ.get("PATH", "")},
    )  # fmt: skip


def source_directory(out: Path) -> Path:
    """첫 실행이 남긴 소스 신원 디렉터리. `frames`·`pcm`은 신원이 아니다."""
    artifacts = out / "artifacts"
    named = [d for d in artifacts.iterdir() if d.is_dir() and d.name not in {"frames", "pcm"}]
    if len(named) != 1:
        raise SystemExit(f"소스 신원 디렉터리를 하나로 좁히지 못했다: {[d.name for d in named]}")
    return named[0]


def place_fixtures(source: Path) -> None:
    (source / "clip.whisper.json").write_text(
        json.dumps(WHISPER, ensure_ascii=False), encoding="utf-8"
    )
    (source / "clip.gpt.json").write_text(json.dumps(GPT, ensure_ascii=False), encoding="utf-8")


def report(out: Path, first: int, second: int) -> int:
    """확인 네 줄. 하나라도 어긋나면 1로 끝난다 — 조용히 통과시키지 않는다."""
    current = out / "package" / "current"
    srt = (current / "VLOG_AI_SOURCE.srt").read_text(encoding="utf-8")
    review = json.loads((current / "review.json").read_text(encoding="utf-8"))
    checks = [
        ("픽스처 없는 첫 실행이 실패로 닫힌다 (PROTO-6b)", first == 1),
        ("픽스처를 놓은 둘째 실행이 끝까지 간다", second == 0),
        ("자막이 비어 있지 않다", srt.strip() != ""),
        (f"글자 교정이 산출물까지 닿는다 ({RETEXTED_WORD})", RETEXTED_WORD in srt),
        ("최종 판정이 차단 없음이다", review["final_accepted"] is True),
    ]
    for label, passed in checks:
        print(f"[{'OK' if passed else 'FAIL'}] {label}")
    if all(passed for _label, passed in checks):
        print(f"\n자막 {srt.count('-->')}장 · 리비전 {current.resolve().name[:12]}")
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
        second = run_pipeline(document, workdir / "out-2", config)
        if second.returncode != 0:
            print(second.stdout, second.stderr, sep="\n")
        return report(workdir / "out-2", first.returncode, second.returncode)
    finally:
        if not args.keep:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
