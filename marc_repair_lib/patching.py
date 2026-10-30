"""Content patching for marc_repair: add or fix required fields, dedupe, recode,
and the non-standard-field tally. Imports the modules before it."""

from __future__ import annotations

import os
import re
import tempfile
from array import array
from dataclasses import replace
from difflib import SequenceMatcher
from typing import Iterator

from .common import (
    CONTROL_TAG_LIMIT,
    FIELDTERM,
    Field_,
    LogEntry,
    ParsedRecord,
    SUBFIELD,
)
from .qa import (
    VALID_SUBFIELD_CODE_CHARS,
    record_identifier,
)
from .core import (
    assemble_marc,
    read_intact_record,
)
from .marc8 import (
    UNICODE_ENCODING_BYTE,
)


# ---------------------------------------------------------------------------
# Content patching: add a required field where it's missing
# ---------------------------------------------------------------------------

def ensure_field(
    parsed: ParsedRecord,
    tag: str,
    indicators: str | None,
    subfields_or_content: list[tuple[str, str]] | str,
) -> bool:
    """Insert a new field into `parsed` if `tag` isn't already present,
    keeping fields in the conventional ascending-tag order. Returns True if a
    field was added, False if `tag` was already there (left untouched).

    For a control field (tag < "010", e.g. 008 -- no indicators/subfields),
    pass `indicators=None` and `subfields_or_content` as the raw content
    string. For a data field, pass a 2-char `indicators` string and
    `subfields_or_content` as a list of (code, data) tuples.
    """
    if any(f.tag == tag for f in parsed.fields):
        return False
    insert_at = len(parsed.fields)
    for i, f in enumerate(parsed.fields):
        # a non-numeric tag (not yet fixed, e.g. --no-fix-invalid-tags was
        # passed) has no defined position in tag order -- treat it as
        # sorting after every real tag rather than crashing on int()
        existing = int(f.tag) if f.tag.isdigit() else 1000
        if existing > int(tag):
            insert_at = i
            break
    if indicators is None:
        new_field = Field_(tag, None, None, content=subfields_or_content)
    else:
        new_field = Field_(tag, indicators, list(subfields_or_content))
    parsed.fields.insert(insert_at, new_field)
    return True


#: Placeholder 008 content for records missing one entirely, matching a
#: widely-used site cleanup script's own blanket default byte-for-byte.
#: This is a real trade-off, not a correction: 008 is material-type- and
#: date-specific, so this placeholder (single known date 1978-06-15,
#: unknown publication date/place, language eng, "d"/serial-ish trailing
#: byte) will be wrong for most records it's applied to -- it exists so a
#: record has *a* syntactically valid 008 (many downstream systems choke
#: on a record with none at all) rather than none whatsoever, not because
#: it's the correct value. See `add_default_008`.
DEFAULT_008_CONTENT = "780615s19uu    xx a                    d"


def add_default_008(parsed: ParsedRecord) -> list[str]:
    """Insert `DEFAULT_008_CONTENT` for a record with no 008 field at all.
    No-op if 008 is already present (e.g. supplied correctly via
    --ensure-field, which runs before this). Logged (category
    "added_default_008") since the inserted value is a placeholder, not a
    real one. Unconditional -- every MARC21 record must have an 008, so
    unlike most of this tool's other placeholder insertions there's no
    --no- flag to leave one out; supply a real value per-record instead
    via --ensure-field, which always takes priority over this default."""
    if ensure_field(parsed, "008", None, DEFAULT_008_CONTENT):
        return [f"added default 008 (was missing): {DEFAULT_008_CONTENT!r}"]
    return []


#: MARC21's Holdings format 008 is a different, shorter fixed field
#: than the bibliographic/authority 008 (32 bytes, not 40 --
#: https://www.loc.gov/marc/holdings/hd008.html; confirmed against real
#: data, where every 008 in a real Bucknell holdings export is exactly
#: 32 bytes). See `fix_008_length`'s `expected_len` for the structural
#: (pad/truncate) side of this; this constant is only the content used
#: when a holdings record has no 008 at all.
HOLDINGS_008_LENGTH = 32


#: Placeholder 008 content for a holdings record missing one entirely.
#: Unlike DEFAULT_008_CONTENT (bibliographic 008, matching a widely-used
#: site cleanup script's own blanket default byte-for-byte), no
#: equivalent holdings-specific default is available -- the 008's
#: content (receipt/acquisition status, expected frequency, completeness,
#: retention policy, etc.) is genuinely institution- and
#: collection-specific, so inventing plausible-looking values for
#: positions this tool can't actually determine would be exactly the
#: kind of silent guess this tool avoids everywhere else. This is a
#: fully blank placeholder instead -- syntactically valid (right length,
#: which is what a strict importer like FOLIO actually enforces) but
#: informationally empty -- logged the same way as the bibliographic
#: placeholder (see `add_default_holdings_008`) so every occurrence is
#: easy to find and review.
DEFAULT_HOLDINGS_008_CONTENT = " " * HOLDINGS_008_LENGTH


#: Placeholder content for an 852 (Location) with no usable location in
#: $a, $b, or $c at all -- see `fix_missing_852_location`. Unlike
#: `add_missing_852c` below (which only ever adds $c, and can be
#: disabled via --no-fix-missing-852c), this always runs, no opt-out
#: flag, and is logged under FIXED/REQUIRES ATTENTION rather than
#: INFORMATIONAL -- a record with no usable location anywhere is a
#: bigger problem than one merely missing a shelving detail.
DEFAULT_852_LOCATION_CONTENT = "Migration"


#: Placeholder content for a missing 852 (Location) $c (shelving
#: location) -- see `add_missing_852c`. Like the 008/245 placeholders
#: above, this deliberately flags itself as inserted rather than
#: guessing a real shelving location, which this tool has no way to
#: know.
DEFAULT_852_C_CONTENT = "Migration"


#: OCLC control-number prefixes ("on"/"ocn"/"ocm" + digits, e.g.
#: "ocm12345678") -- a holdings record's 004 (the linked bib record's
#: control number) starting with one of these means the record came
#: from OCLC WMS, the one common convention where 852's location lives
#: in $c instead of $b (see `_is_wms_holdings_record`,
#: `fix_missing_852_location`).
_WMS_004_PREFIXES = ("on", "ocn", "ocm")


def _is_wms_holdings_record(parsed: ParsedRecord) -> bool:
    """True if this holdings record's 004 starts with a WMS/OCLC
    control-number prefix (see `_WMS_004_PREFIXES`)."""
    return any(
        f.tag == "004" and f.is_control() and f.content
        and f.content.lower().startswith(_WMS_004_PREFIXES)
        for f in parsed.fields
    )


def fix_missing_852_location(parsed: ParsedRecord) -> list[str]:
    """Ensure every 852 (Location) field has a usable location in $a,
    $b (sublocation), or $c (shelving location) -- which one actually
    carries the location code is source-system-dependent, not
    standardized: Alma and Sierra/Millennium primarily use $b (library/
    location code) with $c as secondary context; Koha, FOLIO, and
    Symphony/Horizon primarily use $c (shelving location/collection
    code) with $b as secondary context; OCLC WMS uses $c only, never
    $b. Real production data confirms $a itself is essentially unused:
    one entire 71.6MB export had $b carrying the location on nearly
    every record instead.

    Only when NONE of $a/$b/$c has usable (non-empty, non-punctuation-
    only -- see `_is_punctuation_only`, the same "no real content" rule
    `strip_missing_required_a` uses for bib heading fields' $a) content
    does this insert a placeholder -- never into $a, since none of the
    systems above treat it as the live location subfield. The target is
    $c for a record `_is_wms_holdings_record` identifies as WMS-sourced
    (matching that system's own convention exactly), $b otherwise (the
    subfield every *other* listed system above treats as location-
    bearing, primary or secondary) -- so the placeholder always matches
    the convention the rest of a given file already uses rather than
    introducing a subfield code that convention never touches.
    Always runs (no opt-out flag, unlike --no-fix-missing-852c): a location
    this tool can't find in any of $a/$b/$c is missing, not just
    missing a shelving detail. Logged (category
    "added_missing_852_location") every time it runs, since it's adding
    content -- not just correcting structure -- and a placeholder
    rather than a value recovered from the record's own data. A
    punctuation-only target subfield is replaced in place (keeping its
    position); a missing one is prepended. No-op for an 852 that
    already has a usable $a, $b, or $c.
    """
    target_code = "c" if _is_wms_holdings_record(parsed) else "b"
    details = []
    for f in parsed.fields:
        if f.tag != "852" or f.is_control():
            continue
        if any(
            code in ("a", "b", "c") and data and not _is_punctuation_only(data)
            for code, data in f.subfields
        ):
            continue
        if any(code == target_code for code, _ in f.subfields):
            f.subfields = [
                (code, DEFAULT_852_LOCATION_CONTENT) if code == target_code else (code, data)
                for code, data in f.subfields
            ]
            details.append(
                f"replaced empty/punctuation-only 852 ${target_code} with: "
                f"{DEFAULT_852_LOCATION_CONTENT!r}"
            )
        else:
            f.subfields = [(target_code, DEFAULT_852_LOCATION_CONTENT)] + list(f.subfields)
            details.append(
                f"added missing 852 ${target_code}: {DEFAULT_852_LOCATION_CONTENT!r}"
            )
    return details


def fix_852_call_number(
    parsed: ParsedRecord, remove_bad: bool = True,
) -> tuple[list[str], list[str]]:
    """Handle 852 (Location)'s $h (Classification part -- the call
    number) two different ways depending on what's actually wrong,
    returning (removed_details, missing_details) for the caller to log
    under two different categories:

      * $h present but unusable (empty or nothing but punctuation --
        see `_is_punctuation_only`) is a genuinely bad subfield, so it
        alone is removed -- same "remove just the bad part, leave the
        rest of the field alone" treatment `strip_invalid_subfield_codes`
        gives an invalid subfield code -- and returned in
        `removed_details` (category "removed_bad_call_number", FIXED/
        REQUIRES ATTENTION: real data was discarded).
      * $h missing entirely isn't something to fix at all -- there's no
        bad subfield to remove, just nothing there -- so the field is
        left completely untouched (not even its other subfields), and
        returned in `missing_details` instead (category
        "missing_call_number", INFORMATIONAL; header+count only --
        INFORMATIONAL categories are never listed in full).

    No-op (in both lists) for an 852 that already has a usable $h.
    """
    removed_details = []
    missing_details = []
    for f in parsed.fields:
        if f.tag != "852" or f.is_control():
            continue
        h_subfields = [(code, data) for code, data in f.subfields if code == "h"]
        if any(data and not _is_punctuation_only(data) for _, data in h_subfields):
            continue
        if not h_subfields:
            body = "".join(f"${code}{data}" for code, data in f.subfields)
            missing_details.append(f"={f.tag}  {f.indicators}{body} has no $h subfield")
            continue
        if not remove_bad:
            continue
        f.subfields = [(code, data) for code, data in f.subfields if code != "h"]
        removed = "".join(f"${code}{data}" for code, data in h_subfields)
        preview = h_subfields[0][1][:10]
        removed_details.append(
            f"={f.tag} $h{preview} -- removed unusable $h subfield(s) "
            f"{removed!r} (empty or punctuation-only; rest of the field "
            "left as-is)"
        )
    return removed_details, missing_details


#: Matches a $b (Sublocation) subfield's content that looks like an LC
#: cutter number/call-number fragment rather than a real location code
#: -- either the standard cutter-number shape (optional leading period,
#: one uppercase letter, then a digit -- e.g. ".K487", "A96 1983") or a
#: bare classification-style token with no spaces (e.g. "KGL104"). Real
#: location codes in production data are textual department/collection
#: abbreviations (e.g. "OFC Main", "B.REF", "LAW REF") that never take
#: this shape -- see `_is_852_b_non_location`.
_852_CUTTER_OR_CLASS_PATTERN = re.compile(r"^\.?[A-Z]\d|^[A-Z]{1,4}\d+(\.\d+)?$")


def _is_852_b_non_location(data: str) -> bool:
    """True if a $b (Sublocation) subfield's own content looks like
    something OTHER than a location code: empty/punctuation-only (see
    `_is_punctuation_only`), or matching `_852_CUTTER_OR_CLASS_PATTERN`.
    Used only to pick which of an 852's multiple $b's to discard when
    neither one's position (before/after $h) already settles it -- see
    `fix_852_multiple_b`.
    """
    return not data or _is_punctuation_only(data) or bool(
        _852_CUTTER_OR_CLASS_PATTERN.match(data)
    )


def fix_852_multiple_b(
    parsed: ParsedRecord, recode: bool = True, remove: bool = True,
) -> tuple[list[str], list[str]]:
    """Fix an 852 (Location) field with more than one $b (Sublocation)
    subfield -- $b isn't meant to repeat, so this is always some kind
    of miscoding. Four cases, tried in order:

      1. All of the $b's have the exact same content (e.g.
         `$bOFC Main$bOFC Main...`) -- a plain duplicate, not two
         competing candidates, so this is the one case where there's no
         real ambiguity at all: every extra copy is removed, keeping
         just the first.
      2. Exactly two *distinct* $b's, $h is present, one $b comes
         before $h (the real sublocation) and the other comes after
         it, and there's no $i (Item part) already -- the second $b is
         actually the cutter number/date/copy note that goes with $h's
         classification, just miscoded, e.g.
         `$bOFC Main$hZ678.9 A2$bA96 1983` -- recoded to $i instead of
         removed, since it's real, recoverable data (a real, common
         defect: 63 of 114 852s with multiple $b in one 79,853-record
         export had exactly this shape; none of them were also
         case 1's plain-duplicate shape, so recoding never risks
         turning a duplicated location into a fake item part).
      3. Otherwise, if exactly one of the remaining distinct $b's looks
         like something other than a location code (see
         `_is_852_b_non_location`) -- e.g. the trailing `.K487` in
         `$bOFC PER$hMicrofilm:$i1981-1982$zQA76.5$b.K487` -- that one
         specifically is removed, keeping the real location.
      4. Otherwise -- every $b looks equally location-like (e.g.
         `$bLAW$bLAW REF...`) or none does -- there's no principled way
         to pick a winner, so the LAST $b is removed as an arbitrary
         fallback; no worse than leaving an 852 with two, and flagged
         for a human either way.

    Returns (recoded_details, removed_details) for the caller to log
    under two different categories: case 2 (the only one that recodes
    rather than discards) goes in `recoded_details` (category
    "recoded_852_b_to_i"); cases 1, 3, and 4 (all removals, just for
    different reasons) go in `removed_details` (category
    "removed_extra_852_b") -- both FIXED/REQUIRES ATTENTION: real data
    was reinterpreted or discarded either way, worth a second look. The
    detail string itself says which of the four actually happened
    (case 1 is worded distinctly from case 4: "duplicate" rather than
    "no way to tell", since case 1 is actually certain, not a guess).
    No-op (in both lists) for an 852 with at most one $b.
    """
    recoded_details = []
    removed_details = []
    for f in parsed.fields:
        if f.tag != "852" or f.is_control():
            continue
        codes = [code for code, _ in f.subfields]
        if codes.count("b") <= 1:
            continue
        body = "".join(f"${code}{data}" for code, data in f.subfields)
        b_indices = [idx for idx, (code, _) in enumerate(f.subfields) if code == "b"]
        b_values = {f.subfields[idx][1] for idx in b_indices}
        if len(b_values) == 1:
            if not remove:
                continue
            dup_value = next(iter(b_values))
            keep_first = b_indices[0]
            f.subfields = [
                sf for idx, sf in enumerate(f.subfields)
                if idx == keep_first or idx not in b_indices
            ]
            removed_details.append(
                f"removed duplicate 852 $b {dup_value!r} from "
                f"={f.tag}  {f.indicators}{body} "
                "(identical to another $b already in this field)"
            )
            continue
        h_idx = codes.index("h") if "h" in codes else -1
        b_before_h = h_idx >= 0 and codes[:h_idx].count("b")
        b_after_h = h_idx >= 0 and codes[h_idx + 1:].count("b")
        if (
            codes.count("b") == 2 and "i" not in codes
            and b_before_h == 1 and b_after_h == 1
        ):
            if not recode:
                continue
            seen_h = False
            renamed_value = None
            new_subfields = []
            for code, data in f.subfields:
                if code == "h":
                    seen_h = True
                    new_subfields.append((code, data))
                elif code == "b" and seen_h:
                    renamed_value = data
                    new_subfields.append(("i", data))
                else:
                    new_subfields.append((code, data))
            f.subfields = new_subfields
            recoded_details.append(
                f"recoded 852 $b {renamed_value!r} (after $h, no existing $i) as $i"
            )
            continue
        if not remove:
            continue
        non_location = [idx for idx in b_indices if _is_852_b_non_location(f.subfields[idx][1])]
        if len(non_location) == 1:
            drop_idx = non_location[0]
            reason = "doesn't look like a location code"
        else:
            drop_idx = b_indices[-1]
            reason = "no way to tell which $b is the real location; removed the last one"
        dropped_value = f.subfields[drop_idx][1]
        f.subfields = [sf for idx, sf in enumerate(f.subfields) if idx != drop_idx]
        removed_details.append(
            f"removed extra 852 $b {dropped_value!r} from ={f.tag}  {f.indicators}{body} "
            f"({reason})"
        )
    return recoded_details, removed_details


