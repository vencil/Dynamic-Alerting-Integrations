// Package logprojection decides which tenants' platform logs Vector copies
// into their own VictoriaLogs partition, and writes that decision into
// Vector's configuration (ADR-021 §平台副本與租戶副本, #4212).
//
// A tenant is projected when it holds an AccountID in tenant-api's ledger
// (`_account_registry.yaml`) AND its conf.d config turns `_log_projection` to
// `enable`. The ledger keeps an id for a tenant that has left (ids are never
// reused), and Backfill gives every tenant one, so holding an id alone is not
// a request to be projected: the conf.d flag is.
//
// Every input is read by its one reader: the ledger by account.Parse, a
// tenant's visibility selection by logvis.Parse (both what `tenant-api state
// show` prints), and conf.d by the exporter's loader (config.LoadDir), whose
// tenant map already holds the root platform file's `tenants:` entry and the
// profile the tenant names, under the tenant file's own values. This package
// only joins them.
//
// What Vector does with the result is not written here. The template is the
// Helm-rendered Vector config with placeholders (Render); this package fills
// data into them and writes no VRL and no routing condition of its own.
package logprojection

import (
	"errors"
	"fmt"
	"sort"

	"github.com/vencil/tenant-api/internal/federation/account"
	"github.com/vencil/tenant-api/internal/federation/logvis"
	"github.com/vencil/tenant-api/internal/statestore"
	cfg "github.com/vencil/threshold-exporter/pkg/config"
	"github.com/vencil/threshold-exporter/pkg/tenantid"
)

// FlagKey is the conf.d key that turns a tenant's projection on (registered
// in components/threshold-exporter/app/pkg/config/reserved_keys.json, read by
// `log_projection`).
const FlagKey = "_log_projection"

// The words FlagKey takes. Anything else (another spelling, a boolean, a
// mapping) leaves the tenant unprojected and is reported: the projection
// writes a tenant's rows into a store it can read, so only the one word
// turns it on. docs/schemas/tenant-config.schema.json carries the same
// enum, pinned by TestFlagWordsMatchTheSchema.
const (
	FlagEnable  = "enable"
	FlagDisable = "disable"
)

// FlagOf is the one reading of a FlagKey value: on for `enable`, off for
// `disable` or a null (no value), and ok=false for anything else. A reserved
// key has no schedule (#3269): only the default is read.
func FlagOf(sv cfg.ScheduledValue) (enabled, ok bool) {
	switch sv.Default {
	case FlagEnable:
		return true, true
	case FlagDisable, "":
		return false, true
	}
	return false, false
}

// Projection is one projected tenant.
type Projection struct {
	TenantID  string
	AccountID uint32
	// VisibleFields is the tenant's own selection (logvis.Selection.Fields,
	// the selectable fields it keeps): nil when it made none, which keeps
	// every field the platform curates.
	VisibleFields *[]string
}

// Finding is a tenant this run did not project although something asks for
// it; it is reported, never fatal.
type Finding struct {
	TenantID string
	// Reason is one of the Reason* constants.
	Reason string
}

// The reasons of a Finding.
const (
	// ReasonFlagUnread: FlagKey holds a value FlagOf does not read.
	ReasonFlagUnread = "flag_unread"
	// ReasonNoAccount: the flag is on, but the ledger holds no AccountID
	// for the tenant (it has not been given one yet).
	ReasonNoAccount = "no_account"
	// ReasonTenantIDInvalid: the tenant's id breaks the tenant-id rule
	// (pkg/tenantid). conf.d's loader admits any `tenants:` key, and the id
	// is written into Vector's config, which interpolates `$NAME` before it
	// parses anything (VECTOR_DANGEROUSLY_ALLOW_ENV_VAR_INTERPOLATION), so
	// such a tenant is never projected.
	ReasonTenantIDInvalid = "tenant_id_invalid"
)

// Result is what Compute decided.
type Result struct {
	// Projections, sorted by AccountID.
	Projections []Projection
	Findings    []Finding
}

