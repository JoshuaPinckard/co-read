"""Production subject launch primitive used by the six-arm scheduler.

The scripted gate never imports this module to launch a subject.  It exists so
the gated scheduler has one auditable path for real CLI identity, clean-room
preflight, 20-minute launch-to-finish timeout, 30-second mechanical polling,
and final filesystem attribution.  Agent prose is retained but never parsed.
"""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Mapping

from instruments.arms.canary.instrument import (
    check_certificate_set,
    clean_environment,
    provision_credential,
    remove_credential_copies,
)
from instruments.arms.canary.locations import (
    environment_manifest,
    existing_instruction_sources,
    write_json_atomic,
)

from .adapters import SubjectAdapter, SubjectCommand, task_prompt
from .harness import PRODUCTION_POLL_SECONDS, PRODUCTION_TIMEOUT_SECONDS
from .schema import Side
from .util import (
    EventLog,
    ProcessResult,
    Snapshot,
    ShimError,
    diff_snapshots,
    finish_process,
    sha256_bytes,
    sha256_file,
    snapshot_tree,
    start_process,
)


@dataclasses.dataclass(frozen=True)
class ProductionSubjectConfig:
    adapter: SubjectAdapter
    canary_certificates: Mapping[str, Path]
    clean_room_root: Path
    credential_file: Path | None = None


@dataclasses.dataclass(frozen=True)
class ProductionSubjectResult:
    process: ProcessResult
    write_records: tuple[Mapping[str, Any], ...]
    completion_snapshot: Snapshot
    poll_count: int
    identity: Mapping[str, Any]
    environment_manifest_path: Path
    credential_cleanup_path: Path
    stdout_path: Path
    stderr_path: Path


@dataclasses.dataclass(frozen=True)
class _PreparedSubject:
    env: dict[str, str]
    identity: Mapping[str, Any]
    environment_manifest_path: Path
    credential_records: tuple[dict[str, object], ...]


_REQUIRED_CANARY_SURFACES = frozenset({"codex", "claude"})


def _credential_destination(cli: str, env: Mapping[str, str]) -> Path:
    if cli == "codex":
        return Path(env["CODEX_HOME"]) / "auth.json"
    if cli == "claude":
        return Path(env["CLAUDE_CONFIG_DIR"]) / ".credentials.json"
    raise ShimError(f"credential destination is not approved for {cli!r}")


def _version_command(cli: str) -> tuple[str, ...]:
    executable = shutil.which(cli)
    if not executable:
        raise ShimError(f"subject CLI is absent: {cli}")
    command = [executable, "--version"]
    if os.name == "nt" and Path(executable).suffix.casefold() in {".cmd", ".bat"}:
        return (
            os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe"),
            "/d",
            "/s",
            "/c",
            subprocess.list2cmdline(command),
        )
    return tuple(command)


