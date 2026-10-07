package main

// effective_test.go — `da-guard effective` (#2564): the Go half of
// tests/shared/effective_values_matrix.json, the guard that every tenant
// entry is ResolveEffective's (tenant-api /effective's) own answer, and the
// exit-code contract.

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"runtime"
	"sort"
	"strings"
	"testing"

	"github.com/vencil/threshold-exporter/pkg/config"
)

type effectiveMatrixExpect struct {
	Value      float64          `json:"value"`
	Source     config.KeySource `json:"source"`
	Profile    *string          `json:"profile"`
	MergedHash string           `json:"merged_hash"`
}

type effectiveMatrixTree struct {
	Name   string                           `json:"name"`
	Files  map[string]string                `json:"files"`
	Expect map[string]effectiveMatrixExpect `json:"expect"`
}

func loadEffectiveMatrix(t *testing.T) []effectiveMatrixTree {
	t.Helper()
	_, thisFile, _, _ := runtime.Caller(0)
	path := filepath.Join(filepath.Dir(thisFile), "..", "..", "..", "..", "..",
		"tests", "shared", "effective_values_matrix.json")
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read matrix: %v", err)
	}
	var m struct {
		Comment []string              `json:"_comment"`
		Trees   []effectiveMatrixTree `json:"trees"`
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&m); err != nil {
		t.Fatalf("parse matrix: %v", err)
	}
	if len(m.Trees) == 0 {
		t.Fatal("matrix has no trees — a vacuous table passes nothing")
	}
	return m.Trees
}

// effectiveOut is the document as a reader decodes it: each tenant's body
// kept raw, so it can be compared byte for byte with /effective's.
type effectiveOut struct {
	Schema      string                     `json:"schema"`
	ParseFailed []string                   `json:"parse_failed"`
	Tenants     map[string]json.RawMessage `json:"tenants"`
}

type effectiveExtra struct {
	Profile    *string                     `json:"profile"`
	KeySources map[string]config.KeySource `json:"key_sources"`
}

func runEffectiveOn(t *testing.T, dir string) (int, effectiveOut, string) {
	t.Helper()
	code, stdout, stderr := runOnce(t, effectiveCmd, "--config-dir", dir)
	var doc effectiveOut
	if stdout != "" {
		if err := json.Unmarshal([]byte(stdout), &doc); err != nil {
			t.Fatalf("stdout is not JSON: %v\n%s", err, stdout)
		}
	}
	return code, doc, stderr
}

// assertMatchesResolveEffective: every tenant entry, minus the two fields
// /effective leaves out, is byte for byte what tenant-api writes for
// ResolveEffective(dir, id); `profile` is its BoundProfile; `key_sources`
// names every key of the effective config and nothing else.
func assertMatchesResolveEffective(t *testing.T, dir string, doc effectiveOut) {
	t.Helper()
	if len(doc.Tenants) == 0 {
		t.Fatal("no tenants in the output — nothing compared")
	}
	for id, raw := range doc.Tenants {
		ec, err := config.ResolveEffective(dir, id)
		if err != nil {
			t.Fatalf("ResolveEffective(%q): %v", id, err)
		}
		body := *ec
		body.EffectiveConfig = config.NonFiniteAsText(ec.EffectiveConfig)
		want, err := json.Marshal(body)
		if err != nil {
			t.Fatal(err)
		}
		var got map[string]json.RawMessage
		if err := json.Unmarshal(raw, &got); err != nil {
			t.Fatal(err)
		}
		var extra effectiveExtra
		if err := json.Unmarshal(raw, &extra); err != nil {
			t.Fatal(err)
		}
		delete(got, "profile")
		delete(got, "key_sources")
		gotBody, err := json.Marshal(got) // map keys sorted, like want's re-encode below
		if err != nil {
			t.Fatal(err)
		}
		var wantMap map[string]json.RawMessage
		if err := json.Unmarshal(want, &wantMap); err != nil {
			t.Fatal(err)
		}
		wantBody, _ := json.Marshal(wantMap)
		if !bytes.Equal(gotBody, wantBody) {
			t.Errorf("tenant %q: entry differs from /effective's body\n got %s\nwant %s", id, gotBody, wantBody)
		}

		wantProfile := ec.BoundProfile
		gotProfile := ""
		if extra.Profile != nil {
			gotProfile = *extra.Profile
			if gotProfile == "" {
				t.Errorf("tenant %q: profile is \"\", want null for none", id)
			}
		}
		if gotProfile != wantProfile {
			t.Errorf("tenant %q: profile = %q, want BoundProfile %q", id, gotProfile, wantProfile)
		}

		keys := keysOf(ec.EffectiveConfig)
		srcKeys := keysOf(extra.KeySources)
		sort.Strings(keys)
		sort.Strings(srcKeys)
		if !reflect.DeepEqual(keys, srcKeys) {
			t.Errorf("tenant %q: key_sources keys %v, effective_config keys %v", id, srcKeys, keys)
		}
	}
}

