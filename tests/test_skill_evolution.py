import json
from pathlib import Path

import pytest

from evogate.data import fingerprint
from evogate.evaluation import RoundService, write
from evogate.model import Model
from evogate.optimizer import MarkdownOptimizer
from evogate.service import Service
from evogate.skills import MAX_SKILL_CHARS, PROFILE, parse_document

GOOD_SKILL = """---
name: sales-workflow
description: Count sales orders and sum item revenue by region.
---

# Sales workflow

## Plan and execute

Identify whether the question counts orders or sums item revenue.
For order counts after joining items, use COUNT(DISTINCT o.id).
For revenue, sum item amounts. Apply the requested region filter.
Use execute_sql to check the query before returning the final SQL.
"""
BAD_SKILL = GOOD_SKILL.replace("COUNT(DISTINCT o.id)", "COUNT(*)")
USAGE = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}


class WorkflowModel(Model):
    """Deterministic protocol fixture, never used as performance evidence."""

    def __init__(self, proposals):
        super().__init__(live=True)
        self.proposals = iter(proposals)
        self.optimizer_inputs = []
        self.target_inputs = []
        self.calls = 0

    def complete(self, messages, tools=None, max_tokens=2048):
        self.calls += 1
        if messages[0]["content"].startswith("Evolve the SKILL.md"):
            assert max_tokens == 8192
            context = json.loads(messages[1]["content"])
            self.optimizer_inputs.append(context)
            proposal = next(self.proposals)
            if context["update_mode"] == "patch" and proposal == context["current_skill_md"]:
                proposal = {"base_sha256": context["base_sha256"], "edits": []}
            content = json.dumps(proposal) if isinstance(proposal, dict) else proposal
            return {"role": "assistant", "content": content}, USAGE
        self.target_inputs.append(json.loads(json.dumps(messages)))
        question = json.loads(next(m["content"] for m in messages
                                   if m["role"] == "user"))["question"]
        region = next(region for region in ("华东", "华南", "华北") if region in question)
        skill = next((m["content"] for m in messages
                      if m["content"].startswith("Database-local skill")), "")
        expression = ("SUM(i.amount)" if "金额" in question else
                      "COUNT(DISTINCT o.id)" if "COUNT(DISTINCT o.id)" in skill else "COUNT(*)")
        sql = (f"SELECT {expression} FROM orders o JOIN items i ON i.order_id=o.id "
               f"WHERE o.region='{region}'")
        if skill and not any(m["role"] == "tool" for m in messages):
            return {"role": "assistant", "content": "Checking the aggregate.", "tool_calls": [{
                "id": "execute1", "type": "function", "function": {
                    "name": "execute_sql", "arguments": json.dumps({"sql": sql}),
                },
            }]}, USAGE
        return {"role": "assistant", "content": sql}, USAGE


def test_bootstrap_keeps_markdown_sections_and_total_document_budget():
    document = GOOD_SKILL + "\n".join(f"\n## Step {i}\n" + "Details. " * 80 for i in range(8))
    assert 4000 < len(document) < MAX_SKILL_CHARS
    assert parse_document(document)["name"] == "sales-workflow"
    result = MarkdownOptimizer(lambda messages: ({"content": document}, USAGE)).propose(
        "schema", "", [], [],
    )
    assert result.skill == document and result.details["valid"]
    assert "rules" not in result.details


@pytest.mark.parametrize("document", [
    '{"rules": []}', "# Missing metadata", "---\nname: [bad]\n---\n# Body",
    "---\nname: skill\ndescription: yes\n---\n# Body", GOOD_SKILL + "x" * MAX_SKILL_CHARS,
])
def test_invalid_document_is_not_a_publishable_skill(document):
    with pytest.raises(ValueError):
        parse_document(document)


