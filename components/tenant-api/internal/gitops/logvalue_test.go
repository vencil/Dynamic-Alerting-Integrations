package gitops

import (
	"bytes"
	"errors"
	"go/ast"
	"go/parser"
	"go/token"
	"log/slog"
	"slices"
	"strconv"
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
		"KeyVerdict": &KeyVerdict{Status: "not_completed", Reason: "r " + gitopsLogCanary},
		"KeyVerdict, a status and kind outside the vocabulary": &KeyVerdict{Status: "s-" + gitopsLogCanary,
			Findings: []KeyFinding{{Kind: "k-" + gitopsLogCanary, Severity: gitopsLogCanary}}},
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

// Auxiliary to the behaviour matrix: a new error type in this package — exported or not, an embedded Error()
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

// The key verdict kinds the log keeps (verdictKinds' keys, else "other")
// are the shared verdict's own (pkg/keyverdict, the SSOT; #1560 ⑤-b round
// 7): read from its source, not a third list. A const block that cannot be
// found, or no kind read from it, fails — not read is not equal.
func TestVerdictKinds_AreTheSharedVerdictsKinds(t *testing.T) {
	t.Parallel()
	const src = "../../../threshold-exporter/app/pkg/keyverdict/keyverdict.go"
	f, err := parser.ParseFile(token.NewFileSet(), src, nil, parser.ParseComments)
	if err != nil {
		t.Fatalf("read the shared verdict's kinds: %v", err)
	}
	var kinds []string
	for _, d := range f.Decls {
		g, ok := d.(*ast.GenDecl)
		if !ok || g.Tok != token.CONST || g.Doc == nil || strings.TrimSpace(g.Doc.Text()) != "Finding kinds." {
			continue
		}
		for _, spec := range g.Specs {
			vs := spec.(*ast.ValueSpec)
			for i, name := range vs.Names {
				if !strings.HasPrefix(name.Name, "Kind") || i >= len(vs.Values) {
					continue
				}
				if lit, ok := vs.Values[i].(*ast.BasicLit); ok && lit.Kind == token.STRING {
					v, _ := strconv.Unquote(lit.Value)
					kinds = append(kinds, v)
				}
			}
		}
	}
	if len(kinds) == 0 {
		t.Fatalf("no Kind constant under `// Finding kinds.` in %s", src)
	}
	var ours []string
	for k := range verdictKinds {
		ours = append(ours, k)
	}
	slices.Sort(kinds)
	slices.Sort(ours)
	if !slices.Equal(kinds, ours) {
		t.Errorf("verdictKinds %v, the shared verdict's kinds %v: add a row (its text, and what the log keeps)", ours, kinds)
	}
}
