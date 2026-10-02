"""브라우저 화면의 순수 조립 도우미 — 표준 라이브러리만 쓴다.

이 모듈은 아무 파생 계층도 import하지 않는다(계약: UI 도우미는 순수). 값의
모양을 사람이 읽는 표·HTML로 바꾸거나, 사람이 적은 입력을 값으로 해석할 때만
쓴다. 정본은 어디까지나 서버(공개 서비스·백엔드 작업공간)에 있다.
"""

from __future__ import annotations

import html
from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction


def parse_seconds(text: str, *, what: str = "시각") -> Fraction:
    """입력 칸의 초 표기 해석 — 정수·소수·유리수(`3/7`)를 정확히.

    소수는 `Fraction(str)`이 이진 오차 없이 십진 그대로 잡는다. 검토 경계는
    프레임 격자 위에서만 유효하다 — 어긋난 값은 백엔드가 거부하고 이 함수는
    해석만 한다.
    """
    cleaned = (text or "").strip()
    if not cleaned:
        raise ValueError(f"{what}이 비었다 — 초 단위로 적으세요(예: 12.5 또는 3/7)")
    try:
        return Fraction(cleaned)
    except (ValueError, ZeroDivisionError) as error:
        raise ValueError(f"{what}이 숫자가 아니다: {cleaned!r}") from error


def format_seconds(value: Fraction | float | int) -> str:
    """초를 사람 표기로 — 정수는 그대로, 나머지는 소수 여섯 자리까지."""
    fraction = value if isinstance(value, Fraction) else Fraction(str(value))
    if fraction.denominator == 1:
        return str(fraction.numerator)
    return f"{float(fraction):.6f}".rstrip("0").rstrip(".")


def format_bytes(size: float) -> str:
    """바이트 사람 표기 — 1024 단위, 한 자리까지."""
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= 1024
    return f"{value:.1f}TB"


def media_player_html(
    media_path: str, *, track_path: str | None = None, caption: str | None = None
) -> str:
    """등록 미디어 ID 주소의 재생기 HTML — 자막 트랙·출처 표시는 선택이다.

    경로는 오는 값을 그대로 쓰지 않는다: 호출자(서버)가 만든 **등록 ID 주소**만
    받고, 값은 HTML로 감싸기 전에 그대로 이스케이프한다. `caption`은 재생기 아래
    출처 한 줄(원본·추출 해시 등) — 서버가 검증한 문장만 온다.
    """
    source = html.escape(media_path, quote=True)
    track = ""
    if track_path:
        escaped = html.escape(track_path, quote=True)
        track = (
            f'\n    <track kind="subtitles" srclang="ko" label="검토 자막"'
            f' src="{escaped}" default></track>'
        )
    note = (
        f'\n<p style="color:#667; font-size:12px; margin:4px 0 0;">{html.escape(caption)}</p>'
        if caption
        else ""
    )
    return (
        '<video controls preload="metadata" style="width:100%;max-height:420px;'
        ' border-radius:8px; background:#000;">\n'
        f'    <source src="{source}"></source>{track}\n'
        f"</video>{note}"
    )


def waveform_svg(peaks: Sequence[float], *, height: int = 72) -> str:
    """파형 봉우리를 인라인 SVG로 — 진폭만 보이고 파형 원본은 나가지 않는다.

    W2 작업공간이 잰 PCM 피크 빈(0~1)을 받는다. 막대는 대칭으로 그려 중앙선
    기준 진폭을 그대로 보여준다 — 시각 장식이 아니라 큐 위치를 짚는 근거다.
    """
    values = [max(0.0, min(1.0, float(value))) for value in peaks]
    if not values:
        return '<p style="color:#888">파형 데이터가 없다</p>'
    width = max(1, len(values) * 3)
    mid = height / 2
    bars: list[str] = []
    for index, value in enumerate(values):
        bar = max(1.0, value * mid * 0.94)
        top = mid - bar
        bars.append(
            f'<rect x="{index * 3}" y="{top:.1f}" width="2" height="{bar * 2:.1f}"'
            ' fill="#4a7a96"></rect>'
        )
    return (
        f'<svg viewBox="0 0 {width} {height}" preserveAspectRatio="none"'
        f' style="width:100%;height:{height}px; background:#f4f6f8;'
        ' border-radius:6px;" role="img" aria-label="오디오 파형">\n'
        f'  <line x1="0" y1="{mid}" x2="{width}" y2="{mid}" stroke="#c8d2da"></line>\n'
        + "\n".join(f"  {bar}" for bar in bars)
        + "\n</svg>"
    )


