from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parent))

import metrics_v2


ARMS = metrics_v2.ALL_ARMS


def record(record_id, reads=(), *, failure=False):
    return {
        "id": record_id,
        "followed_by_read": list(reads),
        "followed_by_grep": failure,
    }


def rows_for(record_id, tree, root, rankings=None, *, omit=(), errors=()):
    rankings = rankings or {}
    return [
        {
            "record_id": record_id,
            "arm": arm,
            "ranked_paths": list(rankings.get(arm, [])),
            "response_bytes": 100 if arm == "ripgrep" else 20,
            "latency_ms": 10 if arm == "ripgrep" else 5,
            "error": "cannot run" if arm in errors else None,
            "tree_id": tree,
            "logical_root": root,
        }
        for arm in ARMS
        if arm not in omit
    ]


def provenance(record_id, tree, *, exact=True, gap=10, reason=None, mode=None):
    return {
        "record_id": record_id,
        "target_tree_id": tree,
        "mode": mode or ("historical_commit" if exact else "head_fallback"),
        "exact": exact,
        "commit": f"commit-{record_id}",
        "gap_seconds": gap,
        "reason": reason,
    }


def test_truth_is_filtered_per_tree_and_behavioral_failures_remain():
    records = {
        "300": [
            record("a", [r"C:\alpha\hit.js", r"C:\beta\not-alpha.js"]),
            record("b", failure=True),
            record("external", [r"C:\elsewhere\only.js"]),
        ]
    }
    run_rows = []
    run_rows += rows_for(
        "a",
        "alpha",
        r"C:\alpha",
        {arm: [r"C:\alpha\hit.js"] for arm in ARMS},
    )
    run_rows += rows_for("b", "beta", r"C:\beta")
    run_rows += rows_for("external", "alpha", r"C:\alpha")
    provenances = {
        "a": provenance("a", "alpha"),
        "b": provenance("b", "beta"),
        "external": provenance("external", "alpha"),
    }

    measured = metrics_v2.aggregate_metrics_v2(records, run_rows, provenances)
    window = measured["windows"]["300"]

    assert window["population"]["paired_scored_queries"] == 3
    assert window["population"]["raw_positive_queries"] == 2
    assert window["population"]["scorable_positive_queries"] == 1
    assert window["population"]["positive_queries_emptied_by_tree_filter"] == 1
    assert window["population"]["behavioral_next_grep_failures"] == 1
    assert window["population"]["quality_queries"] == 2
    assert window["population"]["outside_read_labels_removed"] == 2
    assert window["trees"]["alpha"]["population"]["scorable_positive_queries"] == 1
    assert window["trees"]["beta"]["population"]["behavioral_next_grep_failures"] == 1

    arm = window["arms"]["ripgrep"]
    assert arm["positive_queries"] == 1
    assert arm["behavioral_failure_queries"] == 1
    assert arm["quality_queries"] == 2
    assert arm["recall@1"] == 1.0
    assert arm["precision@1"] == 0.5
    assert arm["failure@20"] == 0.5


def test_missing_one_arm_excludes_id_from_every_arm():
    records = {"300": [record("paired", [r"C:\repo\a.js"]), record("partial", [r"C:\repo\b.js"])]}
    run_rows = rows_for(
        "paired",
        "repo",
        r"C:\repo",
        {arm: [r"C:\repo\a.js"] for arm in ARMS},
    )
    run_rows += rows_for(
        "partial",
        "repo",
        r"C:\repo",
        {arm: [r"C:\repo\b.js"] for arm in ARMS},
        omit=("bm25_legacy",),
    )
    provenances = {key: provenance(key, "repo") for key in ("paired", "partial")}

    measured = metrics_v2.aggregate_metrics_v2(records, run_rows, provenances)["windows"]["300"]

    assert measured["population"]["mapped_queries"] == 2
    assert measured["population"]["paired_scored_queries"] == 1
    assert measured["population"]["missing_arm_counts"] == {"bm25_legacy": 1}
    assert all(item["queries"] == 1 for item in measured["arms"].values())
    assert measured["arms"]["ripgrep"]["recall@1"] == 1.0


def test_windows_regenerate_population_independently():
    records = {
        "60": [record("early", [r"C:\repo\a.js"])],
        "300": [record("early", [r"C:\repo\a.js"]), record("late", failure=True)],
        "900": [record("late", failure=True)],
    }
    run_rows = rows_for(
        "early",
        "repo",
        r"C:\repo",
        {arm: [r"C:\repo\a.js"] for arm in ARMS},
    ) + rows_for("late", "repo", r"C:\repo")
    provenances = {key: provenance(key, "repo") for key in ("early", "late")}

    measured = metrics_v2.aggregate_metrics_v2(records, run_rows, provenances)["windows"]

    assert measured["60"]["population"]["paired_scored_queries"] == 1
    assert measured["60"]["arms"]["ripgrep"]["recall@1"] == 1.0
    assert measured["300"]["population"]["paired_scored_queries"] == 2
    assert measured["300"]["arms"]["ripgrep"]["failure@20"] == 0.5
    assert measured["900"]["population"]["paired_scored_queries"] == 1
    assert measured["900"]["arms"]["ripgrep"]["positive_queries"] == 0


