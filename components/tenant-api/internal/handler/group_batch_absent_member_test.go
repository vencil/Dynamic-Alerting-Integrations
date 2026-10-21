package handler

// #2882: a group's member list can name a tenant that no longer exists —
// offboarding deletes the tenant's conf.d file but not its group
// memberships. Before the fix a group batch patch re-created that tenant's
// file (mergePatchYAML builds a new file for an empty base) and answered
// "2 succeeded". Now the member is skipped, judged inside the merge on the
// base the write lands on, in both write modes.

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/async"
)

const goneGroupsYAML = "groups:\n  g-fin:\n    label: Finance\n    members: [t-ok, t-gone]\n"

func newAbsentMemberFixture(t *testing.T, mode WriteMode, groupsYAML string) *groupBatchFixture {
	t.Helper()
	files := batchTree()
	files["_groups.yaml"] = groupsYAML
	return newGroupBatchFixtureOver(t, mode, files)
}

func assertNoFile(t *testing.T, configDir, name string) {
	t.Helper()
	if b, err := os.ReadFile(filepath.Join(configDir, name)); err == nil {
		t.Errorf("%s was created for a member that does not exist:\n%s", name, b)
	}
}

func TestGroupBatch_AbsentMemberSkipped_Direct(t *testing.T) {
	f := newAbsentMemberFixture(t, WriteModeDirect, goneGroupsYAML)
	resp := decodeGroupBatch(t, f.post(t, `{"patch":{"_silent_mode":"warning"}}`))

	got := map[string]BatchResult{}
	for _, r := range resp.Results {
		got[r.TenantID] = r
	}
	if got["t-ok"].Status != "ok" {
		t.Errorf("t-ok = %+v, want ok", got["t-ok"])
	}
	if r := got["t-gone"]; r.Status != "skipped" || r.Message != msgTenantAbsent {
		t.Errorf("t-gone = %+v, want skipped with %q", r, msgTenantAbsent)
	}
	if resp.Summary != "1 succeeded, 1 skipped" {
		t.Errorf("summary = %q, want %q", resp.Summary, "1 succeeded, 1 skipped")
	}
	assertNoFile(t, f.configDir, "t-gone.yaml")
	ok, _ := os.ReadFile(filepath.Join(f.configDir, "t-ok.yaml"))
	if !strings.Contains(string(ok), `_silent_mode: "warning"`) {
		t.Errorf("t-ok.yaml not patched:\n%s", ok)
	}
}

func TestGroupBatch_AbsentMemberSkipped_PR(t *testing.T) {
	f := newAbsentMemberFixture(t, WriteModePR, goneGroupsYAML)
	resp := decodeGroupBatch(t, f.post(t, `{"patch":{"_silent_mode":"warning"}}`))

	if resp.Status != "pending_review" || f.prCalls != 1 {
		t.Fatalf("status = %q, CreatePR calls = %d; want pending_review with one PR", resp.Status, f.prCalls)
	}
	got := map[string]BatchResult{}
	for _, r := range resp.Results {
		got[r.TenantID] = r
	}
	if got["t-ok"].Status != "included" {
		t.Errorf("t-ok = %+v, want included", got["t-ok"])
	}
	if r := got["t-gone"]; r.Status != "skipped" || r.Message != msgTenantAbsent {
		t.Errorf("t-gone = %+v, want skipped with %q", r, msgTenantAbsent)
	}
	if want := "1 included in PR/MR, 0 failed, 1 skipped"; resp.Summary != want {
		t.Errorf("summary = %q, want %q", resp.Summary, want)
	}
	if !strings.Contains(f.title, "1 tenants") {
		t.Errorf("PR title = %q, want it to count only the member written", f.title)
	}
	if out, err := exec.Command("git", "-C", f.configDir, "show", f.head+":t-gone.yaml").CombinedOutput(); err == nil {
		t.Errorf("PR branch %s carries t-gone.yaml:\n%s", f.head, out)
	}
	assertNoFile(t, f.configDir, "t-gone.yaml")
}

// Every member absent: nothing to write in either mode — no PR/MR, no file.
func TestGroupBatch_AllMembersAbsent(t *testing.T) {
	for _, mode := range []WriteMode{WriteModePR, WriteModeDirect} {
		t.Run(string(mode), func(t *testing.T) {
			f := newAbsentMemberFixture(t, mode, "groups:\n  g-fin:\n    label: Finance\n    members: [t-gone]\n")
			before := f.mainSHA(t)
			resp := decodeGroupBatch(t, f.post(t, `{"patch":{"_silent_mode":"warning"}}`))
			if len(resp.Results) != 1 || resp.Results[0].Status != "skipped" || resp.Summary != "1 skipped" {
				t.Errorf("response = %+v, want t-gone skipped and summary \"1 skipped\"", resp)
			}
			if f.prCalls != 0 {
				t.Errorf("CreatePR calls = %d, want 0", f.prCalls)
			}
			if after := f.mainSHA(t); after != before {
				t.Errorf("base branch moved %s → %s", before, after)
			}
			assertNoFile(t, f.configDir, "t-gone.yaml")
		})
	}
}

func TestTenantAbsentIn(t *testing.T) {
	for _, c := range []struct {
		name, existing string
		want           bool
	}{
		{"missing file", "", true},
		{"blank file", "\n  \n", true},
		{"no section for the tenant", "tenants:\n  t-other:\n    cpu_usage_percent: '80'\n", true},
		{"section present", "tenants:\n  t-x:\n    cpu_usage_percent: '80'\n", false},
		{"empty section", "tenants:\n  t-x: {}\n", false},
		// Left to mergePatchYAML, which refuses these rather than writing.
		{"not yaml", "tenants: [\n", false},
		{"no tenants mapping", "other: 1\n", false},
		// #3206: parse-tree refuses these, so they are not read as absent
		// (the first document alone has no t-x section) — mergePatchYAML
		// refuses them as it refuses the rows above.
		{"t-x after a second document", "tenants:\n  t-other: {}\n---\ntenants:\n  t-x: {}\n", false},
		{"trailing empty document", "tenants:\n  t-other: {}\n---\n", false},
		{"!!binary value", "tenants:\n  t-other:\n    k: !!binary dg==\n", false},
		{"non-specific tag", "tenants:\n  t-other:\n    k: ! \"v\"\n", false},
	} {
		if got := tenantAbsentIn([]byte(c.existing), "t-x"); got != c.want {
			t.Errorf("%s: tenantAbsentIn = %v, want %v", c.name, got, c.want)
		}
	}
}

func TestSummarizeResults_Skipped(t *testing.T) {
	for _, c := range []struct {
		statuses []string
		want     string
	}{
		{[]string{"ok", "ok"}, "2 succeeded"},
		{[]string{"ok", "error"}, "1 succeeded, 1 failed"},
		{[]string{"ok", "skipped"}, "1 succeeded, 1 skipped"},
		{[]string{"error", "skipped"}, "1 failed, 1 skipped"},
		{[]string{"ok", "error", "skipped"}, "1 succeeded, 1 failed, 1 skipped"},
		{[]string{"skipped", "skipped"}, "2 skipped"},
	} {
		results := make([]async.TaskResult, len(c.statuses))
		for i, s := range c.statuses {
			results[i] = async.TaskResult{Status: s}
		}
		if got := async.SummarizeResults(results); got != c.want {
			t.Errorf("%v: summary = %q, want %q", c.statuses, got, c.want)
		}
	}
}
