"""Shared MARC constants and record types for marc_repair (no logic; imports nothing
from the other marc_repair modules)."""

from __future__ import annotations

from dataclasses import dataclass, field


SUBFIELD = "\x1f"


FIELDTERM = "\x1e"


RECTERM = "\x1d"


# Known MARC21 bibliographic subfield codes per tag. Not exhaustive -- extend as
# needed. Tags not listed fall back to FALLBACK_CODES (permissive: any lowercase
# letter or digit), which is more likely to produce ambiguous splits.
KNOWN_SUBFIELD_CODES: dict[str, set[str]] = {
    "010": set("abz"),
    "015": set("a2"),
    "020": set("acz"),
    "022": set("ayz"),
    "024": set("a2z"),
    "035": set("az9"),
    "040": set("abcde"),
    "041": set("abdefghjkmn2"),
    "043": set("ac2"),
    "049": set("a"),
    "050": set("ab"),
    "082": set("a2"),
    "088": set("az"),
    "090": set("ab"),
    "100": set("abcdq"),
    "110": set("ab"),
    "111": set("acdn"),
    "130": set("a"),
    "240": set("a"),
    "245": set("abcfghknps"),
    "246": set("abfi"),
    "250": set("a"),
    "254": set("a"),
    "255": set("ab"),
    "260": set("abc"),
    "263": set("a"),
    "300": set("abc"),
    "306": set("a"),
    "310": set("a"),
    "336": set("ab2"),
    "337": set("ab2"),
    "338": set("ab2"),
    "362": set("a"),
    "440": set("av"),
    "490": set("a"),
    "500": set("a"),
    "501": set("a"),
    "502": set("abcdgo"),
    "504": set("a"),
    "505": set("a"),
    "506": set("a"),
    "508": set("a"),
    "510": set("abc"),
    "511": set("a"),
    "515": set("a"),
    "520": set("a"),
    "521": set("a"),
    "522": set("a"),
    "524": set("a"),
    "525": set("a"),
    "526": set("a"),
    "530": set("a"),
    "533": set("abcdefmn"),
    "534": set("abc"),
    "538": set("a"),
    "541": set("a"),
    "546": set("a"),
    "550": set("a"),
    "555": set("a"),
    "561": set("a"),
    "563": set("a"),
    "580": set("a"),
    "581": set("a"),
    "586": set("a"),
    "600": set("abcdqt"),
    "610": set("ab"),
    "611": set("a"),
    "630": set("a"),
    "648": set("ay"),
    "650": set("abcdegvxyz012"),
    "651": set("axyz"),
    "653": set("a"),
    "654": set("abey"),
    "655": set("aby2vxyz"),
    "700": set("abcdejqt4"),
    "710": set("ab"),
    "711": set("acdn"),
    "730": set("a"),
    "740": set("a"),
    "752": set("abcd"),
    "760": set("atw"),
    "762": set("atw"),
    "765": set("atw"),
    "767": set("atw"),
    "770": set("atw"),
    "772": set("atw"),
    "773": set("atw"),
    "774": set("atw"),
    "775": set("atw"),
    "776": set("acwz"),
    "777": set("atw"),
    "780": set("atw"),
    "785": set("atw"),
    "786": set("atw"),
    "787": set("atw"),
    "800": set("abcdqt"),
    "810": set("ab"),
    "811": set("acdn"),
    "830": set("av"),
    "852": set("abchj"),
    "856": set("3uyz"),
    "994": set("ab"),
    "999": set("acdefgilmnprstuwx"),
}


FALLBACK_CODES = set("abcdefghijklmnopqrstuvwxyz0123456789")


CONTROL_TAG_LIMIT = 10  # tags below "010" are control fields: no indicators/subfields


# A MARC directory entry is 12 chars; even a record with an unrealistically
# large 2000 fields would need under 24,000 chars of directory. Bounding the
# slice handed to parse_directory() to this matters a lot in practice: when
# scanning a whole multi-record file at an absolute position, slicing
# "everything from here to the end of the file" to look for a directory
# that's actually only ~200 bytes long turns an O(1) lookup into an O(file
# size) one -- and since that slice is taken once per record, the whole
# scan becomes O(records * file size) instead of O(file size).
MAX_DIRECTORY_SCAN_LEN = 24_000


