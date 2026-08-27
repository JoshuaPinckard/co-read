from __future__ import annotations

import pytest

from instruments.conflicts.miner import MiningError
from instruments.conflicts.recompute_overlap_outputs import row_stages


def test_row_stages_flattens_conflict_entries_in_path_order() -> None:
    row = {
        "conflicts": [
            {
                "path": "z.txt",
                "stage_entries": [
                    {"path": "z.txt", "stage": 1, "blob": "a", "mode": "100644"}
                ],
            },
            {
                "path": "a.txt",
                "stage_entries": [
                    {"path": "a.txt", "stage": 2, "blob": "b", "mode": "100644"}
                ],
            },
        ]
    }
    paths, stages = row_stages(row)
    assert paths == ["a.txt", "z.txt"]
    assert [entry["path"] for entry in stages] == ["z.txt", "a.txt"]


def test_row_stages_rejects_missing_stage_audit() -> None:
    with pytest.raises(MiningError, match="stage_entries"):
        row_stages({"conflicts": [{"path": "x", "stage_entries": None}]})
