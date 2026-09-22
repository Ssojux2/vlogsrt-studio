#!/usr/bin/env python3
"""영상 → 오디오 → 청크 → 타임스탬프 사슬을 실제 파일로 대조한다.

"자막이 소리와 안 맞는다"는 신고가 들어왔을 때, 원인이 **절단**인지 **전사**인지
기계적으로 가른다. 사람이 귀로 듣지 않아도 답이 나와야 한다.

API를 부르지 않는다. 이미 녹화된 픽스처(`tests/fixtures/api/`)와 전처리 캐시
(`.vlogsrt-cache/`), 그리고 실행이 남긴 `trace.json`만 읽는다.

    python scripts/verify_timing.py fr_day_1_2_fhd [--out out]

**건너뛴 검사는 `OK`가 아니라 `SKIP`으로 찍는다.** 예전에는 조각 파일 이름이 어긋나
아무것도 못 찾은 상태가 통과로 보였고, 그래서 이 도구가 반쯤 죽은 채로 오래 돌았다(D32).

여기 있는 허용오차는 **검증 도구의 판정 기준**이지 파이프라인 임계값이 아니다.
파이프라인 임계값은 전부 `vlogsrt/config.py`에 있다(불변 규칙 8).
"""

from __future__ import annotations

import argparse
import array
import json
import os
import re
import statistics
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# 골격 해석 중 나오는 세그먼트별 경고는 여기서 요약으로 대신 센다.
os.environ.setdefault("VLOGSRT_LOG_LEVEL", "ERROR")

from vlogsrt.models import ChunkMark, Segment, Trace  # noqa: E402
from vlogsrt.preprocess import chunk_glob, probe_duration, require_ffmpeg  # noqa: E402
from vlogsrt.transcribe import (  # noqa: E402
    FIXTURE_KIND,
    ChunkResponse,
    build_spine,
    load_fixture_responses,
)

# ── 검증 허용오차 ────────────────────────────────────────────
PROBE_RATE = 4000  # 포락선 계산용 디코딩 주파수. 에너지만 보면 되니 낮춰도 된다.
HOP_MS = 100
MAX_LAG_S = 2.0  # 청크 정렬을 찾을 때 훑는 최대 지연
MAX_SHIFT_S = 3.0  # 골격 시각을 밀어보는 최대 폭
DURATION_TOL_S = 0.5  # 영상 길이와 오디오 길이의 허용 차이
LAG_TOL_S = 0.1  # 청크가 어긋나도 되는 최대 지연
CORR_FLOOR = 0.95  # 청크 ↔ 원본 포락선 상관 하한
SPEECH_LEVEL = 0.06  # 소리 있음으로 볼 정규화 진폭
WINDOW_S = 30.0  # 조각을 들여다보는 창. **머리·가운데·꼬리 셋을 본다**

# ── 시각 붙음(glue) 판정 ─────────────────────────────────────
# whisper가 시각을 **재지 않고 이어 붙인** 비율. `다음.start == 앞.end`가 그 지문이다.
# 조각 뒤로 갈수록 오르면 "조각 끝에서 싱크가 무너지고 다음 조각에서 회복된다"는
# 신고의 기전이다 — 조각 경계가 whisper의 창 위치를 0으로 되돌리기 때문이다(D32).
GLUE_BANDS = 10
GLUE_EDGE = 0.3  # 머리/꼬리로 볼 조각 안 비율
# 실측 근거(골격 기준 — 이 검사가 실제로 세는 단위다): 녹화 픽스처에서 조각 1이
# 머리 0.036 → 꼬리 0.486으로 올랐고(상승 0.449), 조각 0은 0.185 → 0.385(상승 0.200)였다.
# 0.25가 둘 사이에 깨끗이 앉는다. **응답 세그먼트를 통째로 세면 다른 수가 나온다** —
# 글자 없는 세그먼트가 골격에서 빠지면서 사슬이 끊기기 때문이다(원본 기준은 0.00 → 0.92).
GLUE_RISE_TOL = 0.25
GLUE_MIN_SEGMENTS = 10  # 이보다 적은 조각은 비율이 우연에 흔들린다

