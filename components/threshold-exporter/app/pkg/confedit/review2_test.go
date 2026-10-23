package confedit

import "testing"

// TestVerify_AnEmptiedMappingIsOnlyRelaxedForItsStyle: the one leeway for
// the mapping an unset empties is block-or-flow. Each case would pass if
// that leeway also covered what it names.
func TestVerify_AnEmptiedMappingIsOnlyRelaxedForItsStyle(t *testing.T) {
	unsetTA := Edit{Path: []string{"t", "a"}, Op: OpUnset}
	cases := []struct {
		name, base, merged string
		edits              []Edit
		want               DiffKind
	}{
		// Not emptied by an unset: a set whose key went missing.
		{"emptied under a set", "t:\n  a: \"1\"\n", "t: {}\n",
			[]Edit{{Path: []string{"t", "a"}, Op: OpSet}}, DiffNode},
		// The tag, written both times, differs.
		{"another tag", "t: !a\n  a: \"1\"\n", "t: !b {}\n", []Edit{unsetTA}, DiffNode},
		// The written tag dropped.
		{"the written tag dropped", "t: !!map\n  a: \"1\"\n", "t: {}\n", []Edit{unsetTA}, DiffNode},
		// The anchor moved off the emptied mapping onto a node another edit
		// sets: the file still defines one `x`, in the same place in order.
		{"its anchor moved into another edit", "t: &x\n  a: \"1\"\nu:\n  k: \"1\"\n",
			"t: {}\nu:\n  k: &x \"2\"\n", []Edit{unsetTA, {Path: []string{"u", "k"}, Op: OpSet}}, DiffNode},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			d := Verify([]byte(tc.base), []byte(tc.merged), tc.edits)
			if d == nil || d.Kind != tc.want {
				t.Fatalf("Verify = %v, want %s", d, tc.want)
			}
		})
	}
	// The leeway itself.
	if d := Verify([]byte("t:\n  a: \"1\"\n"), []byte("t: {}\n"), []Edit{unsetTA}); d != nil {
		t.Fatalf("emptied by its unset: %s", d)
	}
}
