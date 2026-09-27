"""STAB-S2B-1 — 내구 예산 원장 회귀: 예약이 곧 허가다(S2B 첫 단위).

초점은 **내구 예약** 하나다. 예약은 "보내도 된다"는 허가를 돌려주기 **전에**
디스크에 남아야 하고, 재오픈(크래시와 같은 효과) 뒤에도 그대로 차감되어
있어야 한다. 부족하면 기록을 만들지 않고 거절하고, 같은 요청 ID는 두 번
허가하지 않으며, 확정(reconcile)은 실측으로 예약 상한을 갈아끼운다.

기준선은 역사적 사실로 남긴다: 구현 전 이 파일은 ``vlogsrt.project.
budget_ledger``가 없어 **수집 RED**(exit 2, ModuleNotFoundError)였고, Sol이
깨끗한 환경에서 독립 실행으로 그 RED를 확인했다(2026-09-28). 이제 아래
행렬은 파손·주입 실패·병렬·실측 초과까지 아우른다.

모든 시험은 ``tmp_path`` 합성 JSON·신원만 쓴다. conftest의 네트워크·dotenv
가드는 이 파일의 안전망이지만 **협조적 보호망**이다 — 시험 프로세스 안의
우연한 접근을 막을 뿐 범용 격리 경계가 아니다. 원장 자체는 로컬
파일시스템의 POSIX advisory lock(flock)을 가정한다. 시각은 호출자가 주는
문자열 그대로 기록으로 남을 뿐 판정에 들어가지 않는다 — 시험이 시계에
흔들리지 않는다.
"""

from __future__ import annotations

import copy
import decimal
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from vlogsrt.project import budget_ledger
from vlogsrt.project.budget_ledger import (
    BudgetLedger,
    BudgetLedgerError,
    DuplicateRequest,
    InsufficientBudget,
    LedgerCorrupt,
    LedgerIdentity,
    LedgerIdentityMismatch,
    LedgerUnavailable,
    ReconciliationConflict,
    UnknownRequest,
)
from vlogsrt.project.workflow_analysis import transcription_usage
from vlogsrt.project.workflow_protocol import ActualUsage

RESERVED_AT = "2026-09-28T00:00:00Z"
NOTED_AT = "2026-09-28T00:00:01Z"
RECONCILED_AT = "2026-09-28T00:00:02Z"
WHISPER = "whisper-1"
ATTEMPT = "attempt-1"


def _digest(label: str) -> str:
    """시험 신원용 결정적 SHA-256 — 실 엔드포인트·비밀을 전혀 모방하지 않는다."""
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _identity(**overrides: str) -> LedgerIdentity:
    fields = {
        "input_revision": _digest("input"),
        "policy_digest": _digest("policy"),
        "pricing_revision": _digest("pricing"),
        "authorization_id": _digest("authorization"),
    }
    fields.update(overrides)
    return LedgerIdentity(**fields)


def _usage(
    cost: str = "1.5",
    *,
    attempt_id: str = ATTEMPT,
    model: str = WHISPER,
    input_revision: str | None = None,
    window_id: str | None = None,
) -> ActualUsage:
    """원장 신원과 들어맞는 실측 — 곱셈 원가는 시험 밖의 일이다."""
    return ActualUsage.create(
        input_revision=input_revision or _digest("input"),
        attempt_id=attempt_id,
        window_id=window_id,
        model=model,
        input_tokens=120,
        output_tokens=0,
        audio_seconds=Fraction(90),
        cost=cost,
    )


def _reserve(ledger: BudgetLedger, request_id: str, bound: str) -> object:
    return ledger.reserve(
        request_id=request_id,
        attempt_id=ATTEMPT,
        request_digest=_digest(f"request:{request_id}"),
        model=WHISPER,
        bound=Decimal(bound),
        reserved_at=RESERVED_AT,
    )


def _open(tmp_path: Path, limit: str = "5") -> BudgetLedger:
    return BudgetLedger.open(
        tmp_path / "budget.json", identity=_identity(), approval_limit=Decimal(limit)
    )


def _reconciled_document(tmp_path: Path) -> dict[str, Any]:
    """확정 기록 한 건을 담은 정상 원장의 문서 — 파손 시험의 밑천이다."""
    ledger = _open(tmp_path)
    _reserve(ledger, "req-1", "2")
    ledger.reconcile("req-1", _usage(cost="1.5"), reconciled_at=RECONCILED_AT)
    path = tmp_path / "budget.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _write_document(path: Path, document: dict[str, Any]) -> None:
    """정준 바이트가 아니어도 읽는 계약 — 구조만 맞으면 된다."""
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")


def _mutate_record(document: dict[str, Any], **fields: Any) -> dict[str, Any]:
    mutated = copy.deepcopy(document)
    mutated["records"][0].update(fields)
    return mutated


def _mutate_usage(document: dict[str, Any], **fields: Any) -> dict[str, Any]:
    mutated = copy.deepcopy(document)
    mutated["records"][0]["usage"].update(fields)
    return mutated


