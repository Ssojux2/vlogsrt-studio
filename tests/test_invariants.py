"""CLAUDE.md 불변 규칙을 코드로 못 박는다.

여기가 깨지면 설계가 무너진 것이다. 테스트를 고치지 말고 보고하라.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from vlogsrt import pipeline, refine
from vlogsrt.config import Config
from vlogsrt.hangul import strip_for_compare
from vlogsrt.models import Cue, Segment

SYNTHETIC = Path(__file__).parent / "fixtures" / "synthetic"
ROOT = Path(__file__).resolve().parent.parent
VIDEO = Path("golden/demo.mp4")


@pytest.fixture(autouse=True)
def synthetic_fixtures(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VLOGSRT_FIXTURE_DIR", str(SYNTHETIC))


def test_draft_cue_times_come_straight_from_the_spine(tmp_path: Path) -> None:
    """규칙 1 — 정제 전 큐의 시각은 세그먼트 원본값과 정확히 같다."""
    result = pipeline.run(VIDEO, Config(), tmp_path, force_offline=True)
    draft = pipeline.spine_to_cues(list(result.trace.spine))
    assert [(cue.start, cue.end) for cue in draft] == [
        (segment.start, segment.end) for segment in result.trace.spine
    ]


def test_refined_cue_times_stay_within_refine_bounds(tmp_path: Path) -> None:
    """규칙 3 — refine의 이동량은 lead_in/lead_out 한도 안이다."""
    cfg = Config()
    result = pipeline.run(VIDEO, cfg, tmp_path, force_offline=True)
    segments = {segment.id: segment for segment in result.trace.spine}
    epsilon = 1e-6
    for cue in result.cues:
        assert cue.seg_ids, f"큐 {cue.index}가 seg_ids 없이 나왔다 — 역추적 불가(규칙 2)"
        first = segments[cue.seg_ids[0]]
        last = segments[cue.seg_ids[-1]]
        assert cue.start >= first.start - cfg.lead_in - epsilon
        assert cue.end <= last.end + cfg.lead_out + cfg.min_duration + epsilon


def test_srt_text_is_exactly_the_spine_text(tmp_path: Path) -> None:
    """규칙 1 — 출력 글자 전부가 세그먼트에서 오고, 한 글자도 새지 않는다."""
    result = pipeline.run(VIDEO, Config(), tmp_path, force_offline=True)
    body = strip_for_compare(" ".join(cue.text for cue in result.cues))
    spine_text = strip_for_compare(" ".join(s.text for s in result.trace.spine))
    assert body == spine_text


def test_every_segment_reaches_the_srt(tmp_path: Path) -> None:
    """규칙 9 — 골격의 세그먼트는 하나도 빠짐없이 어떤 큐엔가 들어간다."""
    result = pipeline.run(VIDEO, Config(), tmp_path, force_offline=True)
    covered = {seg_id for cue in result.cues for seg_id in cue.seg_ids}
    assert covered == {segment.id for segment in result.trace.spine}


def test_every_word_the_model_said_reaches_the_srt(tmp_path: Path) -> None:
    """규칙 9 — 어절은 하나도 버리지 않는다. 배분에서도, 정제에서도(D24)."""
    from vlogsrt import transcribe as transcribe_module

    responses = transcribe_module.FixtureTranscriber(fixture_dir=SYNTHETIC).transcribe(
        transcribe_module.TranscriptionJob(stem="demo", chunks=()), Config()
    )
    said = [
        word
        for response in sorted(responses, key=lambda r: r.offset)
        for word in transcribe_module.parse_text(response.payload).split()
    ]
    result = pipeline.run(VIDEO, Config(), tmp_path, force_offline=True)
    written = " ".join(cue.text for cue in result.cues).split()
    # 부호를 뺀 뒤 비교한다 — 조판 정리가 문장부호를 지우기 때문이다(D30).
    # **어절을 하나도 잃지 않는다는 보장은 그대로다.** 부호까지 같아야 한다는 계약만 놓았다.
    assert [strip_for_compare(word) for word in written] == [
        strip_for_compare(word) for word in said
    ]


def test_the_first_and_last_seconds_survive(tmp_path: Path) -> None:
    """**초반·마지막이 날아간 사고의 회귀 테스트**(D25).

    0.0초에서 시작하는 세그먼트와 영상 끝에서 끝나는 세그먼트가 둘 다 자막으로 선다.
    구간을 골라내는 코드가 다시 생기면 여기서 걸린다(규칙 4).
    """
    cfg = Config()
    result = pipeline.run(VIDEO, cfg, tmp_path, force_offline=True)
    assert result.cues[0].start <= cfg.lead_in + 1e-6
    assert result.cues[-1].end >= result.trace.duration - cfg.lead_out - 1e-6


def test_cues_never_overlap_and_never_exceed_capacity(tmp_path: Path) -> None:
    """0.77초에 900자 같은 큐 부류의 봉쇄. 이제 이걸 잡는 검사 단계는 없다 —
    정제가 구조적으로 보장하고, 그 보장을 여기서 못 박는다."""
    cfg = Config()
    result = pipeline.run(VIDEO, cfg, tmp_path, force_offline=True)
    for cue in result.cues:
        assert cue.end > cue.start
        assert cue.text.strip()
        assert len(cue.lines) <= cfg.max_lines
        if len(cue.text) > cfg.max_chars_per_cue:
            assert cue.flags, f"큐 {cue.index}가 표시 없이 용량을 넘었다"
    for previous, following in zip(result.cues, result.cues[1:], strict=False):
        assert following.start >= previous.end


def test_the_real_episode_comes_out_without_a_single_period() -> None:
    """D30 — 영화 자막 관행. 실측 회차(1479 세그먼트)에 온점이 하나도 남지 않는다."""
    spine, duration = _real_episode()
    cues = refine.refine(pipeline.spine_to_cues(spine), Config(), duration)
    assert len(cues) > 1000, "표본이 이만큼은 돼야 의미가 있다"
    assert [cue.text for cue in cues if "." in cue.text] == []


def test_polishing_the_real_episode_deletes_marks_and_nothing_else() -> None:
    """D30의 안전선 — **글자는 한 자도 못 지운다.**

    실측 회차 전체를 정리한 결과가 정리를 끈 결과와 부호를 뺀 뒤 완전히 같아야 한다.
    여기가 깨지면 정리 계층이 발화를 먹고 있는 것이다(규칙 9).
    """
    spine, duration = _real_episode()
    polished = refine.refine(pipeline.spine_to_cues(spine), Config(), duration)
    plain = refine.refine(
        pipeline.spine_to_cues(spine), replace(Config(), polish_enabled=False), duration
    )
    assert strip_for_compare(" ".join(cue.text for cue in polished)) == strip_for_compare(
        " ".join(cue.text for cue in plain)
    )


def _real_episode() -> tuple[list[Segment], float]:
    """실제 회차의 1차 전사 골격과 오디오 길이. **읽기만 한다.**

    길이는 반드시 메타 픽스처에서 온다. 응답 안의 `duration`은 **조각 하나의** 길이라
    (77분 회차의 조각이 38분이다) 그걸 쓰면 뒤쪽 절반이 영상 끝으로 눌려버린다.
    """
    from vlogsrt import transcribe

    fixtures = Path(__file__).parent / "fixtures" / "api"
    spine = transcribe.build_spine(
        transcribe.load_fixture_responses(fixtures / "fr_day_1_2_fhd.whisper.json")
    )
    return spine, float(transcribe.load_meta(fixtures, "fr_day_1_2_fhd")["duration"])


def test_spine_is_immutable() -> None:
    segment = Segment(id=0, start=0.0, end=1.0, text="가")
    with pytest.raises(FrozenInstanceError):
        segment.text = "나"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        Cue(start=0.0, end=1.0, lines=("가",)).start = 2.0  # type: ignore[misc]


def test_offline_run_makes_no_network_call(tmp_path: Path) -> None:
    """conftest가 소켓을 막아둔다. 여기까지 오면 픽스처만 쓴 것이다."""
    result = pipeline.run(VIDEO, Config(), tmp_path, force_offline=True)
    assert result.cues


def test_the_layers_that_edited_subtitles_stay_deleted() -> None:
    """규칙 5 — 판정으로 자막을 고치거나 빼던 계층은 되살리지 않는다.

    `verify.py`는 예외다 — 표시만 하고 출력을 못 건드린다. 그 선은
    `check_invariants.py`가 따로 지킨다.
    """
    package = ROOT / "vlogsrt"
    for name in ("loops.py", "validate.py", "report.py", "mapping.py", "gapfill.py"):
        assert not (package / name).exists(), f"{name}이 부활했다"


def test_verification_cannot_change_the_srt(tmp_path: Path) -> None:
    """규칙 5 — 검증을 켜든 끄든 SRT 바이트가 같다."""
    plain = pipeline.run(
        VIDEO, Config(), tmp_path / "plain", force_offline=True, verify_with_second_model=False
    )
    checked = pipeline.run(VIDEO, Config(), tmp_path / "checked", force_offline=True)
    assert checked.verification is not None  # 기본으로 켜져 있다
    assert plain.outputs["srt"].read_bytes() == checked.outputs["srt"].read_bytes()


def test_a_failed_verification_still_leaves_the_srt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """검증 픽스처가 없어도(= 두 번째 모델이 죽어도) 자막은 나온다."""
    lonely = tmp_path / "no-gpt"
    lonely.mkdir()
    for name in ("demo.whisper.json", "demo.meta.json"):
        (lonely / name).write_bytes((SYNTHETIC / name).read_bytes())
    monkeypatch.setenv("VLOGSRT_FIXTURE_DIR", str(lonely))

    result = pipeline.run(VIDEO, Config(), tmp_path / "out", force_offline=True)
    assert result.outputs["srt"].is_file()
    assert result.verification is None  # 건너뛰었지만 자막은 나왔다


def test_invariant_script_passes() -> None:
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_invariants.py")],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_only_transcribe_imports_openai() -> None:
    offenders = [
        path.name
        for path in (ROOT / "vlogsrt").glob("*.py")
        if path.name != "transcribe.py" and "import openai" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


# ── P0-02: 원본/파생 계약 (docs/PROJECT_CONTRACTS.md) ────────────────────────
#
# 검사기 자체를 여기서 시험한다. 진짜 소스 트리를 더럽히지 않기 위해 tmp_path에
# 가짜 트리를 만들고 검사기의 경로 전역을 그쪽으로 돌린다. 예외는 전부 **정확 경로**로
# 걸어야 한다 — 파일 이름만 보면 `vlogsrt/project/transcribe.py`가 openai를 물려받는다.

_CHECKER_SPEC = importlib.util.spec_from_file_location(
    "check_invariants", ROOT / "scripts" / "check_invariants.py"
)
check_invariants = importlib.util.module_from_spec(_CHECKER_SPEC)
assert _CHECKER_SPEC.loader is not None
_CHECKER_SPEC.loader.exec_module(check_invariants)


def _fake_source_tree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, files: dict[str, str]
) -> None:
    """검사기가 `tmp_path` 아래 가짜 트리를 보게 한다. 진짜 트리는 건드리지 않는다."""
    for rel, body in files.items():
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    monkeypatch.setattr(check_invariants, "ROOT", tmp_path)
    monkeypatch.setattr(check_invariants, "PACKAGE", tmp_path / "vlogsrt")
    monkeypatch.setattr(check_invariants, "UI_DIR", tmp_path / "ui")
    monkeypatch.setattr(check_invariants, "PROJECT_PACKAGE", tmp_path / "vlogsrt" / "project")
    monkeypatch.setattr(
        check_invariants,
        "XML_VALIDATION_MODULE",
        tmp_path / "vlogsrt" / "project" / "xml_validation.py",
    )


def _xml_boundary_source(run: str, extra: str = "") -> str:
    """xmllint 경계 모듈의 시뮬라. `run`은 subprocess.run(...) 호출 인자 덩어리."""
    return (
        "import shutil\n"
        "import subprocess\n"
        "\n"
        "\n"
        "def validate(path: str) -> bool:\n"
        '    executable = shutil.which("xmllint")\n'
        f"{extra}"
        "    completed = subprocess.run(\n"
        f"        {run},\n"
        "        capture_output=True,\n"
        "        text=True,\n"
        "        check=False,\n"
        "    )\n"
        "    return completed.returncode == 0\n"
    )


APPROVED_XMLLINT_RUN = '[executable, "--noout", path]'


@pytest.mark.parametrize(
    "statement",
    [
        "import vlogsrt.project",
        "import vlogsrt.project.vad as vad",
        "from vlogsrt.project import config",
        "from vlogsrt import project",
    ],
)
def test_legacy_absolute_project_import_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, statement: str
) -> None:
    _fake_source_tree(monkeypatch, tmp_path, {"vlogsrt/engine.py": statement + "\n"})
    failures = check_invariants.check_source_does_not_import_project()
    assert len(failures) == 1
    assert "vlogsrt/engine.py" in failures[0]


@pytest.mark.parametrize("statement", ["from .project import config", "from . import project"])
def test_legacy_relative_project_import_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, statement: str
) -> None:
    _fake_source_tree(monkeypatch, tmp_path, {"vlogsrt/engine.py": statement + "\n"})
    assert check_invariants.check_source_does_not_import_project()


def test_the_cli_entrypoint_is_the_sole_legacy_exception(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """조립 진입점 하나만 예외다 — 문서화된 정확 경로 `vlogsrt/cli.py`."""
    _fake_source_tree(monkeypatch, tmp_path, {"vlogsrt/cli.py": "from vlogsrt import project\n"})
    assert check_invariants.check_source_does_not_import_project() == []


@pytest.mark.parametrize(
    "module",
    [
        "transcribe",
        "preprocess",
        "repair",
        "repetition",
        "refine",
        "output",
        "layout",
        "polish",
        "pipeline",
        "models",
        "config",
        "env",
        "log",
        "cutpoints",
        "hangul",
        "keywords",
        "evaluate",
        "verify",
        "__init__",
    ],
)
def test_no_other_legacy_module_or_ui_may_import_project(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, module: str
) -> None:
    _fake_source_tree(
        monkeypatch,
        tmp_path,
        {
            f"vlogsrt/{module}.py": "from vlogsrt import project\n",
            "ui/page.py": "import vlogsrt.project\n",
        },
    )
    failures = check_invariants.check_source_does_not_import_project()
    assert len(failures) == 2
    assert any(f"vlogsrt/{module}.py" in failure for failure in failures)
    assert any("ui/page.py" in failure for failure in failures)


@pytest.mark.parametrize(
    "statement",
    [
        "import numpy",
        "import numpy as np",
        "import numpy.typing",
        "from numpy import ndarray",
        "import onnxruntime",
        "import onnxruntime as ort",
    ],
)
def test_optional_dependencies_fail_in_legacy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, statement: str
) -> None:
    _fake_source_tree(
        monkeypatch,
        tmp_path,
        {"vlogsrt/gadget.py": statement + "\n", "ui/board.py": statement + "\n"},
    )
    failures = check_invariants.check_project_dependency_scope()
    assert len(failures) == 2
    assert any("vlogsrt/gadget.py" in failure for failure in failures)
    assert any("ui/board.py" in failure for failure in failures)


def test_optional_dependencies_pass_under_project(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fake_source_tree(
        monkeypatch,
        tmp_path,
        {
            "vlogsrt/project/vad.py": (
                "import numpy as np\n"
                "import numpy.typing\n"
                "from numpy import ndarray\n"
                "import onnxruntime as ort\n"
            )
        },
    )
    assert check_invariants.check_project_dependency_scope() == []
    assert check_invariants.check_banned_imports() == []


def test_other_banned_names_still_fail_everywhere_in_the_package(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """numpy 완화가 다른 금지 이름을 느슨하게 만들면 안 된다."""
    _fake_source_tree(
        monkeypatch,
        tmp_path,
        {
            "vlogsrt/a.py": "import jamo\n",
            "vlogsrt/project/b.py": "import konlpy\nfrom pydantic import BaseModel\n",
        },
    )
    assert len(check_invariants.check_banned_imports()) == 3


@pytest.mark.parametrize(
    "body",
    [
        "import subprocess\n",
        "import subprocess as sp\nsp.run(['ls'])\n",
        "import subprocess as sp\nsp.Popen(['ls'])\n",
        "import subprocess as sp\nsp.check_call(['ls'])\n",
        "import subprocess as sp\nsp.check_output(['ls'])\n",
        "from subprocess import run\nrun(['ls'])\n",
        "from subprocess import Popen as P\nP(['ls'])\n",
        "from subprocess import check_output\ncheck_output(['ls'])\n",
    ],
)
def test_subprocess_aliases_fail_outside_the_two_boundaries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, body: str
) -> None:
    _fake_source_tree(monkeypatch, tmp_path, {"vlogsrt/gadget.py": body})
    assert check_invariants.check_subprocess_boundaries()


def test_transcribe_keeps_the_openai_monopoly_by_exact_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`vlogsrt/project/transcribe.py`는 이름만 같다고 openai를 물려받지 못한다."""
    _fake_source_tree(
        monkeypatch,
        tmp_path,
        {
            "vlogsrt/transcribe.py": "import openai\n",
            "vlogsrt/project/transcribe.py": "import openai\n",
        },
    )
    failures = check_invariants.check_openai_boundary()
    assert len(failures) == 1
    assert "vlogsrt/project/transcribe.py" in failures[0]


