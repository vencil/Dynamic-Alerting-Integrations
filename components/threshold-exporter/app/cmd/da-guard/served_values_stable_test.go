package main

// #3323: the same tree must give the same served-values output on every run.
//
// tests/golden/fixtures/mixed-mode/conf.d carries a tenant (`tenant-keys`)
// whose `_x` is a mapping with two timestamp keys. Before the fix, yaml.v3 left
// those two keys in Go's map-iteration order, and at least one run in fifteen
// printed them the other way round (2 of 30 measured from the CLI); at that
// rate 100 identical runs in a row happen by chance about once in a thousand.
// Reverting the fix turned this test red in 40 of 40 runs.

import (
	"bytes"
	"path/filepath"
	"strings"
	"testing"
)

func TestServedValues_MixedModeTree_SameOutputEveryRun(t *testing.T) {
	t.Parallel()
	dir := filepath.Join(goldenDir(t), "fixtures", "mixed-mode", "conf.d")
	args := []string{"--config-dir", dir, "--at", "2026-07-01T00:00:00Z"}

	run := func() string {
		var out, errOut bytes.Buffer
		if c := runServedValues(args, &out, &errOut); c != exitOK {
			t.Fatalf("served-values exit %d: %s", c, errOut.String())
		}
		return out.String()
	}

	first := run()
	// Guard against the fixture drifting away from the shape this test is for:
	// without two timestamp keys there is nothing for the order to depend on.
	if !strings.Contains(first, `2026-12-31T00:00:00Z:`) || !strings.Contains(first, `2026-12-31T10:20:30.5+08:00: zoned`) {
		t.Fatalf("fixture no longer has tenant-keys `_x` with two timestamp keys; output:\n%s", first)
	}
	for i := 1; i < 100; i++ {
		if got := run(); got != first {
			t.Fatalf("run %d differs from run 0 for the same tree and --at", i)
		}
	}
}
