package config

// tree_scan_parity_test.go — the standing three-plane parity table for the
// ONE conf.d walker (#1941, #1911 ①(a)).
//
// Three planes answer "which tenants exist under this tree, and what is each
// one's defaults chain":
//
//	WALKER   ScanDirTree →
//	         - tenants: the exporter's whole-tree verdict (sorted Tenants, or
//	           "ERR: <Conflict>" — a duplicate anywhere rejects the tree);
//	         - per tenant: TreeScan.Locate (dup → that tenant's own error,
//	           absent → NOTFOUND), then the chain from
//	           InheritanceGraph().TenantDefaults (the production chain, not
//	           one recomputed here) or, under a conflict where there is no
//	           graph, CollectDefaultsChain over scan.Defaults — the function
//	           the graph is built from;
//	         - scope: the tenants declared by files under the scope, or ERR
//	           iff one of THEM is a duplicate;
//	         plus the walker's own classification: sorted Files keys and
//	         Defaults (root-relative)
//	RESOLVE  ResolveEffective(root, id) → err | DefaultsChain
//	SCOPE    ScopeEffective(root, scope) → err | tenant ids (a row may spell
//	         the scope path differently from the root — scopeDir)
//
// W1 (#1941) moved the walker into this package with ZERO behavior change on
// either side; W2 (#1677) made ResolveEffective / ScopeEffective consume it,
// which flipped the symlinked-root, null-body, hidden-scope and
// duplicate-plus-innocent rows to agree. B8 (#1674) made every chain read
// one carrier selection (SelectDefaultsCarriers over the case-folded
// defaults set), which flipped the two upper-case _DEFAULTS.YAML rows; only
// the scalar-body row stays diverge, until #1957. Every row pins the CURRENT observation
// of all three planes exactly — for the walker that includes which files it
// kept (Files) and which it classified as defaults (Defaults), so a change to
// its skip or classification rules is red even when no chain moves:
//
//   - expect=agree rows additionally assert WALKER == RESOLVE per queried
//     tenant and WALKER == SCOPE on the scope. A red agree row is a
//     regression on one plane.
//   - expect=diverge rows assert the planes still DISAGREE, in exactly the
//     pinned way, and name the ticket that owns the fix. When that fix lands
//     the row goes red with a message saying what to do: move it to
//     expect=agree (drop `owner`) and re-pin `want` to the agreed answer.
//
// ⛔ The CONTROL row failing fails the whole test: if the harness cannot
// make the three planes agree on the plainest tree, every other row's
// verdict is meaningless (anti-vacuity).
//
// Verdict strings: "chain=[...]" (root-relative slash paths), "NOTFOUND"
// (ErrTenantNotFound / tenant absent from the walker), or "ERR: <msg>" with
// the tree's absolute root rewritten to "<root>/" so the pin is portable.
// Seams: none needed — pure functions over t.TempDir(), no globals.

import (
	"errors"
	"fmt"
	"io"
	"log"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"
)

type parityObs struct {
	walkerTenants string            // "[a b]" or "ERR: <conflict>"
	walkerFiles   string            // sorted scan.Files keys (root-relative slash)
	walkerDefs    string            // sorted scan.Defaults, root-relative slash
	walker        map[string]string // query tenant → verdict
	resolve       map[string]string // query tenant → verdict
	walkerScope   string            // walker tenants under the scope, or ERR
	scope         string            // ScopeEffective tenant ids, or ERR
}

type parityCase struct {
	name    string
	build   func(t *testing.T, base string) (root string)
	queries []string
	scope   string // relative to root; "" = the root itself
	// scopeDir, when set, is how the SCOPE plane spells the scope (given the
	// root build returned) — e.g. through the real path behind a symlinked
	// root. `scope` still names the same directory, root-relative, for the
	// WALKER plane. nil = filepath.Join(root, scope).
	scopeDir func(root string) string
	expect   string // "agree" | "diverge"
	owner    string // diverge only: the ticket whose fix flips this row
	want     parityObs
}

func parityWrite(t *testing.T, path, content string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
}

// parityNorm rewrites every spelling of the tree's root to "<root>/" and
// makes separators portable.
func parityNorm(msg string, roots ...string) string {
	for _, r := range roots {
		msg = strings.ReplaceAll(msg, r+string(filepath.Separator), "<root>/")
	}
	return filepath.ToSlash(msg)
}

