#!/usr/bin/env python3
"""영상 → 오디오 → 청크 → 타임스탬프 사슬을 실제 파일로 대조한다.

"자막이 소리와 안 맞는다"는 신고가 들어왔을 때, 원인이 **절단**인지 **전사**인지
기계적으로 가른다. 사람이 귀로 듣지 않아도 답이 나와야 한다.

API를 부르지 않는다. 이미 녹화된 픽스처(`tests/fixtures/api/`)와 전처리 캐시
(`.vlogsrt-cache/`)만 읽는다.

    python scripts/verify_timing.py fr_day_1_2_fhd

여기 있는 허용오차는 **검증 도구의 판정 기준**이지 파이프라인 임계값이 아니다.
파이프라인 임계값은 전부 `vlogsrt/config.py`에 있다(불변 규칙 6).
"""

from __future__ import annotations

import array
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# 골격 해석 중 나오는 세그먼트별 경고는 여기서 요약으로 대신 센다.
os.environ.setdefault("VLOGSRT_LOG_LEVEL", "ERROR")

from vlogsrt.preprocess import probe_duration, require_ffmpeg  # noqa: E402
from vlogsrt.transcribe import FIXTURE_KIND, build_spine, load_fixture_responses  # noqa: E402

# ── 검증 허용오차 ────────────────────────────────────────────
PROBE_RATE = 4000  # 포락선 계산용 디코딩 주파수. 에너지만 보면 되니 낮춰도 된다.
HOP_MS = 100
MAX_LAG_S = 2.0  # 청크 정렬을 찾을 때 훑는 최대 지연
MAX_SHIFT_S = 3.0  # 골격 시각을 밀어보는 최대 폭
DURATION_TOL_S = 0.5  # 영상 길이와 오디오 길이의 허용 차이
LAG_TOL_S = 0.1  # 청크가 어긋나도 되는 최대 지연
CORR_FLOOR = 0.95  # 청크 ↔ 원본 포락선 상관 하한
SPEECH_LEVEL = 0.06  # 소리 있음으로 볼 정규화 진폭


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    passed: bool
    detail: str


def envelope(path: Path, start: float = 0.0, length: float | None = None) -> list[float]:
    """100ms 구간별 최대 진폭. ffmpeg로 디코딩만 하고 계산은 여기서 한다."""
    command = [require_ffmpeg(), "-v", "error", "-nostdin"]
    if start:
        command += ["-ss", f"{start:.6f}"]
    command += ["-i", str(path)]
    if length is not None:
        command += ["-t", f"{length:.6f}"]
    command += ["-f", "s16le", "-ac", "1", "-ar", str(PROBE_RATE), "-"]
    samples = array.array("h")
    samples.frombytes(subprocess.run(command, capture_output=True, check=True).stdout)
    hop = PROBE_RATE * HOP_MS // 1000
    return [
        max(abs(x) for x in samples[i : i + hop]) / 32768
        for i in range(0, len(samples) - hop + 1, hop)
    ]


def correlation(left: list[float], right: list[float]) -> float:
    n = min(len(left), len(right))
    if n < 2:
        return 0.0
    mean_l = sum(left[:n]) / n
    mean_r = sum(right[:n]) / n
    num = sum((left[i] - mean_l) * (right[i] - mean_r) for i in range(n))
    den = (
        sum((left[i] - mean_l) ** 2 for i in range(n))
        * sum((right[i] - mean_r) ** 2 for i in range(n))
    ) ** 0.5
    return num / den if den else 0.0


def best_lag(chunk_env: list[float], full_env: list[float]) -> tuple[float, float]:
    """포락선을 밀어가며 가장 잘 맞는 지연을 찾는다. 0이어야 정상이다."""
    span = int(MAX_LAG_S * 1000 / HOP_MS)
    scored = [
        (correlation(chunk_env, full_env[max(0, lag) :][: len(chunk_env)]), lag)
        for lag in range(-span, span + 1)
    ]
    corr, lag = max(scored)
    return lag * HOP_MS / 1000, corr


# ── 개별 검사 ────────────────────────────────────────────────


def check_durations(video: Path | None, audio: Path, meta: dict) -> Check:
    audio_seconds = probe_duration(audio)
    recorded = float(meta.get("duration", 0.0))
    parts = [f"오디오 {audio_seconds:.3f}초", f"녹화 당시 {recorded:.3f}초"]
    gaps = [abs(audio_seconds - recorded)]
    if video and video.is_file():
        video_seconds = probe_duration(video)
        parts.insert(0, f"영상 {video_seconds:.3f}초")
        gaps.append(abs(video_seconds - audio_seconds))
    worst = max(gaps)
    return Check(
        "길이 보존 (영상 ↔ 오디오)",
        worst <= DURATION_TOL_S,
        f"{' / '.join(parts)}  최대 차이 {worst:.3f}초 (허용 {DURATION_TOL_S})",
    )