def split_holdings_multiple_852(parsed: ParsedRecord) -> tuple[list[ParsedRecord], list[str]]:
    """Split a holdings record with more than one 852 (Location) field
    into one record per 852 -- FOLIO (like every other holdings-
    consuming ILS) expects a single location per holdings record, so
    a source record that packs several 852s together (flagged NOT
    FIXED as "holdings_multiple_852" -- see `repair_holdings_records`)
    isn't representable as one record at all; this is what actually
    resolves it, by producing several.

    An 852 with no $b (Sublocation) at all has no location to split
    out in the first place -- rather than duplicating it into a copy
    of its own, it's dropped entirely and returned in the second list
    (category "incomplete_852", NOT FIXED: there's no data here to
    fix, just a location this tool can't use).

    Every other field is duplicated into every copy verbatim, and
    each copy keeps exactly one of the record's remaining ($b-bearing)
    852s, in its original relative position. This deliberately does
    NOT try to figure out which 853-868/856/etc. field belongs with
    which 852 -- some source systems link that via 852 $8, others
    don't consistently -- so those fields end up duplicated across
    every copy along with everything else; sorting that out is a
    separate, harder problem.

    The first copy keeps field 001's content exactly as it was on the
    source record; every copy after that gets "-2", "-3", etc.
    appended to it (see `_suffix_holdings_identifier`) -- otherwise
    every copy would share the exact same identifier, which is itself
    a real defect this tool flags elsewhere (`find_duplicate_identifiers`).

    Returns `([parsed], [])` unchanged (as a one-element list, so a
    caller can always iterate the first return value the same way
    whether or not a split happened) for a record with at most one
    852 to begin with -- nothing to split, and the $b check above
    doesn't apply either: a record with only one 852 isn't ambiguous
    about which location is "the" location, regardless of which
    subfield ($a/$b/$c -- see `fix_missing_852_location`) actually
    carries it, so it's left alone here for that fix to handle
    generically.
    """
    indices_852 = [i for i, f in enumerate(parsed.fields) if f.tag == "852"]
    if len(indices_852) <= 1:
        return [parsed], []
    incomplete_details = []
    valid_indices = []
    for i in indices_852:
        f = parsed.fields[i]
        if any(code == "b" for code, _ in f.subfields):
            valid_indices.append(i)
            continue
        body = "".join(f"${code}{data}" for code, data in f.subfields)
        incomplete_details.append(
            f"removed 852 with no $b (Sublocation): ={f.tag}  {f.indicators}{body}"
        )
    other_fields = [(i, f) for i, f in enumerate(parsed.fields) if f.tag != "852"]
    if len(valid_indices) <= 1:
        fields = [f for _, f in other_fields]
        if valid_indices:
            keep_idx = valid_indices[0]
            insert_at = sum(1 for i, _ in other_fields if i < keep_idx)
            fields.insert(insert_at, parsed.fields[keep_idx])
        record = ParsedRecord(leader=parsed.leader, entries=list(parsed.entries), fields=fields)
        return [record], incomplete_details
    copies = []
    for copy_num, keep_idx in enumerate(valid_indices, start=1):
        insert_at = sum(1 for i, _ in other_fields if i < keep_idx)
        fields = [f for _, f in other_fields]
        fields.insert(insert_at, parsed.fields[keep_idx])
        if copy_num > 1:
            fields = _suffix_holdings_identifier(fields, f"-{copy_num}")
        copies.append(
            ParsedRecord(leader=parsed.leader, entries=list(parsed.entries), fields=fields)
        )
    return copies, incomplete_details


def _suffix_holdings_identifier(fields: list[Field_], suffix: str) -> list[Field_]:
    """Return `fields` with field 001's content suffixed -- used by
    `split_holdings_multiple_852` and `split_holdings_multiple_004` to
    give each additional holdings record its own distinct identifier
    rather than sharing the source record's 001 verbatim with every
    other copy. Builds a new Field_ (via `dataclasses.replace`) rather
    than mutating in place, since the 001 Field_ instance being
    replaced here is the SAME object shared across every copy's own
    `fields` list -- mutating it would change every other copy's 001
    too. No-op if there's no 001 field at all."""
    return [
        replace(f, content=(f.content or "") + suffix) if f.tag == "001" else f
        for f in fields
    ]


def split_holdings_multiple_004(parsed: ParsedRecord) -> tuple[ParsedRecord, list[ParsedRecord]]:
    """Resolve a holdings record with more than one 004 (Control
    Number -- the linked bib record's own control number) the same
    structural way `split_holdings_multiple_852` resolves multiple
    852s: a holdings record is meant to link to exactly one bib
    record, so one that packs several 004s together isn't
    representable as a single, unambiguous record either.

    Returns `(main_record, additional_records)`. `main_record` is
    `parsed` with every 004 past the first dropped -- i.e. it keeps
    the original record's own identity and every other field
    untouched, just with its linkage narrowed to the first 004.
    `additional_records` has one entry per remaining 004: a full copy
    of `main_record` (every field, including its kept first 004,
    duplicated verbatim) with just that one 004's content swapped in
    for the copy's own 004 -- so each additional record differs from
    `main_record` only in which bib record it links to. Each gets
    "-2", "-3", etc. appended to field 001 (see
    `_suffix_holdings_identifier`), same reasoning as
    `split_holdings_multiple_852`: otherwise every copy would share
    `main_record`'s own identifier.

    This is deliberately NOT a "fix" for a defect -- a holdings record
    legitimately linking to more than one bib record isn't wrong (see
    "holdings_multiple_004"'s own INFORMATIONAL/NO DATA LOSS
    classification) -- just reshaped into the one-004-per-record form
    every downstream consumer expects. The caller
    (`repair_holdings_records`) is responsible for routing
    `additional_records` to a separate "_additional004s" file rather
    than the main output (see `_additional_004_output_path`) --
    unlike `split_holdings_multiple_852`'s own extra copies, which
    belong in the same output file, these shouldn't silently inflate
    the main output's record count for the (overwhelmingly common)
    case where this never happens.

    Returns `(parsed, [])` unchanged for a record with at most one
    004 -- nothing to split."""
    indices_004 = [i for i, f in enumerate(parsed.fields) if f.tag == "004"]
    if len(indices_004) <= 1:
        return parsed, []
    extra_contents = [parsed.fields[i].content for i in indices_004[1:]]
    seen_first = False
    main_fields = []
    for f in parsed.fields:
        if f.tag == "004":
            if seen_first:
                continue
            seen_first = True
        main_fields.append(f)
    main_record = ParsedRecord(
        leader=parsed.leader, entries=list(parsed.entries), fields=main_fields,
    )
    additional_records = []
    for copy_num, content in enumerate(extra_contents, start=2):
        dup_fields = [
            replace(f, content=content) if f.tag == "004" else f for f in main_fields
        ]
        dup_fields = _suffix_holdings_identifier(dup_fields, f"-{copy_num}")
        additional_records.append(
            ParsedRecord(leader=parsed.leader, entries=list(parsed.entries), fields=dup_fields)
        )
    return main_record, additional_records


def strip_empty_852_subfields(parsed: ParsedRecord) -> list[str]:
    """Remove a subfield within an 852 (Location) field that has no
    data at all -- e.g. a bare `$2` with nothing after it. Excludes
    $h (Classification part): that's already handled specifically by
    `fix_852_call_number`, which distinguishes "present but empty"
    (removed, its own more specific "removed_bad_call_number" message)
    from "missing entirely" (left alone, just flagged
    "missing_call_number") -- this generic pass touching $h too would
    blur that distinction for no benefit, since `fix_852_call_number`
    already leaves no empty $h behind it. Meant to run after
    `fix_852_multiple_b`/`fix_852_call_number`/`fix_missing_852_location`
    (see `repair_holdings_records`) so those more specific fixes see
    each field's original shape first; this only mops up whatever
    they left behind. Returns one detail string per affected field
    (category "removed_empty_852_subfield" -- FIXED/REQUIRES
    ATTENTION, same as the rest of this file's 852 content fixes).
    """
    details = []
    for f in parsed.fields:
        if f.tag != "852" or f.is_control():
            continue
        removed_codes = [code for code, data in f.subfields if not data and code != "h"]
        if not removed_codes:
            continue
        body = "".join(f"${code}{data}" for code, data in f.subfields)
        f.subfields = [(code, data) for code, data in f.subfields if data or code == "h"]
        removed = ", ".join(f"${c}" for c in removed_codes)
        details.append(
            f"removed empty subfield(s) {removed} from ={f.tag}  {f.indicators}{body}"
        )
    return details


#: 852 (Location) subfield codes the LC MARC 21 holdings spec defines
#: as Not Repeatable (https://www.loc.gov/marc/holdings/hd852.html) --
#: unlike $b/$c (both officially Repeatable there, though real
#: production data shows this file only ever misuses that
#: repeatability -- see `fix_852_multiple_b`), more than one occurrence
#: of any of these in the same 852 is a genuine structural violation,
#: not just an institutional-convention one.
_852_NON_REPEATABLE_CODES = frozenset({
    "a", "h", "j", "l", "n", "p", "q", "t", "2", "3", "6", "8",
})


def strip_852_duplicate_non_repeatable_subfields(parsed: ParsedRecord) -> list[str]:
    """Remove every occurrence after the first of an 852 (Location)
    subfield the spec defines as Not Repeatable (see
    `_852_NON_REPEATABLE_CODES`) -- e.g. a second $h (Classification
    part). No occurrences of this were found in a real 79,853-record
    holdings export, but the check exists because the violation is
    unambiguous by spec regardless of what any one file happens to
    contain.

    Which occurrence is kept is just the first -- there's no principled
    way to pick otherwise, unlike `_DUPLICATE_FIELD_RESOLVERS`' tag-
    specific rules for a whole duplicated field. This discards real
    data (the kept copy may not be the "right" one), so it's logged
    under FIXED/REQUIRES ATTENTION (category
    "holdings_852_duplicate_nr_subfield" -- see
    `repair_holdings_records`) with the full removed subfield content,
    same treatment `strip_duplicate_non_repeatable_fields` gives a
    duplicated whole field.
    """
    details = []
    for f in parsed.fields:
        if f.tag != "852" or f.is_control():
            continue
        codes = [code for code, _ in f.subfields]
        offending = {code for code in _852_NON_REPEATABLE_CODES if codes.count(code) > 1}
        if not offending:
            continue
        body = "".join(f"${c}{d}" for c, d in f.subfields)
        seen: set[str] = set()
        kept = []
        removed = []
        for code, data in f.subfields:
            if code in offending:
                if code in seen:
                    removed.append((code, data))
                    continue
                seen.add(code)
            kept.append((code, data))
        f.subfields = kept
        removed_str = ", ".join(f"${c}{d}" for c, d in removed)
        details.append(
            f"removed duplicate Not-Repeatable subfield(s) {removed_str} from "
            f"={f.tag}  {f.indicators}{body}"
        )
    return details


#: Matches an 852 $b (Sublocation) that's nothing but digits -- a real
#: location code in production data is always a textual department/
#: collection abbreviation (see `_852_CUTTER_OR_CLASS_PATTERN`'s own
#: docstring for examples); a bare number has no such meaning and is a
#: strong sign this $b is actually a piece/copy number that landed in
#: the wrong subfield. Used only by `fix_852_b_suspect_content`.
_852_B_NUMERIC_PATTERN = re.compile(r"^\d+$")


def fix_852_b_suspect_content(
    parsed: ParsedRecord, allow_single_digit_852b: bool = False,
) -> list[str]:
    """Fix an 852 (Location) $b (Sublocation) whose content doesn't
    look like a real location code at all:

      * purely numeric (see `_852_B_NUMERIC_PATTERN`) -- e.g. `$b0` --
        rather than a textual department/collection abbreviation --
        unless `allow_single_digit_852b` is set and this $b is exactly
        one digit, in which case it's left untouched (some data
        genuinely uses bare single-digit location codes)
      * contains two or more literal "#" characters -- e.g.
        `$b#8 0 #a 1` -- a strong sign that what should have been
        separate subfields (a real MARC subfield delimiter is byte
        0x1F, not the printable character "#") got flattened into
        this one $b as plain text by whatever exported the record

    Both are real defects seen in production holdings exports, not
    hypothetical: the "#"-flattened case is confirmed data corruption
    (subfields collapsed into one), and the purely-numeric case is a
    piece/copy number miscoded as a location. There's no way to
    recover the real location code, so the suspect $b is replaced
    wholesale with `DEFAULT_852_LOCATION_CONTENT` -- the same
    placeholder used elsewhere for 852 content this tool must
    guess at -- and the original content is logged as-is before
    being overwritten (category "holdings_852_b_suspect_content",
    FIXED/REQUIRES ATTENTION: real data discarded).
    """
    details = []
    for f in parsed.fields:
        if f.tag != "852" or f.is_control():
            continue
        new_subfields = []
        for code, data in f.subfields:
            if code != "b":
                new_subfields.append((code, data))
                continue
            if allow_single_digit_852b and _852_B_NUMERIC_PATTERN.match(data) and len(data) == 1:
                new_subfields.append((code, data))
                continue
            if _852_B_NUMERIC_PATTERN.match(data):
                reason = "is purely numeric, not a location code"
            elif data.count("#") >= 2:
                reason = "contains multiple '#' characters"
            else:
                new_subfields.append((code, data))
                continue
            details.append(
                f"=852  {f.indicators}${code}{data} {reason} -- replaced with "
                f"placeholder {DEFAULT_852_LOCATION_CONTENT!r}"
            )
            new_subfields.append((code, DEFAULT_852_LOCATION_CONTENT))
        f.subfields = new_subfields
    return details


def add_missing_852c(parsed: ParsedRecord) -> list[str]:
    """Append subfield $c (shelving location) to every 852 (Location)
    field that's missing it, with placeholder content
    `DEFAULT_852_C_CONTENT`, but only for a record `_is_wms_holdings_record`
    identifies as OCLC WMS-sourced -- the one convention where $c is the
    live location subfield every record is expected to carry (see
    `fix_missing_852_location`). A non-WMS record missing $c isn't
    necessarily missing anything: that system may simply keep its
    location in $b instead, so inserting a $c placeholder there would be
    adding a subfield the record's own convention never uses. On by
    default (see --no-fix-missing-852c); logged (category
    "added_missing_852c") every time it runs, since it's adding content
    -- not just correcting structure -- and a placeholder rather than a
    value recovered from the record's own data. No-op for a non-WMS
    record, or for an 852 that already has a $c, however placed.
    """
    if not _is_wms_holdings_record(parsed):
        return []
    details = []
    for f in parsed.fields:
        if f.tag != "852" or f.is_control():
            continue
        if any(code == "c" for code, _ in f.subfields):
            continue
        f.subfields = list(f.subfields) + [("c", DEFAULT_852_C_CONTENT)]
        details.append(f"added missing 852 $c: {DEFAULT_852_C_CONTENT!r}")
    return details


