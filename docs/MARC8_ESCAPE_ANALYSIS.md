# `suspect_marc8_escape` pattern analysis (WTS_bibs_2026-10-01.out)

## Task

`find_suspect_marc8_escapes` (marc_repair.py) flags a MARC-8 script-switching
escape (Hebrew/Arabic/Cyrillic/Greek/CJK) that decodes to exactly one
character welded directly between two ASCII letters with no word boundary --
the heuristic's working theory is "miskeyed accented letter", e.g. "Schr" +
<CJK escape> + "inger" for "Schrödinger". It is detect-only ("NEEDS REVIEW"),
never auto-fixed: "there's no safe way to guess the intended character."

Running against `WTS_bibs_2026-10-01.out` (421MB, 263,595 bib records)
produced **255 findings across 150 records**. This doc analyzes whether
those findings share a reliable, exploitable pattern -- specifically,
whether a given escape's raw payload bytes reliably predict the same
intended Latin letter, which would let the tool suggest (not auto-apply) a
specific fix instead of a generic "could be ö, é, ñ, ü" hint.

**Conclusion up front: the pattern is real and explainable, but it does not
support a static payload -> letter lookup table.** See Recommendation below.

## Reproducing the finding set

```bash
source venv/bin/activate  # needs pymarc
python3 marc_repair.py WTS_bibs_2026-10-01.out \
    -o /tmp/out.mrc --log /tmp/full.log --log-full suspect_marc8_escape
```

