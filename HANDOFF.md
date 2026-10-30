# Handoff Notes

For closed-out work from prior sessions (the full `fix_marc8_diacritic_escapes`
project, the 010/880 missing-`$a` categories, the `parse_directory` phantom-entry
fix, log-formatting history, etc.), see
[docs/HANDOFF_HISTORY.md](docs/HANDOFF_HISTORY.md). This file only carries the
current state and open items.

## Current state

### DONE -- run profiles, verified on real EC2 data (2026-10-29, merged to `main` and pushed; branch `feature/profiles` deleted 2026-10-29, fully merged)

**Verified on EC2 against a real mixed file:** `--profile mark-wms` run
against `working/bucknell_marc.mrc` (large real Sierra export, now on this
machine too) split and repaired both halves correctly -- bib half honoured
the profile's switches, both `_repaired` files and logs appeared as
expected. First run (before the parsing-overhead fix below) took 776.36s
start to finish; second run (after) took 722.49s. Still unproven: the
`mark-bib` / `mark-holdings` profiles (no switches beyond the mode flag).

**Next step (if wanted): try `--list-profiles` and `--profile mark-bib` /
`mark-holdings`** on already-split files, and a `nohup ... &` run with
`--profile NAME` to confirm that path end-to-end. Decided: with several
profiles and no TTY the run errors (kept deliberately; no fallback
profile).

**First EC2 run (bucknell, 1.47M records) found two things, both fixed:**
`install.sh` never shipped `holdings_required_a_tags.txt` (holdings repair
crashed with FileNotFoundError; a test now checks every data file the code
loads is in `FILES`), and the source-system question came late and silently
skipped without a TTY. `resolve_source_system()` now asks up front (before a
split too), always prints `Source system: ...` (or `NOT SET`), and the bib half
inherits the answer. A missing input path also gets a clean error/prompt.

**Status: built, 655 tests pass (39 in `tests/test_profiles.py`), flake8
(`--max-line-length=100`) clean on the new files; `marc_repair.py` still has its
51 pre-existing warnings, none new. Not yet tried on EC2 / real data.** Code:
`# Run profiles` section + `build_parser()` in `marc_repair.py`;
`marc_repair_profile.py`; samples in `profiles/examples/` (deliberately NOT in
`profiles/` itself -- shipped there they would trigger the multi-profile menu,
or the non-TTY error, for every existing user and test run); README section
"Using profiles"; `install.sh` now fetches the tool and the 3 samples.
Notes: `profiles/*.json` are user data (consider gitignoring if personal);
the wizard asks every option (q ends early).

**Split + profiles (fixed 2026-10-29):** `--split-bib-holdings` used to
recurse via `main([bib_path, "-o", ...])`, which only forwarded `-o` and would
have re-selected a profile and re-split forever under a profile that sets
`split_bib_holdings`. `main()` is now parse + profile selection, then
`run(parser, args)`; the bib half calls `run()` with a copy of the resolved
args (`split_bib_holdings` off, `log` reset, bare `--mrk`/`--sample-problems`).
Holdings half still forwards only its curated few switches -- extending it is
open if wanted.

Original goal: let users keep named sets of switches ("profiles") so the ~60 options do
not have to be retyped, since people use the tool with different philosophies.

Decisions (agreed with the user):
- JSON profiles; keys are long option names without dashes, using the positive
  meaning (`--no-fix-mojibake` -> `"fix_mojibake": false`). Input/output paths
  are never in a profile.
- Search `--profile-dir` if given and usable, else only `profiles/` next to the
  script. 0 profiles: behave as before. 1: print `Using profile: <name>` and
  continue. N: numbered interactive menu. Not a TTY: error unless `--profile`.
- Precedence: defaults < profile < explicit switches (`parser.set_defaults`).
  Unknown keys / wrong types are hard errors.
- Loading and selection live in `marc_repair.py` (argparse setup is pulled
  out of `main()` into `build_parser()`). Creating profiles (`init` wizard and
  `save` from switches) is a separate script, `marc_repair_profile.py`, that
  reuses `build_parser()`. Sample profiles ship in `profiles/`.

### DONE -- skip the exact oversized-record byte check for records that obviously fit (2026-10-29, on `main`: `49bbcc3`)

**Status: built, 656 tests pass, flake8 unchanged (51 pre-existing warnings,
none new), output confirmed byte-identical before/after. Small real-world
win (see profiling below), not the large one hoped for.**

