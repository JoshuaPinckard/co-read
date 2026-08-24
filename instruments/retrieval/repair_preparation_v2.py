"""Repair explicitly failed V2 preparation rows without redoing valid rows.

This narrow utility exists because repository-history reconstruction is much
more expensive than materialising the selected states.  It re-runs only rows
whose prior scope preflight recorded an infrastructure materialisation failure,
and refuses to proceed if the empirical catalog has changed or any run row
already exists.  It never turns a semantic scope failure into a success.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

try:  # package import
    from .history_v2 import GitHistoryCache
    from . import run_v2
    from .tree_catalog_v2 import build_catalog
except ImportError:  # direct script execution
    from history_v2 import GitHistoryCache
    import run_v2
    from tree_catalog_v2 import build_catalog


def _is_materialisation_failure(row: Mapping[str, Any]) -> bool:
    detail = row.get("exclusion_detail")
    if isinstance(detail, Mapping) and isinstance(detail.get("head_preflight"), Mapping):
        detail = detail["head_preflight"]
    return bool(
        isinstance(detail, Mapping)
        and detail.get("failure_kind") == "infrastructure"
        and "snapshot_stream_materialization_failed" in str(row.get("reason") or "")
    )


def repair_preparation(
    eval_dir: Path,
    output_dir: Path,
    scratch_dir: Path,
    *,
    git_timeout: float,
) -> dict[str, Any]:
    for name in ("runs-v2.jsonl", "runs-v2.jsonl.partial"):
        if (output_dir / name).exists():
            raise RuntimeError(f"refusing to repair preparation after run rows exist: {name}")

    plan_path = output_dir / "prepare-plan-v2.json"
    provenance_path = output_dir / "reconstruction-v2.jsonl"
    catalog_path = output_dir / "tree-catalog-v2.json"
    if not plan_path.is_file() or not provenance_path.is_file() or not catalog_path.is_file():
        raise RuntimeError("a completed V2 preparation is required before repair")

    evalsets = run_v2.load_evalsets(eval_dir)
    records, membership = run_v2.union_records(evalsets)
    build = build_catalog(evalsets)
    recorded_catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    if build.to_dict() != recorded_catalog:
        raise RuntimeError("empirical catalog changed; perform a full --reprepare")

    previous = run_v2._load_jsonl(provenance_path)
    by_id = {str(row["record_id"]): dict(row) for row in previous}
    if set(by_id) != set(records):
        raise RuntimeError("prepared provenance is not one-to-one with the eval union")
    repair_ids = sorted(record_id for record_id, row in by_id.items() if _is_materialisation_failure(row))
    if not repair_ids:
        return {"repair_candidates": 0, "changed_rows": 0}

    subset_records = {record_id: records[record_id] for record_id in repair_ids}
    subset_membership = {record_id: membership[record_id] for record_id in repair_ids}
    history = GitHistoryCache(timeout=git_timeout)
    reconstructed = run_v2.reconstruct_union(
        subset_records,
        subset_membership,
        build,
        cache=history,
    )
    repaired = run_v2.preflight_reconstructed_scopes(
        subset_records,
        reconstructed,
        build,
        cache=history,
        worktree_parent=scratch_dir / "worktrees",
        git_timeout=git_timeout,
    )
    repaired = run_v2.preflight_partial_current_scopes(subset_records, repaired)
    repaired_by_id = {str(row["record_id"]): dict(row) for row in repaired}
    if set(repaired_by_id) != set(repair_ids):
        raise RuntimeError("repair did not return exactly the requested provenance rows")
    repeated = [row for row in repaired if _is_materialisation_failure(row)]
    if repeated:
        raise RuntimeError(
            f"{len(repeated)} materialisation failures remain; refusing partial repair write"
        )

    for record_id, row in repaired_by_id.items():
        by_id[record_id] = row
    merged = [by_id[record_id] for record_id in sorted(by_id)]
    exclusions = run_v2.exclusion_rows(merged)
    exclusions_path = output_dir / "exclusions-v2.jsonl"
    run_v2._atomic_jsonl(provenance_path, merged)
    run_v2._atomic_jsonl(exclusions_path, exclusions)

    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    old_fingerprint = str(plan.get("fingerprint") or "")
    plan["generated_utc"] = run_v2._utc_now()
    plan["artifacts"]["provenance"] = run_v2._artifact_descriptor(
        provenance_path, rows=len(merged)
    )
    plan["artifacts"]["exclusions"] = run_v2._artifact_descriptor(
        exclusions_path, rows=len(exclusions)
    )
    plan["mutable_git_state"] = run_v2._mutable_state(merged)
    plan["index_preflight"]["state_count"] = len(run_v2._group_rows_by_state(merged))
    plan["fingerprint"] = run_v2.run_fingerprint(eval_dir, catalog_path, provenance_path)
    plan.setdefault("repairs", []).append(
        {
            "generated_utc": plan["generated_utc"],
            "kind": "retry_snapshot_stream_materialization_failures",
            "candidate_rows": len(repair_ids),
            "old_fingerprint": old_fingerprint,
            "new_fingerprint": plan["fingerprint"],
        }
    )
    run_v2._atomic_json(plan_path, plan)

    modes = Counter(str(row.get("mode") or "unknown") for row in repaired)
    return {
        "repair_candidates": len(repair_ids),
        "changed_rows": sum(previous[index] != merged[index] for index in range(len(merged))),
        "repaired_mode_counts": dict(sorted(modes.items())),
        "scorable_queries": sum(bool(row.get("target_tree_id")) for row in merged),
        "excluded_queries": sum(not bool(row.get("target_tree_id")) for row in merged),
        "fingerprint": plan["fingerprint"],
    }


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    project_root = Path(__file__).resolve().parents[2]
    default = project_root / "exploratory" / "retrieval" / "v2"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--eval-dir", type=Path, default=default)
    parser.add_argument("--output-dir", type=Path, default=default)
    parser.add_argument(
        "--scratch-dir", type=Path, default=Path("D:/Blast-Radius-retrieval-v2-scratch")
    )
    parser.add_argument("--git-timeout", type=float, default=300.0)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    result = repair_preparation(
        args.eval_dir.resolve(),
        args.output_dir.resolve(),
        args.scratch_dir.resolve(),
        git_timeout=args.git_timeout,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
