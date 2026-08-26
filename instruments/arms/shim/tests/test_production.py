from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from instruments.arms.shim.adapters import SubjectCommand
from instruments.arms.shim.production import (
    ProductionSubjectConfig,
    ProductionSubjectLauncher,
)
from instruments.arms.shim.schema import Side
from instruments.arms.shim.util import EventLog, LogicalClock, ShimError


class _FakeAdapter:
    cli = "codex"
    model = "fake-model"

    def __init__(self, program: str, *, command_error: BaseException | None = None) -> None:
        self.program = program
        self.command_error = command_error

    def command(self, *, prompt: str, cwd: Path) -> SubjectCommand:
        if self.command_error is not None:
            raise self.command_error
        return SubjectCommand((sys.executable, "-c", self.program), {})


def _side(root: Path) -> Side:
    return Side(
        label="A",
        source_name="left",
        parent="0" * 40,
        intent_subject="repair source",
        intent_body="",
        source_patch=root / "source.patch",
        source_patch_sha256="0" * 64,
        test_patch=root / "test.patch",
        test_patch_sha256="0" * 64,
        source_paths=("source.py",),
        test_paths=("tests/test_source.py",),
        focal_targets=("tests/test_source.py",),
    )


def _manifest(*_: object, **__: object) -> dict[str, object]:
    return {"warnings": [], "instruction_locations": []}


