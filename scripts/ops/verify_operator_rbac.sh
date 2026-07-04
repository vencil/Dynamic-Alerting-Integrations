#!/usr/bin/env bash
# verify_operator_rbac.sh — assert a cluster's operator ServiceAccount cannot
# tamper cross-tenant ConfigMaps, admission policy, or consuming workloads.
#
# Part A verification for issue #926 (spun out of #903). Read-only: it queries
# effective RBAC via `kubectl auth can-i --as=<sa>` (server-side evaluation, which
# folds in the union of all bindings) and makes NO changes to the cluster.
#
# Usage:
#   verify_operator_rbac.sh --operator-sa <ns>:<sa> [--platform-ns <ns>] \
#       [--deployments "<name> <name> ..."]
#
# Example:
#   verify_operator_rbac.sh --operator-sa my-operators:tenant-operator \
#       --platform-ns monitoring --deployments "federation-gateway vector tenant-api"
#
# Exit: 0 = all dangerous grants withheld; 1 = at least one VIOLATION (or usage error).
set -euo pipefail

OPERATOR_SA=""
PLATFORM_NS="monitoring"
DEPLOYMENTS="federation-gateway vector tenant-api"

usage() {
  grep '^#' "$0" | sed 's/^# \{0,1\}//' | sed -n '2,20p'
  exit 1
}

while [ $# -gt 0 ]; do
  case "$1" in
    --operator-sa) OPERATOR_SA="${2:-}"; shift 2 ;;
    --platform-ns) PLATFORM_NS="${2:-}"; shift 2 ;;
    --deployments) DEPLOYMENTS="${2:-}"; shift 2 ;;
    -h|--help) usage ;;
    *) echo "unknown argument: $1" >&2; usage ;;
  esac
done

if [ -z "$OPERATOR_SA" ]; then
  echo "error: --operator-sa <namespace>:<serviceaccount> is required" >&2
  usage
fi
case "$OPERATOR_SA" in
  *:*) : ;;
  *) echo "error: --operator-sa must be in <namespace>:<serviceaccount> form" >&2; exit 1 ;;
esac

sa_ns="${OPERATOR_SA%%:*}"
sa_name="${OPERATOR_SA##*:}"
SA="system:serviceaccount:${sa_ns}:${sa_name}"

if ! command -v kubectl >/dev/null 2>&1; then
  echo "error: kubectl not found on PATH" >&2
  exit 1
fi

# Fail-closed preflight: if we cannot even LIST the SA's effective permissions
# (bad kubectl context, no cluster connectivity, or no impersonation rights),
# ABORT. A connection/impersonation failure must never silently read as
# "everything denied" → a false PASS on a security check.
if ! kubectl auth can-i --list --as="$SA" -n "$PLATFORM_NS" >/dev/null 2>&1; then
  echo "error: cannot evaluate RBAC for ${SA} in namespace ${PLATFORM_NS}." >&2
  echo "       check kubectl context / cluster connectivity, and that you are" >&2
  echo "       allowed to impersonate (--as) the ServiceAccount." >&2
  exit 1
fi

fail=0

# can_do <verb> <resource> [namespace-flag...] — echoes "yes"/"no", or "" on a
# probe error (kubectl transport/authz failure that prints neither answer).
can_do() {
  local verb="$1" resource="$2"; shift 2
  kubectl auth can-i "$verb" "$resource" --as="$SA" "$@" 2>/dev/null || true
}

# assert_denied <label> <verb> <resource> [namespace-flag...]
# Fail-closed per check: a probe that yields neither "yes" nor "no" is a
# transport/authz error, not a "denied" — abort rather than silently pass it as
# safe. (assert_denied runs in the main shell, so this exit terminates the run;
# an exit inside can_do would only kill the $(...) subshell and be swallowed.)
assert_denied() {
  local label="$1" verb="$2" resource="$3"; shift 3
  local ans; ans="$(can_do "$verb" "$resource" "$@")"
  if [ -z "$ans" ]; then
    echo "error: could not evaluate '${verb} ${label}' for ${SA} (probe returned no yes/no) — aborting" >&2
    exit 1
  fi
  if [ "$ans" = "yes" ]; then
    echo "VIOLATION: ${SA} CAN ${verb} ${label}"
    fail=1
  else
    echo "ok: ${SA} cannot ${verb} ${label}"
  fi
}

