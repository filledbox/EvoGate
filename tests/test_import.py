import hashlib
import json
from pathlib import Path

import pytest

from evogate.data import import_bird, import_manifest
from evogate.evaluation import prepare
from evogate.model import Model
from evogate.service import Service


@pytest.fixture
def bird_files(tmp_path):
    demo = Service(tmp_path / "demo").db("sales-demo")
    db_root = tmp_path / "databases"
    database = db_root / "tiny" / "tiny.sqlite"
    database.parent.mkdir(parents=True)
    database.write_bytes(Path(demo["path"]).read_bytes())
    source = tmp_path / "train.json"
    source.write_text(json.dumps([
        {"db_id": "tiny", "question": "How many orders?", "SQL": "SELECT COUNT(*) FROM orders"},
        {"db_id": "tiny", "question": "Which regions?", "SQL": "SELECT region FROM orders"},
        {"db_id": "tiny", "question": "Total amount?", "SQL": "SELECT SUM(amount) FROM items"},
    ]))
    return source, db_root


def test_published_manifest_restores_exact_tasks_and_rejects_changed_source(tmp_path, bird_files):
    source, db_root = bird_files
    original = import_bird(source, db_root, ["tiny"], per_split=1)[0]
    manifest = tmp_path / "splits.json"
    manifest.write_text(json.dumps({
        "database": "tiny", "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "schema_sha256": original["schema_hash"], "dataset_hash": original["dataset_hash"],
        "tasks": [{"id": t["id"], "split": t["split"]} for t in original["tasks"]],
    }))
    restored = import_manifest(source, db_root, manifest)[0]
    assert restored["tasks"] == original["tasks"]
    source.write_text(source.read_text() + "\n")
    with pytest.raises(ValueError, match="SHA256"):
        import_manifest(source, db_root, manifest)


def test_batch_prepare_uses_given_database_without_model_calls(tmp_path, bird_files, monkeypatch):
    source, db_root = bird_files
    monkeypatch.setattr(Model, "complete", lambda *a, **kw: pytest.fail("no paid calls in prepare"))
    workspace = tmp_path / "evaluation"
    prepare(workspace, 2, source, db_root, ["tiny"], per_split=1, max_calls=20)
    protocol = json.loads((workspace / "round-protocol.json").read_text())
    assert protocol["databases"] == ["tiny"]
    assert protocol["split_counts"]["tiny"] == {"training": 1, "selection": 1, "holdout": 1}
    assert protocol["max_calls"] == 20
    assert not (workspace / "requests").exists()
    assert not json.loads((workspace / "preflight.json").read_text())["invalid"]
    with pytest.raises(ValueError, match="已准备完成"):
        prepare(workspace, 2, source, db_root, ["tiny"])
