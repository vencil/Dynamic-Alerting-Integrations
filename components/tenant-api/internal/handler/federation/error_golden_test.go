package federation

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/go-chi/chi/v5/middleware"
	"github.com/vencil/tenant-api/internal/federation/account"
	"github.com/vencil/tenant-api/internal/federation/token"
	"github.com/vencil/tenant-api/internal/gitops"
	"github.com/vencil/tenant-api/internal/statestore"
)

// errorGoldenPath is the snapshot of what writeFederationGitError answers for
// every error in federationErrorCorpus, and writeIssueError for every error
// in issueErrorCorpus (#3226) — the federation half of
// internal/handler/testdata/error_responses.golden, which cannot call this
// package's unexported entry. The corpus includes errors the entry never
// receives in production (the gitops sentinels: the account allocator and the
// state store it writes through do not import gitops); the golden pins what
// it answers for them too. Regenerate (after an INTENTIONAL change) with:
//
//	UPDATE_GOLDEN=1 go test ./internal/handler/federation/ -run TestFederationErrorResponses_Golden
var errorGoldenPath = filepath.Join("testdata", "error_responses.golden")

const goldenRequestID = "golden-req"

func federationErrorCorpus() []struct {
	name string
	err  error
} {
	type ge = struct {
		name string
		err  error
	}
	c := []ge{{"plain error (server path in text)", errors.New("open /srv/state/_account_registry.yaml: permission denied")}}
	for _, s := range []ge{
		{"statestore.ErrConflict", statestore.ErrConflict},
		{"statestore.ErrUnavailable", statestore.ErrUnavailable},
		{"statestore.ErrTooLarge", statestore.ErrTooLarge},
		{"account.ErrRegistryReset", account.ErrRegistryReset},
		{"account.ErrRegistryBehindHighWater", account.ErrRegistryBehindHighWater},
		{"context.Canceled", context.Canceled},
		{"context.DeadlineExceeded", context.DeadlineExceeded},
		{"gitops.ErrWriteOverloaded", gitops.ErrWriteOverloaded},
		{"gitops.ErrForgeDegraded", gitops.ErrForgeDegraded},
		{"gitops.ErrTreeNotOnBase", gitops.ErrTreeNotOnBase},
		{"gitops.ErrConflict", gitops.ErrConflict},
	} {
		c = append(c, s, ge{"wrapped(" + s.name + ")", fmt.Errorf("op ctx: %w", s.err)})
	}
	return c
}

// renderFederationCell renders one answer on one line: status, code, the
// Retry-After header / retry_after_s pair when either is set, NO-REQUEST-ID
// when the envelope lacks it, other body fields as sorted JSON, the text.
func renderFederationCell(err error) string {
	return renderEntryCell(err, func(w http.ResponseWriter, r *http.Request, err error) {
		writeFederationGitError(w, r, err, "backfill account ids: ")
	})
}

// issueErrorCorpus is what Issue / IssueLogs refuse a mint with, and an error
// they do not name (#3226 b: the mint rate limit's Retry-After).
func issueErrorCorpus() []struct {
	name string
	err  error
} {
	type ge = struct {
		name string
		err  error
	}
	return []ge{
		{"plain error (server path in text)", errors.New("open /srv/state/_federation_tokens.json: permission denied")},
		{"token.ErrTokenLimitReached", token.ErrTokenLimitReached},
		{"wrapped(token.ErrTokenLimitReached)", fmt.Errorf("op ctx: %w", token.ErrTokenLimitReached)},
		{"token.ErrMintRateLimited (bare: the table's Retry-After)", token.ErrMintRateLimited},
		{"MintRateLimitedError{Wait: 42.001s} (rounded up)", &token.MintRateLimitedError{Wait: 42*time.Second + time.Millisecond}},
		{"MintRateLimitedError{Wait: 0} (at least 1s)", &token.MintRateLimitedError{}},
		{"wrapped(MintRateLimitedError{Wait: 7s})", fmt.Errorf("op ctx: %w", &token.MintRateLimitedError{Wait: 7 * time.Second})},
	}
}

