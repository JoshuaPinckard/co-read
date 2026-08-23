"""Frozen co-change radius adapter for the concurrency-posture experiment.

This module deliberately does not derive co-change data itself.  It consumes a
verified stream emitted by :mod:`instruments.replay.extract` and builds/queries
the existing :class:`instruments.replay.replay.ReplayState`.  The only policy
implemented here is the experiment's top-K and score-threshold selection.

``cutoff_index`` has pre-commit semantics: commit records with an index smaller
than it are folded, and the record at ``cutoff_index`` is the first excluded
commit.  Consequently every query observes one immutable history generation.
"""

from __future__ import annotations

import copy
import gzip
import hashlib
import json
import math
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[2]
REPLAY_ROOT = PROJECT_ROOT / "instruments" / "replay"

# replay.py is also a directly executable research script and therefore uses a
# sibling import (``from common import ...``).  Putting only that directory on
# the import path preserves its existing invocation contract without copying
# any replay implementation into this adapter.
if str(REPLAY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPLAY_ROOT))

from common import DECAY_HALF_LIFE_COMMITS, SCHEMA_VERSION  # noqa: E402
from replay import (  # noqa: E402
    ReplayState,
    collect_cochange_histories,
    path_bytes,
    score_cochange_histories,
)


REQUIRED_EXTRACTION_ARGUMENTS = frozenset(
    {
        "--first-parent",
        "--reverse",
        "--root",
        "--diff-merges=first-parent",
        "--find-renames=50%",
        "-l0",
        "--name-status",
        "-z",
    }
)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _live_paths_sha256(paths: Iterable[str]) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths, key=path_bytes):
        encoded = path_bytes(path)
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _display_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return str(resolved)


def _default_metadata_path(stream_path: Path) -> Path:
    suffix = ".jsonl.gz"
    if not stream_path.name.endswith(suffix):
        raise ValueError(
            "metadata_path is required when the stream filename does not end in .jsonl.gz"
        )
    return stream_path.with_name(stream_path.name[: -len(suffix)] + ".meta.json")


@dataclass(frozen=True)
class RadiusCandidate:
    """One scored path selected for a seed file."""

    path: str
    score: float

    def as_dict(self) -> dict[str, Any]:
        return {"path": self.path, "score": self.score}


@dataclass(frozen=True)
class SeedRadius:
    """The independently thresholded top-K radius for one seed."""

    seed_path: str
    candidates: tuple[RadiusCandidate, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "seed_path": self.seed_path,
            "candidates": [candidate.as_dict() for candidate in self.candidates],
        }


@dataclass(frozen=True)
class UnionRadiusCandidate:
    """A path in the union radius, retaining every contributing seed score."""

    path: str
    score: float
    seed_scores: tuple[tuple[str, float], ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "score": self.score,
            "seed_scores": [
                {"seed_path": seed_path, "score": score}
                for seed_path, score in self.seed_scores
            ],
        }


@dataclass(frozen=True)
class RadiusQuery:
    """Per-seed radii and their deterministic set union for a claim."""

    seeds: tuple[str, ...]
    per_seed: tuple[SeedRadius, ...]
    union: tuple[UnionRadiusCandidate, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "seeds": list(self.seeds),
            "per_seed": [radius.as_dict() for radius in self.per_seed],
            "union": [candidate.as_dict() for candidate in self.union],
        }