def add_default_holdings_008(parsed: ParsedRecord) -> list[str]:
    """Insert `DEFAULT_HOLDINGS_008_CONTENT` for a holdings record with
    no 008 field at all. No-op if 008 is already present. Logged
    (category "added_default_holdings_008") since the inserted value is
    a blank placeholder, not a real one -- see
    `DEFAULT_HOLDINGS_008_CONTENT`.
    """
    if ensure_field(parsed, "008", None, DEFAULT_HOLDINGS_008_CONTENT):
        return [
            "added blank placeholder holdings 008 (was missing): "
            f"{DEFAULT_HOLDINGS_008_CONTENT!r}"
        ]
    return []


#: Placeholder 245 (title statement) for records missing one entirely --
#: a real ILS import will often outright reject a record with no title at
#: all, so a placeholder that flags itself as such ("No title") is safer
#: than leaving the record unimportable, even though a title is never
#: actually invented. See `add_default_245`.
DEFAULT_245_INDICATORS = "00"


DEFAULT_245_SUBFIELDS = [("a", "No title")]


#: Tags checked, in priority order, for a usable stand-in title when a
#: record has no 245 $a of its own. See `resolve_245_fallback`.
_245_FALLBACK_TAGS = ("130", "240", "246", "242", "222")


def resolve_245_fallback(parsed: ParsedRecord) -> tuple[str, list[tuple[str, str]]] | None:
    """Look for a real title value elsewhere in the record to use instead
    of the bare "No title" placeholder, for a record with no 245 (or a
    245 with no $a).

    Checks `_245_FALLBACK_TAGS` in order -- 130/240 (uniform title), 246
    (varying title), 242 (translated title), 222 (key title) -- and
    returns the first one found with a non-empty $a, as
    `(tag, [("a", <value>)])`. Only $a is copied, deliberately: the
    caller is building a minimal placeholder, not reconstructing the
    whole field, so $b/$n/$p etc. from the source field are left behind.

    Failing that, looks for an 880 (Alternate Graphic Representation)
    field linked via $6 to 245 itself, or to one of `_245_FALLBACK_TAGS`
    (same priority order, 245 first since that's the record's own
    directly-paired 880), with a non-empty $a. Returns
    `(f"880 linked to {tag}", [("a", <value>)])` for the first match.

    Returns None if nothing usable was found either way.
    """
    for tag in _245_FALLBACK_TAGS:
        field = next((f for f in parsed.fields if f.tag == tag and not f.is_control()), None)
        if field is None:
            continue
        value = next((d for c, d in field.subfields if c == "a" and d), None)
        if value:
            return tag, [("a", value)]

    for tag in ("245",) + _245_FALLBACK_TAGS:
        prefix = f"{tag}-"
        for f in parsed.fields:
            if f.tag != "880" or f.is_control():
                continue
            if not any(c == "6" and d.startswith(prefix) for c, d in f.subfields):
                continue
            value = next((d for c, d in f.subfields if c == "a" and d), None)
            if value:
                return f"880 linked to {tag}", [("a", value)]
    return None


#: Tags excluded when counting a record's "substantive" fields for
#: `looks_like_abandoned_record` -- control fields that every record
#: carries regardless of how much real cataloging was ever done to it.
_ABANDONED_HEURISTIC_CONTROL_TAGS = {"001", "003", "005", "006", "007", "008"}


def _substantive_field_count(parsed: ParsedRecord) -> int:
    """How many fields on `parsed` aren't one of
    `_ABANDONED_HEURISTIC_CONTROL_TAGS`. Used by
    `looks_like_abandoned_record`."""
    return sum(1 for f in parsed.fields if f.tag not in _ABANDONED_HEURISTIC_CONTROL_TAGS)


def looks_like_abandoned_record(parsed: ParsedRecord) -> bool:
    """Heuristic (not exact science -- just a judgment call worth
    surfacing to a human, never one this tool acts on) guess that a
    record with no title anywhere (see `resolve_245_fallback`) is an
    abandoned or test stub rather than a genuine record that simply lost
    its 245: very few substantive fields, no author access point (1xx),
    no subject or added-entry access point (6xx/7xx), and no 300/500
    carrying real content either. Only used to annotate
    `add_default_245`'s "No title" placeholder log line -- it never
    changes what gets written to the record.
    """
    substantive = [f for f in parsed.fields if f.tag not in _ABANDONED_HEURISTIC_CONTROL_TAGS]
    if len(substantive) > 3:
        return False
    for f in substantive:
        if f.tag in ("100", "110", "111", "130") or f.tag[:1] in ("6", "7"):
            return False
        if f.tag in ("300", "500") and not f.is_control() and any(d for _, d in f.subfields):
            return False
    return True


def record_missing_245(parsed: ParsedRecord) -> bool:
    """True if `parsed` has no 245 field, or a 245 with no non-empty $a --
    the same condition `add_default_245` itself fixes. Pulled out as its
    own predicate so a caller that wants to know *whether* a record would
    need fixing, without actually fixing it (e.g. --no-add-default-245
    diverting such a record to a review file instead), shares the exact
    same definition of "missing" rather than re-deriving it -- see
    `main`'s bib loop."""
    field245 = next((f for f in parsed.fields if f.tag == "245"), None)
    return field245 is None or not any(
        code == "a" and data for code, data in field245.subfields
    )


def add_default_245(parsed: ParsedRecord) -> list[str]:
    """Ensure every record has a 245 with a non-empty $a.

    245 is mandatory in every MARC21 record, unlike the heading fields
    `strip_missing_required_a` strips when they lack $a (245 itself is
    deliberately excluded from required_a_tags.txt for this reason) --
    so a 245 that already exists but lacks $a (e.g. one with only
    $h[electronic resource]) is patched in place: $a is inserted at the
    front of its existing subfields, keeping the rest, rather than
    stripping the field and rebuilding it from scratch. Only a record
    with no 245 field at all gets a brand new one. No-op if 245 already
    has a non-empty $a (e.g. supplied correctly via --ensure-field, which
    runs before this).

    When a real $a is genuinely missing, `resolve_245_fallback` is tried
    first, so a record that has a usable title elsewhere (130/240/246/
    242/222, or a linked 880) gets that instead of the bare "No title"
    placeholder. Only when that also finds nothing does this fall back
    to "No title" -- and in that case, `looks_like_abandoned_record` is
    also checked, adding a note to the log detail (not a new log
    category) when the record looks like a minimal/test stub rather than
    a genuine title loss, so a human reviewing the log can tell the two
    apart. All cases log the same category ("added_default_245") since
    the net effect is the same: a placeholder title where a real one was
    missing.
    """
    if not record_missing_245(parsed):
        return []
    field245 = next((f for f in parsed.fields if f.tag == "245"), None)

    def _patch_or_insert(subfields: list[tuple[str, str]]) -> None:
        if field245 is None:
            ensure_field(parsed, "245", DEFAULT_245_INDICATORS, subfields)
        else:
            # drop any $a that's present but empty, so the new $a doesn't
            # end up alongside a second, empty $a
            other_subfields = [
                (c, d) for c, d in field245.subfields if not (c == "a" and not d)
            ]
            field245.subfields = list(subfields) + other_subfields

    fallback = resolve_245_fallback(parsed)
    if fallback is not None:
        source_tag, subfields = fallback
        value = subfields[0][1]
        _patch_or_insert(subfields)
        return [f"populated missing 245 from {source_tag}: $a{value!r}"]

    # checked before inserting the placeholder 245 itself, so a brand
    # new 245 (field245 is None) doesn't inflate its own substantive
    # field count
    abandoned = looks_like_abandoned_record(parsed)
    n = _substantive_field_count(parsed)

    _patch_or_insert(DEFAULT_245_SUBFIELDS)
    if field245 is None:
        detail = "added placeholder 245 (was missing): 00 $aNo title"
    else:
        detail = "added placeholder $aNo title to existing 245 (had no $a)"
    if abandoned:
        detail += (
            f" (heuristic: looks like an abandoned/test record -- {n} substantive "
            "field(s), no author/subject access points)"
        )
    return [detail]


def parse_ensure_field_spec(
    spec: str,
) -> tuple[str, str | None, list[tuple[str, str]] | str]:
    """Parse a CLI --ensure-field spec. Two forms:

      * Control field (tag < "010"): "TAG:CONTENT" -- e.g.
        "008:780615s19uu    xx a                    d". No indicators or
        subfields exist for these, so the rest of the spec after the tag
        is used verbatim as the field's content.
      * Data field: "TAG:INDICATORS:CODE=VALUE[|CODE=VALUE...]" -- e.g.
        "245:00:a=No title" or "500:  :a=Note one|a=Note two".

    Returns (tag, indicators, subfields) for a data field, or
    (tag, None, content) for a control field.
    """
    try:
        tag, rest = spec.split(":", 1)
    except ValueError:
        raise ValueError(f"--ensure-field spec must start with TAG:..., got {spec!r}")
    if not tag.isdigit() or len(tag) != 3:
        raise ValueError(f"tag must be 3 digits, got {tag!r}")
    if int(tag) < CONTROL_TAG_LIMIT:
        return tag, None, rest
    try:
        indicators, subfields_str = rest.split(":", 1)
    except ValueError:
        raise ValueError(
            f"--ensure-field spec must be TAG:INDICATORS:CODE=VALUE, got {spec!r}"
        )
    if len(indicators) != 2:
        raise ValueError(f"indicators must be exactly 2 characters, got {indicators!r}")
    subfields = []
    for part in subfields_str.split("|"):
        code, sep, value = part.partition("=")
        if not sep:
            raise ValueError(f"subfield spec must be CODE=VALUE, got {part!r}")
        subfields.append((code, value))
    return tag, indicators, subfields


DEFAULT_REQUIRED_A_TAGS_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "required_a_tags.txt"
)


DEFAULT_NON_REPEATABLE_TAGS_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "non_repeatable_tags.txt"
)


DEFAULT_HOLDINGS_REQUIRED_A_TAGS_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "holdings_required_a_tags.txt"
)


#: Holdings 853/854/855 (Captions and Pattern -- Basic/Supplementary/
#: Index) get their own required-content check, separate from
#: holdings_required_a_tags.txt's plain $a-only tags -- see
#: `strip_missing_required_a`'s `valid_codes` param.
HOLDINGS_CAPTION_PATTERN_TAGS = {"853", "854", "855"}


HOLDINGS_CAPTION_PATTERN_VALID_CODES = frozenset({"a", "g", "i"})


DEFAULT_STANDARD_MARC_FIELDS_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "standard_marc_fields.txt"
)


#: Category for `NonStandardFieldTally`'s findings, and how many of each
#: tag's occurrences get a detailed log entry (the rest are only counted).
NON_STANDARD_FIELD_CATEGORY = "non_standard_marc_fields"


NON_STANDARD_FIELD_DETAIL_LIMIT = 5


_NON_STANDARD_PREVIEW_CHARS = 20


def load_standard_marc_fields(
    path: str, source_system: str | None = None
) -> tuple[set[str], set[str]]:
    """Load standard_marc_fields.txt: returns (standard, obsolete) tag
    sets. One `TAG | TITLE` per line under a `[standard]`, `[obsolete]` or
    `[wms]` heading; blank lines and lines starting with # are ignored.
    The `[wms]` tags (standard in OCLC WMS only) count as standard only
    when `source_system` is "wms". A tag in no standard section is
    non-standard."""
    sections: dict[str, set[str]] = {"standard": set(), "obsolete": set(), "wms": set()}
    current = None
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line in ("[standard]", "[obsolete]", "[wms]"):
                current = line[1:-1]
                continue
            if current is None:
                raise ValueError(f"{path}: tag {line!r} appears before a [section] heading")
            sections[current].add(line.split("|", 1)[0].strip())
    standard = sections["standard"]
    if source_system == "wms":
        standard = standard | sections["wms"]
    return standard, sections["obsolete"]


def field_preview(f: Field_, max_chars: int = _NON_STANDARD_PREVIEW_CHARS) -> str:
    """Human-readable start of a field's data: no delimiters or
    end-of-field marker, each subfield as "$<code> <data>", cut to
    `max_chars` characters (plus "..." if cut). Indicators are not
    shown."""
    if f.is_control():
        text = f.content or ""
    else:
        text = " ".join(f"${code} {data}" for code, data in f.subfields)
    text = text.replace(FIELDTERM, "").replace(SUBFIELD, "$")
    return text[:max_chars] + "..." if len(text) > max_chars else text


class NonStandardFieldTally:
    """Detect-only check for fields whose tag isn't in
    standard_marc_fields.txt (or is listed there as obsolete); 9XX tags
    and non-numeric tags are never reported. Counts every occurrence per
    tag but only hands back detail lines for the first
    `NON_STANDARD_FIELD_DETAIL_LIMIT` of each tag -- `write_log` uses the
    counts for each tag's heading and its "and N more" line."""

    def __init__(self, standard: set[str], obsolete: set[str],
                 limit: int = NON_STANDARD_FIELD_DETAIL_LIMIT):
        self.standard = standard
        self.obsolete = obsolete - standard
        self.limit = limit
        self.instances: dict[str, int] = {}
        self.records: dict[str, int] = {}
        self.listed: dict[str, int] = {}
        self.records_with_any = 0
        #: every occurrence (not just the first `limit`) is streamed to
        #: one temp file, one line each; `_offsets` holds each tag's
        #: line start offsets so the supplemental file can be built
        #: grouped by tag without keeping the text in memory
        self._spool = tempfile.TemporaryFile("w+", encoding="utf-8", newline="\n")
        self._offsets: dict[str, array] = {}

    def spooled_lines(self, tag: str) -> Iterator[str]:
        """Yield tag's spooled "record ...<TAB>detail" lines, in order."""
        self._spool.flush()
        for off in self._offsets.get(tag, ()):
            self._spool.seek(off)
            yield self._spool.readline().rstrip("\n")

    def close(self) -> None:
        self._spool.close()

    def check(self, parsed: ParsedRecord, record_idx: int = 0,
              rec_id: str = "") -> list[str]:
        """Count this record's non-standard fields; returns the detail
        text ("=<tag> <preview>") for those still under the per-tag
        limit. Every occurrence is also kept in `all_details` for
        `write_non_standard_supplement`. Never modifies the record."""
        details = []
        tags_seen: set[str] = set()
        for f in parsed.fields:
            tag = f.tag
            if len(tag) != 3 or not tag.isdigit() or tag[0] == "9" or tag in self.standard:
                continue
            self.instances[tag] = self.instances.get(tag, 0) + 1
            if tag not in tags_seen:
                tags_seen.add(tag)
                self.records[tag] = self.records.get(tag, 0) + 1
            detail = f"={tag} {field_preview(f)}"
            rec = f"record {record_idx}" + (f" ({rec_id})" if rec_id else "")
            self._spool.seek(0, os.SEEK_END)
            self._offsets.setdefault(tag, array("Q")).append(self._spool.tell())
            self._spool.write(f"{rec}\t{detail}".replace("\n", " ") + "\n")
            if self.listed.get(tag, 0) < self.limit:
                self.listed[tag] = self.listed.get(tag, 0) + 1
                details.append(detail)
        if tags_seen:
            self.records_with_any += 1
        return details


def write_non_standard_toc(fh, tally: NonStandardFieldTally) -> None:
    """A "table of contents" line per tag (with its instance count), in
    tag order, for the top of the non-standard-fields report."""
    fh.write("\nTags found:\n")
    for tag in sorted(tally.instances):
        obsolete = " [OBSOLETE]" if tag in tally.obsolete else ""
        fh.write(f"\t{tag}{obsolete}: {tally.instances[tag]:,} instance(s)\n")


def write_non_standard_supplement(path: str, tally: NonStandardFieldTally) -> bool:
    """Write every non-standard field occurrence, grouped by tag, in the
    same layout as the log's non_standard_marc_fields section but with
    no "and N more" cut-off. Returns False (and writes nothing) if there
    were none."""
    if not tally.instances:
        return False
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(
            f"=== non_standard_marc_fields: all occurrences ===\n"
            f"=== {sum(tally.instances.values()):,} instance(s) in "
            f"{tally.records_with_any:,} record(s), {len(tally.instances)} tag(s) ===\n"
        )
        write_non_standard_toc(fh, tally)
        for tag in sorted(tally.instances):
            obsolete = " [OBSOLETE]" if tag in tally.obsolete else ""
            fh.write(
                f"\n--- {tag}{obsolete}: {tally.instances[tag]:,} instance(s) "
                f"in {tally.records[tag]:,} record(s) ---\n"
            )
            for line in tally.spooled_lines(tag):
                fh.write(f"\t{line}\n")
    return True


