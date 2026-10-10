package config

// effective_scan_test.go — ResolveEffectiveFromScan (#1977): its answer on a
// WARM scan (a prior, ReleaseData'd, every file on the mtime fast-path) and
// on a cold one is the answer of the whole-tree build it replaces, byte for
// byte; it reads only the files the tenant's answer depends on; and it
// refuses bytes the scan did not hash.

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"sort"
	"testing"
	"time"

	"github.com/vencil/threshold-exporter/internal/testutil"
)

// resolveEffectiveWholeBuild is ResolveEffective as it was before #1977: the
// resolver over the whole cold scan, NotServed from the build of the WHOLE
// tree. The reference ResolveEffectiveFromScan must equal.
func resolveEffectiveWholeBuild(configDir, tenantID string) (*EffectiveConfig, error) {
	scan, err := ScanDirTree(configDir, nil, nil, discardLogger)
	if err != nil {
		return nil, err
	}
	r := newEffectiveResolver(scan)
	if _, lerr := scan.Locate(tenantID); lerr == nil {
		built, berr := loadDirBuild(scan, scan.AbsRoot, discardLogger, discardLogger.Printf)
		if berr != nil {
			return nil, berr
		}
		r.served = newServedVerdicts(&built, false)
	}
	ec, err := r.resolve(tenantID)
	return ec.AsWritten(), err
}