def check_chunks(audio: Path, chunk_paths: list[Path], offsets: list[float]) -> Check:
    """각 청크가 전체 오디오의 자기 오프셋 위치와 같은 소리인지 본다."""
    if not chunk_paths:
        return Check("청크 정렬", True, "청크 파일이 없다 (단일 청크). 검사 생략")
    lines: list[str] = []
    passed = True
    probe = 60.0
    for path, offset in zip(chunk_paths, offsets, strict=True):
        lag, corr = best_lag(envelope(path, 0.0, probe), envelope(audio, offset, probe))
        ok = abs(lag) <= LAG_TOL_S and corr >= CORR_FLOOR
        passed = passed and ok
        lines.append(
            f"  {path.name}  오프셋 {offset:9.3f}초  길이 {probe_duration(path):9.3f}초"
            f"  지연 {lag:+.1f}초  상관 {corr:.4f}  {'OK' if ok else 'FAIL'}"
        )
    return Check("청크 정렬 (조각 ↔ 원본 오디오)", passed, "\n" + "\n".join(lines))


def check_spine_shift(audio: Path, spine: list) -> Check:
    """골격 시각을 앞뒤로 밀어보며 실제 소리와 가장 잘 맞는 지점을 찾는다."""
    full = envelope(audio)
    loud = [level > SPEECH_LEVEL for level in full]
    span = int(MAX_SHIFT_S * 1000 / HOP_MS)
    scored = [(_speech_f1(spine, loud, shift), shift) for shift in range(-span, span + 1, 2)]
    score, shift = max(scored)
    seconds = shift * HOP_MS / 1000
    zero = _speech_f1(spine, loud, 0)
    return Check(
        "골격 시각 ↔ 실제 소리",
        abs(seconds) <= LAG_TOL_S,
        f"최적 시프트 {seconds:+.1f}초 (F1 {score:.3f}), 시프트 0 F1 {zero:.3f}",
    )


def _speech_f1(spine: list, loud: list[bool], shift: int) -> float:
    mask = [False] * len(loud)
    for segment in spine:
        lo = max(0, int(segment.start * 1000 / HOP_MS) + shift)
        hi = min(len(mask), int(segment.end * 1000 / HOP_MS) + shift)
        for k in range(lo, hi):
            mask[k] = True
    hit = sum(1 for k in range(len(mask)) if mask[k] and loud[k])
    miss = sum(1 for k in range(len(mask)) if mask[k] != loud[k])
    return hit / (hit + miss / 2) if hit else 0.0


def check_srt_coverage(srt: Path, duration: float) -> Check:
    if not srt.is_file():
        return Check("SRT 시간 커버리지", True, f"{srt} 없음. 검사 생략")
    stamps = re.findall(
        r"(\d+):(\d+):(\d+),(\d+) --> (\d+):(\d+):(\d+),(\d+)", srt.read_text("utf-8")
    )
    spans = [
        (
            int(a) * 3600 + int(b) * 60 + int(c) + int(d) / 1000,
            int(e) * 3600 + int(f) * 60 + int(g) + int(h) / 1000,
        )
        for a, b, c, d, e, f, g, h in stamps
    ]
    covered = sum(end - start for start, end in spans)
    gaps = sorted((spans[i + 1][0] - spans[i][1], spans[i][1]) for i in range(len(spans) - 1))[
        ::-1
    ][:3]
    detail = (
        f"큐 {len(spans)}개 · {covered:.0f}초 / {duration:.0f}초 = {covered / duration * 100:.1f}%"
    )
    if gaps:
        detail += "  최대 공백 " + ", ".join(f"{g:.0f}초@{t:.0f}s" for g, t in gaps)
    return Check("SRT 시간 커버리지", covered / duration >= 0.5, detail)


# ── 조립 ─────────────────────────────────────────────────────


def locate(stem: str) -> tuple[Path, list[Path], Path | None]:
    caches = sorted((ROOT / ".vlogsrt-cache").glob(f"{stem}-*"))
    if not caches:
        raise SystemExit(
            f"전처리 캐시가 없습니다: .vlogsrt-cache/{stem}-*\n"
            f"`vlogsrt run`을 한 번 돌려 오디오를 만들어야 검증할 수 있습니다."
        )
    cache = caches[-1]
    audio = cache / f"{stem}.mp3"
    if not audio.is_file():
        raise SystemExit(f"오디오가 없습니다: {audio}")
    chunks = sorted(cache.glob(f"{stem}.chunk*.mp3"))
    trace = ROOT / "out" / stem / "trace.json"
    video = None
    if trace.is_file():
        source = json.loads(trace.read_text("utf-8")).get("source")
        video = Path(source) if source else None
    return audio, chunks, video


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        raise SystemExit("사용법: python scripts/verify_timing.py <stem>")
    stem = Path(argv[0]).stem
    fixtures = ROOT / "tests" / "fixtures" / "api"
    meta = json.loads((fixtures / f"{stem}.meta.json").read_text("utf-8"))
    audio, chunk_paths, video = locate(stem)

    responses = load_fixture_responses(fixtures / f"{stem}.{FIXTURE_KIND}.json")
    spine = build_spine(responses)
    duration = float(meta["duration"])

    checks = [
        check_durations(video, audio, meta),
        check_chunks(
            audio, chunk_paths, [r.offset for r in sorted(responses, key=lambda x: x.index)]
        ),
        check_spine_shift(audio, spine),
        check_srt_coverage(ROOT / "out" / stem / "output.srt", duration),
    ]

    print(f"\n=== {stem} · {duration:.0f}초 · 청크 {meta.get('chunk_count')}개 ===\n")
    for check in checks:
        print(f"[{'OK  ' if check.passed else 'FAIL'}] {check.name}: {check.detail}")
    print()
    return 0 if all(check.passed for check in checks) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