// TestEffective_Matrix is the Go half of effective_values_matrix.json.
func TestEffective_Matrix(t *testing.T) {
	t.Parallel()
	for _, tree := range loadEffectiveMatrix(t) {
		t.Run(tree.Name, func(t *testing.T) {
			t.Parallel()
			dir := writeParityTree(t, tree.Files)
			code, doc, stderr := runEffectiveOn(t, dir)
			mustOK(t, code, stderr)
			if doc.Schema != effectiveSchema {
				t.Errorf("schema = %q, want %q", doc.Schema, effectiveSchema)
			}
			assertMatchesResolveEffective(t, dir, doc)
			if got, want := keysOf(doc.Tenants), keysOf(tree.Expect); len(got) != len(want) {
				t.Errorf("tenants %v, table expects %v", got, want)
			}
			for id, exp := range tree.Expect {
				raw, ok := doc.Tenants[id]
				if !ok {
					t.Fatalf("tenant %q missing", id)
				}
				var got struct {
					MergedHash      string                      `json:"merged_hash"`
					EffectiveConfig map[string]any              `json:"effective_config"`
					Profile         *string                     `json:"profile"`
					KeySources      map[string]config.KeySource `json:"key_sources"`
				}
				if err := json.Unmarshal(raw, &got); err != nil {
					t.Fatal(err)
				}
				if v := got.EffectiveConfig["mysql_connections"]; v != exp.Value {
					t.Errorf("%s: value = %v, want %v", id, v, exp.Value)
				}
				if src := got.KeySources["mysql_connections"]; !reflect.DeepEqual(src, exp.Source) {
					t.Errorf("%s: source = %+v (level %v), want %+v (level %v)", id, src, deref(src.Level), exp.Source, deref(exp.Source.Level))
				}
				if !reflect.DeepEqual(got.Profile, exp.Profile) {
					t.Errorf("%s: profile = %v, want %v", id, deref(got.Profile), deref(exp.Profile))
				}
				if got.MergedHash != exp.MergedHash {
					t.Errorf("%s: merged_hash = %s, want %s", id, got.MergedHash, exp.MergedHash)
				}
			}
		})
	}
}

func deref[T any](p *T) any {
	if p == nil {
		return nil
	}
	return *p
}

