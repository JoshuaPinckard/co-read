from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any, Mapping

from .util import ShimError, git_text, safe_relative, sha256_file


@dataclasses.dataclass(frozen=True)
class Side:
    label: str
    source_name: str
    parent: str
    intent_subject: str
    intent_body: str
    source_patch: Path
    source_patch_sha256: str
    test_patch: Path
    test_patch_sha256: str
    source_paths: tuple[str, ...]
    test_paths: tuple[str, ...]
    focal_targets: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class Site:
    repo: str
    repo_slug: str
    merge: str
    base_commit: str
    base_tree: str
    answer_commit: str
    answer_tree: str
    stratum: str
    mined_class: str
    strict_overlap_paths: tuple[str, ...]
    corpus_line: int
    joint_status: str
    mirror: Path
    manifest: Path
    sides: Mapping[str, Side]

    @property
    def site_id(self) -> str:
        return f"{self.repo_slug}/{self.merge}"

    def event_identity(self) -> dict[str, Any]:
        return {
            "id": self.site_id,
            "repo": self.repo,
            "merge": self.merge,
            "base_commit": self.base_commit,
            "base_tree": self.base_tree,
            "answer_commit": self.answer_commit,
            "answer_tree": self.answer_tree,
            "corpus_line": self.corpus_line,
            "mined_class": self.mined_class,
            "strict_overlap_paths": list(self.strict_overlap_paths),
            "joint_status": self.joint_status,
            "prepared_manifest_sha256": sha256_file(self.manifest),
            "sides": {
                label: {
                    "parent": side.parent,
                    "source_patch_sha256": side.source_patch_sha256,
                    "test_patch_sha256": side.test_patch_sha256,
                    "source_paths": list(side.source_paths),
                    "test_paths": list(side.test_paths),
                    "focal_targets": list(side.focal_targets),
                    "intent_sha256": __import__("hashlib").sha256(
                        (side.intent_subject + "\n\n" + side.intent_body).encode("utf-8")
                    ).hexdigest(),
                }
                for label, side in sorted(self.sides.items())
            },
        }


def _patch_record(record: Any) -> tuple[str, str]:
    if isinstance(record, dict):
        return str(record["path"]), str(record["sha256"])
    raise ShimError(f"unsupported patch record: {record!r}")


def _intent(mirror: Path, parent: str) -> tuple[str, str]:
    raw = git_text(
        ["--git-dir", str(mirror), "show", "-s", "--format=%s%x00%b", parent],
        cwd=mirror.parent,
    )
    subject, _, body = raw.partition("\x00")
    return subject.strip(), body.strip()


