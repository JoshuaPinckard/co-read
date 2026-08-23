"""Run the strict temporal replay and the five metadata/path-only models."""

from __future__ import annotations

import argparse
import difflib
import functools
import gzip
import hashlib
import heapq
import json
import math
import os
import platform
import random
import statistics
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from common import (
    CAP_COMMITS,
    CAP_THRESHOLD_REACHABLE_COMMITS,
    CLONE_ROOT,
    CORPUS_PATH,
    DECAY_HALF_LIFE_COMMITS,
    RANDOM_SEED,
    RESULT_ROOT,
    SCHEMA_VERSION,
    STREAM_ROOT,
    atomic_write_json,
    ensure_directories,
    load_json,
    run_git,
    selected_repositories,
    utc_now,
)


MODEL_KEYS = (
    "cochange_time_decayed",
    "cochange_plain_confidence",
    "path_name_similarity",
    "popularity_control",
    "random_draw",
)
HARNESS_FILES = ("common.py", "clone.py", "extract.py", "replay.py")

# This changes only the physical representation of exact pair counts. Commits at
# or below the threshold are materialized; larger cliques remain factorized and
# are expanded at query time. Tests compare both representations.
PAIR_MATERIALIZE_MAX_FILES = 64
MAX_PREDICTIONS = 20


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", action="append", dest="repos", help="Repository slug; repeatable.")
    return parser.parse_args()


def path_bytes(path: str) -> bytes:
    return path.encode("utf-8", errors="surrogateescape")


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def harness_hashes() -> tuple[str, dict[str, str]]:
    directory = Path(__file__).resolve().parent
    per_file: dict[str, str] = {}
    combined = hashlib.sha256()
    for name in HARNESS_FILES:
        content = (directory / name).read_bytes()
        per_file[name] = hashlib.sha256(content).hexdigest()
        combined.update(name.encode("ascii") + b"\0" + content + b"\0")
    return combined.hexdigest(), per_file


def directory_parts(path: str) -> tuple[str, ...]:
    return tuple(path.split("/")[:-1])


def basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


@functools.lru_cache(maxsize=500_000)
def _canonical_basename_similarity(left: str, right: str) -> float:
    return difflib.SequenceMatcher(None, left, right, autojunk=False).ratio()


def basename_similarity(left: str, right: str) -> float:
    # Canonicalize before entering the cache so reversed pairs share one entry.
    if path_bytes(left) > path_bytes(right):
        left, right = right, left
    return _canonical_basename_similarity(left, right)


@dataclass(frozen=True)
class RankedResult:
    ids: tuple[int, ...]
    top1_tie: bool | None
    k10_boundary_tie: bool | None
    k20_boundary_tie: bool | None


@dataclass(frozen=True)
class ResolvedChange:
    status: str
    file_id: int
    path: str | None = None
    old_path: str | None = None
    new_path: str | None = None


