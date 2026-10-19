# Handoff Notes

For closed-out work from prior sessions (the full `fix_marc8_diacritic_escapes`
project, the 010/880 missing-`$a` categories, the `parse_directory` phantom-entry
fix, log-formatting history, etc.), see
[docs/HANDOFF_HISTORY.md](docs/HANDOFF_HISTORY.md). This file only carries the
current state and open items.

## Current state

### OPEN -- `--guarantee-importable` flag, branch `guarantee-importable` (2026-10-19)

**Status: implemented and tested on branch `guarantee-importable` (pushed to
origin), not yet merged to `main`. Picking this back up later.**

**Background:** while testing a FOLIO Data Import run of
`tests/fixtures/kitchen_sink_bib.mrc` through the UI (bisecting record-by-
record after the full file died with no job log at all), found that the
culprit was `ks-oversized-ok` -- a record over 99999 bytes, spec-valid
MARC21 using the leader's documented oversized-length sentinel
(`oversized_sentinel_fixed` category). FOLIO Data Import aborts the
**entire batch** with no job log on a record like this, rather than
rejecting just that one record. (Everything else bisected out fine --
ordinary per-record validation errors/warnings, which is the normal,
expected behavior being tested for.)

**What was done:** added `--guarantee-importable` (off by default) to
`marc_repair.py`. When set, a record needing the oversized-length sentinel
is diverted to the `_error.mrc` file and logged as `unfixable` (same
section/category as a genuinely unfixable record) instead of landing in
the main output -- so everything left in the main output is guaranteed
importable. Threaded through both the bib pipeline (`main()`) and the
holdings pipeline (`repair_holdings_records()`, new `guarantee_importable`
param). Updated `docs/REPAIR_CATEGORIES.md` (via
`tools/generate_repair_categories_doc.py`) and added
`TestGuaranteeImportable` in `tests/test_marc_repair_core.py` -- run
against the real `tests/fixtures/kitchen_sink_bib.mrc`, with an
extensible `_GUARANTEE_IMPORTABLE_CASES` table (record id -> expected
output file) for adding future findings. Full suite (487 tests) and
flake8 (`--max-line-length=100`) both clean.

**Where to pick this up:**
1. Re-run the full `kitchen_sink_bib.mrc` (with `--guarantee-importable`)
   through FOLIO Data Import end-to-end to confirm the main output now
   imports cleanly with per-record errors only, no batch-level death.
2. Decide whether any other category belongs in this same "known to break
   a real import target" bucket -- so far `oversized_sentinel_fixed` is
   the only empirically-confirmed one; the registry/test table is
   designed so adding another is a one-line change (see
   `_GUARANTEE_IMPORTABLE_CASES`'s own docstring).
3. Decide whether to merge `guarantee-importable` into `main`, or keep
   iterating on it first.

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
