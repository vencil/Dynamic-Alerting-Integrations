package keyverdict

// The gates' rules (blocks.go): which severities block, what a whole-tree
// gate refuses, and what a write adds. Tenant ids are fixture names
// (CLAUDE.md #9).

import (
	"reflect"
	"strings"
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

// tree is a verdict of findings, with the tenants' own entries: "t1 k a.yaml"
// says t1's own entry writes k in a.yaml.
func tree(findings []Finding, own ...string) Tree {
	t := Tree{Findings: findings, Keys: map[string][]KeyVerdict{}}
	for _, o := range own {
		parts := strings.Fields(o)
		file, layer := parts[2], "tenant"
		t.Keys[parts[0]] = append(t.Keys[parts[0]], KeyVerdict{Tenant: parts[0], Key: parts[1], File: &file, Layer: &layer})
	}
	return t
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
		before, after Tree
		written       []string
		want          []string
	}{
		{"a new error in a written file", tree(nil),
			tree([]Finding{finding(SeverityError, "k", "a.yaml", "t1")}, "t1 k a.yaml"), []string{"a.yaml"},
			[]string{"t1 k a.yaml"}},
		{"one in a file the write does not write", tree(nil),
			tree([]Finding{finding(SeverityError, "k", "a.yaml", "t1")}, "t1 k a.yaml"), []string{"b.yaml"},
			[]string{}},
		{"one the tree had", tree([]Finding{finding(SeverityError, "k", "a.yaml", "t1")}, "t1 k a.yaml"),
			tree([]Finding{finding(SeverityError, "k", "a.yaml", "t1")}, "t1 k a.yaml"), []string{"a.yaml"},
			[]string{}},
		// A second tenant writing the same misspelt key into the same file is new.
		{"the same key for another tenant of the file",
			tree([]Finding{finding(SeverityError, "k", "a.yaml", "t1")}, "t1 k a.yaml"),
			tree([]Finding{finding(SeverityError, "k", "a.yaml", "t1", "t2")}, "t1 k a.yaml", "t2 k a.yaml"),
			[]string{"a.yaml"}, []string{"t2 k a.yaml"}},
		// A tenant moved from a root platform file's tenants: into its own file.
		{"a tenant moved to another file keeps its baseline",
			tree([]Finding{finding(SeverityError, "k", "_platform.yaml", "t1")}, "t1 k _platform.yaml"),
			tree([]Finding{finding(SeverityError, "k", "t1.yaml", "t1")}, "t1 k t1.yaml"), []string{"t1.yaml"},
			[]string{}},
		// A profile the tenant binds writes the misspelling its own file
		// already had: the profile's is new (the tenant's own is no excuse).
		{"a profile's new error beside the tenant's own",
			tree([]Finding{finding(SeverityError, "k", "a.yaml", "t1")}, "t1 k a.yaml"),
			tree([]Finding{finding(SeverityError, "k", "_profiles.yaml", "t1"), finding(SeverityError, "k", "a.yaml", "t1")},
				"t1 k a.yaml"),
			[]string{"_profiles.yaml"}, []string{"t1 k _profiles.yaml"}},
		// A profile no tenant binds: the file and the key.
		{"an unbound profile's key the file had", tree([]Finding{finding(SeverityError, "k", "_profiles.yaml")}),
			tree([]Finding{finding(SeverityError, "k", "_profiles.yaml")}), []string{"_profiles.yaml"}, []string{}},
		{"an unbound profile's new key", tree(nil),
			tree([]Finding{finding(SeverityError, "k", "_profiles.yaml")}), []string{"_profiles.yaml"},
			[]string{" k _profiles.yaml"}},
		// The write unbinds the profile's last tenant: its error was there.
		{"unbinding the last tenant of a profile with an error",
			tree([]Finding{finding(SeverityError, "k", "_profiles.yaml", "t1")}),
			tree([]Finding{finding(SeverityError, "k", "_profiles.yaml")}), []string{"_profiles.yaml", "a.yaml"},
			[]string{}},
		// One root platform file holds a profile and the tenant's entry with
		// the same key: one finding, read as the tenant's own entry or as
		// the profile's as the tenant's effective source moves (#3016 review).
		{"a merged finding read as the profile's after a fix of the entry",
			tree([]Finding{finding(SeverityError, "k", "_p.yaml", "t1")}, "t1 k _p.yaml"),
			tree([]Finding{finding(SeverityError, "k", "_p.yaml", "t1")}), []string{"_p.yaml"}, []string{}},
		{"a merged finding read as the entry's after it was the profile's",
			tree([]Finding{finding(SeverityError, "k", "_p.yaml", "t1")}),
			tree([]Finding{finding(SeverityError, "k", "_p.yaml", "t1")}, "t1 k _p.yaml"), []string{"_p.yaml"}, []string{}},
		// A tenant bound to a profile already wrong: the file had it.
		{"binding a tenant to a profile already wrong", tree([]Finding{finding(SeverityError, "k", "_profiles.yaml")}),
			tree([]Finding{finding(SeverityError, "k", "_profiles.yaml", "t1")}), []string{"_profiles.yaml", "a.yaml"},
			[]string{}},
		{"a warning is never a block", tree(nil),
			tree([]Finding{finding(SeverityWarning, "k", "a.yaml", "t1")}, "t1 k a.yaml"), []string{"a.yaml"},
			[]string{}},
	}
	for _, tc := range cases {
		got := keys(NewBlocks(tc.before, tc.after, written(tc.written...)))
		if !reflect.DeepEqual(got, tc.want) {
			t.Errorf("%s: %q, want %q", tc.name, got, tc.want)
		}
	}
}
