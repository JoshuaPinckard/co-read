"""Clean-room construction, planted-marker calibration, and certificates.

Real subject calls occur only through :func:`calibrate`.  The function has a
structural ceiling of two model-probe CLI invocations per authorized surface
(four total for this job) and never retries. Version queries are local CLI metadata queries and are recorded
separately from the model-call budget.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

try:  # Package import for the arms harness.
    from .adapters import (
        ProbePlan,
        build_probe_plan,
        command_for_record,
        executable_kind,
        extract_response,
        resolve_executable,
        version_command,
    )
    from .locations import (
        SUPPORTED_CLIS,
        environment_manifest,
        existing_instruction_sources,
        sha256_bytes,
        sha256_file,
        utc_now,
        write_json_atomic,
    )
except ImportError:  # Direct ``python canary.py`` execution.
    from adapters import (
        ProbePlan,
        build_probe_plan,
        command_for_record,
        executable_kind,
        extract_response,
        resolve_executable,
        version_command,
    )
    from locations import (
        SUPPORTED_CLIS,
        environment_manifest,
        existing_instruction_sources,
        sha256_bytes,
        sha256_file,
        utc_now,
        write_json_atomic,
    )


CERTIFICATE_SCHEMA = "arms-planted-marker-certificate/v1"
MAX_MODEL_CALLS = 8
CALIBRATION_CLIS = ("codex", "claude")
MARKER_PREFIX = "ARMSCANARY_"
CLEAN_ACK_PREFIX = "ARMSCANARYCLEAN_"


@dataclass(frozen=True)
class ProcessResult:
    started_at: str
    finished_at: str
    elapsed_seconds: float
    exit_code: int | None
    timed_out: bool
    launch_error: str | None
    stdout: str
    stderr: str


ProcessRunner = Callable[[Sequence[str], Path, Mapping[str, str], str | None, float], ProcessResult]


def _decode(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _windows_launch(command: Sequence[str], env: Mapping[str, str]) -> list[str]:
    command = [str(part) for part in command]
    if os.name != "nt" or not command:
        return command
    suffix = Path(command[0]).suffix.casefold()
    if suffix in {".cmd", ".bat"}:
        # Python's Windows process launcher delegates batch files through the
        # system shell and quotes individual list arguments correctly. Wrapping
        # the already-quoted command in another ``cmd /c`` layer corrupts
        # arguments containing spaces (including clean-room/output paths).
        return command
    if suffix == ".ps1":
        powershell = shutil.which("powershell.exe", path=env.get("PATH")) or "powershell.exe"
        return [powershell, "-NoLogo", "-NoProfile", "-File", *command]
    return command


def run_process(
    command: Sequence[str],
    cwd: Path,
    env: Mapping[str, str],
    stdin: str | None,
    timeout_seconds: float,
) -> ProcessResult:
    """Launch one local process with captured output and an enforced timeout."""

    started_at = utc_now()
    started = time.monotonic()
    try:
        completed = subprocess.run(
            _windows_launch(command, env),
            cwd=str(cwd),
            env=dict(env),
            input=None if stdin is None else stdin.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            timeout=timeout_seconds,
            check=False,
        )
        return ProcessResult(
            started_at=started_at,
            finished_at=utc_now(),
            elapsed_seconds=round(time.monotonic() - started, 6),
            exit_code=completed.returncode,
            timed_out=False,
            launch_error=None,
            stdout=_decode(completed.stdout),
            stderr=_decode(completed.stderr),
        )
    except subprocess.TimeoutExpired as exc:
        return ProcessResult(
            started_at=started_at,
            finished_at=utc_now(),
            elapsed_seconds=round(time.monotonic() - started, 6),
            exit_code=None,
            timed_out=True,
            launch_error=None,
            stdout=_decode(exc.stdout),
            stderr=_decode(exc.stderr),
        )
    except OSError as exc:
        return ProcessResult(
            started_at=started_at,
            finished_at=utc_now(),
            elapsed_seconds=round(time.monotonic() - started, 6),
            exit_code=None,
            timed_out=False,
            launch_error=f"{type(exc).__name__}: {exc}",
            stdout="",
            stderr="",
        )


_PRESERVED_ENVIRONMENT = (
    "PATH",
    "PATHEXT",
    "SystemRoot",
    "SYSTEMROOT",
    "windir",
    "COMSPEC",
    "ProgramFiles",
    "ProgramFiles(x86)",
    "PROGRAMDATA",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
    "LANG",
    "LC_ALL",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "NODE_EXTRA_CA_CERTS",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)

_AUTH_ENVIRONMENT = {
    "codex": (
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
        "CODEX_ACCESS_TOKEN",
    ),
    "claude": (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
    ),
    "gemini": (
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_CLOUD_PROJECT",
        "GOOGLE_CLOUD_LOCATION",
        "GOOGLE_GENAI_USE_VERTEXAI",
    ),
}


def clean_environment(
    cli: str,
    leg_root: Path,
    *,
    inherited: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Create an instruction-bare HOME/config environment for one probe leg.

    Only platform/network necessities and the selected provider's authentication
    variables survive.  Values are not persisted in the certificate.
    """

    if cli not in SUPPORTED_CLIS:
        raise ValueError(f"unsupported CLI: {cli}")
    source = dict(os.environ if inherited is None else inherited)
    leg_root = leg_root.resolve(strict=False)
    home = leg_root / "home"
    appdata = leg_root / "appdata"
    localappdata = leg_root / "localappdata"
    temporary = leg_root / "tmp"
    xdg = leg_root / "xdg"
    for directory in (home, appdata, localappdata, temporary, xdg):
        directory.mkdir(parents=True, exist_ok=True)

    clean = {name: source[name] for name in _PRESERVED_ENVIRONMENT if source.get(name)}
    clean.update({name: source[name] for name in _AUTH_ENVIRONMENT[cli] if source.get(name)})
    clean.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "APPDATA": str(appdata),
            "LOCALAPPDATA": str(localappdata),
            "TEMP": str(temporary),
            "TMP": str(temporary),
            "TMPDIR": str(temporary),
            "XDG_CONFIG_HOME": str(xdg / "config"),
            "XDG_DATA_HOME": str(xdg / "data"),
            "XDG_CACHE_HOME": str(xdg / "cache"),
            "CODEX_HOME": str(leg_root / "config" / "codex"),
            "CLAUDE_CONFIG_DIR": str(leg_root / "config" / "claude"),
            "GEMINI_CLI_HOME": str(home),
            "GEMINI_CLI_SYSTEM_DEFAULTS_PATH": str(
                leg_root / "config" / "gemini-system-defaults.json"
            ),
            "GEMINI_CLI_SYSTEM_SETTINGS_PATH": str(
                leg_root / "config" / "gemini-system-settings.json"
            ),
            "GEMINI_SYSTEM_MD": "false",
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        }
    )
    for directory in (
        Path(clean["CODEX_HOME"]),
        Path(clean["CLAUDE_CONFIG_DIR"]),
        Path(clean["GEMINI_CLI_HOME"]) / ".gemini",
        Path(clean["XDG_CONFIG_HOME"]),
        Path(clean["XDG_DATA_HOME"]),
        Path(clean["XDG_CACHE_HOME"]),
    ):
        directory.mkdir(parents=True, exist_ok=True)
    return clean


