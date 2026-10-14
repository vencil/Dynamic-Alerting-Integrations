package handler

// tenant-api's own copy of conf.d (#2939 D3c). An entry the server cannot
// read and git cannot hold — a FIFO, socket or device named like a config
// file, a failed read, a sub-directory that cannot be listed, a failed stat
// that is not a dangling link — refuses every write with 500
// TENANT_STORE_FAULT (gitops.ErrTenantStoreFault). Nothing a client sends
// can clear it, so it is exposed here for the operator:
//
//   - tenant_api_store_unreadable_entries{reason} — the store faults of the
//     last conf.d walk tenant-api completed (a write, a preview, a read of
//     /effective walks it), by the walker's reason: special_file,
//     read_error, walk_error, stat_error. 0 for every reason before the
//     first walk. Alert TenantApiStoreUnreadable on > 0.

import (
	"fmt"
	"io"
	"sync/atomic"

	"github.com/vencil/tenant-api/internal/gitops"
	cfg "github.com/vencil/threshold-exporter/pkg/config"
)

// storeUnreadableReasons are the label values, always emitted, in order.
var storeUnreadableReasons = []string{
	cfg.UnreadableSpecialFile, cfg.UnreadableReadError, cfg.UnreadableWalkError, cfg.UnreadableStatError,
}

// storeUnreadableSource is what the gauge reads: *gitops.Writer.
type storeUnreadableSource interface {
	StoreUnreadable() (map[string]int, bool)
}

var activeStoreUnreadable atomic.Pointer[gitops.Writer]

// SetStoreUnreadableSource installs the Writer /metrics reads the gauge
// above from. Called once at startup.
func SetStoreUnreadableSource(w *gitops.Writer) { activeStoreUnreadable.Store(w) }

func writeStoreUnreadableMetrics(w io.Writer) {
	var src storeUnreadableSource
	if wr := activeStoreUnreadable.Load(); wr != nil {
		src = wr
	}
	writeStoreUnreadableMetricsFrom(w, src)
}

// writeStoreUnreadableMetricsFrom renders the family from src (nil: no
// Writer installed, every reason 0).
func writeStoreUnreadableMetricsFrom(w io.Writer, src storeUnreadableSource) {
	var counts map[string]int
	if src != nil {
		counts, _ = src.StoreUnreadable()
	}
	_, _ = fmt.Fprintf(w, "# HELP tenant_api_store_unreadable_entries Entries of tenant-api's own conf.d copy the server cannot read and git cannot hold (a FIFO/socket/device named like a config file, a failed read, an unlistable directory, a failed stat that is not a dangling link), by reason, as of the last conf.d walk (a write, a preview or /effective). While > 0 every write answers 500 TENANT_STORE_FAULT; repair the volume or reset the copy from git. Alert on > 0.\n")
	_, _ = fmt.Fprintf(w, "# TYPE tenant_api_store_unreadable_entries gauge\n")
	for _, reason := range storeUnreadableReasons {
		_, _ = fmt.Fprintf(w, "tenant_api_store_unreadable_entries{reason=%q} %d\n", reason, counts[reason])
	}
}
