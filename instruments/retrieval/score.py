"""Run the retrieval benchmark, aggregate metrics, and write RESULTS.md.

All retrievers use one clean HEAD worktree.  Transcript records do not contain
commit SHAs, so using one disclosed snapshot for every arm preserves the
head-to-head comparison while making the historical-state limitation explicit.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import math
import ntpath
import os
from pathlib import Path
import random
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from typing import Any, Iterable, Iterator, Sequence

import arms
import index


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_EVAL_DIR = ROOT / "exploratory" / "retrieval"
DEFAULT_REPO = Path(r"C:/Users/USER/Desktop/toolsenabled-current")
WINDOWS = (60, 300, 900)  # authoritative SPEC.md values
KS = (1, 5, 10, 20)
PRIMARY_ARMS = ("ripgrep", "bm25", "ident_first", "bm25_pathboost")
ALL_ARMS = (*PRIMARY_ARMS, "bm25_legacy")
QUERY_ORDER_SEED = 20260823  # @derived: benchmark run date, not label-selected


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--eval-dir", type=Path, default=DEFAULT_EVAL_DIR)
    parser.add_argument("--repo", type=Path, default=DEFAULT_REPO)
    parser.add_argument("--db", type=Path, default=DEFAULT_EVAL_DIR / "index.sqlite")
    parser.add_argument("--runs", type=Path, default=DEFAULT_EVAL_DIR / "runs.jsonl")
    parser.add_argument("--metrics", type=Path, default=DEFAULT_EVAL_DIR / "metrics.json")
    parser.add_argument("--report", type=Path, default=DEFAULT_EVAL_DIR / "RESULTS.md")
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--fresh", action="store_true", help="ignore a matching partial/complete run")
    parser.add_argument("--max-queries", type=int, help="debug-only cap; never use for a final report")
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while piece := handle.read(1 << 20):
            digest.update(piece)
    return digest.hexdigest()


def run(command: Sequence[str], *, cwd: Path | None = None, timeout: int = 120) -> str:
    completed = subprocess.run(
        list(command),
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"{' '.join(command)} failed ({completed.returncode}): {detail}")
    return completed.stdout.strip()


def warm_file(path: Path) -> dict[str, Any]:
    started = time.perf_counter()
    byte_count = 0
    with path.open("rb") as handle:
        while piece := handle.read(1 << 20):
            byte_count += len(piece)
    return {"files": 1, "bytes": byte_count, "errors": 0, "elapsed_seconds": time.perf_counter() - started}


def warm_snapshot(repo: Path) -> dict[str, Any]:
    """Read the fixed tracked snapshot once before steady-state timings.

    A temporary worktree otherwise gives ripgrep an artificial all-cold file
    cache while SQLite has already been read during validation.  This warmup is
    query- and label-independent and is excluded from per-query latency.
    """

    root = repo.resolve(strict=True)
    listed = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-c", "-z"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=120,
        check=False,
    )
    if listed.returncode:
        detail = listed.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"snapshot warmup file listing failed: {detail or listed.returncode}")
    started = time.perf_counter()
    files = 0
    byte_count = 0
    errors = 0
    for raw_relative in listed.stdout.split(b"\0"):
        if not raw_relative:
            continue
        relative = os.fsdecode(raw_relative).replace("\\", "/")
        candidate = root.joinpath(*relative.split("/"))
        try:
            resolved = candidate.resolve(strict=True)
            if os.path.commonpath((str(resolved), str(root))) != str(root) or not resolved.is_file():
                errors += 1
                continue
            with resolved.open("rb") as handle:
                while piece := handle.read(1 << 20):
                    byte_count += len(piece)
            files += 1
        except (OSError, ValueError):
            errors += 1
    return {
        "files": files,
        "bytes": byte_count,
        "errors": errors,
        "elapsed_seconds": time.perf_counter() - started,
    }


def warm_ripgrep(repo: Path) -> dict[str, Any]:
    """Pay the host's one-time executable/file scan outside timed queries."""

    sentinel = "codex_retrieval_cache_warmup_7f3ca18e9b624cd88799e0306ce45a71"
    argv = arms.ripgrep_argv(
        {"pattern": sentinel, "output_mode": "files_with_matches"},
        str(repo.resolve(strict=True)),
    )
    started = time.perf_counter()
    completed = subprocess.run(
        argv,
        cwd=str(repo),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=300,
        check=False,
        shell=False,
    )
    elapsed = time.perf_counter() - started
    if completed.returncode not in (0, 1):
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"ripgrep cache warmup failed ({completed.returncode}): {detail}")
    return {
        "elapsed_seconds": elapsed,
        "exit_code": completed.returncode,
        "stdout_bytes": len(completed.stdout),
        "stderr_bytes": len(completed.stderr),
        "query_and_label_independent": True,
    }


