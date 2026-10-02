"""편집용 전달물 — 폴더·파일 이름, clips.jsonl·clip_index.csv·README(계획 §9).

`clips/` 아래의 실제 파일 배열은 불변 내보내기 스냅샷이다(§9.2): DB가 정본이고
`clips.jsonl`·`clip_index.csv`는 그 순간의 기록이다. 옛 package의 파일을
덮어쓰거나 번호를 다시 붙이는 길은 이 모듈에 없다 — 새 추출은 새 package
디렉터리를 연다(§6.5).

파일 이름은 계획 §9.1의 모양을 따른다: 원본 폴더 `000001_DJI_0001`, 조각 파일
`000001__t000000000-t000012400__c_a1b2.mp4`. 번호는 원본 시간순(전체 순서
리비전의 원본 순번 · 원본 안 조각 순번)이고, 시각은 원본 좌표 밀리초, `c_`는
조각 신원의 지문이다. 이름이 곧 신원은 아니다 — 신원은 DB의 clip_id다(§13.1).
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import secrets
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path

from vlogsrt.project.raw.models import (
    ClipArtifact,
    PackageRevision,
    SegmentPlanEntry,
    SourceAsset,
)

__all__ = [
    "CSV_FIELDS",
    "PACKAGE_ROOT",
    "clip_digest",
    "clip_filename",
    "clip_index_csv",
    "clips_jsonl",
    "clip_relative_path",
    "formula_safe_cell",
    "manifest_names",
    "next_package_dir",
    "readme_text",
    "source_folder_name",
    "write_package_manifest",
]

PACKAGE_ROOT = "clips"
"""package가 사는 작업공간 상대 뿌리 — artifact 미디어의 허용 자리와 짝이다(§4.4)."""

README_NAME = "README.md"
CLIPS_JSONL_NAME = "clips.jsonl"
CLIP_INDEX_NAME = "clip_index.csv"

CSV_FIELDS: tuple[str, ...] = (
    "source_order",
    "segment_order",
    "clip_id",
    "relative_path",
    "source_id",
    "source_filename",
    "core_start",
    "core_end",
    "media_start",
    "media_end",
    "duration",
    "speech_summary",
    "visual_summary",
    "tags",
    "review_status",
)
"""§9.3의 전달물 CSV 머리 — 순서까지 계약이다. A3는 요약·태그 칸을 비워 둔다
(의미 분석은 A4). 비운 칸도 머리에 남긴다 — 나중에 채워질 자리임을 숨기지
않는다."""

_UNSAFE_NAME = re.compile(r"[^\w.\-()가-힣ㄱ-ㅎㅏ-ㅣ ]+")
"""폴더 이름에 남겨 둘 글자 — 나머지는 밑줄로 바꾼다(경로 구분·제어문자 차단)."""


def next_package_dir(existing: Sequence[str]) -> str:
    """다음 package 상대 경로 — `clips/package_NNNNNN`, 건너뛰지 않는다."""
    taken = set(existing)
    number = 1
    while f"{PACKAGE_ROOT}/package_{number:06d}" in taken:
        number += 1
    return f"{PACKAGE_ROOT}/package_{number:06d}"


def source_folder_name(source_order: int, source_name: str) -> str:
    """원본 폴더 이름 — 전체 순서 순번과 원본 이름(안전하게 다듬어)."""
    safe = _UNSAFE_NAME.sub("_", source_name).strip("._ ")
    if not safe:
        safe = "source"
    return f"{source_order:06d}_{safe}"


def clip_digest(entry: SegmentPlanEntry) -> str:
    """조각 지문 — 신원·경계·원본 순서를 묶는 sha256(파일 이름의 `c_` 몫)."""
    payload = json.dumps(
        {
            "segment_id": entry.segment_id,
            "source_order": entry.source_order,
            "segment_order": entry.segment_order,
            "core": [entry.core_start_pts, entry.core_end_pts],
            "time_base": [entry.time_base_num, entry.time_base_den],
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def clip_filename(entry: SegmentPlanEntry, profile_container: str) -> str:
    """조각 파일 이름 — 조각 순번·원본 좌표 밀리초(천장)·신원 지문(§9.1)."""
    time_base = Fraction(entry.time_base_num, entry.time_base_den)
    start_ms = _ceil_ms(Fraction(entry.core_start_pts) * time_base)
    end_ms = _ceil_ms(Fraction(entry.core_end_pts) * time_base)
    return (
        f"{entry.segment_order:06d}__t{start_ms:09d}-t{end_ms:09d}"
        f"__c_{clip_digest(entry)[:4]}.{profile_container}"
    )


def clip_relative_path(
    package_dir: str, entry: SegmentPlanEntry, asset: SourceAsset, profile_container: str
) -> str:
    """조각의 작업공간 상대 경로 — 폴더는 원본 순번, 파일은 조각 순번."""
    return "/".join(
        (
            package_dir,
            source_folder_name(entry.source_order, asset.source_name),
            clip_filename(entry, profile_container),
        )
    )


def _ceil_ms(seconds: Fraction) -> int:
    """정확한 유리수 초를 밀리초 정수로(천장) — 표시용 환산, 시각의 근거는 틱이다."""
    scaled = seconds * 1000
    return -((-scaled.numerator) // scaled.denominator)


def formula_safe_cell(value: str) -> str:
    """수식 주입 방지 — 셀 처음의 =·+·-·@·탭·CR 앞에 어포스트로피를 붙인다(§9.3).

    모델 출력이 아니어도 파일 이름·요약이 스프레드시트 수식으로 실행되는 일은
    없어야 한다. 비어 있으면 그대로 둔다(빈 칸과 막은 칸을 구분한다).
    """
    if value and value[0] in "=+-@\t\r":
        return f"'{value}"
    return value


def _seconds_text(value: Fraction) -> str:
    """정확한 유리수 초를 6자리 고정소수로 — 전달물 표기, 근거는 틱에 있다(§13.3)."""
    scaled = value * 1_000_000
    whole = scaled.numerator // scaled.denominator
    return f"{whole // 1_000_000}.{whole % 1_000_000:06d}"


def _clip_row(clip: ClipArtifact, asset: SourceAsset) -> dict[str, str]:
    """CSV 한 줄 — 값은 전부 문자열로, 수식 방지를 지난다."""
    row = {
        "source_order": str(clip.source_order),
        "segment_order": str(clip.segment_order),
        "clip_id": clip.clip_id,
        "relative_path": clip.relative_path,
        "source_id": clip.asset_id,
        "source_filename": asset.source_name,
        "core_start": _seconds_text(Fraction(clip.core_start_pts) * clip.time_base),
        "core_end": _seconds_text(Fraction(clip.core_end_pts) * clip.time_base),
        "media_start": _seconds_text(Fraction(clip.media_start_pts) * clip.time_base),
        "media_end": _seconds_text(Fraction(clip.media_end_pts) * clip.time_base),
        "duration": _seconds_text(Fraction(clip.media_local_duration)),
        # A4(의미 분석)가 채울 칸 — 지금은 비워 두고 머리로 자리를 지킨다.
        "speech_summary": "",
        "visual_summary": "",
        "tags": "",
        "review_status": clip.status,
    }
    return {key: formula_safe_cell(value) for key, value in row.items()}


def clip_index_csv(clips: Sequence[ClipArtifact], assets: Mapping[str, SourceAsset]) -> str:
    """전달물 CSV 본문 — utf-8-sig로 감싸 부르는 쪽과 함께 Excel에서도 읽힌다."""
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=CSV_FIELDS, lineterminator="\r\n")
    writer.writeheader()
    for clip in clips:
        writer.writerow(_clip_row(clip, assets.get(clip.asset_id, _unknown_asset(clip))))
    return buffer.getvalue()


def _unknown_asset(clip: ClipArtifact) -> SourceAsset:
    """자산 기록이 없는 조각 — 빈 이름으로라도 행을 남긴다(조용히 빼지 않는다)."""
    now = datetime.now(UTC).isoformat(timespec="seconds")
    return SourceAsset(
        asset_id=clip.asset_id,
        batch_dir="",
        relative_path="",
        status="ready",
        size_bytes=0,
        source_root="",
        source_relative_path="",
        source_name="unknown",
        content_sha256=None,
        file_id=None,
        duplicate_of=None,
        capture_time=None,
        duration_seconds=None,
        width=None,
        height=None,
        error=None,
        source_mtime_ns=0,
        source_inode=0,
        ordinal=0,
        created_at=now,
        updated_at=now,
    )


def readme_text(
    package: PackageRevision,
    assets: Mapping[str, SourceAsset],
    clips: Sequence[ClipArtifact],
) -> str:
    """사용 안내 — 무엇이 어떤 순서로 있는지, Final Cut으로 어떻게 가져가는지."""
    total = sum((Fraction(clip.media_local_duration) for clip in clips), Fraction(0))
    lines = [
        "# 편집용 조각 폴더",
        "",
        f"- package: `{package.package_dir}`",
        f"- 분할 계획 리비전: `{package.plan_revision_id}`"
        f" · 전체 순서 리비전: `{package.order_revision_id}`",
        f"- 프로파일: `{package.profile_name}` · 핸들: 앞 {package.handles_before}초 /"
        f" 뒤 {package.handles_after}초",
        f"- 원본 {len({clip.asset_id for clip in clips})}건 · 조각 {len(clips)}건 ·"
        f" 총 길이 {_seconds_text(total)}초",
        "",
        "폴더는 전체 원본 순서 리비전의 순번(000001…)이고, 그 안 파일은 그 원본의",
        "재생 시간순 조각 번호다. 번호를 바꾸거나 파일을 옮기지 말 것 — 순서 목록은",
        "`clips.jsonl`·`clip_index.csv`가 정본 기록이다.",
        "",
        "## Final Cut Pro로 가져가기",
        "",
        "1. Finder에서 이 폴더를 연다(경로는 `clip_index.csv`의 `relative_path`와 같다).",
        "2. 폴더나 파일을 Final Cut의 이벤트로 가져온다(가져오기는 Finder 흐름을 쓴다).",
        "3. 정렬은 이름으로 확인한다 — 파일 번호가 있어도 앱이 원하는 타임라인을",
        "   자동으로 만들지는 않는다(§9.4).",
        "",
        "이 폴더의 파일은 지속 보관 대상이다 — 앱을 종료하거나 캐시를 지워도 남는다.",
    ]
    missing = [clip for clip in clips if clip.status != "published"]
    if missing:
        lines += [
            "",
            "## 게시되지 않은 조각",
            "",
            "아래 조각은 검증을 통과하지 못해 편집용 목록에 없다(§6.3 — 조용히 숨기지 않는다).",
        ]
        lines += [f"- `{clip.clip_id}` ({clip.error or clip.status})" for clip in missing]
    return "\n".join(lines) + "\n"


def manifest_names() -> tuple[str, ...]:
    """package 안 전달물 파일 이름 — 작업공간 상대 경로로 붙여 쓴다."""
    return (CLIPS_JSONL_NAME, CLIP_INDEX_NAME, README_NAME)


def write_package_manifest(
    workspace_root: Path,
    package: PackageRevision,
    assets: Mapping[str, SourceAsset],
    clips: Sequence[ClipArtifact],
) -> tuple[str, ...]:
    """완성된 package의 전달물을 쓴다 — 이미 있으면 죽는다, 덮어쓰지 않는다.

    각 파일은 임시 이름에 쓰고 이름 바꾸기로 제자리에 놓는다. 옛 package를 다시
    쓰는 길은 애초에 없다 — package 번호는 예약이 새로 정하고, 같은 package에
    다시 쓰려는 시도는 여기서 실패로 끝난다(§6.5).
    """
    directory = workspace_root / package.package_dir
    directory.mkdir(parents=True, exist_ok=True)
    payloads: tuple[tuple[str, str], ...] = (
        (CLIPS_JSONL_NAME, clips_jsonl(package, assets, clips)),
        (CLIP_INDEX_NAME, clip_index_csv(clips, assets)),
        (README_NAME, readme_text(package, assets, clips)),
    )
    written: list[str] = []
    for name, body in payloads:
        target = directory / name
        if target.exists():
            raise FileExistsError(f"전달물이 이미 있다(덮어쓰지 않는다): {target}")
        staged = directory / f".{name}.{secrets.token_hex(4)}.partial"
        staged.write_text(body, encoding="utf-8-sig" if name == CLIP_INDEX_NAME else "utf-8")
        staged.replace(target)
        written.append(f"{package.package_dir}/{name}")
    return tuple(written)


def clips_jsonl(
    package: PackageRevision, assets: Mapping[str, SourceAsset], clips: Sequence[ClipArtifact]
) -> str:
    """`clips.jsonl` — 정수 틱 좌표의 불변 스냅샷(§13.3)."""
    lines = [
        json.dumps(
            {
                "schema_version": "raw-workspace/v1",
                "package_dir": package.package_dir,
                "plan_revision": package.plan_revision_id,
                "order_revision": package.order_revision_id,
                "profile": package.profile_name,
                "handles": {
                    "before": package.handles_before,
                    "after": package.handles_after,
                },
                "clip_id": clip.clip_id,
                "segment_id": clip.segment_id,
                "source_id": clip.asset_id,
                "source_filename": _asset_name(assets, clip),
                "source_order": clip.source_order,
                "segment_order": clip.segment_order,
                "relative_path": clip.relative_path,
                "content_sha256": clip.content_sha256,
                "time_base": {"num": clip.time_base_num, "den": clip.time_base_den},
                "core_range_pts": [clip.core_start_pts, clip.core_end_pts],
                "media_range_pts": [clip.media_start_pts, clip.media_end_pts],
                "core_local": [clip.core_local_start, clip.core_local_end],
                "media_local_duration": clip.media_local_duration,
                "frame_count": clip.frame_count,
                "status": clip.status,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        for clip in clips
    ]
    return "\n".join(lines) + ("\n" if lines else "")


def _asset_name(assets: Mapping[str, SourceAsset], clip: ClipArtifact) -> str:
    asset = assets.get(clip.asset_id)
    return asset.source_name if asset is not None else "unknown"
