package handler

// tenant_api_writes_total{result} (#3054): conf.d write attempts by result.
//
// Before #3054 the family had no label and nothing called its increment, so it
// read 0 however much tenant-api wrote.
//
// What one attempt is: one write action against the config repository or the
// forge — one call of a gitops.Writer write (Write, WriteIfUnchanged,
// WritePRChecked, WritePRBatch, WriteBatch) together with, in PR write-back
// mode, the PR/MR it opens. A batch is ONE attempt however many tenants it
// names (it is one commit, or one PR/MR with one commit). Where they are
// counted:
//
//   - PUT /tenants/{id}, direct mode           (PutTenant)
//   - PUT /tenants/{id}, PR/MR mode            (putTenantPRMode)
//   - PUT /tenants/{id}/custom-alerts          (PutTenantCustomAlerts; direct only)
//   - POST /tenants/batch and /groups/{id}/batch, PR/MR mode (runBatchPR)
//   - the same two, direct mode, sync and ?async=true (Deps.executeBatch;
//     an async batch is counted when its task finishes, not on the 202)
//
// An async batch task that panics is not counted: it never reaches the count
// in executeBatch, and TenantApiAsyncTaskPanicked already covers it.
//
// The records in tenant-api's state store (groups, views, the federation
// whitelist and subsets, the AccountID registry) are not conf.d and not git;
// their writes are not counted here.
//
// The result is the answer the attempt got (writeResultOfStatus):
//
//   - ok       — 2xx: written, PR/MR opened, or nothing to change
//   - conflict — 409: the repository or the tenant's file is not in the state
//     the write needs (moved under it, base hash stale, tenant claimed by
//     another file); refresh and retry
//   - error    — 5xx or 429: the server could not finish it (git, the forge,
//     the write plane, the route generator unable to judge)
//
// Any other 4xx is not counted: the request itself was refused (invalid
// content, refused by the route generator's findings, a forbidden write
// scope) — that is the caller's to fix, not the write plane failing. The
// checks that refuse a request before the writer is called are not attempts
// either. One answer says less than it means and is counted as error: the
// 403 a forge answers when tenant-api's own token may not open the PR/MR. (A
// direct write's git or file failure used to be another, a 400; since #1535
// it is the 500 it is — writeTenantWriteFailure.) A direct batch whose commit
// landed but did not finish cleanly (409, committed: true) is an error too —
// though its commit is written.
//
// An attempt that fails after its client went away is not counted: when the
// request's own context is cancelled (r.Context().Err() is context.Canceled,
// clientGone), the 400, 503 or 5xx it ends with says the client left, not that
// the write plane failed. A write that still succeeded is counted ok. A
// deadline (context.DeadlineExceeded) is the server's and still counts.

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"sync/atomic"
)

// writeResult is the result label of one write attempt.
type writeResult int

const (
	writeResultOK writeResult = iota
	writeResultConflict
	writeResultError
	numWriteResults
)

// writeResultLabels is the fixed label set, in exposition order. Every series
// is rendered from process start (value 0), so rate() and the
// TenantApiWriteErrorsHigh ratio always have the series they divide.
var writeResultLabels = [numWriteResults]string{"ok", "conflict", "error"}

// WriteResults holds one counter per result.
type WriteResults struct {
	counters [numWriteResults]atomic.Int64
}

func (m *WriteResults) record(res writeResult) { m.counters[res].Add(1) }

// Snapshot returns the counts keyed by result label.
func (m *WriteResults) Snapshot() map[string]int64 {
	out := make(map[string]int64, numWriteResults)
	for i, l := range writeResultLabels {
		out[l] = m.counters[i].Load()
	}
	return out
}

// processWriteResults is what /metrics renders and every Deps without its own
// WriteResults counts into.
var processWriteResults atomic.Pointer[WriteResults]

func init() { processWriteResults.Store(&WriteResults{}) }

func (d *Deps) writeResults() *WriteResults {
	if d != nil && d.WriteResults != nil {
		return d.WriteResults
	}
	return processWriteResults.Load()
}

