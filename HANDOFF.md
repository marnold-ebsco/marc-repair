# Handoff Notes

For closed-out work from prior sessions (the full `fix_marc8_diacritic_escapes`
project, the 010/880 missing-`$a` categories, the `parse_directory` phantom-entry
fix, log-formatting history, etc.), see
[docs/HANDOFF_HISTORY.md](docs/HANDOFF_HISTORY.md). This file only carries the
current state and open items.

## Current state

### OPEN -- non-standard MARC field check: stage 1 DONE (commit 6263d4b), stage 2 PLANNED, awaiting answers (2026-10-22)

**Do not start stage 2 until the user answers the questions below and says go.**

**Stage 1 (done, committed on `guarantee-importable`):**
`standard_marc_fields.txt` (repo root, hand-editable; `[standard]` 254 tags,
`[obsolete]` 38 tags; one `TAG | TITLE` per line; anything unlisted is
non-standard). Drawn from https://www.loc.gov/marc/bibliographic/ecbdlist.html
(leader/subfield detail ignored). 590 and 690 accepted as local fields (590 is
obsolete on the LOC page; the user's rule wins); every other 59X/69X is
non-standard; 009 is obsolete, 008 is standard. Regenerate with
`tools/build_standard_marc_fields.py [saved_page.html]` (the page is ~256k
chars -- WebFetch truncates it, so download the HTML). Tests:
`tests/test_standard_marc_fields.py`. Not yet wired into `marc_repair.py`.

**Stage 2 requirements (from the user):** check every record and log each
non-standard field. Information only (record never modified). Detailed entries:
position in file, record number, the field, and the first ~20 characters of its
data, human-readable: subfield delimiter removed, subfield code written `$a`,
one space after the code, then the content; end-of-field marker removed.
Category along the lines of "Non-Standard MARC fields". Obsolete-listed tags
reported but marked obsolete. 9XX never reported; "normal" fields such as 880
and 886 are on the LOC list so are standard. If a tag has more than 10
occurrences show only the first 10 in detail, then "and N more".

**Mockup shown to the user** (real GTU examples; `GTU_bibs.mrc`, 404,956 recs):

```
=== INFORMATION ONLY: non_standard_marc_fields ===
=== Record contains a data/control field whose tag is not in
===   standard_marc_fields.txt (LC's MARC 21 bibliographic field list, plus
===   local 590/690) or is listed there as obsolete. 9XX fields are never
===   reported. Nothing is changed or removed; this is a report only.
=== NO DATA LOSS ===
=== <N> instance(s) in <M> record(s); 10 per tag listed in detail ===
	record 175057 (.b12178172)	offset 211038181	605 _0 $a Underground Railr...
	record 354610 (.b14353234)	offset 426106395	605 0_ $a Methodism $x Histo...
	record 281262 (.b13422443)	offset 329652152	606 0_ $a v. 1. Advent to Wh...
	record 250061 (.b1304493x)	offset 296414379	612 _0 $a American Baptist G...
	record 225467 (.b1277540x)	offset 269023709	620 10 $a Society of Friends
	record 265877 (.b13255629)	offset 313278993	646 30 $a One thousand city...
	record 139075 (.b11576376)	offset 163068702	659 _0 $a Ceremonial exchan...
	record 315592 (.b13873003)	offset 372949992	660 10 $a Confederate State...
	record 124982 (.b11411119)	offset 147215821	670 1_ $a Miller, Lewis, $d...
	record 1 (.b10000057)	offset 0	090 __ $a B734 $b .D5  [OBSOLETE]
	... (9 more 090 entries) ...
	and 332,537 more 090 [OBSOLETE]
```

GTU findings: 679 and 684 do not occur in GTU. High-volume non-standard tags:
049 (206,634), 799 (11,620), 440 (1,222, obsolete); 090 (obsolete) 332,547.
444 has 11. 002 (2) and 004 (39) occur with odd content.

**Stage 2 implementation plan (est. 50-70k tokens):**
1. Loader: read `standard_marc_fields.txt` from next to `marc_repair.py` (same
   way `required_a_tags.txt` is located). Returns standard + obsolete sets.
2. Check (bib records only): each tag not in `[standard]` and not 9XX -> one
   finding (record number, byte offset, `record_identifier(parsed)` id, tag,
   indicators, preview). Never modifies the record. Preview: strip 0x1F and
   end-of-field mark, `$<code> ` per subfield, cut at ~20 chars of data with
   "..." if truncated.
3. Per-tag cap: count all occurrences for totals, keep only first 10 per tag as
   entries; `write_log` (marc_repair.py ~line 6591) emits "and N more <tag>"
   lines. Needs a small `write_log` extension.
4. New log section (INFORMATIONAL categories are header+count only, never
   listed in full, so this needs a dedicated section -- see `_section_for`,
   `_DEDICATED_SECTIONS`, `_CHECK_DESCRIPTIONS`, `_CHECK_DATA_LOSS`,
   `_CATEGORY_DISPLAY_NOTE`, `LogEntry.render` ~lines 5951-6700). Make it work
   with `--log-full`.
5. Tests (pytest): standard / obsolete / non-standard / 590,690 / 9XX / cap and
   "and N more" / preview formatting / section against
   `working/GTU_bibs_sample50.mrc`.
6. Verify on GTU; flake8; full suite.
7. Docs: README + regenerate categories doc via
   `tools/generate_repair_categories_doc.py`.

**OPEN QUESTIONS for the user (ask/answer all in one round):**
1. Position in file: record number + byte offset (as in mockup)? Existing logs
   use record number only.
2. On by default, or behind a flag? (Recommended: on by default -- info only,
   cheap.)
3. Is the 10-entry cap per tag (assumed) rather than 10 total?
4. Section/category names: `INFORMATION ONLY` / `non_standard_marc_fields`, or
   "Non-Standard MARC fields" as the user's wording?
5. Show indicators (`_` for blank, as in mockup)?
6. Bib records only (assumed; the list is the bibliographic one)?
7. Add any of 049, 799, 440, 090 to the accepted list? (049/799 look like local
   fields.) Default: leave the list unchanged.

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
