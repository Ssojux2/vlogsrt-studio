"""자막 시각을 **실제 발화**와 대조한다 — 모델의 말이 아니라 소리를 기준으로.

`scripts/verify_timing.py`는 원시 진폭 포락선을 쓴다. 그래서 음악·잡음·숨소리를
발화와 구분하지 못하고, "여기 말이 있었나"를 물을 수 없다. 이 도구는 저장소에
이미 핀으로 박힌 **Silero VAD v6.2.1**(`vlogsrt/project/vad/`)을 그대로 불러서
세 가지를 잰다.

1. **큐 시작 오차** — 큐가 시작하는 자리에 실제로 말이 시작하나.
2. **말은 있는데 자막이 없다** — 규칙 9가 깨진 자리. 공백이 진짜 손실인지 증명한다.
3. **자막은 있는데 말이 없다** — 환각 탐지. 정답 SRT도 API도 없이 기계로 잡는다.

**측정 전용이다.** 파이프라인에 들어가지 않고 출력을 못 바꾼다(규칙 5). D24가
`silencedetect`로 구간을 *고르다가* 앞뒤 3~6분을 날린 것과는 다른 자리다 —
여기서 VAD는 아무것도 고르지 않고 재기만 한다.

MFCC/mel-spectrogram을 손으로 짜지 않은 이유: 62분 × 16kHz를 순수 파이썬 FFT로
돌리는 비용도 문제지만, 결국 더 나쁜 VAD를 재발명하는 셈이다. 컷 계층이 쓰는
것과 **같은 검출기**를 쓰면 두 계층이 "발화"를 같은 뜻으로 쓴다.

    python scripts/speech_onsets.py <media> --out out/<stem>
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from vlogsrt.models import Trace  # noqa: E402
from vlogsrt.project.config import ProjectConfig  # noqa: E402
from vlogsrt.project.pcm import AudioPolicy, PcmReference, build_reference  # noqa: E402
from vlogsrt.project.vad.inference import VadResult, infer_vad  # noqa: E402
from vlogsrt.project.vad.runtime import bundled_artifact, create_cpu_session  # noqa: E402

# 이 도구만의 판정 기준이다. 파이프라인 임계값이 아니다 — 여기서 고쳐도 자막은
# 한 바이트도 바뀌지 않는다.
MIN_SPEECH_RUN = 0.20  # 이보다 짧은 양성은 잡음으로 본다
MERGE_GAP = 0.30  # 이보다 가까운 발화 두 덩이는 한 덩이로 본다
REPORT_GAP = 5.0  # 이보다 긴 "말 있는데 자막 없음"만 보고한다
PHANTOM_OVERLAP = 0.10  # 큐가 발화와 이만큼도 안 겹치면 환각 후보


@dataclass(frozen=True)
class Span:
    start: float
    end: float

    @property
    def length(self) -> float:
        return self.end - self.start


def speech_spans(vad: VadResult, rate: int) -> list[Span]:
    """확률 창을 발화 구간으로 — 채널 합집합. 가드와 같은 방향으로 보수적이다."""
    cfg = ProjectConfig()
    positive = [w for w in vad.windows if w.probability >= cfg.vad_positive]
    raw = sorted((w.start_sample / rate, w.end_sample / rate) for w in positive)
    merged: list[list[float]] = []
    for start, end in raw:
        if merged and start - merged[-1][1] <= MERGE_GAP:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [Span(a, b) for a, b in merged if b - a >= MIN_SPEECH_RUN]


def overlap(a: Span, b: Span) -> float:
    return max(0.0, min(a.end, b.end) - max(a.start, b.start))


def covered_seconds(probe: Span, spans: list[Span]) -> float:
    return sum(overlap(probe, span) for span in spans)


def complement(spans: list[Span], duration: float) -> list[Span]:
    """발화의 여집합 — 조용한 자리."""
    holes: list[Span] = []
    cursor = 0.0
    for span in spans:
        if span.start > cursor:
            holes.append(Span(cursor, span.start))
        cursor = max(cursor, span.end)
    if cursor < duration:
        holes.append(Span(cursor, duration))
    return holes


def inside(value: float, spans: list[Span]) -> bool:
    return any(span.start <= value < span.end for span in spans)


def onset_error(start: float, spans: list[Span]) -> float | None:
    """발화가 **새로 시작하는** 큐만 잰다. 양수면 큐가 늦다.

    말 한가운데서 시작하는 큐를 "가장 가까운 발화 시작"과 비교하면 안 된다 —
    60초짜리 발화 한 덩이 안의 큐는 어느 시작점과도 멀고, 그건 어긋남이 아니라
    지표의 결함이다. 첫 판에서 중앙 1.88초가 나온 이유가 이것이었다.

    그래서 두 갈래로 나눈다. 큐가 발화 안에서 시작하면 정렬 문제가 아니다(None).
    조용한 자리에서 시작하면 **다음** 발화까지의 거리를 잰다.
    """
    if inside(start, spans):
        return None
    following = [span for span in spans if span.start >= start]
    return None if not following else start - following[0].start


def best_shift(cue_starts: list[float], spans: list[Span]) -> tuple[float, float]:
    """전역 시프트 후보 중 '발화 안에서 시작하는 큐' 비율이 가장 높은 것."""
    best = (0.0, _inside_share(cue_starts, spans, 0.0))
    for index in range(-50, 51):
        shift = index * 0.02
        score = _inside_share(cue_starts, spans, shift)
        if score > best[1]:
            best = (shift, score)
    return best


def _inside_share(cue_starts: list[float], spans: list[Span], shift: float) -> float:
    if not cue_starts:
        return 0.0
    return sum(inside(start + shift, spans) for start in cue_starts) / len(cue_starts)


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]


def _clock(seconds: float) -> str:
    return f"{int(seconds // 60):02d}:{seconds % 60:05.2f}"


def build_pcm(media: Path, scratch: Path) -> PcmReference:
    digest = hashlib.sha256()
    with media.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return build_reference(
        source_id=f"onsets:{media.stem}",
        original_stem=media.stem,
        original=media,
        original_sha256=digest.hexdigest(),
        policy=AudioPolicy(),
        destination=scratch / media.stem,
    )


def report(trace: Trace, spans: list[Span], reference: PcmReference) -> int:
    duration = reference.sample_count / reference.sample_rate
    speech = sum(span.length for span in spans)
    cues = list(trace.cues)
    origin = float(reference.pcm_sample0_file_time)

    print(f"\n=== 발화 대조: {reference.original_stem} ===\n")
    print(
        f"PCM {duration:.1f}초 · {reference.sample_rate}Hz · {reference.channels}채널"
        f" · 측정 원점 {origin:+.3f}초"
    )
    print(f"VAD 발화 {len(spans)}구간 · {speech:.0f}초 ({speech / duration:.1%})\n")

    starts = [cue.start for cue in cues]
    shift, shifted = best_shift(starts, spans)
    at_zero = _inside_share(starts, spans, 0.0)
    print(f"[전역 정렬] 큐 시작이 발화 안에 있는 비율 — 시프트 0 에서 {at_zero:.1%}")
    print(
        f"            최적 시프트 {shift:+.2f}초에서 {shifted:.1%} (차이 {shifted - at_zero:+.1%})"
    )
    print(
        "            주의: VAD 시작은 실제 발화보다 늦게 잡히고 `lead_in`(0.1초)은"
        " 일부러 앞선다 — 양수 시프트가 곧 자막이 빠르다는 뜻은 아니다.\n"
    )

    onset_errors = [(cue, onset_error(cue.start, spans)) for cue in cues]
    measured = [(cue, value) for cue, value in onset_errors if value is not None]
    absolute = [abs(value) for _cue, value in measured]
    print(
        f"[발화 시작 대조] 조용한 자리에서 시작하는 큐 {len(measured)}개"
        f" (나머지 {len(cues) - len(measured)}개는 말 한가운데라 대조 대상이 아니다)"
    )
    if measured:
        print(
            f"                 중앙 {_percentile(absolute, 0.5):.3f}초"
            f" · p90 {_percentile(absolute, 0.9):.3f}초"
        )
        worst_cue, worst_value = max(measured, key=lambda item: abs(item[1]))
        print(
            f"                 최악 {worst_value:+.2f}초 @{_clock(worst_cue.start)}"
            f"  {worst_cue.text[:40]!r}\n"
        )

    covered = [Span(cue.start, cue.end) for cue in cues]
    missed: list[Span] = []
    for span in spans:
        if covered_seconds(span, covered) < span.length * 0.5:
            missed.append(span)
    holes = [span for span in missed if span.length >= REPORT_GAP]
    total_missed = sum(span.length for span in missed)
    print(f"[말은 있는데 자막이 없다] 총 {total_missed:.0f}초 · {len(missed)}구간")
    for span in sorted(holes, key=lambda s: -s.length)[:12]:
        print(f"    {_clock(span.start)}~{_clock(span.end)}  {span.length:5.1f}초")
    print()

    phantoms = []
    for cue in cues:
        probe = Span(cue.start, cue.end)
        if probe.length <= 0:
            continue
        share = covered_seconds(probe, spans) / probe.length
        if share < PHANTOM_OVERLAP:
            phantoms.append((share, cue))
    print(f"[자막은 있는데 말이 없다] 큐 {len(phantoms)}개 (환각 후보)")
    for share, cue in sorted(phantoms, key=lambda item: item[0])[:20]:
        print(f"    {_clock(cue.start)}  겹침 {share:4.0%}  {cue.text[:50]!r}")
    print()
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="자막 시각을 실제 발화와 대조한다")
    parser.add_argument("media", type=Path, help="원본 영상/오디오")
    parser.add_argument("--out", type=Path, required=True, help="vlogsrt run 의 출력 폴더")
    parser.add_argument(
        "--scratch", type=Path, default=ROOT / ".vlogsrt-cache" / "onsets", help="PCM 보관 위치"
    )
    args = parser.parse_args(argv)

    trace_path = args.out / "trace.json"
    if not trace_path.exists():
        raise SystemExit(f"trace가 없다: {trace_path} — 건너뛰는 것은 통과가 아니다")
    trace = Trace.load(trace_path)

    print("PCM 참조를 만든다 (원본 해시 + 16kHz 추출)...", flush=True)
    reference = build_pcm(args.media, args.scratch)
    print("VAD 추론...", flush=True)
    session = create_cpu_session(bundled_artifact(ProjectConfig()))
    vad = infer_vad(reference, reference.source_id, ProjectConfig(), session)
    if vad.status != "success":
        raise SystemExit(f"VAD 실패: {vad.error}")
    return report(trace, speech_spans(vad, reference.sample_rate), reference)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
