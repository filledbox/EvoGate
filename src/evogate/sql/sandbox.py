"""Read-only SQLite queries in disposable spawn processes."""

import multiprocessing as mp
import pickle
import sqlite3
import sys
import time
from pathlib import Path

import sqlglot
from sqlglot import exp

from .contracts import Execution, Limits


def validate_sql(sql: str) -> None:
    try:
        statements = sqlglot.parse(sql, read="sqlite")
    except sqlglot.errors.SqlglotError as exc:
        raise ValueError("invalid SQL") from exc
    if len(statements) != 1 or not isinstance(statements[0], exp.Query):
        raise ValueError("one query required")
    forbidden = (
        exp.Insert,
        exp.Update,
        exp.Delete,
        exp.Create,
        exp.Drop,
        exp.Command,
        exp.Alter,
        exp.Into,
        exp.Transaction,
    )
    for node in statements[0].walk():
        if isinstance(node, forbidden):
            raise ValueError("unsafe query")
        if isinstance(node, exp.Table) and node.name.lower().startswith("pragma_"):
            raise ValueError("table-valued PRAGMA forbidden")
        if isinstance(node, exp.Anonymous) and node.name.lower().startswith("pragma_"):
            raise ValueError("table-valued PRAGMA forbidden")
        if isinstance(node, exp.Func) and node.sql_name().lower() in {
            "load_extension",
            "readfile",
            "writefile",
        }:
            raise ValueError("unsafe function")


def _worker(conn, database: str, sql: str, limits: dict):
    applied = {"readonly": True, "authorizer": True, "address_space": False, "cpu": False}
    try:
        import resource

        cfg = Limits.model_validate(limits)
        resource.setrlimit(resource.RLIMIT_CPU, (cfg.cpu_seconds, cfg.cpu_seconds))
        applied["cpu"] = True
        if sys.platform.startswith("linux"):
            resource.setrlimit(resource.RLIMIT_AS, (cfg.memory_bytes, cfg.memory_bytes))
            applied["address_space"] = True
        db = sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True)
        db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, cfg.max_bytes)
        db.enable_load_extension(False)
        db.execute("PRAGMA query_only=ON")
        allowed = {
            sqlite3.SQLITE_SELECT,
            sqlite3.SQLITE_READ,
            sqlite3.SQLITE_FUNCTION,
            sqlite3.SQLITE_RECURSIVE,
        }

        def authorize(action, arg1, arg2, *_):
            if action not in allowed:
                return sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() in {
                "load_extension",
                "readfile",
                "writefile",
                "eval",
            }:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        db.set_authorizer(authorize)
        deadline = time.monotonic() + cfg.wall_seconds
        db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        cursor = db.execute(sql)
        rows, size = [], 0
        for row in cursor:
            size += len(pickle.dumps(row))
            if len(rows) >= cfg.max_rows or size > cfg.max_bytes:
                detail = "row_limit" if len(rows) >= cfg.max_rows else "result_bytes_limit"
                conn.send(Execution(status="resource_limit", detail=detail,
                                    restrictions=applied).model_dump())
                return
            rows.append(row)
        conn.send(Execution(status="success", rows=rows, restrictions=applied).model_dump())
    except sqlite3.DatabaseError as exc:
        status = "timeout" if str(exc) == "interrupted" else "sql_error"
        if "too big" in str(exc):
            conn.send(Execution(status="resource_limit", detail="sqlite_value_limit",
                                restrictions=applied).model_dump())
            return
        if "unable to open" in str(exc):
            status = "infrastructure_error"
        conn.send(Execution(status=status, detail=str(exc), restrictions=applied).model_dump())
    except MemoryError:
        conn.send(Execution(status="resource_limit", restrictions=applied).model_dump())
    except Exception as exc:
        conn.send(
            Execution(
                status="infrastructure_error", detail=type(exc).__name__, restrictions=applied
            ).model_dump()
        )
    finally:
        conn.close()


def execute(database: Path, sql: str, limits: Limits | None = None) -> Execution:
    limits = limits or Limits()
    try:
        validate_sql(sql)
    except ValueError as exc:
        return Execution(status="rejected", detail=str(exc))
    if any(Path(str(database) + suffix).exists() for suffix in ("-wal", "-journal")):
        return Execution(status="infrastructure_error", detail="database has mutable sidecar")
    if not database.is_file():
        return Execution(status="infrastructure_error", detail="database missing")
    ctx = mp.get_context("spawn")
    parent, child = ctx.Pipe(duplex=False)
    process = ctx.Process(target=_worker, args=(child, str(database), sql, limits.model_dump()))
    process.start()
    child.close()
    try:
        if parent.poll(limits.wall_seconds):
            try:
                return Execution.model_validate(parent.recv())
            except EOFError:
                return Execution(status="resource_limit", detail="worker terminated")
        return Execution(status="timeout", detail="parent deadline")
    finally:
        if process.is_alive():
            process.terminate()
        process.join(2)
        if process.is_alive():
            process.kill()
            process.join()
        parent.close()
