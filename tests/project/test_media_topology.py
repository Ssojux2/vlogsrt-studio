"""실제 원본 미디어의 오디오 스트림 위상 — 선언이 아니라 증거로 거절한다(S2A-2).

여기서 증명하는 것:

- 스트림 수 확인은 영상 유무와 무관하다 — 오디오만 있는 원본도 센다
  (`probe_media_summary`의 영상 요구는 컷 합성용 계약이지 위상 검사가 아니다).
- 도구 부재·마감 초과·비0 종료·깨진 JSON은 전부 실행 가능한 실패다 —
  '스트림 0개·무음'으로 해석되는 길이 없다.
- 문턱은 스트림 **수**다 — 스테레오·멀티채널 채널은 한 스트림이므로 지나간다.
- 같은 원본은 한 번만 확인한다(여러 리소스가 같은 파일을 가리킬 때).
- 추정(`inspect`)이 위상 관문을 지난다(유료 분석 문 `analyze`는 D163으로 지웠다).
- XML 선언(`hasAudio=0`)이 남은 트랙을 숨일 수 없다 — 선언과 무관하게 실제
  스트림을 센다. 0스트림·1스트림은 계속 지원한다.
- 앱이 스테이징한 권한 문서는 낡은 camelCase 철자도 읽히고, 원본이 그 루트
  안인지 엔진이 스스로 잰다 — Swift를 거치지 않은 직접 실행도 같은 문이다.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from types import ModuleType

import pytest

from tests._dotenv_guard import stage_synthetic_dotenv
from vlogsrt import cli, preprocess
from vlogsrt.project.media import ResolvedMedia


@pytest.fixture(autouse=True)
def offline_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """CLI 호출 앞의 합성 dotenv·cwd — 실제 저장소로 흐를 길이 없다(S2A-1 원리).

    cli.main의 ensure_loaded가 cwd 조상을 거슬러 올라가므로, 합성 dotenv를
    tmp_path 꼭대기에 두고 실행 위치도 그 안으로 옮겨 탐색이 실제 저장소
    (PACKAGE_ROOT fallback 포함)에 닿기 전에 멈춘다. conftest의 dotenv 보호가
    그 경계를 못 박는다.
    """
    dotenv = stage_synthetic_dotenv(tmp_path)
    monkeypatch.chdir(tmp_path)
    yield dotenv


@pytest.fixture(autouse=True)
def restore_role_application_state() -> None:
    """V2 운반 적용이 남기는 과정 상태를 시험 뒤 되돌린다(S2A-2).

    `apply_role_credentials`는 환경 이름과 `env._applied_vision_key_name` 표식을
    남긴다 — 표식이 남으면 뒤의 마커 없는 시험들이 '앱 관리'로 읽혀 권한 문서를
    요구한다. test_engine_carrier의 재설정 계약과 같은 원리로 시험이 치운다.
    """
    from vlogsrt import env

    saved_marker = env._applied_vision_key_name
    saved_names = {
        name: os.environ.get(name)
        for name in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "GOOGLE_API_KEY", "GEMINI_BASE_URL")
    }
    yield
    env._applied_vision_key_name = saved_marker
    for name, value in saved_names.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


def _resolved(path: Path, *, has_audio: bool, resource: str = "r2") -> ResolvedMedia:
    """위상 검사의 재료 — 미디어 해석이 끝난 한 리소스의 모양만 흉내 낸다."""
    return ResolvedMedia(
        resource_scoped_id=resource,
        asset_content_id="0" * 64,
        original=path,
        image_source=path,
        original_sha256="0" * 64,
        has_audio=has_audio,
        used_proxy=False,
    )


def _streams_payload(*codec_types: str) -> str:
    return json.dumps({"streams": [{"codec_type": kind} for kind in codec_types]})


# ─────────────────────────────────────────────────────────────
#  스트림 수 확인 — 영상 없는 원본도, 실패는 결코 0이 아니다
# ─────────────────────────────────────────────────────────────


def test_stream_count_probe_reads_streams_without_requiring_video(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """오디오만 있는 원본(영상 스트림 0)도 센다 — probe_media_summary와 갈라진 점."""
    commands: list[list[str]] = []

    def run_probe(command: list[str]) -> str:
        commands.append(command)
        return _streams_payload("audio")

    monkeypatch.setattr(preprocess, "_run_probe", run_probe)
    monkeypatch.setattr(preprocess, "require_ffmpeg", lambda tool="ffmpeg": "ffprobe")
    source = tmp_path / "voice-only.m4a"
    source.write_bytes(b"synthetic-audio-only")

    assert preprocess.probe_audio_stream_count(source) == 1

    (command,) = commands
    assert command[-1] == str(source)
    assert "codec_type" in " ".join(command)


@pytest.mark.parametrize(
    "payload",
    [
        _streams_payload(),
        _streams_payload("audio"),
        _streams_payload("video", "audio"),
        _streams_payload("audio", "audio", "audio"),
    ],
)
def test_stream_count_probe_counts_audio_streams_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: str
) -> None:
    monkeypatch.setattr(preprocess, "_run_probe", lambda command: payload)
    monkeypatch.setattr(preprocess, "require_ffmpeg", lambda tool="ffmpeg": "ffprobe")
    source = tmp_path / "clip.mov"
    source.write_bytes(b"synthetic")
    assert preprocess.probe_audio_stream_count(source) == payload.count('"audio"')


@pytest.mark.parametrize(
    "problem",
    ["missing-tool", "nonzero-exit", "malformed-json", "streams-not-a-list", "timeout"],
)
def test_probe_failures_are_actionable_never_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, problem: str
) -> None:
    """도구가 없거나·ffprobe가 비0으로 끝나거나·출력이 깨졌거나·마감이 지나면
    실패로 닫는다 — 0스트림(무음)으로 해석되는 길이 없다."""
    source = tmp_path / "clip.mov"
    source.write_bytes(b"synthetic")
    monkeypatch.setattr(preprocess, "require_ffmpeg", lambda tool="ffmpeg": "ffprobe")

    if problem == "missing-tool":
        monkeypatch.setattr(
            preprocess,
            "require_ffmpeg",
            lambda tool="ffmpeg": (_ for _ in ()).throw(RuntimeError("찾지 못했습니다")),
        )
    elif problem == "malformed-json":
        monkeypatch.setattr(preprocess, "_run_probe", lambda command: "not json at all")
    elif problem == "streams-not-a-list":
        monkeypatch.setattr(preprocess, "_run_probe", lambda command: '{"streams": 3}')
    else:

        def failing(command: list[str]) -> str:
            raise RuntimeError(f"실패 모양: {problem}")

        monkeypatch.setattr(preprocess, "_run_probe", failing)

    with pytest.raises(RuntimeError):
        preprocess.probe_audio_stream_count(source)


def test_probe_runner_passes_a_timeout_to_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """마감은 ffprobe 호출에 붙어 있다 — 멈춘 자식을 무한히 기다리지 않는다."""

    class _Done:
        returncode = 0
        stdout = '{"streams": []}'
        stderr = ""

    seen: dict[str, object] = {}

    def bounded_run(command: object, **kwargs: object) -> _Done:
        seen.update(kwargs)
        return _Done()

    monkeypatch.setattr(preprocess.subprocess, "run", bounded_run)
    monkeypatch.setattr(preprocess, "require_ffmpeg", lambda tool="ffmpeg": "ffprobe")

    assert preprocess._run_probe(["ffprobe", "-v", "error"]) == '{"streams": []}'
    assert isinstance(seen.get("timeout"), (int, float))
    assert float(seen["timeout"]) > 0  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("label", "entry"),
    [
        ("absent", {}),
        ("null", {"codec_type": None}),
        ("wrong-type", {"codec_type": 3}),
        ("unknown", {"codec_type": "mystery"}),
    ],
)
def test_stream_count_probe_fails_closed_on_malformed_stream_classifications(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    label: str,
    entry: dict[str, object],
) -> None:
    """분류를 모르는 스트림 항목은 죽는다 — 깨진 증거가 0스트림(무음)으로
    위장되는 길이 없다. 항목이 dict라서 `_stream_entries`는 통과하는 모양이다."""
    payload = json.dumps({"streams": [entry, {"codec_type": "audio"}]})
    monkeypatch.setattr(preprocess, "_run_probe", lambda command: payload)
    monkeypatch.setattr(preprocess, "require_ffmpeg", lambda tool="ffmpeg": "ffprobe")
    source = tmp_path / "clip.mov"
    source.write_bytes(b"synthetic")

    with pytest.raises(RuntimeError):
        preprocess.probe_audio_stream_count(source)


def test_stream_count_probe_accepts_every_legitimate_stream_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """정당한 스트림 종류는 전부 지나간다 — 비디오·자막·데이터·첨부는 오디오가
    아닐 뿐이고, 빈 목록은 0스트림(지원되는 상태)이다."""
    payload = _streams_payload("video", "audio", "subtitle", "data", "attachment", "audio")
    monkeypatch.setattr(preprocess, "_run_probe", lambda command: payload)
    monkeypatch.setattr(preprocess, "require_ffmpeg", lambda tool="ffmpeg": "ffprobe")
    source = tmp_path / "clip.mov"
    source.write_bytes(b"synthetic")

    assert preprocess.probe_audio_stream_count(source) == 2

    monkeypatch.setattr(preprocess, "_run_probe", lambda command: '{"streams": []}')
    assert preprocess.probe_audio_stream_count(source) == 0


# ─────────────────────────────────────────────────────────────
#  위상 관문 — 스트림 수로만 거절한다, 선언은 증거를 못 감춘다
# ─────────────────────────────────────────────────────────────


@pytest.fixture()
def topology() -> ModuleType:
    from vlogsrt.project import topology as module

    return module


def _fake_counts(
    monkeypatch: pytest.MonkeyPatch, topology: ModuleType, counts: dict[str, int]
) -> list[Path]:
    probed: list[Path] = []

    def probe(source: Path) -> int:
        probed.append(source)
        return counts.get(source.name, 1)

    monkeypatch.setattr(topology, "probe_audio_stream_count", probe)
    return probed


def test_gate_rejects_more_than_one_stream_despite_a_zero_audio_declaration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, topology: ModuleType
) -> None:
    """XML이 hasAudio=0이라고 선언해도 실제 스트림이 둘이면 거절한다."""
    source = tmp_path / "dual.mov"
    source.write_bytes(b"synthetic-dual")
    probed = _fake_counts(monkeypatch, topology, {"dual.mov": 2})
    row = _resolved(source, has_audio=False)  # 선언은 '오디오 없음'

    with pytest.raises(RuntimeError) as raised:
        topology.require_supported_audio_topology((row,))

    message = str(raised.value)
    assert "dual.mov" in message
    assert "믹스다운" in message
    assert len(probed) == 1


def test_gate_passes_zero_and_single_streams(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, topology: ModuleType
) -> None:
    """0스트림·1스트림은 계속 지원한다 — 스테레오 채널은 한 스트림이다."""
    silent = tmp_path / "silent.mov"
    silent.write_bytes(b"synthetic-silent")
    stereo = tmp_path / "stereo.mov"
    stereo.write_bytes(b"synthetic-stereo")
    _fake_counts(monkeypatch, topology, {"silent.mov": 0, "stereo.mov": 1})

    topology.require_supported_audio_topology(
        (_resolved(silent, has_audio=False), _resolved(stereo, has_audio=True))
    )


def test_gate_probes_each_unique_original_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, topology: ModuleType
) -> None:
    """같은 원본을 가리키는 리소스 여럿은 한 번만 확인한다."""
    shared = tmp_path / "shared.mov"
    shared.write_bytes(b"synthetic-shared")
    probed = _fake_counts(monkeypatch, topology, {})

    topology.require_supported_audio_topology(
        (
            _resolved(shared, has_audio=True, resource="r2"),
            _resolved(shared, has_audio=True, resource="r3"),
            _resolved(shared, has_audio=True, resource="r4"),
        )
    )

    assert probed == [shared]


def test_gate_failure_is_actionable_not_silence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, topology: ModuleType
) -> None:
    """관문 안의 확인 실패는 그대로 올라간다 — 부재(0)로 번역하지 않는다."""
    source = tmp_path / "clip.mov"
    source.write_bytes(b"synthetic")

    def broken(probe_source: Path) -> int:
        raise RuntimeError("ffprobe 실패 (코드 1)")

    monkeypatch.setattr(topology, "probe_audio_stream_count", broken)
    with pytest.raises(RuntimeError, match="ffprobe"):
        topology.require_supported_audio_topology((_resolved(source, has_audio=True),))


# ─────────────────────────────────────────────────────────────
#  파이프라인 배선 — 추정이 위상을 드러낸다
# ─────────────────────────────────────────────────────────────


def _staged_workspace(tmp_path: Path) -> Path:
    """receive까지 끝낸 합성 작업공간 — 미디어는 합성 바이트다.

    호출자가 tmp_path의 **안쪽** 폴더를 줄 수 있으므로 뿌리부터 만든다.
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    media = tmp_path / "clip.mov"
    media.write_bytes(b"synthetic-topology-media")
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>
<fcpxml version="1.14">
  <resources>
    <format id="r1" name="FFVideoFormat1080p25" frameDuration="1/25s"/>
    <asset id="r2" name="clip.mov" uid="ASSET-1" start="0s" duration="12s" hasAudio="1" format="r1">
      <media-rep kind="original-media" src="{media.as_uri()}"/>
    </asset>
  </resources>
  <library>
    <event name="이벤트">
      <project name="위상">
        <sequence format="r1" tcStart="0s" duration="12s">
          <spine>
            <asset-clip ref="r2" name="클립0" offset="0s" start="0s" duration="12s"/>
          </spine>
        </sequence>
      </project>
    </event>
  </library>
