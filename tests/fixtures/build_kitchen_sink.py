"""Build the kitchen-sink regression fixtures for marc_repair.py.

Standalone, re-runnable, deterministic generator: constructs one synthetic
record per repair/check category documented in docs/REPAIR_CATEGORIES.md
(reusing the same Field_/ParsedRecord/assemble_marc construction patterns
tests/test_marc_repair.py already uses for each category), then pads each
output file with real, unmodified records copied out of two existing
fixtures until it has at least 50 records total.

Outputs (next to this script, i.e. tests/fixtures/):
    kitchen_sink_bib.mrc
    kitchen_sink_holdings.mrc

Two records intentionally can't be exercised in the single default-flag
bib run (missing_008 is only ever logged when --no-add-default-008
disables the very fix that otherwise supersedes it) -- see
docs/REPAIR_CATEGORIES.md's note that add_default_008 supersedes
missing_008. That record is still included here, in kitchen_sink_bib.mrc,
so it's documented and present; a separate CLI invocation with
--no-add-default-008 is required to see its "missing_008" log line (see
the runbook in the task instructions / README for the two-invocation
bib run shape).

A third record can't be exercised in kitchen_sink_bib.mrc at all, not even
via a second invocation: a leader entry-map byte corrupted to something
that isn't valid UTF-8 anywhere in the byte sequence (as opposed to the
ASCII "45x0" the "ks-entrymap" record above already covers). Merging that
into kitchen_sink_bib.mrc would flip detect_encoding's whole-file guess to
latin-1 (see _ansel()'s docstring below for the identical concern, already
worked around there for MARC-8 records) -- silently corrupting every
*other* record's genuine multi-byte UTF-8 content (ks-moji, ks-smartchar)
in the process. It gets its own single-record output file instead:
    kitchen_sink_bib_entrymap_invalid_utf8_witness.mrc

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

BIB_PADDING_SOURCE = os.path.join(
    FIXTURES, "bad_bib_mandatoryfieldsnashvillestate_bibs_202693_me_fixed.mrc"
)
HOLDINGS_PADDING_SOURCE = os.path.join(REPO_ROOT, "short_bucknell_marc_holdings.mrc")

MIN_RECORDS = 50

_BIB_LEADER = "00000nam a2200000 a 4500"
_HOLDINGS_LEADER = "00000nx  a2200000 a 4500"


def _record(leader: str, fields: list) -> bytes:
    return m.assemble_marc(m.ParsedRecord(leader=leader, entries=[], fields=fields))


def _drop_indicator_char(raw: bytes, needle: bytes, n: int = 1) -> bytes:
    """Corrupt a well-formed record's field by removing `n` indicator
    character(s) right before `needle`'s first subfield delimiter --
    mirrors tests/test_marc_repair.py's _drop_indicator_chars helper."""
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
    tests/test_marc_repair.py's _corrupted_entry_map_bytes."""
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

    # added_default_008: no 008 at all -- also doubles, in a *separate*
    # CLI run with --no-add-default-008, as the "missing_008" witness (see
    # module docstring) -- included here once for the default-flags run.
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
    # --fix-invalid-tags).
    records.append(_record(_BIB_LEADER, [
        m.Field_("001", None, None, content="ks-invalidtag"),
        m.Field_("008", None, None, content="x" * 40),
        m.Field_("245", "00", [("a", "Title.")]),
        m.Field_("24A", "00", [("a", "Odd tag")]),
    ]))

    # non_numeric_tag: same defect, but every 900-999 slot is already
    # used *within this one record* -- fix_invalid_tags' fallback then
    # leaves it flagged instead of silently renaming it, even with
    # --fix-invalid-tags on (the default).
    fields_9xx_full = [
        m.Field_("001", None, None, content="ks-nonnumerictag"),
        m.Field_("008", None, None, content="x" * 40),
        m.Field_("245", "00", [("a", "Title.")]),
        m.Field_("24B", "00", [("a", "Odd tag, no free 9XX slot")]),
    ]
    for n in range(900, 1000):
        fields_9xx_full.append(m.Field_(str(n), "  ", [("a", "filler")]))
    records.append(_record(_BIB_LEADER, fields_9xx_full))

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
    # tipping this into oversized_unfixable instead of the sentinel path
    # this record is meant to exercise.
    records.append(_record(_BIB_LEADER, [
        m.Field_("001", None, None, content="ks-oversized-ok"),
        m.Field_("008", None, None, content="x" * 40),
        m.Field_("245", "00", [("a", "Title.")]),
        m.Field_("590", "  ", [("a", "Ensured field")]),
    ] + [m.Field_(f"5{i:02d}", "  ", [("a", "x" * 9000)]) for i in range(12)]))

    # oversized_unfixable: a single field over the 9999-byte cap -- passed
    # through unchanged, nothing this tool can do safely.
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

    return records


