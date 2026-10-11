"""W-18 세션 18b / D58 — 판정 schema의 `visual_issues`와 엄격해진 `recommended` 기준.

창마다 모델이 화면 문제(흐림·흔들림·어두움·과노출·가림)를 enum 배열로 적는다. 같은 schema가
gpt-6-luna(Responses strict)·Claude Opus 5.5·Claude Sonnet 5.5(Messages json_schema)로 간다 — 개수·
길이 제약은 없다(Claude 구조화 출력은 배열·문자열 제약을 받지 않는다). 저장 표현은 셋이다:

- 키 없음(옛 문서·옛 응답) → `()` — 「알 수 없음」. 등급을 낮추는 근거가 아니다(W-20b).
- `["none"]` → `("none",)` — 화면을 보고 문제가 없다고 답했다.
- 실제 문제 → 그 값들(`none` 없이). 고친 것은 `visual_issues_note`에 남는다.

안내문 리비전을 올렸으므로 끝난 옛 리비전은 재사용되지 않는다 — whisper 청크 열쇠와 1단계 승인
지문은 바이트까지 그대로다. 유료 호출 0회.
"""

from __future__ import annotations

import json
from dataclasses import asdict, replace
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.raw import test_understanding as stage1_tests
from tests.raw.test_tiering import document as tier_document
from tests.raw.test_transcription_identity import (
    PRE_18B_UNDERSTANDING,
    PRE_W18_DEFAULT,
    SPANS,
    pre_w14_asr_settings_fingerprint,
    pre_w14_chunk_key,
)
from tests.raw.test_understanding_luna import SETTINGS, accepted_body, make_audio, make_evidence
from vlogsrt.project.raw import tiering
from vlogsrt.project.raw import understanding_luna as luna
from vlogsrt.project.raw.analysis_approval import asr_settings_fingerprint
from vlogsrt.project.raw.analysis_config import (
    DEFAULT_PROMPT_REVISION,
    TRANSCRIPTION_IDENTITY_PROMPT_REVISION,
    RawUnderstandingSettings,
)
from vlogsrt.project.raw.analysis_workflow import source_input_fingerprint
from vlogsrt.project.raw.config import OPENAI_LUNA_MODEL, RawWorkflowConfig
from vlogsrt.project.raw.selection_analysis import _same_analysis
from vlogsrt.project.raw.understanding import VideoGrid, transcribe_selected_audio
from vlogsrt.project.raw.understanding_luna import (
    VISUAL_ISSUE_VALUES,
    SemanticAnnotation,
    annotation_json,
    build_semantic_payload,
    luna_output_schema,
    parse_luna_response,
)

SONNET = "claude-sonnet-5-5"
OPUS = "claude-opus-5-5"
LUNA = OPENAI_LUNA_MODEL
OLD_PROMPT_REVISION = "raw-suitability-v2"
"""18b 이전의 안내문 리비전 — 이미 있는 작업공간의 끝난 분석이 이 값을 달고 있다."""
LENGTH_KEYWORDS = frozenset(
    {
        "minItems",
        "maxItems",
        "uniqueItems",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "pattern",
        "minProperties",
        "maxProperties",
    }
)


def reply(model: str, body: dict[str, Any]) -> dict[str, Any]:
    """공급자 모양의 응답 — Claude는 Messages 본문, Luna는 Responses `output_text`."""
    text = json.dumps(body, ensure_ascii=False)
    if model == LUNA:
        return {"output_text": text}
    return {
        "type": "message",
        "model": model,
        "content": [
            {"type": "thinking", "thinking": "", "signature": "s"},
            {"type": "text", "text": text},
        ],
        "stop_reason": "end_turn",
    }


def judged(model: str, evidence: Any, **overrides: Any) -> SemanticAnnotation:
    return parse_luna_response(
        reply(model, accepted_body(evidence, **overrides)),
        evidence=evidence,
        model=model,
        request_sha256="f" * 64,
        origin="offline_replay",
        prompt_revision=SETTINGS.prompt_revision,
    )


def _keys(node: object) -> set[str]:
    if isinstance(node, dict):
        return set(node) | {key for value in node.values() for key in _keys(value)}
    if isinstance(node, list):
        return {key for value in node for key in _keys(value)}
    return set()


