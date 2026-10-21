package config

// tenant_id_spelling_parity_test.go — Go's half of
// tests/shared/tenant_id_yaml_spelling_matrix.json (#2114).
//
// The exporter keys a tenant by the `tenants:` key's scalar TEXT (a
// map[string] decode), while PyYAML's YAML 1.1 typing reads `010` as 8 and
// `yes` as True. The Python tools now read tenant ids through
// scripts/tools/_lib_yaml_keys.py; the Python half asserts that reader
// against the same `exporter_key` column this test derives from the
// exporter's own decode (ParseConfigFile, the flat plane's decode).

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"runtime"
	"testing"

	"github.com/vencil/threshold-exporter/pkg/parsetree"
)

type tenantIDSpellingMatrix struct {
	Comment   []string `json:"_comment"`
	Spellings []struct {
		Source      string  `json:"source"`
		ExporterKey *string `json:"exporter_key"`
		SafeLoadStr *string `json:"safe_load_str"` // Python half only
		Rejected    bool    `json:"rejected"`      // both sides refuse the document
	} `json:"spellings"`
}

// loadTenantIDSpellingMatrix reads the shared matrix; also the source of
// tenant_id_bare_scalar_test.go's walker-plane rows (#2118).
func loadTenantIDSpellingMatrix(t *testing.T) tenantIDSpellingMatrix {
	t.Helper()
	_, thisFile, _, _ := runtime.Caller(0)
	path := filepath.Join(filepath.Dir(thisFile), "..", "..", "..", "..", "..",
		"tests", "shared", "tenant_id_yaml_spelling_matrix.json")
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read matrix: %v", err)
	}
	var m tenantIDSpellingMatrix
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&m); err != nil {
		t.Fatalf("parse matrix: %v", err)
	}
	if len(m.Spellings) == 0 {
		t.Fatal("matrix has no rows — a vacuous table passes nothing")
	}
	return m
}

func TestTenantIDSpellingMatrix_ExporterKeyIsTheDecodedKey(t *testing.T) {
	m := loadTenantIDSpellingMatrix(t)
	for _, row := range m.Spellings {
		t.Run(row.Source, func(t *testing.T) {
			doc := "tenants:\n  " + row.Source + ":\n    mysql_connections: \"1\"\n"
			cfg, err := ParseConfigFile([]byte(doc))
			if row.Rejected {
				if err == nil {
					t.Fatalf("source %q: table says the exporter rejects it, it decoded %v", row.Source, cfg.Tenants)
				}
				return
			}
			if row.ExporterKey == nil {
				// A null key: no tenant. Since ADR-036 step 6 (#3206) the
				// exporter refuses the whole file (parse-tree's null_key)
				// rather than dropping the key alone.
				if kind, ok := UnreadableKindOf(err); !ok || kind != parsetree.KindNullKey {
					t.Fatalf("source %q: table says no tenant; want the file refused as null_key, got tenants %v, err %v",
						row.Source, cfg.Tenants, err)
				}
				return
			}
			if err != nil {
				t.Fatalf("ParseConfigFile(%q): %v", doc, err)
			}
			var got []string
			for k := range cfg.Tenants {
				got = append(got, k)
			}
			if len(got) != 1 || got[0] != *row.ExporterKey {
				t.Fatalf("source %q: table says %q, exporter decoded %q", row.Source, *row.ExporterKey, got)
			}
		})
	}
}