class ReplayState:
    """Mutable indexes for one repository. There is deliberately no class hierarchy."""

    def __init__(
        self,
        initial_files: Sequence[str],
        pair_materialize_max_files: int = PAIR_MATERIALIZE_MAX_FILES,
        max_commit_age: int = 100,
    ) -> None:
        self.pair_materialize_max_files = pair_materialize_max_files
        self.decay_weights = tuple(
            math.exp(-math.log(2.0) * age / DECAY_HALF_LIFE_COMMITS)
            for age in range(max_commit_age + 1)
        )
        self.next_file_id = 0
        self.path_to_id: dict[str, int] = {}
        self.id_to_path: dict[int, str] = {}
        self.existing_ids: set[int] = set()
        self.existing_vector: list[int] = []
        self.existing_position: dict[int, int] = {}
        self.prefix_members: dict[tuple[str, ...], set[int]] = {}

        # Commit indexes are retained so both physical pair representations use
        # identical integer counts and math.fsum over identical decay terms.
        self.file_history: dict[int, list[int]] = {}
        # Pair-history list objects are shared by the two directions of adjacency.
        self.adjacency: dict[int, dict[int, list[int]]] = {}
        # Large commit records are shared immutable (index, ids) tuples.
        self.factor_history: dict[int, list[tuple[int, tuple[int, ...]]]] = {}

        self.popularity_heap: list[tuple[int, bytes, int, int]] = []
        self.popularity_version: dict[int, int] = {}
        self.paths_ever_allocated: set[str] = set()
        self.readded_path_identity_count = 0
        self.last_folded_index = -1
        self.materialized_commit_count = 0
        self.factorized_commit_count = 0
        self.max_factorized_commit_size = 0

        for path in sorted(initial_files, key=path_bytes):
            file_id = self.allocate_identity(path)
            self.add_live(file_id, path)
            self.push_popularity(file_id)

    def allocate_identity(self, path: str) -> int:
        if path in self.paths_ever_allocated:
            self.readded_path_identity_count += 1
        self.paths_ever_allocated.add(path)
        file_id = self.next_file_id
        self.next_file_id += 1
        self.id_to_path[file_id] = path
        self.file_history[file_id] = []
        self.adjacency[file_id] = {}
        self.factor_history[file_id] = []
        self.popularity_version[file_id] = 0
        return file_id

    def add_live(self, file_id: int, path: str) -> None:
        if path in self.path_to_id or file_id in self.existing_ids:
            raise ValueError(f"cannot add already-live file identity/path: {file_id}, {path!r}")
        self.id_to_path[file_id] = path
        self.path_to_id[path] = file_id
        self.existing_ids.add(file_id)
        self.existing_position[file_id] = len(self.existing_vector)
        self.existing_vector.append(file_id)
        parts = directory_parts(path)
        for depth in range(1, len(parts) + 1):
            self.prefix_members.setdefault(parts[:depth], set()).add(file_id)

    def remove_live(self, file_id: int, expected_path: str) -> None:
        if self.path_to_id.get(expected_path) != file_id or file_id not in self.existing_ids:
            raise ValueError(f"cannot remove non-live file identity/path: {file_id}, {expected_path!r}")
        parts = directory_parts(expected_path)
        for depth in range(1, len(parts) + 1):
            prefix = parts[:depth]
            members = self.prefix_members[prefix]
            members.remove(file_id)
            if not members:
                del self.prefix_members[prefix]
        del self.path_to_id[expected_path]
        self.existing_ids.remove(file_id)
        position = self.existing_position.pop(file_id)
        last_id = self.existing_vector.pop()
        if position < len(self.existing_vector):
            self.existing_vector[position] = last_id
            self.existing_position[last_id] = position

    def push_popularity(self, file_id: int) -> None:
        self.popularity_version[file_id] += 1
        version = self.popularity_version[file_id]
        count = len(self.file_history[file_id])
        heapq.heappush(
            self.popularity_heap,
            (-count, path_bytes(self.id_to_path[file_id]), file_id, version),
        )

    def assert_query_generation(self, commit_index: int) -> None:
        if self.last_folded_index != commit_index - 1:
            raise AssertionError(
                f"leakage invariant failed: querying commit {commit_index} after folding through "
                f"{self.last_folded_index}"
            )

    def decay_weight(self, age: int) -> float:
        if age < 0 or age >= len(self.decay_weights):
            raise AssertionError(f"decay age {age} is outside the precomputed replay range")
        return self.decay_weights[age]

    def resolve_changes(self, changes: Sequence[dict[str, Any]]) -> list[ResolvedChange]:
        removed_paths: set[str] = set()
        destination_paths: set[str] = set()
        source_ids: set[int] = set()

        for change in changes:
            status = change["status"]
            if status == "A":
                destination = change["path"]
                if destination in destination_paths:
                    raise ValueError(f"duplicate destination path in one commit: {destination!r}")
                destination_paths.add(destination)
            elif status in {"M", "D"}:
                path = change["path"]
                if path not in self.path_to_id:
                    raise ValueError(f"{status} path absent from pre-commit tree: {path!r}")
                file_id = self.path_to_id[path]
                if file_id in source_ids:
                    raise ValueError(f"file identity touched more than once in one commit: {path!r}")
                source_ids.add(file_id)
                if status == "D":
                    removed_paths.add(path)
            elif status == "R":
                old_path = change["old_path"]
                new_path = change["new_path"]
                if old_path not in self.path_to_id:
                    raise ValueError(f"rename source absent from pre-commit tree: {old_path!r}")
                file_id = self.path_to_id[old_path]
                if file_id in source_ids:
                    raise ValueError(f"file identity touched more than once in one commit: {old_path!r}")
                if new_path in destination_paths:
                    raise ValueError(f"duplicate destination path in one commit: {new_path!r}")
                source_ids.add(file_id)
                removed_paths.add(old_path)
                destination_paths.add(new_path)
            else:
                raise ValueError(f"unsupported normalized status: {status!r}")

        for destination in destination_paths:
            if destination in self.path_to_id and destination not in removed_paths:
                raise ValueError(f"destination already exists and is not removed in commit: {destination!r}")

        resolved: list[ResolvedChange] = []
        for change in changes:
            status = change["status"]
            if status == "A":
                path = change["path"]
                resolved.append(ResolvedChange(status="A", file_id=self.allocate_identity(path), path=path))
            elif status in {"M", "D"}:
                path = change["path"]
                resolved.append(ResolvedChange(status=status, file_id=self.path_to_id[path], path=path))
            else:
                old_path = change["old_path"]
                resolved.append(
                    ResolvedChange(
                        status="R",
                        file_id=self.path_to_id[old_path],
                        old_path=old_path,
                        new_path=change["new_path"],
                    )
                )
        return resolved

    def update_pair(self, left: int, right: int, commit_index: int) -> None:
        history = self.adjacency[left].get(right)
        if history is None:
            history = []
            self.adjacency[left][right] = history
            self.adjacency[right][left] = history
        history.append(commit_index)

    def fold(self, commit_index: int, resolved: Sequence[ResolvedChange]) -> None:
        if self.last_folded_index != commit_index - 1:
            raise AssertionError(
                f"fold order invariant failed at {commit_index}; last folded {self.last_folded_index}"
            )
        touched_ids = tuple(dict.fromkeys(change.file_id for change in resolved))
        if len(touched_ids) != len(resolved):
            raise ValueError(f"duplicate logical file after status resolution at commit {commit_index}")

        for file_id in touched_ids:
            self.file_history[file_id].append(commit_index)

        if len(touched_ids) <= self.pair_materialize_max_files:
            self.materialized_commit_count += 1
            for left_index, left in enumerate(touched_ids):
                for right in touched_ids[left_index + 1 :]:
                    self.update_pair(left, right, commit_index)
        else:
            self.factorized_commit_count += 1
            self.max_factorized_commit_size = max(self.max_factorized_commit_size, len(touched_ids))
            factor = (commit_index, touched_ids)
            for file_id in touched_ids:
                self.factor_history[file_id].append(factor)

        # Resolve the tree transition in a batch: all old names disappear before
        # any destination names appear, which also handles rename swaps.
        for change in resolved:
            if change.status == "D":
                assert change.path is not None
                self.remove_live(change.file_id, change.path)
            elif change.status == "R":
                assert change.old_path is not None
                self.remove_live(change.file_id, change.old_path)
        for change in resolved:
            if change.status == "A":
                assert change.path is not None
                self.add_live(change.file_id, change.path)
            elif change.status == "R":
                assert change.new_path is not None
                self.add_live(change.file_id, change.new_path)

        for file_id in touched_ids:
            if file_id in self.existing_ids:
                self.push_popularity(file_id)
        self.last_folded_index = commit_index


