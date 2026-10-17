package handler

// #3062, beyond the four rows of TestPutTenant_ForeignSectionInSharedFile:
// which tenants a body declares is decided on decoded ids, a merge key is
// refused, the splice is refused when it would change what the body does not
// declare (anchors, aliases), and what a shared file holds besides tenant
// sections is kept. Every row runs in both write modes, and no refusal names
// the other tenant or its value.

import (
	"bytes"
	"encoding/base64"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/rbac"
	cfg "github.com/vencil/threshold-exporter/pkg/config"
)

// Out's value in these files, which no refusal may quote.
const spliceOutValue = `"warning"`

// (a) Out declared in every spelling that decodes to its id: each is Out,
// and the caller may not write Out → 403, nothing written.
func TestPutTenant_DeclaredTenantIsTheDecodedID(t *testing.T) {
	t.Parallel()
	out := orgEnfTenantOut
	in := "tenants:\n" + foreignInSection
	section := ":\n    _silent_mode: \"disable\"\n"
	spellings := map[string]string{
		"double_quoted": in + "  \"" + out + "\"" + section,
		"single_quoted": in + "  '" + out + "'" + section,
		"str_tag":       in + "  !!str " + out + section,
		"binary":        in + "  !!binary " + base64.StdEncoding.EncodeToString([]byte(out)) + section,
		// The alias names a scalar the body anchored elsewhere.
		"alias_key": "tenants:\n  " + orgEnfTenantIn + ":\n    _silent_mode: \"critical\"\n" +
			"    _metadata:\n      owner: &o " + out + "\n  *o " + section,
	}
	for _, mode := range foreignModes {
		for name, body := range spellings {
			t.Run(mode.name+"/"+name, func(t *testing.T) {
				t.Parallel()
				w, f, onDisk, branch := foreignPut(t, mode.pr, foreignSharedFile, body, false)
				foreignRefused(t, w, f, foreignSharedFile, onDisk, branch, http.StatusForbidden, out, spliceOutValue)
				if !strings.Contains(w.Body.String(), "declares a tenant section you may not write") {
					t.Errorf("refusal text: %s", w.Body.String())
				}
			})
		}
	}
}

// (b) a merge key at the `tenants:` level — in the body (400) or in the file
// (409) — is refused: what it declares depends on what it merges.
func TestPutTenant_TenantsMergeKeyRefused(t *testing.T) {
	t.Parallel()
	mergeFile := "tenants:\n" + foreignInSection +
		"  <<: {" + orgEnfTenantOut + ": {_silent_mode: " + spliceOutValue + "}}\n"
	for _, mode := range foreignModes {
		for _, tc := range []struct {
			name, file, body string
			code             int
			text             string // JSON escapes the "<<" that follows
		}{
			{"in_body", foreignSharedFile, "tenants:\n" + foreignInChanged +
				"  <<: {" + orgEnfTenantOut + ": {_silent_mode: \"disable\"}}\n", http.StatusBadRequest, "merge key ("},
			{"in_file", mergeFile, foreignBodyOmitOut, http.StatusConflict, "merge key ("},
		} {
			t.Run(mode.name+"/"+tc.name, func(t *testing.T) {
				t.Parallel()
				w, f, onDisk, branch := foreignPut(t, mode.pr, tc.file, tc.body, false)
				foreignRefused(t, w, f, tc.file, onDisk, branch, tc.code, orgEnfTenantOut, spliceOutValue)
				if !strings.Contains(w.Body.String(), tc.text) {
					t.Errorf("refusal does not say %q: %s", tc.text, w.Body.String())
				}
			})
		}
	}
}

