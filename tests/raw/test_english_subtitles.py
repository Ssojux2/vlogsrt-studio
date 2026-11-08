"""W-53(D105) — 초안 내보내기의 영어 자막: 표지 · 견적 · 승인 · 번역(가짜 Gemini) · `draft.en.srt`.

시험이 못 박는 것(합성 폴더 하나를 처음부터 차례로):

- 표지를 켜지 않으면 번역 단계가 없다 — `draft.en.srt`가 없고 보고서 `english.status = off`.
- 켜면 견적은 무료이고 아직 옮기지 않은 원본만 센다. 사람이 본 digest가 다르거나 오프라인이면
  작업을 걸기 전에 닫는다(요청 0회).
- 번역이 실패하면 한국어 초안은 그대로 내고 영어 파일은 쓰지 않는다 — 보고서가 원본 이름·
  까닭을 센다.
- 승인하면 원본마다 옮기고 `draft.en.srt`를 낸다: 큐 수·번호·시각 줄이 `draft.srt`와 한 줄씩 같고
  글자만 영어다. 다음 내보내기는 다시 사지 않는다(요청 0회, 견적은 무료 문구).
- 한국어 `draft.srt`는 영어를 켜도 바이트 그대로다.

유료 호출은 없다 — 전송 자리에 가짜 opener가 앉는다.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest

from tests.raw.test_gemini_timing_provider import _client, _Reply
from tests.raw.test_render_export import UTTERANCES, _encode, _identity, _trip
from tests.raw.test_review_workspace import _wait_terminal
from vlogsrt.project.raw import english_track, timing_engine
from vlogsrt.project.raw import service as raw_service
from vlogsrt.project.raw import translation_io as tio
from vlogsrt.project.raw.analysis_store import ANALYSIS_ROOT
from vlogsrt.project.raw.config import TIMING_ENGINE_WHISPER

pytestmark = [
    pytest.mark.skipif(
        shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
        reason="ffmpeg/ffprobe가 없는 환경",
    ),
    pytest.mark.usefixtures("whisper_for_new_hearings"),
]


class _Translator:
    """받은 묶음의 줄마다 「EN <아이디>」를 돌려준다 — `broken`이면 마지막 줄을 빠뜨린다."""

    def __init__(self, broken: bool = False) -> None:
        self.broken = broken
        self.sent: list[dict[str, Any]] = []

    def __call__(self, request: Any, timeout: float) -> _Reply:
        payload = json.loads(request.data)
        self.sent.append(payload)
        lines = json.loads(payload["contents"][0]["parts"][1]["text"])["lines"]
        items = [{"id": row["id"], "en": f"EN {row['id']} ok"} for row in lines]
        body = {"items": items[:-1] if self.broken else items}
        reply = {
            "candidates": [
                {"finishReason": "STOP", "content": {"parts": [{"text": json.dumps(body)}]}}
            ],
            "usageMetadata": {"promptTokenCount": 300, "candidatesTokenCount": 80},
            "modelVersion": "gemini-3.8-flash-001",
        }
        return _Reply(json.dumps(reply).encode("utf-8"))


@dataclass(frozen=True)
class Trip:
    service: raw_service.RawWorkspaceService
    root: Path


@pytest.fixture(scope="module")
def trip(tmp_path_factory: pytest.TempPathFactory) -> Trip:
    folder = tmp_path_factory.mktemp("english_trip") / "제주 여행"
    _encode(folder / "A_uhd.mp4", "3840x2160", 30, 2, created="2026-09-01T10:05:00Z")
    _encode(folder / "B_hd.mp4", "1920x1080", 30, 9, created="2026-09-01T10:00:00Z")
    _encode(folder / "C_tall.mp4", "1080x1920", 24, 2, created="2026-09-01T09:55:00Z")
    originals = {name: _identity(folder / name) for name in UTTERANCES}
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(timing_engine, "DEFAULT_NEW_HEARING_ENGINE", TIMING_ENGINE_WHISPER)
        patch.setattr(raw_service, "DEFAULT_NEW_HEARING_ENGINE", TIMING_ENGINE_WHISPER)
        made = _trip(folder, originals)
    return Trip(made.service, made.root)


def _online(trip: Trip, monkeypatch: pytest.MonkeyPatch, on: bool = True) -> None:
    monkeypatch.setattr("vlogsrt.env.is_offline", lambda: not on)
    config = trip.service.settings(trip.root)
    mode = "online" if on else "offline"
    trip.service.update_settings(trip.root, replace(config, network_mode=mode))


def _export(trip: Trip, digest: str | None = None, client: object | None = None) -> dict:
    handle = trip.service.start_render_export(trip.root, digest, client)
    record = _wait_terminal(trip.service, trip.root, handle.job_id)
    assert record.status == "succeeded", record.error
    latest = trip.service.latest_render_export(trip.root)
    assert latest is not None
    return latest


def _timelines(path: Path) -> list[str]:
    return [line for line in path.read_text(encoding="utf-8").splitlines() if " --> " in line]


def test_1_off_by_default_writes_no_english(trip: Trip) -> None:
    assert trip.service.english_subtitles(trip.root) is False
    latest = _export(trip)
    assert latest["english_srt"] is None and latest["english"] == {"status": "off"}
    assert not (Path(latest["folder"]) / "draft.en.srt").exists()


def test_2_quote_is_free_and_counts_only_untranslated_sources(trip: Trip) -> None:
    assert trip.service.set_english_subtitles(trip.root, True) is True
    quote = trip.service.english_quote(trip.root)
    lines = sum(len(rows) for rows in UTTERANCES.values())
    assert (quote.sources, quote.missing_sources, quote.missing_lines) == (3, 3, lines)
    assert quote.needs_approval and quote.priced and quote.usd is not None
    assert quote.output_tokens == 3 * english_track.TRANSLATION.max_output_tokens
    assert "영상 3개" in quote.text and f"자막 {lines}줄" in quote.text
    assert len(quote.keys) == 3


def test_3_a_wrong_digest_or_offline_is_refused_before_any_request(
    trip: Trip, monkeypatch: pytest.MonkeyPatch
) -> None:
    translator = _Translator()
    _online(trip, monkeypatch)
    with pytest.raises(raw_service.RawWorkspaceError, match="견적이 바뀌었습니다"):
        trip.service.start_render_export(trip.root, "0" * 64, _client(translator))
    quote = trip.service.english_quote(trip.root)
    _online(trip, monkeypatch, on=False)
    with pytest.raises(raw_service.RawWorkspaceError, match="VLOGSRT_OFFLINE"):
        trip.service.start_render_export(trip.root, quote.digest, _client(translator))
    monkeypatch.setattr("vlogsrt.env.is_offline", lambda: False)  # 환경은 열고 작업공간만 닫힘
    with pytest.raises(raw_service.RawWorkspaceError, match="network_mode"):
        trip.service.start_render_export(trip.root, quote.digest, _client(translator))
    with pytest.raises(raw_service.RawWorkspaceError, match="Gemini 연결"):
        _online(trip, monkeypatch)
        trip.service.start_render_export(trip.root, quote.digest, None)
    assert translator.sent == []


def test_4_a_failed_translation_keeps_the_korean_draft_and_names_the_sources(
    trip: Trip, monkeypatch: pytest.MonkeyPatch
) -> None:
    _online(trip, monkeypatch)
    quote = trip.service.english_quote(trip.root)
    translator = _Translator(broken=True)
    latest = _export(trip, quote.digest, _client(translator))
    assert len(translator.sent) == 3
    assert latest["english_srt"] is None and latest["english"]["status"] == "failed"
    names = sorted(row["name"] for row in latest["english"]["untranslated"])
    assert names == sorted(UTTERANCES)
    assert all("빠졌다" in row["reason"] for row in latest["english"]["untranslated"])
    assert Path(latest["srt"]).is_file()
    folder = trip.root / ANALYSIS_ROOT / tio.TRANSLATION_DIRNAME
    assert len(list(folder.glob("*.failed.json"))) == 3
    assert trip.service.english_quote(trip.root).missing_sources == 3  # 실패는 산 것이 아니다


def test_5_approved_export_writes_english_with_identical_times(
    trip: Trip, monkeypatch: pytest.MonkeyPatch
) -> None:
    _online(trip, monkeypatch)
    korean_only = Path(_korean_reference(trip)).read_bytes()
    quote = trip.service.english_quote(trip.root)
    translator = _Translator()
    latest = _export(trip, quote.digest, _client(translator))
    assert len(translator.sent) == 3
    assert translator.sent[0]["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "LOW"}
    english, korean = Path(latest["english_srt"]), Path(latest["srt"])
    assert english.name == "draft.en.srt" and latest["english"]["status"] == "written"
    assert _timelines(english) == _timelines(korean) and _timelines(english)
    body = english.read_text(encoding="utf-8")
    assert "EN " in body and not any("가" <= ch <= "힣" for ch in body)
    assert korean.read_bytes() == korean_only  # 한국어는 바이트 그대로
    report = json.loads((english.parent / "render_report.json").read_text(encoding="utf-8"))
    assert report["outputs"]["english_srt"] == "draft.en.srt"
    assert report["english"]["cues"] == len(_timelines(korean))


def _korean_reference(trip: Trip) -> str:
    """영어를 켜기 전에 낸 첫 초안의 한국어 SRT — 같은 계획이면 바이트가 같아야 한다."""
    folders = sorted((trip.root / "export").iterdir())
    return str(folders[0] / "draft.srt")


def test_6_the_next_export_never_rebuys(trip: Trip, monkeypatch: pytest.MonkeyPatch) -> None:
    _online(trip, monkeypatch)
    quote = trip.service.english_quote(trip.root)
    assert not quote.needs_approval and quote.usd is None and "무료" in quote.text
    translator = _Translator()
    latest = _export(trip, None, _client(translator))
    assert translator.sent == [] and latest["english"]["status"] == "written"


def test_7_turning_it_off_goes_back_to_korean_only(trip: Trip) -> None:
    assert trip.service.set_english_subtitles(trip.root, False) is False
    latest = _export(trip)
    assert latest["english_srt"] is None and latest["english"] == {"status": "off"}
