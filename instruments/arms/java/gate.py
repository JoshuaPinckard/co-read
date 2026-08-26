"""Build and gate the Commons Lang Java arms-ladder instrument.

This is a fresh Java/Maven implementation of the repository's established
instrument method: freeze focal mapping, retain raw oracle evidence, normalize
test identities rather than timing, and restore perturbed sources by hash.
The protected corpus mirror is copied byte-for-byte before any Git object is
read; worktrees and lazy fetches belong only to that scratch copy.
"""

from __future__ import annotations

import argparse
import collections
import dataclasses
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[3]
CORPUS_PATH = PROJECT_ROOT / "corpus" / "conflicts" / "apache__commons-lang.jsonl"
MIRROR_PATH = PROJECT_ROOT / "corpus" / "_conflict_mirrors" / "apache__commons-lang"
TOOLS_ROOT = PROJECT_ROOT / "tools" / "java"
JAVAPARSER_JAR = TOOLS_ROOT / "javaparser-core-3.28.2.jar"
JAVAPARSER_SHA256 = "b5499a3b1c40b16c0671fabe478c9aafeab38160c6fde74a6c13f42d86716ecd"
PROVENANCE_PATH = TOOLS_ROOT / "PROVENANCE.json"
PERTURBER_SOURCE = Path(__file__).with_name("JavaPerturber.java")
PERTURBER_CLASSES = TOOLS_ROOT / "classes"
MAVEN_REPOSITORY_ROOT = PROJECT_ROOT / "tools" / "maven-repository"
MAVEN_SETTINGS = TOOLS_ROOT / "maven-settings.xml"
MAVEN_USER_HOME_ROOT = TOOLS_ROOT / "maven-user-home"
DEFAULT_OUTPUT = PROJECT_ROOT / "exploratory" / "arms"
EXPECTED_CORPUS_SHA256 = "aad01a6946c91cada8e6f47097b77f49f692339cd4cc9c343d665d8328db391c"
EXPECTED_SITE_COUNT = 19
PROCESS_TIMEOUT_SECONDS = 900.0
SUREFIRE_SOURCE_RE = re.compile(r"^(?:Test.*|.*Test|.*Tests|.*TestCase)\.java$")
JAVA_VERSION_RE = re.compile(r'(?:openjdk|java) version "([^"]+)"', re.IGNORECASE)
DETAIL_SPACE_RE = re.compile(r"\s+")


class GateError(RuntimeError):
    """Fail-closed gate or apparatus error."""


@dataclasses.dataclass(frozen=True)
class ProcessResult:
    argv: tuple[str, ...]
    returncode: int | None
    stdout: bytes
    stderr: bytes
    elapsed_seconds: float
    timed_out: bool
    timeout_termination: str | None = None


