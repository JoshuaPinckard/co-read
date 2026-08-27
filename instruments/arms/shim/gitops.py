from __future__ import annotations

import dataclasses
import shutil
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .schema import Side, Site
from .util import (
    ShimError,
    byte_regions,
    copy_path_state,
    git_text,
    run_git,
    regions_overlap,
    safe_relative,
    sha256_bytes,
    snapshot_tree,
    tree_path,
)


FIXED_GIT_ENV = {
    "GIT_AUTHOR_NAME": "ARMS Shim",
    "GIT_AUTHOR_EMAIL": "arms-shim@example.invalid",
    "GIT_COMMITTER_NAME": "ARMS Shim",
    "GIT_COMMITTER_EMAIL": "arms-shim@example.invalid",
    "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
    "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z",
}


@dataclasses.dataclass(frozen=True)
class MergeResult:
    clean: bool
    commit: str | None
    tree: str | None
    returncode: int | None
    stdout: bytes
    stderr: bytes


class ScratchRepository:
    """Owned bare clone and owned worktrees; the corpus mirror is read-only."""

    def __init__(self, *, site: Site, root: Path) -> None:
        self.site = site
        self.root = root.resolve()
        self.bare = self.root / "owned.git"
        self.worktrees = self.root / "w"
        self._names: set[str] = set()

    def create(self) -> None:
        self.root.mkdir(parents=True, exist_ok=False)
        result = run_git(
            ["clone", "--bare", "--no-hardlinks", str(self.site.mirror), str(self.bare)],
            cwd=self.root.parent,
            check=True,
        )
        self.worktrees.mkdir()
        alternates = self.bare / "objects" / "info" / "alternates"
        if alternates.exists():
            raise ShimError(f"owned mirror unexpectedly depends on alternates: {alternates}")
        run_git(
            ["--git-dir", str(self.bare), "config", "gc.auto", "0"],
            cwd=self.root,
        )
        observed = git_text(
            ["--git-dir", str(self.bare), "rev-parse", f"{self.site.base_commit}^{{tree}}"],
            cwd=self.root,
        )
        if observed != self.site.base_tree:
            raise ShimError(f"owned mirror base-tree mismatch: {observed} != {self.site.base_tree}")

    def worktree(self, name: str, commit: str) -> Path:
        if name in self._names:
            raise ShimError(f"duplicate worktree name: {name}")
        self._names.add(name)
        destination = self.worktrees / name
        run_git(
            [
                "--git-dir",
                str(self.bare),
                "worktree",
                "add",
                "--detach",
                "--force",
                str(destination),
                commit,
            ],
            cwd=self.root,
        )
        actual = git_text(["-C", str(destination), "rev-parse", "HEAD"], cwd=self.root)
        if actual != commit:
            raise ShimError(f"worktree identity mismatch: {actual} != {commit}")
        return destination

    def apply_patch(self, tree: Path, patch: Path) -> tuple[bool, bytes, bytes]:
        result = run_git(
            [
                "-C",
                str(tree),
                "apply",
                "--binary",
                "--whitespace=nowarn",
                str(patch.resolve()),
            ],
            cwd=self.root,
            check=False,
        )
        return result.returncode == 0 and not result.timed_out, result.stdout, result.stderr

    def commit_task_sources(
        self,
        *,
        task_tree: Path,
        source_base: str,
        changed_paths: Iterable[str],
        excluded_test_paths: set[str],
        name: str,
        message: str,
    ) -> str:
        destination = self.worktree(name, source_base)
        for relative in sorted(set(map(safe_relative, changed_paths))):
            if relative in excluded_test_paths:
                continue
            if relative == ".pytest_cache" or relative.startswith(".pytest_cache/"):
                continue
            if relative == "__pycache__" or "/__pycache__/" in f"/{relative}/":
                continue
            source = tree_path(task_tree, relative)
            target = tree_path(destination, relative)
            copy_path_state(source, target)
        run_git(["-C", str(destination), "add", "-A"], cwd=self.root)
        env = FIXED_GIT_ENV
        run_git(
            ["-C", str(destination), "commit", "--allow-empty", "-m", message],
            cwd=self.root,
            env=env,
        )
        return git_text(["-C", str(destination), "rev-parse", "HEAD"], cwd=self.root)

    def merge_tree(self, left: str, right: str, *, message: str) -> MergeResult:
        result = run_git(
            ["--git-dir", str(self.bare), "merge-tree", "--write-tree", left, right],
            cwd=self.root,
            check=False,
        )
        stdout_text = result.stdout.decode("utf-8", errors="replace")
        first = stdout_text.splitlines()[0].strip() if stdout_text.splitlines() else ""
        tree = first if len(first) == 40 and all(c in "0123456789abcdef" for c in first) else None
        if result.returncode != 0 or tree is None:
            return MergeResult(False, None, tree, result.returncode, result.stdout, result.stderr)
        env = FIXED_GIT_ENV
        commit_result = run_git(
            [
                "--git-dir",
                str(self.bare),
                "commit-tree",
                tree,
                "-p",
                left,
                "-p",
                right,
                "-m",
                message,
            ],
            cwd=self.root,
            env=env,
        )
        commit = commit_result.stdout.decode("ascii", errors="replace").strip()
        return MergeResult(True, commit, tree, result.returncode, result.stdout, result.stderr)

    def _blob(self, commit: str, relative: str) -> bytes | None:
        result = run_git(
            ["--git-dir", str(self.bare), "show", f"{commit}:{relative}"],
            cwd=self.root,
            check=False,
        )
        if result.returncode != 0:
            return None
        return result.stdout

    def _mode(self, commit: str, relative: str) -> str | None:
        result = run_git(
            [
                "--git-dir",
                str(self.bare),
                "ls-tree",
                "-z",
                commit,
                "--",
                relative,
            ],
            cwd=self.root,
            check=False,
        )
        if result.returncode != 0 or not result.stdout:
            return None
        header = result.stdout.split(b"\t", 1)[0]
        return header.split(b" ", 1)[0].decode("ascii", errors="replace")

    @staticmethod
    def _edits(base: bytes, produced: bytes, side: str) -> list[dict[str, Any]]:
        return [
            {
                **region,
                "side": side,
                "replacement": produced[
                    int(region["new_start"]) : int(region["new_end"])
                ],
            }
            for region in byte_regions(base, produced)
        ]

    @staticmethod
    def _answer_matches(edit: Mapping[str, Any], answer_edits: Sequence[Mapping[str, Any]]) -> bool:
        return any(
            int(edit["old_start"]) == int(answer["old_start"])
            and int(edit["old_end"]) == int(answer["old_end"])
            and edit["new_region_sha256"] == answer["new_region_sha256"]
            for answer in answer_edits
        )

    def _harvest_bytes(
        self,
        *,
        base: bytes,
        left: bytes,
        right: bytes,
        answer: bytes | None,
    ) -> tuple[bytes, list[dict[str, Any]]]:
        left_edits = self._edits(base, left, "A")
        right_edits = self._edits(base, right, "B")
        all_edits = [*left_edits, *right_edits]
        answer_edits = byte_regions(base, answer) if answer is not None else []
        selected: set[int] = set()
        visited: set[int] = set()
        decisions: list[dict[str, Any]] = []
        for seed in range(len(all_edits)):
            if seed in visited:
                continue
            component = {seed}
            queue = [seed]
            while queue:
                current = queue.pop()
                for candidate in range(len(all_edits)):
                    if candidate in component:
                        continue
                    if all_edits[current]["side"] == all_edits[candidate]["side"]:
                        continue
                    if regions_overlap(all_edits[current], all_edits[candidate]):
                        component.add(candidate)
                        queue.append(candidate)
            visited.update(component)
            sides = {all_edits[index]["side"] for index in component}
            if len(sides) == 1:
                selected.update(component)
                reason = "noncontested-produced-region"
                selected_side = next(iter(sides))
            else:
                scores = {
                    side: sum(
                        self._answer_matches(all_edits[index], answer_edits)
                        for index in component
                        if all_edits[index]["side"] == side
                    )
                    for side in ("A", "B")
                }
                selected_side = "B" if scores["B"] > scores["A"] else "A"
                selected.update(
                    index for index in component if all_edits[index]["side"] == selected_side
                )
                reason = (
                    "answer-key-selected-produced-regions"
                    if scores[selected_side] > 0
                    else "frozen-A-produced-region-tie-break"
                )
            decisions.append(
                {
                    "old_start": min(int(all_edits[index]["old_start"]) for index in component),
                    "old_end": max(int(all_edits[index]["old_end"]) for index in component),
                    "contested": len(sides) > 1,
                    "candidate_sides": sorted(sides),
                    "selected_from": selected_side,
                    "reason": reason,
                    "produced_region_hashes": {
                        all_edits[index]["side"]: all_edits[index]["new_region_sha256"]
                        for index in component
                    },
                }
            )
        output = base
        chosen = [all_edits[index] for index in selected]
        for edit in sorted(
            chosen,
            key=lambda row: (int(row["old_start"]), int(row["old_end"])),
            reverse=True,
        ):
            output = (
                output[: int(edit["old_start"])]
                + bytes(edit["replacement"])
                + output[int(edit["old_end"]) :]
            )
        return output, decisions

    def changed_paths(self, base: str, target: str) -> tuple[str, ...]:
        result = run_git(
            [
                "--git-dir",
                str(self.bare),
                "--literal-pathspecs",
                "diff",
                "--name-only",
                "-z",
                "--no-renames",
                base,
                target,
                "--",
            ],
            cwd=self.root,
        )
        return tuple(
            sorted(
                part.decode("utf-8", errors="surrogateescape")
                for part in result.stdout.split(b"\0")
                if part
            )
        )

    def harvest(
        self,
        *,
        base: str,
        left: str,
        right: str,
        name: str,
        message: str,
    ) -> tuple[str, list[dict[str, Any]]]:
        """Combine produced byte regions; the answer key only selects among them."""
        destination = self.worktree(name, base)
        paths = sorted(set(self.changed_paths(base, left)) | set(self.changed_paths(base, right)))
        choices: list[dict[str, Any]] = []
        for relative in paths:
            base_blob = self._blob(base, relative)
            left_blob = self._blob(left, relative)
            right_blob = self._blob(right, relative)
            answer_blob = self._blob(self.site.answer_commit, relative)
            base_mode = self._mode(base, relative)
            left_mode = self._mode(left, relative)
            right_mode = self._mode(right, relative)
            left_changed = left_blob != base_blob
            right_changed = right_blob != base_blob
            if left_changed and not right_changed:
                selected, selected_blob, reason = "A", left_blob, "only-A-changed"
                selected_mode = left_mode
                region_choices: list[dict[str, Any]] = []
            elif right_changed and not left_changed:
                selected, selected_blob, reason = "B", right_blob, "only-B-changed"
                selected_mode = right_mode
                region_choices = []
            elif left_blob == right_blob:
                selected, selected_blob, reason = "A+B", left_blob, "identical-produced-blob"
                selected_mode = left_mode
                region_choices = []
            elif base_blob is not None and left_blob is not None and right_blob is not None:
                selected_blob, region_choices = self._harvest_bytes(
                    base=base_blob,
                    left=left_blob,
                    right=right_blob,
                    answer=answer_blob,
                )
                selected = "region-composition"
                reason = "mechanical-produced-region-harvest"
                if left_mode == right_mode:
                    selected_mode = left_mode
                elif answer_blob == left_blob:
                    selected_mode = left_mode
                elif answer_blob == right_blob:
                    selected_mode = right_mode
                else:
                    selected_mode = left_mode or right_mode or base_mode
            elif answer_blob is not None and answer_blob == left_blob:
                selected, selected_blob, reason = "A", left_blob, "answer-key-selected-produced-blob"
                selected_mode = left_mode
                region_choices = []
            elif answer_blob is not None and answer_blob == right_blob:
                selected, selected_blob, reason = "B", right_blob, "answer-key-selected-produced-blob"
                selected_mode = right_mode
                region_choices = []
            else:
                selected, selected_blob, reason = "A", left_blob, "frozen-A-tie-break"
                selected_mode = left_mode
                region_choices = []
            target = tree_path(destination, relative)
            if selected_blob is None:
                if target.exists() or target.is_symlink():
                    if target.is_dir():
                        shutil.rmtree(target)
                    else:
                        target.unlink()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                if selected_mode == "120000":
                    if target.exists() or target.is_symlink():
                        target.unlink()
                    try:
                        target.symlink_to(selected_blob.decode("utf-8", errors="surrogateescape"))
                    except OSError as error:
                        raise ShimError(f"could not materialize produced symlink {relative}: {error}") from error
                else:
                    target.write_bytes(selected_blob)
                    if selected_mode == "100755":
                        target.chmod(target.stat().st_mode | 0o111)
            choices.append(
                {
                    "path": relative,
                    "selected_from": selected,
                    "reason": reason,
                    "selected_sha256": sha256_bytes(selected_blob or b""),
                    "answer_key_equal": selected_blob == answer_blob,
                    "selected_mode": selected_mode,
                    "region_choices": region_choices,
                    "oracle_bytes_synthesized": False,
                }
            )
        run_git(["-C", str(destination), "add", "-A"], cwd=self.root)
        env = FIXED_GIT_ENV
        run_git(
            ["-C", str(destination), "commit", "--allow-empty", "-m", message],
            cwd=self.root,
            env=env,
        )
        commit = git_text(["-C", str(destination), "rev-parse", "HEAD"], cwd=self.root)
        return commit, choices

    def tracked_paths(self, commit: str) -> set[str]:
        result = run_git(
            ["--git-dir", str(self.bare), "ls-tree", "-r", "--name-only", "-z", commit],
            cwd=self.root,
        )
        return {
            part.decode("utf-8", errors="surrogateescape")
            for part in result.stdout.split(b"\0")
            if part
        }


def source_paths_from_records(records: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    return tuple(sorted({safe_relative(str(record["path"])) for record in records}))
