package config

// tree_scan_parity_test.go — the standing three-plane parity table for the
// ONE conf.d walker (#1941, #1911 ①(a)).
//
// Three planes answer "which tenants exist under this tree, and what is each
// one's defaults chain":
//
//	WALKER   ScanDirTree → Tenants + CollectDefaultsChain(dir(src), AbsRoot, Defaults)
//	RESOLVE  ResolveEffective(root, id) → err | DefaultsChain
//	SCOPE    ScopeEffective(root, scope) → err | tenant ids
//
// W1 (#1941) moved the walker into this package with ZERO behavior change on
// either side; W2 makes ResolveEffective / ScopeEffective consume it. This
// table is the guard for both steps. Every row pins the CURRENT observation
// of all three planes exactly:
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
	expect  string // "agree" | "diverge"
	owner   string // diverge only: the ticket whose fix flips this row
	want    parityObs
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

func observeParity(t *testing.T, root string, queries []string, scopeRel string) parityObs {
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
	if scan.Conflict != nil {
		obs.walkerTenants = norm(scan.Conflict)
		obs.walkerScope = obs.walkerTenants
		for _, q := range queries {
			obs.walker[q] = obs.walkerTenants
		}
	} else {
		ids := make([]string, 0, len(scan.Tenants))
		for id := range scan.Tenants {
			ids = append(ids, id)
		}
		sort.Strings(ids)
		obs.walkerTenants = fmt.Sprint(ids)

		scopeAbs := filepath.Join(scan.AbsRoot, filepath.FromSlash(scopeRel))
		var inScope []string
		for _, id := range ids {
			src := scan.Tenants[id]
			if scopeRel == "" || strings.HasPrefix(src, scopeAbs+string(filepath.Separator)) {
				inScope = append(inScope, id)
			}
		}
		obs.walkerScope = fmt.Sprint(nonNilStrings(inScope))

		for _, q := range queries {
			src, ok := scan.Tenants[q]
			if !ok {
				obs.walker[q] = "NOTFOUND"
				continue
			}
			chain := CollectDefaultsChain(filepath.Dir(src), scan.AbsRoot, scan.Defaults)
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

	// SCOPE.
	scopeDir := root
	if scopeRel != "" {
		scopeDir = filepath.Join(root, filepath.FromSlash(scopeRel))
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
				walker:        map[string]string{"tz": "chain=[]"},
				resolve:       map[string]string{"tz": "chain=[]"},
				walkerScope:   "[tz]",
				scope:         "[tz]",
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
			expect:  "diverge",
			owner:   "W2, #1677 F1 (ResolveEffective/ScopeEffective do not resolve a symlinked root; WalkDir never follows it)",
			want: parityObs{
				walkerTenants: "[tsym]",
				walker:        map[string]string{"tsym": "chain=[_defaults.yaml]"},
				resolve:       map[string]string{"tsym": "NOTFOUND"},
				walkerScope:   "[tsym]",
				scope:         "[]",
			},
		},
		{
			name: "tenant declared with a null body",
			build: func(t *testing.T, root string) string {
				parityWrite(t, filepath.Join(root, "a.yaml"), "tenants:\n  tnull:\n")
				parityWrite(t, filepath.Join(root, "b.yaml"), "tenants:\n  tok: {}\n")
				return root
			},
			queries: []string{"tnull", "tok"},
			expect:  "diverge",
			owner:   "W2, #1677 F2 (the walker counts a null-bodied tenant; ResolveEffective rejects it, sinking the whole scope)",
			want: parityObs{
				walkerTenants: "[tnull tok]",
				walker:        map[string]string{"tnull": "chain=[]", "tok": "chain=[]"},
				resolve:       map[string]string{"tnull": `ERR: tenant "tnull" not in file`, "tok": "chain=[]"},
				walkerScope:   "[tnull tok]",
				scope:         `ERR: resolve tenant "tnull": tenant "tnull" not in file`,
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
			expect:  "diverge",
			owner:   "W2, #1677 F3 (ScopeEffective does not prune a hidden scope root, then ResolveEffective cannot find what it enumerated)",
			want: parityObs{
				walkerTenants: "[tvis]",
				walker:        map[string]string{"thid": "NOTFOUND", "tvis": "chain=[]"},
				resolve:       map[string]string{"thid": "NOTFOUND", "tvis": "chain=[]"},
				walkerScope:   "[]",
				scope:         `ERR: resolve tenant "thid": tenant not found in config tree`,
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
			expect:  "diverge",
			owner:   "B8, #1674 (defaults-name case folding: the walker's chain matches exact names, ResolveEffective folds case)",
			want: parityObs{
				walkerTenants: "[tk]",
				walker:        map[string]string{"tk": "chain=[sub/_defaults.yml]"},
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
			expect:  "diverge",
			owner:   "B8, #1674 (defaults-name case folding)",
			want: parityObs{
				walkerTenants: "[tu]",
				walker:        map[string]string{"tu": "chain=[]"},
				resolve:       map[string]string{"tu": "chain=[_DEFAULTS.YAML]"},
				walkerScope:   "[tu]",
				scope:         "[tu]",
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
			expect:  "diverge",
			owner:   "W2 (the walker rejects the whole tree; ResolveEffective errors only when the conflict involves the queried tenant)",
			want: parityObs{
				walkerTenants: `ERR: duplicate tenant ID "tx": defined in both <root>/a.yaml and <root>/b.yaml`,
				walker: map[string]string{
					"ty": `ERR: duplicate tenant ID "tx": defined in both <root>/a.yaml and <root>/b.yaml`,
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
	}
}

func checkParityRow(t *testing.T, tc parityCase) {
	t.Helper()
	root := tc.build(t, t.TempDir())
	got := observeParity(t, root, tc.queries, tc.scope)
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
				"to the agreed answer (WALKER/RESOLVE/SCOPE now read):\n  walkerTenants=%q\n  walker=%v\n  resolve=%v\n  walkerScope=%q\n  scope=%q",
				tc.owner, got.walkerTenants, got.walker, got.resolve, got.walkerScope, got.scope)
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
