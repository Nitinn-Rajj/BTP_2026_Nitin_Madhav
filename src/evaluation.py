"""Reference-only scoring. This is the sole module that reads answer fields."""
import json
import math
import re
from difflib import SequenceMatcher
from pathlib import Path

import numpy as np
import pandas as pd

from .dataset import qa_file

_NUM = re.compile(r"^\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?)\s*([a-zA-Zµ%]*(?:\^2)?)\s*$", re.I)
_SCALE = {"": ("", 1), "%": ("%", 1), "ns": ("s", 1e-9), "ps": ("s", 1e-12),
          "us": ("s", 1e-6), "s": ("s", 1), "um": ("m", 1e-6), "mm": ("m", 1e-3),
          "m": ("m", 1), "um^2": ("m2", 1e-12), "mm^2": ("m2", 1e-6), "m^2": ("m2", 1),
          "mw": ("w", 1e-3), "w": ("w", 1)}


def references(design: str) -> dict[str, object]:
    return {q["id"]: q["answer"] for q in json.loads(qa_file(design).read_text())["qa_pairs"]}


def normalize(value):
    if isinstance(value, str):
        value = re.sub(r"\s+", " ", value.lower()).strip()
        return re.sub(r"[^\w.%+-]", "", value)
    if isinstance(value, list):
        return [normalize(x) for x in value]
    if isinstance(value, dict):
        return {k.lower(): normalize(v) for k, v in sorted(value.items())}
    return value


def number(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (float, int)):
        return (float(value), "")
    if not isinstance(value, str):
        return None
    m = _NUM.fullmatch(value)
    if not m:
        return None
    unit = m.group(2).lower()
    if unit not in _SCALE:
        return None
    base, scale = _SCALE[unit]
    return (float(m.group(1)) * scale, base)


def numerical_equal(pred, ref, rel_tol: float = 0.01) -> bool | None:
    a, b = number(pred), number(ref)
    if a is not None and b is not None:
        # A bare predicted number is interpreted in the reference unit when the
        # question's reference explicitly supplies that unit.
        if a[1] == "" and b[1] != "" and isinstance(ref, str):
            match = _NUM.fullmatch(ref)
            if match:
                a = (a[0] * _SCALE[match.group(2).lower()][1], b[1])
        abs_tol = {"s": 1e-12, "m": 1e-9, "m2": 1e-15, "w": 1e-9, "%": 1e-4, "": 1e-6}.get(b[1], 1e-9)
        return a[1] == b[1] and math.isclose(a[0], b[0], rel_tol=rel_tol, abs_tol=abs_tol)
    if isinstance(pred, dict) and isinstance(ref, dict) and pred.keys() == ref.keys():
        results = [numerical_equal(pred[k], ref[k], rel_tol) for k in ref]
    elif isinstance(pred, list) and isinstance(ref, list) and len(pred) == len(ref):
        results = [numerical_equal(x, y, rel_tol) for x, y in zip(pred, ref)]
    else:
        return None
    return all(results) if results and all(x is not None for x in results) else None


def semantic_equal(pred, ref) -> bool:
    """Conservative equivalence for structured answers with renamed keys.

    Values must all match. Similar keys may be paired; free-form prose still
    requires human review rather than a guessed semantic score.
    """
    if isinstance(ref, dict) and isinstance(pred, dict) and len(ref) == len(pred):
        remaining = set(pred)
        for ref_key, ref_value in ref.items():
            candidates = []
            for pred_key in remaining:
                aliases = {"content": "message", "execute_successfully": "completed_successfully"}
                a, b = str(ref_key).lower().replace("_", " "), aliases.get(str(pred_key).lower(), str(pred_key).lower()).replace("_", " ")
                overlap = set(a.split()) & set(b.split())
                similarity = SequenceMatcher(None, a, b).ratio()
                if (similarity >= .62 or overlap) and semantic_equal(pred[pred_key], ref_value):
                    candidates.append((similarity, pred_key))
            if not candidates:
                return False
            remaining.remove(max(candidates)[1])
        return True
    if isinstance(ref, list) and isinstance(pred, list) and len(ref) == len(pred):
        return all(semantic_equal(a, b) for a, b in zip(pred, ref))
    if numerical_equal(pred, ref) is True:
        return True
    return normalize(pred) == normalize(ref)


