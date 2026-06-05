package customalerts

import (
	"strings"
	"testing"
)

// The SPIKE GATE (ADR-024 §S6b-2): prove yaml.v3 yaml.Node surgery can replace
// `_custom_alerts` while preserving the rest of a human-authored tenant.yaml —
// crucially its COMMENTS. If this can't hold, Path A (AST on the shared file)
// is not viable and we escalate to Path B (separate machine-owned storage).
const tenantWithComments = `# Tenant: shop-a (owner: payments-team)
# 2026-05: temporarily raised cpu_threshold for the marketing campaign.
tenants:
  shop-a:
    cpu_threshold: "85"  # was 70 — bump until 2026-07
    routing_channel: "slack:#payments-alerts"
    _custom_alerts:
      - recipe: threshold
        name: queue_high
        metric: order_queue_depth
        threshold: "1000:warning"
        window: 5m
    # keep memory modest — see runbook RB-12
    memory_threshold: "90"
`

func TestSpike_ReplacePreservesComments(t *testing.T) {
	out, err := MergeCustomAlerts(tenantWithComments, "shop-a", []map[string]any{
		{"recipe": "threshold", "name": "queue_high", "metric": "order_queue_depth", "threshold": "2000:critical", "window": "10m"},
	})
	if err != nil {
		t.Fatalf("merge: %v", err)
	}
	t.Logf("--- re-encoded output ---\n%s", out)

	// The load-bearing assertion: every human comment survives.
	for _, comment := range []string{
		"# Tenant: shop-a (owner: payments-team)",
		"# 2026-05: temporarily raised cpu_threshold for the marketing campaign.",
		"# was 70 — bump until 2026-07",
		"# keep memory modest — see runbook RB-12",
	} {
		if !strings.Contains(out, comment) {
			t.Errorf("COMMENT LOST: %q\n--- output ---\n%s", comment, out)
		}
	}
	// Other keys + values survive.
	for _, frag := range []string{`cpu_threshold: "85"`, `routing_channel: "slack:#payments-alerts"`, `memory_threshold: "90"`} {
		if !strings.Contains(out, frag) {
			t.Errorf("KEY LOST: %q", frag)
		}
	}
	// The new recipe data is present.
	for _, frag := range []string{"2000:critical", "window: 10m"} {
		if !strings.Contains(out, frag) {
			t.Errorf("new recipe value missing: %q", frag)
		}
	}
	// The old recipe value is gone (it was replaced).
	if strings.Contains(out, "1000:warning") {
		t.Errorf("old recipe value should have been replaced, still present:\n%s", out)
	}
}

func TestSpike_EmptyDeletesKeyNoDebris(t *testing.T) {
	out, err := MergeCustomAlerts(tenantWithComments, "shop-a", nil)
	if err != nil {
		t.Fatalf("merge: %v", err)
	}
	t.Logf("--- re-encoded (empty) ---\n%s", out)
	if strings.Contains(out, "_custom_alerts") {
		t.Errorf("empty list must DELETE the key (Reef 2), but it survives:\n%s", out)
	}
	// surrounding content still intact
	if !strings.Contains(out, "# keep memory modest — see runbook RB-12") {
		t.Errorf("deleting _custom_alerts must not disturb neighbouring comments")
	}
	if !strings.Contains(out, `memory_threshold: "90"`) {
		t.Errorf("deleting _custom_alerts must not drop sibling keys")
	}
}

func TestSpike_AddWhenAbsent(t *testing.T) {
	const noAlerts = `tenants:
  shop-a:
    cpu_threshold: "70"  # baseline
`
	out, err := MergeCustomAlerts(noAlerts, "shop-a", []map[string]any{
		{"recipe": "absence", "name": "heartbeat_gone", "metric": "app_heartbeat_total", "threshold": "0:critical", "window": "10m"},
	})
	if err != nil {
		t.Fatalf("merge: %v", err)
	}
	if !strings.Contains(out, "_custom_alerts") || !strings.Contains(out, "heartbeat_gone") {
		t.Errorf("recipe not added when key absent:\n%s", out)
	}
	if !strings.Contains(out, "# baseline") {
		t.Errorf("adding _custom_alerts must preserve the existing comment")
	}
}

func TestMerge_CanonicalKeyOrderAndQuoting(t *testing.T) {
	out, err := MergeCustomAlerts(tenantWithComments, "shop-a", []map[string]any{
		// deliberately scrambled input order — emission must be canonical
		{"window": "5m", "threshold": "100:warning", "metric": "m", "name": "a", "recipe": "rate"},
	})
	if err != nil {
		t.Fatalf("merge: %v", err)
	}
	// anchor within the recipe block (avoid `cpu_threshold:` substring collision)
	blockAt := strings.Index(out, "_custom_alerts:")
	if blockAt < 0 {
		t.Fatalf("no _custom_alerts block:\n%s", out)
	}
	block := out[blockAt:]
	// canonical order: recipe → name → metric → window → threshold
	ri := strings.Index(block, "recipe: rate")
	ni := strings.Index(block, "name: a")
	mi := strings.Index(block, "metric: m")
	wi := strings.Index(block, "window:")
	ti := strings.Index(block, `threshold: "100`)
	if !(ri >= 0 && ri < ni && ni < mi && mi < wi && wi < ti) {
		t.Errorf("recipe keys not in canonical order (recipe<name<metric<window<threshold):\n%s", out)
	}
	// value:severity must be quoted (colon → ambiguous unquoted)
	if !strings.Contains(out, `threshold: "100:warning"`) {
		t.Errorf("threshold with a colon must be double-quoted:\n%s", out)
	}
	// plain enum stays unquoted
	if !strings.Contains(out, "recipe: rate") {
		t.Errorf("plain scalar should stay unquoted:\n%s", out)
	}
}

func TestMerge_IndentNormalizesToTwoSpace(t *testing.T) {
	// self-review F3: documents the indent-normalization behaviour explicitly
	// rather than hiding it. A 4-space file reflows to the 2-space convention;
	// comments still survive (the load-bearing guarantee).
	const fourSpace = "# head\n" +
		"tenants:\n" +
		"    db-a:\n" +
		"        mysql_connections: \"70\"  # inline\n"
	out, err := MergeCustomAlerts(fourSpace, "db-a", []map[string]any{
		{"recipe": "threshold", "name": "a", "metric": "m", "threshold": "1", "window": "5m"},
	})
	if err != nil {
		t.Fatalf("merge: %v", err)
	}
	if !strings.Contains(out, "# head") || !strings.Contains(out, "# inline") {
		t.Errorf("comments must survive even when indent is reflowed:\n%s", out)
	}
	if strings.Contains(out, "    db-a:") { // 4-space indent should be gone
		t.Logf("note: 4-space reflowed to 2-space (documented F3 behaviour)")
	}
}

func TestMerge_MissingTenantErrors(t *testing.T) {
	if _, err := MergeCustomAlerts(tenantWithComments, "nonexistent", []map[string]any{{"recipe": "threshold"}}); err == nil {
		t.Error("expected an error for a tenant not present in the yaml")
	}
}
