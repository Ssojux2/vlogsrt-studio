"""W-17 A단계(D57) — 작업대의 시각 바탕: OS를 따르는 테마 · 두 벌의 색 토큰 · 앱이 내는 글꼴.

선언(CSS·테마 값)과 실제 HTTP 응답을 함께 잰다. 화면 배치(격자·카드·명령 줄)는 이 묶음이
다루지 않는다 — 그것은 B·C단계의 몫이고, 여기서는 색·글꼴·대비·초점·스크롤바·알림만 본다.
"""

from __future__ import annotations

import hashlib
import inspect
import re
from pathlib import Path

import pytest

pytest.importorskip(
    "gradio",
    reason="웹 UI extra(gradio)가 없다 — ui extra를 설치하면 이 묶음이 활성화된다",
    exc_type=ImportError,
)
pytest.importorskip(
    "fastapi",
    reason="웹 UI extra(fastapi)가 없다 — ui extra를 설치하면 이 묶음이 활성화된다",
    exc_type=ImportError,
)

import gradio as gr  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from ui import design_tokens, ui_facade, workspace_service  # noqa: E402

UI_DIR = Path(ui_facade.__file__).resolve().parent
PORT = 7860

CARD_GROUNDS = ("surface", "surface-raised", "surface-hover", "accent-soft")
# 글자·땅 짝 — 토큰 설명(usage)이 이름 붙인 땅 위에서 본문 4.5:1(D57, 디자인 README Contrast).
TEXT_ON_GROUNDS: dict[str, tuple[str, ...]] = {
    "ink": (
        "bg",
        "surface",
        "surface-raised",
        "surface-hover",
        "surface-sunken",
        "accent-soft",
        "success-soft",
        "warn-soft",
        "danger-soft",
    ),
    "ink-muted": ("bg", "surface", "surface-raised", "surface-hover", "surface-sunken"),
    "ink-faint": ("surface", "surface-raised"),
    "accent-ink": ("bg", "surface", "surface-raised", "accent-soft"),
    "on-accent": ("accent", "accent-hover"),
    "on-tier-green": ("tier-green",),
    "on-tier-yellow": ("tier-yellow",),
    "on-tier-red": ("tier-red", "tier-red-deep"),
    # 배지·표지 글자는 선택한 카드(accent-soft)와 hover 카드(surface-hover) 위에도 놓인다.
    "tier-green-ink": (*CARD_GROUNDS, "bg", "surface-sunken"),
    "tier-yellow-ink": (*CARD_GROUNDS, "bg", "surface-sunken"),
    "tier-red-ink": (*CARD_GROUNDS, "bg", "surface-sunken"),
    "success": (*CARD_GROUNDS, "success-soft"),
    "warn": (*CARD_GROUNDS, "warn-soft"),
    "danger": (*CARD_GROUNDS, "danger-soft"),
    "on-caption-chip": ("caption-chip",),
}
# 글자가 아닌 표지(조작 테두리·초점 고리·렠 위 선) — 3:1.
MARK_ON_GROUNDS: dict[str, tuple[str, ...]] = {
    "line-strong": ("bg", "surface", "surface-raised"),
    # 자막 칩 위 「다른 귀」 표지(D59 19e) — 모서리 삼각형 warn · 아래 선 accent-ink.
    "accent-ink": ("surface-sunken", "caption-chip"),
    "warn": ("caption-chip",),
    "playhead": ("surface-sunken",),
    "draft": ("surface-sunken",),
    "accent": ("surface-sunken",),
    "tier-yellow-ink": ("surface-sunken",),
}
LEGACY_LITERALS = ("#20b9ae", "#36d5c6", "#43caba", "#6366f1", "#4c6871", "#177e73", "#745638")
# 색 글자는 **값 자리**에서만 센다 — 속성 뒤(`color:#888`), HTML 속성 값(`fill="#4a7a96"`),
# 함수 인자(`gradient(135deg,#7a2d2d …)`). `#add {`·`querySelector('#fade')` 같은 아이디는 아니다.
CSS_SELECTOR = re.compile(r"[^{};\"']*\{")
COLOR_LITERAL = re.compile(
    r"(?:[\w-]+\s*:|=\s*[\"']?|\()\s*[^;{}\"']*?(?<![\w&])"
    r"#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3,4})\b"
    r"|\b(?:rgba?|hsla?)\(|:\s*(?:white|black)\b"
)


