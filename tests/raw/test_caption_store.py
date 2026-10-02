"""A7-3 검토 저장소 — no-replace 게시·연쇄 검증·경로 성분·멱등성의 계약 검사.

순수 파일 선로의 시험이다: 도메인 의미는 넣지 않고, 같은 신원의 바꿔치기가
없다는 것·연쇄가 끊기면 읽지 못한다는 것·통로 탈출 성분이 애초에 못 쓴다는
것을 검사한다(§11.1·§13·§4.4).
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from vlogsrt.project.raw.caption_store import (
    AUTOMATIC_DIR,
    FINAL_DIR,
    MANUAL_DIR,
    CaptionStoreChainError,
    CaptionStoreConflict,
    CaptionStoreIntegrityError,
    CaptionStorePathError,
    CaptionStoreSchemaError,
    ReviewStore,
    canonical_json,
    require_hex_digest,
    require_store_component,
    sha256_text,
)

REV = "a" * 64
FP = "f" * 64


def a_content(marker: str) -> dict[str, object]:
    return {"marker": marker, "cues": []}


AUTO = sha256_text(canonical_json(a_content("v1")))


def a_manual_body(sequence: int, parent_sha: str) -> str:
    return canonical_json(
        {
            "schema_version": "raw-caption-manual/v1",
            "sequence": sequence,
            "parent_sha256": parent_sha,
        }
    )


@pytest.fixture
def store(tmp_path: Path) -> ReviewStore:
    return ReviewStore(tmp_path / "root")


def frozen_store(store: ReviewStore) -> ReviewStore:
    """동결된 저장소 — 자동 내용 해시는 AUTO다."""
    store.freeze_automatic(
        final_video_revision=REV, run_id="run-1", fingerprint=FP, content=a_content("v1")
    )
    return store


# ─────────────────────────────────────────────────────────────
#  동결 — 멱등·같은 신원 다른 내용 거부
# ─────────────────────────────────────────────────────────────


def test_freeze_same_bytes_is_idempotent(store: ReviewStore) -> None:
    """같은 신원·같은 내용의 다시 동결은 멱등이다 — 파일도 해시도 그대로."""
    first = store.freeze_automatic(
        final_video_revision=REV, run_id="run-1", fingerprint=FP, content=a_content("v1")
    )
    body = first.path.read_text(encoding="utf-8")
    second = store.freeze_automatic(
        final_video_revision=REV, run_id="run-1", fingerprint=FP, content=a_content("v1")
    )
    assert second.path == first.path
    assert second.path.read_text(encoding="utf-8") == body
    assert second.auto_content_sha256 == first.auto_content_sha256


def test_freeze_different_content_same_identity_is_refused(store: ReviewStore) -> None:
    """같은 신원에 다른 자동 내용 — 몰래 바꾸는 길이 없다(§11.1)."""
    frozen_store(store)
    with pytest.raises(CaptionStoreConflict):
        store.freeze_automatic(
            final_video_revision=REV, run_id="run-1", fingerprint=FP, content=a_content("v2")
        )
    loaded = store.load_automatic(final_video_revision=REV, run_id="run-1", fingerprint=FP)
    assert loaded.content == a_content("v1")  # 옛 내용이 산 채로 남는다


def test_load_detects_tampered_content_and_unknown_schema(store: ReviewStore) -> None:
    """낡은/손상 기록 — 내용 해시 어긋남·모르는 스키마·모르는 키는 못 들어온다."""
    frozen = store.freeze_automatic(
        final_video_revision=REV, run_id="run-1", fingerprint=FP, content=a_content("v1")
    )
    body = json.loads(frozen.path.read_text(encoding="utf-8"))
    body["content"]["marker"] = "tampered"
    frozen.path.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(CaptionStoreIntegrityError):
        store.load_automatic(final_video_revision=REV, run_id="run-1", fingerprint=FP)
    body["schema"] = "raw-caption-automatic/v2"
    frozen.path.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(CaptionStoreSchemaError):
        store.load_automatic(final_video_revision=REV, run_id="run-1", fingerprint=FP)
    body["schema"] = "raw-caption-automatic/v1"
    body["extra"] = 1
    frozen.path.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(CaptionStoreSchemaError):
        store.load_automatic(final_video_revision=REV, run_id="run-1", fingerprint=FP)


def test_find_automatic_returns_none_without_snapshot(store: ReviewStore) -> None:
    assert store.find_automatic(final_video_revision=REV, run_id="run-9", fingerprint=FP) is None


# ─────────────────────────────────────────────────────────────
#  경로 성분 — 통로 탈출은 애초에 못 쓴다
# ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "bad", ["../evil", "a/b", "a\\b", "", ".", "..", "lead slash", "x" * 81, "한글run"]
)
def test_run_id_path_traversal_is_refused(store: ReviewStore, bad: str) -> None:
    """나쁜 run_id는 자리 이름이 되기 전에 거부된다(§4.4)."""
    with pytest.raises(CaptionStorePathError):
        store.store_key(REV, bad, FP)


def test_identity_fields_must_be_hex64(store: ReviewStore) -> None:
    with pytest.raises(CaptionStorePathError):
        store.store_key("not-hex", "run-1", FP)
    with pytest.raises(CaptionStorePathError):
        require_hex_digest("A" * 64, "fingerprint")  # 대문자는 못 쓴다
    assert require_store_component("run-1", "run_id") == "run-1"


# ─────────────────────────────────────────────────────────────
#  수동 연쇄 — 부모 해시가 증명되지 않으면 못 읽는다
# ─────────────────────────────────────────────────────────────


def test_manual_chain_requires_parent_proof(store: ReviewStore) -> None:
    """리비전 0은 스냅샷에, n은 부모 파일 바이트에 묶인다 — 끊기면 ChainError."""
    frozen_store(store)
    store.save_manual(
        final_video_revision=REV,
        run_id="run-1",
        fingerprint=FP,
        auto_content_sha256=AUTO,
        revision=0,
        body=a_manual_body(0, AUTO),
        parent_sha256=AUTO,
    )
    with pytest.raises(CaptionStoreChainError):  # 부모 해시가 어긋난다
        store.save_manual(
            final_video_revision=REV,
            run_id="run-1",
            fingerprint=FP,
            auto_content_sha256=AUTO,
            revision=1,
            body=a_manual_body(1, "b" * 64),
            parent_sha256="b" * 64,
        )
    save_head(store)  # 옳은 부모 해시로 리비전 1을 저장한다


def save_head(store: ReviewStore) -> str:
    parent = sha256_text(a_manual_body(0, AUTO))
    head = a_manual_body(1, parent)
    store.save_manual(
        final_video_revision=REV,
        run_id="run-1",
        fingerprint=FP,
        auto_content_sha256=AUTO,
        revision=1,
        body=head,
        parent_sha256=parent,
    )
    return head


def test_manual_save_is_idempotent_and_conflicts_on_different_bytes(
    store: ReviewStore,
) -> None:
    """같은 바이트 다시 저장은 멱등, 다른 바이트는 거부다."""
    frozen_store(store)
    store.save_manual(
        final_video_revision=REV,
        run_id="run-1",
        fingerprint=FP,
        auto_content_sha256=AUTO,
        revision=0,
        body=a_manual_body(0, AUTO),
        parent_sha256=AUTO,
    )
    head = save_head(store)
    assert store.manual_head(
        final_video_revision=REV, run_id="run-1", fingerprint=FP, auto_content_sha256=AUTO
    ) == (1, head)
    save_head(store)  # 멱등
    with pytest.raises(CaptionStoreConflict):
        store.save_manual(
            final_video_revision=REV,
            run_id="run-1",
            fingerprint=FP,
            auto_content_sha256=AUTO,
            revision=1,
            body=a_manual_body(1, "b" * 64),
            parent_sha256=sha256_text(a_manual_body(0, AUTO)),
        )


def test_manual_head_detects_hole_and_missing_snapshot(store: ReviewStore) -> None:
    """연쇄 구멍과 스냅샷 없는 기본 리비전 — 머리 읽기가 닫는다."""
    assert (
        store.manual_head(
            final_video_revision=REV, run_id="run-1", fingerprint=FP, auto_content_sha256=AUTO
        )
        is None
    )
    frozen_store(store)
    store.save_manual(
        final_video_revision=REV,
        run_id="run-1",
        fingerprint=FP,
        auto_content_sha256=AUTO,
        revision=0,
        body=a_manual_body(0, AUTO),
        parent_sha256=AUTO,
    )
    head = a_manual_body(1, sha256_text(a_manual_body(0, AUTO)))
    store.save_manual(
        final_video_revision=REV,
        run_id="run-1",
        fingerprint=FP,
        auto_content_sha256=AUTO,
        revision=1,
        body=head,
        parent_sha256=sha256_text(a_manual_body(0, AUTO)),
    )
    (store.root / "manual" / store.store_key(REV, "run-1", FP) / "rev-0000.json").unlink()
    with pytest.raises(CaptionStoreChainError):
        store.manual_head(
            final_video_revision=REV, run_id="run-1", fingerprint=FP, auto_content_sha256=AUTO
        )


# ─────────────────────────────────────────────────────────────
#  확정 게시 — no-replace·병행·중간 실패 보존
# ─────────────────────────────────────────────────────────────


def test_publish_final_no_replace_and_conflict(store: ReviewStore) -> None:
    """같은 token 같은 바이트는 멱등, 다른 바이트는 거부 — 덮어쓰기가 없다."""
    frozen_store(store)
    files = [("final.ko.srt", "1\n00:00:00,500 --> 00:00:02,500\n인사\n")]
    directory = store.publish_final(
        final_video_revision=REV, run_id="run-1", fingerprint=FP, token="r0001-abc", files=files
    )
    body = (directory / "final.ko.srt").read_text(encoding="utf-8")
    store.publish_final(
        final_video_revision=REV, run_id="run-1", fingerprint=FP, token="r0001-abc", files=files
    )
    assert (directory / "final.ko.srt").read_text(encoding="utf-8") == body
    with pytest.raises(CaptionStoreConflict):
        store.publish_final(
            final_video_revision=REV,
            run_id="run-1",
            fingerprint=FP,
            token="r0001-abc",
            files=[("final.ko.srt", "different bytes")],
        )
    assert (directory / "final.ko.srt").read_text(encoding="utf-8") == body


def test_publish_final_midfailure_leaves_old_final_and_no_manifest(
    store: ReviewStore, tmp_path: Path
) -> None:
    """게시 도중 거부 — 이미 게시된 다른 확정은 무사하고 반쪽 manifest는 없다."""
    frozen_store(store)
    old = store.publish_final(
        final_video_revision=REV,
        run_id="run-1",
        fingerprint=FP,
        token="r0001-old",
        files=[("final.ko.srt", "old bytes"), ("manifest.json", '{"schema": "x"}')],
    )
    broken = store.final_dir(
        final_video_revision=REV, run_id="run-1", fingerprint=FP, token="r0002-new"
    )
    broken.mkdir(parents=True)
    (broken / "final.ko.srt").write_text("leftover from another run", encoding="utf-8")
    with pytest.raises(CaptionStoreConflict):
        store.publish_final(
            final_video_revision=REV,
            run_id="run-1",
            fingerprint=FP,
            token="r0002-new",
            files=[("final.ko.srt", "new bytes"), ("manifest.json", '{"schema": "y"}')],
        )
    assert (old / "final.ko.srt").read_text(encoding="utf-8") == "old bytes"
    assert not (broken / "manifest.json").exists()  # 반쪽 완성은 노출되지 않는다
    assert not list(broken.glob(".*.tmp-*"))  # 임시 파일은 정리됐다


def test_concurrent_publications_do_not_clobber(store: ReviewStore) -> None:
    """병행 게시 — 서로 다른 자리는 각자 나가고 같은 자리 같은 바이트는 멱등이다."""
    frozen_store(store)
    shared = [("final.ko.srt", "shared bytes")]

    def publish(token: str) -> Path:
        return store.publish_final(
            final_video_revision=REV, run_id="run-1", fingerprint=FP, token=token, files=shared
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        dirs = list(
            pool.map(
                publish,
                ["r0001-a", "r0002-b", "r0001-a", "r0001-a"],  # 같은 token 병행 포함
            )
        )
    for directory in dirs:
        assert (directory / "final.ko.srt").read_text(encoding="utf-8") == "shared bytes"
    assert len({d.name for d in dirs}) == 2  # 자리는 token 수만큼이다


# ─────────────────────────────────────────────────────────────
#  심볼릭 링크 — 따르지 않고 거부한다, 밖은 무사하다
# ─────────────────────────────────────────────────────────────


def test_symlinked_entries_are_refused_not_followed(store: ReviewStore, tmp_path: Path) -> None:
    """저장소 밖을 겨냥하는 심볼릭 링크 성분 — 게시·읽기·머리 모으기가 닫는다(§4.4)."""
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "keep.txt"
    sentinel.write_text("밖은 무사하다", encoding="utf-8")
    key = store.store_key(REV, "run-1", FP)
    for kind, probe in (
        (FINAL_DIR, "publish"),
        (MANUAL_DIR, "manual"),
        (AUTOMATIC_DIR, "automatic"),
    ):
        (store.root / kind).mkdir(parents=True)
        (store.root / kind / key).symlink_to(outside, target_is_directory=True)
        if probe == "publish":
            with pytest.raises(CaptionStorePathError):  # mkdir 전에 성분을 짚는다
                store.publish_final(
                    final_video_revision=REV,
                    run_id="run-1",
                    fingerprint=FP,
                    token="r0001-x",
                    files=[("final.ko.srt", "x")],
                )
        elif probe == "manual":
            with pytest.raises(CaptionStorePathError):
                store.manual_head(
                    final_video_revision=REV,
                    run_id="run-1",
                    fingerprint=FP,
                    auto_content_sha256=AUTO,
                )
            with pytest.raises(CaptionStorePathError):
                store.load_manual(
                    final_video_revision=REV, run_id="run-1", fingerprint=FP, revision=0
                )
        else:
            with pytest.raises(CaptionStorePathError):
                store.find_automatic(final_video_revision=REV, run_id="run-1", fingerprint=FP)
        (store.root / kind / key).unlink()
    assert [entry.name for entry in outside.iterdir()] == ["keep.txt"]  # 밖은 무사하다

    # 심볼릭 링크 자리(token)와 심볼릭 링크 파일(rev)도 같은 선에서 거부다
    frozen_store(store)
    final_key = store.root / FINAL_DIR / key
    final_key.mkdir(parents=True)
    (final_key / "r0001-x").symlink_to(outside, target_is_directory=True)
    with pytest.raises(CaptionStorePathError):
        store.publish_final(
            final_video_revision=REV,
            run_id="run-1",
            fingerprint=FP,
            token="r0001-x",
            files=[("final.ko.srt", "x")],
        )
    (final_key / "r0001-x").unlink()
    store.save_manual(
        final_video_revision=REV,
        run_id="run-1",
        fingerprint=FP,
        auto_content_sha256=AUTO,
        revision=0,
        body=a_manual_body(0, AUTO),
        parent_sha256=AUTO,
    )
    save_head(store)
    rev_file = store.root / MANUAL_DIR / key / "rev-0001.json"
    rev_file.unlink()
    rev_file.symlink_to(sentinel)
    with pytest.raises(CaptionStorePathError):
        store.load_manual(final_video_revision=REV, run_id="run-1", fingerprint=FP, revision=1)
    rev_file.unlink()
    directory = store.publish_final(  # 링크를 치우면 정상 게시는 그대로 열린다
        final_video_revision=REV, run_id="run-1", fingerprint=FP, token="r0001-ok", files=[]
    )
    assert directory.is_dir() and sentinel.read_text(encoding="utf-8") == "밖은 무사하다"
