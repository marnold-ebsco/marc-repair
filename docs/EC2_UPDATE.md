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

- It re-downloads `marc_repair.py`, `pyproject.toml`, `requirements.txt`, the
  tag lists and the docs, pinned to one commit SHA. Local edits to those files
  are overwritten.
- The existing venv is left alone unless the interpreter changes.
- Other flags: `--ref <branch|tag>`, `--recreate-venv`,
  `--interpreter cpython|pypy`.
- The installed version is recorded in `.marc_repair_install_version`.
- Run it with `venv/bin/python marc_repair.py`.
