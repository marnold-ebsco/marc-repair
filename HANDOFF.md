# Handoff Notes

## TODO (deferred by user): `suspect_hex_encoded_marc8` is still detect-only -- the `{xxxxxx}` text itself is never fixed in output

Follow-up to the per-field transcode isolation fix below ("Important limits
of this fix" item 1). Confirmed by reading `find_suspect_hex_encoded_marc8`
(`marc_repair.py:2523`): it's genuinely detect-only -- it logs a decoded
preview (and an explicit "recovered" vs. "POSSIBLE DATA LOSS" call per
finding) but never mutates the record. So every `{xxxxxx}` hex-brace run
stays in the repaired output verbatim, whether or not the decode happens to
recover clean text. This is a deliberate prior decision, not an oversight --
see the comment above `_HEX_BRACE_GROUP_RE` (`marc_repair.py:2430-2434`):
decoding is boundary-sensitive (a stray byte or incomplete trailing hex
digit commonly survives at a chunk's edge in real examples), so an automatic
rewrite risks silently replacing one corruption with a different,
equally-wrong one.

**Options discussed with user, none chosen yet:**
1. Auto-replace the `{xxxxxx}` run with its decoded text only when
   `_hex_brace_decode_looks_recoverable` says `recoverable` -- leave the
   "POSSIBLE DATA LOSS" (not recoverable) findings untouched/still just
   flagged. Re-home the category from detect-only to FIXED/REQUIRES
   ATTENTION.
2. Same as 1, plus strip the field/subfield entirely when not recoverable,
   mirroring the `removed_untranscodable_subfield` precedent (unusable bytes
   discarded outright rather than left in place).
3. Leave fully detect-only (status quo) -- no code change, just confirm the
   existing log is good enough for a human reviewer.

**Rough cost estimates given to user:**
- Scope quantification (full corpus re-run with `--log-full
  suspect_hex_encoded_marc8`, split findings into recovered vs.
  not-recovered, review the 3 known non-throwing records --
  `.b11077347`, `.b11188236`, `.b11257982`): ~5-10k tokens, no code change.
- Option 3: ~0 beyond the scope step.
- Option 1: ~30-50k tokens (mutator rewrite of the function, call-site wire-up,
  category re-homed, new tests against the real corrupted bytes, docs
  regen, full suite + flake8, full-corpus re-run to diff counts) --
  comparable in shape to the Stage 1-3 per-field transcode isolation fix
  below.
- Option 2: ~45-70k tokens (everything in option 1 plus the strip path and
  its own tests/doc row).
- Biggest variable either way: the boundary-sensitivity risk the existing
  comment already flags means a first implementation could produce a
  subtly wrong splice on a real example, costing at least one extra
  correction round (same shape as the 5-vs-3 prediction miss during the
  Stage 4 re-run below).

**Status: user is putting this off for now, may come back to it later.**
Next session picking this up should start with the cheap scope-quantification
step regardless of which option (if any) ends up chosen.

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

## TODO (later): re-examine `install.sh`'s self-update/re-run story

A user ran `./install.sh --dir .` from inside an EC2 checkout at
`/working/migration/scripts/marc_repair` and got `bash: ./install.sh: No
such file or directory`. Likely cause: that directory was created by
`install.sh`'s minimal-fetch mode, and `install.sh` deliberately never
copies itself into the install dir -- `FILES` (`install.sh:37-45`)
doesn't include `install.sh`, per the comment at `install.sh:116-120`
("it can't safely rewrite itself mid-loop the way those files get
rewritten"). So a dir populated by `curl ... | bash -s -- --dir .` has
no local `install.sh` to re-run, and the only documented recovery is to
re-fetch it by hand or always invoke via the `curl | bash` form.

Worth a look: should the installed dir get its own copy of `install.sh`
(added to `FILES`, with the self-rewrite logic guarded some other way),
or should the "Apply an update in place" hint printed at the end of a
successful install (`install.sh:261-268`) say more explicitly that the
`curl | bash` form is the one to keep using if you don't already have a
local `install.sh`? Confirm first whether the EC2 directory in question
was actually a minimal install vs. a full git clone missing the file for
some other reason -- wasn't confirmed either way in that conversation.

## TODO (later): `build_marc8_corpus_index` reads the whole input file into memory at once

Unlike the rest of the pipeline (which streams one record at a time and
stays O(1) in memory regardless of file size), `build_marc8_corpus_index`
(`marc_repair.py:2333-2334`) does `text = fh.read().decode("latin-1")` --
the entire file as one string, held for the duration of the scan. Only
triggers when `encoding_used == "latin-1"` (true legacy MARC-8 files).

Cost is roughly 1x file size (latin-1 decode is 1 byte/char in CPython's
internal representation, so no multiplier) plus the small, already-bounded
`index_exact`/`index_minus_one` sets. Confirmed fine at 421MB (the
WTS_bibs EC2 file). Not a correctness bug -- just no ceiling the way the
rest of the pipeline has. A sufficiently large latin-1 file (several GB)
could push memory up by that file's full size on a constrained box.
`--no-marc8-corpus-lookup` is already an escape hatch.

**If picked up:** switch to a chunked read with a small overlap buffer
carried across chunk boundaries (so a diacritic+letter match straddling a
chunk edge isn't missed, and the `_MAX_WORD_FRAGMENT_WINDOW=100`-char
before/after slices still resolve correctly near a boundary). Estimated
~40-80k tokens of agent work including new boundary-case tests -- the
overlap-buffer correctness is the fiddly part and likely needs 1-2
correction rounds after the first test run.

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