def find_853_missing_8(parsed: ParsedRecord) -> list[str]:
    """Detect (never fix) an 853 (Captions and Pattern -- Basic) field
    with no $8 (Field link and sequence number) -- the subfield that
    links it to its corresponding 863 (Enumeration and Chronology --
    Basic) field(s). Without it, an 863 has no reliable way to find the
    caption/pattern that defines its structure -- a real issue flagged
    by this institution's own downstream FOLIO/transform validation
    ("853 subfield 8 not in field"). Flagged only, one detail line per
    offending field (category "holdings_853_missing_8", NOT FIXED:
    this tool has no way to invent a real, correct link/sequence
    number).
    """
    details = []
    for f in parsed.fields:
        if f.tag != "853" or f.is_control():
            continue
        if any(code == "8" for code, _ in f.subfields):
            continue
        body = "".join(f"${code}{data}" for code, data in f.subfields)
        details.append(f"={f.tag}  {f.indicators}{body} has no $8 subfield")
    return details


def find_856_missing_u(parsed: ParsedRecord) -> list[str]:
    """Detect (never fix) an 856 (Electronic Location and Access)
    field with no $u (URI) -- without one, there's no actual link for
    the electronic access this field claims to describe. A real issue
    this institution's own downstream FOLIO/transform validation
    already flags ("Missing one or more required property in entity
    electronicAccess (['uri'])"). Flagged only, one detail line per
    offending field (NOT FIXED: this tool has no way to invent a real
    URL); applies equally to bib and holdings records -- logged under
    category "bib_856_missing_u" or "holdings_856_missing_u" by the
    caller depending on which pipeline is running.
    """
    details = []
    for f in parsed.fields:
        if f.tag != "856" or f.is_control():
            continue
        if any(code == "u" for code, _ in f.subfields):
            continue
        body = _subfield_preview(f.subfields)
        details.append(f"={f.tag}  {f.indicators}{body} (has no $u subfield)")
    return details


_STRAY_U_URL_RE = re.compile(r"\$?u\s+(https?://\S+)\s*")


_BARE_URL_RE = re.compile(r"\s*((?:https?://|www\.)\S+)\s*")


def recode_856_missing_u(parsed: ParsedRecord) -> list[str]:
    """Recode the URL in an 856 with no $u to $u, when that's
    unambiguous: (1) the "u" subfield code was lost into another
    subfield's data -- `$a` "u http://..." / "$u http://...", or `$a` "u"
    followed by `$h` "ttps://..." (the code and first letter shifted) --
    or (2) exactly one $a or $z holds nothing but a bare http(s) URL,
    or a scheme-less "www...." address (https:// is added, and the log
    detail says so). Text with a URL embedded in it is left alone for
    `find_856_missing_u` to flag. Returns one detail
    string per field recoded (category "bib_856_missing_u", fixed)."""
    details = []
    for f in parsed.fields:
        if f.tag != "856" or f.is_control():
            continue
        if any(code == "u" for code, _ in f.subfields):
            continue
        subs = list(f.subfields)
        what = None
        for n, (code, data) in enumerate(subs):
            m = _STRAY_U_URL_RE.fullmatch(data)
            if m:
                subs[n] = ("u", m.group(1))
                what = f"${code} recoded to $u, stray 'u' removed"
                break
            if (
                code == "a" and data == "u" and n + 1 < len(subs)
                and subs[n + 1][0] == "h" and subs[n + 1][1].startswith("ttp")
            ):
                subs[n:n + 2] = [("u", "h" + subs[n + 1][1])]
                what = "$a 'u' + $h recombined into $u"
                break
        if what is None:
            urls = [
                n for n, (code, data) in enumerate(subs)
                if code in ("a", "z") and _BARE_URL_RE.fullmatch(data)
            ]
            if len(urls) != 1:
                continue
            code, data = subs[urls[0]]
            url = data.strip()
            what = f"${code} recoded to $u"
            if url.startswith("www."):
                url = "https://" + url
                what += ", https:// scheme added"
            subs[urls[0]] = ("u", url)
        details.append(
            f"={f.tag}  {f.indicators}{_subfield_preview(subs)} ({what})"
        )
        f.subfields = subs
    return details


def _choose_kept_001(fields_for_tag: list[Field_], is_sirsi: bool) -> Field_:
    """A duplicated 001 in a Sierra/Symphony-sourced record (003
    content "SIRSI", case-insensitive) keeps whichever occurrence
    starts with "u" -- that's this system's own real bib-id convention
    (e.g. "u508261"), so a duplicate 001 there is far more likely to be
    a stray *other* identifier (an OCLC number, a barcode) that ended
    up in 001 by mistake than the record's actual id. Falls back to
    the first occurrence if not SIRSI, or if none of them start with
    "u"."""
    if is_sirsi:
        preferred = next(
            (f for f in fields_for_tag
             if f.content and f.content.strip().lower().startswith("u")),
            None,
        )
        if preferred is not None:
            return preferred
    return fields_for_tag[0]


def _choose_kept_005(fields_for_tag: list[Field_], is_sirsi: bool) -> Field_:
    """005 (date/time of latest transaction) is a fixed-width,
    zero-padded YYYYMMDDHHMMSS.F string with a 4-digit year, so plain
    string comparison IS chronological comparison, no parsing needed.
    Keeps the most recent; ties keep the first occurrence."""
    best = fields_for_tag[0]
    for f in fields_for_tag[1:]:
        if (f.content or "") > (best.content or ""):
            best = f
    return best


def _choose_kept_008(fields_for_tag: list[Field_], is_sirsi: bool) -> Field_:
    """008 bytes 0-5 (date entered on file, YYMMDD) compared as a
    plain string -- keeps the most recent; ties keep the first
    occurrence."""
    best = fields_for_tag[0]
    best_key = (best.content or "")[:6]
    for f in fields_for_tag[1:]:
        key = (f.content or "")[:6]
        if key > best_key:
            best, best_key = f, key
    return best


_ORG_CODE_RE = re.compile(r"[A-Za-z][A-Za-z0-9-]*")


def _choose_kept_003(fields_for_tag: list[Field_], is_sirsi: bool) -> Field_:
    """003 (MARC organization code, e.g. "OCoLC") keeps the occurrence
    that looks like one -- letters/digits/hyphens starting with a letter,
    a capital preferred -- over junk such as an OCLC number or a stray
    note ("301198", "WITDRAWN AS PER C.E.D. 11-04") that ended up in 003
    ahead of the real code. Falls back to the first occurrence if none
    looks like a code."""
    codes = [f for f in fields_for_tag if _ORG_CODE_RE.fullmatch((f.content or "").strip())]
    return max(codes, key=lambda f: f.content.strip()[0].isupper(), default=fields_for_tag[0])


#: Per-tag tiebreakers for which occurrence of a duplicated
#: non-repeatable field to keep -- anything not listed here just keeps
#: the first occurrence (see `strip_duplicate_non_repeatable_fields`).
_DUPLICATE_FIELD_RESOLVERS = {
    "001": _choose_kept_001,
    "003": _choose_kept_003,
    "005": _choose_kept_005,
    "008": _choose_kept_008,
}


#: How much of a removed duplicate field's own content (indicators +
#: subfields, or control content) `strip_duplicate_non_repeatable_fields`
#: shows in its log detail -- a duplicate 245 can carry a full title
#: statement, and the log only needs enough of it to recognize the
#: field, not the whole thing (real example: a record with the same
#: 245 $a repeated verbatim produced a single log line over 150
#: characters long for what is, by definition, an exact duplicate).
_DUPLICATE_FIELD_BODY_TRUNCATE_LEN = 25


def strip_duplicate_non_repeatable_fields(
    parsed: ParsedRecord, non_repeatable_tags: set[str]
) -> list[str]:
    """A record must be loadable even when it's genuinely broken --
    MARC21 designates some fields Not Repeatable (see
    non_repeatable_tags.txt), and a second occurrence of one (e.g. two
    245s) is exactly the kind of thing a strict importer like FOLIO can
    reject outright or handle unpredictably. Keeps one occurrence of
    each tag in `non_repeatable_tags`, removes every other one.

    Which occurrence is kept is normally just the first, with tag-
    specific exceptions in `_DUPLICATE_FIELD_RESOLVERS`: 001 prefers a
    "u"-prefixed value on Sierra/Symphony records, 003 prefers a value
    that looks like an organization code over junk, 005 and 008 keep
    the most recent by date.

    This discards real data, so unlike this tool's routine removals
    (an empty field, an unusable subfield code) it's deliberately NOT
    filed as an ordinary "fixed" entry -- see FIXED/REQUIRES ATTENTION
    in `main`, which logs the exact removed field content so a human
    can decide whether it needed to go somewhere else instead (real
    example found in production data: a second "245" containing only
    $a "2nd ed." -- almost certainly a mistagged 250, not a genuine
    second title).
    """
    occurrences: dict[str, list[Field_]] = {}
    for f in parsed.fields:
        if f.tag in non_repeatable_tags:
            occurrences.setdefault(f.tag, []).append(f)

    is_sirsi = any(
        f.tag == "003" and f.content and f.content.strip().lower() == "sirsi"
        for f in parsed.fields
    )
    keep: dict[str, Field_] = {}
    for tag, fields_for_tag in occurrences.items():
        if len(fields_for_tag) <= 1:
            continue
        resolver = _DUPLICATE_FIELD_RESOLVERS.get(tag)
        keep[tag] = resolver(fields_for_tag, is_sirsi) if resolver else fields_for_tag[0]

    details = []
    kept_fields = []
    for f in parsed.fields:
        duplicates = occurrences.get(f.tag)
        if duplicates and len(duplicates) > 1:
            if f is keep[f.tag]:
                kept_fields.append(f)
            else:
                if f.is_control():
                    body = f.content or ""
                else:
                    body = f.indicators + "".join(f"${c}{d}" for c, d in f.subfields)
                if len(body) > _DUPLICATE_FIELD_BODY_TRUNCATE_LEN:
                    body = body[:_DUPLICATE_FIELD_BODY_TRUNCATE_LEN] + "..."
                detail = f"removed duplicate ={f.tag}  {body}\t(non-repeatable field)"
                original = duplicates[0]
                if f is not original and _fields_content_equal(f, original):
                    detail += " -- NO DATA LOSS - fields are exact duplicates"
                else:
                    detail += " -- POSSIBLE DATA LOSS"
                details.append(detail)
            continue
        kept_fields.append(f)
    parsed.fields = kept_fields
    return details


#: Role words that cataloging sometimes leaves in parentheses after a
#: name in $a (e.g. "Daly, Pat (editor)"), mapped to the relator term
#: that belongs in $e instead.
_ROLE_IN_A_TERMS = {
    "editor": "editor", "ed.": "editor", "author": "author",
    "translator": "translator", "compiler": "compiler",
    "illustrator": "illustrator", "contributor": "contributor",
    "preface": "writer of preface", "foreword": "writer of foreword",
    "foreward": "writer of foreword", "introduction": "writer of introduction",
}


_ROLE_IN_A_RE = re.compile(
    r"^(?P<name>.*\S)\s*\((?P<role>" + "|".join(
        re.escape(r) for r in _ROLE_IN_A_TERMS) + r")\)\.?\s*$",
    re.IGNORECASE,
)


#: Subfields a 246 carries -- everything else on a 245 ($c, $k, $s, ...)
#: is dropped when a duplicate 245 is moved to a 246.
_246_CODES = frozenset("abfghinp568")


#: A 245 that's really an edition statement ("Abridged ed", "2nd rev. ed.")
#: -- belongs in a 250, not a 246.
_EDITION_ONLY_RE = re.compile(r"\b(ed\.?|edition|rev\.?|reprint)\s*$", re.IGNORECASE)


def _insert_after_tag(fields: list[Field_], new: list[Field_], tag: str) -> list[Field_]:
    """`fields` with `new` inserted after any existing `tag` fields,
    ahead of the next higher tag."""
    at = next((i for i, f in enumerate(fields) if f.tag > tag), len(fields))
    return fields[:at] + new + fields[at:]


def move_duplicate_100_to_700(parsed: ParsedRecord) -> list[str]:
    """A second (or later) 100 on one record -- 100 is Not Repeatable --
    is another person's name in practice (co-author, editor,
    contributor), so it's recoded as a 700 (Added Entry--Personal Name)
    instead of being removed by `strip_duplicate_non_repeatable_fields`.
    The first 100 stays where it is.

    Left alone (so the generic removal still handles it) when it's an
    exact copy of the first 100, or has no real $a. Indicator 1 is kept
    if 0/1/3, otherwise 1 when $a has a comma and 0 if not; indicator 2
    is blank. A trailing "(editor)"-style role on $a moves to $e. $6 is
    dropped (its occurrence number names the old tag and the paired 880
    would need rewriting too). A move whose 700 is already on the record
    just drops the extra 100.

    Returns a list of detail strings, one per field handled (category
    "moved_duplicate_to_added_entry" -- see `main`)."""
    firsts = [f for f in parsed.fields if f.tag == "100" and not f.is_control()]
    if len(firsts) < 2:
        return []
    first = firsts[0]
    existing_700 = [f for f in parsed.fields if f.tag == "700" and not f.is_control()]
    details = []
    new_fields = []
    kept = []
    for f in parsed.fields:
        if f is first or f.tag != "100" or f.is_control():
            kept.append(f)
            continue
        if _fields_content_equal(f, first) or not _has_real_a(f):
            kept.append(f)
            continue
        subs = [(c, d) for c, d in f.subfields if c != "6" and d]
        notes = ["dropped $6 link"] if len(subs) != len(f.subfields) else []
        a_val = next(d for c, d in subs if c == "a")
        role = _ROLE_IN_A_RE.match(a_val)
        if role and not any(c == "e" for c, _ in subs):
            subs = [("a", role.group("name").rstrip(" ,") + ",") if c == "a" and d == a_val
                    else (c, d) for c, d in subs]
            at = max(i for i, (c, _) in enumerate(subs) if c in "abcdq")
            subs.insert(at + 1, ("e", _ROLE_IN_A_TERMS[role.group("role").lower()] + "."))
            notes.append("role moved from $a to $e")
        ind1 = f.indicators[0] if f.indicators[:1] in ("0", "1", "3") else (
            "1" if "," in a_val else "0")
        new = Field_("700", ind1 + " ", subs)
        shown = f"={f.tag}  {f.indicators}{_subfield_preview(f.subfields)}"
        if any(x.subfields == subs and x.indicators == new.indicators for x in existing_700):
            details.append(f"removed duplicate {shown} (non-repeatable field) -- already "
                           "present as a 700 -- NO DATA LOSS")
            continue
        new_fields.append(new)
        existing_700.append(new)
        tail = f" ({'; '.join(notes)})" if notes else ""
        details.append(
            f"moved duplicate {shown} to ={new.tag}  {new.indicators}"
            f"{_subfield_preview(subs)}{tail} -- "
            f"{'POSSIBLE DATA LOSS' if notes else 'NO DATA LOSS'}"
        )
    if details:
        parsed.fields = _insert_after_tag(kept, new_fields, "700")
    return details


def _normalized_title(text: str) -> str:
    return re.sub(r"\W+", "", text.casefold())


