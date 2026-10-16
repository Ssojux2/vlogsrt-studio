import json
import subprocess
from pathlib import Path

import pytest

from ui import chooser


def test_native_host_passes_kind_and_result_as_arguments(monkeypatch):
    seen = []

    def run(args, **kwargs):
        seen.append(args)
        if args[0] == "/usr/bin/open":
            Path(args[-1]).write_text(json.dumps({"path": "/tmp/합성.fcpbundle"}))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(chooser.sys, "platform", "darwin")
    monkeypatch.setattr(chooser.subprocess, "run", run)
    assert chooser.applescript_path_chooser(chooser.WORKSPACE_KIND) == "/tmp/합성.fcpbundle"
    assert seen[0][0] == "/usr/bin/osacompile"
    assert seen[1][-3:-1] == ["--args", "workspace"]
    assert "NSProcessInfo.processInfo.arguments" in seen[0][-1]


def test_cancel_is_none_and_no_result_is_an_error(monkeypatch):
    def run(args, **kwargs):
        if args[0] == "/usr/bin/open":
            Path(args[-1]).write_text('{"path":null}')
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(chooser.sys, "platform", "darwin")
    monkeypatch.setattr(chooser.subprocess, "run", run)
    assert chooser.applescript_path_chooser("workspace") is None
    monkeypatch.setattr(
        chooser.subprocess, "run", lambda args, **kw: subprocess.CompletedProcess(args, 0, "", "")
    )
    with pytest.raises(chooser.ChooserError, match="결과 없이"):
        chooser.applescript_path_chooser("workspace")


def test_signal_exit_is_reported_instead_of_unknown():
    error = chooser._failure("workspace", subprocess.CompletedProcess([], -2, "", ""))
    assert "SIGINT" in str(error) and "종료코드 -2" in str(error)


def test_timeout_cleans_owned_host(monkeypatch):
    cleaned = []

    def run(args, **kwargs):
        if args[0] == "/usr/bin/open":
            raise subprocess.TimeoutExpired(args, 1)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(chooser.sys, "platform", "darwin")
    monkeypatch.setattr(chooser.subprocess, "run", run)
    monkeypatch.setattr(chooser, "_stop_owned_helper", lambda result, app: cleaned.append(str(app)))
    with pytest.raises(chooser.ChooserError, match="대기 시간"):
        chooser.applescript_path_chooser("workspace", timeout=1)
    assert len(cleaned) == 1


def test_direct_path_fallback_opens_workspace_and_library(tmp_path):
    from tests.raw.test_workspace_chooser_ui import _fcp_library, _server

    server = _server(tmp_path)
    workspace = tmp_path / "프로젝트 폴더"
    server.create_workspace_view("fallback", str(workspace))
    result = server.direct_project_open_view(str(workspace))
    assert result[1] == server.current_signature()
    library = _fcp_library(tmp_path)
    result = server.direct_project_open_view(str(library))
    assert result[1] == server.current_signature()
    assert server._fcp_scan is not None
    assert all(media.role == "original" for media, _ in server._fcp_scan.selected)
    assert not server._path_chooser.calls
