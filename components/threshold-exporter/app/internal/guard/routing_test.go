package guard

import (
	"strings"
	"testing"
)

// fixtureRouting returns a routing dict with one webhook receiver
// (all required fields satisfied) and no overrides — the simplest
// valid configuration used as a starting point in tests.
func fixtureRouting() map[string]any {
	return map[string]any{
		"receiver": map[string]any{
			"type": "webhook",
			"url":  "https://noc.example.com/alerts",
		},
	}
}

// runWithRouting wraps CheckDefaultsImpact with a single tenant so
// each test can focus on the routing finding shape without setting
// up the full CheckInput surface.
func runWithRouting(t *testing.T, tenantID string, routing map[string]any) []Finding {
	t.Helper()
	r, err := CheckDefaultsImpact(CheckInput{
		EffectiveConfigs: map[string]map[string]any{
			tenantID: {"placeholder": 1},
		},
		RoutingByTenant: map[string]map[string]any{
			tenantID: routing,
		},
	})
	if err != nil {
		t.Fatalf("CheckDefaultsImpact: %v", err)
	}
	return r.Findings
}

// --- check 1: unknown receiver type ---------------------------------

func TestRouting_UnknownReceiverTypeIsError(t *testing.T) {
	got := runWithRouting(t, "t1", map[string]any{
		"receiver": map[string]any{"type": "telegram", "url": "https://x"},
	})
	if len(got) != 1 {
		t.Fatalf("got %d findings, want 1", len(got))
	}
	if got[0].Kind != FindingUnknownReceiverType {
		t.Errorf("kind = %q, want unknown_receiver_type", got[0].Kind)
	}
	if got[0].Severity != SeverityError {
		t.Errorf("severity = %q, want error", got[0].Severity)
	}
	if !strings.Contains(got[0].Message, "supported:") {
		t.Errorf("message %q should list supported types", got[0].Message)
	}
}

func TestRouting_KnownTypesAccepted(t *testing.T) {
	cases := map[string]map[string]any{
		"webhook":    {"type": "webhook", "url": "https://x"},
		"email":      {"type": "email", "to": "a@b.c", "smarthost": "smtp:25"},
		"slack":      {"type": "slack", "api_url": "https://hooks.slack/x"},
		"teams":      {"type": "teams", "webhook_url": "https://teams/x"},
		"rocketchat": {"type": "rocketchat", "url": "https://rc/x"},
		"pagerduty":  {"type": "pagerduty", "service_key": "abc"},
	}
	for name, recv := range cases {
		t.Run(name, func(t *testing.T) {
			got := runWithRouting(t, "t1", map[string]any{"receiver": recv})
			for _, f := range got {
				if f.Severity == SeverityError {
					t.Errorf("type %q produced error finding: %v", name, f)
				}
			}
		})
	}
}

// --- check 2: missing required receiver fields ---------------------

func TestRouting_MissingReceiverFieldsAreErrors(t *testing.T) {
	got := runWithRouting(t, "t1", map[string]any{
		"receiver": map[string]any{"type": "email"}, // missing `to` AND `smarthost`
	})
	if len(got) != 2 {
		t.Fatalf("got %d findings, want 2 (one per missing required field)", len(got))
	}
	for _, f := range got {
		if f.Kind != FindingMissingReceiverField {
			t.Errorf("kind = %q, want missing_receiver_field", f.Kind)
		}
		if f.Severity != SeverityError {
			t.Errorf("severity = %q, want error", f.Severity)
		}
	}
}

func TestRouting_EmptyStringFieldCountsAsMissing(t *testing.T) {
	got := runWithRouting(t, "t1", map[string]any{
		"receiver": map[string]any{"type": "webhook", "url": ""},
	})
	if len(got) != 1 {
		t.Fatalf("got %d findings, want 1", len(got))
	}
	if !strings.Contains(got[0].Message, "empty string") {
		t.Errorf("message %q should mention empty string", got[0].Message)
	}
}

func TestRouting_MissingReceiverEntirely(t *testing.T) {
	got := runWithRouting(t, "t1", map[string]any{}) // routing dict with no `receiver`
	if len(got) != 1 {
		t.Fatalf("got %d findings, want 1", len(got))
	}
	if got[0].Field != "receiver" {
		t.Errorf("field = %q, want `receiver`", got[0].Field)
	}
	if !strings.Contains(got[0].Message, "missing or not an object") {
		t.Errorf("message %q should explain the missing receiver", got[0].Message)
	}
}

