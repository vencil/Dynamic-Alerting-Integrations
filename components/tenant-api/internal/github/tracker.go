package github

import (
	"time"

	"github.com/vencil/tenant-api/internal/platform"
)

// Tracker is the GitHub-flavored pending-PR cache. The implementation
// is shared via platform.PollingTracker — github.Tracker exists only
// as a typed alias so call sites can write `*github.Tracker` and stay
// importing this package. See platform/tracker.go for the design.
type Tracker = platform.PollingTracker

// NewTracker creates a PR tracker that polls the given GitHub client.
// `syncInterval` below 10s is clamped to 30s by the underlying
// platform.PollingTracker (see its doc).
func NewTracker(client *Client, syncInterval time.Duration) *Tracker {
	return platform.NewPollingTracker(client.ListOpenPRs, "github", syncInterval)
}
