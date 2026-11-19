"""작업대의 디자인 토큰 — 색·글꼴·간격의 값이 사는 **유일한** 곳(W-17, D57). 표준 라이브러리만 쓴다.

색은 밝은·어두운 두 벌을 한 표에 둔다. 사람이 쓴 CSS(`ui/ui_facade.WORKBENCH_CSS`,
`ui/workspace_service.BASE_CSS`)는 `var(--vl-*)`로만 색을 부르고, 그라디오 테마는
`GRADIO_THEME_COLORS`로 같은 표에서 `*_dark` 짝을 얻는다. 테마는 OS 설정을 따른다 —
그라디오가 `body`에 `.dark`를 달면 아래 `DARK_SELECTOR` 블록이 이긴다(강제하지 않는다).

이름에 `--vl-` 머리를 붙인다: 디자인 원본(tokens.json)의 맨 이름 중 `--accent`·`--success`·
`--danger`·`--radius-*`·`--font-mono`는 그라디오 번들이 이미 쓰는 변수다.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

FONT_ROUTE = "/vl-fonts"
"""글꼴을 내는 앱 경로 — 그라디오 file= 경로가 아니라 고정 목록 라우트다(allowed_paths 없음)."""

FONT_DIR = Path(__file__).resolve().parent / "static" / "fonts"


@dataclass(frozen=True)
class FontFace:
    family: str
    weight: int
    file_name: str


# 묶어 싣는 굵기만 — 본문 400·라벨 500·제목 600, 시간·ID는 Mono 400·500(디자인 README Type).
FONT_FACES: tuple[FontFace, ...] = (
    FontFace("IBM Plex Sans KR", 400, "IBMPlexSansKR-Regular.woff2"),
    FontFace("IBM Plex Sans KR", 500, "IBMPlexSansKR-Medium.woff2"),
    FontFace("IBM Plex Sans KR", 600, "IBMPlexSansKR-SemiBold.woff2"),
    FontFace("IBM Plex Mono", 400, "IBMPlexMono-Regular.woff2"),
    FontFace("IBM Plex Mono", 500, "IBMPlexMono-Medium.woff2"),
)
FONT_SANS_STACK = (
    '"IBM Plex Sans KR"',
    '"Apple SD Gothic Neo"',
    '"Noto Sans KR"',
    "system-ui",
    "sans-serif",
)
FONT_MONO_STACK = ('"IBM Plex Mono"', "ui-monospace", '"SF Mono"', "Menlo", "monospace")

# (밝은, 어두운) — 값은 디자인 시스템 tokens.json(2026-10-09) 그대로. 대비는
# tests/raw/test_workbench_theme.py가 두 테마 모두 4.5:1(표지 3:1)로 잰다.
COLOR_TOKENS: dict[str, tuple[str, str]] = {
    "bg": ("#eef0f2", "#0f1113"),
    "surface": ("#f8f9fa", "#16191c"),
    "surface-raised": ("#ffffff", "#1f2328"),
    "surface-hover": ("#eef1f4", "#272c32"),
    "surface-sunken": ("#e3e6e9", "#0a0b0d"),
    "video-well": ("#050607", "#050607"),
    "line": ("#d5d9de", "#2c3137"),
    "line-strong": ("#7b838d", "#707a86"),
    "ink": ("#15181c", "#e8ebee"),
    "ink-muted": ("#4d5560", "#a8b0b9"),
    "ink-faint": ("#6b737d", "#858d97"),
    # 어두움 짝은 Final Cut 보라 쪽이다(W-90, Final Cut Pro 12.4 `FinalCutPurple` #706BFF).
    # #706BFF 위 흰 글자는 4.01:1이라 채움은 한 단 짙은 #645FF0(4.73:1) · 글자·선은 밝은 #A9A6FF다.
    "accent": ("#2f6db3", "#645ff0"),
    "accent-hover": ("#27599a", "#5852e0"),
    "on-accent": ("#ffffff", "#ffffff"),
    "accent-ink": ("#245a96", "#a9a6ff"),
    "accent-soft": ("#dde9f7", "#25234a"),
    "tier-green": ("#4a9e72", "#86dcaa"),
    "on-tier-green": ("#0b2416", "#0c2a19"),
    "tier-green-ink": ("#1d6b43", "#8fe2b2"),
    "tier-yellow": ("#f0cf63", "#c99a2e"),
    "on-tier-yellow": ("#2a1f00", "#1f1500"),
    "tier-yellow-ink": ("#7a5900", "#e2b850"),
    "tier-red": ("#9a2f27", "#7c2b2e"),
    "tier-red-deep": ("#7d231d", "#561d20"),
    "on-tier-red": ("#ffffff", "#ffe4e0"),
    "tier-red-ink": ("#a3281e", "#ff9d90"),
    # 밝은 쪽만 tokens.json(#1d7a48)보다 한 단 어둡다 — 선택 카드(accent-soft) 위 4.34:1이었다.
    "success": ("#1b7444", "#74d69c"),
    "success-soft": ("#e3f3e9", "#15301f"),
    "warn": ("#8a5a00", "#f0b65a"),
    "warn-soft": ("#fbf0d9", "#33280f"),
    "danger": ("#b42318", "#ff8f80"),
    "danger-soft": ("#fbe9e7", "#3a1d1d"),
    "draft": ("#b4600f", "#e7913a"),
    "playhead": ("#121417", "#f4f6f8"),
    "playhead-casing": ("#ffffff", "#0a0b0d"),
    # 재생기 클립 띠의 클립 — 원본이 바뀔 때마다 두 채움을 번갈아(원본 경계가 보이게) 쓴다. 예전
    # accent-soft · surface-raised는 서로 1.07:1 · 바닥 위 약 1.3:1이라 클립이 갈리지 않았다
    # (디자인 점검 2026-11-19). 어두운 값은 점검이 제안한 #34507a · #3d4a7a 그대로다.
    "clip-fill": ("#8fa8cc", "#34507a"),
    "clip-fill-alt": ("#9fa2d6", "#3d4a7a"),
    "caption-chip": ("#d9e5f3", "#273a55"),
    "on-caption-chip": ("#1d3657", "#d8e6f7"),
    # 영상 위에 얹는 띠·글자 — 영상은 두 테마 모두 어둡게 보이므로 값이 같다.
    "video-overlay": ("#000000c7", "#000000c7"),
    "on-video-overlay": ("#ffffff", "#ffffff"),
}
EFFECT_TOKENS: dict[str, tuple[str, str]] = {
    "shadow-pop": ("0 8px 24px #15181c29", "0 8px 24px #000000b3"),
}
SCALAR_TOKENS: dict[str, str] = {
    "space-1": "4px",
    "space-2": "8px",
    "space-3": "12px",
    "space-4": "16px",
    "radius-xs": "2px",
    "radius-sm": "4px",
    "radius-md": "6px",
    # 껍데기 치수(W-17 B, 디자인 tokens.json `layout`) — 머리줄 · 명령 줄 · 상태 줄 · 조작 높이.
    "topbar-h": "44px",
    "commandbar-h": "48px",
    "statusline-h": "24px",
    "control-h": "32px",
    # Final Cut식 창(W-85, D145) — Final Cut Pro 12.4 `Factory 1 - Default.moduleLayout`의 위 덱
    # 브라우저 539 · 뷰어 539 · 인스펙터 403과 위·아래 덱 0.5/0.5를 비율로 옮겼다(정적 분석,
    # docs/plans/2026-11-16-fcp-window.md). 최소 폭은 예전 목록·패널 열의 바닥 그대로다.
    "browser-fr": "539fr",
    "viewer-fr": "539fr",
    "inspector-fr": "403fr",
    "browser-min": "240px",
    "inspector-min": "280px",
    # 뷰어 칸의 최소 폭 · 위·아래 덱의 최소 높이 — 트랙 손잡이가 이보다 좁히지 않는다(W-86d).
    "viewer-min": "320px",
    "deck-min": "160px",
    # 타임라인 인덱스 칸의 폭 — Final Cut `timelineIndexContents` 318.
    "index-w": "318px",
    # 「자막」 작업공간의 파형 레인 높이(W-87c) — 기본 96px의 두 배. 외부 근거 없는 조정 대상.
    "caption-lane-h": "192px",
    "browser-card-min": "240px",
    # 아래 덱이 차 있으면 무대는 내용 높이다 — 위 덱 뷰어의 영상 높이(W-85, 좁은 화면 규칙과 같은
    # 값). 위·아래 0.5/0.5는 덱이 둘째 영상을 내려놓는 W-86에서 맞춘다(지금 덱의 재생기는 제 영상을
    # 싣고 있어 크다).
    "deck-viewer-h": "clamp(300px, 46vh, 540px)",
    # 「화면 맞춤」(W-86c)을 켜면 위 덱(재생기 줄)과 아래 덱이 남는 높이를 이 비율로 나눈다 —
    # Final Cut 기본 작업공간의 0.5/0.5.
    "upper-deck-fr": "1fr",
    "lower-deck-fr": "1fr",
    "font-sans": ", ".join(FONT_SANS_STACK),
    "font-mono": ", ".join(FONT_MONO_STACK),
}
# 그라디오 테마와 같은 선택자 — `.dark`는 body에 붙고, `:has`는 문서 뿌리(스크롤바·캔버스)도
# 어둡게 한다. 밝은 값을 `.gradio-container`에 다시 선언하면 body의 어두운 값이 가려진다.
DARK_SELECTOR = ":root.dark, :root .dark, :root:has(> body.dark)"

# 그라디오 테마 변수 → 토큰. 옛 Soft 테마가 새던 자리(연보라 이름표·흰 드롭다운·안 보이는
# 인라인 코드·흰 hover·남색 탭)를 전부 여기서 덮는다.
GRADIO_THEME_COLORS: dict[str, str] = {
    "body_background_fill": "bg",
    "body_text_color": "ink",
    "body_text_color_subdued": "ink-muted",
    "background_fill_primary": "surface",
    "background_fill_secondary": "bg",
    "border_color_primary": "line",
    "border_color_accent": "accent-ink",
    "border_color_accent_subdued": "accent-soft",
    # 그라디오는 --color-accent를 주로 글자·선(선택한 탭 글자, 초점선, 로더)에 쓴다 — 본점 채움
    # #2f6db3은 어두운 바탕에서 글자로 3.4:1이라 accent-ink다. 채움은 아래에서 따로 accent를 준다.
    "color_accent": "accent-ink",
    "color_accent_soft": "accent-soft",
    "link_text_color": "accent-ink",
    "link_text_color_active": "accent-ink",
    "link_text_color_hover": "accent-ink",
    "link_text_color_visited": "accent-ink",
    "code_background_fill": "surface-sunken",
    "block_background_fill": "surface",
    "block_border_color": "line",
    "block_info_text_color": "ink-muted",
    "block_label_background_fill": "surface-raised",
    "block_label_border_color": "line",
    "block_label_text_color": "ink-muted",
    "block_title_text_color": "ink",
    "panel_background_fill": "surface",
    "panel_border_color": "line",
    "accordion_text_color": "ink",
    "table_text_color": "ink",
    "table_border_color": "line",
    "table_even_background_fill": "surface",
    "table_odd_background_fill": "surface-raised",
    "table_row_focus": "accent-soft",
    "checkbox_background_color": "surface-raised",
    "checkbox_background_color_focus": "surface-raised",
    "checkbox_background_color_hover": "surface-hover",
    "checkbox_background_color_selected": "accent",
    "checkbox_border_color": "line-strong",
    "checkbox_border_color_focus": "accent-ink",
    "checkbox_border_color_hover": "line-strong",
    "checkbox_border_color_selected": "accent",
    "checkbox_label_background_fill": "surface-raised",
    "checkbox_label_background_fill_hover": "surface-hover",
    "checkbox_label_background_fill_selected": "accent-soft",
    "checkbox_label_border_color": "line-strong",
    "checkbox_label_border_color_hover": "line-strong",
    "checkbox_label_border_color_selected": "accent-ink",
    "checkbox_label_text_color": "ink",
    "checkbox_label_text_color_selected": "ink",
    "error_background_fill": "danger-soft",
    "error_border_color": "danger",
    "error_text_color": "danger",
    "error_icon_color": "danger",
    "input_background_fill": "surface-raised",
    "input_background_fill_focus": "surface-raised",
    "input_background_fill_hover": "surface-raised",
    "input_border_color": "line-strong",
    "input_border_color_focus": "accent-ink",
    "input_border_color_hover": "line-strong",
    "input_placeholder_color": "ink-faint",
    "loader_color": "accent-ink",
    "slider_color": "accent",
    "stat_background_fill": "accent-soft",
    "button_primary_background_fill": "accent",
    "button_primary_background_fill_hover": "accent-hover",
    "button_primary_border_color": "accent",
    "button_primary_border_color_hover": "accent-hover",
    "button_primary_text_color": "on-accent",
    "button_primary_text_color_hover": "on-accent",
    "button_secondary_background_fill": "surface-raised",
    "button_secondary_background_fill_hover": "surface-hover",
    "button_secondary_border_color": "line-strong",
    "button_secondary_border_color_hover": "line-strong",
    "button_secondary_text_color": "ink",
    "button_secondary_text_color_hover": "ink",
    "button_cancel_background_fill": "surface-raised",
    "button_cancel_background_fill_hover": "surface-hover",
    "button_cancel_border_color": "danger",
    "button_cancel_border_color_hover": "danger",
    "button_cancel_text_color": "danger",
    "button_cancel_text_color_hover": "danger",
}
# 두 테마가 같은 값 — 둥글기(6px 넘지 않음)·평평한 패널·라벨 13px/500·움직임 없음.
GRADIO_THEME_SCALARS: dict[str, str] = {
    "block_radius": "6px",
    "container_radius": "6px",
    "embed_radius": "6px",
    "table_radius": "6px",
    "input_radius": "4px",
    "checkbox_border_radius": "2px",
    # 조작(입력칸·단추)은 line-strong 테두리로 땅과 갈린다 — Base 기본 0px이면 색이 안 보인다.
    "input_border_width": "1px",
    "button_border_width": "1px",
    "button_large_radius": "4px",
    "button_medium_radius": "4px",
    "button_small_radius": "4px",
    "button_large_text_size": "13px",
    "button_medium_text_size": "13px",
    "button_small_text_size": "12px",
    "button_large_text_weight": "500",
    "button_medium_text_weight": "500",
    "button_small_text_weight": "500",
    "button_large_padding": "6px 12px",
    "button_medium_padding": "5px 12px",
    "button_small_padding": "4px 10px",
    "button_transition": "none",
    "block_shadow": "none",
    "block_label_shadow": "none",
    "input_shadow": "none",
    "input_shadow_focus": "none",
    "checkbox_shadow": "none",
    "checkbox_label_shadow": "none",
    "button_primary_shadow": "none",
    "button_primary_shadow_hover": "none",
    "button_primary_shadow_active": "none",
    "button_secondary_shadow": "none",
    "button_secondary_shadow_hover": "none",
    "button_secondary_shadow_active": "none",
}
# 그라디오 중립 색조(c50…c950) — 테마 변수로 덮지 못한 구석도 같은 회색 계열에 머문다.
NEUTRAL_RAMP: tuple[tuple[str, int], ...] = (
    ("surface", 0),
    ("bg", 0),
    ("surface-sunken", 0),
    ("line", 0),
    ("ink-muted", 1),
    ("line-strong", 0),
    ("ink-muted", 0),
    ("line", 1),
    ("surface-raised", 1),
    ("surface", 1),
    ("bg", 1),
)


def css_var(name: str) -> str:
    return f"--vl-{name}"


def neutral_ramp() -> tuple[str, ...]:
    """밝은 쪽에서 어두운 쪽으로 — 두 테마의 회색 토큰을 한 줄로 잇는다."""
    return tuple(COLOR_TOKENS[name][side] for name, side in NEUTRAL_RAMP)


def gradio_theme_pairs() -> dict[str, tuple[str, str]]:
    """그라디오 변수 → (밝은, 어두운) 값. 어두운 짝을 받는 변수인지는 호출자가 가린다."""
    pairs = {name: COLOR_TOKENS[token] for name, token in GRADIO_THEME_COLORS.items()}
    pairs["shadow_drop"] = EFFECT_TOKENS["shadow-pop"]
    pairs["shadow_drop_lg"] = EFFECT_TOKENS["shadow-pop"]
    pairs.update({name: (value, value) for name, value in GRADIO_THEME_SCALARS.items()})
    return pairs


def font_path(file_name: str) -> Path | None:
    """허용 목록의 글꼴 파일만 — 사람이 보낸 이름은 목록과 글자 그대로 같을 때만 경로가 된다."""
    for face in FONT_FACES:
        if face.file_name == file_name:
            return FONT_DIR / face.file_name
    return None


def font_face_css() -> str:
    rules = [
        "@font-face {\n"
        f'  font-family: "{face.family}";\n'
        "  font-style: normal;\n"
        f"  font-weight: {face.weight};\n"
        "  font-display: swap;\n"
        f'  src: url("{FONT_ROUTE}/{face.file_name}") format("woff2");\n'
        "}\n"
        for face in FONT_FACES
    ]
    return "".join(rules)


def _declarations(values: dict[str, str]) -> str:
    return "".join(f"  {css_var(name)}: {value};\n" for name, value in values.items())


def token_css() -> str:
    """토큰 선언 — 밝은 값은 `:root`에 한 번, 어두운 값은 `DARK_SELECTOR`에 한 번."""
    paired = {**COLOR_TOKENS, **EFFECT_TOKENS}
    light = {name: values[0] for name, values in paired.items()}
    dark = {name: values[1] for name, values in paired.items()}
    return (
        ":root {\n  color-scheme: light;\n"
        + _declarations(light)
        + _declarations(SCALAR_TOKENS)
        + "}\n"
        + f"{DARK_SELECTOR} {{\n  color-scheme: dark;\n"
        + _declarations(dark)
        + "}\n"
    )
