"""Pure helpers for the hazard invalidation join.

Coordinates are 1-based, half-open line intervals throughout.  A read of
lines 10 through 12 is therefore represented as ``(10, 13)``.  A pure
insertion is a zero-width anchor in the old file: anchor 10 means "before old
line 10".

The helpers deliberately distinguish destructive overlap (old lines that the
reader saw were deleted or replaced), an insertion strictly inside a read
window, and an insertion on a read-window boundary.  Callers can consequently
report a stable primary convention and a boundary-sensitive check.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import ntpath
import os
import re
from typing import Iterable, Mapping, Sequence


Interval = tuple[int, int]

_NUMBERED_READ_LINE = re.compile(r"^\s*(\d+)(?:\t|\N{RIGHTWARDS ARROW})(.*)$")
_NO_NEWLINE_MARKER = "\\ No newline at end of file"


def line_signature(line: str) -> str:
    """Return a deterministic SHA-256 signature for one logical line."""

    if not isinstance(line, str):
        raise TypeError("line must be a string")
    return hashlib.sha256(line.encode("utf-8", errors="surrogatepass")).hexdigest()


def normalize_windows_path(path: str | os.PathLike[str], cwd: str | None = None) -> str:
    """Normalize a Windows path deterministically on any host OS.

    Relative paths are resolved only when ``cwd`` is supplied.  The function
    intentionally does not expand environment variables or user-home syntax:
    transcript paths must not depend on the machine running the analysis.
    """

    value = os.fspath(path)
    if not isinstance(value, str):
        raise TypeError("path must resolve to text")
    if not value:
        raise ValueError("path must not be empty")

    value = value.replace("/", "\\")
    lower = value.lower()
    if lower.startswith("\\\\?\\unc\\"):
        value = "\\\\" + value[8:]
    elif lower.startswith("\\\\?\\"):
        value = value[4:]

    if cwd is not None and not ntpath.isabs(value):
        base = os.fspath(cwd)
        if not isinstance(base, str) or not base:
            raise ValueError("cwd must be non-empty text")
        value = ntpath.join(base.replace("/", "\\"), value)

    return ntpath.normcase(ntpath.normpath(value))


@dataclass(frozen=True)
class ReadWindow:
    """A consecutive window recovered from the visible numbered Read result."""

    start_line: int
    lines: tuple[str, ...]
    signatures: tuple[str, ...] = field(init=False)

    def __post_init__(self) -> None:
        if isinstance(self.start_line, bool) or self.start_line < 1:
            raise ValueError("start_line must be a positive integer")
        if not self.lines:
            raise ValueError("a read window must contain at least one line")
        if any(not isinstance(line, str) for line in self.lines):
            raise TypeError("read-window lines must be strings")
        object.__setattr__(
            self, "signatures", tuple(line_signature(line) for line in self.lines)
        )

    @property
    def num_lines(self) -> int:
        return len(self.lines)

    @property
    def end_line(self) -> int:
        return self.start_line + self.num_lines

    @property
    def interval(self) -> Interval:
        return (self.start_line, self.end_line)


def parse_numbered_read_window(
    visible_result: str,
    *,
    expected_start: int | None = None,
    expected_num_lines: int | None = None,
) -> ReadWindow:
    """Parse Claude Code's visible ``number<TAB>text`` Read rendering.

    Older renderings use a rightwards arrow instead of a tab.  Non-numbered
    wrapper text (for example, a system reminder) is ignored, but every
    numbered line found must belong to one consecutive sequence.  Optional
    expected values let callers cross-check the structured result metadata.
    """

    if not isinstance(visible_result, str):
        raise TypeError("visible_result must be a string")

    numbered: list[tuple[int, str]] = []
    for rendered_line in visible_result.splitlines():
        match = _NUMBERED_READ_LINE.match(rendered_line)
        if match:
            numbered.append((int(match.group(1)), match.group(2)))

    if not numbered:
        raise ValueError("visible Read result contains no numbered lines")

    start = numbered[0][0]
    for offset, (line_number, _) in enumerate(numbered):
        wanted = start + offset
        if line_number != wanted:
            raise ValueError(
                f"numbered Read result is not consecutive: expected {wanted}, "
                f"found {line_number}"
            )

    window = ReadWindow(start, tuple(text for _, text in numbered))
    if expected_start is not None and window.start_line != expected_start:
        raise ValueError(
            f"Read start mismatch: visible={window.start_line}, "
            f"structured={expected_start}"
        )
    if expected_num_lines is not None and window.num_lines != expected_num_lines:
        raise ValueError(
            f"Read length mismatch: visible={window.num_lines}, "
            f"structured={expected_num_lines}"
        )
    return window


@dataclass(frozen=True)
class ChangeBlock:
    """One exact contiguous +/- block from a structured patch.

    Context lines never appear in ``old_lines`` or ``new_lines``.  The start
    fields use the complete old and new file coordinate systems respectively.
    """

    old_start: int
    new_start: int
    old_lines: tuple[str, ...]
    new_lines: tuple[str, ...]
    old_no_newline: bool = False
    new_no_newline: bool = False
    old_signatures: tuple[str, ...] = field(init=False)
    new_signatures: tuple[str, ...] = field(init=False)

    def __post_init__(self) -> None:
        for name, value in (("old_start", self.old_start), ("new_start", self.new_start)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if not self.old_lines and not self.new_lines:
            raise ValueError("a change block cannot be empty on both sides")
        if any(not isinstance(line, str) for line in self.old_lines + self.new_lines):
            raise TypeError("change-block lines must be strings")
        object.__setattr__(
            self,
            "old_signatures",
            tuple(line_signature(line) for line in self.old_lines),
        )
        object.__setattr__(
            self,
            "new_signatures",
            tuple(line_signature(line) for line in self.new_lines),
        )

    @property
    def old_end(self) -> int:
        return self.old_start + len(self.old_lines)

    @property
    def new_end(self) -> int:
        return self.new_start + len(self.new_lines)

    @property
    def old_interval(self) -> Interval:
        return (self.old_start, self.old_end)

    @property
    def new_interval(self) -> Interval:
        return (self.new_start, self.new_end)

    @property
    def is_insertion(self) -> bool:
        return not self.old_lines and bool(self.new_lines)

    @property
    def is_deletion(self) -> bool:
        return bool(self.old_lines) and not self.new_lines


def _integer_field(hunk: Mapping[str, object], name: str) -> int:
    value = hunk.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"structuredPatch hunk {name!r} must be an integer")
    return value


def parse_structured_patch(
    structured_patch: Sequence[Mapping[str, object]],
) -> tuple[ChangeBlock, ...]:
    """Extract exact contiguous change blocks from ``structuredPatch``.

    Hunk context is consumed for coordinate accounting but excluded from the
    returned blocks.  Unified-diff no-newline markers are attached to the side
    of the immediately preceding changed line.
    """

    if isinstance(structured_patch, (str, bytes)) or not isinstance(
        structured_patch, Sequence
    ):
        raise TypeError("structured_patch must be a sequence of hunk mappings")

    result: list[ChangeBlock] = []
    for hunk in structured_patch:
        if not isinstance(hunk, Mapping):
            raise TypeError("each structuredPatch hunk must be a mapping")
        old_start = _integer_field(hunk, "oldStart")
        old_count = _integer_field(hunk, "oldLines")
        new_start = _integer_field(hunk, "newStart")
        new_count = _integer_field(hunk, "newLines")
        if min(old_start, old_count, new_start, new_count) < 0:
            raise ValueError("structuredPatch coordinates and counts must be non-negative")

        rendered = hunk.get("lines")
        if isinstance(rendered, (str, bytes)) or not isinstance(rendered, Sequence):
            raise ValueError("structuredPatch hunk lines must be a sequence")

        old_cursor = old_start
        new_cursor = new_start
        block_old_start: int | None = None
        block_new_start: int | None = None
        old_lines: list[str] = []
        new_lines: list[str] = []
        old_no_newline = False
        new_no_newline = False
        previous_prefix: str | None = None

        def flush() -> None:
            nonlocal block_old_start, block_new_start
            nonlocal old_lines, new_lines, old_no_newline, new_no_newline
            if block_old_start is None or block_new_start is None:
                return
            result.append(
                ChangeBlock(
                    old_start=block_old_start,
                    new_start=block_new_start,
                    old_lines=tuple(old_lines),
                    new_lines=tuple(new_lines),
                    old_no_newline=old_no_newline,
                    new_no_newline=new_no_newline,
                )
            )
            block_old_start = None
            block_new_start = None
            old_lines = []
            new_lines = []
            old_no_newline = False
            new_no_newline = False

        for patch_line in rendered:
            if not isinstance(patch_line, str):
                raise TypeError("structuredPatch lines must be strings")
            if patch_line == _NO_NEWLINE_MARKER:
                if previous_prefix == "-" and block_old_start is not None:
                    old_no_newline = True
                elif previous_prefix == "+" and block_old_start is not None:
                    new_no_newline = True
                # A marker after a context line describes unchanged material,
                # which is intentionally outside an exact change block.
                continue
            if not patch_line or patch_line[0] not in {" ", "+", "-"}:
                raise ValueError(f"unsupported structuredPatch line: {patch_line!r}")

            prefix, text = patch_line[0], patch_line[1:]
            if prefix == " ":
                flush()
                old_cursor += 1
                new_cursor += 1
            else:
                if block_old_start is None:
                    block_old_start = old_cursor
                    block_new_start = new_cursor
                if prefix == "-":
                    old_lines.append(text)
                    old_cursor += 1
                else:
                    new_lines.append(text)
                    new_cursor += 1
            previous_prefix = prefix
        flush()

        consumed_old = old_cursor - old_start
        consumed_new = new_cursor - new_start
        if consumed_old != old_count or consumed_new != new_count:
            raise ValueError(
                "structuredPatch hunk count mismatch: "
                f"declared old/new={old_count}/{new_count}, "
                f"parsed={consumed_old}/{consumed_new}"
            )

    return tuple(result)


def _validate_interval(interval: Interval) -> None:
    if (
        not isinstance(interval, tuple)
        or len(interval) != 2
        or any(isinstance(value, bool) or not isinstance(value, int) for value in interval)
    ):
        raise TypeError("intervals must be (int, int) tuples")
    start, end = interval
    if start < 1 or end < start:
        raise ValueError("intervals must be 1-based and half-open with end >= start")


def union_intervals(intervals: Iterable[Interval]) -> tuple[Interval, ...]:
    """Return sorted, merged non-empty intervals; adjacent intervals merge."""

    materialized: list[Interval] = []
    for interval in intervals:
        _validate_interval(interval)
        if interval[0] != interval[1]:
            materialized.append(interval)
    if not materialized:
        return ()

    materialized.sort()
    merged: list[Interval] = [materialized[0]]
    for start, end in materialized[1:]:
        prior_start, prior_end = merged[-1]
        if start <= prior_end:
            merged[-1] = (prior_start, max(prior_end, end))
        else:
            merged.append((start, end))
    return tuple(merged)


def intervals_overlap(left: Interval, right: Interval) -> bool:
    """Return whether two non-empty half-open intervals intersect."""

    _validate_interval(left)
    _validate_interval(right)
    return left[0] < right[1] and right[0] < left[1]


@dataclass(frozen=True)
class OverlapResult:
    """Three non-conflated forms of read/write region contact."""

    destructive: bool = False
    internal_insertion: bool = False
    boundary_insertion: bool = False

    @property
    def strict(self) -> bool:
        """Primary overlap: destructive contact or a strictly internal insertion."""

        return self.destructive or self.internal_insertion

    @property
    def boundary_sensitive(self) -> bool:
        """Sensitivity overlap that additionally includes edge insertions."""

        return self.strict or self.boundary_insertion


def classify_change_overlap(
    tracked_intervals: Iterable[Interval], changes: Sequence[ChangeBlock]
) -> OverlapResult:
    """Classify exact patch contact with tracked old-file intervals."""

    tracked = union_intervals(tracked_intervals)
    destructive = False
    internal = False
    boundary = False
    for change in changes:
        if change.old_lines:
            old_interval = change.old_interval
            if any(intervals_overlap(interval, old_interval) for interval in tracked):
                destructive = True
        elif change.new_lines:
            anchor = change.old_start
            for start, end in tracked:
                if start < anchor < end:
                    internal = True
                elif anchor == start or anchor == end:
                    boundary = True
    return OverlapResult(destructive, internal, boundary)


@dataclass(frozen=True)
class TransformResult:
    """Surviving tracked-line locations plus how the patch contacted them."""

    intervals: tuple[Interval, ...]
    overlap: OverlapResult


def _ordered_changes(changes: Sequence[ChangeBlock]) -> tuple[ChangeBlock, ...]:
    ordered = tuple(sorted(changes, key=lambda block: (block.old_start, block.old_end)))
    prior: ChangeBlock | None = None
    running_delta = 0
    for block in ordered:
        if prior is not None:
            if block.old_start < prior.old_end:
                raise ValueError("change blocks overlap in old-file coordinates")
            if block.old_start == prior.old_start:
                raise ValueError("multiple change blocks share an ambiguous old anchor")
        expected_new_start = block.old_start + running_delta
        if block.new_start != expected_new_start:
            raise ValueError(
                "change blocks have inconsistent old/new coordinates: "
                f"expected new_start {expected_new_start}, found {block.new_start}"
            )
        running_delta += len(block.new_lines) - len(block.old_lines)
        prior = block
    return ordered


def transform_intervals_through_changes(
    tracked_intervals: Iterable[Interval], changes: Sequence[ChangeBlock]
) -> TransformResult:
    """Map surviving old-line provenance through one exact patch.

    Old lines deleted or replaced by a block disappear from the result.
    Inserted/replacement lines are not included, because the reader never saw
    them.  Pure insertions inside a tracked interval split the mapped interval
    around the new, untracked material.
    """

    tracked = union_intervals(tracked_intervals)
    ordered = _ordered_changes(changes)
    overlap = classify_change_overlap(tracked, ordered)

    def shift_for_start(position: int) -> int:
        shift = 0
        for block in ordered:
            if block.is_insertion:
                if block.old_start <= position:
                    shift += len(block.new_lines)
            elif block.old_end <= position:
                shift += len(block.new_lines) - len(block.old_lines)
        return shift

    def shift_for_end(position: int) -> int:
        shift = 0
        for block in ordered:
            if block.is_insertion:
                if block.old_start < position:
                    shift += len(block.new_lines)
            elif block.old_end <= position:
                shift += len(block.new_lines) - len(block.old_lines)
        return shift

    mapped: list[Interval] = []
    for start, end in tracked:
        cut_points = {start, end}
        destructive_spans: list[Interval] = []
        for block in ordered:
            if block.old_lines:
                left = max(start, block.old_start)
                right = min(end, block.old_end)
                if left < right:
                    cut_points.update((left, right))
                    destructive_spans.append((left, right))
            elif start < block.old_start < end:
                cut_points.add(block.old_start)

        points = sorted(cut_points)
        for left, right in zip(points, points[1:]):
            if left == right:
                continue
            if any(span_start <= left and right <= span_end for span_start, span_end in destructive_spans):
                continue
            new_start = left + shift_for_start(left)
            new_end = right + shift_for_end(right)
            if new_start < new_end:
                mapped.append((new_start, new_end))

    return TransformResult(union_intervals(mapped), overlap)


def is_exact_inverse_patch(
    forward: Sequence[ChangeBlock], candidate_inverse: Sequence[ChangeBlock]
) -> bool:
    """Return whether ``candidate_inverse`` is an exact structural revert.

    This intentionally accepts only one-for-one inverse change blocks.  A
    semantically equivalent patch with different block grouping is not called
    an exact revert by this conservative predicate.
    """

    if len(forward) != len(candidate_inverse):
        return False
    forward_order = sorted(forward, key=lambda block: (block.new_start, block.new_end))
    inverse_order = sorted(
        candidate_inverse, key=lambda block: (block.old_start, block.old_end)
    )
    for original, inverse in zip(forward_order, inverse_order):
        if inverse.old_start != original.new_start:
            return False
        if inverse.new_start != original.old_start:
            return False
        if inverse.old_signatures != original.new_signatures:
            return False
        if inverse.new_signatures != original.old_signatures:
            return False
        if inverse.old_no_newline != original.new_no_newline:
            return False
        if inverse.new_no_newline != original.old_no_newline:
            return False
    return True


__all__ = [
    "ChangeBlock",
    "Interval",
    "OverlapResult",
    "ReadWindow",
    "TransformResult",
    "classify_change_overlap",
    "intervals_overlap",
    "is_exact_inverse_patch",
    "line_signature",
    "normalize_windows_path",
    "parse_numbered_read_window",
    "parse_structured_patch",
    "transform_intervals_through_changes",
    "union_intervals",
]
