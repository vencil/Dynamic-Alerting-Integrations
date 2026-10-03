package handler

// #2486 item 3: the route generator lays the tenant's block over the root
// platform files' `tenants.<id>` entries (_lib_confd.overlay_platform_tenants:
// platform first, the tenant file's keys win whole). So when a tenant file
// carries no `_routing` — a batch unset it, or a PUT body leaves it out — an
// overlay `_routing` / `_routing_profile` renders, and the domain policy must
// judge it. Every check now judges Layers.TenantBlock(own) — PUT and batch,
// direct and PR mode, the pre-check and the in-lock fresh-base check (whose
// overlay is the base's).

import (
	"encoding/json"
	"net/http"
	"path/filepath"
	"strings"
	"testing"
)

// overlayChat is a root platform file giving t-off the violating profile.
const overlayChat = "tenants:\n  t-off:\n    _routing_profile: team-chat\n"

// overlayBoth adds a violating `_routing` to it: a tenant file's own
// `_routing` mapping replaces that one whole (the profile still applies
// under it, but the main receiver is the tenant's).
const overlayBoth = overlayChat +
	"    _routing:\n      receiver: {type: slack, api_url: 'https://hooks.slack.com/services/T/B/o'}\n"

// ownPagerduty: t-off's own `_routing` mapping.
const ownPagerduty = "    _routing:\n      receiver: {type: pagerduty, service_key: own-key}\n"

func overlayTree(tOffDisk, overlay string) map[string]string {
	files := staleTree(tOffDisk)
	files["_platform.yaml"] = overlay
	return files
}

// runOverlayBatch posts ops in mode over files (PR mode with a fake origin
// in sync) and returns the per-op results.
func runOverlayBatch(t *testing.T, mode WriteMode, files map[string]string, ops string) []BatchResult {
	t.Helper()
	f := newStaleFixture(t, files, nil)
	f.deps.WriteMode = mode
	w := postTenantBatch(t, f.deps, ops)
	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, body %s", w.Code, w.Body.String())
	}
	var resp BatchResponse
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatal(err)
	}
	return resp.Results
}

func TestRoutingOverlay_BatchUnset(t *testing.T) {
	for _, mode := range bothModes {
		t.Run(string(mode)+"/overlay profile renders once _routing is unset", func(t *testing.T) {
			results := runOverlayBatch(t, mode, overlayTree("    _routing: disable\n", overlayChat), "["+unsetRoutingOp+"]")
			if len(results) != 1 || results[0].Status != "error" || !strings.Contains(results[0].Message, "policy violation") ||
				!strings.Contains(results[0].Message, "team-chat") {
				t.Errorf("results = %+v, want the op refused for the overlay's profile team-chat", results)
			}
		})
		t.Run(string(mode)+"/own _routing mapping: the overlay's _routing does not decide", func(t *testing.T) {
			results := runOverlayBatch(t, mode, overlayTree(ownPagerduty, overlayBoth),
				`[{"tenant_id":"t-off","patch":{"_routing_profile":"domain-ok"}}]`)
			if got := statuses(results); got != okStatus(mode) {
				t.Errorf("results = %+v, want %s", results, okStatus(mode))
			}
		})
	}
}

func TestRoutingOverlay_Put(t *testing.T) {
	for _, mode := range bothModes {
		t.Run(string(mode)+"/body without _routing: the overlay's profile is judged", func(t *testing.T) {
			f := newStaleFixture(t, overlayTree("    _routing: disable\n", overlayChat), nil)
			f.deps.WriteMode = mode
			w := putStale(t, f, "t-off", "tenants:\n  t-off:\n    cpu_usage_percent: '90'\n")
			if w.Code != http.StatusForbidden || !strings.Contains(w.Body.String(), CodePolicyViolation) ||
				!strings.Contains(w.Body.String(), "team-chat") {
				t.Fatalf("status %d, body %s; want 403 for the overlay's profile team-chat", w.Code, w.Body.String())
			}
			if f.prOpened {
				t.Error("a PR was opened")
			}
		})
		t.Run(string(mode)+"/body with its own _routing mapping: allowed", func(t *testing.T) {
			f := newStaleFixture(t, overlayTree("    _routing: disable\n", overlayBoth), nil)
			f.deps.WriteMode = mode
			w := putStale(t, f, "t-off", "tenants:\n  t-off:\n    cpu_usage_percent: '90'\n"+ownPagerduty)
			if w.Code != http.StatusOK {
				t.Fatalf("status %d, body %s; want 200", w.Code, w.Body.String())
			}
		})
	}
}

// A `_routing` the overlay supplies is named as the overlay's in the
// violation, not as the tenant's own.
func TestRoutingOverlay_ViolationNamesTheOverlay(t *testing.T) {
	files := staleTree("    cpu_usage_percent: '85'\n")
	files["_platform.yaml"] = "tenants:\n  t-off:\n    _routing:\n" +
		"      receiver: {type: slack, api_url: 'https://hooks.slack.com/services/T/B/o'}\n"
	f := newStaleFixture(t, files, nil)
	f.deps.WriteMode = WriteModeDirect
	w := putStale(t, f, "t-off", "tenants:\n  t-off:\n    cpu_usage_percent: '90'\n")
	if w.Code != http.StatusForbidden || !strings.Contains(w.Body.String(), "from the platform overlay") {
		t.Fatalf("status %d, body %s; want 403 naming the platform overlay", w.Code, w.Body.String())
	}
}

// PR mode, in-lock: the overlay is the fresh base's. Origin adds the
// overlay; the pod's tree does not have it, so only the fresh-base check
// can refuse.
func TestRoutingOverlay_PRMode_OverlayFromFreshBase(t *testing.T) {
	origin := map[string]string{"_platform.yaml": overlayChat}
	t.Run("batch unset", func(t *testing.T) {
		f := newStaleFixture(t, staleTree("    _routing: disable\n"), origin)
		before := gitRev(t, f.bare, "main")
		assertFreshBaseRefused(t, f, postTenantBatch(t, f.deps, "["+unsetRoutingOp+"]"), 0, "t-off", before)
	})
	t.Run("put without _routing", func(t *testing.T) {
		f := newStaleFixture(t, staleTree("    _routing: disable\n"), origin)
		before := gitRev(t, f.bare, "main")
		localBefore := mustRead(t, filepath.Join(f.dir, "t-off.yaml"))
		w := putStale(t, f, "t-off", "tenants:\n  t-off:\n    cpu_usage_percent: '90'\n")
		assertPutFreshBaseRefused(t, f, w, "t-off", before, localBefore, false)
	})
}
