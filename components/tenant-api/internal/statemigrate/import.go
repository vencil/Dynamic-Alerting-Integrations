// Package statemigrate moves tenant-api's own records from their old home,
// files in the conf.d git tree, into the state store (#2856). It runs once
// per record at startup: a record already in the store is never touched, so
// re-running it on every boot is a no-op once the move is done.
package statemigrate

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/vencil/tenant-api/internal/confd"
	"github.com/vencil/tenant-api/internal/federation/account"
	"github.com/vencil/tenant-api/internal/statestore"
)

// History is the git-history read the AccountID floor needs:
// gitops.Writer.HistoricalMaxUint.
type History interface {
	HistoricalMaxUint(ctx context.Context, filename, field string) (uint32, error)
}

// FleetSize reports how many tenants conf.d holds (account.FleetSize).
type FleetSize func() (int, error)

// Report lists what one import pass did, by record key.
type Report struct {
	// Imported records came from conf.d into an empty slot of the store.
	Imported []string
	// Kept records were already in the store; their conf.d copy, if any,
	// is no longer read and can be deleted.
	Kept []string
	// Withheld records were deliberately not imported: a blank or missing
	// AccountID registry while tenants exist. Writing one would hide the
	// lost ledger from the startup check that refuses to boot on it.
	Withheld []string
}

// Import copies each legacy conf.d record into the store when the store
// has no such record yet. hist may be nil (no git): the AccountID floor then
// comes from the file alone.
//
// The AccountID registry's next_account_id is raised to the highest value
// the file ever held in git history, so ids handed out before a revert or a
// hand edit are never handed out again (ADR-021). A registry file that does
// not parse is an error, and a blank or missing one is not imported while
// tenants exist: importing a ledger we cannot read, or an empty one, would
// be the silent reset the startup check exists to prevent.
//
// Two subset files for one tenant (`db-a.yaml` and `db-a.yml`) are an error
// unless the store already holds that tenant's subset: the store keeps one
// record per tenant, and picking either file would silently drop the other.
func Import(ctx context.Context, s statestore.Store, configDir string, hist History, fleet FleetSize) (Report, error) {
	var rep Report
	for _, key := range []string{statestore.KeyGroups, statestore.KeyViews, statestore.KeyFederationPolicy} {
		data, ok, err := readLegacy(filepath.Join(configDir, key))
		if err != nil {
			return rep, err
		}
		if !ok {
			continue
		}
		if err := importOne(ctx, s, key, data, &rep); err != nil {
			return rep, err
		}
	}

	subsets, err := confd.ListTenantFiles(confd.FederationSubsetDir(configDir))
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		return rep, fmt.Errorf("statemigrate: list %s: %w", confd.FederationSubsetDir(configDir), err)
	}
	byTenant := map[string][]string{}
	for _, f := range subsets {
		byTenant[f.ID] = append(byTenant[f.ID], f.Name)
	}
	for id, names := range byTenant {
		if len(names) < 2 {
			continue
		}
		if _, ok, err := s.Get(ctx, statestore.SubsetKey(id)); err != nil {
			return rep, fmt.Errorf("statemigrate: read %s: %w", statestore.SubsetKey(id), err)
		} else if !ok {
			return rep, fmt.Errorf("statemigrate: tenant %q has %d federation subset files in %s (%s); "+
				"keep the one in effect, delete the others, and restart",
				id, len(names), confd.FederationSubsetDir(configDir), strings.Join(names, ", "))
		}
	}
	for _, f := range subsets {
		data, ok, err := readLegacy(filepath.Join(confd.FederationSubsetDir(configDir), f.Name))
		if err != nil {
			return rep, err
		}
		if !ok {
			continue
		}
		if err := importOne(ctx, s, statestore.SubsetKey(f.ID), data, &rep); err != nil {
			return rep, err
		}
	}

	if err := importRegistry(ctx, s, configDir, hist, fleet, &rep); err != nil {
		return rep, err
	}
	return rep, nil
}

func readLegacy(path string) ([]byte, bool, error) {
	data, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil, false, nil
	}
	if err != nil {
		return nil, false, fmt.Errorf("statemigrate: read %s: %w", path, err)
	}
	return data, true, nil
}

// importOne writes data under key if the store has no such record.
func importOne(ctx context.Context, s statestore.Store, key string, data []byte, rep *Report) error {
	kept := false
	changed, err := s.Mutate(ctx, key, statestore.Meta{Entity: "import", Author: "tenant-api"}, func(cur []byte) ([]byte, error) {
		kept = cur != nil
		if kept {
			return nil, nil
		}
		return data, nil
	})
	if err != nil {
		return fmt.Errorf("statemigrate: import %s: %w", key, err)
	}
	switch {
	case changed:
		rep.Imported = append(rep.Imported, key)
	case kept:
		rep.Kept = append(rep.Kept, key)
	}
	return nil
}

func importRegistry(ctx context.Context, s statestore.Store, configDir string, hist History, fleet FleetSize, rep *Report) error {
	// Already moved: skip the git-history read entirely, so it runs on the
	// first boot after the upgrade only.
	if _, ok, err := s.Get(ctx, account.RegistryFileName); err != nil {
		return fmt.Errorf("statemigrate: read %s: %w", account.RegistryFileName, err)
	} else if ok {
		rep.Kept = append(rep.Kept, account.RegistryFileName)
		return nil
	}
	data, _, err := readLegacy(filepath.Join(configDir, account.RegistryFileName))
	if err != nil {
		return err
	}
	var histMax uint32
	if hist != nil {
		histMax, err = hist.HistoricalMaxUint(ctx, account.RegistryFileName, "next_account_id")
		if err != nil {
			return fmt.Errorf("statemigrate: read the AccountID high-water mark from git history: %w", err)
		}
	}
	if len(strings.TrimSpace(string(data))) == 0 {
		// Blank or missing. With tenants in conf.d their ids came from a
		// ledger that is gone: leave the slot empty so the startup check
		// (account.VerifyRegistryNotResetWithFleet) and the allocator refuse
		// it, instead of importing an empty ledger that would pass both.
		if fleet != nil {
			n, err := fleet()
			if err != nil {
				return fmt.Errorf("statemigrate: count tenants for the AccountID registry check: %w", err)
			}
			if n > 0 {
				rep.Withheld = append(rep.Withheld, account.RegistryFileName)
				return nil
			}
		}
		if histMax == 0 {
			return nil // never existed: nothing to carry over
		}
		// No tenants left, but git history remembers ids: carry the
		// high-water mark over so they are not handed out again.
	}
	reg, err := account.Parse(data)
	if err != nil {
		return fmt.Errorf("statemigrate: conf.d/%s does not parse, refusing to import it: %w", account.RegistryFileName, err)
	}
	if histMax > reg.NextAccountID {
		reg.NextAccountID = histMax
	}
	next, err := reg.Marshal()
	if err != nil {
		return err
	}
	return importOne(ctx, s, account.RegistryFileName, next, rep)
}
