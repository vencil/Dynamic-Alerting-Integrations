package handler

import (
	"encoding/json"
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strconv"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/confd"
	"github.com/vencil/tenant-api/internal/errcode"
	"github.com/vencil/tenant-api/internal/errkind"
	"github.com/vencil/tenant-api/internal/gitops"
)

// errkindConstants parses internal/errkind for every constant of type Kind:
// the list the table must cover, read from the source so a new Kind cannot
// be forgotten here as well as in the table.
func errkindConstants(t *testing.T) []errkind.Kind {
	t.Helper()
	fset := token.NewFileSet()
	paths, err := filepath.Glob(filepath.Join("..", "errkind", "*.go"))
	if err != nil {
		t.Fatal(err)
	}
	var out []errkind.Kind
	for _, path := range paths {
		if strings.HasSuffix(path, "_test.go") {
			continue
		}
		f, err := parser.ParseFile(fset, path, nil, 0)
		if err != nil {
			t.Fatalf("parse %s: %v", path, err)
		}
		for _, decl := range f.Decls {
			gd, ok := decl.(*ast.GenDecl)
			if !ok || gd.Tok != token.CONST {
				continue
			}
			for _, spec := range gd.Specs {
				vs := spec.(*ast.ValueSpec)
				if id, ok := vs.Type.(*ast.Ident); !ok || id.Name != "Kind" {
					continue
				}
				for _, v := range vs.Values {
					lit, ok := v.(*ast.BasicLit)
					if !ok {
						t.Fatalf("errkind constant %v is not a string literal", vs.Names)
					}
					s, err := strconv.Unquote(lit.Value)
					if err != nil {
						t.Fatal(err)
					}
					out = append(out, errkind.Kind(s))
				}
			}
		}
	}
	if len(out) < 10 {
		t.Fatalf("read %d errkind constants; the parser no longer finds them", len(out))
	}
	return out
}

// TestKindTable_CoversEveryKindOnce: every errkind.Kind has exactly one row,
// every row is a Kind, and every kindRules row and fallback kind has a row —
// kindSpecOf never falls back to Internal for a Kind someone declared.
func TestKindTable_CoversEveryKindOnce(t *testing.T) {
	t.Parallel()
	declared := map[errkind.Kind]bool{}
	for _, k := range errkindConstants(t) {
		if declared[k] {
			t.Errorf("errkind declares %q twice", k)
		}
		declared[k] = true
	}
	rows := map[errkind.Kind]int{}
	for _, sp := range kindTable {
		rows[sp.Kind]++
		if !declared[sp.Kind] {
			t.Errorf("kindTable row %q is not an errkind constant", sp.Kind)
		}
		if sp.Kind != errkind.NoChanges && (sp.Status < 400 || sp.Code == "") {
			t.Errorf("kindTable row %q: status %d code %q — a failure needs an error status and a code", sp.Kind, sp.Status, sp.Code)
		}
	}
	for k := range declared {
		if rows[k] != 1 {
			t.Errorf("errkind.%s has %d kindTable rows, want 1", k, rows[k])
		}
	}
	ruled := map[errkind.Kind]bool{}
	for _, rl := range kindRules {
		if ruled[rl.Kind] {
			t.Errorf("kindRules lists %q twice: the second row is dead", rl.Kind)
		}
		ruled[rl.Kind] = true
		if rows[rl.Kind] != 1 {
			t.Errorf("kindRules row %q has no kindTable row", rl.Kind)
		}
		if !rl.typed && len(rl.sentinels) == 0 && rl.match == nil {
			t.Errorf("kindRules row %q matches nothing", rl.Kind)
		}
	}
	// The kinds no error is: an entry's fallback, or the entry itself
	// (a batch op's permission gate), picks them.
	for _, k := range []errkind.Kind{errkind.BadRequest, errkind.Internal, errkind.ForgeCreateFailed, errkind.Forbidden,
		errkind.FederationNotConfigured, errkind.RouteNotFound, errkind.MethodNotAllowed, errkind.RequestTimeout} {
		if ruled[k] {
			t.Errorf("fallback kind %q has a kindRules row", k)
		}
		ruled[k] = true
	}
	for k := range declared {
		if !ruled[k] {
			t.Errorf("errkind.%s has neither a kindRules row nor a fallback that answers it", k)
		}
	}
}

