# MARC-8 lost-diacritic handling -- what's fixed, what's flagged, what's open

This is a reference table for `fix_marc8_diacritic_escapes` (category
`fixed_marc8_diacritic`, INFORMATIONAL, on by default -- see
`docs/REPAIR_CATEGORIES.md`) and the related detect-only
`find_suspect_marc8_escapes` (category `suspect_marc8_escape`). Both
address the same underlying corruption symptom -- a stray MARC-8 escape
byte (`\x1b...`) sitting mid-word, where a diacritic mark has been lost --
but `fix_marc8_diacritic_escapes` only auto-repairs the specific sub-cases
below that were confirmed by volume against the full `working/GTU_bibs.mrc`
corpus (404,957 records). Everything else still reaches
`suspect_marc8_escape` for a human to review.

All of this is for *this file's* corruption pattern specifically (a bogus
script-switch escape, or a bare un-escaped byte, substituting for a lost
diacritic within an otherwise-intact MARC-8 record). It is not a general
MARC-8/Unicode diacritic reference.

## Fixed automatically -- escape-wrapped payload bytes

These are single raw bytes following a recognized script-switch escape
(`\x1b(X` for charset `X`, closed by `\x1b(B`) that, in the real corruption,
swallowed only the diacritic mark itself -- the base letter survives
immediately before the escape. See `_MARC8_DIACRITIC_PAYLOADS` in
`marc_repair.py`.

| charset | payload byte | mark | example |
|---|---|---|---|
| Q | A | grave accent | "Haur`<escape>`eau" -> "Hauréau" |
| Q | B | acute accent | "kt`<escape>`re" -> "które" |
| Q | C | circumflex | — |
| Q | D | tilde | "Joa`<escape>`o" -> "João" |
| Q | E | macron | "amr`<escape>`ta" -> "amrāta"-style long vowels |
| Q | G | breve | "Krachkovski`<escape>`i" -> "Krachkovskiĭ" (except see below) |
| Q | K | ring above | — |
| Q | M | caron/háček | — |
| 3 | L | cedilla | — |

**Exception:** payload `(Q, G)` normally means breve (confirmed against
2,920 occurrences: Russian "-iĭ" endings, Korean vowels), but within 15
characters of the literal Arabic article `"al-"` it means macron instead (8
of 2,920 occurrences, all genuinely Arabic) -- that narrow case is left
unfixed for human review rather than guessed. See
`_MARC8_AMBIGUOUS_NEAR_AL_PAYLOADS`.

Also handled in the same pass: ordinary ASCII punctuation/whitespace
trapped *inside* the escape when it and the closing `\x1b(B` got swapped
(e.g. `"Facolta" + grave + " " (trapped) + close + "di lettere"` ->
`"Facoltà di lettere"`, ~81,000 occurrences) -- not a different mark, just
a different place the corruption can put the escape boundary.

Not handled, stays detect-only: the 3-byte CJK/EACC script switch
(`\x1b$1...`). Unlike the single-byte switches above, this one genuinely
destroys the base letter along with the diacritic (see
`docs/MARC8_ESCAPE_ANALYSIS.md`), so there's nothing to safely reapply a
mark to.

## Fixed automatically -- bare, un-escaped bytes

These are raw bytes with no recognized script-switch escape at all --
just the same stray junk (`\x1bp+\x1bs`, `\x1bp(\x1bs`, etc.) directly in front
of a bare byte that's itself a valid (but unrelated) ANSEL byte on its
own. The stray-ESC-byte signal immediately before it is what makes it safe
to touch -- without that signal, the same byte could be genuine unrelated
content and must not be touched. See `_MARC8_BARE_COMBINING_BYTES`/
`_MARC8_BARE_STANDALONE_BYTES`.