# ─────────────────────────────────────────────────────────────
#  schema — 세 공급자가 같은 칸을 받는다
# ─────────────────────────────────────────────────────────────


def test_the_schema_lists_visual_issues_as_a_required_enum_array_without_limits() -> None:
    schema = luna_output_schema()
    assert schema["properties"]["visual_issues"] == {
        "type": "array",
        "items": {
            "type": "string",
            "enum": ["blur", "shake", "dark", "overexposed", "obstructed", "none"],
        },
    }
    assert VISUAL_ISSUE_VALUES == ("blur", "shake", "dark", "overexposed", "obstructed", "none")
    assert "visual_issues" in schema["required"]
    assert set(schema["required"]) == set(schema["properties"])
    assert schema["additionalProperties"] is False
    # 개수·길이 제약은 schema 어디에도 없다 — Claude 구조화 출력이 400으로 거절한다
    assert not _keys(schema) & LENGTH_KEYWORDS


def test_every_provider_payload_carries_the_same_schema(tmp_path: Path) -> None:
    evidence = make_evidence(tmp_path)
    schema = luna_output_schema()
    luna_payload = build_semantic_payload(evidence, SETTINGS, LUNA)
    assert luna_payload["text"]["format"]["strict"] is True
    assert luna_payload["text"]["format"]["schema"] == schema
    for model in (OPUS, SONNET):
        payload = build_semantic_payload(evidence, SETTINGS, model)
        assert payload["output_config"]["format"] == {"type": "json_schema", "schema": schema}


def test_the_instruction_names_the_strict_recommended_rubric_and_the_issues(
    tmp_path: Path,
) -> None:
    text = luna._instruction(make_evidence(tmp_path))
    assert "recommended는 프레임에서 다음이 모두 보일 때만" in text
    for criterion in (
        "편집에 남길 만한 장면·행동·순간",
        "또렷이 알아볼",
        "노출",
        "움직임 번짐이나 가림이 보이지 않는다",
        "볼 장면 없이 이동만",
        "되풀이되는 채움",
    ):
        assert criterion in text, criterion
    # 등급의 경계가 서로 맞는다 — 확신이 없다고 모두 낮추지 않는다
    assert "편집에 남길 만한지 애매하면 usable" in text
    assert "보이지 않는 문제를 짐작해 낮추지는 마라" in text
    assert "프레임이 부족하거나 무엇을 찍었는지 알아볼 수 없으면 review" in text
    assert "프록시 압축으로 화면이 조금 뭉개진 것은 blur가 아니다" in text
    assert "프레임에 움직임 번짐이 보일 때만" in text
    for retired in ("확실하지 않으면", "창밖", "기울어짐", "초점이 불분명하면 확인 필요"):
        assert retired not in text, retired
    assert "visual_issues" in text
    for value in VISUAL_ISSUE_VALUES:
        assert value in text, value
    # 아이디 계약은 그대로다 — 제공된 아이디만, 추천에는 프레임 근거
    assert "제공된 window/frame/utterance/boundary 아이디 외의 아이디를 말하지 마라." in text
    assert "recommended에는 실제 frame evidence_ids를 포함하라." in text


# ─────────────────────────────────────────────────────────────
#  판정 — 저장·정규화·거부
# ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("model", [LUNA, OPUS, SONNET])
def test_reported_issues_are_stored_and_round_trip(tmp_path: Path, model: str) -> None:
    evidence = make_evidence(tmp_path)
    annotation = judged(model, evidence, visual_issues=["blur", "dark"])
    assert annotation.status == luna.ANNOTATION_ACCEPTED, annotation.defect
    assert (annotation.visual_issues, annotation.visual_issues_note) == (("blur", "dark"), "")
    written = annotation_json(annotation)
    assert written["visual_issues"] == ["blur", "dark"]
    assert written["visual_issues_note"] == ""
    loaded = SemanticAnnotation(**json.loads(json.dumps(written)))
    assert tuple(loaded.visual_issues) == ("blur", "dark")