def tie_diagnostics(ordered: Sequence[tuple[Any, int]]) -> tuple[bool, bool, bool]:
    top1 = len(ordered) > 1 and ordered[0][0] == ordered[1][0]
    at10 = len(ordered) > 10 and ordered[9][0] == ordered[10][0]
    at20 = len(ordered) > 20 and ordered[19][0] == ordered[20][0]
    return top1, at10, at20


def rank_numeric_scores(state: ReplayState, scores: dict[int, float]) -> RankedResult:
    ordered = sorted(
        ((score, file_id) for file_id, score in scores.items()),
        key=lambda item: (-item[0], path_bytes(state.id_to_path[item[1]]), item[1]),
    )
    top1, at10, at20 = tie_diagnostics(ordered)
    return RankedResult(tuple(file_id for _, file_id in ordered[:MAX_PREDICTIONS]), top1, at10, at20)


def collect_cochange_histories(
    state: ReplayState,
    seed: int,
    commit_index: int,
) -> tuple[list[int], dict[int, list[int]]]:
    state.assert_query_generation(commit_index)
    seed_history = state.file_history[seed]
    if seed_history and seed_history[-1] >= commit_index:
        raise AssertionError(f"seed history leakage: {seed_history[-1]}, query {commit_index}")
    candidate_histories: dict[int, list[int]] = {}
    for candidate, pair_history in state.adjacency[seed].items():
        if candidate not in state.existing_ids or candidate == seed:
            continue
        if pair_history[-1] >= commit_index:
            raise AssertionError(f"pair index leakage: pair updated at {pair_history[-1]}, query {commit_index}")
        candidate_histories[candidate] = pair_history.copy()
    for historical_index, members in state.factor_history[seed]:
        if historical_index >= commit_index:
            raise AssertionError(f"factor index leakage: factor {historical_index}, query {commit_index}")
        for candidate in members:
            if candidate != seed and candidate in state.existing_ids:
                candidate_histories.setdefault(candidate, []).append(historical_index)
    return seed_history, candidate_histories


