"""Persistent local application: query, correct, evolve, freeze, compare."""

import base64
import copy
import difflib
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path

from evogate.sql import extract_sql, schema
from evogate.sql.contracts import Execution, Limits
from evogate.sql.sandbox import execute
from evogate.sql.scorer import evaluate_result

from .data import database, demo, fingerprint, sql_template
from .experience import (
    BudgetModel,
    EvolutionPolicy,
    failure_kind,
    question_key,
    select_replay,
    trigger_reason,
)
from .model import Model
from .optimizer import EvidenceOptimizer, MarkdownOptimizer, comparison, execution_view
from .patches import EditBudget, verify_document_update
from .skills import (
    FULL_DOCUMENT_PROFILE,
    MARKDOWN_PROFILES,
    MAX_SKILL_CHARS,
    PROFILE,
    SkillStore,
    parse_document,
)

TOOLS = [{
    "type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties,
                       "required": list(properties), "additionalProperties": False},
    },
} for name, description, properties in [
    ("query_values", "Inspect up to 12 distinct stored values of a schema field.", {
        "table": {"type": "string"}, "column": {"type": "string"},
    }),
    ("execute_sql", "Execute a read-only SQLite query. Returns a bounded preview or error.", {
        "sql": {"type": "string"},
    }),
]]


def identifier():
    return uuid.uuid4().hex[:12]


