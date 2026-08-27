from __future__ import annotations

from pathlib import Path

import json

from instruments.conflicts.verify_determinism import (
    file_size_sum,
    miner_files,
    refresh_disk_measurement,
)


def test_miner_files_are_the_complete_per_repo_artifact_set(tmp_path: Path) -> None:
    assert [path.relative_to(tmp_path).as_posix() for path in miner_files(tmp_path, "o__r")] == [
        "o__r.jsonl",
        "_all_merges/o__r.jsonl",
        "_summaries/o__r.json",
    ]


def test_file_size_sum_uses_logical_file_lengths(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "a").write_bytes(b"123")
    (tmp_path / "sub" / "b").write_bytes(b"45678")
    assert file_size_sum(tmp_path) == 8
    assert file_size_sum(tmp_path, exclude=tmp_path / "a") == 5


def test_refresh_disk_measurement_preserves_verification_result(tmp_path: Path) -> None:
    mirrors = tmp_path / "mirrors"
    corpus = tmp_path / "corpus"
    mirrors.mkdir()
    corpus.mkdir()
    (mirrors / "objects").write_bytes(b"12345")
    (corpus / "rows.jsonl").write_bytes(b"123")
    report_path = corpus / "DETERMINISM.json"
    report_path.write_text(
        '{"all_byte_identical":true,"repositories":[{"slug":"o__r"}]}\n',
        encoding="utf-8",
    )

    refreshed = refresh_disk_measurement(report_path, mirrors, corpus)

    assert refreshed["all_byte_identical"] is True
    assert refreshed["repositories"] == [{"slug": "o__r"}]
    assert refreshed["mirror_logical_bytes"] == 5
    assert refreshed["corpus_output_logical_bytes"] == 3
    assert refreshed["total_disk_bytes"] == 8
    assert json.loads(report_path.read_text(encoding="utf-8")) == refreshed
