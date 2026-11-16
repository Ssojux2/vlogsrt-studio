"""UI 서버가 부르는 순수 도움말 — **gradio를 import하지 않는다.**

옛 단일 영상 자막 화면(경로 해석 · whisper 파이프라인 호출)은 whisper와 함께 지웠다(D163). 남은
것은 서버 포트 고르기와 사람에게 보일 오류 하나다.
"""

from __future__ import annotations

import socket


class UiError(RuntimeError):
    """사람에게 그대로 보여줄 오류. 화면에서는 로그 한 줄로 나간다."""


def pick_port(preferred: int, attempts: int = 20) -> int:
    """이미 쓰는 포트면 옆으로 비켜간다.

    앞서 띄운 UI가 남아 있는 일이 흔한데, 그때마다 프로세스를 죽이라고 하는 것보다
    다음 포트로 뜨고 알려주는 편이 낫다.
    """
    for offset in range(attempts):
        candidate = preferred + offset
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("127.0.0.1", candidate))
            except OSError:
                continue
        return candidate
    raise UiError(
        f"{preferred}부터 {preferred + attempts - 1}까지 빈 포트가 없습니다. "
        "--port 로 다른 번호를 지정하세요."
    )
