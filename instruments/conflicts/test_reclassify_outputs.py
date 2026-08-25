from __future__ import annotations

from instruments.conflicts.reclassify_outputs import migrate_row, revised_classification


def test_underscore_vendor_is_vendored() -> None:
    conflict = {
        "path": "_vendor/example/project/source.go",
        "classification": {"kind": "handwritten", "rule": "operational-default"},
    }
    assert revised_classification(conflict) == {
        "kind": "vendored",
        "rule": "vendored-segment:_vendor",
    }


def test_underscore_gen_is_generated() -> None:
    conflict = {
        "path": "docs/resources/_gen/images/example.png",
        "classification": {"kind": "handwritten", "rule": "operational-default"},
    }
    assert revised_classification(conflict) == {
        "kind": "generated",
        "rule": "generated-segment:_gen",
    }


def test_historical_generated_header_evidence_is_preserved() -> None:
    conflict = {
        "path": "src/plain.go",
        "classification": {
            "kind": "generated",
            "rule": "generated-header-first-8192-bytes",
            "evidence_blob": "a" * 40,
        },
    }
    assert revised_classification(conflict) == conflict["classification"]


def test_migrate_row_changes_only_classification() -> None:
    row = {
        "merge": "a" * 40,
        "conflicts": [
            {
                "path": "_vendor/x.c",
                "regions": [{"byte_start": 1, "byte_end": 2}],
                "classification": {
                    "kind": "handwritten",
                    "rule": "operational-default",
                },
            }
        ],
    }
    migrated, changed = migrate_row(row)
    assert changed == 1
    assert migrated["merge"] == row["merge"]
    assert migrated["conflicts"][0]["regions"] == row["conflicts"][0]["regions"]
    assert migrated["conflicts"][0]["classification"]["kind"] == "vendored"
