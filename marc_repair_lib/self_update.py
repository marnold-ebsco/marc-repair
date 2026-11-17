"""Update check (and optional self-update) for an install.sh installation.

An install.sh install has no `.git`; it records the commit it was built from
in `.marc_repair_install_info` / `.marc_repair_install_version` next to
marc_repair.py. At the start of a run this module compares that commit with
the latest on GitHub:

  * default: print a one-line notice when a newer commit exists (never an
    error -- no network, a git checkout or an unknown install just stays
    silent);
  * `--self-update`: fetch install.sh at the newest commit, run it against
    this install, then re-launch the same command line with the new code
    (a running Python process can't safely swap its own modules, so it
    restarts rather than carrying on).

A git checkout is never touched: update it with git.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import urllib.request

REPO = "marnold-ebsco/marc-repair"
#: Set in the re-launched process so a failed update can't loop forever.
GUARD_ENV = "MARC_REPAIR_SELF_UPDATED"
_TIMEOUT = 5


def base_dir() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def installed_commit(base: str) -> tuple[str, str]:
    """(sha, ref) recorded by install.sh; ("", "main") when there is no record."""
    info: dict[str, str] = {}
    for line in _read(os.path.join(base, ".marc_repair_install_info")).splitlines():
        key, sep, value = line.partition("=")
        if sep:
            info[key.strip()] = value.strip()
    sha = info.get("sha") or _read(os.path.join(base, ".marc_repair_install_version")).strip()
    return sha, info.get("ref") or "main"


def latest_commit(ref: str) -> str | None:
    """Newest commit SHA of `ref` on GitHub, or None when it can't be reached."""
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{REPO}/commits/{ref}",
            headers={"Accept": "application/vnd.github+json", "User-Agent": "marc_repair"},
        )
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return json.load(resp)["sha"]
    except Exception:  # noqa: BLE001 - offline, rate-limited, bad JSON: all "unknown"
        return None


def check(base: str | None = None) -> tuple[str, str, str] | None:
    """(installed sha, latest sha, ref) when a newer commit exists, else None."""
    base = base or base_dir()
    if os.path.exists(os.path.join(base, ".git")):
        return None
    local, ref = installed_commit(base)
    if not local:
        return None
    remote = latest_commit(ref)
    if remote and remote != local:
        return local, remote, ref
    return None


def run_installer(base: str, remote_sha: str, ref: str) -> bool:
    """Download install.sh at `remote_sha` and run it against `base`."""
    url = f"https://raw.githubusercontent.com/{REPO}/{remote_sha}/install.sh"
    tmp = None
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            script = resp.read()
        with tempfile.NamedTemporaryFile("wb", suffix=".sh", delete=False) as fh:
            fh.write(script)
            tmp = fh.name
        cmd = ["bash", tmp, "--dir", base, "--ref", ref]
        interp = _read(os.path.join(base, ".marc_repair_interpreter")).strip()
        if interp in ("cpython", "pypy"):
            cmd += ["--interpreter", interp]
        return subprocess.run(cmd, check=False).returncode == 0
    except Exception as exc:  # noqa: BLE001
        print(f"self-update failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return False
    finally:
        if tmp and os.path.exists(tmp):
            os.remove(tmp)


def handle(args, argv: list[str] | None) -> None:
    """Run at startup. Prints the notice, or updates and re-launches (never returns
    in that case). Any problem is a warning and the run continues as-is."""
    if os.environ.get(GUARD_ENV):
        return
    want_update = getattr(args, "self_update", False)
    if not (want_update or getattr(args, "update_check", True)):
        return
    base = base_dir()
    if os.path.exists(os.path.join(base, ".git")):
        if want_update:
            print("--self-update: this is a git checkout; update it with git pull",
                  file=sys.stderr)
        return
    found = check(base)
    if found is None:
        if want_update:
            print("--self-update: already up to date (or GitHub unreachable)", file=sys.stderr)
        return
    local, remote, ref = found
    if not want_update:
        print(
            f"update available: {local[:12]} -> {remote[:12]} ({ref}); "
            "re-run with --self-update to install it and continue",
            file=sys.stderr,
        )
        return
    print(f"updating {local[:12]} -> {remote[:12]} ...", file=sys.stderr)
    if not run_installer(base, remote, ref):
        print("self-update failed; continuing with the installed version", file=sys.stderr)
        return
    rest = [a for a in (sys.argv[1:] if argv is None else argv) if a != "--self-update"]
    os.environ[GUARD_ENV] = "1"
    print("update installed; restarting the run", file=sys.stderr)
    sys.stderr.flush()
    os.execv(sys.executable, [sys.executable, os.path.join(base, "marc_repair.py"), *rest])