def test_document_repair_preserves_raw_attempts_and_usage():
    replies = iter(["```json\n{}\n```", GOOD_SKILL])
    result = MarkdownOptimizer(lambda messages: ({"content": next(replies)}, USAGE)).propose(
        "schema", "", [], [],
    )
    assert result.details["valid"] and len(result.details["attempts"]) == 2
    assert result.usage["total_tokens"] == 30
    failed = MarkdownOptimizer(lambda messages: ({"content": "{}"}, USAGE)).propose(
        "schema", "", [], [],
    )
    assert not failed.details["valid"] and failed.skill == ""
    assert len(failed.details["attempts"]) == 2


def test_multiround_document_evolution_uses_development_trajectories(tmp_path):
    model = WorkflowModel([BAD_SKILL, GOOD_SKILL])
    app = Service(tmp_path, model=model)
    db = app.db("sales-demo")
    result = app.evolve(db["id"], rounds=2)
    first, second = result["versions"]
    assert first["status"] == "pending" and second["status"] == "accepted"
    assert first["parent"] == second["parent"] == "v0"
    assert db["active"] == "v2"
    assert (tmp_path / second["skill_path"]).read_text() == GOOD_SKILL
    assert second["skill_sha256"] == fingerprint(GOOD_SKILL)
    assert "v2/SKILL.md" in second["diff"]
    context = model.optimizer_inputs[1]
    assert context["current_skill_md"] == ""
    previous = context["previous_attempt"]
    assert previous["skill_md"] == BAD_SKILL and previous["status"] == "pending"
    assert any(event["kind"] == "tool" for example in previous["development_outcomes"]
               for event in example["trace"])
    encoded = json.dumps(model.optimizer_inputs, ensure_ascii=False)
    assert all(t["question"] not in encoded for t in db["tasks"] if t["split"] != "training")
    task = db["tasks"][0]
    query = app.query(db["id"], task["question"], task_id=task["id"])
    assert query["execution"]["rows"] == [[3]]
    assert query["trace"][0]["kind"] == "skill_loaded"
    assert any(event.get("name") == "execute_sql" for event in query["trace"])
    assert any(event["kind"] == "assistant" for event in query["trace"])
    assert query["skill_path"] == second["skill_path"]
    assert GOOD_SKILL in model.target_inputs[-1][1]["content"]
    restored = Service(tmp_path, model=WorkflowModel([]))
    assert restored.version(restored.db(db["id"]))["skill"] == GOOD_SKILL
    restored.rollback(db["id"], "v0")
    baseline = restored.query(db["id"], task["question"], task_id=task["id"])
    assert baseline["execution"]["rows"] == [[5]]
    restored.rollback(db["id"], "v2")
    report = restored.compare(db["id"])
    assert report["optimizer_profile"] == PROFILE
    assert report["skill_sha256"] == fingerprint(GOOD_SKILL)
    restored.rollback(db["id"], "v0")
    assert restored.compare(db["id"]) == report


def test_unchanged_document_stops_evolution_and_preserves_accepted_parent(tmp_path):
    model = WorkflowModel([GOOD_SKILL, GOOD_SKILL])
    app = Service(tmp_path, model=model)
    result = app.evolve("sales-demo", 3)
    assert len(result["versions"]) == 2 and result["active"] == "v1"
    assert result["versions"][1]["parent"] == "v1"
    assert model.optimizer_inputs[1]["current_skill_md"] == GOOD_SKILL


def test_published_file_is_checked_on_query_and_rollback(tmp_path):
    app = Service(tmp_path)
    version = app.optimize("sales-demo")
    path = tmp_path / version["skill_path"]
    original = path.read_text()
    path.write_text(original + "\nExternal edit")
    task = app.db("sales-demo")["tasks"][0]
    with pytest.raises(ValueError, match="外部修改"):
        app.query("sales-demo", task["question"], task_id=task["id"])
    app.rollback("sales-demo", "v0")
    with pytest.raises(ValueError, match="外部修改"):
        app.rollback("sales-demo", version["id"])
    path.write_text(original)
    app.rollback("sales-demo", version["id"])
    path.unlink()
    with pytest.raises(ValueError, match="无法读取"):
        app.query("sales-demo", task["question"], task_id=task["id"])