def move_duplicate_245_to_246(parsed: ParsedRecord) -> list[str]:
    """A second (or later) 245 on one record -- 245 is Not Repeatable --
    that is really a variant of the title (shortened, alternate,
    cover-style) is recoded as a 246 (Varying Form of Title, indicator 1
    = 3 "no note, added entry", indicator 2 blank) instead of being
    removed by `strip_duplicate_non_repeatable_fields`. The first 245
    stays where it is.

    Deliberately conservative -- the extra 245 is moved only when ALL of
    these hold, anything else is left for the generic removal:
      * it has a real $a and no $6 (the paired 880 would need rewriting)
        and no $c: a statement of responsibility means it's a separate
        title (another volume, a part, a series item), not a variant;
      * its $a doesn't look like an edition statement ("Abridged ed" is a
        250) or start with a stray "0 " (a leaked indicator);
      * the FIRST 245 has no $l, $v, $t or $6: one of those means the
        first 245 is itself a mistagged uniform title or series, so
        which one is the real title can't be told from here;
      * it isn't a normalized copy of a 246 already on the record.
    Subfields a 246 can't carry ($k, $s, ...) are dropped, noted in the
    log line.

    Returns a list of detail strings, one per field moved (category
    "moved_duplicate_to_added_entry" -- see `main`)."""
    firsts = [f for f in parsed.fields if f.tag == "245" and not f.is_control()]
    if len(firsts) < 2:
        return []
    first = firsts[0]
    if any(c in "lvt6" for c, _ in first.subfields):
        return []
    existing = {
        _normalized_title("".join(d for c, d in f.subfields if c in "ab"))
        for f in parsed.fields if f.tag == "246" and not f.is_control()
    }
    details = []
    new_fields = []
    kept = []
    for f in parsed.fields:
        if f is first or f.tag != "245" or f.is_control():
            kept.append(f)
            continue
        codes = {c for c, d in f.subfields if d}
        a_val = next((d for c, d in f.subfields if c == "a" and d), "")
        if (
            _fields_content_equal(f, first) or not _has_real_a(f)
            or "6" in codes or "c" in codes
            or (codes <= {"a"} and _EDITION_ONLY_RE.search(a_val.strip()))
            or re.match(r"0\s", a_val)
        ):
            kept.append(f)
            continue
        subs = [(c, d) for c, d in f.subfields if c in _246_CODES and d]
        dropped = sorted({c for c, d in f.subfields if d and c not in _246_CODES})
        new = Field_("246", "3 ", subs)
        shown = f"={f.tag}  {f.indicators}{_subfield_preview(f.subfields)}"
        key = _normalized_title("".join(d for c, d in subs if c in "ab"))
        if key in existing:
            details.append(f"removed duplicate {shown} (non-repeatable field) -- already "
                           "present as a 246 -- NO DATA LOSS")
            continue
        existing.add(key)
        new_fields.append(new)
        tail = f" (dropped ${', $'.join(dropped)})" if dropped else ""
        details.append(
            f"moved duplicate {shown} to ={new.tag}  {new.indicators}"
            f"{_subfield_preview(subs)}{tail} -- "
            f"{'POSSIBLE DATA LOSS' if dropped else 'NO DATA LOSS'}"
        )
    if details:
        parsed.fields = _insert_after_tag(kept, new_fields, "246")
    return details


def _place(fields: list[Field_], new: list[Field_]) -> list[Field_]:
    """`fields` with each of `new` inserted after any existing field of
    its own tag, ahead of the next higher tag."""
    for f in new:
        fields = _insert_after_tag(fields, [f], f.tag)
    return fields


def _is_mistagged_041(f: Field_) -> bool:
    """A 040 that is really a 041 (language codes): it has an $h, or its
    $a is nothing but lowercase three-letter language codes ("eng",
    "gereng")."""
    a = next((d for c, d in f.subfields if c == "a"), "")
    return any(c == "h" for c, _ in f.subfields) or (
        re.fullmatch(r"(?:[a-z]{3})+", a) is not None
    )


def _is_non_agency_040(f: Field_) -> bool:
    """A 040 that isn't a cataloging source at all -- a call number
    ("BS1430.52", with a $b cutter) or an ISBN in $a, or a $b that isn't
    a three-letter language code."""
    a = next((d for c, d in f.subfields if c == "a"), "")
    return (
        (a != "" and not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]*", a))
        or any(c == "b" and not re.fullmatch(r"[A-Za-z0-9-]+", d) for c, d in f.subfields)
    )


def _valid_nonfiling(ind: str, text: str) -> str:
    """`ind` (a nonfiling-character count copied from another field) if the
    characters it would skip in `text` end at a word boundary (a space,
    apostrophe or hyphen -- "The ", "L'"), else "0": a count that cuts
    into a word is a cataloging error that shouldn't be copied along."""
    n = int(ind) if ind.isdigit() else 0
    return str(n) if n == 0 or (n < len(text) and text[n - 1] in " '-") else "0"


def merge_duplicate_040(parsed: ParsedRecord) -> list[str]:
    """040 (Cataloging Source) is Not Repeatable, and a later 040 on a
    record is almost always a modifying agency's stamp ($d) or the
    transcribing agency ($c) the first one lacks -- so its subfields are
    merged into the first 040 instead of being removed along with it.

    Merge rules: $a/$b/$c keep the first value seen (a later, different
    $a or $c is demoted to a $d, a later different $b is dropped); $d and
    $e are added if not already there (a $d naming the same agency as
    $a/$c is redundant and skipped); $6/$8 are ignored. Result order is
    $a $b $c $d... $e. A 040 that is really a 041 (see
    `_is_mistagged_041`, e.g. "1  $aeng$hgrc$dUtOrBLW") has its language
    subfields moved to a new 041 (when the record has none) and its
    agency subfields merged like any other; with a 041 already present
    it is left alone. When every extra 040 is an exact copy of the first,
    nothing is done here (the generic removal summarizes those).

    Returns a list of detail strings, one per record (category
    "merged_duplicate_040" -- see `main`)."""
    f40 = [f for f in parsed.fields if f.tag == "040" and not f.is_control()]
    if len(f40) < 2:
        return []
    odd = [f for f in f40 if _is_mistagged_041(f)]
    rest = [f for f in f40 if not any(f is o for o in odd)]
    junk = [f for f in rest if _is_non_agency_040(f)]
    real = [f for f in rest if not any(f is j for j in junk)]
    if not real:
        return []
    convert = [] if any(f.tag == "041" for f in parsed.fields) else odd
    extras = real[1:]
    if not convert and not junk and all(_fields_content_equal(x, real[0]) for x in extras):
        return []
    base = real[0]
    cur: dict[str, str] = {}
    d_list: list[str] = []
    e_list: list[str] = []
    lost: list[str] = []
    demoted: list[str] = []

    def add_d(value: str, skip_redundant: bool) -> None:
        if value not in d_list and not (skip_redundant and value in (cur.get("a"), cur.get("c"))):
            d_list.append(value)

    def absorb(subs: list[tuple[str, str]], is_base: bool) -> None:
        for c, d in subs:
            if not d:
                continue
            if c in "abc":
                if c not in cur:
                    cur[c] = d
                elif cur[c] != d:
                    if c == "b":
                        lost.append(f"$b{d}")
                    else:
                        demoted.append(f"${c}{d}")
                        add_d(d, False)
            elif c == "d":
                add_d(d, not is_base)
            elif c == "e":
                if d not in e_list:
                    e_list.append(d)
            elif c not in "68":
                lost.append(f"${c}{d}")

    absorb(base.subfields, True)
    for x in extras:
        absorb(x.subfields, False)
    new_041 = []
    for o in convert:
        lang = []
        for c, d in o.subfields:
            if c == "a" and d:
                lang += [("a", d[i:i + 3]) for i in range(0, len(d), 3)]
            elif c == "h" and d:
                lang.append((c, d))
        new_041.append(Field_("041", ("1" if any(c == "h" for c, _ in lang) else "0") + " ", lang))
        absorb([(c, d) for c, d in o.subfields if c in "cde"], False)
        lost.extend(f"${c}{d}" for c, d in o.subfields if c not in "ahcde68" and d)
    lost.extend(
        f"non-agency =040 {j.indicators}{_subfield_preview(j.subfields)}" for j in junk)
    merged = (
        [(c, cur[c]) for c in "abc" if c in cur]
        + [("d", d) for d in d_list] + [("e", e) for e in e_list]
    )
    gone = [id(x) for x in extras + convert + junk]
    before = f"={base.tag}  {base.indicators}{_subfield_preview(base.subfields)}"
    joined = " + ".join(
        f"={x.tag}  {x.indicators}{_subfield_preview(x.subfields)}"
        for x in extras + convert + junk)
    parsed.fields = [f for f in parsed.fields if id(f) not in gone]
    base.subfields = merged
    parsed.fields = _place(parsed.fields, new_041)
    notes = [f"demoted {', '.join(demoted)} to $d"] if demoted else []
    notes += [f"dropped {', '.join(lost)}"] if lost else []
    notes += [f"{len(new_041)} mistagged 041 recoded"] if new_041 else []
    return [
        f"merged {joined} into {before} -> "
        f"={base.tag}  {base.indicators}{_subfield_preview(merged)}"
        f"{' (' + '; '.join(notes) + ')' if notes else ''} -- "
        f"{'POSSIBLE DATA LOSS' if demoted or lost else 'NO DATA LOSS'}"
    ]


#: Something a serial's cataloger put in a 240 that is really its
#: frequency (310), e.g. "two no. a year from 1998".
_FREQUENCY_RE = re.compile(
    r"\b(?:(?:no|issues?)\.?\s+(?:a|per)\s+(?:year|month|week)|(?:a|per)\s+"
    r"(?:year|month|week)|(?:semi|bi|tri)?(?:monthly|weekly|annual(?:ly)?)|quarterly)\b",
    re.IGNORECASE,
)


_730_FROM_240_CODES = frozenset("adfghklmnoprs012")


def move_duplicate_240_to_730(parsed: ParsedRecord) -> list[str]:
    """A second (or later) 240 -- Not Repeatable -- is another uniform
    title for the record, so it's recoded as a 730 (Added Entry--Uniform
    Title; indicator 1 = the 240's nonfiling count, indicator 2 blank)
    instead of being removed. When its text is simply a part of the 245's
    title it is a variant title and becomes a 246 (`3 0`, "portion of
    title") instead. $6 is dropped.

    Left alone (for the generic removal): an exact copy of the first 240,
    one with no real $a, or one that reads as a frequency statement
    ("three no. a year" -- really a 310). A recode already present on
    the record as a 730/246 just drops the extra.

    Returns a list of detail strings, one per field handled (category
    "moved_duplicate_to_added_entry" -- see `main`)."""
    f240 = [f for f in parsed.fields if f.tag == "240" and not f.is_control()]
    if len(f240) < 2:
        return []
    first = f240[0]
    title_245 = _normalized_title("".join(
        d for f in parsed.fields if f.tag == "245" and not f.is_control()
        for c, d in f.subfields[:6] if c in "ab"
    ))
    seen = {
        _normalized_title(next((d for c, d in f.subfields if c == "a"), ""))
        for f in parsed.fields if f.tag in ("730", "246") and not f.is_control()
    }
    details = []
    new_fields = []
    gone = []
    for f in f240[1:]:
        a_val = next((d for c, d in f.subfields if c == "a" and d), "")
        if _fields_content_equal(f, first) or not _has_real_a(f) or _FREQUENCY_RE.search(a_val):
            continue
        key = _normalized_title(a_val)
        shown = f"={f.tag}  {f.indicators}{_subfield_preview(f.subfields)}"
        gone.append(f)
        if key in seen:
            details.append(f"removed duplicate {shown} (non-repeatable field) -- already "
                           "present as a 730/246 -- NO DATA LOSS")
            continue
        seen.add(key)
        if key and key in title_245:
            new = Field_("246", "30", [(c, d) for c, d in f.subfields if c in "abfghinp" and d])
        else:
            nf = _valid_nonfiling(f.indicators[1:2], a_val)
            new = Field_("730", nf + " ", [
                (c, d) for c, d in f.subfields if c in _730_FROM_240_CODES and d])
        dropped = sorted({c for c, d in f.subfields if d and c not in {x for x, _ in new.subfields}})
        new_fields.append(new)
        details.append(
            f"moved duplicate {shown} to ={new.tag}  {new.indicators}"
            f"{_subfield_preview(new.subfields)}"
            f"{' (dropped $' + ', $'.join(dropped) + ')' if dropped else ''} -- "
            f"{'POSSIBLE DATA LOSS' if dropped else 'NO DATA LOSS'}"
        )
    if details:
        parsed.fields = _place([f for f in parsed.fields if not any(f is g for g in gone)],
                               new_fields)
    return details


_TRAILING_ISBD_RE = re.compile(r"[\s/:;,.]+$")


_PUBLICATION_YEAR_RE = re.compile(r"^\s*c?\[?\s*(?:ca\.\s*)?\d{4}\]?\.?\s*$")


def _strip_isbd(text: str) -> str:
    return _TRAILING_ISBD_RE.sub("", text)


def _ascii_alnum(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.casefold())


def _is_near_duplicate_245(extra: Field_, first: Field_) -> bool:
    """True if `extra` is the first 245 again with different encoding,
    spacing or punctuation (or a corrupted tail): the same non-empty $b
    and $c, or at least 80% similar once reduced to ASCII letters/digits."""
    def text(f: Field_) -> str:
        # a stray leading "0 " is an indicator digit that leaked into $a
        return _ascii_alnum(re.sub(
            r"^0\s+", "", "".join(d for c, d in f.subfields if c in "abc")))

    def sub(f: Field_, code: str) -> str:
        return next((d for c, d in f.subfields if c == code), "")

    if sub(extra, "b") and sub(extra, "b") == sub(first, "b") and sub(extra, "c") == sub(first, "c"):
        return True
    a, b = text(extra), text(first)
    if not (a and b):
        return False
    return (
        len(os.path.commonprefix([a, b])) >= 40
        or SequenceMatcher(None, a, b).ratio() >= 0.8
    )