def score_cochange_histories(
    state: ReplayState,
    seed_history: Sequence[int],
    candidate_histories: dict[int, list[int]],
    commit_index: int,
    *,
    decayed: bool,
) -> dict[int, float]:
    if not seed_history:
        return {}
    denominator = (
        math.fsum(state.decay_weight(commit_index - historical_index) for historical_index in seed_history)
        if decayed
        else len(seed_history)
    )
    if denominator <= 0:
        return {}

    if decayed:
        return {
            candidate: math.fsum(
                state.decay_weight(commit_index - historical_index)
                for historical_index in history
            )
            / float(denominator)
            for candidate, history in candidate_histories.items()
        }
    return {
        candidate: len(history) / float(denominator)
        for candidate, history in candidate_histories.items()
    }


def rank_cochange_histories(
    state: ReplayState,
    seed_history: Sequence[int],
    candidate_histories: dict[int, list[int]],
    commit_index: int,
    *,
    decayed: bool,
) -> RankedResult:
    scores = score_cochange_histories(
        state,
        seed_history,
        candidate_histories,
        commit_index,
        decayed=decayed,
    )
    if not scores:
        return RankedResult((), False, False, False)
    return rank_numeric_scores(state, scores)


def cochange_query(state: ReplayState, seed: int, commit_index: int, *, decayed: bool) -> RankedResult:
    seed_history, candidate_histories = collect_cochange_histories(state, seed, commit_index)
    return rank_cochange_histories(
        state,
        seed_history,
        candidate_histories,
        commit_index,
        decayed=decayed,
    )


def path_query(state: ReplayState, seed: int, commit_index: int) -> RankedResult:
    state.assert_query_generation(commit_index)
    seed_path = state.id_to_path[seed]
    seed_parts = directory_parts(seed_path)
    seed_basename = basename(seed_path)
    scored: list[tuple[tuple[int, float], int]] = []
    deeper_members: set[int] | frozenset[int] = frozenset()

    # Prefix depth is the primary key; basename similarity breaks equal-depth
    # groups. Once a complete depth group gives >20 candidates, shallower groups
    # cannot enter the top 20 or tie its boundary.
    for depth in range(len(seed_parts), -1, -1):
        members = state.prefix_members.get(seed_parts[:depth], set()) if depth else state.existing_ids
        for candidate in members:
            if candidate == seed or candidate in deeper_members:
                continue
            similarity = basename_similarity(seed_basename, basename(state.id_to_path[candidate]))
            scored.append(((depth, similarity), candidate))
        if len(scored) > MAX_PREDICTIONS:
            break
        deeper_members = members

    ordered = sorted(
        scored,
        key=lambda item: (-item[0][0], -item[0][1], path_bytes(state.id_to_path[item[1]]), item[1]),
    )
    top1, at10, at20 = tie_diagnostics(ordered)
    return RankedResult(tuple(file_id for _, file_id in ordered[:MAX_PREDICTIONS]), top1, at10, at20)


def popularity_query(state: ReplayState, seed: int, commit_index: int) -> RankedResult:
    state.assert_query_generation(commit_index)
    popped: list[tuple[int, bytes, int, int]] = []
    ordered: list[tuple[int, int]] = []
    while state.popularity_heap and len(ordered) <= MAX_PREDICTIONS:
        item = heapq.heappop(state.popularity_heap)
        negative_count, encoded_path, file_id, version = item
        if (
            file_id not in state.existing_ids
            or version != state.popularity_version[file_id]
            or encoded_path != path_bytes(state.id_to_path[file_id])
            or negative_count != -len(state.file_history[file_id])
        ):
            continue
        popped.append(item)
        if file_id != seed:
            ordered.append((-negative_count, file_id))
    for item in popped:
        heapq.heappush(state.popularity_heap, item)

    # Heap ordering already uses score descending and raw path ascending.
    top1, at10, at20 = tie_diagnostics(ordered)
    return RankedResult(tuple(file_id for _, file_id in ordered[:MAX_PREDICTIONS]), top1, at10, at20)


