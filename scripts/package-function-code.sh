#!/usr/bin/env bash

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

source scripts/common.sh

PYTHON_BIN="${PYTHON_BIN:-python}"

require_cmd "$PYTHON_BIN"

if [ "$(uname -s)" != "Linux" ]; then
  echo "This packaging step should run on Linux so .python_packages matches Azure Functions Linux." >&2
  echo "Use GitHub Actions, WSL, a Linux VM, or a Linux container for this step." >&2
  exit 1
fi

"$PYTHON_BIN" - <<'PY'
import sys
version = sys.version_info
if (version.major, version.minor) != (3, 11):
    raise SystemExit("Python 3.11 is required for packaging this Function App.")
PY

rm -rf .python_packages dist
mkdir -p .python_packages/lib/site-packages dist

"$PYTHON_BIN" -m pip install --upgrade pip
"$PYTHON_BIN" -m pip install -r requirements.txt --target .python_packages/lib/site-packages

"$PYTHON_BIN" - <<'PY'
import os
import zipfile

root = os.getcwd()
archive_path = os.path.join(root, "dist", "functionapp.zip")

excluded_dirs = {
    ".azure",
    ".git",
    ".github",
    ".pytest_cache",
    ".venv",
    ".vscode",
    "__pycache__",
    "dist",
    "infra",
    "tests",
}
excluded_files = {
    ".DS_Store",
    "README.md",
    "local.settings.json",
    "local.settings.sample.json",
}

with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
    for current_root, dirs, files in os.walk(root):
        rel_root = os.path.relpath(current_root, root)
        rel_parts = [] if rel_root == "." else rel_root.split(os.sep)
        dirs[:] = [d for d in dirs if d not in excluded_dirs]

        if any(part in excluded_dirs for part in rel_parts):
            continue

        for filename in files:
            if filename in excluded_files:
                continue

            source_path = os.path.join(current_root, filename)
            rel_path = os.path.relpath(source_path, root)

            if any(part in excluded_dirs for part in rel_path.split(os.sep)):
                continue

            archive.write(source_path, rel_path)

print(archive_path)
PY

echo "Created deployment archive: dist/functionapp.zip"
