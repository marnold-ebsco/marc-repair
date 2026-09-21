"""Build the kitchen-sink regression fixtures for marc_repair.py.

Standalone, re-runnable, deterministic generator: constructs one synthetic
record per repair/check category documented in docs/REPAIR_CATEGORIES.md
(reusing the same Field_/ParsedRecord/assemble_marc construction patterns
the tests/test_marc_repair_*.py files already use for each category), then pads each
output file with real, unmodified records copied out of two existing
fixtures until it has at least 50 records total.

Outputs (next to this script, i.e. tests/fixtures/):
    kitchen_sink_bib.mrc
    kitchen_sink_holdings.mrc

One record can't be exercised in kitchen_sink_bib.mrc at all, not even
via a second invocation: a leader entry-map byte corrupted to something
that isn't valid UTF-8 anywhere in the byte sequence (as opposed to the
ASCII "45x0" the "ks-entrymap" record above already covers). Merging that
into kitchen_sink_bib.mrc would flip detect_encoding's whole-file guess to
latin-1 (see _ansel()'s docstring below for the identical concern, already
worked around there for MARC-8 records) -- silently corrupting every
*other* record's genuine multi-byte UTF-8 content (ks-moji, ks-smartchar)
in the process. It gets its own single-record output file instead:
    kitchen_sink_bib_entrymap_invalid_utf8_witness.mrc

A second and third record (one per pipeline) can't coexist with their
own file's other invalid_tag example either, for a different reason:
`used_tags` (which tag(s) in 900-999 are already spoken for) is
gathered file-wide, not per-record, so a record that deliberately
fills every 900-999 slot to exercise the "no free 9XX slot" fallback
(category unfixed_non_numeric_tag -- the field gets stripped out
entirely instead of renamed) would ALSO block the *other* invalid-tag
record's own normal, successful rename (category invalid_tag)
elsewhere in the same file, turning it into another
unfixed_non_numeric_tag hit instead of the example it's meant to be.
Each gets its own single-record witness file:
    kitchen_sink_bib_non_numeric_tag_exhausted_witness.mrc
    kitchen_sink_holdings_non_numeric_tag_exhausted_witness.mrc

Run: python tests/fixtures/build_kitchen_sink.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import marc_repair as m  # noqa: E402

FIXTURES = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(FIXTURES))

BIB_OUT = os.path.join(FIXTURES, "kitchen_sink_bib.mrc")
HOLDINGS_OUT = os.path.join(FIXTURES, "kitchen_sink_holdings.mrc")
BIB_INVALID_UTF8_ENTRYMAP_OUT = os.path.join(
    FIXTURES, "kitchen_sink_bib_entrymap_invalid_utf8_witness.mrc"
)
BIB_NON_NUMERIC_TAG_EXHAUSTED_OUT = os.path.join(
    FIXTURES, "kitchen_sink_bib_non_numeric_tag_exhausted_witness.mrc"
)
HOLDINGS_NON_NUMERIC_TAG_EXHAUSTED_OUT = os.path.join(
    FIXTURES, "kitchen_sink_holdings_non_numeric_tag_exhausted_witness.mrc"
)
MIXED_OUT = os.path.join(FIXTURES, "kitchen_sink_mixed.mrc")

BIB_PADDING_SOURCE = os.path.join(
    FIXTURES, "bad_bib_mandatoryfieldsnashvillestate_bibs_202693_me.mrc"
)
#: short_bucknell_marc_holdings.mrc (the file this constant used to
#: point at) was never actually committed anywhere -- it matched the
#: general *.mrc gitignore pattern with no carve-out exception, unlike
#: every other real-data fixture here, so it only ever existed on
#: whatever machine originally built this fixture. holdings_padding_
#: sample.mrc is a 60-record substitute sampled from a real (different,
#: unrelated) institution's holdings export, checked in as its own
#: gitignore exception; see test_repairs_real_short_bucknell_holdings_file
#: for the separate, still-skipped integration test that specifically
#: needs the original file back (its exact-528-records assertion is
#: tied to that one real file, not interchangeable with this sample).
HOLDINGS_PADDING_SOURCE = os.path.join(FIXTURES, "holdings_padding_sample.mrc")

MIN_RECORDS = 50

_BIB_LEADER = "00000nam a2200000 a 4500"
_HOLDINGS_LEADER = "00000nx  a2200000 a 4500"


def _record(leader: str, fields: list) -> bytes:
    return m.assemble_marc(m.ParsedRecord(leader=leader, entries=[], fields=fields))


def _drop_indicator_char(raw: bytes, needle: bytes, n: int = 1) -> bytes:
    """Corrupt a well-formed record's field by removing `n` indicator
    character(s) right before `needle`'s first subfield delimiter --
    mirrors tests/test_marc_repair_bib.py's _drop_indicator_chars helper."""
    assert needle in raw
    return raw.replace(needle, needle[n:], 1)


def _ansel(text: str) -> str:
    """Re-express `text` (containing raw high-byte Latin-1/ANSEL
    characters, e.g. "\\xe5") so that when assemble_marc writes it out
    (as latin-1, since MARC-8-declaring records always are), the bytes
    it produces are ALSO valid UTF-8 for those same intended characters
    -- rather than the single raw high byte a genuine MARC-8 file would
    have on disk. This matters because detect_encoding picks ONE
    encoding for the *whole* file: if any byte anywhere isn't valid
    UTF-8, the entire file (including genuinely UTF-8-declared records
    elsewhere in this same kitchen-sink fixture, e.g. the mojibake/
    smart-character ones) gets read back as latin-1 instead, silently
    corrupting their multi-byte characters. Encoding each intended
    character to UTF-8 first and then re-decoding those bytes as
    latin-1 produces a string that, written out via latin-1 (as this
    record's own leader dictates), leaves valid-UTF-8 bytes on disk;
    once the whole file is (correctly) read back as UTF-8, decoding
    those bytes gives back the exact original single high-byte
    character again -- identical to what a real MARC-8 file's
    single-byte encoding means, just represented losslessly through a
    file that also holds real Unicode content elsewhere."""
    return text.encode("utf-8").decode("latin-1")


