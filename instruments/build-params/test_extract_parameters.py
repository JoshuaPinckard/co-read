import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).with_name("extract_parameters.py")
SPEC = importlib.util.spec_from_file_location("build_params_extract", SCRIPT)
assert SPEC and SPEC.loader
bp = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = bp
SPEC.loader.exec_module(bp)


class BuildParameterExtractorTests(unittest.TestCase):
    def test_nearest_rank(self):
        self.assertEqual(
            bp.nearest_rank(range(1, 101)),
            {
                "count": 100,
                "percentile_method": "nearest-rank; rank=ceil(p*n)",
                "p50": 50,
                "p90": 90,
                "p99": 99,
                "max": 100,
            },
        )
        self.assertIsNone(bp.nearest_rank([])["p50"])

    def test_shell_parser_does_not_execute_or_expand(self):
        intent, mentions = bp.parse_shell_command(
            'Get-Content -LiteralPath ".\\src\\a.ts"; Set-Content $env:USERPROFILE\\secret.txt',
            r"C:\repo",
            r"C:\Users\ignored",
        )
        self.assertEqual(intent, "read_write")
        self.assertIn(r"c:\repo\src\a.ts", {item.path for item in mentions})
        self.assertTrue(any(item.path is None for item in mentions))
        call = bp.Call("mixed", "PowerShell", "a", False, "s", 1.0, r"C:\repo", None, "r", "main", 1, 1)
        merged = bp.merge_shell_group(
            "mixed",
            [bp.ShellCandidate(call, "0" * 64, intent, mentions)],
            __import__("collections").Counter(),
        )
        self.assertIsNotNone(merged)

    def test_sensitive_path_redaction(self):
        shown, redacted = bp.display_path(r"C:\Users\name\repo\vault\api-token.json")
        self.assertTrue(redacted)
        self.assertNotIn("api-token", shown)
        self.assertIn("<credential-path-redacted>", shown)

    def test_paired_concurrency_delta_uses_union_minute_support(self):
        base = [(1.0, "a", ("repo",))]
        expanded = base + [(2.0, "b", ("repo",)), (61.0, "c", ("repo",))]
        delta = bp.paired_concurrency_delta_metrics(base, expanded)
        self.assertEqual(
            delta["global_actor_delta_per_expanded_active_utc_minute"]["count"], 2
        )
        self.assertEqual(
            delta["global_actor_delta_per_expanded_active_utc_minute"]["p50"], 1
        )
        self.assertEqual(delta["global_minutes_with_positive_actor_delta"]["numerator"], 2)

    def test_synthetic_scan_and_metrics(self):
        records = [
            {
                "type": "assistant",
                "timestamp": "2026-08-01T00:00:00.000Z",
                "uuid": "u1",
                "sessionId": "s1",
                "cwd": r"C:\repo",
                "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": r"C:\repo\a.ts"}}]},
            },
            {
                "type": "user",
                "timestamp": "2026-08-01T00:00:01.000Z",
                "parentUuid": "u1",
                "sessionId": "s1",
                "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "1 a"}]},
                "toolUseResult": {"file": {"filePath": r"C:\repo\a.ts", "startLine": 1, "numLines": 1, "content": "a\n"}},
            },
            {
                "type": "assistant",
                "timestamp": "2026-08-01T00:00:02.000Z",
                "uuid": "u2",
                "sessionId": "s1",
                "cwd": r"C:\repo",
                "message": {"content": [{"type": "tool_use", "id": "t2", "name": "Edit", "input": {"file_path": r"C:\repo\a.ts"}}]},
            },
            {
                "type": "user",
                "timestamp": "2026-08-01T00:00:03.000Z",
                "parentUuid": "u2",
                "sessionId": "s1",
                "message": {"content": [{"type": "tool_result", "tool_use_id": "t2", "content": "ok"}]},
                "toolUseResult": {
                    "filePath": r"C:\repo\a.ts",
                    "structuredPatch": [{"oldStart": 1, "oldLines": 1, "newStart": 1, "newLines": 1, "lines": ["-a", "+b"]}],
                    "originalFile": "a",
                },
            },
            {
                "type": "assistant",
                "timestamp": "2026-08-01T00:00:04.000Z",
                "uuid": "u3",
                "sessionId": "s1",
                "cwd": r"C:\repo",
                "message": {"content": [{"type": "tool_use", "id": "t3", "name": "Bash", "input": {"command": "rg needle ./src/a.ts"}}]},
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            corpus = Path(directory) / "projects"
            project = corpus / "c--repo"
            project.mkdir(parents=True)
            transcript = project / "s1.jsonl"
            transcript.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
            operations, calls, shell, spans, metadata, manifest = bp.scan_corpus(corpus, progress_every=0)
            metrics = bp.build_metrics(operations, calls, shell, spans, metadata)
            frozen_manifest = {
                "snapshot_utc": metadata["snapshot_utc"],
                "file_count": metadata["corpus_file_count"],
                "byte_count": metadata["corpus_bytes"],
                "frozen_prefix_sha256": metadata["corpus_snapshot_sha256"],
                "files": manifest,
            }
            transcript.write_text(
                transcript.read_text(encoding="utf-8")
                + json.dumps({"timestamp": "2026-08-02T00:00:00.000Z", "sessionId": "later"})
                + "\n",
                encoding="utf-8",
            )
            (project / "a-new.jsonl").write_text(
                json.dumps({"timestamp": "2026-08-03T00:00:00.000Z", "sessionId": "new"})
                + "\n",
                encoding="utf-8",
            )
            reused = bp.scan_corpus(
                corpus, progress_every=0, frozen_manifest=frozen_manifest
            )
        self.assertEqual(metadata["corpus_file_count"], 1)
        self.assertEqual(len(manifest), 1)
        self.assertEqual(metrics["event_volume"]["successful_deduplicated_structured_events"], 2)
        self.assertEqual(metrics["read_window_sizes"]["num_lines"]["p50"], 1)
        self.assertEqual(metrics["read_window_sizes"]["returned_window_utf8_bytes"]["count"], 1)
        self.assertEqual(metrics["edit_region_sizes"]["parsed_change_block_denominator"], 1)
        self.assertEqual(metrics["capture_coverage"]["deduplicated_shell_commands"], 1)
        reused_metadata = reused[4]
        self.assertEqual(reused_metadata["corpus_snapshot_sha256"], metadata["corpus_snapshot_sha256"])
        self.assertEqual(reused_metadata["corpus_file_count"], 1)
        self.assertEqual(reused_metadata["diagnostics"]["live_files_outside_frozen_manifest"], 1)


if __name__ == "__main__":
    unittest.main()
