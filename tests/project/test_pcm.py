"""원본 PCM 참조 — 저장·키·슬라이스·자를 자리 (P2-02).

참조는 P2-01이 잰 원점을 그대로 믿고 저장 PCM을 내용으로 주소를 매긴다. 슬라이스는
`[start, end)` 프레임을 **바이트 그대로** 옮긴다 — 제거된 오디오의 덧붙임도 문맥도
없다는 것을 패턴 바이트로 증명한다. ffmpeg 경계는 전부 P2-01 흉내로 대신하고, 실측
증거는 합성 미디어를 만들어 잰다(미디어는 tmp_path 안에서만 만든다).
"""

from __future__ import annotations

import array
import hashlib
import json
import shutil
import subprocess
import wave
from dataclasses import dataclass, fields
from fractions import Fraction
from pathlib import Path

import pytest

from vlogsrt import preprocess
from vlogsrt.project import pcm
from vlogsrt.project.pcm import (
    AudioPolicy,
    PcmReference,
    SampleRange,
    build_reference,
    quiet_sample,
    retained_sample_range,
    sample_file_time,
    write_wav_slice,
)

# ─────────────────────────────────────────────────────────────
#  재료 — 패턴 PCM·참조·추출 흉내
# ─────────────────────────────────────────────────────────────


def _frame_value(frame: int, channel: int) -> int:
    """프레임·채널마다 다른 값 — 슬라이스가 남의 프레임을 실어 오면 바로 드러난다."""
    return ((frame * 3 + channel * 977) % 28001) - 14000


def _pattern_pcm(frames: int, channels: int) -> bytes:
    samples = array.array("h")
    for frame in range(frames):
        for channel in range(channels):
            samples.append(_frame_value(frame, channel))
    return samples.tobytes()


def _hand_reference(
    path: Path,
    pcm_bytes: bytes,
    *,
    channels: int = 1,
    origin: Fraction = Fraction(0),
    sample_count: int | None = None,
) -> PcmReference:
    """파일을 직접 쓰고 참조를 손으로 만든다 — 순수 함수 시험용."""
    path.write_bytes(pcm_bytes)
    if sample_count is None:
        sample_count = len(pcm_bytes) // (2 * channels)
    return PcmReference(
        source_id="asset-1",
        original_stem="demo",
        original_sha256="a" * 64,
        path=path,
        pcm_sha256=hashlib.sha256(pcm_bytes).hexdigest(),
        analysis_key="hand-built",
        pcm_sample0_file_time=origin,
        sample_rate=16000,
        channels=channels,
        sample_count=sample_count,
    )


def _sidecar(destination: Path) -> Path:
    """완성 참조의 곁 기록 — 재사용 판정의 전부가 여기 있다."""
    return destination.with_name(destination.name + ".json")


@dataclass
class _FakeRecorder:
    """흉내 낸 추출이 받은 것·돌려준 것. `asked`는 마지막 호출의 인자다."""

    calls: list[preprocess.PcmExtraction]
    asked: dict[str, int]


def _fake_extract(
    monkeypatch: pytest.MonkeyPatch,
    pcm_bytes: bytes,
    *,
    origin: Fraction = Fraction(0),
    channels: int = 2,
    start_time: Fraction | None = None,
    sample_width: int = 2,
    fail_after_write: bool = False,
) -> _FakeRecorder:
    """P2-01 추출을 흉내 낸다. 원본 주파수·전역 번호는 실측과 같은 자리에 둔다."""
    recorder = _FakeRecorder(calls=[], asked={})

    def extract(
        source: Path, destination: Path, *, stream_index: int, sample_rate: int = 16000
    ) -> preprocess.PcmExtraction:
        recorder.asked = {"stream_index": stream_index, "sample_rate": sample_rate}
        destination.write_bytes(pcm_bytes)
        if fail_after_write:
            raise RuntimeError("추출이 반쯤 죽었다")
        extraction = preprocess.PcmExtraction(
            path=destination,
            stream=preprocess.AudioStreamInfo(
                stream_index=stream_index + 1,  # 전역 번호는 순번과 어긋난다
                start_time=origin if start_time is None else start_time,
                sample_rate=44100,  # 원본 주파수 — 참조의 16000과는 다르다
                channels=channels,
            ),
            sample_count=len(pcm_bytes) // (2 * channels),
            sample_width=sample_width,
            pcm_sample0_file_time=origin,
        )
        recorder.calls.append(extraction)
        return extraction

    monkeypatch.setattr(pcm, "extract_pcm_s16le", extract)
    return recorder


