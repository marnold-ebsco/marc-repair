# Handoff Notes

For closed-out work from prior sessions (the full `fix_marc8_diacritic_escapes`
project, the 010/880 missing-`$a` categories, the `parse_directory` phantom-entry
fix, log-formatting history, etc.), see
[docs/HANDOFF_HISTORY.md](docs/HANDOFF_HISTORY.md). This file only carries the
current state and open items.

## Current state

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
counts and this breakdown. `0xAE` and `0xA5` still haven't had this
re-check done -- see the HOWTO below.

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

2. **`suspect_hex_encoded_marc8` stays detect-only.** The `{xxxxxx}` hex-brace
   corruption is never rewritten in output today, even when the decode is
   confirmed recoverable -- deliberate, since decoding is boundary-sensitive.
   Three options were scoped with the user (auto-replace when recoverable;
   same plus strip when not recoverable; leave as-is) with rough token-cost
   estimates for each, but none chosen yet. User is putting this off. See
   [docs/HANDOFF_HISTORY.md](docs/HANDOFF_HISTORY.md) for the full option
   writeup and cost estimates.

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

## HOWTO: re-run the 0xA4 disambiguation methodology for 0xAE, 0xA5

Worked end-to-end for `0xA4` this session (confirmed `z` -> dot below as a new
override; see `_MARC8_BARE_COMBINING_BEFORE_OVERRIDES` in `marc_repair.py` and
`docs/MARC8_DIACRITIC_HANDLING.md`), and for `0xA8` in a later session (no new
override -- the 12 leftover occurrences split across four small, unrelated
groups, none clean/large enough; see the "Current state" entry above and
`docs/MARC8_DIACRITIC_HANDLING.md`'s table). `0xAE` and `0xA5` are each still
restricted to one confirmed before-letter (`h` and `r` respectively -- see
`_MARC8_BARE_COMBINING_RESTRICTED_BEFORE`) with a chunk of real occurrences
left unconfirmed outside that restriction (26/135, 67/318 as of the last
count -- re-verify, since `0xA8`'s 76/324 figure turned out stale by the time
it was actually re-checked; see `docs/MARC8_DIACRITIC_HANDLING.md`'s table).
This is the same kind of "is there a second confirmed mark hiding in the
leftovers" question `0xA4` answered for `z` and `0xA8` answered (in the
negative) for its leftovers -- not yet done for these two. **Not started;
not scoped as "do this automatically" -- read the whole thing before running
anything, since step 4 below is where the real judgment calls are.**

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
