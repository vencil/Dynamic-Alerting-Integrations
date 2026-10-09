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
// that one field. But the field was not special: `incrementalLoadFrom` rebuilds
// SOME of what `fullDirLoad` publishes, and every piece it forgets is a fast
// path that silently disagrees with a restart. Asserting the whole published
// state against a from-scratch load of the same tree makes "which fields did
// you remember" stop being a judgement call.
//
// ⛔ TWO BASE TREES (#1577). The reload is the watch path's (watchReload), and
// that reaches the fast path only for a tree with no `_defaults` carrier — on
// the carrier tree every mutation below is a hierarchical reload (a full flat
// rebuild), which is what production runs there and is still worth pinning
// against a restart. The flat tree is the same layout without a carrier
// anywhere, and is the one that exercises incrementalLoadFrom; mutations that
// write or delete a carrier are skipped on it (they would flip it
// hierarchical, i.e. test the other tree again).
func TestIncrementalLoadLandsWhereAFullLoadWould(t *testing.T) {
	t.Parallel()
	bases := []struct {
		name  string
		build func(t *testing.T, dir string)
		flat  bool
	}{
		{"carrier tree", buildPairBaseTree, false},
		{"flat tree", buildPairFlatTree, true},
	}
	for _, base := range bases {
		for _, mut := range incrementalMutations() {
			if base.flat && mut.touchesCarrier {
				continue
			}
			base, mut := base, mut
			t.Run(base.name+"/"+mut.name, func(t *testing.T) {
				t.Parallel()
				pairMutationAgrees(t, base.build, base.flat, mut)
			})
		}
	}
}

// pairMutationAgrees is one cell of TestIncrementalLoadLandsWhereAFullLoadWould.
func pairMutationAgrees(t *testing.T, build func(t *testing.T, dir string), flat bool, mut incrMutation) {
	// Path A: load the tree, mutate it, reload as the watch path does.
	warm := t.TempDir()
	build(t, warm)
	mIncr := NewConfigManager(warm)
	if err := mIncr.Load(); err != nil {
		t.Fatalf("warm Load: %v", err)
	}
	if flat {
		requireFlatWatchPath(t, mIncr)
	}
	mut.apply(t, warm)
	if err := watchReload(mIncr); err != nil {
		t.Fatalf("watchReload: %v", err)
	}

	// Path B: the same mutated tree, loaded from scratch — what an
	// operator gets by restarting the exporter.
	cold := t.TempDir()
	build(t, cold)
	mut.apply(t, cold)
	mFull := NewConfigManager(cold)
	if err := mFull.Load(); err != nil {
		t.Fatalf("cold Load: %v", err)
	}

	got := publishedStateFingerprint(t, mIncr, warm)
	want := publishedStateFingerprint(t, mFull, cold)
	if got != want {
		t.Fatalf("after %s the reload disagrees with a restart\n--- reload ---\n%s\n--- full ---\n%s",
			mut.name, got, want)
	}
}

