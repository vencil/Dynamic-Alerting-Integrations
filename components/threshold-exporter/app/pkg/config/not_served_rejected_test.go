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

// TestRejectedShownCache_AnotherTenantMovesChainRejection (#2065
// follow-up): a chain file's refused key is recorded in RejectedChainValues
// only for a tenant under it that does not set the key itself. tx sets
// mysql_threads_running at the platform layer (a time-boxed unparseable
// value), so its own build records nothing for team/sub's mysql_cpu, yet its
// resolve names the chain file the winner. Adding tw — who does not set it —
// puts the key in the table and so changes tx's verdict, while every file
// tx's resolve reads is byte-identical. The cached answer must not outlive it.
func TestRejectedShownCache_AnotherTenantMovesChainRejection(t *testing.T) {
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
	before := map[string]map[string]string{"ty": {"mysql_connections": NotServedValueRejected}}
	if got := c.Shown(scan, built); !reflect.DeepEqual(got, before) {
		t.Fatalf("precondition: shown %v; want %v (tx a candidate through team/_defaults.yaml, nothing named)", got, before)
	}

	testutil.WriteTree(t, tmp, map[string]string{
		"conf.d/team/sub/tw.yaml": "tenants:\n  tw:\n    mysql_connections: \"70\"\n",
	})
	scan, built = build()
	var cold RejectedShownCache
	want := cold.Shown(scan, built)
	if want["tx"]["mysql_cpu"] != NotServedValueRejected {
		t.Fatalf("precondition: a cold resolve names tx/mysql_cpu %q; want %q (%v)",
			want["tx"]["mysql_cpu"], NotServedValueRejected, want)
	}
	if got := c.Shown(scan, built); !reflect.DeepEqual(got, want) {
		t.Errorf("after tw's file was added: cached %v; cold %v", got, want)
	}
}
