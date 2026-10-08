from copy import deepcopy
from fractions import Fraction
from pathlib import Path

from vlogsrt.project.raw.auto_cleanup import decisions


def f(n):
    value = Fraction(n)
    return {"num": value.numerator, "den": value.denominator}


def document():
    return {
        "asr": {"chunks": [{"failure": None}]},
        "audio": {"channels": 1, "sample_rate": 16000},
        "windows": {"windows": [{"window_id": "w", "core_start": f(0), "core_end": f(20)}]},
        "annotations": [
            {
                "window_id": "w",
                "status": "accepted",
                "needs_review": False,
                "scene_tags": ["dialogue"],
                "uncertainties": [],
            }
        ],
        "utterances": [
            {"utterance_id": "u1", "start": f(1), "end": f(3), "text": "오늘은 여행을 갑니다."},
            {"utterance_id": "u2", "start": f(7), "end": f(9), "text": "오늘은 여행을 갑니다."},
        ],
    }


def test_silence_requires_actual_quiet_and_keeps_speech_handles(tmp_path: Path):
    doc = document()
    pcm = tmp_path / "synthetic.pcm"
    pcm.write_bytes(bytes(20 * 16000 * 2))
    result = decisions(doc, source_id="s", revision_id="a", pcm_path=pcm)
    silent = next(d for d in result if d.reason == "internal_silence")
    assert (silent.start, silent.end) == (Fraction(33, 10), Fraction(67, 10))
    pcm.write_bytes(b"\xff\x1f" * 20 * 16000)
    assert not any(
        d.reason == "internal_silence"
        for d in decisions(doc, source_id="s", revision_id="a", pcm_path=pcm)
    )


def test_complete_replacement_required_and_never_delete_every_take():
    doc = document()
    doc["utterances"].append(
        {"utterance_id": "u3", "start": f(12), "end": f(14), "text": "오늘은 여행을 갑니다."}
    )
    result = decisions(doc, source_id="s", revision_id="a")
    assert len(result) == 1
    assert result[0].start == 1
    doc["utterances"] = doc["utterances"][:2]
    doc["utterances"][1]["complete"] = False
    assert decisions(doc, source_id="s", revision_id="a") == ()


def test_scenery_ambient_failure_and_uncertainty_stay():
    base = document()
    for change in ("scenery", "ambient", "action", "uncertain", "failed", "no_speech"):
        doc = deepcopy(base)
        if change in {"scenery", "ambient", "action"}:
            doc["annotations"][0]["scene_tags"].append(change)
        elif change == "uncertain":
            doc["annotations"][0]["needs_review"] = True
        elif change == "failed":
            doc["asr"]["chunks"][0]["failure"] = "synthetic failure"
        else:
            doc["utterances"] = []
        assert decisions(doc, source_id="s", revision_id="a") == ()


def test_ng_requires_followup():
    doc = document()
    doc["utterances"][0]["text"] = "다시 할게"
    result = decisions(doc, source_id="s", revision_id="a")
    assert result and result[0].reason == "ng_with_followup"
    doc["utterances"] = doc["utterances"][:1]
    assert decisions(doc, source_id="s", revision_id="a") == ()
