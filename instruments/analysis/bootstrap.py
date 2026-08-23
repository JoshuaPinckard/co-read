"""Compute paired whole-commit bootstrap intervals from stored replay results.

The script prints compact Markdown tables.  It reads only result JSONs and does
not run or import the replay harness.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = PROJECT_ROOT / "exploratory/language-hole/results"
REPLICATES = 10_000
BASE_SEED = "blast-radius-whole-commit-bootstrap-v1"
BATCH_SIZE = 128

# Fixed scope of the completed replay.  The shared replay repository list now
# contains two later targets with no result JSON, so it is intentionally not
# imported here.
REPOSITORIES = (
    ("hashicorp__terraform-provider-random", "hashicorp/terraform-provider-random"),
    ("psf__requests", "psf/requests"),
    ("BurntSushi__ripgrep", "BurntSushi/ripgrep"),
    ("apache__commons-lang", "apache/commons-lang"),
    ("jupyter__notebook", "jupyter/notebook"),
    ("gohugoio__hugo", "gohugoio/hugo"),
    ("redis__redis", "redis/redis"),
    ("prometheus__prometheus", "prometheus/prometheus"),
    ("hashicorp__terraform", "hashicorp/terraform"),
    ("ansible__ansible", "ansible/ansible"),
)
TIME_DECAYED = "cochange_time_decayed"
COMPARATORS = (
    ("popularity", "popularity_control", "Popularity control"),
    ("random", "random_draw", "Random control"),
    ("path", "path_name_similarity", "Path/name similarity"),
    ("plain", "cochange_plain_confidence", "Plain confidence"),
)
METRICS = (
    ("r10", "R@10", "r10_sum", "r_at_10"),
    ("p1", "P@1", "p1_hits", "p_at_1"),
)
ALL_MODELS = (TIME_DECAYED, *(model for _, model, _ in COMPARATORS))


@dataclass(frozen=True)
class Estimate:
    point: float
    low: float
    high: float

    @property
    def excludes_zero(self) -> bool:
        return self.low > 0.0 or self.high < 0.0


@dataclass
class RepositoryAnalysis:
    slug: str
    name: str
    commit_count: int
    query_count: int
    largest_commit_queries: int
    largest_commit_share: float
    top_five_share: float
    estimates: dict[tuple[str, str], Estimate | None]
    missing: dict[tuple[str, str], str]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--replicates", type=int, default=REPLICATES)
    parser.add_argument("--seed", default=BASE_SEED)
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    args = parser.parse_args()
    if args.replicates <= 0 or args.batch_size <= 0:
        parser.error("--replicates and --batch-size must be positive")
    return args


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def repository_seed(base_seed: str, slug: str) -> int:
    payload = base_seed.encode() + b"\0" + slug.encode()
    return int.from_bytes(hashlib.sha256(payload).digest()[:16], "big")


def load_and_validate(path: Path, expected_slug: str, expected_name: str) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        record = json.load(handle)
    repository = record.get("repository", {})
    if record.get("status") != "ok":
        raise ValueError(f"{path}: result status is not ok")
    if (repository.get("slug"), repository.get("name")) != (expected_slug, expected_name):
        raise ValueError(f"{path}: repository identity is outside the fixed scope")

    commits = record.get("eligible_commits")
    if not isinstance(commits, list) or not commits:
        raise ValueError(f"{path}: eligible_commits is absent or empty")
    queries: list[int] = []
    indices: list[int] = []
    shas: list[str] = []
    for position, commit in enumerate(commits):
        query_count = commit.get("query_count")
        if (
            not isinstance(query_count, int)
            or isinstance(query_count, bool)
            or query_count <= 0
            or query_count != commit.get("eligible_file_count")
        ):
            raise ValueError(f"{path}: invalid query denominator at eligible commit {position}")
        queries.append(query_count)
        indices.append(commit.get("index"))
        shas.append(commit.get("sha"))
    if not all(isinstance(index, int) for index in indices) or indices != sorted(set(indices)):
        raise ValueError(f"{path}: commit indices are invalid, duplicated, or unsorted")
    if not all(isinstance(sha, str) and sha for sha in shas) or len(shas) != len(set(shas)):
        raise ValueError(f"{path}: commit SHAs are invalid or duplicated")

    coverage = record.get("coverage", {})
    expected_coverage = (
        len(commits),
        sum(queries),
        max(queries),
        max(queries) / sum(queries),
    )
    actual_coverage = (
        coverage.get("eligible_commit_count"),
        coverage.get("query_count"),
        coverage.get("largest_query_commit_queries"),
        coverage.get("largest_query_commit_share"),
    )
    if actual_coverage[:3] != expected_coverage[:3] or not is_number(actual_coverage[3]):
        raise ValueError(f"{path}: coverage totals do not match eligible commits")
    if not math.isclose(actual_coverage[3], expected_coverage[3], abs_tol=1e-15):
        raise ValueError(f"{path}: largest-commit share does not match eligible commits")

    # Complete per-commit series must reproduce the top-level metric.  Missing
    # series are left for per-cell handling rather than silently filled or
    # dropped.
    for _, _, field, top_field in METRICS:
        for model in ALL_MODELS:
            values = [commit.get("models", {}).get(model, {}).get(field) for commit in commits]
            if not all(is_number(value) for value in values):
                continue
            if any(value < -1e-12 or value > query + 1e-12 for value, query in zip(values, queries)):
                raise ValueError(f"{path}: {model}.{field} is outside [0, query_count]")
            top_value = record.get("models", {}).get(model, {}).get(top_field)
            computed = math.fsum(values) / sum(queries)
            if is_number(top_value) and not math.isclose(computed, top_value, rel_tol=1e-11, abs_tol=1e-12):
                raise ValueError(f"{path}: {model}.{field} does not reproduce its top-level metric")
    return record


def series(
    commits: list[dict[str, Any]], model: str, field: str
) -> tuple[np.ndarray | None, str | None]:
    values: list[float] = []
    for position, commit in enumerate(commits):
        value = commit.get("models", {}).get(model, {}).get(field)
        if not is_number(value):
            identity = commit.get("sha", f"position {position}")
            return None, f"{model}.{field} missing/non-numeric at commit {identity}"
        values.append(float(value))
    return np.asarray(values), None


def analyze(
    record: dict[str, Any], base_seed: str, replicates: int, batch_size: int
) -> RepositoryAnalysis:
    commits = record["eligible_commits"]
    queries = np.asarray([commit["query_count"] for commit in commits], dtype=np.float64)
    differences: list[np.ndarray] = []
    keys: list[tuple[str, str]] = []
    estimates: dict[tuple[str, str], Estimate | None] = {}
    missing: dict[tuple[str, str], str] = {}

    for metric_key, _, field, _ in METRICS:
        decayed, decayed_error = series(commits, TIME_DECAYED, field)
        for comparator_key, comparator, _ in COMPARATORS:
            other, other_error = series(commits, comparator, field)
            key = (metric_key, comparator_key)
            if decayed is None or other is None:
                estimates[key] = None
                missing[key] = decayed_error or other_error or "stored statistic unavailable"
            else:
                keys.append(key)
                differences.append(decayed - other)

    if keys:
        # Column 0 is the denominator; all other columns are paired numerator
        # differences.  One index matrix therefore feeds every available cell.
        matrix = np.column_stack((queries, *differences))
        draws = np.empty((replicates, len(keys)))
        rng = np.random.Generator(np.random.PCG64(repository_seed(base_seed, record["repository"]["slug"])))
        for start in range(0, replicates, batch_size):
            stop = min(start + batch_size, replicates)
            indices = rng.integers(0, len(commits), size=(stop - start, len(commits)))
            totals = matrix[indices].sum(axis=1)
            draws[start:stop] = totals[:, 1:] / totals[:, [0]]
        bounds = np.percentile(draws, (2.5, 97.5), axis=0, method="linear")
        denominator = queries.sum()
        for column, key in enumerate(keys):
            estimates[key] = Estimate(
                float(differences[column].sum() / denominator),
                float(bounds[0, column]),
                float(bounds[1, column]),
            )

    sorted_queries = np.sort(queries)[::-1]
    total_queries = int(queries.sum())
    repository = record["repository"]
    return RepositoryAnalysis(
        slug=repository["slug"],
        name=repository["name"],
        commit_count=len(commits),
        query_count=total_queries,
        largest_commit_queries=int(sorted_queries[0]),
        largest_commit_share=float(sorted_queries[0] / total_queries),
        top_five_share=float(sorted_queries[:5].sum() / total_queries),
        estimates=estimates,
        missing=missing,
    )


def cell(estimate: Estimate | None) -> str:
    if estimate is None:
        return ""
    marker = "*" if estimate.excludes_zero else ""
    return f"{estimate.point:+.4f} [{estimate.low:+.4f}, {estimate.high:+.4f}]{marker}"


def path_classification(repository: RepositoryAnalysis, metric: str) -> str:
    estimate = repository.estimates[(metric, "path")]
    if estimate is None:
        return "unavailable"
    winner = "path" if estimate.point < 0 else "co-change"
    return f"{winner}; {'excludes' if estimate.excludes_zero else 'includes'} zero"


def render(analyses: list[RepositoryAnalysis], seed: str, replicates: int) -> str:
    lines = [
        "# Whole-commit bootstrap computation",
        "",
        f"Replicates: {replicates:,}; base seed: `{seed}`; RNG: NumPy {np.__version__} PCG64; ",
        "repository seed: first 128 bits of SHA-256(base seed + NUL + slug); ",
        "interval: linear 2.5th/97.5th percentiles. `*` means the interval excludes zero.",
        "",
    ]
    pop_r10 = [r for r in analyses if (e := r.estimates[("r10", "popularity")]) and e.low > 0]
    pop_p1 = [r for r in analyses if (e := r.estimates[("p1", "popularity")]) and e.low > 0]
    decay = [
        e
        for r in analyses
        for metric in ("r10", "p1")
        if (e := r.estimates[(metric, "plain")]) and e.excludes_zero
    ]
    lines.extend(
        [
            f"Popularity exclusions: R@10 {len(pop_r10)}/10; P@1 {len(pop_p1)}/10.",
            f"Decay/plain exclusions: {len(decay)}/20 ({sum(e.point > 0 for e in decay)} favor decay; "
            f"{sum(e.point < 0 for e in decay)} favor plain).",
            "",
        ]
    )

    for metric_key, metric_label, _, _ in METRICS:
        lines.extend(
            [
                f"## {metric_label}: time-decayed minus comparator",
                "",
                "| Repository | Popularity | Random | Path | Plain |",
                "|---|---:|---:|---:|---:|",
            ]
        )
        for repository in analyses:
            values = [cell(repository.estimates[(metric_key, key)]) for key, _, _ in COMPARATORS]
            lines.append(f"| {repository.name} | " + " | ".join(values) + " |")
        lines.append("")

    lines.extend(
        [
            "## Named path checks",
            "",
            "| Repository | P@1 | R@10 |",
            "|---|---|---|",
        ]
    )
    for slug in ("hashicorp__terraform-provider-random", "hashicorp__terraform", "ansible__ansible"):
        repository = next(item for item in analyses if item.slug == slug)
        lines.append(
            f"| {repository.name} | {path_classification(repository, 'p1')} | "
            f"{path_classification(repository, 'r10')} |"
        )

    lines.extend(
        [
            "",
            "## Cluster counts",
            "",
            "| Repository | Commits | Queries | Largest queries | Largest share | Top-five share |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for repository in analyses:
        lines.append(
            f"| {repository.name} | {repository.commit_count:,} | {repository.query_count:,} | "
            f"{repository.largest_commit_queries:,} | {100 * repository.largest_commit_share:.1f}% | "
            f"{100 * repository.top_five_share:.1f}% |"
        )

    omissions = [
        f"{r.name} {metric}/{comparator}: {reason}"
        for r in analyses
        for (metric, comparator), reason in r.missing.items()
    ]
    lines.extend(["", "Stored-statistic omissions: " + ("none" if not omissions else ""), ""])
    lines.extend(f"- {omission}" for omission in omissions)
    return "\n".join(lines)


def main() -> None:
    args = parse_args()
    analyses = [
        analyze(
            load_and_validate(args.input / f"{slug}.json", slug, name),
            args.seed,
            args.replicates,
            args.batch_size,
        )
        for slug, name in REPOSITORIES
    ]
    print(render(analyses, args.seed, args.replicates))


if __name__ == "__main__":
    main()
