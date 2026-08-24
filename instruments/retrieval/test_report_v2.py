from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parent))

import metrics_v2
import report_v2


def _write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, values) -> None:
    path.write_text(
        "".join(json.dumps(value, sort_keys=True) + "\n" for value in values),
        encoding="utf-8",
    )


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture(tmp_path: Path, *, unavailable_legacy: bool = False):
    eval_dir = tmp_path / "v2"
    eval_dir.mkdir(parents=True)
    root = r"C:\repo"
    fingerprint = "f" * 64
    hit = root + r"\hit.js"
    record = {
        "id": "q1",
        "followed_by_read": [hit],
        "followed_by_grep": False,
    }
    for filename in report_v2.EVAL_FILES.values():
        _write_jsonl(eval_dir / filename, [record])

    provenance = [
        {
            "record_id": "q1",
            "target_tree_id": "repo-tree",
            "mode": "historical_commit",
            "exact": True,
            "commit": "a" * 40,
            "gap_seconds": 120.0,
            "reason": None,
        }
    ]
    rankings = {
        "ripgrep": [],
        "bm25": [hit],
        "ident_first": [],
        "bm25_pathboost": [hit],
        "bm25_legacy": [],
    }
    runs = []
    for arm in metrics_v2.ALL_ARMS:
        runs.append(
            {
                "record_id": "q1",
                "arm": arm,
                "ranked_paths": rankings[arm],
                "response_bytes": 100 if arm == "ripgrep" else 20,
                "latency_ms": 10 if arm == "ripgrep" else 5,
                "error": "legacy tokenizer unavailable" if unavailable_legacy and arm == "bm25_legacy" else None,
                "tree_id": "repo-tree",
                "logical_root": root,
                "fingerprint": fingerprint,
            }
        )

    records = {str(window): [record] for window in report_v2.WINDOWS}
    metrics = metrics_v2.aggregate_metrics_v2(records, runs, provenance)
    metrics["run_fingerprint"] = fingerprint
    catalog = {
        "schema_version": report_v2.CATALOG_SCHEMA,
        "windows_seconds": list(report_v2.WINDOWS),
        "counts": {
            "records_by_window": {str(window): 1 for window in report_v2.WINDOWS},
            "target_assignments_by_window": {str(window): 1 for window in report_v2.WINDOWS},
            "outside_any_indexed_tree_by_window": {str(window): 0 for window in report_v2.WINDOWS},
        },
        "trees": [
            {
                "tree_id": "repo-tree",
                "logical_root": root,
                "mapping_kind": "current_git_boundary",
                "available": True,
                "note": "valid Git boundary",
                "target_counts": {str(window): 1 for window in report_v2.WINDOWS},
                "cwd_counts": {str(window): 1 for window in report_v2.WINDOWS},
            }
        ],
    }
    retention = {
        "complete": True,
        "corpus_bytes_at_snapshot": 1234,
        "corpus_stream_sha256": "b" * 64,
        "snapshot_utc": "2026-08-23T00:00:00+00:00",
        "files_total": 3,
        "diagnostics": {
            "raw_grep_tool_uses": 2,
            "unique_grep_calls": 2,
            "schema_ids_from_copied_history": 0,
        },
        "retention": {},
    }
    for window in report_v2.WINDOWS:
        retention["retention"][str(window)] = {
            "all_unique_grep_calls": 2,
            "resolvable": 1,
            "retention_rate": 0.5,
            "all_excluded": 1,
            "excluded_abandonment": 1,
            "excluded_missing_grep_result": 0,
            "excluded_unresolved_read_followup": 0,
            "positive_read": 1,
            "failure_next_grep": 0,
        }

    runs_path = eval_dir / "runs-v2.jsonl"
    provenance_path = eval_dir / "reconstruction-v2.jsonl"
    metrics_path = eval_dir / "metrics-v2.json"
    catalog_path = eval_dir / "tree-catalog-v2.json"
    retention_path = eval_dir / "retention.json"
    summary_path = eval_dir / "run-summary-v2.json"
    _write_jsonl(runs_path, runs)
    _write_jsonl(provenance_path, provenance)
    _write_json(metrics_path, metrics)
    _write_json(catalog_path, catalog)
    _write_json(retention_path, retention)
    unavailability = (
        {"repo-tree": {"bm25_legacy": "legacy tokenizer unavailable on this tree"}}
        if unavailable_legacy
        else {}
    )
    summary = {
        "schema_version": report_v2.RUN_SCHEMA,
        "fingerprint": fingerprint,
        "complete": True,
        "debug": False,
        "final": True,
        "generated_utc": "2026-08-23T01:00:00+00:00",
        "windows_seconds": list(report_v2.WINDOWS),
        "arms": list(report_v2.ALL_ARMS),
        "query_cap": None,
        "artifacts": {
            "runs": {"path": str(runs_path), "sha256": _hash(runs_path), "rows": len(runs)},
            "provenance": {
                "path": str(provenance_path),
                "sha256": _hash(provenance_path),
                "rows": len(provenance),
            },
            "metrics": {"path": str(metrics_path), "sha256": _hash(metrics_path)},
            "catalog": {"path": str(catalog_path), "sha256": _hash(catalog_path)},
            "retention": {"path": str(retention_path), "sha256": _hash(retention_path)},
        },
        "execution": {
            "query_order_seed": 20260823,
            "warmup": {"included_in_latency": False},
            "index_caps": {"max_file_bytes": 524288},
        },
        "reconstruction": {
            "exact_queries_by_window": {str(window): 1 for window in report_v2.WINDOWS},
            "fallback_queries_by_window": {str(window): 0 for window in report_v2.WINDOWS},
            "unscored_or_unavailable_queries_by_window": {str(window): 0 for window in report_v2.WINDOWS},
            "non_git_fallback_queries_by_window": {str(window): 0 for window in report_v2.WINDOWS},
            "non_git_unscored_queries_by_window": {str(window): 0 for window in report_v2.WINDOWS},
            "dirty_state_unreconstructable_queries_by_window": {str(window): 1 for window in report_v2.WINDOWS},
        },
        "arm_unavailability_by_tree": unavailability,
        "partial_arm_rows_by_tree": {str(window): {} for window in report_v2.WINDOWS},
        "notes": ["synthetic test run"],
    }
    _write_json(summary_path, summary)
    return {
        "eval_dir": eval_dir,
        "runs": runs_path,
        "provenance": provenance_path,
        "metrics": metrics_path,
        "catalog": catalog_path,
        "retention": retention_path,
        "summary": summary_path,
        "summary_value": summary,
    }


