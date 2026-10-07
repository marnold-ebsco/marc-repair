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

**Next up: `0xB2` and `0xB3`.** Not started. These are two of the four
bytes already sitting in the "Investigated, not implemented" table in
`docs/MARC8_DIACRITIC_HANDLING.md` (`0xA6`, `0xBA`, `0xB2`, `0xB3`, `0xC1`)
-- unlike `0xA8`/`0xA5`/`0xAE`, these never got a *first* confirmed
before-letter at all, so this isn't a re-check of existing leftovers, it's
the original disambiguation pass (HOWTO steps 1-4 below) run for the first
time. From the existing table: `0xB2` has 745 occurrences (398 records)
and was flagged "murky" -- frequently tangled with multiple macron escapes
within the same word, plus at least one occurrence ("JohannesVerl...")
that looks like it needs no diacritic fix at all (possible false-positive
match on the detection shape itself, worth checking early). `0xB3` has 49
occurrences (21 records) and was flagged as two unrelated phenomena
sharing one byte: Dead Sea Scroll sigla superscripts (e.g. "1QIsaᵃ" --
would need a letter-to-Unicode-superscript substitution, a different fix
mechanism entirely, not a mark insertion) and what looks like a Korean
name needing a breve ("Yŏn Presbyterian"). Both need the TSV-report +
sample-file treatment (HOWTO steps 1-3) before any disambiguation
judgment calls -- the existing counts/notes above predate the
close-escape fix and may be stale, same as `0xA4`'s original 93 (actual
79) and `0xA8`'s original 324 (actual 260).

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