// ErrInput is wrapped by every error that means an input could not be
// used: the caller keeps the last result it could use (Runner).
var ErrInput = errors.New("log projection input unusable")

// Compute joins the state records (key → bytes, as a state snapshot or a
// backup holds them) with the loaded conf.d config. An unusable ledger or
// visibility record is an error, never "no projection": a damaged record
// must not silently drop or widen a tenant's copy.
//
// A state without the ledger is an error when some tenant's flag is on: a
// ledger that is not there is not a ledger without the tenant, and reading it
// as one would drop every projection (and forget every AccountID the runner
// has seen) on a read of the wrong store.
func Compute(state map[string][]byte, c *cfg.ThresholdConfig) (Result, error) {
	var reg *account.Registry
	if data, ok := state[statestore.KeyAccountRegistry]; ok {
		r, err := account.Parse(data)
		if err != nil {
			return Result{}, fmt.Errorf("%w: %s: %v", ErrInput, statestore.KeyAccountRegistry, err)
		}
		reg = r
	}
	var res Result
	tenants := make([]string, 0, len(c.Tenants))
	for t := range c.Tenants {
		tenants = append(tenants, t)
	}
	sort.Strings(tenants)
	for _, t := range tenants {
		sv, set := c.Tenants[t][FlagKey]
		if !set {
			continue
		}
		on, ok := FlagOf(sv)
		if !ok {
			res.Findings = append(res.Findings, Finding{TenantID: t, Reason: ReasonFlagUnread})
			continue
		}
		if !on {
			continue
		}
		if !tenantid.Valid(t) {
			res.Findings = append(res.Findings, Finding{TenantID: t, Reason: ReasonTenantIDInvalid})
			continue
		}
		if reg == nil {
			return Result{}, fmt.Errorf("%w: %s is not in the state, and tenant %s turns %s on",
				ErrInput, statestore.KeyAccountRegistry, t, FlagKey)
		}
		id, held := reg.Lookup(t)
		if !held {
			res.Findings = append(res.Findings, Finding{TenantID: t, Reason: ReasonNoAccount})
			continue
		}
		p := Projection{TenantID: t, AccountID: id}
		if data, ok := state[statestore.LogVisibilityKey(t)]; ok {
			sel, err := logvis.Parse(data)
			if err != nil {
				return Result{}, fmt.Errorf("%w: %s: %v", ErrInput, statestore.LogVisibilityKey(t), err)
			}
			if v := logvis.Validate(sel); len(v) > 0 {
				return Result{}, fmt.Errorf("%w: %s: %s %s", ErrInput, statestore.LogVisibilityKey(t), v[0].Field, v[0].Reason)
			}
			p.VisibleFields = sel.Fields
		}
		res.Projections = append(res.Projections, p)
	}
	sort.Slice(res.Projections, func(i, j int) bool {
		return res.Projections[i].AccountID < res.Projections[j].AccountID
	})
	return res, nil
}

// Conflicts lists, for each projection of next, why it contradicts seen
// (every tenant → AccountID ever written): a tenant whose id moved, or an id
// written for another tenant before. The ledger never moves or reuses an id,
// so either means the ledger read is not the ledger, and writing it would
// send rows into another tenant's partition.
func Conflicts(seen map[string]uint32, next []Projection) []string {
	owner := make(map[uint32]string, len(seen))
	for t, id := range seen {
		owner[id] = t
	}
	var out []string
	for _, p := range next {
		if id, ok := seen[p.TenantID]; ok && id != p.AccountID {
			out = append(out, fmt.Sprintf("tenant %s had AccountID %d, now %d", p.TenantID, id, p.AccountID))
		}
		if t, ok := owner[p.AccountID]; ok && t != p.TenantID {
			out = append(out, fmt.Sprintf("AccountID %d belonged to tenant %s, now %s", p.AccountID, t, p.TenantID))
		}
	}
	sort.Strings(out)
	return out
}