def test_legacy_workspace_migrates_without_rewriting_skill_or_frozen_results(tmp_path):
    app = Service(tmp_path, optimizer_profile="legacy")
    version = app.optimize("sales-demo")
    report = app.compare("sales-demo")
    frozen = (tmp_path / "frozen-sales-demo.json").read_bytes()
    state = json.loads(app.file.read_text())
    old_db = state["databases"]["sales-demo"]
    branch = state.pop("skill_branches")[old_db.pop("branch_id")]
    old_db.update(versions=branch["versions"], active=branch["active"])
    state.pop("schema_version")
    for item in old_db["versions"]:
        for key in ("skill_path", "skill_sha256", "skill_format"):
            item.pop(key, None)
    app.file.write_text(json.dumps(state))
    restored = Service(tmp_path)
    migrated = restored.version(restored.db("sales-demo"))
    assert migrated["skill"] == version["skill"]
    assert migrated["skill_format"] == "legacy_text"
    assert (tmp_path / migrated["skill_path"]).read_text() == version["skill"]
    assert (tmp_path / "frozen-sales-demo.json").read_bytes() == frozen
    assert restored.compare("sales-demo") == report


def test_round_proposals_are_scoped_to_iteration_and_development_context(tmp_path):
    model = WorkflowModel([BAD_SKILL, GOOD_SKILL])
    app = RoundService(tmp_path, workers=2, model=model)
    db = app.db("sales-demo")
    for task in db["tasks"]:
        write(tmp_path / "references" / (fingerprint(task["id"])[:20] + ".json"),
              app.sql(db, task["reference_sql"]).model_dump())
    first = app.optimize(db["id"])
    second = app.optimize(db["id"])
    assert first["status"] == "pending" and second["status"] == "accepted"
    assert (tmp_path / "candidates/sales-demo/v1.json").exists()
    assert (tmp_path / "candidates/sales-demo/v2.json").exists()


def test_interrupted_selection_reuses_candidate_file_and_proposal(tmp_path, monkeypatch):
    model = WorkflowModel([GOOD_SKILL])
    app = RoundService(tmp_path, workers=1, model=model)
    db = app.db("sales-demo")
    for task in db["tasks"]:
        write(tmp_path / "references" / (fingerprint(task["id"])[:20] + ".json"),
              app.sql(db, task["reference_sql"]).model_dump())
    evaluate = app.evaluate

    def interrupted(db, tasks, skill):
        if tasks[0]["split"] == "selection":
            raise ValueError("interrupted selection")
        return evaluate(db, tasks, skill)

    monkeypatch.setattr(app, "evaluate", interrupted)
    with pytest.raises(ValueError, match="interrupted"):
        app.optimize(db["id"])
    assert db["active"] == "v0" and len(db["versions"]) == 1
    documents = list((tmp_path / "skills").rglob("SKILL.md"))
    assert len(documents) == 1 and documents[0].read_text() == GOOD_SKILL
    restored = RoundService(tmp_path, workers=1, model=WorkflowModel([]))
    version = restored.optimize(db["id"])
    assert version["status"] == "accepted"
    assert not restored.model.optimizer_inputs
    assert tmp_path / version["skill_path"] == documents[0]


def test_feedback_trajectory_is_available_to_document_optimizer(tmp_path):
    app = Service(tmp_path)
    task = app.db("sales-demo")["tasks"][0]
    query = app.query("sales-demo", task["question"], task_id=task["id"])
    app.feedback(query["id"], task["reference_sql"])
    _, corrections = app.development_context(app.db("sales-demo"), [])
    assert corrections[0]["trace"] == query["trace"]


def test_baseline_report_keeps_current_experiment_profile(tmp_path):
    assert Service(tmp_path).compare("sales-demo")["optimizer_profile"] == PROFILE


def test_bundled_demo_document_is_packaged():
    import evogate

    path = Path(evogate.__file__).parent / "skills/sales-demo/SKILL.md"
    assert parse_document(path.read_text())["name"] == "sales-analysis"