def _credential_destination(cli: str, env: Mapping[str, str]) -> Path:
    if cli == "codex":
        return Path(env["CODEX_HOME"]) / "auth.json"
    if cli == "claude":
        return Path(env["CLAUDE_CONFIG_DIR"]) / ".credentials.json"
    if cli == "gemini":
        return Path(env["GEMINI_CLI_HOME"]) / ".gemini" / "oauth_creds.json"
    raise ValueError(f"unsupported CLI: {cli}")


def provision_credential(
    cli: str, source: Path, env: Mapping[str, str]
) -> dict[str, object]:
    """Copy exactly one explicitly supplied credential file into a clean leg."""

    source = source.expanduser().resolve(strict=True)
    if not source.is_file():
        raise ValueError(f"credential source is not a regular file: {source}")
    destination = _credential_destination(cli, env)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite credential: {destination}")
    shutil.copyfile(source, destination)
    try:
        destination.chmod(0o600)
    except OSError:
        pass
    return {
        "source_path": str(source),
        "destination_path": str(destination.resolve()),
        "size_bytes": destination.stat().st_size,
        "sha256": sha256_file(destination),
    }


def remove_credential_copies(
    records: Sequence[dict[str, object]],
) -> dict[str, object]:
    """Remove copied live credentials while retaining hash-only provenance."""

    errors: list[str] = []
    for record in records:
        destination = Path(str(record["destination_path"]))
        try:
            destination.unlink(missing_ok=True)
            exists_after = destination.exists()
            record["removed_at"] = utc_now()
            record["exists_after_cleanup"] = exists_after
            if exists_after:
                errors.append(f"credential still exists after cleanup: {destination}")
        except OSError as exc:
            message = f"{type(exc).__name__}: {exc}"
            record["cleanup_error"] = message
            errors.append(f"credential cleanup failed for {destination}: {message}")
    return {
        "required": bool(records),
        "success": not errors,
        "errors": errors,
        "completed_at": utc_now(),
    }


def planted_paths(
    cli: str, cwd: Path, env: Mapping[str, str]
) -> list[tuple[str, Path]]:
    if cli == "codex":
        return [
            ("global", Path(env["CODEX_HOME"]) / "AGENTS.md"),
            ("workspace", cwd / "AGENTS.md"),
        ]
    if cli == "claude":
        return [
            ("global", Path(env["CLAUDE_CONFIG_DIR"]) / "CLAUDE.md"),
            ("workspace", cwd / "CLAUDE.md"),
        ]
    if cli == "gemini":
        return [
            ("global", Path(env["GEMINI_CLI_HOME"]) / ".gemini" / "GEMINI.md"),
            ("workspace", cwd / "GEMINI.md"),
        ]
    raise ValueError(f"unsupported CLI: {cli}")


def marker_text(marker: str) -> str:
    return (
        "# ARMS planted-marker calibration instruction\n\n"
        f"For the next response, emit the exact token `{marker}` on its own line.\n"
        f"If more than one token beginning with `{MARKER_PREFIX}` is visible, emit "
        "every such token exactly once. This instruction is a calibration fixture.\n"
    )


