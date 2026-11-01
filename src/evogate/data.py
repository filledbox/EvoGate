"""Demo data and database-local, template-grouped BIRD imports."""

import hashlib
import json
import random
import re
import sqlite3
from pathlib import Path

import sqlglot
from sqlglot import exp

from evogate.sql import schema


def fingerprint(value):
    return hashlib.sha256(value.encode()).hexdigest()


def demo(root):
    path = root / "sales.sqlite"
    if not path.exists():
        with sqlite3.connect(path) as conn:
            conn.executescript("""
                CREATE TABLE orders(id INTEGER PRIMARY KEY, region TEXT, status TEXT);
                CREATE TABLE items(id INTEGER PRIMARY KEY, order_id INTEGER,
                    amount REAL, FOREIGN KEY(order_id) REFERENCES orders(id));
                INSERT INTO orders VALUES
                    (1,'华东','paid'),(2,'华东','refunded'),(3,'华南','paid'),
                    (4,'华北','paid'),(5,'华东','paid'),(6,'华南','refunded');
                INSERT INTO items VALUES
                    (1,1,100),(2,1,50),(3,2,80),(4,3,30),(5,3,70),
                    (6,4,90),(7,5,40),(8,5,60),(9,6,20);
            """)
    tasks = []
    # Distinct inputs, shared scripted mechanism: a UI fixture, not model evidence.
    groups = {
        "training": [("华东", "订单数"), ("华南", "订单数"), ("华北", "金额")],
        "selection": [("华东", "订单数量"), ("华南", "订单数量"), ("华东", "金额")],
        "holdout": [("华东", "有几笔订单"), ("华南", "有几笔订单"), ("华南", "金额")],
    }
    for split, entries in groups.items():
        for index, (region, kind) in enumerate(entries):
            count = kind != "金额"
            expression = "COUNT(DISTINCT o.id)" if count else "SUM(i.amount)"
            reference = (
                f"SELECT {expression} FROM orders o JOIN items i ON i.order_id=o.id "
                f"WHERE o.region='{region}'"
            )
            tasks.append({
                "id": f"demo:{split}:{index}", "split": split,
                "question": f"请统计{region}地区的{kind}（包含全部订单状态）。",
                "evidence": "订单主表 orders；商品明细 items；一笔订单可有多条明细。",
                "reference_sql": reference, "demo_region": region, "demo_count": count,
            })
    return database("sales-demo", "销售演示库", path, tasks, "synthetic", {})


def database(db_id, label, path, tasks, source, metadata):
    schema_text = schema(path)
    return {
        "id": db_id, "label": label, "path": str(path.resolve()), "source": source,
        "schema": schema_text, "schema_hash": fingerprint(schema_text),
        "tasks": tasks, "metadata": metadata,
        "dataset_hash": fingerprint(json.dumps(tasks, sort_keys=True, ensure_ascii=False)),
    }


def sql_template(sql):
    tree = sqlglot.parse_one(sql, read="sqlite")
    return tree.transform(
        lambda node: exp.Placeholder() if isinstance(node, exp.Literal) else node
    ).sql(dialect="sqlite")