def load_python_site(
    project_root: Path,
    *,
    merge: str,
    stratum: str,
    mirror: Path,
) -> Site:
    arms_root = project_root / "exploratory" / "arms"
    payload = json.loads((arms_root / "sites.json").read_text(encoding="utf-8"))
    rows = [row for row in payload["sites"] if row.get("merge") == merge]
    if len(rows) != 1:
        raise ShimError(f"site {merge} occurs {len(rows)} times")
    row = rows[0]
    if row.get("validated") is not True or row.get("verdict") != "VALIDATED":
        raise ShimError(f"site is not independently two-sided validated: {merge}")
    manifest_path = project_root / row["evidence"]["prepared_manifest"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest["merge"] != merge or manifest["base"] != row["base"]:
        raise ShimError(f"site/manifest identity mismatch: {merge}")
    expected_manifest_hash = row["evidence"]["prepared_manifest_sha256"]
    if sha256_file(manifest_path) != expected_manifest_hash:
        raise ShimError(f"site manifest hash mismatch: {manifest_path}")
    if not mirror.is_dir():
        raise ShimError(f"bare source mirror is absent: {mirror}")
    if git_text(
        ["--git-dir", str(mirror), "rev-parse", "--is-bare-repository"],
        cwd=project_root,
    ) != "true":
        raise ShimError(f"source mirror is not bare: {mirror}")
    base_tree = git_text(
        ["--git-dir", str(mirror), "rev-parse", f"{row['base']}^{{tree}}"],
        cwd=project_root,
    )
    answer_tree = git_text(
        ["--git-dir", str(mirror), "rev-parse", f"{merge}^{{tree}}"],
        cwd=project_root,
    )

    corpus_path = project_root / "corpus" / "conflicts" / f"{row['repo_slug']}.jsonl"
    corpus_lines = corpus_path.read_text(encoding="utf-8").splitlines()
    corpus_line = int(row["corpus_line"])
    corpus_row = json.loads(corpus_lines[corpus_line - 1])
    if corpus_row.get("merge") != merge:
        raise ShimError(f"corpus line identity mismatch: {merge}")
    overlap = corpus_row["overlap"]
    mined_class = str(overlap["classification"])
    strict_paths = tuple(sorted(map(str, overlap.get("strict_overlap_paths", []))))
    # Keep the miner's strict-disjoint and endpoint-contact populations
    # distinct.  exploratory/arms/DISJOINT-SITES.md explicitly records that
    # ``boundary_only`` is a sensitivity class and must not be relabelled as
    # the preregistered ``same_file_disjoint`` stratum.
    allowed_mapping = {
        "byte-intersecting": "overlap",
        "same-file-disjoint": "same_file_disjoint",
        "boundary-only": "boundary_only",
        "contradictory-task": "overlap",
    }
    if stratum not in allowed_mapping or mined_class != allowed_mapping[stratum]:
        raise ShimError(
            f"frozen stratum mapping mismatch: {stratum} requires "
            f"{allowed_mapping.get(stratum)!r}, observed {mined_class!r}"
        )
    if stratum == "byte-intersecting" and not strict_paths:
        raise ShimError("byte-intersecting site has no frozen strict overlap path")

    side_values: dict[str, Side] = {}
    for label, source_name in (("A", "parent1"), ("B", "parent2")):
        side_row = row["sides"][source_name]
        side_manifest = manifest["sides"][source_name]
        source_path, source_hash = _patch_record(side_manifest["source_patch"])
        test_path, test_hash = _patch_record(side_manifest["test_patch"])
        source_patch = project_root / source_path
        test_patch = project_root / test_path
        if sha256_file(source_patch) != source_hash:
            raise ShimError(f"source patch hash mismatch: {source_patch}")
        if sha256_file(test_patch) != test_hash:
            raise ShimError(f"test patch hash mismatch: {test_patch}")
        focal = side_row.get("focal_selection", {}).get("frozen_node_ids")
        if not focal:
            focal = side_row.get("focal_node_ids")
        if not focal:
            raise ShimError(f"side {source_name} has no frozen focal targets")
        subject, body = _intent(mirror, str(side_manifest["parent"]))
        side_values[label] = Side(
            label=label,
            source_name=source_name,
            parent=str(side_manifest["parent"]),
            intent_subject=subject,
            intent_body=body,
            source_patch=source_patch,
            source_patch_sha256=source_hash,
            test_patch=test_patch,
            test_patch_sha256=test_hash,
            source_paths=tuple(sorted(safe_relative(p) for p in side_manifest["source_paths"])),
            test_paths=tuple(sorted(safe_relative(p) for p in side_manifest["test_paths"])),
            focal_targets=tuple(sorted(safe_relative(p) for p in focal)),
        )
    joint_status = str(row.get("joint_source_check", {}).get("status", "UNKNOWN"))
    if stratum == "contradictory-task" and joint_status != "MUTUALLY_UNSATISFIABLE":
        raise ShimError(
            "contradictory-task stratum requires a frozen mutually-unsatisfiable "
            f"joint check, observed {joint_status!r}"
        )
    return Site(
        repo=str(row["repo"]),
        repo_slug=str(row["repo_slug"]),
        merge=merge,
        base_commit=str(row["base"]),
        base_tree=base_tree,
        answer_commit=merge,
        answer_tree=answer_tree,
        stratum=stratum,
        mined_class=mined_class,
        strict_overlap_paths=strict_paths,
        corpus_line=corpus_line,
        joint_status=joint_status,
        mirror=mirror.resolve(),
        manifest=manifest_path.resolve(),
        sides=side_values,
    )


def named_intent_paths(side: Side, tracked_paths: set[str]) -> tuple[str, ...]:
    """Match literal repository paths in frozen intent text, never prose guesses."""
    text = f"{side.intent_subject}\n{side.intent_body}"
    matches = []
    for path in sorted(tracked_paths, key=lambda value: (-len(value), value)):
        if path in text:
            matches.append(path)
    return tuple(sorted(set(matches)))