def _splice_leader_byte(raw: bytes, pos: int, value: str) -> bytes:
    raw = bytearray(raw)
    raw[pos] = ord(value)
    return bytes(raw)


def _splice_entry_map(raw: bytes) -> bytes:
    """Corrupt leader bytes 20-23 (always "4500") to "45x0", bypassing
    assemble_marc's own force-correction of those bytes -- see
    tests/test_marc_repair_bib.py's _corrupted_entry_map_bytes."""
    raw = bytearray(raw)
    raw[20:24] = b"45x0"
    return bytes(raw)


def _splice_entry_map_invalid_utf8(raw: bytes) -> bytes:
    """Same corruption as `_splice_entry_map`, but with a byte (0x92) that
    isn't valid UTF-8 anywhere in the sequence, rather than plain ASCII
    "x" -- see tests/test_marc_repair.py's
    _corrupted_entry_map_bytes_invalid_utf8. Real production data has
    been seen with exactly this: a file that's genuinely UTF-8 overall,
    with one byte inside the leader's fixed entry-map constant
    corrupted. Kept out of kitchen_sink_bib.mrc itself -- see the module
    docstring -- since it would flip that whole file's detected encoding
    to latin-1."""
    raw = bytearray(raw)
    raw[20:24] = b"45\x920"
    return bytes(raw)


_OVERSIZED_PLACEHOLDER = "P" * 50


def _oversized_field_record(leader: str, before: list, after: list) -> bytes:
    """Build a record with one field so long (>9999 bytes) that
    assemble_marc itself refuses to represent it (its own directory
    length field is only 4 digits) -- assemble_marc can't build this
    directly (see the RepairError it raises), so this instead assembles
    a well-formed record with a short placeholder in that field's spot
    (a valid record assemble_marc happily builds) and then splices the
    real oversized content in afterward, directly in the raw bytes.
    Mode 1 parsing (what the CLI actually uses) discovers field
    boundaries from the real 0x1E terminators it finds, never from the
    (now-stale) declared lengths -- so this parses fine and hits
    assemble_marc's own oversized-field guard on rebuild, exactly the
    real-world scenario ("a field grew past 9999 bytes") this category
    exists for."""
    placeholder = m.Field_("500", "  ", [("a", _OVERSIZED_PLACEHOLDER)])
    raw = _record(leader, before + [placeholder] + after)
    needle = _OVERSIZED_PLACEHOLDER.encode()
    assert raw.count(needle) == 1
    return raw.replace(needle, b"x" * 10000)


# ---------------------------------------------------------------------------
# Bib pipeline: one synthetic record per category
# ---------------------------------------------------------------------------

