"""W-37a(D97) — 작업공간 모드: 가로(롱폼) · 세로(숏폼). 라이브러리마다 하나, 사람이 바꿀 수 있다.

시험이 못 박는 것:

- 자동 모드는 불러온 영상의 **개수** 다수다(길이가 아니다). 정사각형은 가로(D66과 같은 정의), 동점은
  설정의 편(가로), 크기를 모르는 원본은 세지 않는다.
- 사람이 고른 가로·세로가 자동보다 이긴다. 저장은 작업공간 표지 하나이고 모르는 값은 자동이다.
- 모드는 Final Cut 문서 바이트를 바꾸지 않는다 — 리포트에 모드와 「켠 클립 방향과 다름」 메모만
  싣는다.
- CLI `media mode`가 모드를 인쇄하고 바꾼다.

유료 호출은 없다.
"""

from __future__ import annotations

import argparse
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from vlogsrt.project.raw import commands as media
from vlogsrt.project.raw import service as raw_service
from vlogsrt.project.raw import workspace_mode
from vlogsrt.project.raw.config import (
    MODE_AUTO,
    MODE_LANDSCAPE,
    MODE_PORTRAIT,
    WorkspaceModeSettings,
)

# D104 — whisper 응답 모양의 재생으로 재는 시험이다. 새로 듣는 엔진을 whisper로 둔다.
pytestmark = pytest.mark.usefixtures("whisper_for_new_hearings")

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")
WIDE, TALL, SQUARE = (1920, 1080), (1080, 1920), (1080, 1080)


# ── 순수 계층 ─────────────────────────────────────────────────────────────


def test_auto_follows_the_count_majority_not_the_length() -> None:
    decision = workspace_mode.decide_mode([TALL, TALL, WIDE])
    assert (decision.mode, decision.basis) == (MODE_PORTRAIT, workspace_mode.BASIS_AUTO)
    assert (decision.landscape, decision.portrait, decision.unknown) == (1, 2, 0)


def test_square_is_landscape_ties_follow_the_setting_and_unknown_sizes_are_not_counted() -> None:
    assert workspace_mode.decide_mode([SQUARE, TALL]).mode == MODE_LANDSCAPE  # 1:1 동점 → 가로
    portrait_tie = WorkspaceModeSettings(tie=MODE_PORTRAIT)
    assert workspace_mode.decide_mode([WIDE, TALL], settings=portrait_tie).mode == MODE_PORTRAIT
    unknown = workspace_mode.decide_mode([(None, None), (1080, None), TALL])
    assert (unknown.mode, unknown.unknown, unknown.portrait) == (MODE_PORTRAIT, 2, 1)
    assert workspace_mode.decide_mode([]).mode == MODE_LANDSCAPE  # 아무것도 없으면 동점의 편
    with pytest.raises(ValueError):
        WorkspaceModeSettings(tie="diagonal")


def test_a_manual_choice_wins_and_unknown_choices_are_refused() -> None:
    chosen = workspace_mode.decide_mode([WIDE, WIDE], MODE_PORTRAIT)
    assert (chosen.mode, chosen.basis) == (MODE_PORTRAIT, workspace_mode.BASIS_MANUAL)
    assert (chosen.landscape, chosen.portrait) == (2, 0)  # 자동이 고를 값도 보인다
    assert chosen.auto_mode == MODE_LANDSCAPE
    with pytest.raises(ValueError):
        workspace_mode.decide_mode([WIDE], "square")


# ── 서비스 ──────────────────────────────────────────────────────────────


def _workspace(tmp_path: Path) -> tuple[raw_service.RawWorkspaceService, Path]:
    service = raw_service.RawWorkspaceService()
    root = tmp_path / "workspace"
    service.create_workspace(root, "모드")
    return service, root


