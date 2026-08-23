from __future__ import annotations

import contextlib
import json
from pathlib import Path
import subprocess

import pytest

import run_v2
import report_v2
import metrics_v2
from history_v2 import HeadSelection, HistoricalSelection
from provenance_v2 import BranchResolution, TreeSpec
from tree_catalog_v2 import CatalogBuild, EpochCandidate, ScopeAssignment, TreeCatalogEntry


def _tree(tree_id: str, logical: str, repository: str = "C:/repo") -> TreeSpec:
    return TreeSpec(
        tree_id=tree_id,
        logical_root=logical,
        repository_root=repository,
        repository_identity=repository + "/.git",
        current_root=None,
    )


def _assignment(target: str, cwd: str) -> ScopeAssignment:
    return ScopeAssignment(
        window_seconds=300,
        sequence=0,
        record_id="q",
        effective_scope="C:/target/src",
        cwd="C:/cwd",
        target_tree_id=target,
        cwd_tree_id=cwd,
        target_mapping_kind="git",
        cwd_mapping_kind="git",
        target_reason="mapped",
        cwd_reason="mapped",
        target_available=True,
        cwd_available=True,
        outside_any_indexed_tree=False,
    )


class FakeHistory:
    def __init__(self, selections=None):
        self.selections = selections or {}

    @staticmethod
    def repository_key(tree):
        return str(tree.repository_identity or tree.repository_root)

    def select(self, tree, branch, query_ts):
        selection = self.selections.get(tree.tree_id)
        resolution = BranchResolution(branch, f"refs/heads/{branch}", "local")
        return selection, resolution

    def head(self, tree, query_ts):
        return HeadSelection(
            tree_id=tree.tree_id,
            repository_root=str(tree.repository_root),
            repository_identity=self.repository_key(tree),
            repository_relative_root=tree.repository_relative_root,
            commit="head-commit",
            commit_ts=200,
            gap_seconds=float(query_ts) - 200 if query_ts is not None else None,
        )


def _historical(tree: TreeSpec, commit: str, ts: int) -> HistoricalSelection:
    return HistoricalSelection(
        tree_id=tree.tree_id,
        repository_root=str(tree.repository_root),
        repository_identity=str(tree.repository_identity),
        repository_relative_root=tree.repository_relative_root,
        requested_branch="main",
        resolved_ref="refs/heads/main",
        ref_kind="local",
        commit=commit,
        commit_ts=ts,
        gap_seconds=300 - ts,
        ancestry_order=0,
    )


def test_union_records_requires_identical_execution_inputs():
    first = {"id": "q", "ts": 1, "cwd": "C:/x", "git_branch": "main", "query": {"pattern": "a"}}
    changed = {**first, "query": {"pattern": "b"}}
    with pytest.raises(ValueError, match="different execution inputs"):
        run_v2.union_records({60: [first], 300: [changed]})


def test_shared_common_dir_does_not_license_sibling_tree_exactness():
    target = _tree("target", "C:/target", "C:/shared/worktree-a")
    cwd = TreeSpec(
        tree_id="cwd",
        logical_root="C:/cwd",
        repository_root="C:/shared/worktree-b",
        repository_identity=target.repository_identity,
    )
    entries = {
        "target": TreeCatalogEntry(target, "git"),
        "cwd": TreeCatalogEntry(cwd, "git"),
    }
    row = run_v2.reconstruct_record(
        {"id": "q", "ts": 300, "cwd": "C:/cwd", "git_branch": "main", "query": {"pattern": "x", "path": "C:/target"}},
        _assignment("target", "cwd"),
        entries,
        [300],
        FakeHistory(),
    )
    assert row["exact"] is False
    assert row["mode"] == "head_fallback"
    assert row["reason"] == "cwd_target_logical_tree_mismatch"


def test_divergent_epoch_tree_objects_force_head_fallback(monkeypatch):
    logical = _tree("plain", "C:/plain", "C:/repo-a")
    candidates = (
        EpochCandidate("a", "legacy", "C:/repo-a", "C:/repo-a/.git"),
        EpochCandidate("b", "desktop", "C:/repo-b", "C:/repo-b/.git", "plain"),
    )
    entry = TreeCatalogEntry(logical, "logical_toolsenabled_epoch", epoch_candidates=candidates)
    specs = [candidate.as_tree(logical) for candidate in candidates]
    history = FakeHistory(
        {
            specs[0].tree_id: _historical(specs[0], "a-commit", 290),
            specs[1].tree_id: _historical(specs[1], "b-commit", 295),
        }
    )
    monkeypatch.setattr(
        run_v2,
        "_selection_tree_object",
        lambda selection: "oid-a" if selection.commit == "a-commit" else "oid-b",
    )
    monkeypatch.setattr(run_v2, "_ref_tip", lambda repository, ref: "tip")
    row = run_v2.reconstruct_record(
        {"id": "q", "ts": 300, "cwd": "C:/plain", "git_branch": "main", "query": {"pattern": "x", "path": "C:/plain"}},
        _assignment("plain", "plain"),
        {"plain": entry},
        [300],
        history,
    )
    assert row["mode"] == "head_fallback"
    assert row["reason"] == "ambiguous_epoch_source"
    assert len(row["epoch_candidate_evidence"]) == 2


