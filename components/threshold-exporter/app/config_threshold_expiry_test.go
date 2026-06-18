package main

// Threshold expiry (PREVENT #656) — time-boxed overrides that fail-safe back to
// the platform default on expiry + surface da_config_event. v1: base standard
// metrics only (those in _defaults.yaml); reserved / dimensional / _critical /
// custom-alert keys are out of scope (ResolveThresholdExpiriesAt skips them and
// ValidateTenantKeys warns). Mirrors the silent/maintenance expiry test style.

import (
	"strings"
	"testing"
	"time"

	"gopkg.in/yaml.v3"
)

func TestScheduledValue_ParsesExpiresAndReason(t *testing.T) {
	t.Parallel()
	var sv ScheduledValue
	in := "default: \"2000\"\nexpires: \"2026-07-01T00:00:00Z\"\nreason: \"incident #1234\"\n"
	if err := yaml.Unmarshal([]byte(in), &sv); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if sv.Default != "2000" || sv.Expiry == nil ||
		sv.Expiry.Expires != "2026-07-01T00:00:00Z" || sv.Expiry.Reason != "incident #1234" {
		t.Fatalf("unexpected parse: %+v (expiry %+v)", sv, sv.Expiry)
	}
	// Scalar form carries no expiry (permanent threshold) → nil Expiry pointer.
	var scalar ScheduledValue
	if err := yaml.Unmarshal([]byte(`"80"`), &scalar); err != nil {
		t.Fatalf("unmarshal scalar: %v", err)
	}
	if scalar.Expiry != nil {
		t.Fatalf("scalar should have no expiry, got %+v", scalar.Expiry)
	}
}

func TestResolveBaseRows_ExpiredOverrideFailsSafeToDefault(t *testing.T) {
	t.Parallel()
	// The load-bearing safety property: an expired loosened override reverts to
	// the platform default (MORE protection), never goes silent, and stays
	// identical in cardinality to a present override (still emits one row).
	const past = "2020-01-01T00:00:00Z"
	const future = "2999-01-01T00:00:00Z"
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{"mysql_connections": 80},
		Tenants: map[string]map[string]ScheduledValue{
			"db-expired": {"mysql_connections": {Default: "2000", Expiry: &ExpiryMeta{Expires: past}}},   // expired → default 80
			"db-active":  {"mysql_connections": {Default: "2000", Expiry: &ExpiryMeta{Expires: future}}}, // not yet → 2000
			"db-perm":    {"mysql_connections": {Default: "2000"}},                                       // no expiry → 2000
		},
	}
	got := map[string]float64{}
	for _, r := range cfg.Resolve() {
		got[r.Tenant] = r.Value
	}
	if len(got) != 3 {
		t.Fatalf("expected one threshold per tenant (3), got %d: %+v", len(got), got)
	}
	if got["db-expired"] != 80 {
		t.Errorf("expired override must fail-safe to default 80, got %v", got["db-expired"])
	}
	if got["db-active"] != 2000 || got["db-perm"] != 2000 {
		t.Errorf("active/permanent overrides must keep 2000, got active=%v perm=%v", got["db-active"], got["db-perm"])
	}
}

