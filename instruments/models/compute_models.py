#!/usr/bin/env python3
"""Compute and render the preregistration hazard, collision, and lease models.

This script reads frozen, already-mined local artifacts only.  It never invokes
Git, accesses a corpus mirror, reads transcript source files, or uses a network.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
import math
from pathlib import Path
import sys
from typing import Any, Iterable, Sequence

import numpy as np

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from model_lib import (  # noqa: E402
    GRANULARITIES,
    HAZARD_BOOTSTRAP_SEED,
    HAZARD_MODEL_FORMULA,
    build_lease_analysis,
    cluster_robust_covariance,
    distribution_summary,
    extract_span_pairs,
    fit_logistic,
    hazard_bin_table,
    hazard_point,
    hazard_probability,
    load_conflict_index,
    load_hazard_rows,
    order_stat_extremes,
    pushed_pair_prediction,
    quantile_reconstruction,
    repository_bootstrap,
    result_blob_size_sensitivity,
    sha256_file,
    site_hazards,
    source_record,
    span_curve,
)


SCHEMA_VERSION = "blast-radius-hazard-models/1"


def finite_float(value: Any) -> Any:
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"non-finite float in output: {value}")
        return value
    if isinstance(value, dict):
        return {str(key): finite_float(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [finite_float(item) for item in value]
    return value


def percentile_interval(values: np.ndarray) -> list[float]:
    return [
        float(np.quantile(values, 0.025, method="linear")),
        float(np.quantile(values, 0.975, method="linear")),
    ]


def add_bootstrap_prediction(
    point: dict[str, Any],
    bootstrap_parameters: np.ndarray,
    exposure: float,
) -> None:
    probabilities = np.asarray(
        [float(hazard_probability(theta, exposure)) for theta in bootstrap_parameters]
    )
    point["repository_bootstrap_ci95"] = percentile_interval(probabilities)


def build_hazard_analysis(
    root: Path,
    parameters: dict[str, Any],
    conflict_index: dict[tuple[str, str], dict[str, Any]],
    bootstrap_draws: int,
) -> dict[str, Any]:
    rows = load_hazard_rows(root)
    if len(rows.outcome) != 23_428:
        raise AssertionError(f"expected 23,428 countable-text merges, found {len(rows.outcome):,}")
    if int(np.sum(rows.outcome)) != 238:
        raise AssertionError(f"expected 238 countable-text conflicts, found {int(np.sum(rows.outcome))}")
    if rows.all_evaluable != 25_073:
        raise AssertionError(f"expected 25,073 evaluable merges, found {rows.all_evaluable:,}")
    if rows.unavailable_exposure != 1_645 or rows.unavailable_conflicts != 178:
        raise AssertionError(
            "unavailable exposure reconciliation changed: "
            f"{rows.unavailable_conflicts}/{rows.unavailable_exposure}"
        )

    theta, naive_covariance, success, message = fit_logistic(rows.exposure, rows.outcome)
    if not success:
        raise RuntimeError(f"hazard fit did not converge: {message}")
    covariance, cluster_details = cluster_robust_covariance(
        theta,
        rows.exposure,
        rows.outcome,
        rows.repos,
    )
    bootstrap_parameters, bootstrap_details = repository_bootstrap(
        rows,
        theta,
        draws=bootstrap_draws,
        seed=HAZARD_BOOTSTRAP_SEED,
    )

    cluster_se = np.sqrt(np.diag(covariance))
    normal_low = theta - 1.959963984540054 * cluster_se
    normal_high = theta + 1.959963984540054 * cluster_se
    parameter_names = ("alpha", "beta")
    parameters_out: dict[str, Any] = {}
    for index, name in enumerate(parameter_names):
        parameters_out[name] = {
            "estimate": float(theta[index]),
            "repository_cluster_cr1_se": float(cluster_se[index]),
            "repository_cluster_normal_ci95": [float(normal_low[index]), float(normal_high[index])],
            "repository_bootstrap_percentile_ci95": percentile_interval(bootstrap_parameters[:, index]),
        }

    bins = hazard_bin_table(rows, theta, covariance)
    expected_counts = [(0, 3), (6, 5341), (17, 3770), (39, 4345), (73, 4307), (52, 3049), (51, 2613)]
    actual_counts = [(row["conflicted"], row["evaluable"]) for row in bins]
    if actual_counts != expected_counts:
        raise AssertionError(f"MINING.md bin reconciliation failed: {actual_counts!r}")
    for row in bins:
        bootstrap_probabilities = np.asarray(
            [
                float(hazard_probability(estimate, row["observed_midpoint"]))
                for estimate in bootstrap_parameters
            ]
        )
        row["repository_bootstrap_ci95"] = percentile_interval(bootstrap_probabilities)

    edit_summary = parameters["parameters"]["edit_region_sizes"]["aggregate_claim_lines_per_write"]
    edit_n = int(edit_summary["count"])
    edit_p50 = int(edit_summary["p50"])
    edit_p90 = int(edit_summary["p90"])
    edit_p99 = int(edit_summary["p99"])
    edit_max = int(edit_summary["max"])
    median_prediction = hazard_point(theta, covariance, float(2 * edit_p50))
    median_prediction.update(
        {
            "label": "p50_task_size_pair",
            "per_task_lines": edit_p50,
            "combined_exposure_lines": 2 * edit_p50,
            "input_statistic": "nearest-rank p50 of aggregate_claim_lines_per_write",
        }
    )
    add_bootstrap_prediction(median_prediction, bootstrap_parameters, 2 * edit_p50)
    p90_prediction = hazard_point(theta, covariance, float(2 * edit_p90))
    p90_prediction.update(
        {
            "label": "p90_task_size_pair",
            "per_task_lines": edit_p90,
            "combined_exposure_lines": 2 * edit_p90,
            "input_statistic": "nearest-rank p90 of aggregate_claim_lines_per_write",
        }
    )
    add_bootstrap_prediction(p90_prediction, bootstrap_parameters, 2 * edit_p90)

    reconstructed = quantile_reconstruction(
        edit_n,
        edit_p50,
        edit_p90,
        edit_p99,
        edit_max,
        1,
        integer=True,
    )
    low_distribution, high_distribution = order_stat_extremes(
        edit_n,
        edit_p50,
        edit_p90,
        edit_p99,
        edit_max,
        1,
    )
    reconstructed_prediction = pushed_pair_prediction(theta, reconstructed)
    lower_prediction = pushed_pair_prediction(theta, low_distribution)
    upper_prediction = pushed_pair_prediction(theta, high_distribution)
    reconstruction_bootstrap = np.asarray(
        [pushed_pair_prediction(estimate, reconstructed) for estimate in bootstrap_parameters]
    )
    distribution_prediction = {
        "label": "independent_distribution_pushthrough",
        "estimand": "E[h(W1+W2)] for independent same-distribution task draws",
        "status": "not_point_identified_from_parameters_json",
        "provisional_quantile_reconstruction_probability": reconstructed_prediction,
        "provisional_repository_bootstrap_ci95": percentile_interval(reconstruction_bootstrap),
        "summary_consistent_probability_bounds": [lower_prediction, upper_prediction],
        "bound_status": "sharp under monotonic hazard, integer task sizes, minimum one line, and only the retained nearest-rank constraints",
        "reconstructed_task_distribution": distribution_summary(reconstructed),
        "lower_extreme_task_distribution": distribution_summary(low_distribution),
        "upper_extreme_task_distribution": distribution_summary(high_distribution),
        "inputs": {
            "count": edit_n,
            "minimum_assumption": 1,
            "p50": edit_p50,
            "p90": edit_p90,
            "p99": edit_p99,
            "max": edit_max,
            "percentile_method": edit_summary["percentile_method"],
        },
        "independence_assumption": "W1 and W2 are independent draws with replacement",
        "missing_input": "the 1,354 raw aggregate_claim_lines_per_write observations or a lossless histogram",
    }

    sites, manifest_check = site_hazards(root, conflict_index, theta, covariance)
    site_mean = float(np.mean([site["probability"] for site in sites]))
    site_bootstrap_means = np.asarray(
        [
            float(
                np.mean(
                    [
                        hazard_probability(estimate, site["combined_text_lines_changed"])
                        for site in sites
                    ]
                )
            )
            for estimate in bootstrap_parameters
        ]
    )

    eta = theta[0] + theta[1] * np.log1p(rows.exposure)
    log_likelihood = float(np.sum(rows.outcome * eta - np.logaddexp(0.0, eta)))
    return {
        "model_choice_locked_before_fit": True,
        "model_specification_path": "instruments/models/MODEL_SPEC.md",
        "formula": HAZARD_MODEL_FORMULA,
        "population": {
            "evaluable_merges": rows.all_evaluable,
            "countable_text_merges": len(rows.outcome),
            "countable_text_conflicts": int(np.sum(rows.outcome)),
            "countable_text_conflict_rate": float(np.mean(rows.outcome)),
            "unavailable_text_exposure_merges": rows.unavailable_exposure,
            "unavailable_text_exposure_conflicts": rows.unavailable_conflicts,
            "repositories": len(set(str(value) for value in rows.repos)),
        },
        "fit": {
            "parameters": parameters_out,
            "covariance_repository_cluster_cr1": covariance.tolist(),
            "covariance_naive_information_inverse": naive_covariance.tolist(),
            "log_likelihood": log_likelihood,
            "constraint_active": bool(theta[1] <= 1e-10),
            "optimizer_success": success,
            "optimizer_message": message,
            "cluster_uncertainty": cluster_details,
            "repository_bootstrap": bootstrap_details,
        },
        "sanity_check_against_mining_bins": bins,
        "exposure_only_predictions": {
            "definition": "two concurrent unselected tasks; combined exposure is W1+W2 in changed lines",
            "p50": median_prediction,
            "p90": p90_prediction,
            "distribution_mean_pushthrough": distribution_prediction,
            "preregistered_use": "P6 exposure-only prediction for the unmediated collision rate on future unselected concurrent tasks",
        },
        "site_conditioned_context": {
            "sites": sites,
            "site_count": len(sites),
            "historical_conflicts": sum(site["historical_conflicted"] for site in sites),
            "fit_population_eligible_sites": sum(site["fit_population_eligible"] for site in sites),
            "text_component_binary_present_sites": sum(not site["fit_population_eligible"] for site in sites),
            "historical_realized_collision_rate": 1.0,
            "mean_fitted_hazard": site_mean,
            "mean_fitted_hazard_repository_bootstrap_ci95": percentile_interval(site_bootstrap_means),
            "realized_to_mean_hazard_ratio": 1.0 / site_mean,
            "manifest_check": manifest_check,
            "interpretation": "all sites were selected as historical conflicts; 100% versus the mean hazard measures selection concentration beyond line exposure, not out-of-sample calibration",
            "preregistered_selected_site_claim": "ordering only: realized selected-site collision rate above the exposure-only prediction at agent diff sizes",
        },
        "source_files": list(rows.files),
    }


def build_collision_analysis(
    root: Path,
    conflict_index: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any]:
    pairs, marker_spans, exclusions = extract_span_pairs(conflict_index)
    handwritten = [pair for pair in pairs if pair.path_classification == "handwritten"]
    click_all = [pair for pair in pairs if pair.repo == "pallets/click"]
    click_handwritten = [
        pair for pair in pairs if pair.repo == "pallets/click" and pair.path_classification == "handwritten"
    ]
    if not handwritten:
        raise AssertionError("no handwritten classifiable span pairs")
    if not click_all:
        raise AssertionError("no Click classifiable span pairs")
    if len(marker_spans) != 845:
        raise AssertionError(f"expected 845 measurable handwritten conflict spans, found {len(marker_spans)}")

    pooled_curve = span_curve(handwritten, width_mode="changed_byte_mass")
    click_curve = span_curve(click_all, width_mode="changed_byte_mass")
    click_handwritten_curve = span_curve(click_handwritten, width_mode="changed_byte_mass")
    pooled_hull_sensitivity = span_curve(handwritten, width_mode="bounding_hull")
    click_hull_sensitivity = span_curve(click_all, width_mode="bounding_hull")
    null_disjoint = pooled_curve["null_exact_disjoint_probability"]
    null_overlap = pooled_curve["null_expected_overlap_probability"]

    measured_nonconflicting = 475
    measured_total = 684
    measured_conflicting = measured_total - measured_nonconflicting
    measured_disjoint_proxy = measured_nonconflicting / measured_total
    measured_overlap_proxy = measured_conflicting / measured_total

    full_census_ratio = measured_overlap_proxy / null_overlap
    paired_strict_ratio = (
        click_curve["empirical_strict_overlap_probability"]
        / click_curve["null_expected_overlap_probability"]
    )
    paired_hull_sensitivity_ratio = (
        click_hull_sensitivity["empirical_hull_overlap_probability"]
        / click_hull_sensitivity["null_expected_overlap_probability"]
    )

    by_repo = Counter(pair.repo for pair in handwritten)
    marker_widths = [item["span_bytes"] for item in marker_spans]
    marker_sizes = [item["result_blob_size"] for item in marker_spans]
    marker_fractions = [item["span_bytes"] / item["result_blob_size"] for item in marker_spans]
    result_sensitivity = result_blob_size_sensitivity(handwritten, width_mode="changed_byte_mass")
    return {
        "null_definition": {
            "primary": "two positive contiguous half-open byte spans with independent uniform integer starts conditional on measured changed-byte masses w1,w2 and base_blob_size N",
            "disjoint_formula": "if D=N-w1-w2 >= 0: (D+1)(D+2)/((N-w1+1)(N-w2+1)); else 0",
            "insertion_rule": "an insertion-only edit contributes one effective byte at its anchor (the adjacent in-file byte at EOF)",
            "multi_hunk_rule": "w is unioned changed-byte mass across refined intervals plus effective insertion bytes; the null places one contiguous span of that size",
            "bounding_hull_sensitivity": "a named sensitivity counts unchanged gaps between first and last changed coordinates; it is not primary",
            "scattered_reference": "exp(-w1*w2/N); reported as a distinct birthday/scattered-byte reference, not used as the contiguous null",
            "not_tuned_to_click": True,
        },
        "span_inputs": {
            "all_strict_classifiable_effective_pairs": len(pairs),
            "handwritten_primary_pairs": len(handwritten),
            "click_conflict_selected_pairs": len(click_all),
            "click_conflict_selected_handwritten_pairs": len(click_handwritten),
            "primary_pairs_by_repository": dict(sorted(by_repo.items())),
            "excluded_path_counts": exclusions,
            "handwritten_marker_spans": {
                "span_bytes": distribution_summary(marker_widths),
                "result_blob_size_bytes": distribution_summary(marker_sizes),
                "span_fraction": distribution_summary(marker_fractions),
                "pairing_note": "marker spans describe disputed result regions but do not supply two side-specific widths; the paired null therefore uses unioned changed-byte masses from the mined parent-side base-coordinate spans",
            },
            "primary_width1_bytes": distribution_summary([pair.width1 for pair in handwritten]),
            "primary_width2_bytes": distribution_summary([pair.width2 for pair in handwritten]),
            "bounding_hull_width1_bytes_sensitivity": distribution_summary([pair.hull_width1 for pair in handwritten]),
            "bounding_hull_width2_bytes_sensitivity": distribution_summary([pair.hull_width2 for pair in handwritten]),
            "primary_base_blob_size_bytes": distribution_summary([pair.file_size for pair in handwritten]),
        },
        "file_granularity_prediction": {
            "null_expected_nonoverlap_probability": null_disjoint,
            "null_expected_nonoverlap_units_over_primary_span_sample": null_disjoint * len(handwritten),
            "null_primary_span_denominator": len(handwritten),
            "measured_click_nonconflicting_units": measured_nonconflicting,
            "measured_click_same_file_units": measured_total,
            "measured_click_overblock_rate": measured_disjoint_proxy,
            "measured_minus_null_probability": measured_disjoint_proxy - null_disjoint,
            "comparison_warning": "the null width distribution is from handwritten, classifiable, conflict-selected paths across repositories; the measured Click rate is a complete anchored textual-conflict census",
            "result_blob_N_sensitivity": result_sensitivity,
        },
        "granularity_curves": {
            "granularity_rule": "expand each retained interval/effective insertion byte outward to every fixed file-origin-aligned g-byte block it touches; overblock iff the miner says exact changes are strict-disjoint but padded claim unions overlap",
            "pooled_handwritten_contiguous_null": pooled_curve["null"],
            "click_conflict_selected_empirical_spans": click_curve["empirical"],
            "click_conflict_selected_same_cohort_null": click_curve["null"],
            "click_handwritten_sensitivity_empirical_spans": click_handwritten_curve["empirical"],
            "pooled_bounding_hull_width_null_sensitivity": pooled_hull_sensitivity["null"],
            "click_bounding_hull_width_null_sensitivity": click_hull_sensitivity["null"],
            "click_empirical_denominator_warning": "base-coordinate spans are retained only for conflict-selected mined paths, not the full 684-unit Click census",
        },
        "clustering_statistics": {
            "full_click_textual_proxy": {
                "observed_overlap_units": measured_conflicting,
                "observed_same_file_units": measured_total,
                "observed_overlap_probability": measured_overlap_proxy,
                "null_expected_overlap_units": null_overlap * len(handwritten),
                "null_span_denominator": len(handwritten),
                "null_overlap_probability": null_overlap,
                "observed_to_null_overlap_ratio": full_census_ratio,
                "warning": "numerator and null exposure distribution come from different cohorts; this is contextual, not an identified causal clustering effect",
            },
            "click_span_supported_miner_strict": {
                "observed_overlap_units": click_curve["empirical_strict_overlap_units"],
                "observed_span_pairs": len(click_all),
                "observed_overlap_probability": click_curve["empirical_strict_overlap_probability"],
                "null_expected_overlap_units": click_curve["null_expected_overlap_units"],
                "null_span_pairs": len(click_all),
                "null_overlap_probability": click_curve["null_expected_overlap_probability"],
                "observed_to_null_overlap_ratio": paired_strict_ratio,
            },
            "click_bounding_hull_width_sensitivity": {
                "observed_overlap_units": click_hull_sensitivity["empirical_hull_overlap_units"],
                "observed_span_pairs": len(click_all),
                "observed_overlap_probability": click_hull_sensitivity["empirical_hull_overlap_probability"],
                "null_expected_overlap_units": click_hull_sensitivity["null_expected_overlap_units"],
                "null_span_pairs": len(click_all),
                "null_overlap_probability": click_hull_sensitivity["null_expected_overlap_probability"],
                "observed_to_null_overlap_ratio": paired_hull_sensitivity_ratio,
                "warning": "sensitivity counts unchanged gaps inside each bounding hull as width and overlap",
            },
        },
        "birthday_reference": {
            "pooled_handwritten_mean_disjoint_probability": pooled_curve[
                "scattered_birthday_disjoint_probability"
            ],
            "interpretation": "not the requested contiguous null and not used for conclusions",
        },
    }


def pct(value: float, digits: int = 3) -> str:
    return f"{100 * value:.{digits}f}%"


def num(value: float, digits: int = 6) -> str:
    return f"{value:.{digits}f}"


def interval_text(interval: Sequence[float], percent: bool = False) -> str:
    if percent:
        return f"[{pct(interval[0])}, {pct(interval[1])}]"
    return f"[{num(interval[0])}, {num(interval[1])}]"


def markdown_table(headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(str(item) for item in row) + " |")
    return lines


def selected_sensitivity_rows(rows: list[dict[str, Any]], default_ratio: float) -> list[dict[str, Any]]:
    targets = [0.1, 0.2, 0.5, 1.0, 2.0, default_ratio, 5.0, 10.0]
    selected: list[dict[str, Any]] = []
    for target in targets:
        selected.append(min(rows, key=lambda row: abs(row["numeric_cost_ratio_reacquisition_to_blocking"] - target)))
    unique: dict[float, dict[str, Any]] = {
        row["numeric_cost_ratio_reacquisition_to_blocking"]: row for row in selected
    }
    return [unique[key] for key in sorted(unique)]


def render_markdown(result: dict[str, Any]) -> str:
    hazard = result["hazard"]
    collision = result["collision_null"]
    lease = result["lease"]
    lines: list[str] = []
    lines.extend(
        [
            "# Hazard transfer, collision null, and lease rule",
            "",
            "This report is generated from frozen local artifacts by "
            "[`compute_models.py`](../../instruments/models/compute_models.py). It performs no new mining, "
            "does not invoke Git, and uses no network or agent subjects. The hazard model choice was locked "
            "before the first fit in [`MODEL_SPEC.md`](../../instruments/models/MODEL_SPEC.md); that file also "
            "records two explicit input-definition corrections made during dry-run validation before the "
            "production output was accepted.",
            "",
            "## 1. Conflict hazard and preregistered prediction",
            "",
            "### Model choice stated before fitting",
            "",
            f"The chosen model is `{hazard['formula']}`. The outcome is the stored per-merge conflict "
            "indicator and exposure is the stored two-side combined countable changed-line total. The "
            "slope is constrained nonnegative. Uncertainty uses the CR1 repository-cluster sandwich; a "
            f"{hazard['fit']['repository_bootstrap']['requested_draws']:,}-draw repository bootstrap with "
            f"seed {hazard['fit']['repository_bootstrap']['seed']} is the small-cluster sensitivity.",
            "",
        ]
    )
    population = hazard["population"]
    lines.append(
        f"The fit uses **{population['countable_text_conflicts']:,} / "
        f"{population['countable_text_merges']:,} = {pct(population['countable_text_conflict_rate'])}** "
        f"conflicted countable-text merges from {population['repositories']} repositories. This reconciles "
        f"to {population['countable_text_merges']:,} / {population['evaluable_merges']:,} evaluable merges. "
        f"The excluded exposure-missing stratum is {population['unavailable_text_exposure_conflicts']:,} / "
        f"{population['unavailable_text_exposure_merges']:,} = "
        f"{pct(population['unavailable_text_exposure_conflicts']/population['unavailable_text_exposure_merges'])}; "
        "the fitted relationship is therefore coverage-conditioned."
    )
    lines.extend(["", "Fitted parameters:", ""])
    param_rows = []
    for name in ("alpha", "beta"):
        item = hazard["fit"]["parameters"][name]
        param_rows.append(
            (
                name,
                num(item["estimate"], 8),
                num(item["repository_cluster_cr1_se"], 8),
                interval_text(item["repository_cluster_normal_ci95"]),
                interval_text(item["repository_bootstrap_percentile_ci95"]),
            )
        )
    lines.extend(
        markdown_table(
            ["Parameter", "Estimate", "Repo-cluster CR1 SE", "Cluster-normal 95%", "Repo-bootstrap 95%"],
            param_rows,
        )
    )
    fit = hazard["fit"]
    lines.extend(
        [
            "",
            f"Inputs/fit checks: log likelihood `{fit['log_likelihood']:.6f}`; CR1 correction "
            f"`{fit['cluster_uncertainty']['cr1_correction']:.6f}` over "
            f"{fit['cluster_uncertainty']['clusters']} repository clusters; bootstrap "
            f"{fit['repository_bootstrap']['successful_draws']:,} successful / "
            f"{fit['repository_bootstrap']['requested_draws']:,} requested fits. The monotonicity boundary "
            f"was {'active' if fit['constraint_active'] else 'not active'}.",
            "",
            "### Sanity check against MINING.md bins",
            "",
            "The midpoint below is `(observed minimum + observed maximum)/2` within each fixed MINING.md "
            "bin, including the open top bin. Because the curve is nonlinear, the mean fitted probability "
            "over all rows in the bin is also shown; neither value is expected to equal a pooled observed "
            "rate exactly.",
            "",
        ]
    )
    bin_rows = []
    for row in hazard["sanity_check_against_mining_bins"]:
        bin_rows.append(
            (
                row["bin"],
                f"{row['observed_min']:,}-{row['observed_max']:,}; mid {row['observed_midpoint']:,.1f}",
                f"{row['conflicted']:,}/{row['evaluable']:,} ({pct(row['observed_rate'])})",
                f"{pct(row['probability'])} {interval_text([row['cluster_ci95_low'], row['cluster_ci95_high']], True)}",
                pct(row["mean_fitted_probability_in_bin"]),
                row["contributing_repositories"],
            )
        )
    lines.extend(
        markdown_table(
            ["MINING bin", "Observed x range; midpoint", "Observed", "h(mid), cluster 95%", "Mean h(x) in bin", "Repos"],
            bin_rows,
        )
    )

    predictions = hazard["exposure_only_predictions"]
    p50_row = predictions["p50"]
    p90_row = predictions["p90"]
    dist = predictions["distribution_mean_pushthrough"]
    lines.extend(
        [
            "",
            "### (a) Exposure-only prediction for future unselected tasks",
            "",
            "`W` is one transcript-corpus write's **aggregate claim lines per write**, not one hunk. "
            "For percentile scenarios both concurrent tasks are set to the named percentile. For the "
            "distributional estimand, tasks are independent empirical draws with replacement and the "
            "mean is taken *after* applying the nonlinear hazard.",
            "",
        ]
    )
    prediction_rows = [
        (
            "p50 pair",
            f"W1=W2={p50_row['per_task_lines']} lines; x={p50_row['combined_exposure_lines']} lines",
            pct(p50_row["probability"]),
            interval_text([p50_row["cluster_ci95_low"], p50_row["cluster_ci95_high"]], True),
            interval_text(p50_row["repository_bootstrap_ci95"], True),
        ),
        (
            "p90 pair",
            f"W1=W2={p90_row['per_task_lines']} lines; x={p90_row['combined_exposure_lines']} lines",
            pct(p90_row["probability"]),
            interval_text([p90_row["cluster_ci95_low"], p90_row["cluster_ci95_high"]], True),
            interval_text(p90_row["repository_bootstrap_ci95"], True),
        ),
        (
            "E[h(W1+W2)]",
            f"n={dist['inputs']['count']:,}; p50/p90/p99/max="
            f"{dist['inputs']['p50']}/{dist['inputs']['p90']}/{dist['inputs']['p99']}/{dist['inputs']['max']} lines",
            f"{pct(dist['provisional_quantile_reconstruction_probability'])} provisional",
            f"summary bounds {interval_text(dist['summary_consistent_probability_bounds'], True)}",
            f"provisional repo-bootstrap {interval_text(dist['provisional_repository_bootstrap_ci95'], True)}",
        ),
    ]
    lines.extend(
        markdown_table(
            ["Prediction", "Inputs", "Point", "Cluster/identification interval", "Repo-bootstrap sensitivity"],
            prediction_rows,
        )
    )
    recon = dist["reconstructed_task_distribution"]
    lines.extend(
        [
            "",
            f"The provisional reconstruction has mean task size {recon['mean']:.3f} lines over "
            f"{recon['count']:,} reconstructed values. It is **not** the requested empirical "
            "distributional mean: `parameters.json` retains only count, p50, p90, p99, and maximum. "
            f"Under the retained nearest-rank constraints, integer `W>=1`, and the fitted monotone curve, "
            f"the sharp probability range is {interval_text(dist['summary_consistent_probability_bounds'], True)}. "
            "The amendment should quote the provisional point only with this label, or use the range until "
            "a lossless histogram is exported.",
            "",
            "This exposure-only quantity is the P6 prediction for the unmediated collision rate on "
            "**future, unselected concurrent tasks**. It is not a prediction for the deliberately "
            "conflict-selected arms sites.",
            "",
            "### (b) Site-conditioned context for the 19 selected arms sites",
            "",
        ]
    )
    site_context = hazard["site_conditioned_context"]
    site_rows = []
    for site in site_context["sites"]:
        site_rows.append(
            (
                site["gate"],
                site["repo"],
                f"`{site['merge'][:10]}...`",
                f"{site['parent1_text_lines_changed']:,}+{site['parent2_text_lines_changed']:,}={site['combined_text_lines_changed']:,}",
                "stored" if site["fit_population_eligible"] else "side sum; binary present",
                f"{pct(site['probability'])} {interval_text([site['cluster_ci95_low'], site['cluster_ci95_high']], True)}",
                site["mined_overlap_classification"],
            )
        )
    lines.extend(
        markdown_table(
            ["Gate", "Repository", "Merge", "P1+P2=x lines", "Exposure source", "Fitted h(x), cluster 95%", "Mined byte class"],
            site_rows,
        )
    )
    lines.extend(
        [
            "",
            f"Across the 19 sites, mean fitted exposure hazard = **{pct(site_context['mean_fitted_hazard'])}** "
            f"with repository-bootstrap 95% {interval_text(site_context['mean_fitted_hazard_repository_bootstrap_ci95'], True)}. "
            f"The historical realized rate is **{site_context['historical_conflicts']}/"
            f"{site_context['site_count']} = 100.000%**, or "
            f"{site_context['realized_to_mean_hazard_ratio']:.2f}x the mean fitted exposure hazard. "
            "That gap measures how strongly selecting known historical conflicts concentrates collision "
            "beyond changed-line exposure; it is not a failed forecast, because these outcomes selected the sites.",
            "",
            f"Exposure provenance: {site_context['fit_population_eligible_sites']}/19 sites use the stored "
            "combined countable-text field. For "
            f"{site_context['text_component_binary_present_sites']}/19 sites the miner nulls that field because "
            "a side also changed a binary file; both stored side-specific text totals remain available, so the "
            "table uses their labeled sum. Those hazards are text-component extrapolations outside the fit's "
            "eligibility rule.",
            "",
            "Accordingly, the arms' realized collision rate is preregistered to land **above** the "
            "exposure-only agent-size prediction. For the selected sites the claim is only that ordering. "
            "The numerical ladder prediction remains section 1(a), for the unmediated arm on future "
            "unselected work. No site-specific future collision probability is claimed.",
            "",
            "Hazard caveats: repository clustering does not repair purposive repository selection; only 15 "
            "clusters support small-cluster inference; conflict is Git/textual rather than semantic; changed "
            "lines are historical branch diffs while agent claims are transcript patch-write regions; and "
            "the 1,645 exposure-unavailable rows have a much higher conflict rate.",
            "",
            "## 2. Uniform contiguous-span collision null",
            "",
            "### Exact null and supported inputs",
            "",
            "For measured positive widths `w1,w2` in a base blob of `N` bytes, each contiguous half-open "
            "span start is independently uniform over every integer position where it fits. With "
            "`D=N-w1-w2`, the exact disjoint probability is "
            "`(D+1)(D+2)/((N-w1+1)(N-w2+1))` when `D>=0`, else zero. Each side's multiple refined hunks "
            "and insertion anchors supply a unioned changed-byte mass `w`; the null places one contiguous "
            "span of that size. This is the committed **contiguous-span** null. A bounding-hull sensitivity "
            "is retained separately and counts unchanged gaps. The supplied `exp(-w1*w2/N)` formula instead "
            "describes a scattered-byte reference and is reported without fitting, not used as the null.",
            "",
        ]
    )
    span_inputs = collision["span_inputs"]
    marker = span_inputs["handwritten_marker_spans"]
    lines.append(
        f"The paired null has **{span_inputs['handwritten_primary_pairs']:,}** handwritten, strict-classifiable "
        f"conflict-path pairs across {len(span_inputs['primary_pairs_by_repository'])} repositories; each has "
        "two parent-side base-coordinate span sets and a base blob `N`. Separately, the requested handwritten "
        f"marker-span distribution has **{marker['span_bytes']['count']:,}** measurable result-blob paths: "
        f"median/p90 span = {marker['span_bytes']['p50']:,.0f}/{marker['span_bytes']['p90']:,.0f} bytes, "
        f"median/p90 file size = {marker['result_blob_size_bytes']['p50']:,.0f}/"
        f"{marker['result_blob_size_bytes']['p90']:,.0f} bytes, and median/p90 span fraction = "
        f"{pct(marker['span_fraction']['p50'])}/{pct(marker['span_fraction']['p90'])}. Marker spans do not "
        "contain two side-specific widths, so they are described but not synthetically paired."
    )

    file_prediction = collision["file_granularity_prediction"]
    lines.extend(
        [
            "",
            "### (a) Whole-file over-block prediction versus Click",
            "",
            f"Under the fixed contiguous null, expected exact nonoverlap is **"
            f"{file_prediction['null_expected_nonoverlap_units_over_primary_span_sample']:.3f} / "
            f"{file_prediction['null_primary_span_denominator']:,} = "
            f"{pct(file_prediction['null_expected_nonoverlap_probability'])}**. Whole-file claiming would "
            "serialize every such pair, so this is its null over-block rate.",
            "",
            f"The measured anchored Click census is **{file_prediction['measured_click_nonconflicting_units']} / "
            f"{file_prediction['measured_click_same_file_units']} = "
            f"{pct(file_prediction['measured_click_overblock_rate'])}** textually nonconflicting same-file units "
            f"(the published 69.4%). Measured minus null = "
            f"{100*file_prediction['measured_minus_null_probability']:+.3f} percentage points. The direction "
            "is consistent with edits clustering more than uniform placement, but the magnitude is contextual: "
            "the width sample is conflict-selected and pooled, while 475/684 is Click's full anchored textual census.",
            "",
        ]
    )
    result_sensitivity = file_prediction["result_blob_N_sensitivity"]
    if result_sensitivity["mean_disjoint_probability"] is not None:
        lines.append(
            f"Coordinate-size sensitivity: substituting result-blob size for `N` where both changed-byte masses "
            f"still fit gives {pct(result_sensitivity['mean_disjoint_probability'])} over "
            f"{result_sensitivity['eligible_pairs']:,}/{result_sensitivity['all_pairs']:,} pairs. Base-blob "
            "`N` remains primary because the retained spans are in base coordinates."
        )

    curves = collision["granularity_curves"]
    null_by_label = {row["granularity"]: row for row in curves["pooled_handwritten_contiguous_null"]}
    click_empirical = {row["granularity"]: row for row in curves["click_conflict_selected_empirical_spans"]}
    click_null = {row["granularity"]: row for row in curves["click_conflict_selected_same_cohort_null"]}
    lines.extend(
        [
            "",
            "### (b) Claim-granularity curve",
            "",
            "A `g`-byte empirical claim expands every retained interval/effective insertion byte outward to "
            "the fixed, file-origin-aligned `g`-byte blocks it touches. Over-blocking means the miner's exact "
            "span sets are strict-disjoint but padded claim unions overlap. "
            "Thus exact-span over-block is zero and whole-file over-block equals exact nonoverlap. Null values "
            "are exact counts over all uniform integer-start pairs, not Monte Carlo estimates.",
            "",
        ]
    )
    curve_rows = []
    for label, _ in GRANULARITIES:
        null_row = null_by_label[label]
        empirical_row = click_empirical[label]
        click_null_row = click_null[label]
        curve_rows.append(
            (
                label,
                f"{null_row['expected_overblocked_units']:.3f}/{null_row['denominator_pairs']} ({pct(null_row['overblock_probability'])})",
                f"{empirical_row['overblocked_units']}/{empirical_row['denominator_pairs']} ({pct(empirical_row['overblock_rate'])})",
                f"{click_null_row['expected_overblocked_units']:.3f}/{click_null_row['denominator_pairs']} ({pct(click_null_row['overblock_probability'])})",
            )
        )
    lines.extend(
        markdown_table(
            ["Claim granularity", "Pooled handwritten null", "Click empirical spans", "Same Click-subset null"],
            curve_rows,
        )
    )
    lines.extend(
        [
            "",
            f"The empirical Click curve denominator is **{span_inputs['click_conflict_selected_pairs']}** "
            "strict-classifiable paths from conflicted mined merges. It is a real base-coordinate curve, but "
            "it is not the 684-unit full census: clean-merge census rows retain changed paths and textual "
            "labels, not their base-coordinate spans. That full-census curve therefore cannot be computed "
            "without new mining.",
            "",
            "### (c) Clustering ratios",
            "",
        ]
    )
    full_ratio = collision["clustering_statistics"]["full_click_textual_proxy"]
    paired_ratio = collision["clustering_statistics"]["click_span_supported_miner_strict"]
    hull_ratio = collision["clustering_statistics"]["click_bounding_hull_width_sensitivity"]
    ratio_rows = [
        (
            "Full Click textual proxy",
            f"{full_ratio['observed_overlap_units']}/{full_ratio['observed_same_file_units']} ({pct(full_ratio['observed_overlap_probability'])})",
            f"{full_ratio['null_expected_overlap_units']:.3f}/{full_ratio['null_span_denominator']} ({pct(full_ratio['null_overlap_probability'])})",
            f"{full_ratio['observed_to_null_overlap_ratio']:.3f}x",
        ),
        (
            "Click span-supported mined-strict",
            f"{paired_ratio['observed_overlap_units']}/{paired_ratio['observed_span_pairs']} ({pct(paired_ratio['observed_overlap_probability'])})",
            f"{paired_ratio['null_expected_overlap_units']:.3f}/{paired_ratio['null_span_pairs']} ({pct(paired_ratio['null_overlap_probability'])})",
            f"{paired_ratio['observed_to_null_overlap_ratio']:.3f}x",
        ),
        (
            "Click bounding-hull sensitivity",
            f"{hull_ratio['observed_overlap_units']}/{hull_ratio['observed_span_pairs']} ({pct(hull_ratio['observed_overlap_probability'])})",
            f"{hull_ratio['null_expected_overlap_units']:.3f}/{hull_ratio['null_span_pairs']} ({pct(hull_ratio['null_overlap_probability'])})",
            f"{hull_ratio['observed_to_null_overlap_ratio']:.3f}x",
        ),
    ]
    lines.extend(markdown_table(["Statistic", "Observed overlap", "Null expected overlap", "Observed/null"], ratio_rows))
    lines.extend(
        [
            "",
            "The same-cohort mined-strict ratio is the internally comparable clustering statistic. The full Click "
            "ratio answers the requested paper comparison but mixes a full textual census numerator with a "
            "conflict-selected span distribution; it should be described as contextual. The bounding-hull "
            "sensitivity deliberately counts unchanged gaps as edit width and overlap; the committed null "
            "instead places the measured changed-byte mass as one contiguous span.",
            "",
            f"For reference only, averaging the supplied scattered birthday expression over the primary "
            f"pairs gives disjoint probability {pct(collision['birthday_reference']['pooled_handwritten_mean_disjoint_probability'])}; "
            "it is not substituted for the contiguous result. The null was not tuned to 69.4%. Its "
            "failure--and especially the same-cohort observed/null excess--is the clustering finding.",
            "",
            "## 3. Quantile lease rule",
            "",
            "### Objective, inputs, and default costs",
            "",
        ]
    )
    lease_inputs = lease["inputs"]
    costs = lease["costs"]
    optimum = lease["optimum"]
    lines.append(
        "The fixed rule is `P(T>L)*C_reacquire + E[min(L,D)]*C_block`. `T` is the "
        f"{lease_inputs['read_to_write']['count']}-pair first-read-result to absolute-first-write interval: "
        f"p50/p90/p99/max = {lease_inputs['read_to_write']['p50']:.3f}/"
        f"{lease_inputs['read_to_write']['p90']:.3f}/{lease_inputs['read_to_write']['p99']:.3f}/"
        f"{lease_inputs['read_to_write']['max']:.3f} seconds. `D` is the "
        f"{lease_inputs['linger']['count']:,}-claim last-write-result to observed-end interval: "
        f"p50/p90/p99/max = {lease_inputs['linger']['p50']:.3f}/"
        f"{lease_inputs['linger']['p90']:.3f}/{lease_inputs['linger']['p99']:.3f}/"
        f"{lease_inputs['linger']['max']:.3f} seconds. `E[min(L,D)]` assumes an explicit release at observed "
        "end and otherwise expiry `L` after last use."
    )
    lines.extend(
        [
            "",
            f"Default `C_block` = {costs['blocking_cost_agent_minutes_per_waiting_minute']:.3f} agent-minute "
            "per waiting minute. No launch-overhead measurement exists, so default `C_reacquire` = "
            f"{costs['reacquisition_cost_agent_minutes']:.4f} agent-minutes, the measured p50 structured "
            f"active span {lease_inputs['reacquisition_proxy']['p50']:.3f} seconds divided by 60. This is a "
            "workload-cost proxy, not a startup benchmark.",
            "",
            f"On the declared quantile reconstruction, **L* = {optimum['lease_minutes']:.6f} minutes "
            f"({60*optimum['lease_minutes']:.3f} seconds)**. At L*, false expiry = "
            f"{pct(optimum['false_expiry'])}, expected dangling = "
            f"{optimum['expected_dangling_minutes']:.6f} minutes, and objective = "
            f"{optimum['objective_agent_minutes_per_claim']:.6f} agent-minutes per claim.",
            "",
            "This is **provisional, not an exact empirical optimum**. `parameters.json` contains only the "
            "nearest-rank summaries, so the script reconstructs piecewise-linear quantile functions through "
            "minimum zero, p50, p90, p99, and maximum. The four summary-consistent low/high scenarios put "
            f"L* between {lease['scenario_lease_minimum_minutes']:.6f} and "
            f"{lease['scenario_lease_maximum_minutes']:.6f} minutes under the default costs; that scenario "
            "range is not claimed to be a sharp identification set.",
            "",
            "### Default objective curve",
            "",
        ]
    )
    lease_curve_rows = []
    for row in lease["curve"]:
        lease_curve_rows.append(
            (
                f"{row['lease_minutes']:.6f}",
                pct(row["false_expiry"]),
                f"{row['expected_dangling_minutes']:.6f}",
                f"{row['false_expiry_cost']:.6f}",
                f"{row['blocking_cost']:.6f}",
                f"{row['objective_agent_minutes_per_claim']:.6f}",
            )
        )
    lines.extend(
        markdown_table(
            ["L (min)", "FalseExpiry", "E dangling (min)", "Reacquire term", "Blocking term", "Objective"],
            lease_curve_rows,
        )
    )
    lines.extend(["", "### Cost-ratio sensitivity (1:10 to 10:1)", ""])
    selected = selected_sensitivity_rows(lease["cost_ratio_sensitivity"], costs["numeric_default_ratio"])
    sensitivity_rows = []
    for row in selected:
        ratio = row["numeric_cost_ratio_reacquisition_to_blocking"]
        label = f"{ratio:.4f}:1"
        if math.isclose(ratio, costs["numeric_default_ratio"], rel_tol=1e-12, abs_tol=1e-12):
            label += " (default)"
        sensitivity_rows.append(
            (
                label,
                f"{row['lease_minutes']:.6f}",
                pct(row["false_expiry"]),
                f"{row['expected_dangling_minutes']:.6f}",
                f"{row['objective_agent_minutes_per_claim']:.6f}",
            )
        )
    lines.extend(
        markdown_table(
            ["Numeric C_reacquire:C_block", "L* (min)", "FalseExpiry", "E dangling (min)", "Objective"],
            sensitivity_rows,
        )
    )
    lines.extend(
        [
            "",
            "The machine-readable file contains the full 41-point log-spaced sensitivity plus the measured "
            "default. Numeric ratios compare agent-minutes per false expiry with agent-minutes per dangling "
            "minute, so the ratio has an implicit minute scale; it is a design tradeoff, not a universal constant.",
            "",
            "Lease caveats: read does not establish causal need for the later write; session end is not an "
            "explicit close and the linger tail is right-censored; no launch-overhead benchmark was retained; "
            "ties inside percentile summaries are unknown; and online renewal/heartbeat behavior is outside "
            "this static objective.",
            "",
            "## Claims that could NOT be verified",
            "",
            "- The exact empirical `E[h(W1+W2)]`: the 1,354 task sizes or a lossless histogram are absent from `parameters.json`.",
            "- The exact empirical lease curve or L*: the 405 read-to-write and 3,094 linger observations are absent; only five order-statistic constraints per distribution remain.",
            "- Actual agent relaunch overhead: p50 structured active span is only a workload-cost proxy.",
            "- An uncensored last-use-to-close distribution: observed session end is not a close event.",
            "- A 64B-to-4KB empirical curve on all 684 Click same-file census units: clean-census rows do not retain base-coordinate spans.",
            "- A common-population estimate that directly compares the pooled conflict-selected span null with the complete Click census.",
            "- Side-specific widths from the 845 handwritten marker spans: marker regions measure disputed result text, not the two parent edits.",
            "- Semantic conflict probability, causal effects of exposure, or generalization beyond these selected histories and one transcript workload.",
            "",
            "## What would change this verdict",
            "",
            "- Exporting a lossless histogram (or the 1,354 values) for aggregate claim lines would replace the distributional hazard range/reconstruction with the exact push-through mean.",
            "- Exporting the 405 and 3,094 interval values plus explicit close/crash/heartbeat events would identify the lease objective and remove the quantile reconstruction.",
            "- Measuring agent startup/relaunch latency directly would replace the structured-active-span cost proxy and could move L* materially.",
            "- Retaining base-coordinate interval sets and blob sizes for every Click same-file census unit would produce the requested full empirical granularity curve and a common-cohort null ratio.",
            "- More independently sampled repositories or a future unselected-agent task cohort could materially move the hazard curve and its transfer calibration.",
            "- A different declared null--scattered hunks, nonuniform hot regions, syntax-aware placement, or file-type conditioning--would change the reference. It must be preregistered, not tuned to 69.4%.",
            "- If future selected-site outcomes do not exceed the exposure-only agent-size prediction, the preregistered ordering claim would fail.",
            "",
            "## Per-claim confidence",
            "",
        ]
    )
    confidence_rows = [
        (
            "23,428-row logistic hazard parameters and MINING-bin reconciliation",
            "High for this frozen corpus",
            "Every evaluable JSONL row is streamed; counts are asserted against 238/23,428 and all seven published bins; model and seed were locked before fit.",
        ),
        (
            "Repository-cluster uncertainty",
            "Moderate",
            "CR1 and repository bootstrap agree/disagree visibly, but only 15 clusters exist and repositories were purposively selected.",
        ),
        (
            "p50 and p90 exposure-only predictions",
            "Moderate",
            "Inputs 9 and 56 lines are retained exactly and the curve is reproducible; cross-population hazard transfer remains an untested hypothesis.",
        ),
        (
            "Distributional exposure-only point",
            "Low as a point; high for stated bounds",
            "The point uses an explicit quantile reconstruction. Bounds follow the retained nearest-rank constraints and monotonicity, but depend on minimum-one-line support.",
        ),
        (
            "Nineteen site exposures, hazards, and 100% historical realization",
            "High descriptively",
            "All 19 full identities join uniquely and all rows are conflicted; 17 use the stored combined field and 2 use a labeled side-text sum because binary changes null the fitted-population field; selection prevents predictive interpretation.",
        ),
        (
            "Contiguous null formulas and granularity probabilities",
            "High mathematically; moderate for input representativeness",
            "Closed-form and aligned-block counts are exhaustively unit-tested on small files; measured widths come only from classifiable conflict-selected paths.",
        ),
        (
            "Click conflict-selected empirical granularity curve",
            "High for its retained denominator",
            "Actual base-coordinate interval/anchor sets are used with a deterministic block rule; it excludes census units without retained spans.",
        ),
        (
            "Clustering magnitude against the full Click census",
            "Low-to-moderate",
            "Direction is informative, but the observed textual census and null width distribution are different cohorts; the same-span-subset ratio is stronger internally.",
        ),
        (
            "Lease L*",
            "Low-to-moderate provisional",
            "Objective and costs are explicit, but both distributions are reconstructed, linger is censored, and relaunch cost is proxied.",
        ),
    ]
    lines.extend(markdown_table(["Claim", "Confidence", "Reason"], confidence_rows))
    lines.extend(
        [
            "",
            "## Reproduction",
            "",
            "Run `python instruments/models/compute_models.py --root . --bootstrap 2000`, "
            "`python -m unittest instruments.models.test_model_lib -v`, and "
            "`python instruments/models/verify_models.py`. The JSON records SHA-256, byte size, "
            "and row counts for the fitted merge inputs, plus hashes for the other load-bearing artifacts.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--bootstrap", type=int, default=2000)
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    args = parser.parse_args()

    root = args.root.resolve()
    json_output = (args.json_output or root / "exploratory" / "models" / "hazard.json").resolve()
    markdown_output = (args.markdown_output or root / "exploratory" / "models" / "HAZARD.md").resolve()
    if args.bootstrap < 1:
        parser.error("--bootstrap must be positive")

    parameters_path = root / "exploratory" / "build-params" / "parameters.json"
    parameters = json.loads(parameters_path.read_text(encoding="utf-8"))
    conflict_index, conflict_files = load_conflict_index(root)

    hazard = build_hazard_analysis(root, parameters, conflict_index, args.bootstrap)
    collision = build_collision_analysis(root, conflict_index)
    lease = build_lease_analysis(parameters)
    other_sources = [
        parameters_path,
        root / "exploratory" / "arms" / "sites.json",
        root / "exploratory" / "conflicts" / "MINING.md",
        root / "exploratory" / "conflicts" / "REANALYSIS.md",
        root / "exploratory" / "conflicts" / "SEMANTIC.md",
    ]
    result = finite_float(
        {
            "schema_version": SCHEMA_VERSION,
            "analysis_contract": {
                "path": "instruments/models/MODEL_SPEC.md",
                "sha256": sha256_file(root / "instruments" / "models" / "MODEL_SPEC.md"),
                "model_choice_locked_before_fit": True,
                "no_new_mining": True,
                "network_used": False,
                "agents_used": False,
            },
            "hazard": hazard,
            "collision_null": collision,
            "lease": lease,
            "provenance": {
                "conflict_row_files": conflict_files,
                "other_load_bearing_sources": [source_record(path, root) for path in other_sources],
            },
        }
    )
    markdown = render_markdown(result)
    json_output.parent.mkdir(parents=True, exist_ok=True)
    markdown_output.parent.mkdir(parents=True, exist_ok=True)
    json_output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    markdown_output.write_text(markdown, encoding="utf-8")

    print(
        json.dumps(
            {
                "json": str(json_output),
                "markdown": str(markdown_output),
                "hazard_rows": result["hazard"]["population"]["countable_text_merges"],
                "hazard_conflicts": result["hazard"]["population"]["countable_text_conflicts"],
                "span_pairs": result["collision_null"]["span_inputs"]["handwritten_primary_pairs"],
                "bootstrap_success": result["hazard"]["fit"]["repository_bootstrap"]["successful_draws"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
