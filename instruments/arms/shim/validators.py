from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from .gitops import ScratchRepository
from .schema import Side, Site
from .util import (
    FileState,
    ShimError,
    Snapshot,
    atomic_json,
    process_environment,
    run_process,
    sha256_bytes,
    snapshot_tree,
    tree_path,
)


@dataclasses.dataclass(frozen=True)
class ValidationConfig:
    python: Path
    click_compat_root: Path
    timeout_seconds: float = 120.0

    @classmethod
    def from_protocol(cls, project_root: Path) -> "ValidationConfig":
        protocol = json.loads(
            (project_root / "exploratory" / "arms" / "protocol.json").read_text(
                encoding="utf-8"
            )
        )
        python = Path(protocol["environment"]["python"])
        compat = project_root / protocol["environment"]["click_compat_root"]
        if not python.is_file():
            raise ShimError(f"frozen Python is absent: {python}")
        return cls(python.resolve(), compat.resolve(), float(protocol["environment"]["timeout_seconds"]))


def selected_manifest(snapshot: Snapshot, paths: Sequence[str]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for relative in sorted(set(paths)):
        state = snapshot.get(relative)
        if state is None:
            result[relative] = {"kind": "missing", "mode": None, "bytes": 0, "sha256": sha256_bytes(b"")}
        else:
            result[relative] = {
                "kind": state.kind,
                "mode": state.mode,
                "bytes": len(state.data),
                "sha256": state.sha256,
            }
    return result


def test_integrity(
    before: Snapshot, after: Snapshot, paths: Sequence[str]
) -> tuple[bool, list[dict[str, Any]]]:
    mismatches: list[dict[str, Any]] = []
    for relative in sorted(set(paths)):
        expected = before.get(relative)
        actual = after.get(relative)
        if expected == actual:
            continue
        mismatches.append(
            {
                "path": relative,
                "expected_kind": expected.kind if expected else "missing",
                "actual_kind": actual.kind if actual else "missing",
                "expected_mode": expected.mode if expected else None,
                "actual_mode": actual.mode if actual else None,
                "expected_sha256": sha256_bytes(expected.data if expected else b""),
                "actual_sha256": sha256_bytes(actual.data if actual else b""),
                "expected_bytes": len(expected.data) if expected else 0,
                "actual_bytes": len(actual.data) if actual else 0,
            }
        )
    return not mismatches, mismatches


def is_test_path(relative: str) -> bool:
    """Conservative cross-language classifier for the source-edit prohibition."""

    normalized = relative.replace("\\", "/").strip("/")
    parts = normalized.casefold().split("/")
    name = parts[-1] if parts else ""
    return bool(
        "tests" in parts
        or "test" in parts
        or ("src" in parts and "test" in parts)
        or name.startswith("test_")
        or name.endswith("_test.py")
        or name.endswith("_test.go")
        or name.endswith("test.java")
        or name.endswith("tests.java")
    )


class Validator:
    def __init__(self, *, project_root: Path, config: ValidationConfig) -> None:
        self.project_root = project_root
        self.config = config
        self._counter = 0

    def _environment(self, tree: Path, temp: Path, site: Site) -> dict[str, str]:
        env = process_environment()
        for name in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTEST_DEBUG_TEMPROOT", "PYTHONHOME"):
            env.pop(name, None)
        env.update(
            {
                "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONNOUSERSITE": "1",
                "PYTHONHASHSEED": "0",
                "TEMP": str(temp),
                "TMP": str(temp),
                "TMPDIR": str(temp),
            }
        )
        import_root = tree / "src" if (tree / "src").is_dir() else tree
        python_paths = [str(import_root.resolve())]
        if site.repo == "pallets/click":
            python_paths.append(str(self.config.click_compat_root))
        env["PYTHONPATH"] = os.pathsep.join(python_paths)
        return env

    def focal(
        self,
        *,
        tree: Path,
        site: Site,
        side: Side,
        artifact_root: Path,
        label: str,
    ) -> dict[str, Any]:
        self._counter += 1
        root = artifact_root / f"validate-{self._counter:03d}-{label}"
        root.mkdir(parents=True, exist_ok=False)
        temp = root / "temp"
        temp.mkdir()
        junit = root / "junit.xml"
        argv = [
            str(self.config.python),
            "-m",
            "pytest",
            "--color=no",
            "-q",
        ]
        if site.repo == "pygments/pygments":
            argv.append("--ignore=tests/contrast")
        argv.append(f"--junitxml={junit}")
        argv.extend(side.focal_targets)
        before = snapshot_tree(tree)
        result = run_process(
            argv,
            cwd=tree,
            env=self._environment(tree, temp, site),
            timeout_seconds=self.config.timeout_seconds,
            check=False,
        )
        after = snapshot_tree(tree)
        (root / "stdout.txt").write_bytes(result.stdout)
        (root / "stderr.txt").write_bytes(result.stderr)
        integrity_ok, integrity_mismatches = test_integrity(before, after, side.test_paths)
        record = {
            "label": label,
            "side": side.label,
            "targets": list(side.focal_targets),
            "argv": list(result.argv),
            "returncode": result.returncode,
            "timed_out": result.timed_out,
            "launch_error": result.launch_error,
            "actual_seconds": result.actual_seconds,
            "stdout_sha256": sha256_bytes(result.stdout),
            "stderr_sha256": sha256_bytes(result.stderr),
            "junit_present": junit.is_file(),
            "junit_sha256": __import__("hashlib").sha256(junit.read_bytes()).hexdigest() if junit.is_file() else None,
            "test_integrity_after_validation": integrity_ok,
            "test_integrity_mismatches": integrity_mismatches,
            "green": result.returncode == 0 and not result.timed_out and integrity_ok,
        }
        atomic_json(root / "result.json", record)
        return record

    def buildability(
        self,
        *,
        tree: Path,
        site: Site,
        artifact_root: Path,
        label: str,
    ) -> dict[str, Any]:
        """Run the frozen Python syntax/build screen without writing bytecode."""

        self._counter += 1
        root = artifact_root / f"validate-{self._counter:03d}-{label}"
        root.mkdir(parents=True, exist_ok=False)
        script = (
            "import pathlib,sys\n"
            "root=pathlib.Path(sys.argv[1])\n"
            "bad=[]\n"
            "for path in sorted(root.rglob('*.py')):\n"
            " rel=path.relative_to(root).as_posix()\n"
            " low=rel.casefold().split('/')\n"
            " name=low[-1]\n"
            " if '.git' in low or 'tests' in low or 'test' in low or name.startswith('test_') or name.endswith('_test.py'):\n"
            "  continue\n"
            " try:\n"
            "  compile(path.read_bytes(),rel,'exec')\n"
            " except Exception as exc:\n"
            "  bad.append((rel,type(exc).__name__,str(exc)))\n"
            "for row in bad:\n"
            " print(' | '.join(row),file=sys.stderr)\n"
            "raise SystemExit(1 if bad else 0)\n"
        )
        result = run_process(
            [str(self.config.python), "-c", script, str(tree.resolve())],
            cwd=tree,
            env=self._environment(tree, root, site),
            timeout_seconds=self.config.timeout_seconds,
            check=False,
        )
        (root / "stdout.txt").write_bytes(result.stdout)
        (root / "stderr.txt").write_bytes(result.stderr)
        record = {
            "oracle": "python-source-syntax-compile-v1",
            "scope": "all non-test *.py files in the final integrated tree",
            "returncode": result.returncode,
            "timed_out": result.timed_out,
            "launch_error": result.launch_error,
            "actual_seconds": result.actual_seconds,
            "stdout_sha256": sha256_bytes(result.stdout),
            "stderr_sha256": sha256_bytes(result.stderr),
            "buildable": result.returncode == 0 and not result.timed_out,
            "limitation": "syntax/build screen, not a repository full-suite oracle",
        }
        atomic_json(root / "result.json", record)
        return record

    def integration(
        self,
        *,
        scratch: ScratchRepository,
        site: Site,
        source_commit: str,
        artifact_root: Path,
        name_prefix: str,
    ) -> dict[str, Any]:
        # Correctness is evaluated on one final tree containing both frozen
        # test states, not on two side-specific counterfactual trees.
        tree = scratch.worktree(f"{name_prefix}-integrated", source_commit)
        apply_records: list[dict[str, Any]] = []
        for label in ("A", "B"):
            side = site.sides[label]
            ok, stdout, stderr = scratch.apply_patch(tree, side.test_patch)
            (artifact_root / f"{name_prefix}-integrated-{label}-apply.stdout").write_bytes(stdout)
            (artifact_root / f"{name_prefix}-integrated-{label}-apply.stderr").write_bytes(stderr)
            apply_records.append(
                {
                    "side": label,
                    "ok": ok,
                    "stdout_sha256": sha256_bytes(stdout),
                    "stderr_sha256": sha256_bytes(stderr),
                }
            )
            if not ok:
                return {
                    "sides": {},
                    "test_patch_application": apply_records,
                    "both_focal_green": False,
                    "test_files_byte_identical_on_final_tree": False,
                    "otherwise_buildable": False,
                    "buildability": None,
                    "correct": False,
                    "reason": f"test patch {label} did not apply to the final integrated tree",
                }
        expected = snapshot_tree(tree)
        protected = tuple(
            sorted(set(site.sides["A"].test_paths) | set(site.sides["B"].test_paths))
        )
        results: dict[str, Any] = {}
        for label in ("A", "B"):
            side = site.sides[label]
            focal = self.focal(
                tree=tree,
                site=site,
                side=side,
                artifact_root=artifact_root,
                label=f"{name_prefix}-integrated-{label}",
            )
            after = snapshot_tree(tree)
            unchanged, mismatches = test_integrity(expected, after, protected)
            focal["test_patch_applied"] = True
            focal["test_files_byte_identical"] = unchanged
            focal["test_file_mismatches"] = mismatches
            focal["green"] = bool(focal["green"] and unchanged)
            results[label] = focal
        final_after = snapshot_tree(tree)
        unchanged, final_mismatches = test_integrity(expected, final_after, protected)
        buildability = self.buildability(
            tree=tree,
            site=site,
            artifact_root=artifact_root,
            label=f"{name_prefix}-integrated-build",
        )
        both_green = bool(
            results.get("A", {}).get("green") and results.get("B", {}).get("green")
        )
        correct = bool(both_green and unchanged and buildability["buildable"])
        return {
            "sides": results,
            "test_patch_application": apply_records,
            "both_focal_green": both_green,
            "test_files_byte_identical_on_final_tree": unchanged,
            "test_file_mismatches": final_mismatches,
            "otherwise_buildable": bool(buildability["buildable"]),
            "buildability": buildability,
            "correct": correct,
        }
