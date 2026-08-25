package main

// Sweep B-2 of #1521 (PR #1569): the mechanism PAIRS that must agree.
//
// ⛔ WHY PAIRS ARE THEIR OWN CATEGORY. #1521 was not "a scanner had a bug".
// It was two enumerators over one directory tree with different selection
// predicates and nothing comparing them — the divergence is invisible to any
// test that exercises one side. Every defect this PR fixed after round 1 was
// the same shape one layer in: a second scanner, a second loader, a dedup
// signature against the message it dedups, a bypass predicate against the
// resolvers it models.
//
// So the pairs get tests that assert the AGREEMENT, not the behaviour of
// either half. A test of one half passes while the pair is broken; that is
// exactly how this ticket stayed open.

import (
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"
)

// --- pair 1: the two enumerators -------------------------------------------

// TestBothScannersSeeTheSameFiles is the invariant whose violation IS #1521.
//
// `scanDirFileHashes` feeds `GetConfig()` → the collector → `/metrics`.
// `scanDirHierarchical` feeds `Resolve()` → `/effective`. `fullDirLoad` calls
// both and, before this PR, never compared them: a tenant one directory down
// was in the second and not the first, so `/effective` answered and no series
// existed — with no error, no WARN and no metric to notice it by.
//
// The contract asserted is one-directional on purpose: every file the
// hierarchical walker ATTRIBUTES (as a tenant source or as a defaults file)
// must be in the flat scanner's map. The reverse does not hold and should not
// — a `.yaml` declaring neither `tenants:` nor `defaults:` is hashed by the
// flat scanner (it must be, or editing it would not trigger a reload) and
// attributed by neither.
func TestBothScannersSeeTheSameFiles(t *testing.T) {
	t.Parallel()
	for _, tc := range scannerPairTrees() {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			dir := t.TempDir()
			tc.build(t, dir)

			flat, _, _, _, err := scanDirFileHashes(dir, nil, nil, nil)
			if err != nil {
				t.Fatalf("scanDirFileHashes: %v", err)
			}
			tenants, defaults, _, _, _, err := scanDirHierarchical(dir, nil)
			if err != nil {
				t.Fatalf("scanDirHierarchical: %v", err)
			}

			// The flat scanner keys on root-relative slash paths; the
			// hierarchical one stores absolute paths. Normalise to the former.
			absRoot := resolveScanRoot(mustAbs(t, dir))
			rel := func(p string) string {
				r, err := filepath.Rel(absRoot, resolveScanRoot(filepath.Clean(p)))
				if err != nil {
					t.Fatalf("Rel(%q, %q): %v", absRoot, p, err)
				}
				return filepath.ToSlash(r)
			}

			var missing []string
			for _, src := range tenants {
				if _, ok := flat[rel(src)]; !ok {
					missing = append(missing, "tenant source "+rel(src))
				}
			}
			for path := range defaults {
				if _, ok := flat[rel(path)]; !ok {
					missing = append(missing, "defaults file "+rel(path))
				}
			}
			if len(missing) > 0 {
				sort.Strings(missing)
				keys := make([]string, 0, len(flat))
				for k := range flat {
					keys = append(keys, k)
				}
				sort.Strings(keys)
				t.Fatalf("the hierarchical walker attributes %d file(s) the flat scanner never hashed —\n"+
					"每一個都是一個 /effective 查得到、/metrics 沒有的租戶：\n  %s\nflat scanner saw: %v",
					len(missing), strings.Join(missing, "\n  "), keys)
			}
		})
	}
}

// --- pair 2: the two loaders ------------------------------------------------

