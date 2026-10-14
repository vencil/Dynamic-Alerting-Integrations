package config

import (
	"strings"
	"testing"
)

// #2983: ValidateTenantKeys names a `_routing`-prefixed tenant key the route
// generator does not read — tenant-api's write gate refuses on it — and stays
// silent on the two it reads. The reserved PREFIX used to pass all of them.
func TestValidateTenantKeys_UnknownRoutingKey(t *testing.T) {
	t.Parallel()
	for _, key := range []string{"_routing_profle", "_routing_receiver_type", "_routing_defaults", "_routingProfile"} {
		t.Run(key, func(t *testing.T) {
			t.Parallel()
			kv := declaredCfg(nil, map[string]ScheduledValue{key: {Default: "x"}}).ValidateTenantKeys()
			if len(kv.Errors) != 1 || !strings.Contains(kv.Errors[0], "unknown routing key") ||
				!strings.Contains(kv.Errors[0], key) {
				t.Errorf("errors = %v, want one naming %q", kv.Errors, key)
			}
		})
	}
	kv := declaredCfg(nil, map[string]ScheduledValue{
		"_routing": {Default: "disable"}, "_routing_profile": {Default: "p"}, "_state_maintenance": {Default: "enable"},
	}).ValidateTenantKeys()
	if len(kv.Errors) != 0 {
		t.Errorf("read keys: errors = %v, want none", kv.Errors)
	}
}

func TestIsUnknownRoutingKey(t *testing.T) {
	t.Parallel()
	for _, c := range []struct {
		key      string
		platform bool
		want     bool
	}{
		{"_routing", false, false}, {"_routing_profile", false, false},
		{"_routing_defaults", false, true}, {"_routing_profle", false, true},
		{"_routing_defaults", true, false}, {"_routing_enforced", true, false},
		{"_routing", true, true}, {"_routing_defualts", true, true},
		{"_routng", false, false}, {"routing", true, false},
	} {
		if got := IsUnknownRoutingKey(c.key, c.platform); got != c.want {
			t.Errorf("IsUnknownRoutingKey(%q, %v) = %v, want %v", c.key, c.platform, got, c.want)
		}
	}
}
