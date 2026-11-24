"""캐시 열쇠 안정화(P-6 · 성능 계획 Phase 4-5, D160) — 이미 산 전사·판정을 다시 사지 않는다.

- 청크 열쇠의 제공자 칸은 클래스 이름이 아니라 고정 값 `CACHE_ORIGIN`이다. 그 값은 지금 이름과
  같아 열쇠 바이트가 그대로다(이행이 필요 없다). 하위 클래스는 부모 값을 물려받지 않는다.
- 의미 판정 캐시 폴더는 의미 분석 모델만 묶는다 — 시간 엔진만 바꿔도 같은 판정을 찾는다. 옛 폴더
  (시간 · 글자 모델까지 묶은 것)는 읽기만 한다.
"""

from __future__ import annotations

import inspect
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from vlogsrt.project.raw import analysis_workflow as aw
from vlogsrt.project.raw import media_refs, speech_refill, understanding
from vlogsrt.project.raw.analysis_config import RawUnderstandingSettings
from vlogsrt.project.raw.caption_provider import GeminiTranscribeTimingProvider
from vlogsrt.project.raw.config import RawWorkflowConfig

PROVIDERS = (
    aw.ReplayAsrProvider,
    aw.ApprovedTimingProvider,
    aw.ApprovedPhaseAsrProvider,
    GeminiTranscribeTimingProvider,
    speech_refill.ApprovedRefillTimingProvider,
)


@pytest.mark.parametrize("cls", PROVIDERS, ids=lambda cls: cls.__name__)
def test_the_fixed_origin_keeps_the_old_key_bytes(cls: type) -> None:
    provider = object.__new__(cls)
    assert understanding.provider_origin(provider) == cls.__name__  # 지금 열쇠 바이트 그대로
    assert "CACHE_ORIGIN" in vars(cls)  # 물려받지 않고 제 값을 든다
    settings = RawWorkflowConfig()
    span = understanding.SampleSpan(0, 16000)
    old = understanding.asr_chunk_key("a" * 64, cls.__name__, settings, span)
    new = understanding.asr_chunk_key(
        "a" * 64, understanding.provider_origin(provider), settings, span
    )
    assert new == old


def test_a_renamed_class_keeps_its_origin_and_a_fake_falls_back_to_its_name() -> None:
    renamed = type(
        "SomethingElse",
        (GeminiTranscribeTimingProvider,),
        {"CACHE_ORIGIN": "GeminiTranscribeTimingProvider"},
    )
    assert (
        understanding.provider_origin(object.__new__(renamed)) == "GeminiTranscribeTimingProvider"
    )

    class FakeProvider:
        pass

    assert understanding.provider_origin(FakeProvider()) == "FakeProvider"
    assert speech_refill.WHISPER_ORIGIN == "ApprovedRefillTimingProvider"


def test_no_chunk_key_reads_the_class_name_directly() -> None:
    source = inspect.getsource(understanding)
    assert "type(provider).__name__, settings" not in source
    assert "type(context.provider).__name__" not in source


def test_the_window_cache_folder_binds_only_the_semantic_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(media_refs, "fingerprint", lambda workspace, asset_id: "media")
    workspace = SimpleNamespace(root=tmp_path)
    asset = SimpleNamespace(asset_id="a1", content_sha256="s" * 64)
    settings = RawUnderstandingSettings()
    workflow = RawWorkflowConfig()
    current, legacy = aw._window_cache_dirs(workspace, asset, settings, workflow)
    assert current != legacy
    other_timing = replace(workflow, models=replace(workflow.models, timing_model="whisper-1"))
    if other_timing.models.timing_model == workflow.models.timing_model:
        other_timing = replace(
            workflow, models=replace(workflow.models, timing_model="gemini-3.5-transcribe")
        )
    moved, moved_legacy = aw._window_cache_dirs(workspace, asset, settings, other_timing)
    assert moved == current  # 시간 엔진만 바꿔도 같은 폴더
    assert moved_legacy != legacy  # 옛 열쇠는 시간 모델까지 묶었다
    other_luna = replace(workflow, models=replace(workflow.models, luna_model="claude-sonnet-5-5"))
    assert aw._window_cache_dirs(workspace, asset, settings, other_luna)[0] != current


def test_the_lookup_reads_the_old_folder_and_writes_only_the_new() -> None:
    source = inspect.getsource(aw._finish_source)
    assert "cache_dir, legacy_dir = _window_cache_dirs(" in source
    assert 'candidate = legacy_dir / (digest + ".json")' in source
    assert (
        "legacy_dir.mkdir" not in source
        and "legacy_dir /" not in source.split("def remember")[1].split("def publish_progress")[0]
    )
