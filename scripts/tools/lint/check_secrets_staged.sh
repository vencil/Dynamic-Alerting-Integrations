#!/usr/bin/env bash
# check_secrets_staged.sh — L1 pre-commit secret scan (#445 AC i)
#
# Invokes `trufflehog filesystem` on the list of staged files that
# pre-commit hands us via $@. Runs offline (--no-verification) for
# speed; the L2 server-side workflow (`secret-scan.yml`, Chunk 4) does
# the verified-API check.
#
# Stage: pre-commit (default in .pre-commit-config.yaml).
# Performance target: P95 ≤ 5s on typical staged sets.
#
# False-positive escapes (two mechanisms):
#   - Path-based: `.trufflehogignore` at repo root — newline-separated
#     ERE regex patterns. THIS SCRIPT does the filtering (greps the
#     staged file list against the patterns before invoking trufflehog),
#     NOT trufflehog itself — the `filesystem` subcommand has no stable
#     `--exclude-paths` flag, so we own the path-exclusion logic to stay
#     version-independent.
#   - Line-based: inline `# trufflehog:ignore` comment on the offending
#     line (trufflehog-native; works in most file types).
#   - ⛔ NOT `git commit --no-verify` — see SOP §反 SOP.
#
# Exit codes:
#   0 — clean (no findings); OR no staged files left after .trufflehogignore
#       filtering; OR no staged text files at all; OR trufflehog binary
#       absent (degraded — see below)
#   1 — scan found a secret (trufflehog exit 183), OR trufflehog itself
#       errored (any other non-zero — blocked fail-closed). The two cases
#       get DIFFERENT messages so a contributor doesn't run the
#       rotate-first SOP for what is actually a tool/environment error.
#
# Missing-binary policy — soft-skip with a loud warning, NOT block:
#   If `trufflehog` is not on PATH the hook prints a prominent install
#   warning and exits 0. Rationale:
#     - Matches the repo's existing commit-msg hook convention ("don't
#       block commits on a missing validator").
#     - L1 is explicitly the bypassable best-effort shift-left layer
#       (issue #445 framing: "L1 Pre-commit ... --no-verify 可繞"). The
#       UNBYPASSABLE gate is L2 server-side (`secret-scan.yml`, Chunk 4),
#       which runs regardless of any contributor's local tooling.
#     - Hard-blocking a one-line doc fix on a 30 MB binary install is bad
#       DX and would just push people to `--no-verify` (skips ALL hooks).
#   The warning fires every commit until trufflehog is installed — loud
#   enough to nag, soft enough not to wall off the repo.
#
# References:
#   - issue #445 AC i — L1 layer of the L0/L1/L2/L3 multi-layer
#   - SOP: docs/internal/secret-leak-remediation-sop.md (what to do on hit)
#   - trufflehog upstream: https://github.com/trufflesecurity/trufflehog

set -euo pipefail

