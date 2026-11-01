import json

import pytest

from evogate.model import Model
from evogate.optimizer import (
    EvidenceOptimizer,
    Rule,
    comparison,
    execution_view,
    parse_rules,
    render,
)
from evogate.service import Service


def rule(rule_id="r1", source="train:1", instruction="Count unique orders after item joins."):
    return {"id": rule_id, "when": "Counting orders after joining order items",
            "instruction": instruction, "source_ids": [source]}


class Replies:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.messages = []

    def complete(self, messages):
        self.messages.append(json.loads(json.dumps(messages)))
        return {"role": "assistant", "content": next(self.replies)}, {
            "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
        }


def test_rule_sources_are_not_injected_into_target_skill():
    rules, skill = parse_rules(json.dumps({"rules": [rule()]}), {"train:1"}, [])
    assert rules[0].source_ids == ["train:1"]
    assert "train:1" not in skill
    assert "When:" in skill and "Do:" in skill


def test_field_limits_bound_rendered_length_without_truncation():
    rules = [Rule(id=f"r{i}", when="w" * 140, instruction="d" * 300,
                  source_ids=["train:1"]) for i in range(1, 7)]
    skill = render(rules)
    assert len(skill) <= 4000
    assert skill.count("w" * 140) == 6 and skill.count("d" * 300) == 6


def test_reject_unknown_sources_and_overbroad_rewrites():
    with pytest.raises(ValueError, match="source ids"):
        parse_rules(json.dumps({"rules": [rule(source="holdout:1")]}), {"train:1"}, [])
    previous = [rule(f"r{i}") for i in range(1, 5)]
    changed = [rule(f"r{i}", instruction="Different rule") for i in range(1, 5)]
    with pytest.raises(ValueError, match="at most 3"):
        parse_rules(json.dumps({"rules": changed}), {"train:1"}, previous)
    retained, _ = parse_rules(json.dumps({"rules": previous}), set(), previous)
    assert len(retained) == 4


def test_format_repair_is_bounded_and_usage_is_summed():
    model = Replies(["not json", json.dumps({"rules": [rule()]})])
    result = EvidenceOptimizer(model.complete).propose(
        "schema", "", [{"task_id": "train:1"}], [],
    )
    assert result.details["valid"] and len(result.details["attempts"]) == 2
    assert result.usage["total_tokens"] == 30
    assert "do not add new business hypotheses" in model.messages[1][-1]["content"]
    failure = EvidenceOptimizer(Replies(["bad", "still bad"]).complete).propose(
        "schema", "", [{"task_id": "train:1"}], [],
    )
    assert not failure.details["valid"] and failure.skill == ""
    assert len(failure.details["attempts"]) == 2


def test_transport_failure_does_not_trigger_format_repair():
    calls = []

    def failed(messages):
        calls.append(messages)
        raise ValueError("模型服务连接失败")

    with pytest.raises(ValueError, match="连接失败"):
        EvidenceOptimizer(failed).propose("schema", "", [{"task_id": "train:1"}], [])
    assert len(calls) == 1


def test_diagnostics_distinguish_shape_and_unknown_without_claiming_semantics():
    predicted = execution_view({"status": "success", "rows": [[1, 2]], "row_count": 1})
    reference = execution_view({"status": "success", "rows": [[1]], "row_count": 1})
    assert comparison(predicted, reference, 0)["kind"] == "output_shape_mismatch"
    assert comparison(predicted, reference, None)["kind"] == "unknown"
    empty = execution_view({"status": "success", "rows": [], "row_count": 0})
    assert empty["column_count"] is None


def test_v2_context_contains_execution_evidence_and_only_development(tmp_path):
    app = Service(tmp_path, live=True, optimizer_profile="evidence_v2")
    db = app.db("sales-demo")
    task = next(task for task in db["tasks"] if task["split"] == "training")
    model = Replies([json.dumps({"rules": [rule(source=task["id"])]})])
    app.model.complete = model.complete
    prediction = app.sql(db, "SELECT 1,2")
    training = [{"task_id": task["id"], "sql": "SELECT 1,2", "score": 0,
                 "execution": {"status": prediction.status, "rows": prediction.rows,
                               "row_count": 1}}]
    skill, usage = app.propose(db, training, "")
    context = json.loads(model.messages[0][1]["content"])
    example = context["development_examples"][0]
    assert example["reference_execution"]["row_count"] == 1
    assert example["comparison"]["kind"] == "output_shape_mismatch"
    assert "COUNT(DISTINCT" in example["reference_sql"]
    assert app.optimizer_details["rules"][0]["source_ids"] == [task["id"]]
    assert skill and usage["total_tokens"] == 15
    encoded = json.dumps(context, ensure_ascii=False)
    assert all(t["question"] not in encoded for t in db["tasks"] if t["split"] != "training")
    wrong = next(t for t in db["tasks"] if t["split"] == "holdout")
    with pytest.raises(ValueError, match="只能接收开发"):
        app.development_context(db, [{**training[0], "task_id": wrong["id"]}])


