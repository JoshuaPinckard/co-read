#!/usr/bin/env python3
"""Deterministically analyse mined historical merge conflicts.

The miner intentionally emits two populations: ``_all_merges`` contains every
evaluable or failed exactly-two-parent merge, while the repository JSONL at the
corpus root contains the richer records only for conflicted merges.  This
module reconciles those populations before computing any rate.  It never runs
Git and never reads a working tree.

Run from the project root after mining has completed::

    python instruments/conflicts/analyze.py

The two outputs contain no wall-clock generation timestamp or absolute scratch
path.  With identical inputs, Python version, and bootstrap replicate count,
they are byte-identical.
"""

from __future__ import annotations

import argparse
import bisect
import collections
import dataclasses
import hashlib
import json
import math
import random
import sys
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from instruments.conflicts.miner import (
    CLASSIFICATION_REVISION,
    CLASSIFICATION_RULE,
    CONFLICT_RANGE_REVISION,
    DETERMINISTIC_GIT_ENVIRONMENT_OVERRIDES,
    MERGE_TREE_INTERPRETATION,
    MERGE_TREE_INVOCATION,
    MINER_PROTOCOL_REVISION,
    MINER_SOURCE_SHA256,
    OVERLAP_REVISION,
    OVERLAP_RULE,
    SCRUBBED_GIT_ENVIRONMENT_KEYS,
    SCRUBBED_GIT_ENVIRONMENT_PREFIXES,
    STORAGE_POLICY,
    TEST_PATH_REVISION,
    TEST_PATH_RULE,
    sha256_file,
)

DEFAULT_REPOSITORIES = Path(__file__).with_name("repositories.json")
DEFAULT_CORPUS = PROJECT_ROOT / "corpus" / "conflicts"
DEFAULT_OUTPUT = PROJECT_ROOT / "exploratory" / "conflicts"
BOOTSTRAP_REPLICATES = 10_000
BOOTSTRAP_SEED = "blast-radius-conflicts-analysis-v1"
Z_95 = 1.959963984540054

FAILURE_STATUSES = {
    "error",
    "failed",
    "failure",
    "unevaluable",
    "missing_objects",
    "missing_object",
    "no_merge_base",
    "invalid",
}
ARTIFACT_KINDS = frozenset({"generated", "lockfile", "vendored"})
KNOWN_KINDS = frozenset({"handwritten", *ARTIFACT_KINDS})
MEASURED_RANGE_STATUS = "measured_text_markers"
UNAVAILABLE_RANGE_STATUSES = frozenset(
    {
        "unavailable_binary_result",
        "unavailable_no_result_blob",
        "unavailable_no_text_markers",
        "unavailable_unbalanced_markers",
    }
)


@dataclasses.dataclass(frozen=True)
class BinSpec:
    key: str
    label: str
    lower: float
    upper: float | None

    def contains(self, value: float) -> bool:
        return value >= self.lower and (self.upper is None or value < self.upper)


COMMIT_BINS = (
    BinSpec("0", "0", 0, 1),
    BinSpec("1", "1", 1, 2),
    BinSpec("2_3", "2–3", 2, 4),
    BinSpec("4_7", "4–7", 4, 8),
    BinSpec("8_15", "8–15", 8, 16),
    BinSpec("16_31", "16–31", 16, 32),
    BinSpec("32_63", "32–63", 32, 64),
    BinSpec("64_127", "64–127", 64, 128),
    BinSpec("128_plus", "128+", 128, None),
)
TIME_BINS = (
    BinSpec("under_1_day", "<1 day", 0, 86_400),
    BinSpec("1_to_7_days", "1–<7 days", 86_400, 7 * 86_400),
    BinSpec("7_to_30_days", "7–<30 days", 7 * 86_400, 30 * 86_400),
    BinSpec("30_to_90_days", "30–<90 days", 30 * 86_400, 90 * 86_400),
    BinSpec("90_to_365_days", "90–<365 days", 90 * 86_400, 365 * 86_400),
    BinSpec("365_days_plus", "365+ days", 365 * 86_400, None),
)
LINE_BINS = (
    BinSpec("0", "0", 0, 1),
    BinSpec("1_15", "1–15", 1, 16),
    BinSpec("16_63", "16–63", 16, 64),
    BinSpec("64_255", "64–255", 64, 256),
    BinSpec("256_1023", "256–1,023", 256, 1_024),
    BinSpec("1024_4095", "1,024–4,095", 1_024, 4_096),
    BinSpec("4096_plus", "4,096+", 4_096, None),
)


@dataclasses.dataclass(frozen=True)
class RepositorySpec:
    slug: str
    repo: str
    language: str
    shape: str
    head: str | None
    rationale: str | None
    raw: Mapping[str, Any]


@dataclasses.dataclass
class RepositoryData:
    spec: RepositorySpec
    summary: Mapping[str, Any]
    all_merges: list[dict[str, Any]]
    conflict_merges: list[dict[str, Any]]
    first_parent_commits: int | None

    @property
    def discovered(self) -> int:
        return len(self.all_merges)

    @property
    def evaluable_rows(self) -> list[dict[str, Any]]:
        return [row for row in self.all_merges if row_is_evaluable(row)]

    @property
    def conflicts(self) -> int:
        return sum(bool(row["conflicted"]) for row in self.evaluable_rows)


@dataclasses.dataclass(frozen=True)
class ConflictOccurrence:
    repo: str
    slug: str
    merge: str
    path: str
    language: str
    shape: str
    kind: str
    range_status: str
    file_size: int | None
    conflicted_bytes: int | None

    @property
    def measurable(self) -> bool:
        return (
            self.file_size is not None
            and self.file_size > 0
            and self.conflicted_bytes is not None
        )

    @property
    def ratio(self) -> float | None:
        if not self.measurable:
            return None
        assert self.file_size is not None and self.conflicted_bytes is not None
        return self.conflicted_bytes / self.file_size


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repositories", type=Path, default=DEFAULT_REPOSITORIES)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--replicates", type=int, default=BOOTSTRAP_REPLICATES)
    parser.add_argument("--seed", default=BOOTSTRAP_SEED)
    parser.add_argument("--determinism", type=Path)
    parser.add_argument(
        "--reclassification",
        type=Path,
        help="classifier-migration JSON (default: RECLASSIFICATION.json beside the output)",
    )
    parser.add_argument(
        "--preparation",
        type=Path,
        help="mirror-preparation JSON (default: PREPARATION.json beside the output)",
    )
    parser.add_argument(
        "--hydration",
        type=Path,
        help="final no-lazy hydration audit JSON (default: HYDRATION.json beside the output)",
    )
    args = parser.parse_args(argv)
    if args.replicates <= 0:
        parser.error("--replicates must be positive")
    return args


def is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def stable_path_bytes(value: str) -> bytes:
    """Recover Git path bytes when JSON carries surrogate-escaped names."""
    return value.encode("utf-8", errors="surrogateescape")


