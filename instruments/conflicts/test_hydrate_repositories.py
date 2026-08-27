from __future__ import annotations

import json
from pathlib import Path

import pytest

from instruments.conflicts import hydrate_repositories as hydrate


def test_parse_first_parent_history_selects_exactly_two_parents() -> None:
    root = b"1" * 40
    ordinary = b"2" * 40
    merge = b"3" * 40
    parent1 = b"4" * 40
    parent2 = b"5" * 40
    octopus = b"6" * 40
    parent3 = b"7" * 40
    output = b"\n".join(
        [
            ordinary + b" " + root,
            merge + b" " + parent1 + b" " + parent2,
            octopus + b" " + parent1 + b" " + parent2 + b" " + parent3,
            root,
            b"",
        ]
    )

    commit_count, merges, octopus_count = hydrate.parse_first_parent_history(output)

    assert commit_count == 4
    assert merges == [
        (merge.decode(), parent1.decode(), parent2.decode()),
    ]
    assert octopus_count == 1


def test_parse_raw_object_ids_collects_old_and_new_and_ignores_zero() -> None:
    old_modified = b"1" * 40
    new_modified = b"2" * 40
    new_added = b"3" * 40
    old_deleted = b"4" * 40
    zero = b"0" * 40
    output = b"\0".join(
        [
            b":100644 100644 "
            + old_modified
            + b" "
            + new_modified
            + b" M",
            b"path with spaces.txt",
            b":000000 100644 " + zero + b" " + new_added + b" A",
            b"added.txt",
            b":100644 000000 " + old_deleted + b" " + zero + b" D",
            b"deleted.txt",
            b"",
        ]
    )

    assert hydrate.parse_raw_object_ids(output) == {
        old_modified.decode(),
        new_modified.decode(),
        new_added.decode(),
        old_deleted.decode(),
    }


def test_parse_raw_object_ids_accepts_inline_non_z_compatibility_path() -> None:
    old = b"a" * 40
    new = b"b" * 40
    output = b":100644 100755 " + old + b" " + new + b" M\tfile.sh\0"
    assert hydrate.parse_raw_object_ids(output) == {old.decode(), new.decode()}


def test_parse_raw_object_ids_accepts_stdin_comparison_separators() -> None:
    comparison = b"c" * 40
    old = b"a" * 40
    new = b"b" * 40
    output = b"\0".join(
        [
            comparison,
            b":100644 100644 " + old + b" " + new + b" M",
            b"file.txt",
            comparison,
            b"",
        ]
    )
    assert hydrate.parse_raw_object_ids(output) == {old.decode(), new.decode()}


def test_parse_raw_object_ids_excludes_superproject_gitlinks() -> None:
    old = b"a" * 40
    new = b"b" * 40
    output = b":160000 160000 " + old + b" " + new + b" M\0submodule\0"
    assert hydrate.parse_raw_object_ids(output) == set()


def test_parse_raw_object_ids_rejects_truncated_record() -> None:
    old = b"a" * 40
    new = b"b" * 40
    output = b":100644 100644 " + old + b" " + new + b" M\0"
    with pytest.raises(hydrate.HydrationError, match="lacks its path"):
        hydrate.parse_raw_object_ids(output)


def test_parse_batch_check_retains_only_missing_oids() -> None:
    existing = "1" * 40
    missing = "2" * 40
    output = (
        f"{existing} blob 123\n"
        f"{missing} missing\n"
    ).encode("ascii")
    assert hydrate.parse_batch_check([existing, missing], output) == [missing]


def test_parse_batch_check_rejects_reordered_output() -> None:
    first = "1" * 40
    second = "2" * 40
    output = f"{second} missing\n{first} blob 1\n".encode("ascii")
    with pytest.raises(hydrate.HydrationError, match="returned"):
        hydrate.parse_batch_check([first, second], output)


def test_fetch_command_matches_promisor_lazy_fetch_shape() -> None:
    mirror = Path("task-mirror")
    assert hydrate.fetch_command("git", mirror) == [
        "git",
        "-C",
        str(mirror),
        "-c",
        "fetch.negotiationAlgorithm=noop",
        "fetch",
        "origin",
        "--quiet",
        "--no-tags",
        "--no-write-fetch-head",
        "--recurse-submodules=no",
        "--filter=blob:none",
        "--stdin",
    ]


