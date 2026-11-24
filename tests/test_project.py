import json

import pytest

from src.calculator import calculate
from src.dataset import DESIGNS, select_questions, questions, metric_file
from src.evaluation import numerical_equal, score, summary
from src.retrieval import chunk_file, direct_context, flatten_metrics, get_index
from src.qa import ask, clean_question, _clip
from run_experiments import attempt


def test_official_dataset_and_selection():
    selected = select_questions()
    assert len(selected) == 40
    assert len({(q["design"], q["id"]) for q in selected}) == 40
    assert {q["design"] for q in selected} == set(DESIGNS)
    assert {q["type"] for q in selected} == {"flow_execution", "single_stage", "multi_stage", "stage_to_final"}
    assert select_questions() == selected


def test_reference_isolation():
    q = questions("aes_asap7")[0]
    assert "answer" not in q
    assert "answer" not in json.dumps(q).lower()


def test_retrieval_and_sources():
    metrics = flatten_metrics("aes_asap7")
    assert metrics and all("path" in m and "source" in m for m in metrics)
    index = get_index("aes_asap7")
    hits = index.search("routing WNS", "metric", 5)
    assert len(hits) == 5
    assert all(h["source"].endswith(".json") for h in hits)
    logs = index.search("routing WNS", "log", 3)
    assert all(h["line_start"] <= h["line_end"] for h in logs)
    context, direct_records = direct_context("aes_asap7")
    assert len(context) <= 6000
    assert len({r["source"] for r in direct_records}) > 1
    area_question = "In the final stage, what is the total cell area reported?"
    area_context, _ = _clip(index.search(area_question, "log", 8), 6000)
    assert "Cell type report" in area_context and "Total" in area_context
    flow_question = "How many stages were executed in the flow, and what types were included?"
    _, flow_metrics = _clip(index.search(flow_question, "metric", 12), 3480)
    paths = {record["path"] for record in flow_metrics}
    assert "flow_execution_summary.total_stages" in paths
    assert sum(path.endswith(".stage_type") for path in paths) == 6


def test_calculator_validation():
    evidence = {"M1": {"kind": "metric", "value": 12, "unit": "ns"},
                "M2": {"kind": "metric", "value": 10, "unit": "ns"}}
    assert calculate({"operation": "difference", "evidence_ids": ["M1", "M2"]}, evidence)["result"] == 2
    assert calculate({"operation": "percentage_change", "evidence_ids": ["M1", "M2"]}, evidence)["result"] == 20
    with pytest.raises(ValueError):
        calculate({"operation": "eval", "evidence_ids": ["M1", "M2"]}, evidence)
    with pytest.raises(ValueError):
        calculate({"operation": "ratio", "evidence_ids": ["M1", "fake"]}, evidence)


def test_scoring_units_and_review():
    assert numerical_equal("1000 ps", "1 ns") is True
    assert numerical_equal("1000000 um^2", "1 mm^2") is True
    assert numerical_equal("4181.54", "4181.54 um^2") is True
    assert numerical_equal("1 W", "1 mW") is False
    assert score({"answer": "1 ns", "evidence_ids": [], "evidence": []}, "1000 ps")["correct"]
    assert score({"answer": {"execute_successfully": True, "exit_code": 0}},
                 {"completed_successfully": True, "exit_code": 0})["correct"]
    assert score({"answer": "The flow completed successfully with exit code 0."},
                 {"completed_successfully": True, "exit_code": 0})["correct"]
    assert score({"answer": "equivalent prose", "evidence_ids": [], "evidence": []}, "different prose")["needs_review"]
    failure = score({"answer": None, "status": "api_failure"}, 5)
    assert failure["api_failure"] and not failure["abstained"]
    targets = summary([], select_questions())
    assert targets.numerical_n.nunique() == 1


def test_invalid_citation_is_retained_for_separate_scoring():
    class FakeClient:
        def complete(self, *_):
            return {"data": {"answer": "42", "evidence_ids": ["invented"]},
                    "calls": 1, "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}

    result = ask("aes_asap7", "How many stages?", "rag", FakeClient())
    assert result["status"] == "answered"
    assert result["answer"] == "42"
    assert result["evidence_ids"] == []
    assert result["validation_error"] == "Invalid evidence citation"
    assert result["raw_model_responses"][0]["evidence_ids"] == ["invented"]


def test_clean_prompt_question_and_failure_record(monkeypatch):
    assert clean_question("<Q2> What is the area?\\nDifficulty: easy") == "What is the area?"

    class FakeClient:
        def complete(self, _, prompt):
            assert "<Q2>" not in prompt
            assert "Difficulty:" not in prompt
            assert prompt.rfind("Question: What is the area?") > prompt.find("Evidence:")
            return {"data": {"answer": None, "evidence_ids": []},
                    "calls": 1, "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                                          "total_tokens": 2}}

    answer = ask("aes_asap7", "<Q2> What is the area?\\nDifficulty: easy", "rag", FakeClient())
    assert answer["status"] == "abstained"

    def fail(*_):
        raise ValueError("bad model JSON")

    monkeypatch.setattr("run_experiments.ask", fail)
    failure, disconnected = attempt({"design": "aes_asap7", "id": "Q1",
                                     "question": "What is the area?"}, "rag", FakeClient(), 6000)
    assert failure["status"] == "api_failure" and not disconnected


def test_hybrid_calculation_on_real_metric_records():
    question = "What is the difference in routing WNS?"
    index = get_index("aes_asap7")
    _, visible = _clip(index.search(question, "metric", 12), 2030)
    numeric = [r for r in visible if isinstance(r["value"], (int, float)) and not isinstance(r["value"], bool)]
    first = numeric[0]
    second = next(r for r in numeric[1:] if r["unit"] == first["unit"])
    ids = [first["id"], second["id"]]
    result_value = first["value"] - second["value"]

    class FakeClient:
        def __init__(self):
            self.calls = 0

        def complete(self, *_):
            self.calls += 1
            data = ({"answer": None, "evidence_ids": [],
                     "calculation_request": {"operation": "difference", "evidence_ids": ids}}
                    if self.calls == 1 else
                    {"answer": {"value": result_value, "unit": first["unit"]}, "evidence_ids": ids})
            return {"data": data, "calls": 1,
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}

    answer = ask("aes_asap7", question, "hybrid", FakeClient())
    assert answer["status"] == "answered"
    assert answer["api_calls"] == 2
    assert answer["calculation_trace"]["result"] == result_value


def test_hybrid_rejects_unverified_arithmetic():
    class FakeClient:
        def complete(self, *_):
            return {"data": {"answer": "0.043", "evidence_ids": []}, "calls": 1,
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}

    result = ask("aes_asap7", "What is the power difference between stages?", "hybrid", FakeClient())
    assert result["status"] == "abstained"
    assert result["validation_error"] == "Arithmetic answer lacks a verified calculation"
