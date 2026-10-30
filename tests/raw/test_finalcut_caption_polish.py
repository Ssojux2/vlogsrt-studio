"""D96 — Final Cut으로 나가는 자막 글자를 조판 정리(D30)로 다듬는다(2026-10-27 사용자 지시).

「자막 글자 맨 앞에 띄어쓰기가 들어가 있는 부분 제거 · 마지막 온점도 안 찍히게」. whisper 글자는
앞에 공백을 달고 오고(「 오늘은…」) 문장마다 온점을 찍는다. 원본 자막(작업공간 DB)은 그대로 두고 —
지문·캐시·편집기가 그 글자에 묶여 있다 — **보내는 글자만** `polish.polish_text`로 다듬는다:

- 앞뒤·겹친 공백을 하나로 · 온점을 지운다(숫자 사이 `1.5`는 그대로) · `...` → `…` · 줄 끝 쉼표.
- 부호뿐이라 비게 되는 줄은 원문을 그대로 보낸다(정리는 큐를 비우지 않는다, 규칙 9).
- 원본 자막 캡션과 SRT 타이틀 둘 다. 시각은 건드리지 않는다.
"""

from __future__ import annotations

import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from vlogsrt.project.raw.finalcut_export import caption_text

# D104 — whisper 응답 모양의 재생으로 재는 시험이다. 새로 듣는 엔진을 whisper로 둔다.
pytestmark = pytest.mark.usefixtures("whisper_for_new_hearings")

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")


@pytest.mark.parametrize(
    ("raw", "sent"),
    [
        (" 오늘은 여행을 갑니다.", "오늘은 여행을 갑니다"),
        ("안녕하세요. 오늘은 한강에 왔어요!", "안녕하세요 오늘은 한강에 왔어요!"),
        ("  두 칸   띄어쓰기  ", "두 칸 띄어쓰기"),
        ("1.5초 기다려...", "1.5초 기다려…"),
        ("그런데,", "그런데"),
        ("첫 줄\n 둘째 줄.", "첫 줄\n둘째 줄"),
    ],
)
def test_sent_text_has_no_leading_space_and_no_period(raw: str, sent: str) -> None:
    assert caption_text(raw) == sent


def test_a_line_of_only_marks_is_sent_as_it_was() -> None:
    assert caption_text("...") == "…"
    assert caption_text(" . ") == " . "  # 비게 되면 원문 그대로(규칙 9)


@needs_ffmpeg
def test_exported_captions_and_titles_carry_the_polished_text(tmp_path: Path) -> None:
    from tests.raw.test_auto_cleanup_review_gate import _synthetic_project

    service, root = _synthetic_project(tmp_path)
    stored = [row["text"] for row in _transcript_rows(service, root)]
    assert "오늘은 여행을 갑니다." in stored  # 원본 자막은 그대로다
    captions = ET.parse(service.export_finalcut_timeline(root)).findall(".//caption")
    names = {caption.get("name") for caption in captions}
    assert "오늘은 여행을 갑니다" in names and not any(n.endswith(".") for n in names if n)
    srt = tmp_path / "titles.srt"
    srt.write_text("1\n00:00:00,500 --> 00:00:01,500\n 안녕하세요.\n", encoding="utf-8")
    styles = ET.parse(service.export_finalcut_titles(root, srt)).findall(".//title//text-style")
    assert [style.text for style in styles if style.text] == ["안녕하세요"]


def _transcript_rows(service, root: Path) -> list[dict]:
    (asset,) = service.list_source_assets(root)
    document = service.source_transcript(root, asset.asset_id)
    assert document is not None
    return list(document["utterances"])