</fcpxml>
"""
    input_path = tmp_path / "input.fcpxml"
    input_path.write_text(xml, encoding="utf-8")
    workspace = tmp_path / "workspace"
    assert cli.main(["project", "receive", str(input_path), "--workspace", str(workspace)]) == 0
    return workspace


def _wire_stream_count(monkeypatch: pytest.MonkeyPatch, counts: dict[str, int]) -> None:
    """실제 관문이 부르는 확인 함수를 합성 응답으로 갈아끼운다."""
    from vlogsrt.project import commands as project_commands

    def probe(source: Path) -> int:
        return counts.get(source.name, 1)

    monkeypatch.setattr(project_commands.topology, "probe_audio_stream_count", probe)


def test_key_free_inspect_surfaces_unsupported_topology(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """키 없는 추정이 지원하지 않는 위상을 드러낸다 — 추정 파일은 남지 않는다."""
    workspace = _staged_workspace(tmp_path)
    _wire_stream_count(monkeypatch, {"clip.mov": 2})

    assert cli.main(["project", "inspect", str(workspace)]) == 2
    assert not (workspace / "analysis-estimate.jsonl").is_file()


def test_single_and_zero_stream_estimates_remain_supported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """1스트림·0스트림 원본의 범위 확인은 그대로 통과한다(D110: 인쇄만, 파일은 없다)."""
    workspace = _staged_workspace(tmp_path)
    _wire_stream_count(monkeypatch, {"clip.mov": 1})
    assert cli.main(["project", "inspect", str(workspace)]) == 0
    assert not (workspace / "analysis-estimate.jsonl").is_file()

    zero = _staged_workspace(tmp_path / "second")
    _wire_stream_count(monkeypatch, {"clip.mov": 0})
    assert cli.main(["project", "inspect", str(zero)]) == 0


# ─────────────────────────────────────────────────────────────
#  V2 역할 적용 — 아래 권한 문서 시험이 쓰는 도우미(S2A-2)
# ─────────────────────────────────────────────────────────────


def _apply_v2_roles(
    *, stt: str = "synthetic-stt-key", vision: str = "synthetic-vision-key"
) -> None:
    """실제 V2 운반 적용 — 시험 값은 합성물이고 restore 픽스처가 치운다."""
    from vlogsrt import env

    env.apply_role_credentials(stt, vision, "https://example.invalid/v1")


# ─────────────────────────────────────────────────────────────
#  권한 문서 — 낡은 철자도 읽고, 커버리지는 엔진이 잰다(S2A-2)
# ─────────────────────────────────────────────────────────────


def test_staged_grant_reader_accepts_snake_case_and_legacy_camel_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Swift가 오늘 쓰는 snake_case와 낡은 camelCase 문서 둘 다 읽힌다 —
    두 철자가 함께 있으면 거부한다(정본을 못 고르므로)."""
    from vlogsrt.project import commands as project_commands

    revision = "a" * 64
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    (workspace / "media-grant.json").write_text(
        json.dumps({"grant_revision": revision, "roots": [str(tmp_path)]}), encoding="utf-8"
    )
    assert project_commands._staged_app_grant_revision(workspace) == revision

    # Swift JSONEncoder가 실제로 내는 모양(sortedKeys·슬래시 무이스케이프).
    root_text = json.dumps(str(tmp_path))[1:-1]  # Windows 역슬래시는 JSON에서 이스케이프된다
    swift_written = '{"grant_revision":"' + revision + '","roots":["' + root_text + '"]}\n'
    (workspace / "media-grant.json").write_text(swift_written, encoding="utf-8")
    assert project_commands._staged_app_grant_revision(workspace) == revision

    (workspace / "media-grant.json").write_text(
        json.dumps({"grantRevision": revision, "roots": [str(tmp_path)]}), encoding="utf-8"
    )
    assert project_commands._staged_app_grant_revision(workspace) == revision

    (workspace / "media-grant.json").write_text(
        json.dumps(
            {"grant_revision": revision, "grantRevision": revision, "roots": [str(tmp_path)]}
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="철자가 둘 다"):
        project_commands._staged_app_grant_revision(workspace)


def _grant_document(roots: list[str]) -> str:
    """루트와 지문이 묶인 권한 문서 — Swift가 쓰는 모양과 같은 재료."""
    from vlogsrt.project import commands as project_commands

    return json.dumps(
        {"grant_revision": project_commands._swift_grant_revision(roots), "roots": roots}
    )


def test_engine_independently_enforces_staged_grant_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """권한 문서가 스테이징되어 있으면 원본이 루트 안인지 **엔진이** 잰다 —
    루트 밖이면 키·위상과 무관하게 추정부터 닫힌다. 문서가 없으면 지나간다."""
    workspace = _staged_workspace(tmp_path)
    _wire_stream_count(monkeypatch, {"clip.mov": 1})

    (workspace / "media-grant.json").write_text(
        _grant_document([str(tmp_path / "elsewhere")]), encoding="utf-8"
    )
    assert cli.main(["project", "inspect", str(workspace)]) == 2
    assert "권한 루트 밖" in capsys.readouterr().err
    assert not (workspace / "analysis-estimate.jsonl").is_file()

    (workspace / "media-grant.json").write_text(_grant_document([str(tmp_path)]), encoding="utf-8")
    assert cli.main(["project", "inspect", str(workspace)]) == 0
    assert not (workspace / "analysis-estimate.jsonl").is_file()


def test_coverage_rejects_before_topology_probes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """루트 밖 미디어는 ffprobe로 찔러 보기 **전에** 거절된다 — 권한 없는 경로를
    탐색하고 나서 거절하는 순서가 아니어야 한다."""
    workspace = _staged_workspace(tmp_path)
    probed: list[Path] = []

    from vlogsrt.project import commands as project_commands
    from vlogsrt.project import topology as topology_module

    def probe(source: Path) -> int:
        probed.append(source)
        return 1

    monkeypatch.setattr(topology_module, "probe_audio_stream_count", probe)
    (workspace / "media-grant.json").write_text(
        _grant_document([str(tmp_path / "elsewhere")]), encoding="utf-8"
    )

    assert cli.main(["project", "inspect", str(workspace)]) == 2
    assert "권한 루트 밖" in capsys.readouterr().err
    assert probed == []  # 위상 확인은 아직 시작하지 않았다
    _ = project_commands


def test_app_managed_execution_requires_a_staged_grant_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """V2 운반이 적용된 실행에는 권한 문서가 필수다 — 마커 없는 직접 CLI는
    문서 없이 그대로 돈다(계약 보존). 실제 운반 적용으로 잰다."""
    workspace = _staged_workspace(tmp_path)
    _wire_stream_count(monkeypatch, {"clip.mov": 1})

    _apply_v2_roles()  # 실제 V2 적용 — restore 픽스처가 치운다
    assert cli.main(["project", "inspect", str(workspace)]) == 2
    assert "권한 문서" in capsys.readouterr().err
    assert not (workspace / "analysis-estimate.jsonl").is_file()

    (workspace / "media-grant.json").write_text(_grant_document([str(tmp_path)]), encoding="utf-8")
    assert cli.main(["project", "inspect", str(workspace)]) == 0
    assert not (workspace / "analysis-estimate.jsonl").is_file()

    # 마커 없는 직접 CLI는 문서 없이도 추정이 나온다 — 기본선 계약 보존.
    from vlogsrt import env

    env._applied_vision_key_name = None  # 표식을 내려 마커 없는 상태로 잰다
    direct = _staged_workspace(tmp_path / "markerless")
    _wire_stream_count(monkeypatch, {"clip.mov": 1})
    assert cli.main(["project", "inspect", str(direct)]) == 0
    assert not (direct / "analysis-estimate.jsonl").is_file()  # D110: inspect는 범위를 인쇄만 한다


def test_staged_grant_rejects_malformed_roots_and_mismatched_fingerprints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """상대 경로·빈 루트·지문 불일치는 권한이 아니라 깨진 문서다."""
    workspace = _staged_workspace(tmp_path)
    _wire_stream_count(monkeypatch, {"clip.mov": 1})

    (workspace / "media-grant.json").write_text(
        json.dumps({"grant_revision": "a" * 64, "roots": ["relative/root"]}), encoding="utf-8"
    )
    assert cli.main(["project", "inspect", str(workspace)]) == 2
    assert "절대 경로" in capsys.readouterr().err

    (workspace / "media-grant.json").write_text(
        json.dumps({"grant_revision": "a" * 64, "roots": []}), encoding="utf-8"
    )
    assert cli.main(["project", "inspect", str(workspace)]) == 2

    (workspace / "media-grant.json").write_text(
        json.dumps({"grant_revision": "b" * 64, "roots": [str(tmp_path)]}), encoding="utf-8"
    )
    assert cli.main(["project", "inspect", str(workspace)]) == 2
    assert "지문" in capsys.readouterr().err
    assert not (workspace / "analysis-estimate.jsonl").is_file()


def test_roughcut_destination_writability_is_proven_before_rendering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """읽기 전용 자리·디렉터리 자리는 렌더 전에 교정 글로 닫는다 — 상태 기계를
    review_required까지 실제로 밀어서, 이 실패가 쓰기 증명 때문임을 못 박는다."""
    workspace = _staged_workspace(tmp_path)
    _wire_stream_count(monkeypatch, {"clip.mov": 1})
    assert cli.main(["project", "inspect", str(workspace)]) == 0
    # 분석 문(`project analyze`)은 D163으로 지웠다 — 빈 후보로 review_required까지 저널을 민다.
    from vlogsrt.project.workflow_protocol import WorkflowJournal

    (workspace / "candidates.jsonl").write_text("", encoding="utf-8")
    journal = WorkflowJournal.open(workspace / "workflow.jsonl")
    for state in ("authorized", "analyzing", "review_required"):
        journal.transition(journal.revision.revision, state)
    assert cli.main(["project", "review", str(workspace)]) == 0

    readonly = tmp_path / "rough.fcpxml"
    readonly.write_text("locked\n", encoding="utf-8")
    readonly.chmod(0o444)

    assert cli.main(["project", "roughcut", str(workspace), "-o", str(readonly)]) == 2
    assert readonly.read_text(encoding="utf-8") == "locked\n"  # 덮어쓰지 않는다

    directory = tmp_path / "as-directory"
    directory.mkdir()
    assert cli.main(["project", "roughcut", str(workspace), "-o", str(directory)]) == 2
    readonly.chmod(0o644)


def test_grant_roots_keep_their_exact_characters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """루트 글자는 그대로 살린다 — 끝 공백·유니코드 경로도 변형 없이 지문과
    커버리지를 계산한다(공백 정리가 정당한 경로를 부수는 회귀를 잡는다)."""
    from vlogsrt.project import commands as project_commands

    # Windows는 이름 끝의 보통 공백을 지운다 — 줄바꿈 없는 공백(U+00A0)은 지키고 `strip`도 지운다.
    trailing = tmp_path / (
        "media root trailing " if os.name == "posix" else "media root trailing\u00a0"
    )
    trailing.mkdir()
    unicode_root = tmp_path / "미디어 루트 한글"
    unicode_root.mkdir()
    roots = [str(trailing), str(unicode_root)]
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "media-grant.json").write_text(_grant_document(roots), encoding="utf-8")

    assert project_commands._staged_app_grant_revision(workspace) is not None

    # 원문 그대로의 지문 재료 — strip된 값과는 다른 지문이 나와야 한다.
    fingerprint = project_commands._swift_grant_revision(roots)
    stripped = project_commands._swift_grant_revision([root.strip() for root in roots])
    assert fingerprint != stripped

    # 커버리지도 원문 경로로 잰다 — 그 안의 미디어는 통과한다.
    media = trailing / "clip.mov"
    media.write_bytes(b"synthetic")
    project_commands._require_staged_grant_coverage((_resolved(media, has_audio=True),), workspace)