def test_equivalent_epoch_tree_objects_are_exact(monkeypatch):
    logical = _tree("plain", "C:/plain", "C:/repo-a")
    candidates = (
        EpochCandidate("a", "legacy", "C:/repo-a", "C:/repo-a/.git"),
        EpochCandidate("b", "desktop", "C:/repo-b", "C:/repo-b/.git", "plain"),
    )
    entry = TreeCatalogEntry(logical, "logical_toolsenabled_epoch", epoch_candidates=candidates)
    specs = [candidate.as_tree(logical) for candidate in candidates]
    history = FakeHistory(
        {
            specs[0].tree_id: _historical(specs[0], "a-commit", 290),
            specs[1].tree_id: _historical(specs[1], "b-commit", 295),
        }
    )
    monkeypatch.setattr(run_v2, "_selection_tree_object", lambda selection: "same-oid")
    monkeypatch.setattr(run_v2, "_ref_tip", lambda repository, ref: "tip")
    row = run_v2.reconstruct_record(
        {"id": "q", "ts": 300, "cwd": "C:/plain", "git_branch": "main", "query": {"pattern": "x", "path": "C:/plain"}},
        _assignment("plain", "plain"),
        {"plain": entry},
        [300],
        history,
    )
    assert row["mode"] == "historical_exact"
    assert row["exact"] is True
    assert row["commit"] == "b-commit"


def test_exact_missing_scope_retries_head_before_unscoring(monkeypatch, tmp_path):
    tree = _tree("target", "C:/target", "C:/repo")
    entry = TreeCatalogEntry(tree, "git")
    build = CatalogBuild(entries=(entry,), assignments=(_assignment("target", "target"),), record_counts=((300, 1),))
    initial = {
        "record_id": "q",
        "query_ts": 300.0,
        "assigned_target_tree_id": "target",
        "target_tree_id": "target",
        "logical_root": "C:/target",
        "mode": "historical_exact",
        "exact": True,
        "repository_root": "C:/repo",
        "repository_identity": "C:/repo/.git",
        "repository_relative_root": "",
        "commit": "exact-commit",
        "commit_ts": 290,
        "gap_seconds": 10,
        "resolved_ref": "refs/heads/main",
    }

    @contextlib.contextmanager
    def materializer(row, parent, timeout):
        yield tmp_path / str(row["commit"])

    monkeypatch.setattr(
        run_v2.arms,
        "scope_for_record",
        lambda record, logical_root, source_root: {
            "in_scope": True,
            "available": Path(source_root).name == "head-commit",
            "reason": None if Path(source_root).name == "head-commit" else "file_missing",
        },
    )
    final = run_v2.preflight_reconstructed_scopes(
        {"q": {"id": "q", "query": {"pattern": "x", "path": "C:/target"}}},
        [initial],
        build,
        cache=FakeHistory(),
        worktree_parent=tmp_path,
        materializer=materializer,
    )[0]
    assert final["mode"] == "head_fallback"
    assert final["reason"] == "exact_snapshot_scope_unavailable"
    assert final["commit"] == "head-commit"
    assert final["attempted_exact"]["scope_reason"] == "file_missing"