def build_bib_unresolved_tail() -> bytes:
    """unresolved_record: trailing garbage with no MARC leader at all,
    appended after at least one real record -- iter_repair_stream can't
    resync to a next leader (there isn't one) and reports it UNRESOLVED,
    passed through unchanged, rather than getting stuck or dropping it."""
    return b"not a marc record at all, no leader here whatsoever\x1d"


def build_bib_missing_008_witness() -> bytes:
    """missing_008 (bib): a record with no 008 at all. In the main
    kitchen-sink run (--add-default-008 on by default) this record's
    008 gets a placeholder instead (category added_default_008, already
    covered by the "ks-no008" record above) -- add_default_008 always
    supersedes missing_008 (see docs/REPAIR_CATEGORIES.md). To actually
    see "missing_008" logged, run this same record through the CLI a
    second time with --no-add-default-008 (see the task runbook)."""
    return _record(_BIB_LEADER, [
        m.Field_("001", None, None, content="ks-missing008-witness"),
        m.Field_("245", "00", [("a", "Title with no 008 at all.")]),
    ])


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
    # appended, only when --fix-missing-852c is given.
    records.append(_record(_HOLDINGS_LEADER, [
        m.Field_("004", None, None, content="ks-hol-missing852c"),
        m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
        m.Field_("852", "  ", [("a", "Main Library")]),
    ]))

    # invalid_tag: a non-numeric tag, renamed to a free 9XX slot.
    records.append(_record(_HOLDINGS_LEADER, [
        m.Field_("004", None, None, content="ks-hol-invalidtag"),
        m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
        m.Field_("852", "  ", [("a", "Main Library")]),
        m.Field_("85Z", "  ", [("a", "bad tag")]),
    ]))

    # non_numeric_tag: same defect, but every 900-999 slot is already
    # used within this one record -- left flagged instead of renamed.
    fields_9xx_full = [
        m.Field_("004", None, None, content="ks-hol-nonnumerictag"),
        m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
        m.Field_("852", "  ", [("a", "Main Library")]),
        m.Field_("85Y", "  ", [("a", "bad tag, no free 9XX slot")]),
    ]
    for n in range(900, 1000):
        fields_9xx_full.append(m.Field_(str(n), "  ", [("a", "filler")]))
    records.append(_record(_HOLDINGS_LEADER, fields_9xx_full))

    # holdings_escape_sequence: an ESC byte present -- flagged, not
    # transcoded (holdings MARC-8 transcoding is out of scope for now).
    records.append(_record(_HOLDINGS_LEADER, [
        m.Field_("004", None, None, content="ks-hol-escape"),
        m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
        m.Field_("852", "  ", [("a", "Main Library\x1b(Bfoo")]),
    ]))

    # holdings_null_identifier: a subfield present but empty, alongside a
    # real one (so the field survives strip_empty_fields).
    records.append(_record(_HOLDINGS_LEADER, [
        m.Field_("004", None, None, content="ks-hol-nullid"),
        m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
        m.Field_("852", "  ", [("a", "Main Library"), ("b", "")]),
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

    # oversized_unfixable: a single field over the 9999-byte cap.
    records.append(_oversized_field_record(
        _HOLDINGS_LEADER,
        [
            m.Field_("004", None, None, content="ks-hol-oversized"),
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
        ],
        [],
    ))

    return records


def build_holdings_unresolved_tail() -> bytes:
    """unresolved_record: trailing garbage with no MARC leader at all,
    appended after at least one real holdings record."""
    return b"not a marc record at all, no leader here whatsoever\x1d"


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
    bib_records.append(build_bib_missing_008_witness())
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


if __name__ == "__main__":
    main()
