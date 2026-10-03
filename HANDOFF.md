# Handoff Notes

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
corpus lookup..." progress indicator, or whether `maybe_print_estimate`
needs to account for that upfront cost at all.

## Context usage at handoff (from `/context`)

- Model: claude-sonnet-5
- Tokens: 135.3k / 1m (14%)
- System prompt: 9.9k (1.0%)
- System tools: 20.1k (2.0%)
- MCP tools: 7.2k (0.7%)
- MCP tools (deferred): 43.6k (4.4%)
- System tools (deferred): 16.4k (1.6%)
- Memory files: 214 (0.0%)
- Skills: 4k (0.4%)
- Messages: 94k (9.4%)
- Free space: 831.7k (83.2%)
- Autocompact buffer: 33k (3.3%)
