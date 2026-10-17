package handler

// #3062, second review round.
//
// Anchors and aliases (F4). For a caller the file is withheld from (#3050's
// fileShown: it may not read every tenant the file declares), comparing a
// rebound alias with what it resolved to is an oracle on the hidden value:
// a body that redefines a hidden section's anchor name inside its own section
// rebinds a later alias, and "200 only when the guess equals the hidden
// value" tells the caller whether it guessed right. So for such a caller a
// declared section — the body's, or the one in the file it replaces — holding
// any anchor or alias is refused outright, the same 409 whatever the values.
// A caller who may read every tenant keeps the value comparison.
//
// Also here: fileShown is asked only when needed (F5: a successful write
// counts no read would-deny), a declared tenant that another conf.d file also
// declares (F6), /diff over a file the splice cannot read for a caller
// without #2405's bit, and a declared section whose `_metadata` does not
// decode.

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/rbac"
)

// anchorHiddenFirst is a shared file: Out (hidden from the member caller)
// defines &hooks with outValue, In follows, and a root key after both
// aliases it.
func anchorHiddenFirst(outValue string) string {
	return "tenants:\n" +
		"  " + orgEnfTenantOut + ":\n    _silent_mode: &hooks " + outValue + "\n" +
		"  " + orgEnfTenantIn + ":\n    _silent_mode: \"critical\"\n" +
		"x-notes:\n  q: *hooks\n"
}

// anchorBothDefine: Out and In both define &m; the alias after them binds to
// In's. A body that drops In's anchor rebinds it to Out's.
func anchorBothDefine(outValue string) string {
	return "tenants:\n" +
		"  " + orgEnfTenantOut + ":\n    _silent_mode: &m " + outValue + "\n" +
		"  " + orgEnfTenantIn + ":\n    _silent_mode: &m \"critical\"\n" +
		"x-notes:\n  q: *m\n"
}

func inSection(section string) string { return "tenants:\n  " + orgEnfTenantIn + ":\n" + section }

// sameRefusal asserts that two answers are one and the same withheld 409:
// status and body identical (request_id aside, which differs per request
// when set).
func sameRefusal(t *testing.T, a, b *httptest.ResponseRecorder) {
	t.Helper()
	norm := func(w *httptest.ResponseRecorder) string {
		var m map[string]any
		if err := json.Unmarshal(w.Body.Bytes(), &m); err != nil {
			t.Fatalf("body is not JSON: %v: %s", err, w.Body.String())
		}
		delete(m, "request_id")
		out, _ := json.Marshal(m)
		return string(out)
	}
	if a.Code != http.StatusConflict || b.Code != http.StatusConflict {
		t.Fatalf("status = %d / %d, want 409 for both; bodies:\n%s\n%s", a.Code, b.Code, a.Body.String(), b.Body.String())
	}
	if na, nb := norm(a), norm(b); na != nb {
		t.Fatalf("the two refusals differ:\n%s\n%s", na, nb)
	}
	if !strings.Contains(a.Body.String(), msgSpliceDetailWithheld) || !strings.Contains(a.Body.String(), configErrorNotRoundTrippable) {
		t.Errorf("not the withheld %s refusal: %s", configErrorNotRoundTrippable, a.Body.String())
	}
}

