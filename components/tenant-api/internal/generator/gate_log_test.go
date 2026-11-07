package generator

import (
	"bytes"
	"errors"
	"log/slog"
	"strings"
	"testing"
	"time"

	"github.com/vencil/tenant-api/internal/testutil"
)

// #1560 ⑤-b: a `findings` verdict reaches the platform log as structured
// fields only. stderr_tail and a finding's message / field quote what the
// tenants wrote (a credential among it); the log is read across a trust
// boundary the 422 (disclosedFindings, by RBAC) is not.
//
// Intentionally NOT t.Parallel(): it swaps the process-global default slog
// logger (the convention of handler/middleware_test.go and
// internal_error_redaction_test.go) and restores it.
func TestValidateWrites_FindingsLogNoGeneratorText(t *testing.T) {
	// A test canary, not a credential.
	const canary = "CANARY-gate-log-5b"
	var buf bytes.Buffer
	orig := slog.Default()
	defer slog.SetDefault(orig)
	slog.SetDefault(slog.New(slog.NewJSONHandler(&buf, &slog.HandlerOptions{Level: slog.LevelDebug})))

	fake := testutil.StartFakeGenerator(t)
	fake.SetValidate(func(n int, req testutil.FakeValidateRequest) (int, any) {
		f := testutil.Finding("credential_plaintext", "db-a", "db-a.yaml",
			"ERROR (credential): db-a.yaml: tenants.db-a._routing.receiver.api_url "+canary)
		f["field"] = "tenants.db-a._routing." + canary
		v := testutil.FindingsVerdict(req, "/w", f, testutil.Finding("credential_plaintext", "db-a", "db-a.yaml", "m"))
		v["stderr_tail"] = "  WARN: db-a: invalid group_wait " + canary + "\n"
		return 200, v
	})
	err := errOf(fastGate(t, fake, "").ValidateWrites(ctxFor(t, 20*time.Second), testBase, testFiles()))
	var fe *FindingsError
	if !errors.As(err, &fe) {
		t.Fatalf("err = %v, want *FindingsError", err)
	}
	logs := buf.String()
	if strings.Contains(logs, "CANARY") {
		t.Errorf("the log quotes the generator's text:\n%s", logs)
	}
	// Positive control: the refusal is logged, by its structured fields.
	if !strings.Contains(logs, "write refused with findings") ||
		!strings.Contains(logs, `"findings_by_kind":"credential_plaintext=2"`) {
		t.Errorf("no structured refusal line:\n%s", logs)
	}
}
