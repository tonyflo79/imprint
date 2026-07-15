from __future__ import annotations

import json

from imprint.cli import main


def _config(tmp_path, name):
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps({"operator_slug": name, "data_root": str(tmp_path / name)}))
    return path


def test_ingest_export_import_and_migrate_cli(tmp_path, capsys):
    source_config = _config(tmp_path, "source")
    candidate = tmp_path / "candidate.json"
    candidate.write_text(json.dumps({
        "source_kind": "memory_export",
        "source_locator": "synthetic://cli/1",
        "content": "Imported context is not captured judgment.",
        "metadata": {"fixture": True},
        "extensions": {"org.example.cli": {"schema_version": "1.0.0", "payload": {"x": 1}}},
    }))
    assert main(["--config", str(source_config), "ingest", "scan", "--input", str(candidate)]) == 0
    item_id = json.loads(capsys.readouterr().out)["items"][0]["item_id"]
    assert main(["--config", str(source_config), "ingest", "keep", item_id, "--why", "useful context only"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "kept"
    assert main(["--config", str(source_config), "ingest", "status"]) == 0
    assert json.loads(capsys.readouterr().out)["counts"] == {"unruled": 0, "kept": 1, "killed": 0}

    exported = tmp_path / "imprint.jsonld"
    assert main(["--config", str(source_config), "export", "--format", "jsonld", "--output", str(exported)]) == 0
    capsys.readouterr()
    document = json.loads(exported.read_text())
    assert document["imprint:ledger"]["ingest_rulings"][0]["why"] == "useful context only"

    target_config = _config(tmp_path, "target")
    assert main(["--config", str(target_config), "import", "--format", "jsonld", "--input", str(exported), "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "validated"
    assert main(["--config", str(target_config), "import", "--format", "jsonld", "--input", str(exported)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "imported"

    spec = tmp_path / "migration.json"
    assert main(["--config", str(target_config), "backup", "create"]) == 0
    backup_path = json.loads(capsys.readouterr().out)["path"]
    spec.write_text(json.dumps({
        "migration_id": "3.0.0-cli-labels",
        "from_version": "3.0.0",
        "to_version": "3.0.1",
        "statements": ["CREATE TABLE IF NOT EXISTS cli_labels (id TEXT PRIMARY KEY)"],
        "backup_receipt": backup_path,
    }))
    assert main(["--config", str(target_config), "migrate", "plan", "--spec", str(spec)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "applicable"
    assert main(["--config", str(target_config), "migrate", "apply", "--spec", str(spec)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "applied"
    assert main(["--config", str(target_config), "migrate", "verify"]) == 0
    verified = json.loads(capsys.readouterr().out)
    assert verified["store_schema_version"] == "3.0.1"
    assert verified["migrations"][0]["migration_id"] == "3.0.0-cli-labels"
