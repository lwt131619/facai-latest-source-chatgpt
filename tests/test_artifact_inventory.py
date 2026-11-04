"""Artifact storage baseline must preserve owner boundaries and evidence."""

from facai.artifact_inventory import inventory
from facai.store import Store


def test_inventory_counts_exact_duplicates_only_within_owner(tmp_path):
    store = Store(tmp_path)
    a = store.create("one", "same", ["work"], owner_id="alice")
    b = store.create("two", "same", ["work"], owner_id="alice")
    c = store.create("three", "same", ["work"], owner_id="bob")
    files = []
    for mission in (a, b, c):
        file = tmp_path / "workspaces" / mission["id"] / "result.txt"
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(b"identical result")
        store.add_artifact(mission["id"], "result.txt", file)
        files.append(file)
    report = inventory(tmp_path)
    assert report["artifact_references"] == 3
    assert report["duplicate_references_same_owner"] == 1
    assert report["logical_duplicate_bytes_same_owner"] == len(b"identical result")
    assert report["mode"] == "READ_ONLY" and not report["deduplicated"]
    assert all(file.read_bytes() == b"identical result" for file in files)


def test_inventory_reports_missing_and_rejects_outside_path(tmp_path):
    store = Store(tmp_path / "data")
    mission = store.create("one", "one", ["work"])
    file = tmp_path / "data" / "workspaces" / mission["id"] / "gone.txt"
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text("soon gone")
    store.add_artifact(mission["id"], file.name, file)
    file.unlink()
    report = inventory(tmp_path / "data")
    assert report["missing_files"] == 1
    assert report["readable_bytes"] == 0
