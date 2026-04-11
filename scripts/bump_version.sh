#!/usr/bin/env bash
#
# Bump the lithos-mcp version in pyproject.toml and regenerate uv.lock.
#
# Usage:
#     scripts/bump_version.sh <new-version>
#
# Example:
#     scripts/bump_version.sh 0.1.8
#
# After running, review `git diff pyproject.toml uv.lock` and commit both
# files together. Docker CI uses `uv sync --locked`, so the two files must
# never be committed out of sync.

set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "usage: $0 <new-version>" >&2
    exit 2
fi

new_version="$1"

# Validate the version looks like a PEP 440 release (X.Y.Z, optionally
# with a pre/post/dev suffix). This is a sanity check, not full PEP 440.
if ! [[ "$new_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+([abrc]|rc|\.post|\.dev)?[0-9]*$ ]]; then
    echo "error: '$new_version' does not look like a valid version" >&2
    exit 2
fi

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
pyproject="$repo_root/pyproject.toml"
lockfile="$repo_root/uv.lock"

if [[ ! -f "$pyproject" ]]; then
    echo "error: $pyproject not found" >&2
    exit 1
fi

current_version="$(grep -E '^version = "' "$pyproject" | head -n1 | sed -E 's/version = "(.*)"/\1/')"

if [[ -z "$current_version" ]]; then
    echo "error: could not read current version from $pyproject" >&2
    exit 1
fi

if [[ "$current_version" == "$new_version" ]]; then
    echo "version is already $new_version — nothing to do"
    exit 0
fi

echo "bumping lithos-mcp: $current_version -> $new_version"

# Replace only the [project] version line. The pattern is anchored to the
# start of a line so it won't match version strings of dependencies.
python3 - "$pyproject" "$new_version" <<'PY'
import re
import sys

path, new_version = sys.argv[1], sys.argv[2]
with open(path) as f:
    text = f.read()

new_text, n = re.subn(
    r'^version = "[^"]*"',
    f'version = "{new_version}"',
    text,
    count=1,
    flags=re.MULTILINE,
)
if n != 1:
    sys.exit(f"error: could not find version line in {path}")

with open(path, "w") as f:
    f.write(new_text)
PY

echo "regenerating uv.lock..."
(cd "$repo_root" && uv lock)

# Sanity check: confirm uv.lock now reports the new version for lithos-mcp.
locked_version="$(awk '/^name = "lithos-mcp"$/ {getline; print}' "$lockfile" | sed -E 's/version = "(.*)"/\1/')"
if [[ "$locked_version" != "$new_version" ]]; then
    echo "error: uv.lock reports lithos-mcp $locked_version, expected $new_version" >&2
    exit 1
fi

echo
echo "done. Review and commit:"
echo "    git diff pyproject.toml uv.lock"
echo "    git add pyproject.toml uv.lock"
echo "    git commit -m 'bump version to $new_version'"