func observeParity(t *testing.T, root string, queries []string, scopeRel string, scopeSpelling func(root string) string) parityObs {
	t.Helper()
	absRoot, err := filepath.Abs(root)
	if err != nil {
		t.Fatal(err)
	}
	roots := []string{AbsScanRoot(root), filepath.Clean(absRoot)}
	norm := func(err error) string { return "ERR: " + parityNorm(err.Error(), roots...) }

	obs := parityObs{walker: map[string]string{}, resolve: map[string]string{}}

	// WALKER.
	scan, err := ScanDirTree(root, nil, nil, log.New(io.Discard, "", 0))
	if err != nil {
		t.Fatalf("ScanDirTree(%s): %v — every parity row expects the walk itself to succeed", root, err)
	}
	files := make([]string, 0, len(scan.Files))
	for k := range scan.Files {
		files = append(files, k)
	}
	sort.Strings(files)
	obs.walkerFiles = fmt.Sprint(files)
	defs := make([]string, 0, len(scan.Defaults))
	for p := range scan.Defaults {
		r, rerr := filepath.Rel(scan.AbsRoot, p)
		if rerr != nil {
			t.Fatalf("defaults entry %s not under %s: %v", p, scan.AbsRoot, rerr)
		}
		defs = append(defs, filepath.ToSlash(r))
	}
	sort.Strings(defs)
	obs.walkerDefs = fmt.Sprint(defs)

	scopeAbs := filepath.Join(scan.AbsRoot, filepath.FromSlash(scopeRel))
	underScope := func(p string) bool {
		return scopeRel == "" || strings.HasPrefix(p, scopeAbs+string(filepath.Separator))
	}

	// The graph production serves; nil under a conflict (the exporter
	// rejects the whole tree). ⛔ The exported contract is asserted here, not
	// assumed: under a conflict Tenants must stay nil.
	graph := scan.InheritanceGraph()
	if scan.Conflict != nil {
		if scan.Tenants != nil || graph != nil {
			t.Fatalf("scan has a conflict but Tenants=%v graph=%v — the exporter's whole-tree rejection contract is broken", scan.Tenants, graph)
		}
		// The exporter's whole-tree verdict, unchanged by W2.
		obs.walkerTenants = norm(scan.Conflict)

		// Scope under a conflict: ERR iff an IN-SCOPE tenant is a duplicate
		// (first such tenant in sorted order), else the in-scope tenants.
		set := map[string]bool{}
		for _, f := range scan.Files {
			if underScope(f.AbsPath) {
				for _, id := range f.TenantIDs {
					set[id] = true
				}
			}
		}
		ids := make([]string, 0, len(set))
		for id := range set {
			ids = append(ids, id)
		}
		sort.Strings(ids)
		obs.walkerScope = fmt.Sprint(nonNilStrings(ids))
		for _, id := range ids {
			var dup *DuplicateTenantError
			if _, lerr := scan.Locate(id); errors.As(lerr, &dup) {
				obs.walkerScope = norm(lerr)
				break
			}
		}
	} else {
		ids := make([]string, 0, len(scan.Tenants))
		for id := range scan.Tenants {
			ids = append(ids, id)
		}
		sort.Strings(ids)
		obs.walkerTenants = fmt.Sprint(ids)

		var inScope []string
		for _, id := range ids {
			if underScope(scan.Tenants[id]) {
				inScope = append(inScope, id)
			}
		}
		obs.walkerScope = fmt.Sprint(nonNilStrings(inScope))
	}

	// Per-tenant WALKER verdict: the walk's per-tenant view (Locate), so an
	// uninvolved tenant under a conflict still reads its chain.
	for _, q := range queries {
		src, lerr := scan.Locate(q)
		switch {
		case errors.Is(lerr, ErrTenantNotFound):
			obs.walker[q] = "NOTFOUND"
			continue
		case lerr != nil:
			obs.walker[q] = norm(lerr)
			continue
		}
		var chain []string
		if graph != nil {
			// The chain production serves, read off the scan's own graph —
			// not recomputed here, so a regression in how the scan builds
			// its graph is visible to this table.
			c, ok := graph.TenantDefaults[q]
			if !ok {
				t.Fatalf("tenant %s is located by the scan but missing from InheritanceGraph().TenantDefaults", q)
			}
			chain = c
		} else {
			// No graph under a conflict: the function the graph is built
			// from, over the same scan.
			chain = CollectDefaultsChain(filepath.Dir(src), scan.AbsRoot, scan.Defaults)
		}
		rel := make([]string, 0, len(chain))
		for _, p := range chain {
			r, rerr := filepath.Rel(scan.AbsRoot, p)
			if rerr != nil {
				t.Fatalf("chain entry %s not under %s: %v", p, scan.AbsRoot, rerr)
			}
			rel = append(rel, filepath.ToSlash(r))
		}
		obs.walker[q] = fmt.Sprintf("chain=%v", rel)
	}

	// RESOLVE.
	for _, q := range queries {
		ec, rerr := ResolveEffective(root, q)
		switch {
		case errors.Is(rerr, ErrTenantNotFound):
			obs.resolve[q] = "NOTFOUND"
		case rerr != nil:
			obs.resolve[q] = norm(rerr)
		default:
			obs.resolve[q] = fmt.Sprintf("chain=%v", nonNilStrings(ec.DefaultsChain))
		}
	}

	// SCOPE. The scope path is spelled through `root` unless the row says
	// otherwise (scopeSpelling: e.g. the real path behind a symlinked root).
	scopeDir := root
	if scopeRel != "" {
		scopeDir = filepath.Join(root, filepath.FromSlash(scopeRel))
	}
	if scopeSpelling != nil {
		scopeDir = scopeSpelling(root)
	}
	st, serr := ScopeEffective(root, scopeDir)
	if serr != nil {
		obs.scope = norm(serr)
	} else {
		ids := make([]string, 0, len(st.Tenants))
		for _, ec := range st.Tenants {
			ids = append(ids, ec.TenantID)
		}
		sort.Strings(ids)
		obs.scope = fmt.Sprint(ids)
	}
	return obs
}

