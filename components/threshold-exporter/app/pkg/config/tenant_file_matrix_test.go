package config

// tenant_file_matrix_test.go — Go's half of tests/shared/tenant_file_matrix.json
// (#3999), and the program that writes its `served` column.
//
// Each row is a tenant's key map as the portal's simulator pages take it
// (`body`), and whether the exporter serves the tenant file that holds it:
// `served` is ParseTenantFile's verdict on `tenants:\n  t1:\n` + body indented
// under t1 — the call the exporter's flat scanner makes, so `served: false` is
// a file served-values reports in parse_failed. The portal's half
// (tools/portal/tests/tenant-file-matrix.test.ts) holds parseTenantYaml to it:
// it reports a file-level error exactly where `served` is false, except on a
// row whose `portal_divergence` (hand-written, kept on regeneration) says why
// the portal may differ.
//
// Most rows are generated (generatedTenantFileBodies): each spelling of a
// schedule's `default` key, times each shape of its fields, times each way
// of writing the key that holds the schedule (as is, as an alias, brought in
// by a root merge). Within those dimensions a combination nobody thought of
// (an alias key, #3999) is a row without anyone adding it; outside them, add
// a hand row with only `body`, then regenerate:
//
//	DA_REGEN_TENANT_FILE_MATRIX=1 go test ./pkg/config -run TestTenantFileMatrix
//
// Without the variable the test asserts every generated body is a row and
// every row's `served` is still what ParseTenantFile says.

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
	"testing"
)

type tenantFileRow struct {
	Body   string `json:"body"`
	Served *bool  `json:"served,omitempty"`
	// PortalDivergence: why the portal's parseTenantYaml may judge this body
	// otherwise — an accepted js-yaml / yaml.v3 difference. Hand-written.
	PortalDivergence string `json:"portal_divergence,omitempty"`
}

type tenantFileMatrix struct {
	Comment []string        `json:"_comment"`
	Rows    []tenantFileRow `json:"rows"`
}

func tenantFileMatrixPath() string {
	_, thisFile, _, _ := runtime.Caller(0)
	return filepath.Join(filepath.Dir(thisFile), "..", "..", "..", "..", "..",
		"tests", "shared", "tenant_file_matrix.json")
}

// tenantFileServed is whether the exporter serves a tenant file holding body
// as tenant t1's key map.
func tenantFileServed(body string) bool {
	var b strings.Builder
	b.WriteString("tenants:\n  t1:\n")
	for _, line := range strings.Split(strings.TrimSuffix(body, "\n"), "\n") {
		b.WriteString("    " + line + "\n")
	}
	_, err := ParseTenantFile([]byte(b.String()))
	return err == nil
}

// generatedTenantFileBodies is every spelling of a schedule's `default` key
// (as written, quoted, tagged, an alias, brought in by a merge) times every
// shape of `default` and of one other field, under a threshold key, a `_` key
// and a key named `default`, each written as is, as an alias, and brought in
// by a root merge. Each schedule is one flow mapping.
func generatedTenantFileBodies() []string {
	defaults := []string{`80`, `~`, `""`, `[1]`, `{a: 1}`}
	extras := []string{
		``,
		`overrides: ~`, `overrides: []`, `overrides: 5`, `overrides: "x"`, `overrides: {a: 1}`,
		`overrides: [~]`, `overrides: [1]`, `overrides: [[1]]`,
		`overrides: [{window: "01:00-02:00", value: 80}]`,
		`overrides: [{window: [1], value: 80}]`,
		`overrides: [{window: "01:00-02:00", value: {a: 1}}]`,
		`overrides: [{window: "01:00-02:00", value: 80, extra: [1]}]`,
		`expires: "2030-01-01T00:00:00Z"`, `expires: [1]`, `expires: {a: 1}`,
		`reason: x`, `reason: [1]`,
		`extra: [1]`,
	}
	type spelling struct{ before, field string }
	spellings := []spelling{
		{"", "default: %s"},
		{"", `"default": %s`},
		{"", `'default': %s`},
		{"", `!!str default: %s`},
		{"_d: &d default\n", `*d : %s`},             // an alias key: yaml.v3 compares the anchor's name, not `default`
		{"_d: &default default\n", `*default : %s`}, // its name is `default`: a schedule
		{"_d: &default foo\n", `*default : %s`},     // named `default`, resolving to another key
		{"", `<<: {default: %s}`},                   // brought in by a merge: not the mapping's own key
	}
	// How the key holding the schedule is written: as is, as an alias (yaml.v3
	// resolves an alias key of the tenant's map), as an explicit `? *k` alias
	// key, brought in by a root merge.
	holders := []func(key, schedule string) string{
		func(key, schedule string) string { return key + ": " + schedule + "\n" },
		func(key, schedule string) string { return "_k: &k " + key + "\n*k : " + schedule + "\n" },
		func(key, schedule string) string { return "_k: &k " + key + "\n? *k\n: " + schedule + "\n" },
		func(key, schedule string) string { return "<<: {" + key + ": " + schedule + "}\n" },
	}
	var out []string
	for _, key := range []string{"mysql_replication_lag", "_silent_mode", "default"} {
		for _, hold := range holders {
			for _, s := range spellings {
				for _, d := range defaults {
					fields := strings.Replace(s.field, "%s", d, 1)
					for _, e := range extras {
						body := fields
						if e != "" {
							body += ", " + e
						}
						out = append(out, s.before+hold(key, "{"+body+"}"))
					}
				}
			}
		}
	}
	return out
}