class FrozenCochangeRadius:
    """Read-only co-change index frozen immediately before one stream commit."""

    def __init__(
        self,
        *,
        state: ReplayState,
        cutoff_index: int,
        top_k: int,
        threshold: float,
        threshold_inclusive: bool,
        decayed: bool,
        provenance: dict[str, Any],
    ) -> None:
        self._state = state
        self.cutoff_index = cutoff_index
        self.top_k = top_k
        self.threshold = threshold
        self.threshold_inclusive = threshold_inclusive
        self.decayed = decayed
        self._provenance = provenance

    @classmethod
    def from_stream(
        cls,
        stream_path: Path | str,
        *,
        cutoff_index: int,
        top_k: int,
        threshold: float,
        threshold_inclusive: bool,
        decayed: bool = True,
        metadata_path: Path | str | None = None,
        expected_cutoff_sha: str | None = None,
    ) -> "FrozenCochangeRadius":
        """Build a frozen index from a replay extraction stream.

        The metadata sidecar is mandatory.  Its declared stream hash, commit
        count, source HEAD, and schema must agree with the stream before any
        radius can be queried.  ``expected_cutoff_sha`` optionally pins the
        first excluded commit and is recommended for preregistered runs.
        """

        stream = Path(stream_path).resolve()
        metadata = (
            Path(metadata_path).resolve()
            if metadata_path is not None
            else _default_metadata_path(stream)
        )
        if not stream.is_file():
            raise FileNotFoundError(f"co-change extraction stream not found: {stream}")
        if not metadata.is_file():
            raise FileNotFoundError(f"co-change extraction metadata not found: {metadata}")
        if isinstance(cutoff_index, bool) or not isinstance(cutoff_index, int):
            raise TypeError("cutoff_index must be an integer")
        if cutoff_index < 0:
            raise ValueError("cutoff_index must be non-negative")
        if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k <= 0:
            raise ValueError("top_k must be a positive integer")
        if not math.isfinite(threshold) or not 0.0 <= threshold <= 1.0:
            raise ValueError("threshold must be finite and between 0 and 1")
        if not isinstance(threshold_inclusive, bool):
            raise TypeError("threshold_inclusive must be explicit boolean policy")
        if not isinstance(decayed, bool):
            raise TypeError("decayed must be boolean")

        actual_stream_sha256 = _sha256_file(stream)
        actual_metadata_sha256 = _sha256_file(metadata)
        metadata_record = json.loads(metadata.read_text(encoding="utf-8"))
        if metadata_record.get("status") != "ok":
            raise ValueError("co-change extraction metadata status is not ok")
        if metadata_record.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("co-change extraction metadata schema is unsupported")
        if metadata_record.get("stream_sha256") != actual_stream_sha256:
            raise ValueError("co-change extraction stream SHA-256 does not match metadata")

        prefix_digest = hashlib.sha256()
        records: list[dict[str, Any]] = []
        raw_lines: list[bytes] = []
        with gzip.open(stream, "rb") as handle:
            for raw_line in handle:
                raw_lines.append(raw_line)
                records.append(json.loads(raw_line))
        if not records:
            raise ValueError("empty co-change extraction stream")
        header = records[0]
        commits = records[1:]
        if header.get("type") != "header" or header.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("unsupported or missing co-change extraction stream header")
        if header.get("source_head_sha") != metadata_record.get("source_head_sha"):
            raise ValueError("stream and metadata source HEAD disagree")
        missing_arguments = sorted(
            REQUIRED_EXTRACTION_ARGUMENTS - set(header.get("git_log_arguments", []))
        )
        if missing_arguments:
            raise ValueError(
                f"co-change extraction protocol is stale; missing {missing_arguments}"
            )
        if metadata_record.get("commit_count") != len(commits):
            raise ValueError("stream commit count does not match extraction metadata")
        if cutoff_index > len(commits):
            raise ValueError(
                f"cutoff_index {cutoff_index} exceeds stream commit count {len(commits)}"
            )

        previous_sha: str | None = None
        for expected_index, commit in enumerate(commits):
            if commit.get("type") != "commit":
                raise ValueError("non-commit record follows extraction stream header")
            if commit.get("index") != expected_index:
                raise ValueError(
                    f"non-contiguous stream index {commit.get('index')}; expected {expected_index}"
                )
            parents = commit.get("parents")
            if not isinstance(parents, list):
                raise ValueError(f"commit {expected_index} has invalid parents")
            if previous_sha is not None and (not parents or parents[0] != previous_sha):
                raise ValueError(f"first-parent chain is broken at stream index {expected_index}")
            previous_sha = commit.get("sha")
        initial_tree_sha = header.get("initial_tree_sha")
        if commits:
            first_parents = commits[0]["parents"]
            if initial_tree_sha is None:
                if first_parents:
                    raise ValueError("root extraction record unexpectedly has a parent")
            elif not first_parents or first_parents[0] != initial_tree_sha:
                raise ValueError("first extraction record does not descend from initial_tree_sha")
            if previous_sha != header.get("source_head_sha"):
                raise ValueError("final extraction record does not match source HEAD")

        first_excluded_sha = (
            str(commits[cutoff_index]["sha"]) if cutoff_index < len(commits) else None
        )
        if expected_cutoff_sha is not None and first_excluded_sha != expected_cutoff_sha:
            raise ValueError(
                "first excluded commit does not match expected_cutoff_sha: "
                f"{first_excluded_sha!r} != {expected_cutoff_sha!r}"
            )

        state = ReplayState(
            header["initial_files"],
            max_commit_age=max(1, cutoff_index + 1),
        )
        prefix_digest.update(raw_lines[0])
        for commit_index, commit in enumerate(commits[:cutoff_index]):
            state.assert_query_generation(commit_index)
            resolved = state.resolve_changes(commit["changes"])
            state.fold(commit_index, resolved)
            prefix_digest.update(raw_lines[commit_index + 1])
        state.assert_query_generation(cutoff_index)

        implementation_paths = {
            "extract.py": REPLAY_ROOT / "extract.py",
            "replay.py": REPLAY_ROOT / "replay.py",
            "radius.py": Path(__file__).resolve(),
        }
        provenance: dict[str, Any] = {
            "schema_version": 1,
            "apparatus": "posture-frozen-cochange-radius",
            "source": {
                "stream_path": _display_path(stream),
                "stream_sha256": actual_stream_sha256,
                "metadata_path": _display_path(metadata),
                "metadata_sha256": actual_metadata_sha256,
                "source_head_sha": header.get("source_head_sha"),
                "repository": header.get("repository"),
                "stream_commit_count": len(commits),
                "stream_left_truncated": bool(header.get("capped")),
                "stream_cap_reason": header.get("cap_reason"),
                "initial_tree_sha": header.get("initial_tree_sha"),
                "extraction_protocol": (
                    "first-parent, reverse, first-parent merge diff, "
                    "exhaustive >=50% rename detection"
                ),
            },
            "freeze": {
                "cutoff_semantics": "fold stream indexes strictly less than cutoff_index",
                "cutoff_index": cutoff_index,
                "first_excluded_sha": first_excluded_sha,
                "last_included_index": cutoff_index - 1 if cutoff_index else None,
                "last_included_sha": (
                    str(commits[cutoff_index - 1]["sha"]) if cutoff_index else None
                ),
                "history_commit_count": cutoff_index,
                "decoded_history_prefix_sha256": prefix_digest.hexdigest(),
                "live_file_count": len(state.existing_ids),
                "live_paths_sha256": _live_paths_sha256(state.path_to_id),
            },
            "radius": {
                "model": (
                    "cochange_time_decayed" if decayed else "cochange_plain_confidence"
                ),
                "top_k_per_seed": top_k,
                "threshold": threshold,
                "threshold_comparison": ">=" if threshold_inclusive else ">",
                "decay_half_life_commits": DECAY_HALF_LIFE_COMMITS if decayed else None,
                "candidate_order": "descending score, then raw UTF-8 path bytes, then stable file identity",
                "multi_seed_rule": (
                    "per-seed selection followed by set union; "
                    "union score is maximum contributing score"
                ),
            },
            "implementation": {
                name: _sha256_file(path) for name, path in implementation_paths.items()
            },
        }
        provenance["provenance_sha256"] = _canonical_sha256(provenance)
        return cls(
            state=state,
            cutoff_index=cutoff_index,
            top_k=top_k,
            threshold=threshold,
            threshold_inclusive=threshold_inclusive,
            decayed=decayed,
            provenance=provenance,
        )

    @property
    def provenance(self) -> dict[str, Any]:
        """Return a defensive copy of the reproducibility record."""

        return copy.deepcopy(self._provenance)

    @property
    def live_paths(self) -> tuple[str, ...]:
        """Paths in the immutable pre-cutoff candidate universe."""

        return tuple(sorted(self._state.path_to_id, key=path_bytes))

    def _passes_threshold(self, score: float) -> bool:
        if self.threshold_inclusive:
            return score >= self.threshold
        return score > self.threshold

    def radius_for(self, seed_path: str) -> SeedRadius:
        """Return the thresholded top-K co-change radius for one live path."""

        try:
            seed = self._state.path_to_id[seed_path]
        except KeyError as exc:
            raise KeyError(
                f"seed path is absent at frozen cutoff {self.cutoff_index}: {seed_path!r}"
            ) from exc
        seed_history, candidate_histories = collect_cochange_histories(
            self._state,
            seed,
            self.cutoff_index,
        )
        scores = score_cochange_histories(
            self._state,
            seed_history,
            candidate_histories,
            self.cutoff_index,
            decayed=self.decayed,
        )
        ordered = sorted(
            (
                (score, candidate)
                for candidate, score in scores.items()
                if self._passes_threshold(score)
            ),
            key=lambda item: (
                -item[0],
                path_bytes(self._state.id_to_path[item[1]]),
                item[1],
            ),
        )[: self.top_k]
        return SeedRadius(
            seed_path=seed_path,
            candidates=tuple(
                RadiusCandidate(path=self._state.id_to_path[candidate], score=score)
                for score, candidate in ordered
            ),
        )

    def query(self, seed_paths: Sequence[str] | Iterable[str]) -> RadiusQuery:
        """Return per-seed radii and a deterministic union for one claim set.

        Duplicate seeds are removed while preserving caller order.  Paths that
        are themselves seeds are omitted from the union because the direct
        claim already covers them; they remain visible in each per-seed result.
        """

        seeds = tuple(dict.fromkeys(seed_paths))
        per_seed = tuple(self.radius_for(seed_path) for seed_path in seeds)
        seed_set = set(seeds)
        contributions: dict[str, list[tuple[str, float]]] = {}
        for seed_radius in per_seed:
            for candidate in seed_radius.candidates:
                if candidate.path in seed_set:
                    continue
                contributions.setdefault(candidate.path, []).append(
                    (seed_radius.seed_path, candidate.score)
                )
        union = tuple(
            sorted(
                (
                    UnionRadiusCandidate(
                        path=path,
                        score=max(score for _, score in seed_scores),
                        seed_scores=tuple(seed_scores),
                    )
                    for path, seed_scores in contributions.items()
                ),
                key=lambda candidate: (-candidate.score, path_bytes(candidate.path)),
            )
        )
        return RadiusQuery(seeds=seeds, per_seed=per_seed, union=union)

    def write_provenance(self, destination: Path | str) -> None:
        """Atomically persist the exact frozen-index provenance as JSON."""

        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(self._provenance, indent=2, sort_keys=True, ensure_ascii=True)
            + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
