from __future__ import annotations

import collections
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import build_evalset as extract
import arms
import score


def record(uuid, parent, ts, session="s1", agent=None, message_id=None, content=None, result=None):
    return {
        "type": "assistant" if content and any(x.get("type") == "tool_use" for x in content) else "user",
        "uuid": uuid,
        "parentUuid": parent,
        "sessionId": session,
        "agentId": agent,
        "timestamp": f"2026-01-01T00:00:{ts:06.3f}Z",
        "cwd": r"C:\repo",
        "gitBranch": "main",
        "message": {"id": message_id, "content": content or []},
        **({"toolUseResult": result} if result is not None else {}),
    }


def tool_call(tool_id, name, inputs):
    return {"type": "tool_use", "id": tool_id, "name": name, "input": inputs}


def tool_result(tool_id, content="ok", error=False):
    return {"type": "tool_result", "tool_use_id": tool_id, "content": content, "is_error": error}


def write_jsonl(path: Path, records):
    path.write_text("".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")


def test_result_path_wins_and_split_logical_message_is_causal(tmp_path):
    transcript = tmp_path / "session.jsonl"
    records = [
        record("g", None, 0, message_id="logical-1", content=[tool_call("grep-1", "Grep", {"pattern": "needle", "path": "."})]),
        # Split assistant segment from the same logical API message.
        record("parallel", None, 0.1, message_id="logical-1", content=[tool_call("other", "PowerShell", {})]),
        record(
            "gr", "g", 1, content=[tool_result("grep-1", "Found 1 file\nsrc\\right.js")],
            result={"mode": "files_with_matches", "filenames": [r"src\right.js"]},
        ),
        record("r", "parallel", 2, message_id="logical-2", content=[tool_call("read-1", "Read", {"file_path": r"C:\repo\wrong.js"})]),
        record(
            "rr", "r", 3, content=[tool_result("read-1", "contents")],
            result={"file": {"filePath": r"C:\repo\src\right.js", "startLine": 1, "numLines": 5, "totalLines": 5}},
        ),
    ]
    write_jsonl(transcript, records)
    diagnostics = collections.Counter()
    calls, parents, bundles = extract.scan_transcript(transcript, set(), diagnostics)
    batches = extract.group_batches(calls, diagnostics)
    grep = next(call for call in calls if call["kind"] == "grep")
    timeline, _, _ = extract.causal_timeline(
        grep, batches, [batch["ts"] for batch in batches], parents, bundles, diagnostics
    )
    outcome = extract.outcome_for_window(timeline, 300)
    assert outcome["reads"] == [r"c:\repo\src\right.js"]
    assert extract.grep_returned_paths(grep) == [r"c:\repo\src\right.js"]
    assert outcome["seconds"] == 1.0


def test_next_grep_stops_attribution(tmp_path):
    transcript = tmp_path / "session.jsonl"
    records = [
        record("g1", None, 0, message_id="m1", content=[tool_call("grep-1", "Grep", {"pattern": "bad"})]),
        record("gr1", "g1", 1, content=[tool_result("grep-1", "No matches found")], result={"filenames": []}),
        record("g2", "gr1", 2, message_id="m2", content=[tool_call("grep-2", "Grep", {"pattern": "good"})]),
        record("gr2", "g2", 3, content=[tool_result("grep-2", "Found 1 file\ngood.js")], result={"filenames": ["good.js"]}),
        record("r", "gr2", 4, message_id="m3", content=[tool_call("read-1", "Read", {"file_path": "wrong.js"})]),
        record(
            "rr", "r", 5, content=[tool_result("read-1", "contents")],
            result={"file": {"filePath": r"C:\repo\good.js", "startLine": 1, "numLines": 1, "totalLines": 1}},
        ),
    ]
    write_jsonl(transcript, records)
    diagnostics = collections.Counter()
    calls, parents, bundles = extract.scan_transcript(transcript, set(), diagnostics)
    batches = extract.group_batches(calls, diagnostics)
    times = [batch["ts"] for batch in batches]
    greps = [call for call in calls if call["kind"] == "grep"]
    first = extract.outcome_for_window(
        extract.causal_timeline(greps[0], batches, times, parents, bundles, diagnostics)[0], 300
    )
    second = extract.outcome_for_window(
        extract.causal_timeline(greps[1], batches, times, parents, bundles, diagnostics)[0], 300
    )
    assert first == {"reads": [], "grep": True, "seconds": 1.0}
    assert second["reads"] == [r"c:\repo\good.js"]


def test_actions_before_slow_result_and_sibling_branch_do_not_count(tmp_path):
    transcript = tmp_path / "session.jsonl"
    records = [
        record("g", None, 0, message_id="m1", content=[tool_call("grep-1", "Grep", {"pattern": "x"})]),
        record("early", "g", 1, message_id="m2", content=[tool_call("read-early", "Read", {"file_path": "early.js"})]),
        record("early-r", "early", 2, content=[tool_result("read-early", "early")]),
        record("gr", "g", 10, content=[tool_result("grep-1", "No matches found")], result={"filenames": []}),
        record("sibling", None, 11, message_id="m3", content=[tool_call("read-sibling", "Read", {"file_path": "sibling.js"})]),
        record("sibling-r", "sibling", 12, content=[tool_result("read-sibling", "sibling")]),
    ]
    write_jsonl(transcript, records)
    diagnostics = collections.Counter()
    calls, parents, bundles = extract.scan_transcript(transcript, set(), diagnostics)
    batches = extract.group_batches(calls, diagnostics)
    grep = next(call for call in calls if call["kind"] == "grep")
    timeline, saw_non_descendant, _ = extract.causal_timeline(
        grep, batches, [batch["ts"] for batch in batches], parents, bundles, diagnostics
    )
    assert timeline == []
    assert saw_non_descendant
    assert extract.outcome_for_window(timeline, 300) is None


def test_window_boundary_is_inclusive():
    timeline = [{"delta": 60.0, "reads": ["x"], "unresolved_read": False, "has_grep": False}]
    assert extract.outcome_for_window(timeline, 60)["reads"] == ["x"]


def test_msys_paths_become_drive_qualified():
    assert extract.normalise_path("/c/Users/Joshp/repo/x.js", r"C:\elsewhere") == r"c:\users\joshp\repo\x.js"
    assert extract.normalise_path(r"\c\Users\Joshp\repo\x.js", r"C:\elsewhere") == r"c:\users\joshp\repo\x.js"


def test_returned_paths_are_result_prefixes_inside_query_scope():
    call = {
        "cwd": r"C:\repo",
        "input": {"path": r"C:\repo\src", "output_mode": "files_with_matches"},
        "result_content": (
            r"C:\repo\src\right.js:12:const shown = 'C:\repo\wrong.js';" + "\n"
            + r"C:\output too large. full output saved to: C:\other\result.txt"
        ),
        "structured_result": None,
        "result_error": False,
    }
    assert extract.grep_returned_paths(call) == [r"c:\repo\src\right.js"]


def test_returned_path_parser_ignores_tool_errors():
    call = {
        "cwd": r"C:\repo",
        "input": {"path": r"C:\repo", "output_mode": "count"},
        "result_content": r"<tool_use_error>Path does not exist: C:\repo\missing:1",
        "structured_result": None,
        "result_error": True,
    }
    assert extract.grep_returned_paths(call) == []


def test_split_followup_message_with_read_and_grep_is_positive(tmp_path):
    transcript = tmp_path / "session.jsonl"
    records = [
        record("g0", None, 0, message_id="search", content=[tool_call("grep-0", "Grep", {"pattern": "first"})]),
        record("gr0", "g0", 1, content=[tool_result("grep-0", "No matches found")], result={"filenames": []}),
        record("next-grep", "gr0", 2, message_id="mixed", content=[tool_call("grep-1", "Grep", {"pattern": "second"})]),
        record("next-read", "gr0", 2.1, message_id="mixed", content=[tool_call("read-1", "Read", {"file_path": "good.js"})]),
        record(
            "rr", "next-read", 3, content=[tool_result("read-1", "contents")],
            result={"file": {"filePath": r"C:\repo\good.js", "startLine": 1, "numLines": 1, "totalLines": 1}},
        ),
        record("gr1", "next-grep", 3.1, content=[tool_result("grep-1", "No matches found")], result={"filenames": []}),
    ]
    write_jsonl(transcript, records)
    diagnostics = collections.Counter()
    calls, parents, bundles = extract.scan_transcript(transcript, set(), diagnostics)
    batches = extract.group_batches(calls, diagnostics)
    grep = next(call for call in calls if call["tool_id"] == "grep-0")
    timeline = extract.causal_timeline(
        grep, batches, [batch["ts"] for batch in batches], parents, bundles, diagnostics
    )[0]
    outcome = extract.outcome_for_window(timeline, 300)
    assert outcome["reads"] == [r"c:\repo\good.js"]
    assert not outcome["grep"]


def test_head_limit_zero_means_unlimited():
    payload = b"one\ntwo\n"
    assert arms._slice_response_lines(payload, {"head_limit": 0}) == payload


def test_missing_line_number_option_uses_claude_default():
    default_argv = arms.ripgrep_argv({"pattern": "x", "output_mode": "content"}, ".")
    disabled_argv = arms.ripgrep_argv(
        {"pattern": "x", "output_mode": "content", "-n": False}, "."
    )
    assert "--line-number" in default_argv
    assert "--no-line-number" in disabled_argv
    max_columns = default_argv.index("--max-columns")
    assert default_argv[max_columns + 1] == "500"


def test_ripgrep_replays_claude_long_line_omission(tmp_path):
    source = tmp_path / "repo"
    source.mkdir()
    (source / "long.txt").write_text("needle " + "x" * 600 + "\n", encoding="utf-8")
    logical = r"C:\logical\repo"
    result = arms.run_ripgrep(
        {
            "cwd": logical,
            "query": {
                "pattern": "needle",
                "path": logical,
                "output_mode": "content",
                "-n": True,
            },
        },
        source,
        logical,
    )
    assert result["error"] is None
    assert b"[Omitted long matching line]" in result["payload"]
    assert b"x" * 500 not in result["payload"]
    assert result["ranked_paths"] == [logical.lower() + r"\long.txt"]


def test_physical_control_path_is_not_measured():
    scope = {
        "rg_target_absolute": True,
        "source_scope": r"C:\Temp\retrieval-head\repo",
        "logical_scope": r"C:\Users\USER\Desktop\toolsenabled-current",
    }
    physical = b"C:\\Temp\\retrieval-head\\repo\\src\\x.js:1:match\n"
    visible = arms._logicalise_ripgrep_payload(physical, scope)
    assert b"retrieval-head" not in visible
    assert visible.startswith(b"C:\\Users\\joshp\\Desktop\\toolsenabled-current")


def test_error_response_bytes_match_payload(tmp_path):
    result = arms.run_ripgrep(
        {"cwd": r"C:\repo", "query": {"pattern": "x", "path": r"C:\outside"}},
        tmp_path,
        r"C:\repo",
    )
    assert result["error"]
    assert result["response_bytes"] == len(result["payload"]) > 0
    assert result["payload"].startswith(b"[error]")


def test_ripgrep_stops_after_recorded_line_window(tmp_path):
    source = tmp_path / "repo"
    source.mkdir()
    (source / "many.txt").write_text("".join(f"needle {n}\n" for n in range(100)), encoding="utf-8")
    logical = r"C:\logical\repo"
    result = arms.run_ripgrep(
        {
            "cwd": logical,
            "query": {
                "pattern": "needle",
                "path": logical,
                "output_mode": "content",
                "-n": True,
                "head_limit": 3,
            },
        },
        source,
        logical,
    )
    assert result["error"] is None
    assert result["metadata"]["response_capped"] is True
    assert len(result["payload"].splitlines()) == 3


def test_ripgrep_searches_hidden_project_files_but_not_git(tmp_path):
    source = tmp_path / "repo"
    source.mkdir()
    (source / ".claude").mkdir()
    (source / ".claude" / "settings.json").write_text("needle\n", encoding="utf-8")
    (source / ".git").mkdir()
    (source / ".git" / "config").write_text("needle\n", encoding="utf-8")
    logical = r"C:\logical\repo"
    result = arms.run_ripgrep(
        {
            "cwd": logical,
            "query": {"pattern": "needle", "path": logical, "output_mode": "files_with_matches"},
        },
        source,
        logical,
    )
    assert result["error"] is None
    assert any(path.endswith(r"\.claude\settings.json") for path in result["ranked_paths"])
    assert not any("\\.git\\" in path for path in result["ranked_paths"])


def test_behavioral_failures_remain_in_quality_denominator():
    records = [
        {"id": "positive", "followed_by_read": [r"C:\repo\a.js"], "followed_by_grep": False},
        {"id": "failure", "followed_by_read": [], "followed_by_grep": True},
        {"id": "external", "followed_by_read": [r"C:\other\x.js"], "followed_by_grep": False},
    ]
    arm_rows = {
        "positive": {"ranked_paths": [r"C:\repo\a.js"], "response_bytes": 4, "latency_ms": 1, "error": None},
        "failure": {"ranked_paths": [], "response_bytes": 0, "latency_ms": 1, "error": None},
        "external": {"ranked_paths": [], "response_bytes": 0, "latency_ms": 1, "error": None},
    }
    measured = score.aggregate_arm(records, arm_rows, r"C:\repo")
    assert measured["positive_queries"] == 1
    assert measured["behavioral_failure_queries"] == 1
    assert measured["quality_queries"] == 2
    assert measured["recall@1"] == 1.0
    assert measured["precision@1"] == 0.5
    assert measured["failure@20"] == 0.5


def test_partial_run_recovers_valid_prefix(tmp_path):
    path = tmp_path / "runs.jsonl.partial"
    path.write_text(
        json.dumps({"record_id": "q", "arm": "bm25", "fingerprint": "f"})
        + "\n{\"record_id\":",
        encoding="utf-8",
    )
    rows = score.read_existing_runs(path, "f")
    assert list(rows) == [("q", "bm25")]
