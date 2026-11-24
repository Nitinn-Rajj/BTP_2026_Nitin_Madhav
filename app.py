"""Two-tab Streamlit interface over actual benchmark outputs."""
import json
import time

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from src.dataset import DESIGNS, ROOT, ensure_dataset
from src.llm import LocalLLMClient, credentials_available
from src.qa import ask

load_dotenv(ROOT / ".env")
st.set_page_config(page_title="EDA Assistant", layout="wide")
st.title("EDA Assistant")
ask_tab, results_tab = st.tabs(["Ask Questions", "Experiment Results"])

with ask_tab:
    design = st.selectbox("Chip design", DESIGNS)
    method = st.selectbox("Method", ["Direct LLM", "Standard RAG", "Hybrid RAG"])
    question = st.text_area("Question", placeholder="What is the final WNS after routing?")
    if st.button("Ask", disabled=not question.strip()):
        if not credentials_available():
            st.error("Set LLM_API_KEY, LLM_BASE_URL and LLM_MODEL in .env to run inference.")
        else:
            try:
                ensure_dataset()
                client = LocalLLMClient()
                method_key = {"Direct LLM": "direct", "Standard RAG": "rag", "Hybrid RAG": "hybrid"}[method]
                result = ask(design, question, method_key, client)
                st.subheader("Answer")
                st.write(result["answer"] if result["answer"] is not None else "Insufficient evidence / abstained")
                st.caption(f"Response time: {result['latency_s']:.2f} s · API calls: {result['api_calls']} · Tokens: {result['usage']['total_tokens']}")
                if result.get("validation_error"):
                    st.warning(result["validation_error"])
                if result["calculation_trace"]:
                    st.subheader("Calculation trace")
                    st.json(result["calculation_trace"])
                st.subheader("Supporting evidence")
                cited = [e for e in result["evidence"] if e["id"] in result["evidence_ids"]]
                if cited:
                    for item in cited:
                        st.code(f"{item['id']} {item['source']}:{item.get('line_start', item.get('path'))}\n{item.get('text', str(item.get('value')))}")
                else:
                    st.info("No validated citations returned.")
                    with st.expander("Retrieved context"):
                        for item in result["evidence"]:
                            st.code(f"{item['id']} {item['source']}:{item.get('line_start', item.get('path'))}\n{item.get('text', str(item.get('value')))}")
            except Exception as exc:
                st.error(f"Inference unavailable: {type(exc).__name__}: {exc}")

with results_tab:
    config_path = ROOT / "results" / "experiment_config.json"
    summary_path = ROOT / "results" / "summary.csv"
    evaluation_path = ROOT / "results" / "evaluation.csv"
    if not config_path.exists():
        st.info("No experiment configured. Run python run_experiments.py.")
    else:
        config = json.loads(config_path.read_text())
        st.caption(f"Official EDAFlowQA commit: {config['upstream_commit'][:12]} · Seed: {config['seed']} · Target: {config['sample_size']} questions × 3 methods")
        if summary_path.exists():
            table = pd.read_csv(summary_path)
            st.dataframe(table, hide_index=True, width="stretch")
            complete = table.loc[table.completed == table.target, "method"].tolist()
            pending = table.loc[table.completed < table.target, "method"].tolist()
            if complete:
                st.success("All 40 questions attempted for: " + ", ".join(complete))
            if pending:
                st.info("Still pending: " + ", ".join(pending) +
                        ". Accuracy uses the full 40-question denominator for each method.")
            for name in ["overall_accuracy", "numerical_accuracy", "category_accuracy", "response_time", "api_tokens"]:
                path = ROOT / "plots" / f"{name}.png"
                if path.exists():
                    st.image(str(path), caption=name.replace("_", " ").title())
        if evaluation_path.exists():
            rows = pd.read_csv(evaluation_path)
            if len(rows):
                choice = st.selectbox("Inspect a prediction", rows.index, format_func=lambda i: f"{rows.loc[i, 'design']} · {rows.loc[i, 'id']} · {rows.loc[i, 'method']}")
                row = rows.loc[choice]
                st.write("Prediction", row["answer"])
                st.write("Reference answer", row["reference_answer"])
                st.write("Status", row.get("status"), "Reason", row.get("abstention_reason"))
                st.write("Correct", bool(row["correct"]), "Needs review", bool(row["needs_review"]))
            else:
                st.info("No local-model predictions have been saved yet.")