def build_bib_records() -> list[bytes]:
    records: list[bytes] = []

    # padded_indicators: 245 with 1 indicator char dropped (a real-world
    # defect -- Mode 1 pads it back to 2 with --fix-bad-indicators, on by
    # default).
    raw = _record(_BIB_LEADER, [
        m.Field_("001", None, None, content="ks-padind"),
        m.Field_("008", None, None, content="x" * 40),
        m.Field_("245", "00", [("a", "Padded indicator title.")]),
    ])
    records.append(_drop_indicator_char(raw, b"00" + m.SUBFIELD.encode() + b"a", 1))

    # transcoded_marc8: leader declares MARC-8 (byte 9 blank); 100 $a has
    # a raw ANSEL diacritic byte pymarc converts to Unicode.
    leader = list(_BIB_LEADER)
    leader[9] = " "
    records.append(_record("".join(leader), [
        m.Field_("001", None, None, content="ks-marc8"),
        m.Field_("008", None, None, content="x" * 40),
        m.Field_("100", "1 ", [("a", _ansel("Bal\xe5asim, \xf2Hasan."))]),
    ]))

    # transcode_marc8_failed: leader declares MARC-8; a field ending in a
    # bare/dangling ESC byte with nothing after it makes pymarc's
    # marc8_to_unicode genuinely raise UnicodeDecodeError.
    leader = list(_BIB_LEADER)
    leader[9] = " "
    records.append(_record("".join(leader), [
        m.Field_("001", None, None, content="ks-marc8fail"),
        m.Field_("008", None, None, content="x" * 40),
        m.Field_("500", "  ", [("a", "Truncated escape\x1b")]),
    ]))

    # fixed_mojibake: double-encoded UTF-8 (real UTF-8 bytes mis-read as
    # cp1252), leader already declares Unicode.
    records.append(_record(_BIB_LEADER, [
        m.Field_("001", None, None, content="ks-moji"),
        m.Field_("008", None, None, content="x" * 40),
        m.Field_("245", "00", [("a", "Title.")]),
        m.Field_("500", "  ", [("a", "GroÃŸbritannien")]),
    ]))

    # leader_byte_defaulted: leader byte 5 (record status) holds an
    # invalid code -- defaulted to 'c'.
    leader = _BIB_LEADER[:5] + "0" + _BIB_LEADER[6:]
    records.append(_record(leader, [
        m.Field_("001", None, None, content="ks-leaderbyte"),
        m.Field_("008", None, None, content="x" * 40),
        m.Field_("245", "00", [("a", "Title.")]),
    ]))

    # remapped_999_to_945: a Sierra-style 999 field, remapped only when
    # --remap-999-to-945 is given.
    records.append(_record(_BIB_LEADER, [
        m.Field_("001", None, None, content="ks-999"),
        m.Field_("008", None, None, content="x" * 40),
        m.Field_("245", "00", [("a", "Title.")]),
        m.Field_("999", "  ", [("i", "12345"), ("l", "MAIN")]),
    ]))

    # normalized_subfield_9_to_0: $9 -> $0.
    records.append(_record(_BIB_LEADER, [
        m.Field_("001", None, None, content="ks-sub9"),
        m.Field_("008", None, None, content="x" * 40),
        m.Field_("245", "00", [("a", "Title.")]),
        m.Field_("650", " 0", [("a", "Subject"), ("9", "123456")]),
    ]))

    # normalized_smart_characters: curly quotes/em-dash -> plain ASCII.
    records.append(_record(_BIB_LEADER, [
        m.Field_("001", None, None, content="ks-smartchar"),
        m.Field_("008", None, None, content="x" * 40),
        m.Field_("245", "00", [("a", "Title.")]),
        m.Field_("520", "  ", [("a", "It’s “great”—really.")]),
    ]))

    # removed_invalid_subfield: an uppercase (invalid) subfield code
    # alongside a valid one -- the invalid one is stripped, the field
    # survives.
    records.append(_record(_BIB_LEADER, [
        m.Field_("001", None, None, content="ks-badsubfield"),
        m.Field_("008", None, None, content="x" * 40),
        m.Field_("245", "00", [("a", "Title.")]),
        m.Field_("500", "  ", [("Z", "bad code"), ("a", "Good note.")]),
    ]))

    # fixed_misplaced_subfield_code: a stray space right after the
    # delimiter, immediately followed by the real code -- raw
    # "\x1f c2000." means $c "c2000." (a common AACR2-era copyright-date
    # convention), corrected instead of discarded (real defect found at
    # scale -- 199 of 203 removed_invalid_subfield hits in one production
    # file were exactly this shape).
    records.append(_record(_BIB_LEADER, [
        m.Field_("001", None, None, content="ks-misplacedcode"),
        m.Field_("008", None, None, content="x" * 40),
        m.Field_("245", "00", [("a", "Title.")]),
        m.Field_("260", "  ", [
            ("a", "New York :"), ("b", "Wiley,"), (" ", "c2000."),
        ]),
    ]))

    # field_removed_because_missing_a: 650 (in the default required-$a tag list) with
    # no $a subfield at all -- removed and logged since it has other,
    # non-empty content.
    records.append(_record(_BIB_LEADER, [
        m.Field_("001", None, None, content="ks-missinga"),
        m.Field_("008", None, None, content="x" * 40),
        m.Field_("245", "00", [("a", "Title.")]),
        m.Field_("650", " 0", [("z", "United States.")]),
    ]))

    # removed_non_repeatable_duplicate: a second 245 -- all but the first
    # occurrence are removed.
    records.append(_record(_BIB_LEADER, [
        m.Field_("001", None, None, content="ks-dup245"),
        m.Field_("008", None, None, content="x" * 40),
        m.Field_("245", "14", [("a", "The real title /")]),
        m.Field_("245", "  ", [("a", "2nd ed.")]),
    ]))

    # added_field: a record missing tag 590 -- inserted only when
    # --ensure-field "590:  :a=Ensured field" is passed on this whole run.
    records.append(_record(_BIB_LEADER, [
        m.Field_("001", None, None, content="ks-ensurefield"),
        m.Field_("008", None, None, content="x" * 40),
        m.Field_("245", "00", [("a", "Title.")]),
    ]))

    # added_default_245: no 245 at all.
    records.append(_record(_BIB_LEADER, [
        m.Field_("001", None, None, content="ks-no245"),
        m.Field_("008", None, None, content="x" * 40),
    ]))

    # added_default_245, blank $a variant: a 245 field IS present, with
    # a $a subfield, but it's empty -- add_default_245 treats this the
    # same as no $a at all (see its own "drop any $a that's present but
    # empty" comment) and patches in the placeholder.
    records.append(_record(_BIB_LEADER, [
        m.Field_("001", None, None, content="ks-245blanka"),
        m.Field_("008", None, None, content="x" * 40),
        m.Field_("245", "00", [("a", "")]),
    ]))

    # add_default_245, punctuation-only $a variant: a 245 $a with
    # nothing but a period -- NOTE this is a known gap, not a category
    # this tool currently fixes: add_default_245 only checks `data`
    # truthiness (`if any(code == "a" and data ...)`), unlike
    # strip_missing_required_a/fix_852_call_number's own
    # _is_punctuation_only checks elsewhere, so a punctuation-only $a is
    # treated as if it were a real title and left completely untouched
    # -- no placeholder, no log entry at all. Included here specifically
    # to document and regression-test the CURRENT (arguably
    # inconsistent) behavior, not to exercise added_default_245.
    records.append(_record(_BIB_LEADER, [
        m.Field_("001", None, None, content="ks-245punctonlya"),
        m.Field_("008", None, None, content="x" * 40),
        m.Field_("245", "00", [("a", ".")]),
    ]))

    # added_default_008: no 008 at all -- always fixed, no flag to
    # disable it.
    records.append(_record(_BIB_LEADER, [
        m.Field_("001", None, None, content="ks-no008"),
        m.Field_("245", "00", [("a", "Title.")]),
    ]))

    # fixed_008_length: 008 padded/truncated to 40 bytes (here: too short).
    records.append(_record(_BIB_LEADER, [
        m.Field_("001", None, None, content="ks-short008"),
        m.Field_("008", None, None, content="x" * 30),
        m.Field_("245", "00", [("a", "Title.")]),
    ]))

    # invalid_tag: a non-numeric tag, renamed to a free 9XX slot (default
    # --fix-invalid-tags). Note: the "every 900-999 slot taken" fallback
    # (category unfixed_non_numeric_tag) can't be demonstrated alongside
    # this in the same file -- `used_tags` is gathered file-wide, so a
    # filler record elsewhere using up all of 900-999 would ALSO block
    # this record's own rename, silently turning it into another
    # unfixed_non_numeric_tag hit instead of the successful invalid_tag
    # rename it's meant to show. See
    # build_bib_non_numeric_tag_exhausted_witness for that scenario's
    # own separate file.
    records.append(_record(_BIB_LEADER, [
        m.Field_("001", None, None, content="ks-invalidtag"),
        m.Field_("008", None, None, content="x" * 40),
        m.Field_("245", "00", [("a", "Title.")]),
        m.Field_("FMT", "00", [("a", "Odd tag")]),
    ]))

    # leader_entry_map_fixed: leader bytes 20-23 corrupted from the fixed
    # "4500" constant -- always corrected, logged only with
    # --log-leader-entry-map-fixed.
    raw = _record(_BIB_LEADER, [
        m.Field_("001", None, None, content="ks-entrymap"),
        m.Field_("008", None, None, content="x" * 40),
        m.Field_("245", "00", [("a", "Title.")]),
    ])
    records.append(_splice_entry_map(raw))

    # oversized_sentinel_fixed: many fields (each well under the 9999-byte
    # single-field cap) pushing the total record past 99999 bytes -- the
    # MARC21 sentinel length "99999" is written instead, nothing lost.
    # Includes its own 590 field (this run's --ensure-field target)
    # already present -- otherwise --ensure-field would append one more
    # field at the very end, pushing that field's own starting offset
    # (not just the total length) past the directory's 5-digit cap and
    # tipping this into UNFIXABLE instead of the sentinel path this
    # record is meant to exercise.
    records.append(_record(_BIB_LEADER, [
        m.Field_("001", None, None, content="ks-oversized-ok"),
        m.Field_("008", None, None, content="x" * 40),
        m.Field_("245", "00", [("a", "Title.")]),
        m.Field_("590", "  ", [("a", "Ensured field")]),
    ] + [m.Field_(f"5{i:02d}", "  ", [("a", "x" * 9000)]) for i in range(12)]))

    # unfixable: a single field over the 9999-byte cap -- nothing this
    # tool can do safely, so it's diverted to the "_error" output file
    # instead of the main one (see reattach_orphaned_trailing_fields for
    # the one narrow "can't fix, but CAN still guess safely" case this
    # isn't).
    records.append(_oversized_field_record(
        _BIB_LEADER,
        [
            m.Field_("001", None, None, content="ks-oversized-bad"),
            m.Field_("008", None, None, content="x" * 40),
        ],
        [],
    ))

    # suspect_marc8_escape: leader declares MARC-8; an 880 field with a
    # single-CJK-char escape welded directly to ASCII letters -- a
    # miskeyed diacritic, flagged (not auto-fixed).
    leader = list(_BIB_LEADER)
    leader[9] = " "
    records.append(_record("".join(leader), [
        m.Field_("001", None, None, content="ks-suspectescape"),
        m.Field_("008", None, None, content="x" * 40),
        m.Field_("880", "10", [("a", "Schr\x1b$1)36\x1b(Binger")]),
    ]))

    # doubled_proxy_url: an 856 $u with a literally-repeated proxy prefix.
    records.append(_record(_BIB_LEADER, [
        m.Field_("001", None, None, content="ks-doubledproxy"),
        m.Field_("008", None, None, content="x" * 40),
        m.Field_("245", "00", [("a", "Title.")]),
        m.Field_("856", "4 ", [(
            "u",
            "https://ezproxy.example.edu/login?url="
            "https://ezproxy.example.edu/login?url="
            "http://vendor.example.com/book/123",
        )]),
    ]))

    # invalid_indicator_value: a data field indicator that's neither a
    # digit nor blank.
    records.append(_record(_BIB_LEADER, [
        m.Field_("001", None, None, content="ks-badindicatorvalue"),
        m.Field_("008", None, None, content="x" * 40),
        m.Field_("245", "X0", [("a", "Title.")]),
    ]))

    # invalid_bibliographic_level: leader byte 7 holds a code outside the
    # valid bib-level set.
    leader = _BIB_LEADER[:7] + "9" + _BIB_LEADER[8:]
    records.append(_record(leader, [
        m.Field_("001", None, None, content="ks-badbiblevel"),
        m.Field_("008", None, None, content="x" * 40),
        m.Field_("245", "00", [("a", "Title.")]),
    ]))

    # dangling_880_link: 880 $6 references a tag ("600") that genuinely
    # doesn't exist anywhere else in the record (245/008 are present so
    # neither of those defaults fires and masks this one).
    records.append(_record(_BIB_LEADER, [
        m.Field_("001", None, None, content="ks-dangling880"),
        m.Field_("008", None, None, content="x" * 40),
        m.Field_("245", "00", [("a", "Title.")]),
        m.Field_("880", "1 ", [("6", "600-01"), ("a", "Some heading")]),
    ]))

    # invalid_isbn_issn_checksum: an ISBN-10 with a wrong check digit.
    records.append(_record(_BIB_LEADER, [
        m.Field_("001", None, None, content="ks-badisbn"),
        m.Field_("008", None, None, content="x" * 40),
        m.Field_("245", "00", [("a", "Title.")]),
        m.Field_("020", "  ", [("a", "0596000271")]),
    ]))

    # removed_null_identifier: empty $a immediately followed by another
    # subfield -- seen in the wild as 035 $a$0<local number>, where
    # whatever produced the file split a single value across two
    # subfields and left the first one empty. INFORMATIONAL -- header +
    # count only, never listed in full (see docs/REPAIR_CATEGORIES.md);
    # the fix itself always runs.
    records.append(_record(_BIB_LEADER, [
        m.Field_("001", None, None, content="ks-nullid"),
        m.Field_("008", None, None, content="x" * 40),
        m.Field_("245", "00", [("a", "Title.")]),
        m.Field_("035", "  ", [("a", ""), ("0", "COLOFB  1492")]),
    ]))

    # duplicate_identifier: two records sharing the same 001 -- both
    # flagged (added as a pair, right after each other).
    records.append(_record(_BIB_LEADER, [
        m.Field_("001", None, None, content="ks-dupe-id"),
        m.Field_("008", None, None, content="x" * 40),
        m.Field_("245", "00", [("a", "First copy of a duplicated id.")]),
    ]))
    records.append(_record(_BIB_LEADER, [
        m.Field_("001", None, None, content="ks-dupe-id"),
        m.Field_("008", None, None, content="x" * 40),
        m.Field_("245", "00", [("a", "Second copy, a longer title.")]),
    ]))

    # reattached_orphaned_field: a trailing =700 field physically present
    # in the file but missing its own directory entry, so excluded from
    # the preceding record's own declared length -- real shape (down to
    # the exact subfields) pulled from a production run against
    # ub_bib_records.mrc: "record 58611 (73065)" logged
    #   1 $aQuinn, Frances,$d1963-
    # reattached as a new =700. iter_repair_stream sees this trailing
    # chunk as UNRESOLVED (no leader/directory of its own to parse);
    # reattach_orphaned_trailing_fields recognizes its personal-name
    # shape and merges it into the record immediately before it, which
    # must have parsed cleanly -- built here as one raw byte blob (home
    # record immediately followed by the orphan chunk's own field text
    # + RECTERM, no leader) rather than two separate list entries, so
    # nothing else can end up between them once padding records are
    # appended afterward.
    orphan_home = _record(_BIB_LEADER, [
        m.Field_("001", None, None, content="ks-orphanhome"),
        m.Field_("008", None, None, content="x" * 40),
        m.Field_("245", "00", [("a", "Title.")]),
    ])
    orphan_chunk = (
        "1 " + m.SUBFIELD + "a" + "Quinn, Frances," + m.SUBFIELD + "d" + "1963-" + m.FIELDTERM
    ).encode("utf-8") + b"\x1d"
    records.append(orphan_home + orphan_chunk)

    return records