Prompted by "script run time has increased" -- traced to
`record_exceeds_max_length()` (added by the trim-oversized-record work,
2026-10-29) re-encoding every field of every record to bytes just to check
a 99999-byte limit almost no record is near, duplicating work
`assemble_marc()` does right after. Fixed with a cheap char-count upper
bound computed straight from field structure (no `delimited()` string
build, no `encode()`) -- `len(f.content)` for control fields,
`len(indicators) + sum(2 + len(data) for subfields)` otherwise (2 =
subfield delimiter + code byte). 1 char is at most 4 UTF-8 bytes (latin-1:
exactly 1), so if that pessimistic bound already fits under
`MAX_RECORD_BYTES`, the real encoded total can only be smaller and the
exact per-field encode pass is skipped entirely. Only records that could
plausibly exceed the limit fall through to it. Code:
`record_exceeds_max_length` in `marc_repair.py` (~line 1847).

**Profiled against a real 20k-record sample of `working/bucknell_marc.mrc`
through `--profile mark-wms`** to see why the EC2 before/after times (776s
-> 722s, ~7%) were far short of expectations: the size check was only ever
~1% of total runtime. Core MARC parsing (`_read_intact_at` +
`parse_directory`, `marc_repair.py` ~line 818/447) is ~56% of total time
(38.6% self-time alone) -- the dominant cost by far, and it long predates
this session's work (corruption-recovery parsing: finds real `0x1E`
terminators rather than trusting the directory). All the repair passes
combined (mojibake fix, smart-character normalization, duplicate-field
handling, 010 dedupe, non-standard-field tally, assembly, etc.) are the
remaining ~43%. **Decided (2026-10-29): leave parsing alone for now** --
`_read_intact_at`/`parse_directory` are the most correctness-sensitive code
in the tool (handle corrupted directories, wrapped lengths) and a bigger
win there would need much more careful, higher-risk work than this fix.
Not investigated further: whether the "everything else" 43% has more
redundant-work patterns like the one just fixed.

### IN PROGRESS -- split `marc_repair.py` into modules ("cheap option"), branch `refactor/split-marc-repair` (started 2026-10-29)

Profiles are done, so the refactor deferred earlier is under way, as its own
branch (no feature work mixed in). Pure moves only: no logic edits.

**Status (update this block as each phase finishes):**

