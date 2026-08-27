#!/usr/bin/env python3
"""Command-line entry point for the ARMS planted-marker canary."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Iterable

try:  # ``python -m instruments.arms.canary.canary``
    from .adapters import build_probe_plan, resolve_executable, version_command
    from .instrument import (
        CALIBRATION_CLIS,
        MAX_MODEL_CALLS,
        calibrate,
        check_certificate,
        check_certificate_set,
        probe_prompt,
    )
    from .locations import SUPPORTED_CLIS, environment_manifest, write_json_atomic
except ImportError:  # ``python instruments/arms/canary/canary.py``
    from adapters import build_probe_plan, resolve_executable, version_command
    from instrument import (
        CALIBRATION_CLIS,
        MAX_MODEL_CALLS,
        calibrate,
        check_certificate,
        check_certificate_set,
        probe_prompt,
    )
    from locations import SUPPORTED_CLIS, environment_manifest, write_json_atomic


def _assignments(values: Iterable[str], option: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        key, separator, assigned = value.partition("=")
        if not separator or not key or not assigned:
            raise ValueError(f"{option} expects CLI=VALUE, got {value!r}")
        if key not in SUPPORTED_CLIS:
            raise ValueError(f"{option} has unsupported CLI {key!r}")
        if key in result:
            raise ValueError(f"{option} repeats CLI {key!r}")
        result[key] = assigned
    return result


def _environment_assignments(values: Iterable[str]) -> dict[str, str]:
    result = dict(os.environ)
    for value in values:
        key, separator, assigned = value.partition("=")
        if not separator or not key:
            raise ValueError(f"--env expects NAME=VALUE, got {value!r}")
        result[key] = assigned
    return result


def _print(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False))


def _manifest(args: argparse.Namespace) -> int:
    env = _environment_assignments(args.env)
    manifest = environment_manifest(
        args.cli,
        args.cwd,
        env=env,
        executable=args.executable,
        requested_model=args.model,
        detected_version=args.version,
        draw_id=args.draw_id,
    )
    if args.output:
        write_json_atomic(args.output, manifest)
    _print(manifest)
    return 0


def _plan(args: argparse.Namespace) -> int:
    models = _assignments(args.model, "--model")
    executables = _assignments(args.executable, "--executable")
    surfaces = args.surface
    if len(set(surfaces)) != len(surfaces):
        raise ValueError("duplicate --surface entries are not allowed")
    missing = [surface for surface in surfaces if surface not in models]
    if missing:
        raise ValueError(f"explicit --model entries required for {missing}")
    planned_calls = len(surfaces) * 2
    if planned_calls > MAX_MODEL_CALLS:
        raise ValueError(
            f"planned model calls ({planned_calls}) exceed ceiling ({MAX_MODEL_CALLS})"
        )
    plans: list[dict[str, object]] = []
    for surface in surfaces:
        executable = resolve_executable(surface, executables.get(surface))
        dummy_prompt = probe_prompt("<RUNTIME_CLEAN_ACK>")
        plan = build_probe_plan(
            surface,
            executable=executable,
            model=models[surface],
            prompt=dummy_prompt,
            cwd=args.cwd.resolve(strict=False),
        )
        plans.append(
            {
                "surface": surface,
                "version_command": list(version_command(surface, executable)),
                "planted_probe_command": list(plan.command),
                "clean_probe_command": list(plan.command),
                "prompt_delivery": "stdin" if plan.stdin is not None else "argument",
                "requested_model_identifier": models[surface],
            }
        )
    _print(
        {
            "executes_commands": False,
            "maximum_model_calls": MAX_MODEL_CALLS,
            "planned_model_calls": planned_calls,
            "plans": plans,
        }
    )
    return 0


def _calibrate(args: argparse.Namespace) -> int:
    if not args.confirm_model_calls:
        raise ValueError(
            "calibration launches real model probes; pass --confirm-model-calls explicitly"
        )
    models = _assignments(args.model, "--model")
    executables = _assignments(args.executable, "--executable")
    credential_values = _assignments(args.credential, "--credential")
    credentials = {key: Path(value) for key, value in credential_values.items()}
    certificate_path, certificate = calibrate(
        args.surface,
        models=models,
        executable_overrides=executables,
        credential_sources=credentials,
        output_directory=args.output_directory,
        room_directory=args.room_root,
        timeout_seconds=args.timeout,
    )
    _print(
        {
            "certificate": str(certificate_path),
            "verdict": certificate["verdict"],
            "certified_surfaces": certificate["certified_surfaces"],
            "probe_budget": certificate["probe_budget"],
        }
    )
    return 0 if certificate["verdict"] == "PASS" else 5


def _check(args: argparse.Namespace) -> int:
    result = check_certificate(
        args.certificate,
        required_surfaces=args.require,
        require_sidecar=not args.no_sidecar,
    )
    _print(result)
    return 0 if result["pass"] else 6


def _check_set(args: argparse.Namespace) -> int:
    assignments = _assignments(args.surface_certificate, "--surface-certificate")
    result = check_certificate_set(
        {surface: Path(path) for surface, path in assignments.items()}
    )
    _print(result)
    return 0 if result["pass"] else 6


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build manifests, inspect plans, calibrate planted instruction channels, "
            "and enforce same-day canary certificates."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    manifest = subparsers.add_parser(
        "manifest", help="record instruction candidates without launching a CLI"
    )
    manifest.add_argument("--cli", choices=SUPPORTED_CLIS, required=True)
    manifest.add_argument("--cwd", type=Path, required=True)
    manifest.add_argument("--executable")
    manifest.add_argument("--model")
    manifest.add_argument("--version")
    manifest.add_argument("--draw-id")
    manifest.add_argument("--env", action="append", default=[], metavar="NAME=VALUE")
    manifest.add_argument("--output", type=Path)
    manifest.set_defaults(handler=_manifest)

    plan = subparsers.add_parser(
        "plan", help="print exact probe/version commands without executing them"
    )
    plan.add_argument(
        "--surface", choices=SUPPORTED_CLIS, action="append", required=True
    )
    plan.add_argument("--model", action="append", default=[], metavar="CLI=MODEL")
    plan.add_argument(
        "--executable", action="append", default=[], metavar="CLI=PATH_OR_NAME"
    )
    plan.add_argument("--cwd", type=Path, default=Path.cwd())
    plan.set_defaults(handler=_plan)

    calibration = subparsers.add_parser(
        "calibrate", help="launch two real canary probes per explicitly selected CLI"
    )
    calibration.add_argument(
        "--surface", choices=CALIBRATION_CLIS, action="append", required=True
    )
    calibration.add_argument(
        "--model", action="append", default=[], required=True, metavar="CLI=MODEL"
    )
    calibration.add_argument(
        "--executable", action="append", default=[], metavar="CLI=PATH_OR_NAME"
    )
    calibration.add_argument(
        "--credential",
        action="append",
        default=[],
        metavar="CLI=FILE",
        help="copy one explicitly named credential file into each isolated leg",
    )
    calibration.add_argument(
        "--output-directory",
        type=Path,
        default=Path(__file__).resolve().parent / "certificates",
    )
    calibration.add_argument(
        "--room-root",
        type=Path,
        help=(
            "persistent clean-room parent; defaults to C:\\arms-canary-rooms on "
            "Windows or the system temporary root elsewhere"
        ),
    )
    calibration.add_argument("--timeout", type=float, default=180.0)
    calibration.add_argument(
        "--confirm-model-calls",
        action="store_true",
        help="required acknowledgement that calibration spends real model calls",
    )
    calibration.set_defaults(handler=_calibrate)

    check = subparsers.add_parser(
        "check", help="gate on certificate integrity, date, and required surfaces"
    )
    check.add_argument("--certificate", type=Path, required=True)
    check.add_argument(
        "--require", choices=SUPPORTED_CLIS, action="append", required=True
    )
    check.add_argument(
        "--no-sidecar",
        action="store_true",
        help="testing/legacy escape hatch; production gates should require the sidecar",
    )
    check.set_defaults(handler=_check)

    check_set = subparsers.add_parser(
        "check-set",
        help="gate on per-surface evidence from one or more immutable certificates",
    )
    check_set.add_argument(
        "--surface-certificate",
        action="append",
        required=True,
        metavar="CLI=PATH",
    )
    check_set.set_defaults(handler=_check_set)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (FileNotFoundError, FileExistsError, ValueError) as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    sys.exit(main())