func TestRouting_MissingReceiverType(t *testing.T) {
	got := runWithRouting(t, "t1", map[string]any{
		"receiver": map[string]any{"url": "https://x"},
	})
	if len(got) != 1 {
		t.Fatalf("got %d findings, want 1", len(got))
	}
	if got[0].Field != "receiver.type" {
		t.Errorf("field = %q, want receiver.type", got[0].Field)
	}
}

// --- check 3: empty override matcher --------------------------------

func TestRouting_EmptyOverrideMatcherIsError(t *testing.T) {
	r := fixtureRouting()
	r["overrides"] = []any{
		map[string]any{
			// no matcher fields, only a receiver — would shadow ALL alerts
			"receiver": map[string]any{"type": "pagerduty", "service_key": "abc"},
		},
	}
	got := runWithRouting(t, "t1", r)
	if len(got) != 1 {
		t.Fatalf("got %d findings, want 1", len(got))
	}
	if got[0].Kind != FindingEmptyOverrideMatcher {
		t.Errorf("kind = %q, want empty_override_matcher", got[0].Kind)
	}
	if got[0].Severity != SeverityError {
		t.Errorf("severity = %q, want error", got[0].Severity)
	}
	if got[0].Field != "overrides[0]" {
		t.Errorf("field = %q, want overrides[0]", got[0].Field)
	}
}

func TestRouting_OverrideWithMatcherAccepted(t *testing.T) {
	r := fixtureRouting()
	r["overrides"] = []any{
		map[string]any{
			"alertname": "HighCPU",
			"receiver":  map[string]any{"type": "pagerduty", "service_key": "abc"},
		},
	}
	got := runWithRouting(t, "t1", r)
	for _, f := range got {
		if f.Severity == SeverityError {
			t.Errorf("valid override produced error finding: %v", f)
		}
	}
}

// --- check 4: duplicate override matcher ---------------------------

func TestRouting_DuplicateOverrideMatcherIsWarning(t *testing.T) {
	r := fixtureRouting()
	r["overrides"] = []any{
		map[string]any{
			"alertname": "HighCPU",
			"receiver":  map[string]any{"type": "pagerduty", "service_key": "abc"},
		},
		map[string]any{
			"alertname": "HighCPU", // identical matcher → dead code
			"receiver":  map[string]any{"type": "slack", "api_url": "https://x"},
		},
	}
	got := runWithRouting(t, "t1", r)
	var dup []Finding
	for _, f := range got {
		if f.Kind == FindingDuplicateOverrideMatcher {
			dup = append(dup, f)
		}
	}
	if len(dup) != 1 {
		t.Fatalf("got %d duplicate-matcher findings, want 1", len(dup))
	}
	if dup[0].Severity != SeverityWarn {
		t.Errorf("severity = %q, want warn", dup[0].Severity)
	}
	if dup[0].Field != "overrides[1]" {
		t.Errorf("field = %q, want overrides[1] (the second/dead override)", dup[0].Field)
	}
	if !strings.Contains(dup[0].Message, "overrides[0]") {
		t.Errorf("message %q should reference the original index 0", dup[0].Message)
	}
}

func TestRouting_MatcherOrderIndependence(t *testing.T) {
	// Two overrides whose matcher KEYS are the same set but listed
	// in different YAML order should still hash to the same
	// canonical fingerprint and trigger the duplicate finding.
	r := fixtureRouting()
	r["overrides"] = []any{
		map[string]any{
			"alertname": "X",
			"severity":  "critical",
			"receiver":  map[string]any{"type": "pagerduty", "service_key": "k"},
		},
		map[string]any{
			"severity":  "critical", // keys reordered
			"alertname": "X",
			"receiver":  map[string]any{"type": "slack", "api_url": "https://x"},
		},
	}
	got := runWithRouting(t, "t1", r)
	for _, f := range got {
		if f.Kind == FindingDuplicateOverrideMatcher {
			return // success
		}
	}
	t.Errorf("expected duplicate-matcher finding for reordered identical matchers; got %v", got)
}

// --- check 5: redundant override receiver --------------------------

func TestRouting_RedundantOverrideReceiverIsWarning(t *testing.T) {
	mainReceiver := map[string]any{"type": "webhook", "url": "https://noc/x"}
	r := map[string]any{
		"receiver": mainReceiver,
		"overrides": []any{
			map[string]any{
				"alertname": "Spam",
				// Override receiver IDENTICAL to main — no routing effect
				"receiver": map[string]any{"type": "webhook", "url": "https://noc/x"},
			},
		},
	}
	got := runWithRouting(t, "t1", r)
	var redundant []Finding
	for _, f := range got {
		if f.Kind == FindingRedundantOverrideReceiver {
			redundant = append(redundant, f)
		}
	}
	if len(redundant) != 1 {
		t.Fatalf("got %d redundant-receiver findings, want 1", len(redundant))
	}
	if redundant[0].Severity != SeverityWarn {
		t.Errorf("severity = %q, want warn", redundant[0].Severity)
	}
}