echo "== operator RBAC narrowing check =="
echo "operator SA   : ${SA}"
echo "platform ns   : ${PLATFORM_NS}"
echo "consuming dpl : ${DEPLOYMENTS}"
echo

# Rule 1 — cross-tenant ConfigMaps must not be writable at namespace level.
for verb in create update patch delete deletecollection; do
  assert_denied "configmaps in ${PLATFORM_NS}" "$verb" configmaps -n "$PLATFORM_NS"
done

# Rule 2 — admission policy / binding writes (cluster-scoped, no namespace).
for res in validatingadmissionpolicies.admissionregistration.k8s.io \
           validatingadmissionpolicybindings.admissionregistration.k8s.io; do
  for verb in create update patch delete; do
    assert_denied "$res" "$verb" "$res"
  done
done

# Rule 2b — RBAC self-escalation (external adversarial review, #993): writes on
# roles/rolebindings (namespaced) or clusterroles/clusterrolebindings (cluster-
# scoped) let the operator grant itself anything (e.g. bind cluster-admin).
for res in roles.rbac.authorization.k8s.io rolebindings.rbac.authorization.k8s.io; do
  for verb in create update patch delete; do
    assert_denied "$res in ${PLATFORM_NS}" "$verb" "$res" -n "$PLATFORM_NS"
  done
done
for res in clusterroles.rbac.authorization.k8s.io clusterrolebindings.rbac.authorization.k8s.io; do
  for verb in create update patch delete; do
    assert_denied "$res" "$verb" "$res"
  done
done

# Rule 3 — patch on the specific consuming Deployments (workload-ref redirect, #925).
for dpl in $DEPLOYMENTS; do
  for verb in patch update; do
    assert_denied "deployment ${dpl} in ${PLATFORM_NS}" "$verb" "deployments.apps/${dpl}" -n "$PLATFORM_NS"
  done
done

# Rule 4a — pod / workload creation = ServiceAccount hijack (external adversarial
# review, #993): creating a pod (directly or via any controller) with
# serviceAccountName set to a consuming SA runs code as that SA, sidestepping the
# operator-SA RBAC entirely.
for res in pods deployments.apps statefulsets.apps daemonsets.apps replicasets.apps \
           jobs.batch cronjobs.batch; do
  assert_denied "create ${res} in ${PLATFORM_NS} (SA hijack)" create "$res" -n "$PLATFORM_NS"
done

# Rule 4b — runtime access into consuming pods bypasses config-object defenses
# (external adversarial review, #993): exec/attach/portforward can dump tokens or
# interfere with reload; ephemeralcontainers inject a debug container into a live pod.
# NOTE: subresources go via --subresource=, not "pods/exec" — the slash form is
# parsed as TYPE/NAME (a pod literally named "exec"), not the exec subresource.
for sub in exec attach portforward; do
  assert_denied "create pods/${sub} in ${PLATFORM_NS}" create pods --subresource="$sub" -n "$PLATFORM_NS"
done
for verb in update patch; do
  assert_denied "${verb} pods/ephemeralcontainers in ${PLATFORM_NS}" "$verb" pods --subresource=ephemeralcontainers -n "$PLATFORM_NS"
done

echo
if [ "$fail" -ne 0 ]; then
  echo "RESULT: FAIL — operator RBAC grants at least one dangerous write (see VIOLATION lines)."
  exit 1
fi
echo "RESULT: PASS — operator RBAC is narrowed (no dangerous cross-tenant write grants)."
