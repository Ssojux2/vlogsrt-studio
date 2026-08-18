"""모든 임계값이 모이는 곳(불변 규칙 6).

우선순위: CLI 옵션 > `--preset` > `vlogsrt.toml` > 코드 기본값.
TOML은 읽기 편하라고 섹션을 나누지만, 키 이름은 전역에서 유일하다.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
PROJECT_TOML = Path("vlogsrt.toml")
PRESET_DIRS = (Path("presets"), PACKAGE_ROOT / "presets")


@dataclass(frozen=True, slots=True)
class Config:
    # ─── 전사 ────────────────────────────────────────────────
    language: str = "ko"
    # 힌트는 **검증 모델에만** 간다(규칙 6). whisper 호출에는 실리지 않는다.
    prompt: str = ""
    keywords: tuple[str, ...] = ()
    # 시각과 글자를 모두 만든다. `segments[].{start,end,text}`를 주는 조합이어야 한다.
    # **whisper에는 prompt를 보내지 않는다** — 힌트는 검증 모델로만 간다(규칙 6, D16).
    transcribe_model: str = "whisper-1"
    transcribe_format: str = "verbose_json"
    # **되풀이를 막는 유일한 예방책이다**(D28). whisper는 창(30초)의 디코딩 결과를 다음
    # 창의 prompt로 넣는데(`condition_on_previous_text`), 그 글자가 되풀이면 다음 창도
    # 되풀이다 — 되풀이가 번지는 통로가 그것 하나뿐이다. 레퍼런스 구현은 온도가 0.5를
    # 넘으면 그 글자를 넘기지 않는다(`result.temperature > 0.5` → `prompt_reset_since`).
    # 그래서 0.6이다. 0.2·0.4는 greedy에서만 벗어날 뿐 통로가 그대로 살아 있다.
    #
    # 0은 쓰지 마라. API가 로그 확률을 보고 온도를 스스로 올리기는 하지만 **되풀이에는
    # 원리적으로 반응하지 못한다** — 되풀이에 빠진 whisper는 확신에 차 있다(D27 실측:
    # compression_ratio > 2.4인 676개 세그먼트 전부 temperature 0.0,
    # avg_logprob −0.09 ~ −0.38). 대가는 비결정성이다. 같은 영상이 매번 조금 다르게 나온다.
    transcribe_temperature: float = 0.6
    # ─── 되풀이 재전사 (기본으로 켜져 있다) ──────────────────
    # 되풀이 구간만 다시 잘라 whisper에 다시 묻고, gpt-transcribe가 심판한다(D27).
    # **출력을 바꾸는 유일한 판정 계층이다** — 끄려면 `--no-repair`.
    repair_enabled: bool = True
    # 재호출은 1차보다 뜨겁게 나간다. 같은 온도로 다시 물으면 같은 답이 오고, 0.5 아래로
    # 내리면 창 간 조건이 되살아난다. 시도 2회면 0.8 → 1.0이다(API 상한이 1.0).
    repair_temperature: float = 0.8
    repair_temperature_step: float = 0.2
    repair_max_attempts: int = 2
    repair_margin: float = 2.0  # 재전사 구간 앞뒤 여유. 경계에서 어절이 잘리지 않게 한다
    repair_min_seconds: float = 3.0  # 이보다 짧은 되풀이는 그냥 둔다
    # **재전사도 조각 한도를 지킨다**(D28). 지키지 않던 시절 490초짜리 되풀이를 한 조각으로
    # 다시 물었다 — 이미 whisper를 무너뜨린 것이 증명된 오디오를, 무너뜨린 그 길이 그대로.
    # 1차(180초)보다 보수적으로 간다. 계획이 쪼개지면 판정도 조각 단위가 된다.
    repair_chunk_seconds: float = 120.0
    # 회복본에 이만큼 넘게 되풀이가 남으면 불합격. **비율만으로는 못 잡는다** — 비율은
    # 구간이 길수록 느슨해져서, 490초 구간이 245초를 되풀이해도 합격했다(D28).
    repair_residual_seconds: float = 10.0
    # 회복 못 했을 때 첫 장만 남기고 비우는 최소 길이. 이보다 짧으면 1차 전사를 그대로
    # 둔다. D27의 접기는 1658초짜리 사고를 겨냥한 것이지 10초짜리가 아니었다 —
    # '치즈' 9회·10초는 사진 찍으며 실제로 아홉 번 말했을 수 있다(D28).
    repair_collapse_min_seconds: float = 30.0
    # ─── 검증 (기본으로 켜져 있다) ───────────────────────────
    # 자막을 다시 한 번 다른 모델로 받아 대조한다. **출력을 바꾸지 못하고 표시만 한다**
    # (규칙 5). 그래서 켜 두는 것이 기본이다 — 비용은 2배지만 자막은 그대로다.
    # 끄려면 `--no-verify`. 아래 값들은 외부 근거가 없는 조정 대상이다.
    verify_model: str = "gpt-transcribe"
    verify_format: str = "json"
    verify_chunk_sim_floor: float = 0.80
    verify_cue_match_floor: float = 0.60
    verify_short_cue_chars: int = 4  # "네", "음" 같은 짧은 큐는 대조가 무의미하다
    loop_min_repeats: int = 3
    loop_min_seconds: float = 2.0
    # **되풀이 검출기 둘의 경계선이다**(D29). 이 위는 메아리(글자가 같아야 한다), 이
    # 아래는 낱자(글자가 달라도 한 런이다). 빈틈도 겹침도 없다.
    #
    # 예전에는 메아리의 하한이기만 했고 그 아래는 아무도 안 봤다 — 실측에서 88초·44장이
    # 전부 한 글자('S','F')였는데 검출이 빈 목록을 돌려줬다. whisper의 압축률도 못 잡는다:
    # 그 30초 창의 텍스트가 29바이트뿐이라 zlib 헤더에 눌려 2.4에 걸터앉는다.
    loop_min_chars: int = 2
    # whisper 레퍼런스 구현의 `compression_ratio_threshold`. 응답이 세그먼트마다
    # 실어 보내는 값이라 공짜로 얻는 신호다. 실측 분포가 2.4에서 깨끗하게 갈렸다(D27).
    loop_compression_ratio: float = 2.4
    # 압축률을 재는 창. **통째로 재면 길이 검사가 된다** — 정상 발화도 길어지기만 하면
    # 임계값을 넘는다(실측: 정상 세그먼트 529개를 이어붙이면 2.45). 60어절에서 분포가
    # 갈렸다: 정상 최대 2.01 / 되풀이 최소 3.01.
    loop_ratio_window_words: int = 60
    # 재전사 결과에서 되풀이가 이 비율을 넘게 덮으면 회복 실패로 본다. 일부만 되풀이인
    # 결과는 통째로 버리지 않는다 — 원본보다는 낫다.
    loop_repetitive_share: float = 0.5

    api_max_retries: int = 4
    api_backoff_base: float = 2.0
    cost_per_minute: float = 0.006

    # ─── 오디오 전처리 ───────────────────────────────────────
    sample_rate: int = 16000
    channels: int = 1
    audio_bitrate: str = "64k"

    # ─── 분할 ────────────────────────────────────────────────
    # **자르는 유일한 기준**은 모델의 최대 입력이다. OpenAI 오디오 API는 요청당
    # 25MB — 안전 여유를 두고 24MB. 64kbps 모노면 조각 하나에 약 50분이 들어간다.
    # 낮추면 조각이 늘어난다: whisper가 긴 조각에서 반복 루프에 빠질 때의 탈출구다(D13).
    chunk_max_bytes: int = 24_000_000
    # **두 한도 중 작은 쪽이 이긴다**(D27). 바이트만으로는 못 막는다 — 비트레이트가
    # 바뀌면 같은 24MB가 38분이 되고, 그 길이에서 whisper가 무너진다(D26: 영상의
    # 41.4%가 되풀이).
    #
    # **이 값이 곧 되풀이가 자랄 수 있는 최대 길이다**(D28). 되풀이는 한 API 호출 안에서
    # 창을 타고 번지므로 호출이 바뀌면 반드시 끊긴다 — 조각 경계가 유일한 강제 리셋이다.
    # 600초였을 때 9.7분 조각에서 448초짜리 되풀이가 나왔다. 180초면 상한이 3분이다.
    # 요금이 분당이라 조각을 늘려도 총비용은 같다. 느는 것은 요청 수와 경계 수뿐이다.
    # **올리지 마라.** 내리는 것은 D13이 말한 탈출구다.
    chunk_max_seconds: float = 180.0
    # 아래 셋은 외부 근거가 없다. 자를 자리를 고를 뿐이라 틀려도 구간이 사라지지 않는다.
    cut_search_seconds: float = 5.0  # 목표 경계 앞뒤로 조용한 곳을 찾는 범위
    cut_probe_rate: int = 4000  # 진폭 디코딩 주파수. 에너지만 보면 되니 낮춰도 된다
    cut_quiet_window_ms: int = 20  # 진폭을 재는 창

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


def find_project_toml(path: Path = PROJECT_TOML) -> Path | None:
    """프로젝트 설정 파일. 현재 디렉터리에 없으면 패키지 기준으로도 찾는다.

    상대 경로만 보던 시절, UI나 다른 폴더에서 실행하면 `vlogsrt.toml`을 못 찾고
    코드 기본값으로 돌아갔다. D26이 `chunk_max_bytes`를 낮췄는데도 38분 조각으로
    돌아 되풀이가 재발한 것이 이 경로였다(D27). 프리셋은 이미 이렇게 찾고 있었다.

    **최선의 노력일 뿐이다.** 소스 체크아웃에서는 통하지만 wheel로 설치하면
    `PACKAGE_ROOT`가 `site-packages`라 그 파일이 없다. 그래서 되풀이를 실제로 막는
    값(`chunk_max_seconds = 600`)은 설정 파일이 아니라 `Config` 기본값에 있다.
    """
    if path.is_absolute():
        return path if path.is_file() else None
    for base in (Path.cwd(), PACKAGE_ROOT):
        candidate = base / path
        if candidate.is_file():
            return candidate
    return None


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
    found = find_project_toml(project_toml)
    if found:
        merged.update(read_toml(found))
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
