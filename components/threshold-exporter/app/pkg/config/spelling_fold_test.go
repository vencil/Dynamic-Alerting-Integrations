package config

import (
	"fmt"
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

// #4253: a tenant writing both spellings. The line the tenant layer takes is
// a threshold-shaped one: beside a value no reader takes (`abc`) the other
// spelling's value is the tenant's own — not the root's 30 — and a `disable`
// is a line it takes (canonical wins, the threshold is off).
func TestSpellingFold_TenantTwinIsTheLineTheTenantTakes(t *testing.T) {
	t.Parallel()
	for _, c := range []struct{ current, want string }{
		{`"abc"`, "mysql_threads_running warning 7"},
		{`"9"`, "mysql_threads_running warning 9"},
		{`"disable"`, ""},
	} {
		cfg := loadTree(t, map[string]string{
			"_defaults.yaml": "defaults:\n  mysql_threads_running: 30\n",
			"t.yaml":         "tenants:\n  tx:\n    mysql_threads_running: " + c.current + "\n    " + retiredSpelling + ": \"7\"\n",
		})
		if got := threadsRows(cfg); got != c.want {
			t.Errorf("tenant %s beside %s 7: %q, want %q", c.current, retiredSpelling, got, c.want)
		}
	}
}

// #4253 with #4263: the canonical line wins the fold but is time-boxed; once
// its `expires:` passes it is as deleted, and the retired spelling's line —
// which the tenant layer takes — is what the tenant then writes.
func TestSpellingFold_AnExpiredCanonicalLineGivesWayToTheOtherSpelling(t *testing.T) {
	t.Parallel()
	files := map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_threads_running: 30\n",
		"t.yaml": "tenants:\n  tx:\n    mysql_threads_running:\n      default: \"9\"\n      expires: \"2026-06-01T00:00:00Z\"\n" +
			"    " + retiredSpelling + ": \"7\"\n",
	}
	before := loadTreeAt(t, files, fixedNow.AddDate(-1, 0, 0))
	var rows []string
	for _, r := range offRows(before, "tx", "mysql", fixedNow.AddDate(-1, 0, 0)) {
		if strings.HasPrefix(r, "mysql_threads_running") {
			rows = append(rows, r)
		}
	}
	if got := strings.Join(rows, "|"); got != "mysql_threads_running warning 9" {
		t.Errorf("before expires: %q, want the canonical line's 9", got)
	}
	if got := threadsRows(loadTreeAt(t, files, fixedNow)); got != "mysql_threads_running warning 7" {
		t.Errorf("after expires: %q, want the other spelling's 7", got)
	}
	// The tenant-api merge reads the wall clock — past the June expires:.
	m := MergeTenantWithRootDefaults(writeUndeliverableTree(t, files), "tx", []byte(files["t.yaml"]))
	if got := threadsRows(&m.ThresholdConfig); got != "mysql_threads_running warning 7" {
		t.Errorf("tenant-api after expires: %q, want /metrics' 7", got)
	}
}

// #4253: the decode folds the retired spelling, and what was written is
// still told — the deprecated-keys count and the rename notices.
func TestSpellingFold_WhatWasWrittenIsStillTold(t *testing.T) {
	t.Parallel()
	cfg := loadTree(t, map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_threads_running: 30\n",
		"t.yaml": "tenants:\n  tx:\n    " + retiredSpelling + ": \"7\"\n" +
			"  ty:\n    mysql_threads_running: \"abc\"\n    " + retiredSpelling + ": \"7\"\n",
	})
	if _, st := cfg.ResolveAtWithStats(fixedNow); st.PerTenantDeprecatedKeys["tx"] != 1 || st.PerTenantDeprecatedKeys["ty"] != 1 {
		got := st.PerTenantDeprecatedKeys
		t.Errorf("deprecated keys %v, want 1 for tx and ty", got)
	}
	notices := strings.Join(cfg.ValidateTenantKeys().Notices, "\n")
	for _, want := range []string{
		`tenant=tx: key "` + retiredSpelling + `" was renamed to "mysql_threads_running"`,
		`tenant=ty: "mysql_threads_running" has no value this layer takes, so the deprecated "` + retiredSpelling + `" beside it is served`,
	} {
		if !strings.Contains(notices, want) {
			t.Errorf("notices lack %q:\n%s", want, notices)
		}
	}
}