def test_primary_merge_base_returns_none_for_unrelated_histories(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        hydrate,
        "run_git",
        lambda *_args, **_kwargs: hydrate.GitResult(
            ("git", "merge-base"), 1, b"", b""
        ),
    )
    assert hydrate.primary_merge_base("git", Path("mirror"), "a" * 40, "b" * 40) is None


def test_merge_bases_from_mined_rows_validates_order_parents_and_provenance(
    tmp_path: Path,
) -> None:
    first = ("1" * 40, "2" * 40, "3" * 40)
    second = ("4" * 40, "5" * 40, "6" * 40)
    base = "7" * 40
    other_base = "6" * 40
    frozen_head = "9" * 40
    rows = [
        {
            "schema_version": 1,
            "repo": "owner/repo",
            "merge": first[0],
            "parents": [first[1], first[2]],
            "merge_base": base,
            "merge_bases": [other_base, base],
            "multiple_merge_bases": True,
            "evaluation_status": "clean",
            "miner_protocol_revision": hydrate.MINER_PROTOCOL_REVISION,
            "miner_source_sha256": hydrate.MINER_SOURCE_SHA256,
        },
        {
            "schema_version": 1,
            "repo": "owner/repo",
            "merge": second[0],
            "parents": [second[1], second[2]],
            "merge_base": None,
            "merge_bases": [],
            "multiple_merge_bases": False,
            "evaluation_status": "no_merge_base",
            "miner_protocol_revision": hydrate.MINER_PROTOCOL_REVISION,
            "miner_source_sha256": hydrate.MINER_SOURCE_SHA256,
        },
    ]
    path = tmp_path / "owner__repo.jsonl"
    all_merges_bytes = "".join(
        hydrate.canonical_json(row) + "\n" for row in rows
    ).encode("ascii")
    path.write_bytes(all_merges_bytes)
    summary_path = tmp_path / "owner__repo.summary.json"
    summary = {
        "schema_version": 1,
        "repo": "owner/repo",
        "slug": "owner__repo",
        "head": frozen_head,
        "miner_protocol_revision": hydrate.MINER_PROTOCOL_REVISION,
        "miner_source_sha256": hydrate.MINER_SOURCE_SHA256,
        "first_parent_commits": 4,
        "first_parent_merges": 3,
        "eligible_two_parent_merges": 2,
        "excluded_octopus_merges": 1,
        "clean_merges": 1,
        "conflicted_merges": 0,
        "failed_merges": 1,
        "no_merge_base_merges": 1,
        "multiple_merge_base_merges": 1,
        "output_sha256": {
            "all_merges": hydrate.hashlib.sha256(all_merges_bytes).hexdigest(),
            "conflicts": hydrate.hashlib.sha256(b"").hexdigest(),
        },
    }
    summary_path.write_bytes(
        (hydrate.canonical_json(summary) + "\n").encode("ascii")
    )

    bases, observed_all_hash, observed_summary_hash = hydrate.merge_bases_from_mined_rows(
        path,
        summary_path,
        {
            "repo": "owner/repo",
            "slug": "owner__repo",
            "frozen_head": frozen_head,
        },
        [first, second],
        4,
        1,
    )

    assert bases == [base, None]
    assert observed_all_hash == hydrate.hashlib.sha256(all_merges_bytes).hexdigest()
    assert observed_summary_hash == hydrate.hashlib.sha256(
        summary_path.read_bytes()
    ).hexdigest()