def integer(value: Any, *, label: str, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ValueError(f"{label}: expected integer >= {minimum}, got {value!r}")
    return value


def nested(record: Mapping[str, Any], *paths: Sequence[str]) -> Any:
    for path in paths:
        value: Any = record
        for part in path:
            if not isinstance(value, Mapping) or part not in value:
                break
            value = value[part]
        else:
            return value
    return None


def first_text(record: Mapping[str, Any], *paths: Sequence[str]) -> str | None:
    value = nested(record, *paths)
    return value if isinstance(value, str) and value.strip() else None


def first_text_or_argv(record: Mapping[str, Any], *paths: Sequence[str]) -> str | None:
    """Return prose unchanged or render an argv array without lossy shell quoting."""

    value = nested(record, *paths)
    if isinstance(value, str) and value.strip():
        return value
    if isinstance(value, list) and value and all(isinstance(item, str) for item in value):
        return "argv=" + json.dumps(value, ensure_ascii=True, separators=(",", ":"))
    return None


def first_count(record: Mapping[str, Any], *paths: Sequence[str]) -> int | None:
    value = nested(record, *paths)
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def read_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError as error:
        raise FileNotFoundError(f"required analysis input is absent: {path}") from error


def read_jsonl(path: Path, *, missing_ok: bool = False) -> list[dict[str, Any]]:
    if missing_ok and not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError(f"{path}:{line_number}: JSONL row is not an object")
                rows.append(value)
    except FileNotFoundError as error:
        raise FileNotFoundError(f"required analysis input is absent: {path}") from error
    return rows


def normalize_manifest(raw: Any) -> list[RepositorySpec]:
    if isinstance(raw, Mapping):
        if "schema_version" in raw and raw.get("schema_version") != 1:
            raise ValueError("repositories.json schema_version must be 1")
        items = raw.get("repositories", raw.get("repos"))
        if items is None and all(isinstance(value, Mapping) for value in raw.values()):
            items = [dict(value, slug=key) for key, value in raw.items()]
    else:
        items = raw
    if not isinstance(items, list) or not items:
        raise ValueError("repositories.json must contain a nonempty repository list")

    specs: list[RepositorySpec] = []
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            raise ValueError(f"repositories[{index}] is not an object")
        repo = first_text(
            item,
            ("repo",),
            ("repository",),
            ("name",),
            ("github",),
        )
        slug = first_text(item, ("slug",), ("id",))
        if slug is None and repo is not None:
            slug = repo.replace("/", "__")
        if repo is None and slug is not None:
            repo = slug.replace("__", "/", 1)
        language = first_text(item, ("primary_language",), ("language",))
        shape = first_text(item, ("primary_shape",), ("project_shape",), ("shape",))
        if not all((slug, repo, language, shape)):
            raise ValueError(
                f"repositories[{index}] requires slug/repo, primary language, and primary shape"
            )
        specs.append(
            RepositorySpec(
                slug=str(slug),
                repo=str(repo),
                language=str(language),
                shape=str(shape),
                head=first_text(
                    item,
                    ("head",),
                    ("frozen_head",),
                    ("tip",),
                    ("head_sha",),
                ),
                rationale=first_text(
                    item,
                    ("rationale",),
                    ("selection_rationale",),
                    ("axis",),
                    ("coverage_note",),
                    ("project_shape_note",),
                ),
                raw=item,
            )
        )
    specs.sort(key=lambda spec: spec.slug.encode("utf-8"))
    slugs = [spec.slug for spec in specs]
    if len(slugs) != len(set(slugs)):
        raise ValueError("repositories.json contains duplicate slugs")
    return specs


def row_merge(row: Mapping[str, Any], *, source: str) -> str:
    value = row.get("merge")
    if not isinstance(value, str) or not value:
        raise ValueError(f"{source}: merge is absent or invalid")
    return value


def row_is_evaluable(row: Mapping[str, Any]) -> bool:
    status_value = row.get("evaluation_status")
    status = str(status_value).strip().lower() if status_value is not None else ""
    if status in FAILURE_STATUSES or status.startswith("error") or status.startswith("fail"):
        return False
    return isinstance(row.get("conflicted"), bool)


def validate_row_identity(row: Mapping[str, Any], spec: RepositorySpec, source: str) -> None:
    identity = row.get("repo")
    if identity is not None and identity not in {spec.repo, spec.slug}:
        raise ValueError(f"{source}: repo {identity!r} does not match {spec.repo!r}")
    if row.get("miner_protocol_revision") != MINER_PROTOCOL_REVISION:
        raise ValueError(
            f"{source}: miner protocol revision does not match {MINER_PROTOCOL_REVISION!r}"
        )
    if row.get("miner_source_sha256") != MINER_SOURCE_SHA256:
        raise ValueError(f"{source}: miner source hash does not match the current miner")


def summary_first_parent_count(summary: Mapping[str, Any]) -> int | None:
    return first_count(
        summary,
        ("first_parent_commits",),
        ("first_parent_commit_count",),
        ("counts", "first_parent_commits"),
        ("counts", "first_parent_commit_count"),
        ("history", "first_parent_commits"),
    )


def reconcile_summary_counts(data: RepositoryData) -> None:
    checks = (
        (
            data.discovered,
            first_count(
                data.summary,
                ("two_parent_merges",),
                ("two_parent_merge_count",),
                ("eligible_merges",),
                ("eligible_two_parent_merges",),
                ("counts", "two_parent_merges"),
                ("counts", "eligible_merges"),
            ),
            "two-parent merge count",
        ),
        (
            len(data.evaluable_rows),
            first_count(
                data.summary,
                ("evaluable_merges",),
                ("evaluated_merges",),
                ("counts", "evaluable_merges"),
                ("counts", "evaluated_merges"),
            ),
            "evaluable merge count",
        ),
        (
            data.conflicts,
            first_count(
                data.summary,
                ("conflicted_merges",),
                ("conflict_count",),
                ("counts", "conflicted_merges"),
                ("counts", "conflicts"),
            ),
            "conflicted merge count",
        ),
        (
            data.discovered - len(data.evaluable_rows),
            first_count(
                data.summary,
                ("failed_merges",),
                ("counts", "failed_merges"),
            ),
            "failed merge count",
        ),
        (
            len(data.evaluable_rows) - data.conflicts,
            first_count(
                data.summary,
                ("clean_merges",),
                ("counts", "clean_merges"),
            ),
            "clean merge count",
        ),
    )
    for observed, stored, label in checks:
        if stored is not None and stored != observed:
            raise ValueError(
                f"{data.spec.slug}: summary {label} {stored} != JSONL-derived {observed}"
            )


def load_repository_data(spec: RepositorySpec, corpus: Path) -> RepositoryData:
    summary_path = corpus / "_summaries" / f"{spec.slug}.json"
    all_path = corpus / "_all_merges" / f"{spec.slug}.jsonl"
    conflict_path = corpus / f"{spec.slug}.jsonl"
    summary = read_json(summary_path)
    if not isinstance(summary, Mapping):
        raise ValueError(f"{summary_path}: summary is not an object")
    if "schema_version" in summary and summary.get("schema_version") != 1:
        raise ValueError(f"{summary_path}: schema_version must be 1")
    summary_repo = first_text(summary, ("repo",))
    summary_slug = first_text(summary, ("slug",))
    summary_head = first_text(summary, ("head",), ("frozen_head",))
    if summary_repo is not None and summary_repo != spec.repo:
        raise ValueError(f"{summary_path}: repo does not match manifest")
    if summary_slug is not None and summary_slug != spec.slug:
        raise ValueError(f"{summary_path}: slug does not match manifest")
    if spec.head is not None and summary_head is not None and summary_head != spec.head:
        raise ValueError(f"{summary_path}: frozen head does not match manifest")
    expected_revisions = {
        "miner_protocol_revision": MINER_PROTOCOL_REVISION,
        "miner_source_sha256": MINER_SOURCE_SHA256,
        "classification_revision": CLASSIFICATION_REVISION,
        "conflict_range_revision": CONFLICT_RANGE_REVISION,
        "overlap_revision": OVERLAP_REVISION,
        "test_path_revision": TEST_PATH_REVISION,
    }
    for field, expected in expected_revisions.items():
        observed = summary.get(field)
        if observed != expected:
            raise ValueError(
                f"{summary_path}: {field} {observed!r} != current {expected!r}; "
                "run the declared corpus migrations before analysis"
            )
    expected_rules = {
        "classification_rule": CLASSIFICATION_RULE,
        "git_environment_overrides": dict(
            sorted(DETERMINISTIC_GIT_ENVIRONMENT_OVERRIDES.items())
        ),
        "git_environment_scrubbed": {
            "exact": list(SCRUBBED_GIT_ENVIRONMENT_KEYS),
            "prefixes": list(SCRUBBED_GIT_ENVIRONMENT_PREFIXES),
        },
        "merge_tree_interpretation": MERGE_TREE_INTERPRETATION,
        "merge_tree_invocation": list(MERGE_TREE_INVOCATION),
        "overlap_rule": OVERLAP_RULE,
        "storage_policy": STORAGE_POLICY,
        "test_path_rule": TEST_PATH_RULE,
    }
    for field, expected in expected_rules.items():
        if summary.get(field) != expected:
            raise ValueError(
                f"{summary_path}: {field} does not match the current declared method"
            )
    mirror_verification = summary.get("mirror_verification")
    if not isinstance(mirror_verification, Mapping):
        raise ValueError(f"{summary_path}: mirror verification is absent")
    expected_origin = first_text(spec.raw, ("url",))
    expected_mirror_values = {
        "alternates": False,
        "bare": True,
        "direct_child": True,
        "origin": expected_origin,
        "partial_clone_filter": "blob:none",
        "promisor": True,
        "reparse_point": False,
        "shallow": False,
    }
    if dict(mirror_verification) != expected_mirror_values:
        raise ValueError(f"{summary_path}: mirror verification does not match the manifest")
    if not first_text(summary, ("python_implementation",)) or not first_text(
        summary, ("python_version",)
    ):
        raise ValueError(f"{summary_path}: Python runtime provenance is absent")
    all_merges = read_jsonl(all_path)
    conflict_merges = read_jsonl(conflict_path)
    stored_hashes = summary.get("output_sha256")
    if not isinstance(stored_hashes, Mapping):
        raise ValueError(f"{summary_path}: output_sha256 is absent or invalid")
    for label, path in (("all_merges", all_path), ("conflicts", conflict_path)):
        observed_hash = sha256_file(path)
        if stored_hashes.get(label) != observed_hash:
            raise ValueError(
                f"{summary_path}: {label} hash does not match {path}"
            )

    all_by_merge: dict[str, dict[str, Any]] = {}
    for position, row in enumerate(all_merges):
        source = f"{all_path}:{position + 1}"
        validate_row_identity(row, spec, source)
        merge = row_merge(row, source=source)
        if merge in all_by_merge:
            raise ValueError(f"{all_path}: duplicate merge {merge}")
        all_by_merge[merge] = row

    conflict_by_merge: dict[str, dict[str, Any]] = {}
    for position, row in enumerate(conflict_merges):
        source = f"{conflict_path}:{position + 1}"
        validate_row_identity(row, spec, source)
        merge = row_merge(row, source=source)
        if merge in conflict_by_merge:
            raise ValueError(f"{conflict_path}: duplicate merge {merge}")
        if merge not in all_by_merge:
            raise ValueError(f"{conflict_path}: conflicted merge {merge} absent from all-merges table")
        if not row_is_evaluable(all_by_merge[merge]) or not all_by_merge[merge]["conflicted"]:
            raise ValueError(f"{conflict_path}: {merge} is not conflicted in all-merges table")
        for key in (
            "parents",
            "merge_base",
            "divergence",
            "diffs",
            "conflicted",
            "evaluation_status",
        ):
            if key in all_by_merge[merge] and row.get(key) != all_by_merge[merge].get(key):
                raise ValueError(
                    f"{conflict_path}: {merge} field {key!r} differs from all-merges row"
                )
        overlap = row.get("overlap")
        if not isinstance(overlap, Mapping):
            raise ValueError(f"{conflict_path}: {merge} lacks overlap metadata")
        if overlap.get("rule_revision") != OVERLAP_REVISION:
            raise ValueError(
                f"{conflict_path}: {merge} overlap revision "
                f"{overlap.get('rule_revision')!r} != current {OVERLAP_REVISION!r}"
            )
        raw_conflicts = row.get("conflicts")
        if not isinstance(raw_conflicts, list) or not raw_conflicts:
            raise ValueError(f"{conflict_path}: {merge} lacks conflict-path metadata")
        declared_paths = row.get("conflicted_paths")
        if (
            not isinstance(declared_paths, list)
            or not declared_paths
            or any(not isinstance(path, str) or not path for path in declared_paths)
            or len(set(declared_paths)) != len(declared_paths)
        ):
            raise ValueError(
                f"{conflict_path}: {merge} has absent, invalid, or duplicate conflicted_paths"
            )
        conflict_paths: list[str] = []
        for conflict_index, conflict in enumerate(raw_conflicts):
            classification = (
                conflict.get("classification")
                if isinstance(conflict, Mapping)
                else None
            )
            kind = (
                classification.get("kind")
                if isinstance(classification, Mapping)
                else None
            )
            if kind not in KNOWN_KINDS:
                raise ValueError(
                    f"{conflict_path}: {merge} conflict {conflict_index} has "
                    f"invalid classification {kind!r}"
                )
            path = conflict.get("path") if isinstance(conflict, Mapping) else None
            if not isinstance(path, str) or not path:
                raise ValueError(
                    f"{conflict_path}: {merge} conflict {conflict_index} has invalid path"
                )
            conflict_paths.append(path)
        raw_overlap_paths = overlap.get("paths")
        if not isinstance(raw_overlap_paths, list):
            raise ValueError(f"{conflict_path}: {merge} lacks per-path overlap metadata")
        overlap_paths = [
            item.get("path") if isinstance(item, Mapping) else None
            for item in raw_overlap_paths
        ]
        if (
            any(not isinstance(path, str) or not path for path in overlap_paths)
            or len(set(conflict_paths)) != len(conflict_paths)
            or len(set(overlap_paths)) != len(overlap_paths)
            or set(declared_paths) != set(conflict_paths)
            or set(declared_paths) != set(overlap_paths)
        ):
            raise ValueError(
                f"{conflict_path}: {merge} conflicted_paths, conflicts, and overlap paths "
                "do not reconcile one-to-one"
            )
        conflict_by_merge[merge] = row

    expected_conflicts = {
        merge
        for merge, row in all_by_merge.items()
        if row_is_evaluable(row) and bool(row["conflicted"])
    }
    if set(conflict_by_merge) != expected_conflicts:
        missing = sorted(expected_conflicts - set(conflict_by_merge))
        extra = sorted(set(conflict_by_merge) - expected_conflicts)
        raise ValueError(
            f"{spec.slug}: conflict JSONL does not reconcile; missing={missing[:5]}, extra={extra[:5]}"
        )

    data = RepositoryData(
        spec=spec,
        summary=summary,
        all_merges=all_merges,
        conflict_merges=conflict_merges,
        first_parent_commits=summary_first_parent_count(summary),
    )
    reconcile_summary_counts(data)
    if data.first_parent_commits is not None and data.discovered > data.first_parent_commits:
        raise ValueError(f"{spec.slug}: two-parent merges exceed first-parent commits")
    return data


def rate(numerator: int, denominator: int) -> dict[str, Any]:
    if numerator < 0 or denominator < 0 or numerator > denominator:
        raise ValueError(f"invalid rate {numerator}/{denominator}")
    return {
        "numerator": numerator,
        "denominator": denominator,
        "value": numerator / denominator if denominator else None,
    }


def wilson_interval(successes: int, total: int, z: float = Z_95) -> list[float] | None:
    if total <= 0:
        return None
    if successes < 0 or successes > total:
        raise ValueError(f"invalid Wilson inputs {successes}/{total}")
    proportion = successes / total
    denominator = 1 + z * z / total
    centre = (proportion + z * z / (2 * total)) / denominator
    radius = (
        z
        * math.sqrt(
            proportion * (1 - proportion) / total + z * z / (4 * total * total)
        )
        / denominator
    )
    return [max(0.0, centre - radius), min(1.0, centre + radius)]


def percentile(values: Sequence[float], probability: float) -> float:
    if not values:
        raise ValueError("cannot take percentile of an empty sequence")
    ordered = sorted(values)
    position = probability * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def deterministic_seed(base_seed: str, label: str) -> int:
    material = base_seed.encode("utf-8") + b"\0" + label.encode("utf-8")
    return int.from_bytes(hashlib.sha256(material).digest()[:16], "big")


def bootstrap_percentile_interval(
    values: Sequence[float],
    *,
    label: str,
    base_seed: str,
    replicates: int,
) -> list[float] | None:
    if len(values) < 3:
        return None
    rng = random.Random(deterministic_seed(base_seed, label))
    draws = [
        math.fsum(rng.choice(values) for _ in values) / len(values)
        for _ in range(replicates)
    ]
    return [percentile(draws, 0.025), percentile(draws, 0.975)]


def cluster_rate_interval(
    cluster_counts: Mapping[str, tuple[int, int]],
    *,
    label: str,
    base_seed: str,
    replicates: int,
) -> tuple[list[float] | None, int, int]:
    keys = sorted(cluster_counts, key=lambda item: item.encode("utf-8"))
    contributors = sum(cluster_counts[key][1] > 0 for key in keys)
    if len(keys) < 3 or contributors < 3:
        return None, contributors, 0
    rng = random.Random(deterministic_seed(base_seed, label))
    draws: list[float] = []
    for _ in range(replicates):
        numerator = 0
        denominator = 0
        for _position in keys:
            chosen = rng.choice(keys)
            successes, total = cluster_counts[chosen]
            numerator += successes
            denominator += total
        if denominator:
            draws.append(numerator / denominator)
    if len(draws) < math.ceil(0.95 * replicates):
        return None, contributors, len(draws)
    return [percentile(draws, 0.025), percentile(draws, 0.975)], contributors, len(draws)


def population_summary(repositories: Sequence[RepositoryData]) -> dict[str, Any]:
    first_parent_values = [repository.first_parent_commits for repository in repositories]
    first_parent_complete = all(value is not None for value in first_parent_values)
    first_parent = (
        sum(int(value) for value in first_parent_values if value is not None)
        if first_parent_complete
        else None
    )
    discovered = sum(repository.discovered for repository in repositories)
    evaluable = sum(len(repository.evaluable_rows) for repository in repositories)
    conflicts = sum(repository.conflicts for repository in repositories)
    no_merge_base = sum(
        row.get("evaluation_status") == "no_merge_base"
        for repository in repositories
        for row in repository.all_merges
    )
    first_parent_merge_values = [
        first_count(repository.summary, ("first_parent_merges",))
        for repository in repositories
    ]
    octopus_values = [
        first_count(repository.summary, ("excluded_octopus_merges",))
        for repository in repositories
    ]
    multiple_base_values = [
        first_count(repository.summary, ("multiple_merge_base_merges",))
        for repository in repositories
    ]
    first_parent_merges = (
        sum(int(value) for value in first_parent_merge_values if value is not None)
        if all(value is not None for value in first_parent_merge_values)
        else None
    )
    excluded_octopus = (
        sum(int(value) for value in octopus_values if value is not None)
        if all(value is not None for value in octopus_values)
        else None
    )
    multiple_merge_bases = (
        sum(int(value) for value in multiple_base_values if value is not None)
        if all(value is not None for value in multiple_base_values)
        else None
    )
    return {
        "repository_count": len(repositories),
        "first_parent_commits": first_parent,
        "first_parent_count_coverage": rate(
            sum(value is not None for value in first_parent_values), len(repositories)
        ),
        "two_parent_merges": discovered,
        "first_parent_merges": first_parent_merges,
        "excluded_octopus_merges": excluded_octopus,
        "multiple_merge_base_merges": multiple_merge_bases,
        "evaluable_merges": evaluable,
        "evaluation_failures": discovered - evaluable,
        "no_merge_base_merges": no_merge_base,
        "conflicted_merges": conflicts,
        "merge_prevalence": rate(discovered, first_parent) if first_parent is not None else None,
        "all_merge_prevalence": (
            rate(first_parent_merges, first_parent)
            if first_parent is not None and first_parent_merges is not None
            else None
        ),
        "conflict_rate": rate(conflicts, evaluable),
        "conflict_rate_wilson_95": wilson_interval(conflicts, evaluable),
    }


def repository_population_row(repository: RepositoryData) -> dict[str, Any]:
    summary = population_summary([repository])
    return {
        "slug": repository.spec.slug,
        "repo": repository.spec.repo,
        "language": repository.spec.language,
        "shape": repository.spec.shape,
        "head": repository.spec.head,
        "selection_note": repository.spec.rationale,
        **{key: value for key, value in summary.items() if key != "repository_count"},
    }


def binary_file_count(side: Mapping[str, Any]) -> int | None:
    value = side.get("binary_files")
    if value is None:
        return 0
    if isinstance(value, list):
        return len(value)
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def divergence_value(row: Mapping[str, Any], metric: str) -> float | None:
    divergence = row.get("divergence")
    if not isinstance(divergence, Mapping):
        return None
    if metric == "commits":
        value = divergence.get("combined_commits")
        return float(value) if is_number(value) and value >= 0 else None
    if metric == "time":
        if divergence.get("negative_clock") is True:
            return None
        value = divergence.get("max_wall_clock_seconds")
        return float(value) if is_number(value) and value >= 0 else None
    if metric == "lines":
        diffs = row.get("diffs")
        if not isinstance(diffs, Mapping):
            return None
        total = 0.0
        for parent in ("parent1", "parent2"):
            side = diffs.get(parent)
            if not isinstance(side, Mapping):
                return None
            binaries = binary_file_count(side)
            lines = side.get("lines_changed")
            if binaries is None or binaries > 0 or not is_number(lines) or lines < 0:
                return None
            total += float(lines)
        return total
    raise ValueError(f"unknown divergence metric {metric!r}")


def auc_probability_of_superiority(positives: Sequence[float], negatives: Sequence[float]) -> float:
    if not positives or not negatives:
        raise ValueError("AUC requires at least one positive and one negative")
    ordered_negatives = sorted(negatives)
    score = 0.0
    for value in positives:
        lower = bisect.bisect_left(ordered_negatives, value)
        upper = bisect.bisect_right(ordered_negatives, value)
        score += lower + 0.5 * (upper - lower)
    return score / (len(positives) * len(negatives))


def divergence_auc(
    repositories: Sequence[RepositoryData],
    *,
    metric: str,
    scope_label: str,
    base_seed: str,
    replicates: int,
) -> dict[str, Any]:
    repository_values: list[dict[str, Any]] = []
    for repository in repositories:
        positives: list[float] = []
        negatives: list[float] = []
        for row in repository.evaluable_rows:
            value = divergence_value(row, metric)
            if value is None:
                continue
            (positives if row["conflicted"] else negatives).append(value)
        if positives and negatives:
            repository_values.append(
                {
                    "slug": repository.spec.slug,
                    "conflicted": len(positives),
                    "clean": len(negatives),
                    "auc": auc_probability_of_superiority(positives, negatives),
                }
            )

    aucs = [float(row["auc"]) for row in repository_values]
    macro = math.fsum(aucs) / len(aucs) if aucs else None
    return {
        "interpretation": (
            "Within a repository, the probability that a randomly selected conflicted merge "
            "has a larger exposure than a randomly selected clean merge; ties receive half credit."
        ),
        "null": 0.5,
        "informative_repositories": len(repository_values),
        "repository_values": repository_values,
        "macro_equal_repository_auc": macro,
        "repository_bootstrap_95": bootstrap_percentile_interval(
            aucs,
            label=f"auc:{scope_label}:{metric}",
            base_seed=base_seed,
            replicates=replicates,
        ),
        "repositories_above_null": sum(value > 0.5 for value in aucs),
        "repositories_equal_null": sum(value == 0.5 for value in aucs),
        "repositories_below_null": sum(value < 0.5 for value in aucs),
    }


def select_bin(value: float, bins: Sequence[BinSpec]) -> BinSpec:
    for bin_spec in bins:
        if bin_spec.contains(value):
            return bin_spec
    raise ValueError(f"nonnegative value {value} did not fit declared bins")


def divergence_analysis(
    repositories: Sequence[RepositoryData],
    *,
    metric: str,
    bins: Sequence[BinSpec],
    scope_label: str,
    base_seed: str,
    replicates: int,
) -> dict[str, Any]:
    counts: dict[str, dict[str, list[int]]] = {
        bin_spec.key: {
            repository.spec.slug: [0, 0] for repository in repositories
        }
        for bin_spec in bins
    }
    evaluable = 0
    available = 0
    conflicted_available = 0
    unavailable_conflicted = 0
    for repository in repositories:
        for row in repository.evaluable_rows:
            evaluable += 1
            value = divergence_value(row, metric)
            if value is None:
                unavailable_conflicted += int(bool(row["conflicted"]))
                continue
            available += 1
            conflicted_available += int(bool(row["conflicted"]))
            bin_spec = select_bin(value, bins)
            cell = counts[bin_spec.key][repository.spec.slug]
            cell[0] += int(bool(row["conflicted"]))
            cell[1] += 1

    bin_rows: list[dict[str, Any]] = []
    for bin_spec in bins:
        cluster_counts = {
            slug: (values[0], values[1])
            for slug, values in counts[bin_spec.key].items()
        }
        numerator = sum(values[0] for values in cluster_counts.values())
        denominator = sum(values[1] for values in cluster_counts.values())
        cluster_interval, contributors, valid_draws = cluster_rate_interval(
            cluster_counts,
            label=f"bin:{scope_label}:{metric}:{bin_spec.key}",
            base_seed=base_seed,
            replicates=replicates,
        )
        bin_rows.append(
            {
                "key": bin_spec.key,
                "label": bin_spec.label,
                "lower_inclusive": bin_spec.lower,
                "upper_exclusive": bin_spec.upper,
                "conflict_rate": rate(numerator, denominator),
                "wilson_95": wilson_interval(numerator, denominator),
                "repository_cluster_bootstrap_95": cluster_interval,
                "contributing_repositories": contributors,
                "valid_bootstrap_draws": valid_draws,
            }
        )
    return {
        "metric": metric,
        "availability": {
            "evaluable_merges": evaluable,
            "available_merges": available,
            "unavailable_merges": evaluable - available,
            "conflicted_available": conflicted_available,
            "conflicted_unavailable": unavailable_conflicted,
            "available_rate": rate(available, evaluable),
            "unavailable_conflict_rate": rate(
                unavailable_conflicted, evaluable - available
            ),
        },
        "bins": bin_rows,
        "auc": divergence_auc(
            repositories,
            metric=metric,
            scope_label=scope_label,
            base_seed=base_seed,
            replicates=replicates,
        ),
    }


def union_length(intervals: Iterable[tuple[int, int]]) -> int:
    materialized = sorted((start, end) for start, end in intervals if end > start)
    if not materialized:
        return 0
    total = 0
    current_start, current_end = materialized[0]
    for start, end in materialized[1:]:
        if start <= current_end:
            current_end = max(current_end, end)
        else:
            total += current_end - current_start
            current_start, current_end = start, end
    return total + current_end - current_start


def parse_occurrence(
    repository: RepositoryData,
    merge: str,
    conflict: Mapping[str, Any],
    *,
    source: str,
) -> ConflictOccurrence:
    path = conflict.get("path")
    if not isinstance(path, str) or not path:
        raise ValueError(f"{source}: conflict path is absent or invalid")
    classification = conflict.get("classification")
    kind_value = classification.get("kind") if isinstance(classification, Mapping) else None
    kind = str(kind_value).lower() if isinstance(kind_value, str) else "unknown"
    if kind not in KNOWN_KINDS:
        kind = "unknown"
    range_status_value = conflict.get("range_status")
    range_status = str(range_status_value) if range_status_value is not None else "missing"
    if range_status not in {MEASURED_RANGE_STATUS, *UNAVAILABLE_RANGE_STATUSES}:
        raise ValueError(f"{source}: unknown range_status {range_status!r}")
    result_blob = conflict.get("result_blob")
    if result_blob is not None and not isinstance(result_blob, Mapping):
        raise ValueError(f"{source}: result_blob is neither null nor an object")
    size_value = result_blob.get("size") if isinstance(result_blob, Mapping) else None
    oid_value = result_blob.get("oid") if isinstance(result_blob, Mapping) else None
    if isinstance(result_blob, Mapping) and (
        not isinstance(oid_value, str)
        or not oid_value
        or not isinstance(size_value, int)
        or isinstance(size_value, bool)
        or size_value < 0
    ):
        raise ValueError(f"{source}: result_blob oid or size is invalid")
    file_size = (
        int(size_value)
        if isinstance(size_value, int) and not isinstance(size_value, bool) and size_value >= 0
        else None
    )

    unavailable_status = range_status in UNAVAILABLE_RANGE_STATUSES
    intervals: list[tuple[int, int]] = []
    regions_value = conflict.get("regions")
    if not isinstance(regions_value, list):
        raise ValueError(f"{source}: regions is not a list")
    valid_regions = isinstance(regions_value, list) and bool(regions_value)
    if range_status == MEASURED_RANGE_STATUS and not valid_regions:
        raise ValueError(f"{source}: measured range status lacks regions")
    if unavailable_status and valid_regions:
        raise ValueError(f"{source}: unavailable range status has measured regions")
    if range_status == "unavailable_no_result_blob" and result_blob is not None:
        raise ValueError(f"{source}: no-result-blob status unexpectedly has a result blob")
    if (
        range_status in UNAVAILABLE_RANGE_STATUSES - {"unavailable_no_result_blob"}
        and result_blob is None
    ):
        raise ValueError(f"{source}: blob-backed unavailable status lacks a result blob")
    if valid_regions:
        for region_index, region in enumerate(regions_value):
            if not isinstance(region, Mapping):
                raise ValueError(f"{source}: region {region_index} is not an object")
            start = region.get("byte_start")
            end = region.get("byte_end")
            line_start = region.get("line_start")
            line_end = region.get("line_end")
            if (
                not isinstance(start, int)
                or isinstance(start, bool)
                or not isinstance(end, int)
                or isinstance(end, bool)
                or start < 0
                or end <= start
                or (file_size is not None and end > file_size)
                or not isinstance(line_start, int)
                or isinstance(line_start, bool)
                or not isinstance(line_end, int)
                or isinstance(line_end, bool)
                or line_start < 1
                or line_end < line_start
                or region.get("blob_oid") != oid_value
                or region.get("blob_size") != file_size
                or region.get("coordinate_space") != "merge-tree-result-blob"
                or region.get("includes_marker_lines") is not True
            ):
                raise ValueError(f"{source}: region {region_index} has invalid coordinates")
            intervals.append((start, end))
    if valid_regions and result_blob is None:
        raise ValueError(f"{source}: regions exist without a result blob")
    conflicted_bytes = None
    if not unavailable_status and valid_regions and file_size is not None:
        length = union_length(intervals)
        if length > 0:
            conflicted_bytes = length
            reported_bytes = conflict.get("conflicted_bytes")
            if reported_bytes != length:
                raise ValueError(
                    f"{source}: conflicted_bytes {reported_bytes!r} != region union {length}"
                )
            reported_fraction = conflict.get("conflicted_fraction")
            expected_fraction = length / file_size if file_size else None
            if (
                expected_fraction is None
                or not isinstance(reported_fraction, (int, float))
                or isinstance(reported_fraction, bool)
                or not math.isclose(
                    float(reported_fraction), expected_fraction, rel_tol=1e-12, abs_tol=0.0
                )
            ):
                raise ValueError(f"{source}: conflicted_fraction does not reconcile")
    elif conflict.get("conflicted_bytes") is not None or conflict.get("conflicted_fraction") is not None:
        raise ValueError(f"{source}: unavailable regions must not report byte measures")
    return ConflictOccurrence(
        repo=repository.spec.repo,
        slug=repository.spec.slug,
        merge=merge,
        path=path,
        language=repository.spec.language,
        shape=repository.spec.shape,
        kind=kind,
        range_status=range_status,
        file_size=file_size,
        conflicted_bytes=conflicted_bytes,
    )


def collect_occurrences(repositories: Sequence[RepositoryData]) -> list[ConflictOccurrence]:
    occurrences: list[ConflictOccurrence] = []
    for repository in repositories:
        for row_index, row in enumerate(repository.conflict_merges):
            merge = row_merge(row, source=f"{repository.spec.slug} conflict row {row_index + 1}")
            conflicts = row.get("conflicts")
            if not isinstance(conflicts, list) or not conflicts:
                raise ValueError(f"{repository.spec.slug}:{merge}: conflicts list is absent or empty")
            paths: set[str] = set()
            for conflict_index, conflict in enumerate(conflicts):
                if not isinstance(conflict, Mapping):
                    raise ValueError(
                        f"{repository.spec.slug}:{merge}: conflict {conflict_index} is not an object"
                    )
                occurrence = parse_occurrence(
                    repository,
                    merge,
                    conflict,
                    source=f"{repository.spec.slug}:{merge}:conflicts[{conflict_index}]",
                )
                if occurrence.path in paths:
                    raise ValueError(
                        f"{repository.spec.slug}:{merge}: duplicate conflict path {occurrence.path!r}"
                    )
                paths.add(occurrence.path)
                occurrences.append(occurrence)
    return occurrences


def occurrence_kind_matches(occurrence: ConflictOccurrence, stratum: str) -> bool:
    if stratum == "all":
        return True
    if stratum == "artifacts":
        return occurrence.kind in ARTIFACT_KINDS
    return occurrence.kind == stratum


def concentration(
    occurrences: Sequence[ConflictOccurrence],
    *,
    stratum: str,
) -> dict[str, Any]:
    selected = [item for item in occurrences if occurrence_kind_matches(item, stratum)]
    counts = collections.Counter((item.slug, item.path) for item in selected)
    ordered = sorted(
        counts.items(),
        key=lambda item: (
            -item[1],
            item[0][0].encode("utf-8"),
            stable_path_bytes(item[0][1]),
        ),
    )
    distinct = len(ordered)
    k = max(1, math.ceil(0.01 * distinct)) if distinct else 0
    exact = ordered[:k]
    threshold = exact[-1][1] if exact else None
    tie_inclusive = [item for item in ordered if threshold is not None and item[1] >= threshold]
    exact_count = sum(value for _identity, value in exact)
    tie_count = sum(value for _identity, value in tie_inclusive)
    exact_identities = {identity for identity, _value in exact}
    merge_total = len({(item.slug, item.merge) for item in selected})
    merge_hits = len(
        {
            (item.slug, item.merge)
            for item in selected
            if (item.slug, item.path) in exact_identities
        }
    )

    def file_rows(values: Sequence[tuple[tuple[str, str], int]]) -> list[dict[str, Any]]:
        return [
            {"slug": identity[0], "path": identity[1], "occurrences": count}
            for identity, count in values
        ]

    return {
        "stratum": stratum,
        "conflict_file_occurrences": len(selected),
        "distinct_repo_paths": distinct,
        "top_one_percent_file_count": k,
        "rounding_rule": "max(1, ceil(0.01 * distinct repo-qualified paths))",
        "top_one_percent_occurrence_share": rate(exact_count, len(selected)),
        "top_one_percent_merge_coverage": rate(merge_hits, merge_total),
        "top_files": file_rows(exact),
        "tie_inclusive_file_count": len(tie_inclusive),
        "tie_inclusive_occurrence_share": rate(tie_count, len(selected)),
    }


def ratio_distribution(values: Sequence[float]) -> dict[str, Any] | None:
    if not values:
        return None
    return {
        "count": len(values),
        "minimum": min(values),
        "q1": percentile(values, 0.25),
        "median": percentile(values, 0.5),
        "q3": percentile(values, 0.75),
        "p90": percentile(values, 0.9),
        "maximum": max(values),
    }


def granularity(
    occurrences: Sequence[ConflictOccurrence],
    *,
    stratum: str,
) -> dict[str, Any]:
    selected = [item for item in occurrences if occurrence_kind_matches(item, stratum)]
    measurable = [item for item in selected if item.measurable]
    ratios = [item.ratio for item in measurable]
    assert all(value is not None for value in ratios)
    numeric_ratios = [float(value) for value in ratios if value is not None]
    bytes_total = sum(int(item.conflicted_bytes or 0) for item in measurable)
    size_total = sum(int(item.file_size or 0) for item in measurable)
    thresholds = {
        "at_most_1_percent": sum(
            int(item.conflicted_bytes or 0) * 100 <= int(item.file_size or 0)
            for item in measurable
        ),
        "at_most_5_percent": sum(
            int(item.conflicted_bytes or 0) * 20 <= int(item.file_size or 0)
            for item in measurable
        ),
        "at_most_10_percent": sum(
            int(item.conflicted_bytes or 0) * 10 <= int(item.file_size or 0)
            for item in measurable
        ),
        "at_least_50_percent": sum(
            int(item.conflicted_bytes or 0) * 2 >= int(item.file_size or 0)
            for item in measurable
        ),
        "whole_file": sum(
            item.conflicted_bytes == item.file_size for item in measurable
        ),
    }
    by_merge: dict[tuple[str, str], list[int]] = collections.defaultdict(lambda: [0, 0])
    for item in measurable:
        cell = by_merge[(item.slug, item.merge)]
        cell[0] += int(item.conflicted_bytes or 0)
        cell[1] += int(item.file_size or 0)
    merge_ratios = [
        numerator / denominator
        for numerator, denominator in by_merge.values()
        if denominator
    ]
    return {
        "stratum": stratum,
        "all_conflict_file_occurrences": len(selected),
        "measurable_file_occurrences": len(measurable),
        "measurement_coverage": rate(len(measurable), len(selected)),
        "file_ratio_distribution": ratio_distribution(numeric_ratios),
        "weighted_byte_ratio": rate(bytes_total, size_total),
        "threshold_counts": {
            key: rate(value, len(measurable)) for key, value in thresholds.items()
        },
        "measurable_merges": len(by_merge),
        "merge_ratio_distribution": ratio_distribution(merge_ratios),
        "range_status_counts": dict(
            sorted(collections.Counter(item.range_status for item in selected).items())
        ),
    }


def normalize_overlap(value: Any) -> str:
    if isinstance(value, Mapping):
        value = value.get("classification")
    if not isinstance(value, str):
        return "missing"
    normalized = value.strip().lower().replace("-", "_")
    aliases = {
        "strict_overlap": "overlap",
        "overlapping": "overlap",
        "changed_ranges_overlap": "overlap",
        "same_file_no_overlap": "same_file_disjoint",
        "same_file_nonoverlap": "same_file_disjoint",
        "file_only": "same_file_disjoint",
        "disjoint": "same_file_disjoint",
        "boundary_contact": "boundary_only",
        "boundary_mixed": "boundary_with_unclassifiable",
        "unknown": "unclassifiable",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in {
        "overlap",
        "same_file_disjoint",
        "boundary_only",
        "boundary_with_unclassifiable",
        "unclassifiable",
        "mixed_unclassifiable",
    }:
        return "unknown"
    return normalized


def conflict_merge_rows(repositories: Sequence[RepositoryData]) -> list[tuple[RepositoryData, dict[str, Any]]]:
    return [
        (repository, row)
        for repository in repositories
        for row in repository.conflict_merges
    ]


def overlap_summary(repositories: Sequence[RepositoryData]) -> dict[str, Any]:
    rows = conflict_merge_rows(repositories)
    classes = collections.Counter(
        normalize_overlap(row.get("overlap")) for _repository, row in rows
    )
    overlapping = classes["overlap"]
    disjoint = classes["same_file_disjoint"]
    boundary = classes["boundary_only"]
    boundary_mixed = classes["boundary_with_unclassifiable"]
    strict_decidable = overlapping + disjoint + boundary
    boundary_decidable = strict_decidable + boundary_mixed
    path_statuses: collections.Counter[str] = collections.Counter()
    for _repository, row in rows:
        overlap_value = row.get("overlap")
        if not isinstance(overlap_value, Mapping):
            continue
        raw_paths = overlap_value.get("paths")
        if not isinstance(raw_paths, list):
            continue
        for raw_path in raw_paths:
            if isinstance(raw_path, Mapping):
                path_statuses[str(raw_path.get("status", "missing"))] += 1
    return {
        "conflicted_merge_denominator": len(rows),
        "classification_counts": {
            key: classes.get(key, 0)
            for key in (
                "overlap",
                "same_file_disjoint",
                "boundary_only",
                "boundary_with_unclassifiable",
                "unclassifiable",
                "mixed_unclassifiable",
                "missing",
                "unknown",
            )
        },
        "strict_classification_coverage": rate(strict_decidable, len(rows)),
        "strict_overlap_rate": rate(overlapping, strict_decidable),
        "strict_overlap_wilson_95": wilson_interval(overlapping, strict_decidable),
        "boundary_classification_coverage": rate(boundary_decidable, len(rows)),
        "boundary_inclusive_overlap_rate": rate(
            overlapping + boundary + boundary_mixed, boundary_decidable
        ),
        "boundary_inclusive_wilson_95": wilson_interval(
            overlapping + boundary + boundary_mixed, boundary_decidable
        ),
        "path_status_counts": dict(sorted(path_statuses.items())),
        "path_status_denominator": sum(path_statuses.values()),
        "strict_definition": (
            "Nonempty base-coordinate ranges intersect, equal insertion anchors contact, "
            "or an insertion anchor lies strictly inside the other nonempty range."
        ),
    }


def candidate_summary(repositories: Sequence[RepositoryData]) -> dict[str, Any]:
    rows = conflict_merge_rows(repositories)
    candidates = sum(row.get("both_sides_touched_tests") is True for _repository, row in rows)
    missing = sum(
        not isinstance(row.get("both_sides_touched_tests"), bool)
        for _repository, row in rows
    )
    classified = len(rows) - missing
    return {
        "candidate_merges": candidates,
        "conflicted_merge_denominator": len(rows),
        "classified_conflicted_merges": classified,
        "classification_coverage": rate(classified, len(rows)),
        "candidate_rate": rate(candidates, classified),
        "missing_candidate_classification": missing,
        "rule": (
            "A conflicted merge is a candidate when both complete base-to-parent diffs "
            "contain at least one path classified as a test file; the paths need not match."
        ),
    }


def scope_summary(
    repositories: Sequence[RepositoryData],
    occurrences: Sequence[ConflictOccurrence],
    *,
    scope_label: str,
    base_seed: str,
    replicates: int,
) -> dict[str, Any]:
    slugs = {repository.spec.slug for repository in repositories}
    selected_occurrences = [item for item in occurrences if item.slug in slugs]
    granularity_by_stratum = {
        stratum: granularity(selected_occurrences, stratum=stratum)
        for stratum in ("all", "handwritten", "generated", "artifacts")
    }
    return {
        "population": population_summary(repositories),
        "conflict_file_occurrences": len(selected_occurrences),
        "artifact_occurrence_rate": rate(
            sum(item.kind in ARTIFACT_KINDS for item in selected_occurrences),
            len(selected_occurrences),
        ),
        "generated_occurrence_rate": rate(
            sum(item.kind == "generated" for item in selected_occurrences),
            len(selected_occurrences),
        ),
        "classification_unknown": sum(item.kind == "unknown" for item in selected_occurrences),
        "concentration": {
            stratum: concentration(selected_occurrences, stratum=stratum)
            for stratum in ("all", "handwritten", "generated", "artifacts")
        },
        "granularity": granularity_by_stratum["all"],
        "granularity_by_stratum": granularity_by_stratum,
        "overlap": overlap_summary(repositories),
        "candidates": candidate_summary(repositories),
        "divergence_auc": {
            metric: divergence_auc(
                repositories,
                metric=metric,
                scope_label=scope_label,
                base_seed=base_seed,
                replicates=replicates,
            )
            for metric in ("commits", "time", "lines")
        },
    }


def grouped_summaries(
    repositories: Sequence[RepositoryData],
    occurrences: Sequence[ConflictOccurrence],
    *,
    attribute: str,
    base_seed: str,
    replicates: int,
) -> list[dict[str, Any]]:
    groups: dict[str, list[RepositoryData]] = collections.defaultdict(list)
    for repository in repositories:
        groups[str(getattr(repository.spec, attribute))].append(repository)
    rows: list[dict[str, Any]] = []
    for label in sorted(groups, key=lambda value: value.encode("utf-8")):
        selected = groups[label]
        rows.append(
            {
                "label": label,
                "repositories": [repository.spec.slug for repository in selected],
                **scope_summary(
                    selected,
                    occurrences,
                    scope_label=f"{attribute}:{label}",
                    base_seed=base_seed,
                    replicates=replicates,
                ),
            }
        )
    return rows


def collect_methodology(repositories: Sequence[RepositoryData]) -> dict[str, Any]:
    protocol_revisions = sorted(
        {
            value
            for repository in repositories
            if (
                value := first_text(
                    repository.summary,
                    ("miner_protocol_revision",),
                )
            )
        }
    )
    source_hashes = sorted(
        {
            value
            for repository in repositories
            if (value := first_text(repository.summary, ("miner_source_sha256",)))
        }
    )
    python_runtimes = sorted(
        {
            f"{implementation} {version}"
            for repository in repositories
            if (
                implementation := first_text(
                    repository.summary, ("python_implementation",)
                )
            )
            if (version := first_text(repository.summary, ("python_version",)))
        }
    )
    invocations = sorted(
        {
            value
            for repository in repositories
            if (
                value := first_text_or_argv(
                    repository.summary,
                    ("merge_tree_invocation",),
                    ("merge_tree", "invocation"),
                    ("method", "merge_tree_invocation"),
                )
            )
        }
    )
    interpretations = sorted(
        {
            value
            for repository in repositories
            if (
                value := first_text(
                    repository.summary,
                    ("merge_tree_interpretation",),
                    ("merge_tree", "interpretation"),
                    ("method", "merge_tree_interpretation"),
                )
            )
        }
    )
    storage_policies = sorted(
        {
            value
            for repository in repositories
            if (
                value := first_text(
                    repository.summary,
                    ("storage_policy",),
                    ("storage", "policy"),
                )
            )
        }
    )
    git_versions = sorted(
        {
            value
            for repository in repositories
            if (value := first_text(repository.summary, ("git_version",)))
        }
    )
    classification_rules = sorted(
        {
            value
            for repository in repositories
            if (
                value := first_text(
                    repository.summary,
                    ("classification_rule",),
                    ("classification", "rule"),
                    ("method", "classification_rule"),
                )
            )
        }
    )
    test_rules = sorted(
        {
            value
            for repository in repositories
            if (
                value := first_text(
                    repository.summary,
                    ("test_path_rule",),
                    ("tests", "classification_rule"),
                    ("method", "test_path_rule"),
                )
            )
        }
    )
    return {
        "miner_protocol_revisions": protocol_revisions,
        "miner_source_sha256": source_hashes,
        "python_runtimes": python_runtimes,
        "merge_tree_invocations": invocations,
        "merge_tree_interpretations": interpretations,
        "storage_policies": storage_policies,
        "git_versions": git_versions,
        "classification_rules": classification_rules,
        "test_path_rules": test_rules,
        "protocol_revision_complete": len(protocol_revisions) == 1,
        "source_hash_complete": len(source_hashes) == 1,
        "python_runtime_complete": len(python_runtimes) == 1,
        "invocation_complete": len(invocations) == 1,
        "interpretation_complete": len(interpretations) == 1,
        "storage_policy_complete": len(storage_policies) == 1,
        "classification_rule_complete": len(classification_rules) == 1,
        "test_path_rule_complete": len(test_rules) == 1,
    }


def find_determinism_report(corpus: Path, explicit: Path | None) -> tuple[Path | None, Mapping[str, Any] | None]:
    candidates = [explicit] if explicit is not None else [
        corpus / "DETERMINISM.json",
        corpus / "determinism.json",
        corpus / "_determinism.json",
        corpus / "_summaries" / "determinism.json",
    ]
    for path in candidates:
        if path is not None and path.exists():
            value = read_json(path)
            if not isinstance(value, Mapping):
                raise ValueError(f"{path}: determinism report is not an object")
            if "schema_version" in value and value.get("schema_version") != 1:
                raise ValueError(f"{path}: schema_version must be 1")
            return path, value
    return None, None


def portable_path(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        return path.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()
    except ValueError:
        return path.name


def summarize_preparation(value: Mapping[str, Any]) -> dict[str, Any]:
    """Keep preparation evidence while removing machine-specific absolute paths."""

    raw_results = value.get("results")
    results = raw_results if isinstance(raw_results, list) else []
    status_counts: collections.Counter[str] = collections.Counter()
    mode_counts: collections.Counter[str] = collections.Counter()
    failures: list[dict[str, str]] = []
    independent_verified = 0
    for raw in results:
        if not isinstance(raw, Mapping):
            continue
        status = str(raw.get("status", "unknown"))
        status_counts[status] += 1
        mode = raw.get("clone_mode")
        if isinstance(mode, str):
            mode_counts[mode] += 1
        if status == "failed":
            failures.append(
                {
                    "repo": str(raw.get("repo", "unknown")),
                    "slug": str(raw.get("slug", "unknown")),
                    "error": str(raw.get("error", "not recorded")),
                }
            )
        verification = raw.get("verification")
        if isinstance(verification, Mapping) and (
            verification.get("bare") is True
            and verification.get("promisor") is True
            and verification.get("alternates") is False
            and verification.get("partial_clone_filter") == "blob:none"
            and isinstance(verification.get("pinned_commit"), str)
        ):
            independent_verified += 1
    return {
        "repository_count": len(results),
        "status_counts": dict(sorted(status_counts.items())),
        "clone_mode_counts": dict(sorted(mode_counts.items())),
        "failed_repositories": failures,
        "independent_bare_partial_mirrors_verified": independent_verified,
    }


def summarize_hydration(value: Mapping[str, Any]) -> dict[str, Any]:
    """Summarize the final object-availability audit without machine paths."""

    raw_results = value.get("results")
    results = raw_results if isinstance(raw_results, list) else []
    statuses: collections.Counter[str] = collections.Counter()
    missing_before = 0
    missing_after = 0
    complete = 0
    fetch_batches = 0
    for raw in results:
        if not isinstance(raw, Mapping):
            continue
        status = str(raw.get("status", "unknown"))
        statuses[status] += 1
        before = raw.get("missing_before_count")
        after = raw.get("missing_after_count")
        batches = raw.get("fetch_batch_count")
        if isinstance(before, int) and not isinstance(before, bool):
            missing_before += before
        if isinstance(after, int) and not isinstance(after, bool):
            missing_after += after
        if isinstance(batches, int) and not isinstance(batches, bool):
            fetch_batches += batches
        if status != "failed" and after == 0:
            complete += 1
    return {
        "repository_count": len(results),
        "status_counts": dict(sorted(statuses.items())),
        "repositories_complete_after": complete,
        "missing_before_count": missing_before,
        "missing_after_count": missing_after,
        "fetch_batch_count": fetch_batches,
        "discovery_lazy_fetch": value.get("discovery_lazy_fetch"),
    }


def build_metrics(
    repositories: Sequence[RepositoryData],
    *,
    corpus: Path,
    repositories_path: Path,
    determinism_path: Path | None,
    determinism: Mapping[str, Any] | None,
    base_seed: str,
    replicates: int,
) -> dict[str, Any]:
    occurrences = collect_occurrences(repositories)
    occurrence_counts = collections.Counter(item.slug for item in occurrences)
    for repository in repositories:
        stored = first_count(repository.summary, ("conflict_file_occurrences",))
        if stored is not None and stored != occurrence_counts[repository.spec.slug]:
            raise ValueError(
                f"{repository.spec.slug}: summary conflict-file count {stored} != "
                f"JSONL-derived {occurrence_counts[repository.spec.slug]}"
            )
    overall = scope_summary(
        repositories,
        occurrences,
        scope_label="overall",
        base_seed=base_seed,
        replicates=replicates,
    )
    return {
        "schema_version": 1,
        "inputs": {
            "repositories": portable_path(repositories_path),
            "corpus": portable_path(corpus),
            "determinism_report": portable_path(determinism_path),
        },
        "protocol": {
            "population_unit": (
                "Exactly-two-parent merge commits in first-parent-reachable history "
                "from each frozen repository tip."
            ),
            "conflict_rate_denominator": (
                "Merges whose evaluation_status is not a failure and whose conflicted field is boolean."
            ),
            "zero_denominator": "undefined; never rendered as zero percent",
            "top_one_percent_rule": (
                "One (repo, merge, path) occurrence per conflicted path; repo-qualified paths "
                "rank by descending occurrence count with UTF-8 slug/path tie breaks; "
                "k=max(1,ceil(1% of distinct paths))."
            ),
            "artifact_aggregate": sorted(ARTIFACT_KINDS),
            "range_coordinates": "zero-based half-open byte ranges in result_blob",
            "divergence": {
                "commits": "divergence.combined_commits = side 1 + side 2",
                "time": (
                    "divergence.max_wall_clock_seconds; rows flagged negative_clock are unavailable"
                ),
                "lines": (
                    "parent1.lines_changed + parent2.lines_changed only when neither diff has binary files"
                ),
            },
            "bins": {
                "commits": [dataclasses.asdict(item) for item in COMMIT_BINS],
                "time": [dataclasses.asdict(item) for item in TIME_BINS],
                "lines": [dataclasses.asdict(item) for item in LINE_BINS],
            },
            "wilson_z": Z_95,
            "bootstrap": {
                "replicates": replicates,
                "base_seed": base_seed,
                "seed_derivation": "first 128 bits SHA-256(base seed + NUL + statistic label)",
                "rng": "Python random.Random",
                "interval": "linear 2.5th/97.5th percentiles",
                "cluster": "whole repository",
            },
        },
        "methodology_from_miner": collect_methodology(repositories),
        "determinism": dict(determinism) if determinism is not None else None,
        "determinism_validation": determinism_status(determinism, corpus),
        "repository_population": [
            repository_population_row(repository) for repository in repositories
        ],
        "overall": overall,
        "divergence": {
            "commits": divergence_analysis(
                repositories,
                metric="commits",
                bins=COMMIT_BINS,
                scope_label="overall",
                base_seed=base_seed,
                replicates=replicates,
            ),
            "time": divergence_analysis(
                repositories,
                metric="time",
                bins=TIME_BINS,
                scope_label="overall",
                base_seed=base_seed,
                replicates=replicates,
            ),
            "lines": divergence_analysis(
                repositories,
                metric="lines",
                bins=LINE_BINS,
                scope_label="overall",
                base_seed=base_seed,
                replicates=replicates,
            ),
        },
        "concentration": {
            stratum: concentration(occurrences, stratum=stratum)
            for stratum in (
                "all",
                "handwritten",
                "artifacts",
                "generated",
                "lockfile",
                "vendored",
                "unknown",
            )
        },
        "granularity": {
            stratum: granularity(occurrences, stratum=stratum)
            for stratum in (
                "all",
                "handwritten",
                "artifacts",
                "generated",
                "lockfile",
                "vendored",
                "unknown",
            )
        },
        "overlap": overlap_summary(repositories),
        "candidates": candidate_summary(repositories),
        "breakdowns": {
            "language": grouped_summaries(
                repositories,
                occurrences,
                attribute="language",
                base_seed=base_seed,
                replicates=replicates,
            ),
            "shape": grouped_summaries(
                repositories,
                occurrences,
                attribute="shape",
                base_seed=base_seed,
                replicates=replicates,
            ),
        },
        "repository_details": [
            {
                "slug": repository.spec.slug,
                "repo": repository.spec.repo,
                **scope_summary(
                    [repository],
                    occurrences,
                    scope_label=f"repository:{repository.spec.slug}",
                    base_seed=base_seed,
                    replicates=replicates,
                ),
            }
            for repository in repositories
        ],
    }


def format_integer(value: Any) -> str:
    return f"{value:,}" if isinstance(value, int) and not isinstance(value, bool) else "not observed"


def format_rate(value: Mapping[str, Any] | None) -> str:
    if value is None:
        return "not observed"
    numerator = int(value["numerator"])
    denominator = int(value["denominator"])
    proportion = value.get("value")
    if denominator == 0 or proportion is None:
        return f"{numerator:,} / {denominator:,} (undefined)"
    return f"{numerator:,} / {denominator:,} ({float(proportion):.3%})"


def format_interval(value: Sequence[float] | None) -> str:
    if value is None:
        return "not estimable"
    return f"[{float(value[0]):.3%}, {float(value[1]):.3%}]"


def format_number_interval(value: Sequence[float] | None) -> str:
    if value is None:
        return "not estimable"
    return f"[{float(value[0]):.3f}, {float(value[1]):.3f}]"


def format_percent(value: Any) -> str:
    return f"{float(value):.3%}" if is_number(value) else "not observed"


def markdown_escape(value: Any) -> str:
    safe = str(value).encode("utf-8", errors="backslashreplace").decode("utf-8")
    return safe.replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def methodology_text(values: Sequence[str], missing: str) -> str:
    if not values:
        return f"**[{missing} was not supplied by the miner summaries]**"
    return "; ".join(f"`{markdown_escape(value)}`" for value in values)


def determinism_status(
    determinism: Mapping[str, Any] | None,
    corpus: Path | None = None,
) -> dict[str, Any]:
    if determinism is None:
        return {
            "passed": False,
            "reported": False,
            "repository_count": 0,
            "byte_identical_count": 0,
            "canonical_byte_identical_count": 0,
            "reason": "No determinism report was supplied.",
        }
    repositories = determinism.get("repositories")
    rows = repositories if isinstance(repositories, list) else []
    problems: list[str] = []
    if determinism.get("schema_version") != 1:
        problems.append("schema_version is not 1")
    if determinism.get("miner_protocol_revision") != MINER_PROTOCOL_REVISION:
        problems.append("miner protocol revision is stale or absent")
    if determinism.get("miner_source_sha256") != MINER_SOURCE_SHA256:
        problems.append("miner source hash is stale or absent")
    if not isinstance(repositories, list):
        problems.append("repositories is not a list")

    slugs: list[str] = []
    for position, raw in enumerate(rows, 1):
        if not isinstance(raw, Mapping):
            problems.append(f"repository row {position} is not an object")
            continue
        slug = raw.get("slug")
        if not isinstance(slug, str) or not slug:
            problems.append(f"repository row {position} has no slug")
            continue
        slugs.append(slug)
        expected_paths = {
            f"{slug}.jsonl",
            f"_all_merges/{slug}.jsonl",
            f"_summaries/{slug}.json",
        }
        raw_files = raw.get("files")
        files = raw_files if isinstance(raw_files, list) else []
        paths = [
            item.get("path")
            for item in files
            if isinstance(item, Mapping) and isinstance(item.get("path"), str)
        ]
        if len(files) != 3 or len(paths) != 3 or set(paths) != expected_paths:
            problems.append(f"{slug} does not contain exactly the three expected artifacts")
            continue
        for item in files:
            if not isinstance(item, Mapping):
                problems.append(f"{slug} contains a non-object file result")
                continue
            path = str(item["path"])
            first_hash = item.get("first_sha256")
            second_hash = item.get("second_sha256")
            canonical_hash = item.get("canonical_sha256")
            if (
                item.get("byte_identical") is not True
                or item.get("canonical_byte_identical") is not True
                or not isinstance(first_hash, str)
                or first_hash != second_hash
                or first_hash != canonical_hash
            ):
                problems.append(f"{slug}:{path} does not prove three-way byte identity")
            if corpus is not None:
                canonical_path = corpus / Path(path)
                if not canonical_path.is_file():
                    problems.append(f"{slug}:{path} is absent from the canonical corpus")
                else:
                    if sha256_file(canonical_path) != canonical_hash:
                        problems.append(f"{slug}:{path} canonical hash is stale")
                    size = item.get("size")
                    if not isinstance(size, int) or isinstance(size, bool) or size != canonical_path.stat().st_size:
                        problems.append(f"{slug}:{path} canonical size is stale")

    if len(slugs) != len(set(slugs)):
        problems.append("repository slugs are not unique")
    known_case = determinism.get("known_case")
    if not isinstance(known_case, Mapping) or known_case.get("byte_identical") is not True or known_case.get("exit_codes") != [1, 1]:
        problems.append("known conflict control did not reproduce exit status 1 twice")
    known_clean_case = determinism.get("known_clean_case")
    if not isinstance(known_clean_case, Mapping) or known_clean_case.get("byte_identical") is not True or known_clean_case.get("exit_codes") != [0, 0]:
        problems.append("known clean control did not reproduce exit status 0 twice")
    identical = sum(
        isinstance(row, Mapping) and row.get("byte_identical") is True for row in rows
    )
    canonical_identical = sum(
        isinstance(row, Mapping)
        and row.get("canonical_byte_identical") is True
        for row in rows
    )
    all_identical = determinism.get("all_byte_identical") is True
    passed = (
        all_identical
        and identical >= 3
        and canonical_identical >= 3
        and len(slugs) >= 3
        and not problems
    )
    return {
        "passed": passed,
        "reported": True,
        "repository_count": len(rows),
        "byte_identical_count": identical,
        "canonical_byte_identical_count": canonical_identical,
        "problems": problems,
        "reason": (
            "At least three repositories had two full --no-resume miner runs in "
            "independent output roots; all three artifact files were byte-identical "
            "between runs, and every reported rerun matched its canonical corpus artifacts."
            if passed
            else (
                "The report does not establish byte-identical full reruns on at least three "
                "repositories under the current frozen miner: " + "; ".join(problems)
                if problems
                else "The report does not establish byte-identical full reruns on at least three repositories."
            )
        ),
    }


def determinism_disk_text(determinism: Mapping[str, Any] | None) -> str:
    if determinism is None:
        return "**[disk usage was not supplied by the determinism report]**"
    value = nested(
        determinism,
        ("total_disk_bytes",),
        ("total_disk_used_bytes",),
        ("total_logical_bytes",),
        ("disk_bytes",),
        ("disk_usage",),
        ("disk_usage_notes",),
        ("storage", "total_disk_bytes"),
        ("storage", "total_bytes"),
        ("storage", "disk_usage"),
        ("disk", "total_bytes"),
        ("storage_notes",),
    )
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return f"{value:,} bytes ({value / (1024 * 1024):,.2f} MiB)"
    if isinstance(value, str):
        return markdown_escape(value)
    if value is not None:
        return f"`{markdown_escape(compact_json(value))}`"
    return "**[disk usage was not supplied by the determinism report]**"


def auc_verdict(auc: Mapping[str, Any]) -> str:
    interval = auc.get("repository_bootstrap_95")
    informative = int(auc.get("informative_repositories", 0))
    if not isinstance(interval, list) or len(interval) != 2:
        return f"No directional claim: only {informative} informative repositories or no stable interval."
    if interval[0] > 0.5:
        return "Positive within-repository rank association in this selected corpus; this is not causal."
    if interval[1] < 0.5:
        return "Negative within-repository rank association in this selected corpus; this is not causal."
    return "No directional claim: the repository-bootstrap interval includes the 0.5 null."


def distribution_cell(distribution: Mapping[str, Any] | None, key: str) -> str:
    if distribution is None:
        return "not observed"
    return format_percent(distribution.get(key))


def granularity_verdict_text(row: Mapping[str, Any], label: str) -> str:
    coverage = row.get("measurement_coverage")
    distribution = row.get("file_ratio_distribution")
    if not isinstance(coverage, Mapping) or not isinstance(distribution, Mapping):
        coverage_text = format_rate(coverage if isinstance(coverage, Mapping) else None)
        return (
            f"{label}: no directional granularity verdict is supported; measurable marker "
            f"coverage was {coverage_text}."
        )
    median = distribution.get("median")
    if not is_number(median):
        return f"{label}: no directional granularity verdict is supported."
    median_value = float(median)
    if median_value <= 0.10:
        direction = (
            "the measurable distribution favors localized, byte-level coordination over "
            "whole-file exclusion"
        )
    elif median_value >= 0.50:
        direction = (
            "the measurable distribution favors file-scale coordination over a tiny-region "
            "interpretation"
        )
    else:
        direction = (
            "the measurable distribution is intermediate and does not cleanly favor either "
            "tiny-region or whole-file coordination"
        )
    thresholds = row.get("threshold_counts")
    threshold_map = thresholds if isinstance(thresholds, Mapping) else {}
    return (
        f"{label}: {direction}. Measurable coverage was {format_rate(coverage)}; the "
        f"marker-inclusive median was {format_percent(median_value)}; "
        f"{format_rate(threshold_map.get('at_most_10_percent'))} measurable files were at "
        f"most 10%, {format_rate(threshold_map.get('at_least_50_percent'))} were at least "
        f"50%, and {format_rate(threshold_map.get('whole_file'))} were whole-file."
    )


def compact_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def render_known_case(known_case: Mapping[str, Any]) -> list[str]:
    parents = known_case.get("parents")
    parent_text = (
        ", ".join(f"`{markdown_escape(value)}`" for value in parents)
        if isinstance(parents, list)
        else "not recorded"
    )
    paths = known_case.get("conflict_paths")
    path_text = (
        ", ".join(f"`{markdown_escape(value)}`" for value in paths)
        if isinstance(paths, list)
        else "not recorded"
    )
    invocation = known_case.get("repository_root_invocation")
    invocation_text = (
        "argv=" + compact_json(invocation)
        if isinstance(invocation, list)
        else str(invocation or "not recorded")
    )
    hashes = known_case.get("output_sha256")
    hash_text = (
        compact_json(hashes) if isinstance(hashes, list) else str(hashes or "not recorded")
    )
    lines = [
        "## Known-case protocol check",
        "",
        (
            f"Repository `{markdown_escape(known_case.get('repo', 'not recorded'))}`, merge "
            f"`{markdown_escape(known_case.get('merge', 'not recorded'))}`, parents {parent_text}, "
            f"merge base `{markdown_escape(known_case.get('merge_base', 'not recorded'))}`."
        ),
        "",
        f"Exact repository-root argv: `{markdown_escape(invocation_text)}`",
        "",
        f"Interpretation: {markdown_escape(known_case.get('interpretation', 'not recorded'))}.",
        "",
        (
            f"Observed statuses `{compact_json(known_case.get('exit_codes'))}`; stdout was "
            f"{format_integer(known_case.get('output_size'))} bytes; stderr was "
            f"{format_integer(known_case.get('stderr_size'))} bytes; raw output byte-identical: "
            f"`{str(known_case.get('byte_identical')).lower()}`; stdout SHA-256 values: "
            f"`{markdown_escape(hash_text)}`."
        ),
        "",
        f"Conflict paths: {path_text}.",
        "",
        "| Path | Classification | Result blob bytes | Conflict ranges (1-based lines; 0-based half-open bytes) |",
        "|---|---|---:|---|",
    ]
    conflicts = known_case.get("conflicts")
    conflict_rows = conflicts if isinstance(conflicts, list) else []
    for raw in conflict_rows:
        if not isinstance(raw, Mapping):
            continue
        classification = raw.get("classification")
        kind = classification.get("kind") if isinstance(classification, Mapping) else "unknown"
        result_blob = raw.get("result_blob")
        size = result_blob.get("size") if isinstance(result_blob, Mapping) else None
        raw_regions = raw.get("regions")
        regions = raw_regions if isinstance(raw_regions, list) else []
        region_text = "; ".join(
            f"L{region.get('line_start')}-L{region.get('line_end')}, "
            f"B[{region.get('byte_start')},{region.get('byte_end')})"
            for region in regions
            if isinstance(region, Mapping)
        ) or markdown_escape(raw.get("range_status", "unavailable"))
        lines.append(
            f"| `{markdown_escape(raw.get('path', 'unknown'))}` | `{markdown_escape(kind)}` | "
            f"{format_integer(size)} | {markdown_escape(region_text)} |"
        )
    if not conflict_rows:
        lines.append("| not recorded | not recorded | not observed | not recorded |")
    lines.append("")
    return lines


def render_breakdown_table(title: str, rows: Sequence[Mapping[str, Any]]) -> list[str]:
    lines = [
        f"### {title}",
        "",
        "| Group | Repos | First-parent commits | Exact 2-parent / FP commits | Evaluable / exact 2-parent | Failures | No base | Conflicted / evaluable |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        population = row["population"]
        lines.append(
            f"| {markdown_escape(row['label'])} | {population['repository_count']:,} | "
            f"{format_integer(population['first_parent_commits'])} | "
            f"{format_rate(population['merge_prevalence'])} | "
            f"{population['evaluable_merges']:,} / {population['two_parent_merges']:,} | "
            f"{population['evaluation_failures']:,} | {population['no_merge_base_merges']:,} | "
            f"{format_rate(population['conflict_rate'])} |"
        )
    lines.extend(
        [
            "",
            "| Group | Generated conflict-file occurrences / all | Artifact conflict-file occurrences / all | All top 1% | Handwritten top 1% | Generated top 1% | Artifact top 1% | All measurable / median | Handwritten measurable / median | Generated measurable / median | Artifact measurable / median | Strict overlap / strict-decidable conflicted merges | Boundary-inclusive overlap / boundary-decidable conflicted merges | Both-tests candidates / classified conflicted merges | AUC commits / time / lines |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    for row in rows:
        concentration_rows = row["concentration"]
        granularity_row = row["granularity"]
        granularity_strata = row["granularity_by_stratum"]
        handwritten_granularity = granularity_strata["handwritten"]
        generated_granularity = granularity_strata["generated"]
        artifact_granularity = granularity_strata["artifacts"]
        overlap = row["overlap"]
        candidates = row["candidates"]
        aucs = row["divergence_auc"]
        auc_cells: list[str] = []
        for metric in ("commits", "time", "lines"):
            auc = aucs[metric]
            if auc["macro_equal_repository_auc"] is None:
                auc_cells.append("n/e")
            else:
                auc_cells.append(
                    f"{auc['macro_equal_repository_auc']:.3f} "
                    f"{format_number_interval(auc['repository_bootstrap_95'])}"
                )
        auc_text = " / ".join(auc_cells)
        lines.append(
            f"| {markdown_escape(row['label'])} | {format_rate(row['generated_occurrence_rate'])} | "
            f"{format_rate(row['artifact_occurrence_rate'])} | "
            f"{format_rate(concentration_rows['all']['top_one_percent_occurrence_share'])} | "
            f"{format_rate(concentration_rows['handwritten']['top_one_percent_occurrence_share'])} | "
            f"{format_rate(concentration_rows['generated']['top_one_percent_occurrence_share'])} | "
            f"{format_rate(concentration_rows['artifacts']['top_one_percent_occurrence_share'])} | "
            f"{format_rate(granularity_row['measurement_coverage'])}; median "
            f"{distribution_cell(granularity_row['file_ratio_distribution'], 'median')} | "
            f"{format_rate(handwritten_granularity['measurement_coverage'])}; median "
            f"{distribution_cell(handwritten_granularity['file_ratio_distribution'], 'median')} | "
            f"{format_rate(generated_granularity['measurement_coverage'])}; median "
            f"{distribution_cell(generated_granularity['file_ratio_distribution'], 'median')} | "
            f"{format_rate(artifact_granularity['measurement_coverage'])}; median "
            f"{distribution_cell(artifact_granularity['file_ratio_distribution'], 'median')} | "
            f"{format_rate(overlap['strict_overlap_rate'])} | "
            f"{format_rate(overlap['boundary_inclusive_overlap_rate'])} | "
            f"{format_rate(candidates['candidate_rate'])} | {auc_text} |"
        )
    lines.append("")
    return lines


def render_markdown(metrics: Mapping[str, Any]) -> str:
    population = metrics["overall"]["population"]
    repository_rows = metrics["repository_population"]
    methodology = metrics["methodology_from_miner"]
    determinism = metrics.get("determinism")
    stored_det_status = metrics.get("determinism_validation")
    det_status = (
        dict(stored_det_status)
        if isinstance(stored_det_status, Mapping)
        else determinism_status(
            determinism if isinstance(determinism, Mapping) else None
        )
    )
    preparation = metrics.get("preparation")
    hydration = metrics.get("hydration")
    reclassification = metrics.get("reclassification")
    repository_count = int(population["repository_count"])
    conflict_rate = population["conflict_rate"]
    merge_prevalence = population.get("merge_prevalence")

    lines = [
        "# Historical merge-conflict mining",
        "",
        "## Coverage caveat",
        "",
        (
            f"This report describes {repository_count:,} deliberately selected repositories at "
            "frozen tips, chosen to span languages and project shapes. They are not a probability "
            "sample. Every conflict rate is conditional on exactly-two-parent merges in "
            "first-parent-reachable history and on a recognized `merge-tree` evaluation result. "
            "Operational failures are not counted as clean."
        ),
        "",
        "## Verdict and base rate",
        "",
        (
            f"The miner found **{format_rate(conflict_rate)} conflicted merges**, denominator "
            f"`D_evaluable={population['evaluable_merges']:,}`, among "
            f"`D_2p={population['two_parent_merges']:,}` exactly-two-parent merges. "
            f"There were {population['evaluation_failures']:,} / "
            f"{population['two_parent_merges']:,} unevaluable merges, including "
            f"{population['no_merge_base_merges']:,} / {population['two_parent_merges']:,} "
            "whose parents had no merge base. "
            f"All first-parent merge commits: {format_integer(population['first_parent_merges'])}; "
            f"excluded octopus merges: {format_integer(population['excluded_octopus_merges'])}."
        ),
        "",
    ]
    if merge_prevalence is not None:
        lines.extend(
            [
                (
                    f"Exactly-two-parent merges were {format_rate(merge_prevalence)} of "
                    "first-parent-reachable commits; this workflow denominator includes repositories "
                    "with zero eligible merges."
                ),
                "",
            ]
        )

    lines.extend(
        [
            "## Deterministic method",
            "",
            (
                "Canonical merge-tree argv template recorded by the miner (`-C`'s portable "
                "placeholder denotes each frozen task-owned bare mirror; the concrete Click "
                "repository-root argv appears below): "
            )
            + methodology_text(methodology["merge_tree_invocations"], "invocation"),
            "",
            "Miner protocol revision: "
            + methodology_text(methodology["miner_protocol_revisions"], "revision"),
            "",
            "Miner source SHA-256 embedded in every row and summary: "
            + methodology_text(methodology["miner_source_sha256"], "source hash"),
            "",
            "Miner Python runtime(s): "
            + methodology_text(methodology["python_runtimes"], "Python runtime"),
            "",
            (
                "Process environment fixed by the miner: `GIT_CONFIG_GLOBAL` points at the "
                "platform null file; system Git configuration and system attributes are "
                "disabled; every ambient `GIT_*` variable is scrubbed before the listed fixed "
                "overrides are applied; "
                "promisor lazy fetching and replace refs are disabled; `LANG=C`, `LC_ALL=C`, "
                "and `TZ=UTC`."
            ),
            "",
            "Exact fixed environment overrides: `"
            + markdown_escape(
                compact_json(dict(sorted(DETERMINISTIC_GIT_ENVIRONMENT_OVERRIDES.items())))
            )
            + "`; scrubbed inherited prefixes: `"
            + markdown_escape(compact_json(list(SCRUBBED_GIT_ENVIRONMENT_PREFIXES)))
            + "`.",
            "",
            "Exit/output interpretation recorded by the miner: "
            + methodology_text(methodology["merge_tree_interpretations"], "interpretation"),
            "",
            (
                "Side-diff and divergence anchor: the single stdout object from "
                "`git merge-base P1 P2`; `merge-base --all` was also recorded as an audit. "
                f"Git reported multiple best bases for {format_integer(population.get('multiple_merge_base_merges'))} "
                "eligible merges. `merge-tree` itself computed and, when needed, recursively "
                "merged its own bases."
            ),
            "",
            "Repository storage/mutation policy: "
            + methodology_text(methodology["storage_policies"], "storage policy"),
            "",
            "Git version(s): " + methodology_text(methodology["git_versions"], "Git version"),
            "",
            (
                "Determinism verification runtime: "
                + (
                    f"{markdown_escape(determinism.get('python_implementation', 'unknown'))} "
                    f"{markdown_escape(determinism.get('python_version', 'unknown'))} on "
                    f"{markdown_escape(determinism.get('platform', 'unknown'))}."
                    if isinstance(determinism, Mapping)
                    else "not recorded."
                )
            ),
            "",
            (
                f"Determinism check: **{'PASS' if det_status['passed'] else 'NOT VERIFIED'}**. "
                f"{det_status['byte_identical_count']:,} / {det_status['repository_count']:,} "
                f"reported repository reruns were byte-identical. {det_status['reason']}"
            ),
            "",
            "Total corpus mirror/output disk usage: "
            + determinism_disk_text(determinism if isinstance(determinism, Mapping) else None),
            "",
        ]
    )
    if isinstance(determinism, Mapping):
        determinism_rows = determinism.get("repositories")
        reruns = determinism_rows if isinstance(determinism_rows, list) else []
        lines.extend(
            [
                (
                    "For each repository below, the verifier ran the full miner twice with "
                    "`--no-resume` into independent `run1` and `run2` output roots. It compared "
                    "the conflict JSONL, `_all_merges` JSONL, and summary JSON byte-for-byte "
                    "between runs and against the canonical corpus."
                ),
                "",
                "| Repository slug | All-merge rows | Conflict rows | run1 == run2 | run1 == canonical |",
                "|---|---:|---:|---:|---:|",
            ]
        )
        for raw in reruns:
            if not isinstance(raw, Mapping):
                continue
            lines.append(
                f"| `{markdown_escape(raw.get('slug', 'unknown'))}` | "
                f"{format_integer(raw.get('all_merge_rows'))} | "
                f"{format_integer(raw.get('conflict_rows'))} | "
                f"`{str(raw.get('byte_identical') is True).lower()}` | "
                f"`{str(raw.get('canonical_byte_identical') is True).lower()}` |"
            )
        lines.extend(
            [
                "",
                (
                    "Disk figure rule: "
                    + markdown_escape(
                        determinism.get("disk_measurement_rule", "not recorded")
                    )
                    + ". It is a logical file-length total, not allocated filesystem blocks."
                ),
                "",
            ]
        )
    if isinstance(preparation, Mapping):
        counts = preparation.get("status_counts")
        count_map = counts if isinstance(counts, Mapping) else {}
        prepared = int(count_map.get("prepared", 0)) + int(count_map.get("reused", 0))
        failed = int(count_map.get("failed", 0))
        total = int(preparation.get("repository_count", prepared + failed))
        modes = preparation.get("clone_mode_counts")
        mode_map = modes if isinstance(modes, Mapping) else {}
        reference_count = int(mode_map.get("reference-and-dissociate", 0))
        direct_count = int(mode_map.get("direct", 0))
        verified = int(preparation.get("independent_bare_partial_mirrors_verified", 0))
        lines.extend(
            [
                (
                    f"Mirror preparation: {prepared:,} / {total:,} repositories prepared or "
                    f"reused, with {failed:,} clone failures; {reference_count:,} used local "
                    f"reference-plus-dissociation and {direct_count:,} used a direct "
                    "`--mirror --filter=blob:none` clone. All mining occurred in these "
                    f"task-owned bare mirrors; {verified:,} / {total:,} were verified bare, "
                    "promisor-enabled, dissociated, pinned mirrors."
                ),
                "",
            ]
        )
    if isinstance(hydration, Mapping):
        hydration_repositories = int(hydration.get("repository_count", 0))
        complete_after = int(hydration.get("repositories_complete_after", 0))
        missing_before = int(hydration.get("missing_before_count", 0))
        missing_after = int(hydration.get("missing_after_count", 0))
        fetch_batches = int(hydration.get("fetch_batch_count", 0))
        discovery_no_lazy = hydration.get("discovery_lazy_fetch") is False
        lines.extend(
            [
                (
                    f"Final object-availability audit: {complete_after:,} / "
                    f"{hydration_repositories:,} mirrors had zero required objects missing "
                    f"after the audit; {missing_before:,} required objects were missing before "
                    f"it and {missing_after:,} after it, across {fetch_batches:,} explicit fetch "
                    f"batches. Discovery lazy fetching disabled: "
                    f"`{str(discovery_no_lazy).lower()}`."
                ),
                "",
            ]
        )
    if isinstance(reclassification, Mapping):
        changed = int(
            reclassification.get("total_changed_conflict_file_occurrences", 0)
        )
        revision = reclassification.get("classification_revision", "not recorded")
        classifier_text = (
            f"Classifier migration audit: {changed:,} deterministic conflict-file "
            f"relabeling events were recorded through `{markdown_escape(revision)}`; "
            "merge outcomes, paths, ranges, diffs, and denominators were unchanged."
            if changed
            else (
                f"Final classifier audit: 0 canonical conflict-file rows required migration "
                f"under `{markdown_escape(revision)}` because the row-one rerun already used "
                "the final classifier. Earlier development audits had found omitted Hugo "
                "`_vendor/` and generated-resource `_gen/` spellings."
            )
        )
        lines.extend(
            [
                classifier_text,
                "",
            ]
        )
    if isinstance(determinism, Mapping):
        known_case = determinism.get("known_case")
        if isinstance(known_case, Mapping):
            lines.extend(render_known_case(known_case))
        known_clean_case = determinism.get("known_clean_case")
        if isinstance(known_clean_case, Mapping):
            lines.extend(
                [
                    (
                        "Clean-case control: `"
                        f"{markdown_escape(known_clean_case.get('repo', 'not recorded'))}` merge "
                        f"`{markdown_escape(known_clean_case.get('merge', 'not recorded'))}` returned "
                        f"statuses `{compact_json(known_clean_case.get('exit_codes'))}`, "
                        f"{format_integer(known_clean_case.get('stage_count'))} stages, "
                        f"{format_integer(known_clean_case.get('message_count'))} messages, and "
                        f"{format_integer(known_clean_case.get('stderr_size'))} stderr bytes; its "
                        "two raw outputs were byte-identical."
                    ),
                    "",
                ]
            )

    lines.extend(
        [
            "## Repository selection and coverage",
            "",
            "Repositories were chosen for language and project-shape coverage rather than popularity.",
            "",
            "| Repository | Frozen tip | Primary language | Primary shape | Coverage reason |",
            "|---|---|---|---|---|",
        ]
    )
    for row in repository_rows:
        lines.append(
            f"| `{markdown_escape(row['repo'])}` | `{markdown_escape(row['head'] or 'not supplied')}` | "
            f"{markdown_escape(row['language'])} | {markdown_escape(row['shape'])} | "
            f"{markdown_escape(row['selection_note'] or 'not supplied')} |"
        )
    lines.extend(
        [
            "",
            "## Repository denominators",
            "",
            "| Repository | Language | Shape | First-parent commits | All FP merges | Exact 2-parent | Octopus excluded | Evaluable | Failures | No base | Conflicted / evaluable | Both-tests / conflicted |",
            "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in repository_rows:
        candidate_row = next(
            (
                item
                for item in metrics.get("repository_details", [])
                if item.get("slug") == row["slug"]
            ),
            None,
        )
        if candidate_row is None:
            candidate_text = "see candidate table"
        else:
            candidate_text = format_rate(candidate_row["candidates"]["candidate_rate"])
        lines.append(
            f"| `{markdown_escape(row['repo'])}` | {markdown_escape(row['language'])} | "
            f"{markdown_escape(row['shape'])} | {format_integer(row['first_parent_commits'])} | "
            f"{format_integer(row['first_parent_merges'])} | {row['two_parent_merges']:,} | "
            f"{format_integer(row['excluded_octopus_merges'])} | {row['evaluable_merges']:,} | "
            f"{row['evaluation_failures']:,} | {row['no_merge_base_merges']:,} | "
            f"{format_rate(row['conflict_rate'])} | {candidate_text} |"
        )
    lines.append("")

    lines.extend(
        [
            "A `0 / 0 (undefined)` conflict cell is a workflow finding, not a failed repository.",
            "",
            "## Conflict concentration",
            "",
            (
                "The primary unit is one unique `(repo, merge, path)` conflict-file occurrence. "
                "Paths are repo-qualified and are not stitched across renames. For each stratum, "
                "`k=max(1,ceil(0.01 * distinct paths))`; ties are broken by UTF-8 slug/path bytes."
            ),
            (
                "`Merge coverage` uses merges containing at least one conflict-file occurrence "
                "in that stratum as its denominator, so artifact and handwritten rows have "
                "different merge populations."
            ),
            "",
            "| Stratum | Conflict-file occurrences | Distinct files | Exact top-1% files | Occurrence share | Merge coverage within stratum | Tie-inclusive files / share |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for stratum in ("all", "handwritten", "artifacts", "generated", "lockfile", "vendored", "unknown"):
        row = metrics["concentration"][stratum]
        lines.append(
            f"| `{stratum}` | {row['conflict_file_occurrences']:,} | "
            f"{row['distinct_repo_paths']:,} | {row['top_one_percent_file_count']:,} | "
            f"{format_rate(row['top_one_percent_occurrence_share'])} | "
            f"{format_rate(row['top_one_percent_merge_coverage'])} | "
            f"{row['tie_inclusive_file_count']:,} / {format_rate(row['tie_inclusive_occurrence_share'])} |"
        )
    lines.extend(
        [
            "",
            "`artifacts` is the union of generated, lockfile, and vendored occurrences; it is not silently pooled with handwritten files.",
            "",
            "## Divergence versus conflict probability",
            "",
            (
                "Bins use fixed numeric cut points in the analysis code and are not fitted or "
                "quantile-adapted to outcomes. Wilson intervals display denominator "
                "precision but assume independent merges. Repository-cluster bootstrap intervals "
                "are the dependence-aware sensitivity and are withheld when fewer than three "
                "repositories contribute. Neither interval turns this selected corpus into a "
                "population sample."
            ),
            "",
            (
                "Commit exposure is `git rev-list --count B..P1` plus `B..P2` (all reachable "
                "commits, not only first parents). Time exposure is the larger nonnegative "
                "committer-timestamp span from `B` to either parent; negative-clock rows are "
                "retained but unavailable for this metric. Line exposure is added plus deleted "
                "lines in the two zero-context, no-indent-heuristic Myers base-to-parent diffs "
                "with zero inter-hunk context, fixed a/b prefixes, no relative paths, and "
                "nonignored submodules; a binary diff makes "
                "the combined line metric unavailable."
            ),
            "",
        ]
    )
    divergence_labels = {
        "commits": "Combined commits since the base",
        "time": "Maximum wall-clock span from the base",
        "lines": "Combined countable text lines changed",
    }
    for metric in ("commits", "time", "lines"):
        analysis = metrics["divergence"][metric]
        availability = analysis["availability"]
        auc = analysis["auc"]
        auc_point = (
            "not estimable"
            if auc["macro_equal_repository_auc"] is None
            else f"{auc['macro_equal_repository_auc']:.3f}"
        )
        lines.extend(
            [
                f"### {divergence_labels[metric]}",
                "",
                (
                    f"Available for {format_rate(availability['available_rate'])} evaluable merges; "
                    f"unavailable rows had conflict rate "
                    f"{format_rate(availability['unavailable_conflict_rate'])}. "
                    f"Equal-repository within-repository AUC: {auc_point} "
                    f"with 95% repository bootstrap {format_number_interval(auc['repository_bootstrap_95'])}; "
                    f"{auc['informative_repositories']:,} informative repositories "
                    f"({auc['repositories_above_null']:,} above / {auc['repositories_equal_null']:,} equal / "
                    f"{auc['repositories_below_null']:,} below 0.5). **{auc_verdict(auc)}**"
                ),
                "",
                "| Exposure bin | Conflicted / evaluable | Wilson 95% | Repository-cluster 95% | Contributing repos |",
                "|---|---:|---:|---:|---:|",
            ]
        )
        for row in analysis["bins"]:
            lines.append(
                f"| {row['label']} | {format_rate(row['conflict_rate'])} | "
                f"{format_interval(row['wilson_95'])} | "
                f"{format_interval(row['repository_cluster_bootstrap_95'])} | "
                f"{row['contributing_repositories']:,} |"
            )
        lines.append("")

    granularity_rows = metrics["granularity"]
    lines.extend(
        [
            "## Conflict granularity",
            "",
            (
                "Each text span runs from the opening through the closing conflict-marker line: "
                "lines are 1-based inclusive and bytes are 0-based half-open in the exact "
                "`merge-tree` result blob. Ranges are unioned before division by that same blob's "
                "byte size. Structural/binary or invalid ranges remain unmeasurable; they are not "
                "imputed as whole-file conflicts. Including marker bytes is a disclosed, "
                "deterministic upper bound on the disputed payload."
            ),
            "",
            "| Stratum | Measurable / conflict files | Median | Q1–Q3 | p90 | Byte-weighted ratio | <=10% | >=50% | Whole-file |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for stratum in ("all", "handwritten", "artifacts", "generated", "lockfile", "vendored", "unknown"):
        row = granularity_rows[stratum]
        distribution = row["file_ratio_distribution"]
        iqr = (
            "not observed"
            if distribution is None
            else f"{format_percent(distribution['q1'])}–{format_percent(distribution['q3'])}"
        )
        lines.append(
            f"| `{stratum}` | {format_rate(row['measurement_coverage'])} | "
            f"{distribution_cell(distribution, 'median')} | {iqr} | "
            f"{distribution_cell(distribution, 'p90')} | {format_rate(row['weighted_byte_ratio'])} | "
            f"{format_rate(row['threshold_counts']['at_most_10_percent'])} | "
            f"{format_rate(row['threshold_counts']['at_least_50_percent'])} | "
            f"{format_rate(row['threshold_counts']['whole_file'])} |"
        )
    all_range_status_counts = granularity_rows["all"]["range_status_counts"]
    all_conflict_file_denominator = int(
        granularity_rows["all"]["all_conflict_file_occurrences"]
    )
    lines.extend(
        [
            "",
            "Range availability reasons across the full conflict-file denominator:",
            "",
            "| Range status | Count / all conflict files |",
            "|---|---:|",
        ]
    )
    for status, count in all_range_status_counts.items():
        lines.append(
            f"| `{markdown_escape(status)}` | "
            f"{format_rate(rate(int(count), all_conflict_file_denominator))} |"
        )
    lines.extend(
        [
            "",
            "**Coverage-qualified coordination verdict.**",
            "",
            granularity_verdict_text(granularity_rows["all"], "All conflict files"),
            "",
            granularity_verdict_text(
                granularity_rows["handwritten"], "Handwritten-residual conflict files"
            ),
            "",
            granularity_verdict_text(
                granularity_rows["artifacts"], "Generated/lockfile/vendored conflict files"
            ),
            "",
        ]
    )

    overlap = metrics["overlap"]
    overlap_counts = overlap["classification_counts"]
    lines.extend(
        [
            "## Changed-byte overlap",
            "",
            (
                f"Strict base-coordinate overlap occurred in {format_rate(overlap['strict_overlap_rate'])} "
                "strict-decidable conflicted merges; strict status was decidable for "
                f"{format_rate(overlap['strict_classification_coverage'])} conflicted merges. "
                "With boundary-only insertion contacts included, the rate is "
                f"{format_rate(overlap['boundary_inclusive_overlap_rate'])} among "
                "boundary-decidable merges; boundary-inclusive status was decidable for "
                f"{format_rate(overlap['boundary_classification_coverage'])} conflicted merges. "
                "A merge with a known boundary contact plus another unclassifiable path is a "
                "definite boundary-inclusive positive but is withheld from the strict denominator."
            ),
            "",
            "| Merge classification | Count / all conflicted merges |",
            "|---|---:|",
        ]
    )
    for key, value in overlap_counts.items():
        lines.append(
            f"| `{key}` | {format_rate(rate(int(value), overlap['conflicted_merge_denominator']))} |"
        )
    lines.extend(
        [
            "",
            "| Conflict-path overlap status | Count / all recorded conflict paths |",
            "|---|---:|",
        ]
    )
    path_status_denominator = int(overlap.get("path_status_denominator", 0))
    for key, value in overlap.get("path_status_counts", {}).items():
        lines.append(
            f"| `{markdown_escape(key)}` | "
            f"{format_rate(rate(int(value), path_status_denominator))} |"
        )
    lines.extend(
        [
            "",
            (
                "These are base-coordinate changed-range contacts. The miner first isolates "
                "zero-context, no-indent-heuristic Myers hunks and then refines each hunk with "
                "deterministic raw-byte SequenceMatcher opcodes (`autojunk=False`) when both the "
                "declared byte-product and old+new slice-byte bounds permit it. Contact checks use "
                "linear sweeps/set intersections. Paths beyond either bound are retained as "
                "`unclassifiable_refinement_limit`; this is a disclosed edit script and resource "
                "bound, not a unique semantic alignment."
            ),
            "",
            "## Candidate task sites",
            "",
            (
                f"Both sides touched test files in **{format_rate(metrics['candidates']['candidate_rate'])} "
                "classified conflicted merges**. Candidate classification covered "
                f"{format_rate(metrics['candidates']['classification_coverage'])} conflicted merges. "
                "This is a candidate-site count only; no task was constructed "
                "and no candidate repository test was run."
            ),
            "",
            "| Repository | Both-tests candidates / conflicted merges |",
            "|---|---:|",
        ]
    )
    repository_scopes = metrics.get("repository_details", [])
    for row in repository_scopes:
        lines.append(
            f"| `{markdown_escape(row['repo'])}` | {format_rate(row['candidates']['candidate_rate'])} |"
        )
    if not repository_scopes:
        lines.append("| Per-repository candidate detail | not emitted |")
    lines.extend(
        [
            "",
            "## Language and project-shape breakdown",
            "",
            (
                "Each repository has exactly one frozen primary language and one primary shape, "
                "so group denominators reconcile without duplicating repositories. AUC cells are "
                "commits / time / lines; `n/e` means not estimable."
            ),
            "",
        ]
    )
    lines.extend(render_breakdown_table("Primary language", metrics["breakdowns"]["language"]))
    lines.extend(render_breakdown_table("Primary project shape", metrics["breakdowns"]["shape"]))

    lines.extend(
        [
            "## Classification rules",
            "",
            (
                "The analysis treats miner class `lockfile` first, then `vendored`, then "
                "`generated`, with `handwritten` as the non-artifact class. Artifact concentration "
                "is the union of the first three. Exact miner classification rule: "
                + methodology_text(methodology["classification_rules"], "classification rule")
            ),
            "",
            (
                "`handwritten` is an operational residual: no lockfile, vendored, generated-path, "
                "generated-suffix, or generated-header rule matched. It can include prose, config, "
                "and media assets and is not proof of human authorship."
            ),
            "",
            "Exact test-path rule: "
            + methodology_text(methodology["test_path_rules"], "test-path rule"),
            "",
            "## Claims that could NOT be verified",
            "",
            "- That developers actually saw these conflicts. The dataset recomputes a raw Git collision at historical parents; it is not merge-command or user-interface telemetry.",
            "- The historical merge strategy, custom drivers, attributes, renormalization setting, or conflict style used by maintainers.",
            "- Semantic disagreement, semantic conflicts that merged textually, or semantic harmlessness of textual conflicts.",
            "- A causal isolation-policy threshold. Divergence, repository workflow, era, project size, and merge practice are observationally confounded.",
            "- Active development time from commit timestamps; rebases, delayed commits, and clock anomalies break that interpretation.",
            "- That the disclosed SequenceMatcher byte alignment is the unique or semantically correct edit alignment; equivalent edit scripts can place boundaries differently.",
            "- Human-authored/generated or test-file ground truth beyond the disclosed deterministic classifier.",
            "- That both-tests candidate sites have runnable deterministic tests or can become valid agent tasks.",
            "- Generalization beyond these selected repositories and frozen first-parent histories; octopus and unreachable historical merges are outside scope.",
            "- A conflict label for unrelated-history merges. The required invocation refuses them, so they remain explicit no-merge-base failures rather than being silently replayed with `--allow-unrelated-histories`.",
            "",
            "## What would change this verdict",
            "",
            "- Historical merge telemetry or archived pull-request conflict state would distinguish recomputed raw conflicts from conflicts developers actually encountered.",
            "- Replaying the same parents under documented historical attributes, drivers, strategies, and Git versions would test configuration sensitivity.",
            "- A blinded manual audit of generated/test classification and region mapping would quantify classifier and parser error.",
            "- A principled coordinate/extent definition for structural, binary, modify/delete, rename, and markerless conflicts, or manual ground truth for those cases, is required before the marker-backed byte-ratio result can be generalized beyond its measured subset.",
            "- Independently selected repositories within each language and shape, with consistent within-repository effects and cluster intervals excluding the null, would strengthen external claims.",
            "- Agreement across independently implemented pinned byte-diff algorithms would make the strict-overlap result less sensitive to edit-script choice.",
            "- A scalable independently pinned byte-alignment implementation could classify paths currently withheld by the refinement product or total-slice-byte limits.",
            "- Phase B outcomes on preregistered candidate sites would be required for any causal blocking/advisory/isolation verdict.",
            "- A separately preregistered empty-tree/`--allow-unrelated-histories` policy could evaluate no-base merges, but it would define a different dataset from the required invocation.",
            "",
            "## Confidence by claim",
            "",
            "| Claim | Confidence | Reason |",
            "|---|---|---|",
            "| Counts and base rates describe the frozen mined artifacts | High | All-merges and conflict-only populations are identity-reconciled; every rate carries its exact evaluable denominator and failures remain separate. |",
            (
                "| Full-miner output is reproducible | "
                + ("High, environment-conditional" if det_status["passed"] else "No supported claim")
                + " | "
                + det_status["reason"]
                + " The result does not establish cross-version or cross-platform identity. |"
            ),
            "| Text conflict-region arithmetic is correct within its declared coordinate blobs | Medium-high for measured rows; no claim for withheld rows | Ranges are validated, unioned, and divided only by the matching result-blob size. The explicit availability table shows how much structural, binary, and markerless data is withheld; marker representation may not equal minimal human resolution scope. |",
            "| Conflict concentration is measured in this corpus | High arithmetically; medium substantively | Exact file-occurrence denominators and deterministic tie rules are used, but heuristic origin classification and path changes affect interpretation. |",
            "| Greater divergence predicts conflict in these histories | At most medium; metric-specific | Fixed-bin rates, repository-cluster sensitivities, and within-repository AUCs are direct, but histories are serially dependent and observationally confounded. See each metric's interval and direction count. |",
            "| Changed byte ranges overlap rather than merely share a file | High for declared interval arithmetic; lower for edit-script semantics | Definite positives are retained, unknown and boundary-plus-unknown states use separate denominators, and boundary contacts are a sensitivity; an alternative valid edit alignment can still move boundaries. |",
            "| Both-tests rows are candidate sites | High for the path rule; low for task viability | Both side diffs are checked, but no checkout, task construction, oracle run, or determinism gate occurred in Phase A. |",
            "| Findings generalize by language or project shape | Low-to-moderate descriptively; low population-wide | Groups are deliberately selected and some contain too few informative repositories for clustered intervals. |",
            "| Real developers experienced every recomputed conflict | No supported claim | Historical parent states establish the counterfactual raw merge result, not the developers' actual command, configuration, or experience. |",
            "",
            "## What I got wrong",
            "",
            "| Prior assumption | Verdict | Correction |",
            "|---|---|---|",
            "| Conflict-only JSONL would be sufficient for the report | Wrong denominator | Conflict probability and divergence curves require the `_all_merges` population; the analyzer refuses to infer clean merges from absence. |",
            "| A single pooled interval would describe uncertainty | Too strong | Merge rows cluster by repository and history. The report separates Wilson displays, whole-repository bootstrap sensitivities, and the limits of a deliberately selected corpus. |",
            "| Every textual or structural conflict has a meaningful byte ratio | False | Binary and structural conflicts, missing coordinate blobs, invalid ranges, and zero-size denominators are retained as unmeasurable rather than imputed. |",
            "| `vendor/` covered every vendored-tree spelling | False | Hugo uses `_vendor/`; classifier v2 added that exact segment, final v4 retains it, and the canonical row-one rerun applied v4 uniformly. |",
            "| `gen/` covered every generated-tree spelling | False | Hugo uses `resources/_gen/` for generated image resources; classifier v3 added that exact segment, final v4 retains it, and the canonical row-one rerun applied v4 uniformly. |",
            "| Exact `SequenceMatcher` refinement was safe for every text hunk | False | A 1.15-million-byte generated Prometheus blob exposed quadratic work. Final overlap revision v4 caps both per-hunk byte products and total slice bytes, uses linear contact checks, withholds larger paths, and was applied uniformly by the canonical row-one rerun. |",
            "| A proven boundary contact made the merge a known strict-overlap negative | False | Another unclassifiable path can still hide strict overlap. Final overlap revision v4 records `boundary_with_unclassifiable`, excludes it from the strict denominator, and retains it as a boundary-inclusive positive. |",
            "| Python `bytes.splitlines()` matched Git line coordinates | False | Python recognizes extra ASCII control separators. Range revision v2 uses LF alone as Git's line terminator and removes only one CR immediately before LF. |",
            "| Every legitimate conflict exits 1 with empty stderr | False | An Ansible submodule conflict returned exit 1 plus Git's optional submodule-conflict advice. The canonical argv disables only `advice.submoduleMergeConflict`; any other nonempty stderr still fails the row instead of being normalized away. |",
            "",
            "## Reproducibility details",
            "",
            (
                f"Analysis bootstrap: {metrics['protocol']['bootstrap']['replicates']:,} replicates; "
                f"base seed `{markdown_escape(metrics['protocol']['bootstrap']['base_seed'])}`; "
                "seeded SHA-256 labels; Python `random.Random`; linear percentile interpolation."
            ),
            "",
            "Canonical pipeline commands from the repository root:",
            "",
            "```text",
            "python instruments/conflicts/prepare_repositories.py --report exploratory/conflicts/PREPARATION.json",
            "python instruments/conflicts/hydrate_repositories.py --report exploratory/conflicts/HYDRATION.json",
            "python instruments/conflicts/miner.py --no-resume --merge-workers 7",
            "python instruments/conflicts/reclassify_outputs.py",
            "python instruments/conflicts/recompute_overlap_outputs.py",
            "python instruments/conflicts/verify_determinism.py",
            "python instruments/conflicts/analyze.py",
            "```",
            "",
            "Machine-readable metrics: `exploratory/conflicts/metrics.json`.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=True, sort_keys=True, indent=2)
        handle.write("\n")


def write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(value)


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    specifications = normalize_manifest(read_json(args.repositories))
    repositories = [
        load_repository_data(specification, args.corpus)
        for specification in specifications
    ]
    determinism_path, determinism = find_determinism_report(
        args.corpus, args.determinism
    )
    metrics = build_metrics(
        repositories,
        corpus=args.corpus,
        repositories_path=args.repositories,
        determinism_path=determinism_path,
        determinism=determinism,
        base_seed=args.seed,
        replicates=args.replicates,
    )
    preparation_path = args.preparation or (args.output / "PREPARATION.json")
    if preparation_path.exists():
        preparation = read_json(preparation_path)
        if not isinstance(preparation, Mapping):
            raise ValueError(f"{preparation_path}: preparation report is not an object")
        metrics["preparation"] = summarize_preparation(preparation)
        metrics["inputs"]["preparation_report"] = portable_path(preparation_path)
    else:
        metrics["preparation"] = None
        metrics["inputs"]["preparation_report"] = None
    hydration_path = args.hydration or (args.output / "HYDRATION.json")
    if hydration_path.exists():
        hydration = read_json(hydration_path)
        if not isinstance(hydration, Mapping):
            raise ValueError(f"{hydration_path}: hydration report is not an object")
        metrics["hydration"] = summarize_hydration(hydration)
        metrics["inputs"]["hydration_report"] = portable_path(hydration_path)
    else:
        metrics["hydration"] = None
        metrics["inputs"]["hydration_report"] = None
    reclassification_path = args.reclassification or (
        args.output / "RECLASSIFICATION.json"
    )
    if reclassification_path.exists():
        reclassification = read_json(reclassification_path)
        if not isinstance(reclassification, Mapping):
            raise ValueError(
                f"{reclassification_path}: reclassification report is not an object"
            )
        metrics["reclassification"] = dict(reclassification)
        metrics["inputs"]["reclassification_report"] = portable_path(
            reclassification_path
        )
    else:
        metrics["reclassification"] = None
        metrics["inputs"]["reclassification_report"] = None
    write_json(args.output / "metrics.json", metrics)
    write_text(args.output / "MINING.md", render_markdown(metrics))


if __name__ == "__main__":
    main()
