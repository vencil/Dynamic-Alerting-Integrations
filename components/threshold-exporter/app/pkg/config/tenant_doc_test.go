package config

// #2153 — a tenant file is parsed once (ParseTenantDoc) and shared by every
// tenant it declares. These pin that the sharing is invisible: each tenant
// gets exactly what a parse of its own gave (result, errors, error types),
// and no tenant's merge — nor a caller writing into what it got back — can
// reach another merge taken from the same parse.

import (
	"errors"
	"fmt"
	"reflect"
	"sort"
	"strings"
	"testing"
)

// tenantDocTrees are the shapes one shared parse must survive: several
// tenants per file (empty body, nested maps, a scheduled value, `_metadata`,
// a reserved-key null), a defaults chain, the root platform files' per-tenant
// blocks (#2019) and profiles (#2117), and several files per tree.
func tenantDocTrees() map[string]map[string]string {
	return map[string]map[string]string{
		"one-file-many-tenants": {
			"_defaults.yaml": "defaults:\n  cpu: 80\n  _routing:\n    receiver: root\n    group_by: [a, b]\n",
			"team/all.yaml": "tenants:\n" +
				"  t-1:\n    cpu: \"10\"\n    _routing:\n      receiver: own\n" +
				"  t-2:\n" +
				"  t-3:\n    mem:\n      default: \"90\"\n      overrides:\n        - window: \"22:00-06:00\"\n          value: \"95\"\n" +
				"  t-4:\n    _routing: null\n    _metadata:\n      owner: x\n" +
				"  t-5:\n    cpu: \"50\"\n",
		},
		"chain-many-files": {
			"_defaults.yaml":         "defaults:\n  cpu: 80\n  mem: 85\n",
			"a/_defaults.yaml":       "defaults:\n  cpu: 70\n  _metadata:\n    owner: a\n",
			"a/b/_defaults.yaml":     "defaults:\n  disk: 60\n",
			"a/b/pair.yaml":          "tenants:\n  t-1:\n    cpu: \"1\"\n  t-2:\n    disk: \"2\"\n",
			"a/trio.yaml":            "tenants:\n  t-3:\n  t-4:\n    mem: \"4\"\n  t-5:\n    cpu: \"5\"\n",
			"root-tenants.yaml":      "tenants:\n  t-6:\n    cpu: \"6\"\n  t-7:\n",
			"a/b/c/_defaults.yaml":   "defaults:\n  cpu: 11\n",
			"a/b/c/one-tenant.yaml":  "tenants:\n  t-8:\n    mem: \"8\"\n",
			"a/b/c/another-one.yaml": "tenants:\n  t-9:\n",
		},
		"platform-and-profiles": {
			"_defaults.yaml": "defaults:\n  cpu: 80\n  mem: 85\n" +
				"tenants:\n  t-1:\n    cpu: \"61\"\n    _silent_mode: warning\n  t-3:\n    mem: \"63\"\n",
			"_profiles.yaml": "profiles:\n  gold:\n    cpu: 40\n    disk: 41\n  bronze:\n    mem: 99\n",
			"all.yaml": "tenants:\n" +
				"  t-1:\n    _profile: gold\n    mem: \"1\"\n" +
				"  t-2:\n    _profile: bronze\n" +
				"  t-3:\n    _profile: gold\n    disk: \"3\"\n" +
				"  t-4:\n",
		},
	}
}