// TestIncrementalLoadLandsWhereAFullLoadWould pins that the fast path is a
// pure optimisation.
//
// ⛔ WHY A MATRIX AND NOT THE TWO CASES WE ALREADY HIT. Round 5 found that the
// refused-key set went stale on the incremental path, and shipped a test for
// that one field. But the field was not special: `IncrementalLoad` rebuilds
// SOME of what `fullDirLoad` publishes, and every piece it forgets is a fast
// path that silently disagrees with a restart. Asserting the whole published
// state against a from-scratch load of the same tree makes "which fields did
// you remember" stop being a judgement call.
func TestIncrementalLoadLandsWhereAFullLoadWould(t *testing.T) {
	t.Parallel()
	for _, mut := range incrementalMutations() {
		t.Run(mut.name, func(t *testing.T) {
			t.Parallel()

			// Path A: load the tree, mutate it, take the fast path.
			warm := t.TempDir()
			buildPairBaseTree(t, warm)
			mIncr := NewConfigManager(warm)
			if err := mIncr.Load(); err != nil {
				t.Fatalf("warm Load: %v", err)
			}
			mut.apply(t, warm)
			if err := mIncr.IncrementalLoad(); err != nil {
				t.Fatalf("IncrementalLoad: %v", err)
			}

			// Path B: the same mutated tree, loaded from scratch — what an
			// operator gets by restarting the exporter.
			cold := t.TempDir()
			buildPairBaseTree(t, cold)
			mut.apply(t, cold)
			mFull := NewConfigManager(cold)
			if err := mFull.Load(); err != nil {
				t.Fatalf("cold Load: %v", err)
			}

			got := publishedStateFingerprint(t, mIncr, warm)
			want := publishedStateFingerprint(t, mFull, cold)
			if got != want {
				t.Fatalf("after %s the incremental path disagrees with a restart\n--- incremental ---\n%s\n--- full ---\n%s",
					mut.name, got, want)
			}
		})
	}
}

// TestAnUnparseableFileKeepsItsTenantAttributed guards the half of the
// incremental prune that the equivalence matrix above cannot see, because the
// two paths AGREE there.
//
// The prune that stops a DEPARTED tenant being reported must not also delete
// cause (a)'s true positive: a file that still EXISTS and failed to parse.
// Keying the prune on "the tenant is gone from the merged config" would delete
// exactly that case — the one the divergence audit exists for. Keying it on
// this round's parse result separates the two.
//
// ⛔ THE FIXTURE HAS TO FORCE THE FULL-REBUILD BRANCH, and the first version of
// this test did not. `patchTenants` KEEPS a tenant whose file stopped parsing,
// so on the tenant-only fast path the tenant never leaves the merged config and
// both prune predicates behave identically — measured: swapping the correct
// predicate for the wrong one left this test green. Touching `_defaults.yaml`
// in the same reload takes the `mergePartialConfigs` branch, where the dropped
// file really does take its tenant with it. That is the state cause (a)
// describes, and the only one where the two predicates differ.
func TestAnUnparseableFileKeepsItsTenantAttributed(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeTestYAML(t, filepath.Join(dir, "_defaults.yaml"), "defaults:\n  mysql_connections: 80\n")
	writeTestYAML(t, filepath.Join(dir, "a.yaml"), "tenants:\n  t-a: {}\n")
	writeTestYAML(t, filepath.Join(dir, "b.yaml"), "tenants:\n  t-b: {}\n")

	m, _, logBuf := newAuditedManager(t, dir)
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}
	logBuf.Reset()

	writeTestYAML(t, filepath.Join(dir, "_defaults.yaml"), "defaults:\n  mysql_connections: 81\n")
	writeTestYAML(t, filepath.Join(dir, "a.yaml"), "tenants:\n  t-a: {}\n  \tbroken: [\n")
	if err := m.IncrementalLoad(); err != nil {
		t.Fatalf("IncrementalLoad: %v", err)
	}

	if _, present := m.GetConfig().Tenants["t-a"]; present {
		t.Fatal("fixture no longer reproduces cause (a): the unparseable file's tenant is still in the merged config, " +
			"so this test cannot tell the two prune predicates apart")
	}
	m.mu.RLock()
	_, attributed := m.hierarchy.tenantSources["t-a"]
	m.mu.RUnlock()
	if !attributed {
		t.Fatal("t-a lost its source attribution because its file stopped parsing —\n" +
			"that is cause (a) of the divergence audit, and dropping the attribution makes it unreportable")
	}
	if !strings.Contains(logBuf.String(), "conf.d scanner divergence") ||
		!strings.Contains(logBuf.String(), "t-a") {
		t.Fatalf("no divergence ERROR naming t-a — a tenant vanished from the collector plane in silence\n--- log ---\n%s", logBuf.String())
	}

	// The sibling assertion: a tenant whose file is GONE must be pruned, or the
	// audit reports a deletion as a defect.
	logBuf.Reset()
	if err := os.Remove(filepath.Join(dir, "b.yaml")); err != nil {
		t.Fatalf("Remove: %v", err)
	}
	if err := m.IncrementalLoad(); err != nil {
		t.Fatalf("IncrementalLoad: %v", err)
	}
	m.mu.RLock()
	_, stillThere := m.hierarchy.tenantSources["t-b"]
	m.mu.RUnlock()
	if stillThere {
		t.Fatal("t-b kept its source attribution after its file was deleted — the audit will report " +
			"the deleted tenant under cause (a), pointing operators at a log line that does not exist")
	}
}