# ─────────────────────────────────────────────────────────────
#  입력 검증 — 정책·정체성
# ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("bad", [-1, True, False, 1.0, "0"])
def test_policy_rejects_an_ordinal_that_is_not_a_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad: object
) -> None:
    """오디오 순번은 0 이상 정수다 — bool·float·문자열은 세는 수가 아니다."""
    recorder = _fake_extract(monkeypatch, b"")
    with pytest.raises(ValueError, match="순번"):
        build_reference(
            "asset-1",
            "demo",
            tmp_path / "in.mov",
            "a" * 64,
            AudioPolicy(stream_index=bad),  # type: ignore[arg-type]
            tmp_path / "ref.pcm",
        )
    assert recorder.calls == []


@pytest.mark.parametrize("bad", [44100, 0, -16000, True, 16000.0])
def test_policy_accepts_only_the_contract_pcm_rate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad: object
) -> None:
    """저장 PCM은 16 kHz s16le다(P2-01 계약) — 다른 주파수는 저장 형식이 아니다."""
    recorder = _fake_extract(monkeypatch, b"")
    with pytest.raises(ValueError, match="16000"):
        build_reference(
            "asset-1",
            "demo",
            tmp_path / "in.mov",
            "a" * 64,
            AudioPolicy(sample_rate=bad),  # type: ignore[arg-type]
            tmp_path / "ref.pcm",
        )
    assert recorder.calls == []


@pytest.mark.parametrize("bad", [1, 4, 0, True, 2.0])
def test_policy_accepts_only_the_contract_pcm_width(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad: object
) -> None:
    """s16le의 폭은 2바이트다 — 폭이 다르면 슬라이스 바이트 수 계산이 어긋난다."""
    recorder = _fake_extract(monkeypatch, b"")
    with pytest.raises(ValueError, match="s16le|폭"):
        build_reference(
            "asset-1",
            "demo",
            tmp_path / "in.mov",
            "a" * 64,
            AudioPolicy(sample_width=bad),  # type: ignore[arg-type]
            tmp_path / "ref.pcm",
        )
    assert recorder.calls == []


@pytest.mark.parametrize("field_name", ["source_id", "original_stem", "original_sha256"])
def test_identity_strings_must_be_real(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field_name: str
) -> None:
    """정체성 문자열이 비면 나중 조인이 경로·파일이름으로 지어낸다 — 못 넘긴다."""
    recorder = _fake_extract(monkeypatch, b"")
    names = {"source_id": "asset-1", "original_stem": "demo", "original_sha256": "a" * 64}
    names[field_name] = ""
    with pytest.raises(ValueError, match=field_name):
        build_reference(
            names["source_id"],
            names["original_stem"],
            tmp_path / "in.mov",
            names["original_sha256"],
            AudioPolicy(),
            tmp_path / "ref.pcm",
        )
    assert recorder.calls == []


# ─────────────────────────────────────────────────────────────
#  참조 만들기 — 저장·원점·키
# ─────────────────────────────────────────────────────────────


def test_build_persists_the_pcm_and_the_measured_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """만들면 PCM이 목적지에 남고, 참조는 잰 값들로만 차 있다."""
    pcm_bytes = _pattern_pcm(8000, channels=2)
    recorder = _fake_extract(monkeypatch, pcm_bytes, channels=2)
    destination = tmp_path / "ref.pcm"

    reference = build_reference(
        "asset-1", "demo", tmp_path / "in.mov", "b" * 64, AudioPolicy(), destination
    )

    assert recorder.calls, "추출을 한 번은 불러야 한다"
    assert destination.read_bytes() == pcm_bytes
    assert reference.path == destination
    assert reference.source_id == "asset-1"
    assert reference.original_stem == "demo"
    assert reference.original_sha256 == "b" * 64
    assert reference.pcm_sha256 == hashlib.sha256(pcm_bytes).hexdigest()
    assert reference.sample_count == 8000
    assert reference.channels == 2
    assert reference.sample_rate == 16000
    assert reference.pcm_sample0_file_time == Fraction(0)
    assert len(reference.analysis_key) == 64
    assert _sidecar(destination).is_file()


def test_the_audio_ordinal_is_handed_to_p2_01_as_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """순번은 P2-01에 그대로 간다 — 전역 번호는 P2-01이 ffprobe로 얻는 것이다."""
    recorder = _fake_extract(monkeypatch, _pattern_pcm(100, channels=2), channels=2)
    build_reference(
        "asset-1",
        "demo",
        tmp_path / "in.mov",
        "b" * 64,
        AudioPolicy(stream_index=2),
        tmp_path / "ref.pcm",
    )
    assert recorder.asked == {"stream_index": 2, "sample_rate": 16000}


