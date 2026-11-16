#!/usr/bin/env python3
"""최종 편집(D117) 재생을 재 볼 오프라인 작업공간 하나를 심는다(W-71) — 유료 호출 0.

    python scripts/seed_final_edit_demo.py --out <스크래치>/demo [--seconds 300] [--size 3840x2160]
        [--rate 30] [--cuts 30] [--cut-seconds 1.5]

`<out>/shoot/`에 짧은 원본 하나를 두고 그 폴더의 작업공간(`shoot/_vlogsrt/`)을 만든 뒤, 실제 초안
렌더와 같은 인코딩(`RENDER_SETTINGS` — 고른 인코더 · 화소당 비트 또는 crf · `keyframe_seconds`마다
키프레임 · AAC)으로 합성 `draft.mp4`를 `export/<시각>/`에 쓰고 산출물로 등록한다. `--cuts`가 0보다
크면 키프레임과 어긋난 자리를 고르게 지운 편집을 `final_edit_io.save_edit`(실제 저장 길)로 남긴다 —
재생이 컷 `--cuts`개를 건넌다. 앱은 `python -m ui.app --offline`으로 띄우고 「폴더 불러오기」에
`<out>/shoot`을 넣는다.

D117의 720p 합성 초안은 디코드가 가벼워 키프레임 시크의 멈춤이 드러나지 않았다 — 기본값은
아이폰 원본과 같은 4K 30fps다.
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from vlogsrt import log, preprocess  # noqa: E402
from vlogsrt.project.raw import final_edit_io, render_export  # noqa: E402
from vlogsrt.project.raw.config import RENDER_SETTINGS  # noqa: E402
from vlogsrt.project.raw.service import RawWorkspaceService  # noqa: E402
from vlogsrt.project.raw.workspace import RawWorkspace  # noqa: E402

CUE_EVERY_SECONDS = 3
CUE_SECONDS = 2
CUT_PHASE = Fraction(7, 10)
"""지운 구간 시작을 키프레임(2초 격자)에서 비켜 놓는 몫 — 시크가 GOP 가운데에 떨어지게 한다."""


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--seconds", type=int, default=300)
    parser.add_argument("--size", default="3840x2160")
    parser.add_argument("--rate", default="30", help="프레임레이트 — 30 · 30000/1001 · 60")
    parser.add_argument("--cuts", type=int, default=30)
    parser.add_argument("--cut-seconds", type=Fraction, default=Fraction(3, 2))
    return parser.parse_args()


def _ffmpeg(args: list[str]) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def _source_clip(shoot: Path) -> None:
    """폴더 불러오기가 받을 원본 하나 — 영상이 없는 폴더는 작업공간을 만들지 않는다."""
    shoot.mkdir(parents=True, exist_ok=True)
    _ffmpeg(
        ["-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=2",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
         str(shoot / "source.mp4")]
    )  # fmt: skip


def _video_args(encoder: str, width: int, height: int, frame: Fraction) -> list[str]:
    gop = max(1, math.ceil(RENDER_SETTINGS.keyframe_seconds / frame))
    if encoder == "libx264":
        quality = ["-preset", RENDER_SETTINGS.preset, "-crf", str(RENDER_SETTINGS.crf)]
    else:
        pixels = width * height / frame
        quality = ["-b:v", str(math.ceil(RENDER_SETTINGS.hardware_bits_per_pixel * pixels))]
    return ["-c:v", encoder, *quality, "-g", str(gop), "-pix_fmt", "yuv420p"]


def _draft_video(path: Path, size: str, frame: Fraction, frames: int) -> str:
    """실제 렌더의 인코더 인자로 움직이는 시험 화면 + 매 초 삐 소리."""
    width, height = (int(part) for part in size.split("x"))
    encoder = preprocess.pick_video_encoder(RENDER_SETTINGS.video_codecs)
    rate = f"{frame.denominator}/{frame.numerator}"
    seconds = float(frames * frame)
    beep = "sine=frequency=1000:sample_rate=48000,volume='if(lt(mod(t,1),0.03),1,0)':eval=frame"
    _ffmpeg(
        ["-f", "lavfi", "-i", f"testsrc2=size={size}:rate={rate}",
         "-f", "lavfi", "-i", beep,
         "-frames:v", str(frames), "-t", f"{seconds:.6f}",
         *_video_args(encoder, width, height, frame),
         "-c:a", RENDER_SETTINGS.audio_codec, "-b:a", f"{RENDER_SETTINGS.audio_bitrate_kbps}k",
         "-ar", str(RENDER_SETTINGS.audio_rate), "-ac", str(RENDER_SETTINGS.audio_channels),
         "-movflags", "+faststart", str(path)]
    )  # fmt: skip
    return encoder


def _srt_time(seconds: int) -> str:
    return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d},000"


def _draft_srt(path: Path, seconds: int) -> None:
    blocks = []
    for index, start in enumerate(range(1, seconds - CUE_SECONDS, CUE_EVERY_SECONDS), start=1):
        times = f"{_srt_time(start)} --> {_srt_time(start + CUE_SECONDS)}"
        blocks.append(f"{index}\n{times}\n자막 {index}번 — {start}초\n")
    path.write_text("\n".join(blocks), encoding="utf-8")


def _report(path: Path, frame: Fraction, frames: int, encoder: str) -> None:
    rational = {"num": frame.numerator, "den": frame.denominator}
    total = frames * frame
    report = {
        "schema": render_export.REPORT_SCHEMA,
        "sequence": {"frame_duration": rational},
        "total_frames": frames,
        "total_seconds": {"num": total.numerator, "den": total.denominator},
        "parts": [],
        "encoder": encoder,
        "seeded_by": "scripts/seed_final_edit_demo.py",
    }
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _kept_clips(frames: int, frame: Fraction, cuts: int, cut_seconds: Fraction) -> list[list[int]]:
    """`cuts`개의 지운 구간을 고르게 — 각 구간은 키프레임 격자에서 비켜 GOP 가운데서 시작한다."""
    cut_frames = max(1, round(cut_seconds / frame))
    gop = max(1, math.ceil(RENDER_SETTINGS.keyframe_seconds / frame))
    stride = frames // (cuts + 1)
    clips, start = [], 0
    for index in range(1, cuts + 1):
        cut_at = (index * stride // gop) * gop + round(CUT_PHASE * gop)
        if cut_at <= start or cut_at + cut_frames >= frames:
            continue
        clips.append([start, cut_at])
        start = cut_at + cut_frames
    clips.append([start, frames])
    return clips


def _save_cuts(
    workspace: RawWorkspace, frame: Fraction, frames: int, args: argparse.Namespace
) -> int:
    model = final_edit_io.open_edit(workspace)
    clips = _kept_clips(frames, frame, args.cuts, args.cut_seconds)
    payload = {**model["doc"], "clips": clips, "seedSrt": model["seedSrt"]}
    final_edit_io.save_edit(workspace, model["exportId"], model["revision"], payload)
    return len(clips) - 1


def main() -> int:
    args = _args()
    frame = 1 / Fraction(args.rate)
    frames = round(args.seconds / frame)
    shoot = args.out.resolve() / "shoot"
    _source_clip(shoot)
    status, _ = RawWorkspaceService().managed_folder_workspace(shoot)
    workspace = RawWorkspace.open(Path(status.workspace_root))
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    folder = workspace.root / render_export.EXPORT_FOLDER / stamp
    folder.mkdir(parents=True)
    encoder = _draft_video(folder / render_export.VIDEO_NAME, args.size, frame, frames)
    _draft_srt(folder / render_export.SRT_NAME, args.seconds)
    _report(folder / render_export.REPORT_NAME, frame, frames, encoder)
    workspace.register_media_file(folder / render_export.VIDEO_NAME, "artifact")
    cuts = _save_cuts(workspace, frame, frames, args) if args.cuts > 0 else 0
    log.info(f"초안을 심었다: {folder} ({args.seconds}초 · {args.size} · {encoder} · 컷 {cuts}개)")
    log.info(f"앱: python -m ui.app --offline → 「폴더 불러오기」에 {shoot}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
