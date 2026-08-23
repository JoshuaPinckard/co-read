"""Run and record the posture experiment's repository eligibility gate.

The gate deliberately compares normalized test identities and outcomes rather
than stdout or wall time.  A repository is eligible only when five consecutive
runs are green, have identical normalized results, stay below the configured
runtime ceiling, and leave the tracked worktree unchanged.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "exploratory" / "posture" / "repository-gates"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def run(
    command: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        cwd=cwd,
        env=env,
        timeout=timeout,
        check=check,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
    )


def git(repository: Path, *arguments: str) -> str:
    result = run(["git", "-c", "core.longpaths=true", *arguments], cwd=repository, check=True)
    return result.stdout.strip()


def tracked_state(repository: Path) -> dict[str, str]:
    return {
        "head": git(repository, "rev-parse", "HEAD"),
        "head_tree": git(repository, "rev-parse", "HEAD^{tree}"),
        "index_tree": git(repository, "write-tree"),
        "tracked_status": git(repository, "status", "--porcelain", "--untracked-files=no"),
        "tracked_diff_sha256": hashlib.sha256(
            run(
                ["git", "-c", "core.longpaths=true", "diff", "--binary", "--no-ext-diff"],
                cwd=repository,
                check=True,
            ).stdout.encode("utf-8")
        ).hexdigest(),
    }


def normalized_junit(path: Path) -> dict[str, Any]:
    root = ET.parse(path).getroot()
    cases: list[dict[str, str | None]] = []
    for case in root.iter("testcase"):
        outcome = "passed"
        detail: str | None = None
        for tag in ("failure", "error", "skipped"):
            child = case.find(tag)
            if child is not None:
                outcome = tag
                detail = child.attrib.get("type") or child.attrib.get("message")
                break
        cases.append(
            {
                "classname": case.attrib.get("classname", ""),
                "name": case.attrib.get("name", ""),
                "outcome": outcome,
                "detail": detail,
            }
        )
    cases.sort(key=lambda item: (str(item["classname"]), str(item["name"]), str(item["outcome"])))
    counts = {key: 0 for key in ("passed", "failure", "error", "skipped")}
    for case in cases:
        counts[str(case["outcome"])] += 1
    canonical = json.dumps(cases, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return {
        "counts": counts,
        "case_count": len(cases),
        "cases_sha256": hashlib.sha256(canonical.encode("ascii")).hexdigest(),
        "cases": cases,
    }


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=True, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    os.replace(temporary, path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--name", required=True, help="Stable repository slug for the output directory")
    parser.add_argument("--python", required=True, type=Path)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--runtime-ceiling", type=float, default=120.0)
    parser.add_argument("--timeout", type=float, default=150.0)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repository = args.repo.resolve(strict=True)
    python = args.python.resolve(strict=True)
    output = args.output_root.resolve() / args.name
    output.mkdir(parents=True, exist_ok=True)
    baseline = tracked_state(repository)
    invocation = [str(python), "-m", "pytest"]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(repository / "src")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    records: list[dict[str, Any]] = []

    for index in range(1, args.runs + 1):
        before = tracked_state(repository)
        junit_path = output / f"run-{index}.xml"
        command = [*invocation, f"--junitxml={junit_path}"]
        started_at = utc_now()
        started = time.perf_counter()
        timed_out = False
        try:
            result = run(command, cwd=repository, env=env, timeout=args.timeout)
            returncode = result.returncode
            stdout = result.stdout
            stderr = result.stderr
        except subprocess.TimeoutExpired as error:
            timed_out = True
            returncode = None
            stdout = error.stdout or ""
            stderr = error.stderr or ""
            if isinstance(stdout, bytes):
                stdout = stdout.decode("utf-8", errors="replace")
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", errors="replace")
        elapsed = time.perf_counter() - started
        (output / f"run-{index}.stdout.txt").write_text(stdout, encoding="utf-8", newline="\n")
        (output / f"run-{index}.stderr.txt").write_text(stderr, encoding="utf-8", newline="\n")
        after = tracked_state(repository)
        normalized = normalized_junit(junit_path) if junit_path.exists() else None
        records.append(
            {
                "run": index,
                "started_at_utc": started_at,
                "completed_at_utc": utc_now(),
                "command": command,
                "elapsed_seconds": elapsed,
                "returncode": returncode,
                "timed_out": timed_out,
                "before": before,
                "after": after,
                "state_matches_baseline_before": before == baseline,
                "state_matches_baseline_after": after == baseline,
                "normalized": normalized,
            }
        )
        atomic_json(
            output / "gate.partial.json",
            {"status": "running", "baseline": baseline, "runs": records},
        )

    signatures = [record["normalized"]["cases_sha256"] if record["normalized"] else None for record in records]
    eligible = all(
        record["returncode"] == 0
        and not record["timed_out"]
        and record["elapsed_seconds"] <= args.runtime_ceiling
        and record["state_matches_baseline_before"]
        and record["state_matches_baseline_after"]
        and record["normalized"] is not None
        for record in records
    ) and len(set(signatures)) == 1
    result = {
        "schema_version": 1,
        "measurement": "posture-repository-eligibility-gate",
        "status": "eligible" if eligible else "rejected",
        "repository": str(repository),
        "repository_name": args.name,
        "baseline": baseline,
        "protocol": {
            "required_runs": args.runs,
            "runtime_ceiling_seconds": args.runtime_ceiling,
            "timeout_seconds": args.timeout,
            "determinism_signature": "sorted JUnit testcase (classname, name, outcome, failure/error type or message)",
            "environment": {
                "python_executable": str(python),
                "python_version": platform.python_version(),
                "platform": platform.platform(),
                "PYTHONPATH": env["PYTHONPATH"],
                "PYTHONDONTWRITEBYTECODE": env["PYTHONDONTWRITEBYTECODE"],
            },
        },
        "identical_normalized_results": len(set(signatures)) == 1,
        "normalized_signatures": signatures,
        "runs": records,
        "completed_at_utc": utc_now(),
    }
    atomic_json(output / "gate.json", result)
    partial = output / "gate.partial.json"
    if partial.exists():
        partial.unlink()
    print(json.dumps({"status": result["status"], "signatures": signatures}, indent=2))
    return 0 if eligible else 1


if __name__ == "__main__":
    raise SystemExit(main())
