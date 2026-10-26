# Handoff History

Archive of closed-out (DONE/RESOLVED) work from prior sessions, moved out of
`HANDOFF.md` to keep that file focused on current/open state. Entries are in
original reverse-chronological order (newest first) and otherwise unedited.
This is a reference, not a pickup point -- nobody needs to read it start to
finish.

## DONE: `missing_245` now backfills from 130/240/246/242/222/880 before falling back to "No title"

**Status: shipped to `main` (`6bd2819`), pushed. Full suite 475 passed/1
skipped, flake8 clean except pre-existing findings.**

`add_default_245` previously went straight to the `$aNo title` placeholder
any time 245 was missing or had no `$a`. Changed so it first tries
`resolve_245_fallback(parsed)`: checks 130/240/246/242/222 in that order
for a non-empty `$a`, then -- if none of those exist -- an 880 linked via
`$6` to 245 or to one of those tags. Only `$a` is copied (no `$b`/`$n`/
etc., per the user's "keep it simple" call). Only when nothing usable is
found does it fall back to the old "No title" placeholder, same as
before.

When that placeholder *is* the one used, a new heuristic,
`looks_like_abandoned_record(parsed)`, flags records that look like
minimal/test stubs (<=3 substantive fields excluding 001/003/005/006/
007/008, no 1xx/6xx/7xx access point, no 300/500 with real content) and
appends a note to the same `added_default_245` log line -- no new log
category, no new CLI flag; `--no-add-default-245` still gates the whole
rule as before.

Searched `sample_files/` and `working/` logs for real `missing_245` hits
to build fixtures from -- found none with 130/240/246/242/222/880
content (the rule has apparently never fired on real data with a usable
fallback field present), but the user pointed at two real minimal-245
production records directly:
* `.b1279417x` (`working/WTS_bibs_2026-10-01.out`, record index 261564)
  -- only a blank (all-spaces, not all-x/pipe) 008 plus 907/998, no 001
  at all. Confirms the heuristic can't key off 008 containing a specific
  fill character; it ignores 008 content entirely and just excludes it
  from the field count.
* `.b19347352` (`working/GTU_bibs.mrc`, record index 404058) -- no 245/
  130/240/246/242/222/880, but has 100/260/300/502 (a thesis record
  where the title was apparently mis-cataloged into 260 `$a` instead of
  245). Good proof the heuristic doesn't fire on a record that's merely
  missing a title, only on one that looks abandoned.

Both are now real-data fixtures in `tests/fixtures/build_kitchen_sink.py`
alongside synthetic 240-fallback and 880-linked-to-245 cases; new test
classes `TestResolve245Fallback` and `TestLooksLikeAbandonedRecord` in
`tests/test_marc_repair_bib.py`. README.md (`added_default_245` table
row, bulleted category list, usage-example comment) and
`tools/generate_repair_categories_doc.py` (regenerates
`docs/REPAIR_CATEGORIES.md`) updated to describe the fallback chain.
## DONE: New `dedupe_010_fields` repair: merges a record's multiple 010s into one, keeps the numerically-largest $a, drops a $z matching it

**Status: shipped to `main` (`5839e67`), pushed. Full suite 461 passed/1
skipped, flake8 clean except pre-existing findings.**

**Superseded 2026-10-26** (see HANDOFF.md): the "numerically largest $a wins,
the rest are discarded (DATA LOSS)" rule below no longer applies. A valid LCCN
(per loc.gov/marc/lccn_structure.html) now always keeps $a, newest year then
serial among valid ones, and every other different $a is moved to $z.

Scanning `working/WTS_bibs_2026-10-01.out` (a MARC-8 file -- had to drop
`force_utf8=True` from the one-off pymarc scan script to parse it at all)
turned up 41 records with more than one 010 (LCCN) field -- almost all
duplicate/whitespace-variant LCCNs, one genuinely differing pair. None had
the $a-in-one/$z-in-another split, but the user asked for general-purpose
dedup logic to handle all of these shapes, not just what happened to
appear in this one file.

The generic `strip_duplicate_non_repeatable_fields` (010 was previously
in `non_repeatable_tags.txt`) would have been the wrong tool: it just
keeps the first occurrence of a duplicated non-repeatable field and drops
every other one *whole*, which for 010 would silently destroy any $z
(Canceled/Invalid LCCN) or $b (NUCMC control number) riding on a later
occurrence. New dedicated repair instead:

* `dedupe_010_fields(parsed)` (`marc_repair.py`): collapses a record's
  010s -- or a single 010 with more than one $a, which can also happen --
  down to exactly one. Every $a (across fields, or within one field) is
  compared with its whitespace fully stripped (not just leading/
  trailing -- LCCNs are conventionally padded with leading spaces); if
  they all normalize the same, the stripped value is kept (NO DATA LOSS).
  If two or more normalize to genuinely different values, the
  **numerically largest** one is kept (via a new `_010_a_sort_key`
  helper -- pulls every digit out of the normalized value and compares
  as a single int; a value with no digits sorts lowest) and the rest
  discarded -- real DATA LOSS, always logged in detail naming every
  dropped value. (User-requested change from the original "keep the
  first one found" rule.) Every $z and $b from every original 010 is
  carried onto the surviving field, de-duplicated per code (normalized
  the same way) -- so a $z/$b never gets lost just because the $a it was
  riding with didn't survive, or because another 010 had no $a at all --
  **except** a $z that normalizes to the same value as the surviving $a,
  which is dropped too (also user-requested): a Canceled/Invalid LCCN
  identical to the LCCN actually being kept is a contradiction, not
  useful data. This $z-vs-$a check fires even for the single-field/
  single-$a case that would otherwise short-circuit untouched (the
  function's early-return guard was widened to check for it). $b is
  treated identically to $z for merging/dedup purposes, per the user's
  earlier explicit instruction (rare in practice, but a legitimate 010
  subfield -- added to `KNOWN_SUBFIELD_CODES["010"]`, which only had
  `az` before) -- but NOT for the new $z-vs-$a drop, which is specific to
  $z's Canceled/Invalid LCCN semantics and doesn't apply to $b (NUCMC
  control number).
* Runs unconditionally (no CLI flag, like `strip_whitespace_only_010_a`),
  called right after that function and *before*
  `strip_duplicate_non_repeatable_fields` -- ordering matters here, since
  the latter would otherwise still clobber 010 as an ordinary
  non-repeatable field. `non_repeatable_tags.txt` now explicitly excludes
  `010` with a comment explaining why.
* New category `deduped_010_lccn`: registered in `_CHECK_DESCRIPTIONS`,
  `_CHECK_DATA_LOSS` (`POSSIBLE DATA LOSS` at the category level, same as
  `removed_non_repeatable_duplicate`), `_FIXED_REQUIRES_ATTENTION`,
  `_ALWAYS_FULL_CATEGORIES`, and -- this is what makes "count every
  corrected record, but only show full detail for genuine data loss"
  work -- `_SUMMARIZE_NO_DATA_LOSS_ENTRIES`, which collapses every
  NO-DATA-LOSS merge in a run into a single count line and only lists
  the true-DATA-LOSS ones individually (same mechanism
  `strip_duplicate_non_repeatable_fields` already uses; confirmed working
  against the kitchen sink run: 10 records matched, 7 collapsed to "7
  record(s) where the fields are exact duplicates", 3 listed in full with
  the dropped LCCN named in each).
* `docs/REPAIR_CATEGORIES.md` regenerated via
  `tools/generate_repair_categories_doc.py` after adding its `CategoryRow`
  entry.
* `tests/test_marc_repair_bib.py`: `TestDedupe010Fields` now 18 tests --
  the original permutations (identical $a, whitespace-only-different $a,
  two $a in one field, same-$a/different-$z, different-$a/different-$z,
  different-$a/identical-$z, $a-in-one-$z-in-other, $b-treated-like-$z,
  duplicate-$z removal, no-$a-anywhere, three-way merge, merged field
  keeps its original position) plus 4 new ones for this change: two
  different $a values where the *second* one is numerically larger
  (confirms it's no longer "first wins"), a $z matching the surviving $a
  on a single untouched-looking field (confirms the widened early-return
  guard), a $z matching the surviving $a across a real multi-field merge,
  and a $b matching $a that's correctly left alone. Plus an end-to-end
  `main()` + log-format test confirming the category header, the DATA
  LOSS call, and the dropped value all land in the log.
* `tests/fixtures/build_kitchen_sink.py`: 10 new `ks-010*` records (one
  per permutation above, reusing the same combinations) --
  `kitchen_sink_bib.mrc`/`kitchen_sink_holdings.mrc`/`kitchen_sink_mixed.mrc`
  regenerated (still 50 bib records; no test hard-codes a bib-side
  per-category count, so nothing else needed updating).

Re-ran the full `marc_repair.py` pipeline against
`working/WTS_bibs_2026-10-01.out` (259,369 records, ~87s) to confirm
against real data, not just synthetic fixtures: `deduped_010_lccn` fired
on exactly the same 41 records the earlier one-off pymarc scan had found,
collapsed 39 whitespace-only-difference merges to a count, and listed the
2 genuinely-different-LCCN records in full (`.b11189708`:
`2014004555`/`2013036218`; `.b11255201`: `2018031265`/`2018028463`) with
the dropped value named in each -- matches expectations exactly. (This
run predates the numerically-largest-wins change below; not re-run
against real data since, but in both of these pairs the
numerically-larger value was already the one listed first, so the kept
value is unaffected -- no behavior change on this particular file.)

Follow-up same day, before committing: user asked for two more changes,
both implemented above and covered by new tests --
(1) keep the numerically largest $a on a genuine mismatch, not simply
the first found; (2) drop a $z that normalizes to the same value as the
surviving $a (a Canceled/Invalid LCCN that's identical to the LCCN being
kept is a contradiction). $b is deliberately exempt from (2).

No open follow-up.
## DONE: `removed_010_whitespace_a` moved from FIXED/REQUIRES ATTENTION to INFORMATIONAL

**Status: code + tests done, committed to `main` alongside this HANDOFF
update. Full suite 461 passed/1 skipped.**

User's reasoning: `strip_whitespace_only_010_a` only ever strips a
whitespace-only $a, which is NO DATA LOSS (the data-loss call was
already `NO DATA LOSS`, not `DATA LOSS`/`POSSIBLE DATA LOSS`) -- so
individually listing every record it touched was unnecessary detail; a
header + count is enough, same as every other NO-DATA-LOSS INFORMATIONAL
category.

* Moved `removed_010_whitespace_a` out of `_FIXED_REQUIRES_ATTENTION`
  and into `_INFORMATIONAL` (`marc_repair.py`) -- this alone changes its
  log section header from `FIXED/REQUIRES ATTENTION` to `INFORMATIONAL`
  and stops it from ever being listed in full (INFORMATIONAL categories
  can't be, not even via `--log-full` -- see `write_log`'s existing
  `label != "INFORMATIONAL"` guard). Also removed it from
  `_ALWAYS_FULL_CATEGORIES` (redundant now, but left stale there would
  have been misleading). `_CHECK_DATA_LOSS` entry (`NO DATA LOSS`)
  unchanged -- that line still prints in the header either way.
* Shortened the category's `_CHECK_DESCRIPTIONS` text per the user's
  exact wording: dropped the FOLIO-Data-Import backstory and the
  "no $a / empty $a / punctuation-only $a is untouched" explanation
  (still true, just no longer worth stating in a one-line header now
  that detail isn't shown per-record anyway) down to "A 010 (LCCN)
  field's $a was present but made up entirely of whitespace (e.g. a
  single space). Only the $a subfield is removed. Any $b or $z is
  retained." (also broadened "$z is kept" to "$b or $z is retained",
  matching the dedupe work above where $b became a first-class
  026/010-style subfield here too).
* `docs/REPAIR_CATEGORIES.md` regenerated via
  `tools/generate_repair_categories_doc.py` after updating that script's
  matching `CategoryRow` (section -> INFORMATIONAL, description
  shortened to match, "Logged by default?" -> the standard INFORMATIONAL
  "No" wording).
* `tests/test_marc_repair_bib.py`:
  `test_bib_pipeline_logs_under_own_category_unconditionally` updated --
  now asserts the `INFORMATIONAL` header (not `FIXED/REQUIRES
  ATTENTION`) and asserts NO per-record detail line appears, instead of
  asserting one does.

No open follow-up.
## DONE: Narrowed 010 $a repair to the one condition that actually breaks FOLIO import

**Status: shipped to `main` (`812c76a`), pushed. Full suite 443 passed/1
skipped at the time.**

The old `strip_missing_required_a(parsed, _010_REQUIRED_A_TAGS)` call for
010 (LCCN) was too strict: it stripped the **entire** 010 field (including
any $z Canceled/Invalid LCCN riding along) whenever $a was missing
entirely, empty, or punctuation-only. That turned out to be overcorrecting
for conditions that don't actually break anything.

Verified directly against FOLIO (bugfest-sunflower, then bugfest-trillium,
tenant `fs09000000`, default "Create instance and SRS MARC Bib" job
profile) by building 6 minimal MARC records that each vary only in 010,
uploading them through Data Import, and reading back the per-record job
log (`/metadata-provider/jobLogEntries/{jobId}`):

| # | 010 content | Import result |
|---|---|---|
| 1 | `$a` only (valid LCCN) | Imported -- LCCN identifier created |
| 2 | `$z` only | Imported -- Canceled LCCN identifier created |
| 3 | `$a` + `$z` | Imported -- both identifiers created |
| 4 | `$z` + `$a` **empty** | Imported -- `$a` silently produces no identifier; `$z` unaffected |
| 5 | `$z` + `$a` = **single space** | **FAILS -- record discarded.** `"identifiers[0].value" must not be null` |
| 6 | `$z` + `$a` = punctuation only (`.`) | Imported -- junk-but-valid LCCN identifier `"."` created |

Root cause (confirmed by reading `folio-org/mod-source-record-manager`'s
`marc_bib_rules.json` and `folio-org/data-import-processing-core`'s
`Processor.java`): the default mapping rule applies a `trim` to 010 $a
before setting `identifiers.value`. A whitespace-only $a trims to `""`,
but the subfield's mere *presence* still causes `identifiers.identifierTypeId`
to get set to LCCN -- leaving a half-built identifier object with a type
but no value, which fails the Instance schema's `NotNull` constraint on
`identifiers[].value` and discards the whole record. An $a that's
*entirely absent* never triggers that identifier-object build at all, so
it's harmless; punctuation survives `trim` unchanged, so it's harmless too
(just junk data).

Only condition 5 (whitespace-only $a) is a real import-breaker. Fixed:

* Replaced the 010-specific `strip_missing_required_a` call with a new,
  narrow `strip_whitespace_only_010_a` (`marc_repair.py`): fires only when
  $a is present and composed entirely of whitespace. Removes just the $a
  subfield -- never the whole field -- so any $z always survives. If
  removing $a leaves the field with no subfields at all, only then is the
  (now-empty) field itself dropped. No $a at all, an empty $a, and a
  punctuation-only (but not whitespace-only) $a are all left untouched.
* Retired `_010_REQUIRED_A_TAGS` (the old frozenset that fed the generic
  function for this case).
* Renamed the log category `removed_010_missing_a` ->
  `removed_010_whitespace_a` everywhere it was registered
  (`_FIXED_REQUIRES_ATTENTION`, `_CHECK_DESCRIPTIONS`, `_CHECK_DATA_LOSS`,
  `_ALWAYS_FULL_CATEGORIES`, bib's `active_categories`) and updated its
  description and data-loss call from `DATA LOSS` to `NO DATA LOSS` --
  stripping whitespace that was never real content isn't data loss, unlike
  the old whole-field removal.
* `tests/test_marc_repair_bib.py`: renamed `TestStrip010MissingA` ->
  `TestStrip010WhitespaceOnlyA`, rewrote its 3 old tests and added 4 new
  ones covering each of the untouched conditions (missing $a, empty $a,
  punctuation-only $a) plus the whitespace-only $a + $z case (now asserts
  only $a is removed and $z survives) and the whitespace-only-$a-alone
  case (whole field dropped, nothing left to keep).
* `docs/REPAIR_CATEGORIES.md` regenerated via
  `tools/generate_repair_categories_doc.py` after updating that script's
  `CategoryRow` entry (the doc's single source of truth) to match.
* Created a project `.venv` (none existed; only `venv`/`venv2` did) per
  the standing Python-rules instruction, and added `.venv/` to
  `.gitignore` alongside the existing `venv/`/`venv2/` entries.

The live-FOLIO test scripts themselves (upload/process/poll a Data Import
job via the FOLIO API, pull back per-record job-log results) live in
`working/folio_import_test/` for now -- intended to move into `tools/`
(e.g. `tools/folio_data_import_test/`) as a kept, reusable utility rather
than scratch, but not yet moved.

**Open follow-up:**
- Move `working/folio_import_test/`'s scripts into `tools/` (skip the
  throwaway `.venv`, generated `.mrc` files, and `.log`/`.json` output).
- The `working/folio_import_test/` script itself has never been shown to
  actually work -- see the dedicated OPEN section below
  ("API-driven FOLIO Data Import never completes a job"). Don't move it
  into `tools/` until that's resolved.
## DONE: `add_missing_852c`'s $c placeholder gated on WMS-sourced records

**Status: shipped to `main` (`5bc3181`), pushed. Full suite 436 passed/1
skipped. README also updated (`8dcd623`, pushed) to document updating an
`install.sh` install at a non-default `--dir`.**

User reported a non-WMS holdings record (`$b LAW REF $k REF $h F1651 $i
B232 2009`, no `004`, already fully populated) coming out of repair with
a spurious `$c Migration` appended. Root cause: `add_missing_852c`
(`marc_repair.py`) appended the placeholder to *any* 852 missing `$c`,
regardless of whether `$a`/`$b` already carried real content -- it only
ever checked for `$c`'s absence, with no awareness that `$c` is just one
of several source-system conventions for where location lives (see
`fix_missing_852_location`'s own docstring on that point). Fixed by
gating the whole function on `_is_wms_holdings_record(parsed)` (the same
004-prefix check `fix_missing_852_location` already used to pick `$b` vs
`$c`) -- it now no-ops entirely for a non-WMS record, since `$c` isn't
that record's live location subfield at all. Updated the function's
docstring, the pipeline-summary docstring, `CATEGORY_DESCRIPTIONS`, and
the `--no-fix-missing-852c` CLI help text to match. Fixed two existing
tests that implicitly relied on the old non-WMS behavior, added a new
no-op test for a non-WMS record, and gave
`tests/fixtures/build_kitchen_sink.py`'s `added_missing_852c` demo record
an `ocm`-prefixed `004` so it still exercises the category (regenerated
`kitchen_sink_holdings.mrc`/`kitchen_sink_mixed.mrc` accordingly).

Separately, user asked how to update an existing `install.sh` install at
a non-default path (`/working/migration/scripts/marc-repair`, an EC2 box
set up as part of a larger migration checkout). Documented in `README.md`
(Installation section): pipe `install.sh` in fresh via `curl` with
`--dir <that-path>` every time -- `install.sh` itself isn't one of the
files the install fetches into `--dir` (it can't safely rewrite itself
mid-run), so there's no local copy there to run directly unless placed
separately.

No open follow-up.
## DONE: bib records now also flagged for 856 missing $u

**Status: shipped to `main` (`1d6911a`), pushed. Full suite 435 passed/1
skipped.**

`find_856_missing_u` (856 Electronic Location and Access field with no $u
URI) previously only ran on holdings records (`holdings_856_missing_u`).
Wired the same check into the bib pipeline in `main()` as a new
`bib_856_missing_u` category -- NEEDS REVIEW, always active (no flag),
always listed in full, NO DATA LOSS (detect-only, never auto-fixed -- this
tool has no way to invent a real URL). Registered in every category table
that `holdings_856_missing_u` was in (`_NEEDS_REVIEW`,
`_CHECK_DESCRIPTIONS`, `_CHECK_DATA_LOSS`, `_ALWAYS_FULL_CATEGORIES`, bib's
`active_categories` base set). `docs/REPAIR_CATEGORIES.md` regenerated via
`tools/generate_repair_categories_doc.py` (new bib row added there too).
Tests added in `tests/test_marc_repair_bib.py`
(`TestDetectOnlyChecks`/`TestBib856MissingUCli`). Both categories'
descriptions also now end with "Check to see if the link is in an
incorrect subfield." (user-requested wording -- a missing $u often means
the real URL landed in the wrong subfield, e.g. $a or $z, not that no URL
exists at all).

Deployed copies (EC2, via `install.sh`, not a git clone -- see
`install.sh`'s own header) pick this up with:
```
curl -fsSL https://raw.githubusercontent.com/marnold-ebsco/marc-repair/main/install.sh | bash -s -- --dir <install-dir>
```
No open follow-up.
## DONE: README never documented the `--b2-lexicon` fixer (found + fixed 2026-10-08)

**Status: committed to `main` (`4981503`, "Document the --b2-lexicon
umlaut fixer and its build script in the README"). Was previously logged
here as uncommitted/WSL-down; confirmed closed via `git log` as of
2026-10-12.**

User asked where the lexicon install instructions were, since they weren't
in the README. They weren't anywhere in the README -- the `0xB2`
lexicon-gated umlaut fixer (`fix_marc8_b2_umlaut`/`load_b2_lexicon`,
`--b2-lexicon`/`--no-b2-lexicon`, on by default when `working/b2_lexicon.json`
exists -- see `marc_repair.py` around line 2901) and the script that builds
that file (`tools/context_probe/build_b2_lexicon.py`, which already
orchestrates the corpus scan + `wngerman`/LC-SKOS downloads + merge + trim
end to end) were both already fully implemented and merged to `main`; only
the README mention was missing. Nothing in the code was missing or needed
merging -- this was a documentation gap, not a functionality gap.

Added to `README.md`:
- A new row in the "What gets fixed automatically vs. flagged" table for
  the `0xB2` bare-byte fix.
- A new "0xB2 lexicon-gated umlaut repair" subsection (after "Smart-character
  normalization") with the one-command build
  (`python tools/context_probe/build_b2_lexicon.py --corpus ... --out
  working/b2_lexicon.json`), the `--corpus-only`/`--include-lc-names`
  options, and a pointer to this file's "HOWTO: build the `--b2-lexicon`
  file" section for the full manual walkthrough and license notes.
## DONE: oversized-field directory corruption (`dropped_oversized_field`), plus log header reformat

**Status: shipped to both `context-language-disambiguation` (`3bfffe3`) and
`main` (`db98708`, cherry-picked -- see below for why not a full merge).
`pytest` clean on both (430 passed/1 skipped on the branch, 419 passed/1
skipped on `main` -- the gap is the branch's b2-lexicon-only tests, which
don't exist on `main`). Full-corpus re-run against `working/GTU_bibs.mrc`
confirms the fix and shows no regressions.**

**The bug report.** User found `working/GTU_bibs.mrc`'s 001 `1456853630`
(`.b1929265x`) came out of every prior repaired file (`GTU_bibs_repaired.mrc`,
`_sigfix.mrc`, `_0xa4fix.mrc`) truncated right after its first `505` --
everything past it (the rest of the `505`, `546`, `520`, every `650`/`600`,
all five `700`s) was gone from the main output. Isolating the record (see
`working/single_1456853630*.mrc`) and running it alone reproduced this: the
tool read it as **2 records**, wrote a 7152-byte truncated one to the main
output, and silently shunted the real remainder into the `_error.mrc` file
tagged `UNFIXABLE`, as if it were an unrelated, unparseable fragment.

**Root cause (upstream data, not this tool).** The record's first `505`
(Hebrew TOC note) is genuinely 10,032 bytes -- bloated by ~900 repeats of a
malformed per-character MARC-8 escape pattern (`\x1bb9\x1bs...\x1b(XY\x1b(B`,
apparently from whatever wrapped nearly every individual Hebrew character in
its own spurious subscript/superscript escape switch instead of one escape
around the whole run). ISO 2709's directory length field is only 4 digits
(max 9999), so this field's real length can't be declared at all -- whatever
produced this GTU export wrote a truncated/wrapped length (`1003`) instead.
`parse_directory`'s own cumulative-offset sanity check (`start != cum`,
`marc_repair.py` around line 462 before this fix) caught the lie at the
*next* entry (whose own declared `start` reflects the true byte layout) and,
per its existing (correct, for other corruption shapes) design, gave up and
kept only the entries parsed so far -- silently discarding the other 20 real
directory entries and everything they pointed to. `_read_intact_at` then
walked real `\x1e` delimiters for just those surviving entries, happily
swallowing the entire 10,032-byte blob as "the field's content" (it never
cross-checked that against the directory's declared length), and returned a
"successfully parsed" record that stopped right there.

**The fix.** Two changes, both in `marc_repair.py`:

1. `parse_directory`: when the cumulative check fails, before giving up it
   now checks whether the gap is *fully explained* by the previous entry
   actually being longer than its declared length -- using the next entry's
   own (untouched) `start` as ground truth: `recovered_length =
   entries[-1].length + (start - cum)`. If `recovered_length > 9999` (the
   signature of this exact defect -- a real, uncorrupted entry can never
   need more than 9999), it patches that one entry's length in-memory
   (even past 4-digit range, fine in a Python int) and keeps parsing instead
   of abandoning the rest of the directory. Anything else (recovered length
   <= 9999, or no entries yet) falls through to the old `break` --
   unrelated corruption shapes are untouched.
2. `_read_intact_at`: any directory entry with `length > 9999` (only
   reachable via #1's recovery) is dropped rather than assembled into a
   field -- there's no valid way to declare its real length in a rebuilt
   directory either, so keeping it would just reproduce the same problem on
   write. Recorded on a new `ParsedRecord.dropped_oversized_fields` list
   (tag, true length, 200-char preview), mirroring the existing
   `indicator_fixes`/`reattached_orphaned_fields` pattern, and logged by both
   call sites (`repair_holdings_records` and `main`) under a new
   **`dropped_oversized_field`** category (FIXED/REQUIRES ATTENTION, DATA
   LOSS, always-full) -- so the one field is dropped and *logged*, but the
   rest of the record (every field after it) is recovered instead of lost.

**Log header reformat (separate, done same session, same commit).** Every
category's header in the log file now has a consistent 4-line shape:
section/category line, then the plain description (never itself stating a
data-loss call), then that call **in isolation on its own line** (`DATA
LOSS` / `POSSIBLE DATA LOSS` / `NO DATA LOSS`, no punctuation), then the
record/instance count. The call used to be embedded inside the (sometimes
multi-line-wrapped) description text, which read inconsistently once a
description ran long. New `_CHECK_DATA_LOSS: dict[str, str]` dict (one entry
per category, next to the existing `_CHECK_DESCRIPTIONS`) carries the call;
`_write_description_header` no longer appends a closing `" ==="` suffix to
the description's own lines, since the data-loss line now closes the block.
Every one of `_CHECK_DESCRIPTIONS`'s 56 (`main`: 55, see below) entries had
its data-loss phrase mechanically extracted and moved -- verified by
round-tripping the generated dict text back through `exec()` and diffing
every value against the intended text before writing, rather than eyeballing
56 hand-edited strings. Two entries needed hand-handling: `unfixable` and
`suspect_hex_encoded_marc8` had explanatory text *after* the marker
(`"DATA LOSS. These records cannot be loaded."`) that got folded back into
the main description; `holdings_852_b_suspect_content` embeds a live
f-string reference to `DEFAULT_852_LOCATION_CONTENT` rather than a baked-in
literal, preserved by hand rather than flattened.

**Why `main` got a cherry-pick, not a full merge.** Asked the user; chose
cherry-pick. `context-language-disambiguation` is 11 commits ahead of `main`
with unrelated in-progress work (the 0xB2-lexicon fixer, language/context
disambiguation research, HANDOFF notes) -- merging the whole branch would
have pulled all of that into `main` too. The cherry-pick hit one merge
conflict, entirely inside the `_CHECK_DESCRIPTIONS`/`_CHECK_DATA_LOSS`
dict-literal region (everything else -- `parse_directory`, `_read_intact_at`,
`ParsedRecord`, the category-registry sets, both log call sites --
applied cleanly): the branch's pre-fix state already had one extra category
(`fixed_marc8_b2_umlaut`, from the unrelated 0xB2-lexicon work, not yet on
`main`) that `main` doesn't have at all (`main` has no `b2_lexicon`/
`fix_marc8_b2_umlaut` code whatsoever). Resolved by hand: `main`'s version of
both new dicts has 55 entries (`dropped_oversized_field` added, no
`fixed_marc8_b2_umlaut`), everything else byte-identical to the branch's.

**Verification.**
- Isolated the record into `working/single_1456853630.mrc` (reproduces with
  1 record in, 2 "records" read, before the fix).
- After the fix: 1 record in, 1 record out, no `_error.mrc`; 41 fields
  survive (was: 1 truncated field in the main output, rest orphaned);
  `dropped_oversized_field` logged once with the record's own identifier
  (`.b1929265x`), true length (10032), and a content preview.
- Full-corpus re-run, `working/GTU_bibs.mrc` (404,956 records, 527.5MB,
  ~3m10s): `dropped_oversized_field` fires **exactly once** across the
  whole file -- this record is the only one with this defect. Record-count
  comparison: every prior run (`GTU_bibs_repaired.mrc`/`_sigfix.mrc`/
  `_0xa4fix.mrc`) wrote 404,956 to the main output (including the 1
  truncated record) *plus* 1 to the error file -- 404,957 record-slots
  for 404,956 real source records, because one record was double-counted
  across two files. The new run (`working/GTU_bibs_repaired_final.mrc`)
  writes exactly 404,956 to the main output and produces no error file at
  all -- 1:1 with the input, nothing split or dropped to a side file.
- `main`'s cherry-picked version produces byte-identical `dropped_oversized_
  field` behavior on the same isolated record (re-ran the single-record
  test against `main` after the cherry-pick, confirmed same log output
  modulo the absent b2-lexicon-only category).
## DONE: Experiment: language/context to disambiguate overloaded bytes (branch `context-language-disambiguation`)

**Status: research only. Nothing in `marc_repair.py` changed.** Scripts are in
`tools/context_probe/` (flake8-clean except the user-supplied
`polyglot_detector_marc.py`, left as received). Run from the repo root, e.g.
`python tools/context_probe/a5_context_probe.py --ladder`. Data paths are
`working/...` (gitignored), so the per-byte sample files and
`working/lexicon_GTU.pkl` must exist; build the pickle once with
`python tools/context_probe/lang_lexicon_probe.py build` (~5 min on
`working/GTU_bibs.mrc`). Per the standing instruction, the full corpus was
not re-run for any of the byte checks -- only the per-byte sample files.

**The question.** Can the field/record's *language* (the user's polyglot
detector, plus 008/041) decide which mark an overloaded bare byte should
become (e.g. German -> umlaut), instead of the before-letter table alone?

**What was built** (all in `tools/context_probe/`):

- `polyglot_field_probe.py` -- for every occurrence of 0xA4/A5/A6/A8/AE/B2/B3
  run the field text through the unmodified detector and tabulate detected
  language vs. the mark the current rule applies. First-pass signal check.
- `lang_lexicon_probe.py` -- an earlier, *rejected* approach (a lexicon of
  cleanly-encoded diacritic words from the corpus). Still used as a data
  source by `b2_context_probe.py`. Do not expect it to resolve Sanskrit/
  Arabic: those words never appear cleanly anywhere in this corpus (matched
  2/292 for 0xA5, 1/135 for 0xAE).
- `a5_context_probe.py` -- `ContextDetector` (subclass of the user's
  `MARCPolyglotDetector`) adding English / Sanskrit-IAST / Arabic / Greek /
  French transliteration profiles, scoring record text + the garbled word
  itself (x4) + the 008/041 prior, then applying a language -> (mark, letters,
  how far back) table. Compares against the current rule per occurrence.
- `b2_context_probe.py` / `b2_gold_check.py` -- 0xB2 (whole destroyed vowel):
  lexicon vowel-fill (a/o/u in the gap) and hand-labelled-gold accuracy check.

**Findings, by finding (not by script):**

1. **Detector caveats.** The unmodified detector has no English profile (plain
   English scores as Chinese Pinyin / Japanese Romaji), its French profile's
   4+ letter "trigrams" can never match a 3-letter window, and short MARC
   fields give 0.4-0.7 confidence. It is only trustworthy when it says
   German. Language alone cannot pick a mark: 43 German-detected 0xAE fields
   are confirmed *dot below* (Arabic/Akkadian words inside German records),
   so any table must be per (byte, language), never per language.
2. **Confidence must be top-vs-runner-up among the transliteration classes,
   with English excluded as a competitor.** English score grows with record
   length, and "English record containing a Sanskrit word" is the normal
   case. Share-of-total confidence diluted every clear winner.
3. **The garbled word itself must carry evidence before any mark is applied.**
   Record topic alone produced false `s` -> ṣ fixes: in `.b14669675` the
   junk is a mangled *apostrophe* in English prose ("psalm's place"), not a
   dot below. Added a word-evidence gate (`MIN_WORD_EVIDENCE`) and the same
   gate lets a Sanskrit/Arabic word beat a French/Greek *blocking* record.

**0xA5 result** (292 occurrences, `--min-confidence 0.6`, hand-checked):
244 existing fixes unchanged; **14 new fixes** (11 Sanskrit, all correctly
on the `r` -- smṛti, saṃskṛti, kṛtaḥ, prabhṛta; 2 Muḥammad; 1 *partial*
"aḍḥawiyya" where the real word needs a second mark on `h`); **7 existing
fixes blocked, all genuine false fixes in current output** -- French
"r<byte>le" (rôle, 4), "jr<byte>me" (Jérôme, 2) and Greek
"Rōmaiokatholikōn" are being turned into "ṛ" today. 27 unresolved (mostly
Greek, where the byte is a destroyed long vowel, correctly left alone).
Dial: at 0.8 only 1 new fix; 0.7 -> 2; 0.6 -> 14. The Sanskrit/French
profiles were tuned on these same 292 records, so expect retuning.
**Assessment: 0xA5 alone is not worth building** (~21 occurrences changed
across a ~400,000-record corpus). The 7 false fixes are the real find; a
cheap 008/041 French/Greek `r` skip would remove them but would also lose
legitimate Sanskrit fixes in French-language records (e.g. Bhartṛhari,
`.b11073056`, `fre`) unless the word-level check is kept.

**0xB2 result** (752 gapped words in `GTU_bibs_0xB2_sample.mrc`, 602 with a
single gap). Language *does* help here and the volume is an order of
magnitude larger than 0xA5. Measured against hand-labelled gold
(`b2_gold_check.py`; **labels are the assistant's German knowledge, no word
list exists on this machine; ~33 occurrences left UNSURE, 27 unlabelled, so
read the percentages as estimates, probably optimistic**):

- Of 340 occurrences labelled German: **268 (78.8%) are an o-umlaut that
  "fill the gap with ö" gets exactly right**; 2 are ü (Bühler, Hübsch); **70
  (20.6%) are DISPLACED** -- the vowel belongs one position over from the gap
  (e.g. `relig_ise` is "religiöse", `Rme_r` "Römer", `Gt_tingen` "Göttingen"),
  so filling the gap gives garbage; that needs a different mechanism.
  Among the 270 clean words, ö is right 99.3% (no ä seen among labelled).
- **Gating on 008=`ger` alone is not enough:** 25/342 (7.3%) of single-gap
  words in `ger` records are not German at all (Tamil n-macron, Arabic,
  etc.), and 43 real German occurrences sit in *non-ger* records
  (Göttingen, Schröter, Jörg, Grözinger...) -- the same word-evidence gate
  as 0xA5 is required, not just the language code.
- The corpus lexicon vowel-fill accepted 127 words at full confidence (124 ö,
  3 a) but **4 were contradicted by gold** ("at_" -> "atä" and "R_m" ->
  "röm", both in English records), and "mächte" is ambiguous with "möchte".
  Lexicon coverage is also thin ("religiöse", "gehört", "Königsrahmen" absent).
  So even the lexicon tier needs the language/word gate.

**Cost of building this properly:** roughly 250k-450k tokens to ship 0xA5 +
0xB2 in `marc_repair.py` (the fixer works one subfield at a time; this needs
whole-record text + 008/041 plumbed through), then ~50k-100k per additional
byte. Roughly half the machinery (scoring, confidence dial, word-evidence
gate, per-language blocking, probe-then-compare workflow) is generic;
profiles, mark rules and the hand-check are per byte.

**Recommendation (assistant's, not yet approved by the user): build 0xB2
only, gated, defaulting to off; skip 0xA5 for now.**

- **0xB2 tiers:** (1) *gate* -- apply nothing unless the garbled word itself
  carries German evidence (008=`ger` alone is not enough: 7.3% non-German
  inside `ger` records, 43 real German words in non-`ger` records); (2) a
  lexicon-confirmed vowel; (3) otherwise default to ö (~99% on the clean-gap
  words I labelled); (4) anything else left for `suspect_marc8_escape`
  review. Behind a confidence setting, default off. Reach: roughly a third
  of all 0xB2 occurrences, all unfixed today. Estimated 150k-250k tokens.
- **Skip 0xA5:** ~21 occurrences changed in ~400,000 records, and its
  profiles were tuned on those same records. If 0xB2 is built, the
  French/Greek block (removing the 7 false `r` -> ṛ fixes) comes almost for
  free -- add it then, not before.
- **Park the DISPLACED shape, don't forget it:** ~20% of German 0xB2, plus
  0xA5's Sanskrit `t`/`a` leftovers and 0xAE's Ninḥursag. Language-independent
  and needs its own design (move the mark, don't fill the gap). Take it up
  after 0xB2 ships.
- **Before shipping:** (a) run the on/off transcoded-output diff on every
  affected record -- not done for anything in this experiment; (b) have a
  German reader check the gold list in `b2_gold_check.py` -- the labels are
  the weakest link, treat the 99% as an estimate until then.
- **Stop condition:** if the diff shows the decoder-state regression on more
  than a few records, drop it; `.b18157713` already shows it can make a fix
  worse than doing nothing.

**Open decision for the user:** start on 0xB2, or merge this research branch
first and decide later.

**Outside lexicon: DONE as research (2026-10-07). Result and recommendation.**
Built from `wngerman` (Debian, GPL-2+; `apt-get download` + `dpkg -x`, not
installed) + LC subjects + LC names (`id.loc.gov/download/authorities/
{subjects,names}.skosrdf.nt.gz`, 100 MB / 2.6 GB). All data lives in
`working/outside_lex/` (gitignored, not in the repo). Reproduce, from the repo
root in the venv: `lang_lexicon_probe.py lc-words --nt-gz <file> --out
<words.txt>` for each LC file, then `lang_lexicon_probe.py merge --base
working/lexicon_GTU.pkl --wordlist <ngerman|words.txt> --out <new.pkl>`
(chain `--base` to stack lists), then `b2_context_probe.py --cache <new.pkl>
--min-support N --min-length L` and `b2_gold_check.py`. Word-list words count
1 each, so `--min-support 2` throws most of them away unless two sources agree.

Records (of 396 with 0xB2) that would leave the human-review list, i.e. every
0xB2 occurrence in them is in an accepted word; gold = assistant's labels, no
German reader:

| lexicon | support 2 | support 1 |
|---|---|---|
| corpus only | 48 | 58 |
| + wngerman | 52 | 93 |
| + wngerman + LC subjects | 56 | 95 |
| + wngerman + LC subjects + LC names | 76 | 102 (12 gold-contradicted) |

With the full lexicon, `--min-length 4 --min-support 2` clears **76 records
with 0 gold contradictions** (length counts the gap; short fragments such as
`gr_` -> `grü` were the bad fills). Support 1 + length 4: 96 cleared, 2
contradicted (`h_al` -> `hääl`, Estonian). Surname ambiguity (`K_hler`,
`B_hler`) is unresolvable by any lexicon. Cost if built: lookups ~0.05 us
(negligible); full lexicon is a 31 MB pickle, 2.9 s load, ~487 MB RSS, so a
shipped version needs a trimmed file, and it should load only when the feature
is on. Record-level language detection cost is unmeasured.
**Not built into `marc_repair.py`.** The user wants the ~76 fewer reviews but
no German reader will check the fills, so the unverified-fill risk stands
(cleared records are not reviewed afterwards). If built: German-evidence gate,
length 4, support 2, default off, plus the on/off transcoded-output diff.

**0xB2 fixer BUILT (default off) -- `fix_marc8_b2_umlaut` in `marc_repair.py`.**
Enabled only by `--b2-lexicon PATH`; category `fixed_marc8_b2_umlaut`
(INFORMATIONAL). Lexicon = `lang_lexicon_probe.py trim --base
working/lexicon_GTU_de_sub_names.pkl --out working/b2_lexicon.json` (4.4 MB JSON
of only the a/o/u-umlaut variants, 127k keys; not in the repo, source licences).
Gates: record 008/041 says `ger`; word length >= 4 (gap counts); support >= 2;
>= 80% agreement; <= 2 gaps. Output is ANSEL diaeresis + vowel, transcoded
normally. Capital only at a sentence-ish word start (not after a hyphen) or in
an all-caps word.

**Build tool for `working/b2_lexicon.json`: `tools/context_probe/build_b2_lexicon.py`
(added 2026-10-08).** Orchestrates the steps above end to end (`build` the
corpus-only pickle, optionally fetch+merge `wngerman`/LC subjects/LC names,
`trim` to the final JSON) instead of running each `lang_lexicon_probe.py`
subcommand by hand; see the HOWTO below for usage. Every step is skipped if
its output already exists (resumable after an interrupted LC-names download),
and nothing it produces is committed -- same gitignored-`working/` reasoning
as the hand-run version. Verified against the pickles already on disk from
this session: re-running it with all three sources reproduced
`working/b2_lexicon.json` byte-for-byte (126,870 keys).

**Measured, 2026-10-08: per-source contribution to the lexicon, isolated.**
The table above only tested sources added cumulatively in a fixed order
(corpus -> +wngerman -> +wngerman+subjects -> +wngerman+subjects+names), so
it couldn't show what LC alone (skipping `wngerman`, which is GPL-2+, unlike
LC's US-government-work data) would buy. Using the word lists already cached
from building the table above (no new downloads), ran
`b2_context_probe.py --min-support 2 --min-length 4` against three lexicons
directly (a different metric than the table's "records cleared" --
`accept`-tier *occurrence* count, same script/settings across all three, so
comparable to each other):

| lexicon | accepted occurrences |
|---|---|
| corpus only | 123 |
| + LC subjects + LC names (no wngerman) | 147 |
| + wngerman too (full, as shipped) | 199 |

**`wngerman` is the bigger of the two outside sources here (+52 over
corpus-only), not the smaller one** -- LC alone only adds +24. This cuts
against what the cumulative table above suggests (LC names looked like the
dominant contributor there, going from 56 to 76 records). LC-only is a real
option if the `wngerman` licence is a blocker (`build_b2_lexicon.py
--no-wngerman`), but it leaves roughly a quarter of the currently-accepted
guesses on the table, not a negligible slice. Not re-measured against the
gold list (same "no German reader" caveat as everywhere else in this
section) -- this only shows reach, not whether the extra `wngerman`-sourced
accepts are correct.

**The "no other escape junk left in the subfield" bail-out was dropped
(2026-10-08, user's decision -- see "Effect of relaxing the German gate / junk
rule" below).** It existed because the first version *did* trigger a
decoder-state regression on 2 subfields (`=505` lost "mmigkeit", and `=520`):
both were already corrupted elsewhere, and the fill changed what garbage
appeared. Dropping it raises reach from 89 instances/74 records to **115
instances/92 records** on `working/GTU_bibs_0xB2_sample.mrc` (396 records;
measured directly from the edited code, not a monkeypatch estimate -- see
below, the two numbers don't agree and the measured one is authoritative).
**Re-checked 2026-10-08: no regression reproduces on this sample.** Diffed
old-rule vs new-rule output for all 78 of the 586 `0xB2`-bearing subfields
where dropping the rule actually changes the result (word count, stray
`\x1b` bytes, and U+FFFD replacement chars as the regression signature) --
zero flagged. Every length delta matches exactly the expected shrinkage from
a successful vowel fill; no new garbage anywhere, including by eye in the
three longest/most escape-tangled records (`.b1345665009`, LCCN 2022935593;
`.b1305499147`, no LCCN on file; `.b1351360489`, LCCN 2022940559). The
original `=505`/`=520` finding never recorded record IDs, only field tags and
a word fragment ("mmigkeit"), so it can't be traced back directly -- either
it was specific to an earlier, buggier prototype of the fill algorithm, or it
lives in a record outside this 396-record sample. **Still no German reader**
(see WON'T BE DONE above) to catch a bad fill that isn't a decoder-state
regression (e.g. a wrong-but-plausible vowel) -- this re-check only rules out
the specific garbling failure mode, not correctness in general.
**Records fully cleared of 0xB2 (pre-drop baseline): 46**, not the research
figure of 76: the German gate cost 16 (62 without it) and the junk rule cost
the rest. The "fully cleared" count has not been recomputed since the junk
rule was dropped. Tests: `TestFixMarc8B2Umlaut` updated for the new behavior,
full suite 430 passed/1 skipped, flake8 (max-line 120) clean. Full-corpus run
**not done**; labels are still mine, no German reader.

**For the German reader (review sheet, 2026-10-07). WON'T BE DONE (2026-10-08)
-- the user has no access to a German reader, and these are cataloged-materials
snippets, not running prose, so there's no good substitute reviewer either.**
Distinct words the 0xB2 fixer filled in `working/GTU_bibs_0xB2_sample.mrc` (53
forms; 70 occurrences; full before/after with fields in `working/b2run/diff.txt`,
gitignored; regenerate with the on/off run in the section above). The 99%
gold-label accuracy therefore stays an estimate from the assistant's own German
knowledge, never independently checked, and ships (if it ships) on that basis.

Behörden, Bischöfliche, Bischöflichen, Böhmen, Erstveröffentlichungen, Erörterung, Fröhlich, Frömmigkeit, Förderung, Griechisch-römische, Göttingen, Göttlichem, Höhepunkt, Jörg, Könemann, Könige, Körper, Körperschaften, Lösung, Schöningh, Schöpfung, Sprichwörter, Strömungen, Tröndle, Töpelmann, Versöhnung, Veröffentlichungen, Völkern, befördern, bischöflichen, böhmische, böhmischen, deutsch-französische, eröffnen, eröffnet, gehören, gehört, göttliche, höchsten, können, könnte, nördlichen, persönlich, persönlichen, zeitgenössische, zeitgenössischen, zwölf, Öffentliche, öffentliche, öffentlichen, öffentlicher, ökumenische, ökumenischen

**Known weak spot (stands either way):** surnames like K_hler/B_hler are not
resolvable by any lexicon.

**Tomorrow:** (1) [WON'T BE DONE] get this list checked by a German reader;
(2) relaxing the German gate / junk rule effect measured instead on the
2026-10-08 session -- see "Effect of relaxing the German gate / junk rule"
below; (3) then PR (not opened, per user); (4) not worth doing: displaced-mark
mechanism, more lexicon sources, more byte work.

**Effect of relaxing the German gate / junk rule (measured 2026-10-08).**
First pass used monkeypatching (`_b2_record_is_german` forced True for gate
off; a copy of `_b2_fill_text` with the bail-out deleted for junk rule off)
run through the full `marc_repair.py` CLI pipeline (a standalone call to
`fix_marc8_b2_umlaut` undercounts -- fixer order changes which subfields still
have "other escape junk" left when this one runs) over the 396-record
`working/GTU_bibs_0xB2_sample.mrc`:

| config (monkeypatch estimate) | instances | records touched |
|---|---|---|
| baseline (both rules on) | 89 | 74 |
| gate off, junk rule on | 106 | 90 |
| gate on, junk rule off | 135 | 111 |
| both off | 135 | 111 |

This suggested relaxing the junk rule alone already reached the same ceiling
as relaxing both, so the user chose to **drop the junk-rule bail-out from
`_b2_fill_text` for real** (code change, not a monkeypatch; gate left in
place) -- see the "no other escape junk left" paragraph above for the actual
diff. Re-measuring the real edited code against the baseline gave different,
authoritative numbers that disagree with the monkeypatch estimate above (the
monkeypatch copy of `_b2_fill_text` apparently didn't reproduce something the
real pipeline does -- not tracked down, the real numbers are what matters):

| config (real code, re-measured) | instances | records touched |
|---|---|---|
| baseline (pre-edit) | 89 | 74 |
| junk rule dropped (shipped) | 115 | 92 |

So the actual gain from this change is +26 instances / +18 records, not the
monkeypatch's +46/+37. The German gate was left in place -- it was not
re-measured against the new junk-rule-dropped code to see whether it's still
adding reach (the monkeypatch estimate said it would add nothing once the
junk rule was off, but given the discrepancy above, treat that as unverified
too). **Not independently re-verified against the gold list** (see WON'T BE
DONE above) -- this only shows reach, not whether the extra fills are
correct, and specifically does not re-check the two `=505`/`=520` subfields
that regressed under this exact change when it was first tried (see above).
Scratch outputs in `working/b2run_relax/` and `working/b2run/*_v2*`
(gitignored).

**State at end of session (2026-10-07):** branch `context-language-disambiguation`,
only research tools changed; `marc_repair.py` untouched. Pickles in `working/`:
`lexicon_GTU.pkl` (corpus only), `_de`, `_de_sub`, `_de_sub_names` (full; use
this one). `working/b2_context_probe.tsv` is overwritten by every probe run, so
re-run the probe with the settings you want before `b2_gold_check.py`. Run
commands via `wsl.exe -e bash -lc 'cd ~/scratch/marc_repair && source
venv/bin/activate && ...'` (no `python`; needs the venv for `pymarc`). Gotcha
hit: a background `curl` started inside a subshell is killed when the WSL shell
exits and its "completed" notice is the wrapper's, so a partial 2.6 GB download
looked finished -- run it as the tracked background command itself, resume with
`curl -C -`, and verify with `gzip -t`. Decisions open for the user: whether to
build the gated 0xB2 fixer (user wants the ~76 fewer reviews; no German reader
will check fills), and merge-vs-continue for this branch. Untried: frequency
list for ties (source 3), Sanskrit/Arabic (source 5), `wfrench` etc.

**(Historical plan) outside lexicon, decided with the user.**
Coverage, not logic, was the bottleneck in every lexicon test (matched 2/292
for 0xA5, 1/135 for 0xAE, 127/752 for 0xB2; "religiöse", "gehört",
"Königsrahmen" absent). A corpus-only lexicon can only know words the corpus
spells correctly. An outside lexicon should also *lower* confidence on
ambiguous pairs ("möchte"/"mächte" would both match), which is desirable.
It will **not** fix the DISPLACED shape (that is a position problem, not a
vocabulary one). Sources, cheapest first -- **none of these package names,
URLs or licences have been verified; check each before relying on it:**

1. *Clean UTF-8 already on disk:* `sample_files/Bucknell00000448.mrc` and any
   other UTF-8 export. Free, and matches the catalog's own vocabulary. Do
   **not** use this project's repaired outputs (circular).
2. *Debian `w*` word-list packages* (e.g. `wngerman`, `wfrench`, `wpolish`,
   `wportuguese`; one `apt install` each, plain word lists, no setup). No
   frequencies, so ö-vs-ä ties need source 3. Expect some are GPL.
3. *Frequency lists for tie-breaking* (e.g. the Hermit Dave FrequencyWords
   set; licence unchecked): settles "möchte" over "mächte" without hand
   labels.
4. *Library of Congress authority files* (name + subject, free bulk download
   from id.loc.gov): holds exactly the hard cases -- German surnames/places
   (Köstenberger, Grözinger) and transliterated Sanskrit/Arabic names with
   their diacritics, which no dictionary package has. Recommended after 1-3
   if they help.
5. *Sanskrit/Arabic transliteration:* Cologne Digital Sanskrit Dictionaries
   (Monier-Williams, IAST spellings) for Sanskrit; Arabic romanizations come
   mostly from the LC authority files. The only genuinely hard part.

Plan: reuse the existing structure unchanged (flattened-ASCII key -> set of
diacritic spellings, cached as a pickle -- see `build()` in
`tools/context_probe/lang_lexicon_probe.py`); only add loaders (pure Python).
Keep the downloaded data **out of the repo** (licences vary) and commit only
a download-and-build script. First test: German list + frequencies (sources
2-3), re-run `b2_context_probe.py` / `b2_gold_check.py`, and compare against
the corpus-only result above (127 accepted, 4 contradicted by gold; 20.6%
displaced). Add the LC authority files only if that moves the numbers.
Estimated 20k-40k tokens for loaders + rebuild.

**Other next steps (not started):**

1. (Superseded by the recommendation above.) Decide whether to build for
   0xB2 only: tiers = lexicon-confirmed vowel
   (with the word/language gate) > German-evidence default ö at lower
   confidence > leave alone. Estimated reach ~ 60-70% of the German 0xB2
   occurrences (the clean ones), i.e. roughly 30-40% of all 0xB2.
2. Separately consider the DISPLACED shape (20% of German 0xB2, and 0xA5's
   Sanskrit `t`/`a` leftovers, 0xAE's Ninḥursag) -- a "move the mark to the
   right letter" mechanism is the other big gap, language-independent.
3. Whatever is built: **every fix still needs the on/off transcoded-output
   diff** (see "Lessons learned" -- `.b18157713`, `.b12911008`); that has not
   been run for any of the proposals above.

`marc8-diacritic-fix` was merged into `main` via
[marc-repair#1](https://github.com/marnold-ebsco/marc-repair/pull/1) and the
branch deleted (local + remote).

Since the merge, `marc_repair.py` (tip of `main`) was run against two
previously-untried corpora as a sanity check, both clean -- no new patterns,
no code changes needed:

- `sample_files/nashvillestate_bibs_202693.mrc` (48,017 records): 48,017/48,017
  written, 0 unfixable. 4 `fixed_marc8_diacritic`, 3 `suspect_marc8_escape`,
  all already-familiar shapes.
- `sample_files/Bucknell00000448.mrc` (1,000 records): 1,000/1,000 written, 0
  unfixable. Both MARC-8 diacritic categories are 0 records -- expected, this
  corpus is already declared UTF-8 (leader byte 9 == `'a'`).

A new `0xA4` override (`z` -> combining dot below, 21 occurrences, found
while investigating the 60-record sample `working/GTU_bibs_0xA4_sample.mrc`
-- see `_MARC8_BARE_COMBINING_BEFORE_OVERRIDES` in `marc_repair.py`) was
added, and a prior session's full-corpus re-run against `working/GTU_bibs.mrc`
showed `fixed_marc8_diacritic` completely unchanged at 149,150 instances /
64,340 records (log: `working/GTU_bibs_repaired_0xa4fix_log_20261007T181616Z.log`),
which looked like the override wasn't reachable in the real pipeline at all.

**Root-caused and confirmed working -- it's a counting-metric blind spot,
not a code bug.** `fixed_marc8_diacritic` logs one `detail` string per
*subfield*, not per mark fixed (`marc_repair.py`, where
`fix_marc8_diacritic_escapes`'s per-field loop builds `detail`), and the
corpus-wide "instance" count is just the count of those detail strings. All
21 real `z` occurrences happen to sit in a subfield that *already* has a
different diacritic fixed elsewhere in the same subfield (e.g. the `h` in
"Muòtahharåi, Murtaza..." was already being fixed by the unrelated `0xA3`
row) -- so the `z` override firing correctly doesn't add a *new* detail
line, it just enriches one that was already going to be logged and counted.
The instance count was therefore never going to move, override or no
override; it was the wrong signal to verify against.

Confirmed directly: diffing `working/GTU_bibs_0xA4_sample.mrc`'s repaired
text with the override enabled vs. monkeypatched off shows the instance
count identical (128 either way) but exactly 21 real text differences, all
correct dot-below insertions (`Riza` -> `Riòza`, `Murtaza` -> `Murtaòza`,
`qaziyah` -> `qaòzåiyah`, etc.) -- the override is reachable and correct in
the real multi-escape pipeline, not just the hand-built unit-test fixtures.
Regex ordering (suspect #1 from the prior entry) turned out fine: the
escape-payload regex's "before" letter anchors on the letter *after* the
`0xA4` byte in these records (since `0xA4` itself is excluded from what the
junk-run can swallow), so it never consumes the `z`+junk+`0xA4` text before
the bare-combining regex gets to it.

Code change + regression tests committed (`_MARC8_BARE_COMBINING_BEFORE_OVERRIDES`
and its two tests in `tests/test_marc_repair_bib.py`); docs
(`docs/MARC8_DIACRITIC_HANDLING.md`) updated to describe the override, the
~48 remaining unfixed `0xA4` occurrences (destroyed-vowel corruption, not a
wrong-mark case), and this counting-metric caveat for anyone re-verifying a
similar override against a full-corpus run in the future.

Re-ran the same disambiguation methodology against `0xA8` (one of the three
bytes the HOWTO below flagged as not yet re-checked for a hidden second
override). Built `working/0xA8_occurrences.tsv` and
`working/GTU_bibs_0xA8_sample.mrc` via `working/find_0xa8_occurrences.py`
(adapted from the `0xA4` script, still on disk, gitignored): 260 occurrences
across 154 records in `working/GTU_bibs.mrc` (not 324 -- same stale-count
effect the close-escape fix already explained for `0xA4`'s 93 -> 79). 248
(95%) are the already-confirmed `a`/`e`/`u` + ogonek. **Unlike `0xA4`, the
remaining 12 did NOT turn up a new confirmed override** -- they split
across four small groups, none clean or large enough: 4 `c` occurrences
need cedilla, but `0xA7` already produces that unrestricted, so adding it
under `0xA8` would be redundant, not new; 3 (`o`/`o`/`i`: "Muñoz",
"Quiñones") are the destroyed-base-letter failure mode (missing "ñ" two
letters before the junk, not attachable to the letter immediately before
it, same shape as ~48 of `0xA4`'s leftovers); 1 `o` occurrence needs no
diacritic at all ("...series of Verhandelingen"); and the last 4
(`s`/`S`/`r`/`d`/`h`) are a singleton cedilla, a singleton dot below, and
three occurrences in one title too garbled to confirm. No code change --
`0xA8` stays restricted to `a`/`e`/`u`, same as before this check.
`marc_repair.py`'s `0xA8` comment block and
`docs/MARC8_DIACRITIC_HANDLING.md`'s table updated with the corrected
counts and this breakdown. Re-ran the same methodology against `0xA5`. Built
`working/0xA5_occurrences.tsv` and `working/GTU_bibs_0xA5_sample.mrc` via
`working/find_0xa5_occurrences.py` (gitignored): 291 occurrences across 197
records (not 318 -- same stale-count effect). 251 (86%) are the
already-confirmed `r` + dot below (Sanskrit vocalic r). Found a tempting
but ultimately rejected second case -- see the "Lessons learned" entry
right below this one for the full story, since it's the most important
thing from this session. Short version: `h` (3 occurrences) reconstructs
correctly in isolation as the same dot-below mark (Arabic
"Muh<byte>ammad" -> "Muḥammad"), and was briefly added to
`_MARC8_BARE_COMBINING_RESTRICTED_BEFORE["\xa5"]` (`"r"` -> `"rh"`) with
two regression tests -- then reverted after discovering it makes one real
record's transcoded output *more* garbled, not less, due to an unrelated
decoder bug interacting with it. `_MARC8_BARE_COMBINING_RESTRICTED_BEFORE["\xa5"]`
is back to `"r"` only; no net code change for `0xA5` this session beyond
the comment-block/doc updates recording this investigation. The other 37
occurrences (before-letters `t`/10, `n`/10, `s`/7, `a`/6, `g`/1, `d`/1,
`b`/1, `m`/1) don't cluster into another confirmed mark either: `t`/`a`
(Sanskrit, 16 total) are the vocalic-r mark landing after an entire
following consonant-vowel run instead of right after the actual "r" it
belongs to (e.g. "Smrt<byte>i" is "smṛti", not "smrṭi" -- 2 letters too
far for this before-letter mechanism to reach); `n`/`s` (Greek, 17 total)
are a destroyed vowel before the junk, not a missing mark on the letter
immediately before it (e.g. "Orthodoxn<byte>" is "Orthodoxōn", missing
the whole "ō" -- same destroyed-base-letter failure mode as `0xA4`'s
leftovers); the last 4 (`g`/`d`/`b`/`m`, one each) are singletons too
ambiguous to confirm. `marc_repair.py`'s `0xA5` comment block and
`docs/MARC8_DIACRITIC_HANDLING.md`'s table updated to record all of this.
Per explicit instruction, the full `working/GTU_bibs.mrc` corpus was never
re-run this session -- all verification stayed scoped to the 197-record
`working/GTU_bibs_0xA5_sample.mrc`.

Re-ran the same disambiguation methodology against `0xAE`, the last of the
three bytes flagged by the HOWTO below as not yet re-checked. Built
`working/0xAE_occurrences.tsv` and `working/GTU_bibs_0xAE_sample.mrc` via
`working/find_0xae_occurrences.py` (adapted from `find_0xa5_occurrences.py`,
still on disk, gitignored; double-checked `TARGET_BYTE = '\xae'` by hand
given the sed-substitution trap documented below). 135 occurrences across
83 records in `working/GTU_bibs.mrc` -- **not stale**, unlike `0xA4`/`0xA5`/
`0xA8`: this is the same 135 already recorded in the existing comment
block, both before and after the close-escape fix. 109 (81%) are the
already-confirmed `h`/`H` + dot below. The remaining 26 (`i`/9, `a`/8,
`u`/4, `o`/3, `s`/2) did **not** turn up a new confirmed override -- they
split across five small, unrelated groups, same shape as `0xA8`'s
leftovers: 4 Italian occurrences ("più", "Gesù") are the destroyed-base-
letter failure mode already confirmed for `0xA4` (the whole "ù" is
missing, and the mark needed is a grave accent anyway, not dot below); an
~11-occurrence Akkadian/Hittite/Sumerian cluster ("Ninhursag", "Hattusa",
"Harranu", "Asalluhi", "Kalhu", "Hirbet", "Hissar") has a real dot-below
mark that belongs on the "h" one letter *before* the matched before-letter
(e.g. "Ninhu<byte>rsag" is "Ninḥursag", mark on "h" not "u") -- this
before-letter mechanism can't reach one letter further back, the same
"wrong distance" shape as `0xA5`'s Sanskrit `t`/`a` leftovers, just in the
other direction; a small Germanic/Scandinavian cluster ("understödd",
"Größeres", "Knauß") needs umlaut/o-with-stroke, not dot below -- a
different mark, not enough volume to add as a second override; a 5-
occurrence al-Ghazali/"Ihya" cluster is genuinely ambiguous (no diacritic
on "al-Ghazali" in this transliteration; "Ihya" needs the unrelated hamza
byte instead); and the last 2 are one "où il" (needs grave, not dot below)
and one singleton too garbled to confirm. No code change -- `0xAE` stays
restricted to `h`. `marc_repair.py`'s `0xAE` comment block and
`docs/MARC8_DIACRITIC_HANDLING.md`'s table updated with this breakdown.
Per the same instruction as `0xA5` above, the full `working/GTU_bibs.mrc`
corpus was not re-run this session -- verification stayed scoped to the
83-record `working/GTU_bibs_0xAE_sample.mrc`. **All three bytes the HOWTO
originally flagged (`0xA8`, `0xA5`, `0xAE`) are now re-checked.**

Ran the original disambiguation pass (HOWTO steps 1-4 below, first time
for both bytes -- unlike `0xA8`/`0xA5`/`0xAE`, neither ever got a first
confirmed before-letter) against `0xB2` and `0xB3`, the next two of the
four bytes in the "Investigated, not implemented" table
(`docs/MARC8_DIACRITIC_HANDLING.md`). Neither is in
`_MARC8_BARE_COMBINING_BYTES`/`_MARC8_BARE_STANDALONE_BYTES`, so
`_MARC8_BARE_COMBINING_RE` doesn't match either byte -- built a one-off
scan regex for each from the same reusable pieces
(`_MARC8_NOT_ESCAPE_DESIGNATOR_LOOKBEHIND` + `([A-Za-z])` +
`_MARC8_STRAY_ESCAPE_JUNK_NONEMPTY` + the literal byte) instead of
reusing `_MARC8_BARE_COMBINING_RE` itself:
`working/find_0xb2_occurrences.py` / `working/find_0xb3_occurrences.py`
(gitignored, adapted from `find_0xae_occurrences.py`). Built
`working/0xB2_occurrences.tsv` + `working/GTU_bibs_0xB2_sample.mrc` (832
occurrences / 396 records, not the old 745/398 -- same close-escape-fix
stale-count effect as `0xA4`/`0xA5`/`0xA8`, just upward this time) and
`working/0xB3_occurrences.tsv` + `working/GTU_bibs_0xB3_sample.mrc` (48/20,
not 49/21). Both sample files verified to parse clean (0 unresolved).

**`0xB2`: no new override.** No before-letter dominates (highest is `n`
at 15%), confirming the "murky" read. The largest clusters are German
words (Göcke, Körper, könnte, Köhler, Königsherrschaft, göttliche,
zeitgenössischen, religiöser, persönliche) needing an *inserted* `ö` as
its own letter, not a mark fused onto the matched before-letter --
confirmed via `pymarc.marc8.marc8_to_unicode` on the ANSEL byte order
(mark-byte-before-base-letter-byte): attaching diaeresis to `G` decodes
to `G̈`, not `ö`, so the mechanism's `mark + before` single-combined-
character model structurally can't produce this shape (same "destroyed
base letter" failure mode as roughly half of `0xA4`'s and part of
`0xA8`'s leftovers, just the dominant pattern here). `0xB2` stays
unimplemented.

**`0xB3`: no new override.** 44 of 48 occurrences (~92%) are Dead Sea
Scroll/philological sigla superscripts ("1QIsa"+byte, "4QSamuel"+byte,
etc.) -- not a diacritic at all, would need a letter-to-superscript
substitution mechanism this tool doesn't have. The one exception
(record `.b1122034x`, "Ye"+byte+"n Presbyterian", alternate title for
"Yŏn Presbyterian") tested against the breve hypothesis and decoded to
`ĕn Presbyterian`, not `Yŏn` -- the captured before-letter (`e`) isn't
the vowel (`o`) the title actually needs the mark on, same mismatch
shape as `0xA6`'s already-documented "współczesną" case. Genuinely
ambiguous, same conclusion as before. `0xB3` stays unimplemented.

Both bytes' table rows and new detail writeups in
`docs/MARC8_DIACRITIC_HANDLING.md` updated with the corrected counts and
this breakdown. No code change for either byte -- nothing to re-run
against the full corpus.

**`0xA6` resolved -- new "c" -> cedilla restriction added; `0xC1` still
open.** `0xA6` re-disambiguation (`working/find_0xa6_occurrences.py`,
adapted from `working/find_0xb3_occurrences.py`'s one-off-regex
approach, since `0xA6` isn't in `_MARC8_BARE_COMBINING_BYTES` either):
265 occurrences across 144 records (not the old 267/145 -- same small
stale-count correction as `0xA4`/`0xA5`/`0xA8`). Grouping by
before-letter (`working/0xA6_occurrences.tsv`) surfaced a **materially
different picture than the old writeup**, which only ever discussed `a`
(Polish ogonek) vs `c` (Portuguese/French cedilla) as two
roughly-comparable readings: `s` (79) and `t` (62) are actually the two
largest groups, both overwhelmingly Romanian cedilla/comma-below words
never mentioned before ("Bucureşti", "Timişoara", "Colecţia",
"Mehedinţu", "Nopţi", "şi" = "and") -- `c`/`s`/`t` combined are 214/265
(81%) and all three read correctly under the *same* ANSEL cedilla byte
(`\xf0`) already used for `0xA7`'s French `ç`, not a new mark. `a`
(Polish ogonek, the byte's originally-documented reading) is only 6
occurrences (2%) -- a minority, not a comparably-sized alternative as
the old writeup implied.

**Full-field diff (fix on vs. off, every record in
`working/GTU_bibs_0xA6_sample.mrc`, `working/diff_0xa6_override.py`,
gitignored) split the `c`/`s`/`t` cluster: `c` confirmed clean, `s`/`t`
rejected.** `c` alone: 57 changed subfields, zero regressions, all
correct ("Franc<esc>...>ois" -> "François", "revelac<esc>...>a..." ->
"revelação") -- added as a new `_MARC8_BARE_COMBINING_RESTRICTED_BEFORE`
entry (`"\xa6": "c"`) with mark `\xf0` in `_MARC8_BARE_COMBINING_BYTES`,
plus three regression tests in `TestFixMarc8DiacriticEscapes`
(`tests/test_marc_repair_bib.py`) mirroring the `0xA4`/`z` ones. `s`/`t`
tried the same way and **rejected** -- the full diff found two real
regressions `c` didn't have:

- An off-by-one mark placement (record `.b11564295`, field `710 $b`,
  "Serviciul Relati<esc>...>i Externe Bisericest<esc>...>i"): the
  second occurrence looks like a clean `t`-attach ("Bisericeşti") but
  the literal text already has both an `s` *and* a `t` before the junk
  where the correct word only has `ş` (one letter) -- the mark
  actually belongs on the `s`, one position before the captured `t`,
  same off-by-one shape as `0xAE`'s Akkadian cluster. Confirmed this
  isn't just this one word's quirk: record `.b13281069` has a
  *different* "Bucureşti" occurrence with the identical one-letter
  shift, even though every other "Bucureşti" in the corpus (several of
  them) fixes correctly as a clean `s`-attach -- the underlying
  corruption itself places the junk inconsistently for this shape, so
  no amount of regex refinement makes `t` safe here.
- A decoder-state regression (record `.b12911008`, field `245 $b`):
  applying the trial override there doesn't just misplace one mark, it
  leaves pymarc's `marc8_to_unicode` decoder stuck, turning a long
  stretch of the subfield after the first mark into unreadable
  combining-mark debris -- the same failure mode as `0xA5`'s rejected
  `h` case, just triggered by `s`/`t` instead of `h`.

`s`/`t` stay unfixed. Full-corpus re-run after adding the `c` override
(`--log-full fixed_marc8_diacritic` against `working/GTU_bibs.mrc`):
`fixed_marc8_diacritic` rose from 149,150/64,340 (the figure after the
`0xA4` session) to 149,166 instances / 64,348 records -- a modest
+16/+8, consistent with the same counting-metric quirk `0xA4`'s `z`
override hit (most `c` occurrences share a subfield with an
already-counted fix). `suspect_marc8_escape` stayed at 1,258 records,
unchanged -- expected, since almost every record with a `c` occurrence
also has an unfixed `s`/`t` occurrence still flagging it. `pytest`
(266 passed) and `flake8` both clean. `docs/MARC8_DIACRITIC_HANDLING.md`
updated: `0xA6` moved from "Investigated, not implemented" into the
"Overloaded" table/writeup, with the residual `a`-minority LC-catalog
research kept as a trimmed note under the same byte.

`0xC1`: the standard one-off regex (`working/find_0xc1_occurrences.py`,
same template) found **zero** occurrences -- confirming the original
doc's hedge that this byte's corruption "may not even be the same
mechanism" as the rest of the table; it genuinely isn't reachable by the
stray-junk-before-letter shape at all. A direct raw-byte scan over
`working/GTU_bibs.mrc` (no regex, just `'\xc1' in rec_text`) instead
found 118 occurrences across 55 records, splitting into two unrelated
shapes neither matching the usual mechanism:

- The large majority are a **bare, escape-free two-byte pair**
  (`\xc3\xc1`, i.e. `pymarc.marc8_to_unicode` reads the individual bytes
  as `©` + `ℓ`, which is nonsense) sitting directly in otherwise-plain
  ASCII text with no escape sequence anywhere nearby -- confirmed via
  record `.b14104076`'s `245`: `"SigurÃ°ur HafÃÁo<esc>p+<esc>s<esc>(QB<esc>(Brsson"`,
  an Icelandic name ("Sigurður Hafþórsson") where `Ã°` (`\xc3\xb0`) is a
  genuine, valid UTF-8 encoding of eth (`ð`) but the adjacent `ÃÁ`
  (`\xc3\xc1`) is **not** valid UTF-8 (`\xc1` can't be a continuation
  byte) and isn't the UTF-8 encoding of thorn (`þ` is `\xc3\xbe`, not
  `\xc3\xc1`) either -- the root byte-mapping producing thorn from this
  exact pair is still unexplained, confirmed only by reading the
  reconstructed word, not by any known encoding rule. Leader byte 9 is
  `' '` (declared MARC-8, not UTF-8) for every record checked, so this
  isn't simply an embedded-UTF-8 field.
- The minority matches the original doc's note (`\x1b(QB\x1b(B` close-escape
  fragment immediately before the bare byte, e.g. record `.b10309858`'s
  245: `"...eis ât<esc>(QB<esc>(BÁen texa..."`, Greek "tēn" -- Greek
  before-letters, always right after a close escape with no letter
  between, which `_MARC8_NOT_ESCAPE_DESIGNATOR_LOOKBEHIND` correctly
  excludes as not a real "before" letter since there's nothing for a
  mark to attach to).

**`0xC1` byte-pair investigation, resumed and now closed out.** Scanned
`working/GTU_bibs.mrc` for every `\xc3`-prefixed byte pair (not just
`\xc3\xc1`) to look for a discoverable, consistent table, per the
"Next steps" this entry used to end with. Found one:

```
codepoint = 0xC0 + (second_byte & 0x3F)
```

(the standard 2-byte UTF-8 decode formula, since `\xc3` is `11000011`
-- top bits always contribute `0xC0`.) Confirmed against the three most
common `\xc3`-prefixed pairs, all with **zero escape bytes anywhere
nearby** -- a different shape from this byte's escape-adjacent minority
(see below), and genuinely valid UTF-8 each time:

- `\xc3\xb8` (3,056 occurrences) -> `ø` -- Danish/Norwegian, e.g.
  "KÃ¸benhavn" -> "København", "SlÃ¸k" -> "Sløk".
- `\xc3\xa6` (3,030 occurrences) -> `æ` -- Latin/French ash ligature,
  e.g. "doctrinÃ¦que" -> "doctrinæque", "GroningÃ¦" -> "Groningæ".
- `\xc3\xb0` (92 occurrences) -> `ð` -- Icelandic eth, e.g.
  "SigurÃ°ur" -> "Sigurður" (already noted last session).

**This rules out implementing anything for this byte, rather than
pointing to a fix: `\xc3\xc1` is the one common pair that breaks the
formula.** `0xC0 + (0xC1 & 0x3F)` = `0xC1` (`Á`), but reading the real
occurrences (53 of them, `working/0xC1_occurrences.tsv`-adjacent ad hoc
scan, all still gitignored) shows `\xc3\xc1` needs at least **five**
mutually-incompatible readings depending on the specific word, with no
distinguishing signal between them (no escape adjacency, no
before/after-letter split -- the pair is identical in every case):

- Thorn (`þ`), the plurality: Old/Middle English ("Of Saynte Iohn
  ÃÁe Euangelist" -> "the", "Se ÃÁonne ÃÁisne wealsteal wise
  geÃÁohte" -> "þonne"/"þisne"/"geþohte"), Icelandic ("FriÃ°ÃÁjofs
  saga" -> "Friðþjófs saga", a well-known Icelandic saga title,
  "HafÃÁo<esc>...>rsson" -> "Hafþórsson"), and Gothic ("ÃÁairh
  iohannen" -> "þairh", "through").
- German needing `ü`/`ä`, not thorn at all ("LehrstÃÁhle" ->
  "Lehrstühle", "UniversitÃÁat Hamburg" -> "Universität Hamburg").
- French needing `é` ("Marie-AimÃÁee HÃÁelie-Lucas" -> "Marie-Aimée
  Hélie-Lucas", "Maria JosÃÁe" -> "Maria José").
- A plain ASCII apostrophe, not a diacritic at all ("LukeÃÁs record"
  -> "Luke's", "TillichÃÁs theological" -> "Tillich's", "SugrÃÁivaÂ¿s
  Consecration" -> "Sugriva's" -- note this record has a *second*,
  differently-corrupted apostrophe right next to it, `Â¿`).
- Vietnamese/Sanskrit diacritics that don't reduce to one mark either
  ("ThiÃÁch" -> likely "Thích", "VipasÃÁyana" (6 occurrences, all the
  same `650` subject heading) -> likely "Vipaśyanā" or "Vipassanā" --
  neither confirmed precisely, but neither is thorn).

No before-letter, escape context, or language signal separates these
five readings from each other -- unlike every other overloaded byte in
this file (even `0xB2`'s "murky" German-ö case had a dominant,
explainable cluster). **Genuinely unfixable with this tool's
before-letter/standalone mechanism; no code change.** The minority,
escape-adjacent shape already documented (`\x1b(QB\x1b(B` immediately
before the bare byte, Greek/Sanskrit transliteration macrons, 65 of the
118 total raw occurrences) is unaffected by this and stays correctly
excluded by `_MARC8_NOT_ESCAPE_DESIGNATOR_LOOKBEHIND`, same as before.
A handful of leftover occurrences in this minority group turned out to
be unrelated noise while reading through them: three LC call-number
cutters with a trailing bare `Á` and no escape nearby at all
(`.b11196580`'s `090 $b`, `".B731Á"`), three records with long runs of
repeating `<esc>b8<esc>sÁ` fragments that look like unrelated binary/
OCR corruption rather than this tool's escape-junk shape, and one
genuinely correct, uncorrupted `Á` (`.b19526350`, "Álvaro Cancela" --
a real Spanish name, no escape or `\xc3` prefix adjacent at all,
confirming `0xC1` really is a valid standalone ANSEL byte that must
stay untouched on its own, same safety argument as every other bare
byte in this file).

`0xC1` stays unimplemented -- `_MARC8_BARE_COMBINING_BYTES`/
`_MARC8_BARE_STANDALONE_BYTES` unchanged. `docs/MARC8_DIACRITIC_HANDLING.md`'s
table and `0xC1` row should be updated with this writeup (replacing the
old "may not even be the same corruption mechanism" placeholder, which
undersold how overloaded this turned out to be) the next time that file
is touched.

**All six bytes from the original "Investigated, not implemented"
table (`0xA6`, `0xBA`, `0xB2`, `0xB3`, `0xC1`, plus `0xAE`/`0xA5`/`0xA8`
from the HOWTO's three) have now been re-checked at least once this
project. `0xA6` is the only one that yielded a new, safe override
(restricted to "c"); every other byte's "no safe fix" verdict has been
confirmed, several more than once.** There is no open byte-disambiguation
item left in this file as of this entry.

**Lessons learned this session (read before starting `0xAE`):**

1. **A reconstructed word reading correctly in isolation is necessary but
   not sufficient evidence for a before-letter addition.** The `0xA5`/`h`
   case above is the concrete example: `"Muh<byte>ammad"` -> `"Muḥammad"`
   is unambiguous on its own, but the third "h" occurrence in the full
   corpus (`working/GTU_bibs.mrc`, record `.b18157713`, "Yah<byte> at
   Elephantine") sits in a `505` subfield that *also* has an unrelated,
   pre-existing escape-designator corruption earlier in the same string
   (an unrecognized `\x1bp+\x1b\xe4...` sequence from a mis-encoded German
   "Ägyptische"). That corruption leaves pymarc's `marc8_to_unicode`
   stuck in a bad G1 charset state for the rest of the field. Diffing the
   transcoded subfield with the fix on vs. off showed leaving the bare
   `0xA5` byte alone happens to let the decoder resync afterward and
   recover ~100 characters of legible trailing text, while applying the
   *correct* dot-below substitution keeps the decoder stuck and garbles
   that same trailing text instead. **Net effect: the semantically
   correct fix made that specific real record's output worse, not
   better.** Before adding any before-letter to
   `_MARC8_BARE_COMBINING_RESTRICTED_BEFORE` (or any entry to
   `_MARC8_BARE_COMBINING_BEFORE_OVERRIDES`), after confirming the word
   reconstruction in isolation, also diff full-field transcoded output
   (fix on vs. off) for *every* real occurrence in the corpus sample, not
   just the occurrence(s) used to confirm the word -- a low occurrence
   count (3, here) means it's cheap to check all of them by hand.
2. **How to catch this:** monkeypatch
   `_MARC8_BARE_COMBINING_RESTRICTED_BEFORE[byte]` between the old and
   new value, run `fix_marc8_diacritic_escapes` +
   `transcode_marc8_to_utf8` over the byte's `working/GTU_bibs_0x<BYTE>_sample.mrc`,
   and diff every changed record's affected subfield text (not just
   `len(details)` or an instance count -- those can both stay identical
   while the actual rendered text gets worse, same caveat as the
   `0xA4`/`z` instance-count blind spot documented earlier in this file).
3. A sed-based script adaptation (`find_0xa4_occurrences.py` ->
   `find_0xa5_occurrences.py`) missed `TARGET_BYTE = '\xa8'` because sed
   was told to replace the text `0xa8`/`0xA8`, and the byte literal
   `\xa8` doesn't contain that substring. Caught only because the
   resulting before-letter distribution looked implausible (majority
   class flipped entirely). Double-check every constant in a copied
   script, not just the ones matching the obvious find/replace pattern,
   when adapting one of these one-off scripts for a new byte.

`0xAE` still hasn't had this re-check done -- see the HOWTO below.

## Open items (deferred, not forgotten)

1. **`suspect_marc8_escape` stays detect-only.** ~52,847 distinct records (258,202
   findings) hit this in `working/GTU_bibs.mrc`; same-file corpus lookup only
   resolves ~2% of them to a specific word. Fixing the rest would need an
   external-authority lookup (LC/OCLC by the record's own `010`/`035`
   identifiers) -- a different kind of feature than anything else in this tool
   (network calls, rate limits, auth) and needs its own scoping conversation.
   Decision: not started. A smaller, already-scoped readability improvement to
   this category's log line (collapse the escape run to one `[?]` marker,
   append `LCCN:`/`OCLC:` when present) is also still unimplemented. See
   [docs/HANDOFF_HISTORY.md](docs/HANDOFF_HISTORY.md) for the full writeup and
   a mocked-up example of the improved log line.

   **2026-10-08 addendum: scoped "best-guess, label don't block" idea, costed
   against the 0xB2 sample file.** Considered a narrower alternative to the
   external-authority lookup above: reuse what's already resolvable from
   information already in the file (the existing `corpus_index` same-file
   match inside `find_suspect_marc8_escapes`), actually apply the guess
   instead of only suggesting it, and label each occurrence inline in the log
   body as `FIXED/NEEDS REVIEW` (guessed + applied) vs. `NOT FIXED/NEEDS
   REVIEW` (no guess available). No section-table change needed for this --
   `_section_for` gates purely by category, not per-entry `fixed`, so
   `suspect_marc8_escape` would stay in the NEEDS REVIEW section regardless;
   only the per-bullet label text would change.

   Tested against `working/GTU_bibs_0xB2_sample.mrc` (396 records, read-only,
   via `--log-full suspect_marc8_escape`, output discarded): 22 records /
   ~26 occurrences, **0 of them resolved by the existing `corpus_index`
   same-file lookup** -- every single one still falls back to the generic
   "likely a miskeyed accented letter... verify against another source"
   message. Relabeling with only what's resolvable today would produce an
   all-"NOT FIXED" report on this file; no visible change without extending
   resolution beyond what's already built.

   By eye, several of these 22 records ARE guessable -- German (`Gru[?]ung`
   -> Gründung, `gr[?]ten` -> größten, `Mu[?]ig` -> Mutig, `Referenzgr[?]e`
   -> Referenzgröße), Polish (`Elz[?]bieta` -> Elżbieta), Slovak
   (`Karf[?]kov` -> Karfíková) -- but the same 22 records also include Tamil/
   Sanskrit IAST romanization (`Tiruvitåan[?]kåur`, `Vedåarthasan[?]graha`)
   and Arabic transliteration (`Mustaòhrag[?]`). A single-language lexicon
   (like the German-only `b2_lexicon.json` built for the fixer below) would
   not cover this mix -- guessing these would need per-occurrence
   morphological/positional heuristics plus manual verification, not a
   lexicon-hit-rate win.

   **Cost estimate (not started; no decision made):** full generalization
   across both script shapes this tool already distinguishes in
   `_MARC8_SCRIPT_CHARSETS` (the 1-char Hebrew/Arabic/Cyrillic/Greek case and
   the 3-char CJK/EACC case) -- 60-100k tokens, comparable in scope to the
   0xB2 lexicon-gated fixer work itself (see below). A narrower pilot scoped
   to just this one sample file's 22 records (one script-escape shape, 1-char
   gap only, no CJK) -- 25-35k tokens, weighted toward candidate-generation
   and hand verification rather than lexicon building, given the
   multi-language mix just described.

2. **CLOSED -- `suspect_hex_encoded_marc8` stays detect-only, auto-replace
   not started.** Its "recoverable" heuristic was tightened and shipped
   (`d4caade`), but auto-replacing even the "recoverable" cases turned out
   to be unsafe, confirmed against every real occurrence in the corpus --
   see the full account below. **Decision: stay detect-only. Not
   started, and not picked back up unless item #1's external-authority
   lookup is built first** -- a real fix here needs the same external
   verification, not a decoder tweak, and the scope is small either way:
   this category only ever fires on 4-5 records out of roughly 670,000
   checked across both real corpora (`working/GTU_bibs.mrc`: 0 occurrences;
   `working/WTS_bibs_2026-10-01.out`: 11 findings across 4 records once the
   tightened heuristic excludes `.b11188236`). State-tracking wouldn't grow
   that set -- it only changes what text a candidate produces, and per
   Step 2 below, it makes every one of them worse, not more trustworthy.

   **Step 1, shipped (`d4caade`): tightened the "recoverable" heuristic.**
   Three replace-strategy options were on the table (auto-replace when
   recoverable; same plus strip when not recoverable; leave as-is), but
   picking one was blocked by `_hex_brace_decode_looks_recoverable` itself
   being unreliable -- found a real example (WTS_bibs_2026-10-01 record
   `.b11188236`, `880 $b`) where it called a boundary-shifted decode
   "recoverable" even though the preview still had a second,
   differently-shaped leftover brace run (`"{uD574}{uC11D}{uC790}"`, 5
   chars per group, not a genuine `_HEX_BRACE_GROUP_RE` match) mixed into
   otherwise-readable text. Fixed: the heuristic now treats a literal
   `"{"`/`"}"` surviving in the decoded text as proof of boundary damage,
   overriding the escape/printable-ratio checks. Confirmed against the full
   real `working/WTS_bibs_2026-10-01.out` corpus (`--log-full
   suspect_hex_encoded_marc8`): still 12 findings/5 records, same as
   before, except `.b11188236` now correctly reads "POSSIBLE DATA LOSS"
   instead of "recovered" -- the other 11 findings (including the two
   then-presumed-clean "recovered" cases, `마가복음`-style Korean and "(Ian
   M. Duguid)") were unaffected by this particular change. Regression test
   added (`TestFindSuspectHexEncodedMarc8.test_boundary_shifted_decode_with_
   leftover_braces_is_not_recoverable`, using `.b11188236`'s real raw
   bytes). `pytest` (419 passed, 1 skipped) and `flake8` both clean.

   **Step 2, attempted and reverted: auto-replace when recoverable.**
   Implemented `fix_hex_encoded_marc8` (splice the decoded hex-brace bytes
   back into the field, guarded by a new `--no-fix-hex-encoded-marc8`
   flag, category `fixed_hex_encoded_marc8`) and ran it against a real
   integration test built around `.b11165406`'s `246-02` `880 $a` occurrence
   (the same record `transcode_marc8_to_utf8`'s per-subfield isolation was
   originally built for -- see `docs/HANDOFF_HISTORY.md`'s "Stage 1-3"
   entry). The existing `test_bib_pipeline_logs_removed_untranscodable_
   subfield` test failed: instead of the expected `removed_untranscodable_
   subfield`, the record transcoded cleanly end-to-end with no warning at
   all -- but the recovered 880 text was `"마태        "` (Matthew, with
   trailing blank padding), not the `"마가복음"` (Gospel of Mark) the
   isolated preview had shown moments earlier for the exact same bytes.

   Tracing this down: `_marc8_bytes_to_readable_preview` (used for the log
   preview) and the old `_hex_brace_decode_looks_recoverable` heuristic
   both decode the hex-brace run's bytes *in isolation*, starting from a
   fresh MARC-8 G0/G1 state. But every real occurrence sits inside a field
   that already has its own open CJK/EACC escape (`\x1b$1...`) surrounding
   it. Splicing the "recoverable" bytes back in and transcoding the *whole*
   field (not just the isolated payload) checks out against every real
   finding in the corpus (`working/WTS_bibs_2026-10-01.out`, all 12):

   | record | isolated preview | spliced into full field |
   |---|---|---|
   | `.b11227394` `880 $c` | `"(Ian M. Duguid)"` | `"伊恩          著 ; 郭熙安譯."` |
   | `.b11165406` `880 $a` (×6) | `"마가복음"`, `"요한복음"`, `"로마서"`, `"요한계시록"`, etc. | `"마태        "`, `"누가        "`, `"사도행전       "`, `"일반서신         "`, etc. |
   | `.b11077347` `880 $c` | `"= J. Gresham Machen"` | `"(美) J. 格雷山姆          "` |
   | `.b11257982` `880 $a` (×2) | `"나 "` | long runs of real-looking Korean text, different words |

   **Every single one of the 12 real findings -- including the one
   previously treated as the safest, purely-ASCII example -- produces
   completely different (and largely nonsensical) text once the splice
   respects the real surrounding escape state, instead of decoding fresh.**
   None of these differences raise a warning or exception; pymarc's own
   truncation detector only catches "ran out of bytes mid-multi-byte-char,"
   not "syntactically valid but semantically wrong because the original
   hex-chunking didn't line up with character boundaries." Reverted the
   implementation (`git checkout -- marc_repair.py`) before committing
   anything unsafe; nothing from Step 2 shipped.

   **What this proves, and what it would actually take to fix properly
   (asked and answered separately, logged here per instruction):** the
   natural assumption is that the bug is "we decoded the payload out of
   context, with the wrong escape state" -- and that threading the real
   state through is a big, separate feature. That assumption is only half
   right. The engineering cost of state-correct decoding is actually LOW:
   pymarc's `MARC8ToUnicode` class (`venv/.../pymarc/marc8.py`) already
   supports resuming from explicit state -- its constructor takes
   `G0`/`G1` parameters, and `.translate()` mutates `self.g0`/`self.g1` as
   it scans. In practice, splicing the recovered raw bytes into the whole
   field and running ONE ordinary `marc8_to_unicode()` call over the
   result (exactly what `transcode_marc8_to_utf8` already does for
   everything else) already respects the real state correctly -- this is
   exactly what the "spliced into full field" column above *is*. No
   vendored state machine or reimplementation needed; maybe 20-30 lines,
   largely already written during Step 2.

   **That state-correct decode is exactly what produced the table above --
   and it's still wrong.** Respecting the real escape state didn't produce
   something trustworthy; it produced a *different* syntactically-valid
   decode (Chinese characters instead of an English name; the wrong Bible
   book padded with blanks). This proves the actual bottleneck was never
   "we forgot to track escape state" -- it's that the upstream hex-encoding
   /brace-wrapping already destroyed or shifted byte boundaries before this
   tool ever saw the record (the category's own docstring already warned
   "a stray byte or an incomplete trailing hex digit commonly survives at a
   chunk's edge"). A syntactically valid, state-correct decode of
   boundary-damaged bytes is still just *a* decode, not evidence it's *the*
   original one. No amount of care on the decoding side recovers
   information that's already gone upstream.

   **What would actually be needed to trust an auto-replace:** external
   verification -- matching the candidate decoded text against something
   outside the corrupted bytes themselves (the record's own title/
   cross-reference fields, or an LC/OCLC lookup). That's the same shape of
   feature as open item #1 above (`suspect_marc8_escape`'s
   external-authority lookup) -- network calls, matching logic, probably
   still needing a human to sign off per record -- not a decoder fix. Worth
   scoping together with item #1 if ever picked up, rather than as its own
   smaller task.

3. **`install.sh`'s self-update/re-run story.** A minimal-fetch install
   directory (`curl | bash -s -- --dir .`) never gets its own copy of
   `install.sh`, so `./install.sh` fails with "No such file or directory" if
   someone tries to re-run it locally later -- the only way to update is the
   `curl | bash` one-liner again. Worth deciding whether to ship `install.sh`
   into that directory too, or make the existing "apply an update" hint more
   explicit about needing the `curl | bash` form. Not started.

4. **`build_marc8_corpus_index` reads the whole input file into memory.**
   Unlike the rest of the pipeline (O(1) memory, streamed), this one `read()`s
   the entire file as a latin-1 string when `encoding_used == "latin-1"`.
   Confirmed fine at 421MB; a multi-GB legacy MARC-8 file could still push
   memory up by its full size. `--no-marc8-corpus-lookup` is already an escape
   hatch. If picked up: switch to chunked reads with a small overlap buffer
   (~40-80k tokens of agent work estimated, overlap-buffer correctness is the
   fiddly part). Not started. (The separate worry about this scan needing its
   own startup progress indicator is resolved -- `main()` already prints
   "Scanning input file for MARC-8 corpus lookup..." before it runs.)

5. **Whether holdings records carry the same lost-diacritic corruption bib
   records did.** MARC-8-to-UTF-8 transcoding is skipped entirely for holdings
   records today (a separate, pre-existing scope decision), so even if a
   holdings record had the exact same corruption `fix_marc8_diacritic_escapes`
   now fixes for bibs, nothing would currently touch it. No GTU/WTS holdings
   export has been found to check against; the only holdings `.mrc` on disk
   belongs to an unrelated project/library, not a fair substitute. Deferred by
   the user. Not started.

## HOWTO: build the `--b2-lexicon` file

`marc_repair.py --b2-lexicon PATH` (the gated 0xB2 fixer, `fix_marc8_b2_umlaut`)
loads a small JSON file -- it ships no lexicon of its own (gitignored, source
licences vary), so this has to be built once per corpus/checkout before the
flag does anything. This is the exact chain already run for GTU
(`working/b2_lexicon.json`, referenced from the "0xB2 fixer BUILT" entry
above); repeat it for a new corpus by swapping `--corpus`/`--cache` paths.
Everything below runs from the repo root, in the venv
(`source venv/bin/activate`), via `wsl.exe -e bash -lc '...'` per the
standing WSL instruction.

**`tools/context_probe/build_b2_lexicon.py` now does all of steps 1-3 below
in one call** -- e.g. `python tools/context_probe/build_b2_lexicon.py
--corpus working/GTU_bibs.mrc --out working/b2_lexicon.json` (add
`--corpus-only` to skip the outside sources entirely, `--no-wngerman` to
drop just that one, `--include-lc-names` to pull in the 2.6 GB LC names
dump, `--force` to rebuild a step whose output already exists). The manual
walkthrough below is kept for understanding what each stage actually does
and for debugging a run gone wrong, not because it's the recommended path
day to day.

### 1. Build the base (corpus-only) lexicon

Scans your own MARC file once for every *cleanly*-encoded (no escape, no
corruption) diacritic word, and indexes it by its flattened ASCII form --
this is the "raw data" at its cheapest: nothing to download, just the
corpus already on disk.

```
python tools/context_probe/lang_lexicon_probe.py build \
    --corpus working/GTU_bibs.mrc --cache working/lexicon_GTU.pkl
```

~5 minutes on a 404,957-record / 527 MB file. Output is a pickle (not the
final JSON yet): `{'glob': {flattened_key: Counter({nfd_variant: count})},
'lang': {...by (lang008, key)...}, 'plain': Counter(...)}`.

Coverage from this step alone is thin -- a corpus only knows the words it
already spells correctly somewhere -- so step 2 below adds outside word
lists to fill the gaps (matched only 127/752 occurrences for 0xB2 without
them; see the "Outside lexicon" entry above for the measured per-source
gain). Skip step 2 and go straight to step 3 if a thinner lexicon is
acceptable.

### 2. Retrieve and merge outside word lists (optional, but what the shipped lexicon uses)

Two raw sources, cheapest first. **Licences aren't verified here -- check
each before relying on it**, and keep every downloaded file out of the repo
(`working/outside_lex/`, already gitignored).

**a. Debian `wngerman` package** -- a plain German word list, no
frequencies, GPL-2+. Download the `.deb` without installing it system-wide,
then extract:

```
mkdir -p working/outside_lex && cd working/outside_lex
apt-get download wngerman
dpkg -x wngerman_*.deb extracted/
# word list lands at extracted/usr/share/dict/ngerman (or similar -- check
# the actual path dpkg -x produced before pointing merge at it)
```

**b. Library of Congress SKOS authority dumps** -- holds the hard cases a
plain dictionary doesn't (German surnames/places, transliterated Sanskrit/
Arabic names with their diacritics). Bulk download, free, no auth:

```
curl -O https://id.loc.gov/download/authorities/subjects.skosrdf.nt.gz   # ~100 MB
curl -O https://id.loc.gov/download/authorities/names.skosrdf.nt.gz      # ~2.6 GB
```

The names dump is large enough that a `curl` started inside a subshell and
left to run in the background can get killed when that shell exits while
still reporting success to whatever's watching it -- run it as the tracked
background command itself (not nested), resume an interrupted download with
`curl -C -`, and verify the result with `gzip -t` before trusting it (this
exact failure mode cost a session earlier -- see the 2026-10-07 "State at
end of session" entry above).

Convert each `.nt.gz` into a plain one-word-per-line file `merge` can read
(streams the gzip, no need to decompress to disk first):

```
python tools/context_probe/lang_lexicon_probe.py lc-words \
    --nt-gz working/outside_lex/subjects.skosrdf.nt.gz \
    --out working/outside_lex/subjects_words.txt
python tools/context_probe/lang_lexicon_probe.py lc-words \
    --nt-gz working/outside_lex/names.skosrdf.nt.gz \
    --out working/outside_lex/names_words.txt
```

Merge each word list into the lexicon in turn, chaining `--base` so each
step builds on the last (word-list words have no frequency, so each
occurrence counts as 1 -- `merge`'s `--weight` can raise that, but the
shipped lexicon used the default):

```
python tools/context_probe/lang_lexicon_probe.py merge \
    --base working/lexicon_GTU.pkl \
    --wordlist working/outside_lex/extracted/usr/share/dict/ngerman \
    --out working/lexicon_GTU_de.pkl
python tools/context_probe/lang_lexicon_probe.py merge \
    --base working/lexicon_GTU_de.pkl \
    --wordlist working/outside_lex/subjects_words.txt \
    --out working/lexicon_GTU_de_sub.pkl
python tools/context_probe/lang_lexicon_probe.py merge \
    --base working/lexicon_GTU_de_sub.pkl \
    --wordlist working/outside_lex/names_words.txt \
    --out working/lexicon_GTU_de_sub_names.pkl
```

(The suffixes `_de`/`_de_sub`/`_de_sub_names` on the pickle names aren't
required by the tool -- they're just how the "State at end of session"
entry above refers back to each stage; name the `--out` files however
makes sense.) The fully-merged pickle is ~31 MB, ~2.9 s to load, ~487 MB
resident -- fine as a one-off build artifact, which is exactly why step 3
trims it down before `marc_repair.py` ever has to load it.

### 3. Trim to the JSON file `marc_repair.py` actually loads

`fix_marc8_b2_umlaut` only ever fills an a/o/u umlaut (that's all a bare
0xB2 can stand for -- see the block comment above it in `marc_repair.py`),
so the full pickle's other diacritic variants (acute, cedilla, etc., kept
around for the unrelated 0xA5/0xA8/0xAE disambiguation probes) are dead
weight here:

```
python tools/context_probe/lang_lexicon_probe.py trim \
    --base working/lexicon_GTU_de_sub_names.pkl \
    --out working/b2_lexicon.json
```

Output: JSON, `{flattened_key: {nfd_variant: count}}`, umlaut variants
only -- 4.4 MB / ~127k keys for GTU. This is the file to hand to
`--b2-lexicon`:

```
python marc_repair.py working/GTU_bibs.mrc --b2-lexicon working/b2_lexicon.json ...
```

Neither this JSON file nor any of the intermediate pickles/downloads are
committed to the repo (all under the gitignored `working/` tree) -- re-run
this whole HOWTO after a fresh checkout, or whenever the corpus changes
enough that `build`'s corpus-only pass should be redone.

## HOWTO: re-run the 0xA4 disambiguation methodology for a new byte

Worked end-to-end for `0xA4` this session (confirmed `z` -> dot below as a new
override; see `_MARC8_BARE_COMBINING_BEFORE_OVERRIDES` in `marc_repair.py` and
`docs/MARC8_DIACRITIC_HANDLING.md`), for `0xA8` in a later session (no new
override -- the 12 leftover occurrences split across four small, unrelated
groups, none clean/large enough; see the "Current state" entry above and
`docs/MARC8_DIACRITIC_HANDLING.md`'s table), for `0xA5` in a later session
(no new override -- `h` looked like a clean addition in isolation, same
dot-below mark `r` already gets, but was rejected after it was found to
make a real record's transcoded output worse due to an unrelated decoder
bug interacting with it; see the "Current state" entry and "Lessons
learned" above), and for `0xAE` in a later session still (no new override
-- the 26 leftover occurrences split across five small groups: destroyed-
base-letter Italian, an off-by-one Akkadian/Hittite/Sumerian cluster where
the mark belongs one letter further back than this mechanism can reach, a
Germanic cluster needing a different mark, a genuinely ambiguous
al-Ghazali/"Ihya" cluster, and two singletons; see the "Current state"
entry above and `docs/MARC8_DIACRITIC_HANDLING.md`'s table). **All three
bytes this file previously flagged for re-checking (`0xA8`, `0xA5`, `0xAE`)
are now done** -- `_MARC8_BARE_COMBINING_RESTRICTED_BEFORE` and
`_MARC8_BARE_COMBINING_BEFORE_OVERRIDES` in `marc_repair.py` reflect the
current, fully-re-verified state for every overloaded byte. This HOWTO is
kept as reference methodology for the *next* time a new candidate byte
needs the same disambiguation treatment (e.g. a byte currently in the
"Investigated, not implemented" table, or a wholly new one) -- **read the
whole thing, including the "Lessons learned" entries above, before running
anything against a new byte, since step 4 below is where the real judgment
calls are and the same traps (counting-metric blind spots, off-by-one
marks, destroyed base letters, decoder-interaction regressions) are likely
to recur.**

### 1. Find the records (build a TSV report)

Adapt `working/find_0xa4_occurrences.py` (still on disk, gitignored) -- change
only `TARGET_BYTE` (and the output filenames) for the new byte. The core loop:

```python
import sys, csv
sys.path.insert(0, '/home/marnold/scratch/marc_repair')
import marc_repair as m

PATH = 'working/GTU_bibs.mrc'
TARGET_BYTE = '\xae'  # or '\xa5', '\xa8'

# ... render_010/render_oclc_035/render_245/render_907a/render_066 helpers,
# copied verbatim from working/find_0xa4_occurrences.py ...

rows, matched_record_texts = [], []
for i, (parsed, rec_text) in enumerate(m.iter_repair_stream(PATH)):
    if parsed.unresolved:
        continue
    # ... gather per-record metadata via the render_* helpers ...
    record_has_match = False
    for f in parsed.fields:
        texts = [('', f.content or '')] if f.is_control() else f.subfields
        for code, data in texts:
            if data is None:
                continue
            for mm in m._MARC8_BARE_COMBINING_RE.finditer(data):
                before, byte = mm.group(1), mm.group(2)
                if byte != TARGET_BYTE:
                    continue
                # before = the letter immediately preceding the junk run --
                # this IS the letter a combining mark would attach to, not
                # the letter after. Don't get this backwards (see step 4).
                record_has_match = True
                # ... append a row with record_id/010a/oclc_035/field/
                # before_letter/context/title_245/907a/066 ...
    if record_has_match:
        matched_record_texts.append(rec_text)
```

Run it with the venv active: `source venv/bin/activate && python3
working/find_0x<BYTE>_occurrences.py`. Reusing `m._MARC8_BARE_COMBINING_RE`
(not a hand-rolled regex) matters -- it already has the close-escape-B
exclusion and junk-fragment caps this session's fixes added; a naive regex
will overcount, the same way the stale "93" count for `0xA4` did (see
`docs/HANDOFF_HISTORY.md`).

### 2. Write the report (TSV)

Same `csv.DictWriter` block as `working/find_0xa4_occurrences.py`'s tail --
columns `record_index, record_id, 010a, oclc_035, field, before_letter,
context, title_245, 907a, 066`, written to
`working/0x<BYTE>_occurrences.tsv`. Group by `before_letter` first
(`cut -f6 ... | sort | uniq -c | sort -rn`) to see the shape of the leftovers
before reading anything -- that's what surfaced `0xA4`'s "z" cluster (21 of 79
occurrences, previously invisible inside a generic "everything but u" bucket).

### 3. Create the MARC sample file

Same loop already does this -- `matched_record_texts` collects each
*matched* record's raw, original `rec_text` (not the parsed/re-rendered
version) once per record, written out with:

```python
encoding_used = m.detect_encoding(PATH)
with open('working/GTU_bibs_0x<BYTE>_sample.mrc', 'wb') as sample_fh:
    for rec_text in matched_record_texts:
        sample_fh.write(rec_text.encode(encoding_used, errors='surrogateescape'))
```

Sanity-check it parses clean before trusting it: re-run `iter_repair_stream`
over the new sample file and assert `not parsed.unresolved` for every record.

### 4. The actual disambiguation (the part that isn't mechanical)

For each before-letter group in the TSV, reconstruct the candidate word under
a specific mark hypothesis and check whether it's a real word/name:

```python
import pymarc
# mark-before-letter ANSEL order: combining byte goes BEFORE the base letter
# it modifies, e.g. dot-below (\xf2) + "z" -> "ẕ"
candidate = (before + '\xf2' + after_fragment).encode('latin-1', errors='replace')
print(pymarc.marc8.marc8_to_unicode(candidate))
```

Try each of the already-known marks first (dot below `\xf2`, cedilla `\xf0`,
ogonek `\xf1`, diaeresis `\xe8`, acute `\xe2`) before assuming a new one is
needed -- `0xA4`'s "z" cluster turned out to need dot below, the *same* mark
`0xA3`/`0xAE`/`0xA5` already produce, not a new one.

**Three traps found doing this for `0xA4`, all of which will recur:**

- **Same before-letter, different treatment depending on the word.** `0xA4`'s
  `l`/`L` needed dot below for Tamil ("Tamil<junk>akam" -> "Tamiḻakam") but
  was a *different, unfixable* corruption for Spanish ("Theol<junk>gicos" ->
  "Theológicos" -- see next trap). Same for `t` (Arabic "Khut<junk>ba" ->
  "Khuṭba" vs. Spanish "Cat<junk>lica" -> "Católica"). If a before-letter
  group's context lines look linguistically mixed, do NOT add it as a clean
  override -- it can't be generalized by before-letter alone with this
  mechanism.
- **The base letter itself was destroyed, not just its mark** -- this is the
  big one, and it's why roughly 48 of `0xA4`'s 79 occurrences stayed
  unfixed. "Cat<junk>lica" is missing the whole "ó" between "t" and "l", not
  a mark attached to "t" -- same failure mode as the already-known 3-byte
  CJK/EACC escape case, just showing up through the bare-byte path too. The
  tell: the reconstructed "before+mark" candidate doesn't read as a real
  word AT ALL (not just a different accent) once you spell out the rest of
  the word by hand. `_MARC8_BARE_COMBINING_BYTES`'s whole mechanism assumes
  the base letter survives right before the junk -- it structurally cannot
  fix this shape; don't try to force it in via `_MARC8_BARE_COMBINING_BEFORE_OVERRIDES`.
- **The mark can belong to the letter AFTER the junk, not before.** Found
  once in `0xA4` ("J<junk>o<esc>nsson" -- the "o" survived, just lost its own
  accent, so "Jónsson" needs the mark on "o", not "J"). This mechanism only
  ever attaches to the *before* letter; a before-after case like this needs a
  different fix entirely and should be left alone, not forced.
- **A correct reconstruction in isolation can still make a real record
  worse.** Found for `0xA5`'s `h` (not added, despite reconstructing
  perfectly as "Muh<junk>ammad" -> "Muḥammad"): a different real "h"
  occurrence ("Yah<junk> at Elephantine", `.b18157713`) shares a subfield
  with an unrelated, pre-existing escape-designator corruption that
  leaves pymarc's `marc8_to_unicode` in a bad charset state for the rest
  of the field. Leaving the bare byte alone happened to let the decoder
  resync afterward and recover ~100 characters of legible trailing text;
  applying the semantically-correct fix kept the decoder stuck and
  garbled that same trailing text instead. **Before trusting any
  before-letter group as "confirmed," diff full-field transcoded output
  (fix on vs. off, via monkeypatching
  `_MARC8_BARE_COMBINING_RESTRICTED_BEFORE`/`_MARC8_BARE_COMBINING_BEFORE_OVERRIDES`)
  for every real occurrence in the sample file, not just enough of them
  to confirm the word** -- checking `len(details)` or an instance count
  is not enough, same blind spot as the `0xA4`/`z` counting-metric trap
  documented earlier in this file, just manifesting as a *regression*
  instead of an invisible no-op this time.

### 5. If a byte gets a clean new override

Add it to `_MARC8_BARE_COMBINING_BEFORE_OVERRIDES` (`marc_repair.py`, next to
the `0xA4`/`z` entry already there) -- `{byte: {before_letter_lowercase:
mark}}`. Update the byte's own comment block above
`_MARC8_BARE_COMBINING_BYTES` with the occurrence count and real-word
examples, same style as the `0xA4` entry. Add regression tests to
`TestFixMarc8DiacriticEscapes` mirroring
`test_recovers_bare_combining_dot_below_override_for_0xa4_after_z` /
`test_bare_combining_dot_below_override_transcodes_to_correct_utf8`
(`tests/test_marc_repair_bib.py`) -- **double-check the raw test string has
the before-letter positioned correctly** (immediately before the junk run,
not after) -- this tripped up the `0xA4` tests twice during this session.
Update `docs/MARC8_DIACRITIC_HANDLING.md`'s table/restricted-bytes section
and this file. Re-run full `pytest`/`flake8`, then the full-corpus re-run
against `working/GTU_bibs.mrc` with `--log-full fixed_marc8_diacritic` to
confirm the new override's occurrence count and that nothing else moved.

## DONE: WTS "Signatures:" false-positive -- branch `marc8-diacritic-fix`

Both false positives found while re-validating `fix_marc8_diacritic_escapes`
against a second corpus (`working/WTS_bibs_2026-10-01.out`) are now fixed,
tested, and verified end-to-end against both full corpora. See
`docs/MARC8_DIACRITIC_HANDLING.md`'s new "False positives found and guarded
against" section for the permanent writeup -- summary below.

### What's confirmed and already fixed

Running `fix_marc8_diacritic_escapes` against
`working/WTS_bibs_2026-10-01.out` (a second corpus, different library,
to sanity-check the work already done against `working/GTU_bibs.mrc`)
turned up two distinct false-positive bugs:

1. **Close-escape "B" mistaken for real text -- FIXED, commit
   `46c08f4`.** When a close escape (`\x1b(B`) sat immediately next to
   a different, confirmed-payload escape with no real letter between
   them, the "before letter" capture matched the close escape's own
   "B" byte as if it were text, splicing a real mark onto escape
   machinery (confirmed on record `.b11749192`, an "Imperfect:" note
   about damaged book signatures, nowhere near a diacritic). Fixed with
   `_MARC8_NOT_ESCAPE_DESIGNATOR_LOOKBEHIND`, a negative lookbehind for
   `\x1b\(` applied to both regexes that capture a before-letter. 3
   regression tests added. Confirmed fix doesn't regress GTU (counts
   unchanged) or WTS (the 2 affected records now correctly fall back to
   `suspect_marc8_escape`).

### "Signatures:" collation-statement false positive -- FIXED

2. **"Signatures:" collation-statement false positive -- FIXED, not yet
   committed.** Early-printed-book cataloging convention (ESTC-style):
   a `500 $a` note like `"Signatures: A-C⁴ D²."` records the book's
   gathering/leaf-count structure. In WTS's corrupted data, this reads
   `"Signatures: A-C\x1b(QE \x1b(BD\x1b(QC.\x1b(B"` -- the escape
   mechanism meant for lost ANSEL diacritics is apparently being reused
   by whatever corrupted this corpus for a lost *superscript leaf-count
   digit*, and the fixer couldn't tell the difference: it inserted a
   bogus macron on "C", producing
   `"Signatures: A-\xe5C D\x1b(QC.\x1b(B"` (wrong on every level --
   nothing here needed a diacritic).

   **Four heuristics tried and rejected** (each verified empirically
   against the FULL `working/GTU_bibs.mrc` corpus -- don't re-try any
   of these without new evidence):
   - Restrict the escape regex's captured "after" letter to lowercase
     only -- rejected, breaks **19,707** legitimate GTU fixes (e.g.
     "Vosté, Jacques-M.", "Université Saint-Joseph", "post mortem.
     Aphorismi" -- genuine diacritic words are routinely followed by a
     capitalized next word/sentence).
   - Require the "before" letter to be an isolated single-character
     token (preceded by a non-letter) AND the "after" letter
     uppercase -- rejected, breaks **2,941** legitimate GTU fixes, all
     the single-letter French word "à" (grave accent) immediately
     followed by a capitalized proper noun ("à Kempis", "à Descartes",
     "à Paris") -- syntactically *identical* shape to the false
     positive, genuinely ambiguous without more context.
   - Same as above but also require the "before" letter itself be
     uppercase -- rejected, breaks **293** legitimate GTU fixes, mostly
     French/Czech capitalized initials with an accent immediately
     followed by a capitalized surname ("É. Delaruelle", "Ú.CN").
   - Skip the fix whenever the enclosing subfield text contains the
     word "signature" anywhere (case-insensitive) -- rejected, too
     broad: **25** legitimate GTU fixes sit in fields where "signature"
     appears incidentally in ordinary running French/English prose
     elsewhere in the same field, alongside real diacritic words.

   **What worked:** restrict to fields whose text *starts with* the
   label `"Signatures:"` or `"Signature:"` (a note-type prefix, not
   just the word appearing anywhere). Confirmed via (fixed, corrected
   version of) `working/check_signature_label_fast.py` (still on disk,
   gitignored) against both full corpora: excludes **8/8** WTS
   confirmed-payload matches inside `Signatures:` fields, and excludes
   only **1** of GTU's 310,036 confirmed matches -- and that 1 GTU match
   turned out to be the *same* false-positive pattern (a genuine
   `Signatures:` collation note), not a lost loss, so it's a bonus fix,
   not a regression. (The script as originally written had its own bug
   -- the subfield-boundary lookback didn't skip the subfield-code byte
   after `\x1f`, so no prefix ever matched; fixed before trusting its
   output.)

   Implemented as `_MARC8_SIGNATURES_NOTE_PREFIXES` +
   `text.lstrip().lower().startswith(...)`, checked at the very top of
   `_fix_marc8_diacritics_in_text` (`marc_repair.py:2587`) -- skips both
   the escape-payload and bare-byte paths together (confirmed via the
   full-corpus re-runs below that this was the right scope; all 8 WTS
   false positives were escape-based in practice, but the guard is at
   the shared entry point so bare-byte would be covered too if it ever
   came up). Two regression tests added to
   `TestFixMarc8DiacriticEscapes`
   (`tests/test_marc_repair_bib.py`) using the real WTS example. 412
   tests pass (1 skipped); flake8 clean.

   **Full-corpus verification, both fixes together** (close-escape +
   Signatures:):
   - WTS (`working/WTS_bibs_2026-10-01.out`, 263,595 records):
     `fixed_marc8_diacritic` now 2 records (down from the inflated
     pre-fix count); `suspect_marc8_escape` 86 records (unrelated CJK-
     welding category, unaffected); 263,595/263,595 written, 0
     unfixable; 89.42s. Log:
     `working/WTS_bibs_2026-10-01_repaired5_log_20261007T150801Z.log`.
   - GTU (`working/GTU_bibs.mrc`, 404,957 records):
     `fixed_marc8_diacritic` now 149,150 instances / 64,340 records,
     vs. the true pre-this-session baseline of 149,152/64,341 (Run 3 in
     the entry below, predates both this session's fixes) -- a drop of
     exactly 2 instances / 1 record, matching the 1 GTU false positive
     found above, nothing else changed. Same single pre-existing
     unfixable record (403838) as every prior run. 404,956/404,957
     written; 193.85s. Log:
     `working/GTU_bibs_repaired_sigfix_log_20261007T151052Z.log`.

   **Not yet done:** commit this work (currently uncommitted on
   `marc8-diacritic-fix`), and the doc update this entry references is
   already written to `docs/MARC8_DIACRITIC_HANDLING.md`'s new "False
   positives found and guarded against" section -- include it in the
   same commit or a follow-up.

### DONE: ran against `sample_files/nashvillestate_bibs_202693.mrc` -- clean, nothing new

Ran with both fixes above in place: `python3 marc_repair.py
sample_files/nashvillestate_bibs_202693.mrc -o
working/nashvillestate_bibs_202693_repaired.mrc --log-full
fixed_marc8_diacritic,suspect_marc8_escape`. Result: **48,017/48,017
records written, 0 unfixable**, 18.56s. Log:
`working/nashvillestate_bibs_202693_repaired_log_20261007T151555Z.log`.

Small corpus, correspondingly small diacritic-related counts -- 4
records hit `fixed_marc8_diacritic` (no new pattern; INFORMATIONAL
categories never list full detail regardless of `--log-full`, by
design), 3 hit `suspect_marc8_escape`, all three already-familiar
shapes from GTU/WTS (CJK-welding "Schrödinger", a miskeyed apostrophe
in "who's", "Leonidas"). No `Signatures:`-style false positive, no new
unresolved byte, nothing requiring a code change. 412 tests still pass,
flake8 still clean. Other findings in this file (375 trailing-
whitespace, 617 default-245, 6 duplicate identifiers across
4-way-duplicated records, 16 invalid indicators, etc.) are all
pre-existing categories behaving as documented in
`docs/REPAIR_CATEGORIES.md` -- nothing new to report there either.

### DONE: ran against `sample_files/Bucknell00000448.mrc` -- clean, already UTF-8

Ran the same way: `python3 marc_repair.py
sample_files/Bucknell00000448.mrc -o
working/Bucknell00000448_repaired.mrc --log-full
fixed_marc8_diacritic,suspect_marc8_escape`. Result: **1,000/1,000
records written, 0 unfixable**, 0.60s. Log:
`working/Bucknell00000448_repaired_log_20261007T151732Z.log`.

Both diacritic-related categories are **0 records** -- not a gap,
expected: this corpus's leader byte 9 is already `'a'` (declared
UTF-8), so `fix_marc8_diacritic_escapes`/`find_suspect_marc8_escapes`
both correctly skip every record (same guard `transcode_marc8_to_utf8`
uses; confirmed via a quick `pymarc` check of record 0's leader, and
`transcoded_marc8` itself is also 0 records in this log, consistent).
Other findings (1 record with an empty-`$a` 010, 1 record with 4
empty-`$a` 880s, 3 records with smart-character normalization) are
all pre-existing, already-documented categories at small scale --
nothing new.

**Nothing further queued from the user for this session as of this
entry.**

## IN PROGRESS: `fix_marc8_diacritic_escapes` -- branch `marc8-diacritic-fix`, 13 commits pushed-nowhere

Started as a prototype requested by the user after a long discussion
(triggered by manually comparing `working/GTU_bibs.mrc` record
`.b10000094` against its LC catalog copy and Sierra's own OPAC
display) of whether the `suspect_marc8_escape` detect-only finding
(see `docs/MARC8_ESCAPE_ANALYSIS.md`, the TODO below, and this doc's
own older entries) could be turned into a real auto-fix for at least
some of its ~52,847 flagged records. Short version: **yes, for a
large majority of them**, once you separate out several different
corruption *shapes* sharing the same surface symptom
(`\x1b`/escape noise mid-word). This entry is a full state dump so a
fresh session (after a context clear) can pick this up without
re-deriving any of it.

**Everything described in this entry is now committed** (`47919ac`,
`1308d8f`, `d61cce0`, `0593bbe`, `92d8fdb`, `5cc917c`, plus later
commits through `10a016c` -- see the IN PROGRESS entry above for the
most recent work and what's still open -- all on this branch, not on
`main`, not pushed anywhere). All 410 tests pass (1 skipped), flake8
clean on `marc_repair.py` (`--max-line-length=100`, per README.md).

### Branch / commit state

On branch `marc8-diacritic-fix` (checked out from `main` at `63f55c8`).
First two commits:

- `47919ac` -- initial `fix_marc8_diacritic_escapes`: a curated table
  (`_MARC8_DIACRITIC_PAYLOADS`) mapping (MARC-8 script-switch charset,
  payload byte) -> the genuine ANSEL combining-diacritic byte it was
  corrupted from, for the single-raw-byte switches only (charset codes
  2/3/4/N/Q/S -- NOT the 3-byte CJK/EACC case "1", which really does
  destroy the base letter, unlike this case). Emits real ANSEL bytes
  (mark-before-letter order), not precomposed Unicode -- the record
  stays legitimately MARC-8 so `transcode_marc8_to_utf8` (unchanged,
  runs right after in the pipeline) does the actual UTF-8 conversion.
  New category `fixed_marc8_diacritic` (INFORMATIONAL, on by default,
  `--no-fix-marc8-diacritic-escapes` to disable).
- `1308d8f` -- extended it with `_MARC8_BARE_COMBINING_BYTES`/
  `_MARC8_BARE_STANDALONE_BYTES`: a *second* corruption shape, no
  recognized escape at all, just the same stray junk (`\x1bp+\x1bs`
  etc.) directly in front of a bare un-escaped byte standing in for an
  Arabic dot-below (combining) or hamza/ayn (standalone modifier
  letter). Also removed (demoted) payload `("Q","G")` from the table
  at this point -- see below, it went back in later.

**Committed on top of `1308d8f` as `d61cce0`** (doc row updated +
regenerated in the same commit):

1. **Trapped-punctuation generalization** (the big one). Checked
   against the *full* `working/GTU_bibs.mrc` (not just the 50-record
   sample) what the "rare, only-3-instances" third corruption shape
   flagged in the `1308d8f` commit message actually looked like at
   scale: **80,994 occurrences**, not 3. The shape: the escape's
   closing `\x1b(B` and whatever plain ASCII punctuation/whitespace
   should follow it got swapped, trapping the punctuation *inside* the
   escape instead -- `"Facolta" + grave + " " (trapped) + close + "di
   lettere"` should read `"Facoltà di lettere"`. `_MARC8_DIACRITIC_ESCAPE_RE`
   now captures that trapped run (`[\x20-\x7e]{0,15}`, restricted to
   plain-ASCII-punctuation specifically so it can never misfire on
   genuine multi-character foreign-script content under the same
   charset switch) and reinserts it after the mark+letter instead of
   discarding it.
2. **`("Q","G")` restored**, with a narrow exception. Re-checked the
   "demoted, ambiguous" decision from `1308d8f` against *all* 2,920
   occurrences of this payload in the full file, not just the one
   Arabic counterexample that triggered the demotion: the Arabic case
   (needs MACRON) is the rare exception (8 of 2,920, ~0.27%), not a
   real 50/50 split -- the overwhelming majority are legitimate BREVE
   (Russian "-iĭ" name endings like "Krachkovskii", "Georgii
   Valentinovich"; Korean vowels like "Sŏul", "Yŏn"). All 8 genuine
   Arabic occurrences have the literal article `"al-"` within a few
   characters; Russian/Korean words never do. So `("Q","G")` is back
   in `_MARC8_DIACRITIC_PAYLOADS` as BREVE, with a new
   `_MARC8_AMBIGUOUS_NEAR_AL_PAYLOADS` set + a 15-char-radius `"al-"`
   check in `_marc8_diacritic_replacement` that skips the fix (leaves
   it for `suspect_marc8_escape`) only for that one payload, only near
   `"al-"`. **041/008 language codes were checked first and don't
   work as a disambiguator** -- they describe the record's/work's
   language, not the specific romanized word's origin (e.g. plenty of
   `008` lang `"eng"`/`"ger"`/`"rus"` records contain the Russian
   `-iĭ` pattern; of the 5 records whose `008`/`041` *did* say `"ara"`,
   most of the actual `Q/G` words in them turned out to be Russian or
   Turkish names anyway, not Arabic). The local-text `"al-"` proximity
   check is what actually works.
3. **Two real regex bugs found and fixed** while testing (2) above --
   worth understanding if touching this code again, since they're the
   kind of mistake that's easy to reintroduce:
   - The bare-byte junk pattern (`_MARC8_STRAY_ESCAPE_JUNK[_NONEMPTY]`)
     could swallow an entire recognized escape (open *or* close) as if
     it were stray junk, e.g. `"\x1b(QG"` or `"\x1b(B"` both fit
     comfortably inside one generic `\x1b` + up-to-N-non-ESC-bytes
     fragment. That silently erased escapes this fixer had just
     deliberately decided to leave alone (like the Q/G "al-" exception
     in (2)), re-exposing exactly the bug this was supposed to avoid.
     Fixed two ways: capped each junk fragment at 2 non-ESC bytes
     (every real stray-junk fragment ever observed is 1-2 bytes:
     `"\x1bp+"`, `"\x1bs"`, `"\x1bp("`), AND added a negative lookahead
     excluding any fragment whose first byte is `"("` (every real
     recognized escape piece, open or close, starts with `"("` right
     after the ESC; no real stray-junk fragment ever does). The `"("`
     exclusion is the one that actually matters structurally; the
     length cap is defense-in-depth matching observed data.
   - `_MARC8_DIACRITIC_ESCAPE_RE`'s "after" group required a plain
     `[A-Za-z]` letter immediately following the escape -- fails when
     a *second* corruption (one of the bare bytes from (2) above) sits
     directly against it with no letter in between. Fixed with a
     zero-width lookahead alternative (`(?:([A-Za-z])|(?=\x1b))`) so
     the match still succeeds and leaves the adjacent corruption for
     the next regex pass to handle, instead of failing to match at all
     and silently dropping the diacritic.

All three changes have tests added (see `TestFixMarc8DiacriticEscapes`
in `tests/test_marc_repair_bib.py`) and pass: **391 passed, 1
skipped**, `flake8` clean (no findings beyond pre-existing `E501`
long-line debt) as of this writeup.

### Full-corpus run results (three runs, don't confuse them)

**Run 1** (code state: `1308d8f` + trapped-punctuation fix, Q/G still
demoted) -- `python3 marc_repair.py working/GTU_bibs.mrc -o
/tmp/GTU_full_repaired.mrc --log /tmp/GTU_full.log --log-full
fixed_marc8_diacritic` (actual log file is
`/tmp/GTU_full_20261007T130646Z.log`, 125.25s, 404,957 records):
- `fixed_marc8_diacritic`: **143,535 instances across 62,184 records**
  (auto-fixed, was 0 before this session).
- `suspect_marc8_escape` (still needs human review): **2,080 records**,
  down from the `52,847` baseline in the TODO entry below (96%
  reduction). What's left is legitimately ambiguous/unconfirmed --
  Korean breve "Yŏng-sun"/"Pyŏng-mu" (Q/G was still demoted for this
  run), Turkish dotted İstanbul, Polish "Książka" (ogonek), Sanskrit
  diacritics, a handful of others not yet checked.
- Same single pre-existing unfixable record (403838/missing
  leader+directory) as every prior run -- no new regressions.
- `/tmp` outputs are temp-dir, not committed/gitignored-tracked; treat
  as scratch, regenerate if needed.

**Run 2** (code state: current uncommitted tip, Q/G restored + both
regex bugs fixed) -- `/tmp/GTU_full2_repaired.mrc` /
`/tmp/GTU_full2.log`. **This ran in the background and completed, but
the user explicitly said "don't run against full file yet" moments
after it was kicked off, so its output has deliberately NOT been
reviewed.** Don't treat it as validated. Whoever picks this up should
decide whether to review it now (the code hasn't changed since it
ran, so it should be a valid preview of the current uncommitted
state) or re-run fresh.

**Run 3** (code state: tip of this branch as of commit `e22ec09` --
everything in this entry committed, including the 4 restricted bare
bytes added in a follow-up session) -- `python3 marc_repair.py
working/GTU_bibs.mrc -o /tmp/GTU_now_repaired.mrc --log
/tmp/GTU_now.log --log-full fixed_marc8_diacritic,suspect_marc8_escape`
(actual log file `/tmp/GTU_now_20261007T140549Z.log`, 404,957 records).
Requested explicitly by the user to compare against the true
pre-diacritic-fix baseline (the "DONE: full-corpus run..." entry
below, before `fix_marc8_diacritic_escapes` existed at all):

| metric | before (baseline) | now (Run 3) |
|---|---|---|
| `fixed_marc8_diacritic` | N/A -- category didn't exist | 149,152 instances / 64,341 records |
| `suspect_marc8_escape` | 258,202 findings / 52,847 records | 1,795 findings / 1,258 records |
| unfixable records | 1 (403838) | 1 (403838, same record) |
| elapsed time (tool's own reported time) | 133.93s | 139.31s (+5.38s, +4.0%) |

`suspect_marc8_escape` records dropped 97.6% (52,847 -> 1,258) from the
true pre-fix baseline -- consistent with (and better than) Run 1's
96% reduction above, now that Q/G is restored and the 4 additional
restricted bare bytes (`0xAE`, `0xA5`, `0xA8`, `0xA4`) are included.
The remaining 1,258 are the genuinely unconfirmed cases: CJK/EACC
escapes (never auto-fixed by design), the still-ambiguous bare bytes
(`0xA6`/`0xBA`/`0xB2`/`0xB3`/`0xC1`, see
`docs/MARC8_DIACRITIC_HANDLING.md`), and anything outside the confirmed
before-letter restrictions. `/tmp` outputs are scratch, not committed/
gitignore-tracked, regenerate if needed.

### `0xA7` (cedilla) and `0xB9` (prime/soft-sign) implemented -- `d61cce0`, `0593bbe`

The prior session's scope investigation (below, now historical) found
several more bare-byte corruption candidates via one-off scans (plain
Python/regex over the raw file, not `marc_repair.py`) and left an
"implement the two clean ones vs. validate the murkier ones first"
decision unmade. Resumed cold this session: re-validated `0xA7` and
`0xB9` directly (fresh grep against the full `working/GTU_bibs.mrc`,
plus checking each byte's own unrelated ANSEL meaning via pymarc's
`marc8_to_unicode` to confirm the same "only touch it when preceded by
the stray-ESC-junk signal" safety argument holds), then implemented
both:

- `0xA7` -> ANSEL combining cedilla (`\xf0`). 6,182 occurrences
  re-confirmed (French/Occitan words: "français", "Pourçain"). Added
  to `_MARC8_BARE_COMBINING_BYTES`.
- `0xB9` -> ANSEL modifier letter prime (`\xa7`, i.e. the *byte value*
  0xA7 used standalone rather than as a combining mark -- not a typo,
  confirmed via `marc8_to_unicode(b"\xa7")` -> U+02B9). 2,948
  occurrences re-confirmed (Russian "soft sign" romanizations: "Il'ich",
  "nravstvennost'"). Added to `_MARC8_BARE_STANDALONE_BYTES`.

Both verified end-to-end (fix + `transcode_marc8_to_utf8`) to produce
correct UTF-8 ("français", "Ilʹich") with real tests in
`TestFixMarc8DiacriticEscapes`. No full-corpus re-run done this
session (see "Not yet done" below) -- only the small hand-built test
fixtures above have been exercised; this hasn't been checked against
every real occurrence in `working/GTU_bibs.mrc` the way the escape-
based payloads were.

**Resolved in a follow-up session (commits `92d8fdb`, `5cc917c`):** all
remaining unconfirmed candidate bytes from the original scan (`0xA8`,
`0xAE`, `0xA4`, `0xA6`, `0xB2`, `0xBA`, `0xC1`, `0xB3`, `0xA5`) were
re-validated against the full corpus. Four turned out to be safely
fixable but *overloaded* (same byte, different mark depending on
context) -- each was implemented with a before-letter restriction to
its confirmed-clean context only: `0xAE`→dot-below after `h`,
`0xA5`→dot-below after `r`, `0xA8`→ogonek after `a`/`e`/`u`,
`0xA4`→diaeresis after `u`. The other five (`0xA6`, `0xBA`, `0xB2`,
`0xB3`, `0xC1`) stayed genuinely unconfirmed -- `0xA6` in particular was
checked against two real Sierra catalog records at the user's request
(`.b10016855` Polish, `.b1031037x` Portuguese), confirming it means two
different marks depending on the word *and* that at least one
occurrence needs a fix mechanism (mark-applies-to-the-letter-after, not
before) this codebase doesn't have. In a later follow-up the user
didn't trust Sierra's own rendering for this specific check and asked
to cross-check both titles directly against the Library of Congress's
own catalog (`search.catalog.loc.gov`, both titles have an LCCN) --
LC's record for the Polish title turned out to be internally
inconsistent about this exact word across its own `240`/`500` fields
(three different renderings of the same two words, one with no mark at
all), which reinforces rather than overturns the "leave unconfirmed"
call. Full writeup, including the exact restriction percentages,
every rejected byte's reasoning, and the LC cross-check detail, is now
in `docs/MARC8_DIACRITIC_HANDLING.md` (also created this session) --
that's the doc to hand anyone who asks what's been done. Still not
done: the full-corpus re-run to see the real before/after effect on
`fixed_marc8_diacritic`/`suspect_marc8_escape` counts (see Run 1/Run 2
below, neither reflects any of this session's code).

- Important generalization from the original scan, still true: **the
  same target mark can have more than one corrupted-byte
  representation** (cedilla: both `0xA7` and `0xA6`; dot-below: both
  `0xA3` and `0xAE`) -- the dicts stay keyed by corrupted byte with
  possibly-repeated values, not the other way around.
- **Methodology gotcha**, still applies to any future scan: a naive
  `letter + junk + bare-byte + letter` regex without the `"("`-exclusion
  fix from `1308d8f` will also match pieces of *already-recognized*
  escape-open sequences as false "bare bytes" (e.g. flagged the ASCII
  digit `'3'`, really the Basic-Arabic charset *designator* from an
  unrelated `\x1b(3C...` escape). Exclude ASCII letters/digits that
  coincide with known charset designators (`1`,`2`,`3`,`4`,`N`,`Q`,`S`),
  or just reuse `_MARC8_STRAY_ESCAPE_JUNK_NONEMPTY` (with the
  `"("`-exclusion) for the "junk" part instead of an ad-hoc regex.

### Historical: original scope investigation that found these bytes

While investigating a user-reported "Cyrillic-apostrophe-looking"
pattern (`"d'Rab"`, references to `"l'Ancien Testament"`), the same
"stray junk + one bare un-escaped byte" shape used for the Arabic
dot-below/hamza/ayn bytes in `1308d8f` turned up several *more*
corruption bytes:

| byte | mark | count | evidence |
|---|---|---|---|
| `0xA7` | cedilla (combining) | 6,047 (re-confirmed at 6,182 this session) | `"franc" + byte + "ais"` -> français (extremely common word) -- **now implemented** |
| `0xB9` | prime/soft-sign (combining? standalone -- not confirmed which) | 1,810 (re-confirmed at 2,948 this session, confirmed standalone) | `"Il" + byte + "ich"` -> Ilʹich (Russian soft-sign romanization); "B'nai B'rith" -- **now implemented** |
| `0xA8` | ogonek (combining) | 221 | `"Ksia" + byte + "zka"` -> Książka (Polish "book"); `"We" + byte + "gierski"` -> Węgierski |
| `0xAE` | dot-below (combining) -- a **second**, different byte for the same mark `0xA3` already handles | 104 | `"H" + byte + "ammurabi"` -> Ḥammurabi |
| `0xA4` | diaeresis/umlaut (combining) -- a mark with NO existing table entry at all yet | 68 | `"U" + byte + "bersetzung"` -> Übersetzung; `"Beschlü" + byte + "sse"` -- wait, `"Beschlu" + byte + "sse"` -> Beschlüsse |
| `0xA6` | cedilla (combining) -- a **second** byte for the same mark `0xA7` above | small (not precisely counted) | `"revelac" + byte + "a"` + tilde-o -> revelação (Portuguese) |

**Lower-confidence candidates spotted but NOT validated** (showed up
with lower counts and murkier/less clear-cut word matches in the same
scan): `0xB2`, `0xBA`, `0xC1`, `0xB3`, `0xA5`.

### If resuming this cold, read in this order

1. **`docs/MARC8_DIACRITIC_HANDLING.md`** -- the up-to-date summary
   table of every byte/escape this tool fixes, restricts, or leaves
   unconfirmed, with why. Start here, not with this entry's prose.
2. This entry, for the session-by-session narrative/history behind
   that table.
3. `marc_repair.py`'s `fix_marc8_diacritic_escapes` and everything
   between `_MARC8_DIACRITIC_PAYLOADS` and `_fix_marc8_diacritics_in_text`
   (roughly lines 2300-2550 as of this writeup, but it'll have moved).
4. `tests/test_marc_repair_bib.py`'s `TestFixMarc8DiacriticEscapes` for
   worked examples of every sub-case, including the regression tests
   for both regex bugs and all the restricted-byte tests.
5. `docs/MARC8_ESCAPE_ANALYSIS.md` (including its own "Status update"
   section) for the original detect-only analysis this all grew out
   of, and why the CJK/EACC escape case still isn't auto-fixed.


## DONE: re-ran `working/GTU_bibs.mrc` after the `test_unresolvable_record_diverted_to_error_file` fix

Confirms the test fix (`176b947`) was log-wording-only, no behavior
change: 404,956/404,957 records written in 123.92s, the same single
genuinely-unfixable record (403838) as the prior run. Output/log left
in `working/` (gitignored, nothing to commit): `GTU_bibs_repaired.mrc`,
`GTU_bibs_repaired_error.mrc`,
`GTU_bibs_repaired_log_20261007T010333Z.log`.

## DONE: log header readability -- source-vs-migration wording, line wrapping, instance/record count split

Three related `write_log` formatting changes, all in `marc_repair.py`:

1. **Source-vs-migration wording.** `_CHECK_DESCRIPTIONS` entries for
   `suspect_marc8_escape`, `suspect_hex_encoded_marc8`, `doubled_proxy_url`,
   and `holdings_852_b_suspect_content` now explicitly say the defect is
   already present in the source records (or a prior system migration),
   not something this tool/migration introduced -- a cataloger reading the
   log shouldn't have to guess whether this run caused the problem.
2. **Description line wrapping.** New `_write_description_header` helper
   (`marc_repair.py:5156`) wraps a category's description across multiple
   `=== ...` lines (first line `=== `, continuation lines `===   `, only
   the last line closed with ` ===`), capped at `_DESCRIPTION_LINE_WIDTH`
   (78) characters per line -- long descriptions like
   `suspect_marc8_escape`'s used to produce one very long, hard-to-read
   line. `write_log` calls it instead of writing the description inline.
   Regenerated the description blocks in the illustrative fixtures
   `tests/fixtures/kitchen_sink_bib.log`/`kitchen_sink_holdings.log` to
   match (which also caught two already-stale descriptions,
   `fixed_008_length`/`fixed_holdings_008_length`, unrelated to wrapping).
3. **Instance vs. record count split.** The header's count line (`write_log`,
   `marc_repair.py:~5262`) used to always read `"<N> record(s)"`, but `N` was
   actually counting log entries/findings, not distinct records -- a
   category like `suspect_marc8_escape` can log more than one finding per
   record (see the TODO below: 258,202 findings across only 52,847 distinct
   records in one real run), so the old label was misleading. Now: when a
   category's instance count and distinct-record count differ, the line
   reads `"<N> instance(s) in <M> record(s)"`; when they're equal (the
   common case), it stays the plain `"<N> record(s)"` so most categories'
   output doesn't change. New tests in
   `tests/test_marc_repair_core.py::TestWriteLog`
   (`test_count_line_splits_instances_from_records_when_they_differ`,
   `test_count_line_stays_plain_when_one_instance_per_record`) cover both
   branches. Also updated the `_detail_line_marker` regex/docstring in all
   three test files, which previously assumed a description was always
   exactly one line.

Full suite passes except the pre-existing, unrelated
`TestUnfixableErrorFile::test_unresolvable_record_diverted_to_error_file`
failure (confirmed via `git stash` that it fails the same way on
unmodified `main` -- some earlier session added a `"(N records)"`
suffix to the `Problem filename:`/`Source filename:`/`Repaired filename:`
lines via `_named()` but never updated this one test's literal string
assertion; not touched this session since it's out of scope).

## DONE: fixed the stale `test_unresolvable_record_diverted_to_error_file` assertion

Follow-up to the item just above. Updated the test's literal-string
assertion in `tests/test_marc_repair_bib.py:2212` from `"Problem
filename: out_error.mrc\n"` to `"Problem filename: out_error.mrc (1
record)"`, matching the `(N records)` suffix the `_named()` helper
(`marc_repair.py:5146-5149`) already adds. Full suite now passes clean:
371 passed, 1 skipped, no failures. `flake8` on the touched file shows
only pre-existing `E501` long-line debt elsewhere in the file,
unrelated to this one-line change. Committed as `12cb57c` and pushed.

## DONE: full-corpus run against `working/GTU_bibs.mrc` (527.5MB, 404,957 records)

Ran default settings (`python3 marc_repair.py working/GTU_bibs.mrc`):
404,956/404,957 records repaired and written to
`working/GTU_bibs_repaired.mrc`; log at
`working/GTU_bibs_repaired_log_20261006T234719Z.log` (820,878
record-level findings, 820,877 fixed, 1 not fixed). Took 133.93s.

**1 record genuinely unfixable** (record 403838, a Daniel
Schwartz/Second Temple Jewish-history essay collection -- a 505 contents
note, 500 note, 520 summary, several 650s, a 700 with $0/$1 identifiers,
an 020 ISBN, and local holdings-ish fields), written byte-for-byte
unchanged to `working/GTU_bibs_repaired_error.mrc` instead of the main
output. Inspected the raw bytes: this one is missing its **leader and
directory entirely**, not just corrupted -- the file starts straight
into subfield-delimited data (0x1f/0x1e/0x1d all present and well-formed)
with no leader bytes and no tag/length/start directory anywhere. Neither
repair mode can help: Mode 1 needs the leader to roughly match reality
to rebuild from; Mode 2 needs the directory as ground truth to solve
delimiter placement. With zero surviving tag information, there's no
safe way to guess which chunk is which MARC field -- this needs a human
to reconstruct by hand (likely by re-pulling that one record from the
source ILS) rather than an `--overrides` fix.

## DONE: Stage 4 -- full corpus re-run for the per-field 880 transcode fix below

Re-ran the full pipeline against the original `working/WTS_bibs_2026-10-01.out`
(421MB/263,595 records) with this session's Stage 1-3 changes in place,
diffed every log section count against
`working/WTS_bibs_2026-10-01_repaired4_log_20261005T200749Z.log`:
- `transcode_marc8_failed` dropped from 2 to **0** record(s), as expected.
- Every other category's count is byte-for-byte unchanged (in particular
  `removed_880_missing_a` stayed at 91 and `suspect_hex_encoded_marc8` at
  12) -- confirms this change is additive, nothing else regressed.
- The new `removed_untranscodable_subfield` section appeared with
  **4** record(s) worth of findings (3 for `.b11165406` + 1 for
  `.b11227394`), **not the 6 originally predicted** (5 + 1) -- see
  below for why.

**Why the prediction was off (not a bug).** The original scope analysis
confirmed `.b11165406` has 5 `880` fields containing hex-brace
(`{xxxxxx}`) corruption (`246-02`, `246-03`, `246-04`, `246-06`,
`505-09`) and assumed all 5 would throw on transcode, requiring removal.
Re-verified directly against the real `marc8_to_unicode` call on each of
those 5 field values in isolation: only 3 (`246-02`, `246-03`,
`246-04`) actually raise `"Multi-byte position X exceeds length of
marc8 string Y"` (truncated multi-byte) and get caught/dropped/logged.
The other 2 (`246-06`, `505-09`) produce **no warning at all** -- they
"successfully" decode to garbled/wrong text instead, the same
pre-existing silent-corruption gap already documented under "Important
limits of this fix" item 1 below (hex-brace corruption that doesn't
happen to land on a truncated multi-byte boundary passes straight
through unflagged). That gap had only been confirmed at the
whole-corpus/whole-record level before; this is the same mechanism
shown to apply at individual-field granularity too. Conflating "field
contains hex corruption" with "field will throw on transcode" was the
source of the wrong 5-vs-3 prediction -- the code itself needed no
change.

Output and log left in `working/` for review:
`WTS_bibs_2026-10-01_repaired6.mrc` /
`WTS_bibs_2026-10-01_repaired6_log_<timestamp>.log`.

Separately, expanded the `removed_untranscodable_subfield` /
`transcode_marc8_failed` context window in `convert_labeled`
(`marc_repair.py:2683`, both the `UnicodeDecodeError` and
`_Marc8MultibyteTruncated` branches) from a symmetric ~10 characters on
each side to an asymmetric **-10 before / +20 after** the error
position -- more room to see what follows a truncation point without
widening the leading side. Docstring at `marc_repair.py:2611` updated
to match. All 15 `TestTranscodeMarc8` tests still pass (no test pinned
the exact window content).

## DONE: made `removed_untranscodable_subfield`'s detail line readable for non-technical reviewers (e.g. library staff)

User flagged that a real example (`context: '0053}\x1b(B'`) was unreadable
to someone without MARC-8 byte-level knowledge, and that the "+20 after"
window change above had no visible effect for this category. Root
cause of the latter: the `_Marc8MultibyteTruncated` branch's error
position (`exc.byte_pos`, what pymarc calls the position the multi-byte
character needed to extend to) is *by definition* always past the
field's actual last byte -- that's what "truncated" means -- so the
context window's `end` always collapses to `len(raw)` regardless of how
far past it the window reaches; there is never anything to show "after"
within the subfield's own text, no matter the window size.

Two fixes in the `_Marc8MultibyteTruncated` branch of `convert_labeled`
(`marc_repair.py:2699-2721`):
1. Raw `\x1b` escape bytes in the context are now replaced with a
   plain `<escape>` marker before `repr()`-ing the string, same
   convention `find_suspect_marc8_escapes` already uses -- no more raw
   control-character escapes in the log.
2. The message now explicitly states the shortfall (`exc.byte_pos -
   len(raw)`, e.g. "MARC-8 encoding expected 2 more byte(s) than the
   field provided -- character cut off") and the context string gets an
   explicit trailing `<-- field ends here` marker, since the window
   always ends at the field's actual last byte.

Example, same record as above, now reads: `tag 880 $a (MARC-8 encoding
expected 2 more byte(s) than the field provided -- character cut off);
context: '0053}<escape>(B' <-- field ends here; dropped $a and the rest
of =880 with it (nothing usable without it)`.

Docstring at `marc_repair.py:2604` updated to describe both the
`<escape>` substitution and the "field ends here" marker. Updated the
two `TestTranscodeMarc8` tests that pinned the old wording
(`test_untranscodable_a_subfield_drops_whole_field_not_whole_record`,
`test_untranscodable_non_a_subfield_drops_only_that_subfield`) to assert
the new wording/markers instead.

Separately, wrapped the one pre-existing long line flake8 had been
flagging for several sessions (`marc_repair.py`, the `--sample-log`
summary `print` statement) -- no reason it needed to stay long, just
never gotten to. `flake8 --max-line-length=100 marc_repair.py` is now
fully clean with zero exceptions.

Full suite: 361 passed, 1 skipped, the same pre-existing unrelated
failure as every prior session (`TestUnfixableErrorFile::
test_unresolvable_record_diverted_to_error_file`). Re-ran the full
`working/WTS_bibs_2026-10-01.out` corpus once more with both fixes in
place: 263,595/263,595 written, 0 unfixable, every section count
identical to the Stage 4 run above (purely a log-wording change).
Output/log left in `working/`: `WTS_bibs_2026-10-01_repaired7.mrc` /
`WTS_bibs_2026-10-01_repaired7_log_20261006T163333Z.log`.

## DONE: Stage 1-3 -- per-field (not per-record) isolation when `transcode_marc8_to_utf8` hits corrupted MARC-8 content

Follow-up to the "DONE: fixed `transcode_marc8_to_utf8` crash on a lone
surrogate byte" entry below. A user traced a downstream FOLIO-loader
error (some other tool's own ad hoc "Latin-1 leader heuristic", not
this tool's) back to `transcode_marc8_failed` records this tool leaves
in MARC-8 in the main `_repaired.mrc` output. That downstream tool then
force-decoded the still-MARC-8 bytes itself and silently substituted
blank spaces for every CJK character it couldn't map -- confirmed by
reproducing FOLIO's actual loaded 880 content (`마태        `,
`누가        `, etc. -- real word + blank padding where more text
should be) byte-for-byte from this record's raw bytes. That's **already
live in production FOLIO data**, not hypothetical.

**Scope, confirmed against the real 263,595-record
`WTS_bibs_2026-10-01` corpus:** exactly 2 records hit
`transcode_marc8_failed`, both due to the same root cause --
`suspect_hex_encoded_marc8` corruption (upstream hex-encoded,
brace-wrapped MARC-8, see that category's own description) landing in
an 880 field and breaking `transcode_marc8_to_utf8`'s per-record
all-or-nothing conversion:
- `.b11165406` (5-volume Korean commentary, OCLC `ocn823158355`): 5 of
  its 10 `880` fields are hex-corrupted (`246-02`, `246-03`, `246-04`,
  `246-06`, `505-09`) -- each has only `$6`+`$a`, so there's no partial
  fix, the whole field has to go.
- `.b11227394`: only `$c` of its single `880 245-01` field is
  corrupted; `$a`/`$b` on that same field are clean, so only that one
  subfield needs to go.

**Decision (discussed with user, two options considered):**
1. Quarantine the whole record to the `_error` file, same as
   `unfixable` records already are -- makes `_repaired.mrc`'s "every
   record is valid UTF-8" contract absolute, but the library can't
   necessarily fix/reload a fully-quarantined record themselves.
2. **Chosen middle ground:** remove only the specific hex-corrupted
   880 field(s)/subfield(s) that block transcoding (not a whole
   record), then let the rest of the record transcode and flip to
   UTF-8 normally. Verified directly against the real
   `transcode_marc8_to_utf8` function (not simulated): removing the 5
   fields from `.b11165406` and just the `$c` subfield from
   `.b11227394` lets the real function succeed cleanly on both, with
   no exceptions and no silent character substitution, because the
   unrecoverable bytes are discarded outright rather than guessed at.
   Side effect: the 5 counterpart fields on `.b11165406` (`245`, four
   `246`s, `505`) that still carry a `$6` pointing at a now-removed
   880 become dangling links -- already a handled, detect-only, NO
   DATA LOSS category (`dangling_880_link`), not a new problem.

**Stage 1 (core fix), DONE.** Restructured `transcode_marc8_to_utf8`
(`marc_repair.py:2585`) to isolate a conversion failure per field/
subfield instead of aborting the whole record: on a data-field
subfield's transcode failure, drop just that subfield (the whole field
too if the dropped subfield was `$a` or nothing usable is left), log
it via the function's new `removed_details` return value, then still
convert and flip the leader on everything else. A *control* field's
own failure still aborts the whole record (unchanged from before --
control fields are fixed-format and essentially never carry MARC-8
escape content, so isolating a failure there wasn't needed). Function
signature changed from `-> bool` to `-> tuple[bool, list[str]]`
(`(transcoded, removed_details)`); its one call site in `main()`
(`marc_repair.py:6467`) updated to log each `removed_details` entry
under a new always-on category, `removed_untranscodable_subfield`
(FIXED/REQUIRES ATTENTION, "DATA LOSS" -- genuinely unrecoverable bytes
discarded outright, not guessed at). Named it generically rather than
the originally-suggested `removed_untranscodable_880` since the
restructured function isolates a failure on *any* data field tag, not
just 880 -- mirrors `removed_invalid_subfield`'s own non-tag-specific
naming. Registered in `_CHECK_DESCRIPTIONS`, `_FIXED_REQUIRES_ATTENTION`,
`_ALWAYS_FULL_CATEGORIES`, and `active_categories` in
`main()` (alongside `transcoded_marc8`/`transcode_marc8_failed`, under
the same `args.transcode_marc8` guard). `docs/REPAIR_CATEGORIES.md`
regenerated via `tools/generate_repair_categories_doc.py` (new row
right after the existing "Transcode failure" one).

**Stage 2 (tests), DONE.** All in `TestTranscodeMarc8`
(`tests/test_marc_repair_bib.py`):
- Updated 4 existing tests for the new `tuple[bool, list[str]]` return.
- Replaced `test_atomic_on_failure_partway_through_record` (tested the
  now-gone all-or-nothing contract) with
  `test_control_field_failure_still_aborts_whole_record`, confirming
  the one case that still aborts fully.
- `test_untranscodable_a_subfield_drops_whole_field_not_whole_record`
  and `test_untranscodable_non_a_subfield_drops_only_that_subfield`:
  unit tests using the *actual real MARC-8 bytes* from `.b11165406`'s
  246-02 880 `$a` and `.b11227394`'s 880 `$c` (same raw strings already
  used as `TestFindSuspectHexEncodedMarc8` fixtures just above in the
  same file) -- confirmed these genuinely trigger pymarc's real
  "Multi-byte position ... exceeds length of marc8 string ..." stderr
  warning (not simulated/monkeypatched) when run through the real,
  non-monkeypatched `transcode_marc8_to_utf8`.
- `test_other_hex_corrupted_records_from_corpus_unaffected`: extracted
  the real 3 hex-corrupted-but-non-throwing records (`.b11077347`,
  `.b11188236`, `.b11257982`) from `working/WTS_bibs_2026-10-01.out`
  into a new committed fixture,
  `tests/fixtures/hex_encoded_marc8_no_throw.mrc` (force-added past the
  `*.mrc` gitignore pattern, same as the other real-data fixtures
  already there) -- confirms this change removes nothing from them.
- `test_bib_pipeline_logs_removed_untranscodable_subfield`: full
  `main()` run on a synthetic record carrying the real `.b11165406`
  246-02 bytes, asserting the new category's log header/DATA LOSS
  wording/detail line, the 880 actually gone from the output record,
  and the rest of the record still flipped to UTF-8.

**Stage 3 (full suite + lint), DONE.** 361 passed, 1 skipped, 1
pre-existing unrelated failure (`TestUnfixableErrorFile::
test_unresolvable_record_diverted_to_error_file` -- confirmed via `git
stash` that it fails identically on `main` before this session's
changes; a stale "Problem filename:" log-header assertion, nothing to
do with this fix). `flake8 --max-line-length=100` clean except the
same pre-existing long line (`marc_repair.py:6127`, the sample-log
print statement).

**Also worth a look, separately (not acted on):** the FOLIO records
already loaded with the blanked-out text (at least `.b11165406`'s
246/505 alternate titles) will need to be identified and reloaded/
corrected once this fix is deployed -- the bad data is already live,
this fix only stops it from recurring.

**Important limits of this fix, even once implemented -- do not
overclaim "the whole file is now UTF-8":**
1. **Fixes thrown failures, not silent wrong-output.** This only
   catches corruption that makes `marc8_to_unicode` raise. Of the 5
   records flagged by `suspect_hex_encoded_marc8`, only these 2 throw.
   The other 3 don't throw today either -- their literal `{xxxxxx}`
   hex-brace text just passes through unchanged as valid-but-garbled
   ASCII/UTF-8, "successfully" transcoded with wrong content. This fix
   does not touch that case; it's a separate, pre-existing gap (same
   mechanism that already silently corrupted `.b11165406` in FOLIO,
   just a milder variant that happens not to throw).
2. Depends on `--transcode-marc8` staying enabled (on by default;
   `--no-transcode-marc8` bypasses this entirely).
3. Does not apply to holdings records -- MARC-8-to-UTF-8 conversion is
   already skipped entirely for holdings (see
   `holdings_escape_sequence`), a separate known limitation.
4. Only covers the main `_repaired.mrc` output, not the `_error` file
   -- `unfixable` records are written there byte-for-byte untouched by
   design and are out of scope here.
5. Only guards against corruption shapes that actually throw. A novel
   corruption pattern that fails silently (like item 1) wouldn't be
   caught just because this fix landed.

## DONE: shortened/readable detail messages for `suspect_marc8_escape` and `suspect_hex_encoded_marc8`

All 345 tests passing (1 skipped), flake8 clean.

**`find_suspect_marc8_escapes`** (`marc_repair.py:2151`):
1. Fixed an O(N²) bug -- `context_preview` joined *every* occurrence's
   window in a field with `" ... "`, then every finding in that field
   repeated the whole joined string. Each finding now carries only its
   own window.
2. Trimmed repeated boilerplate out of the per-finding message (dropped
   "in the source record, not real {charset} content").
3. Shortened the suggestion's closing clause ("compare against another
   edition or an authority record to confirm the correct spelling" ->
   "verify against another source").
4. The `context:` field no longer shows the raw `\x1b` escape bytes via
   `repr()` -- they're replaced with a plain `<escape>` marker before
   reprinting, since the before/after letters are already shown
   separately in the message.

**`find_suspect_hex_encoded_marc8`** (`marc_repair.py:2508`), closing out
the "NEXT TASK" note left by the previous session: when a decode is
`recoverable` (the common "NO DATA LOSS (apparent)" case), the preview is
now run through a new `_marc8_bytes_to_readable_preview` helper that
transcodes the recovered MARC-8 bytes to Unicode via pymarc's
`marc8_to_unicode` (same mechanism `transcode_marc8_to_utf8` already
uses) instead of `repr()`-ing the raw latin-1 bytes. E.g. a genuine
CJK/EACC escape that used to show as `'\x1b$1oOfoH_oQFoVf\x1b(B'` now
shows as the actual decoded text (confirmed against the real production
example in `test_flags_double_encoded_run_and_recovers_escape_sequence`:
now reads `'· 마가복음'`). Falls back to the raw latin-1 `repr()` if
pymarc isn't installed, the transcode raises, or pymarc logs a warning
(e.g. a truncated multi-byte character) -- this is a best-effort log
preview, not a correctness check. The `context:` field (surrounding raw
field text, not the decoded payload) was left untouched -- it's meant to
show *where* in the field the run sits, and still can.

Also updated `tests/fixtures/kitchen_sink_bib.log`'s one
`suspect_marc8_escape` line, which had gone stale even before this
session's wording changes (it was showing the older single-character
suggestion wording instead of the CJK/EACC-specific one).

## Session summary (2026-10-02)

Work done on `marc_repair.py` this session:

1. **MARC-8 multi-byte truncation now surfaces as a logged failure.**
   pymarc's `marc8_to_unicode()` doesn't raise for a truncated multi-byte
   (CJK/EACC) character -- it writes `"Multi-byte position X exceeds
   length of marc8 string Y"` straight to stderr and silently substitutes
   a blank space. `transcode_marc8_to_utf8` now captures stderr around
   that call and converts the warning into a proper `RuntimeError`,
   labeled with the tag/subfield and ~10 bytes of context around the
   truncation point. Flows through the existing `transcode_marc8_failed`
   category (already always fully logged via `--log`). See
   `_Marc8MultibyteTruncated` / `_MARC8_MULTIBYTE_TRUNCATED_RE`.

2. **Repaired output always gets a `.mrc` extension**, regardless of the
   input's own extension (`.marc`, `.dat`, `.txt`, or none). Fixed in
   `_default_output_path`, the split-mode bib/holdings repaired paths,
   and `--repair-holdings`'s default path. This matches what `--out`'s
   help text already promised but the code didn't deliver.

3. **Added `--sample-problems PATH` / `--sample-limit N`** (default limit
   25). Writes the raw, original bytes of up to N records that
   triggered at least one not-fixed finding (unfixable,
   `transcode_marc8_failed`, `suspect_marc8_escape`, etc.) to PATH as a
   small standalone `.mrc` file -- for handing a compact sample of a
   file's actual problems to someone (or something) else for analysis,
   without sending the whole file.
   - **Known limitation:** only wired into the default bib pipeline in
     `main()`, not `--repair-holdings` or `--split-bib-holdings` yet.
   - **Known limitation:** misses the rare case where a tag gets renamed
     (`pending_tag_fixes`, logged only after the whole loop ends) or a
     duplicate identifier is found (found only after a full pass) --
     both happen after the per-record sampling check runs.

All 326 tests passing (1 skipped) as of this session's last run.

## DONE: truncate duplicate-245 body in the log detail

Confirmed with an actual sample: the long string was never the section
header (those stay fixed, e.g. "=== FIXED/REQUIRES ATTENTION:
removed_non_repeatable_duplicate ==="); it was the removed field's own
content embedded in the per-record detail line by
`strip_duplicate_non_repeatable_fields` (`marc_repair.py:3337` after
this change, logged around `marc_repair.py:5875`) -- most visibly on
an exact-duplicate 245, where the full title (indicators + all
subfields) got echoed verbatim even though "exact duplicate" already
says all a reader needs to know.

Added `_DUPLICATE_FIELD_BODY_TRUNCATE_LEN = 25` and truncate the
field's rendered body (indicators+subfields, or control content) to
that length + "..." before building the detail string, for every tag
this check handles (001/005/008/245/etc.), not just 245 -- the same
long-body problem applies to any non-repeatable tag with free-text
content. Short bodies (e.g. an edition statement like "2nd ed.") are
left untouched.

New tests: `test_long_title_body_truncated_in_detail`,
`test_short_body_not_truncated` in `tests/test_marc_repair_bib.py`.

All 328 tests passing (1 skipped), flake8 clean.

## DONE: analyzed `suspect_marc8_escape` findings for a recurring pattern

Ran `marc_repair.py` with `--log-full suspect_marc8_escape` against
`WTS_bibs_2026-10-01.out` (421MB / 263,595 bib records) to pull all 255
findings and look for a reliable escape-payload -> intended-letter mapping
that could upgrade the category's generic "ö, é, ñ, ü" suggestion into a
specific one.

Full writeup: [docs/MARC8_ESCAPE_ANALYSIS.md](docs/MARC8_ESCAPE_ANALYSIS.md).
Short version: the pattern is real (virtually all findings are real German/
French words missing a diacritic, corroborated against thousands of
correctly-encoded instances of the same words elsewhere in this file) but
**does not support a static payload -> letter table** -- many different
3-byte escape payloads resolve to the same letter, so the mapping is
mediated by recognizing words, not by decoding bytes. Also found that the
CJK/EACC escape (3 raw bytes) typically swallows the diacritic + base
letter + one more plain letter, not just one character, which the
category's current suggestion text doesn't account for.

No code changes made -- `find_suspect_marc8_escapes` stays detect-only.

**Rejected approach:** a static payload -> letter lookup table. Confirmed
unreliable (many different 3-byte payloads resolve to the same letter) and
specific to this one file's own copy-cataloged vocabulary -- won't
generalize to other libraries' files, which won't have the same recurring
words. Do not build one.

## DONE: two concrete, generalizable improvements to `suspect_marc8_escape`

Both implemented, both still detect-only. All 340 tests passing (1 skipped),
flake8 clean (`--max-line-length=100`, per README.md).

Follow-up: added `--no-marc8-corpus-lookup` (store_false onto
`args.marc8_corpus_lookup`, default `True`) to skip item 2's corpus-index
build entirely -- it costs one extra full read of the input file, held in
memory as a single decoded string, on a large legacy MARC-8 file. Wired in
`main()` right where `marc8_corpus_index` is built: calls
`build_marc8_corpus_index` only when the flag allows it, otherwise uses
`({}, {})` (same as the function's own graceful-fallback empty indexes).
Stays on by default. New `TestMarc8CorpusLookupCli` class
(`tests/test_marc_repair_bib.py`) exercises this end-to-end via `m.main()`
on a real 2-record file (one with the CJK-escape finding, one with
"Tübingen" spelled correctly elsewhere in the same file): default run
names the word specifically, `--no-marc8-corpus-lookup` keeps the generic
hint.

**1. Fixed the suggestion text's character-count claim.** The CJK/EACC
branch of `find_suspect_marc8_escapes`'s suggestion (marc_repair.py) now
reads "likely an accented letter and the letter right after it" instead of
"likely a miskeyed accented letter"; the 1-byte-charset branch (Hebrew/
Arabic/Cyrillic/Greek) keeps the original single-character wording
unchanged. New/updated tests in `TestFindSuspectMarc8Escapes`
(`tests/test_marc_repair_bib.py`): `test_flags_single_cjk_char_welded_to_ascii_letters`
updated for the new wording; added
`test_flags_single_greek_char_welded_to_ascii_letters_single_byte_wording`
to pin the unchanged 1-byte wording.

**2. Added the opportunistic same-file corpus lookup.** New
`build_marc8_corpus_index(input_path, encoding_used)` does a single regex
pre-pass over the raw input bytes for every correctly-encoded ANSEL
diacritic+letter pair, building two indexes keyed by the surrounding
ASCII runs: `index_exact` (direct fragment match, for 1-byte charsets) and
`index_minus_one` (fragment match with the trailing letter of
`ascii_after` dropped, for CJK/EACC -- accounts for the extra swallowed
letter from improvement #1). New `lookup_marc8_corpus_word(corpus_index,
charset_name, word_before, word_after)` resolves a finding's fragment
against it, returning a word only on an unambiguous single match (a
`_MIN_CORPUS_MATCH_FRAGMENT_LEN = 3` floor skips too-short fragments).
`find_suspect_marc8_escapes` takes an optional `corpus_index` parameter
(default `None`, so existing callers/tests are unaffected) and, when a
fragment resolves, swaps in "likely 'Tübingen' -- found spelled correctly
elsewhere in this file" instead of the generic hint. Degrades to two empty
indexes (and thus always falls back to the generic hint) when the file was
read as UTF-8 or pymarc isn't installed -- no new failure mode or hard
dependency.

Wired into `main()`: built once via `build_marc8_corpus_index(args.input,
encoding_used)` right after `encoding_used` is determined, then passed into
the existing `find_suspect_marc8_escapes(parsed, ...)` call inside the main
per-record loop. Not wired into `repair_holdings_records` (MARC-8 escape
detection isn't run there at all, same as before). New tests in
`TestFindSuspectMarc8Escapes` (hand-built `corpus_index` tuples, no file
I/O): match found, no match found, ambiguous match. New
`TestMarc8CorpusIndex` class (writes real bytes to `tmp_path`, exercises
`build_marc8_corpus_index` + `lookup_marc8_corpus_word` together): resolves
an unambiguous "Tübingen" fragment, returns `None` on no match, returns
`None` on an ambiguous two-word fragment, and returns empty indexes when
`encoding_used="utf-8"`.

## RESOLVED: WTS_bibs_2026-10-01.out "no output" -- O(n^2) corpus-index bug

Root cause found and fixed (2026-10-02). It was never a buffering or
EC2-specific issue -- `build_marc8_corpus_index` (added in `2a69437`,
the same-file corpus lookup for `suspect_marc8_escape`) decoded the
*whole* input file to one string, then for every `_MARC8_DIACRITIC_BYTE_RE`
match did `text[:pos]` and `text[pos + 2:]` -- unbounded slices copying
an ever-larger chunk of the whole file on every single match. On
`WTS_bibs_2026-10-01.out` (421MB) there are 149,138 such matches, so
this was genuinely O(n^2): hundreds of MB copied per match, ~150k times
over. The process pegged one core at ~100% CPU (mostly *sys* time from
the huge repeated allocations, confirmed via `/proc/<pid>/fd` showing
no open fd for the input/output files at all -- it had already
`read()` the whole file once and was stuck purely in-memory) and never
produced output or finished, locally or on EC2 -- it would have run
effectively forever on a file this size.

Fix in `marc_repair.py` (`build_marc8_corpus_index`): bound both
slices to a short window (`_MAX_WORD_FRAGMENT_WINDOW = 100` chars) --
`text[max(0, pos - 100):pos]` / `text[pos + 2:pos + 2 + 100]` -- since
the surrounding word fragment it's trying to capture is never longer
than that. Turns the whole index build back into O(n). All 345 tests
pass; a full local re-run of `WTS_bibs_2026-10-01.out` now completes in
107.53s (matches the ~100-110s originally expected) and reports
`transcode_marc8_failed: 2 record(s)` (`.b11165406`, `.b11227394`) as
expected. Re-confirmed again (2026-10-02, 95.21s this time) after the
start/finish/elapsed logging change below -- same 2-record result, so
that change didn't regress anything. **Confirmed on the EC2 box itself
too (2026-10-02, user-reported)** -- fix is fully verified end to end,
nothing further to do here.

## DONE: log start/finish/elapsed time

User wanted the end-of-run `--log` file to record wall-clock start
time, finish time, and elapsed duration (not just the stdout summary
line's elapsed seconds, which doesn't persist in the log file itself).

Added `_write_run_timing_header` (`marc_repair.py`, near `write_log`):
writes a single `=== RUN: started <ts>, finished <ts>, elapsed <N>s ===`
line as the first line of the log file, elapsed to .01s (wall-clock
`finished - started`, not `time.perf_counter()`, so it matches the
timestamps on the same line). Wired into both `main()` and
`repair_holdings_records` right before their respective `write_log`
calls, each capturing its own `run_started` at function/run entry.
`check_holdings_records` (the detect-only helper behind
`--split-bib-holdings`'s check path) was not touched -- it's dead code,
not called anywhere in `marc_repair.py` outside tests.

Verified manually on `tests/fixtures/kitchen_sink_bib.mrc` and
`kitchen_sink_holdings.mrc` (both pipelines) and on the full
`WTS_bibs_2026-10-01.out` run above. All 345 tests pass, flake8 clean.
Committed and pushed as `13d017c`.

## DONE: gitignore WTS_bibs_*.out

Added to `.gitignore` -- these are large EC2-pulled real-world test
files, never meant to be committed. Committed alongside the logging
change above (`13d017c`).

## DONE: log source/repaired/problem filenames; add `--sample-log`

User wanted every combined log to name the run's source/repaired/
problem files underneath the timing header, so the log is
self-describing without needing the original invocation.

Extended `_write_run_timing_header` (`marc_repair.py`) to take
`source_path`/`repaired_path`/`problem_path` and write `Source
filename:`/`Repaired filename:`/`Problem filename:` lines right after
the `=== RUN: ... ===` line, followed by a blank line before `write_log`'s
own output (which already appends, via `open(path, "a")`, so ordering
Just Works). Both call sites updated: `main()` passes `args.input`,
`out_path`, `error_path`; `repair_holdings_records` passes
`input_path`, `output_path`, `error_path`.

Also added `--sample-log [PATH]`, requested separately in the same
session: writes a full log documenting every category in
`_CHECK_DESCRIPTIONS` (header + description + `0 record(s)`, no
per-record findings) without reading any input at all -- handled
before the `args.input` prompt loop so it needs no real file. Meant as
a reference for the log's full structure. Ran once, output left at
`sample_files/sample_log_20261005T*.log` (gitignored, like every other
`.log`, so it won't show up as a change to commit).

All 345 tests pass. Committed and pushed as `fdf8545`.

Separately, answered a question (not yet acted on / nothing to verify)
about updating the non-git EC2 install: since that directory was built
by `install.sh`'s minimal-fetch mode (see the TODO at the top of this
file), the correct update command is the `curl | bash` one-liner
pointed at the existing `--dir`, not a local `./install.sh` (which may
not exist there) -- re-run:
`curl -fsSL https://raw.githubusercontent.com/marnold-ebsco/marc-repair/main/install.sh | bash -s -- --dir /working/migration/scripts/marc_repair`.

## Checked: install.sh was not actually broken

The modified-install.sh seen in git status at the start of a session
turned out to be a stale snapshot artifact, not a real uncommitted
change -- `git diff` against HEAD was empty, `bash -n install.sh`
passed, and it already contains every fix from recent commits (clear
error for a missing flag value, executable bit, the nesting guard,
self-update). No revert or fix was needed or made.

## TODO: check whether ProgressReporter/estimator needs updating for the corpus-index fix

Checked at a glance (2026-10-02): `ProgressReporter` (and its
`maybe_print_estimate` rate/ETA calc) is constructed at line ~6079,
*after* `build_marc8_corpus_index` already ran (~6026) -- so its own
`start_time` doesn't include corpus-index-build time, and the O(n^2)
bug didn't corrupt the rate/ETA math. No estimator code change made.

Still worth a closer look next session: `build_marc8_corpus_index` is
now O(n) instead of O(n^2), but it's still a full pass over the whole
input file that happens *before* the first progress line prints --
on a big enough file this could again look like a multi-second "no
output" stall right at startup, just a bounded one now instead of an
unbounded hang. Consider whether it needs its own "scanning file for
corpus lookup..." progress indicator -- that specific piece is still
open; see the DONE entry below for a related but separate fix (the
*estimate's* sample window, not the corpus-index build itself).

## DONE: log header omits unwritten problem file + basenames; `maybe_print_estimate` warm-up fix

All 350 tests passing, flake8 clean on the touched regions (no new
findings beyond this file's existing pre-change lint debt).

User reported a real run where the printed "Estimated total runtime"
was wildly off: ~1h03m predicted for 421.2MB, actual run finished in
about 2 minutes (~30x overestimate). Root cause: `ProgressReporter
.maybe_print_estimate` (`marc_repair.py`) measured its rate sample
from `self.start_time`, set at `ProgressReporter.__init__` --
essentially at the very start of the run, before the first disk read.
A slow first chunk read (no OS read-ahead/page-cache warmed up yet,
plausible on EC2/network-backed storage) could burn several seconds
while only a couple hundred small records got through, and that
slow-start window dominated the 200-record/1s sample, producing a
rate far below the tool's real steady-state throughput.

Fix: the estimate's sample window is no longer anchored at
`start_time`. It now anchors once `_ESTIMATE_WARMUP_RECORDS` (20)
records have gone by -- that first call just records where the real
window starts and returns without evaluating anything -- and only
*then* waits for the usual 200 further records / 1.0s further elapsed
before firing. Whatever happened before the 20-record mark (almost
certainly dominated by that first slow read) is discarded outright
rather than diluted. Added `TestProgressEstimate
.test_warmup_sample_discards_slow_startup`, which reproduces the bug
shape directly (20s burned on the first 20 records, then a fast/
steady post-warm-up window) and asserts the estimate matches the
steady-state rate, not the slow start. The other `TestProgressEstimate`
tests were updated for the new two-call handshake (first call past
the warm-up floor only anchors the window; a second call is needed to
actually evaluate/fire).

Separately, two smaller log-header fixes landed in the same commit
(`b1b8223`), both in `_write_run_timing_header`:
1. `Problem filename:` is now omitted entirely when there's no actual
   problem/error file -- it's written lazily by `_LazyBinaryWriter`
   and never created when nothing was unfixable, so naming it
   unconditionally was misleading. Both real call sites now pass
   `error_path if os.path.exists(error_path) else None`; the
   `--sample-log` placeholder call site is unaffected (it always
   passes a real, non-None placeholder string).
2. All three header filenames (source/repaired/problem) are now
   `os.path.basename()`'d -- the log is meant as an at-a-glance
   record, not a durable pointer back to the files (stdout/args
   already have the full paths).

Committed and pushed as `b1b8223` (stacked on `f983a4a`, the prior
session's HANDOFF-only commit).

## DONE: dedicated DATA LOSS category for 010 fields with empty/effectively-empty $a

User found that every problem record recovered into
`WTS_source_FOLIO_full.mrc` (via `tools/pull_full_problem_records.py`)
shares the same defect: a 010 (LCCN) field whose $a is all spaces (12
blanks), with the real canceled/invalid LCCN sitting in $z instead --
e.g. `=010  \\$a            $z   70185211`. `strip_missing_required_a`
already detects this shape (its `_is_punctuation_only` check treats a
whitespace-only string the same as a literal "."), but running it
against the existing `required_a_tags.txt` heading-field set would
have logged the removal under the generic `field_removed_because_missing_a`
category, worded "POSSIBLE DATA LOSS" -- not accurate here, since a
010 missing $a this way routinely still carries a real $z that gets
discarded right along with it.

Added a second, separate call to `strip_missing_required_a(parsed,
_010_REQUIRED_A_TAGS)` (`_010_REQUIRED_A_TAGS = frozenset({"010"})`,
`marc_repair.py`) in the bib pipeline, logged under its own always-on
category `removed_010_missing_a` (FIXED/REQUIRES ATTENTION, worded
plain "DATA LOSS" -- see its `_CHECK_DESCRIPTIONS` entry). Registered
in `_FIXED_REQUIRES_ATTENTION`, `_ALWAYS_FULL_CATEGORIES`, and the
unconditional `active_categories` set in `main`. Added
`tools/generate_repair_categories_doc.py`'s row for it and regenerated
`docs/REPAIR_CATEGORIES.md`.

Separately, dropped the per-row timestamp from `LogEntry.render()` --
every detail line repeated the run's own start time for no reason
(`write_log`'s header already states it once); rows are now just
`\trecord N (id)\tdetail`.

Added `TestStrip010MissingA` to `tests/test_marc_repair_bib.py`
(unit-level reuse check, a valid-$a record staying untouched, and a
full `main()` pipeline run asserting the new category/section/DATA
LOSS wording and that the full field -- including $z -- is in the log
line). All 353 tests pass; flake8 clean (one pre-existing long line at
`marc_repair.py:5991`, unrelated).

Ran the tool against `WTS_source_FOLIO_full.mrc` as requested, output
left in the repo root: `WTS_source_FOLIO_full_repaired.mrc` and
`WTS_source_FOLIO_full_repaired_log_20261005T182809Z.log`. 130/132
records had their 010 removed this way; the 2 left untouched are
`.b11065898` (907 $a -- has a genuinely valid LCCN in $a,
`2007043817`) and `.b11081624` (907 $a -- has no 010 field at all).

Committed and pushed as `46f5d52`.

## Analysis: 066 (Character Sets Present) fields missing $a across `WTS_bibs_2026-10-01_repaired.mrc`

User asked for every record whose 066 has no subfield $a, with its 907 $a
and the 066's raw contents, for manual review. Wrote
`tools/list_066_missing_a.py` (uses pymarc, run via the repo's `venv`) --
walks the file once, collects `(907 $a, rendered 066 subfields)` for every
066 lacking $a, writes a TSV to the repo root. Found **1037** such records;
output left at `066_missing_subfield_a.tsv` (gitignored like the other
root-level data exports).

Two of those records -- `.b11081624` and `.b11065898`, both already
flagged above for their 010 $a situation -- were asked about specifically.
They do **not** stand out: both have `066 $c(S` and only `$c(S`, which is
one of the more common shapes in the list (75 of the 1037 records), not an
outlier.

**What the 066 subfield codes actually mean.** They aren't free text --
each is an ISO 2022/MARC-8 escape-sequence identifier (or, for UTF-8
records, a plain ISO 15924 script tag) naming a character set used
somewhere in the record, confirmed against LC's MARC-8 specification
(`https://www.loc.gov/marc/specifications/speccharmarc8.html`):

| Code | Meaning |
|---|---|
| `(B` | Basic Latin (ASCII) |
| `(N` | Basic Cyrillic |
| `(S` | Basic Greek |
| `(2` | Basic Hebrew |
| `(3` | Basic Arabic |
| `(4` | Extended Arabic |
| `(Q` | Extended Cyrillic |
| `$1` | Chinese/Japanese/Korean (EACC) |
| `Grek` / `Cyrl` / `Hebr` / `Hani` / `Armn` / `Syrc` / `Zsym` | ISO 15924 script tags (Greek, Cyrillic, Hebrew, Han, Armenian, Syriac, Symbols) -- used directly instead of an escape sequence in UTF-8-encoded records |

So the file mixes two notations for the same underlying information
(older MARC-8 escape form vs. newer ISO 15924 tag form) depending on which
encoding a given record was in -- e.g. `$c(S` and `$cGrek` both just mean
"this record uses Greek characters," they're not different findings.

**Follow-up: why `b11081624`/`b11065898` actually fail to load.** Not an
066-decoding question after all -- pulled both full raw records directly
from `WTS_bibs_2026-10-01_repaired.mrc` (`tools/_dump_offset.py`, scratch,
not committed) and found the real defect. Both records have an `880`
field (alternate graphic representation -- the vernacular-script version
of a heading) linked via `$6` to their `100` field and explicitly marked
Greek (`$6 100-01/(S`, matching the record's own `066 $c(S`), but the
`880`'s `$a` -- where the actual Greek text belongs -- is blank:
- `.b11081624`: `880 ‡6 100-01/(S ‡a "          "` (10 spaces, nothing else)
- `.b11065898`: `880 ‡6 100-01/(S ‡a "         ."` (9 spaces + a period)

No `\x1b` (MARC-8 escape) bytes anywhere in either raw record and the
leader's char-coding-scheme byte is `a` (UTF-8) on both -- ruled out a
transcoding/escape-sequence bug. This is upstream source data loss (the
vernacular heading was never actually cataloged, just linked), not an
encoding problem, and it's the same defect shape already written up above
for these two records' `010` fields -- except `strip_missing_required_a`
never catches it here because `880` isn't in `required_a_tags.txt`, so
`marc_repair.py` currently leaves the blank `880` in place untouched.
Whatever's failing to load these records downstream is almost certainly
choking on that blank/punctuation-only `880 $a`.

**Not yet decided:** whether to add `880` to `required_a_tags.txt` (would
make `strip_missing_required_a` remove it under the existing
`field_removed_because_missing_a` category) or give it its own dedicated
DATA LOSS category the way `010` got (`removed_010_missing_a`) -- an 880
missing its vernacular text is arguably always worth calling out
specifically rather than folding into the generic category, same
reasoning as the 010 case. Whether this pattern is widespread enough to
be worth generalizing (a quick scan of how many 880 fields across the
file have whitespace/punctuation-only $a) hasn't been done yet.

**Suggested fix, not yet made:** add a "meaning" column to
`list_066_missing_a.py`'s TSV output, decoding each subfield via a small
lookup table (the one above, extended to cover every code actually seen in
`066_missing_subfield_a.tsv` -- run `cut -f2 066_missing_subfield_a.tsv |
sort -u` to get the full set) so a reviewer doesn't have to look up each
code by hand. Low-risk, additive change to a standalone reporting script
(not `marc_repair.py` itself) -- no tests exist for `tools/` scripts yet,
so this would be the first; a plain unit test around the decode-table
lookup function would be enough.

## DONE: dedicated DATA LOSS category for 880 fields with empty/effectively-empty $a

Follow-up to the 066/880 analysis above. User asked to flag 880 fields
missing $a; decided (after a quick tradeoff discussion) to mirror the 010
precedent exactly rather than literally adding "880" to
`required_a_tags.txt` -- an 880's only other subfields ($6 linking data,
etc.) never carry the vernacular heading content itself, so a blank/
punctuation-only $a there is definite DATA LOSS, not just POSSIBLE, same
reasoning as 010's $z.

Added `_880_REQUIRED_A_TAGS = frozenset({"880"})` (`marc_repair.py`, next
to `_010_REQUIRED_A_TAGS`), a second unconditional
`strip_missing_required_a(parsed, _880_REQUIRED_A_TAGS)` call in the bib
pipeline logged under its own always-on category `removed_880_missing_a`
(FIXED/REQUIRES ATTENTION, worded plain "DATA LOSS"). Registered in
`_FIXED_REQUIRES_ATTENTION`, `_ALWAYS_FULL_CATEGORIES`, and the
unconditional `active_categories` set in `main`, same as 010. Added
`tools/generate_repair_categories_doc.py`'s row and regenerated
`docs/REPAIR_CATEGORIES.md`. Added `TestStrip880MissingA` to
`tests/test_marc_repair_bib.py`, mirroring `TestStrip010MissingA`.

All 356 tests pass (1 skipped); flake8 clean (same pre-existing long line
at `marc_repair.py:6012`, unrelated). Verified against the real two
records (`.b11065898`, `.b11081624`) by extracting just those two into a
scratch file and running the tool: both 880s removed and logged, e.g.
`removed =880  0 $6100-01/(S$a          \t($a is punctuation only;
content discarded)`.

**Not yet done:** regenerating the full-file log/output against
`WTS_bibs_2026-10-01_repaired.mrc` to get the real total count of 880s
this newly removes across the whole file -- a full re-run hit an
unrelated crash (see TODO immediately below) before reaching a count.

## DONE: fixed `transcode_marc8_to_utf8` crash on a lone surrogate byte when re-running against an already-UTF-8 file

Follow-up to the TODO this replaces. Root cause turned out to be
different from the original guess (bisecting by naive record-count
split kept *not* reproducing the crash in isolation, which was the
giveaway):

`_read_text_with_encoding` picks ONE encoding for the whole input file
(`detect_encoding`). `WTS_bibs_2026-10-01_repaired.mrc` is mostly
already UTF-8 (output of a prior repair run), so the whole file decodes
as UTF-8 -- except one straggler record, `.b11165406` (a 5-volume Korean
commentary set), whose leader byte 9 is still `" "` (never got
transcoded) and whose 245/246/505 fields carry a raw ANSEL diacritic
byte (`0xe6`) that isn't valid UTF-8 on its own. The UTF-8 decoder
(`errors="surrogateescape"`) preserves that invalid byte as a surrogate
character (`\udce6`), not as the literal latin-1 code point `convert`'s
comment assumed it would always be. `text.encode("latin-1")` (strict)
can't represent a surrogate at all, hence the crash.

Confirmed by monkeypatching `transcode_marc8_to_utf8` to print the
record on failure (isolating just that one record into its own file
was a red herring -- `detect_encoding` then picks *latin-1* for that
tiny file instead of utf-8, so the byte decodes as a literal `\xe6` and
nothing crashes; the discrepancy only shows up at full-file scale).

Fix: `text.encode("latin-1", errors="surrogateescape")` instead of
plain `text.encode("latin-1")` at `marc_repair.py:2621` (now a few lines
later after the comment rewrite) -- reverses a surrogate-escape exactly
when one was applied upstream, and is a no-op otherwise, so it's correct
whether the whole-file decode used utf-8 or latin-1. Added
`test_converts_surrogate_escaped_byte_from_utf8_decoded_input` to
`TestTranscodeMarc8` in `tests/test_marc_repair_bib.py`, mirroring the
existing `test_converts_combining_diacritics_and_flips_leader_byte` but
with `\udce5`/`\udcf2` (surrogate-escaped) instead of `\xe5`/`\xf2`
(literal latin-1) in the subfield text. All 357 tests pass (1 skipped);
`flake8 --max-line-length=100` clean (same pre-existing long line at
`marc_repair.py:6025`, unrelated -- note flake8 needs that flag, see
README.md:530; running it with flake8's bare 79-char default floods
hundreds of false positives across the whole file).

Re-ran the full pipeline against `WTS_bibs_2026-10-01_repaired.mrc`
(263,595 records) with the fix in place: completes cleanly now, no
crash, but still left 9 records UNFIXABLE -- NOT pre-existing/unrelated
after all, see the `parse_directory` fix immediately below, which
resolves them too.

## DONE: fixed `parse_directory` accepting a phantom directory entry that coincidentally matched real cumulative lengths

User pushed back on the 9 UNFIXABLE records above with "they shouldn't
exist" -- right instinct. Traced one (`.b12765077`-ish content,
originally OCLC `on1341033027`) back to the *original* pre-repair
source (`working/WTS_bibs_2026-10-01.out`): byte-for-byte, that record
is a perfectly clean, single, well-formed ISO 2709 record there (one
real directory, one real terminator per field, total length matches
the leader exactly) -- repairing it the first time round produces
valid output (confirmed by checking every field's byte span for stray
0x1E/0x1D: none). But feeding that *valid output* back into
`marc_repair.py` a second time reproduced the exact same "no split
satisfies the rest of the record" + 8x "no consistent directory found"
cascade seen in the full-file run. So the bug was in parsing, not in
anything upstream, and it took re-repairing the tool's own valid
output to expose it (the original source's specific field lengths
happened not to trigger it).

Root cause, confirmed by stepping through `_read_intact_at` by hand:
`parse_directory` (`marc_repair.py:410`) walks 12-byte chunks after the
leader, accepting each as a real directory entry if its length+start
digits are valid AND `start == cum` (the running total of prior
entries' declared lengths) -- the cumulative check exists specifically
to reject a directory-entry-shaped false positive in the field data
right after the real terminator (per the function's own docstring).
For this record, the content of field 001 (`on1341033027`) read
starting *at* the real directory terminator happened to produce a
bogus 40th entry whose `start` digits (`03302`) exactly equalled the
true cumulative length (`3302`) of the 39 real entries -- the one
numeric coincidence the existing guard couldn't catch. That phantom
entry got accepted, `pos` advanced 12 bytes into what was actually
field 001's real content, and every field boundary from there on was
wrong -- which Mode 1 (`_read_intact_at`) does correctly detect and
bail out of (returns `None` rather than corrupting data), but Mode 2
(`_repair_stripped_at`) failed too for the same underlying reason, so
the whole record fell through to the "no consistent directory found"
resync path, chopping the next ~8,400 bytes into 9 garbage chunks
before the next genuine leader pattern-match.

Fix: a real directory entry's 12 raw characters are always plain ASCII
tag+digits -- FIELDTERM (0x1E) and SUBFIELD (0x1F) are reserved
delimiter bytes that can never legitimately appear inside one (they
only ever occur in field *data*, never in the directory). Added `if
FIELDTERM in chunk or SUBFIELD in chunk: break` right after the
length/digit-shape check in `parse_directory`'s entry loop
(`marc_repair.py:410`) -- this rejects the phantom entry immediately
(its own first byte *is* the real terminator), closing the coincidence
hole without weakening the loose, intentionally tag-non-validating
parse for any genuinely corrupted-but-real directory.

Added `TestParseDirectoryTerminatorCoincidence` to
`tests/test_marc_repair_bib.py`, constructing a minimal 2-field record
(001 + 245) whose 001 content is deliberately built so its digits,
read starting at the real directory terminator, produce exactly this
coincidence (`start == cum`) -- asserts the coincidence actually lands
as designed, then asserts `_read_intact_at` still parses the record as
the correct 2 fields instead of desyncing. 358 tests pass (1 skipped);
flake8 clean (same pre-existing long line, now at line 6040 after the
insertions above).

Re-ran the full pipeline against `WTS_bibs_2026-10-01_repaired.mrc`
once more with this fix in place: **263,595/263,595 records written,
zero UNFIXABLE** -- confirms this really was the root cause for all 9,
not just the one traced by hand. `removed_880_missing_a` still fires on
the same **91 records** as the run before this fix (see
`working/WTS_bibs_2026-10-01_repaired3_log_20261005T193353Z.log`) --
expected, since these 9 records are unrelated to the 880 fix's own
logic.

## DONE: root-folder cleanup -- `working/` directory for non-essential files

The project root had accumulated a pile of large, generated, non-source
files (real MARC run inputs/outputs, logs, a JSON data export, a
one-off TSV) sitting alongside the actual source. None of them were
git-tracked (already covered by `.gitignore`'s `*.mrc`/`*.mrk`/`*.log`/
`/*.json` patterns, except the TSV), but they cluttered `ls` in the
project root.

Created `working/` and moved every non-essential root file into it:
`066_missing_subfield_a.tsv`, all `WTS_*` run inputs/outputs/logs,
`folio_instances_transform_bibs.json`, `sampled_problems.mrc`. Added
`working/` to `.gitignore`. Left `venv/`, `venv2/`, `build/`,
`*.egg-info/`, `__pycache__/`, `.pytest_cache/` alone -- those are
already gitignored and tied to absolute paths from `pip install -e .`/
venv activation scripts, so moving them risked breaking the dev install
rather than just tidying. The repair-run input/output files
(`WTS_source_FOLIO_full.mrc` and its repaired re-run,
`WTS_bibs_2026-10-01_repaired.mrc` and its repaired re-run for the
crash fix above) now live under `working/` too.

## DONE: shortened `removed_880_missing_a` detail line to `$6`/`$a` + 20 chars of context

`strip_missing_required_a` (`marc_repair.py:3768`) logged the *entire*
removed field body for every missing-required-`$a` tag, 010 and 880
alike. For 880 (Alternate Graphic Representation) specifically, that
body is the vernacular-script value itself -- often long -- even
though the only subfields that matter for diagnosing the defect are
`$6` (the linking data) and `$a` (the thing that's missing/punctuation-
only). Changed the 880 case only (010 is untouched, still logs the
full field): the detail now shows `$6`/`$a` in full, then at most 20
characters of whatever else the field held, in a trailing `[...]`
(with a `...` suffix if truncated). A field with nothing but `$6`/`$a`
gets no `[...]` suffix at all. Example from a real production record
(`WTS_bibs_2026-10-01.out`, see full-corpus re-run below):

    removed =880  00$6505-00/(S [$tDer Römerbrief und...]	(missing required $a; content discarded)

Commit `ada7f95`.

## DONE: reordered and shortened `suspect_hex_encoded_marc8` detail line

The old detail line buried the actually-useful part (the decoded
preview) after a long explanatory sentence, and always appended
"context" -- the original raw-field text around the `{xxxxxx}` run --
even when the run had already been successfully decoded back to real
readable text, in which case that context adds nothing. Also, that
context window was built as `match.start() - 10` to `match.end() + 10`,
so it included the *entire* matched run (which can be dozens of
`{xxxxxx}` groups, i.e. very long) rather than being bounded the way
"10 characters of context" implies.

Changed `find_suspect_hex_encoded_marc8` (`marc_repair.py:2523`) so the
detail line now reads: recovered content first, then a short data-loss
verdict ("recovered -- verify against source (byte alignment not
guaranteed)" vs. "POSSIBLE DATA LOSS -- doesn't decode to valid
MARC-8/text..."), then the hex-brace-run summary (group count, layers
deep). Original context is only appended when the decode is NOT
recoverable, and even then is now exactly 10 characters immediately
before and 10 after the run -- never the run itself. Real examples from
`WTS_bibs_2026-10-01.out`:

    tag 880 $a: recovers '· 마가복음'; recovered -- verify against source (byte alignment not guaranteed); 23 "{xxxxxx}" hex-brace group(s), 2 layer(s) deep

Updated two existing tests in `TestFindSuspectHexEncodedMarc8`
(`tests/test_marc_repair_bib.py:596`) whose assertions had been passing
only incidentally -- one checked for a literal escape-sequence
substring that, with pymarc's full MARC-8 transcoding, actually now
decodes to readable Korean text ("마가복음") instead; the other checked
for the old "NO DATA LOSS" wording that this change renamed to
"recovered". 358 tests pass (1 skipped); flake8 clean. Commit `d987f5a`.

## DONE: full re-run of `WTS_bibs_2026-10-01.out` against both fixes above

Re-ran the full pipeline against the original 421MB/263,595-record
`working/WTS_bibs_2026-10-01.out` (not the already-repaired copy) to
confirm both log-formatting changes above hold up on real production
data at full scale, not just the synthetic test cases. Result:
**263,595/263,595 records written, 0 unfixable**, 94.34s. 91 records
hit `removed_880_missing_a`, 12 hit `suspect_hex_encoded_marc8` -- same
counts as prior runs, confirming these are purely log-formatting
changes with no effect on what gets fixed or flagged. Output and log
left at `working/WTS_bibs_2026-10-01_repaired4.mrc` /
`working/WTS_bibs_2026-10-01_repaired4_log_20261005T200749Z.log`.

(Separately, pulled `--log-full` detail lines for both categories into
scratch files under `/tmp` to spot-check the new wording against real
records -- those scratch files were deleted after use, not left behind.)

## Next session: no open bugs from this session's changes

Both log-formatting changes above are implemented, tested, and
verified end-to-end against the full real corpus. Nothing new to pick
up from this session specifically -- the remaining open items are the
pre-existing ones higher up: the `install.sh` self-update UX question,
`build_marc8_corpus_index`'s whole-file in-memory read on very large
latin-1 files, and whether that same corpus-index prescan needs its
own progress indicator.

## Context usage at handoff

- Model: claude-sonnet-5
- Tokens: 133.7k / 1m (13%)
- System prompt: 9.8k (1.0%)
- System tools: 20.1k (2.0%)
- MCP tools: 7.2k (0.7%)
- Memory files: 0.3k (0.0%)
- Skills: 4.0k (0.4%)
- Messages: 92.5k (9.2%)
- Autocompacts at: 97%
