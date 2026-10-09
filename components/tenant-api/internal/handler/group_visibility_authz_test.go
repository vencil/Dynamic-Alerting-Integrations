package handler

// #1529 / #1530 / #1531: the group endpoints answered from the STORED member
// list without asking what the caller may see.
//
//   - #1529: PUT authorized only the members the request brought, then
//     replaced the stored list — so PUT could strip members DELETE refuses to
//     remove, and `members: []` skipped the check entirely.
//   - #1530: POST /groups/{id}/batch (route gate PermRead) listed every stored
//     member in its results, and every batch summary counted them.
//   - #1531: DELETE's 403 named stored members the caller cannot read.
//
// Every test here runs on the org-scoped enforce fixture: under a "*" read
// grant every member is readable, and none of these differences show.

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/groups"
	"github.com/vencil/tenant-api/internal/platform"
	"github.com/vencil/tenant-api/internal/rbac"
)

// visGroupsYAML: g-mixed has one member the caller's org owns and one it does
// not; g-out has only the other-org member (invisible to the caller).
const visGroupsYAML = `groups:
  g-mixed:
    label: Mixed
    members: [` + orgEnfTenantIn + `, ` + orgEnfTenantOut + `]
  g-out:
    label: OtherOrg
    members: [` + orgEnfTenantOut + `]
`

func visCall(t *testing.T, f *orgEnfFixture, d *Deps, h http.HandlerFunc, perm rbac.Permission, method, groupID, body string) *httptest.ResponseRecorder {
	t.Helper()
	path := "/api/v1/groups/" + groupID
	if strings.HasSuffix(method, "+batch") {
		method, path = "POST", path+"/batch"
	}
	req := newRequestWithChiParam(method, path, "id", groupID, bytes.NewBufferString(body))
	req.Header.Set("Content-Type", "application/json")
	req = orgEnfIdentity(req, orgEnfMemberOrg)
	w := httptest.NewRecorder()
	wrapWithRBACMiddleware(h, f.rbacMgr, perm, nil).ServeHTTP(w, req)
	return w
}

func storedMembers(t *testing.T, f *orgEnfFixture, groupID string) []string {
	t.Helper()
	data, err := os.ReadFile(filepath.Join(f.configDir, "_groups.yaml"))
	if err != nil {
		t.Fatalf("read _groups.yaml: %v", err)
	}
	cfg, err := groups.ParseConfig(data)
	if err != nil {
		t.Fatalf("parse _groups.yaml: %v", err)
	}
	g, ok := cfg.Groups[groupID]
	if !ok {
		return nil
	}
	return g.Members
}

// #1529: PUT may not drop a stored member the caller cannot write. The
// two-step sequence from the issue — PUT the foreign member out, then DELETE
// the now-"clean" group — is refused at the first step.
func TestPutGroup_StoredMembersGateTheReplace(t *testing.T) {
	t.Parallel()
	f := newOrgEnfFixture(t, map[string]string{"_groups.yaml": visGroupsYAML})
	d := f.deps()

	w := visCall(t, f, d, PutGroup(d), rbac.PermWrite, "PUT", "g-mixed",
		`{"label":"x","members":["`+orgEnfTenantIn+`"]}`)
	if w.Code != http.StatusForbidden {
		t.Fatalf("PUT dropping a foreign member: status = %d, want 403; body=%s", w.Code, w.Body.String())
	}
	if strings.Contains(w.Body.String(), orgEnfTenantOut) {
		t.Errorf("403 names a member the caller cannot read (#1531): %s", w.Body.String())
	}
	if !strings.Contains(w.Body.String(), "cannot view") {
		t.Errorf("403 does not say a hidden member blocked it: %s", w.Body.String())
	}

	w = visCall(t, f, d, DeleteGroup(d), rbac.PermWrite, "DELETE", "g-mixed", "")
	if w.Code != http.StatusForbidden {
		t.Errorf("DELETE after the refused PUT: status = %d, want 403; body=%s", w.Code, w.Body.String())
	}
	if got := storedMembers(t, f, "g-mixed"); len(got) != 2 {
		t.Errorf("g-mixed members on disk = %v, want both still there", got)
	}
	if n := f.writes.Load(); n != 0 {
		t.Errorf("refused requests committed %d time(s), want 0", n)
	}
}

