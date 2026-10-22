package config

// silent_mode_schema_parity_test.go (#3227) — the _silent_mode words of
// docs/schemas/tenant-config.schema.json are the reader's words.
//
// The reader (silentModeOf) knows the silencing targets (silentModeTargets)
// and the words that switch a key off (disabledWords). The schema lists the
// accepted words twice: the scalar branch's enum and the structured branch's
// target enum. Each must be exactly the reader's set — a word the reader takes
// that the schema refuses fails a valid config at author time, and a word the
// schema takes that the reader does not know passes validation and is then
// ignored with a WARN, silencing nothing.

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"slices"
	"sort"
	"testing"
)

// tenantSchemaRaw is docs/schemas/tenant-config.schema.json and its path.
func tenantSchemaRaw(t *testing.T) ([]byte, string) {
	t.Helper()
	_, thisFile, _, _ := runtime.Caller(0)
	// pkg/config → pkg → app → threshold-exporter → components → repo root
	path := filepath.Join(filepath.Dir(thisFile), "..", "..", "..", "..", "..",
		"docs", "schemas", "tenant-config.schema.json")
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read %s: %v", path, err)
	}
	return raw, path
}

// silentModeSchemaEnums reads definitions.silentMode of the tenant schema:
// the scalar branch's enum and the structured branch's target enum. More
// than one branch of a type is refused, not read as "the last one wins": a
// second string branch would let the schema take words the test never saw.
func silentModeSchemaEnums(t *testing.T) (scalar, target []string) {
	t.Helper()
	raw, path := tenantSchemaRaw(t)
	var schema struct {
		Definitions struct {
			SilentMode struct {
				OneOf []struct {
					Type       string   `json:"type"`
					Enum       []string `json:"enum"`
					Properties map[string]struct {
						Enum []string `json:"enum"`
					} `json:"properties"`
				} `json:"oneOf"`
			} `json:"silentMode"`
		} `json:"definitions"`
	}
	if err := json.Unmarshal(raw, &schema); err != nil {
		t.Fatalf("parse %s: %v", path, err)
	}
	var scalarSeen, objectSeen bool
	for _, branch := range schema.Definitions.SilentMode.OneOf {
		switch branch.Type {
		case "string":
			if scalarSeen {
				t.Fatalf("definitions.silentMode.oneOf has more than one string branch")
			}
			scalar, scalarSeen = branch.Enum, true
		case "object":
			if objectSeen {
				t.Fatalf("definitions.silentMode.oneOf has more than one object branch")
			}
			target, objectSeen = branch.Properties["target"].Enum, true
		default:
			t.Fatalf("definitions.silentMode.oneOf has a branch of type %q; this test reads only string and object", branch.Type)
		}
	}
	if !scalarSeen || !objectSeen || len(scalar) == 0 || len(target) == 0 {
		t.Fatalf("definitions.silentMode: want a string branch with an enum and an object branch "+
			"with properties.target.enum; got scalar=%q target=%q", scalar, target)
	}
	return scalar, target
}

// TestSilentModeSchemaEnumsMatchReader pins both schema enums to the reader's
// words, in both directions, and checks the reader reads every schema word
// without a WARN: a target silences, a disabled word turns silent mode off.
func TestSilentModeSchemaEnumsMatchReader(t *testing.T) {
	t.Parallel()
	reader := append(append([]string{}, silentModeTargets...), disabledWords(t)...)
	sort.Strings(reader)

	scalar, target := silentModeSchemaEnums(t)
	for _, e := range []struct {
		name  string
		words []string
	}{
		{"definitions.silentMode.oneOf[string].enum", scalar},
		{"definitions.silentMode.oneOf[object].properties.target.enum", target},
	} {
		got := append([]string{}, e.words...)
		sort.Strings(got)
		if len(slices.Compact(slices.Clone(got))) != len(got) {
			t.Errorf("%s lists a word twice: %q", e.name, e.words)
		}
		for _, w := range reader {
			if !slices.Contains(got, w) {
				t.Errorf("%s: the reader takes %q, the schema refuses it", e.name, w)
			}
		}
		for _, w := range got {
			if !slices.Contains(reader, w) {
				t.Errorf("%s: the schema takes %q, the reader does not know it (WARN, nothing silenced)", e.name, w)
			}
		}
	}

	for _, w := range scalar {
		want := silentModeActive
		if isDisabled(w) {
			want = silentModeOff
		}
		for _, doc := range []string{w, "target: " + w} {
			r := silentModeOf(ScheduledValue{Default: doc})
			if r.problem() != "" || r.kind != want {
				t.Errorf("silentModeOf(%q): kind=%d problem=%q, want kind=%d and no problem",
					doc, r.kind, r.problem(), want)
			}
		}
	}
}

// TestSilentModeSchemaPropertiesUseTheDefinition: every `_silent_mode`
// property anywhere in the tenant schema is exactly a $ref to
// definitions.silentMode, so the pinned enums are the only word list the
// schema has — an inline copy would escape TestSilentModeSchemaEnumsMatchReader.
func TestSilentModeSchemaPropertiesUseTheDefinition(t *testing.T) {
	t.Parallel()
	raw, path := tenantSchemaRaw(t)
	var doc any
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatalf("parse %s: %v", path, err)
	}
	found := 0
	var walk func(v any, at string)
	walk = func(v any, at string) {
		switch n := v.(type) {
		case map[string]any:
			if props, ok := n["properties"].(map[string]any); ok {
				if sm, ok := props["_silent_mode"]; ok {
					found++
					m, _ := sm.(map[string]any)
					if m["$ref"] != "#/definitions/silentMode" {
						t.Errorf("%s/properties/_silent_mode: want $ref #/definitions/silentMode, got %v", at, sm)
					}
					for k := range m {
						if k != "$ref" && k != "title" && k != "description" && k != "$comment" {
							t.Errorf("%s/properties/_silent_mode: keyword %q next to the $ref; keep the words in definitions.silentMode", at, k)
						}
					}
				}
			}
			for k, c := range n {
				walk(c, at+"/"+k)
			}
		case []any:
			for i, c := range n {
				walk(c, fmt.Sprintf("%s/%d", at, i))
			}
		}
	}
	walk(doc, "#")
	if found == 0 {
		t.Fatalf("%s: no _silent_mode property found", path)
	}
}
