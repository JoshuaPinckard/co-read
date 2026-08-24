#!/usr/bin/env python3
"""Acquire and deterministically select the Rule C50-2026-08-23-v1 corpus.

This program deliberately does not invoke or modify ``instruments/replay``.
Its network commands only acquire the two dated selection frames.  Screening,
classification, selection-ledger, and manifest commands operate on local Git
repositories and durable files.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import datetime as dt
import email.utils
import gzip
import hashlib
import io
import json
import math
import os
import re
import shutil
import sqlite3
import stat
import statistics
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Iterable, Iterator, Mapping, Sequence


RULE_ID = "C50-2026-08-23-v1"
SEED = "blast-radius-corpus-50-2026-08-23-v1"
BASE_LISTING_DATE = "2026-08-22"
STRESS_LISTING_DATE = "2026-08-23"
SCOPE_NAME = (
    "50 repositories drawn under Rule C50-2026-08-23-v1 "
    "(10 retained stress anchors, 35 seeded active-frame additions, "
    "and 5 seeded stress-frame additions)"
)
SCHEMA_VERSION = 1
GIB = 1024**3
HARD_CAP_BYTES = 20 * GIB
MIN_D_FREE_BYTES = 12 * GIB
MIN_C_FREE_BYTES = int(1.5 * GIB)
DOWNLOAD_POLL_BYTES = 32 * 1024**2
USER_AGENT = "blast-radius-corpus50/1 (+local measurement acquisition)"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SELECTION_LEDGER = PROJECT_ROOT / "corpus" / "CORPUS-50-LEDGER.jsonl"
DEFAULT_CORPUS_MANIFEST = PROJECT_ROOT / "corpus" / "CORPUS-50.json"

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

STRESS_KEYS = ("config", "catalog", "import", "low_author", "non_english")

LANGUAGE_QUOTAS: dict[str, int] = {
    "C/C++": 4,
    "JVM": 4,
    "JS/TS": 5,
    "Python": 5,
    "Go": 4,
    "Rust": 4,
    ".NET": 3,
    "Ruby/PHP": 3,
    "Other/no-code": 3,
}
LAYOUT_QUOTAS: dict[str, int] = {
    "artifact/config/docs": 6,
    "manifest monorepo": 8,
    "multi-module tree": 9,
    "single-package tree": 12,
}

# The values are the displayed realised languages.  Strata are assigned below.
EXTENSION_LANGUAGE: dict[str, str] = {
    ".c": "C",
    ".h": "C",
    ".cc": "C++",
    ".cpp": "C++",
    ".cxx": "C++",
    ".hpp": "C++",
    ".hh": "C++",
    ".m": "Objective-C",
    ".mm": "Objective-C",
    ".java": "Java",
    ".kt": "Kotlin",
    ".kts": "Kotlin",
    ".scala": "Scala",
    ".clj": "Clojure",
    ".cljs": "Clojure",
    ".cljc": "Clojure",
    ".groovy": "Groovy",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".mjs": "JavaScript",
    ".cjs": "JavaScript",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".vue": "Vue",
    ".svelte": "Svelte",
    ".py": "Python",
    ".pyi": "Python",
    ".pyx": "Python",
    ".go": "Go",
    ".rs": "Rust",
    ".cs": "C#",
    ".fs": "F#",
    ".fsx": "F#",
    ".vb": "VB.NET",
    ".rb": "Ruby",
    ".rake": "Ruby",
    ".php": "PHP",
    ".swift": "Swift",
    ".sh": "Shell",
    ".bash": "Shell",
    ".zsh": "Shell",
    ".fish": "Shell",
    ".lua": "Lua",
    ".hs": "Haskell",
    ".lhs": "Haskell",
    ".ex": "Elixir",
    ".exs": "Elixir",
    ".erl": "Erlang",
    ".hrl": "Erlang",
    ".r": "R",
    ".jl": "Julia",
    ".pl": "Perl",
    ".pm": "Perl",
    ".dart": "Dart",
    ".zig": "Zig",
    ".vim": "Vim Script",
    ".sol": "Solidity",
    ".sql": "SQL",
    ".html": "HTML/CSS",
    ".htm": "HTML/CSS",
    ".css": "HTML/CSS",
    ".scss": "HTML/CSS",
    ".sass": "HTML/CSS",
    ".less": "HTML/CSS",
    ".tf": "Terraform/HCL",
    ".hcl": "Terraform/HCL",
    ".nix": "Nix",
    ".wy": "Wenyan",
}

LANGUAGE_STRATUM: dict[str, str] = {
    "C": "C/C++",
    "C++": "C/C++",
    "Objective-C": "C/C++",
    "Java": "JVM",
    "Kotlin": "JVM",
    "Scala": "JVM",
    "Clojure": "JVM",
    "Groovy": "JVM",
    "JavaScript": "JS/TS",
    "TypeScript": "JS/TS",
    "Vue": "JS/TS",
    "Svelte": "JS/TS",
    "Python": "Python",
    "Go": "Go",
    "Rust": "Rust",
    "C#": ".NET",
    "F#": ".NET",
    "VB.NET": ".NET",
    "Ruby": "Ruby/PHP",
    "PHP": "Ruby/PHP",
}

MANIFEST_NAMES = {
    "package.json",
    "pyproject.toml",
    "setup.py",
    "cargo.toml",
    "go.mod",
    "pom.xml",
    "build.gradle",
    "package.swift",
    "composer.json",
    "gemfile",
    "mix.exs",
}

CONFIG_EXTENSIONS = {
    ".gitignore",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".conf",
    ".config",
    ".xml",
    ".properties",
    ".lock",
    ".md",
    ".rst",
    ".txt",
}


class Corpus50Error(RuntimeError):
    """A refusal or validation failure that preserves the frozen rule."""


@dataclass(frozen=True)
class SnapshotSpec:
    snapshot_id: str
    stress_key: str
    query: str
    page: int

    def url(self, api_base: str = "https://api.github.com") -> str:
        parameters = urllib.parse.urlencode(
            {
                "q": self.query,
                "sort": "stars",
                "order": "desc",
                "per_page": "100",
                "page": str(self.page),
            }
        )
        return f"{api_base.rstrip('/')}/search/repositories?{parameters}"


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise Corpus50Error(f"cannot read valid JSON from {path}: {error}") from error


def atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".partial")
    with temporary.open("wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def atomic_write_json(path: Path, value: object) -> None:
    atomic_write_bytes(path, canonical_json_bytes(value))


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> tuple[str, int]:
    digest = hashlib.sha256()
    length = 0
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
            length += len(chunk)
    return digest.hexdigest(), length


def directory_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    total = 0
    for root, _directories, files in os.walk(path):
        for name in files:
            candidate = Path(root) / name
            try:
                total += candidate.stat().st_size
            except FileNotFoundError:
                continue
    return total


def slug_for_name(name: str) -> str:
    if name.count("/") != 1:
        raise Corpus50Error(f"repository name is not owner/name: {name!r}")
    return name.replace("/", "__")


def priority_key(kind: str, repo_id: int) -> str:
    if kind != "base" and kind not in STRESS_KEYS:
        raise Corpus50Error(f"unknown priority-key kind: {kind}")
    material = f"{SEED}\0{kind}\0{int(repo_id)}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def is_bot(login: str | None) -> bool:
    if not login:
        return False
    folded = login.casefold()
    return folded.endswith("[bot]") or folded == "github-actions"


def github_header_pairs(headers: Mapping[str, str] | Any) -> list[list[str]]:
    if hasattr(headers, "raw_items"):
        return [[str(key), str(value)] for key, value in headers.raw_items()]
    return [[str(key), str(value)] for key, value in headers.items()]


def header_value(header_pairs: Sequence[Sequence[str]], name: str) -> str | None:
    folded = name.casefold()
    values = [value for key, value in header_pairs if key.casefold() == folded]
    return values[-1] if values else None


def parse_http_date(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


@dataclass(frozen=True)
class _LedgerFingerprint:
    device: int
    inode: int
    size: int
    modified_ns: int
    changed_ns: int


@dataclass
class _HashChainTip:
    fingerprint: _LedgerFingerprint
    records: int
    last_record_sha256: str | None
    base_terminal_ranks: set[int] = field(default_factory=set)
    base_terminal_prefix: int = 0
    stress_terminal_ranks: dict[str, set[int]] = field(default_factory=dict)
    stress_terminal_prefixes: dict[str, int] = field(default_factory=dict)


_HASH_CHAIN_TIPS: dict[Path, _HashChainTip] = {}
_HASH_CHAIN_TIPS_LOCK = threading.RLock()


def _ledger_fingerprint(value: os.stat_result) -> _LedgerFingerprint:
    return _LedgerFingerprint(
        device=int(value.st_dev),
        inode=int(value.st_ino),
        size=int(value.st_size),
        modified_ns=int(value.st_mtime_ns),
        changed_ns=int(value.st_ctime_ns),
    )


def _ledger_cache_key(path: Path) -> Path:
    return path.resolve()


def _terminal_rank(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _advance_terminal_prefix(ranks: set[int], current: int) -> int:
    while current + 1 in ranks:
        current += 1
    return current


def _index_selection_terminal(tip: _HashChainTip, record: Mapping[str, Any]) -> None:
    candidate = record.get("candidate")
    outcome = record.get("outcome")
    if not isinstance(candidate, Mapping) or not isinstance(outcome, Mapping):
        return
    event_type = record.get("event_type")
    status = outcome.get("status")

    if status == "rejected" or status == "selected":
        stress_key = candidate.get("stress_key")
        rank = _terminal_rank(candidate.get("candidate_order"))
        if isinstance(stress_key, str) and rank is not None:
            ranks = tip.stress_terminal_ranks.setdefault(stress_key, set())
            ranks.add(rank)
            tip.stress_terminal_prefixes[stress_key] = _advance_terminal_prefix(
                ranks, tip.stress_terminal_prefixes.get(stress_key, 0)
            )

    base_terminal = (
        event_type == "candidate_screened"
        and candidate.get("cohort") == "base"
        and (status == "rejected" or status == "eligible")
    ) or (
        candidate.get("cohort") == "stress" and status == "selected"
    ) or (
        event_type == "base_candidate_removed_as_stress" and status == "excluded"
    )
    if base_terminal:
        rank = _terminal_rank(candidate.get("base_rank"))
        if rank is not None:
            tip.base_terminal_ranks.add(rank)
            tip.base_terminal_prefix = _advance_terminal_prefix(
                tip.base_terminal_ranks, tip.base_terminal_prefix
            )


def _scan_hash_chain(path: Path) -> _HashChainTip:
    """Fully verify one stable ledger snapshot and rebuild its cached tip."""

    if not path.exists():
        raise Corpus50Error(f"ledger does not exist: {path}")
    previous: str | None = None
    count = 0
    with path.open("rb") as stream:
        before = _ledger_fingerprint(os.fstat(stream.fileno()))
        tip = _HashChainTip(
            fingerprint=before,
            records=0,
            last_record_sha256=None,
        )
        for count, raw in enumerate(stream, start=1):
            try:
                record = json.loads(raw)
            except json.JSONDecodeError as error:
                raise Corpus50Error(
                    f"broken JSONL ledger {path} at line {count}: {error}"
                ) from error
            if not isinstance(record, dict):
                raise Corpus50Error(
                    f"non-object JSONL ledger record in {path} at line {count}"
                )
            claimed = record.pop("record_sha256", None)
            if record.get("previous_record_sha256") != previous:
                raise Corpus50Error(f"bad chain link at line {count}")
            actual = hashlib.sha256(canonical_json_bytes(record)).hexdigest()
            if claimed != actual:
                raise Corpus50Error(f"bad record checksum at line {count}")
            _index_selection_terminal(tip, record)
            previous = claimed
        after = _ledger_fingerprint(os.fstat(stream.fileno()))
    try:
        path_after = _ledger_fingerprint(path.stat())
    except OSError as error:
        raise Corpus50Error(f"ledger changed during verification: {path}") from error
    if before != after or after != path_after:
        raise Corpus50Error(f"ledger changed during verification: {path}")
    tip.fingerprint = after
    tip.records = count
    tip.last_record_sha256 = previous
    return tip


def _validated_hash_chain_tip(path: Path) -> _HashChainTip:
    """Return a cached verified tip, rescanning after any filesystem change."""

    key = _ledger_cache_key(path)
    try:
        observed = _ledger_fingerprint(path.stat())
    except OSError as error:
        _HASH_CHAIN_TIPS.pop(key, None)
        raise Corpus50Error(f"ledger does not exist: {path}") from error
    cached = _HASH_CHAIN_TIPS.get(key)
    if cached is not None and cached.fingerprint == observed:
        return cached
    _HASH_CHAIN_TIPS.pop(key, None)
    verified = _scan_hash_chain(path)
    _HASH_CHAIN_TIPS[key] = verified
    return verified


def append_hash_chained(path: Path, record: Mapping[str, Any]) -> dict[str, Any]:
    """Append one fsynced canonical JSON record after validating the cached tip."""

    path.parent.mkdir(parents=True, exist_ok=True)
    key = _ledger_cache_key(path)
    with _HASH_CHAIN_TIPS_LOCK:
        if not path.exists():
            path.open("ab").close()
        for _attempt in range(3):
            tip = _validated_hash_chain_tip(path)
            with path.open("ab") as stream:
                before = _ledger_fingerprint(os.fstat(stream.fileno()))
                if before != tip.fingerprint:
                    _HASH_CHAIN_TIPS.pop(key, None)
                    continue

                completed = dict(record)
                completed.setdefault("schema_version", SCHEMA_VERSION)
                completed.setdefault("rule_id", RULE_ID)
                completed.setdefault("recorded_at_utc", utc_now())
                completed.setdefault("event_id", f"C50-{tip.records + 1:06d}")
                completed["previous_record_sha256"] = tip.last_record_sha256
                completed.pop("record_sha256", None)
                completed["record_sha256"] = hashlib.sha256(
                    canonical_json_bytes(completed)
                ).hexdigest()
                payload = canonical_json_bytes(completed)
                try:
                    written = stream.write(payload)
                    if written != len(payload):
                        raise OSError(
                            f"short ledger append: wrote {written} of {len(payload)} bytes"
                        )
                    stream.flush()
                    os.fsync(stream.fileno())
                    after = _ledger_fingerprint(os.fstat(stream.fileno()))
                    path_after = _ledger_fingerprint(path.stat())
                except Exception:
                    _HASH_CHAIN_TIPS.pop(key, None)
                    raise
                if (
                    (after.device, after.inode) != (before.device, before.inode)
                    or after.size != before.size + len(payload)
                    or path_after != after
                ):
                    _HASH_CHAIN_TIPS.pop(key, None)
                    raise Corpus50Error(f"ledger changed while appending: {path}")
                tip.fingerprint = after
                tip.records += 1
                tip.last_record_sha256 = completed["record_sha256"]
                _index_selection_terminal(tip, completed)
                _HASH_CHAIN_TIPS[key] = tip
                return completed
        raise Corpus50Error(f"ledger changed repeatedly before append: {path}")


def verify_hash_chain(path: Path) -> dict[str, Any]:
    """Force a complete chain verification and refresh the process-local tip."""

    with _HASH_CHAIN_TIPS_LOCK:
        key = _ledger_cache_key(path)
        _HASH_CHAIN_TIPS.pop(key, None)
        verified = _scan_hash_chain(path)
        _HASH_CHAIN_TIPS[key] = verified
        return {
            "valid": True,
            "records": verified.records,
            "last_record_sha256": verified.last_record_sha256,
        }


@dataclass
class DiskGuard:
    frame_root: Path
    accounted_paths: tuple[Path, ...] = ()
    hard_cap_bytes: int = HARD_CAP_BYTES
    min_d_free_bytes: int = MIN_D_FREE_BYTES
    min_c_free_bytes: int = MIN_C_FREE_BYTES

    def paths(self) -> tuple[Path, ...]:
        paths = [self.frame_root.resolve()]
        seen = {str(paths[0]).casefold()}
        for raw in self.accounted_paths:
            resolved = raw.resolve()
            key = str(resolved).casefold()
            # Do not double-count a descendant of an already-accounted root.
            if any(_is_relative_to(resolved, parent) for parent in paths):
                continue
            descendants = [p for p in paths if _is_relative_to(p, resolved)]
            for descendant in descendants:
                paths.remove(descendant)
                seen.discard(str(descendant).casefold())
            if key not in seen:
                paths.append(resolved)
                seen.add(key)
        return tuple(paths)

    def check(self, *, extra_bytes: int = 0) -> dict[str, int]:
        accounted = sum(directory_size(path) for path in self.paths())
        if accounted + max(0, extra_bytes) > self.hard_cap_bytes:
            raise Corpus50Error(
                "20 GiB combined hard cap would be exceeded: "
                f"accounted={accounted}, prospective_extra={extra_bytes}, "
                f"cap={self.hard_cap_bytes}"
            )
        free: dict[str, int] = {"accounted_bytes": accounted}
        for drive, minimum in (("D:\\", self.min_d_free_bytes), ("C:\\", self.min_c_free_bytes)):
            drive_path = Path(drive)
            if not drive_path.exists():
                if drive.startswith("D:"):
                    raise Corpus50Error("required D: volume is unavailable")
                continue
            available = shutil.disk_usage(drive_path).free
            free[f"{drive[0].lower()}_free_bytes"] = available
            if available < minimum:
                raise Corpus50Error(
                    f"{drive[:2]} free space {available} is below frozen guard {minimum}"
                )
        return free


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def ensure_d_frame_root(frame_root: Path) -> Path:
    resolved = frame_root.resolve()
    if os.name == "nt" and resolved.drive.casefold() != "d:":
        raise Corpus50Error(
            f"frame root must be on D: for the frozen run, got {resolved}"
        )
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def gharchive_specs(base_url: str = "https://data.gharchive.org") -> list[dict[str, Any]]:
    return [
        {
            "hour": hour,
            "url": f"{base_url.rstrip('/')}/{BASE_LISTING_DATE}-{hour}.json.gz",
            "filename": f"{BASE_LISTING_DATE}-{hour}.json.gz",
        }
        for hour in range(24)
    ]


def stress_snapshot_specs() -> list[SnapshotSpec]:
    common = "fork:false archived:false size:<200000"
    specs: list[SnapshotSpec] = []

    def add(
        stress_key: str, label: str, query: str, pages: Iterable[int] = (1,)
    ) -> None:
        for page in pages:
            specs.append(
                SnapshotSpec(
                    snapshot_id=f"{stress_key}-{label}-p{page:02d}",
                    stress_key=stress_key,
                    query=f"{query} {common}",
                    page=page,
                )
            )

    for term in ("configuration", "dotfiles", "gitignore"):
        add("config", term, f"{term} in:name,description,topics")
    for term in ("monorepo", "registry", "package-manager"):
        add("catalog", term, f"{term} in:name,description,topics")
    add("import", "stars-ge-1000", "stars:>=1000", range(1, 11))
    for language, label in (
        ("C", "c"),
        ("C++", "cpp"),
        ("Lua", "lua"),
        ("Vim Script", "vim-script"),
    ):
        quoted = json.dumps(language) if " " in language else language
        add(
            "low_author",
            label,
            f"language:{quoted} stars:>=100",
            range(1, 11),
        )
    for index, term in enumerate(
        (
            "classical-chinese",
            "chinese programming-language",
            "japanese",
            "korean",
            "arabic",
            "cyrillic",
            "unicode",
        ),
        start=1,
    ):
        add(
            "non_english",
            f"term-{index:02d}",
            f"{term} in:name,description,readme,topics",
        )
    if len(specs) != 63:
        raise AssertionError(f"fixed query catalog should have 63 snapshots, got {len(specs)}")
    return specs


def acquisition_paths(frame_root: Path) -> dict[str, Path]:
    return {
        "gharchive": frame_root / "raw" / "gharchive" / BASE_LISTING_DATE,
        "search": frame_root / "raw" / "github-search" / STRESS_LISTING_DATE,
        "frames": frame_root / "frames",
        "state": frame_root / "state",
        "manifests": frame_root / "manifests",
        "acquisition_ledger": frame_root / "manifests" / "acquisitions.jsonl",
    }


def validate_gzip(path: Path) -> None:
    try:
        with gzip.open(path, "rb") as stream:
            while stream.read(4 * 1024 * 1024):
                pass
    except (OSError, EOFError) as error:
        raise Corpus50Error(f"corrupt gzip stream {path}: {error}") from error


def _request_headers(token: str | None = None) -> dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Encoding": "identity",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["Accept"] = "application/vnd.github+json"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    return headers


def _safe_request_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {key: value for key, value in headers.items() if key.casefold() != "authorization"}


def _sleep_until_rate_reset(
    header_pairs: Sequence[Sequence[str]], *, max_wait_seconds: int
) -> bool:
    retry_after = header_value(header_pairs, "Retry-After")
    reset = header_value(header_pairs, "X-RateLimit-Reset")
    wait_seconds: float | None = None
    if retry_after:
        try:
            wait_seconds = float(retry_after)
        except ValueError:
            pass
    if wait_seconds is None and reset:
        try:
            wait_seconds = max(0.0, float(reset) - time.time() + 1.0)
        except ValueError:
            pass
    if wait_seconds is None:
        return False
    if wait_seconds > max_wait_seconds:
        raise Corpus50Error(
            f"GitHub rate limit requires {wait_seconds:.0f}s wait, above allowed "
            f"{max_wait_seconds}s"
        )
    remaining = wait_seconds
    while remaining > 0:
        interval = min(30.0, remaining)
        time.sleep(interval)
        remaining -= interval
    return True


def acquire_gharchive(
    frame_root: Path,
    *,
    account_paths: Sequence[Path] = (),
    base_url: str = "https://data.gharchive.org",
    timeout: float = 120.0,
    retries: int = 5,
) -> dict[str, Any]:
    frame_root = ensure_d_frame_root(frame_root)
    paths = acquisition_paths(frame_root)
    target_dir = paths["gharchive"]
    target_dir.mkdir(parents=True, exist_ok=True)
    guard = DiskGuard(frame_root, tuple(account_paths))
    inventory: list[dict[str, Any]] = []

    for spec in gharchive_specs(base_url):
        target = target_dir / spec["filename"]
        metadata_path = target.with_suffix(target.suffix + ".acquisition.json")
        headers_path = target.with_suffix(target.suffix + ".headers.json")
        if target.exists() or metadata_path.exists() or headers_path.exists():
            if not (target.exists() and metadata_path.exists() and headers_path.exists()):
                raise Corpus50Error(
                    f"incomplete prior acquisition beside {target}; preserve it and "
                    "resolve explicitly rather than replacing a dated artifact"
                )
            metadata = _verify_gharchive_artifact(target, metadata_path, headers_path, spec)
            inventory.append(metadata)
            continue

        last_error: Exception | None = None
        for attempt in range(1, retries + 1):
            request_headers = _request_headers()
            request = urllib.request.Request(spec["url"], headers=request_headers)
            partial = target.with_name(target.name + ".partial")
            try:
                guard.check()
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    response_headers = github_header_pairs(response.headers)
                    content_length = header_value(response_headers, "Content-Length")
                    prospective = int(content_length) if content_length and content_length.isdigit() else 0
                    guard.check(extra_bytes=prospective)
                    digest = hashlib.sha256()
                    length = 0
                    next_poll = DOWNLOAD_POLL_BYTES
                    with partial.open("wb") as output:
                        while True:
                            chunk = response.read(1024 * 1024)
                            if not chunk:
                                break
                            output.write(chunk)
                            digest.update(chunk)
                            length += len(chunk)
                            if length >= next_poll:
                                guard.check()
                                next_poll += DOWNLOAD_POLL_BYTES
                        output.flush()
                        os.fsync(output.fileno())
                    validate_gzip(partial)
                    retrieved = utc_now()
                    headers_payload = {
                        "schema_version": SCHEMA_VERSION,
                        "status": int(getattr(response, "status", 200)),
                        "reason": str(getattr(response, "reason", "")),
                        "header_pairs": response_headers,
                    }
                    metadata = {
                        "schema_version": SCHEMA_VERSION,
                        "rule_id": RULE_ID,
                        "listing_date": BASE_LISTING_DATE,
                        "hour": spec["hour"],
                        "url": spec["url"],
                        "retrieved_at_utc": retrieved,
                        "http_date": header_value(response_headers, "Date"),
                        "etag": header_value(response_headers, "ETag"),
                        "byte_length": length,
                        "sha256": digest.hexdigest(),
                        "gzip_valid": True,
                        "request_headers": _safe_request_headers(request_headers),
                        "response_headers_file": headers_path.name,
                    }
                    atomic_write_json(headers_path, headers_payload)
                    os.replace(partial, target)
                    atomic_write_json(metadata_path, metadata)
                    append_hash_chained(
                        paths["acquisition_ledger"],
                        {
                            "event_type": "gharchive_acquired",
                            "url": spec["url"],
                            "artifact": str(target),
                            "outcome": {"status": "complete"},
                            "measurements": {
                                "byte_length": length,
                                "sha256": digest.hexdigest(),
                            },
                        },
                    )
                    inventory.append(metadata)
                    break
            except (OSError, urllib.error.URLError, Corpus50Error) as error:
                last_error = error
                append_hash_chained(
                    paths["acquisition_ledger"],
                    {
                        "event_type": "gharchive_attempt_failed",
                        "url": spec["url"],
                        "outcome": {
                            "status": "failed",
                            "reason": type(error).__name__,
                            "detail": str(error),
                            "attempt": attempt,
                        },
                    },
                )
                if attempt < retries:
                    time.sleep(min(30.0, float(2 ** (attempt - 1))))
        else:
            raise Corpus50Error(
                f"required GH Archive hour {spec['hour']} unavailable after {retries} "
                f"attempts: {last_error}"
            )

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "rule_id": RULE_ID,
        "kind": "gharchive-24-hour-listing",
        "listing_date": BASE_LISTING_DATE,
        "complete": len(inventory) == 24,
        "artifacts": inventory,
    }
    output = paths["manifests"] / f"gharchive-{BASE_LISTING_DATE}.json"
    atomic_write_json(output, manifest)
    return manifest


def _verify_gharchive_artifact(
    target: Path, metadata_path: Path, headers_path: Path, spec: Mapping[str, Any]
) -> dict[str, Any]:
    metadata = read_json(metadata_path)
    if metadata.get("url") != spec["url"] or metadata.get("hour") != spec["hour"]:
        raise Corpus50Error(f"acquisition metadata does not match fixed hour: {metadata_path}")
    digest, length = sha256_file(target)
    if metadata.get("sha256") != digest or metadata.get("byte_length") != length:
        raise Corpus50Error(f"checksum or length mismatch for {target}")
    headers = read_json(headers_path)
    if not isinstance(headers.get("header_pairs"), list):
        raise Corpus50Error(f"response headers are missing from {headers_path}")
    validate_gzip(target)
    return metadata


def _open_base_database(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=60.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS processed_hours (
            hour INTEGER PRIMARY KEY,
            sha256 TEXT NOT NULL,
            byte_length INTEGER NOT NULL,
            raw_event_count INTEGER NOT NULL,
            public_push_event_count INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS repositories (
            repo_id INTEGER PRIMARY KEY,
            latest_name TEXT NOT NULL,
            latest_created_at TEXT NOT NULL,
            latest_event_id INTEGER NOT NULL,
            push_event_count INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS repository_names (
            repo_id INTEGER NOT NULL REFERENCES repositories(repo_id),
            name TEXT NOT NULL,
            name_folded TEXT NOT NULL,
            PRIMARY KEY (repo_id, name)
        );
        CREATE INDEX IF NOT EXISTS repository_names_folded
            ON repository_names(name_folded);
        CREATE TABLE IF NOT EXISTS nonbot_actors (
            repo_id INTEGER NOT NULL REFERENCES repositories(repo_id),
            login_folded TEXT NOT NULL,
            PRIMARY KEY (repo_id, login_folded)
        );
        CREATE TABLE IF NOT EXISTS active_export (
            repo_id INTEGER PRIMARY KEY,
            priority_key TEXT NOT NULL,
            record_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS active_export_priority
            ON active_export(priority_key, repo_id);
        """
    )
    expected_metadata = {
        "schema_version": str(SCHEMA_VERSION),
        "rule_id": RULE_ID,
        "seed": SEED,
        "listing_date": BASE_LISTING_DATE,
    }
    existing = dict(connection.execute("SELECT key, value FROM metadata"))
    if existing and any(existing.get(key) != value for key, value in expected_metadata.items()):
        raise Corpus50Error(
            f"base-frame database {path} belongs to a different rule or schema"
        )
    with connection:
        connection.executemany(
            "INSERT OR IGNORE INTO metadata(key, value) VALUES (?, ?)",
            expected_metadata.items(),
        )
    return connection