def random_query(
    state: ReplayState,
    seed: int,
    commit_index: int,
    repository_slug: str,
) -> RankedResult:
    state.assert_query_generation(commit_index)
    population_size = len(state.existing_vector) - 1
    if population_size <= 0:
        return RankedResult((), None, None, None)
    seed_material = "\0".join(
        (
            RANDOM_SEED,
            repository_slug,
            str(commit_index),
            str(seed),
            state.id_to_path[seed],
        )
    ).encode("utf-8", errors="surrogateescape")
    generator = random.Random(int.from_bytes(hashlib.sha256(seed_material).digest(), "big"))
    seed_position = state.existing_position[seed]
    indices = generator.sample(range(population_size), min(MAX_PREDICTIONS, population_size))
    selected = tuple(
        state.existing_vector[index if index < seed_position else index + 1]
        for index in indices
    )
    return RankedResult(selected, None, None, None)


def new_model_accumulator() -> dict[str, Any]:
    return {
        "queries": 0,
        "p1_hits": 0,
        "p10_hits": 0,
        "r10_sum": 0.0,
        "r20_sum": 0.0,
        "empty_queries": 0,
        "timings_ns": [],
        "top1_tie_queries": 0,
        "k10_boundary_tie_queries": 0,
        "k20_boundary_tie_queries": 0,
        "tie_diagnostics_queries": 0,
    }


def score_prediction(
    accumulator: dict[str, Any],
    ranked: RankedResult,
    ground_truth: set[int],
    elapsed_ns: int,
) -> dict[str, int | float]:
    predictions = ranked.ids
    hits1 = int(bool(predictions) and predictions[0] in ground_truth)
    hits10 = sum(file_id in ground_truth for file_id in predictions[:10])
    hits20 = sum(file_id in ground_truth for file_id in predictions[:20])
    recall10 = hits10 / len(ground_truth)
    recall20 = hits20 / len(ground_truth)

    accumulator["queries"] += 1
    accumulator["p1_hits"] += hits1
    accumulator["p10_hits"] += hits10
    accumulator["r10_sum"] += recall10
    accumulator["r20_sum"] += recall20
    accumulator["empty_queries"] += not predictions
    accumulator["timings_ns"].append(elapsed_ns)
    if ranked.top1_tie is not None:
        accumulator["tie_diagnostics_queries"] += 1
        accumulator["top1_tie_queries"] += ranked.top1_tie
        accumulator["k10_boundary_tie_queries"] += ranked.k10_boundary_tie
        accumulator["k20_boundary_tie_queries"] += ranked.k20_boundary_tie
    return {
        "p1_hits": hits1,
        "p10_hits": hits10,
        "r10_sum": recall10,
        "r20_sum": recall20,
        "empty_queries": int(not predictions),
    }


def finalize_model(accumulator: dict[str, Any]) -> dict[str, Any]:
    query_count = int(accumulator["queries"])
    timings = accumulator.pop("timings_ns")
    if query_count == 0:
        return {
            **accumulator,
            "p_at_1": None,
            "p_at_10": None,
            "r_at_10": None,
            "r_at_20": None,
            "empty_radius_rate": None,
            "median_query_microseconds": None,
            "min_query_microseconds": None,
            "max_query_microseconds": None,
        }
    return {
        **accumulator,
        "p_at_1": accumulator["p1_hits"] / query_count,
        "p_at_10": accumulator["p10_hits"] / (10 * query_count),
        "r_at_10": accumulator["r10_sum"] / query_count,
        "r_at_20": accumulator["r20_sum"] / query_count,
        "empty_radius_rate": accumulator["empty_queries"] / query_count,
        "median_query_microseconds": statistics.median(timings) / 1_000.0,
        "min_query_microseconds": min(timings) / 1_000.0,
        "max_query_microseconds": max(timings) / 1_000.0,
    }


def assert_predictions(state: ReplayState, seed: int, ranked: RankedResult) -> None:
    if len(ranked.ids) > MAX_PREDICTIONS or len(set(ranked.ids)) != len(ranked.ids):
        raise AssertionError("model returned too many or duplicate predictions")
    if seed in ranked.ids:
        raise AssertionError("model returned its seed")
    if any(file_id not in state.existing_ids for file_id in ranked.ids):
        raise AssertionError("model returned a file absent at claim time")


