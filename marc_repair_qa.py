"""QA checks for marc_repair: flag suspicious content, identifier helpers and the
duplicate-identifier check. Imports only marc_repair_common (it sits below marc8,
which uses its identifier helpers)."""

from __future__ import annotations

import re

from marc_repair_common import (
    LogEntry,
    ParsedRecord,
)


# ---------------------------------------------------------------------------
# QA: flag (but don't fix) suspicious content
# ---------------------------------------------------------------------------

_DOUBLED_PROXY_PREFIX = re.compile(r"(https?://[^?]+\?url=)\1")


VALID_SUBFIELD_CODE_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789")


# MARC21 leader byte validity sets (per LC's leader documentation:
# https://www.loc.gov/marc/bibliographic/bdleader.html, and the equivalent
# authority/holdings leader pages -- the codes differ slightly by record
# format, so byte 06's set below is the *union* across bib/authority/
# holdings, since which format a given record is meant to be is exactly
# what this check can't assume). See `fix_invalid_leader_bytes` for the
# defaults applied to bytes 05/06/08/17, and `fix_invalid_bibliographic_
# level` for byte 07's own default (kept separate since it's logged
# under its own category, not lumped into "leader_byte_defaulted").
LEADER_05_RECORD_STATUS_VALID = set("acdnp")


LEADER_06_TYPE_OF_RECORD_VALID = set("acdefgijkmoprtzuvxy")


LEADER_07_BIBLIOGRAPHIC_LEVEL_VALID = set("abcdims")


LEADER_08_TYPE_OF_CONTROL_VALID = set("a ")


LEADER_17_ENCODING_LEVEL_VALID = set("12345 78uz")


#: MARC21 Holdings format's OWN byte 17 (Encoding level) code set --
#: unlike the permissive union above, this is holdings-specific:
#: https://www.loc.gov/marc/holdings/hdleader.html documents only
#: 1/2/3/4/5/m/u/z for holdings -- blank ("full level") is a
#: *bibliographic* code, not a defined holdings one, and holdings adds
#: 'm' (Mixed level), which the union set above doesn't include at
#: all. See `repair_holdings_records`, which passes this (and 'u' as
#: the correction default, not the union's blank) to
#: `fix_invalid_leader_bytes` instead of the shared bib-oriented set.
LEADER_17_ENCODING_LEVEL_VALID_HOLDINGS = set("12345muz")


#: A data field's indicators are almost universally either a digit
#: (0-9) or blank across the whole MARC21 Bibliographic format -- this
#: is a broad, format-wide rule rather than a per-tag table, so it
#: generalizes across any file without needing an exhaustive per-field
#: indicator-value reference. See `find_invalid_indicator_values`.
VALID_INDICATOR_CHARS = set("0123456789 ")


