from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterable

from .util import ShimError, canonical_json, event_semantic_errors, sha256_bytes


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("rb") as handle:
        for line_number, raw in enumerate(handle, start=1):
            if not raw.strip():
                continue
            try:
                value = json.loads(raw)
            except json.JSONDecodeError as error:
                raise ShimError(f"invalid JSONL at {path}:{line_number}: {error}") from error
            if not isinstance(value, dict):
                raise ShimError(f"non-object JSONL row at {path}:{line_number}")
            rows.append(value)
    return rows


def verify_event_log(path: Path) -> dict[str, Any]:
    rows = read_jsonl(path)
    errors: list[str] = []
    previous = "0" * 64
    required = {
        "schema_version",
        "sequence",
        "run_id",
        "draw_id",
        "site",
        "arm",
        "stratum",
        "principal",
        "subject",
        "op",
        "paths",
        "timestamp_utc",
        "monotonic_ns",
        "detail",
        "previous_event_sha256",
        "event_sha256",
    }
    for index, row in enumerate(rows):
        missing = sorted(required - set(row))
        if missing:
            errors.append(f"row {index}: missing {missing}")
        errors.extend(
            f"row {index}: {message}" for message in event_semantic_errors(row)
        )
        if row.get("sequence") != index:
            errors.append(f"row {index}: sequence={row.get('sequence')!r}")
        if row.get("previous_event_sha256") != previous:
            errors.append(f"row {index}: previous-event hash mismatch")
        claimed = row.get("event_sha256")
        unhashed = dict(row)
        unhashed.pop("event_sha256", None)
        observed = sha256_bytes(canonical_json(unhashed))
        if claimed != observed:
            errors.append(f"row {index}: event hash mismatch")
        previous = str(claimed)
    return {
        "path": str(path.resolve()),
        "events": len(rows),
        "pass": not errors,
        "errors": errors,
        "terminal_hash": previous,
    }


def normalize_rows(rows: Iterable[dict[str, Any]]) -> bytes:
    output = bytearray()
    previous_by_draw: dict[str, str] = {}
    sequence_by_draw: dict[str, int] = {}
    for source in rows:
        row = json.loads(json.dumps(source))
        draw_id = str(row["draw_id"])
        expected_sequence = sequence_by_draw.get(draw_id, 0)
        if row.get("sequence") != expected_sequence:
            raise ShimError(
                f"non-contiguous sequence for {draw_id}: "
                f"{row.get('sequence')} != {expected_sequence}"
            )
        row["timestamp_utc"] = "<TIMESTAMP-NORMALIZED>"
        row["monotonic_ns"] = expected_sequence
        row["previous_event_sha256"] = previous_by_draw.get(draw_id, "0" * 64)
        row.pop("event_sha256", None)
        row["event_sha256"] = sha256_bytes(canonical_json(row))
        previous_by_draw[draw_id] = row["event_sha256"]
        sequence_by_draw[draw_id] = expected_sequence + 1
        output.extend(canonical_json(row))
        output.extend(b"\n")
    return bytes(output)


def event_paths(run_root: Path) -> tuple[Path, ...]:
    paths = sorted((run_root / "draws").glob("*/events.jsonl"))
    contract = run_root / "contract-screen" / "events.jsonl"
    if contract.is_file():
        paths.append(contract)
    return tuple(paths)


def normalized_run_bytes(run_root: Path) -> bytes:
    rows: list[dict[str, Any]] = []
    for path in event_paths(run_root):
        rows.extend(read_jsonl(path))
    return normalize_rows(rows)


def write_exclusive(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise ShimError(f"refusing to overwrite: {path}")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()
