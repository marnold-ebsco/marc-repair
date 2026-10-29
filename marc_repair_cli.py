"""Command line, run orchestration, logging and profiles for marc_repair. The top of
the import stack: imports the other marc_repair modules."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import textwrap
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Callable

from marc_repair_common import (
    LogEntry,
    ParsedRecord,
    RepairError,
)
from marc_repair_core import (
    DEFAULT_B2_LEXICON_PATH,
    ESCAPE,
    LEADER_17_ENCODING_LEVEL_VALID_HOLDINGS,
    SOURCE_SYSTEMS,
    _SOURCE_SYSTEM_IDENTIFIER,
    _find_004_issues,
    _sniff_record_types,
    assemble_marc,
    build_marc8_corpus_index,
    check_holdings_record,
    count_records,
    detect_encoding,
    find_dangling_880_links,
    find_duplicate_identifiers,
    find_invalid_indicator_values,
    find_invalid_isbn_issn_checksums,
    find_suspect_hex_encoded_marc8,
    find_suspect_marc8_escapes,
    find_suspicious_fields,
    fix_008_length,
    fix_invalid_bibliographic_level,
    fix_invalid_leader_bytes,
    fix_marc8_b2_umlaut,
    fix_marc8_diacritic_escapes,
    iter_repair_stream,
    load_b2_lexicon,
    missing_identifier_reason,
    read_intact_record,
    reattach_orphaned_trailing_fields,
    record_identifier,
    split_bib_holdings,
    to_mrk,
    transcode_marc8_to_utf8,
    trim_oversized_record,
)
from marc_repair_patching import (
    DEFAULT_852_C_CONTENT,
    DEFAULT_852_LOCATION_CONTENT,
    DEFAULT_HOLDINGS_REQUIRED_A_TAGS_FILE,
    DEFAULT_NON_REPEATABLE_TAGS_FILE,
    DEFAULT_REQUIRED_A_TAGS_FILE,
    DEFAULT_STANDARD_MARC_FIELDS_FILE,
    HOLDINGS_008_LENGTH,
    HOLDINGS_CAPTION_PATTERN_TAGS,
    HOLDINGS_CAPTION_PATTERN_VALID_CODES,
    NON_STANDARD_FIELD_CATEGORY,
    NON_STANDARD_FIELD_DETAIL_LIMIT,
    NonStandardFieldTally,
    _880_REQUIRED_A_TAGS,
    _rewrite_stripping_invalid_tags,
    add_default_008,
    add_default_245,
    add_default_holdings_008,
    add_missing_852c,
    dedupe_010_fields,
    ensure_field,
    find_853_missing_8,
    find_856_missing_u,
    find_and_fix_mojibake,
    fix_852_b_suspect_content,
    fix_852_call_number,
    fix_852_multiple_b,
    fix_invalid_tags,
    fix_misplaced_subfield_codes,
    fix_missing_852_location,
    load_standard_marc_fields,
    load_tag_list,
    merge_duplicate_040,
    move_duplicate_100_to_700,
    move_duplicate_240_to_730,
    move_duplicate_245_to_246,
    normalize_smart_characters,
    normalize_subfield_9_to_0,
    parse_ensure_field_spec,
    pick_unused_9xx_tag,
    recode_6xx_b_to_a,
    recode_856_missing_u,
    recode_misfiled_duplicate_245,
    recode_title_only_7xx_to_730,
    record_missing_245,
    remap_999_to_945,
    split_holdings_multiple_004,
    split_holdings_multiple_852,
    strip_852_duplicate_non_repeatable_subfields,
    strip_duplicate_non_repeatable_fields,
    strip_empty_852_subfields,
    strip_empty_fields,
    strip_invalid_subfield_codes,
    strip_missing_required_a,
    strip_null_identifiers,
    strip_whitespace_only_010_a,
    write_non_standard_supplement,
    write_non_standard_toc,
)


def check_holdings_records(
    path: str, log_path: str, chunk_size: int = 16 * 1024 * 1024
) -> int:
    """Run `check_holdings_record` over every record in a holdings file
    (e.g. one of `split_bib_holdings`'s outputs) and write every issue
    found to its own combined log at `log_path`, grouped the same way as
    the main repair log (see `write_log`) -- everything lands in NOT
    FIXED, since this function only detects, never modifies. For actual
    repair, see `repair_holdings_records`, which the CLI's
    `--split-bib-holdings` uses instead.
    Returns the number of records that had at least one issue.
    """
    encoding = detect_encoding(path)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    log_entries: list[LogEntry] = []
    n_flagged = 0
    idx = 0
    # See the matching comment in `split_bib_holdings` -- a cursor plus
    # periodic compaction avoids re-copying the whole buffer on every
    # single record.
    compact_threshold = 2 * chunk_size

    def _check(raw: bytes) -> None:
        nonlocal n_flagged, idx
        rec_text = raw.decode(encoding)
        issues, record_id = check_holdings_record(rec_text, encoding)
        if issues:
            n_flagged += 1
            for category, detail in issues:
                log_entries.append(LogEntry(category, False, ts, idx, record_id, detail))
        idx += 1

    with open(path, "rb") as fh:
        buf = b""
        pos = 0
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            buf += chunk
            while True:
                term = buf.find(b"\x1d", pos)
                if term == -1:
                    break
                _check(buf[pos:term + 1])
                pos = term + 1
            if pos > compact_threshold:
                buf = buf[pos:]
                pos = 0
        remainder = buf[pos:]
        if remainder:
            _check(remainder)
    if log_entries:
        write_log(log_path, log_entries)
    return n_flagged


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _default_output_path(input_path: str) -> str:
    # Always .mrc regardless of the input's own extension (e.g. .marc,
    # .dat, pasted-text .txt) -- the repaired output is always valid
    # binary MARC, so it should always get the extension that implies
    # that, not whatever the (possibly misleading) input happened to use.
    base, _ext = os.path.splitext(input_path)
    return f"{base}_repaired.mrc"


def _load_overrides(path: str) -> dict[int, dict[int, list[tuple[str, list[tuple[str, str]]]]]]:
    """overrides.json shape:
    {
      "<record_index>": {
        "<field_index>": {"indicators": "  ", "subfields": [["a", "..."], ["b", "..."]]}
      }
    }
    """
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    out: dict[int, dict[int, list]] = {}
    for rec_idx, fields in raw.items():
        out[int(rec_idx)] = {}
        for field_idx, spec in fields.items():
            indicators = spec["indicators"]
            subfields = [(c, d) for c, d in spec["subfields"]]
            out[int(rec_idx)][int(field_idx)] = (indicators, subfields)
    return out


@contextmanager
def _null_writer():
    """A context manager that yields None -- used in place of an optional
    output file (e.g. --mrk not requested) so callers can write `with
    open(...) as fh, (real_or_null) as maybe_fh:` uniformly and just check
    `if maybe_fh:` before writing, without an extra branch for "not
    requested at all"."""
    yield None


def _error_output_path(out_path: str) -> str:
    """Insert "_error" right before `out_path`'s extension -- e.g.
    "bib_repaired.mrc" -> "bib_repaired_error.mrc" -- mirroring
    `_timestamped_log_path`'s own insert-before-extension convention.
    Records this tool concludes it truly can't repair at all (see the
    UNFIXABLE section) are written here, byte-for-byte unchanged,
    instead of the main output -- so a badly mangled record can never
    end up silently mixed into an otherwise-clean load file."""
    base, ext = os.path.splitext(out_path)
    return f"{base}_error{ext}"


def _additional_004_output_path(out_path: str) -> str:
    """Insert "_additional004s" right before `out_path`'s extension --
    e.g. "bib_repaired.mrc" -> "bib_repaired_additional004s.mrc" --
    mirroring `_error_output_path`'s own insert-before-extension
    convention. A holdings record with more than one 004 (Control
    Number -- see `split_holdings_multiple_004`) has every 004 past
    the first split out into its own duplicate record here, instead of
    the main output, so a file that's normally one 004 per holdings
    record stays that way for every downstream consumer -- created
    lazily (see `_LazyBinaryWriter`): a run with no multi-004 records
    never creates an empty file."""
    base, ext = os.path.splitext(out_path)
    return f"{base}_additional004s{ext}"


class _LazyBinaryWriter:
    """A binary file opened only on its first `write()` -- so a run with
    no UNFIXABLE records never creates an empty "_error" file that never
    needed to exist. Usable as its own context manager, alongside the
    other `with open(...) as ...` handles in `main`."""

    def __init__(self, path: str):
        self.path = path
        self._fh = None

    def __enter__(self) -> "_LazyBinaryWriter":
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._fh is not None:
            self._fh.close()

    def write(self, data: bytes) -> None:
        if self._fh is None:
            self._fh = open(self.path, "wb")
        self._fh.write(data)


def _needs_review_output_path(out_path: str) -> str:
    """Insert "_needs_review" right before `out_path`'s extension -- e.g.
    "bib_repaired.mrc" -> "bib_repaired_needs_review.mrc" -- mirroring
    `_error_output_path`'s own insert-before-extension convention. See
    `_divert_to_review`: a record some auto-fix would normally have
    touched, but whose fix is disabled via its own --no-... flag, is
    written here, byte-for-byte unchanged, instead of the main output,
    so a human can decide what to do with it instead of it silently
    missing whatever that fix would have supplied."""
    base, ext = os.path.splitext(out_path)
    return f"{base}_needs_review{ext}"


def _divert_to_review(
    log: Callable[[str, bool, int, str, str], None],
    review_fh: "_LazyBinaryWriter",
    review_path: str,
    rec_text: str,
    encoding_used: str,
    record_idx: int,
    rec_id: str,
    reason: str,
) -> None:
    """Shared by every "--no-fix-X diverts the record instead of leaving
    it half-fixed" check (currently just --no-add-default-245; see
    `main`'s bib loop) -- logs one "diverted_for_review" entry (always
    fixed=False, so it sorts into NOT FIXED -- see `_section_for`) whose
    detail states `reason`, that the record was copied unchanged to
    `review_path`, and the path itself, then writes `rec_text` there
    byte-for-byte unchanged, same as `_error_output_path`'s own
    byte-for-byte UNFIXABLE diversion. A future check that wants this
    same "pull it out for a human instead" behavior just calls this with
    its own `reason` -- no new log category or section registration
    needed, since an unregistered fixed=False category already lands in
    NOT FIXED on its own (see `_section_for`'s docstring)."""
    log(
        "diverted_for_review", False, record_idx, rec_id,
        f"{reason} -- record copied unchanged to {review_path} for manual review "
        "instead of being auto-fixed",
    )
    review_fh.write(rec_text.encode(encoding_used, errors="surrogateescape"))


def _read_text(path: str) -> str:
    """Read a MARC file or pasted-text file. See `_read_text_with_encoding`;
    this just discards which encoding was used, for callers that don't need
    to re-encode anything back to bytes later."""
    return _read_text_with_encoding(path)[0]


def _read_text_with_encoding(path: str) -> tuple[str, str]:
    """Read a MARC file or pasted-text file, returning (text, encoding_used).
    Real .mrc files may be UTF-8 or legacy MARC-8/Latin-1; try UTF-8 first
    (the common case for both pasted text and modern exports) and fall back
    to Latin-1, which never fails and preserves every byte 1:1 -- important
    both because this tool searches for exact control-character byte values,
    and so a record that can't be auto-repaired can still be passed through
    byte-for-byte using the same encoding it was read with."""
    with open(path, "rb") as fh:
        raw = fh.read()
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return raw.decode("latin-1"), "latin-1"


def _format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


#: There is no plain "FIXED" section -- every category that's ever
#: logged with fixed=True is explicitly placed in one of the two
#: sections below (or DUPLICATE RECORDS), so a reader never has to
#: wonder which bucket a given fix landed in.
#:
#: FIXED/REQUIRES ATTENTION: auto-fixed AND still worth a human's
#: attention -- these discard or alter enough real data (or add
#: content a human should double-check) that it's worth a second look
#: even though the record is now loadable. Sits right below NOT
#: FIXED: not as urgent as something left broken, but more urgent
#: than a routine fix.
_FIXED_REQUIRES_ATTENTION = {
    "moved_duplicate_to_added_entry",
    "merged_duplicate_040",
    "removed_non_repeatable_duplicate",
    "fixed_008_length",
    "fixed_holdings_008_length",
    "removed_invalid_subfield",
    "truncated_oversized_field",
    "trimmed_oversized_record",
    "collapsed_oversized_field_escapes",
    "field_removed_because_missing_a",
    "recoded_title_only_7xx_to_730",
    "recoded_6xx_b_to_a",
    "added_field",
    "holdings_leader_byte_defaulted",
    "reattached_orphaned_field",
    "added_missing_852_location",
    "removed_bad_call_number",
    "recoded_852_b_to_i",
    "removed_extra_852_b",
    "removed_empty_852_subfield",
    "holdings_852_duplicate_nr_subfield",
    "split_holdings_multiple_852",
    "added_default_245",
    "added_default_008",
    "padded_indicators",
    "incomplete_852",
    "invalid_bibliographic_level",
    "holdings_852_b_suspect_content",
    "deduped_010_lccn",
    "removed_880_missing_a",
    "removed_untranscodable_subfield",
}


#: INFORMATIONAL, at the very bottom: a fix applied via a fixed
#: default/constant or a systematic, file-wide transformation rather
#: than judgment applied to that record's own content (a placeholder
#: 008/245, a leader byte reset to a default code, $9 promoted to $0,
#: a misplaced subfield code corrected, the leader's entry-map constant
#: restored, an unparseable tag renamed to an unused 9XX slot,
#: typographic punctuation flattened, a record transcoded MARC-8 ->
#: UTF-8, double-encoded UTF-8 corrected, 999 remapped to 945, an
#: oversized record's leader sentinel applied, short indicators padded
#: with spaces) -- or a detect-only finding not urgent enough for NOT
#: FIXED.
_INFORMATIONAL = {
    "added_default_holdings_008",
    "holdings_multiple_004",
    "removed_null_identifier",
    "missing_call_number",
    "leader_byte_defaulted",
    "leader_entry_map_fixed",
    "normalized_subfield_9_to_0",
    "fixed_misplaced_subfield_code",
    "invalid_tag",
    "normalized_smart_characters",
    "transcoded_marc8",
    "dangling_880_link",
    "invalid_isbn_issn_checksum",
    "fixed_mojibake",
    "remapped_999_to_945",
    "oversized_sentinel_fixed",
    "fixed_marc8_diacritic",
    "fixed_marc8_b2_umlaut",
    "removed_010_whitespace_a",
}


#: NEEDS REVIEW: a detect-only finding that's always listed in full
#: (never just header+count) because a cataloger would plausibly want
#: to look at each one individually -- unlike the rest of INFORMATIONAL
#: (a fix applied via a fixed default, or a lower-priority detect-only
#: finding not worth a full per-record listing by default). Sits below
#: FIXED/REQUIRES ATTENTION: nothing was actually touched here, so it's
#: less urgent than something the tool changed, but still worth
#: flagging prominently rather than burying at the bottom with
#: everything else.
_NEEDS_REVIEW = {
    "doubled_proxy_url",
    "invalid_indicator_value",
    "suspect_marc8_escape",
    "suspect_hex_encoded_marc8",
    "transcode_marc8_failed",
    "unfixed_non_numeric_tag",
    "added_missing_852c",
    "holdings_856_missing_u",
    "bib_856_missing_u",
}


#: UNFIXABLE, above even NOT FIXED: a record this tool concluded it
#: genuinely can't repair at all (no consistent directory found by
#: either mode, or a field/base address too large for ISO 2709 to
#: represent) -- see reattach_orphaned_trailing_fields above for the one
#: narrow case that's safely recoverable instead of landing here. Such a
#: record is no longer written into the main output at all: it goes,
#: byte-for-byte unchanged, to a separate "_error" file next to it (see
#: `_error_output_path`), so a badly mangled record can never end up
#: silently mixed into an otherwise-clean load file. Sorted first
#: because it's the one thing a human MUST look at -- everything else
#: in the output is at least loadable as MARC.
_DEDICATED_SECTIONS: dict[str, tuple[int, str]] = {
    "unfixable": (-1, "UNFIXABLE"),
    "record_identifier_required": (-1, "UNFIXABLE"),
    "duplicate_identifier": (3, "DUPLICATE RECORDS"),
    # Below even INFORMATIONAL: report-only, but unlike INFORMATIONAL its
    # entries ARE listed (first few per tag -- see NonStandardFieldTally).
    "non_standard_marc_fields": (5, "INFORMATION ONLY"),
}


for _cat in _FIXED_REQUIRES_ATTENTION:
    _DEDICATED_SECTIONS[_cat] = (1, "FIXED/REQUIRES ATTENTION")


for _cat in _NEEDS_REVIEW:
    _DEDICATED_SECTIONS[_cat] = (2, "NEEDS REVIEW")


for _cat in _INFORMATIONAL:
    _DEDICATED_SECTIONS[_cat] = (4, "INFORMATIONAL")


del _cat


def _section_for(entry: LogEntry) -> tuple[int, str]:
    """(sort_order, section_label) for `entry` -- UNFIXABLE, then NOT
    FIXED, then FIXED/REQUIRES ATTENTION, then NEEDS REVIEW, then any
    other dedicated sections (see `_DEDICATED_SECTIONS`) like DUPLICATE
    RECORDS, then INFORMATIONAL. There is no plain "FIXED" section -- an
    unrecognized category logged with fixed=True falls back to
    INFORMATIONAL rather than a generic bucket, so every new fix
    category must be added to `_FIXED_REQUIRES_ATTENTION` or
    `_INFORMATIONAL` above to land somewhere deliberate."""
    dedicated = _DEDICATED_SECTIONS.get(entry.category)
    if dedicated is not None:
        return dedicated
    return (4, "INFORMATIONAL") if entry.fixed else (0, "NOT FIXED")


def _section_for_category(category: str) -> tuple[int, str]:
    """Same as `_section_for`, but for a bare category name with no
    `LogEntry` to hand -- used by `write_log` to print a category's
    header even when this run found zero matching records (so there's
    no entry to look at). Safe to default an unregistered category to
    NOT FIXED without knowing its `fixed` value: `_section_for`'s own
    docstring notes every category ever logged with fixed=True is
    already registered in `_FIXED_REQUIRES_ATTENTION` or
    `_INFORMATIONAL`, so an unregistered category can only ever be a
    detect-only, fixed=False one."""
    dedicated = _DEDICATED_SECTIONS.get(category)
    return dedicated if dedicated is not None else (0, "NOT FIXED")


#: One-line description of what each category check actually does --
#: printed as the second of the four header lines `write_log` writes
#: for every category active in a given run (see `write_log`), so a
#: reader never has to go dig through this file's docstrings to know
#: what e.g. "holdings_852_b_suspect_content" means. Never itself
#: states a DATA LOSS/POSSIBLE DATA LOSS/NO DATA LOSS call -- that's
#: `_CHECK_DATA_LOSS`'s own job, so every category's call line in the
#: log always looks the same regardless of how long its description
#: runs. Keep in sync with docs/REPAIR_CATEGORIES.md (the two aren't
#: generated from a shared source, but should never say something
#: different).
_CHECK_DESCRIPTIONS: dict[str, str] = {
    "unfixable": "Record has no consistent directory in either parsing mode (or a field/base "
    "address too large for ISO 2709 to represent), or (holdings only) could not "
    "be structurally parsed at all, or a split copy's assembled length can't be "
    "represented in ISO 2709's fixed-width leader/directory -- written "
    "unchanged to the _error file instead of the main output. These records "
    "cannot be loaded. By default, also includes a record whose true length "
    "exceeds 99999 bytes (see oversized_sentinel_fixed): spec-valid MARC21, "
    "but known to abort FOLIO Data Import's entire batch with no job log, so "
    "it's diverted here instead of the main output too, unless --allow-"
    "oversized-sentinel is given.",
    "record_identifier_required": "Bib record has no identifier for its --source-system: "
    "Sierra/iii migrations require a 907 $a (no fallback to 001), any other "
    "system requires a 001. A record without one cannot be loaded -- written "
    "unchanged to the _error file instead of the main output.",
    "diverted_for_review": "A fix that would normally have run was disabled via its own "
    "--no-... flag, and the record it would have applied to was written unchanged to the "
    "_needs_review file instead of the main output, for a human to decide what "
    "to do with it (currently: a record with no 245, with --no-add-default-245 "
    "set).",
    "duplicate_identifier": "The same identifier (usually 001, or 907 $a for Sierra records) is used by "
    "more than one distinct record in this file. (Duplicate 001's may be normal "
    "with records from Voyager.)",
    "moved_duplicate_to_added_entry": "A second 100 (another person's name) was recoded as a 700, a second 245 "
    "or 240 was recoded as the added entry it really is (246/730/740, or a "
    "260/250/240/490 when it was a mistagged publication, edition, uniform title "
    "or series), or a near-duplicate was dropped, instead of being removed as a "
    "duplicate of a non-repeatable field. Only subfields the new field can't "
    "carry ($6, $c, ...) are ever dropped (noted per entry).",
    "merged_duplicate_040": "A record had more than one 040 (Cataloging Source, non-repeatable) -- "
    "the later ones were merged into the first (subfields added, a conflicting $a/$c "
    "demoted to $d) instead of being removed. A 040 that was really a 041 (language "
    "codes) was recoded as one.",
    "removed_non_repeatable_duplicate": "A non-repeatable field (e.g. 001/005/008) appeared more than once on one "
    "record -- all but one copy removed.",
    "field_removed_because_missing_a": "A heading/added-entry field (or a holdings caption field) lacked a "
    "non-empty, non-punctuation-only $a -- the whole field removed.",
    "removed_010_whitespace_a": "A 010 (LCCN) field's $a was present but made up entirely of "
    "whitespace (e.g. a single space). Only the $a subfield is removed. Any $b or $z is "
    "retained.",
    "deduped_010_lccn": "A record had more than one 010 (LCCN) field, or a single 010 with more "
    "than one $a -- merged into exactly one 010. Every $a on the record is compared with its "
    "whitespace stripped: if they all normalize the same, the stripped value is kept; if two "
    "or more normalize to genuinely different values, the best one is kept as $a (a value "
    "that is a valid LCCN under LC's structure rules, https://www.loc.gov/marc/lccn_structure.html, "
    "always beats one that is not; among valid LCCNs the newest year wins, then the highest "
    "serial) and the others are moved to $z (nothing is lost). NOTE: treating the other LCCNs as "
    "canceled/invalid ($z) is a guess -- it has not been verified that they are invalid, "
    "so each should be checked. Every $z "
    "(Canceled/Invalid LCCN) and $b "
    "(NUCMC control number -- rare, treated the same as $z) from every original 010 is "
    "carried onto the surviving field, de-duplicated -- except a $z matching the surviving "
    "$a, which is dropped as a contradiction.",
    "removed_880_missing_a": "An 880 (Alternate Graphic Representation) field whose $a was empty or "
    "effectively empty (e.g. all spaces, or punctuation only) -- the whole "
    "field removed, including any $6 linking data it carried.",
    "removed_invalid_subfield": "A subfield code that isn't a lowercase letter or digit -- the subfield "
    "removed.",
    "removed_untranscodable_subfield": "A subfield's MARC-8 content couldn't be transcoded to UTF-8 (genuinely "
    "malformed bytes) -- the subfield removed (and the whole field with it, if "
    "the removed subfield was $a or nothing usable was left); the rest of the "
    "record still transcodes normally.",
    "removed_bad_call_number": "An 852 $h (call number) that was unusable (e.g. punctuation-only) -- "
    "removed.",
    "removed_extra_852_b": "An 852 had more than one $b (Sublocation) after the first was already "
    "recoded to $i -- the extras removed.",
    "incomplete_852": "One 852 (Location) among several on a holdings record had no $b at all -- "
    "dropped entirely rather than becoming its own split record.",
    "doubled_proxy_url": "A URL subfield (e.g. 856 $u) has a literally duplicated proxy prefix (e.g. "
    "an ezproxy wrapper repeated twice) -- a pre-existing error in the source "
    "record, not something this migration introduced.",
    "non_standard_marc_fields": "A field whose tag isn't in standard_marc_fields.txt (LC's MARC 21 "
    "bibliographic field list plus accepted local fields -- edit that file to add or remove tags) "
    "or is listed there as obsolete ([OBSOLETE]). 9XX fields are never reported. Report only -- "
    "nothing is changed or removed. Each tag is listed separately: its first 5 occurrences in "
    "detail, then \"and N more\".",
    "suspect_marc8_escape": "A MARC-8 script-switching escape (Hebrew/Arabic/Cyrillic/Greek/CJK) "
    "produces only a single character, welded directly between two ASCII "
    "letters with no word boundary -- almost certainly a miskeyed accented "
    "letter (e.g. ö, é, ñ, ü) already present in the source records, not something this migration "
    "introduced, and not real embedded foreign-script text; never auto-fixed, "
    "since there's no safe way to guess the intended character.",
    "suspect_hex_encoded_marc8": 'A field (in practice, always an 880) contains one or more "{xxxxxx}" runs '
    "-- 6 ASCII hex-digit characters wrapped in literal curly braces -- "
    "consistent with real MARC-8 content that got hex-encoded and brace-wrapped "
    "(sometimes twice) in the source records, before this tool ever saw them; "
    "never auto-fixed, since the decode is boundary-sensitive and there's no "
    "safe way to guarantee exact byte alignment automatically. Each finding "
    "says whether its decoded preview looks recovered or shows POSSIBLE DATA "
    "LOSS (doesn't decode to valid MARC-8/text; may be truncated or "
    "boundary-shifted). A recovered preview's byte alignment is not "
    "guaranteed, hence \"verify against source\". Detect-only, though, "
    "so this tool never makes it worse: it's reporting damage that already "
    "happened upstream, before the file ever reached this tool.",
    "holdings_852_b_suspect_content": "An 852 $b (Sublocation) looks like data from a prior system migration that "
    "landed in the wrong subfield (not something introduced by this tool) -- "
    "purely numeric, or containing flattened subfield-delimiter markers -- and "
    "is replaced wholesale with "
    f"{DEFAULT_852_LOCATION_CONTENT!r}.",
    "holdings_853_missing_8": "An 853 (Captions and Pattern) field has no $8 (Field link and sequence "
    "number) -- the 863/864/865 enumeration fields that should reference it "
    "can't be linked.",
    "holdings_856_missing_u": "An 856 (Electronic Location and Access) field has no $u (URI) -- the field "
    "exists but has no actual link. Check to see if the link is in an incorrect subfield.",
    "bib_856_missing_u": "A bib record's 856 (Electronic Location and Access) field has no $u (URI) -- "
    "the field exists but has no actual link. Check to see if the link is in an incorrect subfield. "
    "Where the URL is unambiguous (a lost 'u' code, or a bare http(s) URL alone in $a/$z) it is "
    "recoded to $u; the action taken is in parentheses.",
    "split_holdings_multiple_852": "A holdings record had more than one usable 852 (Location) -- split into "
    'one record per 852, with "-2", "-3", etc. appended to each additional '
    "copy's 001.",
    "unfixed_non_numeric_tag": "A field's tag isn't 3 numeric digits -- normally auto-renamed to an unused "
    "9XX slot (see invalid_tag); this fires only when that never happens. Named "
    "'unfixed' because neither path that lands here actually gives the tag a "
    "valid replacement: --no-fix-invalid-tags skips the rename attempt "
    "entirely, so the invalid tag is left exactly as it was (no loss, but still "
    "won't load in FOLIO); every 9XX slot already taken means there's nowhere "
    "left to rename to, so the whole field is discarded instead of repaired. "
    "This count is 0 unless one of those two conditions actually occurs in this "
    "file.",
    "dangling_880_link": "An 880 (Alternate Graphic Representation) field's $6 linkage doesn't match "
    "any other field's own $6 back-reference.",
    "invalid_isbn_issn_checksum": "An 020/022 $a's check digit doesn't match the rest of the number.",
    "holdings_escape_sequence": "A holdings record contains a raw ESC (0x1B) byte -- likely an "
    "un-transcoded MARC-8 escape sequence (MARC-8-to-UTF-8 conversion is "
    "currently skipped for holdings).",
    "transcode_marc8_failed": "MARC-8-to-UTF-8 transcoding failed for this record's content -- left as "
    "MARC-8, error recorded.",
    "added_default_008": "Record had no 008 -- a fixed generic placeholder was inserted.",
    "added_default_holdings_008": "Holdings record had no 008 -- a fixed generic 32-byte placeholder was "
    "inserted.",
    "added_default_245": 'Record had no 245 -- a placeholder ($a "No title") was inserted.',
    "added_missing_852c": "852 (Location) had no $c (Shelving location) -- a placeholder was inserted "
    "(disable with --no-fix-missing-852c) -- only fills a gap that was already "
    "empty; never overwrites an existing $c. Only runs for a record identified "
    "as OCLC WMS-sourced (see _is_wms_holdings_record), since $c is only that "
    "system's live location subfield.",
    "normalized_subfield_9_to_0": "A $9 subfield was rewritten to $0 (MARC21's standard "
    "authority-control-number code).",
    "normalized_smart_characters": 'Typographic ("smart") quotes/dashes were flattened to their plain-ASCII '
    "equivalents.",
    "transcoded_marc8": "A MARC-8/ANSEL-encoded record was converted to UTF-8 and the leader's "
    "encoding byte flipped to match.",
    "fixed_misplaced_subfield_code": "A space was detected between the subfield delimiter character and the "
    "subfield code. Stray space removed.",
    "removed_null_identifier": "A subfield with no data at all (e.g. a bare $8, or an empty $a immediately "
    "followed by another subfield) was removed.",
    "missing_call_number": "An 852 (Location) had no $h (call number) at all -- left untouched; a call "
    "number can legitimately be absent.",
    "leader_byte_defaulted": "A leader byte (record status/type/bib level/encoding level) held a value "
    "outside MARC21's defined set -- reset to a safe default.",
    "holdings_leader_byte_defaulted": "Same as leader_byte_defaulted, using holdings' own valid-value set.",
    "leader_entry_map_fixed": "Leader bytes 20-23 (the entry map) weren't the MARC21-fixed constant "
    '"4500" -- corrected.',
    "invalid_tag": "A non-numeric tag was renamed to an unused 9XX slot (see "
    "unfixed_non_numeric_tag for when this isn't possible).",
    "invalid_indicator_value": "An indicator held a value outside MARC21's defined set for that field.",
    "invalid_bibliographic_level": "Leader byte 7 (bibliographic level) held a value outside MARC21's defined "
    "set -- defaulted to 'm' (Monograph/Item).",
    "fixed_mojibake": 'Double-encoded UTF-8 ("mojibake") was corrected.',
    "fixed_marc8_b2_umlaut": "A German umlaut vowel destroyed by escape junk + byte 0xB2 was filled in "
    'because a word list confirmed the word (e.g. "G_cke" -> "Göcke"). A '
    "lexicon-backed guess, not verified against the source record; on by "
    "default when a lexicon file is found, off with --no-b2-lexicon.",
    "fixed_marc8_diacritic": "A MARC-8 diacritic mark was lost to either a bogus script-switching escape "
    'sequence around a confirmed-by-volume payload byte (e.g. "Haure" + '
    '<escape> + "au" -> "Hauréau") or a bare, un-escaped byte standing in for '
    'an Arabic transliteration mark, a French/Occitan cedilla, a Russian "soft '
    'sign", a Polish/Lithuanian ogonek, or a German diaeresis (e.g. "al-h" + '
    '<bare byte> + "ujjah" -> "al-ḥujjah") -- the mark was reapplied to (or '
    "reinserted at) the position it was lost from. Also recovers plain ASCII "
    "punctuation/whitespace trapped inside the escape when it and the escape's "
    'closing sequence got swapped (e.g. "Facolta" + <escape> + " " (trapped) + '
    '<close> + "di lettere" -> "Facoltà di lettere"). See suspect_marc8_escape '
    "for the same defect's unconfirmed payloads, left for a human to review.",
    "remapped_999_to_945": 'A 999 field was retagged to 945 (indicators forced to "ff") -- only with '
    "--remap-999-to-945.",
    "trimmed_oversized_record": "A record was over 99999 bytes (the most ISO 2709's leader can "
    "declare) -- whole fields were removed, largest first, until it fit: 9xx "
    "(never a 999 with indicators ff), then 5xx with their linked 880s, then "
    "6xx with theirs. The log lists every removed field. If that still would not "
    "fit, nothing is removed and the record is diverted as unfixable. Disable "
    "with --no-trim-oversized-record.",
    "oversized_sentinel_fixed": "A record's true length exceeds ISO 2709's 5-digit field -- leader declares "
    "the documented 99999 sentinel instead (nothing lost; the real end is still "
    "found from the record terminator). Only happens with --allow-oversized-"
    "sentinel -- by default, a record like this is diverted to the _error file "
    "and logged as unfixable instead (see --allow-oversized-sentinel's own help).",
    "padded_indicators": "A data field had 0 or 1 indicator characters instead of 2 -- padded with "
    "spaces.",
    "holdings_missing_004": "Holdings record has no 004 (link to its bib record) at all.",
    "holdings_multiple_004": "Holdings record has more than one 004 -- not necessarily wrong (can "
    "legitimately link to more than one bib record). The first is kept here; each "
    "additional one becomes its own duplicate holdings record in a separate "
    "_additional004s file.",
    "holdings_852_duplicate_nr_subfield": "852 (Location) had a Not-Repeatable subfield (e.g. $h) more than once -- "
    "every occurrence after the first removed.",
    "fixed_008_length": "008 wasn't exactly 40 characters -- padded (no loss) or truncated "
    "(discards the trailing bytes past 40) to fit.",
    "fixed_holdings_008_length": "Holdings 008 wasn't exactly 32 characters -- padded (no loss) or truncated "
    "(discards the trailing bytes past 32) to fit.",
    "added_field": "A field required via --ensure-field was missing entirely -- inserted with "
    "the given content.",
    "added_missing_852_location": "852 (Location) had none of $a/$b/$c -- a placeholder was inserted so the "
    "field means something.",
    "reattached_orphaned_field": "A field that had drifted outside its record's own boundaries was "
    "reattached to the record it actually belongs to.",
    "removed_empty_852_subfield": "An 852 (Location) subfield (other than $h) was present but held no data -- "
    "removed.",
    "recoded_title_only_7xx_to_730": "A 700/710/711 had a $t (title) but no name in $a -- recoded as a 730 "
    "(Added Entry--Uniform Title), with the $t moved to $a, rather than "
    "removed.",
    "recoded_6xx_b_to_a": "A 650/651/655 had no $a but started with a $b -- recoded so the $b is the "
    "$a (the heading term was just mis-coded) rather than removing the field.",
    "recoded_852_b_to_i": "852 had a second $b positioned after $h -- recoded to $i (it's the "
    "cutter/copy number that goes with $h, just miscoded).",
    "truncated_oversized_field": "A field's real, delimiter-bounded content is longer than 9999 bytes -- too "
    "long for ISO 2709's 4-digit directory length field to ever declare, so the "
    "source record's own directory entry for it was already a lie. Collapsing "
    "redundant MARC-8 escapes (see collapsed_oversized_field_escapes) did not "
    "make it fit, so the field was cut at the last `--` or subfield boundary "
    "that fits; the rest of the record, including everything after it, still "
    "recovers.",
    "collapsed_oversized_field_escapes": "A field's real content was longer than 9999 bytes (see "
    "truncated_oversized_field) but fit once redundant MARC-8 escapes were "
    "collapsed: adjacent same-set escape reset/re-designations were merged and "
    "spurious one-character subscript/superscript escape pairs (ESC b/p, digit, "
    "ESC s) were removed. Nothing else was changed; the field's own text is "
    "kept in full.",
}


#: Each category's own data-loss call (DATA LOSS/POSSIBLE DATA LOSS/NO DATA LOSS) -- printed
#: in isolation as the third of the four header lines `write_log`
#: writes for every active category (see `write_log`), never folded
#: into `_CHECK_DESCRIPTIONS`'s own text, so a reader can always find
#: the call on its own line no matter how the description wraps.
_CHECK_DATA_LOSS: dict[str, str] = {
    "unfixable": "DATA LOSS",
    "record_identifier_required": "DATA LOSS",
    "diverted_for_review": "DATA LOSS",
    "duplicate_identifier": "POSSIBLE DATA LOSS",
    "removed_non_repeatable_duplicate": "POSSIBLE DATA LOSS",
    "field_removed_because_missing_a": "POSSIBLE DATA LOSS",
    "removed_010_whitespace_a": "NO DATA LOSS",
    "deduped_010_lccn": "NO DATA LOSS",
    "removed_880_missing_a": "DATA LOSS",
    "removed_invalid_subfield": "POSSIBLE DATA LOSS",
    "removed_untranscodable_subfield": "DATA LOSS",
    "removed_bad_call_number": "POSSIBLE DATA LOSS",
    "removed_extra_852_b": "POSSIBLE DATA LOSS",
    "incomplete_852": "POSSIBLE DATA LOSS",
    "doubled_proxy_url": "NO DATA LOSS",
    "suspect_marc8_escape": "NO DATA LOSS",
    "suspect_hex_encoded_marc8": "POSSIBLE DATA LOSS",
    "holdings_852_b_suspect_content": "POSSIBLE DATA LOSS",
    "holdings_853_missing_8": "POSSIBLE DATA LOSS",
    "holdings_856_missing_u": "NO DATA LOSS",
    "bib_856_missing_u": "NO DATA LOSS",
    "split_holdings_multiple_852": "NO DATA LOSS",
    "unfixed_non_numeric_tag": "POSSIBLE DATA LOSS",
    "dangling_880_link": "NO DATA LOSS",
    "invalid_isbn_issn_checksum": "NO DATA LOSS",
    "holdings_escape_sequence": "NO DATA LOSS",
    "transcode_marc8_failed": "POSSIBLE DATA LOSS",
    "added_default_008": "NO DATA LOSS",
    "added_default_holdings_008": "NO DATA LOSS",
    "added_default_245": "NO DATA LOSS",
    "added_missing_852c": "NO DATA LOSS",
    "normalized_subfield_9_to_0": "NO DATA LOSS",
    "normalized_smart_characters": "NO DATA LOSS",
    "transcoded_marc8": "NO DATA LOSS",
    "fixed_misplaced_subfield_code": "NO DATA LOSS",
    "removed_null_identifier": "NO DATA LOSS",
    "missing_call_number": "NO DATA LOSS",
    "leader_byte_defaulted": "NO DATA LOSS",
    "holdings_leader_byte_defaulted": "NO DATA LOSS",
    "leader_entry_map_fixed": "NO DATA LOSS",
    "invalid_tag": "NO DATA LOSS",
    "invalid_indicator_value": "NO DATA LOSS",
    "invalid_bibliographic_level": "NO DATA LOSS",
    "fixed_mojibake": "NO DATA LOSS",
    "fixed_marc8_b2_umlaut": "NO DATA LOSS",
    "fixed_marc8_diacritic": "NO DATA LOSS",
    "remapped_999_to_945": "NO DATA LOSS",
    "oversized_sentinel_fixed": "NO DATA LOSS",
    "padded_indicators": "NO DATA LOSS",
    "holdings_missing_004": "POSSIBLE DATA LOSS",
    "holdings_multiple_004": "NO DATA LOSS",
    "holdings_852_duplicate_nr_subfield": "POSSIBLE DATA LOSS",
    "fixed_008_length": "POSSIBLE DATA LOSS",
    "fixed_holdings_008_length": "POSSIBLE DATA LOSS",
    "added_field": "NO DATA LOSS",
    "added_missing_852_location": "NO DATA LOSS",
    "reattached_orphaned_field": "NO DATA LOSS",
    "removed_empty_852_subfield": "NO DATA LOSS",
    "recoded_852_b_to_i": "NO DATA LOSS",
    "recoded_title_only_7xx_to_730": "NO DATA LOSS",
    "moved_duplicate_to_added_entry": "POSSIBLE DATA LOSS",
    "merged_duplicate_040": "POSSIBLE DATA LOSS",
    "recoded_6xx_b_to_a": "NO DATA LOSS",
    "truncated_oversized_field": "DATA LOSS",
    "trimmed_oversized_record": "DATA LOSS",
    "collapsed_oversized_field_escapes": "POSSIBLE DATA LOSS",
    "non_standard_marc_fields": "NO DATA LOSS",
}


#: A short plain-language parenthetical appended to a category's own
#: name in `write_log`'s "=== SECTION: category ===" header line (only,
#: not the description or per-record lines below it) -- for the rare
#: category whose bare machine-readable name (e.g. "fixed_mojibake")
#: doesn't mean much to a reader without cataloging/MARC-internals
#: background, even with the description line right below it.
_CATEGORY_DISPLAY_NOTE: dict[str, str] = {
    "fixed_mojibake": "character encoding issue",
}


#: Categories listed in full (every matching record) by default,
#: rather than just the header + count `write_log` always writes for
#: every category active in a run -- see `write_log`. Chosen by two
#: criteria: real content was discarded or altered (a library needs to
#: know exactly which records), or the finding is something a cataloger
#: would plausibly want to go fix one-by-one (e.g. a broken URL) rather
#: than accept in bulk. Everything else defaults to header+count only
#: -- get the full list for any of those via --log-full (repeatable).
#: Note this can never include an INFORMATIONAL category, though --
#: see `write_log`.
#: Deliberately excludes a couple of categories that are both
#: individually real and can be very high-volume across a file,
#: unlikely to ever be individually fixed (dangling_880_link,
#: invalid_isbn_issn_checksum) -- on request.
_ALWAYS_FULL_CATEGORIES = {
    "non_standard_marc_fields",
    "unfixable",
    "record_identifier_required",
    "diverted_for_review",
    "duplicate_identifier",
    "moved_duplicate_to_added_entry",
    "merged_duplicate_040",
    "removed_non_repeatable_duplicate",
    "field_removed_because_missing_a",
    "recoded_6xx_b_to_a",
    "recoded_title_only_7xx_to_730",
    "deduped_010_lccn",
    "removed_880_missing_a",
    "removed_invalid_subfield",
    "removed_untranscodable_subfield",
    "truncated_oversized_field",
    "trimmed_oversized_record",
    "collapsed_oversized_field_escapes",
    "removed_bad_call_number",
    "removed_extra_852_b",
    "incomplete_852",
    "doubled_proxy_url",
    "suspect_marc8_escape",
    "suspect_hex_encoded_marc8",
    "holdings_852_b_suspect_content",
    "holdings_853_missing_8",
    "holdings_856_missing_u",
    "bib_856_missing_u",
    "split_holdings_multiple_852",
    "holdings_escape_sequence",
    "transcode_marc8_failed",
    "reattached_orphaned_field",
    "holdings_leader_byte_defaulted",
    "holdings_missing_004",
    "holdings_852_duplicate_nr_subfield",
    "added_missing_852_location",
    "added_default_245",
    "added_default_008",
    "padded_indicators",
    "unfixed_non_numeric_tag",
    "invalid_bibliographic_level",
    "invalid_indicator_value",
    "added_missing_852c",
}


#: Categories whose per-entry `detail` already carries its own "NO
#: DATA LOSS" vs. "POSSIBLE DATA LOSS" call (see
#: `strip_duplicate_non_repeatable_fields`) -- for these, `write_log`
#: collapses every NO DATA LOSS entry into a single summary count
#: line instead of listing each one, since an exact-duplicate removal
#: needs no individual review and would otherwise bury the POSSIBLE
#: DATA LOSS entries that do.
_SUMMARIZE_NO_DATA_LOSS_ENTRIES = {
    "removed_non_repeatable_duplicate",
    "deduped_010_lccn",
}


#: Categories listed in full as just one comma-delimited line of record
#: identifiers (bib numbers; "record N" for one with no identifier),
#: de-duplicated and in file order, instead of one detail line per
#: record -- see `write_log`.
_ID_LIST_ONLY_CATEGORIES = {
    "recoded_title_only_7xx_to_730",
}


#: Where each 852 (Location)-related category sits within its own
#: section's 852 cluster (see `_category_sort_key`) -- whole-field/
#: whole-record concerns first (no single subfield to blame: either
#: the field as a whole, or a subfield that varies per record, e.g.
#: "whichever non-repeatable code got duplicated"), then subfield-
#: specific ones grouped and ordered by subfield code.
_852_CATEGORY_ORDER: dict[str, tuple[int, str]] = {
    "added_missing_852_location": (0, ""),
    "incomplete_852": (0, ""),
    "split_holdings_multiple_852": (0, ""),
    "removed_empty_852_subfield": (0, ""),
    "holdings_852_duplicate_nr_subfield": (0, ""),
    "holdings_852_b_suspect_content": (1, "b"),
    "recoded_852_b_to_i": (1, "b"),
    "removed_extra_852_b": (1, "b"),
    "added_missing_852c": (1, "c"),
    "removed_bad_call_number": (1, "h"),
    "missing_call_number": (1, "h"),
}


def _category_sort_key(category: str) -> tuple:
    """Sort key for a category within its section: every non-852
    category keeps its plain alphabetical order; every 852 category
    (see `_852_CATEGORY_ORDER`) sorts after all of those, clustered
    together as one block -- whole-852/whole-record concerns first,
    then subfield-specific ones in subfield-code order -- rather than
    scattered wherever its own name happens to fall alphabetically."""
    order852 = _852_CATEGORY_ORDER.get(category)
    if order852 is None:
        return (0, 0, "", category)
    scope, subfield = order852
    return (1, scope, subfield, category)


def _timestamped_log_path(path: str, run_ts: str) -> str:
    """Insert `run_ts` right before `path`'s extension, so every log this
    tool writes is timestamped -- even one named explicitly via --log --
    and repeat runs never silently overwrite or blend into a prior run's
    log."""
    base, ext = os.path.splitext(path)
    return f"{base}_{run_ts}{ext}"


def _write_run_timing_header(
    log_path: str,
    started: datetime,
    finished: datetime,
    source_path: str,
    repaired_path: str,
    problem_path: str | None,
    source_count: int | None = None,
    repaired_count: int | None = None,
    problem_count: int | None = None,
    review_path: str | None = None,
    review_count: int | None = None,
) -> None:
    """Write a run-level started/finished/elapsed line, followed by the
    source/repaired/problem/needs-review filenames for this run, as the
    very first lines of the log file -- the stdout summary doesn't
    persist once the terminal scrolls away, so the log file needs its
    own record of when the run ran, how long it took, and which files it
    read from/wrote to. Elapsed is wall-clock `finished - started`, to
    .01s.

    `problem_path`/`review_path` are None when the run has no actual
    error/needs-review file to name -- both are written lazily (see
    `_LazyBinaryWriter`) and never created at all when there's nothing to
    put in them, so a caller passing a real path here must first confirm
    the file actually exists; the line is omitted entirely rather than
    naming a file that was never written.

    Each path is reduced to its basename -- the log is meant to be a quick
    at-a-glance record of which files were involved, not a durable pointer
    back to them (the run's own stdout/args already has the full paths).

    `*_count` is the record count for the matching file, appended in
    parentheses when given; None (e.g. --sample-log's placeholder paths,
    which never actually read/write any records) omits it."""
    elapsed = (finished - started).total_seconds()

    def _named(path: str, count: int | None) -> str:
        name = os.path.basename(path)
        if count is None:
            return name
        record_word = "record" if count == 1 else "records"
        return f"{name} ({count} {record_word})"

    with open(log_path, "w", encoding="utf-8") as fh:
        fh.write(
            f"=== RUN: started {started.strftime('%Y-%m-%dT%H:%M:%SZ')}, "
            f"finished {finished.strftime('%Y-%m-%dT%H:%M:%SZ')}, "
            f"elapsed {elapsed:.2f}s ===\n"
        )
        fh.write(f"Source filename: {_named(source_path, source_count)}\n")
        fh.write(f"Repaired filename: {_named(repaired_path, repaired_count)}\n")
        if problem_path is not None:
            fh.write(f"Problem filename: {_named(problem_path, problem_count)}\n")
        if review_path is not None:
            fh.write(f"Needs-review filename: {_named(review_path, review_count)}\n")
        fh.write("\n")


#: Max total characters allowed in each line of a wrapped "=== ... ==="
#: description header (prefix and, on the closing line, the " ===" suffix
#: included) -- see `_write_description_header`.
_DESCRIPTION_LINE_WIDTH = 78


def _write_description_header(fh, description: str) -> None:
    """Write `description` (the data-loss call -- "DATA LOSS"/"POSSIBLE
    DATA LOSS"/"NO DATA LOSS" -- NOT included; see `_CHECK_DATA_LOSS`
    and its own isolated "=== <call> ===" line, written separately by
    the caller right after this) as one or more "=== ..." lines,
    wrapped so no line exceeds `_DESCRIPTION_LINE_WIDTH` characters.
    The first line is prefixed "=== "; continuation lines are prefixed
    "===   " (indented two extra spaces so the wrapped text still
    lines up). Unlike a self-contained "=== ... ===" header line, none
    of these get a closing " ===" suffix -- the data-loss line right
    after is what closes out this whole header block instead."""
    first_prefix = "=== "
    cont_prefix = "===   "
    wrap_width = _DESCRIPTION_LINE_WIDTH - len(cont_prefix)
    lines = textwrap.wrap(description, width=wrap_width) or [""]
    for i, line in enumerate(lines):
        prefix = first_prefix if i == 0 else cont_prefix
        fh.write(f"{prefix}{line}\n")


def _write_non_standard_blocks(fh, group: list[LogEntry], tally: NonStandardFieldTally) -> None:
    """One block per tag, in tag order: a heading with that tag's own
    counts (and [OBSOLETE] if it is), its detail entries, then an "and N
    more <tag>" line if the tally counted more than were listed."""
    by_tag: dict[str, list[LogEntry]] = {}
    for e in group:
        by_tag.setdefault(e.detail[1:4], []).append(e)  # detail is "=TAG ..."
    for tag in sorted(tally.instances):
        n_inst, n_rec = tally.instances[tag], tally.records[tag]
        obsolete = " [OBSOLETE]" if tag in tally.obsolete else ""
        fh.write(f"\n--- {tag}{obsolete}: {n_inst:,} instance(s) in {n_rec:,} record(s) ---\n")
        for e in by_tag.get(tag, []):
            fh.write(e.render() + "\n")
        if n_inst > tally.listed.get(tag, 0):
            fh.write(f"\tand {n_inst - tally.listed[tag]:,} more {tag}\n")


def write_log(
    path: str,
    entries: list[LogEntry],
    active_categories: set[str] = frozenset(),
    full_categories: set[str] | None = None,
    count_notes: dict[str, str] | None = None,
    non_standard_tally: NonStandardFieldTally | None = None,
    include_zero: bool = False,
) -> None:
    """Write `entries` grouped into sections -- NOT FIXED, then FIXED/
    REQUIRES ATTENTION, then DUPLICATE RECORDS, then INFORMATIONAL at
    the bottom (there is no plain "FIXED" section -- see
    `_section_for`) -- and then by category within each.

    Every category in `active_categories` (every check that actually
    ran this invocation, whether or not it found anything -- the
    caller, `main`/`repair_holdings_records`, is responsible for this
    set being complete: a category whose check didn't run at all this
    invocation, e.g. one gated behind an off-by-default flag, must not
    be in it) gets a header, even one with zero matching records:

        === <SECTION>: <category> ===
        === <description of what the check does, wrapped by
        ===   `_write_description_header` so no line exceeds
        ===   `_DESCRIPTION_LINE_WIDTH` characters -- never itself
        ===   says DATA LOSS/POSSIBLE DATA LOSS/NO DATA LOSS>
        === <DATA LOSS|POSSIBLE DATA LOSS|NO DATA LOSS, from
        ===   `_CHECK_DATA_LOSS`, isolated on its own line> ===
        === <N> record(s) ===

    -- or, when a category can log more than one finding per record
    (e.g. suspect_marc8_escape, one per escape in the record), the
    count line instead reads:

        === <N> instance(s) in <M> record(s) ===

    so a reader isn't left guessing whether the number is findings or
    distinct records -- see the `n_instances`/`n_records` split below.
    This split only fires when the two numbers actually differ; a
    category that's always one finding per record still gets the
    plain "<N> record(s)" form.

    -- so a run's log always documents every check it performed, not
    just the ones that found something. Below that header, every
    matching record is listed in full only if `category` is in
    `full_categories` (defaults to `_ALWAYS_FULL_CATEGORIES` when not
    given); otherwise the header's count is all a reader gets, and
    --log-full is how they'd ask for the rest. This split point is the
    whole reason `_ALWAYS_FULL_CATEGORIES` is a small curated set
    rather than "everything": a run with (say) 50,000 padded-indicator
    fixes and 3 doubled-proxy-URLs should make both facts easy to
    find, not bury the 3 under the 50,000.

    INFORMATIONAL categories never get listed in full, regardless of
    `full_categories` -- they're the highest-volume, least-actionable
    findings (typographic normalization, MARC-8 transcoding, and the
    like), so a header+count is genuinely all there is to say about
    them. A category worth a cataloger's individual attention belongs
    in FIXED/REQUIRES ATTENTION or NEEDS REVIEW instead, not forced
    full while still sitting in INFORMATIONAL.

    `count_notes` (category -> note text) appends " - <note>" onto
    that category's own count line, for a category whose per-record
    entries don't by themselves add up to some other total a reader
    would want -- e.g. split_holdings_multiple_852's count is the
    number of *input* records split, not how many holdings records
    that produced in total.

    Unless `include_zero` is set, a category with no findings gets no
    header block above; instead every such category (still only those
    in `active_categories`, i.e. checks that really ran) is listed once,
    by name with a one-line description, in a closing "CHECKS PERFORMED
    WITH NO FINDINGS" block -- so the log stays trim but still shows the
    full scope of what was tested."""
    if full_categories is None:
        full_categories = _ALWAYS_FULL_CATEGORIES
    groups: dict[tuple[int, str, str], list[LogEntry]] = {}
    for e in entries:
        order, label = _section_for(e)
        groups.setdefault((order, label, e.category), []).append(e)
    for category in active_categories:
        order, label = _section_for_category(category)
        groups.setdefault((order, label, category), [])

    def _group_sort_key(key: tuple[int, str, str]) -> tuple:
        order, label, category = key
        return (order, label, *_category_sort_key(category))

    ordered = sorted(groups, key=_group_sort_key)
    skipped: list[tuple[str, str]] = []
    if not include_zero:
        kept = []
        for key in ordered:
            if groups[key]:
                kept.append(key)
            else:
                skipped.append((key[1], key[2]))
        ordered = kept

    with open(path, "a", encoding="utf-8") as fh:
        for section_num, (order, label, category) in enumerate(ordered):
            group = groups[(order, label, category)]
            if section_num > 0:
                fh.write("\n")
            note = _CATEGORY_DISPLAY_NOTE.get(category)
            display_category = f"{category} ({note})" if note else category
            fh.write(f"=== {label}: {display_category} ===\n")
            description = _CHECK_DESCRIPTIONS.get(category, "(no description available)")
            _write_description_header(fh, description)
            data_loss_call = _CHECK_DATA_LOSS.get(category)
            if data_loss_call:
                fh.write(f"=== {data_loss_call} ===\n")
            count_note = (count_notes or {}).get(category)
            n_instances = len(group)
            n_records = len({e.record_idx for e in group})
            if n_instances == n_records:
                count_line = f"{n_instances} record(s)"
            else:
                count_line = f"{n_instances} instance(s) in {n_records} record(s)"
            tally = (
                non_standard_tally
                if category == NON_STANDARD_FIELD_CATEGORY and non_standard_tally is not None
                and non_standard_tally.instances
                else None
            )
            if tally is not None:
                count_line = (
                    f"{sum(tally.instances.values()):,} instance(s) in "
                    f"{tally.records_with_any:,} record(s), {len(tally.instances)} tag(s); "
                    f"first {tally.limit} of each tag listed"
                )
            if count_note:
                count_line += f" - {count_note}"
            fh.write(f"=== {count_line} ===\n")
            if tally is not None:
                write_non_standard_toc(fh, tally)
                _write_non_standard_blocks(fh, group, tally)
            elif category in full_categories and label != "INFORMATIONAL":
                if category in _ID_LIST_ONLY_CATEGORIES:
                    ids = dict.fromkeys(
                        e.record_id or f"record {e.record_idx}" for e in group
                    )
                    fh.write(", ".join(ids) + "\n")
                elif category in _SUMMARIZE_NO_DATA_LOSS_ENTRIES:
                    no_loss_count = sum(1 for e in group if "NO DATA LOSS" in e.detail)
                    if no_loss_count:
                        fh.write(
                            f"\t{no_loss_count} record(s) where the fields are "
                            "exact duplicates -- NO DATA LOSS\n"
                        )
                    for e in group:
                        if "NO DATA LOSS" not in e.detail:
                            fh.write(e.render() + "\n")
                else:
                    for e in group:
                        fh.write(e.render() + "\n")
        if skipped:
            if ordered:
                fh.write("\n")
            fh.write(f"=== CHECKS PERFORMED WITH NO FINDINGS ({len(skipped)}) ===\n")
            for label, category in skipped:
                description = _CHECK_DESCRIPTIONS.get(category, "(no description available)")
                fh.write(f"{label}: {category} -- {_first_sentence(description)}\n")


def _first_sentence(text: str) -> str:
    """`text` up to and including its first sentence-ending period (or
    all of it if there isn't one), whitespace collapsed to single spaces."""
    flat = " ".join(text.split())
    m = re.search(r"\.(\s|$)", flat)
    return flat[: m.end()].strip() if m else flat


class ProgressReporter:
    """Prints a periodic progress line to stderr while a long run is in
    flight -- otherwise a multi-minute run on a huge file gives no sign of
    life until it's completely done. Throttled by both time and record
    count so it doesn't spam a redirected/logged stderr, and self-adjusts
    between an in-place updating line (interactive terminal) and full
    lines (piped/redirected output, e.g. into a log file).
    """

    #: Records to let go by, uncounted, before the estimate's own sample
    #: window starts -- see `maybe_print_estimate`.
    _ESTIMATE_WARMUP_RECORDS = 20

    def __init__(self, total_bytes: int, min_interval: float | None = None):
        self.total_bytes = total_bytes
        self.is_tty = sys.stderr.isatty()
        # an interactive terminal can take frequent in-place updates; a
        # redirected/logged stderr should get full lines only occasionally,
        # or a long run would spam the log with thousands of them
        default_interval = 0.5 if self.is_tty else 10.0
        self.min_interval = min_interval if min_interval is not None else default_interval
        self.start_time = time.perf_counter()
        self.last_print_time = 0.0
        self.bytes_read = 0
        self.printed_anything = False
        self.estimate_shown = False
        # Set once `_ESTIMATE_WARMUP_RECORDS` records have gone by -- the
        # estimate's own sample window is measured from here, not from
        # `start_time` (see `maybe_print_estimate`).
        self._estimate_warmup_n: int | None = None
        self._estimate_warmup_bytes: int | None = None
        self._estimate_warmup_time: float | None = None

    def on_progress(self, bytes_read: int) -> None:
        self.bytes_read = bytes_read

    def maybe_print_estimate(self, n_records: int, bytes_consumed: int) -> None:
        """Print a one-time, early, rough total-runtime estimate -- separate
        from the ongoing ETA in `maybe_print` below, which only starts
        appearing once that method's own throttle interval has elapsed
        (up to 10s on a non-tty stderr, meaning a modest-sized run could
        finish before it ever prints anything).

        The sample used for the estimate is NOT measured from
        `self.start_time` (i.e. from construction) -- it's measured from
        the moment `_ESTIMATE_WARMUP_RECORDS` records have gone by, with
        everything before that point thrown away entirely. A real run
        showed the very first stretch of a run can be dominated by a
        one-time cost -- e.g. the first disk read, before any OS
        read-ahead/page-cache has kicked in -- that has nothing to do
        with this tool's actual steady-state per-record throughput;
        sampling through it produced a rate ~30x too slow and a runtime
        estimate (1h03m) wildly higher than the real one (~2min). Once
        that warm-up point is reached, waits for at least 200 further
        records and 1.0s of further elapsed time -- still fires as
        early as possible otherwise.

        `bytes_consumed` must be actual bytes consumed by the
        `n_records` processed so far (the caller tracks this from each
        record's own text) -- NOT `self.bytes_read`, which reflects the
        streaming reader's buffered read-ahead and can jump to a whole
        32MB+ chunk almost instantly, long before the records in it are
        actually processed. Using that instead here was a real bug: it
        made the very first estimate wildly too optimistic (a "~0s"
        estimate on a run that actually took 12 seconds), because it
        divided a large already-buffered byte count by only a handful
        of actually-finished records, understating the true average
        record size by two orders of magnitude.
        """
        if self.estimate_shown or not self.total_bytes or bytes_consumed <= 0:
            return
        if n_records < self._ESTIMATE_WARMUP_RECORDS:
            return
        if self._estimate_warmup_time is None:
            # this is the first call past the warm-up floor -- anchor the
            # sample window here and wait for the next call to measure
            # anything, so whatever happened before this point (most
            # likely that first, possibly-slow disk read) never factors
            # into the rate below
            self._estimate_warmup_n = n_records
            self._estimate_warmup_bytes = bytes_consumed
            self._estimate_warmup_time = time.perf_counter()
            return
        elapsed = time.perf_counter() - self._estimate_warmup_time
        sample_n = n_records - self._estimate_warmup_n
        sample_bytes = bytes_consumed - self._estimate_warmup_bytes
        if sample_n < 200 or elapsed < 1.0:
            return
        self.estimate_shown = True
        avg_bytes_per_record = sample_bytes / sample_n
        rate = sample_n / elapsed
        if avg_bytes_per_record <= 0 or rate <= 0:
            return
        estimated_total_records = self.total_bytes / avg_bytes_per_record
        estimated_total_time = estimated_total_records / rate
        print(
            f"Estimated total runtime: ~{_format_duration(estimated_total_time)} "
            f"for {self.total_bytes / 1_000_000:.1f} MB "
            f"(rough estimate from {sample_n:,} records after an initial "
            f"{self._ESTIMATE_WARMUP_RECORDS}-record warm-up; assumes no "
            "record needs the invalid-tag second pass)",
            file=sys.stderr,
        )

    def maybe_print(self, n_records: int) -> None:
        now = time.perf_counter()
        if now - self.last_print_time < self.min_interval:
            return
        self.last_print_time = now
        elapsed = now - self.start_time
        rate = n_records / elapsed if elapsed > 0 else 0
        parts = [
            f"{n_records:,} records",
            f"{rate:,.0f} rec/s",
            f"elapsed {_format_duration(elapsed)}",
        ]
        if self.total_bytes:
            pct = 100 * self.bytes_read / self.total_bytes
            parts.insert(1, f"{pct:4.1f}% of input")
            if self.bytes_read > 0 and pct < 100:
                eta = elapsed * (self.total_bytes - self.bytes_read) / self.bytes_read
                parts.append(f"ETA {_format_duration(eta)}")
        line = " | ".join(parts)
        end = "\r" if self.is_tty else "\n"
        print(line, end=end, file=sys.stderr)
        self.printed_anything = True

    def finish(self) -> None:
        if self.printed_anything and self.is_tty:
            print(file=sys.stderr)  # move off the in-place progress line


#: Every category `repair_holdings_records` can log, run unconditionally
#: on every invocation regardless of any of its own parameters -- used
#: to build that run's `active_categories` for `write_log` (see below;
#: "added_missing_852c" is the one holdings category NOT here, since it
#: only runs at all when `fix_missing_852c` is set).
_HOLDINGS_ALWAYS_ACTIVE_CATEGORIES = {
    "unfixable",
    "padded_indicators",
    "holdings_escape_sequence",
    "fixed_mojibake",
    "holdings_leader_byte_defaulted",
    "normalized_subfield_9_to_0",
    "normalized_smart_characters",
    "fixed_misplaced_subfield_code",
    "removed_invalid_subfield",
    "field_removed_because_missing_a",
    "added_default_holdings_008",
    "fixed_holdings_008_length",
    "recoded_852_b_to_i",
    "removed_extra_852_b",
    "removed_bad_call_number",
    "missing_call_number",
    "added_missing_852_location",
    "removed_empty_852_subfield",
    "removed_null_identifier",
    "holdings_missing_004",
    "holdings_multiple_004",
    "holdings_852_duplicate_nr_subfield",
    "holdings_852_b_suspect_content",
    "holdings_853_missing_8",
    "holdings_856_missing_u",
    "incomplete_852",
    "split_holdings_multiple_852",
    "oversized_sentinel_fixed",
    "truncated_oversized_field",
    "trimmed_oversized_record",
    "collapsed_oversized_field_escapes",
}


def repair_holdings_records(
    input_path: str,
    output_path: str,
    log_path: str,
    fix_missing_852c: bool = True,
    allow_single_digit_852b: bool = False,
    allow_oversized_sentinel: bool = False,
    trim_oversized_record_enabled: bool = True,
    full_categories: set[str] | None = None,
    on_progress: Callable[[int], None] | None = None,
    on_record: Callable[[int], None] | None = None,
    on_estimate: Callable[[int, int], None] | None = None,
    include_zero: bool = False,
) -> dict[str, int]:
    """Actually repair a holdings-only MARC file (e.g. one of
    `split_bib_holdings`'s outputs) -- unlike `check_holdings_records`
    (still available, and still purely read-only), this rewrites the
    file. It reuses this tool's same self-determining structural repair
    (`iter_repair_stream`, Mode 1/2 -- fixes bad length/bad directory for
    free, since those are just consequences of a stale declared length
    once the record's real content is re-derived) plus the subset of
    the main bib pipeline's content fixes that are safe and meaningful
    for a holdings record specifically:

      * short-indicator padding (via `iter_repair_stream`'s own
        `fix_bad_indicators`)
      * double-encoded UTF-8 ("mojibake") correction
      * leader byte 05 defaulted the same way as bib records; byte 06
        (type of record) defaults to 'u' (Unknown) instead of bib's
        'a' (Language material); byte 17 (encoding level) is checked
        against holdings' OWN code set
        (`LEADER_17_ENCODING_LEVEL_VALID_HOLDINGS`, which includes 'm'
        and excludes blank, unlike the shared bib/authority/holdings
        union) and defaults to 'u' rather than blank -- see
        `fix_invalid_leader_bytes`. Logged as
        "holdings_leader_byte_defaulted" -- FIXED/REQUIRES ATTENTION,
        not INFORMATIONAL like the bib pipeline's equivalent -- since
        this changes real classification bytes and is worth
        highlighting rather than burying in the bottom, off-by-default
        section
      * $9 -> $0 subfield code normalization
      * typographic "smart" character normalization
      * misplaced-subfield-code recovery (a stray space between the
        delimiter and its real code -- see `fix_misplaced_subfield_codes`),
        same as the bib pipeline; runs first so these are recovered
        rather than caught by invalid-code removal below. Header+count
        always shown; not listed per-record by default (see
        `full_categories`) since this can be a large fraction of a
        file with this defect
      * invalid (non a-z0-9) subfield code removal
      * empty-field removal
      * a missing 008 gets a blank, syntactically-valid placeholder
        (see `add_default_holdings_008` -- there's no institution-
        agnostic *content* default the way bib's placeholder has one)
      * a wrong-length 008 is padded/truncated to the holdings-specific
        32 bytes (see `HOLDINGS_008_LENGTH`), not bib's 40
      * a missing 004 (Control Number linking to the bib record) is
        flagged NOT FIXED (category "holdings_missing_004", never
        invented -- see `_find_004_issues`); more than one is resolved
        the same way multiple 852s are (see `split_holdings_multiple_004`):
        the first 004 is kept on this record, and every additional one
        becomes its own duplicate holdings record (every other field
        copied verbatim) written to a separate "_additional004s" file
        (see `_additional_004_output_path`) rather than the main
        output -- flagged INFORMATIONAL (category "holdings_multiple_004",
        since a legitimate multi-bib link isn't necessarily wrong), NO
        DATA LOSS: every 004 is preserved, just reshaped to one per
        record
      * more than one 852 (Location) field is actually resolved --
        see `split_holdings_multiple_852`: the record is split into
        one output holdings record per usable ($b-bearing) 852, every
        other field duplicated verbatim into each copy, flagged FIXED
        (category "split_holdings_multiple_852"). An 852 with no $b
        at all isn't duplicated into any copy -- there's no location
        to split out -- it's dropped and flagged NOT FIXED instead
        (category "incomplete_852"). Every copy after the first gets
        "-2", "-3", etc. appended to field 001, so they no longer
        share one identifier (see `_suffix_holdings_identifier`).
        This is the one point in this pipeline where a single input
        record can produce more than one output record; see
        `on_estimate`'s caller and the returned "written" count below
      * more than one occurrence, WITHIN one 852, of a subfield the
        MARC 21 spec defines as Not Repeatable there (e.g. two $h --
        see `_852_NON_REPEATABLE_CODES` for the full list, and note $b/
        $c are deliberately excluded: both are officially Repeatable,
        unlike the pattern `fix_852_multiple_b` above actually corrects)
        has every occurrence after the first removed (category
        "holdings_852_duplicate_nr_subfield", FIXED/REQUIRES ATTENTION:
        real data discarded, the kept copy isn't necessarily the
        "right" one); see `strip_852_duplicate_non_repeatable_subfields`
      * an 852 $b (Sublocation) that's purely numeric or contains
        multiple literal "#" characters (a sign of subfields flattened
        into plain text by whatever exported the record) is replaced
        wholesale with `DEFAULT_852_LOCATION_CONTENT` (category
        "holdings_852_b_suspect_content", FIXED/REQUIRES ATTENTION:
        real data discarded, this tool has no way to know what the
        real location should have been) -- unless
        --allow-single-digit-852b is given, in which case a
        single-digit $b is left untouched instead; see
        `fix_852_b_suspect_content`
      * an 853 (Captions and Pattern -- Basic) field with no $8 (Field
        link and sequence number) -- the subfield an 863 needs to find
        its caption/pattern -- is flagged NOT FIXED (category
        "holdings_853_missing_8"): a real issue this institution's own
        downstream FOLIO/transform validation already flags, and this
        tool has no way to invent a real link/sequence number; see
        `find_853_missing_8`
      * an 856 (Electronic Location and Access) field with no $u (URI)
        is flagged NOT FIXED (category "holdings_856_missing_u"): same
        real-world validation, same reasoning -- this tool has no way
        to invent a real URL; see `find_856_missing_u`
      * 863/864/865/866/867/868 (Enumeration and Chronology / Textual
        Holdings, all three "levels") missing a non-empty, non-
        punctuation-only $a are removed the same way bib's
        `strip_missing_required_a` removes a heading field missing $a
        (see holdings_required_a_tags.txt); 853/854/855 (Captions and
        Pattern) get the same check but against {$a, $g, $i} instead of
        just {$a} -- see `HOLDINGS_CAPTION_PATTERN_VALID_CODES` -- since
        $g is a recognized alternate to $a there, and $i alone is valid
        for a chronology-only pattern with no enumeration captions at
        all. Both logged as "field_removed_because_missing_a", same
        category as the bib pipeline's equivalent
      * an 852 (Location) with more than one $b (Sublocation) subfield
        is always fixed down to one (see `fix_852_multiple_b`): if
        every $b has the exact same content, all but the first are
        just removed as a plain duplicate (the one genuinely
        unambiguous case, worded distinctly in the log -- "duplicate",
        not "no way to tell"); otherwise, if there's exactly two
        distinct $b's, $h is present, one $b is before it and the
        other after, and there's no $i (Item part) already, the second
        $b is recoded to $i instead -- it's actually the cutter
        number/date/copy note that goes with $h's classification, just
        miscoded; otherwise, if exactly one $b looks like something
        other than a location code (see `_is_852_b_non_location`) that
        one is removed and the real location kept; otherwise (every
        remaining $b looks equally location-like, or none does) the
        last one is removed as an arbitrary fallback. The recode case
        lands under its own category ("recoded_852_b_to_i"); all three
        removal cases share another ("removed_extra_852_b") -- both
        FIXED/REQUIRES ATTENTION, since real data was reinterpreted or
        discarded either way, worth a second look regardless of which
        of the four happened
      * 852 (Location)'s $h (Classification part -- the call number) is
        handled two ways depending on what's actually wrong (see
        `fix_852_call_number`): present but unusable (empty or
        punctuation-only) is a bad subfield, so just that subfield is
        removed and the rest of the field left alone (category
        "removed_bad_call_number", FIXED/REQUIRES ATTENTION -- real
        data discarded); missing entirely isn't a fix at all -- nothing
        to remove, so the field is left completely untouched and just
        flagged (category "missing_call_number", INFORMATIONAL: a call
        number can legitimately be absent, e.g. for some serials/
        electronic holdings, so this isn't as urgent as a genuine
        defect). Header+count always shown; per-record listing only
        via `full_categories` -- off by default since it can be a
        large fraction of a file, same reasoning as
        "removed_null_identifier" below)
      * an 852 field that survives the above but has no usable location
        in $a, $b, or $c -- which subfield actually carries it is
        source-system-dependent (see `fix_missing_852_location`) --
        always gets a placeholder inserted into $c if the record's own
        004 identifies it as OCLC WMS-sourced (see
        `_is_wms_holdings_record`), $b otherwise (the subfield most
        other systems treat as location-bearing); unlike $c below this
        isn't opt-in
      * if `fix_missing_852c` is set (on by default -- see
        --no-fix-missing-852c) and the record's own 004 identifies it
        as OCLC WMS-sourced (see `_is_wms_holdings_record`), an 852
        (Location) field missing $c (shelving location) gets a
        placeholder $c appended (see `add_missing_852c`); no-op for a
        non-WMS record, since $c isn't that record's live location
        subfield
      * any remaining subfield within an 852 that has no data at all
        (e.g. a bare $2 with nothing after it) is removed -- except
        $h, already handled specifically above -- see
        `strip_empty_852_subfields` (category
        "removed_empty_852_subfield", FIXED/REQUIRES ATTENTION); runs
        after all the other 852 content fixes above, so it only mops
        up whatever they didn't already turn into something else
      * any subfield with no data at all on any OTHER field (852 is
        fully covered by the fixes above) is likewise removed -- see
        `strip_null_identifiers` (category "removed_null_identifier",
        INFORMATIONAL). The fix itself always runs; header+count
        always shown, per-record listing only via `full_categories`
        -- off by default, same large-fraction-of-a-file reasoning as
        "fixed_misplaced_subfield_code" above
      * a non-numeric tag is renamed to an unused 9XX slot, same
        deferred two-pass approach `main` uses for bib records (needs
        every tag in the *holdings* file specifically, so this is
        tracked separately from the bib pass)
      * an 001 (or other record identifier -- see `record_identifier`)
        reused across more than one record in this run is flagged for
        every record involved (category "duplicate_identifier",
        DUPLICATE RECORDS section, same as the bib pipeline) -- see
        `find_duplicate_identifiers`; this is a whole-file check, run
        once after the full pass, same as the bib pipeline's equivalent

    Deliberately NOT applied here (bib-specific, would misfire on a
    holdings record): a placeholder 245 (holdings records have no 245),
    `strip_duplicate_non_repeatable_fields` (its tag list --
    non_repeatable_tags.txt -- was built against bibliographic field
    semantics), and `remap_999_to_945` (a Sierra/bib-specific
    convention). MARC-8-to-UTF-8 transcoding is also skipped for now (an
    explicit, temporary scope decision, not a permanent one) -- an ESC
    byte is still flagged (category "holdings_escape_sequence", NOT
    FIXED) rather than silently left in a record declared UTF-8. A null
    identifier (subfield present but empty) IS fixed, though -- see
    `strip_null_identifiers` above.

    A record that can't be structurally parsed at all, or whose split
    copy assembles too large for ISO 2709 to represent, is diverted --
    byte-for-byte unchanged -- to a separate "_error" file next to the
    main output (see `_error_output_path`), exactly like the main bib
    pipeline treats its own unfixable records (category "unfixable",
    UNFIXABLE section, DATA LOSS: it's excluded from the main output
    entirely). By default, a split copy over 99999 bytes is diverted
    here too, the same way the bib pipeline's own main() loop does --
    confirmed empirically that FOLIO Data Import aborts its entire batch
    with no job log on a record needing that sentinel, rather than
    rejecting just the one record. Pass `allow_oversized_sentinel=True`
    (default False -- see main()'s --allow-oversized-sentinel) only if
    your import target is confirmed to handle the sentinel correctly;
    it's then written to the main output as usual (category
    oversized_sentinel_fixed). One case where output record count differs from input
    record count in the *other* direction is `split_holdings_multiple_852`
    above: one input record with multiple usable 852s becomes multiple
    output records. `split_holdings_multiple_004` is similar but writes
    its extra copies to `_additional_004_output_path(output_path)`
    instead, so it never affects `output_path`'s own record count.

    `on_progress`/`on_record` mirror `split_bib_holdings`'s parameters
    of the same name -- liveness only, no effect on the repair.
    `on_estimate`, if given, is called after every record with
    (records processed so far, bytes consumed so far) -- meant to be
    wired to a `ProgressReporter`'s `maybe_print_estimate`, which
    self-throttles to fire (at most) once early in a long run and is a
    cheap no-op every other call, the same way `main`'s own bib loop
    uses it.

    Returns {"total": n, "written": n, "unfixable": n, "log_lines": n,
    "not_fixed": n, "additional_004_written": n} -- "total" is input
    records read, "written" is output records actually written to
    `output_path` (excludes anything diverted to the "_error" file or
    to the "_additional004s" file; equal to "total" - "unfixable"
    unless `split_holdings_multiple_852` also happened).
    "additional_004_written" is how many extra records
    `split_holdings_multiple_004` wrote to the "_additional004s" file
    (0 if that file was never created).
    """
    run_started = datetime.now(timezone.utc)
    encoding_used = detect_encoding(input_path)
    holdings_required_a_tags = load_tag_list(DEFAULT_HOLDINGS_REQUIRED_A_TAGS_FILE)
    error_path = _error_output_path(output_path)
    additional_004_path = _additional_004_output_path(output_path)
    log_entries: list[LogEntry] = []
    used_tags: set[str] = set()
    pending_tag_fixes: list[tuple[int, int, int, str]] = []
    id_records: list[tuple[int, str, str]] = []
    n_total = 0
    n_written = 0
    n_unfixable = 0
    n_new_holdings_records = 0
    n_additional_004_written = 0
    bytes_consumed_for_estimate = 0

    def log(category: str, fixed: bool, record_idx: int, rec_id: str, detail: str) -> None:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        log_entries.append(LogEntry(category, fixed, ts, record_idx, rec_id, detail))

    with open(output_path, "wb") as out_fh, _LazyBinaryWriter(error_path) as error_fh, \
            _LazyBinaryWriter(additional_004_path) as additional_004_fh:
        record_stream = iter_repair_stream(
            input_path, on_progress=on_progress, fix_bad_indicators=True,
        )
        for i, (parsed, rec_text) in enumerate(record_stream):
            n_total += 1
            if on_record is not None:
                on_record(n_total)
            # errors="surrogateescape" here and below: rec_text is the raw,
            # untouched original text, so it can still carry a
            # surrogate-escaped byte from a source-file corruption that
            # wasn't valid UTF-8 (see iter_repair_stream) -- round-tripping
            # it back to that same original byte, rather than raising, is
            # exactly what "written unchanged to the error file" already
            # means here.
            if on_estimate is not None:
                bytes_consumed_for_estimate += len(
                    rec_text.encode(encoding_used, errors="surrogateescape")
                )
                on_estimate(n_total, bytes_consumed_for_estimate)

            if parsed.unresolved:
                reason = parsed.unresolved[0][3]
                print(
                    f"record {i}: UNFIXABLE ({reason}) -- writing to {error_path} instead",
                    file=sys.stderr,
                )
                log("unfixable", False, i, "", f"written to {error_path} unchanged: {reason}")
                error_fh.write(rec_text.encode(encoding_used, errors="surrogateescape"))
                n_unfixable += 1
                continue

            for tag, spaces_added, original_indicators, preview in parsed.indicator_fixes:
                rec_id = record_identifier(parsed)
                log(
                    "padded_indicators", True, i, rec_id,
                    f"padded {spaces_added} space(s) into short indicators on ={tag} "
                    f"(was {original_indicators!r}, first subfield {preview!r})",
                )
            for tag, before, after, stripped, preview in parsed.collapsed_oversized_fields:
                rec_id = record_identifier(parsed)
                log(
                    "collapsed_oversized_field_escapes", True, i, rec_id,
                    f"={tag} was {before} bytes, over ISO 2709's 9999-byte "
                    f"directory length limit -- collapsed to {after} bytes "
                    f"({stripped} spurious escape pair(s) removed; "
                    f"preview: {preview!r})",
                )
            for tag, before, after, tail in parsed.truncated_oversized_fields:
                rec_id = record_identifier(parsed)
                log(
                    "truncated_oversized_field", True, i, rec_id,
                    f"={tag} was {before} bytes, over ISO 2709's 9999-byte "
                    f"directory length limit -- truncated to {after} bytes "
                    f"(dropped tail starts: {tail!r})",
                )
            if ESCAPE in rec_text:
                rec_id = record_identifier(parsed)
                log(
                    "holdings_escape_sequence", False, i, rec_id,
                    "contains an ESC (0x1B) byte -- possible unconverted MARC-8 "
                    "escape sequence (not transcoded -- MARC-8-to-UTF-8 conversion "
                    "is skipped for holdings records for now)",
                )
            rec_id = record_identifier(parsed)
            for detail in find_and_fix_mojibake(parsed):
                log("fixed_mojibake", True, i, rec_id, detail)
            for detail in fix_invalid_leader_bytes(
                parsed,
                type_of_record_default="u",
                encoding_level_valid=LEADER_17_ENCODING_LEVEL_VALID_HOLDINGS,
                encoding_level_default="u",
            ):
                log("holdings_leader_byte_defaulted", True, i, rec_id, detail)
            for detail in normalize_subfield_9_to_0(parsed):
                log("normalized_subfield_9_to_0", True, i, rec_id, detail)
            for detail in normalize_smart_characters(parsed):
                log("normalized_smart_characters", True, i, rec_id, detail)
            for detail in fix_misplaced_subfield_codes(parsed):
                log("fixed_misplaced_subfield_code", True, i, rec_id, detail)
            for detail in strip_invalid_subfield_codes(parsed):
                log("removed_invalid_subfield", True, i, rec_id, detail)
            for detail in strip_missing_required_a(parsed, holdings_required_a_tags):
                log("field_removed_because_missing_a", True, i, rec_id, detail)
            for detail in strip_missing_required_a(
                parsed, HOLDINGS_CAPTION_PATTERN_TAGS,
                valid_codes=HOLDINGS_CAPTION_PATTERN_VALID_CODES,
            ):
                log("field_removed_because_missing_a", True, i, rec_id, detail)
            strip_empty_fields(parsed)
            for detail in add_default_holdings_008(parsed):
                log("added_default_holdings_008", True, i, rec_id, detail)
            for detail in fix_008_length(parsed, expected_len=HOLDINGS_008_LENGTH):
                log("fixed_holdings_008_length", True, i, rec_id, detail)
            recoded_b_to_i, removed_extra_b = fix_852_multiple_b(parsed)
            for detail in recoded_b_to_i:
                log("recoded_852_b_to_i", True, i, rec_id, detail)
            for detail in removed_extra_b:
                log("removed_extra_852_b", True, i, rec_id, detail)
            removed_call_number, missing_call_number = fix_852_call_number(parsed)
            for detail in removed_call_number:
                log("removed_bad_call_number", True, i, rec_id, detail)
            for detail in missing_call_number:
                log("missing_call_number", False, i, rec_id, detail)
            for detail in fix_missing_852_location(parsed):
                log("added_missing_852_location", True, i, rec_id, detail)
            if fix_missing_852c:
                for detail in add_missing_852c(parsed):
                    log("added_missing_852c", True, i, rec_id, detail)
            for detail in strip_empty_852_subfields(parsed):
                log("removed_empty_852_subfield", True, i, rec_id, detail)
            for detail in strip_null_identifiers(parsed):
                log("removed_null_identifier", True, i, rec_id, detail)
            if not any(f.tag == "004" for f in parsed.fields):
                for category, detail in _find_004_issues(0):
                    log(category, False, i, rec_id, detail)
            for detail in strip_852_duplicate_non_repeatable_subfields(parsed):
                log("holdings_852_duplicate_nr_subfield", True, i, rec_id, detail)
            for detail in fix_852_b_suspect_content(parsed, allow_single_digit_852b):
                log("holdings_852_b_suspect_content", True, i, rec_id, detail)
            for detail in find_853_missing_8(parsed):
                log("holdings_853_missing_8", False, i, rec_id, detail)
            for detail in find_856_missing_u(parsed):
                log("holdings_856_missing_u", False, i, rec_id, detail)

            records_to_write, incomplete_852_details = split_holdings_multiple_852(parsed)
            for detail in incomplete_852_details:
                log("incomplete_852", False, i, rec_id, detail)
            if len(records_to_write) > 1:
                n_new_holdings_records += len(records_to_write) - 1
                log(
                    "split_holdings_multiple_852", True, i, rec_id,
                    f"record had {len(records_to_write) + len(incomplete_852_details)} "
                    f"852 (Location) fields; split into {len(records_to_write)} "
                    "holdings records, one per usable 852",
                )

            resolved_records = []
            additional_004_records: list[ParsedRecord] = []
            for rec in records_to_write:
                main_rec, extra_004_recs = split_holdings_multiple_004(rec)
                resolved_records.append(main_rec)
                if extra_004_recs:
                    n_additional_004_written += len(extra_004_recs)
                    additional_004_records.extend(extra_004_recs)
                    log(
                        "holdings_multiple_004", False, i, rec_id,
                        f"record has {1 + len(extra_004_recs)} 004 (Control "
                        "Number) field(s) -- kept the first here, wrote "
                        f"{len(extra_004_recs)} additional record(s) (one per "
                        f"remaining 004) to {additional_004_path}",
                    )
            records_to_write = resolved_records

            if trim_oversized_record_enabled:
                for rec in records_to_write:
                    trim_detail = trim_oversized_record(rec)
                    if trim_detail:
                        log("trimmed_oversized_record", True, i, rec_id, trim_detail)
            try:
                assembled_records = [assemble_marc(rec) for rec in records_to_write]
                additional_004_assembled = [
                    assemble_marc(rec) for rec in additional_004_records
                ]
            except RepairError as exc:
                print(
                    f"record {i}: {exc} -- writing to {error_path} instead",
                    file=sys.stderr,
                )
                log("unfixable", False, i, rec_id, f"written to {error_path} unchanged: {exc}")
                error_fh.write(rec_text.encode(encoding_used, errors="surrogateescape"))
                n_unfixable += 1
                if rec_id:
                    id_records.append((i, rec_id, rec_text[:5]))
                continue

            if rec_id:
                id_records.append((i, rec_id, assembled_records[0][:5].decode("ascii")))

            for rec, assembled in zip(records_to_write, assembled_records):
                if len(assembled) > 99999 and not allow_oversized_sentinel:
                    # Same reasoning as the bib pipeline's own default
                    # behavior: spec-valid MARC21, but known to abort
                    # FOLIO Data Import's whole batch with no log --
                    # diverted to the _error file instead.
                    print(
                        f"record {i}: {len(assembled)} bytes, requires the MARC21 "
                        f"oversized-length sentinel -- writing to {error_path} "
                        "instead",
                        file=sys.stderr,
                    )
                    log(
                        "unfixable", False, i, rec_id,
                        f"record is {len(assembled)} bytes, requiring the leader's "
                        "MARC21 oversized-length sentinel (99999) -- known to make "
                        "FOLIO Data Import abort the whole job with no log -- "
                        f"written to {error_path} unchanged instead of the main "
                        "output",
                    )
                    error_fh.write(assembled)
                    n_unfixable += 1
                    continue
                if len(assembled) > 99999:
                    log(
                        "oversized_sentinel_fixed", True, i, rec_id,
                        f"record is {len(assembled)} bytes; leader declares the "
                        "MARC21 sentinel 99999 instead (real end is still found "
                        "from the record terminator, nothing lost) -- kept in the "
                        "main output only because allow_oversized_sentinel is set",
                    )

                offset = out_fh.tell()
                out_fh.write(assembled)
                n_written += 1

                has_invalid_tag = False
                for f in rec.fields:
                    if f.tag.isdigit():
                        used_tags.add(f.tag)
                    else:
                        has_invalid_tag = True
                if has_invalid_tag:
                    rec_encoding = "utf-8" if rec.leader[9:10] == "a" else "latin-1"
                    pending_tag_fixes.append((i, offset, len(assembled), rec_encoding))

            for assembled in additional_004_assembled:
                additional_004_fh.write(assembled)

    if pending_tag_fixes:
        replacement_tag = pick_unused_9xx_tag(used_tags)
        fix_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if replacement_tag is None:
            _rewrite_stripping_invalid_tags(
                output_path, pending_tag_fixes, log_entries, fix_ts,
            )
        else:
            with open(output_path, "r+b") as fixup_fh:
                for record_idx, offset, length, rec_encoding in pending_tag_fixes:
                    fixup_fh.seek(offset)
                    raw = fixup_fh.read(length)
                    rec = read_intact_record(raw.decode(rec_encoding))
                    rec_id = record_identifier(rec)
                    for detail in fix_invalid_tags(rec, replacement_tag):
                        log_entries.append(
                            LogEntry("invalid_tag", True, fix_ts, record_idx, rec_id, detail)
                        )
                    fixed_bytes = assemble_marc(rec)
                    assert len(fixed_bytes) == length, (
                        "tag rename must not change a record's total byte length"
                    )
                    fixup_fh.seek(offset)
                    fixup_fh.write(fixed_bytes)

    dup_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    log_entries.extend(find_duplicate_identifiers(id_records, dup_ts))

    active_categories = set(_HOLDINGS_ALWAYS_ACTIVE_CATEGORIES) | {"duplicate_identifier"}
    if fix_missing_852c:
        active_categories.add("added_missing_852c")
    resolved_full_categories = _ALWAYS_FULL_CATEGORIES | (full_categories or set())
    count_notes = {}
    if n_new_holdings_records:
        count_notes["split_holdings_multiple_852"] = (
            f"{n_new_holdings_records} new holdings record(s) added"
        )
    if n_additional_004_written:
        count_notes["holdings_multiple_004"] = (
            f"{n_additional_004_written} additional record(s) written to "
            f"{os.path.basename(additional_004_path)}"
        )
    count_notes = count_notes or None
    _write_run_timing_header(
        log_path, run_started, datetime.now(timezone.utc),
        input_path, output_path,
        error_path if os.path.exists(error_path) else None,
        source_count=n_total, repaired_count=n_written, problem_count=n_unfixable,
    )
    write_log(
        log_path, log_entries, active_categories=active_categories,
        full_categories=resolved_full_categories, count_notes=count_notes,
        include_zero=include_zero,
    )
    n_not_fixed = sum(1 for e in log_entries if _section_for(e)[1] == "NOT FIXED")
    return {
        "total": n_total,
        "written": n_written,
        "unfixable": n_unfixable,
        "log_lines": len(log_entries),
        "not_fixed": n_not_fixed,
        "additional_004_written": n_additional_004_written,
    }


# ---------------------------------------------------------------------------
# Run profiles: named sets of switches kept as JSON files (see profiles/)
# ---------------------------------------------------------------------------

DEFAULT_PROFILE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "profiles")


