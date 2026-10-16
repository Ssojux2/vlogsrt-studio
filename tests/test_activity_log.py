"""D61 — 서버 터미널 줄을 「기록」 판에서도 읽는다. 터미널로 가는 글은 바이트 그대로다.

`ui/activity_log.py`는 표준 라이브러리만 쓴다 — gradio 없이도 돈다.
"""

from __future__ import annotations

import io
import sys

from ui import activity_log
from ui.activity_log import ActivityLog, TeeStream, captured
from vlogsrt import log


def _tee(limit: int = activity_log.LOG_LINE_LIMIT) -> tuple[io.StringIO, TeeStream, ActivityLog]:
    target = io.StringIO()
    buffer = ActivityLog(limit=limit)
    return target, TeeStream(target, "stderr", buffer), buffer


def test_the_terminal_receives_every_byte_unchanged() -> None:
    target, tee, _ = _tee()
    pieces = [
        "\x1b[33mWARN\x1b[0m 분석 프록시 — 잔재를",
        " 지웠습니다\n부분",
        " 줄\r\n",
        "key sk-abcdefghijklmnop1234 Bearer abc.def\n",
        "",
        "끝",
    ]
    written = [tee.write(piece) for piece in pieces]
    assert target.getvalue() == "".join(pieces)  # 가림은 저장 줄에만 — 터미널은 그대로
    assert written == [len(piece) for piece in pieces]


def test_complete_lines_are_stored_without_ansi_and_partial_lines_wait() -> None:
    _, tee, buffer = _tee()
    tee.write("\x1b[33mWARN\x1b[0m 하나")
    assert buffer.lines_since(0) == ()  # 줄바꿈 전에는 남기지 않는다
    tee.write(" 둘\n\x1b]8;id=1;https://x.test\x1b\\링크\x1b]8;;\x1b\\ 셋\r\n넷")
    lines = buffer.lines_since(0)
    assert [line.text for line in lines] == ["WARN 하나 둘", "링크 셋"]
    assert [line.stream for line in lines] == ["stderr", "stderr"]
    assert lines[0].level == "warn"
    tee.write("\n")
    assert buffer.lines_since(lines[-1].seq)[0].text == "넷"


def test_key_looking_tokens_are_masked_in_stored_lines_only() -> None:
    _, tee, buffer = _tee()
    tee.write("키 sk-proj-AbCdEf0123456789xyz 와 sk-ant-api03-ZZZZZZZZZZZZ 실패\n")
    tee.write("Authorization: Bearer eyJhbGciOi.payload.sig\n")
    tee.write('api_key="abc123secret" x-api-key: q1w2e3r4t5\n')
    texts = [line.text for line in buffer.lines_since(0)]
    assert texts[0] == "키 sk-… 와 sk-… 실패"
    assert texts[1] == "Authorization: Bearer …"
    assert "abc123secret" not in texts[2] and "q1w2e3r4t5" not in texts[2]
    assert "sk-" not in "".join(texts).replace("sk-…", "")


def test_the_buffer_is_bounded_and_sequence_numbers_keep_increasing() -> None:
    _, tee, buffer = _tee(limit=5)
    for number in range(1, 9):
        tee.write(f"줄 {number}\n")
    lines = buffer.lines_since(0)
    assert [line.text for line in lines] == ["줄 4", "줄 5", "줄 6", "줄 7", "줄 8"]
    assert [line.seq for line in lines] == [4, 5, 6, 7, 8]
    assert [line.text for line in buffer.lines_since(6)] == ["줄 7", "줄 8"]
    assert buffer.lines_since(8) == ()
    assert buffer.latest_seq == 8


def test_a_line_without_newline_is_cut_at_the_line_limit() -> None:
    _, tee, buffer = _tee()
    tee.write("가" * (activity_log.LINE_MAX_CHARS * 2 + 5))
    lines = buffer.lines_since(0)
    assert len(lines) == 2  # 끝없는 출력이 버퍼 하나를 키우지 않는다
    assert all(len(line.text) == activity_log.LINE_MAX_CHARS for line in lines)


def test_levels_follow_the_log_prefixes() -> None:
    assert activity_log.line_level("WARN 잔재") == "warn"
    assert activity_log.line_level("  WARNING:  uvicorn") == "warn"
    assert activity_log.line_level("ERR 실패") == "error"
    assert activity_log.line_level("ERROR:    Exception in ASGI application") == "error"
    assert activity_log.line_level("Traceback (most recent call last):") == "error"
    assert activity_log.line_level("› 분석 프록시 준비: a.mov (12.0초)") == "info"


