#!/usr/bin/env python3
"""Prepare isolated partial mirrors for deterministic conflict mining.

The preferred clone command is intentionally explicit::

    git clone --mirror --filter=blob:none \
        --reference-if-able <existing-source> --dissociate <url> <temporary>

If the local reference is absent, the preferred command fails, or its result
does not verify, preparation retries with no reference.  A completed mirror is
published only after its pinned commit, bare configuration, partial-clone
configuration, origin, and independence from alternates have been verified.

The shared source repositories are never passed to a mutating Git command.
Only temporary directories below the task-owned mirror root may be removed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = Path(__file__).with_name("repositories.json")
DEFAULT_SOURCE_ROOT = PROJECT_ROOT / "corpus" / "_clones"
SHA1_RE = re.compile(r"[0-9a-f]{40}\Z")
SLUG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
REQUIRED_REPOSITORY_FIELDS = {
    "repo",
    "slug",
    "local_source_slug",
    "url",
    "frozen_head",
    "primary_language",
    "primary_shape",
    "project_shape_note",
}


class PreparationError(RuntimeError):
    """An expected, repository-scoped preparation failure."""


def _display_path(path: Path) -> str:
    """Prefer a stable project-relative path in reports."""

    resolved = path.resolve(strict=False)
    try:
        return resolved.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return str(resolved)


def load_manifest(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    if not isinstance(manifest, dict):
        raise PreparationError("manifest root must be an object")
    if manifest.get("schema_version") != 1:
        raise PreparationError("manifest schema_version must be 1")
    repositories = manifest.get("repositories")
    if not isinstance(repositories, list) or not repositories:
        raise PreparationError("manifest repositories must be a non-empty array")

    seen_repos: set[str] = set()
    seen_slugs: set[str] = set()
    for index, repository in enumerate(repositories):
        if not isinstance(repository, dict):
            raise PreparationError(f"repositories[{index}] must be an object")
        missing = sorted(REQUIRED_REPOSITORY_FIELDS - repository.keys())
        if missing:
            raise PreparationError(
                f"repositories[{index}] lacks required fields: {', '.join(missing)}"
            )
        repo = repository["repo"]
        slug = repository["slug"]
        source_slug = repository["local_source_slug"]
        frozen_head = repository["frozen_head"]
        if not isinstance(repo, str) or repo.count("/") != 1:
            raise PreparationError(f"repositories[{index}].repo is invalid")
        if not isinstance(slug, str) or not SLUG_RE.fullmatch(slug):
            raise PreparationError(f"repositories[{index}].slug is unsafe")
        if not isinstance(source_slug, str) or not SLUG_RE.fullmatch(source_slug):
            raise PreparationError(
                f"repositories[{index}].local_source_slug is unsafe"
            )
        if not isinstance(frozen_head, str) or not SHA1_RE.fullmatch(frozen_head):
            raise PreparationError(
                f"repositories[{index}].frozen_head must be a lowercase SHA-1"
            )
        if repo in seen_repos:
            raise PreparationError(f"duplicate repository name: {repo}")
        if slug in seen_slugs:
            raise PreparationError(f"duplicate repository slug: {slug}")
        seen_repos.add(repo)
        seen_slugs.add(slug)
    return manifest


def select_repositories(
    manifest: dict[str, Any], requested_slugs: Iterable[str]
) -> list[dict[str, Any]]:
    repositories = manifest["repositories"]
    requested = set(requested_slugs)
    if not requested:
        return list(repositories)
    known = {repository["slug"] for repository in repositories}
    unknown = sorted(requested - known)
    if unknown:
        raise PreparationError(f"unknown --only slug(s): {', '.join(unknown)}")
    return [
        repository
        for repository in repositories
        if repository["slug"] in requested
    ]


def _git_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_LFS_SKIP_SMUDGE": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return environment


def run_command(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        check=False,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        env=_git_environment(),
    )


def _command_result(
    mode: str, command: Sequence[str], completed: subprocess.CompletedProcess[str]
) -> dict[str, Any]:
    return {
        "mode": mode,
        "command": list(command),
        "returncode": completed.returncode,
        "stderr": completed.stderr.strip(),
    }


def _git_in_bare(git: str, repository: Path, *arguments: str) -> list[str]:
    return [git, "--git-dir", str(repository), *arguments]


def _read_git_value(git: str, repository: Path, *arguments: str) -> str:
    completed = run_command(_git_in_bare(git, repository, *arguments))
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise PreparationError(
            f"git {' '.join(arguments)} failed with {completed.returncode}: {detail}"
        )
    return completed.stdout.strip()


def ensure_pinned_commit(
    git: str, repository: Path, frozen_head: str
) -> dict[str, Any]:
    expression = f"{frozen_head}^{{commit}}"
    check = run_command(_git_in_bare(git, repository, "cat-file", "-e", expression))
    if check.returncode == 0:
        return {"fetched": False, "commit": frozen_head}

    fetch_command = _git_in_bare(
        git,
        repository,
        "fetch",
        "--quiet",
        "--filter=blob:none",
        "--no-tags",
        "--no-write-fetch-head",
        "origin",
        frozen_head,
    )
    fetch = run_command(fetch_command)
    if fetch.returncode != 0:
        detail = fetch.stderr.strip() or check.stderr.strip()
        raise PreparationError(
            f"pinned commit {frozen_head} is absent and exact fetch failed: {detail}"
        )
    final_check = run_command(
        _git_in_bare(git, repository, "cat-file", "-e", expression)
    )
    if final_check.returncode != 0:
        raise PreparationError(
            f"pinned commit {frozen_head} is absent after a successful fetch"
        )
    return {"fetched": True, "commit": frozen_head}


def verify_mirror(
    git: str, repository: Path, expected_url: str, frozen_head: str
) -> dict[str, Any]:
    if not repository.is_dir():
        raise PreparationError(f"mirror is not a directory: {repository}")
    bare = _read_git_value(git, repository, "rev-parse", "--is-bare-repository")
    if bare != "true":
        raise PreparationError(f"mirror is not bare: {repository}")
    origin = _read_git_value(git, repository, "remote", "get-url", "origin")
    if origin != expected_url:
        raise PreparationError(
            f"origin mismatch: expected {expected_url!r}, observed {origin!r}"
        )
    promisor = _read_git_value(
        git, repository, "config", "--bool", "remote.origin.promisor"
    )
    if promisor != "true":
        raise PreparationError("remote.origin.promisor is not true")
    partial_filter = _read_git_value(
        git, repository, "config", "--get", "remote.origin.partialclonefilter"
    )
    if partial_filter != "blob:none":
        raise PreparationError(
            f"partial clone filter is {partial_filter!r}, not 'blob:none'"
        )
    alternates = repository / "objects" / "info" / "alternates"
    if alternates.exists() and alternates.read_text(encoding="utf-8").strip():
        raise PreparationError("mirror still borrows objects through alternates")
    pinned = ensure_pinned_commit(git, repository, frozen_head)
    resolved = _read_git_value(git, repository, "rev-parse", frozen_head)
    if resolved != frozen_head:
        raise PreparationError(
            f"pinned commit resolved to {resolved}, expected {frozen_head}"
        )
    return {
        "bare": True,
        "origin": origin,
        "promisor": True,
        "partial_clone_filter": partial_filter,
        "alternates": False,
        "pinned_commit": frozen_head,
        "pinned_commit_fetched": pinned["fetched"],
    }


def _remove_readonly(
    function: Any, path: str, _error: tuple[type[BaseException], BaseException, Any]
) -> None:
    os.chmod(path, stat.S_IWRITE)
    function(path)


def remove_temporary(repository: Path, mirror_root: Path) -> None:
    root = mirror_root.resolve(strict=False)
    if repository.parent.resolve(strict=False) != root:
        raise PreparationError(f"refusing to remove a path outside {root}")
    if not repository.name.endswith(".partial"):
        raise PreparationError(f"refusing to remove non-temporary path {repository}")
    if repository.is_symlink():
        raise PreparationError(
            f"refusing to remove symlinked temporary path {repository}"
        )
    if repository.exists():
        shutil.rmtree(repository, onerror=_remove_readonly)


def clone_command(
    git: str,
    repository: dict[str, Any],
    source: Path,
    temporary: Path,
    use_reference: bool,
) -> list[str]:
    command = [git, "clone", "--mirror", "--filter=blob:none"]
    if use_reference:
        command.extend(
            ["--reference-if-able", str(source), "--dissociate"]
        )
    command.extend(["--quiet", repository["url"], str(temporary)])
    return command


def planned_repository(
    git: str,
    repository: dict[str, Any],
    source_root: Path,
    mirror_root: Path,
) -> dict[str, Any]:
    source = source_root / repository["local_source_slug"]
    temporary = mirror_root / f"{repository['slug']}.partial"
    destination = mirror_root / repository["slug"]
    use_reference = source.is_dir()
    return {
        "repo": repository["repo"],
        "slug": repository["slug"],
        "status": "planned",
        "source": _display_path(source),
        "source_reference_available": use_reference,
        "destination": _display_path(destination),
        "command": clone_command(
            git, repository, source, temporary, use_reference=use_reference
        ),
    }


def prepare_repository(
    git: str,
    repository: dict[str, Any],
    source_root: Path,
    mirror_root: Path,
) -> dict[str, Any]:
    source = source_root / repository["local_source_slug"]
    destination = mirror_root / repository["slug"]
    temporary = mirror_root / f"{repository['slug']}.partial"
    base_result: dict[str, Any] = {
        "repo": repository["repo"],
        "slug": repository["slug"],
        "source": _display_path(source),
        "destination": _display_path(destination),
        "attempts": [],
    }

    if destination.exists():
        try:
            verification = verify_mirror(
                git, destination, repository["url"], repository["frozen_head"]
            )
        except (OSError, PreparationError) as error:
            return {
                **base_result,
                "status": "failed",
                "reason": "existing_mirror_failed_verification",
                "error": str(error),
            }
        return {**base_result, "status": "reused", "verification": verification}

    remove_temporary(temporary, mirror_root)
    modes = [True, False] if source.is_dir() else [False]
    last_error = "clone did not run"
    for use_reference in modes:
        mode = "reference-and-dissociate" if use_reference else "direct"
        command = clone_command(
            git, repository, source, temporary, use_reference=use_reference
        )
        completed = run_command(command)
        base_result["attempts"].append(_command_result(mode, command, completed))
        if completed.returncode != 0:
            last_error = (
                completed.stderr.strip() or f"clone exited {completed.returncode}"
            )
            remove_temporary(temporary, mirror_root)
            continue
        try:
            verification = verify_mirror(
                git, temporary, repository["url"], repository["frozen_head"]
            )
        except (OSError, PreparationError) as error:
            last_error = str(error)
            remove_temporary(temporary, mirror_root)
            continue
        try:
            temporary.replace(destination)
        except OSError as error:
            remove_temporary(temporary, mirror_root)
            return {
                **base_result,
                "status": "failed",
                "reason": "atomic_publish_failed",
                "error": str(error),
            }
        return {
            **base_result,
            "status": "prepared",
            "clone_mode": mode,
            "verification": verification,
        }
    return {
        **base_result,
        "status": "failed",
        "reason": "clone_or_verification_failed",
        "error": last_error,
    }


def build_report(results: list[dict[str, Any]], dry_run: bool) -> dict[str, Any]:
    counts = {
        status: sum(result["status"] == status for result in results)
        for status in ("planned", "prepared", "reused", "failed")
    }
    return {
        "schema_version": 1,
        "artifact_type": "conflict_mirror_preparation_report",
        "dry_run": dry_run,
        "repository_count": len(results),
        "counts": counts,
        "results": results,
    }


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.partial")
    temporary.write_text(
        json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(path)


def parse_arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--source-root", type=Path, default=DEFAULT_SOURCE_ROOT)
    parser.add_argument(
        "--mirror-root",
        type=Path,
        help="override the mirror_root recorded in the manifest",
    )
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="SLUG",
        help="prepare only this manifest slug; repeat to select more than one",
    )
    parser.add_argument("--git", default="git", help="Git executable")
    parser.add_argument(
        "--report", type=Path, help="atomically write the JSON report to this path"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate inputs and print commands without creating any directory",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_arguments(argv)
    try:
        manifest = load_manifest(arguments.manifest.resolve())
        repositories = select_repositories(manifest, arguments.only)
        source_root = arguments.source_root.resolve(strict=False)
        if arguments.mirror_root is None:
            mirror_root = (PROJECT_ROOT / manifest["mirror_root"]).resolve(strict=False)
        else:
            mirror_root = arguments.mirror_root.resolve(strict=False)

        if arguments.dry_run:
            results = [
                planned_repository(
                    arguments.git, repository, source_root, mirror_root
                )
                for repository in repositories
            ]
        else:
            mirror_root.mkdir(parents=True, exist_ok=True)
            results = []
            for repository in repositories:
                try:
                    result = prepare_repository(
                        arguments.git,
                        repository,
                        source_root,
                        mirror_root,
                    )
                except Exception as error:  # keep later repositories independent
                    result = {
                        "repo": repository["repo"],
                        "slug": repository["slug"],
                        "status": "failed",
                        "reason": "unexpected_preparation_error",
                        "error": f"{type(error).__name__}: {error}",
                    }
                results.append(result)
        report = build_report(results, arguments.dry_run)
    except (OSError, PreparationError, json.JSONDecodeError) as error:
        print(f"prepare_repositories.py: {error}", file=sys.stderr)
        return 2

    rendered = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    sys.stdout.write(rendered)
    if arguments.report is not None:
        if arguments.dry_run:
            print(
                "prepare_repositories.py: --report is not written in --dry-run mode",
                file=sys.stderr,
            )
        else:
            write_report(arguments.report.resolve(), report)
    return 1 if report["counts"]["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
