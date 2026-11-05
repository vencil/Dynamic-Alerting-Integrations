package main

import (
	"bytes"
	"io"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"sync"
	"testing"

	"github.com/vencil/tenant-api/internal/errcode"
	"github.com/vencil/tenant-api/internal/errkind"
	"github.com/vencil/tenant-api/internal/handler"
)

// TestBuild_OneMeaningPerCode: over the real tables, every code of the
// contract says "always" or "never" — never "depends" (one code, two
// meanings) — and says what its remedy says.
func TestBuild_OneMeaningPerCode(t *testing.T) {
	t.Parallel()
	c, err := build(errcode.Specs(), handler.KindSpecs(), realDirect(t))
	if err != nil {
		t.Fatal(err)
	}
	if len(c.Codes) != len(errcode.Specs()) {
		t.Fatalf("%d entries for %d codes", len(c.Codes), len(errcode.Specs()))
	}
	for _, e := range c.Codes {
		want := retryNever
		if e.Remedy.Retryable() {
			want = retryAlways
		}
		if e.Retryable != want {
			t.Errorf("%s: retryable %q, its remedy %q says %q", e.Code, e.Retryable, e.Remedy, want)
		}
	}
}

// TestBuild_Shapes: what one code's entry is built from — retryable read from
// the remedy, statuses and kinds sorted and unique, retry_after_s only when
// every kind has the same one, and refused: a kind with an unlisted code, and
// a kind whose Retryable disagrees with its code's remedy (one code, two
// meanings — the contract has no "depends").
func TestBuild_Shapes(t *testing.T) {
	t.Parallel()
	codes := []errcode.Spec{
		{Code: "B_MIXED", Remedy: errcode.RemedyRetryAfter},
		{Code: "A_SAME", Remedy: errcode.RemedyRetryAfter},
		{Code: "C_NONE", Remedy: errcode.RemedyRefreshRetry},
		{Code: "D_NONE", Remedy: errcode.RemedyOperator},
	}
	kinds := []handler.KindSpec{
		{Kind: "k2", Status: 503, Code: "B_MIXED", Retryable: true, RetryAfterS: 5},
		{Kind: "k1", Status: 502, Code: "B_MIXED", Retryable: true, RetryAfterS: 5},
		{Kind: "k3", Status: 429, Code: "A_SAME", Retryable: true, RetryAfterS: 7},
		{Kind: "k4", Status: 429, Code: "A_SAME", Retryable: true, RetryAfterS: 7},
		{Kind: errkind.NoChanges, Status: 200},
	}
	c, err := build(codes, kinds, nil)
	if err != nil {
		t.Fatal(err)
	}
	got := map[errcode.Code]entry{}
	var order []string
	for _, e := range c.Codes {
		got[e.Code] = e
		order = append(order, string(e.Code))
	}
	if !slices.IsSorted(order) {
		t.Errorf("codes not sorted: %v", order)
	}
	if e := got["B_MIXED"]; e.Retryable != retryAlways || !slices.Equal(e.Statuses, []int{502, 503}) ||
		!slices.Equal(e.Kinds, []string{"k1", "k2"}) || e.RetryAfterS == nil || *e.RetryAfterS != 5 {
		t.Errorf("B_MIXED = %+v", e)
	}
	if e := got["A_SAME"]; e.Retryable != retryAlways || !slices.Equal(e.Statuses, []int{429}) || e.RetryAfterS == nil || *e.RetryAfterS != 7 {
		t.Errorf("A_SAME = %+v", e)
	}
	if e := got["C_NONE"]; e.Retryable != retryAlways || len(e.Statuses) != 0 || len(e.Kinds) != 0 || e.RetryAfterS != nil {
		t.Errorf("C_NONE = %+v", e)
	}
	if e := got["D_NONE"]; e.Retryable != retryNever {
		t.Errorf("D_NONE = %+v", e)
	}
	kinds[0].RetryAfterS = 1
	if c, _ := build(codes, kinds, nil); c.Codes[1].RetryAfterS != nil {
		t.Errorf("B_MIXED with Retry-After 1 and 5: retry_after_s %d, want null", *c.Codes[1].RetryAfterS)
	}
	if _, err := build(codes, append(kinds, handler.KindSpec{Kind: "k9", Status: 500, Code: "NOT_LISTED"}), nil); err == nil {
		t.Error("a kind with a code errcode does not list was accepted")
	}
	kinds[1].Retryable = false
	if _, err := build(codes, kinds, nil); err == nil || !strings.Contains(err.Error(), "kind k1") {
		t.Errorf("a kind whose retryable contradicts its remedy: err %v, want one naming kind k1", err)
	}
}

