package config

// key_validation_unread_test.go — ADR-038 §11: tenant-api's write gate
// refuses an unread key on the shared key verdict, so KeyValidation marks the
// Errors entries that only judge "does any reader read this key" (Unread)
// and WithoutUnread drops exactly those, keeping every value check.
//
// Tenant ids are fixture names (CLAUDE.md #9).

import (
	"reflect"
	"strings"
	"testing"
)

func unreadFixture() ThresholdConfig {
	return ThresholdConfig{
		Defaults:          map[string]float64{"cpu": 80},
		OptionalOverrides: []string{"mem"},
		Tenants: map[string]map[string]ScheduledValue{"t-a": {
			// who reads it — each is one Unread entry
			"cpu_typo":        {Default: "1"},
			"_myth":           {Default: "1"},
			"_routing_profle": {Default: "x"},
			`disk{mount="/"}`: {Default: "1"},
			"disk_critical":   {Default: "1"},
			"mem_critical":    {Default: "1"},
			// value checks — never Unread
			"cpu":          {Default: "90", Expiry: &ExpiryMeta{Expires: "not-a-time"}},
			"_silent_mode": {Default: "purple"},
		}},
	}
}

func TestKeyValidation_UnreadIsEveryWhoReadsItError(t *testing.T) {
	t.Parallel()
	c := unreadFixture()
	v := c.ValidateTenantKeys()
	want := map[string]string{
		"cpu_typo":        "not in defaults",
		"_myth":           "unknown reserved key",
		"_routing_profle": "unknown routing key",
		`disk{mount="/"}`: "unknown base metric",
		"disk_critical":   "has no base metric",
		"mem_critical":    "cannot be set",
	}
	if len(v.Unread) != len(want) {
		t.Fatalf("Unread = %q, want one entry for each of %v", v.Unread, want)
	}
	for key, phrase := range want {
		found := false
		for _, m := range v.Unread {
			if strings.Contains(m, phrase) && strings.Contains(m, strings.ReplaceAll(key, `"`, `\"`)) {
				found = true
			}
		}
		if !found {
			t.Errorf("no Unread entry for %s (%q) in %q", key, phrase, v.Unread)
		}
	}
	for _, m := range v.Unread {
		if !containsString(v.Errors, m) {
			t.Errorf("Unread entry %q is not in Errors: a caller that does not drop it must stay strict", m)
		}
	}
}

func TestKeyValidation_WithoutUnreadKeepsTheValueChecks(t *testing.T) {
	t.Parallel()
	c := unreadFixture()
	v := c.ValidateTenantKeys()
	got := v.WithoutUnread()
	if len(got.Unread) != 0 {
		t.Errorf("WithoutUnread kept Unread %q", got.Unread)
	}
	if len(got.Errors) != len(v.Errors)-len(v.Unread) {
		t.Fatalf("Errors = %q, want %d entries (all minus Unread)", got.Errors, len(v.Errors)-len(v.Unread))
	}
	for _, phrase := range []string{"invalid `expires:`", "_silent_mode"} {
		found := false
		for _, e := range got.Errors {
			if strings.Contains(e, phrase) {
				found = true
			}
		}
		if !found {
			t.Errorf("value check %q dropped: %q", phrase, got.Errors)
		}
	}
	if !reflect.DeepEqual(got.Values, v.Values) {
		t.Errorf("Values changed: %v, want %v", got.Values, v.Values)
	}
	// ByTenant and Ratchet carry Unread, so the write gate's order
	// (ValidateTenantKeysByTenant → Ratchet → WithoutUnread) drops the same.
	// (Order is the map walk's: compared sorted.)
	byTenant := c.ValidateTenantKeysByTenant().Ratchet(PriorValues(nil)).WithoutUnread()
	if !reflect.DeepEqual(sortedCopy(byTenant.Tenants["t-a"].Errors), sortedCopy(got.Errors)) {
		t.Errorf("by tenant, after Ratchet: Errors = %q, want %q", byTenant.Tenants["t-a"].Errors, got.Errors)
	}
}

func containsString(list []string, s string) bool {
	for _, e := range list {
		if e == s {
			return true
		}
	}
	return false
}

// ADR-038 §2: the `version` label is a value check, so a dimensional key
// whose base has no platform value still has its label judged — dropping
// the unread line must not drop that.
func TestKeyValidation_VersionLabelIsJudgedWhateverTheBase(t *testing.T) {
	t.Parallel()
	for _, key := range []string{`nosuch{version="default"}`, `nosuch2{version=~"v.*"}`} {
		c := ThresholdConfig{
			Defaults: map[string]float64{"cpu": 80},
			Tenants:  map[string]map[string]ScheduledValue{"t-a": {key: {Default: "3"}}},
		}
		got := c.ValidateTenantKeys().WithoutUnread()
		if len(got.Errors) == 0 {
			t.Errorf("%s: no error after WithoutUnread: the version label went unjudged", key)
		}
	}
}