// (1)(2)(3): each pair differs only in what the caller may not read (or in
// its guess of it); both answers must be the same refusal, nothing written.
func TestPutTenant_AnchorRuleIsNotAValueOracle(t *testing.T) {
	t.Parallel()
	type probe struct{ file, body string }
	for _, mode := range foreignModes {
		for _, tc := range []struct {
			name string
			a, b probe
		}{
			// (1) the body redefines Out's anchor name in In: the guess equals
			// the hidden value, or not.
			{"guess_value",
				probe{anchorHiddenFirst(spliceOutValue), inSection("    _silent_mode: &hooks " + spliceOutValue + "\n")},
				probe{anchorHiddenFirst(spliceOutValue), inSection("    _silent_mode: &hooks \"disable\"\n")}},
			// (2) the anchor's name alone: the hidden one, or another.
			{"guess_anchor_name",
				probe{anchorHiddenFirst(spliceOutValue), inSection("    _silent_mode: &hooks \"disable\"\n")},
				probe{anchorHiddenFirst(spliceOutValue), inSection("    _silent_mode: &othername \"disable\"\n")}},
			// (3) In's section in the file defines the anchor the alias binds
			// to; the body drops it, rebinding the alias to Out's. Same body,
			// Out's hidden value equal to In's or not.
			{"dropped_anchor_rebinds",
				probe{anchorBothDefine(`"critical"`), inSection("    _silent_mode: \"critical\"\n")},
				probe{anchorBothDefine(spliceOutValue), inSection("    _silent_mode: \"critical\"\n")}},
		} {
			t.Run(mode.name+"/"+tc.name, func(t *testing.T) {
				t.Parallel()
				wa, fa, diskA, brA := foreignPut(t, mode.pr, tc.a.file, tc.a.body, false)
				wb, fb, diskB, brB := foreignPut(t, mode.pr, tc.b.file, tc.b.body, false)
				sameRefusal(t, wa, wb)
				foreignRefused(t, wa, fa, tc.a.file, diskA, brA, http.StatusConflict, orgEnfTenantOut, "hooks", "anchor", "alias")
				foreignRefused(t, wb, fb, tc.b.file, diskB, brB, http.StatusConflict, orgEnfTenantOut, "hooks", "anchor", "alias")
			})
		}
	}
}

// (4) a caller who may read every tenant of the file keeps the value
// comparison: the same body that (1) refuses is written when it keeps the
// alias's value, refused with a detail when it does not.
func TestPutTenant_AnchorRuleFullReaderComparesValues(t *testing.T) {
	t.Parallel()
	file := anchorHiddenFirst(spliceOutValue)
	for _, mode := range foreignModes {
		t.Run(mode.name+"/same_value", func(t *testing.T) {
			t.Parallel()
			body := inSection("    _silent_mode: &hooks " + spliceOutValue + "\n")
			w, _, onDisk, branch := foreignPut(t, mode.pr, file, body, true)
			if w.Code != http.StatusOK {
				t.Fatalf("status = %d, want 200; body=%s", w.Code, w.Body.String())
			}
			want := "tenants:\n" +
				"  " + orgEnfTenantOut + ":\n    _silent_mode: &hooks " + spliceOutValue + "\n" +
				"  " + orgEnfTenantIn + ":\n    _silent_mode: &hooks " + spliceOutValue + "\n" +
				"x-notes:\n  q: *hooks\n"
			if got := foreignWritten(t, mode.pr, file, onDisk, branch); got != want {
				t.Errorf("written file:\n%s\nwant:\n%s", got, want)
			}
		})
		t.Run(mode.name+"/other_value", func(t *testing.T) {
			t.Parallel()
			w, f, onDisk, branch := foreignPut(t, mode.pr, file, inSection("    _silent_mode: &hooks \"disable\"\n"), true)
			foreignRefused(t, w, f, file, onDisk, branch, http.StatusConflict)
			if strings.Contains(w.Body.String(), msgSpliceDetailWithheld) || !strings.Contains(w.Body.String(), "first difference: ") {
				t.Errorf("a full reader's refusal does not say where: %s", w.Body.String())
			}
		})
	}
}

// (5) anchors and aliases in sections the body does not declare do not stop
// a withheld caller: what it swaps holds none.
func TestPutTenant_AnchorRuleIgnoresUndeclaredAnchors(t *testing.T) {
	t.Parallel()
	file := anchorHiddenFirst(spliceOutValue)
	body := inSection("    _silent_mode: \"disable\"\n")
	want := "tenants:\n" +
		"  " + orgEnfTenantOut + ":\n    _silent_mode: &hooks " + spliceOutValue + "\n" +
		"  " + orgEnfTenantIn + ":\n    _silent_mode: \"disable\"\n" +
		"x-notes:\n  q: *hooks\n"
	for _, mode := range foreignModes {
		t.Run(mode.name, func(t *testing.T) {
			t.Parallel()
			w, _, onDisk, branch := foreignPut(t, mode.pr, file, body, false)
			if w.Code != http.StatusOK {
				t.Fatalf("status = %d, want 200; body=%s", w.Code, w.Body.String())
			}
			if got := foreignWritten(t, mode.pr, file, onDisk, branch); got != want {
				t.Errorf("written file:\n%s\nwant:\n%s", got, want)
			}
		})
	}
}