# Per-run paths, one-shot modes, and the profile options themselves never go
# in a profile.
PROFILE_EXCLUDED_DESTS = frozenset({
    "help", "input", "out", "log", "count", "mrk", "overrides",
    "sample_problems", "sample_log",
    "profile", "profile_dir", "no_profile", "list_profiles",
})


_PROFILE_TOP_LEVEL_KEYS = frozenset({"name", "description", "options"})


class ProfileError(Exception):
    """A profile file is unreadable or holds options that don't validate."""


def profile_option_actions(parser: argparse.ArgumentParser) -> dict:
    """dest -> argparse action, for every option a profile may set."""
    return {
        a.dest: a for a in parser._actions
        if a.option_strings and a.dest not in PROFILE_EXCLUDED_DESTS
    }


def profile_value_kind(action: argparse.Action) -> str:
    """'bool', 'list', 'int' or 'str' -- the JSON type a profile must use for this option."""
    if isinstance(action, (
        argparse._StoreTrueAction, argparse._StoreFalseAction, argparse.BooleanOptionalAction,
    )):
        return "bool"
    if isinstance(action, argparse._AppendAction):
        return "list"
    if action.type is int:
        return "int"
    return "str"


def validate_profile_options(parser: argparse.ArgumentParser, options: dict, source: str) -> dict:
    """Check a profile's options against the real parser; raise ProfileError on the first one."""
    actions = profile_option_actions(parser)
    unknown = sorted(set(options) - set(actions))
    if unknown:
        raise ProfileError(
            f"{source}: unknown option(s) {', '.join(unknown)} -- keys are the long option "
            f"names without dashes, e.g. \"fix_mojibake\" (see --help)"
        )
    for dest, value in options.items():
        action = actions[dest]
        kind = profile_value_kind(action)
        ok = {
            "bool": isinstance(value, bool),
            "list": isinstance(value, list) and all(isinstance(v, str) for v in value),
            "int": isinstance(value, int) and not isinstance(value, bool),
            "str": value is None or isinstance(value, str),
        }[kind]
        if not ok:
            raise ProfileError(f"{source}: option {dest!r} must be a {kind}, got {value!r}")
        if action.choices and value is not None and kind == "str" and value not in action.choices:
            raise ProfileError(
                f"{source}: option {dest!r} must be one of {', '.join(map(str, action.choices))}, "
                f"got {value!r}"
            )
    return dict(options)