// effectiveScanCorpus is the inline trees: every layer /effective reports
// and every way the tree can be broken BESIDE the tenant asked for.
func effectiveScanCorpus() map[string]map[string]string {
	return map[string]map[string]string{
		"chain and siblings": {
			"_defaults.yaml":          "defaults:\n  mysql_connections: 80\n  redis_memory: 5\n",
			"a/_defaults.yaml":        "defaults:\n  mysql_connections: 70\n",
			"a/b/_defaults.yaml":      "defaults:\n  redis_memory: 6\n  only_here: 3\n",
			"a/b/t1.yaml":             "tenants:\n  t1:\n    mysql_connections: \"65\"\n",
			"a/t2.yaml":               "tenants:\n  t2: {}\n",
			"c/_defaults.yaml":        "defaults:\n  mysql_connections: abc\n",
			"c/t3.yaml":               "tenants:\n  t3: {}\n",
			"c/_DEFAULTS.YML":         "defaults:\n  mysql_connections: 1\n",
			"a/b/_defaults.yml":       "defaults:\n  redis_memory: 9\n",
			"a/_notes.yaml":           "anything: [1, 2]\n",
			"a/b/shared.yaml":         "tenants:\n  t4: {}\n  t5:\n    redis_memory: \"8\"\n",
			"a/b/c/d/t6.yaml":         "tenants:\n  t6: {}\n",
			"a/b/c/_defaults.yaml":    "defaults: [\n",
			"a/b/c/d/_defaults.yaml":  "defaults:\n  mysql_connections: 10\n",
			"zz/_defaults.yaml":       "defaults:\n  mysql_connections: 99\n",
			"zz/t7.yaml":              "tenants:\n  t7: {mysql_connections: \"1\"}\n",
			"zz/nested/deeper/t8.yml": "tenants:\n  t8: {}\n",
		},
		"platform tenants and profiles": {
			"_defaults.yaml": "defaults:\n  mysql_connections: 80\n  redis_x: 5\ntenants:\n  t1:\n    mysql_connections: \"60\"\n    _silent_mode: warning\n",
			"_profiles.yaml": "profiles:\n  strict:\n    redis_x: \"11\"\n    pg_connections_critical: \"190\"\ntenants:\n  t2:\n    _profile: strict\n  ghost: {redis_x: \"1\"}\n",
			"_platform.yaml": "tenants:\n  t1:\n    redis_x: \"9\"\n",
			"t1.yaml":        "tenants:\n  t1:\n    redis_x: \"7\"\n    _profile: strict\n",
			"t2.yaml":        "tenants:\n  t2: {}\n",
			"sub/t3.yaml":    "tenants:\n  t3:\n    _profile: nosuch\n",
		},
		"not served reasons": {
			"_defaults.yaml":     "defaults:\n  mysql_connections: 30\n  redis_memory: null\n",
			"sub/_defaults.yaml": "defaults:\n  mysql_connections: 40\n  pg_x: abc\n  sub_only: 4\n",
			"sub/t1.yaml":        "tenants:\n  t1:\n    mysql_connections:\n      <<: {default: \"55\"}\n",
			"sub/t2.yaml": "tenants:\n  t2:\n    mysql_connections:\n      default: \"60\"\n" +
				"      overrides:\n        - window: \"01:00~09:00\"\n          value: \"1000\"\n",
			"t3.yaml": "tenants:\n  t3:\n    redis_memory: 60\n",
		},
		"root value not a number": {
			"_defaults.yaml": "defaults:\n  mysql_connections: abc\n",
			"t1.yaml":        "tenants:\n  t1: {}\n",
		},
		"root unwrapped": {
			"_defaults.yaml": "pg_connections: 100\n",
			"t1.yaml":        "tenants:\n  t1: {}\n",
			"t2.yaml":        "extra:\n  k: 1\n  k: 2\ntenants:\n  t2:\n    cpu: \"80\"\n",
		},
		"chain parse failed": {
			"_defaults.yaml":     "defaults:\n  mysql_connections: 80\n",
			"sub/_defaults.yaml": "defaults: [\n",
			"sub/t1.yaml":        "tenants:\n  t1: {}\n",
		},
		"root parse failed": {
			"_defaults.yaml":     "defaults: [\n",
			"sub/_defaults.yaml": "defaults:\n  mysql_connections: 40\n",
			"sub/t1.yaml":        "tenants:\n  t1: {}\n",
		},
		"duplicate elsewhere": {
			// The exporter installs no inheritance graph for a tree with a
			// duplicate anywhere: sub_only is then no undeliverable verdict.
			"_defaults.yaml":     "defaults:\n  mysql_connections: 80\n",
			"sub/_defaults.yaml": "defaults:\n  mysql_connections: 40\n  sub_only: 4\n",
			"sub/t1.yaml":        "tenants:\n  t1: {}\n",
			"t2.yaml":            "tenants:\n  t2: {}\n",
			"other/t2.yaml":      "tenants:\n  t2: {}\n",
		},
		"duplicate of the tenant": {
			"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
			"t1.yaml":        "tenants:\n  t1: {}\n",
			"t1.yml":         "tenants:\n  t1: {mysql_connections: \"3\"}\n",
		},
		"broken tenant file elsewhere": {
			"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
			"t1.yaml":        "tenants:\n  t1: {}\n",
			"t2.yaml":        "tenants: [\n",
			"t3.yaml":        "tenants:\n  t3: 5\n",
		},
		"tenant decode error": {
			"_defaults.yaml":     "defaults:\n  mysql_connections: 80\n",
			"sub/_defaults.yaml": "defaults:\n  mysql_connections: {a: [}\n",
			"sub/t1.yaml":        "tenants:\n  t1: {}\n",
		},
	}
}

// goldenCorpus is the repo's golden fixture trees (tests/golden/fixtures/*/conf.d).
func goldenCorpus(t *testing.T) map[string]string {
	t.Helper()
	dirs, err := filepath.Glob(filepath.Join("..", "..", "..", "..", "..", "tests", "golden", "fixtures", "*", "conf.d"))
	if err != nil || len(dirs) == 0 {
		t.Fatalf("no golden fixture tree found (err=%v)", err)
	}
	out := make(map[string]string, len(dirs))
	for _, d := range dirs {
		out["golden/"+filepath.Base(filepath.Dir(d))] = d
	}
	return out
}

