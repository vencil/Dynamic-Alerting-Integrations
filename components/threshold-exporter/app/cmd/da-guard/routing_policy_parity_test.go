package main

// routing_policy_parity_test.go — da-guard's half of
// tests/shared/routing_policy_parity_matrix.json (#2280). Each tree is run
// through the CLI (run --format json); the findings must say what the table
// says the route generator does: one domain_policy_violation per `policy`
// row (Field <ref>.receiver.type), one invalid_route_entry per
// `rejected_routes` ref, unknown_routing_profile exactly when the table
// names one, one routing_value_not_string per `values_not_string` field
// (#2431), one routing_group_by_invalid per `group_by_invalid` field
// (#2503) and one with an empty TenantID and Field <file>:<field> per
// `enforced_group_by_invalid` row (#2503), critical_escalation_missing
// exactly when `escalation` says violation and one critical_escalation_leak
// Field per leak ref (#2325), and exactly the tree's `platform` rows as
// TenantID "" findings. The Python half reads only the same table.

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

type daGuardParityExpect struct {
	Targets        json.RawMessage `json:"targets"`
	Policy         [][3]string     `json:"policy"`
	RejectedRoutes []string        `json:"rejected_routes"`
	NotString      []string        `json:"values_not_string"`
	GroupByInvalid [][2]string     `json:"group_by_invalid"`
	UnknownProfile *string         `json:"unknown_profile"`
	TenantAPI      json.RawMessage `json:"tenant_api"`
	PythonDiffers  json.RawMessage `json:"python_differs"`
	Escalation     *struct {
		Verdict string   `json:"verdict"`
		Leaks   []string `json:"leaks"`
	} `json:"escalation"`
	// Refused (#2341): why the generator renders nothing for the tenant —
	// the da-guard finding kind of that name, Field `_routing`.
	Refused *string `json:"refused"`
}

type daGuardParityTree struct {
	Name     string                         `json:"name"`
	Files    map[string]string              `json:"files"`
	Platform [][3]string                    `json:"platform"`
	Expect   map[string]daGuardParityExpect `json:"expect"`
	// EnforcedGroupBy (#2503): [file, field, kind] per bad group_by element
	// of the rendered `_routing_enforced` route(s) — a TenantID ""
	// routing_group_by_invalid finding with Field <file>:<field> each.
	EnforcedGroupBy [][3]string `json:"enforced_group_by_invalid"`
}

func loadRoutingPolicyMatrix(t *testing.T) []daGuardParityTree {
	t.Helper()
	_, thisFile, _, _ := runtime.Caller(0)
	path := filepath.Join(filepath.Dir(thisFile), "..", "..", "..", "..", "..",
		"tests", "shared", "routing_policy_parity_matrix.json")
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read matrix: %v", err)
	}
	var m struct {
		Comment       []string        `json:"_comment"`
		BlockingKinds json.RawMessage `json:"blocking_kinds"`
		TenantIDs     json.RawMessage `json:"tenant_ids"` // pinned by pkg/routingpolicy
		// Why a tenant_api cell is null: the Python half asserts it.
		TenantAPIUnmeasured json.RawMessage     `json:"tenant_api_unmeasured"`
		Trees               []daGuardParityTree `json:"trees"`
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&m); err != nil {
		t.Fatalf("parse matrix: %v", err)
	}
	if len(m.Trees) == 0 {
		t.Fatal("matrix has no trees — a vacuous table passes nothing")
	}
	return m.Trees
}

type jsonFinding struct {
	Severity string `json:"severity"`
	Kind     string `json:"kind"`
	TenantID string `json:"tenant_id"`
	Field    string `json:"field"`
}

