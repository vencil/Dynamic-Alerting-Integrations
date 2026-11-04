package keyverdict

// The gates' rules (blocks.go): which severities block, what a whole-tree
// gate refuses, and what a write adds. Tenant ids are fixture names
// (CLAUDE.md #9).

import (
	"reflect"
	"testing"
)

func finding(sev, key, file string, tenants ...string) Finding {
	f := file
	return Finding{Severity: sev, Kind: KindUnread, Key: key, File: &f, Tenants: append([]string{}, tenants...),
		Blocks: Blocking(sev)}
}

func TestBlocking_OnlyAnErrorBlocks(t *testing.T) {
	t.Parallel()
	for sev, want := range map[string]bool{SeverityError: true, SeverityWarning: false, SeverityNotice: false} {
		if Blocking(sev) != want {
			t.Errorf("Blocking(%q) = %v", sev, !want)
		}
	}
}

// An undecidable key blocks a whole-tree gate (decision 7, #3016 option A):
// the verdict cannot say no reader reads it, nor that one does.
func TestBlocks_AnUndecidableKeyBlocks(t *testing.T) {
	t.Parallel()
	file := "a.yaml"
	tree := Tree{Keys: map[string][]KeyVerdict{"t1": {{Tenant: "t1", Key: "k", File: &file, Verdict: Undecidable}}}}
	got := Blocks(tree)
	if len(got) != 1 || got[0].Kind != Undecidable || got[0].Tenant != "t1" || got[0].File != file {
		t.Fatalf("blocks %+v", got)
	}
	if bs := Blocks(Tree{Findings: []Finding{finding(SeverityWarning, "k", "a.yaml", "t1")}}); len(bs) != 0 {
		t.Fatalf("a warning blocked: %+v", bs)
	}
}

func TestNewBlocks_WhatAWriteAdds(t *testing.T) {
	t.Parallel()
	written := func(files ...string) func(string) bool {
		return func(f string) bool {
			for _, w := range files {
				if w == f {
					return true
				}
			}
			return false
		}
	}
	keys := func(bs []Block) []string {
		out := []string{}
		for _, b := range bs {
			out = append(out, b.Tenant+" "+b.Key+" "+b.File)
		}
		return out
	}
	cases := []struct {
		name          string
		before, after []Finding
		written       []string
		want          []string
	}{
		{"a new error in a written file", nil,
			[]Finding{finding(SeverityError, "k", "a.yaml", "t1")}, []string{"a.yaml"}, []string{"t1 k a.yaml"}},
		{"one in a file the write does not write", nil,
			[]Finding{finding(SeverityError, "k", "a.yaml", "t1")}, []string{"b.yaml"}, []string{}},
		{"one the tree had", []Finding{finding(SeverityError, "k", "a.yaml", "t1")},
			[]Finding{finding(SeverityError, "k", "a.yaml", "t1")}, []string{"a.yaml"}, []string{}},
		// A second tenant writing the same misspelt key into the same file is new.
		{"the same key for another tenant of the file", []Finding{finding(SeverityError, "k", "a.yaml", "t1")},
			[]Finding{finding(SeverityError, "k", "a.yaml", "t1", "t2")}, []string{"a.yaml"}, []string{"t2 k a.yaml"}},
		// A tenant moved from a root platform file's tenants: into its own file.
		{"a tenant moved to another file keeps its baseline", []Finding{finding(SeverityError, "k", "_platform.yaml", "t1")},
			[]Finding{finding(SeverityError, "k", "t1.yaml", "t1")}, []string{"t1.yaml"}, []string{}},
		// A profile no tenant binds: the file and the key.
		{"an unbound profile's key the file had", []Finding{finding(SeverityError, "k", "_profiles.yaml")},
			[]Finding{finding(SeverityError, "k", "_profiles.yaml")}, []string{"_profiles.yaml"}, []string{}},
		{"an unbound profile's new key", nil,
			[]Finding{finding(SeverityError, "k", "_profiles.yaml")}, []string{"_profiles.yaml"}, []string{" k _profiles.yaml"}},
		{"a warning is never a block", nil,
			[]Finding{finding(SeverityWarning, "k", "a.yaml", "t1")}, []string{"a.yaml"}, []string{}},
	}
	for _, tc := range cases {
		got := keys(NewBlocks(Tree{Findings: tc.before}, Tree{Findings: tc.after}, written(tc.written...)))
		if !reflect.DeepEqual(got, tc.want) {
			t.Errorf("%s: %q, want %q", tc.name, got, tc.want)
		}
	}
}
