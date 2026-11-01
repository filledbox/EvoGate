"""Regression coverage for the user-facing continuous SQL workspace lifecycle."""

import json
import threading
from pathlib import Path

import pytest
from test_skill_evolution import BAD_SKILL, GOOD_SKILL, WorkflowModel

from evogate.data import fingerprint
from evogate.experience import EvolutionPolicy, select_replay, trigger_reason
from evogate.service import Service


def register(app, name="business", skill_id="sql-analysis"):
    path = app.db("sales-demo")["path"]
    app.register_database(name, path, context="One order can have several items.",
                          skill_id=skill_id)
    return app.db(name)


def test_imported_skill_branches_are_independent_and_survive_restart(tmp_path):
    app = Service(tmp_path)
    skill = app.import_skill(GOOD_SKILL)
    first = register(app, "first", skill["id"])
    second = register(app, "second", skill["id"])
    assert first["branch_id"] != second["branch_id"]
    assert app.version(first)["skill"] == app.version(second)["skill"] == GOOD_SKILL
    old_branch = first["branch_id"]
    app.bind_skill("first", "sql-analysis")
    assert app.version(second)["skill"] == GOOD_SKILL
    assert old_branch in app.state["skill_branches"]
    app.switch_branch("first", old_branch)
    assert app.version(first)["skill"] == GOOD_SKILL
    with pytest.raises(ValueError, match="不属于"):
        app.switch_branch("second", old_branch)
    restored = Service(tmp_path)
    assert restored.version(restored.db("second"))["skill"] == GOOD_SKILL
    state = json.loads(app.file.read_text())
    assert "versions" not in state["databases"]["first"]
    assert state["skill_catalog"][skill["id"]]["sha256"] == fingerprint(GOOD_SKILL)


def test_registered_database_uses_seed_and_business_context(tmp_path):
    model = WorkflowModel([])
    app = Service(tmp_path, model=model)
    db = register(app)
    result = app.query(db["id"], "华东订单数")
    assert result["branch_id"] == db["branch_id"]
    assert app.version(db)["skill"] in model.target_inputs[-1][1]["content"]
    assert db["context"] in model.target_inputs[-1][0]["content"]
    assert next(iter(app.state["experiences"].values()))["signal"] == "unverified"


def test_case_import_is_atomic_and_prevents_template_leakage(tmp_path):
    app = Service(tmp_path)
    db = register(app)
    with pytest.raises(ValueError, match="模板"):
        app.add_cases(db["id"], [
            {"question": "one", "reference_sql": "SELECT id FROM orders WHERE id=1",
             "split": "training"},
            {"question": "two", "reference_sql": "SELECT id FROM orders WHERE id=2",
             "split": "holdout"},
        ])
    assert not db["tasks"]
    app.add_cases(db["id"], [
        {"question": "count", "reference_sql": "SELECT COUNT(*) FROM orders", "split": "selection"},
        {"question": "regions", "reference_sql": "SELECT region FROM orders", "split": "holdout"},
    ])
    with pytest.raises(ValueError, match="独立评测"):
        app.query(db["id"], "regions")


def test_feedback_replaces_weak_signal_without_counting_duplicates(tmp_path):
    app = Service(tmp_path)
    task = app.db("sales-demo")["tasks"][0]
    query = app.query("sales-demo", task["question"], task_id=task["id"])
    assert next(iter(app.state["experiences"].values()))["signal"] == "unverified"
    app.feedback(query["id"], task["reference_sql"])
    exp = next(iter(app.state["experiences"].values()))
    assert exp["score"] == 0 and exp["signal"] == "verified"
    event = exp["event_id"]
    app.feedback(query["id"], task["reference_sql"])
    assert len(app.state["experiences"]) == 1
    assert next(iter(app.state["experiences"].values()))["event_id"] == event
    assert trigger_reason([exp], [], EvolutionPolicy(batch_size=1)) == "new_feedback_batch"
    assert trigger_reason([exp], [event], EvolutionPolicy(batch_size=1)) is None


def test_failure_trigger_requires_distinct_questions():
    policy = EvolutionPolicy()
    records = [{"key": "same", "event_id": str(i), "failure_kind": "syntax",
                "signal": "execution_failure"} for i in range(3)]
    assert trigger_reason(records, [], policy) is None
    for i, record in enumerate(records):
        record["key"] = str(i)
    assert trigger_reason(records, [], policy) == "recurring_failure"


def test_replay_retains_old_unresolved_cases_and_successes():
    rows = [{"id": str(i), "key": str(i), "updated": i, "score": 0 if i < 4 else 1,
             "resolved": False} for i in range(100)]
    selected = select_replay(rows, 8)
    assert len(selected) == 8
    assert any(r["updated"] < 4 for r in selected)
    assert any(r["score"] == 1 for r in selected)
    assert any(r["updated"] == 99 for r in selected)