def _parse_decimal(value: object, *, field: str, context: str) -> int:
    if isinstance(value, bool):
        raise Corpus50Error(f"{field} is not decimal in {context}: {value!r}")
    text = str(value)
    if not re.fullmatch(r"[0-9]+", text):
        raise Corpus50Error(f"{field} is not decimal in {context}: {value!r}")
    parsed = int(text)
    if parsed > 9_223_372_036_854_775_807:
        raise Corpus50Error(f"{field} exceeds SQLite signed integer range in {context}")
    return parsed


def _validate_created_at(value: object, *, context: str) -> str:
    if not isinstance(value, str):
        raise Corpus50Error(f"created_at is not a string in {context}")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise Corpus50Error(f"invalid created_at in {context}: {value!r}") from error
    if parsed.tzinfo is None:
        raise Corpus50Error(f"created_at has no timezone in {context}: {value!r}")
    # GH Archive uses the sortable fixed-width UTC representation.  Refuse a
    # variant rather than silently changing the frozen tuple ordering.
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", value):
        raise Corpus50Error(f"non-canonical GH Archive created_at in {context}: {value!r}")
    return value


def _ingest_gharchive_hour(
    connection: sqlite3.Connection,
    *,
    hour: int,
    path: Path,
    expected_sha256: str,
    expected_length: int,
) -> dict[str, int]:
    existing = connection.execute(
        "SELECT sha256, byte_length, raw_event_count, public_push_event_count "
        "FROM processed_hours WHERE hour = ?",
        (hour,),
    ).fetchone()
    if existing is not None:
        if existing["sha256"] != expected_sha256 or existing["byte_length"] != expected_length:
            raise Corpus50Error(f"hour {hour} changed after it was committed to the frame")
        return {
            "raw_event_count": int(existing["raw_event_count"]),
            "public_push_event_count": int(existing["public_push_event_count"]),
        }

    raw_count = 0
    push_count = 0
    upsert_repository = """
        INSERT INTO repositories(
            repo_id, latest_name, latest_created_at, latest_event_id, push_event_count
        ) VALUES (?, ?, ?, ?, 1)
        ON CONFLICT(repo_id) DO UPDATE SET
            latest_name = CASE WHEN
                excluded.latest_created_at > repositories.latest_created_at OR
                (excluded.latest_created_at = repositories.latest_created_at AND
                 excluded.latest_event_id > repositories.latest_event_id)
                THEN excluded.latest_name ELSE repositories.latest_name END,
            latest_created_at = CASE WHEN
                excluded.latest_created_at > repositories.latest_created_at OR
                (excluded.latest_created_at = repositories.latest_created_at AND
                 excluded.latest_event_id > repositories.latest_event_id)
                THEN excluded.latest_created_at ELSE repositories.latest_created_at END,
            latest_event_id = CASE WHEN
                excluded.latest_created_at > repositories.latest_created_at OR
                (excluded.latest_created_at = repositories.latest_created_at AND
                 excluded.latest_event_id > repositories.latest_event_id)
                THEN excluded.latest_event_id ELSE repositories.latest_event_id END,
            push_event_count = repositories.push_event_count + 1
    """
    try:
        connection.execute("BEGIN IMMEDIATE")
        with gzip.open(path, "rt", encoding="utf-8", newline="") as stream:
            for line_number, line in enumerate(stream, start=1):
                raw_count += 1
                context = f"{path.name}:{line_number}"
                try:
                    event = json.loads(line)
                except json.JSONDecodeError as error:
                    raise Corpus50Error(f"invalid event JSON at {context}: {error}") from error
                if event.get("type") != "PushEvent" or event.get("public") is not True:
                    continue
                repository = event.get("repo")
                if not isinstance(repository, dict):
                    raise Corpus50Error(f"PushEvent has no repo object at {context}")
                repo_id = _parse_decimal(repository.get("id"), field="repo.id", context=context)
                name = repository.get("name")
                if not isinstance(name, str) or not name or "/" not in name:
                    raise Corpus50Error(f"PushEvent has invalid repo.name at {context}")
                event_id = _parse_decimal(event.get("id"), field="event.id", context=context)
                created_at = _validate_created_at(event.get("created_at"), context=context)
                connection.execute(
                    upsert_repository, (repo_id, name, created_at, event_id)
                )
                connection.execute(
                    "INSERT OR IGNORE INTO repository_names(repo_id, name, name_folded) "
                    "VALUES (?, ?, ?)",
                    (repo_id, name, name.casefold()),
                )
                actor = event.get("actor")
                login = actor.get("login") if isinstance(actor, dict) else None
                if isinstance(login, str) and login and not is_bot(login):
                    connection.execute(
                        "INSERT OR IGNORE INTO nonbot_actors(repo_id, login_folded) "
                        "VALUES (?, ?)",
                        (repo_id, login.casefold()),
                    )
                push_count += 1
        connection.execute(
            "INSERT INTO processed_hours(hour, sha256, byte_length, raw_event_count, "
            "public_push_event_count) VALUES (?, ?, ?, ?, ?)",
            (hour, expected_sha256, expected_length, raw_count, push_count),
        )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    return {"raw_event_count": raw_count, "public_push_event_count": push_count}


def _finalize_active_export(connection: sqlite3.Connection) -> dict[str, int]:
    connection.execute("DELETE FROM active_export")
    anchor_names = {name.casefold() for name in RETAINED_ANCHORS}
    query = """
        WITH actor_counts AS (
            SELECT repo_id, COUNT(*) AS actor_count
            FROM nonbot_actors
            GROUP BY repo_id
        )
        SELECT r.repo_id, r.latest_name, r.latest_created_at, r.latest_event_id,
               r.push_event_count, a.actor_count, n.name, n.name_folded
        FROM repositories AS r
        JOIN actor_counts AS a ON a.repo_id = r.repo_id
        JOIN repository_names AS n ON n.repo_id = r.repo_id
        WHERE (r.push_event_count >= 3 AND a.actor_count >= 1)
           OR a.actor_count >= 2
        ORDER BY r.repo_id ASC, n.name COLLATE BINARY ASC
    """
    active_count = 0
    removed_anchor_count = 0
    current_id: int | None = None
    current: dict[str, Any] | None = None
    names: list[str] = []

    def emit() -> None:
        nonlocal active_count, removed_anchor_count
        if current is None:
            return
        if any(name.casefold() in anchor_names for name in names):
            removed_anchor_count += 1
            return
        repo_id = int(current["repo_id"])
        key = priority_key("base", repo_id)
        record = {
            "repo_id": repo_id,
            "clone_name": current["latest_name"],
            "observed_names": list(names),
            "latest_observation": {
                "created_at": current["latest_created_at"],
                "event_id": str(current["latest_event_id"]),
            },
            "push_event_count": int(current["push_event_count"]),
            "distinct_nonbot_actor_count": int(current["actor_count"]),
            "priority_key": key,
            "url": f"https://github.com/{current['latest_name']}.git",
        }
        connection.execute(
            "INSERT INTO active_export(repo_id, priority_key, record_json) VALUES (?, ?, ?)",
            (
                repo_id,
                key,
                json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            ),
        )
        active_count += 1

    for row in connection.execute(query):
        repo_id = int(row["repo_id"])
        if current_id != repo_id:
            emit()
            current_id = repo_id
            current = dict(row)
            names = []
        names.append(str(row["name"]))
    emit()
    connection.commit()
    return {
        "active_repository_count": active_count,
        "retained_anchor_identity_count_removed": removed_anchor_count,
    }


