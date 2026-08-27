"""Render final build parameters from extraction plus the manual shell audit."""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import math
import os
from pathlib import Path
from typing import Any, Mapping, Sequence


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".partial")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".partial")
    temporary.write_text(value.rstrip() + "\n", encoding="utf-8")
    os.replace(temporary, path)


def fmt_number(value: Any, digits: int = 3) -> str:
    if value is None:
        return "not observed"
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        if value.is_integer():
            return f"{int(value):,}"
        return f"{value:,.{digits}f}".rstrip("0").rstrip(".")
    return str(value)


def fmt_percent(value: Any, digits: int = 1) -> str:
    return "undefined" if value is None else f"{value:.{digits}f}%"


def fmt_dist(value: Mapping[str, Any], unit: str = "") -> str:
    suffix = f" {unit}" if unit else ""
    return (
        f"n={fmt_number(value.get('count'))}; "
        f"p50/p90/p99/max={fmt_number(value.get('p50'))}/"
        f"{fmt_number(value.get('p90'))}/{fmt_number(value.get('p99'))}/"
        f"{fmt_number(value.get('max'))}{suffix}"
    )


def human_duration(seconds: Any) -> str:
    if not isinstance(seconds, (int, float)):
        return "not observed"
    absolute = abs(seconds)
    if absolute < 60:
        return f"{seconds:.3f} s"
    if absolute < 3600:
        return f"{seconds / 60:.2f} min"
    if absolute < 86400:
        return f"{seconds / 3600:.2f} h"
    return f"{seconds / 86400:.2f} d"


def fmt_duration_dist(value: Mapping[str, Any]) -> str:
    return (
        f"n={fmt_number(value.get('count'))}; p50/p90/p99/max="
        f"{human_duration(value.get('p50'))}/{human_duration(value.get('p90'))}/"
        f"{human_duration(value.get('p99'))}/{human_duration(value.get('max'))}"
    )


def wilson(successes: float, total: float, z: float = 1.959963984540054) -> dict[str, Any]:
    if total <= 0:
        return {"successes": successes, "denominator": total, "estimate": None, "low": None, "high": None}
    p = successes / total
    denominator = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denominator
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total) / denominator
    return {
        "successes": successes,
        "denominator": total,
        "estimate": p,
        "low": max(0.0, center - margin),
        "high": min(1.0, center + margin),
        "method": "Wilson score, 95%",
    }


