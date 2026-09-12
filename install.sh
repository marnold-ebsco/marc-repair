#!/usr/bin/env bash
# install.sh -- fetch and set up marc_repair without cloning the full repo.
#
# Downloads only the files marc_repair.py actually needs to run (plus
# README/docs), pinned to one commit SHA, and creates a ready-to-use venv.
# Safe to re-run: with no flags it checks for a newer commit and updates
# in place (leaving the venv alone unless the interpreter choice changes
# or --recreate-venv is passed).
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/marnold-ebsco/marc-repair/main/install.sh | bash -s -- [options]
#   ./install.sh [options]
#
# Options:
#   --dir PATH           Install location (default: ./marc_repair)
#   --interpreter NAME   cpython (default) or pypy
#   --ref REF            Branch or tag to install from (default: main)
#   --recreate-venv      Delete and rebuild the venv even if one exists
#   --check              Only report whether an update is available; change nothing
#   -h, --help           Show this help

set -euo pipefail

REPO="marnold-ebsco/marc-repair"
INSTALL_DIR="./marc_repair"
INTERPRETER="cpython"
REF="main"
RECREATE_VENV=0
CHECK_ONLY=0

# Files needed to run marc_repair.py, relative to repo root. Deliberately
# excludes tests/, fixtures, and generated data -- see docs/REPAIR_CATEGORIES.md
# in the repo for what those are.
FILES=(
  "marc_repair.py"
  "requirements.txt"
  "required_a_tags.txt"
  "non_repeatable_tags.txt"
  "README.md"
  "docs/REPAIR_CATEGORIES.md"
)

VERSION_MARKER=".marc_repair_install_version"

usage() {
  sed -n '/^# Usage:/,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir) INSTALL_DIR="$2"; shift 2 ;;
    --interpreter) INTERPRETER="$2"; shift 2 ;;
    --ref) REF="$2"; shift 2 ;;
    --recreate-venv) RECREATE_VENV=1; shift ;;
    --check) CHECK_ONLY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 1 ;;
  esac
done

if [[ "$INTERPRETER" != "cpython" && "$INTERPRETER" != "pypy" ]]; then
  echo "Error: --interpreter must be 'cpython' or 'pypy', got '$INTERPRETER'" >&2
  exit 1
fi

need() {
  command -v "$1" >/dev/null 2>&1 || { echo "Error: '$1' is required but not found on PATH." >&2; exit 1; }
}
need curl
need python3

resolve_sha() {
  curl -fsSL "https://api.github.com/repos/${REPO}/commits/${REF}" \
    | python3 -c "import json,sys; print(json.load(sys.stdin)['sha'])"
}

echo "Resolving latest commit for ${REPO}@${REF}..."
REMOTE_SHA="$(resolve_sha)"
echo "Latest commit: ${REMOTE_SHA}"

LOCAL_SHA=""
if [[ -f "${INSTALL_DIR}/${VERSION_MARKER}" ]]; then
  LOCAL_SHA="$(cat "${INSTALL_DIR}/${VERSION_MARKER}")"
fi

if [[ -n "$LOCAL_SHA" && "$LOCAL_SHA" == "$REMOTE_SHA" ]]; then
  echo "Already up to date (${LOCAL_SHA})."
  if [[ "$CHECK_ONLY" -eq 1 ]]; then exit 0; fi
elif [[ -n "$LOCAL_SHA" ]]; then
  echo "Update available: ${LOCAL_SHA} -> ${REMOTE_SHA}"
  if [[ "$CHECK_ONLY" -eq 1 ]]; then exit 0; fi
else
  echo "No existing install found at ${INSTALL_DIR}; installing fresh."
  if [[ "$CHECK_ONLY" -eq 1 ]]; then exit 0; fi
fi

mkdir -p "${INSTALL_DIR}/docs"

echo "Fetching files pinned to ${REMOTE_SHA}..."
for f in "${FILES[@]}"; do
  url="https://raw.githubusercontent.com/${REPO}/${REMOTE_SHA}/${f}"
  dest="${INSTALL_DIR}/${f}"
  mkdir -p "$(dirname "$dest")"
  curl -fsSL "$url" -o "$dest"
  echo "  ${f}"
done

echo "${REMOTE_SHA}" > "${INSTALL_DIR}/${VERSION_MARKER}"

VENV_DIR="${INSTALL_DIR}/venv"
INTERPRETER_MARKER="${INSTALL_DIR}/.marc_repair_interpreter"
PREV_INTERPRETER=""
[[ -f "$INTERPRETER_MARKER" ]] && PREV_INTERPRETER="$(cat "$INTERPRETER_MARKER")"

if [[ "$PREV_INTERPRETER" != "$INTERPRETER" && -n "$PREV_INTERPRETER" ]]; then
  echo "Interpreter changed (${PREV_INTERPRETER} -> ${INTERPRETER}); rebuilding venv."
  RECREATE_VENV=1
fi

if [[ "$RECREATE_VENV" -eq 1 && -d "$VENV_DIR" ]]; then
  rm -rf "$VENV_DIR"
fi

if [[ ! -d "$VENV_DIR" ]]; then
  if [[ "$INTERPRETER" == "pypy" ]]; then
    need pypy3
    echo "Creating PyPy venv at ${VENV_DIR}..."
    pypy3 -m venv "$VENV_DIR"
  else
    need python3
    echo "Creating CPython venv at ${VENV_DIR}..."
    python3 -m venv "$VENV_DIR"
  fi
else
  echo "Reusing existing venv at ${VENV_DIR}."
fi

echo "${INTERPRETER}" > "$INTERPRETER_MARKER"

echo "Installing dependencies..."
"${VENV_DIR}/bin/pip" install --quiet --upgrade pip
"${VENV_DIR}/bin/pip" install --quiet -r "${INSTALL_DIR}/requirements.txt"

# $0 is "bash" when run via `curl | bash -s --`, so it's not a usable
# path to re-invoke -- fall back to re-fetching via curl in that case.
case "$0" in
  *install.sh) RERUN_CMD="$0" ;;
  *) RERUN_CMD="curl -fsSL https://raw.githubusercontent.com/${REPO}/main/install.sh | bash -s --" ;;
esac

cat <<EOF

Done. marc_repair (${INTERPRETER}, commit ${REMOTE_SHA:0:12}) is ready at:
  ${INSTALL_DIR}

Activate and run:
  source "${VENV_DIR}/bin/activate"
  python "${INSTALL_DIR}/marc_repair.py" --help

Check for updates later without changing anything:
  ${RERUN_CMD} --dir "${INSTALL_DIR}" --interpreter ${INTERPRETER} --check

Apply an update in place:
  ${RERUN_CMD} --dir "${INSTALL_DIR}" --interpreter ${INTERPRETER}
EOF
