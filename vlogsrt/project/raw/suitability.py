"""Project Luna's explicit visual suitability onto measured source-clock intervals."""

from __future__ import annotations

from fractions import Fraction


def rational(value: dict) -> Fraction:
    return Fraction(value["num"], value["den"])


def _window_spans(document: dict, *, completed_only: bool = False) -> list[dict]:
    annotations = {row["window_id"]: row for row in document.get("annotations", [])}
    result = []
    for window in document.get("windows", {}).get("windows", []):
        annotation = annotations.get(window["window_id"])
        if completed_only and annotation is None:
            continue
        row = annotation or {}
        trustworthy = (
            row.get("status") == "accepted"
            and not row.get("needs_review")
            and not row.get("uncertainties")
        )
        suitability = row.get("suitability", "review") if trustworthy else "review"
        recommendation = {"recommended": "recommended", "usable": "normal"}.get(
            suitability, "review_required"
        )
        reason = (
            row.get("suitability_reason")
            or row.get("defect")
            or "분석 중이거나 사용성 평가가 없습니다"
        )
        if not trustworthy and row:
            reason = (
                row.get("defect")
                or "; ".join(row.get("uncertainties", []))
                or "판정이 불확실합니다 · " + reason
            )
        result.append(
            {
                "start": window["core_start"],
                "end": window["core_end"],
                "recommendation": recommendation,
                "evaluated": row.get("suitability_evaluated", "suitability" in row),
                "reason": str(reason),
                "evidence": row.get("evidence_ids", []),
                "window_id": window["window_id"],
            }
        )
    return result


def spans(document: dict, *, completed_only: bool = False) -> list[dict]:
    windows = _window_spans(document, completed_only=completed_only)
    raw = document.get("recommendation_boundaries")
    if not raw:
        return windows
    boundaries = sorted({rational(value) for value in raw})
    result = []
    for start, end in zip(boundaries, boundaries[1:], strict=False):
        touching = [
            row for row in windows if rational(row["start"]) < end and rational(row["end"]) > start
        ]
        if not touching:
            continue
        complete = (
            sum(
                (
                    min(end, rational(row["end"])) - max(start, rational(row["start"]))
                    for row in touching
                ),
                Fraction(0),
            )
            >= end - start
        )
        kinds = {row["recommendation"] for row in touching}
        rec = (
            "review_required"
            if not complete or "review_required" in kinds
            else "recommended"
            if kinds == {"recommended"}
            else "normal"
        )
        result.append(
            {
                "start": {"num": start.numerator, "den": start.denominator},
                "end": {"num": end.numerator, "den": end.denominator},
                "recommendation": rec,
                "evaluated": complete,
                "reason": " · ".join(dict.fromkeys(row["reason"] for row in touching)),
                "evidence": list(dict.fromkeys(e for row in touching for e in row["evidence"])),
                "window_id": ",".join(row["window_id"] for row in touching),
            }
        )
    return result


def grade(document: dict, start: Fraction, end: Fraction) -> tuple[str, str]:
    rows = [
        row
        for row in spans(document)
        if rational(row["start"]) < end and rational(row["end"]) > start
    ]
    if not rows:
        return "review_required", "사용성 분석을 확인해 주세요"
    cursor = start
    for row in sorted(rows, key=lambda r: rational(r["start"])):
        if rational(row["start"]) > cursor:
            return "review_required", "분석되지 않은 구간이 포함됩니다"
        cursor = max(cursor, rational(row["end"]))
    kinds = {row["recommendation"] for row in rows}
    kind = (
        "review_required"
        if cursor < end or "review_required" in kinds
        else ("recommended" if kinds == {"recommended"} else "normal")
    )
    return kind, " · ".join(dict.fromkeys(row["reason"] for row in rows))


def safe_window_edges(document: dict) -> set[Fraction]:
    speech = [
        (rational(u["start"]) - Fraction(3, 10), rational(u["end"]) + Fraction(3, 10))
        for u in document.get("utterances", [])
        if str(u.get("text", "")).strip()
    ]
    return {
        point
        for row in spans(document)
        if row["evaluated"]
        for point in (rational(row["start"]), rational(row["end"]))
        if not any(a < point < b for a, b in speech)
    }