def validate_and_summarize_shell_audit(
    extraction: Mapping[str, Any], labels: Mapping[str, Any]
) -> dict[str, Any]:
    sample = extraction["parameters"]["shell_validation_sample"]
    frozen_hash = extraction["corpus"]["corpus_snapshot_sha256"]
    if labels.get("frozen_prefix_sha256") != frozen_hash:
        raise ValueError("shell audit labels do not match extraction frozen-prefix SHA-256")
    sample_entries = {entry["selection_hash"]: entry for entry in sample["entries"]}
    label_entries = labels.get("entries")
    if not isinstance(label_entries, list) or len(label_entries) != 50:
        raise ValueError("shell audit must contain exactly 50 entries")
    seen: set[str] = set()
    strata: dict[str, list[dict[str, Any]]] = {}
    cleaned_entries: list[dict[str, Any]] = []
    for label in label_entries:
        selection_hash = label.get("selection_hash")
        if not isinstance(selection_hash, str) or selection_hash not in sample_entries:
            raise ValueError("shell audit contains an unknown selection hash")
        if selection_hash in seen:
            raise ValueError("shell audit contains a duplicate selection hash")
        seen.add(selection_hash)
        source = sample_entries[selection_hash]
        if label.get("sample_rank") != source.get("sample_rank") or label.get("stratum") != source.get("stratum"):
            raise ValueError("shell audit rank/stratum does not match sample")
        numeric = ("human_reference_count", "ambiguous_reference_count", "tp", "fp", "fn", "opaque_reference_count")
        for key in numeric:
            if isinstance(label.get(key), bool) or not isinstance(label.get(key), int) or label[key] < 0:
                raise ValueError(f"invalid shell audit count: {key}")
        if label["human_reference_count"] != label["tp"] + label["fn"]:
            raise ValueError("shell audit invariant failed: human_reference_count != tp + fn")
        safe = {
            "sample_rank": label["sample_rank"],
            "selection_hash": selection_hash,
            "stratum": label["stratum"],
            "human_reference_count": label["human_reference_count"],
            "ambiguous_reference_count": label["ambiguous_reference_count"],
            "tp": label["tp"],
            "fp": label["fp"],
            "fn": label["fn"],
            "command_has_reference": bool(label.get("command_has_reference")),
            "command_any_recovery": bool(label.get("command_any_recovery")),
            "command_complete_recovery": bool(label.get("command_complete_recovery")),
            "parser_positive": bool(label.get("parser_positive")),
            "opaque_reference_count": label["opaque_reference_count"],
            "reason_codes": list(label.get("reason_codes", [])),
        }
        cleaned_entries.append(safe)
        strata.setdefault(safe["stratum"], []).append(safe)
    if seen != set(sample_entries):
        raise ValueError("shell audit does not cover the exact sample")

    def totals(entries: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        commands_with_refs = sum(bool(entry["command_has_reference"]) for entry in entries)
        complete = sum(
            bool(entry["command_complete_recovery"] and entry["command_has_reference"])
            for entry in entries
        )
        any_recovery = sum(
            bool(entry["command_any_recovery"] and entry["command_has_reference"])
            for entry in entries
        )
        tp = sum(int(entry["tp"]) for entry in entries)
        fp = sum(int(entry["fp"]) for entry in entries)
        fn = sum(int(entry["fn"]) for entry in entries)
        return {
            "commands": len(entries),
            "commands_with_human_reference": commands_with_refs,
            "human_reference_mentions": tp + fn,
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "ambiguous_reference_mentions": sum(int(entry["ambiguous_reference_count"]) for entry in entries),
            "opaque_reference_mentions": sum(int(entry["opaque_reference_count"]) for entry in entries),
            "mention_recall": tp / (tp + fn) if tp + fn else None,
            "mention_precision": tp / (tp + fp) if tp + fp else None,
            "commands_any_recovery": any_recovery,
            "commands_complete_recovery": complete,
            "command_any_recovery_rate": any_recovery / commands_with_refs if commands_with_refs else None,
            "command_complete_recovery_rate": complete / commands_with_refs if commands_with_refs else None,
            "command_any_recovery_wilson_95": wilson(any_recovery, commands_with_refs),
            "command_complete_recovery_wilson_95": wilson(complete, commands_with_refs),
        }

    raw = totals(cleaned_entries)
    stratum_population = sample["stratum_population_counts"]
    by_stratum = {name: totals(entries) for name, entries in sorted(strata.items())}
    weighted_tp = weighted_fp = weighted_fn = 0.0
    weighted_ref_commands = weighted_any = weighted_complete = 0.0
    for name, values in by_stratum.items():
        sampled = values["commands"]
        population = int(stratum_population.get(name, 0))
        weight = population / sampled if sampled else 0.0
        values["population_commands"] = population
        values["sample_commands"] = sampled
        values["inverse_sampling_weight"] = weight
        weighted_tp += weight * values["tp"]
        weighted_fp += weight * values["fp"]
        weighted_fn += weight * values["fn"]
        weighted_ref_commands += weight * values["commands_with_human_reference"]
        weighted_any += weight * values["commands_any_recovery"]
        weighted_complete += weight * values["commands_complete_recovery"]
    weighted = {
        "estimated_tp_mentions": weighted_tp,
        "estimated_fp_mentions": weighted_fp,
        "estimated_fn_mentions": weighted_fn,
        "mention_recall": weighted_tp / (weighted_tp + weighted_fn) if weighted_tp + weighted_fn else None,
        "mention_precision": weighted_tp / (weighted_tp + weighted_fp) if weighted_tp + weighted_fp else None,
        "estimated_commands_with_reference": weighted_ref_commands,
        "command_any_recovery_rate": weighted_any / weighted_ref_commands if weighted_ref_commands else None,
        "command_complete_recovery_rate": weighted_complete / weighted_ref_commands if weighted_ref_commands else None,
        "method": "inverse-stratum sampling weights; point estimates only because path mentions cluster within commands",
    }
    return {
        "frozen_prefix_sha256": frozen_hash,
        "sample_size": 50,
        "selection_rule": sample["selection_rule"],
        "reviewer": labels.get("reviewer"),
        "reviewed_utc": labels.get("reviewed_utc"),
        "raw_hand_checked_counts": raw,
        "by_stratum": by_stratum,
        "population_weighted_estimates": weighted,
        "interpretation": (
            "Recovery validates lexical path mentions, not successful I/O or read/write effects. "
            "Ambiguous references are excluded from recall; commands were disproportionately stratified."
        ),
        "entries": sorted(cleaned_entries, key=lambda entry: entry["sample_rank"]),
    }


UNVERIFIED = [
    "True physical byte offsets, original encodings, raw on-disk byte counts, or identity across symlinks/hardlinks/renames.",
    "Historical Git repository roots for targets that no longer resolve to an extant current .git ancestor.",
    "Actual simultaneous execution rather than co-activity within the same UTC minute.",
    "Explicit session close times or uncensored last-write-to-close claim linger.",
    "Files actually read or written by shell subprocesses, scripts, git, package managers, formatters, code generators, variables, or glob expansion.",
    "Reads through Grep/search, prompts, shared context, or other non-Read tools.",
    "That a first read caused a later write or that a claim had to remain live throughout the measured interval.",
    "Binary workload completeness when tools refused or omitted binary payloads.",
    "Event-log byte retention or byte bandwidth before the final serialized event schema is benchmarked.",
    "Sub-minute peak throughput or operation-overlap durations.",
    "The earlier approximate 5:1 shell dominance: this freeze measures 3.292:1 after deduplication and 3.012:1 over raw copied-record occurrences.",
    "The earlier 99.6% Read and 98.7% Edit/Write metadata figures as corpus-wide usable-metadata coverage; source-specific result shapes differ materially.",
    "Generalization beyond this one team, Claude Code harness, compatible-goal history, and Node-dominated workload.",
]

WHAT_CHANGES = [
    "Persist normalized repository/worktree IDs, real file IDs, encodings, raw byte ranges, and before/after hashes on every file operation.",
    "Emit structured shell/subprocess effects with expanded paths, read/write direction, success, and byte intervals.",
    "Emit explicit session start, heartbeat, close, and crash events plus operation start/end intervals.",
    "Capture result-side path/range/patch/pre-image metadata uniformly in main, direct-subagent, and workflow-subagent transcripts.",
    "Serialize and benchmark the finalized event record to convert event-count retention into bytes and sustained write bandwidth.",
    "Validate the shell parser on a larger independently reviewed sample, especially opaque inline-code, heredoc/here-string, variable, and glob cases.",
    "Replicate across teams, harnesses, languages, repository sizes, and intentionally adversarial workloads.",
]

CONFIDENCE = {
    "corpus_freeze": {
        "measurement_confidence": "High",
        "scope_confidence": "in_slice",
        "reason": "Sorted byte lengths were fixed before reads; exact prefixes and per-file/global SHA-256 values are recorded, with read/truncation/growth diagnostics.",
    },
    "event_volume": {
        "measurement_confidence": "High for paired structured events; low as total file activity",
        "scope_confidence": "workload_default_only",
        "reason": "Direct timestamps and exact active-hour denominators, but shell and non-Read channels are not typed file events.",
    },
    "read_window_sizes": {
        "measurement_confidence": "High for lines; Moderate for bytes",
        "scope_confidence": "workload_default_only",
        "reason": "Result startLine/numLines are direct; UTF-8 payload bytes are re-encoded transcript text, not disk bytes. Subagent metadata is absent.",
    },
    "edit_region_sizes": {
        "measurement_confidence": "High in exact-preimage slice; Moderate for byte interpretation",
        "scope_confidence": "workload_default_only",
        "reason": "Patches parse and old blocks validate against same-result originalFile; most successful subagent writes lack this evidence.",
    },
    "read_multiplicity": {
        "measurement_confidence": "High for localized lexical paths; Moderate as total reader fan-out",
        "scope_confidence": "workload_default_only",
        "reason": "Rolling/calendar windows and identities are deterministic, but shell/search exposures are omitted and aliases are unresolved.",
    },
    "read_to_write_intervals": {
        "measurement_confidence": "Moderate-low",
        "scope_confidence": "workload_default_only",
        "reason": "Endpoints are observed, but causality is unproven, many operations lack paths, and observed end is a censored last record rather than close.",
    },
    "observed_concurrency": {
        "measurement_confidence": "High for minute buckets; Moderate-low for true overlap/repository grouping",
        "scope_confidence": "workload_default_only",
        "reason": "Actor sets are deterministic for conflict-free post-quarantine calls; excluded ambiguous IDs may move tails. Same-minute activity is not duration overlap and historical Git-root attribution is incomplete.",
    },
    "index_cardinality": {
        "measurement_confidence": "High in localized structured slice",
        "scope_confidence": "workload_default_only",
        "reason": "First-seen files/pairs and zero-growth ISO weeks are deterministic; completeness is limited by missing structured paths and aliases.",
    },
    "capture_coverage": {
        "measurement_confidence": "Moderate-low for recovered shell paths; High for channel counts",
        "scope_confidence": "workload_default_only",
        "reason": "Shell/tool counts are exact after quarantine; lexical path recovery is conditioned on a 50-command manual audit and does not prove effects.",
    },
    "file_type_mix": {
        "measurement_confidence": "Moderate",
        "scope_confidence": "workload_default_only",
        "reason": "Mutually exclusive extension/basename rules are deterministic; binary means binary-looking path, not inspected content.",
    },
    "session_lengths": {
        "measurement_confidence": "High as observed timestamp span; Low as launch-to-close lifetime",
        "scope_confidence": "workload_default_only",
        "reason": "Spans and kept-call counts are direct after identity reconciliation and quarantine, but excluded ambiguous IDs may move call tails, idle gaps remain, and no close/crash marker exists.",
    },
    "universal_defaults": {
        "measurement_confidence": "Not verified",
        "scope_confidence": "unsupported_universal",
        "reason": "Only one team, harness, and Node-dominated workload was measured.",
    },
}


def derive_seeds(parameters: Mapping[str, Any]) -> dict[str, Any]:
    event = parameters["event_volume"]
    read = parameters["read_window_sizes"]
    edit = parameters["edit_region_sizes"]
    interval = parameters["read_to_write_intervals"]
    concurrency = parameters["observed_concurrency"]
    index = parameters["index_cardinality"]
    session = parameters["session_lengths"]
    coverage = parameters["capture_coverage"]
    read_mix = parameters["file_type_mix"]["localized_read_event_distribution"]["categories"]
    write_mix = parameters["file_type_mix"]["localized_write_event_distribution"]["categories"]
    gap7 = parameters["shell_gap_items_1_through_7"]["item_7_index_cardinality"]
    all_tool = concurrency["all_deduplicated_tool_calls_project_bucket_sensitivity"]
    git_slice = concurrency["structured_target_current_git_root"]
    return {
        "interpretation": "Observed percentile seeds for @perrepo startup; recalibrate online and retain observed maxima as diagnostics, not universal hard limits.",
        "event_log": {
            "p99_events_per_active_actor_hour": event["active_session_hour_event_count"]["p99"],
            "observed_max_events_per_active_actor_hour": event["active_session_hour_event_count"]["max"],
            "p99_aggregate_events_per_active_utc_hour": event["aggregate_utc_hour_event_count"]["p99"],
            "observed_max_aggregate_events_per_active_utc_hour": event["aggregate_utc_hour_event_count"]["max"],
            "retention_bytes": None,
            "retention_bytes_reason": "final event serialization has not been benchmarked",
        },
        "read_claim": {
            "p90_lines": read["num_lines"]["p90"],
            "p99_lines": read["num_lines"]["p99"],
            "p90_returned_utf8_bytes": read["returned_window_utf8_bytes"]["p90"],
            "p99_returned_utf8_bytes": read["returned_window_utf8_bytes"]["p99"],
        },
        "write_claim_change_block": {
            "p90_lines": edit["claim_line_span_per_change_block"]["p90"],
            "p99_lines": edit["claim_line_span_per_change_block"]["p99"],
            "p90_lf_normalized_utf8_bytes": edit["claim_lf_normalized_utf8_bytes_per_change_block"]["p90"],
            "p99_lf_normalized_utf8_bytes": edit["claim_lf_normalized_utf8_bytes_per_change_block"]["p99"],
        },
        "lease_proxy": {
            "rounded_initial_renewable_lease_seed_seconds": 3600,
            "seed_derivation": "one hour, rounded from the observed p90 first-read-to-absolute-first-write interval",
            "p90_first_read_to_absolute_first_write_seconds": interval["first_read_result_to_absolute_first_write_call_seconds"]["p90"],
            "p99_first_read_to_absolute_first_write_seconds": interval["first_read_result_to_absolute_first_write_call_seconds"]["p99"],
            "p90_last_file_write_to_observed_actor_end_seconds": interval["last_write_result_to_session_end_seconds"]["p90"],
            "warning": "long/censored tails require renewal and explicit close; do not install the linger percentile as an unattended lease",
        },
        "concurrency": {
            "p99_all_tool_logical_actors_per_active_minute": all_tool["simultaneous_sessions_per_active_utc_minute"]["p99"],
            "observed_max_all_tool_logical_actors_per_active_minute": all_tool["simultaneous_sessions_per_active_utc_minute"]["max"],
            "p99_same_claude_project_bucket_proxy_all_calls": all_tool["simultaneous_sessions_per_active_repo_minute"]["p99"],
            "observed_max_same_claude_project_bucket_proxy_all_calls": all_tool["simultaneous_sessions_per_active_repo_minute"]["max"],
            "p99_same_current_git_root_in_resolved_structured_slice": git_slice["simultaneous_sessions_per_active_repo_minute"]["p99"],
            "observed_max_same_current_git_root_in_resolved_structured_slice": git_slice["simultaneous_sessions_per_active_repo_minute"]["max"],
            "current_git_root_structured_attribution_coverage": concurrency["current_git_root_attribution"],
        },
        "read_index": {
            "structured_seed_distinct_files": index["distinct_files_ever_read"],
            "structured_seed_file_actor_pairs": index["distinct_file_session_pairs"],
            "p99_new_files_per_iso_week": index["new_distinct_files_per_iso_week"]["p99"],
            "p99_new_file_actor_pairs_per_iso_week": index["new_file_session_pairs_per_iso_week"]["p99"],
            "all_shell_mentions_as_reads_candidate_paths": gap7["union_if_all_recovered_shell_mentions_were_reads_distinct_paths"],
            "all_shell_mentions_as_reads_candidate_path_actor_pairs": gap7["union_if_all_recovered_shell_mentions_were_reads_path_session_pairs"],
        },
        "capture_adapter": {
            "shell_to_structured_file_tool_call_ratio": coverage["shell_channel_calls_to_structured_file_tool_calls"]["ratio"],
            "weighted_shell_parser_mention_recall": coverage["shell_parser_hand_validation"]["population_weighted_estimates"]["mention_recall"],
            "weighted_shell_parser_mention_precision": coverage["shell_parser_hand_validation"]["population_weighted_estimates"]["mention_precision"],
            "mechanical_claims_from_lexical_shell_mentions": False,
        },
        "format_fast_path": {
            "source_plus_markdown_localized_read_percent": read_mix["source"]["percent"] + read_mix["markdown"]["percent"],
            "source_plus_markdown_localized_write_percent": write_mix["source"]["percent"] + write_mix["markdown"]["percent"],
            "observed_lock_read_write_events": read_mix["lock"]["count"] + write_mix["lock"]["count"],
            "observed_binary_read_write_events": read_mix["binary"]["count"] + write_mix["binary"]["count"],
        },
        "one_shot_dispatch": {
            "p99_all_tool_calls_per_actor": session["all_tool_calls_per_actor"]["p99"],
            "observed_max_all_tool_calls_per_actor": session["all_tool_calls_per_actor"]["max"],
            "p99_successful_structured_events_per_core_active_actor": session["successful_read_edit_write_events_per_core_active_actor"]["p99"],
            "observed_max_successful_structured_events_per_core_active_actor": session["successful_read_edit_write_events_per_core_active_actor"]["max"],
            "p99_observed_actor_wall_clock_seconds": session["wall_clock_seconds_first_to_last_timestamped_record"]["p99"],
            "warning": "observed wall clock includes idle gaps and is not a dispatch timeout or close boundary",
        },
    }


def parameter_table_rows(parameters: Mapping[str, Any], validation: Mapping[str, Any]) -> list[tuple[str, str, str, str]]:
    event = parameters["event_volume"]
    read = parameters["read_window_sizes"]
    edit = parameters["edit_region_sizes"]
    mult = parameters["read_multiplicity"]
    interval = parameters["read_to_write_intervals"]
    conc = parameters["observed_concurrency"]
    index = parameters["index_cardinality"]
    coverage = parameters["capture_coverage"]
    session = parameters["session_lengths"]
    gap = parameters["shell_gap_items_1_through_7"]
    weighted = validation["population_weighted_estimates"]
    all_tool = conc["all_deduplicated_tool_calls_project_bucket_sensitivity"]
    git_slice = conc["structured_target_current_git_root"]
    mix_r = parameters["file_type_mix"]["localized_read_event_distribution"]
    mix_w = parameters["file_type_mix"]["localized_write_event_distribution"]
    return [
        (
            "1. Event volume",
            f"{fmt_number(event['successful_deduplicated_structured_events'])} successful events; active actor-hour {fmt_dist(event['active_session_hour_event_count'])}; aggregate active UTC hour {fmt_dist(event['aggregate_utc_hour_event_count'])}",
            "Event-log write rate and count retention",
            f"{fmt_number(gap['item_1_event_volume']['untyped_shell_commands_relative_to_structured_events']['numerator'])} shell commands ({fmt_percent(gap['item_1_event_volume']['untyped_shell_commands_relative_to_structured_events']['percent'])} of structured-event count) are untyped effects.",
        ),
        (
            "2. Read windows",
            f"lines {fmt_dist(read['num_lines'])}; returned UTF-8 bytes {fmt_dist(read['returned_window_utf8_bytes'])}",
            "Default read-claim region granularity",
            f"Only {fmt_number(read['localized_valid_window_denominator'])}/{fmt_number(read['successful_read_denominator'])} successful reads localized after all-channel dedup; {fmt_number(gap['item_2_read_windows']['shell_canonical_path_mentions_without_line_windows'])} shell mentions have no line window.",
        ),
        (
            "3. Edit regions",
            f"change-block lines {fmt_dist(edit['claim_line_span_per_change_block'])}; LF-normalized UTF-8 bytes {fmt_dist(edit['claim_lf_normalized_utf8_bytes_per_change_block'])}",
            "Write-claim granularity and escalation region",
            f"Exact-preimage population is {fmt_number(edit['exact_preimage_validated_patch_write_denominator'])}/{fmt_number(edit['successful_edit_write_denominator'])} writes; shell write/ambiguous commands have no patch spans.",
        ),
        (
            "4. Read multiplicity",
            f"rolling 24 h {fmt_dist(mult['per_file_rolling_24h_max_distinct_sessions'])}; rolling 7 d {fmt_dist(mult['per_file_rolling_7d_max_distinct_sessions'])}",
            "Inverted-index hot-key fan-out",
            f"Treating every recovered shell mention as a read raises the 7-day candidate maximum by up to {fmt_number(gap['item_4_read_multiplicity']['maximum_rolling_7d_absolute_lift'])} actors; this is an exposure sensitivity, not observed reads.",
        ),
        (
            "5. Read→write / linger",
            f"first read→absolute first write {fmt_duration_dist(interval['first_read_result_to_absolute_first_write_call_seconds'])}; last file write→observed end {fmt_duration_dist(interval['last_write_result_to_session_end_seconds'])}",
            "Lease/renewal and release-at-close behavior",
            f"No close event exists; {fmt_number(gap['item_5_read_write_intervals']['literal_pairs_with_any_same_path_shell_mention']['numerator'])}/{fmt_number(gap['item_5_read_write_intervals']['literal_pairs_with_any_same_path_shell_mention']['denominator'])} eligible pairs also have same-path shell mentions.",
        ),
        (
            "6. Observed concurrency",
            f"all-tool logical actors/min {fmt_dist(all_tool['simultaneous_sessions_per_active_utc_minute'])}; same Claude project-bucket proxy {fmt_dist(all_tool['simultaneous_sessions_per_active_repo_minute'])}; same current Git root in resolved structured slice {fmt_dist(git_slice['simultaneous_sessions_per_active_repo_minute'])}",
            "Shim actor concurrency and per-repository contention",
            f"Minute co-activity is not true overlap; {fmt_percent(conc['global_tool_id_group_quarantine']['all_tool_calls']['excluded_percent'])} of eligible call-ID groups were quarantined; current Git-root attribution covers {fmt_number(conc['current_git_root_attribution']['numerator'])}/{fmt_number(conc['current_git_root_attribution']['denominator'])} structured events and project-bucket results are only a proxy.",
        ),
        (
            "7. Index cardinality",
            f"{fmt_number(index['distinct_files_ever_read'])} files; {fmt_number(index['distinct_file_session_pairs'])} file/actor pairs; weekly new files {fmt_dist(index['new_distinct_files_per_iso_week'])}",
            "Initial index memory and weekly growth headroom",
            f"All shell mentions-as-reads sensitivity reaches {fmt_number(gap['item_7_index_cardinality']['union_if_all_recovered_shell_mentions_were_reads_distinct_paths'])} candidate paths and {fmt_number(gap['item_7_index_cardinality']['union_if_all_recovered_shell_mentions_were_reads_path_session_pairs'])} candidate pairs.",
        ),
        (
            "8. Capture coverage",
            f"{fmt_number(coverage['deduplicated_shell_commands'])} shell vs {fmt_number(coverage['deduplicated_structured_file_tool_calls'])} structured calls ({coverage['shell_channel_calls_to_structured_file_tool_calls']['ratio']:.2f}:1); mentions/parser-positive command {fmt_dist(coverage['mentions_per_parser_positive_command'])}; weighted parser recall {fmt_percent(100 * weighted['mention_recall'] if weighted['mention_recall'] is not None else None)}",
            "Instrumentation priority and shell adapter budget",
            "Recovered text is a lexical mention, not a successful read/write; 50-command audit is stratified and small.",
        ),
        (
            "9. File-type mix",
            f"{fmt_number(mix_r['denominator'])} localized read events / {fmt_number(mix_w['denominator'])} localized writes across source/config/markdown/json/lock/binary/other",
            "Fast-path formats versus tolerated formats",
            "Extension/basename classifier; binary means binary-looking extension, not inspected content.",
        ),
        (
            "10. Session length",
            f"all tool calls/actor {fmt_dist(session['all_tool_calls_per_actor'])}; observed wall clock {fmt_duration_dist(session['wall_clock_seconds_first_to_last_timestamped_record'])}",
            "One-shot dispatch payload and statelessness envelope",
            f"Observed span includes idle gaps and has no launch/close/crash boundary; raw parent session and logical actor populations differ; {fmt_percent(session['global_tool_id_group_quarantine']['all_tool_calls']['excluded_percent'])} of eligible call-ID groups were quarantined.",
        ),
    ]


def render_report(final: Mapping[str, Any]) -> str:
    p = final["parameters"]
    corpus = final["corpus"]
    validation = p["capture_coverage"]["shell_parser_hand_validation"]
    rows = parameter_table_rows(p, validation)
    lines: list[str] = [
        "# Operational build parameters",
        "",
        "**Scope up front:** one team, one Claude Code harness, and a Node-dominated workload. These are observed seeds for `@perrepo` self-calibration, not universal constants. The corpus was opened read-only; no file contents or shell commands are reproduced.",
        "",
        "## Parameter table",
        "",
        "| Name | Value / distribution | What it sizes | Blind spot |",
        "|---|---|---|---|",
    ]
    for name, value, sizes, blind in rows:
        lines.append(f"| {name} | {value} | {sizes} | {blind} |")

    diagnostics = corpus["diagnostics"]
    if "frozen_manifest_files_resolved" in diagnostics:
        freeze_tail = (
            f"frozen manifest prefixes resolved={fmt_number(diagnostics.get('frozen_manifest_files_resolved'))}; "
            f"live JSONL files outside the frozen manifest at rescan={fmt_number(diagnostics.get('live_files_outside_frozen_manifest'))}"
        )
    else:
        freeze_tail = (
            "files added between enumeration and the post-scan check="
            f"{fmt_number(diagnostics.get('files_added_after_snapshot', 0))}"
        )
    lines.extend(
        [
            "",
            "## Corpus freeze and measurement contract",
            "",
            f"The final freeze fixed **{fmt_number(corpus['corpus_file_count'])} JSONL byte prefixes** totaling **{fmt_number(corpus['corpus_bytes'])} bytes** at `{corpus['snapshot_utc']}`. The frozen-prefix SHA-256 is `{corpus['corpus_snapshot_sha256']}`. All {fmt_number(diagnostics.get('files_read_successfully', 0))} prefixes were read; read failures={fmt_number(diagnostics.get('files_read_failed', 0))}, truncations={fmt_number(diagnostics.get('files_truncated_after_snapshot', 0))}, malformed JSONL lines={fmt_number(diagnostics.get('malformed_jsonl_lines', 0))}, and {freeze_tail}.",
            "",
            "The freeze hashes ordinal, frozen byte length, and exact prefix bytes, matching the oscillation study. It does not hash path names and is not a transactional filesystem snapshot: a same-length concurrent mutation could produce a mixed-time view, although the digest commits exactly to the bytes read. Percentiles are observed nearest-rank values (`x[ceil(p*n)-1]`); every `n` below is its denominator. UTC day/week/hour/minute buckets are half-open, and ISO weeks begin Monday.",
            "",
            "Calls and results are paired source-locally by tool-use ID, then copied IDs are reconciled globally; conflicts are quarantined. The coordination identity is `agentId` when nonempty, otherwise `sessionId`. Structured path metrics use result-side paths only. Repository concurrency uses current extant `.git` ancestors where resolvable and labels Claude project-directory grouping separately as a proxy.",
            "",
            "## Seed defaults for `@perrepo` self-calibration",
            "",
            "These are percentile seeds, not safety limits. Long tails should renew or escalate; observed maxima should remain diagnostics.",
            "",
        ]
    )
    seeds = final["recommended_seeds"]
    lines.extend(
        [
            f"- Event log: p99 **{fmt_number(seeds['event_log']['p99_events_per_active_actor_hour'])} events/active actor-hour** and **{fmt_number(seeds['event_log']['p99_aggregate_events_per_active_utc_hour'])} aggregate events/active UTC hour**; observed maxima {fmt_number(seeds['event_log']['observed_max_events_per_active_actor_hour'])} and {fmt_number(seeds['event_log']['observed_max_aggregate_events_per_active_utc_hour'])}. Byte retention remains unverified until the final event record is serialized.",
            "",
            f"- Read claim: seed p90 **{fmt_number(seeds['read_claim']['p90_lines'])} lines / {fmt_number(seeds['read_claim']['p90_returned_utf8_bytes'])} returned UTF-8 bytes**; retain p99 **{fmt_number(seeds['read_claim']['p99_lines'])} lines / {fmt_number(seeds['read_claim']['p99_returned_utf8_bytes'])} bytes** as escalation/tail telemetry.",
            "",
            f"- Write claim change block: seed p90 **{fmt_number(seeds['write_claim_change_block']['p90_lines'])} lines / {fmt_number(seeds['write_claim_change_block']['p90_lf_normalized_utf8_bytes'])} LF-normalized UTF-8 bytes**; p99 is **{fmt_number(seeds['write_claim_change_block']['p99_lines'])} lines / {fmt_number(seeds['write_claim_change_block']['p99_lf_normalized_utf8_bytes'])} bytes**.",
            "",
            f"- Lease proxy: use **{human_duration(seeds['lease_proxy']['rounded_initial_renewable_lease_seed_seconds'])} renewable** as the initial self-calibration seed, rounded from the observed p90 first-read→first-write interval of **{human_duration(seeds['lease_proxy']['p90_first_read_to_absolute_first_write_seconds'])}**; p99 is **{human_duration(seeds['lease_proxy']['p99_first_read_to_absolute_first_write_seconds'])}**. The p90 last-file-write→observed-end tail is **{human_duration(seeds['lease_proxy']['p90_last_file_write_to_observed_actor_end_seconds'])}**; require renewal/heartbeat and explicit close rather than installing that censored tail as an unattended lease.",
            "",
            f"- Concurrency: global p99/max is **{fmt_number(seeds['concurrency']['p99_all_tool_logical_actors_per_active_minute'])}/{fmt_number(seeds['concurrency']['observed_max_all_tool_logical_actors_per_active_minute'])} logical actors/active minute**. For an initial `@perrepo` seed, the all-call Claude project-bucket proxy is p99/max **{fmt_number(seeds['concurrency']['p99_same_claude_project_bucket_proxy_all_calls'])}/{fmt_number(seeds['concurrency']['observed_max_same_claude_project_bucket_proxy_all_calls'])}**. Keep the current-Git-root resolved structured slice p99/max **{fmt_number(seeds['concurrency']['p99_same_current_git_root_in_resolved_structured_slice'])}/{fmt_number(seeds['concurrency']['observed_max_same_current_git_root_in_resolved_structured_slice'])}** as diagnostics only because attribution covers {fmt_number(seeds['concurrency']['current_git_root_structured_attribution_coverage']['numerator'])}/{fmt_number(seeds['concurrency']['current_git_root_structured_attribution_coverage']['denominator'])} events.",
            "",
            f"- Read index: structured seed **{fmt_number(seeds['read_index']['structured_seed_distinct_files'])} files / {fmt_number(seeds['read_index']['structured_seed_file_actor_pairs'])} file-actor pairs**, with p99 weekly increments **{fmt_number(seeds['read_index']['p99_new_files_per_iso_week'])}/{fmt_number(seeds['read_index']['p99_new_file_actor_pairs_per_iso_week'])}**. The all-shell-mentions-as-reads sensitivity is {fmt_number(seeds['read_index']['all_shell_mentions_as_reads_candidate_paths'])} candidate paths / {fmt_number(seeds['read_index']['all_shell_mentions_as_reads_candidate_path_actor_pairs'])} pairs.",
            "",
            f"- Capture adapter: shell calls are **{seeds['capture_adapter']['shell_to_structured_file_tool_call_ratio']:.3f}:1** versus structured file-tool calls; the audited lexical parser's weighted recall/precision is only **{fmt_percent(100 * seeds['capture_adapter']['weighted_shell_parser_mention_recall'])}/{fmt_percent(100 * seeds['capture_adapter']['weighted_shell_parser_mention_precision'])}**, so mentions must not create mechanical claims.",
            "",
            f"- Format fast path: source plus Markdown account for **{fmt_percent(seeds['format_fast_path']['source_plus_markdown_localized_read_percent'])}** of localized reads and **{fmt_percent(seeds['format_fast_path']['source_plus_markdown_localized_write_percent'])}** of localized writes. Lock and binary events were both zero in the localized slice; tolerate them, but do not infer they are absent from shell activity.",
            "",
            f"- One-shot dispatch: provision the p99 envelope at **{fmt_number(seeds['one_shot_dispatch']['p99_all_tool_calls_per_actor'])} all-tool calls/actor** and **{fmt_number(seeds['one_shot_dispatch']['p99_successful_structured_events_per_core_active_actor'])} successful structured events/core-active actor**; observed maxima are {fmt_number(seeds['one_shot_dispatch']['observed_max_all_tool_calls_per_actor'])}/{fmt_number(seeds['one_shot_dispatch']['observed_max_successful_structured_events_per_core_active_actor'])}. The p99 observed wall span is {human_duration(seeds['one_shot_dispatch']['p99_observed_actor_wall_clock_seconds'])}, but includes idle time and is not a timeout default.",
        ]
    )

    ev = p["event_volume"]
    gap = p["shell_gap_items_1_through_7"]
    quarantine = ev["global_tool_id_group_quarantine"]
    lines.extend(
        [
            "",
            "## 1. Event volume",
            "",
            f"Corpus total: **{fmt_number(ev['successful_deduplicated_structured_events'])}** successful Read/Edit/Write results: Read={fmt_number(ev['by_tool'].get('Read', 0))}, Edit={fmt_number(ev['by_tool'].get('Edit', 0))}, Write={fmt_number(ev['by_tool'].get('Write', 0))}. Calls={fmt_number(ev['deduplicated_structured_calls'])}; result-linked operations including errors={fmt_number(ev['result_linked_core_operations_including_errors'])}; failed results={fmt_number(ev['failed_result_linked_structured_events'])}.",
            "",
            f"Global-ID quarantine diagnostics: operation session-identity conflicts={fmt_number(diagnostics.get('operation_dedup_session_identity_conflicts', 0))}, operation metadata conflicts={fmt_number(diagnostics.get('operation_dedup_metadata_conflicts', 0))}, call session-identity conflicts={fmt_number(diagnostics.get('call_dedup_session_identity_conflicts', 0))}, and call tool/time conflicts={fmt_number(diagnostics.get('call_dedup_tool_or_time_conflicts', 0))}. These are excluded, not guessed or double-counted.",
            "",
            f"Post-quarantine tool-ID groups kept/excluded: all calls {fmt_number(quarantine['all_tool_calls']['kept_groups'])}/{fmt_number(quarantine['all_tool_calls']['excluded_ambiguous_groups'])} of {fmt_number(quarantine['all_tool_calls']['eligible_tool_id_groups'])} ({fmt_percent(quarantine['all_tool_calls']['excluded_percent'])} excluded); result-linked operations {fmt_number(quarantine['result_linked_operations']['kept_groups'])}/{fmt_number(quarantine['result_linked_operations']['excluded_ambiguous_groups'])} of {fmt_number(quarantine['result_linked_operations']['eligible_tool_id_groups'])} ({fmt_percent(quarantine['result_linked_operations']['excluded_percent'])} excluded); shell commands {fmt_number(quarantine['shell_commands']['kept_groups'])}/{fmt_number(quarantine['shell_commands']['excluded_ambiguous_groups'])} of {fmt_number(quarantine['shell_commands']['eligible_tool_id_groups'])} ({fmt_percent(quarantine['shell_commands']['excluded_percent'])} excluded). All distributions are conditional on the kept slice.",
            "",
            f"Active logical-actor/UTC-hour buckets: {fmt_dist(ev['active_session_hour_event_count'])}. Aggregate active UTC hours: {fmt_dist(ev['aggregate_utc_hour_event_count'])}. Per-tool active actor-hours: Read {fmt_dist(ev['active_session_hour_by_tool']['Read'])}; Edit {fmt_dist(ev['active_session_hour_by_tool']['Edit'])}; Write {fmt_dist(ev['active_session_hour_by_tool']['Write'])}. Combined write-family: {fmt_dist(ev['active_session_hour_by_family']['write'])}.",
            "",
            f"**Shell-channel qualification:** {fmt_number(gap['item_1_event_volume']['untyped_shell_commands_relative_to_structured_events']['numerator'])} deduplicated shell commands equal {fmt_percent(gap['item_1_event_volume']['untyped_shell_commands_relative_to_structured_events']['percent'])} of the structured-event count; parser-positive shell commands equal {fmt_percent(gap['item_1_event_volume']['parser_positive_shell_commands_relative_to_structured_events']['percent'])}. They cannot be added as events because direction, effect, and success are unknown. The event count sizes count throughput only, not serialized bytes.",
        ]
    )

    read = p["read_window_sizes"]
    lines.extend(
        [
            "",
            "## 2. Read window sizes",
            "",
            f"Eligible localized reads: **{fmt_number(read['localized_valid_window_denominator'])}/{fmt_number(read['successful_read_denominator'])}** ({fmt_percent(read['localized_coverage']['percent'])}). `startLine`: {fmt_dist(read['start_line'], 'one-based lines')}. `numLines`: {fmt_dist(read['num_lines'], 'logical lines')}.",
            "",
            f"Same-result returned UTF-8 payload bytes: {fmt_dist(read['returned_window_utf8_bytes'], 'bytes')}; bytes/declared line: {fmt_dist(read['returned_utf8_bytes_per_line'], 'bytes/line')}. The byte denominator is every localized read with a same-result content string: {fmt_number(read['returned_content_byte_denominator'])}/{fmt_number(read['localized_valid_window_denominator'])}. Under the tool's observed terminal-separator-as-empty-line convention, {fmt_number(read['returned_content_line_count_matches_metadata']['numerator'])}/{fmt_number(read['returned_content_line_count_matches_metadata']['denominator'])} strings align with metadata; the aligned-only byte sensitivity is {fmt_dist(read['returned_window_utf8_bytes_metadata_line_count_aligned'], 'bytes')}. Alignment is diagnostic and does not gate the primary bytes. These are transcript-decoded payload bytes, not physical disk bytes or encoding/line-ending truth. Another event's `originalFile` was not joined to a read because state can change.",
            "",
            f"**Shell-channel qualification:** {fmt_number(gap['item_2_read_windows']['shell_canonical_path_mentions_without_line_windows'])} canonical shell path mentions, or {fmt_percent(gap['item_2_read_windows']['shell_mentions_relative_to_read_windows']['percent'])} of the structured-window count, have no recoverable line/byte window; {fmt_number(gap['item_2_read_windows']['heuristic_read_or_read_write_shell_commands_with_canonical_paths'])} commands were heuristically read-like/mixed. Neither quantity can repair the distribution.",
        ]
    )

    edit = p["edit_region_sizes"]
    lines.extend(
        [
            "",
            "## 3. Edit region sizes",
            "",
            f"Successful Edit/Write denominator={fmt_number(edit['successful_edit_write_denominator'])}; localized writes={fmt_number(edit['localized_write_denominator'])}; valid nonempty patches before pre-image validation={fmt_number(edit['valid_nonempty_patch_write_denominator_before_preimage_validation'])}; exact string-preimage/applying patches={fmt_number(edit['exact_preimage_validated_patch_write_denominator'])}. The exact slice contains {fmt_number(edit['raw_structured_patch_hunk_denominator'])} raw hunks and {fmt_number(edit['parsed_change_block_denominator'])} contiguous `+/-` change blocks.",
            "",
            f"Raw hunk declared old lines (including context): {fmt_dist(edit['declared_old_lines_per_raw_hunk_including_context'])}; declared new lines: {fmt_dist(edit['declared_new_lines_per_raw_hunk_including_context'])}; exact transcript-UTF-8 old span: {fmt_dist(edit['transcript_utf8_old_span_bytes_per_raw_hunk'], 'bytes')}.",
            "",
            f"Parsed change-block removed lines: {fmt_dist(edit['removed_line_count_per_change_block'])}; added lines: {fmt_dist(edit['added_line_count_per_change_block'])}; claim span `max(removed,added)`: {fmt_dist(edit['claim_line_span_per_change_block'])}. LF-normalized UTF-8 claim bytes: {fmt_dist(edit['claim_lf_normalized_utf8_bytes_per_change_block'], 'bytes')}. Pure insertions={fmt_number(edit['pure_insertion_change_blocks']['numerator'])}/{fmt_number(edit['pure_insertion_change_blocks']['denominator'])}; pure deletions={fmt_number(edit['pure_deletion_change_blocks']['numerator'])}/{fmt_number(edit['pure_deletion_change_blocks']['denominator'])}. Insertions remain zero-width old-side anchors.",
            "",
            f"Per exact write: change blocks {fmt_dist(edit['hunks_per_write'])}; aggregate claim lines {fmt_dist(edit['aggregate_claim_lines_per_write'])}; aggregate claim bytes {fmt_dist(edit['aggregate_claim_bytes_per_write'], 'bytes')}. Full Write creates with null pre-image/empty patch={fmt_number(edit['write_creates_without_patch'])}; result-content size {fmt_dist(edit['write_create_result_utf8_bytes'], 'bytes')}.",
            "",
            f"**Shell-channel qualification:** structured exact patch blocks={fmt_number(gap['item_3_edit_regions']['structured_patch_hunks'])}; heuristic shell write/mixed commands with canonical paths={fmt_number(gap['item_3_edit_regions']['shell_write_or_read_write_commands_with_canonical_paths'])}; ambiguous canonical-path commands={fmt_number(gap['item_3_edit_regions']['ambiguous_shell_commands_with_canonical_paths'])}. Shell commands supply no trusted hunks, pre-images, or byte spans.",
        ]
    )

    mult = p["read_multiplicity"]
    lines.extend(
        [
            "",
            "## 4. Read multiplicity",
            "",
            f"One observation per read file: rolling 24-hour maximum {fmt_dist(mult['per_file_rolling_24h_max_distinct_sessions'])}; rolling 7-day maximum {fmt_dist(mult['per_file_rolling_7d_max_distinct_sessions'])}; denominator={fmt_number(mult['distinct_file_denominator'])} files. Calendar nonempty file/day cells: {fmt_dist(mult['distinct_sessions_per_file_utc_day'])}; file/ISO-week cells: {fmt_dist(mult['distinct_sessions_per_file_iso_week'])}.",
            "",
            "Top 20 rank by rolling 7-day maximum, then rolling 24-hour maximum, lifetime actors, read-event count, and normalized path:",
            "",
            "| # | Redacted path | Category | 24 h max | 7 d max | Lifetime actors | Read events |",
            "|---:|---|---|---:|---:|---:|---:|",
        ]
    )
    for row in mult["top_20_hottest_files"]:
        safe_path = str(row["path"]).replace("|", "\\|")
        lines.append(
            f"| {row['rank']} | `{safe_path}` | {row['category']} | {fmt_number(row['rolling_24h_max_distinct_sessions'])} | {fmt_number(row['rolling_7d_max_distinct_sessions'])} | {fmt_number(row['all_time_distinct_sessions'])} | {fmt_number(row['read_events'])} |"
        )
    g4 = gap["item_4_read_multiplicity"]
    lines.extend(
        [
            "",
            f"Credential-bearing paths redacted={fmt_number(mult['credential_redacted_top_path_count'])}/20. **Shell-channel qualification:** if every recovered shell mention is treated as a possible read, {fmt_number(g4['paths_whose_rolling_24h_max_increases'])} path keys rise in rolling-24-hour maximum and {fmt_number(g4['paths_whose_rolling_7d_max_increases'])} rise in rolling-7-day maximum; maximum absolute lifts are {fmt_number(g4['maximum_rolling_24h_absolute_lift'])} and {fmt_number(g4['maximum_rolling_7d_absolute_lift'])} actors. This is a noisy all-recovered-mentions scenario, neither an upper nor lower bound on total shell exposure, and includes directories/non-effects.",
        ]
    )

    interval = p["read_to_write_intervals"]
    g5 = gap["item_5_read_write_intervals"]
    lines.extend(
        [
            "",
            "## 5. Read-to-write intervals and claim linger",
            "",
            f"Pairs with both operations={fmt_number(interval['session_file_pairs_with_read_and_write'])}; literal eligible pairs whose absolute first write was not before the first read={fmt_number(interval['literal_pairs_first_write_at_or_after_first_read'])}; first-write-before-first-read pairs={fmt_number(interval['pairs_first_write_before_first_read'])}. Literal first-read result→absolute first-write call: {fmt_duration_dist(interval['first_read_result_to_absolute_first_write_call_seconds'])}. The separately labeled first-subsequent-write sensitivity is {fmt_duration_dist(interval['first_read_result_to_first_following_write_call_seconds_sensitivity'])}.",
            "",
            f"One linger value per actor/file claim: {fmt_duration_dist(interval['last_write_result_to_session_end_seconds'])}; actor-level final write→observed end: {fmt_duration_dist(interval['last_write_of_any_file_to_session_end_seconds_per_actor'])}. Observed end is the last timestamped record, not a close event; pauses count and live/crashed sessions are right-censored.",
            "",
            f"**Shell-channel qualification:** same-path shell mentions touch {fmt_number(g5['literal_pairs_with_any_same_path_shell_mention']['numerator'])}/{fmt_number(g5['literal_pairs_with_any_same_path_shell_mention']['denominator'])} literal eligible pairs; before first read={fmt_number(g5['literal_pairs_with_same_path_shell_mention_before_first_read'])}, between read/write={fmt_number(g5['literal_pairs_with_same_path_shell_mention_between_read_and_write'])}, after first write={fmt_number(g5['literal_pairs_with_same_path_shell_mention_after_first_write'])}. The heuristic structured+shell read/write union yields {fmt_number(g5['heuristic_shell_read_write_union_eligible_pairs'])} candidate pairs with {fmt_duration_dist(g5['heuristic_union_interval_seconds'])}, but shell intent is not effect evidence.",
        ]
    )

    conc = p["observed_concurrency"]
    structured = conc["structured_successful_read_edit_write_project_bucket_proxy"]
    all_tool = conc["all_deduplicated_tool_calls_project_bucket_sensitivity"]
    git_slice = conc["structured_target_current_git_root"]
    raw_session = conc["raw_session_id_project_bucket_proxy"]
    shell_delta = gap["item_6_concurrency"][
        "paired_actor_count_delta_on_structured_plus_shell_minute_support"
    ]
    lines.extend(
        [
            "",
            "## 6. Observed concurrency",
            "",
            f"Logical actors with successful structured invocations per active UTC minute: {fmt_dist(structured['simultaneous_sessions_per_active_utc_minute'])}; all deduplicated tool calls: {fmt_dist(all_tool['simultaneous_sessions_per_active_utc_minute'])}. Raw parent `sessionId` structured sensitivity: {fmt_dist(raw_session['simultaneous_sessions_per_active_utc_minute'])}. Primary identity remains `agentId` else `sessionId`, so parent-session aggregation does not hide sideagents.",
            "",
            f"Same current Git root, only where the structured target resolved at `{conc['current_filesystem_lookup_utc']}`: {fmt_dist(git_slice['simultaneous_sessions_per_active_repo_minute'])} over {fmt_number(git_slice['active_repo_minute_denominator'])} active repo-minutes. Attribution coverage={fmt_number(conc['current_git_root_attribution']['numerator'])}/{fmt_number(conc['current_git_root_attribution']['denominator'])} ({fmt_percent(conc['current_git_root_attribution']['percent'])}); unresolved event targets={fmt_number(conc['current_git_root_unknown_event_count'])}. Claude project-bucket proxy same-project actor counts are {fmt_dist(all_tool['simultaneous_sessions_per_active_repo_minute'])} for all calls.",
            "",
            f"**Shell-channel qualification:** on the union of structured-plus-shell active minutes, the shell-attributable actor-count delta is {fmt_dist(shell_delta['global_actor_delta_per_expanded_active_utc_minute'])}; positive in {fmt_number(shell_delta['global_minutes_with_positive_actor_delta']['numerator'])}/{fmt_number(shell_delta['global_minutes_with_positive_actor_delta']['denominator'])} minutes. On same-project-bucket minute cells it is {fmt_dist(shell_delta['same_project_actor_delta_per_expanded_active_repo_minute'])}; positive in {fmt_number(shell_delta['repo_minutes_with_positive_actor_delta']['numerator'])}/{fmt_number(shell_delta['repo_minutes_with_positive_actor_delta']['denominator'])} cells. This uses timestamped shell calls, not command effects, non-tool thinking time, or duration overlap.",
        ]
    )

    index = p["index_cardinality"]
    g7 = gap["item_7_index_cardinality"]
    lines.extend(
        [
            "",
            "## 7. Index cardinality",
            "",
            f"Structured localized slice: **{fmt_number(index['distinct_files_ever_read'])} distinct files** and **{fmt_number(index['distinct_file_session_pairs'])} distinct file/logical-actor pairs**. Weekly new files: {fmt_dist(index['new_distinct_files_per_iso_week'])}; weekly new pairs: {fmt_dist(index['new_file_session_pairs_per_iso_week'])}. Denominator={fmt_number(index['growth_week_denominator_including_zero_growth_weeks'])} ISO weeks including leading, trailing, and internal zero-growth weeks across the frozen timestamp span; boundary weeks are partial calendar coverage. Span rule: {index['growth_time_span_definition']}.",
            "",
            "| ISO week | New files | Cumulative files | New file/actor pairs | Cumulative pairs |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for row in index["weekly_growth"]:
        lines.append(
            f"| {row['iso_week']} | {fmt_number(row['new_distinct_files'])} | {fmt_number(row['cumulative_distinct_files'])} | {fmt_number(row['new_file_session_pairs'])} | {fmt_number(row['cumulative_file_session_pairs'])} |"
        )
    lines.extend(
        [
            "",
            f"**Shell-channel qualification:** treating every canonical shell mention as a possible read adds {fmt_number(g7['shell_only_distinct_paths'])} candidate path keys and {fmt_number(g7['shell_only_path_session_pairs'])} candidate path/actor pairs, producing union totals {fmt_number(g7['union_if_all_recovered_shell_mentions_were_reads_distinct_paths'])}/{fmt_number(g7['union_if_all_recovered_shell_mentions_were_reads_path_session_pairs'])}; lifts are {fmt_percent(g7['candidate_path_cardinality_lift']['percent'])}/{fmt_percent(g7['candidate_path_session_pair_cardinality_lift']['percent'])}. These are possible endpoints, not proven reads or necessarily files.",
        ]
    )

    coverage = p["capture_coverage"]
    raw_validation = validation["raw_hand_checked_counts"]
    weighted = validation["population_weighted_estimates"]
    stratum_denominators = "; ".join(
        f"{name} population/sample={fmt_number(validation['by_stratum'][name]['population_commands'])}/{fmt_number(validation['by_stratum'][name]['sample_commands'])}"
        for name in ("Bash_positive", "Bash_negative", "PowerShell_positive", "PowerShell_negative")
    )
    lines.extend(
        [
            "",
            "## 8. Capture coverage",
            "",
            f"Deduplicated channel calls: Bash/PowerShell={fmt_number(coverage['deduplicated_shell_commands'])}; structured Read/Edit/Write={fmt_number(coverage['deduplicated_structured_file_tool_calls'])}; ratio **{coverage['shell_channel_calls_to_structured_file_tool_calls']['ratio']:.3f}:1**. Raw copied-record occurrences are {fmt_number(coverage['raw_channel_occurrences']['shell_commands_with_string'])}/{fmt_number(coverage['raw_channel_occurrences']['structured_read_edit_write_calls'])}, or **{coverage['raw_channel_occurrences']['shell_to_structured_ratio']:.3f}:1**. Thus the earlier approximate 5:1 dominance was not reproduced under either disclosed denominator. Commands with any parser path mention={fmt_number(coverage['commands_with_any_parser_path_mention'])}; commands with a canonical non-pattern path={fmt_number(coverage['commands_with_canonical_non_pattern_path'])}; canonical `(command,path)` mentions={fmt_number(coverage['canonical_non_pattern_path_mentions'])}; mentions/parser-positive command {fmt_dist(coverage['mentions_per_parser_positive_command'])}; unresolved/glob mentions={fmt_number(coverage['unresolved_or_pattern_path_mentions'])}; distinct canonical shell paths={fmt_number(coverage['distinct_canonical_shell_paths'])}; distinct shell path/actor pairs={fmt_number(coverage['distinct_canonical_shell_path_actor_pairs'])}.",
            "",
            f"Parser method: {coverage['parser_description']}. Intent classification is heuristic and never treated as filesystem-effect proof.",
            "",
            f"Hand audit: exactly **50 commands**, disproportionately stratified to exercise Bash/PowerShell positives and negatives. Raw mention counts TP/FP/FN={fmt_number(raw_validation['tp'])}/{fmt_number(raw_validation['fp'])}/{fmt_number(raw_validation['fn'])}; raw recall={fmt_percent(100 * raw_validation['mention_recall'] if raw_validation['mention_recall'] is not None else None)}; raw precision={fmt_percent(100 * raw_validation['mention_precision'] if raw_validation['mention_precision'] is not None else None)}. Population-weighted point estimates: recall **{fmt_percent(100 * weighted['mention_recall'] if weighted['mention_recall'] is not None else None)}**, precision **{fmt_percent(100 * weighted['mention_precision'] if weighted['mention_precision'] is not None else None)}**, complete-recovery commands **{fmt_percent(100 * weighted['command_complete_recovery_rate'] if weighted['command_complete_recovery_rate'] is not None else None)}**.",
            "",
            f"Weighting denominators: {stratum_denominators}. Weighted estimated mention totals TP/FP/FN={fmt_number(weighted['estimated_tp_mentions'])}/{fmt_number(weighted['estimated_fp_mentions'])}/{fmt_number(weighted['estimated_fn_mentions'])}; these are inverse-stratum point estimates, not observed integer effects. Reviewer={validation['reviewer']}.",
            "",
            f"Among {fmt_number(raw_validation['commands_with_human_reference'])} audited commands with a manual-audit-confirmed reference, raw any-recovery={fmt_number(raw_validation['commands_any_recovery'])}/{fmt_number(raw_validation['commands_with_human_reference'])} (Wilson 95% {fmt_percent(100 * raw_validation['command_any_recovery_wilson_95']['low'] if raw_validation['command_any_recovery_wilson_95']['low'] is not None else None)}–{fmt_percent(100 * raw_validation['command_any_recovery_wilson_95']['high'] if raw_validation['command_any_recovery_wilson_95']['high'] is not None else None)}); complete recovery={fmt_number(raw_validation['commands_complete_recovery'])}/{fmt_number(raw_validation['commands_with_human_reference'])} (Wilson 95% {fmt_percent(100 * raw_validation['command_complete_recovery_wilson_95']['low'] if raw_validation['command_complete_recovery_wilson_95']['low'] is not None else None)}–{fmt_percent(100 * raw_validation['command_complete_recovery_wilson_95']['high'] if raw_validation['command_complete_recovery_wilson_95']['high'] is not None else None)}). Mention-level intervals are not presented because references cluster within commands.",
            "",
            "Structured result metadata coverage by source occurrence (successful result denominator):",
            "",
            "| Source | Tool | Successful results | Exact structured metadata | Coverage | `originalFile` key | String pre-image |",
            "|---|---|---:|---:|---:|---:|---:|",
        ]
    )
    source_coverage = coverage["structured_result_metadata_by_source_occurrence"]
    for source in ("main", "direct_subagent", "workflow_subagent"):
        for tool in ("Read", "Edit", "Write"):
            row = source_coverage[source][tool]
            lines.append(
                f"| {source} | {tool} | {fmt_number(row['successful_result_occurrences'])} | {fmt_number(row['exact_structured_metadata_occurrences'])} | {fmt_percent(row['exact_metadata_coverage']['percent'])} | {fmt_number(row['original_file_key_occurrences'])} | {fmt_number(row['original_file_string_occurrences'])} |"
            )
    lines.extend(
        [
            "",
            "The earlier 99.6% Read and 98.7% Edit/Write field-presence claims could not be reproduced as corpus-wide usable-metadata rates. Main-result field/key presence is a different population; successful direct/workflow subagent results in this freeze have no exact result metadata. Captured structured event counts are incomplete as total file activity; physical-file cardinalities, percentile tails, and concurrency are not directionally bounded because aliases and proxies can also inflate them.",
        ]
    )

    mix = p["file_type_mix"]
    lines.extend(
        [
            "",
            "## 9. File-type mix",
            "",
            "Categories are mutually exclusive: binary → lock → markdown → JSON → config → source → other. No contents are inspected; binary is extension evidence only.",
            "",
            "| Category | Read events / share | Distinct read files / share | Write events / share | Distinct write files / share | Read events/active actor p50/p90/p99/max | Write events/active actor p50/p90/p99/max |",
            "|---|---:|---:|---:|---:|---|---|",
        ]
    )
    read_mix = mix["localized_read_event_distribution"]
    write_mix = mix["localized_write_event_distribution"]
    for category in ("source", "config", "markdown", "json", "lock", "binary", "other"):
        r = read_mix["categories"][category]
        w = write_mix["categories"][category]
        r_actor = mix["read_category_distribution_per_read_active_actor"][category]["event_count_including_zero"]
        w_actor = mix["write_category_distribution_per_write_active_actor"][category]["event_count_including_zero"]
        lines.append(
            f"| {category} | {fmt_number(r['count'])} / {fmt_percent(r['percent'])} | {fmt_number(r['distinct_file_count'])} / {fmt_percent(r['distinct_file_percent'])} | {fmt_number(w['count'])} / {fmt_percent(w['percent'])} | {fmt_number(w['distinct_file_count'])} / {fmt_percent(w['distinct_file_percent'])} | {fmt_dist(r_actor)} | {fmt_dist(w_actor)} |"
        )

    session = p["session_lengths"]
    lines.extend(
        [
            "",
            "## 10. Session length",
            "",
            f"Logical actors (`agentId` else `sessionId`) with any deduplicated tool call={fmt_number(session['actors_with_any_deduplicated_tool_call'])}. All tool calls/actor: {fmt_dist(session['all_tool_calls_per_actor'])}. Successful Read/Edit/Write events/core-active actor: {fmt_dist(session['successful_read_edit_write_events_per_core_active_actor'])}. Structured active span/core-active actor: {fmt_duration_dist(session['structured_active_span_seconds_per_core_active_actor'])}.",
            "",
            f"Observed wall clock over every timestamped logical actor: {fmt_duration_dist(session['wall_clock_seconds_first_to_last_timestamped_record'])}, denominator={fmt_number(session['wall_clock_actor_denominator'])}. Raw parent sessions={fmt_number(session['raw_session_id_count'])}; wall clock {fmt_duration_dist(session['raw_session_wall_clock_seconds_first_to_last_timestamped_record'])}; all tool calls/raw session including zeros {fmt_dist(session['all_tool_calls_per_raw_session_including_zero'])}. Parent sessions can aggregate many sideagents, so the logical-actor distribution is the coordination substrate's primary dispatch unit.",
            "",
            f"Shell commands/shell-active actor: {fmt_dist(session['shell_commands_per_shell_active_actor'])}. This quantifies how structured event counts understate the one-shot workload. Wall clock remains first-to-last observed record, includes idle gaps, and is not launch-to-close lifetime.",
        ]
    )

    lines.extend(
        [
            "",
            "## Claims that could NOT be verified",
            "",
        ]
    )
    for item in final["claims_that_could_not_be_verified"]:
        lines.extend([f"- {item}", ""])
    lines.extend(["## What would change this verdict", ""])
    for item in final["what_would_change_this_verdict"]:
        lines.extend([f"- {item}", ""])

    lines.extend(
        [
            "## Confidence by claim",
            "",
            "| Claim | Measurement confidence | Scope confidence | Reason |",
            "|---|---|---|---|",
        ]
    )
    for claim, value in final["confidence"].items():
        lines.append(
            f"| `{claim}` | {value['measurement_confidence']} | `{value['scope_confidence']}` | {value['reason']} |"
        )

    lines.extend(
        [
            "",
            "## Reproduction",
            "",
            "```powershell",
            "python instruments/build-params/extract_parameters.py `",
            "  --corpus \"$env:USERPROFILE\\.claude\\projects\" `",
            "  --freeze-manifest-input exploratory/build-params/corpus-manifest.json `",
            "  --output exploratory/build-params/extraction.json `",
            "  --manifest-output exploratory/build-params/corpus-manifest.json `",
            "  --sample-output exploratory/build-params/shell-validation-sample.json",
            "python instruments/build-params/render_parameters.py `",
            "  --extraction exploratory/build-params/extraction.json `",
            "  --labels exploratory/build-params/shell-validation-labels.json `",
            "  --json-output exploratory/build-params/parameters.json `",
            "  --report-output exploratory/build-params/PARAMETERS.md",
            "python -m unittest instruments/build-params/test_extract_parameters.py -v",
            "```",
        ]
    )
    return "\n".join(lines)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--extraction", type=Path, default=Path("exploratory/build-params/extraction.json"))
    parser.add_argument("--labels", type=Path, default=Path("exploratory/build-params/shell-validation-labels.json"))
    parser.add_argument("--json-output", type=Path, default=Path("exploratory/build-params/parameters.json"))
    parser.add_argument("--report-output", type=Path, default=Path("exploratory/build-params/PARAMETERS.md"))
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    extraction = read_json(args.extraction.resolve())
    labels = read_json(args.labels.resolve())
    validation = validate_and_summarize_shell_audit(extraction, labels)
    parameters = copy.deepcopy(extraction["parameters"])
    parameters.pop("shell_validation_sample", None)
    diagnostics = extraction["corpus"]["diagnostics"]
    def quarantine_accounting(kept: int, excluded: int) -> dict[str, Any]:
        eligible = kept + excluded
        return {
            "kept_groups": kept,
            "excluded_ambiguous_groups": excluded,
            "eligible_tool_id_groups": eligible,
            "kept_percent": 100.0 * kept / eligible if eligible else None,
            "excluded_percent": 100.0 * excluded / eligible if eligible else None,
        }

    call_excluded = (
        int(diagnostics.get("call_dedup_session_identity_conflicts", 0))
        + int(diagnostics.get("call_dedup_explicit_actor_conflicts", 0))
        + int(diagnostics.get("call_dedup_tool_or_time_conflicts", 0))
    )
    operation_excluded = (
        int(diagnostics.get("operation_dedup_session_identity_conflicts", 0))
        + int(diagnostics.get("operation_dedup_metadata_conflicts", 0))
        + int(diagnostics.get("operation_dedup_tool_or_time_conflicts", 0))
        + int(diagnostics.get("operation_dedup_result_status_conflicts", 0))
        + int(diagnostics.get("operation_dedup_path_conflicts", 0))
        + int(diagnostics.get("operation_dedup_explicit_actor_conflicts", 0))
    )
    shell_excluded = (
        int(diagnostics.get("shell_dedup_session_identity_conflicts", 0))
        + int(diagnostics.get("shell_dedup_command_or_time_conflicts", 0))
        + int(diagnostics.get("shell_dedup_parser_conflicts", 0))
        + int(diagnostics.get("shell_dedup_explicit_actor_conflicts", 0))
    )
    quarantine = {
        "all_tool_calls": quarantine_accounting(
            int(diagnostics.get("deduplicated_tool_calls", 0)), call_excluded
        ),
        "result_linked_operations": quarantine_accounting(
            int(diagnostics.get("deduplicated_operations", 0)), operation_excluded
        ),
        "shell_commands": quarantine_accounting(
            int(diagnostics.get("deduplicated_shell_commands", 0)), shell_excluded
        ),
        "interpretation": "counts and distributions are conditional on the kept, conflict-free tool-ID groups",
    }
    parameters["event_volume"]["global_tool_id_group_quarantine"] = quarantine
    parameters["observed_concurrency"]["global_tool_id_group_quarantine"] = quarantine
    parameters["session_lengths"]["global_tool_id_group_quarantine"] = quarantine
    parameters["capture_coverage"]["global_tool_id_group_quarantine"] = quarantine
    raw_shell_occurrences = int(diagnostics.get("shell_command_string_occurrences", 0))
    raw_structured_occurrences = sum(
        int(diagnostics.get(f"structured_call_occurrences_{source}_{tool}", 0))
        for source in ("main", "direct_subagent", "workflow_subagent")
        for tool in ("Read", "Edit", "Write")
    )
    parameters["capture_coverage"]["raw_channel_occurrences"] = {
        "shell_commands_with_string": raw_shell_occurrences,
        "structured_read_edit_write_calls": raw_structured_occurrences,
        "shell_to_structured_ratio": (
            raw_shell_occurrences / raw_structured_occurrences
            if raw_structured_occurrences else None
        ),
        "note": "occurrences include copied transcript records; deduplicated conflict-quarantined ratio is primary",
    }
    parameters["capture_coverage"]["shell_parser_hand_validation"] = validation
    final = {
        "schema_version": 1,
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "scope": extraction["scope"],
        "corpus": extraction["corpus"],
        "percentile_rule": extraction["percentile_rule"],
        "parameters": parameters,
        "recommended_seeds": derive_seeds(parameters),
        "claims_that_could_not_be_verified": UNVERIFIED,
        "what_would_change_this_verdict": WHAT_CHANGES,
        "confidence": CONFIDENCE,
    }
    atomic_json(args.json_output.resolve(), final)
    atomic_text(args.report_output.resolve(), render_report(final))
    print(f"wrote {args.json_output.resolve()}")
    print(f"wrote {args.report_output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
