# Updating marc_repair on EC2

marc_repair on EC2 is installed with `install.sh`, not a git clone. There is
no `.git` directory, so `git pull` and `git status` don't apply.

Install dir: `/working/migration/scripts/marc_repair`

## Check whether an update is available (changes nothing)

```bash
curl -fsSL https://raw.githubusercontent.com/marnold-ebsco/marc-repair/main/install.sh | bash -s -- --dir /working/migration/scripts/marc_repair --check
```

## Update to the latest commit on main

```bash
curl -fsSL https://raw.githubusercontent.com/marnold-ebsco/marc-repair/main/install.sh | bash -s -- --dir /working/migration/scripts/marc_repair
```

## Notes

- It re-downloads `marc_repair.py`, its `marc_repair_lib/` package (`__init__.py`, `common.py`, `qa.py`,
  `core.py`, `marc8.py`, `patching.py`, `report.py`, `cli.py`),
  `marc_repair_profile.py`, `pyproject.toml`,
  `requirements.txt`, the tag lists, the profiles (`profiles/mark-*.json` and
  `profiles/examples/`) and the docs, pinned to one commit SHA. Local edits to
  those files are overwritten, so change profiles in the repo, not on the box.
  A new profile only installs once it is added to `FILES` in `install.sh`.
- Moving to the `marc_repair_lib/` package leaves the old flat module files
  (`marc_repair_common.py`, `marc_repair_qa.py`, `marc_repair_core.py`,
  `marc_repair_marc8.py`, `marc_repair_patching.py`, `marc_repair_report.py`,
  `marc_repair_cli.py`) behind in the install directory. Nothing imports them
  any more, so they are harmless, but they can be deleted.
- Every run's log starts with a `Script version:` line (commit SHA, the
  commit's UTC date and the ref), so you can tell which version made it:
  `Script version: 1125957abc12 (committed 2026-10-30T12:00:00Z, ref main)`.
  On EC2 it comes from `.marc_repair_install_info`, written by `install.sh`
  (compare the date with the latest commit on GitHub). An install made by an
  older installer has only the SHA marker, so its logs show the SHA and
  "commit date unknown" until the next update. In a git checkout it is read
  from git and gains ` + uncommitted changes` when tracked files are modified.
- The existing venv is left alone unless the interpreter changes.
- Other flags: `--ref <branch|tag>`, `--recreate-venv`,
  `--interpreter cpython|pypy`.
- The installed version is recorded in `.marc_repair_install_version`.
- Run it with `venv/bin/python marc_repair.py`.

## Profiles after an update

See [Using profiles](../README.md#using-profiles) for the full description.

Check the profiles arrived (expect `mark-bib`, `mark-holdings`, `mark-wms`):

```bash
/working/migration/scripts/marc_repair/venv/bin/python /working/migration/scripts/marc_repair/marc_repair.py --list-profiles
```

- With several profiles, a run from a terminal (SSH, `screen`, `tmux`) shows a
  menu. Without a terminal it stops with an error rather than guess: this
  includes cron, piped input, and **`nohup ... &`** (nohup detaches stdin).
  Pass `--profile NAME` (or `--no-profile`) in those cases, e.g.
  `nohup marc_repair --profile mark-wms big.mrc &`.
- `--profile mark-wms` splits a mixed file into bibs and holdings and repairs
  both; `mark-bib` / `mark-holdings` are for files that are already split.