def test_pending_attempt_keeps_original_evidence_for_later_repair(tmp_path):
    model = WorkflowModel([BAD_SKILL, GOOD_SKILL])
    app = Service(tmp_path, model=model)
    task = app.db("sales-demo")["tasks"][0]
    query = app.query("sales-demo", task["question"], task_id=task["id"])
    app.feedback(query["id"], task["reference_sql"], "Count order entities.")
    first = app.optimize("sales-demo")
    assert first["status"] == "pending"
    exp = next(iter(app.state["experiences"].values()))
    assert not exp["resolved"] and exp["score"] == 0
    second = app.optimize("sales-demo")
    assert second["status"] == "accepted"
    assert exp["resolved"] and len(exp["attempts"]) == 2
    prompt = model.optimizer_inputs[-1]
    assert prompt["reviewed_corrections"][0]["reference_sql"] == task["reference_sql"]
    assert prompt["rejected_updates"][0]["outcome"] == "pending"
    assert prompt["rejected_updates"][0]["development_outcomes"]


def test_missing_validation_stages_candidate_without_publishing(tmp_path):
    app = Service(tmp_path, model=WorkflowModel([GOOD_SKILL]))
    db = app.db("sales-demo")
    db["tasks"] = [t for t in db["tasks"] if t["split"] == "training"]
    result = app.optimize(db["id"])
    assert result["status"] == "pending" and db["active"] == "v0"
    assert "不足" in result["selection"]["reason"]


def test_stream_releases_only_current_window_and_resumes_partial_window(tmp_path, monkeypatch):
    app = Service(tmp_path)
    db = app.db("sales-demo")
    db["released_task_ids"] = []
    original = app.evaluate
    calls = 0

    def interrupt(db, tasks, skill):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ValueError("interrupted")
        return original(db, tasks, skill)

    monkeypatch.setattr(app, "evaluate", interrupt)
    with pytest.raises(ValueError, match="interrupted"):
        app.release_batch(db["id"], 2)
    assert len(db["released_task_ids"]) == 1
    resumed = Service(tmp_path)
    assert resumed.release_batch(db["id"], 2) == 2
    restored = resumed.db(db["id"])
    assert len(restored["released_task_ids"]) == 2
    assert len(resumed.development_tasks(restored)) == 2
    assert db["tasks"][2]["question"] not in json.dumps(resumed.state["experiences"])


def test_background_evolution_leaves_queries_available_and_preserves_new_feedback(tmp_path,
                                                                                 monkeypatch):
    app = Service(tmp_path)
    entered, release, done = threading.Event(), threading.Event(), threading.Event()
    optimize = Service.optimize

    def wait_then_optimize(worker, db_id):
        entered.set()
        assert release.wait(10)
        return optimize(worker, db_id)

    original_save = app.save

    def save():
        original_save()
        if app.state["jobs"] and app.state["jobs"][-1]["status"] != "running":
            done.set()

    monkeypatch.setattr(Service, "optimize", wait_then_optimize)
    monkeypatch.setattr(app, "save", save)
    job = app.schedule("sales-demo", manual=True)
    assert entered.wait(5)
    db = app.db("sales-demo")
    task = db["tasks"][0]
    query = app.query(db["id"], task["question"], task_id=task["id"])
    app.feedback(query["id"], task["reference_sql"])
    with pytest.raises(ValueError, match="正在进化"):
        app.bind_skill(db["id"], "sql-analysis")
    release.set()
    assert done.wait(15)
    assert job["status"] == "completed"
    assert app.state["feedback"] and app.state["experiences"]
    exp = next(iter(app.state["experiences"].values()))
    assert exp["event_id"] not in app.state["processed_events"][db["branch_id"]]


def test_budget_failure_keeps_evidence_and_active_version(tmp_path):
    app = Service(tmp_path, model=WorkflowModel([GOOD_SKILL]))
    app.configure_evolution(max_calls_per_attempt=1)
    job = app.schedule("sales-demo", manual=True)
    # Join the actual worker, avoiding timing-dependent polling.
    worker = next(t for t in threading.enumerate() if t.name == "evogate-evolution")
    worker.join(15)
    assert job["status"] == "failed" and job["model_calls"] == 1
    assert "预算" in job["error"] and app.db("sales-demo")["active"] == "v0"


def test_compare_keeps_initial_snapshot_while_workspace_continues(tmp_path, monkeypatch):
    app = Service(tmp_path)
    db = app.db("sales-demo")
    original = app.evaluate
    monkeypatch.setattr(app, "evaluate", lambda *args: (_ for _ in ()).throw(ValueError("offline")))
    with pytest.raises(ValueError):
        app.compare(db["id"])
    monkeypatch.setattr(app, "evaluate", original)
    app.optimize(db["id"])
    assert db["active"] == "v1"
    report = app.compare(db["id"])
    assert report["version"] == "v0" and report["gain"] == 0
    app.rollback(db["id"], "v0")
    assert set(report["rows"]) == {"baseline", "optimized", "no_skill"}
    assert report["initial_skill_hash"] == fingerprint("")
    assert (Path(app.root) / "frozen-sales-demo.json").exists()


