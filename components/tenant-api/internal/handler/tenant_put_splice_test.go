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
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os/exec"
	"path/filepath"
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

// (b) a merge key at the `tenants:` level — in the body, or in the file — is
// refused: what it declares depends on what it merges. In the file it is one
// of the shapes the splice cannot read, so #2405 answers (400 for this
// caller, who lacks write permission on all tenants; see
// TestPutTenant_UnspliceableBaseFollowsCaller).
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
			{"in_file", mergeFile, foreignBodyOmitOut, http.StatusBadRequest, "write permission on all tenants"},
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
// anchor in In's section: a caller who may read Out may keep that anchor and
// its value in In's body (Out's resolved value is unchanged), but not change
// the value, drop the anchor or define it a second time — each would change
// Out. A caller who may not read Out is refused all four alike: the section
// it replaces holds an anchor, and a value comparison would tell it Out's
// value (TestPutTenant_AnchorRuleIsNotAValueOracle).
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
			t.Run(mode.name+"/withheld/"+tc.name, func(t *testing.T) {
				t.Parallel()
				w, f, onDisk, branch := foreignPut(t, mode.pr, file, tc.body, false)
				// The caller may not read Out: refused whatever the body
				// keeps, and the refusal says nothing about how the file is
				// written (#3050's rule).
				foreignRefused(t, w, f, file, onDisk, branch, http.StatusConflict, orgEnfTenantOut, "anchor", "alias", "tenants.")
				if !strings.Contains(w.Body.String(), configErrorNotRoundTrippable) ||
					!strings.Contains(w.Body.String(), msgSpliceDetailWithheld) {
					t.Errorf("refusal is not %s with the withheld detail: %s", configErrorNotRoundTrippable, w.Body.String())
				}
			})
			t.Run(mode.name+"/full_reader/"+tc.name, func(t *testing.T) {
				t.Parallel()
				w, f, onDisk, branch := foreignPut(t, mode.pr, file, tc.body, true)
				if tc.want == "" {
					foreignRefused(t, w, f, file, onDisk, branch, http.StatusConflict)
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

// The 409's detail to a caller who may read every tenant in the file: the
// place of the first difference, named.
func TestPutTenant_SpliceRefusalDetailShownToFullReader(t *testing.T) {
	t.Parallel()
	const file = "tenants:\n" +
		"  " + orgEnfTenantIn + ":\n    _silent_mode: &m \"warning\"\n" +
		"  " + orgEnfTenantOut + ":\n    _silent_mode: *m\n"
	body := "tenants:\n  " + orgEnfTenantIn + ":\n    _silent_mode: &m \"disable\"\n"
	for _, mode := range foreignModes {
		t.Run(mode.name, func(t *testing.T) {
			t.Parallel()
			w, f, onDisk, branch := foreignPut(t, mode.pr, file, body, true)
			foreignRefused(t, w, f, file, onDisk, branch, http.StatusConflict)
			if want := "tenants." + orgEnfTenantOut + "._silent_mode"; !strings.Contains(w.Body.String(), want) ||
				strings.Contains(w.Body.String(), msgSpliceDetailWithheld) {
				t.Errorf("refusal does not name %s: %s", want, w.Body.String())
			}
		})
	}
}

// #3062 review: a caller who may read every tenant of the file may add, drop
// or rename anchors inside its own sections — a whole section is its to
// write. The set of anchors the file defines is not compared (verifySplice):
// every effect an anchor change has outside the declared sections is an
// alias there resolving differently, which the node comparison refuses
// (TestPutTenant_SpliceKeepsAliasesIntoDeclaredSections). A caller who may
// not read Out is refused all three: the sections swapped hold an anchor.
func TestPutTenant_SpliceOwnAnchorsAreTheBodys(t *testing.T) {
	t.Parallel()
	file := "tenants:\n  " + orgEnfTenantIn + ":\n    _silent_mode: &q \"critical\"\n" + foreignOutSection
	for _, mode := range foreignModes {
		for _, tc := range []struct{ name, section string }{
			{"dropped", "    _silent_mode: \"disable\"\n"},
			{"renamed", "    _silent_mode: &r \"disable\"\n"},
			{"added", "    _silent_mode: &q \"disable\"\n    _metadata:\n      owner: &extra team\n"},
		} {
			t.Run(mode.name+"/"+tc.name, func(t *testing.T) {
				t.Parallel()
				body := "tenants:\n  " + orgEnfTenantIn + ":\n" + tc.section
				wh, fh, diskH, brH := foreignPut(t, mode.pr, file, body, false)
				foreignRefused(t, wh, fh, file, diskH, brH, http.StatusConflict, orgEnfTenantOut)
				w, _, onDisk, branch := foreignPut(t, mode.pr, file, body, true)
				if w.Code != http.StatusOK {
					t.Fatalf("status = %d, want 200; body=%s", w.Code, w.Body.String())
				}
				if got, want := foreignWritten(t, mode.pr, file, onDisk, branch), body+foreignOutSection; got != want {
					t.Errorf("written file:\n%s\nwant:\n%s", got, want)
				}
			})
		}
	}
}

// Out's KEY is an alias of an anchor in In's section: In's body changing
// that anchor's value would rename Out → 409, nothing written.
func TestPutTenant_SpliceKeepsAliasedTenantKey(t *testing.T) {
	t.Parallel()
	const file = "tenants:\n  " + orgEnfTenantIn + ":\n    _silent_mode: \"critical\"\n    _metadata:\n      owner: &o " + orgEnfTenantOut + "\n" +
		"  *o :\n    _silent_mode: \"warning\"\n"
	body := "tenants:\n  " + orgEnfTenantIn + ":\n    _silent_mode: \"critical\"\n    _metadata:\n      owner: &o renamed\n"
	for _, mode := range foreignModes {
		t.Run(mode.name, func(t *testing.T) {
			t.Parallel()
			w, f, onDisk, branch := foreignPut(t, mode.pr, file, body, false)
			foreignRefused(t, w, f, file, onDisk, branch, http.StatusConflict, orgEnfTenantOut, spliceOutValue)
			if !strings.Contains(w.Body.String(), configErrorNotRoundTrippable) {
				t.Errorf("refusal is not %s: %s", configErrorNotRoundTrippable, w.Body.String())
			}
		})
	}
}

// Flow-style tenants, and a file of two documents: what the body does not
// declare is kept.
func TestPutTenant_SpliceKeepsFlowStyleAndLaterDocuments(t *testing.T) {
	t.Parallel()
	for _, mode := range foreignModes {
		for _, tc := range []struct{ name, file, want string }{
			{"flow_tenants",
				"tenants: {" + orgEnfTenantIn + ": {_silent_mode: \"critical\"}, " + orgEnfTenantOut + ": {_silent_mode: " + spliceOutValue + "}}\n",
				"tenants: {" + orgEnfTenantIn + ": {_silent_mode: \"disable\"}, " + orgEnfTenantOut + ": {_silent_mode: " + spliceOutValue + "}}\n"},
			{"second_document",
				foreignSharedFile + "---\nnote: \"keep me\"\n",
				foreignOmitOutResult + "---\nnote: \"keep me\"\n"},
		} {
			t.Run(mode.name+"/"+tc.name, func(t *testing.T) {
				t.Parallel()
				w, _, onDisk, branch := foreignPut(t, mode.pr, tc.file, foreignBodyOmitOut, false)
				if w.Code != http.StatusOK {
					t.Fatalf("status = %d, want 200; body=%s", w.Code, w.Body.String())
				}
				if got := foreignWritten(t, mode.pr, tc.file, onDisk, branch); got != tc.want {
					t.Errorf("written file:\n%s\nwant:\n%s", got, tc.want)
				}
			})
		}
	}
}

// Declaring Out exactly as the file holds it is declaring it: 403 for a
// caller who may not write Out, no "unchanged, so allowed" shortcut.
func TestPutTenant_DeclaringUnchangedForeignSectionDenied(t *testing.T) {
	t.Parallel()
	body := "tenants:\n" + foreignInChanged + foreignOutSection
	for _, mode := range foreignModes {
		t.Run(mode.name, func(t *testing.T) {
			t.Parallel()
			w, f, onDisk, branch := foreignPut(t, mode.pr, foreignSharedFile, body, false)
			foreignRefused(t, w, f, foreignSharedFile, onDisk, branch, http.StatusForbidden, orgEnfTenantOut)
			if !strings.Contains(w.Body.String(), "declares a tenant section you may not write") {
				t.Errorf("refusal text: %s", w.Body.String())
			}
		})
	}
}

// A caller who may write Out declares it as `!!str` / `!!binary`: the
// section it replaces is Out's (decoded id), not a second Out.
func TestPutTenant_TaggedDeclaredKeyReplacesTheSection(t *testing.T) {
	t.Parallel()
	b64 := base64.StdEncoding.EncodeToString([]byte(orgEnfTenantOut))
	for _, mode := range foreignModes {
		for name, key := range map[string]string{"str": "!!str " + orgEnfTenantOut, "binary": "!!binary " + b64} {
			t.Run(mode.name+"/"+name, func(t *testing.T) {
				t.Parallel()
				body := "tenants:\n" + foreignInChanged + "  " + key + ":\n    _silent_mode: \"critical\"\n"
				w, _, onDisk, branch := foreignPut(t, mode.pr, foreignSharedFile, body, true)
				if w.Code != http.StatusOK {
					t.Fatalf("status = %d, want 200; body=%s", w.Code, w.Body.String())
				}
				got := foreignWritten(t, mode.pr, foreignSharedFile, onDisk, branch)
				c, err := cfg.ParseTenantFile([]byte(got))
				if err != nil {
					t.Fatalf("written file does not parse: %v\n%s", err, got)
				}
				if len(c.Tenants) != 2 || c.Tenants[orgEnfTenantOut]["_silent_mode"].Default != "critical" {
					t.Errorf("written file:\n%s", got)
				}
			})
		}
	}
}

// #2405 for the shapes the splice cannot read though the file parses: a
// merge key at the root or in `tenants:`, `tenants` an alias. Like a file
// that does not parse, a caller with write permission on all tenants
// replaces it whole with the body; anyone else gets #2405's 400, the file
// untouched. Both write modes.
func TestPutTenant_UnspliceableBaseFollowsCaller(t *testing.T) {
	t.Parallel()
	const id = "svc-alpha"
	const own = "  " + id + ":\n    mysql_connections: \"70\"\n"
	const body = "tenants:\n  " + id + ":\n    mysql_connections: \"75\"\n"
	shapes := map[string]string{
		"tenants_merge_key": "tenants:\n" + own + "  <<: {svc-beta: {mysql_connections: \"60\"}}\n",
		"root_merge_key":    "<<: {x-notes: {keep: me}}\ntenants:\n" + own,
		"tenants_alias":     "x-base: &t\n" + own + "tenants: *t\n",
	}
	for name, file := range shapes {
		for _, pr := range []bool{false, true} {
			for _, caller := range []struct {
				group     string
				replaceOK bool
			}{{"ops", false}, {"platform", true}} {
				mode := map[bool]string{false: "direct", true: "pr"}[pr]
				t.Run(name+"/"+mode+"/"+caller.group, func(t *testing.T) {
					t.Parallel()
					dir := setupConfigDir(t, map[string]string{id + ".yaml": file, "_defaults.yaml": caDefaults})
					initGitRepo(t, dir)
					runGit(t, dir, "branch", "-M", "main")
					deps := &Deps{ConfigDir: dir, State: mustDirState(dir), Writer: newTestWriter(dir),
						RBAC: newRBACManager(t, repairRBACYAML), WriteMode: WriteModeDirect}
					if pr {
						deps.WriteMode, deps.PRClient, deps.PRTracker = WriteModePR, &mockPlatformClient{}, &mockPlatformTracker{}
					}
					req := newRequestWithChiParam("PUT", "/api/v1/tenants/"+id, "id", id, bytes.NewBufferString(body))
					w := servePopulatingRBAC(t, PutTenant(deps), req, "alice@example.com", []string{caller.group})
					after := mustRead(t, filepath.Join(dir, id+".yaml"))
					var branchFile string
					if pr {
						out, _ := exec.Command("git", "-C", dir, "for-each-ref", "--format=%(refname:short)", "refs/heads").Output()
						for _, br := range strings.Fields(string(out)) {
							if br != "main" {
								c, _ := exec.Command("git", "-C", dir, "show", br+":"+id+".yaml").Output()
								branchFile += string(c)
							}
						}
					}
					if !caller.replaceOK {
						if w.Code != http.StatusBadRequest || !strings.Contains(w.Body.String(), "write permission on all tenants") {
							t.Fatalf("status = %d, want #2405's 400; body=%s", w.Code, w.Body.String())
						}
						if after != file || branchFile != "" {
							t.Errorf("refused PUT wrote: file:\n%s\nbranch:\n%s", after, branchFile)
						}
						return
					}
					if w.Code != http.StatusOK {
						t.Fatalf("status = %d, want 200; body=%s", w.Code, w.Body.String())
					}
					got := after
					if pr {
						got = branchFile
					}
					if got != body {
						t.Errorf("written file:\n%s\nwant the body whole:\n%s", got, body)
					}
				})
			}
		}
	}
}

// A declared section holding `.nan` decodes to NaN on both sides of the
// splice's check; NaN != NaN must not make that a refusal.
func TestSpliceDeclaredSections_NaNIsEqual(t *testing.T) {
	t.Parallel()
	body := "tenants:\n  " + orgEnfTenantIn + ":\n    x: .nan\n    y: [.nan, {z: .NaN}]\n"
	got, err := spliceDeclaredSections([]byte(foreignSharedFile), body, orgEnfTenantIn, nil, nil)
	if err != nil {
		t.Fatalf("splice refused: %v", err)
	}
	if want := body + foreignOutSection; got != want {
		t.Errorf("spliced:\n%s\nwant:\n%s", got, want)
	}
}

// /validate and /diff answer what the PUT they preview would do (#3062).
func foreignPreview(t *testing.T, pr bool, route string, file, body string, outShared bool) *httptest.ResponseRecorder {
	t.Helper()
	f := newOrgEnfFixture(t, map[string]string{orgEnfTenantIn + ".yaml": file})
	if outShared {
		f.shareOut(t)
	}
	d := f.deps()
	if pr {
		d.WriteMode, d.PRClient, d.PRTracker = WriteModePR, &mockPlatformClient{}, &mockPlatformTracker{}
	}
	h := ValidateTenant(d)
	if route == "diff" {
		h = DiffTenant(d)
	}
	req := newRequestWithChiParam("POST", "/api/v1/tenants/"+orgEnfTenantIn+"/"+route, "id", orgEnfTenantIn, bytes.NewBufferString(body))
	req.Header.Set("Content-Type", "application/yaml")
	w := httptest.NewRecorder()
	wrapWithRBACMiddleware(h, f.rbacMgr, rbac.PermRead, nil).ServeHTTP(w, orgEnfIdentity(req, orgEnfMemberOrg))
	if got := foreignFile(t, f); got != file || f.writes.Load() != 0 {
		t.Errorf("a preview wrote: commits=%d file:\n%s", f.writes.Load(), got)
	}
	return w
}

// /validate judges the tenants the body declares as PUT does: declaring Out
// without write permission on it is valid: false with PUT's 403 text, in
// both write modes; with it, or leaving Out out, the body is valid.
func TestValidateTenant_DeclaredTenantsJudgedAsPUT(t *testing.T) {
	t.Parallel()
	for _, mode := range foreignModes {
		for _, tc := range []struct {
			name, body string
			outShared  bool
			valid      bool
		}{
			{"declares_unwritable_out", foreignBodyRewriteOut, false, false},
			{"declares_writable_out", foreignBodyRewriteOut, true, true},
			{"omits_out", foreignBodyOmitOut, false, true},
			{"merge_key_in_body", "tenants:\n" + foreignInChanged + "  <<: {" + orgEnfTenantOut + ": {_silent_mode: \"disable\"}}\n", true, false},
		} {
			t.Run(mode.name+"/"+tc.name, func(t *testing.T) {
				t.Parallel()
				w := foreignPreview(t, mode.pr, "validate", foreignSharedFile, tc.body, tc.outShared)
				var got ValidateResponse
				if err := json.Unmarshal(w.Body.Bytes(), &got); err != nil || w.Code != http.StatusOK {
					t.Fatalf("status = %d, err = %v; body: %s", w.Code, err, w.Body.String())
				}
				if got.Valid != tc.valid {
					t.Fatalf("valid = %v, want %v; warnings %q", got.Valid, tc.valid, got.Warnings)
				}
				switch tc.name {
				case "declares_unwritable_out":
					if len(got.Warnings) != 1 || got.Warnings[0] != msgDeclaresUnwritableTenant {
						t.Errorf("warnings = %q, want PUT's 403 text", got.Warnings)
					}
				case "merge_key_in_body":
					if len(got.Warnings) != 1 || got.Warnings[0] != msgBodyTenantsMergeKey {
						t.Errorf("warnings = %q, want PUT's 400 text", got.Warnings)
					}
				}
			})
		}
	}
}

// /diff previews what PUT writes: the declared sections spliced into the
// file, so Out — left out of the body — shows as kept, not deleted; and a
// splice PUT refuses is PUT's 409.
func TestDiffTenant_PreviewsTheSplice(t *testing.T) {
	t.Parallel()
	const aliasFile = "tenants:\n" +
		"  " + orgEnfTenantIn + ":\n    _silent_mode: &m \"warning\"\n" +
		"  " + orgEnfTenantOut + ":\n    _silent_mode: *m\n"
	t.Run("omitted_out_kept", func(t *testing.T) {
		t.Parallel()
		w := foreignPreview(t, false, "diff", foreignSharedFile, foreignBodyOmitOut, true)
		var got DiffResponse
		if err := json.Unmarshal(w.Body.Bytes(), &got); err != nil || w.Code != http.StatusOK {
			t.Fatalf("status = %d, err = %v; body: %s", w.Code, err, w.Body.String())
		}
		if !got.HasDiff || !strings.Contains(got.Diff, "+"+strings.TrimSuffix(foreignInChanged[strings.Index(foreignInChanged, "\n")+1:], "\n")) {
			t.Errorf("diff does not show In's change:\n%s", got.Diff)
		}
		for _, line := range strings.Split(got.Diff, "\n") {
			if strings.HasPrefix(line, "-") && !strings.HasPrefix(line, "---") && strings.Contains(line, "warning") {
				t.Errorf("diff deletes Out's section:\n%s", got.Diff)
			}
		}
	})
	t.Run("refused_splice_is_puts_409", func(t *testing.T) {
		t.Parallel()
		w := foreignPreview(t, false, "diff", aliasFile, "tenants:\n  "+orgEnfTenantIn+":\n    _silent_mode: &m \"disable\"\n", true)
		if w.Code != http.StatusConflict || !strings.Contains(w.Body.String(), configErrorNotRoundTrippable) {
			t.Fatalf("status = %d, want PUT's 409 %s; body: %s", w.Code, configErrorNotRoundTrippable, w.Body.String())
		}
	})
}