func TestResolveThresholdExpiriesAt_ScopeAndState(t *testing.T) {
	t.Parallel()
	now := time.Date(2026, 6, 18, 0, 0, 0, 0, time.UTC)
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{"mysql_connections": 80, "mysql_cpu": 80},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {
				"mysql_connections":  {Default: "2000", Expiry: &ExpiryMeta{Expires: "2020-01-01T00:00:00Z", Reason: "old incident"}}, // expired (in Defaults)
				"mysql_cpu":          {Default: "95", Expiry: &ExpiryMeta{Expires: "2999-01-01T00:00:00Z"}},                           // active (in Defaults)
				"unmapped_widget":    {Default: "5", Expiry: &ExpiryMeta{Expires: "2020-01-01T00:00:00Z"}},                            // NOT in Defaults → excluded (v1)
				"mysql_cpu_critical": {Default: "99", Expiry: &ExpiryMeta{Expires: "2020-01-01T00:00:00Z"}},                           // _critical → not in Defaults → excluded
				"_custom_alerts":     SV("- recipe: x\n"),                                                                             // reserved → excluded
			},
		},
	}
	byKey := map[string]ResolvedThresholdExpiry{}
	for _, e := range cfg.ResolveThresholdExpiriesAt(now) {
		byKey[e.MetricKey] = e
	}
	if len(byKey) != 2 {
		t.Fatalf("expected 2 in-scope expiry entries (mysql_connections, mysql_cpu), got %d: %+v", len(byKey), byKey)
	}
	if e := byKey["mysql_connections"]; !e.Expired || e.Reason != "old incident" {
		t.Errorf("mysql_connections should be expired w/ reason, got %+v", e)
	}
	if byKey["mysql_cpu"].Expired {
		t.Errorf("mysql_cpu (future expires) should NOT be expired, got %+v", byKey["mysql_cpu"])
	}
	for _, excluded := range []string{"unmapped_widget", "mysql_cpu_critical", "_custom_alerts"} {
		if _, ok := byKey[excluded]; ok {
			t.Errorf("%q is out of v1 scope and must be excluded", excluded)
		}
	}
}

func TestValidateTenantKeys_ExpiresFailLoud(t *testing.T) {
	t.Parallel()
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{"mysql_connections": 80},
		Tenants: map[string]map[string]ScheduledValue{
			"db-ok":        {"mysql_connections": {Default: "2000", Expiry: &ExpiryMeta{Expires: "2026-07-01T00:00:00Z"}}}, // valid + in Defaults → no expires warn
			"db-malformed": {"mysql_connections": {Default: "2000", Expiry: &ExpiryMeta{Expires: "not-a-timestamp"}}},      // in Defaults, bad ts → warn
			"db-scope":     {"unmapped_widget": {Default: "5", Expiry: &ExpiryMeta{Expires: "2026-07-01T00:00:00Z"}}},      // out of scope → warn
		},
	}
	all := strings.Join(cfg.ValidateTenantKeys(), "\n")
	if !strings.Contains(all, "invalid `expires:`") || !strings.Contains(all, "db-malformed") {
		t.Errorf("expected malformed-expires warning for db-malformed, got:\n%s", all)
	}
	if !strings.Contains(all, "is ignored") || !strings.Contains(all, "db-scope") {
		t.Errorf("expected out-of-scope warning for db-scope, got:\n%s", all)
	}
	for _, line := range cfg.ValidateTenantKeys() {
		if strings.Contains(line, "db-ok") && strings.Contains(line, "expires") {
			t.Errorf("valid in-scope expires should not warn: %s", line)
		}
	}
}

func TestResolveBaseRows_ExpiryWinsOverTimeWindows(t *testing.T) {
	t.Parallel()
	// expires gates the WHOLE override: an expired override reverts to the default
	// even if it also carries time-window overrides (the window is never consulted).
	// A non-expired override still applies its window. Fixed `now` (inside the
	// window) keeps this deterministic.
	now := time.Date(2026, 6, 18, 5, 0, 0, 0, time.UTC) // inside 01:00-09:00
	win := []TimeWindowOverride{{Window: "01:00-09:00", Value: "3000"}}
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{"mysql_connections": 80},
		Tenants: map[string]map[string]ScheduledValue{
			"db-expired": {"mysql_connections": {Default: "2000", Overrides: win, Expiry: &ExpiryMeta{Expires: "2020-01-01T00:00:00Z"}}},
			"db-active":  {"mysql_connections": {Default: "2000", Overrides: win, Expiry: &ExpiryMeta{Expires: "2999-01-01T00:00:00Z"}}},
		},
	}
	rows, _ := cfg.ResolveAtWithStats(now)
	got := map[string]float64{}
	for _, r := range rows {
		got[r.Tenant] = r.Value
	}
	if got["db-expired"] != 80 {
		t.Errorf("expired override (despite a matching window) must revert to default 80, got %v", got["db-expired"])
	}
	if got["db-active"] != 3000 {
		t.Errorf("active override should apply its 01:00-09:00 window value 3000 at 05:00, got %v", got["db-active"])
	}
}