// #4253: /effective folds the root `_defaults.yaml` by the root's own reading
// (rootTakesRaw), as /metrics does — `"off"` is a value the root refuses,
// though a subtree level would take it as its `disable`.
func TestSpellingFold_EffectiveReadsTheRootAsTheRootDecodeDoes(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeMergeTree(t, dir, map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_threads_running: \"off\"\n  " + retiredSpelling + ": 7\n",
		"tx.yaml":        "tenants:\n  tx:\n    _metadata: {owner: x}\n",
	})
	ec, err := ResolveEffective(dir, "tx", fixedNow)
	if err != nil {
		t.Fatal(err)
	}
	if got := ec.EffectiveConfig["mysql_threads_running"]; fmt.Sprint(got) != "7" {
		t.Errorf("/effective mysql_threads_running = %v, want the root's other line, 7 (as /metrics)", got)
	}
}

// #4253 review F1: merged_hash is the merge's, the same on every path that
// computes it — /effective's and the exporter's (ComputeMergedHash) — for a
// root writing a twin the root and a subtree level read differently.
func TestSpellingFold_MergedHashIsOneOnEveryPath(t *testing.T) {
	t.Parallel()
	root := "defaults:\n  mysql_threads_running: \"off\"\n  " + retiredSpelling + ": 7\n"
	tenant := "tenants:\n  tx:\n    _metadata: {owner: x}\n"
	dir := t.TempDir()
	writeMergeTree(t, dir, map[string]string{"_defaults.yaml": root, "tx.yaml": tenant})
	ec, err := ResolveEffective(dir, "tx", fixedNow)
	if err != nil {
		t.Fatal(err)
	}
	merge, err := ComputeMergedHash([]byte(tenant), "tx", [][]byte{[]byte(root)})
	if err != nil {
		t.Fatal(err)
	}
	if ec.MergedHash != merge {
		t.Errorf("/effective merged_hash %s, ComputeMergedHash %s: one merge, one hash", ec.MergedHash, merge)
	}
}

// #4253 review F2: a tenant writing both spellings, the canonical line kept,
// still writes the retired one — it is counted, as the notice names it.
func TestSpellingFold_AFoldedRetiredLineIsStillCounted(t *testing.T) {
	t.Parallel()
	cfg := loadTree(t, map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_threads_running: 30\n",
		"t.yaml":         "tenants:\n  tx:\n    " + retiredSpelling + ": \"7\"\n    mysql_threads_running: \"9\"\n",
	})
	if _, st := cfg.ResolveAtWithStats(fixedNow); st.PerTenantDeprecatedKeys["tx"] != 1 {
		t.Errorf("deprecated keys %v, want tx 1", st.PerTenantDeprecatedKeys)
	}
}

// #4253 review F3: a null is no line (#2418) — beside the other spelling it
// is no duplicate, and a redundant override beside it is still judged.
func TestSpellingFold_ANullOtherSpellingIsNoDuplicate(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeMergeTree(t, dir, map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_threads_running: 30\n",
		"sub/tx.yaml":    "tenants:\n  tx:\n    mysql_threads_running: \"30\"\n    " + retiredSpelling + ": null\n",
	})
	ec, err := ResolveEffective(dir, "tx", fixedNow)
	if err != nil {
		t.Fatal(err)
	}
	if len(ec.NotServed) != 0 {
		t.Errorf("not_served %v, want none: a null writes nothing", ec.NotServed)
	}
	if _, judged := ec.JudgeableTenantOverrides()["mysql_threads_running"]; !judged {
		t.Errorf("judgeable overrides %v lack mysql_threads_running", ec.JudgeableTenantOverrides())
	}
}
