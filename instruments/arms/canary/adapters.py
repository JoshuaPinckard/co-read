"""Command construction and response extraction for canary-only probes.

Nothing in this module executes a command.  Keeping construction separate from
execution makes the eight-call ceiling auditable and lets the shim test plans
without touching a real model.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


@dataclass(frozen=True)
class ProbePlan:
    cli: str
    executable: str
    model: str
    command: tuple[str, ...]
    stdin: str | None
    response_file: Path | None


VERSION_ARGUMENTS: Mapping[str, tuple[str, ...]] = {
    "codex": ("--version",),
    "claude": ("--version",),
    "gemini": ("--version",),
}


def resolve_executable(cli: str, override: str | None = None) -> str:
    """Resolve a CLI before HOME/PATH are replaced by the clean environment."""

    requested = override or cli
    candidate = Path(requested).expanduser()
    if candidate.is_absolute() or candidate.parent != Path("."):
        if not candidate.is_file():
            raise FileNotFoundError(f"{cli} executable does not exist: {candidate}")
        return str(candidate.resolve())
    resolved = shutil.which(requested)
    if not resolved:
        raise FileNotFoundError(f"{cli} executable not found on PATH: {requested}")
    return str(Path(resolved).resolve())


def version_command(cli: str, executable: str) -> tuple[str, ...]:
    try:
        arguments = VERSION_ARGUMENTS[cli]
    except KeyError as exc:
        raise ValueError(f"unsupported CLI: {cli}") from exc
    return (executable, *arguments)


def build_probe_plan(
    cli: str,
    *,
    executable: str,
    model: str,
    prompt: str,
    cwd: Path,
) -> ProbePlan:
    """Construct one non-interactive, no-tools calibration call."""

    if not model.strip():
        raise ValueError(f"an explicit model identifier is required for {cli}")
    cwd = cwd.resolve(strict=False)
    if cli == "codex":
        response_file = cwd / ".canary-last-message.txt"
        command = (
            executable,
            "exec",
            "--skip-git-repo-check",
            "--ephemeral",
            "--sandbox",
            "read-only",
            "--json",
            "--output-last-message",
            str(response_file),
            "--model",
            model,
            "-",
        )
        return ProbePlan(cli, executable, model, command, prompt, response_file)
    if cli == "claude":
        command = (
            executable,
            "-p",
            "--permission-mode",
            "acceptEdits",
            "--strict-mcp-config",
            "--tools",
            "",
            "--disable-slash-commands",
            "--output-format",
            "json",
            "--no-session-persistence",
            "--max-turns",
            "1",
            "--model",
            model,
        )
        return ProbePlan(cli, executable, model, command, prompt, None)
    if cli == "gemini":
        command = (
            executable,
            "-p",
            prompt,
            "--output-format",
            "json",
            "--approval-mode",
            "plan",
            "--skip-trust",
            "--model",
            model,
        )
        return ProbePlan(cli, executable, model, command, None, None)
    raise ValueError(f"unsupported CLI: {cli}")


def command_for_record(command: Sequence[str], prompt: str | None = None) -> list[str]:
    """Return a stable command record without serializing prompt text."""

    result = [str(part) for part in command]
    if prompt is not None:
        result.append("<PROMPT_ON_STDIN>")
    return result


def extract_response(
    cli: str,
    *,
    stdout: str,
    response_file: Path | None = None,
) -> str:
    """Extract only model-visible output; stderr is intentionally excluded."""

    if cli == "codex":
        if response_file is not None and response_file.is_file():
            return response_file.read_text(encoding="utf-8", errors="replace")
        # A missing last-message file is not clean evidence. Returning an empty
        # response makes evaluation fail even if diagnostic stdout was present.
        return ""
    if cli == "claude":
        try:
            parsed = json.loads(stdout)
        except json.JSONDecodeError:
            return ""
        if not isinstance(parsed, dict):
            return ""
        value = parsed.get("result")
        return value if isinstance(value, str) else ""
    if cli == "gemini":
        try:
            parsed = json.loads(stdout)
        except json.JSONDecodeError:
            # Some releases have emitted a banner before the JSON object. Do a
            # bounded fallback without treating arbitrary logs as model output.
            start, end = stdout.find("{"), stdout.rfind("}")
            if start < 0 or end <= start:
                return ""
            try:
                parsed = json.loads(stdout[start : end + 1])
            except json.JSONDecodeError:
                return ""
        if not isinstance(parsed, dict):
            return ""
        for key in ("response", "result"):
            value = parsed.get(key)
            if isinstance(value, str):
                return value
        return ""
    raise ValueError(f"unsupported CLI: {cli}")


def executable_kind(path: str) -> str:
    """Describe launch form for diagnostics, especially Windows wrappers."""

    suffix = Path(path).suffix.casefold()
    if os.name == "nt" and suffix in {".cmd", ".bat", ".ps1"}:
        return f"windows-{suffix[1:]}-wrapper"
    return "native-or-script"
