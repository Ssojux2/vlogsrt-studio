"""원본 PCM 참조 — 저장·키·세대 출판·슬라이스·자를 자리 (P2-02).

참조는 P2-01이 잰 원점을 그대로 믿고 저장 PCM을 내용으로 주소를 매긴다. 슬라이스는
`[start, end)` 프레임을 **바이트 그대로** 옮긴다 — 제거된 오디오의 덧붙임도 문맥도
없다는 것을 패턴 바이트로 증명한다. 완성물은 논리 주소의 포인터가 가리키는 불변
세대다: 세대 출판이 실패하거나 프로세스가 포인터 앞에서 죽어도 손에 쥔 옛 참조가
그대로 읽히고 슬라이스가 재추출 없이 됨을 실패 주입과 실제 자식 프로세스 종료로
증명한다. ffmpeg 경계는 전부 P2-01 흉내로 대신하고, 실측 증거는 합성 미디어를
만들어 잰다(미디어는 tmp_path 안에서만 만든다).
"""

from __future__ import annotations

import array
import hashlib
import json
import os
import shutil
import subprocess
import sys
import wave
from dataclasses import dataclass, fields
from dataclasses import replace as dataclasses_replace
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
    write_wav_slice,
)

# ─────────────────────────────────────────────────────────────
#  재료 — 패턴 PCM·실제 원본·참조·추출 흉내·포인터 도구
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


def _original(directory: Path, name: str, content: bytes) -> tuple[Path, str]:
    """진짜 원본 파일과 그 전체 내용 해시 — 정체성 검증은 이것으로만 지난다."""
    directory.mkdir(parents=True, exist_ok=True)
    original = directory / name
    original.write_bytes(content)
    return original, hashlib.sha256(content).hexdigest()


def _hand_reference(
    path: Path,
    pcm_bytes: bytes,
    *,
    channels: int = 1,
    origin: Fraction = Fraction(0),
    sample_count: int | None = None,
) -> PcmReference:
    """파일을 직접 쓰고 참조를 손으로 만든다 — 순수 함수 시험용."""
    path.parent.mkdir(parents=True, exist_ok=True)
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


def _pointer(destination: Path) -> dict[str, object]:
    """논리 주소의 포인터 — 세대를 가리키는 작은 JSON이다."""
    record: dict[str, object] = json.loads(destination.read_text(encoding="utf-8"))
    return record


def _rewrite_pointer(destination: Path, **changes: object) -> None:
    record: dict[str, object] = json.loads(destination.read_text(encoding="utf-8"))
    record.update(changes)
    destination.write_text(json.dumps(record), encoding="utf-8")


def _generation_paths(destination: Path) -> tuple[Path, Path]:
    """포인터가 가리키는 (세대 PCM, 세대 기록)."""
    pointer = _pointer(destination)
    base = destination.parent
    return (
        base / str(pointer["generation_pcm"]),
        base / str(pointer["generation_metadata"]),
    )


def _rewrite_generation_record(destination: Path, field: str, value: object) -> None:
    _, metadata = _generation_paths(destination)
    record: dict[str, object] = json.loads(metadata.read_text(encoding="utf-8"))
    record[field] = value
    metadata.write_text(json.dumps(record), encoding="utf-8")


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


def _failing_replace_when(predicate) -> object:
    """`os.replace` 흉내 — 조건에 맞는 출판 **첫 번째만** 죽인다(한 번 장전)."""
    real_replace = os.replace
    armed = True

    def replace(src: object, dst: object) -> None:
        nonlocal armed
        if armed and predicate(Path(str(dst))):
            armed = False
            raise OSError(f"출판 실패 주입: {dst}")
        real_replace(src, dst)

    return replace


def _failing_replace(target: Path) -> object:
    return _failing_replace_when(lambda path: path == target)


# ─────────────────────────────────────────────────────────────
#  입력 검증 — 정책·정체성·경로
# ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("bad", [-1, True, False, 1.0, "0"])
def test_policy_rejects_an_ordinal_that_is_not_a_count(bad: object) -> None:
    """오디오 순번은 0 이상 정수다 — 잘못된 정책은 만드는 순간에 죽는다."""
    with pytest.raises(ValueError, match="순번"):
        AudioPolicy(stream_index=bad)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", [44100, 0, -16000, True, 16000.0])
def test_policy_accepts_only_the_contract_pcm_rate(bad: object) -> None:
    """저장 PCM은 16 kHz s16le다(P2-01 계약) — 다른 주파수는 저장 형식이 아니다."""
    with pytest.raises(ValueError, match="16000"):
        AudioPolicy(sample_rate=bad)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", [1, 4, 0, True, 2.0])
def test_policy_accepts_only_the_contract_pcm_width(bad: object) -> None:
    """s16le의 폭은 2바이트다 — 폭이 다르면 슬라이스 바이트 수 계산이 어긋난다."""
    with pytest.raises(ValueError, match="s16le|폭"):
        AudioPolicy(sample_width=bad)  # type: ignore[arg-type]


@pytest.mark.parametrize("field_name", ["source_id", "original_stem", "original_sha256"])
def test_identity_strings_must_be_real(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field_name: str
) -> None:
    """정체성 문자열이 비면 나중 조인이 경로·파일이름으로 지어낸다 — 못 넘긴다."""
    source = tmp_path / "src"
    out = tmp_path / "out"
    original, original_sha = _original(source, "in.mov", b"synthetic-original")
    recorder = _fake_extract(monkeypatch, b"")
    names = {"source_id": "asset-1", "original_stem": "demo", "original_sha256": original_sha}
    names[field_name] = ""
    with pytest.raises(ValueError, match=field_name):
        build_reference(
            names["source_id"],
            names["original_stem"],
            original,
            names["original_sha256"],
            AudioPolicy(),
            out / "ref.pcm",
        )
    assert recorder.calls == []


