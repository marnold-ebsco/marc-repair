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

- `0xAE`: 109 of 135 occurrences (81%) are `h` + dot-below (Near
  Eastern/biblical names). The rest are a scatter of unconfirmed words.
- `0xA5`: 251 of 318 occurrences (79%) are `r` + dot-below (Sanskrit IAST
  vocalic r -- "Ṛgveda", "Kṛṣṇa", both textbook-unambiguous). The rest are
  Greek/Tibetan/Arabic transliteration that don't share one mark.
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

## Investigated, not implemented -- genuinely ambiguous or insufficient evidence

These candidate bytes were found by the same methodology (stray junk +
bare byte) but did **not** get a safe, confirmed-by-volume single
interpretation. No code handles them; they still reach
`suspect_marc8_escape`'s generic hint.

| byte | occurrences | why it's unresolved |
|---|---|---|
| `0xA6` | 267 (145 records) | Overloaded *and* structurally broken -- see writeup below. |
| `0xBA` | 69 (37 records) | Means Hungarian double-acute (ő/ű -- "felelős", "György") in roughly half its occurrences and Russian hard sign (ʺ -- "obʺedinenii") in the rest, with no reliable split by surrounding letter. |
| `0xB2` | 745 (398 records) | Murky: frequently tangled with multiple macron escapes within the same word, and at least one occurrence ("JohannesVerl...") appears to need no diacritic fix at all -- a likely false-positive match for the detection shape itself. |
| `0xB3` | 49 (21 records) | Two unrelated phenomena sharing one byte: Dead Sea Scroll sigla superscripts (e.g. "1QIsaᵃ" -- would need a letter-to-Unicode-superscript substitution, a different fix mechanism entirely, not a mark insertion) and what looks like a Korean name needing a breve ("Yŏn Presbyterian"). |
| `0xC1` | 8 (3 records) | Tiny sample with an escape structure (`\x1bb8`/`\x1bb9` fragments) not seen anywhere else in the corpus -- may not even be the same corruption mechanism as the rest of this table. |

**`0xA6`, in detail.** Means ogonek in some Polish words (GTU record
`.b10016855`, "filozofią") but cedilla in Portuguese/Romanian words
(GTU record `.b1031037x`, "revelação") -- confirmed against both
records' real Sierra catalog display. Worse, in at least one
occurrence (the same `.b10016855` record, "współczesną") the byte sits
*before* the vowel that needs the mark rather than after it -- the fix
mechanism (attach to the letter before the junk) can't handle that
shape at all, regardless of which mark is intended.

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
right, so this doesn't change the "leave `0xA6` unconfirmed" decision
-- it reinforces it. Even the authoritative source is inconsistent for
the specific word shape that already defeated the fix mechanism on
syntactic grounds alone.

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