// F5: a successful PUT on a shared file asks no read permission it does not
// need: in shadow mode, an unlabeled tenant sharing the file is not counted
// as a read would-deny (tenant_api_scope_would_deny_total{axis="org"}).
func TestPutTenant_SuccessfulSpliceCountsNoReadWouldDeny(t *testing.T) {
	t.Parallel()
	const unlabeled = "tenant-org-unlabeled"
	file := "tenants:\n" + foreignInSection + "  " + unlabeled + ":\n    _silent_mode: \"warning\"\n"
	for _, mode := range foreignModes {
		t.Run(mode.name, func(t *testing.T) {
			t.Parallel()
			f := newOrgEnfFixture(t, map[string]string{orgEnfTenantIn + ".yaml": file})
			// Shadow: the org axis allows the unlabeled tenant and counts it.
			f.rbacMgr = newRBACManagerWithClaims(t, orgEnfRBACYAML, map[string]string{"org": orgEnfClaimHeader})
			rec := &ScopeWouldDenyMetrics{}
			f.rbacMgr.SetScopeAuditor(rec)
			d := f.deps()
			if mode.pr {
				d.WriteMode, d.PRClient, d.PRTracker = WriteModePR, &mockPlatformClient{}, &mockPlatformTracker{}
			}
			req := newRequestWithChiParam("PUT", "/api/v1/tenants/"+orgEnfTenantIn, "id", orgEnfTenantIn, bytes.NewBufferString(foreignBodyOmitOut))
			w := httptest.NewRecorder()
			wrapWithRBACMiddleware(PutTenant(d), f.rbacMgr, rbac.PermWrite, TenantIDFromPath).
				ServeHTTP(w, orgEnfIdentity(req, orgEnfMemberOrg))
			if w.Code != http.StatusOK {
				t.Fatalf("status = %d, want 200; body=%s", w.Code, w.Body.String())
			}
			if snap := rec.Snapshot(); snap["org"] != 0 {
				t.Errorf("would-deny{axis=org} = %d, want 0: a successful write read no tenant", snap["org"])
			}
		})
	}
}

// F6: a tenant the body declares besides {id} that another conf.d file also
// declares (#2078's duplicate) is refused: 409 TENANT_DECLARED_ELSEWHERE with
// the fixed text, naming neither the tenant nor the file, nothing written.
// Leaving it out of the body writes as before.
func TestPutTenant_DeclaredTenantDeclaredElsewhere(t *testing.T) {
	t.Parallel()
	files := map[string]string{
		orgEnfTenantIn + ".yaml":  foreignSharedFile,
		orgEnfTenantOut + ".yaml": "tenants:\n" + foreignOutSection,
	}
	for _, mode := range foreignModes {
		t.Run(mode.name+"/declared_refused", func(t *testing.T) {
			t.Parallel()
			f := newOrgEnfFixture(t, files)
			f.shareOut(t)
			w, onDisk, branch := foreignPutOn(t, f, mode.pr, foreignBodyRewriteBoth)
			foreignRefused(t, w, f, foreignSharedFile, onDisk, branch, http.StatusConflict,
				orgEnfTenantOut, orgEnfTenantOut+".yaml")
			if !strings.Contains(w.Body.String(), CodeTenantDeclaredElsewhere) {
				t.Errorf("not %s: %s", CodeTenantDeclaredElsewhere, w.Body.String())
			}
		})
		t.Run(mode.name+"/omitted_written", func(t *testing.T) {
			t.Parallel()
			f := newOrgEnfFixture(t, files)
			f.shareOut(t)
			w, onDisk, branch := foreignPutOn(t, f, mode.pr, foreignBodyOmitOut)
			if w.Code != http.StatusOK {
				t.Fatalf("status = %d, want 200; body=%s", w.Code, w.Body.String())
			}
			if got := foreignWritten(t, mode.pr, foreignSharedFile, onDisk, branch); got != foreignOmitOutResult {
				t.Errorf("written file:\n%s\nwant:\n%s", got, foreignOmitOutResult)
			}
		})
	}
}