// TestEffective_MatchesResolveEffective runs the served-values consistency
// tree — defaults, platform `tenants:`, profiles, subtree defaults,
// schedules, aliases, reserved keys — plus shapes the attribution has to get
// right, through the subcommand, and requires every entry to be /effective's.
func TestEffective_MatchesResolveEffective(t *testing.T) {
	t.Parallel()
	files := map[string]string{}
	for k, v := range consistencyTree {
		files[k] = v
	}
	// A platform entry in a retired spelling; a null threshold over the
	// chain; a mapping merged across the chain and the tenant file; an
	// unknown profile (bound to nothing); a profile bound but filling
	// nothing (every key already set).
	files["_platform.yaml"] = "tenants:\n  tenant-d:\n    mysql_cpu: 41\n"
	files["tenant-d.yaml"] = "tenants:\n  tenant-d:\n    mysql_connections: null\n    _profile: nope\n" +
		"    _state_maintenance:\n      expires: \"2030-01-01T00:00:00Z\"\n"
	files["tenant-e.yaml"] = "tenants:\n  tenant-e:\n    _profile: gold\n    redis_memory: 1\n    container_cpu: 2\n"
	dir := writeParityTree(t, files)
	code, doc, stderr := runEffectiveOn(t, dir)
	mustOK(t, code, stderr)
	assertMatchesResolveEffective(t, dir, doc)

	var d, e effectiveExtra
	if err := json.Unmarshal(doc.Tenants["tenant-d"], &d); err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(doc.Tenants["tenant-e"], &e); err != nil {
		t.Fatal(err)
	}
	if got := d.KeySources["mysql_cpu"]; got.Layer != config.KeyLayerPlatform || got.File != "_platform.yaml" {
		t.Errorf("tenant-d mysql_cpu source = %+v, want platform _platform.yaml", got)
	}
	if got := d.KeySources["mysql_connections"]; got.Layer != config.KeyLayerDefaults || got.File != "_defaults.yaml" {
		t.Errorf("tenant-d mysql_connections (null in the tenant file) source = %+v, want defaults _defaults.yaml", got)
	}
	if d.Profile != nil {
		t.Errorf("tenant-d profile = %q, want null (no such profile)", *d.Profile)
	}
	if e.Profile == nil || *e.Profile != "gold" {
		t.Errorf("tenant-e profile = %v, want gold", deref(e.Profile))
	}
}

func TestEffective_ParseFailedFile_ExitsThreeAndNamesIt(t *testing.T) {
	t.Parallel()
	dir := writeParityTree(t, map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
		"tenant-a.yaml":  "tenants:\n  tenant-a:\n    mysql_connections: 70\n",
		"tenant-b.yaml":  "tenants:\n  tenant-b: [1]\n",
	})
	code, doc, stderr := runEffectiveOn(t, dir)
	if code != exitParseFailed {
		t.Fatalf("exit = %d, want %d; stderr=%q", code, exitParseFailed, stderr)
	}
	if !reflect.DeepEqual(doc.ParseFailed, []string{"tenant-b.yaml"}) {
		t.Errorf("parse_failed = %v, want [tenant-b.yaml]", doc.ParseFailed)
	}
	if _, ok := doc.Tenants["tenant-a"]; !ok {
		t.Errorf("tenant-a missing: the JSON is still written for the rest of the tree")
	}
	if !strings.Contains(stderr, "tenant-b.yaml") {
		t.Errorf("stderr does not name the file: %q", stderr)
	}
}

// TestEffective_ParseFailedIsServedValuesVerdict: a file the exporter's load
// drops is named by effective exactly as by served-values (one verdict, the
// load's FlatBuild.ParseFailed), with exit 3 — including a root
// `_defaults.yaml` the walker's lenient chain parse still reads, so
// /effective alone would report values /metrics does not serve.
func TestEffective_ParseFailedIsServedValuesVerdict(t *testing.T) {
	t.Parallel()
	tenants := "tenants:\n  t-x:\n    mysql_connections: \"90\"\n  t-y:\n    _silent_mode: critical\n"
	for name, files := range map[string]map[string]string{
		"reserved key in root defaults":       {"_defaults.yaml": "defaults:\n  mysql_connections: 80\n  _silent_mode: warning\n", "tenants.yaml": tenants},
		"quoted number in root defaults":      {"_defaults.yaml": "defaults:\n  mysql_connections: \"80\"\n", "tenants.yaml": tenants},
		"_metadata in root defaults":          {"_defaults.yaml": "defaults:\n  mysql_connections: 80\n  _metadata:\n    owner: dba\n", "tenants.yaml": tenants},
		"disable in root defaults":            {"_defaults.yaml": "defaults:\n  mysql_connections: 80\n  container_memory: disable\n", "tenants.yaml": tenants},
		"scalar tenant body in root defaults": {"_defaults.yaml": "defaults:\n  mysql_connections: 80\ntenants:\n  t-x: 3\n", "tenants.yaml": tenants},
		"tenant file body a list":             {"_defaults.yaml": "defaults:\n  mysql_connections: 80\n", "tenants.yaml": tenants, "b.yaml": "tenants:\n  t-z: [1]\n"},
	} {
		dir := writeParityTree(t, files)
		code, doc, stderr := runEffectiveOn(t, dir)
		sCode, sOut, sErr := runOnce(t, servedValuesCmd, "--config-dir", dir)
		var served struct {
			ParseFailed []string `json:"parse_failed"`
		}
		if err := json.Unmarshal([]byte(sOut), &served); err != nil {
			t.Fatalf("%s: served-values stdout is not JSON (exit %d): %v; stderr=%q", name, sCode, err, sErr)
		}
		if sCode != exitParseFailed || len(served.ParseFailed) == 0 {
			t.Fatalf("%s: served-values exit %d parse_failed %v — the shape no longer exercises a dropped file", name, sCode, served.ParseFailed)
		}
		if code != exitParseFailed {
			t.Errorf("%s: effective exit = %d, want %d; stderr=%q", name, code, exitParseFailed, stderr)
		}
		if !reflect.DeepEqual(doc.ParseFailed, served.ParseFailed) {
			t.Errorf("%s: effective parse_failed %v, served-values %v", name, doc.ParseFailed, served.ParseFailed)
		}
	}
}