// TestKindTable_OneCodeOneMeaning (#3202): a code means one thing to a
// client. Every kindTable code is an errcode code, and every kind answered
// with it agrees with that code's Remedy — a kind a resend may get past
// (Retryable) has a remedy that resends, one with a Retry-After the remedy
// that waits for it, and the other way round. There is no exception list:
// a kind that needs another remedy needs another code.
//
// ClientError is deliberately not compared: it decides how tenant-api
// renders the error (whose text is shown, the fallback's 400), not what the
// client can do, and TENANT_CONFIG_NOT_LOADABLE's kinds differ in it.
func TestKindTable_OneCodeOneMeaning(t *testing.T) {
	t.Parallel()
	byCode := map[errcode.Code][]errkind.Kind{}
	for _, sp := range kindTable {
		if sp.Code == "" {
			continue // NoChanges: not a failure (TestKindTable_CoversEveryKindOnce)
		}
		spec, ok := errcode.Lookup(sp.Code)
		if !ok {
			t.Errorf("kindTable row %q: code %q is not in internal/errcode's table", sp.Kind, sp.Code)
			continue
		}
		byCode[sp.Code] = append(byCode[sp.Code], sp.Kind)
		if sp.Retryable != spec.Remedy.Retryable() {
			t.Errorf("kindTable row %q: Retryable %v, but %s's remedy %q says %v — give the kind its own code",
				sp.Kind, sp.Retryable, sp.Code, spec.Remedy, spec.Remedy.Retryable())
		}
		if sp.RetryAfterS > 0 && spec.Remedy != errcode.RemedyRetryAfter {
			t.Errorf("kindTable row %q: Retry-After %ds, but %s's remedy is %q, not %q",
				sp.Kind, sp.RetryAfterS, sp.Code, spec.Remedy, errcode.RemedyRetryAfter)
		}
	}
	if len(byCode) < 10 {
		t.Fatalf("kindTable names %d codes; the table is not being read", len(byCode))
	}
}

// retryableWithoutRetryAfter are the retryable 5xx kinds that deliberately
// carry no Retry-After, each with why. Anything else retryable at 5xx must
// say when to come back (TestKindTable_RetryableServerErrorsSayWhen).
var retryableWithoutRetryAfter = map[errkind.Kind]string{
	// The write stopped before it committed because ITS OWN request (or async
	// task) was cancelled or reached its deadline, or one git command hit its
	// own deadline — nothing on the server is busy or recovering, so there is
	// no wait to pace: the caller may resend at once (msgWriteCancelled says
	// it is safe to retry).
	errkind.WriteCancelled: "the request's own cancellation or deadline; nothing server-side to wait for",
}

// TestKindTable_RetryableServerErrorsSayWhen: a retryable 5xx without
// Retry-After leaves an automated client to guess its back-off (#3226 found
// one: the forge PR/MR creation 503). Every Retryable row with Status >= 500
// has RetryAfterS > 0, or is a listed exception; an exception that no longer
// applies is reported too, so the list cannot outlive its reason.
func TestKindTable_RetryableServerErrorsSayWhen(t *testing.T) {
	t.Parallel()
	for _, sp := range kindTable {
		_, exempt := retryableWithoutRetryAfter[sp.Kind]
		needs := sp.Retryable && sp.Status >= 500
		switch {
		case needs && sp.RetryAfterS <= 0 && !exempt:
			t.Errorf("kindTable row %q: retryable %d with no RetryAfterS — give it one, or list it in "+
				"retryableWithoutRetryAfter with the reason", sp.Kind, sp.Status)
		case exempt && (!needs || sp.RetryAfterS > 0):
			t.Errorf("retryableWithoutRetryAfter lists %q, which is not a retryable 5xx without "+
				"Retry-After any more; drop the exception", sp.Kind)
		}
	}
}

