#!/usr/bin/env bash
# #692 P0 ③ spike — the LOAD-BEARING WALL for kubelet_volume_stats_* scraping.
# STATUS: GREEN, verified on kind 2026-06-14 (see RESULTS below).
#
# Proves what a promtool synthetic fixture structurally CANNOT (it would just
# hardcode the labels):
#   (1) the kubelet is reachable + serves /metrics with nodes/metrics RBAC;
#   (2) the kubelet EMITS kubelet_volume_stats_* for a tenant pod's PVCs;
#   (3) those series carry BOTH `namespace` AND `persistentvolumeclaim`
#         - namespace            → namespace→tenant relabel (1:1) is viable;
#         - persistentvolumeclaim → per-PVC weakest-link eval is viable.
#
# ── RESULTS (2026-06-14) ──────────────────────────────────────────────────
#   * Default kind `local-path` (hostPath) → ZERO kubelet_volume_stats_*
#     (751 kubelet_ series, 0 volume-stats): hostPath has no kubelet
#     MetricsProvider. ⇒ disk-fill recipes REQUIRE a CSI driver w/ NodeGetVolumeStats.
#   * With csi-driver-host-path → 12 volume-stats series appeared in ~30s, e.g.
#       kubelet_volume_stats_available_bytes{namespace="db-a",persistentvolumeclaim="data"}
#       kubelet_volume_stats_capacity_bytes{namespace="db-a",persistentvolumeclaim="config"}
#     ⇒ (1)(2)(3) all confirmed. VALUE CAVEAT: csi-hostpath reports the node's
#     backing-fs df (~1 TB), not the PVC quota — so promtool fixtures must use
#     SYNTHETIC per-PVC values with these REAL label shapes to test masking.
#
# ── ENVIRONMENT NOTES (hard-won) ──────────────────────────────────────────
#   * The apiserver node-proxy `/api/v1/nodes/<n>/proxy/metrics` returns
#     NotFound only via EXTERNAL `kubectl get --raw`. Via an IN-CLUSTER SA token
#     with nodes/proxy RBAC it returns HTTP 200 (verified: cadvisor path = 1741
#     container_ series, bare /metrics = 683 kubelet_ series) — which is exactly
#     what the platform's Prometheus job uses. So PR-1's volume-stats scrape job
#     should use the SAME apiserver-proxy path as the existing cadvisor job. This
#     spike scrapes :10250 directly only because the probe-pod path is simpler.
#   * The csi-driver-host-path deploy scripts are SYMLINK-heavy and Windows-git
#     renders them as text (BASE_DIR breaks); kustomize-on-Windows also bugs out;
#     and the dev container collides with kind's 172.18.0.0/16 subnet. So install
#     the CSI driver from a FRESH LINUX CONTAINER on the kind network:
#       kind get kubeconfig --internal --name <cluster> > kc-internal
#       docker run --rm --network kind -v "$PWD:/work" -e KUBECONFIG=/work/kc-internal \
#         alpine/k8s:1.31.2 sh -c '
#           git clone --depth 1 https://github.com/kubernetes-csi/csi-driver-host-path /csi
#           cd /csi/deploy/kubernetes-1.30 && bash ./deploy.sh'
#       kubectl apply -f - <<<"$(printf 'apiVersion: storage.k8s.io/v1\nkind: StorageClass\nmetadata: {name: csi-hostpath-sc}\nprovisioner: hostpath.csi.k8s.io\nvolumeBindingMode: Immediate\n')"
#     (delete kc-internal afterwards — it embeds cluster certs.)
#
# Requires: kind + kubectl + docker, and csi-hostpath-sc pre-installed (above).
# Run:  bash test/disk-stats-spike/run.sh
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLUSTER="${KIND_CLUSTER:-disk-spike}"
# Bind every kubectl to the spike cluster's context, so the script can never act
# on whatever context happens to be current (kind names it `kind-<cluster>`).
KUBECTL=(kubectl --context "kind-${CLUSTER}")

