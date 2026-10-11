package main

import (
	"strings"
	"testing"
)

// There is no default store: a missing or unknown --state-store, or dir mode
// without a directory, refuses to start rather than fall back to a local
// directory (#2856 — the old default lost these records on every restart).
func TestWireStateStore_RefusesWithoutAnExplicitStore(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct {
		f    stateFlags
		want string
	}{
		{stateFlags{}, "--state-store is required"},
		{stateFlags{Kind: "dir"}, "needs --state-dir"},
		{stateFlags{Kind: "file", Dir: t.TempDir()}, "must be configmap or dir"},
		{stateFlags{Kind: "dir", Dir: "/conf.d/state", ConfigDir: "/conf.d"}, "must lie outside --config-dir"},
		{stateFlags{Kind: "dir", Dir: "/conf.d", ConfigDir: "/conf.d/"}, "must lie outside --config-dir"},
	} {
		if _, err := wireStateStore(tc.f, nil); err == nil || !strings.Contains(err.Error(), tc.want) {
			t.Errorf("wireStateStore(%+v) err = %v, want %q", tc.f, err, tc.want)
		}
	}
	root := t.TempDir()
	s, err := wireStateStore(stateFlags{Kind: "dir", Dir: root + "/state", ConfigDir: root + "/conf.d"}, nil)
	if err != nil || s == nil || !strings.HasPrefix(s.Describe(), "dir ") {
		t.Fatalf("dir store: %v, %v", s, err)
	}
}
