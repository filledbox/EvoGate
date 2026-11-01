import json
from pathlib import Path

import pytest

from evogate.data import import_bird, sql_template
from evogate.model import Model
from evogate.service import Service


def test_query_correction_evolution_freeze_and_persistence(tmp_path):
    app = Service(tmp_path)
    db = app.db("sales-demo")
    task = db["tasks"][0]
    before = app.query(db["id"], task["question"], task_id=task["id"])
    assert before["execution"]["rows"] == [[5]]
    feedback = app.feedback(before["id"], task["reference_sql"], "按订单计数，避免连接扇出")
    assert feedback["source"] == "user_confirmed"
    version = app.optimize(db["id"])
    assert version["status"] == "accepted"
    after = app.query(db["id"], task["question"], task_id=task["id"])
    assert after["execution"]["rows"] == [[3]]
    app.rollback(db["id"], "v0")
    assert db["active"] == "v0"
    app.rollback(db["id"], version["id"])
    report = app.compare(db["id"])
    assert report["kind"] == "synthetic_demo"
    assert report["improved"] == 2 and report["regressed"] == 0
    assert report["optimized"] == 1
    assert Service(tmp_path).compare(db["id"])["id"] == report["id"]
    assert json.loads((tmp_path / "report-sales-demo.json").read_text())["id"] == report["id"]
    for operation in (
        lambda: app.optimize(db["id"]),
        lambda: app.rollback(db["id"], "v0"),
        lambda: app.feedback(before["id"], task["reference_sql"]),
    ):
        operation()
    assert app.compare(db["id"])["id"] == report["id"]


def test_public_api_does_not_expose_holdout_questions_or_references(tmp_path):
    app = Service(tmp_path)
    state = app.public_state()
    public = json.dumps(state, ensure_ascii=False)
    assert "reference_sql" not in public
    assert "有几笔订单" not in public
    assert len(state["databases"][0]["examples"]) == 3


def test_invalid_correction_cannot_write_and_preserves_feedback(tmp_path):
    app = Service(tmp_path)
    task = app.db("sales-demo")["tasks"][0]
    query = app.query("sales-demo", task["question"], task_id=task["id"])
    with pytest.raises(ValueError):
        app.feedback(query["id"], "DELETE FROM orders")
    assert not app.state["feedback"]
    assert app.sql(app.db("sales-demo"), "SELECT COUNT(*) FROM orders").rows == [(6,)]


def test_reviewed_feedback_keeps_original_reference(tmp_path):
    app = Service(tmp_path)
    task = app.db("sales-demo")["tasks"][0]
    original_reference = task["reference_sql"]
    query = app.query("sales-demo", task["question"], task_id=task["id"])
    feedback = app.feedback(
        query["id"], original_reference, "核验说明", source="reviewed_feedback",
    )
    assert feedback["source"] == "reviewed_feedback"
    assert task["reference_sql"] == original_reference
    with pytest.raises(ValueError, match="反馈来源"):
        app.feedback(query["id"], original_reference, source="unknown")


def test_unhelpful_candidate_is_rejected(tmp_path, monkeypatch):
    app = Service(tmp_path)
    monkeypatch.setattr(app, "propose", lambda *args: ("Use COUNT(*) for joined orders.", None))
    version = app.optimize("sales-demo")
    assert version["status"] == "rejected"
    assert app.db("sales-demo")["active"] == "v0"
    with pytest.raises(ValueError, match="只能回退"):
        app.rollback("sales-demo", version["id"])


def test_oversized_candidate_is_recorded_without_publication(tmp_path, monkeypatch):
    app = Service(tmp_path, optimizer_profile="legacy")
    monkeypatch.setattr(app, "propose", lambda *args: ("x" * 4001, None))
    version = app.optimize("sales-demo")
    assert version["status"] == "rejected"
    assert version["selection"]["candidate"] is None
    assert "4000" in version["selection"]["reason"]
    assert app.db("sales-demo")["active"] == "v0"


def test_interrupted_holdout_freezes_only_evaluation_snapshot(tmp_path, monkeypatch):
    app = Service(tmp_path)
    monkeypatch.setattr(app, "evaluate", lambda *args: (_ for _ in ()).throw(ValueError("offline")))
    with pytest.raises(ValueError, match="offline"):
        app.compare("sales-demo")
    assert not app.public_state()["databases"][0]["frozen"]
    assert (tmp_path / "frozen-sales-demo.json").exists()
    app.rollback("sales-demo", "v0")


def test_empty_skill_shares_observations(tmp_path):
    report = Service(tmp_path).compare("sales-demo")
    assert report["gain"] == 0
    assert report["shared_observations"]


def test_demo_and_live_results_cannot_mix(tmp_path):
    Service(tmp_path).optimize("sales-demo")
    with pytest.raises(ValueError, match="另一模式"):
        Service(tmp_path, live=True)


def test_bird_import_groups_sql_templates(tmp_path):
    demo = Service(tmp_path / "demo").db("sales-demo")
    root = tmp_path / "dbs"
    destination = root / "tiny" / "tiny.sqlite"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(Path(demo["path"]).read_bytes())
    source = tmp_path / "train.json"
    queries = [
        "SELECT COUNT(*) FROM orders WHERE id=1",
        "SELECT COUNT(*) FROM orders WHERE id=2",
        "SELECT region FROM orders WHERE id=1",
        "SELECT SUM(amount) FROM items WHERE id=1",
        "SELECT MAX(amount) FROM items WHERE id=1",
        "SELECT MIN(amount) FROM items WHERE id=1",
        "SELECT status FROM orders WHERE id=1",
    ]
    source.write_text(json.dumps([{"db_id": "tiny", "question": f"q{i}", "SQL": sql}
                                  for i, sql in enumerate(queries)]))
    imported = import_bird(source, root, ["tiny"], per_split=2)[0]
    assert len(imported["tasks"]) == 6
    groups = {}
    for task in imported["tasks"]:
        template = sql_template(task["reference_sql"])
        assert groups.setdefault(template, task["split"]) == task["split"]
    assert imported["source"] == "BIRD"
    filtered = import_bird(source, root, ["tiny"], per_split=2, excluded_ids={"bird:tiny:0"})[0]
    assert all(task["id"] != "bird:tiny:0" for task in filtered["tasks"])