func TestRouting_DifferentOverrideReceiverNotRedundant(t *testing.T) {
	r := map[string]any{
		"receiver": map[string]any{"type": "webhook", "url": "https://noc"},
		"overrides": []any{
			map[string]any{
				"alertname": "X",
				"receiver":  map[string]any{"type": "pagerduty", "service_key": "k"},
			},
		},
	}
	got := runWithRouting(t, "t1", r)
	for _, f := range got {
		if f.Kind == FindingRedundantOverrideReceiver {
			t.Errorf("different override receiver flagged as redundant: %v", f)
		}
	}
}

// --- input-shape edge cases ----------------------------------------

func TestRouting_NoOpWhenRoutingMapEmpty(t *testing.T) {
	r, err := CheckDefaultsImpact(CheckInput{
		EffectiveConfigs: map[string]map[string]any{"t1": {"x": 1}},
		// RoutingByTenant nil → routing checks skipped
	})
	if err != nil {
		t.Fatalf("CheckDefaultsImpact: %v", err)
	}
	if len(r.Findings) != 0 {
		t.Errorf("got %d findings; want 0 when no routing supplied", len(r.Findings))
	}
}

func TestRouting_NilTenantRoutingSkipped(t *testing.T) {
	// A tenant present in RoutingByTenant with a nil value should
	// be skipped silently — caller may have lost the routing data
	// for that tenant during merge but other tenants still need
	// checking. No finding emitted for the nil-tenant.
	got := checkRoutingGuardrails(CheckInput{
		RoutingByTenant: map[string]map[string]any{"t1": nil},
	})
	if len(got) != 0 {
		t.Errorf("got %d findings, want 0 for nil-tenant routing: %v", len(got), got)
	}
}

func TestRouting_OverrideNotAnObject(t *testing.T) {
	r := fixtureRouting()
	r["overrides"] = []any{"this is a string, not a map"}
	got := runWithRouting(t, "t1", r)
	if len(got) != 1 {
		t.Fatalf("got %d findings, want 1", len(got))
	}
	if !strings.Contains(got[0].Message, "not an object") {
		t.Errorf("message %q should explain the type mismatch", got[0].Message)
	}
}

// --- integration with run.go ---------------------------------------

func TestRouting_FailingTenantCountsTowardPassedTenantCount(t *testing.T) {
	// Routing errors should drop the tenant out of PassedTenantCount,
	// just like schema/required-field errors do.
	r, err := CheckDefaultsImpact(CheckInput{
		EffectiveConfigs: map[string]map[string]any{
			"good": {"placeholder": 1},
			"bad":  {"placeholder": 1},
		},
		RoutingByTenant: map[string]map[string]any{
			"good": fixtureRouting(),
			"bad":  {"receiver": map[string]any{"type": "telegram"}}, // unknown type
		},
	})
	if err != nil {
		t.Fatalf("CheckDefaultsImpact: %v", err)
	}
	if r.Summary.PassedTenantCount != 1 {
		t.Errorf("PassedTenantCount = %d, want 1 (only `good` should pass)", r.Summary.PassedTenantCount)
	}
}

// --- SSOT drift sentinel -------------------------------------------

// TestReceiverTypeSpecs_KeysMatchExpected is a static sentinel that
// guards against accidental edits to receiverTypeSpecs in routing.go.
// If you intentionally add/remove a receiver type here, update the
// expected list below to match _lib_constants.py::RECEIVER_TYPES.
//
// (A full file-level diff against _lib_constants.py would be more
// rigorous but adds Python parsing at test time. PR-1 keeps this as
// a Go-side sentinel; PR-3 may add a freshness CI gate that compares
// the two sources directly.)
func TestReceiverTypeSpecs_KeysMatchExpected(t *testing.T) {
	want := map[string]bool{
		"webhook":    true,
		"email":      true,
		"slack":      true,
		"teams":      true,
		"rocketchat": true,
		"pagerduty":  true,
	}
	if len(receiverTypeSpecs) != len(want) {
		t.Errorf("receiverTypeSpecs has %d types, want %d", len(receiverTypeSpecs), len(want))
	}
	for k := range want {
		if _, ok := receiverTypeSpecs[k]; !ok {
			t.Errorf("missing receiver type %q from receiverTypeSpecs", k)
		}
	}
}
