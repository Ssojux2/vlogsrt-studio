"""모든 임계값이 모이는 곳(불변 규칙 6).

우선순위: CLI 옵션 > `--preset` > `vlogsrt.toml` > 코드 기본값.
TOML은 읽기 편하라고 섹션을 나누지만, 키 이름은 전역에서 유일하다.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any

PROJECT_TOML = Path("vlogsrt.toml")
PRESET_DIRS = (Path("presets"), Path(__file__).resolve().parent.parent / "presets")


@dataclass(frozen=True, slots=True)
class Config:
    # ─── 전사 ────────────────────────────────────────────────
    language: str = "ko"
    extra_languages: tuple[str, ...] = ("ko", "en")
    prompt: str = ""
    keywords: tuple[str, ...] = ()
    # 세그먼트 시각을 주는 모델이어야 한다. 시각이 없으면 SRT를 만들 수 없다(D21).
    # 계정에 이 모델이 없으면 `whisper-1` + `verbose_json`으로 되돌릴 수 있다 —
    # 두 응답 모두 `segments[].{start,end,text}` 형태라 파서가 같다.
    transcribe_model: str = "gpt-4o-transcribe-diarize"
    transcribe_format: str = "diarized_json"
    api_max_retries: int = 4
    api_backoff_base: float = 2.0
    cost_per_minute: float = 0.006

    # ─── 오디오 전처리 ───────────────────────────────────────
    sample_rate: int = 16000
    channels: int = 1
    audio_bitrate: str = "64k"

    # ─── 청크 분할 ───────────────────────────────────────────
    chunk_max_bytes: int = 24_000_000
    # 크기 한도(24MB)만 보면 64kbps에서 50분이 한 조각에 들어간다. 그보다 짧게
    # 끊는 이유는 실패 시 재호출 비용과 경계 손실 사이의 절충이며, 외부 근거가
    # 있는 값이 아니다. (600초이던 시절의 근거였던 whisper-1 순차 디코딩 병리는
    # 모델과 함께 사라졌다 — D13, D21.)
    chunk_max_seconds: float = 1500.0

    # ─── 정제 ────────────────────────────────────────────────
    max_chars_per_line: int = 16
    max_lines: int = 2
    max_cps: float = 12.0
    min_duration: float = 0.85  # Netflix TTSG: 최소 노출 5/6초
    max_duration: float = 7.0
    min_gap: float = 0.08  # Netflix TTSG: 24fps 기준 2프레임
    merge_gap: float = 0.4
    pause_split: float = 0.35
    max_split_depth: int = 4
    lead_in: float = 0.1
    lead_out: float = 0.3
    particles: tuple[str, ...] = (
        "은", "는", "이", "가", "을", "를", "에", "의", "와", "과",
        "도", "만", "로", "으로", "에서", "에게", "한테", "부터", "까지", "라고",
        "이라고", "처럼", "보다", "마다", "조차", "밖에", "이나", "나", "든지", "요",
        "고", "며", "면", "지만", "는데", "인데", "해서", "하고",
    )  # fmt: skip

    # ─── 평가 ────────────────────────────────────────────────
    boundary_tol: float = 0.3
    start_tol: float = 0.2
    mae_match_tol: float = 2.0  # MAE 계산 시 같은 큐로 볼 최대 시차

    @property
    def max_chars_per_cue(self) -> int:
        return self.max_chars_per_line * self.max_lines

    def to_dict(self) -> dict[str, Any]:
        return {f.name: _plain(getattr(self, f.name)) for f in fields(self)}


def _plain(value: Any) -> Any:
    return list(value) if isinstance(value, tuple) else value


_FIELD_DEFAULTS: dict[str, Any] = {f.name: f.default for f in fields(Config)}


def _coerce(key: str, value: Any) -> Any:
    """TOML/CLI에서 온 값을 필드 기본값의 타입에 맞춘다."""
    default = _FIELD_DEFAULTS[key]
    if isinstance(default, tuple):
        if isinstance(value, str):
            return tuple(part.strip() for part in value.split(",") if part.strip())
        return tuple(str(item) for item in value)
    if isinstance(default, bool):
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)
    if isinstance(default, int):
        return int(value)
    if isinstance(default, float):
        return float(value)
    return str(value)


def flatten_toml(raw: dict[str, Any], source: str) -> dict[str, Any]:
    """한 단계 깊이의 섹션을 평평하게 편다. 모르는 키는 조용히 넘기지 않는다."""
    flat: dict[str, Any] = {}
    for key, value in raw.items():
        if isinstance(value, dict):
            for inner_key, inner_value in value.items():
                _put(flat, inner_key, inner_value, f"{source}[{key}]")
        else:
            _put(flat, key, value, source)
    return flat


def _put(flat: dict[str, Any], key: str, value: Any, source: str) -> None:
    if key not in _FIELD_DEFAULTS:
        raise RuntimeError(f"{source}: 알 수 없는 설정 키 {key!r}. Config에 없는 값이다.")
    flat[key] = _coerce(key, value)


def read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    except OSError as exc:
        raise RuntimeError(f"설정 파일을 읽지 못했습니다: {path} ({exc})") from exc
    except tomllib.TOMLDecodeError as exc:
        raise RuntimeError(f"TOML 문법 오류: {path} ({exc})") from exc
    return flatten_toml(raw, str(path))


def find_preset(name: str) -> Path:
    for directory in PRESET_DIRS:
        candidate = directory / f"{name}.toml"
        if candidate.is_file():
            return candidate
    searched = ", ".join(str(d) for d in PRESET_DIRS)
    raise RuntimeError(f"프리셋 {name!r}을 찾지 못했습니다. 탐색 경로: {searched}")


def load_config(
    preset: str | None = None,
    overrides: dict[str, Any] | None = None,
    project_toml: Path = PROJECT_TOML,
) -> Config:
    """기본값 → vlogsrt.toml → 프리셋 → CLI 순으로 덮어쓴다."""
    merged: dict[str, Any] = {}
    if project_toml.is_file():
        merged.update(read_toml(project_toml))
    if preset:
        merged.update(read_toml(find_preset(preset)))
    if overrides:
        for key, value in overrides.items():
            if value is None:
                continue
            _put(merged, key, value, "CLI")
    return replace(Config(), **merged)


def format_config(cfg: Config) -> str:
    lines = []
    for key, value in cfg.to_dict().items():
        rendered = ", ".join(str(v) for v in value) if isinstance(value, list) else value
        lines.append(f"{key} = {rendered}")
    return "\n".join(lines)
