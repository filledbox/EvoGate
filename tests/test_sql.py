import hashlib
import sqlite3

import pytest

from evogate.sql.contracts import Execution, Limits
from evogate.sql.sandbox import execute
from evogate.sql.scorer import evaluate_result


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "db.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript("CREATE TABLE t(x); INSERT INTO t VALUES (1),(1),(NULL),(2);")
    return path


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM t",
        "SELECT 1; SELECT 2",
        "ATTACH 'a' AS a",
        "PRAGMA user_version=1",
        "SELECT load_extension('a')",
        "SELECT * FROM pragma_table_info('t')",
        "/* comment */ UPDATE t SET x=0",
        "WITH a AS (SELECT 1) DELETE FROM t",
        "SELECT writefile('/tmp/no', 'x')",
    ],
)
def test_unsafe(database, sql):
    before = file_hash(database)
    assert execute(database, sql).status in {"rejected", "sql_error"}
    assert file_hash(database) == before


@pytest.mark.parametrize(
    "sql",
    [
        "WITH a AS (SELECT x FROM t) SELECT * FROM a",
        "SELECT * FROM (SELECT x FROM t)",
        "SELECT 1 UNION SELECT 2",
        "SELECT x FROM t -- comment",
    ],
)
def test_queries(database, sql):
    result = execute(database, sql)
    assert result.status == "success"
    assert result.restrictions["readonly"]


def test_limits(database):
    limited = execute(database, "SELECT * FROM t", Limits(max_rows=2))
    assert limited.status == "resource_limit" and limited.detail == "row_limit"
    assert evaluate_result(limited, Execution(status="success", rows=[(1,)])).score == 0
    assert (
        execute(database, "SELECT zeroblob(1000)", Limits(max_bytes=100)).status == "resource_limit"
    )
    sql = "WITH RECURSIVE x(a) AS (SELECT 1 UNION ALL SELECT a+1 FROM x) SELECT sum(a) FROM x"
    assert execute(database, sql, Limits(wall_seconds=0.2)).status == "timeout"
    assert execute(database.parent / "missing", "SELECT 1").status == "infrastructure_error"


@pytest.mark.parametrize(
    "pred,ref,score",
    [
        ([(1,), (1,)], [(1,)], 1),
        ([(2,), (1,)], [(1,), (2,)], 1),
        ([(None,)], [(None,)], 1),
        ([(0.1,)], [(0.10000001,)], 0),
        ([], [], 1),
        ([(1,)], [(2,)], 0),
    ],
)
def test_score(pred, ref, score):
    value = evaluate_result(
        Execution(status="success", rows=pred), Execution(status="success", rows=ref)
    )
    assert value.score == score
    assert value.reason == "bird_ex"
    assert set(value.model_dump()) == {"schema_version", "score", "reason"}


@pytest.mark.parametrize(
    "status,expected",
    [
        ("rejected", 0),
        ("sql_error", 0),
        ("timeout", 0),
        ("resource_limit", None),
        ("infrastructure_error", None),
    ],
)
def test_score_failure(status, expected):
    assert evaluate_result(Execution(status=status), Execution(status="success")).score == expected
    assert evaluate_result(Execution(status="success"), Execution(status=status)).score is None


@pytest.mark.parametrize("detail", ["row_limit", "result_bytes_limit", "sqlite_value_limit"])
def test_known_output_limits_count_as_failure_only_for_predictions(detail):
    limited = Execution(status="resource_limit", detail=detail)
    assert evaluate_result(limited, Execution(status="success")).score == 0
    assert evaluate_result(Execution(status="success"), limited).score is None


def test_unexplained_worker_exit_stays_unknown():
    assert evaluate_result(Execution(status="resource_limit", detail="worker terminated"),
                           Execution(status="success")).score is None


def test_official_scorer_is_unmodified():
    import hashlib
    import inspect

    from evogate._vendor.bird_ex import calculate_ex
    from evogate.sql import scorer

    assert scorer.calculate_ex is calculate_ex
    assert (
        hashlib.sha256(inspect.getsource(calculate_ex).encode()).hexdigest()
        == (scorer.SCORER["function_sha256"])
    )
