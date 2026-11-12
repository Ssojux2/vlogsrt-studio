"""W-63(D114) — 한국어 SRT → 영어 SRT: 읽기 · 승인 문 · 번역(가짜 Gemini) · 같은 시각으로 쓰기.

시험이 못 박는 것:

- 영어 SRT는 입력과 큐 수·번호 차례·(시작, 끝) 밀리초가 같고 글만 영어다 — 여러 줄 큐는 한 줄로
  보내고, 빈 큐는 보내지 않고 같은 시각의 빈 큐로 남고, 겹친 큐도 고치지 않는다(규칙 1·3·9).
- 입력 SRT는 바이트 그대로다. `<이름>.en.srt`가 이미 있고 글이 다르면 덮지 않고 새 이름으로 쓴다.
- 같은 글의 SRT(시각만 고친 것 포함)는 다시 사지 않는다(요청 0회). 글을 고치면 다시 산다.
- 깨진 SRT · UTF-8 아님 · 옮길 글 없음 · 오프라인 · network_mode · 클라이언트 없음은 요청 전에
  닫는다.
- 응답이 한 줄을 빠뜨리면 영어 SRT를 쓰지 않고 `.failed.json`에 까닭을 남긴다.
- 입력 옆에 쓸 수 없으면 작업공간 `export/english/<시각>/`에 쓴다.

유료 호출은 없다 — 전송 자리에 가짜 opener가 앉는다.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from tests.raw.test_gemini_timing_provider import _client, _Reply
from tests.raw.test_review_workspace import _wait_terminal
from vlogsrt import output
from vlogsrt.project.raw import service as raw_service
from vlogsrt.project.raw import srt_english
from vlogsrt.project.raw import translation_io as tio
from vlogsrt.project.raw.analysis_store import ANALYSIS_ROOT

KOREAN = (
    "1\n00:00:01.000 --> 00:00:02,500\n안녕하세요\n여러분\n\n"
    "2\n00:00:02,400 --> 00:00:04,000\n\n\n"
    "7\n00:00:04,000 --> 00:00:06,07\n바다 보러 왔어요\n"
)
"""`.` 구분자 · 두 자리 밀리초 · 틀린 번호 · 여러 줄 큐 · 가운데 빈 큐 · 앞 큐와 겹친 시각."""


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


def _workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, online: bool = True):
    service = raw_service.RawWorkspaceService()
    root = tmp_path / "ws"
    service.create_workspace(root, "영어 변환")
    monkeypatch.setattr("vlogsrt.env.is_offline", lambda: not online)
    mode = "online" if online else "offline"
    service.update_settings(root, replace(service.settings(root), network_mode=mode))
    return service, root


def _srt(folder: Path, text: str = KOREAN, name: str = "draft.srt") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(text, encoding="utf-8")
    return path


def _convert(service, root: Path, srt: Path, client: object | None) -> dict[str, Any]:
    facts: list[dict[str, Any]] = []
    handle = service.start_srt_translation(root, srt, client, facts.append)
    record = _wait_terminal(service, root, handle.job_id)
    assert record.status == "succeeded", record.error
    assert record.kind == raw_service.SRT_TRANSLATE_JOB_KIND
    return facts[0]


def _times(path: Path) -> list[tuple[float, float]]:
    return [(cue.start, cue.end) for cue in output.parse_srt(path.read_text(encoding="utf-8"))]


def test_english_keeps_every_cue_and_time_of_the_korean_srt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, root = _workspace(tmp_path, monkeypatch)
    srt = _srt(tmp_path / "export")
    before = srt.read_bytes()
    translator = _Translator()
    facts = _convert(service, root, srt, _client(translator))
    english = Path(facts["path"])
    assert english == srt.parent / "draft.en.srt" and facts["reused"] is False
    assert _times(english) == _times(srt) and len(_times(english)) == 3
    cues = output.parse_srt(english.read_text(encoding="utf-8"))
    assert [cue.lines for cue in cues] == [("EN 1 ok",), (), ("EN 3 ok",)]
    assert facts["cues"] == 3 and facts["empty_cues"] == 1 and facts["over_two_lines"] == 0
    sent = json.loads(translator.sent[0]["contents"][0]["parts"][1]["text"])["lines"]
    assert sent == [{"id": "1", "ko": "안녕하세요 여러분"}, {"id": "3", "ko": "바다 보러 왔어요"}]
    assert len(translator.sent) == 1
    assert srt.read_bytes() == before  # 입력은 바이트 그대로


def test_the_same_text_is_never_bought_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, root = _workspace(tmp_path, monkeypatch)
    srt = _srt(tmp_path / "a")
    _convert(service, root, srt, _client(_Translator()))
    assert service.srt_translation_cached(root, srt) is True
    again = _Translator()
    facts = _convert(service, root, srt, None)  # 저장된 번역 — 클라이언트 없이도 된다
    assert facts["reused"] is True and facts["already_there"] is True and again.sent == []
    shifted = _srt(tmp_path / "b", KOREAN.replace("00:00:01.000", "00:00:01,200"))
    assert service.srt_translation_cached(root, shifted) is True  # 시각만 고쳤다
    changed = _srt(tmp_path / "c", KOREAN.replace("바다", "산"))
    assert service.srt_translation_cached(root, changed) is False


def test_an_existing_different_english_file_is_never_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, root = _workspace(tmp_path, monkeypatch)
    srt = _srt(tmp_path / "x", name="여행.ko.srt")
    mine = srt.parent / "여행.en.srt"
    mine.write_text("사람이 고친 영어", encoding="utf-8")
    facts = _convert(service, root, srt, _client(_Translator()))
    written = Path(facts["path"])
    assert written.parent == srt.parent and written != mine
    assert written.name.startswith("여행.en.") and written.suffix == ".srt"
    assert mine.read_text(encoding="utf-8") == "사람이 고친 영어"


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"1\nnot a time\nhi\n", "타임코드"),
        ("1\n00:00:01,000 --> 00:00:02,000\n안녕\n".encode("cp949"), "UTF-8로 저장"),
        (b"1\n00:00:01,000 --> 00:00:02,000\n\n", "옮길 자막 글이 없습니다"),
    ],
)
def test_a_broken_srt_is_refused_before_any_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: bytes, message: str
) -> None:
    service, root = _workspace(tmp_path, monkeypatch)
    srt = tmp_path / "bad.srt"
    srt.write_bytes(payload)
    translator = _Translator()
    with pytest.raises(raw_service.RawWorkspaceError, match=message) as caught:
        service.start_srt_translation(root, srt, _client(translator))
    assert "bad.srt" in str(caught.value) or "옮길" in message
    assert translator.sent == [] and not (tmp_path / "bad.en.srt").exists()


def test_offline_or_no_client_is_refused_before_any_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, root = _workspace(tmp_path, monkeypatch, online=False)
    srt = _srt(tmp_path / "e")
    translator = _Translator()
    with pytest.raises(raw_service.RawWorkspaceError, match="VLOGSRT_OFFLINE"):
        service.start_srt_translation(root, srt, _client(translator))
    monkeypatch.setattr("vlogsrt.env.is_offline", lambda: False)  # 환경은 열고 작업공간만 닫힘
    with pytest.raises(raw_service.RawWorkspaceError, match="network_mode"):
        service.start_srt_translation(root, srt, _client(translator))
    service.update_settings(root, replace(service.settings(root), network_mode="online"))
    with pytest.raises(raw_service.RawWorkspaceError, match="Gemini 연결"):
        service.start_srt_translation(root, srt, None)
    assert translator.sent == [] and not (srt.parent / "draft.en.srt").exists()


def test_a_missing_line_in_the_answer_writes_no_english(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, root = _workspace(tmp_path, monkeypatch)
    srt = _srt(tmp_path / "f")
    handle = service.start_srt_translation(root, srt, _client(_Translator(broken=True)))
    record = _wait_terminal(service, root, handle.job_id)
    assert record.status == "failed" and "빠졌다" in str(record.error)
    assert not (srt.parent / "draft.en.srt").exists()
    folder = root / ANALYSIS_ROOT / tio.TRANSLATION_DIRNAME
    (failed,) = folder.glob("*.failed.json")
    assert json.loads(failed.read_text(encoding="utf-8"))["asset_id"] == "draft.srt"
    assert service.srt_translation_cached(root, srt) is False  # 실패는 산 것이 아니다


def test_an_unwritable_folder_falls_back_to_the_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, root = _workspace(tmp_path, monkeypatch)
    srt = _srt(tmp_path / "g")
    real = srt_english._write_new

    def refuse_beside_input(path: Path, payload: bytes) -> None:
        if path.parent == srt.parent:
            raise PermissionError("읽기 전용")
        real(path, payload)

    monkeypatch.setattr(srt_english, "_write_new", refuse_beside_input)
    facts = _convert(service, root, srt, _client(_Translator()))
    written = Path(facts["path"])
    assert written.name == "draft.en.srt"
    assert written.parent.parent == root / "export" / "english"
    assert _times(written) == _times(srt)


def test_english_names() -> None:
    assert srt_english.english_name(Path("draft.srt")) == "draft.en.srt"
    assert srt_english.english_name(Path("여행.ko.srt")) == "여행.en.srt"
    assert srt_english.english_name(Path("a.b.SRT")) == "a.b.en.SRT"
