// Package async tests use goleak to detect goroutine leaks.
//
// TaskManager spawns N worker goroutines on NewManager(N) and joins them
// in Close(). If any test forgets to call Close() — or Close() races and
// returns before goroutines exit — goleak.VerifyTestMain fails the
// package's test binary with a stack trace of the leaked goroutines.
//
// This is the architectural defense suggested by the test methodology
// audit (P1-6 in the testing-quality plan): treat goroutine lifetime as
// a first-class invariant in async/ instead of relying on individual
// tests to spot leaks during local debugging.
package async

import (
	"testing"

	"go.uber.org/goleak"
)

func TestMain(m *testing.M) {
	goleak.VerifyTestMain(m)
}