def read_head_paths(repository: Path) -> set[str]:
    result = run_git(repository, ["ls-tree", "-r", "--name-only", "-z", "HEAD"], text=False)
    assert isinstance(result.stdout, bytes)
    return {
        token.decode("utf-8", errors="surrogateescape")
        for token in result.stdout.split(b"\0")
        if token
    }


def run_repository(spec: dict[str, str], corpus_record: dict[str, Any]) -> dict[str, Any]:
    slug = spec["slug"]
    stream_path = STREAM_ROOT / f"{slug}.jsonl.gz"
    stream_meta = load_json(STREAM_ROOT / f"{slug}.meta.json", default={}) or {}
    if stream_meta.get("status") != "ok" or not stream_path.exists():
        raise RuntimeError(f"extraction unavailable: {stream_meta.get('failure', 'missing stream')}")
    actual_stream_hash = sha256_path(stream_path)
    if actual_stream_hash != stream_meta.get("stream_sha256"):
        raise ValueError("extraction stream SHA-256 does not match its metadata")

    started_at = utc_now()
    harness_sha256, harness_file_sha256 = harness_hashes()
    _canonical_basename_similarity.cache_clear()
    accumulators = {model: new_model_accumulator() for model in MODEL_KEYS}
    eligible_commits: list[dict[str, Any]] = []
    status_counts: dict[str, int] = {}
    total_changes = 0
    created_files_excluded_from_ground_truth = 0
    leakage_generation_assertions = 0
    prediction_universe_assertions = 0
    ground_truth_subset_assertions = 0
    commits_processed = 0

    with gzip.open(stream_path, "rt", encoding="utf-8", errors="surrogatepass") as handle:
        first_line = handle.readline()
        if not first_line:
            raise ValueError("empty extraction stream")
        header = json.loads(first_line)
        if header.get("type") != "header" or header.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("unsupported or missing stream header")
        if header.get("source_head_sha") != corpus_record.get("resolved_head_sha"):
            raise ValueError("stream HEAD does not match corpus manifest")
        log_arguments = header.get("git_log_arguments", [])
        required_log_arguments = {
            "--first-parent",
            "--reverse",
            "--root",
            "--diff-merges=first-parent",
            "--find-renames=50%",
            "-l0",
            "--name-status",
            "-z",
        }
        missing_arguments = sorted(required_log_arguments - set(log_arguments))
        if missing_arguments:
            raise ValueError(f"stream extraction protocol is stale; missing {missing_arguments}")
        expected_cap = int(corpus_record["reachable_commit_count"]) > CAP_THRESHOLD_REACHABLE_COMMITS
        if bool(header.get("capped")) != expected_cap:
            raise ValueError("stream cap decision does not match the corpus manifest")
        if expected_cap and f"--max-count={CAP_COMMITS}" not in log_arguments:
            raise ValueError("capped stream is missing the required max-count argument")
        if not expected_cap and any(str(argument).startswith("--max-count=") for argument in log_arguments):
            raise ValueError("uncapped stream unexpectedly has a max-count argument")
        state = ReplayState(header["initial_files"], max_commit_age=int(stream_meta["commit_count"]) + 1)

        for line in handle:
            commit = json.loads(line)
            if commit.get("type") != "commit":
                raise ValueError("non-commit record after stream header")
            commit_index = int(commit["index"])
            if commit_index != commits_processed:
                raise ValueError(f"non-contiguous commit index {commit_index}; expected {commits_processed}")
            state.assert_query_generation(commit_index)
            resolved = state.resolve_changes(commit["changes"])
            precommit_existing = frozenset(state.existing_ids)
            eligible_ids = tuple(change.file_id for change in resolved if change.status != "A")
            if len(set(eligible_ids)) != len(eligible_ids):
                raise AssertionError("eligible ground-truth files are not unique")
            if any(file_id not in precommit_existing for file_id in eligible_ids):
                raise AssertionError("ground truth includes a file absent at claim time")
            ground_truth_subset_assertions += 1

            created_count = sum(change.status == "A" for change in resolved)
            created_files_excluded_from_ground_truth += created_count
            total_changes += len(resolved)
            for change in resolved:
                status_counts[change.status] = status_counts.get(change.status, 0) + 1

            commit_metrics: dict[str, dict[str, int | float]] = {
                model: {"p1_hits": 0, "p10_hits": 0, "r10_sum": 0.0, "r20_sum": 0.0, "empty_queries": 0}
                for model in MODEL_KEYS
            }
            if len(eligible_ids) >= 2:
                for seed in eligible_ids:
                    state.assert_query_generation(commit_index)
                    leakage_generation_assertions += 1
                    targets = set(eligible_ids)
                    targets.remove(seed)
                    if not targets.issubset(precommit_existing) or seed not in precommit_existing:
                        raise AssertionError("query ground truth is not a subset of the pre-commit universe")

                    # Candidate-history expansion is identical for the two co-change
                    # formulas. Measure it once, reuse it, and charge its full wall
                    # time to both models so neither latency hides shared work.
                    preparation_start_ns = time.perf_counter_ns()
                    seed_history, candidate_histories = collect_cochange_histories(state, seed, commit_index)
                    preparation_ns = time.perf_counter_ns() - preparation_start_ns
                    for model, decayed in (
                        ("cochange_time_decayed", True),
                        ("cochange_plain_confidence", False),
                    ):
                        scoring_start_ns = time.perf_counter_ns()
                        ranked = rank_cochange_histories(
                            state,
                            seed_history,
                            candidate_histories,
                            commit_index,
                            decayed=decayed,
                        )
                        elapsed_ns = preparation_ns + (time.perf_counter_ns() - scoring_start_ns)
                        assert_predictions(state, seed, ranked)
                        prediction_universe_assertions += 1
                        contribution = score_prediction(accumulators[model], ranked, targets, elapsed_ns)
                        for key, value in contribution.items():
                            commit_metrics[model][key] += value

                    query_functions = (
                        ("path_name_similarity", lambda: path_query(state, seed, commit_index)),
                        ("popularity_control", lambda: popularity_query(state, seed, commit_index)),
                        (
                            "random_draw",
                            lambda: random_query(
                                state,
                                seed,
                                commit_index,
                                slug,
                            ),
                        ),
                    )
                    for model, query_function in query_functions:
                        start_ns = time.perf_counter_ns()
                        ranked = query_function()
                        elapsed_ns = time.perf_counter_ns() - start_ns
                        assert_predictions(state, seed, ranked)
                        prediction_universe_assertions += 1
                        contribution = score_prediction(accumulators[model], ranked, targets, elapsed_ns)
                        for key, value in contribution.items():
                            commit_metrics[model][key] += value

                eligible_commits.append(
                    {
                        "index": commit_index,
                        "sha": commit["sha"],
                        "timestamp": commit["timestamp"],
                        "touched_file_count": len(resolved),
                        "eligible_file_count": len(eligible_ids),
                        "created_file_count_excluded": created_count,
                        "query_count": len(eligible_ids),
                        "models": commit_metrics,
                    }
                )

            if state.existing_ids != set(precommit_existing):
                raise AssertionError("a model query mutated the claim-time file universe")
            state.fold(commit_index, resolved)
            commits_processed += 1

    if commits_processed != int(stream_meta["commit_count"]):
        raise ValueError(f"processed {commits_processed} commits, stream metadata says {stream_meta['commit_count']}")
    expected_head_paths = read_head_paths(CLONE_ROOT / slug)
    actual_head_paths = set(state.path_to_id)
    if actual_head_paths != expected_head_paths:
        missing = sorted(expected_head_paths - actual_head_paths, key=path_bytes)[:10]
        extra = sorted(actual_head_paths - expected_head_paths, key=path_bytes)[:10]
        raise AssertionError(f"final replay tree differs from HEAD; missing={missing!r}, extra={extra!r}")

    query_counts = sorted((entry["query_count"] for entry in eligible_commits), reverse=True)
    total_queries = sum(query_counts)
    top1_queries = query_counts[0] if query_counts else 0
    top5_queries = sum(query_counts[:5])
    models = {model: finalize_model(accumulators[model]) for model in MODEL_KEYS}
    if models["cochange_time_decayed"]["empty_queries"] != models["cochange_plain_confidence"]["empty_queries"]:
        raise AssertionError("plain and decayed co-change disagree on empty-radius count")
    for control in ("path_name_similarity", "popularity_control", "random_draw"):
        if models[control]["empty_queries"] != 0 and total_queries:
            raise AssertionError(f"{control} returned an empty radius for an eligible query")

    return {
        "schema_version": SCHEMA_VERSION,
        "measurement": "cross-language-co-change-replay",
        "status": "ok",
        "repository": spec,
        "source_head_sha": corpus_record["resolved_head_sha"],
        "started_at_utc": started_at,
        "completed_at_utc": utc_now(),
        "protocol": {
            "history_order": "first-parent graph order, oldest to newest within replay window",
            "merge_diff": "once against first parent",
            "cap": header.get("cap_reason"),
            "ground_truth": "pre-commit touched logical files; additions excluded; deletions included",
            "rename_treatment": "emitted R is one stable identity, old path at query then migrated to new path",
            "rename_detection": "git --find-renames=50%; similarity detection may lazily fetch blobs but models do not inspect contents",
            "delete_readd_treatment": "a deleted path later added receives a new logical identity and no stale history",
            "candidate_universe": "files existing immediately before the query commit, excluding seed",
            "path_score": "descending tuple(shared directory-prefix component depth, case-sensitive full-basename SequenceMatcher ratio); raw-path tie",
            "precision_denominator": "fixed K; short rankings are padded with misses",
            "random_seed": RANDOM_SEED,
            "random_draw": "uniform without replacement from the pre-commit candidate universe",
            "decay_half_life_commits": DECAY_HALF_LIFE_COMMITS,
            "pair_representation_threshold": PAIR_MATERIALIZE_MAX_FILES,
            "pair_representation_note": "exact representation optimization only; no commit excluded or downweighted",
        },
        "coverage": {
            "first_parent_commits_at_head": corpus_record["first_parent_commit_count"],
            "commits_replayed": commits_processed,
            "left_truncated": bool(header.get("capped")),
            "initial_tree_file_count": len(header["initial_files"]),
            "eligible_commit_count": len(eligible_commits),
            "query_count": total_queries,
            "largest_query_commit_queries": top1_queries,
            "largest_query_commit_share": top1_queries / total_queries if total_queries else None,
            "top_five_query_commits_queries": top5_queries,
            "top_five_query_commits_share": top5_queries / total_queries if total_queries else None,
            "total_file_change_records": total_changes,
            "created_files_excluded_from_ground_truth": created_files_excluded_from_ground_truth,
            "delete_then_readd_new_identity_count": state.readded_path_identity_count,
            "status_counts": dict(sorted(status_counts.items())),
        },
        "models": models,
        "eligible_commits": eligible_commits,
        "implementation": {
            "harness_sha256": harness_sha256,
            "harness_file_sha256": harness_file_sha256,
            "stream_sha256": actual_stream_hash,
            "python": platform.python_version(),
            "platform": platform.platform(),
            "processor": platform.processor(),
            "timing_clock": (
                "time.perf_counter_ns around ranked-list production only; shared co-change candidate-history "
                "expansion is measured once per seed and charged in full to each co-change model"
            ),
            "model_query_order": list(MODEL_KEYS),
            "materialized_commit_count": state.materialized_commit_count,
            "factorized_commit_count": state.factorized_commit_count,
            "max_factorized_commit_size": state.max_factorized_commit_size,
        },
        "invariants": {
            "query_before_fold_assertions": leakage_generation_assertions,
            "prediction_precommit_universe_assertions": prediction_universe_assertions,
            "ground_truth_precommit_subset_assertions": ground_truth_subset_assertions,
            "final_tree_matches_head": True,
            "plain_decayed_empty_counts_match": True,
            "always_available_models_nonempty": True,
        },
    }


def failed_result(spec: dict[str, str], stage: str, exc: Exception) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "measurement": "cross-language-co-change-replay",
        "status": "failed",
        "repository": spec,
        "failure_stage": stage,
        "failure_type": type(exc).__name__,
        "failure": str(exc),
        "completed_at_utc": utc_now(),
    }


def main() -> None:
    ensure_directories()
    corpus = load_json(CORPUS_PATH, default={}) or {}
    corpus_records = corpus.get("repositories", {})
    for spec in selected_repositories(parse_args().repos):
        output_path = RESULT_ROOT / f"{spec['slug']}.json"
        corpus_record = corpus_records.get(spec["slug"])
        if not corpus_record or corpus_record.get("status") != "ok":
            result = failed_result(spec, "clone", RuntimeError("clone did not complete successfully"))
        else:
            try:
                result = run_repository(spec, corpus_record)
            except Exception as exc:
                result = failed_result(spec, "extract_or_replay", exc)
        atomic_write_json(output_path, result)
        print(f"{spec['name']}: {result['status']}", flush=True)


if __name__ == "__main__":
    main()
