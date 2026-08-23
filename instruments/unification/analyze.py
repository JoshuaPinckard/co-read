"""Build and compare co-read and co-change matrices for ToolsEnabled.

Git extraction and logical-file pair counting are delegated to the existing
``instruments/replay`` implementation.  This file owns only the adapter, the
same-agent read-window construction, and the requested comparisons.
"""

from __future__ import annotations

import argparse
import collections
import gzip
import hashlib
import importlib
import json
import math
import ntpath
import os
import statistics
import subprocess
import sys
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np
from scipy import stats


HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parents[1]
REPLAY_ROOT = HERE.parent / "replay"
DEFAULT_REPOSITORY = Path(r"C:/Users/USER/Desktop/toolsenabled-current")
DEFAULT_READ_EVENTS = PROJECT_ROOT / "exploratory/unification/read-events.jsonl.gz"
DEFAULT_OUTPUT = PROJECT_ROOT / "exploratory/unification/metrics.json"
WINDOWS = (60, 300, 900)
PRIMARY_WINDOW = 300
TOP_K = 10
SAMPLE_COUNT = 20

# A matrix with fewer than 30 shared files cannot support both ten-neighbour
# rankings and twenty disagreement examples without repeatedly recycling the
# same tiny universe.  Independent support is gated separately below because a
# quadratic number of pair coordinates is not a quadratic number of examples.
MIN_SHARED_FILES = 30
MIN_INFORMATIVE_UNITS = 20
MIN_EVALUABLE_SEEDS = 20


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=DEFAULT_REPOSITORY)
    parser.add_argument("--read-events", type=Path, default=DEFAULT_READ_EVENTS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def path_bytes(path: str) -> bytes:
    return path.encode("utf-8", errors="surrogateescape")


def git_path_key(path: str) -> str:
    return ntpath.normcase(ntpath.normpath(path.replace("/", "\\"))).replace("\\", "/")


def run_git(repository: Path, arguments: Sequence[str]) -> str:
    result = subprocess.run(
        ("git", "-C", str(repository), *arguments),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="surrogateescape",
    )
    return result.stdout.strip()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def replay_modules() -> tuple[Any, Any]:
    # replay/*.py uses bare ``from common import`` imports, so its own directory
    # must be importable.  Nothing in the replay directory is modified.
    replay_text = str(REPLAY_ROOT)
    if replay_text not in sys.path:
        sys.path.insert(0, replay_text)
    return importlib.import_module("extract"), importlib.import_module("replay")


@dataclass
class GitData:
    state: Any
    commit_count: int
    commit_members: list[tuple[int, ...]]
    aliases: dict[str, set[int]]
    display_paths: dict[int, str]
    source_ids: set[int]
    metadata: dict[str, Any]


def extract_git_history(repository: Path, output_dir: Path) -> GitData:
    replay_extract, replay_model = replay_modules()
    stream_dir = output_dir / "git-stream"
    stream_dir.mkdir(parents=True, exist_ok=True)

    head_before = run_git(repository, ("rev-parse", "HEAD"))
    reachable_count = int(run_git(repository, ("rev-list", "--count", "HEAD")))
    first_parent_count = int(run_git(repository, ("rev-list", "--first-parent", "--count", "HEAD")))
    status_before = run_git(repository, ("status", "--porcelain=v1", "--untracked-files=all"))
    spec = {
        "slug": repository.name,
        "name": repository.name,
        "url": str(repository),
        "language": "mixed",
    }
    corpus_record = {
        "resolved_head_sha": head_before,
        "reachable_commit_count": reachable_count,
        "first_parent_commit_count": first_parent_count,
    }

    # Patch module output/input roots in memory only.  This makes the stock
    # extractor operate on the read-only local target while writing its stream
    # exclusively inside this experiment's output directory.
    replay_extract.CLONE_ROOT = repository.parent
    replay_extract.STREAM_ROOT = stream_dir
    extraction_meta = replay_extract.extract_repository(spec, corpus_record)
    stream_path = stream_dir / f"{repository.name}.jsonl.gz"

    aliases: dict[str, set[int]] = collections.defaultdict(set)
    commit_members: list[tuple[int, ...]] = []
    commit_sizes: list[int] = []
    with gzip.open(stream_path, "rt", encoding="utf-8", errors="surrogatepass") as handle:
        first_line = handle.readline()
        if not first_line:
            raise ValueError("empty Git extraction stream")
        header = json.loads(first_line)
        if header.get("type") != "header":
            raise ValueError("Git extraction stream lacks a header")
        state = replay_model.ReplayState(
            header["initial_files"],
            max_commit_age=int(extraction_meta["commit_count"]) + 1,
        )
        for path, file_id in state.path_to_id.items():
            aliases[git_path_key(path)].add(file_id)

        for expected_index, line in enumerate(handle):
            commit = json.loads(line)
            commit_index = int(commit["index"])
            if commit_index != expected_index:
                raise ValueError(f"non-contiguous Git stream index {commit_index}; expected {expected_index}")
            resolved = state.resolve_changes(commit["changes"])
            members = tuple(dict.fromkeys(change.file_id for change in resolved))
            commit_members.append(members)
            commit_sizes.append(len(members))
            for change in resolved:
                if change.status in {"A", "M", "D"}:
                    assert change.path is not None
                    aliases[git_path_key(change.path)].add(change.file_id)
                else:
                    assert change.old_path is not None and change.new_path is not None
                    aliases[git_path_key(change.old_path)].add(change.file_id)
                    aliases[git_path_key(change.new_path)].add(change.file_id)
            state.fold(commit_index, resolved)

    commit_count = len(commit_members)
    if commit_count != int(extraction_meta["commit_count"]):
        raise ValueError("replayed commit count differs from extractor metadata")
    expected_head_paths = replay_model.read_head_paths(repository)
    if set(state.path_to_id) != expected_head_paths:
        raise AssertionError("ReplayState final tree differs from repository HEAD")
    head_after = run_git(repository, ("rev-parse", "HEAD"))
    status_after = run_git(repository, ("status", "--porcelain=v1", "--untracked-files=all"))
    if head_after != head_before:
        raise RuntimeError(f"target HEAD changed during extraction: {head_before} -> {head_after}")
    if status_after != status_before:
        raise RuntimeError("target worktree status changed during read-only extraction")

    display_paths = {file_id: state.id_to_path[file_id] for file_id in state.existing_ids}
    source_ids = {file_id for file_id in state.existing_ids if state.file_history[file_id]}
    sorted_sizes = sorted(enumerate(commit_sizes), key=lambda item: (-item[1], item[0]))
    metadata = {
        "head_sha": head_before,
        "reachable_commit_count": reachable_count,
        "first_parent_commit_count": first_parent_count,
        "commits_replayed": commit_count,
        "head_live_file_count": len(state.existing_ids),
        "cochange_source_file_count": len(source_ids),
        "worktree_dirty_path_count": len(status_before.splitlines()) if status_before else 0,
        "history_capped": bool(extraction_meta.get("capped")),
        "merge_commit_count": extraction_meta.get("merge_commit_count"),
        "rename_count": extraction_meta.get("rename_count"),
        "materialized_commit_count": state.materialized_commit_count,
        "factorized_commit_count": state.factorized_commit_count,
        "max_factorized_commit_size": state.max_factorized_commit_size,
        "largest_commits": [
            {"index": index, "touched_file_count": size}
            for index, size in sorted_sizes[:10]
        ],
        "stream_path": str(stream_path),
        "stream_sha256": sha256_file(stream_path),
        "extractor_meta": extraction_meta,
        "protocol": {
            "history": "first-parent, oldest to newest; merges diffed once against first parent",
            "renames": "50% exhaustive detection and stable ReplayState logical identity",
            "pair_counting": "ReplayState materialized cliques through 64 files and exact factorized larger cliques",
            "file_universe": "HEAD-live logical files touched at least once in the extracted history",
        },
    }
    return GitData(state, commit_count, commit_members, dict(aliases), display_paths, source_ids, metadata)


def load_read_events(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    events: list[dict[str, Any]] = []
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        first_line = handle.readline()
        if not first_line:
            raise ValueError("empty read-event stream")
        header = json.loads(first_line)
        if header.get("type") != "header" or header.get("schema_version") != 1:
            raise ValueError("unsupported read-event stream")
        for line in handle:
            event = json.loads(line)
            if event.get("type") != "read":
                raise ValueError("non-read record after read-event header")
            events.append(event)
    return events, header


def canonicalize_events(
    events: Sequence[dict[str, Any]],
    git_data: GitData,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    live_ids = set(git_data.state.existing_ids)
    canonical: list[dict[str, Any]] = []
    source_labels: set[tuple[str, Any]] = set()
    mapped_ids: set[int] = set()
    diagnostics: collections.Counter[str] = collections.Counter()
    for event in events:
        raw_path = str(event["path"])
        candidates = git_data.aliases.get(git_path_key(raw_path), set())
        label: tuple[str, Any]
        if len(candidates) == 1:
            file_id = next(iter(candidates))
            if file_id in live_ids and file_id in git_data.source_ids:
                label = ("git", file_id)
                mapped_ids.add(file_id)
                diagnostics["events_mapped_to_live_git_identity"] += 1
            else:
                label = ("raw", git_path_key(raw_path))
                diagnostics["events_matching_only_nonlive_or_untouched_identity"] += 1
        elif len(candidates) > 1:
            label = ("raw", git_path_key(raw_path))
            diagnostics["events_with_ambiguous_delete_readd_alias"] += 1
        else:
            label = ("raw", git_path_key(raw_path))
            diagnostics["events_without_git_history_alias"] += 1
        source_labels.add(label)
        canonical.append({**event, "label": label})
    diagnostics["co_read_source_region_count"] = len(source_labels)
    diagnostics["mapped_shared_region_count"] = len(mapped_ids)
    return canonical, dict(sorted(diagnostics.items()))


def build_task_windows(events: Sequence[dict[str, Any]], window_seconds: int) -> list[dict[str, Any]]:
    by_agent: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for event in events:
        by_agent[str(event["agent"])].append(event)

    tasks: list[dict[str, Any]] = []
    for agent in sorted(by_agent):
        ordered = sorted(
            by_agent[agent],
            key=lambda event: (float(event["timestamp"]), str(event["session"]), str(event["tool_use_id"])),
        )
        current: dict[str, Any] | None = None
        last_timestamp: float | None = None
        for event in ordered:
            timestamp = float(event["timestamp"])
            if current is None or (last_timestamp is not None and timestamp - last_timestamp > window_seconds):
                current = {
                    "agent": agent,
                    "start": timestamp,
                    "end": timestamp,
                    "event_count": 0,
                    "files": {},
                }
                tasks.append(current)
            current["end"] = timestamp
            current["event_count"] += 1
            current["files"].setdefault(event["label"], timestamp)
            last_timestamp = timestamp
    return tasks


def read_counts(
    tasks: Sequence[dict[str, Any]],
    index_by_id: dict[int, int],
) -> tuple[np.ndarray, np.ndarray, np.ndarray, dict[str, Any]]:
    size = len(index_by_id)
    matrix = np.zeros((size, size), dtype=np.int64)
    directed = np.zeros((size, size), dtype=np.int64)
    marginals = np.zeros(size, dtype=np.int64)
    informative = 0
    tied_direction_incidences = 0
    included_tasks = 0
    durations: list[float] = []
    for task in tasks:
        selected = {
            index_by_id[int(label[1])]: timestamp
            for label, timestamp in task["files"].items()
            if label[0] == "git" and int(label[1]) in index_by_id
        }
        if not selected:
            continue
        included_tasks += 1
        durations.append(float(task["end"]) - float(task["start"]))
        indices = sorted(selected)
        marginals[indices] += 1
        if len(indices) >= 2:
            informative += 1
        for left, right in combinations(indices, 2):
            matrix[left, right] += 1
            matrix[right, left] += 1
            left_ts = selected[left]
            right_ts = selected[right]
            if left_ts < right_ts:
                directed[left, right] += 1
            elif right_ts < left_ts:
                directed[right, left] += 1
            else:
                tied_direction_incidences += 1
    duration_array = np.asarray(durations, dtype=float)
    coverage = {
        "task_window_count_with_shared_read": included_tasks,
        "informative_task_window_count": informative,
        "task_window_duration_seconds_median": float(np.median(duration_array)) if duration_array.size else None,
        "task_window_duration_seconds_p95": float(np.percentile(duration_array, 95)) if duration_array.size else None,
        "task_window_duration_seconds_max": float(np.max(duration_array)) if duration_array.size else None,
        "direction_timestamp_tie_incidences": tied_direction_incidences,
    }
    return matrix, directed, marginals, coverage


def cochange_counts(
    git_data: GitData,
    shared_ids: Sequence[int],
) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    _, replay_model = replay_modules()
    index_by_id = {file_id: index for index, file_id in enumerate(shared_ids)}
    matrix = np.zeros((len(shared_ids), len(shared_ids)), dtype=np.int64)
    marginals = np.asarray([len(git_data.state.file_history[file_id]) for file_id in shared_ids], dtype=np.int64)
    for seed in shared_ids:
        _, histories = replay_model.collect_cochange_histories(git_data.state, seed, git_data.commit_count)
        left = index_by_id[seed]
        for candidate, history in histories.items():
            right = index_by_id.get(candidate)
            if right is not None and left < right:
                count = len(history)
                matrix[left, right] = count
                matrix[right, left] = count

    shared_set = set(shared_ids)
    informative_commits = sum(len(shared_set.intersection(members)) >= 2 for members in git_data.commit_members)
    commits_with_shared = sum(bool(shared_set.intersection(members)) for members in git_data.commit_members)
    return matrix, marginals, {
        "commits_with_shared_file": commits_with_shared,
        "informative_commit_count": informative_commits,
    }


def correlation(left: np.ndarray, right: np.ndarray) -> dict[str, float | int | None]:
    finite = np.isfinite(left) & np.isfinite(right)
    x = np.asarray(left[finite], dtype=float)
    y = np.asarray(right[finite], dtype=float)
    if x.size < 2 or np.unique(x).size < 2 or np.unique(y).size < 2:
        return {"pair_coordinates": int(x.size), "spearman": None, "kendall_tau_b": None}
    spearman = stats.spearmanr(x, y).statistic
    kendall = stats.kendalltau(x, y, variant="b").statistic
    return {
        "pair_coordinates": int(x.size),
        "spearman": float(spearman) if math.isfinite(float(spearman)) else None,
        "kendall_tau_b": float(kendall) if math.isfinite(float(kendall)) else None,
    }


def matrix_vectors(
    read_matrix: np.ndarray,
    change_matrix: np.ndarray,
    read_marginals: np.ndarray,
    change_marginals: np.ndarray,
    read_units: int,
    change_units: int,
) -> tuple[dict[str, np.ndarray], dict[str, Any]]:
    upper = np.triu_indices_from(read_matrix, k=1)
    read_all = read_matrix[upper].astype(float)
    change_all = change_matrix[upper].astype(float)
    read_pop_all = (
        read_marginals[upper[0]].astype(float) * read_marginals[upper[1]].astype(float) / max(read_units, 1)
    )
    change_pop_all = (
        change_marginals[upper[0]].astype(float) * change_marginals[upper[1]].astype(float) / max(change_units, 1)
    )
    union = (read_all > 0) | (change_all > 0)
    joint = (read_all > 0) & (change_all > 0)
    vectors = {
        "read": read_all[union],
        "change": change_all[union],
        "read_popularity": read_pop_all[union],
        "change_popularity": change_pop_all[union],
        "read_residual": (read_all - read_pop_all)[union],
        "change_residual": (change_all - change_pop_all)[union],
        "read_all": read_all,
        "change_all": change_all,
        "read_popularity_all": read_pop_all,
        "change_popularity_all": change_pop_all,
        "read_joint": read_all[joint],
        "change_joint": change_all[joint],
    }
    coverage = {
        "all_shared_file_pairs": int(read_all.size),
        "union_nonzero_pair_support": int(union.sum()),
        "joint_nonzero_pair_support": int(joint.sum()),
        "read_nonzero_pair_support": int((read_all > 0).sum()),
        "change_nonzero_pair_support": int((change_all > 0).sum()),
        "double_zero_pair_coordinates_excluded_from_primary_correlation": int((~union).sum()),
    }
    return vectors, coverage


def top_positive(matrix: np.ndarray, labels: Sequence[str], k: int = TOP_K) -> tuple[list[set[int]], dict[str, Any]]:
    tops: list[set[int]] = []
    short = 0
    empty = 0
    boundary_ties = 0
    for seed in range(matrix.shape[0]):
        candidates = [index for index in range(matrix.shape[1]) if index != seed and matrix[seed, index] > 0]
        candidates.sort(key=lambda index: (-float(matrix[seed, index]), path_bytes(labels[index])))
        if not candidates:
            empty += 1
        if len(candidates) < k:
            short += 1
        if len(candidates) > k and matrix[seed, candidates[k - 1]] == matrix[seed, candidates[k]]:
            boundary_ties += 1
        tops.append(set(candidates[:k]))
    return tops, {
        "seed_count": matrix.shape[0],
        "empty_positive_neighbor_seeds": empty,
        "shorter_than_k_seeds": short,
        "k_boundary_tie_seeds": boundary_ties,
    }


def top_popularity(marginals: np.ndarray, labels: Sequence[str], k: int = TOP_K) -> list[set[int]]:
    tops: list[set[int]] = []
    for seed in range(len(marginals)):
        candidates = [index for index in range(len(marginals)) if index != seed]
        candidates.sort(key=lambda index: (-int(marginals[index]), path_bytes(labels[index])))
        tops.append(set(candidates[:k]))
    return tops


def overlap_summary(
    left: Sequence[set[int]],
    right: Sequence[set[int]],
    seeds: Iterable[int] | None = None,
) -> dict[str, float | int | None]:
    selected = list(range(len(left))) if seeds is None else list(seeds)
    values: list[float] = []
    full_values: list[float] = []
    both_full = 0
    for seed in selected:
        union = left[seed] | right[seed]
        if not union:
            continue
        values.append(len(left[seed] & right[seed]) / len(union))
        is_full = len(left[seed]) == TOP_K and len(right[seed]) == TOP_K
        both_full += is_full
        if is_full:
            full_values.append(values[-1])
    return {
        "evaluated_seed_count": len(values),
        "both_full_top_k_seed_count": both_full,
        "mean_jaccard": statistics.fmean(values) if values else None,
        "median_jaccard": statistics.median(values) if values else None,
        "mean_jaccard_both_full_top_k": statistics.fmean(full_values) if full_values else None,
        "median_jaccard_both_full_top_k": statistics.median(full_values) if full_values else None,
        "zero_overlap_seed_count": sum(value == 0 for value in values),
    }


def asymmetry(
    directed: np.ndarray,
    change_matrix: np.ndarray,
    labels: Sequence[str],
) -> dict[str, Any]:
    upper = np.triu_indices_from(directed, k=1)
    forward = directed[upper].astype(np.int64)
    reverse = directed.T[upper].astype(np.int64)
    totals = forward + reverse
    supported = totals > 0

    def support_block(minimum: int) -> dict[str, float | int | None]:
        mask = totals >= minimum
        denominator = int(totals[mask].sum())
        return {
            "pair_count": int(mask.sum()),
            "directional_incidences": denominator,
            "weighted_absolute_imbalance": (
                float(np.abs(forward[mask] - reverse[mask]).sum() / denominator) if denominator else None
            ),
            "observed_both_directions_pair_rate": (
                float(((forward[mask] > 0) & (reverse[mask] > 0)).sum() / mask.sum()) if mask.any() else None
            ),
        }

    outgoing, outgoing_diag = top_positive(directed, labels)
    incoming, incoming_diag = top_positive(directed.T, labels)
    change_top, _ = top_positive(change_matrix, labels)
    return {
        "definition": "first successful occurrence of each file per task window; exact timestamp ties have no direction",
        "all_directional_support": support_block(1),
        "at_least_5_directional_incidences": support_block(5),
        "at_least_10_directional_incidences": support_block(10),
        "successor_vs_predecessor_top10": overlap_summary(outgoing, incoming),
        "successor_vs_cochange_top10": overlap_summary(outgoing, change_top),
        "predecessor_vs_cochange_top10": overlap_summary(incoming, change_top),
        "successor_top10_diagnostics": outgoing_diag,
        "predecessor_top10_diagnostics": incoming_diag,
        "directional_pair_support": int(supported.sum()),
    }


def disagreement_samples(
    read_matrix: np.ndarray,
    change_matrix: np.ndarray,
    directed: np.ndarray,
    labels: Sequence[str],
) -> dict[str, list[dict[str, Any]]]:
    upper = np.triu_indices_from(read_matrix, k=1)
    read_values = read_matrix[upper].astype(float)
    change_values = change_matrix[upper].astype(float)
    union = (read_values > 0) | (change_values > 0)
    pair_positions = np.flatnonzero(union)
    read_union = read_values[union]
    change_union = change_values[union]
    read_ranks = stats.rankdata(-read_union, method="average")
    change_ranks = stats.rankdata(-change_union, method="average")
    denominator = max(len(pair_positions) - 1, 1)
    read_percentiles = 1.0 - (read_ranks - 1.0) / denominator
    change_percentiles = 1.0 - (change_ranks - 1.0) / denominator
    signed = read_percentiles - change_percentiles

    records: list[dict[str, Any]] = []
    for local_index, flat_position in enumerate(pair_positions):
        left = int(upper[0][flat_position])
        right = int(upper[1][flat_position])
        records.append(
            {
                "left": labels[left],
                "right": labels[right],
                "co_read_count": int(read_matrix[left, right]),
                "co_change_count": int(change_matrix[left, right]),
                "co_read_rank": float(read_ranks[local_index]),
                "co_change_rank": float(change_ranks[local_index]),
                "co_read_percentile": float(read_percentiles[local_index]),
                "co_change_percentile": float(change_percentiles[local_index]),
                "signed_percentile_difference": float(signed[local_index]),
                "left_then_right": int(directed[left, right]),
                "right_then_left": int(directed[right, left]),
            }
        )

    tie_key = lambda record: (path_bytes(record["left"]), path_bytes(record["right"]))
    read_high = [record for record in records if record["co_read_count"] > 0]
    read_high.sort(
        key=lambda record: (
            -record["signed_percentile_difference"],
            -record["co_read_count"],
            record["co_change_count"],
            tie_key(record),
        )
    )
    change_high = [record for record in records if record["co_change_count"] > 0]
    change_high.sort(
        key=lambda record: (
            record["signed_percentile_difference"],
            -record["co_change_count"],
            record["co_read_count"],
            tie_key(record),
        )
    )
    return {
        "high_co_read_low_co_change": read_high[:SAMPLE_COUNT],
        "high_co_change_low_co_read": change_high[:SAMPLE_COUNT],
    }


def window_analysis(
    tasks: Sequence[dict[str, Any]],
    window_seconds: int,
    shared_ids: Sequence[int],
    labels: Sequence[str],
    change_matrix: np.ndarray,
    change_marginals: np.ndarray,
    change_coverage: dict[str, Any],
) -> tuple[dict[str, Any], np.ndarray, np.ndarray]:
    index_by_id = {file_id: index for index, file_id in enumerate(shared_ids)}
    read_matrix, directed, read_marginals, read_coverage = read_counts(tasks, index_by_id)
    vectors, pair_coverage = matrix_vectors(
        read_matrix,
        change_matrix,
        read_marginals,
        change_marginals,
        int(read_coverage["task_window_count_with_shared_read"]),
        int(change_coverage["commits_with_shared_file"]),
    )
    read_top, read_top_diag = top_positive(read_matrix, labels)
    change_top, change_top_diag = top_positive(change_matrix, labels)
    read_pop_top = top_popularity(read_marginals, labels)
    change_pop_top = top_popularity(change_marginals, labels)
    observed_overlap = overlap_summary(read_top, change_top)
    eligible_seeds = [seed for seed in range(len(labels)) if read_top[seed] or change_top[seed]]

    correlations = {
        "co_read_vs_co_change": correlation(vectors["read"], vectors["change"]),
        "co_read_vs_co_change_joint_positive_diagnostic": correlation(
            vectors["read_joint"], vectors["change_joint"]
        ),
        "popularity_read_vs_popularity_change": correlation(
            vectors["read_popularity"], vectors["change_popularity"]
        ),
        "co_read_vs_own_popularity": correlation(vectors["read"], vectors["read_popularity"]),
        "co_change_vs_own_popularity": correlation(vectors["change"], vectors["change_popularity"]),
        "popularity_residual_read_vs_change": correlation(
            vectors["read_residual"], vectors["change_residual"]
        ),
        "all_pairs_including_double_zeros_diagnostic": correlation(
            vectors["read_all"], vectors["change_all"]
        ),
    }
    top_k = {
        "co_read_vs_co_change": observed_overlap,
        "popularity_read_vs_popularity_change": overlap_summary(
            read_pop_top, change_pop_top, eligible_seeds
        ),
        "co_read_vs_own_popularity": overlap_summary(read_top, read_pop_top, eligible_seeds),
        "co_change_vs_own_popularity": overlap_summary(change_top, change_pop_top, eligible_seeds),
        "co_read_diagnostics": read_top_diag,
        "co_change_diagnostics": change_top_diag,
    }
    return (
        {
            "window_seconds": window_seconds,
            "coverage": {**read_coverage, **pair_coverage},
            "top10": top_k,
            "correlations_on_union_nonzero_support": correlations,
            "asymmetry": asymmetry(directed, change_matrix, labels),
        },
        read_matrix,
        directed,
    )


def analyze(repository: Path, read_events_path: Path, output_path: Path) -> dict[str, Any]:
    output_dir = output_path.parent
    git_data = extract_git_history(repository, output_dir)
    raw_events, read_header = load_read_events(read_events_path)
    events, mapping_diagnostics = canonicalize_events(raw_events, git_data)
    mapped_ids = {
        int(event["label"][1])
        for event in events
        if event["label"][0] == "git"
    }
    shared_ids = sorted(
        mapped_ids & git_data.source_ids,
        key=lambda file_id: path_bytes(git_data.display_paths[file_id]),
    )
    labels = [git_data.display_paths[file_id] for file_id in shared_ids]
    change_matrix, change_marginals, change_coverage = cochange_counts(git_data, shared_ids)

    coverage = {
        "co_read_source_regions": mapping_diagnostics["co_read_source_region_count"],
        "co_change_source_files": len(git_data.source_ids),
        "shared_file_intersection": len(shared_ids),
        "intersection_fraction_of_co_read_source": (
            len(shared_ids) / mapping_diagnostics["co_read_source_region_count"]
            if mapping_diagnostics["co_read_source_region_count"]
            else None
        ),
        "intersection_fraction_of_co_change_source": (
            len(shared_ids) / len(git_data.source_ids) if git_data.source_ids else None
        ),
        "raw_successful_target_read_events": len(raw_events),
        "mapping": mapping_diagnostics,
        "git": change_coverage,
    }

    # The gate is evaluated from independent windows/commits at the primary
    # setting, before correlations are interpreted.
    primary_tasks = build_task_windows(events, PRIMARY_WINDOW)
    _, _, _, primary_read_coverage = read_counts(primary_tasks, {file_id: index for index, file_id in enumerate(shared_ids)})
    gate_reasons: list[str] = []
    if len(shared_ids) < MIN_SHARED_FILES:
        gate_reasons.append(f"only {len(shared_ids)} shared files; requires at least {MIN_SHARED_FILES}")
    if primary_read_coverage["informative_task_window_count"] < MIN_INFORMATIVE_UNITS:
        gate_reasons.append(
            f"only {primary_read_coverage['informative_task_window_count']} informative 300-second task windows; "
            f"requires at least {MIN_INFORMATIVE_UNITS}"
        )
    if change_coverage["informative_commit_count"] < MIN_INFORMATIVE_UNITS:
        gate_reasons.append(
            f"only {change_coverage['informative_commit_count']} informative commits; requires at least {MIN_INFORMATIVE_UNITS}"
        )

    result: dict[str, Any] = {
        "schema_version": 1,
        "measurement": "co-read-vs-co-change-unification-test",
        "repository": str(repository),
        "coverage": coverage,
        "validity_gate": {
            "passed": not gate_reasons,
            "criteria": {
                "minimum_shared_files": MIN_SHARED_FILES,
                "minimum_informative_task_windows": MIN_INFORMATIVE_UNITS,
                "minimum_informative_commits": MIN_INFORMATIVE_UNITS,
                "minimum_evaluable_top10_seeds": MIN_EVALUABLE_SEEDS,
            },
            "reasons": gate_reasons,
        },
        "read_extraction": read_header,
        "git_extraction": git_data.metadata,
        "protocol": {
            "primary_window_seconds": PRIMARY_WINDOW,
            "sensitivity_windows_seconds": list(WINDOWS),
            "task_window": "maximal same-agent inactivity session; split only when consecutive successful target reads differ by more than W",
            "task_window_caveat": "transitive task duration can exceed W because transcripts expose no explicit task ID",
            "read_pair": "one unordered pair incidence per task window after deduplicating files within the window",
            "change_pair": "one unordered pair incidence per first-parent commit after logical rename resolution",
            "rank_pair_universe": "union of nonzero co-read and co-change support over shared files; absent edge is zero; double-zero pairs excluded",
            "rank_ties": "Spearman average ranks and Kendall tau-b; independence-based p-values omitted",
            "top10": "positive neighbors only, raw-count descending, repository-path bytes ascending tie break",
            "popularity": "source-specific outer product of file unit marginals divided by source unit count",
            "uncertainty": "no pair-level confidence intervals or p-values; pair coordinates are dependent within whole tasks and commits",
        },
        "implementation": {
            "read_events_sha256": sha256_file(read_events_path),
            "scripts_sha256": {
                "extract_reads.py": sha256_file(HERE / "extract_reads.py"),
                "analyze.py": sha256_file(Path(__file__).resolve()),
                "replay_extract.py": sha256_file(REPLAY_ROOT / "extract.py"),
                "replay.py": sha256_file(REPLAY_ROOT / "replay.py"),
            },
            "python": sys.version,
            "numpy": np.__version__,
        },
    }
    if gate_reasons:
        return result

    windows: dict[str, Any] = {}
    primary_read_matrix: np.ndarray | None = None
    primary_directed: np.ndarray | None = None
    for window in WINDOWS:
        tasks = primary_tasks if window == PRIMARY_WINDOW else build_task_windows(events, window)
        window_result, read_matrix, directed = window_analysis(
            tasks,
            window,
            shared_ids,
            labels,
            change_matrix,
            change_marginals,
            change_coverage,
        )
        if window_result["top10"]["co_read_vs_co_change"]["evaluated_seed_count"] < MIN_EVALUABLE_SEEDS:
            result["validity_gate"]["passed"] = False
            result["validity_gate"]["reasons"].append(
                f"{window}s has only {window_result['top10']['co_read_vs_co_change']['evaluated_seed_count']} "
                f"evaluable top-10 seeds; requires at least {MIN_EVALUABLE_SEEDS}"
            )
        if (
            window == PRIMARY_WINDOW
            and window_result["correlations_on_union_nonzero_support"]["co_read_vs_co_change"]["spearman"] is None
        ):
            result["validity_gate"]["passed"] = False
            result["validity_gate"]["reasons"].append(
                "the primary union-support matrices are empty or constant, so rank correlation is undefined"
            )
        windows[str(window)] = window_result
        if window == PRIMARY_WINDOW:
            primary_read_matrix = read_matrix
            primary_directed = directed

    result["windows"] = windows
    if not result["validity_gate"]["passed"]:
        return result
    assert primary_read_matrix is not None and primary_directed is not None
    result["disagreement_samples_300s"] = disagreement_samples(
        primary_read_matrix,
        change_matrix,
        primary_directed,
        labels,
    )

    # Metadata-only sensitivity quantifies dependence on the conservative
    # successful-result/input fallback used by old and subagent transcripts.
    metadata_events = [event for event in events if event.get("path_source") == "result_metadata"]
    metadata_tasks = build_task_windows(metadata_events, PRIMARY_WINDOW)
    metadata_result, _, _ = window_analysis(
        metadata_tasks,
        PRIMARY_WINDOW,
        shared_ids,
        labels,
        change_matrix,
        change_marginals,
        change_coverage,
    )
    result["result_metadata_only_sensitivity_300s"] = {
        "read_event_count": len(metadata_events),
        "note": "same shared file universe retained so this isolates event-path provenance, not coverage selection",
        **metadata_result,
    }

    unambiguous_identity_events = [
        event for event in events if not event.get("fallback_identity_from_copied_prefix", False)
    ]
    unambiguous_tasks = build_task_windows(unambiguous_identity_events, PRIMARY_WINDOW)
    unambiguous_result, _, _ = window_analysis(
        unambiguous_tasks,
        PRIMARY_WINDOW,
        shared_ids,
        labels,
        change_matrix,
        change_marginals,
        change_coverage,
    )
    result["copied_prefix_identity_exclusion_sensitivity_300s"] = {
        "read_event_count": len(unambiguous_identity_events),
        "excluded_read_event_count": len(events) - len(unambiguous_identity_events),
        "note": "excludes globally deduplicated root Read IDs whose copied transcripts disagree on fallback sessionId",
        **unambiguous_result,
    }
    return result


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def main() -> int:
    args = parse_args()
    repository = args.repository.resolve()
    read_events = args.read_events.resolve()
    output = args.output.resolve()
    if not (repository / ".git").exists():
        raise SystemExit(f"target is not a Git worktree: {repository}")
    if not read_events.is_file():
        raise SystemExit(f"read-event stream does not exist: {read_events}")
    result = analyze(repository, read_events, output)
    atomic_write_json(output, result)
    print(
        f"wrote {output}; intersection={result['coverage']['shared_file_intersection']}, "
        f"gate={'passed' if result['validity_gate']['passed'] else 'failed'}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
