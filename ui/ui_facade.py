"""브라우저 화면의 순수 조립 도우미 — 표준 라이브러리만 쓴다.

이 모듈은 아무 파생 계층도 import하지 않는다(계약: UI 도우미는 순수). 값의
모양을 사람이 읽는 표·HTML로 바꾸거나, 사람이 적은 입력을 값으로 해석할 때만
쓴다. 정본은 어디까지나 서버(공개 서비스·백엔드 작업공간)에 있다.
"""

from __future__ import annotations

import html
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
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


# ── V2 작업대 — 순수 조립 + 코드 네이티브 클라이언트 ──────────────────────────
#
# 설계 원칙(코디네이터 계약): 선택·탐색·반복·오버레이는 **이미 받은 값만으로**
# 클라이언트에서 한다(Gradio 이벤트·미디어 생성 0회). 서버에 가는 길은 오직
# 명시적 편집·준비·질의뿐이고, 그 응답은 새 작업대 HTML 값으로 돌아와
# `watch`로 다시 묶인다. 영상 원소는 작업대와 **다른 구성요소**에 살아 있어
# 같은 원본을 다시 고르는 동안 재생 머리가 사라지지 않는다.


def _model_attribute(model: Mapping[str, object]) -> str:
    """작업대 상태를 뿌리 칸의 data 속성에 안전히 싣는다 — <script>는 쓰지 않는다.

    gradio 6은 값에 `<script`가 있으면 경고를 내고 실행도 안 된다(innerHTML).
    대신 이스케이프된 JSON을 속성에 넣고 클라이언트가 `dataset`으로 읽는다.
    `</` 계열도 속성 안에서는 무해하지만 이중 안전망으로 `<`를 유니코드 이스케이프해
    둔다(문자열 값이 태그처럼 보이게 두지 않는다).
    """
    payload = json.dumps(model, ensure_ascii=False, separators=(",", ":"))
    return html.escape(payload.replace("<", "\\u003c"), quote=True)


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
        '<strong>타임라인</strong><span class="vl-legend" aria-label="등급 범례">'
        '<span class="vl-tier-key green">■ 추천</span> · '
        '<span class="vl-tier-key yellow">■ 보통</span> · '
        '<span class="vl-tier-key red">▨ 제외 제안</span> · '
        '<span class="vl-tier-key hold">□ 판정 보류</span></span>'
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
        "</section></div>"
    )


