package config

// SubtreeReservedReported (#3881): the one filter da-guard's main check
// (subtree_default_reserved_key) and the shared key verdict
// (subtree_not_applied) both read. Tenant ids are fixture names.

import (
	"reflect"
	"testing"
)

func TestSubtreeReservedReported(t *testing.T) {
	t.Parallel()
	const wrapped, unwrapped, dropped = "w/_defaults.yaml", "u/_defaults.yaml", "d/_defaults.yaml"
	files := []DefaultsFile{
		{Name: wrapped, Data: []byte("defaults:\n  _policies: []\n  _silent_mode: all\n")},
		{Name: unwrapped, Data: []byte("_policies: []\n_silent_mode: all\n")},
		{Name: dropped, Data: []byte("_silent_mode: all\nx: !!binary aGk=\n")},
	}
	w := func(file string) SubtreeReservedWrite { return SubtreeReservedWrite{File: file, Value: "all"} }
	reserved := func() map[string]map[string][]SubtreeReservedWrite {
		return map[string]map[string][]SubtreeReservedWrite{
			// A TopLevelReadElsewhere key: reported under `defaults:` only.
			"t1": {"_policies": {w(wrapped), w(unwrapped)}},
			// Any other reserved key: reported in both shapes of file.
			"t2": {"_silent_mode": {w(wrapped), w(unwrapped)}},
			// A dropped file writes nothing; the key's other write stays.
			"t3": {"_silent_mode": {w(dropped), w(wrapped)}},
			// A key (and so a tenant) with no write left is not listed.
			"t4": {"_silent_mode": {w(dropped)}, "_policies": {w(unwrapped)}},
			// A file the caller did not hand in is kept, not guessed.
			"t5": {"_policies": {w("unknown/_defaults.yaml")}},
		}
	}
	in := reserved()
	got := SubtreeReservedReported(in, files, []string{dropped})
	want := map[string]map[string][]SubtreeReservedWrite{
		"t1": {"_policies": {w(wrapped)}},
		"t2": {"_silent_mode": {w(wrapped), w(unwrapped)}},
		"t3": {"_silent_mode": {w(wrapped)}},
		"t5": {"_policies": {w("unknown/_defaults.yaml")}},
	}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("reported\n got %v\nwant %v", got, want)
	}
	if !reflect.DeepEqual(in, reserved()) {
		t.Errorf("the input was modified: %v", in)
	}

	// Control: with nothing dropped, the dropped file's writes are reported —
	// and, being unwrapped once it parses, not for `_policies`.
	got = SubtreeReservedReported(reserved(), files, nil)
	if !reflect.DeepEqual(got["t4"], map[string][]SubtreeReservedWrite{"_silent_mode": {w(dropped)}}) ||
		!reflect.DeepEqual(got["t3"]["_silent_mode"], []SubtreeReservedWrite{w(dropped), w(wrapped)}) {
		t.Errorf("nothing dropped: t3 %v t4 %v", got["t3"], got["t4"])
	}
	// With no file data at all nothing is known to be unwrapped: both
	// `_policies` writes are kept (reported), not guessed away.
	if got := SubtreeReservedReported(reserved(), nil, nil); !reflect.DeepEqual(got["t1"]["_policies"],
		[]SubtreeReservedWrite{w(wrapped), w(unwrapped)}) {
		t.Errorf("no file data: %v, want both writes kept", got["t1"])
	}

	if got := SubtreeReservedReported(nil, files, nil); got != nil {
		t.Errorf("no reserved key: %v, want nil", got)
	}
	if got := SubtreeReservedReported(map[string]map[string][]SubtreeReservedWrite{
		"t1": {"_silent_mode": {w(dropped)}}}, files, []string{dropped}); got != nil {
		t.Errorf("every write dropped: %v, want nil", got)
	}
}

// The readElsewhere set is the registry's (topLevelReadElsewhere), not a
// copy: every key of it is filtered, and no routing key reaches this function
// from subtreeReservedKeys in the first place.
func TestSubtreeReservedReported_FiltersEveryTopLevelReadElsewhereKey(t *testing.T) {
	t.Parallel()
	const unwrapped = "u/_defaults.yaml"
	files := []DefaultsFile{{Name: unwrapped, Data: []byte("x: 1\n")}}
	if len(topLevelReadElsewhere) == 0 {
		t.Fatal("topLevelReadElsewhere is empty: the registry was not generated")
	}
	for _, k := range topLevelReadElsewhere {
		reserved := map[string]map[string][]SubtreeReservedWrite{"t1": {k: {{File: unwrapped, Value: 1}}}}
		if got := SubtreeReservedReported(reserved, files, nil); got != nil {
			t.Errorf("%s at the top level of an unwrapped file: %v, want nothing reported", k, got)
		}
	}
}