def test_batch_run_completes_multiple_rounds_then_freezes_and_resumes(tmp_path, monkeypatch):
    from evogate import evaluation

    model = WorkflowModel([BAD_SKILL, GOOD_SKILL])
    app = Service(tmp_path, model=model)
    db = app.db("sales-demo")
    for task in db["tasks"]:
        write(tmp_path / "references" / (fingerprint(task["id"])[:20] + ".json"),
              app.sql(db, task["reference_sql"]).model_dump())
    write(tmp_path / "round-protocol.json", {
        "databases": [db["id"]], "model": model.info(), "max_calls": 100,
        "optimizer_profile": PROFILE, "candidate_rounds": 2,
        "edit_budget": {"max_edits": 1, "max_changed_chars": 2000, "max_change_ratio": 0.2},
    })
    write(tmp_path / "preflight.json", {"invalid": []})
    (tmp_path / "requests").mkdir()
    (tmp_path / "requests/requests.jsonl").write_text("")
    monkeypatch.setattr(evaluation, "RecordedModel", lambda *args, **kwargs: model)
    evaluation.run(tmp_path, workers=2)
    assert model.optimizer_inputs[0]["edit_budget"]["max_edits"] == 1
    frozen = json.loads((tmp_path / "frozen-sales-demo.json").read_text())
    assert frozen["version"] == "v2" and frozen["skill"] == GOOD_SKILL
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["databases"][0]["deployed"] == "v2"
    assert len(summary["databases"][0]["rounds"]) == 2
    calls = model.calls
    evaluation.run(tmp_path, workers=2)
    assert model.calls == calls


def test_candidate_cache_detects_changed_development_input(tmp_path):
    model = WorkflowModel([GOOD_SKILL])
    app = RoundService(tmp_path, workers=1, model=model)
    db = app.db("sales-demo")
    app.propose(db, [], "")
    app.state["feedback"].append({"db_id": db["id"], "note": "New correction"})
    with pytest.raises(ValueError, match="开发上下文已改变"):
        app.propose(db, [], "")


def test_candidate_cache_detects_changed_edit_budget(tmp_path):
    from evogate.patches import EditBudget

    app = RoundService(tmp_path, workers=1, model=WorkflowModel([GOOD_SKILL]))
    db = app.db("sales-demo")
    app.propose(db, [], "")
    app.edit_budget = EditBudget(max_edits=1)
    with pytest.raises(ValueError, match="开发上下文已改变"):
        app.propose(db, [], "")


def test_document_bytes_survive_crlf_and_restart(tmp_path):
    from evogate.skills import SkillStore

    text = GOOD_SKILL.replace("\n", "\r\n")
    parse_document(text)
    version = {"id": "v1", "skill": text}
    store = SkillStore(tmp_path)
    store.persist("sales-demo", version)
    assert (tmp_path / version["skill_path"]).read_bytes() == text.encode("utf-8")
    assert store.load(version) == text
    store.persist("sales-demo", version)


def test_accepted_skill_is_edited_locally_and_rejected_patch_is_remembered(tmp_path):
    patch = {"base_sha256": fingerprint(GOOD_SKILL), "reasoning": "Make the inspection explicit",
             "edits": [{"op": "replace", "target": "check the query",
                        "content": "inspect the result"}]}
    model = WorkflowModel([GOOD_SKILL, patch, GOOD_SKILL])
    app = Service(tmp_path, model=model)
    result = app.evolve("sales-demo", 3)
    initial, edited, unchanged = result["versions"]
    assert initial["status"] == "accepted" and edited["status"] == "pending"
    assert edited["parent"] == "v1" and result["active"] == "v1"
    assert edited["skill"] == GOOD_SKILL.replace("check the query", "inspect the result")
    assert (tmp_path / edited["skill_path"]).read_text() == edited["skill"]
    assert edited["optimizer"]["patch"] == patch
    assert edited["optimizer"]["edit_report"]["edits"][0]["status"] == "applied"
    assert model.optimizer_inputs[2]["rejected_updates"][-1]["patch"] == patch
    assert unchanged["skill"] == GOOD_SKILL


