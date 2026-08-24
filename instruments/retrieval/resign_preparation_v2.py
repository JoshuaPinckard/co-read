"""Re-sign an unchanged V2 preparation after scoring-only implementation edits.

The prepared-run fingerprint includes both immutable inputs and runner source.
Consequently, a reviewed change to scoring execution invalidates the signature
even when the empirical catalog, retained eval sets, reconstruction rows, and
mutable Git anchors are unchanged.  This utility performs a fail-closed audit
of those prepared inputs and records an explicit re-signature reason.

It deliberately does *not* reconstruct history again.  Do not use it after a
change to catalog discovery, branch/commit selection, or provenance semantics;
those changes require ``run_v2.py --reprepare`` instead.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

try:  # package import
    from . import run_v2
    from .tree_catalog_v2 import build_catalog
except ImportError:  # direct script execution
    import run_v2
    from tree_catalog_v2 import build_catalog


IMPLEMENTATION_FILES = (
    "arms.py",
    "index.py",
    "run_v2.py",
    "history_v2.py",
    "incremental_index_v2.py",
    "metrics_v2.py",
    "provenance_v2.py",
    "tree_catalog_v2.py",
)


def _implementation_hashes() -> dict[str, str]:
    root = Path(__file__).resolve().parent
    return {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest()
        for name in IMPLEMENTATION_FILES
    }


def _verify_descriptor(path: Path, descriptor: Mapping[str, Any], label: str) -> None:
    expected = str(descriptor.get("sha256") or "")
    actual = run_v2.sha256_file(path)
    if not expected or actual != expected:
        raise RuntimeError(
            f"prepared {label} changed: expected {expected or '<missing>'}, found {actual}"
        )


def resign_preparation(
    eval_dir: Path,
    output_dir: Path,
    *,
    reason: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    if not reason.strip():
        raise ValueError("an explicit re-signature reason is required")
    for name in ("runs-v2.jsonl", "runs-v2.jsonl.partial"):
        if (output_dir / name).exists():
            raise RuntimeError(f"refusing to re-sign after run rows exist: {name}")

    plan_path = output_dir / "prepare-plan-v2.json"
    catalog_path = output_dir / "tree-catalog-v2.json"
    provenance_path = output_dir / "reconstruction-v2.jsonl"
    exclusions_path = output_dir / "exclusions-v2.jsonl"
    if not all(path.is_file() for path in (plan_path, catalog_path, provenance_path, exclusions_path)):
        raise RuntimeError("a completed V2 preparation is required before re-signing")

    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if plan.get("schema_version") != run_v2.RUN_SCHEMA:
        raise RuntimeError("prepared plan has an incompatible schema")
    artifacts = plan.get("artifacts") or {}
    for label, path in (
        ("catalog", catalog_path),
        ("provenance", provenance_path),
        ("exclusions", exclusions_path),
        ("retention", eval_dir / "retention.json"),
    ):
        _verify_descriptor(path, artifacts.get(label) or {}, label)
    for window in run_v2.WINDOWS:
        path = eval_dir / run_v2.EVAL_FILENAMES[window]
        descriptor = ((artifacts.get("evalsets") or {}).get(str(window)) or {})
        _verify_descriptor(path, descriptor, f"evalset {window}s")

    evalsets = run_v2.load_evalsets(eval_dir)
    if tuple(sorted(evalsets)) != run_v2.WINDOWS:
        raise RuntimeError(f"prepared windows changed: {tuple(sorted(evalsets))}")
    records, _ = run_v2.union_records(evalsets)
    provenance = run_v2._load_jsonl(provenance_path)
    record_ids = [str(row.get("record_id")) for row in provenance]
    if len(record_ids) != len(set(record_ids)) or set(record_ids) != set(records):
        raise RuntimeError("prepared provenance is not one-to-one with the retained union")

    exclusions = run_v2._load_jsonl(exclusions_path)
    if exclusions != run_v2.exclusion_rows(provenance):
        raise RuntimeError("prepared exclusions do not match current provenance rows")
    recorded_catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    if build_catalog(evalsets).to_dict() != recorded_catalog:
        raise RuntimeError("empirical tree catalog changed; perform a full --reprepare")
    run_v2._validate_mutable_state(plan.get("mutable_git_state") or [])

    previous = str(plan.get("fingerprint") or "")
    current = run_v2.run_fingerprint(eval_dir, catalog_path, provenance_path)
    result = {
        "changed": previous != current,
        "dry_run": dry_run,
        "old_fingerprint": previous,
        "new_fingerprint": current,
        "provenance_rows": len(provenance),
        "mutable_git_anchors": len(plan.get("mutable_git_state") or []),
        "reason": reason.strip(),
    }
    if dry_run or previous == current:
        return result

    generated_utc = run_v2._utc_now()
    plan["generated_utc"] = generated_utc
    plan["fingerprint"] = current
    plan.setdefault("execution_resignatures", []).append(
        {
            "generated_utc": generated_utc,
            "reason": reason.strip(),
            "old_fingerprint": previous,
            "new_fingerprint": current,
            "verified_prepared_artifacts": True,
            "verified_empirical_catalog": True,
            "verified_mutable_git_state": True,
            "implementation_sha256": _implementation_hashes(),
        }
    )
    run_v2._atomic_json(plan_path, plan)
    return result


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    project_root = Path(__file__).resolve().parents[2]
    default = project_root / "exploratory" / "retrieval" / "v2"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--eval-dir", type=Path, default=default)
    parser.add_argument("--output-dir", type=Path, default=default)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    result = resign_preparation(
        args.eval_dir.resolve(),
        args.output_dir.resolve(),
        reason=args.reason,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
