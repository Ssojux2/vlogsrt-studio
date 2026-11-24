"""창 요청 신원을 한 번 재기(P-1 · 성능 계획 Phase 4-2, D155).

한 원본의 분석 꼬리(`_finish_source`) 동안 창마다 페이로드를 지어 잰 값(요청 신원 sha · 공식 단가
상한)을 기억한다 — 예전에는 같은 창의 페이로드(클립·프레임 base64)를 다섯 번 짓고 다섯 번 해시했다.
시험이 못 박는 것:

- 신원은 예전 식(`luna_request_sha256(model, build_semantic_payload(...))`) 그대로다 — 모든 공급자,
  클립이 있든 없든. 그래서 그 sha로 열리는 판정 캐시를 다시 사지 않는다.
- 기억은 스코프 안에서만 산다(요청을 넘지 않는다) — 스코프 밖은 예전처럼 매번 잰다.
- 파일이 바뀌면(크기·mtime·inode) 다시 잰다. 크기·mtime까지 그대로 맞춘 바꿔치기는 기억이 못
  알아채지만 **전송 순간의 문**(`respond`)은 기억을 쓰지 않고 실제 페이로드를 다시 해시해
  예약 전에 닫는다.
- 응답자 공장 · 문맥 · 상한 검사 · 판정 단계가 한 창의 클립을 한 번만 읽고, 보낼 때 한 번 더 읽는다.
"""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from tests.raw.test_gemini_analysis import (
    FLASH,
    SETTINGS,
    accepted_body,
    env_online,
    gemini_client,
    gemini_reply,
    responder_for,
    with_clip,
)
from tests.raw.test_understanding_luna import make_evidence
from vlogsrt.project.raw import understanding_luna as luna
from vlogsrt.project.raw.config import OPENAI_LUNA_MODEL

CLAUDE = "claude-sonnet-5-5"


@pytest.mark.parametrize("model", [OPENAI_LUNA_MODEL, CLAUDE, FLASH])
@pytest.mark.parametrize("clip", [False, True])
def test_the_identity_is_the_old_formula_inside_and_outside_the_scope(
    tmp_path: Path, model: str, clip: bool
) -> None:
    evidence = with_clip(tmp_path) if clip else make_evidence(tmp_path)
    old = luna.luna_request_sha256(model, luna.build_semantic_payload(evidence, SETTINGS, model))
    assert luna.semantic_request_sha256(evidence, SETTINGS, model) == old
    with luna.request_identity_scope():
        assert luna.semantic_request_sha256(evidence, SETTINGS, model) == old
        assert luna.semantic_request_sha256(evidence, SETTINGS, model) == old


def _count_reads(monkeypatch: pytest.MonkeyPatch, target: Path) -> list[Path]:
    reads: list[Path] = []
    original = Path.read_bytes

    def counting(self: Path) -> bytes:
        if self == target:
            reads.append(self)
        return original(self)

    monkeypatch.setattr(Path, "read_bytes", counting)
    return reads


def test_the_scope_reads_a_window_once_and_forgets_outside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence = with_clip(tmp_path)
    assert evidence.clip is not None
    reads = _count_reads(monkeypatch, evidence.clip.path)
    with luna.request_identity_scope():
        first = luna.semantic_request_sha256(evidence, SETTINGS, FLASH)
        # 같은 값의 다른 객체(작업 흐름과 응답자가 따로 짓는다)도 같은 기억이다.
        twin = replace(evidence)
        assert luna.semantic_request_sha256(twin, SETTINGS, FLASH) == first
    assert len(reads) == 1
    luna.semantic_request_sha256(evidence, SETTINGS, FLASH)
    luna.semantic_request_sha256(evidence, SETTINGS, FLASH)
    assert len(reads) == 3  # 스코프 밖은 예전처럼 매번 잰다 — 기억은 요청을 넘지 않는다


def test_a_changed_file_is_measured_again(tmp_path: Path) -> None:
    evidence = with_clip(tmp_path)
    assert evidence.clip is not None
    with luna.request_identity_scope():
        before = luna.semantic_request_sha256(evidence, SETTINGS, FLASH)
        evidence.clip.path.write_bytes(evidence.clip.path.read_bytes() + b"more")
        after = luna.semantic_request_sha256(evidence, SETTINGS, FLASH)
    assert before != after
    assert after == luna.luna_request_sha256(
        FLASH, luna.build_semantic_payload(evidence, SETTINGS, FLASH)
    )


def _sending(monkeypatch: pytest.MonkeyPatch, windows: Any) -> list[dict[str, Any]]:
    by_id = {item.window.window_id: item for item in windows}
    sent: list[dict[str, Any]] = []

    def fake_gemini(client: Any, **kwargs: Any) -> dict[str, Any]:
        sent.append(kwargs)
        parts = kwargs["payload"]["contents"][0]["parts"]
        window_id = parts[-1]["text"].split("window_id: ")[1].splitlines()[0]
        return gemini_reply(accepted_body(by_id[window_id]))

    monkeypatch.setattr(luna, "request_gemini_generate", fake_gemini)
    return sent


def test_a_validated_run_reads_each_clip_once_to_measure_and_once_to_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """공장(신원) · 문맥(상한 · 신원 재검사) · 판정 단계(재사용 확인)가 한 번 잰 값을 쓴다 —
    예전에는 창마다 네 번 짓고, 보낼 때 또 지었다."""
    env_online(monkeypatch)
    with luna.request_identity_scope():
        responder = responder_for(tmp_path, gemini_client())
        windows = responder.context.evidence_windows
        clip = windows[0].clip
        assert clip is not None
        reads = _count_reads(monkeypatch, clip.path)
        sent = _sending(monkeypatch, windows)
        annotations = luna.annotate_windows(
            windows, responder, settings=SETTINGS, model=FLASH, origin="live"
        )
    assert all(item.status == luna.ANNOTATION_ACCEPTED for item in annotations)
    assert len(sent) == len(windows)
    assert len(reads) == 1  # 공장에서 이미 쟀다 — 남은 한 번은 보낼 본문


def test_a_same_stat_swap_after_measuring_is_closed_at_the_send_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """크기·mtime·inode를 그대로 둔 바꿔치기 — 기억은 옛 신원을 주지만 전송 문은 기억을 쓰지 않고
    실제 본문을 다시 해시한다. 예약 전에 닫히고 공급자에 아무것도 가지 않는다."""
    env_online(monkeypatch)
    with luna.request_identity_scope():
        responder = responder_for(tmp_path, gemini_client())
        windows = responder.context.evidence_windows
        clip = windows[0].clip
        assert clip is not None
        stat = clip.path.stat()
        swapped = bytes(b ^ 0xFF for b in clip.path.read_bytes())
        with clip.path.open("r+b") as handle:  # 같은 inode · 같은 크기
            handle.write(swapped)
        os.utime(clip.path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        sent = _sending(monkeypatch, windows)
        annotations = luna.annotate_windows(
            windows[:1], responder, settings=SETTINGS, model=FLASH, origin="live"
        )
    assert sent == []
    assert annotations[0].status != luna.ANNOTATION_ACCEPTED
    assert responder.gate.keys == set()  # 예약 전에 닫혔다
