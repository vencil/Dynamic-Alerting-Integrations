package account

// Startup integrity guard + the shared conf.d tenant enumeration (#609 / ADR-021,
// Gemini #2).
//
// Threat: the registry file is truncated to 0 bytes (a botched mount, an
// interrupted write, a disk-full). Parse() treats blank input as a brand-new
// Day-0 file and primes a FRESH registry at the reserved floor (newRegistry,
// next_account_id=1000) — correct for a genuine Day-0, but catastrophic for a
// CORRUPTED one: the first logs-token issuance after the truncation re-hands id
// 1000 to whatever tenant asks first, silently re-issuing ids the (now-erased)
// ledger had already given other tenants → cross-tenant log leak. The runtime
// allocator's fail-closed checks cannot catch this, because a blank file is
// indistinguishable from Day-0 at the allocator level.
//
// The distinguisher is the FLEET: a blank/missing registry is legitimate ONLY
// when no tenant exists yet. If conf.d already holds ≥1 tenant, a blank registry
// is corruption — those tenants were onboarded against a ledger that is now
// gone. So tenant-api refuses to start (fail-loud) in that state rather than
// boot into silent re-issuance. The account package KEEPS its blank→newRegistry
// behaviour (genuine Day-0 must just work); this guard runs once at startup and
// uses fleet-nonemptiness to tell Day-0 from corruption.

import (
	"context"
	"fmt"
	"sort"
	"strings"

	"github.com/vencil/tenant-api/internal/confd"
	"github.com/vencil/tenant-api/internal/statestore"
)

// ListTenantIDs returns the tenant IDs in configDir — one per non-hidden,
// non-`_`-prefixed *.yaml / *.yml file. This mirrors the enumeration
// ListTenants and the threshold-exporter loader use, so callers see exactly the
// set of files that count as tenants (and skip _defaults.yaml, _groups.yaml, the
// _account_registry.yaml itself, etc.).
//
// Shared by the backfill handler and the startup integrity guard; the
// "what counts as a tenant file" rule itself lives once in package confd,
// which every scanner and ValidateTenantID share (no copy to drift), and so
// does the enumeration loop itself (confd.ListTenantFiles, #1680).
//
// ⛔ Content-blind on purpose: a tenant whose file is broken (malformed YAML,
// dangling symlink) is still a tenant that was onboarded against the
// registry, so it must still count toward "the fleet is non-empty". Whether
// the file is usable is confd.ReadTenantFile's question, which this caller
// deliberately does not ask.
func ListTenantIDs(configDir string) ([]string, error) {
	files, err := confd.ListTenantFiles(configDir)
	if err != nil {
		return nil, err
	}
	ids := make([]string, 0, len(files))
	for _, f := range files {
		ids = append(ids, f.ID)
	}
	return ids, nil
}

// VerifyRegistryNotResetWithFleet is the startup self-check for the
// blank-registry-but-fleet-nonempty corruption (Gemini #2). It returns a
// non-nil error — for the caller to treat as FATAL — when the AccountID
// registry record in the state store is blank/missing BUT conf.d already
// holds ≥1 tenant.
//
// Decision table:
//   - registry present and non-blank        → OK (nil). Parse's own
//     fail-closed checks guard its content at issuance time.
//   - registry blank/missing, fleet EMPTY   → OK (nil). Genuine Day-0.
//   - registry blank/missing, fleet NON-EMPTY → ERROR: the ledger those
//     tenants were allocated against is gone; booting would re-issue ids.
//
// An unreadable conf.d or state store is an error too (a misconfigured mount
// or RBAC must not boot silently). Run it after statemigrate.Import, so a
// registry still in its old conf.d home has been carried over first.
func VerifyRegistryNotResetWithFleet(ctx context.Context, configDir string, s statestore.Store) error {
	tenants, err := ListTenantIDs(configDir)
	if err != nil {
		return fmt.Errorf("account: cannot enumerate tenants in %q for registry integrity check: %w", configDir, err)
	}
	if len(tenants) == 0 {
		return nil
	}
	data, ok, err := s.Get(ctx, RegistryFileName)
	switch {
	case err != nil:
		return fmt.Errorf("account: cannot read %s from %s for registry integrity check: %w", RegistryFileName, s.Describe(), err)
	case !ok:
		return registryResetError(s.Describe(), len(tenants), "is missing")
	case isBlank(data):
		return registryResetError(s.Describe(), len(tenants), "is blank (0 bytes / whitespace only)")
	}
	return nil
}

// registryResetError formats the fail-loud message for a blank/missing registry
// alongside a non-empty fleet, naming the remediation.
func registryResetError(store string, tenantCount int, state string) error {
	return fmt.Errorf(
		"account: refusing to start — %s in %s %s, but conf.d already holds %d tenant(s); "+
			"booting would silently re-issue account_ids from %d (cross-tenant log leak). "+
			"Restore %s from a backup, or (only if this is a genuine fresh install "+
			"with no prior allocations) remove the stale tenant files",
		RegistryFileName, store, state, tenantCount, FirstTenantAccountID, RegistryFileName,
	)
}