(`pymarc` must be importable -- marc_repair otherwise refuses to run with
transcoding on by default; `--count` exits before processing anything and
produces no log, don't combine it with `--log-full`.) The
`=== NEEDS REVIEW: suspect_marc8_escape ===` section of the resulting log
contains all 255 lines used for this analysis.

## What the findings actually are

Every one of the 255 findings sits inside a recognizable word -- almost all
German (this file is clearly a seminary/theology-library batch: "Beiträge",
"Hebräisch", "Tübingen", "Göttingen", "Erlösung", "Jürgen" ... ), a few
French/Latin/Greek liturgical terms, and a handful of Korean romanizations.
The escape sits exactly where that word's diacritic should be.

### The escape eats more than one character

Looking at the raw bytes (not the log's text rendering) behind
record 103791 / `.b11040087`, tag 260:

```
...T \x1b$1'Z>\x1b(B ingen :...
```

There is no `b` anywhere in that byte run. The intended word is
**Tübingen** -- so the escape replaced *two* original characters (`ü` and
`b`), not one. This file's correctly-encoded umlauts use the standard
MARC-8/ANSEL form: a combining-diaeresis byte (`0xE8`) immediately *before*
its base letter (`T` + `0xE8` + `u` + `bingen`). "Tübingen" spelled
correctly this way appears **2,546 times** elsewhere in this same file,
which is strong corroboration for both the reading and the mechanism.

The CJK/EACC charset that `_MARC8_SCRIPT_CHARSETS` maps to `"1"` always
consumes exactly 3 raw bytes per character (EACC's fixed width). 3 bytes is
exactly diacritic-byte + base-letter + one more plain ASCII letter --
suggesting whatever corrupted these records didn't substitute "wrong
character for right character" 1-for-1; it mis-parsed an ANSEL
diacritic+letter pair plus whatever letter happened to follow as if they
were the start of a 3-byte EACC character, and replaced all three bytes
with an arbitrary CJK escape payload. That also means `marc_repair`'s
"NO DATA LOSS" framing is accurate only narrowly (decoding the escape itself
loses nothing further) -- the real letter was already unrecoverably
destroyed *before* this file was produced.

Three findings used single-byte script switches (`(2`/`(3`, Hebrew/Arabic)
instead of the CJK `$1` triple-byte switch; those consume only 1 raw byte
and don't fit the same "+1 extra letter" arithmetic. They look like a
different/noisier phenomenon (one, `Grèo[?]eres`, doesn't resolve to any
obvious German/French word) and weren't part of the pattern validated below.

### Cross-checking against the rest of the corpus

A fast index (regex pass, ~5s) was built over every `<diacritic
byte><base letter>` run in the 421MB file, capturing the surrounding ASCII
word fragments. Each of the 255 findings' `word_before`/`word_after`
fragments was looked up against that index to see whether the *same word*,
correctly encoded, exists elsewhere in the batch.

65 distinct 3-byte escape payloads appear across the 255 findings. Results:

- Many payloads **do** recur identically for the same source word across
  multiple records -- e.g. payload `'Z>` only ever appears in "...ingen"
  contexts that resolve to **Tübingen**; `-/"` only ever appears in
  "Beiträg-" contexts (**ä**); `'ZB` only ever appears in
  "...b_her/r_k..." contexts that resolve to **ü**-words (Bücher,
  Rückschau, Osnabrück, ...).
- But **many different payloads resolve to the same letter**: `'Z>`,
  `#7L`, `'ZB`, `#*.`, `#7G`, and `!ZI` all land on **ü** in different
  words; `-W[`, `-/"`, `#-i`, `#-u`, `!WO` all land on **ä**; `'Xu`,
  `'Xi`, `'Xo`, `)3E` all land on **ö**.

That rules out a simple per-letter keyboard/compose-key substitution bug
(which would produce one consistent byte sequence per letter, not six).
Payloads recurring 1:1 with one specific *word* (not just one letter) is
more consistent with the same already-corrupted word being copied or
reused across records -- copy-cataloging, template reuse, or a vendor
batch-load/migration bug that ran once over a shared source -- than with a
general per-character encoding fault.

## Confidence

- **High** that the root cause is a real, structural phenomenon (mis-parsed
  ANSEL diacritic sequences getting wrapped in a bogus multi-byte escape by
  some upstream process), not noise or coincidence -- corroborated by
  thousands of correctly-encoded instances of the same words elsewhere in
  this file.
- **Low** that a static `payload -> letter` table built from this file
  would generalize to other files/batches. The relationship is mediated by
  recognizing *words*, not by decoding bytes -- there is no arithmetic
  relationship between a payload's raw byte values and the letter it
  implies (checked for the `ü` group; no shared offset or encoding-table
  structure found).
- The "exactly one character replaced" assumption in
  `find_suspect_marc8_escapes`'s current suggestion text is **not quite
  right** for the dominant (CJK/`$1`) case: it's usually a diacritic +
  base letter + one more plain letter (2-3 original characters), not one.
  A one-character substitution into `word_before`/`word_after` would often
  still leave the result misspelled (e.g. "Tübngen" instead of
  "Tübingen") unless the suggestion accounts for the extra swallowed
  letter.

## Recommendation

Don't build a static payload-to-letter lookup table -- the evidence here
specifically argues against one generalizing. Also don't change
`find_suspect_marc8_escapes`'s auto-fix behavior (it should stay detect-only
regardless).

Instead, if a stronger suggestion is wanted, the validated approach is a
**corpus/frequency lookup**, not a byte-decode table: for each finding,
search the rest of the batch (or an authority file) for a real word
matching `word_before...word_after` with a diacritic+letter run in between;
if a dominant, unambiguous match is found -- ideally recurring many times,
the way "Tübingen" does 2,546 times in this file -- surface that specific
word as the suggestion instead of the current generic "ö, é, ñ, ü" hint.
This still requires cataloger review and is never auto-applied; it just
makes the hint far more specific when the batch has enough internal
vocabulary reuse to support it (true here; institutional cataloging data
tends to repeat place names, publisher cities, and subject vocabulary
heavily). It would need deliberately narrow matching (a minimum fragment
length on both sides, requiring an unambiguous single resolved word) to
avoid the false corroboration that a naive short-substring search produces.

This has not been implemented -- this doc is the analysis the user asked
for before deciding whether to build the corpus-lookup suggestion feature.
