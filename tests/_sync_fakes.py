"""자막 시각 확인 시험의 공용 재료(D55) — 톤 덩어리 원본·에너지 VAD 가짜·SRT 쓰기.

가짜 VAD는 onnx `run` 계약(입력 `input`·`state`·`sr` → [확률 (1,1) float32, 상태
(2,1,128) float32])을 그대로 지킨다 — 창 끝 512표본의 RMS가 문턱을 넘으면 말소리다.
Silero는 순수 톤을 말로 보지 않으므로 합성 원본에는 이 가짜를 쓴다.
"""

from __future__ import annotations

import math
import subprocess
import sys
import types
from fractions import Fraction
from pathlib import Path
from typing import Any

BURSTS: tuple[tuple[str, str], ...] = (
    ("1.0", "2.6"),
    ("3.4", "4.6"),
    ("5.9", "8.0"),
    ("9.1", "10.0"),
    ("11.2", "13.5"),
    ("14.6", "15.4"),
    ("16.8", "19.0"),
    ("20.1", "21.3"),
    ("22.9", "24.0"),
    ("25.0", "27.2"),
    ("28.4", "29.3"),
    ("30.6", "32.8"),
    ("33.5", "34.6"),
    ("36.0", "37.4"),
)
SECONDS = 40
ENERGY_SPEECH = 0.05


def bursts(shift: str = "0") -> list[tuple[Fraction, Fraction]]:
    """말 덩어리 — `shift`만큼 뒤로(+) 민 시각."""
    delta = Fraction(shift)
    return [(Fraction(a) + delta, Fraction(b) + delta) for a, b in BURSTS]


def encode(args: list[str]) -> None:
    completed = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-400:]


def make_media(path: Path, *, audio_delay: str = "0", silent: bool = False) -> Path:
    """회색 영상 + 스테레오 AAC 톤 덩어리(또는 무음) MOV. `audio_delay`는 오디오 원점이다."""
    gate = "+".join(f"between(t,{a},{b})" for a, b in BURSTS)
    left = "0" if silent else f"0.5*sin(2*PI*330*t)*({gate})"
    right = "0" if silent else f"0.5*sin(2*PI*550*t)*({gate})"
    audio = f"aevalsrc=exprs='{left}|{right}':s=48000:d={SECONDS}"
    delay = ["-itsoffset", audio_delay] if audio_delay != "0" else []
    path.parent.mkdir(parents=True, exist_ok=True)
    encode(
        [
            "-f", "lavfi", "-i", f"color=c=gray:s=64x36:r=10:d={SECONDS}",
            *delay,
            "-f", "lavfi", "-i", audio,
            "-map", "0:v", "-map", "1:a",
            "-c:v", "mpeg4", "-c:a", "aac", "-b:a", "96k",
            str(path),
        ]
    )  # fmt: skip
    return path


def srt_text(spans: list[tuple[Fraction, Fraction]]) -> str:
    def stamp(value: Fraction) -> str:
        millis = round(value * 1000)
        hours, rest = divmod(millis, 3600000)
        minutes, rest = divmod(rest, 60000)
        seconds, millis = divmod(rest, 1000)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"

    blocks = [
        f"{index}\n{stamp(start)} --> {stamp(end)}\n말 {index}\n"
        for index, (start, end) in enumerate(spans, start=1)
    ]
    return "\n".join(blocks)


class EnergySession:
    """onnx 세션 가짜 — 창 RMS로 말소리 확률을 낸다. 부른 횟수를 센다."""

    def __init__(self) -> None:
        self.calls = 0

    def run(self, _names: Any, feeds: dict[str, Any]) -> list[Any]:
        import numpy  # 진짜든 `numpy_or_fake`의 가짜든 — 같은 계약만 쓴다

        self.calls += 1
        window = [float(value) for value in feeds["input"].tolist()[0][-512:]]
        energy = math.sqrt(sum(value * value for value in window) / len(window))
        probability = 0.95 if energy > ENERGY_SPEECH else 0.02
        return [numpy.array([[probability]], dtype=numpy.float32), feeds["state"]]


def energy_factory(project: Any = None) -> Any:
    """`sync_report.default_session_factory` 자리에 꽂는 공장 — 세션은 하나다."""
    session = EnergySession()
    return lambda: session


# ─── numpy — 있으면 진짜, 없으면 최소 가짜(tests/project/test_vad_inference.py와 같은 선) ───


class FakeArray:
    """최소 ndarray — 모양·타입·tolist·첨자만. VAD 추론의 계약 검사에 충분하다."""

    __slots__ = ("data", "shape", "dtype")

    def __init__(self, data: object, dtype: object = None) -> None:
        self.data = data
        dims: list[int] = []
        level = data
        while isinstance(level, list):
            dims.append(len(level))
            if not level:
                break
            level = level[0]
        self.shape = tuple(dims)
        self.dtype = str(dtype)

    def __getitem__(self, index: int) -> object:
        return self.data[index]  # type: ignore[index]

    def tolist(self) -> object:
        return self.data


def install_fake_numpy(monkeypatch: Any) -> types.ModuleType:
    """VAD 추론이 쓰는 최소 numpy — zeros·array·asarray와 dtype 이름만 산다."""
    module = types.ModuleType("numpy")
    module.float32 = "float32"  # type: ignore[attr-defined]
    module.int64 = "int64"  # type: ignore[attr-defined]

    def nested(dims: tuple[int, ...], value: float) -> object:
        return value if not dims else [nested(dims[1:], value) for _ in range(dims[0])]

    def zeros(shape: Any, dtype: object = None) -> FakeArray:
        return FakeArray(nested(tuple(int(dim) for dim in shape), 0.0), dtype)

    module.zeros = zeros  # type: ignore[attr-defined]
    module.array = FakeArray  # type: ignore[attr-defined]
    module.asarray = FakeArray  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "numpy", module)
    return module


def numpy_or_fake(monkeypatch: Any) -> types.ModuleType:
    """진짜 numpy가 있으면 그대로, 없는 개발 환경(VAD extra 없음)에서만 가짜를 깐다."""
    try:
        import numpy
    except ImportError:
        return install_fake_numpy(monkeypatch)
    return numpy
