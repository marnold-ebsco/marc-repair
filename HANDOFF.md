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

2. **`suspect_hex_encoded_marc8` stays detect-only.** Its "recoverable"
   heuristic was tightened and shipped (`d4caade`), but auto-replacing even
   the "recoverable" cases turned out to be unsafe, confirmed against every
   real occurrence in the corpus -- see the full account below. Decision:
   stay detect-only; a real fix would need external verification (a
   different, bigger feature), not a decoder tweak. Not started.

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
