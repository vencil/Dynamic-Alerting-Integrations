package generator

import (
	"bytes"
	"errors"
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"log/slog"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"
)

// A test canary, not a credential.
const logCanary = "CANARY-logvaluer-5b"

func logged(t *testing.T, args ...any) string {
	t.Helper()
	var buf bytes.Buffer
	slog.New(slog.NewJSONHandler(&buf, nil)).Info("x", args...)
	return buf.String()
}

// #1560 ⑤-b: every type of this package that carries the container's or
// the generator's text logs itself as structured fields only — handed to
// slog as `"error", err` (an error interface holding the pointer), the way
// every call site logs one.
func TestLogValuer_ErrorTypesLogNoMessage(t *testing.T) {
	t.Parallel()
	rc := 1
	finding := Finding{Kind: "credential_plaintext", Message: "m " + logCanary}
	for name, tc := range map[string]struct {
		value any
		want  string
	}{
		"UnavailableError": {error(&UnavailableError{Code: CodeValidatorUnavailable, Reason: ReasonDaGuardMissing,
			Message: "container: " + logCanary}), `"reason":"` + ReasonDaGuardMissing + `"`},
		"UnavailableError unknown reason": {error(&UnavailableError{Code: CodeValidatorUnavailable,
			Reason: "r " + logCanary, Message: logCanary}), `"reason":"unknown"`},
		"RejectedError": {error(&RejectedError{Reason: ReasonMemory, Message: logCanary}),
			`"reason":"` + ReasonMemory + `"`},
		"RejectedError unknown reason": {error(&RejectedError{Reason: logCanary, Message: logCanary}),
			`"reason":"unknown"`},
		"FindingsError": {error(&FindingsError{Findings: []Finding{finding}, RC: 1}),
			`"findings_by_kind":["credential_plaintext=1"]`},
		"Verdict": {&Verdict{Result: ResultFindings, RC: &rc, StderrTail: logCanary, Message: logCanary,
			Findings: &FindingsDoc{ConfigDir: "/w/" + logCanary, Findings: []Finding{finding}}},
			`"findings_by_kind":["credential_plaintext=1"]`},
		"KeyVerdict": {&KeyVerdict{Status: "not_completed", Reason: "error", Message: logCanary},
			`"reason":"error"`},
		"Finding": {finding, `"kind":"credential_plaintext"`},
	} {
		out := logged(t, "error", tc.value)
		if strings.Contains(out, "CANARY") {
			t.Errorf("%s: the log quotes the text: %s", name, out)
		}
		if !strings.Contains(out, tc.want) {
			t.Errorf("%s: %s missing from %s", name, tc.want, out)
		}
	}
}

// Pins how slog resolves a LogValuer (Go's log/slog): on the value it is
// handed — an error interface holding *UnavailableError resolves — but not
// through a wrapper: fmt.Errorf("…: %w", e) logs its Error() text, the
// message included. So a call site logs the typed error errors.As found,
// never err (handler/error_render.go, errors.go, tenant_validate_judge.go).
func TestLogValuer_ResolvedOnTheTypedErrorOnly(t *testing.T) {
	t.Parallel()
	typed := &UnavailableError{Code: CodeValidatorUnavailable, Reason: ReasonInternal, Message: logCanary}
	if out := logged(t, "error", error(typed)); strings.Contains(out, logCanary) {
		t.Errorf("an error interface holding the pointer did not resolve LogValue: %s", out)
	}
	if out := logged(t, slog.Any("error", typed)); strings.Contains(out, logCanary) {
		t.Errorf("slog.Any of the pointer did not resolve LogValue: %s", out)
	}
	wrapped := fmt.Errorf("write: %w", typed)
	if out := logged(t, "error", wrapped); !strings.Contains(out, logCanary) {
		t.Errorf("a wrapper now resolves LogValue (update the call-site rule): %s", out)
	}
	var back *UnavailableError
	if !errors.As(wrapped, &back) {
		t.Fatal("errors.As lost the typed error")
	}
	if out := logged(t, "error", back); strings.Contains(out, logCanary) {
		t.Errorf("the typed error errors.As found did not resolve LogValue: %s", out)
	}
}

// Every exported type of this package with an `Error() string` method
// also has `LogValue() slog.Value`: a new error type that would log its
// text fails here first.
func TestLogValuer_EveryExportedErrorType(t *testing.T) {
	t.Parallel()
	files, err := filepath.Glob("*.go")
	if err != nil {
		t.Fatal(err)
	}
	methods := map[string]map[string]bool{}
	fset := token.NewFileSet()
	for _, name := range files {
		if strings.HasSuffix(name, "_test.go") {
			continue
		}
		src, err := os.ReadFile(name)
		if err != nil {
			t.Fatal(err)
		}
		f, err := parser.ParseFile(fset, name, src, 0)
		if err != nil {
			t.Fatal(err)
		}
		for _, decl := range f.Decls {
			fn, ok := decl.(*ast.FuncDecl)
			if !ok || fn.Recv == nil || len(fn.Recv.List) != 1 {
				continue
			}
			recv := fn.Recv.List[0].Type
			if star, ok := recv.(*ast.StarExpr); ok {
				recv = star.X
			}
			ident, ok := recv.(*ast.Ident)
			if !ok {
				continue
			}
			if methods[ident.Name] == nil {
				methods[ident.Name] = map[string]bool{}
			}
			methods[ident.Name][fn.Name.Name] = true
		}
	}
	var errorTypes, missing []string
	for typ, m := range methods {
		if !ast.IsExported(typ) || !m["Error"] {
			continue
		}
		errorTypes = append(errorTypes, typ)
		if !m["LogValue"] {
			missing = append(missing, typ)
		}
	}
	sort.Strings(errorTypes)
	sort.Strings(missing)
	// Positive control: the scan finds the error types it guards.
	for _, want := range []string{"FindingsError", "RejectedError", "UnavailableError"} {
		if i := sort.SearchStrings(errorTypes, want); i >= len(errorTypes) || errorTypes[i] != want {
			t.Fatalf("the scan did not find %s (found %v)", want, errorTypes)
		}
	}
	if len(missing) != 0 {
		t.Errorf("exported error types without slog.LogValuer (they would log their text): %v", missing)
	}
}
