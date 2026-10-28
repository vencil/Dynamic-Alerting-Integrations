package config

import "time"

// fixedNow is the instant this package's tests resolve at when the instant
// itself is not what they test (#3368). pkg/config reads no wall clock, so
// every resolve is handed one, and a test never reads time.Now() for it: a
// `window:` is half-open [start, end) like Alertmanager's time_intervals, so
// "00:00-23:59" is shut for the whole 23:59 minute and a wall-clock test
// fails one run in 1440. 10:00 UTC is inside every "always on" window the
// tests write and outside none of them; a test about a window's edges pins
// its own instants.
var fixedNow = time.Date(2026, 8, 1, 10, 0, 0, 0, time.UTC)