// copyTree copies src into a fresh temp dir (mtimes are reset below).
func copyTree(t *testing.T, src string) string {
	t.Helper()
	dst := t.TempDir()
	err := filepath.Walk(src, func(p string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		rel, _ := filepath.Rel(src, p)
		target := filepath.Join(dst, rel)
		if info.IsDir() {
			return os.MkdirAll(target, 0o755)
		}
		if !info.Mode().IsRegular() {
			return nil
		}
		b, err := os.ReadFile(p)
		if err != nil {
			return err
		}
		return os.WriteFile(target, b, 0o644)
	})
	if err != nil {
		t.Fatal(err)
	}
	return dst
}

// ageTree sets every file's mtime well past TreeScanMtimeGuard, so a scan
// with a prior takes the fast-path for each.
func ageTree(t *testing.T, root string) {
	t.Helper()
	past := time.Now().Add(-time.Hour)
	err := filepath.Walk(root, func(p string, info os.FileInfo, err error) error {
		if err != nil || info.IsDir() {
			return err
		}
		return os.Chtimes(p, past, past)
	})
	if err != nil {
		t.Fatal(err)
	}
}

// warmScanOf is the scan tenant-api's Writer.walkTree hands over on a quiet
// tree: a cold scan, ReleaseData'd as the prior, then a scan with it, also
// ReleaseData'd. Fails the test unless every file took the fast-path.
func warmScanOf(t *testing.T, root string) *TreeScan {
	t.Helper()
	prior, err := ScanDirTree(root, nil, nil, discardLogger)
	if err != nil {
		t.Fatal(err)
	}
	prior.ReleaseData()
	warm, err := ScanDirTree(root, prior, nil, discardLogger)
	if err != nil {
		t.Fatal(err)
	}
	warm.ReleaseData()
	for k, f := range warm.Files {
		if !f.Reused && !f.ParseFailed {
			t.Fatalf("%s: not on the fast-path; the warm comparison would not test a warm scan", k)
		}
	}
	return warm
}

// effectiveAnswer renders a resolve as the comparable bytes: the JSON body,
// or the error text, plus the error's class.
func effectiveAnswer(ec *EffectiveConfig, err error) string {
	if err != nil {
		var dup *DuplicateTenantError
		var de *DecodeError
		class := "other"
		switch {
		case errors.Is(err, ErrTenantNotFound):
			class = "not-found"
		case errors.As(err, &dup):
			class = "duplicate"
		case errors.As(err, &de):
			class = "decode"
		}
		return "error(" + class + "): " + err.Error()
	}
	b, merr := json.Marshal(struct {
		*EffectiveConfig
		EC any `json:"effective_config"`
	}{ec, NonFiniteAsText(ec.EffectiveConfig)})
	if merr != nil {
		return "marshal: " + merr.Error()
	}
	return string(b)
}

// tenantsOf is every tenant the cold scan attributes (duplicates included),
// plus one it does not.
func tenantsOf(t *testing.T, root string) []string {
	t.Helper()
	scan, err := ScanDirTree(root, nil, nil, discardLogger)
	if err != nil {
		t.Fatal(err)
	}
	seen := map[string]bool{"t-absent": true}
	for _, f := range scan.Files {
		for _, id := range f.TenantIDs {
			seen[id] = true
		}
	}
	ids := make([]string, 0, len(seen))
	for id := range seen {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	return ids
}

// ⛔ The correctness floor of #1977: for every tenant of every tree, the
// answer on a warm scan and on a cold one is the whole-tree build's answer,
// byte for byte (body, or error class and text).
func TestResolveEffectiveFromScan_MatchesWholeTreeBuild(t *testing.T) {
	t.Parallel()
	trees := map[string]string{}
	for name, files := range effectiveScanCorpus() {
		dir := t.TempDir()
		testutil.WriteTree(t, dir, files)
		trees[name] = dir
	}
	for name, dir := range goldenCorpus(t) {
		trees[name] = copyTree(t, dir)
	}
	for name, root := range trees {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			ageTree(t, root)
			warm := warmScanOf(t, root)
			for _, id := range tenantsOf(t, root) {
				want := effectiveAnswer(resolveEffectiveWholeBuild(root, id))
				cold := effectiveAnswer(ResolveEffective(root, id))
				got := effectiveAnswer(ResolveEffectiveFromScan(warm, id))
				if cold != want {
					t.Errorf("%s cold:\n got  %s\n want %s", id, cold, want)
				}
				if got != want {
					t.Errorf("%s warm:\n got  %s\n want %s", id, got, want)
				}
			}
		})
	}
}

