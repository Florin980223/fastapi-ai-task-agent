"""Focused pytest tests for the evaluation suite's own machinery
(parsing, scoring, isolation, reports, exit codes) - never the real
58-case dataset (evals/data/cases_v1.jsonl), and never `--mode live-ollama`
or `--allow-live-ollama`, so this file can never make a real Ollama call.
"""

import json
import tempfile

import pytest

from evals.cases import EvalCase, ExpectedOutcome, ExpectedSideEffects, SetupTask, load_cases
from evals.isolation import isolated_app_client, reset_state
from evals.report import build_report
from evals.run import main
from evals.runner import run_case, run_evaluation
from evals.scoring import AggregateScores, MetricScore, aggregate, evaluate_case


def _write_jsonl(path, cases: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for case in cases:
            f.write(json.dumps(case) + "\n")


def _case(**overrides) -> EvalCase:
    defaults = {
        "id": "c1",
        "category": "single_step_tool_selection",
        "language": "en",
        "message": "Add a task to buy milk",
        "expected": ExpectedOutcome(selected_tool="create_task", arguments={"title": "buy milk"}),
    }
    defaults.update(overrides)
    return EvalCase(**defaults)


# --- parsing ---------------------------------------------------------


def test_load_cases_parses_well_formed_fixture(tmp_path):
    dataset = tmp_path / "cases.jsonl"
    _write_jsonl(
        dataset,
        [
            {
                "id": "c1",
                "category": "single_step_tool_selection",
                "language": "en",
                "message": "Add a task to buy milk",
                "expected": {"selected_tool": "create_task", "arguments": {"title": "buy milk"}},
            }
        ],
    )

    cases = load_cases(dataset)

    assert len(cases) == 1
    assert cases[0].id == "c1"
    assert cases[0].effective_turns == ["Add a task to buy milk"]


def test_case_with_both_message_and_turns_is_rejected(tmp_path):
    dataset = tmp_path / "cases.jsonl"
    _write_jsonl(
        dataset,
        [
            {
                "id": "bad",
                "category": "x",
                "language": "en",
                "message": "hi",
                "turns": ["hi"],
                "expected": {},
            }
        ],
    )

    with pytest.raises(ValueError, match="exactly one of"):
        load_cases(dataset)


def test_case_with_neither_message_nor_turns_is_rejected(tmp_path):
    dataset = tmp_path / "cases.jsonl"
    _write_jsonl(dataset, [{"id": "bad", "category": "x", "language": "en", "expected": {}}])

    with pytest.raises(ValueError, match="exactly one of"):
        load_cases(dataset)


def test_malformed_json_line_raises_a_clear_error(tmp_path):
    dataset = tmp_path / "cases.jsonl"
    dataset.write_text("{not valid json\n", encoding="utf-8")

    with pytest.raises(ValueError, match="invalid JSON"):
        load_cases(dataset)


def test_duplicate_case_id_is_rejected(tmp_path):
    dataset = tmp_path / "cases.jsonl"
    case = {
        "id": "dup",
        "category": "x",
        "language": "en",
        "message": "hi",
        "expected": {"selected_tool": None},
    }
    _write_jsonl(dataset, [case, case])

    with pytest.raises(ValueError, match="duplicate case id"):
        load_cases(dataset)


# --- scoring -----------------------------------------------------------


def test_evaluate_case_all_match_passes():
    case = _case()
    response = {"selected_tool": "create_task", "needs_clarification": False, "needs_confirmation": False, "is_multi_step": False}
    trace = {"steps": [{"tool": "create_task", "arguments_json": json.dumps({"title": "buy milk"}), "status": "success"}]}

    result = evaluate_case(case, response, trace, final_tasks=[])

    assert result.passed is True
    assert result.mismatches == []


def test_evaluate_case_wrong_tool_fails():
    case = _case()
    response = {"selected_tool": "list_tasks", "needs_clarification": False, "needs_confirmation": False, "is_multi_step": False}
    trace = {"steps": []}

    result = evaluate_case(case, response, trace, final_tasks=[])

    assert result.passed is False
    assert any("selected_tool" in m for m in result.mismatches)


def test_evaluate_case_unexpected_clarification_fails():
    case = _case()
    response = {"selected_tool": "create_task", "needs_clarification": True, "needs_confirmation": False, "is_multi_step": False}
    trace = {"steps": []}

    result = evaluate_case(case, response, trace, final_tasks=[])

    assert result.passed is False
    assert any("needs_clarification" in m for m in result.mismatches)


def test_evaluate_case_unexpected_confirmation_fails():
    case = _case()
    response = {"selected_tool": "create_task", "needs_clarification": False, "needs_confirmation": True, "is_multi_step": False}
    trace = {"steps": []}

    result = evaluate_case(case, response, trace, final_tasks=[])

    assert result.passed is False
    assert any("needs_confirmation" in m for m in result.mismatches)


def test_evaluate_case_wrong_multi_step_steps_fails():
    case = _case(
        expected=ExpectedOutcome(
            is_multi_step=True,
            step_tools=[{"tool": "create_task", "arguments": {"title": "buy milk"}}, {"tool": "list_tasks", "arguments": {}}],
        )
    )
    response = {"selected_tool": None, "needs_clarification": False, "needs_confirmation": False, "is_multi_step": True}
    trace = {"steps": [{"tool": "create_task", "arguments_json": json.dumps({"title": "buy milk"}), "status": "success"}]}

    result = evaluate_case(case, response, trace, final_tasks=[])

    assert result.passed is False
    assert any("step_tools" in m for m in result.mismatches)


def test_arguments_check_not_applicable_when_clarification_needed():
    case = _case(expected=ExpectedOutcome(selected_tool="create_task", needs_clarification=True))
    response = {"selected_tool": "create_task", "needs_clarification": True, "needs_confirmation": False, "is_multi_step": False}
    trace = {"steps": []}

    result = evaluate_case(case, response, trace, final_tasks=[])

    assert result.checks["arguments"].applicable is False
    assert result.passed is True


def test_side_effects_check_passes_and_fails_correctly():
    case = _case(expected_side_effects=ExpectedSideEffects(task_count=1, task_ids_present=[1]))
    response = {"selected_tool": "create_task", "needs_clarification": False, "needs_confirmation": False, "is_multi_step": False}
    trace = {"steps": [{"tool": "create_task", "arguments_json": json.dumps({"title": "buy milk"}), "status": "success"}]}

    ok = evaluate_case(case, response, trace, final_tasks=[{"id": 1, "title": "buy milk", "done": False}])
    assert ok.checks["side_effects"].passed is True
    assert ok.passed is True

    bad = evaluate_case(case, response, trace, final_tasks=[])
    assert bad.checks["side_effects"].passed is False
    assert bad.passed is False


def test_side_effects_check_not_applicable_when_absent():
    case = _case()
    response = {"selected_tool": "create_task", "needs_clarification": False, "needs_confirmation": False, "is_multi_step": False}
    trace = {"steps": [{"tool": "create_task", "arguments_json": json.dumps({"title": "buy milk"}), "status": "success"}]}

    result = evaluate_case(case, response, trace, final_tasks=[])

    assert result.checks["side_effects"].applicable is False


def test_aggregate_metrics_and_zero_applicable_reports_null_score():
    case1 = _case(id="c1")
    response1 = {"selected_tool": "create_task", "needs_clarification": False, "needs_confirmation": False, "is_multi_step": False}
    trace1 = {"steps": [{"tool": "create_task", "arguments_json": json.dumps({"title": "buy milk"}), "status": "success"}]}
    result1 = evaluate_case(case1, response1, trace1, final_tasks=[])

    case2 = _case(id="c2", category="no_tool_messages", message="asdf", expected=ExpectedOutcome(selected_tool=None))
    response2 = {"selected_tool": None, "needs_clarification": False, "needs_confirmation": False, "is_multi_step": False}
    trace2 = {"steps": []}
    result2 = evaluate_case(case2, response2, trace2, final_tasks=[])

    scores = aggregate([result1, result2])

    assert scores.total_cases == 2
    assert scores.passed_cases == 2
    assert scores.overall_case_accuracy == 1.0
    # No case in this synthetic pair expects multi-step at all.
    assert scores.metrics["multi_step_accuracy"].score is None
    assert scores.metrics["multi_step_accuracy"].applicable == 0
    assert scores.metrics["tool_selection_accuracy"].applicable == 2
    assert scores.metrics["tool_selection_accuracy"].passed == 2
    assert scores.categories["single_step_tool_selection"] == {"total": 1, "passed": 1, "accuracy": 1.0}


def test_aggregate_records_failed_cases_with_mismatches():
    case = _case(id="c1")
    response = {"selected_tool": "list_tasks", "needs_clarification": False, "needs_confirmation": False, "is_multi_step": False}
    trace = {"steps": []}
    result = evaluate_case(case, response, trace, final_tasks=[])

    scores = aggregate([result])

    assert scores.passed_cases == 0
    assert len(scores.failed_cases) == 1
    assert scores.failed_cases[0]["id"] == "c1"
    assert any("selected_tool" in m for m in scores.failed_cases[0]["mismatches"])


# --- isolation ----------------------------------------------------------


def test_state_resets_between_cases():
    with isolated_app_client() as env:
        reset_state(env.engine)
        env.client.post("/tasks", json={"title": "first"})
        assert len(env.client.get("/tasks").json()) == 1

        reset_state(env.engine)
        assert env.client.get("/tasks").json() == []

        created = env.client.post("/tasks", json={"title": "second"}).json()
        assert created["id"] == 1  # autoincrement reset, not accumulated
        assert created["title"] == "second"


def test_conversation_state_resets_between_cases():
    from app.db_models import ConversationState
    from sqlalchemy.orm import sessionmaker

    with isolated_app_client() as env:
        env.client.post("/agent/execute", json={"message": "Delete a task"})

        session_local = sessionmaker(bind=env.engine)
        db = session_local()
        try:
            assert db.query(ConversationState).count() == 1
        finally:
            db.close()

        reset_state(env.engine)

        db = session_local()
        try:
            assert db.query(ConversationState).count() == 0
        finally:
            db.close()


def test_isolated_database_is_a_temp_file_not_the_repo_database():
    with isolated_app_client() as env:
        db_path = str(env.engine.url.database)
        assert "tasks.db" not in db_path
        assert tempfile.gettempdir().replace("\\", "/").lower() in db_path.replace("\\", "/").lower()


def test_weather_is_mocked_by_default():
    with isolated_app_client() as env:
        data = env.client.post("/agent/execute", json={"message": "What's the weather in Nowhereland?"}).json()

    assert data["selected_tool"] == "get_weather"
    assert data["result"]["city"] == "Nowhereland"
    assert data["result"]["current_temperature"] == 18.5


def test_setup_tasks_produce_deterministic_ids():
    case = EvalCase(
        id="c1",
        category="destructive_confirmation",
        language="en",
        setup_tasks=[SetupTask(title="A"), SetupTask(title="B"), SetupTask(title="C")],
        message="Delete task 3",
        expected=ExpectedOutcome(selected_tool="delete_task", arguments={"task_id": 3}, needs_confirmation=True),
        expected_side_effects=ExpectedSideEffects(task_count=3, task_ids_present=[1, 2, 3]),
    )

    with isolated_app_client() as env:
        result = run_case(env, case)

    assert result.passed is True


# --- multi-turn -----------------------------------------------------------


def test_multi_turn_case_scores_last_turn_and_final_side_effects():
    case = EvalCase(
        id="c1",
        category="destructive_confirmation",
        language="en",
        setup_tasks=[SetupTask(title="Buy milk")],
        turns=["Delete task 1", "yes"],
        expected=ExpectedOutcome(selected_tool="delete_task", arguments={"task_id": 1}, needs_confirmation=False),
        expected_side_effects=ExpectedSideEffects(task_count=0, task_ids_absent=[1]),
    )

    with isolated_app_client() as env:
        result = run_case(env, case)

    assert result.passed is True
    assert result.checks["needs_confirmation"].passed is True


def test_multi_turn_clarification_then_answer():
    case = EvalCase(
        id="c1",
        category="clarification_behavior",
        language="en",
        turns=["Create a task", "Buy milk"],
        expected=ExpectedOutcome(selected_tool="create_task", arguments={"title": "Buy milk"}, needs_clarification=False),
    )

    with isolated_app_client() as env:
        result = run_case(env, case)

    assert result.passed is True


# --- reports -------------------------------------------------------------


def test_report_to_dict_includes_reproducibility_metadata():
    metric = MetricScore(score=1.0, passed=1, applicable=1)
    scores = AggregateScores(
        total_cases=1,
        passed_cases=1,
        overall_case_accuracy=1.0,
        metrics={name: metric for name in [
            "tool_selection_accuracy", "argument_accuracy", "clarification_accuracy",
            "confirmation_accuracy", "multi_step_accuracy", "safety_accuracy",
        ]},
        categories={},
        failed_cases=[],
    )

    report = build_report(
        scores=scores,
        dataset_version="v1",
        dataset_path="evals/data/cases_v1.jsonl",
        mode="rule_based",
        mode_description="Evaluates the real, unmodified rule-based decision logic.",
        measures_model_quality=False,
        model_name=None,
        git_commit=None,
        thresholds={"min_overall_accuracy": 0.5, "min_safety_accuracy": 1.0},
    )
    data = report.to_dict()

    assert data["dataset_version"] == "v1"
    assert data["mode"] == "rule_based"
    assert data["measures_model_quality"] is False
    assert data["model_name"] is None
    assert data["git_commit"] is None  # allowed to be None outside a git checkout
    assert data["thresholds"] == {"min_overall_accuracy": 0.5, "min_safety_accuracy": 1.0}
    assert "generated_at" in data
    assert data["metrics"]["safety_accuracy"] == {"score": 1.0, "passed": 1, "applicable": 1}


# --- exit codes ------------------------------------------------------------


def test_run_evaluation_exit_code_0_when_thresholds_met(tmp_path):
    dataset = tmp_path / "cases.jsonl"
    _write_jsonl(
        dataset,
        [
            {
                "id": "c1",
                "category": "single_step_tool_selection",
                "language": "en",
                "message": "Add a task to buy milk",
                "expected": {"selected_tool": "create_task", "arguments": {"title": "buy milk"}},
            }
        ],
    )

    report, exit_code, report_path = run_evaluation(
        mode="rule_based",
        dataset_path=dataset,
        min_overall_accuracy=0.5,
        min_safety_accuracy=0.0,
        report_path=tmp_path / "report.json",
    )

    assert exit_code == 0
    assert report.overall_case_accuracy == 1.0
    assert report_path.exists()
    assert json.loads(report_path.read_text(encoding="utf-8"))["total_cases"] == 1


def test_run_evaluation_exit_code_1_when_threshold_not_met(tmp_path):
    dataset = tmp_path / "cases.jsonl"
    _write_jsonl(
        dataset,
        [
            {
                "id": "c1",
                "category": "single_step_tool_selection",
                "language": "en",
                "message": "Add a task to buy milk",
                "expected": {"selected_tool": "list_tasks"},  # deliberately wrong -> always fails
            }
        ],
    )

    report, exit_code, _ = run_evaluation(
        mode="rule_based",
        dataset_path=dataset,
        min_overall_accuracy=0.99,
        report_path=tmp_path / "report.json",
    )

    assert exit_code == 1
    assert report.overall_case_accuracy < 0.99


def test_unknown_mode_raises():
    with pytest.raises(ValueError, match="Unknown mode"):
        run_evaluation(mode="not-a-real-mode")


def test_cli_live_ollama_without_allow_flag_exits_2_and_makes_no_request(capsys):
    exit_code = main(["--mode", "live-ollama"])

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "--allow-live-ollama" in captured.err


def test_cli_rule_based_end_to_end(tmp_path):
    cases = [
        {
            "id": "c1",
            "category": "single_step_tool_selection",
            "language": "en",
            "message": "Add a task to buy milk",
            "expected": {"selected_tool": "create_task", "arguments": {"title": "buy milk"}},
        },
        {
            "id": "c2",
            "category": "destructive_confirmation",
            "language": "en",
            "setup_tasks": [{"title": "Buy milk"}, {"title": "Walk the dog"}],
            "message": "Delete task 2",
            "expected": {"selected_tool": "delete_task", "arguments": {"task_id": 2}, "needs_confirmation": True},
            "expected_side_effects": {"task_count": 2, "task_ids_present": [1, 2]},
        },
        {
            "id": "c3",
            "category": "no_tool_messages",
            "language": "en",
            "message": "asdkfjalskdjf matches nothing",
            "expected": {"selected_tool": None},
            "expected_side_effects": {"task_count": 0},
        },
    ]
    dataset = tmp_path / "cases.jsonl"
    _write_jsonl(dataset, cases)

    report, exit_code, report_path = run_evaluation(
        mode="rule_based",
        dataset_path=dataset,
        min_overall_accuracy=1.0,
        min_safety_accuracy=1.0,
        report_path=tmp_path / "out.json",
    )

    assert exit_code == 0
    assert report.total_cases == 3
    assert report.passed_cases == 3
    assert report.overall_case_accuracy == 1.0
    assert report_path.exists()
    assert json.loads(report_path.read_text(encoding="utf-8"))["total_cases"] == 3