def import_bird(source, db_root, db_ids, per_split=10, seed=42, excluded_ids=None):
    if per_split < 1:
        raise ValueError("per-split 必须大于零。")
    content = source.read_text()
    rows = (
        [json.loads(line) for line in content.splitlines() if line.strip()]
        if source.suffix == ".jsonl" else json.loads(content)
    )
    result = []
    excluded_ids = set(excluded_ids or ())
    for db_id in db_ids:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", db_id):
            raise ValueError("数据库 ID 格式无效。")
        path = db_root / db_id / (db_id + ".sqlite")
        if not path.is_file():
            raise ValueError(f"数据库不存在：{path}")
        groups, seen_questions = {}, set()
        for index, row in enumerate(rows):
            if row["db_id"] != db_id:
                continue
            if f"bird:{db_id}:{index}" in excluded_ids:
                continue
            question_key = " ".join(row["question"].lower().split())
            if question_key in seen_questions:
                continue
            try:
                template = sql_template(row["SQL"])
            except sqlglot.errors.SqlglotError:
                continue
            seen_questions.add(question_key)
            groups.setdefault(template, []).append({
                "id": f"bird:{db_id}:{index}", "question": row["question"],
                "evidence": row.get("evidence", ""), "reference_sql": row["SQL"],
            })
        templates = sorted(groups)
        random.Random(seed).shuffle(templates)
        splits = {name: [] for name in ("training", "selection", "holdout")}
        for template in templates:
            split = min(splits, key=lambda name: len(splits[name]))
            if len(splits[split]) >= per_split:
                break
            # Every SQL template belongs to just one split; excess variants are unused.
            for task in groups[template][:per_split - len(splits[split])]:
                splits[split].append({**task, "split": split})
        if any(len(tasks) < per_split for tasks in splits.values()):
            raise ValueError(f"{db_id} 独立模板不足；请调低 --per-split。")
        result.append(database(
            db_id, db_id, path, [task for tasks in splits.values() for task in tasks],
            "BIRD", {
                "source_file": str(source.resolve()), "source_sha256": fingerprint(content),
                "seed": seed, "grouping": "literal-normalized SQL templates",
                "prior_exposure": "未核查历史使用；本导入不宣称全新未暴露测试集",
                "license": "BIRD data: CC BY-SA 4.0; https://bird-bench.github.io/",
            },
        ))
    return result


def import_manifest(source, db_root, manifest_path):
    """Restore a published task split against the exact external source file."""
    manifest = json.loads(manifest_path.read_text())
    if hashlib.sha256(source.read_bytes()).hexdigest() != manifest["source_sha256"]:
        raise ValueError("数据源 SHA256 与任务清单不一致。")
    db_id = manifest["database"]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", db_id):
        raise ValueError("数据库 ID 格式无效。")
    path = Path(db_root) / db_id / (db_id + ".sqlite")
    if fingerprint(schema(path)) != manifest["schema_sha256"]:
        raise ValueError("数据库 schema 与任务清单不一致。")
    content = source.read_text()
    rows = ([json.loads(line) for line in content.splitlines() if line.strip()]
            if source.suffix == ".jsonl" else json.loads(content))
    tasks, identities, templates, questions = [], set(), {}, set()
    for entry in manifest["tasks"]:
        task_id, split = entry["id"], entry["split"]
        if (not re.fullmatch(rf"bird:{re.escape(db_id)}:[0-9]+", task_id)
                or task_id in identities or split not in {"training", "selection", "holdout"}):
            raise ValueError("任务清单含无效 ID、重复任务或无效划分。")
        index = int(task_id.rsplit(":", 1)[1])
        if index >= len(rows) or rows[index]["db_id"] != db_id:
            raise ValueError("任务 ID 与数据源不一致。")
        row = rows[index]
        template = sql_template(row["SQL"])
        question = " ".join(row["question"].lower().split())
        if templates.setdefault(template, split) != split or question in questions:
            raise ValueError("任务清单存在跨划分 SQL 模板或重复问题。")
        identities.add(task_id)
        questions.add(question)
        tasks.append({"id": task_id, "split": split, "question": row["question"],
                      "evidence": row.get("evidence", ""), "reference_sql": row["SQL"]})
    if {t["split"] for t in tasks} != {"training", "selection", "holdout"}:
        raise ValueError("任务清单需要开发、选择和测试三个非空划分。")
    db = database(db_id, db_id, path, tasks, "BIRD", {
        "source_sha256": manifest["source_sha256"], "grouping": "published task manifest",
        "prior_exposure": "任务清单固定划分；历史使用范围见随附评测说明。",
        "license": "BIRD data: CC BY-SA 4.0; https://bird-bench.github.io/",
    })
    if db["dataset_hash"] != manifest["dataset_hash"]:
        raise ValueError("还原后的任务内容与清单哈希不一致。")
    return [db]
