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
from vlogsrt.layout import FORCED_BREAK, TOO_LONG
from vlogsrt.models import RETEXT_OFF, Cue, RetextRecord, Segment

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


def test_every_over_width_line_is_flagged_on_the_real_episode() -> None:
    """S12·규칙 9 — 폭을 넘긴 줄은 조용히 넘어가지 않는다. 실측 회차 전체로 못 박는다.

    16자는 목표 폭이다. 어절 경계가 안 맞아 넘는 줄은 그대로 두되, 반드시
    `too_long`·`forced_break` 표시를 달고 나가야 한다.
    """
    cfg = Config()
    spine, duration = _real_episode()
    cues = refine.refine(pipeline.spine_to_cues(spine), cfg, duration)
    over_width = [
        (cue.index, len(line))
        for cue in cues
        for line in cue.lines
        if len(line) > cfg.max_chars_per_line
    ]
    # 폭 초과 줄이 아예 없으면 이 검사는 아무것도 안 본다(D32의 교훈).
    assert over_width, "실측 회차에서 폭 초과 줄이 사라졌다 — 표본을 다시 확인하라"
    for cue in cues:
        if any(len(line) > cfg.max_chars_per_line for line in cue.lines):
            assert {TOO_LONG, FORCED_BREAK} & set(cue.flags), (
                f"큐 {cue.index}의 폭 초과 줄이 표시 없이 나갔다"
            )