| byte | mark | kind | restriction | confirmed example |
|---|---|---|---|---|
| `0xA3` | dot below | combining | none | "al-h`<byte>`ujjah" -> "al-ḥujjah" |
| `0xAE` | dot below (2nd byte) | combining | only after `h` | "H`<byte>`ammurabi" -> "Ḥammurabi" |
| `0xA5` | dot below (3rd byte) | combining | only after `r` | "R`<byte>`gveda" -> "Ṛgveda" |
| `0xA7` | cedilla | combining | none | "franc`<byte>`ais" -> "français" |
| `0xA8` | ogonek | combining | only after `a`, `e`, or `u` | "Ksia`<byte>`zka" -> "Książka" |
| `0xA4` | diaeresis (default); dot below after `z` (override) | combining | only after `u`, or `z` with its own mark | "U`<byte>`bersetzung" -> "Übersetzung"; "Raz`<byte>`a" -> "Raẕa" |
| `0xA6` | cedilla (2nd byte) | combining | only after `c` | "franc`<byte>`ois" -> "François" |
| `0xBC` | hamza (modifier apostrophe) | standalone | none | "Ihya`<byte>`" -> "Ihyāʼ" |
| `0xBB` | ayn (modifier turned comma) | standalone | none | "`<byte>`ulum" -> "ʻulum" |
| `0xB9` | prime (Russian soft sign) | standalone | none | "Il`<byte>`ich" -> "Ilʹich" |

"Combining" marks are reapplied to the letter immediately before the junk
run (ANSEL mark-before-base-letter order); "standalone" marks are inserted
as their own character wherever the junk run sat.

**Why the restricted rows are restricted:** `0xAE`, `0xA5`, `0xA8`, and
`0xA4` are each *overloaded* -- the same corrupted byte stands in for a
different mark depending on the word/language it corrupted, so they're
only auto-fixed in the specific before-letter context the full corpus
confirmed is clean. Outside that context they're left alone for
`suspect_marc8_escape` rather than risk the wrong mark:

- `0xAE`: 109 of 135 occurrences (81% -- recounted; unlike `0xA4`/`0xA5`/
  `0xA8` this figure was **not** stale, same 135 both before and after the
  close-escape fix) are `h` + dot-below (Near Eastern/biblical names,
  "Ḥammurabi", "Yariḥ"). Re-investigated the remaining 26 the same way as
  `0xA4`/`0xA5`/`0xA8`'s leftovers, looking for a second confirmed
  override -- none of the five leftover groups qualify:
  - Italian "pi`<byte>`" / "Ges`<byte>`" (più/Gesù, 4 occurrences) are the
    destroyed-base-letter failure mode already confirmed for `0xA4` -- the
    whole "ù" is missing, not just its mark, and the mark these words
    actually need is a grave accent, not dot below, anyway.
  - An Akkadian/Hittite/Sumerian cluster (~11 occurrences: "Hattusa",
    "Harranu"/"-shubu", "Ninhursag", "Asalluhi", "Kalhu", "Hirbet",
    "Hissar") where the dot-below mark is real but belongs on the "h" one
    letter *before* the matched before-letter (e.g. "Ninhu`<byte>`rsag" is
    "Ninḥursag" -- the mark is on "h", not "u"). This before-letter
    mechanism only ever attaches to the letter immediately before the
    junk, so it can't reach one letter further back -- the same shape as
    `0xA5`'s Sanskrit `t`/`a` leftovers (mark lands the wrong distance from
    where it belongs), just the opposite direction.
  - A Germanic/Scandinavian cluster ("understödd", "Größeres", "Knauß")
    needing umlaut or o-with-stroke, not dot below -- a different mark
    entirely, same overload shape as `0xA4`'s u-vs-z split but without
    enough volume or a clean grouping to add as a second override.
  - A genuinely ambiguous al-Ghazali/"Ihya" cluster (5 occurrences) that
    doesn't read as a clean dot-below case at all: the common
    transliteration "al-Ghazali" carries no diacritic here, and "Ihya"
    needs the unrelated hamza byte (`_MARC8_BARE_STANDALONE_BYTES`), not a
    combining mark.
  - One "où il" (French, needs grave, not dot below) and one singleton too
    garbled to confirm.

  No override added -- `0xAE` stays restricted to `h`.
