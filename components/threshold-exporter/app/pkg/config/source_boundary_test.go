package config

// source_boundary_test.go — pins the DIRECTORY-BOUNDARY half of the hidden-path
// classification in ScanFromConfigSource.
//
// ⛔ WHY A UNIT TEST HERE, when the behaviour already has an end-to-end parity
// test (`config_source_oracle_parity_test.go`, package main). Because that test
// cannot reach this code path: it drives the scanner through
// `InMemoryConfigSource.YAMLFiles`, which filters the corpus to `clean == root
// || HasPrefix(clean, root+"/")` before the classifier ever sees it. Every
// off-root path is gone by then. The classifier's handling of off-root input is
// therefore unobservable end-to-end — and that is exactly what made the earlier
// `strings.TrimPrefix(p, root)` version survive: it answered confidently about
// paths it had no business answering about, and nothing could see it.
//
// ⛔ This is NOT the "test the helper instead of the behaviour" shortcut this
// repo has paid for. The behaviour is pinned end-to-end by the parity test;
// this pins the one input class that pinning cannot reach, and it names the
// production change that would break it: reverting `relToRoot` to a bare
// prefix trim, or dropping the `!inRoot` arm at the call site.

import "testing"

func TestRelToRootRequiresADirectoryBoundary(t *testing.T) {
	t.Parallel()

	cases := []struct {
		name       string
		p, root    string
		wantRel    string
		wantInside bool
	}{
		{"file directly under root", "/sim/a.yaml", "/sim", "a.yaml", true},
		{"file nested under root", "/sim/x/y/a.yaml", "/sim", "x/y/a.yaml", true},
		{"path IS the root", "/sim", "/sim", "", true},
		{"hidden dir under root", "/sim/.git/a.yaml", "/sim", ".git/a.yaml", true},

		// ⛔ The four that a bare TrimPrefix gets wrong. `/sim-backup` and
		// `/simulate` share bytes with `/sim` but not a separator boundary.
		{"sibling sharing a byte prefix", "/sim-backup/.x/t.yaml", "/sim", "", false},
		{"sibling with no separator", "/simulate/a.yaml", "/sim", "", false},
		{"unrelated tree", "/other/.git/a.yaml", "/sim", "", false},
		{"root is a prefix of a longer name", "/simfoo/x.yaml", "/sim", "", false},

		// Filesystem root: path.Clean leaves "/" as the one root that already
		// ends in a separator, so the `root+"/"` form would be "//".
		{"root is slash", "/.git/a.yaml", "/", ".git/a.yaml", true},
		{"root is slash, plain file", "/a.yaml", "/", "a.yaml", true},

		// ⛔ The root's OWN dot segments must not leak into `rel`. The walker
		// never prunes its own starting point, so a source rooted at
		// `.config/conf.d` must yield its whole tree — see
		// TestDotPrefixedRootYieldsItsWholeTree for the end-to-end half.
		{"dot-prefixed root", "/.config/conf.d/t.yaml", "/.config/conf.d", "t.yaml", true},
		{"dot-prefixed root, nested", "/.config/conf.d/x/t.yaml", "/.config/conf.d", "x/t.yaml", true},
		{"dot segment BELOW a dot root", "/.config/conf.d/.git/t.yaml", "/.config/conf.d", ".git/t.yaml", true},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			rel, inside := relToRoot(tc.p, tc.root)
			if inside != tc.wantInside {
				t.Fatalf("relToRoot(%q, %q) inside = %v, want %v", tc.p, tc.root, inside, tc.wantInside)
			}
			if inside && rel != tc.wantRel {
				t.Errorf("relToRoot(%q, %q) rel = %q, want %q", tc.p, tc.root, rel, tc.wantRel)
			}
		})
	}
}

// TestOffRootInputIsNotClassifiedAsVisible states the consequence the boundary
// check exists for, in the terms the scan loop uses: an off-root path must
// never come back as "inside and not hidden", because that is the combination
// the loop admits.
func TestOffRootInputIsNotClassifiedAsVisible(t *testing.T) {
	t.Parallel()

	offRoot := []string{
		"/sim-backup/.x/t.yaml",
		"/simulate/a.yaml",
		"/other/.git/a.yaml",
		"/simfoo/x.yaml",
		"relative/a.yaml",
	}
	for _, p := range offRoot {
		rel, inside := relToRoot(p, "/sim")
		if inside && !hasHiddenSegment(rel) {
			t.Errorf("%q would be ADMITTED against root /sim (rel=%q) — the walker "+
				"never visits anything outside its root, so neither may this scanner", p, rel)
		}
	}
}

// offRootSource is a ConfigSource that does NOT do the root filtering
// InMemoryConfigSource does. The interface permits this — its doc asks the
// source to return "every *.yaml/*.yml file the source wants the merge engine
// to consider" and never says the engine may assume they are under rootPath.
type offRootSource map[string][]byte

func (s offRootSource) YAMLFiles(string) (map[string][]byte, error) { return s, nil }