def build_bib_unresolved_tail() -> bytes:
    """unfixable: trailing garbage with no MARC leader at all, appended
    after at least one real record -- iter_repair_stream can't resync to
    a next leader (there isn't one), so this is genuinely UNFIXABLE
    rather than getting stuck or dropping it: logged at the top of the
    log and diverted, byte-for-byte unchanged, to kitchen_sink_bib_
    error.mrc instead of kitchen_sink_bib.mrc itself."""
    return b"not a marc record at all, no leader here whatsoever\x1d"


def build_bib_non_numeric_tag_exhausted_witness() -> bytes:
    """unfixed_non_numeric_tag (bib): a non-numeric tag with every
    900-999 slot already taken (within this one record), so
    fix_invalid_tags has nowhere to rename it to and the field is
    stripped out entirely instead (see strip_invalid_tags) -- kept out
    of kitchen_sink_bib.mrc itself (see module docstring): `used_tags`
    is gathered file-wide, so this record's own filler fields would
    ALSO block the unrelated "ks-invalidtag"/FMT record above from
    getting its normal successful rename, turning that into an
    unfixed_non_numeric_tag hit too instead of the invalid_tag example
    it's meant to show."""
    fields = [
        m.Field_("001", None, None, content="ks-nonnumerictag-witness"),
        m.Field_("008", None, None, content="x" * 40),
        m.Field_("245", "00", [("a", "Title.")]),
        m.Field_("FMU", "00", [("a", "Odd tag, no free 9XX slot")]),
    ]
    for n in range(900, 1000):
        fields.append(m.Field_(str(n), "  ", [("a", "filler")]))
    return _record(_BIB_LEADER, fields)