# ── 조판 지연(lag) 판정 ──────────────────────────────────────
# `refine`이 큐 시작을 세그먼트보다 **뒤로** 미는 비율. 위 검사들이 전부 골격만 보는 탓에
# 조판 계층이 시각을 미는 결함은 이 게이트에 한 번도 잡히지 않았다(D33). `lead_in`만
# 도는 정상 큐는 음수여야 하고, `lead_out`이 없는 무음을 만들어 이웃을 밀면 양수로 뜬다.
# 실측 근거: 77분 픽스처에서 고치기 전 27.5%(355/1291) → 고친 뒤 1.1%(14/1291).
# 0.05가 둘 사이에 깨끗이 앉는다. 남는 1.1%는 whisper 원본이 겹친 구간의 충돌 해소다.
LAG_LATE_TOL_S = 0.02  # 이보다 늦으면 "늦었다"로 센다. 반올림 잡음 여유
LAG_LATE_RATE_TOL = 0.05


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    passed: bool
    detail: str
    skipped: bool = False

    @property
    def label(self) -> str:
        if self.skipped:
            return "SKIP"
        return "OK  " if self.passed else "FAIL"


def skip(name: str, why: str) -> Check:
    """**건너뛴 것은 통과가 아니다.** 전체 판정은 막지 않되 눈에는 띄어야 한다."""
    return Check(name, passed=True, detail=why, skipped=True)


# ── 포락선 ───────────────────────────────────────────────────


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


def best_lag(probe: list[float], reference: list[float], pad_bins: int) -> tuple[float, float]:
    """`reference`는 앞으로 `pad_bins`만큼 더 떠 온 포락선이다. 지연 0이어야 정상이다.

    예전에는 음의 지연을 훑지 못했다(`full_env[max(0, lag):]`가 음수를 전부 0으로
    눌렀다). 자막이 **빠른** 쪽으로 어긋난 경우를 못 보는 자였다.

    그리고 **모든 지연이 같은 길이를 비교해야 한다.** `correlation`이 짧은 쪽에 맞춰
    자르므로, 참조가 모자라는 지연은 더 적은 데이터로 더 높은 점수를 받는다 — 조각이
    오디오 끝에 걸릴 때마다 그 지연이 이길 수 있었다. 점수를 길이가 정하게 두면 안 된다.
    """
    span = min(len(probe), len(reference) - 2 * pad_bins)
    if span < 2:
        return 0.0, 0.0
    head = probe[:span]
    scored = [
        (correlation(head, reference[pad_bins + lag : pad_bins + lag + span]), lag)
        for lag in range(-pad_bins, pad_bins + 1)
    ]
    corr, lag = max(scored)
    return lag * HOP_MS / 1000, corr


# ── 시각 붙음(glue) — trace.json만 읽는 순수 계산 ────────────


@dataclass(frozen=True, slots=True)
class GlueBand:
    """조각 안 위치 한 구간의 통계. `low`~`high`는 조각 길이 대비 비율이다."""

    low: float
    high: float
    segments: int
    glued: int
    no_speech: float

    @property
    def rate(self) -> float:
        return self.glued / self.segments if self.segments else 0.0


@dataclass(frozen=True, slots=True)
class ChunkGlue:
    """조각 하나의 머리 대 꼬리. `rise`가 조각 안 열화의 크기다."""

    index: int
    segments: int
    head: float
    tail: float

    @property
    def rise(self) -> float:
        return self.tail - self.head


def _glued_flags(spine: Sequence[Segment], mark: ChunkMark) -> list[tuple[float, bool, float]]:
    """조각 하나에 속한 세그먼트의 (조각 안 위치, 붙었는가, no_speech_prob).

    **붙음은 같은 조각 안에서만 센다.** 조각 경계를 넘어 이으면 경계 자체가 붙음으로
    잡혀 우리가 찾는 신호를 가린다.
    """
    inside = [seg for seg in spine if mark.offset <= seg.start < mark.end]
    if mark.duration <= 0:
        return []
    out: list[tuple[float, bool, float]] = []
    previous: float | None = None
    for segment in inside:
        glued = previous is not None and abs(segment.start - previous) < 1e-6
        out.append(((segment.start - mark.offset) / mark.duration, glued, segment.no_speech_prob))
        previous = segment.end
    return out


