"""W-15 × W-16 — 자막 시각이 지금 리비전에서 빨강인 영상만 `자막 시각 확인 필요`를 단다(D55·D56).

규칙은 하나다: **지금(자막 리비전·1단계 PCM·기준이 같은) 빨강 보고서만**. 낡았거나 없거나 측정
불가·초록·노랑이면 달지 않고, 왜 안 달았는지는 보내기 보고서의 `sync_check`에 남긴다. 키워드를
단 영상도 자막은 그대로 싣는다. 보내기는 재지 않는다 — 저장된 보고서를 읽기만 한다. 아무 영상도
걸리지 않으면 문서는 W-16과 바이트까지 같다.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
import xml.etree.ElementTree as ET
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from tests.raw.test_finalcut_timeline import (
    BUNDLE,
    EDGE_DOCUMENTS,
    INSTALLED_DTD,
    needs_installed_dtd,
    needs_xmllint,
    parsed,
    real_like_sources,
    spine_clips,
)
from tests.raw.test_tier_review_gate import tier_project
from vlogsrt.project import sync_check
from vlogsrt.project.config import SYNC_CHECK_SETTINGS
from vlogsrt.project.raw import finalcut_timeline as ft
from vlogsrt.project.raw import service as raw_service
from vlogsrt.project.raw import sync_report
from vlogsrt.project.xml_validation import validate_xml

# D104 — whisper 응답 모양의 재생으로 재는 시험이다. 새로 듣는 엔진을 whisper로 둔다.
pytestmark = pytest.mark.usefixtures("whisper_for_new_hearings")

TITLE = "vlogsrt · 260810_wangbang · 2026-10-09 18:30"

# W-16(d350091)의 빌더가 낸 바이트의 지문 — 걸린 영상이 없으면 그대로여야 한다. 하나만 등급
# 키워드로 바뀌었다(D60 규칙 2): `mixed`의 1프레임 원본 D는 녹색 구간 위의 0.03초 조각이라
# 3초보다 짧아 「추천」이 「보통」이 되고 브라우저 클립의 즐겨찾기가 빠진다(W-16·74c6adb의 값
# `40aa3dd1…4521f8ee`). 나머지 바이트는 같다. W-29d(D80)에서 `mixed`·`real_like`·`red`를 다시
# 고정했다 — 미검토 빨강 클립과 그 캡션에 `enabled="0"`이 붙은 것 말고는 같다(그 diff를 확인했다).
# W-29f(D81)에서 캡션이 있는 `hfr`·`mixed`·`pal`·`real_like`를 다시 고정했다 — 캡션 역할이 SRT다.
W16_DOCUMENT_SHA256 = {
    "film": "160c4890adcc5554ee6c2f6c6b380b9b2e7e7b6f36617bb94c58345acbd6b9a3",
    "hfr": "f29a6cfc05039a333560b18540fb32143940d5270d6fa601a31115bf45b42bcb",
    "mixed": "f8e3635d7109b07d069fde14c27a795ab908409f58be1f50a0fe4d4fde977063",
    "pal": "cc225cc58876bbbc87b1ea55c8f5cbb83bdcd67cbfd119633eec2bd3167c9618",
    "real_like": "0b4cc01f6e5525d580f3b922f1124c09e2c0c5bb6de04c8ce197fbc841d204d1",
    "red": "1cc7f2d925252c8461df08bb146e1de715eaff8551abdd234f54151214304785",
}

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")


def flagged_document(*indexes: int) -> ft.TimelineDocument:
    sources = real_like_sources()
    flagged = frozenset(sources[index].media.source_id for index in indexes)
    return ft.plan(sources, title=TITLE, library_bundle=BUNDLE, caption_timing=flagged)


def keywords(clip: ET.Element) -> list[str]:
    return [str(keyword.get("value")) for keyword in clip.findall("keyword")]


# ─── 순수 빌더 ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", sorted(EDGE_DOCUMENTS))
def test_without_flags_the_documents_are_byte_identical_to_w16(name: str) -> None:
    assert (
        hashlib.sha256(ft.render(EDGE_DOCUMENTS[name]())).hexdigest() == (W16_DOCUMENT_SHA256[name])
    )
    empty = ft.plan(
        real_like_sources(), title=TITLE, library_bundle=BUNDLE, caption_timing=frozenset()
    )
    assert ft.render(empty) == ft.render(
        ft.plan(real_like_sources(), title=TITLE, library_bundle=BUNDLE)
    )


def test_flagged_sources_carry_the_keyword_on_every_clip_and_their_browser_clip() -> None:
    """0008(분석·브라우저 클립 있음)과 0019(분석 전)를 건다 — 0014는 그대로다."""
    sources = real_like_sources()
    flagged_names = {sources[0].media.name, sources[2].media.name}
    root = parsed(flagged_document(0, 2))
    for clip in spine_clips(root):
        words = keywords(clip)
        if clip.get("name") in flagged_names:
            assert words[1] == ft.KEYWORD_CAPTION_TIMING  # 등급 키워드 바로 옆
            assert words.count(ft.KEYWORD_CAPTION_TIMING) == 1
        else:
            assert ft.KEYWORD_CAPTION_TIMING not in words
    browser = {clip.get("name"): clip for clip in root.findall("./library/event/asset-clip")}
    first = browser[sources[0].media.name]
    timing = [k for k in first.findall("keyword") if k.get("value") == ft.KEYWORD_CAPTION_TIMING]
    assert len(timing) == 1
    assert (timing[0].get("start"), timing[0].get("duration")) == (
        first.get("start"),
        first.get("duration"),
    )  # 원본 전체를 덮는다
    assert ft.KEYWORD_CAPTION_TIMING not in keywords(browser[sources[1].media.name])
    collections = [c.get("name") for c in root.findall("./library/event/keyword-collection")]
    assert collections.count(ft.KEYWORD_CAPTION_TIMING) == 1


def test_flagged_sources_keep_their_captions() -> None:
    plain = parsed(ft.plan(real_like_sources(), title=TITLE, library_bundle=BUNDLE))
    flagged = parsed(flagged_document(0, 1, 2))
    assert len(flagged.findall(".//caption")) == len(plain.findall(".//caption")) > 0
    assert [c.findtext("text") for c in flagged.iter("caption")] == [
        c.findtext("text") for c in plain.iter("caption")
    ]


@needs_xmllint
@needs_installed_dtd
def test_flagged_document_passes_the_installed_dtd(tmp_path: Path) -> None:
    target = tmp_path / "flagged.fcpxml"
    target.write_bytes(ft.render(flagged_document(0, 1, 2)))
    result = validate_xml(target, dtd_path=INSTALLED_DTD)
    assert result.dtd_checked is True and result.valid is True, result.diagnostics


def test_a_flag_for_a_source_that_is_not_exported_is_refused() -> None:
    with pytest.raises(ft.FinalCutTimelineError, match="자막 시각"):
        ft.plan(
            real_like_sources(),
            title=TITLE,
            library_bundle=BUNDLE,
            caption_timing=frozenset({"source_없음"}),
        )


def test_keyword_values_never_carry_commas() -> None:
    """`keyword@value`는 쉼표로 나뉘는 목록이다 — 라벨 하나가 둘로 갈라지면 안 된다."""
    assert all("," not in keyword for keyword in ft.KEYWORDS)


# ─── 서비스 — 지금 빨강만, 나머지는 보고서에, 재지 않는다 ─────────────────


def _settle_sync(service: raw_service.RawWorkspaceService, root: Path) -> None:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        latest = service.latest_job(root, raw_service.SYNC_CHECK_JOB_KIND)
        if latest is None or latest.status not in ("queued", "running"):
            return
        time.sleep(0.02)
    raise AssertionError("자막 시각 확인 작업이 끝나지 않았다")


def _put_report(
    workspace: Any, source: sync_report.SyncSource, verdict: str, **document: Any
) -> None:
    """저장된 보고서 하나를 바꿔 끼운다 — 보내기가 읽기만 하는지 보려는 재료다."""
    cache_root = workspace.root / sync_report.SYNC_CACHE
    shutil.rmtree(cache_root / "reports", ignore_errors=True)
    body = sync_report.report_document(
        source, SYNC_CHECK_SETTINGS, sync_check.Verdict(verdict, (), ()), None, None, None
    )
    sync_report.write_report(cache_root, {**body, **document}, SYNC_CHECK_SETTINGS)


def _export(service: raw_service.RawWorkspaceService, root: Path) -> tuple[ET.Element, dict]:
    path = service.export_finalcut_timeline(root)
    report = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    return ET.fromstring(path.read_bytes()), report


@needs_ffmpeg
def test_only_a_current_red_report_flags_the_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, root = tier_project(tmp_path, suitability="usable")
    _settle_sync(service, root)
    (asset,) = service.list_source_assets(root)
    workspace = service._workspace(root)
    source = sync_report.gather_source(workspace, asset)
    assert source is not None and source.stage1 is not None

    def forbidden(*_: Any, **__: Any) -> Any:
        raise AssertionError("보내기는 재지 않는다 — 저장된 보고서만 읽는다")

    monkeypatch.setattr(sync_report, "measure_source", forbidden)
    monkeypatch.setattr(sync_report, "run_checks", forbidden)
    before = service.latest_job(root, raw_service.SYNC_CHECK_JOB_KIND)

    _put_report(workspace, source, sync_check.LEVEL_RED)
    root_xml, report = _export(service, root)
    clips = list(root_xml.iterfind("./library/event/project/sequence/spine/asset-clip"))
    assert clips and all(ft.KEYWORD_CAPTION_TIMING in keywords(clip) for clip in clips)
    assert [row["source_id"] for row in report["sync_check"]["flagged"]] == [asset.asset_id]
    assert report["sync_check"]["rule"] == "current_red_only"
    assert report["counts"]["captions"]["placed"] == 4  # 키워드를 단 영상도 자막은 그대로

    cases = {
        "amber": (sync_check.LEVEL_AMBER, {}),
        "skip": (sync_check.LEVEL_SKIP, {}),
        "green": (sync_check.LEVEL_GREEN, {}),
        "stale": (sync_check.LEVEL_RED, {"stage1_pcm_sha256": "0" * 64}),  # PCM이 바뀐 뒤
    }
    for bucket, (verdict, override) in cases.items():
        _put_report(workspace, source, verdict, **override)
        root_xml, report = _export(service, root)
        assert ft.KEYWORD_CAPTION_TIMING not in ET.tostring(root_xml, encoding="unicode")
        assert [row["source_id"] for row in report["sync_check"][bucket]] == [asset.asset_id]
        assert report["sync_check"]["flagged"] == []

    old = replace(source, transcript_revision="이전-리비전")
    _put_report(workspace, old, sync_check.LEVEL_RED)  # 지난 자막 리비전의 빨강만 남았다
    _root, report = _export(service, root)
    assert [row["source_id"] for row in report["sync_check"]["stale"]] == [asset.asset_id]
    shutil.rmtree(workspace.root / sync_report.SYNC_CACHE / "reports")
    _root, report = _export(service, root)
    assert [row["source_id"] for row in report["sync_check"]["missing"]] == [asset.asset_id]
    assert service.latest_job(root, raw_service.SYNC_CHECK_JOB_KIND) == before  # 걸지도 않았다


@needs_ffmpeg
def test_a_failing_flag_lookup_still_exports_without_keywords_and_says_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """표지 계산은 표시 전용이다 — 깨져도 보내기는 키워드 없이 가고,
    이유를 보고서와 로그에 남긴다."""
    service, root = tier_project(tmp_path, suitability="usable")
    _settle_sync(service, root)
    (asset,) = service.list_source_assets(root)
    workspace = service._workspace(root)
    source = sync_report.gather_source(workspace, asset)
    assert source is not None
    _put_report(workspace, source, sync_check.LEVEL_RED)  # 읽혔다면 키워드가 달렸을 원본

    def broken(*_: Any, **__: Any) -> Any:
        raise ValueError("보고서 JSON이 깨졌다")

    monkeypatch.setattr(service, "sync_report", broken)
    warnings: list[str] = []
    monkeypatch.setattr(raw_service.log, "warn", warnings.append)
    root_xml, report = _export(service, root)
    assert ft.KEYWORD_CAPTION_TIMING not in ET.tostring(root_xml, encoding="unicode")
    assert report["sync_check"]["error"] == "ValueError: 보고서 JSON이 깨졌다"
    assert report["sync_check"]["rule"] == "current_red_only"
    assert "flagged" not in report["sync_check"]  # 재지 못한 칸을 빈 목록으로 꾸미지 않는다
    assert report["counts"]["captions"]["placed"] == 4
    assert [line for line in warnings if "보고서 JSON이 깨졌다" in line]
