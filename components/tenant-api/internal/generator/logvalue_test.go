package generator

import (
	"bytes"
	"errors"
	"fmt"
	"log/slog"
	"slices"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/testutil"
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
		"UnavailableError": {error(&UnavailableError{Code: CodeValidatorUnavailable, Reason: ReasonToolFault,
			Message: "container: " + logCanary}), `"reason":"` + ReasonToolFault + `"`},
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
		"FindingsError, a kind outside the catalog": {error(&FindingsError{Findings: []Finding{
			{Kind: "k-" + logCanary}}, RC: 1}), `"findings_by_kind":["other=1"]`},
		"Verdict, a result outside the vocabulary": {&Verdict{Result: "r-" + logCanary}, `"result":"other"`},
		"KeyVerdict, a status outside the vocabulary": {&KeyVerdict{Status: "s-" + logCanary, Reason: "error"},
			`"status":"other"`},
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
// never err.
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

// Auxiliary to the behaviour matrix (the main guard): every type declared
// here — exported or not — whose
// method set implements error (a promoted Error(), a parenthesised
// receiver included: go/types, not a name or syntax match) also implements
// slog.LogValuer, so a new error type decides whether it carries the
// generator's text.
func TestLogValuer_EveryErrorType(t *testing.T) {
	t.Parallel()
	missing, all := testutil.ErrorTypesWithoutLogValuer(t, ".", nil)
	for _, want := range []string{"FindingsError", "RejectedError", "UnavailableError"} {
		if !slices.Contains(all, want) {
			t.Fatalf("the scan did not find %s (found %v)", want, all)
		}
	}
	if len(missing) != 0 {
		t.Errorf("error types without slog.LogValuer (they would log their text): %v", missing)
	}
}
