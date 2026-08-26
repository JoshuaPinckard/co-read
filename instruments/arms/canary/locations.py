"""Instruction-source discovery and environment manifests for the ARMS canary.

This module is deliberately standard-library only.  It does not launch a subject
CLI.  The discovery lists both existing and absent candidates so a certificate
records which contamination channels were checked, not merely which files
happened to exist.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence

try:  # tomllib entered the standard library in Python 3.11.
    import tomllib
except ImportError:  # pragma: no cover - exercised only on unsupported Python.
    tomllib = None  # type: ignore[assignment]


SCHEMA_VERSION = "arms-environment-manifest/v1"
SUPPORTED_CLIS = ("codex", "claude", "gemini")


def utc_now() -> str:
    """Return a UTC RFC 3339 timestamp with an explicit ``Z`` suffix."""

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolved(path: Path) -> Path:
    """Resolve without requiring the candidate itself to exist."""

    return path.expanduser().resolve(strict=False)


def file_record(
    path: Path,
    *,
    scope: str,
    role: str,
    candidate: str,
    read_semantics: str,
) -> dict[str, object]:
    """Describe a candidate without including its content."""

    # Preserve the candidate's own absolute path so symlink status and distinct
    # discovery locations are not erased by ``Path.resolve()``. File hashing and
    # ``stat`` intentionally follow the target, matching what the CLI can read.
    resolved = Path(os.path.abspath(str(path.expanduser())))
    record: dict[str, object] = {
        "path": str(resolved),
        "scope": scope,
        "role": role,
        "candidate": candidate,
        "read_semantics": read_semantics,
        "exists": False,
        "is_file": False,
        "is_directory": False,
        "is_symlink": False,
        "size_bytes": None,
        "mtime_ns": None,
        "sha256": None,
    }
    try:
        record["exists"] = resolved.exists() or resolved.is_symlink()
        record["is_symlink"] = resolved.is_symlink()
        if resolved.is_symlink():
            try:
                record["symlink_target"] = os.readlink(resolved)
                record["resolved_target"] = str(resolved.resolve(strict=False))
            except OSError as exc:
                record["symlink_error"] = f"{type(exc).__name__}: {exc}"
        if resolved.is_file():
            stat = resolved.stat()
            record.update(
                {
                    "is_file": True,
                    "size_bytes": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                    "sha256": sha256_file(resolved),
                }
            )
        elif record["exists"]:
            record["is_directory"] = resolved.is_dir()
            if role != "instruction_search_root" or not record["is_directory"]:
                record["inspection_error"] = "exists but is not a regular file"
    except OSError as exc:
        record["inspection_error"] = f"{type(exc).__name__}: {exc}"
    return record


def _home_from_env(env: Mapping[str, str]) -> Path:
    for name in ("USERPROFILE", "HOME"):
        value = env.get(name)
        if value:
            return _resolved(Path(value))
    return _resolved(Path.home())


def _ancestry(path: Path) -> list[Path]:
    """Return filesystem root through ``path``, without duplicate entries."""

    current = _resolved(path)
    chain = [current]
    while current.parent != current:
        current = current.parent
        chain.append(current)
    chain.reverse()
    return chain


def _repo_root(cwd: Path) -> Path | None:
    for directory in reversed(_ancestry(cwd)):
        if (directory / ".git").exists():
            return directory
    return None


def _project_chain(cwd: Path, *, repo_bounded: bool) -> list[Path]:
    cwd = _resolved(cwd)
    ancestry = _ancestry(cwd)
    if repo_bounded:
        root = _repo_root(cwd)
        if root is None:
            return [cwd]
        return ancestry[ancestry.index(root) :]
    return ancestry


def _nonempty_file(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _codex_fallback_names(codex_home: Path, warnings: list[str]) -> list[str]:
    config = codex_home / "config.toml"
    if not config.is_file():
        return []
    if tomllib is None:
        warnings.append(
            "config.toml exists but Python <3.11 cannot parse "
            "project_doc_fallback_filenames"
        )
        return []
    try:
        parsed = tomllib.loads(config.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        warnings.append(f"could not parse {config}: {type(exc).__name__}: {exc}")
        return []
    value = parsed.get("project_doc_fallback_filenames", [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        warnings.append(
            f"ignored non-string project_doc_fallback_filenames in {config}"
        )
        return []
    return [item for item in value if item and Path(item).name == item]


def _codex_system_records(
    home: Path, codex_home: Path, env: Mapping[str, str]
) -> list[dict[str, object]]:
    """Record local machine/administrator Codex configuration layers.

    These files are configuration rather than ordinary ``AGENTS.md`` files,
    but they can enable prompt-bearing hooks, rules, MCP servers, or custom
    instruction files.  A clean-room preflight must therefore fail closed when
    any of them exists.
    """

    if os.name == "nt":
        program_data = Path(env.get("PROGRAMDATA", r"C:\ProgramData"))
        system_candidates = [
            (
                program_data / "OpenAI" / "Codex" / "requirements.toml",
                "admin-enforced requirements",
                "Windows system requirements layer",
            )
        ]
    else:
        system_candidates = [
            (
                Path("/etc/codex/config.toml"),
                "system configuration",
                "Unix system configuration layer",
            ),
            (
                Path("/etc/codex/requirements.toml"),
                "admin-enforced requirements",
                "Unix system requirements layer",
            ),
            (
                Path("/etc/codex/managed_config.toml"),
                "managed defaults",
                "Unix managed defaults layer",
            ),
        ]

    # CODEX_HOME is the relocatable ~/.codex root.  Also list the literal
    # default-home spelling when it differs so the manifest catches a client
    # release that treats the documented Windows/non-Unix managed path as
    # home-relative even while CODEX_HOME is redirected.
    managed_candidates = [codex_home / "managed_config.toml"]
    home_managed = home / ".codex" / "managed_config.toml"
    if _resolved(home_managed) != _resolved(managed_candidates[0]):
        managed_candidates.append(home_managed)

    records = [
        file_record(
            path,
            scope="managed",
            role="instruction_configuration",
            candidate=candidate,
            read_semantics=semantics,
        )
        for path, candidate, semantics in system_candidates
    ]
    records.extend(
        file_record(
            path,
            scope="managed",
            role="instruction_configuration",
            candidate="managed defaults",
            read_semantics=(
                "managed_config.toml may enable prompt-bearing hooks, rules, "
                "MCP servers, or instruction files"
            ),
        )
        for path in managed_candidates
    )
    return records


def _codex_locations(
    cwd: Path, env: Mapping[str, str], warnings: list[str]
) -> list[dict[str, object]]:
    home = _home_from_env(env)
    codex_home = _resolved(Path(env.get("CODEX_HOME", home / ".codex")))
    records: list[dict[str, object]] = []

    global_candidates = [codex_home / "AGENTS.override.md", codex_home / "AGENTS.md"]
    selected_global = next(
        (candidate for candidate in global_candidates if _nonempty_file(candidate)), None
    )
    for index, path in enumerate(global_candidates):
        record = file_record(
            path,
            scope="global",
            role="instruction",
            candidate="override" if index == 0 else "standard",
            read_semantics="first non-empty candidate in CODEX_HOME",
        )
        record["selected_by_discovery"] = selected_global == path
        records.append(record)

    records.append(
        file_record(
            codex_home / "config.toml",
            scope="global",
            role="instruction_configuration",
            candidate="codex configuration",
            read_semantics=(
                "may set project_doc_fallback_filenames and other prompt-affecting options"
            ),
        )
    )
    records.extend(_codex_system_records(home, codex_home, env))
    records.extend(
        _instruction_tree_records(
            codex_home / "skills",
            "global",
            "Codex user skills",
            "SKILL.md descriptions are model-visible and bodies may be activated",
            "SKILL.md",
        )
    )
    records.extend(
        _instruction_tree_records(
            home / ".agents" / "skills",
            "global",
            "Codex user skills",
            "user skill metadata is model-visible and bodies may be activated",
            "SKILL.md",
        )
    )
    if os.name != "nt":
        records.extend(
            _instruction_tree_records(
                Path("/etc/codex/skills"),
                "managed",
                "Codex admin skills",
                "admin skill metadata is model-visible and bodies may be activated",
                "SKILL.md",
            )
        )
    records.extend(
        _instruction_tree_records(
            codex_home / "plugins",
            "global",
            "Codex plugins",
            "plugin-contributed skills or agent instructions may be model-visible",
            "*.md",
        )
    )
    fallbacks = _codex_fallback_names(codex_home, warnings)
    for directory in _project_chain(cwd, repo_bounded=True):
        records.append(
            file_record(
                directory / ".codex" / "config.toml",
                scope="project",
                role="instruction_configuration",
                candidate="project Codex configuration",
                read_semantics=(
                    "Codex loads every trusted .codex/config.toml from the "
                    "project root through cwd"
                ),
            )
        )
        candidates = [
            ("override", directory / "AGENTS.override.md"),
            ("standard", directory / "AGENTS.md"),
            *(("configured fallback", directory / name) for name in fallbacks),
        ]
        selected = next((path for _, path in candidates if _nonempty_file(path)), None)
        for candidate, path in candidates:
            record = file_record(
                path,
                scope="project",
                role="instruction",
                candidate=candidate,
                read_semantics="at most one non-empty candidate per project directory",
            )
            record["selected_by_discovery"] = selected == path
            records.append(record)
        for relative in (Path(".agents") / "skills", Path(".codex") / "skills"):
            records.extend(
                _instruction_tree_records(
                    directory / relative,
                    "project",
                    "workspace skills",
                    "workspace skill descriptions are model-visible",
                    "SKILL.md",
                )
            )
    return records


def _managed_claude_directory(env: Mapping[str, str]) -> Path:
    if os.name == "nt":
        program_files = Path(env.get("ProgramFiles", r"C:\Program Files"))
        return program_files / "ClaudeCode"
    if sys.platform == "darwin":
        return Path("/Library/Application Support/ClaudeCode")
    return Path("/etc/claude-code")


def _managed_claude_path(env: Mapping[str, str]) -> Path:
    return _managed_claude_directory(env) / "CLAUDE.md"


def _instruction_tree_records(
    root: Path,
    scope: str,
    candidate: str,
    read_semantics: str,
    pattern: str,
) -> list[dict[str, object]]:
    root_record = file_record(
        root,
        scope=scope,
        role="instruction_search_root",
        candidate=candidate,
        read_semantics=read_semantics,
    )
    records = [root_record]
    paths: list[Path] = []
    if root.is_dir():
        try:
            paths = sorted(path for path in root.rglob(pattern) if path.is_file())
        except OSError as exc:
            root_record["inspection_error"] = f"{type(exc).__name__}: {exc}"
    root_record["candidate_file_count"] = len(paths)
    for path in paths[:1000]:
        records.append(
            file_record(
                path,
                scope=scope,
                role="instruction",
                candidate=candidate,
                read_semantics=read_semantics,
            )
        )
    if len(paths) > 1000:
        root_record["inspection_error"] = (
            f"instruction enumeration truncated: {len(paths)} files found, 1000 recorded"
        )
    return records


def _rule_records(root: Path, scope: str) -> list[dict[str, object]]:
    return _instruction_tree_records(
        root,
        scope,
        "recursive rules directory",
        "rule content may load at launch or by path match",
        "*.md",
    )


def _claude_registry_records() -> list[dict[str, object]]:
    if os.name != "nt":
        return []
    try:
        import winreg
    except ImportError:  # pragma: no cover - defensive on nonstandard Python.
        return []
    records: list[dict[str, object]] = []
    for hive_name, hive in (("HKLM", winreg.HKEY_LOCAL_MACHINE), ("HKCU", winreg.HKEY_CURRENT_USER)):
        location = rf"registry://{hive_name}/SOFTWARE/Policies/ClaudeCode/Settings"
        record: dict[str, object] = {
            "path": location,
            "scope": "managed",
            "role": "instruction_configuration",
            "candidate": "Windows managed settings registry value",
            "read_semantics": "may contain claudeMd or appendSystemPrompt",
            "exists": False,
            "is_file": False,
            "is_directory": False,
            "is_symlink": False,
            "size_bytes": None,
            "mtime_ns": None,
            "sha256": None,
        }
        try:
            with winreg.OpenKey(hive, r"SOFTWARE\Policies\ClaudeCode") as key:
                value, _ = winreg.QueryValueEx(key, "Settings")
            encoded = str(value).encode("utf-8")
            record.update(
                {
                    "exists": True,
                    "size_bytes": len(encoded),
                    "sha256": sha256_bytes(encoded),
                }
            )
        except FileNotFoundError:
            pass
        except OSError as exc:
            record["inspection_error"] = f"{type(exc).__name__}: {exc}"
        records.append(record)
    return records


def _claude_locations(
    cwd: Path, env: Mapping[str, str], warnings: list[str]
) -> list[dict[str, object]]:
    del warnings
    home = _home_from_env(env)
    config_dir = _resolved(Path(env.get("CLAUDE_CONFIG_DIR", home / ".claude")))
    managed_dir = _managed_claude_directory(env)
    records = [
        file_record(
            _managed_claude_path(env),
            scope="managed",
            role="instruction",
            candidate="managed policy",
            read_semantics="organization instruction loaded for every session",
        ),
        file_record(
            managed_dir / "managed-settings.json",
            scope="managed",
            role="instruction_configuration",
            candidate="managed settings",
            read_semantics="may contain claudeMd or appendSystemPrompt",
        ),
        file_record(
            managed_dir / "managed-mcp.json",
            scope="managed",
            role="instruction_configuration",
            candidate="managed MCP settings",
            read_semantics="may expose prompt-bearing tools or resources",
        ),
        file_record(
            config_dir / "CLAUDE.md",
            scope="global",
            role="instruction",
            candidate="user instructions",
            read_semantics="CLAUDE_CONFIG_DIR relocates the ~/.claude tree",
        ),
        file_record(
            config_dir / "settings.json",
            scope="global",
            role="instruction_configuration",
            candidate="user settings",
            read_semantics="may enable hooks, plugins, auto-memory, or managed instructions",
        ),
        file_record(
            home / ".claude.json",
            scope="global",
            role="instruction_configuration",
            candidate="Claude global state and user MCP configuration",
            read_semantics="may expose prompt-bearing MCP tools or per-project settings",
        ),
    ]
    records.extend(
        _instruction_tree_records(
            managed_dir / "managed-settings.d",
            "managed",
            "managed settings drop-ins",
            "JSON drop-ins may contain claudeMd or appendSystemPrompt",
            "*.json",
        )
    )
    records.extend(_claude_registry_records())
    records.extend(_rule_records(config_dir / "rules", "global"))
    for child in ("skills", "commands", "agents", "output-styles", "plugins"):
        records.extend(
            _instruction_tree_records(
                config_dir / child,
                "global",
                f"Claude user {child}",
                "descriptions or bodies may be injected or activated",
                "*.md",
            )
        )
    memory_root = config_dir / "projects"
    memory_record = file_record(
        memory_root,
        scope="global",
        role="instruction_search_root",
        candidate="auto-memory projects directory",
        read_semantics="project-keyed MEMORY.md can be injected at session start",
    )
    memory_paths: list[Path] = []
    if memory_root.is_dir():
        try:
            memory_paths = sorted(memory_root.glob("*/memory/MEMORY.md"))
        except OSError as exc:
            memory_record["inspection_error"] = f"{type(exc).__name__}: {exc}"
    memory_record["candidate_file_count"] = len(memory_paths)
    records.append(memory_record)
    for path in memory_paths[:1000]:
        records.append(
            file_record(
                path,
                scope="global",
                role="instruction",
                candidate="auto-memory entrypoint",
                read_semantics="matching project MEMORY.md may be injected at session start",
            )
        )
    for directory in _project_chain(cwd, repo_bounded=False):
        for candidate, relative in (
            ("project", Path("CLAUDE.md")),
            ("local project", Path("CLAUDE.local.md")),
        ):
            records.append(
                file_record(
                    directory / relative,
                    scope="project",
                    role="instruction",
                    candidate=candidate,
                    read_semantics="loaded along the cwd ancestry",
                )
            )
    workspace_root = _repo_root(cwd) or _resolved(cwd)
    records.append(
        file_record(
            workspace_root / ".claude" / "CLAUDE.md",
            scope="project",
            role="instruction",
            candidate="project .claude",
            read_semantics="alternate project-root instruction location",
        )
    )
    records.extend(_rule_records(workspace_root / ".claude" / "rules", "project"))
    for child in ("skills", "commands", "agents", "output-styles"):
        records.extend(
            _instruction_tree_records(
                workspace_root / ".claude" / child,
                "project",
                f"Claude project {child}",
                "descriptions or bodies may be injected or activated",
                "*.md",
            )
        )
    for directory in _project_chain(cwd, repo_bounded=False):
        for settings_name in ("settings.json", "settings.local.json"):
            records.append(
                file_record(
                    directory / ".claude" / settings_name,
                    scope="project",
                    role="instruction_configuration",
                    candidate="project settings",
                    read_semantics="may enable hooks, plugins, or prompt-affecting behavior",
                )
            )
    return records


def _gemini_system_paths(env: Mapping[str, str]) -> tuple[Path, Path]:
    if os.name == "nt":
        program_data = Path(env.get("PROGRAMDATA", r"C:\ProgramData"))
        defaults = program_data / "gemini-cli" / "system-defaults.json"
        settings = program_data / "gemini-cli" / "settings.json"
    elif sys.platform == "darwin":
        base = Path("/Library/Application Support/GeminiCli")
        defaults, settings = base / "system-defaults.json", base / "settings.json"
    else:
        base = Path("/etc/gemini-cli")
        defaults, settings = base / "system-defaults.json", base / "settings.json"
    return (
        Path(env.get("GEMINI_CLI_SYSTEM_DEFAULTS_PATH", defaults)),
        Path(env.get("GEMINI_CLI_SYSTEM_SETTINGS_PATH", settings)),
    )


def _json_object(path: Path, warnings: list[str]) -> dict[str, object]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        warnings.append(f"could not parse {path}: {type(exc).__name__}: {exc}")
        return {}
    if not isinstance(value, dict):
        warnings.append(f"ignored non-object settings file {path}")
        return {}
    return value


def _gemini_context_names(
    settings_paths: Sequence[Path], warnings: list[str]
) -> list[str]:
    value: object = None
    for path in settings_paths:
        parsed = _json_object(path, warnings)
        context = parsed.get("context")
        if isinstance(context, dict) and "fileName" in context:
            value = context["fileName"]
    if value is None:
        return ["GEMINI.md"]
    names = [value] if isinstance(value, str) else value
    if not isinstance(names, list) or not all(isinstance(item, str) for item in names):
        warnings.append("ignored invalid Gemini context.fileName setting")
        return ["GEMINI.md"]
    safe_names = [item for item in names if item and Path(item).name == item]
    if len(safe_names) != len(names):
        warnings.append("ignored Gemini context filenames containing path components")
    return safe_names or ["GEMINI.md"]


def _gemini_locations(
    cwd: Path, env: Mapping[str, str], warnings: list[str]
) -> list[dict[str, object]]:
    home = _home_from_env(env)
    cli_home = _resolved(Path(env.get("GEMINI_CLI_HOME", home)))
    gemini_dir = cli_home / ".gemini"
    system_defaults, system_settings = _gemini_system_paths(env)
    repo_root = _repo_root(cwd) or _resolved(cwd)
    user_settings = gemini_dir / "settings.json"
    project_settings = repo_root / ".gemini" / "settings.json"
    settings_paths = [system_defaults, user_settings, project_settings, system_settings]
    context_names = _gemini_context_names(settings_paths, warnings)

    records: list[dict[str, object]] = []
    for candidate, path in (
        ("system defaults", system_defaults),
        ("user settings", user_settings),
        ("project settings", project_settings),
        ("system overrides", system_settings),
    ):
        records.append(
            file_record(
                path,
                scope="configuration",
                role="instruction_configuration",
                candidate=candidate,
                read_semantics="may customize context filenames, extensions, policies, or tools",
            )
        )

    for name in context_names:
        records.append(
            file_record(
                gemini_dir / name,
                scope="global",
                role="instruction",
                candidate="global context",
                read_semantics="loaded from GEMINI_CLI_HOME/.gemini",
            )
        )
    for directory in _project_chain(cwd, repo_bounded=False):
        for name in context_names:
            records.append(
                file_record(
                    directory / name,
                    scope="project",
                    role="instruction",
                    candidate="workspace context",
                    read_semantics="hierarchical context discovery",
                )
            )
        for env_name in (".env", str(Path(".gemini") / ".env")):
            records.append(
                file_record(
                    directory / env_name,
                    scope="project",
                    role="instruction_configuration",
                    candidate="environment loader",
                    read_semantics="may set GEMINI_SYSTEM_MD or prompt-affecting variables",
                )
            )
    records.append(
        file_record(
            gemini_dir / ".env",
            scope="global",
            role="instruction_configuration",
            candidate="user environment loader",
            read_semantics="may set GEMINI_SYSTEM_MD or context settings",
        )
    )
    extensions_root = gemini_dir / "extensions"
    extensions_record = file_record(
        extensions_root,
        scope="global",
        role="instruction_search_root",
        candidate="extensions directory",
        read_semantics="installed extensions may inject context files",
    )
    extension_contexts: list[Path] = []
    if extensions_root.is_dir():
        try:
            extension_contexts = sorted(extensions_root.glob("*/GEMINI.md"))
        except OSError as exc:
            extensions_record["inspection_error"] = f"{type(exc).__name__}: {exc}"
    extensions_record["candidate_file_count"] = len(extension_contexts)
    records.append(extensions_record)
    for path in extension_contexts[:1000]:
        records.append(
            file_record(
                path,
                scope="global",
                role="instruction",
                candidate="extension context",
                read_semantics="extension manifest may inject its context file",
            )
        )
    for relative, scope in (
        (Path(".gemini") / "skills", "global"),
        (Path(".agents") / "skills", "global alias"),
    ):
        records.extend(
            _instruction_tree_records(
                cli_home / relative,
                scope,
                "Gemini user skills",
                "skill metadata is model-visible and bodies may be activated",
                "SKILL.md",
            )
        )
    workspace_root = _repo_root(cwd) or _resolved(cwd)
    for relative in (Path(".gemini") / "skills", Path(".agents") / "skills"):
        records.extend(
            _instruction_tree_records(
                workspace_root / relative,
                "project",
                "Gemini workspace skills",
                "skill metadata is model-visible and bodies may be activated",
                "SKILL.md",
            )
        )
    system_prompt = env.get("GEMINI_SYSTEM_MD")
    if system_prompt and system_prompt.lower() not in {"0", "false"}:
        target = (
            repo_root / ".gemini" / "system.md"
            if system_prompt.lower() in {"1", "true"}
            else Path(system_prompt).expanduser()
        )
        if not target.is_absolute():
            target = cwd / target
        records.append(
            file_record(
                target,
                scope="system prompt",
                role="instruction",
                candidate="GEMINI_SYSTEM_MD override",
                read_semantics="replaces the built-in system prompt",
            )
        )
    return records


def instruction_locations(
    cli: str, cwd: Path, env: Mapping[str, str] | None = None
) -> tuple[list[dict[str, object]], list[str]]:
    """Return instruction/configuration candidates and discovery warnings."""

    if cli not in SUPPORTED_CLIS:
        raise ValueError(f"unsupported CLI {cli!r}; choose from {SUPPORTED_CLIS}")
    supplied_env = dict(os.environ if env is None else env)
    cwd = _resolved(cwd)
    if not cwd.is_dir():
        raise ValueError(f"cwd is not a directory: {cwd}")
    warnings: list[str] = []
    builders = {
        "codex": _codex_locations,
        "claude": _claude_locations,
        "gemini": _gemini_locations,
    }
    records = builders[cli](cwd, supplied_env, warnings)
    # Several discovery routes intentionally converge on the same path. Keep
    # the first semantic description to make normalized manifests stable.
    deduplicated: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for record in records:
        key = (str(record["path"]).casefold(), str(record["role"]))
        if key not in seen:
            seen.add(key)
            deduplicated.append(record)
    return deduplicated, warnings


def environment_manifest(
    cli: str,
    cwd: Path,
    *,
    env: Mapping[str, str] | None = None,
    executable: str | None = None,
    requested_model: str | None = None,
    detected_version: str | None = None,
    draw_id: str | None = None,
    generated_at: str | None = None,
) -> dict[str, object]:
    """Build a serializable, secret-free environment manifest."""

    supplied_env = dict(os.environ if env is None else env)
    locations, warnings = instruction_locations(cli, cwd, supplied_env)
    redirect_names = (
        "HOME",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "CODEX_HOME",
        "CLAUDE_CONFIG_DIR",
        "GEMINI_CLI_HOME",
        "GEMINI_CLI_SYSTEM_DEFAULTS_PATH",
        "GEMINI_CLI_SYSTEM_SETTINGS_PATH",
    )
    auth_names = {
        "codex": ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN"),
        "claude": (
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "CLAUDE_CODE_OAUTH_TOKEN",
        ),
        "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS"),
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at or utc_now(),
        "draw_id": draw_id,
        "cli": cli,
        "cwd": str(_resolved(cwd)),
        "executable": executable,
        "requested_model": requested_model,
        "detected_version": detected_version,
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        "redirected_environment": {
            name: supplied_env[name] for name in redirect_names if name in supplied_env
        },
        # Values are intentionally never serialized. Presence is enough to
        # reproduce which authentication route could have been selected.
        "authentication_environment_present": [
            name for name in auth_names[cli] if supplied_env.get(name)
        ],
        "instruction_locations": locations,
        "warnings": warnings,
    }


def existing_instruction_sources(
    manifest: Mapping[str, object],
    *,
    allowed_paths: Iterable[Path] = (),
) -> list[dict[str, object]]:
    """Return existing prompt-bearing sources outside an explicit allowlist."""

    allowed = {str(_resolved(path)).casefold() for path in allowed_paths}
    result: list[dict[str, object]] = []
    locations = manifest.get("instruction_locations", [])
    if not isinstance(locations, list):
        return result
    for item in locations:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        if role not in {"instruction", "instruction_configuration", "instruction_search_root"}:
            continue
        if not item.get("exists"):
            continue
        if role == "instruction_search_root" and not item.get("candidate_file_count"):
            continue
        if str(item.get("path", "")).casefold() in allowed:
            continue
        result.append(item)
    return result


def write_json_atomic(path: Path, value: object) -> None:
    """Write JSON without exposing a partially-written manifest."""

    path = _resolved(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
