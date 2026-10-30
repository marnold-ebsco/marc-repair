"""Repair broken MARC21 (ISO 2709) records and patch missing/required fields.

===============================================================================
HOW TO USE
===============================================================================

Two different failure modes land here, and this tool auto-detects which one
it's looking at:

  MODE 1 -- "intact but the leader/directory lies" (the common real-world
  case: some byte(s) in the leader or directory got mangled -- e.g. a control
  character mis-transcoded through the wrong encoding -- so the declared
  record length or field offsets no longer match reality; import tools
  reject this as "bad length" / "invalid directory"). The actual field data
  still has its real 0x1E field terminators and 0x1F subfield delimiters
  intact, so this mode ignores the stale declared lengths entirely and
  re-derives every field's true boundaries from those real delimiters, then
  rebuilds a correct leader + directory around them. Fast, deterministic, no
  guessing.

  MODE 2 -- "delimiters are just gone" (e.g. someone copy/pasted a record
  through a chat box, email, or spreadsheet cell that silently drops raw
  control bytes). Here there is no real 0x1E/0x1F left to find. This mode
  falls back to the still-intact *directory* (tag/length/start for every
  field -- plain digits, never binary, so paste-proof) as ground truth, and
  searches for the unique way to re-insert delimiters that makes every
  field's reconstructed length match what the directory declares, with the
  very last field ending exactly at the end of the record. When more than
  one insertion is possible, or the search runs out of its work budget
  before finding exactly one, the field is reported UNRESOLVED and needs a
  manual override (see OVERRIDES FILE below) -- it is never silently
  guessed. Be aware this genuinely can't always be automatic: a long
  free-text field whose valid subfield codes include common English
  letters (245's $a/$b/$c over a title, say) can have several
  structurally-valid splits that only human judgment -- recognizing a real
  word or name boundary versus an accidental one -- can tell apart. Short
  fields and fields with a small/unusual code set (dates, numbers, most
  control-like fields) resolve automatically just fine; expect to supply
  overrides for the free-text ones in a record with no real delimiters
  left at all.

Quick examples:

    # Fix a file with a corrupted leader/directory (Mode 1 kicks in
    # automatically). Output defaults to INPUT_repaired.mrc next to the input.
    python marc_repair.py bad_length_bib.mrc

    # Same, but pick the output path explicitly and also dump a
    # human-readable .mrk version for review.
    python marc_repair.py bad_length_bib.mrc -o out.mrc --mrk out.mrk

    # A record is missing 245 or 008 -- a placeholder is added by default
    # (245: 00 $aNo title; 008: a fixed generic default), each logged.
    # Supply real content per-record instead with --ensure-field (which
    # takes priority over the placeholder); repeatable, each spec is
    # TAG:INDICATORS:CODE=VALUE, or TAG:CONTENT for a control field.
    python marc_repair.py bad_missing245_bib.mrc --ensure-field "245:00:a=Real title"
    python marc_repair.py bad_bib.mrc \
        --ensure-field "008:780615s19uu    xx a                    d"

    # Repair text that lost ALL its delimiters (Mode 2), and supply an
    # override for a field the automatic solver flagged as ambiguous.
    python marc_repair.py pasted_records.txt --overrides overrides.json

    # Some fields require a non-empty $a to mean anything (e.g. 650 with no
    # $a is just a bare subject subdivision, not a subject) -- by default
    # such fields are removed (see required_a_tags.txt, editable) and
    # non-empty content that's discarded is timestamp-logged. A subfield
    # code that isn't a lowercase letter or digit (unconditional -- no
    # --no- switch, since there's no safe way to leave one in place), a
    # data field with 0 or 1 indicator characters instead of 2, a field
    # with no non-empty subfields at all, and any individual subfield
    # with no data at all (a "null identifier", e.g. a bare $8, or an
    # empty $a immediately followed by another subfield) are also fixed
    # by default. Turn any of the rest off with
    # --no-strip-missing-required-a, --no-fix-bad-indicators,
    # --no-strip-empty-fields respectively (null-identifier stripping
    # also has no --no- switch -- see --log-removed-null-identifier).
    python marc_repair.py bad_bib_mandatoryfields.mrc

    # A record legitimately declares legacy MARC-8/ANSEL encoding (leader
    # byte 9 blank, not "a") -- not a defect on its own, but most modern
    # systems expect UTF-8. Convert it and flip the leader byte accordingly.
    # Requires pymarc: pip install -r requirements.txt
    python marc_repair.py bad_bib_badescape.mrc --transcode-marc8

    # A file too large to comfortably hold in memory as a single string
    # (many millions of records) -- streamed automatically; no flag needed.
    python marc_repair.py huge_export.mrc

OVERRIDES FILE (only needed for Mode 2 UNRESOLVED fields) -- a JSON file
shaped like:

    {
      "<record_index>": {
        "<field_index>": {"indicators": "  ", "subfields": [["a", "..."], ["b", "..."]]}
      }
    }

Both indices are 0-based, in the order fields/records appear. Run once
without --overrides first; any UNRESOLVED field is reported on stderr with
its tag and the raw text still needing a split, which you copy into this file.

Things this tool does NOT do automatically, because they're genuinely
ambiguous, are content (not structural) issues, or need a human-supplied
default:

  * Decide a Mode 2 field's subfield split when more than one split matches
    the declared length and the tag's known subfield codes -- reported as
    UNRESOLVED, fixed via the overrides file above.
  * Detect a literally doubled proxy-URL prefix in $u, or a duplicate
    record identifier across records -- `find_suspicious_fields()` /
    `find_duplicate_identifiers()` flag and timestamp-log these (see
    --log) but never guess a correction; there isn't a safe one.
  * A tag that isn't 3 numeric digits and a missing 008/245 ARE fixed by
    default now (renamed to an unused 9XX tag; a placeholder 008/245
    inserted) -- these are logged as placeholders (008/245: FIXED;
    9XX rename and other default-driven fixes: INFORMATIONAL) rather than
    silently invented, and each has a --no-... flag to leave it flagged
    instead if you'd rather supply the real value yourself (--no-fix-
    invalid-tags; --no-add-default-245, which instead diverts a 245-less
    record, byte-for-byte unchanged, to its own _needs_review file -- see
    below).
  * A record over 99,999 bytes total has a documented MARC21 sentinel for
    exactly this case (the leader declares length "99999" instead of the
    real value; any reader that finds the record's real end from its
    terminator -- as this tool's own Mode 1 always does -- reads it back
    correctly regardless) -- but by default, this record is instead
    diverted to the _error file and logged as unfixable, the same as a
    record neither parsing mode can resolve at all: confirmed empirically
    that FOLIO Data Import aborts its entire batch with no job log at all
    on a record needing this sentinel, rather than rejecting just the one
    record, so the main/repaired output is importable by default, not
    something you have to opt into. Pass --allow-oversized-sentinel only
    if your import target is confirmed to handle the sentinel correctly;
    the record is then written to the main output as usual, logged as
    fixed (category oversized_sentinel_fixed), nothing lost.
  * --guarantee-importable additionally implies --no-add-default-245: a
    missing 245 is diverted to _needs_review instead of getting a
    guessed placeholder title, for the same "don't leave an uncertain
    guess in the main output" reasoning. With this flag, the main
    output, _error file, and _needs_review file end up meaning,
    respectively: safe to import as written; known to break an import
    target entirely; and needs a human decision before it's safe either
    way.
    What's genuinely unfixable regardless of either flag is a single
    field over 9,999 bytes or a base address over 99,999 (the directory's
    own length/position fields have no such sentinel, and a reader needs
    the real base address to find field data at all) -- those, like any
    record neither mode can parse at all, are UNFIXABLE (see below).

Every removal, warning, transcode, and added-field notice from a run goes
into ONE combined, timestamped log file (default OUT_log_TIMESTAMP.log, see
--log). A record that can't be auto-repaired at all is never written into
the main output -- doing so would silently mix a badly mangled record into
an otherwise-clean load file. Instead it's written, byte-for-byte unchanged
(never dropped), to a second file next to the main output
(INPUT_repaired.mrc gets a sibling INPUT_repaired_error.mrc, created
only if this ever actually happens), and logged as `unfixable` in its
own UNFIXABLE section at the
very top of that same log -- above even NOT FIXED, since it's the one
thing that needs a look before the run's output is usable at all.

===============================================================================
"""

# The implementation lives in the marc_repair_lib/ package, in layers (each
# module imports only the ones before it); this file stays the entry point and
# re-exports every name from them so `import marc_repair` /
# `python marc_repair.py` are unchanged.
#   common    constants and shared record types
#   qa        QA checks, identifier helpers, duplicate-identifier check
#   core      detection, reconstruction, streaming, assembly
#   marc8     MARC-8 (ANSEL) repair and transcoding to UTF-8
#   patching  required-field patching, dedupe, recode
#   report    log file writing and the progress reporter
#   cli       holdings repair, profiles, run orchestration, argument parsing
from marc_repair_lib import cli as _cli
from marc_repair_lib import common as _common
from marc_repair_lib import core as _core
from marc_repair_lib import marc8 as _marc8
from marc_repair_lib import patching as _patching
from marc_repair_lib import qa as _qa
from marc_repair_lib import report as _report
from marc_repair_lib.cli import main

for _mod in (_common, _qa, _core, _marc8, _patching, _report, _cli):
    for _name, _value in vars(_mod).items():
        if not _name.startswith("__"):
            globals()[_name] = _value
del _mod, _name, _value


if __name__ == "__main__":
    raise SystemExit(main())
