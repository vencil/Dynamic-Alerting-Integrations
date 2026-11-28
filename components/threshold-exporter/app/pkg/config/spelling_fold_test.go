package config

import (
	"strings"
	"testing"
)

// retiredSpelling is the #1231 retired spelling of mysql_threads_running,
// built so no test line writes it as a live key.
const retiredSpelling = "mysql_" + "cpu"

// threadsRows is the mysql_threads_running rows tenant tx is served.
func threadsRows(cfg *ThresholdConfig) string {
	var out []string
	for _, r := range offRows(cfg, "tx", "mysql", fixedNow) {
		if strings.HasPrefix(r, "mysql_threads_running") {
			out = append(out, r)
		}
	}
	return strings.Join(out, "|")
}

// #4253: one root `_defaults.yaml` writing both spellings of a threshold.
// The line the root refuses is a line it does not take, whatever the value
// looks like — so the other spelling's value is served; with both taken (or
// neither) the canonical one wins.
func TestSpellingFold_RootTwinIsTheLineTheRootTakes(t *testing.T) {
	t.Parallel()
	for _, c := range []struct{ current, want string }{
		{"abc", "mysql_threads_running warning 7"},
		{".nan", "mysql_threads_running warning 7"},
		{".inf", "mysql_threads_running warning 7"},
		{`"off"`, "mysql_threads_running warning 7"},
		{"0x10", "mysql_threads_running warning 7"},
		{"9", "mysql_threads_running warning 9"},
	} {
		cfg := loadTree(t, map[string]string{
			"_defaults.yaml": "defaults:\n  mysql_threads_running: " + c.current + "\n  " + retiredSpelling + ": 7\n",
			"t.yaml":         "tenants:\n  tx:\n    _metadata: {owner: x}\n",
		})
		if got := threadsRows(cfg); got != c.want {
			t.Errorf("root %s beside %s: 7 served %q, want %q", c.current, retiredSpelling, got, c.want)
		}
	}
}

// #4253 A1: a refused line with no other spelling beside it stays refused —
// nothing is served for it, as before.
func TestSpellingFold_ARefusedRootLineAloneServesNothing(t *testing.T) {
	t.Parallel()
	cfg := loadTree(t, map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_threads_running: abc\n",
		"t.yaml":         "tenants:\n  tx:\n    _metadata: {owner: x}\n",
	})
	if got := threadsRows(cfg); got != "" {
		t.Errorf("root abc alone: %q, want nothing served", got)
	}
}

// #4253: the root decode folds the retired spelling into the canonical key —
// no reader downstream sees it — and records the spelling it kept.
func TestSpellingFold_RootDecodeHoldsOnlyTheCanonicalKey(t *testing.T) {
	t.Parallel()
	for _, c := range []struct{ body, kept string }{
		{"defaults:\n  " + retiredSpelling + ": 7\n", retiredSpelling},
		{"defaults:\n  mysql_threads_running: \"off\"\n  " + retiredSpelling + ": 7\n", retiredSpelling},
		{"defaults:\n  mysql_threads_running: 9\n  " + retiredSpelling + ": 7\n", "mysql_threads_running"},
	} {
		cfg, err := ParseConfigFile([]byte(c.body))
		if err != nil {
			t.Fatal(err)
		}
		if _, held := cfg.Defaults[retiredSpelling]; held {
			t.Errorf("%q: Defaults still holds %s", c.body, retiredSpelling)
		}
		if _, held := cfg.Defaults["mysql_threads_running"]; !held {
			t.Errorf("%q: Defaults lacks the canonical key", c.body)
		}
		if got := RootSpellingKept([]byte(c.body), "mysql_threads_running"); got != c.kept {
			t.Errorf("%q: RootSpellingKept = %q, want %q", c.body, got, c.kept)
		}
	}
}
