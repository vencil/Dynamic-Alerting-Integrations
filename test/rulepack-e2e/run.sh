#!/usr/bin/env bash
# ADR-024 PR3b kind e2e — reproduces the (0a) version-injection prerequisite proof.
#
# What it proves (the one thing promtool's synthetic kube_pod_labels CANNOT):
#   1. DEFAULT kube-state-metrics emits ZERO kube_pod_labels samples → the rule
#      pack's (0a) join matches nothing → version-aware thresholds are INERT.
#   2. With --metric-labels-allowlist=pods=[app.kubernetes.io/version], KSM emits
#      kube_pod_labels{...,label_app_kubernetes_io_version="vN"} — the EXACT label
#      name (0a) relabels into `version`. This is why
#      k8s/03-monitoring/deployment-kube-state-metrics.yaml carries that arg.
#
# Requires: kind + kubectl + a reachable docker daemon. ~2 min.
# Run:  bash test/rulepack-e2e/run.sh
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLUSTER="${KIND_CLUSTER:-adr024-e2e}"
PF_PORT="${PF_PORT:-18080}"
ALLOWLIST='--metric-labels-allowlist=pods=[app.kubernetes.io/version]'

cleanup() { kind delete cluster --name "$CLUSTER" >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "== create kind cluster $CLUSTER =="
kind create cluster --name "$CLUSTER" --wait 90s >/dev/null

echo "== deploy KSM (default, NO allowlist) + versioned pods =="
kubectl apply -f "$HERE/ksm-and-versioned-pods.yaml" >/dev/null
kubectl -n kube-system rollout status deploy/kube-state-metrics --timeout=120s >/dev/null
kubectl -n db-a wait --for=condition=Ready pod/app-v1-x pod/app-v2-y pod/app-noversion --timeout=90s >/dev/null

scrape() {
  kubectl -n kube-system port-forward svc/kube-state-metrics "${PF_PORT}:8080" >/tmp/ksm-pf.log 2>&1 &
  local pf=$!; sleep 4
  curl -s "localhost:${PF_PORT}/metrics" || true
  kill "$pf" 2>/dev/null || true
}

echo "== ASSERT 1: default KSM exposes NO version labels (feature would be inert) =="
if scrape | grep -q 'label_app_kubernetes_io_version='; then
  echo "UNEXPECTED: default KSM exposed version labels — assumption changed"; exit 1
fi
echo "  ✓ default KSM: zero version labels (confirms the silent-inert risk)"

echo "== apply the fix: $ALLOWLIST =="
kubectl -n kube-system patch deploy kube-state-metrics --type=json \
  -p "[{\"op\":\"add\",\"path\":\"/spec/template/spec/containers/0/args\",\"value\":[\"$ALLOWLIST\"]}]" >/dev/null
kubectl -n kube-system rollout status deploy/kube-state-metrics --timeout=90s >/dev/null

echo "== ASSERT 2: allowlisted KSM exposes label_app_kubernetes_io_version=v1/v2 =="
got="$(scrape | grep -oE 'label_app_kubernetes_io_version="[^"]*"' | sort -u | tr '\n' ' ')"
echo "  got: $got"
echo "$got" | grep -q 'v1' && echo "$got" | grep -q 'v2' \
  || { echo "FAIL: expected v1 AND v2 version labels"; exit 1; }
echo "  ✓ allowlisted KSM: (0a) relabel source present with exact key name"

echo "== ASSERT 3: allowlist-ON KSM emits a BASE kube_pod_labels series for the =="
echo "==           version-LESS pod (sentinel scale-to-zero discriminator basis)  =="
if scrape | grep -qE '^kube_pod_labels\{namespace="db-a",pod="app-noversion"'; then
  echo "  ✓ version-less pod has a base kube_pod_labels series → 'zero pod-label"
  echo "    series' uniquely means 'allowlist off', not 'no version pods running'"
else
  echo "FAIL: expected a base kube_pod_labels series for app-noversion"; exit 1
fi

echo "== PASS: (0a) prerequisite + sentinel discriminator validated end-to-end =="
