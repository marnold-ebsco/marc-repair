# Handoff Notes

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

## NEXT TASK: run against WTS_bibs_2026-10-01.out on EC2 -- "no output"

Not started. User wants to run marc_repair against `WTS_bibs_2026-10-01.out`
(421MB / 263,595 bib records -- same file used for the
`suspect_marc8_escape` analysis above and the MARC-8 truncation
investigation; two known real problem records in it: `.b11165406` and
`.b11227394`, both genuine multi-byte-truncated 880 fields, see
`transcode_marc8_failed` in a full `--log` run) on the EC2 box, but is
getting no output there.

Locally (WSL, this repo, venv activated) this file runs fine, e.g.:

    python3 marc_repair.py WTS_bibs_2026-10-01.out -o /tmp/wts_repaired.mrc --log /tmp/wts_report.log

-- takes ~100-110s, prints progress lines to stderr (record counts/rate/ETA)
and a final summary line to stdout, produces `/tmp/wts_report.log` (or
similar) with `transcode_marc8_failed: 2 record(s)` among the findings.

EC2 install is managed by `install.sh` (see the EC2 deploy note elsewhere
in past handoffs/git history -- `/working/migration/scripts/marc_repair`,
no `.git`, `venv/bin/marc_repair` on PATH directly). "No output" needs
triage: is the process actually running (check for a hung/killed process,
disk space for the repaired output + log on a 421MB input), is it an old
pre-fix install (re-run `install.sh --dir .` -- it self-updates on first
run and asks for a second run to apply), is output being redirected/lost
rather than genuinely absent, or is this a different failure mode
entirely (crash with no traceback, permissions, etc.)?

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