// TestATenantDeclaredInTwoFilesSurvivesAnEditToEitherOne guards the removal
// loops against the question they were not actually asking.
//
// ⛔ `patchedTenants` ANSWERS "DID IT MOVE THIS ROUND", NOT "IS IT STILL
// DECLARED". A tenant named by two files at once is invalid — a full load
// hard-rejects it with DuplicateTenantError — but the incremental fast path
// accepts it silently, so a live tree can be in that state. Once it is, both
// removal loops deleted the tenant the moment EITHER owning file was edited
// for any reason, because the other file did not change this round and so was
// absent from `patchedTenants`. The tenant left the merged config while a file
// on disk still declared it, and the divergence audit blamed cause (a) —
// sending the operator to look for a parse-failure line that does not exist.
//
// Measured on both loops, before the fix: `dup present after r2=false,
// divergenceERR=true`; after: `true / false`.
//
// ⚠️ WHAT THIS DOES NOT ASSERT. The fast path still ACCEPTS the duplicate that
// a full load rejects. That asymmetry is real and recorded; it is not what
// these loops are for, and closing it means rejecting a config on the hot
// reload path, which is a policy decision rather than a bug fix.
func TestATenantDeclaredInTwoFilesSurvivesAnEditToEitherOne(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct {
		name            string
		removeWholeFile bool
	}{
		{"the tenant is removed from a file that stays on disk", false},
		{"the whole file is deleted", true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			dir := t.TempDir()
			writeTestYAML(t, filepath.Join(dir, "_defaults.yaml"), "defaults:\n  mysql_connections: 80\n")
			writeTestYAML(t, filepath.Join(dir, "a.yaml"), "tenants:\n  dup: {}\n")
			writeTestYAML(t, filepath.Join(dir, "b.yaml"), "tenants:\n  other: {}\n")

			m, _, logBuf := newAuditedManager(t, dir)
			if err := m.Load(); err != nil {
				t.Fatalf("Load: %v", err)
			}

			// The invalid-but-live state: b.yaml now names dup as well.
			writeTestYAML(t, filepath.Join(dir, "b.yaml"), "tenants:\n  other: {}\n  dup: {}\n")
			if err := m.IncrementalLoad(); err != nil {
				t.Fatalf("IncrementalLoad: %v", err)
			}
			if _, ok := m.GetConfig().Tenants["dup"]; !ok {
				t.Skip("the fast path no longer accepts a cross-file duplicate — this fixture's precondition is gone, and that is a better world")
			}

			// b.yaml is untouched from here on and still declares dup.
			logBuf.Reset()
			if tc.removeWholeFile {
				if err := os.Remove(filepath.Join(dir, "a.yaml")); err != nil {
					t.Fatalf("Remove: %v", err)
				}
			} else {
				writeTestYAML(t, filepath.Join(dir, "a.yaml"), "tenants: {}\n")
			}
			if err := m.IncrementalLoad(); err != nil {
				t.Fatalf("IncrementalLoad: %v", err)
			}

			if _, ok := m.GetConfig().Tenants["dup"]; !ok {
				t.Fatal("dup was deleted from the merged config while b.yaml, untouched this round, still declares it — " +
					"its alerts stop firing until something forces a full reload")
			}
			if strings.Contains(logBuf.String(), "conf.d scanner divergence") {
				t.Fatalf("a divergence ERROR was emitted for a tenant that is present in both planes\n--- log ---\n%s", logBuf.String())
			}
		})
	}
}

