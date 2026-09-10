"""비전 판정 — 조립·정규화·호출 경계의 계약 (P5-05).

네 가지 선을 증명한다:

- **페이로드는 증거만 싣는다.** 프레임은 Base64 `image_url` 데이터로 **공급된
  순서 그대로**, 설정 한도(`cfg.vision_max_images`) 안의 고유 아이디만 들어간다.
  파일 경로·XML·시각·음소거 권한은 실리지 않는다 — 모델이 고르는 것은 우리가
  만든 후보 아이디 하나뿐이고, 후보 창의 초 숫자조차 나가지 않는다.
- **파서는 언제나 블록을 보존한다.** 모르는 아이디·NaN·범위 밖 신뢰도·깨진 꼴은
  전부 오류 상태의 `keep_all` 리뷰로 정규화된다. 모델 응답 하나가 편집을 만들거나
  지울 수 있는 길은 없다.
- **승인은 세 조건의 교집합이다.** 알려진 후보 + 명시적 `reason_kind`
  `repetitive_ordinary_broll` + 신뢰도 ≥ `cfg.vision_apply_confidence`. 한 번뿐인
  사건(`unique_event`)과 `uncertain`은 숫자 신뢰도와 무관하게 `keep_all`로
  정규화된다.
- **호출 경계는 닫혀 있다가 열린다.** `vlogsrt.transcribe.request_vision`이 유일한
  비전 호출 지점이고 명시적 `online=True` 없이는 클라이언트를 만지지도 않는다.
  모델은 `cfg.vision_model` 문자열 그대로고 폴백·재시도가 없다. 가짜 클라이언트로만
  증명한다 — 실제 계정 호환성은 PENDING_HUMAN이다.

경계 배선 자체(오프라인 닫힘·클라이언트 조립·429/타임아웃)는
`tests/test_transcribe.py`가 증명하고, 여기는 정책 계약과 그 위의 조합을 증명한다.
모든 프레임은 합성 바이트다 — 픽스처·골든은 만지지 않는다(실행 규율).
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import os
import subprocess
import sys
import types
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from vlogsrt.project.captions import serialize_srt
from vlogsrt.project.config import ProjectConfig, load_project_config
from vlogsrt.project.fcpxml import parse_project
from vlogsrt.project.vision import policy, sampling
from vlogsrt.transcribe import request_vision

CFG = ProjectConfig()
ASSET = "asset-content-1"
REPO_ROOT = Path(__file__).resolve().parents[2]


# ─── 합성 재료 — 요청·프레임·응답 ────────────────────────────────────────


def _request(**overrides: Any) -> policy.VisionRequest:
    """기본형 요청 — 실험마다 한 조각씩 갈아끼운다. cand-mid 창이 9/2초에 걸쳐 있다."""
    fields: dict[str, Any] = {
        "block_id": "block-1",
        "frame_ids": ("f-00", "f-01", "f-02"),
        "candidates": (
            policy.VisionCandidate("cand-front", Fraction(0), Fraction(3)),
            policy.VisionCandidate("cand-mid", Fraction(9, 2), Fraction(6)),
            policy.VisionCandidate("cand-back", Fraction(6), Fraction(9)),
        ),
        "adjacent_context": "앞뒤로 같은 도시 풍경 B-roll이 이어진다",
    }
    fields.update(overrides)
    return policy.VisionRequest(**fields)


def _sample(
    tmp_path: Path, frame_id: str, *, source_time: Fraction = Fraction(0), data: bytes | None = None
) -> sampling.FrameSample:
    """프레임 한 장 — 바이트는 프레임 아이디에서만 온다(해시 검사용)."""
    raw = data if data is not None else f"image:{frame_id}".encode()
    path = tmp_path / f"{frame_id}.jpg"
    path.write_bytes(raw)
    return sampling.FrameSample(
        frame_id=frame_id,
        source_id=ASSET,
        source_time=source_time,
        image_path=path,
        image_sha256=hashlib.sha256(raw).hexdigest(),
        used_proxy=False,
        original_frame_index=0,
        selected_frame_index=0,
        selected_presentation_time=source_time,
        review=(),
    )


def _samples_for(tmp_path: Path, *frame_ids: str) -> tuple[sampling.FrameSample, ...]:
    return tuple(_sample(tmp_path, frame_id) for frame_id in frame_ids)


def _body(**overrides: Any) -> dict[str, Any]:
    """모델이 답해야 하는 JSON 객체의 기본형."""
    decision: dict[str, Any] = {
        "choice": "cand-mid",
        "reason": "같은 도시 풍경이 반복되는 평범한 B-roll이다",
        "reason_kind": policy.REASON_REPETITIVE_BROLL,
        "evidence_frame_ids": ["f-00", "f-02"],
        "confidence": 0.93,
    }
    decision.update(overrides)
    return decision


def _wrap(body: dict[str, Any]) -> dict[str, Any]:
    content = json.dumps(body, ensure_ascii=False)
    return {"choices": [{"message": {"content": content}}]}


def _reply(**overrides: Any) -> dict[str, Any]:
    return _wrap(_body(**overrides))


def _reply_without(key: str, replacement: Any = None) -> dict[str, Any]:
    body = _body()
    if replacement is None:
        body.pop(key)
    else:
        body[key] = replacement
    return _wrap(body)


def _user_text(payload: dict[str, Any]) -> str:
    """페이로드의 글월 부분 — 프레임 이미지 앞의 안내 문단이다."""
    parts = payload["messages"][0]["content"]
    text_parts = [part["text"] for part in parts if part["type"] == "text"]
    assert len(text_parts) == 1
    return text_parts[0]


def _image_parts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [part for part in payload["messages"][0]["content"] if part["type"] == "image_url"]


def _decoded_images(payload: dict[str, Any]) -> list[bytes]:
    return [
        base64.b64decode(part["image_url"]["url"].split(",", 1)[1])
        for part in _image_parts(payload)
    ]


# ─── 페이로드 — Base64 image_url, 공급 순서, 한도, 무권한 ─────────────────


def test_payload_embeds_base64_image_data_in_supplied_order(tmp_path: Path) -> None:
    payload = policy.build_payload(_request(), _samples_for(tmp_path, "f-00", "f-01", "f-02"), CFG)
    assert _decoded_images(payload) == [b"image:f-00", b"image:f-01", b"image:f-02"]
    for part in _image_parts(payload):
        assert part["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_payload_shape_is_one_user_message_of_text_then_images(tmp_path: Path) -> None:
    payload = policy.build_payload(_request(), _samples_for(tmp_path, "f-00", "f-01", "f-02"), CFG)
    assert set(payload) == {"messages"}
    (message,) = payload["messages"]
    assert message["role"] == "user"
    content = message["content"]
    assert content[0] == {"type": "text", "text": _user_text(payload)}
    for part in content[1:]:
        assert set(part) == {"type", "image_url"}
        assert set(part["image_url"]) == {"url"}


def test_payload_follows_the_request_order_not_the_sample_order(tmp_path: Path) -> None:
    shuffled = (_sample(tmp_path, "f-02"), _sample(tmp_path, "f-00"), _sample(tmp_path, "f-01"))
    payload = policy.build_payload(_request(), shuffled, CFG)
    assert _decoded_images(payload) == [b"image:f-00", b"image:f-01", b"image:f-02"]


def test_payload_accepts_the_configured_frame_limit_and_rejects_one_more(
    tmp_path: Path,
) -> None:
    limit = CFG.vision_max_images
    frame_ids = tuple(f"f-{i:02d}" for i in range(limit))
    payload = policy.build_payload(
        _request(frame_ids=frame_ids), _samples_for(tmp_path, *frame_ids), CFG
    )
    assert len(_image_parts(payload)) == limit

    one_more = frame_ids + (f"f-{limit:02d}",)
    with pytest.raises(policy.VisionPolicyError, match=str(limit)):
        policy.build_payload(_request(frame_ids=one_more), _samples_for(tmp_path, *one_more), CFG)


@pytest.mark.parametrize(
    "vision_request",
    [
        pytest.param(_request(frame_ids=("f-00", "f-00")), id="duplicate-frame-id"),
        pytest.param(_request(frame_ids=("f-00", "  ")), id="blank-frame-id"),
        pytest.param(_request(frame_ids=()), id="no-frames"),
        pytest.param(_request(block_id="  "), id="blank-block-id"),
        pytest.param(_request(candidates=()), id="no-candidates"),
        pytest.param(
            _request(
                candidates=(
                    policy.VisionCandidate("cand-a", Fraction(0), Fraction(3)),
                    policy.VisionCandidate("cand-a", Fraction(3), Fraction(6)),
                )
            ),
            id="duplicate-candidate-id",
        ),
        pytest.param(
            _request(candidates=(policy.VisionCandidate("cand-a", 0.0, Fraction(3)),)),  # type: ignore[arg-type]
            id="float-keep-start",
        ),
        pytest.param(
            _request(candidates=(policy.VisionCandidate("cand-a", Fraction(3), Fraction(3)),)),
            id="zero-keep-window",
        ),
        pytest.param(
            _request(candidates=(policy.VisionCandidate("cand-a", Fraction(6), Fraction(3)),)),
            id="reversed-keep-window",
        ),
        pytest.param(
            _request(candidates=(policy.VisionCandidate("cand-a", Fraction(-1), Fraction(3)),)),
            id="negative-keep-start",
        ),
        pytest.param(_request(adjacent_context=5), id="context-not-a-string"),  # type: ignore[arg-type]
    ],
)
def test_payload_rejects_a_broken_request_before_any_paid_call(
    vision_request: policy.VisionRequest, tmp_path: Path
) -> None:
    """깨진 요청은 조립 단계에서 닫는다 — 유료 호출은 깨진 요청에 값을 쓰지 않는다."""
    samples = _samples_for(tmp_path, *vision_request.frame_ids) if vision_request.frame_ids else ()
    with pytest.raises(policy.VisionPolicyError):
        policy.build_payload(vision_request, samples, CFG)


@pytest.mark.parametrize(
    ("sample_ids", "offender"),
    [
        pytest.param(("f-00", "f-01"), "f-02", id="missing-sample"),
        pytest.param(("f-00", "f-01", "f-02", "f-99"), "f-99", id="extra-sample"),
        pytest.param(("f-00", "f-01", "f-02", "f-00"), "f-00", id="duplicate-sample"),
    ],
)
def test_payload_matches_samples_to_the_request_exactly(
    tmp_path: Path, sample_ids: tuple[str, ...], offender: str
) -> None:
    samples = _samples_for(tmp_path, *sample_ids)
    with pytest.raises(policy.VisionPolicyError, match=offender):
        policy.build_payload(_request(), samples, CFG)


def test_payload_rejects_an_image_that_no_longer_matches_its_hash(tmp_path: Path) -> None:
    stale = dataclasses.replace(
        _sample(tmp_path, "f-01", data=b"stale-bytes"),
        image_sha256=hashlib.sha256(b"different-bytes").hexdigest(),
    )
    samples = (_sample(tmp_path, "f-00"), stale, _sample(tmp_path, "f-02"))
    with pytest.raises(policy.VisionPolicyError, match="해시"):
        policy.build_payload(_request(), samples, CFG)


def test_payload_rejects_an_unreadable_image(tmp_path: Path) -> None:
    missing = _sample(tmp_path, "f-01")
    missing.image_path.unlink()
    samples = (_sample(tmp_path, "f-00"), missing, _sample(tmp_path, "f-02"))
    with pytest.raises(policy.VisionPolicyError, match="f-01"):
        policy.build_payload(_request(), samples, CFG)


def test_payload_exposes_no_path_xml_time_or_mute_authority(tmp_path: Path) -> None:
    """모델에게 권한이 아니라 증거만 간다 — 경로·XML·시각·음소거는 실리지 않는다."""
    samples = _samples_for(tmp_path, "f-00", "f-01", "f-02")
    payload = policy.build_payload(_request(), samples, CFG)  # cand-mid 창: 9/2초
    text = _user_text(payload)
    whole = json.dumps(payload, ensure_ascii=False)

    assert "fcpxml" not in text.lower()
    assert "mute" not in text.lower()
    assert "9/2" not in text  # 후보 창의 분수 표기
    assert "4.5" not in text  # 같은 값의 십진 표기
    for sample in samples:  # 파일 경로·이름은 어디에도 없다(점은 Base64 알파벳 밖이다)
        assert sample.image_path.name not in whole
    for part in _image_parts(payload):  # image_url은 데이터 URL 한 조각일 뿐이다
        assert set(part) == {"type", "image_url"}


def test_payload_lists_candidates_front_to_back_and_carries_the_context(tmp_path: Path) -> None:
    payload = policy.build_payload(_request(), _samples_for(tmp_path, "f-00", "f-01", "f-02"), CFG)
    text = _user_text(payload)
    assert "cand-front, cand-mid, cand-back" in text  # 앞쪽부터 — keep_start 순서다
    assert text.index("cand-front") < text.index("cand-mid") < text.index("cand-back")
    assert "f-00, f-01, f-02" in text  # 프레임 아이디도 시간 순으로 나열된다
    assert "앞뒤로 같은 도시 풍경 B-roll이 이어진다" in text


def test_payload_instruction_names_the_exact_decision_schema(tmp_path: Path) -> None:
    """응답 계약을 모델에게도 같은 철자로 알린다 — 파서가 기대하는 그 키들이다."""
    payload = policy.build_payload(_request(), _samples_for(tmp_path, "f-00", "f-01", "f-02"), CFG)
    text = _user_text(payload)
    for token in (
        '"choice"',
        '"reason"',
        '"reason_kind"',
        '"evidence_frame_ids"',
        '"confidence"',
        policy.KEEP_ALL,
        policy.UNCERTAIN,
        policy.REASON_REPETITIVE_BROLL,
        policy.REASON_UNIQUE_EVENT,
    ):
        assert token in text, token


def test_payload_is_deterministic_for_identical_inputs(tmp_path: Path) -> None:
    request = _request()
    first = policy.build_payload(request, _samples_for(tmp_path, "f-00", "f-01", "f-02"), CFG)
    second = policy.build_payload(request, _samples_for(tmp_path, "f-00", "f-01", "f-02"), CFG)
    assert first == second


# ─── 파서 — 승인·keep_all·오류 정규화 ───────────────────────────────────


@pytest.mark.parametrize("candidate_id", ["cand-front", "cand-mid", "cand-back"])
def test_each_known_candidate_is_accepted_at_high_broll_confidence(candidate_id: str) -> None:
    decision = policy.parse_decision(
        _request(), _reply(choice=candidate_id, evidence_frame_ids=["f-01"]), CFG
    )
    assert decision.status == "accepted"
    assert decision.choice == candidate_id
    assert decision.evidence_frame_ids == ("f-01",)
    assert decision.confidence == pytest.approx(0.93)


@pytest.mark.parametrize(
    ("choice", "reason_kind"),
    [
        pytest.param(policy.KEEP_ALL, policy.REASON_REPETITIVE_BROLL, id="keep-all"),
        pytest.param(policy.KEEP_ALL, policy.REASON_UNIQUE_EVENT, id="keep-all-unique"),
        pytest.param(policy.UNCERTAIN, policy.REASON_REPETITIVE_BROLL, id="uncertain"),
        pytest.param(policy.UNCERTAIN, policy.REASON_UNIQUE_EVENT, id="uncertain-unique"),
    ],
)
def test_keep_all_and_uncertain_normalize_to_keep_all(choice: str, reason_kind: str) -> None:
    decision = policy.parse_decision(
        _request(), _reply(choice=choice, reason_kind=reason_kind), CFG
    )
    assert decision.status == "keep_all"
    assert decision.choice == policy.KEEP_ALL
    assert decision.confidence == pytest.approx(0.93)  # 숫자는 그대로 남는다


def test_a_unique_event_never_applies_regardless_of_confidence() -> None:
    decision = policy.parse_decision(
        _request(), _reply(reason_kind=policy.REASON_UNIQUE_EVENT, confidence=0.99), CFG
    )
    assert decision.status == "keep_all"
    assert decision.choice == policy.KEEP_ALL
    assert "unique_event" in decision.reason
    assert decision.confidence == pytest.approx(0.99)


def test_acceptance_needs_the_configured_confidence() -> None:
    threshold = CFG.vision_apply_confidence
    at_edge = policy.parse_decision(_request(), _reply(confidence=threshold), CFG)
    assert at_edge.status == "accepted"  # 한도는 이상(>=)이다

    below = policy.parse_decision(_request(), _reply(confidence=round(threshold - 0.01, 4)), CFG)
    assert below.status == "keep_all"
    assert below.choice == policy.KEEP_ALL
    assert "한도" in below.reason


def test_a_normalized_decision_keeps_the_models_reason_for_review() -> None:
    decision = policy.parse_decision(
        _request(), _reply(reason_kind=policy.REASON_UNIQUE_EVENT), CFG
    )
    assert "같은 도시 풍경이 반복되는" in decision.reason  # 원문 이유가 지워지지 않는다


def test_unknown_decision_keys_do_not_break_the_parse() -> None:
    decision = policy.parse_decision(_request(), _reply(extra="noise"), CFG)
    assert decision.status == "accepted"


def test_decisions_are_frozen_records() -> None:
    decision = policy.parse_decision(_request(), _reply(), CFG)
    with pytest.raises(dataclasses.FrozenInstanceError):
        decision.status = "error"  # type: ignore[misc]


@pytest.mark.parametrize(
    "reply",
    [
        pytest.param(["choices"], id="raw-not-a-mapping"),
        pytest.param({}, id="choices-missing"),
        pytest.param({"choices": {}}, id="choices-not-a-list"),
        pytest.param({"choices": []}, id="choices-empty"),
        pytest.param(
            {"choices": [{"message": {"content": "{}"}}, {"message": {"content": "{}"}}]},
            id="choices-two",
        ),
        pytest.param({"choices": [{}]}, id="message-missing"),
        pytest.param({"choices": [{"message": {}}]}, id="content-missing"),
        pytest.param(
            {"choices": [{"message": {"content": ["cand-mid"]}}]}, id="content-not-a-string"
        ),
        pytest.param(
            {"choices": [{"message": {"content": "가운데 후보가 좋아 보인다"}}]},
            id="content-not-json",
        ),
        pytest.param({"choices": [{"message": {"content": "[]"}}]}, id="content-json-array"),
        pytest.param({"choices": [{"message": {"content": "null"}}]}, id="content-json-null"),
        pytest.param(_reply_without("choice"), id="choice-missing"),
        pytest.param(_reply_without("choice", replacement=3), id="choice-not-a-string"),
        pytest.param(_reply_without("choice", replacement=True), id="choice-bool"),
        pytest.param(_reply(choice="cand-nope"), id="choice-unknown"),
        pytest.param(_reply(choice="keep all"), id="choice-unknown-spelling"),
        pytest.param(_reply_without("reason"), id="reason-missing"),
        pytest.param(_reply_without("reason", replacement=7), id="reason-not-a-string"),
        pytest.param(_reply(reason="   "), id="reason-blank"),
        pytest.param(_reply_without("reason_kind"), id="reason-kind-missing"),
        pytest.param(_reply(reason_kind="broll"), id="reason-kind-unknown"),
        pytest.param(_reply_without("evidence_frame_ids"), id="evidence-missing"),
        pytest.param(_reply(evidence_frame_ids="f-00"), id="evidence-not-a-list"),
        pytest.param(_reply(evidence_frame_ids=[0]), id="evidence-item-not-a-string"),
        pytest.param(_reply(evidence_frame_ids=["f-99"]), id="evidence-unsampled"),
        pytest.param(_reply(evidence_frame_ids=["f-00", "f-00"]), id="evidence-duplicate"),
        pytest.param(_reply_without("confidence"), id="confidence-missing"),
        pytest.param(_reply(confidence=True), id="confidence-bool"),
        pytest.param(_reply(confidence="0.9"), id="confidence-string"),
        pytest.param(_reply(confidence=float("nan")), id="confidence-nan"),
        pytest.param(_reply(confidence=float("inf")), id="confidence-inf"),
        pytest.param(_reply(confidence=-0.1), id="confidence-below-range"),
        pytest.param(_reply(confidence=1.5), id="confidence-above-range"),
        # 아래 셋은 해석기에서 판정이 아니라 예외로 샜던 길이다 — 오류 판정으로 못 박는다.
        # json은 임의 정밀도 정수를 그대로 담으므로 10**400은 문법적으로 멀쩡한 JSON이고,
        # 자릿수 한도(4300) 초과는 JSONDecodeError가 아닌 ValueError를,
        # 깊은 중첩은 RecursionError을 낸다.
        pytest.param(_reply(confidence=10**400), id="confidence-huge-integer"),
        pytest.param(
            {"choices": [{"message": {"content": "1" * 5000}}]},
            id="content-integer-past-the-digit-limit",
        ),
        pytest.param(
            {"choices": [{"message": {"content": "[" * 100_000 + "]" * 100_000}}]},
            id="content-nested-past-the-recursion-limit",
        ),
    ],
)
def test_a_contract_violating_response_is_an_error_that_keeps_the_block(reply: Any) -> None:
    decision = policy.parse_decision(_request(), reply, CFG)
    assert decision.status == "error"
    assert decision.choice == policy.KEEP_ALL  # 블록은 언제나 보존된다
    assert decision.evidence_frame_ids == ()
    assert decision.confidence == 0.0
    assert "계약" in decision.reason


# ─── 호출 경계 조합 — 가짜 클라이언트로 배선만 증명한다 ──────────────────


class FakeVisionClient:
    """`chat.completions.create` 호출 인자만 기록한다. 네트워크 없음."""

    def __init__(self, reply: object | None = None, failure: Exception | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self.reply: object = {} if reply is None else reply
        self.failure = failure
        self.chat = self
        self.completions = self

    def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if self.failure is not None:
            raise self.failure
        return self.reply


def test_the_configured_model_reaches_the_call_exactly(tmp_path: Path) -> None:
    client = FakeVisionClient(reply={"choices": []})
    payload = policy.build_payload(_request(), _samples_for(tmp_path, "f-00", "f-01", "f-02"), CFG)
    request_vision(client, model=CFG.vision_model, payload=payload, online=True)
    (call,) = client.calls
    assert call["model"] == CFG.vision_model
    assert call["messages"] == payload["messages"]


def test_no_model_fallback_when_the_call_fails() -> None:
    error = RuntimeError("Error code: 404 - model not found")
    error.status_code = 404  # type: ignore[attr-defined]
    client = FakeVisionClient(failure=error)

    with pytest.raises(RuntimeError, match="404"):
        request_vision(client, model=CFG.vision_model, payload={"messages": []}, online=True)
    assert len(client.calls) == 1  # 다른 모델로 다시 부르는 길은 없다
    assert client.calls[0]["model"] == CFG.vision_model


def test_online_round_trip_accepts_a_high_confidence_broll_block(tmp_path: Path) -> None:
    payload = policy.build_payload(_request(), _samples_for(tmp_path, "f-00", "f-01", "f-02"), CFG)
    client = FakeVisionClient(reply=_reply())
    raw = request_vision(client, model=CFG.vision_model, payload=payload, online=True)
    decision = policy.parse_decision(_request(), raw, CFG)
    assert decision.status == "accepted"
    assert decision.choice == "cand-mid"


def test_the_offline_gate_stops_before_touching_the_client() -> None:
    client = FakeVisionClient(reply=_reply())
    with pytest.raises(RuntimeError, match="online=True"):
        request_vision(client, model=CFG.vision_model, payload={"messages": []})
    assert client.calls == []


# ─── 오프라인 무호출 증명 — import 금지가 아니라 행동으로 본다 ────────────


def _openai_spy(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """가짜 openai 꾸러미 — 누군가 속성에 닿으면 기록하고 크게 실패한다."""
    touched: list[str] = []

    def _record(name: str) -> Any:
        touched.append(name)
        raise AssertionError(f"오프라인 경로가 openai.{name}에 닿았다")

    module = types.ModuleType("openai")
    module.__dict__["__getattr__"] = _record
    monkeypatch.setitem(sys.modules, "openai", module)
    return touched


def test_policy_module_never_imports_the_network_boundary() -> None:
    """조립·해석 계층은 경계를 아예 import하지 않는다 — 새 하위 프로세스에서 증명."""
    code = (
        "import sys; import vlogsrt.project.vision.policy; "
        "assert 'openai' not in sys.modules, 'openai'; "
        "assert 'vlogsrt.transcribe' not in sys.modules, 'transcribe'"
    )
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT), "PYTHONDONTWRITEBYTECODE": "1"}
    completed = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env
    )
    assert completed.returncode == 0, completed.stderr


def test_offline_project_surface_never_touches_the_vision_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """설정 읽기·inspect(FCPXML 해석)·captions 직렬화·판정 조립/해석은 공급자를 모른다."""
    touched = _openai_spy(monkeypatch)

    config_path = tmp_path / "vlogsrt.project.toml"
    config_path.write_text("schema_version = 1\n", encoding="utf-8")
    cfg = load_project_config(config_path)

    project_path = tmp_path / "project.fcpxml"
    project_path.write_text(_MINIMAL_FCPXML, encoding="utf-8")
    parse_project(project_path)

    assert serialize_srt(()) == ""

    payload = policy.build_payload(_request(), _samples_for(tmp_path, "f-00", "f-01", "f-02"), cfg)
    decision = policy.parse_decision(_request(), _reply(), cfg)
    assert "messages" in payload
    assert decision.status == "accepted"
    assert touched == []


_MINIMAL_FCPXML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    "<!DOCTYPE fcpxml>\n"
    '<fcpxml version="1.14">\n'
    "  <resources>\n"
    '    <format id="r1" name="FFVideoFormat1080p30" frameDuration="1/30s"'
    ' width="1920" height="1080"/>\n'
    '    <asset id="r2" name="source.mp4" uid="AAAA1111" start="0s" duration="600s"'
    ' hasAudio="1" hasVideo="1" format="r1">\n'
    '      <media-rep kind="original-media" src="file:///media/source.mp4"/>\n'
    "    </asset>\n"
    "  </resources>\n"
    "  <library>\n"
    '    <event name="이벤트">\n'
    '      <project name="프로젝트">\n'
    '        <sequence format="r1" tcStart="0s" duration="600s">\n'
    "          <spine>\n"
    '            <asset-clip ref="r2" name="클립1" offset="0s" start="10s"'
    ' duration="100s" tcFormat="NDF"/>\n'
    "          </spine>\n"
    "        </sequence>\n"
    "      </project>\n"
    "    </event>\n"
    "  </library>\n"
    "</fcpxml>\n"
)
