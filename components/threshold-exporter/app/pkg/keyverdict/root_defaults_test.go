package keyverdict

import (
	"reflect"
	"strings"
	"testing"
)

// #4031: a `_` key of the root `defaults:` no reader reads is one unread
// error on the root carrier — naming the tenants that inherit the root, none
// when no tenant does — unless a tenant's verdict row already reports it
// there (an unregistered key every inheriting tenant reads as unread).
func TestJudge_RootDefaultsUnreadKeysAreErrorsOnTheRootFile(t *testing.T) {
	t.Parallel()
	rd := &RootDefaultsKeys{File: "_defaults.yaml", Keys: []string{"_bogus", "_metadata"}}
	line := func(f Finding) string {
		return strings.Join([]string{f.Severity, f.Kind, f.Key, deref(f.File), deref(f.Reason), boolStr(f.Blocks)}, " ") +
			" [" + strings.Join(f.Tenants, ",") + "]"
	}
	t.Run("no tenant", func(t *testing.T) {
		tree := Judge(Input{RootDefaults: rd, Whole: true})
		var got []string
		for _, f := range tree.Findings {
			got = append(got, line(f))
			if f.Tenants == nil {
				t.Errorf("%s: tenants is nil, want [] (the document writes [])", f.Key)
			}
		}
		want := []string{
			"error unread _bogus _defaults.yaml unregistered true []",
			"error unread _metadata _defaults.yaml merge_dropped true []",
		}
		if !reflect.DeepEqual(got, want) {
			t.Fatalf("findings %q, want %q", got, want)
		}
		if !tree.Blocks {
			t.Fatal("tree.Blocks false")
		}
	})
	t.Run("a tenant inherits the root", func(t *testing.T) {
		in := Input{
			RootDefaults: rd, Whole: true,
			Effective: map[string]EffectiveTenant{"ta": {DefaultsChain: []string{"_defaults.yaml"}}},
			Served:    map[string]ServedTenant{"ta": {}},
		}
		var got []string
		for _, f := range Judge(in).Findings {
			got = append(got, line(f))
		}
		want := []string{
			"error unread _bogus _defaults.yaml unregistered true [ta]",
			"error unread _metadata _defaults.yaml merge_dropped true [ta]",
		}
		if !reflect.DeepEqual(got, want) {
			t.Fatalf("findings %q, want %q", got, want)
		}
	})
	t.Run("not twice", func(t *testing.T) {
		have := []Finding{{Severity: SeverityError, Kind: KindUnread, Key: "_bogus",
			File: optional("_defaults.yaml"), Tenants: []string{"ta"}, Reason: optional(ReasonUnregistered)}}
		got := rootDefaultsFindings(*rd, have, map[string]string{"ta": "_defaults.yaml"})
		if len(got) != 1 || got[0].Key != "_metadata" {
			t.Fatalf("findings %+v, want _metadata's alone", got)
		}
	})
	t.Run("a subset", func(t *testing.T) {
		// A tenant left out may be the one whose row reports the key.
		if fs := Judge(Input{RootDefaults: rd}).Findings; len(fs) != 0 {
			t.Fatalf("findings %+v over a subset, want none", fs)
		}
	})
	t.Run("a write", func(t *testing.T) {
		// The unit is the file and the key: adding one is a new block,
		// leaving it is none.
		before := Judge(Input{RootDefaults: &RootDefaultsKeys{File: "_defaults.yaml"}, Whole: true})
		after := Judge(Input{RootDefaults: rd, Whole: true})
		all := func(string) bool { return true }
		if nb := NewBlocks(before, after, all); len(nb) != 2 {
			t.Fatalf("new blocks %+v, want two", nb)
		}
		if nb := NewBlocks(after, after, all); len(nb) != 0 {
			t.Fatalf("an unchanged tree has new blocks %+v", nb)
		}
	})
}
