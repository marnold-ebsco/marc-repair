# marc_repair

A general-purpose tool for repairing broken MARC21 (ISO 2709) files and
patching common missing/invalid fields — without silently guessing at data
it can't be sure about.

Handles everything from a single record pasted into a chat box that lost
its control characters, to a multi-gigabyte, multi-million-record export
with scattered corruption, in one pass with bounded memory.

## The problem

MARC21 binary records (`.mrc`) are notoriously easy to corrupt in ways that
break most tools outright:

- A leader or directory byte gets mangled (e.g. the entry-map field that
  should always read `4500` becomes `45x0`), so the declared record length
  or field offsets no longer match reality.
- A record gets copy/pasted through something that can't carry raw control
  bytes (a chat box, an email client, a spreadsheet cell) and silently
  drops every `0x1E`/`0x1F`/`0x1D` delimiter.
- Individual fields are missing required subfields, have invalid subfield
  codes, wrong indicator counts, or the record is missing 008 entirely.
- A record is legitimately encoded in legacy MARC-8/ANSEL, not UTF-8.
- The same identifier is reused across multiple, genuinely different
  records.
- A record is too large for ISO 2709's fixed-width length fields to
  represent at all.

Most of these either crash a naive parser or get silently mis-parsed. This
tool is built around one rule: **never silently guess**. Where a fix is
unambiguous, it's applied and logged. Where it's genuinely ambiguous, the
tool says so instead of picking an answer.

## How it works

### Two repair modes, auto-detected per record

**Mode 1 — intact delimiters, corrupted leader/directory.** This is the
common real-world case: some byte(s) in the leader or directory got
mangled, so the declared length or field offsets are wrong, but the actual
field data still has its real `0x1E`/`0x1F` bytes. This mode ignores the
stale declared lengths entirely and re-derives every field's true
boundaries from those real delimiters, then rebuilds a correct leader and
directory around them. Fast and deterministic — no guessing involved.

