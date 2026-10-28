package main

import "time"

// fixedNow is the instant this package's tests hand buildCheckInput and the
// library resolves when the instant itself is not what they test (#3368):
// pkg/config reads no wall clock. A test that runs the CLI and needs a
// `window:` open pins it with served-values' --at.
var fixedNow = time.Date(2026, 8, 1, 10, 0, 0, 0, time.UTC)
