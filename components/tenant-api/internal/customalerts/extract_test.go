package customalerts

import "testing"

func TestExtract_ReturnsRecipes(t *testing.T) {
	got, err := Extract(tenantWithComments, "shop-a")
	if err != nil {
		t.Fatalf("extract: %v", err)
	}
	if len(got) != 1 {
		t.Fatalf("len = %d, want 1; got %v", len(got), got)
	}
	if got[0]["name"] != "queue_high" || got[0]["recipe"] != "threshold" {
		t.Errorf("recipe fields wrong: %v", got[0])
	}
}

func TestExtract_EmptyWhenAbsent(t *testing.T) {
	const noAlerts = "tenants:\n  shop-a:\n    cpu_threshold: \"70\"\n"
	got, err := Extract(noAlerts, "shop-a")
	if err != nil {
		t.Fatalf("extract: %v", err)
	}
	if got == nil || len(got) != 0 {
		t.Errorf("want non-nil empty slice (JSON []), got %v", got)
	}
}

func TestExtract_EmptyWhenTenantMissing(t *testing.T) {
	got, err := Extract(tenantWithComments, "nonexistent")
	if err != nil {
		t.Fatalf("extract: %v", err)
	}
	if len(got) != 0 {
		t.Errorf("absent tenant should yield empty, got %v", got)
	}
}

func TestExtract_RoundTripsSelectors(t *testing.T) {
	const withSel = `tenants:
  shop-a:
    _custom_alerts:
      - recipe: rate
        name: r
        metric: http_requests_total
        selectors_re:
          status: "5.."
`
	got, err := Extract(withSel, "shop-a")
	if err != nil {
		t.Fatalf("extract: %v", err)
	}
	if len(got) != 1 {
		t.Fatalf("len = %d, want 1", len(got))
	}
	sel, ok := got[0]["selectors_re"].(map[string]any)
	if !ok || sel["status"] != "5.." {
		t.Errorf("nested selectors_re not extracted: %v", got[0])
	}
}
