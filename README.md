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

### Tolerating a corrupted byte in an otherwise-UTF-8 file

Real exports can be almost entirely valid UTF-8 but still have a single
byte somewhere that isn't — seen in production data as a legacy byte
standing in for one digit of a leader's fixed `4500` entry-map constant.
`detect_encoding` only samples the first few MB to decide whether to read
a file as UTF-8 or Latin-1, so a bad byte much further into a large file
doesn't change that file-wide guess (correctly — falling back to Latin-1
for the *whole* file just to route around one bad byte would silently
mangle every genuine multi-byte UTF-8 character elsewhere in it). Instead,
the streaming decoder treats an invalid byte as data to carry through
losslessly (via `errors="surrogateescape"`), not a fatal error: parsing
never depended on that byte being valid in the first place (Mode 1 finds
record boundaries from real delimiters), and writing a record back out
round-trips the byte to its original value unless normal repair already
replaces it outright — e.g. the entry map, which is always rewritten as
literal `4500` and never copied through from the original leader.

### Records that truly can't be fixed go to a separate `_error` file

A record neither mode can parse at all (no consistent directory found),
or one with a field/base address too large for ISO 2709's fixed-width
directory to represent, is never written into the main output file —
doing so would silently mix a badly mangled record into an otherwise
clean load file. Instead it's written, byte-for-byte unchanged, to a
second file next to the main output: `--out INPUT_fixed.mrc` (or
whatever `-o` was given) gets a sibling `INPUT_fixed_error.mrc`, created
only if at least one such record actually occurs. Every one is logged
under category `unfixable`, in its own UNFIXABLE section — sorted above
even NOT FIXED, since it's the one thing in the log that requires action
before the run's output is usable at all — with the underlying reason
(the same detail Mode 1/2 or `assemble_marc` itself already produce)
included in full, e.g. "no consistent directory found for this record
at all" or "field is 10005 bytes, but the directory's length field is
only 4 digits". The CLI exits with status 1 whenever this happens, the
same as it always has for a record needing manual attention.

### What gets fixed automatically vs. flagged

| Issue | Behavior |
|---|---|
| Corrupted leader/directory (Mode 1) | Fixed automatically — no flag needed |
| Record too large for the leader's 5-digit length field | Fixed automatically, using MARC21's own documented sentinel (`99999`); nothing is lost since the real end is always found from the terminator; logged as `oversized_sentinel_fixed` (INFORMATIONAL) |
| Single field or base address too large to represent at all | Not fixable — no sentinel exists for these; diverted to a separate `_error` file unchanged, logged as `unfixable` (UNFIXABLE, see above) |
| Missing 245, or a 245 present but missing $a | Placeholder `$aNo title` added by default — many real-world imports reject a record with no title at all — either as a new field or, if a 245 already exists (e.g. one with only `$h[electronic resource]`), patched into the existing field alongside its other subfields, not stripped and rebuilt; `--ensure-field "245:..."` takes priority per-record if supplied; `--no-add-default-245` to leave such records untouched instead; logged as `added_default_245` (INFORMATIONAL) |
| Missing any other field | `--ensure-field` (opt-in; you supply the content); logged as `added_field` under **FIXED/REQUIRES ATTENTION** since a human-supplied value is worth double-checking |
| Missing 008 | Placeholder inserted by default (a fixed, material-type-agnostic default — real content still needs `--ensure-field "008:..."`, which takes priority per-record); `--no-add-default-008` to leave such records with no 008 instead; logged as `added_default_008` (INFORMATIONAL) |
| Fields missing a required `$a` | Removed by default (see `required_a_tags.txt`, editable); `--no-strip-missing-required-a` to leave them instead; the exact removed content is logged in full as `field_removed_because_missing_a` under **FIXED/REQUIRES ATTENTION** since real data was discarded |
| Subfield code is a stray space immediately followed by its real, still-present code (e.g. raw `\x1f c2000.` really meaning `$c` "c2000." — a common AACR2-era copyright-date convention, corrupted by one extra inserted space; seen at real scale in production data) | Corrected by default, before invalid-code removal below gets a chance to discard it — nothing is guessed or lost, the real code is simply the very next character; logged in full as `fixed_misplaced_subfield_code` under **FIXED/REQUIRES ATTENTION**; `--no-fix-misplaced-subfield-codes` to leave it for invalid-code removal to strip instead |
| Invalid subfield codes (not `[a-z0-9]`) | Removed by default; `--no-strip-invalid-subfield-codes` to leave them instead; the exact removed content is logged in full as `removed_invalid_subfield` under **FIXED/REQUIRES ATTENTION** since real data was discarded |
| A field where every subfield's data is empty (any tag) | Removed by default (not logged since nothing is discarded); `--no-strip-empty-fields` to leave them instead |
| Data field with 0 or 1 indicator characters instead of 2 | Padded with spaces by default; `--no-fix-bad-indicators` to leave it instead (such a field then fails Mode 1 and falls back to Mode 2/UNRESOLVED); logged as `padded_indicators` (INFORMATIONAL) |
| `999` fields (Sierra's internal item-linking field, not part of MARC21) | Left as-is by default (not every source is Sierra-originated, and it's not a structural defect); pass `--remap-999-to-945` to retag every one to `945` with indicators `ff` (a locally-defined field other systems will actually accept) instead. Not logged by default even when enabled (a record can carry many 999s) — also pass `--log-999-to-945` to log each one as `remapped_999_to_945` (INFORMATIONAL) |
| `$9` subfields (legacy/local stand-in for `$0`) | Rewritten to `$0` by default; `--no-normalize-subfield-9` to leave as-is. Not logged per-record by default (this can be nearly every record in a file that uses `$9`) — pass `--log-normalized-subfield-9-to-0` to log each one as `normalized_subfield_9_to_0` (INFORMATIONAL) |
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
| An 880 field's `$6` linking subfield references a tag that doesn't exist elsewhere in the record | Off by default — pass `--check-dangling-880-links` to detect and log it as `dangling_880_link` (INFORMATIONAL); never auto-fixed — breaks the record's own romanized/original-script pairing |
| A 020 (ISBN) or 022 (ISSN) `$a` whose check digit fails the standard checksum for its length | Off by default — pass `--check-isbn-issn-checksum` to detect and log it as `invalid_isbn_issn_checksum` (INFORMATIONAL); never auto-fixed — no safe way to know which digit was wrong |
| A record that can't be auto-repaired by either mode at all | Never dropped, but not written into the main output either — diverted unchanged to a separate `_error` file (see above), logged as `unfixable` (UNFIXABLE) |
| A trailing field physically present in the file but missing its own directory entry (so excluded from its record's declared length) — always shaped like a personal name heading ($a plus any of $b/$c/$d/$e/$q/$4) immediately after the record it belongs to | Reattached to that record as a new `=700` by default — the tag itself is a guess (however confident: this subfield-code shape is essentially unambiguous), so it's logged in full as `reattached_orphaned_field` under **FIXED/REQUIRES ATTENTION**; `--no-reattach-orphaned-fields` to leave it `unfixable` (diverted to the `_error` file) instead |

The main output file has the same number of records as the input, minus any diverted to the `_error` file (every input record still ends up in exactly one of the two) and minus one for every successful `reattached_orphaned_field` fix, which by design merges two records-worth of input bytes (a real record, plus a trailing fragment that was never really a separate record to begin with) into one output record.

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
   heading field missing its required `$a` removed (`field_removed_because_missing_a`),
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
   ISBN/ISSN with a bad check digit). **Off by default** — pass
   `--log-informational` to include this section, since it's typically
   the highest-volume one (e.g. every MARC-8 record transcoded)