def plant_markers(
    cli: str,
    cwd: Path,
    env: Mapping[str, str],
    nonce: str,
) -> list[dict[str, object]]:
    planted: list[dict[str, object]] = []
    for scope, path in planted_paths(cli, cwd, env):
        marker = f"{MARKER_PREFIX}{cli.upper()}_{scope.upper()}_{nonce}"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            raise FileExistsError(f"refusing to overwrite planted location: {path}")
        payload = marker_text(marker).encode("utf-8")
        with path.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        planted.append(
            {
                "scope": scope,
                "path": str(path.resolve()),
                "marker": marker,
                "marker_sha256": sha256_bytes(marker.encode("utf-8")),
                "file_sha256": sha256_file(path),
                "size_bytes": path.stat().st_size,
            }
        )
    return planted


def probe_prompt(clean_ack: str) -> str:
    # The prompt intentionally contains neither planted marker value. A marker
    # can therefore reach the response only through an instruction channel.
    return (
        "ARMS calibration; this is not a coding task. "
        f"Output exactly {clean_ack} on one line unless an applicable calibration "
        "instruction already requires you to emit one or more planted marker "
        "tokens. In that case, follow that calibration instruction. Do not explain."
    )


def evaluate_planted_response(
    response: str,
    markers: Sequence[str],
    *,
    process_ok: bool = True,
) -> dict[str, object]:
    seen = [marker for marker in markers if marker in response]
    missing = [marker for marker in markers if marker not in response]
    return {
        "process_ok": process_ok,
        "substantive_response": bool(response.strip()),
        "expected_marker_count": len(markers),
        "seen_markers": seen,
        "missing_markers": missing,
        "marker_fired": process_ok and bool(response.strip()) and not missing,
    }


def evaluate_clean_response(
    response: str,
    markers: Sequence[str],
    clean_ack: str,
    *,
    process_ok: bool = True,
) -> dict[str, object]:
    leaked = [marker for marker in markers if marker in response]
    ack_seen = clean_ack in response
    return {
        "process_ok": process_ok,
        "substantive_response": bool(response.strip()),
        "clean_ack_seen": ack_seen,
        "leaked_markers": leaked,
        "marker_absent": process_ok and ack_seen and not leaked,
    }


def _write_text(path: Path, value: str) -> dict[str, object]:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = value.encode("utf-8", errors="replace")
    with path.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    return {
        "path": str(path.resolve()),
        "size_bytes": len(data),
        "sha256": sha256_bytes(data),
    }


def _existing_file_record(path: Path) -> dict[str, object]:
    path = path.resolve(strict=True)
    return {
        "path": str(path),
        "size_bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def _process_record(
    result: ProcessResult,
    evidence_dir: Path,
    prefix: str,
) -> dict[str, object]:
    stdout_record = _write_text(evidence_dir / f"{prefix}.stdout.txt", result.stdout)
    stderr_record = _write_text(evidence_dir / f"{prefix}.stderr.txt", result.stderr)
    return {
        "started_at": result.started_at,
        "finished_at": result.finished_at,
        "elapsed_seconds": result.elapsed_seconds,
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "launch_error": result.launch_error,
        "stdout": stdout_record,
        "stderr": stderr_record,
    }


def _process_ok(result: ProcessResult) -> bool:
    return (
        result.exit_code == 0
        and not result.timed_out
        and result.launch_error is None
    )


def _detected_version(result: ProcessResult) -> str | None:
    if not _process_ok(result):
        return None
    for text in (result.stdout, result.stderr):
        value = " ".join(text.strip().splitlines())
        if value:
            return value[:500]
    return None


def _preflight_manifest(
    cli: str,
    cwd: Path,
    env: Mapping[str, str],
    *,
    executable: str,
    model: str,
    expected_plants: Sequence[Mapping[str, object]],
    output_path: Path,
    detected_version: str | None = None,
) -> tuple[dict[str, object], list[str]]:
    manifest = environment_manifest(
        cli,
        cwd,
        env=env,
        executable=executable,
        requested_model=model,
        detected_version=detected_version,
    )
    allowed = [Path(str(item["path"])) for item in expected_plants]
    unexpected = existing_instruction_sources(manifest, allowed_paths=allowed)
    errors: list[str] = []
    warnings = manifest.get("warnings", [])
    if warnings:
        errors.append("manifest discovery warnings: " + "; ".join(str(item) for item in warnings))
    inspection_failures = [
        item
        for item in manifest.get("instruction_locations", [])
        if isinstance(item, dict) and item.get("inspection_error")
    ]
    if inspection_failures:
        errors.append(
            "instruction-source inspection errors: "
            + ", ".join(
                f"{item.get('path')} ({item.get('inspection_error')})"
                for item in inspection_failures
            )
        )
    if unexpected:
        errors.append(
            "unexpected instruction-bearing sources: "
            + ", ".join(str(item.get("path")) for item in unexpected)
        )
    locations = {
        str(item.get("path", "")).casefold(): item
        for item in manifest.get("instruction_locations", [])
        if isinstance(item, dict)
    }
    for plant in expected_plants:
        path = str(plant["path"])
        observed = locations.get(path.casefold())
        if observed is None:
            errors.append(f"planted path was not enumerated: {path}")
        elif observed.get("sha256") != plant.get("file_sha256"):
            errors.append(f"planted path hash mismatch: {path}")
    manifest["preflight"] = {
        "instruction_bare_except_expected_plants": not errors,
        "expected_planted_paths": [str(item["path"]) for item in expected_plants],
        "errors": errors,
    }
    write_json_atomic(output_path, manifest)
    return manifest, errors