// TestRender_Bytes: two-space JSON, LF only, one final newline, and the
// same bytes every time.
func TestRender_Bytes(t *testing.T) {
	t.Parallel()
	c, err := build(errcode.Specs(), handler.KindSpecs(), realDirect(t))
	if err != nil {
		t.Fatal(err)
	}
	a, err := render(c)
	if err != nil {
		t.Fatal(err)
	}
	b, _ := render(c)
	if !bytes.Equal(a, b) {
		t.Fatal("render is not deterministic")
	}
	if bytes.Contains(a, []byte("\r")) || !bytes.HasSuffix(a, []byte("}\n")) || bytes.HasSuffix(a, []byte("\n\n")) {
		t.Fatalf("not LF with one final newline: %q", a[len(a)-8:])
	}
	if !bytes.HasPrefix(a, []byte("{\n  \"_generated\": ")) {
		t.Fatalf("unexpected head: %q", a[:40])
	}
}

// TestRun_ExitCodes: 0 written / in sync, 1 drift or missing, 2 a caller
// error. The first run scans the module itself; the later ones, over the
// same tree, are handed that answer (realScans) instead of each scanning it
// again (#3574).
func TestRun_ExitCodes(t *testing.T) {
	t.Parallel()
	mod, err := testModuleRoot()
	if err != nil {
		t.Fatal(err)
	}
	reuse := func(m string) ([]directUse, []extraUse, error) {
		if m != mod {
			t.Errorf("run read the module at %s, want %s", m, mod)
		}
		direct, extras := realScans(t)
		return direct, extras, nil
	}
	again := func(args []string, stdout, stderr io.Writer) int { return runWith(args, stdout, stderr, reuse) }
	out := filepath.Join(t.TempDir(), "errors.json")
	docs := copyDocs(t)
	var so, se bytes.Buffer
	if rc := run([]string{"--check", "--out", out, "--docs", docs}, &so, &se); rc != 1 || !strings.Contains(se.String(), "missing") {
		t.Errorf("--check of a missing file: rc %d, stderr %q", rc, se.String())
	}
	if rc := again([]string{"--out", out, "--docs", docs}, &so, &se); rc != 0 {
		t.Fatalf("write: rc %d, stderr %q", rc, se.String())
	}
	if rc := again([]string{"--check", "--out", out, "--docs", docs}, &so, &se); rc != 0 {
		t.Errorf("--check after write: rc %d, stderr %q", rc, se.String())
	}
	b, _ := os.ReadFile(out)
	if err := os.WriteFile(out, bytes.Replace(b, []byte(`"never"`), []byte(`"always"`), 1), 0o644); err != nil {
		t.Fatal(err)
	}
	se.Reset()
	if rc := again([]string{"--check", "--out", out, "--docs", docs}, &so, &se); rc != 1 || !strings.Contains(se.String(), "DRIFT") {
		t.Errorf("--check of an edited file: rc %d, stderr %q", rc, se.String())
	}
	if err := os.WriteFile(out, bytes.ReplaceAll(b, []byte("\n"), []byte("\r\n")), 0o644); err != nil {
		t.Fatal(err)
	}
	if rc := again([]string{"--check", "--out", out, "--docs", docs}, &so, &se); rc != 1 {
		t.Errorf("--check of a CRLF copy: rc %d, want 1 (bytes, not text)", rc)
	}
	// #3272: docs/ as swag alone writes it (no Extra properties) drifts.
	if err := os.WriteFile(out, b, 0o644); err != nil {
		t.Fatal(err)
	}
	y, _ := os.ReadFile(filepath.Join(docs, swaggerYAML))
	if err := os.WriteFile(filepath.Join(docs, swaggerYAML), bytes.Replace(y, []byte("      pr_number:\n"), []byte("      pr_numbr:\n"), 1), 0o644); err != nil {
		t.Fatal(err)
	}
	se.Reset()
	if rc := again([]string{"--check", "--out", out, "--docs", docs}, &so, &se); rc != 1 || !strings.Contains(se.String(), "swagger.yaml") {
		t.Errorf("--check of an edited swagger.yaml: rc %d, stderr %q", rc, se.String())
	}
	for _, args := range [][]string{{"--nope"}, {"extra"}} {
		if rc := again(args, &so, &se); rc != 2 {
			t.Errorf("run(%q): rc %d, want 2", args, rc)
		}
	}
}

