import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from ui import chooser


def _compile_into(args) -> None:
    """가짜 osacompile — 진짜처럼 `-o` 자리에 번들 뼈대를 남긴다."""
    if args[0] == "/usr/bin/osacompile":
        app = Path(args[args.index("-o") + 1])
        (app / "Contents" / "MacOS").mkdir(parents=True)
        (app / "Contents" / "MacOS" / "applet").write_text("synthetic")


def test_native_host_passes_kind_and_result_as_arguments(monkeypatch):
    seen = []

    def run(args, **kwargs):
        seen.append(args)
        _compile_into(args)
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
        _compile_into(args)
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
        _compile_into(args)
        if args[0] == "/usr/bin/open":
            raise subprocess.TimeoutExpired(args, 1)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(chooser.sys, "platform", "darwin")
    monkeypatch.setattr(chooser.subprocess, "run", run)
    monkeypatch.setattr(chooser, "_stop_owned_helper", lambda result, app: cleaned.append(str(app)))
    with pytest.raises(chooser.ChooserError, match="대기 시간"):
        chooser.applescript_path_chooser("workspace", timeout=1)
    assert len(cleaned) == 1


def _recording_run(seen):
    def run(args, **kwargs):
        seen.append(list(args))
        _compile_into(args)
        if args[0] == "/usr/bin/open":
            Path(args[-1]).write_text('{"path":"/tmp/폴더"}')
        return subprocess.CompletedProcess(args, 0, "", "")

    return run


def _opened_app(seen) -> Path:
    (opened,) = [args for args in seen if args[0] == "/usr/bin/open"]
    return Path(opened[opened.index("-n") + 1])


def test_the_applet_compiles_once_and_later_clicks_reuse_it(monkeypatch, private_picker_cache):
    """D106 — 누를 때마다 컴파일·새 번들이던 것이 두 번째부터 캐시의 같은 번들이다."""
    monkeypatch.setattr(chooser.sys, "platform", "darwin")
    first: list[list[str]] = []
    monkeypatch.setattr(chooser.subprocess, "run", _recording_run(first))
    assert chooser.applescript_path_chooser(chooser.FOLDER_KIND) == "/tmp/폴더"
    assert [args[0] for args in first] == ["/usr/bin/osacompile", "/usr/bin/open"]
    second: list[list[str]] = []
    monkeypatch.setattr(chooser.subprocess, "run", _recording_run(second))
    assert chooser.applescript_path_chooser(chooser.FOLDER_KIND) == "/tmp/폴더"
    assert [args[0] for args in second] == ["/usr/bin/open"]
    app = _opened_app(second)
    assert app == _opened_app(first) == chooser._picker_app_path()
    assert app.name == "vlogsrt Picker.app" and app.parent.parent == private_picker_cache
    sha = hashlib.sha256(chooser.PICKER_JXA.encode()).hexdigest()[:12]
    assert app.parent.name == f"picker-{sha}"
    assert sorted(p.name for p in private_picker_cache.iterdir()) == [f"picker-{sha}"]


def test_a_changed_script_gets_a_fresh_bundle(monkeypatch):
    monkeypatch.setattr(chooser.sys, "platform", "darwin")
    seen: list[list[str]] = []
    monkeypatch.setattr(chooser.subprocess, "run", _recording_run(seen))
    chooser.applescript_path_chooser(chooser.FOLDER_KIND)
    old_app = _opened_app(seen)
    monkeypatch.setattr(chooser, "PICKER_JXA", chooser.PICKER_JXA + "\n// 바뀐 판\n")
    seen.clear()
    chooser.applescript_path_chooser(chooser.FOLDER_KIND)
    assert [args[0] for args in seen] == ["/usr/bin/osacompile", "/usr/bin/open"]
    assert seen[0][-1].endswith("// 바뀐 판\n")
    assert _opened_app(seen) != old_app and old_app.is_dir()


def test_a_failed_compile_keeps_its_message_and_leaves_no_bundle(monkeypatch, private_picker_cache):
    def run(args, **kwargs):
        if args[0] == "/usr/bin/osacompile":
            Path(args[args.index("-o") + 1]).mkdir(parents=True)  # 반쯤 만든 번들
            return subprocess.CompletedProcess(args, 1, "", "syntax error: 합성\n")
        raise AssertionError("컴파일이 실패하면 창을 띄우지 않는다")

    monkeypatch.setattr(chooser.sys, "platform", "darwin")
    monkeypatch.setattr(chooser.subprocess, "run", run)
    with pytest.raises(
        chooser.ChooserError, match=r"^경로 선택 오류\(workspace, 종료코드 1\): syntax error: 합성$"
    ):
        chooser.applescript_path_chooser(chooser.WORKSPACE_KIND)
    assert list(private_picker_cache.iterdir()) == []