def marks_from_offsets(offsets: Sequence[float], duration: float) -> list[ChunkMark]:
    """오프셋만으로 조각 경계를 되짚는다. **추정이 아니다.**

    조각은 언제나 `[0, duration]`을 빈틈없이 덮으므로(규칙 4) 한 조각의 끝은 다음
    조각의 시작이고, 마지막 조각의 끝은 영상 끝이다. `trace.json`에 경계가 없는
    옛 실행이나 메타 픽스처만 쓴 `--offline` 실행을 위한 길이다.
    """
    ordered = sorted(offsets)
    if not ordered or duration <= 0:
        return []
    ends = [*ordered[1:], duration]
    return [
        ChunkMark(index=index, offset=offset, duration=end - offset)
        for index, (offset, end) in enumerate(zip(ordered, ends, strict=True))
        if end > offset
    ]


def glue_profile(
    spine: Sequence[Segment], marks: Sequence[ChunkMark], bands: int = GLUE_BANDS
) -> list[GlueBand]:
    """조각 안 위치 십분위별로 모든 조각을 합산한다."""
    buckets: list[list[tuple[bool, float]]] = [[] for _ in range(bands)]
    for mark in marks:
        for position, glued, no_speech in _glued_flags(spine, mark):
            buckets[min(bands - 1, int(position * bands))].append((glued, no_speech))
    return [
        GlueBand(
            low=index / bands,
            high=(index + 1) / bands,
            segments=len(rows),
            glued=sum(1 for glued, _ in rows if glued),
            no_speech=statistics.median([nsp for _, nsp in rows]) if rows else 0.0,
        )
        for index, rows in enumerate(buckets)
    ]


def glue_rises(spine: Sequence[Segment], marks: Sequence[ChunkMark]) -> list[ChunkGlue]:
    """조각마다 머리 30% 대 꼬리 30%의 붙음 비율."""
    out: list[ChunkGlue] = []
    for mark in marks:
        rows = _glued_flags(spine, mark)
        if len(rows) < GLUE_MIN_SEGMENTS:
            continue
        head = [glued for position, glued, _ in rows if position < GLUE_EDGE]
        tail = [glued for position, glued, _ in rows if position > 1.0 - GLUE_EDGE]
        if not head or not tail:
            continue
        out.append(
            ChunkGlue(
                index=mark.index,
                segments=len(rows),
                head=sum(head) / len(head),
                tail=sum(tail) / len(tail),
            )
        )
    return out


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


def _windows(chunk_seconds: float) -> list[tuple[str, float]]:
    """조각을 들여다볼 자리. **꼬리를 반드시 본다** — 신고가 나는 곳이 거기다."""
    if chunk_seconds <= WINDOW_S:
        return [("전체", 0.0)]
    return [
        ("머리", 0.0),
        ("가운데", (chunk_seconds - WINDOW_S) / 2),
        ("꼬리", chunk_seconds - WINDOW_S),
    ]


def _probe_window(
    audio: Path, audio_seconds: float, chunk: Path, offset: float, at: float, span: float
) -> tuple[float, float] | None:
    """창 하나의 (지연, 상관). 훑을 여유가 없으면 `None` — 짐작을 판정으로 쓰지 않는다.

    **탐색 범위는 양쪽 다 실제 오디오 안에 있어야 한다.** 마지막 조각은 오디오 끝에
    걸려서 양의 지연이 파일 밖을 보는데, 그러면 없는 소리와 비교해 놓고 그쪽이 더
    맞는다고 답한다. 대칭으로 잡아야 답이 자의 형편이 아니라 오디오에서 나온다(D32).
    """
    # 오디오 끝에 걸리면 창을 줄여서라도 뒤쪽 여유를 만든다. 창을 통째로 버리는 것보다 낫다.
    span = min(span, audio_seconds - (offset + at) - MAX_LAG_S)
    room = min(MAX_LAG_S, offset + at, audio_seconds - (offset + at + span))
    if room * 1000 < HOP_MS or span < WINDOW_S / 4:
        return None
    reference = envelope(audio, offset + at - room, span + 2 * room)
    return best_lag(envelope(chunk, at, span), reference, int(room * 1000 / HOP_MS))


