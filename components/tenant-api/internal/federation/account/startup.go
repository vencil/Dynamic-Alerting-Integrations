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
	"fmt"
	"os"
	"path/filepath"

	"github.com/vencil/tenant-api/internal/confd"
)

// ListTenantIDs returns the tenant IDs in configDir — one per non-hidden,
// non-`_`-prefixed *.yaml / *.yml file. This mirrors the enumeration
// ListTenants and the threshold-exporter loader use, so callers see exactly the
// set of files that count as tenants (and skip _defaults.yaml, _groups.yaml, the
// _account_registry.yaml itself, etc.).
//
// Shared by the backfill handler and the startup integrity guard; the
// "what counts as a tenant file" rule itself lives once in package confd,
// which every scanner and ValidateTenantID share (no copy to drift).
func ListTenantIDs(configDir string) ([]string, error) {
	entries, err := os.ReadDir(configDir)
	if err != nil {
		return nil, err
	}
	ids := make([]string, 0, len(entries))
	for _, e := range entries {
		if e.IsDir() {
			continue
		}
		id, ok := confd.TenantIDFromFile(e.Name())
		if !ok {
			continue
		}
		ids = append(ids, id)
	}
	return ids, nil
}

// VerifyRegistryNotResetWithFleet is the startup self-check for the
// blank-registry-but-fleet-nonempty corruption (Gemini #2). It returns a
// non-nil error — for the caller to treat as FATAL — when the AccountID
// registry under configDir is blank/missing BUT conf.d already holds ≥1 tenant.
//
// Decision table:
//   - registry present and non-blank        → OK (nil). Normal steady state;
//     Parse's own fail-closed checks guard its content separately.
//   - registry blank/missing, fleet EMPTY   → OK (nil). Genuine Day-0 — the
//     first onboarding will create the registry from the floor. Correct.
//   - registry blank/missing, fleet NON-EMPTY → ERROR. The ledger that those
//     tenants were allocated against is gone; booting would silently re-issue
//     ids from 1000 (cross-tenant leak). Refuse to start.
//
// A configDir that does not exist / cannot be read is reported as an error too
// (a misconfigured mount must not boot silently). Reading the registry file is
// the only I/O on the registry — it does NOT call Parse (a malformed-but-present
// registry is the allocator's fail-closed concern at issuance time, not a
// reason to block startup; this guard is specifically about the blank-reset
// false-Day-0 hazard).
func VerifyRegistryNotResetWithFleet(configDir string) error {
	tenants, err := ListTenantIDs(configDir)
	if err != nil {
		return fmt.Errorf("account: cannot enumerate tenants in %q for registry integrity check: %w", configDir, err)
	}
	if len(tenants) == 0 {
		// Day-0: no tenants yet → a blank/missing registry is expected.
		return nil
	}

	registryPath := filepath.Join(configDir, RegistryFileName)
	data, err := os.ReadFile(registryPath)
	switch {
	case err == nil:
		if isBlank(data) {
			return registryResetError(configDir, len(tenants), "is blank (0 bytes / whitespace only)")
		}
		return nil
	case os.IsNotExist(err):
		return registryResetError(configDir, len(tenants), "is missing")
	default:
		// An unreadable-but-present registry (permissions, I/O) is also a
		// don't-boot condition: we cannot confirm the ledger is intact.
		return fmt.Errorf("account: cannot read %s for registry integrity check: %w", RegistryFileName, err)
	}
}

// registryResetError formats the fail-loud message for a blank/missing registry
// alongside a non-empty fleet, naming the remediation.
func registryResetError(configDir string, tenantCount int, state string) error {
	return fmt.Errorf(
		"account: refusing to start — %s in %q %s, but conf.d already holds %d tenant(s); "+
			"booting would silently re-issue account_ids from %d (cross-tenant log leak). "+
			"Restore %s from git history / backup, or (only if this is a genuine fresh install "+
			"with no prior allocations) remove the stale tenant files",
		RegistryFileName, configDir, state, tenantCount, FirstTenantAccountID, RegistryFileName,
	)
}