@pytest.mark.parametrize(
    ("given", "stored", "note"),
    [
        (["none"], ("none",), ""),
        (["shake"], ("shake",), ""),
        (["none", "blur", "shake"], ("blur", "shake"), "none을 뺐다"),
        (["blur", "none"], ("blur",), "none을 뺐다"),
        (["dark", "dark", "obstructed"], ("dark", "obstructed"), "중복"),
        (["none", "none"], ("none",), "중복"),
        ([], ("none",), "비어 있어"),
    ],
)
def test_visual_issues_are_normalized_conservatively(
    tmp_path: Path, given: list[str], stored: tuple[str, ...], note: str
) -> None:
    """실제 문제는 남기고, 함께 온 `none`은 빼고, 중복은 합치고, 고친 일은 기록한다."""
    evidence = make_evidence(tmp_path)
    annotation = judged(SONNET, evidence, visual_issues=given)
    assert annotation.status == luna.ANNOTATION_ACCEPTED, annotation.defect
    assert annotation.visual_issues == stored
    if note:
        assert note in annotation.visual_issues_note
    else:
        assert annotation.visual_issues_note == ""
    assert annotation_json(annotation)["visual_issues_note"] == annotation.visual_issues_note


@pytest.mark.parametrize("model", [LUNA, SONNET])
@pytest.mark.parametrize(
    "given",
    [["fuzzy"], ["blur", "noise"], "blur", [1], [None], {"blur": True}],
)
def test_values_outside_the_enum_reject_the_judgment(
    tmp_path: Path, model: str, given: object
) -> None:
    """enum 밖의 값·배열이 아닌 값은 다른 schema 위반처럼 rejected다 — 아무것도 받지 않는다."""
    evidence = make_evidence(tmp_path)
    annotation = judged(model, evidence, visual_issues=given)
    assert annotation.status == luna.ANNOTATION_REJECTED
    assert "visual_issues" in annotation.defect
    assert annotation.visual_issues == ()
    assert annotation.suggested_boundary_ids == ()


def test_claude_casing_is_normalized_but_luna_stays_strict(tmp_path: Path) -> None:
    """suitability와 같다 — Claude의 대소문자만 맞추고, Luna(strict)는 그대로 거부한다."""
    evidence = make_evidence(tmp_path)
    claude = judged(SONNET, evidence, visual_issues=["Blur", "DARK"])
    assert claude.status == luna.ANNOTATION_ACCEPTED, claude.defect
    assert claude.visual_issues == ("blur", "dark")
    assert claude.visual_issues_note == "visual_issues의 대소문자를 맞췄다"  # 고친 일은 남는다
    folded = judged(SONNET, evidence, visual_issues=["NONE", "none"])
    assert folded.visual_issues == ("none",)
    assert "대소문자" in folded.visual_issues_note and "중복" in folded.visual_issues_note
    assert judged(SONNET, evidence, visual_issues=["blur"]).visual_issues_note == ""
    assert judged(LUNA, evidence, visual_issues=["Blur"]).status == luna.ANNOTATION_REJECTED


@pytest.mark.parametrize("given", [["none"], ["blur", "dark"], []])
def test_a_window_without_frames_stores_unknown_whatever_it_answered(
    tmp_path: Path, given: list[str]
) -> None:
    """오디오만인 창은 화면을 본 적이 없다 — `none`도 문제도 「알 수 없음」(`()`)으로 남긴다."""
    evidence = replace(make_evidence(tmp_path), frames=())
    annotation = judged(SONNET, evidence, visual_issues=given)
    assert annotation.status == luna.ANNOTATION_ACCEPTED, annotation.defect
    assert annotation.visual_issues == ()
    assert "프레임 증거가 없는" in annotation.visual_issues_note
    assert annotation_json(annotation)["visual_issues"] == []
    assert judged(SONNET, evidence).visual_issues_note == ""  # 키가 없으면 고칠 것도 없다


def test_a_reply_without_the_key_is_unknown_not_none(tmp_path: Path) -> None:
    """옛 응답(키 없음)은 받아들이되 「알 수 없음」이다 — `none`으로 지어내지 않는다."""
    evidence = make_evidence(tmp_path)
    annotation = judged(SONNET, evidence)
    assert annotation.status == luna.ANNOTATION_ACCEPTED, annotation.defect
    assert (annotation.visual_issues, annotation.visual_issues_note) == ((), "")
    assert annotation_json(annotation)["visual_issues"] == []
    explicit = judged(SONNET, evidence, visual_issues=["none"])
    assert explicit.visual_issues == ("none",) != annotation.visual_issues


