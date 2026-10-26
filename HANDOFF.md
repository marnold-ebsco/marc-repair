# Handoff Notes

For closed-out work from prior sessions (the full `fix_marc8_diacritic_escapes`
project, the 010/880 missing-`$a` categories, the `parse_directory` phantom-entry
fix, log-formatting history, etc.), see
[docs/HANDOFF_HISTORY.md](docs/HANDOFF_HISTORY.md). This file only carries the
current state and open items.

## Current state

### DONE -- supplemental file listing every non-standard MARC field (2026-10-26, on `main`: `1fb4f6a`)

**Status: built, tested (540 pass), committed and pushed.** The log's
`non_standard_marc_fields` section still lists only the first 5 per tag
("and N more"); `<log name>_non_standard_fields.txt` (next to the log) lists
every occurrence in the same layout, grouped by tag, with no cut-off. Not
written when nothing is non-standard; its path is printed to stderr.

- `NonStandardFieldTally.check(parsed, record_idx, rec_id)` streams each
  occurrence to a `tempfile.TemporaryFile` (one line each, per-tag line
  offsets in `array("Q")`), so memory stays small on big files;
  `write_non_standard_supplement(path, tally)` builds the file from it and
  `tally.close()` releases the spool. Only the bib `main` path uses it.
- Tests: `TestNonStandardMarcFields` (supplement lists all 8 of 8; no file
  when nothing non-standard).

### DONE -- recode unambiguous 856-without-`$u` URLs in bibs (2026-10-26, on `main`)