def test_duplicate_and_unchanged_candidates_skip_evaluation_and_keep_final_feedback(
    tmp_path, monkeypatch,
):
    patch = {"base_sha256": fingerprint(GOOD_SKILL),
             "reasoning": "Earlier draft exceeded budget",
             "edits": [{"op": "replace", "target": "check the query",
                        "content": "inspect the result"}]}
    model = WorkflowModel([patch, patch, GOOD_SKILL, patch])
    app = Service(tmp_path, model=model)
    app.bind_skill("sales-demo", app.import_skill(GOOD_SKILL)["id"])
    calls = []
    evaluate = app.evaluate

    def tracked(db, tasks, skill):
        calls.append((tasks[0]["split"], skill))
        return evaluate(db, tasks, skill)

    monkeypatch.setattr(app, "evaluate", tracked)
    first = app.optimize("sales-demo")
    assert first["selection"]["decision"] == "no_gain"
    calls.clear()
    second = app.optimize("sales-demo")
    assert calls == [("training", GOOD_SKILL)]
    assert second["selection"]["decision"] == "duplicate_candidate"
    assert second["reused_version"] == first["id"] and not second["candidate_training"]
    calls.clear()
    third = app.optimize("sales-demo")
    assert calls == [("training", GOOD_SKILL)]
    assert third["selection"]["decision"] == "unchanged"
    feedback = model.optimizer_inputs[1]["previous_attempt"]
    assert feedback["document_valid"] and feedback["document_error"] is None
    assert feedback["gate_feedback"]["decision"] == "no_gain"
    assert feedback["development_pairs"]
    reused = model.optimizer_inputs[2]["previous_attempt"]
    assert reused["development_source_version"] == first["id"]
    assert reused["development_pairs"] == feedback["development_pairs"]
    encoded = json.dumps(model.optimizer_inputs, ensure_ascii=False)
    assert all(t["question"] not in encoded for t in app.db("sales-demo")["tasks"]
               if t["split"] != "training")
    app.configure_evolution(min_net_improvements=2)
    calls.clear()
    fourth = app.optimize("sales-demo")
    assert fourth["reused_version"] is None
    assert any(split == "selection" for split, _ in calls)


def test_incomplete_candidate_scores_are_evaluated_again(tmp_path, monkeypatch):
    model = WorkflowModel([BAD_SKILL, BAD_SKILL])
    app = Service(tmp_path, model=model)
    evaluate = app.evaluate

    def unknown_score(db, tasks, skill):
        rows = evaluate(db, tasks, skill)
        if tasks[0]["split"] == "selection" and skill == BAD_SKILL:
            rows[0]["score"] = None
        return rows

    monkeypatch.setattr(app, "evaluate", unknown_score)
    first = app.optimize("sales-demo")
    assert first["selection"]["decision"] == "insufficient_validation"
    monkeypatch.setattr(app, "evaluate", evaluate)
    second = app.optimize("sales-demo")
    assert second["reused_version"] is None
    assert second["selection"]["candidate"] is not None
    assert second["candidate_training"]


def test_cached_unknown_execution_is_rechecked_without_model_calls(tmp_path, monkeypatch):
    from evogate.sql.contracts import Execution

    model = WorkflowModel([])
    app = RoundService(tmp_path, workers=4, model=model)
    db = app.db("sales-demo")
    task = next(t for t in db["tasks"] if t["split"] == "selection")
    path = (tmp_path / "observations" / db["id"] / fingerprint(GOOD_SKILL)
            / (fingerprint(task["id"])[:20] + ".json"))
    write(path, {"task_id": task["id"], "question": task["question"], "score": None,
                 "reason": "resource_limit", "sql": "SELECT * FROM orders", "trace": [],
                 "execution": {"status": "resource_limit", "rows": []}})
    write(tmp_path / "references" / (fingerprint(task["id"])[:20] + ".json"),
          Execution(status="success", rows=[(3,)]).model_dump())
    monkeypatch.setattr(app, "sql", lambda *args: Execution(
        status="resource_limit", detail="row_limit"))
    row = app.evaluate(db, [task], GOOD_SKILL)[0]
    assert row["score"] == 0 and row["reason"] == "row_limit"
    assert json.loads(path.read_text())["score"] == 0 and model.calls == 0
    assert row["trace"][-1]["kind"] == "cached_sql_recheck"
    monkeypatch.setattr(app, "sql", lambda *args: pytest.fail("Known result should be reused"))
    assert app.evaluate(db, [task], GOOD_SKILL)[0]["score"] == 0