@contextlib.contextmanager
def clean_worktree(repo: Path, commit: str, container_parent: Path) -> Iterator[Path]:
    """Materialize one clean, temporary Git worktree and remove only that path."""
    repo = repo.resolve(strict=True)
    container_parent.mkdir(parents=True, exist_ok=True)
    parent = container_parent.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix=".retrieval-head-", dir=parent) as temporary:
        container = Path(temporary).resolve()
        worktree = container / "repo"
        run(("git", "-C", str(repo), "worktree", "add", "--detach", str(worktree), commit), timeout=300)
        try:
            yield worktree
        finally:
            try:
                run(("git", "-C", str(repo), "worktree", "remove", "--force", str(worktree)), timeout=300)
            finally:
                # TemporaryDirectory owns this exact resolved container.  This
                # guard prevents a malformed worktree path from broad deletion.
                if worktree.exists() and os.path.commonpath((str(worktree.resolve()), str(container))) == str(container):
                    shutil.rmtree(worktree)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"{path}:{number}: {error}") from error
            if isinstance(value, dict):
                rows.append(value)
    return rows


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(path.suffix + ".tmp")
    partial.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(partial, path)


def stable_index_digest(db_path: Path) -> str:
    """Hash result-affecting index rows, excluding volatile build metadata."""

    digest = hashlib.sha256()
    connection = index.connect_index(db_path)
    try:
        for row in connection.execute(
            "SELECT path, size_bytes, content_sha256, chunk_count FROM files ORDER BY path COLLATE BINARY"
        ):
            digest.update("\0".join(str(value) for value in row).encode("utf-8") + b"\n")
        for row in connection.execute(
            "SELECT region_id FROM chunks ORDER BY path COLLATE BINARY, start_byte, end_byte, content_sha256"
        ):
            digest.update(str(row[0]).encode("ascii") + b"\n")
    finally:
        connection.close()
    return digest.hexdigest()