class ToolModel:
    live = True

    def __init__(self):
        self.calls = []

    def info(self):
        return {"mode": "live", "model": "test-only", "base_url": "mock"}

    def complete(self, messages, tools=None):
        self.calls.append(json.loads(json.dumps(messages)))
        if len(self.calls) == 1:
            return {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call1", "type": "function", "function": {
                    "name": "query_values", "arguments": '{"table":"orders","column":"status"}',
                },
            }]}, {"total_tokens": 10}
        return {"role": "assistant", "content": "SELECT COUNT(*) FROM orders"}, {
            "total_tokens": 12,
        }


def test_live_tool_loop_does_not_receive_reference_sql(tmp_path):
    model = ToolModel()
    app = Service(tmp_path, model=model)
    task = app.db("sales-demo")["tasks"][0]
    result = app.query("sales-demo", task["question"], task_id=task["id"])
    assert result["execution"]["rows"] == [[6]]
    assert result["tokens"] == 22
    assert result["trace"][0]["name"] == "query_values"
    assert task["reference_sql"] not in json.dumps(model.calls)
    assert model.calls[1][-1]["role"] == "tool"


def test_tool_budget_exhaustion_is_a_saved_failure(tmp_path, monkeypatch):
    model = ToolModel()

    def complete(messages, tools=None):
        return {"role": "assistant", "content": None, "tool_calls": [{
            "id": f"call{index}", "type": "function", "function": {
                "name": "execute_sql", "arguments": '{"sql":"SELECT 1"}',
            },
        } for index in range(5)]}, {"total_tokens": 10}

    monkeypatch.setattr(model, "complete", complete)
    app = Service(tmp_path, model=model)
    row = app.query("sales-demo", "test budget")
    assert row["execution"]["status"] == "rejected"
    assert row["execution"]["detail"] == "tool_call_limit"
    assert row["tokens"] == 20
    assert row["trace"][-1]["kind"] == "tool_budget_exhausted"
    assert app.state["queries"][-1]["id"] == row["id"]


def test_optimizer_only_receives_training_and_confirmed_feedback(tmp_path, monkeypatch):
    model = Model(live=True)
    app = Service(tmp_path, model=model, optimizer_profile="legacy")
    db = app.db("sales-demo")
    training_task = next(t for t in db["tasks"] if t["split"] == "training")
    training = [{"task_id": training_task["id"], "sql": "SELECT 0", "execution": {}, "score": 0}]
    captured = []

    def complete(messages, tools=None):
        captured.append(messages)
        return {"content": "Count unique orders after joining items."}, {"total_tokens": 10}

    monkeypatch.setattr(model, "complete", complete)
    candidate, _ = app.propose(db, training, "")
    assert candidate
    content = captured[0][1]["content"]
    assert training_task["question"] in content
    assert all(t["question"] not in content for t in db["tasks"] if t["split"] != "training")


def test_schema_change_blocks_stale_skill(tmp_path):
    import sqlite3

    app = Service(tmp_path)
    db = app.db("sales-demo")
    with sqlite3.connect(db["path"]) as conn:
        conn.execute("ALTER TABLE orders ADD COLUMN changed TEXT")
    with pytest.raises(ValueError, match="结构已改变"):
        app.db("sales-demo")


def test_round_observations_resume_and_do_not_mix_skills(tmp_path, monkeypatch):
    from evogate.data import fingerprint
    from evogate.evaluation import RoundService, write

    app = RoundService(tmp_path, workers=2, model=Model(live=False))
    db = app.db("sales-demo")
    tasks = [task for task in db["tasks"] if task["split"] == "training"]
    for task in tasks:
        result = app.sql(db, task["reference_sql"])
        write(tmp_path / "references" / (fingerprint(task["id"])[:20] + ".json"),
              result.model_dump())
    # The production model exposes its request counter for progress output.
    app.model.calls = 0
    baseline = app.evaluate(db, tasks, "")
    candidate = app.evaluate(db, tasks, "Use COUNT(DISTINCT o.id) after joining items.")
    assert baseline[0]["score"] == 0 and candidate[0]["score"] == 1
    monkeypatch.setattr(app, "run", lambda *args: pytest.fail("cached tasks must not rerun"))
    assert app.evaluate(db, tasks, "") == baseline


def test_recorded_model_concurrent_request_limit(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    from evogate.recording import RecordedModel

    monkeypatch.setattr(Model, "complete", lambda *args: (
        {"role": "assistant", "content": "SELECT 1"}, {"total_tokens": 3},
    ))
    model = RecordedModel(tmp_path, max_calls=2, quiet=True)

    def call():
        try:
            model.complete([{"role": "user", "content": "test"}])
            return "ok"
        except ValueError:
            return "limited"

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: call(), range(4)))
    events = [json.loads(line) for line in model.log.read_text().splitlines()]
    assert results.count("ok") == 2 and results.count("limited") == 2
    assert sum(event["kind"] == "request" for event in events) == 2
    assert RecordedModel(tmp_path, max_calls=2, quiet=True).calls == 2