def test_batch_snapshots_use_parallel_evaluation_and_keep_task_order(tmp_path, monkeypatch):
    import threading

    model = WorkflowModel([])
    app = RoundService(tmp_path, workers=3, model=model)
    db = app.db("sales-demo")
    holdout = [t for t in db["tasks"] if t["split"] == "holdout"]
    for task in holdout:
        write(tmp_path / "references" / (fingerprint(task["id"])[:20] + ".json"),
              app.sql(db, task["reference_sql"]).model_dump())
    barrier = threading.Barrier(len(holdout))
    calls = []

    def concurrent_run(db, question, evidence, skill, task):
        calls.append(task["id"])
        barrier.wait(timeout=5)
        prediction = app.reference_execution(db, task)
        return {"sql": task["reference_sql"], "execution": {"status": "success"},
                "trace": [], "tokens": 0, "model_calls": 0}, prediction

    monkeypatch.setattr(app, "run", concurrent_run)
    report = app.compare(db["id"])
    assert len(calls) == len(holdout)  # Empty initial and final skills share observations.
    assert [r["task_id"] for r in report["rows"]["optimized"]] == [t["id"] for t in holdout]
    assert report["optimized"] == 1
    assert app.compare(db["id"]) == report and len(calls) == len(holdout)


def test_sql_parallelism_is_bounded_separately_from_model_workers(tmp_path, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    app = RoundService(tmp_path, workers=32, model=WorkflowModel([]))
    active = maximum = 0
    lock = threading.Lock()
    barrier = threading.Barrier(4)

    def execute_stub(self, db, sql):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        barrier.wait(timeout=5)
        with lock:
            active -= 1
        return sql

    monkeypatch.setattr(Service, "sql", execute_stub)
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(lambda i: app.sql({}, str(i)), range(8))) == list(map(str, range(8)))
    assert maximum == 4


def test_service_rechecks_cached_patch_against_candidate_document(tmp_path, monkeypatch):
    app = Service(tmp_path)
    app.optimize("sales-demo")
    current = app.version(app.db("sales-demo"))["skill"]

    def bypass(*args):
        app.optimizer_details = {"profile": PROFILE, "valid": True, "mode": "bootstrap"}
        return current + "\nUnbounded rewrite", None

    monkeypatch.setattr(app, "propose", bypass)
    version = app.optimize("sales-demo")
    assert version["status"] == "rejected" and "bounded document patch" in (
        version["selection"]["reason"]
    )
    assert app.db("sales-demo")["active"] == "v1"


def test_evaluation_cli_accepts_32_workers(tmp_path, monkeypatch):
    from evogate import evaluation

    calls = []
    monkeypatch.setattr("sys.argv", ["evaluation", "run", "--workspace", str(tmp_path),
                                     "--workers", "32"])
    monkeypatch.setattr(evaluation, "run", lambda root, workers: calls.append((root, workers)))
    evaluation.main()
    assert calls == [(tmp_path, 32)]


@pytest.mark.parametrize("workers", ["0", "-1"])
def test_evaluation_cli_rejects_nonpositive_workers(tmp_path, monkeypatch, workers):
    from evogate import evaluation

    monkeypatch.setattr("sys.argv", ["evaluation", "run", "--workspace", str(tmp_path),
                                     "--workers", workers])
    with pytest.raises(SystemExit) as exc:
        evaluation.main()
    assert exc.value.code == 2
