"""최종 편집(W-66, D117)의 설정 — 순수 모델(`final_edit`)·입출력(`final_edit_io`)·화면이 읽는다.

자막 경고의 넷(`min_duration_ms`·`max_duration_ms`·`min_gap_ms`·`max_cps`)은 **refine 값
그대로**다 — 시험이 `vlogsrt.config.Config`와 같음을 못 박는다(refine을 바꾸면 같이 바꾼다,
여기만 고치지 마라).
나머지는 **외부 근거 없는 조정 대상**이다(규칙 8) — 되돌리기 칸 수·자동 저장 지연·파형 해상도·줌 폭·
스냅 거리·셔틀 배속은 화면을 쓰는 감으로 골랐다. 어느 지문·캐시 열쇠·승인·견적·등급에도 없다 —
바꾸면 다음 편집 화면과 다음 저장만 달라진다. 숫자는 `client_settings()`로 브라우저에 데이터로 간다
(JS에 리터럴을 두지 않는다).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FinalEditSettings:
    """최종 편집 설정.

    - `min_duration_ms`·`max_duration_ms`·`min_gap_ms`·`max_cps`: 자막 경고 기준(refine 그대로).
      경고는 저장을 막지 않는다.
    - `undo_limit`: 브라우저 되돌리기 기록 칸 수. `coalesce_ms`: 같은 미세 조정이 이 안에 이어지면
      기록 한 칸으로 합친다.
    - `min_clip_frames`: 트림이 남기는 클립의 최소 프레임 수.
    - `max_text_chars`·`max_cues`: 저장 문서 검사의 상한(망가진 요청을 거부한다).
    - `autosave_ms`: 마지막 편집 뒤 자동 저장까지의 지연.
    - `new_cue_ms`: 플레이헤드에 새로 넣는 자막의 길이.
    - `reverse_tick_ms`: 역방향 셔틀(J)이 뒤로 한 번 시크하는 간격.
    - `waveform_rate`·`waveform_bin_ms`: 파형 진폭 디코딩 주파수와 빈 하나의 길이.
    - `zoom_*`: 타임라인 1초의 화소 폭 범위와 한 번 줌의 배율. `wheel_zoom_notch_px`: ⌘·Ctrl+휠이
      한 걸음 줌하는 휠 델타(마우스 한 칸 = 100 px) — 트랙패드 핀치의 작은 델타는 작게 줌한다.
      `snap_px`: 스냅이 붙는 거리.
    - `big_step_frames`: Shift+화살표·Shift+밀기의 프레임 수.
    - `shuttle_rates`: L을 거듭 누를 때의 배속.
    """

    min_duration_ms: int = 850
    max_duration_ms: int = 7000
    min_gap_ms: int = 80
    max_cps: int = 12
    undo_limit: int = 200
    coalesce_ms: int = 500
    min_clip_frames: int = 1
    max_text_chars: int = 500
    max_cues: int = 5000
    autosave_ms: int = 1500
    new_cue_ms: int = 2000
    reverse_tick_ms: int = 100
    waveform_rate: int = 4000
    waveform_bin_ms: int = 20
    zoom_min_px_per_s: int = 2
    zoom_max_px_per_s: int = 400
    zoom_step_percent: int = 125
    wheel_zoom_notch_px: int = 100
    snap_px: int = 8
    big_step_frames: int = 10
    shuttle_rates: tuple[int, ...] = (1, 2, 4, 8)

    def __post_init__(self) -> None:
        positive = (
            self.min_duration_ms, self.max_duration_ms, self.max_cps, self.undo_limit,
            self.min_clip_frames, self.max_text_chars, self.max_cues, self.autosave_ms,
            self.new_cue_ms, self.reverse_tick_ms, self.waveform_rate, self.waveform_bin_ms,
            self.zoom_min_px_per_s, self.big_step_frames,
        )  # fmt: skip
        if min(positive) <= 0 or self.min_gap_ms < 0 or self.coalesce_ms < 0 or self.snap_px < 0:
            raise ValueError("최종 편집 설정의 크기·한도는 양수여야 한다")
        if self.wheel_zoom_notch_px < 1:
            raise ValueError("휠 줌 한 걸음의 델타는 양수여야 한다")
        if self.zoom_max_px_per_s < self.zoom_min_px_per_s or self.zoom_step_percent <= 100:
            raise ValueError("줌 범위가 뒤집혔거나 줌 배율이 1 이하다")
        if not self.shuttle_rates or min(self.shuttle_rates) <= 0:
            raise ValueError("셔틀 배속은 양수 하나 이상이어야 한다")


FINAL_EDIT_SETTINGS = FinalEditSettings()


def client_settings(settings: FinalEditSettings = FINAL_EDIT_SETTINGS) -> dict[str, object]:
    """브라우저가 받는 설정 — 이름은 JS 쪽 그대로(camelCase)."""
    return {
        "minDurationMs": settings.min_duration_ms,
        "maxDurationMs": settings.max_duration_ms,
        "minGapMs": settings.min_gap_ms,
        "maxCps": settings.max_cps,
        "undoLimit": settings.undo_limit,
        "coalesceMs": settings.coalesce_ms,
        "minClipFrames": settings.min_clip_frames,
        "maxTextChars": settings.max_text_chars,
        "maxCues": settings.max_cues,
        "autosaveMs": settings.autosave_ms,
        "newCueMs": settings.new_cue_ms,
        "reverseTickMs": settings.reverse_tick_ms,
        "zoomMinPxPerS": settings.zoom_min_px_per_s,
        "zoomMaxPxPerS": settings.zoom_max_px_per_s,
        "zoomStepPercent": settings.zoom_step_percent,
        "wheelZoomNotchPx": settings.wheel_zoom_notch_px,
        "snapPx": settings.snap_px,
        "bigStepFrames": settings.big_step_frames,
        "shuttleRates": list(settings.shuttle_rates),
    }
