package gitlab

import (
	"time"

	"github.com/vencil/tenant-api/internal/platform"
)

// Tracker is the GitLab-flavored pending-MR cache. The implementation
// is shared via platform.PollingTracker — gitlab.Tracker exists only
// as a typed alias so call sites can write `*gitlab.Tracker` and stay
// importing this package. See platform/tracker.go for the design.
type Tracker = platform.PollingTracker

// NewTracker creates an MR tracker that polls the given GitLab client.
// `syncInterval` below 10s is clamped to 30s by the underlying
// platform.PollingTracker (see its doc).
func NewTracker(client *Client, syncInterval time.Duration) *Tracker {
	return platform.NewPollingTracker(client.ListOpenPRs, "gitlab", syncInterval)
}
