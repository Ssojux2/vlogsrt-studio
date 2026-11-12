"""자막 입히기(번인, W-65 D116)의 설정 — 순수 조판(`burn_in`)과 입출력(`burn_export`)이 읽는다.

**모든 값이 외부 근거 없는 조정 대상이다**(규칙 8). 글자 크기·외곽선·여백은 1080p 기준 픽셀로 적고
영상 높이에 비례해 늘리거나 줄인다 — 영화 자막처럼 화면 높이의 약 1/20 글자, 4px 검은 외곽선,
아래 여백 60px가 기본이다. 영어 줄은 한글 줄의 4/5 크기로 그 아래에 쌓는다(사용자 선택 2026-11-12).
글꼴은 강의자료가 쓰는 Pretendard의 SemiBold 한 벌을 꾸러미에 싣는다(SIL OFL 1.1 — `data/fonts/`).
어느 지문·캐시 열쇠·승인·견적·등급에도 없다 — 바꾸면 다음 번인만 달라진다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from fractions import Fraction

BURN_POSITIONS = ("bottom", "top")
"""자막 자리 — 아래(영화 자막) · 위. ASS 정렬 2·8로 옮긴다."""

HEX_COLOR = re.compile(r"#[0-9A-Fa-f]{6}")
"""`#RRGGBB` — 화면의 색 고르기가 내는 꼴."""


@dataclass(frozen=True)
class BurnSettings:
    """번인 설정 — `*_px`는 `reference_height`(1080p) 기준 픽셀이다.

    - `font_file`·`font_name`: 꾸러미 `vlogsrt/project/raw/data/fonts/`의 파일과 libass가 찾는
      전체 이름. 실측 libass 0.17.5가 `fontselect: (Pretendard SemiBold, 400, 0) ->
      Pretendard-SemiBold`로 그 파일을 골랐다.
    - `default_*`: 작업공간에 저장한 모양이 없을 때의 위치·크기·외곽선·여백·글자색.
    - `*_range`: 화면 손잡이와 저장값 검사의 (최소, 최대) — 범위 밖은 거부한다.
    - `english_scale`: 영어 줄 글자·외곽선 크기 = 한글 × 이 값. `side_margin_ratio`: 좌우 여백 =
      영상 너비 × 이 값(긴 줄은 그 안에서 libass가 고르게 접는다, WrapStyle 0).
    - `outline_color`: 외곽선 색은 고정이다(검정) — 사람이 고르는 것은 글자색뿐이다.
    - `preview_long_edge`·`preview_jpeg_quality`·`preview_timeout_seconds`: 미리보기 한 장의 긴 변,
      mjpeg `-q:v`(2–31, 작을수록 좋다), ffmpeg 한 번의 한도.
    - `ffmpeg_candidates`: PATH의 ffmpeg에 libass(`subtitles` 필터)가 없을 때 찾아볼 실행 파일 —
      Homebrew `ffmpeg-full`은 keg-only라 PATH에 없다. 분석·조사는 PATH ffmpeg 그대로다.
    - `output_infix`: `<이름>.burned.mp4`.
    - `fallback_folder`: 영상 옆에 쓸 수 없을 때 작업공간 안 자리.
    """

    font_file: str = "Pretendard-SemiBold.otf"
    font_name: str = "Pretendard SemiBold"
    reference_height: int = 1080
    default_position: str = "bottom"
    default_font_px: int = 54
    default_outline_px: int = 4
    default_margin_px: int = 60
    default_text_color: str = "#FFFFFF"
    outline_color: str = "#000000"
    font_px_range: tuple[int, int] = (20, 160)
    outline_px_range: tuple[int, int] = (0, 16)
    margin_px_range: tuple[int, int] = (0, 480)
    english_scale: Fraction = Fraction(4, 5)
    side_margin_ratio: Fraction = Fraction(1, 20)
    preview_long_edge: int = 960
    preview_jpeg_quality: int = 3
    preview_timeout_seconds: float = 120.0
    ffmpeg_candidates: tuple[str, ...] = (
        "/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg",
        "/usr/local/opt/ffmpeg-full/bin/ffmpeg",
    )
    output_infix: str = ".burned"
    fallback_folder: tuple[str, ...] = ("export", "burned")

    def __post_init__(self) -> None:
        problems = _range_problems(self) + _style_problems(self)
        if problems:
            raise ValueError("자막 입히기 설정이 잘못됐다 — " + " · ".join(problems))


def _range_problems(s: BurnSettings) -> list[str]:
    problems = []
    for name, (low, high), default in (
        ("font_px_range", s.font_px_range, s.default_font_px),
        ("outline_px_range", s.outline_px_range, s.default_outline_px),
        ("margin_px_range", s.margin_px_range, s.default_margin_px),
    ):
        if not 0 <= low <= default <= high:
            problems.append(f"{name} {low}–{high}에 기본값 {default}이 들어야 한다")
    if s.font_px_range[0] < 1:
        problems.append("글자 크기는 1px 이상이어야 한다")
    if s.reference_height < 1 or s.preview_long_edge < 2:
        problems.append("기준 높이·미리보기 긴 변은 양수여야 한다")
    if not 2 <= s.preview_jpeg_quality <= 31:
        problems.append(f"preview_jpeg_quality는 2–31이어야 한다: {s.preview_jpeg_quality}")
    return problems


def _style_problems(s: BurnSettings) -> list[str]:
    problems = []
    if s.default_position not in BURN_POSITIONS:
        problems.append(f"default_position은 {BURN_POSITIONS} 가운데 하나다")
    for name, color in (
        ("default_text_color", s.default_text_color),
        ("outline_color", s.outline_color),
    ):
        if not HEX_COLOR.fullmatch(color):
            problems.append(f"{name}은 #RRGGBB여야 한다: {color!r}")
    if not 0 < s.english_scale <= 1:
        problems.append("english_scale은 0보다 크고 1 이하여야 한다")
    if not 0 <= s.side_margin_ratio < Fraction(1, 2):
        problems.append("side_margin_ratio는 0 이상 1/2 미만이어야 한다")
    if not s.font_file.endswith((".otf", ".ttf")) or "/" in s.font_file:
        problems.append(f"font_file은 경로 없는 .otf·.ttf 이름이어야 한다: {s.font_file!r}")
    if not s.output_infix.startswith(".") or not s.fallback_folder:
        problems.append("output_infix는 점으로 시작하고 fallback_folder는 비지 않아야 한다")
    return problems


BURN_SETTINGS = BurnSettings()
"""지금 쓰는 자막 입히기 설정."""