def test_report_compares_imported_initial_skill_with_evolved_and_no_skill(tmp_path):
    app = Service(tmp_path, model=WorkflowModel([]))
    skill = app.import_skill(GOOD_SKILL)
    app.bind_skill("sales-demo", skill["id"])
    report = app.compare("sales-demo")
    assert report["baseline"] == report["optimized"] == 1
    assert report["no_skill"] < report["baseline"]
    assert report["gain"] == 0 and report["initial_skill"] == GOOD_SKILL


def test_stream_evaluation_uses_batches_and_final_snapshot_then_resumes(tmp_path, monkeypatch):
    from evogate import evaluation
    from evogate.evaluation import write
    from evogate.skills import PROFILE

    model = WorkflowModel([BAD_SKILL, GOOD_SKILL])
    app = Service(tmp_path, model=model)
    db = app.db("sales-demo")
    db.update(released_task_ids=[], stream_round=0)
    app.save()
    for task in db["tasks"]:
        write(tmp_path / "references" / (fingerprint(task["id"])[:20] + ".json"),
              app.sql(db, task["reference_sql"]).model_dump())
    write(tmp_path / "round-protocol.json", {
        "kind": "workspace-skill-stream", "databases": [db["id"]], "model": model.info(),
        "max_calls": 100, "optimizer_profile": PROFILE, "candidate_rounds": 2, "batch_size": 1,
    })
    write(tmp_path / "preflight.json", {"invalid": []})
    (tmp_path / "requests").mkdir()
    (tmp_path / "requests/requests.jsonl").write_text("")
    monkeypatch.setattr(evaluation, "RecordedModel", lambda *args, **kwargs: model)
    evaluation.run(tmp_path, 2)
    restored = Service(tmp_path, model=model)
    current = restored.db(db["id"])
    assert current["stream_round"] == 2 and len(current["released_task_ids"]) == 2
    assert current["active"] == "v2"
    assert db["tasks"][2]["question"] not in json.dumps(model.optimizer_inputs, ensure_ascii=False)
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["macro_average"]["optimized"] == 1
    calls = model.calls
    evaluation.run(tmp_path, 2)
    assert model.calls == calls


def test_stream_advances_to_new_training_after_unchanged_skill(tmp_path, monkeypatch):
    from evogate import evaluation
    from evogate.evaluation import write
    from evogate.skills import PROFILE

    patch = {"base_sha256": fingerprint(GOOD_SKILL),
             "edits": [{"op": "replace", "target": "check the query",
                        "content": "inspect the result"}]}
    model = WorkflowModel([GOOD_SKILL, patch])
    app = Service(tmp_path, model=model)
    app.bind_skill("sales-demo", app.import_skill(GOOD_SKILL)["id"])
    db = app.db("sales-demo")
    db.update(released_task_ids=[], stream_round=0)
    app.save()
    write(tmp_path / "round-protocol.json", {
        "kind": "workspace-skill-stream", "databases": [db["id"]], "model": model.info(),
        "max_calls": 100, "optimizer_profile": PROFILE, "candidate_rounds": 2, "batch_size": 1,
    })
    write(tmp_path / "preflight.json", {"invalid": []})
    (tmp_path / "requests").mkdir()
    (tmp_path / "requests/requests.jsonl").write_text("")
    monkeypatch.setattr(evaluation, "RecordedModel", lambda *args, **kwargs: model)
    evaluation.run(tmp_path, 1)
    restored = Service(tmp_path, model=model)
    db = restored.db("sales-demo")
    assert db["versions"][1]["selection"]["decision"] == "unchanged"
    assert db["versions"][1]["candidate_training"] == []
    assert db["stream_round"] == 2 and len(db["released_task_ids"]) == 2
    assert len(model.optimizer_inputs) == 2
    assert db["tasks"][1]["id"] in {
        row["task_id"] for row in model.optimizer_inputs[1]["development_examples"]}


def test_automatic_trigger_and_cooldown_do_not_reconsume_old_feedback(tmp_path):
    app = Service(tmp_path)
    db = app.db("sales-demo")
    task = db["tasks"][0]
    query = app.query(db["id"], task["question"], task_id=task["id"])
    app.feedback(query["id"], task["reference_sql"])
    app.configure_evolution(enabled=True, batch_size=1)
    job = app.schedule(db["id"])
    assert job["reason"] == "new_feedback_batch"
    for worker in threading.enumerate():
        if worker.name == "evogate-evolution":
            worker.join(15)
    assert job["status"] == "completed"
    assert app.schedule(db["id"]) is None
    app.configure_evolution(cooldown_seconds=0)
    assert app.schedule(db["id"]) is None


def test_schema_registry_migrates_existing_feedback_into_experience(tmp_path):
    app = Service(tmp_path)
    task = app.db("sales-demo")["tasks"][0]
    query = app.query("sales-demo", task["question"], task_id=task["id"])
    app.feedback(query["id"], task["reference_sql"])
    state = json.loads(app.file.read_text())
    state.pop("schema_version")
    state.pop("experiences")
    app.file.write_text(json.dumps(state))
    restored = Service(tmp_path)
    exp = next(iter(restored.state["experiences"].values()))
    assert exp["reference_sql"] == task["reference_sql"] and exp["score"] == 0
