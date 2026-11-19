package keyverdict

import (
	"reflect"
	"strings"
	"testing"
)

// #4031: a `_` key of the root `defaults:` no reader reads is one unread
// error on the root carrier, naming no tenant — none takes its value —
// unless a tenant's verdict row already reports it there (a key a tenant
// inherits from the root). Whether every tenant was read does not change
// it (#4031 review: a write that brought a dropped file back would read a
// finding the tree always had as new).
func TestJudge_RootDefaultsUnreadKeysAreErrorsOnTheRootFile(t *testing.T) {
	t.Parallel()
	rd := &RootDefaultsKeys{File: "_defaults.yaml", Keys: []string{"_bogus", "_metadata"}}
	line := func(f Finding) string {
		return strings.Join([]string{f.Severity, f.Kind, f.Key, deref(f.File), deref(f.Reason), boolStr(f.Blocks)}, " ") +
			" [" + strings.Join(f.Tenants, ",") + "]"
	}
	want := []string{
		"error unread _bogus _defaults.yaml unregistered true []",
		"error unread _metadata _defaults.yaml merge_dropped true []",
	}
	for name, in := range map[string]Input{
		"no tenant": {RootDefaults: rd, Whole: true},
		"a tenant": {RootDefaults: rd, Whole: true, Served: map[string]ServedTenant{"ta": {}},
			Effective: map[string]EffectiveTenant{"ta": {DefaultsChain: []string{"_defaults.yaml"}}}},
		"a subset": {RootDefaults: rd},
	} {
		tree := Judge(in)
		var got []string
		for _, f := range tree.Findings {
			got = append(got, line(f))
			if f.Tenants == nil {
				t.Errorf("%s: %s: tenants is nil, want [] (the document writes [])", name, f.Key)
			}
		}
		if !reflect.DeepEqual(got, want) {
			t.Errorf("%s: findings %q, want %q", name, got, want)
		}
		if !tree.Blocks {
			t.Errorf("%s: tree.Blocks false", name)
		}
	}
	t.Run("once per file", func(t *testing.T) {
		// The root's key is reported by a tenant row on the root: not again.
		// The same key a tenant writes in its own file is that file's, and
		// the root's is still reported.
		have := []Finding{
			{Severity: SeverityError, Kind: KindUnread, Key: "_bogus", File: optional("_defaults.yaml"),
				Tenants: []string{"tb"}, Reason: optional(ReasonUnregistered)},
			{Severity: SeverityError, Kind: KindUnread, Key: "_metadata", File: optional("ta.yaml"),
				Tenants: []string{"ta"}, Reason: optional(ReasonUnregistered)},
		}
		got := rootDefaultsFindings(*rd, have)
		if len(got) != 1 || got[0].Key != "_metadata" || deref(got[0].File) != "_defaults.yaml" {
			t.Fatalf("findings %+v, want the root's _metadata alone", got)
		}
	})
	t.Run("a write", func(t *testing.T) {
		// The unit is the file and the key: adding one is a new block,
		// leaving it is none — whether or not every tenant was read.
		before := Judge(Input{RootDefaults: &RootDefaultsKeys{File: "_defaults.yaml"}, Whole: true})
		after := Judge(Input{RootDefaults: rd, Whole: true})
		all := func(string) bool { return true }
		if nb := NewBlocks(before, after, all); len(nb) != 2 {
			t.Fatalf("new blocks %+v, want two", nb)
		}
		if nb := NewBlocks(Judge(Input{RootDefaults: rd}), after, all); len(nb) != 0 {
			t.Fatalf("a tree that had the keys has new blocks %+v once every tenant is read", nb)
		}
	})
}