def _colour_literal(text: str) -> re.Match[str] | None:
    """선택자(`… {`)를 걷어낸 뒤 값 자리의 색 글자를 찾는다."""
    return COLOR_LITERAL.search(CSS_SELECTOR.sub("", text))


def _luminance(hex_colour: str) -> float:
    digits = hex_colour.lstrip("#")[:6]
    channels = [int(digits[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(first: str, second: str) -> float:
    high, low = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def _rules(css: str) -> list[tuple[str, str]]:
    """가장 안쪽 규칙의 (선택자, 본문) — @media 안의 규칙도 제 선택자로 나온다."""
    flat = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    return [(sel.strip(), body) for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", flat)]


def _block(css: str, selector: str) -> str:
    bodies = [body for sel, body in _rules(css) if sel == selector]
    assert len(bodies) == 1, (selector, len(bodies))
    return bodies[0]


def _authored_css() -> str:
    """토큰 선언을 뺀, 사람이 쓴 규칙 전부 — 색은 여기서 토큰으로만 나온다."""
    return workspace_service.BASE_CSS + ui_facade.WORKBENCH_CSS


# ── 테마 — OS를 따르고, 토큰 두 벌로 지었다 ──────────────────────────────────


def test_gradio_theme_is_built_from_the_tokens_with_dark_pairs() -> None:
    theme = workspace_service.workbench_theme()
    assert isinstance(theme, gr.themes.Base) and not isinstance(theme, gr.themes.Soft)
    css = theme._get_theme_css()
    light, dark = css.split(":root.dark, :root .dark {", 1)
    tokens = design_tokens.COLOR_TOKENS
    assert f"--body-background-fill: {tokens['bg'][0]};" in light
    assert f"--body-background-fill: {tokens['bg'][1]};" in dark
    assert f"--body-text-color: {tokens['ink'][0]};" in light
    assert f"--body-text-color: {tokens['ink'][1]};" in dark
    assert f"--input-background-fill: {tokens['surface-raised'][1]};" in dark
    assert f"--code-background-fill: {tokens['surface-sunken'][1]};" in dark
    for half in (light, dark):
        assert "--button-primary-background-fill: #2f6db3;" in half  # 본점 하나, 두 테마 같다
        assert "--checkbox-background-color-selected: #2f6db3;" in half
    # 그라디오의 --color-accent는 글자·선(선택한 탭 글자 등)이다 — accent-ink(4.5:1)로 간다.
    assert f"--color-accent: {tokens['accent-ink'][0]};" in light
    assert f"--color-accent: {tokens['accent-ink'][1]};" in dark
    # 옛 Soft 테마의 남색·연보라 이름표·흰 입력칸이 남지 않는다.
    assert "#6366f1" not in css and "#e0e7ff" not in css
    # 글꼴은 사람이 고른 묶음 글꼴 → 애플 고딕 → 시스템. 테마는 글꼴표를 따로 싣지 않는다.
    assert theme.font.startswith('"IBM Plex Sans KR", "Apple SD Gothic Neo"')
    assert theme.font_mono.startswith('"IBM Plex Mono"')
    assert theme._stylesheets == [] and theme._font_css == []
    assert "googleapis" not in css and "@import" not in css


def test_every_mapped_theme_value_has_a_dark_twin() -> None:
    """빛·어둠 짝이 있는 그라디오 변수는 둘 다 토큰 값으로 채운다 — 기본값이 새지 않는다."""
    theme = workspace_service.workbench_theme()
    css = theme._get_theme_css()
    light_css, dark_css = css.split(":root.dark, :root .dark {", 1)
    accepted = set(inspect.signature(gr.themes.Base.set).parameters)
    for name, token in design_tokens.GRADIO_THEME_COLORS.items():
        light, dark = design_tokens.COLOR_TOKENS[token]
        assert getattr(theme, name) == light, name
        # `set()`이 어두운 인자를 받지 않는 변수(color_accent)도 어두운 블록에는 어두운 값이다.
        assert getattr(theme, f"{name}_dark", light) == dark, name
        prop = "--" + name.replace("_", "-")
        assert f"{prop}: {light};" in light_css, name
        assert f"{prop}: {dark};" in dark_css, name
    assert "color_accent_dark" not in accepted  # 위 우회의 이유 — 그라디오가 바뀌면 다시 본다


def test_nothing_forces_the_dark_theme() -> None:
    """D57 결정 1 — 테마는 OS 설정을 따른다(그라디오 system 모드). `.dark` 강제는 없다."""
    for path in sorted(UI_DIR.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        assert "__theme" not in text, path
        assert not re.search(r"classList\.(add|toggle)\(\s*['\"]dark", text), path
        assert "theme_mode" not in text, path
    light_block = _block(design_tokens.token_css(), ":root")
    assert "color-scheme: light;" in light_block and "color-scheme: dark" not in light_block


def test_mount_passes_the_workbench_theme_and_css(
    tmp_path: Path, local_socket: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def fake_mount(app: object, blocks: object, **kwargs: object) -> object:
        captured.update(kwargs)
        return app

    monkeypatch.setattr(workspace_service.gr, "mount_gradio_app", fake_mount)
    server = workspace_service.RawWorkspaceUi(port=PORT, state_path=tmp_path / "state.json")
    server.build_fastapi(mount_blocks=True)
    theme = captured["theme"]
    assert isinstance(theme, gr.themes.Base) and not isinstance(theme, gr.themes.Soft)
    assert theme.body_background_fill == design_tokens.COLOR_TOKENS["bg"][0]
    assert captured["css"] == workspace_service.CSS
    assert captured["allowed_paths"] is None  # 글꼴은 file= 경로가 아니라 고정 목록 라우트로 나간다


# ── 토큰 — 밝은 쪽 한 번, 어두운 쪽 한 번 ──────────────────────────────────────


def test_tokens_are_declared_once_for_light_and_once_for_dark() -> None:
    css = design_tokens.token_css()
    light = _block(css, ":root")
    dark = _block(css, design_tokens.DARK_SELECTOR)
    assert ".dark" in design_tokens.DARK_SELECTOR  # 그라디오가 body에 다는 표지
    assert "color-scheme: dark;" in dark
    paired = {**design_tokens.COLOR_TOKENS, **design_tokens.EFFECT_TOKENS}
    for name, (light_value, dark_value) in paired.items():
        prop = design_tokens.css_var(name)
        assert light.count(f"{prop}:") == 1 and f"{prop}: {light_value};" in light, name
        assert dark.count(f"{prop}:") == 1 and f"{prop}: {dark_value};" in dark, name
    for name, value in design_tokens.SCALAR_TOKENS.items():
        prop = design_tokens.css_var(name)
        assert f"{prop}: {value};" in light and prop not in dark, name
    # 영상은 두 테마 모두 어두운 우물 위에 놓인다. 본점은 두 테마가 같다.
    assert design_tokens.COLOR_TOKENS["video-well"] == ("#050607", "#050607")
    assert design_tokens.COLOR_TOKENS["accent"] == ("#2f6db3", "#2f6db3")
    for light_value, dark_value in design_tokens.COLOR_TOKENS.values():
        assert re.fullmatch(r"#[0-9a-f]{6}(?:[0-9a-f]{2})?", light_value), light_value
        assert re.fullmatch(r"#[0-9a-f]{6}(?:[0-9a-f]{2})?", dark_value), dark_value


@pytest.mark.parametrize("theme_index", [0, 1], ids=["light", "dark"])
def test_token_pairs_hold_contrast(theme_index: int) -> None:
    tokens = {name: values[theme_index] for name, values in design_tokens.COLOR_TOKENS.items()}
    for ink, grounds in TEXT_ON_GROUNDS.items():
        for ground in grounds:
            ratio = _contrast(tokens[ink], tokens[ground])
            assert ratio >= 4.5, (ink, ground, round(ratio, 2))
    for mark, grounds in MARK_ON_GROUNDS.items():
        for ground in grounds:
            ratio = _contrast(tokens[mark], tokens[ground])
            assert ratio >= 3.0, (mark, ground, round(ratio, 2))
    # 재생 머리는 양옆 테두리(casing)와 함께 모든 등급 막대 위에서 읽힌다.
    for fill in ("tier-green", "tier-yellow", "tier-red", "tier-red-deep", "surface-sunken"):
        best = max(
            _contrast(tokens["playhead"], tokens[fill]),
            _contrast(tokens["playhead-casing"], tokens[fill]),
        )
        assert best >= 3.0, fill
    # 흰 자막은 영상 위 어두운 띠에 — 띠가 흰 화면 위에 놓여도 4.5:1(알파 합성의 최악).
    overlay = tokens["video-overlay"]
    alpha = int(overlay[7:9], 16) / 255
    worst = "#" + "".join(f"{round(255 * (1 - alpha)):02x}" for _ in range(3))
    assert _contrast(tokens["on-video-overlay"], worst) >= 4.5


def test_tier_fills_sit_at_three_lightness_levels() -> None:
    """색만으로 가르지 않는다 — 흑백 인쇄에서도 세 등급의 밝기가 갈린다(디자인 README Tiers)."""
    light = {k: _luminance(v[0]) for k, v in design_tokens.COLOR_TOKENS.items()}
    dark = {k: _luminance(v[1]) for k, v in design_tokens.COLOR_TOKENS.items()}
    assert dark["tier-green"] > dark["tier-yellow"] > dark["tier-red"]
    assert light["tier-yellow"] > light["tier-green"] > light["tier-red"]


# ── 사람이 쓴 CSS — 색은 토큰으로만 ───────────────────────────────────────────


def test_colour_literal_detector_reads_values_not_ids() -> None:
    """탐지기 자체 — 값 자리의 색은 잡고, 16진 글자로만 된 아이디 선택자는 놓아준다."""
    for flagged in (
        "color:#888",
        '<p style="color:#667; font-size:12px;">',
        ' fill="#4a7a96"></rect>',
        "background:repeating-linear-gradient(135deg,#7a2d2d 0 6px,#561c1c 6px 12px)",
        ".x { border: 1px solid #fff; }",
        "box-shadow: 0 0 0 1px rgba(0, 0, 0, .5);",
        ".a{color:white}",
    ):
        assert _colour_literal(flagged), flagged
    for clean in (
        "#add { display: none; }",
        ".x #fade:hover, #bad > .y {",
        "doc.querySelector('#fade')",
        'getElementById("add")',
        "color: var(--vl-ink); background: var(--vl-bg);",
    ):
        assert not _colour_literal(clean), clean


def test_authored_css_and_markup_carry_no_literal_colours() -> None:
    for selector, body in _rules(_authored_css()):
        assert not _colour_literal(body), (selector, body.strip()[:120])
    for literal in LEGACY_LITERALS:
        assert literal not in workspace_service.CSS, literal
    # 파이썬이 짓는 HTML의 인라인 style도 같다 — 색 글자는 design_tokens.py 한 곳에만 산다.
    for path in sorted(UI_DIR.rglob("*.py")):
        if path.name == "design_tokens.py":
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            assert not _colour_literal(line), f"{path.name}:{number}: {line.strip()[:100]}"


def test_every_token_reference_resolves() -> None:
    """CSS가 부르는 `--vl-*`는 전부 어딘가에 선언돼 있다 — 오타는 조용히 기본색이 된다."""
    css = workspace_service.CSS
    declared = set(re.findall(r"(--vl-[\w-]+)\s*:", css))
    for reference in set(re.findall(r"var\((--vl-[\w-]+)", css)):
        assert reference in declared, reference


def test_tier_marks_follow_the_design() -> None:
    css = _authored_css()
    green = _block(css, ".vl-recommendation.green")
    assert "background: var(--vl-tier-green);" in green
    assert "color: var(--vl-on-tier-green);" in green
    assert "var(--vl-tier-green-ink)" in green
    yellow = _block(css, ".vl-recommendation.yellow")
    assert "background: var(--vl-tier-yellow);" in yellow
    assert "color: var(--vl-on-tier-yellow);" in yellow
    red = _block(css, ".vl-recommendation.red")
    assert (
        "repeating-linear-gradient(135deg, var(--vl-tier-red) 0 6px, "
        "var(--vl-tier-red-deep) 6px 12px)" in red
    )
    assert "color: var(--vl-on-tier-red);" in red
    assert "inset 0 0 0 1px var(--vl-tier-red-ink)" in red  # 빗금 + 단단한 가장자리
    hold = _block(css, ".vl-recommendation.yellow.hold")
    assert "background: transparent;" in hold  # 속이 빈 막대
    # Chromium은 1.5px 점선을 1px로 그린다 — 2px이라야 점선이 보인다(검증 실측).
    assert "border: 2px dotted var(--vl-tier-yellow-ink);" in hold
    assert "color: var(--vl-tier-yellow-ink);" in hold


def test_tier_legend_glyphs_and_legacy_marks() -> None:
    """범례 글자 색은 등급 ink, 글자 표지는 색과 별개로 등급을 말한다. 옛 청록·주황 표기는 없다."""
    css = _authored_css()
    for tier, token in (
        ("green", "tier-green-ink"),
        ("yellow", "tier-yellow-ink"),
        ("red", "tier-red-ink"),
        ("hold", "tier-yellow-ink"),
    ):
        assert f"color: var(--vl-{token});" in _block(css, f".vl-tier-key.{tier}"), tier
    js = ui_facade.VISUAL_BOOTSTRAP_JS
    for glyph in ("★ 추천", "✕ 비추천", "? 보류"):  # 빨강의 화면 이름(W-46a)
        assert glyph in js, glyph
    selectors = {sel for sel, _ in _rules(css)}
    for legacy in (
        ".vl-good",
        ".vl-review",
        ".vl-recommendation.recommended",
        ".vl-recommendation.review_required",
    ):
        assert legacy not in selectors, legacy
    assert ".vl-good" not in css and ".vl-review" not in css


def test_accent_fill_is_reserved_for_primary_handles_and_selected_cut() -> None:
    """본점(#2f6db3)은 채움 셋에만 — 글자·선은 accent-ink다(디자인 README Colour)."""
    filled = {
        sel for sel, body in _rules(_authored_css()) if re.search(r"var\(--vl-accent\)", body)
    }
    allowed = {".vl-btn-primary", ".vl-handle", ".vl-int-sel"}
    assert filled and filled <= allowed, filled - allowed
    assert {".vl-btn-primary", ".vl-handle", ".vl-int-sel"} <= filled


def test_rail_layers_use_the_design_coordinates() -> None:
    body = _block(ui_facade.WORKBENCH_CSS, ".vl-rail")
    coords = {k: int(v) for k, v in re.findall(r"--vl-([\w-]+):\s*(\d+)px", body)}
    assert coords["rail-h"] == 150 and coords["ruler-h"] == 16
    assert (coords["rec-top"], coords["rec-h"]) == (20, 22)
    assert (coords["int-top"], coords["int-h"]) == (46, 36)
    assert (coords["audio-top"], coords["audio-h"]) == (86, 20)
    assert (coords["cue-top"], coords["cue-h"]) == (110, 22)
    stack = [("ruler", 0), ("rec", coords["rec-top"]), ("int", coords["int-top"])]
    stack += [("audio", coords["audio-top"]), ("cue", coords["cue-top"])]
    for (lower, top), (_, next_top) in zip(stack, stack[1:], strict=False):
        assert top + coords[f"{lower}-h"] <= next_top, lower
    assert coords["cue-top"] + coords["cue-h"] <= coords["rail-h"]
    # 손잡이는 선택한 컷의 띠 안에 선다(눈금자 위가 아니다).
    handle = _block(ui_facade.WORKBENCH_CSS, ".vl-handle")
    assert "top: calc(var(--vl-int-top) + (var(--vl-int-h) - var(--vl-handle-h)) / 2);" in handle
    assert coords["handle-h"] <= coords["int-h"]


def test_focus_ring_scrollbars_and_toasts_use_tokens() -> None:
    css = _authored_css()
    focus = _block(css, ".gradio-container :focus-visible")
    assert "outline: 2px solid var(--vl-accent-ink);" in focus
    assert "outline-offset: 2px;" in focus
    assert "scrollbar-color: var(--vl-line-strong) var(--vl-surface);" in _block(
        css, "body, .gradio-container"
    )
    toast = _block(css, ".gradio-container .toast-body")
    assert "background: var(--vl-surface-raised) !important;" in toast
    assert "box-shadow: var(--vl-shadow-pop) !important;" in toast
    assert "color: var(--vl-ink) !important;" in toast
    for kind, token in (
        ("error", "danger"),
        ("warning", "warn"),
        ("success", "success"),
        ("info", "accent-ink"),
    ):
        assert f"--toast-color: var(--vl-{token})" in _block(
            css, f".gradio-container .toast-body.{kind}"
        ), kind
    # 영상은 두 테마 모두 어두운 우물 위에(미디어 쿼리는 높이만 바꾼다 — 첫 선언이 바탕을 정한다).
    video_rules = [b for s, b in _rules(ui_facade.WORKBENCH_CSS) if s == "#visual-preview video"]
    assert "background: var(--vl-video-well);" in video_rules[0]


def test_gradio_prose_lines_and_the_type_floor_follow_the_theme() -> None:
    """그라디오 prose의 #e1e1e1 줄은 토큰 선으로, 글자는 11px 아래로 내려가지 않는다."""
    css = _authored_css()
    hr = _block(css, "body .gradio-container .prose hr")
    assert "border-top-color: var(--vl-line);" in hr
    cells = _block(css, "body .gradio-container .prose th, body .gradio-container .prose td")
    assert "border-bottom-color: var(--vl-line);" in cells
    assert "font-size: 11px;" in _block(css, ".vl-kv small")  # <small>의 smaller는 9.6px였다
    sizes = [int(px) for px in re.findall(r"font(?:-size)?:[^;{}]*?\b(\d+)px", css)]
    assert sizes and min(sizes) >= 11, sorted(set(sizes))


# ── 글꼴 — 묶어 싣고, 앱이 직접 낸다 ──────────────────────────────────────────


def test_font_faces_point_at_bundled_files() -> None:
    faces = re.findall(r"@font-face\s*\{([^}]*)\}", workspace_service.CSS)
    assert len(faces) == len(design_tokens.FONT_FACES) == 5
    weights = {(face.family, face.weight) for face in design_tokens.FONT_FACES}
    assert weights == {
        ("IBM Plex Sans KR", 400),
        ("IBM Plex Sans KR", 500),
        ("IBM Plex Sans KR", 600),
        ("IBM Plex Mono", 400),
        ("IBM Plex Mono", 500),
    }
    readme = (design_tokens.FONT_DIR / "README.md").read_text(encoding="utf-8")
    for face, body in zip(design_tokens.FONT_FACES, faces, strict=True):
        assert f'font-family: "{face.family}";' in body
        assert f"font-weight: {face.weight};" in body
        assert "font-display: swap;" in body
        url = f"{design_tokens.FONT_ROUTE}/{face.file_name}"
        assert f'src: url("{url}") format("woff2");' in body
        payload = (design_tokens.FONT_DIR / face.file_name).read_bytes()
        assert payload[:4] == b"wOF2", face.file_name
        assert hashlib.sha256(payload).hexdigest() in readme, face.file_name
    assert "http" not in "".join(faces)  # 실행 중에 CDN을 부르지 않는다
    license_text = (design_tokens.FONT_DIR / "OFL.txt").read_text(encoding="utf-8")
    assert "SIL Open Font License" in license_text


def test_the_app_serves_only_the_bundled_fonts(tmp_path: Path, local_socket: None) -> None:
    server = workspace_service.RawWorkspaceUi(port=PORT, state_path=tmp_path / "state.json")
    client = TestClient(server.build_fastapi(mount_blocks=True), base_url="http://127.0.0.1")
    for face in design_tokens.FONT_FACES:
        response = client.get(f"{design_tokens.FONT_ROUTE}/{face.file_name}")
        assert response.status_code == 200, face.file_name
        assert response.headers["content-type"] == "font/woff2"
        assert response.content == (design_tokens.FONT_DIR / face.file_name).read_bytes()
        # HEAD도 미디어 라우트처럼 답한다 — 몸 없이 같은 머리.
        head = client.head(f"{design_tokens.FONT_ROUTE}/{face.file_name}")
        assert head.status_code == 200 and head.content == b"", face.file_name
        assert head.headers["content-length"] == str(len(response.content))
    for name in ("README.md", "OFL.txt", "nope.woff2", "..%2Fdesign_tokens.py"):
        assert client.get(f"{design_tokens.FONT_ROUTE}/{name}").status_code == 404, name
        assert client.head(f"{design_tokens.FONT_ROUTE}/{name}").status_code == 404, name
    foreign = client.get(
        f"{design_tokens.FONT_ROUTE}/{design_tokens.FONT_FACES[0].file_name}",
        headers={"Host": "evil.example.com"},
    )
    assert foreign.status_code == 403  # 글꼴도 같은 Host 가드를 지난다