// #1529: `members: []` used to skip the member check entirely
// (tenantsLackingPermission returns nil for an empty list), so it could empty a
// group the caller cannot even see.
func TestPutGroup_EmptyMembersCannotStripAForeignGroup(t *testing.T) {
	t.Parallel()
	f := newOrgEnfFixture(t, map[string]string{"_groups.yaml": visGroupsYAML})
	d := f.deps()

	w := visCall(t, f, d, PutGroup(d), rbac.PermWrite, "PUT", "g-out", `{"label":"x","members":[]}`)
	if w.Code != http.StatusForbidden {
		t.Fatalf("status = %d, want 403; body=%s", w.Code, w.Body.String())
	}
	if strings.Contains(w.Body.String(), orgEnfTenantOut) {
		t.Errorf("403 names a member the caller cannot read: %s", w.Body.String())
	}
	if got := storedMembers(t, f, "g-out"); len(got) != 1 || got[0] != orgEnfTenantOut {
		t.Errorf("g-out members on disk = %v, want [%s]", got, orgEnfTenantOut)
	}
	if n := f.writes.Load(); n != 0 {
		t.Errorf("refused request committed %d time(s), want 0", n)
	}
}

// The new check is on STORED members only: a group whose stored members the
// caller may all write is still replaced as before, and creating a group is
// untouched.
func TestPutGroup_WritableStoredMembersStillReplace(t *testing.T) {
	t.Parallel()
	f := newOrgEnfFixture(t, map[string]string{"_groups.yaml": "groups:\n  g-in:\n    label: In\n    members: [" + orgEnfTenantIn + "]\n"})
	d := f.deps()

	if w := visCall(t, f, d, PutGroup(d), rbac.PermWrite, "PUT", "g-in", `{"label":"y","members":[]}`); w.Code != http.StatusOK {
		t.Fatalf("replace with writable stored members: status = %d, want 200; body=%s", w.Code, w.Body.String())
	}
	if got := storedMembers(t, f, "g-in"); len(got) != 0 {
		t.Errorf("g-in members on disk = %v, want []", got)
	}
	if w := visCall(t, f, d, PutGroup(d), rbac.PermWrite, "PUT", "g-new", `{"label":"n","members":["`+orgEnfTenantIn+`"]}`); w.Code != http.StatusOK {
		t.Errorf("create: status = %d, want 200; body=%s", w.Code, w.Body.String())
	}
}

// #1531: a stored member the caller cannot read is not named in DELETE's 403.
// (A stored member the caller CAN read but not write is still named:
// TestDeleteGroup_* in group_delete_authz_test.go, under a "*" read grant.)
func TestDeleteGroup_403DoesNotNameUnreadableMembers(t *testing.T) {
	t.Parallel()
	f := newOrgEnfFixture(t, map[string]string{"_groups.yaml": visGroupsYAML})
	d := f.deps()

	w := visCall(t, f, d, DeleteGroup(d), rbac.PermWrite, "DELETE", "g-mixed", "")
	if w.Code != http.StatusForbidden {
		t.Fatalf("status = %d, want 403; body=%s", w.Code, w.Body.String())
	}
	if strings.Contains(w.Body.String(), orgEnfTenantOut) {
		t.Errorf("403 names the member GET /groups/g-mixed hides: %s", w.Body.String())
	}
	if !strings.Contains(w.Body.String(), "cannot view") {
		t.Errorf("403 does not say a hidden member blocked it: %s", w.Body.String())
	}
}

// #1531, both kinds of stored member in one 403: one the caller can read but
// not write is named; one it cannot read is only acknowledged.
func TestDeleteGroup_403NamesReadableAndAcknowledgesHidden(t *testing.T) {
	t.Parallel()
	const readOnlyTenant = "t-readonly"
	f := newOrgEnfFixture(t, map[string]string{"_groups.yaml": "groups:\n  g3:\n    label: Three\n    members: [" +
		orgEnfTenantIn + ", " + orgEnfTenantOut + ", " + readOnlyTenant + "]\n"})
	// A second, non-org-scoped rule grants READ on t-readonly only. t-readonly
	// carries no org label, so the org-scoped writer rule cannot write it under
	// enforce: readable, not writable.
	f.rbacMgr = newRBACManagerWithClaims(t, orgEnfRBACYAML+`  - name: ro-extra
    tenants: ["`+readOnlyTenant+`"]
    permissions: [read]
`, map[string]string{"org": orgEnfClaimHeader})
	f.rbacMgr.EnableOrgScopeEnforce()
	d := f.deps()

	req := newRequestWithChiParam("DELETE", "/api/v1/groups/g3", "id", "g3", nil)
	req = orgEnfIdentity(req, orgEnfMemberOrg)
	req.Header.Set("X-Forwarded-Groups", orgEnfGroup+",ro-extra")
	w := httptest.NewRecorder()
	wrapWithRBACMiddleware(DeleteGroup(d), f.rbacMgr, rbac.PermWrite, nil).ServeHTTP(w, req)

	if w.Code != http.StatusForbidden {
		t.Fatalf("status = %d, want 403; body=%s", w.Code, w.Body.String())
	}
	body := w.Body.String()
	if !strings.Contains(body, "forbidden member tenants: "+readOnlyTenant+"; it also has member tenants you cannot view") {
		t.Errorf("403 should name %s and acknowledge the hidden member: %s", readOnlyTenant, body)
	}
	if strings.Contains(body, orgEnfTenantOut) {
		t.Errorf("403 names the member the caller cannot read: %s", body)
	}
}