**Status: built, tested (538 pass), committed and pushed.** Run against
`working/GTU_bibs.mrc` (the 0928 source file isn't on this machine): 14 of the
21 `bib_856_missing_u` records are recoded, 7 stay flagged.

- `recode_856_missing_u` (bib pipeline only, always on, no switch) runs just
  before `find_856_missing_u` in `main()`. Recodes to `$u`: (1) a lost `u` code
  -- `$a`/`$i` data `u http://...` or `$u http://...`, or `$a` "u" + `$h`
  "ttps://..." (shifted code); (2) exactly one `$a`/`$z` that is only an
  http(s) URL; (3) the same for a bare `www.` address, with `https://` added.
- Logged under `bib_856_missing_u` (fixed=True, same NEEDS REVIEW section) with
  the action in parens, e.g. `($a recoded to $u, https:// scheme added)`.
  Unfixed entries now end `(has no $u subfield)` and quote at most 40 chars.
- Left flagged: URL embedded in text (`.b13720995`, `.b14343034`) and no URL
  at all (`.b11054049`, `.b12232397`, `.b14101257`, `.b14898032`,
  `.b19606175`). Holdings `holdings_856_missing_u` is unchanged (detect-only).
- Not done: the section header still counts recoded + unfixed together
  ("21 record(s)"); no `--no-...` opt-out switch.
- Docs: row in `tools/generate_repair_categories_doc.py` (regenerated
  `docs/REPAIR_CATEGORIES.md`). Tests: `TestBib856MissingU` recode cases in
  `tests/test_marc_repair_bib.py`. flake8 still reports the pre-existing E501s.


### DONE -- recode title-only 7xx to 730 / 650 `$b` to `$a`; 40-char log previews (2026-10-26, on `main`)

**Status: built, tested (533 pass), committed and pushed.** Re-run against
the full GTU 2026-09-28 file: `field_removed_because_missing_a` fell from 122
to 8 instances.

- `recode_title_only_7xx_to_730`: a 700/710/711 with exactly one `$t` and no
  real `$a` (missing, empty or punctuation-only) becomes a 730: `$t` -> `$a`,
  other subfields kept if a 730 carries them (`_730_CODES`; no `$6`, `$b`,
  `$e`, `$q`...), ind1 = nonfiling count (leading The/An/A, only when 008/35-37
  is `eng`), ind2 = `2`. New 730s go after existing 730s, before the next
  higher tag. GTU: 113 instances in 20 records. Category
  `recoded_title_only_7xx_to_730` (NO DATA LOSS, FIXED/REQUIRES ATTENTION,
  header + count by default). Opt-out `--no-recode-title-only-7xx`.
- `recode_6xx_b_to_a`: 650/651/655 with no real `$a` whose first subfield is
  the only `$b` -> `$b` becomes `$a` (obsolete `$b` there). Not done for
  600/610/611 (`$b` is a real name part). GTU: 1 record. Category
  `recoded_6xx_b_to_a`, opt-out `--no-recode-6xx-b-to-a`.
- Both run in `main()` after `strip_null_identifiers`, before
  `strip_missing_required_a`. Bib pipeline only.
- Log previews: `_log_preview` / `_subfield_preview` cap quoted field text at
  40 characters + `...`; used by `strip_missing_required_a` (all tags, so also
  holdings) and the new recodes.
- Deliberately NOT done: 6xx `$t`-only -> 630 (the one GTU case, `.b14081994`,
  is three paper titles joined by `--`, not a subject). The 8 remaining GTU
  removals are junk or unrecoverable (`.b11178942`, `.b11568173` holdings
  statement in a 600, `.b14081994`, `$s93.00`, `$a***`, `650 $fMDP`, the
  `710 $b/$c/$k Thesis`, blank-`$a` 100) -- fix by hand in the source if wanted.
- Tests: `TestLogPreviewLength`, `TestRecodeTitleOnly7xxTo730`,
  `TestRecode6xxBToA`, `TestRecodePipeline` in `tests/test_marc_repair_bib.py`.
  Kitchen sink fixtures were not extended.

### DONE -- oversized fields are collapsed, then truncated, instead of dropped (2026-10-26, on `main`)

**Status: built, tested (513 pass), committed and pushed.** Not yet re-run
against the full GTU corpus -- only the isolated record
(`working/single_1456853630.mrc`, 001 `1456853630` / `.b1929265x`).

Supersedes the old `dropped_oversized_field` behavior (see
`docs/HANDOFF_HISTORY.md`): a field whose real, delimiter-bounded content is
over 9999 bytes (recovered by `parse_directory` from a wrapped directory
length) is now shrunk to fit in `_read_intact_at` via
`_shrink_oversized_content`, stopping as soon as it fits:
1. merge redundant `ESC(B` + same-set re-designations (lossless);
2. strip spurious `ESC b|p <digit> ESC s` pairs (drops a subscript/superscript
   digit -- a producer artifact), then merge again;
3. truncate (`_truncate_to_fit`) at the last ` --`, else last subfield
   delimiter, else a hard cut; never splits a multibyte char or ESC sequence,
   appends `ESC(B` if the kept text ends in a non-ASCII escape state.

Log categories: `collapsed_oversized_field_escapes` (new, POSSIBLE DATA LOSS,
FIXED/REQUIRES ATTENTION) and `truncated_oversized_field` (the renamed
`dropped_oversized_field`, DATA LOSS). `ParsedRecord` carries
`collapsed_oversized_fields` / `truncated_oversized_fields`.

b1929265x's 505: 10,033 -> 3,672 bytes by collapse alone (947 pairs removed),
record keeps all 42 fields. Tests: `tests/test_oversized_field.py`; kitchen
sink gained `ks-oversized-collapse` and `ks-oversized-truncate`
(`_wrapped_length_record` in `build_kitchen_sink.py` builds the wrapped
directory length). `kitchen_sink_bib.log` / `_repaired.mrc` were regenerated
from a plain default run, which also refreshed content that had gone stale.
Both categories are in `docs/REPAIR_CATEGORIES.md` (bib and holdings tables).
Open: run a full GTU re-run to confirm nothing else changes.

### OPEN (minor) -- flake8 config (2026-10-26)

No flake8 config exists, so default `flake8` (79 cols) reports ~487 E501 in
`marc_repair.py`; earlier sessions used `--max-line-length=100`. Ignoring E501,
`marc_repair.py`, `tests/` and `tools/generate_repair_categories_doc.py` are
clean; `tools/context_probe/` has 13 pre-existing issues (blank lines,
trailing whitespace). Optional: add a flake8 config (e.g. `max-line-length =
120`, exclude `venv*`/`.venv`/`working`) and clean up `context_probe`.

### DONE -- log detail text condensed / `=TAG` labels (2026-10-23, on `main`: `68bf938`, `04d91c0`, `f9993f0`)

**Status: built, tested (509 pass), committed and pushed.**

Goal: log details use the standard `=TAG` field label (as in
`=856  42$3... has no $u subfield`) and stop repeating boilerplate per line.
- `suspect_marc8_escape`: bullets are `=500: '<window>'` (merged tags:
  `=245, =830: '...'`), not `tag 500: ...`. The "(e.g. o, e, n, u)" hint is
  gone from each record line (now only in the category description), so the
  header reads `likely a miskeyed accented letter; verify against another
  source. LCCN: ... OCLC: ...`.
- `suspect_hex_encoded_marc8`: recoverable -> `=880 $a: recovered '<text>' (23
  groups, 2 layers); verify against source`; not recoverable -> `=880 $a:
  POSSIBLE DATA LOSS, doesn't decode: '<text>' (5 groups, 1 layer); context:
  ...`. The caveats (byte alignment, truncated/boundary-shifted) moved into the
  category description.
- `unfixed_non_numeric_tag`: `=ABC: not 3 numeric digits`.
- `non_standard_marc_fields`: entries start `=<tag> $a ...`.
  `_write_non_standard_blocks` groups by `detail[1:4]` to skip the `=`; any
  other code that slices these details by position must do the same.

Open (not done, only noticed): `added_field` still says `added missing 040
field` and the 880 linking detail says `880 linked to 245` (no `=`); a
whole-field `=856  42$3...` style (indicators + subfields) is not used by the
snippet-based escape/hex categories, which have no indicators to show. Other
categories keep their `=tag: ...` / `=tag  ind$...` forms. Not checked:
`tools/` report scripts that might parse these two detail strings.

### DONE (needs real-data confirmation) -- required record identifier check (`--source-system`), branch `guarantee-importable` (2026-10-23)

**Status: built, tested (509 pass), committed and pushed. Not yet seen to
reject a real record -- see "Open".**

What it is: a bib-only check, UNFIXABLE section but its own category
`record_identifier_required` (not the generic `unfixable`; log header
`=== UNFIXABLE: record_identifier_required ===`, DATA LOSS; `=UNFIXABLE` in
`.mrk`). `--source-system sierra` requires a 907 $a on every record with **no
fallback to 001**; `--source-system other` requires a 001. A failing record is
removed from the main output and written unchanged to `_error.mrc`. If
`--source-system` is not given it is asked for at a prompt; with stdin not a
tty the check is skipped with a stderr warning (so scripts/tests don't hang).

Code: `marc_repair.py` -- `SOURCE_SYSTEMS`, `missing_identifier_reason()`, an
`elif` after the `parsed.unresolved` branch in `main()`, prompt just before the
main loop. `record_identifier()` (log labels, duplicate detection) is unchanged
and still falls back 907 $a -> 001. Tests: `TestSourceSystemIdentifierCheck`
in `tests/test_marc_repair_bib.py`; about 50 existing synthetic records gained
a 001 so they are not diverted.

Not done: holdings pipeline (holdings link by 004, so left alone).

**Open:** the expected failure (loader error "907 $a is missing, although it is
required for Sierra/iii migrations", record 001 `1456853630`) was NOT
reproduced. `GTU_bibs.mrc` / `Downloads/Graduate_Theological_Union_instancesUnsuppressed_2026-09-28.mrc`
(same 527,497,619-byte file) has a 907 $a on all 404,956 records, and that
record has `907 $a .b1929265x` there. The pasted record (shorter leader,
garbled 505, no 907) comes from a different file/version -- need the
`..._repaired.mrc` or its source, to check whether 907 was lost in a run.

### DONE -- non-standard MARC field check (`non_standard_marc_fields`), branch `guarantee-importable` (2026-10-22)

**Status: built, tested (504 pass), committed (`6263d4b` list, `29c2ef7`
check). Nothing outstanding except what's under "Possible follow-ups".**

What it is: a report-only, on-by-default, bib-only check. Every field whose tag
is not in `standard_marc_fields.txt` is counted; the first **5 per tag** are
logged (`record N (.bNNN)<TAB>=<tag> $a first-20-chars...`, no indicators, no
byte offset), then `and N more <tag>`. Each tag gets its own block in numeric
order with its own counts, `[OBSOLETE]` if listed obsolete. 9XX and non-numeric
tags are never reported. Section `INFORMATION ONLY` (sorted last), category
`non_standard_marc_fields`.

Where things are:
- `standard_marc_fields.txt` (repo root, hand-editable, `[standard]` /
  `[obsolete]`, `TAG | TITLE`; unlisted = non-standard). Generated from
  https://www.loc.gov/marc/bibliographic/ecbdlist.html by
  `tools/build_standard_marc_fields.py [saved.html]` (WebFetch truncates the
  page -- download the HTML). Hand-added accepted tags live in that script's
  `LOCAL_STANDARD`, so regenerating keeps them (editing only the .txt will be
  overwritten on regeneration).
- Accepted local: 019 029 049 069 090 092 096 099 590 599 690 799; accepted
  though obsolete in MARC: 265 440. 009 is obsolete (008 is standard).
- Code: `marc_repair.py` -- `load_standard_marc_fields`, `field_preview`,
  `NonStandardFieldTally`, `_write_non_standard_blocks`, `write_log(...,
  non_standard_tally=)`; CLI `--no-check-non-standard-fields`,
  `--standard-marc-fields-file`. Excluded from `--sample-problems`.
- Tests: `tests/test_standard_marc_fields.py`,
  `tests/test_marc_repair_bib.py::TestNonStandardMarcFields`.
- Docs: README file table, `docs/REPAIR_CATEGORIES.md` (regenerate with
  `tools/generate_repair_categories_doc.py`), `install.sh` ships the list.

Still reported in sample data (user left them non-standard on purpose): 004,
691/696/697, 539, 012, 079, 350*, 859, 890, 002, 092-style oddities etc.
(`*` = obsolete). Counts before accepting 440/265/069/599: GTU 1,839
instances/50 tags, Bucknell 5,021/4 tags, WTS 18,853/19 tags, UBahamas
6,915/12 tags.

Possible follow-ups (none requested): a third `[ignore]`-style section for tags
counted but never listed; holdings-record equivalent (the list is bib-only).

### OPEN -- import-safety defaults + `--guarantee-importable`, branch `guarantee-importable` (2026-10-19)

**Status: implemented, tested, and empirically verified end-to-end against
live FOLIO Data Import. Pushed to origin as branch `guarantee-importable`,
NOT yet merged to `main`. The only remaining open item is the merge
decision itself -- see bottom.**

**Background:** testing a FOLIO Data Import run of
`tests/fixtures/kitchen_sink_bib.mrc` through the UI (bisecting record-by-
record after the full file died with no job log at all) found the
culprit: `ks-oversized-ok`, a record over 99999 bytes using MARC21's
documented oversized-length leader sentinel (category
`oversized_sentinel_fixed`) -- spec-valid MARC21, but FOLIO Data Import
aborts the **entire batch** with no job log at all on a record like this,
rather than rejecting just that one record.

**What shipped (3 commits on this branch, in order):**
1. **`--guarantee-importable`** (initially opt-in): diverts the
   oversized-sentinel record to `_error.mrc` (logged as `unfixable`)
   instead of the main output. Also implies `--no-add-default-245`:
   diverts a missing-245 record to `_needs_review.mrc` (category
   `diverted_for_review`) instead of guessing a placeholder title.
   Threaded through both the bib pipeline (`main()`) and holdings
   (`repair_holdings_records()`). Along the way, fixed a real bug this
   exposed: a record that was BOTH missing 245 AND genuinely unfixable
   (e.g. a field over 9999 bytes -- `ks-oversized-bad`) was being
   misclassified into `_needs_review` instead of `_error`, because the
   missing-245 diversion ran before the real UNFIXABLE check and
   short-circuited past it. Fixed by trying `assemble_marc` before
   diverting for review, falling through to UNFIXABLE when it raises.
2. **Made the oversized-sentinel diversion the unconditional default**,
   no flag required. Root cause: a *plain default run* (no flags at all)
   of `kitchen_sink_bib_repaired.mrc` was imported into FOLIO and failed
   with no job log -- the exact same silent batch-kill as the original
   raw-file test, just from the "repaired" output this time. A file named
   `_repaired.mrc` has no business containing something that kills the
   whole import. Added `--allow-oversized-sentinel` (off by default) to
   opt back into the old behavior for an import target confirmed to
   handle the sentinel correctly. `--guarantee-importable`'s only
   remaining job is the missing-245 implication.
3. **Documented exactly what `--guarantee-importable` diverts**, in a new
   "Import-safety" section at the top of `docs/REPAIR_CATEGORIES.md`
   (generated from `tools/generate_repair_categories_doc.py`, not
   hand-edited).

**Empirically confirmed against live FOLIO** (bugfest, correct bib job
profile -- "Default - Create instance and SRS MARC Bib"), not inferred
from this tool's own "data loss" labels:
- The default-settings repaired output of `kitchen_sink_bib.mrc` (48
  records, oversized record already diverted) imported **100% cleanly --
  48/48 created, 0 errors, 0 no-action.**
- This includes every 010 (LCCN dedup/whitespace-strip) and 6xx/7xx
  (missing-`$a` field removal) record -- confirmed these do NOT need
  diverting once repaired; an earlier worry that they might was
  unfounded. `diverted_for_review` (missing 245) remains the only
  category `--guarantee-importable` diverts to `_needs_review.mrc`.
- Earlier raw-file (pre-repair) testing separately confirmed FOLIO also
  rejects (without killing the batch) a record missing 008, or carrying
  a non-numeric tag -- both already fixed unconditionally by
  marc_repair's own defaults (`added_default_008`, `fix_invalid_tags`),
  so neither needed any new handling.

Full suite (492 tests) and flake8 (`--max-line-length=100`) both clean
as of the last commit on this branch.

**Where to pick this up:**
1. Decide whether to merge `guarantee-importable` into `main`. Nothing
   else is blocking it -- this is now just a judgment call on timing/
   review, not unfinished work.
2. If a future FOLIO test ever turns up another category that genuinely
   needs diverting (same rigor as above: verify against a real job log,
   not by reading this tool's own DATA LOSS labels), add it where
   `diverted_for_review` is raised in `marc_repair.py`, extend
   `TestGuaranteeImportable._GUARANTEE_IMPORTABLE_CASES` in
   `tests/test_marc_repair_core.py` (one line), and update the
   "Import-safety" section in `tools/generate_repair_categories_doc.py`.

### OPEN -- API-driven FOLIO Data Import never completes a job (script unproven, don't move it to `tools/` yet) (2026-10-16)

**Status: unresolved, separate from the 010 fix recorded in docs/HANDOFF_HISTORY.md. Scripts live in
`working/folio_import_test/` (NOT yet moved to `tools/` -- that move is
blocked on this). Session being cleared; picking this back up fresh.**

**Goal:** a reusable script to drive a real FOLIO Data Import run from
the command line (upload a `.mrc` file, kick off the default "Create
instance and SRS MARC Bib" job profile, poll to completion, pull back
the per-record job log) -- so 010/880/etc. $a edge cases (and future
ones) can be verified against live FOLIO without a human clicking
through the UI each time.

**What actually works, confirmed:**
- Login against the Kong API gateway: `POST /authn/login` with
  `X-Okapi-Tenant: fs09000000`, body `{"username":"folio","password":"folio"}`
  -- returns 201 with an `okapiToken`/`x-okapi-token` header. The UI host
  (e.g. `https://bugfest-sunflower.int.aws.folio.org/`) is just the
  Stripes frontend (serves `index.html` for every path, including API
  paths -- a trap, see below); the actual API gateway is a separate
  `kong-` subdomain: `https://kong-bugfest-sunflower.int.aws.folio.org`
  and `https://kong-bugfest-trillium.int.aws.folio.org` (same tenant
  `fs09000000`, same `folio`/`folio` creds). This is a Eureka-platform
  tenant, not classic Okapi -- that's WHY the UI host silently returns
  200/HTML for every API call instead of a real 404, which burned a lot
  of time before the user supplied the Kong URL directly.
- `POST /data-import/uploadDefinitions` (create) and
  `POST /data-import/uploadDefinitions/{udId}/files/{fdId}` (upload the
  raw bytes, `Content-Type: application/octet-stream` -- must NOT also
  send FolioClient's default `application/json` header, or the upload
  endpoint 400s) -- both work, file status ends up `UPLOADED`.
- `POST /data-import/uploadDefinitions/{udId}/processFiles` with
  `{"uploadDefinition": <the uploaded definition>, "jobProfileInfo":
  {"id": "e34d7b92-9b83-11eb-a8b3-0242ac130003", "name": "Default -
  Create instance and SRS MARC Bib", "dataType": "MARC"}}` returns
  **204** (accepted) and a `jobExecutionId` does get created.

**What's actually broken:** every job execution created this way sits
at `status: NEW` / `uiStatus: INITIALIZATION`, `progress: {current: 0,
total: 100}` (sometimes `{current: 0, total: 0}` for the composite
parent) **forever** -- confirmed polling for 3+ minutes across many
separate attempts, on BOTH bugfest-sunflower and bugfest-trillium
(`fs09000000` on each). It never advances, never errors out, just
hangs.

**The key clue, not yet followed up on:** when the user instead
uploaded the same file (`all_variants_1.mrc`) through the actual Data
Import **UI** on trillium, it worked -- hrId 12417
(`f7789947-da97-48c3-b3de-76d9df002b86`) actually completed (status
`ERROR` only because one of the 6 test records was expected to fail;
the other 5 show `CREATED`/instance IDs in the job log). That happened
on the SAME tenant, SAME day, using the SAME job profile ID, after
every one of this script's own attempts had already stalled. So:

- This is NOT simply "the environment's Data Import pipeline is down" --
  the UI path demonstrably works. (The earlier write-up in this file
  speculated about a tenant-wide Kafka/pipeline outage based on the
  tenant's own same-day `QuickInstanceExport` jobs also being stuck at
  `NEW` -- that may be true for THAT export feature, but it's no longer
  good evidence of a general outage once the UI-driven Data Import job
  is known to have worked fine.)
- Something the UI does differently from this script's `processFiles`
  call is the likely cause -- not an environment-wide stall.

**Where to pick this up:**
1. Use a browser's network tab (or FOLIO's own request logging) to
   capture the EXACT sequence of requests the Data Import UI makes for
   upload -> process, on the same trillium tenant, and diff it against
   `working/folio_import_test/run_import.py`'s `processFiles` call.
   Candidates for what's missing/different: an extra field in the
   `uploadDefinition` payload, a different/additional header, a required
   follow-up call this script never makes (e.g. something that actually
   triggers the Kafka producer rather than just flipping a status row),
   or a timing issue (UI might poll/retry `processFiles` or wait on a
   websocket/SSE signal before considering the upload "ready").
2. Re-test the script's exact same `processFiles` payload against
   trillium now that a known-good UI job (12417) has run there --
   confirms whether the API path is simply always broken on these
   tenants regardless of payload, or genuinely payload/sequence
   dependent.
3. Scripts to resume from, all in `working/folio_import_test/`:
   `make_records.py` (builds the 6 test `.mrc` variants), `run_import.py`
   (upload + process + poll + job-log dump -- the one that stalls),
   `check_jobs.py` / `job_12417_detail.py` (list/inspect job executions
   and per-record job-log entries by hrId or jobExecutionId -- both
   confirmed working against `/metadata-provider/jobExecutions` and
   `/metadata-provider/jobLogEntries/{jobId}`), `fix_locale.py` (unrelated
   one-off: changed the shared `folio` test user's UI locale on trillium
   from `de-DE` to `en-US` via `PUT /settings/entries/{id}` on the
   `stripes-core.prefs.manage`/`localeSettings` entry -- done, not part
   of this issue).
4. Once `processFiles` is fixed (or its real constraint understood), THEN
   move the whole `folio_import_test/` directory into `tools/` (e.g.
   `tools/folio_data_import_test/`), dropping the throwaway `.venv`,
   generated `.mrc` files, and `.log`/`.json`/`.run.stdout` output -- per
   the user's original ask to keep this as a reusable tool, not scratch.

No commit yet for this HANDOFF entry itself (the 010 fix's own entry, now
in docs/HANDOFF_HISTORY.md, was already committed as part of `812c76a`;
this new section was added after that push).

**2026-10-16 follow-up, abandoned for now:** Re-ran `run_import.py`
against bugfest-trillium (tenant `fs09000000`) with the 6 test variant
files combined -- upload succeeded (`UPLOADED`), `processFiles` returned
204 with `jobExecutionId: 5d409200-53ba-48fe-a615-b3c5a25217e4`, but it
sat at `status: NEW`, `progress: {current: 0, total: 100}` for the full
3-minute/60-poll window, never advancing. A sunflower attempt was killed
by its own timeout before logging anything (stdout buffering), so it's
inconclusive on its own, but the user separately reported that the Data
Import **UI** itself wasn't visibly processing any records on sunflower,
and then also not on trillium. Unlike the original write-up above (where
a UI-driven job on trillium completed on the same day the script
stalled), this time neither tenant's UI appeared to be processing either
-- suggesting today's stall may be a genuine environment-side outage
rather than (or in addition to) the API-vs-UI payload discrepancy
originally suspected. Also confirmed: phpFolioClient (all copies under
`/home/marnold`, including the composer cache) has no data-import class
at all -- only `dataExport()`/`dataExportAll()` (mod-data-export, a
different API) and generic `putFile()`/`postFile()` upload helpers in
`FolioFileHandler.php` -- so there's no reference implementation there to
diff `run_import.py` against beyond the generic octet-stream upload
pattern, which already matches. Abandoning this thread for now; pick back
up with the network-tab capture plan in "Where to pick this up" above,
and/or re-check whether the environment's Data Import pipeline has
recovered before assuming the script itself is at fault.
