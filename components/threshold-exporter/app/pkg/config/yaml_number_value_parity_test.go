package config

// yaml_number_value_parity_test.go — Go's half of
// tests/shared/yaml_number_value_matrix.json (#2415).
//
// yaml.v3 types an unquoted scalar by YAML 1.2's core schema (plus its own
// leniencies: `1_000`, `0b101`), PyYAML by YAML 1.1: `1e3` / `0o17` are
// numbers here and strings there, `12:30:45` the reverse. describe_tenant.py
// now reads int / float values by yaml.v3's rules and writes floats as
// encoding/json does, so its merged_hash matches the exporter's; the Python
// half (tests/shared/test_yaml_number_value_parity.py) asserts that against
// the `go_json` column this test derives from the exporter's own merge.

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"testing"
)

type yamlNumberValueMatrix struct {
	Comment []string `json:"_comment"`
	Rows    []struct {
		Source      string  `json:"source"`
		GoJSON      *string `json:"go_json"`
		GoType      string  `json:"go_type"`
		PyDivergent string  `json:"py_divergent"` // Python half only
	} `json:"rows"`
}

func TestYAMLNumberValueMatrix_GoJSONIsTheExportersEffectiveConfig(t *testing.T) {
	_, thisFile, _, _ := runtime.Caller(0)
	path := filepath.Join(filepath.Dir(thisFile), "..", "..", "..", "..", "..",
		"tests", "shared", "yaml_number_value_matrix.json")
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read matrix: %v", err)
	}
	var m yamlNumberValueMatrix
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&m); err != nil {
		t.Fatalf("parse matrix: %v", err)
	}
	if len(m.Rows) == 0 {
		t.Fatal("matrix has no rows — a vacuous table passes nothing")
	}
	// The same value in the tenant file, and in the `_defaults.yaml` under an
	// empty tenant: one effective config, so one `go_json` for both.
	sides := []struct {
		name     string
		tenant   func(src string) []byte
		defaults func(src string) [][]byte
	}{
		{"tenant", func(src string) []byte {
			return []byte("tenants:\n  t1:\n    _x:\n      v: " + src + "\n")
		}, func(string) [][]byte {
			return [][]byte{[]byte("defaults:\n  mysql_connections: 50\n")}
		}},
		{"defaults", func(string) []byte {
			return []byte("tenants:\n  t1: {}\n")
		}, func(src string) [][]byte {
			return [][]byte{[]byte("defaults:\n  mysql_connections: 50\n  _x:\n    v: " + src + "\n")}
		}},
	}
	for _, row := range m.Rows {
		for _, side := range sides {
			t.Run(side.name+"/"+row.Source, func(t *testing.T) {
				tenant, defaults := side.tenant(row.Source), side.defaults(row.Source)
				eff, err := ComputeEffectiveConfig(tenant, "t1", defaults)
				if row.GoJSON == nil {
					if err == nil {
						t.Fatalf("table says yaml.v3 refuses %q, it merged %v", row.Source, eff)
					}
					return
				}
				if err != nil {
					t.Fatalf("ComputeEffectiveConfig(%q): %v", row.Source, err)
				}
				got, err := CanonicalJSON(eff)
				if err != nil {
					t.Fatalf("CanonicalJSON: %v", err)
				}
				if string(got) != *row.GoJSON {
					t.Errorf("canonical JSON of %q:\n got %s\nwant %s", row.Source, got, *row.GoJSON)
				}
				x, _ := eff["_x"].(map[string]any)
				if gotType := fmt.Sprintf("%T", x["v"]); gotType != row.GoType {
					t.Errorf("type of %q: got %s, table says %s", row.Source, gotType, row.GoType)
				}
				hash, err := ComputeMergedHash(tenant, "t1", defaults)
				if err != nil {
					t.Fatalf("ComputeMergedHash: %v", err)
				}
				sum := sha256.Sum256([]byte(*row.GoJSON))
				if want := hex.EncodeToString(sum[:])[:16]; hash != want {
					t.Errorf("merged_hash of %q: got %s, sha256(go_json)[:16] is %s", row.Source, hash, want)
				}
			})
		}
	}
}