func nonNilStrings(s []string) []string {
	if s == nil {
		return []string{}
	}
	return s
}

// planesAgree reports where WALKER disagrees with RESOLVE or SCOPE.
func planesAgree(obs parityObs, queries []string) (disagreements []string) {
	for _, q := range queries {
		if obs.walker[q] != obs.resolve[q] {
			disagreements = append(disagreements, fmt.Sprintf("tenant %s: WALKER %q vs RESOLVE %q", q, obs.walker[q], obs.resolve[q]))
		}
	}
	if obs.walkerScope != obs.scope {
		disagreements = append(disagreements, fmt.Sprintf("scope: WALKER %q vs SCOPE %q", obs.walkerScope, obs.scope))
	}
	return disagreements
}

func diffObs(got, want parityObs, queries []string) []string {
	var d []string
	if got.walkerTenants != want.walkerTenants {
		d = append(d, fmt.Sprintf("WALKER tenants = %q, pinned %q", got.walkerTenants, want.walkerTenants))
	}
	for _, q := range queries {
		if got.walker[q] != want.walker[q] {
			d = append(d, fmt.Sprintf("WALKER %s = %q, pinned %q", q, got.walker[q], want.walker[q]))
		}
		if got.resolve[q] != want.resolve[q] {
			d = append(d, fmt.Sprintf("RESOLVE %s = %q, pinned %q", q, got.resolve[q], want.resolve[q]))
		}
	}
	if got.walkerFiles != want.walkerFiles {
		d = append(d, fmt.Sprintf("WALKER files = %q, pinned %q", got.walkerFiles, want.walkerFiles))
	}
	if got.walkerDefs != want.walkerDefs {
		d = append(d, fmt.Sprintf("WALKER defaults = %q, pinned %q", got.walkerDefs, want.walkerDefs))
	}
	if got.walkerScope != want.walkerScope {
		d = append(d, fmt.Sprintf("WALKER scope = %q, pinned %q", got.walkerScope, want.walkerScope))
	}
	if got.scope != want.scope {
		d = append(d, fmt.Sprintf("SCOPE = %q, pinned %q", got.scope, want.scope))
	}
	return d
}

const parityControl = "CONTROL"