Five of the highest-volume fixes need their own flag in addition to
`--log-informational` before they're logged at all — the fix always
runs, only the per-record log line doesn't — since any one of these can
otherwise be the majority of a real file's log:
`--log-transcoded-marc8` (every MARC-8 record converted),
`--log-leader-entry-map-fixed` (every record with a corrupted
entry-map byte), `--log-999-to-945` (every Sierra `999` remapped),
`--log-normalized-smart-characters` (every typographic-punctuation
substitution), and `--log-normalized-subfield-9-to-0` (every `$9`
rewritten to `$0`).

Two detect-only checks don't even run by default, since they're pure
overhead with no fix attached unless you're actually looking for their
specific finding: `--check-dangling-880-links` and
`--check-isbn-issn-checksum`. Both still need `--log-informational` too
before their findings show up in the log.

...and by category within each section, with a header and count, so e.g.
all 375 missing-008 findings sit together instead of scattered by record
order.

## Performance

The tool is CPU-bound, not I/O-bound, and scales linearly with input
size. Real timings, measured on the same 16-core machine against two
real files (91MB/48,017 records, and a 2.6GB/706,109-record production
export):

| File | CPython | PyPy | Speedup |
|---|---|---|---|
| 91MB / 48,017 records | 17.6s | 12.2s | 1.44x |
| 2.6GB / 706,109 records | 8m 06s | 4m 51s | 1.67x |

PyPy runs the exact same code — no code changes, no behavior
difference. Both runs above produced byte-identical output `.mrc`
files and identical logs (aside from timestamps), and the full test
suite (225 tests) passes unmodified under PyPy. The speedup ratio
improves with file size, since PyPy's JIT warm-up cost matters less
over a longer run.

