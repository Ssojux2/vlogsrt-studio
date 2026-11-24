"""1단계 동안 Gemini 창 클립을 미리 자르기(P-4 · 성능 계획 Phase 4-4, D158).

- 창의 문맥 구간은 발화와 무관하다 — 발화 없이 세운 계획이 2단계의 구간과 같다.
- 전체 승인 · Gemini일 때만 자른다(「전사만」 · 다른 공급자는 아무것도 하지 않는다).
- 2단계가 쓰는 같은 캐시 · 같은 (시작, 길이)를 자른다 — 2단계는 다시 자르지 않는다.
- 실패는 덤이라 멈추지 않고 경고만, 멈추라면 지금 클립 뒤에 멈춘다.
- 「전사만」이 아니면 승인 실행이 그 깃발을 켠다.
"""

from __future__ import annotations

import inspect
import threading
from dataclasses import replace
from fractions import Fraction
from types import SimpleNamespace
from typing import Any

import pytest

from tests.raw.test_analysis_reconcile import SETTINGS, _timeline, _utterance
from vlogsrt.project.raw import analysis_workflow as aw
from vlogsrt.project.raw import media_refs, selection_analysis
from vlogsrt.project.raw.understanding import plan_common_windows

GEMINI = SimpleNamespace(models=SimpleNamespace(luna_model="gemini-3.8-flash"))
LUNA = SimpleNamespace(models=SimpleNamespace(luna_model="gpt-6-luna"))
ON = aw.AnalysisProviders(origin="live", phase="asr", preencode_clips=True)


def _ranges(plan: Any) -> list[tuple[str, Fraction, Fraction]]:
    return [(w.window_id, w.context_start, w.context_end) for w in plan.windows]


def test_window_context_ranges_do_not_depend_on_utterances() -> None:
    timeline, _ = _timeline(1200)  # 48초 — 창 여럿
    spoken = [_utterance(0, Fraction(3), Fraction(8)), _utterance(1, Fraction(20), Fraction(31))]
    assert _ranges(plan_common_windows(timeline, spoken, SETTINGS)) == _ranges(
        plan_common_windows(timeline, (), SETTINGS)
    )


class _Sampler:
    def __init__(self, fail: bool = False, on_clip: Any = None) -> None:
        self.cut: list[tuple[Fraction, Fraction]] = []
        self.fail = fail
        self.on_clip = on_clip

    def clip(self, *, start: Fraction, seconds: Fraction) -> Any:
        self.cut.append((start, seconds))
        if self.on_clip is not None:
            self.on_clip()
        if self.fail:
            raise RuntimeError("인코더 없음")
        return start


def _start(monkeypatch: pytest.MonkeyPatch, sampler: _Sampler, **overrides: Any) -> Any:
    timeline, grid = _timeline(1200)
    seen: list[Any] = []

    def clip_sampler(workspace: Any, asset: Any, *, audio_stream_index: Any) -> _Sampler:
        seen.append(audio_stream_index)
        return sampler

    monkeypatch.setattr(media_refs, "clip_sampler", clip_sampler)
    arguments: dict[str, Any] = {
        "workspace": object(),
        "asset": SimpleNamespace(source_name="a.mov"),
        "workflow": GEMINI,
        "providers": ON,
        "settings": SETTINGS,
        "grid": grid,
        "audio": SimpleNamespace(audio_stream_index=1),
        "timeline": timeline,
    }
    arguments.update(overrides)
    workspace = arguments.pop("workspace")
    asset = arguments.pop("asset")
    workflow = arguments.pop("workflow")
    providers = arguments.pop("providers")
    handle = aw._start_clip_preencode(workspace, asset, workflow, providers, **arguments)
    return handle, timeline, grid, seen


def test_preencode_cuts_exactly_what_the_semantic_phase_will_ask_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sampler = _Sampler()
    handle, timeline, grid, seen = _start(monkeypatch, sampler)
    handle.join()
    plan = plan_common_windows(timeline, (), SETTINGS)
    expected = [
        (w.context_start + grid.presentation_origin, w.context_end - w.context_start)
        for w in plan.windows
        if w.context_end > w.context_start
    ]
    assert sampler.cut == expected  # `_window_clips`와 같은 (시작, 길이) · 같은 표본기
    assert seen == [1]  # 1단계가 고른 소리 트랙 — 2단계와 같은 캐시 열쇠


@pytest.mark.parametrize(
    "overrides",
    [
        {"providers": aw.AnalysisProviders(origin="live", phase="asr")},  # 「전사만」
        {"workflow": LUNA},  # 클립을 보내지 않는 공급자
    ],
)
def test_nothing_is_cut_unless_a_full_gemini_approval(
    monkeypatch: pytest.MonkeyPatch, overrides: dict[str, Any]
) -> None:
    sampler = _Sampler()
    handle, *_ = _start(monkeypatch, sampler, **overrides)
    assert handle is None and sampler.cut == []


def test_failures_only_warn_and_a_stop_halts_after_the_current_clip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failing = _Sampler(fail=True)
    handle, *_ = _start(monkeypatch, failing)
    handle.join()
    assert len(failing.cut) > 1  # 한 창의 실패가 나머지를 막지 않는다
    holder: list[Any] = []
    gate = threading.Event()

    def stop_now() -> None:
        gate.wait(5)
        holder[0].stop()

    stopping = _Sampler(on_clip=stop_now)
    handle, *_ = _start(monkeypatch, stopping)
    holder.append(handle)
    gate.set()
    handle.join()
    assert len(stopping.cut) == 1


def test_the_full_approval_turns_preencode_on_and_transcribe_only_does_not() -> None:
    source = inspect.getsource(selection_analysis._run_transcription)
    assert "preencode_clips=approved.mode != MODE_TRANSCRIBE" in source
    assert replace(ON, preencode_clips=False).preencode_clips is False
    default = aw.AnalysisProviders(origin="live")
    assert default.preencode_clips is False  # 재생 · 다른 호출자는 예전 그대로
