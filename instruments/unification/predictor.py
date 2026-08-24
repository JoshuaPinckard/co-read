"""Strict-temporal co-read versus co-change prediction replay.

This runner preserves the query, ground-truth, logical-file, control, and metric
semantics in ``instruments/replay/SPEC.md``.  It adds one external signal:
successful agent Read events, causally mapped to the first-parent tree that was
live when the Read result became available.

The transcript corpus and target repository are inputs only.  Generated Git
streams and results are written under the Blast-Radius experiment tree.
"""

from __future__ import annotations

import argparse
import bisect
import collections
import datetime as dt
import gzip
import hashlib
import importlib
import json
import math
import ntpath
import os
import platform
import random
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from typing import Any, Iterable, Sequence


HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parents[1]
REPLAY_ROOT = HERE.parent / "replay"
DEFAULT_REPOSITORY = Path(r"C:/Users/joshp/Desktop/toolsenabled-current")
DEFAULT_READ_EVENTS = (
    PROJECT_ROOT / "exploratory/unification/predictor-artifacts/read-events.jsonl.gz"
)
DEFAULT_OUTPUT = PROJECT_ROOT / "exploratory/unification/predictor-metrics.json"
DEFAULT_WORK_DIR = PROJECT_ROOT / "exploratory/unification/predictor-artifacts"

MODEL_KEYS = ("cochange", "coread", "fused", "popularity", "random")
WINDOW_SECONDS = 300
RRF_K = 60
MIN_ELIGIBLE_COMMITS = 20
MAX_PREDICTIONS = 20
DEFAULT_CORPUS_WINDOW_START = "2026-07-17T00:00:00+00:00"
DEFAULT_BOOTSTRAP_REPLICATES = 10_000
BOOTSTRAP_SEED = "blast-radius-coread-predictor-commit-bootstrap-v1"


@dataclass(frozen=True)
class CommitDates:
    author_timestamp: int
    committer_timestamp: int


@dataclass(frozen=True)
class PreparedRead:
    agent: str
    session: str
    tool_use_id: str
    call_timestamp: float
    result_timestamp: float
    availability_timestamp: float
    path_key: str
    copied_prefix_identity: bool
    file_id: int | None = None


