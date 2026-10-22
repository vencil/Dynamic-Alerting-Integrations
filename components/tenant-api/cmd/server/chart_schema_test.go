package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"sort"
	"testing"

	"github.com/vencil/tenant-api/internal/handler"
	"github.com/vencil/tenant-api/internal/testutil"
)

// TestChartSchemaWriteModesMatchTheBinary pins helm/tenant-api's
// values.schema.json writeBack.mode enum to writeModeLegalValues (#1594).
//
// The schema is what refuses a misspelled mode at `helm install`, before any
// pod starts. If the binary gains a mode the schema does not list, the chart
// cannot select it; if the schema lists one the binary does not accept, the
// install succeeds and the pod crash-loops on a FATAL at startup. Either way
// the two sets have drifted, and this turns that into a red Go test.
//
// The schema's empty string is not a mode: it is the shipped "unset" default,
// which the chart's writeBack guard refuses at render with an explanation.
func TestChartSchemaWriteModesMatchTheBinary(t *testing.T) {
	t.Parallel()
	path := filepath.Join(testutil.RepoRoot(t), "helm", "tenant-api", "values.schema.json")
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("cannot read %s (%v) — a guard that skips on a missing input guards nothing", path, err)
	}
	var schema struct {
		Properties struct {
			WriteBack struct {
				Properties struct {
					Mode struct {
						Enum []string `json:"enum"`
					} `json:"mode"`
				} `json:"properties"`
			} `json:"writeBack"`
		} `json:"properties"`
	}
	if err := json.Unmarshal(raw, &schema); err != nil {
		t.Fatalf("parse %s: %v", path, err)
	}
	enum := schema.Properties.WriteBack.Properties.Mode.Enum
	if len(enum) == 0 {
		t.Fatalf("%s has no properties.writeBack.properties.mode.enum — the schema moved; update this test's path to it", path)
	}

	inSchema := map[handler.WriteMode]bool{}
	sawEmpty := false
	for _, v := range enum {
		if v == "" {
			sawEmpty = true
			continue
		}
		if inSchema[handler.WriteMode(v)] {
			t.Errorf("schema enum lists %q twice", v)
		}
		inSchema[handler.WriteMode(v)] = true
	}
	if !sawEmpty {
		t.Errorf("schema enum has no \"\": the shipped values.yaml default (writeBack.mode: \"\") " +
			"would fail schema validation with a generic message instead of the writeBack guard's explanation")
	}

	// Strict equality: the chart offers exactly the inputs the binary accepts.
	// Compare the raw schema strings against writeModeLegalValues, never through
	// parseWriteMode: that trims whitespace, so a schema entry " pr-github "
	// would pass as legal while being a value no operator should be offered
	// (and a set that differs from the binary's only by padding is still drift).
	legalSet := map[handler.WriteMode]bool{}
	for _, legal := range writeModeLegalValues {
		legalSet[legal] = true
	}
	var stray, missing []string
	for v := range inSchema {
		if !legalSet[v] {
			stray = append(stray, string(v))
		}
	}
	for _, legal := range writeModeLegalValues {
		if !inSchema[legal] {
			missing = append(missing, string(legal))
		}
	}
	sort.Strings(stray)
	sort.Strings(missing)
	if len(stray) > 0 {
		t.Errorf("schema enum offers %v, which --write-mode refuses: the install would succeed and "+
			"the pod crash-loop at startup", stray)
	}
	if len(missing) > 0 {
		t.Errorf("--write-mode accepts %v but the chart schema does not offer it: add it to "+
			"helm/tenant-api/values.schema.json (and wire it in the chart), or stop accepting it", missing)
	}
}