def test_rejected_and_failed_judgments_carry_no_issues(tmp_path: Path) -> None:
    evidence = make_evidence(tmp_path)
    rejected = judged(SONNET, evidence, window_id="window_000999", visual_issues=["blur"])
    assert rejected.status == luna.ANNOTATION_REJECTED
    assert annotation_json(rejected)["visual_issues"] == []


def test_an_old_document_without_the_key_loads_as_unknown(tmp_path: Path) -> None:
    """옛 캐시·진행·완성 문서는 키가 없다 — 엄격한 `SemanticAnnotation(**json)`이 그대로 연다."""
    evidence = make_evidence(tmp_path)
    old = annotation_json(judged(SONNET, evidence))
    del old["visual_issues"], old["visual_issues_note"]
    loaded = SemanticAnnotation(**old)
    assert (loaded.visual_issues, loaded.visual_issues_note) == ((), "")
    for row in tier_document()["annotations"]:  # 18b 이전에 쓴 합성 완성 문서
        assert "visual_issues" not in row
        assert SemanticAnnotation(**row).visual_issues == ()


# ─────────────────────────────────────────────────────────────
#  안내문 리비전 — 옛 분석은 다시 쓰지 않고, 전사는 그대로 쓴다
# ─────────────────────────────────────────────────────────────


def _revision(settings: RawUnderstandingSettings, workflow: RawWorkflowConfig) -> Any:
    models = {"timing": workflow.models.timing_model, "luna": workflow.models.luna_model}
    return SimpleNamespace(
        status="complete",
        fingerprint="fp",
        settings_json=json.dumps(asdict(settings), ensure_ascii=False, sort_keys=True),
        models_json=json.dumps(models, ensure_ascii=False, sort_keys=True),
    )


def test_the_prompt_revision_is_bumped_so_old_completed_analyses_are_not_served() -> None:
    assert DEFAULT_PROMPT_REVISION != OLD_PROMPT_REVISION
    assert RawUnderstandingSettings().prompt_revision == DEFAULT_PROMPT_REVISION
    assert PRE_18B_UNDERSTANDING.prompt_revision == OLD_PROMPT_REVISION
    assert replace(PRE_18B_UNDERSTANDING, prompt_revision=DEFAULT_PROMPT_REVISION) == (
        RawUnderstandingSettings()
    )
    workflow = RawWorkflowConfig()
    assert _same_analysis(_revision(RawUnderstandingSettings(), workflow), {"fp"}, workflow)
    assert not _same_analysis(_revision(PRE_18B_UNDERSTANDING, workflow), {"fp"}, workflow)


def test_a_saved_judgment_of_the_old_revision_is_bought_again(tmp_path: Path) -> None:
    """같은 창이어도 옛 안내문의 판정은 재사용하지 않는다 — 요청 신원과 리비전이 둘 다 다르다."""
    evidence = make_evidence(tmp_path)
    payload = build_semantic_payload(evidence, SETTINGS, SONNET)
    sha = luna.luna_request_sha256(SONNET, payload)
    prior = replace(
        judged(SONNET, evidence),
        request_sha256=sha,
        prompt_revision=OLD_PROMPT_REVISION,
    )
    assert not luna._reusable(prior, sha, settings=SETTINGS, model=SONNET, origin="offline_replay")
    current = replace(prior, prompt_revision=SETTINGS.prompt_revision)
    assert luna._reusable(current, sha, settings=SETTINGS, model=SONNET, origin="offline_replay")


def test_the_analysis_input_fingerprint_moves_with_the_prompt_revision(tmp_path: Path) -> None:
    video = VideoGrid(0, Fraction(0), Fraction(1, 25), 500)
    values = {
        "source_sha256": "a" * 64,
        "video": video,
        "audio": make_audio(tmp_path),
        "workflow": RawWorkflowConfig(),
    }
    current = source_input_fingerprint(settings=RawUnderstandingSettings(), **values)
    assert current != source_input_fingerprint(settings=PRE_18B_UNDERSTANDING, **values)


