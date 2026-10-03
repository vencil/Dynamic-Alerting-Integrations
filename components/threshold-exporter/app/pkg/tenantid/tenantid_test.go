package tenantid

import (
	"encoding/json"
	"os"
	"path/filepath"
	"runtime"
	"testing"
)

// TestValid_SharedCases asserts the case table the Python reader and the
// portal copy also assert, so the three readers agree.
func TestValid_SharedCases(t *testing.T) {
	t.Parallel()
	data, err := os.ReadFile(filepath.Join("testdata", "tenant_id_cases.json"))
	if err != nil {
		t.Fatalf("read cases: %v", err)
	}
	var cases struct {
		Valid   []string `json:"valid"`
		Invalid []string `json:"invalid"`
	}
	if err := json.Unmarshal(data, &cases); err != nil {
		t.Fatalf("parse cases: %v", err)
	}
	if len(cases.Valid) == 0 || len(cases.Invalid) == 0 {
		t.Fatalf("case table has an empty side: %d valid, %d invalid", len(cases.Valid), len(cases.Invalid))
	}
	for _, id := range cases.Valid {
		if !Valid(id) {
			t.Errorf("Valid(%q) = false, want true", id)
		}
	}
	for _, id := range cases.Invalid {
		if Valid(id) {
			t.Errorf("Valid(%q) = true, want false", id)
		}
	}
}

// TestEmbeddedCopyMatchesSchema pins the embedded copy to its source. The
// drift guard compares the files; this compares what Go parsed.
func TestEmbeddedCopyMatchesSchema(t *testing.T) {
	t.Parallel()
	_, thisFile, _, _ := runtime.Caller(0)
	// pkg/tenantid → pkg → app → threshold-exporter → components → repo root
	root := filepath.Join(filepath.Dir(thisFile), "..", "..", "..", "..", "..")
	data, err := os.ReadFile(filepath.Join(root, "docs", "schemas", "tenant-config.schema.json"))
	if err != nil {
		t.Fatalf("read schema: %v", err)
	}
	var schema struct {
		Definitions struct {
			TenantID struct {
				Pattern     string `json:"pattern"`
				Description string `json:"description"`
			} `json:"tenantId"`
		} `json:"definitions"`
	}
	if err := json.Unmarshal(data, &schema); err != nil {
		t.Fatalf("parse schema: %v", err)
	}
	if got, want := Pattern, schema.Definitions.TenantID.Pattern; got != want {
		t.Errorf("Pattern = %q, schema definitions.tenantId.pattern = %q; run `make tenant-id-json`", got, want)
	}
	if got, want := Description, schema.Definitions.TenantID.Description; got != want {
		t.Errorf("Description = %q, schema definitions.tenantId.description = %q; run `make tenant-id-json`", got, want)
	}
}
