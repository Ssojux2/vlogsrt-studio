"""macOS 밖의 경로 선택 창 — 표준 라이브러리 tkinter 한 장을 **따로 띄운 프로세스**에서 연다(W-46a).

서버 프로세스에서 Tk를 열지 않는다: Tk는 그것을 만든 스레드에 묶이고, 창이 떠 있는 동안 그 스레드가
멈춘다. 그래서 `ui.chooser.tkinter_path_chooser`가 이 파일을 `python -I <이 파일> <종류>`로
띄우고, 이 파일은 창 하나를 열어 고른 경로를 표준 출력의 JSON 한 줄(ASCII)로 돌려주고 끝난다.

`-I`로 뜨므로 `ui` 패키지를 import하지 못한다 — 표준 라이브러리만 쓰고, 종류 이름은 `ui.chooser`와
같은 글자로 둔다(시험이 맞춰 본다).
"""

from __future__ import annotations

import json
import sys

WORKSPACE_KIND = "workspace"
FOLDER_KIND = "folder"
VIDEO_KIND = "video"
SRT_KIND = "srt"
KINDS = (WORKSPACE_KIND, FOLDER_KIND, VIDEO_KIND, SRT_KIND)
TITLES = {
    WORKSPACE_KIND: "프로젝트 폴더를 고르세요",
    FOLDER_KIND: "촬영 영상 폴더를 고르세요",
    VIDEO_KIND: "영상 파일을 고르세요",
    SRT_KIND: "SRT 자막 파일을 고르세요",
}
FILE_TYPES = {
    VIDEO_KIND: (("영상", "*.mp4 *.mov *.m4v *.mkv *.webm *.avi *.mp3 *.wav *.m4a"),),
    SRT_KIND: (("SRT 자막", "*.srt"),),
}
USAGE_EXIT = 2


def _dialog(kind: str) -> str | None:
    """창 하나 — 폴더 종류는 폴더 고르기, 파일 종류는 파일 고르기. 취소는 None."""
    import tkinter
    from tkinter import filedialog

    host = tkinter.Tk()
    host.withdraw()
    host.attributes("-topmost", True)  # 브라우저 창 뒤에 숨지 않게
    try:
        if kind in FILE_TYPES:
            chosen = filedialog.askopenfilename(
                parent=host, title=TITLES[kind], filetypes=FILE_TYPES[kind]
            )
        else:
            chosen = filedialog.askdirectory(parent=host, title=TITLES[kind], mustexist=True)
    finally:
        host.destroy()
    return str(chosen) if chosen else None


def _emit(payload: dict[str, object]) -> None:
    # ASCII JSON — Windows 콘솔 인코딩(cp949 등)과 무관하게 부모가 그대로 읽는다.
    sys.stdout.write(json.dumps(payload, ensure_ascii=True) + "\n")
    sys.stdout.flush()


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in KINDS:
        _emit({"error": f"모르는 선택기 종류: {argv[1:]!r}"})
        return USAGE_EXIT
    try:
        _emit({"path": _dialog(argv[1])})
    except Exception as error:  # noqa: BLE001 - 창을 못 열면 사유를 부모에게 넘긴다(화면이 보인다)
        _emit({"error": f"{type(error).__name__}: {error}"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