class RepairError(Exception):
    """Raised when a record can't be parsed at all (leader/directory corrupt)."""


class Ambiguous(Exception):
    """A field's subfield split isn't unique; needs a manual override."""

    def __init__(self, tag: str, content: str, candidates: list[list[tuple[str, str]]]):
        self.tag = tag
        self.content = content
        self.candidates = candidates
        super().__init__(f"={tag}: {len(candidates)} possible subfield splits for {content!r}")


@dataclass
class DirEntry:
    tag: str
    length: int
    start: int


@dataclass
class Field_:
    tag: str
    indicators: str | None  # None for control fields
    subfields: list[tuple[str, str]] | None  # None for control fields
    content: str | None = None  # control-field raw content

    def is_control(self) -> bool:
        return self.indicators is None

    def delimited(self) -> str:
        if self.is_control():
            return (self.content or "") + FIELDTERM
        body = self.indicators + "".join(
            SUBFIELD + code + data for code, data in self.subfields
        )
        return body + FIELDTERM


@dataclass
class ParsedRecord:
    leader: str
    entries: list[DirEntry]
    fields: list[Field_] = field(default_factory=list)
    unresolved: list[tuple[int, DirEntry, str, Exception]] = field(default_factory=list)
    # (tag, spaces_added, original_indicators, first_subfield_preview) for
    # each field corrected during parsing itself (currently only the
    # default-on indicator-padding fix, see --no-fix-bad-indicators, does
    # this) -- unlike the other fixes, which run as a separate pass after
    # parsing and are logged directly by the caller, this correction has
    # to happen inside Mode 1's own parse, so it's carried here for the
    # caller to log. original_indicators is whatever was actually there
    # before padding (0 or 1 characters); first_subfield_preview is the
    # first subfield's own code + up to ~10 characters of its data, so a
    # human reading the log can tell which field this was without
    # re-opening the record.
    indicator_fixes: list[tuple[str, int, str, str]] = field(default_factory=list)
    # (tag, detail) for each field appended by reattach_orphaned_trailing_
    # fields -- that runs as a wrapper around the whole record stream
    # (after this record was already fully parsed), not as part of Mode
    # 1/2 parsing itself, but is carried here the same way indicator_fixes
    # is so the caller (main()) can log it without the wrapper needing to
    # know anything about logging.
    reattached_orphaned_fields: list[tuple[str, str]] = field(default_factory=list)
    # Fields whose real, delimiter-bounded content turned out longer than
    # ISO 2709's 4-digit directory length slot can ever declare (max 9999
    # bytes) -- there is no valid way to write such a field back out, so
    # `_read_intact_at` shrinks it, first losslessly/near-losslessly by
    # collapsing redundant MARC-8 escapes (see `_collapse_oversized_content`)
    # and, only if that isn't enough, by truncating. Carried here the same
    # way indicator_fixes is so the caller can log them.
    # (tag, bytes_before, bytes_after, escape_pairs_stripped, preview)
    collapsed_oversized_fields: list[tuple[str, int, int, int, str]] = field(
        default_factory=list
    )
    # (tag, bytes_before, bytes_after, dropped_tail_preview)
    truncated_oversized_fields: list[tuple[str, int, int, str]] = field(default_factory=list)


@dataclass
class LogEntry:
    """One line destined for the run's combined log. `category` is a
    stable machine-readable label (e.g. "added_default_008", "field_removed_because_missing_a")
    used to group same-type entries together within their fixed/not-fixed
    block (see `write_log`); `fixed` says which block."""

    category: str
    fixed: bool
    ts: str
    record_idx: int
    record_id: str
    detail: str

    def render(self) -> str:
        # The section tag and category name are deliberately omitted
        # here (leaving just the one leading tab, as a visual indent):
        # both are already stated once in this category's own
        # "=== SECTION: category ===" header directly above every
        # group of its rows (see `write_log`), so repeating either on
        # every single row is pure noise for a human reading the log.
        rec = f"record {self.record_idx}"
        if self.record_id:
            rec += f" ({self.record_id})"
        return f"\t{rec}\t{self.detail}"