def test_reservation_persists_before_permission_and_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "budget.json"
    ledger = BudgetLedger.open(path, identity=_identity(), approval_limit=Decimal("5"))

    record = ledger.reserve(
        request_id="req-1",
        attempt_id=ATTEMPT,
        request_digest=_digest("request:req-1"),
        model=WHISPER,
        bound=Decimal("2"),
        reserved_at=RESERVED_AT,
    )

    assert record.state == "reserved", "예약 직후 기록은 아직 미확정(reserved) 상태다"
    snapshot = ledger.snapshot()
    assert snapshot.measured_actual == Decimal("0"), "실측이 없는데 실측이 잡혀 있다"
    assert snapshot.reserved_unresolved == Decimal("2"), "예약 상한이 미확정 칸에 없다"
    assert snapshot.consumed_bound == Decimal("2"), "예약은 곧 상한 소비다(내구 차감)"
    assert snapshot.remaining_bound == Decimal("3"), "남은 상한이 예약만큼 줄지 않았다"

    # 크래시와 같은 효과: 확정 없이 객체를 버리고 다시 연다.
    reopened = BudgetLedger.open(path, identity=_identity(), approval_limit=Decimal("5"))
    after = reopened.snapshot()
    assert after.consumed_bound == Decimal("2"), "재오픈이 예약 차감을 잃었다(크래시 유출)"
    assert after.remaining_bound == Decimal("3"), "재오픈이 남은 상한을 되살렸다"
    assert len(after.records) == 1, "재오픈이 기록을 잃거나 늘렸다"
    assert after.records[0].request_id == "req-1"
    assert after.records[0].state == "reserved", "미확정 예약이 재오픈 뒤 상태를 바꿨다"


def test_exact_remaining_fits_and_insufficient_rejects_without_record(tmp_path: Path) -> None:
    path = tmp_path / "budget.json"
    ledger = BudgetLedger.open(path, identity=_identity(), approval_limit=Decimal("5"))
    _reserve(ledger, "req-1", "3")

    # 남은 상한과 꼭 맞는 예약은 성공해야 한다 — exact fit.
    _reserve(ledger, "req-2", "2")
    snapshot = ledger.snapshot()
    assert snapshot.consumed_bound == Decimal("5"), "꼭 맞는 예약이 전부 반영되지 않았다"
    assert snapshot.remaining_bound == Decimal("0"), "꼭 맞은 뒤 남은 상한은 0이어야 한다"

    before_bytes = path.read_bytes()
    with pytest.raises(InsufficientBudget):
        _reserve(ledger, "req-3", "0.0000001")
    after = ledger.snapshot()
    assert len(after.records) == 2, "거절된 예약이 기록으로 남았다 — 거절은 무기록이어야 한다"
    assert path.read_bytes() == before_bytes, "거절이 디스크를 건드렸다"


def test_zero_limit_allows_only_zero_bound_reservations(tmp_path: Path) -> None:
    ledger = _open(tmp_path, limit="0")
    _reserve(ledger, "req-0", "0")
    with pytest.raises(InsufficientBudget):
        _reserve(ledger, "req-1", "0.0000001")
    snapshot = ledger.snapshot()
    assert len(snapshot.records) == 1, "0USD 예약이 거절당했다 — 꼭 맞는 예약은 성공이다"
    assert snapshot.remaining_bound == Decimal("0")


def test_replayed_request_id_never_grants_a_second_send(tmp_path: Path) -> None:
    ledger = _open(tmp_path)
    _reserve(ledger, "req-1", "1")

    # 같은 매개변수라도 같은 요청 ID는 두 번째 허가를 줄 수 없다.
    with pytest.raises(DuplicateRequest):
        _reserve(ledger, "req-1", "1")
    snapshot = ledger.snapshot()
    assert len(snapshot.records) == 1, "재송신 시도가 기록을 늘렸다"
    assert snapshot.consumed_bound == Decimal("1"), "재송신 시도가 상한을 두 번 담았다"


def test_reconcile_replaces_bound_with_measured_actual_once(tmp_path: Path) -> None:
    ledger = _open(tmp_path)
    _reserve(ledger, "req-1", "2")

    record = ledger.reconcile("req-1", _usage(cost="1.5"), reconciled_at=RECONCILED_AT)
    assert record.state == "reconciled", "확정 뒤 기록 상태가 reconciled가 아니다"

    snapshot = ledger.snapshot()
    assert snapshot.measured_actual == Decimal("1.5"), "실측이 실측 칸에 없다"
    assert snapshot.reserved_unresolved == Decimal("0"), "확정된 예약이 미확정 칸에 남았다"
    assert snapshot.consumed_bound == Decimal("1.5"), "확정은 상한을 실측으로 갈아끼운다"
    assert snapshot.remaining_bound == Decimal("3.5"), "확정 뒤 남은 상한이 실측 기준이 아니다"

    # 똑같은 실측으로 두 번 확정하면 아무 일도 일어나지 않는다(멱등).
    ledger.reconcile("req-1", _usage(cost="1.5"), reconciled_at=RECONCILED_AT)
    assert ledger.snapshot().consumed_bound == Decimal("1.5"), "중복 확정이 이중 계상했다"