def build_bib_invalid_utf8_entrymap_witness() -> bytes:
    """leader_entry_map_fixed, invalid-UTF-8 variant: same category as
    "ks-entrymap" in build_bib_records(), but the corrupted byte isn't
    valid UTF-8 at all (see _splice_entry_map_invalid_utf8) -- kept out
    of kitchen_sink_bib.mrc itself (see module docstring) and written to
    its own single-record file instead. Confirms the CLI doesn't crash
    on this and still corrects the entry map back to "4500"."""
    raw = _record(_BIB_LEADER, [
        m.Field_("001", None, None, content="ks-entrymap-invalidutf8"),
        m.Field_("008", None, None, content="x" * 40),
        m.Field_("245", "00", [("a", "Title.")]),
    ])
    return _splice_entry_map_invalid_utf8(raw)


# ---------------------------------------------------------------------------
# Holdings pipeline: one synthetic record per category
# ---------------------------------------------------------------------------

def build_holdings_records() -> list[bytes]:
    records: list[bytes] = []

    # padded_indicators: 852 with 1 indicator char dropped.
    raw = _record(_HOLDINGS_LEADER, [
        m.Field_("004", None, None, content="ks-hol-padind"),
        m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
        m.Field_("852", "  ", [("a", "Main Library")]),
    ])
    records.append(_drop_indicator_char(raw, b"  " + m.SUBFIELD.encode() + b"a", 1))

    # fixed_mojibake: double-encoded UTF-8 in an 852 note.
    records.append(_record(_HOLDINGS_LEADER, [
        m.Field_("004", None, None, content="ks-hol-moji"),
        m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
        m.Field_("852", "  ", [("a", "GroÃŸbritannien branch")]),
    ]))

    # holdings_leader_byte_defaulted: leader byte 6 (type of record)
    # invalid -- defaulted to 'u' (Unknown), holdings-specific default.
    leader = _HOLDINGS_LEADER[:6] + "!" + _HOLDINGS_LEADER[7:]
    records.append(_record(leader, [
        m.Field_("004", None, None, content="ks-hol-leaderbyte"),
        m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
        m.Field_("852", "  ", [("a", "Main Library")]),
    ]))

    # normalized_subfield_9_to_0: $9 -> $0.
    records.append(_record(_HOLDINGS_LEADER, [
        m.Field_("004", None, None, content="ks-hol-sub9"),
        m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
        m.Field_("852", "  ", [("a", "Main Library"), ("9", "123456")]),
    ]))

    # normalized_smart_characters: curly quotes/em-dash -> plain ASCII.
    records.append(_record(_HOLDINGS_LEADER, [
        m.Field_("004", None, None, content="ks-hol-smartchar"),
        m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
        m.Field_("852", "  ", [("a", "Main Library")]),
        m.Field_("866", "  ", [("a", "It’s “great”—really.")]),
    ]))

    # removed_invalid_subfield: an uppercase (invalid) subfield code.
    records.append(_record(_HOLDINGS_LEADER, [
        m.Field_("004", None, None, content="ks-hol-badsubfield"),
        m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
        m.Field_("852", "  ", [("Z", "bad code"), ("a", "Main Library")]),
    ]))

    # added_default_holdings_008: no 008 at all -- a blank 32-byte
    # placeholder is inserted.
    records.append(_record(_HOLDINGS_LEADER, [
        m.Field_("004", None, None, content="ks-hol-no008"),
        m.Field_("852", "  ", [("a", "Main Library")]),
    ]))

    # fixed_holdings_008_length: 008 is 40 bytes (bib length), padded/
    # truncated to holdings' own 32.
    records.append(_record(_HOLDINGS_LEADER, [
        m.Field_("004", None, None, content="ks-hol-wronglen008"),
        m.Field_("008", None, None, content="x" * 40),
        m.Field_("852", "  ", [("a", "Main Library")]),
    ]))

    # added_missing_852c: 852 with no $c -- a placeholder "Migration" is
    # appended by default (--no-fix-missing-852c to disable).
    records.append(_record(_HOLDINGS_LEADER, [
        m.Field_("004", None, None, content="ks-hol-missing852c"),
        m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
        m.Field_("852", "  ", [("a", "Main Library")]),
    ]))

    # invalid_tag: a non-numeric tag, renamed to a free 9XX slot. Note:
    # the "every 900-999 slot taken" fallback (unfixed_non_numeric_tag)
    # can't be demonstrated alongside this in the same file -- see
    # build_holdings_non_numeric_tag_exhausted_witness for why, same
    # reasoning as the bib pipeline's own build_bib_records.
    records.append(_record(_HOLDINGS_LEADER, [
        m.Field_("004", None, None, content="ks-hol-invalidtag"),
        m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
        m.Field_("852", "  ", [("a", "Main Library")]),
        m.Field_("FMT", "  ", [("a", "bad tag")]),
    ]))

    # holdings_escape_sequence: an ESC byte present -- flagged, not
    # transcoded (holdings MARC-8 transcoding is out of scope for now).
    records.append(_record(_HOLDINGS_LEADER, [
        m.Field_("004", None, None, content="ks-hol-escape"),
        m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
        m.Field_("852", "  ", [("a", "Main Library\x1b(Bfoo")]),
    ]))

    # removed_empty_852_subfield: an 852 subfield present but empty
    # (other than $h, which has its own specific fix above), alongside
    # a real one (so the field survives strip_empty_fields) -- removed,
    # rest of the field left as-is.
    records.append(_record(_HOLDINGS_LEADER, [
        m.Field_("004", None, None, content="ks-hol-empty852sub"),
        m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
        m.Field_("852", "  ", [("a", "Main Library"), ("b", "")]),
    ]))

    # removed_null_identifier: same underlying defect as just above, but
    # on a field OTHER than 852 -- fixed the same way (subfield
    # removed), but only ever LOGGED with --log-removed-null-identifier
    # (off by default, same reasoning as fixed_misplaced_subfield_code).
    records.append(_record(_HOLDINGS_LEADER, [
        m.Field_("004", None, None, content="ks-hol-nullid"),
        m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
        m.Field_("852", "  ", [("a", "Main Library")]),
        m.Field_("866", "  ", [("a", "v.1-10"), ("8", "")]),
    ]))

    # recoded_852_b_to_i: a second $b AFTER $h with no $i yet -- that's
    # actually the cutter/date that goes with $h's classification, just
    # miscoded, so it's recoded to $i rather than removed.
    records.append(_record(_HOLDINGS_LEADER, [
        m.Field_("004", None, None, content="ks-hol-brecode"),
        m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
        m.Field_("852", "  ", [
            ("b", "Main Library"), ("h", "Z678.9 A2"), ("b", "A96 1983"),
        ]),
    ]))

    # removed_extra_852_b: two $b's with the exact same content -- a
    # plain duplicate, so all but the first are just removed.
    records.append(_record(_HOLDINGS_LEADER, [
        m.Field_("004", None, None, content="ks-hol-bdup"),
        m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
        m.Field_("852", "  ", [
            ("b", "Main Library"), ("b", "Main Library"), ("h", "ABC123"),
        ]),
    ]))

    # holdings_missing_004: no 004 field at all -- can't be safely
    # invented (no way to know the linked bib record).
    records.append(_record(_HOLDINGS_LEADER, [
        m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
        m.Field_("852", "  ", [("a", "Main Library")]),
    ]))

    # holdings_multiple_004: more than one 004 -- not necessarily wrong,
    # just surfaced.
    records.append(_record(_HOLDINGS_LEADER, [
        m.Field_("004", None, None, content="ocm100"),
        m.Field_("004", None, None, content="ocm200"),
        m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
        m.Field_("852", "  ", [("a", "Main Library")]),
    ]))

    # unfixable: a single field over the 9999-byte cap -- diverted to
    # the "_error" output instead of the main one (same treatment as
    # the bib pipeline's own unfixable records; formerly its own
    # "oversized_unfixable" category, now merged into "unfixable").
    records.append(_oversized_field_record(
        _HOLDINGS_LEADER,
        [
            m.Field_("004", None, None, content="ks-hol-oversized"),
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
        ],
        [],
    ))

    # holdings_852_b_suspect_content: $b is purely numeric -- looks like
    # data (a piece/copy number) that migrated into the wrong subfield.
    records.append(_record(_HOLDINGS_LEADER, [
        m.Field_("004", None, None, content="ks-hol-852bsuspect"),
        m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
        m.Field_("852", "  ", [("b", "0"), ("c", "Stacks"), ("h", "ABC123")]),
    ]))

    # holdings_853_missing_8: an 853 (Captions and Pattern) with no $8 --
    # the 863/864/865 enumeration fields that should reference it can't
    # be linked.
    records.append(_record(_HOLDINGS_LEADER, [
        m.Field_("004", None, None, content="ks-hol-853missing8"),
        m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
        m.Field_("852", "  ", [("a", "Main Library"), ("c", "Stacks")]),
        m.Field_("853", "  ", [("a", "v.")]),
    ]))

    # holdings_856_missing_u: an 856 (Electronic Location and Access)
    # with no $u -- the field exists but has no actual link.
    records.append(_record(_HOLDINGS_LEADER, [
        m.Field_("004", None, None, content="ks-hol-856missingu"),
        m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
        m.Field_("852", "  ", [("a", "Main Library"), ("c", "Stacks")]),
        m.Field_("856", "4 ", [("z", "Available online")]),
    ]))

    # added_missing_852_location: an 852 with none of $a/$b/$c at all --
    # a placeholder is inserted so the field means something (target
    # subfield is $b here, since this record's 004 isn't WMS/OCLC-
    # prefixed -- see _is_wms_holdings_record).
    records.append(_record(_HOLDINGS_LEADER, [
        m.Field_("004", None, None, content="ks-hol-852nolocation"),
        m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
        m.Field_("852", "  ", [("h", "ABC123")]),
    ]))

    # field_removed_because_missing_a: 866 (Textual Holdings, in
    # holdings_required_a_tags.txt) with real content but no $a --
    # removed and logged since it has other, non-empty content.
    records.append(_record(_HOLDINGS_LEADER, [
        m.Field_("004", None, None, content="ks-hol-866missinga"),
        m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
        m.Field_("852", "  ", [("a", "Main Library"), ("c", "Stacks")]),
        m.Field_("866", "  ", [("z", "Note only, no statement")]),
    ]))

    # holdings_852_duplicate_nr_subfield: a second $h (Classification
    # part -- Not Repeatable per the MARC 21 852 spec) -- removed,
    # keeping the first occurrence.
    records.append(_record(_HOLDINGS_LEADER, [
        m.Field_("004", None, None, content="ks-hol-852duph"),
        m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
        m.Field_("852", "  ", [
            ("a", "Main Library"), ("h", "Z678.9"), ("h", "A2 1983"), ("c", "Stacks"),
        ]),
    ]))

    # incomplete_852 + split_holdings_multiple_852: three 852s -- two
    # usable ($b present) become two split holdings records; the third
    # has no $b at all but DOES have a usable $a (so fix_missing_852_
    # location's own placeholder-fill -- which only fires when NONE of
    # $a/$b/$c has usable content -- doesn't add a $b first and turn
    # this into a third split copy instead), so there's still no
    # location to split out: it's just dropped (incomplete_852) rather
    # than becoming its own copy.
    records.append(_record(_HOLDINGS_LEADER, [
        m.Field_("004", None, None, content="ks-hol-multi852"),
        m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
        m.Field_("852", "  ", [("b", "Main Library"), ("c", "Stacks"), ("h", "ABC123")]),
        m.Field_("852", "  ", [("b", "Annex"), ("c", "Storage"), ("h", "DEF456")]),
        m.Field_("852", "  ", [("a", "No sublocation on this one"), ("h", "GHI789")]),
    ]))

    # removed_bad_call_number: $h is punctuation-only -- unusable,
    # removed; rest of the field left as-is.
    records.append(_record(_HOLDINGS_LEADER, [
        m.Field_("004", None, None, content="ks-hol-852badh"),
        m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
        m.Field_("852", "  ", [("a", "Main Library"), ("c", "Stacks"), ("h", ".")]),
    ]))

    # duplicate_identifier: two holdings records sharing the same 001
    # (uncommon for real holdings data, but record_identifier/
    # find_duplicate_identifiers are the same shared check as the bib
    # pipeline's -- both flagged).
    records.append(_record(_HOLDINGS_LEADER, [
        m.Field_("001", None, None, content="ks-hol-dupe-id"),
        m.Field_("004", None, None, content="ks-hol-dupe1"),
        m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
        m.Field_("852", "  ", [("a", "Main Library"), ("c", "Stacks")]),
    ]))
    records.append(_record(_HOLDINGS_LEADER, [
        m.Field_("001", None, None, content="ks-hol-dupe-id"),
        m.Field_("004", None, None, content="ks-hol-dupe2"),
        m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
        m.Field_("852", "  ", [("a", "Annex"), ("c", "Storage")]),
    ]))

    # fixed_misplaced_subfield_code: a stray space right after the
    # delimiter, immediately followed by the real code -- raw "\x1f
    # z1985-1995" means $z "1985-1995", recovered instead of discarded
    # (same defect shape as the bib pipeline's own example).
    records.append(_record(_HOLDINGS_LEADER, [
        m.Field_("004", None, None, content="ks-hol-misplacedcode"),
        m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
        m.Field_("852", "  ", [("a", "Main Library"), ("c", "Stacks")]),
        m.Field_("866", "  ", [("a", "v.1-10"), (" ", "z1985-1995")]),
    ]))

    # oversized_sentinel_fixed: many fields (each well under the
    # 9999-byte single-field cap) pushing the total record past 99999
    # bytes -- the MARC21 sentinel length "99999" is written instead,
    # nothing lost (same shape as the bib pipeline's own example).
    records.append(_record(_HOLDINGS_LEADER, [
        m.Field_("004", None, None, content="ks-hol-oversized-ok"),
        m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
        m.Field_("852", "  ", [("a", "Main Library"), ("c", "Stacks")]),
    ] + [m.Field_(f"5{i:02d}", "  ", [("a", "x" * 9000)]) for i in range(12)]))

    return records