// foreignPutOn is foreignPut on a fixture the caller built.
func foreignPutOn(t *testing.T, f *orgEnfFixture, pr bool, body string) (w *httptest.ResponseRecorder, onDisk, branch string) {
	t.Helper()
	d := f.deps()
	if pr {
		d.WriteMode, d.PRClient, d.PRTracker = WriteModePR, &mockPlatformClient{}, &mockPlatformTracker{}
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
	return w, onDisk, branch
}

// /diff over a file the splice cannot read: a caller who may read every
// tenant but not write every tenant gets the 400 PUT gives it (#2405), not a
// preview of the whole-file replacement PUT refuses it; a caller with write
// permission on all tenants gets that preview.
func TestDiffTenant_UnspliceableBaseAnswersAsPUT(t *testing.T) {
	t.Parallel()
	const id = "svc-alpha"
	const file = "tenants:\n  " + id + ":\n    mysql_connections: \"70\"\n  <<: {svc-beta: {mysql_connections: \"60\"}}\n"
	const body = "tenants:\n  " + id + ":\n    mysql_connections: \"75\"\n"
	serve := func(t *testing.T, h func(*Deps) http.HandlerFunc, method, path string, groups []string) *httptest.ResponseRecorder {
		dir := setupConfigDir(t, map[string]string{id + ".yaml": file, "_defaults.yaml": caDefaults})
		initGitRepo(t, dir)
		runGit(t, dir, "branch", "-M", "main")
		deps := &Deps{ConfigDir: dir, State: mustDirState(dir), Writer: newTestWriter(dir),
			RBAC: newRBACManager(t, repairRBACYAML), WriteMode: WriteModeDirect}
		req := newRequestWithChiParam(method, path, "id", id, bytes.NewBufferString(body))
		req.Header.Set("Content-Type", "application/yaml")
		return servePopulatingRBAC(t, h(deps), req, "alice@example.com", groups)
	}
	readAll := []string{"auditors", "ops"}
	put := serve(t, PutTenant, "PUT", "/api/v1/tenants/"+id, readAll)
	diff := serve(t, DiffTenant, "POST", "/api/v1/tenants/"+id+"/diff", readAll)
	if put.Code != http.StatusBadRequest || diff.Code != put.Code {
		t.Fatalf("PUT %d, /diff %d; want both PUT's 400; PUT: %s /diff: %s", put.Code, diff.Code, put.Body.String(), diff.Body.String())
	}
	var pe, de ErrorResponse
	if json.Unmarshal(put.Body.Bytes(), &pe) != nil || json.Unmarshal(diff.Body.Bytes(), &de) != nil ||
		pe.Error != de.Error || pe.Code != de.Code || !strings.Contains(de.Error, "write permission on all tenants") {
		t.Errorf("/diff's 400 is not PUT's:\nPUT:   %s\n/diff: %s", put.Body.String(), diff.Body.String())
	}
	platform := serve(t, DiffTenant, "POST", "/api/v1/tenants/"+id+"/diff", []string{"platform"})
	var got DiffResponse
	if err := json.Unmarshal(platform.Body.Bytes(), &got); err != nil || platform.Code != http.StatusOK ||
		!strings.Contains(got.Diff, "-  <<:") {
		t.Errorf("platform caller: status %d, want the whole-file preview: %s", platform.Code, platform.Body.String())
	}
}

// A declared tenant whose `_metadata` in the shared file does not decode has
// labels nobody can read: refused, the same way on the pod's tree and on the
// fresh base (fail closed, shadow mode included), unless the caller may write
// every tenant with no scope.
func TestPutTenant_DeclaredUndecodableMetadataRefused(t *testing.T) {
	t.Parallel()
	const undecodable = "tenants:\n  db-a:\n    _metadata:\n      environment: production\n" + declFreshDB +
		"  db-b:\n    _metadata:\n      environment: [production]\n" + declFreshDB
	for _, tc := range []struct {
		name          string
		local, origin string
		scoped        bool
		direct        bool
		wantOK        bool
	}{
		{"local_and_base", undecodable, undecodable, true, false, false},
		{"local_only", undecodable, declFreshProd, true, false, false},
		{"base_only", declFreshProd, undecodable, true, false, false},
		{"direct", undecodable, undecodable, true, true, false},
		{"unrestricted_caller", undecodable, undecodable, false, false, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			var origin map[string]string // nil: the base is the local tree
			if tc.origin != tc.local {
				origin = map[string]string{"db-a.yaml": tc.origin}
			}
			f := newStaleFixture(t, map[string]string{"_defaults.yaml": freshScopeDefaults, "db-a.yaml": tc.local}, origin)
			if tc.scoped {
				f.deps.RBAC = newRBACManager(t, postStateRBAC) // shadow
			}
			if tc.direct {
				f.deps.WriteMode = WriteModeDirect
			}
			w := putStale(t, f, "db-a", declFreshBody)
			if tc.wantOK {
				if w.Code != http.StatusOK || !f.prOpened {
					t.Fatalf("status %d, PR opened %v; want the PR: %s", w.Code, f.prOpened, w.Body.String())
				}
				return
			}
			if w.Code != http.StatusForbidden || !strings.Contains(w.Body.String(), "declares a tenant section you may not write") {
				t.Fatalf("status %d, body %s; want the declared-tenant 403", w.Code, w.Body.String())
			}
			if f.prOpened {
				t.Error("a PR was opened")
			}
			if b := f.tenantBranches(t); b != "" {
				t.Errorf("branch left behind: %s", b)
			}
			if got := readFileT(t, f.dir, "db-a.yaml"); got != tc.local {
				t.Errorf("the file changed on a refused PUT:\n%s", got)
			}
		})
	}
}