def serializable(value):
    if isinstance(value, bytes):
        return {"blob_base64": base64.b64encode(value).decode()}
    if isinstance(value, dict):
        return {key: serializable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [serializable(item) for item in value]
    return value


def preview(result):
    return {
        "status": result.status, "detail": result.detail,
        "rows": serializable(result.rows[:30]), "row_count": len(result.rows),
        "truncated": len(result.rows) > 30,
    }


class Service:
    def __init__(self, root, live=False, model=None, optimizer_profile=None, edit_budget=None):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.file = self.root / "state.json"
        self.lock = threading.RLock()
        self.report_lock = threading.Lock()
        self.model = model or Model(live)
        self.optimizer_profile = optimizer_profile or os.getenv("EVOGATE_OPTIMIZER", PROFILE)
        if self.optimizer_profile not in {"legacy", "evidence_v2", *MARKDOWN_PROFILES}:
            raise ValueError("EVOGATE_OPTIMIZER 配置无效。")
        self.edit_budget = (EditBudget.runtime_default() if edit_budget is None
                            else EditBudget.model_validate(edit_budget))
        self.skills = SkillStore(self.root)
        self.optimizer_details = {}
        self.limits = Limits(wall_seconds=8, cpu_seconds=6, max_rows=10000)
        self.state = (json.loads(self.file.read_text()) if self.file.exists() else {
            "databases": {}, "queries": [], "feedback": [], "reports": [],
        })
        self.state.setdefault("skill_catalog", {})
        self.state.setdefault("skill_branches", {})
        self.state.setdefault("experiences", {})
        self.state.setdefault("jobs", [])
        self.state.setdefault("evolution_policy", EvolutionPolicy().model_dump())
        self.state.setdefault("processed_events", {})
        seed = (Path(__file__).parent / "skills/sql-analysis/SKILL.md").read_text()
        if "sql-analysis" not in self.state["skill_catalog"]:
            self.state["skill_catalog"]["sql-analysis"] = {
                "id": "sql-analysis", "skill": seed, "metadata": parse_document(seed),
                "created": time.time(), "sha256": fingerprint(seed),
            }
        for db in self.state["databases"].values():
            if "branch_id" not in db:
                # Existing documents and report files retain their original bytes and paths.
                branch_id = identifier()
                self.state["skill_branches"][branch_id] = {
                    "id": branch_id, "skill_id": "legacy-" + db["id"], "db_id": db["id"],
                    "versions": db["versions"], "active": db["active"], "initial": "v0",
                }
                db["branch_id"] = branch_id
            self.attach_branch(db)
            for version in db["versions"]:
                if version["skill"] and not version.get("skill_path"):
                    self.skills.persist(db["branch_id"], version)
                    version.setdefault("skill_format", "legacy_text")
        prior_modes = {v["mode"] for branch in self.state["skill_branches"].values()
                       for v in branch["versions"] if "mode" in v}
        prior_modes.update(r["model"]["mode"] for r in self.state["reports"])
        if prior_modes - {self.model.info()["mode"]}:
            raise ValueError("该工作区包含另一模式的实验，请为 demo/live 使用不同 workspace。")
        for job in self.state["jobs"]:
            if job["status"] == "running":
                job.update(status="interrupted", error="进程中断；经验已保留，可重新发起。")
        if not self.state["databases"]:
            self.add_databases([demo(self.root)])
        if self.state.get("schema_version", 1) < 2:
            queries = {q["id"]: q for q in self.state["queries"]}
            for query in queries.values():
                self.record_experience(self.db(query["db_id"]), query, validate_split=False)
            for feedback in self.state["feedback"]:
                query = queries.get(feedback.get("query_id"))
                if query:
                    self.record_experience(self.db(query["db_id"]), query,
                                           feedback["reference_sql"], feedback.get("note", ""),
                                           feedback.get("source", "user_confirmed"),
                                           validate_split=False)
            self.state["schema_version"] = 2
        self.save()

    def attach_branch(self, db):
        branch = self.state["skill_branches"][db["branch_id"]]
        # In-memory views preserve existing SQL/evaluation callers. Persist only the registry.
        db["versions"], db["active"] = branch["versions"], branch["active"]
        return branch

    def set_active(self, db, version_id):
        self.state["skill_branches"][db["branch_id"]]["active"] = version_id
        db["active"] = version_id

    def save(self):
        with self.lock:
            state = {**self.state, "databases": {
                key: {k: v for k, v in db.items() if k not in {"versions", "active"}}
                for key, db in self.state["databases"].items()
            }}
            temporary = self.file.with_suffix(".tmp")
            temporary.write_text(json.dumps(serializable(state), ensure_ascii=False, indent=2))
            os.replace(temporary, self.file)

    def add_databases(self, databases, skill_id="sql-analysis"):
        with self.lock:
            if skill_id not in self.state["skill_catalog"]:
                raise ValueError("Skill 不存在。")
            for db in databases:
                if db["id"] in self.state["databases"]:
                    raise ValueError(f"数据库已注册：{db['id']}；请使用新的 workspace 重新划分。")
            for db in databases:
                seed = self.state["skill_catalog"][skill_id]["skill"]
                # The deterministic showcase retains its intentionally empty baseline.
                if db["source"] == "synthetic":
                    seed = ""
                branch_id = identifier()
                initial = {"id": "v0", "skill": seed, "status": "baseline",
                           "created": time.time(), "parent": None, "diff": "", "selection": None}
                self.skills.persist(branch_id, initial)
                self.state["skill_branches"][branch_id] = {
                    "id": branch_id, "skill_id": skill_id, "db_id": db["id"],
                    "versions": [initial], "active": "v0", "initial": "v0",
                }
                db["branch_id"] = branch_id
                self.attach_branch(db)
                self.state["databases"][db["id"]] = db
            self.save()

    def import_skill(self, document):
        metadata = parse_document(document)
        skill_id = metadata["name"] + "-" + fingerprint(document)[:10]
        with self.lock:
            self.state["skill_catalog"].setdefault(skill_id, {
                "id": skill_id, "skill": document, "metadata": metadata,
                "created": time.time(), "sha256": fingerprint(document),
            })
            self.save()
        return self.state["skill_catalog"][skill_id]

    def bind_skill(self, db_id, skill_id):
        with self.lock:
            db = self.db(db_id)
            self.require_idle(db_id)
            if skill_id not in self.state["skill_catalog"]:
                raise ValueError("Skill 不存在。")
            branch_id = identifier()
            initial = {"id": "v0", "skill": self.state["skill_catalog"][skill_id]["skill"],
                       "status": "baseline", "created": time.time(), "parent": None,
                       "diff": "", "selection": None}
            self.skills.persist(branch_id, initial)
            self.state["skill_branches"][branch_id] = {
                "id": branch_id, "skill_id": skill_id, "db_id": db_id,
                "versions": [initial], "active": "v0", "initial": "v0",
            }
            db["branch_id"] = branch_id
            self.attach_branch(db)
            for exp in self.state["experiences"].values():
                if exp["db_id"] == db_id:
                    exp["resolved"] = False
            self.save()
            return {"branch_id": branch_id, "skill_id": skill_id}

    def register_database(self, db_id, path, label="", context="", skill_id="sql-analysis"):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", db_id):
            raise ValueError("数据库 ID 格式无效。")
        path = Path(path).expanduser().resolve()
        if not path.is_file():
            raise ValueError("SQLite 文件不存在。")
        db = database(db_id, label or db_id, path, [], "user", {})
        db["context"] = context
        self.add_databases([db], skill_id)
        return {"db_id": db_id, "branch_id": db["branch_id"]}

    def switch_branch(self, db_id, branch_id):
        with self.lock:
            db = self.db(db_id)
            self.require_idle(db_id)
            branch = self.state["skill_branches"].get(branch_id)
            if not branch or branch["db_id"] != db_id:
                raise ValueError("分支不属于当前数据库。")
            db["branch_id"] = branch_id
            self.attach_branch(db)
            for exp in self.state["experiences"].values():
                if exp["db_id"] == db_id:
                    exp["resolved"] = False
            self.save()
            return {"branch_id": branch_id, "active": branch["active"]}

    def add_cases(self, db_id, cases):
        with self.lock:
            db = self.db(db_id)
            self.require_idle(db_id)
            if not isinstance(cases, list) or not cases:
                raise ValueError("案例必须是非空 JSON 数组。")
            proposed = copy.deepcopy(db["tasks"])
            for case in cases:
                split = case.get("split")
                if split not in {"training", "selection", "holdout"}:
                    raise ValueError("任务 split 必须为 training、selection 或 holdout。")
                if not str(case.get("question", "")).strip():
                    raise ValueError("任务问题不能为空。")
                sql = extract_sql(case["reference_sql"])
                if self.sql(db, sql).status != "success":
                    raise ValueError("参考 SQL 执行失败。")
                template = sql_template(sql)
                for prior in proposed:
                    same_question = (question_key(prior["question"])
                                     == question_key(case["question"]))
                    if same_question or (prior["split"] != split
                                         and sql_template(prior["reference_sql"]) == template):
                        raise ValueError("任务重复或 SQL 模板跨划分。")
                for exp in self.state["experiences"].values():
                    if exp["db_id"] != db_id or split == "training":
                        continue
                    if (question_key(exp["question"]) == question_key(case["question"])
                            or exp.get("reference_sql")
                            and sql_template(exp["reference_sql"]) == template):
                        raise ValueError("已暴露经验不能进入选择或保留集。")
                proposed.append({"id": "case:" + identifier(), "split": split,
                                 "question": case["question"], "evidence": case.get("evidence", ""),
                                 "reference_sql": sql})
            db["tasks"] = proposed
            db["dataset_hash"] = fingerprint(
                json.dumps(proposed, sort_keys=True, ensure_ascii=False))
            self.save()
            return {"added": len(cases)}

    def db(self, db_id):
        if db_id not in self.state["databases"]:
            raise ValueError("数据库不存在。")
        db = self.state["databases"][db_id]
        if fingerprint(schema(Path(db["path"]))) != db["schema_hash"]:
            raise ValueError("数据库结构已改变，请在新的 workspace 中重新导入。")
        self.attach_branch(db)
        return db

    def version(self, db, version_id=None):
        version = next(v for v in db["versions"] if v["id"] == (version_id or db["active"]))
        return {**version, "skill": self.skills.load(version)}

    def public_state(self):
        databases = []
        for db in self.state["databases"].values():
            databases.append({
                key: db[key] for key in (
                    "id", "label", "source", "schema", "active", "versions", "metadata", "branch_id"
                )
            } | {
                "versions": [{**v, "skill": self.skills.load(v)} for v in db["versions"]],
                "counts": {split: sum(t["split"] == split for t in db["tasks"])
                           for split in ("training", "selection", "holdout")},
                "examples": [{"id": t["id"], "question": t["question"],
                              "evidence": t["evidence"]}
                             for t in db["tasks"] if t["split"] == "training"],
                "frozen": False,
                "context": db.get("context", ""),
                "skill_id": self.state["skill_branches"][db["branch_id"]]["skill_id"],
            })
        return {
            "model": self.model.info(), "databases": databases,
            "optimizer_profile": self.optimizer_profile,
            "edit_budget": self.edit_budget.model_dump(),
            "queries": self.state["queries"][-50:], "feedback": self.state["feedback"],
            "reports": self.state["reports"],
            "skills": list(self.state["skill_catalog"].values()),
            "branches": [{key: branch[key] for key in ("id", "db_id", "skill_id", "active")}
                         for branch in self.state["skill_branches"].values()],
            "experiences": list(self.state["experiences"].values()),
            "jobs": self.state["jobs"][-30:], "evolution_policy": self.state["evolution_policy"],
        }

    def sql(self, db, sql):
        return execute(Path(db["path"]), sql, self.limits)

    def tool(self, db, name, arguments):
        if name == "execute_sql":
            return preview(self.sql(db, arguments["sql"]))
        if name == "query_values":
            table, column = arguments["table"], arguments["column"]
            # Quote identifiers; values from untrusted model output are never interpolated raw.
            table = '"' + table.replace('"', '""') + '"'
            column = '"' + column.replace('"', '""') + '"'
            return preview(self.sql(db, f"SELECT DISTINCT {column} FROM {table} LIMIT 12"))
        raise ValueError("未知工具。")

    def run(self, db, question, evidence, skill, task=None):
        trace, usages = [], []
        if skill:
            trace.append({"kind": "skill_loaded", "sha256": fingerprint(skill),
                          "characters": len(skill)})
        start = time.monotonic()
        if not self.model.live:
            if db["source"] != "synthetic" or task is None:
                raise ValueError("离线模式请选择销售演示库中的示例；BIRD 查询需要 --live。")
            count = task["demo_count"]
            expression = (
                "COUNT(DISTINCT o.id)" if count and "COUNT(DISTINCT" in skill.upper()
                else "COUNT(*)" if count else "SUM(i.amount)"
            )
            sql = (
                f"SELECT {expression} FROM orders o JOIN items i ON i.order_id=o.id "
                f"WHERE o.region='{task['demo_region']}'"
            )
            result = self.sql(db, sql)
            trace.append({"kind": "scripted_demo", "note": "预设输出，仅演示流程"})
        else:
            messages = [{
                "role": "system", "content": (
                    "You are a SQLite analysis assistant. Use tools to inspect actual values "
                    "and test/repair queries when necessary. Never invent fields. "
                    "Final response: exactly one read-only SQL query, without explanation. "
                    "Database schema:\n" + db["schema"]
                    + "\nBusiness context:\n" + db.get("context", "")
                ),
            }]
            if skill:
                prefix = "Database-local skill:\n"
                if skill.startswith(("---\n", "---\r\n")):
                    prefix = (
                        "Database-local skill loaded from SKILL.md. Follow its applicable "
                        "workflow using the available tools. The question, explicit evidence "
                        "and actual schema determine the answer.\n\n"
                    )
                messages.append({"role": "system", "content": prefix + skill})
            messages.append({"role": "user", "content": json.dumps({
                "question": question, "evidence": evidence,
            }, ensure_ascii=False)})
            sql, result = "", Execution(status="rejected", detail="未生成有效 SQL")
            tool_count = 0
            for turn in range(6):
                message, usage = self.model.complete(messages, TOOLS if turn < 5 else None)
                usages.append(usage)
                messages.append({key: message[key] for key in ("role", "content", "tool_calls")
                                 if key in message})
                calls = message.get("tool_calls") or []
                if calls:
                    if message.get("content"):
                        trace.append({"kind": "assistant", "content": message["content"]})
                    if tool_count + len(calls) > 8:
                        sql = ""
                        result = Execution(status="rejected", detail="tool_call_limit")
                        trace.append({"kind": "tool_budget_exhausted",
                                      "executed": tool_count, "requested": len(calls)})
                        break
                    for call in calls:
                        function = call["function"]
                        try:
                            arguments = json.loads(function["arguments"])
                            output = self.tool(db, function["name"], arguments)
                        except (ValueError, KeyError, TypeError) as exc:
                            output = {"error": str(exc)}
                        trace.append({"kind": "tool", "name": function["name"],
                                      "arguments": function["arguments"], "result": output})
                        messages.append({"role": "tool", "tool_call_id": call["id"],
                                         "content": json.dumps(output, ensure_ascii=False)})
                        tool_count += 1
                    continue
                try:
                    sql = extract_sql(message.get("content") or "")
                    result = self.sql(db, sql)
                except ValueError as exc:
                    result = Execution(status="rejected", detail=str(exc))
                trace.append({"kind": "generation", "sql": sql, "result": preview(result)})
                if result.status == "success":
                    break
                messages.append({"role": "user", "content": (
                    f"Execution status: {result.status}; error: {result.detail}. "
                    "Repair the query; preserve the question's semantics. Return only SQL."
                )})
        known = [u for u in usages if isinstance(u, dict)
                 and isinstance(u.get("total_tokens"), int)]
        return {
            "sql": sql, "execution": preview(result), "trace": trace,
            "elapsed_ms": round((time.monotonic() - start) * 1000),
            "tokens": sum(u["total_tokens"] for u in known) if len(known) == len(usages)
            and usages else None,
            "model_calls": len(usages),
        }, result

    def query(self, db_id, question, evidence="", baseline=False, task_id=None):
        db = self.db(db_id)
        question = question.strip()
        if not question or len(question) > 10000:
            raise ValueError("请输入不超过 10000 字的问题。")
        task = next((t for t in db["tasks"] if t["split"] == "training"
                     and t["id"] == task_id), None)
        if task_id and task is None:
            raise ValueError("只允许使用开发示例进行交互查询。")
        if task:
            question, evidence = task["question"], task["evidence"]
        if any(question_key(t["question"]) == question_key(question)
               for t in db["tasks"] if t["split"] != "training"):
            raise ValueError("选择与保留问题仅供独立评测。")
        with self.lock:
            version = self.version(db)
            branch_id = db["branch_id"]
        row, _ = self.run(db, question, evidence, "" if baseline else version["skill"], task)
        row.update({"id": identifier(), "db_id": db_id, "question": question,
                    "evidence": evidence, "version": "no-skill" if baseline else version["id"],
                    "branch_id": branch_id,
                    "task_id": task_id, "created": time.time(),
                    "mode": self.model.info()["mode"],
                    "skill_path": None if baseline else version.get("skill_path")})
        with self.lock:
            self.state["queries"].append(row)
            self.record_experience(db, row)
            self.save()
        return row

    def feedback(self, query_id, reference_sql, note="", source="user_confirmed"):
        if source not in {"user_confirmed", "reviewed_feedback"}:
            raise ValueError("反馈来源无效。")
        query = next((q for q in self.state["queries"] if q["id"] == query_id), None)
        if query is None:
            raise ValueError("查询记录不存在。")
        db = self.db(query["db_id"])
        sql = extract_sql(reference_sql)
        result = self.sql(db, sql)
        if result.status != "success":
            raise ValueError("纠正 SQL 执行失败：" + result.detail)
        row = {
            "id": identifier(), "db_id": db["id"], "query_id": query_id,
            "question": query["question"], "evidence": query["evidence"],
            "prediction_sql": query["sql"], "reference_sql": sql, "note": note[:5000],
            "source": source, "created": time.time(),
        }
        with self.lock:
            self.record_experience(db, query, sql, note, source)
            self.state["feedback"].append(row)
            self.save()
        return row

    def require_idle(self, db_id):
        if any(j["db_id"] == db_id and j["status"] == "running" for j in self.state["jobs"]):
            raise ValueError("该工作区正在进化，请在任务完成后修改配置或切换版本。")

    def record_experience(self, db, observation, reference_sql=None, note="", source="query",
                          validate_split=True):
        key = question_key(observation["question"], observation.get("evidence", ""))
        experience_id = fingerprint(db["id"] + key)[:24]
        existing = self.state["experiences"].get(experience_id)
        score = None
        if not reference_sql and existing and existing.get("reference_sql"):
            reference_sql = existing["reference_sql"]
            source, note = existing["source"], existing["note"]
        if reference_sql:
            # Source records are retained. Re-execution uses the current database snapshot.
            if validate_split and db["source"] != "synthetic" and any(
                t["split"] != "training"
                and sql_template(t["reference_sql"]) == sql_template(reference_sql)
                for t in db["tasks"]
            ):
                raise ValueError("纠错与选择或保留任务模板重叠，请调整评测划分。")
            reference = self.sql(db, reference_sql)
            predicted = self.sql(db, observation["sql"]) if observation["sql"] else Execution(
                status="rejected", detail="no_sql")
            score = evaluate_result(predicted, reference).score
        kind = failure_kind(observation, score)
        signal = "verified" if reference_sql else "execution_failure" if kind else "unverified"
        event_id = fingerprint(experience_id + (reference_sql or "") + signal + (kind or "")
                               + str(observation.get("version")))
        if existing and existing["event_id"] == event_id:
            return existing
        row = {
            "id": experience_id, "key": key, "db_id": db["id"],
            "branch_id": observation.get("branch_id", db["branch_id"]),
            "version": observation.get("version"), "query_id": observation.get("id"),
            "question": observation["question"], "evidence": observation.get("evidence", ""),
            "prediction_sql": observation["sql"], "execution": observation["execution"],
            "trace": observation.get("trace", []), "reference_sql": reference_sql,
            "note": note[:5000], "source": source, "score": score, "signal": signal,
            "failure_kind": kind, "resolved": False, "event_id": event_id,
            "updated": time.time(), "attempts": existing.get("attempts", []) if existing else [],
        }
        self.state["experiences"][experience_id] = row
        return row

    def development_tasks(self, db):
        released = db.get("released_task_ids")
        tasks = {t["id"]: t for t in db["tasks"] if t["split"] == "training"
                 and (released is None or t["id"] in released)}
        for exp in self.state["experiences"].values():
            if exp["db_id"] != db["id"] or not exp.get("reference_sql"):
                continue
            if any(question_key(t["question"], t.get("evidence", "")) == exp["key"]
                   for t in tasks.values()):
                continue
            tasks["experience:" + exp["id"]] = {
                "id": "experience:" + exp["id"], "split": "training",
                "question": exp["question"], "evidence": exp["evidence"],
                "reference_sql": exp["reference_sql"],
            }
        return list(tasks.values())

    def release_batch(self, db_id, size):
        if size < 1:
            raise ValueError("经验窗口大小必须大于零。")
        db = self.db(db_id)
        released = db.setdefault("released_task_ids", [])
        if not db.get("pending_window"):
            pending = [t for t in db["tasks"] if t["split"] == "training"
                       and t["id"] not in released][:size]
            if not pending:
                return 0
            db["pending_window"] = {"task_ids": [t["id"] for t in pending],
                                    "version": db["active"], "version_count": len(db["versions"])}
            self.save()
        window = db["pending_window"]
        current = self.version(db, window["version"])
        pending = {t["id"]: t for t in db["tasks"]
                   if t["id"] in window["task_ids"] and t["id"] not in released}
        for observation in self.evaluate_stream(db, list(pending.values()), current["skill"]):
            task = pending[observation["task_id"]]
            # Feedback becomes available only after this question's target rollout.
            observation.update(evidence=task["evidence"], version=current["id"])
            self.record_experience(db, observation, task["reference_sql"], source="benchmark")
            released.append(task["id"])
            self.save()
        return len(window["task_ids"])

    def configure_evolution(self, **settings):
        with self.lock:
            policy = EvolutionPolicy.model_validate({**self.state["evolution_policy"], **settings})
            self.state["evolution_policy"] = policy.model_dump()
            self.save()
            return policy.model_dump()

    def schedule(self, db_id, manual=False):
        with self.lock:
            db = self.db(db_id)
            if any(j["status"] == "running" for j in self.state["jobs"]):
                if manual:
                    raise ValueError("已有后台进化任务正在运行。")
                return None
            policy = EvolutionPolicy.model_validate(self.state["evolution_policy"])
            jobs = [j for j in self.state["jobs"] if j["db_id"] == db_id]
            if not manual and not any(t["split"] == "selection" for t in db["tasks"]):
                return None
            if not manual and (not policy.enabled or jobs and
                               time.time() - jobs[-1]["created"] < policy.cooldown_seconds):
                return None
            if sum(j["created"] > time.time() - 86400 for j in jobs) >= policy.max_attempts_per_day:
                if manual:
                    raise ValueError("已达到该工作区过去 24 小时的进化次数预算。")
                return None
            experiences = [e for e in self.state["experiences"].values() if e["db_id"] == db_id]
            processed = self.state["processed_events"].get(db["branch_id"], [])
            reason = "manual" if manual else trigger_reason(experiences, processed, policy)
            if not reason:
                return None
            job = {"id": identifier(), "db_id": db_id, "branch_id": db["branch_id"],
                   "status": "running", "reason": reason, "created": time.time()}
            self.state["jobs"].append(job)
            worker = copy.copy(self)
            worker.state = copy.deepcopy(self.state)
            worker.lock = threading.RLock()
            worker.model = BudgetModel(self.model, policy.max_calls_per_attempt)
            worker.save = lambda: None
            self.save()

        def run_job():
            try:
                version = worker.optimize(db_id)
                with self.lock:
                    branch = worker.state["skill_branches"][job["branch_id"]]
                    self.state["skill_branches"][job["branch_id"]] = branch
                    self.attach_branch(self.state["databases"][db_id])
                    for exp_id, exp in worker.state["experiences"].items():
                        latest = self.state["experiences"].get(exp_id)
                        if latest and latest["event_id"] == exp["event_id"]:
                            latest.update(attempts=exp["attempts"], resolved=exp["resolved"])
                    self.state["processed_events"][job["branch_id"]] = (
                        worker.state["processed_events"].get(job["branch_id"], []))
                    job.update(status="completed", version=version["id"],
                               decision=version["status"])
            except Exception as exc:
                with self.lock:
                    job.update(status="failed", error=str(exc))
            finally:
                with self.lock:
                    job.update(finished=time.time(), model_calls=worker.model.calls)
                    self.save()

        threading.Thread(target=run_job, name="evogate-evolution", daemon=True).start()
        return job

    def evaluate(self, db, tasks, skill):
        rows = []
        for task in tasks:
            reference = self.sql(db, task["reference_sql"])
            if reference.status != "success":
                raise ValueError(f"参考 SQL 无法评分：{task['id']} ({reference.status})")
            observation, prediction = self.run(
                db, task["question"], task["evidence"], skill, task,
            )
            evaluation = evaluate_result(prediction, reference)
            rows.append({"task_id": task["id"], "question": task["question"],
                         "score": evaluation.score, "reason": evaluation.reason, **observation})
        return rows

    def reference_execution(self, db, task):
        return self.sql(db, task["reference_sql"])

    def development_context(self, db, training):
        tasks = {task["id"]: task for task in self.development_tasks(db)}
        examples = []
        for row in training:
            if row["task_id"] not in tasks:
                raise ValueError("优化器只能接收开发任务。")
            task = tasks[row["task_id"]]
            reference = self.reference_execution(db, task)
            predicted = execution_view(row["execution"])
            expected = execution_view(preview(reference))
            examples.append({
                "task_id": task["id"], "question": task["question"],
                "evidence": task["evidence"], "prediction_sql": row["sql"],
                "reference_sql": task["reference_sql"], "score": row["score"],
                "prediction_execution": predicted, "reference_execution": expected,
                "comparison": comparison(predicted, expected, row["score"]),
            })
            if self.optimizer_profile in MARKDOWN_PROFILES:
                examples[-1].update({"trace": row.get("trace", []),
                                     "model_calls": row.get("model_calls")})
        corrections = [{**row, "source_id": "feedback:" + row["id"]}
                       for row in self.state["feedback"] if row["db_id"] == db["id"]]
        if self.optimizer_profile in MARKDOWN_PROFILES:
            queries = {row["id"]: row for row in self.state["queries"]}
            for correction in corrections:
                correction["trace"] = queries.get(correction["query_id"], {}).get("trace", [])
        return examples, corrections

    def optimizer_attempt_context(self, db, version):
        """Expose final decisions and paired development evidence without selection cases."""
        source = next((v for v in db["versions"]
                       if v["id"] == version.get("reused_version")), version)
        outcomes, _ = self.development_context(db, source.get("candidate_training", []))
        before = {row["task_id"]: row for row in source.get("training", [])}
        pairs = [{"task_id": row["task_id"],
                  "before_score": before[row["task_id"]]["score"],
                  "after_score": row["score"],
                  "before_sql": before[row["task_id"]]["sql"], "after_sql": row["sql"]}
                 for row in source.get("candidate_training", []) if row["task_id"] in before]
        optimizer = version.get("optimizer", {})
        return {
            "version": version["id"], "skill_md": self.skills.load(version),
            "status": version["status"], "document_valid": optimizer.get("valid", True),
            "document_error": optimizer.get("reason") if not optimizer.get("valid", True) else None,
            "gate_feedback": {key: value for key, value in (version.get("selection") or {}).items()
                              if key != "rows"},
            "development_source_version": source["id"],
            "development_outcomes": outcomes, "development_pairs": pairs,
        }

    def propose(self, db, training, incumbent):
        if not self.model.live:
            if self.optimizer_profile in MARKDOWN_PROFILES:
                skill = incumbent or (
                    Path(__file__).parent / "skills/sales-demo/SKILL.md"
                ).read_text()
                self.optimizer_details = {
                    "profile": self.optimizer_profile, "valid": True,
                    "metadata": parse_document(skill), "scripted": True,
                    "mode": "patch" if incumbent else "bootstrap",
                    "base_sha256": fingerprint(incumbent),
                    "edit_budget": self.edit_budget.model_dump(),
                    "patch": {"base_sha256": fingerprint(incumbent), "edits": []}
                    if incumbent else None,
                }
                return skill, None
            return (
                "统计订单数量时，若 orders 连接 items 导致一笔订单出现多行，"
                "使用 COUNT(DISTINCT o.id) 计数；统计商品金额仍对明细金额求和。"
            ), None
        if self.optimizer_profile in MARKDOWN_PROFILES:
            examples, corrections = self.development_context(db, training)
            previous_attempt = None
            if len(db["versions"]) > 1:
                previous_attempt = self.optimizer_attempt_context(db, db["versions"][-1])
            recent_candidates = [v for v in db["versions"]
                                 if v["status"] in {"rejected", "pending"}
                                 and not v.get("reused_version")][-5:]
            rejected_updates = [{
                **self.optimizer_attempt_context(db, v),
                "base_sha256": v.get("optimizer", {}).get("base_sha256"),
                "patch": v.get("optimizer", {}).get("patch"),
                "outcome": v["status"],
            } for v in recent_candidates]
            history = select_replay([
                e for e in self.state["experiences"].values() if e["db_id"] == db["id"]
                and e["signal"] != "unverified"
            ], EvolutionPolicy.model_validate(self.state["evolution_policy"]).replay_size)
            corrections = history or corrections[-10:]
            proposal = MarkdownOptimizer(
                self.model.complete_skill, self.edit_budget,
                bounded=self.optimizer_profile != FULL_DOCUMENT_PROFILE,
            ).propose(
                db["schema"], incumbent, examples, corrections, previous_attempt,
                rejected_updates,
            )
            self.optimizer_details = proposal.details
            return proposal.skill, proposal.usage
        if self.optimizer_profile == "evidence_v2":
            examples, corrections = self.development_context(db, training)
            current = self.version(db)
            existing_rules = current.get("optimizer", {}).get("rules", [])
            proposal = EvidenceOptimizer(self.model.complete).propose(
                db["schema"], incumbent, examples, corrections, existing_rules,
            )
            self.optimizer_details = proposal.details
            return proposal.skill, proposal.usage
        return self.legacy_propose(db, training, incumbent)

    def legacy_propose(self, db, training, incumbent):
        tasks = {t["id"]: t for t in db["tasks"] if t["split"] == "training"}
        examples = [{
            "question": tasks[row["task_id"]]["question"],
            "evidence": tasks[row["task_id"]]["evidence"],
            "prediction_sql": row["sql"], "execution": row["execution"],
            "score": row["score"], "reference_sql": tasks[row["task_id"]]["reference_sql"],
        } for row in training]
        corrections = [r for r in self.state["feedback"] if r["db_id"] == db["id"]]
        message, usage = self.model.complete([{
            "role": "system", "content": (
                "Improve a database-local SQL skill using ONLY supplied training examples "
                "and confirmed/reviewed corrections. Return plain skill text, <=4000 characters. "
                "State when each rule applies. Preserve useful existing rules; remove harmful "
                "ones. Distinguish business facts from hypotheses; reference SQL can be wrong. "
                "Do not memorize complete question answers. Tools available at deployment: "
                "query_values and execute_sql, plus schema. Do not require unavailable tools."
            ),
        }, {"role": "user", "content": json.dumps({
            "schema": db["schema"], "current_skill": incumbent,
            "training": examples, "user_corrections": corrections,
        }, ensure_ascii=False)}])
        candidate = (message.get("content") or "").strip()
        self.optimizer_details = {"profile": "legacy", "valid": True}
        return candidate, usage

    def optimize(self, db_id):
        db = self.db(db_id)

        current = self.version(db)
        policy = EvolutionPolicy.model_validate(self.state["evolution_policy"])
        all_tasks = self.development_tasks(db)
        replay = select_replay([e for e in self.state["experiences"].values()
                                if e["db_id"] == db_id and e.get("reference_sql")],
                               policy.replay_size)
        keys = {e["key"] for e in replay}
        training_tasks = [t for t in all_tasks if question_key(t["question"], t.get("evidence", ""))
                          in keys] or all_tasks[:policy.replay_size]
        training_tasks = training_tasks[:policy.replay_size]
        context_experiences = select_replay([
            e for e in self.state["experiences"].values()
            if e["db_id"] == db_id and e["signal"] != "unverified"
        ], policy.replay_size)
        evidence_events = [e["event_id"] for e in context_experiences]
        if not training_tasks and not evidence_events:
            raise ValueError("需要开发案例或执行失败、已确认纠错等经验。")
        selection_tasks = [t for t in db["tasks"] if t["split"] == "selection"]
        training = self.evaluate(db, training_tasks, current["skill"])
        self.optimizer_details = {}
        candidate, usage = self.propose(db, training, current["skill"])
        document = self.optimizer_profile in MARKDOWN_PROFILES
        limit = MAX_SKILL_CHARS if document else 4000
        if document and self.optimizer_details.get("valid", True):
            try:
                self.optimizer_details["metadata"] = (
                    verify_document_update(
                        current["skill"], candidate, self.optimizer_details, self.edit_budget,
                    ) if self.optimizer_profile == PROFILE else parse_document(candidate)
                )
            except ValueError as exc:
                self.optimizer_details.update({"valid": False, "reason": str(exc)})
        valid_candidate = (bool(candidate) and len(candidate) <= limit
                           and candidate != current["skill"]
                           and self.optimizer_details.get("valid", True))
        db_stat = Path(db["path"]).stat()
        evaluation_context = fingerprint(json.dumps(serializable({
            "current_skill": current["skill"], "initial_skill": self.version(db, "v0")["skill"],
            "selection_tasks": selection_tasks, "model": self.model.info(),
            "database": [db["schema_hash"], db_stat.st_size, db_stat.st_mtime_ns],
            "business_context": db.get("context", ""), "limits": self.limits.model_dump(),
            "min_net_improvements": policy.min_net_improvements,
        }), sort_keys=True))
        duplicate = next((v for v in db["versions"]
                          if valid_candidate and v["skill"] == candidate
                          and v.get("evaluation_context") == evaluation_context
                          and v["status"] in {"pending", "rejected"}
                          and all((v.get("selection") or {}).get(key) is not None
                                  for key in ("baseline", "current", "candidate"))), None)
        needs_evaluation = valid_candidate and duplicate is None
        artifact = {"id": f"v{len(db['versions'])}", "skill": candidate}
        if candidate and self.optimizer_details.get("valid", True):
            self.skills.persist(db["branch_id"], artifact)
            candidate = self.skills.load(artifact)
        candidate_training = (
            self.evaluate(db, training_tasks, candidate) if document and needs_evaluation else []
        )
        initial = self.version(db, "v0")
        baseline = self.evaluate(db, selection_tasks, initial["skill"]) if needs_evaluation else []
        previous = []
        if needs_evaluation:
            previous = (baseline if current["skill"] == initial["skill"] else
                        self.evaluate(db, selection_tasks, current["skill"]))
        proposed = self.evaluate(db, selection_tasks, candidate) if needs_evaluation else []
        if duplicate:
            rows = duplicate["selection"]["rows"]
            baseline, previous, proposed = rows["baseline"], rows["current"], rows["candidate"]
        complete = bool(selection_tasks) and all(
            r["score"] is not None for rows in (baseline, previous, proposed) for r in rows
        ) and valid_candidate
        means = {name: self.accuracy(rows) for name, rows in (
            ("baseline", baseline), ("current", previous), ("candidate", proposed)
        )}
        improved = sum(a["score"] == 0 and b["score"] == 1
                       for a, b in zip(previous, proposed, strict=True))
        regressed = sum(a["score"] == 1 and b["score"] == 0
                        for a, b in zip(previous, proposed, strict=True))
        accepted = (needs_evaluation and complete and means["candidate"] >= means["baseline"]
                    and improved - regressed >= policy.min_net_improvements)
        status = "accepted" if accepted else "pending"
        decision = "accepted"
        reason = "验证集净改善达到门槛，且达到初始 Skill 成绩。"
        if not valid_candidate:
            status = "rejected"
            decision = "invalid_candidate"
            if candidate == current["skill"] and self.optimizer_details.get("valid", True):
                decision = "unchanged"
            reason = self.optimizer_details.get("reason") or (
                "候选与当前 Skill 相同，未更新" if candidate == current["skill"]
                else f"候选无效、为空或超过 {limit} 字。")
        elif duplicate:
            status, decision = duplicate["status"], "duplicate_candidate"
            reason = f"与 {duplicate['id']} 文档相同，复用已有验证结论，跳过评测。"
        elif not complete:
            decision = "insufficient_validation"
            reason = "独立验证案例不足或评分未知，保留候选与经验。"
        elif means["candidate"] < means["current"] or means["candidate"] < means["baseline"]:
            decision = "regression"
            status, reason = "rejected", "验证出现净退化，保留改善与退化证据。"
        elif not accepted:
            decision = "no_gain"
            reason = "本次净改善不足，保留候选与经验，等待修订或新证据。"
        version = {
            **artifact, "parent": current["id"],
            "skill_format": "markdown" if document else "legacy_text",
            "created": time.time(), "status": status, "branch_id": db["branch_id"],
            "mode": self.model.info()["mode"], "model": self.model.info(),
            "diff": "\n".join(difflib.unified_diff(
                current["skill"].splitlines(), candidate.splitlines(),
                fromfile=f"{current['id']}/SKILL.md", tofile=f"{artifact['id']}/SKILL.md",
                lineterm="",
            )),
            "evaluation_context": evaluation_context,
            "reused_version": duplicate["id"] if duplicate else None,
            "selection": {**means, "count": len(selection_tasks), "decision": decision,
                          "reason": reason, "improved": improved, "regressed": regressed,
                          "min_net_improvements": policy.min_net_improvements,
                          "rows": {"baseline": baseline, "current": previous,
                                   "candidate": proposed}},
            "training": training, "candidate_training": candidate_training,
            "optimizer_usage": usage,
            "optimizer": self.optimizer_details or {"profile": self.optimizer_profile},
        }
        db["versions"].append(version)
        if accepted:
            self.set_active(db, version["id"])
        outcomes = {question_key(r["question"]): r for r in candidate_training}
        for exp in self.state["experiences"].values():
            if exp["db_id"] != db_id or exp["event_id"] not in evidence_events:
                continue
            outcome = outcomes.get(question_key(exp["question"]))
            exp["attempts"].append({"branch_id": db["branch_id"], "version": version["id"],
                                    "decision": status,
                                    "score": outcome.get("score") if outcome else None})
            if accepted and outcome:
                exp["resolved"] = outcome["score"] == 1
        processed = self.state["processed_events"].setdefault(db["branch_id"], [])
        processed.extend(event for event in evidence_events if event not in processed)
        version["evidence_events"] = evidence_events
        self.save()
        return version

    def evolve(self, db_id, rounds=3):
        if type(rounds) is not int or not 1 <= rounds <= 10:
            raise ValueError("进化轮数须为 1–10。")
        db = self.db(db_id)

        versions = []
        for _ in range(rounds):
            incumbent = self.version(db)["skill"]
            version = self.optimize(db_id)
            versions.append(version)
            if version["skill"] == incumbent and version["optimizer"].get("valid", True):
                break
        return {"versions": versions, "active": db["active"]}

    @staticmethod
    def accuracy(rows):
        return (sum(row["score"] for row in rows) / len(rows)
                if rows and all(row["score"] is not None for row in rows) else None)

    def rollback(self, db_id, version_id):
        db = self.db(db_id)
        self.require_idle(db_id)
        version = next((v for v in db["versions"] if v["id"] == version_id), None)
        if not version or version["status"] not in ("accepted", "baseline"):
            raise ValueError("只能回退到已接受版本或 v0。")
        self.skills.load(version)
        self.set_active(db, version_id)
        for exp in self.state["experiences"].values():
            if exp["db_id"] == db_id:
                exp["resolved"] = False
        self.save()
        return {"active": version_id}

    def compare(self, db_id):
        with self.report_lock:
            return self.compare_snapshot(db_id)

    def evaluate_stream(self, db, tasks, skill):
        """Yield results individually; batch evaluators can use their persisted observations."""
        for task in tasks:
            yield self.evaluate(db, [task], skill)[0]

    def compare_snapshot(self, db_id):
        with self.lock:
            db = copy.deepcopy(self.db(db_id))
            previous = next((r for r in self.state["reports"] if r["db_id"] == db_id), None)
            if previous:
                return previous
            tasks = [t for t in db["tasks"] if t["split"] == "holdout"]
            if not tasks:
                raise ValueError("请先导入独立的 holdout 案例。")
            frozen_path = self.root / f"frozen-{db_id}.json"
            if frozen_path.exists():
                frozen = json.loads(frozen_path.read_text())
                if frozen["model"] != self.model.info():
                    raise ValueError("评测快照的模型已改变，请恢复配置后继续。")
                tasks = frozen.get("tasks", tasks)
            else:
                active = self.version(db)
                frozen = {"version": active["id"], "skill": active["skill"],
                          "branch_id": db["branch_id"],
                          "initial_skill": self.version(db, "v0")["skill"],
                          "dataset_hash": db["dataset_hash"], "model": self.model.info(),
                          "tasks": copy.deepcopy(tasks)}
                frozen_path.write_text(json.dumps(frozen, ensure_ascii=False, indent=2))
        initial_skill = frozen.get("initial_skill", "")
        cache = self.root / "evaluations" / fingerprint(json.dumps(frozen, sort_keys=True))
        cache.mkdir(parents=True, exist_ok=True)

        def observed(skill):
            paths = {task["id"]: cache / (fingerprint(task["id"] + fingerprint(skill)) + ".json")
                     for task in tasks}
            rows = {task_id: json.loads(path.read_text()) for task_id, path in paths.items()
                    if path.exists()}
            pending = [task for task in tasks if task["id"] not in rows
                       or rows[task["id"]]["score"] is None]
            for result in self.evaluate_stream(db, pending, skill):
                paths[result["task_id"]].write_text(
                    json.dumps(serializable(result), ensure_ascii=False))
                rows[result["task_id"]] = result
            return [rows[task["id"]] for task in tasks]

        no_skill = observed("")
        baseline = observed(initial_skill)
        optimized = observed(frozen["skill"])
        b0, final = self.accuracy(baseline), self.accuracy(optimized)
        known_pairs = [(a["score"], b["score"]) for a, b in zip(baseline, optimized, strict=True)
                       if a["score"] is not None and b["score"] is not None]
        report = {
            "id": identifier(), "db_id": db_id, "version": frozen["version"],
            "branch_id": frozen.get("branch_id"),
            "created": time.time(), "model": self.model.info(),
            "kind": "synthetic_demo" if not self.model.live else "fixed_database_evaluation",
            "dataset_source": db["source"], "dataset_hash": frozen["dataset_hash"],
            "skill": frozen["skill"], "skill_hash": fingerprint(frozen["skill"]),
            "skill_sha256": fingerprint(frozen["skill"]),
            "initial_skill": initial_skill, "initial_skill_hash": fingerprint(initial_skill),
            "optimizer_profile": self.optimizer_profile,
            "baseline": b0, "optimized": final, "no_skill": self.accuracy(no_skill),
            "gain": final - b0 if b0 is not None and final is not None else None,
            "count": len(tasks), "known_pairs": len(known_pairs),
            "improved": sum(a == 0 and b == 1 for a, b in known_pairs),
            "regressed": sum(a == 1 and b == 0 for a, b in known_pairs),
            "rows": {"baseline": baseline, "optimized": optimized, "no_skill": no_skill},
            "costs": {name: self.rollout_cost(rows) for name, rows in (
                ("no_skill", no_skill), ("initial", baseline), ("evolved", optimized))},
            "scorer": "BIRD EX (set equality; ignores row order and duplicates)",
            "shared_observations": initial_skill == frozen["skill"],
            "metadata": db["metadata"],
        }
        with self.lock:
            self.state["reports"].append(report)
            self.save()
            (self.root / f"report-{db_id}.json").write_text(
                json.dumps(serializable(report), ensure_ascii=False, indent=2))
        return report

    @staticmethod
    def rollout_cost(rows):
        return {
            "model_calls": sum(r.get("model_calls", 0) for r in rows),
            "tool_calls": sum(e.get("kind") == "tool" for r in rows for e in r.get("trace", [])),
            "tokens": sum(r["tokens"] for r in rows)
            if all(r.get("tokens") is not None for r in rows) else None,
            "elapsed_ms": sum(r.get("elapsed_ms", 0) for r in rows),
        }