def recode_misfiled_duplicate_245(parsed: ParsedRecord) -> list[str]:
    """What's left of the duplicate 245s once `move_duplicate_245_to_246`
    has taken the clean variant titles. For each extra 245, the first
    that applies:
      * it is the first 245 again (see `_is_near_duplicate_245`) -- removed,
        no data lost;
      * the FIRST 245 is itself a mistagged uniform title ($l) or series
        ($v) with no $c and the extra is the real title (has $a and $c, no
        $6) -- the extra takes the first 245's place and the first is
        recoded as a 240 (or a 730 if the record has a 130 or 240), or as a
        490 `0 ` (dropped when the series is already on the record);
      * it is a publication statement ($a ending ":", $b, year-only $c) --
        recoded as a 260;
      * it is only an edition statement ("Abridged ed") -- recoded as a
        250, if the record has none;
      * it is a further volume/part title: $n/$p only (the first 245's $a
        is borrowed) or $a plus its own $c -- recoded as a 740 `x2`
        (analytical added entry; $c is dropped and flagged).
    Anything else is left for the generic removal.

    Returns a list of detail strings, one per extra 245 handled (category
    "moved_duplicate_to_added_entry" -- see `main`)."""
    f245 = [f for f in parsed.fields if f.tag == "245" and not f.is_control()]
    if len(f245) < 2:
        return []
    fields = list(parsed.fields)
    tags = {f.tag for f in fields}
    series_keys = {
        _ascii_alnum(next((d for c, d in f.subfields if c == "a"), ""))
        for f in fields if f.tag in ("490", "830") and not f.is_control()
    }
    existing_250 = [f for f in fields if f.tag == "250" and not f.is_control()]
    existing_26x = {
        _ascii_alnum(next((d for c, d in f.subfields if c == "b"), ""))
        for f in fields if f.tag in ("260", "264") and not f.is_control()
    }
    details = []
    added = []
    first = f245[0]

    def note(src: Field_, new: Field_ | None, why: str, dropped: list[str]) -> str:
        shown = f"={src.tag}  {src.indicators}{_subfield_preview(src.subfields)}"
        tail = f" (dropped ${', $'.join(dropped)})" if dropped else ""
        loss = "POSSIBLE DATA LOSS" if dropped else "NO DATA LOSS"
        if new is None:
            return f"removed duplicate {shown} (non-repeatable field) -- {why} -- {loss}"
        return (f"moved duplicate {shown} to ={new.tag}  {new.indicators}"
                f"{_subfield_preview(new.subfields)}{tail} -- {why} -- {loss}")

    for extra in f245[1:]:
        if _fields_content_equal(extra, first):
            continue
        codes = {c for c, d in extra.subfields if d}
        first_codes = {c for c, d in first.subfields if d}
        a_val = next((d for c, d in extra.subfields if c == "a" and d), "")
        b_val = next((d for c, d in extra.subfields if c == "b" and d), "")
        c_val = next((d for c, d in extra.subfields if c == "c" and d), "")
        if _is_near_duplicate_245(extra, first):
            fields = [f for f in fields if f is not extra]
            details.append(note(extra, None, "near-duplicate of the kept 245", []))
        elif "a" in codes and "c" in codes and "6" not in codes \
                and first_codes & set("lv") and "c" not in first_codes:
            ind2 = _valid_nonfiling(first.indicators[1:2], next(
                (d for c, d in first.subfields if c == "a"), ""))
            if "l" in first_codes:
                subs = [(c, d) for c, d in first.subfields if c in "adfghklmnoprs" and d]
                if tags & {"130", "240"}:
                    new = Field_("730", ind2 + " ", subs)
                else:
                    new = Field_("240", ("1" if tags & {"100", "110", "111"} else "0") + ind2,
                                 subs)
                    tags.add("240")
            else:
                subs = [(c, d) for c, d in first.subfields if c in "avx" and d]
                new = Field_("490", "0 ", subs)
            dropped = sorted({c for c, d in first.subfields if d} - {c for c, _ in new.subfields})
            key = _ascii_alnum(next((d for c, d in subs if c == "a"), ""))
            idx = next(i for i, f in enumerate(fields) if f is first)
            fields = [f for f in fields if f is not extra]
            fields[idx] = extra
            why = "second 245 is the real title and takes its place; first 245 was a mistagged "
            if new.tag == "490" and key and any(key in s or s in key for s in series_keys if s):
                details.append(note(first, None, why + "series already on the record", dropped))
            else:
                if new.tag == "490":
                    series_keys.add(key)
                added.append(new)
                details.append(note(first, new, why + ("uniform title" if "l" in first_codes
                                                       else "series"), dropped))
            first = extra
        elif codes <= {"a", "b", "c"} and {"a", "b", "c"} <= codes \
                and a_val.rstrip().endswith(":") and _PUBLICATION_YEAR_RE.match(c_val):
            year = c_val.strip().rstrip(".")
            new = Field_("260", "  ", [("a", a_val.strip()), ("b", _strip_isbd(b_val) + ","),
                                       ("c", year + ".")])
            fields = [f for f in fields if f is not extra]
            if _ascii_alnum(_strip_isbd(b_val)) in existing_26x:
                details.append(note(extra, None, "publication data already on the record", []))
            else:
                added.append(new)
                existing_26x.add(_ascii_alnum(_strip_isbd(b_val)))
                details.append(note(extra, new, "publication statement keyed as a title", []))
        elif codes == {"a"} and _EDITION_ONLY_RE.search(a_val.strip()) \
                and len(a_val.split()) <= 6 and (
                    not existing_250
                    or _ascii_alnum(a_val) == _ascii_alnum(
                        "".join(d for c, d in existing_250[0].subfields if c == "a"))):
            text = a_val.strip()
            new = Field_("250", "  ", [("a", text if text[-1] in ".]" else text + ".")])
            fields = [f for f in fields if f is not extra]
            if existing_250:
                details.append(note(extra, None, "edition already on the record as a 250", []))
            else:
                added.append(new)
                existing_250.append(new)
                details.append(note(extra, new, "edition statement keyed as a title", []))
        elif "6" not in codes and (
                (not a_val and codes and codes <= {"n", "p", "c"} and codes & {"n", "p"})
                or (a_val and "c" in codes)):
            nf = _valid_nonfiling(extra.indicators[1:2], a_val)
            subs = []
            if a_val:
                joined = a_val.strip() if not b_val else (
                    a_val.strip() + " " + _strip_isbd(b_val)
                    if a_val.rstrip().endswith(":") else _strip_isbd(a_val) + " : "
                    + _strip_isbd(b_val))
                subs.append(("a", _strip_isbd(joined) if not b_val else joined))
            else:
                first_a = _strip_isbd(next((d for c, d in first.subfields if c == "a"), ""))
                subs.append(("a", first_a + "."))
            subs += [(c, _strip_isbd(d) if c == "p" else d)
                     for c, d in extra.subfields if c in "np" and d]
            new = Field_("740", nf + "2", subs)
            fields = [f for f in fields if f is not extra]
            added.append(new)
            same_c = c_val.strip() == next((d for c, d in first.subfields if c == "c"), "").strip()
            details.append(note(extra, new, "further volume/part title",
                                [] if same_c else ["c"]))
    if details:
        parsed.fields = _place(fields, added)
    return details


def _fields_content_equal(a: Field_, b: Field_) -> bool:
    """True if `a` and `b` carry identical content -- same control
    content, or same indicators and subfields for a data field. Used
    by `strip_duplicate_non_repeatable_fields` to tell a real duplicate
    (safe to drop) from a removed field that merely shares a tag."""
    if a.is_control() or b.is_control():
        return a.is_control() == b.is_control() and (a.content or "") == (b.content or "")
    return a.indicators == b.indicators and a.subfields == b.subfields


def load_tag_list(path: str) -> set[str]:
    """Load a tag list from an external file (one tag per line; blank
    lines and lines starting with # are ignored) -- used for both
    `strip_missing_required_a` (see required_a_tags.txt) and
    `strip_duplicate_non_repeatable_fields` (see
    non_repeatable_tags.txt). Kept outside the script on purpose --
    both lists involve some judgment call, not something to hardcode."""
    tags = set()
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.split("#", 1)[0].strip()
            if line:
                tags.add(line)
    return tags


_PUNCTUATION_ONLY_A_RE = re.compile(r"^[^\w]*$", re.UNICODE)


def _is_punctuation_only(data: str) -> bool:
    """True if `data` has no letters or digits (unicode-aware), i.e. it's
    made up entirely of punctuation/symbols/whitespace -- a $a like "." or
    "--" that carries no actual content."""
    return bool(data) and bool(_PUNCTUATION_ONLY_A_RE.match(data))


_WHITESPACE_ONLY_RE = re.compile(r"^\s+$")


def strip_whitespace_only_010_a(parsed: ParsedRecord) -> list[str]:
    """Remove a 010 (LCCN) $a that's present but made up entirely of
    whitespace (e.g. a single space) -- confirmed against a live FOLIO
    Data Import run (default "Create instance and SRS MARC Bib" job
    profile) to be the one 010 $a condition that actually breaks
    import: the mapping rules apply a `trim` to $a before setting
    identifiers.value, so a whitespace-only $a trims to "" and FOLIO's
    instance schema then rejects the record with "identifiers[0].value
    must not be null" -- the whole record is discarded, not just the
    $a.

    Deliberately narrow, and deliberately does NOT touch any of these
    (each one was tested the same way and confirmed to import
    successfully as-is, so "fixing" them would just be data loss for
    no import-safety benefit):

      * No $a at all (e.g. a 010 with only $z) -- FOLIO's mapper simply
        produces no LCCN identifier; the $z (Canceled/Invalid LCCN)
        still maps fine.
      * $a entirely empty (zero-length) -- same outcome as no $a at
        all; FOLIO doesn't even treat it as a mappable occurrence.
      * $a that's punctuation-only (e.g. "." or "--") but not
        whitespace -- `trim` only strips leading/trailing whitespace,
        so this survives trim and maps to a real (if junk-looking)
        LCCN identifier. Still worth flagging for data quality
        elsewhere, but it does not break import, so it's out of scope
        here.

    Only the $a subfield itself is removed -- not the whole 010 field,
    so a $z (Canceled/Invalid LCCN) riding along in the same field is
    always preserved. If removing $a leaves the field with no
    subfields at all, the now-empty field is dropped too."""
    details = []
    kept_fields = []
    for f in parsed.fields:
        if f.is_control() or f.tag != "010":
            kept_fields.append(f)
            continue
        if not any(
            code == "a" and _WHITESPACE_ONLY_RE.match(data)
            for code, data in f.subfields
        ):
            kept_fields.append(f)
            continue
        before = "".join(f"${code}{data}" for code, data in f.subfields)
        remaining = [
            (code, data)
            for code, data in f.subfields
            if not (code == "a" and _WHITESPACE_ONLY_RE.match(data))
        ]
        if remaining:
            f.subfields = remaining
            kept_fields.append(f)
            after = "".join(f"${code}{data}" for code, data in remaining)
            details.append(
                f"removed =010  {f.indicators}{before}\t(whitespace-only $a removed; "
                f"now {f.indicators}{after})"
            )
        else:
            details.append(
                f"removed =010  {f.indicators}{before}\t(whitespace-only $a removed; "
                "nothing else left in the field, so the field itself was dropped)"
            )
    parsed.fields = kept_fields
    return details


_WHITESPACE_RUN_RE = re.compile(r"\s+")


def _normalize_010_value(value: str) -> str:
    """Strip ALL whitespace from a 010 subfield value (not just leading/
    trailing) -- used both to compare 010 $a/$z/$b values across a
    record's multiple 010 fields and as the actual stored form once
    `dedupe_010_fields` has decided which value survives. LCCNs are
    conventionally padded with leading spaces to a fixed width (e.g.
    "  2013008343"); that padding is formatting, not data, so the
    surviving value is always the stripped one."""
    return _WHITESPACE_RUN_RE.sub("", value)


#: Prefixes the LC "Structure of the LC Control Number" page
#: (https://www.loc.gov/marc/lccn_structure.html) gives for MARC records.
#: Bibliographic ones exist only in Structure A (pre-2001 numbers);
#: authority and classification ones (n, nb, ..., cf, ct) are valid in
#: both structures. Always lowercase in MARC.
_LCCN_AUTH_CLASS_PREFIXES = frozenset(
    "n nb nr no sh sj sp cf ct".split()
)


_LCCN_BIB_PREFIXES = frozenset(
    "a ac af afl agr bi br bs c ca cad cd clc cs cx cy d do e es f fi fia "
    "fie g gm gs h ha he hew hex it int j ja jx k kx l llh ltf m ma map med "
    "mic mid mie mif mm mp mpa ms mus ncn ne nex no ntc nuc or pa pho php "
    "phq po pp r ra rc re ru s sa sax sc sd sf sg sn ss su tb tmp um unk w "
    "war x z".split()
)


#: Structure A (1898-2000): prefix, 2-digit year, 6-digit serial, then
#: (bibliographic only) optional "/SUFFIX" identifiers and an optional
#: revision date: "//r74" with no suffix, "/r743" after one.
_LCCN_A_RE = re.compile(
    r"^([a-z]{0,3})(\d{2})(\d{6})(?:/[A-Z]+)*(?:/{1,2}r\d{2,3})?$"
)


#: Structure B (2001-): prefix of at most 2 letters, 4-digit year,
#: 6-digit serial, nothing after.
_LCCN_B_RE = re.compile(r"^([a-z]{0,2})(\d{4})(\d{6})$")


def _lccn_year_serial(norm: str) -> tuple[int, int] | None:
    """(year, serial) if `norm` -- a 010 $a with whitespace already
    stripped -- is a valid LCCN under the LC structure rules, else None.
    Strict: lowercase prefix, no hyphen (LC never stores it), 6-digit
    serial, and a prefix that the LC page lists for that structure. A
    2-digit year's century is decided by the serial, per the page's
    table (98 -> 18xx below 3000, 99 -> 18xx below 6000, 00 -> 19xx
    below 8000, otherwise 19xx; 00 at 8000+ is 2000)."""
    m = _LCCN_A_RE.match(norm)
    if m:
        prefix, yy, serial_s = m.groups()
        if prefix and prefix not in (
            _LCCN_BIB_PREFIXES | _LCCN_AUTH_CLASS_PREFIXES
        ):
            return None
        yy_i, serial = int(yy), int(serial_s)
        if yy_i == 98:
            year = 1898 if serial < 3000 else 1998
        elif yy_i == 99:
            year = 1899 if serial < 6000 else 1999
        elif yy_i == 0:
            year = 1900 if serial < 8000 else 2000
        else:
            year = 1900 + yy_i
        return year, serial
    m = _LCCN_B_RE.match(norm)
    if m:
        prefix, year_s, serial_s = m.groups()
        if prefix and prefix not in _LCCN_AUTH_CLASS_PREFIXES:
            return None
        year = int(year_s)
        if year < 2001:
            return None
        return year, int(serial_s)
    return None


def is_valid_lccn(value: str) -> bool:
    """True if a 010 $a value (whitespace ignored) is a valid LCCN under
    the LC structure rules -- see `_lccn_year_serial`."""
    return _lccn_year_serial(_normalize_010_value(value)) is not None


def _010_a_sort_key(norm: str) -> tuple[int, int, int]:
    """Sort key for a normalized 010 $a value, used by
    `dedupe_010_fields` to pick which of several genuinely different
    values survives as $a (the largest key wins):

      1. A valid LCCN (see `_lccn_year_serial`) always beats anything
         else -- a value that isn't a valid LCCN never takes $a from one
         that is, however new or large it looks.
      2. Among valid LCCNs, the newest year, then the highest serial.

    Among values that are not valid LCCNs, their digits read as one
    integer (-1 if there are none) just so the choice is deterministic."""
    ys = _lccn_year_serial(norm)
    if ys is None:
        digits = "".join(ch for ch in norm if ch.isdigit())
        return (0, int(digits) if digits else -1, 0)
    return (1, ys[0], ys[1])