// TestAnUnparseableFileMovesBothPlanesTogether pins that, on the incremental
// path, a tenant whose file stops parsing is in /effective's population
// (`tenantSources`) exactly when it is in /metrics' (the merged config).
//
// ⛔ UNTIL #1957 THIS TEST ASSERTED THE OPPOSITE, on purpose: the prune kept a
// broken file's tenant attributed after the merged config had dropped it, so
// the divergence audit's cause (a) — "absent from the merged config, still
// declared to the hierarchy" — could fire. With one decode
// (config.ParseConfigFile) a file that fails it declares no tenant on ANY
// plane, and the state cause (a) described is a bug, not a signal.
//
// ⛔ BOTH MERGE BRANCHES. The full-rebuild branch (forced here by touching
// `_profiles.yaml` in the same reload) and the tenant-only branch both drop
// the broken file's tenants, as a restart does. Until #1980 the tenant-only
// branch kept them (patchTenants' "keep the last good values") and this
// table's second leg asserted served=true on both planes; the owner removed
// that fail-safe, so both legs now assert the tenant is gone from both.
//
// ⚠️ THE TREE HAS NO `_defaults` CARRIER, AND MUST NOT (#1577). Both branches
// live in incrementalLoadFrom, which the watch path reaches only for a tree
// with no carrier; with one, every reload is the hierarchical path's full
// flat rebuild and the tenant-only branch never runs. The full-rebuild branch
// is forced with `_profiles.yaml` instead, a platform file that is not a
// carrier.
func TestAnUnparseableFileMovesBothPlanesTogether(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct {
		name       string
		touchPlat  bool // also edit _profiles.yaml → full-rebuild branch
		wantServed bool
	}{
		{"full-rebuild branch drops it from both", true, false},
		{"tenant-only branch drops it from both too (#1980)", false, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			dir := t.TempDir()
			writeTestYAML(t, filepath.Join(dir, "_profiles.yaml"), "profiles:\n  gold:\n    mysql_connections: \"80\"\n")
			writeTestYAML(t, filepath.Join(dir, "a.yaml"), "tenants:\n  t-a: {}\n")
			writeTestYAML(t, filepath.Join(dir, "b.yaml"), "tenants:\n  t-b: {}\n")

			m, _, logBuf := newAuditedManager(t, dir)
			if err := m.Load(); err != nil {
				t.Fatalf("Load: %v", err)
			}
			requireFlatWatchPath(t, m)
			logBuf.Reset()

			if tc.touchPlat {
				writeTestYAML(t, filepath.Join(dir, "_profiles.yaml"), "profiles:\n  gold:\n    mysql_connections: \"81\"\n")
			}
			writeTestYAML(t, filepath.Join(dir, "a.yaml"), "tenants:\n  t-a: {}\n  \tbroken: [\n")
			if err := watchReload(m); err != nil {
				t.Fatalf("reload: %v", err)
			}

			_, served := m.GetConfig().Tenants["t-a"]
			m.mu.RLock()
			_, attributed := m.hierarchy.tenantSources["t-a"]
			m.mu.RUnlock()
			if served != tc.wantServed {
				t.Fatalf("fixture precondition: merged config serves t-a = %v, want %v — "+
					"this leg no longer exercises the branch it is named for", served, tc.wantServed)
			}
			if attributed != served {
				t.Errorf("t-a: /metrics population has it = %v, /effective population has it = %v — "+
					"the two planes disagree about a tenant whose file failed the one decode (#1957)",
					served, attributed)
			}
			if strings.Contains(logBuf.String(), undeliverableAnchor) {
				t.Errorf("an ERROR for a state both planes agree on:\n%s", logBuf.String())
			}

			// The sibling assertion: a tenant whose file is GONE must be pruned.
			if err := os.Remove(filepath.Join(dir, "b.yaml")); err != nil {
				t.Fatalf("Remove: %v", err)
			}
			if err := watchReload(m); err != nil {
				t.Fatalf("reload: %v", err)
			}
			m.mu.RLock()
			_, stillThere := m.hierarchy.tenantSources["t-b"]
			m.mu.RUnlock()
			if stillThere {
				t.Fatal("t-b kept its source attribution after its file was deleted — /effective " +
					"would keep answering for a tenant /metrics no longer serves")
			}
		})
	}
}