// TestKindSpecs_IsACopy: the exported listing cannot edit the table.
func TestKindSpecs_IsACopy(t *testing.T) {
	t.Parallel()
	specs := KindSpecs()
	if len(specs) != len(kindTable) {
		t.Fatalf("KindSpecs has %d rows, the table %d", len(specs), len(kindTable))
	}
	specs[0].Status = 0
	if kindTable[0].Status == 0 {
		t.Fatal("KindSpecs returned the table itself")
	}
}

// TestBatchProjections_CodeIsTheTableAnswer (#3226): a batch op's "error"
// result always carries a code, and it is kindTable's code for the kind the
// result is answered as — the projection reads the table, never a code of
// its own — for every error of the golden corpus.
func TestBatchProjections_CodeIsTheTableAnswer(t *testing.T) {
	t.Parallel()
	// An error about the request keeps its own answer in a batch: every
	// ClientError kind an error can be is one a per-op result names, never
	// the INTERNAL_ERROR fallback.
	for _, rl := range kindRules {
		if kindSpecOf(rl.Kind).ClientError && !kindsBatchOp[rl.Kind] {
			t.Errorf("client-error kind %q is not in kindsBatchOp: a batch op would answer it as INTERNAL_ERROR", rl.Kind)
		}
	}
	for _, ge := range errorGoldenCorpus("0123456789abcdef") {
		for _, err := range []error{ge.err, ge.wrapped} {
			if err == nil {
				continue
			}
			want := kindSpecOf(batchOpKind(err)).Code
			if got := batchErrorResult(goldenTenant, err, false); got.Code == "" || got.Code != want {
				t.Errorf("batchErrorResult(%s) code = %q, want kindTable's %q", ge.name, got.Code, want)
			}
			if got := freshBaseOpResult(goldenTenant, err, false); got.Code == "" {
				t.Errorf("freshBaseOpResult(%s) has no code", ge.name)
			}
			if res, ok := generatorOpResult(goldenTenant, err); ok && res.Code == "" {
				t.Errorf("generatorOpResult(%s) has no code", ge.name)
			}
		}
	}
}

// #3226 review: a batch's whole-request answer for a tenant file that is
// not a regular file names no tenant — never the route's {id}, which on
// POST /groups/{id}/batch is a group id — and shows a fixed text, not the
// error's own ("op ctx: confd: not a regular file: …").
func TestNotRegularFile_BatchAnswerNamesNoTenant(t *testing.T) {
	t.Parallel()
	err := fmt.Errorf("op ctx: %w", fmt.Errorf("%w: t-a.yaml", confd.ErrNotRegularFile))
	for name, write := range map[string]func(http.ResponseWriter, *http.Request){
		"writeBatchFailure": func(w http.ResponseWriter, r *http.Request) { writeBatchFailure(w, r, &Deps{}, err) },
		"writePRBatchError": func(w http.ResponseWriter, r *http.Request) { writePRBatchError(w, r, &Deps{}, err) },
	} {
		w := httptest.NewRecorder()
		write(w, newRequestWithChiParam("POST", "/api/v1/groups/team-payments/batch", "id", "team-payments", nil))
		if w.Code != http.StatusConflict {
			t.Fatalf("%s: status %d, want 409; body %s", name, w.Code, w.Body.String())
		}
		var body map[string]any
		if jerr := json.Unmarshal(w.Body.Bytes(), &body); jerr != nil {
			t.Fatal(jerr)
		}
		if _, ok := body["tenant_id"]; ok || body["error"] != msgTenantFileNotRegular || body["code"] != string(errcode.TenantConfigNotLoadable) {
			t.Errorf("%s: body %s, want %s with msgTenantFileNotRegular and no tenant_id", name, w.Body.String(), errcode.TenantConfigNotLoadable)
		}
	}
}

// #3226 review: a dry-run refused because the git config could not be read
// says so, with the write's fixed text — not the resolver's catch-all.
func TestDryRunRefusalMessage_GitConfigUnreadable(t *testing.T) {
	t.Parallel()
	err := fmt.Errorf("%w: exit status 128", gitops.ErrGitConfigUnreadable)
	if got := dryRunRefusalMessage(err, false); got != msgGitConfigUnreadable {
		t.Errorf("dryRunRefusalMessage = %q, want msgGitConfigUnreadable", got)
	}
}
