"""Recheck exact hybrid/full co-change equivalence on the ripgrep stream.

Ripgrep is small enough to materialize every clique safely and contains commits
on both sides of the 64-file factorization boundary.  This audit deliberately
stays fixed to that stream; using the all-materialized state on the largest
corpus members could consume unreasonable memory.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

from replay import (
    ResolvedChange,
    ReplayState,
    cochange_query,
    collect_cochange_histories,
    rank_cochange_histories,
)


ROOT = Path(__file__).resolve().parents[2]
SLUG = "BurntSushi__ripgrep"
STREAM_ROOT = ROOT / "exploratory" / "language-hole" / "streams"
RESULT_ROOT = ROOT / "exploratory" / "language-hole" / "results"


def resolved_signature(changes: list[ResolvedChange]) -> list[tuple[object, ...]]:
    return [
        (change.status, change.file_id, change.path, change.old_path, change.new_path)
        for change in changes
    ]


def main() -> None:
    metadata = json.loads((STREAM_ROOT / f"{SLUG}.meta.json").read_text(encoding="utf-8"))
    result = json.loads((RESULT_ROOT / f"{SLUG}.json").read_text(encoding="utf-8"))
    max_commit_age = int(metadata["commit_count"]) + 1

    with gzip.open(
        STREAM_ROOT / f"{SLUG}.jsonl.gz",
        "rt",
        encoding="utf-8",
        errors="surrogatepass",
    ) as handle:
        header = json.loads(handle.readline())
        hybrid = ReplayState(
            header["initial_files"],
            pair_materialize_max_files=64,
            max_commit_age=max_commit_age,
        )
        materialized = ReplayState(
            header["initial_files"],
            pair_materialize_max_files=10**9,
            max_commit_age=max_commit_age,
        )
        query_count = 0

        for line in handle:
            commit = json.loads(line)
            commit_index = int(commit["index"])
            hybrid_changes = hybrid.resolve_changes(commit["changes"])
            materialized_changes = materialized.resolve_changes(commit["changes"])
            assert resolved_signature(hybrid_changes) == resolved_signature(materialized_changes)

            eligible = [change.file_id for change in hybrid_changes if change.status != "A"]
            if len(eligible) >= 2:
                for seed in eligible:
                    seed_history, candidate_histories = collect_cochange_histories(
                        hybrid, seed, commit_index
                    )
                    for decayed in (True, False):
                        shared = rank_cochange_histories(
                            hybrid,
                            seed_history,
                            candidate_histories,
                            commit_index,
                            decayed=decayed,
                        )
                        assert shared == cochange_query(
                            hybrid, seed, commit_index, decayed=decayed
                        )
                        assert shared == cochange_query(
                            materialized, seed, commit_index, decayed=decayed
                        )
                    query_count += 1

            hybrid.fold(commit_index, hybrid_changes)
            materialized.fold(commit_index, materialized_changes)

    assert query_count == result["coverage"]["query_count"]
    assert hybrid.factorized_commit_count == result["implementation"]["factorized_commit_count"]
    assert materialized.factorized_commit_count == 0
    assert hybrid.path_to_id == materialized.path_to_id
    print(
        f"PASS: {metadata['commit_count']:,} commits, {query_count:,} seeds, "
        f"{2 * query_count:,} co-change rankings"
    )


if __name__ == "__main__":
    main()