def test_invalid_v2_candidate_is_persisted_as_rejected(tmp_path):
    app = Service(tmp_path)
    # Keep real local SQL evaluation and offline generation for the target.
    original = app.propose

    def invalid(db, training, incumbent):
        result = EvidenceOptimizer(Replies(["bad", "bad"]).complete).propose(
            db["schema"], incumbent, [{"task_id": training[0]["task_id"]}], [],
        )
        app.optimizer_details = result.details
        return result.skill, result.usage

    app.propose = invalid
    version = app.optimize("sales-demo")
    assert version["status"] == "rejected" and app.db("sales-demo")["active"] == "v0"
    assert version["optimizer"]["valid"] is False
    assert len(version["optimizer"]["attempts"]) == 2
    app.propose = original


def test_legacy_switch_keeps_original_proposal_format(tmp_path, monkeypatch):
    monkeypatch.setenv("EVOGATE_OPTIMIZER", "legacy")
    app = Service(tmp_path, model=Model(live=True))
    task = app.db("sales-demo")["tasks"][0]
    model = Replies(["Legacy plain-text skill"])
    app.model.complete = model.complete
    result, _ = app.propose(app.db("sales-demo"), [{
        "task_id": task["id"], "sql": "SELECT 0", "execution": {}, "score": 0,
    }], "")
    assert result == "Legacy plain-text skill"
    assert "training" in json.loads(model.messages[0][1]["content"])
    assert app.optimizer_details["profile"] == "legacy"


def test_structured_proposal_publishes_and_survives_restart(tmp_path):
    class LocalReplies(Model):
        def __init__(self):
            super().__init__(live=True)

        def complete(self, messages, tools=None):
            if messages[0]["content"].startswith("Update a database-local SQL skill"):
                context = json.loads(messages[1]["content"])
                content = json.dumps({"rules": [rule(
                    source=context["development_examples"][0]["task_id"],
                )]})
            else:
                question = json.loads(next(m["content"] for m in messages
                                           if m["role"] == "user"))["question"]
                region = next(region for region in ("华东", "华南", "华北") if region in question)
                guided = any(m["content"].startswith("Database-local skill") for m in messages)
                expression = ("SUM(i.amount)" if "金额" in question else
                              "COUNT(DISTINCT o.id)" if guided else "COUNT(*)")
                content = (f"SELECT {expression} FROM orders o JOIN items i ON i.order_id=o.id "
                           f"WHERE o.region='{region}'")
            return {"role": "assistant", "content": content}, {
                "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
            }

    app = Service(tmp_path, model=LocalReplies(), optimizer_profile="evidence_v2")
    version = app.optimize("sales-demo")
    assert version["status"] == "accepted"
    assert version["optimizer"]["profile"] == "evidence_v2"
    assert version["optimizer"]["rules"][0]["source_ids"] == ["demo:training:0"]
    restored = Service(tmp_path, model=LocalReplies())
    assert restored.db("sales-demo")["active"] == version["id"]
    assert restored.version(restored.db("sales-demo"))["optimizer"] == version["optimizer"]


def test_round_cache_cannot_silently_switch_optimizer(tmp_path):
    from evogate.evaluation import RoundService, write

    app = RoundService(tmp_path, workers=2, model=Model(live=True))
    write(tmp_path / "candidates/sales-demo.json", {"skill": "old skill", "usage": None})
    with pytest.raises(ValueError, match="缓存来自不同优化器"):
        app.propose(app.db("sales-demo"), [], "")
    app.optimizer_profile = "legacy"
    assert app.propose(app.db("sales-demo"), [], "")[0] == "old skill"