// The corpus must reach what it claims to cover: a NotServed of every class
// the inline trees were written for, a ChainParseFailed, a platform and a
// profile overlay, a 409 and a 404 — otherwise the equality above holds
// vacuously for the fields that matter.
func TestResolveEffectiveFromScan_CorpusCoversEveryField(t *testing.T) {
	t.Parallel()
	seen := map[string]bool{}
	for _, files := range effectiveScanCorpus() {
		dir := t.TempDir()
		testutil.WriteTree(t, dir, files)
		for _, id := range tenantsOf(t, dir) {
			ec, err := ResolveEffective(dir, id)
			var dup *DuplicateTenantError
			switch {
			case errors.Is(err, ErrTenantNotFound):
				seen["404"] = true
			case errors.As(err, &dup):
				seen["409"] = true
			case err != nil:
				seen["error"] = true
			default:
				for _, ns := range ec.NotServed {
					seen[ns.Reason] = true
				}
				if len(ec.ChainParseFailed) > 0 {
					seen["chain_parse_failed"] = true
				}
				if len(ec.PlatformOverlay) > 0 {
					seen["platform_overlay"] = true
				}
				if len(ec.ProfileOverlay) > 0 {
					seen["profile_overlay"] = true
				}
			}
		}
	}
	for _, k := range []string{"404", "409", "error", "chain_parse_failed", "platform_overlay", "profile_overlay",
		NotServedParseFailed, NotServedRootDefaultsUnwrapped, NotServedValueRejected, NotServedValueUnparsed,
		NotServedWindowInvalid, NotServedUndeliverable, NotServedRootNullUndeclared} {
		if !seen[k] {
			t.Errorf("corpus reaches no %q", k)
		}
	}
}

// Bytes the scan did not hash are refused, not answered: a tenant file
// rewritten after the walk, or removed, is ErrScanStale.
func TestResolveEffectiveFromScan_StaleBytesAreRefused(t *testing.T) {
	t.Parallel()
	for name, mutate := range map[string]func(string) error{
		"rewritten": func(p string) error { return os.WriteFile(p, []byte("tenants:\n  t1: {x: \"2\"}\n"), 0o644) },
		"removed":   os.Remove,
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			dir := t.TempDir()
			testutil.WriteTree(t, dir, map[string]string{
				"_defaults.yaml": "defaults:\n  x: 1\n",
				"t1.yaml":        "tenants:\n  t1: {x: \"3\"}\n",
			})
			ageTree(t, dir)
			warm := warmScanOf(t, dir)
			if err := mutate(filepath.Join(dir, "t1.yaml")); err != nil {
				t.Fatal(err)
			}
			if _, err := ResolveEffectiveFromScan(warm, "t1"); !errors.Is(err, ErrScanStale) {
				t.Fatalf("err = %v, want ErrScanStale", err)
			}
		})
	}
}

// The scan passed in is a walker's retained prior: the resolve must not
// write to it (bytes, partials), or the next walk would read them.
func TestResolveEffectiveFromScan_LeavesTheScanUntouched(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	testutil.WriteTree(t, dir, effectiveScanCorpus()["platform tenants and profiles"])
	ageTree(t, dir)
	warm := warmScanOf(t, dir)
	if _, err := ResolveEffectiveFromScan(warm, "t1"); err != nil {
		t.Fatal(err)
	}
	if warm.Partials != nil {
		t.Errorf("Partials = %v, want nil", warm.Partials)
	}
	for k, f := range warm.Files {
		if f.Data != nil {
			t.Errorf("%s: Data filled in on the caller's scan", k)
		}
	}
}
