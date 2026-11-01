"""Bounded skill evolution with resumable proposals and task observations."""

import argparse
import json
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from evogate.sql.contracts import Execution, Limits
from evogate.sql.sandbox import execute
from evogate.sql.scorer import evaluate_result

from .data import fingerprint, import_bird, import_manifest
from .experience import EvolutionPolicy
from .patches import add_edit_arguments, budget_from_arguments
from .recording import RecordedModel, load_environment
from .service import Service, preview, serializable


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(serializable(value), ensure_ascii=False, indent=2))
    temporary.replace(path)


def progress(**values):
    print(json.dumps(values, ensure_ascii=False), flush=True)


class RoundService(Service):
    def __init__(self, root, workers, model, edit_budget=None):
        super().__init__(root, model=model, edit_budget=edit_budget)
        self.workers = workers
        self.sql_slots = threading.BoundedSemaphore(min(workers, 4))

    def sql(self, db, sql):
        with self.sql_slots:
            return super().sql(db, sql)

    def evaluate_stream(self, db, tasks, skill):
        return self.evaluate(db, tasks, skill)

    def evaluate(self, db, tasks, skill):
        cache = self.root / "observations" / db["id"] / fingerprint(skill)
        cache.mkdir(parents=True, exist_ok=True)

        def evaluate_one(task):
            path = cache / (fingerprint(task["id"])[:20] + ".json")
            cached = json.loads(path.read_text()) if path.exists() else None
            if cached is not None and cached["score"] is not None:
                return cached
            reference_path = self.root / "references" / (fingerprint(task["id"])[:20] + ".json")
            reference = (Execution.model_validate(json.loads(reference_path.read_text()))
                         if reference_path.exists() else self.sql(db, task["reference_sql"]))
            if cached is None:
                observation, prediction = self.run(
                    db, task["question"], task["evidence"], skill, task,
                )
            else:
                prediction = self.sql(db, cached["sql"])
                observation = {**cached, "execution": preview(prediction),
                               "trace": [*cached.get("trace", []), {
                                   "kind": "cached_sql_recheck", "result": preview(prediction),
                               }]}
            evaluation = evaluate_result(prediction, reference)
            row = {**observation, "task_id": task["id"], "question": task["question"],
                   "score": evaluation.score, "reason": evaluation.reason}
            write(path, row)
            return row

        rows = {}
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            pending = {pool.submit(evaluate_one, task): task for task in tasks}
            for future in as_completed(pending):
                task = pending[future]
                rows[task["id"]] = future.result()
                if len(rows) % 10 == 0 or len(rows) == len(tasks) and len(tasks) > 1:
                    progress(event="evaluation", db=db["id"], skill=fingerprint(skill)[:8],
                             completed=len(rows), total=len(tasks), calls=self.model.calls)
        return [rows[task["id"]] for task in tasks]

    def propose(self, db, training, incumbent):
        path = self.root / "candidates" / db["id"] / f"v{len(db['versions'])}.json"
        legacy_path = self.root / "candidates" / f"{db['id']}.json"
        if len(db["versions"]) == 1 and legacy_path.exists():
            path = legacy_path
        budget_context = self.edit_budget.model_dump()
        if budget_context["min_char_budget"] == 0:
            # Keep hashes of saved ratio-only protocols compatible with their candidates.
            budget_context.pop("min_char_budget")
        context_hash = fingerprint(json.dumps(serializable({
            "incumbent": incumbent, "training": training,
            "feedback": [r for r in self.state["feedback"] if r["db_id"] == db["id"]],
            "experiences": [e for e in self.state["experiences"].values()
                            if e["db_id"] == db["id"]],
            "released_task_ids": db.get("released_task_ids"), "branch_id": db["branch_id"],
            "previous_version": db["versions"][-1]["id"], "profile": self.optimizer_profile,
            "edit_budget": budget_context,
        }), ensure_ascii=False, sort_keys=True))
        if path.exists():
            previous = json.loads(path.read_text())
            details = previous.get("optimizer", {"profile": "legacy", "valid": True})
            if details["profile"] != self.optimizer_profile:
                raise ValueError("候选缓存来自不同优化器，请使用对应配置或新 workspace。")
            if previous.get("context_hash", context_hash) != context_hash:
                raise ValueError("候选缓存的开发上下文已改变，请使用新 workspace。")
            self.optimizer_details = details
            return previous["skill"], previous["usage"]
        candidate, usage = super().propose(db, training, incumbent)
        write(path, {"skill": candidate, "usage": usage, "optimizer": self.optimizer_details,
                     "context_hash": context_hash})
        return candidate, usage

    def reference_execution(self, db, task):
        path = self.root / "references" / (fingerprint(task["id"])[:20] + ".json")
        return (Execution.model_validate(json.loads(path.read_text()))
                if path.exists() else self.sql(db, task["reference_sql"]))


