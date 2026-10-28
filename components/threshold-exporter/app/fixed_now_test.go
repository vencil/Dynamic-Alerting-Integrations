package main

import "time"

// fixedNow is the instant this package's tests resolve at when the instant
// itself is not what they test (#3368): pkg/config reads no wall clock, so a
// resolve is handed one, and a scrape resolves at the manager's clock
// (ConfigManager.Now) — a test whose assertion needs a `window:` open sets
// that clock (SetClock) instead of reading time.Now(). A window is half-open
// [start, end) like Alertmanager's time_intervals, so "00:00-23:59" is shut
// for the whole 23:59 minute and a wall-clock test fails one run in 1440.
var fixedNow = time.Date(2026, 8, 1, 10, 0, 0, 0, time.UTC)