// TestATenantDeclaredInTwoFilesSurvivesAnEditToEitherOne guards the removal
// loops against the question they were not actually asking.
//
// ⛔ `patchedTenants` ANSWERS "DID IT MOVE THIS ROUND", NOT "IS IT STILL
// DECLARED". A tenant named by two files at once is invalid — a full load
// hard-rejects it with DuplicateTenantError — but `incrementalLoadFrom`
// itself does not check, and when this test was written the
// `IncrementalLoad()` entry fed it such a scan unchecked. Once it was, both
// removal loops deleted the tenant the moment EITHER owning file was edited
// for any reason, because the other file did not change this round and so was
// absent from `patchedTenants`. The tenant left the merged config while a file
// on disk still declared it, and the divergence audit (cause "a", removed in
// #1957) blamed a parse failure — sending the operator to look for a line
// that does not exist.
//
// Measured on both loops, before the fix: `dup present after r2=false,
// divergenceERR=true`; after: `true / false`.
//
// ⛔ THE WATCH PATH NO LONGER REACHES THIS STATE, AND THE TEST REACHES IT ON
// PURPOSE (#1577). Since #2452 the watch path rejects a scan carrying a
// duplicate (TreeScan.Conflict) before incrementalLoadFrom sees it, and the
// unchecked `IncrementalLoad()` entry is gone. The removal loops' "is it still
// declared, and whose value" logic is unchanged and still the only thing
// standing between a two-source tenant and a silent drop, so this test feeds
// incrementalLoadFrom the duplicate scan directly, with Conflict cleared by
// hand (incrementalLoadAcceptingDuplicate) — the state the fix was written
// for, stated as the fixture it is rather than reached through a path that
// no longer exists.
func TestATenantDeclaredInTwoFilesSurvivesAnEditToEitherOne(t *testing.T) {
	t.Parallel()
	// ⛔ WHICH DECLARATION GOES AWAY IS LOAD-BEARING, and the first version of
	// this table missed it. `mergePartialConfigs` is last-writer-wins over
	// sorted filenames, so with a.yaml=11 and b.yaml=22 the live value is 22.
	// Editing a.yaml removes the LOSER: "keep whatever was there" and "re-take
	// from the survivor" both answer 22, and the mutation that keeps the orphan
	// value — the defect this test exists for — stayed green. Removing the
	// WINNER is the case that separates them.
	for _, tc := range []struct {
		name            string
		editFile        string // the file the operator edits
		removeWholeFile bool
	}{
		{"the loser declaration is removed from a file that stays", "a.yaml", false},
		{"the WINNER declaration is removed from a file that stays", "b.yaml", false},
		{"the winner's whole file is deleted", "b.yaml", true},
		{"the loser's whole file is deleted", "a.yaml", true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			// ⛔ THE TWO DECLARATIONS CARRY DIFFERENT VALUES. The first version
			// of this fixture wrote `dup: {}` in both files, so the test could
			// not see WHICH file's value survived — and a mutation that emptied
			// the tenant's overrides entirely passed it. Distinct values make
			// the assertion below about the number, not just the key.
			// A profile is part of the fixture so the tree exercises the
			// overlay at all.
			//
			// ⚠️ AN EARLIER VERSION OF THIS COMMENT CLAIMED ADDING IT MADE
			// BOTH SURVIVING-DECLARATION CASES FAIL UNTIL ApplyProfiles WAS
			// HOISTED. That was asserted, not measured, and it is false:
			// putting `ApplyProfiles` back inside the full-rebuild branch
			// leaves all four subtests PASS. The only test that catches that
			// defect is the randomised differential. Kept because the fixture
			// is more realistic with it, not because it guards anything.
			//
			// ⛔ NO `_defaults` CARRIER (#2593). incrementalLoadFrom refuses a
			// scan holding one — the watch path never hands it one — so a
			// carrier here would make every step below fail on that refusal
			// instead of exercising the removal loops. The tree used to have
			// one; it supplied root defaults no assertion reads. Measured
			// before and after removing it, with the two removal loops'
			// reclaim disabled (4/4 subtests red both times) and with the
			// survivor's value not re-taken (the two WINNER subtests red both
			// times).
			dir := t.TempDir()
			writeTestYAML(t, filepath.Join(dir, "_profiles.yaml"),
				"profiles:\n  gold:\n    mysql_slow_queries: \"95\"\n")
			writeTestYAML(t, filepath.Join(dir, "a.yaml"), "tenants:\n  dup:\n    _profile: gold\n    mysql_connections: \"11\"\n")
			writeTestYAML(t, filepath.Join(dir, "b.yaml"), "tenants:\n  other: {}\n")

			m, _, logBuf := newAuditedManager(t, dir)
			if err := m.Load(); err != nil {
				t.Fatalf("Load: %v", err)
			}

			// The invalid-but-live state: b.yaml now names dup as well, at 22.
			writeTestYAML(t, filepath.Join(dir, "b.yaml"), "tenants:\n  other: {}\n  dup:\n    _profile: gold\n    mysql_connections: \"22\"\n")
			if err := incrementalLoadAcceptingDuplicate(t, m, true); err != nil {
				t.Fatalf("incrementalLoadFrom (duplicate accepted by hand): %v", err)
			}
			// ⛔ ASSERTED, NOT SKIPPED. This was a t.Skip whose condition was
			// "dup left the merged config" — which is ALSO what several
			// regressions produce, and a skipped subtest prints nothing but
			// `ok` in CI. Worse: disabling the tenant-only fast path outright
			// made the test PASS, i.e. report green without executing
			// patchTenants at all. If the precondition ever stops holding
			// because the fast path learned to reject duplicates, this fails
			// loudly and someone deletes the test on purpose.
			if _, ok := m.GetConfig().Tenants["dup"]; !ok {
				t.Fatal("precondition gone: the fast path no longer accepts a cross-file duplicate. " +
					"If that is deliberate, delete this test — do not let it skip")
			}
			if !isTenantOnlyChange([]string{tc.editFile}, nil, nil) {
				t.Fatal("this fixture no longer takes the tenant-only fast path, so it exercises none of the code it names")
			}

			// b.yaml is untouched from here on and still declares dup.
			logBuf.Reset()
			if tc.removeWholeFile {
				if err := os.Remove(filepath.Join(dir, tc.editFile)); err != nil {
					t.Fatalf("Remove: %v", err)
				}
			} else {
				body := "tenants: {}\n"
				if tc.editFile == "b.yaml" {
					body = "tenants:\n  other: {}\n" // b.yaml also owns `other`
				}
				writeTestYAML(t, filepath.Join(dir, tc.editFile), body)
			}
			// The edit resolves the duplicate, so this scan has no Conflict;
			// it goes through the same helper so both steps feed
			// incrementalLoadFrom the same way.
			if err := incrementalLoadAcceptingDuplicate(t, m, false); err != nil {
				t.Fatalf("incrementalLoadFrom: %v", err)
			}

			got, ok := m.GetConfig().Tenants["dup"]
			if !ok {
				t.Fatal("dup was deleted from the merged config while b.yaml, untouched this round, still declares it — " +
					"its alerts stop firing until something forces a full reload")
			}
			// ⛔ AND IT MUST CARRY THE SURVIVING FILE'S VALUE. Merely surviving
			// is not enough: declining to delete left the tenant holding the
			// value of the declaration that just went away — a number no file
			// on disk says and a restart does not reproduce. Silent-and-wrong,
			// where the previous behaviour was at least loud-and-wrong.
			//
			// ⚠️ THE ORACLE IS A FULL LOAD OF THE SAME TREE, not a literal. The
			// first version of this assertion hardcoded the value of the file I
			// believed survived and named the wrong one — the fixture edits
			// a.yaml, so b.yaml is the survivor. A literal encodes my belief
			// about the fixture; a full reload encodes the tree.
			// ⛔ COPY WHAT IS THERE, NOT A LIST OF WHAT I REMEMBER PUTTING
			// THERE. A hardcoded filename list with a `continue` on missing
			// files is not a safeguard — it silently builds the oracle from a
			// DIFFERENT tree, and a tree missing the file that would have
			// exposed the bug agrees with the buggy fast path. Measured: with
			// `_profiles.yaml` absent from the list the test passed; with it
			// present it failed and named the real defect.
			fullDir := t.TempDir()
			entries, derr := os.ReadDir(dir)
			if derr != nil {
				t.Fatalf("ReadDir: %v", derr)
			}
			for _, e := range entries {
				if e.IsDir() {
					t.Fatalf("fixture grew a subdirectory (%s); the oracle copies files only", e.Name())
				}
				body, rerr := os.ReadFile(filepath.Join(dir, e.Name()))
				if rerr != nil {
					t.Fatalf("ReadFile %s: %v", e.Name(), rerr)
				}
				writeTestYAML(t, filepath.Join(fullDir, e.Name()), string(body))
			}
			reference := NewConfigManager(fullDir)
			if err := reference.Load(); err != nil {
				t.Fatalf("reference Load: %v", err)
			}
			want := reference.GetConfig().Tenants["dup"]["mysql_connections"].Default
			if v := got["mysql_connections"].Default; v != want {
				t.Fatalf("dup kept %q but a full reload of the same tree gives %q — "+
					"the fast path is holding the value of a declaration that no longer exists", v, want)
			}
			if strings.Contains(logBuf.String(), undeliverableAnchor) {
				t.Fatalf("an undeliverable ERROR was emitted for a tenant that is present in both planes\n--- log ---\n%s", logBuf.String())
			}
		})
	}
}

