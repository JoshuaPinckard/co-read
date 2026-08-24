from __future__ import annotations

import gzip
import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from analysis import corpus50


def append_external_valid_record(ledger: Path, event_type: str) -> dict[str, object]:
    """Simulate a valid append made outside corpus50's process-local cache."""

    rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    completed: dict[str, object] = {
        "event_type": event_type,
        "schema_version": corpus50.SCHEMA_VERSION,
        "rule_id": corpus50.RULE_ID,
        "recorded_at_utc": "2026-08-23T00:00:00Z",
        "event_id": f"C50-{len(rows) + 1:06d}",
        "previous_record_sha256": rows[-1]["record_sha256"] if rows else None,
    }
    completed["record_sha256"] = hashlib.sha256(
        corpus50.canonical_json_bytes(completed)
    ).hexdigest()
    with ledger.open("ab") as stream:
        stream.write(corpus50.canonical_json_bytes(completed))
        stream.flush()
        os.fsync(stream.fileno())
    return completed


def event(
    event_id: int,
    repo_id: int,
    name: str,
    actor: str,
    created_at: str,
) -> dict[str, object]:
    return {
        "id": str(event_id),
        "type": "PushEvent",
        "actor": {"login": actor},
        "repo": {"id": repo_id, "name": name},
        "public": True,
        "created_at": created_at,
    }


def make_archive_fixture(root: Path) -> None:
    paths = corpus50.acquisition_paths(root)
    paths["gharchive"].mkdir(parents=True)
    paths["manifests"].mkdir(parents=True)
    events = [
        event(1, 101, "org/old", "alice", "2026-08-22T00:00:00Z"),
        event(2, 101, "org/new", "alice", "2026-08-22T00:00:01Z"),
        event(3, 101, "org/new", "alice", "2026-08-22T00:00:02Z"),
        event(4, 102, "org/two-actors", "alice", "2026-08-22T00:00:03Z"),
        event(5, 102, "org/two-actors", "bob", "2026-08-22T00:00:04Z"),
        event(6, 103, "org/bots", "dependabot[bot]", "2026-08-22T00:00:05Z"),
        event(7, 103, "org/bots", "github-actions", "2026-08-22T00:00:06Z"),
        event(8, 103, "org/bots", "X[BoT]", "2026-08-22T00:00:07Z"),
        event(
            9,
            104,
            "psf/requests",
            "alice",
            "2026-08-22T00:00:08Z",
        ),
        event(
            10,
            104,
            "renamed/requests",
            "alice",
            "2026-08-22T00:00:09Z",
        ),
        event(
            11,
            104,
            "renamed/requests",
            "alice",
            "2026-08-22T00:00:10Z",
        ),
        {
            "id": "12",
            "type": "WatchEvent",
            "repo": {"id": 999, "name": "org/not-push"},
            "public": True,
            "created_at": "2026-08-22T00:00:11Z",
        },
    ]
    inventory = []
    for spec in corpus50.gharchive_specs():
        lines = events if spec["hour"] == 0 else []
        raw = b"".join(
            json.dumps(item, separators=(",", ":")).encode() + b"\n" for item in lines
        )
        compressed = gzip.compress(raw, mtime=0)
        target = paths["gharchive"] / spec["filename"]
        target.write_bytes(compressed)
        digest = hashlib.sha256(compressed).hexdigest()
        metadata = {
            "schema_version": 1,
            "rule_id": corpus50.RULE_ID,
            "listing_date": corpus50.BASE_LISTING_DATE,
            "hour": spec["hour"],
            "url": spec["url"],
            "retrieved_at_utc": "2026-08-23T01:02:03Z",
            "http_date": "Sun, 23 Aug 2026 01:02:03 GMT",
            "etag": None,
            "byte_length": len(compressed),
            "sha256": digest,
            "gzip_valid": True,
            "response_headers_file": target.name + ".headers.json",
        }
        corpus50.atomic_write_json(
            target.with_suffix(target.suffix + ".headers.json"),
            {"status": 200, "header_pairs": [["Date", metadata["http_date"]]]},
        )
        corpus50.atomic_write_json(
            target.with_suffix(target.suffix + ".acquisition.json"), metadata
        )
        inventory.append(metadata)
    corpus50.atomic_write_json(
        paths["manifests"] / f"gharchive-{corpus50.BASE_LISTING_DATE}.json",
        {
            "schema_version": 1,
            "rule_id": corpus50.RULE_ID,
            "kind": "gharchive-24-hour-listing",
            "listing_date": corpus50.BASE_LISTING_DATE,
            "complete": True,
            "artifacts": inventory,
        },
    )


