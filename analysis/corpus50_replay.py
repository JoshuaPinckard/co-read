"""Durable, guarded orchestration for the frozen Corpus-50 replay.

This module is deliberately outside :mod:`instruments.replay`.  It adapts the
unchanged ten-repository harness to a manifest supplied by the frozen
Corpus-50 selector.  The production command is plan-only unless ``--execute``
is supplied.

Typical use::

    python analysis/corpus50_replay.py --manifest D:\\c50\\manifests\\corpus-50.json
    python analysis/corpus50_replay.py --manifest D:\\c50\\manifests\\corpus-50.json --execute

To replay only the ten retained anchors from already-verified streams::

    python analysis/corpus50_replay.py --manifest ... --members anchors \
        --start-stage replay --force-stage replay --execute
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPLAY_DIRECTORY = PROJECT_ROOT / "instruments" / "replay"
if str(REPLAY_DIRECTORY) not in sys.path:
    # The unchanged harness uses sibling imports (``from common import ...``).
    sys.path.insert(0, str(REPLAY_DIRECTORY))

import clone as replay_clone  # type: ignore  # noqa: E402
import common as replay_common  # type: ignore  # noqa: E402
import extract as replay_extract  # type: ignore  # noqa: E402
import replay as replay_run  # type: ignore  # noqa: E402

for _harness_module in (replay_common, replay_clone, replay_extract, replay_run):
    if Path(_harness_module.__file__).resolve().parent != REPLAY_DIRECTORY:
        raise ImportError(
            f"expected unchanged replay harness module under {REPLAY_DIRECTORY}, "
            f"loaded {_harness_module.__file__}"
        )


RULE_ID = "C50-2026-08-23-v1"
RULE_SEED = "blast-radius-corpus-50-2026-08-23-v1"
SCOPE_NAME = (
    "50 repositories drawn under Rule C50-2026-08-23-v1 "
    "(10 retained stress anchors, 35 seeded active-frame additions, and "
    "5 seeded stress-frame additions)"
)
RETAINED_ANCHORS = (
    "hashicorp/terraform-provider-random",
    "BurntSushi/ripgrep",
    "psf/requests",
    "apache/commons-lang",
    "gohugoio/hugo",
    "ansible/ansible",
    "hashicorp/terraform",
    "redis/redis",
    "prometheus/prometheus",
    "jupyter/notebook",
)
STAGES = ("clone", "extract", "replay")
GIB = 1024**3
DEFAULT_STATE_PATH = PROJECT_ROOT / "exploratory" / "language-hole" / "corpus-50-run.json"
DEFAULT_TOTAL_CAP_BYTES = 20 * GIB
DEFAULT_VOLUME_MINIMUMS = {"D:\\": 12 * GIB, "C:\\": int(1.5 * GIB)}


class ManifestError(ValueError):
    """The selected-member manifest does not implement the frozen rule."""


class StageError(RuntimeError):
    """A stage or its written status failed verification."""


class DiskGuardViolation(StageError):
    """The frozen disk budget or free-space guard was crossed."""


def utc_now() -> str:
    return replay_common.utc_now()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode("utf-8")


def atomic_write_json(path: Path, value: Any) -> None:
    """Write one replaceable, fsynced JSON unit and fsync its directory where possible."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    payload = json_bytes(value)
    with temporary.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    # Windows does not expose a generally usable directory fsync.  On systems
    # that do, make the rename itself durable as well as the file contents.
    try:
        descriptor = os.open(path.parent, os.O_RDONLY)
    except (OSError, TypeError):
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def read_json(path: Path, *, required: bool = True) -> dict[str, Any]:
    if not path.exists():
        if required:
            raise StageError(f"required JSON is missing: {path}")
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StageError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise StageError(f"expected a JSON object in {path}")
    return value