// TestScopeEffectiveSharedParseMatchesPerTenantResolve: ScopeEffective
// resolves every tenant through ONE resolver — one parse per tenant file,
// shared — while ResolveEffective builds a resolver (a fresh parse) per call.
// Every field of every tenant's EffectiveConfig must agree, including the
// guard-only TenantOverridesRaw / MergedDefaults.
func TestScopeEffectiveSharedParseMatchesPerTenantResolve(t *testing.T) {
	t.Parallel()
	for name, files := range tenantDocTrees() {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			dir := writePlatformTree(t, files)
			scoped, err := ScopeEffective(dir, dir)
			if err != nil {
				t.Fatal(err)
			}
			if len(scoped.Tenants) < 4 {
				t.Fatalf("only %d tenants in scope — the comparison would be near-vacuous", len(scoped.Tenants))
			}
			tree, err := EffectiveTree(dir)
			if err != nil {
				t.Fatal(err)
			}
			whole := map[string]*EffectiveConfig{}
			for _, ec := range tree.Tenants {
				whole[ec.TenantID] = ec
			}
			for _, got := range scoped.Tenants {
				want, err := ResolveEffective(dir, got.TenantID)
				if err != nil {
					t.Fatalf("ResolveEffective(%s): %v", got.TenantID, err)
				}
				// #2296: the not-served tables are ResolveEffective's and
				// EffectiveTree's; #2065: the gate (ScopeEffective) names the
				// same keys, but reads no dropped chain file as empty, so
				// ChainParseFailed is theirs alone — compared against the
				// whole-tree answer, then left out below.
				if w := whole[got.TenantID]; w == nil || !reflect.DeepEqual(w.NotServed, want.NotServed) ||
					!reflect.DeepEqual(w.ChainParseFailed, want.ChainParseFailed) {
					t.Errorf("tenant %s: EffectiveTree and ResolveEffective name different not-served keys", got.TenantID)
				}
				want.ChainParseFailed = nil
				if !reflect.DeepEqual(got, want) {
					t.Errorf("tenant %s: shared parse\n %+v\nper-tenant parse\n %+v", got.TenantID, got, want)
				}
			}
		})
	}
}

