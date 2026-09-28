package pyyamlcompat

import (
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"gopkg.in/yaml.v3"
)

type duplicateRow struct {
	Name      string  `json:"name"`
	YAML      string  `json:"yaml"`
	Duplicate *string `json:"duplicate"`
}

// TestFindDuplicateKey_MatchesStrictLoader holds FindDuplicateKeyIn to the
// route generator's StrictLoader on every row of
// testdata/pyyaml_duplicate_keys.json (the verdict is the StrictLoader's own;
// tests/shared/test_pyyaml_duplicate_key_table.py generates and pins it,
// REGEN_PYYAML_DUPLICATES=1): the same files refused, naming the same key.
func TestFindDuplicateKey_MatchesStrictLoader(t *testing.T) {
	data, err := os.ReadFile(filepath.Join("testdata", "pyyaml_duplicate_keys.json"))
	if err != nil {
		t.Fatalf("read table: %v", err)
	}
	var rows []duplicateRow
	if err := json.Unmarshal(data, &rows); err != nil || len(rows) < 30 {
		t.Fatalf("parse table: %v (n=%d)", err, len(rows))
	}
	refused := 0
	for _, r := range rows {
		t.Run(r.Name, func(t *testing.T) {
			dec := yaml.NewDecoder(strings.NewReader(r.YAML))
			for {
				var doc yaml.Node
				if err := dec.Decode(&doc); err != nil {
					if !errors.Is(err, io.EOF) {
						t.Fatalf("yaml.v3 cannot parse the row: %v", err)
					}
					break
				}
			}
			got := FindDuplicateKeyIn([]byte(r.YAML))
			switch {
			case r.Duplicate == nil && got != nil:
				t.Errorf("StrictLoader accepts, FindDuplicateKeyIn refuses: %v", got)
			case r.Duplicate != nil && got == nil:
				t.Errorf("StrictLoader refuses (key %q), FindDuplicateKeyIn accepts", *r.Duplicate)
			case r.Duplicate != nil && got.Key != *r.Duplicate:
				t.Errorf("key %q, StrictLoader names %q", got.Key, *r.Duplicate)
			}
		})
		if r.Duplicate != nil {
			refused++
		}
	}
	if refused == 0 || refused == len(rows) {
		t.Fatalf("table has %d refused of %d rows: both verdicts must be present", refused, len(rows))
	}
}

// TestFindDuplicateKey_Position names where the second occurrence is written.
func TestFindDuplicateKey_Position(t *testing.T) {
	d := FindDuplicateKeyIn([]byte("t:\n  &r receiver : x\n  *r : y\n"))
	if d == nil || d.Key != "receiver" || d.Line != 3 || d.Column != 3 || d.FirstLine != 2 {
		t.Fatalf("got %+v", d)
	}
	if !strings.Contains(d.Error(), `"receiver"`) {
		t.Errorf("message %q does not name the key", d.Error())
	}
}

// TestFindDuplicateKey_NilAndUnparsable: nothing to judge is no duplicate.
func TestFindDuplicateKey_NilAndUnparsable(t *testing.T) {
	if d := FindDuplicateKey(nil); d != nil {
		t.Errorf("nil node: %v", d)
	}
	if d := FindDuplicateKeyIn([]byte("a: [unclosed\n")); d != nil {
		t.Errorf("syntax error: %v", d)
	}
	if d := FindDuplicateKeyIn(nil); d != nil {
		t.Errorf("empty: %v", d)
	}
}