func TestEffective_ChainFileTheResolveRejects_ExitsThree(t *testing.T) {
	t.Parallel()
	dir := writeParityTree(t, map[string]string{
		"_defaults.yaml":     "defaults:\n  mysql_connections: 80\n",
		"sub/_defaults.yaml": "defaults: [\n",
		"sub/tenant-a.yaml":  "tenants:\n  tenant-a:\n    mysql_connections: 70\n",
	})
	code, doc, stderr := runEffectiveOn(t, dir)
	if code != exitParseFailed {
		t.Fatalf("exit = %d, want %d; stderr=%q", code, exitParseFailed, stderr)
	}
	if !reflect.DeepEqual(doc.ParseFailed, []string{"sub/_defaults.yaml"}) {
		t.Errorf("parse_failed = %v, want [sub/_defaults.yaml]", doc.ParseFailed)
	}
	if len(doc.Tenants) != 0 {
		t.Errorf("tenants = %v, want none: the resolve stopped", keysOf(doc.Tenants))
	}
}

func TestEffective_CleanTree_ParseFailedIsEmptyList(t *testing.T) {
	t.Parallel()
	dir := writeParityTree(t, map[string]string{"tenant-a.yaml": "tenants:\n  tenant-a:\n    a: 1\n"})
	code, stdout, stderr := runOnce(t, effectiveCmd, "--config-dir", dir)
	mustOK(t, code, stderr)
	if !strings.Contains(stdout, `"parse_failed": []`) {
		t.Errorf("parse_failed is not an empty list:\n%s", stdout)
	}
}

// #2115 R3: effective's `skipped` names the files the walk takes no tenant
// from — the same list, in the same words, as served-values' `skipped`, so a
// reader that needs only effective (validate-config's profiles row) loses
// nothing. Covers a tree with no tenant at all (the early return) and a
// [] (never null) on a tree with none.
func TestEffective_SkippedIsServedValuesSkipped(t *testing.T) {
	t.Parallel()
	for name, files := range map[string]map[string]string{
		"mixed": {
			"_defaults.yaml":      "defaults:\n  mysql_connections: 80\n",
			"tenant-a.yaml":       "tenants:\n  tenant-a:\n    mysql_connections: 70\n",
			"flat-t.yaml":         "mysql_connections: 5\n",
			"empty-wrapper.yaml":  "tenants: {}\n",
			"team/flat-u.yml":     "mysql_connections: 6\n",
			"team/_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
		},
		"no tenant at all": {
			"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
			"flat-t.yaml":    "mysql_connections: 5\n",
		},
		"none skipped": {
			"tenant-a.yaml": "tenants:\n  tenant-a:\n    mysql_connections: 70\n",
		},
	} {
		dir := writeParityTree(t, files)
		code, stdout, stderr := runOnce(t, effectiveCmd, "--config-dir", dir)
		mustOK(t, code, stderr)
		sCode, sOut, sErr := runOnce(t, servedValuesCmd, "--config-dir", dir)
		mustOK(t, sCode, sErr)
		type skipped struct {
			Skipped *[]skippedFile `json:"skipped"`
		}
		var eff, srv skipped
		if err := json.Unmarshal([]byte(stdout), &eff); err != nil {
			t.Fatalf("%s: effective stdout is not JSON: %v", name, err)
		}
		if err := json.Unmarshal([]byte(sOut), &srv); err != nil {
			t.Fatalf("%s: served-values stdout is not JSON: %v", name, err)
		}
		if eff.Skipped == nil || srv.Skipped == nil {
			t.Fatalf("%s: skipped missing or null (effective %v, served-values %v)", name, eff.Skipped, srv.Skipped)
		}
		if !reflect.DeepEqual(*eff.Skipped, *srv.Skipped) {
			t.Errorf("%s: effective skipped %v, served-values %v", name, *eff.Skipped, *srv.Skipped)
		}
		if name != "none skipped" && len(*eff.Skipped) == 0 {
			t.Errorf("%s: nothing skipped — the shape no longer exercises the field", name)
		}
	}
}

