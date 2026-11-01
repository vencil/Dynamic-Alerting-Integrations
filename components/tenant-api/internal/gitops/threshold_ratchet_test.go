package gitops

// #3267 (ADR-038 decision 12): a threshold value the exporter refuses is a
// refusal when this write puts it there, and a notice when the file already
// holds it as the exporter reads it — compared by the exporter's reading,
// not the text; strict whenever the file before the write is not known.

import (
	"context"
	"errors"
	"strings"
	"testing"

	cfg "github.com/vencil/threshold-exporter/pkg/config"
	"gopkg.in/yaml.v3"
)

const refusedText = "is not a threshold the exporter reads"

func TestRefusedThresholdValues_Ratchet(t *testing.T) {
	sec := func(id, kv string) string { return "  " + id + ":\n    " + kv + "\n" }
	doc := func(secs ...string) string { return "tenants:\n" + strings.Join(secs, "") }
	cases := []struct {
		name, base, body   string
		baseErr            error
		wantErrs, wantNote int
	}{
		{name: "unchanged", base: doc(sec("t1", `k: "abc"`)), body: doc(sec("t1", `k: "abc"`)), wantNote: 1},
		{name: "unchanged unquoted", base: doc(sec("t1", `k: "abc"`)), body: doc(sec("t1", `k: abc`)), wantNote: 1},
		{name: "spacing read alike", base: doc(sec("t1", `k: "abc"`)), body: doc(sec("t1", `k: " abc "`)), wantNote: 1},
		{name: "suffix case read alike", base: doc(sec("t1", `k: "70:HIGH"`)), body: doc(sec("t1", `k: "70 : high"`)), wantNote: 1},
		{name: "default suffix read alike", base: doc(sec("t1", `k: "abc"`)), body: doc(sec("t1", `k: "abc:warning"`)), wantNote: 1},
		{name: "changed", base: doc(sec("t1", `k: "abc"`)), body: doc(sec("t1", `k: "xyz"`)), wantErrs: 1},
		{name: "changed severity", base: doc(sec("t1", `k: "abc"`)), body: doc(sec("t1", `k: "abc:critical"`)), wantErrs: 1},
		{name: "new key", base: doc(sec("t1", `j: "70"`)), body: doc(sec("t1", `k: "1e999"`)), wantErrs: 1},
		{name: "was readable", base: doc(sec("t1", `k: "70"`)), body: doc(sec("t1", `k: "70:high"`)), wantErrs: 1},
		{name: "moved into a window", base: doc(sec("t1", `k: "abc"`)),
			body: doc(sec("t1", "k:\n      default: \"abc\"\n      overrides:\n        - window: \"01:00-02:00\"\n          value: \"abc\"")), wantErrs: 1},
		{name: "no base file", base: "", body: doc(sec("t1", `k: "abc"`)), wantErrs: 1},
		{name: "base read error", base: doc(sec("t1", `k: "abc"`)), baseErr: errors.New("boom"), body: doc(sec("t1", `k: "abc"`)), wantErrs: 1},
		{name: "base does not decode", base: "tenants: [\n", body: doc(sec("t1", `k: "abc"`)), wantErrs: 1},
		{name: "base refused by parse-tree", base: doc(sec("t1", `k: "abc"`)) + "---\n", body: doc(sec("t1", `k: "abc"`)), wantErrs: 1},
		// Every tenant of the file, each against its own section.
		{name: "per tenant", base: doc(sec("t1", `k: "abc"`), sec("t2", `k: "abc"`)),
			body: doc(sec("t1", `k: "abc"`), sec("t2", `k: "xyz"`)), wantErrs: 1, wantNote: 1},
		{name: "other tenant's value is not this one's", base: doc(sec("t2", `k: "abc"`)),
			body: doc(sec("t1", `k: "abc"`)), wantErrs: 1},
		// A null writes nothing (#2518).
		{name: "null", base: "", body: doc(sec("t1", `k: null`))},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			_, tcfg := mustShape(t, c.body)
			errs, notices := refusedThresholdValues(c.body, tcfg, []byte(c.base), c.baseErr)
			if len(errs) != c.wantErrs || len(notices) != c.wantNote {
				t.Fatalf("errs %q notices %q, want %d errs %d notices", errs, notices, c.wantErrs, c.wantNote)
			}
			for _, e := range errs {
				if !strings.Contains(e, refusedText) {
					t.Errorf("err %q", e)
				}
			}
			for _, n := range notices {
				if !strings.HasPrefix(n, "NOTICE: tenant=t1: k ") || !strings.Contains(n, "does not block a write") {
					t.Errorf("notice %q", n)
				}
			}
		})
	}
}

// Through the write gate (validate): the unchanged value is a notice, a
// change is a refusal, and #3140's non-finite check is not ratcheted — a
// NaN the file already holds still refuses a PUT that re-sends it.
func TestValidate_RefusedThresholdRatchet(t *testing.T) {
	dir := seedTreeRepo(t, map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\n  mysql_slow_queries: 5\n",
		"tx.yaml":        "tenants:\n  tx:\n    mysql_connections: \"abc\"\n    mysql_slow_queries: \"NaN\"\n",
	})
	w := newTestWriter(dir, dir)
	run := func(body string) ([]string, []string) {
		errs, notices, err := previewDryRun(context.Background(), w, "tx", body)
		if err != nil {
			t.Fatalf("DryRunValidate: %v", err)
		}
		return errs, notices
	}
	errs, notices := run("tenants:\n  tx:\n    mysql_connections: abc\n    mysql_slow_queries: \"6\"\n")
	if len(errs) != 0 || !strings.Contains(strings.Join(notices, "\n"), "NOTICE: tenant=tx: mysql_connections \"abc\"") {
		t.Errorf("unchanged: errs %q notices %q", errs, notices)
	}
	errs, _ = run("tenants:\n  tx:\n    mysql_connections: \"abd\"\n    mysql_slow_queries: \"6\"\n")
	if !strings.Contains(strings.Join(errs, "\n"), refusedText) {
		t.Errorf("changed: errs %q", errs)
	}
	errs, _ = run("tenants:\n  tx:\n    mysql_connections: \"70\"\n    mysql_slow_queries: \"NaN\"\n")
	if !strings.Contains(strings.Join(errs, "\n"), "is not a finite number") {
		t.Errorf("unchanged NaN: errs %q, want the #3140 refusal", errs)
	}
}

// mustShape is the write gate's plain decode of body.
func mustShape(t *testing.T, body string) (string, cfg.ThresholdConfig) {
	t.Helper()
	var tcfg cfg.ThresholdConfig
	if err := yaml.Unmarshal([]byte(body), &tcfg); err != nil {
		t.Fatalf("decode %q: %v", body, err)
	}
	return body, tcfg
}
