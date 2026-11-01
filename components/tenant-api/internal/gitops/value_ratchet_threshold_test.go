package gitops

// #3267 (ADR-038 decision 12): a threshold value the exporter refuses is a
// value finding (cfg.ValidateTenantKeys) and goes through the same ratchet
// as a reserved value (ratchetValues): a refusal when this write puts it
// there, a notice when the file already holds it as the exporter reads it —
// compared by the exporter's reading, not the text; strict whenever the file
// before the write is not known.

import (
	"context"
	"errors"
	"strings"
	"testing"

	cfg "github.com/vencil/threshold-exporter/pkg/config"
	"gopkg.in/yaml.v3"
)

const refusedText = "is not a threshold the exporter reads"

// thresholdGate is the write gate's key-check half for body over base, as
// validateReplacingOver runs it: keyChecks (the exporter's decode of the
// body), then ratchetValues (the same decode of the base).
func thresholdGate(t *testing.T, body, base string, baseErr error) (errs, notices []string) {
	t.Helper()
	var tcfg cfg.ThresholdConfig
	if err := yaml.Unmarshal([]byte(body), &tcfg); err != nil {
		t.Fatalf("decode %q: %v", body, err)
	}
	kv := ratchetValues(keyChecks("", body, tcfg), []byte(base), baseErr)
	// Only this check's messages: a fixture key outside any defaults also
	// draws other key checks (an `expires:` on it, say).
	for _, v := range kv.Tenants {
		for _, e := range v.Errors {
			if strings.Contains(e, refusedText) {
				errs = append(errs, e)
			}
		}
		for _, n := range v.Notices {
			if strings.Contains(n, refusedText) {
				notices = append(notices, n)
			}
		}
	}
	return errs, notices
}

func TestThresholdValueRatchet(t *testing.T) {
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
		// A null writes nothing (#2518); a written "" is refused.
		{name: "null", base: "", body: doc(sec("t1", `k: null`))},
		{name: "null schedule", base: "", body: doc(sec("t1", "k:\n      default: null"))},
		{name: "written empty", base: "", body: doc(sec("t1", `k: ""`)), wantErrs: 1},
		// A null on disk is no value (#2518): writing "" or a suffix over it
		// is a new value, not an unchanged one.
		{name: "base null, write empty", base: doc(sec("t1", `k: null`)), body: doc(sec("t1", `k: ""`)), wantErrs: 1},
		{name: "base null, write suffix", base: doc(sec("t1", `k: null`)), body: doc(sec("t1", `k: ":warning"`)), wantErrs: 1},
		{name: "base tilde, write empty", base: doc(sec("t1", `k: ~`)), body: doc(sec("t1", `k: ""`)), wantErrs: 1},
		{name: "base tilde, write suffix", base: doc(sec("t1", `k: ~`)), body: doc(sec("t1", `k: ":warning"`)), wantErrs: 1},
		{name: "base null schedule, write empty", base: doc(sec("t1", "k:\n      default: null")), body: doc(sec("t1", `k: ""`)), wantErrs: 1},
		{name: "base null schedule, write suffix", base: doc(sec("t1", "k:\n      default: null")), body: doc(sec("t1", `k: ":warning"`)), wantErrs: 1},
		// Dimensional keys are read in the exporter's canonical spelling: a
		// new value under any spelling is refused, the same row re-spelled is
		// unchanged.
		{name: "dimensional new, two labels", base: doc(sec("t1", `j: "70"`)), body: doc(sec("t1", `k{a="1",b="2"}: "abc"`)), wantErrs: 1},
		{name: "dimensional new, labels reordered", base: doc(sec("t1", `j: "70"`)), body: doc(sec("t1", `k{b="2",a="1"}: "70:high"`)), wantErrs: 1},
		{name: "dimensional new, spaced", base: doc(sec("t1", `j: "70"`)), body: doc(sec("t1", `k{ a="1" }: "abc"`)), wantErrs: 1},
		{name: "dimensional re-spelled is unchanged", base: doc(sec("t1", `k{b="2",a="1"}: "abc"`)),
			body: doc(sec("t1", `k{a="1", b="2"}: "abc"`)), wantNote: 1},
		{name: "dimensional spaced re-spelled is unchanged", base: doc(sec("t1", `k{ a="1" }: "abc"`)),
			body: doc(sec("t1", `k{a="1"}: "abc"`)), wantNote: 1},
		// A schedule is compared value by value and expiry by expiry.
		{name: "same window, other value", base: doc(sec("t1", "k:\n      default: \"abc\"\n      overrides:\n        - window: \"01:00-02:00\"\n          value: \"70\"")),
			body: doc(sec("t1", "k:\n      default: \"abc\"\n      overrides:\n        - window: \"01:00-02:00\"\n          value: \"71\"")), wantErrs: 1},
		{name: "other expires", base: doc(sec("t1", "k:\n      default: \"abc\"\n      expires: \"2030-01-01T00:00:00Z\"")),
			body: doc(sec("t1", "k:\n      default: \"abc\"\n      expires: \"2031-01-01T00:00:00Z\"")), wantErrs: 1},
		{name: "same expires", base: doc(sec("t1", "k:\n      default: \"abc\"\n      expires: \"2030-01-01T00:00:00Z\"")),
			body: doc(sec("t1", "k:\n      default: \"abc\"\n      expires: \"2030-01-01T00:00:00Z\"")), wantNote: 1},
		// #3140 stays out of the ratchet: not a value finding at all.
		{name: "non-finite is not this check's", base: doc(sec("t1", `k: "NaN"`)), body: doc(sec("t1", `k: "NaN"`))},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			errs, notices := thresholdGate(t, c.body, c.base, c.baseErr)
			if len(errs) != c.wantErrs || len(notices) != c.wantNote {
				t.Fatalf("errs %q notices %q, want %d errs %d notices", errs, notices, c.wantErrs, c.wantNote)
			}
			for _, e := range errs {
				if !strings.Contains(e, refusedText) {
					t.Errorf("err %q", e)
				}
			}
			for _, n := range notices {
				if !strings.Contains(n, refusedText) || !strings.Contains(n, "does not block a write") {
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
