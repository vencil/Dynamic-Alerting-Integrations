package config

import (
	"reflect"
	"testing"
)

// #4031: the `_` keys of the root `defaults:` no reader reads — the ones
// RootDefaultsReservedKey leaves out — and nothing else.
func TestRootDefaultsUnreadKeys(t *testing.T) {
	t.Parallel()
	for _, c := range []struct {
		name, doc string
		want      []string
	}{
		{"unregistered and merge-dropped", "defaults:\n  mysql_connections: 80\n  _bogus: 1\n  _metadata: x\n" +
			"  _silent_mode: warning\n  _routing_profile: x\n  _routingProfile: x\n  _nulled: ~\n",
			[]string{"_bogus", "_metadata"}},
		{"no `_` key", "defaults:\n  mysql_connections: 80\n", nil},
		{"no defaults: mapping", "_bogus: 1\nmysql_connections: 80\n", nil},
		{"does not decode", "defaults: [\n", nil},
	} {
		if got := RootDefaultsUnreadKeys([]byte(c.doc)); !reflect.DeepEqual(got, c.want) {
			t.Errorf("%s: %q, want %q", c.name, got, c.want)
		}
	}
	// The two sets partition the root's `_` keys a value is written for:
	// a key in one is never in the other.
	for _, k := range RootDefaultsUnreadKeys([]byte("defaults:\n  _bogus: 1\n  _metadata: x\n")) {
		if RootDefaultsReservedKey(k) {
			t.Errorf("%s is both unread here and root_defaults_reserved_key's", k)
		}
	}
}
