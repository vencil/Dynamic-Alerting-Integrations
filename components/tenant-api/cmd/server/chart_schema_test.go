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

// chartOmittedWriteModes are the legal --write-mode values the Helm chart
// deliberately does not offer. `pr` is the binary's canonical spelling of
// GitHub PR mode, and pr-github its explicit alias; the chart wires its forge
// flags, token env and credential helper on the explicit names only
// (templates/deployment.yaml `eq .Values.writeBack.mode "pr-github"`), so a
// chart value of `pr` would start the binary without --github-repo and fail.
var chartOmittedWriteModes = map[handler.WriteMode]string{
	handler.WriteModePR: "alias of pr-github; the chart's GitHub wiring is keyed on pr-github",
}

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

	// Every chart value must be one the binary accepts.
	var stray []string
	for v := range inSchema {
		if _, err := parseWriteMode(string(v)); err != nil {
			stray = append(stray, string(v))
		}
	}
	sort.Strings(stray)
	if len(stray) > 0 {
		t.Errorf("schema enum offers %v, which --write-mode refuses: the install would succeed and "+
			"the pod crash-loop at startup", stray)
	}

	// Every legal binary value must be in the schema, unless deliberately omitted.
	for _, legal := range writeModeLegalValues {
		_, omitted := chartOmittedWriteModes[legal]
		switch {
		case omitted && inSchema[legal]:
			t.Errorf("%q is listed as chart-omitted (%s) but the schema offers it — drop one",
				legal, chartOmittedWriteModes[legal])
		case !omitted && !inSchema[legal]:
			t.Errorf("--write-mode accepts %q but the chart schema does not offer it: add it to "+
				"helm/tenant-api/values.schema.json (and wire it in the chart), or record why the "+
				"chart omits it in chartOmittedWriteModes", legal)
		}
	}
	// An omission entry for a value the binary no longer accepts is stale.
	for wm := range chartOmittedWriteModes {
		if _, err := parseWriteMode(string(wm)); err != nil {
			t.Errorf("chartOmittedWriteModes lists %q, which --write-mode no longer accepts — delete the entry", wm)
		}
	}
}