// incrementalLoadAcceptingDuplicate walks the tree as the watch path does and
// hands the scan to incrementalLoadFrom — with TreeScan.Conflict cleared, which
// the watch path never does (scanAndCheckHierarchical / detectChange reject
// it). wantConflict states which kind of scan the caller is feeding, so the
// fixture cannot quietly stop building the duplicate it exists for. Only the
// Conflict is cleared: a tree with a `_defaults` carrier is still refused by
// incrementalLoadFrom (#2593), so the fixture must be carrier-free.
func incrementalLoadAcceptingDuplicate(t *testing.T, m *ConfigManager, wantConflict bool) error {
	t.Helper()
	m.reloadMu.Lock()
	defer m.reloadMu.Unlock()
	m.mu.RLock()
	tree := m.flat.tree
	m.mu.RUnlock()
	scan, err := scanDirTree(m.path, tree, m.getMetrics(), m.getLogger())
	if err != nil {
		return err
	}
	if got := scan.Conflict != nil; got != wantConflict {
		t.Fatalf("scan.Conflict present = %v, want %v (%v) — the fixture no longer builds the state this step is for",
			got, wantConflict, scan.Conflict)
	}
	scan.Conflict = nil
	return m.incrementalLoadFrom(scan)
}

// TestReclaimTenantFromMirrorsTheFullMergePrecedence pins the ordering rule
// that no end-to-end fixture can reach.
//
// `reclaimTenantFrom` must resolve a tenant the way `mergePartialConfigs`
// does — sorted filename, last writer wins — so the fast path lands where a
// restart would. But a tree in which TWO files still declare the tenant after
// the edit has no "where a restart would land": a full load rejects it outright
// with DuplicateTenantError. Measured while trying to build that fixture. So
// the precedence is pinned here, directly, instead of being asserted through a
// tree that cannot exist. Without this, swapping last-writer for first-writer
// leaves the whole package green.
func TestReclaimTenantFromMirrorsTheFullMergePrecedence(t *testing.T) {
	t.Parallel()
	sv := func(v string) map[string]ScheduledValue {
		return map[string]ScheduledValue{"mysql_connections": {Default: v}}
	}
	configs := map[string]ThresholdConfig{
		"b.yaml":    {Tenants: map[string]map[string]ScheduledValue{"dup": sv("22")}},
		"a.yaml":    {Tenants: map[string]map[string]ScheduledValue{"dup": sv("11")}},
		"c.yaml":    {Tenants: map[string]map[string]ScheduledValue{"dup": sv("33")}},
		"only.yaml": {Tenants: map[string]map[string]ScheduledValue{"solo": sv("7")}},
	}
	if got, ok := reclaimTenantFrom(configs, indexTenantDeclarations(configs), "dup"); !ok || got["mysql_connections"].Default != "33" {
		t.Fatalf("dup resolved to %v (ok=%v), want c.yaml's 33 — sorted filename, LAST writer wins, "+
			"which is what mergePartialConfigs does", got, ok)
	}
	if got, ok := reclaimTenantFrom(configs, indexTenantDeclarations(configs), "solo"); !ok || got["mysql_connections"].Default != "7" {
		t.Fatalf("solo resolved to %v (ok=%v), want 7", got, ok)
	}
	if _, ok := reclaimTenantFrom(configs, indexTenantDeclarations(configs), "nobody"); ok {
		t.Fatal("a tenant no file declares must not be reported as surviving")
	}
}

