package config

// key_validation_unread_test.go — ADR-038 §11: whether any reader reads a
// key is the shared key verdict's, so ValidateTenantKeys judges no key name
// and keeps every value check.
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
			// who reads it — the shared key verdict's, not judged here
			"cpu_typo":        {Default: "1"},
			"_myth":           {Default: "1"},
			"_routing_profle": {Default: "x"},
			`disk{mount="/"}`: {Default: "1"},
			"disk_critical":   {Default: "1"},
			"mem_critical":    {Default: "1"},
			// value checks — judged here
			"cpu":          {Default: "90", Expiry: &ExpiryMeta{Expires: "not-a-time"}},
			"_silent_mode": {Default: "purple"},
		}},
	}
}

func TestKeyValidation_JudgesNoKeyName(t *testing.T) {
	t.Parallel()
	c := unreadFixture()
	v := c.ValidateTenantKeys()
	for _, key := range []string{"cpu_typo", "_myth", "_routing_profle", `disk{mount=\"/\"}`, "disk_critical", "mem_critical"} {
		for _, e := range append(append([]string{}, v.Errors...), v.Notices...) {
			if strings.Contains(e, key) {
				t.Errorf("%s: judged here (%q) — whether a reader reads it is the shared key verdict's", key, e)
			}
		}
	}
	for _, phrase := range []string{"invalid `expires:`", "_silent_mode"} {
		found := false
		for _, e := range v.Errors {
			if strings.Contains(e, phrase) {
				found = true
			}
		}
		if !found {
			t.Errorf("value check %q dropped: %q", phrase, v.Errors)
		}
	}
	if len(v.Errors) != 2 {
		t.Errorf("Errors = %q, want the two value checks only", v.Errors)
	}
	// ByTenant and Ratchet say the same about the tenant.
	byTenant := c.ValidateTenantKeysByTenant().Ratchet(PriorValues(nil))
	if !reflect.DeepEqual(sortedCopy(byTenant.Tenants["t-a"].Errors), sortedCopy(v.Errors)) {
		t.Errorf("by tenant, after Ratchet: Errors = %q, want %q", byTenant.Tenants["t-a"].Errors, v.Errors)
	}
}

// ADR-038 §2: the `version` label is a value check, so a dimensional key
// whose base has no platform value still has its label judged.
func TestKeyValidation_VersionLabelIsJudgedWhateverTheBase(t *testing.T) {
	t.Parallel()
	for _, key := range []string{`nosuch{version="default"}`, `nosuch2{version=~"v.*"}`} {
		c := ThresholdConfig{
			Defaults: map[string]float64{"cpu": 80},
			Tenants:  map[string]map[string]ScheduledValue{"t-a": {key: {Default: "3"}}},
		}
		got := c.ValidateTenantKeys()
		if len(got.Errors) == 0 {
			t.Errorf("%s: no error: the version label went unjudged", key)
		}
	}
}

// ADR-038 decision 6, §11: a platform file's or a profile's key no reader
// reads is reported by the shared key verdict, not as a notice here; its
// value problems still are.
func TestKeyValidation_PlatformAndProfileUnreadAreNoNotice(t *testing.T) {
	t.Parallel()
	files := map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
		"_platform.yaml": "tenants:\n  tx:\n    platform_typo: \"1\"\n    _silent_mode: purple\n",
		"_profiles.yaml": "profiles:\n  strict:\n    profile_typo: \"1\"\n",
	}
	kv, _ := profileKV(t, files, "tenants:\n  tx:\n    _profile: strict\n")
	all := strings.Join(kv.Notices, "\n")
	if strings.Contains(all, "platform_typo") || strings.Contains(all, "profile_typo") {
		t.Errorf("an unread platform or profile key is a notice: %q", kv.Notices)
	}
	if !strings.Contains(all, "purple") {
		t.Errorf("the platform entry's value problem is no longer a notice: %q", kv.Notices)
	}
}
