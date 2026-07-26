package token

import (
	"log/slog"
	"time"
)

// ── ADR-028 D1 evidence-channel liveness canary (#1234) ───────────────────────
//
// WHY THIS EXISTS. ADR-028's un-revoke detection rests on a chain the reconciler
// cannot see the far end of: tenant-api logs `federation_token_revoked` → Vector
// tails tenant-api → the `federation_evidence` transform → VictoriaLogs → the
// reconciler's LogsQL query. Every link is silent when it breaks, and the
// reconciler's healthy state and its blind state produce the SAME observation:
// zero rows. "No revocations happened" and "the evidence channel is severed" are
// indistinguishable, and the blind case still publishes a clean all-clear with a
// fresh last_reconcile_ts, so no alert fires.
//
// (Today the breakage happens to be LOUD for two unrelated reasons — the pinned
// da-tools:v2.9.0 image does not contain the reconciler script at all, and until
// #1234's Vector work landed the VictoriaLogs NetworkPolicy dropped the
// reconciler's queries — so `FederationRevocationReconcileStale` fires. That is
// exactly the point: once those are fixed the failure mode becomes the silent
// false-green above, and this canary is what keeps it detectable.)
//
// A canary makes the two states distinguishable: emit a content-free event on a
// fixed cadence down the SAME transform and sink path as real evidence, and the
// reconciler can assert liveness independently of whether any revocation
// occurred. An empty window is then unambiguously a broken channel
// (`federation_revocation_channel_up` → 0 → FederationRevocationEvidenceChannelDown).
//
// Rejected alternatives (owner decision, 2026-07-26):
//   - a SYNTHETIC REVOCATION canary (periodically revoke a throwaway token):
//     writes to the single-writer store on a timer, needs a canary tenant id
//     (violating the tenant-agnostic rule), and a CronJob driving it has zero
//     failure detection of its own (#1242).
//   - RECONCILER-SIDE INFERENCE ("the revoked set is non-empty but the log
//     returned nothing"): proves nothing when the revoked set is legitimately
//     empty, and false-fires during the documented ≤4h post-deploy ramp.

// heartbeatEvent is the `event` field value the canary emits. It is ALSO listed
// in the Vector chart's `evidenceChannel.events` allowlist and queried by the
// reconciler's build_heartbeat_query — renaming it here alone severs the channel
// while every component still looks healthy, which is the failure mode this
// canary exists to detect. (helm/vector/values.yaml and
// scripts/tools/ops/_federation_revocation_reconciler.py are the other two
// copies of this string.)
const heartbeatEvent = "federation_revocation_channel_heartbeat"

// DefaultHeartbeatInterval is the canary cadence.
//
// Chosen against the detection side's windows: the reconciler reconciles every
// 300s and queries a 30m heartbeat window, so ~5-6 heartbeats fall inside any
// one window. A single missed emission (pod restart, a scheduling hiccup, one
// log line lost) therefore cannot drive `channel_up` to 0 — only a genuinely
// severed channel can. Raising this toward the 30m window removes that margin;
// lowering it just adds log volume for no extra signal.
const DefaultHeartbeatInterval = 5 * time.Minute

// Heartbeater emits the ADR-028 evidence-channel liveness canary.
//
// It is deliberately dependency-free: it must exercise the LOG path only, and
// must not touch the store, the API server, or anything that could make the
// canary fail for a reason unrelated to the channel it is testing.
type Heartbeater struct {
	// logger, when nil, falls back to slog.Default() — the same per-instance
	// seam configMapStore uses, so a test captures the emission into a buffer
	// without a global slog swap (CLAUDE.md §測試注入 Seam).
	logger *slog.Logger
}

// NewHeartbeater returns a Heartbeater logging through slog.Default(), which in
// production is the JSON handler on stderr installed by main.configureLogger.
func NewHeartbeater() *Heartbeater { return &Heartbeater{} }

// log returns the heartbeater's logger, defaulting to slog.Default().
func (h *Heartbeater) log() *slog.Logger {
	if h.logger != nil {
		return h.logger
	}
	return slog.Default()
}

// emit writes one canary event.
//
// The payload is deliberately minimal, and matches the revoke event's style:
//   - NO timestamp field of our own — the slog JSON handler already emits
//     `time`, and Vector's own event timestamp is what the sink uses as
//     `_time`. A second, hand-rolled one would be a third clock to keep
//     consistent.
//   - NO tenant identifier, and nothing tenant-derived (ADR-028 §D3 PII
//     minimisation): this event lands in a 30-day audit store, so the canary
//     must not be the thing that turns that store into a customer-identifier
//     store. heartbeat_test.go pins the absence.
//
// ⚠️ Info level is load-bearing: tenant-api running at Warn+ silently filters
// this out (the same documented constraint as the revocation event itself), and
// the reconciler would then report a dead channel. That is a true positive about
// the evidence channel, not a false alarm.
func (h *Heartbeater) emit() {
	h.log().Info("federation revocation evidence channel heartbeat",
		"event", heartbeatEvent)
}

// Run emits once immediately, then every interval, until stopCh closes.
//
// The immediate first emission matters on a rollout: without it the channel
// would read "down" for a whole interval after every restart, and the alert's
// 15m window would be spent on a self-inflicted gap rather than on real ones.
//
// A non-positive interval falls back to DefaultHeartbeatInterval with a warning.
// Two failure modes are being avoided, and neither is acceptable for a control
// whose whole job is to not fail silently:
//   - `time.NewTicker(0)` PANICS, so `--federation-heartbeat-interval=0` (or any
//     negative value) would crash tenant-api at startup — a config typo taking
//     down the API server.
//   - Treating 0 as "disable" would silently remove the canary. The alert would
//     eventually fire, so it is not literally silent, but on-call would be
//     chasing a severed evidence channel that is actually a values typo.
func (h *Heartbeater) Run(interval time.Duration, stopCh <-chan struct{}) {
	if interval <= 0 {
		h.log().Warn("federation heartbeat interval is not positive; falling back to the default (a zero/negative ticker would panic, and disabling the canary would hide the evidence channel)",
			"requested", interval, "using", DefaultHeartbeatInterval)
		interval = DefaultHeartbeatInterval
	}
	h.emit()
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for {
		select {
		case <-stopCh:
			return
		case <-ticker.C:
			h.emit()
		}
	}
}