// writeResultOfStatus maps the HTTP status a write attempt was answered with
// to its result, or reports false for a status that is not counted.
func writeResultOfStatus(status int) (writeResult, bool) {
	switch {
	case status >= 200 && status < 300:
		return writeResultOK, true
	case status == http.StatusConflict:
		return writeResultConflict, true
	case status == http.StatusTooManyRequests, status >= 500:
		return writeResultError, true
	}
	return 0, false
}

// writeAttempt wraps the ResponseWriter of one write attempt from the writer
// call on and counts the attempt by the status it is answered with (end).
//
// The status is what WriteHeader sets, and only that: like statusWriter it
// does not override Write, which goes straight to the writer underneath. Every
// exit of a wrapped attempt answers through writeJSON (WriteErrorEnvelope and
// the helpers over it funnel there), which calls WriteHeader before Write. An
// answer written with Write alone (an implicit 200) leaves the status 0 and is
// counted as an error, as no answer at all is — so a new exit must call
// WriteHeader (TestWriteAttempt_CountsOnlyWriteHeader).
type writeAttempt struct {
	http.ResponseWriter
	sink   *WriteResults
	client context.Context // the request's own context (clientGone)
	status int
	fault  bool
	ended  bool
}

// startWriteAttempt begins counting one write attempt of request r answered
// through rw. The caller answers through the returned writer and defers end.
func (d *Deps) startWriteAttempt(rw http.ResponseWriter, r *http.Request) *writeAttempt {
	return &writeAttempt{ResponseWriter: rw, sink: d.writeResults(), client: r.Context()}
}

// Unwrap lets http.ResponseController reach the writer underneath (flush,
// deadlines, hijack).
func (a *writeAttempt) Unwrap() http.ResponseWriter { return a.ResponseWriter }

// clientGone reports whether ctx — a request's own context — was cancelled:
// the client went away. nil (no client) is never gone.
func clientGone(ctx context.Context) bool {
	return ctx != nil && errors.Is(ctx.Err(), context.Canceled)
}

func (a *writeAttempt) WriteHeader(code int) {
	if a.status == 0 {
		a.status = code
	}
	a.ResponseWriter.WriteHeader(code)
}

// answered records the status of an answer the caller writes itself, after
// end (runBatchPR returns its body and status to the endpoint).
func (a *writeAttempt) answered(status int) {
	if a.status == 0 {
		a.status = status
	}
}

// end counts the attempt, once. An attempt that ends with no answer at all
// (a panic on its way out) is an error. One that did not succeed after its
// client went away is not counted (clientGone).
func (a *writeAttempt) end() {
	if a.ended {
		return
	}
	a.ended = true
	res, counted := writeResultOfStatus(a.status)
	if a.fault || a.status == 0 {
		res, counted = writeResultError, true
	}
	if !counted || (res != writeResultOK && clientGone(a.client)) {
		return
	}
	a.sink.record(res)
}

// markWriteFault makes the write attempt answered through w count as an error
// whatever its status: the server failed, though the status says otherwise.
// A no-op for a writer that is not a writeAttempt.
func markWriteFault(w http.ResponseWriter) {
	if a, ok := w.(*writeAttempt); ok {
		a.fault = true
	}
}

func writeWriteResultMetrics(w io.Writer) {
	m := processWriteResults.Load()
	_, _ = fmt.Fprintf(w, "# HELP tenant_api_writes_total Write attempts to the config repository by result: one per git commit or PR/MR attempt (a batch counts once). ok = 2xx, conflict = 409, error = server-side failure: 5xx/429, the forge refusing tenant-api's own token (403), a batch committed but not finished cleanly (409 committed: true; its commit is written). Requests refused as invalid or forbidden, and failures after the client went away, are not counted.\n")
	_, _ = fmt.Fprintf(w, "# TYPE tenant_api_writes_total counter\n")
	for i, l := range writeResultLabels {
		_, _ = fmt.Fprintf(w, "tenant_api_writes_total{result=%q} %d\n", l, m.counters[i].Load())
	}
}
