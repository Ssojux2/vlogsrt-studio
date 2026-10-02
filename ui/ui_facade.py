"""브라우저 화면의 순수 조립 도우미 — 표준 라이브러리만 쓴다.

이 모듈은 아무 파생 계층도 import하지 않는다(계약: UI 도우미는 순수). 값의
모양을 사람이 읽는 표·HTML로 바꾸거나, 사람이 적은 입력을 값으로 해석할 때만
쓴다. 정본은 어디까지나 서버(공개 서비스·백엔드 작업공간)에 있다.
"""

from __future__ import annotations

import html
from collections.abc import Mapping, Sequence
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
            f" | {row.basis} | {row.chars_per_second:.1f} |"
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
            '<p style="color:#888">큐가 없다 — 실행을 먼저 끝내거나, 빈 실행이면'
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
            listen = '<span style="color:#aaa">—</span>'
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
            f'<td style="padding:4px 6px;text-align:right;">{row.chars_per_second:.1f}</td>'
            f'<td style="padding:4px 6px;text-align:center;white-space:nowrap;">{listen}</td>'
            "</tr>"
        )
    parts.append("</tbody></table>")
    if seek_by_cue is None:
        parts.append(
            '<p style="color:#888;font-size:12px;margin:4px 0 0;">선택 오디오 미리보기가'
            " 아직 없다 — 최종 영상 등록(오디오 트랙 확정)을 마치면 큐 단추가 켜진다.</p>"
        )
    else:
        parts.append(
            '<p style="color:#667;font-size:12px;margin:4px 0 0;">'
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
        ' style="width:100%;max-height:420px;background:#000;border-radius:8px;"></video>\n'
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
        f'  <p style="color:#667;font-size:12px;margin:6px 0 0;">{html.escape(provenance)}</p>\n'
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


__all__ = [
    "CueRowView",
    "caption_cue_choices",
    "caption_player_html",
    "cue_table_html",
    "cue_table_markdown",
    "format_bytes",
    "format_seconds",
    "media_player_html",
    "parse_seconds",
    "review_rows_markdown",
    "vtt_timecode",
    "waveform_svg",
]
