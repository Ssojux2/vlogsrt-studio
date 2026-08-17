#!/usr/bin/env python3
"""T1.6 — whisper의 `srt` 출력이 `segments[]`를 직렬화한 것과 같은지 실측한다.

일회성 검증 스크립트다. `vlogsrt/` 안에 넣지 마라.
차이가 발견되면 골격 정의의 전제가 무너지는 것이므로 즉시 보고해야 한다.

사용:
    python scripts/verify_srt_equivalence.py tests/fixtures/api/ep01.whisper.json \\
                                             tests/fixtures/api/ep01.srt
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vlogsrt.output import parse_srt, write_srt  # noqa: E402
from vlogsrt.transcribe import build_spine, load_fixture_responses  # noqa: E402


def cues_from_spine(whisper_json: Path) -> list:
    from vlogsrt.pipeline import spine_to_cues

    responses = load_fixture_responses(whisper_json)
    spine, _ = build_spine(responses)
    return spine_to_cues(spine)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2

    whisper_json = Path(argv[0])
    serialized = write_srt(cues_from_spine(whisper_json))

    srt_path = Path(argv[1])
    if not srt_path.is_file():
        print(f"response_format='srt' 픽스처가 없습니다: {srt_path}")
        print("사람이 녹화해야 합니다. T1.3 녹화 시 함께 받아두는 편이 싸다.")
        return 2

    reference = srt_path.read_text(encoding="utf-8")
    if serialized.strip() == reference.strip():
        print("IDENTICAL")
        return 0

    mine = parse_srt(serialized)
    theirs = parse_srt(reference)
    print(f"DIFFERENT — 큐 수 {len(mine)} vs {len(theirs)}")
    for index, (a, b) in enumerate(zip(mine, theirs, strict=False), start=1):
        if (round(a.start, 3), round(a.end, 3)) != (round(b.start, 3), round(b.end, 3)):
            print(f"  #{index} 시각: {a.start:.3f}~{a.end:.3f} vs {b.start:.3f}~{b.end:.3f}")
        if a.text.strip() != b.text.strip():
            print(f"  #{index} 텍스트: {a.text!r} vs {b.text!r}")
    print()
    print("PLAN §2.4 전제가 무너졌다. 골격 정의를 재검토하고 docs/DECISIONS.md에 기록하라.")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
