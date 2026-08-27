"""Pure helpers for the preregistration models.

The functions in this module deliberately avoid Git, network access, and any
corpus mining.  They operate only on already-mined rows and retained summaries.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Iterable, Iterator, Sequence

import numpy as np
from scipy.optimize import minimize
from scipy.special import expit


HAZARD_MODEL_FORMULA = "logit(p) = alpha + beta * log(1 + combined_text_lines_changed); beta >= 0"
HAZARD_BOOTSTRAP_SEED = 20260825


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def source_record(path: Path, root: Path) -> dict[str, Any]:
    return {
        "path": path.relative_to(root).as_posix(),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON at {path}:{line_number}: {exc}") from exc


@dataclass(frozen=True)
class HazardRows:
    exposure: np.ndarray
    outcome: np.ndarray
    repos: np.ndarray
    merges: tuple[str, ...]
    files: tuple[dict[str, Any], ...]
    all_evaluable: int
    unavailable_exposure: int
    unavailable_conflicts: int


def load_hazard_rows(root: Path) -> HazardRows:
    exposures: list[int] = []
    outcomes: list[int] = []
    repos: list[str] = []
    merges: list[str] = []
    files: list[dict[str, Any]] = []
    all_evaluable = 0
    unavailable_exposure = 0
    unavailable_conflicts = 0

    source_dir = root / "corpus" / "conflicts" / "_all_merges"
    for path in sorted(source_dir.glob("*.jsonl"), key=lambda p: p.name.casefold()):
        row_count = 0
        evaluable_count = 0
        available_count = 0
        conflicted_count = 0
        for row in iter_jsonl(path):
            row_count += 1
            status = row.get("evaluation_status")
            if status not in {"clean", "conflicted"}:
                continue
            evaluable_count += 1
            all_evaluable += 1
            conflicted = bool(row.get("conflicted"))
            if conflicted != (status == "conflicted"):
                raise AssertionError(f"status/outcome mismatch in {path}: {row.get('merge')}")
            exposure = (row.get("divergence") or {}).get("combined_text_lines_changed")
            if exposure is None:
                unavailable_exposure += 1
                unavailable_conflicts += int(conflicted)
                continue
            exposure = int(exposure)
            if exposure < 0:
                raise AssertionError(f"negative exposure in {path}: {row.get('merge')}")
            available_count += 1
            conflicted_count += int(conflicted)
            exposures.append(exposure)
            outcomes.append(int(conflicted))
            repos.append(str(row["repo"]))
            merges.append(str(row["merge"]))
        record = source_record(path, root)
        record.update(
            {
                "rows": row_count,
                "evaluable": evaluable_count,
                "countable_text_rows": available_count,
                "countable_text_conflicts": conflicted_count,
            }
        )
        files.append(record)

    return HazardRows(
        exposure=np.asarray(exposures, dtype=np.float64),
        outcome=np.asarray(outcomes, dtype=np.float64),
        repos=np.asarray(repos, dtype=object),
        merges=tuple(merges),
        files=tuple(files),
        all_evaluable=all_evaluable,
        unavailable_exposure=unavailable_exposure,
        unavailable_conflicts=unavailable_conflicts,
    )


def _logistic_objective(
    theta: np.ndarray,
    design: np.ndarray,
    outcome: np.ndarray,
    weights: np.ndarray,
) -> tuple[float, np.ndarray]:
    eta = design @ theta
    probability = expit(eta)
    loss = float(np.sum(weights * (np.logaddexp(0.0, eta) - outcome * eta)))
    gradient = design.T @ (weights * (probability - outcome))
    return loss, gradient


def fit_logistic(
    exposure: np.ndarray,
    outcome: np.ndarray,
    weights: np.ndarray | None = None,
    start: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray, bool, str]:
    z = np.log1p(exposure.astype(np.float64))
    design = np.column_stack((np.ones_like(z), z))
    if weights is None:
        weights = np.ones_like(outcome, dtype=np.float64)
    else:
        weights = weights.astype(np.float64)
    weighted_mean = float(np.sum(weights * outcome) / np.sum(weights))
    weighted_mean = min(max(weighted_mean, 1e-8), 1 - 1e-8)
    if start is None:
        start = np.asarray([math.log(weighted_mean / (1 - weighted_mean)), 0.5])

    def objective(theta: np.ndarray) -> float:
        return _logistic_objective(theta, design, outcome, weights)[0]

    def gradient(theta: np.ndarray) -> np.ndarray:
        return _logistic_objective(theta, design, outcome, weights)[1]

    result = minimize(
        objective,
        np.asarray(start, dtype=np.float64),
        jac=gradient,
        method="L-BFGS-B",
        bounds=((None, None), (0.0, None)),
        options={"ftol": 1e-14, "gtol": 1e-10, "maxiter": 500},
    )
    if not result.success or not np.all(np.isfinite(result.x)):
        first_message = str(result.message)
        result = minimize(
            objective,
            np.asarray(result.x if np.all(np.isfinite(result.x)) else start, dtype=np.float64),
            jac=gradient,
            method="SLSQP",
            bounds=((None, None), (0.0, None)),
            options={"ftol": 1e-12, "maxiter": 1000},
        )
        result.message = f"fallback SLSQP after L-BFGS-B ({first_message}); {result.message}"
    theta = np.asarray(result.x, dtype=np.float64)
    eta = design @ theta
    probability = expit(eta)
    information = design.T @ ((weights * probability * (1 - probability))[:, None] * design)
    information_inverse = np.linalg.pinv(information)
    return theta, information_inverse, bool(result.success), str(result.message)


def cluster_robust_covariance(
    theta: np.ndarray,
    exposure: np.ndarray,
    outcome: np.ndarray,
    clusters: np.ndarray,
) -> tuple[np.ndarray, dict[str, Any]]:
    z = np.log1p(exposure.astype(np.float64))
    design = np.column_stack((np.ones_like(z), z))
    probability = expit(design @ theta)
    information = design.T @ ((probability * (1 - probability))[:, None] * design)
    bread = np.linalg.pinv(information)
    meat = np.zeros((2, 2), dtype=np.float64)
    unique_clusters = sorted(set(str(value) for value in clusters))
    for cluster in unique_clusters:
        mask = clusters == cluster
        score = design[mask].T @ (outcome[mask] - probability[mask])
        meat += np.outer(score, score)
    n = len(outcome)
    k = design.shape[1]
    g = len(unique_clusters)
    correction = (g / (g - 1)) * ((n - 1) / (n - k)) if g > 1 and n > k else 1.0
    covariance = correction * (bread @ meat @ bread)
    return covariance, {
        "clusters": g,
        "observations": n,
        "parameters": k,
        "cr1_correction": correction,
    }


def hazard_probability(theta: Sequence[float], exposure: Any) -> np.ndarray:
    values = np.asarray(exposure, dtype=np.float64)
    return expit(float(theta[0]) + float(theta[1]) * np.log1p(values))


def hazard_point(
    theta: np.ndarray,
    covariance: np.ndarray,
    exposure: float,
    z_value: float = 1.959963984540054,
) -> dict[str, float]:
    design = np.asarray([1.0, math.log1p(float(exposure))])
    eta = float(design @ theta)
    standard_error_eta = float(math.sqrt(max(0.0, design @ covariance @ design)))
    return {
        "exposure_lines": float(exposure),
        "probability": float(expit(eta)),
        "cluster_logit_se": standard_error_eta,
        "cluster_ci95_low": float(expit(eta - z_value * standard_error_eta)),
        "cluster_ci95_high": float(expit(eta + z_value * standard_error_eta)),
    }


def repository_bootstrap(
    rows: HazardRows,
    theta: np.ndarray,
    draws: int,
    seed: int = HAZARD_BOOTSTRAP_SEED,
) -> tuple[np.ndarray, dict[str, Any]]:
    unique_repos = np.asarray(sorted(set(str(value) for value in rows.repos)), dtype=object)
    repo_index = {repo: index for index, repo in enumerate(unique_repos)}
    row_repo_index = np.asarray([repo_index[str(repo)] for repo in rows.repos], dtype=np.int64)
    rng = np.random.default_rng(seed)
    estimates: list[np.ndarray] = []
    failures = 0
    for _ in range(draws):
        sampled = rng.integers(0, len(unique_repos), size=len(unique_repos))
        multiplicity = np.bincount(sampled, minlength=len(unique_repos)).astype(np.float64)
        weights = multiplicity[row_repo_index]
        if float(np.sum(weights * rows.outcome)) == 0.0:
            failures += 1
            continue
        estimate, _, success, _ = fit_logistic(
            rows.exposure,
            rows.outcome,
            weights=weights,
            start=theta,
        )
        if not success or not np.all(np.isfinite(estimate)):
            failures += 1
            continue
        estimates.append(estimate)
    if not estimates:
        raise RuntimeError("every repository bootstrap fit failed")
    return np.vstack(estimates), {
        "requested_draws": draws,
        "successful_draws": len(estimates),
        "failed_draws": failures,
        "seed": seed,
        "resampling_unit": "repository",
        "repositories": len(unique_repos),
    }


MINING_BINS: tuple[tuple[str, int, int | None], ...] = (
    ("0", 0, 0),
    ("1-15", 1, 15),
    ("16-63", 16, 63),
    ("64-255", 64, 255),
    ("256-1,023", 256, 1023),
    ("1,024-4,095", 1024, 4095),
    ("4,096+", 4096, None),
)


def hazard_bin_table(rows: HazardRows, theta: np.ndarray, covariance: np.ndarray) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for label, lower, upper in MINING_BINS:
        mask = rows.exposure >= lower
        if upper is not None:
            mask &= rows.exposure <= upper
        observed = rows.exposure[mask]
        outcomes = rows.outcome[mask]
        if not len(observed):
            continue
        midpoint = (float(np.min(observed)) + float(np.max(observed))) / 2.0
        point = hazard_point(theta, covariance, midpoint)
        point.update(
            {
                "bin": label,
                "lower": lower,
                "upper": upper,
                "observed_min": int(np.min(observed)),
                "observed_max": int(np.max(observed)),
                "observed_midpoint": midpoint,
                "evaluable": int(len(outcomes)),
                "conflicted": int(np.sum(outcomes)),
                "observed_rate": float(np.mean(outcomes)),
                "mean_fitted_probability_in_bin": float(np.mean(hazard_probability(theta, observed))),
                "contributing_repositories": len(set(str(value) for value in rows.repos[mask])),
            }
        )
        result.append(point)
    return result


def nearest_rank(p: float, n: int) -> int:
    return int(math.ceil(p * n))


def order_stat_extremes(
    n: int,
    p50: float,
    p90: float,
    p99: float,
    maximum: float,
    minimum: float,
) -> tuple[np.ndarray, np.ndarray]:
    ranks = [nearest_rank(p, n) for p in (0.50, 0.90, 0.99)]
    values = [float(p50), float(p90), float(p99), float(maximum)]
    if not (minimum <= values[0] <= values[1] <= values[2] <= values[3]):
        raise ValueError("order-statistic values are not monotone")
    r50, r90, r99 = ranks

    low = np.full(n, float(minimum), dtype=np.float64)
    low[r50 - 1 : r90 - 1] = p50
    low[r90 - 1 : r99 - 1] = p90
    low[r99 - 1 : n - 1] = p99
    low[n - 1] = maximum

    high = np.empty(n, dtype=np.float64)
    high[:r50] = p50
    high[r50:r90] = p90
    high[r90:r99] = p99
    high[r99:] = maximum
    return low, high


def quantile_reconstruction(
    n: int,
    p50: float,
    p90: float,
    p99: float,
    maximum: float,
    minimum: float,
    integer: bool = False,
) -> np.ndarray:
    r50, r90, r99 = [nearest_rank(p, n) for p in (0.50, 0.90, 0.99)]
    result = np.empty(n, dtype=np.float64)
    segments = (
        (0, r50, minimum, p50),
        (r50, r90, p50, p90),
        (r90, r99, p90, p99),
        (r99, n, p99, maximum),
    )
    for start, stop, left, right in segments:
        length = stop - start
        if length <= 0:
            continue
        result[start:stop] = np.linspace(float(left), float(right), length, endpoint=True)
    # Force the reported nearest-rank values and the maximum exactly.
    result[r50 - 1] = p50
    result[r90 - 1] = p90
    result[r99 - 1] = p99
    result[-1] = maximum
    result = np.maximum.accumulate(result)
    if integer:
        result = np.floor(result + 0.5)
        result[r50 - 1] = p50
        result[r90 - 1] = p90
        result[r99 - 1] = p99
        result[-1] = maximum
        result = np.maximum.accumulate(result)
    return result


def pushed_pair_prediction(theta: Sequence[float], values: np.ndarray) -> float:
    if np.any(values < 0):
        raise ValueError("task sizes must be nonnegative")
    if np.all(np.equal(values, np.floor(values))) and float(np.max(values)) <= 1_000_000:
        integer_values = values.astype(np.int64)
        counts = np.bincount(integer_values)
        pmf = counts / float(len(integer_values))
        pair_pmf = np.convolve(pmf, pmf)
        exposures = np.arange(len(pair_pmf), dtype=np.float64)
        return float(np.sum(pair_pmf * hazard_probability(theta, exposures)))
    left = values[:, None]
    return float(np.mean(hazard_probability(theta, left + values[None, :])))


SITE_ROW = re.compile(
    r"^\| (?P<gate>Python validated|Go runner-eligible|Java gate passed) "
    r"\| `(?P<repo>[^`]+)` \| `(?P<merge>[0-9a-f]{40})` \|"
)


def parse_site_identities(path: Path) -> list[dict[str, str]]:
    identities: list[dict[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = SITE_ROW.match(line)
        if match:
            identities.append(match.groupdict())
    if len(identities) != 19:
        raise AssertionError(f"expected 19 site identities in {path}, found {len(identities)}")
    if len({(item['repo'], item['merge']) for item in identities}) != 19:
        raise AssertionError("site identities are not unique")
    return identities


def load_conflict_index(root: Path) -> tuple[dict[tuple[str, str], dict[str, Any]], list[dict[str, Any]]]:
    index: dict[tuple[str, str], dict[str, Any]] = {}
    file_records: list[dict[str, Any]] = []
    source_dir = root / "corpus" / "conflicts"
    for path in sorted(source_dir.glob("*.jsonl"), key=lambda p: p.name.casefold()):
        count = 0
        for row in iter_jsonl(path):
            count += 1
            key = (str(row["repo"]), str(row["merge"]))
            if key in index:
                raise AssertionError(f"duplicate conflict identity: {key}")
            index[key] = row
        record = source_record(path, root)
        record["rows"] = count
        file_records.append(record)
    return index, file_records


def site_hazards(
    root: Path,
    conflict_index: dict[tuple[str, str], dict[str, Any]],
    theta: np.ndarray,
    covariance: np.ndarray,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    reanalysis = root / "exploratory" / "conflicts" / "REANALYSIS.md"
    identities = parse_site_identities(reanalysis)
    results: list[dict[str, Any]] = []
    for identity in identities:
        key = (identity["repo"], identity["merge"])
        row = conflict_index.get(key)
        if row is None:
            raise AssertionError(f"site does not join to a conflict row: {key}")
        side1 = (row.get("diffs") or {}).get("parent1", {}).get("text_lines_changed")
        side2 = (row.get("diffs") or {}).get("parent2", {}).get("text_lines_changed")
        stored_exposure = (row.get("divergence") or {}).get("combined_text_lines_changed")
        if side1 is None or side2 is None:
            raise AssertionError(f"site lacks side-specific text lines: {key}")
        if stored_exposure is None:
            exposure = int(side1) + int(side2)
            exposure_source = "derived_text_component_binary_present"
        else:
            exposure = int(stored_exposure)
            exposure_source = "stored_combined_text_lines_changed"
        if stored_exposure is not None and int(side1) + int(side2) != int(exposure):
            raise AssertionError(f"site side-line sum mismatch: {key}")
        if not bool(row.get("conflicted")):
            raise AssertionError(f"selected site was not conflicted: {key}")
        point = hazard_point(theta, covariance, float(exposure))
        point.update(
            {
                "gate": identity["gate"],
                "repo": identity["repo"],
                "merge": identity["merge"],
                "parent1_text_lines_changed": int(side1),
                "parent2_text_lines_changed": int(side2),
                "combined_text_lines_changed": int(exposure),
                "combined_exposure_source": exposure_source,
                "fit_population_eligible": stored_exposure is not None,
                "binary_files_parent1": int((row.get("diffs") or {}).get("parent1", {}).get("binary_files") or 0),
                "binary_files_parent2": int((row.get("diffs") or {}).get("parent2", {}).get("binary_files") or 0),
                "historical_conflicted": True,
                "mined_overlap_classification": (row.get("overlap") or {}).get("classification"),
            }
        )
        results.append(point)

    manifest_check: dict[str, Any] = {"status": "not_checked"}
    sites_path = root / "exploratory" / "arms" / "sites.json"
    try:
        manifest = json.loads(sites_path.read_text(encoding="utf-8"))
        validated: set[tuple[str, str]] = set()
        for site in manifest.get("sites", []):
            if site.get("verdict") != "VALIDATED":
                continue
            repo = site.get("repository") or site.get("repo")
            merge = site.get("merge") or site.get("commit")
            if repo and merge:
                validated.add((str(repo), str(merge)))
        expected_python = {
            (item["repo"], item["merge"])
            for item in identities
            if item["gate"] == "Python validated"
        }
        manifest_check = {
            "status": "matched" if validated == expected_python else "mismatch",
            "validated_manifest_identities": len(validated),
            "reanalyzed_python_identities": len(expected_python),
            "missing_from_manifest": sorted(expected_python - validated),
            "extra_in_manifest": sorted(validated - expected_python),
        }
    except Exception as exc:  # report a validation gap without changing the join source
        manifest_check = {"status": "error", "error": str(exc)}
    return results, manifest_check


def union_length(intervals: Iterable[Sequence[int]]) -> int:
    cleaned = sorted((int(start), int(end)) for start, end in intervals if int(end) > int(start))
    if not cleaned:
        return 0
    total = 0
    current_start, current_end = cleaned[0]
    for start, end in cleaned[1:]:
        if start <= current_end:
            current_end = max(current_end, end)
        else:
            total += current_end - current_start
            current_start, current_end = start, end
    return total + current_end - current_start


def edit_pieces(side: dict[str, Any], file_size: int) -> tuple[tuple[int, int], ...]:
    pieces: list[tuple[int, int]] = []
    for interval in side.get("intervals") or []:
        if not isinstance(interval, (list, tuple)) or len(interval) != 2:
            continue
        start = max(0, min(file_size, int(interval[0])))
        end = max(0, min(file_size, int(interval[1])))
        if end > start:
            pieces.append((start, end))
    for raw_anchor in side.get("anchors") or []:
        anchor = max(0, min(file_size, int(raw_anchor)))
        if file_size <= 0:
            continue
        if anchor == file_size:
            pieces.append((file_size - 1, file_size))
        else:
            pieces.append((anchor, anchor + 1))
    if not pieces:
        return ()
    pieces.sort()
    merged: list[tuple[int, int]] = [pieces[0]]
    for start, end in pieces[1:]:
        previous_start, previous_end = merged[-1]
        if start <= previous_end:
            merged[-1] = (previous_start, max(previous_end, end))
        else:
            merged.append((start, end))
    return tuple(merged)


def edit_hull(side: dict[str, Any], file_size: int) -> tuple[int, int] | None:
    pieces = edit_pieces(side, file_size)
    if not pieces:
        return None
    return pieces[0][0], pieces[-1][1]


@dataclass(frozen=True)
class SpanPair:
    repo: str
    merge: str
    path: str
    file_size: int
    result_blob_size: int | None
    start1: int
    end1: int
    start2: int
    end2: int
    pieces1: tuple[tuple[int, int], ...]
    pieces2: tuple[tuple[int, int], ...]
    strict_overlap: bool
    boundary_contact: bool
    path_classification: str

    @property
    def width1(self) -> int:
        return sum(end - start for start, end in self.pieces1)

    @property
    def width2(self) -> int:
        return sum(end - start for start, end in self.pieces2)

    @property
    def hull_width1(self) -> int:
        return self.end1 - self.start1

    @property
    def hull_width2(self) -> int:
        return self.end2 - self.start2

    @property
    def hull_overlap(self) -> bool:
        return max(self.start1, self.start2) < min(self.end1, self.end2)


def extract_span_pairs(
    conflict_index: dict[tuple[str, str], dict[str, Any]],
) -> tuple[list[SpanPair], list[dict[str, Any]], dict[str, int]]:
    pairs: list[SpanPair] = []
    marker_spans: list[dict[str, Any]] = []
    exclusions: Counter[str] = Counter()

    for (repo, merge), row in conflict_index.items():
        conflict_by_path = {item.get("path"): item for item in row.get("conflicts") or []}
        for conflict in row.get("conflicts") or []:
            classification = (conflict.get("classification") or {}).get("kind")
            if classification != "handwritten":
                continue
            if conflict.get("range_status") != "measured_text_markers":
                continue
            result_blob = conflict.get("result_blob") or {}
            file_size = result_blob.get("size")
            regions = conflict.get("regions") or []
            if not file_size or not regions:
                continue
            intervals = [(int(region["byte_start"]), int(region["byte_end"])) for region in regions]
            marker_spans.append(
                {
                    "repo": repo,
                    "merge": merge,
                    "path": str(conflict.get("path")),
                    "span_bytes": union_length(intervals),
                    "result_blob_size": int(file_size),
                }
            )

        overlap = row.get("overlap") or {}
        for path_row in overlap.get("paths") or []:
            if path_row.get("status") != "classifiable":
                exclusions[f"status:{path_row.get('status')}"] += 1
                continue
            base_size = path_row.get("base_blob_size")
            if base_size is None or int(base_size) <= 0:
                exclusions["missing_or_nonpositive_base_blob_size"] += 1
                continue
            base_size = int(base_size)
            pieces1 = edit_pieces(path_row.get("parent1") or {}, base_size)
            pieces2 = edit_pieces(path_row.get("parent2") or {}, base_size)
            if not pieces1 or not pieces2:
                exclusions["side_without_positive_effective_hull"] += 1
                continue
            hull1 = (pieces1[0][0], pieces1[-1][1])
            hull2 = (pieces2[0][0], pieces2[-1][1])
            conflict = conflict_by_path.get(path_row.get("path")) or {}
            path_classification = str((conflict.get("classification") or {}).get("kind") or "unknown")
            result_size = (conflict.get("result_blob") or {}).get("size")
            pairs.append(
                SpanPair(
                    repo=repo,
                    merge=merge,
                    path=str(path_row.get("path")),
                    file_size=base_size,
                    result_blob_size=int(result_size) if result_size is not None else None,
                    start1=hull1[0],
                    end1=hull1[1],
                    start2=hull2[0],
                    end2=hull2[1],
                    pieces1=pieces1,
                    pieces2=pieces2,
                    strict_overlap=bool(path_row.get("strict_overlap")),
                    boundary_contact=bool(path_row.get("boundary_contact")),
                    path_classification=path_classification,
                )
            )
    return pairs, marker_spans, dict(sorted(exclusions.items()))


def contiguous_disjoint_probability(width1: int, width2: int, file_size: int) -> float:
    width1 = int(width1)
    width2 = int(width2)
    file_size = int(file_size)
    if file_size <= 0 or width1 <= 0 or width2 <= 0 or width1 > file_size or width2 > file_size:
        raise ValueError("positive widths must fit in the positive file size")
    slack = file_size - width1 - width2
    if slack < 0:
        return 0.0
    numerator = (slack + 1) * (slack + 2)
    denominator = (file_size - width1 + 1) * (file_size - width2 + 1)
    return numerator / denominator


def scattered_birthday_disjoint_probability(width1: int, width2: int, file_size: int) -> float:
    return math.exp(-float(width1) * float(width2) / float(file_size))


def _complement_mod_prefix(n: int, granularity: int) -> int:
    """Sum (-t mod g) for integer t in [0,n)."""
    if n <= 0:
        return 0
    cycles, remainder = divmod(n, granularity)
    cycle_sum = granularity * (granularity - 1) // 2
    partial = 0
    if remainder > 1:
        count = remainder - 1
        partial = count * granularity - count * (count + 1) // 2
    return cycles * cycle_sum + partial


def _oriented_aligned_overblock_count(
    left_width: int,
    right_width: int,
    file_size: int,
    granularity: int,
) -> int:
    # t is the exact end of the left edit; the right edit starts at or after t.
    first_t = left_width
    last_t = file_size - right_width
    if first_t > last_t:
        return 0
    # Away from the right file boundary the count is (-t mod g). Only the final
    # g-1 end positions can be clipped by the right edit's start support.
    regular_last = min(last_t, last_t - granularity + 1)
    total = 0
    if first_t <= regular_last:
        total += _complement_mod_prefix(regular_last + 1, granularity)
        total -= _complement_mod_prefix(first_t, granularity)
    tail_start = max(first_t, regular_last + 1)
    upper_exclusive = last_t + 1
    for t in range(tail_start, upper_exclusive):
        distance_to_support_end = last_t - t + 1
        padding_room = (-t) % granularity
        total += min(distance_to_support_end, padding_room)
    return int(total)


def aligned_overblock_probability(width1: int, width2: int, file_size: int, granularity: int) -> float:
    if granularity <= 0:
        raise ValueError("granularity must be positive")
    if width1 <= 0 or width2 <= 0 or width1 > file_size or width2 > file_size:
        raise ValueError("positive widths must fit in file")
    numerator = _oriented_aligned_overblock_count(width1, width2, file_size, granularity)
    numerator += _oriented_aligned_overblock_count(width2, width1, file_size, granularity)
    denominator = (file_size - width1 + 1) * (file_size - width2 + 1)
    return numerator / denominator


def aligned_claim(start: int, end: int, file_size: int, granularity: int) -> tuple[int, int]:
    claim_start = (start // granularity) * granularity
    claim_end = min(file_size, ((end + granularity - 1) // granularity) * granularity)
    return claim_start, claim_end


def intervals_overlap(left: tuple[int, int], right: tuple[int, int]) -> bool:
    return max(left[0], right[0]) < min(left[1], right[1])


GRANULARITIES: tuple[tuple[str, int | None], ...] = (
    ("exact_span", None),
    ("64B", 64),
    ("256B", 256),
    ("1KB", 1024),
    ("4KB", 4096),
    ("whole_file", -1),
)


def padded_piece_sets_overlap(pair: SpanPair, granularity: int) -> bool:
    claims1 = [aligned_claim(start, end, pair.file_size, granularity) for start, end in pair.pieces1]
    claims2 = [aligned_claim(start, end, pair.file_size, granularity) for start, end in pair.pieces2]
    return any(intervals_overlap(left, right) for left in claims1 for right in claims2)


def span_curve(pairs: Sequence[SpanPair], width_mode: str = "changed_byte_mass") -> dict[str, Any]:
    if not pairs:
        raise ValueError("span curve requires at least one pair")
    if width_mode == "changed_byte_mass":
        widths = [(pair.width1, pair.width2) for pair in pairs]
    elif width_mode == "bounding_hull":
        widths = [(pair.hull_width1, pair.hull_width2) for pair in pairs]
    else:
        raise ValueError(f"unknown width mode: {width_mode}")
    null_rows: list[dict[str, Any]] = []
    empirical_rows: list[dict[str, Any]] = []
    exact_disjoint = np.asarray(
        [
            contiguous_disjoint_probability(width1, width2, pair.file_size)
            for pair, (width1, width2) in zip(pairs, widths)
        ]
    )
    for label, granularity in GRANULARITIES:
        if granularity is None:
            null_values = np.zeros(len(pairs), dtype=np.float64)
            empirical_flags = np.zeros(len(pairs), dtype=bool)
        elif granularity == -1:
            null_values = exact_disjoint
            empirical_flags = np.asarray([not pair.strict_overlap for pair in pairs], dtype=bool)
        else:
            null_values = np.asarray(
                [
                    aligned_overblock_probability(
                        width1,
                        width2,
                        pair.file_size,
                        granularity,
                    )
                    for pair, (width1, width2) in zip(pairs, widths)
                ]
            )
            flags: list[bool] = []
            for pair in pairs:
                if pair.strict_overlap:
                    flags.append(False)
                    continue
                flags.append(padded_piece_sets_overlap(pair, granularity))
            empirical_flags = np.asarray(flags, dtype=bool)
        null_rows.append(
            {
                "granularity": label,
                "g_bytes": None if granularity in {None, -1} else granularity,
                "expected_overblocked_units": float(np.sum(null_values)),
                "denominator_pairs": len(pairs),
                "overblock_probability": float(np.mean(null_values)),
            }
        )
        empirical_rows.append(
            {
                "granularity": label,
                "g_bytes": None if granularity in {None, -1} else granularity,
                "overblocked_units": int(np.sum(empirical_flags)),
                "denominator_pairs": len(pairs),
                "overblock_rate": float(np.mean(empirical_flags)),
            }
        )
    return {
        "null": null_rows,
        "empirical": empirical_rows,
        "null_width_mode": width_mode,
        "null_exact_disjoint_probability": float(np.mean(exact_disjoint)),
        "null_expected_overlap_probability": float(np.mean(1.0 - exact_disjoint)),
        "null_expected_overlap_units": float(np.sum(1.0 - exact_disjoint)),
        "empirical_hull_overlap_units": int(sum(pair.hull_overlap for pair in pairs)),
        "empirical_hull_overlap_probability": float(np.mean([pair.hull_overlap for pair in pairs])),
        "empirical_strict_overlap_units": int(sum(pair.strict_overlap for pair in pairs)),
        "empirical_strict_overlap_probability": float(np.mean([pair.strict_overlap for pair in pairs])),
        "scattered_birthday_disjoint_probability": float(
            np.mean(
                [
                    scattered_birthday_disjoint_probability(width1, width2, pair.file_size)
                    for pair, (width1, width2) in zip(pairs, widths)
                ]
            )
        ),
    }


def result_blob_size_sensitivity(
    pairs: Sequence[SpanPair],
    width_mode: str = "changed_byte_mass",
) -> dict[str, Any]:
    if width_mode == "changed_byte_mass":
        widths = {id(pair): (pair.width1, pair.width2) for pair in pairs}
    elif width_mode == "bounding_hull":
        widths = {id(pair): (pair.hull_width1, pair.hull_width2) for pair in pairs}
    else:
        raise ValueError(f"unknown width mode: {width_mode}")
    eligible = [
        pair
        for pair in pairs
        if pair.result_blob_size is not None
        and pair.result_blob_size > 0
        and widths[id(pair)][0] <= pair.result_blob_size
        and widths[id(pair)][1] <= pair.result_blob_size
    ]
    values = [
        contiguous_disjoint_probability(
            widths[id(pair)][0],
            widths[id(pair)][1],
            int(pair.result_blob_size),
        )
        for pair in eligible
    ]
    return {
        "eligible_pairs": len(eligible),
        "all_pairs": len(pairs),
        "width_mode": width_mode,
        "mean_disjoint_probability": float(np.mean(values)) if values else None,
        "coordinate_warning": "widths are derived from base-coordinate changed spans; result-blob N is a size sensitivity, not the coordinate-correct primary denominator",
    }


def distribution_summary(values: Sequence[float]) -> dict[str, Any]:
    array = np.asarray(values, dtype=np.float64)
    if not len(array):
        return {"count": 0}
    return {
        "count": int(len(array)),
        "minimum": float(np.min(array)),
        "p50": float(np.quantile(array, 0.50, method="inverted_cdf")),
        "p90": float(np.quantile(array, 0.90, method="inverted_cdf")),
        "p99": float(np.quantile(array, 0.99, method="inverted_cdf")),
        "maximum": float(np.max(array)),
        "mean": float(np.mean(array)),
        "percentile_method": "nearest-rank / numpy inverted_cdf",
    }


def false_expiry(intervals_minutes: np.ndarray, lease_minutes: float) -> float:
    return float(np.mean(intervals_minutes > lease_minutes))


def expected_dangling(linger_minutes: np.ndarray, lease_minutes: float) -> float:
    return float(np.mean(np.minimum(linger_minutes, lease_minutes)))


def lease_objective(
    read_write_minutes: np.ndarray,
    linger_minutes: np.ndarray,
    lease_minutes: float,
    reacquisition_cost: float,
    blocking_cost: float,
) -> dict[str, float]:
    expiry = false_expiry(read_write_minutes, lease_minutes)
    dangling = expected_dangling(linger_minutes, lease_minutes)
    return {
        "lease_minutes": float(lease_minutes),
        "false_expiry": expiry,
        "expected_dangling_minutes": dangling,
        "false_expiry_cost": expiry * reacquisition_cost,
        "blocking_cost": dangling * blocking_cost,
        "objective_agent_minutes_per_claim": expiry * reacquisition_cost + dangling * blocking_cost,
    }


def optimize_lease(
    read_write_minutes: np.ndarray,
    linger_minutes: np.ndarray,
    reacquisition_cost: float,
    blocking_cost: float,
) -> dict[str, float]:
    candidates = np.unique(np.concatenate((np.asarray([0.0]), read_write_minutes)))
    rows = [
        lease_objective(
            read_write_minutes,
            linger_minutes,
            float(candidate),
            reacquisition_cost,
            blocking_cost,
        )
        for candidate in candidates
    ]
    return min(rows, key=lambda row: (row["objective_agent_minutes_per_claim"], row["lease_minutes"]))


def build_lease_analysis(parameters: dict[str, Any]) -> dict[str, Any]:
    relevant = parameters["parameters"]["read_to_write_intervals"]
    read_summary = relevant["first_read_result_to_absolute_first_write_call_seconds"]
    linger_summary = relevant["last_write_result_to_session_end_seconds"]
    active_summary = parameters["parameters"]["session_lengths"][
        "structured_active_span_seconds_per_core_active_actor"
    ]

    def reconstructed(summary: dict[str, Any]) -> np.ndarray:
        return quantile_reconstruction(
            int(summary["count"]),
            float(summary["p50"]),
            float(summary["p90"]),
            float(summary["p99"]),
            float(summary["max"]),
            0.0,
        ) / 60.0

    read_values = reconstructed(read_summary)
    linger_values = reconstructed(linger_summary)
    read_low, read_high = order_stat_extremes(
        int(read_summary["count"]),
        float(read_summary["p50"]),
        float(read_summary["p90"]),
        float(read_summary["p99"]),
        float(read_summary["max"]),
        0.0,
    )
    linger_low, linger_high = order_stat_extremes(
        int(linger_summary["count"]),
        float(linger_summary["p50"]),
        float(linger_summary["p90"]),
        float(linger_summary["p99"]),
        float(linger_summary["max"]),
        0.0,
    )
    read_low /= 60.0
    read_high /= 60.0
    linger_low /= 60.0
    linger_high /= 60.0

    reacquisition_cost = float(active_summary["p50"]) / 60.0
    blocking_cost = 1.0
    optimum = optimize_lease(read_values, linger_values, reacquisition_cost, blocking_cost)

    fixed_minutes = np.asarray(
        [0, 1 / 6, 0.25, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 240, 480, 720, 1440, 2880, 4320, 10080],
        dtype=np.float64,
    )
    fixed_minutes = np.unique(np.append(fixed_minutes, optimum["lease_minutes"]))
    curve = [
        lease_objective(read_values, linger_values, float(lease), reacquisition_cost, blocking_cost)
        for lease in fixed_minutes
    ]

    ratios = np.unique(
        np.concatenate(
            (
                np.geomspace(0.1, 10.0, 41),
                np.asarray([reacquisition_cost], dtype=np.float64),
            )
        )
    )
    sensitivity = []
    for ratio in ratios:
        row = optimize_lease(read_values, linger_values, float(ratio), 1.0)
        row["numeric_cost_ratio_reacquisition_to_blocking"] = float(ratio)
        sensitivity.append(row)

    scenarios: list[dict[str, Any]] = []
    for read_label, read_sample in (("low", read_low), ("high", read_high)):
        for linger_label, linger_sample in (("low", linger_low), ("high", linger_high)):
            row = optimize_lease(read_sample, linger_sample, reacquisition_cost, blocking_cost)
            row.update({"read_write_scenario": read_label, "linger_scenario": linger_label})
            scenarios.append(row)

    return {
        "status": "provisional_quantile_reconstruction_not_empirical",
        "L_star_minutes": optimum["lease_minutes"],
        "L_star_seconds": 60.0 * optimum["lease_minutes"],
        "definitions": {
            "false_expiry": "fraction of reconstructed read-to-write intervals strictly greater than L",
            "expected_dangling": "mean min(L,D) over reconstructed last-write-to-observed-end linger",
            "objective": "FalseExpiry(L)*ReacquisitionCost + ExpectedDanglingTime(L)*BlockingCost",
            "units": "minutes and agent-minutes per claim",
        },
        "inputs": {
            "read_to_write": read_summary,
            "linger": linger_summary,
            "reacquisition_proxy": active_summary,
        },
        "reconstruction": {
            "method": "piecewise-linear quantile function through minimum=0, p50, p90, p99, and maximum; forced to reproduce reported nearest ranks",
            "read_to_write_reconstructed_count": len(read_values),
            "linger_reconstructed_count": len(linger_values),
        },
        "costs": {
            "reacquisition_cost_agent_minutes": reacquisition_cost,
            "reacquisition_derivation": "p50 structured active span per core-active actor / 60; workload proxy, not measured startup overhead",
            "blocking_cost_agent_minutes_per_waiting_minute": blocking_cost,
            "numeric_default_ratio": reacquisition_cost / blocking_cost,
        },
        "optimum": optimum,
        "curve": curve,
        "cost_ratio_sensitivity": sensitivity,
        "summary_consistent_extreme_scenarios": scenarios,
        "scenario_lease_minimum_minutes": min(row["lease_minutes"] for row in scenarios),
        "scenario_lease_maximum_minutes": max(row["lease_minutes"] for row in scenarios),
        "caveat": "raw interval values are absent; the optimum and curve are reconstruction-dependent, and the linger endpoint is right-censored rather than an explicit close",
    }