def build_base_frame(frame_root: Path) -> dict[str, Any]:
    frame_root = frame_root.resolve()
    paths = acquisition_paths(frame_root)
    acquisition_manifest_path = (
        paths["manifests"] / f"gharchive-{BASE_LISTING_DATE}.json"
    )
    acquisition_manifest = read_json(acquisition_manifest_path)
    if (
        acquisition_manifest.get("rule_id") != RULE_ID
        or acquisition_manifest.get("listing_date") != BASE_LISTING_DATE
        or acquisition_manifest.get("complete") is not True
    ):
        raise Corpus50Error(f"incomplete or wrong acquisition manifest: {acquisition_manifest_path}")
    artifacts = acquisition_manifest.get("artifacts")
    if not isinstance(artifacts, list) or len(artifacts) != 24:
        raise Corpus50Error("the base listing must contain exactly 24 acquisition records")
    by_hour = {int(item["hour"]): item for item in artifacts}
    if set(by_hour) != set(range(24)):
        raise Corpus50Error("the base listing omits or duplicates a required UTC hour")

    database_path = paths["state"] / "base-frame.sqlite3"
    connection = _open_base_database(database_path)
    hour_counts: list[dict[str, Any]] = []
    try:
        for spec in gharchive_specs():
            hour = spec["hour"]
            metadata = by_hour[hour]
            raw_path = paths["gharchive"] / spec["filename"]
            metadata_path = raw_path.with_suffix(raw_path.suffix + ".acquisition.json")
            headers_path = raw_path.with_suffix(raw_path.suffix + ".headers.json")
            verified = _verify_gharchive_artifact(
                raw_path, metadata_path, headers_path, spec
            )
            if verified.get("sha256") != metadata.get("sha256"):
                raise Corpus50Error(f"inventory checksum disagreement for hour {hour}")
            counts = _ingest_gharchive_hour(
                connection,
                hour=hour,
                path=raw_path,
                expected_sha256=str(metadata["sha256"]),
                expected_length=int(metadata["byte_length"]),
            )
            hour_counts.append({"hour": hour, **counts})
        processed = connection.execute("SELECT COUNT(*) FROM processed_hours").fetchone()[0]
        if processed != 24:
            raise Corpus50Error(f"base construction committed only {processed} of 24 hours")
        export_counts = _finalize_active_export(connection)

        output_path = paths["frames"] / "base-active.jsonl"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = output_path.with_name(output_path.name + ".partial")
        digest = hashlib.sha256()
        count = 0
        with temporary.open("wb") as output:
            for count, row in enumerate(
                connection.execute(
                    "SELECT record_json FROM active_export "
                    "ORDER BY priority_key ASC, repo_id ASC"
                ),
                start=1,
            ):
                record = json.loads(row["record_json"])
                record["base_rank"] = count
                raw = canonical_json_bytes(record)
                output.write(raw)
                digest.update(raw)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, output_path)
        total_raw = sum(item["raw_event_count"] for item in hour_counts)
        total_push = sum(item["public_push_event_count"] for item in hour_counts)
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "rule_id": RULE_ID,
            "seed": SEED,
            "kind": "active-base-frame",
            "listing_date": BASE_LISTING_DATE,
            "source_manifest": str(acquisition_manifest_path.resolve()),
            "source_hours": [
                {
                    "hour": hour,
                    "url": by_hour[hour]["url"],
                    "byte_length": by_hour[hour]["byte_length"],
                    "sha256": by_hour[hour]["sha256"],
                }
                for hour in range(24)
            ],
            "construction": {
                "only_public_push_events": True,
                "repository_identity": "decimal repo.id",
                "clone_name_order": "maximum (created_at, numeric event.id)",
                "bot_rule": "casefold(login) endswith [bot] or equals github-actions",
                "anchor_removal": "any observed name casefold-equals a retained anchor",
                "priority_preimage": "UTF8(seed + NUL + base + NUL + decimal_repo_id)",
            },
            "raw_event_count": total_raw,
            "public_push_event_count": total_push,
            **export_counts,
            "record_count": count,
            "output": str(output_path.resolve()),
            "byte_length": output_path.stat().st_size,
            "sha256": digest.hexdigest(),
            "complete": count == export_counts["active_repository_count"],
        }
        manifest_path = paths["manifests"] / "base-active.json"
        atomic_write_json(manifest_path, manifest)
        return manifest
    finally:
        connection.close()


def _validate_search_body(
    body: bytes, *, spec: SnapshotSpec, context: str
) -> dict[str, Any]:
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise Corpus50Error(f"invalid GitHub Search JSON in {context}: {error}") from error
    if not isinstance(payload, dict):
        raise Corpus50Error(f"GitHub Search response is not an object in {context}")
    if payload.get("incomplete_results") is not False:
        raise Corpus50Error(
            f"GitHub Search returned incomplete_results != false for {spec.snapshot_id}"
        )
    if not isinstance(payload.get("items"), list):
        raise Corpus50Error(f"GitHub Search response has no items list in {context}")
    return payload


def _validate_snapshot_date(
    *, retrieved_at_utc: str, http_date: str | None, context: str
) -> None:
    try:
        retrieved = dt.datetime.fromisoformat(retrieved_at_utc.replace("Z", "+00:00"))
    except ValueError as error:
        raise Corpus50Error(f"invalid retrieval time in {context}") from error
    if retrieved.astimezone(dt.timezone.utc).date().isoformat() != STRESS_LISTING_DATE:
        raise Corpus50Error(
            f"stress snapshot {context} was not retrieved on {STRESS_LISTING_DATE} UTC"
        )
    parsed_http_date = parse_http_date(http_date)
    if parsed_http_date is None:
        raise Corpus50Error(f"stress snapshot {context} lacks a valid HTTP Date header")
    if parsed_http_date.date().isoformat() != STRESS_LISTING_DATE:
        raise Corpus50Error(
            f"HTTP Date for stress snapshot {context} is {parsed_http_date.date()}, "
            f"not frozen date {STRESS_LISTING_DATE}"
        )


def _search_artifact_paths(search_root: Path, snapshot_id: str) -> dict[str, Path]:
    stem = search_root / snapshot_id
    return {
        "body": stem.with_suffix(".response.json"),
        "headers": stem.with_suffix(".headers.json"),
        "metadata": stem.with_suffix(".acquisition.json"),
    }


def _save_search_failure(
    search_root: Path,
    *,
    spec: SnapshotSpec,
    attempt: int,
    status: int | None,
    body: bytes,
    header_pairs: Sequence[Sequence[str]],
    detail: str,
) -> dict[str, Any]:
    failure_root = search_root / "failed-attempts"
    failure_root.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    prefix = failure_root / f"{spec.snapshot_id}-{stamp}-a{attempt:02d}"
    digest = hashlib.sha256(body).hexdigest()
    if body:
        atomic_write_bytes(prefix.with_suffix(".response.bin"), body)
    record = {
        "schema_version": SCHEMA_VERSION,
        "rule_id": RULE_ID,
        "snapshot_id": spec.snapshot_id,
        "stress_key": spec.stress_key,
        "query": spec.query,
        "page": spec.page,
        "attempt": attempt,
        "status": status,
        "retrieved_at_utc": utc_now(),
        "header_pairs": [list(pair) for pair in header_pairs],
        "body_byte_length": len(body),
        "body_sha256": digest,
        "detail": detail,
    }
    atomic_write_json(prefix.with_suffix(".json"), record)
    return record


def _verify_search_artifact(
    search_root: Path,
    spec: SnapshotSpec,
    *,
    api_base: str,
) -> dict[str, Any]:
    paths = _search_artifact_paths(search_root, spec.snapshot_id)
    if not all(path.exists() for path in paths.values()):
        raise Corpus50Error(
            f"incomplete prior search snapshot {spec.snapshot_id}; preserve and resolve it"
        )
    metadata = read_json(paths["metadata"])
    expected_url = spec.url(api_base)
    expected = {
        "rule_id": RULE_ID,
        "snapshot_id": spec.snapshot_id,
        "stress_key": spec.stress_key,
        "query": spec.query,
        "page": spec.page,
        "url": expected_url,
    }
    if any(metadata.get(key) != value for key, value in expected.items()):
        raise Corpus50Error(f"search metadata disagrees with fixed query: {paths['metadata']}")
    digest, length = sha256_file(paths["body"])
    if metadata.get("sha256") != digest or metadata.get("byte_length") != length:
        raise Corpus50Error(f"checksum or length mismatch for {paths['body']}")
    headers = read_json(paths["headers"])
    if not isinstance(headers.get("header_pairs"), list):
        raise Corpus50Error(f"missing full response header list in {paths['headers']}")
    _validate_search_body(paths["body"].read_bytes(), spec=spec, context=str(paths["body"]))
    _validate_snapshot_date(
        retrieved_at_utc=str(metadata.get("retrieved_at_utc")),
        http_date=metadata.get("http_date"),
        context=spec.snapshot_id,
    )
    return metadata


def acquire_search_snapshots(
    frame_root: Path,
    *,
    token: str | None,
    account_paths: Sequence[Path] = (),
    api_base: str = "https://api.github.com",
    timeout: float = 60.0,
    retries: int = 10,
    max_rate_wait_seconds: int = 900,
) -> dict[str, Any]:
    frame_root = ensure_d_frame_root(frame_root)
    paths = acquisition_paths(frame_root)
    search_root = paths["search"]
    search_root.mkdir(parents=True, exist_ok=True)
    guard = DiskGuard(frame_root, tuple(account_paths))
    specs = stress_snapshot_specs()
    catalog = {
        "schema_version": SCHEMA_VERSION,
        "rule_id": RULE_ID,
        "kind": "fixed-github-search-query-catalog",
        "listing_date": STRESS_LISTING_DATE,
        "sort": "stars",
        "order": "desc",
        "per_page": 100,
        "snapshots": [
            {
                "snapshot_id": spec.snapshot_id,
                "stress_key": spec.stress_key,
                "query": spec.query,
                "page": spec.page,
                "url": spec.url(api_base),
            }
            for spec in specs
        ],
    }
    atomic_write_json(paths["manifests"] / "github-search-query-catalog.json", catalog)
    inventory: list[dict[str, Any]] = []

    for spec_index, spec in enumerate(specs):
        artifact_paths = _search_artifact_paths(search_root, spec.snapshot_id)
        present = [path.exists() for path in artifact_paths.values()]
        if any(present):
            if not all(present):
                raise Corpus50Error(
                    f"partial prior snapshot exists for {spec.snapshot_id}; refusing overwrite"
                )
            inventory.append(
                _verify_search_artifact(search_root, spec, api_base=api_base)
            )
            continue
        if dt.datetime.now(dt.timezone.utc).date().isoformat() != STRESS_LISTING_DATE:
            raise Corpus50Error(
                f"missing snapshot {spec.snapshot_id} cannot be acquired after the frozen "
                f"UTC date {STRESS_LISTING_DATE}"
            )

        last_error: Exception | None = None
        for attempt in range(1, retries + 1):
            request_headers = _request_headers(token)
            # The GitHub version/Accept headers are required even without auth.
            request_headers.setdefault("Accept", "application/vnd.github+json")
            request_headers.setdefault("X-GitHub-Api-Version", "2022-11-28")
            url = spec.url(api_base)
            request = urllib.request.Request(url, headers=request_headers)
            response_headers: list[list[str]] = []
            response_body = b""
            try:
                guard.check()
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    status = int(getattr(response, "status", 200))
                    response_headers = github_header_pairs(response.headers)
                    content_length = header_value(response_headers, "Content-Length")
                    prospective = int(content_length) if content_length and content_length.isdigit() else 0
                    if prospective > 64 * 1024**2:
                        raise Corpus50Error(
                            f"implausibly large Search response ({prospective} bytes) for "
                            f"{spec.snapshot_id}"
                        )
                    guard.check(extra_bytes=prospective)
                    body = response.read(64 * 1024**2 + 1)
                    response_body = body
                    if len(body) > 64 * 1024**2:
                        raise Corpus50Error(
                            f"Search response exceeds 64 MiB for {spec.snapshot_id}"
                        )
                    retrieved = utc_now()
                    _validate_search_body(
                        body, spec=spec, context=f"HTTP response {spec.snapshot_id}"
                    )
                    http_date = header_value(response_headers, "Date")
                    _validate_snapshot_date(
                        retrieved_at_utc=retrieved,
                        http_date=http_date,
                        context=spec.snapshot_id,
                    )
                    digest = hashlib.sha256(body).hexdigest()
                    headers_payload = {
                        "schema_version": SCHEMA_VERSION,
                        "status": status,
                        "reason": str(getattr(response, "reason", "")),
                        "header_pairs": response_headers,
                    }
                    metadata = {
                        "schema_version": SCHEMA_VERSION,
                        "rule_id": RULE_ID,
                        "listing_date": STRESS_LISTING_DATE,
                        "snapshot_id": spec.snapshot_id,
                        "stress_key": spec.stress_key,
                        "query": spec.query,
                        "page": spec.page,
                        "url": url,
                        "retrieved_at_utc": retrieved,
                        "http_date": http_date,
                        "etag": header_value(response_headers, "ETag"),
                        "byte_length": len(body),
                        "sha256": digest,
                        "incomplete_results": False,
                        "request_headers": _safe_request_headers(request_headers),
                        "response_headers_file": artifact_paths["headers"].name,
                        "response_file": artifact_paths["body"].name,
                    }
                    atomic_write_bytes(artifact_paths["body"], body)
                    atomic_write_json(artifact_paths["headers"], headers_payload)
                    atomic_write_json(artifact_paths["metadata"], metadata)
                    append_hash_chained(
                        paths["acquisition_ledger"],
                        {
                            "event_type": "github_search_snapshot_acquired",
                            "url": url,
                            "artifact": str(artifact_paths["body"]),
                            "outcome": {"status": "complete"},
                            "measurements": {
                                "snapshot_id": spec.snapshot_id,
                                "byte_length": len(body),
                                "sha256": digest,
                                "incomplete_results": False,
                            },
                        },
                    )
                    inventory.append(metadata)
                    if (
                        spec_index + 1 < len(specs)
                        and header_value(response_headers, "X-RateLimit-Remaining") == "0"
                    ):
                        _sleep_until_rate_reset(
                            response_headers, max_wait_seconds=max_rate_wait_seconds
                        )
                    break
            except urllib.error.HTTPError as error:
                response_headers = github_header_pairs(error.headers)
                body = error.read(4 * 1024**2)
                last_error = error
                _save_search_failure(
                    search_root,
                    spec=spec,
                    attempt=attempt,
                    status=error.code,
                    body=body,
                    header_pairs=response_headers,
                    detail=str(error),
                )
                append_hash_chained(
                    paths["acquisition_ledger"],
                    {
                        "event_type": "github_search_attempt_failed",
                        "url": spec.url(api_base),
                        "outcome": {
                            "status": "failed",
                            "reason": f"HTTP {error.code}",
                            "attempt": attempt,
                        },
                        "measurements": {"snapshot_id": spec.snapshot_id},
                    },
                )
                rate_limited = (
                    error.code == 429
                    or header_value(response_headers, "X-RateLimit-Remaining") == "0"
                    or header_value(response_headers, "Retry-After") is not None
                )
                if rate_limited and _sleep_until_rate_reset(
                    response_headers, max_wait_seconds=max_rate_wait_seconds
                ):
                    continue
                if error.code in (400, 401, 404, 422):
                    break
                if attempt < retries:
                    time.sleep(min(30.0, float(2 ** (attempt - 1))))
            except (OSError, urllib.error.URLError, Corpus50Error) as error:
                last_error = error
                _save_search_failure(
                    search_root,
                    spec=spec,
                    attempt=attempt,
                    status=None,
                    body=response_body,
                    header_pairs=response_headers,
                    detail=str(error),
                )
                append_hash_chained(
                    paths["acquisition_ledger"],
                    {
                        "event_type": "github_search_attempt_failed",
                        "url": spec.url(api_base),
                        "outcome": {
                            "status": "failed",
                            "reason": type(error).__name__,
                            "detail": str(error),
                            "attempt": attempt,
                        },
                        "measurements": {"snapshot_id": spec.snapshot_id},
                    },
                )
                if attempt < retries:
                    time.sleep(min(30.0, float(2 ** (attempt - 1))))
        else:
            raise Corpus50Error(
                f"failed to acquire required Search snapshot {spec.snapshot_id}: {last_error}"
            )
        if len(inventory) != spec_index + 1:
            raise Corpus50Error(
                f"failed to acquire required Search snapshot {spec.snapshot_id}: {last_error}"
            )

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "rule_id": RULE_ID,
        "kind": "github-search-stress-snapshots",
        "listing_date": STRESS_LISTING_DATE,
        "complete": len(inventory) == len(specs),
        "snapshot_count": len(inventory),
        "artifacts": inventory,
    }
    manifest_path = paths["manifests"] / "github-search-snapshots.json"
    atomic_write_json(manifest_path, manifest)
    return manifest