// --- third review round ------------------------------------------------

// F1: a node that contains the declared sections — the `tenants:` value, or
// the root mapping — carrying an anchor. An alias to it resolves to a node
// the splice changes whatever the body says, so whether a hidden part of the
// file aliases it must not decide the answer: for a withheld caller the
// anchor alone is the refusal, the same 409 with or without the alias.
func TestPutTenant_AncestorAnchorIsNotAStructuralOracle(t *testing.T) {
	t.Parallel()
	sections := foreignInSection + foreignOutSection
	for _, mode := range foreignModes {
		for _, tc := range []struct{ name, aliased, plain string }{
			{"tenants_anchor",
				"tenants: &t\n" + sections + "x-notes:\n  q: *t\n",
				"tenants: &t\n" + sections},
			{"root_anchor",
				"&r\ntenants:\n" + sections + "x-notes:\n  q: *r\n",
				"&r\ntenants:\n" + sections},
		} {
			t.Run(mode.name+"/"+tc.name, func(t *testing.T) {
				t.Parallel()
				wa, fa, diskA, brA := foreignPut(t, mode.pr, tc.aliased, foreignBodyOmitOut, false)
				wb, fb, diskB, brB := foreignPut(t, mode.pr, tc.plain, foreignBodyOmitOut, false)
				sameRefusal(t, wa, wb)
				foreignRefused(t, wa, fa, tc.aliased, diskA, brA, http.StatusConflict, orgEnfTenantOut, "anchor", "alias")
				foreignRefused(t, wb, fb, tc.plain, diskB, brB, http.StatusConflict, orgEnfTenantOut, "anchor", "alias")
			})
		}
	}
}

