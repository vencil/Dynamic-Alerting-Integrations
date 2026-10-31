package config

import (
	"testing"
)

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