def build_stress_frames(
    frame_root: Path, *, api_base: str = "https://api.github.com"
) -> dict[str, Any]:
    frame_root = frame_root.resolve()
    paths = acquisition_paths(frame_root)
    search_root = paths["search"]
    inventory_path = paths["manifests"] / "github-search-snapshots.json"
    inventory = read_json(inventory_path)
    specs = stress_snapshot_specs()
    artifacts = inventory.get("artifacts")
    if (
        inventory.get("rule_id") != RULE_ID
        or inventory.get("listing_date") != STRESS_LISTING_DATE
        or inventory.get("complete") is not True
        or not isinstance(artifacts, list)
        or len(artifacts) != len(specs)
    ):
        raise Corpus50Error(f"Search snapshot inventory is not complete: {inventory_path}")
    by_id = {item.get("snapshot_id"): item for item in artifacts}
    if set(by_id) != {spec.snapshot_id for spec in specs}:
        raise Corpus50Error("Search snapshot inventory has missing or unexpected IDs")

    observations: dict[str, dict[int, dict[str, Any]]] = {
        key: {} for key in STRESS_KEYS
    }
    for spec_index, spec in enumerate(specs):
        verified = _verify_search_artifact(search_root, spec, api_base=api_base)
        if verified.get("sha256") != by_id[spec.snapshot_id].get("sha256"):
            raise Corpus50Error(f"inventory checksum disagreement for {spec.snapshot_id}")
        artifact_paths = _search_artifact_paths(search_root, spec.snapshot_id)
        payload = _validate_search_body(
            artifact_paths["body"].read_bytes(), spec=spec, context=spec.snapshot_id
        )
        for result_index, item in enumerate(payload["items"]):
            if not isinstance(item, dict):
                raise Corpus50Error(f"non-object Search item in {spec.snapshot_id}")
            repo_id = _parse_decimal(
                item.get("id"), field="repository.id", context=spec.snapshot_id
            )
            full_name = item.get("full_name")
            if not isinstance(full_name, str) or full_name.count("/") != 1:
                raise Corpus50Error(f"invalid full_name in {spec.snapshot_id} item {result_index}")
            frame = observations[spec.stress_key]
            row = frame.setdefault(
                repo_id,
                {
                    "repo_id": repo_id,
                    "observed_names": set(),
                    "source_snapshot_ids": [],
                    "clone_name": full_name,
                    "url": item.get("clone_url")
                    if isinstance(item.get("clone_url"), str)
                    else f"https://github.com/{full_name}.git",
                    "html_url": item.get("html_url")
                    if isinstance(item.get("html_url"), str)
                    else f"https://github.com/{full_name}",
                    "last_observation_order": [-1, -1],
                },
            )
            row["observed_names"].add(full_name)
            row["source_snapshot_ids"].append(spec.snapshot_id)
            order = [spec_index, result_index]
            if order > row["last_observation_order"]:
                row["clone_name"] = full_name
                row["url"] = (
                    item.get("clone_url")
                    if isinstance(item.get("clone_url"), str)
                    else f"https://github.com/{full_name}.git"
                )
                row["html_url"] = (
                    item.get("html_url")
                    if isinstance(item.get("html_url"), str)
                    else f"https://github.com/{full_name}"
                )
                row["last_observation_order"] = order

    frame_manifests: dict[str, Any] = {}
    paths["frames"].mkdir(parents=True, exist_ok=True)
    for stress_key in STRESS_KEYS:
        rows: list[dict[str, Any]] = []
        for repo_id, mutable in observations[stress_key].items():
            rows.append(
                {
                    "repo_id": repo_id,
                    "clone_name": mutable["clone_name"],
                    "observed_names": sorted(mutable["observed_names"]),
                    "url": mutable["url"],
                    "html_url": mutable["html_url"],
                    "source_snapshot_ids": sorted(set(mutable["source_snapshot_ids"])),
                    "priority_key": priority_key(stress_key, repo_id),
                    "stress_key": stress_key,
                }
            )
        rows.sort(key=lambda row: (row["priority_key"], row["repo_id"]))
        output_path = paths["frames"] / f"stress-{stress_key}.jsonl"
        digest = hashlib.sha256()
        temporary = output_path.with_name(output_path.name + ".partial")
        with temporary.open("wb") as output:
            for rank, row in enumerate(rows, start=1):
                row["stress_rank"] = rank
                raw = canonical_json_bytes(row)
                output.write(raw)
                digest.update(raw)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, output_path)
        frame_manifests[stress_key] = {
            "record_count": len(rows),
            "output": str(output_path.resolve()),
            "byte_length": output_path.stat().st_size,
            "sha256": digest.hexdigest(),
        }
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "rule_id": RULE_ID,
        "seed": SEED,
        "kind": "stress-frames",
        "listing_date": STRESS_LISTING_DATE,
        "source_manifest": str(inventory_path.resolve()),
        "construction": {
            "deduplicate_by": "decimal repository id within stress frame",
            "clone_name": "last observation in fixed snapshot order, then API item order",
            "priority_preimage": "UTF8(seed + NUL + stress_key + NUL + decimal_repo_id)",
        },
        "frames": frame_manifests,
        "complete": set(frame_manifests) == set(STRESS_KEYS),
    }
    atomic_write_json(paths["manifests"] / "stress-frames.json", manifest)
    return manifest


@dataclass(frozen=True)
class TreeEntry:
    mode: str
    object_type: str
    object_id: str
    path: str


def run_git(
    repository: Path,
    arguments: Sequence[str],
    *,
    check: bool = True,
) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise Corpus50Error(
            f"git {' '.join(arguments)} failed in {repository}: {detail}"
        )
    return completed.stdout


def git_text(repository: Path, arguments: Sequence[str]) -> str:
    raw = run_git(repository, arguments)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise Corpus50Error(
            f"git output is not UTF-8 for {' '.join(arguments)} in {repository}"
        ) from error


def list_tree(repository: Path, revision: str = "HEAD") -> list[TreeEntry]:
    raw = run_git(repository, ["ls-tree", "-r", "-z", "--full-tree", revision])
    entries: list[TreeEntry] = []
    for item in raw.split(b"\0"):
        if not item:
            continue
        try:
            metadata, path_raw = item.split(b"\t", 1)
            mode_raw, type_raw, object_id_raw = metadata.split(b" ", 2)
            path = path_raw.decode("utf-8")
        except (ValueError, UnicodeDecodeError) as error:
            raise Corpus50Error(
                f"cannot parse a UTF-8 tracked path from {repository} at {revision}"
            ) from error
        entries.append(
            TreeEntry(
                mode=mode_raw.decode("ascii"),
                object_type=type_raw.decode("ascii"),
                object_id=object_id_raw.decode("ascii"),
                path=path,
            )
        )
    return entries


def path_extension(path: str) -> str:
    return PurePosixPath(path).suffix.casefold()


def is_source_path(path: str) -> bool:
    return path_extension(path) in EXTENSION_LANGUAGE


def classify_paths(paths: Sequence[str]) -> dict[str, Any]:
    tracked_count = len(paths)
    language_counts: Counter[str] = Counter()
    source_by_top: Counter[str] = Counter()
    source_count = 0
    manifest_directories: set[str] = set()
    for path in paths:
        pure = PurePosixPath(path)
        extension = path_extension(path)
        language = EXTENSION_LANGUAGE.get(extension)
        if language is not None:
            source_count += 1
            language_counts[language] += 1
            if len(pure.parts) >= 2:
                source_by_top[pure.parts[0]] += 1
        basename = pure.name.casefold()
        is_manifest = (
            basename in MANIFEST_NAMES
            or basename.endswith(".csproj")
            or basename.endswith(".fsproj")
        )
        if is_manifest and len(pure.parts) >= 2:
            parent = pure.parent.as_posix()
            if parent != ".":
                manifest_directories.add(parent)

    if language_counts:
        maximum = max(language_counts.values())
        primary_language = min(
            language for language, count in language_counts.items() if count == maximum
        )
        language_stratum = LANGUAGE_STRATUM.get(primary_language, "Other/no-code")
    else:
        primary_language = "No source-bearing path"
        language_stratum = "Other/no-code"

    # Integer comparisons avoid floating-point boundary ambiguity at exactly 20%.
    if tracked_count == 0 or source_count * 5 <= tracked_count:
        layout_stratum = "artifact/config/docs"
    elif len(manifest_directories) >= 5:
        layout_stratum = "manifest monorepo"
    elif sum(count >= 5 for count in source_by_top.values()) >= 4:
        layout_stratum = "multi-module tree"
    else:
        layout_stratum = "single-package tree"

    return {
        "tracked_path_count": tracked_count,
        "source_path_count": source_count,
        "source_path_fraction": (source_count / tracked_count if tracked_count else 0.0),
        "primary_language": primary_language,
        "language_stratum": language_stratum,
        "language_path_counts": dict(sorted(language_counts.items())),
        "layout_stratum": layout_stratum,
        "manifest_directory_count": len(manifest_directories),
        "manifest_directories": sorted(manifest_directories),
        "multi_module_top_level_counts": dict(sorted(source_by_top.items())),
    }


def evaluate_config_predicate(paths: Sequence[str]) -> dict[str, Any]:
    source_count = sum(is_source_path(path) for path in paths)
    allowed: list[str] = []
    disallowed: list[str] = []
    for path in paths:
        basename = PurePosixPath(path).name
        folded_basename = basename.casefold()
        extension = path_extension(path)
        accepted = (
            folded_basename == ".gitignore"
            or extension in CONFIG_EXTENSIONS
            or ("." not in basename and folded_basename in {"readme", "license"})
        )
        (allowed if accepted else disallowed).append(path)
    tracked_count = len(paths)
    ratio = len(allowed) / tracked_count if tracked_count else 0.0
    passed = tracked_count > 0 and source_count <= 5 and len(allowed) * 10 >= tracked_count * 9
    return {
        "predicate": "config",
        "passed": passed,
        "tracked_path_count": tracked_count,
        "source_path_count": source_count,
        "allowed_path_count": len(allowed),
        "allowed_path_fraction": ratio,
        "disallowed_paths": disallowed,
    }


def evaluate_catalog_predicate(paths: Sequence[str]) -> dict[str, Any]:
    tracked_count = len(paths)
    candidates: list[dict[str, Any]] = []
    split_paths = [PurePosixPath(path).parts for path in paths]
    prefixes: list[tuple[str, ...]] = [()]
    prefixes.extend(
        (name,) for name in sorted({parts[0] for parts in split_paths if len(parts) >= 2})
    )
    for prefix in prefixes:
        prefix_length = len(prefix)
        if prefix_length not in (0, 1):
            continue
        for sharded in (False, True):
            component_counts: Counter[str] = Counter()
            for parts in split_paths:
                if tuple(parts[:prefix_length]) != prefix:
                    continue
                offset = prefix_length
                if sharded:
                    if len(parts) < offset + 3:
                        continue
                    shard = parts[offset]
                    if not re.fullmatch(r"[A-Za-z0-9]{1,2}", shard):
                        continue
                    component = f"{shard}/{parts[offset + 1]}"
                    component_index = offset + 1
                else:
                    if len(parts) < offset + 2:
                        continue
                    component = parts[offset]
                    component_index = offset
                if len(parts) <= component_index + 1:
                    continue
                component_counts[component] += 1
            component_number = len(component_counts)
            covered = sum(component_counts.values())
            median = (
                float(statistics.median(component_counts.values()))
                if component_counts
                else math.inf
            )
            passed = (
                tracked_count > 0
                and component_number >= 100
                and covered * 10 >= tracked_count * 6
                and median <= 20
            )
            candidates.append(
                {
                    "prefix": "/".join(prefix) if prefix else ".",
                    "sharded": sharded,
                    "component_count": component_number,
                    "covered_path_count": covered,
                    "covered_path_fraction": covered / tracked_count if tracked_count else 0.0,
                    "median_files_per_component": None if math.isinf(median) else median,
                    "passed": passed,
                }
            )
    passing = [candidate for candidate in candidates if candidate["passed"]]
    evidence = max(
        passing or candidates,
        key=lambda item: (
            item["passed"],
            item["covered_path_fraction"],
            item["component_count"],
            not item["sharded"],
            item["prefix"],
        ),
        default=None,
    )
    return {
        "predicate": "catalog",
        "passed": bool(passing),
        "tracked_path_count": tracked_count,
        "label": (
            "flat/sharded catalog: hierarchy carries component identity or shard, "
            "not category semantics"
        ),
        "evidence": evidence,
    }


def _tree_path_bytes(repository: Path, revision: str) -> set[bytes]:
    raw = run_git(repository, ["ls-tree", "-r", "--name-only", "-z", revision])
    return {path for path in raw.split(b"\0") if path}


def _added_paths(repository: Path, commit: str, parent: str | None) -> set[bytes]:
    if parent is None:
        arguments = [
            "diff-tree",
            "--root",
            "--no-commit-id",
            "--name-status",
            "--no-renames",
            "-r",
            "-z",
            commit,
        ]
    else:
        arguments = [
            "diff-tree",
            "--no-commit-id",
            "--name-status",
            "--no-renames",
            "-r",
            "-z",
            parent,
            commit,
        ]
    tokens = [token for token in run_git(repository, arguments).split(b"\0") if token]
    if len(tokens) % 2:
        raise Corpus50Error(f"cannot parse diff-tree name-status output for {commit}")
    return {
        tokens[index + 1]
        for index in range(0, len(tokens), 2)
        if tokens[index] == b"A"
    }


def evaluate_import_predicate(repository: Path) -> dict[str, Any]:
    commits = [
        line
        for line in git_text(
            repository, ["rev-list", "--first-parent", "--reverse", "HEAD"]
        ).splitlines()
        if line
    ]
    first_twenty = commits[:20]
    if not first_twenty:
        return {"predicate": "import", "passed": False, "reason": "no commits"}
    boundary_paths = _tree_path_bytes(repository, first_twenty[-1])
    matches: list[dict[str, Any]] = []
    for position, commit in enumerate(first_twenty, start=1):
        parent_line = git_text(
            repository, ["rev-list", "--parents", "-n", "1", commit]
        ).strip().split()
        parent = parent_line[1] if len(parent_line) >= 2 else None
        parent_paths = _tree_path_bytes(repository, parent) if parent else set()
        if len(parent_paths) > 10:
            continue
        added = _added_paths(repository, commit, parent)
        if len(added) < 500:
            continue
        supplied = len(added & boundary_paths)
        ratio = supplied / len(boundary_paths) if boundary_paths else 0.0
        if supplied * 5 < len(boundary_paths) * 4:
            continue
        message = git_text(repository, ["show", "-s", "--format=%B", commit]).rstrip()
        matches.append(
            {
                "commit": commit,
                "oldest_first_position": position,
                "parent": parent,
                "parent_tree_path_count": len(parent_paths),
                "added_path_count": len(added),
                "paths_live_after_commit_20": len(boundary_paths),
                "supplied_live_path_count": supplied,
                "supplied_live_path_fraction": ratio,
                "commit_message": message,
            }
        )
    return {
        "predicate": "import",
        "passed": bool(matches),
        "first_parent_commit_count": len(commits),
        "boundary_commit_position": len(first_twenty),
        "matches": matches,
        "evidence": matches[0] if matches else None,
    }


def evaluate_low_author_predicate(repository: Path) -> dict[str, Any]:
    raw = run_git(
        repository,
        ["log", "--all", "--use-mailmap", "-z", "--format=%aN%x00%aE"],
    )
    tokens = [token for token in raw.split(b"\0") if token]
    identities: set[tuple[str, str]] = set()
    # Depending on Git version, --format contributes a newline before every
    # record after the first even under -z.  It is a record separator, not part
    # of the mailmapped name.
    if len(tokens) % 2:
        raise Corpus50Error(f"cannot parse mailmapped author stream in {repository}")
    for index in range(0, len(tokens), 2):
        try:
            name = tokens[index].decode("utf-8").lstrip("\n")
            email = tokens[index + 1].decode("utf-8").casefold()
        except UnicodeDecodeError as error:
            raise Corpus50Error(f"non-UTF-8 mailmapped author in {repository}") from error
        identities.add((name, email))
    ordered = [
        {"mailmapped_author_name": name, "casefolded_email": email}
        for name, email in sorted(identities)
    ]
    return {
        "predicate": "low_author",
        "passed": 1 <= len(identities) <= 4,
        "unique_identity_count": len(identities),
        "identities": ordered,
    }


def measure_repository(repository: Path) -> dict[str, Any]:
    repository = repository.resolve()
    head = git_text(repository, ["rev-parse", "--verify", "HEAD^{commit}"]).strip()
    first_parent_count = int(
        git_text(repository, ["rev-list", "--first-parent", "--count", "HEAD"]).strip()
    )
    reachable_count = int(
        git_text(repository, ["rev-list", "--count", "HEAD"]).strip()
    )
    entries = list_tree(repository)
    paths = [entry.path for entry in entries]
    classification = classify_paths(paths)
    classification = classify_paths(paths)
    return {
        "repository_path": str(repository),
        "head": head,
        "first_parent_commit_count": first_parent_count,
        "reachable_commit_count": reachable_count,
        "at_least_500_first_parent_commits": first_parent_count >= 500,
        "classification": classification,
        "cap_required": reachable_count > 20_000,
    }


