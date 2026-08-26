from __future__ import annotations

import dataclasses
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from .util import ProcessResult, ShimError, process_environment, run_process


@dataclasses.dataclass(frozen=True)
class SubjectCommand:
    argv: tuple[str, ...]
    env: Mapping[str, str]
    stdin: bytes | None = None


class SubjectAdapter:
    cli = "unknown"

    def __init__(self, *, version: str, model: str) -> None:
        self.version = version
        self.model = model

    @property
    def identity(self) -> dict[str, str]:
        return {"cli": self.cli, "version": self.version, "model": self.model}

    def command(self, *, prompt: str, cwd: Path) -> SubjectCommand:
        raise NotImplementedError

    def declaration_command(self, *, prompt: str, cwd: Path) -> SubjectCommand:
        return self.command(prompt=prompt, cwd=cwd)


class CodexAdapter(SubjectAdapter):
    cli = "codex"

    def command(self, *, prompt: str, cwd: Path) -> SubjectCommand:
        return SubjectCommand(
            (
                "codex",
                "exec",
                "--ephemeral",
                "--sandbox",
                "workspace-write",
                "--ignore-user-config",
                "--ignore-rules",
                "--model",
                self.model,
                "-",
            ),
            process_environment(),
            prompt.encode("utf-8"),
        )

    def declaration_command(self, *, prompt: str, cwd: Path) -> SubjectCommand:
        return SubjectCommand(
            (
                "codex",
                "exec",
                "--ephemeral",
                "--sandbox",
                "read-only",
                "--ignore-user-config",
                "--ignore-rules",
                "--model",
                self.model,
                "-",
            ),
            process_environment(),
            prompt.encode("utf-8"),
        )


class ClaudeAdapter(SubjectAdapter):
    cli = "claude"

    def command(self, *, prompt: str, cwd: Path) -> SubjectCommand:
        return SubjectCommand(
            (
                "claude",
                "-p",
                "--permission-mode",
                "acceptEdits",
                "--strict-mcp-config",
                "--disable-slash-commands",
                "--no-session-persistence",
                "--model",
                self.model,
            ),
            process_environment(),
            prompt.encode("utf-8"),
        )

    def declaration_command(self, *, prompt: str, cwd: Path) -> SubjectCommand:
        return SubjectCommand(
            (
                "claude",
                "-p",
                "--permission-mode",
                "plan",
                "--strict-mcp-config",
                "--tools",
                "",
                "--disable-slash-commands",
                "--no-session-persistence",
                "--model",
                self.model,
            ),
            process_environment(),
            prompt.encode("utf-8"),
        )


class GeminiAdapter(SubjectAdapter):
    cli = "gemini"

    def command(self, *, prompt: str, cwd: Path) -> SubjectCommand:
        return SubjectCommand(
            (
                "gemini",
                "--approval-mode",
                "auto_edit",
                "--model",
                self.model,
                "--prompt",
                prompt,
            ),
            process_environment(),
        )

    def declaration_command(self, *, prompt: str, cwd: Path) -> SubjectCommand:
        return SubjectCommand(
            (
                "gemini",
                "--approval-mode",
                "plan",
                "--model",
                self.model,
                "--prompt",
                prompt,
            ),
            process_environment(),
        )


class ScriptedAdapter(SubjectAdapter):
    cli = "scripted-fake"

    def __init__(self, script: Path, *, model: str = "deterministic") -> None:
        self.script = script.resolve()
        super().__init__(version="shim-fake-v1", model=model)

    def command_for_spec(self, spec_path: Path, *, declare: bool = False) -> SubjectCommand:
        argv = [sys.executable, str(self.script), "--spec", str(spec_path.resolve())]
        if declare:
            argv.append("--declare")
        return SubjectCommand(
            tuple(argv),
            process_environment(
                {"PYTHONDONTWRITEBYTECODE": "1", "PYTHONHASHSEED": "0"}
            ),
        )

    def command(self, *, prompt: str, cwd: Path) -> SubjectCommand:
        raise ShimError("scripted adapter requires command_for_spec")


def detect_cli_version(command: str) -> dict[str, Any]:
    executable = shutil.which(command)
    if executable is None:
        return {"cli": command, "present": False, "version": None, "executable": None}
    result = run_process(
        [command, "--version"], cwd=Path.cwd(), timeout_seconds=30.0, check=False
    )
    output = (result.stdout + result.stderr).decode("utf-8", errors="replace").strip()
    return {
        "cli": command,
        "present": result.returncode == 0,
        "version": output,
        "executable": str(Path(executable).resolve()),
        "executable_sha256": __import__("hashlib").sha256(Path(executable).read_bytes()).hexdigest(),
    }


def task_prompt(side: Any) -> str:
    intent = side.intent_subject
    if side.intent_body:
        intent += "\n\n" + side.intent_body
    targets = "\n".join(f"- {target}" for target in side.focal_targets)
    return (
        f"{intent}\n\n"
        "The task's test patch is already applied and failing. Edit SOURCE files only "
        "until these frozen focal tests pass. Do not modify test files.\n"
        f"{targets}\n"
    )


def declaration_prompt(side: Any) -> str:
    intent = side.intent_subject + ("\n\n" + side.intent_body if side.intent_body else "")
    return (
        f"{intent}\n\n"
        "Return only a JSON array of repository-relative SOURCE file paths you expect "
        "to modify for this task. Do not edit files."
    )


def parse_declaration(payload: bytes) -> tuple[tuple[str, ...], str | None]:
    text = payload.decode("utf-8", errors="replace").strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        return (), f"invalid JSON declaration: {error}"
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return (), "declaration must be a JSON array of strings"
    try:
        from .util import safe_relative

        paths = tuple(sorted(set(safe_relative(item) for item in value)))
    except ShimError as error:
        return (), str(error)
    return paths, None