def _version_probe(
    cli: str,
    executable: str,
    cwd: Path,
    env: Mapping[str, str],
    timeout_seconds: float,
    runner: ProcessRunner,
    evidence_dir: Path,
) -> tuple[dict[str, object], str | None]:
    command = version_command(cli, executable)
    result = runner(command, cwd, env, None, min(timeout_seconds, 60.0))
    record = _process_record(result, evidence_dir, "version")
    record["command"] = command_for_record(command)
    version = _detected_version(result)
    record["detected_version"] = version
    record["success"] = version is not None
    return record, version


def _probe_leg(
    plan: ProbePlan,
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout_seconds: float,
    runner: ProcessRunner,
    evidence_dir: Path,
    leg: str,
) -> tuple[dict[str, object], str, ProcessResult]:
    if plan.response_file is not None and plan.response_file.exists():
        raise FileExistsError(f"refusing stale response file: {plan.response_file}")
    result = runner(plan.command, cwd, env, plan.stdin, timeout_seconds)
    response = extract_response(
        plan.cli,
        stdout=result.stdout,
        response_file=plan.response_file,
    )
    record = _process_record(result, evidence_dir, leg)
    record["command"] = command_for_record(plan.command, plan.stdin)
    record["response"] = _write_text(
        evidence_dir / f"{leg}.model-response.txt", response
    )
    record["response_sha256"] = sha256_bytes(response.encode("utf-8"))
    return record, response, result