def fingerprint(
    commit: str,
    eval_dir: Path,
    repo: Path,
    db_path: Path,
    index_metadata: dict[str, Any],
) -> str:
    digest = hashlib.sha256()
    digest.update(commit.encode("ascii") + b"\0")
    digest.update(os.path.normcase(str(repo.resolve())).encode("utf-8") + b"\0")
    for name in ("evalset_60.jsonl", "evalset.jsonl", "evalset_900.jsonl", "retention.json"):
        path = eval_dir / name
        digest.update(name.encode("ascii") + b"\0" + sha256_file(path).encode("ascii") + b"\0")
    for name in ("index.py", "arms.py", "score.py"):
        content = (Path(__file__).resolve().parent / name).read_bytes()
        digest.update(name.encode("ascii") + b"\0" + content + b"\0")
    provenance_keys = (
        "schema_version", "index_implementation_sha256", "tokenizer_version", "legacy_tokenizer_version",
        "git_head", "logical_root", "region_identity", "chunk_bytes",
        "chunk_overlap_bytes", "snap_tolerance_bytes", "max_file_bytes",
        "max_line_bytes", "bm25_path_weights", "files", "chunks",
        "identifier_postings",
    )
    provenance = {key: index_metadata.get(key) for key in provenance_keys}
    digest.update(json.dumps(provenance, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\0")
    digest.update(stable_index_digest(db_path).encode("ascii"))
    return digest.hexdigest()


def validate_index(index_metadata: dict[str, Any], commit: str, logical_root: Path) -> None:
    """Refuse a stale/wrong index before any resumable measurements are used."""

    expected = {
        "build_complete": "1",
        "git_head": commit,
        "schema_version": index.SCHEMA_VERSION,
        "index_implementation_sha256": index.INDEX_IMPLEMENTATION_SHA256,
        "tokenizer_version": index.TOKENIZER_VERSION,
        "legacy_tokenizer_version": index.LEGACY_TOKENIZER_VERSION,
        "region_identity": "path+startByte+endByte+sha256",
        "chunk_bytes": str(index.CHUNK_BYTES),
        "chunk_overlap_bytes": str(index.CHUNK_OVERLAP_BYTES),
        "snap_tolerance_bytes": str(index.SNAP_TOLERANCE_BYTES),
        "max_file_bytes": str(index.MAX_FILE_BYTES),
        "max_line_bytes": str(index.MAX_LINE_BYTES),
    }
    mismatches = [
        f"{key}={index_metadata.get(key)!r} (expected {value!r})"
        for key, value in expected.items()
        if str(index_metadata.get(key)) != str(value)
    ]
    indexed_root = os.path.normcase(os.path.normpath(str(index_metadata.get("logical_root", ""))))
    wanted_root = os.path.normcase(os.path.normpath(str(logical_root.resolve())))
    if indexed_root != wanted_root:
        mismatches.append(f"logical_root={indexed_root!r} (expected {wanted_root!r})")
    if index_metadata.get("bm25_path_weights") != list(index.PATH_BM25_WEIGHTS):
        mismatches.append(
            f"bm25_path_weights={index_metadata.get('bm25_path_weights')!r} "
            f"(expected {list(index.PATH_BM25_WEIGHTS)!r})"
        )
    if mismatches:
        raise RuntimeError("index provenance mismatch: " + "; ".join(mismatches))


def normalise_windows_path(path: str) -> str:
    return ntpath.normcase(ntpath.normpath(path))


def inside_repo(path: str, logical_root: str) -> bool:
    candidate = normalise_windows_path(path)
    root = normalise_windows_path(logical_root)
    return candidate == root or candidate.startswith(root + "\\")


def read_existing_runs(path: Path, expected_fingerprint: str) -> dict[tuple[str, str], dict[str, Any]]:
    if not path.exists():
        return {}
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    # A process kill can tear only the line being appended.
                    # Recover a valid prefix only when the malformed line is
                    # the final non-empty content; corruption in the middle
                    # invalidates the run.
                    if handle.read().strip():
                        return {}
                    break
                if not isinstance(row, dict):
                    return {}
                if row.get("fingerprint") != expected_fingerprint:
                    return {}
                key = (str(row.get("record_id")), str(row.get("arm")))
                rows[key] = row
    except (OSError, UnicodeError):
        return {}
    return rows


def serialise_arm_result(
    record_id: str,
    arm_name: str,
    result: dict[str, Any],
    latency_ms: float,
    run_fingerprint: str,
) -> dict[str, Any]:
    payload = result.pop("response", result.pop("payload", b""))
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    if not isinstance(payload, (bytes, bytearray)):
        payload = b""
    response_bytes = int(result.get("response_bytes", len(payload)))
    response_hash = result.get("response_sha256")
    if not response_hash:
        response_hash = hashlib.sha256(bytes(payload)).hexdigest()
    ranked = [str(path) for path in result.get("ranked_paths", [])]
    return {
        "record_id": record_id,
        "arm": arm_name,
        "fingerprint": run_fingerprint,
        "ranked_paths": ranked,
        "returned_paths": len(ranked),
        "response_bytes": response_bytes,
        "response_sha256": response_hash,
        "latency_ms": latency_ms,
        "error": result.get("error"),
        "diagnostic": result.get("diagnostic"),
    }


def run_queries(
    records: list[dict[str, Any]],
    conn: Any,
    source_root: Path,
    logical_root: Path,
    runs_path: Path,
    run_fingerprint: str,
    fresh: bool,
    max_queries: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    scope_exclusions: dict[str, int] = {}
    for record in records:
        scope = arms.scope_for_record(record, source_root=source_root, logical_root=logical_root)
        if scope.get("in_scope"):
            selected.append(record)
        else:
            reason = str(scope.get("reason") or "out_of_scope")
            scope_exclusions[reason] = scope_exclusions.get(reason, 0) + 1

    selected.sort(key=lambda row: row["id"])
    random.Random(QUERY_ORDER_SEED).shuffle(selected)
    if max_queries is not None:
        selected = selected[: max(0, max_queries)]
    partial_path = runs_path.with_suffix(runs_path.suffix + ".partial")
    existing: dict[tuple[str, str], dict[str, Any]] = {}
    if not fresh:
        existing = read_existing_runs(partial_path, run_fingerprint)
        if not existing:
            existing = read_existing_runs(runs_path, run_fingerprint)
    expected = {(record["id"], arm_name) for record in selected for arm_name in ALL_ARMS}
    if expected and expected.issubset(existing):
        return [existing[key] for key in sorted(expected)], {
            "selected_queries": len(selected), "scope_exclusions": scope_exclusions, "resumed_pairs": len(expected)
        }

    partial_path.parent.mkdir(parents=True, exist_ok=True)
    reusable = {key: value for key, value in existing.items() if key in expected}
    # Seed a new partial atomically, then append.  A crash while copying rows
    # cannot truncate the previous durable partial/final run.
    seed_path = partial_path.with_suffix(partial_path.suffix + ".seed")
    with seed_path.open("w", encoding="utf-8", newline="\n") as seed:
        for key in sorted(reusable):
            seed.write(json.dumps(reusable[key], ensure_ascii=False, separators=(",", ":")) + "\n")
        seed.flush()
        os.fsync(seed.fileno())
    os.replace(seed_path, partial_path)
    with partial_path.open("a", encoding="utf-8", newline="\n") as handle:
        completed = dict(reusable)
        for query_number, record in enumerate(selected, 1):
            # Rotate arm order to balance warm-cache/order effects without
            # changing any result-producing parameter.
            rotation = (query_number - 1) % len(ALL_ARMS)
            arm_order = ALL_ARMS[rotation:] + ALL_ARMS[:rotation]
            for arm_name in arm_order:
                key = (record["id"], arm_name)
                if key in completed:
                    continue
                started = time.perf_counter_ns()
                try:
                    result = arms.run_arm(
                        arm_name,
                        record,
                        conn=conn,
                        source_root=source_root,
                        logical_root=logical_root,
                        top_k=max(KS),
                    )
                except Exception as error:  # preserve a measured failure row
                    message = f"{type(error).__name__}: {error}"
                    result = {
                        "ranked_paths": [],
                        "payload": f"[error] {message}\n".encode("utf-8"),
                        "error": message,
                    }
                elapsed = (time.perf_counter_ns() - started) / 1_000_000
                row = serialise_arm_result(record["id"], arm_name, result, elapsed, run_fingerprint)
                completed[key] = row
                handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
                if len(completed) % 20 == 0:
                    handle.flush()
            # Five completed arms per query is the incremental durability unit.
            handle.flush()
            if query_number % 100 == 0 or query_number == len(selected):
                handle.flush()
                print(
                    f"scored {query_number}/{len(selected)} queries; {len(completed)}/{len(expected)} arm rows",
                    file=sys.stderr,
                    flush=True,
                )

    if expected - completed.keys():
        raise RuntimeError(f"run ended with {len(expected - completed.keys())} missing arm rows")
    os.replace(partial_path, runs_path)
    return [completed[key] for key in sorted(expected)], {
        "selected_queries": len(selected),
        "scope_exclusions": scope_exclusions,
        "resumed_pairs": len(reusable),
    }


def percentile_nearest_rank(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]


def truth_for_record(record: dict[str, Any], logical_root: str) -> tuple[list[str], int]:
    inside: list[str] = []
    seen: set[str] = set()
    outside = 0
    for raw in record.get("followed_by_read", []):
        path = normalise_windows_path(str(raw))
        if inside_repo(path, logical_root):
            if path not in seen:
                seen.add(path)
                inside.append(path)
        else:
            outside += 1
    return inside, outside


def aggregate_arm(
    records: list[dict[str, Any]],
    arm_rows: dict[str, dict[str, Any]],
    logical_root: str,
) -> dict[str, Any]:
    recall_values = {k: [] for k in KS}
    precision_values = {k: [] for k in KS}
    failures = 0
    positive_queries = 0
    behavioral_failure_queries = 0
    quality_queries = 0
    bytes_values: list[int] = []
    latencies: list[float] = []
    errors = 0
    for record in records:
        row = arm_rows.get(record["id"])
        if row is None:
            continue
        ranking = [normalise_windows_path(path) for path in row.get("ranked_paths", [])]
        truth, _ = truth_for_record(record, logical_root)
        truth_set = set(truth)
        behavioral_failure = bool(record.get("followed_by_grep"))
        if truth_set:
            positive_queries += 1
            for k in KS:
                relevant = len(set(ranking[:k]) & truth_set)
                recall_values[k].append(relevant / len(truth_set))
        if behavioral_failure:
            behavioral_failure_queries += 1
        # Read-positive records whose only labels are outside this repository
        # are not IR-scorable here.  True next-Grep failures remain in the
        # denominator so the benchmark does not retain only searches that
        # already worked.
        if truth_set or behavioral_failure:
            quality_queries += 1
            for k in KS:
                relevant = len(set(ranking[:k]) & truth_set)
                # A retained next-Grep failure has no positive read label, so
                # its precision is zero. Recall is undefined for that record.
                precision_values[k].append(relevant / k)
            if behavioral_failure or not set(ranking[: max(KS)]) & truth_set:
                failures += 1
        bytes_values.append(int(row.get("response_bytes", 0)))
        latencies.append(float(row.get("latency_ms", 0.0)))
        if row.get("error"):
            errors += 1
    query_count = len(bytes_values)
    unavailable = bool(query_count) and errors == query_count
    result = {
        "queries": query_count,
        "quality_queries": quality_queries,
        "positive_queries": positive_queries,
        "behavioral_failure_queries": behavioral_failure_queries,
        **{f"recall@{k}": statistics.fmean(recall_values[k]) if recall_values[k] else None for k in KS},
        **{f"precision@{k}": statistics.fmean(precision_values[k]) if precision_values[k] else None for k in KS},
        "failure@20": failures / quality_queries if quality_queries else None,
        "response_bytes_total": sum(bytes_values),
        "response_bytes_mean": statistics.fmean(bytes_values) if bytes_values else None,
        "estimated_tokens_mean": statistics.fmean(bytes_values) / 4 if bytes_values else None,
        "execution_error_rate": errors / query_count if query_count else None,
        "latency_median_ms": statistics.median(latencies) if latencies else None,
        "latency_p95_ms": percentile_nearest_rank(latencies, 0.95),
        "available": not unavailable,
    }
    if unavailable:
        for key in (
            *(f"recall@{k}" for k in KS), *(f"precision@{k}" for k in KS),
            "failure@20", "response_bytes_total", "response_bytes_mean",
            "estimated_tokens_mean", "latency_median_ms", "latency_p95_ms",
        ):
            result[key] = None
    return result


def aggregate_metrics(
    eval_dir: Path,
    run_rows: list[dict[str, Any]],
    logical_root: Path,
) -> dict[str, Any]:
    rows_by_arm: dict[str, dict[str, dict[str, Any]]] = {arm_name: {} for arm_name in ALL_ARMS}
    selected_ids: set[str] = set()
    for row in run_rows:
        arm_name = row["arm"]
        rows_by_arm.setdefault(arm_name, {})[row["record_id"]] = row
        selected_ids.add(row["record_id"])

    result: dict[str, Any] = {"windows": {}}
    for window in WINDOWS:
        filename = "evalset.jsonl" if window == 300 else f"evalset_{window}.jsonl"
        records = [row for row in load_jsonl(eval_dir / filename) if row["id"] in selected_ids]
        raw_positive = 0
        scorable = 0
        outside_labels = 0
        emptied = 0
        inside_labels = 0
        for record in records:
            truth, outside = truth_for_record(record, str(logical_root))
            if record.get("followed_by_read"):
                raw_positive += 1
            if truth:
                scorable += 1
                inside_labels += len(truth)
            elif record.get("followed_by_read"):
                emptied += 1
            outside_labels += outside
        result["windows"][str(window)] = {
            "population": {
                "in_scope_resolvable_queries": len(records),
                "raw_positive_queries": raw_positive,
                "scorable_positive_queries": scorable,
                "positive_queries_emptied_by_repo_filter": emptied,
                "outside_read_labels_removed": outside_labels,
                "inside_unique_read_labels": inside_labels,
                "behavioral_next_grep_failures": sum(bool(row.get("followed_by_grep")) for row in records),
            },
            "arms": {
                arm_name: aggregate_arm(records, rows_by_arm[arm_name], str(logical_root))
                for arm_name in ALL_ARMS
            },
        }
    return result


def pct(value: float | None) -> str:
    return "—" if value is None else f"{100 * value:.2f}%"


def num(value: float | int | None, digits: int = 1) -> str:
    if value is None:
        return "—"
    return f"{value:,.{digits}f}"


def metric_table(arms_data: dict[str, Any], include_legacy: bool = False) -> str:
    names = ALL_ARMS if include_legacy else PRIMARY_ARMS
    header = (
        "| Arm | R@1 | R@5 | R@10 | R@20 | P@1 | P@5 | P@10 | P@20 | "
        "Mean bytes | Est. tokens | Failure@20 | Error rate | Median ms | p95 ms |\n"
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"
    )
    lines = [header]
    for name in names:
        item = arms_data.get(name, {})
        values = [
            f"`{name}`", *(pct(item.get(f"recall@{k}")) for k in KS),
            *(pct(item.get(f"precision@{k}")) for k in KS),
            num(item.get("response_bytes_mean")), num(item.get("estimated_tokens_mean")),
            pct(item.get("failure@20")), pct(item.get("execution_error_rate")),
            num(item.get("latency_median_ms"), 2), num(item.get("latency_p95_ms"), 2),
        ]
        lines.append("| " + " | ".join(values) + " |")
    return "\n".join(lines)


def simultaneous_winners(arms_data: dict[str, Any]) -> list[str]:
    control = arms_data.get("ripgrep", {})
    control_recall = control.get("recall@20")
    control_bytes = control.get("response_bytes_mean")
    if control_recall is None or control_bytes is None:
        return []
    return [
        name
        for name in PRIMARY_ARMS[1:]
        if arms_data.get(name, {}).get("recall@20") is not None
        and arms_data[name].get("response_bytes_mean") is not None
        and arms_data[name]["recall@20"] > control_recall
        and arms_data[name]["response_bytes_mean"] < control_bytes
    ]


def head_to_head_verdict(arms_data: dict[str, Any]) -> str:
    control = arms_data.get("ripgrep", {})
    if control.get("recall@20") is None or control.get("response_bytes_mean") is None:
        return "The head-to-head could not be run because the ripgrep control was unavailable."
    winners = simultaneous_winners(arms_data)
    if winners:
        return (
            "Yes: " + ", ".join(f"`{name}`" for name in winners)
            + " beat ripgrep on recall@20 and mean response bytes."
        )
    return "No. Nothing beats ripgrep on both recall@20 and mean response size simultaneously."


def ablation_result(arms_data: dict[str, Any]) -> tuple[float | None, bool | None]:
    aware_value = arms_data.get("bm25", {}).get("recall@20")
    legacy_value = arms_data.get("bm25_legacy", {}).get("recall@20")
    alternatives = [arms_data.get(name, {}).get("recall@20") for name in ("ident_first", "bm25_pathboost")]
    if aware_value is None or legacy_value is None or any(value is None for value in alternatives):
        return None, None
    delta = aware_value - legacy_value
    alternative_deltas = [value - aware_value for value in alternatives]
    return delta, delta > 0 and delta > max([0.0, *alternative_deltas])


def report_markdown(metrics: dict[str, Any], retention: dict[str, Any], metadata: dict[str, Any]) -> str:
    primary = metrics["windows"]["300"]
    p_arms = primary["arms"]
    r = retention["retention"]["300"]
    diag = retention["diagnostics"]
    population = primary["population"]
    verdict = head_to_head_verdict(p_arms)

    aware = p_arms.get("bm25", {})
    legacy = p_arms.get("bm25_legacy", {})
    camel_delta, largest = ablation_result(p_arms)
    if largest is None:
        ablation_verdict = "The prediction could not be tested because at least one required arm was unavailable."
    elif largest:
        ablation_verdict = "The prediction held on recall@20: camel/separator splitting was the largest positive tested lever."
    else:
        ablation_verdict = "The prediction did not hold on recall@20: camel/separator splitting was not the largest positive tested lever."

    sensitivity_sections = []
    for window in WINDOWS:
        block = metrics["windows"][str(window)]
        window_retention = retention["retention"][str(window)]
        window_verdict = head_to_head_verdict(block["arms"])
        window_delta, _ = ablation_result(block["arms"])
        delta_text = "unavailable" if window_delta is None else f"{window_delta * 100:+.2f} pp"
        sensitivity_sections.append(
            f"#### {window} seconds\n\n"
            f"Corpus retention: {window_retention['resolvable']:,}/{window_retention['all_unique_grep_calls']:,} "
            f"({100 * window_retention['retention_rate']:.2f}%); {window_retention['all_excluded']:,} excluded. "
            f"Scored population: {block['population']['in_scope_resolvable_queries']:,} in-scope retained, "
            f"{block['population']['scorable_positive_queries']:,} Read-positive IR queries, and "
            f"{block['population']['behavioral_next_grep_failures']:,} behavioral failures.\n\n"
            + metric_table(block["arms"], include_legacy=True)
            + f"\n\n{window_verdict} Identifier-aware minus legacy BM25 recall@20: **{delta_text}**."
        )

    index_stats = metadata.get("index", {}).get("stats", {})
    commit = metadata.get("snapshot", {}).get("commit", "unknown")
    dirty = metadata.get("snapshot", {}).get("source_dirty", "unknown")
    lines = [
        "# Retention",
        "",
        f"The live corpus contained **{diag.get('raw_grep_tool_uses', 0):,} raw Grep blocks** and "
        f"**{diag.get('unique_grep_calls', 0):,} unique `(sessionId, toolUseId)` calls**. "
        f"The spec's 13,108 count is therefore stale by {diag.get('unique_grep_calls', 0) - 13108:,} calls for this frozen pass.",
        "",
        f"At the authoritative 300-second window, **{r.get('resolvable', 0):,} were retained "
        f"({100 * r.get('retention_rate', 0):.2f}%)** and **{r.get('all_excluded', 0):,} were excluded**: "
        f"{r.get('excluded_abandonment', 0):,} abandonments, {r.get('excluded_missing_grep_result', 0):,} missing Grep results, "
        f"and {r.get('excluded_unresolved_read_followup', 0):,} unresolved Read follow-ups. "
        f"The retained outcomes comprise {r.get('positive_read', 0):,} Read positives and "
        f"{r.get('failure_next_grep', 0):,} next-Grep failures.",
        "",
        "| Window | Unique Greps | Resolvable | Retention | Excluded | Abandoned | Missing Grep result | Unresolved Read |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|",
        *[
            "| " + " | ".join([
                f"{window}s",
                f"{retention['retention'][str(window)].get('all_unique_grep_calls', 0):,}",
                f"{retention['retention'][str(window)].get('resolvable', 0):,}",
                f"{100 * retention['retention'][str(window)].get('retention_rate', 0):.2f}%",
                f"{retention['retention'][str(window)].get('all_excluded', 0):,}",
                f"{retention['retention'][str(window)].get('excluded_abandonment', 0):,}",
                f"{retention['retention'][str(window)].get('excluded_missing_grep_result', 0):,}",
                f"{retention['retention'][str(window)].get('excluded_unresolved_read_followup', 0):,}",
            ]) + " |"
            for window in WINDOWS
        ],
        "",
        f"The 300-second abandonments break down as {r.get('abandoned_next_causal_action_after_window', 0):,} whose next causal action was between 300 and 900 seconds, "
        f"{r.get('abandoned_next_causal_action_after_900s', 0):,} whose next causal action was after 900 seconds, "
        f"{r.get('abandoned_no_later_causal_read_or_grep', 0):,} with no later causal Read/Grep, and "
        f"{r.get('abandoned_only_non_descendant_action_observed', 0):,} with only a non-descendant action observed.",
        "",
        f"The pass froze {retention.get('corpus_bytes_at_snapshot', 0):,} input bytes at "
        f"`{retention.get('snapshot_utc')}` and hashed the streamed snapshot as "
        f"`{retention.get('corpus_stream_sha256')}`. It excluded lines appended after those per-file size boundaries.",
        "",
        f"Copied transcript history remains visible: the {diag.get('unique_grep_calls', 0):,} schema IDs represent "
        f"{diag.get('clone_dedup_grep_executions', 0):,} globally distinct tool-use IDs; "
        f"{diag.get('schema_ids_from_copied_history', 0):,} schema IDs are copied history. No train/dev split or fitting was used.",
        "",
        "Read paths came from structured result metadata when present; "
        f"{diag.get('read_paths_from_paired_input_fallback', 0):,} successful subagent/image results lacked `filePath`, "
        "so only their path was recovered from the paired call. Input offsets and limits were never used.",
        "",
        "## Per-arm results (300 seconds)",
        "",
        f"The target population is {population['in_scope_resolvable_queries']:,} retained queries whose recorded search scope lies in "
        f"`toolsenabled-current`. Recall uses the {population['scorable_positive_queries']:,} queries with at least "
        f"one in-repository Read label. Precision and failure use those plus the "
        f"{population['behavioral_next_grep_failures']:,} retained next-Grep failures. Size, error, and latency use all in-scope retained queries. "
        f"{population['outside_read_labels_removed']:,} external Read labels were removed; "
        f"{population['positive_queries_emptied_by_repo_filter']:,} otherwise-positive queries then had no IR label.",
        "",
        metric_table(p_arms, include_legacy=False),
        "",
        "Response bytes are additive arithmetic means of the benchmark's agent-visible payload; errors are serialized as a UTF-8 "
        "`[error]` diagnostic rather than counted as zero bytes. Estimated tokens are exactly bytes/4. "
        "Recall and precision are macro means across their stated query denominators; each query's precision divides hits by fixed K. "
        "`failure@20` is the fraction of IR-scorable positives plus behavioral next-Grep failures "
        "that either were behavioral failures or had no relevant path in the first 20. Read-positive records left with only external labels "
        "are excluded from quality denominators.",
        "For the control, Claude Grep's fixed `--max-columns 500` omission rule is replayed, absent `-n` uses its line-number default, "
        "explicit `head_limit: 0` is unlimited, and a positive recorded `head_limit` stops the child only after `offset + head_limit` "
        "complete stdout lines have arrived. Absolute temporary-worktree "
        "prefixes are rewritten to the logical checkout path before byte measurement; rankings are parsed from the untouched physical stdout. "
        "Hidden project files are searched while `.git` is excluded.",
        "Each index response contains at most 20 unique paths. Every score-free block is a `path:start-end` header plus at most "
        "400 UTF-8 bytes from that path's best-ranked stable region; scores and benchmark diagnostics are never exposed.",
        "",
        "## Head-to-head verdict",
        "",
        f"**{verdict}**",
        "",
        f"Every arm used the same clean HEAD snapshot `{commit}`. None of the transcript records carries an explicit commit SHA, "
        f"so **all {population['in_scope_resolvable_queries']:,} scored records are HEAD fallbacks**, not exact historical reconstructions. "
        f"The source checkout was dirty (`{dirty}`), but its uncommitted files were not included.",
        "",
        "## Tokenisation ablation",
        "",
        "| Variant | R@1 | R@5 | R@10 | R@20 | Mean bytes | Failure@20 |",
        "|---|---:|---:|---:|---:|---:|---:|",
        "| Identifier-aware `bm25` | " + " | ".join([
            *(pct(aware.get(f"recall@{k}")) for k in KS), num(aware.get("response_bytes_mean")), pct(aware.get("failure@20"))
        ]) + " |",
        "| Legacy `/[a-z0-9_]+/` | " + " | ".join([
            *(pct(legacy.get(f"recall@{k}")) for k in KS), num(legacy.get("response_bytes_mean")), pct(legacy.get("failure@20"))
        ]) + " |",
        "",
        (
            "Identifier-aware tokenisation could not be compared because a required recall@20 value was unavailable. " + ablation_verdict
            if camel_delta is None
            else f"Identifier-aware tokenisation changed recall@20 by **{camel_delta * 100:+.2f} percentage points**. {ablation_verdict}"
        ),
        "",
        "## Follow-up-window sensitivity",
        "",
        "The query labels and retained population were regenerated independently at each fixed window; no window was selected from results.",
        "",
        "\n\n".join(sensitivity_sections),
        "",
        "## Claims that could NOT be verified",
        "",
        "- Exact historical repository-state performance could not be verified because transcripts lack commit SHAs and dirty-state snapshots.",
        "- Customer/general-language portability could not be verified: this is one team's Node-dominated corpus and only the primary repository was scored.",
        "- Counterfactual agent behavior could not be verified. Read labels reflect what agents did after seeing ripgrep, not what they would read after another retriever.",
        "- True model-token counts could not be verified; the required bytes/4 estimate was used.",
        "- Cold-cache latency could not be verified. The timed run uses a disclosed query-independent warmup because each temporary worktree otherwise starts artificially cold.",
        "- Precision treats unjudged returned paths as nonrelevant, as required, but the corpus does not prove that every un-read path was truly irrelevant.",
        "- Copied transcript prefixes could not be assigned a unique causal session without changing the spec's `(sessionId, toolUseId)` record identity.",
        "",
        "## What would change this verdict",
        "",
        "- Explicit per-query commit SHAs or frozen worktrees could change both control and index recall by restoring files that moved or disappeared by HEAD.",
        "- Manual relevance judgments, or logs from randomized retriever exposure, could change the ranking by removing ripgrep's exposure bias.",
        "- A different fixed response contract (K or snippet budget) could change the simultaneous recall/size verdict; it must be declared before seeing labels.",
        "- A deployment dominated by cold repository scans could change the latency comparison; this report measures warmed steady-state queries.",
        "- Re-measurement on non-Node repositories and independent teams could change the portability verdict.",
        "- Including files rejected by the safety boundary could improve index recall, but would violate the benchmark's required security constraint.",
        "",
        "## Per-claim confidence",
        "",
        f"- **Retention — high.** Counts come from one frozen-size streaming pass over {retention.get('files_total', 0):,} files, with duplicate schema IDs, missing results, causality, and exclusions counted explicitly.",
        f"- **Measured HEAD head-to-head — moderate.** All arms share one snapshot and {population['scorable_positive_queries']:,} positive queries, but labels are exposure-biased and copied histories reweight some events.",
        "- **Response-size comparison — high for this serializer.** Native ripgrep stdout, fixed index blocks, and explicit error diagnostics were measured as UTF-8 bytes; only the conversion to tokens is an estimate.",
        "- **Tokenisation-ablation effect — moderate.** The regions, BM25 implementation, queries, and labels are paired; this is still one repository and one implicit-feedback source.",
        "- **Latency — moderate for this machine, low for deployment generalisation.** It includes ripgrep startup and warm persistent SQLite queries after a symmetric byte-cache warmup, but is one Windows host under local load.",
        "- **Historical performance — low.** Every scored record fell back to HEAD, so no claim is made about the exact tree seen by the original agent.",
        "",
        "## Reproduction and fixed caps",
        "",
        f"Index build: {index_stats.get('files_indexed', '—')} files, {index_stats.get('chunks_indexed', '—')} stable regions, "
        f"{index_stats.get('bytes_indexed', '—')} indexed bytes. The fixed caps were 512 KiB/file, 128 KiB/line, "
        "and files over 8 KiB with fewer than two line feeds were rejected. Regions target 4,096 bytes with 512-byte overlap "
        f"and {metadata.get('index', {}).get('snap_tolerance_bytes', '—')}-byte structural snap tolerance. "
        "Symlinks/reparse points, gitignored files (including tracked ignored files), `.git`, `node_modules`, binary/invalid UTF-8/control-heavy files, "
        "and the exact `sensitiveFile` filename/extension/plaintext-secret matches were excluded.",
        "",
        f"Query order was shuffled with fixed seed `{QUERY_ORDER_SEED}` and arm order rotated per query. Latency includes response serialization, "
        "ripgrep subprocess startup, recorded response-window collection, errors, and empty results; index construction is excluded. "
        "Ripgrep's fixed operational timeout is 60 seconds; a recorded positive line cap may complete earlier.",
        "Before timing, the harness performs one query-independent sequential read of every tracked snapshot file and of the SQLite database, "
        "plus one fixed no-match ripgrep traversal to pay this host's executable/file security-scan cost. This removes the temporary-worktree "
        "cold-cache artifact; all warmup time is recorded in `run_metadata.json` and excluded from latency.",
    ]
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    args = parse_args()
    eval_dir = args.eval_dir.resolve()
    repo = args.repo.resolve(strict=True)
    eval_900 = eval_dir / "evalset_900.jsonl"
    retention_path = eval_dir / "retention.json"
    if not eval_900.exists() or not retention_path.exists():
        raise SystemExit("eval sets are missing; run build_evalset.py first")
    commit = run(("git", "-C", str(repo), "rev-parse", "HEAD"))
    branch = run(("git", "-C", str(repo), "branch", "--show-current"))
    dirty = bool(run(("git", "-C", str(repo), "status", "--porcelain")))

    metadata_path = eval_dir / "run_metadata.json"
    with clean_worktree(repo, commit, eval_dir) as source_root:
        if not args.skip_build:
            build_stats = index.build_index(args.db, source_root, logical_root=repo)
        else:
            build_stats = index.index_stats(args.db).get("stats", {})
        index_metadata = index.index_stats(args.db)
        validate_index(index_metadata, commit, repo)
        run_fingerprint = fingerprint(commit, eval_dir, repo, args.db, index_metadata)
        warmup = {
            "snapshot": warm_snapshot(source_root),
            "ripgrep_traversal": warm_ripgrep(source_root),
            "index_database": warm_file(args.db),
            "included_in_query_latency": False,
        }
        conn = index.connect_index(args.db)
        try:
            records = load_jsonl(eval_900)
            run_rows, selection = run_queries(
                records,
                conn,
                source_root,
                repo,
                args.runs,
                run_fingerprint,
                args.fresh,
                args.max_queries,
            )
        finally:
            conn.close()

    metadata = {
        "schema_version": 1,
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "fingerprint": run_fingerprint,
        "query_order_seed": QUERY_ORDER_SEED,
        "snapshot": {"commit": commit, "branch": branch, "source_dirty": dirty, "state_mode": "clean_HEAD_fallback_all_arms"},
        "selection": selection,
        "warmup": warmup,
        "index": {**index_metadata, "stats": build_stats},
        "arms": list(ALL_ARMS),
    }
    atomic_json(metadata_path, metadata)
    metrics = aggregate_metrics(eval_dir, run_rows, repo)
    metrics["metadata"] = metadata
    atomic_json(args.metrics, metrics)
    retention = json.loads(retention_path.read_text(encoding="utf-8"))
    report = report_markdown(metrics, retention, metadata)
    report_partial = args.report.with_suffix(args.report.suffix + ".tmp")
    report_partial.write_text(report, encoding="utf-8")
    os.replace(report_partial, args.report)
    print(json.dumps({"metrics": str(args.metrics), "report": str(args.report), "queries": selection}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
