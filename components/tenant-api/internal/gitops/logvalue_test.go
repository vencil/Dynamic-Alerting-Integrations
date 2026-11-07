package gitops

import (
	"bytes"
	"errors"
	"log/slog"
	"slices"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/testutil"
)

// A test canary, not a credential.
const gitopsLogCanary = "CANARY-gitops-log-5b6"

// #1560 ⑤-b (review N-1): adviseKeyVerdict logged kv.Findings whole — a
// finding's key and reason quote what the tenants wrote. With findings
// withheld from the caller (another file, a truncated verdict), the log
// keeps the verdict's structured fields only.
func TestAdviseKeyVerdict_LogsNoFindingText(t *testing.T) {
	var buf bytes.Buffer
	orig := slog.Default()
	defer slog.SetDefault(orig)
	slog.SetDefault(slog.New(slog.NewJSONHandler(&buf, nil)))
	kv := &KeyVerdict{Status: KeyVerdictComplete, Truncated: 2, Findings: []KeyFinding{{
		Severity: "error", Kind: "unread", Key: "https://hooks.example.com/" + gitopsLogCanary,
		File: gitopsLogCanary + ".yaml", Tenants: []string{"t-" + gitopsLogCanary},
		Reason: "references unknown profile " + gitopsLogCanary}}}
	adviseKeyVerdict(t.Context(), []ProposedFile{{Path: "db-a.yaml", Tenants: []string{"db-a"}}}, kv)
	logs := buf.String()
	if strings.Contains(logs, "CANARY") {
		t.Errorf("the log quotes the key verdict's text:\n%s", logs)
	}
	if !strings.Contains(logs, "findings withheld") || !strings.Contains(logs, `"unread=1"`) {
		t.Errorf("no structured withheld line:\n%s", logs)
	}
}

func TestLogValuer_GitopsTypesLogNoText(t *testing.T) {
	t.Parallel()
	inner := errors.New("inner " + gitopsLogCanary)
	for name, v := range map[string]any{
		"KeyVerdict":         &KeyVerdict{Status: "not_completed", Reason: "r " + gitopsLogCanary},
		"KeyFinding":         KeyFinding{Kind: "unread", Key: gitopsLogCanary, Reason: gitopsLogCanary},
		"KeyVerdictError":    error(&KeyVerdictError{ByTenant: map[string][]string{"db-a": {gitopsLogCanary}}}),
		"BatchRejectedError": error(&BatchRejectedError{Outcomes: []BatchOpOutcome{{Err: inner}}}),
		"normalizedRefusal":  error(&normalizedRefusal{err: inner}),
	} {
		var buf bytes.Buffer
		slog.New(slog.NewJSONHandler(&buf, nil)).Info("x", "error", v)
		if strings.Contains(buf.String(), "CANARY") {
			t.Errorf("%s: the log quotes the text: %s", name, buf.String())
		}
	}
}

// gitopsErrorTypesWithoutGeneratorText: the error types of this package that
// never hold what the generator, da-guard or a tenant's file said — each
// with why. Every other error type must implement slog.LogValuer.
var gitopsErrorTypesWithoutGeneratorText = map[string]string{
	"BaseRestoreError":         "a branch name, a count and git's own error from restoring the base",
	"BatchCommittedError":      "the commit id and git's own error after the commit was made",
	"GitDriverError":           "the git config keys and scopes found (their values are never formatted)",
	"GitManagedError":          "a tenant id and a fixed reason word",
	"PlaceholderError":         "the request body's own masked-placeholder violations (field paths), not the generator's",
	"PreconditionError":        "a tenant id and two content hashes",
	"PushError":                "a branch name and git push's own error",
	"TargetUnreadableError":    "a tenant id, a conf.d file name and the walker's reason word",
	"TreeUnreadableError":      "conf.d file names and the walker's kinds",
	"WriteOutcomeUnknownError": "the commit id and git's own error from the ref update",
}

// Auxiliary to the behaviour matrix (internal/handler/log_canary_matrix_test.go):
// a new error type in this package — exported or not, an embedded Error()
// or a parenthesised receiver included (go/types method sets) — either
// implements slog.LogValuer or is listed above with why it holds no
// generator text.
func TestLogValuer_EveryErrorTypeDecides(t *testing.T) {
	t.Parallel()
	missing, all := testutil.ErrorTypesWithoutLogValuer(t, ".", gitopsErrorTypesWithoutGeneratorText)
	for _, want := range []string{"KeyVerdictError", "BatchRejectedError", "normalizedRefusal"} {
		if !slices.Contains(all, want) {
			t.Fatalf("the scan did not find %s (found %v)", want, all)
		}
	}
	for name := range gitopsErrorTypesWithoutGeneratorText {
		if !slices.Contains(all, name) {
			t.Errorf("exempt entry %s names no error type of this package", name)
		}
	}
	if len(missing) != 0 {
		t.Errorf("error types that neither implement slog.LogValuer nor say why they hold no "+
			"generator text: %v", missing)
	}
}