def check_chunks(
    audio: Path, chunk_paths: list[Path], offsets: list[float], audio_seconds: float
) -> Check:
    """각 청크가 전체 오디오의 자기 오프셋 위치와 같은 소리인지, **창 셋으로** 본다."""
    name = "청크 정렬 (조각 ↔ 원본 오디오)"
    if not chunk_paths:
        return skip(
            name, "조각 mp3가 없다. `vlogsrt run`을 돌려 .vlogsrt-cache/ 를 남겨야 검사한다"
        )
    if len(chunk_paths) != len(offsets):
        return skip(
            name,
            f"조각 파일 {len(chunk_paths)}개와 응답 오프셋 {len(offsets)}개가 다르다. "
            f"캐시와 픽스처가 같은 실행의 것이 아니다",
        )
    lines: list[str] = []
    passed = True
    for path, offset in zip(chunk_paths, offsets, strict=True):
        seconds = probe_duration(path)
        for where, at in _windows(seconds):
            span = min(WINDOW_S, seconds - at)
            probed = _probe_window(audio, audio_seconds, path, offset, at, span)
            if probed is None:
                lines.append(f"  {path.name} {where:>3}  훑을 여유가 없다 (SKIP)")
                continue
            lag, corr = probed
            ok = abs(lag) <= LAG_TOL_S and corr >= CORR_FLOOR
            passed = passed and ok
            lines.append(
                f"  {path.name} {where:>3}  오프셋 {offset + at:9.3f}초"
                f"  지연 {lag:+.1f}초  상관 {corr:.4f}  {'OK' if ok else 'FAIL'}"
            )
    return Check(name, passed, "\n" + "\n".join(lines))


def check_spine_shift(audio: Path, spine: list[Segment]) -> Check:
    """골격 시각을 앞뒤로 밀어보며 실제 소리와 가장 잘 맞는 지점을 찾는다.

    **전역 시프트 하나만 본다.** 오프셋이 통째로 어긋나는 버그는 여기서 드러나지만,
    조각 안에서 뒤로 갈수록 어긋나는 종류는 원리적으로 못 본다 — 그건 아래
    `check_glue`가 맡는다(D32).
    """
    full = envelope(audio)
    loud = [level > SPEECH_LEVEL for level in full]
    span = int(MAX_SHIFT_S * 1000 / HOP_MS)
    scored = [(_speech_f1(spine, loud, shift), shift) for shift in range(-span, span + 1, 2)]
    score, shift = max(scored)
    seconds = shift * HOP_MS / 1000
    zero = _speech_f1(spine, loud, 0)
    return Check(
        "골격 시각 ↔ 실제 소리 (전역)",
        abs(seconds) <= LAG_TOL_S,
        f"최적 시프트 {seconds:+.1f}초 (F1 {score:.3f}), 시프트 0 F1 {zero:.3f}",
    )


def _speech_f1(spine: Sequence[Segment], loud: list[bool], shift: int) -> float:
    mask = [False] * len(loud)
    for segment in spine:
        lo = max(0, int(segment.start * 1000 / HOP_MS) + shift)
        hi = min(len(mask), int(segment.end * 1000 / HOP_MS) + shift)
        for k in range(lo, hi):
            mask[k] = True
    hit = sum(1 for k in range(len(mask)) if mask[k] and loud[k])
    miss = sum(1 for k in range(len(mask)) if mask[k] != loud[k])
    return hit / (hit + miss / 2) if hit else 0.0


def check_glue(spine: list[Segment], marks: Sequence[ChunkMark], source: str = "trace") -> Check:
    """조각 안에서 whisper가 시각을 **재기를 그만두는지** 본다. 오디오를 안 읽는다.

    이 검사만이 "조각 끝에서 무너지고 다음 조각에서 회복"을 직접 잰다.
    포락선 자는 이 콘텐츠에서 너무 무디다 — 실측에서 onset 일치율이 27~38%였고
    최적 시프트가 −2.0~+1.8초로 흩어졌다(D32).
    """
    if not marks:
        return skip(
            "시각 붙음 (조각 안 위치별)",
            "조각 경계를 알 수 없다. trace.json도 응답 오프셋도 없다",
        )
    bands = [band for band in glue_profile(spine, marks) if band.segments]
    if not bands:
        return skip("시각 붙음 (조각 안 위치별)", "조각 안에 든 세그먼트가 없다")
    lines = [f"  조각 {len(marks)}개 (경계 출처: {source})"]
    lines += [
        f"  {band.low * 100:3.0f}-{band.high * 100:3.0f}%  세그먼트 {band.segments:5d}"
        f"  붙음 {band.rate * 100:5.1f}%  no_speech 중앙 {band.no_speech:.3f}"
        for band in bands
    ]
    rises = glue_rises(spine, marks)
    bad = [rise for rise in rises if rise.rise > GLUE_RISE_TOL]
    lines.append(
        f"  조각 {len(rises)}개 중 머리→꼬리 상승이 {GLUE_RISE_TOL:.2f}를 넘는 조각: {len(bad)}개"
    )
    lines += [
        f"    조각 {rise.index}: 머리 {rise.head * 100:.0f}% → 꼬리 {rise.tail * 100:.0f}%"
        f" (상승 {rise.rise:+.2f})"
        for rise in sorted(bad, key=lambda r: -r.rise)[:10]
    ]
    return Check("시각 붙음 (조각 안 위치별)", not bad, "\n" + "\n".join(lines))