// F2: an alias in a base section the body replaces is taken away with it;
// no other node resolves through an alias, so removing it rebinds nothing.
// The withheld caller's write is not refused for it: In's `<<: *d` (d at the
// root) is replaced, Out and x-defaults stay as they are, Out's `*d` still
// binds to x-defaults.
func TestPutTenant_ReplacedSectionAliasIsNotRefused(t *testing.T) {
	t.Parallel()
	file := "x-defaults: &d\n  _silent_mode: \"warning\"\n" +
		"tenants:\n" +
		"  " + orgEnfTenantIn + ":\n    <<: *d\n" +
		"  " + orgEnfTenantOut + ":\n    <<: *d\n"
	want := "x-defaults: &d\n  _silent_mode: \"warning\"\n" +
		"tenants:\n" +
		foreignInChanged +
		"  " + orgEnfTenantOut + ":\n    <<: *d\n"
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

// F2's counter-case: a hidden section's alias that points INTO the replaced
// section is bound by an anchor the replaced section defines, so the write
// is still refused — on that anchor, the same 409 whether or not the hidden
// section aliases it.
func TestPutTenant_ReplacedSectionAnchorStillRefused(t *testing.T) {
	t.Parallel()
	in := "  " + orgEnfTenantIn + ":\n    _silent_mode: &x \"critical\"\n"
	aliased := "tenants:\n" + in + "  " + orgEnfTenantOut + ":\n    _silent_mode: *x\n"
	plain := "tenants:\n" + in + foreignOutSection
	for _, mode := range foreignModes {
		t.Run(mode.name, func(t *testing.T) {
			t.Parallel()
			wa, fa, diskA, brA := foreignPut(t, mode.pr, aliased, foreignBodyOmitOut, false)
			wb, fb, diskB, brB := foreignPut(t, mode.pr, plain, foreignBodyOmitOut, false)
			sameRefusal(t, wa, wb)
			foreignRefused(t, wa, fa, aliased, diskA, brA, http.StatusConflict, orgEnfTenantOut, "anchor", "alias")
			foreignRefused(t, wb, fb, plain, diskB, brB, http.StatusConflict, orgEnfTenantOut, "anchor", "alias")
		})
	}
}

// F7: an anchor on a section's KEY counts as one in the section, on both
// sides. A hidden `&k "zz"` precedes In; an anchor &k on In's key — in the
// body, or in the file — rebinds a later `*k` (to In's key, or back to the
// hidden value). With or without that `*k`, the same withheld 409.
func TestPutTenant_KeyAnchorIsNotAStructuralOracle(t *testing.T) {
	t.Parallel()
	hidden := "  " + orgEnfTenantOut + ":\n    _silent_mode: &k \"zz\"\n"
	const alias = "x-notes:\n  q: *k\n"
	bodyKeyAnchored := "tenants:\n  &k " + orgEnfTenantIn + ":\n    _silent_mode: \"disable\"\n"
	baseKeyAnchored := "tenants:\n" + hidden + "  &k " + orgEnfTenantIn + ":\n    _silent_mode: \"critical\"\n"
	basePlain := "tenants:\n" + hidden + foreignInSection
	for _, mode := range foreignModes {
		for _, tc := range []struct{ name, aliased, plain, body string }{
			{"body_key", basePlain + alias, basePlain, bodyKeyAnchored},
			{"base_key", baseKeyAnchored + alias, baseKeyAnchored, foreignBodyOmitOut},
		} {
			t.Run(mode.name+"/"+tc.name, func(t *testing.T) {
				t.Parallel()
				wa, fa, diskA, brA := foreignPut(t, mode.pr, tc.aliased, tc.body, false)
				wb, fb, diskB, brB := foreignPut(t, mode.pr, tc.plain, tc.body, false)
				sameRefusal(t, wa, wb)
				foreignRefused(t, wa, fa, tc.aliased, diskA, brA, http.StatusConflict, orgEnfTenantOut, "zz", "anchor", "alias")
				foreignRefused(t, wb, fb, tc.plain, diskB, brB, http.StatusConflict, orgEnfTenantOut, "zz", "anchor", "alias")
			})
		}
	}
}

// F6: /diff answers a proposal declaring a tenant another conf.d file also
// declares as the PUT it previews does — 409 TENANT_DECLARED_ELSEWHERE, the
// same body — not with a preview.
func TestDiffTenant_DeclaredElsewhereAnswersAsPUT(t *testing.T) {
	t.Parallel()
	const id, other = "svc-alpha", "svc-beta"
	const file = "tenants:\n  " + id + ":\n    mysql_connections: \"70\"\n  " + other + ":\n    mysql_connections: \"60\"\n"
	const body = "tenants:\n  " + id + ":\n    mysql_connections: \"75\"\n  " + other + ":\n    mysql_connections: \"65\"\n"
	serve := func(t *testing.T, h func(*Deps) http.HandlerFunc, method, path string) *httptest.ResponseRecorder {
		dir := setupConfigDir(t, map[string]string{id + ".yaml": file,
			other + ".yaml": "tenants:\n  " + other + ":\n    mysql_connections: \"60\"\n", "_defaults.yaml": caDefaults})
		initGitRepo(t, dir)
		runGit(t, dir, "branch", "-M", "main")
		deps := &Deps{ConfigDir: dir, State: mustDirState(dir), Writer: newTestWriter(dir),
			RBAC: newRBACManager(t, repairRBACYAML), WriteMode: WriteModeDirect}
		req := newRequestWithChiParam(method, path, "id", id, bytes.NewBufferString(body))
		req.Header.Set("Content-Type", "application/yaml")
		return servePopulatingRBAC(t, h(deps), req, "alice@example.com", []string{"platform"})
	}
	put := serve(t, PutTenant, "PUT", "/api/v1/tenants/"+id)
	diff := serve(t, DiffTenant, "POST", "/api/v1/tenants/"+id+"/diff")
	if put.Code != http.StatusConflict || diff.Code != put.Code {
		t.Fatalf("PUT %d, /diff %d; want both 409; PUT: %s /diff: %s", put.Code, diff.Code, put.Body.String(), diff.Body.String())
	}
	var pe, de ErrorResponse
	if json.Unmarshal(put.Body.Bytes(), &pe) != nil || json.Unmarshal(diff.Body.Bytes(), &de) != nil ||
		pe.Code != CodeTenantDeclaredElsewhere || de.Code != pe.Code || de.Error != pe.Error {
		t.Errorf("/diff's 409 is not PUT's:\nPUT:   %s\n/diff: %s", put.Body.String(), diff.Body.String())
	}
}

// F5, the other side: a full reader's write of a section holding an anchor
// asks fileShown (the anchor rule needs it), and that ask walks read
// permission over every tenant the file declares — in shadow mode an
// unlabeled one is counted as a read would-deny. EXPECTED and not avoidable:
// "may this caller read every tenant of the file" is the very question that
// lets a full reader keep the value comparison, and it cannot be answered
// without asking each tenant. A write with no anchor or alias in what it
// swaps asks nothing (TestPutTenant_SuccessfulSpliceCountsNoReadWouldDeny).
func TestPutTenant_AnchoredSpliceCountsTheReadItAsks(t *testing.T) {
	t.Parallel()
	const unlabeled = "tenant-org-unlabeled"
	file := "tenants:\n" + foreignInSection + "  " + unlabeled + ":\n    _silent_mode: \"warning\"\n"
	body := inSection("    _silent_mode: &a \"disable\"\n")
	for _, mode := range foreignModes {
		t.Run(mode.name, func(t *testing.T) {
			t.Parallel()
			f := newOrgEnfFixture(t, map[string]string{orgEnfTenantIn + ".yaml": file})
			f.rbacMgr = newRBACManagerWithClaims(t, orgEnfRBACYAML, map[string]string{"org": orgEnfClaimHeader})
			rec := &ScopeWouldDenyMetrics{}
			f.rbacMgr.SetScopeAuditor(rec)
			d := f.deps()
			if mode.pr {
				d.WriteMode, d.PRClient, d.PRTracker = WriteModePR, &mockPlatformClient{}, &mockPlatformTracker{}
			}
			req := newRequestWithChiParam("PUT", "/api/v1/tenants/"+orgEnfTenantIn, "id", orgEnfTenantIn, bytes.NewBufferString(body))
			w := httptest.NewRecorder()
			wrapWithRBACMiddleware(PutTenant(d), f.rbacMgr, rbac.PermWrite, TenantIDFromPath).
				ServeHTTP(w, orgEnfIdentity(req, orgEnfMemberOrg))
			if w.Code != http.StatusOK {
				t.Fatalf("status = %d, want 200 (a full reader in shadow mode); body=%s", w.Code, w.Body.String())
			}
			if snap := rec.Snapshot(); snap["org"] == 0 {
				t.Errorf("would-deny{axis=org} = 0: the anchor rule's fileShown ask over the unlabeled tenant was not counted")
			} else {
				t.Logf("would-deny{axis=org} = %d", snap["org"])
			}
		})
	}
}

// F4 (NIT): with no RBAC manager (a test-only state) neither #2405's bit
// nor the undecodable-metadata bit is granted — both fail closed alike.
func TestMayWriteUndecodableMetadata_NoRBACFailsClosed(t *testing.T) {
	t.Parallel()
	d := &Deps{}
	r := httptest.NewRequest("PUT", "/api/v1/tenants/x", nil)
	if mayWriteUndecodableMetadata(d, rbac.RequestPrincipal(r)) {
		t.Error("mayWriteUndecodableMetadata = true without an RBAC manager, want false (fail closed)")
	}
	if mayWriteUndecodableMetadata(d, nil) != mayReplaceUnparseable(r, d) {
		t.Error("mayWriteUndecodableMetadata and mayReplaceUnparseable disagree without an RBAC manager")
	}
}
