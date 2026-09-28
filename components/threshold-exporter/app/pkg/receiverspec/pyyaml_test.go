package receiverspec

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	"gopkg.in/yaml.v3"
)

// TestPyYAMLImplicitKind_MatchesPyYAML holds pyyamlImplicitKind to PyYAML's
// own verdicts (#2295). testdata/pyyaml_plain_scalars.json is PyYAML
// SafeLoader's resolved tag for each candidate plain scalar
// (tests/shared/test_receiver_spec_parity.py generates and pins it). Each
// candidate is decoded here the way da-guard and tenant-api decode it
// (yaml.v3 into `any`):
//
//   - PyYAML does not read a string ⇒ Go does not end up with an accepted
//     string either: yaml.v3 fails or types it (non-string: refused), or
//     pyyamlImplicitKind names it. Otherwise the route generator would skip
//     a receiver da-guard and tenant-api accepted.
//   - Go gets a string that pyyamlImplicitKind names ⇒ PyYAML does not read
//     a string: the rule refuses no plain text PyYAML keeps.
func TestPyYAMLImplicitKind_MatchesPyYAML(t *testing.T) {
	data, err := os.ReadFile(filepath.Join("testdata", "pyyaml_plain_scalars.json"))
	if err != nil {
		t.Fatalf("read table: %v", err)
	}
	var rows []struct {
		Text   string `json:"text"`
		PyYAML string `json:"pyyaml"`
	}
	if err := json.Unmarshal(data, &rows); err != nil || len(rows) < 100 {
		t.Fatalf("parse table: %v (n=%d)", err, len(rows))
	}
	named := 0
	for _, r := range rows {
		var doc map[string]any
		goString, isString := "", false
		if yaml.Unmarshal([]byte("v: "+r.Text), &doc) == nil {
			goString, isString = doc["v"].(string)
		}
		kind := ""
		if isString {
			kind = pyyamlImplicitKind(goString)
		}
		if kind != "" {
			named++
		}
		pyString := r.PyYAML == "str"
		if !pyString && isString && kind == "" {
			t.Errorf("%q: PyYAML reads %s, yaml.v3 a string pyyamlImplicitKind accepts — the generator would skip what da-guard passes", r.Text, r.PyYAML)
		}
		if pyString && kind != "" {
			t.Errorf("%q: PyYAML reads a string, but pyyamlImplicitKind calls it %s", r.Text, kind)
		}
	}
	if named == 0 {
		t.Fatal("no candidate reached pyyamlImplicitKind as a string it names; the table no longer exercises it")
	}
}