// writeParityTree writes files (root-relative slash paths, any depth —
// #2326) under a fresh conf.d and returns it.
func writeParityTree(t *testing.T, files map[string]string) string {
	t.Helper()
	dir := filepath.Join(t.TempDir(), "conf.d")
	for rel, content := range files {
		p := filepath.Join(dir, filepath.FromSlash(rel))
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte(content), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	return dir
}

// runTreeJSON writes files under a fresh conf.d and returns the exit code and
// the report's findings.
func runTreeJSON(t *testing.T, files map[string]string) (int, []jsonFinding, []string) {
	t.Helper()
	dir := writeParityTree(t, files)
	code, stdout, stderr := runOnce(t, "--config-dir", dir, "--format", "json")
	var doc struct {
		ParseFailed []string `json:"parse_failed"`
		Report      struct {
			Findings []jsonFinding `json:"findings"`
		} `json:"report"`
	}
	if err := json.Unmarshal([]byte(stdout), &doc); err != nil {
		t.Fatalf("exit %d, report is not JSON: %v\nstdout=%s\nstderr=%s", code, err, stdout, stderr)
	}
	return code, doc.Report.Findings, doc.ParseFailed
}

func fieldsOf(fs []jsonFinding, tenantID, kind string) []string {
	out := []string{}
	for _, f := range fs {
		if f.TenantID == tenantID && f.Kind == kind {
			out = append(out, f.Field)
		}
	}
	sort.Strings(out)
	return out
}

func TestDaGuard_RoutingPolicyParityMatrix(t *testing.T) {
	t.Parallel()
	for _, tree := range loadRoutingPolicyMatrix(t) {
		t.Run(tree.Name, func(t *testing.T) {
			t.Parallel()
			if len(tree.Expect) == 0 {
				t.Fatal("tree expects nothing")
			}
			// #2326 (e): a duplicate tenant id is rejected by the exporter's
			// own resolution before any check runs — exit 2 naming it, not a
			// finding. The table's duplicate_tenant row is that refusal here.
			for _, row := range tree.Platform {
				if row[0] != "duplicate_tenant" {
					continue
				}
				code, _, stderr := runOnce(t, "--config-dir", writeParityTree(t, tree.Files), "--format", "json")
				tid := strings.TrimPrefix(row[2], "tenants.")
				if code != exitCallerErr || !strings.Contains(stderr, "duplicate tenant ID") ||
					!strings.Contains(stderr, tid) || !strings.Contains(stderr, filepath.FromSlash(row[1])) {
					t.Errorf("duplicate tenant: exit %d, stderr %q; want exit %d naming %s in %s",
						code, stderr, exitCallerErr, tid, row[1])
				}
				return
			}
			code, findings, parseFailed := runTreeJSON(t, tree.Files)
			if code == exitCallerErr || code == exitParseFailed || len(parseFailed) > 0 {
				t.Fatalf("exit %d, parse_failed %v: every matrix tree must be one the exporter reads whole", code, parseFailed)
			}
			gotPlatform, wantPlatform := []string{}, []string{}
			for _, f := range findings {
				if f.TenantID == "" && f.Kind != "routing_group_by_invalid" && f.Kind != "invalid_tenant_id" {
					gotPlatform = append(gotPlatform, f.Kind+" "+f.Field)
				}
			}
			for _, row := range tree.Platform {
				if row[2] == "" { // the whole file (#2677 R1): the finding names the file alone
					wantPlatform = append(wantPlatform, row[0]+" "+row[1])
					continue
				}
				wantPlatform = append(wantPlatform, row[0]+" "+row[1]+":"+row[2])
			}
			sort.Strings(gotPlatform)
			sort.Strings(wantPlatform)
			if !equalStrings(gotPlatform, wantPlatform) {
				t.Errorf("platform findings %v, table says %v", gotPlatform, wantPlatform)
			}
			// #2503: the enforced route(s)' group_by (the kind is pinned by
			// pkg/routingpolicy's half; the finding carries file and field).
			wantEnforced := []string{}
			for _, row := range tree.EnforcedGroupBy {
				wantEnforced = append(wantEnforced, row[0]+":"+row[1])
			}
			sort.Strings(wantEnforced)
			if got := fieldsOf(findings, "", "routing_group_by_invalid"); !equalStrings(got, wantEnforced) {
				t.Errorf("enforced routing_group_by_invalid fields %v, table says %v", got, wantEnforced)
			}
			for tenantID, want := range tree.Expect {
				wantPolicy := []string{}
				for _, row := range want.Policy {
					field := row[1] + ".receiver.type"
					if row[1] == "receiver" {
						field = "receiver.type"
					}
					wantPolicy = append(wantPolicy, field)
				}
				sort.Strings(wantPolicy)
				if got := fieldsOf(findings, tenantID, "domain_policy_violation"); !equalStrings(got, wantPolicy) {
					t.Errorf("%s: domain_policy_violation fields %v, table says %v", tenantID, got, wantPolicy)
				}
				wantRejected := append([]string{}, want.RejectedRoutes...)
				sort.Strings(wantRejected)
				if got := fieldsOf(findings, tenantID, "invalid_route_entry"); !equalStrings(got, wantRejected) {
					t.Errorf("%s: invalid_route_entry fields %v, table says %v", tenantID, got, wantRejected)
				}
				// #2431: a matcher value PyYAML does not read as a string.
				wantNotString := append([]string{}, want.NotString...)
				sort.Strings(wantNotString)
				if got := fieldsOf(findings, tenantID, "routing_value_not_string"); !equalStrings(got, wantNotString) {
					t.Errorf("%s: routing_value_not_string fields %v, table says %v", tenantID, got, wantNotString)
				}
				// #2503: a bad group_by element (the kind is pinned by
				// pkg/routingpolicy's half; the finding carries the field).
				wantGroupBy := []string{}
				for _, row := range want.GroupByInvalid {
					wantGroupBy = append(wantGroupBy, row[0])
				}
				sort.Strings(wantGroupBy)
				if got := fieldsOf(findings, tenantID, "routing_group_by_invalid"); !equalStrings(got, wantGroupBy) {
					t.Errorf("%s: routing_group_by_invalid fields %v, table says %v", tenantID, got, wantGroupBy)
				}
				// #2325: critical_escalation_missing ⇔ verdict violation; the
				// critical_escalation_leak Fields are the leak refs (as a set:
				// findings are sorted, not in render order), per requiring domain.
				wantMissing, wantLeaks := false, []string{}
				if want.Escalation != nil {
					wantMissing = want.Escalation.Verdict == "violation"
					for _, ref := range want.Escalation.Leaks {
						field := ref + ".receiver.type"
						if ref == "receiver" {
							field = "receiver.type"
						}
						wantLeaks = append(wantLeaks, field)
					}
				}
				gotMissing := fieldsOf(findings, tenantID, "critical_escalation_missing")
				if (len(gotMissing) > 0) != wantMissing {
					t.Errorf("%s: critical_escalation_missing %v, table says violation=%v", tenantID, gotMissing, wantMissing)
				}
				sort.Strings(wantLeaks)
				if got := dedupe(fieldsOf(findings, tenantID, "critical_escalation_leak")); !equalStrings(got, dedupe(wantLeaks)) {
					t.Errorf("%s: critical_escalation_leak fields %v, table says %v", tenantID, got, wantLeaks)
				}
				// #2341 R8: one invalid_tenant_id finding iff the id is refused
				// (its Field names the file, which the table does not).
				gotInvalid := len(fieldsOf(findings, tenantID, "invalid_tenant_id")) > 0
				if wantInvalid := want.Refused != nil && *want.Refused == "invalid_tenant_id"; gotInvalid != wantInvalid {
					t.Errorf("%q: invalid_tenant_id reported=%v, table says %v", tenantID, gotInvalid, wantInvalid)
				}
				for _, kind := range []string{"routing_not_mapping"} {
					wantFields := []string{}
					if want.Refused != nil && *want.Refused == kind {
						wantFields = []string{"_routing"}
					}
					if got := fieldsOf(findings, tenantID, kind); !equalStrings(got, wantFields) {
						t.Errorf("%s: %s fields %v, table says %v", tenantID, kind, got, wantFields)
					}
				}
				gotUnknown := len(fieldsOf(findings, tenantID, "unknown_routing_profile")) > 0
				if gotUnknown != (want.UnknownProfile != nil) {
					t.Errorf("%s: unknown_routing_profile reported=%v, table names %v", tenantID, gotUnknown, want.UnknownProfile)
				}
			}
		})
	}
}

// dedupe drops adjacent repeats of a sorted list (one finding per requiring
// domain names the same Field again).
func dedupe(sorted []string) []string {
	out := []string{}
	for i, s := range sorted {
		if i == 0 || s != sorted[i-1] {
			out = append(out, s)
		}
	}
	return out
}

func equalStrings(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}
