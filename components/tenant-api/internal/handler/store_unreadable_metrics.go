package handler

// tenant-api's own copy of conf.d (#2939 D3c). An entry the server cannot
// read and git cannot hold — a FIFO, socket or device named like a config
// file, a failed read, a sub-directory that cannot be listed, a failed stat
// that is not an unresolvable link — refuses every write with 500
// TENANT_STORE_FAULT (gitops.ErrTenantStoreFault). Nothing a client sends
// can clear it, so it is exposed here for the operator:
//
//   - tenant_api_store_unreadable_entries{reason} — the store faults of
//     conf.d by the walker's reason (special_file, read_error, walk_error,
//     stat_error), walked at scrape time — classification only, with a
//     minimum interval (gitops.Writer.StoreUnreadable). Alert
//     TenantApiStoreUnreadable on > 0.
//   - tenant_api_store_scan_last_success_timestamp_seconds — when that walk
//     last succeeded. Alert TenantApiStoreScanStale when it is old or absent
//     while tenant-api is up.
//
// ⛔ UNKNOWN IS NOT ZERO. Until a walk has succeeded neither family is
// exported (Prometheus convention: no sample when the value is not known),
// so "not measured" can never read as "no fault"; a walk that fails later
// keeps the last counts and does NOT move the timestamp.

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

// storeUnreadableSource is what the gauges read: *gitops.Writer.
type storeUnreadableSource interface {
	StoreUnreadable() gitops.StoreScan
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

// writeStoreUnreadableMetricsFrom renders both families from src — nothing
// at all when src is nil (no Writer installed) or no walk has succeeded.
func writeStoreUnreadableMetricsFrom(w io.Writer, src storeUnreadableSource) {
	if src == nil {
		return
	}
	scan := src.StoreUnreadable()
	if !scan.OK {
		return
	}
	_, _ = fmt.Fprintf(w, "# HELP tenant_api_store_unreadable_entries Entries of tenant-api's own conf.d copy the server cannot read and git cannot hold (a FIFO/socket/device named like a config file, a failed read, an unlistable directory, a failed stat that is not an unresolvable link), by reason, from the last successful scrape-time walk (with a minimum interval; absent until one succeeds). While > 0 every write answers 500 TENANT_STORE_FAULT. Alert on > 0.\n")
	_, _ = fmt.Fprintf(w, "# TYPE tenant_api_store_unreadable_entries gauge\n")
	for _, reason := range storeUnreadableReasons {
		_, _ = fmt.Fprintf(w, "tenant_api_store_unreadable_entries{reason=%q} %d\n", reason, scan.Counts[reason])
	}
	_, _ = fmt.Fprintf(w, "# HELP tenant_api_store_scan_last_success_timestamp_seconds Unix time of the last successful walk behind tenant_api_store_unreadable_entries (absent until one succeeds; a failed walk does not move it). Alert when old or absent while tenant-api is up.\n")
	_, _ = fmt.Fprintf(w, "# TYPE tenant_api_store_scan_last_success_timestamp_seconds gauge\n")
	_, _ = fmt.Fprintf(w, "tenant_api_store_scan_last_success_timestamp_seconds %d\n", scan.LastSuccess.Unix())
}
