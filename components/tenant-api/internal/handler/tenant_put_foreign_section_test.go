package handler

// #3062: a whole-file PUT /api/v1/tenants/{id} writes the file {id} lives in,
// and one conf.d file may declare several tenants. addedTenantKeys refused a
// body that ADDS another tenant's section, but a section the file already
// declared was not looked at: a caller with write access to tenant In only
// could drop or rewrite tenant Out's section when the two share a file.
//
// Since the fix a PUT body DECLARES sections: each one it declares must be a
// tenant the caller may write, and the sections it does not declare are kept
// as they are (the server splices the declared ones into the file).
//
// Rows, each in both write modes:
//   - omit_out_kept: the old "delete" attack. Leaving Out out of the body no
//     longer deletes it — omission means "not mine, keep it" — so the write
//     of In's change is a 200 and Out's section is there, unchanged.
//   - rewrite_out_denied: declaring Out with other content is a 403 with a
//     fixed text that names no tenant; the file's bytes do not change and no
//     commit or branch is left.
//   - omit_out_kept_when_caller_writes_both: the control for the first row
//     with a caller who may write both — still kept: omission never deletes
//     (the API has no endpoint that removes a tenant).
//   - rewrite_both_when_caller_writes_both: a caller who may write both
//     changes both sections in one PUT; the body declares everything the
//     file holds, so the file is the body, byte for byte.

import (
	"bytes"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/rbac"
	"github.com/vencil/tenant-api/internal/tenantorg"
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
	// What (a) leaves: In changed, Out as it was.
	foreignOmitOutResult = "tenants:\n" + foreignInChanged + foreignOutSection
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
	out, err := exec.Command("git", "-C", f.configDir, "for-each-ref", "--format=%(refname:short)", "refs/heads").Output()
	if err != nil {
		t.Fatal(err)
	}
	var got []string
	for _, br := range strings.Fields(string(out)) {
		if br == "main" {
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
		f.tenantOrg.Override(&tenantorg.Config{TenantOrgs: map[string][]string{
			orgEnfTenantIn: {orgEnfMemberOrg}, orgEnfTenantOut: {orgEnfMemberOrg}}})
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
	for _, mode := range foreignModes {
		for _, tc := range []struct {
			name, body string
			// outWritable: the caller may also write tenant Out.
			outWritable bool
			// want: the written file on a 200; "" for the 403 refusal.
			want string
		}{
			{"omit_out_kept", foreignBodyOmitOut, false, foreignOmitOutResult},
			{"rewrite_out_denied", foreignBodyRewriteOut, false, ""},
			{"omit_out_kept_when_caller_writes_both", foreignBodyOmitOut, true, foreignOmitOutResult},
			{"rewrite_both_when_caller_writes_both", foreignBodyRewriteBoth, true, foreignBodyRewriteBoth},
		} {
			t.Run(mode.name+"/"+tc.name, func(t *testing.T) {
				t.Parallel()
				w, f, onDisk, branch := foreignPut(t, mode.pr, foreignSharedFile, tc.body, tc.outWritable)
				if tc.want == "" {
					foreignRefused(t, w, f, foreignSharedFile, onDisk, branch, http.StatusForbidden,
						orgEnfTenantOut, `"warning"`, `"disable"`)
					if !strings.Contains(w.Body.String(), "declares a tenant section you may not write") {
						t.Errorf("refusal text: %s", w.Body.String())
					}
					return
				}
				if w.Code != http.StatusOK {
					t.Fatalf("status = %d, want 200; body=%s", w.Code, w.Body.String())
				}
				if got := foreignWritten(t, mode.pr, foreignSharedFile, onDisk, branch); got != tc.want {
					t.Errorf("written file:\n%s\nwant:\n%s", got, tc.want)
				}
			})
		}
	}
}