def test_exact_materialization_failure_never_turns_into_head_fallback(tmp_path):
    tree = _tree("target", "C:/target", "C:/repo")
    build = CatalogBuild(
        entries=(TreeCatalogEntry(tree, "git"),),
        assignments=(),
        record_counts=((300, 1),),
    )
    initial = {
        "record_id": "q",
        "query_ts": 300.0,
        "assigned_target_tree_id": "target",
        "target_tree_id": "target",
        "logical_root": "C:/target",
        "mode": "historical_exact",
        "exact": True,
        "repository_root": "C:/repo",
        "repository_identity": "C:/repo/.git",
        "repository_relative_root": "",
        "commit": "exact-commit",
        "commit_ts": 290,
        "gap_seconds": 10,
    }

    @contextlib.contextmanager
    def broken_materializer(row, parent, timeout):
        raise RuntimeError("checkout infrastructure failed")
        yield tmp_path  # pragma: no cover

    final = run_v2.preflight_reconstructed_scopes(
        {"q": {"id": "q", "query": {"pattern": "x"}}},
        [initial],
        build,
        cache=FakeHistory(),
        worktree_parent=tmp_path,
        materializer=broken_materializer,
    )[0]
    assert final["mode"] == "unscored"
    assert final["target_tree_id"] is None
    assert final["reason"].startswith("exact_snapshot_preflight_failed:")
    assert final["exclusion_detail"]["failure_kind"] == "infrastructure"


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def test_owned_stream_reuses_path_and_cleans_only_scratch(tmp_path):
    repo = tmp_path / "source"
    scratch = tmp_path / "scratch"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "value.txt").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "value.txt")
    _git(repo, "commit", "-qm", "one")
    first = _git(repo, "rev-parse", "HEAD")
    (repo / "value.txt").write_text("two\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "two")
    second = _git(repo, "rev-parse", "HEAD")
    exemplar = {"repository_root": str(repo), "commit": first}
    worktrees = scratch / "worktrees"
    with run_v2.owned_stream_worktree(exemplar, worktrees, timeout=30) as checkout:
        source_one = run_v2.checkout_owned_stream_state(checkout, first, "", scratch, timeout=30)
        assert (source_one / "value.txt").read_text(encoding="utf-8") == "one\n"
        (checkout / "untracked.tmp").write_text("remove me", encoding="utf-8")
        source_two = run_v2.checkout_owned_stream_state(checkout, second, "", scratch, timeout=30)
        assert source_two == source_one
        assert (source_two / "value.txt").read_text(encoding="utf-8") == "two\n"
        assert not (checkout / "untracked.tmp").exists()
    assert repo.exists()
    assert (repo / "value.txt").read_text(encoding="utf-8") == "two\n"


def test_unscore_mode_is_not_contradictory():
    row = run_v2._unscore(
        {"mode": "historical_exact", "exact": True, "commit": "abc", "gap_seconds": 4},
        "missing",
    )
    assert row["mode"] == "unscored"
    assert row["exact"] is False
    assert row["exclusion_detail"]["original_mode"] == "historical_exact"
    assert row["commit"] is None
    assert row["exclusion_detail"]["original_selection"]["commit"] == "abc"


def test_tiny_real_git_prepare_execute_resume_and_report_bundle(tmp_path):
    repo = tmp_path / "fixture-repo"
    eval_dir = tmp_path / "eval"
    output_dir = tmp_path / "output"
    scratch = tmp_path / "scratch"
    repo.mkdir()
    eval_dir.mkdir()
    output_dir.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "branch", "-M", "main")
    (repo / "src").mkdir()
    target = repo / "src" / "alpha.js"
    target.write_text("export const parseAlpha = true;\n", encoding="utf-8")
    _git(repo, "add", "src/alpha.js")
    _git(repo, "commit", "-qm", "fixture")
    commit_ts = int(_git(repo, "show", "-s", "--format=%ct", "HEAD"))
    record = {
        "id": "session:grep",
        "ts": commit_ts + 10,
        "agent": "session",
        "cwd": str(repo),
        "git_branch": "main",
        "query": {
            "pattern": "parseAlpha",
            "path": str(repo / "src"),
            "glob": None,
            "type": None,
            "output_mode": "files_with_matches",
            "-i": False,
            "head_limit": 20,
        },
        "returned_paths": [str(target)],
        "followed_by_read": [str(target)],
        "followed_by_grep": False,
        "seconds_to_next_action": 1.0,
        "result_bytes": 20,
    }
    for filename in run_v2.EVAL_FILENAMES.values():
        (eval_dir / filename).write_text(json.dumps(record) + "\n", encoding="utf-8")
    retention = {
        "complete": True,
        "schema_version": 1,
        "windows_seconds": [60, 300, 900],
        "diagnostics": {},
        "retention": {
            str(window): {
                "resolvable": 1,
                "all_unique_grep_calls": 1,
                "all_excluded": 0,
                "excluded_abandonment": 0,
                "excluded_missing_grep_result": 0,
                "excluded_unresolved_read_followup": 0,
                "positive_read": 1,
                "failure_next_grep": 0,
                "retention_rate": 1.0,
            }
            for window in run_v2.WINDOWS
        },
    }
    (eval_dir / "retention.json").write_text(
        json.dumps(retention), encoding="utf-8"
    )

    evalsets, records, provenance, exclusions, plan = run_v2.prepare_run(
        eval_dir, output_dir, scratch, git_timeout=30
    )
    assert len(provenance) == 1
    assert provenance[0]["target_tree_id"]
    runs_path = output_dir / "runs-v2.jsonl"
    run_rows, execution = run_v2.execute_runs(
        records,
        provenance,
        runs_path=runs_path,
        fingerprint=plan["fingerprint"],
        scratch_dir=scratch,
        git_timeout=30,
        progress_every=1,
    )
    assert len(run_rows) == 5
    assert {row["arm"] for row in run_rows} == set(run_v2.ALL_ARMS)
    resumed_rows, resumed = run_v2.execute_runs(
        records,
        provenance,
        runs_path=runs_path,
        fingerprint=plan["fingerprint"],
        scratch_dir=scratch,
        git_timeout=30,
        progress_every=1,
    )
    assert len(resumed_rows) == 5
    assert resumed["resumed_arm_rows"] == 5
    metrics_path, summary_path = run_v2.write_metrics_and_summary(
        evalsets,
        provenance,
        exclusions,
        resumed_rows,
        plan,
        output_dir=output_dir,
        execution_stats=execution,
    )
    bundle = report_v2.load_report_bundle(
        eval_dir=eval_dir,
        metrics_path=metrics_path,
        retention_path=eval_dir / "retention.json",
        catalog_path=output_dir / "tree-catalog-v2.json",
        summary_path=summary_path,
    )
    assert bundle.complete_run_ids == {record["id"]}
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["artifacts"]["retention"]["sha256"] == run_v2.sha256_file(
        eval_dir / "retention.json"
    )
    assert not list((scratch / "worktrees").glob("retrieval-v2-stream-*"))
    assert not list((scratch / "worktrees").glob("retrieval-v2-state-*"))


