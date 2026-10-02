"""A7-3 검토 저장소 — 불변 게시의 파일 선로(§11.7·§13·A7-3).

`docs/plans/2026-10-02-raw-video-browser-v2.md` §16 A7-3. 자동 스냅샷·수동 리비전·
확정 final이 놓이는 자리를 소유한다. 이 모듈은 **선로일 뿐**이다: 무엇을 게시할지는
`caption_review.py`가, 리비전의 의미는 `caption_manual.py`가 정한다.

지키는 선:

- **덮어쓰기 경로가 없다.** 모든 게시는 fresh 임시 파일 + `os.link` no-replace다.
  같은 자리에 같은 바이트가 이미 있으면 멱등 성공, 다른 바이트면
  `CaptionStoreConflict` — 같은 신원의 내용을 몰래 바꾸는 길이 없다(§11.1).
- **경로는 닫혀 있다.** 디렉터리 이름은 검증된 성분(해시 축약·run_id)으로만
  짓는다 — run_id·응답 ID로 저장소 밖을 겨누는 통로 자체가 없다(§4.4).
  성분이 심볼릭 링크면 **따르지 않고 거부한다** — mkdir·쓰기·읽기 어느 것도
  링크 너머를 향하지 않는다.
- **연쇄는 검증된다.** 수동 리비전은 부모 파일의 해시를 실어 저장되고, 읽을 때
  그 연쇄를 다시 잰다 — 끼어든 자료는 `CaptionStoreChainError`로 드러난다.
- **모르는 스키마·타입은 못 들어온다.** 읽는 문서는 정확한 키·스키마·타입을
  검사하고, 기록된 내용 해시와 파일 바이트가 같은지 다시 본다(§13).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

AUTOMATIC_SCHEMA = "raw-caption-automatic/v1"
"""자동 스냅샷 문서의 스키마 — 최종 해시·run·지문·내용 해시에 묶인다(§11.1)."""

MANUAL_SCHEMA = "raw-caption-manual/v1"
"""수동 리비전 문서의 스키마 — `caption_manual.serialize_revision`의 출력(§11.7)."""

AUTOMATIC_DIR = "automatic"
MANUAL_DIR = "manual"
FINAL_DIR = "final"
SNAPSHOT_NAME = "snapshot.json"

_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class CaptionStoreError(RuntimeError):
    """저장소 오류 — 메시지가 자리·신원·값을 말한다."""


class CaptionStorePathError(CaptionStoreError):
    """경로 성분이 계약 밖이다 — 통로 탈출 성분은 애초에 못 쓴다(§4.4)."""


class CaptionStoreSchemaError(CaptionStoreError):
    """문서가 계약 밖이다 — 모르는 스키마·키·타입은 못 들어온다(§13)."""


class CaptionStoreConflict(CaptionStoreError):
    """같은 신원의 자리에 다른 바이트가 있다 — 바꿔치기가 아니라 새 자리다(§11.1)."""


class CaptionStoreIntegrityError(CaptionStoreError):
    """기록된 해시와 파일 내용이 다르다 — 낡았거나 손상된 기록이다."""


class CaptionStoreChainError(CaptionStoreError):
    """리비전 연쇄가 끊겼다 — 부모를 못 증명하는 리비전은 못 읽는다."""


def require_hex_digest(value: object, field: str) -> str:
    """64자 소문자 해시만 통과 — 신원 필드의 원초적 계약."""
    if not isinstance(value, str) or not _HEX64.match(value):
        raise CaptionStorePathError(f"{field}는 64자 소문자 해시여야 한다: {value!r}")
    return value


def require_store_component(value: object, field: str) -> str:
    """디렉터리 이름 성분 — `[A-Za-z0-9][A-Za-z0-9._-]{0,79}`. `.`/`..`/구분자는 거부."""
    if not isinstance(value, str) or not _COMPONENT.match(value):
        raise CaptionStorePathError(
            f"{field}는 경로 성분으로 쓸 수 없다: {value!r} — 글자·숫자·`._-` 1~80자"
        )
    return value


def canonical_json(document: object) -> str:
    """정준 JSON — 정렬된 키·UTF-8·결정적 바이트. 같은 값은 늘 같은 bytes다."""
    return (
        json.dumps(
            document, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        + "\n"
    )


def sha256_text(body: str) -> str:
    """문서 바이트의 SHA-256 — 연쇄·내용 해시의 원천."""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class FrozenAutomatic:
    """동결된 자동 스냅샷 — 신원 넷(렌더·run·지문·내용 해시)과 본문을 함께."""

    final_video_revision: str
    run_id: str
    fingerprint: str
    auto_content_sha256: str
    body: str
    content: dict[str, object]
    path: Path


class ReviewStore:
    """검토 자료의 저장소 — 호출자가 준 root 안에서만 움직인다."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def store_key(self, final_video_revision: str, run_id: str, fingerprint: str) -> str:
        """자리 이름 — 렌더·지문 축약에 검증된 run_id를 얹는다(렌더가 다르면 갈라진다)."""
        return "{}-{}-{}".format(
            require_hex_digest(final_video_revision, "final_video_revision")[:16],
            require_hex_digest(fingerprint, "fingerprint")[:16],
            require_store_component(run_id, "run_id"),
        )

    def freeze_automatic(
        self,
        *,
        final_video_revision: str,
        run_id: str,
        fingerprint: str,
        content: dict[str, object],
    ) -> FrozenAutomatic:
        """자동 결과를 동결한다 — 같은 신원·같은 바이트는 멱등, 다른 바이트는 거부."""
        key = self.store_key(final_video_revision, run_id, fingerprint)
        document = {
            "schema": AUTOMATIC_SCHEMA,
            "identity": {
                "final_video_revision": final_video_revision,
                "run_id": run_id,
                "fingerprint": fingerprint,
            },
            "auto_content_sha256": sha256_text(canonical_json(content)),
            "content": content,
        }
        path = self._inside(AUTOMATIC_DIR, key, SNAPSHOT_NAME)
        _write_noreplace(path, canonical_json(document))
        return self.load_automatic(
            final_video_revision=final_video_revision, run_id=run_id, fingerprint=fingerprint
        )

    def find_automatic(
        self, *, final_video_revision: str, run_id: str, fingerprint: str
    ) -> FrozenAutomatic | None:
        """스냅샷이 있으면 읽고, 없으면 None — 다른 신원의 자리는 못 본 척하지 않는다."""
        path = self._snapshot_path(final_video_revision, run_id, fingerprint)
        if not path.is_file():
            return None
        return self.load_automatic(
            final_video_revision=final_video_revision, run_id=run_id, fingerprint=fingerprint
        )

    def load_automatic(
        self, *, final_video_revision: str, run_id: str, fingerprint: str
    ) -> FrozenAutomatic:
        """동결 스냅샷을 읽는다 — 스키마·신원·내용 해시를 다시 잰다(낡은 기록 검사)."""
        path = self._snapshot_path(final_video_revision, run_id, fingerprint)
        document = _read_strict(path)
        if document.get("schema") != AUTOMATIC_SCHEMA:
            raise CaptionStoreSchemaError(
                f"모르는 스키마 {document.get('schema')!r} — {AUTOMATIC_SCHEMA!r}만 읽는다: {path}"
            )
        _require_exact_keys(document, {"schema", "identity", "auto_content_sha256", "content"})
        identity = _require_dict(document["identity"], "identity")
        _require_exact_keys(identity, {"final_video_revision", "run_id", "fingerprint"})
        content = _require_dict(document["content"], "content")
        recorded = sha256_text(canonical_json(content))
        declared = document["auto_content_sha256"]
        if not isinstance(declared, str):
            raise CaptionStoreSchemaError("auto_content_sha256은(는) 문자열이어야 한다")
        if recorded != declared:
            raise CaptionStoreIntegrityError(
                f"스냅샷 내용 해시가 어긋난다: 기록 {declared[:12]}… ≠"
                f" 실제 {recorded[:12]}… — 낡거나 손상된 자동 기록이다: {path}"
            )
        if identity != {
            "final_video_revision": final_video_revision,
            "run_id": run_id,
            "fingerprint": fingerprint,
        }:
            raise CaptionStoreIntegrityError(f"스냅샷 신원이 요청과 다르다: {path}")
        return FrozenAutomatic(
            final_video_revision=final_video_revision,
            run_id=run_id,
            fingerprint=fingerprint,
            auto_content_sha256=recorded,
            body=path.read_text(encoding="utf-8"),
            content=content,
            path=path,
        )

    def save_manual(
        self,
        *,
        final_video_revision: str,
        run_id: str,
        fingerprint: str,
        auto_content_sha256: str,
        revision: int,
        body: str,
        parent_sha256: str,
    ) -> Path:
        """수동 리비전을 no-replace로 저장한다 — 부모 연쇄를 먼저 증명한다."""
        key = self.store_key(final_video_revision, run_id, fingerprint)
        require_hex_digest(auto_content_sha256, "auto_content_sha256")
        if revision < 0:
            raise CaptionStorePathError(f"리비전 번호는 0 이상이어야 한다: {revision}")
        self._verify_chain_link(
            key,
            final_video_revision,
            run_id,
            fingerprint,
            auto_content_sha256,
            revision,
            parent_sha256,
        )
        path = self._inside(MANUAL_DIR, key, f"rev-{revision:04d}.json")
        _write_noreplace(path, body)
        return path

    def _verify_chain_link(
        self,
        key: str,
        final_video_revision: str,
        run_id: str,
        fingerprint: str,
        auto_content_sha256: str,
        revision: int,
        parent_sha256: str,
    ) -> None:
        """연쇄 고리 하나 — 0은 스냅샷 내용 해시에, n≥1은 부모 파일 바이트에 묶인다."""
        if revision == 0:
            snapshot = self.load_automatic(
                final_video_revision=final_video_revision,
                run_id=run_id,
                fingerprint=fingerprint,
            )
            if snapshot.auto_content_sha256 != parent_sha256:
                raise CaptionStoreChainError(
                    "기본 리비전(0)은 자동 스냅샷 내용 해시에 묶여야 한다 — 낡은 자동 근거다"
                )
            return
        parent = self._inside(MANUAL_DIR, key, f"rev-{revision - 1:04d}.json")
        if not parent.is_file():
            raise CaptionStoreChainError(f"부모 리비전 {revision - 1}이 없다: {parent}")
        if sha256_text(parent.read_text(encoding="utf-8")) != parent_sha256:
            raise CaptionStoreChainError(
                f"리비전 {revision}의 부모 해시가 저장된 파일과 다르다 — 연쇄가 끊겼다"
            )

    def manual_head(
        self,
        *,
        final_video_revision: str,
        run_id: str,
        fingerprint: str,
        auto_content_sha256: str,
    ) -> tuple[int, str] | None:
        """현재 머리 리비전 (번호, 본문) — 연쇄의 구멍과 마지막 고리를 다시 잰다."""
        key = self.store_key(final_video_revision, run_id, fingerprint)
        numbers = sorted(
            int(match.group(1))
            for match in (
                re.fullmatch(r"rev-(\d{4})\.json", entry.name)
                for entry in _list_dir(self._confined(MANUAL_DIR, key))
            )
            if match
        )
        if not numbers:
            return None
        if numbers != list(range(0, numbers[-1] + 1)):
            raise CaptionStoreChainError(f"리비전 연쇄에 구멍이 있다: {numbers}")
        body = self.load_manual(
            final_video_revision=final_video_revision,
            run_id=run_id,
            fingerprint=fingerprint,
            revision=numbers[-1],
        )
        self._verify_chain_link(
            key,
            final_video_revision,
            run_id,
            fingerprint,
            auto_content_sha256,
            numbers[-1],
            _parent_sha_of(body),
        )
        return numbers[-1], body

    def load_manual(
        self,
        *,
        final_video_revision: str,
        run_id: str,
        fingerprint: str,
        revision: int,
    ) -> str:
        """리비전 본문 — 파일이 없으면 오류다(없음과 빈 값을 구분한다)."""
        key = self.store_key(final_video_revision, run_id, fingerprint)
        path = self._confined(MANUAL_DIR, key, f"rev-{revision:04d}.json")
        if not path.is_file():
            raise CaptionStoreError(f"리비전 {revision}이 없다: {path}")
        return path.read_text(encoding="utf-8")

    def publish_final(
        self,
        *,
        final_video_revision: str,
        run_id: str,
        fingerprint: str,
        token: str,
        files: list[tuple[str, str]],
    ) -> Path:
        """확정 final을 새 리비전 디렉터리에 게시한다 — 순서대로, 덮어쓰기 없이.

        `files` 순서가 곧 게시 순서다 — manifest를 마지막에 실으면 manifest가 보이는
        디렉터리는 완성본이다(§14.1). 어느 고리에서 거부되어도 이미 게시된 **다른**
        확정 자료는 하나도 건드리지 않는다. 자리를 만들기 **전에** 성분을 하나씩
        짚어 심볼릭 링크를 거부한다 — 게시가 링크 너머로 새어 나가지 않는다.
        """
        key = self.store_key(final_video_revision, run_id, fingerprint)
        directory = self._confined(FINAL_DIR, key, require_store_component(token, "token"))
        directory.mkdir(parents=True, exist_ok=True)
        for name, body in files:
            _write_noreplace(self._confined(FINAL_DIR, key, token, name), body)
        return directory

    def final_dir(
        self, *, final_video_revision: str, run_id: str, fingerprint: str, token: str
    ) -> Path:
        """확정 자리의 경로 — 게시와 같은 규칙으로 짓는다(읽기용)."""
        key = self.store_key(final_video_revision, run_id, fingerprint)
        directory = self._confined(FINAL_DIR, key)
        directory.mkdir(parents=True, exist_ok=True)
        return directory / require_store_component(token, "token")

    def _snapshot_path(self, final_video_revision: str, run_id: str, fingerprint: str) -> Path:
        return self._inside(
            AUTOMATIC_DIR, self.store_key(final_video_revision, run_id, fingerprint), SNAPSHOT_NAME
        )

    def _inside(self, kind: str, key: str, name: str) -> Path:
        """root 안쪽 파일 경로 — 디렉터리를 만들기 전에 심볼릭 링크를 거부한다."""
        directory = self._confined(kind, key)
        directory.mkdir(parents=True, exist_ok=True)
        return self._confined(kind, key, name)

    def _confined(self, *parts: str) -> Path:
        """root 안쪽 경로 — 성분을 하나씩 검증하고 심볼릭 링크는 따르지 않는다(§4.4).

        `resolve()` 하나로 가두기를 묻는 건 위험하다 — resolve는 링크를 **따라**
        풀어버려, 밖을 겨냥하는지 알기도 전에 디렉터리가 만들어진다. 그래서 성분을
        앞에서부터 하나씩 짚어 링크인 순간 거부한다: mkdir·쓰기·읽기 앞에서.
        """
        current = self.root
        for part in parts:
            candidate = current / require_store_component(part, "경로 성분")
            if candidate.is_symlink():
                raise CaptionStorePathError(
                    f"저장소 성분이 심볼릭 링크다 — 따르지 않는다: {candidate}"
                )
            current = candidate
        if not current.resolve().is_relative_to(self.root.resolve()):
            raise CaptionStorePathError(f"저장소 밖을 겨냥한다: {current}")
        return current


