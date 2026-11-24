"""선택기 기반 프로젝트·가져오기 화면 — 네이티브 선택기 계약과 실제 Blocks 배선.

생산 코드에 배선 장부를 두지 않는다 — 이 묶음은 **실제 gradio Blocks 의존성**
(`app.config`의 dependencies·`app.fns`)에서 단추→처리기→입출력을 읽어 증명한다.
선택기 자체는 주입 가짜로, FCP 라이브러리는 실제 규격(임의 이름 이벤트 +
`CurrentVersion.fcpevent`) 합성으로 세운다. 경로 입력 칸은 전부 읽기전용이어야
한다 — 경로는 선택기로만 정해진다.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

pytest.importorskip(
    "gradio",
    reason="웹 UI extra(gradio)가 없다 — ui extra를 설치하면 이 묶음이 활성화된다",
    exc_type=ImportError,
)
pytest.importorskip(
    "fastapi",
    reason="웹 UI extra(fastapi)가 없다 — ui extra를 설치하면 이 묶음이 활성화된다",
    exc_type=ImportError,
)

import gradio as gr  # noqa: E402

from ui import chooser as ui_chooser  # noqa: E402
from ui import workspace_service  # noqa: E402
from vlogsrt.project.raw import service as raw_service  # noqa: E402
from vlogsrt.project.raw.models import JobRecord  # noqa: E402

TERMINAL = frozenset({"succeeded", "failed", "cancelled"})


class _ScriptedChooser:
    """가짜 네이티브 선택기 — 응답을 순서대로 돌려주고 예외도 흉내 낸다."""

    def __init__(self, *responses: object) -> None:
        self._responses = list(responses)
        self.calls: list[str] = []

    def __call__(self, kind: str) -> str | None:
        self.calls.append(kind)
        if not self._responses:
            raise AssertionError(f"예상 밖의 선택기 호출이다: {kind}")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item if item is None else str(item)


def _fake_inspect(path: Path) -> raw_service.SourceInspection:
    return raw_service.SourceInspection(
        duration_seconds=2.0, width=320, height=180, audio_streams=1, capture_time=None
    )


def _server(tmp_path: Path, *responses: object) -> workspace_service.RawWorkspaceUi:
    return workspace_service.RawWorkspaceUi(
        port=7860,
        state_path=tmp_path / "state.json",
        inspector=_fake_inspect,
        path_chooser=_ScriptedChooser(*responses),
    )


def _fcp_library(tmp_path: Path) -> Path:
    """합성 FCP 라이브러리(실제 규격) — 임의 이름 이벤트 + CurrentVersion.fcpevent."""
    library = tmp_path / "제주.fcpbundle"
    media = library / "2026-10-07" / "CurrentVersion.fcpevent"
    original = media / "Original Media"
    proxy = media / "Transcoded Media" / "Proxy Media"
    original.mkdir(parents=True)
    proxy.mkdir(parents=True)
    (original / "A001.MP4").write_bytes(b"original-a001")
    (proxy / "A001.mov").write_bytes(b"proxy-a001")
    external = tmp_path / "외장" / "ext-clip.mov"
    external.parent.mkdir(parents=True)
    external.write_bytes(b"external-original")
    (original / "ext-clip.mov").symlink_to(external)
    (original / "gone.mov").symlink_to(tmp_path / "없는-파일.mov")
    return library


def _wait_terminal(server: workspace_service.RawWorkspaceUi, root: Path, job_id: str) -> JobRecord:
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        record = server.raw.get_job(root, job_id)
        if record.status in TERMINAL:
            return record
        time.sleep(0.01)
    return server.raw.get_job(root, job_id)


# ── 기존 프로젝트 열기 — 선택기 성공·취소·오류 ────────────────────────────────


def test_workspace_chooser_opens_existing_folder_workspace(tmp_path: Path) -> None:
    """폴더를 고르면 작업공간이 열리고 경로 표시가 그 자리를 가리킨다."""
    created = _server(tmp_path)
    created.create_workspace_view("A", str(tmp_path / "a"))
    server = _server(tmp_path, str(tmp_path / "a"))
    view = server.open_project_chooser_view()
    assert server.current_signature() is not None
    assert view[-2] == str(tmp_path / "a")  # 읽기전용 경로 표시
    assert server._path_chooser.calls == [ui_chooser.WORKSPACE_KIND]
    _ = created


def test_workspace_chooser_cancel_preserves_current_state(tmp_path: Path) -> None:
    """취소는 어떤 상태도 바꾸지 않는다 — 작업공간·조사 제안이 그대로다."""
    server = _server(tmp_path)
    server.create_workspace_view("A", str(tmp_path / "a"))
    signature = server.current_signature()
    card = tmp_path / "card"
    card.mkdir()
    (card / "DJI_0001.MP4").write_bytes(b"one")
    server.ingest_scan_view(signature, str(card))
    assert server._ingest_proposal is not None

    server._path_chooser._responses = [None]  # type: ignore[attr-defined]
    with pytest.raises(gr.Error, match="취소"):
        server.open_project_chooser_view()
    assert server.current_signature() == signature
    assert server.workspace_root() == (tmp_path / "a").resolve()
    assert server._ingest_proposal is not None  # 조사 제안도 그대로


def test_chooser_error_is_a_screen_error_and_preserves_state(tmp_path: Path) -> None:
    """선택기 오류는 화면 오류로 옮겨 오고 상태는 그대로다."""
    server = _server(tmp_path)
    server.create_workspace_view("A", str(tmp_path / "a"))
    signature = server.current_signature()
    server._path_chooser._responses = [ui_chooser.ChooserError("대화상자 붕괴")]  # type: ignore[attr-defined]
    with pytest.raises(gr.Error, match="대화상자 붕괴"):
        server.open_project_chooser_view()
    assert server.current_signature() == signature


def test_chooser_plain_file_is_rejected_meaningfully(tmp_path: Path) -> None:
    """파일 하나는 작업공간이 아니다 — 안내와 함께 거절한다(조용한 성공 아님)."""
    server = _server(tmp_path)
    plain = tmp_path / "final.mov"
    plain.write_bytes(b"x")
    server._path_chooser._responses = [str(plain)]  # type: ignore[attr-defined]
    with pytest.raises(gr.Error, match="폴더 또는 Final Cut 라이브러리"):
        server.open_project_chooser_view()
    assert server.workspace_root() is None


# ── FCP 라이브러리 선택 — 관리 작업공간·중복 없는 조사·역할 선택 ──────────────


def test_fcpbundle_choice_opens_managed_workspace_with_deduped_scan(tmp_path: Path) -> None:
    """.fcpbundle을 고르면 라이브러리 밖 관리 작업공간이 열리고 원본·프록시가 한 클립으로 묶인다."""
    library = _fcp_library(tmp_path)
    server = _server(tmp_path, str(library), str(library))
    view = server.open_project_chooser_view()
    root = server.workspace_root()
    assert root is not None
    assert root == (tmp_path / "제주-vlogsrt").resolve()  # 라이브러리 밖 안정 자리
    assert not root.is_relative_to(library)
    assert view[-2] == str(root)
    assert view[-1] == str(library.resolve())  # 출처 표시 = 라이브러리 경로(읽기전용)
    ingest_md = view[5]
    assert "FCP 라이브러리" in ingest_md
    assert "끊어진" not in ingest_md or "gone.mov" in ingest_md  # 끊어진 링크가 보인다
    assert server._ingest_proposal is not None
    assert len(server._ingest_proposal.order) == 2  # A001 원본·ext-clip — 프록시 중복 없음
    assert server._fcp_library == library.resolve()

    # 다시 선택하면 같은 작업공간을 이어 연다(두 번째가 생기지 않는다).
    first_signature = server.current_signature()
    again = server.open_project_chooser_view()
    assert server.workspace_root() == root
    assert again[1] == first_signature


def test_fcp_role_rescan_swaps_the_proposal(tmp_path: Path) -> None:
    """역할을 프록시로 바꿔 다시 조사하면 프록시 파일이 제안에 들어간다."""
    library = _fcp_library(tmp_path)
    server = _server(tmp_path, str(library))
    server.open_project_chooser_view()
    signature = server.current_signature()
    markdown = server.ingest_scan_view(signature, str(library), raw_service.FCP_ROLE_PROXY)
    assert "proxy" in markdown
    assert server._ingest_proposal is not None
    chosen = {
        Path(candidate.source_root).name + "/" + candidate.source_relative_path
        for candidate in server._ingest_proposal.order
    }
    assert any("Proxy Media" in item for item in chosen)  # A001 은 프록시 파일로
    assert any(item.endswith("ext-clip.mov") for item in chosen)  # 원본만 있는 클립은 그대로


def test_fcp_rescan_from_view_outputs_through_confirm_and_copy(tmp_path: Path) -> None:
    """열기 보기의 실제 출력값으로 역할 재조사 → 확정 → 복사까지 간다.

    조사 단추는 읽기전용 출처 표시를 입력으로 읽는다 — 열기 보기가 라이브러리
    경로를 내리지 않으면(또는 빈 입력이 서버 라이브러리로 흐르지 않으면) 역할을
    바꿔 누를 길이 없다. 실제 보기 출력(서명·출처 표시)을 그대로 다시 넣어
    프록시 제안을 확정·복사한다.
    """
    library = _fcp_library(tmp_path)
    server = _server(tmp_path, str(library))
    view = server.open_project_chooser_view()
    signature, source_display = view[1], view[-1]
    assert source_display == str(library.resolve())  # 출처 표시가 라이브러리를 가리킨다

    # 실제 화면이 하는 대로 — 보기의 출처 표시 값으로 프록시 재조사.
    markdown = server.ingest_scan_view(signature, source_display, raw_service.FCP_ROLE_PROXY)
    assert "proxy" in markdown
    proposal = server._ingest_proposal
    assert proposal is not None
    names = {candidate.source_name for candidate in proposal.order}
    assert "A001.mov" in names  # 프록시 파일(A001.MP4가 아니라)
    # 출처 표시가 비어 있어도 서버가 이 작업공간의 라이브러리를 안다 — 막히지 않는다.
    again = server.ingest_scan_view(signature, "", raw_service.FCP_ROLE_PROXY)
    assert "proxy" in again

    server.ingest_confirm_view(signature)
    server.ingest_copy_view(signature)
    root = server.workspace_root()
    assert root is not None
    job = server.raw.list_jobs(root)[0]
    record = _wait_terminal(server, root, job.job_id)
    assert record.status == "succeeded", record.error
    assets = server.raw.list_source_assets(root)
    assert [asset.status for asset in assets] == ["ready", "ready"]
    by_name = {asset.source_name: asset for asset in assets}
    proxy = root / by_name["A001.mov"].relative_path
    assert proxy.read_bytes() == b"proxy-a001"  # 프록시 역할 그대로 복사되었다


def test_fcp_scan_failure_stays_visible_and_resumable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """조사 실패는 가져오기 패널에 그대로 보이고, 출처 표시가 남아 다시 조사할 수 있다."""
    library = _fcp_library(tmp_path)
    server = _server(tmp_path, str(library))

    def exploding(*args: object, **kwargs: object) -> None:
        raise raw_service.FcpLibraryError("폭발 — 잠시 후 다시")

    monkeypatch.setattr(server.raw, "scan_fcp_library", exploding)
    view = server.open_project_chooser_view()
    assert "조사 실패" in view[5] and "폭발" in view[5]  # 조용한 빈 성공이 아니다
    assert view[-1] == str(library.resolve())  # 출처 표시는 라이브러리를 계속 가리킨다
    signature = server.current_signature()

    monkeypatch.undo()
    markdown = server.ingest_scan_view(signature, "", raw_service.FCP_ROLE_ORIGINAL)
    assert "FCP 라이브러리" in markdown  # 빈 출처로도 다시 조사해 회복된다
    assert server._ingest_proposal is not None


# ── SD/폴더·최종 영상 선택 — 취소 보존·일반 가져오기 회귀 ─────────────────────


def test_source_folder_chooser_sets_listing_and_cancel_preserves(tmp_path: Path) -> None:
    """원본 폴더 선택기 — 목록·표시가 갱신되고, 취소하면 지금 표시가 그대로다."""
    server = _server(tmp_path)
    server.create_workspace_view("A", str(tmp_path / "a"))
    card = tmp_path / "card"
    card.mkdir()
    (card / "DJI_0001.MP4").write_bytes(b"one")
    (card / "DJI_0002.MP4").write_bytes(b"two")

    server._path_chooser._responses = [str(card)]  # type: ignore[attr-defined]
    listing_md, shown = server.source_folder_chooser_view()
    assert "DJI_0001.MP4" in listing_md and "DJI_0002.MP4" in listing_md
    assert shown == str(card)

    server._path_chooser._responses = [None]  # type: ignore[attr-defined]
    with pytest.raises(gr.Error, match="취소"):
        server.source_folder_chooser_view()
    assert server._last_listing is not None and server._last_listing.path == str(card)


def test_generic_sd_regression_through_chooser_entries(tmp_path: Path) -> None:
    """일반 SD/폴더 흐름 회귀 — 선택기 입력구로 열고·골라 조사→확정→복사까지 간다."""
    card = tmp_path / "card" / "DCIM"
    card.mkdir(parents=True)
    (card / "DJI_0002.MP4").write_bytes(b"two")
    (card / "DJI_0010.MP4").write_bytes(b"ten")
    server = _server(tmp_path, str(tmp_path / "ws"), str(card))

    view = server.create_project_chooser_view("제주")
    signature = view[1]
    root = Path(view[-2])
    assert server.workspace_root() == root

    server.source_folder_chooser_view()
    server.ingest_scan_view(signature, str(card))
    server.ingest_confirm_view(signature)
    server.ingest_copy_view(signature)
    job = server.raw.list_jobs(root)[0]
    record = _wait_terminal(server, root, job.job_id)
    assert record.status == "succeeded", record.error
    assets = server.raw.list_source_assets(root)
    assert [asset.status for asset in assets] == ["ready", "ready"]
    assert (root / assets[0].relative_path).read_bytes() == b"two"


# ── 실제 Blocks 배선 — 장부가 아니라 gradio 의존성 그 자체로 증명한다 ─────────


def _click_dep(app: gr.Blocks, component: object) -> dict:
    """실제 Blocks 의존성에서 단추의 클릭 배선을 찾는다 — 복제 장부가 아니다."""
    trigger = component._id  # type: ignore[attr-defined]  # gradio 구성요소의 내부 식별자
    for dep in app.config["dependencies"]:
        if dep["targets"] == [(trigger, "click")]:
            return dep
    raise AssertionError("클릭 배선이 없다 — 단추가 처리기에 연결되지 않았다")


def test_actual_blocks_bindings_and_readonly_paths(tmp_path: Path, local_socket: None) -> None:
    """선택기 단추의 실제 배선 — API 숨김·공유 동시성 묶음·읽기전용 경로 칸."""
    server = _server(tmp_path)
    app = server.build_blocks()

    assert server._ws_path_box is not None
    assert server._source_box is not None
    assert server._project_name_box is not None
    assert server._open_button is not None
    assert server._create_button is not None
    assert server._browse_button is not None
    assert server._scan_button is not None
    # 경로 입력 칸은 전부 읽기전용 — 이름 칸(경로 아님)은 그대로 쓸 수 있다.
    assert server._ws_path_box.interactive is False
    assert server._source_box.interactive is False
    assert server._project_name_box.interactive is not False

    def bound(dep: dict) -> object:
        return app.fns[dep["id"]].fn

    open_dep = _click_dep(app, server._open_button)
    assert open_dep["inputs"] == []  # 경로 입력 없음 — 선택기가 유일한 길이다
    assert server._ws_path_box._id in open_dep["outputs"]
    assert server._source_box._id in open_dep["outputs"]  # 전환은 낡은 출처 표시를 지운다
    assert str(open_dep["api_name"]).startswith("false")  # API 노출 없음(숨김 표기)
    assert open_dep["api_visibility"] == "private"
    assert app.fns[open_dep["id"]].concurrency_limit == 1
    assert app.fns[open_dep["id"]].concurrency_id == ui_chooser.CHOOSER_CONCURRENCY_ID
    _ = bound

    create_dep = _click_dep(app, server._create_button)
    assert create_dep["inputs"] == [server._project_name_box._id]  # 이름만 — 자리는 선택기
    assert str(create_dep["api_name"]).startswith("false")
    assert app.fns[create_dep["id"]].concurrency_id == ui_chooser.CHOOSER_CONCURRENCY_ID

    browse_dep = _click_dep(app, server._browse_button)
    assert browse_dep["inputs"] == []
    assert browse_dep["outputs"] == [server._listing_md._id, server._source_box._id]
    assert str(browse_dep["api_name"]).startswith("false")
    assert app.fns[browse_dep["id"]].concurrency_id == ui_chooser.CHOOSER_CONCURRENCY_ID

    scan_dep = _click_dep(app, server._scan_button)
    assert server._signature_state._id in scan_dep["inputs"]
    assert server._source_box._id in scan_dep["inputs"]  # 읽기전용 표시가 조사 입력이다
    assert server._fcp_role_dd._id in scan_dep["inputs"]  # 역할 선택이 조사에 함께 간다
    assert server._ingest_md._id in scan_dep["outputs"]


def test_workspace_switch_clears_stale_source_display(tmp_path: Path) -> None:
    """작업공간 전환은 낡은 출처 표시를 지운다 — 이전 프로젝트 경로가 남지 않는다."""
    library = _fcp_library(tmp_path)
    server = _server(tmp_path, str(library), str(tmp_path / "card"))
    server.open_project_chooser_view()
    card = tmp_path / "card"
    card.mkdir()
    server.source_folder_chooser_view()  # 출처 표시가 카드를 가리킨 상태에서

    server.create_workspace_view("B", str(tmp_path / "b"))
    switched = server.open_workspace_view(str(tmp_path / "b"))  # 다른 작업공간으로 전환
    assert switched[-2] == str((tmp_path / "b").resolve())
    assert switched[-1] == ""  # 낡은 출처 표시는 비었다
    assert server._ingest_proposal is None  # FCP 조사 제안도 새 상태로 시작
