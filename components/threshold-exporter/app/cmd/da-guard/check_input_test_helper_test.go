package main

import (
	"testing"
	"time"

	"github.com/vencil/threshold-exporter/internal/guard"
	"github.com/vencil/threshold-exporter/pkg/config"
)

// checkInputOf is buildCheckInput for a test's fixed tree: a tree changed
// during the run is no case of these tests.
func checkInputOf(t *testing.T, scoped *config.ScopedTenants, f *flags, now time.Time) guard.CheckInput {
	t.Helper()
	in, err := buildCheckInput(scoped, f, now)
	if err != nil {
		t.Fatal(err)
	}
	return in
}
