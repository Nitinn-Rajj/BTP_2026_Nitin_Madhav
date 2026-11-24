# EDA Assistant

An independent, small comparison of direct local-model prompting, BM25 retrieval over raw EDA logs, and BM25 plus structured metric retrieval and Python arithmetic. The data comes from [NVIDIA EDAFlowQA](https://github.com/NVlabs/EDAFlowQA), Apache 2.0; its license is reproduced in `LICENSE-EDAFlowQA`. The upstream repository is cloned unchanged into `upstream_EDAFlowQA/`. This project has its own implementation and does not copy upstream source code.

## Setup

Python 3.10+ is required. Network access is needed once to clone the public dataset and install dependencies.

Start `ollama serve` in another terminal if Ollama is not already running. From this project directory:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
.venv/bin/pytest -q
.venv/bin/python run_experiments.py --fresh --methods direct rag
.venv/bin/python run_experiments.py --methods hybrid
.venv/bin/streamlit run app.py
```

Use `--fresh` only when starting a new comparison: it archives previous partial results under `results/archives/`. If Direct/RAG stops early, resume with `.venv/bin/python run_experiments.py --methods direct rag` **without** `--fresh`. Run Hybrid later with `.venv/bin/python run_experiments.py --methods hybrid`, also without `--fresh`, so it adds rows to the same 40-question comparison. To make a small optional smoke run first, use `--smoke-only --methods direct rag` or `--smoke-only --methods hybrid`; smoke rows are separate from benchmark predictions. Use `--retry-failures --methods direct rag` or `--retry-failures --methods hybrid` to retry saved API failures.

The runner automatically clones EDAFlowQA if absent. It checks the configured model via the local `/v1/models` endpoint and makes a real JSON chat request before starting. Ollama's native chat endpoint is derived from `LLM_BASE_URL` to request JSON and disable hidden thinking. A full command runs only the requested methods; smoke testing is optional. Each benchmark attempt is appended to `results/predictions.jsonl` and resumed by design, question ID, and method. An individual generation failure is saved and the runner continues; a disconnected local server stops the run for later resumption. A file lock prevents concurrent runners from mixing predictions.

## Methodology

- The sample uses seed 42, round-robin category selection, and exact normalized question deduplication, across `aes_asap7`, `aes_nangate45`, and `ibex_sky130hd`. IDs and upstream commit are in `results/experiment_config.json`. The category `flow_execution` has fewer than ten unique available questions, so exact balance may be impossible.
- Direct takes fixed head/tail samples from the main raw log and one representative report per numbered stage. RAG splits actual logs and reports into 24-line chunks with 6-line overlap and selects BM25 hits, indexing report filenames to expose stage names and diversifying source files. Hybrid combines BM25 metrics from the official aggregate JSON with BM25 raw chunks. Retrieval normalizes a few common term variants and expands count queries to include total metrics. All methods use the same `qwen3:4b` model, temperature zero, an explicit 8,192-token model context, default repeat penalty 1.0, 768 output tokens, and the same 6,000-character evidence cap; character count is a proxy, not a hard input-token guarantee.
- The inference prompt removes benchmark difficulty tags, puts the question after the evidence, and asks for a short answer with exact citation IDs. Raw model JSON is saved. Missing or invalid citations are flagged separately from the model's answer, so answer coverage is not artificially reduced by citation formatting. A null model answer remains an abstention.
- Hybrid permits one validated JSON arithmetic request over cited metric IDs. Python executes difference, percentage change, ratio, minimum, or maximum. The result is checked against the final answer, and extra API calls and actual token usage are recorded. Unsupported arithmetic, mismatched calculations, and arithmetic answers without a verified trace cause abstention; missing citations on other answers are recorded as citation failures.
- The reference answer is read only in `src/evaluation.py`; question loading projects away the answer field. The upstream scoring utility focuses on Pass@K with multiple samples and model-specific similarity. For this single Pass@1 run, this project uses normalized exact match, numeric comparison with unit conversion (1% relative, plus small unit-specific absolute tolerances in `experiment_config.json`). A bare predicted number is interpreted in the reference unit. Structured comparison permits renamed keys only when every value agrees. Simple prose answers are accepted only when every field of a short reference object can be extracted from the prose. Other semantic paraphrases are flagged `needs_review` and count as incorrect until reviewed. Citation validity checks IDs, not factual entailment.
- `results/evaluation.csv` stores each prediction and reference; `summary.csv` uses the full selected-question denominator for overall accuracy, coverage, and failure counts. Numerical accuracy is full-answer correctness on questions whose references contain a number, with the same numeric-question denominator for every method; `numerical_value_match` separately records direct numeric comparison. Missing predictions remain missing and count as incorrect in rate denominators. Average latency excludes API failures because they have no valid response; call counts include attempts. The five PNG plots are generated only from saved predictions; no example results are embedded. Smoke and connectivity calls are recorded separately from benchmark totals.

## Results and limitations

This project does not claim the paper's reported scores as its own findings. Full benchmark findings remain pending the local 40-question run. `results/smoke.jsonl` contains exploratory checks used to debug retrieval and prompting, so it is not a held-out result. Earlier partial runs are retained separately; they are excluded from a new run started with `--fresh`. The benchmark's reference answers can be complex and sometimes ambiguous. The conservative scorer may undercount semantic paraphrases, which are flagged for review. The fixed character cap can miss evidence, especially for Direct. BM25 is lexical and can retrieve irrelevant chunks. Unit inference for raw JSON numeric fields uses metric names and should be checked for individual cases. A method with 40 saved rows has been fully attempted even if some rows record API failures; those failures count against accuracy and are reported explicitly.
