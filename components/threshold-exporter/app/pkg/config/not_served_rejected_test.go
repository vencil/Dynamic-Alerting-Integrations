package config

import (
	"os"
	"path/filepath"
	"reflect"
	"testing"

	"github.com/vencil/threshold-exporter/internal/testutil"
)

// TestRejectedShownCache_FailedResolveIsNotCached (#2065 r4): a tenant whose
// file changed after the scan is skipped; once the file is back to
// the scanned bytes (same composite, same fingerprint), the next call
// resolves it again and names its value — a cached failure would hide it
// for good.
func TestRejectedShownCache_FailedResolveIsNotCached(t *testing.T) {
	t.Parallel()
	tmp := t.TempDir()
	const tenant = "tenants:\n  tx:\n    mysql_threads_running: \"31\"\n"
	testutil.WriteTree(t, tmp, map[string]string{
		"conf.d/_defaults.yaml":      "defaults:\n  mysql_connections: 80\n  mysql_threads_running: 30\n",
		"conf.d/team/_defaults.yaml": "defaults:\n  mysql_connections:\n    default: \"70\"\n    overrides: \"01:00-09:00\"\n",
		"conf.d/team/t.yaml":         tenant,
	})
	root := filepath.Join(tmp, "conf.d")
	tfile := filepath.Join(root, "team", "t.yaml")
	warm := func() (*TreeScan, *FlatBuild) {
		t.Helper()
		scan, err := ScanDirTree(root, nil, nil, discardLogger)
		if err != nil {
			t.Fatal(err)
		}
		built, err := loadDirBuild(scan, scan.AbsRoot, discardLogger, discardLogger.Printf)
		if err != nil {
			t.Fatal(err)
		}
		scan.ReleaseData() // as the exporter's warm scan: bytes read back from disk
		return scan, &built
	}
	want := map[string]map[string]string{"tx": {"mysql_connections": NotServedValueRejected}}

	var c RejectedShownCache
	scan, built := warm()
	if err := os.WriteFile(tfile, []byte(tenant+"    mysql_connections_critical: \"90\"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	before := c.Resolves
	if got := c.Shown(scan, built); got != nil || c.Resolves != before+1 {
		t.Fatalf("file changed after the scan: shown %v, resolves %d -> %d; want tx tried and not shown",
			got, before, c.Resolves)
	}
	if err := os.WriteFile(tfile, []byte(tenant), 0o600); err != nil {
		t.Fatal(err)
	}
	scan2, built2 := warm()
	if scan2.Composite != scan.Composite {
		t.Fatalf("precondition: the restored tree must scan to the same composite")
	}
	before = c.Resolves
	if got := c.Shown(scan2, built2); !reflect.DeepEqual(got, want) {
		t.Errorf("file back to the scanned bytes: shown %v; want %v", got, want)
	}
	if c.Resolves != before+1 {
		t.Errorf("resolves %d -> %d: want the skipped tenant resolved again", before, c.Resolves)
	}
}

// TestRejectedShownCache_AnotherTenantDoesNotMoveTheVerdict (#2065
// follow-up): a tenant's value_rejected verdict depends on its own inputs
// only. mysql_cpu is an alias (the retired #1231 spelling) of
// mysql_threads_running; tx sets mysql_threads_running at the platform
// layer (a time-boxed unparseable value), so the build — which asks "the
// tenant sets this threshold" under any spelling — refuses team/sub's
// mysql_cpu for tw but not for tx. Keyed by file alone, the table named tx
// as soon as tw (who does not set it) was added, though every file tx's
// resolve reads was byte-identical: tx must be named on neither tree, and
// the cached answer after adding tw must equal a cold one.
func TestRejectedShownCache_AnotherTenantDoesNotMoveTheVerdict(t *testing.T) {
	t.Parallel()
	tmp := t.TempDir()
	testutil.WriteTree(t, tmp, map[string]string{
		"conf.d/_defaults.yaml":          "defaults:\n  mysql_connections: 80\n",
		"conf.d/_platform.yaml":          "tenants:\n  tx:\n    mysql_threads_running: {default: \"abc\", expires: \"2099-07-01T06:00:00Z\", reason: r}\n",
		"conf.d/team/_defaults.yaml":     "defaults:\n  mysql_connections:\n    default: \"70\"\n    overrides: \"01:00-09:00\"\n",
		"conf.d/team/ty.yaml":            "tenants:\n  ty:\n    mysql_threads_running: \"30\"\n",
		"conf.d/team/sub/_defaults.yaml": "defaults:\n  mysql_cpu:\n    default: \"33\"\n    overrides: \"01:00-09:00\"\n",
		"conf.d/team/sub/tx.yaml":        "tenants:\n  tx:\n    mysql_connections: \"70\"\n",
	})
	root := filepath.Join(tmp, "conf.d")
	build := func() (*TreeScan, *FlatBuild) {
		t.Helper()
		scan, err := ScanDirTree(root, nil, nil, discardLogger)
		if err != nil {
			t.Fatal(err)
		}
		built, err := loadDirBuild(scan, scan.AbsRoot, discardLogger, discardLogger.Printf)
		if err != nil {
			t.Fatal(err)
		}
		return scan, &built
	}

	var c RejectedShownCache
	scan, built := build()
	wantA := map[string]map[string]string{"ty": {"mysql_connections": NotServedValueRejected}}
	if got := c.Shown(scan, built); !reflect.DeepEqual(got, wantA) {
		t.Errorf("without tw: shown %v; want %v", got, wantA)
	}

	testutil.WriteTree(t, tmp, map[string]string{
		"conf.d/team/sub/tw.yaml": "tenants:\n  tw:\n    mysql_connections: \"70\"\n",
	})
	scan, built = build()
	var cold RejectedShownCache
	wantB := map[string]map[string]string{
		"ty": {"mysql_connections": NotServedValueRejected},
		"tw": {"mysql_cpu": NotServedValueRejected},
	}
	got := cold.Shown(scan, built)
	if !reflect.DeepEqual(got, wantB) {
		t.Errorf("with tw, cold: shown %v; want %v (tx named on neither tree)", got, wantB)
	}
	if cached := c.Shown(scan, built); !reflect.DeepEqual(cached, got) {
		t.Errorf("with tw: cached %v; cold %v", cached, got)
	}
}