def test_the_service_stores_the_choice_as_a_workspace_marker(tmp_path: Path) -> None:
    service, root = _workspace(tmp_path)
    first = service.workspace_mode(root)
    assert (first.mode, first.basis, first.landscape + first.portrait) == (
        MODE_LANDSCAPE,
        workspace_mode.BASIS_AUTO,
        0,
    )
    assert service.set_workspace_mode(root, MODE_PORTRAIT).mode == MODE_PORTRAIT
    assert service.workspace_mode(root).basis == workspace_mode.BASIS_MANUAL
    assert service.set_workspace_mode(root, MODE_AUTO).basis == workspace_mode.BASIS_AUTO
    with pytest.raises(raw_service.RawWorkspaceError):
        service.set_workspace_mode(root, "square")
    service._workspace(root).set_meta_value(raw_service.WORKSPACE_MODE_KEY, "nonsense")
    assert service.workspace_mode(root).basis == workspace_mode.BASIS_AUTO  # 모르는 값은 자동


# ── 보내기 ──────────────────────────────────────────────────────────────


@needs_ffmpeg
def test_the_mode_never_changes_the_document_only_the_report(tmp_path: Path) -> None:
    from tests.raw.test_auto_cleanup_review_gate import _synthetic_project
    from vlogsrt.project.raw import finalcut_export

    service, root = _synthetic_project(tmp_path)
    moment = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
    auto = finalcut_export.export(service, root, now=lambda: moment)
    service.set_workspace_mode(root, MODE_PORTRAIT)
    portrait = finalcut_export.export(service, root, now=lambda: moment)
    assert auto.fcpxml.read_bytes() == portrait.fcpxml.read_bytes()
    auto_mode = json.loads(auto.report.read_text(encoding="utf-8"))["mode"]
    portrait_mode = json.loads(portrait.report.read_text(encoding="utf-8"))["mode"]
    assert (auto_mode["mode"], auto_mode["basis"], auto_mode["matches_timeline"]) == (
        MODE_LANDSCAPE,
        workspace_mode.BASIS_AUTO,
        True,
    )
    assert (portrait_mode["mode"], portrait_mode["matches_timeline"]) == (MODE_PORTRAIT, False)


# ── CLI ─────────────────────────────────────────────────────────────────


def _args(*argv: str) -> argparse.Namespace:
    import vlogsrt.project.commands as project_commands

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    project_commands.add_media_parser(sub)
    return parser.parse_args(["media", *argv])


def test_the_cli_prints_and_sets_the_mode(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    service, root = _workspace(tmp_path)
    assert media.media_command(_args("mode", "--workspace", str(root))) == 0
    assert "가로 모드" in capsys.readouterr().out
    assert media.media_command(_args("mode", "--workspace", str(root), "--set", "portrait")) == 0
    assert "세로 모드" in capsys.readouterr().out
    assert service.workspace_mode(root).mode == MODE_PORTRAIT


def test_the_drawer_radio_saves_the_mode_and_says_what_it_does(tmp_path: Path) -> None:
    pytest.importorskip("gradio", reason="웹 UI extra(gradio)가 없다", exc_type=ImportError)
    from ui import workspace_service as ws

    server = ws.RawWorkspaceUi(state_path=tmp_path / "ui-state.json")
    server.create_workspace_view("모드", str(tmp_path / "workspace"))
    assert server.workspace_mode_value() == MODE_AUTO
    assert "가로 모드(롱폼)" in server.workspace_mode_status()
    assert "자동(영상 개수가 많은 쪽)" in server.workspace_mode_status()
    status = server.workspace_mode_view(server.current_signature(), MODE_PORTRAIT)
    assert "세로 모드(숏폼)" in status and "직접 고름" in status
    assert ws.WORKSPACE_MODE_NOTES[MODE_PORTRAIT] in status
    assert "자동이라면 다른 모드입니다" in status  # 영상이 없으면 자동은 동점의 편(가로)
    assert server.workspace_mode_value() == MODE_PORTRAIT