// publishedStateFingerprint renders everything a loader publishes that a
// consumer can observe: the merged config, and the hierarchy state the
// divergence audit reads. Paths are made root-relative so the two temp dirs
// compare equal.
func publishedStateFingerprint(t *testing.T, m *ConfigManager, root string) string {
	t.Helper()
	cfg := m.GetConfig()
	var b strings.Builder

	writeSorted := func(label string, items []string) {
		sort.Strings(items)
		fmt.Fprintf(&b, "%s:\n", label)
		for _, it := range items {
			fmt.Fprintf(&b, "  %s\n", it)
		}
	}

	defaults := make([]string, 0, len(cfg.Defaults))
	for k, v := range cfg.Defaults {
		defaults = append(defaults, fmt.Sprintf("%s=%v", k, v))
	}
	writeSorted("defaults", defaults)

	writeSorted("optional_overrides", append([]string(nil), cfg.OptionalOverrides...))

	var tenantRows []string
	for tenant, overrides := range cfg.Tenants {
		for key, sv := range overrides {
			tenantRows = append(tenantRows, fmt.Sprintf("%s/%s=%+v", tenant, key, sv))
		}
		if len(overrides) == 0 {
			tenantRows = append(tenantRows, tenant+"/<no overrides>")
		}
	}
	writeSorted("tenants", tenantRows)

	filters := make([]string, 0, len(cfg.StateFilters))
	for name, f := range cfg.StateFilters {
		filters = append(filters, fmt.Sprintf("%s=%+v", name, f))
	}
	writeSorted("state_filters", filters)

	absRoot := resolveScanRoot(mustAbs(t, root))
	m.mu.RLock()
	sources, unreachable := m.hierarchy.tenantSources, m.hierarchy.unreachableInherited
	var srcRows, unRows []string
	for tenant, path := range sources {
		r, err := filepath.Rel(absRoot, resolveScanRoot(filepath.Clean(path)))
		if err != nil {
			r = path
		}
		srcRows = append(srcRows, tenant+"="+filepath.ToSlash(r))
	}
	for tenant, keys := range unreachable {
		cp := append([]string(nil), keys...)
		sort.Strings(cp)
		unRows = append(unRows, tenant+"="+strings.Join(cp, ","))
	}
	m.mu.RUnlock()
	writeSorted("hierarchy.tenant_sources", srcRows)
	writeSorted("hierarchy.unreachable_inherited", unRows)

	return b.String()
}

// --- fixtures ---------------------------------------------------------------

