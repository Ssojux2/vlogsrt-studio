"""렌더·번인은 급한 자리를 쓴다(W-80, D140) — 사람이 기다리는 일이 배경 프록시 뒤로 밀리지 않는다.

D136이 썸네일·미리보기를 급한 자리로 올리자, 보통 자리였던 렌더·번인은 라이브러리의 쪽 준비가
기다리는 동안 빈 자리를 양보하게 됐다 — 불러오기 도중에 누른 「추천만 이어붙여 내보내기」가 배경
준비 뒤로 밀렸다.
렌더·번인도 사람이 결과를 기다리는 화면 일이다. 미리 만드는 분석 프록시만 보통 자리로 남는다.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

from vlogsrt import preprocess
from vlogsrt.project.raw import burn_export, media_refs, render_export


def _recorded(monkeypatch) -> list[bool]:
    asked: list[bool] = []

    @contextlib.contextmanager
    def slot(*, urgent: bool = False) -> Iterator[None]:
        asked.append(urgent)
        yield

    monkeypatch.setattr(media_refs, "local_media_slot", slot)
    return asked


def test_a_render_piece_takes_an_urgent_slot(monkeypatch, tmp_path: Path) -> None:
    asked = _recorded(monkeypatch)
    monkeypatch.setattr(preprocess, "render_part", lambda *a, **k: tmp_path / "part.mp4")
    item = SimpleNamespace(path=tmp_path / "a.mov", probe=None, clock=None, audio=None, asset=None)
    piece = SimpleNamespace(part_index=0, spec=SimpleNamespace(index=0))
    render_export._render_one(item, piece, None, None, tmp_path)
    assert asked == [True]


def test_a_burn_takes_an_urgent_slot(monkeypatch, tmp_path: Path) -> None:
    asked = _recorded(monkeypatch)
    monkeypatch.setattr(preprocess, "burn_subtitles", lambda *a, **k: "burned")
    job = SimpleNamespace(
        width=1920,
        height=1080,
        frame_duration=Fraction(1, 30),
        edit=None,
        duration=None,
        audio=None,
    )
    assert burn_export._encode(None, tmp_path / "out.mp4", job, "libx264") == "burned"
    assert asked == [True]


def test_prepared_analysis_proxies_stay_on_normal_slots(monkeypatch, tmp_path: Path) -> None:
    asked = _recorded(monkeypatch)
    monkeypatch.setattr(media_refs, "_build_proxy", lambda *a, **k: None)
    monkeypatch.setattr(
        media_refs,
        "validate_analysis_proxy",
        lambda original, target: media_refs.PairValidation("rejected", "시험"),
    )
    media_refs._build_and_verify(tmp_path, tmp_path / "a.mov", media_refs.ANALYSIS_PROXY, 1.0)
    assert asked == [False]