// publishedStateFingerprint renders everything a loader publishes that a
// consumer can observe: the merged config, and the hierarchy state the
// commit-time audit reads. Paths are made root-relative so the two temp dirs
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

// buildPairFlatTree is buildPairBaseTree without a `_defaults` carrier
// anywhere: the same root and nested tenant files, so the watch path stays
// flat and reloads through incrementalLoadFrom (#1577). A `_profiles.yaml`
// stands in as the root platform file, with a `tenants:` block giving t-root a
// key its own file does not — the two-source shape the patch path must union.
func buildPairFlatTree(t *testing.T, dir string) {
	t.Helper()
	writeTestYAML(t, filepath.Join(dir, "_profiles.yaml"),
		"profiles:\n  gold:\n    redis_evicted_keys: 7\n"+
			"tenants:\n  t-root:\n    _profile: gold\n    mysql_connections: 81\n")
	writeTestYAML(t, filepath.Join(dir, "root-tenant.yaml"), "tenants:\n  t-root: {}\n")
	writeTestYAML(t, filepath.Join(dir, "notes.yaml"), "unrelated: true\n")

	mkSub(t, dir, "finance")
	writeTestYAML(t, filepath.Join(dir, "finance", "t-fin.yaml"), "tenants:\n  t-fin: {}\n")

	mkSub(t, filepath.Join(dir, "finance"), "us-east")
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
	// touchesCarrier: writes or deletes a `_defaults` carrier, so it has no
	// meaning on buildPairFlatTree (see TestIncrementalLoadLandsWhereAFullLoadWould).
	touchesCarrier bool
}