def _load(paths):
    return report_v2.load_report_bundle(
        eval_dir=paths["eval_dir"],
        metrics_path=paths["metrics"],
        retention_path=paths["retention"],
        catalog_path=paths["catalog"],
        summary_path=paths["summary"],
    )


def test_report_has_required_order_population_and_caveat(tmp_path):
    paths = _fixture(tmp_path)
    rendered = report_v2.report_markdown(_load(paths))

    headings = [
        "## Retention",
        "## Scored population per tree",
        "## Per-arm results (300 seconds)",
        "## Head-to-head verdict",
        "## Tokenisation ablation",
        "## Follow-up-window sensitivity",
        "## Claims that could NOT be verified",
        "## What would change this verdict",
    ]
    positions = [rendered.index(heading) for heading in headings]
    assert positions == sorted(positions)
    assert "1 of 1 retained queries" in rendered
    assert "C:\\repo" in rendered
    assert "Per-query reconstruction evidence" in rendered
    assert "| rg n | bm25 n | ident n | path n | legacy n |" in rendered
    assert "Empirical target-mapping kinds" in rendered
    assert "restricts each replay to its recorded" in rendered
    assert "previous 20-query collapse" in rendered
    assert "+100.00 percentage points" in rendered
    assert "98.6% of transcript records come from a single transcript-source tree" in rendered
    assert "not the empirical query-target distribution" in rendered
    assert "within-repository generalisation and nothing more" in rendered
    assert "ripgrep replays each recorded `output_mode` and `head_limit`" in rendered
    assert "index arm returns at most 20 fixed snippets" in rendered
    assert "prespecified arm-specific response contracts" in rendered
    assert "not a symmetric same-K or same-snippet-budget comparison" in rendered
    assert "A symmetric-budget recall/size ranking could not be verified" in rendered
    assert "Any alternative must be fixed before labels are inspected" in rendered
    assert "matching local branch" in rendered
    assert "refs that survive in the repositories today" in rendered
    assert "common response contract" not in rendered
    assert "No fitting or validation split is used" in rendered
    assert "Any session-level split would license within-repository generalisation" in rendered
    assert "No eval or run artifact mixes the two snapshots" in rendered
    assert "No session-clustered uncertainty interval was prespecified" in rendered


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("complete", False, "incomplete"),
        ("debug", True, "debug"),
        ("final", False, "not marked final"),
        ("query_cap", 10, "query-capped"),
    ],
)
def test_refuses_nonfinal_or_capped_runs(tmp_path, field, value, message):
    paths = _fixture(tmp_path)
    summary = paths["summary_value"]
    summary[field] = value
    _write_json(paths["summary"], summary)

    with pytest.raises(report_v2.ReportInputError, match=message):
        _load(paths)


