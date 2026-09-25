package handler

import (
	"fmt"
	"os"
	"testing"

	"github.com/vencil/tenant-api/internal/testutil"
)

// TestMain turns off git's background auto-maintenance before any test runs:
// these tests run git in t.TempDir() repos, and a maintenance process still
// writing .git/objects makes the TempDir cleanup fail. See
// testutil.DisableGitAutoMaintenance.
func TestMain(m *testing.M) {
	if err := testutil.DisableGitAutoMaintenance(); err != nil {
		fmt.Fprintln(os.Stderr, "TestMain:", err)
		os.Exit(2)
	}
	os.Exit(m.Run())
}