// buildPairBaseTree is one tree carrying every structural feature the two
// pairs above can disagree on: a root defaults file, two subtree levels each
// with their own defaults, a tenant at each level, a nested tenant that
// inherits, a subtree-only key that must be refused, and a `.yaml` file that
// declares neither tenants nor defaults.
func buildPairBaseTree(t *testing.T, dir string) {
	t.Helper()
	writeTestYAML(t, filepath.Join(dir, "_defaults.yaml"),
		"defaults:\n  mysql_connections: 80\n  redis_evicted_keys: 10\n"+
			"state_filters:\n  maintenance:\n    severity: warning\n")
	writeTestYAML(t, filepath.Join(dir, "root-tenant.yaml"), "tenants:\n  t-root: {}\n")
	writeTestYAML(t, filepath.Join(dir, "notes.yaml"), "unrelated: true\n")

	mkSub(t, dir, "finance")
	writeTestYAML(t, filepath.Join(dir, "finance", "_defaults.yaml"),
		"defaults:\n  mysql_connections: 60\n  finance_only_key: 5\n")
	writeTestYAML(t, filepath.Join(dir, "finance", "t-fin.yaml"), "tenants:\n  t-fin: {}\n")

	mkSub(t, filepath.Join(dir, "finance"), "us-east")
	writeTestYAML(t, filepath.Join(dir, "finance", "us-east", "_defaults.yaml"),
		"defaults:\n  mysql_connections: 70\n")
	writeTestYAML(t, filepath.Join(dir, "finance", "us-east", "t-use.yaml"),
		"tenants:\n  t-use:\n    redis_evicted_keys: 99\n")
}

type pairTree struct {
	name  string
	build func(t *testing.T, dir string)
}

func scannerPairTrees() []pairTree {
	return []pairTree{
		{"the full tree", buildPairBaseTree},
		{"a flat tree with no subdirectory at all", func(t *testing.T, dir string) {
			writeTestYAML(t, filepath.Join(dir, "_defaults.yaml"), "defaults:\n  mysql_connections: 80\n")
			writeTestYAML(t, filepath.Join(dir, "a.yaml"), "tenants:\n  t-a: {}\n")
		}},
		{"a tenant three levels down", func(t *testing.T, dir string) {
			writeTestYAML(t, filepath.Join(dir, "_defaults.yaml"), "defaults:\n  mysql_connections: 80\n")
			deep := filepath.Join(dir, "a", "b", "c")
			if err := os.MkdirAll(deep, 0o755); err != nil {
				t.Fatalf("MkdirAll: %v", err)
			}
			writeTestYAML(t, filepath.Join(deep, "t.yaml"), "tenants:\n  t-deep: {}\n")
		}},
		{"an uppercase extension", func(t *testing.T, dir string) {
			// The two scanners' extension matching drifted once already:
			// `UPPER.YAML` at the ROOT of a tree with no nesting reproduced
			// this ticket's symptom exactly.
			writeTestYAML(t, filepath.Join(dir, "_defaults.yaml"), "defaults:\n  mysql_connections: 80\n")
			writeTestYAML(t, filepath.Join(dir, "UPPER.YAML"), "tenants:\n  t-upper: {}\n")
		}},
		{"same basename at two depths", func(t *testing.T, dir string) {
			// Bare-filename map keys collided here and silently ate one tenant.
			writeTestYAML(t, filepath.Join(dir, "_defaults.yaml"), "defaults:\n  mysql_connections: 80\n")
			writeTestYAML(t, filepath.Join(dir, "x.yaml"), "tenants:\n  t-top: {}\n")
			mkSub(t, dir, "sub")
			writeTestYAML(t, filepath.Join(dir, "sub", "x.yaml"), "tenants:\n  t-sub: {}\n")
		}},
	}
}

type incrMutation struct {
	name  string
	apply func(t *testing.T, dir string)
}

