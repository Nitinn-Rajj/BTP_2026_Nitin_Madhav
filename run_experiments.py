"""Resumable Pass@1 runner; Direct/RAG and Hybrid can run separately."""
import argparse
import fcntl
import json
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

from src.dataset import ROOT, ensure_dataset, select_questions
from src.evaluation import save_outputs
from src.llm import (LocalLLMClient, MAX_OUTPUT_TOKENS, MODEL_CONTEXT_TOKENS,
                     REPEAT_PENALTY, credentials_available)
from src.qa import CONTEXT_CHARS, ask

METHODS = ("direct", "rag", "hybrid")
RESULTS = ROOT / "results"
PREDICTIONS = RESULTS / "predictions.jsonl"
CONFIG = RESULTS / "experiment_config.json"
SMOKE = RESULTS / "smoke.jsonl"
PLOTS = ROOT / "plots"


def read_predictions() -> list[dict]:
    if not PREDICTIONS.exists():
        return []
    latest = {}
    for line in PREDICTIONS.read_text().splitlines():
        if line.strip():
            p = json.loads(line)
            latest[(p["design"], p["id"], p["method"])] = p
    return list(latest.values())


def append_jsonl(path: Path, row: dict) -> None:
    with path.open("a") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def archive_current() -> Path | None:
    files = [p for p in (CONFIG, PREDICTIONS, SMOKE,
                         RESULTS / "evaluation.csv", RESULTS / "summary.csv") if p.exists()]
    plots = list(PLOTS.glob("*.png"))
    if not files and not plots:
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = RESULTS / "archives" / stamp
    destination.mkdir(parents=True)
    for path in files:
        shutil.move(str(path), str(destination / path.name))
    if plots:
        (destination / "plots").mkdir()
        for path in plots:
            shutil.move(str(path), str(destination / "plots" / path.name))
    return destination


def build_config(selected: list[dict], revision: str, context_chars: int) -> dict:
    return {"seed": 42, "sample_size": len(selected),
            "designs": sorted({q["design"] for q in selected}),
            "selected_questions": [{"design": q["design"], "id": q["id"], "type": q["type"]}
                                   for q in selected],
            "upstream_url": "https://github.com/NVlabs/EDAFlowQA",
            "upstream_commit": revision, "methods": METHODS,
            "model": os.getenv("LLM_MODEL", "qwen3:4b"),
            "base_url": os.getenv("LLM_BASE_URL", "http://localhost:11434/v1"),
            "temperature": 0, "max_completion_tokens": MAX_OUTPUT_TOKENS,
            "model_context_tokens": MODEL_CONTEXT_TOKENS,
            "repeat_penalty": REPEAT_PENALTY, "context_chars": context_chars,
            "protocol_version": 8,
            "numerical_rel_tolerance": 0.01,
            "numerical_abs_tolerance_by_base_unit":
            {"s": 1e-12, "m": 1e-9, "w": 1e-9, "%": 1e-4, "unitless": 1e-6},
            "truncation": "Direct: fixed raw sample across numbered stages. RAG/Hybrid: BM25 evidence under the same character cap."}


def check_config(config: dict) -> None:
    if CONFIG.exists():
        previous = json.loads(CONFIG.read_text())
        keys = ("seed", "sample_size", "selected_questions", "upstream_commit",
                "model", "base_url", "temperature", "max_completion_tokens",
                "model_context_tokens", "repeat_penalty", "context_chars",
                "protocol_version")
        changed = [key for key in keys if previous.get(key) != config.get(key)]
        if changed:
            raise RuntimeError("Existing results use different settings (" + ", ".join(changed) +
                               "). Start a new run with --fresh; old outputs will be archived.")
    else:
        CONFIG.write_text(json.dumps(config, indent=2) + "\n")


def attempt(q: dict, method: str, client: LocalLLMClient, context_chars: int) -> tuple[dict, bool]:
    started = time.monotonic()
    try:
        result = ask(q["design"], q["question"], method, client, context_chars)
        result["id"] = q["id"]
        return result, False
    except Exception as exc:
        detail = exc.response.text[:300] if isinstance(exc, httpx.HTTPStatusError) else str(exc)[:300]
        result = {"design": q["design"], "id": q["id"], "method": method,
                  "question": q["question"], "answer": None, "evidence_ids": [],
                  "evidence": [], "calculation_trace": None, "raw_model_responses": [],
                  "status": "api_failure", "error_type": type(exc).__name__,
                  "error_detail": detail, "latency_s": time.monotonic()-started,
                  "api_calls": getattr(exc, "attempts", 1),
                  "usage": {"prompt_tokens": 0, "completion_tokens": 0,
                            "total_tokens": 0}}
        disconnected = isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout))
        return result, disconnected