def build_holdings_unresolved_tail() -> bytes:
    """unfixable (holdings): trailing garbage with no MARC leader at
    all, appended after at least one real holdings record -- diverted
    to the "_error" output, same treatment as the bib pipeline
    (formerly its own "unresolved_record" category, now merged into
    "unfixable")."""
    return b"not a marc record at all, no leader here whatsoever\x1d"


def build_holdings_non_numeric_tag_exhausted_witness() -> bytes:
    """unfixed_non_numeric_tag (holdings): same fallback as
    build_bib_non_numeric_tag_exhausted_witness, on the holdings side --
    every 900-999 slot already taken within this one record, so the
    non-numeric tag gets stripped out entirely instead of renamed. Kept
    out of kitchen_sink_holdings.mrc itself for the identical reason:
    `used_tags` is gathered file-wide, so this would also swallow the
    "ks-hol-invalidtag"/FMT record's own successful rename."""
    fields = [
        m.Field_("004", None, None, content="ks-hol-nonnumerictag-witness"),
        m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
        m.Field_("852", "  ", [("a", "Main Library")]),
        m.Field_("FMU", "  ", [("a", "bad tag, no free 9XX slot")]),
    ]
    for n in range(900, 1000):
        fields.append(m.Field_(str(n), "  ", [("a", "filler")]))
    return _record(_HOLDINGS_LEADER, fields)


