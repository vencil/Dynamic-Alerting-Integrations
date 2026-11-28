package config

import "testing"

// #4331: each condition of RedundancyJudgedByValue, on the registry's own keys.
func TestRedundancyJudgedByValue_FollowsTheRegistry(t *testing.T) {
	t.Parallel()
	for k, want := range map[string]bool{
		"_silent_mode":       true,
		"_state_maintenance": true,
		// A second reader (the custom-alert compiler), in all three layers.
		"_custom_alerts": false,
		// The route generator reads it too; and not in a profile.
		"_severity_dedup":  false,
		"_routing_profile": false,
		// Not read in a profile.
		"_profile": false,
		// A list and a mapping.
		"_expected_exporters": false,
		"_metadata":           false,
		// Not registered.
		"_silent_mode_critical": false,
	} {
		if got := RedundancyJudgedByValue(k); got != want {
			t.Errorf("RedundancyJudgedByValue(%q) = %v, want %v", k, got, want)
		}
	}
}