def fix_invalid_leader_bytes(
    parsed: ParsedRecord,
    type_of_record_default: str = "a",
    encoding_level_valid: set[str] = LEADER_17_ENCODING_LEVEL_VALID,
    encoding_level_default: str = " ",
) -> list[str]:
    """Default four leader bytes to a known-valid value when they hold
    something outside their valid MARC21 code set (see the
    LEADER_*_VALID sets above), mirroring a widely-used site cleanup
    script's leader-repair logic:

      * byte 05 (Record status) -> 'c' (Corrected or revised)
      * byte 06 (Type of record) -> `type_of_record_default` ('a',
        Language material, for a bib file; the holdings pipeline passes
        'u', Unknown, instead -- defaulting a corrupted byte 06 to a
        *bibliographic* code in a file already classified as holdings
        would silently reclassify the record)
      * byte 08 (Type of control) -> ' ' (not specified)
      * byte 17 (Encoding level) -> `encoding_level_default` against
        `encoding_level_valid` (' ', full level, against the permissive
        bib/authority/holdings union `LEADER_17_ENCODING_LEVEL_VALID`,
        by default; the holdings pipeline passes 'u', Unknown, against
        `LEADER_17_ENCODING_LEVEL_VALID_HOLDINGS` instead -- holdings'
        own spec doesn't define a blank code at all, and does define
        'm', which the shared union set doesn't include)

    Each of these bytes carries real classification information (e.g.
    byte 05 distinguishes a deleted record from a merely corrected one),
    so overwriting an invalid value necessarily picks an arbitrary
    replacement rather than recovering the original intent -- that's a
    real, deliberate trade-off (not a structural fix like the entry-map
    correction, which has exactly one right answer), so every change here
    is logged individually with the byte's original and new value.

    Note on the source script: its own fix for byte 17 has a bug -- it
    reads byte 16 (1-indexed substr(leader,17,1)) instead of byte 17, and
    when that check fails it rewrites byte 08 (via
    substr($0,1,8)" "substr($0,10)) instead of byte 17, so in the
    original script byte 17 is never actually validated or corrected at
    all. This implementation validates and defaults the byte the checks
    are actually named for (17, encoding level) rather than reproducing
    that mismatch.
    """
    details = []
    leader = list(parsed.leader)

    def apply(pos: int, valid: set[str], default: str, label: str):
        if pos < len(leader) and leader[pos] not in valid:
            details.append(
                f"leader byte {pos:02d} ({label}) was {leader[pos]!r}, defaulted to {default!r}"
            )
            leader[pos] = default

    apply(5, LEADER_05_RECORD_STATUS_VALID, "c", "record status")
    apply(6, LEADER_06_TYPE_OF_RECORD_VALID, type_of_record_default, "type of record")
    apply(8, LEADER_08_TYPE_OF_CONTROL_VALID, " ", "type of control")
    apply(17, encoding_level_valid, encoding_level_default, "encoding level")
    if details:
        parsed.leader = "".join(leader)
    return details


def find_suspicious_fields(parsed: ParsedRecord) -> list[tuple[str, str]]:
    """Heuristic content-quality warnings -- these are NOT auto-fixed (except
    where a dedicated opt-in flag exists, noted below); they're reported so
    a human can decide. Returns (category, detail) pairs -- category is a
    stable machine-readable label used to group same-type log entries
    together (see `main`); detail is the human-readable specific finding.
    Checks:

      * A literally-repeated proxy prefix in $u (e.g.
        "...ezproxy...?url=...ezproxy...?url=..."). A normal single-hop
        proxy URL has two "://" (one for the proxy, one for the wrapped
        target) and is NOT flagged; only an actually duplicated prefix is.
        category: "doubled_proxy_url"
      * A tag that isn't 3 numeric digits -- MARC21 tags are always
        numeric. Auto-fixed by default (renamed to an unused 9XX tag --
        see `fix_invalid_tags`), so this only fires when that's disabled
        (--no-fix-invalid-tags) or every 9XX slot is already taken.
        category: "unfixed_non_numeric_tag"
    """
    warnings: list[tuple[str, str]] = []
    for f in parsed.fields:
        if not f.tag.isdigit():
            warnings.append(
                ("unfixed_non_numeric_tag", f"={f.tag}: not 3 numeric digits")
            )
        if f.is_control():
            continue
        for code, data in f.subfields:
            if code == "u" and _DOUBLED_PROXY_PREFIX.search(data):
                warnings.append((
                    "doubled_proxy_url",
                    f"={f.tag} $u has a literally duplicated proxy prefix: {data!r}",
                ))
    return warnings