def test_the_tee_delegates_the_stream_protocol() -> None:
    target, tee, _ = _tee()
    tee.flush()
    assert tee.isatty() is False
    assert tee.encoding == target.encoding
    tee.writelines(["a\n", "b\n"])
    assert target.getvalue() == "a\nb\n"


def test_captured_swaps_and_restores_both_streams_and_sees_rich_output() -> None:
    buffer = ActivityLog()
    original = (sys.stdout, sys.stderr)
    with captured(buffer):
        assert isinstance(sys.stdout, TeeStream) and isinstance(sys.stderr, TeeStream)
        log.error("작업 실패 sk-abcdefghijklmnopqrst")
        log.out("결과 줄")
    assert (sys.stdout, sys.stderr) == original
    texts = {(line.stream, line.text) for line in buffer.lines_since(0)}
    assert ("stderr", "ERR 작업 실패 sk-…") in texts
    assert ("stdout", "결과 줄") in texts


def test_lines_since_a_future_sequence_returns_nothing() -> None:
    buffer = ActivityLog()
    buffer.record("stdout", "하나")
    assert buffer.lines_since(99) == ()
    assert buffer.latest_seq == 1


SECRET = "sk-proj-" + "Q7wErTy9UiOp3AsDfGh5JkLz8XcVbNm2" * 3  # 104자 — 폭 40에서 세 줄로 접힌다


def _leaks(secret_body: str, texts: list[str]) -> list[str]:
    """키 몸통의 6자 조각이 저장 줄에 남았는가 — 접힘·잘림 어디서든."""
    joined = "\n".join(texts)
    return [
        secret_body[i : i + 6]
        for i in range(len(secret_body) - 5)
        if secret_body[i : i + 6] in joined
    ]


def test_a_key_folded_by_rich_at_width_40_is_masked_on_every_line() -> None:
    from rich.console import Console

    target = io.StringIO()
    buffer = ActivityLog()
    console = Console(file=TeeStream(target, "stderr", buffer), width=40, color_system=None)
    console.print(f"[red]ERR[/red] 키 실패 {SECRET} 다음 글")
    console.print("Authorization: Bearer " + "eyJhbGciOiJIUzI1NiJ9.payload-part." * 3)
    texts = [line.text for line in buffer.lines_since(0)]
    assert len(texts) >= 4  # 정말 접혔다
    assert _leaks(SECRET[3:], texts) == []
    assert _leaks("eyJhbGciOiJIUzI1NiJ9.payload-part." * 3, texts) == []
    assert SECRET in target.getvalue().replace("\n", "")  # 터미널은 그대로
    assert buffer.lines_since(0)[0].level == "error"  # 색은 가리기 전 머리로 정한다


def test_a_key_cut_at_the_line_limit_is_masked_in_both_pieces() -> None:
    _, tee, buffer = _tee()
    body = "B" * (activity_log.LINE_MAX_CHARS + 500)
    tee.write("가" * 500 + " sk-" + body + "\n")  # 끝쪽 절반에 빈칸이 없어 상한에서 자른다
    texts = [line.text for line in buffer.lines_since(0)]
    assert len(texts) >= 2
    assert _leaks(body, texts) == []


def test_long_lines_prefer_to_break_at_a_space() -> None:
    _, tee, buffer = _tee()
    words = ("낱말 " * activity_log.LINE_MAX_CHARS).rstrip()
    tee.write(words + "\n")
    texts = [line.text for line in buffer.lines_since(0)]
    assert all(len(text) <= activity_log.LINE_MAX_CHARS for text in texts)
    assert all(not text.startswith("말") for text in texts)  # 낱말 한가운데서 자르지 않았다


def test_a_line_ending_in_a_key_does_not_hide_an_unrelated_word_colour() -> None:
    _, tee, buffer = _tee()
    tee.write("키 sk-abcdefghijklmnop\nWARN 다음 줄\n")
    second = buffer.lines_since(1)[0]
    assert second.level == "warn"  # 머리 낱말이 덜 가려질 수는 있어도 색은 지킨다


def test_carriage_return_progress_keeps_only_its_last_state() -> None:
    target, tee, buffer = _tee()
    for percent in range(0, 101, 10):
        tee.write(f"\r진행 {percent}%")
    tee.write("\r\n")
    tee.write("윈도 줄바꿈\r")
    tee.write("\n")
    assert [line.text for line in buffer.lines_since(0)] == ["진행 100%", "윈도 줄바꿈"]
    assert tee._pending == ""
    assert "진행 0%" in target.getvalue()  # 터미널은 그대로


def test_every_buffer_has_its_own_boot_id() -> None:
    first, second = ActivityLog(), ActivityLog()
    assert first.boot_id != second.boot_id
    assert first.boot_id == first.boot_id and len(first.boot_id) == 16