def test_preprocess_keeps_the_subprocess_monopoly_by_exact_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`vlogsrt/project/preprocess.py`는 이름만 같다고 subprocess를 물려받지 못한다."""
    _fake_source_tree(
        monkeypatch,
        tmp_path,
        {
            "vlogsrt/preprocess.py": "import subprocess\nsubprocess.run(['ffmpeg', '-i'])\n",
            "vlogsrt/project/preprocess.py": "import subprocess\nsubprocess.run(['ffmpeg'])\n",
        },
    )
    failures = check_invariants.check_subprocess_boundaries()
    assert len(failures) == 1
    assert "vlogsrt/project/preprocess.py" in failures[0]


def test_model_literals_live_only_in_the_exact_config_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """모델 리터럴은 `vlogsrt/config.py`와 `vlogsrt/project/config.py`에만 있다."""
    _fake_source_tree(
        monkeypatch,
        tmp_path,
        {
            "vlogsrt/config.py": 'MODEL = "whisper-1"\n',
            "vlogsrt/project/config.py": 'MODEL = "whisper-1"\n',
            "vlogsrt/project/tuning/config.py": 'MODEL = "whisper-1"\n',
            "vlogsrt/gadget.py": 'name = "gpt-4o-transcribe"\n',
        },
    )
    failures = check_invariants.check_no_hardcoded_model()
    assert len(failures) == 2
    assert any("vlogsrt/project/tuning/config.py" in failure for failure in failures)
    assert any("vlogsrt/gadget.py" in failure for failure in failures)


def test_xmllint_boundary_accepts_the_approved_shape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fake_source_tree(
        monkeypatch,
        tmp_path,
        {"vlogsrt/project/xml_validation.py": _xml_boundary_source(APPROVED_XMLLINT_RUN)},
    )
    assert check_invariants.check_subprocess_boundaries() == []


def test_xmllint_boundary_accepts_an_aliased_subprocess_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """경계 안에서 별명 자체는 죄가 아니다 — 형태가 맞으면 통과한다."""
    body = (
        "import shutil\n"
        "import subprocess as sp\n"
        "\n"
        "\n"
        "def validate(path: str) -> bool:\n"
        '    executable = shutil.which("xmllint")\n'
        "    completed = sp.run(\n"
        f"        {APPROVED_XMLLINT_RUN},\n"
        "        capture_output=True,\n"
        "        text=True,\n"
        "        check=False,\n"
        "    )\n"
        "    return completed.returncode == 0\n"
    )
    _fake_source_tree(monkeypatch, tmp_path, {"vlogsrt/project/xml_validation.py": body})
    assert check_invariants.check_subprocess_boundaries() == []


@pytest.mark.parametrize(
    "run",
    [
        '[executable, "--noout", path], shell=True',
        '["ls", "-l", path]',
        '["/bin/sh", "-c", "xmllint --noout"]',
        '["xmllint", "--noout", path]',
        '[executable, "--noout" + path]',
        '[executable, "--noout", f"{path}"]',
        '"xmllint --noout " + path',
    ],
)
def test_xmllint_boundary_rejects_wrong_shapes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, run: str
) -> None:
    _fake_source_tree(
        monkeypatch, tmp_path, {"vlogsrt/project/xml_validation.py": _xml_boundary_source(run)}
    )
    failures = check_invariants.check_subprocess_boundaries()
    assert len(failures) == 1
    assert "xml_validation.py" in failures[0]


@pytest.mark.parametrize(
    ("extra", "run", "expected"),
    [
        # which("curl")은 두 번 잡힌다 — 잘못된 which 대상과 비인가 실행파일 각각.
        ('    bad = shutil.which("curl")\n', '[bad, "--noout", path]', 2),
        ('    bad = "ffmpeg"\n', '[bad, "--noout", path]', 1),
    ],
)
def test_xmllint_boundary_rejects_other_executables(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, extra: str, run: str, expected: int
) -> None:
    _fake_source_tree(
        monkeypatch,
        tmp_path,
        {"vlogsrt/project/xml_validation.py": _xml_boundary_source(run, extra=extra)},
    )
    failures = check_invariants.check_subprocess_boundaries()
    assert len(failures) == expected
    assert all("xml_validation.py" in failure for failure in failures)


def test_xmllint_boundary_rejects_star_imports(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """별표 import는 호출 추적을 무력화한다 — 경계 안에서도 금지다."""
    body = (
        "import shutil\n"
        "from subprocess import *\n"
        "\n"
        "\n"
        "def validate(path: str) -> bool:\n"
        '    executable = shutil.which("xmllint")\n'
        "    completed = run(\n"
        f"        {APPROVED_XMLLINT_RUN},\n"
        "        capture_output=True,\n"
        "        text=True,\n"
        "        check=False,\n"
        "    )\n"
        "    return completed.returncode == 0\n"
    )
    _fake_source_tree(monkeypatch, tmp_path, {"vlogsrt/project/xml_validation.py": body})
    assert check_invariants.check_subprocess_boundaries()


def test_missing_xml_boundary_neither_crashes_nor_blinds_the_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """경계 파일이 없어도 터지지 않고, 다른 위반은 그대로 잡는다."""
    _fake_source_tree(monkeypatch, tmp_path, {"vlogsrt/gadget.py": "import subprocess\n"})
    assert check_invariants.check_subprocess_boundaries()


def test_a_clean_fake_tree_passes_every_contract_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fake_source_tree(
        monkeypatch,
        tmp_path,
        {
            "vlogsrt/cli.py": "from vlogsrt import project\n",
            "vlogsrt/transcribe.py": "import openai\n",
            "vlogsrt/preprocess.py": "import subprocess\nsubprocess.run(['ffmpeg'])\n",
            "vlogsrt/config.py": 'MODEL = "whisper-1"\n',
            "vlogsrt/project/vad.py": "import numpy as np\n",
        },
    )
    assert check_invariants.check_source_does_not_import_project() == []
    assert check_invariants.check_project_dependency_scope() == []
    assert check_invariants.check_subprocess_boundaries() == []
    assert check_invariants.check_openai_boundary() == []
    assert check_invariants.check_banned_imports() == []


def test_project_may_compose_legacy_public_boundaries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """방향은 project → 레거시 공개 경계뿐이다. 그 반대는 위의 테스트들이 막는다."""
    _fake_source_tree(
        monkeypatch,
        tmp_path,
        {
            "vlogsrt/project/adapter.py": (
                "from vlogsrt import models, transcribe\nfrom vlogsrt.config import Config\n"
            )
        },
    )
    assert check_invariants.check_source_does_not_import_project() == []
    assert check_invariants.check_project_dependency_scope() == []


def test_source_partition_separates_legacy_from_project(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fake_source_tree(
        monkeypatch,
        tmp_path,
        {"vlogsrt/a.py": "", "vlogsrt/project/b.py": "", "vlogsrt/project/deep/c.py": ""},
    )
    legacy = {check_invariants._rel(p) for p in check_invariants._legacy_sources()}
    project = {check_invariants._rel(p) for p in check_invariants._project_sources()}
    assert legacy == {"vlogsrt/a.py"}
    assert project == {"vlogsrt/project/b.py", "vlogsrt/project/deep/c.py"}


def test_ui_belongs_to_the_legacy_side(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _fake_source_tree(
        monkeypatch, tmp_path, {"vlogsrt/a.py": "", "ui/app.py": "", "ui/deep/page.py": ""}
    )
    legacy = {check_invariants._rel(p) for p in check_invariants._legacy_sources()}
    assert legacy == {"vlogsrt/a.py", "ui/app.py", "ui/deep/page.py"}


def test_the_real_tree_passes_the_new_contract_checks() -> None:
    """진짜 트리에서 새 검사 셋은 조용히 통과해야 한다 — 검증 on/off와 무관하게."""
    assert check_invariants.check_source_does_not_import_project() == []
    assert check_invariants.check_project_dependency_scope() == []
    assert check_invariants.check_subprocess_boundaries() == []


def test_project_sources_stay_below_the_project_package() -> None:
    assert all(
        check_invariants._rel(path).startswith("vlogsrt/project/")
        for path in check_invariants._project_sources()
    )