def test_the_losing_compile_adopts_the_winners_bundle(tmp_path):
    final = tmp_path / "picker-abc"
    (final / "vlogsrt Picker.app" / "Contents" / "MacOS").mkdir(parents=True)
    staged = tmp_path / ".picker-abc.tmp-1-x"
    (staged / "vlogsrt Picker.app" / "Contents" / "MacOS").mkdir(parents=True)
    chooser._adopt_compiled(staged, final)  # 이긴 쪽 번들을 쓴다 — 오류가 아니다
    assert (final / "vlogsrt Picker.app").is_dir()
    lone = tmp_path / ".picker-def.tmp-1-y"
    lone.mkdir()
    chooser._adopt_compiled(lone, tmp_path / "picker-def")
    assert (tmp_path / "picker-def").is_dir() and not lone.exists()


def test_a_broken_final_folder_is_not_adopted(tmp_path):
    final = tmp_path / "picker-abc"
    (final / "남의 것").mkdir(parents=True)  # 번들이 아닌 채 비지 않은 폴더
    staged = tmp_path / ".picker-abc.tmp-1-x"
    staged.mkdir()
    (staged / "x").write_text("x")
    with pytest.raises(OSError):
        chooser._adopt_compiled(staged, final)


def test_an_unwritable_cache_falls_back_to_a_per_call_compile(monkeypatch, tmp_path):
    blocker = tmp_path / "파일"
    blocker.write_text("폴더가 아니다")
    monkeypatch.setattr(chooser, "_picker_cache_dir", lambda: blocker / "vlogsrt")
    monkeypatch.setattr(chooser.sys, "platform", "darwin")
    seen: list[list[str]] = []
    monkeypatch.setattr(chooser.subprocess, "run", _recording_run(seen))
    assert chooser.applescript_path_chooser(chooser.FOLDER_KIND) == "/tmp/폴더"
    assert [args[0] for args in seen] == ["/usr/bin/osacompile", "/usr/bin/open"]
    assert not _opened_app(seen).is_relative_to(blocker)
    with pytest.raises(OSError):
        chooser._ensure_picker_app(chooser.FOLDER_KIND)  # 물러날 자리가 없으면 오류 그대로


def test_warming_compiles_once_and_reports_failures(monkeypatch, private_picker_cache):
    monkeypatch.setattr(chooser.sys, "platform", "darwin")
    seen: list[list[str]] = []
    monkeypatch.setattr(chooser.subprocess, "run", _recording_run(seen))
    chooser.warm_picker_app()
    chooser.warm_picker_app()
    assert [args[0] for args in seen] == ["/usr/bin/osacompile"]
    assert (chooser._picker_app_path() / "Contents" / "MacOS").is_dir()
    warnings: list[str] = []
    monkeypatch.setattr(chooser.log, "warn", warnings.append)
    monkeypatch.setattr(chooser, "PICKER_JXA", "바뀐 판")
    monkeypatch.setattr(
        chooser.subprocess,
        "run",
        lambda args, **kw: subprocess.CompletedProcess(args, 1, "", "합성 실패"),
    )
    chooser.warm_picker_app()
    assert len(warnings) == 1 and "합성 실패" in warnings[0]


def test_the_owned_host_check_matches_the_cached_bundle(monkeypatch, tmp_path):
    app = chooser._picker_app_path()
    result = tmp_path / "result.json"
    result.with_suffix(".json.pid").write_text("4242")
    killed: list[int] = []
    monkeypatch.setattr(chooser.os, "kill", lambda pid, sig: killed.append(pid))
    for command, expected in (
        (f"{app / 'Contents' / 'MacOS' / 'applet'}\n", [4242]),  # 이 운영체제의 구분자로
        ("/Applications/남의 앱.app/Contents/MacOS/other\n", []),
    ):
        killed.clear()
        monkeypatch.setattr(
            chooser.subprocess,
            "run",
            lambda args, _out=command, **kw: subprocess.CompletedProcess(args, 0, _out, ""),
        )
        chooser._stop_owned_helper(result, app)
        assert killed == expected


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