See [PyPy (optional, faster on large files)](#pypy-optional-faster-on-large-files)
in Installation below for setup.

## Installation

Requires **Python 3.12+**. The core tool is pure Python (standard library
only) — nothing to install for repairing structural corruption, missing
fields, invalid subfields, or bad indicators. These steps assume a clean
machine with nothing pre-installed.

```bash
git clone <this repo>   # or just copy marc_repair.py + required_a_tags.txt
cd marc_repair
```

### Quick install (no full clone)

`install.sh` fetches only the files needed to run the tool (the script,
`requirements.txt`, the two tag files, and the docs) pinned to one commit,
and builds a ready-to-use venv — no `git clone`, no repo history, no test
fixtures.

```bash
curl -fsSL https://raw.githubusercontent.com/marnold-ebsco/marc-repair/main/install.sh \
  | bash -s -- --dir ~/marc_repair            # add --interpreter pypy for PyPy instead
```

Re-run the same command later to update in place (the venv is reused
unless the interpreter choice changes), or check without changing
anything:

```bash
~/marc_repair/install.sh --dir ~/marc_repair --check
```

Run `install.sh --help` for all options.

### CPython (standard, recommended for most use)

Ubuntu 24.04 ships Python 3.12 by default; on an older/different system,
install a 3.12+ interpreter first (e.g. via the
[deadsnakes PPA](https://github.com/deadsnakes)) before continuing.

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip
python3 --version   # confirm 3.12 or higher

python3 -m venv venv
```

Only `--transcode-marc8` (converting legacy MARC-8/ANSEL to UTF-8) needs a
dependency, since accurately reimplementing MARC-8's full character-set
mapping tables from scratch would be error-prone — this defers to
`pymarc`'s LC-authoritative tables instead:

```bash
./venv/bin/pip install -r requirements.txt   # only needed for --transcode-marc8
```

Everything else runs with a plain `python3 marc_repair.py ...` — no venv
or install step required.

### PyPy (optional, faster on large files)

See [Performance](#performance) above — PyPy runs this tool's exact,
unmodified code roughly 1.4-1.7x faster with identical output, at the
cost of maintaining a second interpreter/venv alongside CPython. It's
worth setting up if you regularly repair very large files; skip it
otherwise.

```bash
sudo apt update
sudo apt install -y pypy3 pypy3-venv
pypy3 --version

pypy3 -m venv pypy_venv
./pypy_venv/bin/pip install -r requirements.txt   # pymarc, for --transcode-marc8
```

Then run the tool exactly the same way, just pointing at the PyPy venv's
interpreter instead:

```bash
./pypy_venv/bin/python marc_repair.py bad_length_bib.mrc
```

### CPython vs. PyPy: which to use

| | CPython | PyPy |
|---|---|---|
| Speed on large files | Baseline | ~1.4-1.7x faster (see [Performance](#performance)); the larger the file, the bigger the win |
| Startup/small-job overhead | Minimal — negligible interpreter startup cost | JIT warm-up adds fixed overhead per run; for a single record or a small file, that overhead can outweigh the eventual speedup |
| Ecosystem/tooling compatibility | Guaranteed — this is the reference implementation every package targets | Generally solid for pure-Python code like this tool, but any *future* dependency isn't guaranteed to have PyPy-compatible (or C-extension-free) wheels |
| Matches project convention | Yes — this project targets Python 3.12+ | No — the Ubuntu-packaged `pypy3` used here implements the Python 3.9 language level |
| Maintenance | One interpreter/venv | A second interpreter/venv to install and keep in sync alongside CPython |
| Verified correctness | N/A (reference behavior) | Confirmed byte-identical output/logs and all 225 tests passing vs. CPython on real files (see [Performance](#performance)) |

**Rule of thumb:** use CPython by default; reach for PyPy only for large,
repeated, or time-sensitive batch runs (e.g. a multi-gigabyte production
export) where the speedup is worth maintaining a second venv.

### Keeping interpreters and dependencies up to date

Both interpreters come from `apt`, so `sudo apt update && sudo apt
upgrade` picks up new patch releases of whichever `python3`/`pypy3`
package the system currently has installed. A newer *major* version
(e.g. Python 3.13, or a `pypy3` build tracking a newer CPython language
level) generally isn't offered by `apt` until the next Ubuntu release,
so check `python3 --version` / `pypy3 --version` after upgrading — if
you need a version ahead of what `apt` offers, that's a deadsnakes-PPA
(CPython) or a fresh download from [pypy.org](https://www.pypy.org/download.html)
(PyPy) rather than an in-place `apt` upgrade. Either way, a venv tracks
whichever interpreter it was created with, so after installing a new
interpreter version, re-create the venv (`python3 -m venv venv` /
`pypy3 -m venv pypy_venv`) rather than expecting the existing one to
pick it up.

For dependencies (currently just `pymarc`, in `requirements.txt`, plus
`pytest`/`flake8` for development), re-run the same install command to
pick up newer versions — `pip` always installs the latest release
satisfying `requirements.txt` unless a version is pinned there:

```bash
./venv/bin/pip install --upgrade -r requirements.txt
./pypy_venv/bin/pip install --upgrade -r requirements.txt   # if using PyPy
```

After upgrading either an interpreter or a dependency, re-run the test
suite (see [Testing](#testing) below) before trusting the result on
real data — this is exactly the kind of change the PyPy comparison in
[Performance](#performance) was verified against (byte-identical
output, all 225 tests passing), and the same verification should be
repeated whenever a version changes.

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