def test_merge_bases_from_mined_rows_rejects_parent_mismatch(tmp_path: Path) -> None:
    merge = "1" * 40
    parent1 = "2" * 40
    parent2 = "3" * 40
    base = "4" * 40
    path = tmp_path / "owner__repo.jsonl"
    path.write_bytes(
        (
            hydrate.canonical_json(
                {
                    "schema_version": 1,
                    "repo": "owner/repo",
                    "merge": merge,
                    "parents": [parent2, parent1],
                    "merge_base": base,
                    "merge_bases": [base],
                    "multiple_merge_bases": False,
                    "evaluation_status": "conflicted",
                    "miner_protocol_revision": hydrate.MINER_PROTOCOL_REVISION,
                    "miner_source_sha256": hydrate.MINER_SOURCE_SHA256,
                }
            )
            + "\n"
        ).encode("ascii")
    )

    with pytest.raises(hydrate.HydrationError, match="differs from first-parent"):
        hydrate.merge_bases_from_mined_rows(
            path,
            tmp_path / "missing-summary.json",
            {
                "repo": "owner/repo",
                "slug": "owner__repo",
                "frozen_head": "9" * 40,
            },
            [(merge, parent1, parent2)],
            2,
            0,
        )


@pytest.mark.parametrize(
    ("schema_version", "include_base", "message"),
    [
        (True, True, "unsupported schema"),
        (1, False, "lacks explicit merge_base"),
    ],
)
def test_merge_bases_from_mined_rows_rejects_nonexact_schema_or_missing_base(
    tmp_path: Path,
    schema_version: object,
    include_base: bool,
    message: str,
) -> None:
    merge = "1" * 40
    parent1 = "2" * 40
    parent2 = "3" * 40
    row = {
        "schema_version": schema_version,
        "repo": "owner/repo",
        "merge": merge,
        "parents": [parent1, parent2],
        "merge_bases": [],
        "multiple_merge_bases": False,
        "evaluation_status": "no_merge_base",
        "miner_protocol_revision": hydrate.MINER_PROTOCOL_REVISION,
        "miner_source_sha256": hydrate.MINER_SOURCE_SHA256,
    }
    if include_base:
        row["merge_base"] = None
    path = tmp_path / "owner__repo.jsonl"
    path.write_bytes((hydrate.canonical_json(row) + "\n").encode("ascii"))

    with pytest.raises(hydrate.HydrationError, match=message):
        hydrate.merge_bases_from_mined_rows(
            path,
            tmp_path / "missing-summary.json",
            {
                "repo": "owner/repo",
                "slug": "owner__repo",
                "frozen_head": "9" * 40,
            },
            [(merge, parent1, parent2)],
            2,
            0,
        )


def test_fixed_batches_preserve_sorted_input_and_fixed_boundaries() -> None:
    values = [f"{number:040x}" for number in range(5)]
    assert list(hydrate.fixed_batches(values, 2)) == [
        values[0:2],
        values[2:4],
        values[4:5],
    ]


def test_main_continues_after_repository_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mirror_root = tmp_path / "mirrors"
    mirror_root.mkdir()
    repositories = [
        {
            "repo": "owner/first",
            "slug": "owner__first",
            "url": "https://example.invalid/owner/first.git",
            "frozen_head": "1" * 40,
        },
        {
            "repo": "owner/second",
            "slug": "owner__second",
            "url": "https://example.invalid/owner/second.git",
            "frozen_head": "2" * 40,
        },
    ]
    manifest = tmp_path / "repositories.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "mirror_root": "unused",
                "repositories": repositories,
            }
        ),
        encoding="utf-8",
    )
    attempted: list[str] = []

    def fake_hydrate(
        _git: str,
        repository: dict[str, str],
        _mirror_root: Path,
        _batch_size: int,
        _mined_all_merges_root: Path | None = None,
    ) -> dict[str, str]:
        attempted.append(repository["slug"])
        if repository["slug"] == "owner__first":
            raise hydrate.HydrationError("controlled failure")
        return {
            "repo": repository["repo"],
            "slug": repository["slug"],
            "status": "hydrated",
        }

    monkeypatch.setattr(hydrate, "hydrate_repository", fake_hydrate)
    returncode = hydrate.main(
        [
            "--manifest",
            str(manifest),
            "--mirror-root",
            str(mirror_root),
            "--batch-size",
            "7",
        ]
    )
    captured = capsys.readouterr()
    report = json.loads(captured.out)

    assert returncode == 1
    assert attempted == ["owner__first", "owner__second"]
    assert report["counts"] == {
        "already_hydrated": 0,
        "failed": 1,
        "hydrated": 1,
    }
    assert report["batch_size"] == 7
    assert "timestamp" not in captured.out.lower()
    assert "controlled failure" in captured.out