func incrementalMutations() []incrMutation {
	return []incrMutation{
		{"edit a nested tenant file", func(t *testing.T, dir string) {
			writeTestYAML(t, filepath.Join(dir, "finance", "us-east", "t-use.yaml"),
				"tenants:\n  t-use:\n    redis_evicted_keys: 123\n")
		}, false},
		{"edit a subtree defaults file", func(t *testing.T, dir string) {
			writeTestYAML(t, filepath.Join(dir, "finance", "_defaults.yaml"),
				"defaults:\n  mysql_connections: 65\n  finance_only_key: 5\n")
		}, true},
		{"edit the root defaults file", func(t *testing.T, dir string) {
			writeTestYAML(t, filepath.Join(dir, "_defaults.yaml"),
				"defaults:\n  mysql_connections: 85\n  redis_evicted_keys: 10\n"+
					"state_filters:\n  maintenance:\n    severity: warning\n")
		}, true},
		{"add a tenant in a brand new subdirectory", func(t *testing.T, dir string) {
			mkSub(t, dir, "ops")
			writeTestYAML(t, filepath.Join(dir, "ops", "_defaults.yaml"),
				"defaults:\n  mysql_connections: 40\n")
			writeTestYAML(t, filepath.Join(dir, "ops", "t-ops.yaml"), "tenants:\n  t-ops: {}\n")
		}, true},
		{"delete a nested tenant file", func(t *testing.T, dir string) {
			if err := os.Remove(filepath.Join(dir, "finance", "us-east", "t-use.yaml")); err != nil {
				t.Fatalf("Remove: %v", err)
			}
		}, false},
		{"delete a subtree defaults file", func(t *testing.T, dir string) {
			if err := os.Remove(filepath.Join(dir, "finance", "us-east", "_defaults.yaml")); err != nil {
				t.Fatalf("Remove: %v", err)
			}
		}, true},
		{"add a subtree-only key that cannot be delivered", func(t *testing.T, dir string) {
			writeTestYAML(t, filepath.Join(dir, "finance", "us-east", "_defaults.yaml"),
				"defaults:\n  mysql_connections: 70\n  another_subtree_only_key: 7\n")
		}, true},
		// ⛔ THE CASES BELOW ARE THE ONLY ONES THAT REACH THE FAST PATH, and
		// only on the flat tree (#1577: on the carrier tree nothing does).
		// incrementalLoadFrom redirects to `fullDirLoadFrom` the moment any
		// changed scan key names a file below the conf.d root (`anyNestedKey`),
		// so every nested mutation above compares a full load against a full
		// load and cannot see a defect in the incremental code at all.
		// Measured when this was the removed `IncrementalLoad()`: deleting the
		// round-5 refused-set refresh from it reddened exactly one of the eight
		// nested cases and none of the others. Root-level mutations are what
		// actually exercise the path.
		{"edit a root tenant file", func(t *testing.T, dir string) {
			writeTestYAML(t, filepath.Join(dir, "root-tenant.yaml"),
				"tenants:\n  t-root:\n    mysql_connections: 33\n")
		}, false},
		{"add a root tenant file", func(t *testing.T, dir string) {
			writeTestYAML(t, filepath.Join(dir, "root-tenant-2.yaml"), "tenants:\n  t-root-2: {}\n")
		}, false},
		{"delete a root tenant file", func(t *testing.T, dir string) {
			if err := os.Remove(filepath.Join(dir, "root-tenant.yaml")); err != nil {
				t.Fatalf("Remove: %v", err)
			}
		}, false},
		{"remove a tenant from a root file that stays on disk", func(t *testing.T, dir string) {
			writeTestYAML(t, filepath.Join(dir, "root-tenant.yaml"), "tenants: {}\n")
		}, false},
		{"rename a tenant inside a root file", func(t *testing.T, dir string) {
			writeTestYAML(t, filepath.Join(dir, "root-tenant.yaml"), "tenants:\n  t-renamed: {}\n")
		}, false},
		{"edit an unrelated root yaml that declares nothing", func(t *testing.T, dir string) {
			writeTestYAML(t, filepath.Join(dir, "notes.yaml"), "unrelated: false\n")
		}, false},
		{"a tenant starts authoring a key it used to inherit", func(t *testing.T, dir string) {
			writeTestYAML(t, filepath.Join(dir, "finance", "us-east", "t-use.yaml"),
				"tenants:\n  t-use:\n    redis_evicted_keys: 99\n    mysql_connections: 11\n")
		}, false},
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