def prepare(root, workers, source, db_root, db_ids, per_split=10, seed=42,
            max_calls=1000, split_manifest=None, rounds=3, edit_budget=None,
            batch_size=10, initial_skill=None, evolution_policy=None):
    if type(rounds) is not int or not 1 <= rounds <= 10:
        raise ValueError("进化轮数须为 1–10。")
    protocol_path = root / "round-protocol.json"
    if protocol_path.exists():
        raise ValueError("工作区已准备完成；请用 run 继续，或选择新工作区。")
    service = Service(root, live=True, edit_budget=edit_budget)
    if (service.state["queries"] or service.state["feedback"] or service.state["reports"]
            or set(service.state["databases"]) != {"sales-demo"}
            or len(service.db("sales-demo")["versions"]) > 1):
        raise ValueError("准备评测需要新的工作区。")
    if batch_size < 1:
        raise ValueError("batch-size 必须大于零。")
    service.configure_evolution(**(evolution_policy or {}))
    skill_id = service.import_skill(initial_skill)["id"] if initial_skill else "sql-analysis"
    exclusions_path = root / "excluded-references.json"
    exclusions = json.loads(exclusions_path.read_text()) if exclusions_path.exists() else []

    def check(item):
        db, task = item
        result = execute(Path(db["path"]), task["reference_sql"], Limits(
            wall_seconds=8, cpu_seconds=6, max_rows=10000,
        ))
        path = root / "references" / (fingerprint(task["id"])[:20] + ".json")
        write(path, result.model_dump())
        return {"task_id": task["id"], "split": task["split"],
                "status": result.status, "detail": result.detail}

    for attempt in range(12):
        databases = (import_manifest(source, db_root, split_manifest) if split_manifest else
                     import_bird(source, db_root, db_ids, per_split, seed,
                                 excluded_ids=[row["task_id"] for row in exclusions]))
        tasks = [(db, task) for db in databases for task in db["tasks"]]
        invalid = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for index, result in enumerate(pool.map(check, tasks), 1):
                if result["status"] != "success":
                    invalid.append(result)
                if index % 30 == 0 or index == len(tasks):
                    progress(event="reference_check", attempt=attempt + 1, completed=index,
                             total=len(tasks), failures=len(invalid))
        write(root / "preflight.json", {"tasks": len(tasks), "invalid": invalid,
                                        "excluded_count": len(exclusions)})
        if not invalid:
            service.add_databases(databases, skill_id)
            for db in databases:
                db["released_task_ids"] = []
                db["stream_round"] = 0
            service.save()
            write(protocol_path, {
                "kind": "workspace-skill-stream", "created": time.time(),
                "databases": [db["id"] for db in databases], "seed": seed,
                "split_counts": {db["id"]: dict(Counter(t["split"] for t in db["tasks"]))
                                 for db in databases},
                "candidate_rounds": rounds, "max_calls": max_calls,
                "batch_size": batch_size, "skill_id": skill_id,
                "evolution_policy": service.state["evolution_policy"],
                "initial_skill_sha256": service.state["skill_catalog"][skill_id]["sha256"],
                "model": service.model.info(), "optimizer_profile": service.optimizer_profile,
                "edit_budget": service.edit_budget.model_dump(),
                "gate": "candidate >= initial and paired net improvements >= policy threshold",
                "holdout": "freeze snapshot; initial/evolved/no-skill comparison",
                "dataset_hashes": {db["id"]: db["dataset_hash"] for db in databases},
                "reference_exclusions": exclusions,
            })
            progress(event="prepared", tasks=len(tasks),
                     databases=[db["id"] for db in databases], excluded_count=len(exclusions))
            return
        if split_manifest:
            raise ValueError("任务清单的参考 SQL 预检失败；详见 preflight.json，不更改清单。")
        exclusions.extend(invalid)
        write(exclusions_path, exclusions)
    raise ValueError("参考预检补位未完成，尚未调用模型。")


def run(root, workers):
    protocol = json.loads((root / "round-protocol.json").read_text())
    preflight = json.loads((root / "preflight.json").read_text())
    if preflight["invalid"]:
        raise ValueError("参考预检未通过。")
    model = RecordedModel(root / "requests", protocol["max_calls"], quiet=True)
    if model.info() != protocol["model"]:
        raise ValueError("模型配置与冻结运行配置不符。")
    service = RoundService(root, workers, model, edit_budget=protocol.get("edit_budget"))
    if (protocol.get("evolution_policy") is not None
            and service.state["evolution_policy"] != protocol["evolution_policy"]):
        raise ValueError("进化策略与固定协议不符，请恢复策略或准备新的评测工作区。")
    databases = protocol["databases"]
    if protocol.get("optimizer_profile", "legacy") != service.optimizer_profile:
        raise ValueError("优化器与冻结协议不符；请将 EVOGATE_OPTIMIZER 设置为协议中的配置。")
    # Finish development and selection for all databases before any final evaluation.
    for db_id in databases:
        db = service.db(db_id)
        if protocol.get("kind") == "workspace-skill-stream":
            while db.get("stream_round", 0) < protocol["candidate_rounds"]:
                count = service.release_batch(db_id, protocol["batch_size"])
                if not count:
                    break
                if len(db["versions"]) == db["pending_window"]["version_count"]:
                    version = service.optimize(db_id)
                else:
                    version = db["versions"][-1]
                db["stream_round"] += 1
                db["pending_window"] = False
                service.save()
                progress(event="candidate_decision", db=db_id, version=version["id"],
                         status=version["status"], released=len(db["released_task_ids"]),
                         selection={k: v for k, v in version["selection"].items() if k != "rows"})
        else:
            # Previously prepared manifests keep their original batch behavior.
            while len(db["versions"]) - 1 < protocol.get("candidate_rounds", 1):
                last = db["versions"][-1]
                if (last["parent"] is not None and last["optimizer"].get("valid", True)
                        and last["skill"] == service.version(db, last["parent"])["skill"]):
                    break
                service.optimize(db_id)
    for db_id in databases:
        report = service.compare(db_id)
        progress(event="holdout_completed", db=db_id, initial=report["baseline"],
                 deployed=report["optimized"], no_skill=report.get("no_skill"),
                 improved=report["improved"], regressed=report["regressed"],
                 version=report["version"])
    summarize(service)


