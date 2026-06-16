#!/usr/bin/env bash
# _verify_download.sh — supply-chain guard for CI tool-binary installs.
#
# Verifies a freshly-downloaded artifact against a PINNED SHA-256 digest BEFORE
# it is extracted / installed onto the runner. A bare `curl … | install` trusts
# whatever the URL serves; this turns a tampered, corrupted, or upstream-re-pushed
# artifact into a hard failure instead of code running on the runner.
#
# Pinning the expected digest in the workflow (rather than re-downloading the
# upstream `checksums.txt` from the SAME release) is deliberate: it also detects
# an upstream artifact being silently re-pushed under the same tag — which a
# checksum file fetched from that same release cannot.
#
# Usage:
#   _verify_download.sh <file> <expected-sha256-hex>
#
# Behaviour:
#   - match    → prints "OK …" and exits 0.
#   - mismatch → prints a GitHub-Actions ::error:: with expected/actual,
#                DELETES the (untrusted) file, and exits 1.
#   - missing file / bad args → exits 1.
#
# Caller keeps control of best-effort semantics: a step with
# `continue-on-error: true` (or an `if curl …; then` guard) treats the non-zero
# exit as "skip this install, fall back" — the ::error:: annotation still
# surfaces the mismatch in the run.

set -euo pipefail

file="${1:?usage: _verify_download.sh <file> <expected-sha256-hex>}"
expected="${2:?usage: _verify_download.sh <file> <expected-sha256-hex>}"

if [ ! -f "$file" ]; then
  echo "::error::verify-download: file not found: $file" >&2
  exit 1
fi

# Normalise to lowercase hex so a pinned UPPERCASE digest still matches.
expected="$(printf '%s' "$expected" | tr 'A-Z' 'a-z')"
actual="$(sha256sum "$file" | awk '{print $1}')"

if [ "$actual" != "$expected" ]; then
  echo "::error::verify-download: SHA-256 mismatch for $file" >&2
  echo "  expected: $expected" >&2
  echo "  actual:   $actual" >&2
  rm -f "$file"
  exit 1
fi

echo "verify-download: OK $file ($actual)"