def extractive_prose_match(pred, ref) -> bool:
    """Score simple prose only if every reference field is explicitly present."""
    if not isinstance(pred, str) or len(pred) > 800:
        return False
    obj = ref[0] if isinstance(ref, list) and len(ref) == 1 else ref
    if not isinstance(obj, dict) or len(obj) > 3 or any(isinstance(v, (dict, list)) for v in obj.values()):
        return False
    text = pred.lower()
    compact = normalize(pred)
    for key, value in obj.items():
        clues = {"message": ["message", "error"], "completed_successfully": ["complete", "success"],
                 "change_in_violations": ["violation", "reduction"]}
        parts = clues.get(key, [p for p in key.lower().split("_") if p not in {"in", "of", "the", "total"}])
        if not any(p in text for p in parts):
            return False
        if isinstance(value, bool):
            if value and not ("success" in text or re.search(r"\b(?:yes|true)\b", text)):
                return False
            if not value and not re.search(r"\b(?:no|false|not)\b", text):
                return False
        elif number(value) is not None:
            raw = str(value).split()[0]
            if not re.search(r"(?<![\d.])" + re.escape(raw) + r"(?!\d)", text):
                return False
        elif isinstance(value, str):
            expected = re.sub(r"^\[[^]]+\]\s*(?:error:\s*)?", "", value.lower())
            if len(expected) < 8 or normalize(expected) not in compact:
                return False
        else:
            return False
    return True


def has_numeric(value) -> bool:
    if number(value) is not None:
        return True
    if isinstance(value, dict):
        return any(has_numeric(v) for v in value.values())
    if isinstance(value, list):
        return any(has_numeric(v) for v in value)
    return False


def score(prediction: dict, reference) -> dict:
    answer = prediction.get("answer")
    exact = answer is not None and normalize(answer) == normalize(reference)
    numeric_value = numerical_equal(answer, reference) if answer is not None else None
    semantic = answer is not None and semantic_equal(answer, reference)
    extractive = extractive_prose_match(answer, reference)
    cited = prediction.get("evidence_ids", [])
    valid_ids = {e["id"] for e in prediction.get("evidence", [])}
    citation_valid = bool(cited) and isinstance(cited, list) and all(i in valid_ids for i in cited)
    # Strict correctness. Any semantic paraphrase outside these rules requires review.
    correct = bool(exact or numeric_value is True or semantic or extractive)
    review = answer is not None and not correct and numeric_value is None
    numeric_question = has_numeric(reference)
    api_failure = prediction.get("status") == "api_failure"
    return {"normalized_exact_match": bool(exact), "numerical_value_match": numeric_value,
            "numeric_question": numeric_question, "numerical_correct": correct if numeric_question else None,
            "structured_semantic_match": bool(semantic),
            "extractive_prose_match": bool(extractive),
            "correct": correct, "needs_review": review, "citation_valid": citation_valid,
            "abstained": answer is None and not api_failure, "api_failure": api_failure}


def evaluation_rows(predictions: list[dict], selection: list[dict]) -> list[dict]:
    refs = {d: references(d) for d in {q["design"] for q in selection}}
    lookup = {(q["design"], q["id"]): q for q in selection}
    rows = []
    for p in predictions:
        key = (p["design"], p["id"])
        if key not in lookup:
            continue
        q = lookup[key]
        ref = refs[p["design"]][p["id"]]
        rows.append({"design": p["design"], "id": p["id"], "type": q["type"], "method": p["method"],
                     "answer": json.dumps(p.get("answer")), "reference_answer": json.dumps(ref),
                     "status": p.get("status"),
                     "validation_error": p.get("validation_error"),
                     "abstention_reason": (p.get("validation_error") or "model_abstained")
                     if p.get("answer") is None and p.get("status") != "api_failure" else None,
                     **score(p, ref), "latency_s": p.get("latency_s", 0),
                     "api_calls": p.get("api_calls", 0), "prompt_tokens": p.get("usage", {}).get("prompt_tokens", 0),
                     "completion_tokens": p.get("usage", {}).get("completion_tokens", 0),
                     "total_tokens": p.get("usage", {}).get("total_tokens", 0)})
    return rows


