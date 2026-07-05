#!/usr/bin/env bash
# try-local chaos control — "break it" earned-red for the federation-revocation
# demo (Act 2). The FUN half of the security showcase: YOU inject the tamper and
# watch the reconciler catch it, so the red light is exciting, not alarming
# (Falco event-generator pattern — user-triggered fault, not a mystery outage).
#
# HOW IT WORKS (no new service, no attack surface): this writes ONE word — the
# scenario name — to try-local/seed/.demo-mode. The mock-reconciler loop
# (seed/push-federation-metrics.sh) reads that flag at the start of every ~15s
# push and emits the matching bad metric values, so the red HOLDS until you heal.
# WHY a watched flag and not a one-shot push: a single bad push would be
# overwritten by the loop's next healthy push ~15s later and flicker back to
# green. The mount is `./seed:/seed:ro` — read-only for the CONTAINER, but a :ro
# bind mount still reflects HOST writes inward, so this host-side write lands in
# the container on its next read. The flag file is gitignored (runtime artifact).
#
# Usage:  bash try-local/chaos.sh <tamper|stale|drift|failopen|heal>
# Makefile shortcuts for the headline + reset:  make chaos-tamper / make chaos-heal
#
# Requires nothing but a POSIX shell + write access to try-local/seed/ (same as
# the rest of the stack; no curl/jq unlike smoke.sh — it only touches a file).
set -euo pipefail

# Resolve relative to THIS script's own dir so it works from any cwd (mirrors
# smoke.sh's SCRIPT_DIR). The flag lives next to the seed scripts the loop mounts.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MODE_FILE="${SCRIPT_DIR}/seed/.demo-mode"
# Loop cadence, echoed in the "wait ~Ns" hint. Keep in sync with the
# mock-reconciler PUSH_INTERVAL in docker-compose.yaml (default 15s).
PUSH_INTERVAL="${PUSH_INTERVAL:-15}"

usage() {
  cat >&2 <<EOF
usage: $(basename "$0") <scenario>

Inject a synthetic fault into the federation-revocation demo (Act 2). The
mock-reconciler applies it on its next push (~${PUSH_INTERVAL}s), lighting the
matching Grafana panel(s) at http://localhost:3000.

scenarios:
  tamper     🔴 Tamper status → CRITICAL. A revoked-but-live token reappeared
             (the headline un-revoke). Everything else stays green.
  stale      🔴 Reconciler freshness → STALE (last pass ~2000s ago, >1800s
             threshold) + fail-closed rate() climbs. The reconciler stopped.
  drift      🟡 Coverage integrity + erosion ratio → schema drift (revocation
             rows failing to parse; events_dropped > 0).
  failopen   🟡 Gateway fail-open → the gateway couldn't read the revoked set
             and allowed the token through (degraded, not breached).
  heal       ✅ Remove the flag → back to calm-green (the healthy default).

examples:
  make chaos-tamper            # the headline (== chaos.sh tamper)
  bash $(basename "$0") drift
  make chaos-heal              # reset (== chaos.sh heal)
EOF
  exit 2
}

# Exactly one arg required.
[ "$#" -eq 1 ] || usage
scenario="$1"

case "${scenario}" in
  tamper)
    printf 'tamper\n' > "${MODE_FILE}"
    echo "💥 Injected: TAMPER (revoked-but-live token reappeared)."
    echo "   Watch flip 🔴 within ~${PUSH_INTERVAL}s at http://localhost:3000 :"
    echo "     • Tamper status stat        →  🔴 CRITICAL (was ✓ Clean)"
    echo "     • Tamper suspected timeseries →  steps from 0 to 1"
    echo "   Freshness / coverage / gateway stay green — only tamper screams."
    ;;
  stale)
    printf 'stale\n' > "${MODE_FILE}"
    echo "💥 Injected: STALE (reconciler stopped succeeding)."
    echo "   Watch flip within ~${PUSH_INTERVAL}s at http://localhost:3000 :"
    echo "     • Reconciler freshness       →  🔴 Stale (last pass ~2000s ago > 1800s)"
    echo "     • Fail-closed rate()         →  climbs above 0 (errors accumulating)"
    ;;
  drift)
    printf 'drift\n' > "${MODE_FILE}"
    echo "💥 Injected: DRIFT (revocation-log schema drift; rows failing to parse)."
    echo "   Watch flip within ~${PUSH_INTERVAL}s at http://localhost:3000 :"
    echo "     • Coverage integrity         →  🟡 (events_dropped = 3)"
    echo "     • Erosion ratio + dropped timeseries →  🟡 nonzero"
    ;;
  failopen)
    printf 'failopen\n' > "${MODE_FILE}"
    echo "💥 Injected: FAIL-OPEN (gateway couldn't read the revoked set → allowed)."
    echo "   Watch flip within ~${PUSH_INTERVAL}s at http://localhost:3000 :"
    echo "     • Gateway fail-open          →  🟡 (was ✓ OK)"
    echo "   Degraded, not breached — distinct from tamper. Rest stays green."
    ;;
  heal)
    # Remove the flag: absent ⇒ the loop's healthy default. rm -f is idempotent
    # (no error if you heal when already healthy).
    rm -f "${MODE_FILE}"
    echo "✅ Healed: flag removed → back to calm-green."
    echo "   The next reconciler push (~${PUSH_INTERVAL}s) restores all panels to green."
    echo "   (stale's fail-closed counter stays pinned so rate() settles to 0 cleanly.)"
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    echo "chaos: unknown scenario '${scenario}'" >&2
    usage
    ;;
esac

# Honest reminder, always. The data is synthetic — see README Act 2.
if [ "${scenario}" != "heal" ]; then
  echo "   Reset any time:  make chaos-heal   (or: bash $(basename "$0") heal)"
  echo "   (Synthetic demo data — the dashboard + thresholds are the real prod SOT.)"
fi