func parityCases() []parityCase {
	return []parityCase{
		{
			name: parityControl,
			build: func(t *testing.T, root string) string {
				parityWrite(t, filepath.Join(root, "_defaults.yaml"), "defaults:\n  cpu: 1\n")
				parityWrite(t, filepath.Join(root, "sub", "_defaults.yaml"), "defaults:\n  cpu: 2\n")
				parityWrite(t, filepath.Join(root, "sub", "a.yaml"), "tenants:\n  ta: {}\n")
				parityWrite(t, filepath.Join(root, "b.yml"), "tenants:\n  tb: {}\n")
				return root
			},
			queries: []string{"ta", "tb"},
			expect:  "agree",
			want: parityObs{
				walkerTenants: "[ta tb]",
				walkerFiles:   "[_defaults.yaml b.yml sub/_defaults.yaml sub/a.yaml]",
				walkerDefs:    "[_defaults.yaml sub/_defaults.yaml]",
				walker:        map[string]string{"ta": "chain=[_defaults.yaml sub/_defaults.yaml]", "tb": "chain=[_defaults.yaml]"},
				resolve:       map[string]string{"ta": "chain=[_defaults.yaml sub/_defaults.yaml]", "tb": "chain=[_defaults.yaml]"},
				walkerScope:   "[ta tb]",
				scope:         "[ta tb]",
			},
		},
		{
			name: "unparseable tenant file is skipped by every plane",
			build: func(t *testing.T, root string) string {
				parityWrite(t, filepath.Join(root, "a.yaml"), "tenants: [unclosed\n")
				parityWrite(t, filepath.Join(root, "c.yaml"), "tenants:\n  tz: {}\n")
				return root
			},
			queries: []string{"tz"},
			expect:  "agree",
			want: parityObs{
				walkerTenants: "[tz]",
				walkerFiles:   "[a.yaml c.yaml]",
				walkerDefs:    "[]",
				walker:        map[string]string{"tz": "chain=[]"},
				resolve:       map[string]string{"tz": "chain=[]"},
				walkerScope:   "[tz]",
				scope:         "[tz]",
			},
		},
		{
			name: "underscore-prefixed file that declares tenants",
			build: func(t *testing.T, root string) string {
				// `_`-prefixed files are platform files: never tenant carriers
				// on any plane (walker: hashed, never parsed; ResolveEffective
				// and ScopeEffective: skipped before the tenants peek).
				parityWrite(t, filepath.Join(root, "_x.yaml"), "tenants:\n  tunder: {}\n")
				parityWrite(t, filepath.Join(root, "t.yaml"), "tenants:\n  tn: {}\n")
				return root
			},
			queries: []string{"tunder", "tn"},
			expect:  "agree",
			want: parityObs{
				walkerTenants: "[tn]",
				walkerFiles:   "[_x.yaml t.yaml]",
				walkerDefs:    "[]",
				walker:        map[string]string{"tunder": "NOTFOUND", "tn": "chain=[]"},
				resolve:       map[string]string{"tunder": "NOTFOUND", "tn": "chain=[]"},
				walkerScope:   "[tn]",
				scope:         "[tn]",
			},
		},
		{
			name: "upper-case extensions on tenant files",
			build: func(t *testing.T, root string) string {
				// Extension match is case-insensitive on every plane.
				parityWrite(t, filepath.Join(root, "T.YAML"), "tenants:\n  tupper: {}\n")
				parityWrite(t, filepath.Join(root, "t.YML"), "tenants:\n  tyml: {}\n")
				return root
			},
			queries: []string{"tupper", "tyml"},
			expect:  "agree",
			want: parityObs{
				walkerTenants: "[tupper tyml]",
				walkerFiles:   "[T.YAML t.YML]",
				walkerDefs:    "[]",
				walker:        map[string]string{"tupper": "chain=[]", "tyml": "chain=[]"},
				resolve:       map[string]string{"tupper": "chain=[]", "tyml": "chain=[]"},
				walkerScope:   "[tupper tyml]",
				scope:         "[tupper tyml]",
			},
		},
		{
			name: "symlinked root",
			build: func(t *testing.T, base string) string {
				realDir := filepath.Join(base, "real")
				parityWrite(t, filepath.Join(realDir, "_defaults.yaml"), "defaults:\n  cpu: 1\n")
				parityWrite(t, filepath.Join(realDir, "sub", "t.yaml"), "tenants:\n  tsym: {}\n")
				link := filepath.Join(base, "link")
				if err := os.Symlink(realDir, link); err != nil {
					t.Skipf("os.Symlink unavailable here (%v) — the symlinked-root row cannot be built on this platform "+
						"(Windows without the symlink privilege); it is measured on Linux/macOS CI", err)
				}
				return link
			},
			queries: []string{"tsym"},
			expect:  "agree", // was diverge until W2 (#1677 F1): RESOLVE NOTFOUND, SCOPE []
			want: parityObs{
				walkerTenants: "[tsym]",
				walkerFiles:   "[_defaults.yaml sub/t.yaml]",
				walkerDefs:    "[_defaults.yaml]",
				walker:        map[string]string{"tsym": "chain=[_defaults.yaml]"},
				resolve:       map[string]string{"tsym": "chain=[_defaults.yaml]"},
				walkerScope:   "[tsym]",
				scope:         "[tsym]",
			},
		},
		{
			// root=link, scope=link/sub. On main: SCOPE "tenant not found"
			// (the scope walk found tsym, the unresolved re-walk did not).
			name:    "symlinked root, scope spelled through the link",
			build:   paritySymlinkTree(true),
			queries: []string{"tsym"},
			scope:   "sub",
			expect:  "agree",
			want:    paritySymlinkWant,
		},
		{
			// root=link, scope=real/sub. On main: "outside configDir".
			name:     "symlinked root, scope spelled through the real path",
			build:    paritySymlinkTree(true),
			queries:  []string{"tsym"},
			scope:    "sub",
			scopeDir: func(root string) string { return filepath.Join(filepath.Dir(root), "real", "sub") },
			expect:   "agree",
			want:     paritySymlinkWant,
		},
		{
			// root=real, scope=link/sub. On main: "outside configDir".
			name:     "real root, scope spelled through a symlink",
			build:    paritySymlinkTree(false),
			queries:  []string{"tsym"},
			scope:    "sub",
			scopeDir: func(root string) string { return filepath.Join(filepath.Dir(root), "link", "sub") },
			expect:   "agree",
			want:     paritySymlinkWant,
		},
		{
			name: "tenant declared with a null body",
			build: func(t *testing.T, root string) string {
				parityWrite(t, filepath.Join(root, "a.yaml"), "tenants:\n  tnull:\n")
				parityWrite(t, filepath.Join(root, "b.yaml"), "tenants:\n  tok: {}\n")
				return root
			},
			queries: []string{"tnull", "tok"},
			expect:  "agree", // was diverge until W2 (#1677 F2): RESOLVE tnull "not in file", SCOPE failed whole
			want: parityObs{
				walkerTenants: "[tnull tok]",
				walkerFiles:   "[a.yaml b.yaml]",
				walkerDefs:    "[]",
				walker:        map[string]string{"tnull": "chain=[]", "tok": "chain=[]"},
				resolve:       map[string]string{"tnull": "chain=[]", "tok": "chain=[]"},
				walkerScope:   "[tnull tok]",
				scope:         "[tnull tok]",
			},
		},
		{
			name: "tenant body is a scalar",
			build: func(t *testing.T, root string) string {
				parityWrite(t, filepath.Join(root, "a.yaml"), "tenants:\n  tsc: 5\n")
				parityWrite(t, filepath.Join(root, "b.yaml"), "tenants:\n  tok: {}\n")
				return root
			},
			queries: []string{"tsc", "tok"},
			expect:  "diverge",
			owner:   "P, #1957 (unified parse: the walker counts a tenant whose body the full parse rejects)",
			want: parityObs{
				walkerTenants: "[tok tsc]",
				walkerFiles:   "[a.yaml b.yaml]",
				walkerDefs:    "[]",
				walker:        map[string]string{"tsc": "chain=[]", "tok": "chain=[]"},
				resolve:       map[string]string{"tsc": `ERR: tenant "tsc" not in file`, "tok": "chain=[]"},
				walkerScope:   "[tok tsc]",
				scope:         `ERR: resolve tenant "tsc": tenant "tsc" not in file`,
			},
		},
		{
			name: "scope is a hidden directory",
			build: func(t *testing.T, root string) string {
				parityWrite(t, filepath.Join(root, ".hidden", "t.yaml"), "tenants:\n  thid: {}\n")
				parityWrite(t, filepath.Join(root, "visible", "t.yaml"), "tenants:\n  tvis: {}\n")
				return root
			},
			queries: []string{"thid", "tvis"},
			scope:   ".hidden",
			expect:  "agree", // was diverge until W2 (#1677 F3): SCOPE enumerated thid, then RESOLVE could not find it
			want: parityObs{
				walkerTenants: "[tvis]",
				walkerFiles:   "[visible/t.yaml]",
				walkerDefs:    "[]",
				walker:        map[string]string{"thid": "NOTFOUND", "tvis": "chain=[]"},
				resolve:       map[string]string{"thid": "NOTFOUND", "tvis": "chain=[]"},
				walkerScope:   "[]",
				scope:         "[]",
			},
		},
		{
			name: "upper-case _DEFAULTS.YAML beside _defaults.yml",
			build: func(t *testing.T, root string) string {
				parityWrite(t, filepath.Join(root, "sub", "_DEFAULTS.YAML"), "defaults:\n  cpu: 111\n")
				parityWrite(t, filepath.Join(root, "sub", "_defaults.yml"), "defaults:\n  cpu: 222\n")
				parityWrite(t, filepath.Join(root, "sub", "t.yaml"), "tenants:\n  tk: {}\n")
				return root
			},
			queries: []string{"tk"},
			// Was diverge until B8 (#1674): WALKER chain=[sub/_defaults.yml]
			// (exact lower-case names only), RESOLVE chain=[sub/_DEFAULTS.YAML].
			// Both now read SelectDefaultsCarriers: the `.yaml` spelling wins.
			expect: "agree",
			want: parityObs{
				walkerTenants: "[tk]",
				walkerFiles:   "[sub/_DEFAULTS.YAML sub/_defaults.yml sub/t.yaml]",
				walkerDefs:    "[sub/_DEFAULTS.YAML sub/_defaults.yml]",
				walker:        map[string]string{"tk": "chain=[sub/_DEFAULTS.YAML]"},
				resolve:       map[string]string{"tk": "chain=[sub/_DEFAULTS.YAML]"},
				walkerScope:   "[tk]",
				scope:         "[tk]",
			},
		},
		{
			name: "only an upper-case _DEFAULTS.YAML",
			build: func(t *testing.T, root string) string {
				parityWrite(t, filepath.Join(root, "_DEFAULTS.YAML"), "defaults:\n  cpu: 1\n")
				parityWrite(t, filepath.Join(root, "t.yaml"), "tenants:\n  tu: {}\n")
				return root
			},
			queries: []string{"tu"},
			// Was diverge until B8 (#1674): WALKER chain=[] — the file was
			// classified a carrier (walkerDefs) and then left out of the chain.
			expect: "agree",
			want: parityObs{
				walkerTenants: "[tu]",
				walkerFiles:   "[_DEFAULTS.YAML t.yaml]",
				walkerDefs:    "[_DEFAULTS.YAML]",
				walker:        map[string]string{"tu": "chain=[_DEFAULTS.YAML]"},
				resolve:       map[string]string{"tu": "chain=[_DEFAULTS.YAML]"},
				walkerScope:   "[tu]",
				scope:         "[tu]",
			},
		},
		{
			// #1674 measurement (C): the exporter's chain stopped at the root,
			// describe_tenant and ResolveEffective included the subtree file.
			name: "case-variant .yml carrier in a subtree under a lower-case root carrier",
			build: func(t *testing.T, root string) string {
				parityWrite(t, filepath.Join(root, "_defaults.yaml"), "defaults:\n  cpu_pct: 50\n")
				parityWrite(t, filepath.Join(root, "sub2", "_DEFAULTS.YML"), "defaults:\n  cpu_pct: 90\n")
				parityWrite(t, filepath.Join(root, "sub2", "t1.yaml"), "tenants:\n  tc: {}\n")
				return root
			},
			queries: []string{"tc"},
			expect:  "agree",
			want: parityObs{
				walkerTenants: "[tc]",
				walkerFiles:   "[_defaults.yaml sub2/_DEFAULTS.YML sub2/t1.yaml]",
				walkerDefs:    "[_defaults.yaml sub2/_DEFAULTS.YML]",
				walker:        map[string]string{"tc": "chain=[_defaults.yaml sub2/_DEFAULTS.YML]"},
				resolve:       map[string]string{"tc": "chain=[_defaults.yaml sub2/_DEFAULTS.YML]"},
				walkerScope:   "[tc]",
				scope:         "[tc]",
			},
		},
		{
			// #1674 measurement (B): one ROOT with both extensions. The chain
			// reads the `.yaml` only; the flat plane's root Defaults follow it
			// (pinned in app/config_defaults_carrier_test.go) and
			// describe_tenant too (tests/golden carrier-selection fixture).
			name: "root _defaults.yaml beside _defaults.yml",
			build: func(t *testing.T, root string) string {
				parityWrite(t, filepath.Join(root, "_defaults.yaml"), "defaults:\n  cpu_pct: 50\n")
				parityWrite(t, filepath.Join(root, "_defaults.yml"), "defaults:\n  cpu_pct: 90\n")
				parityWrite(t, filepath.Join(root, "t.yaml"), "tenants:\n  tp: {}\n")
				return root
			},
			queries: []string{"tp"},
			expect:  "agree",
			want: parityObs{
				walkerTenants: "[tp]",
				walkerFiles:   "[_defaults.yaml _defaults.yml t.yaml]",
				walkerDefs:    "[_defaults.yaml _defaults.yml]",
				walker:        map[string]string{"tp": "chain=[_defaults.yaml]"},
				resolve:       map[string]string{"tp": "chain=[_defaults.yaml]"},
				walkerScope:   "[tp]",
				scope:         "[tp]",
			},
		},
		{
			name: "duplicate tenant across files, plus an innocent tenant",
			build: func(t *testing.T, root string) string {
				parityWrite(t, filepath.Join(root, "a.yaml"), "tenants:\n  tx: {}\n")
				parityWrite(t, filepath.Join(root, "b.yaml"), "tenants:\n  tx: {}\n")
				parityWrite(t, filepath.Join(root, "c.yaml"), "tenants:\n  ty: {}\n")
				return root
			},
			queries: []string{"ty", "tx"},
			// Was diverge until W2: the walker's per-tenant verdict came from
			// the whole-tree rejection, so innocent ty read the tx error.
			// walkerTenants still pins that rejection — it is the exporter's
			// verdict and W2 does not change it.
			expect: "agree",
			want: parityObs{
				walkerTenants: `ERR: duplicate tenant ID "tx": defined in both <root>/a.yaml and <root>/b.yaml`,
				walkerFiles:   "[a.yaml b.yaml c.yaml]",
				walkerDefs:    "[]",
				walker: map[string]string{
					"ty": "chain=[]",
					"tx": `ERR: duplicate tenant ID "tx": defined in both <root>/a.yaml and <root>/b.yaml`,
				},
				resolve: map[string]string{
					"ty": "chain=[]",
					"tx": `ERR: duplicate tenant ID "tx": defined in both <root>/a.yaml and <root>/b.yaml`,
				},
				walkerScope: `ERR: duplicate tenant ID "tx": defined in both <root>/a.yaml and <root>/b.yaml`,
				scope:       `ERR: duplicate tenant ID "tx": defined in both <root>/a.yaml and <root>/b.yaml`,
			},
		},
		{
			name: "two different duplicates: each queried tenant gets its own",
			build: func(t *testing.T, root string) string {
				parityWrite(t, filepath.Join(root, "a.yaml"), "tenants:\n  tx: {}\n")
				parityWrite(t, filepath.Join(root, "b.yaml"), "tenants:\n  tw: {}\n  tx: {}\n")
				parityWrite(t, filepath.Join(root, "c.yaml"), "tenants:\n  tw: {}\n")
				parityWrite(t, filepath.Join(root, "d.yaml"), "tenants:\n  ty: {}\n")
				return root
			},
			queries: []string{"tw", "tx", "ty"},
			expect:  "agree",
			want: parityObs{
				// The whole-tree verdict names the FIRST duplicate in walk order.
				walkerTenants: `ERR: duplicate tenant ID "tx": defined in both <root>/a.yaml and <root>/b.yaml`,
				walkerFiles:   "[a.yaml b.yaml c.yaml d.yaml]",
				walkerDefs:    "[]",
				walker: map[string]string{
					"tw": `ERR: duplicate tenant ID "tw": defined in both <root>/b.yaml and <root>/c.yaml`,
					"tx": `ERR: duplicate tenant ID "tx": defined in both <root>/a.yaml and <root>/b.yaml`,
					"ty": "chain=[]",
				},
				resolve: map[string]string{
					"tw": `ERR: duplicate tenant ID "tw": defined in both <root>/b.yaml and <root>/c.yaml`,
					"tx": `ERR: duplicate tenant ID "tx": defined in both <root>/a.yaml and <root>/b.yaml`,
					"ty": "chain=[]",
				},
				// The scope names the first duplicated in-scope tenant in
				// sorted order.
				walkerScope: `ERR: duplicate tenant ID "tw": defined in both <root>/b.yaml and <root>/c.yaml`,
				scope:       `ERR: duplicate tenant ID "tw": defined in both <root>/b.yaml and <root>/c.yaml`,
			},
		},
		{
			name: "duplicate entirely outside the scope",
			build: func(t *testing.T, root string) string {
				parityWrite(t, filepath.Join(root, "x", "a.yaml"), "tenants:\n  tx: {}\n")
				parityWrite(t, filepath.Join(root, "x", "b.yaml"), "tenants:\n  tx: {}\n")
				parityWrite(t, filepath.Join(root, "y", "c.yaml"), "tenants:\n  ty: {}\n")
				return root
			},
			queries: []string{"ty", "tx"},
			scope:   "y",
			expect:  "agree",
			want: parityObs{
				walkerTenants: `ERR: duplicate tenant ID "tx": defined in both <root>/x/a.yaml and <root>/x/b.yaml`,
				walkerFiles:   "[x/a.yaml x/b.yaml y/c.yaml]",
				walkerDefs:    "[]",
				walker: map[string]string{
					"ty": "chain=[]",
					"tx": `ERR: duplicate tenant ID "tx": defined in both <root>/x/a.yaml and <root>/x/b.yaml`,
				},
				resolve: map[string]string{
					"ty": "chain=[]",
					"tx": `ERR: duplicate tenant ID "tx": defined in both <root>/x/a.yaml and <root>/x/b.yaml`,
				},
				walkerScope: "[ty]",
				scope:       "[ty]",
			},
		},
	}
}