def dedupe_010_fields(parsed: ParsedRecord) -> list[str]:
    """Collapse a record's multiple 010 (LCCN) fields -- or a single 010
    with more than one $a, which is rarer but does happen -- down to
    exactly one. MARC21 (and FOLIO) treat 010 as Not Repeatable, but
    duplicates show up in practice: almost always the same LCCN typed
    twice (sometimes with different padding), occasionally a genuinely
    different number, sometimes a $a on one occurrence and only a $z
    (Canceled/Invalid LCCN) or $b (NUCMC control number -- rare, and
    treated identically to $z here) on another.

    Deliberately its own repair rather than relying on
    `strip_duplicate_non_repeatable_fields`: that generic pass would
    just keep the first 010 and silently drop every other one whole --
    $z/$b included -- which is exactly the data this function exists to
    save. (non_repeatable_tags.txt deliberately excludes "010" for this
    reason, and this function must run before that one.)

    Must also run after `strip_whitespace_only_010_a`: a 010 $a that's
    nothing but whitespace needs to already be gone before this looks
    at what's left on the field.

    Rules:

      * Every $a across all of the record's 010 fields (including more
        than one within the same field) is compared with its whitespace
        fully stripped. If they all normalize the same, that's NO DATA
        LOSS -- only formatting differed -- and the stripped value is
        what's kept.
      * If two or more $a values normalize to genuinely different
        values, the best one (see `_010_a_sort_key`: normal-looking
        LCCN first, then newest year) is kept as $a and every other one is moved to a $z (nothing is
        lost, though a $z asserts "canceled/invalid", which hasn't been
        verified for these). Always logged in detail naming every
        value moved.
      * Every $z and $b from every 010 on the record is carried onto
        the single surviving field, each normalized the same way and
        de-duplicated (separately per code) -- so a $z or $b is never
        lost just because it happened to live on a different 010 than
        the $a that survived, or because another 010 had no $a at all.
        The exception is a $z that normalizes to the same value as the
        surviving $a: a Canceled/Invalid LCCN identical to the LCCN
        that's actually being kept is a contradiction, not useful
        data, so that $z is dropped too. ($b -- NUCMC control number --
        isn't an LCCN-cancellation marker, so it's kept even if it
        happens to match $a.)

    A record with only one 010, carrying only one $a, is untouched
    (the common case -- nothing to merge) unless that same field also
    carries a $z matching $a, which is still dropped as a
    contradiction."""
    tag_fields = [f for f in parsed.fields if f.tag == "010" and not f.is_control()]
    if not tag_fields:
        return []
    total_a = sum(1 for f in tag_fields for code, _ in f.subfields if code == "a")
    if len(tag_fields) == 1 and total_a <= 1:
        only_field = tag_fields[0]
        only_a = next((d for c, d in only_field.subfields if c == "a"), None)
        only_a_norm = _normalize_010_value(only_a) if only_a is not None else None
        has_z_matching_a = only_a_norm is not None and any(
            c == "z" and _normalize_010_value(d) == only_a_norm
            for c, d in only_field.subfields
        )
        if not has_z_matching_a:
            return []

    kept_field = tag_fields[0]
    tag_field_ids = {id(f) for f in tag_fields}

    a_values: list[str] = []
    zb_values: list[tuple[str, str]] = []
    for f in tag_fields:
        for code, data in f.subfields:
            if code == "a":
                a_values.append(data)
            elif code in ("z", "b"):
                zb_values.append((code, data))

    distinct_a_norms: list[str] = []
    for raw in a_values:
        norm = _normalize_010_value(raw)
        if norm not in distinct_a_norms:
            distinct_a_norms.append(norm)

    if distinct_a_norms:
        surviving_a = max(distinct_a_norms, key=_010_a_sort_key)
        dropped_a_norms = [n for n in distinct_a_norms if n != surviving_a]
    else:
        surviving_a = None
        dropped_a_norms = []

    seen_zb: dict[str, set[str]] = {"z": set(), "b": set()}
    kept_zb: list[tuple[str, str]] = []
    for code, raw in zb_values:
        norm = _normalize_010_value(raw)
        if code == "z" and norm == surviving_a:
            continue
        if norm in seen_zb[code]:
            continue
        seen_zb[code].add(norm)
        kept_zb.append((code, norm))

    # Every non-surviving $a is moved to $z rather than discarded, so no
    # LCCN is ever lost (appended after the existing $z/$b, de-duplicated
    # against the $z values already kept).
    for norm in dropped_a_norms:
        if norm not in seen_zb["z"]:
            seen_zb["z"].add(norm)
            kept_zb.append(("z", norm))

    new_subfields = ([("a", surviving_a)] if surviving_a is not None else []) + kept_zb

    before = "; ".join(
        f.indicators + "".join(f"${c}{d}" for c, d in f.subfields) for f in tag_fields
    )
    after = "".join(f"${c}{d}" for c, d in new_subfields)

    if dropped_a_norms:
        detail = (
            f"merged {len(tag_fields)} =010 field(s) [{before}] into one "
            f"({kept_field.indicators}{after}) -- kept $a{surviving_a}, "
            f"moved {', '.join(f'$a{a}' for a in dropped_a_norms)} to $z "
            "(different LCCN, status unverified) -- all values retained"
        )
    else:
        detail = (
            f"merged {len(tag_fields)} =010 field(s) [{before}] into one "
            f"({kept_field.indicators}{after}) -- NO DATA LOSS"
        )

    merged_field = Field_(tag="010", indicators=kept_field.indicators, subfields=new_subfields)

    new_fields: list[Field_] = []
    inserted = False
    for f in parsed.fields:
        if id(f) in tag_field_ids:
            if not inserted:
                new_fields.append(merged_field)
                inserted = True
            continue
        new_fields.append(f)
    parsed.fields = new_fields

    return [detail]


#: 880 (Alternate Graphic Representation) run of `strip_missing_required_a`
#: on its own (unlike 010 above, this one still uses the general
#: missing/punctuation-only-$a rule -- an 880 has no $z-style fallback
#: content worth preserving) -- see `main`, which logs it under its own
#: category ("removed_880_missing_a") rather
#: than the shared "field_removed_because_missing_a" one. An 880 exists
#: solely to carry a linked vernacular/alternate-script representation of
#: another field; its only other subfields ($6 linking data, $0-$8
#: control subfields) never carry the actual heading content itself, so a
#: blank/punctuation-only $a here means the vernacular text was never
#: populated at all -- confirmed in real production data (WTS_bibs
#: 2026-10-01): two records each had an 880 linked to their 100 and
#: explicitly flagged as Greek script (066/$6 both say so), with $a
#: containing nothing but whitespace. Definite DATA LOSS, not just
#: POSSIBLE.
_880_REQUIRED_A_TAGS = frozenset({"880"})


#: Longest run of a field's own text a log line may quote before it is
#: cut short with "..." -- a whole field (a long 505, a 700 $t that is a
#: full transcribed title) makes the log unreadable, and the first 40
#: characters are plenty to find the field again in the record.
_LOG_PREVIEW_CHARS = 40


def _log_preview(text: str) -> str:
    """`text` cut to its first `_LOG_PREVIEW_CHARS` characters (plus
    "..." if anything was cut), for quoting field content in a log line."""
    if len(text) <= _LOG_PREVIEW_CHARS:
        return text
    return text[:_LOG_PREVIEW_CHARS] + "..."


def _subfield_preview(subfields: list[tuple[str, str]]) -> str:
    """The "$a...$b..." text of `subfields`, cut by `_log_preview`."""
    return _log_preview("".join(f"${code}{data}" for code, data in subfields))


def strip_missing_required_a(
    parsed: ParsedRecord,
    required_a_tags: set[str],
    valid_codes: frozenset[str] = frozenset({"a"}),
) -> list[str]:
    """Remove data fields whose tag is in `required_a_tags` (see
    required_a_tags.txt) but that lack a non-empty, non-punctuation-only $a
    subfield, which is required there. A $a whose content is nothing but
    punctuation (e.g. "." or "--") carries no real data and is treated the
    same as a missing $a.

    `valid_codes` -- normally just {"a"} -- lets a caller widen this to a
    small set of interchangeable codes for the rare tag where $a has a
    recognized alternate: e.g. holdings 853/854/855 (Captions and
    Pattern), which traditionally require $a (or its alternate $g) but
    also allow $i alone for a chronology-only pattern with no
    enumeration captions at all (see `repair_holdings_records`, which
    passes {"a", "g", "i"} for exactly these three tags).

    A field that's entirely empty (no non-empty subfield at all, e.g. a bare
    "$a" with nothing after it and nothing else in the field) is removed
    silently -- there's nothing to lose. A field that has some other
    non-empty subfield data (including a punctuation-only $a) is also
    removed (per the same missing-required-$a rule) but is NOT silent: a
    line describing exactly what was discarded is returned (category
    "field_removed_because_missing_a" -- see `main`) so the caller can log it before the
    content is gone for good.

    For an 880 specifically, the logged line only shows $6 and $a (the
    subfields that actually matter for diagnosing a missing/punctuation-only
    $a) plus 20 characters of whatever else the field held, not the full
    (sometimes very long) vernacular-script body. For every tag, the
    quoted field text is cut to its first 40 characters (see
    `_log_preview`).
    """
    details = []
    kept = []
    code_label = "/".join(f"${c}" for c in sorted(valid_codes))
    for f in parsed.fields:
        if f.is_control() or f.tag not in required_a_tags:
            kept.append(f)
            continue
        if any(
            code in valid_codes and data and not _is_punctuation_only(data)
            for code, data in f.subfields
        ):
            kept.append(f)
            continue
        if any(data for code, data in f.subfields):
            if f.tag == "880":
                body = "".join(
                    f"${code}{data}" for code, data in f.subfields if code in ("6", "a")
                )
                rest = "".join(
                    f"${code}{data}"
                    for code, data in f.subfields
                    if code not in ("6", "a")
                )
                if rest:
                    ellipsis = "..." if len(rest) > 20 else ""
                    body += f" [{rest[:20]}{ellipsis}]"
            else:
                body = "".join(f"${code}{data}" for code, data in f.subfields)
            body = _log_preview(body)
            reason = (
                f"{code_label} is punctuation only"
                if any(code in valid_codes and data for code, data in f.subfields)
                else f"missing required {code_label}"
            )
            details.append(
                f"removed ={f.tag}  {f.indicators}{body}\t({reason}; "
                "content discarded)"
            )
        # else: field was entirely empty -- drop it without logging
    parsed.fields = kept
    return details


def _has_real_a(f: Field_) -> bool:
    """True if `f` has a $a with content that isn't punctuation only."""
    return any(
        code == "a" and data and not _is_punctuation_only(data)
        for code, data in f.subfields
    )


#: Tags `recode_title_only_7xx_to_730` looks at: a name-title added entry
#: whose name was never entered.
_TITLE_ONLY_7XX_TAGS = frozenset({"700", "710", "711"})


#: Subfields that mean the same thing in a 730 as in the 7XX name-title
#: field they came from -- the title-of-work subfields plus the
#: control/link ones. Deliberately excludes everything that describes the
#: (missing) name itself ($b $c $d $e $j $q $u $4, ...), and $6 (its
#: "700-01" occurrence number names the old tag, and the paired 880 would
#: need rewriting too).
_730_CODES = frozenset("tfghiklmnoprsx012358")


#: Leading English articles skipped in filing, as 730 indicator 1.
_ENGLISH_ARTICLES = ("the ", "an ", "a ")


def _nonfiling_count(title: str, english: bool) -> int:
    """Characters of a leading English article ("The ", "An ", "A ")
    in `title` that a 730 should skip in filing -- 0 unless the record's
    language is English (an "A " or "An " could be a word in another
    language)."""
    if not english:
        return 0
    low = title.lower()
    for article in _ENGLISH_ARTICLES:
        if low.startswith(article) and len(title) > len(article):
            return len(article)
    return 0


def recode_title_only_7xx_to_730(parsed: ParsedRecord) -> list[str]:
    """Recode a 700/710/711 that has a $t (title of a work) but no real $a
    (name) as a 730 (Added Entry--Uniform Title). Such a field is invalid
    as a name-title entry, and `strip_missing_required_a` would otherwise
    remove it, losing the title -- seen in the wild on bound collections of
    pamphlets, where the cataloger entered an analytical title for an
    anonymous work in the same 700 pattern as its attributed siblings.

    Only done when the field has exactly one non-empty $t and every other
    non-empty subfield is one a 730 also carries (`_730_CODES`, e.g. $f
    date of work); otherwise the field is left alone for
    `strip_missing_required_a`. A punctuation-only or empty $a is dropped
    in the conversion. The $t becomes the 730's $a; indicator 1 is the
    nonfiling count (leading English article, if the record is
    English -- 008/35-37 "eng"), indicator 2 is 2 (analytical entry).
    The new 730 is placed after any existing 730s, ahead of the next
    higher tag, the converted fields keeping their relative order.

    Returns a list of detail strings, one per field recoded (category
    "recoded_title_only_7xx_to_730" -- see `main`)."""
    english = any(
        f.tag == "008" and f.is_control() and (f.content or "")[35:38] == "eng"
        for f in parsed.fields
    )
    details = []
    kept = []
    converted = []
    for f in parsed.fields:
        if f.is_control() or f.tag not in _TITLE_ONLY_7XX_TAGS or _has_real_a(f):
            kept.append(f)
            continue
        subs = [(code, data) for code, data in f.subfields if code != "a" and data]
        titles = [data for code, data in subs if code == "t"]
        if (
            len(titles) != 1
            or _is_punctuation_only(titles[0])
            or any(code not in _730_CODES for code, data in subs)
        ):
            kept.append(f)
            continue
        new_subs = [("a" if code == "t" else code, data) for code, data in subs]
        new = Field_(
            "730", f"{_nonfiling_count(titles[0], english)}2", new_subs
        )
        converted.append(new)
        details.append(
            f"recoded ={f.tag}  {f.indicators}{_subfield_preview(subs)} to "
            f"={new.tag}  {new.indicators}{_subfield_preview(new_subs)}"
        )
    if converted:
        at = next((i for i, f in enumerate(kept) if f.tag > "730"), len(kept))
        parsed.fields = kept[:at] + converted + kept[at:]
    return details


#: Tags `recode_6xx_b_to_a` looks at: topical/geographic/genre headings,
#: where $b is obsolete (it once held the term following a place-name
#: entry element) -- unlike 600/610/611, where $b is a real, different
#: part of the name.
_B_TO_A_6XX_TAGS = frozenset({"650", "651", "655"})


def recode_6xx_b_to_a(parsed: ParsedRecord) -> list[str]:
    """Recode a 650/651/655 that has no real $a, but starts with a single
    $b, so that $b becomes the $a -- the heading term was simply
    mis-coded (e.g. `650 0 $bPhilosophy$vEarly works to 1800`), and
    `strip_missing_required_a` would otherwise remove the whole field.
    A punctuation-only or empty $a is dropped in the conversion. Returns a
    list of detail strings, one per field recoded (category
    "recoded_6xx_b_to_a" -- see `main`)."""
    details = []
    for f in parsed.fields:
        if f.is_control() or f.tag not in _B_TO_A_6XX_TAGS or _has_real_a(f):
            continue
        subs = [(code, data) for code, data in f.subfields if code != "a" and data]
        if (
            not subs
            or subs[0][0] != "b"
            or _is_punctuation_only(subs[0][1])
            or sum(1 for code, _ in subs if code == "b") != 1
        ):
            continue
        new_subs = [("a", subs[0][1])] + subs[1:]
        details.append(
            f"={f.tag}  {f.indicators}: $b recoded to $a -- "
            f"{_subfield_preview(new_subs)}"
        )
        f.subfields = new_subs
    return details


def fix_misplaced_subfield_codes(parsed: ParsedRecord) -> list[str]:
    """Fix a subfield whose code is a single stray space, immediately
    followed by what's unmistakably the REAL, intended code -- e.g. raw
    bytes "\\x1f c2000." parse (correctly, by the letter of the format)
    as code=" ", data="c2000.", when the actual intended subfield was
    $c "c2000." (a common AACR2-era copyright-date convention: "c" is
    literal data, not the code, but here one extra space got inserted
    between the delimiter and its real code). A real, high-volume defect
    in production data -- 199 of 203 removed_invalid_subfield hits in one
    71.6MB file were exactly this shape -- and unlike most invalid-code
    cases, this one isn't a guess: the intended code is simply the very
    next character, still physically present, so recovering it discards
    nothing (not even the stray space, which was never real data to
    begin with). Must run before `strip_invalid_subfield_codes`, which
    would otherwise discard every one of these outright. Returns a list
    of detail strings, one per subfield fixed (category
    "fixed_misplaced_subfield_code" -- see `main`)."""
    details = []
    for f in parsed.fields:
        if f.is_control():
            continue
        new_subfields = []
        for code, data in f.subfields:
            if code == " " and data and data[0] in VALID_SUBFIELD_CODE_CHARS:
                real_code, real_data = data[0], data[1:]
                details.append(
                    f"={f.tag}: subfield code was a stray space before "
                    f"${real_code} -- corrected to ${real_code}{real_data!r}"
                )
                new_subfields.append((real_code, real_data))
            else:
                new_subfields.append((code, data))
        f.subfields = new_subfields
    return details


def strip_invalid_subfield_codes(parsed: ParsedRecord) -> list[str]:
    """Remove subfields whose code isn't a lowercase letter or digit (see
    `VALID_SUBFIELD_CODE_CHARS`) -- a code that isn't one of those is
    unusable data (there's no way to know what it was supposed to be), so
    unlike `strip_missing_required_a` there's no "keep the field" branch:
    just the bad subfield is dropped. Each removal is returned as a detail
    string (category "removed_invalid_subfield" -- see `main`) since it
    discards whatever data it held. A field left with zero subfields
    afterward is removed entirely (silently, matching
    `strip_missing_required_a`'s "nothing left to keep" rule)."""
    details = []
    kept_fields = []
    for f in parsed.fields:
        if f.is_control():
            kept_fields.append(f)
            continue
        good = [(c, d) for c, d in f.subfields if c in VALID_SUBFIELD_CODE_CHARS]
        bad = [(c, d) for c, d in f.subfields if c not in VALID_SUBFIELD_CODE_CHARS]
        if bad:
            removed = "".join(f"${c}{d}" for c, d in bad)
            details.append(
                f"removed invalid subfield(s) {removed!r} from ={f.tag}"
                "\t(invalid subfield code; content discarded)"
            )
        if good:
            f.subfields = good
            kept_fields.append(f)
        # else: nothing valid left in this field -- drop it entirely
    parsed.fields = kept_fields
    return details