// realScan is this module's own scans, run once per test binary (#3574):
// scanExtra type-checks the whole module, the tree does not change under the
// tests, and every test that read the real answers — times -count — used to
// pay for its own. Written only inside once; each caller gets a copy.
var realScan struct {
	once   sync.Once
	direct []directUse
	extra  []extraUse
	err    error
}

// realScans is the direct answers and the Extra fields of this module.
func realScans(t *testing.T) ([]directUse, []extraUse) {
	t.Helper()
	realScan.once.Do(func() {
		mod, err := testModuleRoot()
		if err != nil {
			realScan.err = err
			return
		}
		realScan.direct, realScan.extra, realScan.err = scanModule(mod)
	})
	if realScan.err != nil {
		t.Fatal(realScan.err)
	}
	return slices.Clone(realScan.direct), slices.Clone(realScan.extra)
}

// testModuleRoot is the module root above the test's directory.
func testModuleRoot() (string, error) {
	wd, err := os.Getwd()
	if err != nil {
		return "", err
	}
	return moduleRoot(wd)
}

// realDirect is the direct answers of this module.
func realDirect(t *testing.T) []directUse {
	t.Helper()
	direct, _ := realScans(t)
	return direct
}

// TestScanDirect_ReadsTheDirectAnswers: the answers no kind gives are read —
// a WriteJSONErrorWithCode, an envelope literal, WriteJSONError through
// codeFromStatus, the RBAC middleware's writeError and writeEnvelope, and
// writeJSON's own 500.
func TestScanDirect_ReadsTheDirectAnswers(t *testing.T) {
	t.Parallel()
	got := map[string]bool{}
	for _, d := range realDirect(t) {
		got[string(d.Code)+" "+strconv.Itoa(d.Status)+" "+strings.SplitN(d.Site, ":", 2)[0]] = true
	}
	for _, want := range []string{
		"GROUP_ID_UNAVAILABLE 409 internal/handler/group.go",
		"TASK_NOT_FOUND 404 internal/handler/task.go",
		"UPSTREAM_ERROR 502 internal/handler/discovery.go",
		"PAYLOAD_TOO_LARGE 413 internal/handler/request_body.go",
		"UNAUTHORIZED 401 internal/rbac/middleware.go",
		"FORBIDDEN 403 internal/rbac/middleware.go",
		"INVALID_BODY 400 internal/handler/federation/policy.go",
		"INTERNAL_ERROR 500 internal/handler/errors.go",
	} {
		if !got[want] {
			t.Errorf("direct answers lack %q", want)
		}
	}
}

// plantedModule is a copy of this module's internal/ and cmd/ — with its
// go.mod (relative replace targets made absolute) and go.sum, so the copy
// type-checks — with file added as internal/handler/zz_planted.go.
func plantedModule(t *testing.T, file string) string {
	t.Helper()
	return plantedModuleFiles(t, map[string]string{"zz_planted.go": file})
}