def test_reference_keeps_the_measured_origin_not_the_stream_start_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """원점은 변환 후 첫 프레임의 실측 pts다 — 컨테이너 start_time과 다르다.

    AAC/MKV 실측: start_time은 −33/125인데 디코드 원점은 −1/5다. 참조가 이 둘을
    같은 것으로 취급하면 64ms가 자막 전체에 실린다.
    """
    recorder = _fake_extract(
        monkeypatch,
        _pattern_pcm(4000, channels=2),
        channels=2,
        origin=Fraction(-1, 5),
        start_time=Fraction(-33, 125),
    )
    reference = build_reference(
        "asset-1", "demo", tmp_path / "in.mkv", "b" * 64, AudioPolicy(), tmp_path / "ref.pcm"
    )

    assert recorder.calls, "추출이 일어났다"
    assert reference.pcm_sample0_file_time == Fraction(-1, 5)
    assert reference.pcm_sample0_file_time != Fraction(-33, 125)
    # 참조에는 start_time 필드 자체가 없다 — 들여올 수도 없다.
    assert "start_time" not in [field.name for field in fields(PcmReference)]


def test_analysis_key_is_stable_for_the_same_content_and_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """같은 내용·같은 정책이면 키가 같다 — 재실행해도 흔들리지 않는다."""
    _fake_extract(
        monkeypatch, _pattern_pcm(1000, channels=2), channels=2, origin=Fraction(242, 125)
    )
    first = build_reference(
        "asset-1", "demo", tmp_path / "in.mov", "b" * 64, AudioPolicy(), tmp_path / "a.pcm"
    )
    second = build_reference(
        "asset-1", "demo", tmp_path / "in.mov", "b" * 64, AudioPolicy(), tmp_path / "b.pcm"
    )
    assert first.analysis_key == second.analysis_key


def test_analysis_key_ignores_names_paths_and_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """키는 내용이다 — source_id·이름·경로가 키를 바꾸면 안 된다."""
    _fake_extract(monkeypatch, _pattern_pcm(1000, channels=1), channels=1)
    first = build_reference(
        "asset-1", "ep01", tmp_path / "one.mov", "b" * 64, AudioPolicy(), tmp_path / "one.pcm"
    )
    second = build_reference(
        "asset-2", "ep77", tmp_path / "two.mov", "b" * 64, AudioPolicy(), tmp_path / "two.pcm"
    )
    assert first.analysis_key == second.analysis_key


@pytest.mark.parametrize(
    ("what", "delta"),
    [
        ("원본 해시", {"original_sha256": "c" * 64}),
        ("원점", {"origin": Fraction(-1, 5)}),
        ("PCM 바이트", {"pcm_bytes": _pattern_pcm(1000, channels=1)[::-1]}),
        ("채널", {"pcm_bytes": _pattern_pcm(500, channels=2), "channels": 2}),
        ("오디오 순번", {"policy": AudioPolicy(stream_index=1)}),
    ],
)
def test_analysis_key_changes_with_every_clock_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, what: str, delta: dict[str, object]
) -> None:
    """원본 해시·원점·PCM 해시·채널·오디오 순번 중 하나라도 바뀌면 키가 바뀐다."""
    base = {
        "original_sha256": "b" * 64,
        "origin": Fraction(242, 125),
        "pcm_bytes": _pattern_pcm(1000, channels=1),
        "channels": 1,
        "policy": AudioPolicy(),
    }

    def build(inputs: dict[str, object], destination: Path) -> PcmReference:
        _fake_extract(
            monkeypatch,
            inputs["pcm_bytes"],  # type: ignore[arg-type]
            channels=inputs["channels"],  # type: ignore[arg-type]
            origin=inputs["origin"],  # type: ignore[arg-type]
        )
        return build_reference(
            "asset-1",
            "demo",
            tmp_path / "in.mov",
            inputs["original_sha256"],  # type: ignore[arg-type]
            inputs["policy"],  # type: ignore[arg-type]
            destination,
        )

    first = build(base, tmp_path / "a.pcm")
    varied = dict(base)
    varied.update(delta)
    second = build(varied, tmp_path / "b.pcm")
    assert first.analysis_key != second.analysis_key, what


def test_analysis_key_carries_the_whole_policy() -> None:
    """주파수·폭도 키 재료다 — 저장 가능한 값이 하나뿐이라 공식으로 직접 증명한다."""
    frozen = AudioPolicy()
    wide = AudioPolicy(sample_rate=44100, sample_width=4)
    origin = Fraction(-1, 5)
    assert (
        len(
            {
                pcm._analysis_key("b" * 64, frozen, origin, "d" * 64, 2, 16000),
                pcm._analysis_key("b" * 64, wide, origin, "d" * 64, 2, 44100),
                pcm._analysis_key("b" * 64, wide, origin, "d" * 64, 2, 16000),
            }
        )
        == 3
    )


# ─────────────────────────────────────────────────────────────
#  재사용 — 검증된 완성물만
# ─────────────────────────────────────────────────────────────


