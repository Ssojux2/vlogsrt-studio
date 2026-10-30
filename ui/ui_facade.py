"""브라우저 화면의 순수 조립 도우미 — 표준 라이브러리만 쓴다.

이 모듈은 아무 파생 계층도 import하지 않는다(계약: UI 도우미는 순수). 값의
모양을 사람이 읽는 표·HTML로 바꾸거나, 사람이 적은 입력을 값으로 해석할 때만
쓴다. 정본은 어디까지나 서버(공개 서비스·백엔드 작업공간)에 있다.
"""

from __future__ import annotations

import html
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from fractions import Fraction
from typing import cast


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


def fraction_ms(value: Fraction | float | int) -> int:
    """초(정확한 유리수)를 ms 정수로 — 가장 가까운 정수, 동률은 올린다.

    저장된 큐의 참값은 언제나 ms 양자화 계약 안에 있으므로 정수다. 그런데
    이진 부동 소수에서 온 값(`Fraction(1.2)` 등)은 1199.999…처럼 표시되어
    `int()`로 자르면 1200→1199로 흐른다(A5 실측 — 저장·표시 왕복이 하루살이
    드리프트를 만든다). 유리수를 그대로 두고 정확히 가장 가까운 정수로 잰다.
    """
    fraction = value if isinstance(value, Fraction) else Fraction(str(value))
    scaled = fraction * 1000
    return (scaled.numerator * 2 + scaled.denominator) // (scaled.denominator * 2)


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
        f'\n<p style="color:var(--vl-ink-muted); font-size:12px; margin:4px 0 0;">'
        f"{html.escape(caption)}</p>"
        if caption
        else ""
    )
    return (
        '<video controls preload="metadata" style="width:100%;max-height:420px;'
        ' border-radius:6px; background:var(--vl-video-well);">\n'
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
        return '<p style="color:var(--vl-ink-muted)">파형 데이터가 없다</p>'
    width = max(1, len(values) * 3)
    mid = height / 2
    bars: list[str] = []
    for index, value in enumerate(values):
        bar = max(1.0, value * mid * 0.94)
        top = mid - bar
        bars.append(
            f'<rect x="{index * 3}" y="{top:.1f}" width="2" height="{bar * 2:.1f}"'
            ' fill="currentColor"></rect>'
        )
    return (
        f'<svg viewBox="0 0 {width} {height}" preserveAspectRatio="none"'
        f' style="width:100%;height:{height}px; background:var(--vl-surface-sunken);'
        ' color:var(--vl-accent-ink); border-radius:6px;" role="img" aria-label="오디오 파형">\n'
        f'  <line x1="0" y1="{mid}" x2="{width}" y2="{mid}"'
        ' style="stroke:var(--vl-line-strong)"></line>\n'
        + "\n".join(f"  {bar}" for bar in bars)
        + "\n</svg>"
    )


@dataclass(frozen=True)
class CueRowView:
    """큐 한 줄의 화면 값 — 시각은 미리 포맷한 초 표기, 글자는 그대로.

    시각은 백엔드의 정확한 유리수(초)를 `format_seconds`로 옮긴 문자열이다 —
    ms 환산에서 반올림 오차가 생기지 않는다. 사람이 주는 편집값만 ms 정수로
    들어간다(서버 뷰에서 검증한다). `text`는 **지금 큐의 글자**다 — 수동 편집
    뒤에는 채택 문구와 달라지고, 화면은 그 차이를 숨기지 않고 나란히 보여준다.
    """

    cue_id: str
    start_seconds: str
    end_seconds: str
    whisper_text: str
    auto_text: str
    text: str
    basis: str
    chars_per_second: float


def _cps_text(value: float) -> str:
    """초당 글자 수 — 길이 0 큐(재지 못함)는 `inf`가 아니라 `—`로 보인다."""
    return f"{value:.1f}" if math.isfinite(value) else "—"


def cue_table_markdown(rows: Sequence[CueRowView]) -> str:
    """큐 행 표 — whisper 골격·채택 문구·지금 글자를 나란히, 초당 글자수까지."""
    if not rows:
        return "_큐가 없다 — 실행을 먼저 끝내거나, 빈 실행이면 무발화 결정을 남겨라._"
    lines = [
        "| 큐 | 시작(초) | 끝(초) | whisper 글자 | 채택 문구 | 지금 글자 | 근거 | 자/초 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        whisper = row.whisper_text.replace("|", "\\|")
        auto = row.auto_text.replace("|", "\\|")
        current = row.text.replace("|", "\\|")
        lines.append(
            f"| `{row.cue_id}` | {row.start_seconds} | {row.end_seconds}"
            f" | {whisper or '(없음)'} | {auto or '(없음)'} | {current or '(없음)'}"
            f" | {row.basis} | {_cps_text(row.chars_per_second)} |"
        )
    return "\n".join(lines)


def _cue_seek_js(start: str, end: str) -> tuple[str, str]:
    """큐 이동·반복의 인라인 실행본 — 문맥에 숫자만 들어가므로 이스케이프 대상이 없다.

    실행본은 `(function(){…})()`로 **즉시 실행**된다 — 괄호를 빠뜨리면 식이 함수를
    그냥 돌려보낼 뿐이라 클릭해도 아무 일도 일어나지 않는다(루트 브라우저 재현).
    반복은 `timeupdate`에서 큐 끝을 넘으면 시작으로 되돌리는 **참 반복**이다 —
    `pause` 이벤트(네이티브·수동 포함)에서 스스로 떼어지므로 정지한 뒤 다시
    재생되지 않는다. `_vlLoop`는 살아 있는 반복을 떼는 파괴자 — 이동·재반복·처음으로는
    먼저 이것을 불러 묵은 손잡이가 새 선택에 어김없이 발동하는 일을 막는다.
    """
    seek = (
        "(function(){var v=document.getElementById('caption-video');"
        "if(!v){return;}"
        "if(v._vlLoop){v._vlLoop();}"
        "v.currentTime=" + start + ";v.play();})()"
    )
    repeat = (
        "(function(){var v=document.getElementById('caption-video');if(!v){return;}"
        "if(v._vlLoop){v._vlLoop();}"
        "var s=" + start + ",e=" + end + ";"
        "var off=function(){v.removeEventListener('timeupdate',loop);"
        "v.removeEventListener('pause',off);v._vlLoop=null;};"
        "var loop=function(){"
        "if(v.currentTime>=e){v.currentTime=s;}"
        "else if(v.currentTime<s-0.25){off();}};"
        "v._vlLoop=off;v.addEventListener('timeupdate',loop);v.addEventListener('pause',off);"
        "v.currentTime=s;v.play();})()"
    )
    return seek, repeat


def cue_table_html(
    rows: Sequence[CueRowView],
    seek_by_cue: Mapping[str, tuple[str, str]] | None = None,
) -> str:
    """큐 편집표의 안전한 HTML — 지금 글자 칸과 큐 이동·반복 단추까지.

    `gr.Markdown`은 SVG·단추를 지워버리므로 큐 표는 이 HTML 구성요소로 나간다.
    `seek_by_cue`는 큐 ID → (미리보기 시작 시각, 끝 시각) 문자열 — 서버가 잰
    원점 매핑 그대로고, 없으면 단추 없이 표만 나간다(빈 성공으로 꾸미지 않는다).
    모든 글자는 이스케이프되고, 실행본에는 숫자만 들어간다.
    """
    if not rows:
        return (
            '<p style="color:var(--vl-ink-muted)">큐가 없다 — 실행을 먼저 끝내거나, 빈 실행이면'
            " 무발화 결정을 남긴다.</p>"
        )
    parts = [
        '<table style="width:100%;border-collapse:collapse;font-size:13px;">',
        "<thead><tr>"
        '<th style="text-align:left;padding:4px 6px;">큐</th>'
        '<th style="text-align:left;padding:4px 6px;">시작(초)</th>'
        '<th style="text-align:left;padding:4px 6px;">끝(초)</th>'
        '<th style="text-align:left;padding:4px 6px;">whisper 글자</th>'
        '<th style="text-align:left;padding:4px 6px;">채택 문구</th>'
        '<th style="text-align:left;padding:4px 6px;">지금 글자</th>'
        '<th style="text-align:left;padding:4px 6px;">근거</th>'
        '<th style="text-align:right;padding:4px 6px;">자/초</th>'
        '<th style="text-align:center;padding:4px 6px;">듣기</th>'
        "</tr></thead>",
        "<tbody>",
    ]
    for row in rows:
        seek_spec = (seek_by_cue or {}).get(row.cue_id)
        if seek_spec is None:
            listen = '<span style="color:var(--vl-ink-faint)">—</span>'
        else:
            start, end = seek_spec
            seek_js, repeat_js = _cue_seek_js(start, end)
            listen = (
                f'<button type="button" data-cue-id="{html.escape(row.cue_id, quote=True)}" '
                f'data-seek-start="{start}" data-seek-end="{end}" '
                f'style="margin:1px;" onclick="{seek_js}">▶</button>'
                f'<button type="button" data-cue-id="{html.escape(row.cue_id, quote=True)}" '
                f'data-seek-start="{start}" data-seek-end="{end}" '
                f'style="margin:1px;" onclick="{repeat_js}">반복</button>'
            )
        parts.append(
            "<tr>"
            f'<td style="padding:4px 6px;"><code>{html.escape(row.cue_id)}</code></td>'
            f'<td style="padding:4px 6px;">{html.escape(row.start_seconds)}</td>'
            f'<td style="padding:4px 6px;">{html.escape(row.end_seconds)}</td>'
            f'<td style="padding:4px 6px;">{html.escape(row.whisper_text) or "(없음)"}</td>'
            f'<td style="padding:4px 6px;">{html.escape(row.auto_text) or "(없음)"}</td>'
            f'<td style="padding:4px 6px;">{html.escape(row.text) or "(없음)"}</td>'
            f'<td style="padding:4px 6px;">{html.escape(row.basis)}</td>'
            f'<td style="padding:4px 6px;text-align:right;">'
            f"{_cps_text(row.chars_per_second)}</td>"
            f'<td style="padding:4px 6px;text-align:center;white-space:nowrap;">{listen}</td>'
            "</tr>"
        )
    parts.append("</tbody></table>")
    if seek_by_cue is None:
        parts.append(
            '<p style="color:var(--vl-ink-muted);font-size:12px;margin:4px 0 0;">'
            "선택 오디오 미리보기가 아직 없다 — 최종 영상 등록(오디오 트랙 확정)을 마치면"
            " 큐 단추가 켜진다.</p>"
        )
    else:
        parts.append(
            '<p style="color:var(--vl-ink-muted);font-size:12px;margin:4px 0 0;">'
            "▶ 큐 시작으로 이동 · 반복 큐 구간을 끝에서 시작으로 되돌려 잰다"
            "(정지·다른 선택으로 풀린다) —"
            " 미리보기 시계(선택 오디오의 잰 원점)에 맞춘 값이다.</p>"
        )
    return "".join(parts)


def caption_player_html(media_path: str, *, waveform: str, provenance: str) -> str:
    """자막 검토 재생기 — 선택 오디오 미리보기 영상·파형·출처를 한데 담는다.

    `gr.HTML`로만 나간다(Markdown은 SVG를 지운다 — 루트 브라우저 재현). 영상
    원소의 아이디 `caption-video`는 큐 표의 이동·반복 실행본이 짚는 대상이다.
    파형은 이미 안전한 SVG 문자열(`waveform_svg`)을 받아 그대로 싣는다.
    """
    source = html.escape(media_path, quote=True)
    return (
        '<div id="caption-player" class="vlogsrt-caption-player">\n'
        f'  <video id="caption-video" controls preload="metadata" src="{source}"'
        ' style="width:100%;max-height:420px;background:var(--vl-video-well);'
        'border-radius:6px;"></video>\n'
        f"  {waveform}\n"
        '  <div style="display:flex;gap:8px;margin-top:6px;">\n'
        '    <button type="button" onclick="(function(){var v='
        "document.getElementById('caption-video');if(!v){return;}"
        'if(v._vlLoop){v._vlLoop();}v.pause();})()">'
        "일시정지</button>\n"
        '    <button type="button" onclick="(function(){var v='
        "document.getElementById('caption-video');if(!v){return;}"
        'if(v._vlLoop){v._vlLoop();}v.pause();v.currentTime=0;})()">'
        "처음으로</button>\n"
        "  </div>\n"
        '  <p style="color:var(--vl-ink-muted);font-size:12px;margin:6px 0 0;">'
        f"{html.escape(provenance)}</p>\n"
        "</div>"
    )


def caption_cue_choices(rows: Sequence[CueRowView]) -> list[tuple[str, str]]:
    """큐 선택 목록 — 아이디·시각·지금 글자 미리보기로 편집 칸을 채우는 재료."""
    return [
        (
            f"{row.cue_id} · {row.start_seconds}→{row.end_seconds} · {row.text[:18] or '(없음)'}",
            row.cue_id,
        )
        for row in rows
    ]


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


# ── V2 작업대 — 순수 조립 + 코드 네이티브 클라이언트 ──────────────────────────
#
# 설계 원칙(코디네이터 계약): 선택·탐색·반복·오버레이는 **이미 받은 값만으로**
# 클라이언트에서 한다(Gradio 이벤트·미디어 생성 0회). 서버에 가는 길은 오직
# 명시적 편집·준비·질의뿐이고, 그 응답은 새 작업대 HTML 값으로 돌아와
# `watch`로 다시 묶인다. 영상 원소는 작업대와 **다른 구성요소**에 살아 있어
# 같은 원본을 다시 고르는 동안 재생 머리가 사라지지 않는다.


# ── 작업대 껍데기(W-17 B, D57) — 머리줄 · 명령 줄 활동 칸 · 서랍 머리 · 상태 줄 ─────────────
#
# 머리줄의 흐름 단계는 **보고만** 한다(누르는 곳이 아니다). 수는 서버가 작업대를 그릴 때마다
# 재서 작업대 값의 속성으로 싣고, 실행본이 그 값을 머리줄에 옮긴다 — 머리줄을 위한 타이머를
# 따로 두지 않는다.

FLOW_STEP_LABELS = ("폴더 불러오기", "분석 승인", "검토", "내보내기")
PROJECT_EMPTY_LABEL = "프로젝트 없음"
SHORTCUT_SEEK_SECONDS = 5
"""J/L이 옮기는 초 — 실행본의 `VL_SEEK_SECONDS`와 같은 값이다(시험이 둘을 맞춰 본다)."""
TOAST_TITLES = {"error": "오류", "warning": "주의", "info": "알림", "success": "완료"}
"""알림(toast) 머리 글자 — 완료는 알림, 막힘은 주의, 실패는 오류(D57 C).

그라디오 6.29는 알림 머리를 종류(type)에서 지어 `Error`·`Warning`·`Info`로 박는다 —
`gr.Error(title=…)`의 제목은 메시지에 실리지만 그려지지 않는다. 그래서 파이썬은 이 제목을 넘기고
(그려지는 날을 위해), 머리줄 실행본(`W.__vlToastTitles`)이 알림이 붙는 순간 머리 글자를 이 표로
바꾼다.
"""
CONFIRM_ALL_SOURCE_ID = "vl-queue-confirm-src"
"""대기열의 「남은 M개 모두 제외 유지」가 누르는 숨은 단추 — 옛 「자동 제외 확인」 처리기다."""
LIBRARY_CLOSE_TEXT = "라이브러리 닫기"
LIBRARY_CLOSE_ID = "vl-library-close"
"""머리줄의 「라이브러리 닫기」(D64 세션 F) — 열린 작업공간이 있을 때만 보인다."""
LIBRARY_CLOSE_SOURCE_ID = "vl-library-close-src"
"""머리줄 닫기 단추가 누르는 숨은 그라디오 단추 — 처리기·입출력은 그쪽에 묶인다."""

_CHECK_SVG = (
    '<svg viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="2"'
    ' stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<path d="M2.5 6.2l2.3 2.3 4.7-5"/></svg>'
)
_KEYS_SVG = (
    '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"'
    ' stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<rect x="1.75" y="4" width="12.5" height="8" rx="1.5"/>'
    '<path d="M4.5 7h.01M7 7h.01M9.5 7h.01M12 7h.01M5 9.5h6"/></svg>'
)
_SETTINGS_SVG = (
    '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"'
    ' stroke-linecap="round" aria-hidden="true"><path d="M2.5 4.5h7M12.5 4.5h1M2.5 11.5h1'
    'M6.5 11.5h7"/><circle cx="11" cy="4.5" r="1.5"/><circle cx="5" cy="11.5" r="1.5"/></svg>'
)
_CHEVRON_SVG = (
    '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"'
    ' stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<path d="M4 6l4 4 4-4"/></svg>'
)
# 단축키 표 — D50이 정한 키만 싣는다(없는 키는 보이지 않는다).
SHORTCUT_SHEET: tuple[tuple[tuple[str, ...], str], ...] = (
    (("Space", "K"), "재생 / 정지"),
    (("J", "L"), f"{SHORTCUT_SEEK_SECONDS}초 뒤로 / 앞으로"),
    ((",", "."), "정지하고 한 프레임 뒤로 / 앞으로"),
    (("←", "→"), "경계 손잡이에 초점이 있을 때 한 프레임씩 옮기기"),
    (("Enter",), "경계 초안을 프레임에 맞추기"),
    (("I", "O"), "지금 위치를 시작 · 끝으로 찍기"),
    (("1", "2", "3"), "찍은 범위를 추천 · 보통 · 비추천으로 표시"),
    (("Esc",), "찍은 시작 · 끝 지우기"),
)
SHORTCUT_HINT: tuple[tuple[tuple[str, ...], str], ...] = (
    (("Space",), "재생"),
    (("J", "L"), f"±{SHORTCUT_SEEK_SECONDS}초"),
    ((",", "."), "한 프레임"),
)


@dataclass(frozen=True)
class FlowCounts:
    """머리줄 흐름 단계의 살아 있는 수 — 작업대를 그릴 때마다 서버가 잰다."""

    imported: int = 0
    analysed: int = 0
    proposals: int = 0
    sent: bool = False
    """Final Cut으로 보냈다(서랍의 「고급 · Final Cut」)."""
    exported: bool = False
    """추천만 이어붙인 초안을 낸 적이 있다(W-46a, D100 §7a)."""


@dataclass(frozen=True)
class FlowStep:
    number: int
    label: str
    state: str
    """`done` · `cur` · `todo` — 지금 단계는 끝나지 않은 첫 단계다."""
    sub: str
    alert: bool
    """미검토 제외 제안이 남았다 — 정보일 뿐 보내기를 막지 않는다(D56)."""


def flow_steps(counts: FlowCounts) -> tuple[FlowStep, ...]:
    """네 단계의 상태와 수 — 보고만 한다. 「검토」의 제외 제안 수는 관문이 아니다(D56)."""
    total = counts.imported
    out = counts.sent or counts.exported
    done = (total > 0, total > 0 and counts.analysed >= total, out, out)
    subs = (
        f"{total}개" if total else "",
        f"{counts.analysed}/{total}" if total else "",
        f"미확인 비추천 {counts.proposals}" if counts.proposals else "",
        "내보냄" if counts.exported else "보냄" if counts.sent else "",
    )
    current = next((index for index, finished in enumerate(done) if not finished), None)
    return tuple(
        FlowStep(
            number=index + 1,
            label=label,
            state="done" if done[index] else "cur" if index == current else "todo",
            sub=subs[index],
            alert=index == 2 and counts.proposals > 0,
        )
        for index, label in enumerate(FLOW_STEP_LABELS)
    )


def flow_steps_html(steps: Sequence[FlowStep]) -> str:
    """흐름 단계 표지 — 끝난 단계는 확인 표, 지금 단계는 `aria-current`, 글자는 전부 이스케이프."""
    parts = []
    for step in steps:
        title = step.label + (f" · {step.sub}" if step.sub else "")
        mark = _CHECK_SVG + '<span class="vl-sr">완료</span>' if step.state == "done" else ""
        number = "" if step.state == "done" else str(step.number)
        current = ' aria-current="step"' if step.state == "cur" else ""
        sub_class = "sub r" if step.alert else "sub"
        sub = f'<span class="{sub_class}">{html.escape(step.sub)}</span>' if step.sub else ""
        parts.append(
            f'<span class="vl-step {step.state}" data-vl-step="{step.number}"{current}'
            f' title="{html.escape(title, quote=True)}"><span class="no">{mark}{number}</span>'
            f'<span class="lbl">{html.escape(step.label)}</span>{sub}</span>'
        )
    return '<span class="vl-step-sep" aria-hidden="true"></span>'.join(parts)


def _kbd_html(keys: Sequence[str]) -> str:
    return "".join(f'<kbd class="vl-kbd">{html.escape(key)}</kbd>' for key in keys)


def keysheet_html() -> str:
    """「단축키」 판 — 머리줄·상태 줄의 단추가 같은 판 하나를 연다(`?` 키로는 열지 않는다)."""
    rows = "".join(
        f"<dt>{_kbd_html(keys)}</dt><dd>{html.escape(meaning)}</dd>"
        for keys, meaning in SHORTCUT_SHEET
    )
    return (
        '<div id="vl-keysheet" class="vl-keysheet-pop" role="dialog" aria-label="단축키" hidden>'
        f'<dl class="vl-keysheet">{rows}</dl>'
        '<p class="vl-keysheet-note">입력 칸에 글을 쓰는 중에는 단축키가 동작하지 않습니다.</p>'
        "</div>"
    )


def topbar_html(
    *, online: bool, mode_note: str, project: str, flow: str, can_close: bool = False
) -> str:
    """머리줄(44px) — 이름 · 프로젝트(왼쪽 서랍) · 닫기 · 흐름 단계 · 연결 방식 · 단축키 · 설정.

    연결 방식은 돈이 나갈 수 있음을 말하므로 온라인이면 경고 모양이다(뒤꼬리 글이 아니다).
    `flow`는 `flow_steps_html`이 지은(이스케이프된) 조각이다. 「라이브러리 닫기」는 열린 작업공간이
    있을 때만 보인다(`can_close`) — 그 뒤로는 작업대 값의 닫힘 표지를 실행본이 따른다.
    """
    mode = "online" if online else "offline"
    hidden = "" if can_close else " hidden"
    return (
        '<header class="vl-topbar">'
        '<span class="vl-wordmark">vlogsrt</span>'
        '<button type="button" id="vl-project" class="vl-projbtn" data-vl-drawer="project-tools"'
        ' aria-controls="project-tools" title="프로젝트 · 라이브러리 가져오기">'
        f'<span id="vl-project-name">{html.escape(project or PROJECT_EMPTY_LABEL)}</span>'
        f"{_CHEVRON_SVG}</button>"
        f'<button type="button" id="{LIBRARY_CLOSE_ID}" class="vl-btn vl-btn-ghost vl-btn-sm"'
        f' title="작업공간을 닫고 빈 화면으로 — 원본·분석 결과는 그대로입니다"{hidden}>'
        f"{LIBRARY_CLOSE_TEXT}</button>"
        f'<nav id="vl-flow" class="vl-steps" aria-label="작업 단계">{flow}</nav>'
        '<span class="vl-grow"></span>'
        f'<span class="vl-mode {mode}" data-vl-mode="{mode}"><span class="vl-dot"'
        f' aria-hidden="true"></span>{html.escape(mode_note)}</span>'
        '<button type="button" class="vl-btn vl-btn-ghost vl-btn-sm" data-vl-keys-open'
        f' aria-controls="vl-keysheet" aria-expanded="false">{_KEYS_SVG}'
        '<span class="vl-btn-label">단축키</span></button>'
        '<button type="button" id="vl-settings-open" class="vl-btn vl-btn-ghost vl-btn-sm"'
        ' data-vl-drawer="settings-drawer" aria-controls="settings-drawer">'
        f'{_SETTINGS_SVG}<span class="vl-btn-label">설정</span></button>'
        f"{keysheet_html()}</header>"
    )


def drawer_head_html(title: str, drawer_id: str) -> str:
    """서랍 머리 — 서랍은 화면 높이 전체를 덮으므로 닫는 단추를 서랍 안에 둔다."""
    return (
        '<div class="vl-drawer-head">'
        f"<strong>{html.escape(title)}</strong>"
        '<button type="button" class="vl-btn vl-btn-ghost vl-btn-sm"'
        f' data-vl-drawer-close="{html.escape(drawer_id, quote=True)}">닫기</button></div>'
    )


RENDER_EXPORT_RUNNING_TEXT = "추천만 이어붙이는 중입니다 — 진행은 활동 칸과 「기록」 판에 보입니다."
RENDER_EXPORT_WARN_TEXT = (
    "확인하지 않은 비추천 구간 {count}곳이 빠졌습니다 — 보고서에 목록이 있습니다."
)


def format_duration(seconds: float) -> str:
    """초 → 「3분 12.4초」·「8.2초」 — 결과 카드의 총 길이."""
    tenths = round(max(0.0, float(seconds)) * 10)
    minutes, rest = divmod(tenths, 600)
    text = f"{rest // 10}.{rest % 10}초"
    return f"{minutes}분 {text}" if minutes else text


def render_export_card_html(
    summary: Mapping[str, object] | None, *, running: bool = False, failure: str | None = None
) -> str:
    """「추천만 이어붙여 내보내기」 결과 카드(W-46a, D100 §7a) — 글자는 전부 이스케이프한다.

    `summary`는 서비스 `latest_render_export`의 값(가장 최근에 다 낸 초안)이다. 확인하지 않은
    비추천이 빠졌으면 빨간 경고 한 줄 — 내보내기는 막지 않고 알린다(사용자 결정 2026-10-27).
    도는 중이면 그 줄을, 지난 시도가 실패했으면 그 사유를 맨 위에 싣는다. 아무것도 없으면 빈 글이다.
    """
    head: list[str] = []
    if running:
        head.append(f'<p class="vl-export-state">{html.escape(RENDER_EXPORT_RUNNING_TEXT)}</p>')
    if failure:
        head.append(f'<p class="vl-export-warn">지난 내보내기 실패 — {html.escape(failure)}</p>')
    body = head + (_export_summary_lines(summary) if summary is not None else [])
    if not body:
        return ""
    return (
        '<section class="vl-export" aria-label="추천만 이어붙인 초안">'
        + "".join(body)
        + ("</section>")
    )


def _number(value: object) -> float:
    return float(value) if isinstance(value, (int, float)) else 0.0


def _export_summary_lines(summary: Mapping[str, object]) -> list[str]:
    seconds = _number(summary.get("total_seconds"))
    encoder = str(summary.get("encoder") or "알 수 없음")
    lines = [
        f'<p class="vl-export-head"><strong>초안</strong> {html.escape(format_duration(seconds))}'
        f" · 조각 {int(_number(summary.get('parts')))}개 · 인코더 {html.escape(encoder)}</p>"
    ]
    dropped = int(_number(summary.get("unreviewed_red_dropped")))
    if dropped > 0:
        warn = RENDER_EXPORT_WARN_TEXT.format(count=dropped)
        lines.append(f'<p class="vl-export-warn" role="alert">{html.escape(warn)}</p>')
    listed = summary.get("unsupported")
    unsupported = [str(name) for name in listed] if isinstance(listed, (list, tuple)) else []
    if unsupported:
        names = ", ".join(unsupported)
        lines.append(
            f'<p class="vl-export-note">분석 지원 밖이라 빠진 원본: {html.escape(names)}</p>'
        )
    media = summary.get("media_file_id")
    if media:
        src = html.escape(f"/media/{media}", quote=True)
        lines.append(
            f'<p><a href="{src}" target="_blank" rel="noopener">초안 영상 새 탭에서 보기</a></p>'
            f'<video controls preload="metadata" src="{src}" aria-label="초안 영상"></video>'
        )
    rows = (("영상", "video"), ("자막", "srt"), ("보고서", "report"))
    paths = "".join(
        f"<dt>{label}</dt><dd>{html.escape(str(summary.get(key) or ''))}</dd>"
        for label, key in rows
    )
    lines.append(f'<dl class="vl-export-paths">{paths}</dl>')
    return lines


def activity_html() -> str:
    """명령 줄의 활동 칸(32px 고정) — 한 줄 말줄임, 전체 글은 「기록」 판과 `title`에 있다.

    내용은 실행본(`ACTIVITY_JS`)이 채운다: 1초 상태 줄(`#quick-status`)의 변화와 단추 응답
    (`ACTIVITY_REPLY_JS`). 글 길이가 칸 높이를 바꾸지 않으므로 무대가 밀리지 않는다.
    서버에서 도는 일이 있으면 띠가 가장 새 서버 줄(없으면 도는 일의 머리줄)과 「N개 작업」 배지를
    보인다 — 새 창·판을 띄우지 않는다(D101 고침).
    """
    return (
        '<div id="vl-activity" class="vl-activity" data-vl-kind="status">'
        '<span class="vl-dot" aria-hidden="true"></span>'
        '<span id="vl-activity-msg" class="msg" role="status" aria-live="polite"></span>'
        '<span id="vl-activity-count" class="vl-activity-count" hidden></span>'
        '<button type="button" id="vl-activity-log-open" class="vl-btn vl-btn-ghost vl-btn-sm"'
        ' aria-controls="vl-activity-log" aria-expanded="false">기록</button>'
        '<div id="vl-activity-log" class="vl-activity-log" role="log" aria-label="최근 활동"'
        ' hidden><p id="vl-activity-usage" class="vl-activity-usage" hidden></p>'
        f"{_SERVER_ACTIVITY_HTML}"
        '<h3 class="vl-server-head">화면 기록</h3>'
        '<p class="vl-activity-empty">아직 기록이 없습니다.</p><ol></ol></div>'
        "</div>"
    )


ACTIVITY_ROUTE = "/vl-activity"
"""「기록」 판의 서버 쪽을 읽는 경로(D61) — 읽기만 한다. 화면 실행본과 서버가 같은 값을 쓴다."""


@dataclass(frozen=True)
class ActivityViewSettings:
    """도는 일을 화면에 바로 보이는 활동 띠의 손잡이(D101).

    모두 외부 근거 없는 조정 대상이다 — 표시만 정하므로 어느 지문·캐시 열쇠·승인·견적에도 없다.
    `poll_busy_ms`: 도는 것이 있거나 「기록」 판이 열린 동안 묻는 주기(D61의 2초 그대로).
    `poll_idle_ms`: 한가할 때의 심장박동 — 다른 탭·자동으로 시작된 작업을 잡는다. 0이면 걸지 않는다.
    `idle_after_empty_polls`: 빈 응답이 이만큼 이어지면 한가하다.
    `server_log_lines`: 한 번에 보내는 서버 줄 상한이자 「기록」 판이 그리는 줄 수.
    `reply_hold_ms`: 단추 응답이 띠에 머무는 동안 — 그 뒤에야 도는 일의 머리줄이 띠를 덮는다.
    """

    poll_busy_ms: int = 2000
    poll_idle_ms: int = 15000
    idle_after_empty_polls: int = 2
    server_log_lines: int = 500
    reply_hold_ms: int = 5000


ACTIVITY_VIEW = ActivityViewSettings()

_SERVER_ACTIVITY_HTML = (
    '<div class="vl-server" aria-live="off">'
    '<h3 class="vl-server-head">지금 도는 작업</h3>'
    '<ul id="vl-server-procs" class="vl-server-rows">'
    '<li class="vl-server-idle">불러오는 중…</li></ul>'
    '<h3 class="vl-server-head">서버 로그 <span id="vl-server-note" class="vl-server-note">'
    "</span></h3>"
    '<div id="vl-server-log" class="vl-server-log" tabindex="0" aria-label="서버 로그"></div>'
    "</div>"
)
"""판이 열린 동안만 채워지는 두 칸 — 2초마다 바뀌므로 화면 낭독기에는 알리지 않는다(`aria-live`)."""


def format_clock(seconds: float) -> str:
    """경과·한도 표기 — `25:10`, 한 시간이 넘으면 `1:02:05`(초는 버린다)."""
    whole = max(0, int(seconds))
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def progress_percent(progress_seconds: float | None, duration_seconds: float | None) -> int | None:
    """원본 길이 대비 출력 시각(내림, 100에서 멈춤) — 둘 중 하나라도 모르면 %가 없다."""
    if progress_seconds is None or not duration_seconds or duration_seconds <= 0:
        return None
    return min(100, int(progress_seconds / duration_seconds * 100))


def process_row_text(
    step: str, name: str, elapsed: float, timeout: float | None, percent: int | None
) -> str:
    """「지금 도는 작업」 프로세스 한 줄 — `단계 · 파일 · 25:10 경과 / 한도 54:31 · 62%`."""
    limit = "한도 없음" if timeout is None else f"한도 {format_clock(timeout)}"
    text = f"{step} · {name} · {format_clock(elapsed)} 경과 / {limit}"
    return text if percent is None else f"{text} · {percent}%"


def job_row_text(label: str, status: str, current: int, total: int, minutes: int | None) -> str:
    """작업 한 줄 — `영상 분석 · 진행 중 · 12/18 · 3분 전 갱신`. 모르는 칸은 뺀다."""
    parts = [label, status]
    if total > 0:
        parts.append(f"{current}/{total}")
    if minutes is not None:
        parts.append("방금 갱신" if minutes <= 0 else f"{minutes}분 전 갱신")
    return " · ".join(parts)


def job_percent(current: int, total: int) -> int | None:
    """작업 진행 막대(내림, 100에서 멈춤) — 전체 수를 모르면 막대가 없다."""
    if total <= 0:
        return None
    return min(100, max(0, current) * 100 // total)


def statusline_html() -> str:
    """무대 아래 상태 줄(24px) — 왼쪽은 작업대 각주(`#vl-footnote`), 오른쪽은 단축키 안내.

    각주는 실행본이 `textContent`로 갈아 쓰므로 안내는 각주 **밖**의 형제에 둔다.
    """
    hints = "".join(
        f"<span>{_kbd_html(keys)} {html.escape(meaning)}</span>" for keys, meaning in SHORTCUT_HINT
    )
    return (
        '<div id="vl-statusline" class="vl-statusline">'
        '<p id="vl-footnote" class="vl-footnote" role="status"></p>'
        f'<span class="vl-keys">{hints}<button type="button" class="vl-btn vl-btn-ghost'
        ' vl-btn-sm" data-vl-keys-open aria-controls="vl-keysheet">단축키</button></span></div>'
    )


ACTIVITY_REPLY_JS = (
    "(reply) => { if (window.__vlActivity) { window.__vlActivity.reply(reply); } return []; }"
)
"""단추 사건이 끝나면 그 응답 칸의 값을 활동 칸으로 — 서버를 다시 부르지 않는다."""

ACTIVITY_WAKE_JS = (
    "(value) => { if (window.__vlServer) { window.__vlServer.wake(true); } return value; }"
)
"""일을 거는 단추(입력 하나)의 앞 단계 — 서버 쪽 고리를 곧바로 깨운다(D101). 입력은 그대로 넘긴다.

폴더 불러오기의 스캔은 요청 안에서 동기로 돌므로, 응답을 기다리지 않고 지금 묻기 시작해야
스캔하는 동안의 줄이 「작업 진행」 판에 흐른다.
"""

_MARKDOWN_PUNCTUATION = re.compile(r"([!-/:-@\[-`{-~])")


def literal_markdown(text: str) -> str:
    """글 그대로 그려지는 마크다운 — ASCII 문장부호를 모두 백슬래시로 escape한다(CommonMark).

    견적 칸의 값은 승인 클릭이 함께 보내는 문구라(D54) 그려진 글이 원문과 같아야 앞 단계의 글
    대조가 선다 — `2~3분`이 `23분`으로, `_x_`가 기울임으로 바뀌면 승인이 영영 막힌다. 앞 단계
    (`APPROVE_GUARD_JS`)가 같은 규칙으로 되풀어 원문을 보낸다.
    """
    return _MARKDOWN_PUNCTUATION.sub(r"\\\1", text)


APPROVE_GUARD_JS = r"""(signature, shownQuote) => {
  // 승인은 일을 건다 — 거절되든 아니든 서버 쪽 고리를 깨운다(D101, 표시만).
  if (window.__vlServer) { window.__vlServer.wake(true); }
  // 승인은 사람이 지금 보는 견적에만 묶인다(D54). 견적 칸이 서랍·판에 덮였거나 화면 밖이거나
  // 칸 안에서 잘렸거나 그려진 글이 보낼 문구와 다르면 빈 견적을 보내 서버가 「새 견적을
  // 확인하세요」로 거절하게 하고 견적을 보이게 한다 — 다음 클릭이 그것을 승인한다.
  const quote = document.getElementById('library-analysis-scope');
  // 읽고 재는 것은 그려진 마크다운 노드뿐이다 — 감싼 칸에는 그라디오 상태 표시가 형제로 있어
  // 1초 틱마다 '0.1s' 같은 글이 잠깐 섞인다. 노드를 못 찾으면 닫힌 쪽(거절 길)이다.
  const md = quote && quote.querySelector ? quote.querySelector('[data-testid="markdown"]') : null;
  const drawerOpen = !!document.querySelector('#project-tools.open, #settings-drawer.open');
  const inside = (r) => r.width >= 1 && r.height >= 1 && r.top >= 0 && r.left >= 0 &&
    r.bottom <= window.innerHeight && r.right <= window.innerWidth;
  const uncovered = (el, r) => [[r.left + r.width / 2, r.top + r.height / 2],
    [r.left + 2, r.top + 2], [r.right - 2, r.top + 2], [r.left + 2, r.bottom - 2],
    [r.right - 2, r.bottom - 2]].every(([x, y]) => {
    const hit = document.elementFromPoint(x, y);
    return !!hit && el.contains(hit);
  });
  // 칸의 값은 문장부호를 escape한 마크다운(`literal_markdown`)이다 — 되풀면 견적 원문이고, 그려진
  // 글도 그 원문이다. 빈칸만 한 칸으로 맞춰 비교하고, 서버에는 되푼 원문을 보낸다(대조는 그대로).
  const sent = String(shownQuote || '').replace(/\\([!-\/:-@\[-`{-~])/g, '$1');
  const flat = (s) => String(s || '').replace(/\s+/g, ' ').trim();
  let seen = !!md && !drawerOpen;
  if (seen) {
    const style = window.getComputedStyle(md);
    const box = md.getBoundingClientRect();
    seen = style.display !== 'none' && style.visibility === 'visible' && inside(box) &&
      uncovered(md, box) && md.scrollHeight <= md.clientHeight + 1 &&
      flat(md.textContent) !== '' && flat(md.textContent) === flat(sent);
  }
  if (seen) { return [signature, sent]; }
  if (window.__vlDrawer) {
    ['project-tools', 'settings-drawer'].forEach((id) => window.__vlDrawer(id, false));
  } else {
    document.querySelectorAll('#project-tools.open > .toggle-button, ' +
      '#settings-drawer.open > .toggle-button').forEach((button) => button.click());
  }
  if (window.__vlClosePopovers) { window.__vlClosePopovers(null); }
  if (quote && quote.scrollIntoView) { quote.scrollIntoView({ block: 'nearest' }); }
  return [signature, ''];
}"""
"""일괄 승인 클릭의 앞 단계 — 견적 칸이 정말 보일 때만 그 문구를 보낸다(덮였으면 빈 문구)."""

SERVER_ACTIVITY_JS = r"""
  // 서버 쪽 기록(D61·D101) — 고리 하나가 읽는다. 걸린 타이머는 언제나 하나, setTimeout 사슬뿐이다.
  // 바쁘거나 「기록」 판이 열린 동안은 짧게, 한가하면 길게 묻는다(0이면 안 묻는다). 바쁜 동안
  // 띠가 가장 새 서버 줄을 보인다 — 새 창·판을 띄우지 않는다(D101 고침).
  // 서버가 준 글(파일 이름·오류 글·로그 줄)은 전부 textContent로 넣는다.
  var SERVER_ROUTE = "__VL_ACTIVITY_ROUTE__";
  var V = __VL_ACTIVITY_VIEW__;
  // 맨 아래에서 이 안이면 맨 아래를 보는 중이다 — 새 줄을 따라간다. 위로 올려 읽던 자리는 그대로다.
  var STICK_PX = 24;
  // 서버 줄 머리의 표지(rich `›`·`·`) — 띠에는 글만 보인다.
  var LINE_MARK = /^[\u203a\u00b7]\s*/;
  var S = W.__vlServer = { timer: null, inflight: false, after: 0, boot: null, soon: false,
    busy: false, empty: 0, logOpen: false, headed: false };
  S.wait = function () {
    if (S.soon) { return 0; }
    if (S.busy || S.logOpen) { return V.poll_busy_ms; }
    return V.poll_idle_ms > 0 ? V.poll_idle_ms : null;
  };
  // 다음 묻기를 다시 건다 — 묻는 중이면 그 응답이 건다(요청은 한 번에 하나).
  S.arm = function () {
    if (S.inflight) { return; }
    if (S.timer !== null) { W.clearTimeout(S.timer); S.timer = null; }
    var wait = S.wait();
    S.soon = false;
    if (wait !== null) { S.timer = W.setTimeout(S.poll, wait); }
  };
  // 깨우기 — 단추(strong)는 바쁨 회차를 열고, 상태 줄 변화는 한가할 때만 한 번 당겨 묻는다.
  S.wake = function (strong) {
    if (strong) { S.enter(); }
    else if (S.busy || S.logOpen) { return; }
    S.soon = true; S.arm();
  };
  S.enter = function () { S.empty = 0; S.busy = true; };
  S.poll = function () {
    S.timer = null;
    S.inflight = true;
    W.fetch(SERVER_ROUTE + "?after=" + S.after, { cache: "no-store" })
      .then(function (r) {
        if (!r.ok) { throw new Error("HTTP " + r.status); }
        return r.json();
      })
      .then(function (body) { S.note(""); S.paint(body); })
      .catch(function (error) {
        S.note("서버에 닿지 않습니다 — " + ((error && error.message) || error));
      })
      .then(function () { S.inflight = false; S.arm(); });
  };
  S.note = function (text) {
    var note = byId("vl-server-note");
    if (note) { note.textContent = text; }
  };
  S.row = function (kind, text) {
    var row = document.createElement("li");
    row.setAttribute("data-vl-kind", kind);
    row.textContent = text; row.title = text;
    return row;
  };
  S.paint = function (body) {
    var log = body.log || {};
    if (S.boot !== null && log.boot !== S.boot) {
      // 서버가 다시 떴다 — 번호가 처음부터다. 이 응답의 줄은 버리고 비운 뒤 곧바로 처음부터 받는다.
      S.boot = log.boot; S.after = 0; S.soon = true;
      var stale = byId("vl-server-log");
      if (stale) { stale.textContent = ""; }
      return;
    }
    // 첫 응답의 줄은 지난 기록이다 — 판에는 보이되 「지금 일하는 중」의 증거로 세지 않는다.
    var lines = S.boot !== null ? log.lines || [] : [];
    S.boot = log.boot;
    S.paintRows(body);
    S.paintLog(log);
    S.settle(body, lines.length ? lines[lines.length - 1] : null);
  };
  S.paintRows = function (body) {
    var list = byId("vl-server-procs");
    if (!list) { return; }
    list.textContent = "";
    (body.processes || []).forEach(function (p) { list.appendChild(S.row("process", p.text)); });
    (body.jobs || []).forEach(function (j) { list.appendChild(S.row("job", j.text)); });
    if (!list.children.length) {
      var idle = S.row("idle", "지금 도는 작업이 없습니다.");
      idle.className = "vl-server-idle"; list.appendChild(idle);
    }
  };
  S.line = function (line) {
    var row = document.createElement("div");
    row.className = "vl-server-line";
    row.setAttribute("data-vl-level", line.level);
    var time = document.createElement("time");
    time.textContent = line.time;
    var text = document.createElement("span");
    text.textContent = line.text;
    row.appendChild(time); row.appendChild(text);
    return row;
  };
  S.paintLog = function (log) {
    var box = byId("vl-server-log");
    if (!box) { return; }
    var stick = box.scrollHeight - box.scrollTop - box.clientHeight <= STICK_PX;
    (log.lines || []).forEach(function (line) { box.appendChild(S.line(line)); });
    while (box.children.length > V.server_log_lines) { box.removeChild(box.firstChild); }
    if (typeof log.latest === "number") { S.after = log.latest; }
    if (stick) { box.scrollTop = box.scrollHeight; }
  };
  // 바쁨 판정 — 도는 것이 있으면 바쁘다. 단추가 연 회차 안의 새 서버 줄도 일하는 중이다(폴더
  // 스캔은 요청 안에서 동기로 돌아 작업 줄이 없다). 빈 응답이 이어지면 한가하다.
  S.settle = function (body, line) {
    if (body.busy) { S.enter(); }
    else if (S.busy && line) { S.empty = 0; }
    else if (S.busy && (S.empty += 1) >= V.idle_after_empty_polls) { S.busy = false; }
    S.paintStrip(body, S.busy ? line : null);
  };
  // 띠 — 바쁜 동안 가장 새 서버 줄(없으면 도는 일의 머리줄)과 「N개 작업」 배지. 방금 온 단추
  // 응답·연결 알림은 덮지 않는다. 한가해지면 마지막 화면 기록을 되살린다.
  S.paintStrip = function (body, line) {
    var count = (body.processes || []).length + (body.jobs || []).length;
    var badge = byId("vl-activity-count");
    if (badge) { badge.textContent = count ? count + "개 작업" : ""; badge.hidden = !count; }
    var latest = A.entries[0];
    var held = !!latest && (latest.text.indexOf(KEY_NOTICE) >= 0 ||
      (latest.kind === "reply" && Date.now() - latest.at < V.reply_hold_ms));
    var text = line ? String(line.text).replace(LINE_MARK, "") : S.busy ? body.headline : "";
    if (text && !held) { A.strip(text, "server"); S.headed = true; }
    else if (!text && S.headed) { S.headed = false; A.restore(); }
  };
  S.panel = function (open) {
    S.logOpen = open;
    if (open) { S.soon = true; }
    S.arm();
  };
""".replace("__VL_ACTIVITY_ROUTE__", ACTIVITY_ROUTE).replace(
    "__VL_ACTIVITY_VIEW__", json.dumps(asdict(ACTIVITY_VIEW))
)
"""서버 쪽 실행본 — `SHELL_JS` 안에 들어간다.

「기록」 판과 띠의 서버 줄·머리줄을 채운다(D61·D101).
"""

SHELL_JS = r"""
(function () {
  var W = window;
  // 서랍·막·판(단축키·활동 기록)·활동 칸 — 문서에 한 번만 묶고, 원소는 사건 순간에 찾는다.
  var DRAWERS = ["project-tools", "settings-drawer"];
  var POPOVERS = { "vl-keysheet": "[data-vl-keys-open]",
    "vl-activity-log": "#vl-activity-log-open" };
  var LOG_LIMIT = 20;
  var STATUS_ID = "quick-status";
  // 실사용 줄(「실사용 $…」)의 원천 — 기록 판 맨 위에 고정해 말줄임 없이 다 보인다(D46).
  var USAGE_ID = "vl-usage-src";
  var SCRIM_ID = "vl-scrim";
  // 키가 없다는 알림(D102 — `.env` 안내) — 도는 일의 머리줄이 이것을 덮지 않는다(D101).
  var KEY_NOTICE = "연결 설정 필요";
  var WATCH_MS = 1000;
  // 단추 응답은 그 사건이 갈아 쓴 상태 줄보다 늦게 온다 — 이 안의 같은 글은 한 번만 적는다.
  var MERGE_MS = 3000;
  // Esc는 입력 칸·펼친 고르기 칸의 몫이다 — 거기서 누른 Esc가 서랍을 닫지 않는다.
  // 펼침 표시만으로 고르지 않는다: 단축키·기록 여닫이 단추도 판을 열면 펼침이 되어
  // 방금 누른 그 단추 위의 Esc가 판을 닫지 못했다.
  var ESC_OWNERS = 'input, textarea, select, [contenteditable=""], [contenteditable="true"], ' +
    '[role="textbox"], [role="combobox"][aria-expanded="true"], [role="listbox"]';
  if (W.__vlShellBound) { return; }
  W.__vlShellBound = true;
  function byId(id) { return document.getElementById(id); }
  function drawerOpen(id) { var d = byId(id); return !!d && d.classList.contains("open"); }
  // 막 — 서랍이 열려 있는 동안 나머지 화면(승인 단추 포함)은 누르지 못한다. 누르면 서랍만 닫힌다.
  function scrim() {
    var s = byId(SCRIM_ID);
    if (!s && document.body) {
      s = document.createElement("div");
      s.id = SCRIM_ID; s.className = "vl-scrim"; s.hidden = true;
      s.setAttribute("aria-hidden", "true");
      document.body.appendChild(s);
    }
    return s;
  }
  // 서랍이 열린 동안 서랍 밖은 키보드로도 닿지 않는다(W-17 C 고침) — 막은 마우스만 막아, 초점이
  // 남은 대기열 단추를 Enter로 누를 수 있었고 Tab은 서랍 밖으로 나갔다. 열린 서랍에서 문서 뿌리까지
  // 올라가며 형제 원소에 inert를 단다(막과 알림 칸은 빼고). 단 것만 표시해 두었다가 서랍이 닫히는
  // 모든 길(닫기 · Esc · 막 · 머리줄 단추 · 그라디오 여닫이 · 서버)에서 이 함수가 다시 돌며 걷는다.
  var INERT_MARK = "data-vl-inert";
  W.__vlInertOutside = function (drawer) {
    Array.prototype.forEach.call(document.querySelectorAll("[" + INERT_MARK + "]"), function (el) {
      el.inert = false; el.removeAttribute("inert"); el.removeAttribute(INERT_MARK);
    });
    for (var node = drawer; node && node.parentElement; node = node.parentElement) {
      Array.prototype.forEach.call(node.parentElement.children, function (sibling) {
        if (sibling === node || sibling.id === SCRIM_ID || sibling.inert ||
            sibling.classList.contains("toast-wrap") ||
            /^(SCRIPT|STYLE|LINK)$/.test(sibling.tagName)) { return; }
        sibling.inert = true; sibling.setAttribute(INERT_MARK, "1");
      });
      if (node.parentElement === document.body) { break; }
    }
  };
  W.__vlSyncDrawers = function () {
    var open = DRAWERS.some(drawerOpen);
    var s = scrim();
    if (s) { s.hidden = !open; }
    if (open) { closePopovers(null); }
    W.__vlInertOutside(open ? byId(DRAWERS.filter(drawerOpen)[0]) : null);
    return open;
  };
  // 서랍 — 그라디오 Sidebar의 여닫이 단추를 대신 누른다(그 단추는 CSS가 감춘다).
  W.__vlDrawer = function (id, open) {
    var drawer = byId(id);
    if (!drawer) { return false; }
    var isOpen = drawer.classList.contains("open");
    var want = open === undefined ? !isOpen : !!open;
    if (want) {
      DRAWERS.forEach(function (other) { if (other !== id) { W.__vlDrawer(other, false); } });
    }
    if (want !== isOpen) {
      var toggle = drawer.querySelector(":scope > .toggle-button");
      if (toggle) { toggle.click(); }
    }
    W.__vlSyncDrawers();
    requestAnimationFrame(function () {
      W.__vlSyncDrawers();
      var close = want ? drawer.querySelector("[data-vl-drawer-close]") : null;
      if (close) { close.focus(); }
    });
    return want;
  };
  // 서버가 서랍을 여닫아도(라이브러리 불러오기 뒤 닫기 등) 막이 따라간다.
  var watched = {};
  function watchDrawers() {
    DRAWERS.forEach(function (id) {
      var d = byId(id);
      if (!d || watched[id] === d) { return; }
      watched[id] = d;
      new MutationObserver(W.__vlSyncDrawers).observe(d,
        { attributes: true, attributeFilter: ["class"] });
      W.__vlSyncDrawers();
    });
  }
  function setPopover(id, open) {
    var box = byId(id);
    if (!box) { return; }
    box.hidden = !open;
    document.querySelectorAll(POPOVERS[id]).forEach(function (opener) {
      opener.setAttribute("aria-expanded", open ? "true" : "false");
    });
    if (open && id === "vl-activity-log" && W.__vlActivity) { W.__vlActivity.paintLog(); }
    if (id === "vl-activity-log" && W.__vlServer) { W.__vlServer.panel(open); }
  }
  function closePopovers(except) {
    var closed = false;
    Object.keys(POPOVERS).forEach(function (id) {
      var box = byId(id);
      if (box && !box.hidden && id !== except) { setPopover(id, false); closed = true; }
    });
    return closed;
  }
  W.__vlClosePopovers = closePopovers;
  function pad(n) { return String(n).padStart(2, "0"); }
  var A = W.__vlActivity = { entries: [], statusText: null, statusEl: null, observer: null };
  // 마크다운 응답을 한 줄로 — 문단은 ' · '로 잇고, 걷는 것은 굵게 표지(**)와
  // [글](주소)의 주소뿐이다.
  // 밑줄 두 개·역따옴표는 식별자의 일부일 수 있어 그대로 둔다(글자를 바꾸지 않는다).
  A.plain = function (markdown) {
    return String(markdown || "")
      .replace(/\[([^\]\n]+)\]\((?:[^()\s]|\([^()\s]*\))+\)/g, "$1")
      .replace(/\*\*/g, "").split(/\n\s*\n/).map(function (part) {
        return part.replace(/\s+/g, " ").trim();
      }).filter(Boolean).join(" · ");
  };
  A.domText = function (el) {
    var blocks = el.querySelectorAll("p, li, h1, h2, h3, h4");
    var parts = Array.prototype.map.call(blocks, function (b) {
      return b.textContent.replace(/\s+/g, " ").trim();
    }).filter(Boolean);
    return parts.length ? parts.join(" · ") : el.textContent.replace(/\s+/g, " ").trim();
  };
  A.paintLog = function () {
    var box = byId("vl-activity-log");
    if (!box) { return; }
    var list = box.querySelector("ol");
    var empty = box.querySelector(".vl-activity-empty");
    if (empty) { empty.hidden = A.entries.length > 0; }
    if (!list) { return; }
    list.textContent = "";
    A.entries.forEach(function (entry) {
      var row = document.createElement("li");
      row.setAttribute("data-vl-kind", entry.kind);
      var time = document.createElement("time");
      time.textContent = entry.time;
      var text = document.createElement("span");
      text.textContent = entry.text;
      row.appendChild(time); row.appendChild(text); list.appendChild(row);
    });
  };
  // 한 줄 칸 — 글이 길어도 높이는 그대로(말줄임), 전체 글은 title과 기록 판에 있다.
  A.strip = function (text, kind) {
    var msg = byId("vl-activity-msg"), box = byId("vl-activity");
    if (!msg || !box) { return false; }
    msg.textContent = text; msg.title = text;
    box.setAttribute("data-vl-kind", kind);
    return true;
  };
  // 도는 일의 머리줄이 걷히면 마지막 기록을 되살린다 — 기록 항목은 늘리지 않는다.
  A.restore = function () {
    var latest = A.entries[0];
    A.strip(latest ? latest.text : "", latest ? latest.kind : "status");
  };
  // 견적을 실은 승인 거절은 견적 칸(#vl-quote-note)이, 실사용 줄은 기록 판 고정 줄이 다 보인다.
  A.show = function (text, kind) {
    if (!text || !byId("vl-activity-msg")) { return; }
    // 도는 일의 머리줄이 띠에 있는 동안 상태 줄은 기록에만 남는다 — 띠가 1초마다 뒤바뀌지 않는다.
    var under = kind === "status" && text.indexOf(KEY_NOTICE) < 0 && W.__vlServer &&
      W.__vlServer.headed;
    if (!under) { A.strip(text, kind); }
    var now = new Date();
    A.entries.unshift({ time: pad(now.getHours()) + ":" + pad(now.getMinutes()) + ":" +
      pad(now.getSeconds()), text: text, kind: kind, at: now.getTime() });
    if (A.entries.length > LOG_LIMIT) { A.entries.length = LOG_LIMIT; }
    var log = byId("vl-activity-log");
    if (log && !log.hidden) { A.paintLog(); }
  };
  // 단추 응답 — 같은 글이라도 누른 만큼 기록한다. 1초 다시 그리기는 이것을 지우지 않는다.
  // 같은 사건이 방금 갈아 쓴 상태 줄(같은 글)은 이 응답 하나로 바꿔 적는다.
  A.reply = function (markdown) {
    var text = A.plain(markdown);
    var latest = A.entries[0];
    if (latest && latest.kind === "status" && latest.text === text &&
        Date.now() - latest.at < MERGE_MS) {
      A.entries.shift();
    }
    A.show(text, "reply");
    // 단추 사건이 끝났다 — 그 사건이 건 작업을 곧바로 보러 간다(D101).
    if (W.__vlServer) { W.__vlServer.wake(true); }
  };
  // 1초 상태 줄 — 글이 **바뀔 때만** 칸을 갈아 쓴다. 방금 기록한 응답과 같은 글이면
  // 다시 적지 않는다.
  A.readStatus = function () {
    var el = A.statusEl;
    if (!el) { return; }
    var text = A.domText(el);
    if (text === A.statusText) { return; }
    A.statusText = text;
    if (W.__vlServer) { W.__vlServer.wake(false); }
    var latest = A.entries[0];
    if (latest && latest.kind === "reply" && latest.text === text) { return; }
    A.show(text, "status");
  };
  A.watchStatus = function () {
    var el = byId(STATUS_ID);
    if (!el || el === A.statusEl) { return; }
    if (A.observer) { A.observer.disconnect(); }
    A.statusEl = el;
    A.observer = new MutationObserver(A.readStatus);
    A.observer.observe(el, { childList: true, subtree: true, characterData: true });
    A.readStatus();
  };
  // 실사용 줄 — 글이 바뀔 때만 고정 줄을 갈아 쓴다. 기록 항목은 늘리지 않는다.
  A.readUsage = function () {
    var src = byId(USAGE_ID), row = byId("vl-activity-usage");
    if (!row) { return; }
    var text = src ? A.domText(src) : "";
    if (text === A.usageText) { return; }
    A.usageText = text;
    row.textContent = text; row.title = text; row.hidden = !text;
  };
__VL_SERVER_ACTIVITY__
  // 알림 머리 글자 — 그라디오가 종류에서 지은 영어(Error·Warning·Info·Success)를 한글로 바꾼다.
  // 글자 마디만 바꾸고(스벨트는 종류가 같으면 다시 쓰지 않는다), 닫기 단추의 이름도 한글로.
  var TOAST_TITLES = __VL_TOAST_TITLES__;
  W.__vlToastTitles = function (scope) {
    var box = scope || document;
    Array.prototype.forEach.call(box.querySelectorAll(".toast-title"), function (title) {
      var kind = Object.keys(TOAST_TITLES).find(function (k) {
        return title.classList.contains(k);
      });
      var text = title.firstChild;
      if (!kind || !text || text.nodeType !== 3) { return; }
      var want = TOAST_TITLES[kind] + " ";
      if (text.nodeValue !== want) { text.nodeValue = want; }
    });
    Array.prototype.forEach.call(box.querySelectorAll('[data-testid="toast-close"]'),
      function (close) {
        if (close.getAttribute("aria-label") !== "닫기") {
          close.setAttribute("aria-label", "닫기");
        }
      });
  };
  // 알림 칸(.toast-wrap)이 생기면 한 번 지켜본다 — 알림이 붙는 같은 순간(그리기 전)에 바꾼다.
  var toastWrap = null;
  function watchToasts() {
    var wrap = document.querySelector(".toast-wrap");
    if (!wrap || wrap === toastWrap) { return; }
    toastWrap = wrap;
    new MutationObserver(function () { W.__vlToastTitles(wrap); }).observe(wrap,
      { childList: true, subtree: true, characterData: true });
    W.__vlToastTitles(wrap);
  }
  // 「라이브러리 닫기」 — 숨은 그라디오 단추를 누른다(처리기·입출력·활동 칸 답은 그쪽에 있다).
  function closeLibrary() {
    var source = byId("vl-library-close-src");
    if (source && source.tagName !== "BUTTON" && source.querySelector) {
      source = source.querySelector("button") || source;
    }
    if (!source || typeof source.click !== "function") {
      A.show("라이브러리 닫기 단추를 찾지 못했습니다. 새로고침하세요.", "reply");
      return;
    }
    source.click();
  }
  function watch() { A.watchStatus(); A.readUsage(); watchDrawers(); watchToasts(); }
  W.setInterval(watch, WATCH_MS);
  watch();
  // 열자마자 한 번 — 다시 띄운 탭도 도는 일을 곧바로 본다.
  S.wake(false);
  document.addEventListener("click", function (ev) {
    var t = ev.target && ev.target.closest ? ev.target : null;
    if (!t) { return; }
    if (t.id === SCRIM_ID) { DRAWERS.forEach(function (id) { W.__vlDrawer(id, false); }); return; }
    var drawer = t.closest("[data-vl-drawer]");
    if (drawer) { W.__vlDrawer(drawer.getAttribute("data-vl-drawer")); return; }
    var closer = t.closest("[data-vl-drawer-close]");
    if (closer) { W.__vlDrawer(closer.getAttribute("data-vl-drawer-close"), false); return; }
    var keys = t.closest("[data-vl-keys-open]");
    var log = t.closest("#vl-activity-log-open");
    var target = keys ? "vl-keysheet" : log ? "vl-activity-log" : null;
    if (target) {
      var box = byId(target);
      closePopovers(target);
      setPopover(target, !!box && box.hidden);
      return;
    }
    if (t.closest("#vl-library-close")) { closeLibrary(); return; }
    if (!t.closest("#vl-keysheet, #vl-activity-log")) { closePopovers(null); }
  });
  // Esc — 떠 있는 판부터, 없으면 서랍을 닫는다(재생 단축키 표에는 없는 키다).
  document.addEventListener("keydown", function (ev) {
    if (ev.key !== "Escape" || ev.isComposing) { return; }
    var t = ev.target;
    if (t && typeof t.closest === "function" && t.closest(ESC_OWNERS)) { return; }
    if (closePopovers(null)) { return; }
    DRAWERS.forEach(function (id) { W.__vlDrawer(id, false); });
  });
})();
""".replace("__VL_TOAST_TITLES__", json.dumps(TOAST_TITLES, ensure_ascii=False)).replace(
    "__VL_SERVER_ACTIVITY__", SERVER_ACTIVITY_JS.strip("\n")
)
"""머리줄의 실행본 — 서랍 여닫이와 막 · 단축키 판 · 활동 칸(1초 상태 줄 관찰 + 단추 응답 기록).

활동 칸의 원천은 둘이다: 1초 상태 줄(`#quick-status`)은 글이 바뀔 때만, 단추 응답은
`ACTIVITY_REPLY_JS`가 사건이 끝날 때마다. 기록은 화면(탭) 메모리에만 최근 것부터 남는다.
"""


MODEL_HOLDER_ATTRIBUTE = "data-vl-model-json"
"""작업대 상태 JSON을 싣는 칸의 표식 — 뿌리의 첫 자식이다(클라이언트 `VL.modelRaw`가 찾는다)."""


def _model_holder(model: Mapping[str, object]) -> str:
    """작업대 상태를 뿌리의 첫 자식(숨은 칸)의 글자로 싣는다 — <script>는 쓰지 않는다.

    gradio 6은 값에 `<script`가 있으면 그릴 때마다 경고를 낸다(innerHTML이라 실행도 안 된다).
    예전엔 JSON을 뿌리의 data 속성에 넣었다 — 따옴표마다 `&quot;`라 그린 값이 JSON의 1.76배였다
    (합성 18개 253 KB, 실데이터 추정 0.8–1.2 MB). 이제 글자로 싣고 JSON 안에서만 `<`·`&`를 유니코드
    이스케이프한다(W-24 세션 G) — HTML이 글자로 풀 것이 없어 `textContent`가 곧 JSON이고, 문자열
    값이 태그나 문자 참조처럼 보이지 않는다. 둘은 JSON 문자열 안에만 나올 수 있어 값은 그대로다.
    """
    payload = json.dumps(model, ensure_ascii=False, separators=(",", ":"))
    text = payload.replace("<", "\\u003c").replace("&", "\\u0026")
    return f'<div class="vl-model-data" {MODEL_HOLDER_ATTRIBUTE} hidden>{text}</div>'


CRITERIA_BADGE_ID = "vl-criteria-badge"
"""범례 옆 「기준 바꿈」 표지 — 작업공간 추천 기준이 기본값과 다를 때만 보인다(W-46a)."""
TIER_LEGEND_HTML = (
    '<span class="vl-legend" aria-label="등급 범례">'
    '<span class="vl-tier-key green">■ 추천</span>'
    '<span class="vl-tier-key yellow">■ 보통</span>'
    '<span class="vl-tier-key red">▨ 비추천</span>'
    '<span class="vl-tier-key hold">□ 판정 보류</span>'
    '<span class="vl-tier-key loose" title="컷 경계와 맞지 않는 제외 제안 — 남은 수에 들지'
    ' 않습니다"><i class="vl-key-hollow" aria-hidden="true"></i> 경계 어긋남</span>'
    f'<span id="{CRITERIA_BADGE_ID}" class="vl-criteria-badge"'
    ' title="이 작업공간의 추천 기준이 기본값과 다릅니다 — 설정의 「추천 기준」에서 봅니다"'
    " hidden>기준 바꿈</span>"
    "</span>"
)
"""타임라인 등급 범례 — 3단 등급·보류와 속 빈 빨강(컷 경계와 맞지 않는 제안, D57 C 고침 4).

빨강의 화면 이름은 「비추천」이다(W-46a, D100 §7a) — 내부 등급 이름(`TIER_LABELS`)과 Final Cut
키워드(「제외 제안」)는 그대로다. 「기준 바꿈」 표지는 작업대 값의 `data-vl-criteria`를 실행본이
따른다.
"""


def visual_player_shell_html(video_src: str | None, note: str) -> str:
    """영상 작업대의 재생기 껍데기 — 서버가 그리는 건 이 안정된 뼈대뿐이다.

    영상 원소·시간 레일·이동 단추는 이 껍데기 안에 살고, **내용물**(레일 구간,
    선택 강조, 재생 머리)은 작업대 구성요소의 클라이언트 실행본이 채운다. 껍데기가
    다시 그려지는 순간은 작업공간 전환뿐이다 — 그때 영상은 문서에서 사라져
    언로드되고 묵은 시청각이 남지 않는다. `js_on_load` 실행본이 단추를 묶는다.
    """
    safe_src = html.escape(video_src, quote=True) if video_src else ""
    # `data-vl-src` 표식까지 서버가 심어 둔다 — 첫 배선이 같은 주소를 다시 대입해
    # 재생기를 재구성하는 일이 없다(원본 전환은 표식이 달라질 때만 일어난다).
    src_attr = f' src="{safe_src}" data-vl-src="{safe_src}"' if video_src else ""
    safe_note = html.escape(note)
    return (
        '<div id="visual-player" class="vl-player">'
        '<section id="visual-preview" aria-label="선택 영상 미리보기">'
        '<div class="vl-video-surface">'
        '<video id="visual-video" controls preload="metadata"'
        f'{src_attr} aria-label="원본 전체 미리보기 재생기"></video>'
        '<div id="visual-source-caption" class="vl-source-overlay"'
        ' aria-live="off" hidden></div></div>'
        '<button type="button" id="vl-current-caption" class="vl-current-caption" disabled'
        ' aria-label="현재 원본 자막 수정">재생하면 원본 자막이 여기에 표시됩니다.</button>'
        f'<p id="visual-note" class="vl-note">{safe_note}</p></section>'
        '<section id="visual-timeline" aria-label="추천 구간과 영상 타임라인">'
        '<div class="vl-transport">'
        f"<strong>타임라인</strong>{TIER_LEGEND_HTML}"
        '<button type="button" id="vl-source-caption-toggle" class="vl-btn"'
        ' aria-pressed="true">재생 자막</button>'
        '<button type="button" id="vl-timeline-mode" class="vl-btn">원본 타임라인</button>'
        '<button type="button" id="vl-kept-play" class="vl-btn">사용할 컷 연속 재생</button>'
        '<button type="button" id="vl-zoom-in" class="vl-btn"'
        ' aria-label="타임라인 확대">＋</button>'
        '<button type="button" id="vl-zoom-out" class="vl-btn"'
        ' aria-label="타임라인 축소">−</button>'
        '<button type="button" id="vl-repeat" class="vl-btn">선택 반복</button>'
        '<button type="button" id="vl-pause" class="vl-btn">정지</button>'
        '<span id="vl-clock" class="vl-clock" aria-live="off">0.000</span></div>'
        '<div id="visual-rail" class="vl-rail" role="group" '
        'aria-label="추천 트랙은 영상 트랙 위에 있습니다. 클릭하면 해당 시각으로 이동합니다"></div>'
        f"{range_markbar_html()}</section>{ranges_panel_html()}</div>"
    )


RANGE_MARK_BUTTONS: tuple[tuple[str, str, str, str], ...] = (
    ("recommended", "green", "추천", "1"),
    ("normal", "yellow", "보통", "2"),
    ("not_recommended", "red", "비추천", "3"),
    ("clear", "clear", "표시 지우기", ""),
)
"""재생기 아래 표시 단추(W-46b, D100 §7b) — (표시, 색, 이름, 단축키). 표시 철자는 서비스
그대로다."""
RANGE_EMPTY_TEXT = "재생하며 In(I)·Out(O)으로 시작과 끝을 찍으세요."
ADVANCED_TOGGLE_ID = "vl-advanced-toggle"
"""「고급 도구」 — 컷 편집 패널·제외 제안 대기열·컷 경계 손잡이를 펼친다(무대의
`data-vl-advanced`)."""


def range_markbar_html() -> str:
    """시작·끝 표시 줄 — In·Out · 찍은 범위 · 추천·보통·비추천·표시 지우기 · 되돌리기(W-46b).

    단추는 시작과 끝이 모두 있고 시작 < 끝일 때만 켜진다(실행본이 켠다). 표시 하나가 검토
    리비전 하나이고 「되돌리기」가 그 한 걸음을 되돌린다(D100 §4).
    """
    marks = "".join(
        f'<button type="button" id="vl-mark-{mark}" class="vl-btn vl-mark-btn {tone}"'
        f' data-vl-mark="{mark}" disabled'
        f' aria-keyshortcuts="{key}">{html.escape(word)}'
        + (f' <kbd class="vl-kbd">{key}</kbd>' if key else "")
        + "</button>"
        for mark, tone, word, key in RANGE_MARK_BUTTONS
    )
    return (
        '<div id="vl-markbar" class="vl-markbar" role="group" aria-label="시작·끝으로 추천 바꾸기">'
        '<button type="button" id="vl-mark-in" class="vl-btn" aria-keyshortcuts="I"'
        ' title="지금 재생 위치를 시작점으로">In <kbd class="vl-kbd">I</kbd></button>'
        '<button type="button" id="vl-mark-out" class="vl-btn" aria-keyshortcuts="O"'
        ' title="지금 재생 위치를 끝점으로">Out <kbd class="vl-kbd">O</kbd></button>'
        f'<span id="vl-range-readout" class="vl-range-readout" aria-live="polite">'
        f"{html.escape(RANGE_EMPTY_TEXT)}</span>{marks}"
        '<button type="button" id="vl-range-undo" class="vl-btn vl-btn-ghost"'
        ' title="마지막 편집 한 걸음을 되돌립니다">되돌리기</button></div>'
    )


def ranges_panel_html() -> str:
    """「추천 구간」 칸 — 고른 영상의 내보낼 부분 목록(누르면 그 시각으로)과 「고급 도구」 단추.

    목록은 실행본이 모델의 `kept`(초안 렌더와 같은 순수 계획, D100 §5a)로만 채운다.
    """
    return (
        '<section id="vl-ranges" class="vl-ranges" aria-label="추천 구간 — 내보낼 부분">'
        '<div class="vl-ranges-head"><strong>내보낼 부분</strong>'
        '<span id="vl-ranges-sum" class="vl-ranges-sum"></span></div>'
        '<div id="vl-range-list" class="vl-range-list" role="list"></div>'
        f'<button type="button" id="{ADVANCED_TOGGLE_ID}" class="vl-btn vl-btn-ghost vl-btn-sm"'
        ' aria-expanded="false" aria-controls="vl-panel">고급 도구 — 컷 편집 · 제외 제안 · 자막'
        "</button></section>"
    )


def _flow_attributes(
    project: str | None, flow: FlowCounts | None, *, criteria_changed: bool = False
) -> str:
    """머리줄로 옮길 값 — 프로젝트 이름과 흐름 단계 조각(이스케이프된 HTML)을 속성에 싣는다.

    작업공간이 없으면(`project` 없음) 닫힘 표지도 싣는다 — 실행본이 「라이브러리 닫기」를 숨긴다.
    추천 기준이 기본값과 다르면 `data-vl-criteria="changed"` — 실행본이 「기준 바꿈」을 보인다
    (기본이면 싣지 않는다: 기준을 고친 적 없는 작업공간의 값은 예전 바이트 그대로다).
    """
    if flow is None:
        return ""
    steps = html.escape(flow_steps_html(flow_steps(flow)), quote=True)
    name = html.escape(project or PROJECT_EMPTY_LABEL, quote=True)
    closed = ' data-vl-closed="1"' if project is None else ""
    criteria = ' data-vl-criteria="changed"' if criteria_changed else ""
    return f' data-vl-flow="{steps}" data-vl-project="{name}"{closed}{criteria}'


def visual_workbench_html(
    model: Mapping[str, object],
    *,
    project: str | None = None,
    flow: FlowCounts | None = None,
    criteria_changed: bool = False,
) -> str:
    """영상 작업대 본문 — 조각 목록·선택 패널의 빈 그릇과 상태 JSON.

    그릇을 채우는 건 이 파일의 클라이언트 실행본(`VISUAL_BOOTSTRAP_JS`)이다.
    서버는 여기까지: 값은 전부 공개 질의가 준 것, 사람이 읽는 글자는 전부
    이스케이프되어 있다(모델 안 글자는 클라이언트가 textContent로만 싣는다).
    목록·패널·상태 줄은 **한 격자의 칸**으로 펼쳐진다 — 뿌리는
    `display:contents`로 무대(`#visual-stage`) 격자에 그대로 참여하고, 재생기
    껍데기(가운데 칸)는 별도 구성요소라 같은 원본을 다시 고르는 동안 재생이
    이어진다(좁은 화면 ≤860px에서는 격자 영역 순서로 재생기·타임라인이 먼저 온다).
    `flow`가 있으면 머리줄의 흐름 단계·프로젝트 이름을 속성으로 싣는다(자리표시 포함).
    """
    flow_attrs = _flow_attributes(project, flow, criteria_changed=criteria_changed)
    placeholder = model.get("empty")
    if isinstance(placeholder, str) and placeholder:
        return (
            f'<div id="visual-workbench" class="vl-workbench" data-vl-empty="1"{flow_attrs}>'
            f'<p class="vl-note">{html.escape(placeholder)}</p></div>'
        )
    return (
        f'<div id="visual-workbench" class="vl-workbench"{flow_attrs}>{_model_holder(model)}\n'
        '  <div id="vl-thumbs" class="vl-thumbs" role="listbox"'
        ' aria-label="영상 목록(촬영 시각 순)"></div>\n'
        '  <div id="vl-panel" class="vl-panel" aria-live="polite"></div>\n'
        f"  {statusline_html()}\n"
        "</div>"
    )


def caption_overlay_player_html(media_path: str, *, waveform: str, provenance: str) -> str:
    """자막 검토 재생기(V2) — 재생 시각 오버레이와 토글 단추가 붙은 껍데기.

    오버레이 문구는 작업대 실행본이 `timeupdate`에서 textContent로만 바꾼다 —
    재생기 구성요소를 다시 그리지 않으므로 저장 뒤에도 재생이 끊기지 않는다.
    기존 아이디(`caption-video`·`caption-player`)는 그대로다(기존 큐 표 실행본
    호환).
    """
    source = html.escape(media_path, quote=True)
    return (
        '<div id="caption-player" class="vlogsrt-caption-player">\n'
        f'  <video id="caption-video" controls preload="metadata" src="{source}"'
        ' aria-label="선택 오디오 미리보기 재생기" style="width:100%;max-height:42vh;'
        'background:var(--vl-video-well);border-radius:6px;object-fit:contain;"></video>\n'
        '  <div id="caption-overlay" class="cap-overlay cap-overlay-on"'
        ' aria-live="off"></div>\n'
        f"  {waveform}\n"
        '  <div style="display:flex;gap:8px;margin-top:6px;flex-wrap:wrap;">\n'
        '    <button type="button" class="vl-btn" id="cap-overlay-toggle"'
        ' aria-pressed="true">재생 자막 오버레이</button>\n'
        '    <button type="button" class="vl-btn" onclick="(function(){var v='
        "document.getElementById('caption-video');if(!v){return;}"
        'if(v._vlLoop){v._vlLoop();}v.pause();})()">'
        "일시정지</button>\n"
        '    <button type="button" class="vl-btn" onclick="(function(){var v='
        "document.getElementById('caption-video');if(!v){return;}"
        'if(v._vlLoop){v._vlLoop();}v.pause();v.currentTime=0;})()">'
        "처음으로</button>\n"
        "  </div>\n"
        # 출처 원문(경로·sha·코덱·등록 ID)은 접힌 상세로 — 기본 재생기 흐름에는
        # 사람 글자만 보인다(원문은 그대로 보존).
        '  <details class="vl-details" style="margin-top:6px;"><summary>영상 상세 —'
        f' 출처·등록</summary><p class="vl-note">{html.escape(provenance)}</p></details>\n'
        "</div>"
    )


def caption_workbench_html(model: Mapping[str, object]) -> str:
    """자막 작업대 본문 — 간결 큐 목록·클라이언트 편집기·접힌 상세의 그릇.

    행 선택·편집 칸 채우기·오버레이 갱신은 전부 클라이언트다(미디어·제공자 0회).
    저장·확정 버튼만 서버 함수로 간다(명시적 저장·명시적 확인 그대로).
    """
    placeholder = model.get("empty")
    if isinstance(placeholder, str) and placeholder:
        return (
            '<div id="caption-workbench" class="vl-workbench" data-vl-empty="1">'
            f'<p class="vl-note">{html.escape(placeholder)}</p></div>'
        )
    return (
        f'<div id="caption-workbench" class="vl-workbench">{_model_holder(model)}\n'
        '  <div id="cap-cues" class="cap-cues" role="listbox"'
        ' aria-label="자막 큐 목록 — 행을 고르면 재생기가 그 큐로 이동한다"></div>\n'
        '  <div class="cap-editor">\n'
        '    <div class="cap-editor-fields">\n'
        '      <label>글자<input type="text" id="cap-text" class="vl-input"></label>\n'
        "    </div>\n"
        '    <div class="vl-row">\n'
        '      <button type="button" id="cap-save" class="vl-btn vl-btn-primary">'
        "글자 저장</button>\n"
        '      <button type="button" id="cap-confirm" class="vl-btn">확정 게시 승인</button>\n'
        "    </div>\n"
        # 정밀 시각·큐 ID는 접힌 상세로 — 기본 흐름은 글자 저장·확정만 본다.
        # 아이디·읽기 전용·실행본 선택자는 그대로(계약 무변경).
        '    <details class="vl-details"><summary>정밀 시각 · 큐 ID</summary>\n'
        '      <div class="cap-editor-fields">\n'
        '        <label>시작(ms)<input type="text" id="cap-start" class="vl-input'
        ' vl-input-narrow" inputmode="numeric"></label>\n'
        '        <label>끝(ms)<input type="text" id="cap-end" class="vl-input'
        ' vl-input-narrow" inputmode="numeric"></label>\n'
        '        <label>큐 ID<input type="text" id="cap-cue-id" class="vl-input'
        ' vl-input-narrow" readonly></label>\n'
        "      </div>\n"
        '      <div class="vl-row">\n'
        '        <button type="button" id="cap-save-time" class="vl-btn">시각 저장</button>\n'
        "      </div>\n"
        "    </details>\n"
        "  </div>\n"
        '  <p id="cap-msg" class="vl-footnote" role="status"></p>\n'
        "</div>"
    )


def caption_workbench_details_html(model: Mapping[str, object]) -> str:
    """자막 작업대의 접힌 상세 — 원시·채택 글자·품질·출처(펼침 전에는 방해하지 않는다).

    서버가 안전하게 조립한 표만 싣는다: 클라이언트는 여기에 손대지 않는다.
    """
    placeholder = model.get("empty")
    if isinstance(placeholder, str) and placeholder:
        return f'<p class="vl-note">{html.escape(placeholder)}</p>'
    rows = cast(Sequence[Mapping[str, object]], model.get("details", ()))
    body: list[str] = []
    for row in rows:
        body.append(
            "<tr>"
            f'<td class="vl-td"><code>{html.escape(str(row.get("id", "")))}</code></td>'
            f'<td class="vl-td">{html.escape(str(row.get("startMs", "")))}→'
            f"{html.escape(str(row.get('endMs', '')))}</td>"
            f'<td class="vl-td">{html.escape(str(row.get("whisper", ""))) or "(없음)"}</td>'
            f'<td class="vl-td">{html.escape(str(row.get("auto", ""))) or "(없음)"}</td>'
            f'<td class="vl-td">{html.escape(str(row.get("now", ""))) or "(없음)"}</td>'
            f'<td class="vl-td">{html.escape(str(row.get("basis", "")))}</td>'
            "</tr>"
        )
    provenance = str(model.get("provenance", ""))
    return (
        '<details class="vl-details"><summary>상세 — whisper·채택 문구·지금 글자·출처</summary>'
        '<table class="vl-table"><thead><tr>'
        '<th class="vl-th">큐</th><th class="vl-th">시각(ms)</th>'
        '<th class="vl-th">whisper 글자</th><th class="vl-th">채택 문구</th>'
        '<th class="vl-th">지금 글자</th><th class="vl-th">근거</th>'
        "</tr></thead><tbody>" + "".join(body) + "</tbody></table>"
        f'<p class="vl-note">{html.escape(provenance)}</p></details>'
    )


_SHELL_CSS = """
/* ── 껍데기(W-17 B, D57) — 머리줄 44 · 명령 줄 48 · 무대(나머지). 바깥 여백은 16px 하나. ── */
.gradio-container .main.fillable { padding: 0 16px; }
.gradio-container main.contain > .column { gap: 0; }
/* 그라디오 HTML 구성요소의 안쪽 칸은 블록 여백 변수(10·12px)를 단다 — 껍데기 칸에서는
   그 변수를 0으로 둔다(안쪽 포장을 범용으로 덮어쓰지 않는다). 떠 있는 판이 밖으로 나오게
   넘침은 보인다. */
#app-header, #vl-activity-block, .vl-drawer-head-block { --block-padding: 0px; }
#app-header { padding: 0; overflow: visible; z-index: 20; }
/* 탭이 하나뿐이면 탭 막대를 감춘다 — 숨은 「자막」 탭은 그대로라 보이게 되면 막대도 돌아온다. */
.gradio-container .tabs:has(> .tabitem#review-tab) > .tab-wrapper:not(:has(button + button)) {
  display: none; }
.vl-topbar {
  position: relative; display: flex; align-items: center; gap: 14px; min-width: 0;
  height: var(--vl-topbar-h); border-bottom: 1px solid var(--vl-line); color: var(--vl-ink);
  font-size: 13px; line-height: 18px;
}
.vl-wordmark { flex: none; font: 600 16px/22px var(--vl-font-sans); letter-spacing: -0.01em; }
.vl-projbtn {
  display: inline-flex; align-items: center; gap: 6px; min-width: 0; max-width: 240px; height: 28px;
  padding: 0 8px; border: 1px solid transparent; border-radius: 4px; background: transparent;
  color: var(--vl-ink-muted); font: 500 13px/18px var(--vl-font-sans); cursor: pointer;
}
.vl-projbtn:hover { background: var(--vl-surface-hover); color: var(--vl-ink); }
.vl-projbtn > span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.vl-topbar svg, .vl-btn svg { width: 14px; height: 14px; flex: none; }
.vl-grow { flex: 1 1 auto; min-width: 0; }
.vl-steps { display: flex; align-items: center; gap: 4px; min-width: 0; }
.vl-step {
  display: inline-flex; align-items: center; gap: 6px; height: 28px; padding: 0 10px 0 6px;
  border-radius: 4px; color: var(--vl-ink-muted); white-space: nowrap;
  font: 500 13px/18px var(--vl-font-sans);
}
.vl-step .no {
  display: inline-grid; place-items: center; box-sizing: border-box; width: 18px; height: 18px;
  border: 1px solid currentColor; border-radius: 50%; font: 600 11px/1 var(--vl-font-mono);
}
.vl-step .no svg { width: 11px; height: 11px; }
.vl-step.done .no { border-color: var(--vl-success); background: var(--vl-success);
  color: var(--vl-bg); }
/* 지금 단계 — 떠 있는 바탕 + 선 + accent-ink 번호(본점 채움은 주 단추·손잡이·선택한 컷뿐). */
.vl-step.cur { color: var(--vl-ink); background: var(--vl-surface-raised);
  box-shadow: inset 0 0 0 1px var(--vl-line-strong); }
.vl-step.cur .no { border-color: var(--vl-accent-ink); color: var(--vl-accent-ink); }
.vl-step .sub { font: 500 11px/14px var(--vl-font-mono); color: var(--vl-ink-muted); }
.vl-step .sub.r { color: var(--vl-tier-red-ink); }
.vl-step-sep { flex: none; width: 12px; height: 1px; background: var(--vl-line-strong); }
.vl-sr { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%);
  white-space: nowrap; }
/* 연결 방식 — 온라인은 돈이 나갈 수 있으니 경고 바탕, 오프라인은 무채색. */
.vl-mode {
  display: inline-flex; flex: none; align-items: center; gap: 6px; height: 24px; padding: 0 8px;
  border: 1px solid var(--vl-warn); border-radius: 4px; background: var(--vl-warn-soft);
  color: var(--vl-ink); font: 500 12px/16px var(--vl-font-sans); white-space: nowrap;
}
.vl-mode .vl-dot { background: var(--vl-warn); }
.vl-mode.offline { border-color: var(--vl-line-strong); background: var(--vl-surface-raised); }
.vl-mode.offline .vl-dot { background: var(--vl-ink-faint); }
.vl-dot {
  flex: none; width: 8px; height: 8px; border-radius: 50%; background: var(--vl-ink-faint); }
.vl-btn-sm {
  display: inline-flex; align-items: center; gap: 6px; height: 28px; padding: 0 10px;
  font-size: 12px; line-height: 16px; white-space: nowrap; }
.vl-btn[hidden] { display: none; }
.vl-btn-ghost { border-color: transparent; background: transparent; color: var(--vl-ink-muted); }
.vl-btn-ghost:hover:not(:disabled) { background: var(--vl-surface-hover); color: var(--vl-ink); }
.vl-btn-danger { border-color: var(--vl-tier-red-ink); color: var(--vl-tier-red-ink); }
/* 떠 있는 판 — 단축키 표와 활동 기록. 무대 위에 겹칠 뿐 무대를 밀지 않는다. */
.vl-keysheet-pop, .vl-activity-log {
  position: absolute; top: calc(100% + 6px); right: 0; z-index: 60; box-sizing: border-box;
  background: var(--vl-surface-raised); border: 1px solid var(--vl-line-strong);
  border-radius: 6px; box-shadow: var(--vl-shadow-pop); color: var(--vl-ink);
}
.vl-keysheet-pop[hidden], .vl-activity-log[hidden], .vl-activity-empty[hidden] { display: none; }
.vl-keysheet-pop { width: min(440px, calc(100vw - 32px)); padding: 16px; }
.vl-keysheet {
  display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 8px 14px; align-items: center;
  margin: 0; font-size: 13px; line-height: 18px; }
.vl-keysheet dt { display: flex; justify-content: flex-end; gap: 3px; margin: 0; }
.vl-keysheet dd { margin: 0; color: var(--vl-ink); }
.vl-keysheet-note { margin: 12px 0 0; font-size: 12px; line-height: 16px;
  color: var(--vl-ink-muted); }
/* 명령 줄(48px) — 단추는 32px, 나머지는 활동 칸. 글 길이는 높이를 바꾸지 않는다. */
#vl-command-bar {
  display: flex; flex-wrap: nowrap; align-items: center; gap: 8px; height: var(--vl-commandbar-h);
  padding: 0; min-width: 0; }
#review-tab > .column > #vl-command-bar > button {
  flex: none; height: var(--vl-control-h); padding: 0 12px; white-space: nowrap; }
#vl-command-bar > #quick-status { display: none; }
#vl-command-bar > #vl-usage-src { display: none; }
#vl-activity-block { flex: 1 1 0; min-width: 120px; padding: 0; overflow: visible; }
.vl-activity {
  position: relative; display: flex; align-items: center; gap: 10px; box-sizing: border-box;
  height: var(--vl-control-h); min-width: 0; padding: 0 4px 0 10px;
  border: 1px solid var(--vl-line); border-radius: 4px; background: var(--vl-surface);
  color: var(--vl-ink); font-size: 13px; line-height: 18px;
}
.vl-activity .msg {
  flex: 1 1 auto; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.vl-activity[data-vl-kind="reply"] > .vl-dot { background: var(--vl-accent-ink); }
.vl-activity .vl-btn-sm { height: 24px; }
.vl-activity-log { width: min(640px, calc(100vw - 32px)); max-height: min(72vh, 640px);
  overflow: auto; padding: 4px 0; }
.vl-activity-empty { margin: 8px 12px; font-size: 12px; color: var(--vl-ink-muted); }
/* 실사용 줄 — 기록 판 맨 위 고정 줄. 재생기 위에 서지 않고 여기서 여러 줄로 다 보인다. */
.vl-activity-usage {
  margin: 0; padding: 6px 12px 8px; border-bottom: 1px solid var(--vl-line); font-size: 12px;
  line-height: 16px; color: var(--vl-ink); white-space: normal; overflow-wrap: anywhere; }
.vl-activity-usage[hidden] { display: none; }
.vl-activity-log ol { list-style: none; margin: 0; padding: 0; }
.vl-activity-log li {
  display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 10px; margin: 0;
  padding: 6px 12px; border-top: 1px solid var(--vl-line); font-size: 12px; line-height: 16px; }
.vl-activity-log li:first-child { border-top: 0; }
.vl-activity-log time { font-family: var(--vl-font-mono); color: var(--vl-ink-muted); }
/* 서버 쪽 기록(D61) — 지금 도는 외부 프로세스·작업 줄과 서버 터미널 줄. 판이 열린 동안만 찬다. */
.vl-server { border-bottom: 1px solid var(--vl-line); padding-bottom: 8px; }
.vl-server-head { margin: 0; padding: 8px 12px 4px; font-size: 11px; font-weight: 600;
  line-height: 16px; color: var(--vl-ink-muted); }
.vl-server-note { font-weight: 400; color: var(--vl-warn); }
.vl-activity-log .vl-server-rows { list-style: none; margin: 0; padding: 0; }
.vl-activity-log .vl-server-rows li {
  display: block; padding: 3px 12px; border-top: 0; font-size: 12px; line-height: 16px;
  color: var(--vl-ink); overflow-wrap: anywhere; }
.vl-activity-log .vl-server-rows li[data-vl-kind="job"],
.vl-activity-log .vl-server-rows li.vl-server-idle { color: var(--vl-ink-muted); }
.vl-server-log {
  max-height: 200px; overflow-y: auto; margin: 0 12px; padding: 4px 8px;
  border: 1px solid var(--vl-line); border-radius: 4px; background: var(--vl-surface);
  font-family: var(--vl-font-mono); font-size: 11px; line-height: 16px; color: var(--vl-ink); }
.vl-server-log:empty::before { content: "아직 서버 줄이 없습니다."; color: var(--vl-ink-muted); }
.vl-server-line { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 8px;
  white-space: pre-wrap; overflow-wrap: anywhere; }
.vl-server-line time { color: var(--vl-ink-muted); }
.vl-server-line[data-vl-level="warn"] { color: var(--vl-warn); }
.vl-server-line[data-vl-level="error"] { color: var(--vl-danger); }
/* 도는 일(D101) — 띠 안의 「N개 작업」 배지와 서버 줄 점. 새 창·판은 띄우지 않는다. */
.vl-activity-count {
  flex: none; padding: 0 6px; border-radius: 8px; background: var(--vl-accent-soft);
  color: var(--vl-accent-ink); font-size: 11px; line-height: 16px; white-space: nowrap; }
.vl-activity-count[hidden] { display: none; }
.vl-activity[data-vl-kind="server"] > .vl-dot { background: var(--vl-accent-ink); }
/* 서랍 — 그라디오 Sidebar는 열리면 부모에 여백을 줘 무대를 옆으로 민다(폭이 바뀌면 영상이
   다시 맞춰진다). 여기서는 무대 위에 겹치기만 한다. 여닫이는 머리줄 단추와 서랍 머리의
   「닫기」(그리고 Esc)가 한다 — 그라디오의 가장자리 단추는 감춘다. 움직임은 없다. */
.sidebar-parent, .sidebar-parent:has(.sidebar.open) {
  padding-left: 0 !important; padding-right: 0 !important; }
#project-tools, #settings-drawer {
  background: var(--vl-surface); transition: none !important; }
#project-tools { border-right: 1px solid var(--vl-line); }
#settings-drawer { border-left: 1px solid var(--vl-line); }
#project-tools.open, #settings-drawer.open { box-shadow: var(--vl-shadow-pop); }
#project-tools > .toggle-button, #settings-drawer > .toggle-button { display: none; }
#project-tools > .sidebar-content, #settings-drawer > .sidebar-content { padding: 12px 16px 24px; }
.vl-drawer-head {
  display: flex; align-items: center; justify-content: space-between; gap: 8px;
  padding-bottom: 8px; border-bottom: 1px solid var(--vl-line); font-size: 14px; }
/* 막 — 서랍이 열려 있는 동안 나머지 화면 전체(머리줄·승인 단추 포함)를 덮는다. 서랍(1000) 바로
   아래 층이고, 누르면 서랍만 닫힌다 — 덮인 견적으로 승인 단추가 눌리지 않는다. */
.vl-scrim {
  position: fixed; inset: 0; z-index: 999;
  background: color-mix(in srgb, var(--vl-bg) 55%, transparent); }
.vl-scrim[hidden] { display: none; }
/* 견적 칸(#vl-quote) — 분석 견적 문구와 그 아래의 전문 칸. 넓은 화면은 패널 열 위, 1050px
   이하는 명령 줄 바로 아래(재생기 앞). 견적 문구는 승인할 견적이 없으면(승인 단추가 꺼져
   있으면) 숨는다. 전문 칸(#vl-quote-note)은 견적·금액을 실은 살아 있는 승인 알림(거절)만
   말줄임 없이 여러 줄로 다 보이고, 그런 알림이 없으면 숨는다(실사용 줄은 기록 판 고정 줄).
   칸은 제 격자 칸 안에서 자란다. */
#vl-quote { grid-area: quote; min-width: 0; gap: 8px; }
/* 넓은 화면(세 열)에서 견적 칸은 재생기와 같은 행들을 나눠 쓴다 — 견적·범위·초안 카드가 자라면
   재생기 행이 함께 늘어나 타임라인(추천 구간)이 화면 밖으로 밀렸다(1440x900 실측: 재생기
   983px · 레일 y=1198). 칸에 상한을 두고 그 안에서 스크롤한다 — 승인 단추와 견적은 그대로
   보인다(D44). */
@media (min-width: 1051px) {
  #vl-quote { max-height: 46vh; overflow: hidden auto;
    scrollbar-color: var(--vl-scrollbar); }
}
#library-analysis-scope, #vl-quote-note {
  min-width: 0; padding: 8px 10px; border-radius: 6px;
  font-size: 12px; line-height: 16px; white-space: normal; overflow-wrap: anywhere; }
#library-analysis-scope {
  border: 1px solid var(--vl-line-strong); background: var(--vl-surface-raised); }
#vl-quote-note { border: 1px solid var(--vl-warn); background: var(--vl-warn-soft); }
#vl-quote-note p { margin: 0 0 4px; }
#vl-quote-note p:last-child { margin-bottom: 0; }
#review-tab:has(#library-approve:disabled) #library-analysis-scope { display: none; }
/* 초안 결과 카드(W-46a, D100 §7a) — 견적 칸 아래. 경로는 고정폭 글자로 줄바꿈해 다 보인다.
   빨간 경고는 확인하지 않은 비추천이 빠졌다는 뜻이다(막지 않고 알린다). */
#vl-export-card { min-width: 0; padding: 0; }
.vl-export { min-width: 0; padding: 8px 10px; border-radius: 6px; font-size: 12px;
  line-height: 16px; border: 1px solid var(--vl-line-strong); background: var(--vl-surface-raised);
  overflow-wrap: anywhere; }
.vl-export p { margin: 0 0 4px; }
.vl-export video { display: block; width: 100%; max-height: 180px; margin: 4px 0;
  }
.vl-export-warn { padding: 4px 6px; border: 1px solid var(--vl-danger); border-radius: 4px;
  color: var(--vl-danger); }
.vl-export-paths { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 2px 8px;
  margin: 4px 0 0; }
.vl-export-paths dt { color: var(--vl-ink-muted); }
.vl-export-paths dd { margin: 0; font-family: var(--vl-font-mono); font-size: 11px; }
/* 승인 범위(D59 19b) — 견적 문구 바로 위의 두 갈래(전사 + 영상 분석 · 전사만). 승인할 견적이
   없으면 견적 문구와 함께 숨는다(그때 패널이 그 줄까지 올라온다). */
#vl-approval-mode {
  min-width: 0; padding: 6px 10px; border-radius: 6px;
  border: 1px solid var(--vl-line); background: var(--vl-surface-raised); }
#vl-approval-mode .wrap { gap: 4px 12px; }
#review-tab:has(#library-approve:disabled) #vl-approval-mode { display: none; }
/* 다른 귀로 교차 확인(D59 19d) — 승인 범위 바로 아래의 켬·끔. 같은 상자 모양이고, 승인할 견적이
   없으면 함께 숨는다. 분량·금액은 칸이 아니라 견적 문구가 말한다. */
#vl-cross-check {
  min-width: 0; padding: 6px 10px; border-radius: 6px;
  border: 1px solid var(--vl-line); background: var(--vl-surface-raised); }
#review-tab:has(#library-approve:disabled) #vl-cross-check { display: none; }
@media (min-width: 1051px) {
  #review-tab:has(#library-approve:disabled)
    #visual-stage:not(:has(#vl-quote-note)):not(:has(.vl-export)) #vl-ranges {
    grid-row: 1 / 3; }
}
/* 패널(D57 B) — 머리 · 결정 · 판정 · 알림 · 편집 · 접힌 상세. 결정 상자는 빗금 띠를 이고 선다. */
/* 패널 칸은 줄어들지 않는다 — 넘치면 패널 안에서 스크롤한다(결정 상자가 눌려 잘리지 않게). */
#vl-panel > * { flex: none; }
#vl-panel > .vl-row, #vl-panel > .vl-details, #vl-panel > p { margin-top: 0; margin-bottom: 0; }
.vl-panel-head { font-size: 13px; line-height: 18px; overflow-wrap: anywhere; }
.vl-decision {
  overflow: hidden; border: 1px solid var(--vl-tier-red-ink); border-radius: 6px;
  background: var(--vl-surface-raised); }
.vl-decision::before { content: ""; display: block; height: 6px; background:
  repeating-linear-gradient(135deg, var(--vl-tier-red) 0 6px, var(--vl-tier-red-deep) 6px 12px); }
.vl-decision > .vl-marker-reason {
  padding: 10px 12px 0; border: 0; border-radius: 0; background: transparent; }
.vl-decision > .vl-marker-reason > strong { font-size: 14px; }
.vl-decision-state { color: var(--vl-tier-red-ink); font-weight: 600; }
/* 컷 경계와 맞지 않는 제안 — 단추가 컷 전체를 정한다는 한 단락(새 동작 없음, D57 C 고침 1). */
.vl-decision-scope {
  margin: 8px 12px 0; padding: 6px 8px; border: 1px solid var(--vl-warn); border-radius: 4px;
  background: var(--vl-warn-soft); color: var(--vl-ink); font-size: 12px; line-height: 16px; }
.vl-actions-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; padding: 10px 12px 12px; }
/* 제외 제안 대기열(D57 C) — 목록 열 맨 위(라이브러리 전체의 일이다). 「다음」으로 영상을 건너
   다음 제안의 결정 상자(패널 맨 위)를 열고, 일괄 확인은 옛 「자동 제외 확인」 그대로다. 미검토
   수는 정보다(D56). 목록을 내려도 붙어 있다 — 바깥 그림자는 목록 바탕색으로 틈을 가린다. */
.vl-queue {
  display: grid; grid-template-columns: minmax(0, 1fr); gap: 8px; padding: 10px 12px;
  border: 1px solid var(--vl-line); border-radius: 6px; background: var(--vl-surface-raised); }
#visual-stage .vl-thumbs > .vl-queue {
  position: sticky; top: 0; z-index: 1; box-shadow: 0 -8px 0 8px var(--vl-surface); }
.vl-queue-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.vl-queue-count { margin: 0; font-size: 13px; line-height: 18px; color: var(--vl-ink); }
.vl-queue-count b { color: var(--vl-tier-red-ink); font-weight: 600; }
.vl-queue-head > .vl-btn { flex: none; }
.vl-queue > .vl-btn-danger { width: 100%; }
/* 안내 줄은 영상 이름을 댄다 — 띄어쓰기 없는 긴 파일 이름도 열 안에서 접는다(D57 C 고침 2). */
.vl-queue-note { margin: 0; font-size: 12px; line-height: 16px; color: var(--vl-ink-muted);
  overflow-wrap: anywhere; }
/* Final Cut 키워드 이름 — 한 덩어리로(줄 가운데서 쪼개지 않는다). */
.vl-kw { white-space: nowrap; font-weight: 600; color: var(--vl-ink); }
/* 사용 | 제외 — 눌린 쪽이 지금 상태다(색 하나가 아니라 바탕·글자·밑줄 셋). 옛 토글 그대로. */
.vl-seg {
  display: inline-flex; overflow: hidden; border: 1px solid var(--vl-line-strong);
  border-radius: 4px; background: var(--vl-surface-raised); }
.vl-seg > .vl-btn {
  min-width: 64px; height: 28px; border: 0; border-radius: 0; background: transparent;
  color: var(--vl-ink-muted); font-size: 13px; }
.vl-seg > .vl-btn + .vl-btn { border-left: 1px solid var(--vl-line-strong); }
.vl-seg > .vl-btn[aria-pressed="true"] {
  background: var(--vl-accent-soft); color: var(--vl-ink); cursor: default;
  box-shadow: inset 0 -2px 0 var(--vl-accent-ink); }
.vl-field { display: flex; flex-direction: column; gap: 4px; }
.vl-memo { flex-wrap: nowrap; align-items: flex-end; }
.vl-memo > .vl-input { flex: 1 1 auto; min-width: 0; }
/* 여러 줄 메모 — 높이는 칸 안에서만 자란다(상한 뒤로는 칸 안 스크롤). 결정 상자를 밀지 않는다. */
.vl-memo-text { min-height: 4.5em; max-height: 9em; resize: vertical; overflow-y: auto;
  font: inherit; line-height: 1.4; }
"""
"""머리줄·명령 줄·활동 칸·서랍·견적 칸·패널 결정 상자 — 색은 토큰만 부른다."""

WORKBENCH_CSS = (
    """
/* V2F1 작업대 — 색은 작업대 토큰(`--vl-*`, ui/design_tokens.py)만 부른다. 밝은·어두운 두 벌이
   같은 이름을 쓰므로 여기에는 테마 분기가 없다. 본점 채움(--vl-accent)은 주 단추·경계 손잡이·
   선택한 컷 셋뿐이고, 글자·선의 강조는 --vl-accent-ink다(D57). */
#visual-player, #visual-workbench, #caption-workbench { max-width: 100%; }
.vl-note {
  color: var(--vl-ink-muted); font-size: 12px; margin: 4px 0 0;
}
/* 무대(W-17 B, D57) — 목록은 무대 높이 전체, 타임라인은 재생기와 패널 아래를 함께 덮는다:
   "thumbs player panel" "thumbs timeline timeline" "foot foot foot". 패널 열 위의 한 칸(quote)은
   분석 견적이다 — 승인 단추가 묶는 문구라 말줄임 없이 보이되 무대 위쪽을 밀지 않는다(승인할
   견적이 없으면 숨고 패널이 그 자리를 쓴다).
   구성요소 껍데기(vl-stage-cell)와 본문 뿌리는 display:contents로 격자에
   참여한다. 무대 치수는 여기 한 곳 — 흩어진 재선언이 마지막에 이겨 목록 칸이
   180px로 묶이고 재생기·타임라인이 화면 아래로 밀리던 결함(D50).
   --vl-stage-top은 실행본(__vlFitStage)이 실측해 덮어쓴다 — 머리줄·명령 줄 높이는
   CSS가 모르므로 무대 높이를 화면에서 무대 위쪽을 뺀 값으로 맞춘다. */
#visual-stage {
  --vl-stage-top: 320px;
  /* 바닥값 = 타임라인 렠 칸(≈200) + 각주·간격(≈30) + 자막 줄·안내(≈60) + 최소 영상(≈110).
     560이면 800px 높이 창(무대 위쪽 279–335px 실측)에서 렠이 6–62px 잘렸다(D50). */
  --vl-stage-min-h: 400px;
  --vl-list-col: minmax(240px, 280px);
  --vl-panel-col: minmax(280px, 320px);
  --vl-stage-gap: 8px;
  --vl-thumb-art-w: 64px; --vl-thumb-art-h: 36px;
  --vl-name-lines: 2;
  --vl-scrollbar: var(--vl-line-strong) var(--vl-surface);
  display: grid;
  grid-template-columns: var(--vl-list-col) minmax(0, 1fr) var(--vl-panel-col);
  grid-template-rows: auto minmax(0, 1fr) auto auto var(--vl-statusline-h);
  grid-template-areas:
    "thumbs player quote" "thumbs player ranges" "thumbs player panel" "thumbs timeline timeline"
    "foot foot foot";
  height: max(var(--vl-stage-min-h), calc(100vh - var(--vl-stage-top)));
  gap: var(--vl-stage-gap); align-items: stretch; margin-top: 0; min-width: 0;
}
#visual-stage .vl-stage-cell { display: contents; }
/* 그라디오 실측(ROOT 1440x900): 구성요소 껍데기(.vl-stage-cell) 안에 네이티브
   .html-container 포장이 남아 칸 배치를 가로막는다 — 재생기 x116 w276 · 목록이
   옆으로 밀려나고 패널이 아래로 떨어지던 결함. 무대 **안에서만** 편다(진단
   셀렉터와 같은 효과). 범용 `.html-container` 덮어쓰기는 금지 — 다른 화면의
   포장을 해치지 않는다. */
#visual-stage .html-container { display: contents !important; }
#visual-workbench:not([data-vl-empty]) { display: contents; }
#visual-workbench[data-vl-empty] { grid-column: 1 / -1; }
#vl-thumbs { grid-area: thumbs; }
/* 카드 열은 minmax(0,1fr) — 그냥 1fr(=minmax(auto,1fr))이면 카드 최소 폭 아래로
   줄지 못해 목록 안에 가로 스크롤이 생긴다. 스크롤바 색을 주지 않으면 어두운
   테마에서 흰 띠로 남는다. */
#visual-stage .vl-thumbs {
  display: grid; grid-template-columns: minmax(0, 1fr); align-content: start;
  gap: 6px; padding: 8px;
  max-height: 100%; min-height: 0; overflow: hidden auto; scrollbar-color: var(--vl-scrollbar);
  border-radius: 6px; border: 1px solid var(--vl-line);
  background: var(--vl-surface);
}
#visual-stage .vl-thumbs > .vl-row { grid-column: 1 / -1; }
/* 필터는 한 줄짜리 묶음 단추 — 300px 열에서 네 번째 단추가 둘째 줄로 떨어지던 자리. */
#visual-stage .vl-thumbs > .vl-row {
  margin: 0; gap: 0; flex-wrap: nowrap; overflow: hidden;
  border: 1px solid var(--vl-line-strong); border-radius: 4px; background: var(--vl-surface-raised);
}
#visual-stage .vl-thumbs > .vl-row > .vl-btn {
  flex: 1 1 auto; min-width: 0; height: 28px; padding: 0 6px; border: 0; border-radius: 0;
  border-left: 1px solid var(--vl-line-strong); background: transparent;
  color: var(--vl-ink-muted); font-size: 12px; white-space: nowrap;
}
#visual-stage .vl-thumbs > .vl-row > .vl-btn:first-child { border-left: 0; }
#visual-stage .vl-thumbs > .vl-row > .vl-btn[aria-pressed="true"] {
  background: var(--vl-accent-soft); color: var(--vl-ink);
  box-shadow: inset 0 -2px 0 var(--vl-accent-ink);
}
.vl-cards { display: grid; grid-template-columns: minmax(0, 1fr); gap: 6px; min-width: 0; }
/* 목록 머리 — 「영상 N개 · 촬영 시각 순」(정렬 단추는 없다, D53). */
.vl-list-head { grid-column: 1 / -1; margin: 0; font-size: 12px; line-height: 16px;
  color: var(--vl-ink-muted); }
.vl-list-head b { color: var(--vl-ink); font-weight: 600; }
/* 테두리·배경은 클래스 단계에 둔다 — 선택(.vl-thumb-sel) 강조가 무대 범위
   선택자에 덮이지 않는다. 배치만 무대 범위에서 정한다. */
.vl-thumb {
  display: flex; border-radius: 6px; text-align: left; cursor: pointer;
  border: 1px solid var(--vl-line);
  background: var(--vl-surface-raised);
  color: var(--vl-ink);
}
.vl-thumb:hover { background: var(--vl-surface-hover); }
/* 선택은 색 하나가 아니라 셋 — 옅은 강조 바탕 · accent-ink 테두리 · 안쪽 1px 선. */
.vl-thumb-sel, .vl-thumb-sel:hover {
  border-color: var(--vl-accent-ink); background: var(--vl-accent-soft);
  box-shadow: inset 0 0 0 1px var(--vl-accent-ink);
}
/* 영상 전체가 제외일 때만 흐리게 — 그림과 이름만(띠·수는 그대로 읽힌다). */
.vl-thumb-ex .vl-thumb-art, .vl-thumb-ex .vl-thumb-name { opacity: 0.55; }
/* 카드(D57 B) — 그림 64×36(길이를 얹는다) · 이름 두 줄 · 수와 배지 한 줄 · 영상 전체 띠. */
#visual-stage .vl-thumb {
  display: grid; grid-template-columns: var(--vl-thumb-art-w) minmax(0, 1fr);
  column-gap: 8px; row-gap: 6px; align-items: start;
  min-width: 0; width: 100%; padding: 8px; font: inherit; }
.vl-thumb-art {
  position: relative; display: block; overflow: hidden; border-radius: 4px;
  width: var(--vl-thumb-art-w); height: var(--vl-thumb-art-h); background: var(--vl-surface-sunken);
}
.vl-thumb-art img {
  display: block; width: 100%; height: 100%; object-fit: cover; background: var(--vl-video-well); }
/* 세로 그림(W-26)은 잘라 채우지 않고 담는다 — 64×36에 cover면 9:16 그림의 위아래 대부분이
   잘려 무엇을 찍었는지 안 보인다. 표지는 그림을 받은 뒤 실행본(VL.fitThumb)이 단다. */
.vl-thumb-art img[data-vl-fit="contain"] { object-fit: contain; }
.vl-thumb-art .dur {
  position: absolute; right: 2px; bottom: 2px; padding: 0 3px; border-radius: 2px;
  background: var(--vl-video-overlay); color: var(--vl-on-video-overlay);
  font: 500 11px/13px var(--vl-font-mono); font-variant-numeric: tabular-nums;
}
.vl-thumb-missing {
  display: flex; align-items: center; justify-content: center; width: 100%; height: 100%;
  color: var(--vl-ink-muted); font-size: 11px; line-height: 13px;
}
/* 긴 파일 이름은 두 줄까지 보이고 말줄임 — 전체 이름은 카드 title 말풍선(D50). */
#visual-stage .vl-thumb-name {
  font-size: 13px; font-weight: 500; color: var(--vl-ink);
  max-width: none; white-space: normal; overflow-wrap: anywhere; line-height: 18px;
  display: -webkit-box; -webkit-line-clamp: var(--vl-name-lines); -webkit-box-orient: vertical;
  overflow: hidden; }
#visual-stage .vl-thumb-meta {
  grid-column: 1 / -1; display: flex; flex-wrap: wrap; align-items: center;
  justify-content: space-between; gap: 4px 8px; min-width: 0; min-height: 18px; }
.vl-cnts {
  display: inline-flex; gap: 8px; white-space: nowrap;
  font: 500 11px/14px var(--vl-font-mono); font-variant-numeric: tabular-nums;
}
.vl-cnt.r { color: var(--vl-tier-red-ink); }
.vl-cnt.h { color: var(--vl-tier-yellow-ink); }
.vl-cnt.g { color: var(--vl-tier-green-ink); }
/* 0은 흐린 글자 — ink-faint는 선택 카드(accent-soft) 위 3.9:1이라 ink-muted다. */
.vl-cnt.z { color: var(--vl-ink-muted); }
/* 영상 전체 띠 — 등급마다 길이대로. 빨강은 빗금, 보류는 점선 결, 분석 전은 선 색. 결정이 끝난
   제외 제안은 반쯤 흐려 아직 열린 제안이 눈에 띈다. */
.vl-mbar {
  grid-column: 1 / -1; display: flex; gap: 1px; height: 6px; overflow: hidden;
  border-radius: 2px; background: var(--vl-surface-sunken);
}
.vl-mbar i { display: block; height: 100%; min-width: 1px; }
.vl-mbar .g { background: var(--vl-tier-green); }
.vl-mbar .y { background: var(--vl-tier-yellow); }
.vl-mbar .r { background: repeating-linear-gradient(135deg,
  var(--vl-tier-red-ink) 0 2px, var(--vl-tier-red-deep) 2px 4px); }
.vl-mbar .r.done { opacity: 0.5; }
/* 컷 경계와 맞지 않는 제외 제안 — 속 빈 빨강. 남은 수에 들지 않고 덮는 컷의 결정을 따른다는
   표지다(결정한 제안의 흐린 빗금과도, 열린 제안의 빗금과도 다르다, D57 C 고침 2). 선은
   tier-red-ink — 띠 바탕(sunken) 위 두 테마 모두 4.5:1 이상(시험). */
.vl-mbar .r.loose { background: transparent; box-shadow: inset 0 0 0 1px var(--vl-tier-red-ink); }
.vl-mbar .h { background:
  repeating-linear-gradient(90deg, var(--vl-tier-yellow-ink) 0 2px, transparent 2px 4px); }
.vl-mbar .n { background: var(--vl-line); }
/* 배지는 줄 안에서 쪼개지지 않고 통째로 다음 줄로 넘어간다 — 잘림 없음. 알약 모양은
   쓰지 않는다(6px보다 둥근 것 없음), 글자는 11px가 바닥이다. */
.vl-badge {
  display: inline-block; font-size: 11px; font-weight: 500; line-height: 14px;
  padding: 1px 6px; border-radius: 2px;
  background: transparent;
  color: var(--vl-ink-muted);
  border: 1px solid var(--vl-line-strong); white-space: nowrap;
}
.vl-badge-warn { border-color: var(--vl-warn); color: var(--vl-warn); }
/* 카드 배지 하나(D57 B) — 분석 상태는 상태 색(돌고 있음 · 경고 · 실패), 기다림은 무채색. */
.vl-badge[data-vl-badge="run"] { border-color: var(--vl-accent-ink); color: var(--vl-accent-ink); }
.vl-badge[data-vl-badge="warn"] { border-color: var(--vl-warn); color: var(--vl-warn); }
.vl-badge[data-vl-badge="err"] { border-color: var(--vl-danger); color: var(--vl-danger); }
#visual-player { display: contents; }
#visual-preview { grid-area: player; min-width: 0; min-height: 0;
  display: flex; flex-direction: column; }
.vl-video-surface { position: relative; flex: 1 1 auto; min-height: 0; display: flex; }
/* 영상은 두 테마 모두 어두운 우물 위에 놓인다 — 밝은 바탕에 영상을 띄우지 않는다. */
#visual-preview video {
  width: 100%; height: auto; flex: 1 1 auto; min-height: 0;
  background: var(--vl-video-well); object-fit: contain; }
.vl-current-caption, #visual-note { flex: none; }
#visual-note, .vl-footnote { overflow-wrap: anywhere; }
#vl-panel { grid-area: panel; max-height: 100%; min-height: 0; overflow: hidden auto;
  box-sizing: border-box; scrollbar-color: var(--vl-scrollbar);
  display: flex; flex-direction: column; gap: 8px; }
#visual-timeline { grid-area:timeline; min-width:0; overflow-x:auto; padding-top:4px;
  scrollbar-color: var(--vl-scrollbar); }
/* 네 영역(W-46b, D100 §7b) — 목록 · 재생기+자막 · 추천 구간(레일 + 「내보낼 부분」 목록) · 재생
   타임라인. 그 밖의 도구(컷 편집 패널 · 제외 제안 대기열 · 컷 경계 손잡이)는 「고급 도구」를
   펼칠 때만 보인다 — 숨길 뿐 지우지 않는다(무대의 `data-vl-advanced`는 실행본이 단다).
   견적·승인 칸(#vl-quote)은 언제나 보인다(D44). */
#vl-ranges { grid-area: ranges; min-width: 0; min-height: 0; overflow: hidden auto;
  display: flex; flex-direction: column; gap: 6px; padding: 8px 10px; box-sizing: border-box;
  border: 1px solid var(--vl-line); border-radius: 6px; background: var(--vl-surface);
  scrollbar-color: var(--vl-scrollbar); }
.vl-ranges-head { display: flex; gap: 8px; align-items: baseline; font-size: 13px; }
.vl-ranges-sum { color: var(--vl-ink-muted); font-size: 12px;
  font-variant-numeric: tabular-nums; }
.vl-range-list { display: flex; flex-direction: column; gap: 2px; min-height: 0; }
.vl-range-row { display: flex; gap: 8px; align-items: baseline; width: 100%; text-align: left;
  border: 0; border-left: 3px solid var(--vl-tier-green); border-radius: 2px; padding: 3px 6px;
  background: none; color: var(--vl-ink); font-size: 12px; cursor: pointer;
  font-variant-numeric: tabular-nums; }
.vl-range-row:hover { background: var(--vl-surface-hover); }
.vl-range-row.human { border-left-color: var(--vl-accent-ink); }
.vl-range-row small { color: var(--vl-ink-muted); font-size: 11px; }
.vl-range-note { margin: 0; color: var(--vl-ink-muted); font-size: 12px; }
#vl-advanced-toggle { align-self: flex-start; margin-top: auto; }
#visual-stage:not([data-vl-advanced="open"]) #vl-panel,
#visual-stage:not([data-vl-advanced="open"]) .vl-thumbs > .vl-queue,
#visual-stage:not([data-vl-advanced="open"]) .vl-handle { display: none; }
@media (min-width: 1051px) {
  #visual-stage[data-vl-advanced="open"] {
    grid-template-rows: auto minmax(0, 1fr) minmax(0, 2fr) auto var(--vl-statusline-h); }
}
.vl-markbar { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; margin-top: 6px; }
.vl-markbar .vl-btn { padding: 4px 10px; }
.vl-markbar .vl-kbd { margin-left: 2px; }
.vl-range-readout { min-width: 14ch; flex: 1 1 14ch; font-size: 12px; color: var(--vl-ink-muted);
  font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
.vl-mark-btn.green:not(:disabled) { border-color: var(--vl-tier-green);
  box-shadow: inset 3px 0 0 var(--vl-tier-green); }
.vl-mark-btn.yellow:not(:disabled) { border-color: var(--vl-tier-yellow-ink);
  box-shadow: inset 3px 0 0 var(--vl-tier-yellow); }
.vl-mark-btn.red:not(:disabled) { border-color: var(--vl-tier-red);
  box-shadow: inset 3px 0 0 var(--vl-tier-red); }
/* 레일 위 사람 표시 — 등급 막대 바로 위의 단색 띠(자동 등급 막대와 갈린다). 찍는 중인 범위는
   레일 전체 높이의 옅은 띠와 양 끝 손잡이(끌면 시작·끝만 옮긴다 — 컷 경계를 제안하지 않는다).
   「내보낼 부분」 띠는 레일 맨 아래 — 렌더 계획이 남길 범위 그대로다. */
.vl-human-mark { position: absolute; top: calc(var(--vl-rec-top) - 4px); height: 3px;
  border-radius: 1px; pointer-events: none; }
.vl-human-mark.green { background: var(--vl-tier-green); }
.vl-human-mark.yellow { background: var(--vl-tier-yellow-ink); }
.vl-human-mark.red { background: var(--vl-tier-red); }
.vl-range-band { position: absolute; top: 0; bottom: 0; pointer-events: none; z-index: 1;
  background: color-mix(in srgb, var(--vl-accent-soft) 70%, transparent);
  box-shadow: inset 0 0 0 1px var(--vl-accent-ink); }
.vl-range-handle { position: absolute; top: 0; bottom: 0; width: 10px; margin-left: -5px;
  padding: 0; border: 0; z-index: 4; cursor: ew-resize; background: none; }
.vl-range-handle::after { content: ""; position: absolute; top: 0; bottom: 0; left: 4px;
  width: 2px; background: var(--vl-accent-ink); }
.vl-range-handle:focus-visible { outline: 2px solid var(--vl-accent-ink); }
.vl-kept-strip { position: absolute; left: 0; right: 0; bottom: 2px; height: 8px;
  pointer-events: none; background: var(--vl-surface-raised); border-radius: 2px; }
.vl-kept { position: absolute; top: 0; bottom: 0; border-radius: 1px;
  background: var(--vl-tier-green); }
.vl-kept.human { background: var(--vl-accent-ink); }
/* 상태 줄(24px) — 왼쪽 각주는 한 줄 말줄임(전체 글은 title), 오른쪽은 단축키 안내. */
#vl-statusline { grid-area: foot; }
.vl-statusline {
  display: flex; align-items: center; justify-content: space-between; gap: 16px;
  min-width: 0; height: var(--vl-statusline-h); overflow: hidden;
}
#vl-footnote {
  flex: 1 1 auto; min-width: 0; margin: 0; font-size: 12px; line-height: 16px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
#visual-note { margin: 2px 0 0; }
.vl-keys {
  display: inline-flex; flex: none; align-items: center; gap: 12px; white-space: nowrap;
  font-size: 12px; line-height: 16px; color: var(--vl-ink-muted);
}
.vl-keys > span { display: inline-flex; align-items: center; gap: 3px; }
.vl-keys > .vl-btn-sm { height: 20px; padding: 0 6px; }
.vl-kbd {
  display: inline-flex; align-items: center; justify-content: center; box-sizing: border-box;
  min-width: 20px; height: 18px; padding: 0 4px; margin: 0;
  border: 1px solid var(--vl-line-strong); border-bottom-width: 2px; border-radius: 2px;
  background: var(--vl-surface-raised); color: var(--vl-ink);
  font: 500 11px/14px var(--vl-font-mono); box-shadow: none;
}
/* 폭 상한은 Blocks(fill_width=True)가 푼다 — 여기는 가로 넘침만 막는다. */
.gradio-container, .gradio-container .app { min-width: 0; max-width: 100vw; }
/* 정리 탭 세로 예산(D50) — 그라디오 .row > * 의 min-width 160·flex 1이 단추 라벨을
   두 줄로 접어 64px를 먹었다. 단추는 제 글자 폭만큼만 차지한다. 실측 DOM:
   div#review-tab.tabitem > div.column > div.row > button(명령 줄, W-17 B). */
#review-tab .column { gap: 6px; }
#review-tab > .column > .row > button { flex: 0 1 auto; min-width: 0; padding: 6px 12px; }
#library-analysis-scope p { margin: 0; }
#visual-video::cue {
  color: var(--vl-on-video-overlay); background: var(--vl-video-overlay); font-size:20px; }
.vl-source-overlay { position:absolute; bottom:44px; left:50%; transform:translateX(-50%);
 max-width:90%; padding:5px 10px; border-radius:4px; text-align:center; white-space:pre-wrap;
 background: var(--vl-video-overlay); color: var(--vl-on-video-overlay);
 font-size:20px; line-height:1.4; pointer-events:none; }
.vl-source-overlay[hidden] { display:none; }
/* 범례는 한 덩어리로 줄을 넘긴다 — 좁은 타임라인에서 글자마다 접히지 않는다. */
/* 범례 다섯 칸은 「 · 」 글자 대신 8px 틈으로 가른다 — 속 빈 빨강 칸을 더해도 1280에서 운송
   줄이 한 줄로 남는다(D57 C 고침 4, 탐침: 글자 「 · 」로는 시계가 둘째 줄로 내려갔다). */
.vl-legend { flex: 1 0 auto; color: var(--vl-ink-muted); font-size:12px; white-space: nowrap;
  display: flex; align-items: center; column-gap: 8px; }
.vl-ruler { position:absolute; inset:0 0 auto; height:var(--vl-ruler-h);
 border-bottom:1px solid var(--vl-line); }
/* 눈금 글자 — 첫 글자는 왼쪽, 끝 글자는 오른쪽에 붙여 양 끝이 잘리지 않는다. */
.vl-ruler span { position:absolute; font: 500 11px/15px var(--vl-font-mono);
 color: var(--vl-ink-muted); transform:translateX(-50%); white-space:nowrap; }
.vl-ruler span:first-child { transform:none; padding-left:3px; }
.vl-ruler span:last-child { transform:translateX(-100%); padding-right:3px; }
.vl-recommendation { position:absolute; top:var(--vl-rec-top); height:var(--vl-rec-h);
 border:0; border-radius:2px; box-sizing:border-box;
 background: var(--vl-surface-raised); color: var(--vl-ink);
 overflow:hidden; white-space:nowrap; text-overflow:ellipsis;
 font-size:11px; font-weight:500; min-width:3px; text-align:left; padding:1px 6px; }
/* 3단 등급(D51) — 색만으로 가르지 않는다: 빗금(빨강)·속 빈 점선 테두리(보류)·글자 표지가
   함께 간다. 세 채움은 밝기 단도 다르다(흑백에서도 갈린다 — 디자인 README Tiers). */
.vl-recommendation.green { background: var(--vl-tier-green); color: var(--vl-on-tier-green);
 box-shadow: inset 0 2px 0 var(--vl-tier-green-ink); }
.vl-recommendation.yellow { background: var(--vl-tier-yellow); color: var(--vl-on-tier-yellow);
 box-shadow: inset 0 2px 0 var(--vl-tier-yellow-ink); }
.vl-recommendation.red { background:
  repeating-linear-gradient(135deg, var(--vl-tier-red) 0 6px, var(--vl-tier-red-deep) 6px 12px);
 color: var(--vl-on-tier-red);
 box-shadow: inset 0 0 0 1px var(--vl-tier-red-ink), inset 0 2px 0 var(--vl-tier-red-ink); }
/* 판정 보류는 결함이 아니라 AI 근거가 없는 자리다 — 채우지 않고 점선 테두리만. 2px인 까닭:
   Chromium은 1.5px 점선을 1px로 그려 점이 흐려진다(W-17 검증 실측). */
.vl-recommendation.yellow.hold { background: transparent; color: var(--vl-tier-yellow-ink);
 border: 2px dotted var(--vl-tier-yellow-ink); box-shadow: none; }
/* 컷 경계와 맞지 않는 제외 제안 — 속 빈 빨강 점선(남은 수에 들지 않는다, D57 C 고침 2). */
.vl-recommendation.red.loose { background: transparent; color: var(--vl-tier-red-ink);
 border: 2px dashed var(--vl-tier-red-ink); box-shadow: none; }
/* 「다음」·막대 클릭으로 고른 등급 구간 — accent-ink 바깥선(고른 컷과 같은 선택 하나). */
.vl-recommendation.vl-rec-sel { outline: 2px solid var(--vl-accent-ink); outline-offset: 1px; }
.vl-tier-key.green { color: var(--vl-tier-green-ink); }
.vl-tier-key.yellow { color: var(--vl-tier-yellow-ink); }
.vl-tier-key.red { color: var(--vl-tier-red-ink); }
.vl-tier-key.hold { color: var(--vl-tier-yellow-ink); }
/* 속 빈 빨강(컷 경계와 맞지 않는 제안)의 범례 — 레일·미니바의 점선 테두리 모양(D57 C 고침 4).
   `color: inherit` — 그라디오의 `.prose *` 글자색이 덮으면 테두리가 본문 잉크가 된다. */
.vl-tier-key.loose { color: var(--vl-tier-red-ink); }
.vl-key-hollow { display: inline-block; width: 0.8em; height: 0.8em; box-sizing: border-box;
  color: inherit; border: 1px dashed currentColor; vertical-align: -1px; }
/* 「기준 바꿈」(W-46a) — 작업공간 추천 기준이 기본값과 다를 때만 범례 끝에 선다. */
.vl-criteria-badge { margin-left: 8px; padding: 0 6px; border: 1px solid var(--vl-warn);
  border-radius: 4px; color: var(--vl-warn); font-size: 11px; line-height: 16px; }
.vl-criteria-badge[hidden] { display: none; }
.vl-badge-green { border-color: var(--vl-tier-green-ink); color: var(--vl-tier-green-ink); }
.vl-badge-red { border-color: var(--vl-tier-red-ink); color: var(--vl-tier-red-ink); }
.vl-badge-amber { border-color: var(--vl-warn); color: var(--vl-warn); }
/* 자막 시각 확인(D55)은 등급이 아니라 상태다 — 상태 색을 쓴다(등급 색은 등급에만). */
.vl-badge[data-vl-sync].vl-badge-red { border-color: var(--vl-danger); color: var(--vl-danger); }
.vl-badge[data-vl-sync].vl-badge-green {
  border-color: var(--vl-success); color: var(--vl-success); }
.vl-marker-reason { padding:10px; border-radius:4px; background: var(--vl-surface-raised);
 border: 1px solid var(--vl-line); border-left: 3px solid var(--vl-line-strong); }
.vl-marker-reason.green { border-left-color: var(--vl-tier-green-ink); }
.vl-marker-reason.yellow { border-left-color: var(--vl-tier-yellow-ink); }
.vl-marker-reason.red { border-left-color: var(--vl-tier-red-ink); }
.vl-marker-reason.red > strong { color: var(--vl-tier-red-ink); }
.vl-rec-pending { position:absolute; top:calc(var(--vl-rec-top) + 1px); left:10px;
 color: var(--vl-ink-muted); font-size:12px; }
/* 그라디오 button{font:inherit}이 16px·22.4px 줄과 큰 단추 여백을 물려 22px 칸에서
   글자가 6px 잘렸다(D50 실측) — 이 층만 제 글자 크기·줄 높이·여백을 정한다. */
.vl-source-caption { position:absolute; top:var(--vl-cue-top); height:var(--vl-cue-h);
  font-size:12px; line-height:1.3; padding:0 6px;
  border:0; border-radius:2px;
  background: var(--vl-caption-chip); color: var(--vl-on-caption-chip);
  white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.vl-source-caption-editor { border:1px solid var(--vl-line-strong); padding:10px; }
/* 다른 귀(D59 19e) — 고르지 않은 후보가 있는 자막은 오른쪽 위 모서리 삼각형(warn), 다른 귀가 고친
   자막은 아래 2px 안쪽 선(accent-ink). 가상 원소·안쪽 그림자라 칸의 자리·크기는 그대로다(B단계
   레일 좌표). 자막 칩(caption-chip) 위 두 테마 모두 3:1 이상(시험). */
.vl-source-caption[data-vl-ear="alt"]::after { content: ""; position: absolute; top: 0; right: 0;
  width: 0; height: 0; border-top: 7px solid var(--vl-warn); border-left: 7px solid transparent; }
.vl-source-caption[data-vl-ear="replaced"] { box-shadow: inset 0 -2px 0 var(--vl-accent-ink); }
/* 목록·편집기의 표지 — 다른 귀 후보와 「확인 필요」는 warn, 다른 귀로 고친 줄은 accent-ink. */
.vl-badge[data-vl-ear="alt"], .vl-badge[data-vl-suspect] {
  border-color: var(--vl-warn); color: var(--vl-warn); }
.vl-badge[data-vl-ear="replaced"] {
  border-color: var(--vl-accent-ink); color: var(--vl-accent-ink); }
.vl-badge[data-vl-truth-stratum], .vl-badge[data-vl-truth] {
  border-color: var(--vl-line-strong); color: var(--vl-ink); }
.vl-truth { border-left: 3px solid var(--vl-line-strong); padding: 2px 0 2px 8px;
  margin: 0 0 8px; }
.vl-truth-line .vl-btn { margin: 0 6px 0 0; }
.vl-ear { border-left: 3px solid var(--vl-line-strong); padding: 2px 0 2px 8px; margin: 0 0 8px; }
.vl-ear-line { display: flex; gap: 8px; align-items: baseline; justify-content: space-between; }
/* 긴 후보(실측 480자)가 문구 칸·「저장」을 밀어내지 않게 줄 높이를 묶고, 넘치면 줄 안에서
   스크롤한다. */
.vl-ear-text { min-width: 0; overflow-wrap: anywhere; max-height: 4.5em; overflow-y: auto; }
.vl-ear-line > .vl-btn { flex: none; }
/* 말소리 빈칸 재전사(D59 19f) — 빈칸 단추는 서로 띄우고, 견적 문구는 승인이 대조하는 글이라
   상자로 묶는다(긴 문구도 칸 안에서 줄바꿈 — 가로로 넘치지 않는다). */
[data-vl-refill] .vl-btn { margin: 0 6px 6px 0; }
/* 시각 맞춤 제안(D59 19h) — 들어 볼 줄은 왼쪽 선으로 묶고, 긴 글자·이유는 칸 안에서 줄바꿈한다. */
[data-vl-snap] .vl-btn { margin: 0 6px 6px 0; }
.vl-snap-cue { border-left: 3px solid var(--vl-line-strong); padding: 2px 0 2px 8px;
  margin: 0 0 8px; overflow-wrap: anywhere; }
.vl-refill-quote { border: 1px solid var(--vl-line-strong); border-radius: 4px; padding: 6px 8px;
  background: var(--vl-surface-raised); overflow-wrap: anywhere; }
.vl-audio-strip { position:absolute; top:var(--vl-audio-top); left:0; right:0;
 height:var(--vl-audio-h); padding:3px 8px; line-height:14px;
 background: var(--vl-surface); color: var(--vl-ink-muted); font-size:11px; box-sizing:border-box; }
/* 컷 띠의 이름표는 썸네일 그림 위에 얹힌다 — 영상 위 띠와 같은 어두운 바탕(두 테마 같음). */
.vl-int span { background: var(--vl-video-overlay); color: var(--vl-on-video-overlay);
 padding:2px 6px; font-size:11px; }
/* 컷 이름표 — 번호와 원본 범위(Mono). 좁은 컷에서는 띠 안에서 말줄임하고, 제외한 컷은 번호에
   취소선을 긋는다(흐림만으로 가르지 않는다, D57 C). */
.vl-int > span {
  display: inline-block; max-width: 100%; box-sizing: border-box; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap; vertical-align: top;
  font-family: var(--vl-font-mono); font-variant-numeric: tabular-nums; }
/* 번호의 굵은 글자는 이름표 글자색 그대로 — 그라디오 밝은 테마는 b에 어두운 글자색을 박는다. */
.vl-int > span > b { color: inherit; font-weight: 600; }
.vl-int-ex > span > b { text-decoration: line-through; }

/* 자막 무대 — 왼쪽 재생기(영상·파형·이동) · 오른쪽 큐 목록+편집기(주 저장·확정).
   세팅·등록·실행은 무대 위 접힌 아코디언으로, 1440x900에서 주 조작이
   문서 스크롤 없이 먼저 들어온다(ROOT 실측 재배치). */
#caption-stage {
  display: grid;
  grid-template-columns: minmax(0, 7fr) minmax(300px, 5fr);
  grid-template-areas: "player cues";
  gap: 12px; align-items: start; margin-top: 4px;
}
#caption-stage .vl-stage-cell { display: contents; }
#caption-stage .html-container { display: contents !important; }
#caption-player { grid-area: player; }
#caption-workbench { grid-area: cues; }
/* 렠 층 좌표는 여기 한 곳(D50) — 202px 렠이 800–900px 창에서 재생기를 130–250px로 눌렀다.
   실행본은 층의 left·width만 정하고 세로 좌표는 CSS만 안다. 층 사이는 4px씩 띄운다
   (등급 20+22 · 컷 46+36 · 오디오 86+20 · 자막 110+22 — 디자인 Timeline, D57). */
.vl-rail {
  --vl-rail-h: 150px; --vl-ruler-h: 16px;
  --vl-rec-top: 20px; --vl-rec-h: 22px;
  --vl-int-top: 46px; --vl-int-h: 36px; --vl-handle-h: 28px;
  --vl-audio-top: 86px; --vl-audio-h: 20px;
  --vl-cue-top: 110px; --vl-cue-h: 22px;
  position: relative; height: var(--vl-rail-h); border-radius: 4px; margin-top: 4px;
  touch-action: none; cursor: pointer; overflow: visible;
  background: var(--vl-surface-sunken);
  border: 1px solid var(--vl-line);
}
.vl-int {
  position: absolute; top: var(--vl-int-top); height: var(--vl-int-h);
  border: 1px solid var(--vl-line-strong); border-radius: 2px;
  background-color: var(--vl-surface-raised); background-size: auto 100%;
  background-repeat: repeat-x; min-width: 2px; padding: 0;
}
/* 선택한 컷 — accent-ink 바깥선 + 본점 옅은 덮개. 안쪽 그림자는 썸네일 그림 위에 그려진다. */
.vl-int-sel {
  outline: 2px solid var(--vl-accent-ink); outline-offset: 1px;
  background-color: var(--vl-accent-soft);
  box-shadow: inset 0 0 0 100vmax color-mix(in srgb, var(--vl-accent) 28%, transparent);
}
.vl-int-ex { opacity: 0.45; }
.vl-int-needs { border-bottom: 3px solid var(--vl-warn); }
/* 재생 머리는 양옆 1px 테두리(casing)와 함께 — 어느 등급 막대 위에서도 읽힌다. */
.vl-playhead {
  position: absolute; top: 0; bottom: 0; width: 2px;
  background: var(--vl-playhead); pointer-events: none;
  box-shadow: -1px 0 0 var(--vl-playhead-casing), 1px 0 0 var(--vl-playhead-casing);
}
/* 손잡이는 선택한 컷 띠 안 가운데에 선다 — 눈금자·등급 막대를 가리지 않는다. */
.vl-handle {
  position: absolute;
  top: calc(var(--vl-int-top) + (var(--vl-int-h) - var(--vl-handle-h)) / 2);
  width: 12px; height: var(--vl-handle-h); border-radius: 2px; box-sizing: border-box;
  background: var(--vl-accent); border: 1px solid var(--vl-on-accent);
  cursor: ew-resize; padding: 0; z-index: 2;
}
.vl-handle:focus-visible { outline: 2px solid var(--vl-accent-ink); outline-offset: 2px; }
.vl-handle:disabled {
  background: var(--vl-line-strong); cursor: default;
}
.vl-draftmark {
  position: absolute; top: -4px; bottom: -4px; width: 2px;
  background: var(--vl-draft); pointer-events: none; z-index: 3;
}
.vl-transport { display:flex; gap:8px; align-items:center; margin-top:2px; flex-wrap:wrap; }
.vl-clock {
  font-family: var(--vl-font-mono); font-variant-numeric: tabular-nums; font-weight: 500;
  color: var(--vl-ink); font-size: 13px;
}
.vl-panel {
  border: 1px solid var(--vl-line); border-radius: 6px;
  padding: 10px; min-width: 0;
  background: var(--vl-surface);
}
.vl-row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin-top: 8px; }
.vl-btn {
  border: 1px solid var(--vl-line-strong);
  background: var(--vl-surface-raised);
  color: var(--vl-ink);
  border-radius: 4px; padding: 5px 12px; font-size: 13px; font-weight: 500; cursor: pointer;
}
.vl-btn:hover:not(:disabled) { background: var(--vl-surface-hover); }
/* 눌린 토글 — 옅은 강조 바탕 · ink 글자 · accent-ink 밑줄 2px(색 하나에 기대지 않는다). */
.vl-btn[aria-pressed="true"], .vl-btn[aria-pressed="true"]:hover:not(:disabled) {
  background: var(--vl-accent-soft); border-color: var(--vl-accent-ink); color: var(--vl-ink);
  box-shadow: inset 0 -2px 0 var(--vl-accent-ink);
}
.vl-btn:disabled {
  background: var(--vl-surface); border-color: var(--vl-line); color: var(--vl-ink-faint);
  box-shadow: none; cursor: default;
}
.vl-btn-primary { background: var(--vl-accent); border-color: var(--vl-accent);
  color: var(--vl-on-accent); }
.vl-btn-primary:hover:not(:disabled) {
  background: var(--vl-accent-hover); border-color: var(--vl-accent-hover); }
.vl-input {
  border: 1px solid var(--vl-line-strong); border-radius: 4px;
  padding: 4px 8px; font-size: 13px; width: 100%; box-sizing: border-box;
  background: var(--vl-surface-raised);
  color: var(--vl-ink);
}
.vl-input::placeholder { color: var(--vl-ink-faint); }
.vl-input-narrow { width: 9ch; }
.vl-kv {
  font-size: 12px; color: var(--vl-ink); margin: 2px 0;
  font-variant-numeric: tabular-nums;
}
/* <small>은 기본 `smaller`라 12px 안에서 9.6px가 된다 — 글자 바닥 11px에 묶는다. */
.vl-kv small { font-size: 11px; }
.vl-draft {
  border: 1px dashed var(--vl-draft); border-radius: 6px; padding: 8px; margin-top: 8px;
  background: var(--vl-surface-raised);
}
.vl-details { margin-top: 8px; font-size: 12px; }
.vl-details summary { cursor: pointer; color: var(--vl-ink-muted); }
.vl-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.vl-th, .vl-td {
  text-align: left; padding: 3px 6px;
  border-bottom: 1px solid var(--vl-line); vertical-align: top;
}
.vl-th { color: var(--vl-ink-muted); }
.vl-footnote {
  color: var(--vl-ink); font-size: 12px;
  margin: 6px 0 0; min-height: 1em;
}
.cap-overlay {
  min-height: 1.7em; margin-top: 4px; padding: 4px 10px; font-size: 15px;
  background: var(--vl-surface); border-radius: 6px;
  color: var(--vl-ink);
  border: 1px solid var(--vl-line);
}
.cap-overlay:not(.cap-overlay-on) { display: none; }
.cap-cues {
  max-height: 34vh; overflow-y: auto; display: flex; flex-direction: column; gap: 4px;
  margin-top: 8px; border-radius: 6px; padding: 6px;
  border: 1px solid var(--vl-line);
}
.cap-row {
  display: flex; gap: 8px; border: none; background: none; padding: 4px 6px;
  border-radius: 4px; cursor: pointer; text-align: left; align-items: baseline;
  width: 100%; color: var(--vl-ink);
}
.cap-row:hover { background: var(--vl-surface-hover); }
.cap-row-sel { background: var(--vl-accent-soft); }
.cap-row-time {
  flex: none; font-size: 11px; color: var(--vl-ink-muted);
  font-family: var(--vl-font-mono); font-variant-numeric: tabular-nums; width: 12ch;
}
.cap-row-text { font-size: 14px; color: var(--vl-ink); }
.cap-row-manual .cap-row-text::after {
  content: " ·수동"; color: var(--vl-ink-muted); font-size: 11px; }
.cap-editor { margin-top: 8px; }
.cap-editor-fields { display: flex; gap: 8px; flex-wrap: wrap; }
.cap-editor-fields label {
  display: flex; flex-direction: column; gap: 2px; font-size: 11px;
  color: var(--vl-ink-muted); min-width: 0;
}
.cap-editor-fields label:first-child { flex: 1 1 220px; }
#visual-video::cue,.vl-source-overlay { font-size:clamp(20px,1.8vw,28px); }
.vl-current-caption { width:100%; min-height:36px; padding:5px 10px; margin-top:4px;
 color: var(--vl-ink); background: var(--vl-surface); border:1px solid var(--vl-line);
 border-radius:4px; font-size:18px; line-height:1.3; text-align:center; white-space:pre-wrap;
 cursor:pointer; }
.vl-current-caption:disabled { color: var(--vl-ink-muted); font-size:14px; cursor:default; }
#vl-panel [data-vl-act="toggle-sel"] { flex: 1 1 100%; }
"""
    + _SHELL_CSS
    + """
/* 반응형 — 넓은 쪽(1050) 다음 좁은 쪽(860) 순서여야 좁은 규칙이 이긴다(D50:
   예전엔 860 뒤에 무대 재선언이 와서 860 규칙이 죽어 있었다). 좁은 화면은 무대
   높이를 화면에 묶지 않고 문서 스크롤에 맡긴다.
   두 분기점 모두 견적 칸이 명령 줄 바로 아래(재생기 앞)다 — 승인 단추를 누르는 자리에서
   견적이 함께 보인다(옛 배치와 같다). 승인할 견적·전문이 없으면 그 줄은 비어 높이가 없다.
   ≤1050: 두 열 — 목록이 왼쪽 열 전체, 타임라인은 재생기 바로 아래(사용자 결정 6). 목록은
   크기 봉쇄(contain: size)로 행 높이를 부풀리지 않고 제 칸 안에서 스크롤한다.
   ≤860: 한 열 — (견적) · 재생기 · 타임라인 · 목록(가로 띠) · 패널 · 상태 줄(사용자 결정 6). */
@media (max-width: 1050px) {
  #visual-stage { height: auto; grid-template-columns: minmax(180px, 220px) minmax(0, 1fr);
    grid-template-rows: none;
    grid-template-areas: "quote quote" "thumbs player" "thumbs timeline" "thumbs ranges"
      "thumbs panel" "foot foot"; }
  #visual-stage .vl-thumbs { max-height: none; contain: size; }
  #vl-panel { max-height: 64vh; }
  #visual-preview video { flex: none; height: clamp(300px, 48vh, 540px); }
  #vl-command-bar { flex-wrap: wrap; height: auto; padding: 8px 0; }
}
@media (max-width: 860px) {
  #visual-stage { grid-template-columns: minmax(0, 1fr);
    grid-template-areas: "quote" "player" "timeline" "ranges" "thumbs" "panel" "foot"; }
  #caption-stage { grid-template-columns: minmax(0, 1fr); grid-template-areas: "player" "cues"; }
  #visual-stage .vl-thumbs { max-height: none; overflow: visible; contain: none; }
  #visual-stage .vl-thumbs > .vl-queue { position: static; box-shadow: none; }
  #visual-stage .vl-cards {
    display: flex; overflow: auto hidden; padding-bottom: 4px;
    scrollbar-color: var(--vl-scrollbar); }
  #visual-stage .vl-cards > .vl-thumb { flex: none; width: 236px; }
  #visual-preview video { height: clamp(230px, 45vh, 420px); }
  .cap-cues { max-height: 28vh; }
  .vl-topbar { flex-wrap: wrap; height: auto; min-height: var(--vl-topbar-h); padding: 6px 0;
    gap: 6px 8px; }
  .vl-step-sep { display: none; }
  .vl-step:not(.cur) .lbl, .vl-step:not(.cur) .sub { display: none; }
  .vl-topbar .vl-btn-label { position: absolute; width: 1px; height: 1px; overflow: hidden;
    clip-path: inset(50%); white-space: nowrap; }
  #vl-activity-block { flex: 1 1 100%; order: 3; }
  .vl-statusline .vl-keys > span { display: none; }
}

"""
)
_CLIENT_LIB_BODY = r"""
var VL = window.__vlLib;
if (!VL) {
  VL = { version: 1 };
  VL.gcd = function (a, b) {
    a = Math.abs(a); b = Math.abs(b);
    while (b) { var t = a % b; a = b; b = t; }
    return a || 1;
  };
  VL.rat = function (n, d) {
    if (!isFinite(n) || !isFinite(d) || d === 0) { return null; }
    if (d < 0) { n = -n; d = -d; }
    var g = VL.gcd(n, d);
    return [n / g, d / g];
  };
  function isSafe(n) { return typeof n === "number" && isFinite(n) && Math.abs(n) < 9e15; }
  VL.parse = function (text) {
    if (text === null || text === undefined) { return null; }
    var s = String(text).trim();
    var m = /^(-?\d+)(?:\/(-?\d+))?$/.exec(s);
    if (m) {
      var n = parseInt(m[1], 10);
      var d = m[2] !== undefined ? parseInt(m[2], 10) : 1;
      return VL.rat(n, d);
    }
    var dec = /^(-?)(\d+)(?:\.(\d+))?$/.exec(s);
    if (dec) {
      var frac = dec[3] || "";
      var base = parseInt(dec[2], 10);
      if (!isSafe(base) || frac.length > 12) { return null; }
      var den = Math.pow(10, frac.length);
      var num = base * den + (frac ? parseInt(frac, 10) : 0);
      return VL.rat(dec[1] === "-" ? -num : num, den);
    }
    return null;
  };
  VL.add = function (a, b) {
    if (!a || !b) { return null; }
    return VL.rat(a[0] * b[1] + b[0] * a[1], a[1] * b[1]);
  };
  VL.sub = function (a, b) {
    if (!a || !b) { return null; }
    return VL.rat(a[0] * b[1] - b[0] * a[1], a[1] * b[1]);
  };
  VL.mul = function (a, b) {
    if (!a || !b) { return null; }
    return VL.rat(a[0] * b[0], a[1] * b[1]);
  };
  VL.div = function (a, b) {
    if (!a || !b || b[0] === 0) { return null; }
    return VL.rat(a[0] * b[1], a[1] * b[0]);
  };
  VL.cmp = function (a, b) { return a[0] * b[1] - b[0] * a[1]; };
  VL.toFloat = function (a) { return a ? a[0] / a[1] : 0; };
  VL.str = function (a) { return a ? (a[1] === 1 ? String(a[0]) : a[0] + "/" + a[1]) : "0"; };
  VL.pct = function (part, whole) {
    var f = VL.div(part, whole);
    return f ? (VL.toFloat(f) * 100) : 0;
  };
  VL.fmt = function (a, places) {
    var f = VL.toFloat(a);
    return f.toFixed(places === undefined ? 3 : places);
  };
  // 작업대 모델 JSON(W-24 세션 G) — 뿌리의 첫 자식 [data-vl-model-json]의 글자다. 뿌리의
  // data-vl-model 속성은 옛 모양이다(시험 하네스가 아직 쓴다) — 칸이 없으면 속성을 읽는다.
  VL.modelHolder = function (root) {
    var holder = root ? root.firstElementChild : null;
    return holder && holder.hasAttribute && holder.hasAttribute("data-vl-model-json")
      ? holder : null;
  };
  VL.modelRaw = function (root) {
    if (!root || !root.getAttribute) { return null; }
    var holder = VL.modelHolder(root);
    if (holder) { return holder.textContent || null; }
    return root.getAttribute("data-vl-model");
  };
  // 카드 그림의 방향(W-26) — 세로 그림(높이 > 너비)이면 `data-vl-fit="contain"`을 단다. 크기는
  // 그림을 받은 뒤에야 알므로 이미 받았으면 바로, 아니면 load 때 단다. 정사각형은 가로다(D66).
  VL.fitThumb = function (img) {
    if (!img || !img.setAttribute) { return; }
    function mark() {
      if (img.naturalHeight > img.naturalWidth) { img.setAttribute("data-vl-fit", "contain"); }
    }
    if (img.complete && img.naturalWidth) { mark(); return; }
    if (img.addEventListener) { img.addEventListener("load", mark, { once: true }); }
  };
  window.__vlLib = VL;
}
"""


def _workbench_state(component_id: str, state_key: str) -> str:
    """공용 실행본 머리 — 상태 저장소·서버 호출·늦은 응답 버림(두 작업대 공용).

    서버 응답은 `echoEpoch`가 **지금 문서의 모델 epoch**와 같을 때만 적용한다
    (A→B→A 전환 뒤 도착한 낡은 응답은 조용히 버린다 — 잘못된 프로젝트에
    떨어지지 않는다). `html`이 있을 때만 `props.value`를 갈아끼우므로, 제안처럼
    상태를 안 바꾸는 응답은 화면을 다시 그리지 않고 초안 상자만 고친다.
    """
    return f"""
      var W = window;
      var state = W.__vl{state_key}State;
      if (!state) {{
        state = {{ epoch: null, selectedId: null, selectedPlan: null, draft: null,
          loop: null, lastMsg: "", downloads: null }};
        W.__vl{state_key}State = state;
      }}
      var props = W.__vl{state_key}Props;
      var server = W.__vl{state_key}Server;
      function modelEpoch() {{
        var el = doc.getElementById("{component_id}");
        if (!el) {{ return null; }}
        var raw = W.__vlLib.modelRaw(el);
        if (!raw) {{ return null; }}
        try {{ return JSON.parse(raw).epoch; }} catch (err) {{ return null; }}
      }}
      function humanLine(text) {{
        // 각주에는 사람 문장만 — `코드 조각`(식별자·리비전)은 접힌 상세가 싣는다
        // (적용 응답 뒤 커밋된 상세가 정확한 리비전을 다시 그린다). 오류 문장은
        // 원문 그대로 간다(숨김 없음).
        return String(text || "").replace(/`[^`]*`/g, "")
          .replace(/\\s+([.!?])/g, "$1").replace(/\\s{{2,}}/g, " ").trim();
      }}
      function call(name, payload, note, onResponse) {{
        // 사건의 신원은 **이 배선이 잡은 모델**(클로저)에서 온다 — 지금 문서의
        // 값으로 바꿔 다시 찍지 않는다. 재렌더 뒤 남아 있는 옛 클로저 사건은
        // 옛 epoch로 나가 응답이 조용히 버려진다(화면 효과 0, A5 실측).
        payload.filter = state.filter || "all";
        payload.epoch = model ? model.epoch : modelEpoch();
        if (model) {{
          payload.signature = model.signature;
          payload.token = model.token;
        }} else if (state.model) {{
          payload.signature = state.model.signature;
          payload.token = state.model.token;
        }}
        // 요청이 나간 순간의 신선도 — 이 배선(세대)·이 epoch·이 뿌리가 응답
        // 때까지 살아 있어야만 어떤 효과도 낸다. 성공 분기의 echoEpoch 검사는
        // null 검사 **뒤**라서 null·거부 응답은 아무 데서도 막히지 않았다(A9
        // 실측: 화면이 바뀐 뒤 도착한 늦은 실패 안내문이 새 화면의 각주·
        // lastMsg를 덮어썼다). 가드를 모든 응답 분기 **앞**에 둔다. 신선한
        // 화면의 진짜 오류·빈 응답 안내는 그대로 산다.
        var epochAtCall = payload.epoch;
        function lateRequest() {{
          return staleWire() || modelEpoch() !== epochAtCall;
        }}
        msg(note || "서버에 보내는 중입니다…");
        // 버린 답의 「보내는 중」은 남기지 않는다(D64) — 다시 그린 화면은 그 각주를 되살려
        // 그리므로 답이 버려지면 영원히 「보내는 중」이었다. 뒤에 나간 요청이 각주를 맡았으면
        // 건드리지 않는다.
        var sendSerial = (state.sendSerial = (state.sendSerial || 0) + 1);
        var sendingLine = state.lastMsg;
        function dropped() {{
          if (state.sendSerial === sendSerial && state.lastMsg === sendingLine) {{
            msg("화면이 그 사이 다시 그려져 이 답은 반영하지 않았습니다. 결과가 안 보이면 " +
              "새로고침하세요.");
          }}
        }}
        // 새 요청이 시작되면 이 화면에 남아 있던 내려받기 링크는 물러난다 —
        // 편집 뒤 낡은 머리의 링크가 살아 있지 않게(자막 확정 흐름).
        state.downloads = null;
        if (!server || !server[name]) {{ msg("서버 함수가 없습니다: " + name); return; }}
        server[name](payload).then(function (res) {{
          if (lateRequest()) {{ dropped(); return; }}
          if (!res) {{ msg("서버 응답이 비어 있습니다."); return; }}
          if (res.ok === false) {{
            // 신선한 요청의 진짜 오류다 — 공개 생산자의 오류 응답은 echoEpoch 없이
            // {{ok:false, error}}로 온다(실측: 늦은 머리·빈 글자 안내가 echo 검사에서
            // 갈려 저장 중 표시만 남던 결함). 신선도는 lateRequest가 이미 판정했다;
            // 오류는 상태를 못 바꾸니 원문 그대로 보인다(숨김 없음).
            msg(res.error || "실패했습니다.");
            if (onResponse) {{ onResponse(res); }}
            return;
          }}
          var now = modelEpoch();
          if (res.echoEpoch !== now) {{ dropped(); return; }}
          if (!res.ok) {{
            msg(res.error || "실패했습니다.");
            if (onResponse) {{ onResponse(res); }}
            return;
          }}
          msg(humanLine(res.message));
          if (res.downloads && res.downloads.length &&
            typeof appendDownloads === "function") {{
            appendDownloads(res.downloads);
          }}
          if (onResponse) {{ onResponse(res); }}
          if (res.html && props) {{
            props.value = res.html;
            // 직접 대입은 watch를 부르지 않는다(설치본 실측 — 세터는 렌더
            // 마이크로태스크만 돌린다). 대입 직후 스케줄러로 커밋 뒤 재배선을
            // 환기한다; 놓치는 커밋은 부팅 때부터 상시인 관찰자가 잡는다.
            if (W.__vl{state_key}Rewire) {{ W.__vl{state_key}Rewire(); }}
          }}
        }}, function (err) {{
          if (lateRequest()) {{ dropped(); return; }}
          msg("서버 오류입니다. " + (err && err.message ? err.message : String(err)));
        }});
      }}
"""


VISUAL_BOOTSTRAP_JS = (
    _CLIENT_LIB_BODY
    + r"""
(function () {
  var W = window;
  // 전역 단축키 상수(D50) — 함수 안에 숫자를 두지 않는다. ←/→는 렠 손잡이의
  // 프레임 이동 몫이라 전역 표에 넣지 않는다. 글자 키는 ev.code로 견준다 —
  // 한글 입력기에서는 ev.key가 자모('ㅏ')로 오기 때문이다.
  var VL_SEEK_SECONDS = 5;
  var VL_FALLBACK_FRAME_SECONDS = 1 / 30;
  // 입력기 조합 중 keydown은 keyCode 229로 온다(isComposing을 안 싣는 브라우저 대비).
  var VL_IME_PROCESS_KEYCODE = 229;
  var VL_EDITABLE = 'input, textarea, select, [contenteditable=""], ' +
    '[contenteditable="true"], [role="textbox"]';
  // 열린 서랍 안에 초점이 있으면 재생 단축키를 쓰지 않는다 — 서랍을 열면 「닫기」에 초점이 가는데,
  // 거기서 누른 스페이스가 막 뒤의 영상을 틀면 안 된다(W-17 B).
  var VL_OPEN_DRAWER = ".sidebar.open";
  // 보이는 첫 영상 하나만 조작한다 — 숨은 탭의 영상은 offsetParent가 null이다.
  var VL_SHORTCUT_VIDEOS = [["visual-video", "vl-footnote"], ["caption-video", "cap-msg"]];
  var VL_SHORTCUT_ACTIONS = { "Space": "toggle", "KeyK": "toggle", "KeyJ": "back",
    "KeyL": "forward", "Comma": "frameBack", "Period": "frameForward" };
  var VL_PLAY_FAILED_TEXT = "재생할 수 없습니다: ";
  // 시작·끝 표시 키(W-46b) — 재생 키와 같은 막기(입력 칸·조합·반복·수정키·열린 서랍)를 지난다.
  var VL_RANGE_KEYS = { "KeyI": "in", "KeyO": "out", "Digit1": "recommended",
    "Digit2": "normal", "Digit3": "not_recommended", "Escape": "cancel" };
  var VL_RANGE_MARKS = ["recommended", "normal", "not_recommended", "clear"];
  if (!W.__vlWireVisual) {
    W.__vlWireVisual = function (root, doc) {
"""
    + _workbench_state("visual-workbench", "VisualWorkbench")
    + r"""
      var VL = W.__vlLib;
      if (!root || !root.getAttribute) { return; }
      var rawModel = VL.modelRaw(root);
      if (!rawModel) { return; }
      var model;
      try { model = JSON.parse(rawModel); } catch (err) { return; }
      // 배선 세대 표식 — 이 뿌리에 묶은 **모델 문자열 그대로**를 남긴다.
      // 설치본 렌더기는 같은 태그의 뿌리를 그 자리에서 morph한다(원소 신원은
      // 유지한 채 data-vl-model과 동적 자식만 갈아친다 — A8 실측) — 참/거짓
      // 표식이나 원소 신원만으로는 바뀐 모델을 알 수 없다. 커밋된 모델이 다르면
      // 다시 묶는다(아래 wireCommitted 참조).
      root.__vlVisualWiredModel = rawModel;
      var firstWire = state.epoch !== model.epoch;
      if (state.selectionToken !== model.selectionToken) {
        state.selectedId = null; state.marker = null; state.focusCard = null;
        state.selectionToken = model.selectionToken;
      }
      state.model = model;
      // 필터는 탭의 것이다(D64) — 필터를 실어 온 요청의 답만 값을 싣는다. 틱·명령 줄 단추의 다시
      // 그리기는 빈 값이라 탭이 고른 필터가 그대로 남는다(예전엔 「전체」로 되돌아갔다).
      state.filter = model.filter || state.filter || "all";
      state.epoch = model.epoch;
      if (firstWire) { state.draft = null; }
      // 배선 세대 — 이 클로저의 사건은 이 배선이 살아 있는 동안만 유효하다.
      // 같은 작업공간 재렌더든 작업공간 전환이든 새 배선이 오면 옛 클로저는
      // 공용 상태·문서·서버를 건드리기 **전에** 여기서 물러난다(A6 실측: 붙잡아
      // 둔 옛 A1 클릭이 새 A2 화면의 선택·이동·DOM을 바꾸던 결함).
      var wireSerial = (W.__vlVisualWireSerial = (W.__vlVisualWireSerial || 0) + 1);
      state.wire = wireSerial;
      function staleWire() {
        return state.wire !== wireSerial || state.epoch !== model.epoch ||
          doc.getElementById("visual-workbench") !== root;
      }

      function msg(text) {
        state.lastMsg = foot(text);
      }
      function foot(text) {
        // 상태 줄은 한 줄(24px)이다 — 마크다운 굵게 표지는 걷고, 잘린 글 전체는 title에 둔다.
        var line = String(text || "").replace(/\*\*/g, "");
        var el = doc.getElementById("vl-footnote");
        if (el) { el.textContent = line; el.title = line; }
        return line;
      }
      // 수를 말하는 각주는 그때의 수다 — `lastMsg`로 남기지 않는다. 다음 그리기가 대기열과 같은
      // 값으로 갈아 쓰거나 지운다(D57 C 고침 3: 일괄 확인 뒤에도 옛 수가 다시 그려졌다).
      function countFoot(text) {
        state.liveFoot = foot(text);
        state.lastMsg = "";
      }
      function liveFootnote() {
        var loose = model.redUnmaterialized || 0;
        var text = loose > 0 ? "제외 제안 " + loose + "개가 컷 경계와 맞지 않아 그 구간을 덮는" +
          " 컷의 결정을 따릅니다. 따로 정하려면 「현재 위치에서 나누기」로 컷을 나누세요." : "";
        var el = doc.getElementById("vl-footnote");
        var shown = el ? el.textContent : "";
        if (text || (state.liveFoot && shown === state.liveFoot)) { countFoot(text); }
      }
      function say(text) {
        var el = doc.getElementById("visual-note");
        if (el) { el.textContent = text || ""; }
      }
      function esc(value) {
        return String(value === null || value === undefined ? "" : value)
          .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
          .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
      }
      function ratOf(value) { return VL.parse(value); }
      function videoEl() { return doc.getElementById("visual-video"); }
      function sourceInfo(id) { return (model.sources || {})[id] || null; }
      function curSourceId() {
        if (state.keptPlaying && state.playingSource) { return state.playingSource; }
        var card = selectedCard();
        if (card) { return card.sourceId; }
        return model.selectedSourceId || null;
      }
      function curSource() { return sourceInfo(curSourceId()); }
      function originRat() {
        var s = curSource();
        return s && s.origin ? ratOf(s.origin) : [0, 1];
      }
      function railOf() {
        var s = curSource();
        if (!s || !s.rail || state.timelineMode !== "result") { return s ? s.rail : null; }
        var at = [0, 1];
        var intervals = s.rail.intervals.filter(function(i) {
          return i.sel !== "excluded";
        }).map(function(i) {
          var next = Object.assign({}, i);
          next.sourceStart = i.start; next.sourceEnd = i.end;
          next.start = VL.str(at); at = VL.add(at, VL.sub(ratOf(i.end), ratOf(i.start)));
          next.end = VL.str(at); return next;
        });
        return Object.assign({}, s.rail, { intervals: intervals, duration: VL.str(at) });
      }
      function sourceTime(t) {
        if (state.timelineMode !== "result") { return t; }
        var rail = railOf();
        for (var i = 0; rail && i < rail.intervals.length; i += 1) {
          var row = rail.intervals[i];
          if (VL.cmp(t, ratOf(row.end)) <= 0) {
            return VL.add(ratOf(row.sourceStart), VL.sub(t, ratOf(row.start)));
          }
        }
        return t;
      }
      function displayTime(t) {
        if (state.timelineMode !== "result") { return t; }
        var rail = railOf();
        for (var i = 0; rail && i < rail.intervals.length; i += 1) {
          var row = rail.intervals[i];
          if (VL.cmp(t, ratOf(row.sourceStart)) < 0) { return ratOf(row.start); }
          if (VL.cmp(t, ratOf(row.sourceEnd)) <= 0) {
            return VL.add(ratOf(row.start), VL.sub(t, ratOf(row.sourceStart)));
          }
        }
        return rail ? ratOf(rail.duration) : t;
      }
      function localOf(t) { return VL.sub([Math.round(t * 1e6), 1000000], originRat()); }
      function playheadLocal() {
        var v = videoEl();
        return v ? localOf(v.currentTime) : null;
      }
      function clearSourceCaptions(v) {
        var overlay = doc.getElementById('visual-source-caption');
        if (overlay) { overlay.textContent = ''; overlay.hidden = true; }
        var current = doc.getElementById('vl-current-caption');
        if (current) {
          current.textContent = '재생하면 원본 자막이 여기에 표시됩니다.';
          current.disabled = true;
        }
        if (v && v._vlSourceTrack) {
          v._vlSourceTrack.mode = 'hidden';
          while (v._vlSourceTrack.cues && v._vlSourceTrack.cues.length) {
            v._vlSourceTrack.removeCue(v._vlSourceTrack.cues[0]);
          }
          v._vlSourceTrack.mode = 'disabled'; v._vlSourceTrackKey = null;
        }
      }
      function paintSourceCaptions() {
        if (staleWire()) { return; }
        var v = videoEl(), info = curSource();
        var overlay = doc.getElementById('visual-source-caption');
        if (!v || !info || !info.src || v.getAttribute('data-vl-src') !== info.src) {
          clearSourceCaptions(v); return;
        }
        var cues = info.subtitles || [];
        var enabled = state.sourceCaptionEnabled !== false;
        var sourceSecond = VL.toFloat(localOf(v.currentTime));
        var activeCues = cues.filter(function(cue) {
          return VL.toFloat(ratOf(cue.start)) <= sourceSecond &&
            sourceSecond < VL.toFloat(ratOf(cue.end));
        });
        var current = doc.getElementById('vl-current-caption');
        if (current) {
          current.textContent = activeCues.length ? activeCues.map(function(c) {
            return String(c.text || '');
          }).join('\n') : '발화 구간에서 원본 자막을 확인할 수 있습니다.';
          current.disabled = !activeCues.length;
        }
        if (typeof v.addTextTrack === 'function' && typeof W.VTTCue === 'function') {
          if (!v._vlSourceTrack) {
            v._vlSourceTrack = v.addTextTrack('subtitles','원본 자막','ko');
          }
          var key = curSourceId() + ':' + info.origin + ':' +
            (info.transcriptRevision || JSON.stringify(cues));
          if (key !== v._vlSourceTrackKey) {
            clearSourceCaptions(v);
            if (current) {
              current.textContent = activeCues.map(function(c) {
                return String(c.text || '');
              }).join('\n');
              current.disabled = !activeCues.length;
            }
            cues.forEach(function(cue) {
              var start = VL.toFloat(VL.add(ratOf(cue.start), originRat()));
              var end = VL.toFloat(VL.add(ratOf(cue.end), originRat()));
              if (isFinite(start) && isFinite(end) && start >= 0 && end > start) {
                var nativeCue = new W.VTTCue(start,end,String(cue.text || '')
                  .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'));
                nativeCue.line = -2; nativeCue.position = 50;
                nativeCue.size = 90; nativeCue.align = 'center';
                v._vlSourceTrack.addCue(nativeCue);
              }
            });
            v._vlSourceTrackKey = key;
          }
          v._vlSourceTrack.mode = enabled ? 'showing' : 'disabled';
          if (overlay) { overlay.textContent = ''; overlay.hidden = true; }
          return;
        }
        if (!overlay) { return; }
        var local = VL.toFloat(localOf(v.currentTime));
        var text = enabled && v.readyState !== 0 ? cues.filter(function(cue) {
          return VL.toFloat(ratOf(cue.start)) <= local && local < VL.toFloat(ratOf(cue.end));
        }).map(function(cue) { return String(cue.text || ''); }).join('\n') : '';
        overlay.textContent = text; overlay.hidden = !text;
      }
      function seekTo(local) {
        var v = videoEl();
        var target = VL.add(local, originRat());
        if (!v || !target) { return; }
        v.currentTime = VL.toFloat(target);
        paintPlayhead(); paintSourceCaptions();
      }
      // 컷을 먼저 푼다 — 고른 영상의 컷(쪽과 무관, D64) → 이 쪽 목록의 컷 → 서버가 고른 컷 →
      // 「다음」이 기억한 컷. 영상 카드는 첫 컷의 ID를 빌려 쓰므로 그 넷보다 먼저 보면 컷이 영상
      // 전체 범위로 풀린다(검증 MAJOR 2: 컷 #1의 결정 상자 위에 0~202초가 섰다). 영상 카드 값은
      // 넷이 모를 때만 쓴다. 쪽 넘김이 없어 쪽은 늘 1이다 — 첫 쪽(24컷) 밖 영상의 컷은 고른 영상의
      // 컷(`viewCards`)에만 있다.
      function knownCuts() { return (model.viewCards || []).concat(model.cards || []); }
      function findCut(id) {
        var cuts = knownCuts();
        for (var i = 0; i < cuts.length; i += 1) {
          if (cuts[i].id === id) { return cuts[i]; }
        }
        if (model.selectedCard && model.selectedCard.id === id) { return model.selectedCard; }
        // 「다음」이 고른 컷은 이 쪽 목록 밖일 수 있다 — 서버가 그 컷을 고른 값을 같은 리비전
        // 동안만 기억한다(2초 틱이 다른 영상을 고른 값을 그려도 초점이 남는다, D57 C).
        var focus = state.focusCard;
        if (focus && focus.card.id === id && focus.plan === model.basePlanId &&
            focus.rev === model.revisionId) { return focus.card; }
        return null;
      }
      function findCard(id) {
        var cut = findCut(id);
        if (cut) { return cut; }
        var videos = model.mediaCards || [];
        for (var i = 0; i < videos.length; i += 1) {
          if (videos[i].id === id) { return videos[i]; }
        }
        return null;
      }
      function selectedId() {
        // 로컬 선택은 **같은 계획 안**에서만 이긴다(F2 계약 — 같은 화면 생명의
        // 0 RPC 선택 보존). 계획 세대가 바뀐 커밋(재계획) 뒤엔 옛 ID가 다른
        // 구간에 재주조될 수 있으니 커밋된 선택(서버가 매핑으로 보존한 것)을
        // 따른다(실측: 같은 ID가 다른 조각에 붙어 머리 밖 구간을 가리켰다).
        return state.selectedId && state.selectedPlan === model.basePlanId &&
          findCard(state.selectedId) ? state.selectedId : model.selectedId;
      }
      function selectedCard() {
        var id = selectedId();
        if (model.selectedCard && model.selectedCard.id === id) { return model.selectedCard; }
        return findCard(id);
      }
      function stateLabel(s) {
        return ({ ready: "준비됨", preparing: "준비 중", failed: "준비 실패",
          stale: "낡음", missing: "아직 없음" })[s] || s;
      }
      function speechLabel(s) {
        return ({ speech_detected: "발화 있음", unconfirmed_empty: "발화 없음(미측정)",
          audio_absent: "소리 없음", analysis_failed: "발화 분석 실패" })[s] || "";
      }
      function recLabel(r) {
        return ({ recommended: "사용 권고", normal: "보통",
          review_required: "검토 필요" })[r] || "";
      }
      // 자막 시각 확인(D55) — 표시만 한다. 측정 불가는 회색이다(결코 초록이 아니다).
      function syncBadge(info) {
        var sync = info && info.syncCheck;
        if (!sync) { return ""; }
        var tone = ({ red: " vl-badge-red", amber: " vl-badge-amber",
          green: " vl-badge-green" })[sync.tone] || "";
        return ' <span class="vl-badge' + tone + '" data-vl-sync="' + esc(sync.level) + '">' +
          esc(sync.label) + (sync.stale ? " (낡음)" : "") + "</span>";
      }
      function syncFlagWord(flag) {
        return ({ no_speech: "말소리를 못 찾음 (측정 한계일 수 있음)",
          early: "말보다 먼저 뜸" })[flag] || flag;
      }
      function syncPanel(info) {
        var sync = info && info.syncCheck;
        if (!sync) { return ""; }
        var open = sync.level === "red" || sync.level === "amber";
        var out = ['<details class="vl-details" data-vl-sync-panel="1"' + (open ? " open" : "") +
          "><summary>자막 시각 확인" + syncBadge(info) + "</summary>"];
        (sync.reasons || []).forEach(function(text) {
          out.push('<p class="vl-kv">' + esc(text) + "</p>");
        });
        (sync.cues || []).forEach(function(cue) {
          out.push('<button type="button" class="vl-btn" data-vl-act="source-pick" ' +
            'data-vl-cue-id="' + esc(cue.id) + '">' + esc(VL.fmt(ratOf(cue.start)) + "초 · " +
            cue.text + " · " + (cue.flags || []).map(syncFlagWord).join(" · ")) + "</button>");
        });
        (sync.checks || []).forEach(function(check) {
          out.push('<p class="vl-kv"><small>' + esc("[" + String(check.status).toUpperCase() +
            "] " + check.name + " — " + check.detail) + "</small></p>");
        });
        out.push('<button type="button" class="vl-btn" data-vl-act="sync-recheck">' +
          "다시 재기(무료)</button><small>자막과 컷은 바뀌지 않습니다 — 빨강이면 이 영상의 추천은" +
          " 보통까지입니다." +
          "</small></details>");
        return out.join("");
      }
      // 시각 맞춤 제안(D59 19g·19h) — 자막 시각 확인이 잰 말소리 경계에 큐를 맞춘 **제안**이다.
      // 적용은 무료이고 사람이 누를 때만, 그 영상의 모든 줄을 함께 바꾼다(막힘·못 찾음 줄의 밀기
      // 포함). 적용은 누른 순간 그려져 있던 제안의 sha에 묶이고 서버가 다시 재어 대조한다 — 새로
      // 그려진 지 REFILL_READ_MS가 안 된 제안은 들을 틈이 없었으니 빈 sha다(서버가 거절하고 새
      // 제안을 그린다).
      // 들어 볼 줄은 끝이 크게 움직인 것부터 — 옛 시각과 제안 시각을 같은 재생기로 차례로 듣는다.
      function snapPanel(info) {
        var sn = info && info.snap;
        if (!sn) { return ""; }
        var open = sn.state === "ready" || !!sn.undo;
        var out = ['<details class="vl-details" data-vl-snap="1"' + (open ? " open" : "") +
          "><summary>시각 맞춤 제안" +
          (sn.undo ? ' <span class="vl-badge">적용함</span>' : "") + "</summary>"];
        out.push('<p class="vl-kv">' + esc(sn.explanation) + "</p>");
        if (sn.undo) {
          out.push('<p class="vl-kv">' + esc(sn.undo.note) + "</p>");
          out.push('<button type="button" class="vl-btn" data-vl-act="snap-revert" ' +
            'data-vl-revision="' + esc(sn.undo.revision) + '">되돌리기</button>');
        }
        ["shift", "counts", "changes"].forEach(function(key) {
          if (sn[key]) { out.push('<p class="vl-kv">' + esc(sn[key]) + "</p>"); }
        });
        if (sn.prediction) {
          out.push('<p class="vl-kv">' + esc(sn.prediction) + " <small>" +
            esc(sn.predictionLabel) + "</small></p>");
        }
        if (sn.listenNote) { out.push('<p class="vl-kv">' + esc(sn.listenNote) + "</p>"); }
        (sn.listen || []).forEach(function(row) { out.push(snapListenRow(row)); });
        if (sn.order) { out.push('<p class="vl-kv"><small>' + esc(sn.order) + "</small></p>"); }
        if (sn.undoRule) {
          out.push('<p class="vl-kv"><small>' + esc(sn.undoRule) + "</small></p>");
        }
        if (sn.state === "ready" && sn.sha) {
          noteSnapShown(sn.sha);
          out.push('<button type="button" class="vl-btn vl-btn-primary" ' +
            'data-vl-act="snap-apply" data-vl-sha="' + esc(sn.sha) + '">' +
            "시각 맞춤 적용(무료)</button>");
        } else if (sn.reason) {
          out.push('<p class="vl-kv">' + esc(sn.reason) + "</p>");
        }
        out.push(snapAllPart(sn.all));
        out.push("</details>");
        return out.join("");
      }
      // 모든 영상 시각 맞춤(W-25e) — 확인 단추는 언제나, 확인한 목록과 「모두 적용」은 확인한
      // 뒤에만.
      // 적용은 그 목록의 묶음 토큰에 묶이고 서버가 다시 재어 대조한다.
      function snapAllPart(all) {
        var out = ['<div class="vl-snap-all">'];
        if (all) {
          out.push('<p class="vl-kv">' + esc(all.explanation) + "</p>");
          out.push('<p class="vl-kv">' + esc(all.summary) + "</p>");
          (all.rows || []).forEach(function(row) {
            out.push('<p class="vl-kv"><small>' + esc(row) + "</small></p>");
          });
          if (all.token) {
            out.push('<button type="button" class="vl-btn vl-btn-primary" ' +
              'data-vl-act="snap-all-apply" data-vl-token="' + esc(all.token) + '">' +
              "확인한 영상 모두 적용(무료)</button>");
          }
        }
        out.push('<button type="button" class="vl-btn" data-vl-act="snap-all-check">' +
          "모든 영상 시각 맞춤 확인(무료)</button></div>");
        return out.join("");
      }
      function snapSpan(start, end) {
        return VL.fmt(ratOf(start)) + "–" + VL.fmt(ratOf(end)) + "초";
      }
      // 들어 볼 줄 하나 — 옛 시각·제안 시각 단추는 그 구간만 재생하고 끝에서 멈춘다(A/B).
      function snapListenRow(row) {
        return '<div class="vl-snap-cue" data-vl-snap-cue="' + esc(row.id) + '">' +
          '<p class="vl-kv">' + esc(row.text) + ' <span class="vl-badge">' + esc(row.label) +
          "</span></p>" +
          '<p class="vl-kv"><small>' + esc(row.reason) + "</small></p>" +
          '<button type="button" class="vl-btn" data-vl-act="snap-listen" data-vl-from="' +
          esc(row.oldStart) + '" data-vl-to="' + esc(row.oldEnd) + '">' +
          esc("옛 시각 " + snapSpan(row.oldStart, row.oldEnd)) + "</button>" +
          '<button type="button" class="vl-btn" data-vl-act="snap-listen" data-vl-from="' +
          esc(row.newStart) + '" data-vl-to="' + esc(row.newEnd) + '">' +
          esc("제안 " + snapSpan(row.newStart, row.newEnd)) + "</button></div>";
      }
      function noteSnapShown(sha) {
        if (!state.snapShown || state.snapShown.sha !== sha) {
          state.snapShown = { sha: sha, at: Date.now() };
        }
      }
      // 누른 순간의 신원 — 적용은 그때 그려져 있던 sha(읽을 틈이 지났을 때만), 되돌리기는 그
      // 리비전.
      function snapPress(node) {
        var act = node.getAttribute("data-vl-act");
        if (act === "snap-revert") {
          return { act: act, sourceId: curSourceId(),
            revision: node.getAttribute("data-vl-revision") || "" };
        }
        var sha = node.getAttribute("data-vl-sha") || "";
        var shown = state.snapShown;
        var unread = !shown || shown.sha !== sha || Date.now() - shown.at < REFILL_READ_MS;
        return { act: act, sourceId: curSourceId(), sha: unread ? "" : sha };
      }
      // 들어 보기 — 그 구간만 재생하고 끝에서 멈춘다(반복 없음). 새 소리 파일은 없다. 끝은 화면
      // 프레임마다 본다 — `timeupdate`는 0.25초(부하가 있으면 그 이상)마다라 끝 경계를 넘겨
      // 들려준다(탐침이 0.48초 넘김을 잡았다). 프레임 콜백이 없는 곳에서는 틱(`timeupdate`)이
      // 멈춘다.
      function listenSpan(from, to) {
        var v = videoEl();
        var info = curSource();
        if (!v || !info || !info.src || !from || !to) {
          msg("이 원본의 미리보기가 없습니다. 준비가 끝나면 들을 수 있습니다.");
          return;
        }
        stopLoop(); state.keptPlaying = false;
        state.listenEnd = to;
        seekTo(from);
        v.play();
        if (W.requestAnimationFrame) { W.requestAnimationFrame(watchListenEnd); }
      }
      function watchListenEnd() {
        var v = videoEl();
        if (staleWire() || !state.listenEnd || !v || listenDone(v)) { return; }
        W.requestAnimationFrame(watchListenEnd);
      }
      function listenDone(v) {
        if (v.currentTime < VL.toFloat(VL.add(state.listenEnd, originRat()))) { return false; }
        state.listenEnd = null;
        v.pause();
        return true;
      }
      // 다시 듣기(D59 19b) — 누르면 차례만 오르고(무료) 승인 범위가 「전사만」이 된다. 유료 전사는
      // 견적을 확인하고 승인해야 시작한다. 사람이 고친 자막은 덮지 않는다(단추가 없다).
      function relistenPanel(info) {
        var re = info && info.relisten;
        if (!re) { return ""; }
        var red = info.syncCheck && info.syncCheck.level === "red";
        var out = ['<details class="vl-details" data-vl-relisten="1"' +
          (red || re.pending ? " open" : "") + "><summary>다시 듣기" +
          (re.pending ? ' <span class="vl-badge">승인 대기</span>' : "") + "</summary>"];
        out.push('<p class="vl-kv">' + esc(re.explanation) + "</p>");
        if (re.engine) { out.push('<p class="vl-kv">' + esc(re.engine) + "</p>"); }
        if (re.edited && re.snapped) {
          // 시각 맞춤만 얹은 자막 — 「되돌리기」로 적용 전 자막으로 돌리면 다시 들을 수
          // 있다(D59 19h).
          out.push('<p class="vl-kv">' + esc(re.snapped) + "</p></details>");
          return out.join("");
        }
        if (re.edited) {
          out.push('<p class="vl-kv">직접 고친 자막이 있어 다시 듣지 않습니다 — 고친 자막을' +
            " 덮어쓰지 않습니다.</p></details>");
          return out.join("");
        }
        if (re.refillRunning) {
          // 빈칸 재전사가 넣으면 고친 자막이 된다 — 그동안 다시 듣기를 받지 않는다(D59 19f).
          out.push('<p class="vl-kv">' + esc(re.refillRunning) + "</p></details>");
          return out.join("");
        }
        if (re.pending) {
          out.push('<p class="vl-kv">다시 듣기를 기다리고 있습니다 — 견적을 확인하고 승인하면' +
            " 시작합니다.</p></details>");
          return out.join("");
        }
        if (re.heard) { out.push('<p class="vl-kv">다시 들은 전사입니다.</p>'); }
        out.push('<button type="button" class="vl-btn" data-vl-act="relisten">다시 듣기</button>' +
          "<small>누르면 승인 범위가 「전사만」으로 바뀝니다. 견적을 확인하고 승인해야" +
          " 시작합니다.</small></details>");
        return out.join("");
      }
      // 말소리 빈칸 재전사(D59 19f) — 말소리 측정이 찾았는데 자막이 없는 곳을 그 구간만 다시
      // 묻는다.
      // 유료라 견적을 보고 승인해야 시작한다 — 승인은 **보이는 견적 문구**와 digest를 보내고 서버가
      // 지금 견적과 대조한다(D54). 금액·모델 이름은 견적 문구 안에만 있다(D57). 넣은 자막은 직접
      // 고친 자막이 되므로 순서 경고를 늘 보인다.
      function refillPanel(info) {
        var rf = info && info.refill;
        if (!rf) { return ""; }
        var open = rf.state === "ready" || rf.state === "running";
        var out = ['<details class="vl-details" data-vl-refill="1"' + (open ? " open" : "") +
          "><summary>말소리 빈칸 재전사" +
          (rf.state === "running" ? ' <span class="vl-badge">진행 중</span>' : "") +
          "</summary>"];
        out.push('<p class="vl-kv">' + esc(rf.explanation) + "</p>");
        if (rf.summary) { out.push('<p class="vl-kv">' + esc(rf.summary) + "</p>"); }
        (rf.spans || []).forEach(function(span) {
          out.push('<button type="button" class="vl-btn" data-vl-act="refill-pick" ' +
            'data-vl-at="' + esc(span.start) + '">' + esc(VL.fmt(ratOf(span.start), 1) + "–" +
            VL.fmt(ratOf(span.end), 1) + "초" + (span.failed ? " · 전사 실패 구간" : "")) +
            "</button>");
        });
        if (rf.more) { out.push('<p class="vl-kv"><small>외 ' + esc(rf.more) + "곳</small></p>"); }
        (rf.notes || []).forEach(function(note) {
          out.push('<p class="vl-kv">' + esc(note) + "</p>");
        });
        out.push('<p class="vl-kv"><small>' + esc(rf.order) + "</small></p>");
        if (rf.state === "ready" && rf.quote) {
          noteRefillQuote(rf.digest);
          out.push('<p class="vl-kv vl-refill-quote" id="vl-refill-quote">' + esc(rf.quote) +
            "</p>");
          out.push('<button type="button" class="vl-btn vl-btn-primary" ' +
            'data-vl-act="refill-approve" data-vl-digest="' + esc(rf.digest) + '">' +
            "빈칸 재전사 승인</button>");
        } else if (rf.reason) {
          out.push('<p class="vl-kv">' + esc(rf.reason) + "</p>");
        }
        if (rf.last) { out.push('<p class="vl-kv"><small>' + esc(rf.last) + "</small></p>"); }
        if (rf.skippedNote) {
          out.push('<p class="vl-kv">' + esc(rf.skippedNote) + "</p>");
          (rf.skipped || []).forEach(function(row) {
            out.push('<button type="button" class="vl-btn" data-vl-act="refill-pick" ' +
              'data-vl-at="' + esc(row.start) + '">' + esc(VL.fmt(ratOf(row.start), 1) + "–" +
              VL.fmt(ratOf(row.end), 1) + "초 · " + row.text + " · " + row.why) + "</button>");
          });
          if (rf.skippedMore) {
            out.push('<p class="vl-kv"><small>외 ' + esc(rf.skippedMore) + "줄</small></p>");
          }
        }
        out.push("</details>");
        return out.join("");
      }
      // 승인 앞 단계(D54) — 견적 칸이 정말 보일 때만 그려진 문구를 보낸다. 일괄 승인의 앞 단계
      // (APPROVE_GUARD_JS)와 같은 규칙이다: 칸이 있고 · 보이고 · 화면 안이고 · 다른 것에 덮이지
      // 않았고 · 서랍이 닫혔고 · 칸 안에서 잘리지 않았다. 못 재면 닫힌 쪽(빈 문구)이다. 빈 문구면
      // 서버가 「새 견적을 확인하세요」로 거절한다(유료 호출 0) — 그때 견적 칸을 보이게 한다.
      // 묶는 것은 **누른 순간**(pointerdown)에 그려져 있던 견적이다 — 누른 뒤 1초 틱이 새 견적을
      // 그려도 그 digest를 보내지 않는다. 새 견적이 그려진 지 REFILL_READ_MS가 안 됐으면 읽을
      // 틈이 없었으니 빈 문구다(누르기 직전에 바뀐 견적은 승인되지 않는다).
      var REFILL_READ_MS = 1500;
      function noteRefillQuote(digest) {
        if (!state.refillShown || state.refillShown.digest !== digest) {
          state.refillShown = { digest: digest, at: Date.now() };
        }
      }
      function refillUncovered(node, box) {
        if (!doc.elementFromPoint || !node.contains) { return false; }
        return [[box.left + box.width / 2, box.top + box.height / 2], [box.left + 2, box.top + 2],
          [box.right - 2, box.top + 2], [box.left + 2, box.bottom - 2],
          [box.right - 2, box.bottom - 2]].every(function(point) {
          var hit = doc.elementFromPoint(point[0], point[1]);
          return !!hit && node.contains(hit);
        });
      }
      function refillQuoteSeen() {
        var node = doc.getElementById("vl-refill-quote");
        if (!node || !node.getBoundingClientRect || !W.getComputedStyle || !doc.querySelector) {
          return "";
        }
        if (doc.querySelector("#project-tools.open, #settings-drawer.open")) { return ""; }
        var style = W.getComputedStyle(node);
        var box = node.getBoundingClientRect();
        var inside = box.width >= 1 && box.height >= 1 && box.top >= 0 && box.left >= 0 &&
          box.bottom <= W.innerHeight && box.right <= W.innerWidth;
        var text = String(node.textContent || "");
        var seen = style.display !== "none" && style.visibility === "visible" && inside &&
          refillUncovered(node, box) && !(node.scrollHeight > node.clientHeight + 1) &&
          text.trim() !== "";
        return seen ? text : "";
      }
      function refillPress(node) {
        var digest = node.getAttribute("data-vl-digest") || "";
        var shown = state.refillShown;
        var unread = !shown || shown.digest !== digest || Date.now() - shown.at < REFILL_READ_MS;
        return { sourceId: curSourceId(), digest: digest, quote: unread ? "" : refillQuoteSeen() };
      }
      function refillReveal() {
        if (W.__vlDrawer) {
          ["project-tools", "settings-drawer"].forEach(function(id) { W.__vlDrawer(id, false); });
        }
        var node = doc.getElementById("vl-refill-quote");
        if (node && node.scrollIntoView) { node.scrollIntoView({ block: "nearest" }); }
      }
      // 3단 등급(D51) — 글자 표지는 색과 별개로 등급을 말한다(색약·흑백에서도 읽힌다). 빨강의
      // 화면 이름은 「비추천」(W-46a) — 대기열·결정 상자의 「제외 제안」은 기다리는 제안이다.
      function tierWord(tier, hold) {
        if (tier === "yellow" && hold) { return "판정 보류"; }
        return ({ green: "추천", yellow: "보통", red: "비추천" })[tier] || "";
      }
      function tierGlyph(tier, hold) {
        if (tier === "yellow" && hold) { return "? 보류"; }
        return ({ green: "★ 추천", yellow: "보통", red: "✕ 비추천" })[tier] || "";
      }
      function tierOf(row) {
        return row.tier || (row.rec === "recommended" ? "green" : "yellow");
      }
      function tierDetail(row) {
        if (!row.audio || !row.video) { return row.reason || ""; }
        // 표가 추천이라 했지만 추천하지 않는 까닭(D60) — 막대의 제목·읽기 글에도 붙인다.
        var caps = (row.caps || []).map(function(text) { return " · 추천 제한: " + text; });
        // 「자막 확인됨」으로 추천한 까닭(D65) — 추천 제한과 같은 자리에 쉬운 말로.
        var lift = row.lift ? " · 추천 근거: " + row.lift : "";
        return "자막: " + (row.audio.reason || row.audio.label || "") +
          " · 영상: " + (row.video.reason || row.video.label || "") + caps.join("") + lift;
      }
      function tagText(card, prefix) {
        var hit = (card.tags || []).find(function(t) { return t.indexOf(prefix) === 0; });
        return hit ? hit.slice(prefix.length) : "";
      }
      function intervalAt(info, row) {
        // 빨강 끝은 프레임 안쪽으로 붙어 있다 — 시작점이 아니라 가운데로 조각을 찾는다.
        var mid = VL.mul(VL.add(ratOf(row.start), ratOf(row.end)), VL.parse("1/2"));
        return info && info.rail && info.rail.intervals.find(function(it) {
          return VL.cmp(ratOf(it.start), mid) <= 0 && VL.cmp(ratOf(it.end), mid) > 0;
        });
      }
      function stopLoop() { state.loop = null; state.listenEnd = null; }
      function unloadVideo(v) {
        clearSourceCaptions(v);
        stopLoop();
        v.pause();
        v.removeAttribute("src");
        try { v.load(); } catch (err) { /* 능동 언로드 — 재생 정지가 본질이다 */ }
        v.setAttribute("data-vl-src", "");
      }
      function applySource(info, v) {
        var have = v.getAttribute("data-vl-src") || "";
        if (have !== info.src) {
          clearSourceCaptions(v);
          stopLoop();
          v.pause();
          v.setAttribute("data-vl-src", info.src);
          v.src = info.src;
        }
        // 전역 단축키(쉼표·마침표)가 클로저 없이 이 원본의 한 프레임 길이를 읽는다.
        v._vlFrameSec = info.rail && info.rail.frameSec ? VL.toFloat(ratOf(info.rail.frameSec)) : 0;
      }
      function syncVideo() {
        var v = videoEl();
        if (!v) { return; }
        var info = curSource();
        if (!info) { say("영상을 고르면 여기에서 재생합니다."); return; }
        if (!info.src) {
          // 이 원본의 미리보기가 없다 — 이전 원본을 틀은 채 새 이름표를 붙이지
          // 않는다. 재생을 끊고 상태를 알린다(빈 성공 금지).
          if (v.getAttribute("data-vl-src")) { unloadVideo(v); }
          say("원본 " + info.sourceName + " — 미리보기 " + stateLabel(info.state) +
            ". 패널의 「재생 설정」에서 준비할 수 있습니다.");
          return;
        }
        applySource(info, v);
        paintSourceCaptions();
        say("원본 " + info.sourceName + " · 미리보기 " + stateLabel(info.state));
      }

      // 카드는 영상 전체를 요약한다(D57 B) — 첫 구간 하나로 카드의 등급을 정하지 않는다.
      function durLabel(value) {
        var seconds = Math.max(0, Math.round(VL.toFloat(ratOf(value)) || 0));
        return Math.floor(seconds / 60) + ":" + String(seconds % 60).padStart(2, "0");
      }
      function spanClass(row) {
        var tier = tierOf(row);
        if (tier === "yellow" && row.hold) { return "h"; }
        // 경계와 맞지 않는 빨강은 속 빈 빨강 — 남은 수에 들지 않는다(결정한 제안의 흐린
        // 빗금과 다르다).
        if (tier === "red") { return row.loose ? "r loose" : row.open === false ? "r done" : "r"; }
        return tier === "green" ? "g" : "y";
      }
      // 길이대로 잇는 띠 — 판정이 없는 자리(분석 전)는 회색 칸으로 채운다.
      function cardSpans(info) {
        var rail = info && info.rail;
        var total = rail ? VL.toFloat(ratOf(rail.duration)) || 0 : 0;
        var rows = ((info && info.suggestions) || []).slice().sort(function (a, b) {
          return VL.toFloat(ratOf(a.start)) - VL.toFloat(ratOf(b.start));
        });
        var out = [], cursor = 0;
        rows.forEach(function (row) {
          var a = VL.toFloat(ratOf(row.start)), b = VL.toFloat(ratOf(row.end));
          if (!(b > a)) { return; }
          if (a > cursor) { out.push(["n", a - cursor]); }
          out.push([spanClass(row), b - a]);
          cursor = Math.max(cursor, b);
        });
        if (total > cursor) { out.push(["n", total - cursor]); }
        if (!out.length) { out.push(["n", 1]); }
        return out.map(function (part) {
          return '<i class="' + part[0] + '" style="flex:' + part[1].toFixed(3) + '"></i>';
        }).join("");
      }
      // 수는 ✕(아직 결정을 기다리는 제외 제안만) · ?(판정 보류) · ★(추천) 순서다. ✕는 대기열 줄
      // (그 영상의 미검토 컷) 수다 — 카드 ✕의 합이 대기열의 M이다(D57 C 고침 1).
      function cardCounts(info) {
        var rows = (info && info.suggestions) || [];
        var red = info ? (openProposalsBySource()[info.sourceId] || 0) : 0;
        if (!rows.length && !red) { return ""; }
        var hold = rows.filter(function (r) { return spanClass(r) === "h"; }).length;
        var green = rows.filter(function (r) { return spanClass(r) === "g"; }).length;
        function cnt(cls, glyph, n) {
          return '<span class="vl-cnt ' + (n ? cls : "z") + '">' + glyph + " " + n + "</span>";
        }
        return '<span class="vl-cnts" aria-label="' + esc("결정을 기다리는 제외 제안 " + red +
          "개 · 판정 보류 " + hold + "개 · 추천 " + green + "개") + '">' + cnt("r", "✕", red) +
          cnt("h", "?", hold) + cnt("g", "★", green) + "</span>";
      }
      // 배지는 하나뿐, 눈을 불러야 할 때만 — 자막 시각 확인(D55)이 있으면 그것이 배지다.
      function cardBadge(c, info) {
        var sync = syncBadge(info);
        if (sync) { return sync; }
        var badge = info && info.analysisBadge;
        if (badge && badge.label) {
          return ' <span class="vl-badge" data-vl-badge="' + esc(badge.tone) + '">' +
            esc(badge.label) + "</span>";
        }
        if (info && info.originalMissing) {
          return ' <span class="vl-badge" data-vl-badge="warn">원본 연결 필요</span>';
        }
        // 영상의 컷 하나라도 경계가 계획과 다르면 — 대표 컷 하나만 보던 표시는 쪽에 따라
        // 갈렸다(D64).
        var stale = videoCuts(info).some(function (it) { return it.needsExtract; });
        return stale ? ' <span class="vl-badge vl-badge-warn">영상 갱신 필요</span>' : "";
      }
      // 그릴 수 있는 썸네일 — 준비됨이고 주소가 있어야 한다(낡음·실패의 옛 주소는 404다).
      function drawableThumb(c) {
        return c && c.thumb && c.thumb.state === "ready" && c.thumb.src ? c.thumb.src : null;
      }
      // 카드 그림은 서버가 고른 그 영상의 그릴 수 있는 썸네일(`cardThumb`, W-24 세션 I) — 대표 컷
      // 것이 없으면 다른 컷 것이다. 그릴 것이 없으면 대표 컷의 상태 글자다(주소 없는 준비됨은
      // 아직 없음).
      function cardArt(c) {
        var src = c.cardThumb !== undefined ? c.cardThumb : drawableThumb(c);
        var shown = c.thumb && c.thumb.state === "ready" ? "missing" : c.thumb && c.thumb.state;
        var art = src
          ? '<img src="' + esc(src) + '" alt="조각 대표 프레임">'
          : '<span class="vl-thumb-missing">' + esc(shown ? stateLabel(shown) : "없음") +
            "</span>";
        return '<span class="vl-thumb-art">' + art + '<span class="dur">' + durLabel(c.dur) +
          "</span></span>";
      }
      // 영상의 컷 — 쪽과 무관하게 그 영상 전부다(레일의 구간 목록, D64).
      function videoCuts(info) { return info && info.rail ? info.rail.intervals : []; }
      function cardExcluded(info) {
        var cuts = videoCuts(info);
        return cuts.length > 0 && cuts.every(function (it) { return it.sel === "excluded"; });
      }
      function cardButton(c, sel) {
        var info = (model.sources || {})[c.sourceId] || null;
        // 고른 컷이 이 영상의 것이면 이 카드가 골라진 것이다 — 보이는 강조와 aria가 같은 값이다
        // (D64: 예전 aria는 대표 컷 ID만 견줘 영상의 다른 컷을 고르면 아무 카드도 골라지지 않았다).
        var picked = c.id === sel || c.sourceId === curSourceId();
        var cls = "vl-thumb" + (picked ? " vl-thumb-sel" : "") +
          (cardExcluded(info) ? " vl-thumb-ex" : "");
        return '<button type="button" class="' + cls + '" data-vl-card="' +
            esc(c.id) + '" role="option"' + ' title="' + esc(c.sourceName) + '"' +
          ' aria-selected="' + (picked ? "true" : "false") + '">' + cardArt(c) +
          '<span class="vl-thumb-name">' + esc(c.sourceName) + "</span>" +
          '<span class="vl-thumb-meta">' + cardCounts(info) + cardBadge(c, info) + "</span>" +
          '<span class="vl-mbar" aria-hidden="true">' + cardSpans(info) + "</span></button>";
      }
      // 「제외 제안」 보기(D57 C) — 결정을 기다리는 제외 제안만: 대기열에 열린 제안이 남은 영상.
      // 옛 이름의 이 보기는 보류·검토 필요 컷도 실었다.
      function openProposalsBySource() {
        var count = {};
        queueItems().forEach(function (it) { count[it.sourceId] = (count[it.sourceId] || 0) + 1; });
        return count;
      }
      // 「사용할 컷」·「제외된 컷」은 영상마다 그 영상의 컷 전부로 판정한다 — 맞는 컷이 하나라도
      // 있으면 보인다(D64). 예전에는 쪽에 따라 바뀌는 대표 컷 하나의 사용 여부로 갈렸다.
      function cardFiltered(c, open) {
        var want = ({ included: "included", excluded: "excluded" })[state.filter];
        if (want) {
          return !videoCuts(sourceInfo(c.sourceId)).some(function (it) { return it.sel === want; });
        }
        if (state.filter === "review") { return !open[c.sourceId]; }
        return false;
      }
      // 목록 머리 — 영상은 언제나 촬영 시각 순이다(D53). 영상 카드는 모든 영상을 한 번에 그리므로
      // 쪽 넘김이 없다(D64 — 옛 「다음 쪽」은 그리지 않는 컷 목록을 넘겼다).
      // 계획이 덮지 못한 영상(VFR, D68)도 수에 든다 — 빠진 영상이 수에서도 사라지지 않게.
      function listHead(shown, skipped) {
        var extra = skipped.length ? " · 분석 지원 밖 " + skipped.length + "개" : "";
        return '<p class="vl-list-head"><b>영상 ' + (shown.length + skipped.length) +
          "개</b> · 촬영 시각 순" + extra + "</p>";
      }
      // 덮지 못한 영상의 카드(D68) — 재생할 조각이 없어 고르지 않는다. 누르면 까닭을 보인다.
      function unsupportedCard(u) {
        return '<button type="button" class="vl-thumb vl-thumb-ex vl-thumb-unsupported"' +
          ' data-vl-act="unsupported-pick" data-vl-unsupported="' + esc(u.sourceId) +
          '" data-vl-note="' + esc(u.note) + '" title="' + esc(u.sourceName) + '">' +
          '<span class="vl-thumb-art"><span class="vl-thumb-missing">VFR</span></span>' +
          '<span class="vl-thumb-name">' + esc(u.sourceName) + "</span>" +
          '<span class="vl-thumb-meta"><span class="vl-badge vl-badge-amber">' +
          esc(u.reason) + "</span></span></button>";
      }
      // 촬영 시각 순(D53) — 덮지 못한 영상은 서버가 정한 다음 영상(`before`) 카드 앞에 선다.
      function cardsInOrder(shown, skipped, sel) {
        var placed = {};
        var out = [];
        shown.forEach(function (c) {
          skipped.forEach(function (u) {
            if (u.before === c.sourceId && !placed[u.sourceId]) {
              placed[u.sourceId] = true; out.push(unsupportedCard(u));
            }
          });
          out.push(cardButton(c, sel));
        });
        skipped.forEach(function (u) {
          if (!placed[u.sourceId]) { out.push(unsupportedCard(u)); }
        });
        return out;
      }
      function renderThumbs() {
        var mount = doc.getElementById("vl-thumbs");
        if (!mount) { return; }
        var rows = ['<div class="vl-row" role="group" aria-label="보기 필터">'];
        [["all","전체"],["included","사용할 컷"],
         ["excluded","제외된 컷"],["review","제외 제안"]].forEach(function(f) {
          rows.push('<button type="button" class="vl-btn" data-vl-act="filter-' + f[0] +
            '" aria-pressed="' + ((state.filter || "all") === f[0]) + '">' + f[1] + '</button>');
        });
        var sel = selectedId();
        var open = openProposalsBySource();
        var shown = (model.mediaCards || []).filter(function (c) {
          return !cardFiltered(c, open);
        });
        // 덮지 못한 영상은 컷이 없어 「전체」 보기에만 선다.
        var skipped = (state.filter || "all") === "all" ? (model.unsupportedSources || []) : [];
        rows.push('</div><div class="vl-cards">');
        rows = rows.concat(cardsInOrder(shown, skipped, sel));
        rows.push("</div>");
        // 다시 그려도 「다음」의 키보드 초점은 새 「다음」으로 옮긴다(연달아 Enter). 서버 답은
        // 그라디오가 작업대를 갈아 그려 초점이 문서로 떨어진 뒤에 온다 — 마지막으로 누른 것이
        // 「다음」이고 초점이 어디에도 없을 때만 되돌린다(다른 칸에 쓰는 중이면 빼앗지 않는다).
        var active = doc.activeElement;
        var keep = !!(active && active.getAttribute &&
          active.getAttribute("data-vl-act") === "queue-next") ||
          (!!state.queueRefocus && (!active || active === doc.body));
        mount.innerHTML = queueBlock() + listHead(shown, skipped) + rows.join("");
        if (typeof mount.querySelectorAll === "function" && VL.fitThumb) {
          Array.prototype.forEach.call(mount.querySelectorAll(".vl-thumb-art img"), VL.fitThumb);
        }
        var next = keep && typeof mount.querySelector === "function"
          ? mount.querySelector('[data-vl-act="queue-next"]') : null;
        if (next && !next.disabled) { next.focus(); }
      }

      function railIndex(id) {
        var rail = railOf();
        if (!rail) { return -1; }
        for (var i = 0; i < rail.intervals.length; i += 1) {
          if (rail.intervals[i].id === id) { return i; }
        }
        return -1;
      }

      // 컷 이름표(D57 C) — 영상 안의 번호(원본 타임라인 순서)와 원본 범위. 정리 결과 보기에서도
      // 번호·범위는 원본 그대로라 두 보기에서 같은 컷이 같은 이름이다.
      function cutNumbers(info) {
        var numbers = {};
        ((info && info.rail && info.rail.intervals) || []).forEach(function (it, index) {
          numbers[it.id] = index + 1;
        });
        return numbers;
      }
      function cutRangeLabel(it, number) {
        return { number: number || 0,
          from: VL.fmt(ratOf(it.sourceStart || it.start), 1),
          to: VL.fmt(ratOf(it.sourceEnd || it.end), 1) };
      }
      // 등급 막대 표지는 누른 순간의 구간을 베낀 것이다 — 그리기마다 지금 구간(같은 원본·시작·끝)
      // 에서 다시 읽는다. 자막 시각을 다시 재면 같은 구간의 등급·까닭이 바뀐다(D60). 그 구간이
      // 사라졌으면 표지를 거둔다. 원본 자막·큐 표지는 등급 구간이 아니라 그대로 둔다.
      function refreshTierMarker() {
        var mark = state.marker;
        if (!mark || !mark.tierSpan) { return; }
        var info = sourceInfo(mark.sourceId);
        if (!info || !Array.isArray(info.suggestions)) { return; }
        var row = info.suggestions.find(function (r) {
          return r.start === mark.start && r.end === mark.end;
        });
        if (!row) { state.marker = null; return; }
        state.marker = Object.assign({ sourceId: mark.sourceId, segmentId: mark.segmentId,
          tierSpan: true, label: mark.queued ? tierWord("red", false) :
            tierWord(tierOf(row), row.hold) || recLabel(row.rec),
          queued: mark.queued }, row);
      }
      // ── 시작·끝 표시(W-46b, D100 §7b) — In·Out으로 범위를 찍고 추천·보통·비추천으로 표시한다.
      // 찍은 범위는 탭의 것이다(`state.range` — 원본 하나에 묶이고, 원점 기준 초를 프레임에 맞춘
      // 정확 유리수). 시작이 끝보다 뒤로 가면 둘을 **바꾼다**(거절하지 않는다). 손잡이 끌기는
      // 시작·끝만 옮긴다 — 컷 경계를 제안하지 않는다(컷 경계 손잡이는 「고급 도구」에만 있다).
      function rangeRail() { var s = curSource(); return s && s.rail ? s.rail : null; }
      function rangeFrame() { var r = rangeRail(); return r ? ratOf(r.frameSec) : null; }
      function rangeDur() { var r = rangeRail(); return r ? ratOf(r.duration) : null; }
      function snapRange(t) {
        var fd = rangeFrame(), dur = rangeDur();
        if (!t || !fd || !dur || fd[0] <= 0) { return null; }
        // 가장 가까운 프레임 — 한가운데면 앞장(서버 `snapped_frame_index`와 같은 규칙).
        var q = VL.div(t, fd);
        var v = VL.mul([Math.ceil((2 * q[0] - q[1]) / (2 * q[1])), 1], fd);
        if (VL.cmp(v, [0, 1]) < 0) { v = [0, 1]; }
        if (VL.cmp(v, dur) > 0) { v = dur; }
        return v;
      }
      function curRange() {
        var r = state.range;
        return r && r.sourceId === curSourceId() ? r : null;
      }
      function rangeValid() {
        var r = curRange();
        return !!(r && r.inSec && r.outSec && VL.cmp(r.inSec, r.outSec) < 0);
      }
      function setRangePoint(which, t) {
        var sid = curSourceId(), v = snapRange(t);
        if (!sid || !v) {
          msg("재생기를 찾지 못했습니다. 미리보기가 준비된 뒤 다시 찍으세요."); return false;
        }
        var r = Object.assign({ sourceId: sid, inSec: null, outSec: null }, curRange() || {});
        r[which === "in" ? "inSec" : "outSec"] = v;
        if (r.inSec && r.outSec && VL.cmp(r.inSec, r.outSec) > 0) {
          var swap = r.inSec; r.inSec = r.outSec; r.outSec = swap;
        }
        state.range = r;
        renderRail();
        return true;
      }
      function clearRange() {
        if (!curRange()) { return false; }
        state.range = null; renderRail(); return true;
      }
      function rangeSeconds(a) { return VL.fmt(a, 2) + "초"; }
      function paintRangeControls() {
        var r = curRange(), ok = rangeValid();
        var out = doc.getElementById("vl-range-readout");
        if (out) {
          var text = "재생하며 In(I)·Out(O)으로 시작과 끝을 찍으세요.";
          if (r && r.inSec && r.outSec) {
            text = rangeSeconds(r.inSec) + " ~ " + rangeSeconds(r.outSec) + " (" +
              rangeSeconds(VL.sub(r.outSec, r.inSec)) + ")" +
              (ok ? "" : " — 시작과 끝이 같은 프레임입니다");
          } else if (r && r.inSec) {
            text = "시작 " + rangeSeconds(r.inSec) + " · 끝(O)을 찍으세요";
          } else if (r && r.outSec) {
            text = "끝 " + rangeSeconds(r.outSec) + " · 시작(I)을 찍으세요";
          }
          out.textContent = text;
        }
        VL_RANGE_MARKS.forEach(function (mark) {
          var button = doc.getElementById("vl-mark-" + mark);
          if (button) { button.disabled = !ok; }
        });
      }
      function sendMark(mark) {
        if (staleWire()) { return false; }
        if (!rangeValid()) { msg("시작과 끝을 먼저 찍으세요(In · Out)."); return true; }
        var r = curRange();
        call("vl_apply", { op: "range", mark: mark, sourceId: r.sourceId,
          startSec: VL.str(r.inSec), endSec: VL.str(r.outSec),
          revisionId: model.revisionId, basePlanId: model.basePlanId, page: model.page,
          selectedId: selectedId() }, "범위를 표시하는 중…", function (res) {
          // 저장했으면 찍은 범위를 걷는다 — 낡음(다른 곳의 편집)이면 그대로 두어 다시 누르게 한다.
          if (res && res.ok && !res.rangeStale && state.range === r) { state.range = null; }
        });
        return true;
      }
      function sendUndo() {
        if (staleWire()) { return; }
        call("vl_apply", { op: "undo", sourceId: curSourceId(), revisionId: model.revisionId,
          basePlanId: model.basePlanId, page: model.page, selectedId: selectedId() },
          "되돌리는 중…");
      }
      state.rangeKey = function (action) {
        if (staleWire()) { return false; }
        if (action === "in" || action === "out") { return setRangePoint(action, playheadLocal()); }
        if (action === "cancel") { return clearRange(); }
        return sendMark(action);
      };
      function railPct(t, dur) { return VL.pct(displayTime(t), dur); }
      function rangeLayers(info, rail, dur) {
        var html = [];
        (rail.intervals || []).forEach(function (it) {
          var tone = { recommended: "green", normal: "yellow", not_recommended: "red" }[it.mark];
          if (!tone) { return; }
          var a = railPct(ratOf(it.start), dur), b = railPct(ratOf(it.end), dur);
          html.push('<span class="vl-human-mark ' + tone + '" data-vl-human-mark="' +
            esc(it.mark) + '" style="left:' + a.toFixed(3) + '%;width:' +
            Math.max(.4, b - a).toFixed(3) + '%"></span>');
        });
        html.push('<div class="vl-kept-strip" aria-label="내보낼 부분">');
        ((info && info.kept) || []).forEach(function (row) {
          var a = railPct(ratOf(row.start), dur), b = railPct(ratOf(row.end), dur);
          html.push('<span class="vl-kept' + (row.human ? ' human' : '') + '" style="left:' +
            a.toFixed(3) + '%;width:' + Math.max(.3, b - a).toFixed(3) + '%"></span>');
        });
        html.push('</div>');
        var r = curRange();
        if (r && r.inSec && r.outSec) {
          var x = railPct(r.inSec, dur), y = railPct(r.outSec, dur);
          html.push('<div class="vl-range-band" style="left:' + x.toFixed(3) + '%;width:' +
            Math.max(0, y - x).toFixed(3) + '%"></div>');
        }
        [["in", r && r.inSec, "시작"], ["out", r && r.outSec, "끝"]].forEach(function (h) {
          if (!h[1]) { return; }
          html.push('<button type="button" class="vl-range-handle" data-vl-range-handle="' + h[0] +
            '" aria-label="찍은 ' + h[2] + ' ' + esc(VL.fmt(h[1], 2)) + '초 — 끌면 ' + h[2] +
            '만 옮깁니다" style="left:' + railPct(h[1], dur).toFixed(3) + '%"></button>');
        });
        return html.join("");
      }
      function renderRanges() {
        var list = doc.getElementById("vl-range-list");
        var sum = doc.getElementById("vl-ranges-sum");
        var info = curSource();
        var rows = (info && info.kept) || [];
        var total = rows.reduce(function (acc, row) {
          return VL.add(acc, VL.sub(ratOf(row.end), ratOf(row.start)));
        }, [0, 1]);
        if (sum) { sum.textContent = info ? rows.length + "곳 · " + VL.fmt(total, 1) + "초" : ""; }
        if (!list) { return; }
        var html = rows.map(function (row, index) {
          var len = VL.sub(ratOf(row.end), ratOf(row.start));
          return '<button type="button" role="listitem" class="vl-range-row' +
            (row.human ? ' human' : '') + '" data-vl-range-seek="' + esc(row.start) + '">' +
            '<b>#' + (index + 1) + '</b> ' + esc(VL.fmt(ratOf(row.start), 1)) + '–' +
            esc(VL.fmt(ratOf(row.end), 1)) + '초 <small>' + esc(VL.fmt(len, 1)) + '초 · ' +
            (row.human ? '사람 추천' : '자동 추천') + '</small></button>';
        });
        if (!info) {
          html = ['<p class="vl-range-note">영상을 고르면 내보낼 부분이 보입니다.</p>'];
        } else if (info.keptNote) {
          html.push('<p class="vl-range-note">' + esc(info.keptNote) + '</p>');
        }
        else if (!rows.length) {
          html.push('<p class="vl-range-note">이 영상에서 내보낼 부분이 없습니다. ' +
            'In·Out으로 범위를 찍고 「추천」으로 표시하면 들어갑니다.</p>');
        }
        list.innerHTML = html.join("");
      }
      function rangeDrag(ev, which) {
        var r = curRange();
        if (!r || !r[which === "in" ? "inSec" : "outSec"]) { return; }
        if (ev.preventDefault) { ev.preventDefault(); }
        var mount = doc.getElementById("visual-rail");
        var move = function (mv) {
          if (staleWire() || !mv || state.range !== r) { return; }
          var at = secAt(mv.clientX);
          var v = at ? snapRange(sourceTime(at)) : null;
          var fd = rangeFrame();
          if (!v || !fd) { return; }
          // 끌기는 제 끝만 옮기고 다른 끝을 넘지 않는다(한 프레임 앞에서 멈춘다) — 원본 안으로
          // 묶인다.
          if (which === "in" && r.outSec && VL.cmp(v, VL.sub(r.outSec, fd)) > 0) {
            v = VL.sub(r.outSec, fd);
          }
          if (which === "out" && r.inSec && VL.cmp(v, VL.add(r.inSec, fd)) < 0) {
            v = VL.add(r.inSec, fd);
          }
          r[which === "in" ? "inSec" : "outSec"] = v;
          renderRail();
        };
        var up = function () {
          if (mount) {
            mount.removeEventListener("pointermove", mount._vlRangeMove);
            mount.removeEventListener("pointerup", mount._vlRangeUp);
          }
        };
        if (mount) {
          if (mount._vlRangeMove) { mount.removeEventListener("pointermove", mount._vlRangeMove); }
          if (mount._vlRangeUp) { mount.removeEventListener("pointerup", mount._vlRangeUp); }
          mount._vlRangeMove = move; mount._vlRangeUp = up;
          mount.addEventListener("pointermove", move);
          mount.addEventListener("pointerup", up);
        }
      }
      function advancedOpen() {
        var stage = doc.getElementById("visual-stage");
        return !!(stage && stage.getAttribute && stage.getAttribute("data-vl-advanced") === "open");
      }
      function setAdvanced(open) {
        var stage = doc.getElementById("visual-stage");
        if (stage && stage.setAttribute) {
          if (open) { stage.setAttribute("data-vl-advanced", "open"); }
          else if (stage.removeAttribute) { stage.removeAttribute("data-vl-advanced"); }
        }
        var toggle = doc.getElementById("vl-advanced-toggle");
        if (toggle) { toggle.setAttribute("aria-expanded", open ? "true" : "false"); }
      }
      function renderRail() {
        drawRail();
        paintRangeControls();
        renderRanges();
      }
      function drawRail() {
        refreshTierMarker();
        var mount = doc.getElementById("visual-rail");
        if (!mount) { return; }
        var rail = railOf();
        if (!rail) { mount.innerHTML = ""; return; }
        var dur = ratOf(rail.duration);
        if (!dur || dur[0] <= 0) { mount.innerHTML = ""; return; }
        var sel = selectedId();
        var at = railIndex(sel);
        var html = ['<div class="vl-ruler">'];
        for (var tickIndex = 0; tickIndex <= 10; tickIndex += 1) {
          var seconds = VL.toFloat(dur) * tickIndex / 10;
          var label = Math.floor(seconds / 60) + ':' +
            String(Math.floor(seconds % 60)).padStart(2,'0');
          html.push('<span style="left:' + (tickIndex * 10) + '%">' + label + '</span>');
        }
        html.push('</div>');
        var info = curSource();
        var suggestions = (info && info.suggestions) || [];
        if (!suggestions.length) {
          html.push('<div class="vl-rec-pending">' +
            esc(info && (info.analysisLabel || info.analysisState) || '분석 전') + '</div>');
        }
        suggestions.forEach(function(row,index) {
          var a = displayTime(ratOf(row.start)), b = displayTime(ratOf(row.end));
          if (!a || !b || VL.cmp(a,b) >= 0) { return; }
          var tier = tierOf(row);
          var loose = tier === "red" && row.loose;
          var detail = (loose ? "컷 경계와 맞지 않음 · 남은 수에 들지 않음 · " : "") +
            tierDetail(row);
          // 「다음」·막대 클릭으로 고른 구간은 accent-ink 바깥선으로 보인다(선택은 하나, D57 C).
          var picked = state.marker && state.marker.sourceId === curSourceId() &&
            state.marker.start === row.start && state.marker.end === row.end;
          html.push('<button type="button" class="vl-recommendation ' + esc(row.rec) + ' ' +
            esc(tier) + (row.hold ? ' hold' : '') + (loose ? ' loose' : '') +
            (picked ? ' vl-rec-sel' : '') +
            '" data-vl-recommendation="' + index +
            '" data-vl-tier="' + esc(tier) + '" style="left:' + VL.pct(a,dur).toFixed(3) +
            '%;width:' + Math.max(.4,VL.pct(VL.sub(b,a),dur)).toFixed(3) +
            '%" aria-label="' + esc(tierWord(tier, row.hold) + ' ' +
              VL.fmt(ratOf(row.start), 1) + '초~' + VL.fmt(ratOf(row.end), 1) + '초 · ' + detail) +
            '" title="' + esc(detail) + '">' + esc(tierGlyph(tier, row.hold)) + '</button>');
        });
        ((info && info.subtitles) || []).forEach(function(cue,index) {
          var a = displayTime(ratOf(cue.start)), b = displayTime(ratOf(cue.end));
          if (!a || !b || VL.cmp(a,b) >= 0) { return; }
          html.push('<button type="button" class="vl-source-caption" data-vl-source-cue="' + index +
            '"' + (cue.ear ? ' data-vl-ear="' + esc(cue.ear.mark) + '"' : '') +
            (cue.suspects && cue.suspects.length ? ' data-vl-suspect="1"' : '') +
            ' style="left:' + VL.pct(a,dur).toFixed(3) + '%;width:' +
            Math.max(.6,VL.pct(VL.sub(b,a),dur)).toFixed(3) + '%" title="' +
            esc(cueTitle(cue)) + '">' + esc(cue.text) + '</button>');
        });
        html.push('<div class="vl-audio-strip">' +
          (info && info.tracks && info.tracks.length ? '원본 오디오' : '오디오 없음') + '</div>');
        var numbers = cutNumbers(info);
        for (var i = 0; i < rail.intervals.length; i += 1) {
          var it = rail.intervals[i];
          var cls = "vl-int" + (it.id === sel ? " vl-int-sel" : "") +
            (it.sel === "excluded" ? " vl-int-ex" : "") + (it.needsExtract ? " vl-int-needs" : "");
          var cutLabel = cutRangeLabel(it, numbers[it.id]);
          var frameCard = findCard(it.id);
          var thumb = drawableThumb(frameCard);
          if (!thumb) {
            var sourceCard = knownCuts().find(function(c) {
              return c.sourceId === curSourceId() && drawableThumb(c);
            });
            thumb = drawableThumb(sourceCard);
          }
          html.push('<button type="button" class="' + cls +
            '" data-vl-interval="' + esc(it.id) + '"' +
            ' style="' + (thumb ? "background-image:url('" + esc(thumb) + "');" : "") +
            'left:' + VL.pct(ratOf(it.start), dur).toFixed(3) + "%;width:" +
            Math.max(0.4, VL.pct(VL.sub(ratOf(it.end), ratOf(it.start)), dur)).toFixed(3) + '%"' +
            ' aria-label="컷 #' + cutLabel.number + " · " + cutLabel.from + "초~" + cutLabel.to +
              '초 — 클릭·Enter로 그 시각으로 이동">' + "<span><b>#" + cutLabel.number +
              "</b> " + cutLabel.from + "–" + cutLabel.to + "</span></button>");
        }
        var leftPair = at > 0 && ratOf(rail.intervals[at - 1].end) &&
          VL.cmp(ratOf(rail.intervals[at - 1].end), ratOf(rail.intervals[at].start)) === 0;
        var rightPair = at >= 0 && at + 1 < rail.intervals.length &&
          VL.cmp(ratOf(rail.intervals[at + 1].start), ratOf(rail.intervals[at].end)) === 0;
        if (at >= 0 && state.timelineMode !== "result") {
          html.push('<button type="button" class="vl-handle" data-vl-handle="left"' +
            (leftPair ? "" : " disabled") +
            ' aria-label="왼쪽 경계 손잡이 — 화살표로 초안, Enter로 스냅 제안"' +
            ' style="left:calc(' + VL.pct(ratOf(rail.intervals[at].start),
              dur).toFixed(3) + '% - 6px)"></button>');
          html.push('<button type="button" class="vl-handle" data-vl-handle="right"' +
            (rightPair ? "" : " disabled") +
            ' aria-label="오른쪽 경계 손잡이 — 화살표로 초안, Enter로 스냅 제안"' +
            ' style="left:calc(' + VL.pct(ratOf(rail.intervals[at].end),
              dur).toFixed(3) + '% - 6px)"></button>');
        }
        html.push(rangeLayers(info, rail, dur));
        html.push('<div class="vl-playhead" id="vl-playhead" style="left:0"></div>');
        if (state.draft && state.draft.epoch === model.epoch && state.draft.sec) {
          html.push('<div class="vl-draftmark" style="left:' +
            VL.pct(state.draft.sec, dur).toFixed(3) + '%"></div>');
        }
        mount.innerHTML = html.join("");
      }

      function markerBlock(mk, card, pendingWord) {
        // 자막과 영상은 따로 판정한다(D51) — 패널은 두 판정을 함께 보인다. 판정 규칙의 식별자는
        // 접힌 상세(분석 근거)에만 한글 설명 곁에 둔다(디자인 README Voice).
        var out = ['<div class="vl-marker-reason ' + esc(mk.tier || "") + '"><strong>' +
          esc(mk.label) + '</strong>' +
          (pendingWord ? '<span class="vl-decision-state"> · 미검토</span>' : "")];
        if (mk.audio && mk.video) {
          out.push('<p class="vl-kv">자막 · ' + esc(mk.audio.label) + ': ' +
            esc(mk.audio.reason) + '</p><p class="vl-kv">영상 · ' + esc(mk.video.label) +
            ': ' + esc(mk.video.reason) + '</p>');
        } else if (mk.reason) {
          out.push('<p>' + esc(mk.reason) + '</p>');
        }
        (mk.caps || []).forEach(function(text) {
          out.push('<p class="vl-kv">추천 제한 · ' + esc(text) + '</p>');
        });
        if (mk.lift) { out.push('<p class="vl-kv">추천 근거 · ' + esc(mk.lift) + '</p>'); }
        if (mk.start !== undefined && mk.end !== undefined) {
          out.push('<small>' + esc(VL.fmt(ratOf(mk.start)) + '초 ~ ' +
            VL.fmt(ratOf(mk.end)) + '초') + '</small>');
        }
        out.push('</div>');
        return out.join("");
      }
      // 결정 상자는 지금 선택한 컷 하나만 겨냥한다 — 미검토 제외 제안이거나 빨강 막대가 이 컷을
      // 짚었을 때. 막대를 누르든 카드·컷을 고르든 같은 상자다. 다른 페이지의 조각은 겨냥하지
      // 않는다(D35).
      function redMarkerOf(card) {
        var mark = state.marker;
        return card && mark && mark.tier === "red" && mark.segmentId === card.id &&
          mark.sourceId === curSourceId() ? mark : null;
      }
      function decisionTarget(card) {
        return card && (card.autoPending || redMarkerOf(card)) ? card.id : null;
      }
      function redRowFor(info, card) {
        var mid = VL.mul(VL.add(ratOf(card.start), ratOf(card.end)), VL.parse("1/2"));
        return ((info && info.suggestions) || []).find(function (row) {
          return tierOf(row) === "red" && VL.cmp(ratOf(row.start), mid) <= 0 &&
            VL.cmp(ratOf(row.end), mid) > 0;
        }) || null;
      }
      // 컷 경계와 맞지 않는 제안 — 결정 단추는 제안 구간이 아니라 그 컷 전체를 정한다. 그렇게
      // 읽히지 않도록 두 범위와 나누는 길을 말한다(새 동작은 없다, D57 C 고침 1).
      function decisionScope(card, row) {
        if (!row || !row.loose) { return ""; }
        var number = cutNumbers(sourceInfo(card.sourceId))[card.id];
        return '<p class="vl-decision-scope">이 제외 제안(' + VL.fmt(ratOf(row.start)) + "~" +
          VL.fmt(ratOf(row.end)) + "초)은 컷 경계와 맞지 않습니다. 아래 두 단추는 제안 구간이" +
          " 아니라 컷" + (number ? " #" + number : "") + " 전체(" + VL.fmt(ratOf(card.start)) +
          "~" + VL.fmt(ratOf(card.end)) + "초)를 정합니다. 제안 구간만 정하려면 먼저" +
          " 「현재 위치에서 나누기」로 컷을 나누세요.</p>";
      }
      function decisionBlock(card, info) {
        var row = redMarkerOf(card) || redRowFor(info, card);
        var mk = Object.assign({ label: tierWord("red", false) }, row || {}, { tier: "red" });
        return '<section class="vl-decision" aria-label="제외 제안 결정">' +
          markerBlock(mk, card, !!card.autoPending) + decisionScope(card, row) +
          '<div class="vl-actions-2">' +
          '<button type="button" class="vl-btn vl-btn-danger" data-vl-act="red-confirm">' +
          '제외 유지(확인)</button>' +
          '<button type="button" class="vl-btn" data-vl-act="red-revert">' +
          '사용으로 되돌리기</button></div></section>';
      }
      // 제외 제안 대기열(D57 C) — 서버가 그릴 때마다 센 값이다(촬영 시각 순 · 영상 안 시각 순,
      // 보관하지 않는다). 미검토 수는 정보일 뿐 Final Cut으로 보내기를 막지 않는다(D56).
      function queueOf() { return model.queue || null; }
      function queueItems() { var q = queueOf(); return (q && q.items) || []; }
      function queueBlock() {
        var q = queueOf();
        var loose = q ? (q.loose || 0) : 0;
        if (!q || !(q.total > 0 || q.confirmable > 0 || loose > 0)) { return ""; }
        // 남은 것은 미검토 컷뿐이다(M = 일괄 확인이 확인하는 수). 「다음」은 그것만 걷고, 남은 것이
        // 없으면 꺼진다 — 제안이 하나도 없으면(경계와 맞지 않는 빨강만) 「다음」도 없다.
        var count = q.total > 0 ? "제외 제안 " + q.total + "개 중 <b>" + q.open + "개</b> 남음"
          : "남은 제외 제안이 없습니다.";
        var next = q.total > 0 ? '<button type="button" class="vl-btn vl-btn-sm"' +
          ' data-vl-act="queue-next"' + (q.open > 0 ? "" : " disabled") + ">다음</button>" : "";
        var out = ['<section class="vl-queue" aria-label="제외 제안 대기열">' +
          '<div class="vl-queue-head"><p class="vl-queue-count">' + count + "</p>" + next +
          "</div>"];
        if (q.open > 0) {
          out.push('<button type="button" class="vl-btn vl-btn-danger vl-btn-sm" ' +
            'data-vl-act="queue-confirm">남은 ' + q.open + "개 모두 제외 유지</button>" +
            '<p class="vl-queue-note">미검토 제외 제안 ' + q.open +
            "개는 <span class=\"vl-kw\">제외 제안(미검토)</span> 키워드로 갑니다 — 빨강은 " +
            "꺼진 채 갑니다(Final Cut에서 V로 켭니다).</p>");
        }
        if (loose > 0) {
          out.push('<p class="vl-queue-note">' + looseLine(loose, q.looseSources || []) + "</p>");
        }
        out.push("</section>");
        return out.join("");
      }
      // 경계와 맞지 않는 제안은 남은 수에 들지 않는다 — 대신 그것이 있는 영상을 셋까지 이름으로
      // 대고 나머지는 「외 N개」로 센다(D57 C 고침 2). 영상 카드는 목록 열에 그대로 보인다.
      function looseLine(count, sources) {
        var names = sources.slice(0, 3).map(function (s) { return esc(s.sourceName); }).join(", ");
        var more = sources.length > 3 ? " 외 " + (sources.length - 3) + "개 영상" : "";
        var where = names ? "가 " + names + more + "에 있습니다. 남은 수에 들지 않고, "
          : "는 남은 수에 들지 않고, ";
        return "컷 경계와 맞지 않는 제외 제안 " + count + "개" + where +
          "그 구간을 덮는 컷의 결정을 따릅니다. 따로 정하려면 그 구간에서" +
          " 「현재 위치에서 나누기」로 컷을 나누세요.";
      }
      function queueRank(sourceId) {
        var q = queueOf();
        return q && q.order ? q.order.indexOf(sourceId) : -1;
      }
      // 「다음」 — 지금 고른 컷 뒤의 첫 열린 제안(영상 카드를 골랐으면 그 영상의 첫 제안). 지금
      // 컷을 겨냥한 제안은 건너뛴다(그 결정 상자는 이미 열려 있다). 뒤에 없으면 처음으로 돌아간다.
      function nextQueueItem() {
        var items = queueItems();
        var card = selectedCard();
        if (!items.length || !card) { return items[0] || null; }
        var rank = queueRank(card.sourceId);
        var from = card.media ? null : VL.toFloat(ratOf(card.start));
        return items.find(function (it) {
          var r = queueRank(it.sourceId);
          if (r !== rank) { return r > rank; }
          return from === null || (it.segmentId !== card.id && VL.toFloat(ratOf(it.start)) > from);
        }) || items[0];
      }
      // 빨강 막대를 누른 것과 같은 표지 — 결정 상자는 표지가 짚은 컷을 겨냥한다(같은 길 하나).
      // 줄의 빨강 구간이 있으면 그 막대를, 없으면 컷 범위를 짚는다.
      function queueMarker(item) {
        var info = sourceInfo(item.sourceId);
        var row = item.redStart === undefined ? null :
          ((info && info.suggestions) || []).find(function (r) {
            return r.start === item.redStart && r.end === item.redEnd;
          });
        return Object.assign({ sourceId: item.sourceId, label: tierWord("red", false),
          segmentId: item.segmentId, tierSpan: !!row, queued: true },
          row || { start: item.start, end: item.end, tier: "red" });
      }
      // 옮길 시각 — 빨강 시작과 컷 시작 중 늦은 쪽(빨강이 컷보다 앞이면 이웃 컷으로 떨어진다).
      function queueSeek(item) {
        var cut = ratOf(item.start);
        var red = item.redStart === undefined ? null : ratOf(item.redStart);
        return red && VL.cmp(red, cut) > 0 ? red : cut;
      }
      function focusQueueItem(item) {
        if (!item) { countFoot("남은 제외 제안이 없습니다."); return; }
        var said = "제외 제안 · " + item.sourceName + " " + VL.fmt(queueSeek(item), 1) + "초";
        if (item.sourceId === curSourceId() && findCut(item.segmentId)) {
          state.marker = queueMarker(item);
          selectCard(item.segmentId, false);
          seekTo(queueSeek(item));
          msg(said);
          return;
        }
        // 다른 영상이나 이 쪽 밖의 컷 — 서버가 그 컷을 고른 값을 다시 그린다(분석 선택·견적 준비는
        // 하지 않는다). 줄은 미검토 제외 컷이라 「사용할 컷」 밖 어디서나 보인다. 그사이 그 컷이
        // 사라졌으면(나누기·합치기) 서버가 값 없이 알린다 — 활동 칸에 그 말을 남긴다.
        if (state.filter === "included") { state.filter = "all"; }
        call("vl_refresh", { page: model.page, focusId: item.segmentId, sourceId: item.sourceId,
          basePlanId: model.basePlanId }, "다음 제외 제안으로 옮기는 중입니다…", function (res) {
          if (res && res.ok && res.focusStale) {
            if (W.__vlActivity) { W.__vlActivity.reply(res.message); }
            return;
          }
          if (res && res.ok && res.html) { state.queueFocus = { item: item, said: said }; }
        });
      }
      // 「다음」의 답이 커밋되면 — 서버가 그 컷을 골랐을 때만 그 컷과 빨강 구간을 고르고 그
      // 시각으로 옮긴다. 고른 컷은 같은 리비전 동안 기억한다(이 쪽 목록 밖이어도 2초 틱에 남는다).
      function applyQueueFocus() {
        var pending = state.queueFocus;
        state.queueFocus = null;
        if (!pending || !model.selectedCard || model.selectedId !== pending.item.segmentId) {
          return null;
        }
        state.selectedId = model.selectedId; state.selectedPlan = model.basePlanId;
        state.focusCard = { card: model.selectedCard, plan: model.basePlanId,
          rev: model.revisionId };
        state.marker = queueMarker(pending.item);
        state.lastMsg = pending.said;
        return queueSeek(pending.item);
      }
      function keepFocusCard() {
        var focus = state.focusCard;
        if (focus && model.selectedCard && model.selectedCard.id === focus.card.id) {
          state.focusCard = { card: model.selectedCard, plan: model.basePlanId,
            rev: model.revisionId };
        }
      }
      // 일괄 확인은 옛 「자동 제외 확인」의 그라디오 처리기 그대로다 — 그 숨은 단추를 누른다(서버
      // 함수 길이 아니다: 같은 처리기 · 같은 서비스 호출 · 활동 칸 응답 · 알림).
      function confirmAllProposals() {
        var source = doc.getElementById("vl-queue-confirm-src");
        if (source && source.tagName && source.tagName !== "BUTTON" && source.querySelector) {
          source = source.querySelector("button") || source;
        }
        if (!source || typeof source.click !== "function") {
          msg("일괄 확인 단추를 찾지 못했습니다. 새로고침하세요.");
          return;
        }
        // 답은 그 처리기의 자리(활동 칸·알림)로 온다 — 상태 줄에 '하는 중'을 남기지 않는다.
        source.click();
      }
      // 패널을 다시 그려도 사람이 펼쳐 둔 접힌 상세는 펼친 채로 둔다.
      function openedDetails(mount) {
        var opened = {};
        if (typeof mount.querySelectorAll !== "function") { return opened; }
        Array.prototype.forEach.call(mount.querySelectorAll("details[data-vl-keep]"),
          function (d) { if (d.open) { opened[d.getAttribute("data-vl-keep")] = true; } });
        return opened;
      }
      function details(opened, key, summary, body) {
        return '<details class="vl-details" data-vl-keep="' + key + '"' +
          (opened[key] ? " open" : "") + "><summary>" + summary + "</summary>" + body +
          "</details>";
      }
      function cardStatusLines(card) {
        var out = [];
        var speech = speechLabel(card.speech);
        var rec = recLabel(card.rec);
        var grade = card.autoPending ? "비추천(미확인)" : tierWord(card.tier || "", false);
        var subtitleWhy = tagText(card, "자막: ") || speech;
        var videoWhy = tagText(card, "영상: ");
        out.push('<p class="vl-kv">상태 ' + (card.sel === "excluded" ? "제외" : "사용") +
          (grade ? " · 등급 " + esc(grade) : "") +
          (subtitleWhy ? " · 자막 " + esc(subtitleWhy) : "") +
          (videoWhy ? " · 영상 " + esc(videoWhy) : "") + "</p>");
        out.push('<p class="vl-kv">' + (speech ? esc(speech) : "") +
          (speech && rec ? " · " : "") + (rec ? "권고 " + esc(rec) : "") + "</p>");
        var automatic = autoNotes(card)[0];
        if (automatic) {
          out.push('<p class="vl-kv">자동 판정 이유 · ' + esc(autoReason(automatic.reason)) +
            "</p>");
        }
        return out.join("");
      }
      // 기계 근거(서버가 메모에서 가른 줄, D57 C 고침 2) — 메모 칸에는 사람 메모만 있다.
      function autoNotes(card) {
        return (card.machine || []).filter(function (row) { return row.kind === "auto"; });
      }
      function autoReason(reason) {
        return ({ internal_silence: "긴 무음", retake_overlap: "완전한 반복 테이크 확인",
          ng_with_followup: "NG 뒤 완전한 테이크 확인",
          tier_silence: "자막 없음 · AI가 풍경으로 보지 않음",
          tier_video_defect: "영상 결함(옛 등급판)" })[reason] || "기록된 이유";
      }
      // 「분석 근거」의 기계 줄 — 날 JSON 대신 이유 이름과 구간(초)을 쉬운 말로.
      function machineLines(card) {
        return (card.machine || []).map(function (row) {
          if (row.kind !== "auto") {
            return '<p class="vl-kv">확인 필요 · ' + esc(row.text) + "</p>";
          }
          var span = row.start && row.end ? " · " + VL.fmt(ratOf(row.start), 1) + "~" +
            VL.fmt(ratOf(row.end), 1) + "초" : "";
          return '<p class="vl-kv">자동 제외 근거 · ' + esc(autoReason(row.reason)) + span + "</p>";
        }).join("");
      }
      function panelNotices(card, info) {
        var out = [];
        if (model.redUnmaterialized > 0) {
          out.push('<p class="vl-kv"><span class="vl-badge vl-badge-red">제외 제안 ' +
            model.redUnmaterialized + '개</span> 컷 경계와 맞지 않아 그 구간을 덮는 컷의 결정을' +
            " 따릅니다. 따로 정하려면 「현재 위치에서 나누기」로 컷을 나누세요.</p>");
        }
        if (info && info.mediaStatus) {
          out.push('<p class="vl-kv">' + esc(info.mediaStatus) + '</p>');
          if (info.originalMissing) {
            out.push('<label>원본 경로<input id="vl-original-path" class="vl-input"></label>' +
              '<button type="button" class="vl-btn" data-vl-act="relink-original">' +
            '원본 재연결</button>');
          }
        }
        out.push(syncPanel(info));
        out.push(snapPanel(info));
        out.push(relistenPanel(info));
        out.push(refillPanel(info));
        ((info && info.crossSourceCandidates) || []).forEach(function(candidate) {
          out.push('<p class="vl-kv">다른 원본의 중복 후보 · ' +
            esc(candidate.other_source + ': ' + candidate.text) +
            ' · 자동으로 제외하지 않습니다.</p>');
        });
        if (info && info.transcriptDirty) {
          out.push('<p class="vl-kv">자막 변경 · 분석 갱신 필요</p>');
        }
        if (card.needsExtract) {
          out.push('<p class="vl-kv"><span class="vl-badge vl-badge-warn">영상 갱신 필요</span>' +
            " 경계가 계획과 달라 새로 추출해야 갱신됩니다.</p>");
        }
        if (info && !info.src) {
          out.push('<p class="vl-kv"><span class="vl-badge vl-badge-warn">미리보기 ' +
            stateLabel(info.state) + "</span>" +
            (info.error ? " " + esc(info.error)
              : " 「재생 설정」의 「미리보기 다시 준비」로 준비할 수 있습니다.") + "</p>");
        }
        return out.join("");
      }
      // 「다른 귀」·「확인 필요」(D59 19e) — 표시만 한다. 자막을 지우거나 숨기거나 고치지 않는다.
      // 「candidate」는 Gemini 원본(D92) — 엔진의 글자와 시각이 한 응답이라 다른 귀는 후보로만.
      var EAR_CANDIDATE_NOTE = "전사 엔진이 들은 글을 그대로 두었습니다 — 다른 귀의 글자는" +
        " 후보로만 보입니다";
      function earNote(ear) {
        if (!ear) { return ""; }
        if (ear.mark === "candidate") { return EAR_CANDIDATE_NOTE; }
        return ear.mark === "alt" ? "다른 귀는 다르게 들었습니다 — 고르지 않은 후보가 있습니다"
          : "다른 귀가 고친 줄입니다 — 처음 들은 글과 다릅니다";
      }
      function earWord(ear) {
        return ear && (ear.mark === "alt" || ear.mark === "candidate") ? "다른 귀 후보"
          : "다른 귀로 고침";
      }
      function suspectNote(cue) {
        return (cue.suspects || []).map(function (s) { return s.text; }).join(" · ");
      }
      function cueTitle(cue) {
        var parts = [cue.text];
        if (cue.ear) { parts.push(earNote(cue.ear)); }
        if (cue.suspects && cue.suspects.length) { parts.push("확인 필요 — " + suspectNote(cue)); }
        return parts.join(" · ");
      }
      function cueBadges(cue) {
        var out = "";
        if (cue.ear) {
          out += ' <span class="vl-badge" data-vl-ear="' + esc(cue.ear.mark) + '" title="' +
            esc(earNote(cue.ear)) + '">' + esc(earWord(cue.ear)) + "</span>";
        }
        if (cue.suspects && cue.suspects.length) {
          out += ' <span class="vl-badge" data-vl-suspect="1" title="' + esc(suspectNote(cue)) +
            '">확인 필요</span>';
        }
        return out + truthBadges(cue);
      }
      // 시각 정답(W-25b) — 권하는 층과 찍은 정답 수를 보일 뿐이다. 자막은 그대로다.
      function truthBadges(cue) {
        var out = "";
        if (cue.truthStratum) {
          out += ' <span class="vl-badge" data-vl-truth-stratum="' +
            esc(cue.truthStratum.code) + '" title="시각 정답을 찍어 볼 줄">' +
            esc(cue.truthStratum.label) + "</span>";
        }
        var marked = ["start", "end"].filter(function (edge) {
          return cue.truth && cue.truth[edge];
        }).length;
        if (marked) {
          out += ' <span class="vl-badge" data-vl-truth="' + marked + '">정답 ' + marked +
            "/2</span>";
        }
        return out;
      }
      function truthValue(cue, edge) {
        var mark = cue.truth && cue.truth[edge];
        return mark ? VL.fmt(ratOf(mark.at)) + "초" : "아직 없음";
      }
      function truthLine(cue, edge) {
        var label = edge === "start" ? "참 시작" : "참 끝";
        var mark = cue.truth && cue.truth[edge];
        return '<p class="vl-kv vl-truth-line"><button type="button" class="vl-btn" ' +
          'data-vl-act="truth-mark" data-vl-edge="' + edge + '">' + label + "</button> " +
          esc(label + " " + truthValue(cue, edge)) +
          (mark ? ' <button type="button" class="vl-btn" data-vl-act="truth-clear" ' +
            'data-vl-edge="' + edge + '" aria-label="' + label + ' 지우기">지우기</button>' : "") +
          (mark && mark.stale ? " <small>자막 줄이 바뀌어 낡은 정답입니다 — 다시 찍으세요." +
            "</small>" : "") + "</p>";
      }
      function truthLines(cue) {
        var out = ['<div class="vl-truth" role="group" aria-label="시각 정답">'];
        if (cue.truthStratum) {
          out.push('<p class="vl-kv"><span class="vl-badge" data-vl-truth-stratum="' +
            esc(cue.truthStratum.code) + '">' + esc(cue.truthStratum.label) +
            "</span> 시각 정답을 찍어 볼 줄입니다.</p>");
        }
        out.push(truthLine(cue, "start"));
        out.push(truthLine(cue, "end"));
        out.push('<p class="vl-kv"><small>들어 보고 말이 실제로 시작·끝나는 자리에서 멈춘 뒤 ' +
          "「참 시작」·「참 끝」을 누르세요(「,」·「.」로 한 프레임씩 옮길 수 있습니다). " +
          "정답은 자막과 따로 저장하고 자막은 그대로입니다 — 시각 정확도 보고서만 " +
          "읽습니다.</small></p></div>");
        return out.join("");
      }
      function earLine(label, text, take) {
        // 긴 후보는 줄 안에서 스크롤한다(높이 상한은 CSS) — 키보드로도 닿게 초점을 받는다.
        return '<p class="vl-kv vl-ear-line"><span class="vl-ear-text" tabindex="0">' + label +
          ": " + esc(text) + '</span><button type="button" class="vl-btn" data-vl-act="ear-take" ' +
          'data-vl-take="' + take + '" aria-label="' + label + ' 가져오기">가져오기</button></p>';
      }
      function earLines(cue) {
        var ear = cue.ear;
        if (!ear) { return ""; }
        var out = ['<div class="vl-ear" role="group" aria-label="다른 귀">'];
        out.push(earLine("다른 귀", ear.other, "other"));
        if (ear.first) {
          var firstLabel = "처음 들은 글" + (ear.firstEngine ? " (" + ear.firstEngine + ")" : "");
          out.push(earLine(firstLabel, ear.first, "first"));
        }
        out.push('<p class="vl-kv"><small>' + (ear.mark === "candidate" ? EAR_CANDIDATE_NOTE + "."
          : ear.mark === "alt" ? "두 귀가 맞지 않아 글자를 바꾸지 않았습니다."
          : "두 귀가 맞춰 본 줄이라 다른 귀의 글자로 바꿨습니다.") +
          " 「가져오기」는 문구 칸만 채웁니다 — 「저장」을 눌러야 바뀝니다.</small></p></div>");
        return out.join("");
      }
      // Gemini가 이 줄의 창 클립에서 들은 말(D88) — 창 전체라 줄보다 넓을 수 있다. 참고만 한다.
      function geminiLines(cue) {
        var heard = cue.gemini && cue.gemini.heard;
        if (!heard) { return ""; }
        return '<div class="vl-ear" role="group" aria-label="Gemini가 들은 말">' +
          earLine("Gemini가 들은 말(이 구간 전체)", heard, "gemini") +
          '<p class="vl-kv"><small>Gemini는 이 줄이 든 분석 창의 클립 전체를 들었습니다' +
          " — 이 줄보다 넓을 수 있어 필요한 부분만 남기세요. 「가져오기」는 문구 칸만 채웁니다" +
          " — 「저장」을 눌러야 바뀝니다.</small></p></div>";
      }
      function suspectLines(cue) {
        if (!cue.suspects || !cue.suspects.length) { return ""; }
        return '<p class="vl-kv"><span class="vl-badge" data-vl-suspect="1">확인 필요</span> ' +
          esc(suspectNote(cue)) + '</p><p class="vl-kv"><small>표시만 합니다 — 자막은 그대로' +
          "입니다.</small></p>";
      }
      // 「가져오기」로 넣은 글자 — 같은 줄·같은 리비전이면 패널을 다시 그려도 문구 칸에 남는다.
      function takenText(cue, info) {
        var take = state.sourceTake;
        return take && take.sourceId === curSourceId() && take.id === cue.id &&
          take.revision === info.transcriptRevision ? take.text : cue.text;
      }
      var EAR_TAKE_NOTE = "문구 칸에 넣었습니다 — 「저장」을 눌러야 원본 자막이 바뀝니다.";
      // 다른 줄을 고르면 가져온 글자를 버린다 — 그 각주도 걷는다(다른 각주는 그대로 둔다).
      function dropTake() {
        if (!state.sourceTake) { return; }
        state.sourceTake = null;
        if (state.lastMsg === EAR_TAKE_NOTE) { msg(""); }
      }
      // 「가져오기」 — 문구 칸만 채운다. 서버를 부르지 않는다(저장은 사람이 「저장」으로 한다).
      function takeEar(which) {
        var info = curSource();
        var cue = info && state.sourceCue && state.sourceCue.sourceId === curSourceId() ?
          (info.subtitles || []).find(function (u) { return u.id === state.sourceCue.id; }) : null;
        var ear = cue && cue.ear;
        var text = which === "gemini" ? (cue && cue.gemini && cue.gemini.heard) || "" :
          ear ? (which === "first" ? ear.first : ear.other) : "";
        var box = doc.getElementById("vl-source-text");
        if (!text || !box) { return; }
        box.value = text;
        state.sourceTake = { sourceId: curSourceId(), id: cue.id,
          revision: info.transcriptRevision, text: text };
        msg(EAR_TAKE_NOTE);
      }
      function captionEditor(info) {
        var cue = info && (info.subtitles || []).find(function(u) {
          return state.sourceCue && state.sourceCue.sourceId === curSourceId() &&
            u.id === state.sourceCue.id;
        });
        if (!cue) { return ""; }
        return '<fieldset class="vl-source-caption-editor"><legend>원본 자막 수정</legend>' +
          earLines(cue) + geminiLines(cue) + suspectLines(cue) + truthLines(cue) +
          '<label>문구<textarea id="vl-source-text" class="vl-input">' +
          esc(takenText(cue, info)) + '</textarea></label>' +
          '<label>시작(초)<input id="vl-source-start" class="vl-input" value="' +
          VL.fmt(ratOf(cue.start)) + '"></label>' +
          '<label>끝(초)<input id="vl-source-end" class="vl-input" value="' +
          VL.fmt(ratOf(cue.end)) + '"></label>' +
          '<button type="button" class="vl-btn" data-vl-act="source-edit">저장</button>' +
          '<label>분할 뒤 문구<textarea id="vl-source-right" class="vl-input">' +
          '</textarea></label>' +
          '<button type="button" class="vl-btn" data-vl-act="source-split">' +
          '현재 위치에서 자막 분할</button>' +
          '<button type="button" class="vl-btn" data-vl-act="source-merge">' +
          '다음 자막과 병합</button>' +
          '<small>컷 검토용 원본 시간입니다. ' +
          '최종 SRT는 최종 영상에서 생성합니다.</small></fieldset>';
      }
      function draftBox() {
        if (!state.draft || state.draft.epoch !== model.epoch) { return ""; }
        var d = state.draft;
        var out = ['<div class="vl-draft"><p class="vl-kv">' +
          (d.kind === "split"
            ? "나누기 초안 — " + VL.fmt(d.sec) + "초에서 둘로 나눕니다."
            : "경계 초안 — " + (d.side === "left" ? "왼쪽" :
              "오른쪽") + " 경계를 " + VL.fmt(d.sec) + "초로 옮깁니다.") +
          "</p>"];
        if (d.pending) {
          out.push('<p class="vl-kv">프레임에 맞춘 시각을 계산하는 중입니다…</p>');
        } else if (d.proposal && d.proposal.valid) {
          // 보이는 줄은 사람 시각(초 소수 셋째) — 정확한 유리수·프레임은 접힌
          // 상세가 싣는다(요청 payload는 제안의 정확값 그대로).
          out.push('<p class="vl-kv">확정 시각 <strong>' +
            VL.fmt(ratOf(d.proposal.snapSeconds)) + "초</strong> (프레임 " +
              d.proposal.frame + ", 동률이면 앞 프레임) — 저장하면 새 리비전이 생깁니다.</p>");
          out.push('<details class="vl-details"><summary>스냅 정확값</summary>' +
            '<p class="vl-kv">정확 시각 <code>' + esc(d.proposal.snapSeconds) +
            "</code>초 · 프레임 <code>" + d.proposal.frame + "</code></p></details>");
          out.push('<div class="vl-row">' +
            '<button type="button" class="vl-btn vl-btn-primary" data-vl-act="apply-draft">' +
            (d.kind === "split" ? "나누기 적용" : "경계 적용") + '</button>' +
            '<button type="button" class="vl-btn" data-vl-act="cancel-draft">' +
            "취소</button></div>");
        } else {
          // 기본 줄은 사람 시각으로만 — 서버 판정의 원문(PTS 등)은 접힌 상세가
          // 그대로 싣는다(빈 성공 꾸미기 없음, 원문 보존).
          out.push('<p class="vl-kv">적용 불가 — ' + VL.fmt(d.sec) + "초에서는 " +
            (d.kind === "split" ? "나눌 수" : "경계를 옮길 수") + " 없습니다.</p>");
          out.push('<details class="vl-details"><summary>서버 판정 사유</summary>' +
            '<p class="vl-kv"><code>' +
            esc((d.proposal && (d.proposal.reason || d.proposal.error)) || "사유 없음") +
            "</code></p></details>");
          out.push('<div class="vl-row">' +
            '<button type="button" class="vl-btn" data-vl-act="cancel-draft">' +
            "취소</button></div>");
        }
        out.push("</div>");
        return out.join("");
      }
      function editRow(card) {
        return '<div class="vl-row vl-edit">' +
          '<button type="button" class="vl-btn" data-vl-act="split">현재 위치에서 나누기</button>' +
          '<button type="button" class="vl-btn" data-vl-act="merge"' +
            (card.neighbor ? "" : " disabled") + ">오른쪽 컷과 합치기</button>" +
          (model.needsPlan
            ? '<button type="button" class="vl-btn vl-btn-primary" data-vl-act="replan">' +
              "계획 반영 · 재추출</button>"
            : "") +
          "</div>";
      }
      function playbackSettings(info) {
        var body = "";
        if (info && info.tracks && info.tracks.length) {
          var opts = [];
          var current = info.audioOrdinal === null || info.audioOrdinal === undefined
            ? "silence" : String(info.audioOrdinal);
          for (var i = 0; i < info.tracks.length; i += 1) {
            var tr = info.tracks[i];
            opts.push('<option value="' + tr.ordinal + '"' +
              (String(tr.ordinal) === current ? " selected" :
                "") + ">" + esc(tr.label) + "</option>");
          }
          opts.push('<option value="silence"' + (current === "silence" ?
            " selected" : "") + ">침묵(영상만)</option>");
          body += '<label class="vl-kv vl-field">소리 트랙' +
            '<select id="vl-track" class="vl-input">' + opts.join("") + "</select></label>";
        }
        return body + '<div class="vl-row">' +
          '<button type="button" class="vl-btn" data-vl-act="prepare">미리보기 다시 준비</button>' +
          '<button type="button" class="vl-btn" data-vl-act="refresh">새로고침</button></div>';
      }
      function identityDetails(card, info) {
        return '<p class="vl-kv">컷 <code>' + esc(card.id) +
            "</code> · 원본 <code>" + esc(card.sourceId) + "</code></p>" +
          '<p class="vl-kv">검토 리비전 <code>' + esc(model.revisionId) +
            "</code> · 기본 계획 <code>" +
          esc(model.basePlanId) + "</code></p>" +
          (info
            ? '<p class="vl-kv">미리보기 ' + esc(info.state) +
              (info.error ? " — " + esc(info.error) : "") +
              " · 소리 " + (info.audioOrdinal === null || info.audioOrdinal === undefined
                ? "침묵" : "트랙 " + info.audioOrdinal) + "</p>" +
            '<p class="vl-kv">브라우저 시각 = 원본 시각 + ' + VL.fmt(ratOf(info.origin)) +
              "초(출력 영상 첫 PTS · <code>" + esc(VL.str(ratOf(info.origin))) +
              "</code> · <code>G3</code>)</p>"
            : "") +
          // 날 열거값은 이 상세 줄에만, 한글 이름 곁의 식별자로(디자인 README Voice).
          '<p class="vl-kv">발화 상태 ' + esc(speechLabel(card.speech) || "-") +
            " <code>" + esc(card.speech || "-") + "</code> · 권고 " +
            esc(recLabel(card.rec) || "-") + " <code>" + esc(card.rec || "-") + "</code></p>" +
          (model.activeJobs && model.activeJobs.length
            ? '<p class="vl-kv">준비 작업 ' + model.activeJobs.length +
              "개가 진행 중입니다. 끝나면 새로고침하세요.</p>"
            : "") +
          (model.staleSources && model.staleSources.length
            ? '<p class="vl-kv">낡은 원본 ' + model.staleSources.length +
              "개 — 검토한 뒤 원본이 바뀌었습니다.</p>"
            : "");
      }
      // 판정 규칙 식별자 — 이 컷을 짚은 막대(표지) 또는 이 컷의 빨강 구간의 규칙. 상세에만 보인다.
      function judgedRule(card, info) {
        var mark = state.marker && state.marker.sourceId === curSourceId() ? state.marker : null;
        var row = mark && mark.rule ? mark : redRowFor(info, card);
        return row && row.rule ? row.rule : "";
      }
      // 목록 머리 — 확인 필요·다른 귀 줄 수(있을 때만). 표시만 하므로 걸러 보이지 않는다.
      function cueListSummary(subtitles) {
        var suspects = subtitles.filter(function (u) {
          return u.suspects && u.suspects.length;
        }).length;
        var ears = subtitles.filter(function (u) { return !!u.ear; }).length;
        return "원본 자막 목록" + (suspects ? " · 확인 필요 " + suspects + "개" : "") +
          (ears ? " · 다른 귀 " + ears + "개" : "");
      }
      // 사라진 줄의 시각 정답(W-25b) — 목록 맨 위 한 줄. 정답은 보고서에서 빠진다.
      function truthMissingLine(info) {
        return info && info.truthMissing ?
          '<p class="vl-kv"><small>' + esc(info.truthMissing) + "</small></p>" : "";
      }
      function folded(card, info, opened) {
        // 접힌 상세 — 사람의 메모는 기계 근거(분석 근거)와 따로 둔다(D52).
        var out = [];
        if (info && info.subtitles && info.subtitles.length) {
          out.push(details(opened, "cues", cueListSummary(info.subtitles),
            truthMissingLine(info) + info.subtitles.map(function (u) {
              return '<button type="button" class="vl-btn" data-vl-act="source-pick" ' +
                'data-vl-cue-id="' + esc(u.id) + '" title="' + esc(cueTitle(u)) + '">' +
                esc(VL.fmt(ratOf(u.start)) + '초 · ' + u.text) + cueBadges(u) + '</button>';
            }).join("")));
        }
        var tags = card.tags || [];
        var rule = judgedRule(card, info);
        var machine = machineLines(card);
        if (tags.length || rule || machine) {
          out.push(details(opened, "evidence", "분석 근거", (rule ? '<p class="vl-kv">판정 규칙 ' +
            "<code>" + esc(rule) + "</code> · 자막 판정과 영상 판정을 합친 표의 칸</p>" : "") +
            machine + (tags.length ? '<p class="vl-kv">' +
              tags.map(function (t) { return esc(t); }).join(" · ") + "</p>" : "")));
        }
        // 여러 줄 칸 — 사람의 줄을 그대로 보이고 저장한다(합친 컷의 두 메모를 한 줄로 붙이지
        // 않는다, D57 C 고침 4). Enter는 줄바꿈이다(저장 단축키는 없다 — 「메모 저장」을 누른다).
        out.push(details(opened, "memo", "메모", '<div class="vl-row vl-memo">' +
          '<textarea id="vl-note-input" class="vl-input vl-memo-text" aria-label="메모" rows="3">' +
          esc(card.note) + '</textarea><button type="button" class="vl-btn"' +
          ' data-vl-act="save-note">메모 저장</button></div>'));
        out.push(details(opened, "play", "재생 설정", playbackSettings(info)));
        out.push(details(opened, "ids", "상세 — 식별자·리비전·시계·준비 상태",
          identityDetails(card, info)));
        return out.join("");
      }
      // 사용 | 제외 — 눌린 쪽이 지금 상태다. 누를 수 있는 것은 반대쪽 하나이고 처리기는 옛
      // 토글(toggle-sel) 그대로다 — 눌린 쪽에는 동작이 없다(D57 C).
      function useExcludeControl(card) {
        var excluded = card.sel === "excluded";
        function segment(label, pressed) {
          return '<button type="button" class="vl-btn"' +
            (pressed ? "" : ' data-vl-act="toggle-sel"') + ' aria-pressed="' + pressed + '">' +
            label + "</button>";
        }
        return '<div class="vl-seg" role="group" aria-label="이 컷 사용 여부">' +
          segment("사용", !excluded) + segment("제외", excluded) + "</div>";
      }
      function panelHead(card) {
        var number = card.media ? 0 : cutNumbers(sourceInfo(card.sourceId))[card.id];
        return '<p class="vl-kv vl-panel-head"><strong>' + esc(card.sourceName) + "</strong> · " +
          (number ? "컷 #" + number + " · " : "") + VL.fmt(ratOf(card.start)) + "~" +
          VL.fmt(ratOf(card.end)) + "초 (" + VL.fmt(ratOf(card.dur)) + "초)</p>";
      }
      // 패널 순서는 고정이다(D57 B): 머리 · 결정 · 판정 · 알림 · 편집 · 접힌 상세. 제외 제안
      // 대기열은 목록 열 맨 위다 — 결정 상자가 1280×800에서 패널 위쪽을 지킨다(D57 C).
      function renderPanel() {
        var mount = doc.getElementById("vl-panel");
        if (!mount) { return; }
        var card = selectedCard();
        if (!card) {
          mount.innerHTML =
            '<p class="vl-note">고른 컷이 없습니다. 영상을 고르면 컷이 여기에 나타납니다.</p>';
          return;
        }
        var info = curSource();
        var opened = openedDetails(mount);
        var html = [panelHead(card)];
        var decided = decisionTarget(card);
        if (decided) {
          html.push(decisionBlock(card, info));
        } else {
          html.push('<div class="vl-row vl-decide">' + useExcludeControl(card) + "</div>");
        }
        if (state.marker && state.marker.sourceId === curSourceId() &&
            !(decided && redMarkerOf(card))) {
          html.push(markerBlock(state.marker, card, false));
        }
        html.push(captionEditor(info));
        html.push(cardStatusLines(card));
        html.push(panelNotices(card, info));
        html.push(editRow(card));
        html.push(draftBox());
        html.push(folded(card, info, opened));
        mount.innerHTML = html.join("");
        // 빈칸 재전사 승인을 거절했으면 견적 칸을 보이게 한다 — 거절 응답이 칸을 다시 그린
        // **뒤**에도(그 전에 옮긴 스크롤은 다시 그리기가 흔든다).
        if (state.refillReveal) { state.refillReveal = false; refillReveal(); }
      }

      function paintPlayhead() {
        var v = videoEl();
        var mark = doc.getElementById("vl-playhead");
        var clock = doc.getElementById("vl-clock");
        var info = curSource();
        var local = v && info && info.src ? localOf(v.currentTime) : null;
        var rail = railOf();
        if (mark && rail) {
          var dur = ratOf(rail.duration);
          if (dur && local) {
            var pct = VL.toFloat(VL.div(displayTime(local), dur)) * 100;
            mark.style.left = Math.max(0, Math.min(100, pct)).toFixed(3) + "%";
          }
        }
        if (clock) { clock.textContent = local ? VL.fmt(local) : ""; }
      }

      function tick() {
        var v = videoEl();
        if (state.listenEnd && v) { listenDone(v); }
        if (state.loop && v) {
          var endB = VL.toFloat(VL.add(state.loop.end, originRat()));
          var startB = VL.toFloat(VL.add(state.loop.start, originRat()));
          if (v.currentTime >= endB) { v.currentTime = startB; }
        }
        if (state.keptPlaying && v && (!v.paused || v.ended)) {
          var info = curSource();
          var local = VL.toFloat(localOf(v.currentTime));
          var kept = info && info.rail ? info.rail.intervals.filter(function(i) {
            return i.sel !== "excluded";
          }) : [];
          var next = kept.find(function(i) { return VL.toFloat(ratOf(i.end)) > local + 0.015; });
          if (next && local < VL.toFloat(ratOf(next.start))) { seekTo(ratOf(next.start)); }
          if (!next) {
            var ids = Object.keys(model.sources || {});
            var idx = ids.indexOf(curSourceId());
            var target = null;
            for (var k = idx + 1; k < ids.length && !target; k += 1) {
              var candidate = model.sources[ids[k]];
              var first = candidate.rail && candidate.rail.intervals.find(function(i) {
                return i.sel !== "excluded";
              });
              if (candidate.src && first) {
                target = { info: candidate, id: ids[k], first: first };
              }
            }
            if (target) {
              state.playingSource = target.id; applySource(target.info, v);
              seekTo(ratOf(target.first.start)); v.play(); renderRail();
            } else { state.keptPlaying = false; v.pause(); }
          }
        }
        paintPlayhead(); paintSourceCaptions();
      }
      var v0 = videoEl();
      var vPrev = W.__vlVisualVideoEl;
      if (vPrev && vPrev !== v0) {
        // 껍데기가 다시 그려져 옛 영상 원소가 문서에서 떨어졌다 — 능동 언로드.
        // 브라우저는 떨어진 원소의 소리·재생 상태를 스스로 거두지 않으므로
        // 여기서 정지·청취 해제·주소 제거까지 한다(옛 반복도 함께 지운다).
        if (vPrev._vlTick) { vPrev.removeEventListener("timeupdate", vPrev._vlTick); }
        if (vPrev._vlLoopOff) { vPrev.removeEventListener("pause", vPrev._vlLoopOff); }
        ['seeking','seeked','pause','ratechange','loadedmetadata','emptied']
          .forEach(function(event) {
          if (vPrev._vlCaptionSync) { vPrev.removeEventListener(event,vPrev._vlCaptionSync); }
        });
        clearSourceCaptions(vPrev);
        stopLoop();
        try {
          vPrev.pause();
          vPrev.removeAttribute("src");
          vPrev.load();
        } catch (err) {
          // 언로드 최선 — 재생 정지가 본질이다
        }
      }
      if (v0) {
        W.__vlVisualVideoEl = v0;
        if (v0._vlTick) { v0.removeEventListener("timeupdate", v0._vlTick); }
        if (v0._vlLoopOff) { v0.removeEventListener("pause", v0._vlLoopOff); }
        v0._vlTick = tick;
        v0._vlLoopOff = stopLoop;
        v0.addEventListener("timeupdate", tick);
        if (v0._vlEnded) { v0.removeEventListener("ended", v0._vlEnded); }
        v0._vlEnded = tick; v0.addEventListener("ended", tick);
        v0.addEventListener("pause", stopLoop);
        ['seeking','seeked','pause','ratechange','loadedmetadata','emptied']
          .forEach(function(event) {
          if (v0._vlCaptionSync) { v0.removeEventListener(event,v0._vlCaptionSync); }
          v0.addEventListener(event,paintSourceCaptions);
        });
        v0._vlCaptionSync = paintSourceCaptions;
      }

      function railRect() {
        var mount = doc.getElementById("visual-rail");
        return mount && mount.getBoundingClientRect ? mount.getBoundingClientRect() : null;
      }
      function secAt(clientX) {
        var rect = railRect();
        var rail = railOf();
        if (!rect || !rail || rect.width <= 0) { return null; }
        var px = Math.max(0, Math.min(rect.width, clientX - rect.left));
        var dur = ratOf(rail.duration);
        return VL.rat(Math.round(px * dur[0]), Math.round(rect.width * dur[1]));
      }
      function boundaryPair(side) {
        var card = selectedCard();
        var rail = railOf();
        if (!card || !rail) { return null; }
        var at = railIndex(card.id);
        if (at < 0) { return null; }
        var pair = side === "left"
          ? { left: rail.intervals[at - 1], right: rail.intervals[at] }
          : { left: rail.intervals[at], right: rail.intervals[at + 1] };
        if (!pair.left || !pair.right) { return null; }
        return pair;
      }
      function propose(kind, draft) {
        state.draft = draft;
        renderPanel();
        renderRail();
        var payload = { op: kind, revisionId: model.revisionId, basePlanId: model.basePlanId,
            page: model.page, sourceId: curSourceId(),
            selectedId: selectedId(), playheadSec: VL.str(playheadLocal()),
            desiredSec: VL.str(draft.sec) };
        if (kind === "boundary") { payload.leftId = draft.leftId; payload.rightId = draft.rightId; }
        else { payload.segmentId = draft.segmentId; }
        call("vl_propose", payload, "프레임에 맞춘 시각을 계산하는 중입니다…", function (res) {
          // 요청 세대 — 같은 epoch에서 제안이 겹치면 마지막 초안만 살린다(초안
          // 객체 신원으로 잰다). 오래된 응답이 새 초안을 대신 채우지 못하고,
          // 취소된 초안도 여기를 못 지난다(A6 실측 — echoEpoch만으로는 부족했다).
          if (state.draft !== draft || !draft.pending || draft.epoch !== model.epoch) {
            return;
          }
          draft.pending = false;
          draft.proposal = res.ok
            ? { valid: res.valid, reason: res.reason, snapSeconds: res.snapSeconds,
                frame: res.frame, snapPts: res.snapPts, headRevisionId: res.headRevisionId }
            : { valid: false, error: res.error };
          renderPanel();
        });
      }
      function railPointerDown(ev) {
        if (staleWire()) { return; }
        if (!ev || !ev.target || !ev.target.getAttribute) { return; }
        var rangeSide = ev.target.getAttribute("data-vl-range-handle");
        if (rangeSide) { rangeDrag(ev, rangeSide); return; }
        var side = ev.target.getAttribute("data-vl-handle");
        if (!side) { return; }
        var pair = boundaryPair(side);
        if (!pair) { return; }
        if (ev.preventDefault) { ev.preventDefault(); }
        var mount = doc.getElementById("visual-rail");
        var move = function (mv) {
          if (staleWire() || !mv) { return; }
          var sec = secAt(mv.clientX);
          if (sec) {
            state.draft = { epoch: model.epoch, kind: "boundary", side: side, sec: sec,
              leftId: pair.left.id, rightId: pair.right.id, pending: false, proposal: null };
            renderRail();
          }
        };
        var up = function (up_ev) {
          if (mount) {
            if (mount._vlMove) { mount.removeEventListener("pointermove", mount._vlMove); }
            if (mount._vlUp) { mount.removeEventListener("pointerup", mount._vlUp); }
          }
          if (staleWire()) { return; }
          var sec = up_ev && up_ev.clientX !== undefined ? secAt(up_ev.clientX) : null;
          if (!sec && state.draft && state.draft.epoch === model.epoch) { sec = state.draft.sec; }
          if (sec) {
            propose("boundary", { epoch: model.epoch, kind: "boundary", side: side, sec: sec,
              leftId: pair.left.id, rightId: pair.right.id, pending: true, proposal: null });
          }
        };
        if (mount) {
          mount._vlMove = move;
          mount._vlUp = up;
          mount.addEventListener("pointermove", move);
          mount.addEventListener("pointerup", up);
        }
      }
      function railKey(ev) {
        if (staleWire()) { return; }
        if (!ev || !ev.target || !ev.target.getAttribute) { return; }
        var side = ev.target.getAttribute("data-vl-handle");
        if (!side || !railOf()) { return; }
        var frameSec = ratOf(railOf().frameSec);
        if (!frameSec) { return; }
        var pair = boundaryPair(side);
        var card = selectedCard();
        if (!pair || !card) { return; }
        var base = state.draft && state.draft.epoch === model.epoch &&
          state.draft.kind === "boundary"
          ? state.draft.sec
          : (side === "left" ? ratOf(card.start) : ratOf(card.end));
        if (ev.key === "Enter") {
          if (ev.preventDefault) { ev.preventDefault(); }
          propose("boundary", { epoch: model.epoch, kind: "boundary", side: side, sec: base,
            leftId: pair.left.id, rightId: pair.right.id, pending: true, proposal: null });
          return;
        }
        var next = null;
        if (ev.key === "ArrowLeft") { next = VL.sub(base, frameSec); }
        else if (ev.key === "ArrowRight") { next = VL.add(base, frameSec); }
        if (next) {
          if (ev.preventDefault) { ev.preventDefault(); }
          state.draft = { epoch: model.epoch, kind: "boundary", side: side, sec: next,
            leftId: pair.left.id, rightId: pair.right.id, pending: false, proposal: null };
          renderRail();
          renderPanel();
        }
      }
      function railClick(ev) {
        if (staleWire()) { return; }
        if (!ev || !ev.target || !ev.target.getAttribute) { return; }
        if (ev.target.getAttribute("data-vl-range-handle")) { return; }
        var cueIndex = ev.target.getAttribute("data-vl-source-cue");
        if (cueIndex !== null) {
          var cueInfo = curSource();
          var cue = cueInfo && (cueInfo.subtitles || [])[Number(cueIndex)];
          if (cue) {
            state.sourceCue = {sourceId:curSourceId(), id:cue.id}; dropTake();
            state.marker = {sourceId:curSourceId(), label:'원본 자막',
              reason:cue.text, start:cue.start, end:cue.end};
            seekTo(ratOf(cue.start)); renderPanel();
          }
          return;
        }
        var suggestionIndex = ev.target.getAttribute("data-vl-recommendation");
        if (suggestionIndex !== null) {
          var info = curSource();
          var row = info && (info.suggestions || [])[Number(suggestionIndex)];
          if (row) {
            var selectedInterval = intervalAt(info, row);
            if (selectedInterval && findCard(selectedInterval.id)) {
              state.selectedId = selectedInterval.id; state.selectedPlan = model.basePlanId;
            }
            // 제외 확인 단추가 겨냥할 조각 — 이 페이지에 없으면 선택이 바뀌지 않으므로
            // 단추가 옛 선택을 겨냥하지 않도록 조각 ID를 표지에 묶어 둔다.
            state.marker = Object.assign({sourceId:curSourceId(), label:
              tierWord(tierOf(row), row.hold) || recLabel(row.rec),
              segmentId: selectedInterval ? selectedInterval.id : null, tierSpan: true}, row);
            seekTo(ratOf(row.start)); renderPanel();
          }
          return;
        }
        if (ev.target.getAttribute("data-vl-handle")) { return; }
        // 렠 클릭은 **이동 전용**이다 — 칠한 조각을 눌러도 선택이 바뀌지 않고
        // 포인터가 가리키는 시각으로 간다(A5 실측: 조각 시작 선택+이동은 위반).
        // 포인터 시각이 없는 키보드 Enter만 그 조각의 시작으로 이동한다.
        var rail = railOf();
        if (!rail) { return; }
        var sec = null;
        if (ev.clientX !== undefined && ev.clientX !== null && isFinite(ev.clientX)) {
          sec = secAt(ev.clientX);
        }
        if (!sec) {
          var intervalId = ev.target.getAttribute("data-vl-interval");
          var at = intervalId ? railIndex(intervalId) : -1;
          if (at >= 0) { sec = ratOf(rail.intervals[at].start); }
        }
        if (sec) { seekTo(sourceTime(sec)); }
      }
      function selectCard(id, seek) {
        state.keptPlaying = false;
        var card = findCard(id);
        if (!card) { return; }
        state.selectedId = id;
        state.selectedPlan = model.basePlanId;
        if (seek !== false) {
          var info = sourceInfo(card.sourceId);
          var v = videoEl();
          // 원본이 달라지면 먼저 스트림을 갈아끼운다(반복·리스너는 끊는다) —
          // 그 뒤에 이 원본의 잰 원점을 더해 **시작점**으로 이동한다(G3).
          if (v && info && info.src) {
            applySource(info, v);
            var target = VL.add(ratOf(card.start), ratOf(info.origin));
            if (target) { v.currentTime = VL.toFloat(target); }
          }
        }
        renderThumbs();
        renderRail();
        renderPanel();
        syncVideo();
        paintPlayhead();
      }
      function repeatSelected() {
        if (staleWire()) { return; }
        var card = selectedCard();
        var v = videoEl();
        if (!card || !v) { return; }
        var info = curSource();
        if (!info || !info.src) {
          msg("이 원본의 미리보기가 없습니다. 준비가 끝나면 반복할 수 있습니다.");
          return;
        }
        state.loop = { start: ratOf(card.start), end: ratOf(card.end) };
        v.currentTime = VL.toFloat(VL.add(ratOf(card.start), originRat()));
        v.play();
      }

      var prevRootClick = root._vlClick;
      root._vlClick = function (ev) {
        if (staleWire()) { return; }
        if (!ev || !ev.target) { return; }
        // 말풍선 대상에서 **메타데이터를 가진 조상**까지 올라간다 — 카드 안
        // 글자·그림 등 자식 원소를 눌러도 그 단추의 사건으로 잡힌다(A5 실측:
        // 자식이 getAttribute를 가져서 곧바로 멈춰 무시되던 결함).
        var node = ev.target;
        while (node && node !== root) {
          if (node.getAttribute) {
            if (node.getAttribute("data-vl-card") || node.getAttribute("data-vl-act")) {
              break;
            }
          }
          node = node.parentNode;
        }
        if (!node || node === root || !node.getAttribute) { return; }
        state.queueRefocus = node.getAttribute("data-vl-act") === "queue-next";
        var cardId = node.getAttribute("data-vl-card");
        if (cardId) {
          var chosen = findCard(cardId); selectCard(cardId, true);
          if (chosen && model.autoAnalyzeSelection && state.requestedSource !== chosen.sourceId) {
            state.requestedSource = chosen.sourceId;
            call('vl_select_source', {sourceId:chosen.sourceId, selectedId:cardId});
          }
          return;
        }
        var act = node.getAttribute("data-vl-act");
        if (!act) { return; }
        if (act === "unsupported-pick") { say(node.getAttribute("data-vl-note")); return; }
        if (act === 'relink-original') {
          call('vl_relink_original',
            {sourceId:curSourceId(), path:doc.getElementById('vl-original-path').value});
          return;
        }
        if (act === 'sync-recheck') {
          call('vl_sync_recheck', {sourceId:curSourceId()}, '자막 시각을 다시 재는 중(무료)…');
          return;
        }
        if (act === 'relisten') {
          call('vl_relisten', {sourceId:curSourceId()}, '다시 듣기를 준비하는 중(무료)…');
          return;
        }
        if (act === 'refill-pick') { seekTo(ratOf(node.getAttribute('data-vl-at'))); return; }
        if (act === 'snap-listen') {
          listenSpan(ratOf(node.getAttribute('data-vl-from')),
            ratOf(node.getAttribute('data-vl-to')));
          return;
        }
        if (act === 'snap-apply' || act === 'snap-revert') {
          // 누른 순간 적어 둔 신원(키보드로 눌렀으면 지금)을 보낸다 — 한 번 쓰고 지운다.
          var snap = state.snapPress;
          state.snapPress = null;
          if (!snap || snap.act !== act || snap.sourceId !== curSourceId()) {
            snap = snapPress(node);
          }
          if (act === 'snap-apply') {
            call('vl_snap_apply', {sourceId:curSourceId(), sha:snap.sha},
              '시각 맞춤을 적용하는 중(무료)…');
          } else {
            call('vl_snap_revert', {sourceId:curSourceId(), revision:snap.revision},
              '시각 맞춤을 되돌리는 중(무료)…');
          }
          return;
        }
        if (act === 'snap-all-check') {
          call('vl_snap_all_check', {sourceId:curSourceId()},
            '모든 영상의 시각 맞춤 제안을 확인하는 중(무료)…');
          return;
        }
        if (act === 'snap-all-apply') {
          call('vl_snap_all_apply', {sourceId:curSourceId(),
            snapToken:node.getAttribute('data-vl-token') || ''},
            '모든 영상에 시각 맞춤을 적용하는 중(무료)…');
          return;
        }
        if (act === 'refill-approve') {
          // 누른 순간 적어 둔 견적(키보드로 눌렀으면 지금)을 보낸다 — 한 번 쓰고 지운다.
          var press = state.refillPress;
          state.refillPress = null;
          if (!press || press.sourceId !== curSourceId()) { press = refillPress(node); }
          if (!press.quote) { refillReveal(); state.refillReveal = true; }
          call('vl_refill', {sourceId:curSourceId(), digest:press.digest, quote:press.quote},
            '빈칸 재전사 견적을 확인하는 중…');
          return;
        }
        if (act === 'ear-take') { takeEar(node.getAttribute('data-vl-take')); return; }
        if (act === 'source-pick') {
          var info = curSource();
          var picked = info && (info.subtitles || []).find(function(u) {
            return u.id === node.getAttribute('data-vl-cue-id');
          });
          if (picked) {
            state.sourceCue = {sourceId:curSourceId(),id:picked.id}; dropTake();
            state.marker = {sourceId:curSourceId(),label:'원본 자막',
              reason:picked.text,start:picked.start,end:picked.end};
            seekTo(ratOf(picked.start)); renderPanel();
          }
          return;
        }
        if (act === 'truth-mark' || act === 'truth-clear') {
          // 시각 정답(W-25b) — 지금 재생 위치(원본 시각)를 정답으로 찍는다. 자막은 그대로다.
          var truthInfo = curSource();
          var at = playheadLocal();
          if (!truthInfo || !state.sourceCue || (act === 'truth-mark' && !at)) { return; }
          call('vl_timing_truth', {sourceId:curSourceId(),
            transcriptRevision:truthInfo.transcriptRevision, utteranceId:state.sourceCue.id,
            edge:node.getAttribute('data-vl-edge'), action:act.slice(6),
            at:at ? VL.str(at) : ''}, '시각 정답을 저장하는 중…');
          return;
        }
        if (act.indexOf('source-') === 0) {
          var info = curSource();
          if (!info || !state.sourceCue) { return; }
          call('vl_source_caption',
            {sourceId:curSourceId(),
            transcriptRevision:info.transcriptRevision,
            utteranceId:state.sourceCue.id, action:act.slice(7),
            text:doc.getElementById('vl-source-text').value,
            start:doc.getElementById('vl-source-start').value,
            end:doc.getElementById('vl-source-end').value,
            splitAt:VL.str(playheadLocal()), rightText:doc.getElementById('vl-source-right').value},
            '원본 자막을 저장하는 중…'); return;
        }
        if (act.indexOf("filter-") === 0) {
          state.filter = act.slice(7);
          call("vl_refresh", {page: 1, filter: state.filter}); return;
        }
        if (act === "queue-next") { focusQueueItem(nextQueueItem()); return; }
        if (act === "queue-confirm") { confirmAllProposals(); return; }
        var card = selectedCard();
        // 질의·페이징도 화면이 보던 문맥(계획·원본·재생 머리)을 실은 채 나간다 —
        // 재계획이 대기 중일 때 옛 계획의 선택 ID가 새 계획에서 다른 구간에
        // 재주조돼 있어도 서버가 같은 core의 조각으로 되찾을 재료다(실측).
        if (act === "refresh") { call("vl_refresh", { page: model.page,
          selectedId: selectedId(), basePlanId: model.basePlanId,
          sourceId: curSourceId(), playheadSec: VL.str(playheadLocal()) }); return; }
        if (act === "prepare") {
          var trackEl = doc.getElementById("vl-track");
          var pick = trackEl ? trackEl.value : "";
          var sel = {};
          var sid = curSourceId();
          // 고른 값만 실는다 — 트랙 칸이 없으면(첫 준비 전) 서명 None을 보내
          // 침묵을 명령하는 게 되므로 키를 아예 뺀다(서버 기본 = 첫 트랙).
          if (sid && pick !== "" && pick !== null && pick !== undefined) {
            sel[sid] = pick === "silence" ? null : parseInt(pick, 10);
          }
          // 고른 영상 하나를 준비한다 — 쪽과 무관하게 그 영상의 컷 전부(D64).
          call("vl_prepare", { page: model.page, selectedId: selectedId(), sourceId: sid,
            audioSelections: sel }, "이 영상의 미리보기를 준비합니다. 없는 것만 만듭니다…");
          return;
        }
        if (act === "red-confirm" || act === "red-revert") {
          // 이 조각만 사람이 확인한다 — 선택 편집이 미검토 표지를 걷는다(D35). 결정 상자가
          // 겨냥한 컷(지금 선택)이 아니면 — 빨강 막대가 다른 페이지 조각을 짚었으면 —
          // 아무것도 보내지 않는다. 막대·카드·컷 어디로 왔든 같은 상자·같은 호출이다.
          var decided = decisionTarget(card);
          if (!decided) { return; }
          call("vl_apply", {
            op: "selection", segmentId: decided,
            selection: act === "red-confirm" ? "excluded" : "included",
            revisionId: model.revisionId, basePlanId: model.basePlanId,
            page: model.page, sourceId: curSourceId(),
            selectedId: card.id, playheadSec: VL.str(playheadLocal()),
          });
          return;
        }
        if (act === "toggle-sel" && card) {
          call("vl_apply", {
            op: "selection", segmentId: card.id,
            selection: card.sel === "excluded" ? "included" : "excluded",
            revisionId: model.revisionId, basePlanId: model.basePlanId,
            page: model.page, sourceId: curSourceId(),
            selectedId: card.id, playheadSec: VL.str(playheadLocal()),
          });
          return;
        }
        if (act === "split" && card) {
          var local = playheadLocal();
          if (!local) { msg("재생기를 찾지 못했습니다. 미리보기가 준비된 뒤 다시 시도하세요.");
            return; }
          propose("split", { epoch: model.epoch, kind: "split", sec: local,
            segmentId: card.id, pending: true, proposal: null });
          return;
        }
        if (act === "merge" && card && card.neighbor) {
          call("vl_apply", {
            op: "merge", firstId: card.id, secondId: card.neighbor,
            revisionId: model.revisionId, basePlanId: model.basePlanId,
            page: model.page, sourceId: curSourceId(),
            selectedId: card.id, playheadSec: VL.str(playheadLocal()),
          });
          return;
        }
        if (act === "save-note" && card) {
          var box = doc.getElementById("vl-note-input");
          call("vl_apply", {
            op: "note", segmentId: card.id, note: box ? box.value : "",
            revisionId: model.revisionId, basePlanId: model.basePlanId,
            page: model.page, sourceId: curSourceId(),
            selectedId: card.id, playheadSec: VL.str(playheadLocal()),
          }, "메모를 저장하는 중…");
          return;
        }
        if (act === "replan") {
          call("vl_replan", { revisionId: model.revisionId, basePlanId: model.basePlanId,
            page: model.page, sourceId: curSourceId(),
            selectedId: selectedId(), playheadSec: VL.str(playheadLocal()) },
            "편집을 새 계획으로 옮기고 추출을 시작합니다…");
          return;
        }
        if (act === "apply-draft" && state.draft && state.draft.epoch === model.epoch &&
          state.draft.proposal && state.draft.proposal.valid) {
          var d = state.draft;
          var payload = { revisionId: d.proposal.headRevisionId,
            basePlanId: model.basePlanId, page: model.page,
            sourceId: curSourceId(),
            selectedId: selectedId(), playheadSec: VL.str(playheadLocal()) };
          if (d.kind === "boundary") {
            payload.op = "boundary";
            payload.leftId = d.leftId;
            payload.rightId = d.rightId;
            payload.boundaryPts = d.proposal.snapPts;
          } else {
            payload.op = "split";
            payload.segmentId = d.segmentId;
            payload.splitPts = d.proposal.snapPts;
          }
          state.draft = null;
          call("vl_apply", payload, "저장하는 중…");
          return;
        }
        if (act === "cancel-draft") { state.draft = null; renderPanel(); renderRail(); return; }
      };
      // 배선마다 듣는이를 갈아끼운다 — 첫 배선의 클로저가 재렌더 뒤에도 살아
      // 옛 모델로 사건을 처리하는 일이 없다(듣는이 재사용 금지, A5 실측).
      if (prevRootClick) { root.removeEventListener("click", prevRootClick); }
      root.addEventListener("click", root._vlClick);
      // 누르는 순간(pointerdown) 빈칸 재전사 승인 단추면 그때 보이던 견적을 적어 둔다(D54) —
      // 다른 곳을 누르면 지운다(남은 누름이 나중 키보드 승인에 묻어가지 않는다).
      if (root._vlDown) { root.removeEventListener("pointerdown", root._vlDown); }
      root._vlDown = function (ev) {
        if (staleWire() || !ev || !ev.target) { return; }
        var node = ev.target;
        while (node && node !== root && !(node.getAttribute && node.getAttribute("data-vl-act"))) {
          node = node.parentNode;
        }
        var pressed = node && node !== root && node.getAttribute ?
          node.getAttribute("data-vl-act") : "";
        state.refillPress = pressed === "refill-approve" ? refillPress(node) : null;
        state.snapPress = pressed === "snap-apply" || pressed === "snap-revert" ?
          snapPress(node) : null;
      };
      root.addEventListener("pointerdown", root._vlDown);

      var railMount = doc.getElementById("visual-rail");
      if (railMount) {
        if (railMount._vlRailClick) { railMount.removeEventListener("click",
          railMount._vlRailClick); }
        if (railMount._vlRailDown) { railMount.removeEventListener("pointerdown",
          railMount._vlRailDown); }
        if (railMount._vlRailKey) { railMount.removeEventListener("keydown",
          railMount._vlRailKey); }
        railMount._vlRailClick = railClick;
        railMount._vlRailDown = railPointerDown;
        railMount._vlRailKey = railKey;
        railMount.addEventListener("click", railClick);
        railMount.addEventListener("pointerdown", railPointerDown);
        railMount.addEventListener("keydown", railKey);
      }
      var repeatBtn = doc.getElementById("vl-repeat");
      if (repeatBtn) {
        if (repeatBtn._vlRepeat) { repeatBtn.removeEventListener("click", repeatBtn._vlRepeat); }
        repeatBtn._vlRepeat = repeatSelected;
        repeatBtn.addEventListener("click", repeatSelected);
      }
      var pauseBtn = doc.getElementById("vl-pause");
      if (pauseBtn) {
        if (pauseBtn._vlPause) { pauseBtn.removeEventListener("click", pauseBtn._vlPause); }
        pauseBtn._vlPause = function () {
          if (staleWire()) { return; }
          stopLoop(); state.keptPlaying = false;
          var v = videoEl();
          if (v) { v.pause(); }
        };
        pauseBtn.addEventListener("click", pauseBtn._vlPause);
      }

      function bindControl(id, action) {
        var el = doc.getElementById(id);
        if (!el) { return; }
        if (el._vlControl) { el.removeEventListener("click", el._vlControl); }
        el._vlControl = function() { if (!staleWire()) { action(el); } };
        el.addEventListener("click", el._vlControl);
      }
      bindControl('vl-current-caption', function() {
        var info = curSource(), second = VL.toFloat(playheadLocal());
        var cue = info && (info.subtitles || []).find(function(c) {
          return VL.toFloat(ratOf(c.start)) <= second && second < VL.toFloat(ratOf(c.end));
        });
        if (cue) {
          var v = videoEl(); if (v) { v.pause(); }
          state.sourceCue = {sourceId:curSourceId(),id:cue.id}; dropTake();
          setAdvanced(true);  // 자막 편집기는 「고급 도구」 패널에 산다
          renderPanel();
        }
      });
      bindControl('vl-source-caption-toggle', function(el) {
        state.sourceCaptionEnabled = state.sourceCaptionEnabled === false;
        el.setAttribute('aria-pressed',state.sourceCaptionEnabled ? 'true' : 'false');
        paintSourceCaptions();
      });
      bindControl("vl-timeline-mode", function(el) {
        state.timelineMode = state.timelineMode === "result" ? "original" : "result";
        el.textContent = state.timelineMode === "result" ? "정리 결과 타임라인" : "원본 타임라인";
        state.draft = null; renderRail(); paintPlayhead();
      });
      bindControl("vl-kept-play", function() {
        state.loop = null; state.playingSource = curSourceId(); state.keptPlaying = true;
        var info = curSource();
        var first = info && info.rail && info.rail.intervals.find(function(i) {
          return i.sel !== "excluded";
        });
        if (first) { seekTo(ratOf(first.start)); var v = videoEl(); if (v) { v.play(); } }
        else { msg("이 원본에서 사용할 컷이 없습니다."); }
      });
      function zoom(amount) {
        state.zoom = Math.max(1, Math.min(8, (state.zoom || 1) + amount));
        var el = doc.getElementById("visual-rail");
        if (el) {
          el.style.width = (state.zoom * 100) + "%";
          el.parentElement.style.overflowX = "auto";
        }
      }
      bindControl("vl-zoom-in", function() { zoom(1); });
      bindControl("vl-zoom-out", function() { zoom(-1); });
      bindControl("vl-mark-in", function() { setRangePoint("in", playheadLocal()); });
      bindControl("vl-mark-out", function() { setRangePoint("out", playheadLocal()); });
      VL_RANGE_MARKS.forEach(function (mark) {
        bindControl("vl-mark-" + mark, function() { sendMark(mark); });
      });
      bindControl("vl-range-undo", sendUndo);
      bindControl("vl-advanced-toggle", function() { setAdvanced(!advancedOpen()); });
      setAdvanced(advancedOpen());
      var rangeList = doc.getElementById("vl-range-list");
      if (rangeList) {
        if (rangeList._vlSeek) { rangeList.removeEventListener("click", rangeList._vlSeek); }
        rangeList._vlSeek = function (ev) {
          if (staleWire()) { return; }
          var node = ev && ev.target;
          while (node && node !== rangeList && !(node.getAttribute &&
            node.getAttribute("data-vl-range-seek") !== null)) { node = node.parentNode; }
          var at = node && node !== rangeList ?
            ratOf(node.getAttribute("data-vl-range-seek")) : null;
          if (at) { seekTo(at); }
        };
        rangeList.addEventListener("click", rangeList._vlSeek);
      }
      keepFocusCard();
      var focusAt = applyQueueFocus();
      if (state.marker && state.selectedPlan !== model.basePlanId) {
        var markerSource = sourceInfo(state.marker.sourceId);
        var matching = intervalAt(markerSource, state.marker);
        if (matching && findCard(matching.id)) {
          state.selectedId = matching.id; state.selectedPlan = model.basePlanId;
        }
      }
      syncVideo();
      if (focusAt) { seekTo(focusAt); }
      renderThumbs();
      renderRail();
      renderPanel();
      paintPlayhead();
      if (state.lastMsg) { msg(state.lastMsg); }
      else { liveFootnote(); }
      // 보던 영상이 계획이 덮지 못한 영상이면(D68) 「준비 중」 대신 까닭을 말한다.
      if (model.viewUnsupported) { say(model.viewUnsupported); }
    };
  }
  // 커밋 뒤 재배선 — 설치된 그라디오(6.29) 실측 세 가지를 다 다룬다:
  // (1) watch 콜백은 **인자 없이** 불린다(callback()). (2) props.value 직접
  // 대입은 렌더 마이크로태스크만 돌고 watch를 아예 안 부른다 — 세터가 안팎
  // 거울을 함께 갈아 동률 효과가 물러난다. (3) 렌더 마이크로태스크는 같은 태그의
  // 뿌리를 그 자리에서 morph한다(원소 신원 유지·속성·동적 자식만 갈아침) —
  // 통째 교체와 달리 원소 신원으로는 새 모델을 알 수 없으니 **커밋된 모델
  // 문자열**(세대)로 다시 묶을지를 정한다. 재배선의 주된 길은 부팅 때부터
  // 상시인 관찰자와 대입 직후 스케줄러이고 watch는 보조 신호다. 표식을 먼저
  // 찍으니 배선이 하위 원소를 고쳐 관찰자가 되울 때도 정확히 한 번만 묶는다 —
  // 같은 모델 재배선·관찰자 루프가 없다(A7 실측: watch가 옛 뿌리를 다시 묶는
  // 사이 교체가 새 화면을 빈 채 0카드로 남기던 결함).
  var teardownEmpty = function () {
    // 자리표시 커밋(작업공간 없음·계획 없음 — data-vl-empty, 모델 없음)도 그 자리
    // morph로 온다(A9 실측). 묶은 화면이 있으면 **정리 세대**를 밟는다: 배선
    // 세대를 올려 붙잡아 둔 옛 클로저·제안을 죽이고(늦은 응답은 문서의 epoch가
    // 사라져 어차피 버려진다), 상태·문·목록을 비우고, 재생 중이던 영상을 능동
    // 언로드한다(정지·청취 해제·주소 제거).
    W.__vlVisualWireSerial = (W.__vlVisualWireSerial || 0) + 1;
    var st = W.__vlVisualWorkbenchState;
    if (st) {
      st.wire = null; st.epoch = null; st.model = null;
      st.draft = null; st.loop = null; st.selectedId = null;
      st.selectedPlan = null; st.lastMsg = ""; st.focusCard = null; st.queueFocus = null;
      st.liveFoot = "";
    }
    var d = W.__vlVisualDoc || document;
    var note = d.getElementById("visual-note");
    if (note) { note.textContent = ""; }
    var foot = d.getElementById("vl-footnote");
    if (foot) { foot.textContent = ""; }
    var thumbs = d.getElementById("vl-thumbs");
    if (thumbs) { thumbs.innerHTML = ""; }
    var v = W.__vlVisualVideoEl;
    if (v) {
      if (v._vlTick) { v.removeEventListener("timeupdate", v._vlTick); }
      if (v._vlLoopOff) { v.removeEventListener("pause", v._vlLoopOff); }
      if (v._vlEnded) { v.removeEventListener("ended", v._vlEnded); }
      ['seeking','seeked','pause','ratechange','loadedmetadata','emptied'].forEach(function(event) {
        if (v._vlCaptionSync) { v.removeEventListener(event,v._vlCaptionSync); }
      });
      if (v._vlSourceTrack) {
        v._vlSourceTrack.mode = 'hidden';
        while (v._vlSourceTrack.cues && v._vlSourceTrack.cues.length) {
          v._vlSourceTrack.removeCue(v._vlSourceTrack.cues[0]);
        }
        v._vlSourceTrack.mode = 'disabled'; v._vlSourceTrackKey = null;
      }
      var caption = d.getElementById('visual-source-caption');
      if (caption) { caption.textContent = ''; caption.hidden = true; }
      try {
        v.pause();
        v.removeAttribute("src");
        v.load();
        v.setAttribute("data-vl-src", "");
      } catch (err) {
        // 언로드 최선 — 정지가 본질이다
      }
    }
    W.__vlVisualVideoEl = null;
  };
  // 머리줄 흐름 단계·프로젝트 이름 — 작업대 값이 실어 온 것을 옮긴다(자리표시 값도 싣는다).
  // 단계 조각은 서버가 이스케이프해 지은 HTML이고, 이름은 textContent로만 넣는다.
  var paintFlow = function (fresh) {
    var d = W.__vlVisualDoc || document;
    var steps = fresh.getAttribute("data-vl-flow");
    var nav = d.getElementById("vl-flow");
    if (steps !== null && nav && nav.__vlFlow !== steps) {
      nav.innerHTML = steps; nav.__vlFlow = steps;
    }
    var project = fresh.getAttribute("data-vl-project");
    var name = d.getElementById("vl-project-name");
    if (project !== null && name && name.textContent !== project) { name.textContent = project; }
    // 「라이브러리 닫기」 — 열린 작업공간이 있을 때만 보인다(닫힘 표지가 없을 때).
    var closer = d.getElementById("vl-library-close");
    if (steps !== null && closer) {
      var shut = fresh.hasAttribute("data-vl-closed");
      if (closer.hidden !== shut) { closer.hidden = shut; }
    }
    // 「기준 바꿈」 — 추천 기준이 기본값과 다른 작업공간에서만 보인다(W-46a).
    var badge = d.getElementById("vl-criteria-badge");
    if (steps !== null && badge) {
      var changed = fresh.getAttribute("data-vl-criteria") === "changed";
      if (badge.hidden === changed) { badge.hidden = !changed; }
    }
  };
  var wireCommitted = function () {
    var d = W.__vlVisualDoc || document;
    var fresh = d.getElementById("visual-workbench");
    if (!fresh || !fresh.getAttribute) { return; }
    paintFlow(fresh);
    var raw = W.__vlLib.modelRaw(fresh);
    if (!raw) {
      // 반복 통보는 무동작(같은 빈 세대 표식) — 관찰자 루프 없음.
      if (fresh.__vlVisualWiredModel === "__vlEmpty__") { return; }
      fresh.__vlVisualWiredModel = "__vlEmpty__";
      teardownEmpty();
      return;
    }
    if (fresh.__vlVisualWiredModel === raw) { return; }
    // 먼저 찍고 묶는다 — 배선 중 관찰자가 다시 불려도 같은 모델엔 재배선 0.
    fresh.__vlVisualWiredModel = raw;
    W.__vlWireVisual(fresh, d);
  };
  var rewireScheduled = function () {
    wireCommitted();
    Promise.resolve().then(wireCommitted);
  };
  W.__vlVisualBlockEl = element;
  W.__vlVisualDoc = document;
  // 공용 call()이 찾는 이름은 state_key 집합(__vlVisualWorkbench*)이다 — 같은
  // 이름으로 단다(A8 실측: 짝이 안 맞으면 대입 직후 스케줄러가 조용히 no-op).
  W.__vlVisualWorkbenchRewire = rewireScheduled;
  var Observer = W.MutationObserver ||
    (typeof MutationObserver !== "undefined" ? MutationObserver : null);
  if (Observer && element && !element.__vlVisualObserver) {
    // 그라디오는 포장 안쪽에서 morph하므로 subtree로 보고, 모델 칸의 글자(characterData —
    // morph는 같은 칸의 글자 마디만 고친다)와 옛 모델 속성도 본다 — 화면이 같아 보이는
    // 새로고침(epoch만 바뀐 모델)은 자식 변화가 없을 수 있다. 배선은 하위 원소만 고치고
    // 모델은 건드리지 않는다 — 루프 없음(같은 모델이면 wireCommitted가 무동작).
    element.__vlVisualObserver = new Observer(function () { wireCommitted(); });
    element.__vlVisualObserver.observe(element, { childList: true, subtree: true,
      characterData: true, attributes: true, attributeFilter: ["data-vl-model"] });
  }
  if (!W.__vlStageFitBound && typeof W.addEventListener === "function") {
    W.__vlStageFitBound = true;
    // 머리글·아코디언 높이는 CSS가 모른다 — 무대의 문서 상단을 재서 CSS 변수로 넘긴다.
    W.__vlFitStage = function () {
      var stage = document.getElementById("visual-stage");
      // 숨은 탭에서 재면 위쪽이 0이라 한 프레임 동안 무대가 화면 전체로 번쩍인다.
      if (!stage || !stage.getBoundingClientRect || stage.offsetParent === null) { return; }
      var next = Math.round(stage.getBoundingClientRect().top + (W.scrollY || 0)) + "px";
      if (stage.style.getPropertyValue("--vl-stage-top") !== next) {
        stage.style.setProperty("--vl-stage-top", next);
      }
    };
    W.addEventListener("resize", W.__vlFitStage);
    var stageEl = document.getElementById("visual-stage");
    if (W.ResizeObserver && stageEl && stageEl.parentElement) {
      new W.ResizeObserver(function () { W.__vlFitStage(); }).observe(stageEl.parentElement);
    }
    W.__vlFitStage();
  }
  // 전역 단축키(D50) — 문서에 한 번만 묶고 DOM은 사건 순간에 찾는다(작업대가
  // 다시 그려져도 옛 원소를 붙잡지 않는다). 렠 손잡이의 railKey가 먼저 처리해
  // preventDefault한 키는 건드리지 않는다.
  if (!W.__vlGlobalKeysBound && typeof document.addEventListener === "function") {
    W.__vlGlobalKeysBound = true;
    W.__vlShortcutVideo = function () {
      for (var i = 0; i < VL_SHORTCUT_VIDEOS.length; i += 1) {
        var v = document.getElementById(VL_SHORTCUT_VIDEOS[i][0]);
        if (v && v.offsetParent !== null) { return { video: v, noteId: VL_SHORTCUT_VIDEOS[i][1] }; }
      }
      return null;
    };
    W.__vlShortcutBlocked = function (ev) {
      if (!ev || ev.isComposing || ev.keyCode === VL_IME_PROCESS_KEYCODE || ev.repeat) {
        return true;
      }
      if (ev.defaultPrevented || ev.metaKey || ev.ctrlKey || ev.altKey || ev.shiftKey) {
        return true;
      }
      // 서랍이 열려 있으면 막 뒤의 영상을 움직이지 않는다 — 초점이 막 뒤(승인 단추 등)에
      // 남아 있어도 마찬가지다.
      if (typeof document.querySelector === "function" && document.querySelector(VL_OPEN_DRAWER)) {
        return true;
      }
      var t = ev.target;
      return !!(t && (t.isContentEditable || (typeof t.closest === "function" &&
        (t.closest(VL_EDITABLE) || t.closest(VL_OPEN_DRAWER)))));
    };
    W.__vlShortcutAction = function (ev) {
      if (W.__vlShortcutBlocked(ev)) { return null; }
      var code = ev.key === " " ? "Space" : ev.code;
      return Object.prototype.hasOwnProperty.call(VL_SHORTCUT_ACTIONS, code)
        ? VL_SHORTCUT_ACTIONS[code] : null;
    };
    // 재생 거부(자동 재생 정책·소스 없음)는 삼키지 않고 그 화면의 각주에 드러낸다.
    W.__vlTogglePlay = function (target) {
      var v = target.video;
      if (!v.paused && !v.ended) { v.pause(); return; }
      var started = v.play();
      if (started && typeof started.then === "function") {
        started.then(null, function (err) {
          var note = document.getElementById(target.noteId);
          if (note) { note.textContent = VL_PLAY_FAILED_TEXT + (err && err.name ? err.name : err); }
        });
      }
    };
    W.__vlStepFrame = function (v, direction) {
      var frame = v._vlFrameSec > 0 ? v._vlFrameSec : VL_FALLBACK_FRAME_SECONDS;
      v.pause();
      W.__vlSeekBy(v, direction * frame);
    };
    W.__vlSeekBy = function (v, delta) {
      var end = v.duration > 0 && isFinite(v.duration) ? v.duration : Infinity;
      v.currentTime = Math.min(end, Math.max(0, (v.currentTime || 0) + delta));
    };
    W.__vlShortcutRun = {
      toggle: function (target) { W.__vlTogglePlay(target); },
      back: function (target) { W.__vlSeekBy(target.video, -VL_SEEK_SECONDS); },
      forward: function (target) { W.__vlSeekBy(target.video, VL_SEEK_SECONDS); },
      frameBack: function (target) { W.__vlStepFrame(target.video, -1); },
      frameForward: function (target) { W.__vlStepFrame(target.video, 1); }
    };
    // 시작·끝 표시 키 — 보이는 영상이 작업대 재생기일 때만, 지금 배선(`state.rangeKey`)에 넘긴다.
    // Esc는 떠 있는 단축키 판이 먼저다(머리줄 실행본이 닫는다). 처리한 키만 기본 동작을 막는다.
    W.__vlRangeAction = function (ev) {
      if (W.__vlShortcutBlocked(ev)) { return null; }
      if (!Object.prototype.hasOwnProperty.call(VL_RANGE_KEYS, ev.code)) { return null; }
      var sheet = document.getElementById("vl-keysheet");
      if (ev.code === "Escape" && sheet && !sheet.hidden) { return null; }
      var target = W.__vlShortcutVideo();
      return target && target.video.id === "visual-video" ? VL_RANGE_KEYS[ev.code] : null;
    };
    W.__vlShortcutKey = function (ev) {
      var action = W.__vlShortcutAction(ev);
      var target = action ? W.__vlShortcutVideo() : null;
      if (!target) {
        var range = action ? null : W.__vlRangeAction(ev);
        var st = W.__vlVisualWorkbenchState;
        if (range && st && typeof st.rangeKey === "function" && st.rangeKey(range)) {
          ev.preventDefault();
        }
        return;
      }
      ev.preventDefault();
      W.__vlShortcutRun[action](target);
    };
    // 초점 단추는 스페이스 keyup에서 눌린다 — 영상 토글이 이기도록 그 기본 동작을 막는다(Q1).
    W.__vlShortcutKeyUp = function (ev) {
      var space = ev && (ev.code === "Space" || ev.key === " ");
      if (space && W.__vlShortcutAction(ev) && W.__vlShortcutVideo()) { ev.preventDefault(); }
    };
    document.addEventListener("keydown", W.__vlShortcutKey);
    document.addEventListener("keyup", W.__vlShortcutKeyUp);
  }
  if (!W.__vlVisualWatchBound) {
    W.__vlVisualWatchBound = true;
    watch("value", rewireScheduled);
  }
  W.__vlVisualWorkbenchProps = props;
  W.__vlVisualWorkbenchServer = server;
  var firstRoot = document.getElementById("visual-workbench");
  if (firstRoot && firstRoot.getAttribute) { paintFlow(firstRoot); }
  W.__vlWireVisual(firstRoot, document);
})();
"""
)


CAPTION_BOOTSTRAP_JS = (
    _CLIENT_LIB_BODY
    + r"""
(function () {
  var W = window;
  if (!W.__vlWireCaption) {
    W.__vlWireCaption = function (root, doc) {
"""
    + _workbench_state("caption-workbench", "CaptionWorkbench")
    + r"""
      var VL = W.__vlLib;
      if (!root || !root.getAttribute) { return; }
      var rawModel = VL.modelRaw(root);
      if (!rawModel) { return; }
      var model;
      try { model = JSON.parse(rawModel); } catch (err) { return; }
      // 같은 화면 생명의 **낡은 원본 출력** 방어 — 등록·실행·검토 같은 옛 원본
      // 처리의 지연 도착이, 이미 받아들인 검토 머리보다 **낮은 머리**의 HTML로
      // 화면을 되돌리던 길(ROOT 실측: 저장·확정 뒤 머리2 화면이 머리1로 굴러가
      // 편집 칸·행·오버레이만 옛 글자를 보였다 — A10 확증 10케이스). 토큰이 같고
      // 머리가 낮으면 못 믿는다 — **새 배선 세대를 만들지 않고** 지금 커밋된
      // 모델로 화면만 다시 그린다: 대기 중인 진짜 요청의 신선도(배선 세대·epoch)
      // 까지 무효화하면 옛 출력이 이긴 꼴이 된다(A10 실측: 대기 저장이 맥빗줬다).
      // 같은 머리 재렌더·새 토큰 전환·진짜 오류 안내는 그대로다. 머리 숫자는
      // **같은 실행(run) 안에서만** 비교한다 — 같은 작업공간의 새 실행은 머리가
      // 낮게 다시 시작해도 의도한 현재 화면이니 열어 준다.
      if (state.model && model.token === state.model.token &&
          model.runId === state.model.runId &&
          typeof model.head === "number" && typeof state.model.head === "number" &&
          model.head < state.model.head) {
        var keepRaw = JSON.stringify(state.model);
        var keepHolder = VL.modelHolder(root);
        try {
          if (keepHolder) { keepHolder.textContent = keepRaw; }
          else { root.setAttribute("data-vl-model", keepRaw); }
        } catch (setErr) {
          // 읽기 전용 뿌리 — 표식만으로도 다음 배선이 지금 모델을 본다.
        }
        root.__vlCaptionWiredModel = keepRaw;
        if (typeof root.__vlCaptionRepaint === "function") {
          root.__vlCaptionRepaint();
        }
        return;
      }
      // 배선 세대 표식 — 영상 작업대와 같은 재료: 같은 태그 뿌리의 그 자리
      // morph(원소 유지·모델만 교체, A8 실측)까지 잡는다(아래 wireCommitted).
      root.__vlCaptionWiredModel = rawModel;
      state.model = model;
      state.epoch = model.epoch;
      // 배선 세대 — 옛 클로저 사건은 새 배선 앞에서 공용 상태·문서·서버를
      // 건드리기 전에 물러난다(A6 실측: 옛 A1 자막 클릭이 새 A2 편집 칸·재생기를
      // 바꾸던 결함). 화면 수명 토큰은 작업공간을 다시 열 때만 바뀐다 — 보통의
      // 저장 재렌더(epoch만 바뀜)는 같은 화면이므로 선택·재생이 그대로 이어진다.
      var wireSerial = (W.__vlCaptionWireSerial = (W.__vlCaptionWireSerial || 0) + 1);
      state.wire = wireSerial;
      var screenFresh = state.token !== model.token;
      state.token = model.token;
      if (screenFresh) {
        state.selectedId = null;
        state.downloads = null; // 새 화면 생명 — 옛 확정 머리의 링크는 못 남는다
      }
      function staleWire() {
        return state.wire !== wireSerial || state.epoch !== model.epoch ||
          doc.getElementById("caption-workbench") !== root;
      }

      function msg(text) {
        var el = doc.getElementById("cap-msg");
        if (el) { el.textContent = text || ""; }
        state.lastMsg = text || "";
      }
      function paintDownloads() {
        // 상태에 남아 있는 이 화면의 내려박기 링크를 안전한 <a>로 다시 그린다.
        // 커밋 morph가 각주의 동적 자식을 지우고 재배선이 안내문을 복원하므로,
        // 링크도 **커밋 뒤 재배선에서** 다시 붙는다(ROOT 실측: 옛 DOM에만 붙은
        // 링크는 실제 props 커밋 뒤 사라졌다).
        var box = doc.getElementById("cap-msg");
        if (!box || !box.appendChild) { return; }
        var items = state.downloads || [];
        for (var i = 0; i < items.length; i += 1) {
          var anchor = doc.createElement("a");
          anchor.href = String(items[i].href);
          anchor.setAttribute("download", "");
          anchor.textContent = String(items[i].label || items[i].href);
          box.appendChild(doc.createTextNode(" "));
          box.appendChild(anchor);
        }
      }
      function appendDownloads(items) {
        // 서버가 등록 미디어 ID(`/media/<id>`)로만 준 링크를 검증해 상태에 둔다 —
        // 사용자 글자·임의 경로가 HTML이 되는 길이 없다(글자는 textContent만).
        // 새 요청 시작(call)과 화면 전환(토큰·자리표시 정리)이 상태를 비운다.
        var kept = [];
        for (var i = 0; i < items.length; i += 1) {
          var href = String((items[i] && items[i].href) || "");
          if (href.indexOf("/media/") !== 0) { continue; }
          kept.push({ href: href, label: String((items[i] && items[i].label) || href) });
        }
        state.downloads = kept;
        paintDownloads();
      }
      function esc(value) {
        return String(value === null || value === undefined ? "" : value)
          .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
          .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
      }
      function videoEl() { return doc.getElementById("caption-video"); }

      function renderCues() {
        var mount = doc.getElementById("cap-cues");
        if (!mount) { return; }
        var rows = [];
        for (var i = 0; i < (model.cues || []).length; i += 1) {
          var cue = model.cues[i];
          rows.push(
            '<button type="button" class="cap-row' + (cue.basis === "manual" ?
              " cap-row-manual" : "") +
            (cue.id === state.selectedId ? " cap-row-sel" : "") +
            '" data-vl-cue="' + esc(cue.id) + '" role="option"' +
            ' aria-selected="' + (cue.id === state.selectedId ? "true" : "false") + '"' +
            ' data-seek-start="' + esc(cue.seek) + '" data-seek-end="' + esc(cue.endSeek) + '"' +
            ' data-text="' + esc(cue.text) + '" data-start-ms="' + esc(cue.startMs) + '"' +
            ' data-end-ms="' + esc(cue.endMs) + '">' +
            '<span class="cap-row-time">' + esc(cue.startMs) + "→" + esc(cue.endMs) + "</span>" +
            '<span class="cap-row-text">' + esc(cue.text || "(빈 큐)") + "</span></button>"
          );
        }
        mount.innerHTML = rows.join("") ||
          '<p class="vl-note">큐가 없다 — 실행을 끝내고 새로고침하면 여기 나타난다.</p>';
      }

      function fillEditor(cue) {
        var idEl = doc.getElementById("cap-cue-id");
        var textEl = doc.getElementById("cap-text");
        var startEl = doc.getElementById("cap-start");
        var endEl = doc.getElementById("cap-end");
        if (idEl) { idEl.value = cue.id; }
        if (textEl) { textEl.value = cue.text || ""; }
        if (startEl) { startEl.value = cue.startMs; }
        if (endEl) { endEl.value = cue.endMs; }
      }

      function pickCue(id) {
        for (var i = 0; i < (model.cues || []).length; i += 1) {
          if (model.cues[i].id === id) { return model.cues[i]; }
        }
        return null;
      }

      function overlayFor(t) {
        var cues = model.cues || [];
        for (var i = 0; i < cues.length; i += 1) {
          var s = parseFloat(cues[i].seek);
          var e = parseFloat(cues[i].endSeek);
          if (isFinite(s) && isFinite(e) && s <= t && t < e) { return cues[i]; }
        }
        return null;
      }
      function tick() {
        var v = videoEl();
        var overlay = doc.getElementById("caption-overlay");
        if (!v || !overlay) { return; }
        var cue = overlayFor(v.currentTime);
        overlay.textContent = cue ? (cue.text || "") : "";
      }
      var v0 = videoEl();
      var vPrev = W.__vlCaptionVideoEl;
      if (vPrev && vPrev !== v0) {
        // 재생기 껍데기가 다시 그려져 옛 자막 영상이 문서에서 떨어졌다(작업공간
        // 전환·새 원본 등록) — 능동 언로드한다: 정지·주소 제거·청취 해제·옛 반복
        // 해제에 이어 오버레이까지 지운다. 보통의 저장 재렌더는 껍데기가 그대로라
        // (vPrev === v0) 이 길을 쓰지 않는다 — 재생 머리가 살아 있다(A6 실측).
        if (vPrev._vlCapTick) { vPrev.removeEventListener("timeupdate", vPrev._vlCapTick); }
        if (vPrev._vlLoop) {
          try { vPrev._vlLoop(); } catch (err) { /* 최선 언로드 — 정지가 본질이다 */ }
          vPrev._vlLoop = null;
        }
        try {
          vPrev.pause();
          vPrev.removeAttribute("src");
          vPrev.load();
        } catch (err) {
          // 언로드 최선 — 재생 정지가 본질이다
        }
        var staleOverlay = doc.getElementById("caption-overlay");
        if (staleOverlay) { staleOverlay.textContent = ""; }
      }
      if (v0) {
        W.__vlCaptionVideoEl = v0;
        if (v0._vlCapTick) { v0.removeEventListener("timeupdate", v0._vlCapTick); }
        v0._vlCapTick = tick;
        v0.addEventListener("timeupdate", tick);
      }
      var toggle = doc.getElementById("cap-overlay-toggle");
      if (toggle) {
        if (toggle._vlToggle) { toggle.removeEventListener("click", toggle._vlToggle); }
        toggle._vlToggle = function () {
          if (staleWire()) { return; }
          var overlay = doc.getElementById("caption-overlay");
          var on = toggle.getAttribute("aria-pressed") === "true";
          toggle.setAttribute("aria-pressed", on ? "false" : "true");
          if (overlay) {
            if (on) { overlay.classList.remove("cap-overlay-on"); }
            else { overlay.classList.add("cap-overlay-on"); }
          }
        };
        toggle.addEventListener("click", toggle._vlToggle);
      }

      var prevCapClick = root._vlCapClick;
      root._vlCapClick = function (ev) {
        if (staleWire()) { return; }
        if (!ev || !ev.target) { return; }
        // 자식(글자·시각 칸)을 눌러도 그 행 단추의 사건으로 — 메타데이터 가진
        // 조상까지 올라간다(A5 실측: 자식 클릭이 무시되던 결함).
        var node = ev.target;
        while (node && node !== root) {
          if (node.getAttribute) {
            var nid = node.getAttribute("id");
            if (node.getAttribute("data-vl-cue") || nid === "cap-save" ||
              nid === "cap-save-time" || nid === "cap-confirm") {
              break;
            }
          }
          node = node.parentNode;
        }
        if (!node || node === root || !node.getAttribute) { return; }
        var cueId = node.getAttribute("data-vl-cue");
        if (cueId) {
          var cue = pickCue(cueId);
          if (!cue) { return; }
          state.selectedId = cueId;
          renderCues();
          fillEditor(cue);
          var v = videoEl();
          var seekAt = parseFloat(cue.seek);
          if (v && isFinite(seekAt)) { v.currentTime = seekAt; v.play(); }
          return;
        }
        var act = node.getAttribute("id");
        if (act === "cap-save" || act === "cap-save-time") {
          var idEl = doc.getElementById("cap-cue-id");
          var textEl = doc.getElementById("cap-text");
          var startEl = doc.getElementById("cap-start");
          var endEl = doc.getElementById("cap-end");
          if (!idEl || !idEl.value) { msg("큐를 먼저 목록에서 고르세요."); return; }
          call("cap_save", {
            op: act === "cap-save" ? "text" : "time",
            cueId: idEl.value,
            text: textEl ? textEl.value : "",
            startMs: startEl ? startEl.value : "",
            endMs: endEl ? endEl.value : "",
            head: model.head,
          }, "저장하는 중… — 재생은 그대로 이어진다");
          return;
        }
        if (act === "cap-confirm") {
          if (!model.canConfirm) {
            msg(model.blockReason || "확정 조건이 남아 있다 — 열린 경고·미대응을 먼저 처리하세요.");
            return;
          }
          call("cap_confirm", { head: model.head }, "확정 게시하는 중…");
          return;
        }
      };
      // 배선마다 듣는이를 갈아끼운다 — 옛 클로저가 재렌더 뒤 살아 있지 않게.
      if (prevCapClick) { root.removeEventListener("click", prevCapClick); }
      root.addEventListener("click", root._vlCapClick);

      renderCues();
      var first = pickCue(state.selectedId) || (model.cues &&
        model.cues.length ? model.cues[0] : null);
      if (first) { fillEditor(first); }
      tick();
      if (state.lastMsg) { msg(state.lastMsg); }
      // 커밋 morph가 지운 내려박기 링크를 이 화면의 것만 다시 그린다(확정 흐름).
      paintDownloads();
      // 낡은 원본 출력 방어의 재그림 고리 — 새 배선 세대를 만들지 않고 이 배선의
      // 그림 함수로 지금 커밋된 모델을 다시 칠한다(위 방어 분기가 쓴다).
      root.__vlCaptionRepaint = function () {
        renderCues();
        var cue = pickCue(state.selectedId) || (model.cues &&
          model.cues.length ? model.cues[0] : null);
        if (cue) { fillEditor(cue); }
        tick();
        if (state.lastMsg) { msg(state.lastMsg); }
        paintDownloads();
      };
    };
  }
  // 영상 작업대와 같은 고침(설치본 실측: watch는 인자 없이 불리고 직접 대입은
  // watch를 아예 안 부르며, 렌더기는 같은 태그 뿌리를 그 자리에서 morph한다 —
  // A7/A8 실측의 0큐 결함). 재배선의 주된 길은 부팅 때부터 상시인 관찰자와 대입
  // 직후 스케줄러, watch는 보조 신호다. **커밋된 모델 문자열**(세대)이 다를 때만
  // 다시 묶는다 — 보통의 저장 재렌더(같은 토큰·새 epoch)는 morph든 교체든 선택·
  // 재생이 그대로 이어진다.
  var teardownEmpty = function () {
    // 자리표시 커밋(등록된 최종 영상 없음 — data-vl-empty, 모델 없음)도 그 자리
    // morph로 온다(A9 실측). 묶은 화면을 정리한다: 배선 세대 무효화(옛 클로저·
    // 늦은 응답 사망 — 문서 epoch가 사라져 echoEpoch 검사도 버린다), 상태·편집
    // 칸·큐 목록·안내문·오버레이 청소, 재생 중이던 영상 능동 언로드.
    W.__vlCaptionWireSerial = (W.__vlCaptionWireSerial || 0) + 1;
    var st = W.__vlCaptionWorkbenchState;
    if (st) {
      st.wire = null; st.epoch = null; st.model = null; st.token = null;
      st.selectedId = null; st.lastMsg = ""; st.downloads = null;
    }
    var d = W.__vlCaptionDoc || document;
    var idEl = d.getElementById("cap-cue-id");
    if (idEl) { idEl.value = ""; }
    var textEl = d.getElementById("cap-text");
    if (textEl) { textEl.value = ""; }
    var startEl = d.getElementById("cap-start");
    if (startEl) { startEl.value = ""; }
    var endEl = d.getElementById("cap-end");
    if (endEl) { endEl.value = ""; }
    var cues = d.getElementById("cap-cues");
    if (cues) { cues.innerHTML = ""; }
    var msgEl = d.getElementById("cap-msg");
    if (msgEl) { msgEl.textContent = ""; }
    var overlay = d.getElementById("caption-overlay");
    if (overlay) { overlay.textContent = ""; }
    var v = W.__vlCaptionVideoEl;
    if (v) {
      if (v._vlCapTick) { v.removeEventListener("timeupdate", v._vlCapTick); }
      if (v._vlLoop) {
        try { v._vlLoop(); } catch (err) { /* 최선 정리 — 정지가 본질이다 */ }
        v._vlLoop = null;
      }
      try {
        v.pause();
        v.removeAttribute("src");
        v.load();
      } catch (err) {
        // 언로드 최선 — 정지가 본질이다
      }
    }
    W.__vlCaptionVideoEl = null;
  };
  var wireCommitted = function () {
    var d = W.__vlCaptionDoc || document;
    var fresh = d.getElementById("caption-workbench");
    if (!fresh || !fresh.getAttribute) { return; }
    var raw = W.__vlLib.modelRaw(fresh);
    if (!raw) {
      // 반복 통보는 무동작(같은 빈 세대 표식) — 관찰자 루프 없음.
      if (fresh.__vlCaptionWiredModel === "__vlEmpty__") { return; }
      fresh.__vlCaptionWiredModel = "__vlEmpty__";
      teardownEmpty();
      return;
    }
    if (fresh.__vlCaptionWiredModel === raw) { return; }
    // 먼저 찍고 묶는다 — 배선 중 관찰자가 다시 불려도 같은 모델엔 재배선 0.
    fresh.__vlCaptionWiredModel = raw;
    W.__vlWireCaption(fresh, d);
  };
  var rewireScheduled = function () {
    wireCommitted();
    Promise.resolve().then(wireCommitted);
  };
  W.__vlCaptionBlockEl = element;
  W.__vlCaptionDoc = document;
  // 영상 작업대와 같은 이름 규칙 — call()의 {state_key}Rewire 조회와 짝 맞춤.
  W.__vlCaptionWorkbenchRewire = rewireScheduled;
  var Observer = W.MutationObserver ||
    (typeof MutationObserver !== "undefined" ? MutationObserver : null);
  if (Observer && element && !element.__vlCaptionObserver) {
    // 영상 작업대와 같은 관찰 — 포장 안쪽 morph(subtree)와 모델 칸의 글자·옛 모델 속성
    // (세대)을 함께 본다. 배선은 모델을 건드리지 않는다 — 루프 없음(낡은 출력 방어가 칸을
    // 지금 모델로 되돌려도 표식이 같아 wireCommitted가 무동작).
    element.__vlCaptionObserver = new Observer(function () { wireCommitted(); });
    element.__vlCaptionObserver.observe(element, { childList: true, subtree: true,
      characterData: true, attributes: true, attributeFilter: ["data-vl-model"] });
  }
  if (!W.__vlCaptionWatchBound) {
    W.__vlCaptionWatchBound = true;
    watch("value", rewireScheduled);
  }
  W.__vlCaptionWorkbenchProps = props;
  W.__vlCaptionWorkbenchServer = server;
  W.__vlWireCaption(document.getElementById("caption-workbench"), document);
})();
"""
)


__all__ = [
    "CAPTION_BOOTSTRAP_JS",
    "CueRowView",
    "VISUAL_BOOTSTRAP_JS",
    "WORKBENCH_CSS",
    "caption_cue_choices",
    "caption_overlay_player_html",
    "caption_player_html",
    "cue_table_html",
    "cue_table_markdown",
    "caption_workbench_details_html",
    "caption_workbench_html",
    "cue_table_markdown",
    "format_bytes",
    "format_seconds",
    "media_player_html",
    "parse_seconds",
    "review_rows_markdown",
    "visual_player_shell_html",
    "visual_workbench_html",
    "vtt_timecode",
    "waveform_svg",
]