@dataclass(frozen=True, order=True)
class PairIncidence:
    availability_timestamp: float
    left_id: int
    right_id: int
    task_ordinal: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=DEFAULT_REPOSITORY)
    parser.add_argument("--read-events", type=Path, default=DEFAULT_READ_EVENTS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--work-dir", type=Path, default=DEFAULT_WORK_DIR)
    parser.add_argument(
        "--corpus-window-start",
        default=DEFAULT_CORPUS_WINDOW_START,
        help="Externally declared transcript-corpus start (ISO-8601).",
    )
    parser.add_argument(
        "--bootstrap-replicates",
        type=int,
        default=DEFAULT_BOOTSTRAP_REPLICATES,
    )
    parser.add_argument(
        "--include-copied-prefix-events",
        action="store_true",
        help="Sensitivity only: include events whose fallback agent identity uses transcript ctime.",
    )
    return parser.parse_args()


def parse_iso_timestamp(value: str) -> float:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp must include a timezone: {value!r}")
    return parsed.timestamp()


def iso_utc(timestamp: float) -> str:
    return dt.datetime.fromtimestamp(timestamp, tz=dt.timezone.utc).isoformat()


def path_bytes(path: str) -> bytes:
    return path.encode("utf-8", errors="surrogateescape")


def git_path_key(path: str) -> str:
    return ntpath.normcase(ntpath.normpath(path.replace("/", "\\"))).replace("\\", "/")


def windows_absolute_key(path: str | os.PathLike[str]) -> str:
    return ntpath.normcase(ntpath.normpath(os.fspath(path).replace("/", "\\")))


def assert_read_stream_repository(header: dict[str, Any], repository: Path) -> None:
    recorded = header.get("target_repository")
    if not isinstance(recorded, str) or not recorded:
        raise ValueError("read-event header does not identify its target repository")
    if windows_absolute_key(recorded) != windows_absolute_key(repository.resolve()):
        raise ValueError(
            f"read-event target {recorded!r} does not match requested repository {str(repository)!r}"
        )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def run_git(repository: Path, arguments: Sequence[str], *, text: bool = True) -> str | bytes:
    result = subprocess.run(
        ("git", "-c", "core.longpaths=true", "-C", str(repository), *arguments),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=text,
        encoding="utf-8" if text else None,
        errors="surrogateescape" if text else None,
    )
    return result.stdout.strip() if text else result.stdout


def replay_modules() -> tuple[Any, Any]:
    replay_text = str(REPLAY_ROOT)
    if replay_text not in sys.path:
        sys.path.insert(0, replay_text)
    return importlib.import_module("extract"), importlib.import_module("replay")


def extract_git_stream(repository: Path, work_dir: Path) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    """Run the stock replay extractor without changing its source or target tree."""
    replay_extract, _ = replay_modules()
    stream_dir = work_dir / "git-stream"
    stream_dir.mkdir(parents=True, exist_ok=True)

    head_before = str(run_git(repository, ("rev-parse", "HEAD")))
    status_before = str(run_git(repository, ("status", "--porcelain=v1", "--untracked-files=all")))
    reachable_count = int(str(run_git(repository, ("rev-list", "--count", "HEAD"))))
    first_parent_count = int(
        str(run_git(repository, ("rev-list", "--first-parent", "--count", "HEAD")))
    )
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

    prior_clone_root = replay_extract.CLONE_ROOT
    prior_stream_root = replay_extract.STREAM_ROOT
    try:
        replay_extract.CLONE_ROOT = repository.parent
        replay_extract.STREAM_ROOT = stream_dir
        metadata = replay_extract.extract_repository(spec, corpus_record)
    finally:
        replay_extract.CLONE_ROOT = prior_clone_root
        replay_extract.STREAM_ROOT = prior_stream_root

    stream_path = stream_dir / f"{repository.name}.jsonl.gz"
    metadata_path = stream_dir / f"{repository.name}.meta.json"
    atomic_write_json(metadata_path, metadata)
    if metadata.get("source_head_sha") != head_before:
        raise AssertionError("Git extraction metadata changed source HEAD")
    if metadata.get("stream_sha256") != sha256_file(stream_path):
        raise AssertionError("Git stream hash differs from extraction metadata")

    head_after = str(run_git(repository, ("rev-parse", "HEAD")))
    status_after = str(run_git(repository, ("status", "--porcelain=v1", "--untracked-files=all")))
    if head_after != head_before:
        raise RuntimeError(f"target HEAD changed during extraction: {head_before} -> {head_after}")
    if status_after != status_before:
        raise RuntimeError("target worktree status changed during read-only extraction")

    provenance = {
        "head_sha": head_before,
        "reachable_commit_count": reachable_count,
        "first_parent_commit_count": first_parent_count,
        "worktree_dirty_path_count": len(status_before.splitlines()) if status_before else 0,
        "worktree_status_sha256": hashlib.sha256(
            status_before.encode("utf-8", errors="surrogateescape")
        ).hexdigest(),
        "stream_path": str(stream_path),
        "stream_sha256": sha256_file(stream_path),
        "extractor_metadata": metadata,
    }
    return stream_path, metadata, provenance


def load_git_stream(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    commits: list[dict[str, Any]] = []
    with gzip.open(path, "rt", encoding="utf-8", errors="surrogatepass") as handle:
        first = handle.readline()
        if not first:
            raise ValueError("empty Git stream")
        header = json.loads(first)
        if header.get("type") != "header":
            raise ValueError("Git stream is missing its header")
        for expected_index, line in enumerate(handle):
            commit = json.loads(line)
            if commit.get("type") != "commit" or int(commit.get("index", -1)) != expected_index:
                raise ValueError(f"invalid Git stream record at index {expected_index}")
            commits.append(commit)
    return header, commits


def load_commit_dates(repository: Path) -> dict[str, CommitDates]:
    output = str(
        run_git(
            repository,
            ("log", "--first-parent", "--reverse", "--root", "--format=%H%x09%at%x09%ct", "HEAD"),
        )
    )
    dates: dict[str, CommitDates] = {}
    for line in output.splitlines():
        sha, author, committer = line.split("\t")
        dates[sha] = CommitDates(int(author), int(committer))
    return dates


def load_read_events(
    path: Path,
    *,
    include_copied_prefix_events: bool,
) -> tuple[list[PreparedRead], dict[str, Any], dict[str, Any]]:
    diagnostics: collections.Counter[str] = collections.Counter()
    prepared: list[PreparedRead] = []
    seen_ids: set[str] = set()
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        first = handle.readline()
        if not first:
            raise ValueError("empty read-event stream")
        header = json.loads(first)
        if header.get("type") != "header" or header.get("schema_version") != 1:
            raise ValueError("unsupported read-event stream")
        snapshot_timestamp = parse_iso_timestamp(str(header["snapshot_utc"]))
        for line in handle:
            event = json.loads(line)
            if event.get("type") != "read":
                raise ValueError("non-read record after read-event header")
            diagnostics["input_events"] += 1
            tool_use_id = str(event["tool_use_id"])
            if tool_use_id in seen_ids:
                raise AssertionError(f"duplicate tool-use ID in compact stream: {tool_use_id}")
            seen_ids.add(tool_use_id)
            copied = bool(event.get("fallback_identity_from_copied_prefix"))
            if copied and not include_copied_prefix_events:
                diagnostics["excluded_copied_prefix_identity"] += 1
                continue
            call_value = event.get("timestamp")
            result_value = event.get("result_timestamp")
            if not isinstance(call_value, (int, float)) or not math.isfinite(float(call_value)):
                diagnostics["excluded_invalid_call_timestamp"] += 1
                continue
            if not isinstance(result_value, (int, float)) or not math.isfinite(float(result_value)):
                diagnostics["excluded_missing_or_invalid_result_timestamp"] += 1
                continue
            call_timestamp = float(call_value)
            result_timestamp = float(result_value)
            availability_timestamp = max(call_timestamp, result_timestamp)
            if call_timestamp > snapshot_timestamp or result_timestamp > snapshot_timestamp:
                diagnostics["excluded_timestamp_after_corpus_snapshot"] += 1
                continue
            if result_timestamp < call_timestamp:
                diagnostics["result_timestamp_before_call"] += 1
            prepared.append(
                PreparedRead(
                    agent=str(event["agent"]),
                    session=str(event["session"]),
                    tool_use_id=tool_use_id,
                    call_timestamp=call_timestamp,
                    result_timestamp=result_timestamp,
                    availability_timestamp=availability_timestamp,
                    path_key=git_path_key(str(event["path"])),
                    copied_prefix_identity=copied,
                )
            )
            diagnostics["usable_events"] += 1

    diagnostics["distinct_agents"] = len({event.agent for event in prepared})
    diagnostics["distinct_paths"] = len({event.path_key for event in prepared})
    diagnostics["call_before_result"] = sum(
        event.call_timestamp < event.result_timestamp for event in prepared
    )
    diagnostics["maximum_result_lag_milliseconds"] = round(
        max(
            (event.result_timestamp - event.call_timestamp) * 1000.0
            for event in prepared
        ),
        6,
    ) if prepared else None
    return prepared, header, dict(sorted(diagnostics.items()))


def map_reads_at_availability(
    events: Sequence[PreparedRead],
    git_header: dict[str, Any],
    commits: Sequence[dict[str, Any]],
    commit_dates: dict[str, CommitDates],
) -> tuple[list[PreparedRead], dict[str, Any]]:
    """Map paths only against the first-parent tree live at result availability."""
    _, replay_model = replay_modules()
    commit_times = [commit_dates[str(commit["sha"])].committer_timestamp for commit in commits]
    if any(right < left for left, right in zip(commit_times, commit_times[1:])):
        raise ValueError("committer timestamps are non-monotone; temporal path mapping is ambiguous")

    state = replay_model.ReplayState(
        git_header["initial_files"],
        max_commit_age=len(commits) + 1,
    )
    live_by_key: dict[str, set[int]] = collections.defaultdict(set)
    for path, file_id in state.path_to_id.items():
        live_by_key[git_path_key(path)].add(file_id)

    def rebuild_live_map() -> None:
        live_by_key.clear()
        for path, file_id in state.path_to_id.items():
            live_by_key[git_path_key(path)].add(file_id)

    ordered = sorted(
        events,
        key=lambda event: (
            event.availability_timestamp,
            event.call_timestamp,
            event.agent,
            event.session,
            event.tool_use_id,
        ),
    )
    mapped: list[PreparedRead] = []
    diagnostics: collections.Counter[str] = collections.Counter()
    commit_cursor = 0
    for event in ordered:
        while (
            commit_cursor < len(commits)
            and commit_times[commit_cursor] < event.availability_timestamp
        ):
            commit = commits[commit_cursor]
            state.assert_query_generation(commit_cursor)
            resolved = state.resolve_changes(commit["changes"])
            state.fold(commit_cursor, resolved)
            commit_cursor += 1
            rebuild_live_map()
        candidates = live_by_key.get(event.path_key, set())
        if len(candidates) == 1:
            file_id = next(iter(candidates))
            mapped.append(
                PreparedRead(
                    **{
                        **event.__dict__,
                        "file_id": file_id,
                    }
                )
            )
            diagnostics["mapped_events"] += 1
        elif len(candidates) > 1:
            mapped.append(event)
            diagnostics["unmapped_case_collision"] += 1
        else:
            mapped.append(event)
            diagnostics["unmapped_path_absent_from_live_tree"] += 1

    diagnostics["events_preserved_for_window_boundaries"] = len(mapped)
    diagnostics["mapped_distinct_file_ids"] = len(
        {event.file_id for event in mapped if event.file_id is not None}
    )
    diagnostics["commits_folded_while_mapping"] = commit_cursor
    return mapped, dict(sorted(diagnostics.items()))


def build_pair_incidences(
    events: Sequence[PreparedRead],
    window_seconds: int = WINDOW_SECONDS,
) -> tuple[list[PairIncidence], dict[str, Any]]:
    """Build causal pair events while preserving unmapped reads as task activity."""
    by_agent: dict[str, list[PreparedRead]] = collections.defaultdict(list)
    for event in events:
        by_agent[event.agent].append(event)

    tasks: list[dict[str, Any]] = []
    for agent in sorted(by_agent):
        ordered = sorted(
            by_agent[agent],
            key=lambda event: (
                event.call_timestamp,
                event.session,
                event.tool_use_id,
            ),
        )
        current: dict[str, Any] | None = None
        previous_call: float | None = None
        for event in ordered:
            if (
                current is None
                or previous_call is not None
                and event.call_timestamp - previous_call > window_seconds
            ):
                current = {
                    "agent": agent,
                    "start": event.call_timestamp,
                    "end": event.call_timestamp,
                    "events": 0,
                    "files": {},
                }
                tasks.append(current)
            current["end"] = event.call_timestamp
            current["events"] += 1
            if event.file_id is not None:
                current["files"].setdefault(event.file_id, event)
            previous_call = event.call_timestamp

    incidences: list[PairIncidence] = []
    informative = 0
    mapped_tasks = 0
    durations: list[float] = []
    for task_ordinal, task in enumerate(tasks):
        selected: dict[int, PreparedRead] = task["files"]
        if selected:
            mapped_tasks += 1
        if len(selected) >= 2:
            informative += 1
        durations.append(float(task["end"]) - float(task["start"]))
        for left_id, right_id in combinations(sorted(selected), 2):
            left_event = selected[left_id]
            right_event = selected[right_id]
            availability = max(
                left_event.call_timestamp,
                left_event.result_timestamp,
                right_event.call_timestamp,
                right_event.result_timestamp,
            )
            if availability < left_event.call_timestamp or availability < right_event.call_timestamp:
                raise AssertionError("pair availability precedes a component Read")
            incidences.append(
                PairIncidence(availability, left_id, right_id, task_ordinal)
            )
    incidences.sort()
    diagnostics = {
        "window_seconds": window_seconds,
        "task_count": len(tasks),
        "tasks_with_mapped_read": mapped_tasks,
        "informative_task_count": informative,
        "pair_incidence_count": len(incidences),
        "first_pair_availability_utc": iso_utc(incidences[0].availability_timestamp)
        if incidences
        else None,
        "last_pair_availability_utc": iso_utc(incidences[-1].availability_timestamp)
        if incidences
        else None,
        "median_task_duration_seconds": statistics.median(durations) if durations else None,
        "maximum_task_duration_seconds": max(durations) if durations else None,
    }
    return incidences, diagnostics


def strict_pair_prefix(
    incidences: Sequence[PairIncidence],
    timestamps: Sequence[float],
    cutoff: float,
) -> int:
    prefix = bisect.bisect_left(timestamps, cutoff)
    if prefix and not incidences[prefix - 1].availability_timestamp < cutoff:
        raise AssertionError("co-read prefix contains a pair at or after the commit cutoff")
    if prefix < len(incidences) and incidences[prefix].availability_timestamp < cutoff:
        raise AssertionError("co-read prefix omitted a pair strictly before the commit cutoff")
    return prefix


def coread_adjacency(
    incidences: Sequence[PairIncidence],
    prefix: int,
    live_ids: set[int] | frozenset[int],
) -> dict[int, dict[int, int]]:
    counts: collections.Counter[tuple[int, int]] = collections.Counter(
        (incidence.left_id, incidence.right_id) for incidence in incidences[:prefix]
    )
    adjacency: dict[int, dict[int, int]] = collections.defaultdict(dict)
    for (left, right), count in counts.items():
        if left == right:
            raise AssertionError("co-read pair contains the same logical file twice")
        if left not in live_ids or right not in live_ids:
            continue
        adjacency[left][right] = count
        adjacency[right][left] = count
    return dict(adjacency)


def ordered_scores(state: Any, scores: dict[int, float]) -> list[tuple[float, int]]:
    return sorted(
        ((score, file_id) for file_id, score in scores.items() if score > 0.0),
        key=lambda item: (-item[0], path_bytes(state.id_to_path[item[1]]), item[1]),
    )


def ranked_result(replay_model: Any, ordered: Sequence[tuple[float, int]]) -> Any:
    top1, at10, at20 = replay_model.tie_diagnostics(ordered)
    return replay_model.RankedResult(
        tuple(file_id for _, file_id in ordered[:MAX_PREDICTIONS]),
        top1,
        at10,
        at20,
    )


def coread_query(
    state: Any,
    replay_model: Any,
    seed: int,
    commit_index: int,
    adjacency: dict[int, dict[int, int]],
    *,
    latest_included_timestamp: float | None,
    cutoff: float,
) -> tuple[Any, list[tuple[float, int]]]:
    state.assert_query_generation(commit_index)
    if latest_included_timestamp is not None and not latest_included_timestamp < cutoff:
        raise AssertionError("co-read query includes a Read pair that is not strictly historical")
    scores = {
        candidate: float(count)
        for candidate, count in adjacency.get(seed, {}).items()
        if candidate in state.existing_ids and candidate != seed and count > 0
    }
    ordered = ordered_scores(state, scores)
    return ranked_result(replay_model, ordered), ordered


def cochange_query_full(
    state: Any,
    replay_model: Any,
    seed: int,
    commit_index: int,
) -> tuple[Any, list[tuple[float, int]]]:
    seed_history, candidate_histories = replay_model.collect_cochange_histories(
        state, seed, commit_index
    )
    scores = replay_model.score_cochange_histories(
        state,
        seed_history,
        candidate_histories,
        commit_index,
        decayed=True,
    )
    ordered = ordered_scores(state, scores)
    return ranked_result(replay_model, ordered), ordered


def reciprocal_rank_fusion(
    state: Any,
    replay_model: Any,
    rankings: Sequence[Sequence[tuple[float, int]]],
    *,
    rrf_k: int = RRF_K,
) -> Any:
    scores: dict[int, float] = collections.defaultdict(float)
    for ranking in rankings:
        for rank, (_, file_id) in enumerate(ranking, 1):
            scores[file_id] += 1.0 / (rrf_k + rank)
    return ranked_result(replay_model, ordered_scores(state, dict(scores)))


def empty_commit_metrics() -> dict[str, int | float]:
    return {
        "p1_hits": 0,
        "p10_hits": 0,
        "r10_sum": 0.0,
        "r20_sum": 0.0,
        "empty_queries": 0,
    }


def add_contribution(target: dict[str, int | float], contribution: dict[str, int | float]) -> None:
    for key, value in contribution.items():
        target[key] += value


def percentile(sorted_values: Sequence[float], probability: float) -> float:
    if not sorted_values:
        raise ValueError("cannot take a percentile of an empty sequence")
    position = (len(sorted_values) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(sorted_values[lower])
    fraction = position - lower
    return float(sorted_values[lower] * (1.0 - fraction) + sorted_values[upper] * fraction)


def aggregate_sample(
    commits: Sequence[dict[str, Any]],
    sample: Sequence[int],
    model: str,
) -> dict[str, float]:
    queries = sum(int(commits[index]["query_count"]) for index in sample)
    if queries <= 0:
        raise AssertionError("bootstrap sample contains no queries")
    p1_hits = sum(float(commits[index]["models"][model]["p1_hits"]) for index in sample)
    p10_hits = sum(float(commits[index]["models"][model]["p10_hits"]) for index in sample)
    r10_sum = sum(float(commits[index]["models"][model]["r10_sum"]) for index in sample)
    r20_sum = sum(float(commits[index]["models"][model]["r20_sum"]) for index in sample)
    return {
        "p_at_1": p1_hits / queries,
        "p_at_10": p10_hits / (10.0 * queries),
        "r_at_10": r10_sum / queries,
        "r_at_20": r20_sum / queries,
    }


def commit_bootstrap(
    commits: Sequence[dict[str, Any]],
    models: dict[str, dict[str, Any]],
    replicates: int,
) -> dict[str, Any]:
    if replicates <= 0:
        return {
            "performed": False,
            "reason": "bootstrap replicate count was zero",
        }
    comparisons = (
        ("coread_minus_cochange", "coread", "cochange"),
        ("coread_minus_popularity", "coread", "popularity"),
        ("coread_minus_random", "coread", "random"),
        ("cochange_minus_popularity", "cochange", "popularity"),
        ("cochange_minus_random", "cochange", "random"),
        ("fused_minus_cochange", "fused", "cochange"),
        ("fused_minus_popularity", "fused", "popularity"),
        ("fused_minus_random", "fused", "random"),
    )
    metric_names = ("p_at_1", "p_at_10", "r_at_10", "r_at_20")
    draws: dict[str, dict[str, list[float]]] = {
        name: {metric: [] for metric in metric_names}
        for name, _, _ in comparisons
    }
    seed = int.from_bytes(hashlib.sha256(BOOTSTRAP_SEED.encode("ascii")).digest(), "big")
    generator = random.Random(seed)
    commit_count = len(commits)
    for _ in range(replicates):
        sample = [generator.randrange(commit_count) for _ in range(commit_count)]
        sampled = {
            model: aggregate_sample(commits, sample, model)
            for model in MODEL_KEYS
        }
        for name, left, right in comparisons:
            for metric in metric_names:
                draws[name][metric].append(sampled[left][metric] - sampled[right][metric])

    result: dict[str, Any] = {}
    for name, left, right in comparisons:
        result[name] = {}
        for metric in metric_names:
            values = sorted(draws[name][metric])
            point_delta = float(models[left][metric]) - float(models[right][metric])
            result[name][metric] = {
                "point_delta": point_delta,
                "percentile_95_interval": [
                    percentile(values, 0.025),
                    percentile(values, 0.975),
                ],
                "bootstrap_probability_delta_above_zero": sum(value > 0 for value in values)
                / len(values),
            }
    return {
        "performed": True,
        "resampling_unit": "eligible commit; all dependent seed queries from a sampled commit move together",
        "paired_across_arms": True,
        "replicates": replicates,
        "seed": BOOTSTRAP_SEED,
        "interval": "ordinary percentile 95%; descriptive for this one time-ordered repository",
        "comparisons": result,
    }


def inside_window(timestamp: int, start: float, end: float) -> bool:
    return start <= timestamp <= end


def count_eligible_records(commit: dict[str, Any]) -> int:
    return sum(change["status"] != "A" for change in commit["changes"])


def run_replay(
    repository: Path,
    read_events_path: Path,
    work_dir: Path,
    *,
    corpus_window_start: float,
    include_copied_prefix_events: bool,
    bootstrap_replicates: int,
) -> dict[str, Any]:
    replay_extract, replay_model = replay_modules()
    del replay_extract
    started_at = dt.datetime.now(dt.timezone.utc).isoformat()
    stream_path, stream_meta, git_provenance = extract_git_stream(repository, work_dir)
    git_header, commits = load_git_stream(stream_path)
    if len(commits) != int(stream_meta["commit_count"]):
        raise AssertionError("Git stream length differs from extraction metadata")
    commit_dates = load_commit_dates(repository)
    if set(commit_dates) != {str(commit["sha"]) for commit in commits}:
        raise AssertionError("author/committer date map differs from replay first-parent stream")
    for commit in commits:
        dates = commit_dates[str(commit["sha"])]
        if int(commit["timestamp"]) != dates.committer_timestamp:
            raise AssertionError("replay timestamp is not Git committer time")

    reads, read_header, read_diagnostics = load_read_events(
        read_events_path,
        include_copied_prefix_events=include_copied_prefix_events,
    )
    assert_read_stream_repository(read_header, repository)
    corpus_window_end = parse_iso_timestamp(str(read_header["snapshot_utc"]))
    if corpus_window_start >= corpus_window_end:
        raise ValueError("corpus window start is not before its snapshot end")
    mapped_reads, mapping_diagnostics = map_reads_at_availability(
        reads, git_header, commits, commit_dates
    )
    pair_incidences, task_diagnostics = build_pair_incidences(mapped_reads)
    pair_timestamps = [incidence.availability_timestamp for incidence in pair_incidences]

    committer_times = [commit_dates[str(commit["sha"])].committer_timestamp for commit in commits]
    author_times = [commit_dates[str(commit["sha"])].author_timestamp for commit in commits]
    nonmonotone_committer_edges = sum(
        right < left for left, right in zip(committer_times, committer_times[1:])
    )
    if nonmonotone_committer_edges:
        raise AssertionError("committer timestamp order changed after temporal mapping")
    differing_dates = [
        abs(author - committer)
        for author, committer in zip(author_times, committer_times)
        if author != committer
    ]
    tied_timestamp_groups = collections.Counter(committer_times)

    in_window_commits = [
        commit
        for commit in commits
        if inside_window(
            commit_dates[str(commit["sha"])].committer_timestamp,
            corpus_window_start,
            corpus_window_end,
        )
    ]
    eligible_count = sum(count_eligible_records(commit) >= 2 for commit in in_window_commits)
    preliminary_coverage = {
        "commits_replayed": len(commits),
        "commits_inside_corpus_window": len(in_window_commits),
        "eligible_commits_inside_corpus_window": eligible_count,
        "minimum_eligible_commit_gate": MIN_ELIGIBLE_COMMITS,
    }
    if eligible_count < MIN_ELIGIBLE_COMMITS:
        return {
            "schema_version": 1,
            "measurement": "strict-temporal-coread-vs-cochange-predictor",
            "status": "insufficient_sample",
            "reason": (
                f"only {eligible_count} eligible commits fall inside the common signal window; "
                f"the predeclared minimum is {MIN_ELIGIBLE_COMMITS}; no arm metrics were computed"
            ),
            "started_at_utc": started_at,
            "completed_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "coverage": preliminary_coverage,
            "git": git_provenance,
            "read_corpus": {
                "header": read_header,
                "stream_sha256": sha256_file(read_events_path),
                "extraction_diagnostics": read_diagnostics,
                "temporal_mapping": mapping_diagnostics,
                "task_construction": task_diagnostics,
            },
        }

    state = replay_model.ReplayState(
        git_header["initial_files"],
        max_commit_age=len(commits) + 1,
    )
    accumulators = {
        model: replay_model.new_model_accumulator()
        for model in MODEL_KEYS
    }
    eligible_commits: list[dict[str, Any]] = []
    created_excluded_in_window = 0
    commits_inside_count = 0
    no_query_inside_count = 0
    temporal_pair_prefix_assertions = 0
    coread_temporal_query_assertions = 0
    prediction_universe_assertions = 0
    ground_truth_subset_assertions = 0
    coread_nonempty_queries = 0
    coread_pair_prefixes: list[int] = []

    for expected_index, commit in enumerate(commits):
        commit_index = int(commit["index"])
        if commit_index != expected_index:
            raise AssertionError("non-contiguous replay index")
        state.assert_query_generation(commit_index)
        resolved = state.resolve_changes(commit["changes"])
        precommit_existing = frozenset(state.existing_ids)
        eligible_ids = tuple(change.file_id for change in resolved if change.status != "A")
        if len(set(eligible_ids)) != len(eligible_ids):
            raise AssertionError("eligible logical files are not unique")
        if any(file_id not in precommit_existing for file_id in eligible_ids):
            raise AssertionError("ground truth contains a file absent before the commit")
        ground_truth_subset_assertions += 1

        dates = commit_dates[str(commit["sha"])]
        cutoff = float(dates.committer_timestamp)
        in_window = inside_window(cutoff, corpus_window_start, corpus_window_end)
        if in_window:
            commits_inside_count += 1
            created_excluded_in_window += sum(change.status == "A" for change in resolved)
            if len(eligible_ids) < 2:
                no_query_inside_count += 1

        if in_window and len(eligible_ids) >= 2:
            prefix = strict_pair_prefix(pair_incidences, pair_timestamps, cutoff)
            temporal_pair_prefix_assertions += 1
            coread_pair_prefixes.append(prefix)
            latest_pair_timestamp = (
                pair_incidences[prefix - 1].availability_timestamp if prefix else None
            )
            adjacency = coread_adjacency(pair_incidences, prefix, precommit_existing)
            commit_metrics = {model: empty_commit_metrics() for model in MODEL_KEYS}

            for seed in eligible_ids:
                state.assert_query_generation(commit_index)
                targets = set(eligible_ids)
                targets.remove(seed)
                if seed not in precommit_existing or not targets.issubset(precommit_existing):
                    raise AssertionError("query or target is outside the pre-commit universe")

                cochange_start = time.perf_counter_ns()
                cochange_ranked, cochange_full = cochange_query_full(
                    state, replay_model, seed, commit_index
                )
                cochange_elapsed = time.perf_counter_ns() - cochange_start

                coread_start = time.perf_counter_ns()
                coread_ranked, coread_full = coread_query(
                    state,
                    replay_model,
                    seed,
                    commit_index,
                    adjacency,
                    latest_included_timestamp=latest_pair_timestamp,
                    cutoff=cutoff,
                )
                coread_elapsed = time.perf_counter_ns() - coread_start
                coread_temporal_query_assertions += 1
                coread_nonempty_queries += bool(coread_ranked.ids)

                fused_start = time.perf_counter_ns()
                fused_ranked = reciprocal_rank_fusion(
                    state,
                    replay_model,
                    (cochange_full, coread_full),
                )
                fused_elapsed = (
                    cochange_elapsed
                    + coread_elapsed
                    + time.perf_counter_ns()
                    - fused_start
                )

                popularity_start = time.perf_counter_ns()
                popularity_ranked = replay_model.popularity_query(state, seed, commit_index)
                popularity_elapsed = time.perf_counter_ns() - popularity_start

                random_start = time.perf_counter_ns()
                random_ranked = replay_model.random_query(
                    state, seed, commit_index, repository.name
                )
                random_elapsed = time.perf_counter_ns() - random_start

                ranked_by_model = {
                    "cochange": (cochange_ranked, cochange_elapsed),
                    "coread": (coread_ranked, coread_elapsed),
                    "fused": (fused_ranked, fused_elapsed),
                    "popularity": (popularity_ranked, popularity_elapsed),
                    "random": (random_ranked, random_elapsed),
                }
                for model, (ranked, elapsed) in ranked_by_model.items():
                    replay_model.assert_predictions(state, seed, ranked)
                    prediction_universe_assertions += 1
                    contribution = replay_model.score_prediction(
                        accumulators[model], ranked, targets, elapsed
                    )
                    add_contribution(commit_metrics[model], contribution)

            eligible_commits.append(
                {
                    "index": commit_index,
                    "sha": commit["sha"],
                    "author_timestamp": dates.author_timestamp,
                    "committer_timestamp": dates.committer_timestamp,
                    "coread_pair_incidence_prefix": prefix,
                    "eligible_file_count": len(eligible_ids),
                    "query_count": len(eligible_ids),
                    "ground_truth_size_per_query": len(eligible_ids) - 1,
                    "created_file_count_excluded": sum(
                        change.status == "A" for change in resolved
                    ),
                    "models": commit_metrics,
                }
            )

        if state.existing_ids != set(precommit_existing):
            raise AssertionError("a query mutated the pre-commit universe")
        state.fold(commit_index, resolved)

    expected_head_paths = replay_model.read_head_paths(repository)
    if set(state.path_to_id) != expected_head_paths:
        raise AssertionError("final replay tree differs from target HEAD")
    if len(eligible_commits) != eligible_count:
        raise AssertionError("preflight eligible-commit count differs from replay")
    if commits_inside_count != len(in_window_commits):
        raise AssertionError("preflight in-window count differs from replay")

    query_counts = sorted(
        (int(commit["query_count"]) for commit in eligible_commits), reverse=True
    )
    total_queries = sum(query_counts)
    ground_truth_total = sum(
        int(commit["query_count"]) * int(commit["ground_truth_size_per_query"])
        for commit in eligible_commits
    )
    models = {
        model: replay_model.finalize_model(accumulators[model])
        for model in MODEL_KEYS
    }
    for control in ("popularity", "random"):
        if total_queries and models[control]["empty_queries"]:
            raise AssertionError(f"mandatory control {control} returned an empty radius")

    uncertainty = commit_bootstrap(
        eligible_commits,
        models,
        bootstrap_replicates,
    )
    first_target_call = min((event.call_timestamp for event in reads), default=None)
    first_target_availability = min(
        (event.availability_timestamp for event in reads), default=None
    )
    first_pair_availability = (
        pair_incidences[0].availability_timestamp if pair_incidences else None
    )
    eligible_before_first_target_read = sum(
        int(commit["query_count"]) > 0
        and first_target_availability is not None
        and float(commit["committer_timestamp"]) < first_target_availability
        for commit in eligible_commits
    )

    script_hash = sha256_file(Path(__file__).resolve())
    return {
        "schema_version": 1,
        "measurement": "strict-temporal-coread-vs-cochange-predictor",
        "status": "ok",
        "started_at_utc": started_at,
        "completed_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "repository": str(repository),
        "source_head_sha": git_provenance["head_sha"],
        "protocol": {
            "base_spec": "instruments/replay/SPEC.md unchanged for queries, ground truth, logical identities, controls, and metrics",
            "history_order": "first-parent graph order; query immutable state at i, then fold i once",
            "commit_cutoff": "Git committer timestamp (%ct), matching the stock replay stream",
            "read_availability": "max(Read tool-use timestamp, successful tool-result timestamp)",
            "temporal_rule": "pair availability must be strictly less than commit cutoff; equality is excluded",
            "read_identity": "path mapped only to the logical file live at result availability; no future aliases; unmapped reads remain task-boundary events",
            "task_window": "same effective agent; split only when consecutive Read-call gap is >300 seconds; file deduplicated within task",
            "coread_score": "raw prior task-pair incidence count; same per-seed rank as forward confidence; positive support only",
            "cochange_score": "stock replay time-decayed confidence; 150-commit half-life",
            "fusion": "RRF over both complete positive-support rankings; score=sum(1/(60+rank)); missing-arm contribution zero",
            "candidate_universe": "logical files existing immediately before query commit, seed excluded",
            "ground_truth": "other pre-commit touched logical files; additions excluded; deletions and renames included",
            "precision_denominator": "fixed K; short rankings count missing positions as misses",
            "corpus_window_start": iso_utc(corpus_window_start),
            "corpus_window_start_source": "user-supplied task boundary; the compact read header records only the exact snapshot endpoint",
            "corpus_window_end": iso_utc(corpus_window_end),
            "copied_prefix_events_included": include_copied_prefix_events,
            "primary_win_rule": "co-read beats co-change only if its point estimate is higher on both P@1 and P@10; a split is mixed",
        },
        "coverage": {
            **preliminary_coverage,
            "commits_before_window": sum(timestamp < corpus_window_start for timestamp in committer_times),
            "commits_after_window": sum(timestamp > corpus_window_end for timestamp in committer_times),
            "inside_window_no_query_commit_count": no_query_inside_count,
            "query_count": total_queries,
            "query_weighted_mean_ground_truth_size": ground_truth_total / total_queries,
            "median_commit_ground_truth_size": statistics.median(
                commit["ground_truth_size_per_query"] for commit in eligible_commits
            ),
            "largest_commit_ground_truth_size": max(
                commit["ground_truth_size_per_query"] for commit in eligible_commits
            ),
            "largest_query_commit_queries": query_counts[0],
            "largest_query_commit_share": query_counts[0] / total_queries,
            "top_five_query_commit_share": sum(query_counts[:5]) / total_queries,
            "created_files_excluded_inside_window": created_excluded_in_window,
            "first_target_read_call_utc": iso_utc(first_target_call)
            if first_target_call is not None
            else None,
            "first_target_read_availability_utc": iso_utc(first_target_availability)
            if first_target_availability is not None
            else None,
            "first_coread_pair_availability_utc": iso_utc(first_pair_availability)
            if first_pair_availability is not None
            else None,
            "eligible_commits_before_first_target_read_availability": eligible_before_first_target_read,
            "coread_nonempty_query_count": coread_nonempty_queries,
            "coread_nonempty_query_rate": coread_nonempty_queries / total_queries,
            "minimum_coread_pair_prefix": min(coread_pair_prefixes),
            "maximum_coread_pair_prefix": max(coread_pair_prefixes),
        },
        "date_diagnostics": {
            "author_committer_timestamp_difference_count": len(differing_dates),
            "maximum_author_committer_absolute_difference_seconds": max(differing_dates)
            if differing_dates
            else 0,
            "nonmonotone_committer_edges": nonmonotone_committer_edges,
            "equal_committer_timestamp_group_count": sum(
                count > 1 for count in tied_timestamp_groups.values()
            ),
            "commits_in_equal_timestamp_groups": sum(
                count for count in tied_timestamp_groups.values() if count > 1
            ),
            "maximum_equal_timestamp_group_size": max(tied_timestamp_groups.values()),
        },
        "models": models,
        "uncertainty": uncertainty,
        "eligible_commits": eligible_commits,
        "git": git_provenance,
        "read_corpus": {
            "header": read_header,
            "stream_path": str(read_events_path),
            "stream_sha256": sha256_file(read_events_path),
            "extraction_diagnostics": read_diagnostics,
            "temporal_mapping": mapping_diagnostics,
            "task_construction": task_diagnostics,
        },
        "invariants": {
            "git_stream_timestamp_equals_committer_date": True,
            "committer_dates_monotone_for_temporal_path_mapping": True,
            "strict_pair_prefix_assertions": temporal_pair_prefix_assertions,
            "coread_strict_temporal_query_assertions": coread_temporal_query_assertions,
            "cochange_history_before_commit_asserted_by_stock_replay": True,
            "prediction_precommit_universe_assertions": prediction_universe_assertions,
            "ground_truth_precommit_subset_assertions": ground_truth_subset_assertions,
            "all_arms_same_query_count": len({models[model]["queries"] for model in MODEL_KEYS}) == 1,
            "final_tree_matches_head": True,
            "target_head_and_worktree_status_unchanged_during_extraction": True,
        },
        "implementation": {
            "script_sha256": script_hash,
            "python": platform.python_version(),
            "platform": platform.platform(),
            "model_query_order": list(MODEL_KEYS),
            "maximum_predictions": MAX_PREDICTIONS,
            "rrf_k": RRF_K,
            "timing_clock": "perf_counter_ns around ranked-list production; fused includes both source rankings plus fusion",
        },
    }


def main() -> int:
    args = parse_args()
    repository = args.repository.resolve()
    read_events = args.read_events.resolve()
    work_dir = args.work_dir.resolve()
    output = args.output.resolve()
    if not (repository / ".git").exists():
        raise SystemExit(f"target is not a Git worktree: {repository}")
    if not read_events.is_file():
        raise SystemExit(f"read-event stream does not exist: {read_events}")
    if args.bootstrap_replicates < 0:
        raise SystemExit("--bootstrap-replicates cannot be negative")

    result = run_replay(
        repository,
        read_events,
        work_dir,
        corpus_window_start=parse_iso_timestamp(args.corpus_window_start),
        include_copied_prefix_events=args.include_copied_prefix_events,
        bootstrap_replicates=args.bootstrap_replicates,
    )
    atomic_write_json(output, result)
    if result["status"] == "ok":
        print(
            f"wrote {output}; commits={result['coverage']['eligible_commits_inside_corpus_window']}; "
            f"queries={result['coverage']['query_count']}",
            flush=True,
        )
    else:
        print(f"wrote {output}; {result['status']}: {result['reason']}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
