"""Build and validate Click causal tasks against one historical base.

The procedure is deliberately mechanical:

* enumerate commits in a fixed history mode after a fixed anchor, oldest first;
* require an unambiguous PR number, a ``src/click`` change, and an added or
  modified Python test;
* apply the exact source-and-test portion of the first-parent diff to the
  anchor with Git's three-way machinery, rejecting every conflict;
* re-diff that index against the anchor (this only refreshes context);
* require full-suite green on the base, red with tests only, and green with
  both source and tests.

No conflict is resolved and no patch hunk is edited by this program.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path


DEFAULT_ANCHOR = "02046e7a19480f85fff7e4577486518abe47e401"
EXISTING_PRS = {
    "787",
    "973",
    "994",
    "999",
    "1014",
    "1061",
    "2972",
    "2991",
    "3013",
    "3137",
    "3239",
    "3299",
    "3330",
}
COMMIT_MARKER = b"@@@BLAST_RADIUS_COMMIT@@@"
DIFF_SPLIT = re.compile(br"(?m)(?=^diff --git )")
DIFF_HEADER = re.compile(br"diff --git a/(.+?) b/(.+)\r?$")


@dataclass(frozen=True)
class Candidate:
    sha: str
    first_parent: str
    subject: str
    pr: str
    historical_patch: bytes


def run(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    stdin: bytes | None = None,
    timeout: int = 180,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        input=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        shell=False,
    )


def git(
    repository: Path,
    *arguments: str,
    env: dict[str, str] | None = None,
    stdin: bytes | None = None,
    timeout: int = 180,
) -> subprocess.CompletedProcess[bytes]:
    return run(
        ["git", "-c", "core.longpaths=true", *arguments],
        cwd=repository,
        env=env,
        stdin=stdin,
        timeout=timeout,
    )


def chunks(patch: bytes) -> list[bytes]:
    return [part for part in DIFF_SPLIT.split(patch) if part.startswith(b"diff --git ")]


def new_path(chunk: bytes) -> str | None:
    match = DIFF_HEADER.match(chunk.splitlines()[0])
    if match is None:
        return None
    return match.group(2).decode("utf-8", errors="surrogateescape")


def relevant_historical_patch(patch: bytes) -> tuple[bytes, list[str], list[str]]:
    selected: list[bytes] = []
    source_paths: list[str] = []
    test_paths: list[str] = []
    for chunk in chunks(patch):
        path = new_path(chunk)
        if path is None:
            continue
        if path.startswith("src/click/"):
            selected.append(chunk)
            source_paths.append(path)
        elif path.startswith("tests/"):
            selected.append(chunk)
            if (
                path.endswith(".py")
                and b"deleted file mode" not in chunk
                and (b"@@" in chunk or b"new file mode" in chunk)
            ):
                test_paths.append(path)
    return b"".join(selected), sorted(set(source_paths)), sorted(set(test_paths))


def enumerate_candidates(
    repository: Path,
    anchor: str,
    end: str,
    history_mode: str,
    excluded_prs: set[str],
) -> list[Candidate]:
    format_arg = (
        "--format="
        + COMMIT_MARKER.decode("ascii")
        + "%H%x09%P%x09%s"
    )
    if history_mode == "first-parent":
        result = git(
            repository,
            "log",
            "--first-parent",
            "--reverse",
            format_arg,
            "-p",
            "--binary",
            "--full-index",
            "--find-renames=50%",
            "-l0",
            f"{anchor}..{end}",
            timeout=300,
        )
    else:
        result = git(
            repository,
            "log",
            "--reverse",
            "--topo-order",
            format_arg,
            f"{anchor}..{end}",
            timeout=300,
        )
    if result.returncode != 0:
        raise RuntimeError(result.stdout.decode("utf-8", errors="replace"))

    candidates: list[Candidate] = []
    seen_prs = set(EXISTING_PRS) | excluded_prs
    for record in result.stdout.split(COMMIT_MARKER)[1:]:
        header, _, inline_patch = record.partition(b"\n")
        fields = header.decode("utf-8", errors="replace").rstrip().split("\t", 2)
        if len(fields) != 3:
            continue
        sha, parents, subject = fields
        parent_list = parents.split()
        if not parent_list:
            continue
        pr_numbers = re.findall(r"#(\d+)", subject)
        if not pr_numbers:
            continue
        pr = pr_numbers[-1]
        if pr in seen_prs:
            continue
        if history_mode == "first-parent":
            patch = inline_patch
        else:
            diff = git(
                repository,
                "diff",
                "--binary",
                "--full-index",
                "--find-renames=50%",
                "-l0",
                parent_list[0],
                sha,
                "--",
                "src/click/",
                "tests/",
            )
            if diff.returncode != 0:
                raise RuntimeError(diff.stdout.decode("utf-8", errors="replace"))
            patch = diff.stdout
        relevant, source_paths, test_paths = relevant_historical_patch(patch)
        if not source_paths or not test_paths:
            continue
        seen_prs.add(pr)
        candidates.append(
            Candidate(
                sha=sha,
                first_parent=parent_list[0],
                subject=subject,
                pr=pr,
                historical_patch=relevant,
            )
        )
    return candidates


def temporary_index(repository: Path, anchor: str) -> tuple[dict[str, str], Path]:
    descriptor, raw_path = tempfile.mkstemp(prefix="blast-radius-index-", suffix=".idx")
    os.close(descriptor)
    path = Path(raw_path)
    path.unlink()
    env = os.environ.copy()
    env["GIT_INDEX_FILE"] = str(path)
    result = git(repository, "read-tree", anchor, env=env)
    if result.returncode != 0:
        raise RuntimeError(result.stdout.decode("utf-8", errors="replace"))
    return env, path


def regenerate(
    repository: Path, candidate: Candidate, anchor: str
) -> tuple[bytes | None, bytes | None, dict[str, object]]:
    env, index_path = temporary_index(repository, anchor)
    try:
        applied = git(
            repository,
            "apply",
            "--3way",
            "--cached",
            "--binary",
            "--whitespace=nowarn",
            "-",
            env=env,
            stdin=candidate.historical_patch,
        )
        application_output = applied.stdout.decode("utf-8", errors="replace").strip()
        if applied.returncode != 0:
            return None, None, {
                "outcome": "three_way_conflict",
                "three_way_exit": applied.returncode,
                "three_way_output": application_output,
            }

        regenerated = git(
            repository,
            "diff",
            "--cached",
            "--binary",
            "--full-index",
            anchor,
            "--",
            "src/click/",
            "tests/",
            env=env,
        )
        if regenerated.returncode != 0:
            raise RuntimeError(regenerated.stdout.decode("utf-8", errors="replace"))
        patch = regenerated.stdout
        test_patch = b"".join(
            chunk for chunk in chunks(patch) if (new_path(chunk) or "").startswith("tests/")
        )
        source_paths = sorted(
            {
                path
                for chunk in chunks(patch)
                if (path := new_path(chunk)) is not None and path.startswith("src/click/")
            }
        )
        test_paths = sorted(
            {
                path
                for chunk in chunks(test_patch)
                if (path := new_path(chunk)) is not None
                and path.endswith(".py")
                and b"deleted file mode" not in chunk
                and (b"@@" in chunk or b"new file mode" in chunk)
            }
        )
        if not patch or not test_patch or not source_paths or not test_paths:
            return None, None, {
                "outcome": "empty_regenerated_component",
                "three_way_exit": 0,
                "three_way_output": application_output,
                "source_paths": source_paths,
                "test_paths": test_paths,
            }
        return patch, test_patch, {
            "outcome": "regenerated",
            "three_way_exit": 0,
            "three_way_output": application_output,
            "source_paths": source_paths,
            "test_paths": test_paths,
        }
    finally:
        if index_path.exists():
            index_path.unlink()


def junit_summary(path: Path) -> dict[str, object]:
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    # Avoid double counting a parent aggregate if nested suites ever appear.
    leaves = [suite for suite in suites if not any(child.tag == "testsuite" for child in suite)]
    counts = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    for suite in leaves:
        for key in counts:
            counts[key] += int(suite.attrib.get(key, "0"))
    cases: list[str] = []
    for case in root.iter("testcase"):
        if case.find("failure") is not None or case.find("error") is not None:
            classname = case.attrib.get("classname", "")
            name = case.attrib.get("name", "")
            cases.append(f"{classname}::{name}" if classname else name)
    counts["passed"] = counts["tests"] - counts["failures"] - counts["errors"] - counts["skipped"]
    return {**counts, "failing_cases": sorted(cases)}


def pytest_suite(base: Path, pythonpath: str, label: str) -> dict[str, object]:
    descriptor, raw_xml = tempfile.mkstemp(prefix=f"blast-radius-{label}-", suffix=".xml")
    os.close(descriptor)
    xml_path = Path(raw_xml)
    xml_path.unlink()
    env = os.environ.copy()
    env["PYTHONPATH"] = pythonpath
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTEST_ADDOPTS"] = ""
    started = time.monotonic()
    try:
        result = run(
            [sys.executable, "-m", "pytest", "-q", f"--junitxml={xml_path}"],
            cwd=base,
            env=env,
            timeout=60,
        )
        exit_code = result.returncode
        output_bytes = result.stdout
    except subprocess.TimeoutExpired as error:
        exit_code = 124
        output_bytes = error.stdout or b""
    elapsed = time.monotonic() - started
    output = output_bytes.decode("utf-8", errors="replace")
    fallback_summary = {
        "tests": 0,
        "failures": 0,
        "errors": 1,
        "skipped": 0,
        "passed": 0,
        "failing_cases": ["pytest_timeout_or_missing_junit"],
    }
    try:
        try:
            summary = junit_summary(xml_path) if xml_path.exists() else fallback_summary
        except (ET.ParseError, OSError):
            summary = fallback_summary
    finally:
        if xml_path.exists():
            xml_path.unlink()
    return {
        "exit": exit_code,
        "seconds": round(elapsed, 3),
        "summary": summary,
        "output_tail": "\n".join(output.splitlines()[-30:]),
    }


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if not path.is_file() or ".pytest_cache" in path.parts or "__pycache__" in path.parts:
            continue
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        data = path.read_bytes()
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def apply_patch(base: Path, patch: bytes, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    env = os.environ.copy()
    env["GIT_CEILING_DIRECTORIES"] = str(base.parent)
    return git(
        base,
        "apply",
        "--binary",
        "--whitespace=nowarn",
        *arguments,
        "-",
        env=env,
        stdin=patch,
    )


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def validate(
    repository: Path,
    base: Path,
    compat_site_packages: Path,
    output: Path,
    target: int,
    anchor: str,
    end: str,
    base_name: str,
    history_mode: str,
    excluded_prs: set[str],
) -> dict[str, object]:
    candidates = enumerate_candidates(
        repository, anchor, end, history_mode, excluded_prs
    )
    source_root = base / "src"
    pythonpath = os.pathsep.join((str(source_root), str(compat_site_packages)))
    pristine_digest = tree_digest(base)
    baseline = pytest_suite(base, pythonpath, "baseline")
    if baseline["exit"] != 0:
        raise RuntimeError(f"base suite is not green: {baseline}")

    attempts: list[dict[str, object]] = []
    accepted = 0
    for candidate in candidates:
        if accepted >= target:
            break
        print(f"[{len(attempts) + 1}] PR {candidate.pr} {candidate.sha[:12]}", flush=True)
        patch, test_patch, regeneration = regenerate(repository, candidate, anchor)
        record: dict[str, object] = {
            "candidate_number": len(attempts) + 1,
            "pr": int(candidate.pr),
            "task_id": f"pr-{candidate.pr}",
            "commit": candidate.sha,
            "first_parent": candidate.first_parent,
            "subject": candidate.subject,
            "base": base_name,
            "base_anchor": anchor,
            "regeneration": regeneration,
        }
        attempts.append(record)
        if patch is None or test_patch is None:
            record["accepted"] = False
            continue

        test_application = apply_patch(base, test_patch, "--check")
        source_application = apply_patch(base, patch, "--check", "--exclude=tests/**")
        record["apply_check"] = {
            "test_exit": test_application.returncode,
            "test_output": test_application.stdout.decode("utf-8", errors="replace").strip(),
            "source_exit": source_application.returncode,
            "source_output": source_application.stdout.decode("utf-8", errors="replace").strip(),
        }
        if test_application.returncode != 0 or source_application.returncode != 0:
            record["accepted"] = False
            record["outcome"] = "regenerated_patch_did_not_apply"
            continue

        applied_tests = apply_patch(base, test_patch)
        if applied_tests.returncode != 0:
            raise RuntimeError(applied_tests.stdout.decode("utf-8", errors="replace"))
        red = pytest_suite(base, pythonpath, f"pr-{candidate.pr}-red")
        record["test_only"] = red
        red_summary = red["summary"]
        is_red = red["exit"] == 1 and (
            int(red_summary["failures"]) + int(red_summary["errors"])
        ) > 0
        if not is_red:
            reversed_tests = apply_patch(base, test_patch, "--reverse")
            if reversed_tests.returncode != 0:
                raise RuntimeError(reversed_tests.stdout.decode("utf-8", errors="replace"))
            record["accepted"] = False
            record["outcome"] = "test_only_not_red"
            if tree_digest(base) != pristine_digest:
                raise RuntimeError(f"base restoration failed after PR {candidate.pr}")
            continue

        applied_source = apply_patch(base, patch, "--exclude=tests/**")
        if applied_source.returncode != 0:
            raise RuntimeError(applied_source.stdout.decode("utf-8", errors="replace"))
        green = pytest_suite(base, pythonpath, f"pr-{candidate.pr}-green")
        record["source_and_test"] = green
        is_green = green["exit"] == 0 and int(green["summary"]["failures"]) == 0 and int(
            green["summary"]["errors"]
        ) == 0

        reversed_source = apply_patch(base, patch, "--reverse", "--exclude=tests/**")
        reversed_tests = apply_patch(base, test_patch, "--reverse")
        if reversed_source.returncode != 0 or reversed_tests.returncode != 0:
            raise RuntimeError(
                "patch reversal failed: "
                + reversed_source.stdout.decode("utf-8", errors="replace")
                + reversed_tests.stdout.decode("utf-8", errors="replace")
            )
        if tree_digest(base) != pristine_digest:
            raise RuntimeError(f"base restoration failed after PR {candidate.pr}")

        if not is_green:
            record["accepted"] = False
            record["outcome"] = "source_and_test_not_green"
            continue

        task_dir = output / "patches"
        task_dir.mkdir(parents=True, exist_ok=True)
        patch_path = task_dir / f"pr-{candidate.pr}.patch"
        test_patch_path = task_dir / f"pr-{candidate.pr}.tests.patch"
        patch_path.write_bytes(patch)
        test_patch_path.write_bytes(test_patch)
        record.update(
            {
                "accepted": True,
                "outcome": "green_red_green",
                "patch_sha256": sha256(patch),
                "patch_bytes": len(patch),
                "test_patch_sha256": sha256(test_patch),
                "test_patch_bytes": len(test_patch),
            }
        )
        accepted += 1
        print(
            f"  accepted {accepted}/{target}; red failures+errors="
            f"{int(red_summary['failures']) + int(red_summary['errors'])}",
            flush=True,
        )

    result = {
        "schema_version": 1,
        "selection_order": (
            "first-parent commits after anchor, oldest to newest"
            if history_mode == "first-parent"
            else "all reachable commits after anchor, reverse topological order"
        ),
        "history_mode": history_mode,
        "excluded_prs": sorted(int(pr) for pr in excluded_prs),
        "anchor": anchor,
        "end": end,
        "base_name": base_name,
        "anchor_tree": git(repository, "rev-parse", f"{anchor}^{{tree}}").stdout.decode().strip(),
        "base_digest_sha256": pristine_digest,
        "baseline": baseline,
        "candidate_universe_after_static_filter": len(candidates),
        "candidates_examined": len(attempts),
        "tasks_yielded": accepted,
        "target": target,
        "attempts": attempts,
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "results.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True, type=Path)
    parser.add_argument("--base", required=True, type=Path)
    parser.add_argument("--compat-site-packages", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--target", type=int, default=40)
    parser.add_argument("--anchor", default=DEFAULT_ANCHOR)
    parser.add_argument("--end", default="HEAD")
    parser.add_argument("--base-name", default="base-expansion")
    parser.add_argument(
        "--history-mode", choices=("first-parent", "all"), default="first-parent"
    )
    parser.add_argument(
        "--exclude-pr",
        action="append",
        default=[],
        help="PR number already examined in an earlier disjoint pass (repeatable)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repository = args.repository.resolve(strict=True)
    base = args.base.resolve(strict=True)
    compat = args.compat_site_packages.resolve(strict=True)
    output = args.output.resolve()
    if output.exists():
        raise FileExistsError(f"refusing to overwrite output directory: {output}")
    anchor_result = git(repository, "rev-parse", f"{args.anchor}^{{commit}}")
    end_result = git(repository, "rev-parse", f"{args.end}^{{commit}}")
    if anchor_result.returncode != 0 or end_result.returncode != 0:
        raise ValueError("anchor or end does not resolve to a commit")
    anchor = anchor_result.stdout.decode().strip()
    end = end_result.stdout.decode().strip()
    result = validate(
        repository,
        base,
        compat,
        output,
        args.target,
        anchor,
        end,
        args.base_name,
        args.history_mode,
        set(args.exclude_pr),
    )
    print(json.dumps({key: result[key] for key in ("candidates_examined", "tasks_yielded", "target")}, indent=2))
    return 0 if result["tasks_yielded"] == result["target"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