@dataclass(frozen=True)
class CueRowView:
    """큐 한 줄의 화면 값 — 시각은 미리 포맷한 초 표기, 글자는 그대로.

    시각은 백엔드의 정확한 유리수(초)를 `format_seconds`로 옮긴 문자열이다 —
    ms 환산에서 반올림 오차가 생기지 않는다. 사람이 주는 편집값만 ms 정수로
    들어간다(서버 뷰에서 검증한다).
    """

    cue_id: str
    start_seconds: str
    end_seconds: str
    whisper_text: str
    auto_text: str
    basis: str
    chars_per_second: float


def cue_table_markdown(rows: Sequence[CueRowView]) -> str:
    """큐 행 표 — whisper 골격 글자와 채택 문구를 나란히, 초당 글자수까지."""
    if not rows:
        return "_큐가 없다 — 실행을 먼저 끝내거나, 빈 실행이면 무발화 결정을 남겨라._"
    lines = [
        "| 큐 | 시작(초) | 끝(초) | whisper 글자 | 채택 문구 | 근거 | 자/초 |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        whisper = row.whisper_text.replace("|", "\\|")
        auto = row.auto_text.replace("|", "\\|")
        lines.append(
            f"| `{row.cue_id}` | {row.start_seconds} | {row.end_seconds}"
            f" | {whisper or '(없음)'} | {auto or '(없음)'} | {row.basis}"
            f" | {row.chars_per_second:.1f} |"
        )
    return "\n".join(lines)


def review_rows_markdown(rows: Sequence[dict[str, object]]) -> str:
    """검토 조각 표 — 시간순 그대로, 선택·발화 상태·태그·메모까지 한 줄씩.

    `rows`는 백엔드 조회가 준 값 사전을 그대로 받는다(키가 없으면 정직한 `-`).
    순서는 호출자가 시간순으로 정렬해 준 것을 믿는다 — 여기서 다시 섞지 않는다.
    """

    def cell(row: dict[str, object], key: str) -> str:
        value = row.get(key)
        if value is None or value == "":
            return "-"
        return str(value).replace("|", "\\|")

    if not rows:
        return "_검토 리비전이 없다 — **검토 시작**으로 계획·분석 증거에서 만든다._"
    lines = [
        "| 조각 | 순번 | 원본 | 시작(초) | 끝(초) | 발화 | 선택 | 권고 | 태그 | 메모 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            f"| `{cell(row, 'segment_id')}` | {cell(row, 'source_order')}"
            f" | {cell(row, 'source_name')} | {cell(row, 'start_seconds')}"
            f" | {cell(row, 'end_seconds')} | {cell(row, 'speech_status')}"
            f" | {cell(row, 'selection')} | {cell(row, 'recommendation')}"
            f" | {cell(row, 'visual_tags')} | {cell(row, 'notes')} |"
        )
    return "\n".join(lines)


def vtt_timecode(total_ms: int) -> str:
    """ms 정수를 VTT 시각 표기로 — 화면용이므로 반올림하지 않고 자른다."""
    clamped = max(0, int(total_ms))
    hours, remainder = divmod(clamped, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


__all__ = [
    "CueRowView",
    "cue_table_markdown",
    "format_bytes",
    "format_seconds",
    "media_player_html",
    "parse_seconds",
    "review_rows_markdown",
    "vtt_timecode",
    "waveform_svg",
]
