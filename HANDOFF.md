# Handoff Notes

For closed-out work from prior sessions (the full `fix_marc8_diacritic_escapes`
project, the 010/880 missing-`$a` categories, the `parse_directory` phantom-entry
fix, log-formatting history, etc.), see
[docs/HANDOFF_HISTORY.md](docs/HANDOFF_HISTORY.md). This file only carries the
current state and open items.

## Current state

`marc8-diacritic-fix` was merged into `main` via
[marc-repair#1](https://github.com/marnold-ebsco/marc-repair/pull/1) and the
branch deleted (local + remote). Nothing is in progress.

Since the merge, `marc_repair.py` (tip of `main`) was run against two
previously-untried corpora as a sanity check, both clean -- no new patterns,
no code changes needed:

- `sample_files/nashvillestate_bibs_202693.mrc` (48,017 records): 48,017/48,017
  written, 0 unfixable. 4 `fixed_marc8_diacritic`, 3 `suspect_marc8_escape`,
  all already-familiar shapes.
- `sample_files/Bucknell00000448.mrc` (1,000 records): 1,000/1,000 written, 0
  unfixable. Both MARC-8 diacritic categories are 0 records -- expected, this
  corpus is already declared UTF-8 (leader byte 9 == `'a'`).

**Nothing further queued from the user as of this entry.**

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