def inspect_repository(repository: Path) -> dict[str, Any]:
    measured = measure_repository(repository)
    paths = [entry.path for entry in list_tree(repository.resolve())]
    return {
        **measured,
        "stress_predicates": {
            "config": evaluate_config_predicate(paths),
            "catalog": evaluate_catalog_predicate(paths),
            "import": evaluate_import_predicate(repository),
            "low_author": evaluate_low_author_predicate(repository),
        },
    }


def _source_blobs(repository: Path) -> list[TreeEntry]:
    return [
        entry
        for entry in list_tree(repository)
        if entry.object_type == "blob" and is_source_path(entry.path)
    ]


PROMISOR_PREFETCH_CHUNK_SIZE = 256


def _missing_blob_object_ids(
    repository: Path, object_ids: Sequence[str]
) -> list[str]:
    """Return absent object IDs without triggering partial-clone lazy fetching."""

    if not object_ids:
        return []
    environment = os.environ.copy()
    environment["GIT_NO_LAZY_FETCH"] = "1"
    completed = subprocess.run(
        ["git", "-C", str(repository), "cat-file", "--batch-check"],
        input=("\n".join(object_ids) + "\n").encode("ascii"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise Corpus50Error(
            f"git cat-file --batch-check failed while identifying missing source "
            f"blobs in {repository}: {detail or f'exit {completed.returncode}'}"
        )
    lines = completed.stdout.splitlines()
    if len(lines) != len(object_ids):
        raise Corpus50Error(
            "git cat-file --batch-check returned an unexpected number of source-blob "
            f"records in {repository}: expected {len(object_ids)}, got {len(lines)}"
        )
    missing: list[str] = []
    for expected, line in zip(object_ids, lines):
        fields = line.split()
        expected_raw = expected.encode("ascii")
        if fields == [expected_raw, b"missing"]:
            missing.append(expected)
        elif len(fields) == 3 and fields[0] == expected_raw and fields[1] == b"blob":
            try:
                int(fields[2])
            except ValueError as error:
                raise Corpus50Error(
                    f"invalid blob size from git cat-file for {expected} in {repository}"
                ) from error
        else:
            raise Corpus50Error(
                f"unexpected git cat-file record for source blob {expected} in "
                f"{repository}: {line!r}"
            )
    return missing


def _prefetch_missing_source_blobs(
    repository: Path,
    entries: Sequence[TreeEntry],
    *,
    disk_guard: DiskGuard | None = None,
    expected_head: str | None = None,
    chunk_size: int = PROMISOR_PREFETCH_CHUNK_SIZE,
) -> None:
    """Hydrate missing source blobs in bounded native promisor-fetch batches."""

    if chunk_size <= 0:
        raise Corpus50Error(f"source-blob prefetch chunk size must be positive: {chunk_size}")
    repository = repository.resolve()
    observed_head = git_text(
        repository, ["rev-parse", "--verify", "HEAD^{commit}"]
    ).strip().casefold()
    head_before = observed_head if expected_head is None else expected_head.casefold()
    if observed_head != head_before:
        raise Corpus50Error(
            f"repository HEAD changed before source-blob prefetch in {repository}: "
            f"expected={head_before}, observed={observed_head}"
        )
    unique_object_ids = list(dict.fromkeys(entry.object_id for entry in entries))
    missing = _missing_blob_object_ids(repository, unique_object_ids)
    chunk_count = (len(missing) + chunk_size - 1) // chunk_size

    if not missing:
        head_after = git_text(
            repository, ["rev-parse", "--verify", "HEAD^{commit}"]
        ).strip().casefold()
        if head_after != head_before:
            raise Corpus50Error(
                f"repository HEAD changed during source-blob prefetch in {repository}: "
                f"before={head_before}, after={head_after}"
            )
        return

    command = [
        "git",
        "-C",
        str(repository),
        "-c",
        "fetch.negotiationAlgorithm=noop",
        "fetch",
        "origin",
        "--no-tags",
        "--no-write-fetch-head",
        "--recurse-submodules=no",
        "--filter=blob:none",
        "--stdin",
    ]
    for offset in range(0, len(missing), chunk_size):
        chunk = missing[offset : offset + chunk_size]
        chunk_number = offset // chunk_size + 1
        if disk_guard is not None:
            disk_guard.check()
        try:
            completed = subprocess.run(
                command,
                input=("\n".join(chunk) + "\n").encode("ascii"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        except OSError as error:
            if disk_guard is not None:
                disk_guard.check()
            raise Corpus50Error(
                f"could not start source-blob promisor prefetch chunk "
                f"{chunk_number}/{chunk_count} ({len(chunk)} objects) in "
                f"{repository}: {error}"
            ) from error
        if disk_guard is not None:
            disk_guard.check()
        head_after = git_text(
            repository, ["rev-parse", "--verify", "HEAD^{commit}"]
        ).strip().casefold()
        if head_after != head_before:
            raise Corpus50Error(
                f"repository HEAD changed during source-blob prefetch chunk "
                f"{chunk_number}/{chunk_count} in {repository}: before={head_before}, "
                f"after={head_after}"
            )
        if completed.returncode != 0:
            stderr = completed.stderr.decode("utf-8", errors="replace").strip()
            stdout = completed.stdout.decode("utf-8", errors="replace").strip()
            detail = stderr or stdout or f"exit {completed.returncode} with no output"
            raise Corpus50Error(
                f"source-blob promisor prefetch chunk {chunk_number}/{chunk_count} "
                f"failed for {len(chunk)} objects in {repository} "
                f"(first={chunk[0]}, last={chunk[-1]}, exit={completed.returncode}): "
                f"{detail}"
            )


def _iter_blob_contents(
    repository: Path, entries: Sequence[TreeEntry]
) -> Iterator[tuple[TreeEntry, bytes]]:
    process = subprocess.Popen(
        ["git", "-C", str(repository), "cat-file", "--batch"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    try:
        for entry in entries:
            process.stdin.write(entry.object_id.encode("ascii") + b"\n")
            process.stdin.flush()
            header = process.stdout.readline()
            fields = header.rstrip(b"\n").split()
            if len(fields) != 3 or fields[1] != b"blob":
                raise Corpus50Error(
                    f"git cat-file did not return blob {entry.object_id} ({header!r})"
                )
            size = int(fields[2])
            body = process.stdout.read(size)
            terminator = process.stdout.read(1)
            if len(body) != size or terminator != b"\n":
                raise Corpus50Error(f"truncated git cat-file output for {entry.path}")
            yield entry, body
        process.stdin.close()
        return_code = process.wait(timeout=60)
        if return_code != 0:
            detail = process.stderr.read().decode("utf-8", errors="replace")
            raise Corpus50Error(f"git cat-file failed in {repository}: {detail.strip()}")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def _sanitize_source(text: str) -> list[str]:
    """Conservatively blank common comments and strings while retaining columns."""

    lines = text.splitlines()
    sanitized: list[str] = []
    block_end: str | None = None
    triple_end: str | None = None
    for line in lines:
        output = list(line)
        index = 0
        while index < len(line):
            if block_end is not None:
                end = line.find(block_end, index)
                stop = len(line) if end < 0 else end + len(block_end)
                for position in range(index, stop):
                    output[position] = " "
                index = stop
                if end >= 0:
                    block_end = None
                continue
            if triple_end is not None:
                end = line.find(triple_end, index)
                stop = len(line) if end < 0 else end + len(triple_end)
                for position in range(index, stop):
                    output[position] = " "
                index = stop
                if end >= 0:
                    triple_end = None
                continue
            if line.startswith("/*", index):
                block_end = "*/"
                continue
            if line.startswith("<!--", index):
                block_end = "-->"
                continue
            if line.startswith("'''", index) or line.startswith('\"\"\"', index):
                triple_end = line[index : index + 3]
                continue
            if any(line.startswith(marker, index) for marker in ("//", "#", "--")):
                for position in range(index, len(line)):
                    output[position] = " "
                index = len(line)
                continue
            if line[index] in {"'", '\"', "`"}:
                quote = line[index]
                output[index] = " "
                index += 1
                escaped = False
                while index < len(line):
                    output[index] = " "
                    character = line[index]
                    index += 1
                    if escaped:
                        escaped = False
                    elif character == "\\":
                        escaped = True
                    elif character == quote:
                        break
                continue
            index += 1
        sanitized.append("".join(output))
    return sanitized


IDENTIFIER_RE = re.compile(r"[^\W\d]\w*", re.UNICODE)
DECLARATION_KEYWORDS = (
    "def",
    "class",
    "function",
    "fn",
    "func",
    "let",
    "const",
    "var",
    "type",
    "struct",
    "enum",
    "interface",
    "trait",
    "module",
    "mod",
    "val",
    "object",
    "sub",
    "my",
    "our",
    "local",
)
DECLARATION_KEYWORD_SET = frozenset(DECLARATION_KEYWORDS)
DECLARATION_WHITESPACE_RE = re.compile(r"\s*", re.UNICODE)
DECLARATION_WHITESPACE_PLUS_RE = re.compile(r"\s+", re.UNICODE)
DECLARATION_ANNOTATION_DELIMITER_RE = re.compile(r"[=,:]")
WORD_CHARACTER_RE = re.compile(r"\w", re.UNICODE)


def _has_non_ascii_letter(token: str) -> bool:
    return any(
        ord(character) > 127 and unicodedata.category(character).startswith("L")
        for character in token
    )


def _has_left_word_boundary(line: str, position: int) -> bool:
    return position == 0 or WORD_CHARACTER_RE.match(line, position - 1) is None


def _is_assignment_operator_at(line: str, position: int) -> bool:
    if line.startswith(":=", position):
        return True
    return (
        position < len(line)
        and line[position] == "="
        and (position + 1 == len(line) or line[position + 1] != "=")
    )


def _assignment_follows_identifier(line: str, identifier_end: int) -> bool:
    position = DECLARATION_WHITESPACE_RE.match(line, identifier_end).end()
    if _is_assignment_operator_at(line, position):
        return True
    if position == len(line) or line[position] != ":":
        return False

    # Preserve the original annotation grammar exactly: after the first colon,
    # at least one character other than ``=``, ``,`` or ``:`` must precede the
    # first delimiter, and that delimiter itself must be an assignment operator.
    # Stopping at the first delimiter also keeps consecutive annotation scans
    # disjoint instead of repeatedly searching the rest of a long line.
    delimiter = DECLARATION_ANNOTATION_DELIMITER_RE.search(line, position + 1)
    if delimiter is None or delimiter.start() == position + 1:
        return False
    return _is_assignment_operator_at(line, delimiter.start())


def _declaration_tokens(line: str) -> set[str]:
    declarations: set[str] = set()
    previous: re.Match[str] | None = None
    previous_has_left_boundary = False
    for match in IDENTIFIER_RE.finditer(line):
        has_left_boundary = _has_left_word_boundary(line, match.start())
        if has_left_boundary:
            if (
                previous is not None
                and previous_has_left_boundary
                and previous.group(0) in DECLARATION_KEYWORD_SET
                and DECLARATION_WHITESPACE_PLUS_RE.fullmatch(
                    line, previous.end(), match.start()
                )
                is not None
            ):
                declarations.add(match.group(0))
            if _assignment_follows_identifier(line, match.end()):
                declarations.add(match.group(0))
        previous = match
        previous_has_left_boundary = has_left_boundary
    return declarations


def scan_non_english_identifiers(
    repository: Path, *, disk_guard: DiskGuard | None = None
) -> dict[str, Any]:
    repository = repository.resolve()
    repository_head = git_text(
        repository, ["rev-parse", "--verify", "HEAD^{commit}"]
    ).strip().casefold()
    evidence_by_token: dict[str, dict[str, Any]] = {}
    invalid_utf8_paths: list[str] = []
    blobs = _source_blobs(repository)
    if disk_guard is not None:
        disk_guard.check()
    _prefetch_missing_source_blobs(
        repository,
        blobs,
        disk_guard=disk_guard,
        expected_head=repository_head,
    )
    for entry, body in _iter_blob_contents(repository, blobs):
        if disk_guard is not None:
            disk_guard.check()
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            invalid_utf8_paths.append(entry.path)
            continue
        raw_lines = text.splitlines()
        sanitized = _sanitize_source(text)
        declarations: dict[str, tuple[int, str]] = {}
        for line_number, line in enumerate(sanitized, start=1):
            for token in _declaration_tokens(line):
                if _has_non_ascii_letter(token) and token not in declarations:
                    declarations[token] = (
                        line_number,
                        raw_lines[line_number - 1].strip()[:500],
                    )
            present = {
                match.group(0)
                for match in IDENTIFIER_RE.finditer(line)
                if _has_non_ascii_letter(match.group(0))
            }
            for token in present:
                if token in evidence_by_token or token not in declarations:
                    continue
                declaration_line, declaration_excerpt = declarations[token]
                if line_number <= declaration_line:
                    continue
                evidence_by_token[token] = {
                    "token": token,
                    "path": entry.path,
                    "declaration_line": declaration_line,
                    "declaration_excerpt": declaration_excerpt,
                    "later_use_line": line_number,
                    "later_use_excerpt": raw_lines[line_number - 1].strip()[:500],
                }
    evidence = [evidence_by_token[token] for token in sorted(evidence_by_token)]
    return {
        "predicate": "non_english",
        "repository_head": repository_head,
        "repository_path": str(repository),
        "passed": None,
        "machine_pass": len(evidence) >= 10,
        "requires_human_review": True,
        "machine_candidate_token_count": len(evidence),
        "source_blob_count_scanned": len(blobs),
        "invalid_utf8_paths": invalid_utf8_paths,
        "evidence": evidence,
        "review_constraint": (
            "human review may reject syntax/string/comment false positives but may never "
            "promote a machine predicate miss"
        ),
    }


def finalize_non_english_review(
    scan: Mapping[str, Any], accepted_tokens: Sequence[str]
) -> dict[str, Any]:
    if scan.get("predicate") != "non_english":
        raise Corpus50Error("review input is not a non-English identifier scan")
    evidence = scan.get("evidence")
    if not isinstance(evidence, list):
        raise Corpus50Error("review input has no evidence list")
    known = {
        item.get("token"): item
        for item in evidence
        if isinstance(item, dict) and isinstance(item.get("token"), str)
    }
    accepted = sorted(set(accepted_tokens))
    unknown = sorted(set(accepted) - set(known))
    if unknown:
        raise Corpus50Error(
            "human review cannot promote tokens absent from machine evidence: "
            + ", ".join(unknown)
        )
    machine_pass = scan.get("machine_pass") is True
    return {
        **dict(scan),
        "passed": machine_pass and len(accepted) >= 10,
        "reviewed_at_utc": utc_now(),
        "accepted_token_count": len(accepted),
        "accepted_evidence": [known[token] for token in accepted],
        "rejected_tokens": sorted(set(known) - set(accepted)),
        "requires_human_review": False,
    }


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, start=1):
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise Corpus50Error(f"non-object JSONL row at {path}:{line_number}")
                rows.append(value)
    except (OSError, json.JSONDecodeError) as error:
        raise Corpus50Error(f"cannot read JSONL {path}: {error}") from error
    return rows


def stress_candidate_order(
    frame_root: Path,
    stress_key: str,
    *,
    excluded_repo_ids: Iterable[int] = (),
) -> list[dict[str, Any]]:
    if stress_key not in STRESS_KEYS:
        raise Corpus50Error(f"unknown stress key: {stress_key}")
    paths = acquisition_paths(frame_root.resolve())
    excluded = {int(value) for value in excluded_repo_ids}
    own = read_jsonl(paths["frames"] / f"stress-{stress_key}.jsonl")
    all_stress: dict[int, dict[str, Any]] = {}
    for key in STRESS_KEYS:
        for row in read_jsonl(paths["frames"] / f"stress-{key}.jsonl"):
            all_stress.setdefault(int(row["repo_id"]), row)
    base = read_jsonl(paths["frames"] / "base-active.jsonl")
    output: list[dict[str, Any]] = []
    seen = set(excluded)

    def extend(rows: Iterable[dict[str, Any]], stage: str) -> None:
        ranked = sorted(
            rows,
            key=lambda row: (
                priority_key(stress_key, int(row["repo_id"])),
                int(row["repo_id"]),
            ),
        )
        for row in ranked:
            repo_id = int(row["repo_id"])
            if repo_id in seen:
                continue
            seen.add(repo_id)
            output.append(
                {
                    **row,
                    "priority_key": priority_key(stress_key, repo_id),
                    "stress_key": stress_key,
                    "fallback_stage": stage,
                    "candidate_order": len(output) + 1,
                }
            )

    extend(own, "own_stress_frame")
    extend(all_stress.values(), "union_all_stress_frames")
    extend(base, "base_active_frame")
    return output


@dataclass
class _FlowEdge:
    target: int
    reverse: int
    capacity: int


class _Dinic:
    def __init__(self, nodes: int) -> None:
        self.graph: list[list[_FlowEdge]] = [[] for _ in range(nodes)]

    def add_edge(self, source: int, target: int, capacity: int) -> None:
        forward = _FlowEdge(target, len(self.graph[target]), capacity)
        reverse = _FlowEdge(source, len(self.graph[source]), 0)
        self.graph[source].append(forward)
        self.graph[target].append(reverse)

    def maximum_flow(self, source: int, sink: int) -> int:
        total = 0
        while True:
            level = [-1] * len(self.graph)
            level[source] = 0
            queue = deque([source])
            while queue:
                node = queue.popleft()
                for edge in self.graph[node]:
                    if edge.capacity and level[edge.target] < 0:
                        level[edge.target] = level[node] + 1
                        queue.append(edge.target)
            if level[sink] < 0:
                return total
            cursor = [0] * len(self.graph)

            def send(node: int, amount: int) -> int:
                if node == sink:
                    return amount
                while cursor[node] < len(self.graph[node]):
                    edge = self.graph[node][cursor[node]]
                    if edge.capacity and level[edge.target] == level[node] + 1:
                        pushed = send(edge.target, min(amount, edge.capacity))
                        if pushed:
                            edge.capacity -= pushed
                            self.graph[edge.target][edge.reverse].capacity += pushed
                            return pushed
                    cursor[node] += 1
                return 0

            while True:
                pushed = send(source, 10**9)
                if not pushed:
                    break
                total += pushed


def _exact_feasible(
    capacities: Mapping[tuple[str, str], int],
    language_remaining: Mapping[str, int],
    layout_remaining: Mapping[str, int],
) -> bool:
    if any(value < 0 for value in language_remaining.values()) or any(
        value < 0 for value in layout_remaining.values()
    ):
        return False
    required = sum(language_remaining.values())
    if required != sum(layout_remaining.values()):
        return False
    languages = list(LANGUAGE_QUOTAS)
    layouts = list(LAYOUT_QUOTAS)
    source = 0
    language_offset = 1
    layout_offset = language_offset + len(languages)
    sink = layout_offset + len(layouts)
    flow = _Dinic(sink + 1)
    for index, language in enumerate(languages):
        flow.add_edge(source, language_offset + index, int(language_remaining[language]))
    for language_index, language in enumerate(languages):
        for layout_index, layout in enumerate(layouts):
            capacity = int(capacities.get((language, layout), 0))
            if capacity:
                flow.add_edge(
                    language_offset + language_index,
                    layout_offset + layout_index,
                    capacity,
                )
    for index, layout in enumerate(layouts):
        flow.add_edge(layout_offset + index, sink, int(layout_remaining[layout]))
    return flow.maximum_flow(source, sink) == required


@dataclass
class _CostEdge:
    target: int
    reverse: int
    capacity: int
    cost: int


class _MinCostFlow:
    def __init__(self, nodes: int) -> None:
        self.graph: list[list[_CostEdge]] = [[] for _ in range(nodes)]

    def add_edge(self, source: int, target: int, capacity: int, cost: int) -> None:
        forward = _CostEdge(target, len(self.graph[target]), capacity, cost)
        reverse = _CostEdge(source, len(self.graph[source]), 0, -cost)
        self.graph[source].append(forward)
        self.graph[target].append(reverse)

    def flow(self, source: int, sink: int, required: int) -> tuple[int, int]:
        sent = 0
        cost = 0
        node_count = len(self.graph)
        while sent < required:
            distance = [10**12] * node_count
            parent: list[tuple[int, int] | None] = [None] * node_count
            distance[source] = 0
            # Bellman-Ford is tiny here (15 nodes) and safely handles the -1
            # convex marginal edges and residual edges.
            for _ in range(node_count - 1):
                changed = False
                for node, edges in enumerate(self.graph):
                    if distance[node] == 10**12:
                        continue
                    for edge_index, edge in enumerate(edges):
                        candidate = distance[node] + edge.cost
                        if edge.capacity and candidate < distance[edge.target]:
                            distance[edge.target] = candidate
                            parent[edge.target] = (node, edge_index)
                            changed = True
                if not changed:
                    break
            if parent[sink] is None:
                break
            amount = required - sent
            node = sink
            while node != source:
                prior, edge_index = parent[node]  # type: ignore[misc]
                amount = min(amount, self.graph[prior][edge_index].capacity)
                node = prior
            node = sink
            while node != source:
                prior, edge_index = parent[node]  # type: ignore[misc]
                edge = self.graph[prior][edge_index]
                edge.capacity -= amount
                self.graph[node][edge.reverse].capacity += amount
                node = prior
            sent += amount
            cost += amount * distance[sink]
        return sent, cost


def _minimum_final_deviation(
    capacities: Mapping[tuple[str, str], int],
    current_languages: Mapping[str, int],
    current_layouts: Mapping[str, int],
    remaining: int,
) -> int | None:
    if remaining < 0 or sum(capacities.values()) < remaining:
        return None
    languages = list(LANGUAGE_QUOTAS)
    layouts = list(LAYOUT_QUOTAS)
    source = 0
    language_offset = 1
    layout_offset = language_offset + len(languages)
    sink = layout_offset + len(layouts)
    flow = _MinCostFlow(sink + 1)
    for index, language in enumerate(languages):
        current = int(current_languages.get(language, 0))
        quota = LANGUAGE_QUOTAS[language]
        for increment in range(1, remaining + 1):
            before = abs(current + increment - 1 - quota)
            after = abs(current + increment - quota)
            flow.add_edge(source, language_offset + index, 1, after - before)
    for language_index, language in enumerate(languages):
        for layout_index, layout in enumerate(layouts):
            capacity = int(capacities.get((language, layout), 0))
            if capacity:
                flow.add_edge(
                    language_offset + language_index,
                    layout_offset + layout_index,
                    capacity,
                    0,
                )
    for index, layout in enumerate(layouts):
        current = int(current_layouts.get(layout, 0))
        quota = LAYOUT_QUOTAS[layout]
        for increment in range(1, remaining + 1):
            before = abs(current + increment - 1 - quota)
            after = abs(current + increment - quota)
            flow.add_edge(layout_offset + index, sink, 1, after - before)
    sent, incremental_cost = flow.flow(source, sink, remaining)
    if sent != remaining:
        return None
    starting_cost = sum(
        abs(int(current_languages.get(key, 0)) - quota)
        for key, quota in LANGUAGE_QUOTAS.items()
    ) + sum(
        abs(int(current_layouts.get(key, 0)) - quota)
        for key, quota in LAYOUT_QUOTAS.items()
    )
    return starting_cost + incremental_cost


def _candidate_cell(row: Mapping[str, Any]) -> tuple[str, str]:
    language = row.get("language_stratum")
    layout = row.get("layout_stratum")
    if language is None and isinstance(row.get("classification"), dict):
        language = row["classification"].get("language_stratum")
    if layout is None and isinstance(row.get("classification"), dict):
        layout = row["classification"].get("layout_stratum")
    if language not in LANGUAGE_QUOTAS:
        raise Corpus50Error(f"candidate has invalid language stratum: {language!r}")
    if layout not in LAYOUT_QUOTAS:
        raise Corpus50Error(f"candidate has invalid layout stratum: {layout!r}")
    return str(language), str(layout)


def solve_base_selection(
    candidates: Sequence[Mapping[str, Any]], *, active_frame_exhausted: bool = False
) -> dict[str, Any]:
    """Apply first-feasible-prefix and lexicographic selection exactly.

    ``candidates`` contains only candidates that passed clone, 500-first-parent,
    and duplicate-HEAD screening, but retains each row's original ``base_rank``.
    Every rejected candidate remains represented in the external selection
    ledger rather than being passed to this solver.
    """

    rows = [dict(row) for row in candidates]
    if len(rows) < 35:
        if active_frame_exhausted:
            raise Corpus50Error("exhausted active frame has fewer than 35 eligible candidates")
        return {
            "schema_version": SCHEMA_VERSION,
            "rule_id": RULE_ID,
            "status": "not_yet_feasible",
            "active_frame_exhausted": False,
            "eligible_candidate_count_considered": len(rows),
            "selected": [],
        }
    rows.sort(key=lambda row: (str(row["priority_key"]), int(row["repo_id"])))
    seen_ids: set[int] = set()
    previous_rank = 0
    for index, row in enumerate(rows, start=1):
        repo_id = int(row["repo_id"])
        if repo_id in seen_ids:
            raise Corpus50Error(f"duplicate eligible base repository id: {repo_id}")
        seen_ids.add(repo_id)
        expected_key = priority_key("base", repo_id)
        if row.get("priority_key") != expected_key:
            raise Corpus50Error(f"wrong base priority key for repository id {repo_id}")
        rank = int(row.get("base_rank", index))
        if rank <= previous_rank:
            raise Corpus50Error("eligible candidates are not in increasing base-frame rank")
        previous_rank = rank
        _candidate_cell(row)

    capacities: Counter[tuple[str, str]] = Counter()
    prefix_end: int | None = None
    for index, row in enumerate(rows):
        capacities[_candidate_cell(row)] += 1
        if index + 1 >= 35 and _exact_feasible(
            capacities, LANGUAGE_QUOTAS, LAYOUT_QUOTAS
        ):
            prefix_end = index + 1
            break

    if prefix_end is not None:
        prefix = rows[:prefix_end]
        suffix_capacities = Counter(_candidate_cell(row) for row in prefix)
        language_remaining = dict(LANGUAGE_QUOTAS)
        layout_remaining = dict(LAYOUT_QUOTAS)
        selected: list[dict[str, Any]] = []
        for row in prefix:
            cell = _candidate_cell(row)
            suffix_capacities[cell] -= 1
            language, layout = cell
            proposed_languages = dict(language_remaining)
            proposed_layouts = dict(layout_remaining)
            proposed_languages[language] -= 1
            proposed_layouts[layout] -= 1
            if _exact_feasible(
                suffix_capacities, proposed_languages, proposed_layouts
            ):
                selected.append(row)
                language_remaining = proposed_languages
                layout_remaining = proposed_layouts
        if len(selected) != 35:
            raise AssertionError("exact greedy selection did not produce 35 members")
        method = "first feasible prefix; lexicographically smallest priority tuple"
        minimum_deviation = 0
        first_feasible_base_rank = int(prefix[-1].get("base_rank", prefix_end))
    elif not active_frame_exhausted:
        return {
            "schema_version": SCHEMA_VERSION,
            "rule_id": RULE_ID,
            "status": "not_yet_feasible",
            "active_frame_exhausted": False,
            "eligible_candidate_count_considered": len(rows),
            "selected": [],
        }
    else:
        all_capacities = Counter(_candidate_cell(row) for row in rows)
        optimum = _minimum_final_deviation(all_capacities, {}, {}, 35)
        if optimum is None:
            raise Corpus50Error("full eligible base frame cannot supply 35 members")
        suffix_capacities = Counter(all_capacities)
        current_languages: Counter[str] = Counter()
        current_layouts: Counter[str] = Counter()
        selected = []
        for row in rows:
            cell = _candidate_cell(row)
            suffix_capacities[cell] -= 1
            if len(selected) == 35:
                continue
            language, layout = cell
            proposed_languages = Counter(current_languages)
            proposed_layouts = Counter(current_layouts)
            proposed_languages[language] += 1
            proposed_layouts[layout] += 1
            conditional = _minimum_final_deviation(
                suffix_capacities,
                proposed_languages,
                proposed_layouts,
                35 - len(selected) - 1,
            )
            if conditional == optimum:
                selected.append(row)
                current_languages = proposed_languages
                current_layouts = proposed_layouts
        if len(selected) != 35:
            raise AssertionError("fallback greedy selection did not produce 35 members")
        method = "minimum total absolute margin deviation over exhausted active frame"
        minimum_deviation = optimum
        first_feasible_base_rank = None

    selected_languages = Counter(_candidate_cell(row)[0] for row in selected)
    selected_layouts = Counter(_candidate_cell(row)[1] for row in selected)
    language_deviations = {
        key: selected_languages[key] - quota for key, quota in LANGUAGE_QUOTAS.items()
    }
    layout_deviations = {
        key: selected_layouts[key] - quota for key, quota in LAYOUT_QUOTAS.items()
    }
    selected_output = []
    for rank, row in enumerate(selected, start=1):
        selected_output.append({**row, "base_selection_rank": rank})
    return {
        "schema_version": SCHEMA_VERSION,
        "rule_id": RULE_ID,
        "status": "selected",
        "active_frame_exhausted": prefix_end is None,
        "method": method,
        "first_feasible_base_rank": first_feasible_base_rank,
        "eligible_candidate_count_considered": (
            prefix_end if prefix_end is not None else len(rows)
        ),
        "minimum_total_absolute_margin_deviation": minimum_deviation,
        "realised_language_counts": dict(selected_languages),
        "language_deviations": language_deviations,
        "realised_layout_counts": dict(selected_layouts),
        "layout_deviations": layout_deviations,
        "selected": selected_output,
    }


EXPECTED_STRESS = {
    "config": "configuration-only repository with almost no code",
    "catalog": (
        "flat/sharded catalog: hierarchy carries component identity or shard, "
        "not category semantics"
    ),
    "import": "one qualifying bulk import within the oldest 20 first-parent commits",
    "low_author": "one to four unique mailmapped author identities",
    "non_english": "at least ten reviewed non-ASCII-letter identifiers with declaration/use evidence",
}


def _selected_rows(path: Path) -> list[dict[str, Any]]:
    _metadata, rows = _selected_document(path)
    return rows


def _selected_document(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if path.suffix.casefold() == ".jsonl":
        return {}, read_jsonl(path)
    value = read_json(path)
    if isinstance(value, list):
        rows = value
        metadata: dict[str, Any] = {}
    elif isinstance(value, dict) and isinstance(value.get("selected"), list):
        rows = value["selected"]
        metadata = dict(value)
        metadata.pop("selected", None)
    else:
        raise Corpus50Error(f"selected-member file has no list or selected[]: {path}")
    if not all(isinstance(row, dict) for row in rows):
        raise Corpus50Error(f"selected-member file contains non-object rows: {path}")
    return metadata, [dict(row) for row in rows]


def _member_from_selected(row: Mapping[str, Any], cohort: str) -> dict[str, Any]:
    name = row.get("name") or row.get("clone_name")
    if not isinstance(name, str) or name.count("/") != 1:
        raise Corpus50Error(f"selected {cohort} member lacks owner/name: {name!r}")
    repo_id = _parse_decimal(row.get("repo_id"), field="repo_id", context=name)
    head = row.get("head")
    if not isinstance(head, str) or not re.fullmatch(r"[0-9a-fA-F]{40,64}", head):
        raise Corpus50Error(f"selected addition {name} lacks a frozen commit hash")
    first_parent_count = int(row.get("first_parent_commit_count", 0))
    if first_parent_count < 500:
        raise Corpus50Error(
            f"selected addition {name} has only {first_parent_count} first-parent commits"
        )
    classification = row.get("classification")
    if not isinstance(classification, dict):
        classification = {}
    language = row.get("primary_language", classification.get("primary_language"))
    language_stratum = row.get(
        "language_stratum", classification.get("language_stratum")
    )
    layout = row.get("layout_stratum", classification.get("layout_stratum"))
    if not isinstance(language, str) or language_stratum not in LANGUAGE_QUOTAS:
        raise Corpus50Error(f"selected addition {name} lacks a valid local language classification")
    if layout not in LAYOUT_QUOTAS:
        raise Corpus50Error(f"selected addition {name} lacks a valid local layout classification")
    tracked_count = row.get(
        "tracked_path_count", classification.get("tracked_path_count")
    )
    source_count = row.get("source_path_count", classification.get("source_path_count"))
    if not isinstance(tracked_count, int) or not isinstance(source_count, int):
        raise Corpus50Error(f"selected addition {name} lacks tracked/source path counts")
    priority = row.get("priority_key")
    priority_kind = "base" if cohort == "base" else str(row.get("stress_key"))
    if priority != priority_key(priority_kind, repo_id):
        raise Corpus50Error(f"selected addition {name} has a wrong or missing priority key")
    member = {
        "repo_id": repo_id,
        "slug": slug_for_name(name),
        "name": name,
        "url": row.get("url") or f"https://github.com/{name}.git",
        "cohort": cohort,
        "head": head.lower(),
        "first_parent_commit_count": first_parent_count,
        "reachable_commit_count": (
            int(row["reachable_commit_count"])
            if row.get("reachable_commit_count") is not None
            else None
        ),
        "primary_language": language,
        "language_stratum": language_stratum,
        "layout_stratum": layout,
        "tracked_path_count": tracked_count,
        "source_path_count": source_count,
        "priority_key": priority,
        "selection_status": "selected",
    }
    reachable = member["reachable_commit_count"]
    member["capped"] = bool(reachable is not None and reachable > 20_000)
    if cohort == "stress":
        stress_key = row.get("stress_key")
        if stress_key not in STRESS_KEYS:
            raise Corpus50Error(f"selected stress member {name} has invalid stress key")
        if row.get("stress_predicate_passed") is not True and not (
            isinstance(row.get("predicate_result"), dict)
            and row["predicate_result"].get("passed") is True
        ):
            raise Corpus50Error(
                f"selected stress member {name} lacks a durable passing predicate result"
            )
        member["stress_key"] = stress_key
        member["expected_stress"] = EXPECTED_STRESS[stress_key]
        if isinstance(row.get("predicate_result"), dict):
            member["stress_predicate"] = row["predicate_result"]
        if row.get("predicate_artifact") is not None:
            member["stress_predicate_artifact"] = row["predicate_artifact"]
    return member


def _anchor_member(project_root: Path, selection_order: int, name: str) -> dict[str, Any]:
    repository = project_root / "corpus" / "_clones" / slug_for_name(name)
    if not repository.exists():
        raise Corpus50Error(f"retained anchor clone is missing: {repository}")
    measured = measure_repository(repository)
    classification = measured["classification"]
    reachable = int(measured["reachable_commit_count"])
    return {
        "selection_order": selection_order,
        "slug": slug_for_name(name),
        "name": name,
        "url": f"https://github.com/{name}.git",
        "cohort": "retained_anchor",
        "head": measured["head"],
        "first_parent_commit_count": measured["first_parent_commit_count"],
        "reachable_commit_count": reachable,
        "primary_language": classification["primary_language"],
        "language_stratum": classification["language_stratum"],
        "layout_stratum": classification["layout_stratum"],
        "tracked_path_count": classification["tracked_path_count"],
        "source_path_count": classification["source_path_count"],
        "capped": reachable > 20_000,
        "selection_status": "selected",
    }


def assemble_corpus_manifest(
    *,
    frame_root: Path,
    stress_selected_path: Path,
    base_selected_path: Path,
    project_root: Path = PROJECT_ROOT,
    accounted_paths: Sequence[Path] = (),
    output_path: Path = DEFAULT_CORPUS_MANIFEST,
) -> dict[str, Any]:
    _stress_metadata, stress_rows = _selected_document(stress_selected_path)
    base_metadata, base_rows = _selected_document(base_selected_path)
    if len(stress_rows) != 5:
        raise Corpus50Error(f"stress selection has {len(stress_rows)} rows, expected 5")
    if len(base_rows) != 35:
        raise Corpus50Error(f"base selection has {len(base_rows)} rows, expected 35")
    stress_by_key: dict[str, Mapping[str, Any]] = {}
    for row in stress_rows:
        key = row.get("stress_key")
        if key in stress_by_key:
            raise Corpus50Error(f"stress slot is duplicated: {key}")
        if key not in STRESS_KEYS:
            raise Corpus50Error(f"unknown stress slot in selected rows: {key}")
        stress_by_key[str(key)] = row
    if set(stress_by_key) != set(STRESS_KEYS):
        raise Corpus50Error("selected stress rows do not fill all five fixed slots")

    members = [
        _anchor_member(project_root.resolve(), index, name)
        for index, name in enumerate(RETAINED_ANCHORS, start=1)
    ]
    for key in STRESS_KEYS:
        members.append(_member_from_selected(stress_by_key[key], "stress"))
    ordered_base = sorted(
        base_rows,
        key=lambda row: (str(row.get("priority_key")), int(row.get("repo_id"))),
    )
    members.extend(_member_from_selected(row, "base") for row in ordered_base)
    for selection_order, member in enumerate(members, start=1):
        member["selection_order"] = selection_order

    names = [member["name"].casefold() for member in members]
    if len(set(names)) != 50:
        raise Corpus50Error("selected manifest contains duplicate repository names")
    repo_ids = [member.get("repo_id") for member in members if member.get("repo_id") is not None]
    if len(repo_ids) != len(set(repo_ids)):
        raise Corpus50Error("selected additions contain duplicate immutable repository IDs")
    heads = [member.get("head") for member in members]
    if len(heads) != len(set(heads)):
        raise Corpus50Error("selected manifest contains duplicate exact HEADs")

    base_language_counts = Counter(member["language_stratum"] for member in members if member["cohort"] == "base")
    base_layout_counts = Counter(member["layout_stratum"] for member in members if member["cohort"] == "base")
    language_deviations = {
        key: base_language_counts[key] - quota for key, quota in LANGUAGE_QUOTAS.items()
    }
    layout_deviations = {
        key: base_layout_counts[key] - quota for key, quota in LAYOUT_QUOTAS.items()
    }
    total_deviation = sum(abs(value) for value in language_deviations.values()) + sum(
        abs(value) for value in layout_deviations.values()
    )
    if total_deviation:
        if not (
            base_metadata.get("active_frame_exhausted") is True
            and base_metadata.get("minimum_total_absolute_margin_deviation")
            == total_deviation
            and str(base_metadata.get("method", "")).startswith("minimum total absolute")
        ):
            raise Corpus50Error(
                "non-exact base margins are allowed only from the documented exhausted-frame "
                "minimum-deviation solver"
            )

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "rule_id": RULE_ID,
        "scope_name": SCOPE_NAME,
        "listing_dates": {
            "base": BASE_LISTING_DATE,
            "stress": STRESS_LISTING_DATE,
        },
        "seed": SEED,
        "frame_root": str(frame_root.resolve()),
        "accounted_paths": [str(path.resolve()) for path in accounted_paths],
        "disk_cap_bytes": HARD_CAP_BYTES,
        "base_realised_language_counts": dict(base_language_counts),
        "base_realised_layout_counts": dict(base_layout_counts),
        "base_language_deviations": language_deviations,
        "base_layout_deviations": layout_deviations,
        "base_total_absolute_margin_deviation": total_deviation,
        "base_selection_method": base_metadata.get("method"),
        "members": members,
    }
    atomic_write_json(output_path.resolve(), manifest)
    return manifest


def append_selection_event(
    ledger_path: Path,
    event: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(event.get("event_type"), str):
        raise Corpus50Error("selection ledger event requires string event_type")
    if event.get("rule_id") not in (None, RULE_ID):
        raise Corpus50Error("selection ledger event has the wrong rule_id")
    return append_hash_chained(ledger_path, event)


def append_unique_jsonl(path: Path, row: Mapping[str, Any], *, key: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    wanted = row.get(key)
    if wanted is None:
        raise Corpus50Error(f"cannot append row without unique key {key}")
    if path.exists():
        for existing in read_jsonl(path):
            if existing.get(key) == wanted:
                if canonical_json_bytes(existing) != canonical_json_bytes(dict(row)):
                    raise Corpus50Error(
                        f"{path} already has a different row with {key}={wanted!r}"
                    )
                return
    with path.open("ab") as stream:
        stream.write(canonical_json_bytes(dict(row)))
        stream.flush()
        os.fsync(stream.fileno())


def _candidate_from_listing(path: Path, rank: int) -> dict[str, Any]:
    if rank < 1:
        raise Corpus50Error("candidate rank is one-based and must be positive")
    rows = read_jsonl(path)
    for row in rows:
        observed = row.get("candidate_order", row.get("base_rank"))
        if observed is not None and int(observed) == rank:
            return row
    if rank <= len(rows):
        return rows[rank - 1]
    raise Corpus50Error(f"candidate rank {rank} is absent from {path}")


def _validate_github_clone_url(url: str, name: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.netloc.casefold() != "github.com":
        raise Corpus50Error(f"candidate {name} does not use a public HTTPS GitHub clone URL")
    expected_path = f"/{name}".casefold()
    actual_path = parsed.path.removesuffix(".git").casefold()
    if actual_path != expected_path:
        raise Corpus50Error(
            f"candidate name/clone URL disagree: name={name!r}, url={url!r}"
        )


def _clone_size(path: Path) -> int:
    try:
        return directory_size(path)
    except OSError:
        return 0


def _safe_remove_owned_clone(
    destination: Path, clone_root: Path, ownership: Mapping[str, Any]
) -> None:
    resolved_root = clone_root.resolve()
    resolved = destination.resolve()
    if resolved.parent != resolved_root or resolved == resolved_root:
        raise Corpus50Error(f"refusing cleanup outside exact clone root: {resolved}")
    if ownership.get("destination") != str(resolved):
        raise Corpus50Error(f"screening ownership record does not match {resolved}")
    if destination.exists():
        def clear_readonly_and_retry(function: Any, path: str, _error: Any) -> None:
            # Git for Windows marks received pack/index/rev files read-only.
            # Selection is already durable before this cleanup runs; clearing
            # that attribute changes only whether a rejected clone can be
            # reclaimed under the frozen disk cap.
            os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
            function(path)

        shutil.rmtree(destination, onerror=clear_readonly_and_retry)


def _load_included_members(
    project_root: Path, paths: Sequence[Path]
) -> tuple[set[str], set[str]]:
    names: set[str] = set()
    heads: set[str] = set()
    for name in RETAINED_ANCHORS:
        repository = project_root / "corpus" / "_clones" / slug_for_name(name)
        if not repository.exists():
            raise Corpus50Error(f"retained anchor clone is missing: {repository}")
        names.add(name.casefold())
        heads.add(git_text(repository, ["rev-parse", "--verify", "HEAD^{commit}"]).strip().casefold())
    for path in paths:
        _metadata, rows = _selected_document(path)
        for row in rows:
            name = row.get("name") or row.get("clone_name")
            head = row.get("head")
            if isinstance(name, str):
                names.add(name.casefold())
            if isinstance(head, str):
                heads.add(head.casefold())
    return names, heads


def _selection_ledger_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    verify_hash_chain(path)
    return read_jsonl(path)


def _assert_candidate_sequence(
    ledger_path: Path, candidate: Mapping[str, Any], cohort: str, stress_key: str | None
) -> None:
    with _HASH_CHAIN_TIPS_LOCK:
        tip = _validated_hash_chain_tip(ledger_path)
        if cohort == "stress":
            rank = candidate.get("candidate_order")
            if rank is None:
                raise Corpus50Error(
                    "stress candidate lacks candidate_order from emit-stress-order"
                )
            numeric_rank = int(rank)
            completed = tip.stress_terminal_ranks.get(str(stress_key), set())
            prefix = tip.stress_terminal_prefixes.get(str(stress_key), 0)
            if prefix >= numeric_rank - 1:
                return
            missing = [
                value
                for value in range(1, numeric_rank)
                if value not in completed
            ][:20]
            raise Corpus50Error(
                f"stress candidate order is not sequential; prior ranks lack terminal "
                f"ledger outcomes: {missing}"
            )

        rank = candidate.get("base_rank")
        if rank is None:
            raise Corpus50Error("base candidate lacks base_rank from the active frame")
        numeric_rank = int(rank)
        if tip.base_terminal_prefix >= numeric_rank - 1:
            return
        missing = [
            value
            for value in range(1, numeric_rank)
            if value not in tip.base_terminal_ranks
        ][:20]
        raise Corpus50Error(
            f"base candidate order is not sequential; prior ranks lack terminal ledger "
            f"outcomes: {missing}"
        )


def _guarded_screening_context(
    frame_root: Path, clone_root: Path, account_paths: Sequence[Path]
) -> tuple[Any, list[dict[str, Any]]]:
    try:
        from analysis import corpus50_replay as replay_guard
    except ImportError:  # Running this file directly places analysis/ on sys.path.
        import corpus50_replay as replay_guard  # type: ignore

    accounted = replay_guard.normalized_roots(
        [frame_root.resolve(), clone_root.resolve(), *[path.resolve() for path in account_paths]]
    )
    policy = replay_guard.DiskPolicy(
        accounted,
        HARD_CAP_BYTES,
        {"D:\\": MIN_D_FREE_BYTES, "C:\\": MIN_C_FREE_BYTES},
    )
    initial = policy.snapshot()
    if initial.violations:
        raise Corpus50Error("; ".join(initial.violations))
    snapshots: list[dict[str, Any]] = [initial.as_json()]
    context = replay_guard.GuardedSubprocesses(
        policy,
        2.0,
        lambda snapshot: snapshots.append(snapshot.as_json()),
    )
    return context, snapshots


def _predicate_for_candidate(
    repository: Path,
    stress_key: str,
    reviewed_predicate: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if stress_key in ("config", "catalog"):
        paths = [entry.path for entry in list_tree(repository)]
        return (
            evaluate_config_predicate(paths)
            if stress_key == "config"
            else evaluate_catalog_predicate(paths)
        )
    if stress_key == "import":
        return evaluate_import_predicate(repository)
    if stress_key == "low_author":
        return evaluate_low_author_predicate(repository)
    if reviewed_predicate is not None:
        if reviewed_predicate.get("predicate") != "non_english":
            raise Corpus50Error("reviewed predicate artifact is not non_english evidence")
        if reviewed_predicate.get("requires_human_review") is not False:
            raise Corpus50Error("non-English predicate artifact is not finalized")
        reviewed_head = reviewed_predicate.get("repository_head")
        actual_head = git_text(
            repository, ["rev-parse", "--verify", "HEAD^{commit}"]
        ).strip().casefold()
        if not isinstance(reviewed_head, str) or reviewed_head.casefold() != actual_head:
            raise Corpus50Error(
                "non-English predicate artifact is not bound to this frozen repository HEAD"
            )
        return dict(reviewed_predicate)
    return scan_non_english_identifiers(repository)


def screen_candidate(
    candidate: Mapping[str, Any],
    *,
    cohort: str,
    stress_key: str | None,
    frame_root: Path,
    project_root: Path,
    ledger_path: Path,
    output_path: Path,
    included_member_paths: Sequence[Path] = (),
    account_paths: Sequence[Path] = (),
    reviewed_predicate_path: Path | None = None,
) -> dict[str, Any]:
    if cohort not in {"base", "stress"}:
        raise Corpus50Error("screen cohort must be base or stress")
    if cohort == "stress" and stress_key not in STRESS_KEYS:
        raise Corpus50Error("stress screening requires one fixed stress key")
    if cohort == "base" and stress_key is not None:
        raise Corpus50Error("base screening cannot have a stress key")
    frame_root = ensure_d_frame_root(frame_root)
    project_root = project_root.resolve()
    clone_root = (project_root / "corpus" / "_clones").resolve()
    clone_root.mkdir(parents=True, exist_ok=True)

    repo_id = _parse_decimal(candidate.get("repo_id"), field="repo_id", context="candidate")
    name = candidate.get("clone_name") or candidate.get("name")
    if not isinstance(name, str) or name.count("/") != 1:
        raise Corpus50Error("candidate lacks owner/name")
    url = candidate.get("url") or f"https://github.com/{name}.git"
    if not isinstance(url, str):
        raise Corpus50Error("candidate lacks clone URL")
    _validate_github_clone_url(url, name)
    expected_kind = "base" if cohort == "base" else str(stress_key)
    expected_priority = priority_key(expected_kind, repo_id)
    if candidate.get("priority_key") != expected_priority:
        raise Corpus50Error(f"candidate {name} has wrong frozen priority key")
    _assert_candidate_sequence(ledger_path, candidate, cohort, stress_key)

    slug = slug_for_name(name)
    destination = clone_root / slug
    ownership_path = (
        acquisition_paths(frame_root)["state"] / "screening-clones" / f"{slug}.json"
    )
    ownership: dict[str, Any]
    if ownership_path.exists():
        ownership = read_json(ownership_path)
        if (
            ownership.get("destination") != str(destination.resolve())
            or ownership.get("repo_id") != repo_id
        ):
            raise Corpus50Error(f"screening ownership collision at {ownership_path}")
        owned = True
    else:
        owned = False
        ownership = {
            "schema_version": SCHEMA_VERSION,
            "rule_id": RULE_ID,
            "repo_id": repo_id,
            "name": name,
            "url": url,
            "destination": str(destination.resolve()),
            "created_at_utc": utc_now(),
            "status": "intended",
        }
        if not destination.exists():
            atomic_write_json(ownership_path, ownership)
            owned = True

    candidate_record = {
        "repo_id": repo_id,
        "name": name,
        "url": url,
        "cohort": cohort,
        "priority_key": expected_priority,
    }
    for field in ("base_rank", "candidate_order", "fallback_stage"):
        if candidate.get(field) is not None:
            candidate_record[field] = candidate[field]
    if stress_key is not None:
        candidate_record["stress_key"] = stress_key

    included_paths = list(included_member_paths)
    if output_path.exists():
        included_paths.append(output_path)
    included_names, included_heads = _load_included_members(project_root, included_paths)
    reviewed = read_json(reviewed_predicate_path) if reviewed_predicate_path else None
    if destination.exists() and not owned and name.casefold() not in included_names:
        # Local, non-selected workspace state is not an eligibility criterion
        # under the frozen rule. Stop for operator resolution; never turn this
        # collision into a candidate rejection and silently advance the draw.
        raise Corpus50Error(
            "pre-existing non-anchor clone has no frozen selection status; "
            f"screening cannot advance past the path collision: {destination}"
        )
    last_disk: dict[str, Any] | None = None
    clone_action = "unknown"
    measured: dict[str, Any] = {}
    predicate_result: dict[str, Any] | None = None
    outcome_status = "rejected"
    reason = "unknown_failure"
    detail: str | None = None

    try:
        context, snapshots = _guarded_screening_context(
            frame_root, clone_root, account_paths
        )
        with context:
            if name.casefold() in included_names:
                reason = "duplicate_included_repository_name"
                raise Corpus50Error(f"repository name is already included: {name}")
            if destination.exists():
                if not (destination / ".git").exists():
                    raise Corpus50Error(
                        f"screening destination exists but is not a Git clone: {destination}"
                    )
                if not owned:
                    reason = "preexisting_unowned_clone_conflict"
                    raise Corpus50Error(
                        "pre-existing non-anchor clone has no frozen selection status and "
                        f"cannot be reused: {destination}"
                    )
                clone_action = "reused_owned_screening_clone"
            else:
                completed = subprocess.run(
                    [
                        "git",
                        "clone",
                        "--filter=blob:none",
                        "--no-checkout",
                        url,
                        str(destination),
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                if completed.returncode != 0:
                    tail = completed.stderr.decode("utf-8", errors="replace")[-4000:]
                    raise Corpus50Error(
                        f"git clone exited {completed.returncode}: {tail.strip()}"
                    )
                clone_action = "cloned_filter_blob_none_no_checkout"
                ownership["status"] = "cloned"
                ownership["cloned_at_utc"] = utc_now()
                atomic_write_json(ownership_path, ownership)
                owned = True
            origin = git_text(destination, ["remote", "get-url", "origin"]).strip()
            if origin.casefold().removesuffix(".git") != url.casefold().removesuffix(".git"):
                raise Corpus50Error(f"existing clone origin mismatch: {origin!r}")
            promisor = git_text(
                destination, ["config", "--get", "remote.origin.promisor"]
            ).strip()
            if promisor.casefold() != "true":
                raise Corpus50Error("candidate clone is not a blob:none promisor clone")
            measured = measure_repository(destination)
            head = str(measured["head"]).casefold()
            if head in included_heads:
                reason = "duplicate_exact_head"
                raise Corpus50Error(f"exact HEAD is already included: {head}")
            if not measured["at_least_500_first_parent_commits"]:
                reason = "fewer_than_500_first_parent_commits"
                raise Corpus50Error(
                    f"only {measured['first_parent_commit_count']} first-parent commits"
                )
            if cohort == "stress":
                assert stress_key is not None
                predicate_result = _predicate_for_candidate(
                    destination, stress_key, reviewed
                )
                if predicate_result.get("passed") is None:
                    outcome_status = "review_required"
                    reason = "non_english_human_review_required"
                elif predicate_result.get("passed") is not True:
                    reason = "stress_predicate_miss"
                    raise Corpus50Error(f"{stress_key} predicate did not pass")
                else:
                    outcome_status = "selected"
                    reason = "first_eligible_stress_candidate"
            else:
                outcome_status = "eligible"
                reason = "passed_common_eligibility_and_local_classification"
        if snapshots:
            last_disk = snapshots[-1]
    except Exception as error:
        detail = str(error)
        if outcome_status not in {"review_required"}:
            outcome_status = "rejected"
        if reason == "unknown_failure":
            reason = (
                "disk_denial"
                if "disk" in type(error).__name__.casefold()
                or "free space" in detail.casefold()
                or "hard cap" in detail.casefold()
                else "clone_or_local_screen_failure"
            )

    classification = measured.get("classification") if measured else None
    selected_row: dict[str, Any] | None = None
    if outcome_status in {"eligible", "selected", "review_required"}:
        if not isinstance(classification, dict):
            raise Corpus50Error("successful local screen has no classification")
        selected_row = {
            **dict(candidate),
            "repo_id": repo_id,
            "name": name,
            "clone_name": name,
            "url": url,
            "slug": slug,
            "clone_path": str(destination.resolve()),
            "head": measured["head"],
            "first_parent_commit_count": measured["first_parent_commit_count"],
            "reachable_commit_count": measured["reachable_commit_count"],
            "classification": classification,
            "primary_language": classification["primary_language"],
            "language_stratum": classification["language_stratum"],
            "layout_stratum": classification["layout_stratum"],
            "tracked_path_count": classification["tracked_path_count"],
            "source_path_count": classification["source_path_count"],
            "priority_key": expected_priority,
            "selection_status": "selected" if outcome_status == "selected" else outcome_status,
        }
        if stress_key is not None:
            selected_row["stress_key"] = stress_key
            selected_row["stress_predicate_passed"] = (
                predicate_result is not None and predicate_result.get("passed") is True
            )
            selected_row["predicate_result"] = predicate_result
            if reviewed_predicate_path is not None:
                digest, length = sha256_file(reviewed_predicate_path)
                selected_row["predicate_artifact"] = {
                    "path": str(reviewed_predicate_path.resolve()),
                    "sha256": digest,
                    "byte_length": length,
                }

    event = {
        "event_type": "candidate_screened",
        "candidate": candidate_record,
        "outcome": {
            "status": outcome_status,
            "reason": reason,
            **({"detail": detail} if detail else {}),
        },
        "measurements": {
            **({
                "head": measured.get("head"),
                "first_parent_commit_count": measured.get("first_parent_commit_count"),
                "reachable_commit_count": measured.get("reachable_commit_count"),
                "primary_language": classification.get("primary_language") if isinstance(classification, dict) else None,
                "language_stratum": classification.get("language_stratum") if isinstance(classification, dict) else None,
                "layout_stratum": classification.get("layout_stratum") if isinstance(classification, dict) else None,
            } if measured else {}),
            "clone_size_bytes": _clone_size(destination),
            "clone_action": clone_action,
            "partial_clone_promisor": (
                bool(measured) and destination.exists()
            ),
            "last_disk_guard_snapshot": last_disk,
        },
        "artifacts": {
            "clone_path": str(destination.resolve()),
            "ownership_record": str(ownership_path.resolve()) if owned else None,
            "predicate_result": predicate_result,
        },
    }
    appended = append_selection_event(ledger_path, event)

    if selected_row is not None and outcome_status in {"eligible", "selected"}:
        append_unique_jsonl(output_path, selected_row, key="repo_id")
        ownership["status"] = outcome_status
        ownership["frozen_head"] = measured["head"]
        atomic_write_json(ownership_path, ownership)

    cleaned = False
    if outcome_status == "rejected" and owned:
        try:
            _safe_remove_owned_clone(destination, clone_root, ownership)
            ownership["status"] = "cleaned_after_rejection"
            ownership["cleaned_at_utc"] = utc_now()
            atomic_write_json(ownership_path, ownership)
            append_selection_event(
                ledger_path,
                {
                    "event_type": "screening_clone_cleanup",
                    "candidate": candidate_record,
                    "outcome": {
                        "status": "complete",
                        "reason": "rejected_candidate_evidence_was_durable",
                    },
                    "artifacts": {"removed_path": str(destination.resolve())},
                },
            )
            cleaned = True
        except Exception as cleanup_error:
            append_selection_event(
                ledger_path,
                {
                    "event_type": "screening_clone_cleanup",
                    "candidate": candidate_record,
                    "outcome": {
                        "status": "failed",
                        "reason": type(cleanup_error).__name__,
                        "detail": str(cleanup_error),
                    },
                    "artifacts": {"retained_path": str(destination.resolve())},
                },
            )
    return {
        "rule_id": RULE_ID,
        "candidate": candidate_record,
        "outcome": event["outcome"],
        "selected_row": selected_row,
        "clone_cleaned": cleaned,
        "ledger_record_sha256": appended["record_sha256"],
    }


def cleanup_unselected_base_clones(
    *,
    eligible_path: Path,
    selected_path: Path,
    frame_root: Path,
    project_root: Path,
    ledger_path: Path,
) -> dict[str, Any]:
    eligible = _selected_rows(eligible_path)
    selected_metadata, selected = _selected_document(selected_path)
    if selected_metadata.get("status") != "selected" or len(selected) != 35:
        raise Corpus50Error("base selection is not a completed 35-member solver result")
    selected_ids = {int(row["repo_id"]) for row in selected}
    clone_root = (project_root.resolve() / "corpus" / "_clones").resolve()
    ownership_root = acquisition_paths(frame_root.resolve())["state"] / "screening-clones"
    removed: list[str] = []
    for row in eligible:
        repo_id = int(row["repo_id"])
        if repo_id in selected_ids:
            continue
        name = str(row.get("name") or row.get("clone_name"))
        slug = slug_for_name(name)
        destination = clone_root / slug
        ownership_path = ownership_root / f"{slug}.json"
        ownership = read_json(ownership_path)
        candidate = {
            "repo_id": repo_id,
            "name": name,
            "url": row.get("url"),
            "cohort": "base",
            "base_rank": row.get("base_rank"),
            "priority_key": row.get("priority_key"),
        }
        append_selection_event(
            ledger_path,
            {
                "event_type": "base_candidate_not_selected",
                "candidate": candidate,
                "outcome": {
                    "status": "rejected",
                    "reason": "not_in_lexicographically_smallest_first_feasible_solution",
                },
                "measurements": {
                    "head": row.get("head"),
                    "first_parent_commit_count": row.get("first_parent_commit_count"),
                    "reachable_commit_count": row.get("reachable_commit_count"),
                    "clone_size_bytes": _clone_size(destination),
                },
            },
        )
        _safe_remove_owned_clone(destination, clone_root, ownership)
        ownership["status"] = "cleaned_not_selected"
        ownership["cleaned_at_utc"] = utc_now()
        atomic_write_json(ownership_path, ownership)
        append_selection_event(
            ledger_path,
            {
                "event_type": "screening_clone_cleanup",
                "candidate": candidate,
                "outcome": {
                    "status": "complete",
                    "reason": "nonselected_base_evidence_was_durable",
                },
                "artifacts": {"removed_path": str(destination.resolve())},
            },
        )
        removed.append(name)
    return {"rule_id": RULE_ID, "removed_count": len(removed), "removed": removed}


def _add_frame_root(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--frame-root",
        required=True,
        type=Path,
        help="D:-resident root containing raw frames, state, and manifests",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    acquire_archive = subparsers.add_parser(
        "acquire-gharchive",
        help="acquire and verify all 24 GH Archive hours",
    )
    _add_frame_root(acquire_archive)
    acquire_archive.add_argument(
        "--account-path",
        action="append",
        default=[],
        type=Path,
        help="additional clone/result root counted toward the combined 20 GiB cap",
    )
    acquire_archive.add_argument(
        "--base-url",
        default="https://data.gharchive.org",
        help="fixture override; production default is the frozen GH Archive origin",
    )
    acquire_archive.add_argument("--timeout", type=float, default=120.0)
    acquire_archive.add_argument("--retries", type=int, default=5)
    acquire_archive.add_argument(
        "--dry-run",
        action="store_true",
        help="print the exact URLs and paths without network or filesystem writes",
    )

    build_base = subparsers.add_parser(
        "build-base-frame",
        help="stream all verified hours into the exact active base frame",
    )
    _add_frame_root(build_base)

    acquire_search = subparsers.add_parser(
        "acquire-search",
        help="acquire all 63 fixed GitHub Search stress snapshots",
    )
    _add_frame_root(acquire_search)
    acquire_search.add_argument(
        "--account-path", action="append", default=[], type=Path
    )
    acquire_search.add_argument(
        "--api-base",
        default="https://api.github.com",
        help="fixture override; production default is GitHub's API",
    )
    acquire_search.add_argument(
        "--token-env",
        default="GITHUB_TOKEN",
        help="environment variable holding a GitHub token (GH_TOKEN is fallback)",
    )
    acquire_search.add_argument("--timeout", type=float, default=60.0)
    acquire_search.add_argument("--retries", type=int, default=10)
    acquire_search.add_argument("--max-rate-wait-seconds", type=int, default=900)
    acquire_search.add_argument("--dry-run", action="store_true")

    build_stress = subparsers.add_parser(
        "build-stress-frames", help="verify snapshots and emit five ranked frames"
    )
    _add_frame_root(build_stress)
    build_stress.add_argument("--api-base", default="https://api.github.com")

    classify = subparsers.add_parser(
        "classify-repo", help="measure local eligibility, language, and layout"
    )
    classify.add_argument("--repo", required=True, type=Path)

    predicate = subparsers.add_parser(
        "evaluate-stress", help="evaluate one frozen stress predicate locally"
    )
    predicate.add_argument("--repo", required=True, type=Path)
    predicate.add_argument("--stress-key", required=True, choices=STRESS_KEYS)
    predicate.add_argument(
        "--frame-root",
        type=Path,
        help="required for non_english so blob hydration is charged to the frozen guard",
    )
    predicate.add_argument("--account-path", action="append", default=[], type=Path)
    predicate.add_argument(
        "--output", type=Path, help="atomically save evidence JSON as well as printing it"
    )

    review_nonenglish = subparsers.add_parser(
        "review-non-english",
        help="finalize human rejections without permitting machine-miss promotion",
    )
    review_nonenglish.add_argument("--scan", required=True, type=Path)
    review_nonenglish.add_argument(
        "--accepted-token", action="append", default=[], dest="accepted_tokens"
    )
    review_nonenglish.add_argument("--output", required=True, type=Path)

    stress_order = subparsers.add_parser(
        "emit-stress-order", help="emit own/union/base fallback candidate order"
    )
    _add_frame_root(stress_order)
    stress_order.add_argument("--stress-key", required=True, choices=STRESS_KEYS)
    stress_order.add_argument("--exclude-repo-id", action="append", default=[], type=int)
    stress_order.add_argument("--output", required=True, type=Path)

    solve_base = subparsers.add_parser(
        "solve-base", help="solve exact dual margins at the first feasible prefix"
    )
    solve_base.add_argument("--candidates", required=True, type=Path)
    solve_base.add_argument("--output", required=True, type=Path)
    solve_base.add_argument(
        "--active-frame-exhausted",
        action="store_true",
        help="permit the frozen minimum-deviation fallback only after full exhaustion",
    )

    record_ledger = subparsers.add_parser(
        "record-ledger", help="append one fsynced SHA-chained selector event"
    )
    record_ledger.add_argument("--ledger", type=Path, default=DEFAULT_SELECTION_LEDGER)
    record_ledger.add_argument(
        "--input",
        type=str,
        default="-",
        help="JSON object path, or - for stdin",
    )

    assemble = subparsers.add_parser(
        "assemble-manifest", help="validate and write the canonical 50-member manifest"
    )
    _add_frame_root(assemble)
    assemble.add_argument("--stress-selected", required=True, type=Path)
    assemble.add_argument("--base-selected", required=True, type=Path)
    assemble.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    assemble.add_argument("--account-path", action="append", default=[], type=Path)
    assemble.add_argument("--output", type=Path, default=DEFAULT_CORPUS_MANIFEST)

    verify_ledger = subparsers.add_parser(
        "verify-ledger", help="verify a SHA-chained JSONL ledger"
    )
    verify_ledger.add_argument("path", type=Path)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "acquire-gharchive":
            if args.dry_run:
                root = args.frame_root.resolve()
                result: object = {
                    "rule_id": RULE_ID,
                    "frame_root": str(root),
                    "disk_cap_bytes": HARD_CAP_BYTES,
                    "minimum_free_bytes": {"D:": MIN_D_FREE_BYTES, "C:": MIN_C_FREE_BYTES},
                    "accounted_paths": [str(path.resolve()) for path in args.account_path],
                    "requests": [
                        {
                            **spec,
                            "target": str(
                                root
                                / "raw"
                                / "gharchive"
                                / BASE_LISTING_DATE
                                / spec["filename"]
                            ),
                        }
                        for spec in gharchive_specs(args.base_url)
                    ],
                }
            else:
                result = acquire_gharchive(
                    args.frame_root,
                    account_paths=args.account_path,
                    base_url=args.base_url,
                    timeout=args.timeout,
                    retries=args.retries,
                )
        elif args.command == "build-base-frame":
            result = build_base_frame(args.frame_root)
        elif args.command == "acquire-search":
            if args.dry_run:
                result = {
                    "rule_id": RULE_ID,
                    "listing_date": STRESS_LISTING_DATE,
                    "frame_root": str(args.frame_root.resolve()),
                    "disk_cap_bytes": HARD_CAP_BYTES,
                    "authenticated": bool(
                        os.environ.get(args.token_env) or os.environ.get("GH_TOKEN")
                    ),
                    "requests": [
                        {
                            "snapshot_id": spec.snapshot_id,
                            "stress_key": spec.stress_key,
                            "query": spec.query,
                            "page": spec.page,
                            "url": spec.url(args.api_base),
                        }
                        for spec in stress_snapshot_specs()
                    ],
                }
            else:
                token = os.environ.get(args.token_env) or os.environ.get("GH_TOKEN")
                result = acquire_search_snapshots(
                    args.frame_root,
                    token=token,
                    account_paths=args.account_path,
                    api_base=args.api_base,
                    timeout=args.timeout,
                    retries=args.retries,
                    max_rate_wait_seconds=args.max_rate_wait_seconds,
                )
        elif args.command == "build-stress-frames":
            result = build_stress_frames(args.frame_root, api_base=args.api_base)
        elif args.command == "classify-repo":
            result = measure_repository(args.repo)
        elif args.command == "evaluate-stress":
            repository = args.repo.resolve()
            if args.stress_key in ("config", "catalog"):
                local_paths = [entry.path for entry in list_tree(repository)]
                result = (
                    evaluate_config_predicate(local_paths)
                    if args.stress_key == "config"
                    else evaluate_catalog_predicate(local_paths)
                )
            elif args.stress_key == "import":
                result = evaluate_import_predicate(repository)
            elif args.stress_key == "low_author":
                result = evaluate_low_author_predicate(repository)
            else:
                if args.frame_root is None:
                    raise Corpus50Error(
                        "non_english evaluation requires --frame-root for disk guarding"
                    )
                result = scan_non_english_identifiers(
                    repository,
                    disk_guard=DiskGuard(
                        args.frame_root.resolve(), tuple(args.account_path)
                    ),
                )
            if args.output:
                atomic_write_json(args.output, result)
        elif args.command == "review-non-english":
            result = finalize_non_english_review(
                read_json(args.scan), args.accepted_tokens
            )
            atomic_write_json(args.output, result)
        elif args.command == "emit-stress-order":
            rows = stress_candidate_order(
                args.frame_root,
                args.stress_key,
                excluded_repo_ids=args.exclude_repo_id,
            )
            payload = b"".join(canonical_json_bytes(row) for row in rows)
            atomic_write_bytes(args.output, payload)
            result = {
                "rule_id": RULE_ID,
                "stress_key": args.stress_key,
                "record_count": len(rows),
                "output": str(args.output.resolve()),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        elif args.command == "solve-base":
            result = solve_base_selection(
                _selected_rows(args.candidates),
                active_frame_exhausted=args.active_frame_exhausted,
            )
            atomic_write_json(args.output, result)
        elif args.command == "record-ledger":
            if args.input == "-":
                event = json.load(sys.stdin)
            else:
                event = read_json(Path(args.input))
            if not isinstance(event, dict):
                raise Corpus50Error("ledger input must be a JSON object")
            result = append_selection_event(args.ledger, event)
        elif args.command == "assemble-manifest":
            result = assemble_corpus_manifest(
                frame_root=args.frame_root,
                stress_selected_path=args.stress_selected,
                base_selected_path=args.base_selected,
                project_root=args.project_root,
                accounted_paths=args.account_path,
                output_path=args.output,
            )
        elif args.command == "verify-ledger":
            result = verify_hash_chain(args.path)
        else:  # pragma: no cover - argparse prevents this
            raise AssertionError(args.command)
    except Corpus50Error as error:
        print(f"corpus50: ERROR: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
