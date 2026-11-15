package main

// served_schedules_ungathered_test.go — the document's `ungathered` (#3991):
// where in the UTC day /metrics cannot be gathered, written once for the
// tree. The schedules' segments carry that error only on the keys a tenant
// serves, so a tree colliding all day printed every tenant's schedules {}
// with exit 0 and nothing else — a reader of the day judged nothing and said
// so as "nothing to compare".

import (
	"encoding/json"
	"strings"
	"testing"
)

// allDayCollisionTree is #3991's tree: the tenant's `70:critical` row and its
// `_critical` key's give one series at every minute of the day.
var allDayCollisionTree = map[string]string{
	"_defaults.yaml": "defaults:\n  mysql_connection_usage_percent: 80\n",
	"ta.yaml": "tenants:\n  ta:\n    mysql_connection_usage_percent: \"70:critical\"\n" +
		"    mysql_connection_usage_percent_critical: \"95\"\n",
}

type ungatheredDoc struct {
	Tenants map[string]struct {
		Schedules map[string]servedSchedule `json:"schedules"`
	} `json:"tenants"`
	Ungathered *[]ungatheredPart `json:"ungathered"`
}

func schedulesOnlyDoc(t *testing.T, files map[string]string, at string) ungatheredDoc {
	t.Helper()
	dir := writeConfD(t, files)
	code, stdout, stderr := runOnce(t, servedValuesCmd, "--config-dir", dir, "--at", at, "--schedules-only")
	mustOK(t, code, stderr)
	var doc ungatheredDoc
	if err := json.Unmarshal([]byte(stdout), &doc); err != nil {
		t.Fatal(err)
	}
	if doc.Ungathered == nil {
		t.Fatalf("no ungathered in the --schedules-only document:\n%s", stdout)
	}
	return doc
}

// TestServedSchedulesOnly_AllDayUngatheredIsNamed: no key is served at any
// minute, so no segment can carry the error; the document names the whole
// day instead, with the Gather's failure.
func TestServedSchedulesOnly_AllDayUngatheredIsNamed(t *testing.T) {
	t.Parallel()
	doc := schedulesOnlyDoc(t, allDayCollisionTree, "2026-07-01T12:00:00Z")
	if s := doc.Tenants["ta"].Schedules; len(s) != 0 {
		t.Fatalf("ta's schedules = %v; this test is about a tenant with none", s)
	}
	u := *doc.Ungathered
	if len(u) != 1 || u[0].From != "00:00" || u[0].To != "24:00" || !strings.Contains(u[0].Error, "HTTP 500") ||
		!strings.Contains(u[0].Error, "mysql_connection_usage_percent") {
		t.Errorf("ungathered = %+v, want the whole day (00:00-24:00) with the Gather failure naming the keys", u)
	}
}

// TestServedSchedulesOnly_UngatheredCoversATenantServingNoKey: tx collides
// 01:00–03:00; ty serves no threshold row at all (every key it is served
// switched off), so its schedules are {} and say nothing of that part of
// the day. The document's ungathered names it, with the very error tx's
// segment there carries.
func TestServedSchedulesOnly_UngatheredCoversATenantServingNoKey(t *testing.T) {
	t.Parallel()
	files := map[string]string{
		"_defaults.yaml": windowCollisionTree["_defaults.yaml"],
		"tx.yaml":        windowCollisionTree["tx.yaml"],
		"ty.yaml":        "tenants:\n  ty: {}\n",
	}
	// The keys ty is served — the root defaults and those the platform
	// ships switched on — asked of served-values at a gatherable minute,
	// not listed here: each is then written `disable`.
	code, stdout, stderr := runOnce(t, servedValuesCmd, "--config-dir", writeConfD(t, files), "--at", "2026-07-01T12:00:00Z")
	mustOK(t, code, stderr)
	var first struct {
		Tenants map[string]struct {
			Severities map[string]string `json:"severities"`
		} `json:"tenants"`
	}
	if err := json.Unmarshal([]byte(stdout), &first); err != nil {
		t.Fatal(err)
	}
	ty := "tenants:\n  ty:\n"
	for k := range first.Tenants["ty"].Severities {
		ty += "    " + k + ": disable\n"
	}
	files["ty.yaml"] = ty
	doc := schedulesOnlyDoc(t, files, "2026-07-01T12:00:00Z")
	if s := doc.Tenants["ty"].Schedules; len(s) != 0 {
		t.Fatalf("ty's schedules = %v; this test is about a tenant serving none", s)
	}
	var segErr string
	for _, seg := range doc.Tenants["tx"].Schedules["mysql_connections"].Segments {
		if seg.Error != "" {
			segErr = seg.Error
		}
	}
	u := *doc.Ungathered
	if segErr == "" || len(u) != 1 || u[0].From != "01:00" || u[0].To != "03:00" || u[0].Error != segErr {
		t.Errorf("ungathered = %+v, want one part 01:00-03:00 carrying tx's segment error %q", u, segErr)
	}
}