def summarize(service):
    root = service.root
    usage = Counter()
    with (root / "requests/requests.jsonl").open() as source:
        for line in source:
            event = json.loads(line)
            usage[event["kind"]] += 1
            if event["kind"] == "response":
                if event.get("usage"):
                    for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
                        usage[name] += event["usage"].get(name, 0)
                else:
                    usage["unknown_usage"] += 1
    summary = {"usage": dict(usage), "databases": []}
    protocol = json.loads((root / "round-protocol.json").read_text())
    for db_id in protocol["databases"]:
        db = service.db(db_id)
        version = db["versions"][-1]
        report = next(r for r in service.state["reports"] if r["db_id"] == db_id)
        summary["databases"].append({
            "db_id": db_id, "selection_baseline": version["selection"]["baseline"],
            "selection_candidate": version["selection"]["candidate"],
            "decision": version["status"], "deployed": db["active"],
            "holdout_baseline": report["baseline"], "holdout_deployed": report["optimized"],
            "holdout_count": report["count"], "improved": report["improved"],
            "regressed": report["regressed"],
            "rounds": [{"version": v["id"], "parent": v["parent"], "status": v["status"],
                        "candidate": v["selection"]["candidate"],
                        "skill_path": v.get("skill_path")} for v in db["versions"][1:]],
        })
    reports = [r for r in service.state["reports"] if r["db_id"] in protocol["databases"]]
    summary["macro_average"] = {
        name: sum(r[name] for r in reports) / len(reports)
        if reports and all(r.get(name) is not None for r in reports) else None
        for name in ("baseline", "optimized", "no_skill", "gain")
    }
    write(root / "summary.json", summary)
    progress(event="round_completed", **summary)


def main():
    parser = argparse.ArgumentParser(description="导入 BIRD，运行 Markdown Skill 进化与评测")
    parser.add_argument("step", choices=("prepare", "run"))
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--workers", type=int, default=4, help="并发评测任务数，必须大于零")
    parser.add_argument("--source", type=Path, help="BIRD JSON / JSONL 文件")
    parser.add_argument("--db-root", type=Path, help="包含 <db_id>/<db_id>.sqlite 的目录")
    parser.add_argument("--db-ids", nargs="+")
    parser.add_argument("--per-split", type=int, default=10)
    parser.add_argument("--min-net-improvements", type=int, default=1)
    parser.add_argument("--replay-size", type=int, default=40)
    parser.add_argument("--batch-size", type=int, default=10, help="每个经验窗口新增题数")
    parser.add_argument("--skill", type=Path, help="正式对照采用的初始 SKILL.md")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--max-calls", type=int, default=1000, help="累计模型请求上限")
    parser.add_argument("--rounds", type=int, default=3, choices=range(1, 11),
                        help="prepare 时固定的经验窗口数，默认 3；每个窗口释放新开发反馈")
    parser.add_argument("--split-manifest", type=Path, help="还原已发布的单库任务划分")
    add_edit_arguments(parser)
    args = parser.parse_args()
    if args.workers < 1:
        parser.error("--workers 必须大于零")
    if args.max_calls < 1:
        parser.error("--max-calls 必须大于零")
    if args.env_file:
        load_environment(args.env_file)
    if args.step == "prepare":
        if not args.source or not args.db_root or not (args.db_ids or args.split_manifest):
            parser.error("prepare 需要 --source、--db-root 以及 --db-ids 或 --split-manifest")
        if args.db_ids and args.split_manifest:
            parser.error("--db-ids 与 --split-manifest 二选一")
        prepare(args.workspace, args.workers, args.source, args.db_root, args.db_ids,
                args.per_split, args.seed, args.max_calls, args.split_manifest, args.rounds,
                budget_from_arguments(args), args.batch_size,
                args.skill.read_text() if args.skill else None,
                EvolutionPolicy(min_net_improvements=args.min_net_improvements,
                                replay_size=args.replay_size).model_dump())
    else:
        run(args.workspace, args.workers)


if __name__ == "__main__":
    main()
