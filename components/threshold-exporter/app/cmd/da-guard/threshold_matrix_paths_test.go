package main

// threshold_matrix_paths_test.go — every row of
// tests/shared/threshold_value_matrix.json, written on every path a threshold
// value is served through, in a real tree (#3457 review).
//
// pkg/config's half (threshold_value_parity_test.go) holds the readers — the
// functions — to the matrix. That does not show that every SERVING PATH goes
// through them: a resolver that read its value some other way (its own
// strconv.ParseFloat, say) would serve `1_000` as 1000 with every reader test
// green. Here each row's value is written once per path and the two things a
// deployment sees are asserted from the matrix's columns alone:
//
//   - what `da-guard served-values` says /metrics serves for the key;
//   - whether the whole-tree guard names the key `value_not_served` (or, for
//     the root `defaults:`, `root_default_refused`).
//
// The paths: a base key (resolveBaseRows), a `<key>_critical`
// (resolveCriticalRows), a declared key (resolveDeclaredRows), a dimensional
// key (appendDimensionalRow), a time window of a scheduled value, a subtree
// `_defaults.yaml` (the overlay), the root `defaults:` (readRootDefault) and a
// custom alert's `threshold:` (resolveOneCustomAlert).

import (
	"encoding/json"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"testing"

	"github.com/vencil/threshold-exporter/internal/testutil"
)

// thresholdPathRow is the matrix row with the columns this test reads.
type thresholdPathRow struct {
	Value               string  `json:"value"`
	OK                  bool    `json:"ok"`
	ValuePart           *string `json:"value_part"`
	Severity            *string `json:"severity"`
	FloatOK             *bool   `json:"float_ok"`
	RowDisabled         bool    `json:"row_disabled"`
	CustomAlertDisabled bool    `json:"custom_alert_disabled"`
}

// thresholdPathsAt is inside the window the scheduled key writes.
const thresholdPathsAt = "2026-08-01T10:00:00Z"

// The keys of the tree, one per path. Every default is 80.
const (
	pathBase      = "mysql_connections"
	pathCritical  = "pg_connections_critical"
	pathDeclared  = "oracle_sessions"
	pathDim       = `redis_memory{db="a"}`
	pathScheduled = "kafka_lag"
	pathSubtree   = "container_cpu"
	pathRoot      = "haproxy_sessions"
	pathCustom    = "_custom_alerts[0]"
)

// thresholdPathsTree is the tree with value on every path. value is written
// as a double-quoted YAML scalar with everything outside printable ASCII
// escaped (Go's escapes are YAML's), so the text reaches the exporter as it
// is, whatever it holds — a literal U+FEFF in a file is refused by parse-tree
// before any reader sees it.
func thresholdPathsTree(value string) map[string]string {
	v := strconv.QuoteToASCII(value)
	return map[string]string{
		"_defaults.yaml": "dimensions:\n  redis_memory: [db]\n" +
			"defaults:\n  mysql_connections: 80\n  pg_connections: 80\n  redis_memory: 80\n  kafka_lag: 80\n" +
			"  container_cpu: 80\n  " + pathRoot + ": " + v + "\n" +
			"optional_overrides:\n  - " + pathDeclared + "\n",
		"sub/_defaults.yaml": "defaults:\n  " + pathSubtree + ": " + v + "\n",
		"sub/t.yaml": "tenants:\n  t:\n" +
			"    " + pathBase + ": " + v + "\n" +
			"    " + pathCritical + ": " + v + "\n" +
			"    " + pathDeclared + ": " + v + "\n" +
			"    '" + pathDim + "': " + v + "\n" +
			"    " + pathScheduled + `: {default: "70", overrides: [{window: "00:00-23:59", value: ` + v + "}]}\n" +
			"    _custom_alerts:\n" +
			`      - {recipe: threshold, name: a, metric: m, op: ">", window: 5m, threshold: ` + v + "}\n",
	}
}

func TestThresholdMatrix_EveryServingPath(t *testing.T) {
	t.Parallel()
	_, thisFile, _, _ := runtime.Caller(0)
	raw, err := os.ReadFile(filepath.Join(filepath.Dir(thisFile), "..", "..", "..", "..", "..",
		"tests", "shared", "threshold_value_matrix.json"))
	if err != nil {
		t.Fatal(err)
	}
	var m struct {
		Rows []thresholdPathRow `json:"rows"`
	}
	if err := json.Unmarshal(raw, &m); err != nil {
		t.Fatal(err)
	}
	if len(m.Rows) < 100 {
		t.Fatalf("matrix has %d rows", len(m.Rows))
	}
	served, refused := 0, 0
	for i, r := range m.Rows {
		r := r
		rowOK := r.OK && *r.FloatOK
		if rowOK {
			served++
		} else if !r.RowDisabled {
			refused++
		}
		t.Run(strconv.Itoa(i), func(t *testing.T) {
			t.Parallel()
			checkThresholdPaths(t, r)
		})
	}
	if served < 20 || refused < 50 {
		t.Fatalf("matrix has %d served and %d refused rows; this test needs plenty of both", served, refused)
	}
}