// TestScanRejectsOffRootPathsFromASourceThatDoesNotFilter drives the off-root
// arm through the PUBLIC API rather than through the helper, which is the only
// way to pin the call site itself. Deleting the `!inRoot` arm in
// ScanFromConfigSource turns this red; the helper-level tests above do not,
// because they never execute that line.
func TestScanRejectsOffRootPathsFromASourceThatDoesNotFilter(t *testing.T) {
	t.Parallel()

	src := offRootSource{
		"/sim/inside.yaml":         []byte("tenants:\n  inside:\n    x: 1\n"),
		"/sim-backup/outside.yaml": []byte("tenants:\n  outside:\n    x: 2\n"),
		"/other/far.yaml":          []byte("tenants:\n  far:\n    x: 3\n"),
	}
	tenants, _, hashes, _, err := ScanFromConfigSource(src, "/sim")
	if err != nil {
		t.Fatalf("ScanFromConfigSource: %v", err)
	}
	for _, unwanted := range []string{"outside", "far"} {
		if p, ok := tenants[unwanted]; ok {
			t.Errorf("tenant %q registered from %q — it is not under /sim, and the "+
				"walker this scanner mirrors only ever visits files under its root", unwanted, p)
		}
	}
	if _, ok := tenants["inside"]; !ok {
		t.Fatalf("tenant \"inside\" missing — the guard is rejecting everything, so "+
			"this test would pass for the wrong reason; got %v", tenants)
	}
	for p := range hashes {
		if _, inside := relToRoot(p, "/sim"); !inside {
			t.Errorf("hashed %q, which is outside /sim", p)
		}
	}
}

// TestDotPrefixedRootYieldsItsWholeTree pins the half of the invariant that
// `hasHiddenSegment`'s own doc comment promises in words — "The root is never
// tested … so a caller scanning `.config/conf.d` gets its whole tree, not
// nothing" — and that nothing was checking.
//
// ⛔ WHY IT IS A SEPARATE TEST AND NOT ANOTHER TABLE ROW. The promise is
// load-bearing on the CALL SITE passing `rel` rather than `p`. Both are
// in-scope strings of the same type one line apart, so `hasHiddenSegment(p)` is
// an ordinary slip — and it is invisible to every other fixture in this repo,
// because every root used anywhere in the suite is `/sim` or `/`, neither of
// which has a dot segment for `p` to contribute over `rel`. Measured: with that
// substitution the entire suite stayed green (`go test ./... -count=1`, rc=0)
// while this scenario returned an empty tenant map. A test that exercises the
// helper alone cannot see it; only driving the public entry point with a
// dot-prefixed root can.
func TestDotPrefixedRootYieldsItsWholeTree(t *testing.T) {
	t.Parallel()

	const dotRoot = "/.config/conf.d"
	src := offRootSource{
		dotRoot + "/_defaults.yaml": []byte("defaults:\n  cpu_usage: 80\n"),
		dotRoot + "/tenant.yaml":    []byte("tenants:\n  t1:\n    x: 1\n"),
		dotRoot + "/sub/deep.yaml":  []byte("tenants:\n  t2:\n    x: 2\n"),
		// Still pruned: this one's dot segment is BELOW the root, not part of it.
		dotRoot + "/.git/leak.yaml": []byte("tenants:\n  leaked:\n    x: 3\n"),
	}
	tenants, defaults, _, _, err := ScanFromConfigSource(src, dotRoot)
	if err != nil {
		t.Fatalf("ScanFromConfigSource: %v", err)
	}
	for _, want := range []string{"t1", "t2"} {
		if _, ok := tenants[want]; !ok {
			t.Errorf("tenant %q missing under a dot-prefixed root — the root's own "+
				"dot segment is being treated as pruning, which the walker never does "+
				"(got %v)", want, tenants)
		}
	}
	if p, ok := tenants["leaked"]; ok {
		t.Errorf("tenant \"leaked\" registered from %q — `.git` is BELOW the root and "+
			"must still be pruned; the fix must not swing the other way", p)
	}
	if _, ok := defaults[dotRoot+"/_defaults.yaml"]; !ok {
		t.Errorf("the chain carrier under a dot-prefixed root was not classified as "+
			"defaults (got %v)", defaults)
	}
}

// TestHasHiddenSegmentChecksEverySegment pins the depth half: the walker prunes
// a hidden directory's ENTIRE subtree with fs.SkipDir, so a plain file under a
// plain directory under a hidden directory is still invisible to it.
func TestHasHiddenSegmentChecksEverySegment(t *testing.T) {
	t.Parallel()

	// `..odd` is hidden: the walker's rule is a bare `HasPrefix(name, ".")`, so a
	// name that merely STARTS with two dots is pruned like any other dot-name.
	// `sub.` is visible: the dot is at the end, and the rule is a prefix test.
	hidden := []string{".git/a.yaml", ".cache/deep/x.yaml", "a/.b/c/d.yaml", ".hidden.yaml", "a/b/.c.yaml", "..odd/x.yaml"}
	visible := []string{"", "a.yaml", "a/b.yaml", "a/b/c.yaml", "sub./x.yaml", "a/sub./b.yaml"}

	for _, rel := range hidden {
		if !hasHiddenSegment(rel) {
			t.Errorf("hasHiddenSegment(%q) = false, want true", rel)
		}
	}
	for _, rel := range visible {
		if hasHiddenSegment(rel) {
			t.Errorf("hasHiddenSegment(%q) = true, want false", rel)
		}
	}
}
