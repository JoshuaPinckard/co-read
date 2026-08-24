from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

import pytest

from analysis import corpus50_report as report


ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "exploratory" / "language-hole" / "results"
STREAMS = ROOT / "exploratory" / "language-hole" / "streams"


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def _ledger_record() -> dict[str, object]:
    record: dict[str, object] = {
        "schema_version": 1,
        "event_id": "event-000001",
        "recorded_at_utc": "2026-08-23T00:00:00+00:00",
        "rule_id": report.EXPECTED_RULE_ID,
        "event_type": "test",
        "candidate": {"name": "fixture/member"},
        "outcome": {"status": "selected", "reason": "fixture"},
        "measurements": {},
        "artifacts": {},
        "previous_record_sha256": None,
    }
    preimage = json.dumps(
        record, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ) + "\n"
    record["record_sha256"] = hashlib.sha256(preimage.encode("utf-8")).hexdigest()
    return record


def _write_ledger(path: Path) -> report.LedgerSummary:
    record = _ledger_record()
    path.write_text(
        json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return report.load_ledger(path)


def _rule_blob() -> str:
    payload = report.RULE_PATH.read_bytes().replace(b"\r\n", b"\n")
    preimage = b"blob " + str(len(payload)).encode("ascii") + b"\0" + payload
    return hashlib.sha1(preimage).hexdigest()


def _attach_selection_attestation(
    manifest: dict[str, object],
    members: list[report.Member],
    ledger_path: Path,
    root: Path,
) -> report.LedgerSummary:
    ledger = _write_ledger(ledger_path)
    ledger_bytes = ledger_path.read_bytes()
    ledger_verification = {
        "valid": True,
        "records": len(ledger.records),
        "last_record_sha256": ledger.records[-1]["record_sha256"],
    }
    rule_freeze = {
        "rule_path": str(report.RULE_PATH.resolve()),
        "git_commit": "4b1d5defee41fa3934f994cae7fc03d0b57b079e",
        "git_blob": _rule_blob(),
        "committed_at": "2026-08-23T12:42:24-07:00",
        "first_acquisition_event_at_utc": "2026-08-23T18:49:31+00:00",
        "last_acquisition_event_at_utc": "2026-08-23T19:06:01+00:00",
        "first_selection_event_at_utc": "2026-08-23T20:00:00+00:00",
        "committed_before_selection_verified": True,
        "committed_before_frame_acquisition_verified": False,
        "acquisition_ledger_verification": {
            "valid": True,
            "records": 1,
            "last_record_sha256": "a" * 64,
        },
        "interpretation": "local timestamp ordering fixture",
    }
    stress = {
        str(member.raw["stress_key"]): {
            "repo_id": member.raw["repo_id"],
            "candidate_order": index + 1,
            "prior_rejection_count": index,
            "terminal_event_count": index + 1,
            "predicate_recheck": {"recomputed": True},
        }
        for index, member in enumerate(
            item for item in members if item.raw.get("cohort") == "stress"
        )
    }
    anchors = {
        member.name: {
            "head": member.raw["head"],
            "first_parent_commit_count": member.raw["first_parent_commit_count"],
            "reachable_commit_count": member.raw["reachable_commit_count"],
        }
        for member in members
        if member.raw.get("cohort") == "retained_anchor"
    }
    provenance = {
        "schema_version": 1,
        "rule_id": report.EXPECTED_RULE_ID,
        "verified_at_utc": "2026-08-23T20:01:00+00:00",
        "rule_freeze": rule_freeze,
        "frame_verification": {"verified": {}},
        "ledger_verification": ledger_verification,
        "stress": stress,
        "pending_stress": None,
        "base": {
            "selected_count": 35,
            "selected_repo_ids": [
                member.raw["repo_id"]
                for member in members
                if member.raw.get("cohort") == "base"
            ],
        },
        "retained_anchors": {"verified": anchors},
    }
    provenance_path = root / "selection-provenance.json"
    _write_json(provenance_path, provenance)
    provenance_bytes = provenance_path.read_bytes()
    manifest["selection_provenance"] = {
        "path": str(provenance_path.resolve()),
        "sha256": hashlib.sha256(provenance_bytes).hexdigest(),
        "byte_length": len(provenance_bytes),
        "ledger_path": str(ledger_path.resolve()),
        "ledger_sha256": hashlib.sha256(ledger_bytes).hexdigest(),
        "ledger_byte_length": len(ledger_bytes),
        "ledger_verification": ledger_verification,
    }
    manifest["rule_freeze_provenance"] = rule_freeze
    return ledger


def _synthetic_members() -> list[report.Member]:
    members: list[report.Member] = []
    for index in range(50):
        cohort = "retained_anchor" if index < 10 else "stress" if index < 15 else "base"
        raw: dict[str, object] = {
            "selection_order": index + 1,
            "slug": f"fixture__attested-{index}",
            "name": f"fixture/attested-{index}",
            "cohort": cohort,
            "selection_status": "selected",
            "head": f"{index + 100:040x}",
            "first_parent_commit_count": 500 + index,
            "reachable_commit_count": 700 + index,
            "language_stratum": "fixture-language",
            "layout_stratum": "fixture-layout",
            "capped": False,
        }
        if cohort == "stress":
            raw["repo_id"] = 20_000 + index
            raw["stress_key"] = report.STRESS_KEYS[index - 10]
        elif cohort == "base":
            raw["repo_id"] = 20_000 + index
        members.append(
            report.Member(
                order=index + 1,
                slug=str(raw["slug"]),
                name=str(raw["name"]),
                raw=raw,
            )
        )
    return members


def _success_result(member: report.Member, *, capped: bool) -> dict[str, object]:
    commit_models = {
        model_key: {
            "empty_queries": 0,
            "p1_hits": 1,
            "p10_hits": 2,
            "r10_sum": 2.0,
            "r20_sum": 2.0,
        }
        for model_key in report.MODEL_LABELS
    }
    models = {
        model_key: {
            "queries": 2,
            "empty_queries": 0,
            "p1_hits": 1,
            "p10_hits": 2,
            "r10_sum": 2.0,
            "r20_sum": 2.0,
            "p_at_1": 0.5,
            "p_at_10": 0.1,
            "r_at_10": 1.0,
            "r_at_20": 1.0,
            "empty_radius_rate": 0.0,
        }
        for model_key in report.MODEL_LABELS
    }
    first_parent = int(member.raw["first_parent_commit_count"])
    return {
        "status": "ok",
        "repository": {"slug": member.slug, "name": member.name},
        "source_head_sha": member.raw["head"],
        "implementation": {"harness_sha256": "b" * 64},
        "protocol": {
            "cap": (
                "reachable history exceeds 20000; learned indexes start empty"
                if capped
                else "none; full first-parent history replayed"
            )
        },
        "coverage": {
            "query_count": 2,
            "eligible_commit_count": 1,
            "commits_replayed": report.CAP_REPLAY_COUNT if capped else first_parent,
            "first_parent_commits_at_head": first_parent,
            "left_truncated": capped,
        },
        "eligible_commits": [
            {
                "sha": "c" * 40,
                "query_count": 2,
                "eligible_file_count": 2,
                "models": commit_models,
            }
        ],
        "models": models,
    }


def test_correlations_and_type7_quantile() -> None:
    assert report.quantile([0.0, 10.0], 0.25) == pytest.approx(2.5)
    assert report.pearson([1.0, 2.0, 3.0], [3.0, 2.0, 1.0]) == pytest.approx(-1.0)
    assert report.spearman([10.0, 10.0, 20.0], [1.0, 1.0, 3.0]) == pytest.approx(1.0)


def test_ledger_digest_includes_jsonl_newline(tmp_path: Path) -> None:
    ledger_path = tmp_path / "ledger.jsonl"
    record = _ledger_record()
    ledger_path.write_text(
        json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    summary = report.load_ledger(ledger_path)
    assert not summary.linkage_errors
    assert not summary.digest_errors
    assert summary.outcome_counts == Counter({"selected": 1})


def test_final_selection_attestation_reconciles_and_detects_drift(tmp_path: Path) -> None:
    members = _synthetic_members()
    manifest: dict[str, object] = {
        "rule_id": report.EXPECTED_RULE_ID,
        "scope_name": f"50 repositories drawn under Rule {report.EXPECTED_RULE_ID}",
        "listing_dates": {"base": "2026-08-22", "stress": "2026-08-23"},
        "seed": "fixture",
        "disk_cap_bytes": 20 * 1024**3,
        "members": [dict(member.raw) for member in members],
    }
    ledger_path = tmp_path / "ledger.jsonl"
    ledger = _attach_selection_attestation(manifest, members, ledger_path, tmp_path)
    assert report._selection_provenance_issues(
        manifest, members, ledger, ledger_path
    ) == []

    provenance_record = manifest["selection_provenance"]
    assert isinstance(provenance_record, dict)
    provenance_path = Path(str(provenance_record["path"]))
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    provenance["stress"]["config"]["repo_id"] += 1
    _write_json(provenance_path, provenance)
    payload = provenance_path.read_bytes()
    provenance_record["sha256"] = hashlib.sha256(payload).hexdigest()
    provenance_record["byte_length"] = len(payload)
    issues = report._selection_provenance_issues(manifest, members, ledger, ledger_path)
    assert "stress attestation repo_id differs for config" in issues

    ledger_path.write_text(ledger_path.read_text(encoding="utf-8") + "{}\n", encoding="utf-8")
    drifted_ledger = report.load_ledger(ledger_path)
    issues = report._selection_provenance_issues(
        manifest, members, drifted_ledger, ledger_path
    )
    assert "current selection-ledger SHA-256 differs from the final attestation" in issues
    assert any("attested ledger_verification.records" in issue for issue in issues)


def test_success_provenance_and_cap_contract_are_symmetric(tmp_path: Path) -> None:
    raw: dict[str, object] = {
        "selection_order": 1,
        "slug": "fixture__cap-contract",
        "name": "fixture/cap-contract",
        "cohort": "base",
        "selection_status": "selected",
        "head": "d" * 40,
        "first_parent_commit_count": 600,
        "reachable_commit_count": 25_001,
        "capped": True,
        "language_stratum": "fixture",
        "layout_stratum": "fixture",
    }
    member = report.Member(order=1, slug=str(raw["slug"]), name=str(raw["name"]), raw=raw)
    result = _success_result(member, capped=True)
    result["protocol"] = {"cap": "left-truncated without cold-start attestation"}
    stream = {
        "status": "ok",
        "repository": {"slug": member.slug},
        "source_head_sha": raw["head"],
        "reachable_commit_count": 25_002,
        "first_parent_commit_count": 600,
        "capped": True,
        "cap_reason": "synthetic history cap",
    }
    results_dir = tmp_path / "results"
    streams_dir = tmp_path / "streams"
    _write_json(results_dir / f"{member.slug}.json", result)
    _write_json(streams_dir / f"{member.slug}.meta.json", stream)
    observation = report.collect_observations([member], results_dir, streams_dir)[0]
    assert any("reachable-commit counts disagree" in note for note in observation.validation_notes)
    assert "capped result does not attest that learned indexes start empty" in observation.validation_notes

    raw["reachable_commit_count"] = 1_000
    stream["reachable_commit_count"] = 1_000
    _write_json(streams_dir / f"{member.slug}.meta.json", stream)
    observation = report.collect_observations([member], results_dir, streams_dir)[0]
    assert any("expected False from reachable count 1,000" in note for note in observation.validation_notes)
    assert any("expected 600 from the cap decision" in note for note in observation.validation_notes)

    result.pop("implementation")
    result.pop("source_head_sha")
    _write_json(results_dir / f"{member.slug}.json", result)
    observation = report.collect_observations([member], results_dir, streams_dir)[0]
    assert "successful result lacks a valid replay harness SHA-256" in observation.validation_notes
    assert "successful result lacks source_head_sha" in observation.validation_notes


def test_runner_state_preserves_failure_when_disk_guard_prevents_artifact(
    tmp_path: Path,
) -> None:
    raw = {
        "selection_order": 1,
        "slug": "fixture__guarded",
        "name": "fixture/guarded",
        "selection_status": "selected",
    }
    member = report.Member(order=1, slug=str(raw["slug"]), name=str(raw["name"]), raw=raw)
    run_state = {
        "repositories": {
            member.slug: {
                "stages": {
                    "clone": {
                        "status": "failed",
                        "failure_type": "DiskGuardViolation",
                        "failure": "20 GiB cap reached",
                    }
                },
                "cap": {
                    "applied": True,
                    "reachable_commit_count": 25001,
                    "threshold_reachable_commits": 20000,
                    "replay_commits": 5000,
                },
            }
        }
    }
    observations = report.collect_observations(
        [member], tmp_path / "results", tmp_path / "streams", run_state
    )
    assert observations[0].status == "failed"
    assert observations[0].failure_stage == "clone"
    assert observations[0].failure == "20 GiB cap reached"
    assert observations[0].capped is True
    assert observations[0].cap_reason is not None


def test_existing_replay_outputs_validate_and_render_as_draft(tmp_path: Path) -> None:
    """Exercise real schema/results without pretending the current ten are fifty."""

    result_documents = [
        json.loads(path.read_text(encoding="utf-8")) for path in sorted(RESULTS.glob("*.json"))
    ]
    assert result_documents, "the checked-out replay corpus should contain stored results"

    # Incremental local reruns can temporarily contain two harness hashes. Use
    # one internally comparable group, exactly as the final report requires.
    hash_counts = Counter(
        document.get("implementation", {}).get("harness_sha256")
        for document in result_documents
        if document.get("status") == "ok"
    )
    selected_hash, _ = hash_counts.most_common(1)[0]
    selected = [
        document
        for document in result_documents
        if document.get("status") == "ok"
        and document.get("implementation", {}).get("harness_sha256") == selected_hash
    ]
    assert selected

    members = []
    for index, document in enumerate(selected, start=1):
        repository = document["repository"]
        members.append(
            {
                "selection_order": index,
                "slug": repository["slug"],
                "name": repository["name"],
                "url": repository.get("url"),
                "cohort": "retained_anchor",
                "selection_status": "selected",
                "language_stratum": "fixture",
                "layout_stratum": "fixture",
            }
        )

    manifest = {
        "schema_version": 1,
        "rule_id": report.EXPECTED_RULE_ID,
        "scope_name": (
            "50 repositories drawn under Rule C50-2026-08-23-v1 "
            "(test fixture; deliberately incomplete)"
        ),
        "listing_dates": {"fixture": "2026-08-22"},
        "seed": "fixture",
        "disk_cap_bytes": 20 * 1024**3,
        "members": members,
    }
    manifest_path = tmp_path / "manifest.json"
    ledger_path = tmp_path / "ledger.jsonl"
    output_path = tmp_path / "CORPUS-50.md"
    plot_prefix = tmp_path / "recall-gt"
    _write_json(manifest_path, manifest)
    ledger_record = _ledger_record()
    ledger_path.write_text(
        json.dumps(ledger_record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )

    complete = report.build_report(
        manifest_path=manifest_path,
        ledger_path=ledger_path,
        results_dir=RESULTS,
        streams_dir=STREAMS,
        output_path=output_path,
        plot_prefix=plot_prefix,
    )
    assert complete is False
    markdown = output_path.read_text(encoding="utf-8")
    assert "**This is an incomplete draft, not a 50-member verdict.**" in markdown
    assert "## Claims that could NOT be verified" in markdown
    assert "## What would change this verdict" in markdown
    assert "Mean GT per query" in markdown
    assert "No correlation statistic was preregistered" in markdown
    assert "measured retained anchors span **0.293–0.678**" in markdown
    assert "**9/10** strictly above 0.500" in markdown
    assert "8 full-history retained anchors span **0.531–0.678**" in markdown
    assert "**8/8** above 0.500" in markdown
    assert plot_prefix.with_suffix(".png").stat().st_size > 1000
    assert plot_prefix.with_suffix(".svg").stat().st_size > 1000

    if any(document["repository"]["slug"] == "psf__requests" for document in selected):
        request_document = next(
            document for document in selected if document["repository"]["slug"] == "psf__requests"
        )
        request_member = next(member for member in members if member["slug"] == "psf__requests")
        mean_gt = report.validate_success_result(
            report.Member(
                order=int(request_member["selection_order"]),
                slug=str(request_member["slug"]),
                name=str(request_member["name"]),
                raw=request_member,
            ),
            request_document,
        )
        assert mean_gt == pytest.approx(12.8021892103, abs=1e-9)


def test_complete_render_keeps_terminal_failure_in_scope(tmp_path: Path) -> None:
    members: list[report.Member] = []
    for index in range(50):
        selection_order = index + 1
        cohort = "retained_anchor" if index < 10 else "stress" if index < 15 else "base"
        raw = {
            "selection_order": selection_order,
            "slug": f"fixture__member-{index}",
            "name": f"fixture/member-{index}",
            "cohort": cohort,
            "selection_status": "selected",
            "head": f"{index:040x}",
            "first_parent_commit_count": 500 + index,
            "reachable_commit_count": 25_000 if index == 1 else 700 + index,
            "language_stratum": "fixture-language",
            "layout_stratum": "fixture-layout",
            "capped": index == 1,
        }
        if cohort == "stress":
            raw["repo_id"] = 10_000 + index
            raw["stress_key"] = report.STRESS_KEYS[index - 10]
        elif cohort == "base":
            raw["repo_id"] = 10_000 + index
        members.append(
            report.Member(
                order=selection_order,
                slug=str(raw["slug"]),
                name=str(raw["name"]),
                raw=raw,
            )
        )

    manifest = {
        "rule_id": report.EXPECTED_RULE_ID,
        "scope_name": (
            "50 repositories drawn under Rule C50-2026-08-23-v1 "
            "(complete synthetic rendering test)"
        ),
        "listing_dates": {"base": "2026-08-22", "stress": "2026-08-23"},
        "seed": "fixture",
        "disk_cap_bytes": 20 * 1024**3,
        "members": [dict(member.raw) for member in members],
    }
    ledger_path = tmp_path / "ledger.jsonl"
    ledger = _attach_selection_attestation(manifest, members, ledger_path, tmp_path)
    observations: list[report.Observation] = []
    for index, member in enumerate(members[:-1]):
        recall = 0.9 / (1.0 + index / 8.0)
        p1 = 0.4 if index == 0 else 0.6
        models = {
            model_key: {
                "p_at_1": p1,
                "p_at_10": 0.2,
                "r_at_10": (
                    0.0
                    if model_key == "popularity_control" and index == 2
                    else recall
                    if model_key != "popularity_control"
                    else recall / 2.0
                ),
                "r_at_20": min(1.0, recall + 0.1),
                "empty_radius_rate": 0.01,
                "median_query_microseconds": 10.0,
            }
            for model_key in report.MODEL_LABELS
        }
        result = {
            "status": "ok",
            "models": models,
            "coverage": {
                "commits_replayed": 5000 if index == 1 else 1000,
                "eligible_commit_count": 100,
                "query_count": 200,
                "largest_query_commit_share": 0.05,
            },
        }
        observations.append(
            report.Observation(
                member=member,
                status="ok",
                result=result,
                stream={"reachable_commit_count": 25_000 if index == 1 else 2_000},
                mean_ground_truth_size=float(index + 2),
                capped=index == 1,
                cap_reason="synthetic cap log" if index == 1 else None,
            )
        )
    observations.append(
        report.Observation(
            member=members[-1],
            status="failed",
            failure_stage="extraction",
            failure="synthetic terminal failure",
        )
    )

    markdown, complete = report.render_report(
        manifest=manifest,
        members=members,
        ledger=ledger,
        observations=observations,
        output_path=tmp_path / "CORPUS-50.md",
        ledger_path=ledger_path,
        png_path=tmp_path / "plot.png",
        svg_path=tmp_path / "plot.svg",
    )
    assert complete is True
    assert "48 of the 50 selected members" in markdown
    assert "synthetic terminal failure" in markdown
    assert "synthetic cap log" in markdown
    assert "sign alone does not verify that recall ‘tracks’ mean GT" in markdown
    assert "Correction to the ten-member premise" in markdown
    assert "different analysis sets" in markdown
    assert "selects one member for each of the five fixed stress keys" in markdown
    assert "- Final selection attestation:" in markdown
    assert "### Realised fixed stress-shape strata" in markdown
    assert "| non_english | 1 | 1 |" in markdown
    assert "47/48 full-history successful members" in markdown
    assert "fixture/member-2" in markdown
    assert "does not independently prove when recording occurred" in markdown
    assert "2026-08-23T12:42:24-07:00" in markdown
    assert "stronger pre-acquisition freeze claim is unverified" in markdown
    assert "**This is an incomplete draft" not in markdown
    metrics_section = markdown.split("## All per-member model metrics", 1)[1].split(
        "## Coverage, caps, and failures", 1
    )[0]
    model_labels = tuple(report.MODEL_LABELS.values())
    for line in metrics_section.splitlines():
        if line.startswith("| fixture/") and any(label in line for label in model_labels):
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            assert cells[1] != "—", f"numeric recall row lacks adjacent mean GT: {line}"