// DELETE re-judges visibility in the lock, on the group as stored: the snapshot
// can be stale (groupMgr has no WatchLoop). Here the snapshot still has the
// member-org tenant, while the file now has only the other-org one — DELETE
// answers the 404 of a missing group, and writes nothing.
func TestDeleteGroup_InLockVisibilityUsesTheFileNotTheSnapshot(t *testing.T) {
	t.Parallel()
	f := newOrgEnfFixture(t, map[string]string{"_groups.yaml": "groups:\n  g-mixed:\n    label: M\n    members: [" + orgEnfTenantIn + "]\n"})
	d := f.deps() // the snapshot is loaded here: g-mixed = [in]
	if err := os.WriteFile(filepath.Join(f.configDir, "_groups.yaml"),
		[]byte("groups:\n  g-mixed:\n    label: M\n    members: ["+orgEnfTenantOut+"]\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	hidden := visCall(t, f, d, DeleteGroup(d), rbac.PermWrite, "DELETE", "g-mixed", "")
	missing := visCall(t, f, d, DeleteGroup(d), rbac.PermWrite, "DELETE", "g-none", "")
	if hidden.Code != http.StatusNotFound ||
		strings.ReplaceAll(hidden.Body.String(), "g-mixed", "<id>") != strings.ReplaceAll(missing.Body.String(), "g-none", "<id>") {
		t.Errorf("stale-snapshot DELETE = %d %s; missing group = %d %s — want the same 404",
			hidden.Code, hidden.Body.String(), missing.Code, missing.Body.String())
	}
	if got := storedMembers(t, f, "g-mixed"); len(got) != 1 || got[0] != orgEnfTenantOut {
		t.Errorf("g-mixed on disk = %v, want it untouched", got)
	}
	if n := f.writes.Load(); n != 0 {
		t.Errorf("refused DELETE committed %d time(s), want 0", n)
	}
}

// #1530 / #1531: a group the caller cannot see gets, from GET, DELETE and the
// batch endpoint (well-formed body or not), the same answer as a group that
// does not exist — so none of them confirms it.
func TestGroupInvisible_AnsweredLikeMissing(t *testing.T) {
	t.Parallel()
	f := newOrgEnfFixture(t, map[string]string{"_groups.yaml": visGroupsYAML})
	d := f.deps()

	type call struct {
		name   string
		h      http.HandlerFunc
		perm   rbac.Permission
		method string
		body   string
	}
	calls := []call{
		{"GET", GetGroup(d), rbac.PermRead, "GET", ""},
		{"DELETE", DeleteGroup(d), rbac.PermWrite, "DELETE", ""},
		{"batch", GroupBatch(d), rbac.PermRead, "POST+batch", `{"patch":{"_silent_mode":"warning"}}`},
		{"batch empty patch", GroupBatch(d), rbac.PermRead, "POST+batch", `{"patch":{}}`},
		{"batch bad JSON", GroupBatch(d), rbac.PermRead, "POST+batch", `{`},
	}
	for _, c := range calls {
		hidden := visCall(t, f, d, c.h, c.perm, c.method, "g-out", c.body)
		missing := visCall(t, f, d, c.h, c.perm, c.method, "g-none", c.body)
		got := strings.ReplaceAll(hidden.Body.String(), "g-out", "<id>")
		want := strings.ReplaceAll(missing.Body.String(), "g-none", "<id>")
		if hidden.Code != missing.Code || got != want {
			t.Errorf("%s: invisible group = %d %s; missing group = %d %s — they must not differ",
				c.name, hidden.Code, hidden.Body.String(), missing.Code, missing.Body.String())
		}
		if hidden.Code != http.StatusNotFound {
			t.Errorf("%s: invisible group status = %d, want 404", c.name, hidden.Code)
		}
	}
	if n := f.writes.Load(); n != 0 {
		t.Errorf("calls on an invisible group committed %d time(s), want 0", n)
	}
}

// An empty group is visible to every caller, so it stays listable, readable
// and deletable. Before, it was a 404 to everyone once _rbac.yaml had rules —
// platform admins included — and putting the GET predicate on DELETE as-is
// would have left it impossible to delete.
func TestListGroups_EmptyGroupVisible(t *testing.T) {
	t.Parallel()
	f := newOrgEnfFixture(t, map[string]string{"_groups.yaml": "groups:\n  g-empty:\n    label: E\n    members: []\n"})
	d := f.deps()

	req := httptest.NewRequest("GET", "/api/v1/groups", nil)
	req = orgEnfIdentity(req, orgEnfMemberOrg)
	w := httptest.NewRecorder()
	wrapWithRBACMiddleware(ListGroups(d), f.rbacMgr, rbac.PermRead, nil).ServeHTTP(w, req)
	if w.Code != http.StatusOK || !strings.Contains(w.Body.String(), `"g-empty"`) {
		t.Errorf("LIST = %d %s, want g-empty listed", w.Code, w.Body.String())
	}
	if w := visCall(t, f, d, GetGroup(d), rbac.PermRead, "GET", "g-empty", ""); w.Code != http.StatusOK {
		t.Errorf("GET = %d %s, want 200", w.Code, w.Body.String())
	}
	if w := visCall(t, f, d, DeleteGroup(d), rbac.PermWrite, "DELETE", "g-empty", ""); w.Code != http.StatusOK {
		t.Errorf("DELETE = %d %s, want 200", w.Code, w.Body.String())
	}
}

// #1530, PR write-back mode: the group batch response names only members the
// caller may read, and its summary counts only those.
func TestGroupBatch_PRMode_HidesUnreadableMembers(t *testing.T) {
	t.Parallel()
	f := newOrgEnfFixture(t, map[string]string{"_groups.yaml": visGroupsYAML})
	d := f.deps()
	d.WriteMode = WriteModePR
	d.PRClient = &mockPlatformClient{
		createPRFunc: func(title, body, headBranch string, labels []string) (*platform.PRInfo, error) {
			return &platform.PRInfo{Number: 5, WebURL: "https://gh/5", State: "open", HeadRef: headBranch}, nil
		},
	}
	d.PRTracker = &mockPlatformTracker{}

	w := visCall(t, f, d, GroupBatch(d), rbac.PermRead, "POST+batch", "g-mixed", `{"patch":{"_silent_mode":"warning"}}`)
	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200; body=%s", w.Code, w.Body.String())
	}
	var resp GroupBatchResponse
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if strings.Contains(w.Body.String(), orgEnfTenantOut) {
		t.Errorf("response names the member GET /groups/g-mixed hides: %s", w.Body.String())
	}
	if len(resp.Results) != 1 || resp.Results[0].TenantID != orgEnfTenantIn || resp.Results[0].Status != "included" {
		t.Errorf("results = %+v, want only %s included", resp.Results, orgEnfTenantIn)
	}
	if want := "1 included in PR/MR, 0 failed"; resp.Summary != want {
		t.Errorf("summary = %q, want %q (it must not count the hidden member)", resp.Summary, want)
	}
}

// prBatchSummary is now the only source of runBatchPR's summary line; these
// are the three strings it rendered inline before, for /tenants/batch.
func TestPRBatchSummary_MatchesPreviousWording(t *testing.T) {
	t.Parallel()
	inc := BatchResult{Status: "included"}
	bad := BatchResult{Status: "error"}
	cases := []struct {
		status  string
		results []BatchResult
		want    string
	}{
		{"completed", []BatchResult{bad, bad}, "2 failed"},
		{"completed", []BatchResult{inc, bad, inc}, "2 unchanged"},
		{"pending_review", []BatchResult{inc, bad, inc}, "2 included in PR/MR, 1 failed"},
		{"pending_review", []BatchResult{inc}, "1 included in PR/MR, 0 failed"},
	}
	for _, c := range cases {
		if got := prBatchSummary(c.status, c.results); got != c.want {
			t.Errorf("prBatchSummary(%q, %d results) = %q, want %q", c.status, len(c.results), got, c.want)
		}
	}
}
