package handler

// #3062 → #3064: a whole-file PUT /api/v1/tenants/{id} writes the file {id}
// lives in, and one conf.d file may declare several tenants. #3062 spliced
// the sections a body declares into such a file; #3064 drew the line at the
// file instead: a tenant whose file is shared is git-managed, and no PUT
// reaches it — in either write mode, for any caller, whatever the body
// declares. The other tenant's section can therefore neither be dropped nor
// rewritten through the API.
//
// Rows, each in both write modes:
//   - own_body_refused: the body declares In alone (what a writer of In
//     sends). 409 TENANT_GIT_MANAGED, the file unchanged, no branch.
//   - own_body_refused_when_caller_writes_both: the same, for a caller who
//     may write both: the line is the file's shape, not the caller.
//   - rewrite_out_refused: the body declares Out too — not API-shaped, the
//     400 body refusal before the file is read; nothing names Out's values.
//   - rewrite_both_refused_when_caller_writes_both: likewise.
//   - own_file_accepted: the control — In's own file, the same body: 200,
//     the file is the body byte for byte.

import (
	"bytes"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/errcode"
	"github.com/vencil/tenant-api/internal/rbac"
)

const (
	foreignInSection  = "  " + orgEnfTenantIn + ":\n    _silent_mode: \"critical\"\n"
	foreignOutSection = "  " + orgEnfTenantOut + ":\n    _silent_mode: \"warning\"\n"
	// The file on disk: one conf.d file declaring both tenants.
	foreignSharedFile = "tenants:\n" + foreignInSection + foreignOutSection
	// In's section, changed.
	foreignInChanged = "  " + orgEnfTenantIn + ":\n    _silent_mode: \"disable\"\n"
	// Body (a): only In, changed — Out is not declared.
	foreignBodyOmitOut = "tenants:\n" + foreignInChanged
	// Body (b): In plus a rewritten Out.
	foreignBodyRewriteOut = "tenants:\n" + foreignInSection +
		"  " + orgEnfTenantOut + ":\n    _silent_mode: \"disable\"\n"
	// Body (c): both changed.
	foreignBodyRewriteBoth = "tenants:\n" + foreignInChanged +
		"  " + orgEnfTenantOut + ":\n    _silent_mode: \"critical\"\n"
)