def test_rebuild_reuses_a_verified_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """곁 기록·해시·정책이 전부 맞으면 다시 뽑지 않는다."""
    pcm_bytes = _pattern_pcm(1000, channels=2)
    recorder = _fake_extract(monkeypatch, pcm_bytes, channels=2)
    destination = tmp_path / "ref.pcm"

    first = build_reference(
        "asset-1", "demo", tmp_path / "in.mov", "b" * 64, AudioPolicy(), destination
    )
    second = build_reference(
        "asset-1", "demo", tmp_path / "in.mov", "b" * 64, AudioPolicy(), destination
    )

    assert len(recorder.calls) == 1, "검증된 완성물은 다시 뽑지 않는다"
    assert second == first
    assert {path.name for path in tmp_path.iterdir()} == {"ref.pcm", "ref.pcm.json"}


@pytest.mark.parametrize(
    "tamper",
    [
        "pcm_bytes",
        "truncated_pcm",
        "original_sha256",
        "stream_ordinal",
        "delete_sidecar",
        "corrupt_sidecar",
        "unknown_key",
    ],
)
def test_reuse_requires_every_recorded_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper: str
) -> None:
    """하나라도 어긋나면 재사용하지 않는다 — 어긋난 채 믿는 것이 사고의 씨앗이다."""
    pcm_bytes = _pattern_pcm(1000, channels=2)
    recorder = _fake_extract(monkeypatch, pcm_bytes, channels=2)
    destination = tmp_path / "ref.pcm"
    build_reference("asset-1", "demo", tmp_path / "in.mov", "b" * 64, AudioPolicy(), destination)

    if tamper == "pcm_bytes":
        destination.write_bytes(b"\x00" * len(pcm_bytes))  # 크기는 같고 해시는 다르다
        rebuild = ("asset-1", "demo", "b" * 64, AudioPolicy())
    elif tamper == "truncated_pcm":
        destination.write_bytes(pcm_bytes[:-4])  # 프레임 수가 기록과 어긋난다
        rebuild = ("asset-1", "demo", "b" * 64, AudioPolicy())
    elif tamper == "original_sha256":
        rebuild = ("asset-1", "demo", "c" * 64, AudioPolicy())
    else:
        rebuild = ("asset-1", "demo", "b" * 64, AudioPolicy(stream_index=1))
        if tamper == "delete_sidecar":
            _sidecar(destination).unlink()
        elif tamper == "corrupt_sidecar":
            _sidecar(destination).write_text("{ 반쯤 죽은 JSON", encoding="utf-8")
        elif tamper == "unknown_key":
            record = json.loads(_sidecar(destination).read_text(encoding="utf-8"))
            record["bogus"] = 1
            _sidecar(destination).write_text(json.dumps(record), encoding="utf-8")

    build_reference(
        rebuild[0], rebuild[1], tmp_path / "in.mov", rebuild[2], rebuild[3], destination
    )
    assert len(recorder.calls) == 2, f"{tamper}: 다시 뽑아야 한다"
    assert destination.read_bytes() == pcm_bytes, "다시 뽑으면 원래 바이트가 돌아온다"


# ─────────────────────────────────────────────────────────────
#  원자성 — 실패해도 자리를 더럽히지 않는다
# ─────────────────────────────────────────────────────────────


def test_failed_extraction_leaves_no_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """추출이 반쯤 죽으면 목적지도 임시파일도 남지 않는다."""
    _fake_extract(monkeypatch, b"\x7f" * 100, fail_after_write=True)
    with pytest.raises(RuntimeError, match="반쯤"):
        build_reference(
            "asset-1",
            "demo",
            tmp_path / "in.mov",
            "b" * 64,
            AudioPolicy(),
            tmp_path / "ref.pcm",
        )
    assert list(tmp_path.iterdir()) == []


def test_failed_rebuild_keeps_the_previous_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """재뽑다가 죽어도 있던 참조는 그대로다 — 반쯤 짠 것으로 못 덮어쓴다."""
    pcm_bytes = _pattern_pcm(1000, channels=2)
    _fake_extract(monkeypatch, pcm_bytes, channels=2)
    destination = tmp_path / "ref.pcm"
    build_reference("asset-1", "demo", tmp_path / "in.mov", "b" * 64, AudioPolicy(), destination)

    _sidecar(destination).unlink()  # 재뽑을 강제한다
    _fake_extract(monkeypatch, b"\x7f" * 100, fail_after_write=True)
    with pytest.raises(RuntimeError, match="반쯤"):
        build_reference(
            "asset-1", "demo", tmp_path / "in.mov", "b" * 64, AudioPolicy(), destination
        )
    assert destination.read_bytes() == pcm_bytes
    assert [path.name for path in tmp_path.iterdir()] == ["ref.pcm"]