def test_original_hash_mismatch_fails_before_any_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """원본을 통째로 해시해 정체성과 같은지 먼저 본다 — 재사용·추출 이전에."""
    source = tmp_path / "src"
    out = tmp_path / "out"
    original, _sha = _original(source, "in.mov", b"synthetic-original")
    recorder = _fake_extract(monkeypatch, _pattern_pcm(10, channels=2))
    with pytest.raises(ValueError, match="원본 해시"):
        build_reference("asset-1", "demo", original, "c" * 64, AudioPolicy(), out / "ref.pcm")
    assert recorder.calls == []
    assert not (out / "ref.pcm").exists()


def test_a_missing_original_fails_before_any_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """없는 원본으로는 정체성 자체를 증명할 수 없다."""
    recorder = _fake_extract(monkeypatch, _pattern_pcm(10, channels=2))
    with pytest.raises(ValueError, match="원본 파일"):
        build_reference(
            "asset-1",
            "demo",
            tmp_path / "src" / "없는-원본.mov",
            "c" * 64,
            AudioPolicy(),
            tmp_path / "out" / "ref.pcm",
        )
    assert recorder.calls == []


@pytest.mark.parametrize("case", ["same_path", "symlink_alias"])
def test_path_collisions_are_rejected_before_any_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """원본 미디어는 캐시 주소와 같은 곳에 살면 안 된다 — 시작 전에 거부한다."""
    source = tmp_path / "src"
    out = tmp_path / "out"
    out.mkdir(parents=True)
    content = "원본 미디어".encode()
    original, original_sha = _original(source, "in.mov", content)
    destination = out / "ref.pcm"
    if case == "same_path":
        destination = original
    else:
        os.symlink(original, destination)
    recorder = _fake_extract(monkeypatch, _pattern_pcm(10, channels=2))

    with pytest.raises(ValueError, match="충돌|덮어"):
        build_reference("asset-1", "demo", original, original_sha, AudioPolicy(), destination)

    assert recorder.calls == []
    assert original.read_bytes() == content, "원본은 못 건드린다"


# ─────────────────────────────────────────────────────────────
#  참조 만들기 — 저장·원점·키
# ─────────────────────────────────────────────────────────────


def test_build_persists_an_immutable_generation_under_the_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """만들면 세대 PCM·세대 기록이 새 이름에 남고, 포인터만 논리 주소에 산다."""
    source = tmp_path / "src"
    out = tmp_path / "out"
    original, original_sha = _original(source, "in.mov", b"synthetic-original")
    pcm_bytes = _pattern_pcm(8000, channels=2)
    recorder = _fake_extract(monkeypatch, pcm_bytes, channels=2)
    destination = out / "ref.pcm"

    reference = build_reference(
        "asset-1", "demo", original, original_sha, AudioPolicy(), destination
    )

    assert recorder.calls, "추출을 한 번은 불러야 한다"
    pointer = _pointer(destination)
    assert pointer["format"] == "vlogsrt-pcm-pointer-v1"
    assert set(pointer) == {"format", "generation_pcm", "generation_metadata"}
    assert reference.path == out / str(pointer["generation_pcm"])
    assert reference.path != destination, "참조의 길은 세대 PCM이다 — 논리 주소가 아니다"
    assert reference.path.read_bytes() == pcm_bytes
    assert (out / str(pointer["generation_metadata"])).is_file()
    assert reference.source_id == "asset-1"
    assert reference.original_stem == "demo"
    assert reference.original_sha256 == original_sha
    assert reference.pcm_sha256 == hashlib.sha256(pcm_bytes).hexdigest()
    assert reference.sample_count == 8000
    assert reference.channels == 2
    assert reference.sample_rate == 16000
    assert reference.pcm_sample0_file_time == Fraction(0)
    assert len(reference.analysis_key) == 64
    assert len(list(out.iterdir())) == 3  # 포인터 + 세대 둘
    assert not any(path.name.endswith(".tmp") for path in out.iterdir())


def test_the_audio_ordinal_is_handed_to_p2_01_as_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """순번은 P2-01에 그대로 간다 — 전역 번호는 P2-01이 ffprobe로 얻는 것이다."""
    source = tmp_path / "src"
    original, original_sha = _original(source, "in.mov", b"synthetic-original")
    recorder = _fake_extract(monkeypatch, _pattern_pcm(100, channels=2), channels=2)
    build_reference(
        "asset-1",
        "demo",
        original,
        original_sha,
        AudioPolicy(stream_index=2),
        tmp_path / "out" / "ref.pcm",
    )
    assert recorder.asked == {"stream_index": 2, "sample_rate": 16000}