# ---------------------------------------------------------------------------
# Padding: real, unmodified records copied from existing fixtures
# ---------------------------------------------------------------------------

def _read_raw_records(path: str) -> list[bytes]:
    with open(path, "rb") as fh:
        data = fh.read()
    parts = data.split(b"\x1d")
    return [p + b"\x1d" for p in parts if p]


def _pad_to_minimum(records: list[bytes], padding_source: str, minimum: int) -> list[bytes]:
    padding = _read_raw_records(padding_source)
    result = list(records)
    i = 0
    while len(result) < minimum:
        result.append(padding[i % len(padding)])
        i += 1
    return result


def main() -> None:
    bib_records = build_bib_records()
    bib_records = _pad_to_minimum(bib_records, BIB_PADDING_SOURCE, MIN_RECORDS)
    with open(BIB_OUT, "wb") as fh:
        for rec in bib_records:
            fh.write(rec)
        fh.write(build_bib_unresolved_tail())
    print(f"wrote {len(bib_records)} record(s) + 1 unresolved tail to {BIB_OUT}")

    holdings_records = build_holdings_records()
    holdings_records = _pad_to_minimum(holdings_records, HOLDINGS_PADDING_SOURCE, MIN_RECORDS)
    with open(HOLDINGS_OUT, "wb") as fh:
        for rec in holdings_records:
            fh.write(rec)
        fh.write(build_holdings_unresolved_tail())
    print(f"wrote {len(holdings_records)} record(s) + 1 unresolved tail to {HOLDINGS_OUT}")

    with open(BIB_INVALID_UTF8_ENTRYMAP_OUT, "wb") as fh:
        fh.write(build_bib_invalid_utf8_entrymap_witness())
    print(f"wrote 1 record to {BIB_INVALID_UTF8_ENTRYMAP_OUT}")

    with open(BIB_NON_NUMERIC_TAG_EXHAUSTED_OUT, "wb") as fh:
        fh.write(build_bib_non_numeric_tag_exhausted_witness())
    print(f"wrote 1 record to {BIB_NON_NUMERIC_TAG_EXHAUSTED_OUT}")

    with open(HOLDINGS_NON_NUMERIC_TAG_EXHAUSTED_OUT, "wb") as fh:
        fh.write(build_holdings_non_numeric_tag_exhausted_witness())
    print(f"wrote 1 record to {HOLDINGS_NON_NUMERIC_TAG_EXHAUSTED_OUT}")

    # kitchen_sink_mixed.mrc: kitchen_sink_bib.mrc and kitchen_sink_
    # holdings.mrc concatenated, byte-for-byte, into one file -- a
    # permanent fixture for --split-bib-holdings, which classifies each
    # record purely by its own leader byte 6, so record order/origin
    # doesn't matter. Includes each source file's own trailing
    # unresolved-tail garbage too (see build_bib_unresolved_tail/
    # build_holdings_unresolved_tail), which also exercises
    # split_bib_holdings' own "unclassified" bucket (that garbage's
    # byte 6 doesn't match any known bib or holdings leader code).
    with open(BIB_OUT, "rb") as bib_fh, open(HOLDINGS_OUT, "rb") as holdings_fh:
        bib_bytes = bib_fh.read()
        holdings_bytes = holdings_fh.read()
    with open(MIXED_OUT, "wb") as fh:
        fh.write(bib_bytes)
        fh.write(holdings_bytes)
    print(f"wrote {BIB_OUT} + {HOLDINGS_OUT} concatenated to {MIXED_OUT}")


if __name__ == "__main__":
    main()
