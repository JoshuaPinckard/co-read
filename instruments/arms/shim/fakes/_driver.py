from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath


def safe_path(root: Path, value: str) -> Path:
    pure = PurePosixPath(value.replace("\\", "/"))
    if pure.is_absolute() or not pure.parts or ".." in pure.parts:
        raise RuntimeError(f"unsafe path: {value!r}")
    candidate = root.joinpath(*pure.parts).resolve()
    resolved = root.resolve()
    if candidate != resolved and resolved not in candidate.parents:
        raise RuntimeError(f"path escapes root: {value!r}")
    return candidate


def git_apply(root: Path, patch: str) -> int:
    result = subprocess.run(
        [
            "git",
            "-c",
            "core.autocrlf=false",
            "-c",
            "core.safecrlf=false",
            "-C",
            str(root),
            "apply",
            "--binary",
            "--whitespace=nowarn",
            str(Path(patch).resolve()),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        check=False,
    )
    if result.returncode != 0:
        sys.stderr.buffer.write(result.stderr)
    return result.returncode


def main(mode: str) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", required=True)
    parser.add_argument("--declare", action="store_true")
    args = parser.parse_args()
    spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
    if args.declare:
        print(json.dumps(spec.get("declared_paths", []), separators=(",", ":")))
        return 0
    root = Path.cwd()
    pre_delay = float(spec.get("pre_delay_seconds", 0.0))
    post_delay = float(spec.get("post_delay_seconds", 0.0))
    if pre_delay:
        time.sleep(pre_delay)
    started_signal = spec.get("started_signal")
    if started_signal:
        ready = Path(str(started_signal)).resolve()
        ready.parent.mkdir(parents=True, exist_ok=True)
        ready.write_bytes(b"ready-for-write\n")
    write_release_signal = spec.get("write_release_signal")
    if write_release_signal:
        release = Path(str(write_release_signal)).resolve()
        deadline = time.monotonic() + float(
            spec.get("write_release_timeout_seconds", 30.0)
        )
        while not release.is_file():
            if time.monotonic() >= deadline:
                raise RuntimeError("timed out waiting for scripted write release")
            time.sleep(0.01)
    operation_returncode = 0
    if mode in {"collision", "cheater", "redraw"}:
        operation_returncode = git_apply(root, spec["source_patch"])
    elif mode == "benign":
        target = safe_path(root, spec["benign_path"])
        marker = str(spec["benign_marker"]).encode("utf-8")
        with target.open("ab") as handle:
            handle.write(b"\n" + marker + b"\n")
    elif mode == "answer":
        listing = subprocess.run(
            ["git", "-C", str(root), "ls-tree", "-r", "--name-only", "-z", spec["answer_commit"]],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            check=False,
        )
        if listing.returncode != 0:
            sys.stderr.buffer.write(listing.stderr)
            operation_returncode = listing.returncode
        else:
            answer_paths = {
                item.decode("utf-8", errors="surrogateescape")
                for item in listing.stdout.split(b"\0")
                if item
            }
            existing = [
                relative for relative in spec["source_paths"] if relative in answer_paths
            ]
            if existing:
                result = subprocess.run(
                    [
                        "git",
                        "-C",
                        str(root),
                        "checkout",
                        spec["answer_commit"],
                        "--",
                        *existing,
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    shell=False,
                    check=False,
                )
                if result.returncode != 0:
                    sys.stderr.buffer.write(result.stderr)
                    operation_returncode = result.returncode
            if operation_returncode == 0:
                for relative in spec["source_paths"]:
                    target = safe_path(root, relative)
                    if relative not in answer_paths:
                        if target.is_file() or target.is_symlink():
                            target.unlink()
    elif mode == "alternator":
        target = safe_path(root, spec["alternation_path"])
        data = target.read_bytes()
        token = str(spec["alternation_token"]).encode("ascii")
        start = int(spec["alternation_start"])
        end = int(spec["alternation_end"])
        target.write_bytes(data[:start] + token + data[end:])
    elif mode == "staller":
        time.sleep(float(spec.get("stall_seconds", 60.0)))
    else:
        raise RuntimeError(f"unsupported fake mode: {mode}")
    if mode == "cheater" and operation_returncode == 0:
        target = safe_path(root, spec["cheat_path"])
        with target.open("ab") as handle:
            handle.write(b"\n# ARMS_SHIM_SCRIPTED_TEST_CHEAT\n")
    write_signal = spec.get("write_signal")
    if write_signal:
        signal = Path(str(write_signal)).resolve()
        signal.parent.mkdir(parents=True, exist_ok=True)
        signal.write_bytes(b"writes-complete\n")
    if post_delay:
        time.sleep(post_delay)
    print(
        json.dumps(
            {
                "fake": mode,
                "status": "finished",
                "operation_returncode": operation_returncode,
            },
            separators=(",", ":"),
        )
    )
    return operation_returncode
