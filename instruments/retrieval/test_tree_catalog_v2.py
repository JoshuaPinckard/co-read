from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import provenance_v2 as provenance
import tree_catalog_v2 as catalog


def git(repo: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode:
        raise AssertionError(completed.stderr or completed.stdout)
    return completed.stdout.strip()


def init_repo(path: Path, filename: str = "state.txt") -> Path:
    path.mkdir(parents=True)
    git(path, "init")
    git(path, "config", "user.name", "Tree Catalog Test")
    git(path, "config", "user.email", "tree-catalog@example.invalid")
    git(path, "branch", "-M", "main")
    (path / filename).write_text("initial\n", encoding="utf-8")
    git(path, "add", filename)
    git(path, "commit", "-m", "initial")
    return path


def record(record_id: str, cwd: Path, path: Path | None, branch: str = "main") -> dict:
    query: dict[str, object] = {"pattern": "needle"}
    if path is not None:
        query["path"] = str(path)
    return {
        "id": record_id,
        "ts": 1_800_000_000.0,
        "cwd": str(cwd),
        "git_branch": branch,
        "query": query,
    }


def fixture_evalsets(tmp_path: Path) -> tuple[dict[int, list[dict]], dict[str, Path]]:
    desktop = init_repo(tmp_path / "Desktop", "desktop.txt")
    engine = init_repo(tmp_path / "toolsenabled-current", "engine.txt")
    mission = init_repo(tmp_path / "mission-control", "mission.txt")
    worktree = desktop / "toolsenabled" / "opensource" / "wt-capability"
    worktree.parent.mkdir(parents=True)
    git(mission, "worktree", "add", "-b", "capability", str(worktree))
    legacy = init_repo(desktop / "toolsenabled" / "legacy", "legacy.txt")

    missing_alias = desktop / "wt-capability" / "src"
    plain = desktop / "toolsenabled"
    component_collision = desktop / "toolsenabledness" / "src"
    live_target = worktree / "src"
    rows = {
        60: [record("alias", engine, missing_alias)],
        300: [
            record("alias", engine, missing_alias),
            record("plain", plain, None),
            record("component", engine, component_collision),
            record("live", worktree, live_target, "capability"),
        ],
        900: [
            record("alias", engine, missing_alias),
            record("plain", plain, None),
            record("live", worktree, live_target, "capability"),
        ],
    }
    return rows, {
        "desktop": desktop,
        "engine": engine,
        "mission": mission,
        "legacy": legacy,
        "worktree": worktree,
        "missing_alias": desktop / "wt-capability",
        "plain": plain,
        "component_collision": desktop / "toolsenabledness",
    }


@pytest.fixture(scope="module")
def built_fixture(tmp_path_factory: pytest.TempPathFactory):
    evalsets, paths = fixture_evalsets(tmp_path_factory.mktemp("tree-catalog"))
    return evalsets, paths, catalog.build_catalog(evalsets)


def entry_at(build: catalog.CatalogBuild, path: Path) -> catalog.TreeCatalogEntry:
    normalised = provenance.normalise_absolute_path(path)
    return next(entry for entry in build.entries if entry.tree.logical_root == normalised)


def test_catalog_maps_absolute_targets_and_vanished_worktree_aliases(built_fixture):
    _, paths, build = built_fixture
    alias_entry = entry_at(build, paths["missing_alias"])
    assert alias_entry.mapping_kind == "worktree_registry_basename_alias"
    assert alias_entry.tree.repository_identity
    assert alias_entry.tree.current_root == provenance.normalise_absolute_path(paths["worktree"])

    assignment = next(
        item
        for item in build.assignments
        if item.window_seconds == 300 and item.record_id == "alias"
    )
    assert assignment.target_tree_id == alias_entry.tree.tree_id
    assert assignment.cwd_tree_id != assignment.target_tree_id
    assert assignment.effective_scope == provenance.normalise_absolute_path(
        paths["missing_alias"] / "src"
    )
    assert not assignment.outside_any_indexed_tree


def test_plain_toolsenabled_exposes_epoch_candidates_and_matching_is_component_safe(
    built_fixture,
):
    _, paths, build = built_fixture
    plain = entry_at(build, paths["plain"])
    assert plain.mapping_kind == "logical_toolsenabled_epoch"
    assert {candidate.candidate_id for candidate in plain.epoch_candidates} == {
        "archived-legacy-root",
        "desktop-subtree",
        "legacy-engine-root",
    }
    assert all(candidate.repository_root for candidate in plain.epoch_candidates)

    collision = entry_at(build, paths["component_collision"])
    assert collision.tree.tree_id != plain.tree.tree_id
    component_assignment = next(
        item
        for item in build.assignments
        if item.window_seconds == 300 and item.record_id == "component"
    )
    assert component_assignment.target_tree_id == collision.tree.tree_id


def test_non_git_temp_roots_preserve_session_boundary():
    path = (
        r"C:\Users\Joshp\AppData\Local\Temp\claude"
        r"\c--users-joshp-desktop-toolsenabled"
        r"\1f77c7bb-a78e-4f94-a8e5-3b4000bf503e\scratchpad\result.txt"
    )
    assert catalog._empirical_non_git_root(path) == (
        r"c:\users\joshp\appdata\local\temp\claude"
        r"\c--users-joshp-desktop-toolsenabled"
        r"\1f77c7bb-a78e-4f94-a8e5-3b4000bf503e"
    )


def test_counts_cover_each_independently_generated_window_and_outputs_are_deterministic(
    tmp_path: Path, built_fixture,
):
    evalsets, _, build = built_fixture
    assert dict(build.record_counts) == {60: 1, 300: 4, 900: 3}
    assert len(build.assignments) == 8
    assert sum(dict(entry.target_counts)[300] for entry in build.entries) == 4

    output = tmp_path / "output"
    catalog_path, manifest_path = catalog.write_catalog_outputs(build, output)
    first_catalog = catalog_path.read_bytes()
    first_manifest = manifest_path.read_bytes()
    catalog.write_catalog_outputs(build, output)
    assert catalog_path.read_bytes() == first_catalog
    assert manifest_path.read_bytes() == first_manifest
    manifest = [json.loads(line) for line in manifest_path.read_text().splitlines()]
    assert len(manifest) == 8
    assert [item["window_seconds"] for item in manifest] == sorted(
        item["window_seconds"] for item in manifest
    )