func foreignFile(t *testing.T, f *orgEnfFixture) string {
	t.Helper()
	b, err := os.ReadFile(filepath.Join(f.configDir, orgEnfTenantIn+".yaml"))
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

// foreignPRBranchFile returns the tenant file as committed on the PR-mode
// feature branch(es) the write left behind ("" when there is none). The
// fixture has no origin, so the push fails and the branch is kept.
func foreignPRBranchFile(t *testing.T, f *orgEnfFixture) string {
	t.Helper()
	// The pushed branch is read from origin's tracking ref: the writer
	// drops the local one after the push (#641, #1724).
	out, err := exec.Command("git", "-C", f.configDir, "for-each-ref", "--format=%(refname)", "refs/heads", "refs/remotes").Output()
	if err != nil {
		t.Fatal(err)
	}
	var got []string
	for _, br := range strings.Fields(string(out)) {
		if br == "refs/heads/main" {
			continue
		}
		c, err := exec.Command("git", "-C", f.configDir, "show", br+":"+orgEnfTenantIn+".yaml").Output()
		if err != nil {
			continue
		}
		got = append(got, string(c))
	}
	return strings.Join(got, "\n")
}

// foreignModes are the two write modes every #3062 row runs in.
var foreignModes = []struct {
	name string
	pr   bool
}{{"direct", false}, {"pr", true}}

// foreignPut sends body as PUT /tenants/<In> by the member caller, against a
// fixture whose In file is file, and returns the answer, the file on disk
// and (PR mode) the file on the feature branch.
func foreignPut(t *testing.T, pr bool, file, body string, outWritable bool) (w *httptest.ResponseRecorder, f *orgEnfFixture, onDisk, branch string) {
	t.Helper()
	f = newOrgEnfFixture(t, map[string]string{orgEnfTenantIn + ".yaml": file})
	if outWritable {
		f.shareOut(t)
	}
	d := f.deps()
	if pr {
		d.WriteMode = WriteModePR
		d.PRClient = &mockPlatformClient{}
		d.PRTracker = &mockPlatformTracker{}
	}
	req := newRequestWithChiParam("PUT", "/api/v1/tenants/"+orgEnfTenantIn, "id", orgEnfTenantIn, bytes.NewBufferString(body))
	w = httptest.NewRecorder()
	wrapWithRBACMiddleware(PutTenant(d), f.rbacMgr, rbac.PermWrite, TenantIDFromPath).
		ServeHTTP(w, orgEnfIdentity(req, orgEnfMemberOrg))
	onDisk = foreignFile(t, f)
	if pr {
		branch = foreignPRBranchFile(t, f)
	}
	t.Logf("status=%d body=%s", w.Code, strings.TrimSpace(w.Body.String()))
	return w, f, onDisk, branch
}

// foreignWritten is the file a successful write left: on disk (direct) or
// on the feature branch (PR, where the disk keeps the base).
func foreignWritten(t *testing.T, pr bool, file, onDisk, branch string) string {
	t.Helper()
	if !pr {
		return onDisk
	}
	if onDisk != file {
		t.Errorf("PR mode: the serving tree changed:\n%s", onDisk)
	}
	return branch
}

// foreignRefused asserts a refusal with wantCode that changed nothing and
// whose text names none of hidden.
func foreignRefused(t *testing.T, w *httptest.ResponseRecorder, f *orgEnfFixture, file, onDisk, branch string, wantCode int, hidden ...string) {
	t.Helper()
	if w.Code != wantCode {
		t.Errorf("status = %d, want %d; body=%s", w.Code, wantCode, w.Body.String())
	}
	if onDisk != file {
		t.Errorf("the file changed on a refused PUT:\n%s", onDisk)
	}
	if n := f.writes.Load(); n != 0 {
		t.Errorf("writer commits = %d, want 0", n)
	}
	if branch != "" {
		t.Errorf("a PR branch carries the write:\n%s", branch)
	}
	for _, h := range hidden {
		if strings.Contains(w.Body.String(), h) {
			t.Errorf("the refusal names %q: %s", h, w.Body.String())
		}
	}
}

func TestPutTenant_ForeignSectionInSharedFile(t *testing.T) {
	t.Parallel()
	ownFile := "tenants:\n" + foreignInSection
	for _, mode := range foreignModes {
		for _, tc := range []struct {
			name, file, body string
			// outWritable: the caller may also write tenant Out.
			outWritable bool
			// wantStatus, wantText: the refusal; 200 writes the body.
			wantStatus int
			wantText   string
		}{
			{"own_body_refused", foreignSharedFile, foreignBodyOmitOut, false, http.StatusConflict, string(errcode.TenantGitManaged)},
			{"own_body_refused_when_caller_writes_both", foreignSharedFile, foreignBodyOmitOut, true, http.StatusConflict, string(errcode.TenantGitManaged)},
			{"rewrite_out_refused", foreignSharedFile, foreignBodyRewriteOut, false, http.StatusBadRequest, "and no other tenant"},
			{"rewrite_both_refused_when_caller_writes_both", foreignSharedFile, foreignBodyRewriteBoth, true, http.StatusBadRequest, "and no other tenant"},
			{"own_file_accepted", ownFile, foreignBodyOmitOut, false, http.StatusOK, ""},
		} {
			t.Run(mode.name+"/"+tc.name, func(t *testing.T) {
				t.Parallel()
				w, f, onDisk, branch := foreignPut(t, mode.pr, tc.file, tc.body, tc.outWritable)
				if tc.wantStatus != http.StatusOK {
					foreignRefused(t, w, f, tc.file, onDisk, branch, tc.wantStatus,
						orgEnfTenantOut, `"warning"`, `"disable"`)
					if !strings.Contains(w.Body.String(), tc.wantText) {
						t.Errorf("refusal text: %s, want %q", w.Body.String(), tc.wantText)
					}
					return
				}
				if w.Code != http.StatusOK {
					t.Fatalf("status = %d, want 200; body=%s", w.Code, w.Body.String())
				}
				if got := foreignWritten(t, mode.pr, tc.file, onDisk, branch); got != tc.body {
					t.Errorf("written file:\n%s\nwant:\n%s", got, tc.body)
				}
			})
		}
	}
}