def make_search_fixture(root: Path) -> None:
    paths = corpus50.acquisition_paths(root)
    paths["search"].mkdir(parents=True)
    paths["manifests"].mkdir(parents=True, exist_ok=True)
    inventory = []
    key_ids = {key: 1000 + index for index, key in enumerate(corpus50.STRESS_KEYS)}
    for spec in corpus50.stress_snapshot_specs():
        repo_id = key_ids[spec.stress_key]
        full_name = f"fixture/{spec.stress_key}"
        body = json.dumps(
            {
                "total_count": 1,
                "incomplete_results": False,
                "items": [
                    {
                        "id": repo_id,
                        "full_name": full_name,
                        "clone_url": f"https://github.com/{full_name}.git",
                        "html_url": f"https://github.com/{full_name}",
                    }
                ],
            },
            separators=(",", ":"),
        ).encode()
        artifact_paths = corpus50._search_artifact_paths(paths["search"], spec.snapshot_id)
        artifact_paths["body"].write_bytes(body)
        corpus50.atomic_write_json(
            artifact_paths["headers"],
            {
                "status": 200,
                "header_pairs": [["Date", "Sun, 23 Aug 2026 01:02:03 GMT"]],
            },
        )
        metadata = {
            "schema_version": 1,
            "rule_id": corpus50.RULE_ID,
            "listing_date": corpus50.STRESS_LISTING_DATE,
            "snapshot_id": spec.snapshot_id,
            "stress_key": spec.stress_key,
            "query": spec.query,
            "page": spec.page,
            "url": spec.url(),
            "retrieved_at_utc": "2026-08-23T01:02:03Z",
            "http_date": "Sun, 23 Aug 2026 01:02:03 GMT",
            "etag": None,
            "byte_length": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
            "incomplete_results": False,
            "response_headers_file": artifact_paths["headers"].name,
            "response_file": artifact_paths["body"].name,
        }
        corpus50.atomic_write_json(artifact_paths["metadata"], metadata)
        inventory.append(metadata)
    corpus50.atomic_write_json(
        paths["manifests"] / "github-search-snapshots.json",
        {
            "schema_version": 1,
            "rule_id": corpus50.RULE_ID,
            "kind": "github-search-stress-snapshots",
            "listing_date": corpus50.STRESS_LISTING_DATE,
            "complete": True,
            "snapshot_count": 63,
            "artifacts": inventory,
        },
    )


def test_priority_key_matches_frozen_preimage() -> None:
    expected = hashlib.sha256(
        (
            corpus50.SEED.encode()
            + b"\0base\0"
            + b"123"
        )
    ).hexdigest()
    assert corpus50.priority_key("base", 123) == expected


def test_bot_rule() -> None:
    assert corpus50.is_bot("dependabot[bot]")
    assert corpus50.is_bot("X[BOT]")
    assert corpus50.is_bot("GitHub-Actions")
    assert not corpus50.is_bot("robot")


def test_fixed_snapshot_catalog() -> None:
    specs = corpus50.stress_snapshot_specs()
    assert len(specs) == 63
    assert sum(spec.stress_key == "config" for spec in specs) == 3
    assert sum(spec.stress_key == "catalog" for spec in specs) == 3
    assert sum(spec.stress_key == "import" for spec in specs) == 10
    assert sum(spec.stress_key == "low_author" for spec in specs) == 40
    assert sum(spec.stress_key == "non_english" for spec in specs) == 7
    assert all("fork:false archived:false size:<200000" in spec.query for spec in specs)
    assert all("per_page=100" in spec.url() for spec in specs)