def test_file_valued_non_git_partial_control_is_windowed_truth_safe_and_resumable(tmp_path):
    current = tmp_path / "current"
    current.mkdir()
    target = current / "settings.json"
    sibling = current / "sibling.json"
    target.write_text('{"needle": true}\n', encoding="utf-8")
    sibling.write_text('{"needle": true}\n', encoding="utf-8")
    record = {
        "id": "partial",
        "cwd": str(current),
        "query": {
            "pattern": "needle",
            "path": str(target),
            "output_mode": "files_with_matches",
            "head_limit": 20,
        },
        "followed_by_read": [str(target), str(sibling)],
        "followed_by_grep": False,
    }
    provenance = [
        {
            "record_id": "partial",
            "windows_seconds": [300],
            "assigned_target_tree_id": "file-tree",
            "target_tree_id": None,
            "logical_root": str(target),
            "current_root": str(target),
            "partial_arms": ["ripgrep"],
            "mode": "non_git_current_fallback",
            "exact": False,
            "commit": None,
            "repository_identity": None,
            "repository_relative_root": None,
        }
    ]
    prepared = run_v2.preflight_partial_current_scopes({"partial": record}, provenance)
    assert prepared[0]["partial_source_root"] == str(current.resolve())
    assert prepared[0]["partial_logical_root"] == str(target.parent)
    runs_path = tmp_path / "runs-v2.jsonl"
    fingerprint = "a" * 64
    rows, _ = run_v2.execute_runs(
        {"partial": record},
        prepared,
        runs_path=runs_path,
        fingerprint=fingerprint,
        scratch_dir=tmp_path / "scratch",
        git_timeout=30,
        progress_every=1,
    )
    assert len(rows) == 1
    assert rows[0]["arm"] == "ripgrep"
    assert rows[0]["logical_root"] == str(target)
    assert rows[0]["execution_logical_root"] == str(target.parent)
    truth, outside = metrics_v2.truth_for_row(record, rows[0])
    assert truth == [metrics_v2.normalise_windows_path(str(target))]
    assert outside == 1
    counts = run_v2._partial_arm_rows_by_tree(rows, prepared)
    assert counts["300"] == {"file-tree": {"ripgrep": 1}}
    assert counts["60"] == {}
    assert counts["900"] == {}

    # A complete resumed partial pair must not touch a now-disappeared current
    # source.  The durable row remains auditable and no duplicate is added.
    target.unlink()
    resumed, stats = run_v2.execute_runs(
        {"partial": record},
        prepared,
        runs_path=runs_path,
        fingerprint=fingerprint,
        scratch_dir=tmp_path / "scratch",
        git_timeout=30,
        progress_every=1,
    )
    assert resumed == rows
    assert stats["resumed_arm_rows"] == 1