def test_refuses_hash_mismatch_and_incomplete_pairing(tmp_path):
    paths = _fixture(tmp_path)
    paths["runs"].write_text(paths["runs"].read_text(encoding="utf-8") + "{}\n", encoding="utf-8")
    with pytest.raises(report_v2.ReportInputError, match="hash mismatch"):
        _load(paths)

    paths = _fixture(tmp_path / "second")
    run_rows = [
        json.loads(line)
        for line in paths["runs"].read_text(encoding="utf-8").splitlines()
        if line.strip()
    ][:-1]
    _write_jsonl(paths["runs"], run_rows)
    summary = paths["summary_value"]
    summary["artifacts"]["runs"]["sha256"] = _hash(paths["runs"])
    summary["artifacts"]["runs"]["rows"] = len(run_rows)
    _write_json(paths["summary"], summary)
    with pytest.raises(
        report_v2.ReportInputError,
        match="independent recomputation|complete_five_arm_row_queries|paired_scored_queries",
    ):
        _load(paths)


def test_unavailable_arm_is_empty_and_reason_is_reported(tmp_path):
    paths = _fixture(tmp_path, unavailable_legacy=True)
    rendered = report_v2.report_markdown(_load(paths))

    legacy_rows = [line for line in rendered.splitlines() if line.startswith("| `bm25_legacy` |")]
    assert legacy_rows
    assert all("| — | — | — |" in line for line in legacy_rows)
    assert "legacy tokenizer unavailable on this tree" in rendered
    assert "The ablation could not be measured" in rendered


def test_rejects_mixed_fingerprints_and_reconstruction_count_tamper(tmp_path):
    paths = _fixture(tmp_path / "run-row")
    rows = [json.loads(line) for line in paths["runs"].read_text(encoding="utf-8").splitlines()]
    rows[0]["fingerprint"] = "0" * 64
    _write_jsonl(paths["runs"], rows)
    summary = paths["summary_value"]
    summary["artifacts"]["runs"]["sha256"] = _hash(paths["runs"])
    _write_json(paths["summary"], summary)
    with pytest.raises(report_v2.ReportInputError, match="run rows.*fingerprint"):
        _load(paths)

    paths = _fixture(tmp_path / "metrics")
    metrics = json.loads(paths["metrics"].read_text(encoding="utf-8"))
    metrics["run_fingerprint"] = "1" * 64
    _write_json(paths["metrics"], metrics)
    summary = paths["summary_value"]
    summary["artifacts"]["metrics"]["sha256"] = _hash(paths["metrics"])
    _write_json(paths["summary"], summary)
    with pytest.raises(report_v2.ReportInputError, match="metrics run_fingerprint"):
        _load(paths)

    paths = _fixture(tmp_path / "counts")
    summary = paths["summary_value"]
    summary["reconstruction"]["fallback_queries_by_window"]["300"] = 1
    _write_json(paths["summary"], summary)
    with pytest.raises(report_v2.ReportInputError, match="class counts disagree|do not sum"):
        _load(paths)


def test_rejects_numerical_metric_tamper_even_with_updated_hash(tmp_path):
    paths = _fixture(tmp_path)
    metrics = json.loads(paths["metrics"].read_text(encoding="utf-8"))
    metrics["windows"]["300"]["arms"]["ripgrep"]["response_bytes_mean"] = 1
    _write_json(paths["metrics"], metrics)
    summary = paths["summary_value"]
    summary["artifacts"]["metrics"]["sha256"] = _hash(paths["metrics"])
    _write_json(paths["summary"], summary)

    with pytest.raises(report_v2.ReportInputError, match="independent recomputation"):
        _load(paths)


def test_requires_per_query_exact_or_fallback_status(tmp_path):
    paths = _fixture(tmp_path)
    row = json.loads(paths["provenance"].read_text(encoding="utf-8"))
    row.pop("exact")
    _write_jsonl(paths["provenance"], [row])
    summary = paths["summary_value"]
    summary["artifacts"]["provenance"]["sha256"] = _hash(paths["provenance"])
    _write_json(paths["summary"], summary)

    with pytest.raises(report_v2.ReportInputError, match="boolean exact status"):
        _load(paths)
