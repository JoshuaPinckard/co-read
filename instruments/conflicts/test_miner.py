from __future__ import annotations

import hashlib
import io
import tempfile
from pathlib import Path

import pytest

from instruments.conflicts.miner import (
    GIT_CONFIG_ARGS,
    Hunk,
    GitResult,
    MINER_SOURCE_SHA256,
    MergeTreeOperationalError,
    MiningError,
    RepositoryLock,
    boundary_contact,
    canonical_json,
    changed_base_ranges,
    classify_path,
    deterministic_git_environment,
    find_tree_entry,
    git_line_body,
    hunk_refinement_products,
    hunk_refinement_total_bytes,
    is_test_path,
    line_offsets,
    marker_regions,
    merge_tree,
    parse_merge_tree_output,
    parse_patch,
    primary_merge_base,
    read_nul_field,
    split_lf_lines,
    stage_blob,
    strict_contact,
    tree_blob,
)


class FakeRepository:
    def __init__(self, result: GitResult):
        self.result = result

    def run(self, _arguments: list[str], *, check: bool = True) -> GitResult:
        return self.result


def test_mining_disables_promisor_lazy_fetches_and_scrubs_ambient_git(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_COMMON_DIR", "ambient-common")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "99")
    environment = deterministic_git_environment()
    assert environment["GIT_NO_LAZY_FETCH"] == "1"
    assert "GIT_COMMON_DIR" not in environment
    assert "GIT_CONFIG_COUNT" not in environment
    assert "advice.submoduleMergeConflict=false" in GIT_CONFIG_ARGS


def test_present_non_gitlink_stage_blob_fails_closed_when_missing() -> None:
    class MissingCatFile:
        def get(self, _oid: str) -> None:
            return None

    entry = {"blob": "a" * 40, "mode": "100644"}
    with pytest.raises(MiningError, match="stage blob.*missing"):
        stage_blob(MissingCatFile(), entry)  # type: ignore[arg-type]


def test_gitlink_stage_is_structural_without_blob_lookup() -> None:
    class ExplodingCatFile:
        def get(self, _oid: str) -> None:
            raise AssertionError("gitlink must not be read as a blob")

    entry = {"blob": "a" * 40, "mode": "160000"}
    assert stage_blob(ExplodingCatFile(), entry) is None  # type: ignore[arg-type]


def test_primary_merge_base_reports_unrelated_histories() -> None:
    repository = FakeRepository(GitResult(("merge-base",), 1, b"", b""))
    assert primary_merge_base(repository, "a" * 40, "b" * 40) is None  # type: ignore[arg-type]


def test_merge_tree_operational_error_retains_raw_streams() -> None:
    result = GitResult(("merge-tree",), 128, b"partial", b"fatal: unrelated\n")
    repository = FakeRepository(result)
    with pytest.raises(MergeTreeOperationalError) as captured:
        merge_tree(repository, "a" * 40, "b" * 40)  # type: ignore[arg-type]
    assert captured.value.returncode == 128
    assert captured.value.stdout == b"partial"
    assert captured.value.stderr == b"fatal: unrelated\n"


def test_parse_nul_merge_tree_messages_and_stages() -> None:
    tree = b"5" * 40
    base = b"1" * 40
    ours = b"2" * 40
    theirs = b"3" * 40
    output = b"\0".join(
        [
            tree,
            b"100644 " + base + b" 1\tspace name.txt",
            b"100644 " + ours + b" 2\tspace name.txt",
            b"100644 " + theirs + b" 3\tspace name.txt",
            b"",
            b"1",
            b"space name.txt",
            b"CONFLICT (contents)",
            b"CONFLICT (content): Merge conflict in space name.txt\n",
            b"",
        ]
    )
    result_tree, stages, messages = parse_merge_tree_output(output)
    assert result_tree == tree.decode()
    assert [entry["stage"] for entry in stages] == [1, 2, 3]
    assert {entry["path"] for entry in stages} == {"space name.txt"}
    assert messages == [
        {
            "message": "CONFLICT (content): Merge conflict in space name.txt\n",
            "paths": ["space name.txt"],
            "type": "CONFLICT (contents)",
        }
    ]


def test_parse_clean_merge_tree_output() -> None:
    tree, stages, messages = parse_merge_tree_output(b"a" * 40 + b"\0\0")
    assert tree == "a" * 40
    assert stages == []
    assert messages == []


def test_parse_merge_tree_requires_terminal_nul_and_unique_stages() -> None:
    with pytest.raises(MiningError, match="terminal NUL"):
        parse_merge_tree_output(b"a" * 40)

    tree = b"a" * 40
    stage = b"100644 " + b"b" * 40 + b" 1\tfile.txt"
    with pytest.raises(MiningError, match="duplicate merge-tree stage"):
        parse_merge_tree_output(b"\0".join([tree, stage, stage, b"", b""]))


def test_merge_tree_exit_status_must_agree_with_conflict_listing() -> None:
    tree = b"a" * 40
    stage = b"100644 " + b"b" * 40 + b" 1\tfile.txt"
    clean_with_stage = b"\0".join([tree, stage, b"", b""])
    with pytest.raises(MiningError, match="exit 0 emitted"):
        merge_tree(
            FakeRepository(GitResult(("merge-tree",), 0, clean_with_stage, b"")),
            "1" * 40,
            "2" * 40,
        )  # type: ignore[arg-type]

    with pytest.raises(MiningError, match="exit 1 emitted no conflict paths"):
        merge_tree(
            FakeRepository(GitResult(("merge-tree",), 1, tree + b"\0\0", b"")),
            "1" * 40,
            "2" * 40,
        )  # type: ignore[arg-type]


def test_marker_regions_use_result_blob_coordinates() -> None:
    blob = (
        b"before\n"
        b"<<<<<<< ours\n"
        b"left\n"
        b"=======\n"
        b"right\n"
        b">>>>>>> theirs\n"
        b"after\n"
    )
    status, regions = marker_regions(blob, "f" * 40)
    assert status == "measured_text_markers"
    assert regions == [
        {
            "blob_oid": "f" * 40,
            "blob_size": len(blob),
            "byte_end": len(blob) - len(b"after\n"),
            "byte_start": len(b"before\n"),
            "coordinate_space": "merge-tree-result-blob",
            "includes_marker_lines": True,
            "line_end": 6,
            "line_start": 2,
            "marker_size": 7,
        }
    ]


def test_marker_regions_respect_tracked_marker_size() -> None:
    blob = b"<<<<<<<<< ours\na\n=========\nb\n>>>>>>>>> theirs"
    status, regions = marker_regions(blob, "e" * 40)
    assert status == "measured_text_markers"
    assert regions[0]["marker_size"] == 9
    assert regions[0]["byte_end"] == len(blob)


def test_lf_only_line_semantics_preserve_other_control_bytes_and_extra_cr() -> None:
    blob = b"a\x0bb\x0cc\x1ed\nz\r\nlast"
    assert split_lf_lines(blob) == [b"a\x0bb\x0cc\x1ed\n", b"z\r\n", b"last"]
    assert line_offsets(blob) == [0, 8, 11, 15]
    assert git_line_body(b"=======\r\r\n") == b"=======\r"
    assert git_line_body(b"=======\r\n") == b"======="
    assert git_line_body(b"=======\x0b") == b"=======\x0b"


def test_marker_regions_use_lf_offsets_with_control_bytes() -> None:
    prefix = b"prefix\x0b\x0c\x1e\n"
    blob = (
        prefix
        + b"<<<<<<< ours\r\n"
        + b"left\r\n"
        + b"=======\r\n"
        + b"right\r\n"
        + b">>>>>>> theirs\r\n"
    )
    status, regions = marker_regions(blob, "c" * 40)
    assert status == "measured_text_markers"
    assert regions[0]["byte_start"] == len(prefix)
    assert regions[0]["byte_end"] == len(blob)
    assert regions[0]["line_start"] == 2
    assert regions[0]["line_end"] == 6


def test_byte_refinement_detects_adjacent_not_overlapping_click_edits() -> None:
    base = b'version = "8.4.2.dev"\n'
    parent1 = b'version = "8.5.0.dev"\n'
    parent2 = b'version = "8.4.2"\n'
    hunk = Hunk(old_start=1, old_count=1, new_start=1, new_count=1)
    intervals1, anchors1 = changed_base_ranges(base, parent1, [hunk])
    intervals2, anchors2 = changed_base_ranges(base, parent2, [hunk])
    assert intervals1 == [(13, 14), (15, 16)]
    assert intervals2 == [(16, 20)]
    assert anchors1 == anchors2 == []
    assert not strict_contact(intervals1, anchors1, intervals2, anchors2)
    assert boundary_contact(intervals1, anchors1, intervals2, anchors2)


def test_insertions_at_same_anchor_are_strict_overlap() -> None:
    base = b"a\n"
    hunk = Hunk(old_start=1, old_count=0, new_start=2, new_count=1)
    ranges1, anchors1 = changed_base_ranges(base, b"a\nx\n", [hunk])
    ranges2, anchors2 = changed_base_ranges(base, b"a\ny\n", [hunk])
    assert ranges1 == ranges2 == []
    assert anchors1 == anchors2 == [2]
    assert strict_contact(ranges1, anchors1, ranges2, anchors2)


def test_hunk_refinement_product_is_computed_without_byte_matching() -> None:
    base = b"a" * 200_000 + b"\n"
    side = b"b" * 200_000 + b"\n"
    hunk = Hunk(old_start=1, old_count=1, new_start=1, new_count=1)
    assert hunk_refinement_products(base, side, [hunk]) == [len(base) * len(side)]


def test_hunk_refinement_total_bytes_bounds_one_sided_insertions() -> None:
    hunk = Hunk(old_start=1, old_count=0, new_start=1, new_count=1)
    assert hunk_refinement_products(b"", b"inserted\n", [hunk]) == [0]
    assert hunk_refinement_total_bytes(b"", b"inserted\n", [hunk]) == [9]


def test_patch_parser_handles_spaces_and_counts_hunks() -> None:
    patch = (
        b"diff --git a/path with space.txt b/path with space.txt\n"
        b"index 1111111..2222222 100644\n"
        b"--- a/path with space.txt\n"
        b"+++ b/path with space.txt\n"
        b"@@ -3,2 +3,4 @@\n"
        b"-a\n-b\n+c\n+d\n+e\n+f\n"
    )
    files = parse_patch(patch)
    assert len(files) == 1
    assert files[0].path == "path with space.txt"
    assert files[0].hunks == [Hunk(3, 2, 3, 4)]


def test_classification_priority_and_test_rule() -> None:
    assert classify_path("vendor/package-lock.json", [])["kind"] == "lockfile"
    assert classify_path("third_party/src/code.c", [])["kind"] == "vendored"
    assert classify_path("src/generated/code.go", [])["kind"] == "generated"
    generated = classify_path(
        "src/code.go", [("a" * 40, b"// Code generated by tool. DO NOT EDIT.\n")]
    )
    assert generated["kind"] == "generated"
    assert classify_path("src/code.go", [])["kind"] == "handwritten"
    assert is_test_path("src/test/java/org/example/WidgetTest.java")
    assert is_test_path("pkg/widget_test.go")
    assert is_test_path("crates/search/widget_test.rs")
    assert not is_test_path("src/widget.go")


def test_git_paths_treat_backslash_as_a_literal_character() -> None:
    assert classify_path(r"vendor\package-lock.json", [])["kind"] == "handwritten"
    assert classify_path(r"src\generated\code.go", [])["kind"] == "handwritten"
    assert not is_test_path(r"pkg\test\widget.py")


def test_nul_field_reader_preserves_newline_and_carriage_return_bytes() -> None:
    stream = io.BytesIO(b"path\nwith\rcontrols\0next")
    assert read_nul_field(stream) == b"path\nwith\rcontrols"
    assert stream.read() == b"next"


def test_literal_tree_lookup_supports_newline_paths_and_fails_closed() -> None:
    tree_oid = "a" * 40
    blob_oid = "b" * 40
    payload = b"100644 weird\nname\0" + bytes.fromhex(blob_oid)

    class FakeCatFile:
        def get_with_oid(self, specification: str):
            if specification == tree_oid:
                return tree_oid, "tree", payload
            return None

        def get(self, specification: str):
            if specification == blob_oid:
                return "blob", b"contents"
            return None

    cat_file = FakeCatFile()
    assert find_tree_entry(cat_file, tree_oid, b"weird\nname") == ("100644", blob_oid)  # type: ignore[arg-type]
    assert tree_blob(cat_file, tree_oid, "weird\nname") == (blob_oid, b"contents")  # type: ignore[arg-type]

    class WrongTypeCatFile(FakeCatFile):
        def get(self, specification: str):
            if specification == blob_oid:
                return "commit", b"not a blob"
            return None

    with pytest.raises(MiningError, match="not blob"):
        tree_blob(WrongTypeCatFile(), tree_oid, "weird\nname")  # type: ignore[arg-type]


def test_canonical_json_is_byte_stable() -> None:
    first = canonical_json({"z": "é", "a": [2, 1]}) + "\n"
    second = canonical_json({"a": [2, 1], "z": "é"}) + "\n"
    assert first == second
    assert hashlib.sha256(first.encode()).hexdigest() == hashlib.sha256(second.encode()).hexdigest()
    assert len(MINER_SOURCE_SHA256) == 64
    int(MINER_SOURCE_SHA256, 16)


def test_repository_lock_rejects_a_second_writer_and_recovers() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        with RepositoryLock(root, "example__repo"):
            with pytest.raises(MiningError, match="another miner"):
                with RepositoryLock(root, "example__repo"):
                    pass
        with RepositoryLock(root, "example__repo"):
            pass