| Phase | What | Status |
|---|---|---|
| 0 Recon | dependency map, split plan | DONE 2026-10-29 (findings below) |
| 1 Constants / shared types | header constants + record types (+ `LogEntry`) -> `marc_repair_common.py` | DONE 2026-10-29 |
| (added) core module | detection/reconstruct/streaming/assembly/QA -> `marc_repair_core.py` (needed for an acyclic import graph, see Design) | DONE 2026-10-29 |
| 3 Bib patching rules | -> `marc_repair_patching.py` | DONE 2026-10-29 |
| 4+5+6 Holdings, reporting/log, `run`/`build_parser`/`main` | -> `marc_repair_cli.py` (+ `check_holdings_records` moved up) | DONE 2026-10-29 |
| 7 Closeout | tests, flake8, installer, docs, before/after output comparison, commit, push | DONE 2026-10-29 (results below); real EC2 data check still TODO |
| QA split | QA checks + identifier helpers + duplicate-identifier check out of core -> `marc_repair_qa.py` (sits BELOW core: core's MARC-8 code uses `_record_lccn`/`_record_oclc`) | DONE 2026-10-29: core 3,721 -> 3,272, qa 464; 659 tests, flake8, 244-file output comparison identical |
| 2 Pure helpers | MARC-8 fixers/transcoder out of core -> `marc_repair_marc8.py` | DONE 2026-10-29: core 3,272 -> 1,635, marc8 1,648; same checks identical |
| 5 Reporting/log | log tables, `write_log`, `ProgressReporter` out of cli -> `marc_repair_report.py` | DONE 2026-10-29: cli 3,909 -> 2,873, report 1,063; same checks identical |
| 8 Package folder | modules moved into `marc_repair_lib/` (user approved 2026-10-29, 4th commit) | DONE 2026-10-29 (see Final layout) |

The split was generated in one mechanical pass, so phases 1/core/3/4-6 landed
together; each was marked DONE only after the full suite and the before/after
comparison passed.

**Result (2026-10-29):** `marc_repair.py` 10,774 -> 188 lines (docstring + facade).
New modules: common 249, core 3,721, patching 3,020, cli 3,905 lines. 659/659
tests pass; `flake8 --select=F,E9` clean (style count unchanged apart from a
few E501 in the new module docstrings). Before/after comparison: all `.mrc`,
`.mrk`, logs and stdout byte-identical across 21 runs once timestamps/timings
are normalised, with one expected difference: a fixture that crashes with an
uncaught `RepairError` both before and after now shows new module paths in the
traceback and `marc_repair_common.RepairError` instead of `RepairError`.
Tests retargeted (setup only): `conftest.py` and `test_profiles.py` patch
`marc_repair_cli` (`DEFAULT_PROFILE_DIR`, `run`); two bib tests patch
`detect_encoding` in both `marc_repair_core` and `marc_repair_cli`.
`install.sh` `FILES`, README file table and `docs/EC2_UPDATE.md` list the new
modules.

**Final layout (2026-10-29, after the four follow-up commits)** -- `marc_repair.py`
(188-line facade/entry point) + package `marc_repair_lib/`, layered low to high,
each module importing only earlier ones (relative imports, `from .qa import ...`):

| Module | Lines | Holds |
|---|---|---|
| `common` | 249 | constants, `RepairError`, record types, `LogEntry` |
| `qa` | 464 | QA checks, LCCN/OCLC identifier helpers, duplicate-identifier check |
| `core` | 1,635 | record detection, Mode 1/2 reconstruction, dispatcher, streaming, assembly |
| `marc8` | 1,648 | MARC-8 (ANSEL) fixers, suspect-escape finders, MARC-8 -> UTF-8 transcoder |
| `patching` | 3,024 | content patching rules, non-standard-field tally |
| `report` | 1,063 | log category tables, `write_log`, `ProgressReporter` |
| `cli` | 2,873 | holdings repair, profiles, `run`, `build_parser`, `main` |

`qa` sits below `core` because the MARC-8 code (now `marc8`) uses
`_record_lccn`/`_record_oclc`. Data files (`profiles/`, tag lists,
`working/b2_lexicon.json`) are still found relative to the repo root: the six
`__file__`-based paths gained one extra `dirname`. Tests: `conftest.py`,
`test_profiles.py`, `test_marc_repair_bib.py` import `marc_repair_lib.cli` /
`.core` under their old alias names so the patch targets are unchanged.
`install.sh` `FILES` lists `marc_repair_lib/*.py`; an existing install keeps
the old flat module files (harmless, deletable; noted in `docs/EC2_UPDATE.md`).
Each commit was verified by 659 tests, `flake8 --select=F,E9`, and a 244-file
byte comparison (17 tracked fixtures x plain/`--split-bib-holdings`/
`--repair-holdings`; `/tmp/cmp.sh DIR`, baseline `/tmp/base`) -- the only
difference is `marc_repair_lib.common.RepairError` in the one crashing
fixture's traceback. The harness is in `/tmp` (lost on WSL restart); it runs
each tracked fixture copied into DIR with `--no-profile --source-system other`
and normalises timestamps, durations and paths. An early harness run wrote
~370 generated files into `tests/fixtures/` (git-ignored); they were moved
aside, the suite re-run (659 pass), and deleted (2026-10-29). The older
`ub_holdings_*` files there are not from this work and were left alone.

**Real-data check done locally (2026-10-29):** `working/WTS_bibs_2026-10-01.out`
(402 MB, 263,595 bibs) with `--profile mark-bib --source-system sierra`:
all records written in 100.6 s, 0 not fixed. The same command on the
pre-refactor code (commit `45ed37b`, in a temporary worktree) produced a
byte-identical `.mrc` and non-standard-fields file; the logs differ only in
the output filename and one check (`fixed_marc8_b2_umlaut`) listed under "no
findings", most likely because the temporary worktree lacked the untracked
`working/b2_lexicon.json` (not tested directly). Outputs kept in `working/`:
`WTS_bibs_2026-10-01_libsplit.mrc`, `..._libsplit_log_20261030T001759Z.log`,
`..._non_standard_fields.txt`, `..._libsplit.run.stdout`.

**Merge readiness (2026-10-29):** branch is 6 commits ahead of `main`, 0
behind (fast-forwardable), pushed, tree clean. The only unverified part is
`install.sh` fetching the new `marc_repair_lib/` files (syntax-checked only).

**Morning checklist (user will test):**
1. On the EC2 box, update with the installer against this branch:
   `install.sh --ref refactor/split-marc-repair` (the installer pins a commit
   SHA, so it can fetch these files without merging first). Confirm
   `marc_repair_lib/` (`__init__.py` + 7 modules) is created in the install dir.
2. Run one real file with the venv wrapper and compare to a previous run's
   output (`.mrc` byte-identical expected; log differs only in timestamps and
   filenames).
3. If it works: merge `refactor/split-marc-repair` into `main` (fast-forward)
   and push. If `install.sh` misses a file, fix `FILES` and re-test.
4. Optionally delete the old flat `marc_repair_*.py` files an existing install
   keeps (harmless; see `docs/EC2_UPDATE.md`).

**Not yet done:** run on real EC2 data (the installer is untested against a
real pinned install: after merging, update the EC2 box with the installer and
confirm a run matches the previous output). Merge to `main` is the user's call.

**Recon findings (Phase 0)**
- File was 10,774 lines, ~196 top-level defs, ~134 module constants scattered
  through every section (only 7 in the header). No `global` statements. The
  `# ----` section banners mislead: the "Run profiles" banner is really
  `run()` (1,010 lines) + `build_parser()` (680 lines) + `main`; the "CLI"
  banner holds `repair_holdings_records`, `write_log`, `ProgressReporter`;
  "Content patching" is the 78-def bib-rules block.
- Layering is clean: common <- core <- patching <- cli. Only two real
  back-edges: `LogEntry` (a dataclass in the CLI section) is used by core and
  patching -> moved DOWN into common; `check_holdings_records` (core/streaming
  section) calls `write_log` -> moved UP into cli. Other reported back-edges
  were a local variable named `run`.
- Module-level code that must travel with its block: the for-loops/`del _cat`
  that fill `_DEDICATED_SECTIONS` (with the log code, cli).
- Data files are located via `__file__`; every new module is a sibling of
  them, so those paths are unchanged.

**Design**
- Why `core` is its own module: with only patching and cli extracted, they
  would have to `import marc_repair` while `marc_repair.py` re-exports them (a
  cycle), and running `python marc_repair.py` loads it as `__main__`, so the
  import would load a *second copy* (duplicate classes). So `marc_repair.py`
  is now a thin entry point/facade and all code is in sibling modules that
  never import it.
- `marc_repair.py` keeps the module docstring (HOW TO USE), imports `main`,
  re-exports every public and private name of the four modules (a loop over
  `vars()`), and keeps the `__main__` guard. `import marc_repair as m`,
  `python marc_repair.py`, the venv wrapper and the EC2 install all still work.
- Test consequence: `monkeypatch.setattr(m, "X", ...)` on the facade no longer
  reaches code in another module. Three tests patch names (`m.detect_encoding`
  x2, `m.run`); retarget them to the module that uses the name (`main` and
  `run` live in `marc_repair_cli`). Test-setup change only.
- `install.sh` `FILES` and the file lists in README / `docs/EC2_UPDATE.md` /
  this file must gain the four new modules, or an installed copy breaks.

**Safety net**
- Baseline before the refactor: 659 tests pass; `flake8 --select=F,E9` clean
  (the ~655 style warnings are pre-existing).
- Before/after output comparison: `/tmp/cmp.sh DIR` (WSL) runs the tool over 7
  fixtures x {plain, `--split-bib-holdings`, `--repair-holdings`} with
  `--no-profile --source-system other`, normalising timestamps; baseline output
  of the original code is in `/tmp/base` (regenerate from `main` if /tmp was
  cleared). Compare `out.mrc`, `out.mrk`, `stdout.txt`, logs byte for byte.
- Generation scripts (recon, dependency, split) were throwaway; the split is
  reproducible from the rules above (AST statement chunks, per-block import
  lists derived from pyflakes undefined-name reports).

**Estimates:** the full split was ~500-750k tokens over 7 phases; this "cheap
option" ~250-400k. Doing the rest later costs ~10-15% more than one pass.

**Follow-ups done (2026-10-29):** Phase 2 (MARC-8 helpers out of core), Phase 5
(reporting/log out of cli), the QA split, and the `marc_repair_lib/` folder --
four more pure-move commits on this branch. Possible further work: `cli` is
still 2,873 lines (`run` ~1,000, `build_parser` ~680, holdings repair ~600,
profiles ~180) and `patching` 3,024; both could be cut further the same way.

### DONE -- trim records over 99999 bytes instead of diverting them (2026-10-29, on `main`)

**Status: built, 616 tests pass, committed and pushed. Not yet run against a real corpus.** Code: `record_exceeds_max_length`,
`trim_oversized_record` (before `assemble_marc` in `marc_repair.py`), wired into
bib `main()` and `repair_holdings_records(trim_oversized_record_enabled=)`,
switch `--trim-oversized-record` / `--no-trim-oversized-record` (BooleanOptionalAction, default on), log category `trimmed_oversized_record`
(DATA LOSS). Fixtures `ks-toobig-trim` (mid-file, after `ks-oversized-ok`) and
`ks-toobig-notrimmable`; tests in `tests/test_oversized_record.py`; fixture
outputs and `docs/REPAIR_CATEGORIES.md` regenerated. The text below is the
original plan (kept for reference).

**Today:** a bib/holdings record whose assembled size is over 99999 bytes is
diverted to `_error.mrc` (log category `unfixable`) unless
`--allow-oversized-sentinel` keeps it with the leader sentinel `99999`
(`oversized_sentinel_fixed`). Both pipelines do this: bib in `main()`
(~line 10081) and holdings in `repair_holdings_records()` (~line 8600).
`assemble_marc()` (~line 1837) only *builds* the record; it never shrinks.
The existing fixture `ks-oversized-ok` (12 x 9000-byte 5xx) exercises the
divert path; its test must keep passing under `--no-trim-oversized-record`.

**Goal:** shrink such a record under the limit by removing whole fields,
log every removal, so it can go to the main output.

**1. Fixture** (`tests/fixtures/build_kitchen_sink.py`, `build_bib_records()`)
- New record `ks-toobig-trim`, inserted in the *middle* of the list (right
  after `ks-oversized-ok`, ~line 616), not appended.
- Contents: 001, 008, 245, a `999 ff $a` (must survive), a 590 (the run's
  `--ensure-field` target, same reason as `ks-oversized-ok`), and ~12 5xx
  fields of *different* sizes (e.g. 520 = 9500, 505 = 9000, 500 = 8000,
  504 = 3000, 586 = 200, ...), total ~110k, so removing the largest few is
  enough and the *removal order* is observable.
- Second small record `ks-toobig-notrimmable` (999 ff + non-5xx bulk only,
  e.g. many 650/700) to exercise the "5xx isn't enough" branch.
- Regenerate `kitchen_sink_bib.mrc`, `.log`, `_repaired.mrc`; update the
  id lists in `test_marc_repair_core.py` (`_GUARANTEE_IMPORTABLE_CASES`,
  ~line 907) and any record-count assertions.

**2. Detection** (test + log, independent of the fix)
- New helper `record_exceeds_max_length(parsed) -> int | None` (assembled
  size when over 99999, else None), used by both pipelines in place of the
  inline `len(assembled) > 99999` checks.
- `tests/test_oversized_record.py` (new): builds records in memory and
  asserts the helper flags exactly the over-limit ones (99999 ok, 100000 not).
- Fixture-level test: the default pipeline run on the kitchen sink logs
  `ks-toobig-trim` under the new category.

**3. Fix** -- new `trim_oversized_record(parsed, max_bytes=99999)` in
`marc_repair.py`, called just before assembly in both pipelines (after the
9999-byte per-field collapse/truncate and after `--ensure-field` and other
additions, so the measured size is what would actually be written):
1. Measure the assembled size; if <= 99999 do nothing.
2. Protected, never removed: leader, 001/003/005/008, 245, and any `999`
   with indicators `ff` (FOLIO's SRS/instance link; removing it breaks the
   record). A 999 with other indicators is NOT protected.
3. Pass A: remove 5xx fields largest first (encoded bytes incl. terminator),
   ties broken later-in-record first, re-measure after each removal, stop as
   soon as it fits. A linked 880 (`$6` pointing at the removed 5xx) goes
   with it so no orphan is left.
4. Pass B (only if all removable 5xx are gone and it still doesn't fit):
   remove the largest remaining non-protected fields, same rule.
5. If still too big (protected fields alone > 99999): leave untouched and
   fall back to today's behavior (divert to `_error`, `unfixable`).
6. Log category `trimmed_oversized_record` (DATA LOSS, FIXED/REQUIRES
   ATTENTION): one entry per record listing each removed field as
   `=TAG (N bytes) "first 40 chars..."` plus size before -> after. Add a
   `ParsedRecord.trimmed_oversized_record` list (like
   `truncated_oversized_fields`) and register the category in
   `docs/REPAIR_CATEGORIES.md` via `tools/generate_repair_categories_doc.py`
   (bib and holdings tables).
7. New flag `--no-trim-oversized-record` restores the divert/sentinel
   behavior; default is to trim. `--allow-oversized-sentinel` then only
   matters for records trimming couldn't fix.

**4. Tests** (`tests/test_oversized_record.py`)
- removes largest 5xx first, in order, stops once under limit (assert exact
  removed tags and final size <= 99999);
- `999 ff` survives even when it is the largest field; other 999 can go;
- Pass B only after 5xx are exhausted; unfixable fallback when protected
  fields alone are too big;
- log lists every removed field; nothing logged for a record that fits;
- holdings pipeline path; `--no-trim-oversized-record` restores diversion;
- update `TestGuaranteeImportable` expectations for `ks-oversized-ok` (now
  trimmed into the main output, not `error`) and add the new fixture ids.

**5. Verify:** full pytest, flake8 `--max-line-length=100`, regenerate the
kitchen-sink outputs, confirm `ks-toobig-trim` repaired size <= 99999 and its
999 ff is intact.

**As built (user's answers changed the plan):** protected set = 001/003/005/008/245
(only 9xx/5xx/6xx are ever candidates), linked 880 goes with its 5xx and 6xx,
default on, `ks-oversized-ok` kept as the divert witness (its tests pass
`--no-trim-oversized-record`). Removal order is **9xx (not 999 ff) -> 5xx
(+880) -> 6xx(+880)**, largest first in each group, tie -> later in record
first; there is no "Pass B" over other tags (7xx etc. never touched). If even
removing every candidate won't fit, the record is left unchanged and diverted
as `unfixable`. Note `assemble_marc` also refuses a record whose last field
*starts* past offset 99999, so the fixtures must keep the last field's start
below that.

### DONE -- `dedupe_010_fields` moves different LCCNs to `$z`; valid LCCN always wins `$a` (2026-10-26, uncommitted on `main`)

**Status: built and tested (607 pass), `docs/REPAIR_CATEGORIES.md` regenerated;
not yet committed.** `deduped_010_lccn` used to merge several 010s by keeping one
`$a` and discarding the others (the log still said "merged"). Now:

- Every non-surviving `$a` is moved to `$z` (after existing `$z/$b`, de-duplicated
  against them), so nothing is lost. Category label is NO DATA LOSS; entries
  read "kept $a..., moved $a... to $z (different LCCN, status unverified) -- all
  values retained". Whitespace-only merges are still collapsed into a count line;
  moved-value records are still listed in full. The category header carries a
  caveat: treating the other LCCNs as canceled/invalid (`$z`) is a guess and
  should be verified.
- Which value keeps `$a` (`_010_a_sort_key`): a **valid LCCN always beats
  anything else**, however new or big the other looks; among valid ones the
  newest year, then the highest serial. Validity (`_lccn_year_serial`,
  `is_valid_lccn`) follows https://www.loc.gov/marc/lccn_structure.html:
  - pre-2001: 0-3 letter prefix (lowercase, from the page's bibliographic +
    authority/classification list), 2-digit year, 6-digit serial, optional
    `/SUFFIX` (any uppercase letters accepted -- only a partial identifier list
    was available) and `//r74`-style revision. 2-digit year's century comes from
    the page's serial-number table (98/99/00).
  - 2001+: prefix of at most 2 letters, only authority/classification
    (`n nb nr no sh sj sp cf ct`) -- the page lists bibliographic prefixes for
    the old structure only; 4-digit year >= 2001, 6-digit serial, no suffix.
  - Strict: uppercase prefix and a stored hyphen are invalid. Stored values are
    never rewritten by the check (a `//r96` suffix is kept).
- Tests: `Test010ASortKey` (new) plus updated `TestDedupe010Fields`.
- GTU (`working/GTU_bibs.mrc`, 404,956 recs; 207,301 with a 010): only 9 records
  have more than one 010/`$a`. The four the user checked (b10542450, b12348879,
  b13132258, b13133056) merge as: 31022135 (z 02002135); 09002724 (z 02003919);
  27021481 (z AFX34000080 -- the only one where the choice depends on validity);
  29021407 (z 14003305).

### OUT OF SCOPE (for now) -- flag invalid 010 `$a` values on any record (2026-10-26)

The repair above only touches records with more than one `$a`. Under the strict
LC rules, **1,379 GTU records (0.67% of those with a 010)** carry at least one
010 `$a` that is not a valid LCCN, and nothing flags them. Breakdown:

| Count | Why invalid | Examples |
|---|---|---|
| 1,351 | right shape, prefix/suffix not allowed | `gb` 734, `cn` 403, `ce` 12, `miu` 8; bad revision suffix (`//r` with no year, `/rev`) ~140 |
| 11 | uppercase letters in prefix | `A54009040`, `Rc00000850` |
| 10 | wrong digit count | `525159`, `7978670` |
| 7 | not LCCN-shaped | `w231`, `5029559rev`, `ic`, `.40010711` |

About 1,140 of these are `gb` (British Library) and `cn` (Canadian) control
numbers that look like real national-library numbers but are not on LC's prefix
list. **Open question for the user:** treat those as invalid, or exempt them?
Idea if picked up: a new log category (INFORMATIONAL or FIXED/REQUIRES ATTENTION)
listing records with an invalid 010 `$a`, reusing `is_valid_lccn`; no data would
be changed. Not started.

### DONE -- move duplicate 100 -> 700 and clean duplicate 245 -> 246 (2026-10-26, uncommitted on `main`)

**Status: built, tested (567 pass), flake8 clean on the touched files; not yet
committed.** A second+ 100/245 used to be deleted by
`strip_duplicate_non_repeatable_fields`; now `move_duplicate_100_to_700` and
`move_duplicate_245_to_246` run just before it (default on; opt-out
`--no-move-duplicate-100-to-700` / `--no-move-duplicate-245-to-246`). Anything
that doesn't qualify still falls through to the old removal. One log category,
`moved_duplicate_to_added_entry` (FIXED/REQUIRES ATTENTION, always listed in
full, category label POSSIBLE DATA LOSS; each entry says NO/POSSIBLE).

- 100 -> 700: any extra 100 with a real `$a` that isn't an exact copy. `$6`
  dropped (flagged), trailing "(editor)"-style role moved to `$e`, indicator 1
  kept if 0/1/3 else guessed from a comma, indicator 2 blank; already-present
  700 -> just dropped.
- 245 -> 246 (`3 `): only when the extra has a real `$a`, no `$c` (a statement
  of responsibility means a separate title/volume), no `$6`, isn't an edition
  statement ("Abridged ed" -> 250), doesn't start with a stray "0 ", AND the
  first 245 has no `$l/$v/$t/$6` (first is itself a mistagged uniform title or
  series). Keeps `$a $b $f $g $h $i $n $p $5 $6 $8`; drops the rest, flagged.
- GTU (`working/GTU_bibs.mrc`, 404,956 recs): 24 instances in 20 records moved
  (14 x 100, 10 x 245); 12 extra 245s left for removal, 0 100s. Matches the
  pre-build estimate exactly.
- Tests: `TestMoveDuplicate100To700`, `TestMoveDuplicate245To246`,
  `TestMoveDuplicatePipeline`. Docs: README table row and
  `docs/REPAIR_CATEGORIES.md` (regenerated via the tool).

### DONE -- phase 2: the other duplicate 245s, duplicate 240s, duplicate 040s (2026-10-26, uncommitted on `main`)

**Status: built and tested (new test classes `TestMergeDuplicate040`,
`TestMoveDuplicate240To730`, `TestRecodeMisfiledDuplicate245`,
`TestMisfiledDuplicatePipeline`), docs regenerated; not yet committed.** All
default on; same flag style as above.

- `recode_misfiled_duplicate_245` (`--no-recode-misfiled-duplicate-245`), runs
  after the clean 245 -> 246 move. Per extra 245, first match wins:
  near-duplicate of the kept 245 (same non-empty `$b`+`$c`, or 40-char common
  prefix, or >= 0.8 similarity on ASCII letters/digits; a leaked leading "0 " is
  ignored) -> dropped, NO DATA LOSS; first 245 is a mistagged uniform title
  (`$l`) or series (`$v`) with no `$c` and the extra has `$a`+`$c` -> extra
  takes the first 245's place, first becomes 240 (730 if the record has a
  130/240) or 490 `0 ` (dropped if that series is already on the record);
  publication statement (`$a` ends ":", `$b`, year-only `$c`) -> 260; edition
  statement -> 250 (only if none, else dropped when identical); `$n/$p`-only or
  `$a`+own `$c` -> 740 `x2` (analytical; `$c` dropped and flagged unless it
  equals the first's).
- `move_duplicate_240_to_730` (`--no-move-duplicate-240-to-730`): extra 240 ->
  730 (indicator 1 = the 240's nonfiling count, only if it ends at a word
  boundary), or 246 `30` when its text is a part of the 245 title. Frequency
  statements ("two no. a year") are left for the generic removal.
- `merge_duplicate_040` (`--no-merge-duplicate-040`, category
  `merged_duplicate_040`): 040 IS Not Repeatable in MARC21, but the later ones
  were mostly a modifying agency's `$d` or the `$c` the first lacked, so they are
  merged (`$a $b $c $d $e` order; conflicting `$a`/`$c` -> `$d`; `$d` equal to
  `$a`/`$c` skipped). Mistagged 041s (`$h`, or `$a` of lowercase 3-letter language
  codes) become a 041 if the record has none; 040s holding a call number/ISBN
  are dropped (flagged). Exact copies still go through the generic removal.
- GTU (`working/GTU_bibs.mrc`): all 28 duplicate 040s merged (5 flagged POSSIBLE
  DATA LOSS: 3 non-agency drops, 2 demotions of `$a` to `$d`), every duplicate
  245/240 handled except `b11571408`.

### DONE -- 003 resolver for duplicate 003s (2026-10-26, on `main`: `e259f80`)

**Status: built, tested (599 pass), flake8 clean, committed and pushed.**
`_choose_kept_003` (in `_DUPLICATE_FIELD_RESOLVERS`) keeps the 003 that looks like
an organization code (letters/digits/hyphens, starts with a letter, a capital
preferred) over junk; falls back to the first. GTU: 11 records had a junk 003
(an OCLC-style number, `gsto`, a stray note) ahead of the real `OCoLC`, so the old
first-wins rule kept the junk. The removal count is unchanged (the junk is still the
one removed, logged POSSIBLE DATA LOSS although the OCLC number is already in each
record's 035); only which 003 survives changed. Tests: `TestChooseKept003`.
GTU non-exact duplicate-field removals are now 15 (was 83): 11 x 003, 2 x 008,
1 x 005, 1 x 240 (`b11571408`).

### OPEN (minor) -- leftovers after phase 2 (2026-10-26)
- `b11571408`: both 240s are really frequency statements ("three no. a year", "two no.
  a year from 1998") -> should be a 310/321 and the 240s dropped. Only one record in GTU; the
  second 240 is still removed by the generic pass.
- `b13582938` (ISBN in `$a`), `b13733989` and `b14152538` (call numbers in `$a`/`$b`)
  have their junk 040 dropped; the call number probably belongs in a 050 and the ISBN in a
  020. Not recoded.
- 740 `x2` for a further volume drops that title's own `$c` when it differs from the first
  245's (`b16436428`, `b13947904` vol. 2): flagged POSSIBLE DATA LOSS in the log.

### DONE -- trimmed log, `--log-zero` to restore zero-count headers (2026-10-26)

**Status: built, tested (545 pass), flake8 clean, committed and pushed (`d4bf4ed`).**
`write_log(include_zero=False)` drops header blocks with no findings and
lists them once at the end under `=== CHECKS PERFORMED WITH NO FINDINGS (N) ===`
(section: category -- first sentence of its description; only checks that
actually ran). `--log-zero` / `include_zero=True` restores the old output;
`--sample-log` always passes True. Existing tests that assert on zero-count
headers now pass `--log-zero`; `TestWriteLogTrimmed` covers the new default.
GTU (`working/GTU_bibs.mrc`): 41 categories = 19 in body + 22 in footer,
identical to the `--log-zero` set. The saving on GTU is small (3,701 -> 3,553
lines) because per-record entries, not empty headers, dominate it.

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

### DONE -- import-safety defaults + `--guarantee-importable` (2026-10-19, merged to `main`: `4f41993`..`919747c`; branch deleted 2026-10-29)

**Status: implemented, tested, and empirically verified end-to-end against
live FOLIO Data Import. Already merged to `main` (confirmed 2026-10-29 --
the branch no longer existed locally or on origin; the `pre-guarantee-importable`
tag marks the point before this work landed, and all 6 commits are reachable
from `main`). This entry was previously stale, marking it OPEN after the
merge had already happened.**

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
as of the last commit of this work.

**Where to pick this up:**
1. If a future FOLIO test ever turns up another category that genuinely
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