def test_conflicting_or_unknown_reconciliation_rejects(tmp_path: Path) -> None:
    ledger = _open(tmp_path)
    _reserve(ledger, "req-1", "2")
    ledger.reconcile("req-1", _usage(cost="1.5"), reconciled_at=RECONCILED_AT)

    with pytest.raises(ReconciliationConflict):
        ledger.reconcile("req-1", _usage(cost="1.2"), reconciled_at=RECONCILED_AT)
    assert ledger.snapshot().consumed_bound == Decimal("1.5"), "충돌 확정이 계상을 바꿨다"

    with pytest.raises(UnknownRequest):
        ledger.reconcile("req-404", _usage(), reconciled_at=RECONCILED_AT)


def test_reconcile_rejects_mismatched_attempt_model_or_input_revision(tmp_path: Path) -> None:
    ledger = _open(tmp_path)
    _reserve(ledger, "req-1", "2")

    mismatches = (
        _usage(attempt_id="attempt-2"),
        _usage(model="gpt-transcribe"),
        _usage(input_revision=_digest("other-input")),
    )
    for usage in mismatches:
        with pytest.raises(ReconciliationConflict):
            ledger.reconcile("req-1", usage, reconciled_at=RECONCILED_AT)
    snapshot = ledger.snapshot()
    assert snapshot.records[0].state == "reserved", "들어맞지 않는 실측이 확정을 만들었다"
    assert snapshot.consumed_bound == Decimal("2"), "들어맞지 않는 실측이 계상을 바꿨다"


def test_directly_built_usage_cannot_smuggle_bools_ints_or_forged_digests(
    tmp_path: Path,
) -> None:
    """직접 구성된 ActualUsage도 원장 입구에서 다시 검사된다.

    ``__post_init__``의 ``min() < 0`` 검사는 bool을 int로 놓치고, 다이제스트는
    필드와 무관하게 주입할 수 있다 — 원장은 재구성·재계산으로 그 둘을 잡는다.
    """
    ledger = _open(tmp_path)
    _reserve(ledger, "req-1", "1")

    smuggles = (
        ActualUsage(
            input_revision=_digest("input"),
            attempt_id=ATTEMPT,
            window_id=None,
            model=WHISPER,
            input_tokens=True,
            output_tokens=0,
            audio_seconds=Fraction(90),
            cost="1",
            usage_revision=_digest("forged"),
        ),
        ActualUsage(
            input_revision=_digest("input"),
            attempt_id=ATTEMPT,
            window_id=None,
            model=WHISPER,
            input_tokens=10,
            output_tokens=0,
            audio_seconds=90,
            cost="1",
            usage_revision=_digest("forged"),
        ),
        ActualUsage(
            input_revision=_digest("input"),
            attempt_id=ATTEMPT,
            window_id=None,
            model=WHISPER,
            input_tokens=10,
            output_tokens=0,
            audio_seconds=Fraction(90),
            cost="1",
            usage_revision=_digest("forged"),
        ),
    )
    for usage in smuggles:
        with pytest.raises(BudgetLedgerError):
            ledger.reconcile("req-1", usage, reconciled_at=RECONCILED_AT)
    snapshot = ledger.snapshot()
    assert snapshot.records[0].state == "reserved", "밀수 시도가 확정을 만들었다"
    assert snapshot.consumed_bound == Decimal("1"), "밀수 시도가 계상을 바꿨다"


def test_explicit_unknown_outcome_keeps_the_full_bound_consumed(tmp_path: Path) -> None:
    path = tmp_path / "budget.json"
    ledger = BudgetLedger.open(path, identity=_identity(), approval_limit=Decimal("5"))
    _reserve(ledger, "req-1", "2")

    record = ledger.mark_unknown("req-1", noted_at=NOTED_AT)
    assert record.state == "unknown", "명시적 미지수 표시 뒤 상태가 unknown이 아니다"
    snapshot = ledger.snapshot()
    assert snapshot.measured_actual == Decimal("0"), "모르는 결과가 0USD 실측으로 둔갑했다"
    assert snapshot.consumed_bound == Decimal("2"), "미지수는 상한을 그대로 소비해야 한다"
    assert snapshot.remaining_bound == Decimal("3"), "미지수가 상한을 돌려줬다(환불 금지)"

    reopened = BudgetLedger.open(path, identity=_identity(), approval_limit=Decimal("5"))
    after = reopened.snapshot()
    assert after.records[0].state == "unknown", "재오픈이 미지수 상태를 잃었다"
    assert after.consumed_bound == Decimal("2"), "재오픈이 미지수 차감을 잃었다"


def test_mark_unknown_is_idempotent_and_late_usage_still_reconciles(tmp_path: Path) -> None:
    ledger = _open(tmp_path)
    _reserve(ledger, "req-1", "2")

    twice = ledger.mark_unknown("req-1", noted_at=NOTED_AT)
    again = ledger.mark_unknown("req-1", noted_at="2026-09-28T00:00:03Z")
    assert twice.state == "unknown" and again.state == "unknown", "중복 미지수 표시가 거절됐다"

    # 미지수 뒤에 늦게 도착한 실측은 여전히 확정된다 — 상한은 실측으로 바뀐다.
    ledger.reconcile("req-1", _usage(cost="1.5"), reconciled_at=RECONCILED_AT)
    snapshot = ledger.snapshot()
    assert snapshot.records[0].state == "reconciled", "늦은 실측이 확정되지 못했다"
    assert snapshot.consumed_bound == Decimal("1.5"), "늦은 실측이 계상을 바꾸지 못했다"

    with pytest.raises(ReconciliationConflict):
        ledger.mark_unknown("req-1", noted_at=NOTED_AT)
    with pytest.raises(UnknownRequest):
        ledger.mark_unknown("req-404", noted_at=NOTED_AT)