func TestEffective_DuplicateTenant_ExitsTwo(t *testing.T) {
	t.Parallel()
	dir := writeParityTree(t, map[string]string{
		"a.yaml": "tenants:\n  tenant-a:\n    a: 1\n",
		"b.yaml": "tenants:\n  tenant-a:\n    a: 2\n",
	})
	code, stdout, stderr := runOnce(t, effectiveCmd, "--config-dir", dir)
	if code != exitCallerErr {
		t.Fatalf("exit = %d, want %d; stderr=%q", code, exitCallerErr, stderr)
	}
	if stdout != "" {
		t.Errorf("stdout = %q, want nothing on exit 2", stdout)
	}
	if !strings.Contains(stderr, "duplicate tenant") {
		t.Errorf("stderr = %q, want it to name the duplicate", stderr)
	}
}

func TestEffective_CallerErrors(t *testing.T) {
	t.Parallel()
	for name, args := range map[string][]string{
		"no config-dir": {effectiveCmd},
		"stray arg":     {effectiveCmd, "--config-dir", t.TempDir(), "extra"},
		"unknown flag":  {effectiveCmd, "--config-dir", t.TempDir(), "--at", "2026-01-01T00:00:00Z"},
		"empty tree":    {effectiveCmd, "--config-dir", t.TempDir()},
		"missing dir":   {effectiveCmd, "--config-dir", filepath.Join(t.TempDir(), "nope")},
	} {
		code, stdout, stderr := runOnce(t, args...)
		if code != exitCallerErr {
			t.Errorf("%s: exit = %d, want %d; stderr=%q", name, code, exitCallerErr, stderr)
		}
		if stdout != "" {
			t.Errorf("%s: stdout = %q, want nothing on exit 2", name, stdout)
		}
	}
	if code, _, stderr := runOnce(t, effectiveCmd, "-h"); code != exitOK || !strings.Contains(stderr, "Usage: da-guard effective") {
		t.Errorf("-h: exit = %d, stderr = %q", code, stderr)
	}
}

func TestEffective_NonFiniteSentAsText(t *testing.T) {
	t.Parallel()
	dir := writeParityTree(t, map[string]string{
		"tenant-a.yaml": "tenants:\n  tenant-a:\n    a: .inf\n    b: .nan\n",
	})
	code, doc, stderr := runEffectiveOn(t, dir)
	mustOK(t, code, stderr)
	assertMatchesResolveEffective(t, dir, doc)
	var got struct {
		EffectiveConfig map[string]any `json:"effective_config"`
	}
	if err := json.Unmarshal(doc.Tenants["tenant-a"], &got); err != nil {
		t.Fatal(err)
	}
	if got.EffectiveConfig["a"] != "Infinity" || got.EffectiveConfig["b"] != "NaN" {
		t.Errorf("effective_config = %v, want a=Infinity b=NaN as text", got.EffectiveConfig)
	}
}