def test_build_base_frame_exact_active_rule_and_anchor_identity(tmp_path: Path) -> None:
    root = tmp_path / "frame"
    make_archive_fixture(root)
    manifest = corpus50.build_base_frame(root)
    assert manifest["complete"] is True
    assert manifest["record_count"] == 2
    assert manifest["public_push_event_count"] == 11
    assert manifest["retained_anchor_identity_count_removed"] == 1
    rows = [json.loads(line) for line in (root / "frames/base-active.jsonl").read_text().splitlines()]
    assert {row["repo_id"] for row in rows} == {101, 102}
    assert [row["priority_key"] for row in rows] == sorted(
        row["priority_key"] for row in rows
    )
    renamed = next(row for row in rows if row["repo_id"] == 101)
    assert renamed["clone_name"] == "org/new"
    assert renamed["observed_names"] == ["org/new", "org/old"]
    assert renamed["distinct_nonbot_actor_count"] == 1

    # A second run resumes the 24 committed hour transactions without doubling.
    assert corpus50.build_base_frame(root)["record_count"] == 2


def test_hash_chained_ledger_detects_mutation(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    corpus50.append_hash_chained(ledger, {"event_type": "one"})
    corpus50.append_hash_chained(ledger, {"event_type": "two"})
    assert corpus50.verify_hash_chain(ledger)["records"] == 2
    rows = ledger.read_text().splitlines()
    rows[0] = rows[0].replace('"one"', '"tampered"')
    ledger.write_text("\n".join(rows) + "\n")
    with pytest.raises(corpus50.Corpus50Error, match="checksum"):
        corpus50.verify_hash_chain(ledger)


def test_hash_chain_tip_makes_appends_incremental_but_public_verify_forces_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = tmp_path / "ledger.jsonl"
    scans = 0
    fsyncs = 0
    scan_hash_chain = corpus50._scan_hash_chain
    fsync = corpus50.os.fsync

    def counted_scan(path: Path) -> corpus50._HashChainTip:
        nonlocal scans
        scans += 1
        return scan_hash_chain(path)

    def counted_fsync(file_descriptor: int) -> None:
        nonlocal fsyncs
        fsyncs += 1
        fsync(file_descriptor)

    monkeypatch.setattr(corpus50, "_scan_hash_chain", counted_scan)
    monkeypatch.setattr(corpus50.os, "fsync", counted_fsync)
    appended = [
        corpus50.append_hash_chained(ledger, {"event_type": f"event-{index}"})
        for index in range(1, 101)
    ]

    assert scans == 1
    assert fsyncs == 100
    assert [row["event_id"] for row in appended] == [
        f"C50-{index:06d}" for index in range(1, 101)
    ]
    assert appended[0]["previous_record_sha256"] is None
    assert all(
        current["previous_record_sha256"] == previous["record_sha256"]
        for previous, current in zip(appended, appended[1:])
    )

    assert corpus50.verify_hash_chain(ledger)["records"] == 100
    assert corpus50.verify_hash_chain(ledger)["records"] == 100
    assert scans == 3


def test_cached_hash_chain_tip_rescans_after_external_valid_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = tmp_path / "ledger.jsonl"
    scans = 0
    scan_hash_chain = corpus50._scan_hash_chain

    def counted_scan(path: Path) -> corpus50._HashChainTip:
        nonlocal scans
        scans += 1
        return scan_hash_chain(path)

    monkeypatch.setattr(corpus50, "_scan_hash_chain", counted_scan)
    first = corpus50.append_hash_chained(ledger, {"event_type": "one"})
    external = append_external_valid_record(ledger, "external")
    third = corpus50.append_hash_chained(ledger, {"event_type": "three"})

    assert scans == 2
    assert external["previous_record_sha256"] == first["record_sha256"]
    assert third["event_id"] == "C50-000003"
    assert third["previous_record_sha256"] == external["record_sha256"]
    assert corpus50.verify_hash_chain(ledger)["records"] == 3


def test_cached_hash_chain_tip_rejects_external_in_place_corruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = tmp_path / "ledger.jsonl"
    scans = 0
    scan_hash_chain = corpus50._scan_hash_chain

    def counted_scan(path: Path) -> corpus50._HashChainTip:
        nonlocal scans
        scans += 1
        return scan_hash_chain(path)

    monkeypatch.setattr(corpus50, "_scan_hash_chain", counted_scan)
    corpus50.append_hash_chained(ledger, {"event_type": "one"})
    original_stat = ledger.stat()
    original = ledger.read_bytes()
    tampered = original.replace(b'"event_type":"one"', b'"event_type":"eno"')
    assert tampered != original and len(tampered) == len(original)
    ledger.write_bytes(tampered)
    os.utime(
        ledger,
        ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns + 1_000_000_000),
    )

    with pytest.raises(corpus50.Corpus50Error, match="checksum"):
        corpus50.append_hash_chained(ledger, {"event_type": "two"})
    assert scans == 2
    assert len(ledger.read_text(encoding="utf-8").splitlines()) == 1