// paritySymlinkTree builds <base>/real (a root _defaults.yaml and
// sub/t.yaml declaring tsym) plus <base>/link → real, and returns the link
// (viaLink) or the real directory as the root.
func paritySymlinkTree(viaLink bool) func(t *testing.T, base string) string {
	return func(t *testing.T, base string) string {
		realDir := filepath.Join(base, "real")
		parityWrite(t, filepath.Join(realDir, "_defaults.yaml"), "defaults:\n  cpu: 1\n")
		parityWrite(t, filepath.Join(realDir, "sub", "t.yaml"), "tenants:\n  tsym: {}\n")
		link := filepath.Join(base, "link")
		if err := os.Symlink(realDir, link); err != nil {
			t.Skipf("os.Symlink unavailable here (%v) — symlinked rows cannot be built on this platform "+
				"(Windows without the symlink privilege); they are measured on Linux/macOS CI", err)
		}
		if viaLink {
			return link
		}
		return realDir
	}
}

// paritySymlinkWant is what every plane reads for paritySymlinkTree scoped
// to sub/, whichever way root and scope are spelled.
var paritySymlinkWant = parityObs{
	walkerTenants: "[tsym]",
	walkerFiles:   "[_defaults.yaml sub/t.yaml]",
	walkerDefs:    "[_defaults.yaml]",
	walker:        map[string]string{"tsym": "chain=[_defaults.yaml]"},
	resolve:       map[string]string{"tsym": "chain=[_defaults.yaml]"},
	walkerScope:   "[tsym]",
	scope:         "[tsym]",
}