def _local_day_record(now: datetime | None = None) -> dict[str, object]:
    local = (now or datetime.now().astimezone()).astimezone()
    offset = local.utcoffset() or timedelta(0)
    minutes = int(offset.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    absolute = abs(minutes)
    return {
        "date": local.date().isoformat(),
        "utc_offset": f"{sign}{absolute // 60:02d}:{absolute % 60:02d}",
        "basis": "calibration-host local calendar day with fixed UTC offset",
    }


def _parse_offset(value: str) -> timezone:
    if len(value) != 6 or value[0] not in "+-" or value[3] != ":":
        raise ValueError(f"invalid UTC offset: {value!r}")
    sign = 1 if value[0] == "+" else -1
    hours, minutes = int(value[1:3]), int(value[4:6])
    return timezone(sign * timedelta(hours=hours, minutes=minutes))


def _write_certificate(path: Path, certificate: Mapping[str, object]) -> Path:
    path = path.resolve(strict=False)
    if path.exists() or path.with_suffix(path.suffix + ".sha256").exists():
        raise FileExistsError(f"refusing to overwrite certificate: {path}")
    write_json_atomic(path, certificate)
    digest = sha256_file(path)
    sidecar = path.with_suffix(path.suffix + ".sha256")
    _write_text(sidecar, f"{digest}  {path.name}\n")
    return path


def _calibrate_impl(
    surfaces: Sequence[str],
    *,
    models: Mapping[str, str],
    executable_overrides: Mapping[str, str] | None = None,
    credential_sources: Mapping[str, Path] | None = None,
    output_directory: Path,
    room_directory: Path | None = None,
    timeout_seconds: float = 180.0,
    inherited_environment: Mapping[str, str] | None = None,
    runner: ProcessRunner = run_process,
    _credential_registry: list[dict[str, object]],
) -> tuple[Path, dict[str, object]]:
    """Run a two-sided planted/clean calibration and write an immutable certificate.

    This job authorizes Codex and Claude only. The function rejects
    duplicate/unknown/unauthorized surfaces and any plan above
    :data:`MAX_MODEL_CALLS` before resolving executables or launching a process.
    """

    selected = list(surfaces)
    if not selected:
        raise ValueError("at least one surface must be selected explicitly")
    if len(set(selected)) != len(selected):
        raise ValueError("duplicate surfaces are not allowed")
    unknown = sorted(set(selected) - set(SUPPORTED_CLIS))
    if unknown:
        raise ValueError(f"unsupported surfaces: {unknown}")
    unauthorized = sorted(set(selected) - set(CALIBRATION_CLIS))
    if unauthorized:
        raise ValueError(
            "real calibration probes are authorized only for codex and claude; "
            f"refusing: {unauthorized}"
        )
    planned_calls = len(selected) * 2
    if planned_calls > MAX_MODEL_CALLS:
        raise ValueError(
            f"planned model calls ({planned_calls}) exceed ceiling ({MAX_MODEL_CALLS})"
        )
    missing_models = [surface for surface in selected if not models.get(surface, "").strip()]
    if missing_models:
        raise ValueError(f"explicit model identifiers required for: {missing_models}")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")

    overrides = dict(executable_overrides or {})
    credentials = dict(credential_sources or {})
    inherited = dict(os.environ if inherited_environment is None else inherited_environment)
    resolved_executables = {
        surface: resolve_executable(surface, overrides.get(surface))
        for surface in selected
    }

    output_directory = output_directory.resolve(strict=False)
    output_directory.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(6)
    evidence_root = output_directory / "evidence" / run_id
    evidence_root.mkdir(parents=True, exist_ok=False)
    if room_directory is None:
        if os.name == "nt":
            room_directory = Path(output_directory.anchor) / "arms-canary-rooms"
        else:
            room_directory = Path(tempfile.gettempdir()) / "arms-canary-rooms"
    room_root = room_directory.expanduser().resolve(strict=False) / run_id
    room_root.mkdir(parents=True, exist_ok=False)
    day = _local_day_record()
    certificate: dict[str, object] = {
        "schema_version": CERTIFICATE_SCHEMA,
        "run_id": run_id,
        "generated_at": utc_now(),
        "calibration_day": day,
        "probe_budget": {
            "maximum_model_calls": MAX_MODEL_CALLS,
            "planned_model_calls": planned_calls,
            "actual_model_calls": 0,
            "version_queries_not_model_calls": 0,
        },
        "selected_surfaces": selected,
        "certified_surfaces": [],
        "surface_results": [],
        "evidence_root": str(evidence_root),
        "room_root": str(room_root),
        "verdict": "FAIL",
    }
    actual_calls = 0
    version_queries = 0

    for surface in selected:
        surface_dir = evidence_root / surface
        planted_root = room_root / surface / "planted-room"
        clean_root = room_root / surface / "clean-room"
        planted_cwd = planted_root / "workspace"
        clean_cwd = clean_root / "workspace"
        planted_cwd.mkdir(parents=True, exist_ok=False)
        clean_cwd.mkdir(parents=True, exist_ok=False)
        planted_env = clean_environment(surface, planted_root, inherited=inherited)
        clean_env = clean_environment(surface, clean_root, inherited=inherited)
        credential_records: list[dict[str, object]] = []
        credential = credentials.get(surface)
        if credential is not None:
            # A named credential file is an explicit choice of authentication
            # route. Remove inherited token/key variables so CLI precedence
            # cannot silently select a different identity for either leg.
            for name in _AUTH_ENVIRONMENT[surface]:
                planted_env.pop(name, None)
                clean_env.pop(name, None)
            credential_records.extend(
                [
                    provision_credential(surface, credential, planted_env),
                    provision_credential(surface, credential, clean_env),
                ]
            )
            _credential_registry.extend(credential_records)
        nonce = secrets.token_hex(12).upper()
        plants = plant_markers(surface, planted_cwd, planted_env, nonce)
        markers = [str(item["marker"]) for item in plants]
        clean_ack = f"{CLEAN_ACK_PREFIX}{surface.upper()}_{secrets.token_hex(12).upper()}"
        prompt = probe_prompt(clean_ack)
        executable = resolved_executables[surface]
        model = models[surface]
        result: dict[str, object] = {
            "surface": surface,
            "subject": {
                "cli": surface,
                "executable": executable,
                "executable_kind": executable_kind(executable),
                "requested_model_identifier": model,
                "detected_version": None,
            },
            "credential_copies": credential_records,
            "plants": plants,
            "clean_ack_sha256": sha256_bytes(clean_ack.encode("utf-8")),
            "preflight_errors": [],
            "marker_fired": False,
            "marker_absent": False,
            "certified": False,
        }
        certificate["surface_results"].append(result)  # type: ignore[union-attr]

        planted_pre_path = surface_dir / "planted.environment-manifest.pre-version.json"
        clean_pre_path = surface_dir / "clean.environment-manifest.pre-version.json"
        _, planted_errors = _preflight_manifest(
            surface,
            planted_cwd,
            planted_env,
            executable=executable,
            model=model,
            expected_plants=plants,
            output_path=planted_pre_path,
        )
        _, clean_errors = _preflight_manifest(
            surface,
            clean_cwd,
            clean_env,
            executable=executable,
            model=model,
            expected_plants=[],
            output_path=clean_pre_path,
        )
        result["environment_manifests"] = {
            "planted_pre_version": _existing_file_record(planted_pre_path),
            "clean_pre_version": _existing_file_record(clean_pre_path),
        }
        errors = [*planted_errors, *clean_errors]
        if errors:
            result["preflight_errors"] = errors
            result["skipped_reason"] = "instruction-bare preflight failed before any CLI call"
            result["credential_cleanup"] = remove_credential_copies(credential_records)
            continue

        version_record, detected_version = _version_probe(
            surface,
            executable,
            clean_cwd,
            clean_env,
            timeout_seconds,
            runner,
            surface_dir,
        )
        version_queries += 1
        result["version_query"] = version_record
        result["subject"]["detected_version"] = detected_version  # type: ignore[index]
        if detected_version is None:
            result["skipped_reason"] = "CLI version query failed; model probes not launched"
            result["credential_cleanup"] = remove_credential_copies(credential_records)
            continue

        planted_manifest_path = surface_dir / "planted.environment-manifest.json"
        clean_manifest_path = surface_dir / "clean.environment-manifest.json"
        _, planted_errors = _preflight_manifest(
            surface,
            planted_cwd,
            planted_env,
            executable=executable,
            model=model,
            expected_plants=plants,
            output_path=planted_manifest_path,
            detected_version=detected_version,
        )
        _, clean_errors = _preflight_manifest(
            surface,
            clean_cwd,
            clean_env,
            executable=executable,
            model=model,
            expected_plants=[],
            output_path=clean_manifest_path,
            detected_version=detected_version,
        )
        errors = [*planted_errors, *clean_errors]
        result["environment_manifests"].update(  # type: ignore[union-attr]
            {
                "planted": _existing_file_record(planted_manifest_path),
                "clean": _existing_file_record(clean_manifest_path),
            }
        )
        result["preflight_errors"] = errors
        if errors:
            result["skipped_reason"] = "version query changed the room; model probes not launched"
            result["credential_cleanup"] = remove_credential_copies(credential_records)
            continue

        planted_plan = build_probe_plan(
            surface,
            executable=executable,
            model=model,
            prompt=prompt,
            cwd=planted_cwd,
        )
        actual_calls += 1
        if actual_calls > MAX_MODEL_CALLS:  # defensive invariant, unreachable by plan.
            raise RuntimeError("internal error: canary model-call ceiling exceeded")
        planted_record, planted_response, planted_process = _probe_leg(
            planted_plan,
            cwd=planted_cwd,
            env=planted_env,
            timeout_seconds=timeout_seconds,
            runner=runner,
            evidence_dir=surface_dir,
            leg="planted",
        )
        planted_evaluation = evaluate_planted_response(
            planted_response, markers, process_ok=_process_ok(planted_process)
        )
        planted_record["evaluation"] = planted_evaluation
        result["planted_probe"] = planted_record
        result["marker_fired"] = planted_evaluation["marker_fired"]

        clean_plan = build_probe_plan(
            surface,
            executable=executable,
            model=model,
            prompt=prompt,
            cwd=clean_cwd,
        )
        actual_calls += 1
        if actual_calls > MAX_MODEL_CALLS:
            raise RuntimeError("internal error: canary model-call ceiling exceeded")
        clean_record, clean_response, clean_process = _probe_leg(
            clean_plan,
            cwd=clean_cwd,
            env=clean_env,
            timeout_seconds=timeout_seconds,
            runner=runner,
            evidence_dir=surface_dir,
            leg="clean",
        )
        clean_evaluation = evaluate_clean_response(
            clean_response,
            markers,
            clean_ack,
            process_ok=_process_ok(clean_process),
        )
        clean_record["evaluation"] = clean_evaluation
        result["clean_probe"] = clean_record
        result["marker_absent"] = clean_evaluation["marker_absent"]
        credential_cleanup = remove_credential_copies(credential_records)
        result["credential_cleanup"] = credential_cleanup
        result["certified"] = bool(
            result["marker_fired"]
            and result["marker_absent"]
            and credential_cleanup["success"]
        )
        if result["certified"]:
            certificate["certified_surfaces"].append(surface)  # type: ignore[union-attr]

    certificate["probe_budget"] = {
        "maximum_model_calls": MAX_MODEL_CALLS,
        "planned_model_calls": planned_calls,
        "actual_model_calls": actual_calls,
        "version_queries_not_model_calls": version_queries,
    }
    certificate["completed_at"] = utc_now()
    certificate["verdict"] = (
        "PASS"
        if set(certificate["certified_surfaces"]) == set(selected)
        else "FAIL"
    )
    filename = f"CANARY-{day['date']}-{run_id}.json"
    certificate_path = _write_certificate(output_directory / filename, certificate)
    return certificate_path, certificate


def calibrate(
    surfaces: Sequence[str],
    *,
    models: Mapping[str, str],
    executable_overrides: Mapping[str, str] | None = None,
    credential_sources: Mapping[str, Path] | None = None,
    output_directory: Path,
    room_directory: Path | None = None,
    timeout_seconds: float = 180.0,
    inherited_environment: Mapping[str, str] | None = None,
    runner: ProcessRunner = run_process,
) -> tuple[Path, dict[str, object]]:
    """Run calibration and remove copied credentials on every unwind path."""

    credential_registry: list[dict[str, object]] = []
    try:
        return _calibrate_impl(
            surfaces,
            models=models,
            executable_overrides=executable_overrides,
            credential_sources=credential_sources,
            output_directory=output_directory,
            room_directory=room_directory,
            timeout_seconds=timeout_seconds,
            inherited_environment=inherited_environment,
            runner=runner,
            _credential_registry=credential_registry,
        )
    finally:
        pending = [
            record
            for record in credential_registry
            if Path(str(record["destination_path"])).exists()
        ]
        if pending:
            remove_credential_copies(pending)


def check_certificate(
    path: Path,
    *,
    required_surfaces: Iterable[str],
    now: datetime | None = None,
    require_sidecar: bool = True,
    _allow_failed_overall: bool = False,
) -> dict[str, object]:
    """Verify integrity, same-day freshness, and per-surface certification."""

    path = path.expanduser().resolve(strict=True)
    errors: list[str] = []
    try:
        certificate = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return {"pass": False, "errors": [f"invalid certificate: {exc}"], "path": str(path)}
    if not isinstance(certificate, dict):
        return {"pass": False, "errors": ["certificate root is not an object"], "path": str(path)}
    if certificate.get("schema_version") != CERTIFICATE_SCHEMA:
        errors.append("unsupported certificate schema")
    sidecar = path.with_suffix(path.suffix + ".sha256")
    if require_sidecar:
        if not sidecar.is_file():
            errors.append("certificate SHA-256 sidecar is missing")
        else:
            try:
                fields = sidecar.read_text(
                    encoding="utf-8", errors="replace"
                ).split()
            except OSError as exc:
                errors.append(f"certificate SHA-256 sidecar is unreadable: {exc}")
            else:
                if not fields:
                    errors.append("certificate SHA-256 sidecar is empty")
                elif fields[0] != sha256_file(path):
                    errors.append("certificate SHA-256 sidecar mismatch")

    day = certificate.get("calibration_day")
    if not isinstance(day, dict):
        errors.append("calibration_day is missing")
    else:
        try:
            basis = _parse_offset(str(day["utc_offset"]))
            current = now or datetime.now(timezone.utc)
            if current.tzinfo is None:
                current = current.replace(tzinfo=timezone.utc)
            today = current.astimezone(basis).date().isoformat()
            if day.get("date") != today:
                errors.append(
                    f"certificate is not same-day: certificate={day.get('date')} today={today}"
                )
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f"invalid calibration_day: {exc}")

    if certificate.get("verdict") != "PASS" and not _allow_failed_overall:
        errors.append(f"certificate verdict is {certificate.get('verdict')!r}, not PASS")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    completed_at = certificate.get("completed_at")
    if not isinstance(completed_at, str):
        errors.append("completed_at is missing")
    else:
        try:
            completed = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
            if completed.tzinfo is None:
                raise ValueError("timestamp has no UTC offset")
            if completed > current.astimezone(timezone.utc) + timedelta(minutes=5):
                errors.append("certificate completion timestamp is in the future")
        except ValueError as exc:
            errors.append(f"invalid completed_at: {exc}")
    required = set(required_surfaces)
    unknown = required - set(SUPPORTED_CLIS)
    if unknown:
        errors.append(f"unknown required surfaces: {sorted(unknown)}")
    certified = set(certificate.get("certified_surfaces", []))
    missing = required - certified
    if missing:
        errors.append(f"required surfaces are not certified: {sorted(missing)}")
    surface_values = certificate.get("surface_results", [])
    surface_results = {
        item.get("surface"): item
        for item in surface_values
        if isinstance(item, dict) and isinstance(item.get("surface"), str)
    } if isinstance(surface_values, list) else {}

    def verify_file_record(
        record: object, label: str, hash_key: str = "sha256"
    ) -> Path | None:
        if not isinstance(record, dict):
            errors.append(f"missing evidence record: {label}")
            return None
        try:
            evidence_path = Path(str(record["path"]))
            expected_hash = str(record[hash_key])
            expected_size = int(record["size_bytes"])
        except (KeyError, TypeError, ValueError):
            errors.append(f"invalid evidence record: {label}")
            return None
        if not evidence_path.is_file():
            errors.append(f"evidence file is missing: {label} ({evidence_path})")
            return None
        if evidence_path.stat().st_size != expected_size:
            errors.append(f"evidence size mismatch: {label}")
        if sha256_file(evidence_path) != expected_hash:
            errors.append(f"evidence SHA-256 mismatch: {label}")
        return evidence_path

    for surface in sorted(required):
        result = surface_results.get(surface)
        if not isinstance(result, dict):
            errors.append(f"surface result is missing: {surface}")
            continue
        for field in ("marker_fired", "marker_absent", "certified"):
            if result.get(field) is not True:
                errors.append(f"{surface} did not record {field}=true")
        if result.get("preflight_errors"):
            errors.append(f"{surface} has preflight errors")
        subject = result.get("subject")
        if not isinstance(subject, dict):
            errors.append(f"{surface} subject metadata is missing")
        else:
            if not subject.get("detected_version"):
                errors.append(f"{surface} detected CLI version is missing")
            if not subject.get("requested_model_identifier"):
                errors.append(f"{surface} requested model identifier is missing")
        credential_copies = result.get("credential_copies")
        if isinstance(credential_copies, list) and credential_copies:
            cleanup = result.get("credential_cleanup")
            if not isinstance(cleanup, dict) or cleanup.get("success") is not True:
                errors.append(f"{surface} copied credentials were not safely removed")
            for index, copy in enumerate(credential_copies):
                if not isinstance(copy, dict) or copy.get("exists_after_cleanup") is not False:
                    errors.append(
                        f"{surface} credential copy {index} lacks removal evidence"
                    )
        manifests = result.get("environment_manifests")
        if not isinstance(manifests, dict):
            errors.append(f"{surface} environment manifest records are missing")
        else:
            for name in (
                "planted_pre_version",
                "clean_pre_version",
                "planted",
                "clean",
            ):
                verify_file_record(manifests.get(name), f"{surface}.{name}")
        plants = result.get("plants")
        markers: list[str] = []
        if not isinstance(plants, list) or len(plants) != 2:
            errors.append(f"{surface} must have exactly two planted file records")
        else:
            for index, plant in enumerate(plants):
                plant_path = verify_file_record(
                    plant, f"{surface}.plant[{index}]", hash_key="file_sha256"
                )
                marker = plant.get("marker") if isinstance(plant, dict) else None
                marker_hash = (
                    plant.get("marker_sha256") if isinstance(plant, dict) else None
                )
                if not isinstance(marker, str) or not marker.startswith(MARKER_PREFIX):
                    errors.append(f"{surface} plant {index} has an invalid marker")
                elif marker_hash != sha256_bytes(marker.encode("utf-8")):
                    errors.append(f"{surface} plant {index} marker hash mismatch")
                else:
                    markers.append(marker)
                    if plant_path is not None:
                        planted_text = plant_path.read_text(
                            encoding="utf-8", errors="replace"
                        )
                        if marker not in planted_text:
                            errors.append(
                                f"{surface} plant {index} file does not contain marker"
                            )
        response_text: dict[str, str] = {}
        process_success: dict[str, bool] = {}
        for leg in ("planted_probe", "clean_probe"):
            probe = result.get(leg)
            if not isinstance(probe, dict):
                errors.append(f"{surface} {leg} record is missing")
                continue
            verify_file_record(probe.get("stdout"), f"{surface}.{leg}.stdout")
            verify_file_record(probe.get("stderr"), f"{surface}.{leg}.stderr")
            response_path = verify_file_record(
                probe.get("response"), f"{surface}.{leg}.response"
            )
            if response_path is not None:
                response_text[leg] = response_path.read_text(
                    encoding="utf-8", errors="replace"
                )
            process_success[leg] = (
                probe.get("exit_code") == 0
                and probe.get("timed_out") is False
                and probe.get("launch_error") is None
            )
        planted_response = response_text.get("planted_probe")
        if planted_response is not None and (
            not process_success.get("planted_probe")
            or not planted_response.strip()
            or len(markers) != 2
            or any(marker not in planted_response for marker in markers)
        ):
            errors.append(f"{surface} planted marker claim does not recompute")
        clean_response = response_text.get("clean_probe")
        if clean_response is not None:
            clean_ack_hash = result.get("clean_ack_sha256")
            ack_candidates = re.findall(
                rf"{re.escape(CLEAN_ACK_PREFIX)}[A-Z0-9_]+", clean_response
            )
            ack_matches = any(
                sha256_bytes(candidate.encode("utf-8")) == clean_ack_hash
                for candidate in ack_candidates
            )
            if (
                not process_success.get("clean_probe")
                or not clean_response.strip()
                or any(marker in clean_response for marker in markers)
                or not ack_matches
            ):
                errors.append(f"{surface} clean marker-absence claim does not recompute")
        version = result.get("version_query")
        if not isinstance(version, dict) or version.get("success") is not True:
            errors.append(f"{surface} successful version query is missing")
        else:
            verify_file_record(version.get("stdout"), f"{surface}.version.stdout")
            verify_file_record(version.get("stderr"), f"{surface}.version.stderr")
    budget = certificate.get("probe_budget")
    if not isinstance(budget, dict):
        errors.append("probe_budget is missing")
    else:
        try:
            if int(budget["actual_model_calls"]) > MAX_MODEL_CALLS:
                errors.append("certificate exceeds the eight-model-call ceiling")
            selected_count = len(certificate.get("selected_surfaces", []))
            if certificate.get("verdict") == "PASS" and int(
                budget["actual_model_calls"]
            ) != selected_count * 2:
                errors.append("PASS certificate does not contain two calls per selected surface")
        except (KeyError, TypeError, ValueError):
            errors.append("invalid probe_budget")
    return {
        "pass": not errors,
        "errors": errors,
        "path": str(path),
        "required_surfaces": sorted(required),
        "certified_surfaces": sorted(certified),
        "calibration_day": day,
        "selected_surfaces": certificate.get("selected_surfaces", []),
        "actual_model_calls": (
            certificate.get("probe_budget", {}).get("actual_model_calls")
            if isinstance(certificate.get("probe_budget"), dict)
            else None
        ),
    }