# Graceful no-op when pre-commit invokes us with zero files (nothing
# staged matches our `types: [text]` filter in .pre-commit-config.yaml).
if [ $# -eq 0 ]; then
  exit 0
fi

# --- Tool presence check (soft-skip with loud warning if absent) ---
if ! command -v trufflehog >/dev/null 2>&1; then
  cat >&2 <<'EOF'
⚠️  L1 secret-scan SKIPPED — trufflehog binary not found on PATH.

This commit is NOT being scanned for secrets locally. Install trufflehog
to restore L1 protection (one-time):

  Linux:    curl -sSfL https://raw.githubusercontent.com/trufflesecurity/trufflehog/main/scripts/install.sh \
              | sudo sh -s -- -b /usr/local/bin
  macOS:    brew install trufflehog
  Windows:  download the windows_amd64 archive from
            https://github.com/trufflesecurity/trufflehog/releases
            (or `scoop install trufflehog` if you use scoop; or use WSL)

  NOTE: `go install .../trufflehog/v3@latest` does NOT work — trufflehog's
  go.mod has replace directives that block `go install`. Use install.sh.

After install, verify with: trufflehog --version

Not blocking the commit (L1 is best-effort shift-left). The L2 server-side
scan (#445 AC ii) still runs on push and WILL catch a leak — but local
detection is faster + cheaper. Please install.
EOF
  exit 0
fi

REPO_ROOT=$(git rev-parse --show-toplevel)
IGNORE_FILE="${REPO_ROOT}/.trufflehogignore"

# --- Apply .trufflehogignore path filtering ---
# We do this ourselves rather than relying on a trufflehog flag. Read
# each non-comment non-blank line as an ERE pattern; drop any staged
# path that matches any pattern.
declare -a SCAN_PATHS=()
declare -a IGNORE_PATTERNS=()
if [ -f "${IGNORE_FILE}" ]; then
  while IFS= read -r line; do
    # Trim leading + trailing whitespace before any interpretation. ERE
    # patterns never carry significant edge whitespace, and leaving
    # leading whitespace in (a) breaks the comment/blank skip below for
    # indented lines and (b) bakes literal spaces into the regex, which
    # silently fails-open (the pattern then matches no path → the file
    # gets scanned anyway). Trim-first makes both correct.
    line="${line#"${line%%[![:space:]]*}"}"   # strip leading whitespace
    line="${line%"${line##*[![:space:]]}"}"   # strip trailing whitespace
    # Skip blank lines and comments (leading # after optional whitespace).
    case "${line}" in
      ''|'#'*) continue ;;
    esac
    IGNORE_PATTERNS+=("${line}")
  done < "${IGNORE_FILE}"
fi

for f in "$@"; do
  skip=0
  for pat in "${IGNORE_PATTERNS[@]:-}"; do
    [ -z "${pat}" ] && continue
    if printf '%s\n' "${f}" | grep -Eq -- "${pat}"; then
      skip=1
      break
    fi
  done
  if [ "${skip}" -eq 0 ]; then
    SCAN_PATHS+=("${f}")
  fi
done

# All staged files were .trufflehogignore'd → nothing to scan.
if [ "${#SCAN_PATHS[@]}" -eq 0 ]; then
  exit 0
fi

# --- Run the scan ---
#   filesystem        — scan local files (vs git/github/etc. modes)
#   --fail            — exit 183 on findings (otherwise just logs)
#   --no-verification — skip API calls (offline + fast for L1; L2 verifies)
#
# Capture status without `set -e` aborting — we want to print a helpful
# error message rather than exit silently.
set +e
trufflehog filesystem "${SCAN_PATHS[@]}" \
  --fail \
  --no-verification
SCAN_RC=$?
set -e

if [ "${SCAN_RC}" -eq 0 ]; then
  exit 0
fi

# trufflehog with `--fail` exits EXACTLY 183 when it detects findings.
# Any OTHER non-zero is the tool itself erroring (bad args, crash,
# unreadable path, …). Distinguish: a contributor should not be sent
# down the rotate-first SOP for what is actually a scanner malfunction.
if [ "${SCAN_RC}" -ne 183 ]; then
  printf '%s\n' "" \
    "❌ trufflehog exited ${SCAN_RC} (not its findings code 183) — the scan" \
    "   TOOL errored rather than detecting a secret. Commit blocked" \
    "   fail-closed: a malfunctioning scanner must not silently pass." \
    "" \
    "   Review the trufflehog output above. If it is an environment issue" \
    "   (bad path, crash, version mismatch), fix it and recommit. This is" \
    "   NOT necessarily a secret leak — do not run the rotation SOP unless" \
    "   the output actually shows a credential." >&2
  exit 1
fi

# SCAN_RC == 183 — genuine findings.
cat >&2 <<'EOF'

🚨 Detected one or more potential secrets in staged files (see output above).

If a real secret leaked: STOP. Do NOT commit. Follow the SOP:
  → docs/internal/secret-leak-remediation-sop.md
  Rule #1: ASSUME COMPROMISE. ROTATE FIRST. (Step 2 of the 5-step response.)

If this is a false positive (test fixture / known-fake string):
  Path-based escape: add a regex matching the file path to `.trufflehogignore`
                     (repo root, one ERE pattern per line).
  Line-based escape: append a `# trufflehog:ignore` comment to the offending
                     line (works in most file types — Python, YAML, shell, …).
  Recommit after adding the escape.

⛔ DO NOT use `git commit --no-verify` to bypass.
   The L2 server-side scan (#445 AC ii, Chunk 4) will catch the same finding
   on push regardless — `--no-verify` only delays the failure AND ships your
   local clone with a leak the SOP says to rotate-first.

   See `docs/internal/secret-leak-remediation-sop.md` §反 SOP for the full
   list of known-amplifies-damage actions.
EOF
exit 1
