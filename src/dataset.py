"""Question-only benchmark access and official artifact paths.

Reference answers are deliberately loaded only by src.evaluation.
"""
import json
import random
import re
import subprocess
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM = ROOT / "upstream_EDAFlowQA"
URL = "https://github.com/NVlabs/EDAFlowQA.git"
DESIGNS = ("aes_asap7", "aes_nangate45", "ibex_sky130hd")
CATEGORIES = ("flow_execution", "single_stage", "multi_stage", "stage_to_final")


def ensure_dataset() -> Path:
    if (UPSTREAM / "qa_release").is_dir():
        return UPSTREAM
    subprocess.run(["git", "clone", "--depth", "1", URL, str(UPSTREAM)], check=True)
    return UPSTREAM


def qa_file(design: str) -> Path:
    if design not in DESIGNS:
        raise ValueError(f"Unsupported design: {design}")
    files = sorted((ensure_dataset() / "qa_release" / design).glob("*_qa_all_types.json"))
    if len(files) != 1:
        raise FileNotFoundError(f"Expected one QA release file for {design}, found {len(files)}")
    return files[0]


def questions(design: str) -> list[dict]:
    # Strict projection prevents accidental answer leakage into inference.
    return [{"id": q["id"], "design": design, "type": q["type"],
             "question": q["question"], "stage": q.get("stage")}
            for q in json.loads(qa_file(design).read_text())["qa_pairs"]]


def _canonical(question: str) -> str:
    question = re.sub(r"<Q\d+>|Difficulty:\s*\w+", "", question, flags=re.I)
    return re.sub(r"\W+", " ", question.lower()).strip()


def select_questions(seed: int = 42, count: int = 40) -> list[dict]:
    rng = random.Random(seed)
    pools = {c: [] for c in CATEGORIES}
    seen = set()
    # Round-robin designs before shuffling ensures all three occur in each category.
    for design in DESIGNS:
        for q in questions(design):
            key = (design, _canonical(q["question"]))
            if key not in seen and q["type"] in pools:
                seen.add(key)
                pools[q["type"]].append(q)
    for pool in pools.values():
        rng.shuffle(pool)
    selected = []
    while len(selected) < count and any(pools.values()):
        for c in CATEGORIES:
            if pools[c] and len(selected) < count:
                while pools[c]:
                    candidate = pools[c].pop()
                    canonical = _canonical(candidate["question"])
                    if all(SequenceMatcher(None, canonical, _canonical(q["question"])).ratio() < 0.85
                           for q in selected):
                        selected.append(candidate)
                        break
    if len(selected) != count:
        raise ValueError(f"Only {len(selected)} unique questions available")
    return selected


def artifact_files(design: str) -> list[Path]:
    if design not in DESIGNS:
        raise ValueError(design)
    base = ensure_dataset() / "design_artifacts" / design
    return sorted(p for p in base.rglob("*") if p.is_file() and p.suffix in {".txt", ".rpt"})


def metric_file(design: str) -> Path:
    if design not in DESIGNS:
        raise ValueError(design)
    return ensure_dataset() / "extracted_design_stage_metrics" / f"test_output_{design}" / f"{design}_all_stages_metrics.json"