def find_invalid_indicator_values(parsed: ParsedRecord) -> list[tuple[str, str]]:
    """Flag a data field indicator character that isn't a digit or
    blank (see `VALID_INDICATOR_CHARS`). Skips the 900-999 locally-
    defined range entirely -- MARC21 doesn't prescribe indicator
    meanings there at all, so a local field's own convention (e.g.
    this tool's own `remap_999_to_945` intentionally sets indicators
    "ff" on 945, which isn't a digit/blank but also isn't wrong) can't
    be judged against the standard fields' rule. Detect-only -- there's
    no safe way to guess the intended value, and unlike a missing
    indicator (see --fix-bad-indicators), a *present but wrong*
    character isn't even structurally broken, just semantically
    suspect. category: "invalid_indicator_value"."""
    findings = []
    for f in parsed.fields:
        if f.is_control() or (f.tag.isdigit() and 900 <= int(f.tag) <= 999):
            continue
        for pos, ch in enumerate(f.indicators, start=1):
            if ch not in VALID_INDICATOR_CHARS:
                findings.append((
                    "invalid_indicator_value",
                    f"={f.tag} indicators {f.indicators!r}: indicator {pos} is "
                    f"{ch!r}, not a digit or blank",
                ))
    return findings


def fix_invalid_bibliographic_level(
    parsed: ParsedRecord, default: str = "m"
) -> list[str]:
    """Default leader byte 07 (bibliographic level) to `default` ('m',
    Monograph/Item -- the single most common real-world value) when it
    holds something outside its valid MARC21 code set (see
    `LEADER_07_BIBLIOGRAPHIC_LEVEL_VALID`), mirroring the byte 05/06/08/17
    defaults `fix_invalid_leader_bytes` already applies. This byte
    carries real classification information, so overwriting an invalid
    value is an arbitrary replacement, not a recovery of the original
    intent -- logged individually with the byte's original and new
    value (category "invalid_bibliographic_level", FIXED/REQUIRES
    ATTENTION: worth a second look even though nothing is technically
    broken anymore; NO DATA LOSS since the original value was already
    outside the valid set, not real classification data)."""
    leader = parsed.leader
    if len(leader) >= 8 and leader[7] not in LEADER_07_BIBLIOGRAPHIC_LEVEL_VALID:
        original = leader[7]
        parsed.leader = leader[:7] + default + leader[8:]
        return [
            f"leader byte 07 (bibliographic level) was {original!r}, not one "
            f"of {sorted(LEADER_07_BIBLIOGRAPHIC_LEVEL_VALID)!r}; defaulted to "
            f"{default!r}"
        ]
    return []


def find_dangling_880_links(parsed: ParsedRecord) -> list[tuple[str, str]]:
    """Flag an 880 (Alternate Graphic Representation) field whose $6
    linking subfield references a tag that doesn't exist anywhere else
    in the record (e.g. $6 "245-01" but there's no 245). A dangling
    link breaks the record's own romanized/original-script pairing --
    detect-only, since there's no way to know what the correct link
    should have been. category: "dangling_880_link". Off by default in
    the CLI -- see --check-dangling-880-links."""
    findings = []
    tags_present = {f.tag for f in parsed.fields}
    for f in parsed.fields:
        if f.tag != "880" or f.is_control():
            continue
        for code, data in f.subfields:
            if code != "6":
                continue
            ref_tag = data[:3]
            if ref_tag and ref_tag not in tags_present:
                findings.append((
                    "dangling_880_link",
                    f"880 $6 references ={ref_tag}, but no field with that "
                    f"tag exists in this record: {data!r}",
                ))
    return findings


def _isbn10_checksum_valid(digits: str) -> bool:
    if len(digits) != 10:
        return False
    total = 0
    for i, ch in enumerate(digits):
        if ch == "X" and i == 9:
            value = 10
        elif ch.isdigit():
            value = int(ch)
        else:
            return False
        total += (10 - i) * value
    return total % 11 == 0


def _isbn13_checksum_valid(digits: str) -> bool:
    if len(digits) != 13 or not digits.isdigit():
        return False
    total = sum((1 if i % 2 == 0 else 3) * int(ch) for i, ch in enumerate(digits))
    return total % 10 == 0


def _issn_checksum_valid(digits: str) -> bool:
    if len(digits) != 8:
        return False
    if not digits[:7].isdigit():
        return False
    total = sum((8 - i) * int(ch) for i, ch in enumerate(digits[:7]))
    check = digits[7]
    check_value = 10 if check == "X" else (int(check) if check.isdigit() else None)
    if check_value is None:
        return False
    return (total + check_value) % 11 == 0