def test_reopen_rejects_different_identity_or_limit_without_touching_disk(tmp_path: Path) -> None:
    path = tmp_path / "budget.json"
    ledger = BudgetLedger.open(path, identity=_identity(), approval_limit=Decimal("5"))
    _reserve(ledger, "req-1", "1")
    before_bytes = path.read_bytes()

    with pytest.raises(LedgerIdentityMismatch):
        BudgetLedger.open(path, identity=_identity(), approval_limit=Decimal("6"))
    with pytest.raises(LedgerIdentityMismatch):
        BudgetLedger.open(
            path,
            identity=_identity(policy_digest=_digest("other-policy")),
            approval_limit=Decimal("5"),
        )
    assert path.read_bytes() == before_bytes, "거절된 재오픈이 디스크를 건드렸다"

    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["approval_limit"] == "5", "원장의 승인 상한이 바뀌었다"


def test_ledger_file_holds_only_digest_shaped_identity(tmp_path: Path) -> None:
    """원장은 신원을 네 다이제스트로만 안다 — 비밀·엔드포인트 원문은 애초에 없다."""
    path = tmp_path / "budget.json"
    ledger = BudgetLedger.open(path, identity=_identity(), approval_limit=Decimal("5"))
    _reserve(ledger, "req-1", "1")

    document = json.loads(path.read_text(encoding="utf-8"))
    assert set(document["identity"]) == {
        "input_revision",
        "policy_digest",
        "pricing_revision",
        "authorization_id",
    }, "원장 신원 블록이 계약의 네 다이제스트 밖에 값을 담았다"
    assert "totals" not in document, "합계가 저장됐다 — 합계는 매번 기록에서 다시 계산한다"


def test_snapshot_and_records_expose_the_full_contract(tmp_path: Path) -> None:
    path = tmp_path / "budget.json"
    ledger = BudgetLedger.open(path, identity=_identity(), approval_limit=Decimal("5"))
    _reserve(ledger, "req-1", "2")
    usage = _usage(cost="1.5", window_id="window-7")
    ledger.reconcile("req-1", usage, reconciled_at=RECONCILED_AT)

    snapshot = ledger.snapshot()
    assert snapshot.identity == _identity(), "스냅샷 신원이 열 때의 것과 다르다"
    assert snapshot.approval_limit == Decimal("5"), "스냅샷 상한이 열 때의 것과 다르다"
    record = snapshot.records[0]
    assert record.bound == Decimal("2")
    assert record.attempt_id == ATTEMPT and record.model == WHISPER
    assert record.usage is not None
    assert record.usage.cost == "1.5"
    assert record.usage.window_id == "window-7", "창 ID가 왕복에서 살아남지 못했다"

    reopened = BudgetLedger.open(path, identity=_identity(), approval_limit=Decimal("5"))
    stored = reopened.snapshot().records[0].usage
    assert stored is not None
    assert stored.usage_revision == usage.usage_revision, "재오픈이 사용량 다이제스트를 바꿨다"


def test_identity_and_limit_validation_at_api_entry(tmp_path: Path) -> None:
    with pytest.raises(BudgetLedgerError):
        LedgerIdentity(
            input_revision="not-a-digest",
            policy_digest=_digest("policy"),
            pricing_revision=_digest("pricing"),
            authorization_id=_digest("authorization"),
        )
    path = tmp_path / "budget.json"
    for bad_limit in (Decimal("-1"), Decimal("NaN"), Decimal("Infinity"), 5):
        with pytest.raises(BudgetLedgerError):
            BudgetLedger.open(path, identity=_identity(), approval_limit=bad_limit)
    assert not path.exists(), "거절된 열기가 원장 파일을 만들었다"


def test_reserve_rejects_malformed_request_fields_without_record(tmp_path: Path) -> None:
    path = tmp_path / "budget.json"
    ledger = BudgetLedger.open(path, identity=_identity(), approval_limit=Decimal("5"))
    before_bytes = path.read_bytes()
    attempts = (
        {"request_id": "", "bound": Decimal("1")},
        {"request_id": "req-1", "attempt_id": ""},
        {"request_id": "req-1", "request_digest": "not-a-digest", "bound": Decimal("1")},
        {"request_id": "req-1", "model": "", "bound": Decimal("1")},
        {"request_id": "req-1", "bound": Decimal("-1")},
        {"request_id": "req-1", "bound": Decimal("NaN")},
        {"request_id": "req-1", "bound": 1},
        {"request_id": "req-1", "bound": Decimal("1"), "reserved_at": ""},
    )
    for overrides in attempts:
        kwargs: dict[str, Any] = {
            "request_id": "req-1",
            "attempt_id": ATTEMPT,
            "request_digest": _digest("request:req-1"),
            "model": WHISPER,
            "bound": Decimal("1"),
            "reserved_at": RESERVED_AT,
        }
        kwargs.update(overrides)
        with pytest.raises(BudgetLedgerError):
            ledger.reserve(**kwargs)
    assert path.read_bytes() == before_bytes, "거절이 디스크를 건드렸다"
    assert len(ledger.snapshot().records) == 0, "거절된 예약이 기록으로 남았다"