@dataclasses.dataclass(frozen=True)
class Jdk:
    home: Path
    major: int
    java_version: str
    javac_version: str
    java_sha256: str
    javac_sha256: str

    @property
    def label(self) -> str:
        first = self.java_version.splitlines()[0] if self.java_version.splitlines() else "unknown"
        return f"JDK {self.major}: {first}"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha512_file(path: Path) -> str:
    digest = hashlib.sha512()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_write(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
    temporary.write_bytes(value)
    os.replace(temporary, path)


def atomic_json(path: Path, value: Any) -> None:
    atomic_write(
        path,
        (json.dumps(value, indent=2, ensure_ascii=True, sort_keys=True) + "\n").encode("utf-8"),
    )


def decode(value: bytes) -> str:
    return value.decode("utf-8", errors="replace")


def portable_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(PROJECT_ROOT.resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


def run_process(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str] | None = None,
    timeout: float = PROCESS_TIMEOUT_SECONDS,
) -> ProcessResult:
    started = time.monotonic()
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    process = subprocess.Popen(
        list(argv),
        cwd=cwd,
        env=dict(env) if env is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        shell=False,
        creationflags=creationflags,
        start_new_session=os.name != "nt",
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        return ProcessResult(
            argv=tuple(str(item) for item in argv),
            returncode=process.returncode,
            stdout=stdout,
            stderr=stderr,
            elapsed_seconds=time.monotonic() - started,
            timed_out=False,
        )
    except subprocess.TimeoutExpired:
        termination = ""
        termination_failed = False
        if os.name == "nt":
            try:
                killed = subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    stdin=subprocess.DEVNULL,
                    shell=False,
                    timeout=30,
                    check=False,
                )
                termination = (
                    f"taskkill exit {killed.returncode}: "
                    + decode(killed.stdout + b"\n" + killed.stderr).strip()
                )
                termination_failed = killed.returncode != 0
            except (subprocess.TimeoutExpired, OSError) as exception:
                termination = f"taskkill failed: {type(exception).__name__}: {exception}"
                termination_failed = True
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
                termination = "SIGKILL sent to isolated process group"
            except ProcessLookupError:
                termination = "isolated process group had already exited"
        try:
            stdout, stderr = process.communicate(timeout=30)
        except subprocess.TimeoutExpired as exception:
            process.kill()
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired as reap_exception:
                raise GateError(
                    f"timed-out process root could not be reaped for PID {process.pid}; "
                    f"termination={termination}"
                ) from reap_exception
            raise GateError(
                f"timed-out process tree retained output pipes for PID {process.pid}; "
                f"termination={termination}"
            ) from exception
        if process.poll() is None:
            raise GateError(f"timed-out process tree remains alive for PID {process.pid}")
        if termination_failed:
            raise GateError(
                f"timed-out process tree termination was not verified for PID {process.pid}: "
                f"{termination}"
            )
        return ProcessResult(
            argv=tuple(str(item) for item in argv),
            returncode=process.returncode,
            stdout=stdout,
            stderr=stderr,
            elapsed_seconds=time.monotonic() - started,
            timed_out=True,
            timeout_termination=termination,
        )


def git_environment(*, allow_lazy_fetch: bool) -> dict[str, str]:
    environment = dict(os.environ)
    for key in list(environment):
        if key.startswith("GIT_"):
            environment.pop(key, None)
    environment.update(
        {
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "LANG": "C",
            "LC_ALL": "C",
            "TZ": "UTC",
        }
    )
    if not allow_lazy_fetch:
        environment["GIT_NO_LAZY_FETCH"] = "1"
    return environment


def run_git(
    repository: Path,
    arguments: Sequence[str],
    *,
    bare: bool,
    allow_lazy_fetch: bool,
    check: bool = True,
) -> ProcessResult:
    prefix = ["git", "--git-dir", str(repository)] if bare else ["git", "-C", str(repository)]
    result = run_process(
        [*prefix, "-c", "core.autocrlf=false", "-c", f"core.attributesFile={os.devnull}", *arguments],
        cwd=PROJECT_ROOT,
        env=git_environment(allow_lazy_fetch=allow_lazy_fetch),
    )
    if check and (result.timed_out or result.returncode != 0):
        raise GateError(
            f"git {' '.join(arguments)} failed ({result.returncode}): {decode(result.stderr)[-2000:]}"
        )
    return result


def load_sites() -> list[dict[str, Any]]:
    observed_hash = sha256_file(CORPUS_PATH)
    if observed_hash != EXPECTED_CORPUS_SHA256:
        raise GateError(
            f"corpus hash mismatch: expected {EXPECTED_CORPUS_SHA256}, observed {observed_hash}"
        )
    sites: list[dict[str, Any]] = []
    with CORPUS_PATH.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            row = json.loads(line)
            if row.get("evaluation_status") == "conflicted" and row.get(
                "both_sides_touched_tests"
            ) is True:
                row["corpus_line"] = line_number
                sites.append(row)
    if len(sites) != EXPECTED_SITE_COUNT:
        raise GateError(f"expected {EXPECTED_SITE_COUNT} Java sites, observed {len(sites)}")
    return sites


def mirror_snapshot(path: Path) -> dict[str, Any]:
    records: list[tuple[str, int, str]] = []
    total_bytes = 0
    for file in sorted((item for item in path.rglob("*") if item.is_file()), key=lambda item: item.as_posix()):
        relative = file.relative_to(path).as_posix()
        size = file.stat().st_size
        digest = sha256_file(file)
        records.append((relative, size, digest))
        total_bytes += size
    canonical = json.dumps(records, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    return {
        "file_count": len(records),
        "logical_bytes": total_bytes,
        "manifest_sha256": sha256_bytes(canonical),
    }


def verified_project_path(raw: str) -> Path:
    path = (PROJECT_ROOT / raw).resolve()
    if PROJECT_ROOT.resolve() not in path.parents and path != PROJECT_ROOT.resolve():
        raise GateError(f"provenance path escapes the project: {raw}")
    return path


def verify_zip_install(archive: Path, installed: Path) -> dict[str, Any]:
    archive_files: dict[str, zipfile.ZipInfo] = {}
    roots: set[str] = set()
    with zipfile.ZipFile(archive, "r") as handle:
        for info in handle.infolist():
            normalized = info.filename.replace("\\", "/")
            parts = [part for part in normalized.split("/") if part]
            if not parts:
                continue
            if normalized.startswith("/") or any(part in {".", ".."} for part in parts):
                raise GateError(f"unsafe ZIP entry in {archive}: {info.filename}")
            roots.add(parts[0])
            if info.is_dir():
                continue
            if len(parts) < 2:
                raise GateError(f"archive file is outside its single root: {info.filename}")
            relative = "/".join(parts[1:])
            if relative in archive_files:
                raise GateError(f"duplicate ZIP entry after root removal: {relative}")
            archive_files[relative] = info
        if len(roots) != 1:
            raise GateError(f"expected one archive root in {archive}, observed {sorted(roots)}")
        installed_files = {
            path.relative_to(installed).as_posix(): path
            for path in installed.rglob("*")
            if path.is_file()
        }
        if set(installed_files) != set(archive_files):
            missing = sorted(set(archive_files) - set(installed_files))[:10]
            extra = sorted(set(installed_files) - set(archive_files))[:10]
            raise GateError(
                f"installed tree differs from archive {archive.name}; missing={missing}, extra={extra}"
            )
        records: list[tuple[str, int, str]] = []
        for relative in sorted(archive_files):
            info = archive_files[relative]
            path = installed_files[relative]
            if path.stat().st_size != info.file_size:
                raise GateError(f"installed size mismatch: {path}")
            installed_hash = sha256_file(path)
            with handle.open(info, "r") as source:
                digest = hashlib.sha256()
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(block)
            if installed_hash != digest.hexdigest():
                raise GateError(f"installed bytes differ from archive: {path}")
            records.append((relative, info.file_size, installed_hash))
    canonical = json.dumps(records, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    return {
        "archive_root": next(iter(roots)),
        "file_count": len(records),
        "manifest_sha256": sha256_bytes(canonical),
        "exact_archive_match": True,
    }


def verify_toolchain_provenance(
    supplied_jdk_homes: Sequence[Path], supplied_maven: Path
) -> dict[str, Any]:
    if not PROVENANCE_PATH.is_file():
        raise GateError(f"tool provenance file is absent: {PROVENANCE_PATH}")
    provenance_bytes = PROVENANCE_PATH.read_bytes()
    provenance = json.loads(provenance_bytes)
    if provenance.get("schema_version") != 1 or not isinstance(provenance.get("artifacts"), list):
        raise GateError("unsupported tools/java/PROVENANCE.json schema")
    records: list[dict[str, Any]] = []
    expected_jdks: set[Path] = set()
    expected_maven: Path | None = None
    for artifact in provenance["artifacts"]:
        archive = verified_project_path(str(artifact["archive"]))
        if not archive.is_file():
            raise GateError(f"pinned artifact is absent: {archive}")
        observed_size = archive.stat().st_size
        observed_sha256 = sha256_file(archive)
        if observed_size != int(artifact["bytes"]) or observed_sha256 != artifact["sha256"]:
            raise GateError(f"pinned artifact mismatch: {archive}")
        record: dict[str, Any] = {
            "component": artifact["component"],
            "url": artifact["url"],
            "archive": portable_path(archive),
            "bytes": observed_size,
            "sha256": observed_sha256,
        }
        if "sha512" in artifact:
            observed_sha512 = sha512_file(archive)
            if observed_sha512 != artifact["sha512"]:
                raise GateError(f"pinned SHA-512 mismatch: {archive}")
            record["sha512"] = observed_sha512
            if "sha512_provenance" in artifact:
                record["sha512_provenance"] = artifact["sha512_provenance"]
        installed_raw = artifact.get("installed_at")
        if installed_raw is not None:
            installed = verified_project_path(str(installed_raw))
            if not installed.is_dir():
                raise GateError(f"pinned installation is absent: {installed}")
            record["installed_at"] = portable_path(installed)
            record["installed_tree"] = verify_zip_install(archive, installed)
            record["installed_snapshot_before"] = mirror_snapshot(installed)
            normalized = installed.as_posix().lower()
            if "/jdks/" in normalized:
                expected_jdks.add(installed)
            elif "apache-maven-" in installed.name:
                expected_maven = installed / "bin" / ("mvn.cmd" if os.name == "nt" else "mvn")
        records.append(record)
    supplied_jdks = {path.resolve() for path in supplied_jdk_homes}
    if supplied_jdks != expected_jdks:
        raise GateError(
            "--jdk-home must name exactly the project-local pinned installations; "
            f"expected={sorted(str(path) for path in expected_jdks)}, "
            f"observed={sorted(str(path) for path in supplied_jdks)}"
        )
    if expected_maven is None or supplied_maven.resolve() != expected_maven.resolve():
        raise GateError(
            f"--maven must name the project-local pinned launcher: {expected_maven}"
        )
    return {
        "path": portable_path(PROVENANCE_PATH),
        "sha256": sha256_bytes(provenance_bytes),
        "verified_before_run": True,
        "artifacts": records,
    }


def verify_toolchain_unchanged(record: Mapping[str, Any]) -> dict[str, Any]:
    after: list[dict[str, Any]] = []
    for artifact in record["artifacts"]:
        archive = (PROJECT_ROOT / artifact["archive"]).resolve()
        if archive.stat().st_size != artifact["bytes"] or sha256_file(archive) != artifact["sha256"]:
            raise GateError(f"pinned archive changed during gate: {archive}")
        observed: dict[str, Any] = {
            "archive": artifact["archive"],
            "sha256": artifact["sha256"],
        }
        if "installed_at" in artifact:
            installed = (PROJECT_ROOT / artifact["installed_at"]).resolve()
            snapshot = mirror_snapshot(installed)
            if snapshot != artifact["installed_snapshot_before"]:
                raise GateError(f"pinned installation changed during gate: {installed}")
            observed["installed_snapshot_after"] = snapshot
            observed["unchanged"] = True
        after.append(observed)
    return {"verified_after_run": True, "artifacts": after}


def prepare_scratch(source: Path, scratch_root: Path) -> Path:
    if scratch_root.exists():
        raise GateError(f"scratch root already exists; refusing to reuse it: {scratch_root}")
    scratch_root.mkdir(parents=True)
    owned_mirror = scratch_root / "commons-lang.git"
    print(f"COPY mirror -> {owned_mirror}", flush=True)
    shutil.copytree(source, owned_mirror, copy_function=shutil.copy2)
    return owned_mirror


def map_test_side(paths: Sequence[str]) -> dict[str, Any]:
    classes: list[dict[str, str]] = []
    helpers: list[str] = []
    non_java: list[str] = []
    for raw_path in sorted(set(str(item) for item in paths)):
        normalized = raw_path.replace("\\", "/")
        prefix = "src/test/java/"
        if not normalized.startswith(prefix) or not normalized.endswith(".java"):
            non_java.append(normalized)
            continue
        name = normalized.rsplit("/", 1)[-1]
        if not SUREFIRE_SOURCE_RE.fullmatch(name):
            helpers.append(normalized)
            continue
        relative_class = normalized[len(prefix) : -len(".java")]
        classes.append(
            {
                "path": normalized,
                "focal_node_id": relative_class.replace("/", "."),
                "selector": name[: -len(".java")],
            }
        )
    return {
        "test_side_files": sorted(set(str(item).replace("\\", "/") for item in paths)),
        "focal_classes": classes,
        "non_runnable_java_helpers": helpers,
        "non_java_test_artifacts": non_java,
    }


def map_site(site: Mapping[str, Any], tree_paths: set[str]) -> dict[str, Any]:
    sides: dict[str, Any] = {}
    for index, side_name in enumerate(("parent1", "parent2")):
        mapped = map_test_side(site["diffs"][side_name]["test_files"])
        mapped["parent"] = site["parents"][index]
        for item in mapped["focal_classes"]:
            item["exists_at_base"] = item["path"] in tree_paths
        sides[side_name] = mapped
    union_by_id: dict[str, dict[str, str]] = {}
    for side in sides.values():
        for item in side["focal_classes"]:
            union_by_id.setdefault(
                item["focal_node_id"],
                {
                    "path": item["path"],
                    "focal_node_id": item["focal_node_id"],
                    "selector": item["selector"],
                    "exists_at_base": item["exists_at_base"],
                },
            )
    classes = [union_by_id[key] for key in sorted(union_by_id)]
    selectors: dict[str, list[str]] = collections.defaultdict(list)
    for item in classes:
        selectors[item["selector"]].append(item["focal_node_id"])
    ambiguous = {key: value for key, value in selectors.items() if len(value) > 1}
    base_by_selector: dict[str, list[str]] = collections.defaultdict(list)
    for tree_path in sorted(tree_paths):
        normalized = tree_path.replace("\\", "/")
        if not normalized.startswith("src/test/java/") or not normalized.endswith(".java"):
            continue
        name = normalized.rsplit("/", 1)[-1]
        if not SUREFIRE_SOURCE_RE.fullmatch(name):
            continue
        focal_id = normalized[len("src/test/java/") : -len(".java")].replace("/", ".")
        base_by_selector[name[: -len(".java")]].append(focal_id)
    base_collisions = {
        selector: sorted(base_by_selector[selector])
        for selector in sorted(selectors)
        if len(base_by_selector.get(selector, [])) > 1
        or (
            len(base_by_selector.get(selector, [])) == 1
            and base_by_selector[selector][0] not in selectors[selector]
        )
    }
    missing = [item for item in classes if not item["exists_at_base"]]
    return {
        "sides": sides,
        "focal_classes": classes,
        "selectors": sorted(selectors),
        "missing_at_base": missing,
        "ambiguous_selectors": ambiguous,
        "base_selector_collisions": base_collisions,
    }


def materialize_worktree(owned_mirror: Path, worktrees: Path, base: str) -> Path:
    destination = worktrees / base[:12]
    if destination.exists():
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    print(f"WORKTREE {base[:12]}", flush=True)
    run_git(
        owned_mirror,
        ["worktree", "add", "--detach", "--force", str(destination), base],
        bare=True,
        allow_lazy_fetch=True,
    )
    actual = decode(
        run_git(destination, ["rev-parse", "HEAD"], bare=False, allow_lazy_fetch=True).stdout
    ).strip()
    if actual != base:
        raise GateError(f"worktree {destination} is {actual}, expected {base}")
    return destination


def tracked_worktree_snapshot(worktree: Path) -> dict[str, Any]:
    listed = run_git(
        worktree,
        ["ls-files", "-z"],
        bare=False,
        allow_lazy_fetch=False,
    ).stdout
    paths = sorted(decode(item) for item in listed.split(b"\0") if item)
    records: list[tuple[str, int, str]] = []
    for relative in paths:
        path = worktree / relative
        if not path.is_file():
            raise GateError(f"tracked path is not a regular file in fresh export: {path}")
        records.append((relative, path.stat().st_size, sha256_file(path)))
    canonical = json.dumps(records, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    return {
        "tracked_file_count": len(records),
        "tracked_manifest_sha256": sha256_bytes(canonical),
    }


def fresh_attempt_worktree(
    owned_mirror: Path,
    attempts_root: Path,
    base: str,
    label: str,
) -> tuple[Path, dict[str, Any]]:
    destination = attempts_root / base[:12] / local_name(label)
    if destination.exists():
        raise GateError(f"fresh attempt destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    run_git(
        owned_mirror,
        ["worktree", "add", "--detach", "--force", str(destination), base],
        bare=True,
        allow_lazy_fetch=True,
    )
    head = decode(
        run_git(destination, ["rev-parse", "HEAD"], bare=False, allow_lazy_fetch=False).stdout
    ).strip()
    status = decode(
        run_git(
            destination,
            ["status", "--porcelain=v1", "--untracked-files=all"],
            bare=False,
            allow_lazy_fetch=False,
        ).stdout
    )
    if head != base or status.strip():
        raise GateError(
            f"attempt is not a fresh untouched base export: {destination}; "
            f"head={head}, status={status!r}"
        )
    return destination, {
        "path": portable_path(destination),
        "base": base,
        "head_before": head,
        "clean_including_untracked_before": True,
        "tracked_before": tracked_worktree_snapshot(destination),
    }


def finish_attempt_worktree(worktree: Path, identity: Mapping[str, Any]) -> dict[str, Any]:
    head = decode(
        run_git(worktree, ["rev-parse", "HEAD"], bare=False, allow_lazy_fetch=False).stdout
    ).strip()
    status = decode(
        run_git(
            worktree,
            ["status", "--porcelain=v1", "--untracked-files=no"],
            bare=False,
            allow_lazy_fetch=False,
        ).stdout
    )
    tracked_after = tracked_worktree_snapshot(worktree)
    unchanged = (
        head == identity["base"]
        and not status.strip()
        and tracked_after == identity["tracked_before"]
    )
    return {
        **dict(identity),
        "head_after": head,
        "tracked_status_after": status,
        "tracked_after": tracked_after,
        "tracked_unchanged": unchanged,
    }


def extract_patches(
    owned_mirror: Path,
    site: Mapping[str, Any],
    mapped: Mapping[str, Any],
    output_root: Path,
) -> dict[str, dict[str, Any]]:
    merge = site["merge"]
    patch_root = output_root / "patches" / "apache__commons-lang" / merge
    patch_root.mkdir(parents=True, exist_ok=True)
    results: dict[str, dict[str, Any]] = {}
    for index, side_name in enumerate(("parent1", "parent2")):
        parent = site["parents"][index]
        base = site["merge_base"]
        names = run_git(
            owned_mirror,
            ["diff", "--name-only", "-z", "--no-renames", base, parent],
            bare=True,
            allow_lazy_fetch=True,
        ).stdout
        changed = [decode(item).replace("\\", "/") for item in names.split(b"\0") if item]
        test_paths = set(mapped["sides"][side_name]["test_side_files"])
        absent_from_diff = sorted(test_paths - set(changed))
        if absent_from_diff:
            raise GateError(
                f"frozen {side_name} test paths are absent from actual B-to-parent diff "
                f"for {merge}: {absent_from_diff}"
            )
        expected_changed_count = int(site["diffs"][side_name]["files"])
        if len(changed) != expected_changed_count:
            raise GateError(
                f"actual B-to-parent path count differs from frozen corpus for {merge} "
                f"{side_name}: expected {expected_changed_count}, observed {len(changed)}"
            )
        source_paths = sorted(path for path in changed if path not in test_paths)
        ordered_test_paths = sorted(path for path in changed if path in test_paths)

        def diff_for(paths: Sequence[str]) -> bytes:
            if not paths:
                return b""
            return run_git(
                owned_mirror,
                [
                    "diff",
                    "--binary",
                    "--full-index",
                    "--no-renames",
                    base,
                    parent,
                    "--",
                    *paths,
                ],
                bare=True,
                allow_lazy_fetch=True,
            ).stdout

        source_patch = patch_root / f"{side_name}-source.patch"
        test_patch = patch_root / f"{side_name}-test.patch"
        source_bytes = diff_for(source_paths)
        test_bytes = diff_for(ordered_test_paths)
        atomic_write(source_patch, source_bytes)
        atomic_write(test_patch, test_bytes)
        results[side_name] = {
            "source_patch": portable_path(source_patch),
            "source_patch_sha256": sha256_bytes(source_bytes),
            "source_patch_bytes": len(source_bytes),
            "test_patch": portable_path(test_patch),
            "test_patch_sha256": sha256_bytes(test_bytes),
            "test_patch_bytes": len(test_bytes),
            "frozen_test_paths_verified_in_diff": True,
            "changed_path_count": len(changed),
        }
    return results


def executable(home: Path, name: str) -> Path:
    suffix = ".exe" if os.name == "nt" else ""
    return home / "bin" / f"{name}{suffix}"


def inspect_jdk(home: Path) -> Jdk:
    resolved = home.resolve()
    java = executable(resolved, "java")
    javac = executable(resolved, "javac")
    if not java.is_file() or not javac.is_file():
        raise GateError(f"JDK home lacks java/javac: {resolved}")
    java_result = run_process([str(java), "-version"], cwd=PROJECT_ROOT, timeout=30)
    javac_result = run_process([str(javac), "-version"], cwd=PROJECT_ROOT, timeout=30)
    if java_result.returncode != 0 or javac_result.returncode != 0:
        raise GateError(f"JDK executables failed under {resolved}")
    java_text = (decode(java_result.stdout) + decode(java_result.stderr)).strip()
    javac_text = (decode(javac_result.stdout) + decode(javac_result.stderr)).strip()
    match = JAVA_VERSION_RE.search(java_text)
    if match is None:
        raise GateError(f"cannot parse Java version from: {java_text}")
    version = match.group(1)
    major = int(version.split(".")[1] if version.startswith("1.") else version.split(".")[0])
    return Jdk(
        home=resolved,
        major=major,
        java_version=java_text,
        javac_version=javac_text,
        java_sha256=sha256_file(java),
        javac_sha256=sha256_file(javac),
    )


def jdk_record(jdk: Jdk) -> dict[str, Any]:
    return {
        "major": jdk.major,
        "java_version": jdk.java_version,
        "javac_version": jdk.javac_version,
        "java_executable_sha256": jdk.java_sha256,
        "javac_executable_sha256": jdk.javac_sha256,
        "installation": portable_path(jdk.home),
    }


def tool_environment(jdk: Jdk) -> dict[str, str]:
    environment = dict(os.environ)
    for key in (
        "JAVA_TOOL_OPTIONS",
        "_JAVA_OPTIONS",
        "JDK_JAVA_OPTIONS",
        "CLASSPATH",
        "MAVEN_ARGS",
        "MAVEN_BASEDIR",
        "MAVEN_PROJECTBASEDIR",
        "MAVEN_CMD_LINE_ARGS",
        "M2_HOME",
        "MAVEN_HOME",
        "MAVEN_BATCH_ECHO",
        "MAVEN_BATCH_PAUSE",
    ):
        environment.pop(key, None)
    environment["JAVA_HOME"] = str(jdk.home)
    environment["PATH"] = str(jdk.home / "bin") + os.pathsep + environment.get("PATH", "")
    environment["MAVEN_OPTS"] = ""
    environment["MAVEN_SKIP_RC"] = "1"
    environment["LANG"] = "C"
    environment["LC_ALL"] = "C"
    environment["TZ"] = "UTC"
    return environment


def command_for_batch(executable_path: Path, arguments: Sequence[str]) -> list[str]:
    logical = [str(executable_path), *arguments]
    if os.name == "nt" and executable_path.suffix.lower() in {".cmd", ".bat"}:
        return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", *logical]
    return logical


def compile_perturber(jdk: Jdk) -> dict[str, Any]:
    if sha256_file(JAVAPARSER_JAR) != JAVAPARSER_SHA256:
        raise GateError("vendored JavaParser JAR hash mismatch")
    if PERTURBER_CLASSES.exists():
        resolved = PERTURBER_CLASSES.resolve()
        if TOOLS_ROOT.resolve() not in resolved.parents:
            raise GateError(f"unsafe classes directory: {resolved}")
        shutil.rmtree(PERTURBER_CLASSES)
    PERTURBER_CLASSES.mkdir(parents=True)
    javac = executable(jdk.home, "javac")
    result = run_process(
        [
            str(javac),
            "-encoding",
            "UTF-8",
            "-cp",
            str(JAVAPARSER_JAR),
            "-d",
            str(PERTURBER_CLASSES),
            str(PERTURBER_SOURCE),
        ],
        cwd=PROJECT_ROOT,
        env=tool_environment(jdk),
        timeout=120,
    )
    if result.timed_out or result.returncode != 0:
        raise GateError(f"JavaPerturber compilation failed: {decode(result.stderr)}")
    class_file = PERTURBER_CLASSES / "JavaPerturber.class"
    return {
        "source": portable_path(PERTURBER_SOURCE),
        "source_sha256": sha256_file(PERTURBER_SOURCE),
        "class_sha256": sha256_file(class_file),
        "compiler": jdk_record(jdk),
        "javaparser_jar": portable_path(JAVAPARSER_JAR),
        "javaparser_version": "3.28.2",
        "javaparser_sha256": JAVAPARSER_SHA256,
    }


def run_perturber(
    jdk: Jdk, command: str, file: Path, state: Path | None = None
) -> ProcessResult:
    classpath = os.pathsep.join((str(PERTURBER_CLASSES), str(JAVAPARSER_JAR)))
    arguments = [
        str(executable(jdk.home, "java")),
        "-cp",
        classpath,
        "JavaPerturber",
        command,
        "--file",
        str(file),
    ]
    if state is not None:
        arguments.extend(["--state", str(state)])
    return run_process(
        arguments,
        cwd=PROJECT_ROOT,
        env=tool_environment(jdk),
        timeout=120,
    )


def is_generated_candidate(path: Path) -> bool:
    lowered_segments = {segment.lower() for segment in path.parts}
    if lowered_segments.intersection(
        {"target", "build", "gen", "generated", "generated-sources", "autogenerated"}
    ):
        return True
    if path.name.lower().endswith(".generated.java"):
        return True
    header = path.read_bytes()[:8192].decode("utf-8", errors="replace").lower()
    return (
        "@generated" in header
        or "automatically generated" in header
        or ("generated" in header and "do not edit" in header)
    )


def roundtrip_gate(
    jdk: Jdk,
    worktree_by_base: Mapping[str, Path],
    sample_count: int,
    scratch_root: Path,
) -> dict[str, Any]:
    preferred_names = [
        "StringUtils.java",
        "ArrayUtils.java",
        "ObjectUtils.java",
        "ClassUtils.java",
        "SystemUtils.java",
        "Validate.java",
        "BooleanUtils.java",
        "CharUtils.java",
    ]
    candidate_by_base: dict[str, list[Path]] = {}
    for base, worktree in sorted(worktree_by_base.items()):
        candidates = [
            path
            for path in (worktree / "src" / "main" / "java").rglob("*.java")
            if path.name not in {"package-info.java", "module-info.java"}
            and not is_generated_candidate(path)
        ]
        candidates.sort(
            key=lambda path: (
                preferred_names.index(path.name)
                if path.name in preferred_names
                else len(preferred_names),
                path.relative_to(worktree).as_posix(),
            )
        )
        candidate_by_base[base] = candidates

    plans: dict[tuple[str, str], dict[str, Any]] = {}
    ineligible: list[dict[str, Any]] = []

    def plan_candidate(base: str, path: Path) -> dict[str, Any]:
        worktree = worktree_by_base[base]
        relative = path.relative_to(worktree).as_posix()
        key = (base, relative)
        if key in plans:
            return plans[key]
        result = run_perturber(jdk, "plan", path)
        if result.timed_out or result.returncode != 0:
            raise GateError(
                f"read-only perturbation planning failed for {base}/{relative}: "
                f"{decode(result.stderr).strip()}"
            )
        plan = json.loads(decode(result.stdout))
        if plan.get("event") != "plan" or not isinstance(
            plan.get("concreteMethodBodies"), int
        ):
            raise GateError(f"invalid perturbation plan response for {base}/{relative}")
        plans[key] = plan
        if not plan.get("eligible"):
            ineligible.append(
                {
                    "base": base,
                    "file": relative,
                    "reason": plan.get("reason", "ineligible"),
                    "concrete_method_bodies": plan["concreteMethodBodies"],
                }
            )
        return plan

    selected: list[tuple[str, Path]] = []
    selected_keys: set[tuple[str, str]] = set()
    for base in sorted(candidate_by_base):
        worktree = worktree_by_base[base]
        for path in candidate_by_base[base]:
            relative = path.relative_to(worktree).as_posix()
            plan = plan_candidate(base, path)
            if plan.get("eligible"):
                selected.append((base, path))
                selected_keys.add((base, relative))
                break

    remainder: list[tuple[str, Path]] = []
    for base, candidates in candidate_by_base.items():
        worktree = worktree_by_base[base]
        for path in candidates:
            relative = path.relative_to(worktree).as_posix()
            if (base, relative) not in selected_keys:
                remainder.append((base, path))
    remainder.sort(
        key=lambda item: (
            preferred_names.index(item[1].name)
            if item[1].name in preferred_names
            else len(preferred_names),
            item[0],
            item[1].relative_to(worktree_by_base[item[0]]).as_posix(),
        )
    )
    for base, path in remainder:
        if len(selected) >= sample_count:
            break
        relative = path.relative_to(worktree_by_base[base]).as_posix()
        if (base, relative) in selected_keys:
            continue
        plan = plan_candidate(base, path)
        if plan.get("eligible"):
            selected.append((base, path))
            selected_keys.add((base, relative))
    selected = selected[:sample_count]
    if len(selected) != sample_count:
        raise GateError(
            f"round-trip selection froze only {len(selected)} of {sample_count} required files"
        )

    rows: list[dict[str, Any]] = []
    state_root = scratch_root / "perturbation-state"
    for attempt_index, (base, file) in enumerate(selected, 1):
        worktree = worktree_by_base[base]
        relative = file.relative_to(worktree).as_posix()
        original = sha256_file(file)
        state = state_root / f"attempt-{attempt_index:03d}"
        plan = plans[(base, relative)]
        applied: dict[str, Any] | None = None
        observed_perturbed: str | None = None
        primary_error: BaseException | None = None
        restore_error: BaseException | None = None
        restored: dict[str, Any] | None = None
        try:
            apply_result = run_perturber(jdk, "apply", file, state)
            if apply_result.returncode != 0 or apply_result.timed_out:
                raise GateError(
                    f"frozen round-trip sample failed for {base}/{relative}: "
                    f"{decode(apply_result.stderr).strip()}"
                )
            applied = json.loads(decode(apply_result.stdout))
            observed_perturbed = sha256_file(file)
            if (
                observed_perturbed != applied.get("perturbedSha256")
                or observed_perturbed == original
            ):
                raise GateError(f"perturbed hash check failed: {file}")
            if applied.get("injectedMethodBodies") != plan["concreteMethodBodies"]:
                raise GateError(f"apply count differs from frozen plan: {file}")
        except BaseException as exception:
            primary_error = exception
        finally:
            current_hash = sha256_file(file)
            manifest_exists = (state / "manifest.properties").is_file()
            if observed_perturbed is not None or current_hash != original:
                if not manifest_exists:
                    restore_error = GateError(
                        f"source changed but no restoration manifest exists: {file}"
                    )
                else:
                    try:
                        restore_result = run_perturber(jdk, "restore", file, state)
                        if restore_result.returncode != 0 or restore_result.timed_out:
                            raise GateError(
                                f"restoration failed for {file}: "
                                f"{decode(restore_result.stderr).strip()}"
                            )
                        restored = json.loads(decode(restore_result.stdout))
                    except BaseException as exception:
                        restore_error = exception
            if sha256_file(file) != original and restore_error is None:
                restore_error = GateError(f"byte-exact round trip failed: {file}")
        if restore_error is not None:
            raise GateError(
                f"unconditional restoration failed for {base}/{relative}: {restore_error}; "
                f"primary error: {primary_error}"
            ) from restore_error
        if primary_error is not None:
            raise primary_error
        if applied is None or observed_perturbed is None or restored is None:
            raise GateError(f"incomplete successful round-trip record for {file}")
        restored_hash = sha256_file(file)
        if restored_hash != original or restored.get("originalSha256") != original:
            raise GateError(f"byte-exact round trip verification failed: {file}")
        rows.append(
            {
                "base": base,
                "file": relative,
                "original_sha256": original,
                "perturbed_sha256": observed_perturbed,
                "restored_sha256": restored_hash,
                "injected_method_bodies": applied["injectedMethodBodies"],
                "status": "passed",
            }
        )
        print(f"ROUNDTRIP {len(rows):02d}/{sample_count} {base[:12]} {relative}", flush=True)
    if len(rows) != sample_count:
        raise GateError(f"only {len(rows)} of {sample_count} required round trips succeeded")
    return {
        "rule": "read-only JavaParser plan excludes generated/package/module and zero-concrete-body sources; freeze one eligible preferred core source per base, then relative path/base fill; no apply-outcome replacement",
        "selection_frozen_before_apply": True,
        "planned_before_apply": len(plans),
        "ineligible_before_apply": ineligible,
        "required": sample_count,
        "passed": len(rows),
        "status": "passed",
        "samples": rows,
    }


def maven_environment(jdk: Jdk, user_home: Path) -> dict[str, str]:
    environment = tool_environment(jdk)
    environment["MAVEN_OPTS"] = f"-Duser.home={user_home.resolve()}"
    environment["MAVEN_SKIP_RC"] = "1"
    return environment


def maven_arguments(
    *, repository: Path, offline: bool, goal_arguments: Sequence[str]
) -> list[str]:
    arguments = ["-q"]
    if offline:
        arguments.append("-o")
    arguments.extend(
        [
            "-s",
            str(MAVEN_SETTINGS),
            "-gs",
            str(MAVEN_SETTINGS),
            f"-Dmaven.repo.local={repository}",
            *goal_arguments,
        ]
    )
    return arguments


def run_maven(
    maven: Path,
    jdk: Jdk,
    worktree: Path,
    *,
    repository: Path,
    user_home: Path,
    offline: bool,
    goal_arguments: Sequence[str],
) -> ProcessResult:
    arguments = maven_arguments(
        repository=repository, offline=offline, goal_arguments=goal_arguments
    )
    return run_process(
        command_for_batch(maven, arguments),
        cwd=worktree,
        env=maven_environment(jdk, user_home),
    )


def logical_maven_command(
    *, offline: bool, goal_arguments: Sequence[str]
) -> list[str]:
    command = ["mvn", "-q"]
    if offline:
        command.append("-o")
    command.extend(
        [
            "-s",
            "<project>/tools/java/maven-settings.xml",
            "-gs",
            "<project>/tools/java/maven-settings.xml",
            "-Dmaven.repo.local=<project>/tools/maven-repository",
            *goal_arguments,
        ]
    )
    return command


def safe_remove_reports(worktree: Path) -> None:
    root = worktree.resolve()
    for report_dir in worktree.rglob("surefire-reports"):
        resolved = report_dir.resolve()
        if resolved.name != "surefire-reports" or root not in resolved.parents:
            raise GateError(f"unsafe report cleanup target: {resolved}")
        if resolved.is_dir():
            shutil.rmtree(resolved)


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def normalize_detail(element: ET.Element) -> str:
    detail = element.attrib.get("type") or element.attrib.get("message") or (element.text or "")
    return DETAIL_SPACE_RE.sub(" ", detail).strip()


def collect_surefire(worktree: Path) -> dict[str, Any]:
    reports = sorted(worktree.rglob("target/surefire-reports/*.xml"))
    records: list[dict[str, str]] = []
    report_rows: list[dict[str, Any]] = []
    parse_errors: list[str] = []
    for report in reports:
        relative = report.relative_to(worktree).as_posix()
        report_rows.append(
            {"path": relative, "bytes": report.stat().st_size, "sha256": sha256_file(report)}
        )
        try:
            root = ET.parse(report).getroot()
        except (ET.ParseError, OSError) as exception:
            parse_errors.append(f"{relative}: {exception}")
            continue
        for testcase in (item for item in root.iter() if local_name(item.tag) == "testcase"):
            outcome = "pass"
            detail = ""
            for child in testcase:
                kind = local_name(child.tag)
                if kind in {"flakyFailure", "flakyError", "rerunFailure", "rerunError"}:
                    outcome = {
                        "flakyFailure": "flaky_failure",
                        "flakyError": "flaky_error",
                        "rerunFailure": "rerun_failure",
                        "rerunError": "rerun_error",
                    }[kind]
                    detail = normalize_detail(child)
                    break
                if kind == "error":
                    outcome = "error"
                    detail = normalize_detail(child)
                    break
                if kind == "failure":
                    outcome = "failure"
                    detail = normalize_detail(child)
                    break
                if kind == "skipped" and outcome == "pass":
                    outcome = "skipped"
                    detail = normalize_detail(child)
            records.append(
                {
                    "classname": testcase.attrib.get("classname", ""),
                    "name": testcase.attrib.get("name", ""),
                    "outcome": outcome,
                    "detail": detail,
                }
            )
    records.sort(key=lambda row: (row["classname"], row["name"], row["outcome"], row["detail"]))
    signature_records = [
        {"classname": row["classname"], "name": row["name"], "outcome": row["outcome"]}
        for row in records
    ]
    canonical = json.dumps(
        signature_records, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    counts = collections.Counter(row["outcome"] for row in records)
    return {
        "reports": report_rows,
        "parse_errors": parse_errors,
        "tests": records,
        "test_count": len(records),
        "outcome_counts": dict(sorted(counts.items())),
        "normalized_sha256": sha256_bytes(canonical),
        "normalization": "sorted (classname,name,outcome); timing and diagnostics omitted from signature; diagnostics retained as evidence",
    }


def archive_reports(worktree: Path, destination: Path) -> None:
    reports = sorted(worktree.rglob("target/surefire-reports/*.xml"))
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for report in reports:
            archive.write(report, report.relative_to(worktree).as_posix())


def save_run_evidence(
    evidence_root: Path,
    label: str,
    process: ProcessResult,
    logical_command: Sequence[str],
    normalized: Mapping[str, Any] | None,
    worktree: Path | None,
    source_export: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    evidence_root.mkdir(parents=True, exist_ok=True)
    stdout_path = evidence_root / f"{label}.stdout.bin"
    stderr_path = evidence_root / f"{label}.stderr.bin"
    atomic_write(stdout_path, process.stdout)
    atomic_write(stderr_path, process.stderr)
    record: dict[str, Any] = {
        "label": label,
        "command": list(logical_command),
        "returncode": process.returncode,
        "timed_out": process.timed_out,
        "timeout_termination": process.timeout_termination,
        "elapsed_seconds": round(process.elapsed_seconds, 6),
        "stdout": portable_path(stdout_path),
        "stdout_sha256": sha256_bytes(process.stdout),
        "stderr": portable_path(stderr_path),
        "stderr_sha256": sha256_bytes(process.stderr),
    }
    if source_export is not None:
        record["source_export"] = dict(source_export)
    if normalized is not None:
        normalized_path = evidence_root / f"{label}.normalized.json"
        atomic_json(normalized_path, normalized)
        record["normalized"] = {
            "path": portable_path(normalized_path),
            "sha256": sha256_file(normalized_path),
            "test_count": normalized["test_count"],
            "outcome_counts": normalized["outcome_counts"],
            "normalized_sha256": normalized["normalized_sha256"],
            "parse_errors": normalized["parse_errors"],
        }
        if worktree is not None:
            archive_path = evidence_root / f"{label}.reports.zip"
            archive_reports(worktree, archive_path)
            record["reports_archive"] = portable_path(archive_path)
            record["reports_archive_sha256"] = sha256_file(archive_path)
    atomic_json(evidence_root / f"{label}.process.json", record)
    return record


def focal_scope(
    focal_ids: Sequence[str], tests: Sequence[Mapping[str, str]]
) -> dict[str, list[str]]:
    def matching_focal(classname: str) -> str | None:
        for focal in focal_ids:
            if classname == focal or classname.startswith(focal + "$"):
                return focal
        return None

    classnames = {row["classname"] for row in tests}
    missing: list[str] = []
    no_passing: list[str] = []
    for focal in focal_ids:
        if not any(name == focal or name.startswith(focal + "$") for name in classnames):
            missing.append(focal)
        elif not any(
            row["outcome"] == "pass"
            and (row["classname"] == focal or row["classname"].startswith(focal + "$"))
            for row in tests
        ):
            no_passing.append(focal)
    unexpected = sorted(
        {row["classname"] for row in tests if matching_focal(row["classname"]) is None}
    )
    return {
        "unobserved_focal_classes": missing,
        "focal_classes_without_pass": no_passing,
        "unexpected_test_classes": unexpected,
    }


def compile_base(
    base: str,
    owned_mirror: Path,
    attempts_root: Path,
    jdks: Sequence[Jdk],
    maven: Path,
    repository: Path,
    user_home: Path,
    evidence_root: Path,
) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []
    goal = ["-DskipTests", "test"]
    for attempt_number, jdk in enumerate(jdks, 1):
        print(f"COMPILE {base[:12]} JDK {jdk.major}", flush=True)
        worktree, identity = fresh_attempt_worktree(
            owned_mirror,
            attempts_root,
            base,
            f"c{attempt_number:02d}j{jdk.major}",
        )
        result = run_maven(
            maven,
            jdk,
            worktree,
            repository=repository,
            user_home=user_home,
            offline=False,
            goal_arguments=goal,
        )
        finished = finish_attempt_worktree(worktree, identity)
        evidence = save_run_evidence(
            evidence_root,
            f"compile-{attempt_number:02d}-jdk-{jdk.major}",
            result,
            logical_maven_command(offline=False, goal_arguments=goal),
            None,
            None,
            finished,
        )
        attempts.append({"jdk": jdk_record(jdk), "process": evidence})
        if not result.timed_out and result.returncode == 0 and finished["tracked_unchanged"]:
            return {"status": "passed", "selected_jdk": jdk_record(jdk), "attempts": attempts}
    return {"status": "failed", "selected_jdk": None, "attempts": attempts}


def match_jdk(record: Mapping[str, Any], jdks: Sequence[Jdk]) -> Jdk:
    for jdk in jdks:
        if jdk.java_sha256 == record.get("java_executable_sha256"):
            return jdk
    raise GateError("selected JDK is not present in the current inventory")


def focal_gate(
    site: Mapping[str, Any],
    mapped: Mapping[str, Any],
    owned_mirror: Path,
    attempts_root: Path,
    jdk: Jdk,
    maven: Path,
    repository: Path,
    user_home: Path,
    evidence_root: Path,
) -> dict[str, Any]:
    selectors = mapped["selectors"]
    selector_value = ",".join(selectors)
    goal = ["test", f"-Dtest={selector_value}", "-DfailIfNoTests=false"]
    focal_ids = [item["focal_node_id"] for item in mapped["focal_classes"]]

    print(f"WARMUP {site['merge'][:12]} {len(selectors)} classes JDK {jdk.major}", flush=True)
    worktree, identity = fresh_attempt_worktree(
        owned_mirror, attempts_root, site["merge_base"], "warm"
    )
    warmup_process = run_maven(
        maven,
        jdk,
        worktree,
        repository=repository,
        user_home=user_home,
        offline=False,
        goal_arguments=goal,
    )
    warmup_normalized = collect_surefire(worktree)
    warmup_finished = finish_attempt_worktree(worktree, identity)
    warmup = save_run_evidence(
        evidence_root,
        "warmup-online",
        warmup_process,
        logical_maven_command(offline=False, goal_arguments=goal),
        warmup_normalized,
        worktree,
        warmup_finished,
    )
    if warmup_process.timed_out or (
        warmup_process.returncode != 0 and not warmup_normalized["reports"]
    ) or not warmup_finished["tracked_unchanged"]:
        warmup_reasons: list[str] = []
        if warmup_process.timed_out:
            warmup_reasons.append("online focal warm-up timed out")
        if warmup_process.returncode != 0 and not warmup_normalized["reports"]:
            warmup_reasons.append(
                "online focal warm-up failed before producing Surefire reports"
            )
        if not warmup_finished["tracked_unchanged"]:
            warmup_reasons.append("online focal warm-up changed a tracked base file")
        return {
            "status": "failed",
            "reason": "; ".join(warmup_reasons),
            "selectors": selectors,
            "warmup": warmup,
            "runs": [],
        }

    runs: list[dict[str, Any]] = []
    for run_number in range(1, 6):
        print(f"FOCAL {site['merge'][:12]} run {run_number}/5 JDK {jdk.major}", flush=True)
        worktree, identity = fresh_attempt_worktree(
            owned_mirror,
            attempts_root,
            site["merge_base"],
            f"r{run_number}",
        )
        process = run_maven(
            maven,
            jdk,
            worktree,
            repository=repository,
            user_home=user_home,
            offline=True,
            goal_arguments=goal,
        )
        normalized = collect_surefire(worktree)
        scope = focal_scope(focal_ids, normalized["tests"])
        finished = finish_attempt_worktree(worktree, identity)
        evidence = save_run_evidence(
            evidence_root,
            f"run-{run_number}",
            process,
            logical_maven_command(offline=True, goal_arguments=goal),
            normalized,
            worktree,
            finished,
        )
        runs.append(
            {
                "run": run_number,
                "process": evidence,
                "normalized_sha256": normalized["normalized_sha256"],
                "test_count": normalized["test_count"],
                "outcome_counts": normalized["outcome_counts"],
                **scope,
                "parse_errors": normalized["parse_errors"],
                "tracked_source_unchanged": finished["tracked_unchanged"],
            }
        )

    signatures = {run["normalized_sha256"] for run in runs}
    reasons: list[str] = []
    if any(run["process"]["timed_out"] for run in runs):
        reasons.append("at least one offline focal run timed out")
    if any(run["process"]["returncode"] != 0 for run in runs):
        reasons.append("at least one offline focal run was not green")
    if any(run["test_count"] == 0 for run in runs):
        reasons.append("at least one offline run produced zero testcases")
    if any(run["parse_errors"] for run in runs):
        reasons.append("at least one Surefire report could not be parsed")
    if any(run["unobserved_focal_classes"] for run in runs):
        reasons.append("at least one mapped focal class produced no testcase record")
    if any(run["focal_classes_without_pass"] for run in runs):
        reasons.append("at least one mapped focal class produced no passing testcase")
    if any(run["unexpected_test_classes"] for run in runs):
        reasons.append("at least one offline run executed a non-focal test class")
    if any(not run["tracked_source_unchanged"] for run in runs):
        reasons.append("at least one fresh base export changed a tracked file")
    if len(signatures) != 1:
        reasons.append("five normalized per-test signatures were not identical")
    if any(
        any(
            run["outcome_counts"].get(outcome, 0)
            for outcome in (
                "failure",
                "error",
                "flaky_failure",
                "flaky_error",
                "rerun_failure",
                "rerun_error",
            )
        )
        for run in runs
    ):
        reasons.append("focal baseline contained failure/error/rerun-flaky outcomes")
    status = "passed" if not reasons else "failed"
    return {
        "status": status,
        "reason": "five offline normalized focal runs were identical and green"
        if status == "passed"
        else "; ".join(reasons),
        "selectors": selectors,
        "warmup": warmup,
        "runs": runs,
        "signature_sha256": runs[0]["normalized_sha256"] if len(signatures) == 1 else None,
    }


def inspect_maven(maven: Path, jdk: Jdk, user_home: Path) -> dict[str, Any]:
    result = run_process(
        command_for_batch(maven, ["-version"]),
        cwd=PROJECT_ROOT,
        env=maven_environment(jdk, user_home),
        timeout=30,
    )
    if result.returncode != 0 or result.timed_out:
        raise GateError(f"Maven failed: {decode(result.stderr)}")
    version = (decode(result.stdout) + decode(result.stderr)).strip()
    record: dict[str, Any] = {
        "version": version,
        "launcher": portable_path(maven),
        "launcher_sha256": sha256_file(maven),
    }
    core_jars = sorted(maven.resolve().parents[1].glob("lib/maven-core-*.jar"))
    if core_jars:
        record["core_jar"] = portable_path(core_jars[0])
        record["core_jar_sha256"] = sha256_file(core_jars[0])
    return record


def summarize_failure(process_record: Mapping[str, Any]) -> str:
    stderr_path = PROJECT_ROOT / process_record["stderr"]
    stdout_path = PROJECT_ROOT / process_record["stdout"]
    combined = decode(stdout_path.read_bytes() + b"\n" + stderr_path.read_bytes())
    lines = [line.strip() for line in combined.splitlines() if line.strip()]
    return " | ".join(lines[-4:])[:1000]


def write_sites_json(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    projected = [
        {
            "repo": row["repo"],
            "merge": row["merge"],
            "base": row["base"],
            "sides": {
                side_name: {
                    "parent": row["sides"][side_name]["parent"],
                    "source_patch": row["sides"][side_name]["source_patch"],
                    "test_patch": row["sides"][side_name]["test_patch"],
                    "focal_node_ids": row["sides"][side_name]["focal_node_ids"],
                }
                for side_name in ("parent1", "parent2")
            },
            "verdict": row["verdict"],
        }
        for row in rows
    ]
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(existing, list):
            raise GateError(f"existing sites-java container is not a list: {path}")
        incoming = {item["merge"] for item in projected}
        duplicates = sorted(
            row.get("merge")
            for row in existing
            if isinstance(row, dict)
            and row.get("repo") == "apache/commons-lang"
            and row.get("merge") in incoming
        )
        if duplicates:
            raise GateError(
                f"refusing to overwrite existing sites-java rows: {duplicates}"
            )
        value = [*existing, *projected]
    else:
        value = projected
    atomic_json(path, value)


def markdown_escape(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", "<br>")


def write_markdown(path: Path, report: Mapping[str, Any]) -> None:
    rows = report["sites"]
    passed = sum(row["verdict"] == "passed" for row in rows)
    lines: list[str] = [f"Gate verdict — {passed} / {EXPECTED_SITE_COUNT} sites passed.", ""]
    lines.extend(
        [
            "# Java arms runner",
            "",
            "This is a Phase 0 runner gate, not the later side-patch discrimination check. A passing row means its merge base compiled, every mapped focal class existed and executed, and five offline Surefire runs had one identical green per-test signature.",
            "",
            "## Environment and pinned tools",
            "",
            f"- Corpus: `{report['corpus']['path']}`, SHA-256 `{report['corpus']['sha256']}`; selection yielded exactly 19 both-tests conflicted rows.",
            f"- Tool provenance: `{report['toolchain_provenance']['path']}`, SHA-256 `{report['toolchain_provenance']['sha256']}`. Every downloaded archive was checked against its pinned size/SHA-256 (and SHA-512 where recorded), and every installed Maven/JDK file was checked byte-for-byte against its ZIP before execution; archive and installed-tree snapshots were unchanged afterward.",
            f"- JavaParser: `com.github.javaparser:javaparser-core:3.28.2`, `{report['perturber']['javaparser_jar']}`, SHA-256 `{report['perturber']['javaparser_sha256']}`.",
            f"- Maven: `{report['maven']['version'].splitlines()[0]}`, launcher SHA-256 `{report['maven']['launcher_sha256']}`, core JAR SHA-256 `{report['maven'].get('core_jar_sha256', 'not found')}`; fresh local repository `{report['maven_repository']['path']}` (post-run manifest `{report['maven_repository']['snapshot_after']['manifest_sha256']}`).",
            f"- Maven settings: `{report['maven_settings']['path']}`, SHA-256 `{report['maven_settings']['sha256']}`. `MAVEN_SKIP_RC=1`; Maven/JDK injection variables were removed, and Java `user.home` was a fresh project-local directory `{report['maven_user_home']['path']}`.",
            "- Maven used the repository POM's Surefire version/configuration unchanged. The only test-selection properties were `-Dtest=...` and `-DfailIfNoTests=false`; one online focal run used its own fresh base worktree to warm the pinned repository, and each of the five scored runs used another fresh base worktree plus `-o`.",
            "",
            "JDKs were tried newest-first (25, 11, then 8), with a fresh untouched base worktree for every attempt. Each Maven compile-check exit and log is retained without guessing its cause; a site is rejected only if no installed pinned JDK passes the compile check.",
            "",
            "| JDK major | Exact runtime | Exact compiler | java.exe SHA-256 | javac.exe SHA-256 | Installed archive SHA-256 |",
            "|---:|---|---|---|---|---|",
        ]
    )
    artifact_by_install = {
        artifact.get("installed_at"): artifact
        for artifact in report["toolchain_provenance"]["artifacts"]
        if artifact.get("installed_at")
    }
    for jdk in report["jdks"]:
        archive = artifact_by_install[jdk["installation"]]
        lines.append(
            f"| {jdk['major']} | {markdown_escape(jdk['java_version'].splitlines()[0])} | {markdown_escape(jdk['javac_version'].splitlines()[0])} | `{jdk['java_executable_sha256']}` | `{jdk['javac_executable_sha256']}` | `{archive['sha256']}` |"
        )

    lines.extend(
        [
            "",
            "## Perturbation operator and round-trip gate",
            "",
            "For one explicit, non-generated `.java` target, JavaParser identifies every `MethodDeclaration` with a body and inserts `if (true) throw new RuntimeException(\"perturbed\");` at statement index zero. Constructors are excluded (constructors are not method declarations, and an explicit `this(...)`/`super(...)` must remain first). Static and instance initializer blocks are excluded. Abstract, native, and body-less interface methods are excluded; interface default/static/private methods with bodies are included. Methods declared inside anonymous/local classes are included; lambda bodies are not method declarations and are excluded.",
            "",
            "Generated sources are refused when the filename ends `.generated.java`, a path segment is `target`, `build`, `gen`, `generated`, `generated-sources`, or `autogenerated`, or the first 8,192 bytes contain `@generated`, `automatically generated`, or both `generated` and `do not edit` (case-insensitive). Candidate sampling also excludes `package-info.java`, `module-info.java`, and sources whose read-only JavaParser plan finds zero concrete method bodies. A planning parse error fails the apparatus; it is not an eligibility exclusion.",
            "",
            f"The read-only plan froze 20 eligible files before any apply outcome (`{report['roundtrip']['planned_before_apply']}` files planned; `{len(report['roundtrip']['ineligible_before_apply'])}` zero-body files declared ineligible). Before mutation, the operator stores the exact original bytes plus SHA-256 in a dedicated state directory. Its Java write path performs a hash-guarded rollback after any post-write error, and the scored Python path invokes restore in `finally`. Restore refuses an unexpected live hash, writes only saved bytes, and verifies the restored hash. All 20 frozen samples passed:",
            "",
            "| # | Base | File | Methods | Original/restored SHA-256 |",
            "|---:|---|---|---:|---|",
        ]
    )
    for index, sample in enumerate(report["roundtrip"]["samples"], 1):
        lines.append(
            f"| {index} | `{sample['base'][:12]}` | `{markdown_escape(sample['file'])}` | {sample['injected_method_bodies']} | `{sample['original_sha256']}` |"
        )

    lines.extend(
        [
            "",
            "## Focal mapping rule",
            "",
            "For each parent side independently, start from that corpus row's frozen `diffs.parentN.test_files`, and first require every frozen path to occur in the actual B-to-parent diff (the actual changed-path count must also equal the corpus count). Retain only paths under `src/test/java/` whose filename matches Surefire's conventional `Test*.java`, `*Test.java`, `*Tests.java`, or `*TestCase.java`; record every other test-side helper instead of silently treating it as runnable. The path below `src/test/java/` is the focal node ID (slashes become dots), while the filename stem is the `-Dtest` selector for compatibility with old Surefire releases. Duplicate stems across packages reject as ambiguous. The scored command uses the union of both sides' selectors, and any mapped source absent at base rejects the site.",
            "",
            "Before execution, every selected filename stem is checked against every conventional test source in the base tree; any same-stem package collision rejects. Nested test classes are not emitted as `$...` selectors: selecting the enclosing source class lets the repository's JUnit provider discover its nested cases. Parameterized classes/methods are likewise selected once; all parameter invocations and display names appearing in Surefire XML remain distinct normalized testcase records. XML may name only an exact focal class or its `$Nested` binary form. Unexpected classes, zero reports, a missing mapped class, a class with no passing testcase, or Surefire rerun/flaky elements reject.",
            "",
            "Worked examples:",
            "",
            "1. Site `640953167adf`: `src/test/java/org/apache/commons/lang3/reflect/TypeUtilsTest.java` maps to focal ID `org.apache.commons.lang3.reflect.TypeUtilsTest` and selector `TypeUtilsTest`.",
            "2. Site `4a882e76d9c9`: `FastDatePrinterTimeZonesTest.java` is JUnit 4 parameterized at the base. It contributes one selector, `FastDatePrinterTimeZonesTest`; each parameter row remains a separate XML identity.",
            "3. Site `ee87df847299`: `ValidateTest.java` contains only JUnit 5 `@Nested` cases. It contributes the outer selector `ValidateTest`; an XML classname beginning `org.apache.commons.lang3.ValidateTest$` would satisfy that mapping, while zero such records rejects rather than silently dropping the class.",
            "",
            "The five-run signature is sorted `(classname, name, outcome)`. XML order, timing, and diagnostic text are ignored; diagnostics remain in raw normalized evidence. Pass, skip, failure, error, and rerun/flaky outcomes remain distinct.",
            "",
            "## Per-site gate",
            "",
            "| # | Merge / base | JDK used | Focal classes | Five-run result | Verdict | Reason |",
            "|---:|---|---|---|---|---|---|",
        ]
    )
    for index, row in enumerate(rows, 1):
        jdk = row["runner_gate"].get("jdk")
        jdk_text = f"{jdk['major']}" if isinstance(jdk, dict) else "none"
        classes = ", ".join(
            item["focal_node_id"]
            for item in row["runner_gate"]["mapping"]["focal_classes"]
        )
        focal = row["runner_gate"].get("focal_gate")
        if focal and focal.get("runs"):
            hashes = [run["normalized_sha256"][:12] for run in focal["runs"]]
            counts = [run["test_count"] for run in focal["runs"]]
            identical = len(set(hashes)) == 1
            green = sum(
                run["process"]["returncode"] == 0
                and not run["process"]["timed_out"]
                for run in focal["runs"]
            )
            five = (
                f"{green}/5 Maven-green; identical={str(identical).lower()}; "
                f"hashes {','.join(hashes)}; tests {','.join(str(value) for value in counts)}"
            )
        else:
            five = "not run"
        lines.append(
            f"| {index} | `{row['merge'][:12]}` / `{row['base'][:12]}` | {jdk_text} | {markdown_escape(classes)} | {markdown_escape(five)} | **{row['verdict'].upper()}** | {markdown_escape(row['reason'])} |"
        )

    lines.extend(
        [
            "",
            "All raw Maven stdout/stderr, normalized JSON, and compressed Surefire XML are retained under the run-specific directory below `exploratory/arms/java-gates/`; each full report row points to its evidence. Base-to-parent source/test patches are under `exploratory/arms/patches/apache__commons-lang/`. `sites-java.json` follows the frozen harness field contract: repo, merge, base, two per-side parent/patch/focal-ID records, and verdict. Scratch worktrees belong to the recorded external scratch root, not the corpus mirror.",
            "",
            "## Claims that could NOT be verified",
            "",
            "- This gate does not prove either parent's test patch discriminates its source patch from the base. That red/green site-validation step is a separate Phase 0 result.",
            "- Twenty byte-exact round trips do not prove JavaParser can parse every historical or future Commons Lang source file, nor that every injected method is reached by a focal test.",
            "- Five identical runs establish only short-run determinism under this machine, toolchain, dependency cache, locale, and operating system.",
            "- A green focal subset does not establish full-suite health or cross-JDK equivalence; the full suite was intentionally not substituted for the focal oracle.",
            "- No concrete `exploratory/arms/sites.json` artifact existed at gate construction time, so byte-for-byte schema parity could not be checked; `sites-java.json` uses the exact row fields guaranteed by the frozen site-validation prompt.",
            "- The local archive hashes prove exactly which bytes ran, but upstream checksum provenance could be independently verified only for Maven's official SHA-512 sidecar; the JavaParser and Temurin SHA-256 values are locally pinned acquisition records.",
            "- The corpus mirror is not byte-pristine relative to lane start: two read-only inventory `git show` calls auto-hydrated two small promisor packs before the partial-clone hazard was recognized. Refs and commit identities did not change; all scored work used an independent byte copy made afterward, and that source snapshot remained unchanged during the scored run.",
            "",
            "## What would change this verdict",
            "",
            "- A different normalized outcome in any repeat would reject the affected site as flaky.",
            "- A restored-file hash mismatch, a changed tracked worktree, a JavaParser/JDK/Maven artifact hash mismatch, or a source-mirror snapshot change during the scored run would invalidate the instrument gate.",
            "- Completing the independent per-side red/green discrimination checks could remove currently passing sites from the eventual arms population; it cannot retroactively make a failed runner gate pass.",
            "- Repeating on another OS/toolchain and obtaining the same signatures would strengthen portability; disagreement would narrow the environment claim.",
            "- Reconstructing the task-owned mirror from its frozen upstream identity would be required for a byte-pristine corpus-mirror claim.",
            "- A newly discovered same-stem test, unexpected Surefire XML classname, skipped-only focal class, or rerun/flaky element would reject the affected row under the fail-closed oracle.",
            "",
            "## Confidence by claim",
            "",
            "| Claim | Confidence | Reason |",
            "|---|---|---|",
            "| The candidate census is exactly 19 | High | The input JSONL hash is pinned, the predicate is explicit, and all full merge/base/parent identities are retained. |",
            "| The recorded toolchain bytes are exact | High for local identity; medium for upstream origin | Every archive hash and every installed file was checked before execution and snapshotted afterward; only Maven also has an independently recorded official checksum sidecar. |",
            "| A selected base passed its Maven compile check under the recorded JDK | High, environment-conditional | Every JDK attempt used a fresh base worktree and retained exit/log evidence; the claim is about this Maven/JDK/cache/OS, not universal buildability. |",
            "| Focal source-to-selector mapping follows the fixed rule | High for path arithmetic; medium-high for test semantics | Frozen paths are checked against actual diffs, all base stems are scanned for collisions, and XML is checked both for missing and unexpected classes; convention still does not prove a changed helper is irrelevant to later discrimination. |",
            "| Perturbation restores bytes exactly on the gate sample | High for the 20 frozen eligible samples | Read-only planning precedes selection; original, perturbed, and restored hashes are checked, and successful apply is paired with `finally` restoration. Extrapolation beyond the sample is not claimed. |",
            "| Passing focal subsets were deterministic | High, environment-conditional | Five independent fresh-base offline runs compare stable per-test identities/outcomes and retain raw XML; five runs do not establish indefinite or cross-platform determinism. |",
            "| Passing sites are ready for the arms pilot | Not yet supported | Runner eligibility is necessary, but the separate two-sided source/test discrimination gate has not been performed here. |",
        ]
    )
    atomic_write(path, ("\n".join(lines) + "\n").encode("utf-8"))


def gate_all(arguments: argparse.Namespace) -> dict[str, Any]:
    sites = load_sites()
    implementation_paths = {
        "gate_py": Path(__file__).resolve(),
        "java_perturber": PERTURBER_SOURCE.resolve(),
        "installer": Path(__file__).with_name("install-tools.ps1").resolve(),
        "tests": Path(__file__).with_name("test_gate.py").resolve(),
        "provenance": PROVENANCE_PATH.resolve(),
        "maven_settings": MAVEN_SETTINGS.resolve(),
    }
    implementation_before = {
        name: sha256_file(path) for name, path in implementation_paths.items()
    }
    output_root = arguments.output.resolve()
    if output_root != DEFAULT_OUTPUT.resolve():
        raise GateError(f"the approved gate output is fixed at {DEFAULT_OUTPUT.resolve()}")
    scratch_resolved = arguments.scratch_root.resolve()
    if scratch_resolved == PROJECT_ROOT.resolve() or PROJECT_ROOT.resolve() in scratch_resolved.parents:
        raise GateError("scratch root must be an external sibling, not inside the project")
    output_root.mkdir(parents=True, exist_ok=True)
    run_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", arguments.scratch_root.resolve().name)
    evidence_root = output_root / "java-gates" / run_id
    if evidence_root.exists():
        raise GateError(f"evidence root already exists; refusing to mix runs: {evidence_root}")

    toolchain_provenance = verify_toolchain_provenance(
        arguments.jdk_home, arguments.maven.resolve()
    )

    jdks = sorted(
        (inspect_jdk(path) for path in arguments.jdk_home),
        key=lambda item: item.major,
        reverse=True,
    )
    if not jdks:
        raise GateError("at least one --jdk-home is required")
    majors = [jdk.major for jdk in jdks]
    if len(majors) != len(set(majors)):
        raise GateError(f"duplicate JDK majors are not supported: {majors}")
    maven = arguments.maven.resolve()
    if not maven.is_file():
        raise GateError(f"Maven launcher is absent: {maven}")
    maven_repository = MAVEN_REPOSITORY_ROOT / f"java-gate-{run_id}"
    if maven_repository.exists():
        raise GateError(
            f"pinned Maven repository already exists; refusing state reuse: {maven_repository}"
        )
    maven_repository.mkdir(parents=True)
    maven_repository_before = mirror_snapshot(maven_repository)
    maven_user_home = MAVEN_USER_HOME_ROOT / f"java-gate-{run_id}"
    if maven_user_home.exists():
        raise GateError(
            f"isolated Maven user.home already exists; refusing state reuse: {maven_user_home}"
        )
    maven_user_home.mkdir(parents=True)
    maven_user_home_before = mirror_snapshot(maven_user_home)

    mirror_before = mirror_snapshot(MIRROR_PATH)
    owned_mirror = prepare_scratch(MIRROR_PATH, arguments.scratch_root.resolve())
    owned_mirror_snapshot = mirror_snapshot(owned_mirror)
    if owned_mirror_snapshot != mirror_before:
        raise GateError("scratch mirror copy is not byte-identical to its protected source")
    worktrees_root = arguments.scratch_root.resolve() / "w"
    attempts_root = arguments.scratch_root.resolve() / "a"

    tree_by_base: dict[str, set[str]] = {}
    worktree_by_base: dict[str, Path] = {}
    mapped_by_merge: dict[str, dict[str, Any]] = {}
    patches_by_merge: dict[str, dict[str, Any]] = {}
    for site in sites:
        base = site["merge_base"]
        if base not in tree_by_base:
            raw = run_git(
                owned_mirror,
                ["ls-tree", "-r", "--name-only", base],
                bare=True,
                allow_lazy_fetch=True,
            ).stdout
            tree_by_base[base] = set(decode(raw).splitlines())
            worktree_by_base[base] = materialize_worktree(owned_mirror, worktrees_root, base)
        mapped = map_site(site, tree_by_base[base])
        mapped_by_merge[site["merge"]] = mapped
        patches_by_merge[site["merge"]] = extract_patches(
            owned_mirror, site, mapped, output_root
        )

    perturber = compile_perturber(jdks[0])
    roundtrip = roundtrip_gate(
        jdks[0], worktree_by_base, arguments.roundtrip_samples, arguments.scratch_root.resolve()
    )
    atomic_json(output_root / "java-roundtrip.json", roundtrip)
    maven_record = inspect_maven(maven, jdks[0], maven_user_home)

    compile_by_base: dict[str, dict[str, Any]] = {}
    result_rows: list[dict[str, Any]] = []
    for site_number, site in enumerate(sites, 1):
        merge = site["merge"]
        base = site["merge_base"]
        mapped = mapped_by_merge[merge]
        worktree = worktree_by_base[base]
        if base not in compile_by_base:
            compile_by_base[base] = compile_base(
                base,
                owned_mirror,
                attempts_root / "c",
                jdks,
                maven,
                maven_repository,
                maven_user_home,
                evidence_root / "_bases" / base,
            )
        compile_gate = compile_by_base[base]
        reasons: list[str] = []
        selected_jdk: Jdk | None = None
        focal: dict[str, Any] | None = None
        if compile_gate["status"] != "passed":
            last = compile_gate["attempts"][-1]["process"]
            reasons.append(
                "base Maven compile check did not pass under any installed JDK: "
                + summarize_failure(last)
            )
        else:
            selected_jdk = match_jdk(compile_gate["selected_jdk"], jdks)
        if mapped["ambiguous_selectors"]:
            reasons.append(f"ambiguous Surefire selectors: {mapped['ambiguous_selectors']}")
        if mapped["base_selector_collisions"]:
            reasons.append(
                f"Surefire selector collides in base test tree: {mapped['base_selector_collisions']}"
            )
        if mapped["missing_at_base"]:
            paths = ", ".join(item["path"] for item in mapped["missing_at_base"])
            reasons.append(f"mapped focal class absent at base: {paths}")
        for side_name in ("parent1", "parent2"):
            if not mapped["sides"][side_name]["focal_classes"]:
                reasons.append(f"{side_name} maps to no runnable Surefire class")
        if not reasons and selected_jdk is not None:
            focal = focal_gate(
                site,
                mapped,
                owned_mirror,
                attempts_root / "f" / merge[:12],
                selected_jdk,
                maven,
                maven_repository,
                maven_user_home,
                evidence_root / merge,
            )
            if focal["status"] != "passed":
                reasons.append(focal["reason"])

        verdict = "passed" if not reasons else "rejected"
        row = {
            "repo": site["repo"],
            "merge": merge,
            "base": base,
            "parents": list(site["parents"]),
            "corpus_line": site["corpus_line"],
            "verdict_scope": "java_runner_base_gate",
            "verdict": verdict,
            "reason": "runner base gate passed" if verdict == "passed" else "; ".join(reasons),
            "sides": {},
            "focal_node_ids": [item["focal_node_id"] for item in mapped["focal_classes"]],
            "runner_gate": {
                "mapping": mapped,
                "compile": compile_gate,
                "jdk": jdk_record(selected_jdk) if selected_jdk is not None else None,
                "focal_gate": focal,
                "evidence": {
                    "compile": portable_path(evidence_root / "_bases" / base),
                    "focal": portable_path(evidence_root / merge) if focal is not None else None,
                },
            },
        }
        for side_name in ("parent1", "parent2"):
            row["sides"][side_name] = {
                "parent": mapped["sides"][side_name]["parent"],
                **patches_by_merge[merge][side_name],
                "focal_node_ids": [
                    item["focal_node_id"]
                    for item in mapped["sides"][side_name]["focal_classes"]
                ],
                "focal_selectors": [
                    item["selector"] for item in mapped["sides"][side_name]["focal_classes"]
                ],
                "non_runnable_test_files": [
                    *mapped["sides"][side_name]["non_runnable_java_helpers"],
                    *mapped["sides"][side_name]["non_java_test_artifacts"],
                ],
            }
        result_rows.append(row)
        print(f"SITE {site_number:02d}/19 {merge[:12]} {verdict.upper()} {row['reason']}", flush=True)

    tracked_checks: dict[str, Any] = {}
    for base, worktree in sorted(worktree_by_base.items()):
        status = run_git(
            worktree,
            ["status", "--porcelain=v1", "--untracked-files=no"],
            bare=False,
            allow_lazy_fetch=True,
        )
        clean = not status.stdout.strip()
        tracked_checks[base] = {
            "clean": clean,
            "porcelain": decode(status.stdout),
        }
        if not clean:
            for row in result_rows:
                if row["base"] == base:
                    row["verdict"] = "rejected"
                    row["reason"] += "; tracked worktree changed during gate"

    mirror_after = mirror_snapshot(MIRROR_PATH)
    mirror_unchanged = mirror_before == mirror_after
    if not mirror_unchanged:
        for row in result_rows:
            row["verdict"] = "rejected"
            row["reason"] += "; protected source mirror changed during scored run"
    corpus_after_sha256 = sha256_file(CORPUS_PATH)
    if corpus_after_sha256 != EXPECTED_CORPUS_SHA256:
        raise GateError("frozen corpus JSONL changed during the scored run")

    toolchain_after = verify_toolchain_unchanged(toolchain_provenance)
    implementation_after = {
        name: sha256_file(path) for name, path in implementation_paths.items()
    }
    if implementation_after != implementation_before:
        raise GateError("gate implementation changed during the scored run")
    maven_repository_after = mirror_snapshot(maven_repository)
    maven_user_home_after = mirror_snapshot(maven_user_home)

    report = {
        "schema_version": 1,
        "generated_at_utc": utc_now(),
        "corpus": {
            "path": portable_path(CORPUS_PATH),
            "sha256": EXPECTED_CORPUS_SHA256,
            "sha256_after": corpus_after_sha256,
            "unchanged_during_scored_run": True,
            "selection": "evaluation_status == conflicted and both_sides_touched_tests == true",
            "site_count": len(sites),
        },
        "source_mirror": {
            "path": portable_path(MIRROR_PATH),
            "snapshot_before": mirror_before,
            "snapshot_after": mirror_after,
            "unchanged_during_scored_run": mirror_unchanged,
            "scratch_copy": portable_path(owned_mirror),
            "scratch_copy_snapshot": owned_mirror_snapshot,
            "scratch_copy_byte_identical_at_creation": True,
        },
        "scratch_root": portable_path(arguments.scratch_root.resolve()),
        "jdks": [jdk_record(jdk) for jdk in jdks],
        "maven": maven_record,
        "toolchain_provenance": toolchain_provenance,
        "toolchain_after": toolchain_after,
        "maven_repository": {
            "path": portable_path(maven_repository),
            "snapshot_before": maven_repository_before,
            "snapshot_after": maven_repository_after,
            "fresh_before_run": maven_repository_before["file_count"] == 0,
        },
        "maven_settings": {
            "path": portable_path(MAVEN_SETTINGS),
            "sha256": sha256_file(MAVEN_SETTINGS),
        },
        "maven_user_home": {
            "path": portable_path(maven_user_home),
            "snapshot_before": maven_user_home_before,
            "snapshot_after": maven_user_home_after,
            "fresh_before_run": maven_user_home_before["file_count"] == 0,
        },
        "implementation_bindings": {
            "before": implementation_before,
            "after": implementation_after,
            "unchanged_during_scored_run": True,
        },
        "perturber": perturber,
        "roundtrip": roundtrip,
        "tracked_worktree_checks": tracked_checks,
        "sites": result_rows,
        "summary": {
            "attempted": len(result_rows),
            "passed": sum(row["verdict"] == "passed" for row in result_rows),
            "rejected": sum(row["verdict"] != "passed" for row in result_rows),
        },
    }
    atomic_json(output_root / "java-gate-report.json", report)
    write_sites_json(output_root / "sites-java.json", result_rows)
    write_markdown(output_root / "JAVA-RUNNER.md", report)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--jdk-home",
        type=Path,
        action="append",
        required=True,
        help="installed JDK home; repeat for every locally available compatibility candidate",
    )
    parser.add_argument("--maven", type=Path, required=True, help="pinned mvn or mvn.cmd launcher")
    parser.add_argument("--scratch-root", type=Path, required=True, help="new, disposable owned root")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--roundtrip-samples", type=int, default=20)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    if arguments.roundtrip_samples != 20:
        parser.error("the approved Java gate requires exactly 20 round-trip samples")
    try:
        report = gate_all(arguments)
    except (GateError, OSError, ValueError, json.JSONDecodeError) as exception:
        print(f"GATE ERROR: {exception}", file=sys.stderr)
        return 1
    summary = report["summary"]
    print(
        f"JAVA GATE COMPLETE: {summary['passed']} passed / {summary['attempted']} attempted",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