// renderEntryCell runs entry on err and renders the answer on one line.
func renderEntryCell(err error, entry func(w http.ResponseWriter, r *http.Request, err error)) string {
	r := httptest.NewRequest("POST", "/api/v1/federation/accounts/backfill", nil)
	r = r.WithContext(context.WithValue(r.Context(), middleware.RequestIDKey, goldenRequestID))
	rec := httptest.NewRecorder()
	entry(rec, r, err)
	var body map[string]any
	if jerr := json.Unmarshal(rec.Body.Bytes(), &body); jerr != nil {
		return fmt.Sprintf("%d non-JSON body %q", rec.Code, rec.Body.String())
	}
	str := func(k string) string {
		v, ok := body[k]
		if !ok {
			return "<absent>"
		}
		delete(body, k)
		if s, ok := v.(string); ok {
			return s
		}
		return fmt.Sprint(v)
	}
	text, code := str("error"), str("code")
	var opt []string
	raS := str("retry_after_s")
	if raH := rec.Header().Get("Retry-After"); raH != "" || raS != "<absent>" {
		if raH == "" {
			raH = "<absent>"
		}
		opt = append(opt, "Retry-After="+raH+" retry_after_s="+raS)
	}
	if str("request_id") != goldenRequestID {
		opt = append(opt, "NO-REQUEST-ID")
	}
	if len(body) > 0 {
		extra, _ := json.Marshal(body)
		opt = append(opt, "extra="+string(extra))
	}
	opt = append(opt, fmt.Sprintf("error=%q", text))
	return fmt.Sprintf("%d %s %s", rec.Code, code, strings.Join(opt, " "))
}

// TestFederationErrorResponses_Golden compares writeFederationGitError's
// answers with testdata/error_responses.golden. Each cell writes its own
// recorder; nothing process-global is written.
func TestFederationErrorResponses_Golden(t *testing.T) {
	t.Parallel()
	var b strings.Builder
	b.WriteString("# writeFederationGitError golden (#3226; msg500 = \"backfill account ids: \"). Regenerate with\n")
	b.WriteString("# UPDATE_GOLDEN=1 and review every changed line: each is a changed answer.\n\n")
	for _, ce := range federationErrorCorpus() {
		fmt.Fprintf(&b, "%s => %s\n", ce.name, renderFederationCell(ce.err))
	}
	b.WriteString("\n# writeIssueError (a federation token mint Issue / IssueLogs refused)\n")
	for _, ce := range issueErrorCorpus() {
		fmt.Fprintf(&b, "%s => %s\n", ce.name, renderEntryCell(ce.err, writeIssueError))
	}
	got := b.String()

	if os.Getenv("UPDATE_GOLDEN") != "" {
		if err := os.MkdirAll(filepath.Dir(errorGoldenPath), 0o755); err != nil {
			t.Fatalf("mkdir testdata: %v", err)
		}
		if err := os.WriteFile(errorGoldenPath, []byte(got), 0o644); err != nil {
			t.Fatalf("write golden: %v", err)
		}
		t.Logf("golden updated: %s — review the diff before committing", errorGoldenPath)
		return
	}
	want, err := os.ReadFile(errorGoldenPath)
	if err != nil {
		t.Fatalf("read golden: %v (regenerate with UPDATE_GOLDEN=1)", err)
	}
	if !strings.Contains(string(want), "statestore.ErrConflict") {
		t.Fatal("golden file is empty or truncated — regenerate with UPDATE_GOLDEN=1")
	}
	if got != string(want) {
		gl, wl := strings.Split(got, "\n"), strings.Split(string(want), "\n")
		for i := 0; i < len(gl) || i < len(wl); i++ {
			var g, w string
			if i < len(gl) {
				g = gl[i]
			}
			if i < len(wl) {
				w = wl[i]
			}
			if g != w {
				t.Fatalf("writeFederationGitError drifted from %s at line %d:\n  got:  %s\n  want: %s\n(if intentional, regenerate with UPDATE_GOLDEN=1 and review the diff)",
					errorGoldenPath, i+1, g, w)
			}
		}
	}
}
