"""Final Cut이 FCPXML에 적는 철자 표 — 표준 라이브러리만 쓰는 값 모음.

입력 합성(`clips.py`)과 전체 타임라인 보내기(`raw/finalcut_timeline.py`, D56)가 같은 철자를
쓴다. 표가 `clips.py`에 있으면 순수 빌더가 그것을 import하는 순간 ffprobe 경계(`preprocess`)와
내용 해시(`media`)까지 따라 들어온다 — 그래서 값만 여기로 떼어 둔다.
"""

from __future__ import annotations

from fractions import Fraction

# FCPXML `format@name`의 프레임 레이트 꼬리표. Final Cut이 쓰는 철자 그대로다 —
# 29.97은 `2997`, 23.976은 `2398`이다.
FRAME_RATE_TAGS: dict[Fraction, str] = {
    Fraction(1001, 24000): "2398",
    Fraction(1, 24): "24",
    Fraction(1, 25): "25",
    Fraction(1001, 30000): "2997",
    Fraction(1, 30): "30",
    Fraction(1, 50): "50",
    Fraction(1001, 60000): "5994",
    Fraction(1, 60): "60",
}

# (primaries, transfer, matrix) → FCPXML `colorSpace` 철자. ffprobe가 부르는 이름과
# Final Cut이 적는 이름이 달라서 표로 잇는다. 표에 없는 값을 기본값으로 밀어 넣으면 그
# 추측이 타임라인 선언이 된다 — 입력 합성은 거부하고, 타임라인 보내기는 생략하고 보고한다.
COLOR_SPACES: dict[tuple[str, str, str], str] = {
    ("bt709", "bt709", "bt709"): "1-1-1 (Rec. 709)",
    ("smpte170m", "bt709", "smpte170m"): "6-1-6 (Rec. 601 (NTSC))",
    ("bt470bg", "bt709", "bt470bg"): "5-1-6 (Rec. 601 (PAL))",
    ("bt2020", "arib-std-b67", "bt2020nc"): "9-18-9 (Rec. 2020 HLG)",
    ("bt2020", "smpte2084", "bt2020nc"): "9-16-9 (Rec. 2020 PQ)",
}

# 시퀀스 `audioLayout`·`audioRate`의 철자 — DTD가 받는 값 가운데 이 프로젝트가 아는 것만.
AUDIO_LAYOUTS: dict[int, str] = {1: "mono", 2: "stereo"}
AUDIO_RATE_TAGS: dict[int, str] = {44100: "44.1k", 48000: "48k"}
