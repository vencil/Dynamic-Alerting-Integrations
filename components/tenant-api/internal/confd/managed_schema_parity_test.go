package confd

// #3504: the top-level keys a tenant file may hold are the tenant-config
// schema's, the schema that check_confd_schema holds every tenant file to
// (additionalProperties false). TenantFileManagement reads them from
// tenantFileTopLevelKeys; this holds the two equal, so a key added to one
// and not the other turns this red instead of making the API and the schema
// gate disagree about what a tenant file is.

import (
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"testing"
)

func TestTenantFileTopLevelKeys_MatchTheSchema(t *testing.T) {
	t.Parallel()
	path := filepath.Join("..", "..", "..", "..", "docs", "schemas", "tenant-config.schema.json")
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read %s: %v", path, err)
	}
	var schema struct {
		Properties           map[string]json.RawMessage `json:"properties"`
		AdditionalProperties *bool                      `json:"additionalProperties"`
	}
	if err := json.Unmarshal(raw, &schema); err != nil {
		t.Fatalf("decode %s: %v", path, err)
	}
	if schema.AdditionalProperties == nil || *schema.AdditionalProperties {
		t.Fatalf("%s no longer closes the top level (additionalProperties false); the key set below is then not the whole rule", path)
	}
	var fromSchema, fromCode []string
	for k := range schema.Properties {
		fromSchema = append(fromSchema, k)
	}
	for k := range tenantFileTopLevelKeys {
		fromCode = append(fromCode, k)
	}
	sort.Strings(fromSchema)
	sort.Strings(fromCode)
	if !reflect.DeepEqual(fromSchema, fromCode) {
		t.Errorf("tenant-config schema top-level properties %v, tenantFileTopLevelKeys %v", fromSchema, fromCode)
	}
}