def test_actual_overrun_preserves_charge_and_blocks_positive_requests(tmp_path: Path) -> None:
    path = tmp_path / "budget.json"
    ledger = BudgetLedger.open(path, identity=_identity(), approval_limit=Decimal("5"))
    _reserve(ledger, "req-1", "1")

    # 실측이 예약 상한·승인 상한을 함께 넘었다 — 기록은 남고 초과는 숨지 않는다.
    ledger.reconcile("req-1", _usage(cost="6"), reconciled_at=RECONCILED_AT)
    snapshot = ledger.snapshot()
    assert snapshot.measured_actual == Decimal("6"), "초과 실측이 버려졌다"
    assert snapshot.consumed_bound == Decimal("6"), "초과 실측이 계상에 반영되지 않았다"
    assert snapshot.remaining_bound == Decimal("0"), "남은 상한이 음수로 노출됐다"

    with pytest.raises(InsufficientBudget):
        _reserve(ledger, "req-2", "0.5")
    _reserve(ledger, "req-3", "0")

    reopened = BudgetLedger.open(path, identity=_identity(), approval_limit=Decimal("5"))
    after = reopened.snapshot()
    stored = after.records[0].usage
    assert stored is not None and stored.cost == "6", "재오픈이 초과 증거를 덮었다"
    assert after.remaining_bound == Decimal("0"), "재오픈이 초과를 리셋했다"
    assert len(after.records) == 2, "0USD 예약이 초과 뒤 차단됐다 — 막는 것은 양의 요청이다"