def find_invalid_isbn_issn_checksums(parsed: ParsedRecord) -> list[tuple[str, str]]:
    """Flag a 020 $a (ISBN) or 022 $a (ISSN) whose check digit doesn't
    match the standard checksum algorithm for its length (ISBN-10:
    mod-11 weighted 10..1, 'X' = 10; ISBN-13: mod-10 weighted 1/3
    alternating; ISSN: mod-11 weighted 8..2 over 8 digits). A single
    mis-keyed or corrupted digit is exactly what this catches. Ignores
    values whose cleaned length doesn't match a known ISBN/ISSN form at
    all (e.g. a qualifier-only $a) rather than guessing. Detect-only --
    there's no safe way to know which digit was wrong. category:
    "invalid_isbn_issn_checksum". Off by default in the CLI -- see
    --check-isbn-issn-checksum."""
    findings = []
    for f in parsed.fields:
        if f.is_control():
            continue
        if f.tag not in ("020", "022"):
            continue
        for code, data in f.subfields:
            if code != "a":
                continue
            cleaned = re.sub(r"[^0-9Xx]", "", data).upper()
            if f.tag == "020":
                if len(cleaned) == 10 and not _isbn10_checksum_valid(cleaned):
                    findings.append((
                        "invalid_isbn_issn_checksum",
                        f"020 $a {data!r}: ISBN-10 checksum invalid",
                    ))
                elif len(cleaned) == 13 and not _isbn13_checksum_valid(cleaned):
                    findings.append((
                        "invalid_isbn_issn_checksum",
                        f"020 $a {data!r}: ISBN-13 checksum invalid",
                    ))
            elif len(cleaned) == 8 and not _issn_checksum_valid(cleaned):
                findings.append((
                    "invalid_isbn_issn_checksum",
                    f"022 $a {data!r}: ISSN checksum invalid",
                ))
    return findings


def fix_008_length(parsed: ParsedRecord, expected_len: int = 40) -> list[str]:
    """008 must always be exactly `expected_len` characters -- 40 for a
    bibliographic/authority record (the default), or 32 for a holdings
    record (see `HOLDINGS_008_LENGTH`; MARC21's Holdings format defines
    a shorter, differently-laid-out 008 -- https://www.loc.gov/marc/
    holdings/hd008.html -- confirmed against real data: every 008 in a
    real Bucknell holdings export is exactly 32 bytes). Pads a too-short
    one with trailing spaces (the generic "not specified" filler used
    throughout 008's own byte positions) or truncates a too-long one.
    This makes the record loadable (a strict importer like FOLIO can
    reject a wrong-length 008 outright) but is still a real content
    change worth a second look -- logged under FIXED/REQUIRES
    ATTENTION (category "fixed_008_length"), with the original content
    included in full, rather than as an ordinary fixed entry.
    """
    details = []
    for f in parsed.fields:
        if f.tag == "008" and f.content is not None and len(f.content) != expected_len:
            original = f.content
            f.content = original.ljust(expected_len)[:expected_len]
            action = "padded" if len(original) < expected_len else "truncated"
            details.append(
                f"008 was {len(original)} bytes (must be exactly {expected_len}); "
                f"{action} to {expected_len}; original content: {original!r}"
            )
    return details


def _record_lccn(parsed: ParsedRecord) -> str | None:
    """Field 010 $a (LCCN), stripped -- or None. Surfaced on
    `suspect_marc8_escape` findings so a cataloger (or a future
    external-authority lookup -- see HANDOFF.md) has an identifier to
    check the record's real text against, without a separate lookup."""
    field = next((f for f in parsed.fields if f.tag == "010"), None)
    if field is None:
        return None
    value = next((d for c, d in field.subfields if c == "a"), None)
    return value.strip() if value else None


