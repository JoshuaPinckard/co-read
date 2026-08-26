#!/usr/bin/env python3
"""Independent integrity checks for exploratory/models/hazard.json."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import warnings

import numpy as np
from sklearn.linear_model import LogisticRegression

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from model_lib import load_hazard_rows, sha256_file  # noqa: E402


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    output_path = root / "exploratory" / "models" / "hazard.json"
    output = json.loads(output_path.read_text(encoding="utf-8"))

    rows = load_hazard_rows(root)
    design = np.log1p(rows.exposure).reshape(-1, 1)
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="Setting penalty=None will ignore the C and l1_ratio parameters",
            category=UserWarning,
        )
        independent = LogisticRegression(
            C=np.inf,
            l1_ratio=0.0,
            solver="lbfgs",
            tol=1e-12,
            max_iter=10_000,
        ).fit(design, rows.outcome)
    expected_alpha = output["hazard"]["fit"]["parameters"]["alpha"]["estimate"]
    expected_beta = output["hazard"]["fit"]["parameters"]["beta"]["estimate"]
    if not np.isclose(independent.intercept_[0], expected_alpha, rtol=0, atol=1e-6):
        raise AssertionError((independent.intercept_[0], expected_alpha))
    if not np.isclose(independent.coef_[0, 0], expected_beta, rtol=0, atol=1e-6):
        raise AssertionError((independent.coef_[0, 0], expected_beta))

    for record in output["provenance"]["conflict_row_files"]:
        path = root / record["path"]
        if path.stat().st_size != record["bytes"] or sha256_file(path) != record["sha256"]:
            raise AssertionError(f"source drift: {record['path']}")
    for record in output["provenance"]["other_load_bearing_sources"]:
        path = root / record["path"]
        if path.stat().st_size != record["bytes"] or sha256_file(path) != record["sha256"]:
            raise AssertionError(f"source drift: {record['path']}")

    bootstrap = output["hazard"]["fit"]["repository_bootstrap"]
    if bootstrap["successful_draws"] != bootstrap["requested_draws"]:
        raise AssertionError("incomplete repository bootstrap")
    sites = output["hazard"]["site_conditioned_context"]
    if sites["site_count"] != 19 or sites["historical_conflicts"] != 19:
        raise AssertionError("site reconciliation changed")
    if sites["fit_population_eligible_sites"] + sites["text_component_binary_present_sites"] != 19:
        raise AssertionError("site exposure provenance does not reconcile")

    collision = output["collision_null"]
    null_curve = collision["granularity_curves"]["pooled_handwritten_contiguous_null"]
    if null_curve[0]["overblock_probability"] != 0.0:
        raise AssertionError("exact-span null over-block must be zero")
    if not np.isclose(
        null_curve[-1]["overblock_probability"],
        collision["file_granularity_prediction"]["null_expected_nonoverlap_probability"],
    ):
        raise AssertionError("whole-file curve does not equal exact nonoverlap")

    lease = output["lease"]
    if not np.isclose(lease["L_star_minutes"], lease["optimum"]["lease_minutes"]):
        raise AssertionError("L_star_minutes alias does not match optimum")
    curve_minimum = min(
        lease["curve"],
        key=lambda row: (row["objective_agent_minutes_per_claim"], row["lease_minutes"]),
    )
    if not np.isclose(curve_minimum["lease_minutes"], lease["optimum"]["lease_minutes"]):
        raise AssertionError("reported lease optimum is absent from/default curve")

    print(
        json.dumps(
            {
                "status": "ok",
                "sklearn_alpha": float(independent.intercept_[0]),
                "sklearn_beta": float(independent.coef_[0, 0]),
                "hazard_rows": len(rows.outcome),
                "sites": sites["site_count"],
                "bootstrap_draws": bootstrap["successful_draws"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
