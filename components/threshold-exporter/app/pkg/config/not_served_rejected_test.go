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
	want := map[string]map[string]string{"tx": {"mysql_connections": "team/_defaults.yaml"}}

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