def _write_noreplace(path: Path, body: str) -> bool:
    """fresh 임시 파일을 쓰고 `os.link`로 못박는다 — 같은 바이트면 멱등, 다르면 거부."""
    temp = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        temp.write_text(body, encoding="utf-8")
        try:
            os.link(temp, path)
            return True
        except FileExistsError:
            if path.read_text(encoding="utf-8") == body:
                return False
            raise CaptionStoreConflict(
                f"자리가 이미 다른 바이트로 차 있다: {path} — 새 실행/리비전 디렉터리를 써라"
            ) from None
    finally:
        temp.unlink(missing_ok=True)


def _read_strict(path: Path) -> dict[str, object]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CaptionStoreSchemaError(f"문서를 읽을 수 없다: {path} — {error}") from error
    return _require_dict(document, "문서")


def _parent_sha_of(body: str) -> str:
    """리비전 본문에서 부모 해시를 읽는다 — 연쇄 검사용 최소한의 해독."""
    document = json.loads(body)
    parent = document.get("parent_sha256") if isinstance(document, dict) else None
    if not isinstance(parent, str):
        raise CaptionStoreSchemaError("리비전 문서에 parent_sha256이 없다")
    return parent


def _require_exact_keys(values: dict[str, object], expected: set[str]) -> None:
    unknown = sorted(set(values) - expected)
    missing = sorted(expected - set(values))
    if unknown or missing:
        raise CaptionStoreSchemaError(
            f"문서 키가 계약 밖이다 — 모르는 키 {unknown}, 없는 키 {missing}"
        )


def _require_dict(item: object, where: str) -> dict[str, object]:
    if not isinstance(item, dict):
        raise CaptionStoreSchemaError(f"{where}는 객체여야 한다")
    return item


def _list_dir(directory: Path) -> list[Path]:
    try:
        return sorted(directory.iterdir())
    except FileNotFoundError:
        return []