def test_empty_arm_and_tree_are_json_serializable_and_null_metric_safe():
    records = {"300": [record("partial", [r"C:\occupied\a.js"])]}
    run_rows = rows_for("partial", "occupied", r"C:\occupied", omit=("bm25_legacy",))
    # Mentioning an otherwise unused tree in provenance makes it part of the
    # stable per-window tree schema.
    provenances = {
        "partial": provenance("partial", "occupied"),
        "not-in-window": provenance("not-in-window", "empty"),
    }

    measured = metrics_v2.aggregate_metrics_v2(records, run_rows, provenances)
    window = measured["windows"]["300"]

    assert window["population"]["paired_scored_queries"] == 0
    assert window["arms"]["ripgrep"]["queries"] == 0
    assert window["arms"]["ripgrep"]["recall@20"] is None
    assert window["head_to_head"]["verdict"] is None
    assert window["tokenization_ablation"]["available"] is False
    assert window["trees"]["empty"]["population"]["retained_queries"] == 0
    assert window["trees"]["empty"]["arms"]["bm25"]["response_bytes_mean"] is None
    json.dumps(measured, allow_nan=False)


def test_reconstruction_counts_and_signed_gap_buckets():
    records = {"300": [record("exact"), record("fallback")]}
    run_rows = rows_for("exact", "repo", r"C:\repo") + rows_for("fallback", "repo", r"C:\repo")
    provenances = {
        "exact": provenance("exact", "repo", exact=True, gap=7_200),
        "fallback": provenance(
            "fallback",
            "repo",
            exact=False,
            gap=-90_000,
            reason="cross_repo_branch_unproven",
        ),
    }

    reconstruction = metrics_v2.aggregate_metrics_v2(records, run_rows, provenances)["windows"]["300"][
        "reconstruction"
    ]

    assert reconstruction["exact_queries"] == 1
    assert reconstruction["fallback_queries"] == 1
    assert reconstruction["exact_gap_seconds"]["buckets"]["1h_to_1d"] == 1
    assert reconstruction["fallback_gap_seconds"]["negative"] == 1
    assert reconstruction["fallback_gap_seconds"]["buckets"]["minus_7d_to_minus_1d"] == 1
    assert reconstruction["fallback_reason_counts"] == {"cross_repo_branch_unproven": 1}


def test_reconstruction_summary_keeps_unscored_separate_from_fallback():
    values = {
        "exact": {"record_id": "exact", "exact": True, "mode": "historical_exact", "commit": "a", "gap_seconds": 1},
        "fallback": {"record_id": "fallback", "exact": False, "mode": "head_fallback", "commit": "b", "gap_seconds": -2},
        "missing": {"record_id": "missing", "exact": False, "mode": "unscored", "commit": None, "gap_seconds": None, "reason": "scope_absent"},
    }
    summary = metrics_v2._provenance_summary(values, values)
    assert summary["exact_queries"] == 1
    assert summary["fallback_queries"] == 1
    assert summary["unscored_or_unavailable_queries"] == 1
    assert summary["fallback_gap_seconds"]["count"] == 1
    assert summary["unscored_or_unavailable_gap_seconds"]["missing"] == 1


def test_head_to_head_and_tokenization_delta_use_paired_population():
    records = {"300": [record("q", [r"C:\repo\hit.js"])]}
    rankings = {
        "ripgrep": [],
        "bm25": [r"C:\repo\hit.js"],
        "ident_first": [],
        "bm25_pathboost": [r"C:\repo\hit.js"],
        "bm25_legacy": [],
    }
    run_rows = rows_for("q", "repo", r"C:\repo", rankings)
    # Make ripgrep larger than the index responses.
    for row in run_rows:
        row["response_bytes"] = 100 if row["arm"] == "ripgrep" else 20

    window = metrics_v2.aggregate_metrics_v2(
        records, run_rows, {"q": provenance("q", "repo")}
    )["windows"]["300"]

    assert window["head_to_head"]["verdict"] is True
    assert window["head_to_head"]["winners"] == ["bm25", "bm25_pathboost"]
    assert window["tokenization_ablation"]["deltas"]["recall@20"] == 1.0
    assert window["tokenization_ablation"]["recall@20_percentage_points"] == 100.0


def test_inconsistent_tree_rows_are_reported_as_a_bug():
    records = {"300": [record("q")]}
    run_rows = rows_for("q", "repo", r"C:\repo")
    run_rows[-1]["tree_id"] = "wrong-tree"

    with pytest.raises(ValueError, match="disagree with target tree"):
        metrics_v2.aggregate_metrics_v2(records, run_rows, {"q": provenance("q", "repo")})
