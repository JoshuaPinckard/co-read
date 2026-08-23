"""Extract one first-parent chronological commit stream per successfully cloned repo."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import BinaryIO, Iterator

from common import (
    CAP_COMMITS,
    CAP_THRESHOLD_REACHABLE_COMMITS,
    CLONE_ROOT,
    CORPUS_PATH,
    SCHEMA_VERSION,
    STREAM_ROOT,
    atomic_write_json,
    ensure_directories,
    load_json,
    run_git,
    selected_repositories,
    utc_now,
)


LOG_FORMAT = "%x00COMMIT%x00%H%x00%ct%x00%P"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", action="append", dest="repos", help="Repository slug; repeatable.")
    return parser.parse_args()


def null_tokens(stream: BinaryIO, chunk_size: int = 1 << 20) -> Iterator[bytes]:
    pending = b""
    while True:
        chunk = stream.read(chunk_size)
        if not chunk:
            if pending:
                yield pending
            return
        pieces = (pending + chunk).split(b"\0")
        pending = pieces.pop()
        yield from pieces


def decode_path(raw: bytes) -> str:
    return raw.decode("utf-8", errors="surrogateescape")


def parse_log(stream: BinaryIO) -> Iterator[dict[str, object]]:
    """Parse `git log --name-status -z` without assuming newline-safe file names."""
    tokens = iter(null_tokens(stream))
    try:
        first = next(tokens)
    except StopIteration:
        return
    if first != b"" or next(tokens, None) != b"COMMIT":
        raise ValueError("git log stream did not begin with the expected NUL commit marker")

    while True:
        sha_raw = next(tokens, None)
        timestamp_raw = next(tokens, None)
        parents_raw = next(tokens, None)
        if sha_raw is None or timestamp_raw is None or parents_raw is None:
            raise ValueError("truncated commit header")
        changes: list[dict[str, str]] = []
        next_commit = False

        for raw_status in tokens:
            if raw_status == b"":
                marker = next(tokens, None)
                if marker != b"COMMIT":
                    raise ValueError(f"unexpected empty log token followed by {marker!r}")
                next_commit = True
                break

            status_text = raw_status.lstrip(b"\r\n").decode("ascii", errors="strict")
            code = status_text[:1]
            if code in {"A", "M", "D", "T"}:
                path_raw = next(tokens, None)
                if path_raw is None:
                    raise ValueError(f"truncated {status_text} record")
                changes.append(
                    {"status": "M" if code == "T" else code, "raw_status": status_text, "path": decode_path(path_raw)}
                )
            elif code == "R":
                old_raw = next(tokens, None)
                new_raw = next(tokens, None)
                if old_raw is None or new_raw is None:
                    raise ValueError(f"truncated {status_text} record")
                changes.append(
                    {
                        "status": "R",
                        "raw_status": status_text,
                        "old_path": decode_path(old_raw),
                        "new_path": decode_path(new_raw),
                    }
                )
            else:
                raise ValueError(f"unsupported git name-status code {status_text!r}")

        yield {
            "sha": sha_raw.decode("ascii"),
            "timestamp": int(timestamp_raw),
            "parents": parents_raw.decode("ascii").split(),
            "changes": changes,
        }
        if not next_commit:
            return


def tree_paths(repository: Path, treeish: str) -> list[str]:
    result = run_git(repository, ["ls-tree", "-r", "--name-only", "-z", treeish], text=False)
    assert isinstance(result.stdout, bytes)
    return sorted(decode_path(token) for token in result.stdout.split(b"\0") if token)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def object_storage(repository: Path) -> dict[str, int | str]:
    result = run_git(repository, ["count-objects", "-v"])
    parsed: dict[str, int | str] = {}
    for line in str(result.stdout).splitlines():
        if ": " not in line:
            continue
        key, value = line.split(": ", 1)
        parsed[key] = int(value) if value.isdigit() else value
    return parsed


def extract_repository(spec: dict[str, str], corpus_record: dict[str, object]) -> dict[str, object]:
    repository = CLONE_ROOT / spec["slug"]
    destination = STREAM_ROOT / f"{spec['slug']}.jsonl.gz"
    temporary = destination.with_name(destination.name + ".tmp")
    meta_path = STREAM_ROOT / f"{spec['slug']}.meta.json"
    first_parent_count = int(corpus_record["first_parent_commit_count"])
    reachable_count = int(corpus_record["reachable_commit_count"])
    capped = reachable_count > CAP_THRESHOLD_REACHABLE_COMMITS
    expected_count = min(CAP_COMMITS, first_parent_count) if capped else first_parent_count
    arguments = [
        "log",
        "--first-parent",
        "--reverse",
        "--root",
        "--diff-merges=first-parent",
        # Correctly keep ordinary modified renames as one identity. A partial clone
        # may lazily fetch blobs for similarity detection; storage is recorded.
        "--find-renames=50%",
        "-l0",
        "--name-status",
        "-z",
        f"--format={LOG_FORMAT}",
    ]
    if capped:
        arguments.append(f"--max-count={CAP_COMMITS}")
    arguments.append("HEAD")

    command = [
        "git",
        "-c",
        "core.longpaths=true",
        "-c",
        "diff.renameLimit=0",
        "-C",
        str(repository),
        *arguments,
    ]
    started_at = utc_now()
    object_storage_before = object_storage(repository)
    stderr_capture = tempfile.TemporaryFile()
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=stderr_capture)
    assert process.stdout is not None
    commits = parse_log(process.stdout)
    first_commit: dict[str, object] | None = None
    previous_sha: str | None = None
    count = 0
    merge_count = 0
    rename_count = 0
    status_histogram: dict[str, int] = {}

    try:
        with gzip.open(temporary, "wt", encoding="utf-8", errors="surrogatepass", newline="\n") as output:
            for commit in commits:
                if first_commit is None:
                    first_commit = commit
                    parents = commit["parents"]
                    assert isinstance(parents, list)
                    initial_tree = parents[0] if parents else None
                    initial_files = tree_paths(repository, initial_tree) if initial_tree else []
                    header = {
                        "type": "header",
                        "schema_version": SCHEMA_VERSION,
                        "repository": spec,
                        "source_head_sha": corpus_record["resolved_head_sha"],
                        "first_parent_commit_count": first_parent_count,
                        "capped": capped,
                        "reachable_commit_count": reachable_count,
                        "cap_threshold_reachable_commits": CAP_THRESHOLD_REACHABLE_COMMITS,
                        "cap_basis": "all commits reachable from HEAD",
                        "cap_commits": CAP_COMMITS if capped else None,
                        "cap_reason": (
                            f"reachable history {reachable_count} > {CAP_THRESHOLD_REACHABLE_COMMITS}; "
                            f"left-truncated replay of the most recent {CAP_COMMITS} commits; "
                            "the learned indexes start empty at the window boundary"
                            if capped
                            else None
                        ),
                        "initial_tree_sha": initial_tree,
                        "initial_files": initial_files,
                        "git_log_arguments": arguments,
                        "extracted_at_utc": started_at,
                    }
                    output.write(json.dumps(header, ensure_ascii=True, sort_keys=True) + "\n")

                parents = commit["parents"]
                assert isinstance(parents, list)
                if previous_sha is not None and (not parents or parents[0] != previous_sha):
                    raise ValueError(
                        f"first-parent chain broken at {commit['sha']}: expected first parent {previous_sha}, got {parents}"
                    )
                if len(parents) > 1:
                    merge_count += 1
                changes = commit["changes"]
                assert isinstance(changes, list)
                for change in changes:
                    status = str(change["status"])
                    status_histogram[status] = status_histogram.get(status, 0) + 1
                    rename_count += status == "R"
                commit["type"] = "commit"
                commit["index"] = count
                output.write(json.dumps(commit, ensure_ascii=True, sort_keys=True) + "\n")
                previous_sha = str(commit["sha"])
                count += 1

        returncode = process.wait()
        stderr_capture.seek(0)
        stderr = stderr_capture.read().decode("utf-8", errors="replace")
        if returncode != 0:
            raise RuntimeError(f"git log exited {returncode}: {stderr[-4000:]}")
        if "rename detection was skipped" in stderr.lower():
            raise RuntimeError(f"git skipped exhaustive rename detection: {stderr[-4000:]}")
        if count != expected_count:
            raise ValueError(f"expected {expected_count} first-parent commits, extracted {count}")
        if previous_sha != corpus_record["resolved_head_sha"]:
            raise ValueError(f"stream ended at {previous_sha}, expected HEAD {corpus_record['resolved_head_sha']}")
        os.replace(temporary, destination)
    except Exception:
        if process.poll() is None:
            process.kill()
        process.wait()
        stderr_capture.close()
        raise
    stderr_capture.close()

    metadata: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "repository": spec,
        "status": "ok",
        "source_head_sha": corpus_record["resolved_head_sha"],
        "commit_count": count,
        "first_parent_commit_count": first_parent_count,
        "reachable_commit_count": reachable_count,
        "capped": capped,
        "cap_reason": (
            f"reachable history {reachable_count} > {CAP_THRESHOLD_REACHABLE_COMMITS}; "
            f"left-truncated replay of the most recent {CAP_COMMITS}; learned indexes started empty"
            if capped
            else None
        ),
        "merge_commit_count": merge_count,
        "rename_count": rename_count,
        "status_histogram": dict(sorted(status_histogram.items())),
        "stream_path": f"exploratory/language-hole/streams/{destination.name}",
        "stream_sha256": sha256_file(destination),
        "object_storage_before_git_log": object_storage_before,
        "object_storage_after_git_log": object_storage(repository),
        "git_log_stderr_tail": stderr[-8_000:],
        "started_at_utc": started_at,
        "completed_at_utc": utc_now(),
    }
    atomic_write_json(meta_path, metadata)
    return metadata


def failed_metadata(spec: dict[str, str], exc: Exception) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "repository": spec,
        "status": "failed",
        "failure_type": type(exc).__name__,
        "failure": str(exc),
        "completed_at_utc": utc_now(),
    }


def main() -> None:
    ensure_directories()
    corpus = load_json(CORPUS_PATH, default={}) or {}
    records = corpus.get("repositories", {})
    for spec in selected_repositories(parse_args().repos):
        record = records.get(spec["slug"])
        meta_path = STREAM_ROOT / f"{spec['slug']}.meta.json"
        if not record or record.get("status") != "ok":
            metadata = failed_metadata(spec, RuntimeError("clone did not complete successfully"))
        else:
            try:
                metadata = extract_repository(spec, record)
            except Exception as exc:
                metadata = failed_metadata(spec, exc)
        atomic_write_json(meta_path, metadata)
        print(f"{spec['name']}: {metadata['status']}", flush=True)


if __name__ == "__main__":
    main()
