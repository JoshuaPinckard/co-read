"""Shared constants and small I/O helpers for the replay measurement."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CLONE_ROOT = PROJECT_ROOT / "corpus" / "_clones"
CORPUS_PATH = PROJECT_ROOT / "corpus" / "CORPUS.json"
OUTPUT_ROOT = PROJECT_ROOT / "exploratory" / "language-hole"
STREAM_ROOT = OUTPUT_ROOT / "streams"
RESULT_ROOT = OUTPUT_ROOT / "results"

SCHEMA_VERSION = 1
CAP_THRESHOLD_REACHABLE_COMMITS = 20_000
CAP_COMMITS = 5_000
DECAY_HALF_LIFE_COMMITS = 150.0
RANDOM_SEED = "blast-radius-cross-language-replay-v1"

# Ordered approximately smallest-first so useful results land early.
REPOSITORIES: tuple[dict[str, str], ...] = (
    {
        "slug": "pallets__click",
        "name": "pallets/click",
        "url": "https://github.com/pallets/click.git",
        "axis": "Python; mature command-line toolkit",
        "expected_stress": "Fast posture target with broad independent test modules",
    },
    {
        "slug": "pallets__itsdangerous",
        "name": "pallets/itsdangerous",
        "url": "https://github.com/pallets/itsdangerous.git",
        "axis": "Python; compact cryptographic serialization library",
        "expected_stress": "Fast deterministic posture-experiment target",
    },
    {
        "slug": "hashicorp__terraform-provider-random",
        "name": "hashicorp/terraform-provider-random",
        "url": "https://github.com/hashicorp/terraform-provider-random.git",
        "axis": "Go + HCL + YAML, small",
        "expected_stress": "Baseline non-JavaScript control",
    },
    {
        "slug": "psf__requests",
        "name": "psf/requests",
        "url": "https://github.com/psf/requests.git",
        "axis": "Python; tests/ directory",
        "expected_stress": "Different test convention again",
    },
    {
        "slug": "BurntSushi__ripgrep",
        "name": "BurntSushi/ripgrep",
        "url": "https://github.com/BurntSushi/ripgrep.git",
        "axis": "Rust; tests inline in source files",
        "expected_stress": "Test-to-source path affinity should collapse",
    },
    {
        "slug": "apache__commons-lang",
        "name": "apache/commons-lang",
        "url": "https://github.com/apache/commons-lang.git",
        "axis": "Java; parallel src/test/java tree",
        "expected_stress": "Deep mirrored hierarchy; path similarity should do unusually well",
    },
    {
        "slug": "jupyter__notebook",
        "name": "jupyter/notebook",
        "url": "https://github.com/jupyter/notebook.git",
        "axis": "Notebooks + JavaScript",
        "expected_stress": "JSON-enveloped code",
    },
    {
        "slug": "gohugoio__hugo",
        "name": "gohugoio/hugo",
        "url": "https://github.com/gohugoio/hugo.git",
        "axis": "Go; _test.go adjacent to source",
        "expected_stress": "Test file in the same directory",
    },
    {
        "slug": "redis__redis",
        "name": "redis/redis",
        "url": "https://github.com/redis/redis.git",
        "axis": "C; no standard test convention",
        "expected_stress": "Language with no import statement in the scraped sense",
    },
    {
        "slug": "prometheus__prometheus",
        "name": "prometheus/prometheus",
        "url": "https://github.com/prometheus/prometheus.git",
        "axis": "Go with vendored tree history",
        "expected_stress": "Vendored blobs; near-duplicate content",
    },
    {
        "slug": "hashicorp__terraform",
        "name": "hashicorp/terraform",
        "url": "https://github.com/hashicorp/terraform.git",
        "axis": "Go + HCL, large",
        "expected_stress": "Scale",
    },
    {
        "slug": "ansible__ansible",
        "name": "ansible/ansible",
        "url": "https://github.com/ansible/ansible.git",
        "axis": "Python + very large YAML surface",
        "expected_stress": "Config-heavy; huge file count",
    },
)

REPOSITORY_BY_SLUG = {repo["slug"]: repo for repo in REPOSITORIES}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_directories() -> None:
    for path in (CLONE_ROOT, OUTPUT_ROOT, STREAM_ROOT, RESULT_ROOT):
        path.mkdir(parents=True, exist_ok=True)


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, indent=2, sort_keys=True, ensure_ascii=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def run_git(
    repository: Path | None,
    arguments: Iterable[str],
    *,
    check: bool = True,
    text: bool = True,
) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
    command = ["git", "-c", "core.longpaths=true"]
    if repository is not None:
        command.extend(["-C", str(repository)])
    command.extend(arguments)
    return subprocess.run(
        command,
        check=check,
        capture_output=True,
        text=text,
        encoding="utf-8" if text else None,
        errors="replace" if text else None,
    )


def selected_repositories(slugs: list[str] | None) -> list[dict[str, str]]:
    if not slugs:
        return list(REPOSITORIES)
    unknown = sorted(set(slugs) - REPOSITORY_BY_SLUG.keys())
    if unknown:
        choices = ", ".join(REPOSITORY_BY_SLUG)
        raise SystemExit(f"Unknown repository slug(s): {', '.join(unknown)}. Choices: {choices}")
    requested = set(slugs)
    return [repo for repo in REPOSITORIES if repo["slug"] in requested]