**Mode 2 — delimiters are just gone.** For text that's been stripped of
its control bytes entirely (e.g. pasted through a chat box). This mode
falls back to the still-intact *directory* — plain digits, never binary,
so paste-proof — as ground truth, and does a whole-record backtracking
search for the unique way to re-insert delimiters that makes every field's
reconstructed length match what the directory declares. When more than one
insertion is possible, or the search runs out of its work budget, the
field is reported `UNRESOLVED` and needs a manual override rather than
being guessed. (A long free-text field whose valid subfield codes include
common English letters — a title's `$a`/`$b`/`$c`, say — can genuinely have
several structurally-valid splits that only human judgment can
disambiguate; that's a property of the missing information, not a bug.)

For each record, Mode 1 is tried first; Mode 2 only kicks in when the data
doesn't actually have real delimiters to trust.

### Self-determining record boundaries

Record boundaries are *not* found by scanning for the next record's leader
up front. Instead, each record determines its own true end from its own
content — real delimiters for Mode 1, directory-length arithmetic for Mode
2 — and the next record picks up exactly where the previous one ended.
This matters because a corrupted entry-map field makes the *next* leader
undetectable by pattern-matching; a naive "slice up front, then parse each
slice" approach would silently merge that record's bytes into the
previous one and lose it. (This was a real bug found and fixed during
development — it was silently dropping ~18% of records from a 91MB real
file before the self-determining redesign.)

### Streaming, not whole-file-in-memory

The CLI always reads the input incrementally in chunks (default 32MB)
rather than loading the whole file into memory, and writes each record out
immediately after processing rather than collecting them all first. Memory
use stays bounded by roughly one chunk plus one record's worth of parsed
data at a time, regardless of whether the input has a thousand records or
ten million.

### What gets fixed automatically vs. flagged

| Issue | Behavior |
|---|---|
| Corrupted leader/directory (Mode 1) | Fixed automatically — no flag needed |
| Record too large for the leader's 5-digit length field | Fixed automatically, using MARC21's own documented sentinel (`99999`); nothing is lost since the real end is always found from the terminator; logged as `oversized_sentinel_fixed` (INFORMATIONAL) |
| Single field or base address too large to represent at all | Not fixable — no sentinel exists for these; record passed through unchanged, logged as `oversized_unfixable` (NOT FIXED) |
| Missing 245, or a 245 present but missing $a | Placeholder `$aNo title` added by default — many real-world imports reject a record with no title at all — either as a new field or, if a 245 already exists (e.g. one with only `$h[electronic resource]`), patched into the existing field alongside its other subfields, not stripped and rebuilt; `--ensure-field "245:..."` takes priority per-record if supplied; `--no-add-default-245` to leave such records untouched instead; logged as `added_default_245` (INFORMATIONAL) |
| Missing any other field | `--ensure-field` (opt-in; you supply the content); logged as `added_field` under **FIXED/REQUIRES ATTENTION** since a human-supplied value is worth double-checking |
| Missing 008 | Placeholder inserted by default (a fixed, material-type-agnostic default — real content still needs `--ensure-field "008:..."`, which takes priority per-record); `--no-add-default-008` to leave such records with no 008 instead; logged as `added_default_008` (INFORMATIONAL) |
| Fields missing a required `$a` | Removed by default (see `required_a_tags.txt`, editable); `--no-strip-missing-required-a` to leave them instead; the exact removed content is logged in full as `removed_missing_a` under **FIXED/REQUIRES ATTENTION** since real data was discarded |
| Invalid subfield codes (not `[a-z0-9]`) | Removed by default; `--no-strip-invalid-subfield-codes` to leave them instead; the exact removed content is logged in full as `removed_invalid_subfield` under **FIXED/REQUIRES ATTENTION** since real data was discarded |
| A field where every subfield's data is empty (any tag) | Removed by default (not logged since nothing is discarded); `--no-strip-empty-fields` to leave them instead |
| Data field with 0 or 1 indicator characters instead of 2 | Padded with spaces by default; `--no-fix-bad-indicators` to leave it instead (such a field then fails Mode 1 and falls back to Mode 2/UNRESOLVED); logged as `padded_indicators` (INFORMATIONAL) |
| `999` fields (Sierra's internal item-linking field, not part of MARC21) | Retagged to `945` with indicators `ff` by default; `--no-remap-999-to-945` to leave as-is. Not logged by default (a record can carry many 999s) — pass `--log-999-to-945` to log each one as `remapped_999_to_945` (INFORMATIONAL) |
| `$9` subfields (legacy/local stand-in for `$0`) | Rewritten to `$0` by default; `--no-normalize-subfield-9` to leave as-is; logged as `normalized_subfield_9_to_0` (INFORMATIONAL) |
| Typographic "smart" Unicode punctuation (curly quotes, em/en dashes, ellipsis — see table below) | Normalized to plain ASCII by default; `--no-normalize-smart-characters` to leave as-is. Not logged per-record by default (this can be nearly every record in a file with typographic punctuation) — pass `--log-normalized-smart-characters` to log each one as `normalized_smart_characters` (INFORMATIONAL) |
| Legacy MARC-8/ANSEL encoding | Converted to UTF-8 by default (requires `pymarc`; the run fails loudly if it's missing, rather than silently leaving non-UTF-8 output — install it, or pass `--no-transcode-marc8` if you explicitly want non-UTF-8 records left as-is). Not logged per-record by default (this can be nearly every record in a legacy file) — pass `--log-transcoded-marc8` to log each one as `transcoded_marc8` (INFORMATIONAL) |
| A tag that isn't 3 numeric digits (e.g. `24A` from directory corruption) | Renamed to an unused tag in the 900-999 locally-defined range by default, picked from tags seen during the normal single pass (no extra full pass — only the rare record needing this gets a second, targeted look afterward); `--no-fix-invalid-tags` to leave it as-is instead; logged as `invalid_tag` (INFORMATIONAL) |
| Doubled proxy URLs, duplicate record identifiers | Always detected and logged (NOT FIXED / DUPLICATE RECORDS), never auto-fixed — no safe correction to guess |
| A single Hebrew/Arabic/Cyrillic/Greek/CJK character welded directly between two ASCII letters with no word boundary (e.g. real data found: "Schr" + one CJK character + "inger", almost certainly a miskeyed "ö") | Always detected and logged as `suspect_marc8_escape` (NOT FIXED), never auto-fixed — there's no safe way to guess the intended character; flag this to the source system/cataloger to correct |
| Leader bytes 05/06/08/17 (record status, type of record, type of control, encoding level) outside their valid MARC21 code set | Defaulted (05→`c`, 06→`a`, 08/17→blank) by default; `--no-fix-invalid-leader-bytes` to leave as-is; logged as `leader_byte_defaulted` (INFORMATIONAL) |
| Double-encoded UTF-8 ("mojibake" — see table below) in a record already declaring UTF-8 | Fixed by default (only when re-decoding as UTF-8 actually succeeds, which is effectively impossible by coincidence for text that wasn't really double-encoded); `--no-fix-mojibake` to leave as-is; logged as `fixed_mojibake` (INFORMATIONAL) |
| A Not-Repeatable field appears more than once (see `non_repeatable_tags.txt`, editable — e.g. two `245`s; real data found: a second "245" containing only `$a "2nd ed."`, almost certainly a mistagged `250`) | One occurrence is kept, every later one removed by default so the record is loadable — a strict importer like FOLIO can reject or mishandle the duplicate otherwise. Which one is kept is normally the first, with tag-specific exceptions: a duplicated `001` in a Sierra/Symphony record (`003` = "SIRSI", case-insensitive) keeps whichever occurrence starts with `u` (that system's real bib-id convention) rather than a stray OCLC number/barcode; a duplicated `005` or `008` keeps the most recent by date (ties keep the first). `--no-strip-duplicate-non-repeatable-fields` to leave as-is; the exact removed content is logged in full as `removed_non_repeatable_duplicate` under **FIXED/REQUIRES ATTENTION** (see Logging below) since real data was discarded |
| 008 not exactly 40 characters | Padded with trailing spaces or truncated to 40 by default — a wrong-length 008 can make a record unloadable; `--no-fix-008-length` to leave as-is; the original content is logged in full as `fixed_008_length` under **FIXED/REQUIRES ATTENTION** |
| A data field indicator character that isn't a digit or blank | Always detected and logged as `invalid_indicator_value` (INFORMATIONAL), never auto-fixed — no safe correction to guess |
| Leader byte 07 (bibliographic level) outside its valid MARC21 code set | Always detected and logged as `invalid_bibliographic_level` (INFORMATIONAL), never auto-fixed |
| An 880 field's `$6` linking subfield references a tag that doesn't exist elsewhere in the record | Always detected and logged as `dangling_880_link` (INFORMATIONAL), never auto-fixed — breaks the record's own romanized/original-script pairing |
| A 020 (ISBN) or 022 (ISSN) `$a` whose check digit fails the standard checksum for its length | Always detected and logged as `invalid_isbn_issn_checksum` (INFORMATIONAL), never auto-fixed — no safe way to know which digit was wrong |
| A record that can't be auto-repaired by either mode at all | Passed through to the output unchanged (never dropped), logged as `UNRESOLVED` (NOT FIXED) |

The output file always has the same number of records as the input.

### Smart-character normalization

`normalize_smart_characters` replaces typographic Unicode punctuation
with plain ASCII equivalents, character-by-character, in every
subfield and control field:

| From | To | What it is |
|---|---|---|
| `' ' ‚ ‛` | `'` or `,` | curly single quotes / low-9 quote |
| `" " „ ‟` | `"` | curly double quotes |
| `–` `—` | `-` `--` | en-dash, em-dash |
| `‐ ‑ ‒` | `-` | hyphen, non-breaking hyphen, figure dash |
| `…` | `...` | ellipsis character (one codepoint) → three literal periods |
| `′ ″` | `'` `"` | prime, double-prime (often used for feet/inches, minutes/seconds) |
| non-breaking space | regular space | |
| soft hyphen | removed | invisible hyphenation hint |

These characters are valid Unicode and not a structural defect — a
properly UTF-8-declared record can legitimately contain them — but
some downstream MARC tooling (including MarcEdit) flags them as
suspect, and older/MARC-8-oriented systems can mis-render them even
when the UTF-8 declaration is correct. It's a judgment call (some
cataloging practice deliberately keeps typographic quotes/dashes),
which is why it's overridable (`--no-normalize-smart-characters`)
rather than hardcoded on with no escape. Logged as
`normalized_smart_characters` under the INFORMATIONAL section (see
below) since it changes real content via a fixed substitution table
rather than reconstructing the record's own original data.

### Logging

Every run that changes or flags anything writes one combined,
timestamped log file (default `OUTPUT_log_TIMESTAMP.log`, see `--log`).
Entries are grouped into sections, in this order:

There is no plain "FIXED" section — every successful fix lands in one
of the two sections below (or DUPLICATE RECORDS), so it's always clear
which bucket a given fix fell into:

1. **NOT FIXED** — still needs your attention (warnings, unresolved
   passthroughs, unfixable oversized records)
2. **FIXED/REQUIRES ATTENTION** — the record is now loadable, but real
   data was discarded or altered (or added from outside the record) to
   get there, so it's worth a second look even though nothing is
   technically broken anymore: a duplicate Not-Repeatable field removed
   (`removed_non_repeatable_duplicate`, with the exact removed content),
   an 008 padded/truncated to the required 40 characters
   (`fixed_008_length`, with the original content), a field with an
   invalid subfield code removed (`removed_invalid_subfield`), a
   heading field missing its required `$a` removed (`removed_missing_a`),
   or a missing field added from a human-supplied `--ensure-field` value
   (`added_field`). The goal throughout this tool is a MARC file that's
   always loadable, even when that requires discarding something — but
   that loss is always surfaced here, never silent.
3. **DUPLICATE RECORDS** — the same identifier (`001`, or `907$a` if it
   looks like a Sierra bib number) used on more than one record
4. **INFORMATIONAL** — either fixed via a fixed default/constant rather
   than recovered from the record itself (a placeholder 008/245, a
   leader byte reset to a default code, the leader's entry-map constant
   restored, `$9` promoted to `$0`, typographic punctuation flattened,
   double-encoded UTF-8 corrected, a record transcoded MARC-8 → UTF-8,
   an unparseable tag renamed to an unused 9XX slot, `999` remapped to
   `945`, an oversized record's leader sentinel applied, short
   indicators padded with spaces), or a detect-only finding not urgent
   enough for NOT FIXED (an indicator value outside `[0-9 ]`, leader
   byte 07 outside its valid code set, a dangling 880 `$6` link, an
   ISBN/ISSN with a bad check digit). **Off by default** — the
   underlying fixes/detections still run either way, only the log
   content changes — pass `--log-informational` to include this
   section, since it's typically the highest-volume one (e.g. every
   MARC-8 record transcoded)

Four of the highest-volume fixes need their own flag in addition to
`--log-informational` before they're logged at all — the fix always
runs, only the per-record log line doesn't — since any one of these can
otherwise be the majority of a real file's log:
`--log-transcoded-marc8` (every MARC-8 record converted),
`--log-leader-entry-map-fixed` (every record with a corrupted
entry-map byte), `--log-999-to-945` (every Sierra `999` remapped), and
`--log-normalized-smart-characters` (every typographic-punctuation
substitution).

...and by category within each section, with a header and count, so e.g.
all 375 missing-008 findings sit together instead of scattered by record
order.

## Installation

Requires **Python 3.12+**. The core tool is pure Python (standard library
only) — nothing to install for repairing structural corruption, missing
fields, invalid subfields, or bad indicators.

```bash
git clone <this repo>   # or just copy marc_repair.py + required_a_tags.txt
cd marc_repair
```

Only `--transcode-marc8` (converting legacy MARC-8/ANSEL to UTF-8) needs a
dependency, since accurately reimplementing MARC-8's full character-set
mapping tables from scratch would be error-prone — this defers to
`pymarc`'s LC-authoritative tables instead:

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt   # only needed for --transcode-marc8
```

Everything else runs with a plain `python3 marc_repair.py ...` — no venv
or install step required.

## Usage

```bash
# Fix a file with a corrupted leader/directory. Output defaults to
# INPUT_fixed.mrc next to the input.
python3 marc_repair.py bad_length_bib.mrc

# Pick the output path explicitly and also dump a human-readable .mrk
# version for review.
python3 marc_repair.py bad_length_bib.mrc -o out.mrc --mrk out.mrk

# Just count the records in a file and exit -- no parsing/repair, no
# output file, as fast as possible (counts raw record-terminator bytes
# in big binary chunks; a 91 MB/48,017-record file counts in ~0.3s).
python3 marc_repair.py huge_export.mrc --count

# A record missing 245 or 008 gets a placeholder by default (245: 00
# $aNo title; 008: a fixed generic default), logged either way. Supply
# real content per-record instead with --ensure-field (which takes
# priority over the placeholder); each spec is TAG:INDICATORS:CODE=VALUE,
# or TAG:CONTENT for a control field.
python3 marc_repair.py bad_missing245_bib.mrc --ensure-field "245:00:a=Real title"
python3 marc_repair.py bad_bib.mrc \
    --ensure-field "008:780615s19uu    xx a                    d"

# Some fields require a non-empty $a to mean anything (e.g. 650 with no $a
# is just a bare subject subdivision, not a subject) -- by default such
# fields are removed and logged (see required_a_tags.txt, editable --
# see its header comment for exceptions like 505). A subfield code that
# isn't a lowercase letter or digit, a data field with 0 or 1 indicator
# characters instead of 2, and a field with no non-empty subfields at all
# are also fixed by default. All of the above run automatically --
# nothing extra to pass:
python3 marc_repair.py bad_bib_mandatoryfields.mrc

# Turn any of the above off if you'd rather see them flagged (or left
# alone) instead of fixed:
python3 marc_repair.py bad_bib.mrc \
    --no-strip-missing-required-a \
    --no-strip-invalid-subfield-codes \
    --no-fix-bad-indicators \
    --no-strip-empty-fields

# A record legitimately declares legacy MARC-8/ANSEL encoding. Convert it
# to UTF-8 and flip the leader byte accordingly. Requires pymarc.
python3 marc_repair.py bad_bib_badescape.mrc --transcode-marc8

# Repair text that lost ALL its delimiters (Mode 2), and supply an
# override for a field the automatic solver flagged as ambiguous.
python3 marc_repair.py pasted_records.txt --overrides overrides.json

# A file too large to comfortably hold in memory as a single string (many
# millions of records) -- streamed automatically, no flag needed.
python3 marc_repair.py huge_export.mrc
```

Run `python3 marc_repair.py --help` for the full flag reference — the
module docstring at the top of `marc_repair.py` has the same content plus
more detail on each mode.

### Overrides file (Mode 2 only)

If a record falls back to Mode 2 and a field's subfield split is
genuinely ambiguous, it's reported `UNRESOLVED` on stderr with the tag and
raw text still needing a split. Supply the correct split via a JSON file:

```json
{
  "<record_index>": {
    "<field_index>": {"indicators": "  ", "subfields": [["a", "..."], ["b", "..."]]}
  }
}
```

Both indices are 0-based, in the order records/fields appear. Run once
without `--overrides` first, then fill this in from the stderr output and
re-run with `--overrides overrides.json`.

## Files

| File | Purpose |
|---|---|
| `marc_repair.py` | The tool |
| `required_a_tags.txt` | Editable tag list for `--strip-missing-required-a` — deliberately external, since which fields truly require `$a` is a cataloging-practice judgment call, not something to hardcode |
| `non_repeatable_tags.txt` | Editable tag list for `--strip-duplicate-non-repeatable-fields` — deliberately conservative (only tags whose Not-Repeatable status is well-established); extend it if you find more in your own data |
| `requirements.txt` | Only `pymarc`, only needed for `--transcode-marc8` |
| `tests/test_marc_repair.py` | pytest suite |
| `tests/fixtures/` | Real (anonymized) MARC extracts exercising each defect class |

## Testing

```bash
python3 -m venv venv
./venv/bin/pip install pytest flake8 pymarc
./venv/bin/python -m pytest tests/ -v
./venv/bin/python -m flake8 --max-line-length=100 marc_repair.py
```