def nested_int(raw: Mapping[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = raw.get(key)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError) as exc:
                raise ManifestError(f"{key} must be an integer, got {value!r}") from exc
    metadata = raw.get("metadata")
    if isinstance(metadata, Mapping):
        return nested_int(metadata, *keys)
    return None


@dataclass(frozen=True)
class Member:
    selection_order: int
    slug: str
    name: str
    url: str
    cohort: str
    first_parent_commit_count: int | None
    axis: str
    expected_stress: str
    raw: Mapping[str, Any] = field(compare=False, repr=False)

    @property
    def is_anchor(self) -> bool:
        return self.name in RETAINED_ANCHORS

    def harness_spec(self) -> dict[str, str]:
        # Keep the harness's original record shape.  Selection metadata stays in
        # the frozen manifest and runner state, rather than silently becoming a
        # model input or changing stream provenance.
        return {
            "slug": self.slug,
            "name": self.name,
            "url": self.url,
            "axis": self.axis,
            "expected_stress": self.expected_stress,
        }


@dataclass(frozen=True)
class CorpusManifest:
    path: Path
    sha256: str
    rule_id: str
    scope_name: str
    members: tuple[Member, ...]
    frame_root: Path | None
    accounted_paths: tuple[Path, ...]

    @property
    def canonical_order(self) -> tuple[str, ...]:
        return tuple(member.slug for member in self.members)

    @classmethod
    def load(cls, path: Path, *, allow_incomplete: bool = False) -> "CorpusManifest":
        path = path.resolve()
        raw = read_json(path)
        rule_id = str(raw.get("rule_id") or raw.get("rule_identifier") or "")
        if rule_id != RULE_ID:
            raise ManifestError(f"manifest rule_id must be {RULE_ID!r}, got {rule_id!r}")
        declared_cap = raw.get("disk_cap_bytes")
        if declared_cap is not None and int(declared_cap) != DEFAULT_TOTAL_CAP_BYTES:
            raise ManifestError(
                f"manifest disk_cap_bytes must be the frozen 20 GiB ({DEFAULT_TOTAL_CAP_BYTES}), "
                f"got {declared_cap!r}"
            )
        if not allow_incomplete:
            if raw.get("seed") != RULE_SEED:
                raise ManifestError(f"production manifest seed must be {RULE_SEED!r}")
            if declared_cap is None:
                raise ManifestError("production manifest must declare the frozen disk_cap_bytes")
            listing_dates = json.dumps(raw.get("listing_dates", None), sort_keys=True)
            if "2026-08-22" not in listing_dates or "2026-08-23" not in listing_dates:
                raise ManifestError(
                    "production manifest listing_dates must name the 2026-08-22 base listing "
                    "and 2026-08-23 stress snapshots"
                )
        scope_name = str(raw.get("scope_name") or SCOPE_NAME)
        if scope_name != SCOPE_NAME:
            raise ManifestError("manifest scope_name does not match the frozen scope-naming rule")
        raw_members = raw.get("members", raw.get("repositories"))
        if not isinstance(raw_members, list):
            raise ManifestError("manifest must contain a members list")

        members: list[Member] = []
        for position, value in enumerate(raw_members):
            if not isinstance(value, Mapping):
                raise ManifestError(f"members[{position}] is not an object")
            order = nested_int(value, "selection_order", "canonical_order")
            if order is None:
                order = position
            name = str(value.get("name") or "")
            slug = str(value.get("slug") or "")
            url = str(value.get("url") or "")
            if not name or name.count("/") != 1:
                raise ManifestError(f"members[{position}].name must be an owner/repository name")
            if not slug or slug in {".", ".."} or any(character in slug for character in "/\\:"):
                raise ManifestError(f"unsafe or missing slug for {name!r}: {slug!r}")
            parsed_url = urlparse(url)
            if parsed_url.scheme != "https" or parsed_url.netloc.casefold() != "github.com":
                raise ManifestError(f"{name}: url must be a public HTTPS GitHub URL")
            cohort = str(value.get("cohort") or "").casefold().replace("-", "_")
            if not cohort:
                if name in RETAINED_ANCHORS:
                    cohort = "retained_anchor"
                elif value.get("stress_key"):
                    cohort = "stress"
                else:
                    cohort = "base"
            aliases = {
                "anchor": "retained_anchor",
                "retained": "retained_anchor",
                "base_addition": "base",
                "stress_addition": "stress",
            }
            cohort = aliases.get(cohort, cohort)
            if cohort not in {"retained_anchor", "base", "stress"}:
                raise ManifestError(f"{name}: unsupported cohort {cohort!r}")
            if (name in RETAINED_ANCHORS) != (cohort == "retained_anchor"):
                raise ManifestError(f"{name}: retained-anchor identity and cohort disagree")
            selection_status = value.get("selection_status")
            if selection_status is not None and str(selection_status).casefold() != "selected":
                raise ManifestError(
                    f"{name}: final manifest member must have selection_status='selected', "
                    f"got {selection_status!r}"
                )
            if not allow_incomplete and selection_status is None:
                raise ManifestError(f"{name}: production member lacks selection_status")
            count = nested_int(value, "first_parent_commit_count", "first_parent_count")
            if count is not None and count < 500:
                raise ManifestError(f"{name}: selected member has fewer than 500 first-parent commits")
            members.append(
                Member(
                    selection_order=order,
                    slug=slug,
                    name=name,
                    url=url,
                    cohort=cohort,
                    first_parent_commit_count=count,
                    axis=str(value.get("axis") or value.get("layout_stratum") or cohort),
                    expected_stress=str(
                        value.get("expected_stress")
                        or value.get("stress_key")
                        or "Frozen Corpus-50 selected member"
                    ),
                    raw=value,
                )
            )

        expected_orders = list(range(len(members)))
        actual_orders = [member.selection_order for member in members]
        if actual_orders == list(range(1, len(members) + 1)):
            members = [
                Member(
                    selection_order=member.selection_order - 1,
                    slug=member.slug,
                    name=member.name,
                    url=member.url,
                    cohort=member.cohort,
                    first_parent_commit_count=member.first_parent_commit_count,
                    axis=member.axis,
                    expected_stress=member.expected_stress,
                    raw=member.raw,
                )
                for member in members
            ]
            actual_orders = [member.selection_order for member in members]
        if actual_orders != expected_orders:
            raise ManifestError(
                "members must already be in contiguous canonical selection_order; "
                f"got {actual_orders[:10]!r}"
            )
        names = [member.name.casefold() for member in members]
        slugs = [member.slug for member in members]
        if len(names) != len(set(names)) or len(slugs) != len(set(slugs)):
            raise ManifestError("manifest has a duplicate repository name or slug")

        if not allow_incomplete:
            counts = {
                cohort: sum(member.cohort == cohort for member in members)
                for cohort in ("retained_anchor", "base", "stress")
            }
            if len(members) != 50 or counts != {"retained_anchor": 10, "base": 35, "stress": 5}:
                raise ManifestError(
                    "production manifest must contain exactly 10 retained anchors, 35 base additions, "
                    f"and 5 stress additions; got {counts} across {len(members)} members"
                )
            if {member.name for member in members if member.is_anchor} != set(RETAINED_ANCHORS):
                raise ManifestError("production manifest does not contain the exact ten frozen anchors")
            missing_counts = [
                member.name
                for member in members
                if not member.is_anchor and member.first_parent_commit_count is None
            ]
            if missing_counts:
                raise ManifestError(
                    "selected additions require first_parent_commit_count for smallest-first execution: "
                    + ", ".join(missing_counts)
                )

        frame_value = raw.get("frame_root")
        frame_root = Path(str(frame_value)).resolve() if frame_value else None
        manifest_paths: list[Path] = []
        for item in raw.get("accounted_paths", []):
            candidate = Path(str(item))
            if not candidate.is_absolute():
                candidate = path.parent / candidate
            manifest_paths.append(candidate.resolve())
        return cls(
            path=path,
            sha256=sha256_file(path),
            rule_id=rule_id,
            scope_name=scope_name,
            members=tuple(members),
            frame_root=frame_root,
            accounted_paths=tuple(manifest_paths),
        )


def member_count(member: Member, corpus_records: Mapping[str, Any]) -> int:
    if member.first_parent_commit_count is not None:
        return member.first_parent_commit_count
    record = corpus_records.get(member.slug)
    if isinstance(record, Mapping) and record.get("first_parent_commit_count") is not None:
        return int(record["first_parent_commit_count"])
    raise ManifestError(f"{member.name}: first-parent commit count is required for smallest-first ordering")


def choose_members(
    manifest: CorpusManifest,
    corpus_records: Mapping[str, Any],
    *,
    group: str,
    requested_slugs: Sequence[str],
) -> list[Member]:
    if group == "anchors":
        selected = [member for member in manifest.members if member.is_anchor]
    elif group == "additions":
        selected = [member for member in manifest.members if not member.is_anchor]
    else:
        selected = list(manifest.members)
    if requested_slugs:
        unknown = sorted(set(requested_slugs) - set(manifest.canonical_order))
        if unknown:
            raise ManifestError(f"unknown requested slug(s): {', '.join(unknown)}")
        requested = set(requested_slugs)
        selected = [member for member in selected if member.slug in requested]
    return sorted(selected, key=lambda member: (member_count(member, corpus_records), member.selection_order))


def normalized_roots(paths: Iterable[Path]) -> tuple[Path, ...]:
    """Resolve roots and remove duplicates/nested roots so bytes are counted once."""
    resolved: list[Path] = []
    for path in paths:
        candidate = path.resolve()
        if candidate not in resolved:
            resolved.append(candidate)
    result: list[Path] = []
    for candidate in sorted(resolved, key=lambda item: (len(item.parts), os.fspath(item).casefold())):
        if any(candidate == parent or parent in candidate.parents for parent in result):
            continue
        result.append(candidate)
    return tuple(result)


def path_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file() or path.is_symlink():
        return path.stat().st_size
    total = 0
    stack = [path]
    while stack:
        directory = stack.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                try:
                    if entry.is_symlink():
                        total += entry.stat(follow_symlinks=False).st_size
                    elif entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
                except FileNotFoundError:
                    # Concurrent git temp-file rename/removal; the next poll sees
                    # the durable state.
                    continue
    return total


@dataclass(frozen=True)
class DiskSnapshot:
    observed_at_utc: str
    accounted_bytes: int
    accounted_by_path: Mapping[str, int]
    volume_free_bytes: Mapping[str, int | None]
    violations: tuple[str, ...]

    def as_json(self) -> dict[str, Any]:
        return {
            "observed_at_utc": self.observed_at_utc,
            "accounted_bytes": self.accounted_bytes,
            "accounted_gib": self.accounted_bytes / GIB,
            "accounted_by_path": dict(self.accounted_by_path),
            "volume_free_bytes": dict(self.volume_free_bytes),
            "violations": list(self.violations),
        }


@dataclass(frozen=True)
class DiskPolicy:
    accounted_paths: tuple[Path, ...]
    total_cap_bytes: int = DEFAULT_TOTAL_CAP_BYTES
    volume_minimums: Mapping[str, int] = field(default_factory=lambda: DEFAULT_VOLUME_MINIMUMS.copy())

    def snapshot(self) -> DiskSnapshot:
        sizes: dict[str, int] = {}
        violations: list[str] = []
        for path in self.accounted_paths:
            try:
                sizes[str(path)] = path_size(path)
            except OSError as exc:
                sizes[str(path)] = 0
                violations.append(f"cannot measure accounted path {path}: {exc}")
        total = sum(sizes.values())
        if total > self.total_cap_bytes:
            violations.append(
                f"combined accounted storage {total} bytes exceeds hard cap {self.total_cap_bytes} bytes"
            )
        free: dict[str, int | None] = {}
        for volume, minimum in self.volume_minimums.items():
            try:
                available = shutil.disk_usage(volume).free
            except OSError as exc:
                free[volume] = None
                violations.append(f"cannot verify free-space guard for {volume}: {exc}")
                continue
            free[volume] = available
            if available < minimum:
                violations.append(
                    f"{volume} free space {available} bytes is below required {minimum} bytes"
                )
        return DiskSnapshot(utc_now(), total, sizes, free, tuple(violations))


class StateLog:
    """One atomic durable run document, updated after every decision."""

    def __init__(
        self,
        path: Path,
        manifest: CorpusManifest,
        run_order: Sequence[Member],
        policy: DiskPolicy,
    ) -> None:
        self.path = path.resolve()
        self._lock = threading.RLock()
        if self.path.exists():
            state = read_json(self.path)
            if state.get("manifest_sha256") != manifest.sha256:
                raise StageError(
                    "existing run state belongs to a different manifest; use a distinct --state path"
                )
        else:
            state = {
                "schema_version": 1,
                "measurement": "cross-language-co-change-replay-corpus-50",
                "created_at_utc": utc_now(),
                "events": [],
                "repositories": {},
            }
        state.update(
            {
                "updated_at_utc": utc_now(),
                "rule_id": manifest.rule_id,
                "scope_name": manifest.scope_name,
                "manifest_path": str(manifest.path),
                "manifest_sha256": manifest.sha256,
                "canonical_repository_order": list(manifest.canonical_order),
                "current_run_order": [member.slug for member in run_order],
                "disk_policy": {
                    "combined_cap_bytes": policy.total_cap_bytes,
                    "accounted_paths": [str(path) for path in policy.accounted_paths],
                    "volume_minimum_free_bytes": dict(policy.volume_minimums),
                },
            }
        )
        repositories = state.setdefault("repositories", {})
        for member in manifest.members:
            record = repositories.setdefault(member.slug, {})
            record.update(
                {
                    "name": member.name,
                    "selection_order": member.selection_order,
                    "cohort": member.cohort,
                    "first_parent_commit_count": member.first_parent_commit_count,
                }
            )
            record.setdefault("stages", {})
        self.state = state
        self.flush()

    def flush(self) -> None:
        with self._lock:
            self.state["updated_at_utc"] = utc_now()
            atomic_write_json(self.path, self.state)

    def event(self, event: str, *, member: Member | None = None, stage: str | None = None, **details: Any) -> None:
        with self._lock:
            events = self.state.setdefault("events", [])
            entry: dict[str, Any] = {
                "sequence": len(events),
                "at_utc": utc_now(),
                "event": event,
            }
            if member is not None:
                entry.update({"slug": member.slug, "name": member.name})
            if stage is not None:
                entry["stage"] = stage
            entry.update(details)
            events.append(entry)
            self.flush()

    def stage(self, member: Member, stage: str, status: str, **details: Any) -> None:
        with self._lock:
            repository = self.state["repositories"][member.slug]
            repository["stages"][stage] = {
                "status": status,
                "observed_at_utc": utc_now(),
                **details,
            }
            self.event("stage_status", member=member, stage=stage, status=status, **details)


class GuardedSubprocesses:
    """Poll disk guards and terminate stage subprocesses after a violation.

    The unchanged clone and extraction functions are synchronous.  Temporarily
    tracking ``subprocess.Popen`` lets this outer layer stop their Git child
    processes while still calling the harness functions directly.
    """

    def __init__(self, policy: DiskPolicy, interval_seconds: float, on_snapshot: Callable[[DiskSnapshot], None]):
        self.policy = policy
        self.interval_seconds = max(0.05, interval_seconds)
        self.on_snapshot = on_snapshot
        self.violation: DiskSnapshot | None = None
        self._processes: list[subprocess.Popen[Any]] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._original_popen: Any = None

    def __enter__(self) -> "GuardedSubprocesses":
        self._original_popen = subprocess.Popen

        def tracked_popen(*args: Any, **kwargs: Any) -> subprocess.Popen[Any]:
            if self.violation is not None:
                raise DiskGuardViolation("disk guard already violated; refusing another subprocess")
            process = self._original_popen(*args, **kwargs)
            with self._lock:
                self._processes.append(process)
            return process

        subprocess.Popen = tracked_popen  # type: ignore[assignment]
        self._thread = threading.Thread(target=self._monitor, name="corpus50-disk-guard", daemon=True)
        self._thread.start()
        return self

    def _monitor(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            snapshot = self.policy.snapshot()
            if snapshot.violations:
                self.violation = snapshot
            try:
                self.on_snapshot(snapshot)
            except Exception as exc:
                # A guard observation that cannot be made durable is itself a
                # reason to stop: continuing would create an unlogged run.
                snapshot = DiskSnapshot(
                    snapshot.observed_at_utc,
                    snapshot.accounted_bytes,
                    snapshot.accounted_by_path,
                    snapshot.volume_free_bytes,
                    (*snapshot.violations, f"cannot persist disk-guard poll: {type(exc).__name__}: {exc}"),
                )
                self.violation = snapshot
            if self.violation is not None:
                self._terminate_children()
                return

    def _terminate_children(self) -> None:
        with self._lock:
            processes = list(self._processes)
        for process in processes:
            if process.poll() is not None:
                continue
            if os.name == "nt":
                try:
                    killer = self._original_popen(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                    killer.communicate(timeout=15)
                except Exception:
                    process.terminate()
            else:
                process.terminate()

    def __exit__(self, exc_type: Any, exc: BaseException | None, traceback: Any) -> bool:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=60.0)
            if self._thread.is_alive() and self.violation is None:
                self.violation = DiskSnapshot(
                    utc_now(),
                    0,
                    {},
                    {},
                    ("disk-guard monitor did not stop within 60 seconds",),
                )
        subprocess.Popen = self._original_popen  # type: ignore[assignment]
        if self.violation is not None:
            violation = DiskGuardViolation("; ".join(self.violation.violations))
            if exc is not None:
                raise violation from exc
            raise violation
        return False


@dataclass(frozen=True)
class HarnessPaths:
    corpus: Path = replay_common.CORPUS_PATH
    clones: Path = replay_common.CLONE_ROOT
    streams: Path = replay_common.STREAM_ROOT
    results: Path = replay_common.RESULT_ROOT

    def stream(self, slug: str) -> Path:
        return self.streams / f"{slug}.jsonl.gz"

    def stream_meta(self, slug: str) -> Path:
        return self.streams / f"{slug}.meta.json"

    def result(self, slug: str) -> Path:
        return self.results / f"{slug}.json"


def sync_corpus_document(paths: HarnessPaths, manifest: CorpusManifest) -> dict[str, Any]:
    """Install the canonical 50 order without dropping any existing records."""
    document = read_json(paths.corpus, required=False)
    repositories = document.get("repositories")
    if not isinstance(repositories, dict):
        repositories = {}
    document.update(
        {
            "schema_version": replay_common.SCHEMA_VERSION,
            "measurement": "cross-language-co-change-replay",
            "updated_at_utc": utc_now(),
            "clone_root": "corpus/_clones",
            "clone_filter": "blob:none",
            "repository_order": list(manifest.canonical_order),
            "repositories": repositories,
            "corpus_50": {
                "rule_id": manifest.rule_id,
                "scope_name": manifest.scope_name,
                "manifest_path": str(manifest.path),
                "manifest_sha256": manifest.sha256,
            },
        }
    )
    atomic_write_json(paths.corpus, document)
    return document


def inspect_clone(
    paths: HarnessPaths,
    member: Member,
    expected_order: Sequence[str] | None = None,
) -> dict[str, Any]:
    corpus = read_json(paths.corpus)
    if corpus.get("repository_order") is None:
        raise StageError("corpus manifest has no repository_order")
    if expected_order is not None and corpus.get("repository_order") != list(expected_order):
        raise StageError("corpus manifest repository_order was reset away from the frozen 50 order")
    records = corpus.get("repositories")
    record = records.get(member.slug) if isinstance(records, dict) else None
    if not isinstance(record, dict):
        raise StageError(f"{member.name}: clone record is missing")
    if record.get("status") != "ok":
        raise StageError(f"{member.name}: clone status is {record.get('status')!r}: {record.get('failure', '')}")
    required = (
        "resolved_head_sha",
        "reachable_commit_count",
        "first_parent_commit_count",
        "tracked_file_count_at_head",
    )
    missing = [key for key in required if record.get(key) is None]
    if missing:
        raise StageError(f"{member.name}: clone record lacks {missing}")
    if not record.get("partial_clone_promisor"):
        raise StageError(f"{member.name}: clone is not recorded as --filter=blob:none/promisor")
    if str(record.get("name", "")).casefold() != member.name.casefold():
        raise StageError(f"{member.name}: clone record names a different repository")
    recorded_url = str(record.get("url", "")).casefold().removesuffix(".git")
    expected_url = member.url.casefold().removesuffix(".git")
    if recorded_url != expected_url:
        raise StageError(f"{member.name}: clone record URL differs from selected manifest")
    frozen_head = member.raw.get("head") or member.raw.get("resolved_head_sha")
    if frozen_head and str(record["resolved_head_sha"]) != str(frozen_head):
        raise StageError(f"{member.name}: clone HEAD differs from the selector's frozen HEAD")
    if (
        member.first_parent_commit_count is not None
        and int(record["first_parent_commit_count"]) != member.first_parent_commit_count
    ):
        raise StageError(f"{member.name}: clone first-parent count differs from selected manifest")
    clone_path = paths.clones / member.slug
    if not (clone_path / ".git").exists():
        raise StageError(f"{member.name}: recorded clone directory is unavailable: {clone_path}")
    if int(record["first_parent_commit_count"]) < 500:
        raise StageError(f"{member.name}: clone no longer meets the frozen 500 first-parent minimum")
    return record


def inspect_stream(paths: HarnessPaths, member: Member, corpus_record: Mapping[str, Any]) -> dict[str, Any]:
    meta = read_json(paths.stream_meta(member.slug))
    stream_path = paths.stream(member.slug)
    if meta.get("status") != "ok":
        raise StageError(f"{member.name}: extraction status is {meta.get('status')!r}: {meta.get('failure', '')}")
    if not stream_path.exists():
        raise StageError(f"{member.name}: extraction stream is missing")
    actual_hash = sha256_file(stream_path)
    if actual_hash != meta.get("stream_sha256"):
        raise StageError(f"{member.name}: extraction stream SHA-256 mismatch")
    if meta.get("source_head_sha") != corpus_record.get("resolved_head_sha"):
        raise StageError(f"{member.name}: extraction HEAD differs from clone manifest")
    expected_cap = int(corpus_record["reachable_commit_count"]) > replay_common.CAP_THRESHOLD_REACHABLE_COMMITS
    if bool(meta.get("capped")) != expected_cap:
        raise StageError(f"{member.name}: extraction cap decision differs from clone manifest")
    expected_commits = (
        min(replay_common.CAP_COMMITS, int(corpus_record["first_parent_commit_count"]))
        if expected_cap
        else int(corpus_record["first_parent_commit_count"])
    )
    if int(meta.get("commit_count", -1)) != expected_commits:
        raise StageError(
            f"{member.name}: extraction has {meta.get('commit_count')} commits, expected {expected_commits}"
        )
    with gzip.open(stream_path, "rt", encoding="utf-8", errors="surrogatepass") as handle:
        try:
            header = json.loads(handle.readline())
        except (OSError, json.JSONDecodeError) as exc:
            raise StageError(f"{member.name}: cannot read extraction header: {exc}") from exc
    required_arguments = {
        "--first-parent",
        "--reverse",
        "--root",
        "--diff-merges=first-parent",
        "--find-renames=50%",
        "-l0",
        "--name-status",
        "-z",
    }
    if header.get("type") != "header" or header.get("schema_version") != replay_common.SCHEMA_VERSION:
        raise StageError(f"{member.name}: unsupported extraction header")
    if header.get("source_head_sha") != corpus_record.get("resolved_head_sha"):
        raise StageError(f"{member.name}: stream header HEAD differs from clone manifest")
    missing_arguments = sorted(required_arguments - set(header.get("git_log_arguments", [])))
    if missing_arguments:
        raise StageError(f"{member.name}: stale extraction protocol; missing {missing_arguments}")
    return meta


def inspect_result(
    paths: HarnessPaths,
    member: Member,
    corpus_record: Mapping[str, Any],
    stream_meta: Mapping[str, Any],
) -> dict[str, Any]:
    result = read_json(paths.result(member.slug))
    if result.get("status") != "ok":
        raise StageError(f"{member.name}: replay status is {result.get('status')!r}: {result.get('failure', '')}")
    if result.get("source_head_sha") != corpus_record.get("resolved_head_sha"):
        raise StageError(f"{member.name}: result HEAD differs from clone manifest")
    implementation = result.get("implementation")
    if not isinstance(implementation, Mapping):
        raise StageError(f"{member.name}: result implementation provenance is missing")
    current_harness_hash, _ = replay_run.harness_hashes()
    if implementation.get("harness_sha256") != current_harness_hash:
        raise StageError(f"{member.name}: result was produced by a different harness revision")
    if implementation.get("stream_sha256") != stream_meta.get("stream_sha256"):
        raise StageError(f"{member.name}: result was produced from a different stream")
    coverage = result.get("coverage")
    if not isinstance(coverage, Mapping) or int(coverage.get("commits_replayed", -1)) != int(
        stream_meta["commit_count"]
    ):
        raise StageError(f"{member.name}: result coverage differs from extraction metadata")
    return result


class Runner:
    def __init__(
        self,
        manifest: CorpusManifest,
        members: Sequence[Member],
        policy: DiskPolicy,
        state_path: Path,
        *,
        paths: HarnessPaths = HarnessPaths(),
        start_stage: str = "clone",
        stop_stage: str = "replay",
        force_stages: Iterable[str] = (),
        poll_seconds: float = 5.0,
    ) -> None:
        self.manifest = manifest
        self.members = list(members)
        self.policy = policy
        self.paths = paths
        self.start_index = STAGES.index(start_stage)
        self.stop_index = STAGES.index(stop_stage)
        if self.start_index > self.stop_index:
            raise ManifestError("--start-stage must not follow --stop-stage")
        self.force_stages = set(force_stages)
        self.poll_seconds = poll_seconds
        self.state = StateLog(state_path, manifest, members, policy)
        harness_sha256, harness_files = replay_run.harness_hashes()
        self.state.event(
            "runner_configured",
            start_stage=start_stage,
            stop_stage=stop_stage,
            force_stages=sorted(self.force_stages, key=STAGES.index),
            poll_seconds=poll_seconds,
            harness_sha256=harness_sha256,
            harness_file_sha256=harness_files,
        )

    def disk_check(self, event: str, member: Member | None = None, stage: str | None = None) -> DiskSnapshot:
        snapshot = self.policy.snapshot()
        self.state.event(event, member=member, stage=stage, disk=snapshot.as_json())
        if snapshot.violations:
            raise DiskGuardViolation("; ".join(snapshot.violations))
        return snapshot

    def _guarded(self, member: Member, stage: str, action: Callable[[], Any]) -> Any:
        self.disk_check("disk_guard_pre_stage", member, stage)

        def record(snapshot: DiskSnapshot) -> None:
            self.state.event("disk_guard_poll", member=member, stage=stage, disk=snapshot.as_json())

        with GuardedSubprocesses(self.policy, self.poll_seconds, record):
            value = action()
        self.disk_check("disk_guard_post_stage", member, stage)
        return value

    def _record_cap(self, member: Member, corpus_record: Mapping[str, Any], source: str) -> None:
        reachable = int(corpus_record["reachable_commit_count"])
        first_parent = int(corpus_record["first_parent_commit_count"])
        capped = reachable > replay_common.CAP_THRESHOLD_REACHABLE_COMMITS
        details = {
            "source": source,
            "applied": capped,
            "reachable_commit_count": reachable,
            "threshold_reachable_commits": replay_common.CAP_THRESHOLD_REACHABLE_COMMITS,
            "first_parent_commit_count": first_parent,
            "replay_commits": replay_common.CAP_COMMITS if capped else first_parent,
            "left_truncated": capped,
            "learned_indexes_start_empty": capped,
            "non_comparable_for_warm_history_claims": capped,
        }
        self.state.state["repositories"][member.slug]["cap"] = details
        self.state.event("replay_cap_applied" if capped else "replay_cap_not_applied", member=member, **details)

    def _clone(self, member: Member) -> dict[str, Any]:
        document = sync_corpus_document(self.paths, self.manifest)

        def action() -> None:
            # Required seam: invoke the existing harness directly for an
            # arbitrary manifest-supplied spec.
            replay_clone.process_repository(member.harness_spec(), document)

        guard_failure: DiskGuardViolation | None = None
        try:
            self._guarded(member, "clone", action)
        except Exception as exc:
            # process_repository normally records its own failure.  A guard can
            # terminate it between atomic writes, so make that exceptional state
            # durable without removing any earlier repository records.
            latest = read_json(self.paths.corpus, required=False)
            records = latest.setdefault("repositories", {})
            record = records.setdefault(member.slug, member.harness_spec())
            record.update(
                {
                    "status": "failed",
                    "failure_type": type(exc).__name__,
                    "failure": str(exc),
                    "clone_completed_at_utc": utc_now(),
                }
            )
            latest["repository_order"] = list(self.manifest.canonical_order)
            latest["updated_at_utc"] = utc_now()
            atomic_write_json(self.paths.corpus, latest)
            if isinstance(exc, DiskGuardViolation):
                guard_failure = exc
        if guard_failure is not None:
            raise guard_failure
        return inspect_clone(self.paths, member, self.manifest.canonical_order)

    def _extract(self, member: Member, corpus_record: Mapping[str, Any]) -> dict[str, Any]:
        guard_failure: DiskGuardViolation | None = None
        try:
            metadata = self._guarded(
                member,
                "extract",
                # Required seam: invoke the existing extractor directly.
                lambda: replay_extract.extract_repository(member.harness_spec(), dict(corpus_record)),
            )
        except Exception as exc:
            metadata = replay_extract.failed_metadata(member.harness_spec(), exc)
            if isinstance(exc, DiskGuardViolation):
                guard_failure = exc
        replay_common.atomic_write_json(self.paths.stream_meta(member.slug), metadata)
        if guard_failure is not None:
            raise guard_failure
        return inspect_stream(self.paths, member, corpus_record)

    def _replay(
        self,
        member: Member,
        corpus_record: Mapping[str, Any],
        stream_meta: Mapping[str, Any],
    ) -> dict[str, Any]:
        self.disk_check("disk_guard_pre_stage", member, "replay")
        try:
            # Required seam: invoke the existing strict temporal replay directly.
            result = replay_run.run_repository(member.harness_spec(), dict(corpus_record))
        except Exception as exc:
            result = replay_run.failed_result(member.harness_spec(), "extract_or_replay", exc)
        # The result contains per-commit aggregates and can be large.  Count its
        # exact replacement delta before writing so the 20 GiB cap cannot be
        # crossed by the final atomic file.
        payload_size = len(json_bytes(result))
        existing_size = self.paths.result(member.slug).stat().st_size if self.paths.result(member.slug).exists() else 0
        snapshot = self.policy.snapshot()
        projected = snapshot.accounted_bytes - existing_size + payload_size
        if projected > self.policy.total_cap_bytes:
            violation = DiskGuardViolation(
                f"result would raise accounted storage to {projected} bytes above "
                f"{self.policy.total_cap_bytes}"
            )
            result = replay_run.failed_result(
                member.harness_spec(),
                "disk_guard",
                violation,
            )
        else:
            violation = None
        replay_common.atomic_write_json(self.paths.result(member.slug), result)
        self.disk_check("disk_guard_post_stage", member, "replay")
        if violation is not None:
            raise violation
        return inspect_result(self.paths, member, corpus_record, stream_meta)

    def _write_downstream_failure(self, member: Member, failed_stage: str, exc: Exception) -> None:
        """Mirror harness-main failure artifacts when a full member run stops early."""
        if isinstance(exc, DiskGuardViolation):
            # The atomic state/corpus status already records the denial.  Do not
            # write more bytes after a hard storage guard has fired.
            return
        if failed_stage == "clone" and self.stop_index >= STAGES.index("extract"):
            metadata = replay_extract.failed_metadata(member.harness_spec(), exc)
            replay_common.atomic_write_json(self.paths.stream_meta(member.slug), metadata)
        if self.stop_index >= STAGES.index("replay") and failed_stage != "replay":
            result = replay_run.failed_result(member.harness_spec(), failed_stage, exc)
            replay_common.atomic_write_json(self.paths.result(member.slug), result)

    def _existing(
        self,
        stage: str,
        member: Member,
        clone_record: Mapping[str, Any] | None,
        stream_meta: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        if stage == "clone":
            return inspect_clone(self.paths, member, self.manifest.canonical_order)
        if clone_record is None:
            raise StageError(f"{member.name}: clone dependency was not verified")
        if stage == "extract":
            return inspect_stream(self.paths, member, clone_record)
        if stream_meta is None:
            raise StageError(f"{member.name}: extraction dependency was not verified")
        return inspect_result(self.paths, member, clone_record, stream_meta)

    def run_member(self, member: Member) -> None:
        self.state.event("repository_started", member=member)
        clone_record: dict[str, Any] | None = None
        stream_meta: dict[str, Any] | None = None

        # Verify dependencies before the requested start stage.  This is what
        # enables a current-harness replay of the original ten from frozen,
        # hash-verified streams without extracting again.
        for dependency_index in range(self.start_index):
            dependency = STAGES[dependency_index]
            try:
                value = self._existing(dependency, member, clone_record, stream_meta)
                if dependency == "clone":
                    clone_record = value
                    self._record_cap(member, value, "verified_dependency")
                elif dependency == "extract":
                    stream_meta = value
                self.state.stage(member, dependency, "verified_dependency")
            except Exception as exc:
                self.state.stage(
                    member,
                    dependency,
                    "failed",
                    failure_type=type(exc).__name__,
                    failure=str(exc),
                )
                self.state.event("repository_completed", member=member, status="failed")
                self._write_downstream_failure(member, dependency, exc)
                if isinstance(exc, DiskGuardViolation):
                    raise
                return

        for stage_index in range(self.start_index, self.stop_index + 1):
            stage = STAGES[stage_index]
            try:
                if stage not in self.force_stages:
                    try:
                        value = self._existing(stage, member, clone_record, stream_meta)
                    except StageError:
                        value = None
                    if value is not None:
                        if stage == "clone":
                            clone_record = value
                            self._record_cap(member, value, "reused_clone")
                        elif stage == "extract":
                            stream_meta = value
                        self.state.stage(member, stage, "reused_verified_output")
                        continue

                self.state.stage(member, stage, "in_progress")
                if stage == "clone":
                    clone_record = self._clone(member)
                    self._record_cap(member, clone_record, "clone_status_json")
                elif stage == "extract":
                    if clone_record is None:
                        raise StageError(f"{member.name}: clone dependency is unavailable")
                    stream_meta = self._extract(member, clone_record)
                    if stream_meta.get("capped"):
                        self.state.event(
                            "extraction_cap_verified",
                            member=member,
                            cap_reason=stream_meta.get("cap_reason"),
                            non_comparable_for_warm_history_claims=True,
                        )
                else:
                    if clone_record is None or stream_meta is None:
                        raise StageError(f"{member.name}: clone/extraction dependencies are unavailable")
                    self._replay(member, clone_record, stream_meta)
                self.state.stage(member, stage, "ok", status_source="written_json_reinspection")
            except Exception as exc:
                self.state.stage(
                    member,
                    stage,
                    "failed",
                    status_source="written_json_reinspection",
                    failure_type=type(exc).__name__,
                    failure=str(exc),
                )
                self.state.event("repository_completed", member=member, status="failed")
                self._write_downstream_failure(member, stage, exc)
                if isinstance(exc, DiskGuardViolation):
                    raise
                return
        self.state.event("repository_completed", member=member, status="ok")

    def run(self) -> None:
        sync_corpus_document(self.paths, self.manifest)
        self.disk_check("disk_guard_run_start")
        for member in self.members:
            # Complete all requested stages for one member before advancing.
            self.run_member(member)
        self.disk_check("disk_guard_run_end")


def plan_document(
    manifest: CorpusManifest,
    members: Sequence[Member],
    policy: DiskPolicy,
    *,
    start_stage: str,
    stop_stage: str,
    force_stages: Iterable[str],
    state_path: Path,
    effective_counts: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    counts = effective_counts or {}
    return {
        "mode": "plan_only",
        "execute_requires": "--execute",
        "rule_id": manifest.rule_id,
        "scope_name": manifest.scope_name,
        "manifest_path": str(manifest.path),
        "manifest_sha256": manifest.sha256,
        "canonical_repository_order": list(manifest.canonical_order),
        "run_order_smallest_first": [
            {
                "slug": member.slug,
                "name": member.name,
                "cohort": member.cohort,
                "first_parent_commit_count": counts.get(member.slug, member.first_parent_commit_count),
            }
            for member in members
        ],
        "stages": list(STAGES[STAGES.index(start_stage) : STAGES.index(stop_stage) + 1]),
        "force_stages": sorted(force_stages, key=STAGES.index),
        "state_path": str(state_path.resolve()),
        "disk_policy": {
            "combined_cap_bytes": policy.total_cap_bytes,
            "accounted_paths": [str(path) for path in policy.accounted_paths],
            "volume_minimum_free_bytes": dict(policy.volume_minimums),
        },
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True, help="Frozen selected-member JSON manifest.")
    parser.add_argument(
        "--members",
        choices=("additions", "anchors", "all"),
        default="additions",
        help="Defaults to the forty additions; anchors must be requested explicitly.",
    )
    parser.add_argument("--repo", action="append", default=[], help="Limit to a manifest slug; repeatable.")
    parser.add_argument("--start-stage", choices=STAGES, default="clone")
    parser.add_argument("--stop-stage", choices=STAGES, default="replay")
    parser.add_argument("--force-stage", choices=STAGES, action="append", default=[])
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE_PATH)
    parser.add_argument("--frame-root", type=Path, help="Root containing dated frame/screening artifacts.")
    parser.add_argument(
        "--accounted-path",
        type=Path,
        action="append",
        default=[],
        help="Additional path charged to the combined 20 GiB cap; repeatable.",
    )
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    parser.add_argument(
        "--allow-incomplete-manifest",
        action="store_true",
        help="Test/development only: relax the exact 10+35+5 membership check.",
    )
    parser.add_argument(
        "--skip-volume-guards",
        action="store_true",
        help="Test/development only: do not enforce the frozen C:/D: free-space floors.",
    )
    execution = parser.add_mutually_exclusive_group()
    execution.add_argument(
        "--execute",
        action="store_true",
        help="Perform work. Without this flag, print a JSON plan and change nothing.",
    )
    execution.add_argument(
        "--dry-run",
        action="store_true",
        help="Explicit alias for the default no-write JSON plan.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    manifest = CorpusManifest.load(args.manifest, allow_incomplete=args.allow_incomplete_manifest)
    corpus = read_json(replay_common.CORPUS_PATH, required=False)
    records = corpus.get("repositories") if isinstance(corpus.get("repositories"), dict) else {}
    members = choose_members(manifest, records, group=args.members, requested_slugs=args.repo)
    frame_root = args.frame_root.resolve() if args.frame_root else manifest.frame_root
    accounted = [
        replay_common.CLONE_ROOT,
        replay_common.STREAM_ROOT,
        replay_common.RESULT_ROOT,
        *manifest.accounted_paths,
        *args.accounted_path,
    ]
    if frame_root is not None:
        accounted.append(frame_root)
    if (
        args.execute
        and args.members in {"additions", "all"}
        and frame_root is None
        and not manifest.accounted_paths
        and not args.accounted_path
    ):
        raise ManifestError(
            "addition execution requires --frame-root, manifest frame_root/accounted_paths, or --accounted-path "
            "so the dated-frame and screening bytes participate in the 20 GiB guard"
        )
    volume_minimums = {} if args.skip_volume_guards else DEFAULT_VOLUME_MINIMUMS.copy()
    policy = DiskPolicy(normalized_roots(accounted), DEFAULT_TOTAL_CAP_BYTES, volume_minimums)
    plan = plan_document(
        manifest,
        members,
        policy,
        start_stage=args.start_stage,
        stop_stage=args.stop_stage,
        force_stages=args.force_stage,
        state_path=args.state,
        effective_counts={member.slug: member_count(member, records) for member in members},
    )
    if not args.execute:
        print(json.dumps(plan, indent=2, sort_keys=True, ensure_ascii=True))
        return 0

    runner = Runner(
        manifest,
        members,
        policy,
        args.state,
        start_stage=args.start_stage,
        stop_stage=args.stop_stage,
        force_stages=args.force_stage,
        poll_seconds=args.poll_seconds,
    )
    runner.run()
    print(args.state.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