def test_the_asr_approval_fingerprint_is_the_pre_18b_value() -> None:
    """1단계 승인 지문은 안내문 리비전을 듣지 않는다 — 18b 이전의 기본 지문과 바이트까지 같다."""
    assert TRANSCRIPTION_IDENTITY_PROMPT_REVISION == OLD_PROMPT_REVISION
    workflow = RawWorkflowConfig()
    current = asr_settings_fingerprint(RawUnderstandingSettings(), workflow)
    assert current == pre_w14_asr_settings_fingerprint(PRE_18B_UNDERSTANDING, PRE_W18_DEFAULT)
    assert asr_settings_fingerprint(PRE_18B_UNDERSTANDING, workflow) == current
    other = replace(RawUnderstandingSettings(), prompt_revision="raw-suitability-v9")
    assert asr_settings_fingerprint(other, workflow) == current
    # 전사 설정은 여전히 묶인다
    shorter = replace(RawUnderstandingSettings(), asr_chunk_max_seconds=60.0)
    assert asr_settings_fingerprint(shorter, workflow) != current


def test_whisper_chunk_keys_do_not_see_the_prompt_revision(tmp_path: Path) -> None:
    """옛 안내문으로 산 전사를 새 안내문의 1단계가 전송 0회로 다시 쓴다 — 열쇠는 W-14 이전 값."""
    audio = stage1_tests.make_audio(tmp_path, seconds=20.0)
    cache = tmp_path / "asr-cache"

    def run(understanding: RawUnderstandingSettings) -> stage1_tests.FakeTimingProvider:
        provider = stage1_tests.FakeTimingProvider(
            replies={0: [{"text": "첫 문장", "start": 0.0, "end": 4.0}], 1: []}
        )
        transcribe_selected_audio(
            audio,
            SPANS,
            settings=RawWorkflowConfig(),
            understanding=understanding,
            provider=provider,
            on=stage1_tests.BEFORE_SHUTDOWN,
            request_id="run-18b",
            workdir=tmp_path,
            cache_dir=cache,
        )
        return provider

    first = run(PRE_18B_UNDERSTANDING)
    assert len(first.seen) == len(SPANS)
    keys = {path.stem for path in cache.glob("*.json")}
    assert keys == {
        pre_w14_chunk_key(tmp_path / f"asr-chunk-{position:04d}.wav", span, PRE_W18_DEFAULT)
        for position, span in enumerate(SPANS)
    }
    second = run(RawUnderstandingSettings())
    assert second.seen == []  # whisper를 다시 사지 않는다
    assert {path.stem for path in cache.glob("*.json")} == keys


# ─────────────────────────────────────────────────────────────
#  화면 — 「분석 근거」의 영상 이유에 쉬운 말로
# ─────────────────────────────────────────────────────────────


def test_the_video_reason_names_the_issues_in_plain_korean_without_changing_tiers() -> None:
    plain = tier_document()
    flagged = tier_document()
    flagged["annotations"][0]["visual_issues"] = ["blur", "shake", "dark"]
    flagged["annotations"][1]["visual_issues"] = ["overexposed", "obstructed"]
    flagged["annotations"][2]["visual_issues"] = ["none"]
    window0 = tiering.judge_video(tiering.evidence_of(flagged), Fraction(0), Fraction(10))
    assert "화면 문제: 흐림·흔들림·어두움" in window0.reason
    window1 = tiering.judge_video(tiering.evidence_of(flagged), Fraction(16), Fraction(20))
    assert "화면 문제: 과노출·가림" in window1.reason
    for start, end in ((Fraction(31), Fraction(39)), (Fraction(46), Fraction(59))):
        reason = tiering.judge_video(tiering.evidence_of(flagged), start, end).reason
        assert "화면 문제" not in reason  # none·키 없음은 아무 말도 보태지 않는다
    for text in (window0.reason, window1.reason):
        for word in ("Luna", "Claude", "Sonnet", "Opus", "gpt", "$"):
            assert word not in text.split(" · 화면 문제")[1]
    # 표시만 바뀐다 — 등급·규칙·구간은 그대로다(등급에 쓰는 것은 W-20b)
    shown = [(s.start, s.end, s.tier, s.rule) for s in tiering.classify(flagged).spans]
    assert shown == [(s.start, s.end, s.tier, s.rule) for s in tiering.classify(plain).spans]
    # 「분석 근거」가 보이는 검토 조각의 `영상:` 태그에 그 말이 실린다
    _rec, tags = tiering.segment_grade(tiering.classify(flagged), Fraction(0), Fraction(10))
    video = [tag for tag in tags if tag.startswith(tiering.VIDEO_TAG_PREFIX)]
    assert len(video) == 1 and "화면 문제: 흐림·흔들림·어두움" in video[0]