// plantedModuleFiles is plantedModule with each file added under
// internal/handler by its name. A scan of a copy builds every package in it
// (its directory is new to the build cache), so a test with several planted
// shapes plants them all in one copy (#3574).
func plantedModuleFiles(t *testing.T, files map[string]string) string {
	t.Helper()
	wd, _ := os.Getwd()
	mod, err := moduleRoot(wd)
	if err != nil {
		t.Fatal(err)
	}
	dst := t.TempDir()
	for _, dir := range []string{"internal", "cmd"} {
		if err := os.CopyFS(filepath.Join(dst, dir), os.DirFS(filepath.Join(mod, dir))); err != nil {
			t.Fatal(err)
		}
	}
	gomod, err := os.ReadFile(filepath.Join(mod, "go.mod"))
	if err != nil {
		t.Fatal(err)
	}
	gomod = bytes.ReplaceAll(gomod, []byte("=> ../"), []byte("=> "+filepath.Dir(mod)+"/"))
	gosum, err := os.ReadFile(filepath.Join(mod, "go.sum"))
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dst, "go.mod"), gomod, 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dst, "go.sum"), gosum, 0o644); err != nil {
		t.Fatal(err)
	}
	for name, file := range files {
		if err := os.WriteFile(filepath.Join(dst, "internal", "handler", name), []byte(file), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	return dst
}

// TestScanDirect_FailsClosed: a writer call whose status or code cannot be
// read, an envelope built apart from its writer, a status codeFromStatus does
// not map, and an errcode constant used some other way each stop the scan
// with their place — never a contract that silently lacks them.
func TestScanDirect_FailsClosed(t *testing.T) {
	t.Parallel()
	for name, body := range map[string]string{
		"status variable": `WriteJSONErrorWithCode(w, r, st, errcode.Conflict, "x")`,
		"code variable":   `WriteJSONErrorWithCode(w, r, http.StatusConflict, c, "x")`,
		"envelope apart":  `env := ErrorResponse{Error: "x", Code: errcode.Conflict}; WriteErrorEnvelope(w, r, http.StatusConflict, env)`,
		"unmapped status": `WriteJSONError(w, r, http.StatusTeapot, "x")`,
		"string code":     `WriteErrorEnvelope(w, r, http.StatusConflict, ErrorResponse{Code: "MADE_UP"})`,
		"other use":       `_ = []errcode.Code{errcode.Conflict}`,
		"2xx status":      `WriteJSONErrorWithCode(w, r, http.StatusOK, errcode.Conflict, "x")`,
	} {
		src := "package handler\n\nimport (\n\t\"net/http\"\n\n\t\"github.com/vencil/tenant-api/internal/errcode\"\n)\n\n" +
			"func planted(w http.ResponseWriter, r *http.Request, st int, c errcode.Code) {\n\t" + body + "\n}\n"
		_, err := scanDirect(plantedModule(t, src))
		if err == nil || !strings.Contains(err.Error(), "internal/handler/zz_planted.go:") {
			t.Errorf("%s: scan error %v, want one naming the planted file", name, err)
		}
	}
	// The control: a literal call in the planted file is read, not refused.
	direct, err := scanDirect(plantedModule(t, "package handler\n\nimport (\n\t\"net/http\"\n\n\t\"github.com/vencil/tenant-api/internal/errcode\"\n)\n\n"+
		"func planted(w http.ResponseWriter, r *http.Request) {\n\tWriteJSONErrorWithCode(w, r, http.StatusTeapot, errcode.PendingPR, \"x\")\n}\n"))
	if err != nil {
		t.Fatal(err)
	}
	if !slices.ContainsFunc(direct, func(d directUse) bool { return d.Status == 418 && d.Code == errcode.PendingPR }) {
		t.Error("the planted literal call was not read")
	}
}

// TestBuild_StatusMustFitRemedy: an answer whose status says the opposite of
// its code's remedy is refused.
func TestBuild_StatusMustFitRemedy(t *testing.T) {
	t.Parallel()
	codes := []errcode.Spec{{Code: "X", Remedy: errcode.RemedyRefreshRetry}}
	if _, err := build(codes, nil, []directUse{{Status: 409, Code: "X", Site: "a.go:1"}}); err != nil {
		t.Errorf("409 refresh_retry: %v", err)
	}
	if _, err := build(codes, nil, []directUse{{Status: 400, Code: "X", Site: "a.go:1"}}); err == nil || !strings.Contains(err.Error(), "a.go:1") {
		t.Errorf("400 refresh_retry: err %v, want one naming a.go:1", err)
	}
	if _, err := build(codes, []handler.KindSpec{{Kind: "k", Status: 503, Code: "X"}}, nil); err == nil {
		t.Error("a 503 kind with remedy refresh_retry was accepted")
	}
}
