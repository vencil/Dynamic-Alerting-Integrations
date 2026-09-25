package federation

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
	cleanup, err := testutil.DisableGitAutoMaintenance()
	if err != nil {
		fmt.Fprintln(os.Stderr, "TestMain:", err)
		os.Exit(2)
	}
	code := m.Run()
	cleanup()
	os.Exit(code)
}