- `0xA5`: 251 of 291 occurrences (86% -- recounted after the close-escape
  fix removed 27 stale false-positive matches from the original 318, same
  stale-count effect as `0xA4`/`0xA8`) are `r` + dot-below (Sanskrit IAST
  vocalic r -- "Ṛgveda", "Kṛṣṇa", both textbook-unambiguous). `h` (3
  occurrences) looked like a second clean case *in isolation* -- the SAME
  dot-below mark `0xA3`/`0xAE` already produce, confirmed-by-volume-of-2
  ("Muh`<byte>`ammad" -> "Muḥammad") -- but was **not added**: the third
  "h" occurrence ("Yah`<byte>` at Elephantine", real record `.b18157713`
  in `working/GTU_bibs_0xA5_sample.mrc`) sits in the same subfield as an
  unrelated, pre-existing escape-designator corruption earlier in the
  string that leaves pymarc's `marc8_to_unicode` decoder stuck in a bad
  G1 charset state for the rest of the field. Confirmed by diffing
  transcoded output with the fix enabled vs. disabled on that exact
  subfield: leaving the bare `0xA5` byte untouched happens to let the
  decoder resync and recover the ~100 characters of legible text after it
  ("Bob Becking -- The Judeans/Arameans..."); replacing it with the
  correct dot-below + "h" keeps the decoder in the broken state and that
  whole tail renders as placeholder glyphs instead -- the semantically
  *correct* fix makes this real record's output *more* garbled, not less.
  **Lesson: a before-letter reconstruction that reads correctly in
  isolation isn't sufficient evidence -- it must also be checked against
  the full surrounding field text in a real sample record, since
  unrelated corruption earlier in the same field can interact with the
  fix in ways a standalone test string won't reveal.** Left unfixed
  pending either a fix to the upstream escape-designator decoder bug or a
  narrower guard; `_MARC8_BARE_COMBINING_RESTRICTED_BEFORE["\xa5"]` stays
  `"r"`. The remaining 37 occurrences (before-letters `t`/10, `n`/10,
  `s`/7, `a`/6, `g`/1, `d`/1, `b`/1, `m`/1) also don't cluster into
  another confirmed mark: the `t`/`a` groups are Sanskrit words where the
  vocalic-r mark lands after an entire following consonant-vowel run
  rather than right after the actual "r" (e.g. "Smrt`<byte>`i" is
  "smṛti", not "smrṭi" -- the mark belongs 2 letters before the junk,
  which this before-letter mechanism structurally can't reach); the
  `n`/`s` groups are Greek words with a destroyed vowel before the junk,
  not a missing mark on the letter immediately before it (e.g.
  "Orthodoxn`<byte>`" is "Orthodoxōn", missing the whole "ō", same
  destroyed-base-letter failure mode as `0xA4`'s leftovers); and the last
  4 (`g`/`d`/`b`/`m`, one occurrence each) are singletons too ambiguous to
  confirm on their own.
- `0xA8`: 248 of 260 occurrences (95% -- recounted after the close-escape
  fix removed 64 stale false-positive matches from the original 324, same
  stale-count effect as `0xA4`'s 93 -> 79) are `a`/`e` + ogonek (Polish) or
  `u` + ogonek (Lithuanian, e.g. "Katalikų"). Re-investigated the remaining
  12 the same way `0xA4`'s leftovers were, looking for a second confirmed
  override like `z` turned out to be for `0xA4` -- none of the four
  leftover groups qualify: 4 `c` occurrences need cedilla (e.g.
  "Franc`<byte>`ais" -> "français"), but the unrestricted `0xA7` row
  already produces that, so adding it under `0xA8` too would be redundant,
  not new; 3 occurrences (`o`/`o`/`i`: "Muo`<byte>`z" -> "Muñoz",
  "Qui`<byte>`ones" -> "Quiñones") are the destroyed-base-letter failure
  mode already confirmed for `0xA4` (the missing "ñ" sits two letters
  before the junk, not attachable to the letter immediately before it);
  1 `o` occurrence ("nieuwe series o`<byte>`f" -> "...of Verhandelingen")
  needs no diacritic at all; and the last 4 (`s`/`S`/`r`/`d`/`h`, one pair
  of words) mix a singleton cedilla ("Karakas`<byte>`oglu" ->
  "Karakaşoğlu"), a singleton dot below ("S`<byte>`uhbat" -> "Ṣuḥbat"),
  and three occurrences in one title too garbled to confirm at all
  ("Ar`<byte>`hd`<byte>`zerh`<byte>`n bar`<byte>`haran"). No override
  added -- `0xA8` stays restricted to `a`/`e`/`u`.
- `0xA4`: 10 of 79 occurrences (13%) are `u` + diaeresis (German
  "Übersetzung", "Beschlüsse", "Seegrün"); a further 21 (27%) are `z` + dot
  below (Persian/Arabic transliteration, e.g. "Riza" -> "Riẕa", "Murtaza"
  -> "Murtaẕa", "qaziyya" -> "qaẕiyya") -- see
  `_MARC8_BARE_COMBINING_BEFORE_OVERRIDES` in `marc_repair.py`, which lets
  this one byte carry two different confirmed marks depending on the
  before-letter. The remaining ~48 occurrences were investigated and
  deliberately left unfixed, for two different reasons -- *not* "needs a
  third mark" the way `z` needed a second one:
  - Most (Spanish/Hungarian/Icelandic/Portuguese/Polish/Catalan: "Católica",
    "Gastón Espinosa", "Tóth", "López", "Jerónimos") turn out to have lost
    an entire base vowel (almost always "ó"), not just its mark -- the same
    failure mode already known for the 3-byte CJK/EACC escape (see above),
    just newly confirmed coming through this bare-byte path too. This
    mechanism has no safe way to reapply a mark to a vowel that no longer
    exists, so these stay unfixed rather than attaching a mark to the wrong
    (consonant) letter.
  - A handful reuse the *same* before-letter for both the dot-below case and
    the destroyed-vowel case depending on the specific word (`l`/`L`:
    Tamil "Tamilakam" -> "Tamiḻakam" needs dot-below, but "Theológicos" is a
    destroyed vowel; `t`: Arabic "Khutba" -> "Khuṭba" needs dot-below, but
    "Católica" is a destroyed vowel) -- these can't be generalized by
    before-letter alone the way the `z` override could, so they're left for
    human review too.

  (Recounted after the close-escape fix below: the original 93-occurrence
  figure included 14 false-positive matches where a close escape's own "B"
  byte was misread as the "before" letter -- `_MARC8_NOT_ESCAPE_DESIGNATOR_
  LOOKBEHIND` now excludes those, leaving 79 genuine occurrences. The 10
  confirmed `u` + diaeresis fixes are unaffected either way.)

  **A counting quirk worth knowing if re-verifying this against a full
  corpus run:** all 21 `z` occurrences happen to sit in a subfield that
  already contains a *different* diacritic fix (e.g. the `h` in
  "Muòtahharåi, Murtaza..." was already being fixed by the `0xA3` row
  above). `fixed_marc8_diacritic` logs (and the CLI's summary count) one
  `detail` string per *subfield*, not per mark -- so the `z` override
  firing correctly does not raise the corpus-wide instance count at all;
  it just enriches a detail line that was already going to be logged. A
  before/after instance-count diff is therefore the wrong way to confirm
  this kind of fix landed -- diff the actual field text instead (confirmed
  by diffing `working/GTU_bibs_0xA4_sample.mrc` with the override on vs.
  off: instance count identical, 21 real text differences, all correct).
- `0xA6`: cedilla, but only after `c` -- the SAME mark `0xA7` already
  produces, not a new one. Confirmed by a full-field diff (fix on vs.
  off, every record in `working/GTU_bibs_0xA6_sample.mrc`, 144 records):
  57 changed subfields for `c`, all correct French/Occitan/Portuguese/
  Turkish/Sanskrit-transliteration cedilla insertions ("français",
  "revelação", "Kaçar", "Çârîraka"), zero regressions. Unlike every
  other overloaded byte above, `c` is a *minority* before-letter for
  this byte -- the dominant ones are `s` and `t` (Romanian `ş`/`ţ`
  comma-below/cedilla, e.g. "Bucureşti", "Colecţia"), which were trialed
  the same way and **rejected**, despite looking just as clean at first
  glance: the same full-field diff found two different real regressions
  specific to `s`/`t` that `c` didn't have --
  - An off-by-one mark placement, found in two different records
    (`.b11564295`, `.b13281069`): the junk sometimes lands one letter
    later than the letter that actually needs the mark (correct target
    "Bisericeşti"/"Bucureşti" needs the mark on `s`, but the captured
    before-letter is `t`, one position later) -- same shape as `0xAE`'s
    Akkadian cluster, just via `t` instead of `h`. Confirmed this isn't
    a fixable regex ordering issue: `.b13281069`'s "Bucureşti" has the
    junk shifted by one letter compared to every *other* "Bucureşti" in
    the corpus, which all fix correctly -- the underlying corruption
    itself is inconsistent about where the junk lands for this cluster.
  - A decoder-state regression (`.b12911008`): applying the trial
    override there doesn't just misplace one mark, it leaves pymarc's
    `marc8_to_unicode` decoder stuck, turning a long stretch of the
    subfield after the first mark into unreadable combining-mark debris
    -- the same failure mode already documented for `0xA5`'s rejected
    `h` case above, just triggered by `s`/`t` instead.

  `s`/`t` stay unfixed, same as `0xA6`'s other non-`c` occurrences
  always have. Full-corpus re-run after adding the `c` override
  (`--log-full fixed_marc8_diacritic`): `fixed_marc8_diacritic` rose
  from 149,150/64,340 (the figure after the `0xA4` session) to
  149,166 instances / 64,348 records -- a modest +16/+8, consistent
  with the same counting-metric quirk documented for `0xA4`'s `z`
  above (most `c` occurrences share a subfield with an already-counted
  fix). `suspect_marc8_escape` stayed at 1,258 records, unchanged --
  expected, since almost every record with a `c` occurrence also has
  an unfixed `s`/`t` occurrence still flagging it.

## Investigated, not implemented -- genuinely ambiguous or insufficient evidence

These candidate bytes were found by the same methodology (stray junk +
bare byte) but did **not** get a safe, confirmed-by-volume single
interpretation. No code handles them; they still reach
`suspect_marc8_escape`'s generic hint.

| byte | occurrences | why it's unresolved |
|---|---|---|
| `0xBA` | 69 (37 records) | Means Hungarian double-acute (ő/ű -- "felelős", "György") in roughly half its occurrences and Russian hard sign (ʺ -- "obʺedinenii") in the rest, with no reliable split by surrounding letter. |
| `0xB2` | 832 (396 records) | Dominant clusters need an *inserted* vowel-with-mark (German ö, Dravidian macron-n̄, etc.) between the matched before-letter and the following text, not a mark combined onto the before-letter itself -- the before-letter mechanism structurally can't produce that shape. See writeup below. |
| `0xB3` | 48 (20 records) | Two unrelated phenomena sharing one byte: Dead Sea Scroll/philological sigla superscripts (e.g. "1QIsaᵃ" -- would need a letter-to-Unicode-superscript substitution, a different fix mechanism entirely, not a mark insertion), ~92% of occurrences, and a single ambiguous Korean name case needing a breve ("Yŏn Presbyterian"). See writeup below. |
| `0xC1` | 8 (3 records) | Tiny sample with an escape structure (`\x1bb8`/`\x1bb9` fragments) not seen anywhere else in the corpus -- may not even be the same corruption mechanism as the rest of this table. |

**`0xA6`'s residual `a` (ogonek) minority, in detail.** `0xA6` after
`c` is now fixed (cedilla -- see the "Overloaded" section above); this
writeup is about the small `a` minority that isn't. Means ogonek in
some Polish words (GTU record `.b10016855`, "filozofią") but cedilla in
Portuguese/Romanian words (GTU record `.b1031037x`, "revelação") --
confirmed against both records' real Sierra catalog display. Worse, in
at least one occurrence (the same `.b10016855` record, "współczesną")
the byte sits *before* the vowel that needs the mark rather than after
it -- the fix mechanism (attach to the letter before the junk) can't
handle that shape at all, regardless of which mark is intended.

Checked against the Library of Congress's own authoritative catalog
records for both titles (not Sierra's display, which the user didn't
trust for this check) via `search.catalog.loc.gov`, since both are
LC-cataloged works with an LCCN:

- LC's record for the Polish title (LCCN 76350390) is itself
  internally inconsistent about this exact word: field `240` (uniform
  title) renders it `"Z badań nad filosofia̦ współczesna̦..."` --
  spelled with "s" (not "z"), and marked with what looks like a
  combining comma-below rather than textbook ogonek, on *both* final
  "a"s. Field `500` (a quoted note) renders the same title
  `"...Z badań nad filozofią współczesna."` -- correct ogonek on
  "filozofią" (spelled with "z" this time), but **no mark at all** on
  the final "a" of "współczesna". Three different renderings of the
  same two words, from the same authoritative record. This directly
  confirms the earlier finding: the "współczesną" shape that breaks
  this fixer's before-letter mechanism isn't a corner case to design
  around -- even LC's own catalogers didn't transcribe that specific
  ending consistently by hand.
- LC's record for the Portuguese title (LCCN 74210234) is clean and
  confirms the cedilla reading: field `245` renders
  `"O conceito de revelação na controvérsia modernista..."` with a
  plain, unambiguous cedilla on "revelação".

Net effect: both of the original readings (ogonek vs. cedilla) were
right, so this doesn't change the "leave the `a` minority unfixed"
decision -- it reinforces it. Even the authoritative source is
inconsistent for the specific word shape that already defeated the fix
mechanism on syntactic grounds alone.

**`0xB2`, in detail.** `0xB2` is not in `_MARC8_BARE_COMBINING_BYTES` or
`_MARC8_BARE_STANDALONE_BYTES` -- it never got a first confirmed
before-letter at all, so `_MARC8_BARE_COMBINING_RE` doesn't match it.
Re-scanned `working/GTU_bibs.mrc` with a one-off regex built from the
same reusable pieces (`_MARC8_NOT_ESCAPE_DESIGNATOR_LOOKBEHIND` +
`([A-Za-z])` + `_MARC8_STRAY_ESCAPE_JUNK_NONEMPTY` + the literal byte;
see `working/find_0xb2_occurrences.py`, gitignored): 832 occurrences
across 396 records (not 745 -- the close-escape fix changed the count
here too, same as it did for `0xA4`/`0xA5`/`0xA8`, just upward instead
of downward this time). No single before-letter dominates (highest is
`n` at 129/832, 15%) -- confirming the original "murky" read, not
overturning it.

The largest clusters (`G`/71, `K`/47, `k`/39, `g`/47, many `n`/`l`/`d`/
`s` occurrences) are overwhelmingly German (Göcke, Körper, könnte,
Köhler, Königsherrschaft, göttliche, zeitgenössischen, religiöser,
persönliche) plus a smaller Dravidian/Tamil cluster (Murukan̄). Tested
the diaeresis hypothesis directly via `pymarc.marc8.marc8_to_unicode`
(ANSEL order, mark byte before base-letter byte): for a clean case like
`\xe8` + `G` + `cke`, the decoder correctly produces a single
diaeresis-bearing letter -- but that letter is `G̈`, not `ö`. The real
words all need a whole **inserted** `ö` as its own letter between the
matched before-letter and the following consonants (`zeitgen` + `ö` +
`ssischen` = `zeitgenössischen`), not a mark fused onto the
before-letter itself. `_MARC8_BARE_COMBINING_BEFORE_OVERRIDES`'s whole
mechanism (`mark + before`, producing one combined character) cannot
produce this shape -- same structural "destroyed base letter" failure
mode already confirmed for roughly half of `0xA4`'s leftovers and part
of `0xA8`'s, just the dominant pattern here rather than a minority one.
The remaining smaller groups (Arabic "dhimmi", the Korean/Arabic
singletons, the suspected false-positive "JohannesVerl...") don't add up
to a second confirmable cluster either. **No code change** -- `0xB2`
stays unimplemented, now with a concrete mechanism explanation instead
of just "murky."

**`0xB3`, in detail.** Also not in either BARE dict; same one-off-regex
treatment (`working/find_0xb3_occurrences.py`, gitignored): 48
occurrences across 20 records (not 49 -- one occurrence's stale count,
same close-escape-fix effect). Grouping by before-letter (`e`/12, `a`/9,
`c`/5, `b`/5, `o`/4, ...) looked promising at first glance, but reading
every context line shows the shape is near-identical across almost all
of them: `<letter>` + `\x1bp+\x1bs` + `\xb3` immediately preceding or
following a philological siglum -- Dead Sea Scroll manuscript sigla
("1QIsa" + byte, "4QSamuel" + byte, "4QpaleoExod" + byte, "11QPs" +
byte), a Qumran scroll cave reference, and similar superscript-letter
notation in Semitic-studies titles/notes (44 of 48 occurrences, ~92%).
None of these are a diacritic at all -- the byte is standing in for a
superscript Roman letter (e.g. "1QIsaᵃ"), which this tool's mark-
insertion mechanism (`_MARC8_BARE_COMBINING_BEFORE_OVERRIDES`) cannot
produce regardless of which mark is chosen; it would need a dedicated
letter-to-Unicode-superscript substitution, a different fix entirely.
The one exception, record `.b1122034x` ("Ye" + byte + "n Presbyterian",
alternate title for "We Presbyterians = Yŏn Presbyterian"), tested
against the breve hypothesis (`\xe6` + `e` + `n Presbyterian`) decodes
to `ĕn Presbyterian`, not `Yŏn` -- the before-letter captured (`e`)
isn't the vowel the title actually needs the breve on (`o`), the same
mismatch shape as `0xA6`'s "współczesną" case. Genuinely ambiguous,
same conclusion as before. **No code change** -- `0xB3` stays
unimplemented.

## False positives found and guarded against

Both found by re-running `fix_marc8_diacritic_escapes` against a second,
different-library corpus (`working/WTS_bibs_2026-10-01.out`) as a sanity
check after the table above was confirmed against GTU alone.

**A close escape's own "B" byte mistaken for real text.** When a close
escape (`\x1b(B`) sits immediately next to a different, confirmed-payload
escape with no real letter between them, the "before letter" capture could
match the close escape's own "B" as if it were text, splicing a mark onto
escape machinery itself rather than real content (found on a WTS
"Imperfect:" note about damaged book signatures, nowhere near a
diacritic). Fixed with `_MARC8_NOT_ESCAPE_DESIGNATOR_LOOKBEHIND`, a
negative lookbehind for `\x1b\(` applied to both regexes that capture a
before-letter.

**Early-printed-book "Signatures:" collation notes.** ESTC-style
cataloging convention: a `500 $a` note like `"Signatures: A-C⁴ D²."`
records a book's gathering/leaf-count structure, using a superscript digit
per gathering. In WTS's corrupted data this reads `"Signatures: A-C` +
`<escape>` + `D` + `<escape>` + `".\x1b(B"` -- the escape mechanism meant
for a lost diacritic is apparently reused by whatever corrupted this
corpus for a lost superscript leaf-count digit instead, and the fix
mechanism can't tell the difference: it inserted a bogus mark on the
nearest letter, producing nonsense on a note that was never about accents.

Four heuristics based on the surrounding letters' case/position were tried
and rejected -- each verified against the full `working/GTU_bibs.mrc`
corpus, each broke between 25 and 19,707 legitimate fixes (genuine accented
words are routinely followed by a capitalized next word, and the
single-letter French word "à" immediately before a capitalized proper noun
is syntactically identical to the false-positive shape). What works instead:
skip the escape-payload/bare-byte fix entirely whenever the enclosing
subfield or control-field text itself starts with the label `"Signatures:"`
or `"Signature:"` (case-insensitive, leading whitespace allowed) --
`_MARC8_SIGNATURES_NOTE_PREFIXES`, checked at the top of
`_fix_marc8_diacritics_in_text`. Confirmed empirically
(`working/check_signature_label_fast.py`): excludes all 8 confirmed false
positives in WTS and 0 genuine fixes in GTU's 310,036 -- GTU's lone
`"Signatures:"` match is the identical false positive (a lost superscript
leaf-count mark, not a lost diacritic), so excluding it is a bonus fix, not
a regression.

## Rejected approaches

**A static payload -> letter table for the CJK/EACC escape.** Analyzed
separately in `docs/MARC8_ESCAPE_ANALYSIS.md`: that escape shape destroys
the base letter, not just the diacritic, so many different byte payloads
resolve to the same intended letter with no decodable structure. Addressed
instead by a same-file corpus/frequency lookup
(`build_marc8_corpus_index`/`lookup_marc8_corpus_word`) that suggests (but
never auto-applies) a specific word when it's spelled correctly elsewhere
in the same file.

**Guessing on any of the "investigated, not implemented" bytes above.**
In every case, the byte means more than one thing depending on context,
or the evidence was too thin/too tangled to trust at the volumes already
confirmed for the bytes in the tables above.
