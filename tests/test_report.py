"""report.md — 사람이 어디를 봐야 하는지 20초 안에 알게 한다.

리포트의 핵심은 이제 검증 결과다: 의심 큐(일치율 낮은 순)와
자막이 발화를 놓쳤을 수 있는 구간.
"""

from __future__ import annotations

from vlogsrt.config import Config
from vlogsrt.models import Cue, Segment
from vlogsrt.report import build_report
from vlogsrt.verify import ChunkCheck, CueCheck, Verification

from .conftest import make_cue, make_segment


def sample() -> tuple[list[Cue], list[Segment]]:
    cues = [make_cue(0.0, 2.0, "안녕하세요", index=1)]
    spine = [make_segment(0, 0.0, 2.0, "안녕하세요")]
    return cues, spine


def clean_chunk() -> ChunkCheck:
    return ChunkCheck(index=0, span=(0.0, 10.0), similarity=0.95, cue_count=1, flagged=False)


def test_report_without_verification_says_so(cfg: Config) -> None:
    cues, spine = sample()
    body = build_report("demo.mp4", cues, spine, [], 10.0, cfg, None)
    assert "검증 생략" in body


def test_report_lists_suspects_worst_first(cfg: Config) -> None:
    cues, spine = sample()
    verification = Verification(
        chunks=(clean_chunk(),),
        suspects=(
            CueCheck(cue_index=1, span=(0.0, 2.0), text="덜 심한 큐", coverage=0.55),
            CueCheck(cue_index=2, span=(3.0, 4.0), text="심한 큐", coverage=0.10),
        ),
        cue_total=2,
    )
    body = build_report("demo.mp4", cues, spine, [], 10.0, cfg, verification)
    assert "의심 큐 2개" in body
    assert body.index("심한 큐") < body.index("덜 심한 큐")  # 낮은 일치율부터


def test_report_flags_regions_where_subtitles_missed_speech(cfg: Config) -> None:
    cues, spine = sample()
    verification = Verification(
        chunks=(
            ChunkCheck(index=0, span=(100.0, 200.0), similarity=0.42, cue_count=3, flagged=True),
        ),
        suspects=(),
        cue_total=1,
    )
    body = build_report("demo.mp4", cues, spine, [], 200.0, cfg, verification)
    assert "발화를 놓쳤을 수 있는 구간" in body
    assert "00:01:40" in body


def test_clean_verification_reads_clean(cfg: Config) -> None:
    cues, spine = sample()
    verification = Verification(chunks=(clean_chunk(),), suspects=(), cue_total=1)
    body = build_report("demo.mp4", cues, spine, [], 10.0, cfg, verification)
    assert "전 구간이 gpt 전사와 일치" in body