def summary(rows: list[dict], selection: list[dict]) -> pd.DataFrame:
    target_n = len(selection)
    refs = {d: references(d) for d in {q["design"] for q in selection}}
    numeric_target_n = sum(has_numeric(refs[q["design"]][q["id"]]) for q in selection)
    frame = pd.DataFrame(rows)
    methods = ("direct", "rag", "hybrid")
    data = []
    for method in methods:
        group = frame[frame.method == method] if not frame.empty else pd.DataFrame()
        n = len(group)
        num = group["numerical_correct"].dropna() if n else pd.Series(dtype=bool)
        data.append({"method": method, "completed": n, "target": target_n,
                     "accuracy": float(group.correct.sum()/target_n) if n else 0.0,
                     "numerical_accuracy": float(num.sum()/numeric_target_n) if numeric_target_n else np.nan,
                     "numerical_n": numeric_target_n,
                     "numerical_completed": len(num), "answer_coverage": float((n-group.abstained.sum()-group.api_failure.sum())/target_n) if n else 0.0,
                     "abstentions": int(group.abstained.sum()) if n else 0,
                     "abstention_rate": float(group.abstained.sum()/target_n) if n else 0.0,
                     "api_failures": int(group.api_failure.sum()) if n else 0,
                     "api_failure_rate": float(group.api_failure.sum()/target_n) if n else 0.0,
                     "citation_valid_rate": float(group.citation_valid.sum()/target_n) if n else 0.0,
                     "review_count": int(group.needs_review.sum()) if n else 0,
                     "avg_latency_s": float(group.loc[~group.api_failure, "latency_s"].mean()) if n else np.nan,
                     "api_calls": int(group.api_calls.sum()) if n else 0,
                     "total_tokens": int(group.total_tokens.sum()) if n else 0})
    return pd.DataFrame(data)


def save_outputs(predictions: list[dict], selection: list[dict], results_dir: Path, plots_dir: Path):
    results_dir.mkdir(exist_ok=True)
    plots_dir.mkdir(exist_ok=True)
    rows = evaluation_rows(predictions, selection)
    pd.DataFrame(rows, columns=["design", "id", "type", "method", "answer", "reference_answer",
                 "status", "validation_error", "abstention_reason",
                 "normalized_exact_match", "numerical_value_match", "numeric_question", "numerical_correct",
                 "structured_semantic_match", "extractive_prose_match", "correct", "needs_review", "citation_valid",
                 "abstained", "api_failure", "latency_s", "api_calls", "prompt_tokens", "completion_tokens",
                 "total_tokens"]).to_csv(results_dir / "evaluation.csv", index=False)
    table = summary(rows, selection)
    table.to_csv(results_dir / "summary.csv", index=False)
    if rows:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        colors = ["#516fa9", "#55a898", "#dd9e4b"]
        for column, title, ylabel, filename in [
            ("accuracy", "Overall accuracy (all 40 questions)", "Correct / 40", "overall_accuracy.png"),
            ("numerical_accuracy", "Numerical question accuracy", "Correct / numeric question count", "numerical_accuracy.png"),
            ("avg_latency_s", "Average response time", "Seconds", "response_time.png"),
            ("total_tokens", "Local model token consumption", "Tokens", "api_tokens.png")]:
            fig, ax = plt.subplots(figsize=(6.4, 4))
            ax.bar(table.method, table[column], color=colors)
            ax.set(title=title, ylabel=ylabel)
            if "accuracy" in column:
                ax.set_ylim(0, 1)
            fig.tight_layout(); fig.savefig(plots_dir / filename, dpi=180); plt.close(fig)
        frame = pd.DataFrame(rows)
        numerators = frame.groupby(["type", "method"]).correct.sum()
        targets = pd.Series([q["type"] for q in selection]).value_counts()
        pivot = pd.DataFrame({method: {category: numerators.get((category, method), 0) / count
                                       for category, count in targets.items()} for method in table.method})
        fig, ax = plt.subplots(figsize=(9.2, 4.8))
        pivot.plot.bar(ax=ax, color=colors)
        for bars, method in zip(ax.containers, pivot.columns):
            for bar, category in zip(bars, pivot.index):
                count = int(numerators.get((category, method), 0))
                ax.annotate(f"{count}/{targets[category]}",
                            (bar.get_x() + bar.get_width() / 2, bar.get_height()),
                            xytext=(0, 3), textcoords="offset points",
                            ha="center", va="bottom", fontsize=8)
        ax.set(title="Full-answer accuracy by question category",
               xlabel="Category", ylabel="Accuracy", ylim=(0, 1.05))
        fig.tight_layout(); fig.savefig(plots_dir / "category_accuracy.png", dpi=180); plt.close(fig)
