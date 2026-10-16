package receiverspec

import (
	"encoding/json"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

// TestKnown_MatchesSchema pins the receiver types to the receiver definitions
// of docs/schemas/tenant-config.schema.json (definitions.receiver.oneOf →
// each definition's properties.type.const), in both directions.
func TestKnown_MatchesSchema(t *testing.T) {
	defs := schemaDefinitions(t)
	var union struct {
		OneOf []struct {
			Ref string `json:"$ref"`
		} `json:"oneOf"`
	}
	if err := json.Unmarshal(defs["receiver"], &union); err != nil || len(union.OneOf) == 0 {
		t.Fatalf("definitions.receiver.oneOf not readable (err=%v)", err)
	}
	fromSchema := map[string]bool{}
	for _, branch := range union.OneOf {
		name := strings.TrimPrefix(branch.Ref, "#/definitions/")
		var def struct {
			Properties map[string]map[string]json.RawMessage `json:"properties"`
		}
		if err := json.Unmarshal(defs[name], &def); err != nil {
			t.Fatalf("definition %s: %v", name, err)
		}
		var rtype string
		if err := json.Unmarshal(def.Properties["type"]["const"], &rtype); err != nil || rtype == "" {
			t.Fatalf("definition %s has no properties.type.const", name)
		}
		fromSchema[rtype] = true
		if !Known(rtype) {
			t.Errorf("schema defines receiver type %q; Known does not take it", rtype)
		}
	}
	for rtype := range types {
		if !fromSchema[rtype] {
			t.Errorf("Known takes %q; the schema defines no such receiver type", rtype)
		}
	}
	for _, near := range []string{"", "Webhook", " webhook", "webhook ", "slack\n"} {
		if Known(near) {
			t.Errorf("Known(%q): the type must match exactly (#2180)", near)
		}
	}
}

// schemaDefinitions reads definitions of docs/schemas/tenant-config.schema.json.
func schemaDefinitions(t *testing.T) map[string]json.RawMessage {
	t.Helper()
	_, thisFile, _, _ := runtime.Caller(0)
	// pkg/receiverspec → pkg → app → threshold-exporter → components → repo root
	root := filepath.Join(filepath.Dir(thisFile), "..", "..", "..", "..", "..")
	data, err := os.ReadFile(filepath.Join(root, "docs", "schemas", "tenant-config.schema.json")) // #nosec G304 -- repo schema
	if err != nil {
		t.Fatalf("read schema: %v", err)
	}
	var schema struct {
		Definitions map[string]json.RawMessage `json:"definitions"`
	}
	if err := json.Unmarshal(data, &schema); err != nil {
		t.Fatalf("parse schema: %v", err)
	}
	return schema.Definitions
}