def test_make_cues_warns_once_counting_lines_not_cues(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """S12·규칙 9 — 그래도 폭을 넘긴 줄이 남으면 `WARN` 한 줄로 센다. 큐 수가 아니라 줄 수다."""
    monkeypatch.setenv("VLOGSRT_LOG_LEVEL", "WARN")
    cfg = Config()
    blob = (Segment(id=0, start=0.0, end=1.5, text="가" * 20 + " " + "나" * 19),)
    cues = pipeline._make_cues(blob, cfg, 2.5, RetextRecord(status=RETEXT_OFF))
    err = capsys.readouterr().err
    assert len(cues) == 1  # 짧아서 못 쪼갠 큐 한 장 — 줄은 둘 다 폭을 넘는다
    assert err.count("WARN") == 1
    assert "넘는 줄 2줄이" in err

    calm = (Segment(id=0, start=0.0, end=2.0, text="짧은 자막"),)
    pipeline._make_cues(calm, cfg, 3.0, RetextRecord(status=RETEXT_OFF))
    assert "WARN" not in capsys.readouterr().err


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
    monkeypatch.setattr(
        check_invariants,
        "EDIT_MODULE_PATH",
        tmp_path / "vlogsrt" / "project" / "edit.py",
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


APPROVED_XMLLINT_RUN = '[executable, "--nonet", "--noout", path]'


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
    """조립 진입점 하나만 예외다 — 정확 경로 `vlogsrt/cli.py`, 대상은 project.commands."""
    _fake_source_tree(
        monkeypatch, tmp_path, {"vlogsrt/cli.py": "from vlogsrt.project import commands\n"}
    )
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


def test_optional_dependencies_pass_under_the_vad_package(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fake_source_tree(
        monkeypatch,
        tmp_path,
        {
            "vlogsrt/project/vad/model.py": (
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
    """모델 리터럴은 config 경로 셋에만 있다 — 2026-10-02 v2가 raw config를 더했다."""
    _fake_source_tree(
        monkeypatch,
        tmp_path,
        {
            "vlogsrt/config.py": 'MODEL = "whisper-1"\n',
            "vlogsrt/project/config.py": 'MODEL = "whisper-1"\n',
            "vlogsrt/project/raw/config.py": 'MODEL = "whisper-1"\n',
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
        '[executable, "--nonet", "--noout", path], shell=True',
        '["ls", "--nonet", "--noout", path]',
        '["/bin/sh", "--nonet", "--noout", path]',
        '["xmllint", "--nonet", "--noout", path]',
        '[executable, "--nonet", "--noout", path + ".xml"]',
        '[executable, "--nonet", "--noout", f"{path}"]',
        '"xmllint --nonet --noout " + path',
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
        ('    bad = shutil.which("curl")\n', '[bad, "--nonet", "--noout", path]', 2),
        ('    bad = "ffmpeg"\n', '[bad, "--nonet", "--noout", path]', 1),
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


# ── P0-02 수정(Astra 재검토): xmllint 경계의 우회와 오답 ─────────────────────
#
# 재검토가 재현한 우회 넷(A·B·C·E)과 오답 하나(D)를 소스 문자열로 그대로 세운다.
# subprocess는 실행하지 않는다 — 파서와 경계 검사만 돈다. 진짜 트리에
# vlogsrt/project/ 도 만들지 않는다.

_XML_BOUNDARY_REL = "vlogsrt/project/xml_validation.py"


def _boundary_case(imports: str, setup: str, call: str) -> str:
    """경계 모듈의 시뮬라. `setup`은 실행파일 확보 줄, `call`은 subprocess 호출."""
    return (
        f"{imports}"
        "import subprocess\n"
        "\n"
        "\n"
        "def validate(path: str) -> bool:\n"
        f"    {setup}\n"
        f"    {call}\n"
        "    return True\n"
    )


@pytest.mark.parametrize(
    ("imports", "setup", "call", "expected"),
    [
        pytest.param(
            "import shutil\n",
            'exe = shutil.which("xmllint")',
            'completed = subprocess.run([exe, "--nonet", "--noout", path], executable="/bin/sh")',
            1,
            id="executable-override",
        ),
        pytest.param(
            "import shutil\n",
            'exe = shutil.which("xmllint")',
            'completed = subprocess.run([exe, "--nonet", "--noout", path], **{"shell": True})',
            1,
            id="dict-expansion-shell",
        ),
        pytest.param(
            "",
            'exe = unrelated.which("xmllint")',
            'completed = subprocess.run([exe, "--nonet", "--noout", path], capture_output=True)',
            2,
            id="foreign-which",
        ),
        pytest.param(
            "import shutil\n",
            'exe = shutil.which("xmllint")',
            'process = subprocess.Popen([exe, "--nonet", "--noout", path], -1, "/bin/sh")',
            1,
            id="popen-positional-executable",
        ),
    ],
)
def test_xmllint_boundary_rejects_the_reviewed_bypasses(
    imports: str, setup: str, call: str, expected: int
) -> None:
    """재검토가 재현한 우회 넷 — 어느 하나도 조용히 지나가면 안 된다."""
    body = _boundary_case(imports, setup, call)
    failures = check_invariants._xmllint_boundary_failures(_XML_BOUNDARY_REL, body)
    assert len(failures) == expected, failures
    assert all(_XML_BOUNDARY_REL in failure for failure in failures)


def test_xmllint_boundary_accepts_the_aliased_which_import() -> None:
    """재검토 사례 D — `from shutil import which as …`은 정당한 별명이다."""
    body = _boundary_case(
        "from shutil import which as find_executable\n",
        'exe = find_executable("xmllint")',
        'completed = subprocess.run([exe, "--nonet", "--noout", path], capture_output=True)',
    )
    assert check_invariants._xmllint_boundary_failures(_XML_BOUNDARY_REL, body) == []


def test_xmllint_boundary_keeps_passing_the_aliased_import_shapes() -> None:
    """승인 형태 보존 — 모듈 별명·from-import 별명·리터럴 False·동적 경로."""
    body = _boundary_case(
        "import shutil as sh\nfrom subprocess import run as launch\n",
        'exe = sh.which("xmllint")',
        'completed = launch([exe, "--nonet", "--noout", path], shell=False, capture_output=True)',
    )
    assert check_invariants._xmllint_boundary_failures(_XML_BOUNDARY_REL, body) == []


def test_xmllint_boundary_rejects_a_shadowed_which_binding() -> None:
    """import를 덮어쓴 `which`는 출처 불명이다 — 보수적으로 거부한다."""
    body = _boundary_case(
        "from shutil import which\n",
        'which = str  # import를 덮어썼다\n    exe = which("xmllint")',
        'completed = subprocess.run([exe, "--nonet", "--noout", path], capture_output=True)',
    )
    failures = check_invariants._xmllint_boundary_failures(_XML_BOUNDARY_REL, body)
    assert len(failures) == 2, failures


def test_xmllint_boundary_rejects_shutil_star_imports() -> None:
    """`from shutil import *`는 which 출처 추적을 무력화한다."""
    body = _boundary_case(
        "from shutil import *\n",
        'exe = which("xmllint")',
        'completed = subprocess.run([exe, "--nonet", "--noout", path], capture_output=True)',
    )
    failures = check_invariants._xmllint_boundary_failures(_XML_BOUNDARY_REL, body)
    assert len(failures) == 3, failures


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
            "vlogsrt/cli.py": "from vlogsrt.project import commands\n",
            "vlogsrt/transcribe.py": "import openai\n",
            "vlogsrt/preprocess.py": "import subprocess\nsubprocess.run(['ffmpeg'])\n",
            "vlogsrt/config.py": 'MODEL = "whisper-1"\n',
            "vlogsrt/project/config.py": 'VISION_MODEL = "gemini-3.8-flash"\n',
            "vlogsrt/project/vad/model.py": "import numpy as np\n",
        },
    )
    assert check_invariants.check_source_does_not_import_project() == []
    assert check_invariants.check_project_dependency_scope() == []
    assert check_invariants.check_subprocess_boundaries() == []
    assert check_invariants.check_openai_boundary() == []
    assert check_invariants.check_banned_imports() == []
    assert check_invariants.check_no_hardcoded_model() == []


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


# ── P0-02b(Astra 재조정): 비준된 원본/파생 계약의 다섯 가지 좁히기 ────────────
#
# ① 선택 extra는 `vlogsrt/project/vad/` 아래로, ② CLI 예외는 project.commands로,
# ③ glm-*·gemini-* 모델 리터럴 가드(정확히 프로젝트 config 예외), ④ project models.py의
# frozen, ⑤ XML 경계의 subprocess API를 run 하나·argv 두 형태로 얼린다.
# 전부 파서만 돈다 — 실행하지 않는다.


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
def test_optional_dependencies_pass_only_under_the_vad_package(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, statement: str
) -> None:
    _fake_source_tree(
        monkeypatch,
        tmp_path,
        {
            "vlogsrt/project/vad/model.py": statement + "\n",
            "vlogsrt/project/vad/silero/loader.py": statement + "\n",
        },
    )
    assert check_invariants.check_banned_imports() == []
    assert check_invariants.check_project_dependency_scope() == []


@pytest.mark.parametrize(
    "statement", ["import numpy as np", "import onnxruntime", "import onnxruntime as ort"]
)
@pytest.mark.parametrize(
    "rel",
    [
        "vlogsrt/project/vision.py",
        "vlogsrt/project/vision/policy.py",
        "vlogsrt/project/vad.py",
        "vlogsrt/project/score/frames.py",
        "vlogsrt/gadget.py",
    ],
)
def test_optional_dependencies_fail_outside_the_vad_package(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, rel: str, statement: str
) -> None:
    """vad 디렉터리 아래가 유일한 허용 지점이다 — 비전·바깥·이름 사칭은 거부한다.

    두 검사가 함께 본다(P0-02c): `check_banned_imports`는 extra를 금지 목록으로
    다루고 `check_project_dependency_scope`은 허용 지점을 vad/ 아래로 못 박는다.
    어느 한쪽이라도 project 쪽 파일을 못 보면 `project/vision/policy.py`의
    onnxruntime가 조용히 지나간다 — 그게 이번 회귀다. ui/**는 legacy 목록에만
    있으므로 scope 검사가 맡는다(위 `test_optional_dependencies_fail_in_legacy`).
    """
    _fake_source_tree(monkeypatch, tmp_path, {rel: statement + "\n"})
    banned = check_invariants.check_banned_imports()
    assert len(banned) == 1, banned
    assert rel in banned[0]
    scope = check_invariants.check_project_dependency_scope()
    assert len(scope) == 1, scope
    assert rel in scope[0]


@pytest.mark.parametrize(
    "statement",
    [
        "import vlogsrt.project.commands",
        "import vlogsrt.project.commands as project_commands",
        "from vlogsrt.project import commands",
        "from vlogsrt.project import commands as project_commands",
        "from vlogsrt.project.commands import build_parser",
        "from .project import commands",
        "from .project.commands import build_parser",
    ],
)
def test_the_cli_entrypoint_may_compose_only_project_commands(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, statement: str
) -> None:
    """절대·상대 형태 전부 정확히 project.commands 모듈로 귀결할 때만 통과한다."""
    _fake_source_tree(monkeypatch, tmp_path, {"vlogsrt/cli.py": statement + "\n"})
    assert check_invariants.check_source_does_not_import_project() == []


@pytest.mark.parametrize(
    "statement",
    [
        "import vlogsrt.project",
        "import vlogsrt.project.config",
        "import vlogsrt.project.commands.parser",
        "from vlogsrt import project",
        "from vlogsrt import project, config",
        "from vlogsrt.project import config",
        "from vlogsrt.project import commands, config",
        "from vlogsrt.project.config import ProjectConfig",
        "from vlogsrt.project.commands.parser import build_parser",
        "from . import project",
        "from .project import config",
        "from .project.config import ProjectConfig",
        "from .project.commands.parser import build_parser",
    ],
)
def test_the_cli_entrypoint_rejects_every_other_project_import(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, statement: str
) -> None:
    """CLI 예외는 commands 모듈 하나다 — 하위 모듈(commands.parser)도 거부한다."""
    _fake_source_tree(monkeypatch, tmp_path, {"vlogsrt/cli.py": statement + "\n"})
    failures = check_invariants.check_source_does_not_import_project()
    assert len(failures) == 1, failures
    assert "vlogsrt/cli.py" in failures[0]


def test_the_commands_exception_stays_confined_to_the_cli(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """다른 레거시 모듈은 commands조차 못 가져온다 — 예외가 이동하지 않는다."""
    _fake_source_tree(
        monkeypatch,
        tmp_path,
        {
            "vlogsrt/engine.py": "from vlogsrt.project import commands\n",
            "ui/page.py": "from vlogsrt.project import commands\n",
        },
    )
    failures = check_invariants.check_source_does_not_import_project()
    assert len(failures) == 2
    assert any("vlogsrt/engine.py" in failure for failure in failures)
    assert any("ui/page.py" in failure for failure in failures)


# ── A0(2026-10-02 v2 계획 §3.2): 브라우저 작업공간 서비스의 정확 경로 예외 ──────
#
# `ui/workspace_service.py` 한 파일이 **정확히 `vlogsrt.project.raw.service` 모듈**을
# 가져올 수 있다. CLI 예외와 같은 모양의 난간이다 — 하위 모듈·패키지 핸들은
# 거부하고, ui/는 vlogsrt 패키지 안이 아니라서 상대 형태는 애초에
# vlogsrt.project로 귀결하지도 않는다(절대 형태만 인정).


@pytest.mark.parametrize(
    "statement",
    [
        "import vlogsrt.project.raw.service",
        "import vlogsrt.project.raw.service as raw_service",
        "from vlogsrt.project.raw.service import RawWorkspaceService",
        "from vlogsrt.project.raw import service",
        "from vlogsrt.project.raw import service as raw_service",
    ],
)
def test_the_workspace_service_may_compose_only_raw_service(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, statement: str
) -> None:
    """절대 형태가 정확히 project.raw.service 모듈로 귀결할 때만 통과한다."""
    _fake_source_tree(monkeypatch, tmp_path, {"ui/workspace_service.py": statement + "\n"})
    assert check_invariants.check_source_does_not_import_project() == []


@pytest.mark.parametrize(
    "statement",
    [
        # 패키지 핸들과 이웃 모듈
        "import vlogsrt.project",
        "import vlogsrt.project.raw",
        "import vlogsrt.project.commands",
        "from vlogsrt import project",
        "from vlogsrt.project import raw",
        "from vlogsrt.project import service",
        "from vlogsrt.project.raw import models",
        "from vlogsrt.project.raw import service, models",
        "from vlogsrt.project.commands import build_parser",
        # 하위 모듈(descendant) — 예외는 모듈 자체까지만이다
        "import vlogsrt.project.raw.service.models",
        "from vlogsrt.project.raw.service.models import WorkspaceModels",
        # 상대 형태 — ui/ 안에서 `.project`는 vlogsrt.project가 아니다
        "from .project.raw.service import RawWorkspaceService",
        "from .project import raw",
        "from . import project",
        "from ..project.raw import service",
    ],
)
def test_the_workspace_service_rejects_every_other_project_import(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, statement: str
) -> None:
    """UI 예외는 raw.service 모듈 하나다 — 하위·이웃·상대 형태는 전부 거부한다."""
    _fake_source_tree(monkeypatch, tmp_path, {"ui/workspace_service.py": statement + "\n"})
    failures = check_invariants.check_source_does_not_import_project()
    assert len(failures) == 1, failures
    assert "ui/workspace_service.py" in failures[0]


def test_the_workspace_service_exception_does_not_cover_sibling_imports(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """허용된 import 옆의 다른 project import는 그대로 걸린다 — 예외는 문장 단위다."""
    _fake_source_tree(
        monkeypatch,
        tmp_path,
        {
            "ui/workspace_service.py": (
                "import vlogsrt.project.raw.service\nfrom vlogsrt.project import config\n"
            )
        },
    )
    failures = check_invariants.check_source_does_not_import_project()
    assert len(failures) == 1, failures
    assert "ui/workspace_service.py" in failures[0]


def test_the_raw_service_exception_stays_confined_to_workspace_service(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """다른 ui 파일·레거시·CLI는 raw.service조차 못 가져온다 — 예외가 이동하지 않는다."""
    _fake_source_tree(
        monkeypatch,
        tmp_path,
        {
            "ui/app.py": "import vlogsrt.project.raw.service\n",
            "ui/service.py": "from vlogsrt.project.raw import service\n",
            "ui/components/player.py": "import vlogsrt.project.raw.service\n",
            "vlogsrt/engine.py": "import vlogsrt.project.raw.service\n",
            "vlogsrt/cli.py": "import vlogsrt.project.raw.service\n",
        },
    )
    failures = check_invariants.check_source_does_not_import_project()
    assert len(failures) == 5, failures
    for rel in (
        "ui/app.py",
        "ui/service.py",
        "ui/components/player.py",
        "vlogsrt/engine.py",
        "vlogsrt/cli.py",
    ):
        assert any(f"{rel}:" in failure for failure in failures)


@pytest.mark.parametrize(
    "rel",
    [
        "ui/raw_service.py",
        "ui/workspace.py",
        "ui/deep/workspace_service.py",
        "vlogsrt/workspace_service.py",
    ],
)
def test_the_workspace_service_exception_is_an_exact_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, rel: str
) -> None:
    """`ui/workspace_service.py`만 진입점이다 — 비슷한 이름·중첩 경로는 물려받지 못한다."""
    _fake_source_tree(monkeypatch, tmp_path, {rel: "import vlogsrt.project.raw.service\n"})
    failures = check_invariants.check_source_does_not_import_project()
    assert len(failures) == 1, failures
    assert failures[0].startswith(f"{rel}:")


def test_the_cli_exception_does_not_cover_raw_service(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """CLI 예외는 commands뿐이다 — raw.service는 CLI에서도 거부한다."""
    _fake_source_tree(
        monkeypatch, tmp_path, {"vlogsrt/cli.py": "from vlogsrt.project.raw import service\n"}
    )
    failures = check_invariants.check_source_does_not_import_project()
    assert len(failures) == 1, failures
    assert "vlogsrt/cli.py" in failures[0]
    assert "vlogsrt.project.raw" in failures[0]


def test_glm_literals_pass_inside_the_exact_config_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fake_source_tree(
        monkeypatch,
        tmp_path,
        {
            "vlogsrt/config.py": 'MODEL = "whisper-1"\n',
            "vlogsrt/project/config.py": 'VISION_MODEL = "gemini-3.8-flash"\n',
        },
    )
    assert check_invariants.check_no_hardcoded_model() == []


_VISION_LITERAL_PATHS = (
    "vlogsrt/config.py",
    "vlogsrt/gadget.py",
    "vlogsrt/project/vision.py",
    "vlogsrt/project/tuning/config.py",
    "vlogsrt/project/raw/config.py",
)


@pytest.mark.parametrize(
    ("rel", "literal"),
    [
        (rel, literal)
        for rel in _VISION_LITERAL_PATHS
        for literal in ("glm-5.3-flash", "gemini-3.8-flash")
        # Gemini 의미 분석 프로파일은 raw config에 산다 — 아래 시험이 따로 겨냥한다(D84)
        if (rel, literal) != ("vlogsrt/project/raw/config.py", "gemini-3.8-flash")
    ],
)
def test_vision_literals_fail_outside_the_exact_project_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, rel: str, literal: str
) -> None:
    """비전 모델 리터럴은 파생 계층의 것이다 — 원본·raw config는 물려받지 않는다.

    공급자를 glm에서 gemini로 바꿔도 난간은 좁아지지 않는다: 쓰지 않게 된
    접두사도 계속 걸린다. 안 그러면 되돌리기가 조용히 커밋된다.
    """
    _fake_source_tree(monkeypatch, tmp_path, {rel: f'MODEL = "{literal}"\n'})
    failures = check_invariants.check_no_hardcoded_model()
    assert len(failures) == 1, failures
    assert rel in failures[0]


def test_gemini_analysis_profiles_live_in_the_raw_config_but_glm_still_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Gemini는 이제 의미 분석 모델이다(W-30b, D84) — 등록표가 단가와 함께 raw config에 둔다.
    glm은 여전히 파생 계층 비전 전용이라 raw config에 들지 않는다."""
    _fake_source_tree(
        monkeypatch,
        tmp_path,
        {
            "vlogsrt/project/raw/config.py": 'FLASH = "gemini-3.8-flash"\nGLM = "glm-5.3-flash"\n',
            "vlogsrt/project/raw/understanding_luna.py": 'MODEL = "gemini-3.8-flash"\n',
        },
    )
    failures = check_invariants.check_no_hardcoded_model()
    assert len(failures) == 2, failures
    assert any("raw/config.py:2" in failure for failure in failures)
    assert any("understanding_luna.py" in failure for failure in failures)


# ── A0(2026-10-02 v2 계획 §3.2): raw 전용 모델 리터럴의 정확 경로 가드 ─────────


def test_raw_model_literals_pass_inside_the_exact_raw_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """문구·시간·의미 분석 역할의 리터럴 셋은 `vlogsrt/project/raw/config.py`에 산다."""
    _fake_source_tree(
        monkeypatch,
        tmp_path,
        {
            "vlogsrt/project/raw/config.py": (
                'TEXT_MODEL = "gpt-transcribe"\n'
                'TIMING_MODEL = "whisper-1"\n'
                'LUNA_MODEL = "gpt-6-luna"\n'
            )
        },
    )
    assert check_invariants.check_no_hardcoded_model() == []


@pytest.mark.parametrize(
    "rel",
    [
        "vlogsrt/config.py",
        "vlogsrt/project/config.py",
        "vlogsrt/gadget.py",
        "vlogsrt/project/raw/models.py",
        "vlogsrt/project/tuning/raw/config.py",
    ],
)
def test_luna_literals_fail_outside_the_exact_raw_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, rel: str
) -> None:
    """의미 분석 모델 리터럴은 다른 config도 물려받지 못한다 — 오직 raw config뿐."""
    _fake_source_tree(monkeypatch, tmp_path, {rel: 'MODEL = "gpt-6-luna"\n'})
    failures = check_invariants.check_no_hardcoded_model()
    assert len(failures) == 1, failures
    assert rel in failures[0]


def test_project_models_absence_stays_valid_until_p0_04(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fake_source_tree(monkeypatch, tmp_path, {"vlogsrt/a.py": ""})
    assert check_invariants.check_project_models_frozen() == []


def test_project_models_all_frozen_passes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """별명 장식자도 알아본다 — `@dc.dataclass`(모듈 별명)와 `@dc`(심볼 별명)."""
    _fake_source_tree(
        monkeypatch,
        tmp_path,
        {
            "vlogsrt/project/models.py": (
                "import dataclasses\n"
                "import dataclasses as dc\n"
                "from dataclasses import dataclass as datum\n"
                "\n"
                "\n"
                "@dataclasses.dataclass(frozen=True)\n"
                "class AssetRef:\n"
                "    path: str\n"
                "\n"
                "\n"
                "@dc.dataclass(frozen=True)\n"
                "class InstanceRef:\n"
                "    asset: AssetRef\n"
                "\n"
                "\n"
                "@datum(frozen=True, slots=True)\n"
                "class MarkerRef:\n"
                "    asset: AssetRef\n"
            )
        },
    )
    assert check_invariants.check_project_models_frozen() == []


def test_project_models_mutable_dataclasses_fail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`@dataclass` bare(인자 없음)도 가변이다 — 별명 형태도 frozen=True를 못 박는다."""
    _fake_source_tree(
        monkeypatch,
        tmp_path,
        {
            "vlogsrt/project/models.py": (
                "from dataclasses import dataclass\n"
                "import dataclasses as dc\n"
                "from dataclasses import dataclass as datum\n"
                "\n"
                "\n"
                "@dataclass\n"
                "class Mutable:\n"
                "    path: str\n"
                "\n"
                "\n"
                "@dc.dataclass(frozen=False)\n"
                "class AliasedMutable:\n"
                "    path: str\n"
                "\n"
                "\n"
                "@datum\n"
                "class BareAlias:\n"
                "    path: str\n"
            )
        },
    )
    failures = check_invariants.check_project_models_frozen()
    assert len(failures) == 3, failures
    assert all("vlogsrt/project/models.py" in failure for failure in failures)
    assert any("Mutable" in failure for failure in failures)
    assert any("AliasedMutable" in failure for failure in failures)
    assert any("BareAlias" in failure for failure in failures)


def test_project_models_shadowed_dataclass_alias_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """덮어쓰인 dataclass 별명은 출처 불명이다 — frozen 검사를 못 피하게 한다."""
    _fake_source_tree(
        monkeypatch,
        tmp_path,
        {
            "vlogsrt/project/models.py": (
                "from dataclasses import dataclass as datum\n"
                "\n"
                "\n"
                "datum = wrap  # import를 덮어썼다\n"
                "\n"
                "\n"
                "@datum(frozen=True)\n"
                "class Sneaky:\n"
                "    path: str\n"
            )
        },
    )
    failures = check_invariants.check_project_models_frozen()
    assert len(failures) == 1, failures
    assert "Sneaky" in failures[0]


@pytest.mark.parametrize(
    ("setup", "run"),
    [
        ('exe = shutil.which("xmllint")', '[exe, "--nonet", "--noout", path]'),
        ('exe = shutil.which("xmllint")', '[exe, "--nonet", "--noout", str(path)]'),
        (
            'exe = shutil.which("xmllint")\n    dtd_uri = bundle.dtd_uri',
            '[exe, "--nonet", "--noout", "--dtdvalid", dtd_uri, str(path)]',
        ),
    ],
)
def test_xmllint_boundary_accepts_the_two_ratified_argv_forms(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, setup: str, run: str
) -> None:
    """재검토가 얼린 형태 둘 — 마지막 칸(path·dtd_uri)만 동적 표현식을 쓴다."""
    call = f"completed = subprocess.run(\n        {run},\n        capture_output=True,\n    )"
    body = _boundary_case("import shutil\n", setup, call)
    _fake_source_tree(monkeypatch, tmp_path, {_XML_BOUNDARY_REL: body})
    assert check_invariants.check_subprocess_boundaries() == []


@pytest.mark.parametrize(
    "run",
    [
        '[exe, "--noout", path]',
        '[exe, "--nonet", path]',
        '[exe, "--noout", "--nonet", path]',
        '[exe, "--nonet", "--noout"]',
        '[exe, "--nonet", "--noout", "--loaddtds", path]',
        '[exe, "--nonet", "--noout", "--dtdvalid", path]',
        '[exe, "--nonet", "--noout", "--dtdvalidfpu", dtd_uri, path]',
        '[exe, "--nonet", "--noout", "--dtdvalid", dtd_uri, path, "--noout"]',
    ],
)
def test_xmllint_boundary_rejects_wrong_flag_shapes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, run: str
) -> None:
    """깃발 집합·순서가 정해진 둘과 다르면 거부한다 — 끼워넣기·빠짐·뒤집힘 전부."""
    call = f"completed = subprocess.run({run}, capture_output=True)"
    body = _boundary_case(
        "import shutil\n", 'exe = shutil.which("xmllint")\n    dtd_uri = str(bundle)', call
    )
    _fake_source_tree(monkeypatch, tmp_path, {_XML_BOUNDARY_REL: body})
    failures = check_invariants.check_subprocess_boundaries()
    assert len(failures) == 1, failures
    assert _XML_BOUNDARY_REL in failures[0]


@pytest.mark.parametrize(
    ("imports", "setup", "call"),
    [
        pytest.param(
            "import shutil\n",
            'exe = shutil.which("xmllint")',
            'process = subprocess.Popen([exe, "--nonet", "--noout", path], capture_output=True)',
            id="popen-module-attribute",
        ),
        pytest.param(
            "import shutil\n",
            'exe = shutil.which("xmllint")',
            'subprocess.check_call([exe, "--nonet", "--noout", path])',
            id="check-call",
        ),
        pytest.param(
            "import shutil\n",
            'exe = shutil.which("xmllint")',
            'subprocess.check_output([exe, "--nonet", "--noout", path])',
            id="check-output",
        ),
        pytest.param(
            "import shutil\n",
            'exe = shutil.which("xmllint")',
            'subprocess.call([exe, "--nonet", "--noout", path])',
            id="plain-call",
        ),
        pytest.param(
            "import shutil\nfrom subprocess import Popen\n",
            'exe = shutil.which("xmllint")',
            'process = Popen([exe, "--nonet", "--noout", path])',
            id="popen-from-import",
        ),
        pytest.param(
            "import shutil\nfrom vlogsrt.helpers import run\n",
            'exe = shutil.which("xmllint")',
            'completed = run([exe, "--nonet", "--noout", path])',
            id="run-from-unrelated-module",
        ),
        pytest.param(
            "import shutil\nfrom subprocess import run\n",
            'run = launch_via  # import를 덮어썼다\n    exe = shutil.which("xmllint")',
            'completed = run([exe, "--nonet", "--noout", path])',
            id="shadowed-run-alias",
        ),
        pytest.param(
            "import shutil\nimport subprocess as sp\n",
            'sp = helpers  # import를 덮어썼다\n    exe = shutil.which("xmllint")',
            'completed = sp.run([exe, "--nonet", "--noout", path])',
            id="shadowed-module-alias",
        ),
        pytest.param(
            "import shutil\n",
            'exe = shutil.which("xmllint")',
            'completed = helpers.run([exe, "--nonet", "--noout", path])',
            id="run-on-unrelated-object",
        ),
        pytest.param(
            "import shutil\n",
            'exe = shutil.which("xmllint")',
            'completed = subprocess.run([exe, "--nonet", "--noout", path], -1, "/bin/sh")',
            id="extra-positional-args",
        ),
        pytest.param(
            "import shutil\nfrom subprocess import run as launch\n",
            'exe = shutil.which("xmllint")',
            'completed = launch([exe, "--nonet", "--noout", path], shell=True)',
            id="aliased-run-shell-true",
        ),
        pytest.param(
            "import shutil\nfrom subprocess import run as launch\n",
            'exe = shutil.which("xmllint")',
            'completed = launch([exe, "--nonet", "--noout", path], **{"shell": True})',
            id="aliased-run-dict-expansion",
        ),
        pytest.param(
            "import shutil\nfrom subprocess import run as launch\n",
            'exe = shutil.which("xmllint")',
            'completed = launch([exe, "--nonet", "--noout", path], executable="/bin/sh")',
            id="aliased-run-executable-override",
        ),
        pytest.param(
            "import shutil\nfrom subprocess import run as launch\n",
            'exe = shutil.which("xmllint")',
            'completed = launch([exe, "--noout", path])',
            id="aliased-run-wrong-flags",
        ),
        pytest.param(
            "import shutil\nfrom subprocess import run as launch\n",
            'exe = shutil.which("xmllint")',
            'completed = launch([exe, "--nonet", path])',
            id="aliased-run-missing-flag",
        ),
        pytest.param(
            "import shutil\nfrom subprocess import run as launch\n",
            'exe = shutil.which("xmllint")',
            'completed = launch([exe, "--noout", "--nonet", path])',
            id="aliased-run-reordered-flags",
        ),
        pytest.param(
            "import shutil\nfrom subprocess import run as launch\n",
            'exe = shutil.which("xmllint")',
            'completed = launch([exe, "--nonet", "--noout", path], -1, "/bin/sh")',
            id="aliased-run-extra-positional-args",
        ),
        pytest.param(
            "import shutil\nfrom subprocess import Popen as pop\n",
            'exe = shutil.which("xmllint")',
            'process = pop([exe, "--nonet", "--noout", path])',
            id="aliased-popen-from-import",
        ),
        pytest.param(
            "import shutil\nfrom subprocess import call as invoke\n",
            'exe = shutil.which("xmllint")',
            'invoke([exe, "--nonet", "--noout", path])',
            id="aliased-plain-call",
        ),
        pytest.param(
            "import shutil\nfrom subprocess import check_call as checked\n",
            'exe = shutil.which("xmllint")',
            'checked([exe, "--nonet", "--noout", path])',
            id="aliased-check-call",
        ),
        pytest.param(
            "import shutil\nfrom subprocess import check_output as capture\n",
            'exe = shutil.which("xmllint")',
            'out = capture([exe, "--nonet", "--noout", path])',
            id="aliased-check-output",
        ),
        pytest.param(
            "import shutil\nfrom subprocess import run as launch\n",
            'launch = invoke  # import를 덮어썼다\n    exe = shutil.which("xmllint")',
            'completed = launch([exe, "--nonet", "--noout", path])',
            id="shadowed-aliased-run",
        ),
    ],
)
def test_xmllint_boundary_freezes_the_api_to_subprocess_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, imports: str, setup: str, call: str
) -> None:
    """run 외 메서드·출처 불명 run·남는 위치 인자는 전부 거부한다."""
    body = _boundary_case(imports, setup, call)
    _fake_source_tree(monkeypatch, tmp_path, {_XML_BOUNDARY_REL: body})
    failures = check_invariants.check_subprocess_boundaries()
    assert len(failures) == 1, failures
    assert _XML_BOUNDARY_REL in failures[0]


@pytest.mark.parametrize(
    ("setup", "run"),
    [
        pytest.param(
            'exe = shutil.which("xmllint")',
            '[exe, "--nonet", "--noout", path]',
            id="four-slot-shell-omitted",
        ),
        pytest.param(
            'exe = shutil.which("xmllint")',
            '[exe, "--nonet", "--noout", str(path)]',
            id="four-slot-dynamic-path",
        ),
        pytest.param(
            'exe = shutil.which("xmllint")\n    dtd_uri = bundle.dtd_uri',
            '[exe, "--nonet", "--noout", "--dtdvalid", dtd_uri, str(path)]',
            id="six-slot-dynamic-dtd-and-path",
        ),
    ],
)
def test_xmllint_boundary_validates_the_aliased_run_like_subprocess_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, setup: str, run: str
) -> None:
    """P0-02c의 양면 — 별명 run은 몰래 건너뛰는 것도, 통째로 거부하는 것도 아니다.

    `subprocess.run`과 같은 검사를 통과해야 한다: 두 argv 형태, 동적 마지막 칸,
    생략된 shell. 형태가 틀리면 위의 `freezes_the_api` 매개변수들이 잡는다.
    """
    call = f"completed = launch(\n        {run},\n        capture_output=True,\n    )"
    body = _boundary_case("import shutil\nfrom subprocess import run as launch\n", setup, call)
    _fake_source_tree(monkeypatch, tmp_path, {_XML_BOUNDARY_REL: body})
    assert check_invariants.check_subprocess_boundaries() == []


def test_a_cli_basename_spoof_gets_no_exception(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`ui/cli.py`는 진입점이 아니다 — 예외는 정확 경로 `vlogsrt/cli.py`만 물려준다."""
    _fake_source_tree(
        monkeypatch,
        tmp_path,
        {"ui/cli.py": "from vlogsrt.project import commands\n"},
    )
    failures = check_invariants.check_source_does_not_import_project()
    assert len(failures) == 1, failures
    assert "ui/cli.py" in failures[0]


# ── P6-01(규칙 4·9): 제거 계층은 오디오를 듣지 않는다 ──────────────────────────
#
# edit.py의 import 방향 검사기 자체를 시험한다. 유일한 예외는 FrameSampleRequest
# 데이터 타입 한 형태다 — **타입은 이음매를 건너도 되고 행동은 안 된다**(코디네이터
# 재결). 허용목록이 조용히 넓어지는 것을 이 회귀 셋이 막는다(P5-06의 교훈).

_EDIT_REL = "vlogsrt/project/edit.py"


def test_edit_layer_accepts_the_sanctioned_sample_type_import(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """정당한 형태 — sampling에서 FrameSampleRequest 하나만, policy·guard 타입은 자유."""
    _fake_source_tree(
        monkeypatch,
        tmp_path,
        {
            _EDIT_REL: (
                "from vlogsrt.project.vision.policy import VisionCandidate\n"
                "from vlogsrt.project.vision.sampling import FrameSampleRequest\n"
                "from vlogsrt.project.vad.guard import InstanceGuard\n"
            )
        },
    )
    assert check_invariants.check_edit_layer_ignores_audio() == []


def test_edit_layer_accepts_an_aliased_sample_type_import(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """가져오는 이름이 정확히 하나면 별명 자체는 죄가 아니다 — 대상이 그 타입일 때만."""
    _fake_source_tree(
        monkeypatch,
        tmp_path,
        {_EDIT_REL: "from vlogsrt.project.vision.sampling import FrameSampleRequest as Sample\n"},
    )
    assert check_invariants.check_edit_layer_ignores_audio() == []


@pytest.mark.parametrize(
    "statement",
    [
        "from vlogsrt.project.vision.sampling import sample_frames\n",
        "from vlogsrt.project.vision.sampling import establish_correspondence\n",
        "from vlogsrt.project.vision.sampling import FrameSampleRequest, sample_frames\n",
        "from vlogsrt.project.vision.sampling import FrameSample, FrameSampleRequest\n",
        "from vlogsrt.project.vision.sampling import *\n",
    ],
)
def test_edit_layer_rejects_every_other_name_from_sampling(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, statement: str
) -> None:
    """데이터 타입 외의 이름 — 추출·프로브 행동이 이음매를 건너는 순간이다."""
    _fake_source_tree(monkeypatch, tmp_path, {_EDIT_REL: statement})
    failures = check_invariants.check_edit_layer_ignores_audio()
    assert len(failures) == 1, failures
    assert _EDIT_REL in failures[0]
    assert "FrameSampleRequest" in failures[0]


@pytest.mark.parametrize(
    "statement",
    [
        "import vlogsrt.project.vision.sampling\n",
        "import vlogsrt.project.vision.sampling as sampling\n",
        # 별명 우회 — asname이 허용 형태의 이름처럼 보여도 `ast.Import` 행은
        # 모듈 경로가 곧 가져온 이름이다. 통과하면 허용목록이 뚫린다(P6-01b 항목 3).
        "import vlogsrt.project.vision.sampling as FrameSampleRequest\n",
        "from vlogsrt.project.vision import sampling\n",
        "from vlogsrt.project.vision import sampling as frames\n",
        "from .vision.sampling import FrameSampleRequest, sample_frames\n",
        "from .vision import sampling\n",
    ],
)
def test_edit_layer_rejects_the_sampling_module_handle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, statement: str
) -> None:
    """모듈 핸들을 쥐면 그 안의 전부에 닿는다 — 통째로 드는 형태는 금지다."""
    _fake_source_tree(monkeypatch, tmp_path, {_EDIT_REL: statement})
    failures = check_invariants.check_edit_layer_ignores_audio()
    assert len(failures) == 1, failures
    assert _EDIT_REL in failures[0]


@pytest.mark.parametrize(
    "statement",
    [
        "import subprocess\n",
        "import openai\n",
        "import numpy\n",
        "import onnxruntime\n",
        "import vlogsrt.transcribe\n",
        "from vlogsrt.transcribe import request_vision\n",
        "from vlogsrt.preprocess import probe_video_stream\n",
        "from vlogsrt.project.pcm import PcmReference\n",
        "from vlogsrt.project.speech import SourceTrace\n",
        "from vlogsrt.project.vad.inference import VadResult\n",
        "from vlogsrt.project.vad.runtime import load_vad_model\n",
        "from vlogsrt.repetition import find_repeats\n",
        "from vlogsrt.repair import settle\n",
        "from vlogsrt.refine import refine\n",
        "from vlogsrt.output import save_srt\n",
        "from vlogsrt.layout import layout\n",
        # 상대 형태 — edit.py는 vlogsrt/project/ 안에 산다
        "from .pcm import PcmReference\n",
        "from .speech import SourceTrace\n",
        "from .vad.inference import VadResult\n",
        "from ..transcribe import request_vision\n",
        # 모듈 핸들 우회 — 경로는 부모지만 묶는 이름이 금지 모듈이다
        "from vlogsrt.project import pcm\n",
        "from vlogsrt import transcribe\n",
        # 하위 모듈 경로도 금지 대상의 접두사로 잡는다
        "from vlogsrt.project.vad.inference.windows import something\n",
    ],
)
def test_edit_layer_rejects_every_banned_module_in_any_form(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, statement: str
) -> None:
    """금지 목록의 모듈은 절대·상대·별명·하위 경로 어떤 형태로도 못 들어온다."""
    _fake_source_tree(monkeypatch, tmp_path, {_EDIT_REL: statement})
    failures = check_invariants.check_edit_layer_ignores_audio()
    assert len(failures) >= 1, failures
    assert all(_EDIT_REL in failure for failure in failures)


def test_edit_layer_tolerates_a_missing_module(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """edit.py가 없는 동안은 통과다 — P6-01이 만들 때 선이 먼저 서 있다."""
    _fake_source_tree(monkeypatch, tmp_path, {"vlogsrt/gadget.py": "import subprocess\n"})
    assert check_invariants.check_edit_layer_ignores_audio() == []


def test_the_real_edit_module_passes_the_removal_layer_check() -> None:
    """진짜 edit.py는 이 검사를 통과한다 — 허용목록이 실제 코드와 함께 산다."""
    assert check_invariants.check_edit_layer_ignores_audio() == []


# ── D51: 3단 등급은 순수 계층이다 ─────────────────────────────────────────


@pytest.mark.parametrize(
    "statement",
    [
        "import subprocess",
        "import openai",
        "from vlogsrt.project.raw import service",
        "from vlogsrt.project.raw.service import RawWorkspaceService",
        "from vlogsrt.project.raw.review_workspace import ReviewWorkspaceFacade",
        "from vlogsrt.project.raw import understanding_luna",
        "from vlogsrt import preprocess",
        "BODY = Path('x').read_text()",
        "with open('x') as handle:\n    pass",
        # D60 — 시각 확인 값은 서비스가 값으로 넘긴다. 등급이 확인 계층·원본 자막을 알면 안 된다.
        "from vlogsrt.project import sync_check",
        "from vlogsrt.project.sync_check import LEVEL_RED",
        "from vlogsrt.project.raw import sync_report",
        "import vlogsrt.project.raw.sync_report as reports",
        "from vlogsrt.project.raw import subtitle_suspects",
        "from vlogsrt.project.raw.source_transcript import load",
        # 글로 이름을 넘겨 들이는 길(20a 검증 뒤 고침).
        "import importlib",
        "MODULE = __import__('vlogsrt.project.sync_check')",
        "exec('from vlogsrt.project import sync_check')",
    ],
)
def test_tiering_is_pure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, statement: str) -> None:
    clean = (
        "from fractions import Fraction\n"
        "from vlogsrt.project.raw.analysis_config import TIER_SETTINGS\n"
        "from vlogsrt.project.raw.suitability import rational\n"
    )
    target = "vlogsrt/project/raw/tiering.py"
    _fake_source_tree(monkeypatch, tmp_path, {target: clean})
    assert check_invariants.check_tiering_stays_pure() == []
    (tmp_path / target).write_text(clean + statement + "\n", encoding="utf-8")
    assert check_invariants.check_tiering_stays_pure()
    (tmp_path / target).unlink()
    assert check_invariants.check_tiering_stays_pure() == [
        "vlogsrt/project/raw/tiering.py: 3단 등급 계층이 사라졌다 (D51)"
    ]


def test_real_tiering_module_passes_the_purity_check() -> None:
    assert check_invariants.check_tiering_stays_pure() == []


# ── D60: 등급의 시각 확인 값은 서비스 한 자리만 ─────────────────────────────

TIER_SERVICE = "vlogsrt/project/raw/service.py"
CLEAN_TIER_SERVICE = (
    "from vlogsrt.project.raw import tiering\n"
    "\n"
    "\n"
    "class RawWorkspaceService:\n"
    "    def _tier_report_for_row(self, workspace, row):\n"
    "        if row:\n"
    "            return tiering.classify({}, timing=self._tier_timing(workspace, row))\n"
    "        return tiering.classify({'p': 1}, timing=self._tier_timing(workspace, row))\n"
    "\n"
    "    def _tier_timing(self, workspace, asset_id):\n"
    "        if asset_id:\n"
    "            return tiering.TimingSignals(level='red')\n"
    "        return tiering.UNKNOWN_TIMING\n"
)
CLEAN_TIERING = (
    "class TimingSignals:\n    pass\n\n\nUNKNOWN_TIMING = TimingSignals()\n\n\n"
    "def classify(document, *, timing=UNKNOWN_TIMING):\n    return document\n"
)


@pytest.mark.parametrize(
    ("rel", "body", "line", "fragment"),
    [
        # 서비스의 다른 메서드가 부른다 — 값과 함께여도 자리가 틀렸다.
        (
            TIER_SERVICE,
            CLEAN_TIER_SERVICE + "\n    def tier_report(self, ws, row):\n"
            "        return tiering.classify({}, timing=self._tier_timing(ws, row))\n",
            16,
            "tiering.classify를 RawWorkspaceService.tier_report에서 부른다",
        ),
        # 그 자리에서 값을 빠뜨렸다.
        (
            TIER_SERVICE,
            CLEAN_TIER_SERVICE.replace(
                "tiering.classify({}, timing=self._tier_timing(workspace, row))",
                "tiering.classify({})",
            ),
            7,
            "timing=self._tier_timing(…)이 없다",
        ),
        # 그 자리에서 다른 값을 넘겼다(모름으로 덮어 신호를 버린다).
        (
            TIER_SERVICE,
            CLEAN_TIER_SERVICE.replace(
                "timing=self._tier_timing(workspace, row))\n        return",
                "timing=tiering.UNKNOWN_TIMING)\n        return",
            ),
            7,
            "timing=self._tier_timing(…)이 없다",
        ),
        # 그 자리 안에 둔 함수는 그 자리가 아니다.
        (
            TIER_SERVICE,
            CLEAN_TIER_SERVICE + "\n    def _other(self, ws, row):\n"
            "        def inner():\n"
            "            return tiering.classify({}, timing=self._tier_timing(ws, row))\n"
            "        return inner()\n",
            17,
            "RawWorkspaceService._other.inner에서 부른다",
        ),
        # 모듈 수준에서 값을 만든다.
        (
            TIER_SERVICE,
            CLEAN_TIER_SERVICE + "\nDEFAULT = tiering.TimingSignals(level='red')\n",
            15,
            "TimingSignals를 모듈 수준에서 가리킨다",
        ),
        # 다른 모듈이 등급을 직접 매긴다·값을 만든다·이름으로 가져온다.
        (
            "vlogsrt/project/raw/finalcut_export.py",
            "from vlogsrt.project.raw import tiering\n\n\n"
            "def report(document):\n    return tiering.classify(document)\n",
            5,
            "tiering.classify를 report에서 부른다",
        ),
        (
            "vlogsrt/project/raw/review_workspace.py",
            "from vlogsrt.project.raw.tiering import TimingSignals\n",
            1,
            "TimingSignals를 모듈 수준에서 가리킨다",
        ),
        (
            "ui/workspace_service.py",
            "from vlogsrt.project.raw.tiering import classify\n",
            1,
            "tiering.classify를 이름으로 가져온다",
        ),
        (
            "vlogsrt/project/raw/review_workspace.py",
            "from vlogsrt.project.raw import tiering\n\n\n"
            "def grade(report):\n    return tiering.UNKNOWN_TIMING\n",
            5,
            "UNKNOWN_TIMING를 grade에서 가리킨다",
        ),
        # 독립 검증이 찾은 빈틈(20a 검증 뒤 고침) — 별명·getattr 글·부르지 않고 꺼내기·값의 문 읽기.
        (
            TIER_SERVICE,
            CLEAN_TIER_SERVICE + "\n    def _removal_cuts(self, ws, row):\n"
            "        from vlogsrt.project.raw import tiering as _t\n"
            "        return _t.classify({})\n",
            17,
            "tiering.classify를 RawWorkspaceService._removal_cuts에서 부른다",
        ),
        (
            TIER_SERVICE,
            CLEAN_TIER_SERVICE + "\n    def other(self, doc):\n"
            "        return getattr(tiering, 'classify')(doc)\n",
            16,
            "등급 부르기를 글 'classify'로 가리킨다",
        ),
        (
            TIER_SERVICE,
            CLEAN_TIER_SERVICE + "\n    def other(self, doc):\n"
            "        fn = tiering.classify\n        return fn(doc)\n",
            16,
            "tiering.classify를 부르지 않고 꺼낸다",
        ),
        (
            TIER_SERVICE,
            CLEAN_TIER_SERVICE + "\n    def _removal_cuts(self, ws, row):\n"
            "        return self._tier_timing(ws, row)\n",
            16,
            "_tier_timing를 RawWorkspaceService._removal_cuts에서 읽는다",
        ),
        (
            TIER_SERVICE,
            CLEAN_TIER_SERVICE + "\n    def other(self, ws, row):\n"
            "        return getattr(self, '_tier_timing')(ws, row)\n",
            16,
            "등급 부르기를 글 '_tier_timing'로 가리킨다",
        ),
        (
            TIER_SERVICE,
            CLEAN_TIER_SERVICE + "\nMODULE = tiering\n",
            15,
            "등급 모듈을 다른 이름에 묶는다",
        ),
        (
            "vlogsrt/project/raw/review_workspace.py",
            "import importlib\n\n\ndef grade(doc):\n"
            "    module = importlib.import_module('vlogsrt.project.raw.tiering')\n"
            "    return module.classify(doc)\n",
            5,
            "등급 부르기를 글 'vlogsrt.project.raw.tiering'로 가리킨다",
        ),
        (
            "ui/workspace_service.py",
            "from vlogsrt.project.raw.tiering import *\n",
            1,
            "tiering.classify를 이름으로 가져온다",
        ),
        (
            "vlogsrt/project/raw/finalcut_export.py",
            "import vlogsrt.project.raw.tiering\n\n\n"
            "def report(doc):\n    return vlogsrt.project.raw.tiering.classify(doc)\n",
            5,
            "tiering.classify를 report에서 부른다",
        ),
        (
            "ui/workspace_service.py",
            "import vlogsrt.project.raw.tiering as tm\n\n\n"
            "def report(doc):\n    return tm.classify(doc, timing=None)\n",
            5,
            "tiering.classify를 report에서 부른다",
        ),
    ],
)
def test_tier_timing_reaches_tiering_only_through_the_service_entry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, rel: str, body: str, line: int, fragment: str
) -> None:
    clean = {TIER_SERVICE: CLEAN_TIER_SERVICE, "vlogsrt/project/raw/tiering.py": CLEAN_TIERING}
    _fake_source_tree(monkeypatch, tmp_path, clean)
    (tmp_path / "ui").mkdir(exist_ok=True)
    assert check_invariants.check_tier_timing_only_from_service() == []
    (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / rel).write_text(body, encoding="utf-8")
    failures = check_invariants.check_tier_timing_only_from_service()
    assert any(
        failure.startswith(f"{rel}:{line}: ") and fragment in failure for failure in failures
    ), failures
    assert all(failure.endswith("(D60)") for failure in failures)


def test_real_tree_passes_the_tier_timing_entry_check() -> None:
    assert check_invariants.check_tier_timing_only_from_service() == []


# ── W-25b: 시각 정답은 자막·등급·내보내기와 따로 ────────────────────────────


@pytest.mark.parametrize("reader", check_invariants.TIMING_TRUTH_FORBIDDEN_READERS)
@pytest.mark.parametrize(
    "statement",
    [
        "from vlogsrt.project.raw import timing_truth",
        "from vlogsrt.project.raw.timing_truth import marks",
        "import vlogsrt.project.raw.timing_truth as truth",
        "from . import timing_truth",
        "from vlogsrt.project.config import TIMING_TRUTH_SETTINGS",
        "from vlogsrt.project.config import TimingTruthSettings",
        "import importlib\nM = importlib.import_module('vlogsrt.project.raw.timing_truth')",
    ],
)
def test_truth_marks_stay_out_of_subtitles_tiers_approval_and_export(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, reader: str, statement: str
) -> None:
    clean = "from fractions import Fraction\n"
    files = {
        f"vlogsrt/project/{rel}": clean for rel in check_invariants.TIMING_TRUTH_FORBIDDEN_READERS
    }
    files["vlogsrt/project/raw/timing_truth.py"] = clean
    _fake_source_tree(monkeypatch, tmp_path, files)
    assert check_invariants.check_timing_truth_stays_apart() == []
    (tmp_path / "vlogsrt" / "project" / reader).write_text(clean + statement + "\n", "utf-8")
    (failure,) = check_invariants.check_timing_truth_stays_apart()
    assert failure.startswith(f"vlogsrt/project/{reader}: ") and failure.endswith("(D67)")


def test_the_truth_check_names_the_readers_and_notices_a_missing_module(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert set(check_invariants.TIMING_TRUTH_FORBIDDEN_READERS) >= {
        "raw/understanding.py",
        "raw/source_transcript.py",
        "raw/finalcut_export.py",
        "raw/finalcut_timeline.py",
        "raw/tiering.py",
    }
    _fake_source_tree(monkeypatch, tmp_path, {"vlogsrt/project/raw/tiering.py": ""})
    assert check_invariants.check_timing_truth_stays_apart() == [
        "vlogsrt/project/raw/timing_truth.py: 시각 정답 계층이 사라졌다 (D67)"
    ]


def test_real_tree_keeps_truth_marks_apart() -> None:
    assert check_invariants.check_timing_truth_stays_apart() == []


# ── D56: Final Cut 타임라인 빌더는 순수 계층이다 ──────────────────────────


@pytest.mark.parametrize(
    "statement",
    [
        "import subprocess",
        "import openai",
        "import os",
        "import shutil",
        "import sqlite3",
        "from vlogsrt import preprocess",
        "from vlogsrt.preprocess import probe_timecode",
        "from vlogsrt.project.raw import service",
        "from vlogsrt.project.raw.review_workspace import ReviewWorkspaceFacade",
        "from vlogsrt.project.raw import confirmed_package",
        "from vlogsrt.project.raw.confirmed_package import export_confirmed",
        "from vlogsrt.project import library",
        "from vlogsrt.project.raw import media_refs",
        "from vlogsrt.project import clips",
        "from vlogsrt.project.clips import FRAME_RATE_TAGS",
        "from vlogsrt.project.media import sha256_file",
        "BODY = Path('x').read_bytes()",
        "Path('x').write_bytes(b'')",
        "tree.write('x.fcpxml')",
        "FOUND = Path('x').exists()",
        "FACTS = Path('x').stat()",
        "with open('x') as handle:\n    pass",
    ],
)
@pytest.mark.parametrize("builder", ["finalcut_timeline.py", "finalcut_titles.py"])
def test_finalcut_timeline_is_pure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, statement: str, builder: str
) -> None:
    clean = (
        "import xml.etree.ElementTree as ET\n"
        "from fractions import Fraction\n"
        "from pathlib import Path\n"
        "from vlogsrt.project.fcp_tables import FRAME_RATE_TAGS\n"
        "from vlogsrt.project.raw.tiering import segment_keyword\n"
        "from vlogsrt.project.timebase import frame_nearest\n"
        "TEXT = 'a\\r\\n'.replace('\\r\\n', '\\n')\n"
        "URI = Path('/x').as_uri()\n"
    )
    builders = {
        f"vlogsrt/project/raw/{name}": clean for name in check_invariants.FINALCUT_PURE_BUILDERS
    }
    target = f"vlogsrt/project/raw/{builder}"
    _fake_source_tree(monkeypatch, tmp_path, builders)
    assert check_invariants.check_finalcut_timeline_stays_pure() == []
    (tmp_path / target).write_text(clean + statement + "\n", encoding="utf-8")
    failures = check_invariants.check_finalcut_timeline_stays_pure()
    assert failures and all(line.startswith(builder) for line in failures)
    (tmp_path / target).unlink()
    assert check_invariants.check_finalcut_timeline_stays_pure() == [
        f"{target}: Final Cut 빌더가 사라졌다 (D56·D90)"
    ]


def test_real_finalcut_timeline_module_passes_the_purity_check() -> None:
    assert check_invariants.check_finalcut_timeline_stays_pure() == []


# ── D56: Final Cut 보내기는 원본을 복사·재해시하지 않는다 ──────────────────


@pytest.mark.parametrize(
    "statement",
    [
        "import shutil",
        "import hashlib",
        "import subprocess",
        "from vlogsrt.project.raw import confirmed_package",
        "from vlogsrt.project.raw.confirmed_package import verify_confirmed_export",
        "from vlogsrt.project.raw import " + "final_cut" + "_handoff",  # 지운 옛 출력 계층
        "from vlogsrt.project.clips import probe_clips",
        "from vlogsrt.project.media import sha256_file",
        "DIGEST = review_workspace._sha256_file(path)",
        "FACTS = media_refs._facts(path)",
        "ASSET = service._verified_asset(workspace, asset_id)",
        "CLIPS = clips_module.probe_clips(paths)",
        "OUTCOME = facade.export_confirmed(package)",
        "os.replace(draft, final)",
        "os.rename(draft, final)",
        "draft.replace(final)",
        "draft.rename(final)",
    ],
)
def test_finalcut_export_never_copies_or_rehashes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, statement: str
) -> None:
    clean = (
        "import json\n"
        "import os\n"
        "from vlogsrt import preprocess\n"
        "from vlogsrt.project import library\n"
        "from vlogsrt.project.raw import finalcut_timeline, media_refs\n"
        "PATH = media_refs.resolve(workspace, asset, 'export')\n"
        "os.link(draft, final)\n"
    )
    target = "vlogsrt/project/raw/finalcut_export.py"
    _fake_source_tree(monkeypatch, tmp_path, {target: clean})
    assert check_invariants.check_finalcut_export_never_copies() == []
    (tmp_path / target).write_text(clean + statement + "\n", encoding="utf-8")
    assert check_invariants.check_finalcut_export_never_copies()
    (tmp_path / target).unlink()
    assert check_invariants.check_finalcut_export_never_copies() == [
        "vlogsrt/project/raw/finalcut_export.py: Final Cut 보내기 계층이 사라졌다 (D56)"
    ]


def test_real_finalcut_export_module_passes_the_no_copy_check() -> None:
    assert check_invariants.check_finalcut_export_never_copies() == []


# ── D56: 옛 「선택한 컷」 출력 길은 되살리지 않는다 ─────────────────────────
# 이름은 조각을 이어 만든다 — 저장소 전체 grep 인수(옛 이름 0건)가 이 시험 자신에 걸리지 않게.
_OLD_EXPORT = "export_" + "final_cut"
_OLD_MATERIALIZE = "materialize" + "_review"
_OLD_OPEN = "capcut" + "_open_view"
_OLD_MODULE = "final_cut" + "_handoff"


@pytest.mark.parametrize(
    ("relative", "body"),
    [
        ("vlogsrt/project/raw/service.py", f"def {_OLD_EXPORT}(self):\n    pass\n"),
        ("vlogsrt/project/raw/service.py", f"X = service.{_OLD_MATERIALIZE}(root)\n"),
        ("ui/workspace_service.py", f"def {_OLD_OPEN}(self):\n    pass\n"),
        (f"vlogsrt/project/raw/{_OLD_MODULE}.py", "X = 1\n"),
    ],
)
def test_old_selected_cuts_finalcut_path_stays_dead(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, relative: str, body: str
) -> None:
    clean = {
        "vlogsrt/project/raw/service.py": "def export_finalcut_timeline(self):\n    pass\n",
        "ui/workspace_service.py": "def finalcut_send_view(self):\n    pass\n",
    }
    _fake_source_tree(monkeypatch, tmp_path, clean)
    assert check_invariants.check_old_finalcut_path_stays_dead() == []
    target = tmp_path / relative
    target.write_text(body, encoding="utf-8")
    failures = check_invariants.check_old_finalcut_path_stays_dead()
    assert failures and all("D56" in failure for failure in failures)


def test_real_tree_has_no_old_finalcut_path() -> None:
    assert check_invariants.check_old_finalcut_path_stays_dead() == []


# ── D55: 자막 시각 확인은 순수하고, 표시만 한다 ─────────────────────────────────

SYNC_CLEAN = (
    "from fractions import Fraction\nfrom vlogsrt.project.config import SyncCheckSettings\n"
)
SYNC_REPORT_CLEAN = (
    "from vlogsrt import preprocess\n"
    "from vlogsrt.project import sync_check\n"
    "from vlogsrt.project.raw.source_transcript import load\n"
)


@pytest.mark.parametrize(
    "statement",
    [
        "import subprocess",
        "import numpy",
        "import onnxruntime",
        "from vlogsrt import preprocess",
        "from vlogsrt import output",
        "from vlogsrt import transcribe",
        "from vlogsrt.project.raw import service",
        "BODY = Path('x').read_text()",
        "with open('x') as handle:\n    pass",
    ],
)
def test_sync_check_is_pure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, statement: str
) -> None:
    target = "vlogsrt/project/sync_check.py"
    _fake_source_tree(monkeypatch, tmp_path, {target: SYNC_CLEAN})
    assert check_invariants.check_sync_check_stays_pure() == []
    (tmp_path / target).write_text(SYNC_CLEAN + statement + "\n", encoding="utf-8")
    assert check_invariants.check_sync_check_stays_pure()
    (tmp_path / target).unlink()
    assert check_invariants.check_sync_check_stays_pure() == [
        "vlogsrt/project/sync_check.py: 자막 시각 확인 계층이 사라졌다 (D55)"
    ]


@pytest.mark.parametrize(
    ("planted", "body"),
    [
        ("vlogsrt/project/raw/tiering.py", "from vlogsrt.project.raw import sync_report\n"),
        ("vlogsrt/project/raw/auto_cleanup.py", "from vlogsrt.project import sync_check\n"),
        ("vlogsrt/project/raw/finalcut_timeline.py", "from vlogsrt.project import sync_check\n"),
        ("vlogsrt/project/raw/finalcut_export.py", "from vlogsrt.project.raw import sync_report\n"),
        (
            "vlogsrt/project/raw/sync_report.py",
            SYNC_REPORT_CLEAN + "from vlogsrt.project.raw import finalcut_export\n",
        ),
        (
            "vlogsrt/project/raw/config.py",
            "class RawWorkflowConfig:\n    sync: SyncCheckSettings = SyncCheckSettings()\n",
        ),
        ("vlogsrt/project/raw/understanding.py", "KEY = SYNC_CHECK_SETTINGS\n"),
        (
            "vlogsrt/project/raw/sync_report.py",
            SYNC_REPORT_CLEAN + "from vlogsrt.project.raw.source_transcript import seed\n",
        ),
        (
            "vlogsrt/project/raw/sync_report.py",
            SYNC_REPORT_CLEAN + "SQL = 'INSERT INTO source_transcripts VALUES (?)'\n",
        ),
        ("vlogsrt/project/raw/sync_report.py", SYNC_REPORT_CLEAN + "from vlogsrt import output\n"),
        # 검증자 재검(D55): 모듈 객체를 들여와 `.edit()`을 부르는 길 · 소문자 SQL
        (
            "vlogsrt/project/raw/sync_report.py",
            SYNC_REPORT_CLEAN
            + "from vlogsrt.project.raw import source_transcript\n"
            + "source_transcript.edit(None, 'a', 'r', action='edit', utterance_id='u')\n",
        ),
        (
            "vlogsrt/project/raw/sync_report.py",
            SYNC_REPORT_CLEAN + "import vlogsrt.project.raw.source_transcript\n",
        ),
        (
            "vlogsrt/project/raw/sync_report.py",
            SYNC_REPORT_CLEAN + "SQL = 'insert into source_transcripts values (?)'\n",
        ),
        (
            "vlogsrt/project/raw/sync_report.py",
            SYNC_REPORT_CLEAN + "SQL = 'update source_transcripts set payload = ?'\n",
        ),
    ],
)
def test_sync_results_stay_on_screen(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, planted: str, body: str
) -> None:
    clean = {
        "vlogsrt/project/sync_check.py": SYNC_CLEAN,
        "vlogsrt/project/raw/sync_report.py": SYNC_REPORT_CLEAN,
        "vlogsrt/project/raw/service.py": "from vlogsrt.project.raw import sync_report\n",
        "vlogsrt/project/config.py": "class ProjectConfig:\n    schema_version: int = 1\n",
    }
    _fake_source_tree(monkeypatch, tmp_path, clean)
    assert check_invariants.check_sync_check_only_displays() == []
    (tmp_path / planted).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / planted).write_text(body, encoding="utf-8")
    assert check_invariants.check_sync_check_only_displays()


def test_real_tree_keeps_the_sync_check_on_screen() -> None:
    assert check_invariants.check_sync_check_stays_pure() == []
    assert check_invariants.check_sync_check_only_displays() == []


def test_sync_report_may_use_python_replace_and_update() -> None:
    """SQL 검사는 대소문자를 가리지 않지만 `dict.update`·`replace(...)`는 SQL이 아니다."""
    pattern = check_invariants.SYNC_WRITES_SQL
    assert pattern.search("insert into source_transcripts values (?)")
    assert pattern.search("DELETE FROM jobs")
    assert not pattern.search("payload.update(stale=True)")
    assert not pattern.search("cue = replace(cue, start=start)")
    assert not pattern.search("os.replace(temporary, path)")


# ── D54: 네트워크 모듈은 한 파일, 그 파일은 transcribe.py만 안다 ────────────


_NETWORK_FILE = "vlogsrt/anthropic_messages.py"


@pytest.mark.parametrize(
    "statement",
    [
        "import urllib.request",
        "import urllib.request as fetch",
        "import urllib.error",
        "from urllib import request",
        "from urllib import request as fetch",
        "from urllib.request import urlopen",
        "from urllib.error import URLError",
        "import http.client",
        "from http import client",
        "from http.client import HTTPSConnection",
        "import socket",
        "import socket as raw",
        "from socket import create_connection",
        "import ssl",
        "from ssl import create_default_context",
    ],
)
def test_network_modules_fail_outside_the_exact_transport_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, statement: str
) -> None:
    """이름이 같은 다른 경로도 예외를 물려받지 못한다 — 판정은 정확 경로다."""
    _fake_source_tree(
        monkeypatch,
        tmp_path,
        {
            _NETWORK_FILE: statement + "\n",
            "vlogsrt/gadget.py": statement + "\n",
            "vlogsrt/project/anthropic_messages.py": statement + "\n",
        },
    )
    failures = check_invariants.check_network_boundary()
    assert len(failures) == 2, failures
    assert any("vlogsrt/gadget.py" in failure for failure in failures)
    assert any("vlogsrt/project/anthropic_messages.py" in failure for failure in failures)


def test_url_parsing_is_not_a_network_import(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fake_source_tree(
        monkeypatch,
        tmp_path,
        {
            "vlogsrt/gadget.py": (
                "import urllib.parse\nfrom urllib.parse import urlsplit\nfrom urllib import parse\n"
            )
        },
    )
    assert check_invariants.check_network_boundary() == []


@pytest.mark.parametrize(
    "statement",
    [
        "from vlogsrt import anthropic_messages",
        "from . import anthropic_messages",
        "import vlogsrt.anthropic_messages",
        "from vlogsrt.anthropic_messages import post_messages",
        "from .anthropic_messages import post_messages",
        "from ... import anthropic_messages",
    ],
)
def test_only_transcribe_may_import_the_claude_transport(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, statement: str
) -> None:
    """승인 문·관찰·취소를 건너뛰는 길은 없다 — 전송 파일을 아는 것은 transcribe.py뿐이다."""
    _fake_source_tree(
        monkeypatch,
        tmp_path,
        {
            "vlogsrt/transcribe.py": statement + "\n",
            "vlogsrt/project/raw/understanding_luna.py": statement + "\n",
            "ui/workspace_service.py": statement + "\n",
        },
    )
    failures = check_invariants.check_transport_entry()
    assert len(failures) == 2, failures
    assert any("understanding_luna.py" in failure for failure in failures)
    assert any("ui/workspace_service.py" in failure for failure in failures)


@pytest.mark.parametrize(
    "statement",
    [
        "from vlogsrt import gemini_messages",
        "from . import gemini_messages",
        "import vlogsrt.gemini_messages",
        "from vlogsrt.gemini_messages import post_generate_content",
        "from .gemini_messages import post_generate_content",
        "import importlib\nimportlib.import_module('vlogsrt.gemini_messages')",
        "import vlogsrt\nvlogsrt.gemini_messages.post_generate_content(client, 'm', {})",
    ],
)
def test_only_transcribe_may_import_the_gemini_transport(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, statement: str
) -> None:
    """Gemini 전송도 transcribe.py만 안다(W-30a, D83) — Claude 전송 파일도 그것을 모른다."""
    _fake_source_tree(
        monkeypatch,
        tmp_path,
        {
            "vlogsrt/transcribe.py": statement + "\n",
            "vlogsrt/gemini_messages.py": statement + "\n",
            _NETWORK_FILE: statement + "\n",
            "vlogsrt/project/raw/understanding_luna.py": statement + "\n",
        },
    )
    failures = check_invariants.check_transport_entry()
    assert len(failures) == 2, failures
    assert any("understanding_luna.py" in failure for failure in failures)
    assert any(_NETWORK_FILE in failure for failure in failures)


def test_the_gemini_transport_file_may_use_network_modules_at_its_exact_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """네트워크 경계는 공급자 전송 파일 둘의 정확 경로다 — 같은 이름의 다른 경로는 아니다."""
    statement = "import urllib.request\nimport ssl\n"
    _fake_source_tree(
        monkeypatch,
        tmp_path,
        {
            "vlogsrt/gemini_messages.py": statement,
            _NETWORK_FILE: statement,
            "vlogsrt/project/gemini_messages.py": statement,
        },
    )
    failures = check_invariants.check_network_boundary()
    assert failures and all("vlogsrt/project/gemini_messages.py" in f for f in failures), failures


@pytest.mark.parametrize(
    "statement",
    [
        "import google.genai",
        "from google import genai",
        "import google.generativeai as genai",
        "import vertexai",
    ],
)
def test_google_sdks_are_banned_everywhere(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, statement: str
) -> None:
    """Gemini도 표준 라이브러리로 부른다(W-30a, D83) — Google SDK 의존성은 금지다(규칙 10)."""
    _fake_source_tree(monkeypatch, tmp_path, {"vlogsrt/transcribe.py": statement + "\n"})
    failures = check_invariants.check_banned_imports()
    assert len(failures) == 1, failures


@pytest.mark.parametrize("name", ["anthropic", "httpx", "requests", "urllib3", "aiohttp"])
def test_http_client_libraries_are_banned_everywhere(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str
) -> None:
    """Claude는 표준 라이브러리로 부른다 — SDK·HTTP 클라이언트 의존성은 금지다(규칙 10)."""
    _fake_source_tree(
        monkeypatch,
        tmp_path,
        {
            "vlogsrt/transcribe.py": f"import {name}\n",
            "vlogsrt/project/a.py": f"from {name} import x\n",
        },
    )
    failures = check_invariants.check_banned_imports()
    assert len(failures) == 2, failures
    assert all(name in failure for failure in failures)


def test_claude_model_literals_live_only_in_the_exact_raw_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fake_source_tree(
        monkeypatch,
        tmp_path,
        {
            "vlogsrt/project/raw/config.py": 'CLAUDE = "claude-opus-5-5"\n',
            "vlogsrt/config.py": 'CLAUDE = "claude-opus-5-5"\n',
            "vlogsrt/project/config.py": "CLAUDE = 'claude-sonnet-5-5'\n",
            "vlogsrt/project/raw/understanding_luna.py": 'MODEL = "claude-opus-5-5"\n',
        },
    )
    failures = check_invariants.check_no_hardcoded_model()
    assert len(failures) == 3, failures
    assert not any("vlogsrt/project/raw/config.py" in failure for failure in failures)


def test_the_workbench_cannot_hardcode_a_model_name_either(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """기본 모델(D58)은 설정 한 곳에서 온다 — 작업대가 이름을 박으면 두 곳이 갈린다."""
    _fake_source_tree(
        monkeypatch,
        tmp_path,
        {
            "vlogsrt/project/raw/config.py": 'SONNET = "claude-sonnet-5-5"\n',
            "ui/workspace_service.py": 'DEFAULT = "claude-sonnet-5-5"\nLUNA = "gpt-6-luna"\n',
        },
    )
    failures = check_invariants.check_no_hardcoded_model()
    assert len(failures) == 2, failures
    assert all("ui/workspace_service.py" in failure for failure in failures)


@pytest.mark.parametrize(
    ("target", "check_name"),
    [
        ("vlogsrt/retext.py", "check_retext_stays_pure"),
        ("vlogsrt/project/raw/tiering.py", "check_tiering_stays_pure"),
        ("vlogsrt/project/edit.py", "check_edit_layer_ignores_audio"),
    ],
)
def test_pure_layers_cannot_reach_the_claude_transport(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, target: str, check_name: str
) -> None:
    _fake_source_tree(monkeypatch, tmp_path, {target: "from vlogsrt import anthropic_messages\n"})
    assert getattr(check_invariants, check_name)()
    _fake_source_tree(monkeypatch, tmp_path, {target: "from vlogsrt import gemini_messages\n"})
    assert getattr(check_invariants, check_name)()


@pytest.mark.parametrize("statement", ["import socket", "from urllib import request", "import ssl"])
def test_the_pure_cut_layer_cannot_import_network_modules(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, statement: str
) -> None:
    files = {f"vlogsrt/{name}": "" for name in check_invariants.PURE_MODULES}
    files["vlogsrt/cutpoints.py"] = statement + "\n"
    _fake_source_tree(monkeypatch, tmp_path, files)
    assert check_invariants.check_pure_layer()


def test_the_real_tree_passes_the_network_checks() -> None:
    assert check_invariants.check_network_boundary() == []
    assert check_invariants.check_transport_entry() == []
    assert check_invariants.check_no_hardcoded_model() == []


@pytest.mark.parametrize(
    "body",
    [
        "import importlib\nmodule = importlib.import_module('urllib.request')\n",
        "import importlib as il\nmodule = il.import_module('socket')\n",
        "from importlib import import_module\nmodule = import_module('ssl')\n",
        "from importlib import import_module as im\nmodule = im('socket')\n",
        "raw = __import__('socket')\n",
        "client = __import__('http.client')\n",
        "import urllib\nurllib.request.urlopen('https://x.example')\n",
        "import urllib\nurllib.error.URLError('x')\n",
        "import http\nhttp.client.HTTPSConnection('x.example')\n",
        "import urllib.parse\nurllib.request.urlopen('https://x.example')\n",
        "import urllib as u\nu.request.urlopen('https://x.example')\n",
    ],
)
def test_dynamic_and_attribute_network_imports_are_caught(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, body: str
) -> None:
    """지연·속성 우회 — 문자열 상수로 부르는 import와 맨 패키지 import 뒤의 하위 모듈 접근."""
    _fake_source_tree(monkeypatch, tmp_path, {"vlogsrt/gadget.py": body, _NETWORK_FILE: body})
    failures = check_invariants.check_network_boundary()
    assert failures and all("vlogsrt/gadget.py" in failure for failure in failures), failures


@pytest.mark.parametrize(
    "body",
    [
        "import urllib\nurllib.parse.quote('x')\n",
        "import http\nstatus = http.HTTPStatus.OK\n",
        "import importlib\nmodule = importlib.import_module('json')\n",
        "name = 'socket'\nmodule = __import__(name)\n",  # 상수가 아니면 판정하지 않는다
        "module = __import__(f'vlogsrt.project.raw.{name}')\n",
    ],
)
def test_harmless_dynamic_or_attribute_forms_pass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, body: str
) -> None:
    _fake_source_tree(monkeypatch, tmp_path, {"vlogsrt/gadget.py": body})
    assert check_invariants.check_network_boundary() == []
    assert check_invariants.check_transport_entry() == []


@pytest.mark.parametrize(
    "body",
    [
        "import importlib\nimportlib.import_module('vlogsrt.anthropic_messages')\n",
        "from importlib import import_module\nimport_module('.anthropic_messages', 'vlogsrt')\n",
        "from importlib import import_module as im\nim('vlogsrt.anthropic_messages')\n",
        "__import__('vlogsrt.anthropic_messages')\n",
        # 패키지 속성 — 누군가 한 번 import한 뒤라면 `vlogsrt.anthropic_messages`로 닿는다
        "import vlogsrt\nvlogsrt.anthropic_messages.post_messages(client, {})\n",
        "import vlogsrt as v\nsend = v.anthropic_messages.post_messages\n",
    ],
)
def test_dynamic_imports_of_the_claude_transport_are_caught(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, body: str
) -> None:
    _fake_source_tree(
        monkeypatch,
        tmp_path,
        {"vlogsrt/transcribe.py": body, "vlogsrt/project/raw/understanding_luna.py": body},
    )
    failures = check_invariants.check_transport_entry()
    assert len(failures) == 1, failures
    assert "understanding_luna.py" in failures[0]


@pytest.mark.parametrize(
    ("rel", "statement", "allowed"),
    [
        ("ui/instance.py", "import urllib.request", True),
        ("ui/instance.py", "from urllib.request import urlopen", True),
        ("ui/instance.py", "import socket", False),
        ("ui/instance.py", "import ssl", False),
        ("ui/service.py", "import socket", True),
        ("ui/service.py", "import urllib.request", False),
        ("ui/service.py", "import http.client", False),
        ("ui/app.py", "import socket", False),
        ("ui/deep/instance.py", "import urllib.request", False),  # 정확 경로만
    ],
)
def test_ui_network_imports_follow_an_exact_allowlist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, rel: str, statement: str, allowed: bool
) -> None:
    """ui/의 로컬 서버 코드는 파일마다 정해진 모듈 하나만 — 일괄 면제가 아니다."""
    _fake_source_tree(monkeypatch, tmp_path, {rel: statement + "\n"})
    failures = check_invariants.check_network_boundary()
    if allowed:
        assert failures == []
    else:
        assert len(failures) == 1 and rel in failures[0], failures


# ── D59 19c: 요청 빌더의 칸 — whisper에는 힌트가, 글자에는 시각 입자가 없다 ──────────

CAPTION_PROVIDER = "vlogsrt/project/raw/caption_provider.py"
CLEAN_BUILDERS = (
    "def timing_transcription_request(chunk, *, settings, temperature=None):\n"
    "    return Request(\n"
    "        chunk=chunk, timestamp_granularities=('segment',), temperature=temperature)\n"
    "\n"
    "def text_transcription_request(\n"
    "        chunk, *, settings, prompt='', keywords=None, languages=None):\n"
    "    return Request(chunk=chunk, prompt=prompt, keywords=keywords, languages=languages)\n"
)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("settings, temperature=None)", "settings, temperature=None, prompt='')"),
        ("temperature=temperature)", "temperature=temperature, keywords=('왕방',))"),
        ("temperature=temperature)", "temperature=temperature, languages=('ko',))"),
        ("languages=languages)\n", "languages=languages, timestamp_granularities=('segment',))\n"),
        (", languages=None):", ", languages=None, timestamp_granularities=()):"),
    ],
)
def test_request_builders_keep_their_lanes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, old: str, new: str
) -> None:
    _fake_source_tree(monkeypatch, tmp_path, {CAPTION_PROVIDER: CLEAN_BUILDERS})
    assert check_invariants.check_caption_request_lanes() == []
    (tmp_path / CAPTION_PROVIDER).write_text(CLEAN_BUILDERS.replace(old, new), encoding="utf-8")
    failures = check_invariants.check_caption_request_lanes()
    assert len(failures) == 1, failures
    (tmp_path / CAPTION_PROVIDER).write_text("def other():\n    pass\n", encoding="utf-8")
    assert len(check_invariants.check_caption_request_lanes()) == 2  # 빌더가 사라져도 통과 없음


def test_the_real_request_builders_keep_their_lanes() -> None:
    assert check_invariants.check_caption_request_lanes() == []


# ── D59 19c: 용어집은 DB만 알고, 설정은 용어집·글자 경로를 모른다 ─────────────────

GLOSSARY = "vlogsrt/project/raw/glossary.py"
RAW_CONFIG = "vlogsrt/project/raw/config.py"
CLEAN_GLOSSARY = (
    "from vlogsrt.keywords import dedupe, parse_keywords\n"
    "from vlogsrt.project.raw.models import RawWorkspaceError\n"
)


def _config_class(name: str) -> str:
    fields = check_invariants.CONFIG_PINNED_FIELDS[name]
    return f"class {name}:\n" + "".join(f"    {field}: object = None\n" for field in fields) + "\n"


CLEAN_CONFIG = (
    _config_class("RawCaptionContract")
    + _config_class("RawWorkflowConfig")
    + "class SourceTextSettings:\n    prompt: str = ''\n"
)


@pytest.mark.parametrize(
    "statement",
    [
        "import subprocess",
        "import openai",
        "from vlogsrt import transcribe",
        "from vlogsrt.project.raw import caption_provider",
        "from vlogsrt.project.raw.understanding import asr_chunk_key",
        "from vlogsrt.project.raw.config import RawWorkflowConfig",
        "from vlogsrt.project.raw import service",
        "BODY = Path('terms.txt').read_text()",
    ],
)
def test_the_glossary_layer_knows_only_the_workspace_db(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, statement: str
) -> None:
    _fake_source_tree(monkeypatch, tmp_path, {GLOSSARY: CLEAN_GLOSSARY, RAW_CONFIG: CLEAN_CONFIG})
    assert check_invariants.check_glossary_stays_local() == []
    (tmp_path / GLOSSARY).write_text(CLEAN_GLOSSARY + statement + "\n", encoding="utf-8")
    assert check_invariants.check_glossary_stays_local()
    (tmp_path / GLOSSARY).unlink()
    assert check_invariants.check_glossary_stays_local() == [
        "vlogsrt/project/raw/glossary.py: 용어집 계층이 사라졌다 (D59 19c)"
    ]


@pytest.mark.parametrize(
    ("old", "new", "count"),
    [
        # 이름 무늬와 고정 칸 둘 다에 걸린다
        (
            "    keywords: object = None\n",
            "    keywords: object = None\n    glossary: tuple = ()\n",
            2,
        ),
        # 이름 무늬로는 못 잡는 칸 — 고정 칸 목록이 잡는다
        (
            "    require_explicit_authorization: object = None\n",
            "    require_explicit_authorization: object = None\n    hints: tuple = ()\n",
            1,
        ),
        # 칸은 그대로인데 기본값이 글자 경로를 싣는다 — 이름 무늬가 잡는다
        ("    keywords: object = None\n", "    keywords: object = SOURCE_TEXT_TERMS\n", 1),
    ],
)
def test_the_workflow_config_never_carries_the_glossary_or_text_lane(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, old: str, new: str, count: int
) -> None:
    _fake_source_tree(monkeypatch, tmp_path, {GLOSSARY: CLEAN_GLOSSARY, RAW_CONFIG: CLEAN_CONFIG})
    assert check_invariants.check_glossary_stays_local() == []
    (tmp_path / RAW_CONFIG).write_text(CLEAN_CONFIG.replace(old, new), encoding="utf-8")
    failures = check_invariants.check_glossary_stays_local()
    assert len(failures) == count and all("whisper 열쇠" in f for f in failures), failures


def test_the_real_glossary_and_config_pass() -> None:
    assert check_invariants.check_glossary_stays_local() == []


# ── D59 19e: 원본 자막의 「확인 필요」 표시는 순수하고, 서비스만 읽고, 지문 밖에 산다 ───────────

SUSPECTS = "vlogsrt/project/raw/subtitle_suspects.py"
CLEAN_SUSPECTS = (
    "import unicodedata\n"
    "from vlogsrt.hangul import strip_for_compare\n"
    "from vlogsrt.project.raw.config import SUBTITLE_SUSPECT_SETTINGS\n"
)


@pytest.mark.parametrize(
    "statement",
    [
        "import subprocess",
        "import sqlite3",
        "from vlogsrt.project import sync_check",
        "from vlogsrt.project.raw import sync_report",
        "from vlogsrt.project.raw import source_transcript",
        "from vlogsrt.project.raw import service",
        "from vlogsrt.project.raw.tiering import classify",
        "BODY = Path('cues.json').read_text()",
    ],
)
def test_the_suspect_layer_takes_values_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, statement: str
) -> None:
    _fake_source_tree(monkeypatch, tmp_path, {SUSPECTS: CLEAN_SUSPECTS})
    assert check_invariants.check_subtitle_suspects_only_display() == []
    (tmp_path / SUSPECTS).write_text(CLEAN_SUSPECTS + statement + "\n", encoding="utf-8")
    assert check_invariants.check_subtitle_suspects_only_display()
    (tmp_path / SUSPECTS).unlink()
    assert check_invariants.check_subtitle_suspects_only_display() == [
        f"{SUSPECTS}: 자막 의심 표시 계층이 사라졌다 (D59 19e)"
    ]


@pytest.mark.parametrize(
    "importer",
    [
        "vlogsrt/project/raw/tiering.py",
        "vlogsrt/project/raw/auto_cleanup.py",
        "vlogsrt/project/raw/finalcut_export.py",
        "vlogsrt/project/raw/source_transcript.py",
    ],
)
def test_only_the_service_reads_the_suspects(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, importer: str
) -> None:
    service = "vlogsrt/project/raw/service.py"
    reads = "from vlogsrt.project.raw import subtitle_suspects\n"
    _fake_source_tree(monkeypatch, tmp_path, {SUSPECTS: CLEAN_SUSPECTS, service: reads})
    assert check_invariants.check_subtitle_suspects_only_display() == []
    (tmp_path / importer).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / importer).write_text(reads, encoding="utf-8")
    assert check_invariants.check_subtitle_suspects_only_display() == [
        f"{importer}: subtitle_suspects를 import한다 — 의심 표시가 표시 밖으로 샌다 (D59 19e)"
    ]


@pytest.mark.parametrize(
    ("rel", "body"),
    [
        (
            RAW_CONFIG,
            "class RawWorkflowConfig:\n    suspects: object = SUBTITLE_SUSPECT_SETTINGS\n",
        ),
        (RAW_CONFIG, "class SourceTextSettings:\n    hints: object = SubtitleSuspectSettings()\n"),
        ("vlogsrt/project/raw/source_text.py", "from .config import SUBTITLE_SUSPECT_SETTINGS\n"),
        ("vlogsrt/project/raw/cross_check.py", "KEY = 'subtitle_suspects'\n"),
    ],
)
def test_the_suspect_settings_stay_outside_every_fingerprint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, rel: str, body: str
) -> None:
    defined = "class SubtitleSuspectSettings:\n    repeat_run_min: int = 3\n"
    _fake_source_tree(monkeypatch, tmp_path, {SUSPECTS: CLEAN_SUSPECTS, RAW_CONFIG: defined})
    assert check_invariants.check_subtitle_suspects_only_display() == []  # 정의 자리는 괜찮다
    (tmp_path / rel).write_text(defined + body, encoding="utf-8")
    (failure,) = check_invariants.check_subtitle_suspects_only_display()
    assert failure.startswith(rel) and "지문·캐시 열쇠 밖" in failure


def test_the_real_suspect_layer_passes() -> None:
    assert check_invariants.check_subtitle_suspects_only_display() == []


# ── D59 19f: 말소리 빈칸 재전사 — 고르기는 순수, 넣기는 승인된 빈칸 재전사만, whisper 힌트 없음

GAPS = "vlogsrt/project/raw/speech_gaps.py"
CLEAN_GAPS = (
    "from vlogsrt.project import sync_check\n"
    "from vlogsrt.project.config import GapRefillSettings, SyncCheckSettings\n"
)
REFILL = "vlogsrt/project/raw/speech_refill.py"
CLEAN_REFILL = (
    "from vlogsrt.project.raw import speech_gaps\n"
    "def run(audio, spans, provider):\n"
    "    return stage1.transcribe_selected_audio(audio, spans, provider=provider, on=None)\n"
)
SERVICE = "vlogsrt/project/raw/service.py"
CLEAN_SERVICE = (
    "from vlogsrt.project.raw import speech_gaps\n"
    "class RawWorkspaceService:\n"
    "    def _refill_settle(self, workspace, rows):\n"
    "        return source_transcript.insert_segments(workspace, 'a', 'r', rows)\n"
)


@pytest.mark.parametrize(
    "statement",
    [
        "import subprocess",
        "import sqlite3",
        "from vlogsrt import preprocess",
        "from vlogsrt.project.raw import sync_report",
        "from vlogsrt.project.raw import source_transcript",
        "from vlogsrt.project.raw import service",
        "from vlogsrt.project.raw import understanding",
        "from vlogsrt.project.raw import speech_refill",
        "from vlogsrt.project.raw.tiering import classify",
        "BODY = Path('vad.json').read_text()",
    ],
)
def test_the_gap_selection_takes_values_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, statement: str
) -> None:
    _fake_source_tree(monkeypatch, tmp_path, {GAPS: CLEAN_GAPS})
    assert check_invariants.check_speech_gaps_stays_pure() == []
    (tmp_path / GAPS).write_text(CLEAN_GAPS + statement + "\n", encoding="utf-8")
    assert check_invariants.check_speech_gaps_stays_pure()
    (tmp_path / GAPS).unlink()
    assert check_invariants.check_speech_gaps_stays_pure() == [
        f"{GAPS}: 말소리 빈칸 고르기 계층이 사라졌다 (D59 19f)"
    ]


@pytest.mark.parametrize(
    "importer",
    [
        "vlogsrt/project/raw/tiering.py",
        "vlogsrt/project/raw/auto_cleanup.py",
        "vlogsrt/project/raw/selection_analysis.py",
        "vlogsrt/project/raw/finalcut_export.py",
    ],
)
def test_only_the_refill_and_the_service_read_the_gaps(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, importer: str
) -> None:
    reads = "from vlogsrt.project.raw import speech_gaps\n"
    _fake_source_tree(
        monkeypatch, tmp_path, {GAPS: CLEAN_GAPS, SERVICE: reads, REFILL: CLEAN_REFILL}
    )
    assert check_invariants.check_speech_gaps_stays_pure() == []
    (tmp_path / importer).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / importer).write_text(reads, encoding="utf-8")
    assert check_invariants.check_speech_gaps_stays_pure() == [
        f"{importer}: speech_gaps를 import한다 — 다시 물을 구간이 묻기 밖으로 샌다 (D59 19f)"
    ]


def test_the_gap_selection_may_read_the_sync_cells_but_nothing_else_may_newly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """D55 「표시만」의 예외는 빈칸 고르기 하나다 — 빈칸 재전사(묻기)는 확인 칸을 직접 모른다."""
    clean = {
        "vlogsrt/project/sync_check.py": SYNC_CLEAN,
        "vlogsrt/project/raw/sync_report.py": SYNC_REPORT_CLEAN,
        "vlogsrt/project/config.py": "class ProjectConfig:\n    schema_version: int = 1\n",
        GAPS: CLEAN_GAPS,
    }
    _fake_source_tree(monkeypatch, tmp_path, clean)
    assert check_invariants.check_sync_check_only_displays() == []
    (tmp_path / REFILL).write_text("from vlogsrt.project import sync_check\n", encoding="utf-8")
    assert check_invariants.check_sync_check_only_displays() == [
        f"{REFILL}: sync_check를 import한다 — 판정이 표시 밖으로 샌다 (D55)"
    ]


@pytest.mark.parametrize(
    ("rel", "body"),
    [
        (RAW_CONFIG, "class RawWorkflowConfig:\n    gaps: object = GAP_REFILL_SETTINGS\n"),
        ("vlogsrt/project/raw/understanding.py", "KEY = GapRefillSettings()\n"),
        ("vlogsrt/project/raw/analysis_approval.py", "from . import speech_gaps\n"),
        (
            "vlogsrt/project/config.py",
            "class ProjectConfig:\n    gaps: object = GapRefillSettings()\n",
        ),
    ],
)
def test_the_gap_settings_stay_outside_every_fingerprint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, rel: str, body: str
) -> None:
    clean = {
        "vlogsrt/project/sync_check.py": SYNC_CLEAN,
        "vlogsrt/project/raw/sync_report.py": SYNC_REPORT_CLEAN,
        "vlogsrt/project/config.py": "class ProjectConfig:\n    schema_version: int = 1\n",
    }
    _fake_source_tree(monkeypatch, tmp_path, clean)
    assert check_invariants.check_sync_check_only_displays() == []
    (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / rel).write_text(body, encoding="utf-8")
    failures = check_invariants.check_sync_check_only_displays()
    assert any("지문·캐시 열쇠 밖" in failure for failure in failures), failures


@pytest.mark.parametrize(
    ("rel", "body", "line"),
    [
        (
            SERVICE,
            "class RawWorkspaceService:\n"
            "    def edit_source_transcript(self, workspace, rows):\n"
            "        return source_transcript.insert_segments(workspace, 'a', 'r', rows)\n",
            3,
        ),
        (
            "vlogsrt/project/raw/analysis_workflow.py",
            "def _seed(workspace):\n"
            "    source_transcript.insert_segments(workspace, 'a', 'r', [])\n",
            2,
        ),
        (
            "vlogsrt/project/raw/speech_refill.py",
            "from vlogsrt.project.raw.source_transcript import insert_segments\n",
            1,
        ),
        (
            "ui/workspace_service.py",
            "def refill_x(raw):\n    raw.insert_segments(1, 2, 3, [])\n",
            2,
        ),
        # 검증 지적 7 — 이름 앞머리(refill_)로는 안 된다. 정산 한 자리만이다.
        (
            SERVICE,
            "class RawWorkspaceService:\n"
            "    def refill_source(self, workspace, rows):\n"
            "        return source_transcript.insert_segments(workspace, 'a', 'r', rows)\n",
            3,
        ),
        # 가리키기만 해도 자리다 — 별명·문자열 이름·이름 가져오기·람다로 넘기기.
        (
            SERVICE,
            "class RawWorkspaceService:\n"
            "    def _refill_settle(self, workspace, rows):\n"
            "        put = source_transcript.insert_segments\n"
            "        return put(workspace, 'a', 'r', rows)\n",
            3,
        ),
        (
            SERVICE,
            "class RawWorkspaceService:\n"
            "    def _refill_plan(self, workspace):\n"
            "        put = getattr(source_transcript, 'insert_segments')\n"
            "        return put(workspace, 'a', 'r', [])\n",
            3,
        ),
        (
            SERVICE,
            "from vlogsrt.project.raw.source_transcript import insert_segments as put\n",
            1,
        ),
        (
            SERVICE,
            "class RawWorkspaceService:\n"
            "    def _refill_settle(self, workspace, rows):\n"
            "        later = lambda r: source_transcript.insert_segments(workspace, 'a', 'r', r)\n"
            "        return later(rows)\n",
            3,
        ),
        (
            "vlogsrt/project/raw/source_transcript.py",
            "def insert_segments(workspace, asset_id, revision, rows):\n    return None\n"
            "def seed(workspace):\n    return insert_segments(workspace, 'a', 'r', [])\n",
            4,
        ),
        (
            "vlogsrt/pipeline.py",
            "from vlogsrt.project.raw.source_transcript import insert_segments\n",
            1,
        ),
    ],
)
def test_only_the_approved_refill_inserts_rows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, rel: str, body: str, line: int
) -> None:
    owner = (
        "def insert_segments(workspace, asset_id, revision, rows):\n    return None\n"
        "__all__ = ['insert_segments']\n"
    )
    _fake_source_tree(
        monkeypatch,
        tmp_path,
        {SERVICE: CLEAN_SERVICE, "vlogsrt/project/raw/source_transcript.py": owner},
    )
    assert check_invariants.check_insert_segments_only_from_refill() == []
    (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / rel).write_text(body, encoding="utf-8")
    (failure,) = check_invariants.check_insert_segments_only_from_refill()
    assert failure.startswith(f"{rel}:{line}: insert_segments를") and "_refill_settle" in failure


@pytest.mark.parametrize(
    "importer",
    [
        "ui/workspace_service.py",
        "ui/app.py",
        "vlogsrt/project/raw/analysis_workflow.py",
        "vlogsrt/cli.py",
    ],
)
def test_only_the_service_calls_the_paid_refill(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, importer: str
) -> None:
    """검증 지적 7 — 빈칸 재전사는 서비스만 부른다(화면은 서비스 메서드로만 닿는다)."""
    reads = "from vlogsrt.project.raw import speech_refill\n"
    _fake_source_tree(monkeypatch, tmp_path, {SERVICE: reads, REFILL: CLEAN_REFILL})
    assert check_invariants.check_speech_refill_only_from_service() == []
    (tmp_path / importer).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / importer).write_text(reads, encoding="utf-8")
    assert check_invariants.check_speech_refill_only_from_service() == [
        f"{importer}: speech_refill을 import한다 — 빈칸 재전사는 서비스의 승인 문으로만 닿는다"
        " (D59 19f)"
    ]


@pytest.mark.parametrize(
    "body",
    [
        "stage1.transcribe_selected_audio(audio, spans, provider=p, prompt='한국 여행 브이로그')\n",
        "provider.timed_transcript(binding=b, chunk=c, keywords=('왕방',))\n",
        "provider.timed_transcript(binding=b, chunk=c, languages=('ko',))\n",
        "request = timing_transcription_request(chunk, settings=s)\n",
        "sender.send_request(request, cfg, kind='caption-timing', stem='', source='')\n",
        # 검증 지적 7 — 풀어 넣은 인자는 무엇이 실리는지 보이지 않는다.
        "stage1.transcribe_selected_audio(audio, spans, provider=p, **extra)\n",
        "provider.timed_transcript(*parts)\n",
    ],
)
def test_the_refill_whisper_carries_no_hints(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, body: str
) -> None:
    _fake_source_tree(monkeypatch, tmp_path, {REFILL: CLEAN_REFILL})
    assert check_invariants.check_refill_whisper_has_no_hints() == []
    (tmp_path / REFILL).write_text(CLEAN_REFILL + body, encoding="utf-8")
    assert check_invariants.check_refill_whisper_has_no_hints()
    (tmp_path / REFILL).unlink()
    assert check_invariants.check_refill_whisper_has_no_hints() == [
        f"{REFILL}: 말소리 빈칸 재전사 계층이 사라졌다 (D59 19f)"
    ]


def test_the_real_refill_layers_pass() -> None:
    assert check_invariants.check_speech_gaps_stays_pure() == []
    assert check_invariants.check_insert_segments_only_from_refill() == []
    assert check_invariants.check_refill_whisper_has_no_hints() == []
    assert check_invariants.check_speech_refill_only_from_service() == []


# ── D59 19g: 시각 맞춤 제안 — 순수하고, 정해진 자리(19g에는 없음)만 부르며, 기준은 지문 밖 ───

SNAP = "vlogsrt/project/sync_snap.py"
CLEAN_SNAP = (
    "from __future__ import annotations\n"
    "import hashlib\n"
    "from bisect import bisect_left\n"
    "from collections.abc import Sequence\n"
    "from fractions import Fraction\n"
    "from vlogsrt.project import sync_check\n"
    "from vlogsrt.project.config import SyncSnapSettings, sync_snap_problems\n"
    "from .sync_check import Cue\n"
)


@pytest.mark.parametrize(
    "statement",
    [
        "import os",
        "import sqlite3",
        "import subprocess",
        "import importlib",
        "from pathlib import Path",
        "from vlogsrt import refine",
        "from vlogsrt import retext",
        "from vlogsrt import transcribe",
        "from vlogsrt import preprocess",
        "from vlogsrt.project.raw import sync_report",
        "from vlogsrt.project.raw import source_transcript",
        "from vlogsrt.project.raw import service",
        "from vlogsrt.project.raw.tiering import classify",
        "from vlogsrt.project import sync_check, pcm",
        "from .raw import speech_gaps",
        "from .. import output",
        "import vlogsrt.output as cues",
        "BODY = open('cues.json').read()",
        "DATA = PCM.read_bytes()",
        "MODULE = __import__('os')",
        "OPEN = getattr(__builtins__, 'op' + 'en')",
    ],
)
def test_the_snap_proposal_takes_values_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, statement: str
) -> None:
    _fake_source_tree(monkeypatch, tmp_path, {SNAP: CLEAN_SNAP})
    assert check_invariants.check_sync_snap_stays_pure() == []
    (tmp_path / SNAP).write_text(CLEAN_SNAP + statement + "\n", encoding="utf-8")
    assert check_invariants.check_sync_snap_stays_pure()
    (tmp_path / SNAP).unlink()
    assert check_invariants.check_sync_snap_stays_pure() == [
        f"{SNAP}: 시각 맞춤 제안 계층이 사라졌다 (D59 19g)"
    ]


@pytest.mark.parametrize(
    ("importer", "body"),
    [
        # 별명 · ui/ · 등급 · 보내기 · 지문 자리 · 레거시 · 문자열로 끌어오기 · 서비스 옆
        # 빈칸 재전사
        ("vlogsrt/project/raw/tiering.py", "from vlogsrt.project import sync_snap as snap\n"),
        ("ui/workspace_service.py", "from vlogsrt.project import sync_snap\n"),
        ("ui/ui_facade.py", "import vlogsrt.project.sync_snap as proposals\n"),
        (
            "vlogsrt/project/raw/finalcut_export.py",
            "from vlogsrt.project.sync_snap import propose\n",
        ),
        ("vlogsrt/project/raw/understanding.py", "from ..sync_snap import plan_document\n"),
        ("vlogsrt/cli.py", "from vlogsrt.project import sync_snap\n"),
        (
            "vlogsrt/project/raw/analysis_workflow.py",
            "import importlib\nSNAP = importlib.import_module('vlogsrt.project.sync_snap')\n",
        ),
        # 19h가 서비스를 목록에 적었다 — 그 옆의 다른 raw 모듈은 여전히 자리다.
        ("vlogsrt/project/raw/speech_refill.py", "from vlogsrt.project import sync_snap\n"),
        # 기준 이름만 가져와도 자리다 — 등급·타임라인·지문 자리
        ("vlogsrt/project/raw/tiering.py", "from vlogsrt.project.config import SyncSnapSettings\n"),
        (
            "vlogsrt/project/raw/finalcut_timeline.py",
            "from vlogsrt.project.config import SYNC_SNAP_SETTINGS as LIMITS\n",
        ),
        ("vlogsrt/project/raw/config.py", "from ..config import sync_snap_problems\n"),
        # 19g 검증 뒤 고침 4 — import 문이 아닌 길: 설정 모듈의 속성 꺼내기 · 이름에 담은 문자열
        (
            "vlogsrt/project/raw/tiering.py",
            "from vlogsrt.project import config as _cfg\n"
            "_R = _cfg.SYNC_SNAP_SETTINGS.reach_seconds\n",
        ),
        (
            "vlogsrt/project/raw/finalcut_export.py",
            "import vlogsrt.project.config as _c\n_X = _c.SyncSnapSettings\n",
        ),
        (
            "vlogsrt/project/raw/finalcut_timeline.py",
            "import importlib\n_N = 'vlogsrt.project.sync_snap'\n"
            "_M = importlib.import_module(_N)\n",
        ),
    ],
)
def test_only_listed_sites_reach_the_snap_proposal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, importer: str, body: str
) -> None:
    owner = "class SyncSnapSettings:\n    reach_seconds: object = None\n"
    _fake_source_tree(monkeypatch, tmp_path, {SNAP: CLEAN_SNAP, "vlogsrt/project/config.py": owner})
    assert check_invariants.check_sync_snap_only_from_listed() == []
    (tmp_path / importer).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / importer).write_text(body, encoding="utf-8")
    (failure,) = check_invariants.check_sync_snap_only_from_listed()
    assert failure.startswith(f"{importer}: ") and "(D59 19g)" in failure


def test_the_snap_importer_list_is_the_service_only() -> None:
    """19h — 제안을 부르는 자리는 서비스 하나다(그 안에서도 적용·되돌리기 함수만, 아래 검사)."""
    assert check_invariants.SYNC_SNAP_IMPORTERS == ("vlogsrt/project/raw/service.py",)


@pytest.mark.parametrize(
    ("rel", "body"),
    [
        (RAW_CONFIG, "class RawWorkflowConfig:\n    snap: object = SYNC_SNAP_SETTINGS\n"),
        ("vlogsrt/project/raw/understanding.py", "KEY = SyncSnapSettings()\n"),
        ("vlogsrt/project/raw/analysis_approval.py", "from vlogsrt.project import sync_snap\n"),
        ("vlogsrt/project/raw/final_caption.py", "LIMIT = sync_snap.propose\n"),
        (
            "vlogsrt/project/config.py",
            "class ProjectConfig:\n    snap: object = SyncSnapSettings()\n",
        ),
    ],
)
def test_the_snap_settings_stay_outside_every_fingerprint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, rel: str, body: str
) -> None:
    clean = {
        "vlogsrt/project/sync_check.py": SYNC_CLEAN,
        "vlogsrt/project/raw/sync_report.py": SYNC_REPORT_CLEAN,
        "vlogsrt/project/config.py": "class ProjectConfig:\n    schema_version: int = 1\n",
        SNAP: CLEAN_SNAP,
    }
    _fake_source_tree(monkeypatch, tmp_path, clean)
    assert check_invariants.check_sync_check_only_displays() == []
    (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / rel).write_text(body, encoding="utf-8")
    failures = check_invariants.check_sync_check_only_displays()
    assert any("지문·캐시 열쇠 밖" in failure for failure in failures), failures


def test_the_snap_proposal_may_read_the_sync_cells_but_nothing_new_may(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """D55 「표시만」의 예외는 제안 모듈 하나다 — 옆에 새 모듈을 두면 확인 계산을 못 들인다."""
    clean = {
        "vlogsrt/project/sync_check.py": SYNC_CLEAN,
        "vlogsrt/project/raw/sync_report.py": SYNC_REPORT_CLEAN,
        "vlogsrt/project/config.py": "class ProjectConfig:\n    schema_version: int = 1\n",
        SNAP: CLEAN_SNAP,
    }
    _fake_source_tree(monkeypatch, tmp_path, clean)
    assert check_invariants.check_sync_check_only_displays() == []
    helper = "vlogsrt/project/sync_snap_apply.py"
    (tmp_path / helper).write_text("from vlogsrt.project import sync_check\n", encoding="utf-8")
    assert check_invariants.check_sync_check_only_displays() == [
        f"{helper}: sync_check를 import한다 — 판정이 표시 밖으로 샌다 (D55)"
    ]


def test_the_real_snap_layer_passes() -> None:
    assert check_invariants.check_sync_snap_stays_pure() == []
    assert check_invariants.check_sync_snap_only_from_listed() == []
    assert check_invariants.check_sync_check_only_displays() == []


# ── D59 19h: 시각 맞춤 적용·되돌리기 — 서비스 안에서도 apply_snap*·revert_snap*만 ───

CLEAN_SNAP_SERVICE = (
    "class RawWorkspaceService:\n"
    "    def apply_snap(self, workspace, plan):\n"
    "        from vlogsrt.project.raw import source_transcript\n"
    "        return source_transcript.apply_changes(workspace, 'a', 'r', [], plan_sha256='', "
    "settings_version='')\n"
    "    def _apply_snap_plan(self, cues):\n"
    "        from vlogsrt.project import sync_snap\n"
    "        from vlogsrt.project.config import SYNC_SNAP_SETTINGS\n"
    "        return sync_snap.propose(cues, None, None, source=None, settings=SYNC_SNAP_SETTINGS)\n"
    "    def revert_snap(self, workspace, revision):\n"
    "        from vlogsrt.project.raw import source_transcript\n"
    "        return source_transcript.restore(workspace, 'a', revision)\n"
    "    def revert_snap_state(self, document):\n"
    "        return source_transcript.snap_applied(document)\n"
    "def _apply_snap_preview(plan):\n"
    "    from vlogsrt.project import sync_snap\n"
    "    return sync_snap.STATUSES\n"
)
SNAP_OWNER = (
    "def apply_changes(workspace, asset_id, revision, times, *, plan_sha256, settings_version):\n"
    "    return None\n"
    "def restore(workspace, asset_id, revision):\n"
    "    return None\n"
)


@pytest.mark.parametrize(
    ("body", "line"),
    [
        ("from vlogsrt.project import sync_snap\n", 1),  # 모듈 수준 — 모든 메서드가 쥔다
        (
            "class RawWorkspaceService:\n"
            "    def refill_quote(self, cues):\n"
            "        from vlogsrt.project import sync_snap\n"
            "        return sync_snap.propose(cues)\n",
            3,
        ),
        (
            "class RawWorkspaceService:\n"
            "    def _refill_plan(self):\n"
            "        from vlogsrt.project import config\n"
            "        return config.SYNC_SNAP_SETTINGS\n",
            4,
        ),
        (
            "class RawWorkspaceService:\n"
            "    def edit_source_transcript(self):\n"
            "        import importlib\n"
            "        return importlib.import_module('vlogsrt.project.sync_snap')\n",
            4,
        ),
        (
            "class RawWorkspaceService:\n"
            "    def relisten_source(self):\n"
            "        from vlogsrt.project.sync_snap import propose as snap\n"
            "        return snap\n",
            3,
        ),
        # 적용 함수 안에 둔 다른 이름의 함수는 그 함수의 자리다.
        (
            "class RawWorkspaceService:\n"
            "    def apply_snap(self):\n"
            "        def helper():\n"
            "            from vlogsrt.project import sync_snap\n"
            "            return sync_snap\n"
            "        return helper()\n",
            4,
        ),
        ("def _tier_snap(plan):\n    return plan.sync_snap\n", 2),
    ],
)
def test_inside_the_service_only_the_snap_entries_reach_the_proposal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, body: str, line: int
) -> None:
    _fake_source_tree(monkeypatch, tmp_path, {SERVICE: CLEAN_SNAP_SERVICE})
    assert check_invariants.check_sync_snap_in_service_only_from_snap_entries() == []
    (tmp_path / SERVICE).write_text(body, encoding="utf-8")
    failures = check_invariants.check_sync_snap_in_service_only_from_snap_entries()
    assert failures and failures[0].startswith(f"{SERVICE}:{line}: 시각 맞춤 제안을"), failures
    assert all("(D59 19h)" in failure for failure in failures)


@pytest.mark.parametrize(
    ("rel", "body", "line", "name"),
    [
        # 서비스 안 — 쓰기는 각자의 자리 하나다(적용은 apply_snap, 되돌리기는 revert_snap).
        (
            SERVICE,
            "class RawWorkspaceService:\n"
            "    def revert_snap(self, workspace):\n"
            "        return source_transcript.apply_changes(workspace, 'a', 'r', [])\n",
            3,
            "apply_changes",
        ),
        (
            SERVICE,
            "class RawWorkspaceService:\n"
            "    def apply_snap(self, workspace):\n"
            "        return source_transcript.restore(workspace, 'a', 'r')\n",
            3,
            "restore",
        ),
        (
            SERVICE,
            "class RawWorkspaceService:\n"
            "    def apply_snap_preview(self, workspace):\n"
            "        return source_transcript.apply_changes(workspace, 'a', 'r', [])\n",
            3,
            "apply_changes",
        ),
        (
            SERVICE,
            "class RawWorkspaceService:\n"
            "    def edit_source_transcript(self, workspace):\n"
            "        return source_transcript.restore(workspace, 'a', 'r')\n",
            3,
            "restore",
        ),
        # 가리키기만 해도 자리다 — 별명·문자열 이름·이름 가져오기·람다로 넘기기.
        (
            SERVICE,
            "class RawWorkspaceService:\n"
            "    def apply_snap(self, workspace):\n"
            "        put = source_transcript.apply_changes\n"
            "        return put(workspace, 'a', 'r', [])\n",
            3,
            "apply_changes",
        ),
        (
            SERVICE,
            "class RawWorkspaceService:\n"
            "    def apply_snap(self, workspace):\n"
            "        put = getattr(source_transcript, 'apply_changes')\n"
            "        return put(workspace, 'a', 'r', [])\n",
            3,
            "apply_changes",
        ),
        (
            SERVICE,
            "class RawWorkspaceService:\n"
            "    def revert_snap(self, workspace):\n"
            "        undo = getattr(source_transcript, 'restore')\n"
            "        return undo(workspace, 'a', 'r')\n",
            3,
            "restore",
        ),
        (
            SERVICE,
            "from vlogsrt.project.raw.source_transcript import restore as undo\n",
            1,
            "restore",
        ),
        (
            SERVICE,
            "class RawWorkspaceService:\n"
            "    def apply_snap(self, workspace):\n"
            "        later = lambda: source_transcript.apply_changes(workspace, 'a', 'r', [])\n"
            "        return later()\n",
            3,
            "apply_changes",
        ),
        # 화면·등급·제거·보내기·지문 자리·작업 흐름·레거시 — 시각 맞춤의 쓰기에 닿지 못한다.
        (
            "ui/workspace_service.py",
            "def snap(raw):\n    return raw.restore(1, 2, 3)\n",
            2,
            "restore",
        ),
        (
            "ui/ui_facade.py",
            "def snap(raw):\n    return raw.source_transcript.apply_changes(1, 2, 3, [])\n",
            2,
            "apply_changes",
        ),
        (
            "vlogsrt/project/raw/tiering.py",
            "from vlogsrt.project.raw.source_transcript import apply_changes\n",
            1,
            "apply_changes",
        ),
        (
            "vlogsrt/project/edit.py",
            "def cut(workspace):\n    return source_transcript.restore(workspace, 'a', 'r')\n",
            2,
            "restore",
        ),
        (
            "vlogsrt/project/raw/finalcut_export.py",
            "import operator\nUNDO = operator.attrgetter('restore')\n",
            2,
            "restore",
        ),
        (
            "vlogsrt/project/raw/understanding.py",
            "def key(workspace):\n    return source_transcript.apply_changes\n",
            2,
            "apply_changes",
        ),
        (
            "vlogsrt/project/raw/analysis_workflow.py",
            "def seed(workspace):\n    source_transcript.apply_changes(workspace, 'a', 'r', [])\n",
            2,
            "apply_changes",
        ),
        (
            "vlogsrt/pipeline.py",
            "from vlogsrt.project.raw import source_transcript as st\nX = st.restore\n",
            2,
            "restore",
        ),
        # 정의한 모듈 안에서도 다른 함수가 부르면 자리다.
        (
            "vlogsrt/project/raw/source_transcript.py",
            SNAP_OWNER
            + "def edit(workspace):\n    return apply_changes(workspace, 'a', 'r', [])\n",
            6,
            "apply_changes",
        ),
    ],
)
def test_only_the_snap_entries_write_the_snapped_transcript(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, rel: str, body: str, line: int, name: str
) -> None:
    clean = {SERVICE: CLEAN_SNAP_SERVICE, "vlogsrt/project/raw/source_transcript.py": SNAP_OWNER}
    _fake_source_tree(monkeypatch, tmp_path, clean)
    assert check_invariants.check_snap_writes_only_from_service() == []
    (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / rel).write_text(body, encoding="utf-8")
    (failure,) = check_invariants.check_snap_writes_only_from_service()
    assert failure.startswith(f"{rel}:{line}: {name}을"), failure
    assert check_invariants.SNAP_WRITES[name] in failure and "(D59 19h)" in failure


def test_the_common_word_restore_is_not_a_write_by_itself(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`restore`는 흔한 낱말이다 — 화면의 단추 변수·동작 이름 문자열은 자리가 아니다."""
    ui = (
        "def build(gr, view):\n"
        "    restore = gr.Button('자동 제외 전체 복원')\n"
        "    return restore, view('restore')\n"
    )
    clean = {
        SERVICE: CLEAN_SNAP_SERVICE,
        "vlogsrt/project/raw/source_transcript.py": SNAP_OWNER,
        "ui/workspace_service.py": ui,
    }
    _fake_source_tree(monkeypatch, tmp_path, clean)
    assert check_invariants.check_snap_writes_only_from_service() == []


def test_the_screen_reaches_the_snap_only_through_the_service(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """화면은 제안 모듈도 원본 자막 모듈도 들이지 못한다 — 서비스 메서드로만 닿는다."""
    allowed = "from vlogsrt.project.raw import service as raw_service\n"
    _fake_source_tree(monkeypatch, tmp_path, {"ui/workspace_service.py": allowed})
    assert check_invariants.check_source_does_not_import_project() == []
    assert check_invariants.check_sync_snap_only_from_listed() == []
    screen = tmp_path / "ui/workspace_service.py"
    screen.write_text(allowed + "from vlogsrt.project.raw import source_transcript\n", "utf-8")
    assert check_invariants.check_source_does_not_import_project()
    screen.write_text(allowed + "from vlogsrt.project import sync_snap\n", "utf-8")
    assert check_invariants.check_source_does_not_import_project()
    assert check_invariants.check_sync_snap_only_from_listed()


def test_the_real_snap_entries_pass() -> None:
    assert check_invariants.check_sync_snap_in_service_only_from_snap_entries() == []
    assert check_invariants.check_snap_writes_only_from_service() == []
    assert check_invariants.check_sync_snap_only_from_listed() == []


# ── 19h 검증 뒤 고침 2 — 빈틈 다섯: 서비스를 거친 제안 · 정의 모듈의 맨 이름 · 모듈 수준
# 같은 이름 · 앞머리 정규식 · 첨자 꺼내기 ───────────────────────────────────


@pytest.mark.parametrize(
    ("rel", "body", "line", "name"),
    [
        # 서비스 메서드가 아닌 모듈 수준 같은 이름 — 소유자는 정확히
        # `RawWorkspaceService.apply_snap`이다.
        (
            SERVICE,
            CLEAN_SNAP_SERVICE
            + "def apply_snap(workspace):\n"
            + "    return source_transcript.apply_changes(workspace, 'a', 'r', [])\n",
            CLEAN_SNAP_SERVICE.count("\n") + 2,
            "apply_changes",
        ),
        (
            SERVICE,
            CLEAN_SNAP_SERVICE
            + "def revert_snap(workspace):\n"
            + "    return source_transcript.restore(workspace, 'a', 'r')\n",
            CLEAN_SNAP_SERVICE.count("\n") + 2,
            "restore",
        ),
        # 적용 메서드 안에 둔 같은 이름의 함수
        (
            SERVICE,
            "class RawWorkspaceService:\n"
            "    def apply_snap(self, workspace):\n"
            "        def apply_snap(times):\n"
            "            return source_transcript.apply_changes(workspace, 'a', 'r', times)\n"
            "        return apply_snap([])\n",
            4,
            "apply_changes",
        ),
        # 앞머리만 같은 남의 이름
        (
            SERVICE,
            "class RawWorkspaceService:\n"
            "    def apply_snapshot_cleanup(self, workspace):\n"
            "        return source_transcript.restore(workspace, 'a', 'r')\n",
            3,
            "restore",
        ),
        # 정의한 모듈 안의 맨 이름 `restore` — 흔한 낱말 예외는 다른 모듈에만 있다.
        (
            "vlogsrt/project/raw/source_transcript.py",
            SNAP_OWNER + "def edit(workspace):\n    return restore(workspace, 'a', 'r')\n",
            6,
            "restore",
        ),
        # 첨자 꺼내기 — `vars(m)[…]` · `m.__dict__[…]` · `globals()[…]`
        (
            "vlogsrt/project/raw/tiering.py",
            "def _undo(m, ws):\n    return vars(m)['restore'](ws, 'a', 'r')\n",
            2,
            "restore",
        ),
        (
            "vlogsrt/project/raw/finalcut_export.py",
            "def _snap(m, ws):\n    return m.__dict__['restore'](ws, 'a', 'r')\n",
            2,
            "restore",
        ),
        (
            "vlogsrt/project/raw/source_transcript.py",
            SNAP_OWNER
            + "def edit(workspace):\n    return globals()['restore'](workspace, 'a', 'r')\n",
            6,
            "restore",
        ),
        # `*` import — 정의 모듈의 이름을 통째로 들인다.
        (
            "vlogsrt/project/raw/speech_refill.py",
            "from vlogsrt.project.raw.source_transcript import *  # noqa: F403\n",
            1,
            "*",
        ),
    ],
)
def test_the_snap_writes_have_no_side_doors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, rel: str, body: str, line: int, name: str
) -> None:
    clean = {SERVICE: CLEAN_SNAP_SERVICE, "vlogsrt/project/raw/source_transcript.py": SNAP_OWNER}
    _fake_source_tree(monkeypatch, tmp_path, clean)
    assert check_invariants.check_snap_writes_only_from_service() == []
    (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / rel).write_text(body, encoding="utf-8")
    failures = check_invariants.check_snap_writes_only_from_service()
    assert failures and all(f.startswith(f"{rel}:{line}: {name}을") for f in failures), failures


def test_the_dunder_dict_key_apply_changes_is_counted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    clean = {SERVICE: CLEAN_SNAP_SERVICE, "vlogsrt/project/raw/source_transcript.py": SNAP_OWNER}
    _fake_source_tree(monkeypatch, tmp_path, clean)
    rel = "vlogsrt/project/raw/selection_analysis.py"
    (tmp_path / rel).write_text(
        "def _snap(m, ws):\n    return m.__dict__['apply_changes'](ws, 'a', 'r', [])\n",
        encoding="utf-8",
    )
    failures = check_invariants.check_snap_writes_only_from_service()
    assert failures and all(f.startswith(f"{rel}:2: apply_changes을") for f in failures)


@pytest.mark.parametrize(
    ("body", "line"),
    [
        # 앞머리만 같은 남의 메서드 · 목록 밖의 적용 자리 · 같은 이름의 모듈 함수
        (
            "class RawWorkspaceService:\n"
            "    def apply_snapshot_tiers(self):\n"
            "        from vlogsrt.project import sync_snap\n"
            "        return sync_snap.propose\n",
            3,
        ),
        (
            "class RawWorkspaceService:\n"
            "    def revert_snap_state(self):\n"
            "        from vlogsrt.project import sync_snap\n"
            "        return sync_snap\n",
            3,
        ),
        (
            "def _apply_snap_plan(cues):\n"
            "    from vlogsrt.project import sync_snap\n"
            "    return sync_snap.propose(cues)\n",
            2,
        ),
    ],
)
def test_the_service_sites_for_the_proposal_are_an_exact_list(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, body: str, line: int
) -> None:
    _fake_source_tree(monkeypatch, tmp_path, {SERVICE: CLEAN_SNAP_SERVICE})
    assert check_invariants.check_sync_snap_in_service_only_from_snap_entries() == []
    (tmp_path / SERVICE).write_text(body, encoding="utf-8")
    failures = check_invariants.check_sync_snap_in_service_only_from_snap_entries()
    assert failures and failures[0].startswith(f"{SERVICE}:{line}: 시각 맞춤 제안을"), failures


@pytest.mark.parametrize(
    ("rel", "body", "line", "name"),
    [
        # 등급·보내기·선택 분석·지문 자리·제거·화면 그리기가 서비스를 거쳐 제안·적용을 쓰면 실패다.
        (
            "vlogsrt/project/raw/tiering.py",
            "def _x(service, root, a):\n    return service.apply_snap_preview(root, a)\n",
            2,
            "apply_snap_preview",
        ),
        (
            "vlogsrt/project/raw/finalcut_export.py",
            "def _p(scope, a):\n"
            "    return scope.service._apply_snap_plan(scope.root, a, strict=False)\n",
            2,
            "_apply_snap_plan",
        ),
        (
            "vlogsrt/project/raw/selection_analysis.py",
            "def _b(service, root, a):\n    return service.apply_snap_preview(root, a).sha256\n",
            2,
            "apply_snap_preview",
        ),
        (
            "vlogsrt/project/raw/understanding.py",
            "from vlogsrt.project.raw.service import SnapPreview\n",
            1,
            "SnapPreview",
        ),
        (
            "vlogsrt/project/edit.py",
            "def _cut(service, root, a, r):\n    return service.revert_snap(root, a, r)\n",
            2,
            "revert_snap",
        ),
        (
            "ui/ui_facade.py",
            "def _x(raw):\n    return getattr(raw, 'apply_snap')\n",
            2,
            "apply_snap",
        ),
        # 서비스의 다른 메서드도 자리다.
        (
            SERVICE,
            CLEAN_SNAP_SERVICE
            + "class Other:\n"
            + "    def tier_view(self, root, a):\n"
            + "        return self.apply_snap_preview(root, a)\n",
            CLEAN_SNAP_SERVICE.count("\n") + 3,
            "apply_snap_preview",
        ),
    ],
)
def test_only_the_service_and_the_screen_reach_the_snap_api(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, rel: str, body: str, line: int, name: str
) -> None:
    screen = (
        "def _snap_view(raw, root, sid):\n"
        "    return raw.apply_snap_preview(root, sid), raw.revert_snap_state(root, sid)\n"
    )
    clean = {SERVICE: CLEAN_SNAP_SERVICE, "ui/workspace_service.py": screen}
    _fake_source_tree(monkeypatch, tmp_path, clean)
    assert check_invariants.check_snap_api_only_from_service_and_screen() == []
    (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / rel).write_text(body, encoding="utf-8")
    (failure,) = check_invariants.check_snap_api_only_from_service_and_screen()
    assert failure.startswith(f"{rel}:{line}: {name}을") and "(D59 19h)" in failure


def test_an_unrelated_snapshot_name_is_not_the_snap_api(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    body = "def apply_snapshot_tiers(x):\n    return x.apply_snapshot\n"
    _fake_source_tree(monkeypatch, tmp_path, {"vlogsrt/project/raw/tiering.py": body})
    assert check_invariants.check_snap_api_only_from_service_and_screen() == []


def test_the_real_tree_passes_the_tightened_snap_checks() -> None:
    assert check_invariants.check_snap_api_only_from_service_and_screen() == []
    assert check_invariants.check_snap_writes_only_from_service() == []
    assert check_invariants.check_sync_snap_in_service_only_from_snap_entries() == []
    assert check_invariants.check_store_edit_stays_private() == []


STORE_EDIT_OWNER = (
    "def _store_edit(workspace, asset_id, expected_revision, payload):\n"
    "    return None\n"
    "def edit(workspace, asset_id, expected_revision, rows):\n"
    "    return _store_edit(workspace, asset_id, expected_revision, {'utterances': rows})\n"
)


@pytest.mark.parametrize(
    ("rel", "body", "line"),
    [
        # 서비스 메서드가 낮은 문을 바로 열어 시각을 바꾼 줄을 쓴다(재검증 n05).
        (
            SERVICE,
            "class RawWorkspaceService:\n"
            "    def tidy(self, workspace, rows):\n"
            "        from vlogsrt.project.raw import source_transcript\n"
            "        return source_transcript._store_edit(workspace, 'a', 'r', rows)\n",
            4,
        ),
        (
            "vlogsrt/project/raw/speech_refill.py",
            "from vlogsrt.project.raw.source_transcript import _store_edit as write\n",
            1,
        ),
        (
            "ui/workspace_service.py",
            "def save(st, ws):\n    return getattr(st, '_store_edit')(ws, 'a', 'r', {})\n",
            2,
        ),
        (
            "vlogsrt/project/raw/tiering.py",
            "def save(st, ws):\n    return vars(st)['_store_edit'](ws, 'a', 'r', {})\n",
            2,
        ),
    ],
)
def test_the_low_transcript_write_stays_inside_its_module(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, rel: str, body: str, line: int
) -> None:
    """정의 모듈 안의 공개 쓰기는 낮은 문을 열 수 있고, 밖의 어느 자리도 열지 못한다."""
    owner = "vlogsrt/project/raw/source_transcript.py"
    _fake_source_tree(monkeypatch, tmp_path, {owner: STORE_EDIT_OWNER})
    assert check_invariants.check_store_edit_stays_private() == []
    (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / rel).write_text(body, encoding="utf-8")
    (failure,) = check_invariants.check_store_edit_stays_private()
    assert failure.startswith(f"{rel}:{line}: _store_edit을") and "(D59 19h)" in failure


def test_a_docstring_naming_the_low_write_is_not_a_reference(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    body = 'def check():\n    """`_store_edit`의 `guard` 안에서 다시 본다."""\n'
    _fake_source_tree(monkeypatch, tmp_path, {SERVICE: body})
    assert check_invariants.check_store_edit_stays_private() == []


# ── 읽기 범위(W-23 세션 B, D63) — 범위는 화면 진입점만 쓰기 뒤에 연다 ──────────────

READ_SCOPE_SCREEN = (
    "from vlogsrt.project.raw import read_scope\n"
    "class RawWorkspaceUi:\n"
    "    def _capcut_result(self, root):\n"
    "        with read_scope.open():\n"
    "            return root\n"
    "    def capcut_tick_view(self, signature):\n"
    "        with read_scope.open():\n"
    "            return signature\n"
    "    def visual_workbench_action(self, payload, kind):\n"
    "        with read_scope.open():\n"
    "            return kind\n"
    "    def visual_workbench_render(self, root):\n"
    "        with read_scope.open():\n"
    "            return root\n"
)


@pytest.mark.parametrize(
    ("rel", "body", "line"),
    [
        # 화면의 다른 처리기가 쓰기 앞에서 범위를 열면 쓰기를 넘어 기억한다.
        (
            "ui/workspace_service.py",
            READ_SCOPE_SCREEN + "    def capcut_edit_view(self, signature):\n"
            "        with read_scope.open():\n"
            "            return signature\n",
            16,
        ),
        # 서비스·작업 본문은 범위를 열지 않는다 — 작업 스레드는 범위 없이 돈다.
        (
            SERVICE,
            "from vlogsrt.project.raw import read_scope\n"
            "def tick(root):\n"
            "    with read_scope.open():\n"
            "        return root\n",
            3,
        ),
        # 화면이 서비스 손잡이로 열어도 같은 문이다 — 진입점이 아닌 처리기다.
        (
            "ui/workspace_service.py",
            READ_SCOPE_SCREEN + "    def capcut_confirm_auto_view(self, signature):\n"
            "        with raw_service.read_scope.open():\n"
            "            return signature\n",
            16,
        ),
        # 이름으로 가져와 다른 이름으로 열어도 같은 문이다.
        ("ui/ui_facade.py", "from vlogsrt.project.raw.read_scope import open as scoped\n", 1),
        # 담아 두었다가 여는 것도 자리다.
        (
            "ui/ui_facade.py",
            "from vlogsrt.project.raw import read_scope\nOPEN = read_scope.open\n",
            2,
        ),
    ],
)
def test_only_the_screen_entry_points_open_a_read_scope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, rel: str, body: str, line: int
) -> None:
    _fake_source_tree(monkeypatch, tmp_path, {"ui/workspace_service.py": READ_SCOPE_SCREEN})
    assert check_invariants.check_read_scope_only_at_entry_points() == []
    (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / rel).write_text(body, encoding="utf-8")
    (failure,) = check_invariants.check_read_scope_only_at_entry_points()
    assert failure.startswith(f"{rel}:{line}: ") and "(D63)" in failure


def test_reading_through_a_scope_is_not_opening_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    body = (
        "from vlogsrt.project.raw import read_scope\n"
        "def load(workspace, asset_id):\n"
        "    return read_scope.memo(('load', asset_id), lambda: None), read_scope.current()\n"
    )
    _fake_source_tree(monkeypatch, tmp_path, {"vlogsrt/project/raw/source_transcript.py": body})
    assert check_invariants.check_read_scope_only_at_entry_points() == []


def test_the_one_second_status_tick_is_a_screen_entry_point(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """W-23 세션 C — 1초 상태 틱은 쓰지 않는 틱이라 제 범위를 연다(진입점 다섯째)."""
    body = READ_SCOPE_SCREEN + (
        "    def status_tick(self, signature):\n"
        "        with raw_service.read_scope.open():\n"
        "            return signature\n"
    )
    _fake_source_tree(monkeypatch, tmp_path, {"ui/workspace_service.py": body})
    assert check_invariants.check_read_scope_only_at_entry_points() == []
    assert "RawWorkspaceUi.status_tick" in check_invariants.READ_SCOPE_ENTRY_POINTS


def test_the_real_tree_opens_read_scopes_only_at_the_screen_entry_points() -> None:
    assert check_invariants.check_read_scope_only_at_entry_points() == []


# ── D92: Gemini 시간 요청에도 힌트가 실리지 않는다(규칙 6) ─────────────────────

_GEMINI_TIMING_CLEAN = """
class GeminiTranscriptionRequest:
    model: str
    language_codes: tuple[str, ...]


def _transcription_body(request, audio):
    return {"model": request.model, "store": False, "input": []}


def _transcription_config(request):
    return {"language_codes": [], "mode": {"type": "verbatim"}}
"""


@pytest.mark.parametrize(
    "dirty",
    [
        _GEMINI_TIMING_CLEAN.replace("    model: str", "    model: str\n    prompt: str"),
        _GEMINI_TIMING_CLEAN.replace('"store": False', '"custom_vocabulary": []'),
        _GEMINI_TIMING_CLEAN.replace('"mode":', '"system_instruction": "", "mode":'),
    ],
)
def test_a_hint_in_the_gemini_timing_request_is_caught(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, dirty: str
) -> None:
    _fake_source_tree(monkeypatch, tmp_path, {"vlogsrt/transcribe.py": _GEMINI_TIMING_CLEAN})
    assert check_invariants.check_gemini_timing_request_lane() == []
    (tmp_path / "vlogsrt" / "transcribe.py").write_text(dirty, encoding="utf-8")
    assert check_invariants.check_gemini_timing_request_lane()


def test_a_missing_gemini_timing_builder_is_not_a_silent_pass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    body = _GEMINI_TIMING_CLEAN.replace("def _transcription_config", "def _renamed")
    _fake_source_tree(monkeypatch, tmp_path, {"vlogsrt/transcribe.py": body})
    (failure,) = check_invariants.check_gemini_timing_request_lane()
    assert "_transcription_config" in failure


def test_the_real_gemini_timing_request_has_no_hint() -> None:
    assert check_invariants.check_gemini_timing_request_lane() == []


# ── D92: Gemini 단어 해석·묶기는 순수하다 ─────────────────────────────────────


@pytest.mark.parametrize("module", ["gemini_transcribe_parse.py", "word_cues.py"])
@pytest.mark.parametrize(
    "statement",
    [
        "from vlogsrt.project.raw import source_text",
        "from vlogsrt.project.raw.retext import plan",
        "from vlogsrt import transcribe",
        "import sqlite3",
        "BODY = Path('x').read_text()",
    ],
)
def test_word_cue_layers_stay_pure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, module: str, statement: str
) -> None:
    clean = "from fractions import Fraction\nfrom pathlib import Path\n"
    files = {f"vlogsrt/project/raw/{name}": clean for name in check_invariants.WORD_CUES_MODULES}
    _fake_source_tree(monkeypatch, tmp_path, files)
    assert check_invariants.check_word_cues_stays_pure() == []
    target = tmp_path / "vlogsrt" / "project" / "raw" / module
    target.write_text(clean + statement + "\n", encoding="utf-8")
    failures = check_invariants.check_word_cues_stays_pure()
    assert failures and all(line.startswith(module) for line in failures)
    target.unlink()
    assert check_invariants.check_word_cues_stays_pure() == [
        f"vlogsrt/project/raw/{module}: 단어 시각 계층이 사라졌다 (D92)"
    ]


def test_the_real_word_cue_layers_are_pure() -> None:
    assert check_invariants.check_word_cues_stays_pure() == []


def test_a_hint_in_the_gemini_timing_builder_is_caught(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    clean = (
        "def gemini_timing_request(chunk, *, settings):\n"
        "    return Request(model='m', chunk=chunk, language_codes=('ko-KR',))\n"
    )
    _fake_source_tree(monkeypatch, tmp_path, {CAPTION_PROVIDER: clean})
    assert check_invariants.check_gemini_timing_builder_lane() == []
    dirty = clean.replace("language_codes=", "prompt='여행', language_codes=")
    (tmp_path / CAPTION_PROVIDER).write_text(dirty, encoding="utf-8")
    (failure,) = check_invariants.check_gemini_timing_builder_lane()
    assert "prompt" in failure
    (tmp_path / CAPTION_PROVIDER).write_text("def other():\n    pass\n", encoding="utf-8")
    assert check_invariants.check_gemini_timing_builder_lane()


def test_the_real_gemini_timing_builder_has_no_hint() -> None:
    assert check_invariants.check_gemini_timing_builder_lane() == []


WORD_TEXT_CLEAN = (
    "from fractions import Fraction\nclass LineText:\n    line_id: str\n    text: str\n"
)


@pytest.mark.parametrize(
    "statement",
    [
        "from vlogsrt.project.raw import word_cues",
        "from vlogsrt.project.raw import source_text",
        "import sqlite3",
        "from vlogsrt import refine",
        "Path('x').write_text('y')",
    ],
)
def test_word_text_impurity_is_caught(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, statement: str
) -> None:
    """W-35a(D95) — 단어 배정은 전송·전사·다른 귀·DB·파일·조판 계층을 모른다."""
    files = {"vlogsrt/project/raw/word_text.py": WORD_TEXT_CLEAN}
    _fake_source_tree(monkeypatch, tmp_path, files)
    assert check_invariants.check_word_text_stays_pure() == []
    target = tmp_path / "vlogsrt" / "project" / "raw" / "word_text.py"
    target.write_text(WORD_TEXT_CLEAN + statement + "\n", encoding="utf-8")
    failures = check_invariants.check_word_text_stays_pure()
    assert failures and all(line.startswith("word_text.py") for line in failures)


def test_a_time_field_on_the_line_plan_and_a_missing_layer_are_caught(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """줄 계획에 시각 칸이 생기면 단어 시각이 줄의 시각이 된다(규칙 1·3, D95)."""
    files = {"vlogsrt/project/raw/word_text.py": WORD_TEXT_CLEAN + "    start: Fraction\n"}
    _fake_source_tree(monkeypatch, tmp_path, files)
    (failure,) = check_invariants.check_word_text_stays_pure()
    assert "LineText.start" in failure
    (tmp_path / "vlogsrt" / "project" / "raw" / "word_text.py").unlink()
    assert check_invariants.check_word_text_stays_pure() == [
        "vlogsrt/project/raw/word_text.py: 단어 배정 계층이 사라졌다 (D95)"
    ]


def test_the_real_word_text_layer_is_pure() -> None:
    assert check_invariants.check_word_text_stays_pure() == []