_OCLC_PREFIX_RE = re.compile(r"\(OCoLC\)\s*")


def _record_oclc(parsed: ParsedRecord) -> str | None:
    """Field 035 $a's OCLC number (the "(OCoLC)1234" form), prefix
    stripped -- or None. See `_record_lccn`."""
    for f in parsed.fields:
        if f.tag != "035":
            continue
        for code, data in f.subfields:
            if code != "a":
                continue
            m = _OCLC_PREFIX_RE.match(data)
            if m:
                return data[m.end():].strip()
    return None


def _is_sierra_number(value: str) -> bool:
    """Sierra bib numbers look like "b1234567" or ".b1234567" (an optional
    leading period, then a leading "b")."""
    return value.lower().lstrip(".").startswith("b")


def record_identifier(parsed: ParsedRecord) -> str:
    """The record's own identifier, for use in log lines: subfield $a of
    field 907 if it looks like a Sierra bib number (".b..." or "b..."),
    otherwise field 001's content. Falls back to whichever of the two is
    actually present if the preferred one is missing; empty string if
    neither is present at all."""
    field_907 = next((f for f in parsed.fields if f.tag == "907"), None)
    value_907 = (
        next((d for c, d in field_907.subfields if c == "a"), None)
        if field_907 is not None
        else None
    )
    if value_907 and _is_sierra_number(value_907):
        return value_907

    field_001 = next((f for f in parsed.fields if f.tag == "001"), None)
    if field_001 is not None and field_001.content:
        return field_001.content

    return value_907 or ""


SOURCE_SYSTEMS = ("sierra", "wms", "other")


_SOURCE_SYSTEM_IDENTIFIER = {"sierra": "907 $a", "wms": "001", "other": "001"}


def missing_identifier_reason(parsed: ParsedRecord, source_system: str | None) -> str | None:
    """Why `parsed` has no usable record identifier for `source_system`, or
    None if it does (or `source_system` is None, which skips the check).
    Sierra records must have a 907 $a -- there's no fallback to 001; records
    from any other system must have a non-empty 001."""
    if source_system == "sierra":
        field_907 = next((f for f in parsed.fields if f.tag == "907"), None)
        value = (
            next((d for c, d in field_907.subfields if c == "a"), "")
            if field_907 is not None
            else ""
        )
        if not value.strip():
            return "907 $a is missing, although it is required for Sierra/iii migrations"
    elif source_system in ("wms", "other"):
        field_001 = next((f for f in parsed.fields if f.tag == "001"), None)
        if field_001 is None or not (field_001.content or "").strip():
            return "001 is missing, although it is required for non-Sierra migrations"
    return None


def find_duplicate_identifiers(
    id_records: list[tuple[int, str, str]], ts: str
) -> list[LogEntry]:
    """Given (record_idx, identifier, declared_length) for every record that
    had a usable identifier (see `record_identifier`; `declared_length` is
    each record's own leader bytes 00-04), find identifiers used on more
    than one record in this run and return a LogEntry -- category
    "duplicate_identifier", not fixed, since there's no way to know which
    occurrence (if any) is the "real" one, only that the same identifier
    was reused -- for every record involved in such a collision. This is
    inherently a whole-file check (a record can't tell it's a duplicate in
    isolation), so it runs once after the full pass rather than per-record.
    """
    by_id: dict[str, list[tuple[int, str]]] = {}
    for idx, ident, length in id_records:
        if ident:
            by_id.setdefault(ident, []).append((idx, length))

    entries = []
    for ident, occurrences in by_id.items():
        if len(occurrences) < 2:
            continue
        all_idx = [idx for idx, _ in occurrences]
        for idx, length in occurrences:
            others = [i for i in all_idx if i != idx]
            entries.append(LogEntry(
                "duplicate_identifier", False, ts, idx, ident,
                f"identifier used on {len(occurrences)} records total "
                f"(also records {others}); this record's length (LDR 00-04) = {length}",
            ))
    return entries