// writeTenantFileMatrix writes one row per line, so a diff names the row.
func writeTenantFileMatrix(t *testing.T, path string, m tenantFileMatrix) {
	t.Helper()
	var b bytes.Buffer
	head, err := json.MarshalIndent(struct {
		Comment []string `json:"_comment"`
	}{m.Comment}, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	b.Write(bytes.TrimSuffix(head, []byte("\n}")))
	b.WriteString(",\n  \"rows\": [\n")
	for i, r := range m.Rows {
		var line bytes.Buffer
		enc := json.NewEncoder(&line)
		enc.SetEscapeHTML(false)
		if err := enc.Encode(r); err != nil {
			t.Fatal(err)
		}
		b.WriteString("    ")
		b.Write(bytes.TrimSuffix(line.Bytes(), []byte("\n")))
		if i < len(m.Rows)-1 {
			b.WriteString(",")
		}
		b.WriteString("\n")
	}
	b.WriteString("  ]\n}\n")
	if err := os.WriteFile(path, b.Bytes(), 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestTenantFileMatrix(t *testing.T) {
	path := tenantFileMatrixPath()
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read matrix: %v", err)
	}
	var m tenantFileMatrix
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&m); err != nil {
		t.Fatalf("parse matrix: %v", err)
	}

	generated := generatedTenantFileBodies()
	if os.Getenv("DA_REGEN_TENANT_FILE_MATRIX") == "1" {
		byBody := map[string]tenantFileRow{}
		var order []string
		for _, r := range m.Rows { // hand rows and their notes first, in file order
			if _, dup := byBody[r.Body]; !dup {
				order = append(order, r.Body)
			}
			byBody[r.Body] = r
		}
		for _, body := range generated {
			if _, ok := byBody[body]; !ok {
				byBody[body] = tenantFileRow{Body: body}
				order = append(order, body)
			}
		}
		m.Rows = m.Rows[:0]
		for _, body := range order {
			r := byBody[body]
			served := tenantFileServed(body)
			r.Served = &served
			m.Rows = append(m.Rows, r)
		}
		writeTenantFileMatrix(t, path, m)
		t.Logf("rewrote %d rows of %s", len(m.Rows), path)
		return
	}

	if len(m.Rows) == 0 {
		t.Fatal("matrix has no rows — a vacuous table passes nothing")
	}
	seen := map[string]bool{}
	var served, refused int
	for _, r := range m.Rows {
		if seen[r.Body] {
			t.Errorf("duplicate row %q", r.Body)
		}
		seen[r.Body] = true
		if r.Served == nil {
			t.Errorf("row %q has no `served` (regenerate with DA_REGEN_TENANT_FILE_MATRIX=1)", r.Body)
			continue
		}
		if got := tenantFileServed(r.Body); got != *r.Served {
			t.Errorf("row %q: matrix says served=%v, ParseTenantFile says %v "+
				"(regenerate with DA_REGEN_TENANT_FILE_MATRIX=1)", r.Body, *r.Served, got)
		}
		if *r.Served {
			served++
		} else {
			refused++
		}
	}
	var missing []string
	for _, body := range generated {
		if !seen[body] {
			missing = append(missing, body)
		}
	}
	sort.Strings(missing)
	if len(missing) > 0 {
		t.Errorf("%d generated bodies are not rows, e.g. %q (regenerate with DA_REGEN_TENANT_FILE_MATRIX=1)",
			len(missing), missing[0])
	}
	// Both verdicts present: a table of one verdict holds the portal to nothing.
	if served == 0 || refused == 0 {
		t.Errorf("matrix has %d served and %d refused rows; it needs both", served, refused)
	}
}
