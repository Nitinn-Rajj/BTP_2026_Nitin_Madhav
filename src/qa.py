"""Three inference methods sharing one local model and output contract."""
import json
import re
import time

from .calculator import calculate
from .retrieval import direct_context, evidence_text, get_index

CONTEXT_CHARS = 6000

SYSTEM = ("Extract the facts needed to answer the chip-design question from the supplied evidence. "
          "Return only a JSON object with answer and evidence_ids. "
          "For one numeric result, use a short string such as "
          "{\"answer\":\"12.3\",\"evidence_ids\":[\"L00001\"]}. "
          "Include a unit only if the evidence shows it; a missing unit does not make a shown number unusable. "
          "For tables, read the requested row and column. A stage label may appear after its table. "
          "For total power, use the Total row and Total Power column, not a component row. "
          "Distinguish top-level flow stages from individual tool passes. "
          "For multiple requested facts, use a concise object or list. Cite the exact IDs that support the answer. "
          "Set answer to null and evidence_ids to [] only when a required fact is absent from the evidence. "
          "Do not invent values or citations.")


def clean_question(question: str) -> str:
    question = re.sub(r"^\s*<Q\d+>\s*", "", question, flags=re.I)
    question = re.sub(r"(?:\\n|\n)\s*Difficulty:\s*\w+\s*$", "", question, flags=re.I)
    return question.strip()


def _clip(records: list[dict], char_budget: int = 6000) -> tuple[str, list[dict]]:
    kept, parts, size = [], [], 0
    for record in records:
        line = evidence_text(record, 1600)
        remaining = char_budget - size - (2 if parts else 0)
        if len(line) > remaining:
            if record["kind"] == "metric":
                continue
            if remaining < 60:
                break
            line = line[:max(0, remaining)]
        if not line:
            break
        kept_record = record.copy()
        if record["kind"] == "log":
            # The saved evidence reflects exactly the material sent to the model.
            kept_record["text"] = line.split("\n", 1)[-1]
            kept_record["line_end"] = kept_record["line_start"] + kept_record["text"].count("\n")
        kept.append(kept_record)
        parts.append(line)
        size += len(line) + (2 if len(parts) > 1 else 0)
    return "\n\n".join(parts), kept


def ask(design: str, question: str, method: str, client, context_chars: int = CONTEXT_CHARS) -> dict:
    start = time.monotonic()
    query = clean_question(question)
    if method == "direct":
        context, records = direct_context(design, context_chars)
    elif method == "rag":
        context, records = _clip(get_index(design).search(query, "log", 8), context_chars)
    elif method == "hybrid":
        index = get_index(design)
        # Metric and log evidence share the same total character budget as RAG.
        metric_context, metrics = _clip(index.search(query, "metric", 12), int(context_chars * .58))
        log_context, logs = _clip(index.search(query, "log", 5), context_chars - len(metric_context) - 2)
        context, records = metric_context + "\n\n" + log_context, metrics + logs
    else:
        raise ValueError(method)
    evidence = {r["id"]: r for r in records}
    prompt = f"Evidence:\n{context}\n\nQuestion: {query[:550]}\nReturn only the requested JSON object."
    if method == "hybrid":
        prompt += (" Request arithmetic only for a difference, percentage change, ratio, minimum, "
                   "or maximum when the required operands are visible numeric M evidence IDs. "
                   "For arithmetic, return answer:null, evidence_ids:[], "
                   "calculation_request:{operation:one of difference, percentage_change, ratio, minimum, maximum, "
                   "evidence_ids:[numeric metric IDs]}. Operand order is result operand then baseline operand.")
    call = client.complete(SYSTEM, prompt)
    data = call["data"]
    if not isinstance(data, dict):
        raise ValueError("Model response must be a JSON object")
    raw_responses = [data]
    validation_error = None
    trace = None
    calls = call["calls"]
    usage = call["usage"].copy()
    if method == "hybrid" and data.get("calculation_request"):
        try:
            trace = calculate(data["calculation_request"], evidence)
            second = client.complete(SYSTEM, f"Evidence:\n{context}\nVerified calculation: {json.dumps(trace)}\n"
                f"Question: {query[:550]}\nReturn JSON with answer as the exact computed number and unit "
                "in a short string, and evidence_ids. Cite both operand IDs.")
            data = second["data"]
            if not isinstance(data, dict):
                raise ValueError("Model response must be a JSON object")
            raw_responses.append(data)
            calls += second["calls"]
            for k in usage:
                usage[k] += second["usage"][k]
        except ValueError as exc:
            validation_error = str(exc)
            data = {"answer": None, "evidence_ids": []}
    ids = data.get("evidence_ids", [])
    if not isinstance(ids, list) or any(not isinstance(i, str) or i not in evidence for i in ids):
        validation_error = "Invalid evidence citation"
        ids = []
    answer = data.get("answer")
    if isinstance(answer, str) and not answer.strip():
        answer = None
        validation_error = "Empty model answer"
    if method == "hybrid" and trace is not None and answer is not None:
        # A calculated answer must faithfully report Python's result.
        try:
            raw_number = answer.get("value") if isinstance(answer, dict) else answer
            match = re.match(r"^\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?)", str(raw_number), re.I)
            if not match:
                raise ValueError("No numeric result")
            numeric = float(match.group(1))
            if abs(numeric-trace["result"]) > max(1e-6, abs(trace["result"])*1e-5):
                validation_error = "Calculation mismatch"
                answer, ids = None, []
        except (TypeError, ValueError, AttributeError):
            validation_error = "Unverifiable calculation answer"
            answer, ids = None, []
    if (method == "hybrid" and trace is None and answer is not None and
            re.search(r"\b(differences?|percentage change|percent change|ratio|minimum|maximum)\b",
                      query, re.I)):
        validation_error = "Arithmetic answer lacks a verified calculation"
        answer, ids = None, []
    if answer is not None and not ids and validation_error is None:
        validation_error = "Answer lacks citations"
    return {"design": design, "method": method, "question": question, "answer": answer,
            "evidence_ids": ids, "evidence": records, "calculation_trace": trace,
            "raw_model_responses": raw_responses, "validation_error": validation_error,
            "latency_s": time.monotonic()-start,
            "api_calls": calls, "usage": usage, "status": "abstained" if answer is None else "answered"}
