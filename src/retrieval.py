"""BM25 search over official raw artifacts and aggregate metric JSON."""
import json
import re
from functools import lru_cache
from pathlib import Path

from rank_bm25 import BM25Okapi

from .dataset import ROOT, artifact_files, metric_file

STOPWORDS = {"a", "an", "and", "are", "as", "at", "by", "did", "do", "does",
             "for", "from", "how", "in", "is", "it", "of", "on", "or", "the",
             "this", "to", "was", "were", "what", "when", "which", "with",
             "reported", "design", "process", "included", "many"}


def tokens(text: str) -> list[str]:
    words = re.findall(r"[a-z]+|\d+(?:\.\d+)?", text.lower().replace("_", " "))
    result = []
    for word in words:
        if word in STOPWORDS:
            continue
        if word == "routing":
            word = "route"
        elif len(word) > 4 and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]
        result.append(word)
    return result


def chunk_file(path: Path, lines_per_chunk: int = 24, overlap: int = 6) -> list[dict]:
    lines = path.read_text(errors="replace").splitlines()
    result = []
    for start in range(0, len(lines), lines_per_chunk - overlap):
        excerpt = "\n".join(lines[start:start + lines_per_chunk]).strip()
        if excerpt:
            result.append({"kind": "log", "source": str(path.relative_to(ROOT)),
                           "line_start": start + 1, "line_end": min(start + lines_per_chunk, len(lines)),
                           "text": excerpt})
    return result


def _unit(path: str, value) -> str:
    if isinstance(value, str):
        m = re.fullmatch(r"\s*[+-]?\d+(?:\.\d+)?\s*([a-zA-Z%µ]+)\s*", value)
        if m:
            return m.group(1)
    p = path.lower()
    if any(x in p for x in ("wns", "tns", "slack", "clock_period", "arrival_time", "required_time")):
        return "ns"
    if "power" in p and "percent" not in p:
        return "W"
    if "percent" in p or "utilization" in p:
        return "%"
    if any(x in p for x in ("wirelength", "hpwl")):
        return "um"
    return ""


def flatten_metrics(design: str) -> list[dict]:
    source = metric_file(design)
    data = json.loads(source.read_text())
    records = []

    def visit(node, path: str, stage: str):
        if isinstance(node, dict):
            stage = str(node.get("stage", stage)) if isinstance(node.get("stage", stage), str) else stage
            for k, v in node.items():
                visit(v, f"{path}.{k}" if path else k, stage)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                visit(v, f"{path}[{i}]", stage)
        elif node is not None and isinstance(node, (str, int, float, bool)):
            # Long descriptions and timestamps add little searchable metric value.
            if isinstance(node, str) and len(node) > 250:
                return
            value = node
            unit = _unit(path, value)
            if isinstance(value, str):
                match = re.fullmatch(r"\s*([+-]?\d+(?:\.\d+)?)\s*[a-zA-Z%µ]+\s*", value)
                if match:
                    value = float(match.group(1))
            records.append({"kind": "metric", "design": design, "stage": stage,
                            "metric": path.split(".")[-1], "path": path, "value": value,
                            "unit": unit, "source": str(source.relative_to(ROOT))})

    visit(data.get("flow_execution_summary", {}), "flow_execution_summary", "flow_execution")
    visit(data.get("stages", []), "stages", "")
    return records


class Index:
    def __init__(self, design: str):
        self.design = design
        self.logs = [r for p in artifact_files(design) for r in chunk_file(p)]
        self.metrics = flatten_metrics(design)
        for prefix, records in (("L", self.logs), ("M", self.metrics)):
            for i, r in enumerate(records):
                r["id"] = f"{prefix}{i:05d}"
        # File names carry stage names that may be absent from a report excerpt.
        self.log_bm25 = BM25Okapi([tokens(Path(r["source"]).name + " " + r["text"]) for r in self.logs])
        self.metric_bm25 = BM25Okapi([tokens(r["stage"] + " " + r["path"] + " " + str(r["value"])) for r in self.metrics])

    def search(self, question: str, kind: str = "log", limit: int = 6) -> list[dict]:
        records, index = (self.logs, self.log_bm25) if kind == "log" else (self.metrics, self.metric_bm25)
        query_tokens = tokens(question)
        if kind == "metric" and re.search(r"\b(how many|number of|count of)\b", question, re.I):
            query_tokens.append("total")
        scores = index.get_scores(query_tokens)
        ranking = sorted(range(len(records)), key=lambda i: (-scores[i], i))
        if kind != "log":
            return [records[i] for i in ranking[:limit]]
        # Avoid using the entire evidence budget on overlapping windows from
        # one file when the question spans stages.
        chosen, per_source = [], {}
        for i in ranking[:limit * 8]:
            source = records[i]["source"]
            if per_source.get(source, 0) < 2:
                chosen.append(i)
                per_source[source] = per_source.get(source, 0) + 1
            if len(chosen) == limit:
                break
        for i in ranking:
            if len(chosen) == limit:
                break
            if i not in chosen:
                chosen.append(i)
        return [records[i] for i in chosen]


@lru_cache(maxsize=3)
def get_index(design: str) -> Index:
    return Index(design)


def evidence_text(record: dict, max_chars: int = 1100) -> str:
    if record["kind"] == "log":
        return f"[{record['id']}] {record['source']}:{record['line_start']}-{record['line_end']}\n{record['text'][:max_chars]}"
    # The complete source and JSON path remain in the saved evidence record.
    return f"[{record['id']}] {record['path']} = {record['value']} {record['unit']}"


def direct_context(design: str, char_budget: int = 6000) -> tuple[str, list[dict]]:
    """Fixed raw sample: main-log ends plus one report from each numbered stage."""
    files = artifact_files(design)
    main = next((p for p in files if p.suffix == ".txt"), files[0])
    groups = {}
    for path in files:
        if path != main:
            groups.setdefault(path.name.split("_", 1)[0], []).append(path)
    reports = [paths[-1] for _, paths in sorted(groups.items())]
    selected = [main] + reports
    main_share = int(char_budget * .4) if reports else char_budget
    shares = [main_share] + ([max(250, (char_budget-main_share)//len(reports))] * len(reports) if reports else [])
    pieces, records, used = [], [], 0
    for path, allowance in zip(selected, shares):
        lines = path.read_text(errors="replace").splitlines()
        half = max(0, (allowance - 180) // 2)
        head, tail = [], []
        length = 0
        for line in lines:
            if length + len(line) + 1 > half:
                break
            head.append(line)
            length += len(line) + 1
        length = 0
        for line in reversed(lines[len(head):]):
            if length + len(line) + 1 > half:
                break
            tail.append(line)
            length += len(line) + 1
        tail.reverse()
        excerpts = ((1, len(head), head),
                    (len(lines)-len(tail)+1, len(lines), tail))
        for start, end, excerpt in excerpts:
            if not excerpt:
                continue
            source = str(path.relative_to(ROOT))
            prefix = f"[D{len(records):04d}] {source}:{start}-{end}\n"
            available = char_budget - used - (2 if pieces else 0) - len(prefix)
            if available < 20:
                break
            content = "\n".join(excerpt)[:available]
            record = {"id": f"D{len(records):04d}", "kind": "log", "source": source,
                      "line_start": start, "line_end": end, "text": content}
            records.append(record)
            pieces.append(prefix + content)
            used += len(prefix) + len(content) + (2 if len(pieces) > 1 else 0)
    return "\n\n".join(pieces), records