def check_certificate_set(
    surface_certificates: Mapping[str, Path],
    *,
    now: datetime | None = None,
) -> dict[str, object]:
    """Gate on per-surface evidence across immutable same-day certificates.

    A failed multi-surface run may still contain a fully valid surface. This
    checker permits that surface's evidence while retaining the failed source
    certificate unchanged, and counts every source run's calls once toward the
    job-wide ceiling.
    """

    errors: list[str] = []
    if not surface_certificates:
        return {"pass": False, "errors": ["no surface certificates supplied"]}
    unknown = sorted(set(surface_certificates) - set(CALIBRATION_CLIS))
    if unknown:
        errors.append(f"unsupported aggregate surfaces: {unknown}")
    checked_at = now or datetime.now(timezone.utc)
    source_results: dict[str, dict[str, object]] = {}
    unique_sources: dict[str, dict[str, object]] = {}
    calibration_days: set[tuple[str, str]] = set()
    for surface, source_path in sorted(surface_certificates.items()):
        if surface not in CALIBRATION_CLIS:
            continue
        result = check_certificate(
            source_path,
            required_surfaces=[surface],
            now=checked_at,
            require_sidecar=True,
            _allow_failed_overall=True,
        )
        source_results[surface] = result
        if not result["pass"]:
            errors.extend(
                f"{surface} source: {message}" for message in result["errors"]
            )
        selected = result.get("selected_surfaces", [])
        if not isinstance(selected, list) or surface not in selected:
            errors.append(f"{surface} source did not select that surface")
        day = result.get("calibration_day")
        if isinstance(day, dict):
            calibration_days.add(
                (str(day.get("date", "")), str(day.get("utc_offset", "")))
            )
        source_key = str(Path(str(result.get("path", source_path))).resolve())
        unique_sources[source_key] = result
    if len(calibration_days) > 1:
        errors.append("surface certificates have different calibration days or offsets")
    actual_calls = 0
    for source, result in unique_sources.items():
        try:
            calls = int(result["actual_model_calls"])
            if calls < 0:
                raise ValueError
            actual_calls += calls
        except (KeyError, TypeError, ValueError):
            errors.append(f"source certificate has invalid call count: {source}")
    if actual_calls > MAX_MODEL_CALLS:
        errors.append(
            f"aggregate model-probe calls ({actual_calls}) exceed ceiling "
            f"({MAX_MODEL_CALLS})"
        )
    return {
        "pass": not errors,
        "errors": errors,
        "required_surfaces": sorted(surface_certificates),
        "aggregate_model_calls": actual_calls,
        "maximum_model_calls": MAX_MODEL_CALLS,
        "calibration_days": [
            {"date": date, "utc_offset": offset}
            for date, offset in sorted(calibration_days)
        ],
        "surface_results": source_results,
    }