def load_profile_file(path: str, parser: argparse.ArgumentParser) -> dict:
    """Read and validate one profile file: {"name", "description", "path", "options"}."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        raise ProfileError(f"{path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("options"), dict):
        raise ProfileError(f"{path}: expected a JSON object with an \"options\" object")
    extra = sorted(set(data) - _PROFILE_TOP_LEVEL_KEYS)
    if extra:
        raise ProfileError(f"{path}: unknown top-level key(s) {', '.join(extra)}")
    name = data.get("name") or os.path.splitext(os.path.basename(path))[0]
    description = data.get("description") or ""
    if not isinstance(name, str) or not isinstance(description, str):
        raise ProfileError(f"{path}: \"name\" and \"description\" must be strings")
    return {
        "name": name,
        "description": description,
        "path": path,
        "options": validate_profile_options(parser, data["options"], path),
    }


def resolve_profile_dir(requested: str | None) -> str:
    """--profile-dir if it is a readable directory, else the profiles/ dir next to the script."""
    if requested:
        if os.path.isdir(requested) and os.access(requested, os.R_OK):
            return requested
        print(
            f"warning: --profile-dir {requested!r} is not a readable directory; "
            f"using {DEFAULT_PROFILE_DIR}",
            file=sys.stderr,
        )
    return DEFAULT_PROFILE_DIR


def discover_profiles(directory: str, parser: argparse.ArgumentParser) -> list[dict]:
    """Every valid *.json profile in directory, sorted by file name; invalid ones are
    skipped with a warning on stderr."""
    if not os.path.isdir(directory):
        return []
    profiles = []
    for filename in sorted(os.listdir(directory)):
        if not filename.lower().endswith(".json"):
            continue
        try:
            profiles.append(load_profile_file(os.path.join(directory, filename), parser))
        except ProfileError as exc:
            print(f"warning: skipping invalid profile -- {exc}", file=sys.stderr)
    return profiles


def _choose_profile_interactively(profiles: list[dict], parser: argparse.ArgumentParser):
    print("Multiple profiles found:")
    for i, p in enumerate(profiles, 1):
        print(f"  {i}. {p['name']}" + (f" -- {p['description']}" if p["description"] else ""))
    print("  0. none (use the built-in defaults)")
    while True:
        try:
            answer = input(f"Which profile? [0-{len(profiles)}]: ").strip()
        except EOFError:
            parser.error("no profile chosen")
        if answer == "0":
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(profiles):
            return profiles[int(answer) - 1]
        for p in profiles:
            if answer and answer.lower() == p["name"].lower():
                return p
        print(f"{answer!r} is not a choice -- try again, or Ctrl-C to quit", file=sys.stderr)


def select_profile(parser: argparse.ArgumentParser, args: argparse.Namespace) -> dict | None:
    """The profile to run with, or None. --no-profile: none. --profile NAME|PATH: that one.
    Otherwise discover: none found -> None, one -> it, several -> ask (error if not a terminal)."""
    if args.no_profile:
        return None
    if args.profile and os.path.isfile(args.profile):
        try:
            return load_profile_file(args.profile, parser)
        except ProfileError as exc:
            parser.error(str(exc))
    profiles = discover_profiles(resolve_profile_dir(args.profile_dir), parser)
    if args.profile:
        for p in profiles:
            stem = os.path.splitext(os.path.basename(p["path"]))[0]
            if args.profile.lower() in (p["name"].lower(), stem.lower()):
                return p
        found = ", ".join(p["name"] for p in profiles) or "none"
        parser.error(f"no profile {args.profile!r} found (available: {found})")
    if not profiles:
        return None
    if len(profiles) == 1:
        return profiles[0]
    if not sys.stdin.isatty():
        parser.error(
            "multiple profiles found and stdin is not a terminal -- pass --profile NAME "
            "or --no-profile (found: " + ", ".join(p["name"] for p in profiles) + ")"
        )
    return _choose_profile_interactively(profiles, parser)


def list_profiles_command(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    directory = resolve_profile_dir(args.profile_dir)
    profiles = discover_profiles(directory, parser)
    if not profiles:
        print(f"no profiles found in {directory}")
        return 0
    print(f"{len(profiles)} profile(s) in {directory}:")
    for p in profiles:
        print(f"  {p['name']} ({os.path.basename(p['path'])})"
              + (f" -- {p['description']}" if p["description"] else ""))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "input", nargs="?", default=None,
        help="MARC file (.mrc) or pasted-text file with one or more records "
        "-- prompted for interactively if omitted",
    )
    parser.add_argument(
        "-o", "--out", help="output .mrc file (default: INPUT_repaired.mrc next to the input)"
    )
    parser.add_argument(
        "--count",
        action="store_true",
        help="print the number of records in the input file and exit "
        "immediately -- counts raw record-terminator (0x1D) bytes in "
        "big binary chunks without parsing or repairing anything, for "
        "speed on large files. No output file is written",
    )
    parser.add_argument(
        "--split-bib-holdings",
        action="store_true",
        help="split the input into separate bib and holdings files by each "
        "record's leader byte 6 (type of record) first -- no repair is "
        "done as part of the split step itself. Output: INPUT_bib.EXT "
        "and INPUT_holdings.EXT next to the input; a record whose leader "
        "byte 6 isn't a recognized bib or holdings code (or is "
        "missing/corrupted) is never guessed at -- it's written "
        "instead to INPUT_unclassified.EXT (only created if needed) "
        "and reported on stderr. Both resulting files are then "
        "automatically repaired: holdings records via "
        "repair_holdings_records() -- the same categories of "
        "structural/content fix already applied to bib records (bad "
        "length, bad directory, missing 008 [a holdings-specific "
        "32-byte placeholder, not bib's], bad indicators, invalid "
        "subfield codes, mojibake, smart characters, invalid tags), "
        "except MARC-8-to-UTF-8 transcoding (skipped for holdings for "
        "now) and the bib-specific tag-list fixes (245/required-$a/"
        "duplicate-field, which don't apply to holdings semantics; see "
        "also --no-fix-missing-852c to disable the one holdings-"
        "specific content fix) -- output INPUT_holdings_repaired.EXT, "
        "logged to INPUT_holdings_log_TIMESTAMP.log; bib records via "
        "this same pipeline, as if separately run as "
        "`marc_repair.py INPUT_bib.EXT` with this invocation's other "
        "switches and profile (except -o, --log, which are derived "
        "from the bib output) -- output "
        "INPUT_bib_repaired.EXT, logged to "
        "INPUT_bib_repaired_log_TIMESTAMP.log. Either side's own "
        "unfixable records are diverted, byte-for-byte unchanged, to a "
        "separate _error file, same as the main bib pipeline",
    )
    parser.add_argument(
        "--repair-holdings",
        action="store_true",
        help="treat the input as an already-holdings-only MARC file (e.g. "
        "one of --split-bib-holdings' own INPUT_holdings.EXT outputs) and "
        "repair it directly, then exit immediately -- the exact same "
        "repair --split-bib-holdings applies to its holdings output (see "
        "repair_holdings_records()), without re-splitting anything or "
        "touching bib records. Useful when the bib/holdings split was "
        "already done in an earlier run (or by some other tool) and only "
        "the holdings side needs (re-)repairing. Output: INPUT_repaired.EXT "
        "next to the input (or -o/--out); log: INPUT_log_TIMESTAMP.log (or "
        "--log). See --no-fix-missing-852c to disable the one "
        "holdings-specific content fix",
    )
    parser.add_argument(
        "--no-fix-missing-852c",
        dest="fix_missing_852c",
        action="store_false",
        default=True,
        help="(holdings records only, used by --split-bib-holdings and "
        "--repair-holdings) do NOT add a "
        f"placeholder $c (shelving location) subfield -- content "
        f"{DEFAULT_852_C_CONTENT!r} -- to any 852 (Location) field missing "
        "one in an OCLC WMS-sourced record (identified by its own 004; see "
        "_is_wms_holdings_record) -- the one convention where $c is always "
        "the live location subfield. By default one is inserted there, "
        "same as the 008/245 placeholders elsewhere -- 852 $c is real "
        "location data this tool has no way to know, but leaving it out "
        "entirely is worse than a clearly-flagged placeholder; a non-WMS "
        "record missing $c is left untouched, since that system's "
        "location convention may simply be $b instead; each insertion is "
        "logged (see --log) as a placeholder, not a real value",
    )
    parser.add_argument(
        "--allow-single-digit-852b",
        action="store_true",
        default=False,
        help="(holdings records only) do NOT treat a single-digit 852 $b "
        "(Sublocation) -- e.g. $b0 -- as suspect content; leave it "
        "untouched instead of replacing it with "
        f"{DEFAULT_852_LOCATION_CONTENT!r}. By default a single digit is "
        "treated the same as any other purely-numeric $b (see "
        "holdings_852_b_suspect_content): almost always a piece/copy "
        "number miscoded into the wrong subfield, not a real location "
        "code, but pass this flag if your data genuinely uses bare "
        "single-digit location codes",
    )
    parser.add_argument(
        "--mrk",
        nargs="?",
        const="",
        default=None,
        metavar="PATH",
        help="also write a mnemonic .mrk text file; not written unless this "
        "flag is given. PATH is optional -- bare --mrk defaults to OUT.mrk "
        "next to the .mrc output",
    )
    parser.add_argument("--overrides", help="JSON file with manual subfield splits (Mode 2 only)")
    parser.add_argument(
        "--ensure-field",
        action="append",
        default=[],
        metavar="TAG:INDICATORS:CODE=VALUE",
        help="add this field to any record missing TAG (repeatable), "
        "e.g. --ensure-field \"245:00:a=No title\"; for a control field "
        "(tag < 010, e.g. 008), use TAG:CONTENT instead, e.g. "
        "--ensure-field \"008:780615s19uu    xx a                    d\"",
    )
    parser.add_argument(
        "--no-recode-title-only-7xx",
        dest="recode_title_only_7xx",
        action="store_false",
        default=True,
        help="do NOT recode a 700/710/711 that has a $t (title) but no "
        "name in $a as a 730 (Added Entry--Uniform Title, $t moved to "
        "$a). By default this runs BEFORE --strip-missing-required-a, "
        "so the title is kept instead of the field being removed; pass "
        "this flag to leave such fields for that removal instead. The "
        "--log lists the recoded records' identifiers as one "
        "comma-delimited line",
    )
    parser.add_argument(
        "--no-recode-6xx-b-to-a",
        dest="recode_6xx_b_to_a",
        action="store_false",
        default=True,
        help="do NOT recode a 650/651/655 that has no $a but starts with "
        "a $b so that the $b becomes $a. By default this runs BEFORE "
        "--strip-missing-required-a, so the heading is kept instead of "
        "the field being removed; pass this flag to leave such fields "
        "for that removal instead. Every recoded record is listed in "
        "the --log",
    )
    parser.add_argument(
        "--no-strip-missing-required-a",
        dest="strip_missing_required_a",
        action="store_false",
        default=True,
        help="do NOT remove fields (from the tag list in "
        "--required-a-tags-file) that lack a required, non-empty $a "
        "subfield. By default such fields ARE removed; fields with other "
        "non-empty content are logged (see --log) before being "
        "discarded, empty ones are dropped silently",
    )
    parser.add_argument(
        "--required-a-tags-file",
        default=DEFAULT_REQUIRED_A_TAGS_FILE,
        help=f"tag list for the default $a-required-fields removal (see "
        f"--no-strip-missing-required-a), one tag per line "
        f"(default: {DEFAULT_REQUIRED_A_TAGS_FILE})",
    )
    parser.add_argument(
        "--no-merge-duplicate-040",
        dest="merge_duplicate_040",
        action="store_false",
        default=True,
        help="do NOT merge a second (or later) 040 into the first (a later "
        "$c/$d/$e is added, a later different $a/$c becomes a $d, a "
        "mistagged language-code 040 is recoded as a 041). By default this "
        "runs BEFORE the duplicate-non-repeatable-field removal, so the "
        "agency information is kept instead of removed. Logged in full as "
        "merged_duplicate_040",
    )
    parser.add_argument(
        "--no-move-duplicate-240-to-730",
        dest="move_duplicate_240_to_730",
        action="store_false",
        default=True,
        help="do NOT recode a second (or later) 240 as a 730 (or a 246 "
        "when it's just part of the 245 title). Frequency statements "
        "keyed as a 240 are always left for the removal. Logged in full as "
        "moved_duplicate_to_added_entry",
    )
    parser.add_argument(
        "--no-recode-misfiled-duplicate-245",
        dest="recode_misfiled_duplicate_245",
        action="store_false",
        default=True,
        help="do NOT fix the second (or later) 245s that are not plain "
        "variant titles: a near-duplicate is dropped, a real title that "
        "follows a mistagged uniform-title/series 245 takes its place (the "
        "first becomes a 240/730/490), a publication statement becomes a "
        "260, an edition statement a 250, and a further volume/part title "
        "a 740. Anything else is left for the removal. Logged in full as "
        "moved_duplicate_to_added_entry",
    )
    parser.add_argument(
        "--no-move-duplicate-100-to-700",
        dest="move_duplicate_100_to_700",
        action="store_false",
        default=True,
        help="do NOT recode a second (or later) 100 as a 700 (Added Entry--"
        "Personal Name). By default this runs BEFORE the duplicate-non-"
        "repeatable-field removal (see --no-strip-duplicate-non-"
        "repeatable-fields), so the name is kept instead of removed; pass "
        "this flag to leave it for that removal. Logged in full as "
        "moved_duplicate_to_added_entry",
    )
    parser.add_argument(
        "--no-move-duplicate-245-to-246",
        dest="move_duplicate_245_to_246",
        action="store_false",
        default=True,
        help="do NOT recode a second (or later) 245 that is a plain variant "
        "title (no $c, $6, edition statement, ...) as a 246. By default "
        "this runs BEFORE the duplicate-non-repeatable-field removal, so "
        "the title is kept instead of removed; anything that doesn't look "
        "like a clean variant title is left for that removal. Logged in "
        "full as moved_duplicate_to_added_entry",
    )
    parser.add_argument(
        "--no-strip-duplicate-non-repeatable-fields",
        dest="strip_duplicate_non_repeatable_fields",
        action="store_false",
        default=True,
        help="do NOT remove later occurrences of a Not-Repeatable field "
        "(from the tag list in --non-repeatable-tags-file, e.g. a "
        "second 245) that appears more than once. By default all but "
        "the first occurrence ARE removed -- a strict importer like "
        "FOLIO can reject or mishandle the duplicate otherwise -- and "
        "every removed field's exact content is logged in full under "
        "FIXED/REQUIRES ATTENTION (see --log)",
    )
    parser.add_argument(
        "--non-repeatable-tags-file",
        default=DEFAULT_NON_REPEATABLE_TAGS_FILE,
        help=f"tag list for the default duplicate-non-repeatable-field "
        f"removal (see --no-strip-duplicate-non-repeatable-fields), one "
        f"tag per line (default: {DEFAULT_NON_REPEATABLE_TAGS_FILE})",
    )
    parser.add_argument(
        "--no-fix-008-length",
        dest="fix_008_length",
        action="store_false",
        default=True,
        help="do NOT pad/truncate an 008 field that isn't exactly 40 "
        "characters. By default this IS done -- a wrong-length 008 "
        "can make a record unloadable in strict importers -- and "
        "logged in full under FIXED/REQUIRES ATTENTION (see --log)",
    )
    parser.add_argument(
        "--no-fix-mojibake",
        dest="fix_mojibake",
        action="store_false",
        default=True,
        help="do NOT fix double-encoded UTF-8 (\"mojibake\", e.g. text "
        "read once as UTF-8 then mis-read again as Latin-1) in "
        "records already declaring UTF-8. By default this IS fixed "
        "and logged (see --log); only applied when re-decoding the "
        "text as UTF-8 actually succeeds, which is effectively "
        "impossible by coincidence for text that wasn't genuinely "
        "double-encoded",
    )
    parser.add_argument(
        "--b2-lexicon",
        metavar="PATH",
        default=None,
        help="JSON word list built by tools/context_probe/lang_lexicon_probe.py "
        "`trim`; enables filling a destroyed German umlaut vowel (escape junk "
        "+ byte 0xB2) in records with German 008/041 evidence, only for words "
        "the list confirms (logged as fixed_marc8_b2_umlaut). A "
        "lexicon-backed guess, not a verified fix. ON BY DEFAULT if "
        f"{DEFAULT_B2_LEXICON_PATH} exists; pass a PATH here to use a "
        "different lexicon file, or --no-b2-lexicon to turn it off",
    )
    parser.add_argument(
        "--no-b2-lexicon",
        dest="no_b2_lexicon",
        action="store_true",
        default=False,
        help="force the 0xB2 lexicon-gated umlaut fixer OFF, even if "
        f"{DEFAULT_B2_LEXICON_PATH} exists or --b2-lexicon was given",
    )
    parser.add_argument(
        "--no-fix-marc8-diacritic-escapes",
        dest="fix_marc8_diacritic_escapes",
        action="store_false",
        default=True,
        help="do NOT recover a MARC-8 diacritic mark lost to a bogus "
        "script-switching escape wrapped around it (e.g. \"Haure\" + "
        "<escape> + \"au\" -> \"Hauréau\"), for the confirmed high-"
        "volume escape payloads in _MARC8_DIACRITIC_PAYLOADS, or to a "
        "bare un-escaped byte standing in for an Arabic "
        "transliteration mark (dot-below/hamza/ayn -- see "
        "_MARC8_BARE_COMBINING_BYTES/_MARC8_BARE_STANDALONE_BYTES). By "
        "default this IS fixed and logged (see --log) as "
        "fixed_marc8_diacritic; anything not covered still falls "
        "through to suspect_marc8_escape for human review",
    )
    parser.add_argument(
        "--log-full",
        dest="log_full",
        action="append",
        default=[],
        metavar="CATEGORY",
        help="list every matching record for CATEGORY (e.g. "
        "\"removed_null_identifier\" -- the machine-readable name in "
        "the log's own \"=== SECTION: category ===\" headers, also in "
        "docs/REPAIR_CATEGORIES.md) instead of just the header + count "
        "every check that ran always gets (see --log). Repeatable. A "
        "curated set of categories -- real data loss, or the kind of "
        "content problem (e.g. a broken URL) a cataloger would "
        "plausibly want to go fix one-by-one -- are already listed in "
        "full without needing this; use it for anything else you need "
        "the per-record detail on. Has no effect on a category "
        "currently in the INFORMATIONAL section, which is never "
        "listed in full",
    )
    parser.add_argument(
        "--log-zero",
        dest="log_zero",
        action="store_true",
        help="also write a header block for every check that ran but "
        "found nothing (0 records). By default the log is trimmed to "
        "categories with at least one finding, and the rest are listed "
        "by name, with a one-line description, in a closing \"CHECKS "
        "PERFORMED WITH NO FINDINGS\" block so the log still shows the "
        "full scope of what was tested",
    )
    parser.add_argument(
        "--sample-problems",
        nargs="?",
        const="",
        default=None,
        metavar="PATH",
        help="write the raw, original bytes of up to --sample-limit "
        "records that triggered at least one not-fixed finding (e.g. "
        "unfixable, transcode_marc8_failed, suspect_marc8_escape, "
        "suspect_hex_encoded_marc8 -- "
        "anything that would show up under REQUIRES ATTENTION in --log) "
        "to PATH as a small standalone .mrc file -- meant for handing a "
        "compact, representative sample of a file's actual problems to "
        "someone (or something) else for analysis, without sending the "
        "whole file. Records with only auto-fixed findings (e.g. "
        "padded_indicators) don't count -- those aren't problems "
        "needing a second look. PATH is optional -- bare --sample-problems "
        "defaults to sampled_problems.mrc next to the output file",
    )
    parser.add_argument(
        "--sample-limit",
        type=int,
        default=25,
        metavar="N",
        help="max number of records to write to --sample-problems "
        "(default: 25); has no effect without --sample-problems",
    )
    parser.add_argument(
        "--sample-log",
        nargs="?",
        const="",
        default=None,
        metavar="PATH",
        help="write a full log file to PATH documenting every check/"
        "category this tool can log -- one header + description per "
        "category, exactly as --log would produce if every check ran "
        "and found nothing -- then exit immediately. No input is read "
        "or repaired; every count is 0 and there are no per-record "
        "findings. Meant as a reference/template of the log's full "
        "structure, e.g. for building something that parses it. PATH "
        "is optional -- bare --sample-log defaults to "
        "sample_log_TIMESTAMP.log",
    )
    parser.add_argument(
        "--no-fix-misplaced-subfield-codes",
        dest="fix_misplaced_subfield_codes",
        action="store_false",
        default=True,
        help="do NOT recover a subfield whose code is a single stray "
        "space immediately followed by its real, still-present code "
        "(e.g. raw \"\\x1f c2000.\" -- code ' ', data 'c2000.' -- really "
        "meant $c \"c2000.\"; a real, high-volume defect in some source "
        "data). By default this runs BEFORE --strip-invalid-subfield-"
        "codes so these are recovered rather than discarded; pass this "
        "flag to leave such subfields for --strip-invalid-subfield-codes "
        "to remove instead. Header + count always logged (see --log); "
        "not listed per-record by default (see --log-full) since this "
        "can be a large fraction of a file with this defect",
    )
    parser.add_argument(
        "--no-strip-empty-fields",
        dest="strip_empty_fields",
        action="store_false",
        default=True,
        help="do NOT remove a data field where every subfield's data is "
        "empty (e.g. a bare \"$a\" with nothing after it), for any tag. "
        "By default such fields ARE removed -- unlike "
        "--no-strip-missing-required-a this isn't scoped to a tag list, "
        "since a field with zero non-empty subfields holds no "
        "information under any tag. Nothing is discarded (there's "
        "nothing there), so this isn't logged either way",
    )
    parser.add_argument(
        "--no-fix-bad-indicators",
        dest="fix_bad_indicators",
        action="store_false",
        default=True,
        help="do NOT pad a data field's indicators with space(s) when 0 "
        "or 1 characters precede its first subfield delimiter instead of "
        "the required 2 (a common corruption). By default such fields "
        "ARE padded and logged (see --log); with this flag such a field "
        "fails Mode 1 instead and the whole record falls back to Mode "
        "2/UNRESOLVED",
    )
    parser.add_argument(
        "--no-fix-invalid-leader-bytes",
        dest="fix_invalid_leader_bytes",
        action="store_false",
        default=True,
        help="do NOT default leader bytes 05/06/08/17 (record status, type "
        "of record, type of control, encoding level) when they hold a "
        "value outside their valid MARC21 code set. By default each is "
        "reset to a fixed default (see fix_invalid_leader_bytes()) and "
        "logged (see --log); pass this flag to leave them as-is instead",
    )
    parser.add_argument(
        "--no-reattach-orphaned-fields",
        dest="reattach_orphaned_fields",
        action="store_false",
        default=True,
        help="do NOT reattach a trailing field that's physically present "
        "in the file but missing its own directory entry (so excluded "
        "from its record's own declared length) -- a real defect seen "
        "in production data, always shaped like a personal name heading "
        "(see reattach_orphaned_trailing_fields). By default such a "
        "field is reattached to the record right before it as a new =700 "
        "and logged under FIXED/REQUIRES ATTENTION (see --log), since the "
        "tag is a guess (however confident); with this flag it's left "
        "unfixable instead -- diverted to the _error output file like any "
        "other record neither mode can resolve",
    )
    parser.add_argument(
        "--no-add-default-245",
        dest="add_default_245",
        action="store_false",
        default=True,
        help="do NOT insert a placeholder 245 ($a \"No title\") for a "
        "record with no 245 at all. By default one is inserted and "
        "logged (see --log) -- many real-world imports outright reject a "
        "record with no title -- rather than leaving such records with "
        "no 245; --ensure-field \"245:...\" for a specific record takes "
        "priority over this default. Pass this flag to instead divert "
        "such records, byte-for-byte unchanged, to a _needs_review file "
        "alongside the main output (logged under NOT FIXED, category "
        "diverted_for_review) rather than inserting a placeholder or "
        "leaving them without a title in the main output. --guarantee-"
        "importable implies this flag too, for the same reason it diverts "
        "an oversized record instead of leaving it in the main output",
    )
    parser.add_argument(
        "--no-fix-invalid-tags",
        dest="fix_invalid_tags",
        action="store_false",
        default=True,
        help="do NOT rename fields whose tag isn't 3 numeric digits (e.g. "
        "\"ABC\" from directory corruption) to an unused tag in the "
        "900-999 locally-defined-field range. By default this is done "
        "and logged (see --log); pass this flag to leave such fields "
        "with their invalid tag instead. The replacement tag is chosen "
        "from tags seen during the normal single pass over the file, so "
        "this adds no extra full pass -- only records with an invalid "
        "tag (rare) get a second, targeted look afterward",
    )
    parser.add_argument(
        "--remap-999-to-945",
        dest="remap_999_to_945",
        action="store_true",
        default=False,
        help="retag 999 fields to 945 (indicators forced to \"ff\"). "
        "999 (Sierra's internal item-linking field, not part of MARC21) "
        "is left as-is by default; pass this flag to have every one "
        "retagged instead to 945, a locally-defined field other "
        "systems will actually accept. Off by default since it's not a "
        "structural defect and not every source is Sierra-originated. "
        "Header + count always logged when this runs (see --log); not "
        "listed per-record by default (see --log-full) since this "
        "remap is typically extremely high-volume -- multiple 999s per "
        "record",
    )
    parser.add_argument(
        "--no-normalize-subfield-9",
        dest="normalize_subfield_9",
        action="store_false",
        default=True,
        help="do NOT rewrite $9 subfield codes to $0. By default every "
        "$9 is unconditionally rewritten to $0 (a long-standing cleanup "
        "convention treating $9 as a legacy/local stand-in for the "
        "standard authority-linking subfield); pass this flag to leave "
        "$9 subfields as-is instead. Header + count always logged (see "
        "--log); not listed per-record by default (see --log-full) "
        "since this can be nearly every record in a file that uses $9",
    )
    parser.add_argument(
        "--check-isbn-issn-checksum",
        action="store_true",
        help="detect a 020 (ISBN) or 022 (ISSN) $a whose check digit "
        "fails the standard checksum for its length. Off by default "
        "since it's a detect-only, no-fix check on data that's often "
        "already correct; pass this flag to have it run and be logged "
        "as invalid_isbn_issn_checksum -- INFORMATIONAL, so only a "
        "header + count, never the full per-record list",
    )
    parser.add_argument(
        "--check-dangling-880-links",
        action="store_true",
        help="detect an 880 field whose $6 linking subfield references "
        "a tag that doesn't exist elsewhere in the record. Off by "
        "default since it's a detect-only, no-fix check; pass this "
        "flag to have it run and be logged as dangling_880_link -- "
        "INFORMATIONAL, so only a header + count, never the full "
        "per-record list",
    )
    parser.add_argument(
        "--no-check-non-standard-fields",
        dest="check_non_standard_fields",
        action="store_false",
        default=True,
        help="do NOT report fields whose tag isn't in "
        "--standard-marc-fields-file. By default this report-only check "
        "runs (bib records only; 9XX never reported) and logs the first "
        f"{NON_STANDARD_FIELD_DETAIL_LIMIT} occurrences of each such tag "
        "under INFORMATION ONLY as non_standard_marc_fields; nothing is "
        "changed or removed",
    )
    parser.add_argument(
        "--standard-marc-fields-file",
        default=DEFAULT_STANDARD_MARC_FIELDS_FILE,
        help="accepted/obsolete tag list for the non-standard-field "
        f"check, editable (default: {DEFAULT_STANDARD_MARC_FIELDS_FILE})",
    )
    parser.add_argument(
        "--allow-oversized-sentinel",
        action="store_true",
        default=False,
        help="allow a record that needs MARC21's oversized-length leader "
        "sentinel (true length > 99999 bytes) to stay in the main output, "
        "logged as oversized_sentinel_fixed (spec-valid MARC21; the real "
        "end is still found from the record terminator, nothing lost). "
        "Off by default: such a record is instead diverted to the _error "
        "file and logged as unfixable, because FOLIO Data Import "
        "(confirmed in practice) aborts the entire batch with no job log "
        "at all on a record like this, rather than rejecting just that "
        "one record -- so the main/repaired output is importable by "
        "default, not something you have to opt into. Pass this flag "
        "only if you know your import target handles the sentinel "
        "correctly and specifically want the record kept instead",
    )
    parser.add_argument(
        "--trim-oversized-record",
        dest="trim_oversized_record",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="shrink a record over 99999 bytes by removing whole fields "
        "until it fits: 9xx (never 999 ff) first, then 5xx with their "
        "linked 880s, then 6xx, largest first; logged as "
        "trimmed_oversized_record. On by default; --no-trim-oversized-record "
        "instead diverts such a record to the _error file (or keeps it with "
        "the sentinel, see --allow-oversized-sentinel)",
    )
    parser.add_argument(
        "--source-system",
        choices=SOURCE_SYSTEMS,
        default=None,
        help="source system of the bib records: 'sierra' requires a 907 $a on "
        "every record (no fallback to 001), 'wms' and 'other' require a 001 "
        "('wms' also treats the [wms] tags in standard_marc_fields.txt as "
        "standard); a record "
        "without one is UNFIXABLE (diverted to the _error file). Asked for "
        "interactively when not given; skipped when stdin isn't a terminal.",
    )
    parser.add_argument(
        "--guarantee-importable",
        action="store_true",
        default=False,
        help="also divert a record with no 245 to the _needs_review file "
        "instead of getting a guessed placeholder title (implies --no-"
        "add-default-245), for the same \"don't leave a guess in the main "
        "output\" reasoning --allow-oversized-sentinel's absence already "
        "applies by default. Off by default: a missing 245 still gets a "
        "real title (backfilled from 130/240/246/242/222/880, or a "
        "placeholder) so the record imports normally. With this flag, "
        "the main output ends up split three ways -- records that break "
        "an import target entirely (_error, always the case for an "
        "oversized record unless --allow-oversized-sentinel is given), "
        "records a human should look at before deciding what to do with "
        "them (_needs_review), and records safe to import as they are "
        "(the main output)",
    )
    parser.add_argument(
        "--no-normalize-smart-characters",
        dest="normalize_smart_characters",
        action="store_false",
        default=True,
        help="do NOT replace typographic \"smart\" Unicode punctuation "
        "(curly quotes, em/en dashes, ellipsis -- see SMART_CHAR_MAP) with "
        "plain ASCII equivalents. By default this normalization runs on "
        "every subfield and control field and is logged (see --log); pass "
        "this flag to leave smart characters as-is instead",
    )
    parser.add_argument(
        "--log",
        help="single combined log file for every removal, warning, and "
        "pass-through-unchanged notice from this run (default: "
        "OUT_log_TIMESTAMP.log). The run timestamp is always inserted "
        "before the extension -- even when this is given explicitly -- so "
        "repeat runs never overwrite or blend into a prior run's log",
    )
    parser.add_argument(
        "--no-transcode-marc8",
        dest="transcode_marc8",
        action="store_false",
        default=True,
        help="do NOT convert MARC-8/ANSEL encoded records to UTF-8. By "
        "default such records ARE converted (leader's encoding byte "
        "flipped accordingly; records already declaring Unicode are "
        "left alone); header + count always logged, not listed "
        "per-record by default (see --log-full) since this can be "
        "nearly every record in a legacy file. Requires pymarc (pip "
        "install -r "
        "requirements.txt) -- if it's not installed, the run fails "
        "immediately rather than silently producing non-UTF-8 output; "
        "pass this flag to skip transcoding deliberately instead",
    )
    parser.add_argument(
        "--no-marc8-corpus-lookup",
        dest="marc8_corpus_lookup",
        action="store_false",
        default=True,
        help="do NOT build the same-file MARC-8 corpus index used to "
        "sharpen suspect_marc8_escape suggestions (see "
        "build_marc8_corpus_index) -- looks up a finding's surrounding "
        "text against every correctly-encoded accented word elsewhere "
        "in this same file, naming the specific word (e.g. "
        "\"Tubingen\") instead of a generic \"could be accented\" hint "
        "when exactly one match is found. On by default; on a large "
        "legacy MARC-8 file this costs one extra full read of the "
        "input, held in memory as a single decoded string, on top of "
        "this tool's normal streaming pass -- pass this flag to skip "
        "that and keep the generic hint for every finding instead",
    )
    parser.add_argument(
        "--profile",
        metavar="NAME|PATH",
        help="run with a saved profile -- a JSON file of options (see profiles/examples/ and "
        "marc_repair_profile.py) -- instead of choosing one. NAME is matched against the "
        "profiles in the profile directory; PATH is a profile file. Without this flag the "
        "profile directory is searched: one profile is used automatically (its name is "
        "printed), several are offered as a menu, none leaves the built-in defaults. "
        "Switches given on the command line override the profile (a repeatable switch such "
        "as --log-full adds to the profile's list)",
    )
    parser.add_argument(
        "--profile-dir",
        metavar="DIR",
        help=f"directory to search for profiles (default: {DEFAULT_PROFILE_DIR}); if DIR "
        "is not a readable directory a warning is printed and the default is used",
    )
    parser.add_argument(
        "--no-profile",
        action="store_true",
        help="ignore all profiles and run with the built-in defaults plus the switches given",
    )
    parser.add_argument(
        "--list-profiles",
        action="store_true",
        help="list the profiles found in the profile directory and exit",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args, _ = parser.parse_known_args(argv)
    if args.list_profiles:
        return list_profiles_command(parser, args)
    if not (args.count or args.sample_log is not None):
        profile = select_profile(parser, args)
        if profile:
            print(f"Using profile: {profile['name']} ({profile['path']})")
            parser.set_defaults(**profile["options"])
    args = parser.parse_args(argv)
    return run(parser, args)


def resolve_source_system(args: argparse.Namespace) -> None:
    """Ask for the source system if none was given (terminal only), then always say what
    this run will use, so it is never a silent default. Runs once per invocation: the bib
    half of --split-bib-holdings inherits the answer."""
    if getattr(args, "_source_system_resolved", False):
        return
    args._source_system_resolved = True
    if args.source_system is None and sys.stdin.isatty():
        print("Source system of these records:")
        for i, name in enumerate(SOURCE_SYSTEMS, 1):
            print(f"  {i}. {name} ({_SOURCE_SYSTEM_IDENTIFIER[name]} required)")
        while args.source_system is None:
            answer = input(f"Which source system? [1-{len(SOURCE_SYSTEMS)}]: ").strip().lower()
            if answer.isdigit() and 1 <= int(answer) <= len(SOURCE_SYSTEMS):
                args.source_system = SOURCE_SYSTEMS[int(answer) - 1]
            elif answer in SOURCE_SYSTEMS:
                args.source_system = answer
            else:
                print(f"please enter a number from 1 to {len(SOURCE_SYSTEMS)}", file=sys.stderr)
    if args.source_system == "sierra":
        print("Source system: sierra (every bib record needs a 907 $a)")
    elif args.source_system == "wms":
        print("Source system: wms (every bib record needs a 001)")
    elif args.source_system == "other":
        print("Source system: other (every bib record needs a 001)")
    else:
        print(
            "Source system: NOT SET -- the required-identifier check is skipped "
            "(pass --source-system sierra|wms|other, or run from a terminal to be asked)"
        )


def run(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    """Everything main() does once the arguments (command line plus any profile) are final."""
    if args.sample_log is not None:
        run_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        log_path = (
            _timestamped_log_path(args.sample_log, run_ts) if args.sample_log
            else f"sample_log_{run_ts}.log"
        )
        now = datetime.now(timezone.utc)
        _write_run_timing_header(
            log_path, now, now,
            "(none -- --sample-log, no input read)",
            "(none -- --sample-log, no input read)",
            "(none -- --sample-log, no input read)",
        )
        write_log(log_path, [], active_categories=set(_CHECK_DESCRIPTIONS), include_zero=True)
        print(
            f"sample log ({len(_CHECK_DESCRIPTIONS)} category header(s), "
            f"all 0 record(s)) written to {log_path}"
        )
        return 0

    if args.input and not os.path.isfile(args.input):
        # a path given on the command line gets the same check as a typed one
        if not sys.stdin.isatty():
            parser.error(f"input file {args.input!r} is not a file")
        print(f"{args.input!r} is not a file -- enter another path", file=sys.stderr)
        args.input = None

    while not args.input:
        try:
            args.input = input("Input MARC file: ").strip()
        except EOFError:
            parser.error("no input file given")
        if not args.input:
            print("no input file given -- try again, or Ctrl-C to quit", file=sys.stderr)
            continue
        if not os.path.isfile(args.input):
            print(f"{args.input!r} is not a file -- try again", file=sys.stderr)
            args.input = None

    if args.count:
        print(f"{count_records(args.input)} record(s) in {args.input}")
        return 0

    if args.split_bib_holdings:
        resolve_source_system(args)  # asked before the long split, used by the bib half
        base, ext = os.path.splitext(args.input)
        bib_path = f"{base}_bib{ext}"
        holdings_path = f"{base}_holdings{ext}"
        unclassified_path = f"{base}_unclassified{ext}"
        split_start = time.perf_counter()
        split_progress = ProgressReporter(total_bytes=os.path.getsize(args.input))
        counts = split_bib_holdings(
            args.input,
            bib_path,
            holdings_path,
            unclassified_path,
            on_progress=split_progress.on_progress,
            on_record=split_progress.maybe_print,
        )
        split_progress.finish()
        print(f"{counts['bib']} bib record(s) written to {bib_path}")
        print(f"{counts['holdings']} holdings record(s) written to {holdings_path}")
        if counts["unclassified"]:
            print(
                f"{counts['unclassified']} record(s) could not be classified as "
                f"bib or holdings from leader byte 6 -- written unchanged to "
                f"{unclassified_path} instead of being guessed at or dropped",
                file=sys.stderr,
            )
        if counts["holdings"]:
            holdings_repaired_path = f"{base}_holdings_repaired.mrc"
            run_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            holdings_log_path = f"{base}_holdings_repaired_log_{run_ts}.log"
            holdings_progress = ProgressReporter(total_bytes=os.path.getsize(holdings_path))
            result = repair_holdings_records(
                holdings_path,
                holdings_repaired_path,
                holdings_log_path,
                fix_missing_852c=args.fix_missing_852c,
                allow_single_digit_852b=args.allow_single_digit_852b,
                allow_oversized_sentinel=args.allow_oversized_sentinel,
                trim_oversized_record_enabled=args.trim_oversized_record,
                full_categories=set(args.log_full),
                include_zero=args.log_zero,
                on_progress=holdings_progress.on_progress,
                on_record=holdings_progress.maybe_print,
                on_estimate=holdings_progress.maybe_print_estimate,
            )
            holdings_progress.finish()
            print(
                f"{result['written']} holdings record(s) repaired from "
                f"{result['total']} input record(s) and written to "
                f"{holdings_repaired_path} "
                f"({result['total'] - result['unfixable']} corrected/passed clean, "
                f"{result['unfixable']} diverted to "
                f"{_error_output_path(holdings_repaired_path)})"
            )
            print(
                f"holdings log (findings only; checks with none are listed at the end; --log-zero for all headers) written to "
                f"{holdings_log_path} ({result['log_lines']} record-level finding(s): "
                f"{result['not_fixed']} not fixed, "
                f"{result['log_lines'] - result['not_fixed']} fixed)",
                file=sys.stderr,
            )
            if result["additional_004_written"]:
                print(
                    f"{result['additional_004_written']} additional holdings record(s) "
                    "(one per 004 past a record's first) written to "
                    f"{_additional_004_output_path(holdings_repaired_path)}",
                    file=sys.stderr,
                )
        if counts["bib"]:
            bib_repaired_path = f"{base}_bib_repaired.mrc"
            # Re-enters run() for the bib side with this invocation's own
            # resolved arguments (command line + profile), as if the user
            # separately ran `marc_repair.py bib_path -o bib_repaired_path`
            # with the same switches -- reuses the whole bib pipeline below
            # rather than duplicating it. It must not go back through
            # main(): that would select a profile again, and a profile that
            # sets split_bib_holdings would split the bib file forever.
            # Per-run output paths are not shared with the holdings side:
            # --log is left to its default (derived from the bib output),
            # and a bare --mrk / --sample-problems is kept bare so each
            # derives its path from the bib output.
            bib_args = argparse.Namespace(**vars(args))
            bib_args.input = bib_path
            bib_args.out = bib_repaired_path
            bib_args.split_bib_holdings = False
            bib_args.log = None
            bib_args.mrk = None if args.mrk is None else ""
            bib_args.sample_problems = None if args.sample_problems is None else ""
            run(parser, bib_args)
        elapsed = time.perf_counter() - split_start
        print(f"done in {elapsed:.2f}s")
        return 0

    if args.repair_holdings:
        base, _ext = os.path.splitext(args.input)
        out_path = args.out or f"{base}_repaired.mrc"
        run_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        log_path = (
            _timestamped_log_path(args.log, run_ts) if args.log
            else os.path.splitext(out_path)[0] + f"_log_{run_ts}.log"
        )
        repair_start = time.perf_counter()
        progress = ProgressReporter(total_bytes=os.path.getsize(args.input))
        result = repair_holdings_records(
            args.input,
            out_path,
            log_path,
            fix_missing_852c=args.fix_missing_852c,
            allow_single_digit_852b=args.allow_single_digit_852b,
            allow_oversized_sentinel=args.allow_oversized_sentinel,
            trim_oversized_record_enabled=args.trim_oversized_record,
            full_categories=set(args.log_full),
            include_zero=args.log_zero,
            on_progress=progress.on_progress,
            on_record=progress.maybe_print,
            on_estimate=progress.maybe_print_estimate,
        )
        progress.finish()
        elapsed = time.perf_counter() - repair_start
        print(
            f"Wrote {result['written']} holdings record(s) from "
            f"{result['total']} input record(s) to {out_path} "
            f"({result['total'] - result['unfixable']} corrected/passed clean, "
            f"{result['unfixable']} diverted to {_error_output_path(out_path)}) "
            f"in {elapsed:.2f}s"
        )
        print(
            f"log (findings only; checks with none are listed at the end; --log-zero for all headers) written to {log_path} "
            f"({result['log_lines']} record-level finding(s): {result['not_fixed']} not "
            f"fixed, {result['log_lines'] - result['not_fixed']} fixed)",
            file=sys.stderr,
        )
        if result["additional_004_written"]:
            print(
                f"{result['additional_004_written']} additional holdings record(s) "
                "(one per 004 past a record's first) written to "
                f"{_additional_004_output_path(out_path)}",
                file=sys.stderr,
            )
        if result["unfixable"]:
            print(
                f"{result['unfixable']} record(s) could not be auto-repaired at all -- "
                f"written, byte-for-byte unchanged, to {_error_output_path(out_path)} "
                "instead.",
                file=sys.stderr,
            )
            return 1
        return 0

    # The default pipeline below applies bib-specific fixes unconditionally
    # (e.g. forcing every 008 to exactly 40 bytes) -- silently correct for a
    # bib record, but actively destructive for a holdings one, whose 008 is
    # 32 bytes with a completely different fixed-field layout. A real run
    # against a holdings-only file with no --repair-holdings/--split-bib-
    # holdings flag did exactly this: force-padded every record's 008,
    # corrupting all of them. A cheap probe (see _sniff_record_types) of
    # this file's own leaders -- not a guess -- catches that mistake before
    # any output is written, rather than after.
    n_bib_sample, n_holdings_sample = _sniff_record_types(args.input)
    if n_holdings_sample and not n_bib_sample:
        print(
            f"error: {args.input} looks like a holdings-only file (leader "
            "byte 6 identifies every sampled record as holdings, not "
            "bibliographic) -- this default pipeline applies bib-specific "
            "fixes (e.g. forcing every 008 to exactly 40 bytes) that would "
            "corrupt a holdings record's own 32-byte 008 and other "
            "holdings-specific structure. Use --repair-holdings instead "
            "(or --split-bib-holdings if this file actually mixes bib and "
            "holdings records).",
            file=sys.stderr,
        )
        return 2

    resolve_source_system(args)

    if args.transcode_marc8:
        try:
            import pymarc.marc8  # noqa: F401
        except ImportError:
            # UTF-8 output is a hard requirement -- silently skipping
            # the one thing that makes MARC-8 records comply with it
            # would leave the output non-conformant without the user
            # ever explicitly choosing that. Fail loudly instead;
            # --no-transcode-marc8 remains available for anyone who
            # explicitly wants non-UTF-8 output preserved as-is.
            print(
                "pymarc not installed, but MARC-8-to-UTF-8 transcoding runs "
                "by default so this run's output would not be all UTF-8 -- "
                "install it (pip install -r requirements.txt) or pass "
                "--no-transcode-marc8 if you explicitly want non-UTF-8 "
                "records left as-is",
                file=sys.stderr,
            )
            return 1

    if args.no_b2_lexicon:
        b2_lexicon_path = None
    elif args.b2_lexicon is not None:
        b2_lexicon_path = args.b2_lexicon
    elif os.path.isfile(DEFAULT_B2_LEXICON_PATH):
        b2_lexicon_path = DEFAULT_B2_LEXICON_PATH
    else:
        b2_lexicon_path = None

    _banner = "*" * 78
    print(_banner, file=sys.stderr)
    if b2_lexicon_path:
        print(
            f"*** 0xB2 LEXICON MODE: ON  -- using {b2_lexicon_path} "
            "(--no-b2-lexicon to disable)",
            file=sys.stderr,
        )
    else:
        print(
            "*** 0xB2 LEXICON MODE: OFF -- German umlaut-vowel guessing "
            "disabled (--b2-lexicon PATH to enable)",
            file=sys.stderr,
        )
    print(_banner, file=sys.stderr)

    start_time = time.perf_counter()
    run_started = datetime.now(timezone.utc)

    out_path = args.out or _default_output_path(args.input)
    # --guarantee-importable implies the same treatment --no-add-default-245
    # gives a missing 245 (divert to _needs_review, same as the
    # oversized-sentinel case diverts to _error) rather than inventing a
    # placeholder title -- the whole point of --guarantee-importable is
    # that nothing in the main output has an uncertain fate FOLIO might
    # reject without a human looking at it first, and a guessed "No
    # title" is exactly that kind of guess. An explicit --no-add-
    # default-245 has the same effect either way, so this is a no-op if
    # both are given together.
    add_default_245_effective = args.add_default_245 and not args.guarantee_importable
    run_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    required_a_tags = (
        load_tag_list(args.required_a_tags_file)
        if args.strip_missing_required_a
        else set()
    )
    non_repeatable_tags = (
        load_tag_list(args.non_repeatable_tags_file)
        if args.strip_duplicate_non_repeatable_fields
        else set()
    )
    non_standard_tally = (
        NonStandardFieldTally(
            *load_standard_marc_fields(args.standard_marc_fields_file, args.source_system)
        )
        if args.check_non_standard_fields
        else None
    )
    ensure_specs = [parse_ensure_field_spec(s) for s in args.ensure_field]

    overrides = _load_overrides(args.overrides) if args.overrides else {}

    # repair_record's overrides param expects int -> (indicators, subfields) tuples
    normalized_overrides = {
        rec_idx: {fi: spec for fi, spec in fields.items()}
        for rec_idx, fields in overrides.items()
    }

    encoding_used = detect_encoding(args.input)
    if args.marc8_corpus_lookup and encoding_used == "latin-1":
        # build_marc8_corpus_index is a full pass over the whole input file
        # that runs before the first progress line prints -- on a big file
        # this is a real, if bounded, multi-second gap with no output at
        # all otherwise (see HANDOFF.md).
        print("Scanning input file for MARC-8 corpus lookup...", file=sys.stderr)
    marc8_corpus_index = (
        build_marc8_corpus_index(args.input, encoding_used)
        if args.marc8_corpus_lookup
        else ({}, {})
    )
    b2_lexicon = load_b2_lexicon(b2_lexicon_path) if b2_lexicon_path else None
    mrk_path = None
    if args.mrk is not None:
        mrk_path = args.mrk or os.path.splitext(out_path)[0] + ".mrk"
    sample_problems_path = None
    if args.sample_problems is not None:
        sample_problems_path = args.sample_problems or os.path.join(
            os.path.dirname(out_path), "sampled_problems.mrc"
        )

    # Invalid-tag fixing (see fix_invalid_tags) needs to know every tag
    # used anywhere in the file before it can safely pick a 9XX
    # replacement that won't collide with a real field elsewhere -- but
    # that's only knowable once the whole file's been read, and this
    # tool otherwise makes exactly one streaming pass over the input. So
    # any record found with an invalid tag during the main pass below is
    # written out UNCHANGED for now and remembered here (by its exact
    # byte offset + length in the output, both fixed once written -- a
    # tag rename never changes a record's total length); `used_tags`
    # accumulates every valid tag seen along the way. Only if this ends
    # up non-empty does a second, targeted pass run afterward -- seeking
    # directly to just those few byte ranges and overwriting them in
    # place, not re-reading the whole file again.
    used_tags: set[str] = set()
    pending_tag_fixes: list[tuple[int, int, int, str]] = []

    # One pass over the input: each record is repaired, patched, and
    # written out immediately, then discarded -- memory use stays bounded
    # by the streaming buffer (see iter_repair_stream) plus one record's
    # worth of parsed data at a time, never by the total number of
    # records. Log lines are the exception: they're buffered in memory for
    # the whole run so they can be reordered before writing (not-fixable
    # first, fixed second, each clearly labeled -- see below), but even a
    # pathological run where every single record has something to log is
    # nowhere near the size of holding every record itself would be.
    n_total = 0
    n_unfixable = 0
    n_diverted_review = 0
    resolve_source_system(args)  # normally already done up front; a no-op then
    source_system = args.source_system
    error_path = _error_output_path(out_path)
    review_path = _needs_review_output_path(out_path)
    # Only accumulated until the one-time early estimate fires (see
    # ProgressReporter.maybe_print_estimate) -- cheap to compute (just
    # len() on text already in hand) but no reason to keep paying it for
    # the rest of a long run once it's no longer needed.
    bytes_consumed_for_estimate = 0
    log_entries: list[LogEntry] = []
    # (record_idx, identifier, declared_length) for every record that has a
    # usable identifier -- used after the full pass to find identifiers
    # reused across more than one record (see find_duplicate_identifiers).
    # Tiny compared to holding the records themselves: two short strings
    # per record, not the parsed record data.
    id_records: list[tuple[int, str, str]] = []
    progress = ProgressReporter(total_bytes=os.path.getsize(args.input))
    n_sampled = 0

    def log(category: str, fixed: bool, record_idx: int, rec_id: str, detail: str) -> None:
        log_entries.append(LogEntry(category, fixed, ts, record_idx, rec_id, detail))

    with open(out_path, "wb") as out_fh, \
            (
                open(mrk_path, "w", encoding="utf-8", errors="surrogateescape")
                if mrk_path else _null_writer()
            ) as mrk_fh, \
            _LazyBinaryWriter(error_path) as error_fh, \
            _LazyBinaryWriter(review_path) as review_fh, \
            (
                _LazyBinaryWriter(sample_problems_path)
                if sample_problems_path else _null_writer()
            ) as sample_fh:
        record_stream = iter_repair_stream(
            args.input,
            normalized_overrides,
            on_progress=progress.on_progress,
            fix_bad_indicators=args.fix_bad_indicators,
        )
        if args.reattach_orphaned_fields:
            record_stream = reattach_orphaned_trailing_fields(record_stream)
        for i, (parsed, rec_text) in enumerate(record_stream):
            n_total += 1
            # errors="surrogateescape" here and below: rec_text is the raw,
            # untouched original text, so it can still carry a
            # surrogate-escaped byte from a source-file corruption that
            # wasn't valid UTF-8 (see iter_repair_stream) -- round-tripping
            # it back to that same original byte, rather than raising, is
            # exactly what "passed through unchanged" already means here.
            if not progress.estimate_shown:
                bytes_consumed_for_estimate += len(
                    rec_text.encode(encoding_used, errors="surrogateescape")
                )
                progress.maybe_print_estimate(n_total, bytes_consumed_for_estimate)
            progress.maybe_print(n_total)
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            log_entries_before_record = len(log_entries)

            if parsed.unresolved:
                reason = parsed.unresolved[0][3]
                print(
                    f"record {i}: UNFIXABLE ({reason}) -- writing to {error_path} instead",
                    file=sys.stderr,
                )
                log("unfixable", False, i, "", f"written to {error_path} unchanged: {reason}")
                error_fh.write(rec_text.encode(encoding_used, errors="surrogateescape"))
                if mrk_fh:
                    mrk_fh.write(
                        f"=UNFIXABLE  (written to {os.path.basename(error_path)} instead)\n\n"
                    )
                n_unfixable += 1
            elif (id_reason := missing_identifier_reason(parsed, source_system)):
                # No usable identifier for this source system (907 $a for
                # Sierra, 001 otherwise): the record can't be loaded --
                # diverted to the _error file, unchanged, like any other
                # UNFIXABLE record.
                reason = f"record has no identifier: {id_reason}"
                print(
                    f"record {i}: UNFIXABLE ({reason}) -- writing to {error_path} instead",
                    file=sys.stderr,
                )
                log(
                    "record_identifier_required", False, i, "",
                    f"written to {error_path} unchanged: {reason}",
                )
                error_fh.write(rec_text.encode(encoding_used, errors="surrogateescape"))
                if mrk_fh:
                    mrk_fh.write(
                        f"=UNFIXABLE  (written to {os.path.basename(error_path)} instead)\n\n"
                    )
                n_unfixable += 1
            else:
                for tag, spaces_added, original_indicators, preview in parsed.indicator_fixes:
                    rec_id = record_identifier(parsed)
                    log(
                        "padded_indicators", True, i, rec_id,
                        f"padded {spaces_added} space(s) into short indicators on ={tag} "
                        f"(was {original_indicators!r}, first subfield {preview!r})",
                    )
                for tag, detail in parsed.reattached_orphaned_fields:
                    rec_id = record_identifier(parsed)
                    log("reattached_orphaned_field", True, i, rec_id, detail)
                for tag, before, after, stripped, preview in parsed.collapsed_oversized_fields:
                    rec_id = record_identifier(parsed)
                    log(
                        "collapsed_oversized_field_escapes", True, i, rec_id,
                        f"={tag} was {before} bytes, over ISO 2709's 9999-byte "
                        f"directory length limit -- collapsed to {after} bytes "
                        f"({stripped} spurious escape pair(s) removed; "
                        f"preview: {preview!r})",
                    )
                for tag, before, after, tail in parsed.truncated_oversized_fields:
                    rec_id = record_identifier(parsed)
                    log(
                        "truncated_oversized_field", True, i, rec_id,
                        f"={tag} was {before} bytes, over ISO 2709's 9999-byte "
                        f"directory length limit -- truncated to {after} bytes "
                        f"(dropped tail starts: {tail!r})",
                    )
                if args.fix_marc8_diacritic_escapes:
                    rec_id = record_identifier(parsed)
                    for detail in fix_marc8_diacritic_escapes(parsed):
                        log("fixed_marc8_diacritic", True, i, rec_id, detail)
                if b2_lexicon is not None:
                    rec_id = record_identifier(parsed)
                    for detail in fix_marc8_b2_umlaut(parsed, b2_lexicon):
                        log("fixed_marc8_b2_umlaut", True, i, rec_id, detail)
                for category, detail in find_suspect_marc8_escapes(
                    parsed, corpus_index=marc8_corpus_index,
                ):
                    rec_id = record_identifier(parsed)
                    log(category, False, i, rec_id, detail)
                for category, detail in find_suspect_hex_encoded_marc8(parsed):
                    rec_id = record_identifier(parsed)
                    log(category, False, i, rec_id, detail)
                if args.transcode_marc8:
                    try:
                        transcoded, removed_untranscodable = transcode_marc8_to_utf8(parsed)
                    except (UnicodeDecodeError, RuntimeError) as exc:
                        # transcode_marc8_to_utf8 leaves `parsed`
                        # untouched on failure (see its own docstring) --
                        # only a *control* field's content can still
                        # raise this far; a data field's own failure is
                        # isolated internally and surfaces via
                        # `removed_untranscodable` instead. Safe to just
                        # skip this fix and let every other one continue
                        # -- important now that this runs by default, so
                        # one genuinely malformed MARC-8 field can't
                        # crash the whole run over records that never
                        # asked for this.
                        rec_id = record_identifier(parsed)
                        log(
                            "transcode_marc8_failed", False, i, rec_id,
                            f"could not transcode MARC-8 -> UTF-8: {exc}",
                        )
                    else:
                        rec_id = record_identifier(parsed)
                        for detail in removed_untranscodable:
                            log("removed_untranscodable_subfield", True, i, rec_id, detail)
                        if transcoded:
                            log("transcoded_marc8", True, i, rec_id, "transcoded MARC-8 -> UTF-8")
                if args.fix_mojibake:
                    rec_id = record_identifier(parsed)
                    for detail in find_and_fix_mojibake(parsed):
                        log("fixed_mojibake", True, i, rec_id, detail)
                if args.fix_invalid_leader_bytes:
                    rec_id = record_identifier(parsed)
                    for detail in fix_invalid_leader_bytes(parsed):
                        log("leader_byte_defaulted", True, i, rec_id, detail)
                if args.remap_999_to_945:
                    rec_id = record_identifier(parsed)
                    for detail in remap_999_to_945(parsed):
                        log("remapped_999_to_945", True, i, rec_id, detail)
                if args.normalize_subfield_9:
                    rec_id = record_identifier(parsed)
                    for detail in normalize_subfield_9_to_0(parsed):
                        log("normalized_subfield_9_to_0", True, i, rec_id, detail)
                if args.normalize_smart_characters:
                    rec_id = record_identifier(parsed)
                    for detail in normalize_smart_characters(parsed):
                        log("normalized_smart_characters", True, i, rec_id, detail)
                if args.fix_misplaced_subfield_codes:
                    rec_id = record_identifier(parsed)
                    for detail in fix_misplaced_subfield_codes(parsed):
                        log("fixed_misplaced_subfield_code", True, i, rec_id, detail)
                rec_id = record_identifier(parsed)
                for detail in strip_invalid_subfield_codes(parsed):
                    log("removed_invalid_subfield", True, i, rec_id, detail)
                for detail in strip_null_identifiers(parsed):
                    log("removed_null_identifier", True, i, rec_id, detail)
                if args.recode_title_only_7xx:
                    rec_id = record_identifier(parsed)
                    for detail in recode_title_only_7xx_to_730(parsed):
                        log("recoded_title_only_7xx_to_730", True, i, rec_id, detail)
                if args.recode_6xx_b_to_a:
                    rec_id = record_identifier(parsed)
                    for detail in recode_6xx_b_to_a(parsed):
                        log("recoded_6xx_b_to_a", True, i, rec_id, detail)
                if args.strip_missing_required_a:
                    rec_id = record_identifier(parsed)
                    for detail in strip_missing_required_a(parsed, required_a_tags):
                        log("field_removed_because_missing_a", True, i, rec_id, detail)
                rec_id = record_identifier(parsed)
                for detail in strip_whitespace_only_010_a(parsed):
                    log("removed_010_whitespace_a", True, i, rec_id, detail)
                rec_id = record_identifier(parsed)
                for detail in dedupe_010_fields(parsed):
                    log("deduped_010_lccn", True, i, rec_id, detail)
                rec_id = record_identifier(parsed)
                for detail in strip_missing_required_a(parsed, _880_REQUIRED_A_TAGS):
                    log("removed_880_missing_a", True, i, rec_id, detail)
                if args.strip_empty_fields:
                    strip_empty_fields(parsed)
                if args.merge_duplicate_040:
                    rec_id = record_identifier(parsed)
                    for detail in merge_duplicate_040(parsed):
                        log("merged_duplicate_040", True, i, rec_id, detail)
                if args.move_duplicate_100_to_700:
                    rec_id = record_identifier(parsed)
                    for detail in move_duplicate_100_to_700(parsed):
                        log("moved_duplicate_to_added_entry", True, i, rec_id, detail)
                if args.move_duplicate_245_to_246:
                    rec_id = record_identifier(parsed)
                    for detail in move_duplicate_245_to_246(parsed):
                        log("moved_duplicate_to_added_entry", True, i, rec_id, detail)
                # after the clean 245 -> 246 move above: this takes what's left
                if args.recode_misfiled_duplicate_245:
                    rec_id = record_identifier(parsed)
                    for detail in recode_misfiled_duplicate_245(parsed):
                        log("moved_duplicate_to_added_entry", True, i, rec_id, detail)
                if args.move_duplicate_240_to_730:
                    rec_id = record_identifier(parsed)
                    for detail in move_duplicate_240_to_730(parsed):
                        log("moved_duplicate_to_added_entry", True, i, rec_id, detail)
                if args.strip_duplicate_non_repeatable_fields:
                    rec_id = record_identifier(parsed)
                    for detail in strip_duplicate_non_repeatable_fields(
                        parsed, non_repeatable_tags
                    ):
                        log("removed_non_repeatable_duplicate", True, i, rec_id, detail)
                for tag, indicators, subfields in ensure_specs:
                    if ensure_field(parsed, tag, indicators, subfields):
                        rec_id = record_identifier(parsed)
                        log("added_field", True, i, rec_id, f"added missing {tag} field")
                if add_default_245_effective:
                    rec_id = record_identifier(parsed)
                    for detail in add_default_245(parsed):
                        log("added_default_245", True, i, rec_id, detail)
                elif record_missing_245(parsed):
                    rec_id = record_identifier(parsed)
                    try:
                        assemble_marc(parsed)
                    except RepairError as exc:
                        # A genuinely unfixable record (e.g. a field over
                        # 9999 bytes -- see ks-oversized-bad in the
                        # kitchen-sink fixture, which has neither a 245
                        # nor a field ISO 2709 can represent) takes
                        # priority over missing-245 diversion -- both can
                        # coexist on the same record, and without this
                        # check the `continue` below would short-circuit
                        # before the record ever reached the real
                        # UNFIXABLE check further down, silently
                        # misclassifying "can't be safely included in a
                        # bulk import at all" as merely "needs review".
                        print(
                            f"record {i}: {exc} -- writing to {error_path} instead",
                            file=sys.stderr,
                        )
                        log(
                            "unfixable", False, i, rec_id,
                            f"written to {error_path} unchanged: {exc}",
                        )
                        error_fh.write(rec_text.encode(encoding_used, errors="surrogateescape"))
                        if mrk_fh:
                            mrk_fh.write(
                                f"=UNFIXABLE  (written to {os.path.basename(error_path)} "
                                "instead)\n\n"
                            )
                        n_unfixable += 1
                        if rec_id:
                            id_records.append((i, rec_id, rec_text[:5]))
                        continue
                    # --no-add-default-245 (or --guarantee-importable,
                    # which implies it): rather than leave this record
                    # with no 245 at all, pull it out of the main output
                    # entirely and divert it, byte-for-byte unchanged, to
                    # the review file instead (see `_divert_to_review`) --
                    # the `continue` below skips every remaining fix and
                    # the final assemble/write for this record, same as
                    # the UNFIXABLE branch above skips them by being in
                    # the other half of this if/else.
                    reason = (
                        "record has no 245 (title statement) and "
                        + (
                            "--guarantee-importable is set (implies --no-add-default-245)"
                            if args.guarantee_importable
                            else "--no-add-default-245 is set"
                        )
                    )
                    _divert_to_review(
                        log, review_fh, review_path, rec_text, encoding_used, i, rec_id, reason,
                    )
                    if mrk_fh:
                        mrk_fh.write(
                            f"=NOT_FIXED  (copied to {os.path.basename(review_path)} "
                            "for review)\n\n"
                        )
                    n_diverted_review += 1
                    continue
                rec_id = record_identifier(parsed)
                for detail in add_default_008(parsed):
                    log("added_default_008", True, i, rec_id, detail)
                if args.fix_008_length:
                    rec_id = record_identifier(parsed)
                    for detail in fix_008_length(parsed):
                        log("fixed_008_length", True, i, rec_id, detail)
                rec_id = record_identifier(parsed)
                for detail in fix_invalid_bibliographic_level(parsed):
                    log("invalid_bibliographic_level", True, i, rec_id, detail)
                extra_detect_only_findings = []
                if args.check_dangling_880_links:
                    extra_detect_only_findings += find_dangling_880_links(parsed)
                if args.check_isbn_issn_checksum:
                    extra_detect_only_findings += find_invalid_isbn_issn_checksums(parsed)
                for category, detail in (
                    find_invalid_indicator_values(parsed)
                    + extra_detect_only_findings
                ):
                    rec_id = record_identifier(parsed)
                    log(category, False, i, rec_id, detail)
                for detail in recode_856_missing_u(parsed):
                    rec_id = record_identifier(parsed)
                    log("bib_856_missing_u", True, i, rec_id, detail)
                for detail in find_856_missing_u(parsed):
                    rec_id = record_identifier(parsed)
                    log("bib_856_missing_u", False, i, rec_id, detail)
                if non_standard_tally is not None:
                    rec_id = record_identifier(parsed)
                    for detail in non_standard_tally.check(parsed, i, rec_id):
                        log(NON_STANDARD_FIELD_CATEGORY, False, i, rec_id, detail)
                for category, detail in find_suspicious_fields(parsed):
                    # unfixed_non_numeric_tag is suppressed here when
                    # --fix-invalid-tags is on (the default): it's
                    # deferred and fixed (or, in the rare case no 9XX
                    # slot is free, flagged for real) after the main
                    # pass -- see pending_tag_fixes below.
                    if category == "unfixed_non_numeric_tag" and args.fix_invalid_tags:
                        continue
                    rec_id = record_identifier(parsed)
                    log(category, False, i, rec_id, detail)
                if parsed.leader[20:24] != "4500":
                    rec_id = record_identifier(parsed)
                    log(
                        "leader_entry_map_fixed", True, i, rec_id,
                        f"leader bytes 20-23 were {parsed.leader[20:24]!r}, always "
                        "a fixed constant in MARC21; corrected to '4500'",
                    )
                if args.trim_oversized_record:
                    trim_detail = trim_oversized_record(parsed)
                    if trim_detail:
                        rec_id = record_identifier(parsed)
                        log("trimmed_oversized_record", True, i, rec_id, trim_detail)
                try:
                    assembled = assemble_marc(parsed)
                except RepairError as exc:
                    # e.g. a single field or the base address too large for
                    # ISO 2709's fixed-width fields to represent at all --
                    # can't write a valid leader/directory for it, so this
                    # is UNFIXABLE the same as a record with no consistent
                    # directory at all. (Total record length alone has a
                    # documented sentinel and doesn't hit this -- see
                    # assemble_marc.)
                    print(
                        f"record {i}: {exc} -- writing to {error_path} instead",
                        file=sys.stderr,
                    )
                    log("unfixable", False, i, "", f"written to {error_path} unchanged: {exc}")
                    error_fh.write(rec_text.encode(encoding_used, errors="surrogateescape"))
                    if mrk_fh:
                        mrk_fh.write(
                            f"=UNFIXABLE  (written to {os.path.basename(error_path)} instead)\n\n"
                        )
                    n_unfixable += 1
                    rec_id = record_identifier(parsed)
                    if rec_id:
                        id_records.append((i, rec_id, rec_text[:5]))
                else:
                    if len(assembled) > 99999 and not args.allow_oversized_sentinel:
                        # Default behavior: this record is spec-valid
                        # MARC21, but empirically makes FOLIO Data Import
                        # abort the whole job with no log at all -- same
                        # practical effect on the batch as a genuinely
                        # UNFIXABLE record, so it's diverted to the same
                        # _error file instead of leaving the main output
                        # with a record other importers might choke on.
                        # --allow-oversized-sentinel opts back into the
                        # old behavior for an import target confirmed to
                        # handle the sentinel correctly.
                        rec_id = record_identifier(parsed)
                        print(
                            f"record {i}: {len(assembled)} bytes, requires the MARC21 "
                            f"oversized-length sentinel -- writing to {error_path} "
                            "instead",
                            file=sys.stderr,
                        )
                        log(
                            "unfixable", False, i, rec_id,
                            f"record is {len(assembled)} bytes, requiring the leader's "
                            "MARC21 oversized-length sentinel (99999) -- known to make "
                            "FOLIO Data Import abort the whole job with no log -- "
                            f"written to {error_path} unchanged instead of the main "
                            "output",
                        )
                        error_fh.write(assembled)
                        if mrk_fh:
                            mrk_fh.write(
                                f"=UNFIXABLE  (written to {os.path.basename(error_path)} "
                                "instead)\n\n"
                            )
                        n_unfixable += 1
                        if rec_id:
                            id_records.append((i, rec_id, assembled[:5].decode("ascii")))
                    else:
                        if len(assembled) > 99999:
                            rec_id = record_identifier(parsed)
                            log(
                                "oversized_sentinel_fixed", True, i, rec_id,
                                f"record is {len(assembled)} bytes; leader declares the "
                                "MARC21 sentinel 99999 instead (real end is still found "
                                "from the record terminator, nothing lost) -- kept in the "
                                "main output only because --allow-oversized-sentinel is set",
                            )
                        offset = out_fh.tell()
                        out_fh.write(assembled)
                        if mrk_fh:
                            mrk_fh.write(to_mrk(parsed))
                            mrk_fh.write("\n")
                        rec_id = record_identifier(parsed)
                        if rec_id:
                            id_records.append((i, rec_id, assembled[:5].decode("ascii")))
                        if args.fix_invalid_tags:
                            has_invalid_tag = False
                            for f in parsed.fields:
                                if f.tag.isdigit():
                                    used_tags.add(f.tag)
                                else:
                                    has_invalid_tag = True
                            if has_invalid_tag:
                                rec_encoding = "utf-8" if parsed.leader[9:10] == "a" else "latin-1"
                                pending_tag_fixes.append((i, offset, len(assembled), rec_encoding))

            # --sample-problems: this record counts as a "problem" if
            # anything logged for it above was NOT auto-fixed -- the same
            # set of findings that'd show up under REQUIRES ATTENTION in
            # --log. Checked against log_entries_before_record rather than
            # a running per-record flag so nothing above needs to know
            # this feature exists. Misses the rare case where a tag gets
            # renamed instead of stripped (pending_tag_fixes, logged only
            # after this whole loop ends) or a duplicate identifier
            # (found only after a full pass) -- both acceptable gaps for
            # a best-effort sample, not a guaranteed-complete one.
            if (
                sample_problems_path
                and n_sampled < args.sample_limit
                and any(
                    not entry.fixed and entry.category != NON_STANDARD_FIELD_CATEGORY
                    for entry in log_entries[log_entries_before_record:]
                )
            ):
                sample_fh.write(rec_text.encode(encoding_used, errors="surrogateescape"))
                n_sampled += 1

    progress.finish()

    if pending_tag_fixes:
        # Now that the whole file's been read, we finally know every tag
        # actually in use -- pick the replacement and go fix just these
        # few records, by seeking directly to their known byte offsets
        # rather than re-reading the whole output file. If every 9XX
        # slot is somehow already taken, there's nowhere left to rename
        # to -- strip the invalid-tag field(s) out entirely instead
        # (see `strip_invalid_tags`), which needs a full second pass
        # over the file rather than an in-place patch, since removing a
        # field changes the record's byte length.
        replacement_tag = pick_unused_9xx_tag(used_tags)
        fix_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if replacement_tag is None:
            _rewrite_stripping_invalid_tags(out_path, pending_tag_fixes, log_entries, fix_ts)
        else:
            with open(out_path, "r+b") as fixup_fh:
                for record_idx, offset, length, rec_encoding in pending_tag_fixes:
                    fixup_fh.seek(offset)
                    raw = fixup_fh.read(length)
                    rec = read_intact_record(raw.decode(rec_encoding))
                    rec_id = record_identifier(rec)
                    for detail in fix_invalid_tags(rec, replacement_tag):
                        log_entries.append(
                            LogEntry("invalid_tag", True, fix_ts, record_idx, rec_id, detail)
                        )
                    fixed_bytes = assemble_marc(rec)
                    assert len(fixed_bytes) == length, (
                        "tag rename must not change a record's total byte length"
                    )
                    fixup_fh.seek(offset)
                    fixup_fh.write(fixed_bytes)

    dup_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    log_entries.extend(find_duplicate_identifiers(id_records, dup_ts))

    # Every category this run's flags actually allow to fire -- used so
    # `write_log` can print a header (with a real count, even zero) for
    # every check that ran, not just the ones that found something. A
    # category left out here because its flag is off must genuinely
    # never be logged this run (see each `log(...)` call site above).
    active_categories = {
        "unfixable", "record_identifier_required", "added_default_008", "doubled_proxy_url", "removed_invalid_subfield",
        "unfixed_non_numeric_tag", "invalid_indicator_value", "invalid_bibliographic_level",
        "leader_entry_map_fixed", "oversized_sentinel_fixed", "duplicate_identifier",
        "removed_null_identifier", "suspect_marc8_escape", "suspect_hex_encoded_marc8",
        "removed_010_whitespace_a", "removed_880_missing_a", "truncated_oversized_field", "collapsed_oversized_field_escapes",
        "trimmed_oversized_record", "bib_856_missing_u",
    }
    if args.fix_bad_indicators:
        active_categories.add("padded_indicators")
    if args.reattach_orphaned_fields:
        active_categories.add("reattached_orphaned_field")
    if args.transcode_marc8:
        active_categories |= {
            "transcoded_marc8", "transcode_marc8_failed", "removed_untranscodable_subfield",
        }
    if args.fix_mojibake:
        active_categories.add("fixed_mojibake")
    if args.fix_marc8_diacritic_escapes:
        active_categories.add("fixed_marc8_diacritic")
    if b2_lexicon_path:
        active_categories.add("fixed_marc8_b2_umlaut")
    if args.fix_invalid_leader_bytes:
        active_categories.add("leader_byte_defaulted")
    if args.remap_999_to_945:
        active_categories.add("remapped_999_to_945")
    if args.normalize_subfield_9:
        active_categories.add("normalized_subfield_9_to_0")
    if args.normalize_smart_characters:
        active_categories.add("normalized_smart_characters")
    if args.fix_misplaced_subfield_codes:
        active_categories.add("fixed_misplaced_subfield_code")
    if args.recode_title_only_7xx:
        active_categories.add("recoded_title_only_7xx_to_730")
    if args.recode_6xx_b_to_a:
        active_categories.add("recoded_6xx_b_to_a")
    if args.strip_missing_required_a:
        active_categories.add("field_removed_because_missing_a")
    if (args.move_duplicate_100_to_700 or args.move_duplicate_245_to_246
            or args.recode_misfiled_duplicate_245 or args.move_duplicate_240_to_730):
        active_categories.add("moved_duplicate_to_added_entry")
    if args.merge_duplicate_040:
        active_categories.add("merged_duplicate_040")
    if args.strip_duplicate_non_repeatable_fields:
        active_categories.add("removed_non_repeatable_duplicate")
    if ensure_specs:
        active_categories.add("added_field")
    if add_default_245_effective:
        active_categories.add("added_default_245")
    else:
        active_categories.add("diverted_for_review")
    if args.fix_008_length:
        active_categories.add("fixed_008_length")
    if args.check_dangling_880_links:
        active_categories.add("dangling_880_link")
    if args.check_isbn_issn_checksum:
        active_categories.add("invalid_isbn_issn_checksum")
    if args.fix_invalid_tags:
        active_categories.add("invalid_tag")
    if args.check_non_standard_fields:
        active_categories.add(NON_STANDARD_FIELD_CATEGORY)

    resolved_full_categories = _ALWAYS_FULL_CATEGORIES | set(args.log_full)

    # Not-fixable/not-fixed-in-this-run issues first (still need your
    # attention in the output), fixed ones last; within each, grouped
    # by category with a header and count, so e.g. all 375 missing-008
    # findings sit together instead of scattered by record order.
    log_path = (
        _timestamped_log_path(args.log, run_ts) if args.log
        else os.path.splitext(out_path)[0] + f"_log_{run_ts}.log"
    )
    _write_run_timing_header(
        log_path, run_started, datetime.now(timezone.utc),
        args.input, out_path,
        error_path if os.path.exists(error_path) else None,
        source_count=n_total, repaired_count=n_total - n_unfixable - n_diverted_review,
        problem_count=n_unfixable,
        review_path=review_path if os.path.exists(review_path) else None,
        review_count=n_diverted_review,
    )
    write_log(
        log_path, log_entries, active_categories=active_categories,
        full_categories=resolved_full_categories,
        non_standard_tally=non_standard_tally,
        include_zero=args.log_zero,
    )
    if non_standard_tally is not None:
        supplement_path = os.path.splitext(log_path)[0] + "_non_standard_fields.txt"
        if write_non_standard_supplement(supplement_path, non_standard_tally):
            print(
                f"all non-standard field occurrences written to {supplement_path}",
                file=sys.stderr,
            )
        non_standard_tally.close()
    n_log_lines = len(log_entries)
    # Based on which section an entry actually lands in, not the raw
    # `fixed` flag -- INFORMATIONAL can now include detect-only findings
    # (e.g. invalid_indicator_value) logged with fixed=False for correct
    # section placement, which would otherwise inflate this "not fixed"
    # count. UNFIXABLE counts as not fixed too -- it's the same "still
    # needs a human" idea, just urgent enough to also be diverted out of
    # the main output.
    n_not_fixed = sum(
        1 for e in log_entries if _section_for(e)[1] in ("NOT FIXED", "UNFIXABLE")
    )
    n_info_only = sum(1 for e in log_entries if e.category == NON_STANDARD_FIELD_CATEGORY)
    n_fixed = n_log_lines - n_not_fixed - n_info_only
    print(
        f"log (findings only; checks with none are listed at the end; --log-zero for all headers) written to {log_path} "
        f"({n_log_lines} record-level finding(s): {n_not_fixed} not fixed, "
        f"{n_fixed} fixed"
        + (f", {n_info_only} information only" if n_info_only else "")
        + ")",
        file=sys.stderr,
    )

    elapsed = time.perf_counter() - start_time
    n_clean = n_total - n_unfixable - n_diverted_review
    print(f"Wrote {n_clean}/{n_total} record(s) to {out_path} in {elapsed:.2f}s")
    if sample_problems_path:
        print(f"{n_sampled} problem record(s) sampled to {sample_problems_path}")
    if n_diverted_review:
        print(
            f"{n_diverted_review} record(s) diverted, byte-for-byte unchanged, to "
            f"{review_path} instead of {out_path} for manual review (see --log).",
            file=sys.stderr,
        )
    if n_unfixable:
        print(
            f"{n_unfixable} record(s) could not be auto-repaired at all -- written, "
            f"byte-for-byte unchanged, to {error_path} instead of {out_path} (see "
            "stderr above for why each one); supply overrides via --overrides and "
            "re-run to fix them too.",
            file=sys.stderr,
        )
    if n_unfixable or n_diverted_review:
        return 1
    return 0