def test_probe_failure_diagnostics_never_echo_carried_role_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """ffprobe 실패 문면에 운반된 역할 값이 섞여 있어도 사용자 진단에는
    나가지 않는다 — 준비 경로의 마지막 문이 지운다(S2A-2)."""
    stt_key = "synthetic-carried-stt-key"
    vision_key = "synthetic-carried-vision-key"
    _apply_v2_roles(stt=stt_key, vision=vision_key)
    workspace = _staged_workspace(tmp_path)
    (workspace / "media-grant.json").write_text(_grant_document([str(tmp_path)]), encoding="utf-8")

    def echoing_probe(source: Path) -> int:
        raise RuntimeError(f"ffprobe 실패 (코드 1)\n{stt_key} {vision_key} synthetic-carried-tail")

    from vlogsrt.project import topology as topology_module

    monkeypatch.setattr(topology_module, "probe_audio_stream_count", echoing_probe)

    assert cli.main(["project", "inspect", str(workspace)]) == 2
    captured = capsys.readouterr().err
    assert "ffprobe" in captured  # 진단은 산다 — 지움이 진단을 죽이지 않는다
    assert stt_key not in captured
    assert vision_key not in captured
    assert "synthetic-carried-tail" in captured  # 키가 아닌 부분은 그대로
    assert not (workspace / "analysis-estimate.jsonl").is_file()


def test_sanitize_cli_failure_closes_with_a_fixed_message_when_redaction_breaks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """지우개가 무너지면 원문 대신 정해진 글로 닫는다 — 원문 폭기 금지."""
    from vlogsrt.project import commands as project_commands

    with monkeypatch.context() as patch:
        patch.setattr(
            project_commands,
            "_redact_failure_text",
            lambda args, text: (_ for _ in ()).throw(RuntimeError("지움 붕괴")),
        )
        sanitized = project_commands._sanitize_cli_failure(
            argparse.Namespace(project_config=None), "원문에 synthetic-key가 있을지도"
        )
    assert sanitized == "실패 진단을 정리하지 못했습니다 — 명령을 다시 실행해 주세요"