class ProductionLauncherTests(unittest.TestCase):
    def _launcher(
        self,
        root: Path,
        program: str,
        *,
        command_error: BaseException | None = None,
    ) -> ProductionSubjectLauncher:
        credential = root / "source-auth.json"
        credential.write_text('{"secret":"not-persisted"}', encoding="utf-8")
        return ProductionSubjectLauncher(
            ProductionSubjectConfig(
                adapter=_FakeAdapter(program, command_error=command_error),  # type: ignore[arg-type]
                canary_certificates={
                    "codex": root / "codex-certificate.json",
                    "claude": root / "claude-certificate.json",
                },
                clean_room_root=root / "clean-rooms",
                credential_file=credential,
            )
        )

    def _patch_preflight(
        self,
    ) -> tuple[mock._patch, mock._patch, mock._patch, mock._patch]:
        return (
            mock.patch(
                "instruments.arms.shim.production.check_certificate_set",
                return_value={"pass": True, "errors": [], "aggregate_model_calls": 6},
            ),
            mock.patch(
                "instruments.arms.shim.production.detect_version",
                return_value="fake-cli 1.0",
            ),
            mock.patch(
                "instruments.arms.shim.production.environment_manifest",
                side_effect=_manifest,
            ),
            mock.patch(
                "instruments.arms.shim.production.certified_subject_binding",
                return_value={
                    "detected_version": "fake-cli 1.0",
                    "requested_model_identifier": "fake-model",
                },
            ),
        )

    def test_mechanical_polls_ignore_subject_prose_and_cleanup_credential(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tree = root / "tree"
            tree.mkdir()
            tree.joinpath("source.py").write_text("before\n", encoding="utf-8")
            program = (
                "from pathlib import Path; import time; "
                "Path('source.py').write_text('after\\n'); "
                "print('FILES-READ: phantom.py; WRITE-SET: invented.py'); "
                "time.sleep(0.5)"
            )
            launcher = self._launcher(root, program)
            patches = self._patch_preflight()
            with patches[0] as certificate_check, patches[1], patches[2], patches[3], mock.patch(
                "instruments.arms.shim.production.PRODUCTION_POLL_SECONDS", 0.1
            ), mock.patch(
                "instruments.arms.shim.production.PRODUCTION_TIMEOUT_SECONDS", 15.0
            ), EventLog(
                root / "events.jsonl",
                run_id="run",
                draw_id="draw",
                site={"id": "site"},
                arm={"id": 6},
                stratum="test",
                clock=LogicalClock(),
            ) as log:
                result = launcher.run(
                    draw_id="draw",
                    instance_id="A0",
                    side=_side(root),
                    tree=tree,
                    artifact_root=root / "artifacts",
                    log=log,
                    poll_writes=True,
                )

            self.assertGreaterEqual(result.poll_count, 2)
            self.assertEqual([row["path"] for row in result.write_records], ["source.py"])
            self.assertEqual(
                result.completion_snapshot["source.py"].data.replace(b"\r\n", b"\n"),
                b"after\n",
            )
            self.assertIn(b"phantom.py", result.process.stdout)
            certificate_check.assert_called_once()
            self.assertEqual(
                set(certificate_check.call_args.args[0]), {"codex", "claude"}
            )
            copied = root / "clean-rooms" / "draw" / "A0" / "config" / "codex" / "auth.json"
            self.assertFalse(copied.exists())
            cleanup = json.loads(result.credential_cleanup_path.read_text(encoding="utf-8"))
            self.assertEqual(cleanup["phase"], "launch-complete")
            self.assertTrue(cleanup["credential_cleanup"]["success"])
            self.assertFalse(
                cleanup["credential_copies"][0]["exists_after_cleanup"]
            )

    def test_timeout_uses_launch_to_finish_budget_and_cleans_credential(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tree = root / "tree"
            tree.mkdir()
            launcher = self._launcher(root, "import time; time.sleep(10)")
            patches = self._patch_preflight()
            with patches[0], patches[1], patches[2], patches[3], mock.patch(
                "instruments.arms.shim.production.PRODUCTION_POLL_SECONDS", 0.03
            ), mock.patch(
                "instruments.arms.shim.production.PRODUCTION_TIMEOUT_SECONDS", 0.12
            ), EventLog(
                root / "events.jsonl",
                run_id="run",
                draw_id="draw",
                site={"id": "site"},
                arm={"id": 6},
                stratum="test",
                clock=LogicalClock(),
            ) as log:
                result = launcher.run(
                    draw_id="draw",
                    instance_id="A0",
                    side=_side(root),
                    tree=tree,
                    artifact_root=root / "artifacts",
                    log=log,
                )

            self.assertTrue(result.process.timed_out)
            # Tree termination/pipe collection can finish after the budget,
            # especially on Windows; the process is nevertheless classified
            # timed out at the launch-relative deadline.
            self.assertGreaterEqual(result.process.actual_seconds, 0.12)
            copied = root / "clean-rooms" / "draw" / "A0" / "config" / "codex" / "auth.json"
            self.assertFalse(copied.exists())
            cleanup = json.loads(result.credential_cleanup_path.read_text(encoding="utf-8"))
            self.assertEqual(cleanup["phase"], "launch-timeout")

    def test_nonzero_completion_is_data_and_credential_is_cleaned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tree = root / "tree"
            tree.mkdir()
            launcher = self._launcher(root, "raise SystemExit(7)")
            patches = self._patch_preflight()
            with patches[0], patches[1], patches[2], patches[3], EventLog(
                root / "events.jsonl",
                run_id="run",
                draw_id="draw",
                site={"id": "site"},
                arm={"id": 6},
                stratum="test",
                clock=LogicalClock(),
            ) as log:
                result = launcher.run(
                    draw_id="draw",
                    instance_id="A0",
                    side=_side(root),
                    tree=tree,
                    artifact_root=root / "artifacts",
                    log=log,
                )

            self.assertEqual(result.process.returncode, 7)
            copied = root / "clean-rooms" / "draw" / "A0" / "config" / "codex" / "auth.json"
            self.assertFalse(copied.exists())
            cleanup = json.loads(result.credential_cleanup_path.read_text(encoding="utf-8"))
            self.assertEqual(cleanup["phase"], "launch-failure")

    def test_command_exception_cleans_credential(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tree = root / "tree"
            tree.mkdir()
            launcher = self._launcher(
                root,
                "",
                command_error=RuntimeError("synthetic command construction failure"),
            )
            patches = self._patch_preflight()
            with patches[0], patches[1], patches[2], patches[3], EventLog(
                root / "events.jsonl",
                run_id="run",
                draw_id="draw",
                site={"id": "site"},
                arm={"id": 6},
                stratum="test",
                clock=LogicalClock(),
            ) as log:
                with self.assertRaisesRegex(RuntimeError, "synthetic command"):
                    launcher.run(
                        draw_id="draw",
                        instance_id="A0",
                        side=_side(root),
                        tree=tree,
                        artifact_root=root / "artifacts",
                        log=log,
                    )

            copied = root / "clean-rooms" / "draw" / "A0" / "config" / "codex" / "auth.json"
            self.assertFalse(copied.exists())
            cleanup = json.loads(
                (root / "artifacts" / "credential-cleanup.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertTrue(cleanup["credential_cleanup"]["success"])
            self.assertEqual(cleanup["phase"], "launch-exception")

    def test_preflight_exception_cleans_credential(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tree = root / "tree"
            tree.mkdir()
            launcher = self._launcher(root, "")
            with mock.patch(
                "instruments.arms.shim.production.check_certificate_set",
                return_value={"pass": True, "errors": []},
            ), mock.patch(
                "instruments.arms.shim.production.detect_version",
                side_effect=ShimError("synthetic version failure"),
            ):
                with self.assertRaisesRegex(ShimError, "synthetic version"):
                    with EventLog(
                        root / "events.jsonl",
                        run_id="run",
                        draw_id="draw",
                        site={"id": "site"},
                        arm={"id": 6},
                        stratum="test",
                        clock=LogicalClock(),
                    ) as log:
                        launcher.run(
                            draw_id="draw",
                            instance_id="A0",
                            side=_side(root),
                            tree=tree,
                            artifact_root=root / "artifacts",
                            log=log,
                        )

            copied = root / "clean-rooms" / "draw" / "A0" / "config" / "codex" / "auth.json"
            self.assertFalse(copied.exists())

    def test_partial_provision_exception_cleans_known_destination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tree = root / "tree"
            tree.mkdir()
            launcher = self._launcher(root, "")

            def partial_copy(cli: str, source: Path, env: dict[str, str]) -> dict[str, object]:
                self.assertEqual(cli, "codex")
                destination = Path(env["CODEX_HOME"]) / "auth.json"
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(source.read_bytes())
                raise OSError("synthetic post-copy failure")

            with mock.patch(
                "instruments.arms.shim.production.check_certificate_set",
                return_value={"pass": True, "errors": []},
            ), mock.patch(
                "instruments.arms.shim.production.provision_credential",
                side_effect=partial_copy,
            ):
                with self.assertRaisesRegex(OSError, "synthetic post-copy"):
                    with EventLog(
                        root / "events.jsonl",
                        run_id="run",
                        draw_id="draw",
                        site={"id": "site"},
                        arm={"id": 6},
                        stratum="test",
                        clock=LogicalClock(),
                    ) as log:
                        launcher.run(
                            draw_id="draw",
                            instance_id="A0",
                            side=_side(root),
                            tree=tree,
                            artifact_root=root / "artifacts",
                            log=log,
                        )

            copied = root / "clean-rooms" / "draw" / "A0" / "config" / "codex" / "auth.json"
            self.assertFalse(copied.exists())
            cleanup = json.loads(
                (root / "artifacts" / "credential-cleanup.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertFalse(
                cleanup["credential_copies"][0]["exists_after_cleanup"]
            )

    def test_missing_aggregate_surface_fails_before_clean_room(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tree = root / "tree"
            tree.mkdir()
            launcher = self._launcher(root, "")
            launcher = ProductionSubjectLauncher(
                ProductionSubjectConfig(
                    adapter=launcher.config.adapter,
                    canary_certificates={"codex": root / "codex.json"},
                    clean_room_root=root / "clean-rooms",
                    credential_file=launcher.config.credential_file,
                )
            )
            with EventLog(
                root / "events.jsonl",
                run_id="run",
                draw_id="draw",
                site={"id": "site"},
                arm={"id": 6},
                stratum="test",
                clock=LogicalClock(),
            ) as log:
                with self.assertRaisesRegex(ShimError, "one same-day certificate"):
                    launcher.run(
                        draw_id="draw",
                        instance_id="A0",
                        side=_side(root),
                        tree=tree,
                        artifact_root=root / "artifacts",
                        log=log,
                    )
            self.assertFalse((root / "clean-rooms").exists())

    def test_current_cli_version_must_match_certified_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tree = root / "tree"
            tree.mkdir()
            launcher = self._launcher(root, "raise SystemExit(0)")
            with mock.patch(
                "instruments.arms.shim.production.check_certificate_set",
                return_value={"pass": True, "errors": []},
            ), mock.patch(
                "instruments.arms.shim.production.detect_version",
                return_value="fake-cli 2.0",
            ), mock.patch(
                "instruments.arms.shim.production.certified_subject_binding",
                return_value={
                    "detected_version": "fake-cli 1.0",
                    "requested_model_identifier": "fake-model",
                },
            ), mock.patch(
                "instruments.arms.shim.production.environment_manifest",
                side_effect=_manifest,
            ), EventLog(
                root / "events.jsonl",
                run_id="run",
                draw_id="draw",
                site={"id": "site"},
                arm={"id": 1},
                stratum="test",
                clock=LogicalClock(),
            ) as log:
                with self.assertRaisesRegex(ShimError, "CLI version does not match"):
                    launcher.run(
                        draw_id="draw",
                        instance_id="A0",
                        side=_side(root),
                        tree=tree,
                        artifact_root=root / "artifacts",
                        log=log,
                    )
            manifest = json.loads(
                (root / "artifacts" / "environment-manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertFalse(
                manifest["production_preflight"]["canary_subject_binding"]["matches"]
            )

    def test_requested_model_must_match_certified_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tree = root / "tree"
            tree.mkdir()
            launcher = self._launcher(root, "raise SystemExit(0)")
            with mock.patch(
                "instruments.arms.shim.production.check_certificate_set",
                return_value={"pass": True, "errors": []},
            ), mock.patch(
                "instruments.arms.shim.production.detect_version",
                return_value="fake-cli 1.0",
            ), mock.patch(
                "instruments.arms.shim.production.certified_subject_binding",
                return_value={
                    "detected_version": "fake-cli 1.0",
                    "requested_model_identifier": "different-model",
                },
            ), mock.patch(
                "instruments.arms.shim.production.environment_manifest",
                side_effect=_manifest,
            ), EventLog(
                root / "events.jsonl",
                run_id="run",
                draw_id="draw",
                site={"id": "site"},
                arm={"id": 1},
                stratum="test",
                clock=LogicalClock(),
            ) as log:
                with self.assertRaisesRegex(ShimError, "model does not match"):
                    launcher.run(
                        draw_id="draw",
                        instance_id="A0",
                        side=_side(root),
                        tree=tree,
                        artifact_root=root / "artifacts",
                        log=log,
                    )
            manifest = json.loads(
                (root / "artifacts" / "environment-manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertFalse(
                manifest["production_preflight"]["canary_subject_binding"]["matches"]
            )


if __name__ == "__main__":
    unittest.main()