def visual_workbench_html(model: Mapping[str, object]) -> str:
    """영상 작업대 본문 — 조각 목록·선택 패널의 빈 그릇과 상태 JSON.

    그릇을 채우는 건 이 파일의 클라이언트 실행본(`VISUAL_BOOTSTRAP_JS`)이다.
    서버는 여기까지: 값은 전부 공개 질의가 준 것, 사람이 읽는 글자는 전부
    이스케이프되어 있다(모델 안 글자는 클라이언트가 textContent로만 싣는다).
    목록·패널·각주는 **한 격자의 세 칸**으로 펼쳐진다 — 뿌리는
    `display:contents`로 무대(`#visual-stage`) 격자에 그대로 참여하고, 재생기
    껍데기(가운데 칸)는 별도 구성요소라 같은 원본을 다시 고르는 동안 재생이
    이어진다(모바일 ≤860px에서는 격자 영역 순서로 재생기가 맨 먼저 온다).
    """
    placeholder = model.get("empty")
    if isinstance(placeholder, str) and placeholder:
        return (
            '<div id="visual-workbench" class="vl-workbench" data-vl-empty="1">'
            f'<p class="vl-note">{html.escape(placeholder)}</p></div>'
        )
    return (
        '<div id="visual-workbench" class="vl-workbench"'
        f' data-vl-model="{_model_attribute(model)}">\n'
        '  <div id="vl-thumbs" class="vl-thumbs" role="listbox"'
        ' aria-label="조각 목록(시간순, 페이지 간 24개)"></div>\n'
        '  <div id="vl-panel" class="vl-panel" aria-live="polite"></div>\n'
        '  <p id="vl-footnote" class="vl-footnote" role="status"></p>\n'
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
        'background:#000;border-radius:8px;object-fit:contain;"></video>\n'
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
        '<div id="caption-workbench" class="vl-workbench"'
        f' data-vl-model="{_model_attribute(model)}">\n'
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


WORKBENCH_CSS = """
/* V2F1 작업대 — 무채색은 그라디오 테마 토큰을 따르고 본점(선택 강조)은 하나다. */
#visual-player, #visual-workbench, #caption-workbench { max-width: 100%; }
.vl-note {
  color: var(--body-text-color-subdued, #667); font-size: 12px; margin: 4px 0 0;
}
/* 3단 무대 — 왼쪽 시간순 목록 · 가운데 큰 재생기+시간 렠 · 오른쪽 선택 패널.
   구성요소 껍데기(vl-stage-cell)와 본문 뿌리는 display:contents로 격자에
   참여한다. 무대 치수는 여기 한 곳 — 흩어진 재선언이 마지막에 이겨 목록 칸이
   180px로 묶이고 재생기·타임라인이 화면 아래로 밀리던 결함(D50).
   --vl-stage-top은 실행본(__vlFitStage)이 실측해 덮어쓴다 — 머리글·아코디언
   높이는 CSS가 모르므로 무대 높이를 화면에서 무대 위쪽을 뺀 값으로 맞춘다. */
#visual-stage {
  --vl-stage-top: 320px;
  /* 바닥값 = 타임라인 렠 칸(≈200) + 각주·간격(≈30) + 자막 줄·안내(≈60) + 최소 영상(≈110).
     560이면 800px 높이 창(무대 위쪽 279–335px 실측)에서 렠이 6–62px 잘렸다(D50). */
  --vl-stage-min-h: 400px;
  --vl-list-col: minmax(240px, 300px);
  --vl-panel-col: minmax(240px, 300px);
  --vl-stage-gap: 10px;
  --vl-thumb-art-w: 64px; --vl-thumb-art-h: 46px;
  --vl-name-lines: 2;
  --vl-scrollbar: #555e66 #292b2f;
  display: grid;
  grid-template-columns: var(--vl-list-col) minmax(0, 1fr) var(--vl-panel-col);
  grid-template-rows: minmax(0, 1fr) auto auto;
  grid-template-areas: "thumbs player panel" "timeline timeline timeline" "foot foot foot";
  height: max(var(--vl-stage-min-h), calc(100vh - var(--vl-stage-top)));
  gap: var(--vl-stage-gap); align-items: stretch; margin-top: 4px; min-width: 0;
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
  border-radius: 8px; border: 1px solid var(--block-border-color, #e3e8ee);
  background: var(--block-background-fill, transparent);
}
#visual-stage .vl-thumbs > .vl-row, #visual-stage .vl-pager { grid-column: 1 / -1; }
/* 테두리·배경은 클래스 단계에 둔다 — 선택(.vl-thumb-sel) 강조가 무대 범위
   선택자에 덮이지 않는다. 배치만 무대 범위에서 정한다. */
.vl-thumb {
  display: flex; border-radius: 8px; text-align: left; cursor: pointer;
  border: 1px solid var(--block-border-color, #e3e8ee);
  background: var(--block-background-fill, #fff);
  color: var(--body-text-color, #223);
}
.vl-thumb-sel { border-color: #2f6db3; box-shadow: 0 0 0 1px #2f6db3; }
.vl-thumb-ex { opacity: 0.55; }
#visual-stage .vl-thumb {
  min-width: 0; width: 100%; flex-direction: row; gap: 6px; padding: 5px; align-items: center; }
.vl-thumb img { background: #000; border-radius: 4px; }
.vl-thumb-missing {
  border-radius: 4px; background: var(--block-background-fill, #eef1f4);
  color: var(--body-text-color-subdued, #889); font-size: 10px;
  display: flex; align-items: center; justify-content: center;
}
#visual-stage .vl-thumb img, #visual-stage .vl-thumb-missing {
  width: var(--vl-thumb-art-w); height: var(--vl-thumb-art-h); object-fit: cover; flex: none; }
#visual-stage .vl-thumb-meta {
  display: flex; flex-direction: column; gap: 2px; flex: 1 1 auto; min-width: 0; }
/* 긴 파일 이름은 두 줄까지 보이고 말줄임 — 전체 이름은 카드 title 말풍선(D50). */
#visual-stage .vl-thumb-name {
  font-size: 13px; color: var(--body-text-color, #223);
  max-width: none; white-space: normal; overflow-wrap: anywhere; line-height: 1.25;
  display: -webkit-box; -webkit-line-clamp: var(--vl-name-lines); -webkit-box-orient: vertical;
  overflow: hidden; }
.vl-thumb-sub {
  display: flex; flex-wrap: wrap; gap: 3px; align-items: center; min-width: 0;
  font-size: 11px; color: var(--body-text-color-subdued, #778);
  font-variant-numeric: tabular-nums;
}
/* 배지는 줄 안에서 쪼개지지 않고 통째로 다음 줄로 넘어간다 — 잘림 없음. */
.vl-badge {
  display: inline-block; font-size: 10px; padding: 1px 6px; border-radius: 8px;
  background: var(--block-background-fill, #eef1f4);
  color: var(--body-text-color-subdued, #556);
  border: 1px solid var(--block-border-color, transparent); white-space: nowrap;
}
.vl-badge-warn { background: rgba(179, 69, 47, 0.14); color: #b3452f; }
#visual-player { display: contents; }
#visual-preview { grid-area: player; min-width: 0; min-height: 0;
  display: flex; flex-direction: column; }
.vl-video-surface { position: relative; flex: 1 1 auto; min-height: 0; display: flex; }
#visual-preview video {
  width: 100%; height: auto; flex: 1 1 auto; min-height: 0;
  background: #08090b; object-fit: contain; }
.vl-current-caption, #visual-note { flex: none; }
#visual-note, .vl-footnote { overflow-wrap: anywhere; }
#vl-panel { grid-area: panel; max-height: 100%; min-height: 0; overflow: hidden auto;
  box-sizing: border-box; scrollbar-color: var(--vl-scrollbar); }
#visual-timeline { grid-area:timeline; min-width:0; overflow-x:auto; padding-top:4px; }
#vl-footnote { grid-area: foot; }
/* 재생기 아래 두 줄의 여백 — 계약 시험이 위 격자 선언을 글자 그대로 본다. */
#vl-footnote, #visual-note { margin: 2px 0 0; }
/* 폭 상한은 Blocks(fill_width=True)가 푼다 — 여기는 가로 넘침만 막는다. */
.gradio-container, .gradio-container .app { min-width: 0; max-width: 100vw; }
/* 정리 탭 세로 예산(D50) — 그라디오 .row > * 의 min-width 160·flex 1이 단추 라벨을
   두 줄로 접어 64px를 먹었다. 단추는 제 글자 폭만큼만 차지한다. 실측 DOM:
   div#review-tab.tabitem > div.column > div.row > button. */
#review-tab .column { gap: 6px; }
#review-tab > .column > .row > button { flex: 0 1 auto; min-width: 0; padding: 6px 12px; }
#quick-status { padding: 2px 8px; }
#quick-status p { margin: 1px 0; font-size: 12px; }
#library-analysis-scope p { margin: 0; }
#visual-video::cue { color:#fff; background:rgba(0,0,0,.78); font-size:20px; }
.vl-source-overlay { position:absolute; bottom:44px; left:50%; transform:translateX(-50%);
 max-width:90%; padding:5px 10px; border-radius:4px; text-align:center; white-space:pre-wrap;
 background:rgba(0,0,0,.78); color:#fff; font-size:20px; line-height:1.4; pointer-events:none; }
.vl-source-overlay[hidden] { display:none; }
.vl-legend { flex:1; color:#b8c3ca; font-size:12px; }
.vl-good { color:#49d8c7; } .vl-review { color:#edbd74; }
.vl-ruler { position:absolute; inset:0 0 auto; height:var(--vl-ruler-h);
 border-bottom:1px solid #41464c; }
.vl-ruler span { position:absolute;font-size:10px;color:#b1b9c1;transform:translateX(-50%); }
.vl-recommendation { position:absolute; top:var(--vl-rec-top); height:var(--vl-rec-h);
 border:0;border-radius:3px;
 background:#4c6871;color:white;overflow:hidden;white-space:nowrap;text-overflow:ellipsis;
 font-size:11px;min-width:3px;text-align:left;padding:1px 6px; }
.vl-recommendation.recommended { background:#177e73;color:#e9fffb;border-top:2px solid #48d7c7; }
.vl-recommendation.review_required {
 background:#745638;color:#ffe8c4;border-top:2px solid #edbd74; }
/* 3단 등급(D51) — 색만으로 가르지 않는다: 빗금(빨강)·점선(보류)·글자 표지가 함께 간다. */
.vl-recommendation.green{background:#1f6f46;border-top:2px solid #6fe3a5}
.vl-recommendation.yellow{background:#6f5f1a;border-top:2px solid #ffd760}
.vl-recommendation.red{background:repeating-linear-gradient(135deg,#7a2d2d 0 6px,#561c1c 6px 12px);
 border-top:2px solid #ff8a80}
.vl-recommendation.yellow.hold{border-top-style:dotted}
.vl-tier-key.green{color:#6fe3a5} .vl-tier-key.yellow{color:#ffd760}
.vl-tier-key.red{color:#ff8a80} .vl-tier-key.hold{color:#b8c3ca}
.vl-badge-green{border-color:#6fe3a5;color:#6fe3a5}
.vl-badge-red{border-color:#ff8a80;color:#ff8a80}
.vl-marker-reason.red{background:#3a2222;border-left-color:#ff8a80}
.vl-marker-reason.yellow{background:#36311a;border-left-color:#ffd760}
.vl-rec-pending { position:absolute; top:calc(var(--vl-rec-top) + 1px); left:10px;
 color:#aab5be; font-size:12px; }
/* 그라디오 button{font:inherit}이 16px·22.4px 줄과 큰 단추 여백을 물려 22px 칸에서
   글자가 6px 잘렸다(D50 실측) — 이 층만 제 글자 크기·줄 높이·여백을 정한다. */
.vl-source-caption { position:absolute; top:var(--vl-cue-top); height:var(--vl-cue-h);
  font-size:12px; line-height:1.3; padding:0 6px;
  border:0;border-radius:3px;
  background:#36598a;color:#fff;white-space:nowrap;overflow:hidden;text-overflow:ellipsis; }
.vl-source-caption-editor { border:1px solid #36598a;padding:10px; }
.vl-audio-strip { position:absolute; top:var(--vl-audio-top); left:0; right:0;
 height:var(--vl-audio-h); padding:3px 8px;
 background:#193749;color:#bad8e8;font-size:11px;box-sizing:border-box; }
.vl-int span { background:#172a32b0;padding:2px 6px;font-size:10px;color:#f2fbff; }
.vl-marker-reason { padding:10px;background:#223d3a;border-left:3px solid #43caba; }

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
   실행본은 층의 left·width만 정하고 세로 좌표는 CSS만 안다. */
.vl-rail {
  --vl-rail-h: 150px; --vl-ruler-h: 16px;
  --vl-rec-top: 20px; --vl-rec-h: 20px;
  --vl-int-top: 44px; --vl-int-h: 40px; --vl-handle-h: 28px;
  --vl-audio-top: 88px; --vl-audio-h: 22px;
  --vl-cue-top: 114px; --vl-cue-h: 22px;
  position: relative; height: var(--vl-rail-h); border-radius: 6px; margin-top: 4px;
  touch-action: none; cursor: pointer; overflow: visible;
  background: var(--block-background-fill, #edf1f5);
  border: 1px solid var(--block-border-color, transparent);
}
.vl-int {
  position: absolute; top: var(--vl-int-top); height: var(--vl-int-h);
  border:1px solid #697d85; border-radius:4px;
  background-size:auto 100%;background-repeat:repeat-x;
  background: var(--border-color-primary, #c8d4de); min-width: 2px; padding: 0;
  background-size:auto 100%;background-repeat:repeat-x;
}
.vl-int-sel {
  outline: 2px solid #2f6db3; outline-offset: 1px;
  background: rgba(47, 109, 179, 0.28);
}
.vl-int-ex { opacity: 0.45; }
.vl-int-needs { border-bottom: 3px solid #b3452f; }
.vl-playhead {
  position: absolute; top: 0; bottom: 0; width: 2px;
  background: #b3452f; pointer-events: none;
}
.vl-handle {
  position: absolute; top: 2px; width: 12px; height: var(--vl-handle-h); border-radius: 6px;
  background: #2f6db3; cursor: ew-resize; border: none; padding: 0; z-index: 2;
}
.vl-handle:focus-visible { outline: 2px solid #17406b; }
.vl-handle:disabled {
  background: var(--border-color-primary, #b9c2cb); cursor: default;
}
.vl-draftmark {
  position: absolute; top: -4px; bottom: -4px; width: 2px;
  background: #d98324; pointer-events: none; z-index: 3;
}
.vl-transport { display:flex; gap:8px; align-items:center; margin-top:2px; flex-wrap:wrap; }
.vl-clock {
  font-variant-numeric: tabular-nums;
  color: var(--body-text-color, #445); font-size: 13px;
}
.vl-panel {
  border: 1px solid var(--block-border-color, #e3e8ee); border-radius: 8px;
  padding: 10px; min-width: 0;
  background: var(--block-background-fill, transparent);
}
.vl-row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin-top: 8px; }
.vl-btn {
  border: 1px solid var(--border-color-primary, #c9d3dc);
  background: var(--button-secondary-background-fill, #fff);
  color: var(--button-secondary-text-color, #233);
  border-radius: 6px; padding: 5px 12px; font-size: 13px; cursor: pointer;
}
.vl-btn:hover:not(:disabled) {
  background: var(--button-secondary-background-fill-hover, #f2f6fa);
}
.vl-btn:disabled { color: var(--body-text-color-subdued, #9aa); cursor: default; }
.vl-btn-primary { background: #2f6db3; border-color: #2f6db3; color: #fff; }
.vl-btn-primary:hover:not(:disabled) { background: #27599a; }
.vl-input {
  border: 1px solid var(--input-border-color, #c9d3dc); border-radius: 6px;
  padding: 4px 8px; font-size: 13px; width: 100%; box-sizing: border-box;
  background: var(--input-background-fill, #fff);
  color: var(--body-text-color, #233);
}
.vl-input-narrow { width: 9ch; }
.vl-kv {
  font-size: 12px; color: var(--body-text-color, #556); margin: 2px 0;
  font-variant-numeric: tabular-nums;
}
.vl-draft {
  border: 1px dashed #d98324; border-radius: 8px; padding: 8px; margin-top: 8px;
  background: rgba(217, 131, 36, 0.08);
}
.vl-details { margin-top: 8px; font-size: 12px; }
.vl-details summary { cursor: pointer; color: var(--body-text-color-subdued, #556); }
.vl-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.vl-th, .vl-td {
  text-align: left; padding: 3px 6px;
  border-bottom: 1px solid var(--block-border-color, #edf1f5); vertical-align: top;
}
.vl-th { color: var(--body-text-color-subdued, #667); }
.vl-footnote {
  color: var(--body-text-color, #445); font-size: 12px;
  margin: 6px 0 0; min-height: 1em;
}
.vl-pager {
  display: flex; gap: 8px; align-items: center; justify-content: center;
  padding: 6px 0 2px; font-size: 12px; color: var(--body-text-color-subdued, #556);
}
.cap-overlay {
  min-height: 1.7em; margin-top: 4px; padding: 4px 10px; font-size: 15px;
  background: var(--block-background-fill, #f4f7fa); border-radius: 6px;
  color: var(--body-text-color, #223);
  border: 1px solid var(--block-border-color, transparent);
}
.cap-overlay:not(.cap-overlay-on) { display: none; }
.cap-cues {
  max-height: 34vh; overflow-y: auto; display: flex; flex-direction: column; gap: 4px;
  margin-top: 8px; border-radius: 8px; padding: 6px;
  border: 1px solid var(--block-border-color, #e3e8ee);
}
.cap-row {
  display: flex; gap: 8px; border: none; background: none; padding: 4px 6px;
  border-radius: 6px; cursor: pointer; text-align: left; align-items: baseline;
  width: 100%; color: var(--body-text-color, #223);
}
.cap-row:hover { background: var(--block-background-fill, #f2f6fa); }
.cap-row-sel { background: rgba(47, 109, 179, 0.16); }
.cap-row-time {
  flex: none; font-size: 11px; color: var(--body-text-color-subdued, #778);
  font-variant-numeric: tabular-nums; width: 12ch;
}
.cap-row-text { font-size: 14px; color: var(--body-text-color, #223); }
.cap-row-manual .cap-row-text::after { content: " ·수동"; color: #977; font-size: 11px; }
.cap-editor { margin-top: 8px; }
.cap-editor-fields { display: flex; gap: 8px; flex-wrap: wrap; }
.cap-editor-fields label {
  display: flex; flex-direction: column; gap: 2px; font-size: 11px;
  color: var(--body-text-color-subdued, #667); min-width: 0;
}
.cap-editor-fields label:first-child { flex: 1 1 220px; }
#visual-video::cue,.vl-source-overlay { font-size:clamp(20px,1.8vw,28px); }
.vl-current-caption { width:100%; min-height:36px; padding:5px 10px; margin-top:4px;
 color:#f5f7f8; background:#292e34; border:1px solid #54616b; border-radius:5px;
 font-size:18px; line-height:1.3; text-align:center; white-space:pre-wrap; cursor:pointer; }
.vl-current-caption:disabled { color:#b7c2cb;font-size:14px;cursor:default; }
#vl-panel [data-vl-act="toggle-sel"] { flex:1 1 100%;font-size:15px;padding:9px; }
#app-header p { font-size:12px;margin:2px 0; }
/* 반응형 — 넓은 쪽(1050) 다음 좁은 쪽(860) 순서여야 좁은 규칙이 이긴다(D50:
   예전엔 860 뒤에 무대 재선언이 와서 860 규칙이 죽어 있었다). 좁은 화면은 무대
   높이를 화면에 묶지 않고 문서 스크롤에 맡긴다. 390px 등에서 격자 영역 순서가
   재생기를 맨 처음 보내고 가로 넘침은 없다(minmax(0,1fr) 단일 칸). */
@media (max-width: 1050px) {
  #visual-stage { height: auto; grid-template-columns: minmax(150px, 200px) minmax(0, 1fr);
    grid-template-areas: "thumbs player" "thumbs panel" "timeline timeline" "foot foot"; }
  #visual-stage .vl-thumbs, #vl-panel { max-height: 64vh; }
  #visual-preview video { flex: none; height: clamp(300px, 48vh, 540px); }
}
@media (max-width: 860px) {
  #visual-stage { grid-template-columns: minmax(0, 1fr);
    grid-template-areas: "player" "thumbs" "panel" "timeline" "foot"; }
  #caption-stage { grid-template-columns: minmax(0, 1fr); grid-template-areas: "player" "cues"; }
  #visual-stage .vl-thumbs { grid-template-columns: repeat(2, minmax(0, 1fr)); max-height: 40vh; }
  #visual-preview video { height: clamp(230px, 45vh, 420px); }
  .cap-cues { max-height: 28vh; }
}

"""
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
        var raw = el.getAttribute("data-vl-model");
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
        msg(note || "서버에 보내는 중…");
        // 새 요청이 시작되면 이 화면에 남아 있던 내려받기 링크는 물러난다 —
        // 편집 뒤 낡은 머리의 링크가 살아 있지 않게(자막 확정 흐름).
        state.downloads = null;
        if (!server || !server[name]) {{ msg("서버 함수가 없다: " + name); return; }}
        server[name](payload).then(function (res) {{
          if (lateRequest()) {{ return; }}
          if (!res) {{ msg("빈 응답이다"); return; }}
          if (res.ok === false) {{
            // 신선한 요청의 진짜 오류다 — 공개 생산자의 오류 응답은 echoEpoch 없이
            // {{ok:false, error}}로 온다(실측: 늦은 머리·빈 글자 안내가 echo 검사에서
            // 갈려 저장 중 표시만 남던 결함). 신선도는 lateRequest가 이미 판정했다;
            // 오류는 상태를 못 바꾸니 원문 그대로 보인다(숨김 없음).
            msg(res.error || "실패했다");
            if (onResponse) {{ onResponse(res); }}
            return;
          }}
          var now = modelEpoch();
          if (res.echoEpoch !== now) {{ return; }}
          if (!res.ok) {{
            msg(res.error || "실패했다");
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
          if (lateRequest()) {{ return; }}
          msg("서버 오류 — " + (err && err.message ? err.message : String(err)));
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
  // 보이는 첫 영상 하나만 조작한다 — 숨은 탭의 영상은 offsetParent가 null이다.
  var VL_SHORTCUT_VIDEOS = [["visual-video", "vl-footnote"], ["caption-video", "cap-msg"]];
  var VL_SHORTCUT_ACTIONS = { "Space": "toggle", "KeyK": "toggle", "KeyJ": "back",
    "KeyL": "forward", "Comma": "frameBack", "Period": "frameForward" };
  var VL_PLAY_FAILED_TEXT = "재생할 수 없다: ";
  if (!W.__vlWireVisual) {
    W.__vlWireVisual = function (root, doc) {
"""
    + _workbench_state("visual-workbench", "VisualWorkbench")
    + r"""
      var VL = W.__vlLib;
      if (!root || !root.getAttribute) { return; }
      var rawModel = root.getAttribute("data-vl-model");
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
        state.selectedId = null; state.marker = null;
        state.selectionToken = model.selectionToken;
      }
      state.model = model;
      state.filter = model.filter || "all";
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
        var el = doc.getElementById("vl-footnote");
        if (el) { el.textContent = text || ""; }
        state.lastMsg = text || "";
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
      function findCard(id) {
        var allCards = (model.cards || []).concat(model.mediaCards || []);
        for (var i = 0; i < allCards.length; i += 1) {
          if (allCards[i].id === id) { return allCards[i]; }
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
          audio_absent: "소리 없음", analysis_failed: "분석 실패" })[s] || "";
      }
      function recLabel(r) {
        return ({ recommended: "사용 권고", normal: "보통",
          review_required: "검토 필요" })[r] || "";
      }
      // 3단 등급(D51) — 글자 표지는 색과 별개로 등급을 말한다(색약·흑백에서도 읽힌다).
      function tierWord(tier, hold) {
        if (tier === "yellow" && hold) { return "판정 보류"; }
        return ({ green: "추천", yellow: "보통", red: "제외 제안" })[tier] || "";
      }
      function tierGlyph(tier, hold) {
        if (tier === "yellow" && hold) { return "? 보류"; }
        return ({ green: "★ 추천", yellow: "보통", red: "✕ 제외 제안" })[tier] || "";
      }
      function tierOf(row) {
        return row.tier || (row.rec === "recommended" ? "green" : "yellow");
      }
      function tierDetail(row) {
        if (!row.audio || !row.video) { return row.reason || ""; }
        return "자막: " + (row.audio.reason || row.audio.label || "") +
          " · 영상: " + (row.video.reason || row.video.label || "");
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
      function stopLoop() { state.loop = null; }
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
        if (!info) { say("선택 조각의 원본 전체 미리보기가 여기 재생된다."); return; }
        if (!info.src) {
          // 이 원본의 미리보기가 없다 — 이전 원본을 틀은 채 새 이름표를 붙이지
          // 않는다. 재생을 끊고 상태를 알린다(빈 성공 금지).
          if (v.getAttribute("data-vl-src")) { unloadVideo(v); }
          say("원본 " + info.sourceName + " — 미리보기 " + stateLabel(info.state) +
            " (오른쪽에서 준비한다)");
          return;
        }
        applySource(info, v);
        paintSourceCaptions();
        say("원본 " + info.sourceName + " · 미리보기 " + stateLabel(info.state));
      }

      function renderThumbs() {
        var mount = doc.getElementById("vl-thumbs");
        if (!mount) { return; }
        var rows = ['<div class="vl-row" role="group" aria-label="컷 필터">'];
        [["all","전체"],["included","사용할 컷"],
         ["excluded","제외된 컷"],["review","검토 대기"]].forEach(function(f) {
          rows.push('<button type="button" class="vl-btn" data-vl-act="filter-' + f[0] +
            '" aria-pressed="' + ((state.filter || "all") === f[0]) + '">' + f[1] + '</button>');
        });
        rows.push('</div>');
        var sel = selectedId();
        var shownCards = model.mediaCards || model.cards || [];
        for (var i = 0; i < shownCards.length; i += 1) {
          var c = shownCards[i];
          if (state.filter === "included" && c.sel !== "included") { continue; }
          if (state.filter === "excluded" && c.sel !== "excluded") { continue; }
          if (state.filter === "review" && !c.autoPending && c.rec !== "review_required" &&
              c.speech !== "analysis_failed") { continue; }
          var cls = "vl-thumb" +
            (c.id === sel || (c.media && c.sourceId === curSourceId()) ? " vl-thumb-sel" : "") +
            (c.sel === "excluded" ? " vl-thumb-ex" : "");
          var art = c.thumb && c.thumb.state === "ready" && c.thumb.src
            ? '<img src="' + esc(c.thumb.src) + '" alt="조각 대표 프레임">'
            : '<span class="vl-thumb-missing">' + esc(c.thumb &&
              c.thumb.state ? stateLabel(c.thumb.state) : "없음") + "</span>";
          var speech = speechLabel(c.speech);
          rows.push(
            '<button type="button" class="' + cls + '" data-vl-card="' +
              esc(c.id) + '" role="option"' + ' title="' + esc(c.sourceName) + '"' +
            ' aria-selected="' + (c.id === sel ? "true" : "false") + '">' + art +
            '<span class="vl-thumb-meta"><span class="vl-thumb-name">' +
              esc(c.sourceName) + (c.media ? "" : " " + (c.order + 1) + "번") + "</span>" +
            '<span class="vl-thumb-sub">' + VL.fmt(ratOf(c.start)) + "~" + VL.fmt(ratOf(c.end)) +
            "초 · " + VL.fmt(ratOf(c.dur)) + '초</span><span class="vl-thumb-sub">' +
            (c.sel === "excluded" ? '<span class="vl-badge vl-badge-warn">제외</span>' :
              '<span class="vl-badge">사용</span>') +
            (c.autoPending ?
              ' <span class="vl-badge vl-badge-red" data-vl-tier="red">제외 제안</span>' :
              c.tier === "green" ?
              ' <span class="vl-badge vl-badge-green" data-vl-tier="green">추천</span>' :
              c.tier === "yellow" ?
              ' <span class="vl-badge" data-vl-tier="yellow">보통</span>' : "") +
            (c.rec === "review_required" || c.speech === "analysis_failed" ?
              ' <span class="vl-badge vl-badge-warn">확인 필요</span>' : "") +
            (c.needsExtract ? ' <span class="vl-badge vl-badge-warn">영상 갱신 필요</span>' : "") +
            (speech ? ' <span class="vl-badge">' + esc(speech) + "</span>" : "") +
            ((model.sources[c.sourceId] || {}).analysisState ?
              ' <span class="vl-badge">' +
              esc(model.sources[c.sourceId].analysisState) + '</span>' : '') +
            "</span></span></button>"
          );
        }
        var pager = '<div class="vl-pager">' +
          '<button type="button" class="vl-btn" data-vl-act="page-prev"' +
          (model.page <= 1 ? " disabled" : "") + '>이전</button><span>페이지 ' +
          model.page + "/" + model.pageCount +
          " · 조각 " + model.total + '개</span>' +
          '<button type="button" class="vl-btn" data-vl-act="page-next"' +
          (model.page >= model.pageCount ? " disabled" : "") + ">다음</button></div>";
        mount.innerHTML = rows.join("") + pager;
      }

      function railIndex(id) {
        var rail = railOf();
        if (!rail) { return -1; }
        for (var i = 0; i < rail.intervals.length; i += 1) {
          if (rail.intervals[i].id === id) { return i; }
        }
        return -1;
      }

      function renderRail() {
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
            esc(info && info.analysisState || 'Luna 분석 대기') + '</div>');
        }
        suggestions.forEach(function(row,index) {
          var a = displayTime(ratOf(row.start)), b = displayTime(ratOf(row.end));
          if (!a || !b || VL.cmp(a,b) >= 0) { return; }
          var tier = tierOf(row);
          var detail = tierDetail(row);
          html.push('<button type="button" class="vl-recommendation ' + esc(row.rec) + ' ' +
            esc(tier) + (row.hold ? ' hold' : '') + '" data-vl-recommendation="' + index +
            '" data-vl-tier="' + esc(tier) + '" style="left:' + VL.pct(a,dur).toFixed(3) +
            '%;width:' + Math.max(.4,VL.pct(VL.sub(b,a),dur)).toFixed(3) +
            '%" aria-label="' + esc(tierWord(tier, row.hold) + ' ' + row.start + '초~' +
              row.end + '초 · ' + detail) +
            '" title="' + esc(detail) + '">' + esc(tierGlyph(tier, row.hold)) + '</button>');
        });
        ((info && info.subtitles) || []).forEach(function(cue,index) {
          var a = displayTime(ratOf(cue.start)), b = displayTime(ratOf(cue.end));
          if (!a || !b || VL.cmp(a,b) >= 0) { return; }
          html.push('<button type="button" class="vl-source-caption" data-vl-source-cue="' + index +
            '" style="left:' + VL.pct(a,dur).toFixed(3) + '%;width:' +
            Math.max(.6,VL.pct(VL.sub(b,a),dur)).toFixed(3) + '%" title="' +
            esc(cue.text) + '">' + esc(cue.text) + '</button>');
        });
        html.push('<div class="vl-audio-strip">' +
          (info && info.tracks && info.tracks.length ? '원본 오디오' : '오디오 없음') + '</div>');
        for (var i = 0; i < rail.intervals.length; i += 1) {
          var it = rail.intervals[i];
          var cls = "vl-int" + (it.id === sel ? " vl-int-sel" : "") +
            (it.sel === "excluded" ? " vl-int-ex" : "") + (it.needsExtract ? " vl-int-needs" : "");
          var frameCard = findCard(it.id);
          var thumb = frameCard && frameCard.thumb && frameCard.thumb.src;
          if (!thumb) {
            var sourceCard = (model.cards || []).find(function(c) {
              return c.sourceId === curSourceId() && c.thumb && c.thumb.src;
            });
            thumb = sourceCard && sourceCard.thumb.src;
          }
          html.push('<button type="button" class="' + cls +
            '" data-vl-interval="' + esc(it.id) + '"' +
            ' style="' + (thumb ? "background-image:url('" + esc(thumb) + "');" : "") +
            'left:' + VL.pct(ratOf(it.start), dur).toFixed(3) + "%;width:" +
            Math.max(0.4, VL.pct(VL.sub(ratOf(it.end), ratOf(it.start)), dur)).toFixed(3) + '%"' +
            ' aria-label="조각 ' + VL.fmt(ratOf(it.start)) +
              "초~" + VL.fmt(ratOf(it.end)) + '초 — 클릭·Enter로 그 시각으로 이동">' +
              '<span>' + esc(info && info.sourceName || '영상') + '</span></button>');
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
        html.push('<div class="vl-playhead" id="vl-playhead" style="left:0"></div>');
        if (state.draft && state.draft.epoch === model.epoch && state.draft.sec) {
          html.push('<div class="vl-draftmark" style="left:' +
            VL.pct(state.draft.sec, dur).toFixed(3) + '%"></div>');
        }
        mount.innerHTML = html.join("");
      }

      function markerBlock(mk, card) {
        // 자막과 영상은 따로 판정한다(D51) — 패널은 두 판정과 그 규칙을 함께 보인다.
        var out = ['<div class="vl-marker-reason ' + esc(mk.tier || "") + '"><strong>' +
          esc(mk.label) + '</strong>'];
        if (mk.audio && mk.video) {
          out.push('<p class="vl-kv">자막 · ' + esc(mk.audio.label) + ': ' +
            esc(mk.audio.reason) + '</p><p class="vl-kv">영상 · ' + esc(mk.video.label) +
            ': ' + esc(mk.video.reason) + '</p><p class="vl-kv">규칙 ' + esc(mk.rule) + '</p>');
        } else {
          out.push('<p>' + esc(mk.reason) + '</p>');
        }
        out.push('<small>' + esc(VL.fmt(ratOf(mk.start)) + '초 ~ ' +
          VL.fmt(ratOf(mk.end)) + '초') + '</small>');
        if (mk.tier === "red" && mk.segmentId && card && card.id === mk.segmentId) {
          out.push('<div class="vl-row">' +
            '<button type="button" class="vl-btn" data-vl-act="red-confirm">' +
            '제외 유지(확인)</button>' +
            '<button type="button" class="vl-btn" data-vl-act="red-revert">' +
            '사용으로 되돌리기</button>' +
            '</div>');
        }
        out.push('</div>');
        return out.join("");
      }

      function renderPanel() {
        var mount = doc.getElementById("vl-panel");
        if (!mount) { return; }
        var card = selectedCard();
        if (!card) {
          mount.innerHTML =
            '<p class="vl-note">조각이 없다 — 분할 계획을 만들고 검토를 시작하면 나타난다.</p>';
          return;
        }
        var info = curSource();
        var html = [];
        if (state.marker && state.marker.sourceId === curSourceId()) {
          html.push(markerBlock(state.marker, card));
        }
        if (model.redUnmaterialized > 0) {
          html.push('<p class="vl-kv"><span class="vl-badge vl-badge-red">제외 제안 ' +
            model.redUnmaterialized + '개</span> 현재 조각 경계와 맞지 않습니다.</p>');
        }
        if (info && info.mediaStatus) {
          html.push('<p class="vl-kv">' + esc(info.mediaStatus) + '</p>');
          if (info.originalMissing) {
            html.push('<label>원본 경로<input id="vl-original-path" class="vl-input"></label>' +
              '<button type="button" class="vl-btn" data-vl-act="relink-original">' +
            '원본 재연결</button>');
          }
        }
        ((info && info.crossSourceCandidates) || []).forEach(function(candidate) {
          html.push('<p class="vl-kv">다른 원본의 중복 후보 · ' +
            esc(candidate.other_source + ': ' + candidate.text) + ' · 자동 제외하지 않음</p>');
        });
        if (info && info.transcriptDirty) {
          html.push('<p class="vl-kv">자막 변경 · 분석 갱신 필요</p>');
        }
        if (info && info.subtitles && info.subtitles.length) {
          html.push('<details class="vl-details"><summary>원본 자막 목록</summary>');
          info.subtitles.forEach(function(u) {
            html.push('<button type="button" class="vl-btn" data-vl-act="source-pick" ' +
              'data-vl-cue-id="' + esc(u.id) + '">' +
              esc(VL.fmt(ratOf(u.start)) + '초 · ' + u.text) + '</button>');
          });
          html.push('</details>');
        }
        var cue = info && (info.subtitles || []).find(function(u) {
          return state.sourceCue && state.sourceCue.sourceId === curSourceId() &&
            u.id === state.sourceCue.id;
        });
        if (cue) {
          html.push('<fieldset class="vl-source-caption-editor"><legend>원본 자막 수정</legend>' +
            '<label>문구<textarea id="vl-source-text" class="vl-input">' +
            esc(cue.text) + '</textarea></label>' +
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
            '최종 SRT는 최종 영상에서 생성합니다.</small></fieldset>');
        }
        html.push('<p class="vl-kv"><strong>' + esc(card.sourceName) + "</strong> · " +
          VL.fmt(ratOf(card.start)) + "~" + VL.fmt(ratOf(card.end)) +
            "초 (" + VL.fmt(ratOf(card.dur)) + "초)</p>");
        var speech = speechLabel(card.speech);
        var rec = recLabel(card.rec);
        var grade = card.autoPending ? "제외 제안" : tierWord(card.tier || "", false);
        var subtitleWhy = tagText(card, "자막: ") || speech;
        var videoWhy = tagText(card, "영상: ");
        html.push('<p class="vl-kv">상태 ' + (card.sel === "excluded" ? "제외" : "사용") +
          (grade ? " · 등급 " + esc(grade) : "") +
          (subtitleWhy ? " · 자막 " + esc(subtitleWhy) : "") +
          (videoWhy ? " · 영상 " + esc(videoWhy) : "") + "</p>");
        html.push('<p class="vl-kv">' + (speech ? "발화 " + esc(speech) : "") +
          (speech && rec ? " · " : "") + (rec ? "권고 " + esc(rec) : "") + "</p>");
        var automaticAt = (card.note || "").indexOf("자동 제외: ");
        if (automaticAt >= 0) {
          try {
            var automatic = JSON.parse(card.note.slice(automaticAt + "자동 제외: ".length));
            var reasons = {internal_silence:"긴 무음", retake_overlap:"완전한 반복 테이크 확인",
              ng_with_followup:"NG 뒤 완전한 테이크 확인",
              tier_silence:"자막 무음(측정)", tier_video_defect:"영상 결함(측정)"};
            html.push('<p class="vl-kv">자동 판정 이유 · ' +
              esc(reasons[automatic.reason] || automatic.reason) + '</p>');
          } catch (error) { /* Historical/manual notes remain readable below. */ }
        }
        var tags = card.tags || [];
        if (tags.length) {
          html.push('<details class="vl-details"><summary>분석 근거</summary><p class="vl-kv">' +
            tags.map(function (t) { return esc(t); }).join(" · ") + "</p></details>");
        }
        if (card.needsExtract) {
          html.push('<p class="vl-kv"><span class="vl-badge vl-badge-warn">영상 갱신 필요</span>' +
            " 경계가 계획과 달라 새 추출로만 갱신된다.</p>");
        }
        if (info && !info.src) {
          html.push('<p class="vl-kv"><span class="vl-badge vl-badge-warn">미리보기 ' +
            stateLabel(info.state) + "</span>" +
            (info.error ? " " + esc(info.error) : " 아래 단추로 준비한다.") + "</p>");
        }
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
          html.push('<div class="vl-row"><label class="vl-kv">소리 트랙' +
            '<select id="vl-track" class="vl-input vl-input-narrow">' +
            opts.join("") + "</select></label></div>");
        }
        html.push('<details class="vl-details"><summary>재생 설정</summary><div class="vl-row">' +
          '<button type="button" class="vl-btn" data-vl-act="prepare">미리보기 다시 준비</button>' +
          '<button type="button" class="vl-btn" data-vl-act="refresh">' +
          '새로고침</button></div></details>');
        html.push('<div class="vl-row">' +
          '<button type="button" class="vl-btn" data-vl-act="toggle-sel">' +
          (card.sel === "excluded" ? "사용으로 표시" : "제외로 표시") + "</button>" +
          '<button type="button" class="vl-btn" data-vl-act="split">현재 위치에서 나누기</button>' +
          '<button type="button" class="vl-btn" data-vl-act="merge"' +
            (card.neighbor ? "" : " disabled") + ">오른쪽 조각과 합치기</button>" +
          (model.needsPlan
            ? '<button type="button" class="vl-btn vl-btn-primary" data-vl-act="replan">' +
              "계획 반영 · 재추출</button>"
            : "") +
          "</div>");
        html.push('<div class="vl-row"><label class="vl-kv" style="flex:1 1 240px">메모' +
          '<input type="text" id="vl-note-input" class="vl-input" value="' +
            esc(card.note) + '"></label>' +
          '<button type="button" class="vl-btn" data-vl-act="save-note">메모 저장</button></div>');
        if (state.draft && state.draft.epoch === model.epoch) {
          var d = state.draft;
          html.push('<div class="vl-draft"><p class="vl-kv">' +
            (d.kind === "split"
              ? "나누기 초안 — " + VL.fmt(d.sec) + "초 자리에서 둘로 나눈다."
              : "경계 초안 — " + (d.side === "left" ? "왼쪽" :
                "오른쪽") + " 경계를 " + VL.fmt(d.sec) + "초로 옮긴다.") +
            "</p>");
          if (d.pending) {
            html.push('<p class="vl-kv">서버가 스냅 시각을 계산하는 중…</p>');
          } else if (d.proposal && d.proposal.valid) {
            // 보이는 줄은 사람 시각(초 소수 셋째) — 정확한 유리수·프레임은 접힌
            // 상세가 싣는다(요청 payload는 제안의 정확값 그대로).
            html.push('<p class="vl-kv">확정 시각 <strong>' +
              VL.fmt(ratOf(d.proposal.snapSeconds)) + "초</strong> (프레임 " +
                d.proposal.frame + ", 동률이면 앞 프레임) — 저장하면 새 리비전이 생긴다.</p>");
            html.push('<details class="vl-details"><summary>스냅 정확값</summary>' +
              '<p class="vl-kv">정확 시각 <code>' + esc(d.proposal.snapSeconds) +
              "</code>초 · 프레임 <code>" + d.proposal.frame + "</code></p></details>");
            html.push('<div class="vl-row">' +
              '<button type="button" class="vl-btn vl-btn-primary" data-vl-act="apply-draft">' +
              (d.kind === "split" ? "나누기 적용" : "경계 적용") + '</button>' +
              '<button type="button" class="vl-btn" data-vl-act="cancel-draft">' +
              "취소</button></div>");
          } else {
            // 기본 줄은 사람 시각으로만 — 서버 판정의 원문(PTS 등)은 접힌 상세가
            // 그대로 싣는다(빈 성공 꾸미기 없음, 원문 보존).
            html.push('<p class="vl-kv">적용 불가 — ' + VL.fmt(d.sec) + "초 자리에서는 " +
              (d.kind === "split" ? "나눌 수" : "경계를 옮길 수") + " 없다.</p>");
            html.push('<details class="vl-details"><summary>서버 판정 사유</summary>' +
              '<p class="vl-kv"><code>' +
              esc((d.proposal && (d.proposal.reason || d.proposal.error)) || "사유 없음") +
              "</code></p></details>");
            html.push('<div class="vl-row">' +
              '<button type="button" class="vl-btn" data-vl-act="cancel-draft">' +
              "취소</button></div>");
          }
          html.push("</div>");
        }
        // 상세 — 구현 출처(원점 공식)·원값 상태·식별자는 접힌 채로만 나간다.
        html.push('<details class="vl-details">' +
          '<summary>상세 — 식별자·리비전·시계·준비 상태</summary>' +
          '<p class="vl-kv">조각 <code>' + esc(card.id) +
            "</code> · 원본 <code>" + esc(card.sourceId) + "</code></p>" +
          '<p class="vl-kv">검토 리비전 <code>' + esc(model.revisionId) +
            "</code> · 기본 계획 <code>" +
          esc(model.basePlanId) + "</code></p>" +
          (info
            ? '<p class="vl-kv">미리보기 ' + esc(info.state) +
              (info.error ? " — " + esc(info.error) : "") +
              " · 소리 " + (info.audioOrdinal === null || info.audioOrdinal === undefined
                ? "침묵" : "트랙 " + info.audioOrdinal) + "</p>" +
            '<p class="vl-kv">브라우저 시각 = 원본 시각 + ' +
              VL.str(ratOf(info.origin)) + "초(출력 영상 첫 PTS, G3)</p>"
            : "") +
          '<p class="vl-kv">발화 상태 ' + esc(card.speech || "-") +
            " · 권고 " + esc(card.rec || "-") + "</p>" +
          (model.activeJobs && model.activeJobs.length
            ? '<p class="vl-kv">준비 작업 ' + model.activeJobs.length +
              "개 진행 중 — 끝나면 새로고침으로 반영한다.</p>"
            : "") +
          (model.staleSources && model.staleSources.length
            ? '<p class="vl-kv">낡은 원본 ' + model.staleSources.length +
              "개 — 원본이 검토 뒤 바뀌었다.</p>"
            : "") +
          "</details>");
        mount.innerHTML = html.join("");
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
        call("vl_propose", payload, "스냅 시각을 계산하는 중…", function (res) {
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
        var cueIndex = ev.target.getAttribute("data-vl-source-cue");
        if (cueIndex !== null) {
          var cueInfo = curSource();
          var cue = cueInfo && (cueInfo.subtitles || [])[Number(cueIndex)];
          if (cue) {
            state.sourceCue = {sourceId:curSourceId(), id:cue.id};
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
              segmentId: selectedInterval ? selectedInterval.id : null}, row);
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
          msg("이 원본의 미리보기가 없다 — 준비가 끝나면 반복할 수 있다.");
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
        if (act === 'relink-original') {
          call('vl_relink_original',
            {sourceId:curSourceId(), path:doc.getElementById('vl-original-path').value});
          return;
        }
        if (act === 'source-pick') {
          var info = curSource();
          var picked = info && (info.subtitles || []).find(function(u) {
            return u.id === node.getAttribute('data-vl-cue-id');
          });
          if (picked) {
            state.sourceCue = {sourceId:curSourceId(),id:picked.id};
            state.marker = {sourceId:curSourceId(),label:'원본 자막',
              reason:picked.text,start:picked.start,end:picked.end};
            seekTo(ratOf(picked.start)); renderPanel();
          }
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
        var card = selectedCard();
        // 질의·페이징도 화면이 보던 문맥(계획·원본·재생 머리)을 실은 채 나간다 —
        // 재계획이 대기 중일 때 옛 계획의 선택 ID가 새 계획에서 다른 구간에
        // 재주조돼 있어도 서버가 같은 core의 조각으로 되찾을 재료다(실측).
        if (act === "refresh") { call("vl_refresh", { page: model.page,
          selectedId: selectedId(), basePlanId: model.basePlanId,
          sourceId: curSourceId(), playheadSec: VL.str(playheadLocal()) }); return; }
        if (act === "page-prev") { call("vl_refresh", { page: model.page - 1,
          selectedId: selectedId(), basePlanId: model.basePlanId,
          sourceId: curSourceId(), playheadSec: VL.str(playheadLocal()) }); return; }
        if (act === "page-next") { call("vl_refresh", { page: model.page + 1,
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
          call("vl_prepare", { page: model.page, selectedId: selectedId(), audioSelections: sel },
            "미리보기를 준비한다 — 없는 항목만 만든다…");
          return;
        }
        if (act === "red-confirm" || act === "red-revert") {
          // 이 조각만 사람이 확인한다 — 선택 편집이 미검토 표지를 걷는다(D35). 표지가
          // 묶은 조각이 지금 선택과 다르면(다른 페이지) 아무것도 보내지 않는다.
          var mark = state.marker;
          if (!mark || !mark.segmentId || !card || card.id !== mark.segmentId) { return; }
          call("vl_apply", {
            op: "selection", segmentId: mark.segmentId,
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
          if (!local) { msg("재생기를 찾지 못했다 — 미리보기가 준비된 뒤 다시 시도하세요.");
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
            "편집을 새 계획으로 옮기고 추출을 시작한다…");
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
          state.sourceCue = {sourceId:curSourceId(),id:cue.id};
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
      if (state.marker && state.selectedPlan !== model.basePlanId) {
        var markerSource = sourceInfo(state.marker.sourceId);
        var matching = intervalAt(markerSource, state.marker);
        if (matching && findCard(matching.id)) {
          state.selectedId = matching.id; state.selectedPlan = model.basePlanId;
        }
      }
      syncVideo();
      renderThumbs();
      renderRail();
      renderPanel();
      paintPlayhead();
      if (state.lastMsg) { msg(state.lastMsg); }
      else if (model.redUnmaterialized > 0) {
        msg("제외 제안 " + model.redUnmaterialized + "개가 현재 조각 경계와 맞지 않습니다 — " +
          "「자동 정리」로 경계를 갱신하거나 직접 나누어 제외하세요.");
      }
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
      st.selectedPlan = null; st.lastMsg = "";
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
  var wireCommitted = function () {
    var d = W.__vlVisualDoc || document;
    var fresh = d.getElementById("visual-workbench");
    if (!fresh || !fresh.getAttribute) { return; }
    var raw = fresh.getAttribute("data-vl-model");
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
    // 그라디오는 포장 안쪽에서 morph하므로 subtree로 보고, 모델 속성도 본다 —
    // 화면이 같아 보이는 새로고침(epoch만 바뀐 모델)은 자식 변화가 없을 수
    // 있다. 배선은 하위 원소만 고치고 data-vl-model은 건드리지 않는다 — 루프
    // 없음.
    element.__vlVisualObserver = new Observer(function () { wireCommitted(); });
    element.__vlVisualObserver.observe(element, { childList: true, subtree: true,
      attributes: true, attributeFilter: ["data-vl-model"] });
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
      var t = ev.target;
      return !!(t && (t.isContentEditable ||
        (typeof t.closest === "function" && t.closest(VL_EDITABLE))));
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
    W.__vlShortcutKey = function (ev) {
      var action = W.__vlShortcutAction(ev);
      var target = action ? W.__vlShortcutVideo() : null;
      if (!target) { return; }
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
  W.__vlWireVisual(document.getElementById("visual-workbench"), document);
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
      var rawModel = root.getAttribute("data-vl-model");
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
        try { root.setAttribute("data-vl-model", keepRaw); } catch (setErr) {
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
    var raw = fresh.getAttribute("data-vl-model");
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
    // 영상 작업대와 같은 관찰 — 포장 안쪽 morph(subtree)와 모델 속성(세대)을
    // 함께 본다. 배선은 data-vl-model을 건드리지 않는다 — 루프 없음.
    element.__vlCaptionObserver = new Observer(function () { wireCommitted(); });
    element.__vlCaptionObserver.observe(element, { childList: true, subtree: true,
      attributes: true, attributeFilter: ["data-vl-model"] });
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