def test_truncated_ledger_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "budget.json"
    _reconciled_document(tmp_path)
    raw = path.read_bytes()
    path.write_bytes(raw[: len(raw) // 2])
    corrupted = path.read_bytes()

    with pytest.raises(LedgerCorrupt):
        BudgetLedger.open(path, identity=_identity(), approval_limit=Decimal("5"))
    assert path.read_bytes() == corrupted, "거절된 오픈이 잘린 파일을 고쳤다"


_CORRUPTIONS = (
    ("unknown-version", lambda d: {**d, "ledger_version": 2}),
    ("missing-identity", lambda d: {k: v for k, v in d.items() if k != "identity"}),
    ("extra-root-key", lambda d: {**d, "surprise": True}),
    ("numeric-bound", lambda d: _mutate_record(d, bound=2)),
    ("negative-bound", lambda d: _mutate_record(d, bound="-1")),
    ("nonfinite-bound", lambda d: _mutate_record(d, bound="NaN")),
    (
        "duplicate-request-id",
        lambda d: {**copy.deepcopy(d), "records": [copy.deepcopy(d["records"][0])] * 2},
    ),
    ("reconciled-without-usage", lambda d: _mutate_record(d, usage=None)),
    ("unhashable-state", lambda d: _mutate_record(d, state={"nested": True})),
    ("out-of-domain-bound", lambda d: _mutate_record(d, bound="1E+25")),
    ("out-of-domain-cost", lambda d: _mutate_usage(d, cost="1E-43")),
    ("bool-usage-tokens", lambda d: _mutate_usage(d, input_tokens=True)),
    ("forged-usage-revision", lambda d: _mutate_usage(d, usage_revision=_digest("forged"))),
)


@pytest.mark.parametrize(("name", "mutate"), _CORRUPTIONS)
def test_corrupt_ledger_fails_closed_before_any_reservation(
    tmp_path: Path, name: str, mutate: Any
) -> None:
    path = tmp_path / "budget.json"
    _write_document(path, mutate(_reconciled_document(tmp_path)))
    corrupted = path.read_bytes()

    with pytest.raises(LedgerCorrupt):
        BudgetLedger.open(path, identity=_identity(), approval_limit=Decimal("5"))
    assert path.read_bytes() == corrupted, f"거절된 오픈이 훼손 파일을 고쳤다: {name}"


def test_external_corruption_blocks_open_objects_before_further_reservations(
    tmp_path: Path,
) -> None:
    """이미 열린 객체도 디스크를 다시 읽는다 — 훼손 뒤 예약은 없다."""
    path = tmp_path / "budget.json"
    ledger = BudgetLedger.open(path, identity=_identity(), approval_limit=Decimal("5"))
    _reserve(ledger, "req-1", "1")
    document = json.loads(path.read_text(encoding="utf-8"))
    document["ledger_version"] = 99
    _write_document(path, document)
    corrupted = path.read_bytes()

    with pytest.raises(LedgerCorrupt):
        ledger.reserve(
            request_id="req-2",
            attempt_id=ATTEMPT,
            request_digest=_digest("request:req-2"),
            model=WHISPER,
            bound=Decimal("1"),
            reserved_at=RESERVED_AT,
        )
    with pytest.raises(LedgerCorrupt):
        ledger.snapshot()
    assert path.read_bytes() == corrupted, "훼손 검사가 파일을 건드렸다"


def test_write_failure_before_replace_grants_nothing_and_poisons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "budget.json"
    ledger = BudgetLedger.open(path, identity=_identity(), approval_limit=Decimal("5"))
    initial = path.read_bytes()

    def _fail(*args: object, **kwargs: object) -> None:
        raise OSError("injected: write")

    monkeypatch.setattr(budget_ledger, "_write_durable_bytes", _fail)
    with pytest.raises(LedgerUnavailable):
        _reserve(ledger, "req-1", "2")
    assert path.read_bytes() == initial, "실패한 쓰기가 원장을 바꿨다"
    with pytest.raises(LedgerUnavailable):
        ledger.snapshot()

    reopened = BudgetLedger.open(path, identity=_identity(), approval_limit=Decimal("5"))
    assert len(reopened.snapshot().records) == 0, "허가 없는 예약이 살아남았다"


def test_replace_failure_grants_nothing_and_leaves_no_orphan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "budget.json"
    ledger = BudgetLedger.open(path, identity=_identity(), approval_limit=Decimal("5"))
    initial = path.read_bytes()

    def _fail(*args: object, **kwargs: object) -> None:
        raise OSError("injected: replace")

    monkeypatch.setattr(budget_ledger, "_replace_file", _fail)
    with pytest.raises(LedgerUnavailable):
        _reserve(ledger, "req-1", "2")
    assert path.read_bytes() == initial, "교체 실패 뒤 원장이 바뀌었다"
    assert list(tmp_path.glob(".*.tmp")) == [], "고아 임시파일이 남았다"
    with pytest.raises(LedgerUnavailable):
        ledger.reserve(
            request_id="req-2",
            attempt_id=ATTEMPT,
            request_digest=_digest("request:req-2"),
            model=WHISPER,
            bound=Decimal("1"),
            reserved_at=RESERVED_AT,
        )


def test_failure_after_replace_retains_reservation_conservatively(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """교체 뒤 실패 — 호출자는 허가를 받지 못했고, 원장은 예약을 지킨다."""
    path = tmp_path / "budget.json"
    ledger = BudgetLedger.open(path, identity=_identity(), approval_limit=Decimal("5"))

    def _fail(*args: object, **kwargs: object) -> None:
        raise OSError("injected: dir fsync")

    monkeypatch.setattr(budget_ledger, "_fsync_directory", _fail)
    with pytest.raises(LedgerUnavailable):
        _reserve(ledger, "req-1", "2")
    with pytest.raises(LedgerUnavailable):
        ledger.snapshot()

    monkeypatch.undo()
    reopened = BudgetLedger.open(path, identity=_identity(), approval_limit=Decimal("5"))
    after = reopened.snapshot()
    assert after.consumed_bound == Decimal("2"), "불확실한 쓰기가 예약을 잃었다(환불 금지)"
    assert after.records[0].state == "reserved"
    _reserve(reopened, "req-2", "3")
    assert reopened.snapshot().remaining_bound == Decimal("0"), "남은 상한 계산이 무너졌다"


def test_orphan_temp_files_are_never_read_as_approval(tmp_path: Path) -> None:
    path = tmp_path / "budget.json"
    orphan = tmp_path / ".budget.json.999.0.tmp"
    orphan.write_bytes(b"{}")

    ledger = BudgetLedger.open(path, identity=_identity(), approval_limit=Decimal("5"))
    _reserve(ledger, "req-1", "1")
    assert ledger.snapshot().consumed_bound == Decimal("1"), "고아 임시파일이 판정에 들어왔다"
    assert orphan.read_bytes() == b"{}", "고아 임시파일을 원장으로 읽었다"


def test_competing_instances_and_threads_never_overspend(tmp_path: Path) -> None:
    path = tmp_path / "budget.json"
    identity = _identity()
    BudgetLedger.open(path, identity=identity, approval_limit=Decimal("10"))
    ledgers = [
        BudgetLedger.open(path, identity=identity, approval_limit=Decimal("10")) for _ in range(4)
    ]

    def _attempt(index: int) -> str:
        try:
            _reserve(ledgers[index % 4], f"req-{index}", "1")
            return "granted"
        except InsufficientBudget:
            return "insufficient"

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(_attempt, range(20)))
    assert outcomes.count("granted") == 10, f"승인 수가 상한과 다르다: {outcomes.count('granted')}"
    assert outcomes.count("insufficient") == 10, "거절 사유가 예산 부족이 아니다"

    snapshot = BudgetLedger.open(path, identity=identity, approval_limit=Decimal("10")).snapshot()
    assert len(snapshot.records) == 10, "경쟁 쓰기가 기록을 잃었다"
    assert snapshot.consumed_bound == Decimal("10"), "경쟁 쓰기가 초과 지출했다"
    assert snapshot.remaining_bound == Decimal("0")


def test_single_instance_threads_do_not_lose_updates(tmp_path: Path) -> None:
    path = tmp_path / "budget.json"
    identity = _identity()
    ledger = BudgetLedger.open(path, identity=identity, approval_limit=Decimal("7"))

    def _attempt(index: int) -> str:
        try:
            _reserve(ledger, f"req-{index}", "1")
            return "granted"
        except InsufficientBudget:
            return "insufficient"

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(_attempt, range(12)))
    assert outcomes.count("granted") == 7, "한 객체 안의 스레드가 상한을 넘겼다"
    snapshot = BudgetLedger.open(path, identity=identity, approval_limit=Decimal("7")).snapshot()
    assert len(snapshot.records) == 7, "한 객체 안의 스레드가 기록을 잃었다"


def test_sha256_fields_reject_non_string_shapes(tmp_path: Path) -> None:
    """16진 64원소 리스트는 len·순회를 통과한다 — 문자열인지부터 본다."""
    hex_list = ["a"] * 64
    with pytest.raises(BudgetLedgerError):
        LedgerIdentity(
            input_revision=hex_list,  # type: ignore[arg-type]
            policy_digest=_digest("policy"),
            pricing_revision=_digest("pricing"),
            authorization_id=_digest("authorization"),
        )
    ledger = _open(tmp_path)
    with pytest.raises(BudgetLedgerError):
        ledger.reserve(
            request_id="req-1",
            attempt_id=ATTEMPT,
            request_digest=hex_list,  # type: ignore[arg-type]
            model=WHISPER,
            bound=Decimal("1"),
            reserved_at=RESERVED_AT,
        )
    assert len(ledger.snapshot().records) == 0, "모양이 틀린 신원이 원장에 들어왔다"


def test_duplicate_json_keys_fail_closed(tmp_path: Path) -> None:
    """json.loads는 중복 키를 뒤 값으로 조용히 덮는다 — 열기 전에 닫는다."""
    path = tmp_path / "budget.json"
    canonical = json.dumps(
        _reconciled_document(tmp_path), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )

    path.write_text(canonical.replace('"bound":"2"', '"bound":"2","bound":"9"'), encoding="utf-8")
    with pytest.raises(LedgerCorrupt):
        BudgetLedger.open(path, identity=_identity(), approval_limit=Decimal("5"))

    path.write_text(
        canonical.replace('"ledger_version":1', '"ledger_version":1,"ledger_version":2'),
        encoding="utf-8",
    )
    with pytest.raises(LedgerCorrupt):
        BudgetLedger.open(path, identity=_identity(), approval_limit=Decimal("5"))


def test_exact_arithmetic_survives_low_ambient_decimal_context(tmp_path: Path) -> None:
    """문맥 정밀도가 낮아도 작은 예약은 묻히지 않고 남은 상한은 정확하다."""
    with decimal.localcontext() as ambient:
        ambient.prec = 6
        path = tmp_path / "budget.json"
        ledger = BudgetLedger.open(path, identity=_identity(), approval_limit=Decimal("1E+24"))
        _reserve(ledger, "req-1", "0.0000001")

        snapshot = ledger.snapshot()
        assert snapshot.consumed_bound == Decimal("0.0000001"), "작은 예약이 문맥 정밀도에 묻혔다"
        with decimal.localcontext() as wide:
            wide.prec = 60
            expected_remaining = Decimal("1E+24") - Decimal("0.0000001")
        assert snapshot.remaining_bound == expected_remaining, (
            "남은 상한이 자리올림으로 되살아났다 — 초과 지출의 통로다"
        )

        reopened = BudgetLedger.open(path, identity=_identity(), approval_limit=Decimal("1E+24"))
        assert reopened.snapshot().records[0].bound == Decimal("0.0000001"), (
            "작은 예약이 저장 문면에서 자리올림됐다"
        )


def test_long_precision_usage_costs_account_exactly(tmp_path: Path) -> None:
    """낮추기 전 문맥에서 만든 실측은 좁은 문맥에서 확정해도 값이 깎이지 않는다."""
    usage = _usage(cost="1234.567891")  # 기본 문맥(넉넉)에서 먼저 만든다
    with decimal.localcontext() as ambient:
        ambient.prec = 6
        ledger = _open(tmp_path, limit="1000000")
        _reserve(ledger, "req-1", "10")
        ledger.reconcile("req-1", usage, reconciled_at=RECONCILED_AT)
        snapshot = ledger.snapshot()
        assert snapshot.measured_actual == Decimal("1234.567891"), (
            "실측 원가가 확정 문맥의 정밀도에 깎였다"
        )


def test_out_of_domain_amounts_fail_promptly_at_api_entry(tmp_path: Path) -> None:
    """거대 지수·과잉 자릿수는 계산이나 직렬화 전에 즉시 거절한다."""
    path = tmp_path / "budget.json"
    for bad_limit in (
        Decimal("1E+25"),
        Decimal("1E+999999999"),
        Decimal("1E-43"),
        Decimal("1.1234567890123456789012345678901"),
    ):
        with pytest.raises(BudgetLedgerError):
            BudgetLedger.open(path, identity=_identity(), approval_limit=bad_limit)
    assert not path.exists(), "영역 밖 금액이 원장 파일을 만들게 했다"

    ledger = _open(tmp_path)
    for bad_bound in (
        Decimal("1E+25"),
        Decimal("1E-43"),
        Decimal("1.1234567890123456789012345678901"),
    ):
        with pytest.raises(BudgetLedgerError):
            ledger.reserve(
                request_id="req-1",
                attempt_id=ATTEMPT,
                request_digest=_digest("request:req-1"),
                model=WHISPER,
                bound=bad_bound,
                reserved_at=RESERVED_AT,
            )
    assert len(ledger.snapshot().records) == 0, "영역 밖 예약이 기록으로 남았다"


def test_out_of_domain_usage_cost_rejects_reconciliation(tmp_path: Path) -> None:
    ledger = _open(tmp_path)
    _reserve(ledger, "req-1", "2")
    for bad_cost in ("1E+25", "1E-43"):
        with pytest.raises(BudgetLedgerError):
            ledger.reconcile("req-1", _usage(cost=bad_cost), reconciled_at=RECONCILED_AT)
    assert ledger.snapshot().records[0].state == "reserved", "영역 밖 실측이 확정을 만들었다"


def test_transcription_usage_costs_survive_reconcile_and_reopen(tmp_path: Path) -> None:
    """기본 문맥의 28자리 원가를 만드는 실제 조력자의 실측도 그대로 살아남는다."""
    path = tmp_path / "budget.json"
    usage = transcription_usage(
        input_revision=_digest("input"),
        attempt_id=ATTEMPT,
        model=WHISPER,
        audio_seconds=Fraction(1, 3),
        per_minute="0.006",
    )
    ledger = BudgetLedger.open(path, identity=_identity(), approval_limit=Decimal("5"))
    _reserve(ledger, "req-1", "1")
    ledger.reconcile("req-1", usage, reconciled_at=RECONCILED_AT)
    snapshot = ledger.snapshot()
    assert snapshot.measured_actual == Decimal(usage.cost), "실제 조력자의 원가가 확정에서 깎였다"

    reopened = BudgetLedger.open(path, identity=_identity(), approval_limit=Decimal("5"))
    after = reopened.snapshot()
    stored = after.records[0].usage
    assert stored is not None
    assert stored.usage_revision == usage.usage_revision, "재오픈이 사용량 다이제스트를 바꿨다"
    assert after.measured_actual == Decimal(usage.cost), "재오픈이 원가를 반올림했다"


def test_mixed_concurrent_reserve_and_reconcile_keeps_exact_accounting(tmp_path: Path) -> None:
    """확정은 예약 상한을 실측으로 갈아끼운다 — 혼합 경쟁 뒤 장부는 정확히 닫힌다.

    이 작업량에서는 16건 전부 들어간다: 어느 시점에도 미확정 예약은 활성
    워커 수(6)를 못 넘고, 확정은 각 0.5USD다. 그래서 어떤 예약 직전의 최대
    소비도 6×1 + 10×0.5 = 11USD < 12USD — 남은 상한이 1USD 밑으로 떨어질
    방법이 없다. 예산 부족 거절의 정확한 수는 예약 전용 경쟁 시험이 맡는다.
    """
    path = tmp_path / "budget.json"
    identity = _identity()
    BudgetLedger.open(path, identity=identity, approval_limit=Decimal("12"))
    instances = [
        BudgetLedger.open(path, identity=identity, approval_limit=Decimal("12")) for _ in range(3)
    ]

    def _attempt(index: int) -> str:
        ledger = instances[index % 3]
        try:
            _reserve(ledger, f"req-{index}", "1")
        except InsufficientBudget:
            return "insufficient"
        ledger.reconcile(f"req-{index}", _usage(cost="0.5"), reconciled_at=RECONCILED_AT)
        return "reconciled"

    with ThreadPoolExecutor(max_workers=6) as pool:
        outcomes = list(pool.map(_attempt, range(16)))
    assert outcomes.count("reconciled") == 16, "들어맞아야 할 예약이 거절됐다"
    assert outcomes.count("insufficient") == 0, "남은 상한이 1USD 밑으로 떨어졌다"

    snapshot = BudgetLedger.open(path, identity=identity, approval_limit=Decimal("12")).snapshot()
    granted_ids = {f"req-{index}" for index in range(16)}
    assert {record.request_id for record in snapshot.records} == granted_ids, (
        "혼합 경쟁이 기록을 잃거나 늘렸다"
    )
    assert snapshot.measured_actual == Decimal("8"), "동시 확정이 실측을 잃거나 두 번 담았다"
    assert snapshot.reserved_unresolved == Decimal("0"), "확정한 예약이 미확정으로 남았다"
    assert snapshot.consumed_bound == Decimal("8"), "소비 상한이 실측과 어긋난다"
    assert snapshot.remaining_bound == Decimal("4"), "남은 상한이 정확하지 않다"
    assert all(record.state == "reconciled" for record in snapshot.records), (
        "확정 예약이 경쟁 뒤 제자리로 돌아갔다"
    )


def test_open_requires_caller_provided_directory(tmp_path: Path) -> None:
    """원장은 디렉터리를 만들지 않는다 — 조상 내구성은 호출자의 계약이다.

    만들기를 맡으면 실패한 내구화를 재시도·동시 오픈이 '이미 있는 조상'으로
    건너뛰는 구멍이 생긴다. 그래서 없는 자리는 닫히고, 호출자가 만든 자리는
    그대로 쓴다.
    """
    missing = tmp_path / "deep" / "nested"
    with pytest.raises(LedgerUnavailable):
        BudgetLedger.open(
            missing / "budget.json", identity=_identity(), approval_limit=Decimal("5")
        )
    assert not (missing / "budget.json").exists(), "없는 자리에 원장이 생겼다"

    missing.mkdir(parents=True)
    ledger = BudgetLedger.open(
        missing / "budget.json", identity=_identity(), approval_limit=Decimal("5")
    )
    _reserve(ledger, "req-1", "1")
    assert ledger.snapshot().consumed_bound == Decimal("1"), "호출자가 만든 자리가 거부됐다"