def check_cue_lag(trace: Trace | None) -> Check:
    """조판이 큐 시작을 세그먼트보다 **뒤로** 미는 비율. 오디오를 안 읽는다.

    한 세그먼트를 여러 큐가 나눠 가질 때는 **첫 큐만** 센다. 분할된 뒤쪽 조각은
    원래 늦는 게 맞아서, 같이 세면 결함이 정상에 묻힌다.
    """
    name = "조판 지연 (큐 시작 − 세그먼트 시작)"
    if trace is None or not trace.cues:
        return skip(name, "trace.json에 큐가 없다. `vlogsrt run --out`을 돌려야 한다")
    starts = {segment.id: segment.start for segment in trace.spine}
    seen: set[int] = set()
    lags: list[float] = []
    for cue in trace.cues:
        head = cue.seg_ids[0] if cue.seg_ids else None
        if head is None or head in seen or head not in starts:
            continue
        seen.add(head)
        lags.append(cue.start - starts[head])
    if not lags:
        return skip(name, "골격을 역추적할 수 있는 큐가 없다")
    late = [lag for lag in lags if lag > LAG_LATE_TOL_S]
    rate = len(late) / len(lags)
    detail = (
        f"큐 {len(lags)}개 · 중앙 {statistics.median(lags):+.3f}초 · "
        f"늦은 큐 {len(late)}개({rate * 100:.1f}%) · 최대 {max(lags):+.3f}초"
    )
    return Check(name, rate <= LAG_LATE_RATE_TOL, detail)


def check_srt_coverage(srt: Path, duration: float) -> Check:
    if not srt.is_file():
        return skip("SRT 시간 커버리지", f"{srt} 없음")
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
    if not spans:
        return skip("SRT 시간 커버리지", f"{srt}에 타임코드가 없다")
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


@dataclass(frozen=True, slots=True)
class Located:
    audio: Path
    chunks: list[Path]
    video: Path | None
    trace: Trace | None


def load_trace(outdir: Path) -> Trace | None:
    """`vlogsrt run --out <outdir>`이 남긴 기록. 없으면 없는 대로 간다."""
    path = outdir / "trace.json"
    if not path.is_file():
        return None
    try:
        return Trace.load(path)
    except RuntimeError as exc:
        print(f"  (trace를 못 읽었다: {exc})")
        return None


def locate(stem: str, outdir: Path) -> Located:
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
    # **이름은 preprocess가 정한다.** 여기서 따로 지으면 또 어긋난다(D32).
    chunks = sorted(cache.glob(chunk_glob(stem)))
    trace = load_trace(outdir)
    video = Path(trace.source) if trace and trace.source else None
    return Located(audio=audio, chunks=chunks, video=video, trace=trace)


def _boundaries(
    trace: Trace | None, responses: Sequence[ChunkResponse], duration: float
) -> tuple[Sequence[ChunkMark], str]:
    """조각 경계와 그 출처. trace가 들고 있으면 그게 옳다."""
    if trace and trace.chunks:
        return trace.chunks, "trace.json"
    return marks_from_offsets([response.offset for response in responses], duration), "응답 오프셋"