def run(methods: tuple[str, ...] = ("direct", "rag"), smoke_only: bool = False,
        fresh: bool = False, retry_failures: bool = False,
        context_chars: int = CONTEXT_CHARS) -> int:
    load_dotenv(ROOT / ".env")
    RESULTS.mkdir(exist_ok=True)
    PLOTS.mkdir(exist_ok=True)
    os.environ.setdefault("MPLCONFIGDIR", str(RESULTS / ".mplconfig"))
    # This prevents two local-model runners from duplicating and mixing rows.
    with (RESULTS / ".experiment.lock").open("a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another experiment runner is active") from exc
        if fresh:
            archived = archive_current()
            if archived:
                print("Previous results archived at", archived)
        upstream = ensure_dataset()
        selected = select_questions()
        revision = subprocess.run(["git", "-C", str(upstream), "rev-parse", "HEAD"],
                                  capture_output=True, text=True, check=True).stdout.strip()
        check_config(build_config(selected, revision, context_chars))
        save_outputs(read_predictions(), selected, RESULTS, PLOTS)
        if not credentials_available():
            print("Local LLM settings unavailable. No inference attempted.")
            return 2
        try:
            client = LocalLLMClient()
            connection = client.connectivity()
            print("Local model connectivity passed:", connection["model"])
        except Exception as exc:
            print(f"Local model connectivity unavailable: {type(exc).__name__}: {exc}")
            return 2
        if smoke_only:
            smoke_questions = [next(q for q in selected if q["type"] == category)
                               for category in ("flow_execution", "single_stage", "multi_stage")]
            prior_smoke = SMOKE.read_text().splitlines() if SMOKE.exists() else []
            done = {(p["design"], p["id"], p["method"])
                    for p in (json.loads(line) for line in prior_smoke)}
            for q in smoke_questions:
                for method in methods:
                    key = (q["design"], q["id"], method)
                    if key in done:
                        continue
                    result, disconnected = attempt(q, method, client, context_chars)
                    append_jsonl(SMOKE, result)
                    print("Smoke", *key, result["status"])
                    if disconnected:
                        return 2
            return 0
        existing = {(p["design"], p["id"], p["method"]): p
                    for p in read_predictions()}
        target = len(selected) * len(methods)
        completed = sum((q["design"], q["id"], method) in existing
                        for q in selected for method in methods)
        interrupted = False
        try:
            for q in selected:
                for method in methods:
                    key = (q["design"], q["id"], method)
                    old = existing.get(key)
                    if old and (not retry_failures or old["status"] != "api_failure"):
                        continue
                    result, disconnected = attempt(q, method, client, context_chars)
                    append_jsonl(PREDICTIONS, result)
                    existing[key] = result
                    if old is None:
                        completed += 1
                    print(f"{completed}/{target} {key}: {result['status']}, "
                          f"{result['usage']['total_tokens']} tokens", flush=True)
                    if completed % 10 == 0:
                        save_outputs(read_predictions(), selected, RESULTS, PLOTS)
                    if disconnected:
                        print("Local model disconnected; saved results can be resumed.")
                        interrupted = True
                        break
                if interrupted:
                    break
        finally:
            save_outputs(read_predictions(), selected, RESULTS, PLOTS)
        print(f"Requested methods attempted: {completed}/{target}")
        return 2 if interrupted else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--methods", nargs="+", choices=METHODS, default=["direct", "rag"],
                        help="Methods to run; default: direct rag")
    parser.add_argument("--smoke-only", action="store_true")
    parser.add_argument("--fresh", action="store_true",
                        help="Archive current results and start a new experiment")
    parser.add_argument("--retry-failures", action="store_true")
    parser.add_argument("--context-chars", type=int, default=CONTEXT_CHARS)
    args = parser.parse_args()
    if args.context_chars < 1000:
        parser.error("--context-chars must be at least 1000")
    raise SystemExit(run(tuple(dict.fromkeys(args.methods)), args.smoke_only,
                         args.fresh, args.retry_failures, args.context_chars))