// VerifyLiveTokensAgainstRegistry is the startup check that a live logs
// token agrees with the registry (#2884). Every logs token still valid
// carries the account_id it was issued for; the registry must hold that same
// id for that tenant, below next_account_id. A token the registry does not
// account for means the registry went back in time — typically an older
// backup put back with `kubectl apply` — and the next allocation would
// re-issue an id a tenant's live token already uses (ADR-021 cross-tenant
// log leak). tenant-api refuses to start until the registry is restored
// through `tenant-api state restore`, which merges these tokens back in.
//
// Token records are kept only until the token expires, so an id whose
// tokens have all expired is outside this check.
func VerifyLiveTokensAgainstRegistry(ctx context.Context, s statestore.Store, tokens []TokenAllocation) error {
	if len(tokens) == 0 {
		return nil
	}
	data, _, err := s.Get(ctx, RegistryFileName)
	if err != nil {
		return fmt.Errorf("account: cannot read %s from %s for the live-token check: %w", RegistryFileName, s.Describe(), err)
	}
	reg, err := Parse(data)
	if err != nil {
		return fmt.Errorf("account: %s in %s: %w", RegistryFileName, s.Describe(), err)
	}
	var problems []string
	for _, tk := range tokens {
		got, ok := reg.Lookup(tk.TenantID)
		switch {
		case tk.AccountID >= reg.NextAccountID:
			problems = append(problems, fmt.Sprintf("token %s of tenant %q carries account_id %d, at/above next_account_id %d",
				tk.TokenID, tk.TenantID, tk.AccountID, reg.NextAccountID))
		case !ok:
			problems = append(problems, fmt.Sprintf("token %s of tenant %q carries account_id %d, but the registry has no allocation for the tenant",
				tk.TokenID, tk.TenantID, tk.AccountID))
		case got != tk.AccountID:
			problems = append(problems, fmt.Sprintf("token %s of tenant %q carries account_id %d, but the registry allocates %d",
				tk.TokenID, tk.TenantID, tk.AccountID, got))
		}
	}
	if len(problems) == 0 {
		return nil
	}
	sort.Strings(problems)
	return fmt.Errorf("account: refusing to start — %s in %s disagrees with %d live logs token(s); "+
		"the next allocation could re-issue an id in use (cross-tenant log leak). "+
		"Restore the registry with `tenant-api state restore`, which merges these tokens' ids back in "+
		"(the pod cannot start, so run it in a copy of the pod: see the tenant-api README, state store backup and restore):\n  %s",
		RegistryFileName, s.Describe(), len(problems), strings.Join(problems, "\n  "))
}

// VerifyRegistryAgainstHighWater is the startup check against the AccountID
// high-water mark (#2928). A registry counter below the base of the last
// reservation means an older registry was put back; tenant-api refuses to start
// until `tenant-api state restore` merges it, which also lifts the counter
// above the mark. Otherwise a lagging mark — the first start after the mark
// was introduced, or its ConfigMap restored older — is raised to what the
// registry accounts for.
//
// ⚠️ The first start after upgrade therefore takes the registry as it is: a
// registry already rolled back at that moment becomes the baseline.
func VerifyRegistryAgainstHighWater(ctx context.Context, s statestore.Store, hw HighWater) error {
	data, _, err := s.Get(ctx, RegistryFileName)
	if err != nil {
		return fmt.Errorf("account: cannot read %s from %s for the high-water check: %w", RegistryFileName, s.Describe(), err)
	}
	reg, err := Parse(data)
	if err != nil {
		return fmt.Errorf("account: %s in %s: %w", RegistryFileName, s.Describe(), err)
	}
	mark, err := hw.Get(ctx)
	if err != nil {
		return fmt.Errorf("account: read the AccountID high-water mark: %w", err)
	}
	if judgeHighWater(reg.NextAccountID, mark) == hwBehind {
		return fmt.Errorf("account: refusing to start — %s in %s has next_account_id %d, but AccountIDs up to %d have been allocated "+
			"from next_account_id %d (high-water mark); an older registry was put back, and the next allocation could re-issue an id in use (cross-tenant log leak). "+
			"Restore the registry with `tenant-api state restore`, which lifts next_account_id above the mark "+
			"(the pod cannot start, so run it in a copy of the pod: see the tenant-api README, state store backup and restore)",
			RegistryFileName, s.Describe(), reg.NextAccountID, mark.Max, mark.Base)
	}
	return raiseHighWater(ctx, hw, reg.NextAccountID)
}