func incrementalMutations() []incrMutation {
	return []incrMutation{
		{"edit a nested tenant file", func(t *testing.T, dir string) {
			writeTestYAML(t, filepath.Join(dir, "finance", "us-east", "t-use.yaml"),
				"tenants:\n  t-use:\n    redis_evicted_keys: 123\n")
		}},
		{"edit a subtree defaults file", func(t *testing.T, dir string) {
			writeTestYAML(t, filepath.Join(dir, "finance", "_defaults.yaml"),
				"defaults:\n  mysql_connections: 65\n  finance_only_key: 5\n")
		}},
		{"edit the root defaults file", func(t *testing.T, dir string) {
			writeTestYAML(t, filepath.Join(dir, "_defaults.yaml"),
				"defaults:\n  mysql_connections: 85\n  redis_evicted_keys: 10\n"+
					"state_filters:\n  maintenance:\n    severity: warning\n")
		}},
		{"add a tenant in a brand new subdirectory", func(t *testing.T, dir string) {
			mkSub(t, dir, "ops")
			writeTestYAML(t, filepath.Join(dir, "ops", "_defaults.yaml"),
				"defaults:\n  mysql_connections: 40\n")
			writeTestYAML(t, filepath.Join(dir, "ops", "t-ops.yaml"), "tenants:\n  t-ops: {}\n")
		}},
		{"delete a nested tenant file", func(t *testing.T, dir string) {
			if err := os.Remove(filepath.Join(dir, "finance", "us-east", "t-use.yaml")); err != nil {
				t.Fatalf("Remove: %v", err)
			}
		}},
		{"delete a subtree defaults file", func(t *testing.T, dir string) {
			if err := os.Remove(filepath.Join(dir, "finance", "us-east", "_defaults.yaml")); err != nil {
				t.Fatalf("Remove: %v", err)
			}
		}},
		{"add a subtree-only key that cannot be delivered", func(t *testing.T, dir string) {
			writeTestYAML(t, filepath.Join(dir, "finance", "us-east", "_defaults.yaml"),
				"defaults:\n  mysql_connections: 70\n  another_subtree_only_key: 7\n")
		}},
		// ⛔ THE FOUR CASES BELOW ARE THE ONLY ONES THAT REACH THE FAST PATH.
		// `IncrementalLoad` redirects to `fullDirLoad` the moment any changed
		// scan key names a file below the conf.d root (`anyNestedKey`), so every
		// nested mutation above compares fullDirLoad against fullDirLoad and
		// cannot see a defect in the incremental code at all. Measured: deleting
		// the round-5 refused-set refresh from `IncrementalLoad` reddened
		// exactly one of the eight nested cases and none of the others. Root-
		// level mutations are what actually exercise the path.
		{"edit a root tenant file", func(t *testing.T, dir string) {
			writeTestYAML(t, filepath.Join(dir, "root-tenant.yaml"),
				"tenants:\n  t-root:\n    mysql_connections: 33\n")
		}},
		{"add a root tenant file", func(t *testing.T, dir string) {
			writeTestYAML(t, filepath.Join(dir, "root-tenant-2.yaml"), "tenants:\n  t-root-2: {}\n")
		}},
		{"delete a root tenant file", func(t *testing.T, dir string) {
			if err := os.Remove(filepath.Join(dir, "root-tenant.yaml")); err != nil {
				t.Fatalf("Remove: %v", err)
			}
		}},
		{"remove a tenant from a root file that stays on disk", func(t *testing.T, dir string) {
			writeTestYAML(t, filepath.Join(dir, "root-tenant.yaml"), "tenants: {}\n")
		}},
		{"rename a tenant inside a root file", func(t *testing.T, dir string) {
			writeTestYAML(t, filepath.Join(dir, "root-tenant.yaml"), "tenants:\n  t-renamed: {}\n")
		}},
		{"edit an unrelated root yaml that declares nothing", func(t *testing.T, dir string) {
			writeTestYAML(t, filepath.Join(dir, "notes.yaml"), "unrelated: false\n")
		}},
		{"a tenant starts authoring a key it used to inherit", func(t *testing.T, dir string) {
			writeTestYAML(t, filepath.Join(dir, "finance", "us-east", "t-use.yaml"),
				"tenants:\n  t-use:\n    redis_evicted_keys: 99\n    mysql_connections: 11\n")
		}},
	}
}

func mustAbs(t *testing.T, p string) string {
	t.Helper()
	abs, err := filepath.Abs(p)
	if err != nil {
		t.Fatalf("Abs(%q): %v", p, err)
	}
	return filepath.Clean(abs)
}
