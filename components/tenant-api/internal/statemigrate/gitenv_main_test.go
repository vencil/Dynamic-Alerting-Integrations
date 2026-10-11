package statemigrate

import (
	"fmt"
	"os"
	"testing"

	"github.com/vencil/tenant-api/internal/testutil"
)

// TestMain turns off git's background auto-maintenance before any test runs,
// as every package whose tests may run git does (see
// testutil.DisableGitAutoMaintenance and its package guard).
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
