package main

import (
	"bytes"
	"os"
	"path/filepath"
	"slices"
	"strings"
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
	c, err := build(errcode.Specs(), handler.KindSpecs())
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

// TestBuild_Shapes: what one code's entry is built from — "depends" when its
// kinds disagree, statuses and kinds sorted and unique, retry_after_s only
// when every kind has the same one, a code no kind answers judged by its
// remedy, and a kind with an unlisted code refused.
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
		{Kind: "k1", Status: 502, Code: "B_MIXED", RetryAfterS: 5},
		{Kind: "k3", Status: 429, Code: "A_SAME", Retryable: true, RetryAfterS: 7},
		{Kind: "k4", Status: 429, Code: "A_SAME", Retryable: true, RetryAfterS: 7},
		{Kind: errkind.NoChanges, Status: 200},
	}
	c, err := build(codes, kinds)
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
	if e := got["B_MIXED"]; e.Retryable != retryDepends || !slices.Equal(e.Statuses, []int{502, 503}) ||
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
	if c, _ := build(codes, kinds); c.Codes[1].RetryAfterS != nil {
		t.Errorf("B_MIXED with Retry-After 1 and 5: retry_after_s %d, want null", *c.Codes[1].RetryAfterS)
	}
	if _, err := build(codes, append(kinds, handler.KindSpec{Kind: "k9", Status: 500, Code: "NOT_LISTED"})); err == nil {
		t.Error("a kind with a code errcode does not list was accepted")
	}
}

// TestRender_Bytes: two-space JSON, LF only, one final newline, and the
// same bytes every time.
func TestRender_Bytes(t *testing.T) {
	t.Parallel()
	c, err := build(errcode.Specs(), handler.KindSpecs())
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
// error.
func TestRun_ExitCodes(t *testing.T) {
	t.Parallel()
	out := filepath.Join(t.TempDir(), "errors.json")
	var so, se bytes.Buffer
	if rc := run([]string{"--check", "--out", out}, &so, &se); rc != 1 || !strings.Contains(se.String(), "missing") {
		t.Errorf("--check of a missing file: rc %d, stderr %q", rc, se.String())
	}
	if rc := run([]string{"--out", out}, &so, &se); rc != 0 {
		t.Fatalf("write: rc %d, stderr %q", rc, se.String())
	}
	if rc := run([]string{"--check", "--out", out}, &so, &se); rc != 0 {
		t.Errorf("--check after write: rc %d, stderr %q", rc, se.String())
	}
	b, _ := os.ReadFile(out)
	if err := os.WriteFile(out, bytes.Replace(b, []byte(`"never"`), []byte(`"always"`), 1), 0o644); err != nil {
		t.Fatal(err)
	}
	se.Reset()
	if rc := run([]string{"--check", "--out", out}, &so, &se); rc != 1 || !strings.Contains(se.String(), "DRIFT") {
		t.Errorf("--check of an edited file: rc %d, stderr %q", rc, se.String())
	}
	if err := os.WriteFile(out, bytes.ReplaceAll(b, []byte("\n"), []byte("\r\n")), 0o644); err != nil {
		t.Fatal(err)
	}
	if rc := run([]string{"--check", "--out", out}, &so, &se); rc != 1 {
		t.Errorf("--check of a CRLF copy: rc %d, want 1 (bytes, not text)", rc)
	}
	for _, args := range [][]string{{"--nope"}, {"extra"}} {
		if rc := run(args, &so, &se); rc != 2 {
			t.Errorf("run(%q): rc %d, want 2", args, rc)
		}
	}
}
