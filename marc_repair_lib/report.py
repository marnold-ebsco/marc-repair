"""Reporting for marc_repair: the log file (category tables, descriptions, run timing,
`write_log`) and the progress reporter. Imports the modules before it."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import textwrap
import time
from datetime import datetime, timezone

from .common import (
    LogEntry,
)
from .patching import (
    DEFAULT_852_LOCATION_CONTENT,
    NON_STANDARD_FIELD_CATEGORY,
    NonStandardFieldTally,
    write_non_standard_toc,
)


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
    "removed_880_missing_6",
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
    "removed_880_missing_6": "An 880 (Alternate Graphic Representation) field with no $6 (its only "
    "required subfield: the link to the field it represents) -- the whole "
    "field removed. An 880 without a $a is kept.",
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
    "removed_880_missing_6": "DATA LOSS",
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
    "bib_856_missing_u": "POSSIBLE DATA LOSS",
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
    "removed_880_missing_6",
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


#: Written by install.sh next to marc_repair.py: `sha=`, `date=` (the commit's
#: committer date) and `ref=` lines. `.marc_repair_install_version` is the older,
#: SHA-only marker the installer has always written.
INSTALL_INFO_FILE = ".marc_repair_install_info"
INSTALL_VERSION_FILE = ".marc_repair_install_version"


def _utc_iso(stamp: str) -> str:
    """A git/GitHub ISO-8601 timestamp (any UTC offset) as `YYYY-MM-DDTHH:MM:SSZ`;
    returned unchanged when it doesn't parse."""
    try:
        return datetime.fromisoformat(stamp.strip()).astimezone(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    except ValueError:
        return stamp.strip()


def _git(base_dir: str, *args: str) -> str | None:
    try:
        done = subprocess.run(
            ["git", "-C", base_dir, *args], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def script_version(base_dir: str | None = None) -> str:
    """One line saying which commit of the script this run used, so a log shows
    whether the run was made with an up-to-date copy: `<sha> (committed <UTC
    time>, ref <ref>)`.

    A git checkout is asked directly (and flagged `+ uncommitted changes` when
    tracked files differ from that commit). An install.sh install has no `.git`,
    so it reads the info file the installer wrote, or failing that the older
    SHA-only marker (no date). `base_dir` is the folder holding marc_repair.py."""
    if base_dir is None:
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if os.path.exists(os.path.join(base_dir, ".git")):
        head = _git(base_dir, "log", "-1", "--format=%H%n%cI")
        if head and "\n" in head:
            sha, stamp = head.split("\n", 1)
            ref = _git(base_dir, "rev-parse", "--abbrev-ref", "HEAD") or "?"
            dirty = " + uncommitted changes" if _git(
                base_dir, "status", "--porcelain", "--untracked-files=no"
            ) else ""
            return f"{sha[:12]} (committed {_utc_iso(stamp)}, ref {ref}){dirty}"
    info: dict[str, str] = {}
    try:
        with open(os.path.join(base_dir, INSTALL_INFO_FILE), encoding="utf-8") as fh:
            for line in fh:
                key, sep, value = line.partition("=")
                if sep:
                    info[key.strip()] = value.strip()
    except OSError:
        pass
    if info.get("sha"):
        when = f"committed {_utc_iso(info['date'])}" if info.get("date") else "commit date unknown"
        ref = f", ref {info['ref']}" if info.get("ref") else ""
        return f"{info['sha'][:12]} ({when}{ref})"
    try:
        with open(os.path.join(base_dir, INSTALL_VERSION_FILE), encoding="utf-8") as fh:
            sha = fh.read().strip()
    except OSError:
        sha = ""
    if sha:
        return f"{sha[:12]} (commit date unknown: re-run install.sh to record it)"
    return "unknown (not a git checkout and no install.sh record found)"


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
    extra_lines: list[str] | None = None,
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
        fh.write(f"Script version: {script_version()}\n")
        for line in extra_lines or ():
            fh.write(line + "\n")
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
                    # DATA LOSS entries first, NO DATA LOSS last (stable)
                    for e in sorted(group, key=_data_loss_rank):
                        fh.write(e.render() + "\n")
        if skipped:
            if ordered:
                fh.write("\n")
            fh.write(f"=== CHECKS PERFORMED WITH NO FINDINGS ({len(skipped)}) ===\n")
            for label, category in skipped:
                description = _CHECK_DESCRIPTIONS.get(category, "(no description available)")
                fh.write(f"{label}: {category} -- {_first_sentence(description)}\n")


def _data_loss_rank(entry) -> int:
    """Sort key: 0 = DATA LOSS, 1 = unmarked/POSSIBLE DATA LOSS,
    2 = NO DATA LOSS."""
    if "NO DATA LOSS" in entry.detail:
        return 2
    if "POSSIBLE DATA LOSS" in entry.detail:
        return 1
    return 0 if "DATA LOSS" in entry.detail else 1


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
