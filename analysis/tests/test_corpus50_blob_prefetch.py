from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from analysis import corpus50


class RecordingGuard:
    def __init__(self) -> None:
        self.calls = 0

    def check(self) -> dict[str, int]:
        self.calls += 1
        return {"accounted_bytes": 0}


def blob_entries(object_ids: list[str]) -> list[corpus50.TreeEntry]:
    return [
        corpus50.TreeEntry("100644", "blob", object_id, f"source-{index}.py")
        for index, object_id in enumerate(object_ids)
    ]


def test_missing_blob_probe_disables_lazy_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local_oid = "1" * 40
    missing_oid = "2" * 40
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append((command, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout=(f"{local_oid} blob 17\n{missing_oid} missing\n").encode(),
            stderr=b"",
        )

    monkeypatch.setattr(corpus50.subprocess, "run", fake_run)
    assert corpus50._missing_blob_object_ids(tmp_path, [local_oid, missing_oid]) == [
        missing_oid
    ]
    command, kwargs = calls[0]
    assert command == ["git", "-C", str(tmp_path), "cat-file", "--batch-check"]
    assert kwargs["input"] == f"{local_oid}\n{missing_oid}\n".encode()
    assert kwargs["env"]["GIT_NO_LAZY_FETCH"] == "1"  # type: ignore[index]
    assert kwargs["check"] is False


def test_prefetch_deduplicates_and_chunks_exact_promisor_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    object_ids = [f"{index:040x}" for index in range(258)]
    entries = blob_entries([*object_ids, object_ids[7], object_ids[257]])
    probed: list[str] = []
    runs: list[tuple[list[str], dict[str, object]]] = []
    head_calls = 0

    def fake_git_text(repository: Path, arguments: list[str]) -> str:
        nonlocal head_calls
        head_calls += 1
        assert repository == tmp_path.resolve()
        assert arguments == ["rev-parse", "--verify", "HEAD^{commit}"]
        return "a" * 40 + "\n"

    def fake_missing(repository: Path, candidates: list[str]) -> list[str]:
        assert repository == tmp_path.resolve()
        probed.extend(candidates)
        return list(candidates)

    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        runs.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(corpus50, "git_text", fake_git_text)
    monkeypatch.setattr(corpus50, "_missing_blob_object_ids", fake_missing)
    monkeypatch.setattr(corpus50.subprocess, "run", fake_run)
    guard = RecordingGuard()

    corpus50._prefetch_missing_source_blobs(tmp_path, entries, disk_guard=guard)

    assert probed == object_ids
    assert len(runs) == 2
    expected_command = [
        "git",
        "-C",
        str(tmp_path.resolve()),
        "-c",
        "fetch.negotiationAlgorithm=noop",
        "fetch",
        "origin",
        "--no-tags",
        "--no-write-fetch-head",
        "--recurse-submodules=no",
        "--filter=blob:none",
        "--stdin",
    ]
    assert [command for command, _ in runs] == [expected_command, expected_command]
    assert runs[0][1]["input"] == ("\n".join(object_ids[:256]) + "\n").encode()
    assert runs[1][1]["input"] == ("\n".join(object_ids[256:]) + "\n").encode()
    assert all(kwargs["check"] is False for _, kwargs in runs)
    assert guard.calls == 4
    assert head_calls == 3


def test_prefetch_failure_is_detailed_and_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    object_ids = ["3" * 40, "4" * 40]
    runs = 0

    monkeypatch.setattr(corpus50, "git_text", lambda *_args, **_kwargs: "a" * 40)
    monkeypatch.setattr(
        corpus50, "_missing_blob_object_ids", lambda _repository, candidates: candidates
    )

    def failed_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        nonlocal runs
        runs += 1
        return SimpleNamespace(
            returncode=128,
            stdout=b"",
            stderr=b"fatal: remote error: upload-pack: not our ref",
        )

    monkeypatch.setattr(corpus50.subprocess, "run", failed_run)
    guard = RecordingGuard()
    with pytest.raises(corpus50.Corpus50Error) as captured:
        corpus50._prefetch_missing_source_blobs(
            tmp_path, blob_entries(object_ids), disk_guard=guard
        )
    message = str(captured.value)
    assert "chunk 1/1" in message
    assert "failed for 2 objects" in message
    assert f"first={object_ids[0]}" in message
    assert f"last={object_ids[-1]}" in message
    assert "exit=128" in message
    assert "not our ref" in message
    assert runs == 1
    assert guard.calls == 2


def test_prefetch_refuses_changed_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    heads = iter(["a" * 40, "b" * 40])
    monkeypatch.setattr(corpus50, "git_text", lambda *_args, **_kwargs: next(heads))
    monkeypatch.setattr(
        corpus50, "_missing_blob_object_ids", lambda _repository, candidates: candidates
    )
    monkeypatch.setattr(
        corpus50.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout=b"", stderr=b""
        ),
    )
    with pytest.raises(corpus50.Corpus50Error, match="HEAD changed"):
        corpus50._prefetch_missing_source_blobs(
            tmp_path, blob_entries(["5" * 40])
        )


def test_scanner_result_is_identical_with_prefetch_on_local_blobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.name", "Test User"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.email", "test@example.com"],
        check=True,
    )
    source = "变量甲 = 1\n变量乙 = 2\nprint(变量甲)\nprint(变量乙)\n"
    (repository / "main.py").write_text(source, encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "main.py"], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-q", "-m", "fixture"],
        check=True,
    )

    prefetched = corpus50.scan_non_english_identifiers(repository)
    monkeypatch.setattr(
        corpus50, "_prefetch_missing_source_blobs", lambda *_args, **_kwargs: None
    )
    legacy = corpus50.scan_non_english_identifiers(repository)

    assert prefetched == legacy
    assert prefetched["machine_candidate_token_count"] == 2
    assert corpus50.canonical_json_bytes(prefetched["evidence"]) == corpus50.canonical_json_bytes(
        legacy["evidence"]
    )