# The spike does NOT auto-create/destroy the cluster (CSI install is a manual
# Linux-container step, above). Run against an existing kind cluster that already
# has csi-hostpath-sc.
"${KUBECTL[@]}" cluster-info >/dev/null 2>&1 || { echo "no reachable cluster kind-${CLUSTER} — create kind + install CSI (see header)"; exit 2; }
"${KUBECTL[@]}" get sc csi-hostpath-sc >/dev/null 2>&1 \
  || { echo "csi-hostpath-sc missing — install csi-driver-host-path first (see header ENVIRONMENT NOTES)"; exit 2; }

echo "== deploy tenant pod (two CSI PVCs) + privileged kubelet probe =="
"${KUBECTL[@]}" apply -f "$HERE/manifest.yaml" >/dev/null
"${KUBECTL[@]}" -n db-a wait --for=condition=Ready pod/mariadb-sim pod/kprobe --timeout=120s >/dev/null

NODE="$("${KUBECTL[@]}" get nodes -o jsonpath='{.items[0].metadata.name}')"
HOSTIP="$("${KUBECTL[@]}" get node "$NODE" -o jsonpath='{.status.addresses[?(@.type=="InternalIP")].address}')"
echo "== scrape kubelet $HOSTIP:10250/metrics in-cluster (volume-stats cadence ~1 min) =="
metrics=""
for _ in $(seq 1 18); do
  metrics="$("${KUBECTL[@]}" -n db-a exec kprobe -- sh -c \
    'T=$(cat /var/run/secrets/kubernetes.io/serviceaccount/token); curl -sk -H "Authorization: Bearer $T" https://'"$HOSTIP"':10250/metrics' 2>/dev/null || true)"
  echo "$metrics" | grep -q '^kubelet_volume_stats_available_bytes' && break
  sleep 10
done

kubelet_lines="$(echo "$metrics" | grep -c '^kubelet_' || true)"
volstats_lines="$(echo "$metrics" | grep -c '^kubelet_volume_stats_' || true)"

echo "== ASSERT (1): kubelet reachable + serving metrics =="
[ "$kubelet_lines" -gt 0 ] || { echo "FAIL(1): zero kubelet_ metrics — RBAC/reachability (got: $(echo "$metrics" | head -c 120))"; exit 1; }
echo "  ok ($kubelet_lines kubelet_ series)"

echo "== ASSERT (2): kubelet emits volume-stats =="
if [ "$volstats_lines" -eq 0 ]; then
  echo "FAIL(2): kubelet serves metrics but ZERO kubelet_volume_stats_* —"
  echo "         the PVC storage backend has no kubelet MetricsProvider."
  "${KUBECTL[@]}" get pv -o jsonpath='{range .items[*]}{"  PV "}{.metadata.name}{" csi="}{.spec.csi.driver}{" hostPath="}{.spec.hostPath.path}{"\n"}{end}'
  exit 1
fi
echo "  ok ($volstats_lines volume-stats series)"

echo "== ASSERT (3): series carry namespace AND persistentvolumeclaim (both PVCs) =="
sample="$(echo "$metrics" | grep '^kubelet_volume_stats_available_bytes' | grep 'namespace="db-a"' || true)"
echo "$sample" | sed 's/^/    /'
echo "$sample" | grep -q 'namespace="db-a"' || { echo "FAIL(3a): no namespace label"; exit 1; }
if ! { echo "$sample" | grep -q 'persistentvolumeclaim="data"' && echo "$sample" | grep -q 'persistentvolumeclaim="config"'; }; then
  echo "FAIL(3b): persistentvolumeclaim missing or does not distinguish data/config"; exit 1
fi

echo
echo "ALL SPIKE ASSERTIONS PASSED:"
echo "  → namespace→tenant relabel (1:1) viable; per-PVC weakest-link viable."
echo "  → safe to land the design and proceed to PR-1 scrape enablement."