def test_extraction_of_another_width_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P2-01이 s16le 밖의 폭을 돌려주면 온 것이다 — 참조로 삼지 않는다."""
    _fake_extract(monkeypatch, _pattern_pcm(100, channels=2), channels=2, sample_width=4)
    with pytest.raises(RuntimeError, match="폭"):
        build_reference(
            "asset-1",
            "demo",
            tmp_path / "in.mov",
            "b" * 64,
            AudioPolicy(),
            tmp_path / "ref.pcm",
        )
    assert list(tmp_path.iterdir()) == []


# ─────────────────────────────────────────────────────────────
#  범위 환산 — ceil/floor로 안쪽으로만
# ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (Fraction(1, 3), Fraction(2, 3), (5334, 10666)),  # 16000/3=5333.33, 32000/3=10666.67
        (Fraction(15999, 32000), Fraction(32001, 32000), (8000, 16000)),  # 7999.5·16000.5
        (Fraction(1, 2), Fraction(1), (8000, 16000)),  # 격자 위 — 그대로
        (Fraction(0), Fraction(1, 4), (0, 4000)),
    ],
)
def test_range_rounds_inward_with_ceil_and_floor(
    tmp_path: Path, start: Fraction, end: Fraction, expected: tuple[int, int]
) -> None:
    """시작은 올라가고 끝은 내려간다 — 남은 오디오만 남는다."""
    reference = _hand_reference(tmp_path / "ref.pcm", _pattern_pcm(16000, channels=1))
    assert retained_sample_range(reference, start, end) == SampleRange(*expected)


def test_range_clamps_to_the_stored_frames(tmp_path: Path) -> None:
    """시작은 0보다, 끝은 실제 프레임 수보다 나가지 않는다."""
    reference = _hand_reference(tmp_path / "ref.pcm", _pattern_pcm(16000, channels=1))
    span = retained_sample_range(reference, Fraction(-5), Fraction(1000))
    assert span == SampleRange(0, 16000)


def test_range_is_independent_of_the_origin(tmp_path: Path) -> None:
    """범위는 PCM 국소 시각이다 — 원점이 어디든 환산은 같다."""
    for origin in (Fraction(0), Fraction(242, 125), Fraction(-1, 5)):
        reference = _hand_reference(
            tmp_path / "ref.pcm", _pattern_pcm(16000, channels=1), origin=origin
        )
        span = retained_sample_range(reference, Fraction(1, 3), Fraction(2, 3))
        assert span == SampleRange(5334, 10666)


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (Fraction(1, 2), Fraction(1, 2)),  # 같은 시각
        (Fraction(2, 3), Fraction(1, 3)),  # 뒤집힘
        (Fraction(15999, 32000), Fraction(1, 2)),  # 한 프레임 안으로 함께 뭉그러진다
        (Fraction(100), Fraction(100)),  # 저장보다 멀리 — 비었다
    ],
)
def test_range_rejects_empty_or_reversed_after_conversion(
    tmp_path: Path, start: Fraction, end: Fraction
) -> None:
    """환산 뒤 end <= start면 남는 프레임이 없다 — tolerance를 붙이지 않는다."""
    reference = _hand_reference(tmp_path / "ref.pcm", _pattern_pcm(16000, channels=1))
    with pytest.raises(ValueError, match="비었"):
        retained_sample_range(reference, start, end)


@pytest.mark.parametrize("bad", [0.5, -0.5, True, "1/2", None])
def test_range_rejects_inexact_seconds(tmp_path: Path, bad: object) -> None:
    """float·bool·문자열 초는 금지 — 이 계층의 시각은 정확한 유리수만 안다."""
    reference = _hand_reference(tmp_path / "ref.pcm", _pattern_pcm(160, channels=1))
    with pytest.raises(ValueError, match="Fraction|정수"):
        retained_sample_range(reference, bad, Fraction(0))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="Fraction|정수"):
        retained_sample_range(reference, Fraction(0), bad)  # type: ignore[arg-type]


# ─────────────────────────────────────────────────────────────
#  WAV 슬라이스 — 바이트 그대로
# ─────────────────────────────────────────────────────────────


def _decoded(path: Path) -> tuple[tuple[int, int, int, int], list[int]]:
    with wave.open(str(path), "rb") as handle:
        params = (
            handle.getnchannels(),
            handle.getsampwidth(),
            handle.getframerate(),
            handle.getnframes(),
        )
        raw = handle.readframes(handle.getnframes())
    samples = array.array("h")
    samples.frombytes(raw)
    return params, list(samples)


def test_mono_slice_is_exactly_the_retained_frames(tmp_path: Path) -> None:
    """슬라이스 데이터 == 저장 PCM의 `[start,end)` 바이트. 남의 프레임은 없다."""
    pcm_bytes = _pattern_pcm(1000, channels=1)
    reference = _hand_reference(tmp_path / "ref.pcm", pcm_bytes)
    destination = tmp_path / "slice.wav"

    returned = write_wav_slice(reference, SampleRange(100, 200), destination)

    assert returned == destination
    params, samples = _decoded(destination)
    assert params == (1, 2, 16000, 100)
    assert samples == [_frame_value(frame, 0) for frame in range(100, 200)]
    assert destination.read_bytes()[44:] == pcm_bytes[200:400]
    assert destination.stat().st_size == 44 + 100 * 2 * 1


def test_stereo_slice_keeps_interleaved_phase_opposed_channels(tmp_path: Path) -> None:
    """채널은 따로 저장된 그대로 인터리브된다 — 반대 위상도 상쇄 없이 그대로."""
    samples = array.array("h")
    for frame in range(20):
        value = 500 + frame * 13
        samples.extend((value, -value))
    pcm_bytes = samples.tobytes()
    reference = _hand_reference(tmp_path / "ref.pcm", pcm_bytes, channels=2)

    write_wav_slice(reference, SampleRange(5, 10), tmp_path / "slice.wav")

    params, decoded = _decoded(tmp_path / "slice.wav")
    assert params == (2, 2, 16000, 5)
    assert decoded == [
        value for frame in range(5, 10) for value in (500 + frame * 13, -(500 + frame * 13))
    ]
    assert (tmp_path / "slice.wav").read_bytes()[44:] == pcm_bytes[20:40]


@pytest.mark.parametrize(
    ("start", "end"),
    [(0, 1001), (-1, 5), (5, 5), (9, 3), (True, 5), (1.5, 5), (0, 100.0)],
)
def test_slice_rejects_ranges_the_reference_cannot_satisfy(
    tmp_path: Path, start: object, end: object
) -> None:
    """참조가 줄 수 없는 범위는 바이트를 내주기 전에 죽는다."""
    reference = _hand_reference(tmp_path / "ref.pcm", _pattern_pcm(1000, channels=1))
    with pytest.raises(ValueError, match="범위|프레임"):
        write_wav_slice(
            reference,
            SampleRange(start, end),
            tmp_path / "slice.wav",  # type: ignore[arg-type]
        )


def test_slice_fails_loudly_when_the_stored_pcm_drifted(tmp_path: Path) -> None:
    """저장 PCM이 참조와 어긋나면(크기가 다르면) 슬라이스를 만들지 않는다."""
    reference = _hand_reference(tmp_path / "ref.pcm", _pattern_pcm(1000, channels=1))
    (tmp_path / "ref.pcm").write_bytes(_pattern_pcm(999, channels=1))
    with pytest.raises(RuntimeError, match="어긋"):
        write_wav_slice(reference, SampleRange(0, 100), tmp_path / "slice.wav")
    assert not (tmp_path / "slice.wav").exists()


def test_slice_write_is_atomic(tmp_path: Path) -> None:
    """쓰다 실패해도 있던 파일은 그대로고 임시파일은 남지 않는다."""
    reference = _hand_reference(tmp_path / "ref.pcm", _pattern_pcm(100, channels=1))
    destination = tmp_path / "slice.wav"
    previous = "이전 슬라이스".encode()
    destination.write_bytes(previous)
    tmp_path.chmod(0o500)  # 임시파일을 만들 수 없게 한다
    try:
        with pytest.raises(OSError):
            write_wav_slice(reference, SampleRange(0, 10), destination)
    finally:
        tmp_path.chmod(0o700)
    assert destination.read_bytes() == previous
    assert [path.name for path in tmp_path.iterdir()] == ["ref.pcm", "slice.wav"]


# ─────────────────────────────────────────────────────────────
#  자를 자리 — 조용한 창만 고르고 말/침묵을 판정하지 않는다
# ─────────────────────────────────────────────────────────────


def _mono_pcm(values: list[int]) -> bytes:
    return array.array("h", values).tobytes()


def test_quiet_sample_picks_the_quietest_window_near_the_target(tmp_path: Path) -> None:
    """진폭이 낮은 창이 목표까지의 거리보다 우선한다 — 조용한 쪽으로 옮긴다."""
    values = [1000] * 1000
    values[600:650] = [0] * 50
    reference = _hand_reference(tmp_path / "ref.pcm", _mono_pcm(values))
    # 무음 창의 목표(700)에 가장 가까운 후보는 640이다(거리 60). 700은 거리 0이지만 진폭이 크다.
    assert quiet_sample(reference, target=700, search_samples=100, window_samples=10) == 640


def test_quiet_ties_break_to_the_nearest_then_the_earlier(tmp_path: Path) -> None:
    """같은 진폭이면 목표에 가까운 쪽, 거리까지 같으면 앞쪽이다."""
    values = [500] * 2000
    values[170:230] = [0] * 60
    values[720:780] = [0] * 60
    reference = _hand_reference(tmp_path / "ref.pcm", _mono_pcm(values))
    # 목표 470에서 무음 창 끝의 거리: 앞 220과 뒤 720이 같다(250) — 앞쪽이 이긴다.
    assert quiet_sample(reference, target=470, search_samples=2000, window_samples=10) == 220
    # 전부 같은 진폭이면 목표 자체가 답이다.
    flat = _hand_reference(tmp_path / "flat.pcm", _mono_pcm([1000] * 100))
    assert quiet_sample(flat, target=50, search_samples=10, window_samples=10) == 50


def test_phase_opposed_channels_are_not_quiet(tmp_path: Path) -> None:
    """채널을 따로 잰다 — 좌우가 반대 위상이면 합치면 조용해 보여도 조용하지 않다."""
    samples = array.array("h")
    for frame in range(1200):
        samples.extend((2000, -2000) if frame < 600 else (0, 0))
    reference = _hand_reference(tmp_path / "ref.pcm", samples.tobytes(), channels=2)
    # 합산 구현은 앞쪽(0)을 조용하다고 속이고 그곳을 고른다. 따로 재면 600이다.
    assert quiet_sample(reference, target=300, search_samples=600, window_samples=50) == 600


def test_quiet_search_never_leaves_the_bounded_window(tmp_path: Path) -> None:
    """창 밖의 아주 조용한 구간은 보지도 않는다 — 탐색은 목표 주위로 묶여 있다."""
    values = [1000] * 10000
    values[5000:5100] = [0] * 100
    reference = _hand_reference(tmp_path / "ref.pcm", _mono_pcm(values))
    cut = quiet_sample(reference, target=1000, search_samples=100, window_samples=10)
    assert 900 <= cut <= 1100  # 경계 안 — 멀리 있는 무음(5000)은 답이 아니다
    assert cut == 1000  # 경계 안이 온통 같은 진폭이면 목표 그대로


@pytest.mark.parametrize(
    ("target", "search", "window", "match"),
    [
        (500, 100, 0, "창"),
        (500, 100, -5, "창"),
        (500, -1, 10, "탐색"),
        (-1, 100, 10, "target|목표"),
        (True, 100, 10, "target|목표"),
        (1001, 100, 10, "target|목표"),
        (500, 100, 2000, "길"),
        (1000, 10, 100, "비었"),  # 목표가 끝이라 창을 놓을 자리가 없다
    ],
)
def test_quiet_rejects_impossible_measurements(
    tmp_path: Path, target: object, search: object, window: object, match: str
) -> None:
    reference = _hand_reference(tmp_path / "ref.pcm", _mono_pcm([100] * 1000))
    with pytest.raises(ValueError, match=match):
        quiet_sample(
            reference,
            target=target,  # type: ignore[arg-type]
            search_samples=search,  # type: ignore[arg-type]
            window_samples=window,  # type: ignore[arg-type]
        )


def test_quiet_fails_loudly_when_the_stored_pcm_drifted(tmp_path: Path) -> None:
    """저장 PCM이 참조와 어긋나면 자를 자리도 고르지 않는다."""
    reference = _hand_reference(tmp_path / "ref.pcm", _mono_pcm([100] * 1000))
    (tmp_path / "ref.pcm").write_bytes(_mono_pcm([100] * 999))
    with pytest.raises(RuntimeError, match="어긋"):
        quiet_sample(reference, target=500, search_samples=10, window_samples=10)


# ─────────────────────────────────────────────────────────────
#  원점 매핑 — file_time = sample/rate + 원점
# ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("origin", "index", "expected"),
    [
        (Fraction(-1, 5), 3200, Fraction(0)),  # -0.2초 + 0.2초 = 0
        (Fraction(242, 125), 0, Fraction(242, 125)),
        (Fraction(1, 3), 1, Fraction(16003, 48000)),  # 1/3 + 1/16000
        (Fraction(0), 16000, Fraction(1)),
    ],
)
def test_file_time_is_exact_fraction_arithmetic(
    tmp_path: Path, origin: Fraction, index: int, expected: Fraction
) -> None:
    reference = _hand_reference(
        tmp_path / "ref.pcm", _pattern_pcm(16001, channels=1), origin=origin
    )
    assert sample_file_time(reference, index) == expected


@pytest.mark.parametrize("bad", [1.0, -1, True, 16002, "0"])
def test_file_time_rejects_indexes_that_are_not_frames(tmp_path: Path, bad: object) -> None:
    """프레임 번호는 0 이상 정수다 — 저장 범위 밖의 시각은 지어내지 않는다."""
    reference = _hand_reference(tmp_path / "ref.pcm", _pattern_pcm(16000, channels=1))
    with pytest.raises(ValueError, match="프레임|정수"):
        sample_file_time(reference, bad)  # type: ignore[arg-type]


# ─────────────────────────────────────────────────────────────
#  실측 — 합성 미디어로 잰 원점·슬라이스·자를 자리
# ─────────────────────────────────────────────────────────────

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe가 없는 환경",
)


def _impulse_pcm_bytes() -> bytes:
    """1초 스테레오 s16. 프레임 4000(=0.25초)에만 좌 양수·우 음수 임펄스가 있다."""
    samples = array.array("h", [0, 0] * 16000)
    samples[4000 * 2] = 20000
    samples[4000 * 2 + 1] = -12000
    return samples.tobytes()


def _mux_shifted_impulse(tmp_path: Path, name: str, *, codec: str, offset: Fraction) -> Path:
    """임펄스 오디오 + 16x16 합성 영상. 오디오 PTS를 `offset`만큼 옮겨 담는다."""
    raw = tmp_path / "impulse.s16"
    raw.write_bytes(_impulse_pcm_bytes())
    media = tmp_path / name
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "s16le", "-ar", "16000", "-ac", "2", "-i", str(raw),
            "-f", "lavfi", "-i", "testsrc=size=16x16:rate=25:duration=1",
            "-filter_complex",
            f"[0:a]asetpts=PTS+({offset.numerator}/{offset.denominator})/TB[a]",
            "-map", "1:v", "-map", "[a]",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", codec, "-avoid_negative_ts", "disabled",
            str(media),
        ],
        capture_output=True,
        check=True,
    )  # fmt: skip
    return media


@needs_ffmpeg
def test_reference_slices_and_finds_quiet_in_the_stored_pcm_real(tmp_path: Path) -> None:
    """+2초로 옮겨 담은 PCM 영상: 원점은 2, 임펄스는 파일 시각 9/4에 말한다."""
    media = _mux_shifted_impulse(tmp_path, "plus.mov", codec="pcm_s16le", offset=Fraction(2))
    reference = build_reference(
        "asset-1", "plus", media, "b" * 64, AudioPolicy(), tmp_path / "ref.pcm"
    )

    assert reference.pcm_sample0_file_time == 2
    assert reference.channels == 2
    assert reference.sample_rate == 16000
    assert reference.sample_count == 16000  # 1초 × 16000 — 딱
    assert sample_file_time(reference, 4000) == Fraction(9, 4)

    # 슬라이스 [3999, 4001): 저장 PCM의 그 두 프레임이 그대로 들어 있다
    write_wav_slice(reference, SampleRange(3999, 4001), tmp_path / "around.wav")
    _, decoded = _decoded(tmp_path / "around.wav")
    assert decoded == [0, 0, 20000, -12000]

    # [0, 4000)에는 임펄스가 없다 — 자른 범위 밖의 소리는 안 나온다
    write_wav_slice(reference, SampleRange(0, 4000), tmp_path / "head.wav")
    assert _decoded(tmp_path / "head.wav")[1] == [0] * 8000

    # 자를 자리: 목표 4000 근처에서 임펄스를 피해 한 프레임 뒤로 간다
    assert quiet_sample(reference, target=4000, search_samples=100, window_samples=10) == 4001


@needs_ffmpeg
def test_negative_epoch_from_aac_mkv_survives_the_reference_real(tmp_path: Path) -> None:
    """결정적인 AAC/MKV 행: 컨테이너 start_time은 −33/125지만 원점은 −1/5다."""
    media = _mux_shifted_impulse(tmp_path, "minus.mkv", codec="aac", offset=Fraction(-1, 5))
    reference = build_reference(
        "asset-1", "minus", media, "b" * 64, AudioPolicy(), tmp_path / "ref.pcm"
    )

    assert reference.pcm_sample0_file_time == Fraction(-1, 5)
    assert reference.pcm_sample0_file_time != Fraction(-33, 125)
    assert reference.sample_count == 16384  # 프라이밍이 잘려난 뒤의 실측(P2-01 표)

    # 슬라이스는 무손실 복사다 — 저장 PCM의 해당 바이트와 한 바이트도 다르지 않다
    stored = reference.path.read_bytes()
    write_wav_slice(reference, SampleRange(8000, 8010), tmp_path / "slice.wav")
    assert (tmp_path / "slice.wav").read_bytes()[44:] == stored[8000 * 4 : 8010 * 4]


@needs_ffmpeg
def test_real_rebuild_reuses_the_verified_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """진짜 추출로 만든 참조도 두 번째엔 ffmpeg를 부르지 않는다."""
    media = _mux_shifted_impulse(tmp_path, "plus.mov", codec="pcm_s16le", offset=Fraction(2))
    real_extract = pcm.extract_pcm_s16le
    calls: list[Path] = []

    def counting(
        source: Path, destination: Path, *, stream_index: int, sample_rate: int = 16000
    ) -> preprocess.PcmExtraction:
        calls.append(destination)
        return real_extract(source, destination, stream_index=stream_index, sample_rate=sample_rate)

    monkeypatch.setattr(pcm, "extract_pcm_s16le", counting)
    destination = tmp_path / "ref.pcm"
    first = build_reference("asset-1", "plus", media, "b" * 64, AudioPolicy(), destination)
    second = build_reference("asset-1", "plus", media, "b" * 64, AudioPolicy(), destination)

    assert len(calls) == 1
    assert second == first