func checkParityRow(t *testing.T, tc parityCase) {
	t.Helper()
	root := tc.build(t, t.TempDir())
	got := observeParity(t, root, tc.queries, tc.scope, tc.scopeDir)
	disagree := planesAgree(got, tc.queries)
	drift := diffObs(got, tc.want, tc.queries)

	switch tc.expect {
	case "agree":
		if len(disagree) > 0 {
			t.Errorf("expect=agree but the planes now DISAGREE — a regression on one plane "+
				"(the walker move #1941 must not change either side):\n  %s", strings.Join(disagree, "\n  "))
		}
		if len(drift) > 0 {
			t.Errorf("expect=agree row drifted from its pinned answer (both planes may have moved together; "+
				"if the new answer is intended, re-pin `want`):\n  %s", strings.Join(drift, "\n  "))
		}
	case "diverge":
		if tc.owner == "" {
			t.Fatalf("diverge row without an owner: name the ticket whose fix flips it")
		}
		if len(disagree) == 0 {
			t.Errorf("expect=diverge (owner: %s) but the three planes now AGREE.\n"+
				"If this is that fix landing: move this row to expect: \"agree\", delete `owner`, and re-pin `want` "+
				"to the agreed answer (WALKER/RESOLVE/SCOPE now read):\n  walkerTenants=%q\n  walkerFiles=%q\n  walkerDefs=%q\n  walker=%v\n  resolve=%v\n  walkerScope=%q\n  scope=%q",
				tc.owner, got.walkerTenants, got.walkerFiles, got.walkerDefs, got.walker, got.resolve, got.walkerScope, got.scope)
			return
		}
		if len(drift) > 0 {
			t.Errorf("expect=diverge (owner: %s): the divergence changed SHAPE, so the pin no longer describes it:\n  %s\n"+
				"If the change is the owner's partial fix, re-pin `want`; if it came from an unrelated change "+
				"(e.g. the #1941 walker move), that change altered behavior and is a regression.",
				tc.owner, strings.Join(drift, "\n  "))
		}
	default:
		t.Fatalf("expect must be \"agree\" or \"diverge\", got %q", tc.expect)
	}
}

func TestTreeScanParity_WalkerResolveScope(t *testing.T) {
	t.Parallel()
	cases := parityCases()
	if cases[0].name != parityControl {
		t.Fatalf("the first row must be %s", parityControl)
	}
	// ⛔ Anti-vacuity: the CONTROL row runs first, synchronously, and its
	// failure stops the whole table.
	if !t.Run(parityControl, func(t *testing.T) { checkParityRow(t, cases[0]) }) {
		t.Fatalf("%s row failed: the three planes do not agree on the plainest tree, so no other row's "+
			"verdict can be trusted — fix the harness or the regression before reading the rest", parityControl)
	}
	for _, tc := range cases[1:] {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			checkParityRow(t, tc)
		})
	}
}
