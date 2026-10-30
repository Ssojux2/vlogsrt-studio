"""작업공간별 추천 기준(W-42, D100 §3) — 기본값은 그대로, 덮은 기준은 낡음 열쇠에 실린다.

순수 쪽은 덮기 JSON ↔ `RawTierSettings` 변환과 기준 digest를 본다. 서비스 쪽은 합성 미디어(15초 ·
창 하나)에 재생 분석을 돌리고(유료 호출 0회) 작업공간 표지 `tier_settings`로 기준을 덮는다:

- 기본 기준의 digest는 `"default"`이고 모든 낡음 열쇠·표지가 오늘과 바이트까지 같다.
- 덮은 기준은 등급 구간을 바꾸고, 검토 머리는 한 번만 낡는다 — 다시 매김이 사람 결정을 지킨다.
- 빈 덮기(`{}`)는 기본으로 돌아간다.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from tests.raw.test_review_workspace import _wait_terminal
from tests.raw.test_tier_review_gate import UTTERANCES, analyzed_tier_project
from tests.raw.test_tier_timing_service import _head_tiers, _project, _report, _upkeep
from vlogsrt.project.raw import context_review, tiering
from vlogsrt.project.raw import service as raw_service
from vlogsrt.project.raw.analysis_config import (
    EDITABLE_TIER_KEYS,
    TIER_SETTINGS,
    RawTierSettings,
    tier_settings_from_overrides,
    tier_settings_overrides,
)
from vlogsrt.project.raw.review_workspace import ReviewWorkspaceFacade
from vlogsrt.project.raw.segment_review import unreviewed_auto_exclusions

# D104 — whisper 응답 모양의 재생으로 재는 시험이다. 새로 듣는 엔진을 whisper로 둔다.
pytestmark = pytest.mark.usefixtures("whisper_for_new_hearings")

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")

LONG_MINIMUM = {"recommended_min_seconds": 20.0}
"""15초 원본의 녹색 구간을 「추천 최소 길이」로 보통까지 내리는 덮기."""


# ── 순수: 덮기 ↔ 설정 · digest ──────────────────────────────────────────────


def test_empty_overrides_are_the_default_settings() -> None:
    assert tier_settings_from_overrides({}) == TIER_SETTINGS
    assert tier_settings_overrides(TIER_SETTINGS) == {}


def test_overrides_round_trip_through_json() -> None:
    overrides = {
        "recommended_min_seconds": 5.0,
        "speech_density_green": 0.5,
        "scenery_green": False,
        "visual_issue_caps": ["dark"],
        "same_text_run_min": 4,
    }
    settings = tier_settings_from_overrides(overrides)
    assert settings.visual_issue_caps == ("dark",)
    assert settings.scenery_green is False
    again = tier_settings_overrides(settings)
    assert again == overrides
    assert json.loads(json.dumps(again)) == again
    assert tier_settings_from_overrides(again) == settings


def test_override_equal_to_default_is_not_kept_and_ints_become_floats() -> None:
    settings = tier_settings_from_overrides(
        {"recommended_min_seconds": 3, "silence_min_seconds": 6}
    )
    assert settings.silence_min_seconds == 6.0 and isinstance(settings.silence_min_seconds, float)
    assert tier_settings_overrides(settings) == {"silence_min_seconds": 6.0}


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"no_such_knob": 1}, "no_such_knob"),
        ({"tier_version": "raw-tier/v99"}, "tier_version"),
        ({"speech_density_green": 2.0}, "발화 밀도"),
        ({"recommended_min_seconds": -1.0}, "추천 최소 길이"),
        ({"recommended_min_seconds": "3"}, "recommended_min_seconds"),
        ({"usable_speech_green": 1}, "usable_speech_green"),
        ({"recommended_min_seconds": True}, "recommended_min_seconds"),
        ({"same_text_run_min": 2.5}, "same_text_run_min"),
        ({"visual_issue_caps": ["dark", "glare"]}, "glare"),
        ({"visual_issue_caps": "dark"}, "visual_issue_caps"),
        ({"visual_issue_caps": ["dark", "dark"]}, "visual_issue_caps"),
        ({"context_raise_rules": ["both-good"]}, "both-good"),
        ({"context_lower_verdicts": ["core"]}, "내리기와 올리기"),
        ({"segment_green_share": 0.0}, "조각 녹색 몫"),
    ],
)
def test_bad_overrides_are_rejected(overrides: dict[str, Any], fragment: str) -> None:
    with pytest.raises(ValueError, match=fragment):
        tier_settings_from_overrides(overrides)


def test_editable_keys_are_real_fields() -> None:
    names = set(RawTierSettings.__dataclass_fields__)
    assert set(EDITABLE_TIER_KEYS) <= names
    assert "tier_version" not in EDITABLE_TIER_KEYS
    assert EDITABLE_TIER_KEYS == (
        "recommended_min_seconds",
        "speech_density_green",
        "silence_min_seconds",
        "usable_speech_green",
        "scenery_green",
        "good_sparse_green",
        "segment_green_share",
        "visual_issue_caps",
    )


def test_default_tier_values_are_unchanged() -> None:
    """이 변경은 기본값을 바꾸지 않는다 — 사람만 작업공간마다 덮는다."""
    assert RawTierSettings() == TIER_SETTINGS
    assert (
        TIER_SETTINGS.tier_version,
        TIER_SETTINGS.recommended_min_seconds,
        TIER_SETTINGS.speech_density_green,
        TIER_SETTINGS.silence_min_seconds,
        TIER_SETTINGS.segment_green_share,
        TIER_SETTINGS.visual_issue_caps,
    ) == ("raw-tier/v7", 3.0, 0.35, 5.0, 0.5, ("dark", "overexposed"))


def test_criteria_digest_is_default_for_defaults_and_stable_otherwise() -> None:
    assert tiering.criteria_digest(TIER_SETTINGS) == tiering.CRITERIA_DEFAULT == "default"
    assert tiering.criteria_digest(RawTierSettings()) == "default"
    assert tiering.tier_key(TIER_SETTINGS) == "raw-tier/v7"
    custom = tier_settings_from_overrides(LONG_MINIMUM)
    digest = tiering.criteria_digest(custom)
    assert len(digest) == 12 and int(digest, 16) >= 0
    assert tiering.criteria_digest(tier_settings_from_overrides(dict(LONG_MINIMUM))) == digest
    assert tiering.tier_key(custom) == f"raw-tier/v7|{digest}"
    other = tier_settings_from_overrides({"recommended_min_seconds": 21.0})
    assert tiering.criteria_digest(other) != digest


# ── 서비스: 표지 · 등급 · 낡음 ──────────────────────────────────────────────


@needs_ffmpeg
def test_default_workspace_keys_and_labels_are_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, root, asset, workspace, _source = _project(tmp_path)
    view = service.tier_settings_view(root)
    assert view["digest"] == "default" and view["overrides"] == {}
    assert view["values"] == view["defaults"]
    assert view["editable_keys"] == list(EDITABLE_TIER_KEYS)
    assert workspace.meta_value(raw_service.TIER_SETTINGS_KEY) is None
    assert service.tier_key(root) == "raw-tier/v7"
    assert service._tier_settings(workspace) is TIER_SETTINGS

    seen: list[str] = []
    real = context_review.context_key

    def spy(parts: Any, *, tier_version: str, **kwargs: Any) -> str:
        seen.append(tier_version)
        return real(parts, tier_version=tier_version, **kwargs)

    monkeypatch.setattr(context_review, "context_key", spy)
    service._context_key(root)
    assert seen == ["raw-tier/v7"]

    facade = ReviewWorkspaceFacade(root)
    head = facade.load_head().revision
    monkeypatch.setattr(type(facade), "stale_grade_segments", lambda self, h: (("s", "n", ()),))
    regrade = raw_service._regrade_state(facade, head)
    assert regrade is not None
    assert regrade.startswith(f"다시 매김|raw-tier/v7|{head.revision_id}|")
    monkeypatch.setattr(type(facade), "needs_tier_migration", lambda self, h: True)
    migration = raw_service._tier_state(facade, head)
    assert migration is not None and migration.startswith("raw-tier/v7|표지 True|옛 제안 ")


@needs_ffmpeg
def test_override_changes_spans_and_regrades_the_head_once_keeping_humans(
    tmp_path: Path,
) -> None:
    service, root, asset, workspace, _source = _project(tmp_path)
    assert [s.tier for s in _report(service, root, asset.asset_id).spans] == ["green"]
    assert _head_tiers(root) == [("recommended", ["등급: 추천"])]
    facade = ReviewWorkspaceFacade(root)
    head = facade.load_head().revision
    (segment,) = head.segments
    head = facade.set_notes(head.base_plan_id, segment.segment_id, "사람 메모")

    # 표지만 바뀌었다(다시 매김 전) — 정리 고리가 등급 할 일을 한 번 본다.
    workspace.set_meta_value(raw_service.TIER_SETTINGS_KEY, json.dumps(LONG_MINIMUM))
    digest = tiering.criteria_digest(tier_settings_from_overrides(LONG_MINIMUM))
    assert service.tier_key(root) == f"raw-tier/v7|{digest}"
    spans = _report(service, root, asset.asset_id).spans
    assert [s.tier for s in spans] == ["yellow"]
    assert spans[0].caps == ((tiering.CAP_TOO_SHORT, "20초보다 짧아 추천하지 않습니다"),)
    pending = service._upkeep_pending(root)
    assert pending[raw_service.UPKEEP_TIERS].startswith(f"다시 매김|raw-tier/v7|{digest}|")
    assert _upkeep(service, root) is not None
    after = facade.load_head().revision
    (kept,) = after.segments
    assert kept.recommendation == "normal" and kept.notes == "사람 메모"
    assert (kept.start_pts, kept.end_pts) == (segment.start_pts, segment.end_pts)
    assert after.edit is not None and after.edit.notes == f"등급 재계산 raw-tier/v7|{digest}"
    assert raw_service.UPKEEP_TIERS not in service._upkeep_pending(root)
    assert _upkeep(service, root) is None  # 두 번째 읽기는 낡지 않았다


@needs_ffmpeg
def test_set_tier_settings_regrades_and_reset_returns_to_defaults(tmp_path: Path) -> None:
    service, root, asset, workspace, _source = _project(tmp_path)
    view = service.set_tier_settings(root, LONG_MINIMUM)
    assert view["overrides"] == LONG_MINIMUM and view["digest"] != "default"
    assert view["values"]["recommended_min_seconds"] == 20.0
    assert _head_tiers(root)[0][0] == "normal"  # 저장이 곧 다시 매김이다(정리 고리의 등급 단계)
    assert raw_service.UPKEEP_TIERS not in service._upkeep_pending(root)

    with pytest.raises(raw_service.RawWorkspaceError, match="no_such_knob"):
        service.set_tier_settings(root, {"no_such_knob": 1})
    assert service.tier_settings_view(root)["overrides"] == LONG_MINIMUM  # 거부는 쓰지 않는다

    reset = service.set_tier_settings(root, {})
    assert reset["digest"] == "default" and reset["overrides"] == {}
    assert service.tier_key(root) == "raw-tier/v7"
    assert service._tier_settings(workspace) == TIER_SETTINGS
    assert _head_tiers(root) == [("recommended", ["등급: 추천"])]
    assert raw_service.UPKEEP_TIERS not in service._upkeep_pending(root)


@needs_ffmpeg
def test_context_key_follows_the_criteria(tmp_path: Path) -> None:
    service, root, *_ = _project(tmp_path)
    before, _model = service._context_key(root)
    service.set_tier_settings(root, LONG_MINIMUM)
    custom, _model = service._context_key(root)
    assert custom != before
    service.set_tier_settings(root, {})
    assert service._context_key(root)[0] == before


@needs_ffmpeg
def test_widened_silence_releases_a_red_proposal_the_criteria_no_longer_make(
    tmp_path: Path,
) -> None:
    """8초 자막 공백의 빨강 제안 — 공백 기준을 10초로 덮으면 빨강이 아니므로 사용으로 풀린다."""
    service, root, *_ = _project(tmp_path, suitability="usable", utterances=UTTERANCES)
    facade = ReviewWorkspaceFacade(root)
    (pending,) = unreviewed_auto_exclusions(facade.load_head().revision)
    assert json.loads(pending.notes.removeprefix("자동 제외: "))["version"] == "raw-tier/v7"
    service.set_tier_settings(root, {"silence_min_seconds": 6.0})  # 아직 빨강 — 그대로 둔다
    assert [s.segment_id for s in unreviewed_auto_exclusions(facade.load_head().revision)] == [
        pending.segment_id
    ]
    service.set_tier_settings(root, {"silence_min_seconds": 10.0})
    head = facade.load_head().revision
    assert unreviewed_auto_exclusions(head) == ()
    released = next(s for s in head.segments if s.segment_id == pending.segment_id)
    assert released.selection == "included" and released.notes == ""
    assert "등급: 제외 제안" not in released.visual_tags
    assert raw_service.UPKEEP_TIERS not in service._upkeep_pending(root)


@needs_ffmpeg
def test_proposal_made_under_custom_criteria_carries_their_key(tmp_path: Path) -> None:
    """덮은 기준(공백 6초)으로 세운 검토의 빨강 제안은 그 기준 열쇠를 단다 — 기본으로 돌아가도
    빨강이면 그대로, 그 뒤 빨강이 아니게 되면(공백 10초) 옛 기준의 제안으로 보여 풀린다."""
    service, root = analyzed_tier_project(tmp_path, suitability="usable")
    service.set_tier_settings(root, {"silence_min_seconds": 6.0})  # 검토 머리가 아직 없다
    finished = _wait_terminal(service, root, service.start_cleanup(root).job_id)
    assert finished.status == "succeeded", finished.error
    facade = ReviewWorkspaceFacade(root)
    (pending,) = unreviewed_auto_exclusions(facade.load_head().revision)
    six = tiering.tier_key(tier_settings_from_overrides({"silence_min_seconds": 6.0}))
    assert six != "raw-tier/v7"
    assert json.loads(pending.notes.removeprefix("자동 제외: "))["version"] == six
    assert "등급판 raw-tier/v7" in pending.visual_tags  # 조각의 판 표지는 등급판 그대로다
    service.set_tier_settings(root, {})
    assert [s.segment_id for s in unreviewed_auto_exclusions(facade.load_head().revision)] == [
        pending.segment_id
    ]
    service.set_tier_settings(root, {"silence_min_seconds": 10.0})
    assert unreviewed_auto_exclusions(facade.load_head().revision) == ()


@needs_ffmpeg
def test_corrupt_stored_criteria_fall_back_to_defaults_loudly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, root, asset, workspace, _source = _project(tmp_path)
    workspace.set_meta_value(raw_service.TIER_SETTINGS_KEY, '{"tier_version": "x"}')
    warnings: list[str] = []
    monkeypatch.setattr(raw_service.log, "warn", warnings.append)
    assert service._tier_settings(workspace) is TIER_SETTINGS
    assert [line for line in warnings if "추천 기준" in line]
    view = service.tier_settings_view(root)
    assert view["digest"] == "default" and view["stored_error"]