def test_cached_hash_chain_tip_rejects_replaced_corrupt_ledger(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    corpus50.append_hash_chained(ledger, {"event_type": "one"})
    original = ledger.read_bytes()
    replacement = tmp_path / "replacement.jsonl"
    replacement.write_bytes(
        original.replace(b'"event_type":"one"', b'"event_type":"eno"')
    )
    os.replace(replacement, ledger)

    with pytest.raises(corpus50.Corpus50Error, match="checksum"):
        corpus50.append_hash_chained(ledger, {"event_type": "two"})
    assert len(ledger.read_text(encoding="utf-8").splitlines()) == 1


def test_cached_terminal_prefix_matches_stress_and_base_sequence_rules(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "ledger.jsonl"
    corpus50.append_hash_chained(
        ledger,
        {
            "event_type": "candidate_screened",
            "candidate": {
                "cohort": "stress",
                "stress_key": "config",
                "candidate_order": 1,
                "base_rank": 2,
            },
            "outcome": {"status": "selected"},
        },
    )
    corpus50._assert_candidate_sequence(
        ledger, {"candidate_order": 2}, "stress", "config"
    )
    with pytest.raises(corpus50.Corpus50Error, match=r"outcomes: \[2\]"):
        corpus50._assert_candidate_sequence(
            ledger, {"candidate_order": 3}, "stress", "config"
        )

    corpus50.append_hash_chained(
        ledger,
        {
            "event_type": "candidate_screened",
            "candidate": {
                "cohort": "base",
                "base_rank": 1,
            },
            "outcome": {"status": "rejected"},
        },
    )
    corpus50.append_hash_chained(
        ledger,
        {
            "event_type": "base_candidate_removed_as_stress",
            "candidate": {"cohort": "base", "base_rank": 3},
            "outcome": {"status": "excluded"},
        },
    )
    corpus50._assert_candidate_sequence(ledger, {"base_rank": 4}, "base", None)
    with pytest.raises(corpus50.Corpus50Error, match=r"outcomes: \[4\]"):
        corpus50._assert_candidate_sequence(ledger, {"base_rank": 5}, "base", None)

    corpus50.append_hash_chained(
        ledger,
        {
            "event_type": "candidate_screened",
            "candidate": {
                "cohort": "stress",
                "stress_key": "config",
                "candidate_order": 2,
            },
            "outcome": {"status": "rejected"},
        },
    )
    corpus50._assert_candidate_sequence(
        ledger, {"candidate_order": 3}, "stress", "config"
    )


def test_acquisition_dry_run_has_24_targets(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = corpus50.main(
        ["acquire-gharchive", "--frame-root", "D:/corpus50", "--dry-run"]
    )
    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert len(output["requests"]) == 24
    assert output["disk_cap_bytes"] == 21_474_836_480


def test_build_stress_frames_deduplicates_and_ranks(tmp_path: Path) -> None:
    root = tmp_path / "frame"
    make_search_fixture(root)
    manifest = corpus50.build_stress_frames(root)
    assert manifest["complete"] is True
    assert set(manifest["frames"]) == set(corpus50.STRESS_KEYS)
    for key in corpus50.STRESS_KEYS:
        assert manifest["frames"][key]["record_count"] == 1
        row = json.loads((root / f"frames/stress-{key}.jsonl").read_text())
        assert row["stress_key"] == key
        assert row["stress_rank"] == 1
        assert row["priority_key"] == corpus50.priority_key(key, row["repo_id"])


def test_search_dry_run_has_exact_63_urls(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = corpus50.main(
        ["acquire-search", "--frame-root", "D:/corpus50", "--dry-run"]
    )
    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert len(output["requests"]) == 63
    assert all("sort=stars" in request["url"] for request in output["requests"])


def test_path_classifiers_follow_frozen_priority() -> None:
    artifact = corpus50.classify_paths(["main.py", *[f"doc-{i}.md" for i in range(5)]])
    assert artifact["layout_stratum"] == "artifact/config/docs"
    assert artifact["language_stratum"] == "Python"

    monorepo = corpus50.classify_paths(
        [
            path
            for index in range(5)
            for path in (f"pkg{index}/package.json", f"pkg{index}/index.js")
        ]
    )
    assert monorepo["layout_stratum"] == "manifest monorepo"

    multi = corpus50.classify_paths(
        [f"module{module}/file{index}.go" for module in range(4) for index in range(5)]
    )
    assert multi["layout_stratum"] == "multi-module tree"
    assert multi["primary_language"] == "Go"


def test_config_and_catalog_predicates() -> None:
    config_paths = ["README", *[f"config-{index}.json" for index in range(8)], "tool.py"]
    result = corpus50.evaluate_config_predicate(config_paths)
    assert result["passed"] is True
    assert result["allowed_path_fraction"] == 0.9

    catalog_paths = [f"registry/component-{index:03d}/metadata.json" for index in range(100)]
    catalog = corpus50.evaluate_catalog_predicate(catalog_paths)
    assert catalog["passed"] is True
    assert catalog["evidence"]["component_count"] == 100
    assert catalog["evidence"]["median_files_per_component"] == 1


def eligible_candidates() -> list[dict[str, object]]:
    languages = [
        language
        for language, count in corpus50.LANGUAGE_QUOTAS.items()
        for _ in range(count)
    ]
    layouts = [
        layout
        for layout, count in corpus50.LAYOUT_QUOTAS.items()
        for _ in range(count)
    ]
    identities = sorted(
        range(1, 36), key=lambda repo_id: (corpus50.priority_key("base", repo_id), repo_id)
    )
    return [
        {
            "repo_id": repo_id,
            "priority_key": corpus50.priority_key("base", repo_id),
            "base_rank": rank,
            "language_stratum": language,
            "layout_stratum": layout,
        }
        for rank, (repo_id, language, layout) in enumerate(
            zip(identities, languages, layouts), start=1
        )
    ]


def test_base_solver_hits_both_exact_margins() -> None:
    result = corpus50.solve_base_selection(eligible_candidates())
    assert result["first_feasible_base_rank"] == 35
    assert result["minimum_total_absolute_margin_deviation"] == 0
    assert result["realised_language_counts"] == corpus50.LANGUAGE_QUOTAS
    assert result["realised_layout_counts"] == corpus50.LAYOUT_QUOTAS
    assert len(result["selected"]) == 35


def test_base_solver_fallback_minimizes_reported_deviation() -> None:
    identities = sorted(
        range(100, 135), key=lambda repo_id: (corpus50.priority_key("base", repo_id), repo_id)
    )
    candidates = [
        {
            "repo_id": repo_id,
            "priority_key": corpus50.priority_key("base", repo_id),
            "base_rank": rank,
            "language_stratum": "Python",
            "layout_stratum": "single-package tree",
        }
        for rank, repo_id in enumerate(identities, start=1)
    ]
    result = corpus50.solve_base_selection(candidates, active_frame_exhausted=True)
    assert result["first_feasible_base_rank"] is None
    assert result["minimum_total_absolute_margin_deviation"] == 106
    assert len(result["selected"]) == 35


def init_git_repository(path: Path, source: str) -> None:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test User"], check=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "TEST@EXAMPLE.COM"], check=True
    )
    (path / "main.py").write_text(source, encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "main.py"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "fixture"], check=True)


def test_non_english_scan_requires_review_and_cannot_promote(tmp_path: Path) -> None:
    tokens = [f"变量{character}" for character in "甲乙丙丁戊己庚辛壬癸"]
    source = "\n".join(
        [*[f"{token} = 1" for token in tokens], *[f"print({token})" for token in tokens]]
    )
    repository = tmp_path / "repo"
    init_git_repository(repository, source + "\n")
    scan = corpus50.scan_non_english_identifiers(repository)
    assert scan["passed"] is None
    assert scan["machine_pass"] is True
    assert scan["machine_candidate_token_count"] == 10
    reviewed = corpus50.finalize_non_english_review(scan, tokens)
    assert reviewed["passed"] is True
    with pytest.raises(corpus50.Corpus50Error, match="cannot promote"):
        corpus50.finalize_non_english_review(scan, [*tokens, "不存在"])


def test_low_author_uses_mailmap_and_casefolded_email(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    init_git_repository(repository, "x = 1\n")
    result = corpus50.evaluate_low_author_predicate(repository)
    assert result["passed"] is True
    assert result["unique_identity_count"] == 1
    assert result["identities"][0]["casefolded_email"] == "test@example.com"