// TestEffectiveResolverParsesEachTenantFileOnce is the optimisation itself,
// which the equality test above cannot see: resolving every tenant of the
// tree through one resolver parses each tenant file exactly once.
func TestEffectiveResolverParsesEachTenantFileOnce(t *testing.T) {
	t.Parallel()
	dir := writePlatformTree(t, tenantDocTrees()["chain-many-files"])
	scan, err := ScanDirTree(dir, nil, nil, discardLogger)
	if err != nil {
		t.Fatal(err)
	}
	r := newEffectiveResolver(scan)
	parses := map[string]int{}
	r.onTenantParse = func(p string) { parses[p]++ }
	ids := make([]string, 0, len(scan.Tenants))
	for id := range scan.Tenants {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	for _, id := range ids {
		if _, err := r.resolve(id); err != nil {
			t.Fatalf("resolve %s: %v", id, err)
		}
	}
	files := map[string]struct{}{}
	for _, p := range scan.Tenants {
		files[p] = struct{}{}
	}
	if len(ids) <= len(files) {
		t.Fatalf("%d tenants over %d files — no file declares two, the test would not see a re-parse", len(ids), len(files))
	}
	if len(parses) != len(files) {
		t.Errorf("parsed %d tenant files, want every one of the %d", len(parses), len(files))
	}
	for p, n := range parses {
		if n != 1 {
			t.Errorf("%s parsed %d times, want once", p, n)
		}
	}
}

// TestTenantDocIsolatesTenantsSharingOneParse is the isolation guarantee
// (TenantDoc: a deep copy of the tenant's block per merge). A caller that
// writes into what one merge returned — tenantRaw is exported as
// EffectiveConfig.TenantOverridesRaw — must not change any later merge taken
// from the same parse, of that tenant or another.
func TestTenantDocIsolatesTenantsSharingOneParse(t *testing.T) {
	t.Parallel()
	b := []byte("tenants:\n" +
		"  t-1:\n    cpu: \"10\"\n    _routing:\n      receiver: own\n      group_by: [a, b]\n" +
		"  t-2:\n    cpu: \"20\"\n")
	chain := [][]byte{[]byte("defaults:\n  cpu: 80\n  _routing:\n    receiver: root\n")}
	doc := ParseTenantDoc(b)

	for _, tid := range []string{"t-1", "t-2"} {
		first, err := computeEffectiveConfigDocDetailed(doc, tid, chain, nil, nil)
		if err != nil {
			t.Fatal(err)
		}
		// Write into everything the caller got that came from the file.
		first.tenantRaw["cpu"] = "999"
		first.tenantRaw["injected"] = true
		if r, ok := first.tenantRaw["_routing"].(map[string]any); ok {
			r["receiver"] = "hijacked"
			r["group_by"].([]any)[0] = "hijacked"
		}
		first.merged["cpu"] = "888"

		for _, other := range []string{"t-1", "t-2"} {
			got, err := computeEffectiveConfigDocDetailed(doc, other, chain, nil, nil)
			if err != nil {
				t.Fatal(err)
			}
			want, err := computeEffectiveConfigBytesDetailed(b, other, chain, nil, nil)
			if err != nil {
				t.Fatal(err)
			}
			if !reflect.DeepEqual(got, want) {
				t.Errorf("after writing into %s's result, %s from the shared parse\n %+v\nwant (own parse)\n %+v", tid, other, got, want)
			}
		}
	}
}

// TestTenantDocKeepsEachTenantsErrors: every tenant merged from one shared
// parse gets the error — text, type, and position in the merge — its own
// parse gave: a syntax error as its own *tenantParseError (after the chain's
// parse errors, which still win), an absent or non-mapping body as the
// extract error for that tenant only.
func TestTenantDocKeepsEachTenantsErrors(t *testing.T) {
	t.Parallel()
	good := []byte("defaults:\n  cpu: 80\n")
	bad := []byte("defaults:\n  cpu: {unclosed\n")
	cases := []struct {
		name   string
		tenant string
		chain  [][]byte
		tids   []string
	}{
		{"broken-file", "tenants:\n  t-1: [unclosed\n  t-2:\n", [][]byte{good}, []string{"t-1", "t-2"}},
		{"broken-file-and-chain", "tenants:\n  t-1: [unclosed\n", [][]byte{good, bad}, []string{"t-1", "t-2"}},
		{"one-tenant-not-a-mapping", "tenants:\n  t-1:\n    cpu: \"1\"\n  t-2: 5\n  t-3:\n", [][]byte{good}, []string{"t-1", "t-2", "t-3", "t-absent"}},
		{"no-tenants-key", "defaults:\n  cpu: 1\n", [][]byte{good}, []string{"t-1", "t-2"}},
		{"non-dict-root", "- a\n- b\n", nil, []string{"t-1", "t-2"}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			doc := ParseTenantDoc([]byte(tc.tenant))
			for _, tid := range tc.tids {
				got, gotErr := computeEffectiveConfigDocDetailed(doc, tid, tc.chain, nil, nil)
				want, wantErr := computeEffectiveConfigBytesDetailed([]byte(tc.tenant), tid, tc.chain, nil, nil)
				if fmt.Sprint(gotErr) != fmt.Sprint(wantErr) {
					t.Errorf("%s: err %v, own parse %v", tid, gotErr, wantErr)
				}
				var gt, wt *tenantParseError
				var gc, wc *chainParseError
				if errors.As(gotErr, &gt) != errors.As(wantErr, &wt) || errors.As(gotErr, &gc) != errors.As(wantErr, &wc) {
					t.Errorf("%s: error type differs: %T vs %T", tid, gotErr, wantErr)
				}
				if !reflect.DeepEqual(got, want) {
					t.Errorf("%s: result %+v, own parse %+v", tid, got, want)
				}
				h, herr := ComputeMergedHashDoc(doc, tid, tc.chain)
				wh, wherr := ComputeMergedHash([]byte(tc.tenant), tid, tc.chain)
				if h != wh || fmt.Sprint(herr) != fmt.Sprint(wherr) {
					t.Errorf("%s: hash %q/%v, own parse %q/%v", tid, h, herr, wh, wherr)
				}
			}
			// Each tenant's *tenantParseError is its own value.
			_, e1 := computeEffectiveConfigDocDetailed(doc, tc.tids[0], nil, nil, nil)
			_, e2 := computeEffectiveConfigDocDetailed(doc, tc.tids[1], nil, nil, nil)
			if e1 != nil && strings.HasPrefix(e1.Error(), "parse tenant:") && e1 == e2 {
				t.Errorf("two tenants share one error value %p", e1)
			}
		})
	}
}