def test_reference_keeps_the_measured_origin_not_the_stream_start_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """원점은 변환 후 첫 프레임의 실측 pts다 — 컨테이너 start_time과 다르다.

    AAC/MKV 실측: start_time은 −33/125인데 디코드 원점은 −1/5다. 참조가 이 둘을
    같은 것으로 취급하면 64ms가 자막 전체에 실린다.
    """
    source = tmp_path / "src"
    original, original_sha = _original(source, "in.mkv", b"synthetic-original")
    recorder = _fake_extract(
        monkeypatch,
        _pattern_pcm(4000, channels=2),
        channels=2,
        origin=Fraction(-1, 5),
        start_time=Fraction(-33, 125),
    )
    reference = build_reference(
        "asset-1", "demo", original, original_sha, AudioPolicy(), tmp_path / "out" / "ref.pcm"
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
    source = tmp_path / "src"
    original, original_sha = _original(source, "in.mov", b"synthetic-original")
    _fake_extract(
        monkeypatch, _pattern_pcm(1000, channels=2), channels=2, origin=Fraction(242, 125)
    )
    first = build_reference(
        "asset-1", "demo", original, original_sha, AudioPolicy(), tmp_path / "out" / "a.pcm"
    )
    second = build_reference(
        "asset-1", "demo", original, original_sha, AudioPolicy(), tmp_path / "out" / "b.pcm"
    )
    assert first.analysis_key == second.analysis_key


def test_analysis_key_ignores_names_paths_and_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """키는 내용이다 — source_id·이름·경로가 키를 바꾸면 안 된다."""
    source = tmp_path / "src"
    content = "같은 내용의 원본".encode()
    one, one_sha = _original(source, "one.mov", content)
    two, two_sha = _original(source / "nested", "two.mov", content)
    assert one_sha == two_sha  # 같은 내용 — 다른 경로·이름
    _fake_extract(monkeypatch, _pattern_pcm(1000, channels=1), channels=1)
    first = build_reference(
        "asset-1", "ep01", one, one_sha, AudioPolicy(), tmp_path / "out" / "one.pcm"
    )
    second = build_reference(
        "asset-2", "ep77", two, two_sha, AudioPolicy(), tmp_path / "out" / "two.pcm"
    )
    assert first.analysis_key == second.analysis_key


@pytest.mark.parametrize(
    ("what", "delta"),
    [
        ("원본 해시", {"other_original": True}),
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
    source = tmp_path / "src"
    original, original_sha = _original(source, "in.mov", b"synthetic-original")
    base = {
        "original": original,
        "original_sha256": original_sha,
        "origin": Fraction(242, 125),
        "pcm_bytes": _pattern_pcm(1000, channels=1),
        "channels": 1,
        "policy": AudioPolicy(),
    }
    if delta.get("other_original"):
        other, other_sha = _original(source, "other.mov", "다른 내용의 원본".encode())
        delta = {"original": other, "original_sha256": other_sha}

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
            inputs["original"],  # type: ignore[arg-type]
            inputs["original_sha256"],  # type: ignore[arg-type]
            inputs["policy"],  # type: ignore[arg-type]
            destination,
        )

    first = build(base, tmp_path / "out" / "a.pcm")
    varied = dict(base)
    varied.update(delta)
    second = build(varied, tmp_path / "out" / "b.pcm")
    assert first.analysis_key != second.analysis_key, what


def test_analysis_key_material_names_every_input() -> None:
    """키 재료는 전부 이름 붙여 들어간다 — 정책 주파수·폭도 재료에 남는다.

    정책 검증이 생성 시점으로 옮겨져 잘못된 정책은 이제 아예 못 만든다. 그래서
    주파수·폭의 키 기여는 해시 이전 재료 문자열로 직접 증명한다.
    """
    material = pcm._key_material("b" * 64, AudioPolicy(), Fraction(-1, 5), "d" * 64, 2, 16000)
    for named in (
        "original_sha256=" + "b" * 64,
        "audio_ordinal=0",
        "pcm_sample_rate=16000",
        "pcm_sample_width=2",
        "pcm_sample0_file_time=-1/5",
        "pcm_sha256=" + "d" * 64,
        "channels=2",
        "sample_rate=16000",
    ):
        assert named in material, named


# ─────────────────────────────────────────────────────────────
#  재사용 — 검증된 완성 세대만
# ─────────────────────────────────────────────────────────────


def test_rebuild_reuses_a_verified_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """포인터·세대 기록·해시·정책이 전부 맞으면 다시 뽑지 않는다."""
    source = tmp_path / "src"
    out = tmp_path / "out"
    original, original_sha = _original(source, "in.mov", b"synthetic-original")
    recorder = _fake_extract(monkeypatch, _pattern_pcm(1000, channels=2), channels=2)
    destination = out / "ref.pcm"

    first = build_reference("asset-1", "demo", original, original_sha, AudioPolicy(), destination)
    second = build_reference("asset-1", "demo", original, original_sha, AudioPolicy(), destination)

    assert len(recorder.calls) == 1, "검증된 완성 세대는 다시 뽑지 않는다"
    assert second == first
    assert len(list(out.iterdir())) == 3  # 포인터 + 세대 둘 — 늘어난 게 없다


@pytest.mark.parametrize(
    "tamper",
    [
        "pcm_bytes",
        "truncated_pcm",
        "original_sha256",
        "stream_ordinal",
        "delete_pointer",
        "corrupt_pointer",
        "unknown_pointer_key",
        "pointer_missing_generation",
        "pointer_escape",
        "pointer_crossed",
        "meta_hash_field",
        "meta_count_field",
        "meta_ordinal_field",
        "meta_origin_denominator",
    ],
)
def test_reuse_requires_every_recorded_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper: str
) -> None:
    """하나라도 어긋나면 재사용하지 않는다 — 어긋난 채 믿는 것이 사고의 씨앗이다.

    포인터·세대 기록을 망가뜨린 경우 정책은 그대로다: 같은 입력으로 다시 불러도
    재추출이 일어나야 한다. 원점 분모 0(`1/0`)은 `ValueError`로 죽어야지
    `ZeroDivisionError`가 새어 나가면 안 된다.
    """
    source = tmp_path / "src"
    out = tmp_path / "out"
    original, original_sha = _original(source, "in.mov", b"synthetic-original")
    other, other_sha = _original(source, "other.mov", "다른 내용의 원본".encode())
    pcm_bytes = _pattern_pcm(1000, channels=2)
    recorder = _fake_extract(monkeypatch, pcm_bytes, channels=2)
    destination = out / "ref.pcm"
    build_reference("asset-1", "demo", original, original_sha, AudioPolicy(), destination)
    generation_pcm, generation_meta = _generation_paths(destination)

    if tamper == "pcm_bytes":
        generation_pcm.write_bytes(b"\x00" * len(pcm_bytes))  # 크기는 같고 해시는 다르다
        rebuild = (original, original_sha, AudioPolicy())
    elif tamper == "truncated_pcm":
        generation_pcm.write_bytes(pcm_bytes[:-4])  # 프레임 수가 기록과 어긋난다
        rebuild = (original, original_sha, AudioPolicy())
    elif tamper == "original_sha256":
        rebuild = (other, other_sha, AudioPolicy())
    elif tamper == "stream_ordinal":
        rebuild = (original, original_sha, AudioPolicy(stream_index=1))
    else:
        rebuild = (original, original_sha, AudioPolicy())
        if tamper == "delete_pointer":
            destination.unlink()
        elif tamper == "corrupt_pointer":
            destination.write_text("{ 반쯤 죽은 JSON", encoding="utf-8")
        elif tamper == "unknown_pointer_key":
            _rewrite_pointer(destination, bogus=1)
        elif tamper == "pointer_missing_generation":
            _rewrite_pointer(destination, generation_pcm=f"{destination.name}.ghost.pcm")
        elif tamper == "pointer_escape":
            _rewrite_pointer(destination, generation_pcm="../escape.pcm")
        elif tamper == "pointer_crossed":
            _rewrite_pointer(destination, generation_pcm=generation_meta.name)
        elif tamper == "meta_hash_field":
            _rewrite_generation_record(
                destination, "pcm_sha256", hashlib.sha256("다른 pcm".encode()).hexdigest()
            )
        elif tamper == "meta_count_field":
            _rewrite_generation_record(destination, "sample_count", 1001)
        elif tamper == "meta_ordinal_field":
            _rewrite_generation_record(destination, "stream_index", 1)
        elif tamper == "meta_origin_denominator":
            _rewrite_generation_record(destination, "pcm_sample0_file_time", "1/0")

    build_reference("asset-1", "demo", rebuild[0], rebuild[1], rebuild[2], destination)
    assert len(recorder.calls) == 2, f"{tamper}: 다시 뽑아야 한다"
    new_pcm, _new_meta = _generation_paths(destination)
    assert new_pcm.read_bytes() == pcm_bytes, "다시 뽑으면 원래 바이트가 돌아온다"


# ─────────────────────────────────────────────────────────────
#  세대 출판 — 실패 주입·실제 자식 종료로 증명하는 보존
# ─────────────────────────────────────────────────────────────


def test_failed_extraction_leaves_no_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """추출이 반쯤 죽으면 포인터는 없다 — 남는 것은 참조되지 않는 사유 자료뿐이다."""
    source = tmp_path / "src"
    out = tmp_path / "out"
    original, original_sha = _original(source, "in.mov", b"synthetic-original")
    _fake_extract(monkeypatch, b"\x7f" * 100, fail_after_write=True)
    with pytest.raises(RuntimeError, match="반쯤"):
        build_reference("asset-1", "demo", original, original_sha, AudioPolicy(), out / "ref.pcm")
    assert not (out / "ref.pcm").exists(), "포인터는 출판된 적이 없다"
    assert all(
        path.name.startswith("ref.pcm.") and not path.name.endswith(".tmp")
        for path in out.iterdir()
    ), "남은 것은 이 시도의 사유 세대 자료뿐이다"


def test_failed_rebuild_keeps_the_pointer_and_the_old_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """재뽑다가 죽으면 포인터와 세대는 그대로다 — 반쯤 짠 것으로 못 덮어쓴다."""
    source = tmp_path / "src"
    out = tmp_path / "out"
    original, original_sha = _original(source, "in.mov", b"synthetic-original")
    pcm_bytes = _pattern_pcm(1000, channels=2)
    _fake_extract(monkeypatch, pcm_bytes, channels=2)
    destination = out / "ref.pcm"
    build_reference("asset-1", "demo", original, original_sha, AudioPolicy(), destination)
    old_pointer = destination.read_bytes()
    generation_pcm, generation_meta = _generation_paths(destination)
    old_record = generation_meta.read_bytes()

    generation_pcm.write_bytes(b"\x00" * len(pcm_bytes))  # 재뽑을 강제한다
    _fake_extract(monkeypatch, b"\x7f" * 100, fail_after_write=True)
    with pytest.raises(RuntimeError, match="반쯤"):
        build_reference("asset-1", "demo", original, original_sha, AudioPolicy(), destination)
    # 재빌드 전에 보이는 포인터와 그것이 가리키는 짝을 직접 본다 — 재추출이 결함을 덮지 않게.
    assert destination.read_bytes() == old_pointer, "포인터는 그대로다"
    still_pcm, still_meta = _generation_paths(destination)
    assert still_pcm == generation_pcm and still_meta == generation_meta
    assert still_meta.read_bytes() == old_record, "가리킨 세대 기록도 그대로다"

    # 다음 시도는 성공한다 — 새 세대가 수복하고 옛 세대 파일은 남는다.
    _fake_extract(monkeypatch, pcm_bytes, channels=2)
    reference = build_reference(
        "asset-1", "demo", original, original_sha, AudioPolicy(), destination
    )
    assert reference.path != generation_pcm, "수복은 새 세대로 한다 — 자리를 고치지 않는다"
    assert reference.path.read_bytes() == pcm_bytes
    assert generation_pcm.exists(), "망가진 옛 세대도 지우지 않는다"


def test_extraction_of_another_width_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P2-01이 s16le 밖의 폭을 돌려주면 온 것이다 — 참조로 삼지 않는다."""
    source = tmp_path / "src"
    out = tmp_path / "out"
    original, original_sha = _original(source, "in.mov", b"synthetic-original")
    _fake_extract(monkeypatch, _pattern_pcm(100, channels=2), channels=2, sample_width=4)
    with pytest.raises(RuntimeError, match="폭"):
        build_reference("asset-1", "demo", original, original_sha, AudioPolicy(), out / "ref.pcm")
    assert not (out / "ref.pcm").exists(), "포인터는 출판된 적이 없다"
    assert all(
        path.name.startswith("ref.pcm.") and not path.name.endswith(".tmp")
        for path in out.iterdir()
    ), "남은 것은 이 시도의 사유 세대 자료뿐이다"


def test_metadata_publication_failure_preserves_the_valid_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """세대 기록 출판이 죽어도 옛 참조는 그대로다 — 슬라이스도 재추출 0회로 된다."""
    source = tmp_path / "src"
    out = tmp_path / "out"
    one, one_sha = _original(source, "one.mov", "첫 원본".encode())
    two, two_sha = _original(source, "two.mov", "둘째 원본 — 다른 내용".encode())
    first_bytes = _pattern_pcm(1000, channels=2)
    second_bytes = _pattern_pcm(999, channels=2)
    destination = out / "ref.pcm"
    _fake_extract(monkeypatch, first_bytes, channels=2)
    old = build_reference("asset-1", "demo", one, one_sha, AudioPolicy(), destination)
    old_pointer = destination.read_bytes()

    recorder = _fake_extract(monkeypatch, second_bytes, channels=2)
    with pytest.MonkeyPatch.context() as injected:
        injected.setattr(
            pcm.os, "replace", _failing_replace_when(lambda path: path.suffix == ".json")
        )
        with pytest.raises(OSError, match="출판 실패"):
            build_reference("asset-1", "demo", two, two_sha, AudioPolicy(), destination)

    assert destination.read_bytes() == old_pointer, "포인터는 부분 짝을 노출한 적이 없다"
    assert old.path.read_bytes() == first_bytes, "옛 세대 PCM이 그대로다"
    write_wav_slice(old, SampleRange(0, 100), out / "old-slice.wav")
    assert (out / "old-slice.wav").read_bytes()[44:] == first_bytes[: 100 * 2 * 2]
    assert len(recorder.calls) == 1, "실패한 시도의 추출 한 번뿐이다"

    # 옛 입력의 재빌드는 재추출 없이 재사용한다.
    again = build_reference("asset-1", "demo", one, one_sha, AudioPolicy(), destination)
    assert len(recorder.calls) == 1
    assert again == old


def test_pointer_publication_failure_preserves_the_valid_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """포인터 출판이 죽어도 옛 참조는 그대로다 — 슬라이스도 재추출 0회로 된다."""
    source = tmp_path / "src"
    out = tmp_path / "out"
    one, one_sha = _original(source, "one.mov", "첫 원본".encode())
    two, two_sha = _original(source, "two.mov", "둘째 원본 — 다른 내용".encode())
    first_bytes = _pattern_pcm(1000, channels=2)
    second_bytes = _pattern_pcm(999, channels=2)
    destination = out / "ref.pcm"
    _fake_extract(monkeypatch, first_bytes, channels=2)
    old = build_reference("asset-1", "demo", one, one_sha, AudioPolicy(), destination)
    old_pointer = destination.read_bytes()

    recorder = _fake_extract(monkeypatch, second_bytes, channels=2)
    with pytest.MonkeyPatch.context() as injected:
        injected.setattr(pcm.os, "replace", _failing_replace(destination))
        with pytest.raises(OSError, match="출판 실패"):
            build_reference("asset-1", "demo", two, two_sha, AudioPolicy(), destination)

    assert destination.read_bytes() == old_pointer, "포인터는 부분 짝을 노출한 적이 없다"
    assert old.path.read_bytes() == first_bytes, "옛 세대 PCM이 그대로다"
    write_wav_slice(old, SampleRange(0, 100), out / "old-slice.wav")
    assert (out / "old-slice.wav").read_bytes()[44:] == first_bytes[: 100 * 2 * 2]
    # 포인터 + 옛 세대 둘 + 슬라이스 + 실패한 시도가 남긴 사유 세대 둘 — 지워진 게 없다.
    names = {path.name for path in out.iterdir()}
    assert len(names) == 6
    assert all(
        name.startswith("ref.pcm.") or name in {"ref.pcm", "old-slice.wav"} for name in names
    )
    assert len(recorder.calls) == 1, "실패한 시도의 추출 한 번뿐이다"

    again = build_reference("asset-1", "demo", one, one_sha, AudioPolicy(), destination)
    assert len(recorder.calls) == 1
    assert again == old


_CHILD_DIES_BEFORE_POINTER = """
import os
import sys
from fractions import Fraction
from pathlib import Path

from vlogsrt import preprocess
from vlogsrt.project import pcm

destination = Path(sys.argv[1])
source = Path(sys.argv[2])
original_sha256 = sys.argv[3]


def fake_extract(source_file, destination_file, *, stream_index, sample_rate=16000):
    destination_file.write_bytes(b"\\x55" * 2468)
    return preprocess.PcmExtraction(
        path=destination_file,
        stream=preprocess.AudioStreamInfo(
            stream_index=stream_index + 1,
            start_time=Fraction(0),
            sample_rate=44100,
            channels=2,
        ),
        sample_count=617,
        sample_width=2,
        pcm_sample0_file_time=Fraction(0),
    )


real_replace = os.replace


def dying(src, dst):
    if Path(str(dst)) == destination:
        os._exit(77)  # 포인터 출판 직전에 프로세스가 죽는다
    real_replace(src, dst)


pcm.extract_pcm_s16le = fake_extract
pcm.os.replace = dying
pcm.build_reference("asset-child", "child", source, original_sha256, pcm.AudioPolicy(), destination)
raise SystemExit("포인터 출판 앞에서 죽었어야 한다")
"""


def test_a_child_exit_before_pointer_commit_cannot_invalidate_the_old_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """포인터 앞에서 프로세스가 죽는다 — `finally` 같은 안전망은 없다. 그래도 옛 참조는 산다."""
    source = tmp_path / "src"
    out = tmp_path / "out"
    one, one_sha = _original(source, "one.mov", "첫 원본".encode())
    two, two_sha = _original(source, "two.mov", "둘째 원본 — 다른 내용".encode())
    first_bytes = _pattern_pcm(1000, channels=2)
    destination = out / "ref.pcm"
    _fake_extract(monkeypatch, first_bytes, channels=2)
    old = build_reference("asset-1", "demo", one, one_sha, AudioPolicy(), destination)
    old_pointer = destination.read_bytes()

    child = subprocess.run(
        [sys.executable, "-c", _CHILD_DIES_BEFORE_POINTER, str(destination), str(two), two_sha],
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
    )
    assert child.returncode == 77, f"자식이 포인터 앞에서 죽어야 한다: {child.stderr}"

    assert destination.read_bytes() == old_pointer, "포인터는 부분 짝을 노출한 적이 없다"
    assert old.path.read_bytes() == first_bytes, "옛 세대 PCM이 그대로다"
    write_wav_slice(old, SampleRange(0, 100), out / "old-slice.wav")
    assert (out / "old-slice.wav").read_bytes()[44:] == first_bytes[: 100 * 2 * 2]

    # 옛 입력의 재빌드는 재추출 없이 재사용한다 — 죽은 시도가 남긴 것은 아무 상관이 없다.
    recorder = _fake_extract(monkeypatch, first_bytes, channels=2)
    again = build_reference("asset-1", "demo", one, one_sha, AudioPolicy(), destination)
    assert len(recorder.calls) == 0
    assert again == old


def test_pointer_temp_cleanup_failure_after_commit_keeps_the_visible_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """갈아끼운 포인터의 임시파일 치우기가 죽어도 출판을 실패로 되돌리지 않는다.

    포인터가 이미 갈아끼워진 뒤의 치우기 실패가 세대 삭제로 이어지면 보이는
    포인터가 깨진다 — 그래서 커밋 뒤 치우기는 최선책이고, 어떤 세대도 지우지
    않는다.
    """
    source = tmp_path / "src"
    out = tmp_path / "out"
    one, one_sha = _original(source, "one.mov", "첫 원본".encode())
    two, two_sha = _original(source, "two.mov", "둘째 원본 — 다른 내용".encode())
    first_bytes = _pattern_pcm(1000, channels=2)
    second_bytes = _pattern_pcm(999, channels=2)
    destination = out / "ref.pcm"
    _fake_extract(monkeypatch, first_bytes, channels=2)
    old = build_reference("asset-1", "demo", one, one_sha, AudioPolicy(), destination)

    _fake_extract(monkeypatch, second_bytes, channels=2)
    state = {"committed": False}
    real_replace = os.replace
    real_unlink = Path.unlink

    def watching_replace(src: object, dst: object) -> None:
        real_replace(src, dst)
        if Path(str(dst)) == destination:
            state["committed"] = True

    def unlink(self: Path, missing_ok: bool = False) -> None:
        if state["committed"] and self.parent == out and self.name.endswith(".tmp"):
            raise PermissionError("치우기 실패 주입")
        real_unlink(self, missing_ok=missing_ok)

    with pytest.MonkeyPatch.context() as injected:
        injected.setattr(pcm.os, "replace", watching_replace)
        injected.setattr(Path, "unlink", unlink)
        new = build_reference("asset-1", "demo", two, two_sha, AudioPolicy(), destination)

    # 출판은 성공이다 — 보이는 포인터는 완전한 짝을 가리키고 새 참조가 돌아왔다.
    assert _generation_paths(destination)[0] == new.path
    assert new.path.read_bytes() == second_bytes
    write_wav_slice(new, SampleRange(0, 100), out / "new-slice.wav")
    assert (out / "new-slice.wav").read_bytes()[44:] == second_bytes[: 100 * 2 * 2]
    # 옛 참조도 그대로 산다 — 어떤 세대도 지워지지 않았다.
    write_wav_slice(old, SampleRange(0, 100), out / "old-slice.wav")
    assert (out / "old-slice.wav").read_bytes()[44:] == first_bytes[: 100 * 2 * 2]


def test_an_interrupt_at_the_commit_boundary_cannot_break_the_visible_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """포인터가 갈아끼워진 직후에 죽는 인터럽트 — 세대 삭제는 없다, 포인터는 완전하다."""
    source = tmp_path / "src"
    out = tmp_path / "out"
    one, one_sha = _original(source, "one.mov", "첫 원본".encode())
    two, two_sha = _original(source, "two.mov", "둘째 원본 — 다른 내용".encode())
    first_bytes = _pattern_pcm(1000, channels=2)
    second_bytes = _pattern_pcm(999, channels=2)
    destination = out / "ref.pcm"
    _fake_extract(monkeypatch, first_bytes, channels=2)
    old = build_reference("asset-1", "demo", one, one_sha, AudioPolicy(), destination)

    recorder = _fake_extract(monkeypatch, second_bytes, channels=2)
    real_replace = os.replace

    def interrupting_replace(src: object, dst: object) -> None:
        real_replace(src, dst)
        if Path(str(dst)) == destination:
            raise KeyboardInterrupt  # 갈아끼움 직후의 죽음

    with pytest.MonkeyPatch.context() as injected:
        injected.setattr(pcm.os, "replace", interrupting_replace)
        with pytest.raises(KeyboardInterrupt):
            build_reference("asset-1", "demo", two, two_sha, AudioPolicy(), destination)

    # 보이는 포인터는 새 세대의 완전한 짝을 가리킨다 — 지워진 것이 없다.
    new_pcm, new_meta = _generation_paths(destination)
    assert new_pcm.read_bytes() == second_bytes
    assert new_meta.is_file()
    # 재빌드는 그 짝을 재추출 없이 재사용한다 — 재추출이 결함을 덮는 일은 없게.
    again = build_reference("asset-1", "demo", two, two_sha, AudioPolicy(), destination)
    assert len(recorder.calls) == 1, "인터럽트된 시도의 추출 한 번뿐이다"
    assert again.path == new_pcm
    assert again.pcm_sha256 == hashlib.sha256(second_bytes).hexdigest()
    # 옛 참조도 그대로 산다.
    write_wav_slice(old, SampleRange(0, 100), out / "old-slice.wav")
    assert (out / "old-slice.wav").read_bytes()[44:] == first_bytes[: 100 * 2 * 2]


def test_a_successful_changed_source_update_retains_the_old_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """성공한 갱신은 포인터를 옮기고 새 세대를 돌려준다 — 옛 세대도 지우지 않는다."""
    source = tmp_path / "src"
    out = tmp_path / "out"
    one, one_sha = _original(source, "one.mov", "첫 원본".encode())
    two, two_sha = _original(source, "two.mov", "둘째 원본 — 다른 내용".encode())
    first_bytes = _pattern_pcm(1000, channels=2)
    second_bytes = _pattern_pcm(999, channels=2)
    destination = out / "ref.pcm"
    _fake_extract(monkeypatch, first_bytes, channels=2)
    old = build_reference("asset-1", "demo", one, one_sha, AudioPolicy(), destination)

    _fake_extract(monkeypatch, second_bytes, channels=2)
    new = build_reference("asset-1", "demo", two, two_sha, AudioPolicy(), destination)

    assert new.path != old.path, "다른 내용은 다른 불변 세대다"
    assert new.pcm_sha256 != old.pcm_sha256
    assert _generation_paths(destination)[0] == new.path, "포인터는 새 세대를 가리킨다"
    assert new.path.read_bytes() == second_bytes
    assert old.path.is_file() and old.path.read_bytes() == first_bytes, "옛 세대가 남는다"
    write_wav_slice(old, SampleRange(0, 100), out / "old-slice.wav")
    assert (out / "old-slice.wav").read_bytes()[44:] == first_bytes[: 100 * 2 * 2]
    # 포인터 + 옛 세대 둘 + 새 세대 둘 + 슬라이스 — 아무것도 지워지지 않았다.
    assert len(list(out.iterdir())) == 6


def test_verification_recomputes_the_analysis_key_from_persisted_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """포인터가 세대를 가리키려면 세대 기록만으로 분석 키가 다시 만들어져야 한다.

    기록의 사실이 참조의 키와 어긋나면 출판하던 중에 죽는다 — 기록이 증명하지
    못하는 세대를 포인터가 이름 붙일 수 없게.
    """
    source = tmp_path / "src"
    out = tmp_path / "out"
    original, original_sha = _original(source, "in.mov", b"synthetic-original")
    _fake_extract(monkeypatch, _pattern_pcm(100, channels=2), channels=2)
    destination = out / "ref.pcm"
    reference = build_reference(
        "asset-1", "demo", original, original_sha, AudioPolicy(), destination
    )
    generation_pcm, generation_meta = _generation_paths(destination)
    policy = AudioPolicy()

    pcm._verify_generation(generation_pcm, generation_meta, reference, policy)  # 온전한 짝은 지난다

    forged = dataclasses_replace(reference, analysis_key="0" * 64)
    with pytest.raises(RuntimeError, match="분석 키"):
        pcm._verify_generation(generation_pcm, generation_meta, forged, policy)


def test_concurrent_writers_get_exclusive_private_generations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """동시에 쓰는 둘은 저마다의 사유 세대를 만든다 — 세대 생성은 독점적이어야 한다.

    내용에서 유도한 이름은 독점 생성이 아니다: 같은 주소로 동시에 쓰는 writer 둘이
    서로의 세대를 덮으면 안 된다. mkstemp의 배타적 생성이 그 선을 지킨다.
    """
    from threading import Thread

    source = tmp_path / "src"
    out = tmp_path / "out"
    one, one_sha = _original(source, "one.mov", "첫 원본".encode())
    two, two_sha = _original(source, "two.mov", "둘째 원본 — 다른 내용".encode())
    first_bytes = _pattern_pcm(1000, channels=2)
    second_bytes = _pattern_pcm(999, channels=2)
    payloads = {one: first_bytes, two: second_bytes}
    destination = out / "ref.pcm"

    def extract(
        source_file: Path, destination_file: Path, *, stream_index: int, sample_rate: int = 16000
    ) -> preprocess.PcmExtraction:
        pcm_bytes = payloads[source_file]
        destination_file.write_bytes(pcm_bytes)
        return preprocess.PcmExtraction(
            path=destination_file,
            stream=preprocess.AudioStreamInfo(
                stream_index=stream_index + 1,
                start_time=Fraction(0),
                sample_rate=44100,
                channels=2,
            ),
            sample_count=len(pcm_bytes) // 4,
            sample_width=2,
            pcm_sample0_file_time=Fraction(0),
        )

    monkeypatch.setattr(pcm, "extract_pcm_s16le", extract)
    outcome: dict[str, object] = {}

    def worker(name: str, original: Path, original_sha: str) -> None:
        try:
            outcome[name] = build_reference(
                "asset-1", "demo", original, original_sha, AudioPolicy(), destination
            )
        except BaseException as exc:  # Thread는 예외를 삼키므로 직접 받는다
            outcome[name] = exc

    threads = [
        Thread(target=worker, args=("one", one, one_sha)),
        Thread(target=worker, args=("two", two, two_sha)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    references = dict(outcome)
    assert not [name for name, value in references.items() if isinstance(value, BaseException)]
    generation_pcms = {value.path for value in references.values()}
    assert len(generation_pcms) == 2, "writer마다 저마다의 세대다 — 겹치지 않는다"

    pointed = _generation_paths(destination)[0]
    assert pointed in generation_pcms, "포인터는 둘 중 하나의 온전한 세대를 가리킨다"


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


def test_every_consumer_verifies_the_stored_hash_not_just_size(tmp_path: Path) -> None:
    """같은 길이의 변조는 크기 검사를 지난다 — 해시까지 봐야 잡힌다."""
    pcm_bytes = bytearray(_pattern_pcm(1000, channels=1))
    reference = _hand_reference(tmp_path / "ref.pcm", bytes(pcm_bytes))
    pcm_bytes[len(pcm_bytes) // 2] ^= 0xFF  # 길이는 그대로, 내용은 다르다
    (tmp_path / "ref.pcm").write_bytes(bytes(pcm_bytes))

    with pytest.raises(RuntimeError, match="어긋"):
        write_wav_slice(reference, SampleRange(0, 100), tmp_path / "slice.wav")
    with pytest.raises(RuntimeError, match="어긋"):
        quiet_sample(reference, target=500, search_samples=10, window_samples=10)
    assert not (tmp_path / "slice.wav").exists()


def test_slice_refuses_to_overwrite_the_stored_pcm(tmp_path: Path) -> None:
    """슬라이스가 세대 PCM을 갈아끼우는 일은 없다 — 경로가 같으면 시작도 안 한다."""
    pcm_bytes = _pattern_pcm(100, channels=1)
    stored = tmp_path / "ref.pcm"
    reference = _hand_reference(stored, pcm_bytes)
    alias = tmp_path / "alias.pcm"
    os.symlink(stored, alias)

    for destination in (stored, alias):
        with pytest.raises(ValueError, match="저장 PCM 자리"):
            write_wav_slice(reference, SampleRange(0, 10), destination)

    assert stored.read_bytes() == pcm_bytes
    assert alias.read_bytes() == pcm_bytes
    assert [path.name for path in tmp_path.iterdir()] == ["ref.pcm", "alias.pcm"]


@pytest.mark.parametrize("failure", ["replace", "wave_open"])
def test_slice_publication_is_atomic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """쓰다 죽어도 있던 파일은 그대로고 임시파일은 남지 않는다 — 주입으로 증명한다."""
    reference = _hand_reference(tmp_path / "ref.pcm", _pattern_pcm(100, channels=1))
    destination = tmp_path / "slice.wav"
    previous = "이전 슬라이스".encode()
    destination.write_bytes(previous)

    with pytest.MonkeyPatch.context() as injected:
        if failure == "replace":
            injected.setattr(pcm.os, "replace", _failing_replace(destination))
        else:

            def broken_open(*args: object, **kwargs: object) -> object:
                raise OSError("WAV 쓰기 시작 실패 주입")

            injected.setattr(pcm.wave, "open", broken_open)
        with pytest.raises(OSError):
            write_wav_slice(reference, SampleRange(0, 10), destination)

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


# ─────────────────────────────────────────────────────────────
#  원점 매핑 — file_time = sample/rate + 원점 (비공개 도우미)
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
    assert pcm._sample_file_time(reference, index) == expected


@pytest.mark.parametrize("bad", [1.0, -1, True, 16002, "0"])
def test_file_time_rejects_indexes_that_are_not_frames(tmp_path: Path, bad: object) -> None:
    """프레임 번호는 0 이상 정수다 — 저장 범위 밖의 시각은 지어내지 않는다."""
    reference = _hand_reference(tmp_path / "ref.pcm", _pattern_pcm(16000, channels=1))
    with pytest.raises(ValueError, match="프레임|정수"):
        pcm._sample_file_time(reference, bad)  # type: ignore[arg-type]


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
    tmp_path.mkdir(parents=True, exist_ok=True)
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
    source = tmp_path / "src"
    out = tmp_path / "out"
    media = _mux_shifted_impulse(source, "plus.mov", codec="pcm_s16le", offset=Fraction(2))
    original_sha = hashlib.sha256(media.read_bytes()).hexdigest()
    reference = build_reference(
        "asset-1", "plus", media, original_sha, AudioPolicy(), out / "ref.pcm"
    )

    assert reference.pcm_sample0_file_time == 2
    assert reference.channels == 2
    assert reference.sample_rate == 16000
    assert reference.sample_count == 16000  # 1초 × 16000 — 딱
    assert pcm._sample_file_time(reference, 4000) == Fraction(9, 4)

    # 슬라이스 [3999, 4001): 저장 PCM의 그 두 프레임이 그대로 들어 있다
    write_wav_slice(reference, SampleRange(3999, 4001), out / "around.wav")
    _, decoded = _decoded(out / "around.wav")
    assert decoded == [0, 0, 20000, -12000]

    # [0, 4000)에는 임펄스가 없다 — 자른 범위 밖의 소리는 안 나온다
    write_wav_slice(reference, SampleRange(0, 4000), out / "head.wav")
    assert _decoded(out / "head.wav")[1] == [0] * 8000

    # 자를 자리: 목표 4000 근처에서 임펄스를 피해 한 프레임 뒤로 간다
    assert quiet_sample(reference, target=4000, search_samples=100, window_samples=10) == 4001


@needs_ffmpeg
def test_negative_epoch_from_aac_mkv_survives_the_reference_real(tmp_path: Path) -> None:
    """결정적인 AAC/MKV 행: 컨테이너 start_time은 −33/125지만 원점은 −1/5다."""
    source = tmp_path / "src"
    out = tmp_path / "out"
    media = _mux_shifted_impulse(source, "minus.mkv", codec="aac", offset=Fraction(-1, 5))
    original_sha = hashlib.sha256(media.read_bytes()).hexdigest()
    reference = build_reference(
        "asset-1", "minus", media, original_sha, AudioPolicy(), out / "ref.pcm"
    )

    assert reference.pcm_sample0_file_time == Fraction(-1, 5)
    assert reference.pcm_sample0_file_time != Fraction(-33, 125)
    assert reference.sample_count == 16384  # 프라이밍이 잘려난 뒤의 실측(P2-01 표)

    # 슬라이스는 무손실 복사다 — 저장 PCM의 해당 바이트와 한 바이트도 다르지 않다
    stored = reference.path.read_bytes()
    write_wav_slice(reference, SampleRange(8000, 8010), out / "slice.wav")
    assert (out / "slice.wav").read_bytes()[44:] == stored[8000 * 4 : 8010 * 4]


@needs_ffmpeg
def test_real_rebuild_reuses_the_verified_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """진짜 추출로 만든 참조도 두 번째엔 ffmpeg를 부르지 않는다."""
    source = tmp_path / "src"
    out = tmp_path / "out"
    media = _mux_shifted_impulse(source, "plus.mov", codec="pcm_s16le", offset=Fraction(2))
    original_sha = hashlib.sha256(media.read_bytes()).hexdigest()
    real_extract = pcm.extract_pcm_s16le
    calls: list[Path] = []

    def counting(
        source_file: Path, destination: Path, *, stream_index: int, sample_rate: int = 16000
    ) -> preprocess.PcmExtraction:
        calls.append(destination)
        return real_extract(
            source_file, destination, stream_index=stream_index, sample_rate=sample_rate
        )

    monkeypatch.setattr(pcm, "extract_pcm_s16le", counting)
    destination = out / "ref.pcm"
    first = build_reference("asset-1", "plus", media, original_sha, AudioPolicy(), destination)
    second = build_reference("asset-1", "plus", media, original_sha, AudioPolicy(), destination)

    assert len(calls) == 1
    assert second == first