def detect_version(cli: str, *, cwd: Path, env: Mapping[str, str]) -> str:
    try:
        result = subprocess.run(
            _version_command(cli),
            cwd=cwd,
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
            shell=False,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ShimError(f"could not detect {cli} version: {error}") from error
    value = (result.stdout + result.stderr).decode("utf-8", errors="replace").strip()
    if result.returncode != 0 or not value:
        raise ShimError(f"{cli} --version failed with exit {result.returncode}: {value}")
    return " ".join(value.splitlines())[:500]


def certified_subject_binding(certificate_path: Path, surface: str) -> dict[str, str]:
    """Read the exact CLI version/model bound by a validated certificate."""
    try:
        certificate = json.loads(certificate_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ShimError(f"could not read {surface} canary binding: {error}") from error
    values = certificate.get("surface_results")
    matches = [
        row
        for row in values
        if isinstance(row, dict) and row.get("surface") == surface
    ] if isinstance(values, list) else []
    if len(matches) != 1 or matches[0].get("certified") is not True:
        raise ShimError(
            f"{surface} canary lacks one certified surface result for subject binding"
        )
    subject = matches[0].get("subject")
    if not isinstance(subject, dict):
        raise ShimError(f"{surface} canary subject binding is absent")
    version = subject.get("detected_version")
    model = subject.get("requested_model_identifier")
    if not isinstance(version, str) or not version or not isinstance(model, str) or not model:
        raise ShimError(f"{surface} canary subject binding is incomplete")
    return {"detected_version": version, "requested_model_identifier": model}


class ProductionSubjectLauncher:
    """Fail-closed launcher; the arm scheduler owns retries and integration."""

    def __init__(self, config: ProductionSubjectConfig) -> None:
        self.config = config

    @staticmethod
    def _cleanup_credentials(
        records: tuple[dict[str, object], ...],
        *,
        artifact_root: Path,
        phase: str,
    ) -> Path:
        """Remove copied credentials and persist hash-only cleanup evidence.

        This helper is called from both the preflight and launch unwind paths.
        It never consults subject output and it fails closed if a copied live
        credential remains on disk.
        """

        cleanup = remove_credential_copies(records)
        cleanup_path = artifact_root / "credential-cleanup.json"
        write_json_atomic(
            cleanup_path,
            {
                "phase": phase,
                "credential_copies": list(records),
                "credential_cleanup": cleanup,
            },
        )
        if cleanup.get("success") is not True:
            raise ShimError(
                "production credential cleanup failed: "
                + "; ".join(map(str, cleanup.get("errors", [])))
            )
        return cleanup_path

    def _preflight(
        self,
        *,
        draw_id: str,
        instance_id: str,
        tree: Path,
        artifact_root: Path,
    ) -> _PreparedSubject:
        cli = self.config.adapter.cli
        configured_surfaces = set(self.config.canary_certificates)
        if configured_surfaces != _REQUIRED_CANARY_SURFACES:
            raise ShimError(
                "production requires one same-day certificate source for each of "
                f"{sorted(_REQUIRED_CANARY_SURFACES)}; got "
                f"{sorted(configured_surfaces)}"
            )
        if cli not in _REQUIRED_CANARY_SURFACES:
            raise ShimError(
                f"production subject surface {cli!r} has no approved canary calibration"
            )
        certificate_check = check_certificate_set(self.config.canary_certificates)
        if not certificate_check["pass"]:
            raise ShimError(
                "same-day aggregate codex+claude canary failed: "
                + "; ".join(certificate_check["errors"])
            )
        leg_root = (
            self.config.clean_room_root.resolve()
            / draw_id
            / instance_id
        )
        if leg_root.exists():
            raise ShimError(f"refusing reused production clean room: {leg_root}")
        leg_root.mkdir(parents=True, exist_ok=False)
        env = clean_environment(cli, leg_root, inherited=os.environ)
        env.update(
            {
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_NO_LAZY_FETCH": "1",
                "LANG": "C",
                "LC_ALL": "C",
                "TZ": "UTC",
            }
        )
        credential_records: list[dict[str, object]] = []
        try:
            if self.config.credential_file is not None:
                destination = _credential_destination(cli, env)
                try:
                    credential_records.append(
                        provision_credential(cli, self.config.credential_file, env)
                    )
                except BaseException:
                    # ``provision_credential`` normally returns the record
                    # immediately after copying. If an I/O error occurs after
                    # the copy but before return, register the known target so
                    # the outer unwind still removes it.
                    if destination.exists() and not credential_records:
                        try:
                            size_bytes: int | None = destination.stat().st_size
                            digest: str | None = sha256_file(destination)
                        except OSError:
                            size_bytes = None
                            digest = None
                        credential_records.append(
                            {
                                "source_path": str(
                                    self.config.credential_file.expanduser().resolve(
                                        strict=False
                                    )
                                ),
                                "destination_path": str(destination.resolve()),
                                "size_bytes": size_bytes,
                                "sha256": digest,
                                "provisioning_completed": False,
                            }
                        )
                    raise
            version = detect_version(cli, cwd=tree, env=env)
            certified_binding = certified_subject_binding(
                self.config.canary_certificates[cli], cli
            )
            binding_errors: list[str] = []
            if version != certified_binding["detected_version"]:
                binding_errors.append(
                    "current CLI version does not match same-day calibration: "
                    f"current={version!r} certified={certified_binding['detected_version']!r}"
                )
            if self.config.adapter.model != certified_binding["requested_model_identifier"]:
                binding_errors.append(
                    "requested model does not match same-day calibration: "
                    f"current={self.config.adapter.model!r} "
                    f"certified={certified_binding['requested_model_identifier']!r}"
                )
            manifest = environment_manifest(
                cli,
                tree,
                env=env,
                executable=shutil.which(cli),
                requested_model=self.config.adapter.model,
                detected_version=version,
                draw_id=draw_id,
            )
            unexpected = existing_instruction_sources(manifest)
            errors = list(map(str, manifest.get("warnings", [])))
            errors.extend(binding_errors)
            errors.extend(
                f"unexpected instruction source: {row.get('path')}" for row in unexpected
            )
            errors.extend(
                f"instruction inspection error: {row.get('path')}: "
                f"{row.get('inspection_error')}"
                for row in manifest.get("instruction_locations", [])
                if isinstance(row, dict) and row.get("inspection_error")
            )
            manifest["production_preflight"] = {
                "instruction_bare": not errors,
                "errors": errors,
                "credential_copies": credential_records,
                "credential_cleanup_evidence": str(
                    (artifact_root / "credential-cleanup.json").resolve()
                ),
                "canary_check": certificate_check,
                "canary_subject_binding": {
                    "certified": certified_binding,
                    "current": {
                        "detected_version": version,
                        "requested_model_identifier": self.config.adapter.model,
                    },
                    "matches": not binding_errors,
                    "errors": binding_errors,
                },
            }
            manifest_path = artifact_root / "environment-manifest.json"
            write_json_atomic(manifest_path, manifest)
            if errors:
                raise ShimError(
                    "instruction-bare production preflight failed: "
                    + "; ".join(errors)
                )
            identity = {
                "cli": cli,
                "version": version,
                "model": self.config.adapter.model,
            }
            return _PreparedSubject(
                env=env,
                identity=identity,
                environment_manifest_path=manifest_path,
                credential_records=tuple(credential_records),
            )
        except BaseException:
            self._cleanup_credentials(
                tuple(credential_records),
                artifact_root=artifact_root,
                phase="preflight-unwind",
            )
            raise

    def run(
        self,
        *,
        draw_id: str,
        instance_id: str,
        side: Side,
        tree: Path,
        artifact_root: Path,
        log: EventLog,
        poll_writes: bool = False,
    ) -> ProductionSubjectResult:
        artifact_root.mkdir(parents=True, exist_ok=False)
        prepared: _PreparedSubject | None = None
        running = None
        cleanup_phase = "launch-exception"
        try:
            prepared = self._preflight(
                draw_id=draw_id,
                instance_id=instance_id,
                tree=tree,
                artifact_root=artifact_root,
            )
            prompt = task_prompt(side)
            command = self.config.adapter.command(prompt=prompt, cwd=tree)
            command = SubjectCommand(command.argv, prepared.env, command.stdin)
            baseline = snapshot_tree(tree)
            log.emit(
                "launch",
                principal={"side": side.label, "instance_id": instance_id},
                subject=prepared.identity,
                detail={
                    "timeout_seconds": PRODUCTION_TIMEOUT_SECONDS,
                    "poll_seconds": PRODUCTION_POLL_SECONDS if poll_writes else None,
                    "poll_writes": poll_writes,
                    "task_prompt_sha256": sha256_bytes(prompt.encode("utf-8")),
                    "environment_manifest_sha256": __import__("hashlib").sha256(
                        prepared.environment_manifest_path.read_bytes()
                    ).hexdigest(),
                },
            )
            running = start_process(
                command.argv,
                cwd=tree,
                env=command.env,
                stdin=command.stdin,
            )
            poll_count = 0
            if poll_writes and running.process is not None:
                launch_deadline = (
                    running.started_monotonic + PRODUCTION_TIMEOUT_SECONDS
                )
                next_poll = running.started_monotonic + PRODUCTION_POLL_SECONDS
                while running.process.poll() is None:
                    now = time.monotonic()
                    if now >= launch_deadline:
                        break
                    wake_at = min(next_poll, launch_deadline)
                    while running.process.poll() is None and time.monotonic() < wake_at:
                        remaining = wake_at - time.monotonic()
                        if remaining > 0:
                            time.sleep(min(0.25, remaining))
                    if running.process.poll() is not None:
                        break
                    now = time.monotonic()
                    # Poll at each elapsed 30-second boundary during the run,
                    # but never add a nominal poll at the timeout boundary.
                    if now >= launch_deadline:
                        break
                    if now >= next_poll:
                        current = snapshot_tree(tree)
                        log.emit(
                            "poll",
                            principal={
                                "side": side.label,
                                "instance_id": instance_id,
                            },
                            subject=prepared.identity,
                            paths=diff_snapshots(baseline, current),
                            detail={
                                "poll_index": poll_count,
                                "poll_seconds": PRODUCTION_POLL_SECONDS,
                            },
                        )
                        poll_count += 1
                        # Snapshot work can cross a later boundary. Advance to
                        # the first future boundary rather than burst-polling.
                        poll_finished = time.monotonic()
                        while next_poll <= poll_finished:
                            next_poll += PRODUCTION_POLL_SECONDS
            process = finish_process(running, PRODUCTION_TIMEOUT_SECONDS)
            if process.timed_out:
                cleanup_phase = "launch-timeout"
            elif process.launch_error is not None or process.returncode != 0:
                cleanup_phase = "launch-failure"
            else:
                cleanup_phase = "launch-complete"
            after = snapshot_tree(tree)
            writes = tuple(diff_snapshots(baseline, after))
            stdout_path = artifact_root / "stdout.txt"
            stderr_path = artifact_root / "stderr.txt"
            stdout_path.write_bytes(process.stdout)
            stderr_path.write_bytes(process.stderr)
            log.emit(
                "write-set",
                principal={"side": side.label, "instance_id": instance_id},
                subject=prepared.identity,
                paths=writes,
                detail={
                    "basis": "filesystem-snapshot-diff",
                    "agent_text_consulted": False,
                },
            )
            return ProductionSubjectResult(
                process=process,
                write_records=writes,
                completion_snapshot=after,
                poll_count=poll_count,
                identity=prepared.identity,
                environment_manifest_path=prepared.environment_manifest_path,
                credential_cleanup_path=artifact_root / "credential-cleanup.json",
                stdout_path=stdout_path,
                stderr_path=stderr_path,
            )
        except BaseException:
            if (
                running is not None
                and running.process is not None
                and running.process.poll() is None
            ):
                aborted = finish_process(running, 0.0)
                (artifact_root / "stdout.txt").write_bytes(aborted.stdout)
                (artifact_root / "stderr.txt").write_bytes(aborted.stderr)
            raise
        finally:
            if prepared is not None:
                self._cleanup_credentials(
                    prepared.credential_records,
                    artifact_root=artifact_root,
                    phase=cleanup_phase,
                )
