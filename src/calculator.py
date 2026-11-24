"""Safe arithmetic over retrieved numeric evidence records."""
import math

OPS = {"difference", "percentage_change", "ratio", "minimum", "maximum"}


def calculate(request: dict, evidence: dict[str, dict]) -> dict:
    if not isinstance(request, dict) or set(request) != {"operation", "evidence_ids"}:
        raise ValueError("Calculation requires operation and evidence_ids")
    op, ids = request["operation"], request["evidence_ids"]
    if op not in OPS or not isinstance(ids, list) or len(ids) < 2 or len(ids) > 12 or len(set(ids)) != len(ids):
        raise ValueError("Unsupported operation or evidence IDs")
    if op in {"difference", "percentage_change", "ratio"} and len(ids) != 2:
        raise ValueError(f"{op} requires exactly two operands")
    records = []
    for eid in ids:
        if eid not in evidence or evidence[eid].get("kind") != "metric":
            raise ValueError(f"Unknown numeric metric evidence: {eid}")
        record = evidence[eid]
        value = record.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("Operands must be finite numbers")
        records.append(record)
    units = {r.get("unit", "") for r in records}
    if len(units) != 1:
        raise ValueError("Operand units differ")
    values = [float(r["value"]) for r in records]
    if op == "difference":
        result = values[0] - values[1]
    elif op == "percentage_change":
        if values[1] == 0:
            raise ValueError("Zero baseline")
        result = (values[0] - values[1]) / abs(values[1]) * 100
    elif op == "ratio":
        if values[1] == 0:
            raise ValueError("Zero denominator")
        result = values[0] / values[1]
    elif op == "minimum":
        result = min(values)
    else:
        result = max(values)
    unit = "%" if op == "percentage_change" else "" if op == "ratio" else records[0].get("unit", "")
    return {"operation": op, "evidence_ids": ids, "operands": values, "result": result, "unit": unit}
