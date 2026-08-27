from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


CANARY_ROOT = Path(__file__).resolve().parents[1]
if str(CANARY_ROOT) not in sys.path:
    sys.path.insert(0, str(CANARY_ROOT))

from adapters import build_probe_plan, extract_response, version_command  # noqa: E402
from instrument import (  # noqa: E402
    CERTIFICATE_SCHEMA,
    MARKER_PREFIX,
    ProcessResult,
    _windows_launch,
    _write_certificate,
    calibrate,
    check_certificate,
    check_certificate_set,
    clean_environment,
    evaluate_clean_response,
    evaluate_planted_response,
    plant_markers,
    probe_prompt,
)
from locations import (  # noqa: E402
    environment_manifest,
    existing_instruction_sources,
    sha256_bytes,
    sha256_file,
)


class LocationManifestTests(unittest.TestCase):
    def test_codex_manifest_hashes_global_and_repo_chain(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            codex_home = home / ".codex"
            repo = root / "repo"
            cwd = repo / "nested"
            codex_home.mkdir(parents=True)
            (repo / ".git").mkdir(parents=True)
            cwd.mkdir(parents=True)
            project_config = repo / ".codex" / "config.toml"
            project_config.parent.mkdir()
            user_skill = home / ".agents" / "skills" / "audit" / "SKILL.md"
            user_skill.parent.mkdir(parents=True)
            global_file = codex_home / "AGENTS.md"
            root_file = repo / "AGENTS.md"
            nested_file = cwd / "AGENTS.override.md"
            global_file.write_text("global\n", encoding="utf-8")
            root_file.write_text("root\n", encoding="utf-8")
            nested_file.write_text("nested\n", encoding="utf-8")
            project_config.write_text("model = 'test'\n", encoding="utf-8")
            user_skill.write_text("---\nname: audit\n---\n", encoding="utf-8")

            manifest = environment_manifest(
                "codex",
                cwd,
                env={
                    "HOME": str(home),
                    "USERPROFILE": str(home),
                    "CODEX_HOME": str(codex_home),
                    "PROGRAMDATA": str(root / "ProgramData"),
                    "OPENAI_API_KEY": "must-not-be-serialized",
                },
            )
            records = {
                str(item["path"]).casefold(): item
                for item in manifest["instruction_locations"]
            }
            for path in (global_file, root_file, nested_file):
                record = records[str(path.resolve()).casefold()]
                self.assertTrue(record["exists"])
                self.assertEqual(record["sha256"], sha256_file(path))
                self.assertTrue(record["selected_by_discovery"])
            project_record = records[str(project_config.resolve()).casefold()]
            self.assertTrue(project_record["exists"])
            self.assertEqual(project_record["role"], "instruction_configuration")
            user_skill_record = records[str(user_skill.resolve()).casefold()]
            self.assertTrue(user_skill_record["exists"])
            self.assertEqual(user_skill_record["role"], "instruction")
            payload = json.dumps(manifest)
            self.assertNotIn("must-not-be-serialized", payload)
            self.assertEqual(
                manifest["authentication_environment_present"], ["OPENAI_API_KEY"]
            )

    def test_claude_and_gemini_relocation_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cwd = root / "workspace"
            cwd.mkdir()
            claude_config = root / "claude-config"
            gemini_home = root / "gemini-home"
            claude = environment_manifest(
                "claude",
                cwd,
                env={
                    "HOME": str(root / "home"),
                    "USERPROFILE": str(root / "home"),
                    "CLAUDE_CONFIG_DIR": str(claude_config),
                },
            )
            gemini = environment_manifest(
                "gemini",
                cwd,
                env={
                    "HOME": str(root / "home"),
                    "USERPROFILE": str(root / "home"),
                    "GEMINI_CLI_HOME": str(gemini_home),
                    "GEMINI_CLI_SYSTEM_DEFAULTS_PATH": str(root / "defaults.json"),
                    "GEMINI_CLI_SYSTEM_SETTINGS_PATH": str(root / "system.json"),
                },
            )
            claude_paths = {item["path"] for item in claude["instruction_locations"]}
            gemini_paths = {item["path"] for item in gemini["instruction_locations"]}
            self.assertIn(str((claude_config / "CLAUDE.md").resolve()), claude_paths)
            self.assertIn(str((cwd / "CLAUDE.md").resolve()), claude_paths)
            self.assertIn(
                str((gemini_home / ".gemini" / "GEMINI.md").resolve()),
                gemini_paths,
            )
            self.assertIn(str((cwd / "GEMINI.md").resolve()), gemini_paths)

    def test_codex_override_is_contamination_when_only_standard_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cwd = root / "workspace"
            config = root / "config"
            cwd.mkdir()
            config.mkdir()
            standard = config / "AGENTS.md"
            override = config / "AGENTS.override.md"
            standard.write_text("allowed", encoding="utf-8")
            override.write_text("unexpected", encoding="utf-8")
            manifest = environment_manifest(
                "codex",
                cwd,
                env={
                    "HOME": str(root / "home"),
                    "USERPROFILE": str(root / "home"),
                    "CODEX_HOME": str(config),
                },
            )
            unexpected = existing_instruction_sources(
                manifest, allowed_paths=[standard]
            )
            self.assertIn(str(override.resolve()), {item["path"] for item in unexpected})


class CleanRoomTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "Windows batch quoting regression")
    def test_windows_batch_wrapper_preserves_argument_boundaries(self) -> None:
        import subprocess

        with tempfile.TemporaryDirectory(prefix="arms-batch-quote-") as temporary:
            script = Path(temporary) / "fake cli.cmd"
            script.write_text(
                "@echo off\r\n"
                "echo ONE=[%~1]\r\n"
                "echo TWO=[%~2]\r\n"
                "echo THREE=[%~3]\r\n"
                "echo FOUR=[%~4]\r\n",
                encoding="ascii",
            )
            command = [
                str(script),
                "FIRST VALUE",
                "SECOND VALUE",
                "",
                "TAIL VALUE",
            ]
            launched = _windows_launch(command, dict(__import__("os").environ))
            self.assertEqual(launched, command)
            completed = subprocess.run(
                launched,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                timeout=5,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            output = completed.stdout.decode("utf-8", errors="replace")
            self.assertIn("ONE=[FIRST VALUE]", output)
            self.assertIn("TWO=[SECOND VALUE]", output)
            self.assertIn("THREE=[]", output)
            self.assertIn("FOUR=[TAIL VALUE]", output)

    def test_clean_environment_redirects_homes_and_filters_other_provider_auth(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            inherited = {
                "PATH": "a-path",
                "OPENAI_API_KEY": "openai-secret",
                "CODEX_ACCESS_TOKEN": "codex-oauth-secret",
                "OPENAI_BASE_URL": "https://untrusted.invalid",
                "ANTHROPIC_API_KEY": "anthropic-secret",
                "CLAUDE_CODE_OAUTH_TOKEN": "claude-oauth-secret",
                "ANTHROPIC_BASE_URL": "https://untrusted.invalid",
                "CODEX_HOME": "dirty-codex-home",
                "CLAUDE_CONFIG_DIR": "dirty-claude-home",
                "GEMINI_CLI_HOME": "dirty-gemini-home",
            }
            clean = clean_environment(
                "codex", Path(temporary) / "leg", inherited=inherited
            )
            self.assertEqual(clean["OPENAI_API_KEY"], "openai-secret")
            self.assertEqual(clean["CODEX_ACCESS_TOKEN"], "codex-oauth-secret")
            self.assertNotIn("ANTHROPIC_API_KEY", clean)
            self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", clean)
            self.assertNotIn("OPENAI_BASE_URL", clean)
            self.assertNotIn("ANTHROPIC_BASE_URL", clean)
            self.assertNotEqual(clean["CODEX_HOME"], "dirty-codex-home")
            self.assertNotEqual(clean["CLAUDE_CONFIG_DIR"], "dirty-claude-home")
            self.assertNotEqual(clean["GEMINI_CLI_HOME"], "dirty-gemini-home")
            self.assertEqual(clean["GEMINI_SYSTEM_MD"], "false")
            self.assertTrue(Path(clean["CODEX_HOME"]).is_dir())

            claude_clean = clean_environment(
                "claude", Path(temporary) / "claude-leg", inherited=inherited
            )
            self.assertEqual(
                claude_clean["CLAUDE_CODE_OAUTH_TOKEN"], "claude-oauth-secret"
            )
            self.assertEqual(
                claude_clean["ANTHROPIC_API_KEY"], "anthropic-secret"
            )
            self.assertNotIn("OPENAI_API_KEY", claude_clean)
            self.assertNotIn("CODEX_ACCESS_TOKEN", claude_clean)
            self.assertNotIn("OPENAI_BASE_URL", claude_clean)
            self.assertNotIn("ANTHROPIC_BASE_URL", claude_clean)

    def test_unique_markers_are_planted_in_home_and_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cwd = root / "workspace"
            cwd.mkdir()
            env = clean_environment("codex", root / "leg", inherited={})
            plants = plant_markers("codex", cwd, env, "NONCE")
            self.assertEqual(len(plants), 2)
            self.assertEqual(len({item["marker"] for item in plants}), 2)
            for item in plants:
                path = Path(item["path"])
                self.assertTrue(path.is_file())
                self.assertEqual(sha256_file(path), item["file_sha256"])


class AdapterAndEvaluationTests(unittest.TestCase):
    def test_probe_prompt_uses_benign_precedence_not_hidden_context_exfiltration(self) -> None:
        prompt = probe_prompt("ARMSCANARYCLEAN_TEST_ACK")
        self.assertIn("ARMSCANARYCLEAN_TEST_ACK", prompt)
        self.assertIn("applicable calibration instruction", prompt)
        self.assertNotIn("instruction context contains", prompt)
        self.assertNotIn("hidden instruction", prompt)

    def test_probe_commands_pin_models_and_version_hooks(self) -> None:
        cwd = Path("C:/isolated/workspace")
        codex = build_probe_plan(
            "codex",
            executable="codex.cmd",
            model="gpt-explicit",
            prompt="probe",
            cwd=cwd,
        )
        claude = build_probe_plan(
            "claude",
            executable="claude.exe",
            model="claude-explicit",
            prompt="probe",
            cwd=cwd,
        )
        gemini = build_probe_plan(
            "gemini",
            executable="gemini.cmd",
            model="gemini-explicit",
            prompt="probe",
            cwd=cwd,
        )
        self.assertEqual(codex.command[codex.command.index("--model") + 1], "gpt-explicit")
        self.assertEqual(
            claude.command[claude.command.index("--model") + 1], "claude-explicit"
        )
        self.assertEqual(
            gemini.command[gemini.command.index("--model") + 1], "gemini-explicit"
        )
        for flag in (
            "--skip-git-repo-check",
            "--ephemeral",
            "--sandbox",
            "--json",
            "--output-last-message",
            "--model",
        ):
            self.assertIn(flag, codex.command)
        self.assertEqual(codex.command[codex.command.index("--sandbox") + 1], "read-only")
        for flag in (
            "-p",
            "--permission-mode",
            "--strict-mcp-config",
            "--tools",
            "--disable-slash-commands",
            "--output-format",
            "--no-session-persistence",
            "--max-turns",
            "--model",
        ):
            self.assertIn(flag, claude.command)
        self.assertEqual(
            claude.command[claude.command.index("--permission-mode") + 1],
            "acceptEdits",
        )
        self.assertEqual(claude.command[claude.command.index("--tools") + 1], "")
        self.assertEqual(claude.command[claude.command.index("--max-turns") + 1], "1")
        self.assertEqual(version_command("codex", "codex.cmd"), ("codex.cmd", "--version"))

    def test_response_extractors_never_treat_diagnostics_as_model_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            response_file = Path(temporary) / "last.txt"
            self.assertEqual(
                extract_response("codex", stdout="diagnostic marker", response_file=response_file),
                "",
            )
            response_file.write_text("model response", encoding="utf-8")
            self.assertEqual(
                extract_response("codex", stdout="diagnostic", response_file=response_file),
                "model response",
            )
        self.assertEqual(
            extract_response("claude", stdout='{"result":"claude response"}'),
            "claude response",
        )
        self.assertEqual(
            extract_response("gemini", stdout='{"response":"gemini response"}'),
            "gemini response",
        )

    def test_two_sided_evaluation_requires_every_marker_and_clean_ack(self) -> None:
        markers = ["ARMSCANARY_CODEX_GLOBAL_X", "ARMSCANARY_CODEX_WORKSPACE_X"]
        fired = evaluate_planted_response("\n".join(markers), markers)
        partial = evaluate_planted_response(markers[0], markers)
        clean = evaluate_clean_response("ACK", markers, "ACK")
        leaked = evaluate_clean_response(f"ACK {markers[0]}", markers, "ACK")
        self.assertTrue(fired["marker_fired"])
        self.assertFalse(partial["marker_fired"])
        self.assertTrue(clean["marker_absent"])
        self.assertFalse(leaked["marker_absent"])


class CalibrationAndCertificateTests(unittest.TestCase):
    @staticmethod
    def _fake_codex_runner(
        command: list[str] | tuple[str, ...],
        cwd: Path,
        env: dict[str, str],
        stdin: str | None,
        timeout: float,
    ) -> ProcessResult:
        del timeout
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        if "--version" in command:
            return ProcessResult(now, now, 0.001, 0, False, None, "codex-cli fake-1\n", "")
        markers: list[str] = []
        for path in (Path(env["CODEX_HOME"]) / "AGENTS.md", cwd / "AGENTS.md"):
            if path.is_file():
                markers.extend(re.findall(r"ARMSCANARY_[A-Z0-9_]+", path.read_text()))
        if markers:
            response = "\n".join(sorted(set(markers)))
        else:
            assert stdin is not None
            match = re.search(r"ARMSCANARYCLEAN_[A-Z0-9_]+", stdin)
            assert match
            response = match.group(0)
        output_index = command.index("--output-last-message") + 1
        Path(command[output_index]).write_text(response, encoding="utf-8")
        return ProcessResult(now, now, 0.002, 0, False, None, "{}\n", "")

    def test_fake_runner_produces_verifiable_same_day_certificate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake_executable = root / "codex-fake.exe"
            fake_executable.write_bytes(b"fake")
            fake_credential = root / "source-auth.json"
            fake_credential.write_text('{"token":"fixture-only"}', encoding="utf-8")
            certificate_path, certificate = calibrate(
                ["codex"],
                models={"codex": "fake-model-id"},
                executable_overrides={"codex": str(fake_executable)},
                credential_sources={"codex": fake_credential},
                output_directory=root / "certificates",
                room_directory=root / "rooms",
                inherited_environment={
                    "PATH": "",
                    "OPENAI_API_KEY": "must-not-win-over-credential-file",
                },
                runner=self._fake_codex_runner,
            )
            self.assertEqual(certificate["verdict"], "PASS")
            self.assertEqual(certificate["certified_surfaces"], ["codex"])
            self.assertEqual(certificate["probe_budget"]["actual_model_calls"], 2)
            self.assertEqual(
                certificate["probe_budget"]["version_queries_not_model_calls"], 1
            )
            result = certificate["surface_results"][0]
            self.assertTrue(result["credential_cleanup"]["success"])
            self.assertEqual(len(result["credential_copies"]), 2)
            for copy in result["credential_copies"]:
                self.assertFalse(copy["exists_after_cleanup"])
                self.assertFalse(Path(copy["destination_path"]).exists())
            planted_manifest_path = Path(
                result["environment_manifests"]["planted"]["path"]
            )
            planted_manifest = json.loads(
                planted_manifest_path.read_text(encoding="utf-8")
            )
            self.assertEqual(
                planted_manifest["authentication_environment_present"], []
            )
            checked = check_certificate(
                certificate_path, required_surfaces=["codex"]
            )
            self.assertTrue(checked["pass"], checked["errors"])
            source_certificate = json.loads(
                certificate_path.read_text(encoding="utf-8")
            )
            source_certificate["verdict"] = "FAIL"
            certificate_path.write_text(
                json.dumps(source_certificate, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            certificate_path.with_suffix(
                certificate_path.suffix + ".sha256"
            ).write_text(
                f"{sha256_file(certificate_path)}  {certificate_path.name}\n",
                encoding="utf-8",
            )
            ordinary_failed = check_certificate(
                certificate_path, required_surfaces=["codex"]
            )
            self.assertFalse(ordinary_failed["pass"])
            aggregate = check_certificate_set({"codex": certificate_path})
            self.assertTrue(aggregate["pass"], aggregate["errors"])
            self.assertEqual(aggregate["aggregate_model_calls"], 2)
            source_certificate["verdict"] = "PASS"
            certificate_path.write_text(
                json.dumps(source_certificate, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            certificate_path.with_suffix(
                certificate_path.suffix + ".sha256"
            ).write_text(
                f"{sha256_file(certificate_path)}  {certificate_path.name}\n",
                encoding="utf-8",
            )
            response_path = Path(
                certificate["surface_results"][0]["clean_probe"]["response"]["path"]
            )
            original_response = response_path.read_text(encoding="utf-8")
            response_path.write_text(
                original_response + "tampered",
                encoding="utf-8",
            )
            tampered_evidence = check_certificate(
                certificate_path, required_surfaces=["codex"]
            )
            self.assertFalse(tampered_evidence["pass"])
            self.assertTrue(
                any(
                    "clean_probe.response" in error
                    for error in tampered_evidence["errors"]
                )
            )

            # Even if a local editor recomputes every unkeyed hash, the checker
            # must derive the semantic marker claim from response evidence.
            leaked_response = (
                original_response + "\n" + result["plants"][0]["marker"]
            )
            response_path.write_text(leaked_response, encoding="utf-8")
            rewritten = json.loads(certificate_path.read_text(encoding="utf-8"))
            rewritten_response = rewritten["surface_results"][0]["clean_probe"][
                "response"
            ]
            rewritten_response["size_bytes"] = len(leaked_response.encode("utf-8"))
            rewritten_response["sha256"] = sha256_bytes(
                leaked_response.encode("utf-8")
            )
            certificate_path.write_text(
                json.dumps(rewritten, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            certificate_path.with_suffix(
                certificate_path.suffix + ".sha256"
            ).write_text(
                f"{sha256_file(certificate_path)}  {certificate_path.name}\n",
                encoding="utf-8",
            )
            semantic_tamper = check_certificate(
                certificate_path, required_surfaces=["codex"]
            )
            self.assertFalse(semantic_tamper["pass"])
            self.assertIn(
                "codex clean marker-absence claim does not recompute",
                semantic_tamper["errors"],
            )

    def test_credential_copies_are_removed_when_runner_raises(self) -> None:
        def exploding_runner(*args: object, **kwargs: object) -> ProcessResult:
            del args, kwargs
            raise RuntimeError("scripted runner exception")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake_executable = root / "codex-fake.exe"
            fake_executable.write_bytes(b"fake")
            fake_credential = root / "source-auth.json"
            fake_credential.write_text('{"token":"fixture-only"}', encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "scripted runner exception"):
                calibrate(
                    ["codex"],
                    models={"codex": "fake-model-id"},
                    executable_overrides={"codex": str(fake_executable)},
                    credential_sources={"codex": fake_credential},
                    output_directory=root / "certificates",
                    room_directory=root / "rooms",
                    inherited_environment={"PATH": ""},
                    runner=exploding_runner,
                )
            copied_credentials = list((root / "rooms").rglob("auth.json"))
            self.assertEqual(copied_credentials, [])

    def test_real_gemini_calibration_is_rejected_before_launch(self) -> None:
        def forbidden_runner(*args: object, **kwargs: object) -> ProcessResult:
            del args, kwargs
            self.fail("runner must not be reached for Gemini calibration")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake_executable = root / "gemini-fake.exe"
            fake_executable.write_bytes(b"fake")
            with self.assertRaisesRegex(ValueError, "authorized only for codex and claude"):
                calibrate(
                    ["gemini"],
                    models={"gemini": "fake-model-id"},
                    executable_overrides={"gemini": str(fake_executable)},
                    output_directory=root / "certificates",
                    room_directory=root / "rooms",
                    inherited_environment={"PATH": ""},
                    runner=forbidden_runner,
                )

    def test_same_day_checker_rejects_stale_and_tampered_certificates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            now = datetime.now(timezone.utc)
            stale_day = (now - timedelta(days=1)).date().isoformat()
            certificate = {
                "schema_version": CERTIFICATE_SCHEMA,
                "verdict": "PASS",
                "certified_surfaces": ["codex"],
                "calibration_day": {
                    "date": stale_day,
                    "utc_offset": "+00:00",
                    "basis": "test",
                },
                "probe_budget": {"actual_model_calls": 2},
            }
            path = _write_certificate(root / "stale.json", certificate)
            stale = check_certificate(
                path, required_surfaces=["codex"], now=now
            )
            self.assertFalse(stale["pass"])
            self.assertTrue(any("not same-day" in error for error in stale["errors"]))
            sidecar = path.with_suffix(path.suffix + ".sha256")
            original_sidecar = sidecar.read_text(encoding="utf-8")
            sidecar.write_text("", encoding="utf-8")
            empty_sidecar = check_certificate(
                path, required_surfaces=["codex"], now=now
            )
            self.assertFalse(empty_sidecar["pass"])
            self.assertIn(
                "certificate SHA-256 sidecar is empty", empty_sidecar["errors"]
            )
            sidecar.write_text(original_sidecar, encoding="utf-8")
            path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")
            tampered = check_certificate(
                path, required_surfaces=["codex"], now=now
            )
            self.assertFalse(tampered["pass"])
            self.assertIn("certificate SHA-256 sidecar mismatch", tampered["errors"])


if __name__ == "__main__":
    unittest.main()