def strip_empty_fields(parsed: ParsedRecord) -> None:
    """Remove data fields where every subfield's data is empty (e.g. a
    bare "$a" with nothing after it and nothing else in the field) --
    regardless of tag, unlike `strip_missing_required_a` which only checks
    tags known to require $a specifically. This is unconditional and
    silent (no return value): a field with zero non-empty subfields holds
    no information under any tag, so removing it never discards anything,
    the same "nothing to lose" case `strip_missing_required_a` and
    `strip_invalid_subfield_codes` already drop silently when they empty a
    field out via their own tag/code-scoped rules."""
    parsed.fields = [
        f for f in parsed.fields
        if f.is_control() or any(data for code, data in f.subfields)
    ]


def strip_null_identifiers(parsed: ParsedRecord) -> list[str]:
    """Remove any subfield, on any field, that has no data at all (a
    "null identifier") -- e.g. a bare $8 with nothing after it, or an
    empty $a immediately followed by another subfield (seen in the
    wild in 035 fields as `$a$0<local number>`, where whatever
    produced the file split a single value across two subfields and
    left the first one empty). Used by both `main`'s default bib
    pipeline and `repair_holdings_records` as the general catch-all
    for every field -- on the holdings side, OTHER than 852
    (Location): that one already has its own, more specific fixes for
    the same underlying problem (`strip_empty_852_subfields`,
    `fix_852_call_number`'s $h handling) that run first and leave
    nothing empty behind for this to find. Returns one detail string
    per subfield removed (category "removed_null_identifier" -- see
    `repair_holdings_records` and `main`).
    """
    details = []
    for f in parsed.fields:
        if f.is_control():
            continue
        removed_codes = [code for code, data in f.subfields if not data]
        if not removed_codes:
            continue
        f.subfields = [(code, data) for code, data in f.subfields if data]
        for code in removed_codes:
            details.append(f"={f.tag}: removed subfield ${code} (null identifier, had no data)")
    return details


def normalize_subfield_9_to_0(parsed: ParsedRecord) -> list[str]:
    """Rewrite every $9 subfield code to $0, unconditionally, in every data
    field. $0 is MARC21's standard subfield for an authority record control
    number/URI; $9 is formally reserved for locally-defined data, but a
    long-standing convention in widely-used cleanup tooling (this mirrors
    a specific site's battle-tested "marcfix" script) treats $9 as that
    same link stored under a legacy/local code and unconditionally
    promotes it to the standard one. Because $9 is *reserved* rather than
    standardized, a $9 could in principle mean something unrelated in some
    other system's data -- run by default per that script's convention,
    and --no-normalize-subfield-9 is available to skip it. Returns one
    detail string per field changed (category "normalized_subfield_9_to_0"
    if the caller logs them); the CLI always logs a header + count for
    this, but doesn't list every record by default (see --log-full) --
    a file that uses $9 at all often has it on nearly every record.
    """
    details = []
    for f in parsed.fields:
        if f.is_control():
            continue
        if not any(code == "9" for code, _ in f.subfields):
            continue
        f.subfields = [("0" if code == "9" else code, data) for code, data in f.subfields]
        details.append(f"normalized $9 -> $0 in ={f.tag}")
    return details


def remap_999_to_945(parsed: ParsedRecord) -> list[str]:
    """Retag every 999 data field to 945, forcing its indicators to "ff".
    999 is not part of MARC21 at all -- it's Sierra's own internal
    item-linking field, meaningless (and often rejected outright) outside
    that system -- while 945 is a commonly-used locally-defined field for
    the same purpose that other systems (e.g. OCLC) will actually accept.
    Subfield content and order are left untouched -- only the tag and
    indicators change. Returns one detail string per field remapped
    (category "remapped_999_to_945" if the caller logs them); the CLI
    always logs a header + count for this, but doesn't list every
    record by default (see --log-full) -- a single record can carry
    many 999s (one per item copy) and listing each would dominate the
    log.
    """
    details = []
    for f in parsed.fields:
        if f.is_control() or f.tag != "999":
            continue
        f.tag = "945"
        f.indicators = "ff"
        details.append("remapped =999 to =945, indicators set to 'ff'")
    return details


#: MARC21 tags are always 3 numeric digits; 900-999 is the block reserved
#: for locally-defined fields, so it's the natural place to park a tag
#: that's genuinely garbage (e.g. "ABC" from directory corruption) --
#: better than leaving a record with an unparseable tag, but still
#: clearly marked as "not a real MARC21 tag" by construction. See
#: `pick_unused_9xx_tag`.
_9XX_CANDIDATES = [str(n) for n in range(900, 1000)]


def pick_unused_9xx_tag(used_tags: set[str]) -> str | None:
    """First tag in 900-999 not present in `used_tags`, or None if all 100
    are somehow already in use (a caller should fall back to stripping
    the invalid-tag field(s) out entirely in that case -- see
    `strip_invalid_tags` -- since FOLIO can't load a non-numeric tag and
    there's nowhere left to rename it to)."""
    for candidate in _9XX_CANDIDATES:
        if candidate not in used_tags:
            return candidate
    return None


def fix_invalid_tags(parsed: ParsedRecord, replacement_tag: str) -> list[str]:
    """Rename every field whose tag isn't 3 numeric digits (e.g. "ABC"
    from directory corruption -- MARC21 tags are always numeric) to
    `replacement_tag`. Indicators/subfields/content are left untouched --
    only the tag changes. Logged (category "invalid_tag") since the
    replacement tag is a placeholder, not recovered from the record's own
    data; see `pick_unused_9xx_tag` for how it's chosen, and `main`'s
    deferred-fix pass for how the file's used tags are gathered without
    a separate full-file scan.
    """
    details = []
    for f in parsed.fields:
        if not f.tag.isdigit():
            details.append(f"Invalid tag {f.tag!r} renamed to ={replacement_tag}")
            f.tag = replacement_tag
    return details


def strip_invalid_tags(parsed: ParsedRecord) -> list[str]:
    """Remove every field whose tag isn't 3 numeric digits outright --
    the fallback for when `fix_invalid_tags` has nowhere left to rename
    one to (every 900-999 tag already used elsewhere in the file; see
    `pick_unused_9xx_tag`). FOLIO (like any strict MARC21 importer)
    can't load a non-numeric tag at all, so leaving it in place isn't
    actually safer than discarding it -- this is real content loss
    (category "unfixed_non_numeric_tag", NEEDS REVIEW,
    POSSIBLE DATA LOSS: this is the one path where the field's own
    content, not just its tag, is gone for good; logged with the full
    removed field so a human can decide whether it needed to go
    somewhere else instead). Unlike a rename, this changes the record's
    total assembled length, so a caller can't patch it in place the way
    `fix_invalid_tags` is -- see `main`'s deferred-fix pass for the
    full-file rewrite this actually requires.
    """
    details = []
    kept = []
    for f in parsed.fields:
        if f.tag.isdigit():
            kept.append(f)
            continue
        if f.is_control():
            body = f.content or ""
        else:
            body = f.indicators + "".join(f"${c}{d}" for c, d in f.subfields)
        details.append(
            f"removed =~{f.tag}  {body}\t(invalid tag, no free 9XX slot to rename to; "
            "content discarded)"
        )
    parsed.fields = kept
    return details


def _rewrite_stripping_invalid_tags(
    out_path: str,
    pending: list[tuple[int, int, int, str]],
    log_entries: list["LogEntry"],
    fix_ts: str,
) -> None:
    """Full second pass over an already-written output file, for the
    rare case where every 900-999 tag is already taken and an invalid
    (non-numeric) tag can't be renamed out of the way -- see
    `pick_unused_9xx_tag`, and `main`'s/`repair_holdings_records`'s own
    deferred tag-fix passes, both of which call this. Unlike a rename
    (`fix_invalid_tags`, patched in place at each record's known byte
    offset), stripping the field (`strip_invalid_tags`) changes the
    record's byte length, so it can't be patched in place the same way
    -- every later record's offset in the file would shift too.
    Instead, every record in `out_path` is re-split on its record
    terminator (RECTERM, `\\x1d`) -- NOT trusted by its own leader-
    declared 5-byte length, which is a lie (the documented `99999`
    sentinel) for any record `oversized_sentinel_fixed` already caught
    -- and written to a temporary file: unchanged for every record
    whose index isn't in `pending`, with its invalid-tag field(s)
    stripped and the record reassembled for the ones that are. The
    temp file then atomically replaces `out_path`.

    `pending` is `(record_idx, offset, length, rec_encoding)` tuples --
    same shape the caller already built for the in-place rename path;
    only `record_idx` and `rec_encoding` are used here, since offsets
    into the *original* file are meaningless once record lengths start
    changing.
    """
    encodings = {record_idx: rec_encoding for record_idx, _, _, rec_encoding in pending}
    with open(out_path, "rb") as src:
        data = src.read()
    records_raw = data.split(b"\x1d")
    if records_raw and records_raw[-1] == b"":
        records_raw.pop()
    tmp_path = out_path + ".tagstrip.tmp"
    with open(tmp_path, "wb") as dst:
        for record_idx, body in enumerate(records_raw):
            raw = body + b"\x1d"
            rec_encoding = encodings.get(record_idx)
            if rec_encoding is None:
                dst.write(raw)
            else:
                rec = read_intact_record(raw.decode(rec_encoding))
                rec_id = record_identifier(rec)
                for detail in strip_invalid_tags(rec):
                    log_entries.append(
                        LogEntry(
                            "unfixed_non_numeric_tag", True, fix_ts, record_idx, rec_id, detail,
                        )
                    )
                dst.write(assemble_marc(rec))
    os.replace(tmp_path, out_path)


#: Typographic "smart" Unicode punctuation -> plain ASCII equivalent. These
#: characters are perfectly valid Unicode and not a structural defect on
#: their own (a record correctly declaring UTF-8 can contain them with no
#: problem), but some downstream MARC tooling flags them as suspect, and
#: some legacy/MARC-8-oriented systems mis-render them regardless of a
#: correct UTF-8 declaration.
SMART_CHAR_MAP: dict[str, str] = {
    "‘": "'", "’": "'", "‚": ",", "‛": "'",  # ' ' ‚ ‛
    "“": '"', "”": '"', "„": '"', "‟": '"',  # " " „ ‟
    "–": "-", "—": "--",  # – —
    "‐": "-", "‑": "-", "‒": "-",  # ‐ ‑ ‒
    "…": "...",  # …
    "′": "'", "″": '"',  # ′ ″
    " ": " ",  # non-breaking space
    "­": "",  # soft hyphen
}


#: `str.translate`-compatible table built once from SMART_CHAR_MAP. This
#: runs the substitution in C rather than the Python-level per-character
#: loop `_replace_smart_chars` used to do -- profiling a full run against
#: a real 91MB/48k-record file showed that loop alone (called on every
#: field's content, even fields with no smart characters at all) was
#: responsible for over 40% of total run time.
_SMART_CHAR_TABLE = str.maketrans(SMART_CHAR_MAP)


def _replace_smart_chars(text: str) -> tuple[str, int]:
    # Every SMART_CHAR_MAP key is non-ASCII, so ASCII text can never
    # match -- `isascii()` is an O(1) check on CPython's compact string
    # representation (it already knows its own max codepoint), while
    # `translate()`/`count()` are full O(len) scans. Real-world MARC
    # text is overwhelmingly ASCII, so this short-circuit skips those
    # scans for nearly every field instead of running them just to
    # discover nothing changed.
    if text.isascii():
        return text, 0
    new_text = text.translate(_SMART_CHAR_TABLE)
    if new_text == text:
        return text, 0
    count = sum(text.count(ch) for ch in SMART_CHAR_MAP)
    return new_text, count


def normalize_smart_characters(parsed: ParsedRecord) -> list[str]:
    """Replace typographic "smart" Unicode punctuation (see
    `SMART_CHAR_MAP`) with its plain-ASCII equivalent, in every subfield and
    control field. Runs by default; pass --no-normalize-smart-characters to
    skip it (some catalogers deliberately prefer proper typographic
    quotes/dashes in note fields, so leaving them alone needs to stay
    possible even though the default favors plain ASCII)."""
    details = []
    for f in parsed.fields:
        if f.is_control():
            if f.content:
                new_content, count = _replace_smart_chars(f.content)
                if count:
                    f.content = new_content
                    details.append(f"normalized {count} smart character(s) in ={f.tag}")
            continue
        total_count = 0
        codes_touched: list[str] = []
        new_subfields = []
        for code, data in f.subfields:
            new_data, count = _replace_smart_chars(data)
            if count:
                total_count += count
                codes_touched.append(code)
            new_subfields.append((code, new_data))
        if total_count:
            f.subfields = new_subfields
            codes = ",".join(f"${c}" for c in codes_touched)
            details.append(f"normalized {total_count} smart character(s) in ={f.tag} {codes}")
    return details


#: Telltale characters produced when correctly-decoded UTF-8 text gets
#: re-interpreted a second time as Latin-1/Windows-1252 and re-encoded
#: (a common real-world corruption from Excel/CSV round-trips and some
#: legacy export pipelines) -- e.g. "e" (U+00E9, 2 UTF-8 bytes C3 A9)
#: becomes "Ã©" once those 2 bytes are misread as 2 separate Latin-1
#: characters. "Ã" (U+00C3) is by far the most common marker since it's
#: the lead byte for most accented Latin letters' UTF-8 encoding; "Â"
#: and "â€" cover the rest of the common cases (nbsp/symbols, smart
#: quotes/dashes). See `_fix_mojibake`.
_MOJIBAKE_MARKERS = ("Ã", "Â", "â€")


def _fix_mojibake(text: str) -> str | None:
    """Return the corrected text if `text` looks like it was UTF-8 that
    got double-encoded, or None if not (leave it alone). Re-encoding as
    cp1252 (Windows-1252) recovers the original bytes 1:1 -- cp1252
    rather than strict latin-1, since real-world mojibake overwhelmingly
    comes from Windows "ANSI" tools (Excel, legacy exports) that use
    cp1252's byte assignments for 0x80-0x9F (e.g. byte 0x9F is 'Y with
    diaeresis' in cp1252 vs. an unprintable C1 control code in strict
    latin-1); cp1252 is identical to latin-1 everywhere else, so this
    is strictly more capable, never less. Decoding those bytes as
    UTF-8 only succeeds if they were genuinely valid UTF-8 to begin
    with -- an essentially impossible coincidence for text that wasn't
    actually double-encoded, so a successful round-trip is strong
    confirmation, not a guess."""
    # Same reasoning as _replace_smart_chars: every marker is non-ASCII,
    # so this O(1) check skips the substring scans (and the encode/decode
    # round-trip below) for ordinary ASCII text, which is most of it.
    if text.isascii():
        return None
    if not any(marker in text for marker in _MOJIBAKE_MARKERS):
        return None
    try:
        candidate = text.encode("cp1252").decode("utf-8")
    except (UnicodeDecodeError, UnicodeEncodeError):
        return None
    return candidate if candidate != text else None


def find_and_fix_mojibake(parsed: ParsedRecord) -> list[str]:
    """Fix double-encoded UTF-8 (see `_fix_mojibake`) in every subfield
    and control field. Only meaningful for text that's actually meant
    to be UTF-8 already -- skipped entirely for a record still
    declaring MARC-8, where the same marker bytes can legitimately
    appear as raw ANSEL diacritic codes and "fixing" them would corrupt
    real content. Logged (category "fixed_mojibake") since it changes
    real content, but this is a genuine, verified recovery of the
    record's own original text (not a placeholder), so it's an
    ordinary FIXED entry, not FIXED/REQUIRES ATTENTION.
    """
    if parsed.leader[9:10] != UNICODE_ENCODING_BYTE:
        return []
    details = []
    for f in parsed.fields:
        if f.is_control():
            if f.content:
                fixed = _fix_mojibake(f.content)
                if fixed is not None:
                    details.append(
                        f"fixed double-encoded UTF-8 in ={f.tag}: {f.content!r} -> {fixed!r}"
                    )
                    f.content = fixed
        else:
            new_subfields = []
            changed_codes = []
            for code, data in f.subfields:
                fixed = _fix_mojibake(data)
                if fixed is not None:
                    changed_codes.append(code)
                    new_subfields.append((code, fixed))
                else:
                    new_subfields.append((code, data))
            if changed_codes:
                f.subfields = new_subfields
                codes = ",".join(f"${c}" for c in changed_codes)
                details.append(f"fixed double-encoded UTF-8 in ={f.tag} {codes}")
    return details
