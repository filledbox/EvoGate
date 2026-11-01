"""Run a bounded real-model functional check on the bundled sales database."""

import argparse
import json
import shutil
from pathlib import Path

from evogate.data import fingerprint
from evogate.evaluation import write
from evogate.optimizer import MarkdownOptimizer
from evogate.recording import RecordedModel, load_environment
from evogate.service import Service
from evogate.skills import PROFILE


def verify(workspace, output, max_calls):
    model = RecordedModel(workspace / "requests", max_calls=max_calls)
    service = Service(workspace, model=model, optimizer_profile=PROFILE)
    db = service.db("sales-demo")
    task = next(t for t in db["tasks"] if t["split"] == "training")
    query = next((q for q in service.state["queries"]
                  if q["question"] == task["question"]), None)
    if query is None:
        query = service.query(db["id"], task["question"], task_id=task["id"])
    if query["execution"]["status"] != "success":
        raise ValueError("Live query failed; inspect the saved request log and execution trace")
    if not any(f["query_id"] == query["id"] for f in service.state["feedback"]):
        reference = service.sql(db, task["reference_sql"])
        if reference.rows != [(3,)]:
            raise ValueError("Bundled sales reference does not match the expected three orders")
        service.feedback(query["id"], task["reference_sql"],
                         "核对统计口径，一笔订单计一次，包含全部订单状态。",
                         source="reviewed_feedback")
    version = service.optimize(db["id"]) if len(db["versions"]) == 1 else db["versions"][1]
    if not version["optimizer"].get("valid") or not version["skill"]:
        raise ValueError("Live Markdown generation failed; inspect the persisted candidate")

    # Exercise the patch editor against the generated document even if the release
    # gate kept v0. This probe has its own artifact and does not publish a version.
    probe_path = workspace / "bounded-probe.json"
    if probe_path.exists():
        probe = json.loads(probe_path.read_text())
    else:
        examples, corrections = service.development_context(db, version["candidate_training"])
        proposal = MarkdownOptimizer(model.complete_skill, service.edit_budget).propose(
            db["schema"], version["skill"], examples, corrections,
        )
        probe = {"skill": proposal.skill, "usage": proposal.usage, "details": proposal.details,
                 "published": False}
        write(probe_path, probe)
    if not probe["details"]["valid"] or probe["details"]["mode"] != "patch":
        raise ValueError("Live bounded patch probe failed; inspect bounded-probe.json")

    report = service.compare(db["id"])
    events = [json.loads(line) for line in model.log.read_text().splitlines()]
    responses = [e for e in events if e["kind"] == "response"]
    usages = [e.get("usage") for e in responses]
    known = all(isinstance(u, dict) and type(u.get("total_tokens")) is int for u in usages)
    project = Path(__file__).resolve().parents[1]
    source_hashes = {path.relative_to(project).as_posix(): fingerprint(path.read_text())
                     for path in sorted((project / "src").rglob("*.py"))}
    summary = {
        "kind": "real_model_functional_check", "dataset": "bundled_synthetic_sales",
        "optimizer_profile": PROFILE, "model": model.info(), "max_calls": max_calls,
        "task_counts": {split: sum(t["split"] == split for t in db["tasks"])
                        for split in ("training", "selection", "holdout")},
        "query_succeeded": True, "confirmed_feedback_saved": True,
        "generated_skill_sha256": version["skill_sha256"],
        "release_decision": version["status"], "deployed_version": db["active"],
        "selection": {k: v for k, v in version["selection"].items() if k != "rows"},
        "bounded_probe": {"valid": True, "mode": "patch", "published": False,
                          "edit_report": probe["details"]["edit_report"]},
        "comparison_completed": True, "benchmark_performance_claim": False,
        "request_count": model.calls, "response_count": len(responses),
        "error_count": sum(e["kind"] == "error" for e in events),
        "total_tokens": sum(u["total_tokens"] for u in usages) if known else None,
        "source_hashes": source_hashes,
    }
    output.mkdir(parents=True, exist_ok=True)
    write(output / "summary.json", summary)
    write(output / "query.json", query)
    write(output / "comparison.json", report)
    write(output / "bounded-probe.json", probe)
    (output / "initial-SKILL.md").write_text(version["skill"])
    (output / "bounded-SKILL.md").write_text(probe["skill"])
    shutil.copy2(model.log, output / "requests.jsonl")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description="限额验证真实查询、Skill 生成、局部补丁与冻结对比")
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--output", type=Path, default=Path("workspaces/live-check-output"))
    parser.add_argument("--max-calls", type=int, default=50)
    args = parser.parse_args()
    if args.max_calls < 1:
        parser.error("--max-calls 必须大于零")
    if args.env_file:
        load_environment(args.env_file)
    verify(args.workspace.resolve(), args.output.resolve(), args.max_calls)


if __name__ == "__main__":
    main()
