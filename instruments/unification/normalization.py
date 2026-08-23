"""Compare normalized co-read and co-change scores on the fixed unification corpus.

This is an adapter around :mod:`instruments.unification.analyze`.  The existing
extractor, identity mapping, task windowing, raw co-read matrix, raw co-change
matrix, and replay co-change scorer remain authoritative and are not modified.

Run from the project root with::

    python -m instruments.unification.normalization
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import numpy as np
from scipy import sparse, stats

from instruments.unification import analyze as base


HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parents[1]
DEFAULT_REPOSITORY = Path(r"C:/Users/joshp/Desktop/toolsenabled-current")
DEFAULT_READ_EVENTS = PROJECT_ROOT / "exploratory/unification/read-events.jsonl.gz"
DEFAULT_OUTPUT = PROJECT_ROOT / "exploratory/unification/normalization-metrics.json"
DEFAULT_WORK_DIR = PROJECT_ROOT / "exploratory/unification/normalization-artifacts"

WINDOW_SECONDS = 300
TOP_K = 10
CHANGE_HALF_LIFE_COMMITS = 150.0
READ_HALF_LIFE_TASK_WINDOWS = 150.0
DEFAULT_NULL_REPLICATES = 200
DEFAULT_MIXING_CHECK_REPLICATES = 50
DEFAULT_NULL_SEED = "blast-radius-unification-normalization-v1"
DEFAULT_BURN_IN_PROPOSALS_PER_EDGE = 20
DEFAULT_PROPOSALS_PER_EDGE_BETWEEN_DRAWS = 5


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=DEFAULT_REPOSITORY)
    parser.add_argument("--read-events", type=Path, default=DEFAULT_READ_EVENTS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--work-dir", type=Path, default=DEFAULT_WORK_DIR)
    parser.add_argument("--null-replicates", type=int, default=DEFAULT_NULL_REPLICATES)
    parser.add_argument(
        "--mixing-check-replicates",
        type=int,
        default=DEFAULT_MIXING_CHECK_REPLICATES,
    )
    parser.add_argument("--null-seed", default=DEFAULT_NULL_SEED)
    return parser.parse_args()


def seed_integer(seed: str, lane: str) -> int:
    digest = hashlib.sha256(f"{seed}\0{lane}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=False)


def exponential_weights(ages: np.ndarray, half_life: float) -> np.ndarray:
    if half_life <= 0:
        raise ValueError("half-life must be positive")
    ages = np.asarray(ages, dtype=float)
    if np.any(ages < 0):
        raise ValueError("ages must be nonnegative")
    return np.exp(-math.log(2.0) * ages / float(half_life))


@dataclass(frozen=True)
class IncidenceData:
    matrix: np.ndarray
    weights: np.ndarray
    global_indices: np.ndarray
    global_unit_count: int
    included_unit_count: int


def _read_task_sort_key(task: dict[str, Any]) -> tuple[Any, ...]:
    file_key = tuple(
        sorted(
            ((str(label[0]), str(label[1])) for label in task["files"]),
            key=lambda value: (value[0].encode("utf-8"), value[1].encode("utf-8")),
        )
    )
    return (
        float(task["end"]),
        float(task["start"]),
        str(task["agent"]),
        file_key,
    )


def read_incidence(
    tasks: Sequence[dict[str, Any]],
    index_by_id: dict[int, int],
    *,
    half_life: float = READ_HALF_LIFE_TASK_WINDOWS,
) -> IncidenceData:
    """Build a weighted incidence adapter from the existing task windows.

    The all-ones matrix is asserted against ``base.read_counts`` by the caller.
    Age advances across every target-read task window, including windows that do
    not contain a file in the shared comparison universe.
    """

    ordered = sorted(tasks, key=_read_task_sort_key)
    rows: list[list[int]] = []
    global_indices: list[int] = []
    for global_index, task in enumerate(ordered):
        selected = sorted(
            {
                index_by_id[int(label[1])]
                for label in task["files"]
                if label[0] == "git" and int(label[1]) in index_by_id
            }
        )
        if selected:
            rows.append(selected)
            global_indices.append(global_index)

    matrix = np.zeros((len(rows), len(index_by_id)), dtype=bool)
    for row, members in enumerate(rows):
        matrix[row, members] = True
    indices = np.asarray(global_indices, dtype=np.int64)
    # Newest completed window has age one, matching replay's query-at-next-index
    # convention.  Age zero would multiply every weight by one common constant
    # and therefore cannot alter confidence scores.
    ages = len(ordered) - indices
    weights = exponential_weights(ages, half_life)
    return IncidenceData(matrix, weights, indices, len(ordered), len(rows))


def change_incidence(
    commit_members: Sequence[Sequence[int]],
    index_by_id: dict[int, int],
    *,
    half_life: float = CHANGE_HALF_LIFE_COMMITS,
) -> IncidenceData:
    rows: list[list[int]] = []
    global_indices: list[int] = []
    for commit_index, members in enumerate(commit_members):
        selected = sorted({index_by_id[file_id] for file_id in members if file_id in index_by_id})
        if selected:
            rows.append(selected)
            global_indices.append(commit_index)
    matrix = np.zeros((len(rows), len(index_by_id)), dtype=bool)
    for row, members in enumerate(rows):
        matrix[row, members] = True
    indices = np.asarray(global_indices, dtype=np.int64)
    ages = len(commit_members) - indices
    weights = exponential_weights(ages, half_life)
    return IncidenceData(matrix, weights, indices, len(commit_members), len(rows))


def incidence_counts(matrix: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    binary = sparse.csr_matrix(np.asarray(matrix, dtype=np.int32))
    pair = (binary.T @ binary).toarray().astype(np.int64, copy=False)
    marginals = np.diag(pair).copy()
    np.fill_diagonal(pair, 0)
    return pair, marginals


def weighted_incidence_counts(
    matrix: np.ndarray,
    weights: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    if matrix.shape[0] != len(weights):
        raise ValueError("one weight is required per incidence row")
    binary = sparse.csr_matrix(np.asarray(matrix, dtype=float))
    weighted = binary.multiply(np.sqrt(np.asarray(weights, dtype=float))[:, None])
    pair = (weighted.T @ weighted).toarray()
    marginals = np.asarray(binary.T @ np.asarray(weights, dtype=float)).reshape(-1)
    np.fill_diagonal(pair, 0.0)
    return pair, marginals


def confidence_scores(pair: np.ndarray, marginals: np.ndarray) -> np.ndarray:
    pair = np.asarray(pair, dtype=float)
    denominator = np.asarray(marginals, dtype=float)[:, None]
    result = np.zeros_like(pair, dtype=float)
    np.divide(pair, denominator, out=result, where=denominator > 0)
    np.fill_diagonal(result, 0.0)
    return result


def pmi_scores(
    pair: np.ndarray,
    marginals: np.ndarray,
    unit_count: int,
    *,
    normalized: bool,
) -> np.ndarray:
    if unit_count <= 0:
        raise ValueError("PMI requires a positive unit count")
    pair = np.asarray(pair, dtype=float)
    marginals = np.asarray(marginals, dtype=float)
    support = pair > 0
    outer = marginals[:, None] * marginals[None, :]
    if np.any(support & (outer <= 0)):
        raise AssertionError("supported pair has a zero marginal")
    result = np.zeros_like(pair, dtype=float)
    result[support] = np.log(pair[support] * float(unit_count) / outer[support])
    if normalized:
        denominator = np.zeros_like(pair, dtype=float)
        denominator[support] = -np.log(pair[support] / float(unit_count))
        ordinary = result.copy()
        stable = support & (denominator > 0)
        result.fill(0.0)
        result[stable] = ordinary[stable] / denominator[stable]
        # P(a,b)=1 makes the literal NPMI expression 0/0 in the only possible
        # observed case (both marginals are also one).  This degenerate event
        # carries no ranking information, so its declared score is zero.
        result[support & ~stable] = 0.0
    np.fill_diagonal(result, 0.0)
    return result


@dataclass(frozen=True)
class ScoreBundle:
    support: np.ndarray
    raw: np.ndarray
    confidence: np.ndarray
    decayed_confidence: np.ndarray
    pmi: np.ndarray
    npmi: np.ndarray


def score_bundle(
    incidence: IncidenceData,
    *,
    raw_pair: np.ndarray | None = None,
    raw_marginals: np.ndarray | None = None,
    decayed_confidence_override: np.ndarray | None = None,
) -> ScoreBundle:
    if raw_pair is None or raw_marginals is None:
        raw_pair, raw_marginals = incidence_counts(incidence.matrix)
    weighted_pair, weighted_marginals = weighted_incidence_counts(
        incidence.matrix,
        incidence.weights,
    )
    decayed = confidence_scores(weighted_pair, weighted_marginals)
    if decayed_confidence_override is not None:
        if not np.allclose(decayed, decayed_confidence_override, rtol=2e-12, atol=2e-12):
            maximum = float(np.max(np.abs(decayed - decayed_confidence_override)))
            raise AssertionError(
                f"incidence decay adapter differs from deployed replay scorer; max abs diff={maximum}"
            )
        decayed = np.asarray(decayed_confidence_override, dtype=float)
    raw = np.asarray(raw_pair, dtype=float)
    support = raw > 0
    return ScoreBundle(
        support=support,
        raw=raw,
        confidence=confidence_scores(raw_pair, raw_marginals),
        decayed_confidence=decayed,
        pmi=pmi_scores(raw_pair, raw_marginals, incidence.included_unit_count, normalized=False),
        npmi=pmi_scores(raw_pair, raw_marginals, incidence.included_unit_count, normalized=True),
    )


def deployed_cochange_scores(
    git_data: base.GitData,
    shared_ids: Sequence[int],
    *,
    decayed: bool,
) -> np.ndarray:
    _, replay_model = base.replay_modules()
    index_by_id = {file_id: index for index, file_id in enumerate(shared_ids)}
    result = np.zeros((len(shared_ids), len(shared_ids)), dtype=float)
    for seed in shared_ids:
        seed_history, candidate_histories = replay_model.collect_cochange_histories(
            git_data.state,
            seed,
            git_data.commit_count,
        )
        scores = replay_model.score_cochange_histories(
            git_data.state,
            seed_history,
            candidate_histories,
            git_data.commit_count,
            decayed=decayed,
        )
        left = index_by_id[seed]
        for candidate, score in scores.items():
            right = index_by_id.get(candidate)
            if right is not None:
                result[left, right] = float(score)
    return result


def top_k_mask(
    scores: np.ndarray,
    support: np.ndarray,
    *,
    k: int = TOP_K,
) -> tuple[np.ndarray, dict[str, int]]:
    """Select supported neighbors; column order is the path-byte tie rule."""

    scores = np.asarray(scores, dtype=float)
    support = np.asarray(support, dtype=bool)
    if scores.shape != support.shape or scores.ndim != 2 or scores.shape[0] != scores.shape[1]:
        raise ValueError("scores and support must be aligned square matrices")
    selected = np.zeros_like(support, dtype=bool)
    short = 0
    empty = 0
    boundary_ties = 0
    for seed in range(scores.shape[0]):
        candidates = np.flatnonzero(support[seed])
        candidates = candidates[candidates != seed]
        if candidates.size == 0:
            empty += 1
            short += 1
            continue
        if candidates.size <= k:
            selected[seed, candidates] = True
            short += int(candidates.size < k)
            continue
        values = scores[seed, candidates]
        boundary = float(np.partition(values, -k)[-k])
        greater = candidates[values > boundary]
        tied = candidates[values == boundary]
        needed = k - len(greater)
        # shared_ids/labels are path-byte sorted, hence tied candidate indexes
        # are already in the required deterministic order.
        chosen = np.concatenate((greater, tied[:needed]))
        selected[seed, chosen] = True
        boundary_ties += int(len(tied) > needed)
    return selected, {
        "seed_count": int(scores.shape[0]),
        "empty_supported_neighbor_seeds": empty,
        "shorter_than_k_seeds": short,
        "k_boundary_tie_seeds": boundary_ties,
    }


def top_overlap(
    left: np.ndarray,
    right: np.ndarray,
    eligible_seeds: np.ndarray,
) -> dict[str, float | int | None]:
    left = np.asarray(left, dtype=bool)
    right = np.asarray(right, dtype=bool)
    eligible = np.asarray(eligible_seeds, dtype=bool)
    intersections = np.logical_and(left, right).sum(axis=1)
    unions = np.logical_or(left, right).sum(axis=1)
    evaluated = eligible & (unions > 0)
    values = intersections[evaluated] / unions[evaluated]
    return {
        "evaluated_seed_count": int(evaluated.sum()),
        "mean_jaccard": float(np.mean(values)) if values.size else None,
        "zero_overlap_seed_count": int(np.sum(values == 0)),
    }


def _missing_value(variant: str, left: np.ndarray, right: np.ndarray, union: np.ndarray) -> float:
    if variant == "pmi":
        supported_values = np.concatenate((left[union], right[union]))
        finite = supported_values[np.isfinite(supported_values)]
        return float(np.min(finite) - 1.0) if finite.size else -1.0
    if variant == "npmi":
        return -1.0
    return 0.0


def union_spearman(
    left: np.ndarray,
    right: np.ndarray,
    left_support: np.ndarray,
    right_support: np.ndarray,
    *,
    variant: str,
    reverse: bool = False,
) -> dict[str, float | int | None]:
    upper = np.triu_indices_from(left, k=1)
    union = left_support[upper] | right_support[upper]
    if reverse:
        left_values = left.T[upper].astype(float, copy=True)
        right_values = right.T[upper].astype(float, copy=True)
        left_present = left_support.T[upper]
        right_present = right_support.T[upper]
    else:
        left_values = left[upper].astype(float, copy=True)
        right_values = right[upper].astype(float, copy=True)
        left_present = left_support[upper]
        right_present = right_support[upper]
    left_values = left_values[union]
    right_values = right_values[union]
    left_present = left_present[union]
    right_present = right_present[union]
    if left_values.size < 2:
        return {"pair_coordinates": int(left_values.size), "spearman": None}
    missing = _missing_value(variant, left_values, right_values, left_present | right_present)
    left_values[~left_present] = missing
    right_values[~right_present] = missing
    if np.unique(left_values).size < 2 or np.unique(right_values).size < 2:
        rho = None
    else:
        value = float(stats.spearmanr(left_values, right_values).statistic)
        rho = value if math.isfinite(value) else None
    return {"pair_coordinates": int(left_values.size), "spearman": rho}


def evaluate_matched(
    read: ScoreBundle,
    change: ScoreBundle,
    eligible_seeds: np.ndarray,
    *,
    diagnostics: bool,
) -> dict[str, Any]:
    def top_block(
        left_score: np.ndarray,
        right_score: np.ndarray,
        *,
        transpose: bool = False,
    ) -> dict[str, Any]:
        if transpose:
            left_score = left_score.T
            right_score = right_score.T
        left_top, left_diag = top_k_mask(left_score, read.support)
        right_top, right_diag = top_k_mask(right_score, change.support)
        block: dict[str, Any] = top_overlap(left_top, right_top, eligible_seeds)
        if diagnostics:
            block["co_read_diagnostics"] = left_diag
            block["co_change_diagnostics"] = right_diag
        return block

    result: dict[str, Any] = {}
    for name, left_score, right_score in (
        ("raw_pair_count", read.raw, change.raw),
        ("pmi", read.pmi, change.pmi),
        ("normalized_pmi", read.npmi, change.npmi),
    ):
        variant = "npmi" if name == "normalized_pmi" else name.replace("_pair_count", "")
        result[name] = {
            "top10": top_block(left_score, right_score),
            "union_support_spearman": union_spearman(
                left_score,
                right_score,
                read.support,
                change.support,
                variant=variant,
            ),
        }

    for name, left_score, right_score, variant in (
        ("confidence", read.confidence, change.confidence, "confidence"),
        (
            "time_decayed_confidence",
            read.decayed_confidence,
            change.decayed_confidence,
            "decayed_confidence",
        ),
    ):
        result[name] = {
            "top10": {
                "seed_to_candidate": top_block(left_score, right_score),
                "candidate_to_seed": top_block(left_score, right_score, transpose=True),
            },
            "union_support_spearman": {
                "path_ordered_a_to_b": union_spearman(
                    left_score,
                    right_score,
                    read.support,
                    change.support,
                    variant=variant,
                ),
                "path_ordered_b_to_a": union_spearman(
                    left_score,
                    right_score,
                    read.support,
                    change.support,
                    variant=variant,
                    reverse=True,
                ),
            },
        }
    return result


class DegreePreservingSwapper:
    """Binary bipartite two-edge switch chain preserving both degree sequences."""

    def __init__(self, matrix: np.ndarray, rng: np.random.Generator) -> None:
        self.matrix = np.asarray(matrix, dtype=bool).copy()
        self.rng = rng
        self.edge_rows, self.edge_columns = np.nonzero(self.matrix)
        self.edge_rows = self.edge_rows.astype(np.int32, copy=False)
        self.edge_columns = self.edge_columns.astype(np.int32, copy=False)
        self.initial_row_sums = self.matrix.sum(axis=1)
        self.initial_column_sums = self.matrix.sum(axis=0)

    @property
    def edge_count(self) -> int:
        return int(len(self.edge_rows))

    def advance(self, proposal_count: int) -> dict[str, int | float]:
        """Run a fixed number of proposals, retaining rejections as self-loops."""

        if proposal_count <= 0 or self.edge_count < 2:
            return {"accepted": 0, "attempted": 0, "acceptance_rate": 0.0}
        accepted = 0
        attempted = 0
        while attempted < proposal_count:
            batch = min(16_384, proposal_count - attempted)
            picks = self.rng.integers(0, self.edge_count, size=(batch, 2), endpoint=False)
            for first, second in picks:
                attempted += 1
                if first == second:
                    continue
                row_a = int(self.edge_rows[first])
                row_b = int(self.edge_rows[second])
                column_a = int(self.edge_columns[first])
                column_b = int(self.edge_columns[second])
                if row_a == row_b or column_a == column_b:
                    continue
                if self.matrix[row_a, column_b] or self.matrix[row_b, column_a]:
                    continue
                self.matrix[row_a, column_a] = False
                self.matrix[row_b, column_b] = False
                self.matrix[row_a, column_b] = True
                self.matrix[row_b, column_a] = True
                self.edge_columns[first] = column_b
                self.edge_columns[second] = column_a
                accepted += 1
        return {
            "accepted": accepted,
            "attempted": attempted,
            "acceptance_rate": accepted / attempted,
        }

    def assert_invariants(self) -> None:
        np.testing.assert_array_equal(self.matrix.sum(axis=1), self.initial_row_sums)
        np.testing.assert_array_equal(self.matrix.sum(axis=0), self.initial_column_sums)
        if int(self.matrix.sum()) != self.edge_count:
            raise AssertionError("edge count changed during switching")


class TreeAccumulator:
    def __init__(self) -> None:
        self.values: dict[tuple[str, ...], list[float]] = collections.defaultdict(list)

    def add(self, value: Any, path: tuple[str, ...] = ()) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                self.add(child, (*path, str(key)))
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            if math.isfinite(float(value)):
                self.values[path].append(float(value))

    def means(self) -> dict[str, Any]:
        root: dict[str, Any] = {}
        for path, values in sorted(self.values.items()):
            cursor = root
            for key in path[:-1]:
                cursor = cursor.setdefault(key, {})
            cursor[path[-1]] = math.fsum(values) / len(values)
        return root


def simulate_popularity_null(
    read_incidence_data: IncidenceData,
    change_incidence_data: IncidenceData,
    eligible_seeds: np.ndarray,
    *,
    replicates: int,
    seed: str,
    burn_in_proposals_per_edge: int,
    proposals_per_edge_between_draws: int,
) -> dict[str, Any]:
    if replicates <= 0:
        return {"replicates": 0, "mean": {}}
    read_chain = DegreePreservingSwapper(
        read_incidence_data.matrix,
        np.random.default_rng(seed_integer(seed, "read")),
    )
    change_chain = DegreePreservingSwapper(
        change_incidence_data.matrix,
        np.random.default_rng(seed_integer(seed, "change")),
    )
    read_burn = read_chain.advance(burn_in_proposals_per_edge * read_chain.edge_count)
    change_burn = change_chain.advance(burn_in_proposals_per_edge * change_chain.edge_count)
    accumulator = TreeAccumulator()
    read_draw_accepted = 0
    read_draw_attempted = 0
    change_draw_accepted = 0
    change_draw_attempted = 0
    for replicate in range(replicates):
        if replicate:
            read_step = read_chain.advance(
                proposals_per_edge_between_draws * read_chain.edge_count
            )
            change_step = change_chain.advance(
                proposals_per_edge_between_draws * change_chain.edge_count
            )
            read_draw_accepted += int(read_step["accepted"])
            read_draw_attempted += int(read_step["attempted"])
            change_draw_accepted += int(change_step["accepted"])
            change_draw_attempted += int(change_step["attempted"])
        read_draw = IncidenceData(
            read_chain.matrix,
            read_incidence_data.weights,
            read_incidence_data.global_indices,
            read_incidence_data.global_unit_count,
            read_incidence_data.included_unit_count,
        )
        change_draw = IncidenceData(
            change_chain.matrix,
            change_incidence_data.weights,
            change_incidence_data.global_indices,
            change_incidence_data.global_unit_count,
            change_incidence_data.included_unit_count,
        )
        accumulator.add(
            evaluate_matched(
                score_bundle(read_draw),
                score_bundle(change_draw),
                eligible_seeds,
                diagnostics=False,
            )
        )
    read_chain.assert_invariants()
    change_chain.assert_invariants()

    def chain_summary(
        chain: DegreePreservingSwapper,
        burn: dict[str, int | float],
        accepted: int,
        attempted: int,
    ) -> dict[str, Any]:
        return {
            "edge_count": chain.edge_count,
            "burn_in": burn,
            "between_draws_accepted": accepted,
            "between_draws_attempted": attempted,
            "between_draws_acceptance_rate": accepted / attempted if attempted else None,
            "row_and_column_degrees_preserved": True,
        }

    return {
        "replicates": replicates,
        "seed": seed,
        "burn_in_proposals_per_edge": burn_in_proposals_per_edge,
        "proposals_per_edge_between_draws": proposals_per_edge_between_draws,
        "mean": accumulator.means(),
        "chains": {
            "co_read": chain_summary(
                read_chain,
                read_burn,
                read_draw_accepted,
                read_draw_attempted,
            ),
            "co_change": chain_summary(
                change_chain,
                change_burn,
                change_draw_accepted,
                change_draw_attempted,
            ),
        },
    }


def fixed_support_spearman(
    left: np.ndarray,
    right: np.ndarray,
    union_support: np.ndarray,
    *,
    reverse: bool = False,
) -> dict[str, float | int | None]:
    upper = np.triu_indices_from(left, k=1)
    selected = union_support[upper]
    left_values = (left.T if reverse else left)[upper][selected]
    right_values = (right.T if reverse else right)[upper][selected]
    if (
        left_values.size < 2
        or np.unique(left_values).size < 2
        or np.unique(right_values).size < 2
    ):
        rho = None
    else:
        value = float(stats.spearmanr(left_values, right_values).statistic)
        rho = value if math.isfinite(value) else None
    return {"pair_coordinates": int(left_values.size), "spearman": rho}


def independent_tie_expected_jaccard(candidate_count: int, k: int = TOP_K) -> float:
    """Exact E[Jaccard] for independent uniform k-subsets of one candidate set."""

    if candidate_count <= 0:
        raise ValueError("candidate_count must be positive")
    selected = min(k, candidate_count)
    expectation = 0.0
    for intersection in range(selected + 1):
        probability = float(
            stats.hypergeom.pmf(
                intersection,
                candidate_count,
                selected,
                selected,
            )
        )
        union = 2 * selected - intersection
        if union:
            expectation += probability * intersection / union
    return expectation


def all_tie_top_null(eligible_seeds: np.ndarray, file_count: int) -> dict[str, Any]:
    return {
        "evaluated_seed_count": int(np.asarray(eligible_seeds, dtype=bool).sum()),
        "mean_jaccard": independent_tie_expected_jaccard(file_count - 1),
        "tie_resolution": "exact expectation under independent uniform source-specific resolution of the all-score tie",
    }


def analytic_popularity_null(
    read_pair: np.ndarray,
    change_pair: np.ndarray,
    read_marginals: np.ndarray,
    change_marginals: np.ndarray,
    read_data: IncidenceData,
    change_data: IncidenceData,
    eligible_seeds: np.ndarray,
) -> dict[str, Any]:
    file_count = len(read_marginals)
    if len(change_marginals) != file_count:
        raise ValueError("source marginal vectors are not aligned")
    full_support = np.ones((file_count, file_count), dtype=bool)
    np.fill_diagonal(full_support, False)
    union_support = (read_pair > 0) | (change_pair > 0)

    read_units = float(read_data.included_unit_count)
    change_units = float(change_data.included_unit_count)
    read_q = np.outer(read_marginals, read_marginals) / read_units
    change_q = np.outer(change_marginals, change_marginals) / change_units
    np.fill_diagonal(read_q, 0.0)
    np.fill_diagonal(change_q, 0.0)
    read_confidence = np.broadcast_to(
        np.asarray(read_marginals, dtype=float)[None, :] / read_units,
        read_q.shape,
    ).copy()
    change_confidence = np.broadcast_to(
        np.asarray(change_marginals, dtype=float)[None, :] / change_units,
        change_q.shape,
    ).copy()
    np.fill_diagonal(read_confidence, 0.0)
    np.fill_diagonal(change_confidence, 0.0)

    _, read_weighted_marginals = weighted_incidence_counts(
        read_data.matrix,
        read_data.weights,
    )
    _, change_weighted_marginals = weighted_incidence_counts(
        change_data.matrix,
        change_data.weights,
    )
    read_weight_total = float(np.sum(read_data.weights))
    change_weight_total = float(np.sum(change_data.weights))
    read_decayed = np.broadcast_to(
        read_weighted_marginals[None, :] / read_weight_total,
        read_q.shape,
    ).copy()
    change_decayed = np.broadcast_to(
        change_weighted_marginals[None, :] / change_weight_total,
        change_q.shape,
    ).copy()
    np.fill_diagonal(read_decayed, 0.0)
    np.fill_diagonal(change_decayed, 0.0)

    def deterministic_top(left: np.ndarray, right: np.ndarray) -> dict[str, Any]:
        left_top, _ = top_k_mask(left, full_support)
        right_top, _ = top_k_mask(right, full_support)
        return top_overlap(left_top, right_top, eligible_seeds)

    all_tie = all_tie_top_null(eligible_seeds, file_count)
    zero = np.zeros_like(read_q)
    return {
        "definition": "source-specific analytic independence expectation Q_s(a,b)=m_s(a)*m_s(b)/N_s, transformed separately by each estimator",
        "raw_pair_count": {
            "top10": deterministic_top(read_q, change_q),
            "union_support_spearman": fixed_support_spearman(
                read_q,
                change_q,
                union_support,
            ),
        },
        "confidence": {
            "top10": {
                "seed_to_candidate": deterministic_top(read_confidence, change_confidence),
                "candidate_to_seed": all_tie,
            },
            "union_support_spearman": {
                "path_ordered_a_to_b": fixed_support_spearman(
                    read_confidence,
                    change_confidence,
                    union_support,
                ),
                "path_ordered_b_to_a": fixed_support_spearman(
                    read_confidence,
                    change_confidence,
                    union_support,
                    reverse=True,
                ),
            },
        },
        "time_decayed_confidence": {
            "top10": {
                "seed_to_candidate": deterministic_top(read_decayed, change_decayed),
                "candidate_to_seed": all_tie,
            },
            "union_support_spearman": {
                "path_ordered_a_to_b": fixed_support_spearman(
                    read_decayed,
                    change_decayed,
                    union_support,
                ),
                "path_ordered_b_to_a": fixed_support_spearman(
                    read_decayed,
                    change_decayed,
                    union_support,
                    reverse=True,
                ),
            },
        },
        "pmi": {
            "top10": all_tie,
            "union_support_spearman": fixed_support_spearman(zero, zero, union_support),
            "analytic_score": "zero for every pair under independence",
        },
        "normalized_pmi": {
            "top10": all_tie,
            "union_support_spearman": fixed_support_spearman(zero, zero, union_support),
            "analytic_score": "zero for every pair under independence",
        },
    }


def normalized_inputs(
    repository: Path,
    read_events_path: Path,
    work_dir: Path,
) -> tuple[
    base.GitData,
    list[int],
    list[str],
    IncidenceData,
    IncidenceData,
    np.ndarray,
    np.ndarray,
    np.ndarray,
    np.ndarray,
    dict[str, Any],
]:
    git_data = base.extract_git_history(repository, work_dir)
    raw_events, read_header = base.load_read_events(read_events_path)
    events, mapping = base.canonicalize_events(raw_events, git_data)
    mapped_ids = {
        int(event["label"][1])
        for event in events
        if event["label"][0] == "git"
    }
    shared_ids = sorted(
        mapped_ids & git_data.source_ids,
        key=lambda file_id: base.path_bytes(git_data.display_paths[file_id]),
    )
    labels = [git_data.display_paths[file_id] for file_id in shared_ids]
    if labels != sorted(labels, key=base.path_bytes):
        raise AssertionError("shared labels are not in deterministic path-byte order")
    index_by_id = {file_id: index for index, file_id in enumerate(shared_ids)}
    tasks = base.build_task_windows(events, WINDOW_SECONDS)
    read_pair, _, read_marginals, read_coverage = base.read_counts(tasks, index_by_id)
    change_pair, change_marginals, change_coverage = base.cochange_counts(git_data, shared_ids)
    read_data = read_incidence(tasks, index_by_id)
    change_data = change_incidence(git_data.commit_members, index_by_id)

    reconstructed_read_pair, reconstructed_read_marginals = incidence_counts(read_data.matrix)
    reconstructed_change_pair, reconstructed_change_marginals = incidence_counts(change_data.matrix)
    np.testing.assert_array_equal(reconstructed_read_pair, read_pair)
    np.testing.assert_array_equal(reconstructed_read_marginals, read_marginals)
    np.testing.assert_array_equal(reconstructed_change_pair, change_pair)
    np.testing.assert_array_equal(reconstructed_change_marginals, change_marginals)
    if read_data.included_unit_count != int(read_coverage["task_window_count_with_shared_read"]):
        raise AssertionError("read incidence unit count differs from existing builder")
    if change_data.included_unit_count != int(change_coverage["commits_with_shared_file"]):
        raise AssertionError("change incidence unit count differs from existing builder")

    metadata = {
        "read_header": read_header,
        "mapping": mapping,
        "read_coverage": read_coverage,
        "change_coverage": change_coverage,
        "raw_event_count": len(raw_events),
        "all_300_second_task_window_count": len(tasks),
    }
    return (
        git_data,
        shared_ids,
        labels,
        read_data,
        change_data,
        read_pair,
        read_marginals,
        change_pair,
        change_marginals,
        metadata,
    )


def analyze_normalization(
    repository: Path,
    read_events_path: Path,
    output_path: Path,
    work_dir: Path,
    *,
    null_replicates: int,
    mixing_check_replicates: int,
    null_seed: str,
) -> dict[str, Any]:
    (
        git_data,
        shared_ids,
        labels,
        read_data,
        change_data,
        read_pair,
        read_marginals,
        change_pair,
        change_marginals,
        input_metadata,
    ) = normalized_inputs(repository, read_events_path, work_dir)

    deployed_plain = deployed_cochange_scores(git_data, shared_ids, decayed=False)
    derived_plain = confidence_scores(change_pair, change_marginals)
    if not np.array_equal(deployed_plain, derived_plain):
        if not np.allclose(deployed_plain, derived_plain, rtol=0, atol=0):
            raise AssertionError("plain confidence differs from deployed replay scorer")
    deployed_decayed = deployed_cochange_scores(git_data, shared_ids, decayed=True)
    read_scores = score_bundle(
        read_data,
        raw_pair=read_pair,
        raw_marginals=read_marginals,
    )
    change_scores = score_bundle(
        change_data,
        raw_pair=change_pair,
        raw_marginals=change_marginals,
        decayed_confidence_override=deployed_decayed,
    )

    raw_read_top, _ = top_k_mask(read_scores.raw, read_scores.support)
    raw_change_top, _ = top_k_mask(change_scores.raw, change_scores.support)
    eligible_seeds = np.logical_or(raw_read_top.any(axis=1), raw_change_top.any(axis=1))
    observed = evaluate_matched(read_scores, change_scores, eligible_seeds, diagnostics=True)

    popularity_null = analytic_popularity_null(
        read_pair,
        change_pair,
        read_marginals,
        change_marginals,
        read_data,
        change_data,
        eligible_seeds,
    )
    baseline_top = observed["raw_pair_count"]["top10"]["mean_jaccard"]
    baseline_rho = observed["raw_pair_count"]["union_support_spearman"]["spearman"]
    if not math.isclose(float(baseline_top), 0.07815103136532413, rel_tol=0, abs_tol=1e-15):
        raise AssertionError(f"raw top-10 baseline did not reproduce: {baseline_top}")
    if not math.isclose(float(baseline_rho), -0.5051254955251694, rel_tol=0, abs_tol=1e-15):
        raise AssertionError(f"raw Spearman baseline did not reproduce: {baseline_rho}")
    legacy_top = popularity_null["raw_pair_count"]["top10"]["mean_jaccard"]
    if not math.isclose(float(legacy_top), 0.17683485283710088, rel_tol=0, abs_tol=1e-15):
        raise AssertionError(f"legacy raw popularity null did not reproduce: {legacy_top}")

    finite_null = simulate_popularity_null(
        read_data,
        change_data,
        eligible_seeds,
        replicates=null_replicates,
        seed=null_seed,
        burn_in_proposals_per_edge=DEFAULT_BURN_IN_PROPOSALS_PER_EDGE,
        proposals_per_edge_between_draws=DEFAULT_PROPOSALS_PER_EDGE_BETWEEN_DRAWS,
    )
    mixing_check = simulate_popularity_null(
        read_data,
        change_data,
        eligible_seeds,
        replicates=mixing_check_replicates,
        seed=f"{null_seed}:double-swaps",
        burn_in_proposals_per_edge=2 * DEFAULT_BURN_IN_PROPOSALS_PER_EDGE,
        proposals_per_edge_between_draws=2 * DEFAULT_PROPOSALS_PER_EDGE_BETWEEN_DRAWS,
    )

    upper = np.triu_indices_from(read_pair, k=1)
    union_support = (read_pair[upper] > 0) | (change_pair[upper] > 0)
    joint_support = (read_pair[upper] > 0) & (change_pair[upper] > 0)
    result: dict[str, Any] = {
        "schema_version": 1,
        "measurement": "co-read-vs-co-change-normalization-test",
        "repository": str(repository),
        "repository_head": git_data.metadata["head_sha"],
        "window_seconds": WINDOW_SECONDS,
        "shared_file_count": len(shared_ids),
        "coverage": {
            **input_metadata,
            "read_included_unit_count": read_data.included_unit_count,
            "read_global_task_window_count_for_decay_clock": read_data.global_unit_count,
            "change_included_unit_count": change_data.included_unit_count,
            "change_global_commit_count_for_decay_clock": change_data.global_unit_count,
            "raw_union_pair_support": int(union_support.sum()),
            "raw_joint_pair_support": int(joint_support.sum()),
            "eligible_seed_count": int(eligible_seeds.sum()),
        },
        "score_protocol": {
            "raw_pair_count": "one pair incidence per deduplicated whole source unit",
            "confidence": "joint(a,b)/marginal(a); directional; both seed-to-candidate and candidate-to-seed neighborhoods reported",
            "time_decayed_confidence": "weighted joint(a,b)/weighted marginal(a), with exponential source-unit age weights",
            "cochange_half_life_commits": CHANGE_HALF_LIFE_COMMITS,
            "coread_half_life_task_window_indices": READ_HALF_LIFE_TASK_WINDOWS,
            "coread_half_life_choice": "same outcome-independent count of source-event units as the deployed 150-commit estimator; not asserted to be the same wall-clock duration",
            "decay_age": "newest historical unit has age 1; all global units advance age, including units with no shared file",
            "task_decay_order": "all 300-second windows globally ordered by (end, start, agent, deterministic file key)",
            "pmi": "natural log(joint*N/(marginal(a)*marginal(b))); no smoothing",
            "normalized_pmi": "PMI/-log(joint/N); P(joint)=1 degenerate case declared zero",
            "top10_support": "underlying observed pair incidence > 0; supported nonpositive PMI remains eligible; unseen pairs never fill",
            "top10_ties": "score descending, then repository-path bytes ascending; exactly ten where available",
            "spearman_support": "fixed union of underlying nonzero pair support; double-zero pairs excluded",
            "spearman_missing": "zero for raw/confidence; tied bottom below finite PMI; -1 NPMI limit",
            "confidence_directions": "top neighborhoods report score rows and score-transpose rows; Spearman reports path-ordered a->b and b->a separately",
        },
        "observed": observed,
        "popularity_null": {
            "primary_finite_sample": {
                "definition": "independent source-specific bipartite two-edge-switch chains use fixed-count symmetric edge-pair proposals with rejections retained as self-loops; they preserve every file marginal and whole-unit size, remove observed pair identity, retain chronological row weights, and recompute every estimator on each complete randomized matrix",
                **finite_null,
            },
            "double_swap_mixing_check": mixing_check,
            "legacy_analytic_per_variant": popularity_null,
            "legacy_analytic_definition": "the original Q=m(a)m(b)/N expectation transformed anew by each score; retained to reproduce the stated 17.68% raw baseline",
            "analytic_all_tie_rule": "PMI/NPMI and inverse confidence are constant under Q; the exact independent-tie expectation is diagnostic only, because it does not reproduce finite-sample estimator noise",
        },
        "reuse_and_validation": {
            "existing_read_builder_reproduced_by_all_ones_incidence": True,
            "existing_change_builder_reproduced_by_all_ones_incidence": True,
            "deployed_plain_cochange_scorer_matches_matrix_confidence": True,
            "deployed_decayed_cochange_scorer_matches_weighted_incidence_adapter": True,
            "raw_top10_reproduced": baseline_top,
            "raw_union_spearman_reproduced": baseline_rho,
            "legacy_raw_null_top10_reproduced": legacy_top,
        },
        "uncertainty": {
            "confidence_intervals": None,
            "reason": "pair cells are dependent within commits and task windows; no pair-level interval or p-value is valid, and this run does not perform a whole-unit bootstrap",
            "finite_sample_null_draws_are_calibration_not_confidence_intervals": True,
        },
        "implementation": {
            "read_events_sha256": base.sha256_file(read_events_path),
            "scripts_sha256": {
                "normalization.py": base.sha256_file(Path(__file__).resolve()),
                "analyze.py": base.sha256_file(HERE / "analyze.py"),
                "replay.py": base.sha256_file(base.REPLAY_ROOT / "replay.py"),
            },
            "git_extraction": git_data.metadata,
            "output_path": str(output_path),
            "work_dir": str(work_dir),
            "python": sys.version,
            "numpy": np.__version__,
        },
    }
    return result


def main() -> int:
    args = parse_args()
    repository = args.repository.resolve()
    read_events = args.read_events.resolve()
    output = args.output.resolve()
    work_dir = args.work_dir.resolve()
    if args.null_replicates <= 0:
        raise SystemExit("--null-replicates must be positive")
    if args.mixing_check_replicates < 0:
        raise SystemExit("--mixing-check-replicates cannot be negative")
    if not (repository / ".git").exists():
        raise SystemExit(f"target is not a Git worktree: {repository}")
    if not read_events.is_file():
        raise SystemExit(f"read-event stream does not exist: {read_events}")
    result = analyze_normalization(
        repository,
        read_events,
        output,
        work_dir,
        null_replicates=args.null_replicates,
        mixing_check_replicates=args.mixing_check_replicates,
        null_seed=str(args.null_seed),
    )
    base.atomic_write_json(output, result)
    print(
        f"wrote {output}; shared={result['shared_file_count']}; "
        f"null_replicates={args.null_replicates}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