// (c) anchors and aliases across sections. Out's value is an alias of an
// anchor in In's section: In's body may keep that anchor and its value
// (Out's resolved value is unchanged), but not change the value, drop the
// anchor or define it a second time — each would change Out.
func TestPutTenant_SpliceKeepsAliasesIntoDeclaredSections(t *testing.T) {
	t.Parallel()
	const file = "tenants:\n" +
		"  " + orgEnfTenantIn + ":\n    _silent_mode: &m \"warning\"\n" +
		"  " + orgEnfTenantOut + ":\n    _silent_mode: *m\n"
	inBody := func(section string) string { return "tenants:\n  " + orgEnfTenantIn + ":\n" + section }
	for _, mode := range foreignModes {
		for _, tc := range []struct {
			name, body string
			want       string // the written file on a 200; "" for a 409
		}{
			{"anchor_value_changed", inBody("    _silent_mode: &m \"disable\"\n"), ""},
			{"anchor_dropped", inBody("    _silent_mode: \"warning\"\n"), ""},
			{"anchor_defined_twice", inBody("    _silent_mode: &m \"warning\"\n    _metadata:\n      owner: &m team\n"), ""},
			{"anchor_kept", inBody("    _silent_mode: &m \"warning\"\n    _metadata:\n      owner: team\n"),
				"tenants:\n  " + orgEnfTenantIn + ":\n    _silent_mode: &m \"warning\"\n    _metadata:\n      owner: team\n" +
					"  " + orgEnfTenantOut + ":\n    _silent_mode: *m\n"},
		} {
			t.Run(mode.name+"/"+tc.name, func(t *testing.T) {
				t.Parallel()
				w, f, onDisk, branch := foreignPut(t, mode.pr, file, tc.body, false)
				if tc.want == "" {
					foreignRefused(t, w, f, file, onDisk, branch, http.StatusConflict, orgEnfTenantOut)
					if !strings.Contains(w.Body.String(), configErrorNotRoundTrippable) {
						t.Errorf("refusal is not %s: %s", configErrorNotRoundTrippable, w.Body.String())
					}
					return
				}
				if w.Code != http.StatusOK {
					t.Fatalf("status = %d, want 200; body=%s", w.Code, w.Body.String())
				}
				if got := foreignWritten(t, mode.pr, file, onDisk, branch); got != tc.want {
					t.Errorf("written file:\n%s\nwant:\n%s", got, tc.want)
				}
			})
		}
	}
}

// (d) what a shared file holds besides tenant sections — a root key other
// than `tenants` (a body may not carry one), comments — is kept, as is Out.
func TestPutTenant_SpliceKeepsRootKeysAndComments(t *testing.T) {
	t.Parallel()
	const file = "# shared file, owned by two teams\n" +
		"tenants:\n" + foreignInSection +
		"  # Out's own comment\n" + foreignOutSection +
		"x-notes:\n  keep: \"me\"\n"
	const want = "# shared file, owned by two teams\n" +
		"tenants:\n" + foreignInChanged +
		"  # Out's own comment\n" + foreignOutSection +
		"x-notes:\n  keep: \"me\"\n"
	for _, mode := range foreignModes {
		t.Run(mode.name, func(t *testing.T) {
			t.Parallel()
			w, _, onDisk, branch := foreignPut(t, mode.pr, file, foreignBodyOmitOut, false)
			if w.Code != http.StatusOK {
				t.Fatalf("status = %d, want 200; body=%s", w.Code, w.Body.String())
			}
			if got := foreignWritten(t, mode.pr, file, onDisk, branch); got != want {
				t.Errorf("written file:\n%s\nwant:\n%s", got, want)
			}
		})
	}
}

// X-DA-Base-Hash on a shared file (direct mode only): the precondition is
// judged first, on the whole file's bytes; with a current hash the splice
// keeps Out.
func TestPutTenant_SplicePreconditionFirst(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct {
		name, hash string
		code       int
	}{
		{"stale", cfg.ComputeSourceHash([]byte(foreignBodyOmitOut)), http.StatusConflict},
		{"current", cfg.ComputeSourceHash([]byte(foreignSharedFile)), http.StatusOK},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			f := newOrgEnfFixture(t, map[string]string{orgEnfTenantIn + ".yaml": foreignSharedFile})
			req := newRequestWithChiParam("PUT", "/api/v1/tenants/"+orgEnfTenantIn, "id", orgEnfTenantIn, bytes.NewBufferString(foreignBodyOmitOut))
			req.Header.Set(BaseHashHeader, tc.hash)
			w := httptest.NewRecorder()
			wrapWithRBACMiddleware(PutTenant(f.deps()), f.rbacMgr, rbac.PermWrite, TenantIDFromPath).
				ServeHTTP(w, orgEnfIdentity(req, orgEnfMemberOrg))
			if w.Code != tc.code {
				t.Fatalf("status = %d, want %d; body=%s", w.Code, tc.code, w.Body.String())
			}
			want := foreignSharedFile
			if tc.code == http.StatusOK {
				want = foreignOmitOutResult
			} else if !strings.Contains(w.Body.String(), CodeConflict) {
				t.Errorf("not the precondition's 409: %s", w.Body.String())
			}
			if got := foreignFile(t, f); got != want {
				t.Errorf("file:\n%s\nwant:\n%s", got, want)
			}
		})
	}
}