// checkThresholdPaths asserts the tree of r.Value from r's columns.
func checkThresholdPaths(t *testing.T, r thresholdPathRow) {
	t.Helper()
	rowOK := r.OK && *r.FloatOK
	var number float64
	severity := ""
	if rowOK {
		number, _ = strconv.ParseFloat(*r.ValuePart, 64)
		severity = *r.Severity
	}
	// A value read whole — a `<key>_critical`, a root default — is a number
	// with no `:severity`.
	whole := rowOK && !strings.Contains(r.Value, ":")

	type want struct {
		value    any    // nil: no row for the key
		severity string // of the row, when value is not nil
		finding  string // the guard's finding kind for the key, or ""
	}
	served := func(v float64, sev string) want { return want{value: v, severity: sev} }
	none := func(kind string) want { return want{finding: kind} }
	const notServed = "value_not_served"

	// A threshold row: a number is served, a disable is no row and no
	// finding, anything else is refused.
	row := func(fallback *float64, numberOK bool, sev string) want {
		switch {
		case numberOK:
			return served(number, sev)
		case r.RowDisabled:
			return none("")
		case fallback != nil:
			return want{value: *fallback, severity: "warning", finding: notServed}
		default:
			return none(notServed)
		}
	}
	def := 80.0
	wants := map[string]want{
		pathBase:      row(&def, rowOK, severity),
		pathScheduled: row(&def, rowOK, severity),
		pathDeclared:  row(nil, rowOK, severity),
		pathDim:       row(nil, rowOK, severity),
		pathCritical:  row(nil, whole, "critical"),
		pathSubtree:   row(&def, rowOK, severity),
	}
	// The root `defaults:` takes a number alone: no severity, no disable.
	if whole {
		wants[pathRoot] = served(number, "warning")
	} else {
		wants[pathRoot] = none("root_default_refused")
	}
	// A custom alert: `disable`, with or without a severity, is its opt-out.
	switch {
	case rowOK:
		wants[pathCustom] = served(number, severity)
	case r.CustomAlertDisabled:
		wants[pathCustom] = none("")
	case r.Value == "":
		// No threshold at all: the declaration lacks a required field.
		wants[pathCustom] = none("custom_alert_rejected")
	default:
		wants[pathCustom] = none(notServed)
	}

	dir := filepath.Join(t.TempDir(), "conf.d")
	tree := map[string]string{}
	for name, body := range thresholdPathsTree(r.Value) {
		tree["conf.d/"+name] = body
	}
	testutil.WriteTree(t, filepath.Dir(dir), tree)

	code, stdout, stderr := runOnce(t, servedValuesCmd, "--config-dir", dir, "--at", thresholdPathsAt)
	if code != exitOK {
		t.Fatalf("%q: served-values exit %d: %s", r.Value, code, stderr)
	}
	var doc servedOut
	if err := json.Unmarshal([]byte(stdout), &doc); err != nil {
		t.Fatalf("%q: served-values: %v", r.Value, err)
	}
	tv, ok := doc.Tenants["t"]
	if !ok {
		t.Fatalf("%q: tenant t is not served; stdout %s", r.Value, stdout)
	}
	for key, w := range wants {
		var got any
		gotSeverity := ""
		if key == pathCustom {
			if rows, _ := tv.Values["_custom_alerts"].([]any); len(rows) == 1 {
				one, _ := rows[0].(map[string]any)
				got, gotSeverity = one["value"], one["severity"].(string)
			}
		} else if v, in := tv.Values[key]; in {
			got, gotSeverity = v, tv.Severities[key]
		}
		if w.value == nil {
			if got != nil {
				t.Errorf("%q on %s: served %v, the matrix says no row", r.Value, key, got)
			}
			continue
		}
		if got != w.value || gotSeverity != w.severity {
			t.Errorf("%q on %s: served %v (%s), the matrix says %v (%s)", r.Value, key, got, gotSeverity,
				w.value, w.severity)
		}
	}

	code, stdout, stderr = runOnce(t, "--config-dir", dir, "--format", "json")
	var guard struct {
		Report struct {
			Findings []struct {
				Kind  string `json:"kind"`
				Field string `json:"field"`
			} `json:"findings"`
		} `json:"report"`
	}
	if err := json.Unmarshal([]byte(stdout), &guard); err != nil {
		t.Fatalf("%q: guard stdout is not JSON (%v, exit %d): %s", r.Value, err, code, stderr)
	}
	got := map[string][]string{}
	for _, f := range guard.Report.Findings {
		got[f.Field] = append(got[f.Field], f.Kind)
	}
	anyFinding := false
	for key, w := range wants {
		field := key
		if key == pathRoot {
			field = "_defaults.yaml:defaults." + pathRoot
		}
		kinds := got[field]
		sort.Strings(kinds)
		if w.finding == "" {
			for _, k := range kinds {
				if k == notServed || k == "root_default_refused" || k == "custom_alert_rejected" {
					t.Errorf("%q on %s: the guard reports %v, the matrix says the value is read", r.Value, key, kinds)
				}
			}
			continue
		}
		anyFinding = true
		found := false
		for _, k := range kinds {
			found = found || k == w.finding
		}
		if !found {
			t.Errorf("%q on %s: the guard reports %v, the matrix says %s", r.Value, key, kinds, w.finding)
		}
	}
	if anyFinding && code != exitFindings {
		t.Errorf("%q: guard exit %d with a value it must refuse, want %d", r.Value, code, exitFindings)
	}
}