def _fixture_responses(path: Path) -> Sequence[ChunkResponse]:
    """녹화된 1차 응답 — 실제 유료 실행에는 없다. 없으면 빈 채로 간다.

    예전에는 없으면 그 자리에서 죽어서, 픽스처를 녹화한 회차에만 이 게이트가 돌았다.
    trace가 있으면 골격도 조각 경계도 trace가 더 옳다(재전사가 반영된 최종본이다).
    """
    if not path.exists():
        return ()
    return load_fixture_responses(path)


def _run_checks(found: Located, stem: str, meta: dict, outdir: Path) -> list[Check]:
    fixtures = ROOT / "tests" / "fixtures" / "api"
    responses = _fixture_responses(fixtures / f"{stem}.{FIXTURE_KIND}.json")
    # 골격은 trace가 있으면 그쪽이 옳다 — 재전사가 반영된 최종본이기 때문이다.
    if found.trace is None and not responses:
        raise SystemExit(
            f"{stem}의 골격을 알 수 없다 — trace.json 도 녹화된 응답도 없다.\n"
            "건너뛰는 것은 통과가 아니다."
        )
    spine = list(found.trace.spine) if found.trace else build_spine(responses)
    duration = float(meta["duration"])
    marks, source = _boundaries(found.trace, responses, duration)
    # 조각 정렬도 같은 출처를 쓴다 — trace의 경계가 실제로 자른 자리다.
    offsets = [mark.offset for mark in sorted(marks, key=lambda mark: mark.index)]
    return [
        check_durations(found.video, found.audio, meta),
        check_chunks(found.audio, found.chunks, offsets, probe_duration(found.audio)),
        check_spine_shift(found.audio, spine),
        check_glue(spine, marks, source),
        check_cue_lag(found.trace),
        check_srt_coverage(outdir / "output.srt", duration),
    ]


def _episode_meta(stem: str, outdir: Path) -> tuple[dict, str]:
    """회차 길이와 조각 수 — 픽스처 meta가 정본이고, 없으면 실제 실행의 trace다.

    예전에는 `tests/fixtures/api/<stem>.meta.json`만 봤다. 그래서 **녹화된 픽스처가
    있는 회차에만** 이 게이트가 돌았고, 실제 유료 실행의 산출물(`out/<stem>/trace.json`)
    에는 한 번도 돌지 않았다 — 프로젝트의 유일한 자동 시각 게이트가 정작 실물을
    못 본 셈이다.

    둘 다 없으면 **조용히 건너뛰지 않고 죽는다**(D32: SKIP은 통과가 아니다).
    길이 출처는 화면에 찍어서, trace에서 온 값을 픽스처 검증으로 오해하지 않게 한다.
    """
    fixture = ROOT / "tests" / "fixtures" / "api" / f"{stem}.meta.json"
    if fixture.exists():
        return json.loads(fixture.read_text("utf-8")), "픽스처 meta"
    trace = load_trace(outdir)
    if trace is None:
        raise SystemExit(
            f"{stem}의 길이를 알 수 없다 — {fixture} 도 {outdir / 'trace.json'} 도 없다.\n"
            "먼저 `vlogsrt run`을 돌리거나 픽스처를 녹화해라. 건너뛰는 것은 통과가 아니다."
        )
    return {"duration": trace.duration, "chunk_count": len(trace.chunks)}, "trace.json"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="자막 시각을 실제 파일로 대조한다")
    parser.add_argument("stem", help="회차 이름 (예: fr_day_1_2_fhd)")
    parser.add_argument("--out", type=Path, default=Path("out"), help="vlogsrt run 의 출력 폴더")
    args = parser.parse_args(argv)

    stem = Path(args.stem).stem
    outdir = args.out if args.out.is_absolute() else ROOT / args.out
    meta, origin = _episode_meta(stem, outdir)
    checks = _run_checks(locate(stem, outdir), stem, meta, outdir)

    print(
        f"\n=== {stem} · {float(meta['duration']):.0f}초 · 청크 {meta.get('chunk_count')}개"
        f" · 길이 출처 {origin} ===\n"
    )
    for check in checks:
        print(f"[{check.label}] {check.name}: {check.detail}")
    skipped = [check.name for check in checks if check.skipped]
    if skipped:
        print(f"\n건너뛴 검사 {len(skipped)}개: {', '.join(skipped)}. **통과가 아니다.**")
    print()
    return 0 if all(check.passed for check in checks) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
